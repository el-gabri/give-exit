"""Document text extraction with PyMuPDF.

See ADR 0005 for why PyMuPDF and how the OCR decision works.
"""

import struct
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import fitz  # PyMuPDF

# Below this average of extractable characters per page we assume the PDF is
# a scan (image-only) and needs OCR. A page of legal text has 1500-3500
# chars; scans yield ~0. The generous margin tolerates cover pages/stamps.
MIN_AVG_CHARS_PER_PAGE = 50

# JPEG start-of-frame markers carry the image size; standalone markers have
# no length field to skip; start of scan means the size never came.
_JPEG_START_OF_FRAME_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_STANDALONE_MARKERS = frozenset({*range(0xD0, 0xD8), 0xD8, 0xD9, 0x01})
_JPEG_START_OF_SCAN = 0xDA


@dataclass(frozen=True)
class PdfExtraction:
    """Raw result of native text extraction."""

    page_texts: list[str]
    page_needs_ocr: list[bool]

    @property
    def needs_ocr(self) -> bool:
        """Whether at least one page needs OCR."""
        return any(self.page_needs_ocr)

    @property
    def ocr_page_indexes(self) -> list[int]:
        """Zero-based indexes of pages that need OCR."""
        return [index for index, needed in enumerate(self.page_needs_ocr) if needed]


def extract_text(path: Path, max_pages: int | None = None) -> PdfExtraction:
    """Extract each page's text layer and decide whether OCR is needed.

    Raises:
        FileNotFoundError: if the file does not exist.
        ValueError: if the file is not a readable document.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        doc = fitz.open(path)
    except Exception as exc:  # fitz raises generic RuntimeError subclasses
        raise ValueError(f"Not a readable document: {path.name}") from exc

    with doc:
        if max_pages is not None and len(doc) > max_pages:
            raise ValueError(
                f"Document exceeds the {max_pages}-page limit: {path.name} has {len(doc)} pages"
            )
        page_texts = [_page_text(page) for page in doc]

    if not page_texts:
        return PdfExtraction(page_texts=[], page_needs_ocr=[])

    return PdfExtraction(
        page_texts=page_texts,
        page_needs_ocr=[
            len(page_text.strip()) < MIN_AVG_CHARS_PER_PAGE for page_text in page_texts
        ],
    )


def _page_text(page: fitz.Page) -> str:
    """The page's text blocks in reading order, one blank line between blocks.

    ``get_text("text")`` ends every visual line with a single newline and never
    marks a paragraph, so the chunker saw each page as one paragraph and cut it
    at fixed character offsets, splitting amounts such as ``R$ 1.2|50,00``
    across chunks. PyMuPDF's blocks are the layout's own paragraphs, table
    cells and statement rows; image blocks carry no text.
    """
    blocks = page.get_text("blocks", sort=True)
    return "\n\n".join(
        text.strip() for *_, text, _, block_type in blocks if block_type == 0 and text.strip()
    )


def image_dimensions(path: Path, *, max_pixels: int | None = None) -> tuple[int, int]:
    """Read PNG/JPEG dimensions without decoding the image raster.

    Pillow is preferred because it understands the container formats and
    exposes its decompression-bomb guards. The small header parser is a safe
    fallback for deployments where the optional OCR dependency is absent.
    """
    if not path.exists():
        raise FileNotFoundError(path)

    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError:
        width, height = _image_dimensions_from_header(path)
    else:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.format not in {"PNG", "JPEG"}:
                        raise ValueError(f"Unsupported image container: {path.name}")
                    width, height = image.size
        except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
            limit = (
                f"the {max_pixels}-pixel safety limit"
                if max_pixels is not None
                else "the image safety limit"
            )
            raise ValueError(f"Image exceeds {limit}: {path.name}") from exc
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"Not a readable image: {path.name}") from exc

    if width <= 0 or height <= 0:
        raise ValueError(f"Not a readable image: {path.name}")
    if max_pixels is not None and width * height > max_pixels:
        raise ValueError(f"Image exceeds the {max_pixels}-pixel safety limit: {path.name}")
    return width, height


def _image_dimensions_from_header(path: Path) -> tuple[int, int]:
    """Return PNG/JPEG dimensions from bounded metadata reads only."""
    with path.open("rb") as stream:
        header = stream.read(32)
        if (
            header.startswith(b"\x89PNG\r\n\x1a\n")
            and header[8:16] == b"\x00\x00\x00\rIHDR"
            and len(header) >= 24
        ):
            return struct.unpack(">II", header[16:24])

        if not header.startswith(b"\xff\xd8"):
            raise ValueError(f"Not a readable PNG or JPEG image: {path.name}")

        stream.seek(2)
        dimensions = _jpeg_frame_dimensions(stream)
    if dimensions is None:
        raise ValueError(f"Not a readable JPEG image: {path.name}")
    return dimensions


def _jpeg_frame_dimensions(stream: BinaryIO) -> tuple[int, int] | None:
    """Walk JPEG marker segments up to the frame header; None if it never comes."""
    for _ in range(512):
        marker_code = _next_jpeg_marker(stream)
        if marker_code is None or marker_code == _JPEG_START_OF_SCAN:
            return None
        if marker_code in _JPEG_STANDALONE_MARKERS:
            continue

        length_bytes = stream.read(2)
        if len(length_bytes) != 2:
            return None
        segment_length = int.from_bytes(length_bytes, "big")
        if segment_length < 2:
            return None

        if marker_code in _JPEG_START_OF_FRAME_MARKERS:
            frame_header = stream.read(5)
            if len(frame_header) != 5 or segment_length < 7:
                return None
            height = int.from_bytes(frame_header[1:3], "big")
            width = int.from_bytes(frame_header[3:5], "big")
            return width, height

        stream.seek(segment_length - 2, 1)
    return None


def _next_jpeg_marker(stream: BinaryIO) -> int | None:
    """The next marker code, skipping fill bytes; None on a bad prefix or at EOF."""
    if stream.read(1) != b"\xff":
        return None
    marker = stream.read(1)
    while marker == b"\xff":
        marker = stream.read(1)
    return marker[0] if marker else None


def render_page_images(
    path: Path, page_indexes: list[int] | None = None, dpi: int = 200
) -> list[bytes]:
    """Render selected pages as PNGs (input for OCR engines)."""
    with fitz.open(path) as doc:
        indexes = page_indexes if page_indexes is not None else list(range(len(doc)))
        return [doc.load_page(index).get_pixmap(dpi=dpi).tobytes("png") for index in indexes]
