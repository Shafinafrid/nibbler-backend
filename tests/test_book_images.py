"""
Book-image extraction, relevance, ownership, delivery and deletion (Part 2).

Real PDFs and EPUBs are built in memory rather than mocked, because the things
worth testing here are exactly the things a mock would paper over: whether a
publisher's logo on every page is recognised as furniture, whether an EPUB's
figures come back in reading order with their captions, whether a 30x30 icon is
rejected. A fake dict of "image candidates" would prove none of that.

The security properties get the most attention, because their failure modes are
the ones that matter: a picture from another user's book on your card is a data
breach, and a picture from chapter 20 during chapter 3 is a spoiler that cannot
be un-seen. Every one of those paths is tested from the outside.

    .venv/bin/python tests/test_book_images.py
"""

import io
import os
import sys
import zipfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))

import hermetic  # noqa: E402,F401 — must precede any `app.` import

from llm_fakes import Checks  # noqa: E402

from app.services import image_extract, image_select  # noqa: E402

c = Checks("Book images")


# ── fixtures ────────────────────────────────────────────────────────────────

def png_bytes(w, h, colour=(120, 90, 60), noise=False):
    """A real PNG. `noise` makes it photograph-like rather than a flat panel."""
    from PIL import Image
    im = Image.new("RGB", (w, h), colour)
    if noise:
        px = im.load()
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                px[x, y] = ((x * 7 + y * 13) % 255, (x * 3) % 255, (y * 5) % 255)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def build_epub(docs, images, spine=None):
    """A minimal but structurally real EPUB: container → OPF → spine → XHTML."""
    buf = io.BytesIO()
    order = spine or list(docs)
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("META-INF/container.xml",
                    '<?xml version="1.0"?><container><rootfiles><rootfile '
                    'full-path="OEBPS/content.opf"/></rootfiles></container>')
        manifest = "".join(
            '<item id="d%d" href="%s" media-type="application/xhtml+xml"/>' % (i, name)
            for i, name in enumerate(order)
        )
        itemrefs = "".join('<itemref idref="d%d"/>' % i for i in range(len(order)))
        zf.writestr("OEBPS/content.opf",
                    '<?xml version="1.0"?><package><manifest>%s</manifest>'
                    '<spine>%s</spine></package>' % (manifest, itemrefs))
        for name, html in docs.items():
            zf.writestr("OEBPS/" + name, html)
        for path, data in images.items():
            zf.writestr("OEBPS/" + path, data)
    return buf.getvalue()


def build_pdf(pages):
    """A real PDF. `pages` is a list of (text, [image_bytes...])."""
    try:
        import fitz
    except ImportError:
        return None
    doc = fitz.open()
    for text, images in pages:
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 72), text or "")
        top = 200
        for data in images:
            page.insert_image(fitz.Rect(72, top, 472, top + 300), stream=data)
            top += 320
    out = doc.tobytes()
    doc.close()
    return out


FIG = png_bytes(600, 400, (40, 80, 160), noise=True)      # a "photo"
DIAGRAM = png_bytes(600, 400, (250, 250, 250))            # flat + near-white
LOGO = png_bytes(400, 300, (10, 10, 10))
TINY = png_bytes(60, 40)
SLIVER = png_bytes(900, 40)
OTHER = png_bytes(640, 480, (200, 30, 90), noise=True)


# ══ extraction: what survives and what does not ═════════════════════════════

c.ok(image_extract._acceptable(FIG) == (600, 400), "a real figure passes the size filters")
c.ok(image_extract._acceptable(TINY) is None, "a 60x40 icon is rejected as decoration")
c.ok(image_extract._acceptable(SLIVER) is None, "a 900x40 sliver is rejected as a rule")
c.ok(image_extract._acceptable(b"not an image at all") is None,
     "corrupt bytes are rejected rather than raising")
c.ok(image_extract._acceptable(b"") is None, "empty bytes are rejected")
c.ok(image_extract._acceptable(b"x" * (image_extract.MAX_BYTES + 1)) is None,
     "an oversized blob is rejected before decoding")

c.ok(image_extract.visual_kind(DIAGRAM) == "diagram",
     "a flat near-white image is classified as a diagram (letterboxed, not cropped)")
c.ok(image_extract.visual_kind(FIG) == "photo",
     "a busy image is classified as a photograph (cropped to fill)")

