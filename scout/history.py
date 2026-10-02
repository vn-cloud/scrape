"""Memory of everything that has already been reported, stored as a JSON file.

The file is committed back to the repository after every successful daily run,
so the next run knows what is not new any more.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .models import Block, Item

FORMAT_VERSION = 1


class History:
    def __init__(self, path: Path):
        self.path = path
        self.data = {"version": FORMAT_VERSION, "initialized_sources": [], "seen": {}}
        if path.exists():
            with open(path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        for block in Block:
            self.data["seen"].setdefault(block.value, {})

    # --- sources -----------------------------------------------------------
    # A source that runs successfully for the first time only *remembers* what
    # it sees, without reporting it. Otherwise adding a new source would flood
    # the next report with everything that source has ever listed.

    def source_initialized(self, source: str) -> bool:
        return source in self.data["initialized_sources"]

    def mark_source_initialized(self, source: str) -> None:
        if source not in self.data["initialized_sources"]:
            self.data["initialized_sources"].append(source)
            self.data["initialized_sources"].sort()

    @property
    def is_empty(self) -> bool:
        return not self.data["initialized_sources"]

    # --- items ---------------------------------------------------------------

    def is_seen(self, item: Item) -> bool:
        return item.key() in self.data["seen"][item.block.value]

    def mark_seen(self, item: Item, reported: bool) -> None:
        bucket = self.data["seen"][item.block.value]
        entry = bucket.get(item.key())
        if entry is None:
            bucket[item.key()] = {
                "project": item.project,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                "sources": sorted(item.sources),
                "reported": reported,
            }
        else:
            entry["sources"] = sorted(set(entry["sources"]) | set(item.sources))

    def count(self, block: Block | None = None) -> int:
        if block:
            return len(self.data["seen"][block.value])
        return sum(len(v) for v in self.data["seen"].values())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file first so a crash never leaves a half-written history.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)
