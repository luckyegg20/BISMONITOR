#!/usr/bin/env python3
"""
Stock watch.

For each URL in products.json:
  1. Fetches the product page.
  2. Decides in stock / out of stock / unknown.
  3. Writes docs/index.html: two columns, product name and stock.
  4. Emails when something flips from out of stock to in stock.

Run:  python check.py
      python check.py --dry-run    check and print, send nothing
      python check.py --seed       record current state, alert on nothing
"""

import argparse
import json
import os
import random
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent
PRODUCTS_FILE = ROOT / "products.json"
STATE_FILE = ROOT / "state.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

TIMEOUT = 35
REQUEST_PAUSE = (3.0, 9.0)   # random seconds between products
JITTER_MAX = 150             # random seconds before the first request
HISTORY_KEEP = 5             # restock timestamps kept per product

# curl error 92 and friends: the server killed the HTTP/2 stream rather than
# answering. Retrying the same request over HTTP/1.1 normally succeeds.
STREAM_RESET = re.compile(
    r"stream\s+\d*\s*reset|INTERNAL_ERROR|HTTP/2|\(92\)|\(16\)", re.I
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
}


def make_fetcher():
    """
    Return (get, name).

    Retailers behind Akamai and Cloudflare fingerprint the TLS handshake, not
    just the headers. Python's handshake is recognisable whatever User-Agent it
    claims, which is where the 403 comes from. curl_cffi reproduces Chrome's
    handshake and gets through. Plain requests stays as the fallback for sites
    that do not check.
    """
    try:
        from curl_cffi import requests as cffi

        try:
            from curl_cffi.const import CurlHttpVersion
            v11 = CurlHttpVersion.V1_1
        except Exception:  # noqa: BLE001
            v11 = None

        session = cffi.Session(impersonate="chrome")

        def get(url):
            try:
                return session.get(url, timeout=TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                # Akamai answers some requests with an HTTP/2 stream reset.
                # The same request over HTTP/1.1 usually goes straight through.
                if v11 is None or not STREAM_RESET.search(str(exc)):
                    raise
                return session.get(url, timeout=TIMEOUT, http_version=v11)

        return get, "curl_cffi, Chrome handshake"
    except ImportError:
        session = requests.Session()

        def get(url):
            return session.get(url, headers=HEADERS, timeout=TIMEOUT)

        return get, "requests, Python handshake"


# ----------------------------------------------------------------------
# NVIDIA marketplace
# ----------------------------------------------------------------------
# marketplace.nvidia.com renders the Add to Cart button with display:none and
# only reveals it after its own JS calls an inventory endpoint. The page ships
# the sku and the endpoint in plain sight, so this does what the page does.

NV_HOST = "marketplace.nvidia.com"
NV_UPC = re.compile(r'var\s+upc\s*=\s*"([^"]+)"')
NV_DRID = re.compile(r'var\s+drid\s*=\s*"([^"]*)"')
NV_API = "https://api.store.nvidia.com/partner/v1/feinventory?skus=%s&locale=%s"


def nvidia_inventory(get, url, html):
    """Return (status, evidence, price) for an NVIDIA marketplace product."""
    m = NV_UPC.search(html)
    if not m:
        return "unknown", "nvidia sku not found in page", None
    sku = m.group(1).split("_")[0].upper()

    drid = NV_DRID.search(html)
    if drid and drid.group(1).strip():
        sku = drid.group(1).strip().upper()

    parts = [p for p in urlparse(url).path.split("/") if p]
    locale = parts[0] if parts else "en-us"

    try:
        resp = get(NV_API % (sku, locale))
        rows = json.loads(resp.text).get("listMap") or []
    except Exception as exc:  # noqa: BLE001
        return "unknown", "nvidia api error: %s" % str(exc)[:60], None

    if not rows:
        return "unknown", "nvidia api returned no rows for %s" % sku, None

    row = rows[0]
    active = str(row.get("is_active", "")).strip().lower()
    price = row.get("price")
    price = ("$" + str(price)) if price not in (None, "") else None

    if active == "true":
        return "in", "nvidia api is_active true (%s)" % sku, price
    if active == "false":
        return "out", "nvidia api is_active false (%s)" % sku, price
    return "unknown", "nvidia api is_active=%r" % row.get("is_active"), price


# ----------------------------------------------------------------------
# Stock detection
# ----------------------------------------------------------------------
# Three layers, checked in order. The first layer that gives a clear answer
# wins. If none do, the answer is "unknown" and nothing gets claimed.

# Layer 0: the state the retailer's own buy button reads. Best Buy ships a
# static schema.org InStock block on pages whose button says Sold Out, so this
# has to outrank layer 1. Only fires when the field is actually present.
BTN = re.compile(r'"buttonState"\s*:\s*"([A-Z_]+)"')
BTN_IN = {"ADD_TO_CART", "PRE_ORDER", "PRE_ORDER_MOBILE", "BUY_NOW", "SHOP_NOW"}
BTN_OUT = {"SOLD_OUT", "NOT_AVAILABLE", "COMING_SOON", "CHECK_STORES",
           "UNAVAILABLE", "DISCONTINUED", "GET_NOTIFIED", "NOTIFY_ME"}

# Layer 1: schema.org product markup.
LD_IN = re.compile(r'"availability"\s*:\s*"[^"]*?(?:InStock|LimitedAvailability)', re.I)
LD_OUT = re.compile(
    r'"availability"\s*:\s*"[^"]*?(?:OutOfStock|SoldOut|Discontinued|PreOrder|BackOrder)', re.I
)

# Layer 2: embedded state JSON the page ships to its own JavaScript.
JSON_IN = re.compile(
    r'"(?:inStock|isInStock|in_stock|available|isAvailable|purchasable|orderable)"\s*:\s*true', re.I
)
JSON_OUT = re.compile(
    r'"(?:inStock|isInStock|in_stock|available|isAvailable|purchasable|orderable)"\s*:\s*false', re.I
)
QTY = re.compile(r'"(?:quantityAvailable|availableQuantity|inventory|stockLevel)"\s*:\s*(\d+)', re.I)

# Layer 3: visible text. Last resort.
TEXT_ADD = re.compile(r"add to (?:bag|cart)", re.I)
TEXT_OUT = re.compile(
    r"(sold\s*out|currently unavailable|no longer available|notify me when)", re.I
)
TEXT_UNAVAIL = re.compile(r">\s*Unavailable\s*<", re.I)

STRIP_TAGS = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.I | re.S)
HIDDEN_NEAR = re.compile(r"display\s*:\s*none", re.I)


