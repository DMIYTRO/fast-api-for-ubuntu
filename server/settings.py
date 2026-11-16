"""Server settings."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import math
from pathlib import Path

from config.profiles import (
    DEFAULT_AUTO_CORRECT_MAX_MM,
    DEFAULT_CORRECTION_CONFIRM_MAX_MM,
    DEFAULT_MAX_DPI,
    DEFAULT_TARGET_DPI,
)


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


def _env_optional_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value.strip().replace(",", "."))
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError("invalid percentage")
        return parsed
    except ValueError as exc:
        raise ValueError(f"{name} должен быть числом (процент), получено: {value!r}") from exc


def _env_correction_limit(name: str, default: float = DEFAULT_AUTO_CORRECT_MAX_MM) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip().replace(",", "."))
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError("invalid limit")
        return parsed
    except ValueError as exc:
        raise ValueError(
            f"{name} должен быть неотрицательным числом (мм), получено: {value!r}"
        ) from exc


def _env_max_dpi(name: str, default: float = DEFAULT_MAX_DPI) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value.strip().replace(",", "."))
        if not math.isfinite(parsed) or parsed < DEFAULT_TARGET_DPI:
            raise ValueError("invalid dpi")
        return parsed
    except ValueError as exc:
        raise ValueError(
            f"{name} должен быть числом DPI не меньше {DEFAULT_TARGET_DPI:g}, получено: {value!r}"
        ) from exc


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Профиль .kfpx сделан в desktop pdfToolbox и лежит на сервере в «Image Magic/» (callas_profiles/).
# Прежний профиль «Convert colors/Convert to CMYK, keep spot colors (ISO Coated v2 (ECI))»
# по-прежнему можно задать через IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE.
DEFAULT_RGB_TO_CMYK_PROFILE = "Image Magic/NEW_Convert color to 4c for digital printing"
# Профиль-обёртка: применяет QuickFix JSON из переменной quickfix_config
# (scripts/build_quickfix_profile.py, callas_profiles/Apply QuickFix config.kfpx).
DEFAULT_PDF_RESIZE_PROFILE = "Image Magic/Apply QuickFix config"
DEFAULT_BLACK_SOLID_AREA_THRESHOLD = 0.25
# Нижний порог доступности кнопки «в составной чёрный»: доля площади страницы с чистым K100.
DEFAULT_BLACK_SOLID_FIX_AREA_THRESHOLD = 0.01
DEFAULT_HIGH_PPI_LIMIT = 500.0
# Стандартный fixup callas: уменьшает до 300 ppi всё, что выше 300 ppi (переменных не принимает).
DEFAULT_DOWNSAMPLE_PROFILE = "PDF fixups/Downsample image resolution to specified value"


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _callas_poll_intervals() -> tuple[float, float]:
    """Начальный и максимальный интервал опроса callas; максимум не меньше начального."""
    interval = max(0.1, float(os.environ.get("IMAGE_MAGIC_CALLAS_POLL_INTERVAL_SECONDS", "0.2")))
    ceiling = max(interval, float(os.environ.get("IMAGE_MAGIC_CALLAS_POLL_MAX_INTERVAL_SECONDS", "2")))
    return interval, ceiling


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
    # Одновременных проверок PitStop Server (1 — строго последовательно, 1..8).
    pitstop_max_concurrent: int = 4
    pitstop_mac_shared_root: Path = Path("/mnt/shared")
    pitstop_windows_shared_root: str = "//192.168.88.100/shared"
    pitstop_profiles: tuple[tuple[str, str, str], ...] = ()
    callas_url: str = ""
    callas_token: str = field(default="", repr=False)
    callas_share: Path = Path("/mnt/shared")
    callas_server_share: str = "/mnt/shared"
    callas_user: str = "image-magic"
    # Устарело: склейка PDF всегда идёт через callas; значение читается, но игнорируется.
    callas_merge_enabled: bool = False
    callas_render_enabled: bool = False
    callas_process_enabled: bool = False
    callas_inkcoverage_enabled: bool = False
    # Переопределение порогов красочности профиля (None — из профиля направления).
    ink_min_percent: float | None = None
    ink_max_percent: float | None = None
    # Предел автоматической коррекции размера (мм обрезки), IMAGE_MAGIC_AUTO_CORRECT_MAX_MM.
    auto_correct_max_mm: float = DEFAULT_AUTO_CORRECT_MAX_MM
    # Жёсткий максимум коррекции с подтверждением оператора (выше — отклонение).
    correction_confirm_max_mm: float = DEFAULT_CORRECTION_CONFIRM_MAX_MM
    # Растры с верным размером и разрешением выше max_dpi уменьшаются до целевого DPI.
    downsample_high_dpi_enabled: bool = True
    max_dpi: float = DEFAULT_MAX_DPI
    strip_icc_cmyk: bool = False
    # Автоматический перевод RGB → CMYK итогового PDF процесс-планом callas.
    callas_rgb_to_cmyk_enabled: bool = False
    callas_rgb_to_cmyk_profile: str = DEFAULT_RGB_TO_CMYK_PROFILE
    # Коррекция размера PDF QuickFix callas (те же правила, что у растра); выключена по умолчанию.
    callas_pdf_resize_enabled: bool = False
    callas_pdf_resize_profile: str = DEFAULT_PDF_RESIZE_PROFILE
    # Автоматические допечатные проверки callas на итоговом PDF: шрифты (Warn Fonts),
    # растры выше предела ppi, крупная плашка 100% K (цветоделения).
    callas_prepress_enabled: bool = True
    black_solid_area_threshold: float = DEFAULT_BLACK_SOLID_AREA_THRESHOLD
    black_solid_fix_area_threshold: float = DEFAULT_BLACK_SOLID_FIX_AREA_THRESHOLD
    high_ppi_limit: float = DEFAULT_HIGH_PPI_LIMIT
    callas_downsample_profile: str = DEFAULT_DOWNSAMPLE_PROFILE
    callas_request_timeout_seconds: float = 30.0
    callas_job_timeout_seconds: float = 600.0
    callas_poll_interval_seconds: float = 0.2
    callas_poll_max_interval_seconds: float = 2.0
    # 6 — лимит рендер-линии сервера callas (из 8 рабочих слотов).
    callas_max_concurrent_jobs: int = 6
    callas_max_retries: int = 4

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
        auto_correct_max_mm = _env_correction_limit("IMAGE_MAGIC_AUTO_CORRECT_MAX_MM")
        correction_confirm_max_mm = _env_correction_limit(
            "IMAGE_MAGIC_CORRECTION_CONFIRM_MAX_MM",
            max(DEFAULT_CORRECTION_CONFIRM_MAX_MM, auto_correct_max_mm),
        )
        if correction_confirm_max_mm < auto_correct_max_mm:
            raise ValueError(
                "IMAGE_MAGIC_CORRECTION_CONFIRM_MAX_MM не может быть меньше "
                f"IMAGE_MAGIC_AUTO_CORRECT_MAX_MM ({auto_correct_max_mm:g} мм)"
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
            pitstop_max_concurrent=max(
                1,
                min(
                    8,
                    int(
                        os.environ.get(
                            "IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT", "4"
                        ).strip()
                        or "4"
                    ),
                ),
            ),
            pitstop_mac_shared_root=Path(
                os.environ.get(
                    "IMAGE_MAGIC_PITSTOP_MAC_SHARED_ROOT", "/mnt/shared"
                )
            ).expanduser(),
            pitstop_windows_shared_root=os.environ.get(
                "IMAGE_MAGIC_PITSTOP_WINDOWS_SHARED_ROOT",
                "//192.168.88.100/shared",
            ).strip(),
            pitstop_profiles=pitstop_profiles,
            callas_url=os.environ.get("IMAGE_MAGIC_CALLAS_URL", "").strip(),
            callas_token=os.environ.get("IMAGE_MAGIC_CALLAS_TOKEN", "").strip(),
            callas_share=Path(
                os.environ.get("IMAGE_MAGIC_CALLAS_SHARE", "/mnt/shared")
            ).expanduser(),
            callas_server_share=os.environ.get(
                "IMAGE_MAGIC_CALLAS_SERVER_SHARE", "/mnt/shared"
            ).strip()
            or "/mnt/shared",
            callas_user=os.environ.get("IMAGE_MAGIC_CALLAS_USER", "image-magic").strip()
            or "image-magic",
            callas_merge_enabled=_env_bool("IMAGE_MAGIC_CALLAS_MERGE_ENABLED", False),
            callas_render_enabled=_env_bool("IMAGE_MAGIC_CALLAS_RENDER_ENABLED", False),
            callas_process_enabled=_env_bool("IMAGE_MAGIC_CALLAS_PROCESS_ENABLED", False),
            callas_inkcoverage_enabled=_env_bool(
                "IMAGE_MAGIC_CALLAS_INKCOVERAGE_ENABLED", False
            ),
            ink_min_percent=_env_optional_float("IMAGE_MAGIC_INK_MIN_PERCENT"),
            ink_max_percent=_env_optional_float("IMAGE_MAGIC_INK_MAX_PERCENT"),
            auto_correct_max_mm=auto_correct_max_mm,
            correction_confirm_max_mm=correction_confirm_max_mm,
            downsample_high_dpi_enabled=_env_bool(
                "IMAGE_MAGIC_DOWNSAMPLE_HIGH_DPI_ENABLED", True
            ),
            max_dpi=_env_max_dpi("IMAGE_MAGIC_MAX_DPI"),
            strip_icc_cmyk=_env_bool("IMAGE_MAGIC_STRIP_ICC_CMYK", False),
            callas_rgb_to_cmyk_enabled=_env_bool(
                "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_ENABLED", False
            ),
            callas_rgb_to_cmyk_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE", ""
            ).strip()
            or DEFAULT_RGB_TO_CMYK_PROFILE,
            callas_pdf_resize_enabled=_env_bool("IMAGE_MAGIC_CALLAS_PDF_RESIZE_ENABLED", False),
            callas_pdf_resize_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_PDF_RESIZE_PROFILE", ""
            ).strip()
            or DEFAULT_PDF_RESIZE_PROFILE,
            callas_prepress_enabled=_env_bool("IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED", True),
            black_solid_area_threshold=_env_float(
                "IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD",
                DEFAULT_BLACK_SOLID_AREA_THRESHOLD, minimum=0.0, maximum=1.0,
            ),
            black_solid_fix_area_threshold=_env_float(
                "IMAGE_MAGIC_BLACK_SOLID_FIX_AREA_THRESHOLD",
                DEFAULT_BLACK_SOLID_FIX_AREA_THRESHOLD, minimum=0.0, maximum=1.0,
            ),
            callas_downsample_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_DOWNSAMPLE_PROFILE", ""
            ).strip()
            or DEFAULT_DOWNSAMPLE_PROFILE,
            high_ppi_limit=_env_float(
                "IMAGE_MAGIC_HIGH_PPI_LIMIT", DEFAULT_HIGH_PPI_LIMIT, minimum=72.0, maximum=5000.0
            ),
            callas_request_timeout_seconds=max(
                1.0,
                float(os.environ.get("IMAGE_MAGIC_CALLAS_REQUEST_TIMEOUT_SECONDS", "30")),
            ),
            callas_job_timeout_seconds=max(
                1.0,
                float(os.environ.get("IMAGE_MAGIC_CALLAS_JOB_TIMEOUT_SECONDS", "600")),
            ),
            callas_poll_interval_seconds=_callas_poll_intervals()[0],
            callas_poll_max_interval_seconds=_callas_poll_intervals()[1],
            callas_max_concurrent_jobs=max(
                1, int(os.environ.get("IMAGE_MAGIC_CALLAS_MAX_CONCURRENT_JOBS", "6"))
            ),
            callas_max_retries=max(
                0, int(os.environ.get("IMAGE_MAGIC_CALLAS_MAX_RETRIES", "4"))
            ),
        )
