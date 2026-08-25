"""Bounded streaming helpers for untrusted consumer uploads."""

from __future__ import annotations

from pathlib import Path

from fastapi import UploadFile

UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_HEADER_BYTES = 16


class UploadTooLargeError(ValueError):
    """Raised when an upload exceeds its configured byte limit."""


async def write_upload_in_chunks(
    *, file: UploadFile, path: Path, max_upload_bytes: int
) -> bytes:
    """Stream an upload to disk while enforcing size and retaining its header."""

    total = 0
    header = bytearray()
    try:
        with path.open("wb") as destination:
            while chunk := await file.read(UPLOAD_CHUNK_BYTES):
                total += len(chunk)
                if total > max_upload_bytes:
                    raise UploadTooLargeError
                if len(header) < UPLOAD_HEADER_BYTES:
                    header.extend(chunk[: UPLOAD_HEADER_BYTES - len(header)])
                destination.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return bytes(header)
