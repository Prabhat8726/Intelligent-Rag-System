"""PDFium access (pypdfium2).

PDFium is not thread-safe, so every call in a process is serialized through PDFIUM_LOCK and
parallelism comes from running more worker processes. Licence: Apache-2.0/BSD (ADR-009).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

PDFIUM_LOCK = threading.Lock()


class PdfOpenError(Exception):
    """The file could not be opened as a PDF."""

    def __init__(self, message: str, *, password_protected: bool) -> None:
        super().__init__(message)
        self.password_protected = password_protected


@contextmanager
def open_pdf(path: Path) -> Iterator[pdfium.PdfDocument]:
    """Open a PDF while holding the process-wide PDFium lock (released on exit)."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as exc:
            password = exc.err_code == pdfium_c.FPDF_ERR_PASSWORD
            reason = "password protected" if password else "not a readable PDF"
            raise PdfOpenError(reason, password_protected=password) from exc
        try:
            yield document
        finally:
            document.close()


def pdf_page_count(path: Path) -> int:
    with open_pdf(path) as document:
        return len(document)
