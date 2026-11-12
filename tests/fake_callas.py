"""Test-only stand-in for the callas pdfToolbox merger (no network, no real callas)."""
from pathlib import Path
from typing import Sequence

import pymupdf


class FakeCallasMerger:
    """Mimics ``make_callas_pdf_merger``: merges parts in order into ``output``."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, parts: Sequence[Path], output: Path, label: str) -> None:
        self.calls.append(([Path(p).name for p in parts], label))
        document = pymupdf.open()
        try:
            for part in parts:
                with pymupdf.open(part) as source:
                    document.insert_pdf(source)
            document.save(output)
        finally:
            document.close()
