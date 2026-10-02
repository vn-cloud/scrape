"""Rules that decide which collected items are relevant at all (before the "is it new" check)."""

from __future__ import annotations

from datetime import date, timedelta

from .config import Settings
from .models import FundingRound, Item
from .normalize import stage_key


def is_relevant(item: Item, settings: Settings, today: date) -> bool:
    if isinstance(item, FundingRound):
        if stage_key(item.stage) not in settings.funding_stages:
            return False
        if item.announced and item.announced < today - timedelta(days=settings.funding_max_age_days):
            return False
    return True