def has_visible_add_button(visible):
    """True only for an add-to-cart whose surrounding tag is not hidden.

    NVIDIA and others ship the button permanently and toggle display:none, so
    the bare presence of the words means nothing. Looks back over the tag the
    match sits in and rejects it if that tag declares display:none.
    """
    for m in TEXT_ADD.finditer(visible):
        window = visible[max(0, m.start() - 400):m.start()]
        tag_start = window.rfind("<")
        if tag_start != -1 and HIDDEN_NEAR.search(window[tag_start:]):
            continue
        if HIDDEN_NEAR.search(window[-250:]):
            continue
        return True
    return False


def decide_stock(html):
    """Return (status, evidence). status is 'in', 'out', or 'unknown'."""
    states = set(BTN.findall(html))
    if states & BTN_IN:
        return "in", "buy button %s" % sorted(states & BTN_IN)[0]
    if states & BTN_OUT:
        return "out", "buy button %s" % sorted(states & BTN_OUT)[0]

    ld_in, ld_out = bool(LD_IN.search(html)), bool(LD_OUT.search(html))
    if ld_in and not ld_out:
        return "in", "schema.org InStock"
    if ld_out and not ld_in:
        return "out", "schema.org OutOfStock"
    if ld_in and ld_out:
        # multi-variant product, at least one variant buyable
        return "in", "schema.org mixed, at least one InStock"

    if JSON_IN.search(html):
        return "in", "page data available:true"
    qty = [int(m) for m in QTY.findall(html)]
    if qty and max(qty) > 0:
        return "in", "page data quantity %d" % max(qty)
    if JSON_OUT.search(html) or (qty and max(qty) == 0):
        return "out", "page data available:false"

    visible = STRIP_TAGS.sub(" ", html)
    add = has_visible_add_button(visible)
    gone = bool(TEXT_OUT.search(visible)) or bool(TEXT_UNAVAIL.search(visible))
    if add and not gone:
        return "in", "add to bag button present"
    if gone and not add:
        return "out", "page reads unavailable"

    return "unknown", "no stock signal found"


