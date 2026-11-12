"""Адаптер «склеить PDF-части в один файл» поверх CallasClient."""

from __future__ import annotations

import hashlib
import logging
import shutil
import uuid
from pathlib import Path
from typing import Callable, Sequence

from services.callas.client import CallasClient, deterministic_job_id

logger = logging.getLogger("image_magic.callas")

PdfMerger = Callable[[Sequence[Path], Path, str], None]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_callas_pdf_merger(client: CallasClient) -> PdfMerger:
    """Возвращает merger(parts, output, label): склейка через callas, результат копируется в output."""

    def merger(parts: Sequence[Path], output: Path, label: str) -> None:
        # Служба callas помнит завершённые задания: повтор того же job_id вернул бы
        # старый статус "done" без файлов. Поэтому job_id уникален на операцию;
        # повторы HTTP внутри клиента его сохраняют.
        job_id = deterministic_job_id(
            f"merge-{label}", *(_sha256(Path(p)) for p in parts), uuid.uuid4().hex[:12]
        )
        logger.info("callas.merge.start label=%s job_id=%s inputs=%d", label, job_id, len(parts))
        try:
            result = client.merge([Path(p) for p in parts], output.name, job_id=job_id)
            shutil.copyfile(result, output)
            logger.info("callas.merge.done label=%s job_id=%s", label, job_id)
        finally:
            try:
                client.delete(job_id)
            except Exception:
                pass
            shutil.rmtree(client.job_dir(job_id), ignore_errors=True)

    return merger
