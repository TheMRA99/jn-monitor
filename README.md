# SG + Johor ticket monitor

Runs free on GitHub Actions every 15 min. Watches a list of movies across five
cinema sites and **emails whenever a new cinema or a new showtime opens** for any
of them — repeating every run until you stop watching that movie.

## Sites & coverage

| Site | Region | Granularity |
|------|--------|-------------|
| Shaw Theatres | 🇸🇬 SG, all cinemas | bookable (first-open) |
| Golden Village | 🇸🇬 SG, all cinemas | bookable (first-open) |
| myCinemas | 🇸🇬 SG | per showtime |
| TGV | 🇲🇾 Johor only | per cinema + showtime |
| GSC | 🇲🇾 Johor only | per cinema + showtime |

Malaysia sites are filtered to Johor cinemas only (TGV: Bukit Indah, Kulaijaya,
Tasek Central, Tebrau City, Toppen; GSC: all Johor-Bahru-area cinemas).

## Configure

Edit the top of `monitor.py`:

- `MOVIES` — the titles to watch. Loose matching handles `(Tamil)` suffixes,
  `Spider-Man` vs `Spider Man`, sequel numbers, etc.
- `STOPPED` — move a title here (or delete it from `MOVIES`) to stop its emails.

## Setup (one-time)

1. **Sender email**: a Gmail with an [App Password](https://myaccount.google.com/apppasswords)
   (needs 2-Step Verification).
2. Push this repo (private).
3. **Secrets** → repo Settings → Secrets and variables → Actions:
   - `SMTP_USER` — sender email
   - `SMTP_PASS` — app password
   - `ALERT_TO` — recipient email
   - `SMTP_HOST` / `SMTP_PORT` — optional (default Gmail)
4. **Test**: Actions → run workflow with `test_email = true` to get a test email.

## How it works

Each run collects the set of currently-open slots (movie × site × cinema × date ×
time), diffs against `state.json` (the slots already emailed), and emails only the
new ones. `state.json` is committed back by the workflow so memory persists.

- Detection uses each site's own JSON/XML backend (their HTML pages are empty JS
  shells). No scraping of rendered pages.
- A site failing (e.g. rate-limited) is logged and skipped; the others still run.
