from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scout import runner
from scout.config import Settings
from scout.history import History
from scout.models import Block, FundingRound, SourceResult
from scout.normalize import format_amount, parse_amount, parse_date, project_key, stage_key
from scout.report import MAX_MESSAGE_LEN, build_report
from scout.sources.base import Source, SourceError
from scout.telegram import TelegramBot, TelegramError

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


def rnd(name, stage="Seed", amount=None, investors=(), days_ago=1, pid=None):
    return FundingRound(
        project=name, stage=stage, amount_usd=amount, investors=list(investors),
        announced=TODAY - timedelta(days=days_ago), ids={"_pid": str(pid)} if pid is not None else {},
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
        out = []
        for r in self._items:
            ids = {self.name: r.ids["_pid"]} if "_pid" in r.ids else {}
            out.append(FundingRound(**{**r.__dict__, "links": {self.name: f"https://{self.name}/{r.project}"},
                                       "sources": [], "ids": ids, "flags": []}))
        return out


class FakeBot:
    sent: list = []
    fail_on: set = set()  # 1-based message numbers that fail

    def __init__(self, token):
        pass

    def send(self, chat_id, text):
        FakeBot.sent.append(text)
        if len(FakeBot.sent) in FakeBot.fail_on:
            raise TelegramError("sendMessage: HTTP 502")


def text(outcome) -> str:
    return "\n".join(m.text for m in outcome.messages)


@pytest.fixture
def run_with(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    FakeBot.sent = []
    FakeBot.fail_on = set()

    def _run(sources, **kw):
        monkeypatch.setattr(runner, "all_sources", lambda: sources)
        return runner.run(settings, **kw)

    def _persist(sources):
        """Simulate a daily run that sends and saves (Telegram replaced by FakeBot)."""
        monkeypatch.setattr(runner, "all_sources", lambda: sources)
        monkeypatch.setattr(runner, "TelegramBot", FakeBot)
        settings.telegram_bot_token = "x"
        settings.telegram_chat_id = "1"
        return runner.run(settings)

    _run.persist = _persist
    _run.settings = settings
    return _run


# --- normalize ----------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Foo Labs", "foo"), ("FOO Protocol", "Foo"), ("Foo (prev. Bar)", "foo"), ("Foo-Bar", "foobar"),
])
def test_project_key_matches_variants(a, b):
    assert project_key(a) == project_key(b)


def test_project_key_keeps_single_suffix_word():
    assert project_key("Network") == "network"


def test_project_key_keeps_non_latin_names_apart():
    assert project_key("火币 Labs") == "火币"
    assert project_key("Ω Protocol") != project_key("Δ Protocol")
    assert project_key("Café Labs") == "cafe"


