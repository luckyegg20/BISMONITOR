#!/usr/bin/env python3
"""
NYC DOB BIS monitor.

For each building in buildings.json:
  1. Looks it up on BIS by borough + house number + street (same as the manual search).
  2. Opens Complaints, Violations-DOB, and Violations-OATH/ECB.
  3. Counts how many records are open.
  4. Alerts when an open count moves.

Run:  python monitor.py
      python monitor.py --dry-run      check and print, send nothing
      python monitor.py --seed         record current counts, alert on nothing
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
BUILDINGS_FILE = ROOT / "buildings.json"
STATE_FILE = ROOT / "state.json"
DASHBOARD_FILE = ROOT / "docs" / "index.html"

BIS_BASE = "https://a810-bisweb.nyc.gov/bisweb/"
PROFILE_URL = BIS_BASE + "PropertyProfileOverviewServlet"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}

SECTIONS = ["complaints", "dob_violations", "ecb_violations"]
SECTION_LABEL = {
    "complaints": "Complaints",
    "dob_violations": "Violations - DOB",
    "ecb_violations": "Violations - OATH/ECB",
}
SHORT_LABEL = {
    "complaints": "Complaints",
    "dob_violations": "DOB",
    "ecb_violations": "OATH/ECB",
}

REQUEST_PAUSE = 2.0
TIMEOUT = 40


# ----------------------------------------------------------------------
# BIS fetching
# ----------------------------------------------------------------------

def fetch(session, url, params=None, attempts=3):
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            time.sleep(REQUEST_PAUSE)
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(4 * (i + 1))
    raise RuntimeError("BIS request failed after %d tries: %s (%s)" % (attempts, url, last))


def open_profile(session, building):
    params = {
        "boro": str(building["boro"]),
        "houseno": str(building["houseno"]),
        "street": building["street"],
        "go2": " GO ",
        "requestid": "0",
    }
    r = fetch(session, PROFILE_URL, params=params)
    return r.text, r.url


def find_bin(html):
    m = re.search(r"allbin=(\d{6,8})", html)
    if m:
        return m.group(1)
    m = re.search(r"BIN\s*#?\s*[:\-]?\s*(\d{7})", html, re.I)
    return m.group(1) if m else None


def find_section_links(html, base_url):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split()).lower()
        low_href = a["href"].lower()
        target = urljoin(base_url, a["href"])

        if ("ecbquerybylocation" in low_href or "ecb" in text or "oath" in text) \
                and "ecb_violations" not in found:
            found["ecb_violations"] = target
        elif "actionsbylocation" in low_href and "stypeocv3=v" in low_href \
                and "dob_violations" not in found:
            found["dob_violations"] = target
        elif "complaint" in low_href and "complaint" in text \
                and "complaints" not in found:
            found["complaints"] = target
    return found


def fallback_links(bin_number):
    b = str(bin_number)
    return {
        "complaints": BIS_BASE + "OverviewForComplaintServlet?" + urlencode(
            {"requestid": "1", "allbin": b, "allinquirytype": "BXS3OCV3"}
        ),
        "dob_violations": BIS_BASE + "ActionsByLocationServlet?" + urlencode(
            {"requestid": "1", "allbin": b, "allinquirytype": "BXS4OCV3", "stypeocv3": "V"}
        ),
        "ecb_violations": BIS_BASE + "ECBQueryByLocationServlet?" + urlencode(
            {"requestid": "1", "allbin": b}
        ),
    }


# ----------------------------------------------------------------------
# Counting open records
# ----------------------------------------------------------------------

NOISE = re.compile(r"(date of this report|requestid|©|copyright|privacy policy|back to)", re.I)
HAS_RECORD = re.compile(r"\d{2}/\d{2}/\d{4}|\d{6,}|\b[A-Z]?\d{5,}\b")

# BIS closed markers. Complaints close as CLOSED. DOB violations show RESOLVE or
# DISMISS, and a dismissed number carries an asterisk (V*7052-18P). ECB violations
# are open in ACTIVE status and closed in DISMISSED status.
CLOSED = re.compile(
    r"\b(CLOSED|RESOLVE[DS]?|DISMISS(ED)?|CURED|COMPLIED|WRITTEN OFF|PAID IN FULL)\b", re.I
)
OPEN_WORD = re.compile(r"\b(ACTIVE|OPEN|IN VIOLATION|DEFAULTED|OUTSTANDING)\b", re.I)
DISMISSED_NUMBER = re.compile(r"^[A-Z]\*")


def count_records(html):
    """Return (open_count, total_count) for a BIS results page."""
    soup = BeautifulSoup(html, "html.parser")
    total = 0
    open_count = 0
    for tr in soup.find_all("tr"):
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        line = " | ".join(cells)
        if NOISE.search(line) or not HAS_RECORD.search(line):
            continue
        total += 1
        if DISMISSED_NUMBER.match(cells[0]):
            continue
        if OPEN_WORD.search(line):
            open_count += 1
        elif not CLOSED.search(line):
            # no status wording either way, count it open so nothing gets missed
            open_count += 1
    return open_count, total


# ----------------------------------------------------------------------
# Check one building
# ----------------------------------------------------------------------

def check_building(session, building):
    result = {
        "label": building.get("label") or "%s %s" % (building["houseno"], building["street"]),
        "bin": None,
        "profile_url": None,
        "sections": {},
        "error": None,
    }
    try:
        html, url = open_profile(session, building)
        result["profile_url"] = url
        bin_number = building.get("bin") or find_bin(html)
        result["bin"] = bin_number

        links = find_section_links(html, url)
        if bin_number:
            for name, fb in fallback_links(bin_number).items():
                links.setdefault(name, fb)

        for name in SECTIONS:
            link = links.get(name)
            if not link:
                result["sections"][name] = {"url": None, "error": "link not found"}
                continue
            try:
                page = fetch(session, link)
                opened, total = count_records(page.text)
                result["sections"][name] = {
                    "url": link, "open": opened, "total": total, "error": None
                }
            except Exception as exc:  # noqa: BLE001
                result["sections"][name] = {"url": link, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# ----------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------

def build_alert_text(changes, checked_at):
    lines = ["NYC DOB BIS check - %s" % checked_at, ""]
    for c in changes:
        lines.append(c["building"])
        for sec in SECTIONS:
            d = c["counts"].get(sec)
            if not d:
                continue
            if d["was"] is None:
                lines.append("  %-22s %3d open" % (SECTION_LABEL[sec], d["now"]))
            elif d["now"] != d["was"]:
                lines.append(
                    "  %-22s %3d open   was %d   %+d"
                    % (SECTION_LABEL[sec], d["now"], d["was"], d["now"] - d["was"])
                )
            else:
                lines.append("  %-22s %3d open   no change" % (SECTION_LABEL[sec], d["now"]))
        if c.get("url"):
            lines.append("  " + c["url"])
        lines.append("")
    return "\n".join(lines)


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("ALERT_TO")
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
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )


DASHBOARD_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:#101418;color:#E8E6E1;
 font:16px/1.5 "Helvetica Neue",Helvetica,Arial,sans-serif;font-variant-numeric:tabular-nums}
.wrap{max-width:900px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:2px solid #E8E6E1;padding-bottom:14px;margin-bottom:28px;
 display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between}
h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-.01em}
.stamp{font-size:13px;color:#8A9199}
.headline{font-size:clamp(26px,4.5vw,38px);line-height:1.2;font-weight:600;
 letter-spacing:-.02em;margin:0 0 26px;max-width:22ch}
.headline.quiet{color:#8A9199;font-weight:400}
.moved{border-left:3px solid #E2564D;background:#171C21;padding:12px 16px;margin-bottom:10px;
 font-size:15px}
.moved b{font-weight:600}
.moved .delta{color:#E2564D;font-weight:600}
.moved .delta.down{color:#5E9E7B}
table{width:100%;border-collapse:collapse;margin-top:14px}
th{text-align:left;font-size:12px;font-weight:600;color:#8A9199;
 padding:0 10px 8px 0;border-bottom:1px solid #2B333A}
th.n,td.n{text-align:right;padding-right:14px}
td{padding:12px 10px 12px 0;border-bottom:1px solid #1D2429;font-size:15px;vertical-align:top}
td a{color:#E8E6E1;text-decoration:none;border-bottom:1px solid #3C444B}
td a:hover,td a:focus{border-bottom-color:#E8E6E1}
td.n{font-weight:600;font-size:19px}
td.n.hot{color:#E2564D}
td.n.zero{color:#5A6169}
.of{display:block;font-size:11px;color:#6C737A;font-weight:400;margin-top:2px}
.sub{display:block;font-size:12px;color:#7C838A;margin-top:3px}
.err{color:#D9A441;font-size:13px}
footer{margin-top:40px;font-size:13px;color:#6C737A;line-height:1.7}
a:focus-visible{outline:2px solid #D9A441;outline-offset:3px}
@media(max-width:620px){th.n,td.n{padding-right:6px}td.n{font-size:17px}}
"""


