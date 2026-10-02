"""One scouting run: collect from every source, keep what is new, send the report."""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from . import report
from .config import Settings
from .filters import is_relevant
from .history import History
from .http import make_session
from .models import Block, Item, SourceResult
from .report import Message
from .sources import all_sources
from .sources.base import Context, Source, SourceError
from .telegram import TelegramBot, TelegramError

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    messages: list[Message] = field(default_factory=list)
    results: list[SourceResult] = field(default_factory=list)
    sent: bool = False
    saved: bool = False


def collect(source: Source, ctx: Context) -> SourceResult:
    """Run one source; any failure is caught so the other sources keep working."""
    result = SourceResult(source=source.name, block=source.block)
    try:
        items = source.fetch(ctx)
        if not items and source.expect_items:
            raise SourceError("returned no data (the page layout may have changed)")
        for item in items:
            if source.name not in item.sources:
                item.sources.append(source.name)
        result.items = items
        log.info("%s: %d items", source.name, len(items))
    except SourceError as exc:
        result.error = str(exc)
        log.warning("%s failed: %s", source.name, exc)
    except Exception as exc:  # noqa: BLE001 - a broken source must never stop the run
        result.error = f"unexpected error ({type(exc).__name__})"
        log.error("%s crashed:\n%s", source.name, traceback.format_exc())
    return result


def merge_duplicates(items: list[Item]) -> list[Item]:
    """Combine entries for the same project reported by several sources.

    Two items with the same name key are only merged if no source gives them
    different IDs; otherwise they are different projects that share a name.
    """
    merged: dict[str, Item] = {}
    for item in items:
        existing = merged.get(item.key())
        if existing is not None and (existing.conflicts_with(item.ids) or item.conflicts_with(existing.ids)):
            item.disambiguate()
            existing = merged.get(item.key())
        if existing is None:
            merged[item.key()] = item
        else:
            existing.merge(item)
    return list(merged.values())


def _sort_key(item: Item):
    announced = getattr(item, "announced", None)
    return (-(announced.toordinal()) if announced else 0, item.project.lower())


def run(settings: Settings, *, only: list[str] | None = None, dry_run: bool = False, preview: bool = False) -> RunOutcome:
    today = datetime.now(settings.timezone).date()
    ctx = Context(settings=settings, session=make_session())
    history = History(settings.history_path)
    outcome = RunOutcome()

    sources = [s for s in all_sources() if settings.source_enabled(s.name)]
    if only:
        wanted = {o.lower() for o in only}
        sources = [s for s in sources if s.name.lower() in wanted]
    if not sources:
        raise SystemExit("No sources enabled - check scout/config.toml or --only")

    outcome.results = [collect(s, ctx) for s in sources]
    active_blocks = sorted({s.block for s in sources}, key=list(Block).index)

    # Sources that ran fine for the first time only fill the memory (silent start).
    newly_initialized = [r.source for r in outcome.results if r.ok and not history.source_initialized(r.source)]
    first_run_ever = history.is_empty and not preview
    silent_by_source = {s: 0 for s in newly_initialized}
    silent_total = 0

    new_items: dict[Block, list[Item]] = {b: [] for b in active_blocks}
    to_remember: list[Item] = []  # stored without being reported
    for block in active_blocks:
        block_items = [
            item
            for r in outcome.results if r.ok and r.block == block
            for item in r.items
            if is_relevant(item, settings, today)
        ]
        for item in merge_duplicates(block_items):
            if preview:
                new_items[block].append(item)
                continue
            entry = history.entry(item)
            established = [s for s in item.sources if s not in newly_initialized]
            if entry is None:
                reportable = bool(established)
            else:
                # Already reported -> never again. Only remembered during another source's silent
                # start -> report it once an established source that had not listed it finds it.
                reportable = not entry.get("reported") and any(s not in entry.get("sources", []) for s in established)
            if reportable and not first_run_ever:
                new_items[block].append(item)
                continue
            to_remember.append(item)
            if entry is None:
                silent_total += 1
                for src in item.sources:
                    if src in silent_by_source:
                        silent_by_source[src] += 1
        new_items[block].sort(key=_sort_key)

    if first_run_ever:
        outcome.messages = report.build_started_message(
            title=settings.report_title, remembered=silent_total, by_source=silent_by_source, results=outcome.results,
        )
    else:
        outcome.messages = report.build_report(
            title=settings.report_title,
            today=today,
            new_items=new_items,
            active_blocks=active_blocks,
            results=outcome.results,
            newly_initialized={} if preview else silent_by_source,
            preview=preview,
        )

    if dry_run:
        for msg in outcome.messages:
            print("-" * 60)
            print(msg.text)
        print("-" * 60)
        log.info("Dry run: nothing sent, history not saved")
        return outcome

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see scout/README.md)")
    if not settings.telegram_chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is not set (see scout/README.md)")
    bot = TelegramBot(settings.telegram_bot_token)

    delivered: list[Item] = []
    failure: TelegramError | None = None
    n = 0
    for n, msg in enumerate(outcome.messages, 1):
        try:
            bot.send(settings.telegram_chat_id, msg.text)
        except TelegramError as exc:
            failure = exc
            log.error("Message %d/%d could not be sent: %s", n, len(outcome.messages), exc)
            break
        delivered.extend(msg.items)
    outcome.sent = failure is None
    log.info("Sent %d/%d message(s) to Telegram", n if failure is None else n - 1, len(outcome.messages))

    # Save whatever reached the user, so a later failure never causes repeats.
    if not preview and (failure is None or n > 1):
        for item in delivered:
            history.mark_seen(item, reported=True)
        for item in to_remember:
            history.mark_seen(item, reported=False)
        for src in newly_initialized:
            history.mark_source_initialized(src)
        history.save()
        outcome.saved = True
        log.info("History saved: %d entries", history.count())
    if failure is not None:
        raise failure
    return outcome
