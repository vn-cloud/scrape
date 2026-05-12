#!/usr/bin/env python3
"""
Proof of Talk Speaker Scraper
-------------------------------
Scrapes https://proofoftalk.io/speakers/ for all speaker profiles,
then searches LinkedIn for each speaker and exports everything to Excel.

Run locally (not from a datacenter — the site blocks datacenter IPs):
    pip install playwright openpyxl
    playwright install chromium
    python scraper.py
"""

import re
import time
import json
import urllib.parse
from pathlib import Path

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

SPEAKERS_URL = "https://proofoftalk.io/speakers/"
OUTPUT_FILE = "proof_of_talk_speakers.xlsx"

# ─── Seed data collected from Google's index of the site ─────────────────────
# These are speakers whose individual profile pages appeared in search results.
# The scraper will top this list up with anything new it finds on the live page.
SEED_SPEAKERS = [
    {"slug": "jenny-johnson",       "name": "Jenny Johnson",          "title": "CEO",                                "company": "Franklin Templeton"},
    {"slug": "stani-kulechov-3f336","name": "Stani Kulechov",         "title": "Founder & CEO",                      "company": "Aave / Avara"},
    {"slug": "yat-siu-e2dd1",       "name": "Yat Siu",                "title": "Co-Founder & Executive Chairman",    "company": "Animoca Brands"},
    {"slug": "diogo-monica",        "name": "Diogo Monica",           "title": "General Partner",                    "company": "Haun Ventures"},
    {"slug": "mo-shaikh",           "name": "Mo Shaikh",              "title": "Founder",                            "company": "Aptos Labs"},
    {"slug": "jacob-steeves",       "name": "Jacob Steeves",          "title": "Co-Founder",                         "company": "Bittensor"},
    {"slug": "steven-haft",         "name": "Steven Haft",            "title": "Global Partnerships Lead / Climate Lead", "company": "Consensys"},
    {"slug": "michael-heinrich",    "name": "Michael Heinrich",       "title": "Co-Founder & CEO",                   "company": "0G Labs"},
    {"slug": "kavan-canekeratneusman","name": "Kavan Canekeratne",    "title": "Investor",                           "company": "CoinFund"},
    {"slug": "jiahao-sun",          "name": "Jiahao Sun",             "title": "CEO",                                "company": "FLock.io"},
    {"slug": "maxime-sebti",        "name": "Maxime Sebti",           "title": "CEO & Co-Founder",                   "company": "Score"},
    {"slug": "sebastian-borget",    "name": "Sebastien Borget",       "title": "Co-Founder",                         "company": "The Sandbox"},
    {"slug": "reeve-collins",       "name": "Reeve Collins",          "title": "Co-Founder & Chairman",              "company": "Pi Protocol & WeFi"},
    {"slug": "rachel-wolfson",      "name": "Rachel Wolfson",         "title": "Podcast Host / Reporter",            "company": "Web3 Deep Dive / Cointelegraph"},
    {"slug": "cecilia-hsueh",       "name": "Cecilia Hsueh",          "title": "Co-Founder & CEO",                   "company": "Morph"},
    {"slug": "sebastien-couture",   "name": "Sebastien Couture",      "title": "Founder",                            "company": "Interop Ventures"},
    {"slug": "arrash-yasavolian",   "name": "Arrash Yasavolian",      "title": "CEO",                                "company": "Taoshi"},
    {"slug": "sergej-kunz",         "name": "Sergej Kunz",            "title": "Co-Founder",                         "company": "1inch"},
    {"slug": "catie-romero-finger", "name": "Catie Romero-Finger",    "title": "CEO & Founder",                      "company": "BABs"},
    {"slug": "kevin-hurley",        "name": "Kevin Hurley",           "title": "Creator, Spark; Co-Founder & CTO",   "company": "Lightspark"},
    {"slug": "robinson-burkey",     "name": "Robinson Burkey",        "title": "Co-Founder",                         "company": "Wormhole Foundation"},
    {"slug": "tim-kravchunovsky",   "name": "Tim Kravchunovsky",      "title": "CEO",                                "company": "Chirp"},
    {"slug": "bogdan-radulescu",    "name": "Bogdan Radulescu",       "title": "Co-Founder & CBO",                   "company": "Rhuna.io and UNTOLD"},
    {"slug": "nicola-massella",     "name": "Nicola Massella",        "title": "Legal Partner",                      "company": "STORM Partners"},
    {"slug": "francois-volpoet",    "name": "François Volpoet",       "title": "General Manager",                    "company": "Chainalysis"},
    {"slug": "paolo-tasca",         "name": "Paolo Tasca",            "title": "Co-Founder & Chairman",              "company": "DLT Science Foundation"},
    {"slug": "santiago-roel-santos","name": "Santiago Roel Santos",   "title": "Founder",                            "company": "Inversion"},
    # Speakers confirmed via press releases but individual pages not indexed yet
    {"slug": "",                    "name": "Barry Silbert",          "title": "Founder",                            "company": "Digital Currency Group"},
    {"slug": "",                    "name": "Cathie Wood",            "title": "Founder & CEO",                      "company": "ARK Investment Management"},
    {"slug": "",                    "name": "Sandy Kaul",             "title": "EVP Head of Innovation",             "company": "Franklin Templeton"},
    {"slug": "",                    "name": "Tom Zschach",            "title": "CIO",                                "company": "SWIFT"},
    {"slug": "",                    "name": "Ken Moore",              "title": "Chief Innovation Officer",           "company": "Mastercard"},
    {"slug": "",                    "name": "Emma Landriault",        "title": "Executive Director, JPM Coin",       "company": "JPMorganChase"},
    {"slug": "",                    "name": "Charles Guillemet",      "title": "CTO",                                "company": "Ledger"},
    {"slug": "",                    "name": "Caroline Pham",          "title": "Former Acting Chair",                "company": "CFTC"},
    {"slug": "",                    "name": "Arnaud Caudoux",         "title": "Deputy CEO",                         "company": "Bpifrance"},
    {"slug": "",                    "name": "David Rutter",           "title": "CEO",                                "company": "R3"},
    {"slug": "",                    "name": "Carlos Domingo",         "title": "CEO",                                "company": "Securitize"},
    {"slug": "",                    "name": "Ala Shaabana",           "title": "Co-Founder",                         "company": "Bittensor"},
    {"slug": "",                    "name": "Johann Kerbrat",         "title": "SVP & GM Crypto",                    "company": "Robinhood"},
    {"slug": "",                    "name": "Ari Juels",              "title": "Chief Scientist",                    "company": "Chainlink Labs / Cornell Tech"},
    {"slug": "",                    "name": "Rob Hadick",             "title": "General Partner",                    "company": "Dragonfly"},
    {"slug": "",                    "name": "Tom Lee",                "title": "CIO",                                "company": "Fundstrat"},
    {"slug": "",                    "name": "Evan Cheng",             "title": "Co-Founder & CEO",                   "company": "Mysten Labs"},
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def parse_page_title(raw_title: str) -> dict:
    """
    Parse titles like:
      'Speaker: John Doe, CEO, Acme Corp | Proof of Talk Summit'
    into {name, title, company}.
    """
    # Strip site suffix
    clean = re.sub(r"\s*\|.*$", "", raw_title).strip()
    # Strip 'Speaker:' prefix
    clean = re.sub(r"^Speaker:\s*", "", clean).strip()
    parts = [p.strip() for p in clean.split(",")]
    return {
        "name":    parts[0] if len(parts) > 0 else "",
        "title":   parts[1] if len(parts) > 1 else "",
        "company": ", ".join(parts[2:]) if len(parts) > 2 else "",
    }


def clean_linkedin_url(href: str) -> str:
    """Unwrap Google redirect URLs and return a clean linkedin.com/in/... link."""
    if not href:
        return ""
    if "google.com" in href and "url=" in href:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        href = qs.get("url", [href])[0]
    # Normalise: keep only up to the profile slug
    m = re.search(r"(https?://(?:www\.)?linkedin\.com/in/[^/?&#\s]+)", href)
    return m.group(1) if m else href


# ─── Scraping ─────────────────────────────────────────────────────────────────

def get_all_speaker_urls(page) -> list[str]:
    """Load the speakers index and collect every /speakers/<slug> link."""
    print(f"\n[1/3] Loading {SPEAKERS_URL} ...")
    page.goto(SPEAKERS_URL, wait_until="networkidle", timeout=90_000)
    page.wait_for_timeout(4_000)

    # Scroll to trigger lazy-load
    for _ in range(6):
        page.evaluate("window.scrollBy(0, window.innerHeight)")
        page.wait_for_timeout(800)

    hrefs: set[str] = set()
    for el in page.query_selector_all("a[href]"):
        href = el.get_attribute("href") or ""
        # Match /speakers/<something> but not the index itself
        m = re.match(r".*/speakers/([^/?\s]+)/?$", href)
        if m and m.group(1):
            slug = m.group(1)
            hrefs.add(f"https://proofoftalk.io/speakers/{slug}")

    print(f"   Found {len(hrefs)} speaker profile links on the page.")
    return sorted(hrefs)


def scrape_speaker_profile(page, url: str) -> dict:
    """Visit one speaker profile page and return extracted data."""
    slug = url.rstrip("/").split("/")[-1]
    print(f"   • {url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40_000)
        page.wait_for_timeout(2_000)

        # ── Try JSON-LD structured data ──────────────────────────────────────
        for script in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.inner_text())
                if isinstance(data, dict) and data.get("name"):
                    works_for = data.get("worksFor", {})
                    company = works_for.get("name", "") if isinstance(works_for, dict) else ""
                    return {
                        "slug":    slug,
                        "name":    data.get("name", "").strip(),
                        "title":   data.get("jobTitle", "").strip(),
                        "company": company.strip(),
                        "url":     url,
                    }
            except Exception:
                pass

        # ── Parse from <title> tag ───────────────────────────────────────────
        page_title = page.title()
        if "Speaker:" in page_title:
            parsed = parse_page_title(page_title)
            if parsed["name"]:
                return {**parsed, "slug": slug, "url": url}

        # ── Fallback: DOM selectors ──────────────────────────────────────────
        def text(sel):
            el = page.query_selector(sel)
            return el.inner_text().strip() if el else ""

        name = (
            text("h1.speaker-name")
            or text("h1")
            or slug.replace("-", " ").title()
        )
        title   = text(".speaker-title, .job-title, [class*='title']")
        company = text(".speaker-company, [class*='company'], [class*='org']")

        return {"slug": slug, "name": name, "title": title, "company": company, "url": url}

    except PWTimeout:
        print(f"     Timeout — using slug as name fallback.")
        return {
            "slug": slug,
            "name": slug.replace("-", " ").title(),
            "title": "", "company": "", "url": url,
        }
    except Exception as e:
        print(f"     Error: {e}")
        return {"slug": slug, "name": "", "title": "", "company": "", "url": url}


# ─── LinkedIn search ──────────────────────────────────────────────────────────

def find_linkedin(page, name: str, company: str) -> str:
    """
    Google search for a LinkedIn profile URL.
    Returns the first linkedin.com/in/ link found, or empty string.
    """
    if not name:
        return ""

    query = f'site:linkedin.com/in "{name}"'
    if company:
        # Use just the first token of company to avoid overly specific queries
        short_co = company.split("/")[0].split("&")[0].strip()
        query += f' "{short_co}"'

    search_url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    print(f"   LinkedIn → {name} @ {company}")

    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_500)

        # Check for CAPTCHA / rate-limit page
        if "unusual traffic" in page.content().lower() or "captcha" in page.content().lower():
            print("     Google CAPTCHA triggered — skipping LinkedIn search for now.")
            return ""

        # Look for direct linkedin.com/in links in anchors
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            if "linkedin.com/in/" in href:
                return clean_linkedin_url(href)

        # Fallback: regex over raw HTML
        m = re.search(r"linkedin\.com/in/([a-zA-Z0-9\-_%]+)", page.content())
        if m:
            return f"https://www.linkedin.com/in/{m.group(1)}"

    except Exception as e:
        print(f"     LinkedIn search error: {e}")

    return ""


