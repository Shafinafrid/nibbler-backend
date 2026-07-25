"""
Structure-preserving text extraction.

The product promise for story mode is that the user reads *the book*, not a
paraphrase of it — so paragraph breaks, dialogue lines and page boundaries have
to survive extraction. The old pipeline destroyed all of them:

    text = " ".join(page.extract_text() or "" for page in reader.pages)

PyPDF2's default extraction emits one newline per *visual line* (not per
paragraph) and nothing marks where a paragraph starts, so the only honest thing
that could be done with that output was to flatten it — which is exactly what
made a nibble read as one endless run-on block.

pypdf's ``extraction_mode="layout"`` keeps horizontal position, so a paragraph's
first line arrives with its real indent and blank lines survive between blocks.
That is enough to rebuild paragraphs deterministically:

  * a line that starts indented begins a new paragraph
  * a blank line ends the current paragraph
  * every other line continues the current paragraph (joined with a space,
    with end-of-line hyphenation repaired)
  * a paragraph that runs off the bottom of a page is stitched to the top of
    the next one, unless that page starts with an indent

Nothing here rewrites words: it only decides where the line breaks the *author*
wrote actually are.
"""

import re
from typing import List

# A line is treated as starting a new paragraph when it is pushed in at least
# this many columns. Book indents are typically 3-5 spaces in layout output;
# 2 is low enough to catch tight typesetting without firing on the ragged
# left edge of a justified block (which is column 0).
INDENT_MIN = 2

# Beyond this, the line isn't an indented paragraph — it's centred (a heading,
# a chapter number, a scene-break ornament). Those get their own paragraph too,
# but must never be glued onto the body text around them.
CENTERED_MIN = 18

_SENTENCE_END = ('.', '!', '?', '"', '”', '’', "'", '…', ':', '—')

# Page furniture: a running header/footer sitting alone on the first or last
# line of a page. Deliberately conservative — dropping a real line of the book
# is worse than leaving a stray page number in.
_PAGE_NUM_ONLY = re.compile(r'^[\divxlcIVXLC]{1,7}$')
_PAGE_NUM_EDGE = re.compile(r'^(\d{1,4}\s+\S.*|.*\S\s+\d{1,4})$')


def _norm(line: str) -> str:
    """Collapse the tabs/multiple spaces layout mode uses as word separators."""
    return ' '.join(line.replace('\t', ' ').split())


def _is_page_furniture(text: str) -> bool:
    """True for a running head / folio, e.g. '11' or 'THE BOY WHO LIVED  11'."""
    if not text:
        return False
    if _PAGE_NUM_ONLY.match(text):
        return True
    words = text.split()
    if len(words) <= 8 and _PAGE_NUM_EDGE.match(text):
        # A short line bookended by a number, and no sentence punctuation in
        # the middle of it — a real sentence that short is rare and would
        # normally end in a full stop.
        return not text.rstrip().endswith(('.', '!', '?'))
    return False


def _join(prev: str, nxt: str) -> str:
    """Append a wrapped line, repairing end-of-line hyphenation."""
    if prev.endswith('-') and not prev.endswith(('--', ' -')):
        return prev[:-1] + nxt
    if prev.endswith(('—', '–')):
        return prev + nxt          # a dash break carries no space in print
    return prev + ' ' + nxt


def _page_paragraphs(raw: str) -> List[dict]:
    """One page of layout-mode text → [{'text', 'indented', 'centered'}]."""
    lines = raw.split('\n')

    # Strip a running header / footer before anything else, so it can't be
    # stitched into the body paragraph that continues across the page break.
    content = [i for i, l in enumerate(lines) if l.strip()]
    for edge in (content[:1] + content[-1:]) if content else []:
        if _is_page_furniture(_norm(lines[edge])):
            lines[edge] = ''

    paras: List[dict] = []
    cur = None
    for line in lines:
        if not line.strip():
            cur = None            # blank line closes the paragraph
            continue
        indent = len(line) - len(line.lstrip(' \t'))
        text = _norm(line)
        if not text:
            continue
        centered = indent >= CENTERED_MIN
        starts_new = cur is None or indent >= INDENT_MIN
        if starts_new or centered or (cur and cur['centered']):
            cur = {'text': text, 'indented': indent >= INDENT_MIN, 'centered': centered}
            paras.append(cur)
        else:
            cur['text'] = _join(cur['text'], text)
    return paras


def pdf_to_structured_text(pdf_bytes: bytes, max_chars: int) -> str:
    """Full PDF → text with real paragraph breaks (``\\n\\n`` between them).

    Falls back to plain extraction for any page layout mode can't handle, so a
    single awkward page never costs the whole book.
    """
    import io
    import pypdf

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    out: List[str] = []
    for page in reader.pages:
        try:
            raw = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            raw = page.extract_text() or ""
        if not raw.strip():
            continue

        for i, para in enumerate(_page_paragraphs(raw)):
            # A paragraph broken by a page break: the previous page ended
            # mid-sentence and this page opens flush-left, so it is the same
            # paragraph the author wrote — put it back together.
            if (
                i == 0
                and out
                and not para['indented']
                and not para['centered']
                and not out[-1].endswith(_SENTENCE_END)
            ):
                out[-1] = _join(out[-1], para['text'])
            else:
                out.append(para['text'])

        if sum(len(p) + 2 for p in out) >= max_chars:
            break

    return '\n\n'.join(out[find_body_start(out):])[:max_chars]


