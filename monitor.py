"""Multi-movie ticket-availability monitor — Shaw, GV, myCinemas (SG) + TGV,
GSC (MY / Johor only).

Runs every 15 min on GitHub Actions. For each watched movie it collects the
set of currently-open "slots" (movie x site x cinema x date x time) across all
sites, diffs against what it has already emailed (state.json), and emails only
the NEW slots. It keeps doing this every run — so you get a fresh email each
time a new cinema or a new showtime opens — until a movie is moved to STOPPED.

All sites are JS front-ends; we call the same JSON/XML backends they use:
- Shaw : snow-pwsm-legacy.sice.tech  get_selectors (bookable list) +
         get_show_times (per-movie showtimes). SG, all cinemas.
- GV   : www.gv.com.sg/.gv-api nowshowing+advancesales (needs Origin header).
         SG. Detected at "now bookable" level (GV showtime feed is coarse).
- myCinemas : mycinemas.sg SSR page + per-movie page showtimes. SG.
- TGV  : api.tgv.com.my nowselling + boxoffice moviesession_get. MY, filtered
         to the 5 Johor cinemas.
- GSC  : epaymentapi.gsc.com.my getEpaymentMovie_ParentChild (bookable list) +
         getShowTimesByMovie_ParentChild_V2 (per-cinema showtimes). MY, filtered
         to Johor cinemas (address state code JHR).
"""

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from email.mime.text import MIMEText

STATE_FILE = "state.json"

# --- movies to watch (add/remove freely) ---------------------------------
MOVIES = [
    "Jana Nayagan",
    "Spider-Man: Brand New Day",
    "Toxic",
    "Ramayana",
    "King",
    "Jailer 2",
    "Avengers: Doomsday",
    "I'm Game",
]

# Movies to stop watching (no more emails). Move a title here when done.
STOPPED: set[str] = set()

# How many days ahead to scan for showtimes (advance sales).
LOOKAHEAD_DAYS = 14

# --- title matching -------------------------------------------------------
# Bracketed qualifiers "(Tamil)" / "[IMAX]" are dropped; these standalone
# format/language tags are ignored too. A single-word title that is also a
# COMMON word must match the site title exactly (avoids "King" matching
# "The Lion King").
FORMAT_TAGS = {"imax", "3d", "2d", "atmos", "dolby", "4dx", "screenx", "hfr", "gv"}
COMMON_WORDS = {"king", "day", "war", "one", "end", "home", "the", "up", "it"}
# tokens allowed to trail a single-word title (sequel markers) e.g. "Ramayana Part 1"
QUALIFIER_RE = re.compile(r"^(?:\d+|i{1,3}|iv|vi{0,3}|part|chapter|vol|volume|final)$")


def normalize(text: str) -> list[str]:
    text = text.lower()
    text = re.sub(r"[’'`]", "", text)                   # I'm -> im (no split)
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", text)   # drop (..) and [..]
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return [t for t in text.split() if t and t not in FORMAT_TAGS]


def title_matches(target: str, site_title: str) -> bool:
    t = normalize(target)
    s = normalize(site_title)
    if not t or not s:
        return False
    n = len(t)
    if not any(s[i:i + n] == t for i in range(len(s) - n + 1)):
        return False
    if n >= 2:
        return True                       # multi-word title is specific enough
    word = t[0]
    if word in COMMON_WORDS:
        return s == [word]                # generic word must be the whole title
    # other single-word titles: any extra tokens must be sequel markers
    return all(QUALIFIER_RE.match(tok) for tok in s if tok != word)


def watched() -> list[str]:
    return [m for m in MOVIES if m not in STOPPED]


def match_movie(site_title: str) -> str | None:
    for m in watched():
        if title_matches(m, site_title):
            return m
    return None


# --- http -----------------------------------------------------------------
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    SSL_CTX = ssl.create_default_context()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")


