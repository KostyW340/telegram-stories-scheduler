from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)
_SYNC_MANAGED_DIR_LABELS: tuple[tuple[str, str], ...] = (
    ("onedrive", "OneDrive"),
    ("dropbox", "Dropbox"),
    ("google drive", "Google Drive"),
    ("googledrive", "GoogleDrive"),
    ("icloud", "iCloud"),
)


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        logger.debug("Environment file not found at %s", env_path)
        return

    logger.debug("Loading environment variables from %s", env_path)
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("export "):
            stripped = stripped[7:].strip()

        if "=" not in stripped:
            logger.warning("Ignoring malformed .env line %s in %s", line_number, env_path)
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = _strip_wrapping_quotes(value.strip())
        os.environ.setdefault(key, value)


def _get_env_int(name: str, default: int | None = None) -> int | None:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        logger.error("Environment variable %s must be an integer, got %r", name, raw_value)
        raise ValueError(f"{name} must be an integer") from exc


def _get_env_float(name: str, default: float | None = None) -> float | None:
    raw_value = os.getenv(name)
    if raw_value is None or raw_value == "":
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        logger.error("Environment variable %s must be a float, got %r", name, raw_value)
        raise ValueError(f"{name} must be a float") from exc


def _get_env_csv_ints(name: str) -> tuple[int, ...]:
    raw_value = os.getenv(name, "")
    if not raw_value.strip():
        return ()

    parsed: list[int] = []
    for token in raw_value.split(","):
        normalized = token.strip()
        if not normalized:
            continue
        try:
            parsed.append(int(normalized))
        except ValueError as exc:
            logger.error("Environment variable %s contains a non-integer value: %r", name, normalized)
            raise ValueError(f"{name} must contain only comma-separated integers") from exc

    return tuple(parsed)


def detect_sync_managed_path(path: Path) -> str | None:
    normalized_parts = [part.casefold() for part in path.parts]
    for marker, label in _SYNC_MANAGED_DIR_LABELS:
        if any(marker in part for part in normalized_parts):
            return label
    return None


@dataclass(slots=True, frozen=True)
class AppPaths:
    project_root: Path
    bundle_root: Path
    data_dir: Path
    database_path: Path
    photos_dir: Path
    videos_dir: Path
    prepared_photos_dir: Path
    prepared_videos_dir: Path
    temp_dir: Path
    logs_dir: Path
    sessions_dir: Path


@dataclass(slots=True, frozen=True)
class BotSettings:
    token: str | None
    allowed_user_ids: tuple[int, ...]
    api_base_url: str | None
    proxy_url: str | None

    def require_token(self) -> str:
        if not self.token:
            raise RuntimeError("BOT_TOKEN is required for bot runtime")
        return self.token


@dataclass(slots=True, frozen=True)
class TelegramClientSettings:
    api_id: int | None
    api_hash: str | None
    phone_number: str | None
    session_name: str
    device_model: str
    system_version: str
    app_version: str
    lang_code: str
    system_lang_code: str
    mtproto_proxy_host: str | None
    mtproto_proxy_port: int | None
    mtproto_proxy_secret: str | None

    def require_api_credentials(self) -> tuple[int, str]:
        if self.api_id is None or not self.api_hash:
            raise RuntimeError("API_ID and API_HASH are required for MTProto runtime")
        return self.api_id, self.api_hash


@dataclass(slots=True, frozen=True)
class MediaSettings:
    ffmpeg_bin: str
    ffprobe_bin: str
    max_video_duration_seconds: int
    max_video_size_bytes: int


@dataclass(slots=True, frozen=True)
class RuntimeSettings:
    timezone: str
    log_level: str
    scheduler_poll_interval_seconds: int
    worker_batch_size: int
    job_lock_ttl_seconds: int
    publish_retry_max_attempts: int
    publish_retry_base_delay_seconds: int
    publish_retry_max_delay_seconds: int
    publish_retry_window_seconds: int
    publish_retry_max_flood_wait_seconds: int
    one_time_min_lead_seconds: int
    connectivity_summary_interval_seconds: int
    mtproto_probe_interval_seconds: int
    mtproto_connect_timeout_seconds: float
    mtproto_probe_timeout_seconds: float
    mtproto_publish_timeout_seconds: float
    worker_cycle_timeout_seconds: float
    bot_polling_backoff_min_delay_seconds: float
    bot_polling_backoff_max_delay_seconds: float
    bot_polling_backoff_factor: float
    bot_polling_backoff_jitter: float
    bot_polling_backoff_reset_after_seconds: int


