# DOB watch

Hourly check of NYC DOB BIS for Complaints, DOB Violations, and OATH/ECB Violations
on a list of buildings. Reports how many are open. Emails you when an open count
moves. Writes a one-page dashboard.

## What it does per building

1. Submits the Search by Property form: borough, house number, street.
2. Reads the BIN off the Property Profile Overview.
3. Opens the three record pages linked from that profile.
4. Counts the open records on each, and compares against the previous run.

You get three numbers per building: open complaints, open DOB violations, open
OATH/ECB violations. Nothing is sent unless one of those numbers moves.

## Files

    monitor.py            the checker
    buildings.json        your building list
    state.json            last snapshot, created on first run
    docs/index.html       the dashboard, rewritten every run
    requirements.txt      requests, beautifulsoup4
    .github/workflows/    hourly schedule

## Add buildings

Edit buildings.json. Borough codes: 1 Manhattan, 2 Bronx, 3 Brooklyn, 4 Queens,
5 Staten Island. Street goes in exactly as you type it in the BIS dropdown form.

    [
      { "label": "530 East 23rd Street", "boro": 1, "houseno": "530",
        "street": "east 23rd street" },
      { "label": "Kips Bay Court", "boro": 1, "houseno": "310",
        "street": "east 30th street" }
    ]

Optional per building: `"bin": "1083696"` to skip the address lookup.

Large complexes span several BINs. Add one entry per address you check by hand.

## First run

If you are running this on GitHub, do the first run from the Actions tab: pick the
DOB watch workflow, click Run workflow, and set mode to `seed`. No local install.

To run it on your own machine instead:

    pip install -r requirements.txt
    python monitor.py --seed

`--seed` records today's counts so your first real check has something to compare
against. Every run after that reports only movement.

    python monitor.py --dry-run    check and print, send nothing
    python monitor.py              check, alert, save

Open docs/index.html in a browser to see the dashboard.

## Run it hourly

### GitHub Actions, no server

Push this folder to a private GitHub repo. The workflow runs at the top of every
hour and commits state.json plus the dashboard back to the repo. Turn on Pages
(Settings, Pages, source: main branch, /docs) and the dashboard gets a URL you
open on your phone.

Add these under Settings, Secrets and variables, Actions:

    SMTP_HOST      smtp.gmail.com
    SMTP_PORT      465
    SMTP_USER      you@gmail.com
    SMTP_PASS      a Gmail app password, not your login password
    ALERT_TO       where alerts go
    SLACK_WEBHOOK  optional, an incoming webhook URL

Scheduled runs on GitHub queue under load, so expect the odd run 10 to 20 minutes
late. Nothing is missed, the next run catches it.

### Your own machine, cron

    0 * * * * cd /path/to/dob-monitor && /usr/bin/python3 monitor.py >> log.txt 2>&1

Windows Task Scheduler works the same way: hourly trigger, action `python monitor.py`.

## Notes

- BIS is the live system, so this catches records the day they post. NYC Open Data
  carries the same records through a Socrata API with no HTML parsing, but it
  refreshes daily, so hourly polling there buys you nothing.
- The script waits 2 seconds between requests. One building is 4 requests per hour.
  Keep the list under about 40 buildings and the load stays trivial.
- Open versus closed is read off the status wording on each row: CLOSED, RESOLVED,
  and DISMISSED count as closed, and a dismissed DOB violation number carries an
  asterisk (V*7052-18P). A row with no status wording either way counts as open, so
  the number errs high rather than missing something.
- Row parsing reads any table row with 3 or more cells and a date or record number
  in it. If BIS changes its markup, counts go to zero rather than throwing. Watch
  for a building showing 0 across all three columns when you know it has records.
- A failed fetch reuses the previous count, so a BIS outage never fires a false drop.
