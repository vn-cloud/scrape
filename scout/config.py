"""Loads settings: secrets from the environment (.env locally, GitHub Secrets in CI)
and everything else from config.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
DEFAULT_HISTORY_PATH = REPO_DIR / "data" / "seen.json"


@dataclass
class Settings:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    timezone: ZoneInfo
    report_title: str
    funding_stages: list[str]
    funding_max_age_days: int
    enabled_sources: dict[str, bool] = field(default_factory=dict)
    history_path: Path = DEFAULT_HISTORY_PATH
    rootdata_skill_key: str | None = None

    def source_enabled(self, name: str) -> bool:
        return self.enabled_sources.get(name.lower(), True)


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings(config_path: Path | None = None) -> Settings:
    load_dotenv(REPO_DIR / ".env")
    with open(config_path or PACKAGE_DIR / "config.toml", "rb") as fh:
        cfg = tomllib.load(fh)

    report = cfg.get("report", {})
    funding = cfg.get("funding", {})
    history_override = _env("SCOUT_HISTORY_PATH")

    return Settings(
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        timezone=ZoneInfo(report.get("timezone", "UTC")),
        report_title=report.get("title", "Scouting report"),
        funding_stages=[s.lower() for s in funding.get("stages", ["pre-seed", "seed"])],
        funding_max_age_days=int(funding.get("max_age_days", 14)),
        enabled_sources={k.lower(): bool(v) for k, v in cfg.get("sources", {}).items()},
        history_path=Path(history_override) if history_override else DEFAULT_HISTORY_PATH,
        rootdata_skill_key=_env("ROOTDATA_SKILL_KEY"),
    )
