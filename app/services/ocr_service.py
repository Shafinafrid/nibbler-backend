"""
OCR for scanned PDFs — reading books that have no text layer.

A scan is a photograph of a page: there is no text in the file to extract, so
the normal pipeline correctly finds nothing. This renders each page and reads
it with Tesseract, producing the text layer the book never had.

Two guards matter more than the OCR itself, and both are about protecting the
API rather than the bill (a 335-page book is roughly 8 vCPU-minutes, about
half a cent):

  * CONCURRENCY. These jobs run in the API process via BackgroundTasks. One
    OCR pins a core for minutes, and the service has 1-2 vCPU — three at once
    would starve nibble generation, Connect chat and every other request. A
    semaphore lets one through at a time and queues the rest. Users are already
    told it takes a while, so waiting costs nothing.
  * PAGE CAP. One pathological upload must not hold a worker for half an hour.

Tesseract is a SYSTEM binary, installed via nixpacks.toml. If it is missing,
is_available() returns False and the feature stays dark rather than failing
uploads.
"""

import io
import logging
import shutil
import threading
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# One at a time. See the module docstring — this is the guard that keeps the
# rest of the API responsive while a book is being read.
_slot = threading.Semaphore(1)

MAX_PAGES = 600
# 200 dpi is the accuracy/speed knee for body text. 300 is barely better on
# clean print and takes roughly twice as long.
DPI = 200
# Below this, OCR found so little that indexing it would poison the book's
# nibbles with noise — better to report failure than to serve garbage.
MIN_CHARS_PER_PAGE = 40


def is_available() -> bool:
    """True when both the Python binding and the tesseract binary are present."""
    try:
        import pytesseract  # noqa: F401
        import fitz  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def ocr_pdf(
    pdf_bytes: bytes,
    on_progress: Optional[Callable[[int, int], None]] = None,
    max_pages: int = MAX_PAGES,
) -> str:
    """Read a scanned PDF and return its text.

    `on_progress(done, total)` is called after each page so the client can show
    real progress — a job this long with only a spinner feels broken.

    Raises RuntimeError when OCR is unavailable or the result is too thin to
    be worth indexing.
    """
    if not is_available():
        raise RuntimeError("OCR is not available on this server.")

    import fitz
    import pytesseract
    from PIL import Image

    with _slot:                      # queue rather than compete for the CPU
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        total = min(len(doc), max_pages)
        zoom = DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pages = []

        try:
            for i in range(total):
                try:
                    pix = doc[i].get_pixmap(matrix=matrix, colorspace=fitz.csGRAY)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    pages.append(pytesseract.image_to_string(img) or "")
                except Exception as e:
                    # One unreadable page must not lose the other 334.
                    logger.warning("OCR failed on page %s: %s", i, e)
                    pages.append("")
                if on_progress:
                    try:
                        on_progress(i + 1, total)
                    except Exception:
                        pass
        finally:
            doc.close()

    # Blank lines between pages keep the paragraph rebuilder downstream working
    # the same way it does for a normal extraction.
    text = "\n\n".join(p.strip() for p in pages if p.strip())

    if len(text) < MIN_CHARS_PER_PAGE * max(1, total) * 0.25:
        raise RuntimeError(
            "OCR could not read enough text from this PDF — the scan may be too "
            "faint, skewed or handwritten."
        )
    return text
