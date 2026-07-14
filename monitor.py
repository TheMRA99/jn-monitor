"""Jana Nayagan SG ticket-availability monitor — Shaw + Golden Village.

Runs via GitHub Actions. Emails once when booking looks open.

Both sites are JavaScript SPAs, so scraping their HTML pages is useless
(the page shells contain no movie data). This monitor instead calls the
same JSON backends their front-ends use:

- Shaw: https://snow-pwsm-legacy.sice.tech/get_movie_release?id=<id>
  returns the movie's `primaryTitle`, which is prefixed "[POSTPONED]"
  while the film is postponed. Booking open == that prefix is gone.
- GV: https://www.gv.com.sg/.gv-api/{nowshowing,advancesales} return the
  bookable-movie feeds. They require an `Origin: https://www.gv.com.sg`
  header (without it the server replies {"success":false,
  "errorMessage":"Unauthorized Request"}). Booking open == the film shows
  up in either feed (it starts life in `comingsoon`, which is not
  bookable).
"""

import json
import os
import re
import smtplib
import sys
import urllib.request
from email.mime.text import MIMEText

STATE_FILE = "state.json"
MOVIE_RE = re.compile(r"jana\s*nayagan", re.IGNORECASE)

# Shaw's internal movie id for Jana Nayagan (from shaw.sg/movie-details/1624).
SHAW_MOVIE_ID = "1624"

BLAST = (
    "\U0001F6A8 JANA NAYAGAN SG BOOKINGS ARE OPEN \U0001F6A8\n\n"
    "Thalapathy's LAST film. Release: 24 July.\n\n"
    "Book NOW \u2014 Shaw / GV. FDFS sold out in under an hour last time, "
    "don't wait.\n\n"
    "Drop your preferred day + timing ASAP so we can lock seats together. "
    "GO GO GO \U0001F525"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept-Language": "en-SG,en;q=0.9",
}


def http(url: str, data: bytes | None = None, extra: dict | None = None) -> str:
    headers = {**HEADERS, **(extra or {})}
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def check_shaw() -> tuple[bool, str]:
    """Shaw's movie-detail JSON carries a "[POSTPONED]" title prefix while
    the film is postponed; booking open == prefix gone (+ showtimes live)."""
    body = http(
        f"https://snow-pwsm-legacy.sice.tech/get_movie_release?id={SHAW_MOVIE_ID}",
        extra={"Accept": "application/json", "Origin": "https://shaw.sg",
               "Referer": "https://shaw.sg/"},
    )
    data = json.loads(body)
    title = data.get("primaryTitle", "")
    if not MOVIE_RE.search(title):
        # Wrong id / unexpected payload — surface it rather than false-alert.
        return False, f"unexpected title {title!r}"
    if "postponed" in title.lower():
        return False, "still marked POSTPONED"

    # POSTPONED lifted. Confirm real showtimes exist before firing.
    try:
        times = http(
            f"https://snow-pwsm-legacy.sice.tech/get_show_times?movieId={SHAW_MOVIE_ID}",
            extra={"Accept": "application/json", "Origin": "https://shaw.sg",
                   "Referer": "https://shaw.sg/"},
        )
        sessions = json.loads(times)
        if isinstance(sessions, list) and sessions:
            return True, f"POSTPONED lifted, {len(sessions)} showtimes live"
    except Exception as exc:  # noqa: BLE001
        print(f"[Shaw/showtimes] {exc}", file=sys.stderr)
    return True, "POSTPONED lifted from title"


def check_gv() -> tuple[bool, str]:
    """GV is API-driven. Movie appearing in the nowshowing/advancesales
    JSON feeds (it starts in comingsoon) = booking open. The feeds require
    an Origin header or they reject with 'Unauthorized Request'."""
    for endpoint in ("nowshowing", "advancesales"):
        try:
            body = http(
                f"https://www.gv.com.sg/.gv-api/{endpoint}",
                data=b"{}",
                extra={"Content-Type": "application/json",
                       "Origin": "https://www.gv.com.sg"},
            )
            payload = json.loads(body)
            if not payload.get("success"):
                print(f"[GV/{endpoint}] {payload.get('errorMessage')}",
                      file=sys.stderr)
                continue
            films = payload.get("data") or []
            if any(MOVIE_RE.search(f.get("filmTitle", "")) for f in films):
                return True, f"listed in GV {endpoint}"
        except Exception as exc:  # noqa: BLE001
            print(f"[GV/{endpoint}] {exc}", file=sys.stderr)
    return False, "not in GV booking feeds yet"


TARGETS = [
    ("Shaw Theatres", check_shaw, "https://shaw.sg/movie-details/1624"),
    ("Golden Village", check_gv, "https://www.gv.com.sg"),
]


def send_email(subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr

    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


def main() -> int:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            if json.load(f).get("alerted"):
                print("Already alerted; nothing to do.")
                return 0

    hits = []
    for name, check, link in TARGETS:
        try:
            is_open, reason = check()
            print(f"[{name}] open={is_open} ({reason})")
            if is_open:
                hits.append((name, link))
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] check failed: {exc}", file=sys.stderr)

    if not hits:
        print("Booking not detected yet.")
        return 0

    links = "\n".join(f"{n}: {u}" for n, u in hits)
    send_email("JANA NAYAGAN SG bookings are open!", f"{BLAST}\n\nDetected on:\n{links}")
    with open(STATE_FILE, "w") as f:
        json.dump({"alerted": True}, f)
    print("ALERT SENT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