# ─── Excel export ─────────────────────────────────────────────────────────────

def write_excel(speakers: list[dict], path: str) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Speakers"

    # Styles
    hdr_font    = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    hdr_fill    = PatternFill("solid", fgColor="1B4F8A")
    hdr_align   = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_align  = Alignment(vertical="center", wrap_text=False)
    alt_fill    = PatternFill("solid", fgColor="EBF2FF")
    link_font   = Font(name="Calibri", color="0563C1", underline="single", size=10)
    normal_font = Font(name="Calibri", size=10)
    thin        = Side(style="thin", color="CCCCCC")
    border      = Border(bottom=thin)

    headers    = ["First Name", "Last Name", "Title", "Company", "LinkedIn URL", "Profile Page"]
    col_widths = [18, 22, 45, 30, 58, 58]

    for ci, (hdr, width) in enumerate(zip(headers, col_widths), 1):
        cell = ws.cell(row=1, column=ci, value=hdr)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.alignment = hdr_align
        ws.column_dimensions[cell.column_letter].width = width

    ws.row_dimensions[1].height = 28

    for ri, spk in enumerate(speakers, 2):
        first, last = split_name(spk.get("name", ""))
        row_values = [
            first,
            last,
            spk.get("title", ""),
            spk.get("company", ""),
            spk.get("linkedin", ""),
            spk.get("url", ""),
        ]
        use_alt = (ri % 2 == 0)
        for ci, val in enumerate(row_values, 1):
            cell = ws.cell(row=ri, column=ci, value=val)
            cell.border = border
            cell.alignment = data_align
            if use_alt:
                cell.fill = alt_fill
            if ci in (5, 6) and val:
                cell.font      = link_font
                cell.hyperlink = val
            else:
                cell.font = normal_font

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:F{len(speakers) + 1}"

    wb.save(path)
    print(f"\n✓ Saved → {path}  ({len(speakers)} speakers)")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    with sync_playwright() as pw:
        # headless=False reduces bot-detection risk on the target site
        browser = pw.chromium.launch(headless=False, slow_mo=50)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => false});"
        )

        main_page   = ctx.new_page()
        search_page = ctx.new_page()

        # ── Step 1: collect live speaker URLs ─────────────────────────────────
        live_urls: list[str] = []
        try:
            live_urls = get_all_speaker_urls(main_page)
        except Exception as e:
            print(f"[!] Could not load speakers index: {e}")
            print("    Falling back entirely to seed data.")

        # Build a combined, de-duplicated speaker list
        # Seed data takes priority for known speakers; live URLs add new ones.
        seen_slugs: set[str] = set()
        speakers: list[dict] = []

        for spk in SEED_SPEAKERS:
            slug = spk.get("slug", "")
            seen_slugs.add(slug)
            entry = dict(spk)
            if slug:
                entry["url"] = f"https://proofoftalk.io/speakers/{slug}"
            else:
                entry["url"] = ""
            speakers.append(entry)

        # Add any additional live URLs not already in seed
        extra_urls = [u for u in live_urls if u.rstrip("/").split("/")[-1] not in seen_slugs]
        if extra_urls:
            print(f"\n[+] {len(extra_urls)} new speaker(s) found on live page — scraping profiles...")
        for url in extra_urls:
            profile = scrape_speaker_profile(main_page, url)
            speakers.append(profile)
            time.sleep(1.2)

        # ── Step 2: enrich seed speakers that have a URL but no scraped detail
        print(f"\n[2/3] Verifying / enriching {len(speakers)} speaker profiles ...")
        for spk in speakers:
            url = spk.get("url", "")
            # Skip if name already populated or no URL to visit
            if not url or spk.get("name"):
                continue
            profile = scrape_speaker_profile(main_page, url)
            spk.update({k: v for k, v in profile.items() if v})  # only overwrite blanks
            time.sleep(1.0)

        # ── Step 3: LinkedIn search ────────────────────────────────────────────
        print(f"\n[3/3] Searching LinkedIn profiles ({len(speakers)} speakers) ...")
        for spk in speakers:
            linkedin = find_linkedin(search_page, spk.get("name", ""), spk.get("company", ""))
            spk["linkedin"] = linkedin
            time.sleep(2.5)   # polite delay — Google will block if you hammer it

        browser.close()

    # ── Step 4: export ────────────────────────────────────────────────────────
    write_excel(speakers, OUTPUT_FILE)


if __name__ == "__main__":
    main()