TITLE = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
TITLE2 = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def page_name(html, fallback):
    m = TITLE.search(html)
    if not m:
        m = TITLE2.search(html)
    if not m:
        return fallback
    name = re.sub(r"\s+", " ", m.group(1)).strip()
    name = re.split(r"\s*\|\s*", name)[0].strip()
    return name or fallback


# ----------------------------------------------------------------------
# Checking
# ----------------------------------------------------------------------

def load_products():
    raw = json.loads(PRODUCTS_FILE.read_text())
    out = []
    for item in raw:
        if isinstance(item, str):
            out.append({"url": item, "label": None})
        else:
            out.append({"url": item["url"], "label": item.get("label")})
    return out


def check_one(get, product, debug=False):
    url = product["url"]
    result = {"url": url, "label": product["label"], "status": "unknown",
              "evidence": None, "error": None, "price": None}
    last = None
    for attempt in range(3):
        try:
            r = get(url)
            if r.status_code in (403, 429, 503):
                raise RuntimeError("blocked, HTTP %d" % r.status_code)
            r.raise_for_status()
            html = r.text
            if debug:
                Path(ROOT / "last_page.html").write_text(html, encoding="utf-8")
                print("saved last_page.html, %d bytes" % len(html))

            nv_reason = None
            if NV_HOST in url:
                st, ev, pr = nvidia_inventory(get, url, html)
                if st != "unknown":
                    result["status"], result["evidence"] = st, ev
                    result["price"] = pr
                    result["label"] = product["label"] or page_name(html, url)
                    time.sleep(random.uniform(*REQUEST_PAUSE))
                    return result
                nv_reason = ev
            result["status"], result["evidence"] = decide_stock(html)
            if result["status"] == "unknown" and nv_reason:
                result["evidence"] = nv_reason
            result["label"] = product["label"] or page_name(html, url)
            p = re.search(r'"(?:price|listPrice|currentPrice)"\s*:\s*"?(\d+\.\d{2})', html)
            if not p:
                p = re.search(r"\$(\d+\.\d{2})", STRIP_TAGS.sub(" ", html))
            if p:
                result["price"] = "$" + p.group(1)
            time.sleep(random.uniform(*REQUEST_PAUSE))
            return result
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(random.uniform(4, 8) * (attempt + 1))
    result["error"] = str(last)
    result["label"] = product["label"] or url
    return result


# ----------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------

def send_email(subject, body):
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("ALERT_TO")
    if not host or not to:
        print("email not configured, skipping")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("ALERT_FROM", os.environ.get("SMTP_USER", to))
    msg["To"] = to
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "465"))
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=30)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.starttls()
    with server:
        user = os.environ.get("SMTP_USER")
        if user:
            server.login(user, os.environ["SMTP_PASS"])
        server.send_message(msg)
    print("email sent to %s" % to)
    return True


def send_slack(text):
    url = os.environ.get("SLACK_WEBHOOK")
    if not url:
        return False
    requests.post(url, json={"text": text}, timeout=20)
    print("slack notified")
    return True


# ----------------------------------------------------------------------
# Dashboard
# ----------------------------------------------------------------------

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


