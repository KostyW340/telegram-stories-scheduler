from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config.settings import Settings, load_settings

logger = logging.getLogger(__name__)


def run_migrations(settings: Settings | None = None) -> None:
    current_settings = settings or load_settings()
    alembic_ini = current_settings.paths.bundle_root / "alembic.ini"
    logger.info("Running Alembic migrations against %s", current_settings.paths.database_path)
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(current_settings.paths.bundle_root / "migrations"))
    config.set_main_option("sqlalchemy.url", current_settings.alembic_database_url)
    command.upgrade(config, "head")


def ensure_runtime_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
