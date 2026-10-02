"""Turns the new items into Telegram messages (HTML formatting, English)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

from .models import Block, FundingRound, Item, SourceResult
from .normalize import format_amount, stage_key

# Telegram allows 4096 characters per message; keep a safety margin because
# the limit is counted after HTML parsing and emoji count double.
MAX_MESSAGE_LEN = 3800
MAX_INVESTORS_SHOWN = 6
MAX_TAGS_SHOWN = 3


@dataclass
class Message:
    text: str
    #: Items whose entry is inside this message: once it is delivered they count as reported.
    items: list[Item] = field(default_factory=list)


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


def _plain(text: str) -> str:
    """HTML -> escaped plain text, for the rare line that has to be cut."""
    return esc(html.unescape(re.sub(r"<[^>]+>", "", text)))


def _cut_line(line: str) -> list[str]:
    """Cut one over-long line into pieces, never inside an HTML tag or an entity like &amp;."""
    line = _plain(line)
    pieces = []
    while len(line) > MAX_MESSAGE_LEN:
        cut = line.rfind(" ", 0, MAX_MESSAGE_LEN)
        if cut <= 0:
            cut = MAX_MESSAGE_LEN
        amp = line.rfind("&", max(0, cut - 8), cut)
        if amp != -1 and ";" not in line[amp:cut]:
            cut = amp
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    pieces.append(line)
    return pieces


def _fit(text: str) -> list[str]:
    """Split an over-long part at line breaks so that no chunk exceeds the Telegram limit."""
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
    chunks: list[str] = []
    current = ""
    for raw_line in text.split("\n"):
        for line in ([raw_line] if len(raw_line) <= MAX_MESSAGE_LEN else _cut_line(raw_line)):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) > MAX_MESSAGE_LEN:
                chunks.append(current)
                current = line
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def _pack(parts: list[tuple[str, Item | None]]) -> list[Message]:
    """Join message parts into as few Telegram messages as possible, never splitting a part."""
    messages: list[Message] = []
    current = Message("")
    for text, item in parts:
        for chunk in _fit(text):
            candidate = f"{current.text}\n\n{chunk}" if current.text else chunk
            if len(candidate) > MAX_MESSAGE_LEN and current.text:
                messages.append(current)
                current = Message(chunk)
            else:
                current.text = candidate
        if item is not None:
            current.items.append(item)
    if current.text:
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
) -> list[Message]:
    header = f"🔭 <b>{esc(title)}</b> — {today.day} {today.strftime('%b %Y')}"
    if preview:
        header += "\n🧪 <i>Preview: history ignored, showing recent entries</i>"
    else:
        header += "\n<i>Only what is new since the previous report</i>"
    parts: list[tuple[str, Item | None]] = [(header, None)]

    for block in Block:
        if block not in active_blocks or block not in SECTIONS:
            continue
        section_title, empty_text, render = SECTIONS[block]
        items = new_items.get(block, [])
        block_results = [r for r in results if r.block == block]
        section = f"{section_title} ({len(items)})"
        if not items:
            if block_results and not any(r.ok for r in block_results):
                section += "\n<i>No data today: the sources did not respond (see below).</i>"
            else:
                section += f"\n<i>{empty_text}</i>"
        parts.append((section, None))
        for n, item in enumerate(items, 1):
            parts.append((render(n, item), item))

    for r in results:
        if not r.ok:
            parts.append((f"⚠️ Source <b>{esc(r.source)}</b> did not respond: {esc(r.error)}", None))
    for source, count in newly_initialized.items():
        parts.append((
            f"ℹ️ New source <b>{esc(source)}</b> connected: remembered {count} existing entries, "
            "new ones will be reported from the next run.",
            None,
        ))
    ok_sources = [r.source for r in results if r.ok]
    if ok_sources:
        parts.append(("<i>Checked: " + ", ".join(esc(s) for s in ok_sources) + "</i>", None))
    return _pack(parts)


def build_started_message(
    *, title: str, remembered: int, by_source: dict[str, int], results: list[SourceResult]
) -> list[Message]:
    lines = [f"🔭 <b>{esc(title)}</b> is running."]
    if by_source:
        details = ", ".join(f"{esc(s)}: {n}" for s, n in by_source.items())
        lines.append(f"First run: remembered {remembered} existing entries ({details}).")
        lines.append("From the next run you will only get what is new.")
    else:
        lines.append("First run: no source responded, nothing remembered yet. The next run will try again.")
    parts: list[tuple[str, Item | None]] = [("\n".join(lines), None)]
    for r in results:
        if not r.ok:
            parts.append((f"⚠️ Source <b>{esc(r.source)}</b> did not respond: {esc(r.error)}", None))
    return _pack(parts)