@pytest.mark.parametrize("raw,expected", [
    ("Pre-Seed", "pre-seed"), ("pre seed", "pre-seed"), ("Seed Round", "seed"), ("Seed+", "seed"),
    ("Series A", "series-a"), ("Pre-Series A", "pre-series-a"), ("", "unknown"), ("Strategic", "strategic"),
])
def test_stage_key(raw, expected):
    assert stage_key(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("$4.5M", 4.5e6), ("4,500,000", 4.5e6), ("Undisclosed", None), ("--", None), (None, None),
    (0, None), (2_000_000, 2e6), ("$800K", 8e5), ("1.2 billion", 1.2e9), ("approx. $5M", 5e6), ("Seed, $5M", 5e6),
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
    ("20261001", date(2026, 10, 1)), ("", None), ("garbage", None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


# --- merge --------------------------------------------------------------------

def test_merge_combines_sources_and_investors():
    a = FundingRound(project="Foo Labs", stage="Seed", amount_usd=None, investors=["Hashed"],
                     sources=["A"], links={"A": "https://a"}, ids={"A": "1"})
    b = FundingRound(project="Foo", stage="seed", amount_usd=3e6, investors=["hashed", "Polychain"],
                     sources=["B"], links={"B": "https://b"}, announced=TODAY, ids={"B": "9"})
    merged = runner.merge_duplicates([a, b])
    assert len(merged) == 1
    m = merged[0]
    assert m.sources == ["A", "B"]
    assert m.investors == ["Hashed", "Polychain"]
    assert m.amount_usd == 3e6
    assert set(m.links) == {"A", "B"}
    assert m.ids == {"A": "1", "B": "9"}


def test_different_stages_are_different_items():
    assert len(runner.merge_duplicates([rnd("Foo", "Pre-Seed"), rnd("Foo", "Seed")])) == 2


def test_same_name_key_but_different_ids_is_not_merged():
    a = FundingRound(project="Nexus Labs", stage="Seed", sources=["A"], ids={"A": "1"})
    b = FundingRound(project="Nexus Network", stage="Seed", sources=["A"], ids={"A": "2"})
    merged = runner.merge_duplicates([a, b])
    assert len(merged) == 2
    assert len({m.key() for m in merged}) == 2


# --- runner -------------------------------------------------------------------

def test_first_run_is_silent_and_remembers(run_with):
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Bar")]), FakeSource("B", [rnd("Foo")])])
    assert "remembered 2 existing entries (A: 2, B: 1)" in text(out)
    assert out.saved
    history = History(run_with.settings.history_path)
    assert history.source_initialized("A") and history.source_initialized("B")
    assert history.count(Block.FUNDING) == 2


def test_first_run_with_every_source_failing(run_with):
    out = run_with.persist([FakeSource("A", error=SourceError("HTTP 403"))])
    assert "no source responded" in text(out)
    assert History(run_with.settings.history_path).is_empty


def test_second_run_reports_only_new(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Baz")])])
    assert "Baz" in text(out) and "Foo" not in text(out)
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Baz")])])
    assert "(0)" in text(out) and "No new rounds." in text(out)


def test_new_source_is_silent_but_old_source_still_reports(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Shared")]),
                            FakeSource("B", [rnd("Shared"), rnd("OnlyB1"), rnd("OnlyB2")])])
    assert "Shared" in text(out)        # an established source found it
    assert "OnlyB1" not in text(out)    # only the brand-new source found it
    assert "New source <b>B</b> connected: remembered 2" in text(out)
    out = run_with.persist([FakeSource("B", [rnd("OnlyB1"), rnd("OnlyB3")])])
    assert "OnlyB3" in text(out) and "OnlyB1" not in text(out)


def test_round_swallowed_by_silent_start_is_reported_when_established_source_finds_it(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    # A is down the day B is connected; B's "Fresh" is only remembered.
    run_with.persist([FakeSource("A", error=SourceError("down")), FakeSource("B", [rnd("Fresh")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo"), rnd("Fresh")]), FakeSource("B", [rnd("Fresh")])])
    assert "Fresh" in text(out)
    out = run_with.persist([FakeSource("A", [rnd("Fresh")]), FakeSource("B", [rnd("Fresh")])])
    assert "Fresh" not in text(out)


def test_items_remembered_on_first_run_are_not_reported_later(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", [rnd("Foo")])])
    assert "Foo" not in text(out)


def test_same_name_different_project_is_reported(run_with):
    run_with.persist([FakeSource("A", [rnd("Seed0", pid=0)])])
    out = run_with.persist([FakeSource("A", [rnd("Nexus Labs", pid=1)])])
    assert "Nexus Labs" in text(out)
    out = run_with.persist([FakeSource("A", [rnd("Nexus Labs", pid=1), rnd("Nexus Network", pid=2)])])
    assert "Nexus Network" in text(out) and "Nexus Labs" not in text(out)
    out = run_with.persist([FakeSource("A", [rnd("Nexus Labs", pid=1), rnd("Nexus Network", pid=2)])])
    assert "Nexus" not in text(out)


def test_broken_source_does_not_stop_others(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")]), FakeSource("B", [rnd("Bar")])])
    out = run_with.persist([FakeSource("A", error=RuntimeError("boom")),
                            FakeSource("B", [rnd("Bar"), rnd("New")])])
    assert "Source <b>A</b> did not respond: unexpected error (RuntimeError)" in text(out)
    assert "New" in text(out)


def test_block_with_only_failed_sources_says_no_data(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", error=SourceError("HTTP 403"))])
    assert "No data today" in text(out) and "No new rounds." not in text(out)


def test_empty_result_counts_as_failure(run_with):
    out = run_with([FakeSource("A", [])], dry_run=True)
    assert out.results[0].error and "no data" in out.results[0].error


def test_source_error_message_is_shown(run_with):
    run_with.persist([FakeSource("A", [rnd("Foo")])])
    out = run_with.persist([FakeSource("A", error=SourceError("HTTP 403 (blocked)"))])
    assert "HTTP 403 (blocked)" in text(out)


def test_filters_stage_and_age(run_with):
    run_with.persist([FakeSource("A", [rnd("Seed0")])])
    out = run_with.persist([FakeSource("A", [
        rnd("Seed0"), rnd("SeriesA", "Series A"), rnd("Old", days_ago=40), rnd("PreSeed", "Pre-Seed"),
    ])])
    assert "PreSeed" in text(out) and "SeriesA" not in text(out) and "Old" not in text(out)


def test_dry_run_saves_nothing(run_with):
    run_with([FakeSource("A", [rnd("Foo")])], dry_run=True)
    assert not run_with.settings.history_path.exists()


def test_preview_ignores_history_and_saves_nothing(run_with):
    out = run_with([FakeSource("A", [rnd("Foo")])], dry_run=True, preview=True)
    assert "Foo" in text(out)
    assert not run_with.settings.history_path.exists()


def test_missing_chat_id_stops_before_sending(run_with, monkeypatch):
    monkeypatch.setattr(runner, "TelegramBot", FakeBot)
    run_with.settings.telegram_bot_token = "x"
    with pytest.raises(SystemExit, match="TELEGRAM_CHAT_ID"):
        run_with([FakeSource("A", [rnd("Foo")])])
    assert FakeBot.sent == []


def test_partial_send_failure_saves_only_delivered_items(run_with):
    run_with.persist([FakeSource("A", [rnd("Seed0")])])
    many = [rnd(f"Project{i:02d}", investors=[f"Fund {j}" for j in range(6)]) for i in range(30)]
    FakeBot.sent = []
    FakeBot.fail_on = {2}
    with pytest.raises(TelegramError):
        run_with.persist([FakeSource("A", many)])
    first_message = FakeBot.sent[0]
    delivered = [p.project for p in many if f"<b>{p.project}</b>" in first_message]
    assert 0 < len(delivered) < 30
    FakeBot.sent = []
    FakeBot.fail_on = set()
    out = run_with.persist([FakeSource("A", many)])
    again = text(out)
    assert all(f"<b>{d}</b>" not in again for d in delivered)
    assert all(f"<b>{p.project}</b>" in again for p in many if p.project not in delivered)


# --- report -------------------------------------------------------------------

def test_report_escapes_html_and_splits_long_reports():
    items = [rnd(f"Proj <{i}> & Co", investors=[f"Fund {j}" for j in range(10)]) for i in range(80)]
    msgs = build_report(title="T", today=TODAY, new_items={Block.FUNDING: items},
                        active_blocks=[Block.FUNDING], results=[], newly_initialized={})
    assert len(msgs) > 1
    assert all(len(m.text) <= MAX_MESSAGE_LEN for m in msgs)
    joined = "\n".join(m.text for m in msgs)
    assert "&lt;0&gt; &amp; Co" in joined
    assert "+4 more" in joined
    assert joined.count("<b>Proj &lt;") == 80
    assert sum(len(m.items) for m in msgs) == 80


def test_many_long_failures_never_break_html():
    results = [SourceResult(source=f"Src{i}", block=Block.FUNDING, error="x" * 400 + " & <tag>") for i in range(25)]
    results.append(SourceResult(source="Huge", block=Block.FUNDING, error="word " * 2000))
    msgs = build_report(title="T", today=TODAY, new_items={}, active_blocks=[Block.FUNDING],
                        results=results, newly_initialized={})
    for m in msgs:
        assert len(m.text) <= MAX_MESSAGE_LEN
        assert m.text.count("<b>") == m.text.count("</b>")
        assert m.text.count("<i>") == m.text.count("</i>")
        assert "<tag>" not in m.text


# --- telegram -----------------------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        return self.answers.pop(0)


def test_telegram_retries_server_errors(monkeypatch):
    monkeypatch.setattr("scout.telegram.time.sleep", lambda s: None)
    session = _Session([_Resp(502, {}), _Resp(200, {"ok": True, "result": {}})])
    TelegramBot("t", session).send("1", "hi")
    assert session.calls == 2


def test_telegram_does_not_retry_bad_requests(monkeypatch):
    monkeypatch.setattr("scout.telegram.time.sleep", lambda s: None)
    session = _Session([_Resp(400, {"ok": False, "description": "Bad Request: can't parse entities"})])
    with pytest.raises(TelegramError, match="can't parse entities"):
        TelegramBot("t", session).send("1", "hi")
    assert session.calls == 1
