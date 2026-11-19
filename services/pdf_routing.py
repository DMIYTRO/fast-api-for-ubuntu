"""Status folders for built PDFs under ``<input>/PDF/``.

Every PDF the checker produces lives in exactly one of three folders, chosen
from the order's current status:

======== =============== ====================================================
status   folder          meaning
======== =============== ====================================================
passed   ``PDF/Checked`` passed every mandatory check, waits for the operator
warning  ``PDF/Rework``  readable result requiring correction (local findings
                         or PitStop errors/warnings)
error    ``PDF/Failed``  unreadable result or technical processing/check failure
======== =============== ====================================================

``PDF/Print`` is not a status folder: only the operator's «в печать»
(``FileLifecycle.accept_for_print``) moves a PDF there, so a check never puts
a PDF into Print before the decision (order 26237201, 2026-11-19: a green PDF
sat in Print for 18 s before anyone pressed the button).

Invariant: ``PDF/Print`` never contains a PDF whose current status is
``warning`` or ``error``.  ``route_pdf`` therefore writes the destination
atomically (temporary file + ``os.replace``) while staging the source and
any stale copy of the same name from the other folders (Print included) and
from the legacy ``PDF/`` root.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any

from core.share_tempdir import share_temp_dir


PDF_FOLDER_BY_STATUS: dict[str, str] = {
    "passed": "Checked",
    "warning": "Rework",
    "error": "Failed",
}
CHECKED_FOLDER = PDF_FOLDER_BY_STATUS["passed"]
REWORK_FOLDER = PDF_FOLDER_BY_STATUS["warning"]
FAILED_FOLDER = PDF_FOLDER_BY_STATUS["error"]
#: Отправленные оператором в печать; сюда переносит только ``accept_for_print``.
PRINT_FOLDER = "Print"
#: Все папки собранных PDF под ``PDF/`` (статусы проверки и Print).
STATUS_FOLDERS: tuple[str, ...] = (*PDF_FOLDER_BY_STATUS.values(), PRINT_FOLDER)


def folder_for_status(status: str) -> str:
    """Folder name under ``PDF/`` for a completed order status."""
    try:
        return PDF_FOLDER_BY_STATUS[status]
    except KeyError as exc:
        raise ValueError(f"нет папки PDF для статуса заказа: {status}") from exc


def pdf_root_for(pdf_path: Path | str) -> Path:
    """``PDF/`` directory a built PDF belongs to.

    Works for PDFs already routed into a status folder and for legacy PDFs
    that still sit directly in ``PDF/``.
    """
    pdf = Path(pdf_path)
    if pdf.parent.name in STATUS_FOLDERS and pdf.parent.parent.name == "PDF":
        return pdf.parent.parent
    return pdf.parent


def is_in_status_folder(pdf_path: Path | str, status: str) -> bool:
    return Path(pdf_path).parent.name == folder_for_status(status)


def completed_status(
    *,
    local_errors: bool,
    local_warnings: bool,
    pitstop: dict[str, Any] | None,
) -> str:
    """Final order status from local inspection and an optional PitStop DTO.

    ``local_errors`` means a technical processing/readability failure;
    readable artwork findings belong in ``local_warnings``.
    """
    if local_errors:
        return "error"
    if pitstop:
        if pitstop.get("execution_status") != "completed":
            return "error"
        verdict = pitstop.get("verdict")
        if verdict == "error":
            return "warning"
        if verdict == "warning":
            return "warning"
    if local_warnings:
        return "warning"
    return "passed"


def _link_or_copy(source: Path, target: Path) -> None:
    """Make *target* a second name for *source*'s bytes, copying only if needed.

    A hard link on the same filesystem costs no I/O however large the PDF is.
    The source is renamed away right after (see ``route_pdf``) and never
    rewritten in place, so sharing the inode is safe.  Symlinks are copied so
    that the result never aliases a file outside the PDF folders; a failing
    ``os.link`` (other filesystem, SMB/FAT without hard links, permissions)
    falls back to the byte copy.
    """
    if not source.is_symlink():
        try:
            os.link(source, target)
            return
        except OSError:
            pass
    shutil.copyfile(source, target)


def route_pdf(
    pdf_path: Path | str,
    status: str,
    *,
    pdf_dir: Path | str | None = None,
    name: str | None = None,
) -> Path:
    """Move *pdf_path* into the status folder and return its new location.

    ``name`` lets a corrected PDF take over the production file name.  The
    destination is written atomically; previous copies are staged outside
    the status folders and restored when publication fails. On
    any failure the source (and the previous production PDF) stay untouched
    and no temporary file is left behind.
    """
    source = Path(pdf_path)
    root = Path(pdf_dir) if pdf_dir is not None else pdf_root_for(source)
    destination = root / folder_for_status(status) / (name or source.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[Path, Path]] = []
    # Stage previous copies outside every status folder, so a successful
    # publish has exactly one visible result. Restore them on a failed save.
    with share_temp_dir(prefix=".pdf-route-", dir=root) as backup_dir:
        temporary = Path(backup_dir) / "candidate.pdf"
        try:
            _link_or_copy(source, temporary)
            candidates = [source, destination, root / destination.name]
            candidates.extend(root / folder / destination.name for folder in STATUS_FOLDERS)
            seen: set[Path] = set()
            for candidate in candidates:
                resolved = candidate.resolve()
                if resolved in seen or not candidate.is_file():
                    continue
                seen.add(resolved)
                backup = Path(backup_dir) / str(len(staged))
                candidate.rename(backup)
                staged.append((candidate, backup))
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            for original, backup in reversed(staged):
                if backup.exists():
                    backup.rename(original)
            raise
    return destination
