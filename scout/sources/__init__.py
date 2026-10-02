"""Registry of all data sources. To add a source: create a module here and list it below."""

from __future__ import annotations

from .base import Source
from .rootdata import RootDataSource


def all_sources() -> list[Source]:
    return [
        RootDataSource(),
    ]
