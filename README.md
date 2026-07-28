# Concert Scout

A small Python 3.12 program that checks the official Ticketmaster Discovery API
each morning and emails only new concerts or meaningful changes. It uses no AI,
database, web scraping, server, or paid hosting.

## What it searches

Search locations, radii, priority artists, and genre terms are all editable in
`config.json`. The supplied configuration covers Kearney, Lincoln, Omaha, and
Denver and searches events from today through 12 months ahead.

Ticketmaster results are deduplicated by their stable event ID, filtered to
remove obvious non-concert/add-on/tribute listings, classified, sorted, and
compared with `data/seen_events.json`. The email is sent only for a new event or
a meaningful change to its URL, newly published price, venue, date/time, or
status. Ticketmaster's genre metadata is used to find similar established acts;
named music acts outside the watchlist remain eligible as discoveries.

Distances are approximate straight-line distances from Elm Creek, not driving
distances.

## One-time setup

1. Create a free Ticketmaster developer account and Discovery API key.
2. For the sending Gmail account, enable two-step verification and create an
   app password.
3. Create a new GitHub repository and push this folder:

   ```bash
   git init
   git add .
   git commit -m "Initial concert scout"
   git branch -M main
   git remote add origin YOUR_GITHUB_REPOSITORY_URL
   git push -u origin main
   ```

4. In GitHub, open **Settings → Secrets and variables → Actions** and add:
   `TICKETMASTER_API_KEY`, `ALERT_EMAIL_FROM`, `ALERT_EMAIL_TO`, and
   `GMAIL_APP_PASSWORD`.
5. Open **Actions → Concert Scout → Run workflow** for the first test.

The workflow has only `contents: write` permission, needed to commit its compact
state file. Its commit includes `[skip ci]`; scheduled workflows do not trigger
from pushes anyway, and concurrency prevents overlapping scouts.

## Schedule and daylight saving time

GitHub cron is UTC and cannot automatically follow Central daylight saving
time. The included `0 13 * * *` runs at approximately:

- 8:00 AM in Nebraska during daylight time (CDT)
- 7:00 AM in Nebraska during standard time (CST)

To favor 8:00 AM during standard time, change `13` to `14` (which then runs at
9:00 AM during daylight time). GitHub notes that scheduled jobs can be delayed
during high load, so the time is approximate.

## Local use

Python 3.12 is required. Runtime has no third-party dependencies.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install pytest
cp .env.example .env
```

Fill in `.env`, then export it in your shell before running (the program does
not parse `.env` itself):

```bash
set -a
source .env
set +a
python -m pytest -q
python concert_scout.py
```

No email is sent when nothing has changed. If a city request fails, the other
cities are still attempted, and secrets are never logged.

## Adding another official source later

`TicketmasterSource` owns all Ticketmaster-specific HTTP and pagination logic.
Add another source class with equivalent search methods, then merge its
normalized events before the existing filtering/state/email stages. Use only
official APIs or venue feeds that permit automated access.
