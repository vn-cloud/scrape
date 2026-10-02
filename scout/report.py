"""Turns the new items into Telegram messages (HTML formatting, English)."""

from __future__ import annotations

import html
from datetime import date
from typing import Callable

from .models import Block, FundingRound, Item, SourceResult
from .normalize import format_amount, stage_key

# Telegram allows 4096 characters per message; keep a safety margin because
# the limit is counted after HTML parsing and emoji count double.
MAX_MESSAGE_LEN = 3800
MAX_INVESTORS_SHOWN = 6
MAX_TAGS_SHOWN = 3


def esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def link(url: str, label: str) -> str:
    return f'<a href="{esc(url)}">{esc(label)}</a>'


def fmt_date(d: date | None) -> str:
    return f"{d.day} {d.strftime('%b')}" if d else "date n/a"


_STAGE_LABELS = {"pre-seed": "Pre-Seed", "seed": "Seed", "pre-series-a": "Pre-Series A"}


def _links_line(item: Item, website: str | None = None) -> str:
    parts = [link(url, src) for src, url in item.links.items()]
    if website:
        parts.append(link(website, "Website"))
    return "🔗 " + " · ".join(parts) if parts else ""


def render_funding(n: int, item: Item) -> str:
    assert isinstance(item, FundingRound)
    stage = _STAGE_LABELS.get(stage_key(item.stage), item.stage or "Round n/a")
    lines = [f"{n}. <b>{esc(item.project)}</b> — {esc(stage)} · {format_amount(item.amount_usd)} · {fmt_date(item.announced)}"]
    tags = [t for t in item.tags if t.casefold() != (item.summary or "").casefold()][:MAX_TAGS_SHOWN]
    about = " · ".join(x for x in (item.summary, ", ".join(tags)) if x)
    if about:
        lines.append(f"   <i>{esc(about)}</i>")
    if item.investors:
        shown = ", ".join(esc(i) for i in item.investors[:MAX_INVESTORS_SHOWN])
        extra = len(item.investors) - MAX_INVESTORS_SHOWN
        lines.append(f"   👥 {shown}" + (f" +{extra} more" if extra > 0 else ""))
    else:
        lines.append("   👥 investors not disclosed")
    links = _links_line(item, item.website)
    if links:
        lines.append(f"   {links}")
    for flag in item.flags:
        lines.append(f"   ⚠️ {esc(flag)}")
    return "\n".join(lines)


# Block -> (section title, text when nothing is new, item renderer)
SECTIONS: dict[Block, tuple[str, str, Callable[[int, Item], str]]] = {
    Block.FUNDING: ("💰 <b>Funding rounds — projects without a token</b>", "No new rounds.", render_funding),
}


def _pack(parts: list[str]) -> list[str]:
    """Join message parts into as few Telegram messages as possible, never splitting a part."""
    messages: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}\n\n{part}" if current else part
        if len(candidate) <= MAX_MESSAGE_LEN:
            current = candidate
            continue
        if current:
            messages.append(current)
        # A single part longer than the limit (should not happen) is hard-cut.
        current = part[:MAX_MESSAGE_LEN]
    if current:
        messages.append(current)
    return messages


def build_report(
    *,
    title: str,
    today: date,
    new_items: dict[Block, list[Item]],
    active_blocks: list[Block],
    results: list[SourceResult],
    newly_initialized: dict[str, int],
    preview: bool = False,
) -> list[str]:
    header = f"🔭 <b>{esc(title)}</b> — {today.day} {today.strftime('%b %Y')}"
    if preview:
        header += "\n🧪 <i>Preview: history ignored, showing recent entries</i>"
    else:
        header += "\n<i>Only what is new since the previous report</i>"
    parts = [header]

    for block in Block:
        if block not in active_blocks or block not in SECTIONS:
            continue
        section_title, empty_text, render = SECTIONS[block]
        items = new_items.get(block, [])
        parts.append(f"{section_title} ({len(items)})")
        if not items:
            parts[-1] += f"\n<i>{empty_text}</i>"
        for n, item in enumerate(items, 1):
            parts.append(render(n, item))

    footer = []
    failed = [r for r in results if not r.ok]
    for r in failed:
        footer.append(f"⚠️ Source <b>{esc(r.source)}</b> did not respond: {esc(r.error)}")
    for source, count in newly_initialized.items():
        footer.append(
            f"ℹ️ New source <b>{esc(source)}</b> connected: remembered {count} existing entries, "
            "new ones will be reported from the next run."
        )
    ok_sources = [r.source for r in results if r.ok]
    if ok_sources:
        footer.append("<i>Checked: " + ", ".join(esc(s) for s in ok_sources) + "</i>")
    if footer:
        parts.append("\n".join(footer))
    return _pack(parts)


def build_started_message(*, title: str, remembered: dict[str, int], results: list[SourceResult]) -> list[str]:
    lines = [f"🔭 <b>{esc(title)}</b> is running."]
    total = sum(remembered.values())
    by_source = ", ".join(f"{esc(s)} ({n})" for s, n in remembered.items())
    lines.append(f"First run: remembered {total} existing entries from {by_source}.")
    lines.append("From the next run you will only get what is new.")
    for r in results:
        if not r.ok:
            lines.append(f"⚠️ Source <b>{esc(r.source)}</b> did not respond: {esc(r.error)}")
    return _pack(["\n".join(lines)])
