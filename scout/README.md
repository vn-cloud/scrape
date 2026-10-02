# ChainGPT Pad Scout

A daily Telegram digest for launchpad scouting. Every morning it sends one report with three sections:

1. **Funding rounds** (pre-seed / seed) of projects that have **no token** yet.
2. **New airdrop campaigns** *(step 3)*.
3. **New sales / IDOs on competitor launchpads** *(steps 4–5)*.

Only things that were never reported before are shown. When the same project shows up in several sources, it becomes one entry with several links. If a source is down or blocks the request, the rest still run and the report says `⚠️ Source X did not respond`.

## Status

| Step | What | State |
|---|---|---|
| 1 | RootData → Telegram | built, waiting for Telegram secrets to test delivery |
| 2 | DefiLlama + CoinGecko token check | planned |
| 3 | Airdrop sites | planned |
| 4 | Competitor launchpad websites | planned |
| 5 | Competitor Telegram chats + Claude | planned |
| 6 | Daily schedule on GitHub Actions | planned |

## How it works

```
scout/
  sources/        one module per data source (rootdata.py, ...)
  runner.py       runs every source in isolation, filters, merges duplicates, compares with history
  history.py      memory of what was already reported  ->  data/seen.json
  report.py       builds the Telegram message (HTML)
  telegram.py     sends it
  config.toml     non-secret settings (round types, time zone, sources on/off)
  tools/probe.py  diagnostic tool that shows how a website loads its data
```

**Silent start.** The first time a source runs successfully it only *remembers* what it currently lists,
without reporting it — otherwise adding a new source would flood the report with old entries.
The very first run sends a short "Scout is running, remembered N entries" message.

## Sources

### RootData (funding rounds)

The website `rootdata.com/Fundraising` shows an interactive captcha to cloud servers (Tencent Cloud WAF),
so it cannot be scraped from GitHub Actions. Instead the scout uses RootData's **free "skill" API**
(made for AI agents, no registration):

* `POST https://api.rootdata.com/open/skill/init` → anonymous API key (fetched automatically every run;
  optionally store one as `ROOTDATA_SKILL_KEY`).
* `POST /open/skill/get_fac` → funding rounds of the last 365 days; the scout asks for the last
  `max_age_days` (14) and keeps only `stages` from `config.toml` (pre-seed, seed).
* `POST /open/skill/get_item` → project card. A non-empty `token_symbol` means the project already has
  a token, so the round is dropped. This replaces the website's "Token Issuance = No Token" filter.

Limits of the free API: at most **3 investors per round**, no valuation, 200 requests/minute.

## Secrets

Secrets never go into the repository. Locally, create a file named `.env` in the repository root
(it is ignored by git); for the daily run, add the same names as **GitHub Secrets**
(repository → Settings → Secrets and variables → Actions → New repository secret).

| Name | Needed from | Where to get it |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | step 1 | Telegram → @BotFather → `/newbot` (or `/mybots` → your bot → API Token) |
| `TELEGRAM_CHAT_ID` | step 1 | Telegram → @userinfobot → it replies with your numeric `Id`. Send `/start` to your bot once, otherwise it cannot write to you |
| `ROOTDATA_SKILL_KEY` | optional | not needed: a free key is requested automatically on every run |
| `COINGECKO_API_KEY` | step 2 | coingecko.com/en/api/pricing → Demo (free) → create account → Developer Dashboard |
| `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` | step 5 | my.telegram.org → API development tools (instructions will be added in step 5) |
| `ANTHROPIC_API_KEY` | step 5 | platform.claude.com → API Keys |

Example `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

## Running it yourself

```
pip install -r scout/requirements.txt
python -m playwright install chromium

python -m scout --preview --dry-run   # print a sample report from recent data, send nothing, save nothing
python -m scout --dry-run             # what today's real report would be, send nothing, save nothing
python -m scout                       # real run: send to Telegram and update data/seen.json
python -m scout --only RootData       # run a single source
python -m pytest scout/tests          # tests
```

## Adding a source

1. Create `scout/sources/<name>.py` with a class that inherits `Source`, sets `name` and `block`,
   and implements `fetch(ctx)` returning a list of items (e.g. `FundingRound`).
   Raise `SourceError("short reason")` when the site blocks you or returns garbage — the reason is shown in the report.
2. Add it to the list in `scout/sources/__init__.py` and to `[sources]` in `scout/config.toml`.

That is all: merging, history, report and error handling are shared.
