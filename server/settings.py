"""Server settings."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import math
from pathlib import Path
import logging

from config.check_policies import (
    DEFAULT_INPUT_CHECK_POLICY,
    DEFAULT_STRICT_SOURCE_DIRS,
    POLICIES,
    POLICY_FULL,
)
from config.overprint import DEFAULT_OVERPRINT_IGNORE_SPOTS, DEFAULT_OVERPRINT_PROFILE
from config.profiles import (
    DEFAULT_AUTO_CORRECT_MAX_MM,
    DEFAULT_CORRECTION_CONFIRM_MAX_MM,
    DEFAULT_MAX_DPI,
    DEFAULT_NO_SCALE_NAME_MARKERS,
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
# Наименьшая сторона сплошного поля K100 (мм), которое считается плашкой: текст и тонкие
# линии (вектор, кривые или растр) тоньше этого в долю площади не попадают и остаются K100.
DEFAULT_BLACK_SOLID_MIN_SIZE_MM = 4.0
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


def _env_csv_set(name: str, default: str) -> frozenset[str]:
    """Comma-separated values; an empty or missing variable means ``default``."""
    raw = os.environ.get(name, "").strip() or default
    return frozenset(item.strip() for item in raw.split(",") if item.strip())


def _env_no_scale_markers(
    name: str, default: tuple[str, ...] = DEFAULT_NO_SCALE_NAME_MARKERS
) -> tuple[str, ...]:
    """Метки «без масштабирования» через запятую; не задано — по умолчанию, пусто — выключено."""
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    markers: list[str] = []
    for item in raw.split(","):
        marker = item.strip()
        if marker and marker.casefold() not in {value.casefold() for value in markers}:
            markers.append(marker)
    return tuple(markers)


def _env_name_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Имена через запятую без повторов (регистр не важен); не задано — ``default``, пусто — ничего."""
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    names: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if value and value.casefold() not in {known.casefold() for known in names}:
            names.append(value)
    return tuple(names)


def _env_input_check_policy(name: str) -> str:
    """Политика проверки корня входной папки; неизвестное значение — ``full`` (как раньше)."""
    raw = os.environ.get(name, "").strip().casefold()
    if not raw:
        return DEFAULT_INPUT_CHECK_POLICY
    if raw not in POLICIES:
        logging.getLogger("image_magic").warning(
            "%s=%r неизвестна (доступны: %s); включена полная проверка",
            name,
            raw,
            ", ".join(POLICIES),
        )
        return POLICY_FULL
    return raw


def _env_strict_source_dirs(
    name: str, default: tuple[str, ...] = DEFAULT_STRICT_SOURCE_DIRS
) -> tuple[str, ...]:
    """Папки полной проверки через запятую; не задано — по умолчанию, пусто — нет таких папок."""
    raw = os.environ.get(name)
    if raw is None:
        return tuple(default)
    names: list[str] = []
    for item in raw.split(","):
        folder = item.strip().strip("/\\")
        if not folder or folder in {".", ".."} or "/" in folder or "\\" in folder:
            continue
        if folder.casefold() not in {value.casefold() for value in names}:
            names.append(folder)
    return tuple(names)


def _env_allowed_roots() -> tuple[Path, ...]:
    """IMAGE_MAGIC_ALLOWED_ROOTS (через os.pathsep), как в control_panel; пусто — папка проекта."""
    configured = os.environ.get("IMAGE_MAGIC_ALLOWED_ROOTS", "")
    roots = [
        Path(value).expanduser().resolve()
        for value in configured.split(os.pathsep)
        if value.strip()
    ]
    return tuple(roots or [PROJECT_DIR.resolve()])


def _env_fetch_mode(name: str) -> str:
    raw = os.environ.get(name, "").strip().lower() or "move"
    if raw not in {"move", "copy"}:
        # Опечатка не должна удалять исходники: неизвестное значение — безопасный copy.
        logging.getLogger("image_magic").warning(
            "%s=%r не распознан (move|copy), используется copy", name, raw
        )
        return "copy"
    return raw


