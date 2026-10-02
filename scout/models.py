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
    #: The project's own ID inside each source (e.g. {"RootData": "26083"}), when the source has one.
    ids: dict[str, str] = field(default_factory=dict)
    #: Set when two different projects end up with the same name key (see conflicts_with).
    disambiguator: str = ""

    block: ClassVar[Block]

    def base_key(self) -> str:
        return project_key(self.project)

    def key(self) -> str:
        """Identity used for merging duplicates and for the "already seen" history."""
        base = self.base_key()
        return f"{base}#{self.disambiguator}" if self.disambiguator else base

    def conflicts_with(self, ids: dict[str, str]) -> bool:
        """True if a source gives this item and `ids` different IDs, i.e. they are different projects
        that merely share a name key ("Nexus Labs" vs "Nexus Network")."""
        return any(src in ids and str(ids[src]) != str(own) for src, own in self.ids.items())

    def disambiguate(self) -> None:
        src, own = sorted(self.ids.items())[0]
        self.disambiguator = f"{src}:{own}"

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
        for src, own in other.ids.items():
            self.ids.setdefault(src, own)


@dataclass
class FundingRound(Item):
    stage: str = ""
    amount_usd: float | None = None
    investors: list[str] = field(default_factory=list)
    announced: date | None = None
    website: str | None = None
    summary: str | None = None  # one-line description of the project
    tags: list[str] = field(default_factory=list)

    block: ClassVar[Block] = Block.FUNDING

    def base_key(self) -> str:
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
        self.summary = self.summary or other.summary
        for tag in other.tags:
            if tag not in self.tags:
                self.tags.append(tag)


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
