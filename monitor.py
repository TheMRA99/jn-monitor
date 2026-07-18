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

# --- movies to watch ------------------------------------------------------
# title   : the film name (loose matching, see title_matches)
# lang    : preferred language; skips wrong-language versions (None = any)
# to      : recipient key(s) — "rees" -> ALERT_TO (reeslikefood), "self" -> you.
#           A list sends to several people. Defaults to ["self"].
# sites   : restrict to these site names (omit = all 5 sites).
#           Names: "Shaw Theatres", "Golden Village", "myCinemas", "TGV", "GSC".
# subs    : "eng" -> only showings that display English subtitles (Shaw).
# premium : True -> only premium showings (IMAX / Lumiere / premiere halls) so
#           you get the best screen, sound and seats (Shaw).
MOVIES = [
    {"title": "Jana Nayagan", "lang": "Tamil", "to": ["rees", "self"],
     "sites": ["Shaw Theatres", "Golden Village", "myCinemas"]},   # SG only
    {"title": "Spider-Man: Brand New Day", "lang": "English",
     "to": ["rees", "self"], "sites": ["Shaw Theatres", "Golden Village"],
     "subs": "eng", "premium": True},
    {"title": "Avengers: Doomsday", "lang": "English",
     "to": ["rees", "self"], "sites": ["Shaw Theatres", "Golden Village"],
     "subs": "eng", "premium": True},   # premium halls (need not be IMAX)
    {"title": "Toxic",                     "lang": "Tamil"},
    {"title": "Ramayana",                  "lang": "Hindi"},
    {"title": "King",                      "lang": "Hindi"},
    {"title": "Jailer 2",                  "lang": "Tamil"},
    {"title": "I'm Game",                  "lang": None},
]

# Titles to stop watching (no more emails). Add a title here when done.
STOPPED: set[str] = set()

# How many days ahead to scan for showtimes (advance sales open ~2-3 wks out).
LOOKAHEAD_DAYS = 25

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


# --- language disambiguation ---------------------------------------------
LANG_TOKENS = {
    "tamil": {"tamil", "tam", "tml"},
    "hindi": {"hindi", "hin"},
    "english": {"english", "eng"},
    "telugu": {"telugu", "tel"},
    "malayalam": {"malayalam", "mal"},
    "kannada": {"kannada", "kan"},
    "mandarin": {"mandarin", "mand", "chinese"},
}


def _langs_in(*texts) -> set[str]:
    toks = set()
    for t in texts:
        toks |= set(re.split(r"[^a-z]+", (t or "").lower()))
    return {lang for lang, keys in LANG_TOKENS.items() if toks & keys}


def lang_ok(desired, *texts) -> bool:
    """Conservative: only reject when the text clearly names a *different*
    language. Missing/ambiguous language always passes (never lose a match)."""
    if not desired:
        return True
    mentioned = _langs_in(*texts)
    if not mentioned:
        return True
    return desired.lower() in mentioned


def watched() -> list[dict]:
    return [m for m in MOVIES if m["title"] not in STOPPED]


def match_movie(site: str, site_title: str, *lang_texts) -> str | None:
    """Match against watched movies, honouring per-movie `sites` scoping and
    language. `site` is the calling collector's site name."""
    for m in watched():
        if not title_matches(m["title"], site_title):
            continue
        if not lang_ok(m.get("lang"), site_title, *lang_texts):
            continue
        allowed = m.get("sites")
        if allowed and site not in allowed:
            continue
        return m["title"]
    return None


def movie_lang(title: str) -> str | None:
    return next((m.get("lang") for m in MOVIES if m["title"] == title), None)


def movie_conf(title: str) -> dict:
    return next((m for m in MOVIES if m["title"] == title), {})


def recipients_for(title: str) -> list[str]:
    """Resolve a movie's recipient key(s) to email addresses."""
    self_addr = os.environ["SMTP_USER"]
    keymap = {"rees": os.environ.get("ALERT_TO") or self_addr, "self": self_addr}
    for m in MOVIES:
        if m["title"] == title:
            keys = m.get("to", ["self"])
            if isinstance(keys, str):
                keys = [keys]
            return list(dict.fromkeys(keymap.get(k, self_addr) for k in keys))
    return [self_addr]


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


def http_browser(url, data=None, extra=None):
    """GV sits behind Cloudflare, which blocks urllib's TLS fingerprint. Use
    curl_cffi to impersonate a real Chrome (falls back to plain http())."""
    try:
        from curl_cffi import requests as creq
        headers = {"Accept-Language": "en"}
        headers.update(extra or {})
        method = "POST" if data is not None else "GET"
        resp = creq.request(method, url, data=data, headers=headers,
                            impersonate="chrome", timeout=30)
        return resp.text
    except ImportError:
        return http(url, data=data, extra=extra)


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
# Subtitle codes that include English (NOSB = no subtitles -> excluded).
ENG_SUB_CODES = {"ENSB", "ECSB", "ELSB"}
# Premium screen/sound/seat indicators.
PREMIUM_FORMATS = {"IMLS", "IMAX", "IMGT"}          # IMAX variants
PREMIUM_VENUE_KW = ("imax", "lumiere", "premiere", "dolby", "atmos", "gold")


