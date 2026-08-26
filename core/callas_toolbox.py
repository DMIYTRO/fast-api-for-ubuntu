"""Safe adapter for the installed callas pdfToolbox CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from core.tool_runner import ExternalToolError, run_command
from server.settings import Settings


class CallasToolboxError(RuntimeError):
    """Ошибка конфигурации или выполнения callas CLI."""


@dataclass(frozen=True, slots=True)
class CallasResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class CallasToolbox:
    def __init__(self, settings: Settings | None = None) -> None:
        settings = settings or Settings.from_env()
        self.cli_path = Path(settings.callas_cli_path).expanduser().resolve()
        self.cache_dir = Path(settings.callas_cache_dir).expanduser().resolve()
        self.timeout_seconds = settings.callas_timeout_seconds

    def _run(self, *args: str) -> CallasResult:
        if not self.cli_path.is_file():
            raise CallasToolboxError(f"CLI callas не найден: {self.cli_path}")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        command = [str(self.cli_path), f"--cachefolder={self.cache_dir}", "--noprogress", *args]
        try:
            completed = run_command(command, timeout=self.timeout_seconds, text=True,
                                     capture_output=True)
        except (ExternalToolError, OSError) as exc:
            raise CallasToolboxError(f"Не удалось запустить callas pdfToolbox CLI: {exc}") from exc
        result = CallasResult(tuple(command), completed.returncode,
                              completed.stdout or "", completed.stderr or "")
        if completed.returncode >= 100:
            detail = (result.stderr.strip() or result.stdout.strip() or "нет подробностей")
            raise CallasToolboxError(f"callas завершился с ошибкой {completed.returncode}: {detail}")
        return result

    def version(self) -> str:
        return self._run("--version").stdout.strip()

    def status(self) -> str:
        return self._run("--status").stdout.strip()

    def merge_pdfs(self, input_paths: list[Path], output_path: Path) -> CallasResult:
        if not input_paths:
            raise CallasToolboxError("Список PDF-файлов для объединения пуст.")
        resolved = [Path(path).expanduser().resolve() for path in input_paths]
        missing = next((path for path in resolved if not path.is_file()), None)
        if missing:
            raise CallasToolboxError(f"Входной PDF не найден: {missing}")
        output = Path(output_path).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        result = self._run("--mergepdf", f"--outputfile={output}", *(str(path) for path in resolved))
        if not output.is_file() or output.stat().st_size == 0:
            raise CallasToolboxError(f"callas не создал итоговый PDF: {output}")
        return result
