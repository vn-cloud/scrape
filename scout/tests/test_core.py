from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scout import runner
from scout.config import Settings
from scout.history import History
from scout.models import Block, FundingRound
from scout.normalize import format_amount, parse_amount, parse_date, project_key, stage_key
from scout.report import MAX_MESSAGE_LEN, build_report
from scout.sources.base import Source, SourceError

TODAY = date.today()


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token=None,
        telegram_chat_id=None,
        timezone=ZoneInfo("Europe/Kyiv"),
        report_title="Test Scout",
        funding_stages=["pre-seed", "seed"],
        funding_max_age_days=14,
        history_path=tmp_path / "seen.json",
    )


def rnd(name, stage="Seed", amount=None, investors=(), days_ago=1, url=None):
    return FundingRound(
        project=name, stage=stage, amount_usd=amount, investors=list(investors),
        announced=TODAY - timedelta(days=days_ago), links={"Fake": url or f"https://x/{name}"},
    )


class FakeSource(Source):
    block = Block.FUNDING

    def __init__(self, name, items=None, error=None):
        self.name = name
        self._items = items or []
        self._error = error

    def fetch(self, ctx):
        if self._error:
            raise self._error
        return [FundingRound(**{**r.__dict__, "links": {self.name: f"https://{self.name}/{r.project}"}, "sources": []})
                for r in self._items]


@pytest.fixture
def run_with(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)

    def _run(sources, **kw):
        monkeypatch.setattr(runner, "all_sources", lambda: sources)
        return runner.run(settings, **kw)

    def _persist(sources):
        """Simulate a successful daily run (send + save) without Telegram."""
        monkeypatch.setattr(runner, "all_sources", lambda: sources)
        monkeypatch.setattr(runner, "TelegramBot", _FakeBot)
        settings.telegram_bot_token = "x"
        settings.telegram_chat_id = "1"
        return runner.run(settings)

    _run.persist = _persist
    _run.settings = settings
    return _run


class _FakeBot:
    sent: list = []

    def __init__(self, token):
        pass

    def send(self, chat_id, text):
        _FakeBot.sent.append(text)


# --- normalize ----------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Foo Labs", "foo"), ("FOO Protocol", "Foo"), ("Foo (prev. Bar)", "foo"), ("Foo-Bar", "foobar"),
])
def test_project_key_matches_variants(a, b):
    assert project_key(a) == project_key(b)


def test_project_key_keeps_single_suffix_word():
    assert project_key("Network") == "network"


