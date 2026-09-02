#!/usr/bin/env python3
"""
NYC DOB BIS monitor.

For each building in buildings.json:
  1. Looks it up on BIS by borough + house number + street (same as the manual search).
  2. Opens Complaints, Violations-DOB, and Violations-OATH/ECB.
  3. Compares every record row against the previous run.
  4. Reports new records, status changes, and removed records.
  5. Writes docs/index.html (the dashboard) and sends email / Slack alerts.

Run:  python monitor.py
Test: python monitor.py --dry-run      (no alerts sent)
      python monitor.py --seed         (record current state, alert on nothing)
"""

import argparse
import hashlib
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

REQUEST_PAUSE = 2.0  # seconds between BIS requests, keeps the load light
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
    """Run the Search by Property form and return (html, final_url)."""
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
    """Locate the Complaints / DOB Violations / ECB Violations links on the profile page."""
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        text = " ".join(a.get_text(" ", strip=True).split()).lower()
        href = a["href"]
        target = urljoin(base_url, href)
        low_href = href.lower()

        is_ecb = "ecbquerybylocation" in low_href or "ecb" in text or "oath" in text
        is_dob_viol = "actionsbylocation" in low_href and "stypeocv3=v" in low_href
        is_complaint = "complaint" in low_href or "complaint" in text

        if is_ecb and "ecb_violations" not in found:
            found["ecb_violations"] = target
        elif is_dob_viol and "dob_violations" not in found:
            found["dob_violations"] = target
        elif is_complaint and "complaint" in text and "complaints" not in found:
            found["complaints"] = target
    return found


def fallback_links(bin_number):
    """Direct URLs, used when the profile page markup changes."""
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
# Row extraction and diffing
# ----------------------------------------------------------------------

NOISE = re.compile(r"(date of this report|requestid|©|copyright|privacy policy|back to)", re.I)
HAS_RECORD = re.compile(r"\d{2}/\d{2}/\d{4}|\d{6,}|\b[A-Z]?\d{5,}\b")


def extract_rows(html):
    """
    Pull record rows out of a BIS results table.

    BIS uses nested tables with no useful classes, so any <tr> with 3+ cells and
    something record-shaped in it (a date or a long number) counts as a record.
    Returns {key: row_text}.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = {}
    for tr in soup.find_all("tr"):
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        if len(cells) < 3:
            continue
        line = " | ".join(cells)
        if NOISE.search(line) or not HAS_RECORD.search(line):
            continue
        key = cells[0] if len(cells[0]) >= 4 else hashlib.sha1(line.encode()).hexdigest()[:12]
        # collision guard when a record number repeats
        base, n = key, 1
        while key in rows and rows[key] != line:
            n += 1
            key = "%s#%d" % (base, n)
        rows[key] = line
    return rows


def diff_rows(old, new):
    added = [{"key": k, "text": v} for k, v in new.items() if k not in old]
    removed = [{"key": k, "text": v} for k, v in old.items() if k not in new]
    changed = [
        {"key": k, "before": old[k], "after": new[k]}
        for k in new
        if k in old and old[k] != new[k]
    ]
    return {"added": added, "removed": removed, "changed": changed}


# ----------------------------------------------------------------------
# Check one building
# ----------------------------------------------------------------------

def check_building(session, building):
    result = {
        "label": building.get("label") or "%s %s" % (building["houseno"], building["street"]),
        "boro": building["boro"],
        "houseno": building["houseno"],
        "street": building["street"],
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
            for name, fallback in fallback_links(bin_number).items():
                links.setdefault(name, fallback)

        for name in SECTIONS:
            link = links.get(name)
            if not link:
                result["sections"][name] = {"url": None, "rows": {}, "error": "link not found"}
                continue
            try:
                page = fetch(session, link)
                result["sections"][name] = {
                    "url": link,
                    "rows": extract_rows(page.text),
                    "error": None,
                }
            except Exception as exc:  # noqa: BLE001
                result["sections"][name] = {"url": link, "rows": {}, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


# ----------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------

def build_alert_text(changes, checked_at):
    lines = ["NYC DOB BIS check - %s" % checked_at, ""]
    for item in changes:
        lines.append(item["building"])
        for sec, d in item["by_section"].items():
            for row in d["added"]:
                lines.append("  NEW  [%s] %s" % (SECTION_LABEL[sec], row["text"]))
            for row in d["changed"]:
                lines.append("  CHG  [%s] %s" % (SECTION_LABEL[sec], row["after"]))
                lines.append("       was: %s" % row["before"])
            for row in d["removed"]:
                lines.append("  GONE [%s] %s" % (SECTION_LABEL[sec], row["text"]))
        if item.get("urls"):
            lines.append("  " + item["urls"])
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
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


DASHBOARD_CSS = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;background:#101418;color:#E8E6E1;
 font:16px/1.5 "Helvetica Neue",Helvetica,Arial,sans-serif;
 font-variant-numeric:tabular-nums}
.wrap{max-width:1040px;margin:0 auto;padding:32px 20px 80px}
header{border-bottom:2px solid #E8E6E1;padding-bottom:14px;margin-bottom:28px;
 display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between}
h1{margin:0;font-size:22px;font-weight:600;letter-spacing:-.01em}
.stamp{font-size:13px;color:#8A9199}
.headline{font-size:clamp(28px,5vw,42px);line-height:1.15;font-weight:600;
 letter-spacing:-.02em;margin:0 0 28px;max-width:20ch}
.headline.quiet{color:#8A9199;font-weight:400}
.change{border-left:3px solid #E2564D;background:#171C21;padding:14px 16px;margin-bottom:10px}
.change .who{font-weight:600;margin-bottom:6px}
.change .kind{display:inline-block;min-width:52px;font-size:12px;font-weight:600;
 letter-spacing:.04em;color:#101418;background:#E2564D;padding:1px 6px;margin-right:8px}
.change .kind.chg{background:#D9A441}
.change .kind.gone{background:#5E7F6B;color:#E8E6E1}
.change .row{font-size:14px;padding:4px 0;border-top:1px solid #232A31;
 word-break:break-word;color:#C9C6C0}
.change .was{font-size:13px;color:#7C838A;padding-left:60px}
table{width:100%;border-collapse:collapse;margin-top:14px}
th{text-align:left;font-size:12px;font-weight:600;color:#8A9199;
 padding:0 10px 8px 0;border-bottom:1px solid #2B333A}
th.n,td.n{text-align:right;padding-right:14px}
td{padding:11px 10px 11px 0;border-bottom:1px solid #1D2429;font-size:15px;vertical-align:top}
td a{color:#E8E6E1;text-decoration:none;border-bottom:1px solid #3C444B}
td a:hover,td a:focus{border-bottom-color:#E8E6E1}
td.n{font-weight:600}
td.n.hot{color:#E2564D}
.sub{display:block;font-size:12px;color:#7C838A;margin-top:3px}
.err{color:#D9A441;font-size:13px}
footer{margin-top:40px;font-size:13px;color:#6C737A;line-height:1.7}
a:focus-visible{outline:2px solid #D9A441;outline-offset:3px}
@media(max-width:620px){th.n,td.n{padding-right:6px}td{font-size:14px}}
"""