def render_dashboard(results, changes, checked_at):
    total_open = sum(s.get("open", 0) for r in results for s in r["sections"].values())
    if changes:
        head = '<p class="headline">%d open record%s. %d building%s moved.</p>' % (
            total_open, "" if total_open == 1 else "s",
            len(changes), "" if len(changes) == 1 else "s",
        )
    else:
        head = '<p class="headline quiet">%d open record%s. Nothing moved.</p>' % (
            total_open, "" if total_open == 1 else "s"
        )

    blocks = []
    for c in changes:
        bits = []
        for sec in SECTIONS:
            d = c["counts"].get(sec)
            if not d or d["was"] is None or d["now"] == d["was"]:
                continue
            delta = d["now"] - d["was"]
            cls = "delta down" if delta < 0 else "delta"
            bits.append(
                '%s %d open <span class="%s">(%+d)</span>'
                % (esc(SHORT_LABEL[sec]), d["now"], cls, delta)
            )
        if bits:
            blocks.append(
                '<div class="moved"><b>%s</b>: %s</div>' % (esc(c["building"]), ", ".join(bits))
            )

    rows = []
    for r in results:
        cells = []
        for sec in SECTIONS:
            s = r["sections"].get(sec, {})
            if s.get("error") or "open" not in s:
                cells.append('<td class="n err">!</td>')
                continue
            cls = "n hot" if s["open"] else "n zero"
            inner = str(s["open"])
            if s.get("url"):
                inner = '<a href="%s">%s</a>' % (esc(s["url"]), s["open"])
            cells.append(
                '<td class="%s">%s<span class="of">of %s</span></td>'
                % (cls, inner, s.get("total", "?"))
            )
        name = esc(r["label"])
        if r.get("profile_url"):
            name = '<a href="%s">%s</a>' % (esc(r["profile_url"]), name)
        sub = "BIN %s" % esc(r["bin"]) if r.get("bin") else '<span class="err">not found on BIS</span>'
        if r.get("error"):
            sub = '<span class="err">%s</span>' % esc(r["error"][:120])
        rows.append('<tr><td>%s<span class="sub">%s</span></td>%s</tr>' % (name, sub, "".join(cells)))

    html = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DOB watch</title>