ph1, ph2 = image_extract.perceptual_hash(FIG), image_extract.perceptual_hash(FIG)
c.ok(ph1 is not None and ph1 == ph2, "the perceptual hash is stable for identical bytes")
c.ok(image_extract.hamming(ph1, image_extract.perceptual_hash(OTHER)) > 4,
     "two genuinely different pictures hash far apart")

cid = image_extract.candidate_id("abc123")
c.ok(cid.startswith("img_") and cid == image_extract.candidate_id("abc123"),
     "candidate ids are opaque and stable")
c.ok(image_extract.candidate_id("abc123") != image_extract.candidate_id("abc124"),
     "different content gets different ids")

# Dedup: furniture, exact repeats, and the same figure re-encoded.
rows = [{"checksum": "same", "phash": 1} for _ in range(image_extract.REPEAT_LIMIT + 1)]
c.ok(image_extract.dedupe(rows) == [], "a logo repeated on many pages is dropped entirely")
c.ok(len(image_extract.dedupe([{"checksum": "a", "phash": 1},
                               {"checksum": "a", "phash": 1}])) == 1,
     "byte-identical duplicates collapse to one")
c.ok(len(image_extract.dedupe([{"checksum": "a", "phash": 0b1010},
                               {"checksum": "b", "phash": 0b1011}])) == 1,
     "the same figure at two resolutions collapses to one (visual dedup)")
c.ok(len(image_extract.dedupe([{"checksum": "a", "phash": 1},
                               {"checksum": "b", "phash": 0xFFFFFFFF}])) == 2,
     "two different figures both survive")

key = image_extract.image_key("user-1", "book-1", "img_x", "png")
c.ok(key == "book-images/user-1/book-1/img_x.png",
     "S3 keys are scoped by owner then book, so deletion cannot escape the prefix")


# ══ EPUB: reading order, captions, alt text ═════════════════════════════════

epub = build_epub(
    docs={
        "c1.xhtml": ('<html><body><h1>Small Habits</h1>'
                     '<figure><img src="images/loop.png" alt="the habit loop"/>'
                     '<figcaption>Figure 1: cue, routine, reward</figcaption></figure>'
                     '</body></html>'),
        "c2.xhtml": ('<html><body><h1>The Ending</h1>'
                     '<p>Late text about the finale.'
                     '<img src="images/finale.png" alt="the last scene"/></p>'
                     '</body></html>'),
    },
    images={"images/loop.png": FIG, "images/finale.png": OTHER},
    spine=["c1.xhtml", "c2.xhtml"],
)
found = image_extract.extract_epub_images(epub)
c.ok(len(found) == 2, "both EPUB figures are found (got %d)" % len(found))
if len(found) == 2:
    first, last = found[0], found[1]
    c.ok(first["chapter"] == "Small Habits", "the chapter heading is captured")
    c.ok("cue, routine, reward" in first["caption"], "the figcaption is captured")
    c.ok(first["alt"] == "the habit loop", "the img alt text is captured")
    c.ok(first["spine"] == 0 and last["spine"] == 1,
         "figures are ordered by SPINE position, not archive order")
    c.ok(first["position"] < last["position"],
         "position increases through the book (the Story spoiler guard)")
    c.ok(first["order"] == 0 and last["order"] == 1, "source order is recorded")

cover_epub = build_epub(
    docs={"c1.xhtml": '<html><body><img src="images/cover.png" alt="cover"/></body></html>'},
    images={"images/cover.png": FIG},
)
c.ok(image_extract.extract_epub_images(cover_epub) == [],
     "the cover is rejected — it illustrates no idea inside the book")

logo_epub = build_epub(
    docs={"c1.xhtml": '<html><body><img src="images/publisher-logo.png" alt="logo"/></body></html>'},
    images={"images/publisher-logo.png": LOGO},
)
c.ok(image_extract.extract_epub_images(logo_epub) == [],
     "a publisher logo is rejected by name even though its size passes")

c.ok(image_extract.extract_epub_images(b"not a zip") == [],
     "a corrupt EPUB yields no images instead of raising")

broken_opf = io.BytesIO()
with zipfile.ZipFile(broken_opf, "w") as zf:
    zf.writestr("META-INF/container.xml", "<broken")
    zf.writestr("c1.xhtml", '<html><body><img src="fig.png" alt="a figure here"/></body></html>')
    zf.writestr("fig.png", FIG)
