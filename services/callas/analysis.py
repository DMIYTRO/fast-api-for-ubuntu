"""Запуск analyze-профиля callas с разбором XML-отчёта (для проверок макета).

Вход копируется в локальный снимок: callas отклоняет результат, если size/mtime входа
изменились за время работы (на сетевых дисках mtime «плывёт»). Папку задания после
использования удаляем.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from services.callas.client import deterministic_job_id
from services.callas.report import read_report_root


def run_analysis(
    client: Any, pdf: Path | str, profile: str, label: str
) -> tuple[Any, ET.Element]:
    """``client.process(..., "analyze")`` → ``(ProcessResult, корень XML-отчёта)``."""
    source = Path(pdf)
    job_id = deterministic_job_id(f"an-{label}", str(source), profile, uuid.uuid4().hex[:12])
    try:
        with tempfile.TemporaryDirectory(prefix="callas-an-") as snapshot_dir:
            snapshot = Path(snapshot_dir) / source.name
            shutil.copyfile(source, snapshot)
            result = client.process(
                snapshot, profile, "analyze", job_id=job_id, report=("xml",), keep_name=True
            )
        if result.report_xml is None:
            raise RuntimeError("callas не вернул XML-отчёт проверки")
        return result, read_report_root(result.report_xml)
    finally:
        try:
            client.delete(job_id)
        except Exception:  # noqa: BLE001 — уборка не должна ломать проверку
            pass
        job_dir = getattr(client, "job_dir", None)
        if job_dir is not None:
            shutil.rmtree(job_dir(job_id), ignore_errors=True)