CSS = """
:root{
  --ink:#0E1216; --panel:#161B21; --line:#232B33; --line2:#2E3841;
  --text:#E9E7E2; --dim:#8B939B; --faint:#666E76;
  --hot:#E2564D; --cool:#5E9E7B; --warn:#D9A441;
}
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ink);color:var(--text);
  font:16px/1.5 "Helvetica Neue",Helvetica,Arial,sans-serif;
  font-variant-numeric:tabular-nums}
.wrap{max-width:760px;margin:0 auto;padding:28px 20px 80px}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:baseline;
  justify-content:space-between;border-bottom:2px solid var(--text);
  padding-bottom:12px;margin-bottom:24px}
h1{margin:0;font-size:21px;font-weight:600;letter-spacing:-.01em}
.stamp{font-size:13px;color:var(--dim)}
.stamp b{color:var(--text);font-weight:600}
.headline{font-size:clamp(25px,4.6vw,36px);line-height:1.18;font-weight:600;
  letter-spacing:-.02em;margin:0 0 28px;max-width:22ch}
.headline.quiet{color:var(--dim);font-weight:400}
.hit{border-left:3px solid var(--cool);background:var(--panel);
  padding:14px 16px;margin-bottom:10px}
.hit .who{font-weight:600}
.hit a{color:var(--cool);text-decoration:none;border-bottom:1px solid var(--line2);
  font-size:14px}
.hit a:hover,.hit a:focus{border-bottom-color:var(--cool)}
table{width:100%;border-collapse:collapse;margin-top:6px}
th{text-align:left;font-size:12px;font-weight:600;color:var(--dim);
  letter-spacing:.04em;text-transform:uppercase;
  padding:0 10px 8px 0;border-bottom:1px solid var(--line2)}
th.s,td.s{text-align:right;padding-right:0;white-space:nowrap}
td{padding:13px 10px 13px 0;border-bottom:1px solid var(--line);
  font-size:15px;vertical-align:top}
td a{color:var(--text);text-decoration:none;border-bottom:1px solid var(--line2)}
td a:hover,td a:focus{border-bottom-color:var(--text)}
.sub{display:block;font-size:12px;color:var(--faint);margin-top:3px}
.hist{display:block;font-size:12px;color:var(--cool);margin-top:3px;
  font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:12px;font-weight:600;letter-spacing:.04em;
  padding:2px 8px}
.tag.in{background:var(--cool);color:var(--ink)}
.tag.out{color:var(--dim);border:1px solid var(--line2)}
.tag.unk{color:var(--warn);border:1px solid var(--warn)}
.tag.err{background:var(--hot);color:var(--ink)}
footer{margin-top:36px;font-size:13px;color:var(--faint);line-height:1.7}
a:focus-visible{outline:2px solid var(--warn);outline-offset:3px}
@media(max-width:560px){td{font-size:14px;padding-right:6px}}
"""

TAG = {
    "in": ('<span class="tag in">IN STOCK</span>', "in"),
    "out": ('<span class="tag out">out of stock</span>', "out"),
    "unknown": ('<span class="tag unk">unknown</span>', "unk"),
}


def fmt_stamp(iso):
    """'2026-09-12T15:14:00-04:00' -> 'Sep 12, 3:14 PM'.

    Windows strftime rejects the %-d / %-I no-padding flags, so the zeros come
    off by hand rather than in the format string.
    """
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    day = str(int(dt.strftime("%d")))
    hour = str(int(dt.strftime("%I")))
    return "%s %s, %s:%s %s" % (dt.strftime("%b"), day, hour,
                                dt.strftime("%M"), dt.strftime("%p"))