recovered = image_extract.extract_epub_images(broken_opf.getvalue())
c.ok(len(recovered) == 1, "a malformed OPF falls back to sorted paths rather than failing")


# ══ PDF: page context and captions ═════════════════════════════════════════

pdf = build_pdf([
    ("Chapter one discusses compound interest at length. "
     "Figure 1: the compounding curve over forty years.", [FIG]),
    ("A later page about something else entirely.", [OTHER]),
])
if pdf is None:
    c.ok(True, "PyMuPDF unavailable — PDF fixtures skipped (reported, not hidden)")
else:
    texts = image_extract.pdf_page_texts(pdf)
    c.ok(len(texts) == 2 and "compound interest" in texts[0],
         "per-page text is extracted for figure context")
    pdf_found = image_extract.extract_pdf_images(pdf, texts)
    c.ok(len(pdf_found) == 2, "both PDF figures are found (got %d)" % len(pdf_found))
    if pdf_found:
        c.ok(pdf_found[0]["page"] == 1, "the page number is recorded")
        c.ok("compound interest" in pdf_found[0]["context"],
             "the page's text is attached as context")
        c.ok(pdf_found[0]["caption"].lower().startswith("figure 1"),
             "a 'Figure 1: ...' caption line is recognised")
        # A figure on page one starts at 0.0 — it is where the unit BEGINS, so
        # a figure at the top of the last page is not "100% through the book".
        c.ok(0 <= pdf_found[0]["position"] <= 1.0, "position is a 0..1 fraction")
        c.ok(pdf_found[0]["position_basis"] == "words",
             "PDF positions are measured in WORDS when page text is available — the "
             "same unit story_progress uses")
        c.ok(image_extract.extract_pdf_images(pdf, None)[0]["position_basis"] == "pages",
             "without page text the basis is honestly recorded as pages, not faked")

    logo_pdf = build_pdf([("page %d" % i, [LOGO]) for i in range(6)])
    c.ok(image_extract.extract_pdf_images(logo_pdf, None) == [],
         "the same image on six pages is furniture and is dropped")

c.ok(image_extract.extract_pdf_images(b"%PDF-broken", None) == [],
     "a corrupt PDF yields no images instead of raising")


# ══ storage: never breaks an upload ════════════════════════════════════════

class ExplodingS3:
    def upload_file(self, *a, **k):
        raise RuntimeError("S3 is down")


import app.services.s3_service as s3_module  # noqa: E402
_real_s3 = s3_module.S3Service
stored_keys = []


class StubS3:
    def upload_file(self, file_content, filename, content_type):
        stored_keys.append(filename)
        return filename

    def delete_file(self, ref):
        return True

    def generate_presigned_url(self, key, expiry=3600):
        return "https://s3.example.test/%s?sig=abc" % key


s3_module.S3Service = ExplodingS3
c.ok(image_extract.extract_and_store(epub, "b.epub", "item1", "user1") == [],
     "an S3 outage yields no images rather than failing the book")

s3_module.S3Service = StubS3
stored = image_extract.extract_and_store(epub, "b.epub", "item1", "user1")
c.ok(len(stored) == 2, "images are stored when S3 works")
if stored:
    row = stored[0]
    for field in ("id", "item_id", "user_id", "key", "mime", "checksum", "order",
                  "w", "h", "chapter", "context", "caption", "alt", "position", "visual"):
        c.ok(field in row, "the stored row carries %s" % field)
    c.ok(row["user_id"] == "user1" and row["item_id"] == "item1",
         "ownership is recorded on the row itself")
    c.ok(all(k.startswith("book-images/user1/item1/") for k in stored_keys),
         "every object lands under the owner+book prefix")
    c.ok("data" not in row, "raw image bytes are NOT persisted to the database")

c.ok(image_extract.extract_and_store(b"garbage", "b.epub", "i", "u") == [],
     "a corrupt file yields no images and does not raise")


# ══ relevance: which picture, if any ═══════════════════════════════════════

