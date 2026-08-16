"""
Pull usable pictures out of an uploaded book, with enough provenance to decide
later whether one belongs on a given card.

A nibble card may carry a picture from the source itself — a diagram, a plate,
a chart the author drew. Never a generated one: AI image generation is off for
every tier, and a card with no relevant picture is a correct, expected outcome.

**The hard part is not extraction, it is REJECTION.** A book's image stream is
mostly furniture: rules, drop caps, publisher logos, the ornament on every
chapter head. Handing those to a card looks broken. So most of this file is
filters, and the filters are deliberately harsh — a missing figure costs
nothing, a publisher's logo on a card costs the card.

The second job is CONTEXT. Deciding "is this picture about the idea on this
card" needs something to compare, and running a vision model over every figure
in every book costs more than the feature is worth. So each survivor carries
the text around it: the page it sat on (PDF), or its caption, alt text and
chapter (EPUB). That text is what the shortlist matches against.

The third job is POSITION. Story mode serves a book in order, and a picture
from chapter 20 shown during chapter 3 is a spoiler — a character, a location,
a plot beat the reader has not reached. Every candidate therefore records how
far through the book it sits, as a 0..1 fraction, and Story selection refuses
anything past the reader's position.

Nothing here raises. A book whose pictures cannot be read is still a perfectly
good book, and failing an upload over a figure would be absurd.
"""

import hashlib
import io
import logging
import posixpath
import re
import zipfile
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── rejection thresholds ────────────────────────────────────────────────────
# Below this, it is a glyph, a rule, an icon or a drop cap — never a figure.
MIN_W, MIN_H = 320, 220
# A 6:1 sliver is a horizontal rule or a border, not a picture.
MAX_ASPECT = 6.0
# The same picture on more than this many pages is a header/footer/logo.
REPEAT_LIMIT = 3
# A hard cap per book, so a 600-page illustrated volume cannot fill S3 or
# bloat the item row. Applied AFTER deduplication — capping during collection
# let sixty copies of a publisher's logo consume the whole budget and hide the
# one real figure on page 61.
MAX_IMAGES = 60
# How many raw candidates to look at before deduplication. Bounded separately
# so the scan itself cannot run away on a 900-page volume.
MAX_SCAN = 400
# Past this it is almost certainly a font atlas or a scan of a whole page.
MAX_BYTES = 8 * 1024 * 1024
# Total bytes held in memory across all candidates during one extraction.
# MAX_SCAN x MAX_BYTES would be 3 GiB and MAX_IMAGES x MAX_BYTES still 480 MiB
# — enough to OOM the single Railway process on one upload. This is the bound
# that actually protects the box.
MAX_TOTAL_BYTES = 64 * 1024 * 1024
# A figure with almost no tonal variation is a blank panel, a solid fill or a
# scanning artefact. It passes every dimensional filter and looks like a bug on
# a card.
MIN_BLANK_VARIANCE = 12.0
# A page-one page carrying one picture and fewer words than this is a cover,
# not a page that happens to have a figure on it.
COVER_MAX_WORDS = 25
# Two images whose perceptual hashes differ by fewer bits than this are the
# same picture re-encoded — common when a publisher ships a figure at two
# resolutions. Byte-equality alone misses these entirely.
PHASH_DISTANCE = 4
# Bounded so a page of dense text cannot push a multi-kilobyte string into the
# item row, or into a model prompt.
MAX_CONTEXT_CHARS = 600
MAX_CAPTION_CHARS = 300

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_MIME = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "gif": "image/gif", "webp": "image/webp",
}

# Words that mark a picture as furniture regardless of its size. A 400x300
# publisher logo passes every dimensional filter.
_FURNITURE_HINTS = ("logo", "colophon", "publisher", "imprint", "ornament",
                    "divider", "rule", "border", "icon", "bullet", "swash")


# ── low-level image inspection ──────────────────────────────────────────────

def _pil():
    """Pillow, or None. Imported lazily so this module loads without it and
    image extraction simply stays dark rather than breaking uploads."""
    try:
        from PIL import Image
        return Image
    except ImportError:  # pragma: no cover — Pillow is in requirements
        logger.warning("Pillow not installed — image extraction disabled")
        return None


