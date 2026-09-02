#!/usr/bin/env python3
"""
NYC DOB open-record monitor.

Reports three numbers per building: open complaints, open DOB violations,
open OATH/ECB violations. Emails you only when one of those numbers moves.

Two data sources:

  opendata (default)  NYC Open Data Socrata API. Works from any IP including
                      GitHub Actions. Refreshes daily.
  bis                 Scrapes a810-bisweb.nyc.gov. Live to the minute, but BIS
                      returns 403 to cloud IPs, so this only works from your own
                      machine or office network.

Run:  python monitor.py
      python monitor.py --source bis
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
DATA_FILE = ROOT / "docs" / "data.json"
PAGE_FILE = ROOT / "docs" / "index.html"
REPORT_FILE = ROOT / "docs" / "report.html"
HISTORY_CAP = 500   # snapshots kept in data.json

SECTIONS = ["complaints", "dob_violations", "ecb_violations"]
SECTION_LABEL = {
    "complaints": "Complaints",
    "dob_violations": "Violations - DOB",
    "ecb_violations": "Violations - OATH/ECB",
}
SHORT_LABEL = {"complaints": "Complaints", "dob_violations": "DOB", "ecb_violations": "OATH/ECB"}

TIMEOUT = 40


# ----------------------------------------------------------------------
# Source A: NYC Open Data (Socrata)
# ----------------------------------------------------------------------

SODA = "https://data.cityofnewyork.us/resource/%s.json"

# dataset id, status field, wording that means the record is still open
DATASETS = {
    "complaints":     ("eabe-havv", "status", "ACTIVE"),
    "dob_violations": ("3h2n-5cm9", "violation_category", "ACTIVE"),
    "ecb_violations": ("6bgk-3dad", "ecb_violation_status", "ACTIVE"),
}

BIS_LINKS = {
    "complaints": "https://a810-bisweb.nyc.gov/bisweb/ComplaintsByAddressServlet"
                  "?requestid=1&allbin=%s",
    "dob_violations": "https://a810-bisweb.nyc.gov/bisweb/ActionsByLocationServlet"
                      "?requestid=1&allbin=%s&allinquirytype=BXS4OCV3&stypeocv3=V",
    "ecb_violations": "https://a810-bisweb.nyc.gov/bisweb/ECBQueryByLocationServlet"
                      "?requestid=1&allbin=%s",
}


def soda(session, dataset, params):
    headers = {}
    token = os.environ.get("SOCRATA_TOKEN")
    if token:
        headers["X-App-Token"] = token
    last = None
    for i in range(3):
        try:
            r = session.get(SODA % dataset, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(6)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(3 * (i + 1))
    raise RuntimeError("Open Data request failed: %s (%s)" % (dataset, last))


def soda_count(session, dataset, where):
    rows = soda(session, dataset, {"$select": "count(1) as n", "$where": where})
    return int(rows[0]["n"]) if rows else 0


def count_opendata(session, bin_number):
    """Return {section: {'open': n, 'total': n, 'url': ...}} for one BIN."""
    b = str(bin_number).strip()
    out = {}
    for sec, (dataset, field, open_word) in DATASETS.items():
        entry = {"url": BIS_LINKS[sec] % b}
        try:
            # bin is text in these datasets; fall back to numeric if that returns nothing
            where = "bin='%s'" % b
            total = soda_count(session, dataset, where)
            if total == 0:
                alt = "bin=%s" % b
                if soda_count(session, dataset, alt) > 0:
                    where = alt
                    total = soda_count(session, dataset, where)
            opened = soda_count(
                session, dataset,
                "%s AND upper(%s) like '%%%s%%'" % (where, field, open_word),
            )
            entry.update({"open": opened, "total": total, "error": None})
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:160]
        out[sec] = entry
    return out


SHOW_FIELDS = {
    "complaints": ["complaint_number", "date_entered", "status", "complaint_category",
                   "disposition_date", "disposition_code", "inspection_date"],
    "dob_violations": ["number", "violation_number", "issue_date", "violation_category",
                       "violation_type", "disposition_date", "disposition_comments"],
    "ecb_violations": ["ecb_violation_number", "issue_date", "ecb_violation_status",
                       "severity", "hearing_status", "violation_type"],
}


def explain(session, bin_number, label):
    """Print the records behind each open count so you can compare against BIS."""
    b = str(bin_number).strip()
    print("\n=== %s  BIN %s ===" % (label, b))
    for sec, (dataset, field, open_word) in DATASETS.items():
        where = "bin='%s' AND upper(%s) like '%%%s%%'" % (b, field, open_word)
        print("\n%s  counted open  [%s]" % (SECTION_LABEL[sec], dataset))
        try:
            rows = soda(session, dataset, {"$where": where, "$limit": 25})
        except Exception as exc:  # noqa: BLE001
            print("  query failed: %s" % exc)
            continue
        if not rows:
            print("  none")
            continue
        for row in rows:
            bits = ["%s=%s" % (k, row[k]) for k in SHOW_FIELDS[sec] if row.get(k)]
            extra = [k for k in row if k not in SHOW_FIELDS[sec]
                     and k in ("bin", "house_number", "street", "boro", "block", "lot")]
            bits += ["%s=%s" % (k, row[k]) for k in extra]
            print("  " + "  ".join(bits))
        if len(rows) == 25:
            print("  (first 25 shown)")


def resolve_bin(session, building):
    """Look up a BIN from house number + street using the DOB Violations dataset."""
    house = str(building["houseno"]).strip().upper()
    street = str(building["street"]).strip().upper()
    where = (
        "upper(house_number)='%s' AND upper(street) like '%%%s%%' AND boro='%s'"
        % (house.replace("'", ""), street.replace("'", ""), building["boro"])
    )
    rows = soda(session, "3h2n-5cm9", {"$select": "bin", "$where": where, "$limit": 1})
    return rows[0]["bin"] if rows else None


# ----------------------------------------------------------------------
# Source B: BIS scrape (local networks only)
# ----------------------------------------------------------------------

BIS_BASE = "https://a810-bisweb.nyc.gov/bisweb/"
PROFILE_URL = BIS_BASE + "PropertyProfileOverviewServlet"
BIS_HOME = BIS_BASE + "bispi00.jsp"

BIS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
              "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}

BIS_PAUSE = 2.0


def bis_fetch(session, url, params=None, referer=BIS_HOME, attempts=3):
    headers = dict(BIS_HEADERS)
    headers["Referer"] = referer
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if r.status_code == 403:
                raise RuntimeError(
                    "403 Forbidden. BIS blocks datacenter IPs. Run with the default "
                    "--source opendata, or run this script from your own machine."
                )
            r.raise_for_status()
            time.sleep(BIS_PAUSE)
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if "403" in str(exc):
                break
            time.sleep(4 * (i + 1))
    raise RuntimeError("BIS request failed: %s (%s)" % (url, last))


NOISE = re.compile(r"(date of this report|requestid|©|copyright|privacy policy|back to)", re.I)
HAS_RECORD = re.compile(r"\d{2}/\d{2}/\d{4}|\d{6,}|\b[A-Z]?\d{5,}\b")
CLOSED = re.compile(
    r"\b(CLOSED|RESOLVE[DS]?|DISMISS(ED)?|CURED|COMPLIED|WRITTEN OFF|PAID IN FULL)\b", re.I
)
OPEN_WORD = re.compile(r"\b(ACTIVE|OPEN|IN VIOLATION|DEFAULTED|OUTSTANDING)\b", re.I)
DISMISSED_NUMBER = re.compile(r"^[A-Z]\s*\*")


def count_records(html):
    soup = BeautifulSoup(html, "html.parser")
    total = open_count = 0
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
            open_count += 1  # no status wording, count it open rather than miss it
    return open_count, total


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
        low = a["href"].lower()
        target = urljoin(base_url, a["href"])
        if ("ecbquerybylocation" in low or "ecb" in text or "oath" in text) \
                and "ecb_violations" not in found:
            found["ecb_violations"] = target
        elif "actionsbylocation" in low and "stypeocv3=v" in low \
                and "dob_violations" not in found:
            found["dob_violations"] = target
        elif "complaint" in low and "complaint" in text and "complaints" not in found:
            found["complaints"] = target
    return found


def count_bis(session, building):
    params = {
        "boro": str(building["boro"]), "houseno": str(building["houseno"]),
        "street": building["street"], "go2": " GO ", "requestid": "0",
    }
    r = bis_fetch(session, PROFILE_URL, params=params)
    html, url = r.text, r.url
    bin_number = building.get("bin") or find_bin(html)
    links = find_section_links(html, url)
    if bin_number:
        for sec, tmpl in BIS_LINKS.items():
            links.setdefault(sec, tmpl % bin_number)

    out = {}
    for sec in SECTIONS:
        link = links.get(sec)
        if not link:
            out[sec] = {"url": None, "error": "link not found"}
            continue
        try:
            page = bis_fetch(session, link, referer=url)
            opened, total = count_records(page.text)
            out[sec] = {"url": link, "open": opened, "total": total, "error": None}
        except Exception as exc:  # noqa: BLE001
            out[sec] = {"url": link, "error": str(exc)[:160]}
    return bin_number, url, out


# ----------------------------------------------------------------------
# Check one building
# ----------------------------------------------------------------------

def check_building(session, building, source):
    result = {
        "label": building.get("label") or "%s %s" % (building["houseno"], building["street"]),
        "bin": building.get("bin"),
        "profile_url": None,
        "sections": {},
        "error": None,
    }
    try:
        if source == "bis":
            bin_number, url, sections = count_bis(session, building)
            result.update({"bin": bin_number, "profile_url": url, "sections": sections})
        else:
            if not result["bin"]:
                result["bin"] = resolve_bin(session, building)
                if result["bin"]:
                    print("   resolved BIN %s, add it to buildings.json" % result["bin"])
            if not result["bin"]:
                raise RuntimeError("no BIN. Add \"bin\": \"1234567\" to this building.")
            result["sections"] = count_opendata(session, result["bin"])
            result["profile_url"] = (
                "https://a810-bisweb.nyc.gov/bisweb/PropertyProfileOverviewServlet?"
                + urlencode({
                    "boro": building["boro"], "houseno": building["houseno"],
                    "street": building["street"], "go2": " GO ", "requestid": "0",
                })
            )
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)[:200]
    return result


# ----------------------------------------------------------------------
# Alerts
# ----------------------------------------------------------------------

def build_alert_text(changes, checked_at, source):
    lines = ["NYC DOB open-record check - %s (%s)" % (checked_at, source), ""]
    for c in changes:
        lines.append(c["building"])
        for sec in SECTIONS:
            d = c["counts"].get(sec)
            if not d:
                continue
            if d["was"] is None:
                lines.append("  %-22s %4d open" % (SECTION_LABEL[sec], d["now"]))
            elif d["now"] != d["was"]:
                lines.append("  %-22s %4d open   was %d   %+d"
                             % (SECTION_LABEL[sec], d["now"], d["was"], d["now"] - d["was"]))
            else:
                lines.append("  %-22s %4d open   no change" % (SECTION_LABEL[sec], d["now"]))
        if c.get("url"):
            lines.append("  " + c["url"])
        lines.append("")
    return "\n".join(lines)


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
    server = (smtplib.SMTP_SSL(host, port, timeout=30) if port == 465
              else smtplib.SMTP(host, port, timeout=30))
    with server:
        if port != 465:
            server.starttls()
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

def write_data(results, checked_at, source, sids):
    """Publish docs/data.json for the web page. Keeps a rolling history."""
    old = {}
    if DATA_FILE.exists():
        try:
            old = json.loads(DATA_FILE.read_text())
        except Exception:  # noqa: BLE001
            old = {}
    history = old.get("history", [])
    if not isinstance(history, list):
        history = []

    buildings, snapshot = [], {}
    for r, sid in zip(results, sids):
        secs = {}
        for sec in SECTIONS:
            s = r["sections"].get(sec, {})
            secs[sec] = {
                "open": s["open"] if isinstance(s.get("open"), int) else None,
                "total": s.get("total"),
                "url": s.get("url"),
                "error": s.get("error"),
            }
        buildings.append({
            "id": sid,
            "label": r["label"],
            "bin": r["bin"],
            "profile_url": r["profile_url"],
            "error": r["error"],
            "sections": secs,
        })
        snapshot[sid] = {sec: secs[sec]["open"] for sec in SECTIONS}

    history.append({"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "counts": snapshot})
    history = history[-HISTORY_CAP:]

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps({
        "checked_at": checked_at,
        "source": source,
        "sections": SECTIONS,
        "labels": SHORT_LABEL,
        "buildings": buildings,
        "history": history,
    }, indent=1))
    print("data written to %s (%d snapshots)" % (DATA_FILE, len(history)))

    # A single self-contained file with the data baked in. Download it from the
    # repo and open it anywhere, no web server and no GitHub Pages needed.
    if PAGE_FILE.exists():
        page = PAGE_FILE.read_text()
        marker = "let EMBEDDED = null;"
        if marker in page:
            payload = json.dumps(json.loads(DATA_FILE.read_text()))
            REPORT_FILE.write_text(page.replace(marker, "let EMBEDDED = " + payload + ";"))
            print("self-contained page written to %s" % REPORT_FILE)
        else:
            print("skipped report.html: docs/index.html is an older version "
                  "with no '%s' line. Replace it to get the offline page." % marker)
    else:
        print("skipped report.html: docs/index.html not found in the repo")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def load_state():
    """Read state.json, discarding anything that is not a plain integer count."""
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}
    clean = {}
    for sid, secs in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(secs, dict):
            continue
        keep = {k: v for k, v in secs.items() if k in SECTIONS and isinstance(v, int)}
        if keep:
            clean[sid] = keep
    if clean != raw:
        print("state.json had entries from an older version, ignoring those")
    return clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["opendata", "bis"],
                    default=os.environ.get("SOURCE", "opendata"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--explain", action="store_true",
                    help="list the records behind each open count, then stop")
    args = ap.parse_args()

    buildings = json.loads(BUILDINGS_FILE.read_text())
    state = load_state()
    checked_at = datetime.now(timezone.utc).astimezone().strftime("%b %d, %Y at %I:%M %p %Z")

    session = requests.Session()

    if args.explain:
        for b in buildings:
            bin_number = b.get("bin") or resolve_bin(session, b)
            if not bin_number:
                print("\n=== %s === no BIN found" % b.get("label", b["houseno"]))
                continue
            explain(session, bin_number,
                    b.get("label") or "%s %s" % (b["houseno"], b["street"]))
        return 0

    results, changes, new_state, sids = [], [], {}, []

    for b in buildings:
        r = check_building(session, b, args.source)
        results.append(r)
        sid = "%s|%s|%s" % (b["boro"], b["houseno"], str(b["street"]).lower())
        sids.append(sid)
        prev = state.get(sid, {})
        new_state[sid] = {}

        counts, moved = {}, False
        for sec in SECTIONS:
            s = r["sections"].get(sec, {})
            was = prev.get(sec)
            if not isinstance(s.get("open"), int):
                if isinstance(was, int):
                    # a failed lookup keeps the old number, so an outage never fakes a drop
                    new_state[sid][sec] = was
                continue
            new_state[sid][sec] = s["open"]
            counts[sec] = {"now": s["open"], "was": was if isinstance(was, int) else None}
            if isinstance(was, int) and s["open"] != was:
                moved = True

        if moved and not args.seed:
            changes.append({"building": r["label"], "counts": counts,
                            "url": r.get("profile_url")})

        print("%-32s BIN %-9s %s" % (
            r["label"], r["bin"] or "-",
            "  ".join("%s %s" % (SHORT_LABEL[s], r["sections"].get(s, {}).get("open", "?"))
                      for s in SECTIONS)))
        if r.get("error"):
            print("   error: %s" % r["error"])
        for sec in SECTIONS:
            e = r["sections"].get(sec, {}).get("error")
            if e:
                print("   %s: %s" % (SHORT_LABEL[sec], e))

    write_data(results, checked_at, args.source, sids)

    if changes and not args.dry_run and not args.seed:
        body = build_alert_text(changes, checked_at, args.source)
        head = changes[0]
        first = next((SHORT_LABEL[s] for s in SECTIONS
                      if head["counts"].get(s) and head["counts"][s]["was"] is not None
                      and head["counts"][s]["now"] != head["counts"][s]["was"]), "records")
        subject = "DOB watch: %s open count moved at %s%s" % (
            first, head["building"],
            "" if len(changes) == 1 else " and %d more" % (len(changes) - 1))
        send_email(subject, body)
        send_slack(subject + "\n```\n" + body + "\n```")
    elif changes:
        print("\n" + build_alert_text(changes, checked_at, args.source))
    else:
        print("no change in open counts")

    STATE_FILE.write_text(json.dumps(new_state, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
