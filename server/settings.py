"""Server settings."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
# Временный пароль для локального тестового сервера. Удалить после тестового этапа.
TEST_SERVER_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$N2Kma52ne4WjmBNi7AYvBw$"
    "VdDv4X2PUCX+vQGlJvbbjP5MdwqOR5Kqx3tjxYBG37s"
)


def _default_sborka_api_dir() -> Path:
    """Prefer the shared Sborka API checkout when it provides rework support."""
    shared_dir = PROJECT_DIR.parent / "sborka_api"
    if (shared_dir / "sborka_touser.py").is_file():
        return shared_dir
    return PROJECT_DIR / "sborka_api"


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
    sborka_api_dir: Path = _default_sborka_api_dir()
    sborka_timeout_seconds: int = 20
    sborka_enabled: bool = False
    pitstop_enabled: bool = False
    pitstop_host: str = ""
    pitstop_port: int = 22
    pitstop_username: str = ""
    pitstop_cli_path: str = ""
    pitstop_known_hosts_file: Path = Path.home() / ".ssh" / "known_hosts"
    pitstop_identity_file: Path | None = None
    pitstop_connect_timeout_seconds: int = 10
    pitstop_command_timeout_seconds: float = 180.0
    pitstop_mac_shared_root: Path = Path("/Users/admin")
    pitstop_windows_shared_root: str = r"C:\Mac\Home"
    pitstop_profiles: tuple[tuple[str, str, str], ...] = ()

    @classmethod
    def from_env(cls) -> "Settings":
        default_db = PROJECT_DIR / "image_magic.db"
        secure_value = os.environ.get("IMAGE_MAGIC_COOKIE_SECURE")
        password_hash = (
            os.environ.get("IMAGE_MAGIC_PASSWORD_HASH", "").strip()
            or TEST_SERVER_PASSWORD_HASH
        )
        pitstop_profiles = tuple(
            (direction, f"PitStop — {direction}", path)
            for direction, path in (
                (
                    "digital",
                    os.environ.get("IMAGE_MAGIC_PITSTOP_PROFILE_DIGITAL", "").strip(),
                ),
                (
                    "offset",
                    os.environ.get("IMAGE_MAGIC_PITSTOP_PROFILE_OFFSET", "").strip(),
                ),
            )
            if path
        )
        identity_value = os.environ.get(
            "IMAGE_MAGIC_PITSTOP_IDENTITY_FILE", ""
        ).strip()
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
            sborka_api_dir=Path(
                os.environ.get("IMAGE_MAGIC_SBORKA_API_DIR", _default_sborka_api_dir())
            ).expanduser().resolve(),
            sborka_timeout_seconds=max(
                1, int(os.environ.get("IMAGE_MAGIC_SBORKA_TIMEOUT_SECONDS", "20"))
            ),
            sborka_enabled=_env_bool(
                "IMAGE_MAGIC_SBORKA_ENABLED",
                (
                    Path(
                        os.environ.get(
                            "IMAGE_MAGIC_SBORKA_API_DIR", _default_sborka_api_dir()
                        )
                    ).expanduser()
                    / "sborka_api_key.txt"
                ).is_file(),
            ),
            pitstop_enabled=_env_bool("IMAGE_MAGIC_PITSTOP_ENABLED", False),
            pitstop_host=os.environ.get("IMAGE_MAGIC_PITSTOP_HOST", "").strip(),
            pitstop_port=max(
                1, int(os.environ.get("IMAGE_MAGIC_PITSTOP_PORT", "22"))
            ),
            pitstop_username=os.environ.get(
                "IMAGE_MAGIC_PITSTOP_USERNAME", ""
            ).strip(),
            pitstop_cli_path=os.environ.get(
                "IMAGE_MAGIC_PITSTOP_CLI_PATH", ""
            ).strip(),
            pitstop_known_hosts_file=Path(
                os.environ.get(
                    "IMAGE_MAGIC_PITSTOP_KNOWN_HOSTS",
                    Path.home() / ".ssh" / "known_hosts",
                )
            ).expanduser(),
            pitstop_identity_file=(
                Path(identity_value).expanduser() if identity_value else None
            ),
            pitstop_connect_timeout_seconds=max(
                1,
                int(
                    os.environ.get(
                        "IMAGE_MAGIC_PITSTOP_CONNECT_TIMEOUT_SECONDS", "10"
                    )
                ),
            ),
            pitstop_command_timeout_seconds=max(
                1.0,
                float(
                    os.environ.get(
                        "IMAGE_MAGIC_PITSTOP_COMMAND_TIMEOUT_SECONDS", "180"
                    )
                ),
            ),
            pitstop_mac_shared_root=Path(
                os.environ.get(
                    "IMAGE_MAGIC_PITSTOP_MAC_SHARED_ROOT", "/Users/admin"
                )
            ).expanduser(),
            pitstop_windows_shared_root=os.environ.get(
                "IMAGE_MAGIC_PITSTOP_WINDOWS_SHARED_ROOT", r"C:\Mac\Home"
            ).strip(),
            pitstop_profiles=pitstop_profiles,
        )