def _shaw_premium(fmt, venue):
    return fmt in PREMIUM_FORMATS or any(k in venue.lower()
                                         for k in PREMIUM_VENUE_KW)


def collect_shaw():
    slots = []
    selectors = json.loads(http(f"{SHAW_BASE}/get_selectors", extra=SHAW_HDRS))
    theatres = {str(x["code"]): x["name"].replace("Shaw Theatres", "").strip()
                for x in selectors if x.get("type") == 2}
    movies = [x for x in selectors if x.get("type") == 1]
    for mv in movies:
        movie = match_movie("Shaw Theatres", mv["name"])
        if not movie:
            continue
        conf = movie_conf(movie)
        want_eng = conf.get("subs") == "eng"
        want_prem = conf.get("premium")
        code = mv["code"]
        link = f"https://shaw.sg/movie-details/{code}"
        got_times = False
        try:
            groups = json.loads(
                http(f"{SHAW_BASE}/get_show_times?movieCode={code}", extra=SHAW_HDRS))
            for g in groups:
                if str(g.get("movieId")) != str(code):
                    continue
                for st in g.get("showTimes", []):
                    got_times = True
                    venue = st.get("locationVenueName", "")
                    fmt = st.get("formatCode", "")
                    if want_eng and st.get("subtitleCode") not in ENG_SUB_CODES:
                        continue
                    if want_prem and not _shaw_premium(fmt, venue):
                        continue
                    # premium movies get the exact hall (shows IMAX/Lumiere);
                    # others get the (grouped) theatre name.
                    cinema = venue if want_prem else theatres.get(
                        str(st.get("locationId")), venue or "Shaw")
                    slots.append(slot(movie, "Shaw Theatres", "SG", cinema,
                                      st.get("displayDate", ""),
                                      st.get("displayTime", ""), link))
        except Exception as exc:  # noqa: BLE001
            print(f"[Shaw/showtimes {code}] {exc}", file=sys.stderr)
        if not got_times:
            # bookable but no showtimes parsed yet — still worth one alert
            slots.append(slot(movie, "Shaw Theatres", "SG", "", "", "", link))
    return slots


GV_LINK = "https://www.gv.com.sg/GVBuyTickets"
# GV models premium experiences as named cinemas.
GV_PREMIUM_KW = ("gvmax", "gold class", "dolby", "atmos", "deluxe")


def _gv(path):
    body = http_browser(f"https://www.gv.com.sg/.gv-api/{path}", data="{}",
                        extra={"Content-Type": "application/json",
                               "Origin": "https://www.gv.com.sg"})
    return json.loads(body)


def _gv_date(s):  # "18-07-2026" -> "2026-07-18"
    p = s.split("-")
    return f"{p[2]}-{p[1]}-{p[0]}" if len(p) == 3 else s


def collect_gv():
    slots = []
    # 1. Which watched movies are bookable on GV (now-showing / advance sales)?
    bookable = set()
    for endpoint in ("nowshowing", "advancesales"):
        payload = _gv(endpoint)
        if not payload.get("success"):
            print(f"[GV/{endpoint}] {payload.get('errorMessage')}", file=sys.stderr)
            continue
        for film in payload.get("data") or []:
            movie = match_movie("Golden Village", film.get("filmTitle", ""))
            if movie:
                bookable.add(movie)
    if not bookable:
        return slots

    # 2. Same-day session detail (GV only exposes today's feed). Used to give
    #    premium/subtitle-filtered showtimes for movies that ask for them.
    detailed = set()
    want_detail = {m for m in bookable
                   if movie_conf(m).get("premium") or movie_conf(m).get("subs")}
    if want_detail:
        try:
            names = {c["id"]: c["name"] for c in (_gv("cinemas").get("data") or [])}
            bt = _gv("v2buytickets").get("data", {})
            for c in bt.get("cinemas") or []:
                cname = names.get(c["id"], "")
                is_prem = any(k in cname.lower() for k in GV_PREMIUM_KW)
                for m in c.get("movies") or []:
                    movie = match_movie("Golden Village", m.get("filmTitle", ""))
                    if movie not in want_detail:
                        continue
                    conf = movie_conf(movie)
                    if conf.get("premium") and not is_prem:
                        continue
                    if conf.get("subs") == "eng" and \
                            "English" not in (m.get("subTitles") or []):
                        continue
                    for t in m.get("times") or []:
                        slots.append(slot(movie, "Golden Village", "SG", cname,
                                          _gv_date(t.get("showDate", "")),
                                          t.get("time12", ""), GV_LINK))
                        detailed.add(movie)
        except Exception as exc:  # noqa: BLE001
            print(f"[GV/sessions] {exc}", file=sys.stderr)

    # 3. Bookable-level alert for movies without same-day session detail yet
    #    (advance sales are future-dated and not in GV's same-day feed).
    for movie in bookable - detailed:
        slots.append(slot(movie, "Golden Village", "SG", "", "", "", GV_LINK))
    return slots


