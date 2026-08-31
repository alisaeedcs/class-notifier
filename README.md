# ucla-soc-watcher

Watches UCLA Schedule of Classes sections for status changes (e.g. Closed -> Open)
and emails you when one changes. Runs for free on a GitHub Actions schedule.

## How it works

- `soc_client.py` — talks to UCLA's public `GetCourseSummary` endpoint directly
  (reverse-engineered from the SOC search UI's own XHR call) and parses the
  section table (status, waitlist, day/time, instructor, etc). Also usable as a
  standalone CLI:

  ```
  python3 soc_client.py STATS 102C 26F
  ```

- `run_check.py` — reads `watch_config.json`, fetches current status for each
  watched course, diffs against `state.json` (committed to the repo as the
  persisted "last known state"), and emails you a summary if anything changed.
  On the very first run it just records a baseline (no email).

- `.github/workflows/watch.yml` — runs `run_check.py` on a schedule (every 15
  min) and commits the updated `state.json` back to the repo.

- UCLA's own SOC page states seat/status data is only refreshed **once per
  hour** server-side, so 15-minute polling is just a safety margin, not an
  attempt at real-time tracking.

- UCLA serves the schedule from several servers whose snapshots can differ by
  hours, and the summary table carries no timestamp saying which snapshot you
  got. A class sitting right on a seat boundary (e.g. 39 of 40 taken) therefore
  looks like it flaps open/closed forever, one email per poll.

  So a change is never emailed on the summary table alone. When the summary
  reports one, `run_check.py` confirms it against that section's *detail* page,
  which reports the seat status **and** the time that status was refreshed in
  the same response — a per-section timestamp, not the page-wide "Status as of"
  banner, which comes from a different request and doesn't describe the seat
  data. The change is only accepted if the detail page agrees the seats really
  moved and its timestamp is no older than the last one accepted for that
  section; otherwise the old value is kept and it's rechecked next run.

  That extra fetch only happens when a change is seen, so a quiet run still
  costs one request per course. Accepted timestamps are recorded per section in
  `state.json` as `_as_of` / `_as_of_epoch`.

## Setup

1. Push this repo to GitHub (can be public — nothing sensitive is stored,
   just course codes and status strings. Public repos also get unlimited free
   GitHub Actions minutes).

2. Edit `watch_config.json` to list the courses you want watched:

   ```json
   {
     "watches": [
       { "name": "STATS 102C", "term": "26F", "subject": "STATS", "catalog": "102C", "sections": null }
     ]
   }
   ```

   `sections`: `null` watches every section; or restrict to specific ones,
   e.g. `["Lec 1", "Lec 3"]`.

3. In the GitHub repo settings, add these Actions secrets
   (Settings -> Secrets and variables -> Actions -> New repository secret):

   - `SMTP_HOST` — e.g. `smtp.gmail.com`
   - `SMTP_PORT` — e.g. `587`
   - `SMTP_USER` — your email address
   - `SMTP_PASS` — an app password (for Gmail: Google Account -> Security ->
     2-Step Verification -> App passwords; a normal password won't work)
   - `NOTIFY_EMAIL_TO` — where to send alerts (can be the same address, or an
     SMS gateway address like `1234567890@vtext.com` for a text message)

4. The workflow runs automatically every 15 minutes. You can also trigger it
   manually from the Actions tab ("Run workflow").

## Local testing

```
pip install -r requirements.txt
python3 run_check.py
```

Requires `SMTP_*`/`NOTIFY_EMAIL_TO` environment variables to be set locally
only if a change is actually detected (first run never sends email).