def render(results, restocks, checked_at):
    if restocks:
        n = len(restocks)
        head = '<p class="headline">%d item%s back in stock.</p>' % (n, "" if n == 1 else "s")
    else:
        n_in = sum(1 for r in results if r["status"] == "in")
        if n_in:
            head = '<p class="headline quiet">%d of %d in stock. No change.</p>' % (
                n_in, len(results))
        else:
            head = '<p class="headline quiet">Nothing in stock yet.</p>'

    blocks = []
    for r in restocks:
        blocks.append(
            '<div class="hit"><div class="who">%s</div>'
            '<a href="%s">Open the product page</a></div>'
            % (esc(r["label"]), esc(r["url"]))
        )

    rows = []
    for r in results:
        if r["error"]:
            tag = '<span class="tag err">check failed</span>'
            sub = esc(r["error"][:90])
        else:
            tag = TAG[r["status"]][0]
            sub = esc(r["evidence"] or "")
            if r["price"]:
                sub = "%s . %s" % (esc(r["price"]), sub)
        hist = r.get("history") or []
        if hist:
            seen = "In stock: " + " &middot; ".join(fmt_stamp(h) for h in hist)
        else:
            seen = "No restocks recorded yet"
        rows.append(
            '<tr><td><a href="%s">%s</a><span class="sub">%s</span>'
            '<span class="hist">%s</span></td>'
            '<td class="s">%s</td></tr>'
            % (esc(r["url"]), esc(r["label"]), sub, seen, tag)
        )

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Stock watch</title>
<style>%s</style></head>
<body><div class="wrap">
<header><h1>Stock watch</h1><div class="stamp">Checked <b>%s</b></div></header>
%s
%s
<table>
<thead><tr><th>Product</th><th class="s">Stock</th></tr></thead>
<tbody>%s</tbody></table>
<footer>Click a product name to open the retailer page.
The green line under each product lists the last %d times it came back in stock.
An item marked unknown means the page loaded but carried no stock signal the
checker recognises.</footer>
</div></body></html>""" % (CSS, esc(checked_at), head, "".join(blocks), "".join(rows),
                           HISTORY_KEEP)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="save the fetched page to last_page.html")
    ap.add_argument("--no-jitter", action="store_true",
                    help="start immediately instead of waiting a random delay")
    args = ap.parse_args()

    # Task Scheduler fires on the clock. Requests landing at exactly :00, :05,
    # :10 for weeks read as automated however infrequent they are. A random
    # delay up to JITTER_MAX seconds breaks the pattern. Skipped when you are
    # sitting at the terminal waiting for output.
    if not (args.seed or args.dry_run or args.no_jitter or args.debug):
        wait = random.randint(0, JITTER_MAX)
        print("jitter: waiting %ds" % wait)
        time.sleep(wait)

    products = load_products()
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    checked_at = datetime.now(timezone.utc).astimezone().strftime(
        "%b %d, %Y at %I:%M %p %Z")

    session_stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    get, transport = make_fetcher()
    print("transport: %s" % transport)
    results, restocks, new_state = [], [], {}

    for p in products:
        r = check_one(get, p, debug=args.debug)
        results.append(r)
        prev_entry = state.get(r["url"], {})
        prev = prev_entry.get("status")
        history = list(prev_entry.get("history", []))

        # A flip to in stock is worth dating. Only counts when we previously
        # saw the item NOT in stock, so seeding a live item logs nothing.
        if not r["error"] and r["status"] == "in" and prev in ("out", "unknown"):
            history.insert(0, session_stamp)
            history = history[:HISTORY_KEEP]
            if not args.seed:
                restocks.append(r)

        r["history"] = history

        # A failed check keeps the old status so an outage never fakes a restock.
        new_state[r["url"]] = {
            "status": prev if r["error"] else r["status"],
            "label": r["label"],
            "history": history,
        }

        print("%-52s %-8s %s" % (r["label"][:52], r["status"],
                                 r["error"] or r["evidence"]))

    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(render(results, restocks, checked_at))
    print("dashboard written to %s" % DASHBOARD_FILE)

    if restocks and not args.dry_run and not args.seed:
        body = "\n\n".join("%s\nIN STOCK%s\n%s" % (
            r["label"], (" . " + r["price"]) if r["price"] else "", r["url"])
            for r in restocks)
        subject = "In stock: " + ", ".join(r["label"] for r in restocks)[:90]
        send_email(subject, body + "\n\nChecked %s" % checked_at)
        send_slack(subject + "\n" + body)
    elif restocks:
        print("\nRESTOCK: " + ", ".join(r["label"] for r in restocks))

    STATE_FILE.write_text(json.dumps(new_state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(130)
