"""PDF text extraction + chunking, with an OCR fallback for scanned pages.

Uses PyMuPDF (`pymupdf`, imported under its non-deprecated name -- `import fitz` still
works but is being phased out upstream) rather than pypdf, after finding it's a strict
upgrade for both of the corpus-specific gotchas confirmed by actually reading every file
in dataset/textos/ (docs/dataset-eda.md §6):

1. `Appendicitis/REVISIÓN DE LA LITERATURA SOBRE LAAPENDICITIS AGUDA PEDIATRICA NO
   ESPECIFICADA EN EL PERI000 2000-2021.pdf` is scanned with no text layer. PyMuPDF
   returns "" for its page just like pypdf did, but it can ALSO render that page to a
   raster image directly (`page.get_pixmap`) with no extra library needed -- which is
   exactly what OCR needs as input. Verified end to end: OCR on this exact page produces
   real, usable Spanish medical text ("La causa mas frecuente de abdomen agudo e
   indicacion quirurgica..."), not garbage.
2. `breast_cancer/Herramientas-Tecnica-Cancer-cuello-uterino-2018.pdf` is AES-encrypted --
   pypdf needed the extra `cryptography` package to open it. PyMuPDF opens it directly,
   no extra dependency needed at all. Both `pypdf` and `cryptography` were dropped from
   pyproject.toml after switching (verified nothing else in this service used them).

OCR needs the `tesseract` binary + language data on the host/image (`apt-get install
tesseract-ocr tesseract-ocr-spa` on the Dockerfile's Debian base; `brew install
tesseract tesseract-lang` for local dev) -- if it's missing, `_ocr_page` logs and returns
"" rather than crashing the whole ingest request over one unreadable page.
"""

import logging
from dataclasses import dataclass

import pymupdf
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE_WORDS = 220
DEFAULT_CHUNK_OVERLAP_WORDS = 40
OCR_DPI = 200
OCR_LANGUAGES = "spa+eng"  # corpus is Spanish/English mixed, docs/dataset-eda.md §1


@dataclass
class Chunk:
    text: str
    page: int
    chunk_index: int


def extract_pages(file_bytes: bytes) -> list[tuple[int, str]]:
    """Returns [(page_number (1-indexed), text)] for a PDF's pages, OCR'ing any page
    that has no extractable text layer."""
    doc = pymupdf.open(stream=file_bytes, filetype="pdf")
    pages = []
    for i, page in enumerate(doc, start=1):
        text = page.get_text().strip()
        if not text:
            logger.warning("page %d has no text layer, falling back to OCR", i)
            text = _ocr_page(page)
        pages.append((i, text))
    return pages


def _ocr_page(page: "pymupdf.Page") -> str:
    pix = page.get_pixmap(dpi=OCR_DPI)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    try:
        return pytesseract.image_to_string(img, lang=OCR_LANGUAGES).strip()
    except pytesseract.TesseractNotFoundError:
        logger.error(
            "tesseract binary not found -- OCR fallback unavailable, "
            "page will contribute no text. Install tesseract-ocr (+ tesseract-ocr-spa)."
        )
        return ""


def chunk_pages(
    pages: list[tuple[int, str]],
    chunk_size_words: int = DEFAULT_CHUNK_SIZE_WORDS,
    overlap_words: int = DEFAULT_CHUNK_OVERLAP_WORDS,
) -> list[Chunk]:
    """Sliding-window word chunking, scoped per page so citations can always point to a
    single page number (plan §2.4/§3.2 -- chunk metadata carries `page`).

    TODO(workstream B): a page-scoped window means a sentence that straddles a page break
    never forms one coherent chunk. Fine for a v1 -- revisit if retrieval quality on
    boundary content turns out to matter for the grading corpus.
    """
    chunks: list[Chunk] = []
    idx = 0
    for page_number, text in pages:
        if not text:
            continue
        words = text.split()
        start = 0
        while start < len(words):
            end = min(start + chunk_size_words, len(words))
            chunk_text_ = " ".join(words[start:end])
            chunks.append(Chunk(text=chunk_text_, page=page_number, chunk_index=idx))
            idx += 1
            if end == len(words):
                break
            start = end - overlap_words
    return chunks
