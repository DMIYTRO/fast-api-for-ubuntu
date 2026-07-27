"""File-system lifecycle for checked orders.

The checker owns file creation.  This module owns only the later, explicit
operator transitions: accepting an order for print or returning it for
rework.  Keeping it here prevents API handlers and UI screens from acquiring
their own, subtly different file-moving rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import filecmp
from pathlib import Path
import shutil
from typing import Any


class FileLifecycleError(RuntimeError):
    """Raised when an order cannot safely be moved to its next state."""


@dataclass(frozen=True)
class FileTransition:
    source_paths: dict[str, str]
    pdf_path: str | None
    preview_paths: list[str]
    moves: tuple[tuple[str, str, bool], ...] = ()


class FileLifecycle:
    """Own the fixed folders and all-or-nothing moves for one input folder."""

    def __init__(self, input_dir: Path | str) -> None:
        self.input_dir = Path(input_dir).resolve()
        self.pdf_dir = self.input_dir / "PDF"
        self.preview_dir = self.input_dir / "Previews"
        self.troubles_dir = self.input_dir / "Troubles"
        self.processed_dir = self.input_dir / "Processed"
        self.print_pdf_dir = self.pdf_dir / "Print"
        self.processed_preview_dir = self.preview_dir / "Processed"

    def initialize(self) -> None:
        """Create managed folders before a check begins.

        The input folder itself remains the source location.  ``Processed``
        and the two nested output folders are deliberately not scanned by the
        existing batch processor, which scans top-level files only.
        """
        # ``BatchProcessor`` historically creates PDF/ and Previews/ itself.
        # Do not create those parents here: custom adapters may use mkdir()
        # without exist_ok=True.  Their transition subfolders are created only
        # when a real move is performed.
        for directory in (self.troubles_dir, self.processed_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def accept_for_print(self, order: dict[str, Any]) -> FileTransition:
        """Move a complete successful order into the internal print staging."""
        self.initialize()
        sources = self._source_paths(order)
        pdf = self._required_path(order.get("pdf_path"), "PDF")
        previews = self._required_paths(order.get("preview_paths") or [], "превью")
        moves = [
            *( (source, self.processed_dir / source.name) for source in sources ),
            (pdf, self.print_pdf_dir / pdf.name),
            *( (preview, self.processed_preview_dir / preview.name) for preview in previews ),
        ]
        completed = self._move_all(moves)
        return self._transition_from_moves(order, completed)

    def return_for_rework(self, order: dict[str, Any]) -> FileTransition:
        """Move every available artifact of an operator-returned order to Troubles."""
        self.initialize()
        target_dir = self.troubles_dir / str(order.get("order_id") or "UNPARSED")
        candidates = [*self._existing_source_paths(order)]
        pdf = self._optional_path(order.get("pdf_path"))
        if pdf:
            candidates.append(pdf)
        candidates.extend(
            path for path in (self._optional_path(value) for value in order.get("preview_paths") or []) if path
        )
        if not candidates:
            raise FileLifecycleError("Для возврата не найдено ни одного файла заказа.")
        completed = self._move_all(
            ((path, target_dir / path.name) for path in candidates),
            reuse_identical=True,
        )
        return self._transition_from_moves(order, completed)

    def route_to_errors(self, order: dict[str, Any]) -> FileTransition:
        """Best-effort containment for an incomplete or broken order."""
        try:
            return self.return_for_rework(order)
        except FileLifecycleError:
            return FileTransition({}, None, [])

    @staticmethod
    def rollback(transition: FileTransition) -> None:
        """Compensate a completed transition after a later persistence failure."""
        errors: list[str] = []
        for source_value, destination_value, reused in reversed(transition.moves):
            source = Path(source_value)
            destination = Path(destination_value)
            try:
                if source.exists():
                    continue
                source.parent.mkdir(parents=True, exist_ok=True)
                if reused:
                    shutil.copy2(destination, source)
                elif destination.exists():
                    shutil.move(str(destination), str(source))
            except OSError as exc:
                errors.append(f"{destination.name}: {exc}")
        if errors:
            raise FileLifecycleError(
                "Не удалось полностью отменить перемещение: " + "; ".join(errors)
            )

    def _source_paths(self, order: dict[str, Any]) -> list[Path]:
        paths = self._existing_source_paths(order)
        expected = len(order.get("files") or [])
        if not paths or len(paths) != expected:
            raise FileLifecycleError("Не найден один или несколько исходных файлов заказа.")
        return paths

    @staticmethod
    def _required_path(value: Any, label: str) -> Path:
        path = FileLifecycle._optional_path(value)
        if path is None:
            raise FileLifecycleError(f"Не найден или повреждён {label} заказа.")
        return path

    @staticmethod
    def _required_paths(values: list[Any], label: str) -> list[Path]:
        paths = [FileLifecycle._optional_path(value) for value in values]
        if not paths or any(path is None for path in paths):
            raise FileLifecycleError(f"Не найдено или повреждено {label} заказа.")
        return [path for path in paths if path is not None]

    @staticmethod
    def _optional_path(value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        return path if path.is_file() else None

    def _existing_source_paths(self, order: dict[str, Any]) -> list[Path]:
        paths = [self._optional_path(item.get("path")) for item in order.get("files") or []]
        return [path for path in paths if path is not None]

    @staticmethod
    def _move_all(
        moves: Any, *, reuse_identical: bool = False
    ) -> list[tuple[Path, Path, bool]]:
        planned = list(moves)
        destinations = [destination for _, destination in planned]
        if len(set(destinations)) != len(destinations):
            raise FileLifecycleError("В заказе есть файлы с одинаковым именем.")
        for source, destination in planned:
            if not destination.exists():
                continue
            if reuse_identical and filecmp.cmp(source, destination, shallow=False):
                continue
            raise FileLifecycleError(
                f"Файл уже существует в целевой папке: {destination.name}"
            )

        completed: list[tuple[Path, Path, bool]] = []
        try:
            for source, destination in planned:
                if destination.exists():
                    # An earlier check may already have copied this same
                    # failed file to Troubles.  Remove the duplicate source
                    # so the final state still contains one canonical copy.
                    source.unlink()
                    completed.append((source, destination, True))
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                completed.append((source, destination, False))
        except Exception as exc:
            for source, destination, reused in reversed(completed):
                if destination.exists() and not source.exists():
                    try:
                        if reused:
                            shutil.copy2(destination, source)
                        else:
                            shutil.move(str(destination), str(source))
                    except OSError:
                        pass
            raise FileLifecycleError(f"Не удалось переместить файлы заказа: {exc}") from exc
        return completed

    @staticmethod
    def _transition_from_moves(
        order: dict[str, Any], moves: list[tuple[Path, Path, bool]]
    ) -> FileTransition:
        destinations = {
            str(source): str(destination) for source, destination, _ in moves
        }
        source_paths = {
            str(item.get("path")): destinations[str(item.get("path"))]
            for item in order.get("files") or []
            if str(item.get("path")) in destinations
        }
        pdf = order.get("pdf_path")
        preview_paths = order.get("preview_paths") or []
        return FileTransition(
            source_paths=source_paths,
            pdf_path=destinations.get(str(pdf)) if pdf else None,
            preview_paths=[destinations[str(path)] for path in preview_paths if str(path) in destinations],
            moves=tuple(
                (str(source), str(destination), reused)
                for source, destination, reused in moves
            ),
        )