# ── Front matter ─────────────────────────────────────────────────────────────
# Every book file opens with material nobody wants served as their daily read:
# a cover blurb, pages of press quotes, the title page, the imprint/copyright
# page, a table of contents. Story mode reads from word 0, so without this the
# first few days of a book are its copyright page.

# Legal boilerplate that only ever appears on a copyright page. Counts as front
# matter however long the paragraph is.
_LEGAL = re.compile(
    r'all rights reserved|no part of this publication|supports copyright'
    r'|limit of liability|disclaimer of warrant|moral right of the author'
    r'|a cip catalogue|british library cataloguing|without the prior permission',
    re.I,
)

# Weaker hints. A book's own prose says "first published in 1997" or names its
# publisher all the time, so these only count on a SHORT line — the shape front
# matter actually has. Counting them anywhere dragged the start 100 paragraphs
# into an author's introduction.
_FRONT_HINT = re.compile(
    r'copyright ©|©\s*\d{4}|isbn|first published|typeset by|printed and bound'
    r'|imprint of|published by|random house|bloomsbury|digital edition'
    r'|trademark|^contents$|^table of contents$|^copyright$|^title page$',
    re.I,
)


def _is_front_matter(para: str) -> bool:
    if _LEGAL.search(para):
        return True
    return len(para.split()) < PROSE_WORDS and bool(_FRONT_HINT.search(para))

_CHAPTER_RE = re.compile(
    r'^(chapter\s+\S{1,12}|prologue|preface|foreword|introduction(\s*[:—-].{0,60})?'
    r'|part\s+(one|two|three|four|five|\d{1,2}|[ivxlc]{1,5}))\s*$',
    re.I,
)

PROSE_WORDS = 45           # what a real body paragraph looks like
FRONT_MATTER_LIMIT = 0.15  # refuse to skip more than this much of the book
FRONT_MATTER_MAX = 150     # ...and never more than this many paragraphs. A long
                           # book mentions publishers and ISBNs in its own text;
                           # without an absolute cap, one footnote about a
                           # publisher dragged the start 38% into the book.


def find_body_start(paras: List[str]) -> int:
    """Index of the paragraph where the book actually begins.

    Deliberately conservative — it only skips anything when the text carries an
    unmistakable front-matter signal (a copyright/imprint line), and it gives up
    rather than skip a large fraction of the file. Serving a page of front
    matter is a bad day; swallowing the first chapter is a much worse one.
    """
    if not paras:
        return 0

    limit = min(len(paras), max(20, int(len(paras) * FRONT_MATTER_LIMIT)), FRONT_MATTER_MAX)
    last_signal = -1
    for i in range(limit):
        if _is_front_matter(paras[i]):
            last_signal = i
    if last_signal < 0:
        return 0          # no imprint page — not a published book, leave it alone

    for i in range(last_signal + 1, limit):
        para = paras[i]
        if _is_front_matter(para):
            continue      # still inside the front matter
        # A chapter heading counts only when real prose follows it — otherwise
        # every line of the table of contents looks like one.
        if _CHAPTER_RE.match(para) and any(
            len(paras[j].split()) >= PROSE_WORDS
            for j in range(i + 1, min(i + 3, len(paras)))
        ):
            return i
        if len(para.split()) >= PROSE_WORDS:
            return i
    return 0


def strip_front_matter(text: str) -> str:
    paras = [p for p in re.split(r"\n[ \t]*\n", text) if p.strip()]
    return "\n\n".join(paras[find_body_start(paras):])


# ── EPUB ─────────────────────────────────────────────────────────────────────

_BLOCK_TAGS = (
    'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'li', 'blockquote', 'pre', 'figcaption', 'td', 'dd', 'dt',
)


def epub_doc_paragraphs(raw: bytes) -> List[str]:
    """One EPUB chapter document → its paragraphs, in document order.

    ``soup.get_text(separator="\\n")`` (what this used to do) breaks a sentence
    apart at every inline ``<em>``/``<a>``, which is worse than no structure at
    all. Walking block-level elements instead gives paragraphs exactly as the
    publisher marked them up.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, 'html.parser')
    for tag in soup(['script', 'style', 'nav']):
        tag.decompose()

    body = soup.body or soup
    out: List[str] = []
    for el in body.find_all(_BLOCK_TAGS):
        # Only leaf blocks — a wrapper <div> would repeat everything inside it.
        if el.find(_BLOCK_TAGS):
            continue
        text = _norm(el.get_text(' ', strip=True))
        if text:
            out.append(text)
    if not out:
        text = _norm(body.get_text(' ', strip=True))
        if text:
            out.append(text)
    return out
