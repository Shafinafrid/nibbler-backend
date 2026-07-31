"""
Pull usable pictures out of an uploaded book.

A nibble card can carry a picture from the source itself — a diagram, a plate,
a chart the author drew. That is always preferable to a generated one: it is
what the author actually put on the page, and it costs nothing to produce.

The hard part is not extraction, it is REJECTION. A book's image stream is
mostly furniture: rules, drop caps, publisher logos, the running ornament on
every chapter head. Handing those to a card would look broken. So everything
here is a filter:

  * anything below MIN_W x MIN_H is decoration, not an illustration
  * anything with a wildly thin aspect ratio is a rule or a border
  * identical bytes appearing on more than REPEAT_LIMIT pages is furniture
    (a logo in the footer is the single most common false positive)
  * a hard cap of MAX_IMAGES per book, so a 600-page illustrated volume can't
    fill S3 or the item row

Each survivor is uploaded to S3 and described by the text around it, which is
what lets the session generator decide whether a picture is relevant to the
card it is writing.
"""

import io
import hashlib
import logging
import zipfile
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)

# Below this, it is a glyph, a rule or an icon — never an illustration.
MIN_W, MIN_H = 320, 220
# A 20:1 sliver is a horizontal rule, not a picture.
MAX_ASPECT = 6.0
# The same bytes on more than this many pages is a header/footer/logo.
REPEAT_LIMIT = 3
# Nothing about a nibble needs more than this many candidates.
MAX_IMAGES = 60
# Anything past this is almost certainly an embedded font atlas or a scan of a
# whole page rather than a figure.
MAX_BYTES = 8 * 1024 * 1024

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def _dims(data: bytes) -> Optional[tuple]:
    """(width, height) or None when the bytes aren't a decodable image."""
    try:
        from PIL import Image  # imported lazily so the module loads without Pillow
    except ImportError:
        logger.warning("Pillow not installed — image extraction disabled")
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return None


def _acceptable(data: bytes) -> Optional[tuple]:
    """Run the rejection filters. Returns (w, h) when the image is usable."""
    if not data or len(data) > MAX_BYTES:
        return None
    size = _dims(data)
    if not size:
        return None
    w, h = size
    if w < MIN_W or h < MIN_H:
        return None
    if max(w, h) / max(1, min(w, h)) > MAX_ASPECT:
        return None
    return (w, h)


def _drop_repeats(found: List[Dict]) -> List[Dict]:
    """Remove furniture: identical bytes recurring across many pages."""
    counts: Dict[str, int] = {}
    for f in found:
        counts[f["hash"]] = counts.get(f["hash"], 0) + 1
    kept, seen = [], set()
    for f in found:
        if counts[f["hash"]] > REPEAT_LIMIT:
            continue          # logo / ornament
        if f["hash"] in seen:
            continue          # same picture twice — keep the first
        seen.add(f["hash"])
        kept.append(f)
    return kept


def extract_pdf_images(file_bytes: bytes, page_texts: Optional[List[str]] = None) -> List[Dict]:
    """Candidate figures from a PDF, each with the text of the page it sat on.

    That page text is the only signal available for "is this picture about the
    idea on this card" — the alternative would be running a vision model over
    every figure in every book, which costs more than the feature is worth.
    """
    from pypdf import PdfReader

    out: List[Dict] = []
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
    except Exception as e:
        logger.warning("Could not open PDF for image extraction: %s", e)
        return []

    for page_no, page in enumerate(reader.pages, start=1):
        if len(out) >= MAX_IMAGES:
            break
        try:
            images = list(page.images)
        except Exception:
            continue                      # encrypted / malformed page — skip it
        for img in images:
            if len(out) >= MAX_IMAGES:
                break
            try:
                data = img.data
            except Exception:
                continue
            size = _acceptable(data)
            if not size:
                continue
            context = ""
            if page_texts and page_no <= len(page_texts):
                context = (page_texts[page_no - 1] or "")[:600]
            out.append({
                "data": data,
                "ext": (img.name.rsplit(".", 1)[-1].lower() if "." in (img.name or "") else "png"),
                "page": page_no,
                "w": size[0], "h": size[1],
                "context": context,
                "hash": hashlib.sha1(data).hexdigest(),
            })
    return _drop_repeats(out)


def extract_epub_images(file_bytes: bytes) -> List[Dict]:
    """Candidate figures from an EPUB. An EPUB is a zip, so this needs no
    parser — the images are plain files inside it."""
    out: List[Dict] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(file_bytes))
    except Exception as e:
        logger.warning("Could not open EPUB for image extraction: %s", e)
        return []

    with zf:
        for name in zf.namelist():
            if len(out) >= MAX_IMAGES:
                break
            if not name.lower().endswith(_IMG_EXTS):
                continue
            # The cover is the one picture guaranteed NOT to illustrate an idea.
            if "cover" in name.lower():
                continue
            try:
                data = zf.read(name)
            except Exception:
                continue
            size = _acceptable(data)
            if not size:
                continue
            out.append({
                "data": data,
                "ext": name.rsplit(".", 1)[-1].lower(),
                "page": None,
                "w": size[0], "h": size[1],
                "context": "",
                "hash": hashlib.sha1(data).hexdigest(),
            })
    return _drop_repeats(out)


def extract_and_store(file_bytes: bytes, filename: str, item_id: str,
                      page_texts: Optional[List[str]] = None) -> List[Dict]:
    """Extract, upload to S3, and return the rows to persist on the item.

    Never raises: a book whose pictures can't be read is still a perfectly good
    book, and failing the whole upload over a figure would be absurd.
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

    s3 = S3Service()
    stored: List[Dict] = []
    for i, f in enumerate(found):
        try:
            ext = f["ext"] if f["ext"] in ("jpg", "jpeg", "png", "gif", "webp") else "png"
            ref = s3.upload_file(
                f["data"],
                f"book-images/{item_id}/{i}.{ext}",
                f"image/{'jpeg' if ext == 'jpg' else ext}",
            )
            stored.append({
                "ref": ref,
                "page": f["page"],
                "w": f["w"], "h": f["h"],
                "context": f["context"],
            })
        except Exception as e:
            logger.warning("Could not store image %s for %s: %s", i, item_id, e)

    logger.info("Extracted %s usable images for item %s", len(stored), item_id)
    return stored
