"""Адаптер «извлечь страницу PDF в отдельный файл» поверх задания callas ``split``."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

from services.callas.client import CallasClient, deterministic_job_id
from services.callas.merger import _sha256

logger = logging.getLogger("image_magic.callas")

# extractor(source_pdf, page_number_1based, output_pdf, label)
PdfPageExtractor = Callable[[Path, int, Path, str], None]


def make_callas_page_extractor(client: CallasClient) -> PdfPageExtractor:
    """Возвращает extractor: одна страница через callas split, результат копируется в output."""

    def extractor(source: Path, page_number: int, output: Path, label: str) -> None:
        source = Path(source)
        digest = _sha256(source)
        job_id = deterministic_job_id(f"split-{label}", digest, str(page_number))
        part_name = "page.pdf"
        logger.info(
            "callas.split.start label=%s job_id=%s page=%d", label, job_id, page_number
        )
        try:
            result = client.split(source, [(part_name, [page_number])], job_id=job_id)
            temporary = output.with_name(f".{output.name}.callas.part")
            try:
                shutil.copyfile(result[part_name], temporary)
                temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
            logger.info("callas.split.done label=%s job_id=%s", label, job_id)
        finally:
            try:
                client.delete(job_id)
            except Exception:  # noqa: BLE001 - очистка не должна скрыть результат
                pass
            shutil.rmtree(client.job_dir(job_id), ignore_errors=True)

    return extractor