def render_dashboard(results, changes, checked_at):
    if changes:
        n = sum(
            len(d["added"]) + len(d["changed"]) + len(d["removed"])
            for c in changes
            for d in c["by_section"].values()
        )
        head = '<p class="headline">%d record%s changed since the last check.</p>' % (
            n,
            "" if n == 1 else "s",
        )
    else:
        head = '<p class="headline quiet">No change since the last check.</p>'

    blocks = []
    for item in changes:
        parts = ['<div class="change"><div class="who">%s</div>' % esc(item["building"])]
        for sec, d in item["by_section"].items():
            for row in d["added"]:
                parts.append(
                    '<div class="row"><span class="kind">NEW</span>%s: %s</div>'
                    % (esc(SECTION_LABEL[sec]), esc(row["text"]))
                )
            for row in d["changed"]:
                parts.append(
                    '<div class="row"><span class="kind chg">CHANGED</span>%s: %s</div>'
                    '<div class="was">was: %s</div>'
                    % (esc(SECTION_LABEL[sec]), esc(row["after"]), esc(row["before"]))
                )
            for row in d["removed"]:
                parts.append(
                    '<div class="row"><span class="kind gone">CLEARED</span>%s: %s</div>'
                    % (esc(SECTION_LABEL[sec]), esc(row["text"]))
                )
        parts.append("</div>")
        blocks.append("".join(parts))

    rows = []
    for r in results:
        counts = []
        for sec in SECTIONS:
            s = r["sections"].get(sec, {})
            if s.get("error"):
                counts.append('<td class="n err">!</td>')
            else:
                c = len(s.get("rows", {}))
                cls = "n hot" if c else "n"
                cell = str(c)
                if s.get("url"):
                    cell = '<a href="%s">%s</a>' % (esc(s["url"]), c)
                counts.append('<td class="%s">%s</td>' % (cls, cell))
        name = esc(r["label"])
        if r.get("profile_url"):
            name = '<a href="%s">%s</a>' % (esc(r["profile_url"]), name)
        sub = "BIN %s" % esc(r["bin"]) if r.get("bin") else '<span class="err">not found on BIS</span>'
        if r.get("error"):
            sub = '<span class="err">%s</span>' % esc(r["error"][:120])
        rows.append(
            "<tr><td>%s<span class=\"sub\">%s</span></td>%s</tr>"
            % (name, sub, "".join(counts))
        )

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
<footer>Counts are open records on file at the Buildings Information System.
Click a count to open that BIS page. Checked hourly.</footer>
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
    ap.add_argument("--seed", action="store_true", help="save current state without alerting")
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

        by_section = {}
        for sec in SECTIONS:
            rows = r["sections"].get(sec, {}).get("rows", {})
            err = r["sections"].get(sec, {}).get("error")
            if err:
                # keep the old snapshot so a fetch failure never fakes a "cleared" alert
                new_state[sid][sec] = prev.get(sec, {})
                continue
            new_state[sid][sec] = rows
            d = diff_rows(prev.get(sec, {}), rows)
            if d["added"] or d["changed"] or d["removed"]:
                by_section[sec] = d

        if by_section and prev and not args.seed:
            changes.append(
                {
                    "building": r["label"],
                    "by_section": by_section,
                    "urls": r.get("profile_url") or "",
                }
            )
        print(
            "%-38s BIN %-9s %s"
            % (
                r["label"],
                r["bin"] or "-",
                " ".join(
                    "%s=%d" % (s[:3], len(r["sections"].get(s, {}).get("rows", {})))
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
        count = sum(
            len(d["added"]) + len(d["changed"]) + len(d["removed"])
            for c in changes
            for d in c["by_section"].values()
        )
        subject = "DOB watch: %d change%s (%s)" % (
            count,
            "" if count == 1 else "s",
            ", ".join(c["building"] for c in changes)[:80],
        )
        send_email(subject, body)
        send_slack(subject + "\n```\n" + body + "\n```")
    elif changes:
        print("\n" + build_alert_text(changes, checked_at))
    else:
        print("no changes")

    STATE_FILE.write_text(json.dumps(new_state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
