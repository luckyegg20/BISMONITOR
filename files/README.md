# DOB watch

Tracks open DOB records for a list of NYC buildings and emails you when a count
moves. Three numbers per building: open complaints, open DOB violations, open
OATH/ECB violations. Silent otherwise.

## Two data sources

`opendata` (default) reads the NYC Open Data API. Works from any network,
including GitHub Actions. Refreshes daily.

`bis` scrapes a810-bisweb.nyc.gov, which is live to the minute. BIS returns 403
to datacenter IP ranges, so this only runs from your own machine or office
network, never from GitHub Actions.

    python monitor.py                 opendata
    python monitor.py --source bis    local only

Either way the dashboard and the emails link straight back to the BIS pages, so
you click through to the same screens you check by hand.

## Files

    monitor.py            the checker
    buildings.json        your building list
    state.json            last counts, created on first run
    docs/index.html       the dashboard, rewritten every run
    requirements.txt      requests, beautifulsoup4
    .github/workflows/    hourly schedule

## Add buildings

Borough codes: 1 Manhattan, 2 Bronx, 3 Brooklyn, 4 Queens, 5 Staten Island.

    [
      { "label": "530 East 23rd Street", "boro": 1, "houseno": "530",
        "street": "east 23rd street", "bin": "1083696" },
      { "label": "7 Peter Cooper Road", "boro": 1, "houseno": "7",
        "street": "peter cooper road" }
    ]

Include `bin` whenever you know it. The Open Data source is keyed on BIN, and
supplying it removes every address-matching guess. Grab it off the BIS property
profile page you already open. Without it the script looks the BIN up by address
and prints what it found so you can paste it in.

Large complexes span several BINs. Add one entry per address you check by hand.

## First run

On GitHub: Actions tab, DOB watch, Run workflow, set mode to `seed`.

On your own machine:

    pip install -r requirements.txt
    python monitor.py --seed

`--seed` records today's counts so the first real check has something to compare
against. Every run after that reports only movement.

    python monitor.py --dry-run    check and print, send nothing
    python monitor.py              check, alert, save

## Run it hourly

### GitHub Actions

Push this folder to a private repo. The workflow runs hourly and commits
state.json and the dashboard back. Secrets under Settings, Secrets and variables,
Actions:

    SMTP_HOST      smtp.gmail.com
    SMTP_PORT      465
    SMTP_USER      you@gmail.com
    SMTP_PASS      a Gmail app password, not your login password
    ALERT_TO       where alerts go
    SLACK_WEBHOOK  optional
    SOCRATA_TOKEN  optional, raises the API rate limit

### Your own machine, cron

    0 * * * * cd /path/to/dob-monitor && /usr/bin/python3 monitor.py --source bis >> log.txt 2>&1

Windows Task Scheduler works the same way.

## Notes

- Open versus closed comes from the status field: complaints are open when ACTIVE,
  DOB violations when the category reads ACTIVE rather than DISMISSED, ECB
  violations when the status reads ACTIVE rather than RESOLVE.
- In `bis` mode a row with no status wording either way counts as open, so the
  number errs high rather than missing something.
- A failed lookup reuses the previous count, so an outage never fires a false drop.
- state.json from an older version is ignored rather than crashing the run. You
  will see one line saying so, then a clean seed.
- Open Data is free and needs no key. A token only raises the hourly request cap.
