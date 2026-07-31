"""Batch upload generated previews to Sborka's FTP folder."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from ftplib import FTP, all_errors
import json
import logging
from pathlib import Path


logger = logging.getLogger("image_magic.ftp")
FTP_ERRORS = (OSError, *all_errors)


class FtpPreviewUploadError(RuntimeError):
    """A preview batch could not be fully uploaded."""


class FtpPreviewUploader:
    """Upload many files over one FTP connection."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        remote_dir: str = "inbox/press/",
        timeout: int = 20,
        ftp_factory: Callable[[], FTP] = FTP,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.remote_dir = remote_dir.strip("/")
        self.timeout = timeout
        self.ftp_factory = ftp_factory

    def upload(self, preview_paths: Iterable[Path | str]) -> list[str]:
        """Upload all paths using a single connected FTP client."""
        paths = list(dict.fromkeys(Path(path) for path in preview_paths))
        missing = [path.name for path in paths if not path.is_file()]
        if missing:
            raise FtpPreviewUploadError(
                "Не найдены файлы превью: " + ", ".join(missing)
            )
        if not paths:
            return []

        ftp = self.ftp_factory()
        uploaded: list[str] = []
        failed: list[str] = []
        try:
            ftp.connect(self.host, timeout=self.timeout)
            ftp.login(self.username, self.password)
            self._change_to_remote_dir(ftp)
            for path in paths:
                try:
                    with path.open("rb") as source:
                        ftp.storbinary(f"STOR {path.name}", source)
                    uploaded.append(path.name)
                except FTP_ERRORS as exc:
                    logger.warning(
                        "preview.ftp_upload_failed file=%s error=%s", path.name, exc
                    )
                    failed.append(path.name)
        except FTP_ERRORS as exc:
            raise FtpPreviewUploadError(
                f"Не удалось подключиться к FTP-серверу: {exc}"
            ) from exc
        finally:
            try:
                ftp.quit()
            except FTP_ERRORS:
                try:
                    ftp.close()
                except OSError:
                    pass

        if failed:
            raise FtpPreviewUploadError(
                "Не удалось загрузить превью: " + ", ".join(failed)
            )
        logger.info("preview.ftp_uploaded count=%s", len(uploaded))
        return uploaded

    def _change_to_remote_dir(self, ftp: FTP) -> None:
        """Use the configured folder, accommodating accounts jailed in inbox/."""
        if not self.remote_dir:
            return
        candidates = [self.remote_dir]
        if self.remote_dir.startswith("inbox/"):
            candidates.append(self.remote_dir.removeprefix("inbox/"))
        failures: list[Exception] = []
        for directory in dict.fromkeys(candidates):
            try:
                ftp.cwd(directory)
                logger.info("preview.ftp_directory directory=%s", directory)
                return
            except all_errors as exc:
                failures.append(exc)
        raise FtpPreviewUploadError(
            "FTP-папка превью недоступна: " + ", ".join(candidates)
        ) from failures[-1]


def build_ftp_preview_uploader(
    credentials_path: Path, *, timeout: int = 20
) -> Callable[[Iterable[Path | str]], list[str]] | None:
    """Read local ignored credentials and build the uploader when configured."""
    if not credentials_path.is_file():
        logger.info("preview.ftp_disabled credentials_file_missing")
        return None
    try:
        credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
        uploader = FtpPreviewUploader(
            str(credentials["host"]),
            str(credentials["username"]),
            str(credentials["password"]),
            remote_dir=str(credentials.get("remote_dir", "inbox/press/")),
            timeout=timeout,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FtpPreviewUploadError(
            f"Некорректный файл FTP-учётных данных: {credentials_path.name}"
        ) from exc
    return uploader.upload