@pytest.mark.parametrize("raw,expected", [
    ("Pre-Seed", "pre-seed"), ("pre seed", "pre-seed"), ("Seed Round", "seed"), ("Seed+", "seed"),
    ("Series A", "series-a"), ("Pre-Series A", "pre-series-a"), ("", "unknown"), ("Strategic", "strategic"),
])
def test_stage_key(raw, expected):
    assert stage_key(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("$4.5M", 4.5e6), ("4,500,000", 4.5e6), ("Undisclosed", None), ("--", None), (None, None),
    (0, None), (2_000_000, 2e6), ("$800K", 8e5), ("1.2 billion", 1.2e9),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


def test_format_amount():
    assert format_amount(4.5e6) == "$4.5M"
    assert format_amount(4e6) == "$4M"
    assert format_amount(None) == "undisclosed"


@pytest.mark.parametrize("raw,expected", [
    ("2026-10-01", date(2026, 10, 1)), ("2026-10-01T10:00:00Z", date(2026, 10, 1)),
    (1759276800, date(2025, 10, 1)), (1759276800000, date(2025, 10, 1)), ("Oct 01, 2026", date(2026, 10, 1)),
    ("", None), ("garbage", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


# --- merge --------------------------------------------------------------------

def test_merge_combines_sources_and_investors():
    a = FundingRound(project="Foo Labs", stage="Seed", amount_usd=None, investors=["Hashed"],
                     sources=["A"], links={"A": "https://a"})
    b = FundingRound(project="Foo", stage="seed", amount_usd=3e6, investors=["hashed", "Polychain"],
                     sources=["B"], links={"B": "https://b"}, announced=TODAY)
    merged = runner.merge_duplicates([a, b])
    assert len(merged) == 1
    m = merged[0]
    assert m.sources == ["A", "B"]
    assert m.investors == ["Hashed", "Polychain"]
    assert m.amount_usd == 3e6
    assert set(m.links) == {"A", "B"}


def test_different_stages_are_different_items():
    assert len(runner.merge_duplicates([rnd("Foo", "Pre-Seed"), rnd("Foo", "Seed")])) == 2


# --- runner -------------------------------------------------------------------

def test_first_run_is_silent_and_remembers(run_with):
    src = FakeSource("A", [rnd("Foo"), rnd("Bar")])
    out = run_with.persist([src])
    assert "remembered 2 existing entries" in out.messages[0]
    assert out.saved
    history = History(run_with.settings.history_path)
    assert history.source_initialized("A")
    assert history.count(Block.FUNDING) == 2


def test_second_run_reports_only_new(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Baz")])])
    text = "\n".join(out.messages)
    assert "Baz" in text and "Foo" not in text
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Baz")])])
    assert "(0)" in out.messages[0]


def test_new_source_is_silent_but_old_source_still_reports(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Shared")]),
                            FakeSource("B", [rnd("Shared"), rnd("OnlyB1"), rnd("OnlyB2")])])
    text = "\n".join(out.messages)
    assert "Shared" in text           # an established source found it
    assert "OnlyB1" not in text       # only the brand-new source found it
    assert "New source <b>B</b> connected: remembered 2" in text
    out = run_with.persist([FakeSource("B", [rnd("OnlyB1"), rnd("OnlyB3")])])
    text = "\n".join(out.messages)
    assert "OnlyB3" in text and "OnlyB1" not in text


def test_broken_source_does_not_stop_others(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")]), FakeSource("B", [rnd("Bar")])])
    out = run_with.persist([FakeSource("A", error=RuntimeError("boom")),
                            FakeSource("B", [rnd("Bar"), rnd("New")])])
    text = "\n".join(out.messages)
    assert "Source <b>A</b> did not respond" in text
    assert "New" in text


def test_empty_result_counts_as_failure(run_with):
    out = run_with([FakeSource("A", [])], dry_run=True)
    assert out.results[0].error and "no data" in out.results[0].error


def test_source_error_message_is_shown(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", error=SourceError("HTTP 403 (blocked)"))])
    assert "HTTP 403 (blocked)" in out.messages[0]


def test_filters_stage_and_age(run_with):
    run_with.persist([FakeSource("A", [rnd("Seed0")])])
    out = run_with.persist([FakeSource("A", [
        rnd("Seed0"), rnd("SeriesA", "Series A"), rnd("Old", days_ago=40), rnd("PreSeed", "Pre-Seed"),
    ])])
    text = "\n".join(out.messages)
    assert "PreSeed" in text and "SeriesA" not in text and "Old" not in text


def test_dry_run_saves_nothing(run_with):
    run_with([FakeSource("A", [rnd("Foo")])], dry_run=True)
    assert not run_with.settings.history_path.exists()


def test_preview_ignores_history_and_saves_nothing(run_with):
    out = run_with([FakeSource("A", [rnd("Foo")])], dry_run=True, preview=True)
    assert "Foo" in "\n".join(out.messages)
    assert not run_with.settings.history_path.exists()


# --- report -------------------------------------------------------------------

def test_report_escapes_html_and_splits_long_reports():
    items = [rnd(f"Proj <{i}> & Co", investors=[f"Fund {j}" for j in range(10)]) for i in range(80)]
    msgs = build_report(title="T", today=TODAY, new_items={Block.FUNDING: items},
                        active_blocks=[Block.FUNDING], results=[], newly_initialized={})
    assert len(msgs) > 1
    assert all(len(m) <= MAX_MESSAGE_LEN for m in msgs)
    joined = "\n".join(msgs)
    assert "&lt;0&gt; &amp; Co" in joined
    assert "+4 more" in joined
    assert joined.count("<b>Proj &lt;") == 80
