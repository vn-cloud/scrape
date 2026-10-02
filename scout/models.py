"""Data types shared by all sources, the history store and the report."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import ClassVar

from .normalize import project_key, stage_key


class Block(str, Enum):
    """The three sections of the daily report."""

    FUNDING = "funding"
    AIRDROPS = "airdrops"
    SALES = "sales"


@dataclass
class Item:
    """Base class for anything a source can find.

    `sources` lists every source that reported the item; `links` maps a source
    name to the page we found it on. `flags` holds human-readable warnings that
    are shown next to the item in the report (e.g. "possible token").
    """

    project: str
    sources: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)

    block: ClassVar[Block]

    def key(self) -> str:
        """Identity used for merging duplicates and for the "already seen" history."""
        return project_key(self.project)

    def merge(self, other: "Item") -> None:
        """Fold a duplicate of this item (same key) found by another source into it."""
        for src in other.sources:
            if src not in self.sources:
                self.sources.append(src)
        for src, url in other.links.items():
            self.links.setdefault(src, url)
        for flag in other.flags:
            if flag not in self.flags:
                self.flags.append(flag)


@dataclass
class FundingRound(Item):
    stage: str = ""
    amount_usd: float | None = None
    investors: list[str] = field(default_factory=list)
    announced: date | None = None
    website: str | None = None

    block: ClassVar[Block] = Block.FUNDING

    def key(self) -> str:
        # The same project can legitimately show up again with a later round
        # (pre-seed now, seed in six months), so the stage is part of the identity.
        return f"{project_key(self.project)}|{stage_key(self.stage)}"

    def merge(self, other: Item) -> None:
        super().merge(other)
        if not isinstance(other, FundingRound):
            return
        known = {i.casefold() for i in self.investors}
        for inv in other.investors:
            if inv.casefold() not in known:
                self.investors.append(inv)
                known.add(inv.casefold())
        if other.amount_usd and (not self.amount_usd or other.amount_usd > self.amount_usd):
            self.amount_usd = other.amount_usd
        if other.announced and (not self.announced or other.announced < self.announced):
            self.announced = other.announced
        self.website = self.website or other.website


@dataclass
class SourceResult:
    """What one source produced in one run."""

    source: str
    block: Block
    items: list[Item] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