def _dims(data: bytes) -> Optional[Tuple[int, int]]:
    Image = _pil()
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def perceptual_hash(data: bytes) -> Optional[int]:
    """A 64-bit average hash: 8x8 greyscale, one bit per pixel vs the mean.

    Deliberately the simplest perceptual hash there is. It catches the case
    that actually occurs — the same figure re-encoded, rescaled or lightly
    recompressed — without pretending to judge whether two different pictures
    are "similar", which is a job for a model, not for eight rows of pixels.
    """
    Image = _pil()
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("L").resize((8, 8), Image.BILINEAR)
            pixels = list(small.getdata())
    except Exception:
        return None
    if not pixels:
        return None
    mean = sum(pixels) / len(pixels)
    bits = 0
    for i, px in enumerate(pixels):
        if px >= mean:
            bits |= 1 << i
    return bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _grey_stats(data: bytes):
    """(near_white_ratio, distinct_buckets, variance) or None.

    Computed from the FULL greyscale HISTOGRAM, with no resampling at all.
    Both obvious shortcuts are wrong here:

      * a smoothing resample averages fine detail back toward the local mean,
        so a detailed photograph measures as flat as a blank panel;
      * NEAREST avoids that but ALIASES — sampling a regularly-patterned image
        on a fixed stride can land on the same phase every time and report a
        busy picture as uniform. That is not hypothetical: it classified a
        noisy 640x480 figure as blank.

    A histogram has neither failure mode, counts every pixel exactly, and PIL
    computes it in C.
    """
    Image = _pil()
    if Image is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            hist = im.convert("L").histogram()[:256]
    except Exception:
        return None
    total = sum(hist)
    if total <= 0:
        return None

    near_white = sum(hist[235:]) / total
    mean = sum(i * n for i, n in enumerate(hist)) / total
    variance = sum(n * (i - mean) ** 2 for i, n in enumerate(hist)) / total
    # RAW levels in real use, not 8-wide buckets. A bucket that wide cannot
    # tell a diagram from a low-contrast photograph: a high-key picture living
    # entirely between 190 and 255 occupies about seven buckets and looked like
    # flat artwork. Counting actual levels separates them properly — a diagram
    # uses a handful of exact ink values, a photograph uses a continuum.
    # The floor keeps a stray speck of ink from making a blank page look rich.
    floor = total * 0.001
    distinct = sum(1 for n in hist if n > floor)
    return (near_white, distinct, variance)


def is_blank(data: bytes) -> bool:
    """True for a solid fill, an empty panel or a scanning artefact.

    These pass every dimensional filter — a 600x400 sheet of white is a
    perfectly well-formed image — and look like a broken card. Judged by
    variance rather than by colour, so a black separator panel and a white one
    are both caught.
    """
    stats = _grey_stats(data)
    if stats is None:
        return False
    _near_white, distinct, variance = stats
    return distinct <= 4 and variance < MIN_BLANK_VARIANCE


def visual_kind(data: bytes) -> str:
    """"diagram" or "photo" — display guidance, not a claim about content.

    The app crops photographs to fill a card (`cover`) but must letterbox a
    diagram (`contain`), because cropping the axis off a chart destroys it.
    The distinction is made cheaply: diagrams are mostly flat background with
    few distinct tones, photographs are not. A wrong guess costs a slightly
    odd crop, so a cheap heuristic is the right amount of machinery.
    """
    stats = _grey_stats(data)
    if stats is None:
        return "photo"
    near_white, distinct, variance = stats

    # A diagram is built from a handful of exact ink values; a photograph is a
    # continuum. Whiteness ALONE is not the test — a high-key photograph (snow,
    # a white studio backdrop, an overexposed sky) is also mostly near-white,
    # and calling it a diagram letterboxes a picture that should fill the card.
    # So brightness only counts when the tones are also genuinely few.
    if distinct <= 24:
        return "diagram"
    if near_white > 0.45 and distinct <= 40 and variance < 400:
        return "diagram"
    return "photo"