CANDIDATES = [
    {"id": "img_loop", "user_id": "u1", "item_id": "b1", "key": "book-images/u1/b1/img_loop.png",
     "caption": "Figure 1: cue routine reward habit loop", "alt": "the habit loop",
     "chapter": "Small Habits", "context": "habits form through repetition",
     "position": 0.2, "position_basis": "words", "order": 0, "visual": "diagram",
     "w": 600, "h": 400},
    {"id": "img_late", "user_id": "u1", "item_id": "b1", "key": "book-images/u1/b1/img_late.png",
     "caption": "Figure 9: the final confrontation", "alt": "the last scene",
     "chapter": "The Ending", "context": "the villain is unmasked",
     "position": 0.95, "position_basis": "words", "order": 1, "visual": "photo",
     "w": 600, "h": 400},
]

habit_text = "Habits form through repetition: a cue, a routine, and a reward."
short = image_select.shortlist(CANDIDATES, habit_text)
c.ok([x["id"] for x in short] == ["img_loop"],
     "only the genuinely related figure is shortlisted")
c.ok(image_select.shortlist(CANDIDATES, "a passage about marine biology") == [],
     "an unrelated passage shortlists nothing — a text-only deck")
c.ok(image_select.shortlist(None, habit_text) == [],
     "a book with no extracted images shortlists nothing")
c.ok(image_select.shortlist([], habit_text) == [], "an empty candidate list is safe")

many = [dict(CANDIDATES[0], id="img_%d" % i, order=i) for i in range(30)]
c.ok(len(image_select.shortlist(many, habit_text)) <= image_select.MAX_SHORTLIST,
     "the shortlist is bounded at MAX_SHORTLIST")

rows = image_select.prompt_rows(short)
blob = str(rows)
for leak in ("book-images", ".png", "key", "https", "u1", "b1"):
    c.ok(leak not in blob or leak in ("key",),
         "the model's view leaks no %s" % leak) if leak != "key" else None
c.ok(all(set(r) == {"id", "description"} for r in rows),
     "the model sees ONLY an opaque id and a description")
c.ok("book-images" not in blob and "https" not in blob,
     "no S3 key or URL is ever shown to the model")

prompt = image_select.describe_for_prompt(short)
c.ok("img_loop" in prompt and "imageId" in prompt, "the shortlist renders into the prompt")
c.ok(image_select.describe_for_prompt([]) == "",
     "no candidates means no image instructions in the prompt at all")


# ══ validation: the model's answer is a suggestion, never an authorisation ══

def validated(selected, **kw):
    args = dict(shortlisted=short, user_id="u1", item_id="b1")
    args.update(kw)
    return image_select.validate_selection(selected, **args)


c.ok(validated("img_loop") is not None, "a shortlisted candidate validates")
c.ok(validated(None) is None, "no selection is valid — text-only is expected")
c.ok(validated("") is None, "an empty selection is rejected")
c.ok(validated("img_invented") is None, "an invented id is rejected")
c.ok(validated("img_late") is None, "an id we did not offer is rejected")
c.ok(validated("book-images/u1/b1/img_loop.png") is None, "an S3 key is not an id")
c.ok(validated("https://evil.example/x.png") is None, "a URL is not an id")
c.ok(validated("../../other/img.png") is None, "a traversal-shaped string is rejected")
c.ok(validated({"id": "img_loop"}) is None, "a non-string selection is rejected")

c.ok(validated("img_loop", user_id="u2") is None,
     "a candidate belonging to another USER is rejected")
c.ok(validated("img_loop", item_id="b2") is None,
     "a candidate belonging to another BOOK is rejected")

keyless = [dict(short[0], key="")]
c.ok(image_select.validate_selection("img_loop", shortlisted=keyless, user_id="u1",
                                     item_id="b1") is None,
     "a candidate with no stored object is rejected")

# Story spoiler guard, on both halves of the belt-and-braces pair.
early = image_select.shortlist(CANDIDATES, "the villain is unmasked", max_position=0.5)
c.ok(early == [], "a figure from later in the book is never even offered")
c.ok(image_select.validate_selection(
    "img_late", shortlisted=CANDIDATES, user_id="u1", item_id="b1",
    max_position=0.5) is None,
    "and is rejected again after the model answers")
c.ok(image_select.validate_selection(
    "img_late", shortlisted=CANDIDATES, user_id="u1", item_id="b1",
    max_position=0.99) is not None,
    "the same figure IS allowed once the reader has reached it")
c.ok(image_select.validate_selection(
    "img_loop", shortlisted=[dict(CANDIDATES[0], position=None)], user_id="u1",
    item_id="b1", max_position=0.5) is None,
    "a candidate with unknown position is refused in Story mode, not guessed")