def http(url, data=None, extra=None):
    headers = {"User-Agent": UA, "Accept-Language": "en"}
    headers.update(extra or {})
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def upcoming_dates():
    today = date.today()
    return [(today + timedelta(days=d)).isoformat() for d in range(LOOKAHEAD_DAYS)]


# --- slot type ------------------------------------------------------------
# A slot is one bookable unit we can alert on:
#   {movie, site, region, cinema, date, time, link}
# Its identity (for dedup) is movie|site|cinema|date|time.
def slot(movie, site, region, cinema, date_, time_, link):
    return {"movie": movie, "site": site, "region": region,
            "cinema": cinema, "date": date_, "time": time_, "link": link}


def slot_key(s):
    return "|".join([s["movie"], s["site"], s["cinema"], s["date"], s["time"]])


# =========================================================================
# Site collectors — each returns a list[slot] for watched movies.
# Each is wrapped in try/except by the caller, so one site failing never
# blocks the others.
# =========================================================================
SHAW_HDRS = {"Accept": "application/json", "Origin": "https://shaw.sg",
             "Referer": "https://shaw.sg/"}
SHAW_BASE = "https://snow-pwsm-legacy.sice.tech"


def collect_shaw():
    slots = []
    selectors = json.loads(http(f"{SHAW_BASE}/get_selectors", extra=SHAW_HDRS))
    theatres = {str(x["code"]): x["name"].replace("Shaw Theatres", "").strip()
                for x in selectors if x.get("type") == 2}
    movies = [x for x in selectors if x.get("type") == 1]
    for mv in movies:
        movie = match_movie(mv["name"])
        if not movie:
            continue
        code = mv["code"]
        link = f"https://shaw.sg/movie-details/{code}"
        got_times = False
        try:
            groups = json.loads(
                http(f"{SHAW_BASE}/get_show_times?movieCode={code}", extra=SHAW_HDRS))
            for g in groups:
                if str(g.get("movieId")) != str(code):
                    continue
                for stime in g.get("showTimes", []):
                    cinema = theatres.get(str(stime.get("locationId")),
                                          stime.get("locationVenueName", "Shaw"))
                    slots.append(slot(movie, "Shaw Theatres", "SG", cinema,
                                      stime.get("displayDate", ""),
                                      stime.get("displayTime", ""), link))
                    got_times = True
        except Exception as exc:  # noqa: BLE001
            print(f"[Shaw/showtimes {code}] {exc}", file=sys.stderr)
        if not got_times:
            # bookable but no showtimes parsed yet — still worth one alert
            slots.append(slot(movie, "Shaw Theatres", "SG", "", "", "", link))
    return slots


def collect_gv():
    slots = []
    seen = set()
    for endpoint in ("nowshowing", "advancesales"):
        body = http(f"https://www.gv.com.sg/.gv-api/{endpoint}", data=b"{}",
                    extra={"Content-Type": "application/json",
                           "Origin": "https://www.gv.com.sg"})
        payload = json.loads(body)
        if not payload.get("success"):
            print(f"[GV/{endpoint}] {payload.get('errorMessage')}", file=sys.stderr)
            continue
        for film in payload.get("data") or []:
            movie = match_movie(film.get("filmTitle", ""))
            if movie and movie not in seen:
                seen.add(movie)
                # GV's showtime feed is coarse; alert at "now bookable" level.
                slots.append(slot(movie, "Golden Village", "SG", "", "", "",
                                  "https://www.gv.com.sg/GVBuyTickets"))
    return slots


def collect_mycinemas():
    slots = []
    html = http("https://mycinemas.sg/indexdesk")
    links = dict.fromkeys(re.findall(
        r'href="(https://mycinemas\.sg/movie/([^"]+))"', html))
    for full, slug in links:
        movie = match_movie(slug.replace("-", " "))
        if not movie:
            continue
        try:
            page = http(full)
        except Exception as exc:  # noqa: BLE001
            print(f"[myCinemas/{slug}] {exc}", file=sys.stderr)
            continue
        times = re.findall(r"\b\d{1,2}:\d{2}\s?(?:am|pm)\b", page, re.I)
        if len(times) >= 2:  # real showtimes present == bookable
            for t in dict.fromkeys(times):
                slots.append(slot(movie, "myCinemas", "SG", "myCinemas", "", t, full))
        # else: listed but no showtimes yet (coming soon) -> ignore
    return slots


