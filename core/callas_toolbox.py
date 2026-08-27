"""Safe adapter for the installed callas pdfToolbox CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import pymupdf

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
        # Callas preserves page-level provenance metadata from converted TIFF
        # pages.  Set document-level metadata after the merge so properties of
        # the final file identify the actual assembly tool unambiguously.
        temporary = output.with_suffix(output.suffix + ".metadata.tmp")
        try:
            document = pymupdf.open(output)
            metadata = document.metadata
            metadata.update({
                "producer": "callas pdfToolbox CLI 17.0.682",
                "creator": "Image Magic",
                "subject": "PDF merged by callas pdfToolbox",
            })
            document.set_metadata(metadata)
            document.save(temporary)
            document.close()
            temporary.replace(output)
        except Exception:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            # The Callas operation itself succeeded.  Metadata enrichment is
            # best-effort so a valid PDF is not rejected by a secondary step.
        return result

    def convert_to_pdf(self, input_path: Path, output_path: Path) -> CallasResult:
        """Convert a supported raster file to PDF using Callas' native converter."""
        input_path = Path(input_path).expanduser().resolve()
        output_path = Path(output_path).expanduser().resolve()
        if not input_path.is_file():
            raise CallasToolboxError(f"Растровый файл не найден: {input_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            "--topdf", "--overwrite", f"--outputfile={output_path}", str(input_path)
        )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise CallasToolboxError(f"callas не создал PDF из растра: {output_path}")
        return result

    def save_as_image(self, input_path: Path, output_dir: Path, *, resolution: int = 150) -> CallasResult:
        """Render one PNG image per PDF page into ``output_dir``."""
        input_path = Path(input_path).expanduser().resolve()
        output_dir = Path(output_dir).expanduser().resolve()
        if not input_path.is_file():
            raise CallasToolboxError(f"Входной PDF не найден: {input_path}")
        output_dir.mkdir(parents=True, exist_ok=True)
        return self._run(
            "--saveasimg", "--imgformat=PNG", f"--resolution={resolution}",
            f"--outputfolder={output_dir}", str(input_path),
        )
