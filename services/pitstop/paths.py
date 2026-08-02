"""Safe path conversion for the Parallels shared-folder boundary."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath


class SharedPathError(ValueError):
    pass


def mac_shared_path_to_windows(
    path: Path,
    *,
    mac_shared_root: Path,
    windows_shared_root: PureWindowsPath,
) -> PureWindowsPath:
    """Map a local path only when it is contained by the configured share."""
    local_root = mac_shared_root.expanduser().resolve(strict=False)
    local_path = path.expanduser().resolve(strict=False)
    if not local_root.is_absolute() or not windows_shared_root.is_absolute():
        raise SharedPathError("корни общей папки должны быть абсолютными")
    try:
        relative = local_path.relative_to(local_root)
    except ValueError as exc:
        raise SharedPathError(
            f"путь не находится в разрешённой общей папке: {local_path}"
        ) from exc
    return windows_shared_root.joinpath(*relative.parts)
