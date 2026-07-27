"""Server settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent

def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    password_hash: str | None
    session_hours: int = 12
    cookie_name: str = "image_magic_session"
    cookie_secure: bool | None = None
    login_attempts: int = 5
    login_window_seconds: int = 300
    login_failure_delay_seconds: float = 0.5
    log_dir: Path = PROJECT_DIR / "logs"
    log_level: str = "INFO"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 10
    log_heartbeat_seconds: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = PROJECT_DIR / "image_magic.db"
        secure_value = os.environ.get("IMAGE_MAGIC_COOKIE_SECURE")
        password_hash = os.environ.get("IMAGE_MAGIC_PASSWORD_HASH", "").strip()
        if not password_hash:
            raise RuntimeError(
                "Не задан IMAGE_MAGIC_PASSWORD_HASH. "
                "Создайте хеш командой '.venv/bin/python manage.py set-password' "
                "и передайте его через окружение."
            )
        return cls(
            database_url=os.environ.get(
                "IMAGE_MAGIC_DATABASE_URL", f"sqlite:///{default_db}"
            ),
            password_hash=password_hash,
            session_hours=max(
                1, int(os.environ.get("IMAGE_MAGIC_SESSION_HOURS", "12"))
            ),
            cookie_name=os.environ.get(
                "IMAGE_MAGIC_SESSION_COOKIE", "image_magic_session"
            ),
            cookie_secure=(
                _env_bool("IMAGE_MAGIC_COOKIE_SECURE", False)
                if secure_value is not None
                else None
            ),
            login_attempts=max(
                1, int(os.environ.get("IMAGE_MAGIC_LOGIN_ATTEMPTS", "5"))
            ),
            login_window_seconds=max(
                1, int(os.environ.get("IMAGE_MAGIC_LOGIN_WINDOW_SECONDS", "300"))
            ),
            login_failure_delay_seconds=max(
                0.0,
                float(
                    os.environ.get(
                        "IMAGE_MAGIC_LOGIN_FAILURE_DELAY_SECONDS", "0.5"
                    )
                ),
            ),
            log_dir=Path(
                os.environ.get("IMAGE_MAGIC_LOG_DIR", PROJECT_DIR / "logs")
            ).expanduser(),
            log_level=os.environ.get("IMAGE_MAGIC_LOG_LEVEL", "INFO"),
            log_max_bytes=max(
                1024,
                int(
                    os.environ.get(
                        "IMAGE_MAGIC_LOG_MAX_BYTES", str(10 * 1024 * 1024)
                    )
                ),
            ),
            log_backup_count=max(
                1, int(os.environ.get("IMAGE_MAGIC_LOG_BACKUP_COUNT", "10"))
            ),
            log_heartbeat_seconds=max(
                10, int(os.environ.get("IMAGE_MAGIC_LOG_HEARTBEAT_SECONDS", "60"))
            ),
        )