# ══ attaching to cards ═════════════════════════════════════════════════════

HABIT_CARD = {"title": "The habit loop", "body": "A cue, a routine and a reward."}


def cards_with(*ids, text=None):
    """Cards whose words match the candidate, unless told otherwise."""
    base = text if text is not None else HABIT_CARD
    return [dict(base, kind="insight", imageId=i) for i in ids]


deck = cards_with("img_loop", None, "img_invented")
attached = image_select.attach_images(deck, shortlisted=short, user_id="u1", item_id="b1")
c.ok(attached == 1, "exactly the valid selection is attached")
c.ok(deck[0]["image"]["id"] == "img_loop", "the card carries the validated image")
c.ok("image" not in deck[1] and "image" not in deck[2],
     "cards with no or invalid selections stay text-only")
c.ok(all("imageId" not in card for card in deck),
     "the internal imageId key never reaches the client")

payload = deck[0]["image"]
c.ok(payload["source"] == "book", "the payload records that this came from the book")
c.ok(payload["url"] == "/library/b1/images/img_loop",
     "the card stores a durable, BOOK-SCOPED API path, not a presigned URL")
c.ok("key" not in payload and "s3" not in str(payload).lower(),
     "the raw S3 key is never sent to the client")
c.ok(payload["fit"] == "contain" and payload["visual"] == "diagram",
     "a diagram is letterboxed so its axes survive")
c.ok(image_select.card_image_payload(dict(CANDIDATES[1]))["fit"] == "cover",
     "a photograph fills the card instead")
c.ok(payload["alt"] == "the habit loop", "alt text rides along for accessibility")

dupes = cards_with("img_loop", "img_loop")
image_select.attach_images(dupes, shortlisted=short, user_id="u1", item_id="b1")
c.ok("image" in dupes[0] and "image" not in dupes[1],
     "the same picture is not repeated on two cards in one session")

legacy = [{"kind": "insight", "title": "t", "body": "b"}]
image_select.attach_images(legacy, shortlisted=short, user_id="u1", item_id="b1")
c.ok("image" not in legacy[0], "a card that never mentioned an image is untouched")
c.ok(image_select.attach_images([], shortlisted=short, user_id="u1", item_id="b1") == 0,
     "an empty deck is safe")
c.ok(image_select.attach_images(None, shortlisted=[], user_id="u1", item_id="b1") == 0,
     "a missing deck is safe")


# ══════════════════════════════════════════════════════════════════════════
# AUDIT 2026-08-03 — the adversarial cases the first round did not cover
# ══════════════════════════════════════════════════════════════════════════

# ── 1. Relevance must be judged per CARD, not per deck ─────────────────────
# The shortlist is built once from all of a deck's passages, so membership
# only proves "relevant to this session". A habit-loop diagram legitimately
# shortlisted for a deck was accepted on that deck's marine-biology card.

MARINE = {"title": "Coral bleaching", "body": "Warming seas expel the algae that feed coral."}
marine_deck = cards_with("img_loop", text=MARINE)
image_select.attach_images(marine_deck, shortlisted=short, user_id="u1", item_id="b1")
c.ok("image" not in marine_deck[0],
     "a shortlisted image is REJECTED on a card it has nothing to do with")

on_topic = cards_with("img_loop")
image_select.attach_images(on_topic, shortlisted=short, user_id="u1", item_id="b1")
c.ok("image" in on_topic[0], "and still accepted on the card it does match")

c.ok(image_select.validate_selection("img_loop", shortlisted=short, user_id="u1",
                                     item_id="b1", card=MARINE) is None,
     "validate_selection rejects an off-card candidate directly")
c.ok(image_select.validate_selection("img_loop", shortlisted=short, user_id="u1",
                                     item_id="b1") is not None,
     "with no card supplied the per-card check is skipped, not guessed")
c.ok(image_select.card_text({"title": "a", "body": "b", "highlight": "c"}) == "a b c",
     "card text is drawn from title, body and pull-quote")


# ── 2. Story position must be in the SAME UNIT as reading progress ─────────
# story_progress is a WORD offset. A page or spine fraction is not comparable:
# a figure on page 12 of 300 is not "4% through" when 40 pages are front matter.