def _acceptable(data: bytes) -> Optional[Tuple[int, int]]:
    """Dimensional filters. Returns (w, h) when the image could be a figure."""
    if not data or len(data) > MAX_BYTES:
        return None
    size = _dims(data)
    if not size:
        return None            # corrupt or not an image at all
    w, h = size
    if w < MIN_W or h < MIN_H:
        return None            # decoration
    if max(w, h) / max(1, min(w, h)) > MAX_ASPECT:
        return None            # a rule or a border
    if is_blank(data):
        return None            # a solid panel or a scanning artefact
    return (w, h)


def _looks_like_furniture(*texts: Optional[str]) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(hint in blob for hint in _FURNITURE_HINTS)


def _clean(text: Optional[str], limit: int) -> str:
    return " ".join((text or "").split())[:limit]


# ── deduplication ───────────────────────────────────────────────────────────

def dedupe(found: List[Dict]) -> List[Dict]:
    """Drop furniture and duplicates, preserving source order.

    Three passes, because they catch different things:
      * repeats — identical bytes on more than REPEAT_LIMIT pages is a running
        header or a logo, which is the single most common false positive;
      * exact — the same checksum twice, keep the first;
      * visual — a near-identical perceptual hash, which is the same figure
        shipped at two resolutions.
    """
    counts: Dict[str, int] = {}
    for f in found:
        counts[f["checksum"]] = counts.get(f["checksum"], 0) + 1

    kept: List[Dict] = []
    seen_checksums = set()
    seen_hashes: List[int] = []
    for f in found:
        if counts[f["checksum"]] > REPEAT_LIMIT:
            continue
        if f["checksum"] in seen_checksums:
            continue
        ph = f.get("phash")
        if ph is not None and any(hamming(ph, prev) <= PHASH_DISTANCE for prev in seen_hashes):
            continue
        seen_checksums.add(f["checksum"])
        if ph is not None:
            seen_hashes.append(ph)
        kept.append(f)
    return kept


def candidate_id(checksum: str, item_id: str = "") -> str:
    """A short, opaque, stable id, unique WITHIN a book AND across books.

    Opaque matters: this is the ONLY image identifier a model ever sees, and it
    must not be a path, a URL or anything the model could construct.

    The book id is mixed in because a checksum alone is not unique across a
    library — the same figure in two uploaded books produced the same id, and
    delivery then had two rows to choose from. Salting by item makes each
    book's copy its own object while keeping the id stable across
    re-processing of the same book.
    """
    return "img_" + hashlib.sha1(("%s:%s" % (item_id, checksum)).encode()).hexdigest()[:12]


# ── PDF ─────────────────────────────────────────────────────────────────────

# "Figure 4.1 — ..." / "Fig. 2: ..." / "Table 3 ..." — the line that names a
# figure describes it far better than the whole page's prose does.
_CAPTION_BODY = (
    r"(?:fig(?:ure)?|table|plate|chart|diagram|exhibit)\.?\s*[\dIVXivx]+[.:)]?\s+.{0,200}"
)
# A caption on its own line is the strong case and is tried first.
_CAPTION_LINE_RE = re.compile(r"^\s*(" + _CAPTION_BODY + r")", re.IGNORECASE | re.MULTILINE)
# But PDF text extraction frequently reflows a page into one long run, so the
# caption arrives mid-line. Anchoring only to line starts silently found
# nothing on exactly the documents this matters for.
_CAPTION_ANY_RE = re.compile(r"(" + _CAPTION_BODY + r")", re.IGNORECASE)


def _caption_from(page_text: str) -> str:
    text = page_text or ""
    m = _CAPTION_LINE_RE.search(text) or _CAPTION_ANY_RE.search(text)
    return _clean(m.group(1), MAX_CAPTION_CHARS) if m else ""


