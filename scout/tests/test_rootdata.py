import json
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from scout.config import Settings
from scout.sources.base import Context, SourceError
from scout.sources.rootdata import RootDataSource, project_url


class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Answers RootData endpoints from a dict: endpoint -> callable(body, headers) -> FakeResp."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def post(self, url, json=None, headers=None, **kw):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, json, headers))
        return self.routes[endpoint](json, headers)


def settings(tmp_path: Path, key=None) -> Settings:
    return Settings(
        telegram_bot_token=None, telegram_chat_id=None, timezone=ZoneInfo("Europe/Kyiv"),
        report_title="T", funding_stages=["pre-seed", "seed"], funding_max_age_days=14,
        history_path=tmp_path / "seen.json", rootdata_skill_key=key,
    )


ROUNDS = [
    {"name": "NoTokenSeed", "rounds": "Seed", "amount": 3000000, "published_time": "2026-09-30",
     "project_id": 1, "source_url": "https://news/1", "X": "https://x.com/notoken", "one_liner": "Payments rails",
     "invests": [{"name": "Hashed"}, {"name": "Polychain"}]},
    {"name": "HasToken", "rounds": "Pre-Seed", "amount": None, "published_time": "2026-09-29",
     "project_id": 2, "invests": []},
    {"name": "SeriesA", "rounds": "Series A", "amount": 10000000, "published_time": "2026-09-29",
     "project_id": 3, "invests": []},
    {"name": "CardMissing", "rounds": "Pre-Seed", "amount": 0, "published_time": 1759190400000,
     "project_id": 4, "invests": []},
]
PROJECTS = {
    1: {"project_id": 1, "token_symbol": "", "social_media": {"website": "https://notoken.xyz"},
        "tags": ["Infra", "Payment"], "rootdataurl": "https://www.rootdata.com/Projects/detail/NoTokenSeed?k=MQ=="},
    2: {"project_id": 2, "token_symbol": "HTK"},
    3: {"project_id": 3},  # token_symbol is sometimes missing entirely
}


def routes(rounds=ROUNDS, key="k1"):
    def init(body, headers):
        return FakeResp({"result": "200", "message": "Store this key", "data": {"api_key": key}})

    def get_fac(body, headers):
        page = body["page"]
        size = body["page_size"]
        chunk = rounds[(page - 1) * size: page * size]
        return FakeResp({"result": 200, "data": {"total": len(rounds), "items": chunk}})

    def get_item(body, headers):
        pid = body["project_id"]
        if pid not in PROJECTS:
            return FakeResp({"result": 404, "message": "not found"})
        return FakeResp({"result": 200, "data": PROJECTS[pid]})

    return {"init": init, "get_fac": get_fac, "get_item": get_item}


def test_fetch_keeps_seed_rounds_without_token(tmp_path):
    session = FakeSession(routes())
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=session))
    names = [i.project for i in items]
    assert names == ["NoTokenSeed", "CardMissing"]
    first = items[0]
    assert first.amount_usd == 3e6
    assert first.investors == ["Hashed", "Polychain"]
    assert first.announced == date(2026, 9, 30)
    assert first.website == "https://notoken.xyz"
    assert set(first.links) == {"RootData", "News", "X"}
    assert first.links["RootData"].endswith("k=MQ==")
    assert first.summary == "Payments rails" and first.tags == ["Infra", "Payment"]
    assert not first.flags
    assert items[1].amount_usd is None
    assert items[1].flags and "not verified" in items[1].flags[0]
    # Series A was skipped before any project lookup.
    assert ("get_item", {"project_id": 3, "include_investors": False}) not in [(c[0], c[1]) for c in session.calls]


def test_uses_existing_key_and_refreshes_on_401(tmp_path):
    state = {"n": 0}
    r = routes(key="fresh")
    orig = r["get_fac"]

    def get_fac(body, headers):
        if headers["Authorization"] == "Bearer stale":
            return FakeResp({"result": 401, "message": "invalid key"}, status=401)
        return orig(body, headers)

    r["get_fac"] = get_fac
    session = FakeSession(r)
    items = RootDataSource().fetch(Context(settings=settings(tmp_path, key="stale"), session=session))
    assert items
    assert [c[0] for c in session.calls][:3] == ["get_fac", "init", "get_fac"]