paged = [dict(CANDIDATES[1], position=0.1, position_basis="pages")]
c.ok(image_select.shortlist(paged, "the villain is unmasked", max_position=0.5) == [],
     "a page-based position is never offered in Story mode, even when it looks early")
c.ok(image_select.validate_selection("img_late", shortlisted=paged, user_id="u1",
                                     item_id="b1", max_position=0.5) is None,
     "and is refused after the model answers — an incomparable unit is not converted")
spined = [dict(CANDIDATES[1], position=0.1, position_basis="spine")]
c.ok(image_select.shortlist(spined, "the villain is unmasked", max_position=0.5) == [],
     "a spine-based position is refused for the same reason")
c.ok(image_select.shortlist(paged, "the villain is unmasked") != [],
     "outside Story mode the basis does not matter — Wisdom has no reading position")

# A position is the END of the unit that contains the figure, not its start.
# Neither format reports where on a page a figure actually sits, so the only
# safe assumption is the furthest point it could be — a figure at the bottom of
# a long page one was otherwise recorded at 0.0 and offered at 10% progress.
wp = image_extract._word_positions(["ten words " * 5, "ten words " * 5])
c.ok(wp == [0.5, 1.0], "a unit's position is where it ENDS, not where it begins")
wp = image_extract._word_positions(["a", "b " * 99])
c.ok(wp[0] == 0.01, "a short first unit ends early")
c.ok(wp[-1] == 1.0, "the last unit ends at the end of the book")
c.ok(all(wp[i] < wp[i + 1] for i in range(len(wp) - 1)), "positions increase monotonically")
c.ok(image_extract._word_positions(["", ""]) is None,
     "no text means no word position — the basis is recorded as unusable")

# The exact case the audit found: a figure at the bottom of a long page one.
long_page = "word " * 500
wp = image_extract._word_positions([long_page, long_page, long_page])
c.ok(wp[0] > 0.3,
     "a figure anywhere on a long page one is NOT treated as being at 0% "
     "(it would be offered to a reader 10%% in)")
c.ok(image_select.shortlist(
    [dict(CANDIDATES[0], position=wp[0], position_basis="words")],
    "habits cue routine reward", max_position=0.1) == [],
    "and it is therefore not offered at 10% progress")
c.ok(image_select.shortlist(
    [dict(CANDIDATES[0], position=wp[0], position_basis="words")],
    "habits cue routine reward", max_position=0.9) != [],
    "but it is offered once the reader is past it")


# ── 3. Cap must be applied AFTER deduplication ─────────────────────────────
# Sixty copies of a logo consumed the whole quota and hid the real figure.

logo_rows = [{"checksum": "logo", "phash": 1} for _ in range(60)]
real_row = [{"checksum": "real", "phash": 0xFFFFFFFF}]
kept = image_extract.dedupe(logo_rows + real_row)[:image_extract.MAX_IMAGES]
c.ok(any(r["checksum"] == "real" for r in kept),
     "a genuine figure behind sixty logo repeats survives the cap")
c.ok(len(kept) == 1, "and the logos are gone entirely rather than filling the quota")
c.ok(image_extract.MAX_SCAN > image_extract.MAX_IMAGES,
     "the scan budget is separate from the retained cap")
c.ok(image_extract.MAX_TOTAL_BYTES <= 64 * 1024 * 1024,
     "total retained bytes are bounded well below MAX_IMAGES x MAX_BYTES (480 MiB)")

# The budget is checked BEFORE accepting an image, not after subtracting.
# Subtracting afterwards let one more 8 MiB image through on an exhausted
# budget — a 72 MiB peak against a 64 MiB limit.
_src = open(os.path.join(BACKEND, "app", "services", "image_extract.py")).read()
c.ok("if len(data) > budget:" in _src,
     "an image that would overshoot the byte budget is refused before it is kept")
c.ok(_src.index("if len(data) > budget:") < _src.index("budget -= len(data)"),
     "the check happens before the subtraction, so the limit cannot be exceeded")
# The comment above that line explains why list() is wrong, so strip comments
# before asserting the code itself does not use it.
_code = "\n".join(l for l in _src.splitlines() if not l.strip().startswith("#"))
c.ok("images = page.images" in _code and "list(page.images)" not in _code,
     "PDF page images are iterated lazily — list() would materialise a whole "
     "pathological page before any budget applied")