# --- TGV (MY, Johor only) -------------------------------------------------
TGV_JOHOR_CINEMAS = {"BI0": "Bukit Indah", "KUL": "Kulaijaya",
                     "TSC": "Tasek Central", "TBR": "Tebrau City", "TOP": "Toppen"}


def _tgv_post(path, body):
    return json.loads(http(f"https://api.tgv.com.my/api/{path}",
                           data=json.dumps(body).encode(),
                           extra={"Content-Type": "application/json",
                                  "Accept": "application/json"}))


def collect_tgv():
    slots = []
    payload = json.loads(http(
        "https://api.tgv.com.my/api/movies/v1/movielist/nowselling",
        extra={"Accept": "application/json"}))
    movies = payload.get("results", {}).get("movies", [])
    for mv in movies:
        movie = match_movie(mv.get("name", ""))
        if not movie:
            continue
        recid = mv.get("recid")
        itemkey = mv.get("itemkey", "")
        link = f"https://www.tgv.com.my/movie/{itemkey}"
        try:
            dates = _tgv_post("boxoffice/v1/moviesession_getsessionbusinessdates",
                              {"movierecid": recid})["results"]["businessdates"]
        except Exception as exc:  # noqa: BLE001
            print(f"[TGV/dates {itemkey}] {exc}", file=sys.stderr)
            dates = []
        found = False
        for d in dates[:LOOKAHEAD_DAYS]:
            for cid, cname in TGV_JOHOR_CINEMAS.items():
                try:
                    res = _tgv_post("boxoffice/v1/moviesession_get", {
                        "movierecid": recid, "businessdate": d, "cinemaid": cid,
                        "location": "", "experience": "", "experiencegroup": ""})
                    cinemas = res.get("results", {}).get("businessday", {}).get("cinemas", [])
                except Exception as exc:  # noqa: BLE001
                    print(f"[TGV/session {itemkey} {cid} {d}] {exc}", file=sys.stderr)
                    continue
                # endpoint returns every movie at the cinema — keep only ours
                for c in cinemas:
                    for m in c.get("movies", []):
                        for exp in m.get("experiences", []) or []:
                            for sess in exp.get("sessions", []) or []:
                                if sess.get("movieid") != recid:
                                    continue
                                iso = sess.get("showtimemy", "")
                                t = iso[11:16] if "T" in iso else ""
                                slots.append(slot(movie, "TGV", "Johor", cname,
                                                  d, t, link))
                                found = True
        if not found:
            # bookable nationally but no Johor sessions parsed — one heads-up.
            slots.append(slot(movie, "TGV", "Johor", "", "", "", link))
    return slots


# --- GSC (MY, Johor only) -------------------------------------------------
GSC_MOVIES = ("https://epaymentapi.gsc.com.my/showtimews/service.asmx/"
              "getEpaymentMovie_ParentChild?includeChild=true&parent=")