def test_paginates(tmp_path):
    many = [dict(ROUNDS[0], name=f"P{i}", project_id=1) for i in range(120)]
    session = FakeSession(routes(rounds=many))
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=session))
    assert len(items) == 120
    assert [c[0] for c in session.calls].count("get_fac") == 3
    assert [c[0] for c in session.calls].count("get_item") == 1  # cached per project


def test_non_json_answer_is_a_clear_error(tmp_path):
    r = routes()
    r["get_fac"] = lambda body, headers: FakeResp("<html>captcha</html>")
    with pytest.raises(SourceError, match="not JSON"):
        RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(r)))


def test_api_error_is_reported(tmp_path):
    r = routes()
    r["get_fac"] = lambda body, headers: FakeResp({"result": 110, "message": "no permission"})
    with pytest.raises(SourceError, match="110 no permission"):
        RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(r)))


def test_project_url():
    assert project_url("Ethena Labs", 10026) == "https://www.rootdata.com/Projects/detail/Ethena%20Labs?k=MTAwMjY%3D"


def test_401_without_json_body_refreshes_the_key(tmp_path):
    r = routes(key="fresh")
    orig = r["get_fac"]

    def get_fac(body, headers):
        if headers["Authorization"] == "Bearer stale":
            return FakeResp("<html>401</html>", status=401)
        return orig(body, headers)

    r["get_fac"] = get_fac
    session = FakeSession(r)
    assert RootDataSource().fetch(Context(settings=settings(tmp_path, key="stale"), session=session))
    assert "init" in [c[0] for c in session.calls]


@pytest.mark.parametrize("card_answer", [
    {"result": 200, "data": None}, {"result": 200, "data": {}}, {"result": 200},
])
def test_empty_project_card_is_not_treated_as_verified(tmp_path, card_answer):
    r = routes(rounds=[ROUNDS[0]])
    r["get_item"] = lambda body, headers: FakeResp(card_answer)
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(r)))
    assert len(items) == 1 and items[0].flags and "not verified" in items[0].flags[0]


def test_paginates_without_total(tmp_path):
    many = [dict(ROUNDS[0], name=f"P{i}", project_id=1) for i in range(120)]
    r = routes(rounds=many)
    orig = r["get_fac"]

    def get_fac(body, headers):
        resp = orig(body, headers)
        resp._payload["data"].pop("total")
        return resp

    r["get_fac"] = get_fac
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(r)))
    assert len(items) == 120


def test_round_without_project_id_is_flagged(tmp_path):
    raw = dict(ROUNDS[0])
    raw.pop("project_id")
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(routes(rounds=[raw]))))
    assert items[0].flags and "not verified" in items[0].flags[0]
    assert "RootData" not in items[0].links


def test_quiet_day_is_not_an_error_but_no_rounds_at_all_is(tmp_path):
    only_series_a = [ROUNDS[2]]
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(routes(rounds=only_series_a))))
    assert items == []
    with pytest.raises(SourceError, match="no funding rounds"):
        RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(routes(rounds=[]))))


def test_lookups_stop_after_repeated_failures(tmp_path):
    many = [dict(ROUNDS[0], name=f"P{i}", project_id=100 + i) for i in range(10)]
    r = routes(rounds=many)
    calls = {"n": 0}

    def get_item(body, headers):
        calls["n"] += 1
        return FakeResp({"result": 500, "message": "boom"})

    r["get_item"] = get_item
    items = RootDataSource().fetch(Context(settings=settings(tmp_path), session=FakeSession(r)))
    assert len(items) == 10 and all(i.flags for i in items)
    assert calls["n"] == 3


def test_shape_hides_keys():
    from scout.sources.rootdata import _shape
    shown = str(_shape({"api_key": "short", "data": {"token_symbol": "", "apikey": "x"}}))
    assert "short" not in shown and "<hidden>" in shown and "token_symbol" in shown
