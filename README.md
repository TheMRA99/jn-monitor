# Jana Nayagan SG ticket monitor

Runs free on GitHub Actions every 15 min. When Shaw or GV shows booking open,
it emails your blast text, then stops alerting.

## Setup (10 min, one-time)

1. **Sender email**: use a Gmail (or other SMTP) account. For Gmail, create
   an [App Password](https://myaccount.google.com/apppasswords) (needs
   2-Step Verification enabled) — do NOT use your normal account password.
2. **Repo**: create a private repo (e.g. `themra99/jn-monitor`), push these
   three files.
3. **Secrets**: repo → Settings → Secrets and variables → Actions → add:
   - `SMTP_USER` — sender email address
   - `SMTP_PASS` — app password
   - `ALERT_TO` — recipient email (defaults to `SMTP_USER` if omitted)
   - `SMTP_HOST` / `SMTP_PORT` — optional, default to Gmail
     (`smtp.gmail.com` / `587`)
4. **Test**: Actions tab → jana-nayagan-monitor → Run workflow. Logs should
   show `open=False` per site (or an alert if it's already live).

## Notes

- Detection is heuristic (showtime patterns / "book now" / POSTPONED flag
  gone). If a site blocks the scraper, the run logs the error and continues
  with the other site.
- Delete the repo or disable the workflow after you've booked.

## Shaw + GV specifics

- **Shaw**: watches the movie page directly (`shaw.sg/movie-details/1624`);
  alerts when POSTPONED is gone and booking indicators appear.
- **GV**: their site is API-driven — the script checks GV's nowshowing /
  advance-sales feeds for "Jana Nayagan", with the Buy Tickets page as
  fallback. Endpoints are best-effort (untested from sandbox): your first
  manual "Run workflow" will show in the logs whether GV responds; if it
  errors, Shaw detection still covers you since both open around the same
  time here.