# BOTH loops, driven against a one-byte budget with real files. The first
# version of this test only exercised the PDF path, and the EPUB loop was
# subtracting before checking — so it still admitted one image and could
# overshoot the nominal limit by up to MAX_BYTES.
_orig_budget = image_extract.MAX_TOTAL_BYTES
try:
    image_extract.MAX_TOTAL_BYTES = 1            # smaller than any real image
    if pdf is not None:
        c.ok(image_extract.extract_pdf_images(pdf, image_extract.pdf_page_texts(pdf)) == [],
             "PDF: an exhausted byte budget stops the scan rather than admitting one more")
    c.ok(image_extract.extract_epub_images(epub) == [],
         "EPUB: an exhausted byte budget stops the scan rather than admitting one more")
finally:
    image_extract.MAX_TOTAL_BYTES = _orig_budget

# The budget is not merely all-or-nothing: it must admit what fits and stop.
_orig_budget = image_extract.MAX_TOTAL_BYTES
try:
    image_extract.MAX_TOTAL_BYTES = len(FIG) + 1
    _partial_epub = image_extract.extract_epub_images(epub)
    c.ok(len(_partial_epub) == 1,
         "EPUB: a budget with room for one image admits exactly one (got %d)"
         % len(_partial_epub))
finally:
    image_extract.MAX_TOTAL_BYTES = _orig_budget

# Both loops guard the same way — a fix applied to one and not the other is
# exactly how this was missed the first time.
c.ok(_src.count("if len(data) > budget:") == 2,
     "BOTH the PDF and EPUB loops check the budget before accepting an image")
for _loop in ("extract_pdf_images", "extract_epub_images"):
    _body = _src[_src.index("def %s" % _loop):]
    _body = _body[:_body.index("\ndef ", 1)] if "\ndef " in _body[1:] else _body
    c.ok(_body.index("if len(data) > budget:") < _body.index("budget -= len(data)"),
         "%s checks the budget before subtracting from it" % _loop)


# ── 4. Blank images and PDF covers ─────────────────────────────────────────

BLANK_WHITE = png_bytes(600, 400, (255, 255, 255))
BLANK_BLACK = png_bytes(600, 400, (0, 0, 0))
BLANK_GREY = png_bytes(600, 400, (128, 128, 128))
c.ok(image_extract.is_blank(BLANK_WHITE), "a blank white panel is recognised as blank")
c.ok(image_extract.is_blank(BLANK_BLACK), "a solid black panel too — judged by variance, not colour")
c.ok(image_extract.is_blank(BLANK_GREY), "and a solid mid-grey")
c.ok(not image_extract.is_blank(FIG), "a real figure is not blank")
c.ok(image_extract._acceptable(BLANK_WHITE) is None,
     "a 600x400 blank is rejected despite passing every dimensional filter")

# A high-key photograph — bright, but tonally rich — must NOT be a diagram.
from PIL import Image as _PILImage  # noqa: E402
_hk = _PILImage.new("RGB", (600, 400), (250, 250, 250))
_px = _hk.load()
for _y in range(400):
    for _x in range(0, 600, 3):
        _px[_x, _y] = (200 + (_x % 55), 190 + (_y % 60), 210 + (_x % 45))
_buf = io.BytesIO()
_hk.save(_buf, format="PNG")
HIGH_KEY = _buf.getvalue()
c.ok(image_extract.visual_kind(HIGH_KEY) == "photo",
     "a bright but detailed photograph fills the card rather than being letterboxed")
c.ok(not image_extract.is_blank(HIGH_KEY), "and it is not mistaken for a blank")

if pdf is not None:
    cover_pdf = build_pdf([("", [FIG])])
    import fitz as _fitz  # noqa: E402
    _doc = _fitz.open()
    _p = _doc.new_page(width=595, height=842)
    _p.insert_image(_fitz.Rect(0, 0, 595, 842), stream=FIG)   # full bleed
    _full = _doc.tobytes()
    _doc.close()
    c.ok(image_extract.extract_pdf_images(_full, None) == [],
         "a full-bleed image on page one is rejected as the cover")

    # A content page: prose AND a figure. That is what distinguishes it from a
    # cover, which is artwork with no words on it.
    _doc = _fitz.open()
    _p = _doc.new_page(width=595, height=842)
    _prose = ("Habits form through repetition. A cue triggers a routine, the routine "
              "earns a reward, and the reward makes the cue matter more next time. "
              "The figure below traces one full turn of that loop from start to end.")
    _p.insert_text((72, 72), _prose)
    _p.insert_image(_fitz.Rect(72, 200, 472, 500), stream=FIG)
    _partial = _doc.tobytes()
    _doc.close()
    _texts = image_extract.pdf_page_texts(_partial)
    c.ok(len(image_extract.extract_pdf_images(_partial, _texts)) == 1,
         "but a figure on a page one that also has prose is kept — 'page one' "
         "alone is not a cover")


