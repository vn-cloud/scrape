"""Diagnostic tool: shows how a web page loads its data, to help write or fix a source.

    python -m scout.tools.probe https://www.rootdata.com/Fundraising [more URLs] [--wait 8]

For each URL it prints (1) what a plain HTTP request gets back (status, anti-bot
markers, embedded JSON state) and (2) every XHR/fetch request a real headless
browser makes, with a preview of the JSON responses. Full dumps are written to
probe_output/ (uploaded as a build artifact in GitHub Actions).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..http import USER_AGENT

OUT_DIR = Path("probe_output")
MARKERS = {
    "__NUXT__": "Nuxt state", "__NEXT_DATA__": "Next.js state", "__INITIAL_STATE__": "initial state",
    "cf-chl": "Cloudflare challenge", "Just a moment": "Cloudflare interstitial",
    "challenge-platform": "Cloudflare challenge", "captcha": "captcha",
}


def _slug(url: str) -> str:
    p = urlparse(url)
    return re.sub(r"[^A-Za-z0-9]+", "_", p.netloc + p.path).strip("_")[:80]


def plain_request(url: str) -> None:
    print(f"\n=== PLAIN GET {url}")
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
    except requests.RequestException as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return
    print(f"  status={r.status_code} final_url={r.url} len={len(r.text)}")
    for h in ("server", "cf-ray", "content-type", "x-powered-by"):
        if h in r.headers:
            print(f"  {h}: {r.headers[h]}")
    title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    print(f"  title: {title.group(1).strip()[:120] if title else '-'}")
    found = [desc for m, desc in MARKERS.items() if m in r.text]
    print(f"  markers: {', '.join(found) or 'none'}")
    print(f"  cookies: {', '.join(r.cookies.keys()) or '-'}")
    if len(r.text) < 6000 or found:
        print("  body (first 3000 chars):")
        print("  | " + r.text[:3000].replace("\n", "\n  | "))
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / f"{_slug(url)}.plain.html").write_text(r.text, encoding="utf-8")


def browser_probe(url: str, wait: float, max_body: int) -> None:
    print(f"\n=== BROWSER {url}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed")
        return

    calls = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1440, "height": 2000})

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch"):
                return
            entry = {"method": req.method, "url": resp.url, "status": resp.status,
                     "post": (req.post_data or "")[:600], "ctype": resp.headers.get("content-type", "")}
            try:
                entry["body"] = resp.text()
            except Exception as exc:  # noqa: BLE001
                entry["body"] = f"<unreadable: {exc}>"
            calls.append(entry)

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(wait * 1000)
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(2000)
        except Exception as exc:  # noqa: BLE001
            print(f"  navigation error: {exc}")
        print(f"  final_url={page.url} title={page.title()[:120]!r}")
        html_text = page.content()
        try:
            visible = page.inner_text("body")
        except Exception:  # noqa: BLE001
            visible = ""
        OUT_DIR.mkdir(exist_ok=True)
        slug = _slug(url)
        (OUT_DIR / f"{slug}.browser.html").write_text(html_text, encoding="utf-8")
        (OUT_DIR / f"{slug}.calls.json").write_text(json.dumps(calls, indent=1, ensure_ascii=False), encoding="utf-8")
        page.screenshot(path=str(OUT_DIR / f"{slug}.png"), full_page=False)
        browser.close()

    found = [desc for m, desc in MARKERS.items() if m in html_text]
    print(f"  markers: {', '.join(found) or 'none'}")
    if len(html_text) < 6000:
        print("  rendered html (first 3000 chars):")
        print("  | " + html_text[:3000].replace("\n", "\n  | "))
    print(f"  visible text ({len(visible)} chars), first 2500:")
    print("  | " + visible[:2500].replace("\n", "\n  | "))
    print(f"  XHR/fetch calls: {len(calls)}")
    for c in calls:
        print(f"  --> {c['method']} {c['status']} {c['url'][:300]}")
        if c["post"]:
            print(f"      post: {c['post']}")
        body = c["body"] or ""
        if "json" in c["ctype"] or body[:1] in "{[":
            print(f"      json[{len(body)}]: {body[:max_body]}")


def api_request(method: str, url: str, body: str | None) -> None:
    """Plain API call; ROOTDATA_API_KEY (if set) is sent as the 'apikey' header for rootdata hosts."""
    print(f"\n=== API {method} {url} {body or ''}")
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json", "language": "en"}
    key = os.environ.get("ROOTDATA_API_KEY")
    if key and "rootdata" in url:
        headers["apikey"] = key
    try:
        r = requests.request(method, url, headers=headers, data=body, timeout=30)
    except requests.RequestException as exc:
        print(f"  ERROR {type(exc).__name__}: {exc}")
        return
    print(f"  status={r.status_code} len={len(r.text)} ctype={r.headers.get('content-type', '')} server={r.headers.get('server', '')}")
    print("  | " + r.text[:2500].replace("\n", "\n  | "))


def read_targets(path: Path) -> list[tuple[str, str, str | None]]:
    """Lines: 'URL' (page probe), 'GET URL' or 'POST URL {json}' (plain API call)."""
    targets = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        if parts[0].upper() in ("GET", "POST"):
            targets.append((parts[0].upper(), parts[1], parts[2] if len(parts) > 2 else None))
        else:
            targets.append(("PAGE", parts[0], None))
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="*")
    parser.add_argument("--targets", type=Path, help="file with one target per line (see read_targets)")
    parser.add_argument("--wait", type=float, default=12, help="seconds to wait after page load")
    parser.add_argument("--max-body", type=int, default=1200, help="characters of each JSON response to print")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    targets = [("PAGE", u, None) for u in args.urls]
    if args.targets:
        targets += read_targets(args.targets)
    for kind, url, body in targets:
        if kind == "PAGE":
            plain_request(url)
            if not args.no_browser:
                browser_probe(url, args.wait, args.max_body)
        else:
            api_request(kind, url, body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
