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


class RootDataSource(Source):
    name = "RootData"
    block = Block.FUNDING

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
                raise SourceError(f"{endpoint}: HTTP {resp.status_code}, not JSON (blocked or captcha?)") from None
            code = str(payload.get("result", payload.get("code", "")))  # sometimes a string: "200"
            if auth and attempt == 1 and (resp.status_code == 401 or code == "401"):
                self._key = self._new_key()
                continue
            if resp.status_code >= 400 or code != "200":
                raise SourceError(f"{endpoint}: API error {code or resp.status_code} {payload.get('message', '')}".strip())
            return payload.get("data")
        raise SourceError(f"{endpoint}: API key rejected")

    def _new_key(self) -> str:
        data = self._post("init", {}, auth=False)
        key = (data or {}).get("api_key")
        if not key:
            raise SourceError("could not obtain an API key from /skill/init")
        return key

    # --- data ---------------------------------------------------------------

    def _funding_pages(self, start: str):
        for page in range(1, MAX_PAGES + 1):
            data = self._post("get_fac", {"page": page, "page_size": PAGE_SIZE, "start_time": start}) or {}
            items = data.get("items") or []
            if page == 1:
                log.info("RootData get_fac: total=%s since %s", data.get("total"), start)
                if items:
                    log.debug("RootData sample round: %s", items[0])
            yield from items
            total = int(data.get("total") or 0)
            if not items or page * PAGE_SIZE >= total:
                return
        log.warning("RootData: stopped after %d pages", MAX_PAGES)

    def _project(self, project_id) -> dict:
        data = self._post("get_item", {"project_id": project_id, "include_investors": False}) or {}
        return data if isinstance(data, dict) else {}

    def fetch(self, ctx: Context) -> list[FundingRound]:
        self._session = ctx.session
        self._key = ctx.settings.rootdata_skill_key or self._new_key()
        today = datetime.now(ctx.settings.timezone).date()
        start = (today - timedelta(days=ctx.settings.funding_max_age_days)).isoformat()

        rounds: list[FundingRound] = []
        token_cache: dict = {}
        logged_project = False
        for raw in self._funding_pages(start):
            if stage_key(raw.get("rounds", "")) not in ctx.settings.funding_stages:
                continue
            project_id = raw.get("project_id")
            name = (raw.get("name") or "").strip()
            if not name:
                continue

            website = None
            if project_id is not None:
                if project_id not in token_cache:
                    try:
                        token_cache[project_id] = self._project(project_id)
                    except SourceError as exc:
                        log.warning("RootData get_item %s (%s) failed: %s", project_id, name, exc)
                        token_cache[project_id] = None
                    if not logged_project and token_cache[project_id]:
                        log.debug("RootData sample project: %s", token_cache[project_id])
                        logged_project = True
                project = token_cache[project_id]
                if project and (project.get("token_symbol") or "").strip():
                    continue  # the project already has a token
                website = _website(project)

            item = FundingRound(
                project=name,
                stage=raw.get("rounds", ""),
                amount_usd=parse_amount(raw.get("amount")),
                investors=[i.get("name", "").strip() for i in raw.get("invests") or [] if i.get("name")],
                announced=parse_date(raw.get("published_time")),
                website=website,
                links={"RootData": project_url(name, project_id)} if project_id is not None else {},
            )
            if project_id is not None and token_cache.get(project_id) is None:
                item.flags.append("token status not verified (RootData project card unavailable)")
            if raw.get("source_url"):
                item.links["News"] = raw["source_url"]
            if raw.get("X"):
                item.links["X"] = raw["X"]
            rounds.append(item)
        return rounds


def project_url(name: str, project_id) -> str:
    """RootData project pages look like /Projects/detail/<Name>?k=<base64 of the numeric id>."""
    k = base64.b64encode(str(project_id).encode()).decode()
    return f"https://www.rootdata.com/Projects/detail/{quote(name)}?k={quote(k)}"


def _website(project: dict | None) -> str | None:
    if not project:
        return None
    social = project.get("social_media") or {}
    if isinstance(social, dict):
        site = social.get("website") or social.get("Website")
        if isinstance(site, str) and site.startswith("http"):
            return site
    return None