<style>__CSS__</style></head>
<body><div class="wrap">
<header><h1>DOB watch</h1><div class="stamp">Checked __WHEN__</div></header>
__HEAD__
__BLOCKS__
<table>
<thead><tr><th>Building</th><th class="n">Complaints</th><th class="n">DOB</th><th class="n">OATH/ECB</th></tr></thead>
<tbody>__ROWS__</tbody></table>
<footer>Large number is open records. Small number is everything on file.
Click a count to open that page on BIS. Checked hourly.</footer>
</div></body></html>"""
    return (
        html.replace("__CSS__", DASHBOARD_CSS)
        .replace("__WHEN__", esc(checked_at))
        .replace("__HEAD__", head)
        .replace("__BLOCKS__", "".join(blocks))
        .replace("__ROWS__", "".join(rows))
    )


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="check and print, send nothing")
    ap.add_argument("--seed", action="store_true", help="save current counts without alerting")
    args = ap.parse_args()

    buildings = json.loads(BUILDINGS_FILE.read_text())
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%b %d, %Y at %I:%M %p %Z")

    session = requests.Session()
    results, changes, new_state = [], [], {}

    for b in buildings:
        r = check_building(session, b)
        results.append(r)
        sid = "%s|%s|%s" % (b["boro"], b["houseno"], b["street"].lower())
        prev = state.get(sid, {})
        new_state[sid] = {}

        counts, moved = {}, False
        for sec in SECTIONS:
            s = r["sections"].get(sec, {})
            was = prev.get(sec)
            if s.get("error") or "open" not in s:
                # a failed fetch keeps the old number, so an outage never fakes a drop
                if was is not None:
                    new_state[sid][sec] = was
                    r["sections"].setdefault(sec, {})["open"] = was
                    r["sections"][sec].setdefault("total", was)
                    r["sections"][sec]["error"] = None
                continue
            new_state[sid][sec] = s["open"]
            counts[sec] = {"now": s["open"], "was": was}
            if was is not None and s["open"] != was:
                moved = True

        if moved and not args.seed:
            changes.append({"building": r["label"], "counts": counts, "url": r.get("profile_url")})

        print(
            "%-34s BIN %-9s %s"
            % (
                r["label"], r["bin"] or "-",
                "  ".join(
                    "%s %s open" % (SHORT_LABEL[s], r["sections"].get(s, {}).get("open", "?"))
                    for s in SECTIONS
                ),
            )
        )
        if r.get("error"):
            print("   error: %s" % r["error"])

    DASHBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_FILE.write_text(render_dashboard(results, changes, checked_at))
    print("dashboard written to %s" % DASHBOARD_FILE)

    if changes and not args.dry_run and not args.seed:
        body = build_alert_text(changes, checked_at)
        head = changes[0]
        first = next(
            (SHORT_LABEL[s] for s in SECTIONS
             if head["counts"].get(s) and head["counts"][s]["was"] is not None
             and head["counts"][s]["now"] != head["counts"][s]["was"]),
            "records",
        )
        subject = "DOB watch: %s open count moved at %s%s" % (
            first, head["building"],
            "" if len(changes) == 1 else " and %d more" % (len(changes) - 1),
        )
        send_email(subject, body)
        send_slack(subject + "\n```\n" + body + "\n```")
    elif changes:
        print("\n" + build_alert_text(changes, checked_at))
    else:
        print("no change in open counts")

    STATE_FILE.write_text(json.dumps(new_state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
