"""Адаптер «извлечь страницу PDF в отдельный файл» поверх задания callas ``split``."""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Callable

from services.callas.client import CallasClient, deterministic_job_id

logger = logging.getLogger("image_magic.callas")

# extractor(source_pdf, page_number_1based, output_pdf, label)
PdfPageExtractor = Callable[[Path, int, Path, str], None]


def make_callas_page_extractor(client: CallasClient) -> PdfPageExtractor:
    """Возвращает extractor: одна страница через callas split, результат копируется в output."""

    def extractor(source: Path, page_number: int, output: Path, label: str) -> None:
        source = Path(source)
        # Задание уникально на операцию (как в merge/render): хеш всего PDF ради
        # идентификатора не нужен, а одинаковый id у параллельных одинаковых извлечений
        # давал бы конфликт 409 и общую папку задания.
        job_id = deterministic_job_id(f"split-{label}", str(page_number), uuid.uuid4().hex[:12])
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