# ── 5. Malformed image rows must never fail a session ──────────────────────

for junk in ("not a list", 42, {"id": "x"}, [None], [{"id": None}], [[]],
             [{"id": "a", "order": "not-an-int", "caption": None}]):
    c.ok(image_select.safe_shortlist(junk, "habits") == [],
         "malformed images %r yield an empty shortlist, not an exception" % (str(junk)[:26],))

c.ok(image_select.safe_prompt(None) == "", "a broken shortlist yields no prompt text")
c.ok(image_select.safe_prompt([{"id": "x"}]) != "" or True,
     "safe_prompt never raises")


class Exploding(list):
    def __iter__(self):
        raise RuntimeError("corrupt JSON blob")


c.ok(image_select.safe_shortlist(Exploding([1]), "habits") == [],
     "even a container that raises while iterating degrades to text-only")


# ── 6. Book-scoped ids and delivery ────────────────────────────────────────
# A checksum-only id collided across books: the same figure uploaded twice
# produced one id, and library-wide delivery had two rows to choose from.

c.ok(image_extract.candidate_id("same-sum", "book-a")
     != image_extract.candidate_id("same-sum", "book-b"),
     "the same image in two books gets two different ids")
c.ok(image_extract.candidate_id("same-sum", "book-a")
     == image_extract.candidate_id("same-sum", "book-a"),
     "and the id is still stable across re-processing of one book")
c.ok(image_select.card_image_payload(dict(CANDIDATES[0]))["url"].startswith("/library/b1/images/"),
     "the delivery path names the book as well as the image")
c.ok(image_select.card_image_payload(dict(CANDIDATES[0]))["itemId"] == "b1",
     "the payload carries the book id so the client can rebuild the path")


# ══ AI image generation stays dark, now that images exist at all ═══════════
# The Part 1 tests proved image_gen was unreachable when no image path existed.
# The question worth re-asking is whether BUILDING one opened a door.

runtime_files = []
for root_dir, dirs, files in os.walk(os.path.join(BACKEND, "app")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".py"):
            runtime_files.append(os.path.join(root_dir, f))

importers = [os.path.basename(p) for p in runtime_files
             if "image_gen" in open(p).read()
             and os.path.basename(p) not in ("image_gen.py", "config.py")]
c.ok(not importers, "no runtime module reaches image_gen (found: %s)" % importers)

image_src = "\n".join(
    open(os.path.join(BACKEND, "app", "services", f)).read()
    for f in ("image_extract.py", "image_select.py")
)
for forbidden in ("images/generations", "gpt-image", "dall-e", "stability",
                  "replicate", "openai_api_key", "image_generation_enabled"):
    c.ok(forbidden not in image_src,
         "the book-image pipeline never references %r" % forbidden)

c.ok("openai" not in image_src.lower(),
     "no image-generation provider is reachable from the book-image path")

from app.config import Settings  # noqa: E402
s = Settings(_env_file=None, database_url="sqlite:///x", firebase_project_id="t")
c.ok(s.image_generation_enabled is False,
     "AI image generation is still OFF by default after Part 2")
c.ok(Settings(_env_file=None, database_url="sqlite:///x", firebase_project_id="t",
              openai_llm_api_key="sk-luna").image_generation_enabled is False,
     "configuring Luna still cannot switch image generation on")

# Tier is irrelevant: nothing in the image path consults premium status.
for word in ("is_premium", "effective_premium", "premium"):
    c.ok(word not in image_src,
         "the image pipeline does not branch on %r — free, trial and premium "
         "users get identical behaviour" % word)

# The only way a card gets a picture is a validated, pre-existing book figure.
c.ok(image_select.card_image_payload(dict(CANDIDATES[0]))["source"] == "book",
     "every attached image is labelled as coming from the book")

s3_module.S3Service = _real_s3
sys.exit(1 if c.finish() else 0)
