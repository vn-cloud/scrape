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
from .sources import all_sources
from .sources.base import Context, Source, SourceError
from .telegram import TelegramBot

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    messages: list[str] = field(default_factory=list)
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
        result.error = f"unexpected error ({type(exc).__name__}: {str(exc)[:150]})"
        log.error("%s crashed:\n%s", source.name, traceback.format_exc())
    return result


def merge_duplicates(items: list[Item]) -> list[Item]:
    """Combine entries for the same project reported by several sources."""
    merged: dict[str, Item] = {}
    for item in items:
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
    session = make_session()
    ctx = Context(settings=settings, session=session)
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
    newly_initialized = {
        r.source: 0 for r in outcome.results if r.ok and not history.source_initialized(r.source)
    }
    first_run_ever = history.is_empty and not preview

    new_items: dict[Block, list[Item]] = {b: [] for b in active_blocks}
    to_remember: list[tuple[Item, bool]] = []
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
            if history.is_seen(item):
                to_remember.append((item, False))  # refresh the list of sources
                continue
            # Report only if at least one *established* source found it.
            reportable = any(src not in newly_initialized for src in item.sources)
            if reportable and not first_run_ever:
                new_items[block].append(item)
            else:
                for src in item.sources:
                    if src in newly_initialized:
                        newly_initialized[src] += 1
            to_remember.append((item, reportable and not first_run_ever))
        new_items[block].sort(key=_sort_key)

    if first_run_ever:
        outcome.messages = report.build_started_message(
            title=settings.report_title, remembered=newly_initialized, results=outcome.results,
        )
    else:
        outcome.messages = report.build_report(
            title=settings.report_title,
            today=today,
            new_items=new_items,
            active_blocks=active_blocks,
            results=outcome.results,
            newly_initialized={} if preview else newly_initialized,
            preview=preview,
        )

    if dry_run:
        for msg in outcome.messages:
            print("-" * 60)
            print(msg)
        print("-" * 60)
        log.info("Dry run: nothing sent, history not saved")
        return outcome

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see scout/README.md)")
    bot = TelegramBot(settings.telegram_bot_token)
    chat_id = settings.telegram_chat_id or bot.find_private_chat_id()
    if not chat_id:
        raise SystemExit("TELEGRAM_CHAT_ID is not set and nobody has sent /start to the bot yet")
    for msg in outcome.messages:
        bot.send(chat_id, msg)
    outcome.sent = True
    log.info("Sent %d message(s) to Telegram", len(outcome.messages))

    if preview:
        return outcome

    # Save only after a successful send, so nothing is lost if Telegram fails.
    for item, reported in to_remember:
        history.mark_seen(item, reported=reported)
    for src in newly_initialized:
        history.mark_source_initialized(src)
    history.save()
    outcome.saved = True
    log.info("History saved: %d entries", history.count())
    return outcome
