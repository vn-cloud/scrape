"""Every data source is a small class with a name, a report block and a fetch() method."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from ..config import Settings
from ..models import Block, Item


@dataclass
class Context:
    settings: Settings
    session: requests.Session


class SourceError(RuntimeError):
    """Raise this with a short human-readable reason; it is shown in the report."""


class Source(ABC):
    #: Name shown in the report and stored in the history ("RootData").
    name: str
    #: Which report section the items belong to.
    block: Block
    #: If True, an empty result is treated as a failure: the listing pages we
    #: scrape are never empty, so zero items means the site changed or blocked us.
    expect_items: bool = True

    @abstractmethod
    def fetch(self, ctx: Context) -> list[Item]:
        """Return everything currently listed by the source (recent entries only).

        Filtering for "new" happens later against the history, so a source does
        not need to know what was reported before.
        """