def pdf_page_texts(file_bytes: bytes, limit: int = 400) -> List[str]:
    """Plain text per page, for describing the figures on it.

    Separate from `text_extract.pdf_to_structured_text`, which returns one
    reflowed document because Story mode serves it verbatim. Here the page
    BOUNDARY is the whole point — it is what associates a figure with words —
    so this is a deliberately dumb per-page extraction rather than a reuse of
    the structural one. Never raises; a page that will not extract yields "".
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception:
        return []
    texts: List[str] = []
    for page in reader.pages[:limit]:
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            texts.append("")
    return texts


def _word_positions(texts: List[str]) -> Optional[List[float]]:
    """Fraction of the book's WORDS up to the END of each unit, or None.

    Story progress is stored as a word offset (`library_items.story_progress`),
    so a spoiler guard expressed in pages or spine documents is comparing
    incompatible units — a picture on page 12 of 300 is not "4% through" if the
    first 40 pages are front matter. Positions are therefore measured in words,
    the same currency the reader's progress is in.

    **The END of the unit, not the start.** Neither PDF nor EPUB extraction
    tells us where on a page or in a chapter a figure actually sits, so the
    only safe assumption is the furthest point it could be. Using the start
    gave every figure on page one a position of 0.0 — including one at the very
    bottom of a long page, which was then offered to a reader 10% in. A figure
    is treated as being at the end of the unit that contains it, so the reader
    must have finished that unit before it can appear.

    Returns None when there is no text to measure, and callers then record the
    position basis as unusable rather than substituting a page fraction — the
    guard refuses what it cannot compare.
    """
    counts = [len((t or "").split()) for t in texts]
    total = sum(counts)
    if total <= 0:
        return None
    positions, seen = [], 0
    for n in counts:
        seen += n
        positions.append(round(seen / total, 4))
    return positions


def extract_pdf_images(file_bytes: bytes, page_texts: Optional[List[str]] = None) -> List[Dict]:
    """Candidate figures from a PDF, each carrying its page's text and caption."""
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        return []

    out: List[Dict] = []
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        pages = reader.pages
        total_pages = max(1, len(pages))
    except Exception as e:
        logger.warning("Could not open PDF for image extraction: %s", e)
        return []

    word_positions = _word_positions(page_texts) if page_texts else None
    basis = "words" if word_positions else "pages"
    order = 0
    budget = MAX_TOTAL_BYTES

    for page_no, page in enumerate(pages, start=1):
        # Scan and byte budgets, NOT the retained cap: capping here is what let
        # sixty copies of a logo fill the quota and hide the real figure on
        # page 61. MAX_IMAGES is applied after deduplication instead.
        if len(out) >= MAX_SCAN or budget <= 0:
            break
        try:
            # NOT list(page.images): pypdf's sequence decodes lazily on index,
            # so materialising it pulls every image on the page into memory
            # before MAX_SCAN or the byte budget gets a say. A single
            # pathological page could therefore blow both. len() is cheap — it
            # counts XObjects without decoding any of them.
            images = page.images
            image_count = len(images)
        except Exception:
            continue                       # encrypted or malformed page
        page_text = ""
        if page_texts and page_no <= len(page_texts):
            page_text = page_texts[page_no - 1] or ""
        # A cover is a page of artwork with no prose on it. That is the signal
        # available here: pypdf exposes an image's PIXEL dimensions but not the
        # rectangle it is DRAWN into, so "does it fill the page" cannot be
        # measured — comparing 600x400 pixels against 595x842 points compares
        # nothing at all, which is how a full-bleed cover slipped through.
        # Word count is a property of the page itself and needs no geometry.
        # A caption is decisive the other way: a page that names "Figure 1" is
        # discussing a figure, however few words surround it.
        page_caption = _caption_from(page_text)
        looks_like_cover_page = (
            page_no == 1
            and image_count == 1
            and len(page_text.split()) < COVER_MAX_WORDS
            and not page_caption
        )

        for img in images:
            if len(out) >= MAX_SCAN or budget <= 0:
                break
            try:
                data = img.data
                name = img.name or ""
            except Exception:
                continue
            size = _acceptable(data)
            if not size:
                continue
            if _looks_like_furniture(name):
                continue
            # The cover illustrates the book, not any idea inside it. EPUBs
            # name theirs; PDFs do not. All three conditions are needed: page
            # one alone also describes a book that opens with a real figure,
            # and rejecting that loses genuine content.
            if looks_like_cover_page:
                continue
            # Checked BEFORE accepting, not after. Subtracting afterwards let
            # one more image through on an already-exhausted budget — with
            # MAX_BYTES at 8 MiB that is a 72 MiB peak against a 64 MiB limit.
            if len(data) > budget:
                logger.info("image byte budget reached for this book — stopping the scan")
                budget = 0
                break
            budget -= len(data)
            checksum = hashlib.sha1(data).hexdigest()
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else "png"
            out.append({
                "data": data,
                "ext": ext if ext in _MIME else "png",
                "checksum": checksum,
                "phash": perceptual_hash(data),
                "order": order,
                "w": size[0], "h": size[1],
                "page": page_no,
                "spine": None,
                "chapter": None,
                "href": name or None,
                "context": _clean(page_text, MAX_CONTEXT_CHARS),
                "caption": page_caption,
                "alt": "",
                # Measured in WORDS where possible — the same unit as the
                # reader's story progress. See _word_positions.
                # The END of this page — see _word_positions. Placement within
                # a page is unknown, so the safe assumption is the furthest
                # point the figure could occupy.
                "position": (word_positions[page_no - 1] if word_positions
                             else round(page_no / total_pages, 4)),
                "position_basis": basis,
            })
            order += 1
    return dedupe(out)[:MAX_IMAGES]