def _env_fetch_routes(name: str) -> tuple[tuple[tuple[tuple[str, ...], str], ...], str]:
    """(маршруты, ошибка): неверный формат не роняет приложение, а запрещает получение."""
    from services.order_fetch import FetchConfigError, parse_routes

    try:
        return parse_routes(os.environ.get(name, "")), ""
    except FetchConfigError as exc:
        logging.getLogger("image_magic").warning("%s: %s", name, exc)
        return (), f"{name}: {exc}"


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
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
    # Схема послепечатной обработки, загруженная заказчиком
    # (https://sborka.ua/preview/<filename>-preview.png, services/customer_preview.py).
    sborka_customer_preview_enabled: bool = True
    sborka_customer_preview_max_concurrent: int = 3
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
    # Повторы временного сбоя PitStop (0–3) и пауза перед первым повтором, с;
    # пауза растёт с каждым повтором (3 с, 6 с, ...).
    pitstop_transient_retries: int = 2
    pitstop_retry_pause_seconds: float = 3.0
    pitstop_mac_shared_root: Path = Path("/mnt/shared")
    pitstop_windows_shared_root: str = "//192.168.88.100/shared"
    pitstop_profiles: tuple[tuple[str, str, str], ...] = ()
    # Проверка PitStop PDF после принятого исправления Callas сравнивается с проверкой
    # исходного PDF: находки этих действий, добавленные самим исправлением, не считаются.
    pitstop_fix_baseline_enabled: bool = True
    pitstop_fix_baseline_actions: frozenset[str] = frozenset({"2079", "3004"})
    pitstop_fix_baseline_profiles: frozenset[str] = frozenset({"digital"})
    # Исправление, которое только дорисовало вылеты, сравнивается по всем действиям
    # PitStop: всё новое по сравнению с PDF клиента принесли вылеты.
    pitstop_bleed_fix_all_actions: bool = True
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
    # Светлые участки (сумма красок от ink_light_floor_percent до ink_min_percent): флаг —
    # если они занимают не меньше ink_light_min_area_percent площади страницы
    # (IMAGE_MAGIC_INK_LIGHT_MIN_AREA_PERCENT, 50 — «большая часть макета»), участки уже
    # ink_light_min_width_mm не считаются, ниже ink_light_floor_percent — белая бумага
    # (IMAGE_MAGIC_INK_LIGHT_FLOOR_PERCENT, 3). См. services/callas/ink_coverage.py.
    ink_light_min_area_percent: float = 50.0
    ink_light_min_width_mm: float = 2.5
    ink_light_floor_percent: float = 3.0
    # Предел автоматической коррекции размера (мм обрезки), IMAGE_MAGIC_AUTO_CORRECT_MAX_MM.
    auto_correct_max_mm: float = DEFAULT_AUTO_CORRECT_MAX_MM
    # Жёсткий максимум коррекции с подтверждением оператора (выше — отклонение).
    correction_confirm_max_mm: float = DEFAULT_CORRECTION_CONFIRM_MAX_MM
    # Растры с верным размером и разрешением выше max_dpi уменьшаются до целевого DPI.
    downsample_high_dpi_enabled: bool = True
    max_dpi: float = DEFAULT_MAX_DPI
    # Метки в имени файла, при которых макет не масштабируется (только белые поля или
    # обрезка), IMAGE_MAGIC_NO_SCALE_NAME_MARKERS; пустой кортеж выключает правило.
    no_scale_name_markers: tuple[str, ...] = DEFAULT_NO_SCALE_NAME_MARKERS
    # Политика проверки файлов из корня входной папки (config/check_policies.py):
    # "basic" — облегчённая, "full" — как раньше (выключатель), IMAGE_MAGIC_INPUT_CHECK_POLICY.
    input_check_policy: str = DEFAULT_INPUT_CHECK_POLICY
    # Подпапки входной папки с полной проверкой, IMAGE_MAGIC_STRICT_SOURCE_DIRS
    # (через запятую; пусто — таких папок нет).
    strict_source_dirs: tuple[str, ...] = DEFAULT_STRICT_SOURCE_DIRS
    strip_icc_cmyk: bool = False
    # Автоматический перевод RGB → CMYK итогового PDF процесс-планом callas.
    callas_rgb_to_cmyk_enabled: bool = False
    callas_rgb_to_cmyk_profile: str = DEFAULT_RGB_TO_CMYK_PROFILE
    # Снимать ICC-профили CMYK/Gray и OutputIntent у PDF без RGB тем же профилем
    # конвертации; действует, только если конвертер RGB → CMYK создан.
    callas_strip_icc_enabled: bool = True
    # Коррекция размера PDF QuickFix callas (те же правила, что у растра); выключена по умолчанию.
    callas_pdf_resize_enabled: bool = False
    callas_pdf_resize_profile: str = DEFAULT_PDF_RESIZE_PROFILE
    # Режим «Автоматически»: поля вылета дорисовываются планом Callas size_correction при
    # осмотре файла и принимаются сами, если сравнение подтвердило «только вылеты»
    # (services/callas_auto_bleed.py). Нужен IMAGE_MAGIC_CALLAS_PROCESS_ENABLED.
    callas_auto_bleed_enabled: bool = True
    callas_auto_bleed_max_concurrent: int = 2
    # Автоматические допечатные проверки callas на итоговом PDF: шрифты (Warn Fonts),
    # растры выше предела ppi, крупная плашка 100% K (цветоделения).
    callas_prepress_enabled: bool = True
    # Overprint итогового PDF (services/overprint.py): overprint не только на технических
    # красках — ошибка заказа в любой политике проверки; не зависит от callas_prepress_enabled.
    callas_overprint_enabled: bool = True
    callas_overprint_profile: str = DEFAULT_OVERPRINT_PROFILE
    overprint_ignore_spots: tuple[str, ...] = DEFAULT_OVERPRINT_IGNORE_SPOTS
    black_solid_area_threshold: float = DEFAULT_BLACK_SOLID_AREA_THRESHOLD
    black_solid_fix_area_threshold: float = DEFAULT_BLACK_SOLID_FIX_AREA_THRESHOLD
    black_solid_min_size_mm: float = DEFAULT_BLACK_SOLID_MIN_SIZE_MM
    high_ppi_limit: float = DEFAULT_HIGH_PPI_LIMIT
    callas_downsample_profile: str = DEFAULT_DOWNSAMPLE_PROFILE
    callas_request_timeout_seconds: float = 30.0
    callas_job_timeout_seconds: float = 600.0
    callas_poll_interval_seconds: float = 0.2
    callas_poll_max_interval_seconds: float = 2.0
    # 6 — лимит рендер-линии сервера callas (из 8 рабочих слотов).
    callas_max_concurrent_jobs: int = 6
    callas_max_retries: int = 4
    # Входная папка по умолчанию и разрешённые корни (те же переменные, что у control_panel).
    input_dir: Path = (PROJECT_DIR / "input_files").resolve()
    allowed_roots: tuple[Path, ...] = (PROJECT_DIR.resolve(),)
    # «Получить заказ» (services/order_fetch.py): перенос из сетевых папок во входную.
    fetch_source_root: Path = Path("/mnt/work20")
    # Полная замена маршрутов «src1|src2>dst;...», пусто — маршруты по умолчанию.
    fetch_routes: tuple[tuple[tuple[str, ...], str], ...] = ()
    # Ошибка разбора IMAGE_MAGIC_FETCH_ROUTES: получение заказов отказывает с этим текстом.
    fetch_routes_error: str = ""
    # Корень источника обязан быть точкой монтирования (иначе — пустая локальная папка).
    fetch_require_mount: bool = True
    fetch_workers: int = 4
    fetch_mode: str = "move"
    # Файл, изменённый позже этого, ещё дописывается клиентом — не трогаем.
    fetch_min_age_seconds: float = 10.0
    fetch_retries: int = 3
    # Окно стабильности исходника (с): последний stat — не раньше, чем через столько
    # секунд после stat сканирования (атрибуты CIFS кэшируются).
    fetch_stable_seconds: float = 2.0
    # None — <log_dir>/order-fetch.log.
    fetch_log: Path | None = None

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
        fetch_routes, fetch_routes_error = _env_fetch_routes("IMAGE_MAGIC_FETCH_ROUTES")
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
            sborka_customer_preview_enabled=_env_bool(
                "IMAGE_MAGIC_SBORKA_CUSTOMER_PREVIEW_ENABLED", True
            ),
            sborka_customer_preview_max_concurrent=_env_int(
                "IMAGE_MAGIC_SBORKA_CUSTOMER_PREVIEW_MAX_CONCURRENT", 3, minimum=1, maximum=16
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
            pitstop_transient_retries=int(
                _env_float("IMAGE_MAGIC_PITSTOP_TRANSIENT_RETRIES", 2, minimum=0, maximum=3)
            ),
            pitstop_retry_pause_seconds=_env_float(
                "IMAGE_MAGIC_PITSTOP_RETRY_PAUSE_SECONDS", 3.0, minimum=0.0, maximum=60.0
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
            pitstop_fix_baseline_enabled=_env_bool(
                "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ENABLED", True
            ),
            pitstop_fix_baseline_actions=_env_csv_set(
                "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_ACTIONS", "2079,3004"
            ),
            pitstop_fix_baseline_profiles=_env_csv_set(
                "IMAGE_MAGIC_PITSTOP_FIX_BASELINE_PROFILES", "digital"
            ),
            pitstop_bleed_fix_all_actions=_env_bool(
                "IMAGE_MAGIC_PITSTOP_BLEED_FIX_ALL_ACTIONS", True
            ),
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
            ink_light_min_area_percent=_env_float(
                "IMAGE_MAGIC_INK_LIGHT_MIN_AREA_PERCENT", 50.0, minimum=0.0, maximum=100.0
            ),
            ink_light_min_width_mm=_env_float(
                "IMAGE_MAGIC_INK_LIGHT_MIN_WIDTH_MM", 2.5, minimum=0.0, maximum=100.0
            ),
            ink_light_floor_percent=_env_float(
                "IMAGE_MAGIC_INK_LIGHT_FLOOR_PERCENT", 3.0, minimum=0.0, maximum=100.0
            ),
            auto_correct_max_mm=auto_correct_max_mm,
            correction_confirm_max_mm=correction_confirm_max_mm,
            downsample_high_dpi_enabled=_env_bool(
                "IMAGE_MAGIC_DOWNSAMPLE_HIGH_DPI_ENABLED", True
            ),
            max_dpi=_env_max_dpi("IMAGE_MAGIC_MAX_DPI"),
            no_scale_name_markers=_env_no_scale_markers("IMAGE_MAGIC_NO_SCALE_NAME_MARKERS"),
            input_check_policy=_env_input_check_policy("IMAGE_MAGIC_INPUT_CHECK_POLICY"),
            strict_source_dirs=_env_strict_source_dirs("IMAGE_MAGIC_STRICT_SOURCE_DIRS"),
            strip_icc_cmyk=_env_bool("IMAGE_MAGIC_STRIP_ICC_CMYK", False),
            callas_rgb_to_cmyk_enabled=_env_bool(
                "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_ENABLED", False
            ),
            callas_strip_icc_enabled=_env_bool("IMAGE_MAGIC_CALLAS_STRIP_ICC_ENABLED", True),
            callas_rgb_to_cmyk_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE", ""
            ).strip()
            or DEFAULT_RGB_TO_CMYK_PROFILE,
            callas_pdf_resize_enabled=_env_bool("IMAGE_MAGIC_CALLAS_PDF_RESIZE_ENABLED", False),
            callas_pdf_resize_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_PDF_RESIZE_PROFILE", ""
            ).strip()
            or DEFAULT_PDF_RESIZE_PROFILE,
            callas_auto_bleed_enabled=_env_bool("IMAGE_MAGIC_CALLAS_AUTO_BLEED_ENABLED", True),
            callas_auto_bleed_max_concurrent=max(
                1,
                min(8, int(os.environ.get("IMAGE_MAGIC_CALLAS_AUTO_BLEED_MAX_CONCURRENT", "2").strip() or "2")),
            ),
            callas_prepress_enabled=_env_bool("IMAGE_MAGIC_CALLAS_PREPRESS_ENABLED", True),
            callas_overprint_enabled=_env_bool("IMAGE_MAGIC_CALLAS_OVERPRINT_ENABLED", True),
            callas_overprint_profile=os.environ.get(
                "IMAGE_MAGIC_CALLAS_OVERPRINT_PROFILE", ""
            ).strip()
            or DEFAULT_OVERPRINT_PROFILE,
            overprint_ignore_spots=_env_name_list(
                "IMAGE_MAGIC_OVERPRINT_IGNORE_SPOTS", DEFAULT_OVERPRINT_IGNORE_SPOTS
            ),
            black_solid_area_threshold=_env_float(
                "IMAGE_MAGIC_BLACK_SOLID_AREA_THRESHOLD",
                DEFAULT_BLACK_SOLID_AREA_THRESHOLD, minimum=0.0, maximum=1.0,
            ),
            black_solid_fix_area_threshold=_env_float(
                "IMAGE_MAGIC_BLACK_SOLID_FIX_AREA_THRESHOLD",
                DEFAULT_BLACK_SOLID_FIX_AREA_THRESHOLD, minimum=0.0, maximum=1.0,
            ),
            black_solid_min_size_mm=_env_float(
                "IMAGE_MAGIC_BLACK_SOLID_MIN_SIZE_MM",
                DEFAULT_BLACK_SOLID_MIN_SIZE_MM, minimum=0.0, maximum=100.0,
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
            input_dir=Path(
                os.environ.get("IMAGE_MAGIC_INPUT_DIR", PROJECT_DIR / "input_files")
            ).expanduser().resolve(),
            allowed_roots=_env_allowed_roots(),
            fetch_source_root=Path(
                os.environ.get("IMAGE_MAGIC_FETCH_SOURCE_ROOT", "").strip() or "/mnt/work20"
            ).expanduser(),
            fetch_routes=fetch_routes,
            fetch_routes_error=fetch_routes_error,
            fetch_require_mount=_env_bool("IMAGE_MAGIC_FETCH_REQUIRE_MOUNT", True),
            fetch_workers=_env_int("IMAGE_MAGIC_FETCH_WORKERS", 4, minimum=1, maximum=32),
            fetch_mode=_env_fetch_mode("IMAGE_MAGIC_FETCH_MODE"),
            fetch_min_age_seconds=_env_float(
                "IMAGE_MAGIC_FETCH_MIN_AGE_SECONDS", 10.0, minimum=0.0, maximum=86400.0
            ),
            fetch_retries=_env_int("IMAGE_MAGIC_FETCH_RETRIES", 3, minimum=0, maximum=10),
            fetch_stable_seconds=_env_float(
                "IMAGE_MAGIC_FETCH_STABLE_SECONDS", 2.0, minimum=0.0, maximum=600.0
            ),
            fetch_log=(
                Path(os.environ["IMAGE_MAGIC_FETCH_LOG"].strip()).expanduser()
                if os.environ.get("IMAGE_MAGIC_FETCH_LOG", "").strip()
                else None
            ),
        )