def collect_mycinemas():
    slots = []
    html = http("https://mycinemas.sg/indexdesk")
    links = dict.fromkeys(re.findall(
        r'href="(https://mycinemas\.sg/movie/([^"]+))"', html))
    for full, slug in links:
        movie = match_movie("myCinemas", slug.replace("-", " "))
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
        movie = match_movie("TGV", mv.get("name", ""))
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
    # No national fallback: MY sites alert only on real Johor showtimes.
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
        movie = match_movie("GSC", title)
        if not movie:
            continue
        desired = movie_lang(movie)
        code = parent.get("code")
        link = (f"https://epaymentwebapp.gsc.com.my/showtime-by-movies/"
                f"{code}/{_slugify(title)}?id={code}")
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
                for child in loc.findall("child"):
                    if not lang_ok(desired, child.get("lang", "")):
                        continue  # skip wrong-language version
                    times = [(s.get("timestr") or s.get("time") or "").strip()
                             for s in child.findall("show")]
                    for t in dict.fromkeys(t for t in times if t):
                        slots.append(slot(movie, "GSC", "Johor", cinema, d, t, link))
    # No national fallback: MY sites alert only on real Johor showtimes.
    return slots


COLLECTORS = [
    ("Shaw", collect_shaw),
    ("GV", collect_gv),
    ("myCinemas", collect_mycinemas),
    ("TGV", collect_tgv),
    ("GSC", collect_gsc),
]


# --- email ----------------------------------------------------------------
def send_email(subject, body, to_addr=None):
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addr = to_addr or os.environ.get("ALERT_TO") or user

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.starttls()
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())


# Above this many new showtimes for one site, summarise instead of listing
# every line (keeps the first "it's fully open" email readable).
DETAIL_LIMIT = 25

# Reference "sweet spot" seat guidance for premium (immersive) movies.
SEAT_TIP = (
    "   \U0001F3A7 Best seats: dead-centre columns, ~2/3 of the way back — the "
    "audio/visual reference spot (a row or two behind the exact middle). "
    "IMAX: sit a touch further back so the screen fills your view. "
    "Dolby Atmos: centre-back for the truest surround + overhead mix. "
    "Avoid the front third and the very back/under-balcony."
)


def compose(new_slots):
    """Group new slots into a readable email body. Small deltas are itemised
    (exact new cinema/date/time); large initial dumps are summarised."""
    by_movie = {}
    for s in new_slots:
        by_movie.setdefault(s["movie"], {}).setdefault(
            (s["site"], s["region"], s["link"]), []).append(s)

    lines = ["New ticket availability:\n"]
    for movie in sorted(by_movie):
        lines.append(f"\U0001F3AC {movie}")
        if movie_conf(movie).get("premium"):
            lines.append(SEAT_TIP)
        for (site, region, link), items in by_movie[movie].items():
            tag = site if region in ("SG", "") else f"{site} (Johor)"
            detailed = [it for it in items
                        if it["cinema"] or it["date"] or it["time"]]
            lines.append(f"  ✅ OPEN on {tag}")

            if not detailed:                       # bookable-level (Shaw/GV)
                lines.append(f"       Book: {link}")
                continue

            if len(detailed) > DETAIL_LIMIT:       # summarise big dumps
                cinemas = sorted({it["cinema"] for it in detailed if it["cinema"]})
                dates = sorted({it["date"] for it in detailed if it["date"]})
                span = f"{dates[0]} to {dates[-1]}" if dates else ""
                lines.append(f"       {len(detailed)} showtimes across "
                             f"{len(cinemas)} cinema(s){', ' + span if span else ''}")
                lines.append(f"       Cinemas: {', '.join(cinemas)}")
            else:                                  # itemise small deltas
                by_cd = {}
                for it in detailed:
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
                   "showtime, with a direct booking link.",
                   to_addr=os.environ["SMTP_USER"])
        print("Test email sent to self.")
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

    # Route by recipient. A movie may have several recipients (Jana Nayagan
    # goes to both reeslikefood and you); each gets the relevant slots.
    by_recipient = {}
    for s in new_slots:
        for addr in recipients_for(s["movie"]):
            by_recipient.setdefault(addr, []).append(s)

    for to_addr, slots_for in by_recipient.items():
        movies_hit = sorted({s["movie"] for s in slots_for})
        subject = "Ticket availability update: " + ", ".join(movies_hit)
        send_email(subject, compose(slots_for), to_addr=to_addr)
        print(f"Emailed {len(slots_for)} slot(s) for {', '.join(movies_hit)} "
              f"-> {to_addr}")

    seen.update(slot_key(s) for s in all_slots)
    save_state(seen)
    print(f"State now has {len(seen)} slots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
