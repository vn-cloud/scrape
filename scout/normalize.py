"""Helpers that turn messy values from different sites into comparable ones."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone

# Trailing words that sites add or drop inconsistently ("Foo Labs" vs "Foo").
_NAME_SUFFIXES = {
    "labs", "lab", "protocol", "network", "foundation", "finance",
    "official", "inc", "ltd", "llc", "technologies", "technology",
}


def project_key(name: str) -> str:
    """Canonical form of a project name used to match it across sources."""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"\(.*?\)", " ", text.lower())  # "Foo (prev. Bar)" -> "foo"
    words = re.findall(r"[a-z0-9]+", text)
    while len(words) > 1 and words[-1] in _NAME_SUFFIXES:
        words.pop()
    return "".join(words) or name.strip().lower()


def stage_key(stage: str) -> str:
    """Canonical round name: "Pre-Seed", "pre seed", "PreSeed" -> "pre-seed"."""
    s = re.sub(r"[^a-z0-9]", "", (stage or "").lower())
    if not s:
        return "unknown"
    if s.startswith("preseed"):
        return "pre-seed"
    if s.startswith("seed"):  # "Seed", "Seed Round", "Seed Extension", "Seed+"
        return "seed"
    if s.startswith("preseriesa"):
        return "pre-series-a"
    if s.startswith("series") and len(s) > 6:
        return f"series-{s[6]}"
    return s


_AMOUNT_RE = re.compile(r"(\d[\d.,]*)\s*([kmb]|thousand|million|billion)?", re.I)
_MULTIPLIERS = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def parse_amount(value) -> float | None:
    """'$4.5M' -> 4500000.0; 'Undisclosed' / '--' / '' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    match = _AMOUNT_RE.search(str(value).replace(" ", ""))
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = _MULTIPLIERS.get((match.group(2) or "").lower(), 1)
    amount = number * mult
    return amount if amount > 0 else None


def format_amount(amount: float | None) -> str:
    if not amount:
        return "undisclosed"
    if amount >= 1e9:
        return f"${amount / 1e9:.1f}B".replace(".0B", "B")
    if amount >= 1e6:
        return f"${amount / 1e6:.1f}M".replace(".0M", "M")
    if amount >= 1e3:
        return f"${amount / 1e3:.0f}K"
    return f"${amount:.0f}"


def parse_date(value) -> date | None:
    """Accepts unix seconds/milliseconds, ISO strings and a few common text formats."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) == 8 and value.isdigit() and value.startswith(("19", "20")):
        try:  # compact "20261001"
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            pass
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        ts = float(value)
        if ts > 1e11:  # milliseconds
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y/%m/%d", "%b %d, %Y", "%d %b %Y", "%B %d, %Y", "%m/%d/%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None