GSC_TIMES = ("https://epaymentapi.gsc.com.my/showtimews/service.asmx/"
             "getShowTimesByMovie_ParentChild_V2?parentid={code}&oprndate={d}")


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def collect_gsc():
    slots = []
    films_xml = ET.fromstring(http(GSC_MOVIES))
    for parent in films_xml.findall("parent"):
        title = parent.get("title", "")
        movie = match_movie(title)
        if not movie:
            continue
        code = parent.get("code")
        link = (f"https://epaymentwebapp.gsc.com.my/showtime-by-movies/"
                f"{code}/{_slugify(title)}?id={code}")
        found = False
        for d in upcoming_dates():
            try:
                locs = ET.fromstring(http(GSC_TIMES.format(code=code, d=d)))
            except Exception as exc:  # noqa: BLE001
                print(f"[GSC/times {code} {d}] {exc}", file=sys.stderr)
                continue
            for loc in locs.findall("location"):
                if ",JHR," not in loc.get("address", ""):
                    continue  # Johor cinemas only
                cinema = loc.get("name", "")
                times = []
                for show in loc.iter("show"):
                    ts = (show.get("timestr") or show.get("time") or "").strip()
                    if ts:
                        times.append(ts)
                for t in dict.fromkeys(times):
                    slots.append(slot(movie, "GSC", "Johor", cinema, d, t, link))
                    found = True
                if not times:  # location present but no parsed times
                    slots.append(slot(movie, "GSC", "Johor", cinema, d, "", link))
                    found = True
        if not found:
            slots.append(slot(movie, "GSC", "Johor", "", "", "", link))
    return slots


COLLECTORS = [
    ("Shaw", collect_shaw),
    ("GV", collect_gv),
    ("myCinemas", collect_mycinemas),
    ("TGV", collect_tgv),
    ("GSC", collect_gsc),
]


# --- email ----------------------------------------------------------------
def send_email(subject, body):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = os.environ.get("ALERT_TO") or user

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


def compose(new_slots):
    """Group new slots into a readable email body."""
    by_movie = {}
    for s in new_slots:
        by_movie.setdefault(s["movie"], {}).setdefault(
            (s["site"], s["region"], s["link"]), []).append(s)

    lines = ["New ticket availability detected:\n"]
    for movie in sorted(by_movie):
        lines.append(f"\U0001F3AC {movie}")
        for (site, region, link), items in by_movie[movie].items():
            tag = site if region in ("SG", "") else f"{site} (Johor)"
            lines.append(f"  ✅ OPEN on {tag}")
            # group times by cinema+date
            by_cd = {}
            for it in items:
                if it["cinema"] or it["date"] or it["time"]:
                    by_cd.setdefault((it["cinema"], it["date"]), []).append(it["time"])
            for (cinema, d), times in sorted(by_cd.items()):
                times = [t for t in dict.fromkeys(times) if t]
                where = " / ".join(x for x in (cinema, d) if x)
                when = ("  " + ", ".join(times)) if times else ""
                lines.append(f"       - {where}{when}")
            lines.append(f"       Book: {link}")
        lines.append("")
    return "\n".join(lines)


# --- main -----------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f).get("seen", []))
    return set()


def save_state(seen):
    with open(STATE_FILE, "w") as f:
        json.dump({"seen": sorted(seen)}, f, indent=0)


def main():
    if "--test" in sys.argv:
        send_email("jn-monitor test alert",
                   "Test from your multi-movie ticket monitor. Alerts are "
                   "wired up. Real alerts name the movie, site, cinema and "
                   "showtime, with a direct booking link.")
        print("Test email sent.")
        return 0

    all_slots = []
    for name, fn in COLLECTORS:
        try:
            got = fn()
            print(f"[{name}] {len(got)} open slot(s) for watched movies")
            all_slots.extend(got)
        except Exception as exc:  # noqa: BLE001
            print(f"[{name}] FAILED: {exc}", file=sys.stderr)

    seen = load_state()
    new_slots = [s for s in all_slots if slot_key(s) not in seen]

    if not new_slots:
        print("No new availability.")
        return 0

    body = compose(new_slots)
    movies_hit = sorted({s["movie"] for s in new_slots})
    print(f"New availability for: {', '.join(movies_hit)}")
    send_email("Ticket availability update: " + ", ".join(movies_hit), body)
    seen.update(slot_key(s) for s in all_slots)
    save_state(seen)
    print(f"Emailed {len(new_slots)} new slot(s); state now has {len(seen)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
