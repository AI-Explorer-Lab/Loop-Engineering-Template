"""Load public defaults and environment-specific overrides."""

from __future__ import annotations

import os
from pathlib import Path

from dynaconf import Dynaconf

CONFIG_FILE = Path(__file__).with_name("app.yaml")


def load_environment(environment: str | None = None) -> Dynaconf:
    """Create settings for one environment.

    Secrets are expected through LOOP_* environment variables or an external
    secret manager. They must not be added to app.yaml.
    """

    selected = environment or os.getenv("LOOP_ENV", "development")
    return Dynaconf(
        env=selected,
        environments=True,
        envvar_prefix="LOOP",
        load_dotenv=True,
        settings_files=[str(CONFIG_FILE)],
    )


settings = load_environment()


def validate_settings(config: Dynaconf = settings) -> None:
    """Fail before serving traffic when required settings are missing."""

    required = {
        "app.name": config.get("app.name"),
        "environment.name": config.get("environment.name"),
        "db.url": config.get("db.url"),
    }
    missing = [name for name, value in required.items() if value in (None, "")]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Missing required configuration: {joined}")
