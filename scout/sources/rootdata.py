"""RootData funding rounds, via RootData's free "skill" API (built for AI agents).

The website itself (rootdata.com/Fundraising) shows an interactive captcha to
cloud servers, so we use the official API instead:

  * POST /open/skill/init      -> anonymous, free API key (no registration)
  * POST /open/skill/get_fac   -> funding rounds of the past 365 days (top 3 investors per round)
  * POST /open/skill/get_item  -> project card; an empty token_symbol means "no token"

Rate limit is 200 requests/minute per key, far above what a daily run needs.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta
from urllib.parse import quote

import requests

from ..models import Block, FundingRound
from ..normalize import parse_amount, parse_date, stage_key
from .base import Context, Source, SourceError

log = logging.getLogger(__name__)

BASE_URL = "https://api.rootdata.com/open/skill"
PAGE_SIZE = 50
MAX_PAGES = 20
# Project-card lookups stop after this many seconds or this many failures in a row;
# the remaining rounds are still reported, flagged as "token status not verified".
LOOKUP_BUDGET_SECONDS = 180
MAX_LOOKUP_FAILURES_IN_A_ROW = 3
UNVERIFIED = "token status not verified (RootData project card unavailable)"


class RootDataSource(Source):
    name = "RootData"
    block = Block.FUNDING
    # Zero rounds after our own stage/token filtering is a normal quiet day;
    # fetch() raises by itself if the API returns no rounds at all.
    expect_items = False

    def __init__(self):
        self._key: str | None = None
        self._session: requests.Session | None = None

    # --- API plumbing -------------------------------------------------------

    def _post(self, endpoint: str, body: dict, *, auth: bool = True):
        """Call one endpoint; transparently gets a fresh key once if the current one is rejected."""
        for attempt in (1, 2):
            headers = {"Content-Type": "application/json", "language": "en"}
            if auth:
                headers["Authorization"] = f"Bearer {self._key}"
            try:
                # The shared session already retries 429/5xx with back-off.
                resp = self._session.post(f"{BASE_URL}/{endpoint}", json=body, headers=headers)
            except requests.exceptions.RetryError:
                raise SourceError(f"{endpoint}: rate limited or server error after retries") from None
            except requests.RequestException as exc:
                raise SourceError(f"{endpoint}: network error ({type(exc).__name__})") from None
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            code = str(payload.get("result", payload.get("code", ""))) if isinstance(payload, dict) else ""
            if auth and attempt == 1 and (resp.status_code == 401 or code == "401"):
                self._key = self._new_key()
                continue
            if payload is None:
                raise SourceError(f"{endpoint}: HTTP {resp.status_code}, not JSON (blocked or captcha?)")
            log.debug("RootData %s -> HTTP %s %s", endpoint, resp.status_code, _shape(payload))
            if not isinstance(payload, dict):
                raise SourceError(f"{endpoint}: unexpected answer format")
            # The code may be a string ("200") or missing (the init answer has none).
            if resp.status_code >= 400 or code not in ("200", "0", ""):
                raise SourceError(f"{endpoint}: API error {code or resp.status_code} {payload.get('message', '')}".strip())
            return payload.get("data", payload)
        raise SourceError(f"{endpoint}: API key rejected")

    def _new_key(self) -> str:
        data = self._post("init", {}, auth=False)
        key = _find(data, "api_key")
        if not key:
            raise SourceError("could not obtain an API key from /skill/init")
        return key

    # --- data ---------------------------------------------------------------

    def _funding_pages(self, start: str):
        collected = 0
        for page in range(1, MAX_PAGES + 1):
            data = self._post("get_fac", {"page": page, "page_size": PAGE_SIZE, "start_time": start})
            if not isinstance(data, dict):
                data = {}
            items = data.get("items") or []
            try:
                total = int(data.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            if page == 1:
                log.info("RootData get_fac: total=%s since %s", total or "?", start)
            if not items:
                return
            yield from items
            collected += len(items)
            if total and collected >= total:
                return
        log.warning("RootData: stopped after %d pages", MAX_PAGES)

    def _project(self, project_id) -> dict | None:
        """The project card, or None if RootData did not return a usable one."""
        data = self._post("get_item", {"project_id": project_id, "include_investors": False})
        if isinstance(data, dict) and (data.get("project_id") is not None or data.get("project_name")):
            return data
        return None

    def fetch(self, ctx: Context) -> list[FundingRound]:
        self._session = ctx.session
        self._key = ctx.settings.rootdata_skill_key or self._new_key()
        today = datetime.now(ctx.settings.timezone).date()
        start = (today - timedelta(days=ctx.settings.funding_max_age_days)).isoformat()

        rounds: list[FundingRound] = []
        cards: dict = {}  # project_id -> card, or None if unavailable
        lookups_open = True
        failures_in_a_row = 0
        deadline = time.monotonic() + LOOKUP_BUDGET_SECONDS
        seen = wrong_stage = has_token = 0

        for raw in self._funding_pages(start):
            seen += 1
            if stage_key(raw.get("rounds", "")) not in ctx.settings.funding_stages:
                wrong_stage += 1
                continue
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            project_id = raw.get("project_id")

            if project_id is not None and project_id not in cards and lookups_open:
                try:
                    cards[project_id] = self._project(project_id)
                    failures_in_a_row = 0
                except SourceError as exc:
                    log.warning("RootData get_item %s (%s) failed: %s", project_id, name, exc)
                    cards[project_id] = None
                    failures_in_a_row += 1
                if failures_in_a_row >= MAX_LOOKUP_FAILURES_IN_A_ROW or time.monotonic() > deadline:
                    log.warning("RootData: stopping project lookups (repeated failures or time budget)")
                    lookups_open = False
            card = cards.get(project_id)

            if card and (card.get("token_symbol") or "").strip():
                has_token += 1
                log.info("RootData: skip %s (%s) - has token %s", name, raw.get("rounds"), card["token_symbol"])
                continue

            item = FundingRound(
                project=name,
                stage=raw.get("rounds", ""),
                amount_usd=parse_amount(raw.get("amount")),
                investors=[i.get("name", "").strip() for i in raw.get("invests") or [] if i.get("name")],
                announced=parse_date(raw.get("published_time")),
                website=_website(card),
                summary=(raw.get("one_liner") or "").strip() or None,
                tags=[t for t in (card or {}).get("tags") or [] if isinstance(t, str)],
            )
            if project_id is not None:
                item.ids["RootData"] = str(project_id)
                item.links["RootData"] = (card or {}).get("rootdataurl") or project_url(name, project_id)
            if card is None:
                item.flags.append(UNVERIFIED)
            if raw.get("source_url"):
                item.links["News"] = raw["source_url"]
            if raw.get("X"):
                item.links["X"] = raw["X"]
            rounds.append(item)

        if seen == 0:
            raise SourceError(f"no funding rounds at all since {start} (API changed?)")
        log.info(
            "RootData: %d rounds since %s; %d other stages, %d with a token, %d kept",
            seen, start, wrong_stage, has_token, len(rounds),
        )
        return rounds


def project_url(name: str, project_id) -> str:
    """RootData project pages look like /Projects/detail/<Name>?k=<base64 of the numeric id>."""
    k = base64.b64encode(str(project_id).encode()).decode()
    return f"https://www.rootdata.com/Projects/detail/{quote(name)}?k={quote(k)}"


def _website(card: dict | None) -> str | None:
    if not card:
        return None
    social = card.get("social_media") or {}
    if isinstance(social, dict):
        site = social.get("website") or social.get("Website")
        if isinstance(site, str) and site.startswith("http"):
            return site
    return None


def _find(obj, field: str):
    """Depth-first search for a key anywhere in a JSON answer."""
    if isinstance(obj, dict):
        if obj.get(field):
            return obj[field]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find(child, field)
        if found:
            return found
    return None


_SECRET_HINTS = ("key", "token", "secret", "auth", "password")


def _shape(obj, depth: int = 0):
    """Structure of a JSON answer with values hidden, for debugging without leaking keys."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {
            k: "<hidden>" if k != "token_symbol" and any(h in str(k).lower() for h in _SECRET_HINTS)
            else _shape(v, depth + 1)
            for k, v in list(obj.items())[:25]
        }
    if isinstance(obj, list):
        return [_shape(obj[0], depth + 1), f"x{len(obj)}"] if obj else []
    if isinstance(obj, str):
        return f"str({len(obj)})" if len(obj) > 24 else repr(obj)
    return obj