@dataclass(slots=True, frozen=True)
class Settings:
    paths: AppPaths
    bot: BotSettings
    telegram: TelegramClientSettings
    media: MediaSettings
    runtime: RuntimeSettings

    @property
    def database_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.paths.database_path.as_posix()}"

    @property
    def alembic_database_url(self) -> str:
        return f"sqlite:///{self.paths.database_path.as_posix()}"

    @property
    def session_file(self) -> Path:
        return self.paths.sessions_dir / f"{self.telegram.session_name}.session"

    @property
    def runtime_session_string_file(self) -> Path:
        return self.paths.sessions_dir / f"{self.telegram.session_name}.runtime-session.txt"

    def to_relative_runtime_path(self, path: Path) -> str:
        candidate = path.resolve()
        logical_roots = (
            ("prepared/photos", self.paths.prepared_photos_dir),
            ("prepared/videos", self.paths.prepared_videos_dir),
            ("photos", self.paths.photos_dir),
            ("videos", self.paths.videos_dir),
        )
        for prefix, root in logical_roots:
            try:
                suffix = candidate.relative_to(root.resolve())
                return (Path(prefix) / suffix).as_posix()
            except ValueError:
                continue
        try:
            relative = candidate.relative_to(self.paths.project_root)
            return relative.as_posix()
        except ValueError:
            return candidate.as_posix()

    def resolve_runtime_path(self, path_value: str) -> Path:
        candidate = Path(path_value)
        if candidate.is_absolute():
            return candidate
        normalized = candidate.as_posix()
        logical_roots = (
            ("prepared/photos", self.paths.prepared_photos_dir),
            ("prepared/videos", self.paths.prepared_videos_dir),
            ("photos", self.paths.photos_dir),
            ("videos", self.paths.videos_dir),
        )
        for prefix, root in logical_roots:
            if normalized == prefix:
                return root
            if normalized.startswith(f"{prefix}/"):
                return root / normalized.removeprefix(f"{prefix}/")
        return self.paths.project_root / candidate

    def ensure_runtime_paths(self) -> None:
        directories = (
            self.paths.data_dir,
            self.paths.photos_dir,
            self.paths.videos_dir,
            self.paths.prepared_photos_dir,
            self.paths.prepared_videos_dir,
            self.paths.temp_dir,
            self.paths.logs_dir,
            self.paths.sessions_dir,
        )
        for path in directories:
            path.mkdir(parents=True, exist_ok=True)
            logger.debug("Ensured runtime path exists: %s", path)

    def log_summary(self) -> None:
        logger.info(
            "Runtime configuration loaded: timezone=%s poll_interval=%ss batch_size=%s retry_max_attempts=%s retry_window=%ss",
            self.runtime.timezone,
            self.runtime.scheduler_poll_interval_seconds,
            self.runtime.worker_batch_size,
            self.runtime.publish_retry_max_attempts,
            self.runtime.publish_retry_window_seconds,
        )
        logger.info(
            "Connectivity settings: summary_interval=%ss mtproto_probe_interval=%ss mtproto_connect_timeout=%ss mtproto_probe_timeout=%ss mtproto_publish_timeout=%ss worker_cycle_timeout=%ss bot_polling_backoff=[min=%ss max=%ss factor=%s jitter=%s reset_after=%ss]",
            self.runtime.connectivity_summary_interval_seconds,
            self.runtime.mtproto_probe_interval_seconds,
            self.runtime.mtproto_connect_timeout_seconds,
            self.runtime.mtproto_probe_timeout_seconds,
            self.runtime.mtproto_publish_timeout_seconds,
            self.runtime.worker_cycle_timeout_seconds,
            self.runtime.bot_polling_backoff_min_delay_seconds,
            self.runtime.bot_polling_backoff_max_delay_seconds,
            self.runtime.bot_polling_backoff_factor,
            self.runtime.bot_polling_backoff_jitter,
            self.runtime.bot_polling_backoff_reset_after_seconds,
        )
        if self.bot.api_base_url:
            logger.info("Custom Telegram Bot API base URL configured: %s", self.bot.api_base_url)
        if self.bot.proxy_url:
            logger.info("Telegram Bot API proxy configured")
        if self.telegram.mtproto_proxy_host and self.telegram.mtproto_proxy_port:
            logger.info(
                "Telegram MTProto proxy configured host=%s port=%s",
                self.telegram.mtproto_proxy_host,
                self.telegram.mtproto_proxy_port,
            )
        logger.debug(
            "Resolved storage paths: database=%s photos=%s videos=%s prepared_photos=%s prepared_videos=%s sessions=%s bundle_root=%s",
            self.paths.database_path,
            self.paths.photos_dir,
            self.paths.videos_dir,
            self.paths.prepared_photos_dir,
            self.paths.prepared_videos_dir,
            self.paths.sessions_dir,
            self.paths.bundle_root,
        )
        logger.debug(
            "Configured MTProto fingerprint: device_model=%s system_version=%s app_version=%s lang_code=%s",
            self.telegram.device_model,
            self.telegram.system_version,
            self.telegram.app_version,
            self.telegram.lang_code,
        )