# ── EPUB ────────────────────────────────────────────────────────────────────

def _epub_spine(zf: zipfile.ZipFile) -> List[str]:
    """Document paths in READING order, via container.xml → OPF → spine.

    Reading order is not archive order, and it is what "how far through the
    book" means for an EPUB. Falls back to sorted document paths when a
    publisher's OPF is malformed — the same fallback the text extractor uses.
    """
    from bs4 import BeautifulSoup

    names = set(zf.namelist())
    try:
        container = BeautifulSoup(zf.read("META-INF/container.xml"), "html.parser")
        opf_path = container.find("rootfile")["full-path"]
        base = posixpath.dirname(opf_path)
        opf = BeautifulSoup(zf.read(opf_path), "html.parser")
        manifest = {}
        for item in opf.find_all("item"):
            iid, href = item.get("id"), item.get("href")
            if iid and href:
                manifest[iid] = posixpath.normpath(posixpath.join(base, href))
        ordered = []
        for ref in opf.find_all("itemref"):
            path = manifest.get(ref.get("idref"))
            if path and path in names:
                ordered.append(path)
        if ordered:
            return ordered
    except Exception as e:
        logger.info("EPUB spine unreadable (%s) — falling back to sorted paths", e)

    return sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))


def _epub_figures(zf: zipfile.ZipFile, doc_path: str) -> List[Dict]:
    """Every <img> in one chapter, with its alt text, figcaption and heading.

    Walking the document rather than the zip's file list is what makes an
    EPUB's images describable at all: the archive knows a file is called
    `img_03.jpg`, the document knows it is "Figure 3: the feedback loop"
    inside the chapter "Small Habits, Big Results".
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(zf.read(doc_path), "html.parser")
    except Exception:
        return []

    heading = ""
    h = soup.find(["h1", "h2", "h3"])
    if h:
        heading = _clean(h.get_text(" "), 120)

    base = posixpath.dirname(doc_path)
    figures = []
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if not src or src.startswith("data:"):
            continue
        href = posixpath.normpath(posixpath.join(base, src))
        caption = ""
        fig = img.find_parent("figure")
        if fig is not None:
            cap = fig.find("figcaption")
            if cap is not None:
                caption = _clean(cap.get_text(" "), MAX_CAPTION_CHARS)
        if not caption:
            # Some publishers use a sibling <p class="caption"> instead.
            nxt = img.find_next_sibling()
            if nxt is not None and "caption" in " ".join(nxt.get("class") or []).lower():
                caption = _clean(nxt.get_text(" "), MAX_CAPTION_CHARS)

        # The surrounding paragraph is the "nearby text" — the EPUB equivalent
        # of a PDF's page text.
        context = ""
        holder = img.find_parent(["figure", "div", "section", "p"]) or img.parent
        if holder is not None:
            context = _clean(holder.get_text(" "), MAX_CONTEXT_CHARS)

        figures.append({
            "href": href,
            "alt": _clean(img.get("alt"), MAX_CAPTION_CHARS),
            "caption": caption,
            "context": context,
            "chapter": heading,
        })
    return figures


def extract_epub_images(file_bytes: bytes) -> List[Dict]:
    """Candidate figures from an EPUB, in reading order, with their captions."""
    out: List[Dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except Exception as e:
        logger.warning("Could not open EPUB for image extraction: %s", e)
        return []

    with zf:
        names = set(zf.namelist())
        spine = _epub_spine(zf)
        total = max(1, len(spine))

        # Word counts per spine document, so a figure's position is measured in
        # the same unit as the reader's story progress. A spine fraction would
        # not be: a book of 40 short chapters and one enormous one is not 50%
        # read at chapter 20.
        doc_words = []
        for doc_path in spine:
            try:
                from bs4 import BeautifulSoup
                doc_words.append(BeautifulSoup(zf.read(doc_path), "html.parser").get_text(" "))
            except Exception:
                doc_words.append("")
        word_positions = _word_positions(doc_words)
        basis = "words" if word_positions else "spine"

        order = 0
        budget = MAX_TOTAL_BYTES
        seen_hrefs = set()

        for spine_index, doc_path in enumerate(spine):
            if len(out) >= MAX_SCAN or budget <= 0:
                break
            for fig in _epub_figures(zf, doc_path):
                if len(out) >= MAX_SCAN or budget <= 0:
                    break
                href = fig["href"]
                if href in seen_hrefs or href not in names:
                    continue
                if not href.lower().endswith(_IMG_EXTS):
                    continue
                # The cover is the one picture guaranteed NOT to illustrate an
                # idea inside the book.
                if "cover" in href.lower() or _looks_like_furniture(href, fig["alt"], fig["caption"]):
                    continue
                try:
                    data = zf.read(href)
                except Exception:
                    continue
                size = _acceptable(data)
                if not size:
                    continue
                # Checked BEFORE accepting, exactly as in the PDF loop.
                # Subtracting first lets one more image through on an already
                # exhausted budget — with MAX_BYTES at 8 MiB that is a 72 MiB
                # peak against a 64 MiB limit.
                if len(data) > budget:
                    logger.info("image byte budget reached for this book — stopping the scan")
                    budget = 0
                    break
                seen_hrefs.add(href)
                budget -= len(data)
                checksum = hashlib.sha1(data).hexdigest()
                ext = href.rsplit(".", 1)[-1].lower()
                out.append({
                    "data": data,
                    "ext": ext if ext in _MIME else "png",
                    "checksum": checksum,
                    "phash": perceptual_hash(data),
                    "order": order,
                    "w": size[0], "h": size[1],
                    "page": None,
                    "spine": spine_index,
                    "chapter": fig["chapter"] or None,
                    "href": href,
                    "context": fig["context"],
                    "caption": fig["caption"],
                    "alt": fig["alt"],
                    # The END of this spine document — placement within a
                    # chapter is unknown, so assume the last word of it.
                    "position": (word_positions[spine_index] if word_positions
                                 else round((spine_index + 1) / total, 4)),
                    "position_basis": basis,
                })
                order += 1
    return dedupe(out)[:MAX_IMAGES]


# ── extraction + storage ────────────────────────────────────────────────────

def image_key(user_id: str, item_id: str, cid: str, ext: str, attempt_token: Optional[str] = None) -> str:
    """Private S3 key, scoped by owner THEN book THEN (Task 2 closeout,
    Verified Blocker 3) the worker attempt that uploaded it.

    The owner/book prefix order is a safety property, not a filing
    preference: deletion walks `<user_id>/<item_id>/`, so a key built this
    way cannot be made to address another user's object however the ids
    are manipulated. The ATTEMPT segment is what makes two different
    attempts' uploads for the same book structurally unable to collide —
    a stale attempt's own compensating cleanup can delete only ITS OWN
    attempt-scoped objects, never a newer attempt's live ones at what
    would otherwise be the identical path. `attempt_token=None` (legacy
    callers) falls back to the un-scoped path for backward compatibility.
    """
    if attempt_token:
        return "book-images/%s/%s/%s/%s.%s" % (user_id, item_id, attempt_token, cid, ext)
    return "book-images/%s/%s/%s.%s" % (user_id, item_id, cid, ext)


def extract_and_store(
    file_bytes: bytes,
    filename: str,
    item_id: str,
    user_id: str,
    page_texts: Optional[List[str]] = None,
    attempt_token: Optional[str] = None,
) -> List[Dict]:
    """Extract, upload privately to S3, and return the rows to persist.

    NEVER RAISES. Image extraction is a garnish on a pipeline whose real job is
    text: a corrupt figure, a missing Pillow, a dead S3 must all end with a
    normal text-only book rather than a failed upload. Every failure path here
    returns a list — possibly empty — and logs.
    """
    from app.services.s3_service import S3Service

    lower = (filename or "").lower()
    try:
        if lower.endswith(".epub"):
            found = extract_epub_images(file_bytes)
        else:
            found = extract_pdf_images(file_bytes, page_texts)
    except Exception as e:
        logger.warning("Image extraction failed for %s: %s", item_id, e)
        return []

    if not found:
        return []

    try:
        s3 = S3Service()
    except Exception as e:
        logger.warning("S3 unavailable — no images stored for %s: %s", item_id, e)
        return []

    stored: List[Dict] = []
    for f in found:
        cid = candidate_id(f["checksum"], item_id)
        ext = f["ext"]
        key = image_key(user_id, item_id, cid, ext, attempt_token=attempt_token)
        try:
            s3.upload_file(f["data"], key, _MIME.get(ext, "image/png"))
        except Exception as e:
            logger.warning("Could not store image %s for %s: %s", cid, item_id, e)
            continue
        stored.append({
            "id": cid,
            "item_id": item_id,
            "user_id": user_id,
            "key": key,
            "mime": _MIME.get(ext, "image/png"),
            "checksum": f["checksum"],
            "order": f["order"],
            "w": f["w"], "h": f["h"],
            "page": f["page"],
            "spine": f["spine"],
            "chapter": f["chapter"],
            "href": f["href"],
            "context": f["context"],
            "caption": f["caption"],
            "alt": f["alt"],
            "position": f["position"],
            # "words" | "pages" | "spine". The Story guard only trusts "words",
            # because that is the unit story_progress is stored in.
            "position_basis": f.get("position_basis") or "pages",
            "visual": visual_kind(f["data"]),
        })

    logger.info("Stored %d usable images for item %s", len(stored), item_id)
    return stored


def delete_stored(images: Optional[List[Dict]]) -> None:
    """Best-effort removal of objects we uploaded but could not record.

    Called when persisting the rows fails after the uploads succeeded. Without
    it, a database error leaves paid-for objects in the bucket that nothing
    knows about — invisible to book deletion, invisible to account erasure, and
    therefore permanent.
    """
    if not images:
        return
    keys = [(img or {}).get("key") for img in images if (img or {}).get("key")]
    try:
        from app.services.s3_service import S3Service
        s3 = S3Service()
    except Exception as e:
        # Returning quietly here was the worst outcome available: the objects
        # exist, nothing references them, and no one is told. Name every key
        # so they can be removed by hand.
        logger.error(
            "ORPHANED IMAGE OBJECTS: could not build an S3 client to clean up "
            "after a failed write (%s). %d object(s) are now unreferenced and "
            "need manual deletion: %s", e, len(keys), keys,
        )
        return
    for img in images:
        key = (img or {}).get("key")
        if not key:
            continue
        try:
            if not s3.delete_file(key):
                logger.error("ORPHANED IMAGE OBJECT: %s could not be deleted and is "
                             "now unreferenced", key)
        except Exception as e:  # noqa: BLE001 — cleanup must never raise
            logger.error("ORPHANED IMAGE OBJECT: %s (%s) — needs manual deletion", key, e)