@lru_cache(maxsize=1)
def load_settings(project_root: Path | None = None) -> Settings:
    runtime_root = (project_root or _detect_runtime_root()).resolve()
    bundle_root = _detect_bundle_root(runtime_root)
    _load_env_file(runtime_root / ".env")

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = runtime_root / data_dir

    def resolve_path(env_name: str, default_relative: str) -> Path:
        raw_value = os.getenv(env_name, default_relative)
        path = Path(raw_value)
        return path if path.is_absolute() else runtime_root / path

    database_path = resolve_path("DB_PATH", os.getenv("DB_FILENAME", "tasks.db"))

    settings = Settings(
        paths=AppPaths(
            project_root=runtime_root,
            bundle_root=bundle_root,
            data_dir=data_dir,
            database_path=database_path,
            photos_dir=resolve_path("PHOTOS_DIR", "data/storage/photos"),
            videos_dir=resolve_path("VIDEOS_DIR", "data/storage/videos"),
            prepared_photos_dir=resolve_path("PREPARED_PHOTOS_DIR", "data/storage/prepared/photos"),
            prepared_videos_dir=resolve_path("PREPARED_VIDEOS_DIR", "data/storage/prepared/videos"),
            temp_dir=resolve_path("TEMP_DIR", "data/tmp"),
            logs_dir=resolve_path("LOGS_DIR", "data/logs"),
            sessions_dir=resolve_path("SESSIONS_DIR", "data/sessions"),
        ),
        bot=BotSettings(
            token=os.getenv("BOT_TOKEN") or None,
            allowed_user_ids=_get_env_csv_ints("BOT_ALLOWED_USER_IDS"),
            api_base_url=os.getenv("BOT_API_BASE_URL") or None,
            proxy_url=os.getenv("BOT_PROXY_URL") or None,
        ),
        telegram=TelegramClientSettings(
            api_id=_get_env_int("API_ID"),
            api_hash=os.getenv("API_HASH") or None,
            phone_number=os.getenv("PHONE_NUMBER") or None,
            session_name=os.getenv("SESSION_NAME", "account"),
            device_model=os.getenv("CLIENT_DEVICE_MODEL", "Desktop"),
            system_version=os.getenv("CLIENT_SYSTEM_VERSION", "Windows 10 x64"),
            app_version=os.getenv("CLIENT_APP_VERSION", "6.5.1 x64"),
            lang_code=os.getenv("CLIENT_LANG_CODE", "ru"),
            system_lang_code=os.getenv("CLIENT_SYSTEM_LANG_CODE", "ru"),
            mtproto_proxy_host=os.getenv("TELEGRAM_MTPROXY_HOST") or None,
            mtproto_proxy_port=_get_env_int("TELEGRAM_MTPROXY_PORT"),
            mtproto_proxy_secret=os.getenv("TELEGRAM_MTPROXY_SECRET") or None,
        ),
        media=MediaSettings(
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg"),
            ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe"),
            max_video_duration_seconds=_get_env_int("MAX_VIDEO_DURATION_SECONDS", 60) or 60,
            max_video_size_bytes=_get_env_int("MAX_VIDEO_SIZE_BYTES", 500 * 1024 * 1024) or 500 * 1024 * 1024,
        ),
        runtime=RuntimeSettings(
            timezone=os.getenv("APP_TIMEZONE", "Europe/Moscow"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            scheduler_poll_interval_seconds=_get_env_int("SCHEDULER_POLL_INTERVAL_SECONDS", 15) or 15,
            worker_batch_size=_get_env_int("WORKER_BATCH_SIZE", 10) or 10,
            job_lock_ttl_seconds=_get_env_int("JOB_LOCK_TTL_SECONDS", 900) or 900,
            publish_retry_max_attempts=_get_env_int("PUBLISH_RETRY_MAX_ATTEMPTS", 5) or 5,
            publish_retry_base_delay_seconds=_get_env_int("PUBLISH_RETRY_BASE_DELAY_SECONDS", 15) or 15,
            publish_retry_max_delay_seconds=_get_env_int("PUBLISH_RETRY_MAX_DELAY_SECONDS", 180) or 180,
            publish_retry_window_seconds=_get_env_int("PUBLISH_RETRY_WINDOW_SECONDS", 900) or 900,
            publish_retry_max_flood_wait_seconds=_get_env_int("PUBLISH_RETRY_MAX_FLOOD_WAIT_SECONDS", 300) or 300,
            one_time_min_lead_seconds=_get_env_int("ONE_TIME_MIN_LEAD_SECONDS", 120) or 120,
            connectivity_summary_interval_seconds=_get_env_int("CONNECTIVITY_SUMMARY_INTERVAL_SECONDS", 60) or 60,
            mtproto_probe_interval_seconds=_get_env_int("MTPROTO_PROBE_INTERVAL_SECONDS", 60) or 60,
            mtproto_connect_timeout_seconds=_get_env_float("MTPROTO_CONNECT_TIMEOUT_SECONDS", 45.0) or 45.0,
            mtproto_probe_timeout_seconds=_get_env_float("MTPROTO_PROBE_TIMEOUT_SECONDS", 10.0) or 10.0,
            mtproto_publish_timeout_seconds=_get_env_float("MTPROTO_PUBLISH_TIMEOUT_SECONDS", 3600.0) or 3600.0,
            worker_cycle_timeout_seconds=_get_env_float("WORKER_CYCLE_TIMEOUT_SECONDS", 7200.0) or 7200.0,
            bot_polling_backoff_min_delay_seconds=_get_env_float("BOT_POLLING_BACKOFF_MIN_DELAY_SECONDS", 1.0) or 1.0,
            bot_polling_backoff_max_delay_seconds=_get_env_float("BOT_POLLING_BACKOFF_MAX_DELAY_SECONDS", 60.0) or 60.0,
            bot_polling_backoff_factor=_get_env_float("BOT_POLLING_BACKOFF_FACTOR", 1.8) or 1.8,
            bot_polling_backoff_jitter=_get_env_float("BOT_POLLING_BACKOFF_JITTER", 0.2) or 0.2,
            bot_polling_backoff_reset_after_seconds=_get_env_int("BOT_POLLING_BACKOFF_RESET_AFTER_SECONDS", 300) or 300,
        ),
    )

    if not settings.bot.token:
        logger.warning("BOT_TOKEN is not configured; bot runtime will not start until it is set")
    if settings.telegram.api_id is None or not settings.telegram.api_hash:
        logger.warning("API_ID/API_HASH are not configured; MTProto runtime will not start until they are set")
    if not settings.telegram.phone_number:
        logger.warning("PHONE_NUMBER is not configured; auth CLI will require manual phone input")
    if bool(settings.telegram.mtproto_proxy_host) ^ bool(settings.telegram.mtproto_proxy_port):
        logger.warning("Incomplete MTProto proxy configuration; TELEGRAM_MTPROXY_HOST and TELEGRAM_MTPROXY_PORT must be set together")

    settings.ensure_runtime_paths()
    settings.log_summary()
    return settings


def _detect_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _detect_bundle_root(runtime_root: Path) -> Path:
    bundled_path = getattr(sys, "_MEIPASS", None)
    if bundled_path:
        return Path(bundled_path).resolve()
    return runtime_root
