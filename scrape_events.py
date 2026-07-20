#!/usr/bin/env python3
"""
Builds events.json for the DASH app's Events page.

Design goal: always produce a valid feed. Sources are merged, and every
scraped source is isolated — if one breaks, the others plus the manual
entries still ship.

  1. manual_events.json — hand-maintained entries you fully control.
     This alone makes the feature work.

  2. sources.json — optional list of iCal (.ics) URLs, parsed if present.

  3. De Anza events calendar month view
     (deanza.edu/events/month.html?m=MM&y=YYYY) — every campus event for
     the next few months, with time, location and description. The month
     view is scraped instead of the list/category/detail pages because
     those endpoints error server-side.

  4. ISP "Events and Workshops" page (deanza.edu/international/workshops/)
     — the international-student workshop table (WORKSHOP | DATE | TIME |
     LOCATION | REGISTRATION LINK) plus the special-event sections under
     <h3> headings (transfer fair, graduation, Nowruz, ...). Events from
     this page are tagged `international`, except transfer-focused ones
     (UC TAG/TAP, CSU/UC application workshops, transfer fairs), which
     are tagged `transfer`.

  5. Academic "Dates and Deadlines" page
     (deanza.edu/calendar/dates-and-deadlines.html) — per-quarter <dl>
     lists of add/drop, registration, holidays and finals. Deadlines are
     what F-1 students most need to keep 12 units and pay on time.

The site sits behind Cloudflare, which blocks obvious bots. Requests send
browser-like headers; if a run still comes back with 403s, check the
Action log — manual entries keep the feed alive regardless.

Offline testing: set DASH_FIXTURES to a directory of saved pages and the
fetchers read files (month-YYYY-MM.html, workshops.html, deadlines.html)
instead of the network.

Standard library only — no pip install, so the Action stays simple.
"""

import html as htmllib
import json
import os
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    CAMPUS_TZ = ZoneInfo("America/Los_Angeles")
except Exception:                                   # very old Python: fixed PDT
    CAMPUS_TZ = timezone(timedelta(hours=-7))

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "events.json")
SITE = "https://www.deanza.edu"

# How many months of the events calendar to walk, and how far ahead the
# feed reaches. The deadlines page lists ~4 quarters; ~8 months keeps the
# next two quarters' deadlines without bloating the feed.
MONTHS_AHEAD = 3
HORIZON_DAYS = 240

HEADERS = {
    # Cloudflare rejects default urllib/bot user agents.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17.5 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Keyword → category rules, checked in order; first match wins.
CATEGORY_RULES = [
    ("international", ["international", "f-1", "f1 visa", "cpt", "opt", "sevis",
                       "visa", "isp", "i-20", "i20", "global"]),
    ("deadline",      ["deadline", "last day", "due", "registration open",
                       "registration begin", "add/drop", "tuition", "payment",
                       "apply by", "classes begin", "final exam",
                       "priority registration", "drop classes", "add 12-week"]),
    ("career",        ["career", "job fair", "internship", "employer", "resume",
                       "hiring", "job search"]),
    ("transfer",      ["transfer", "(tag)", "(tap)", "uc tag", "uc tap",
                       "csu application", "csu workshop",
                       "uc application", "(uc) application",
                       "admission guarantee", "university representative"]),
    ("workshop",      ["workshop", "tutoring", "advising", "orientation",
                       "info session", "seminar", "webinar"]),
    ("wellness",      ["health", "wellness", "counsel", "clinic", "mental",
                       "flu shot", "vaccine", "meditation"]),
    ("social",        ["club", "fair", "mixer", "festival", "celebration",
                       "social", "party", "meetup", "flea market", "concert",
                       "performance", "movie night"]),
]

MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july",
               "august", "september", "october", "november", "december"]


def categorize(title, details):
    text = f"{title} {details}".lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "general"


def isp_category(title, details=""):
    """ISP-page events default to `international`, but transfer-focused
    sessions (UC TAG/TAP, CSU/UC application workshops, transfer fairs)
    belong under `transfer` so they match the app's Transfer category.

    The transfer keywords are checked directly rather than through
    `categorize`, whose `international` rule fires first and would always
    win here — ISP details invariably mention international students."""
    text = f"{title} {details}".lower()
    transfer_keywords = dict(CATEGORY_RULES)["transfer"]
    return "transfer" if any(k in text for k in transfer_keywords) else "international"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ---- fetching ---------------------------------------------------------------

def fetch(url, fixture=None):
    fixtures = os.environ.get("DASH_FIXTURES")
    if fixtures and fixture:
        path = os.path.join(fixtures, fixture)
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---- HTML helpers -----------------------------------------------------------

def strip_tags(fragment):
    """Comments and tags out, entities decoded, whitespace collapsed."""
    text = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def first_link(fragment, *, skip=("instagram.com", ".png", ".jpg", ".jpeg", ".gif")):
    for href in re.findall(r'href="([^"]+)"', fragment):
        if any(s in href.lower() for s in skip):
            continue
        if href.startswith("/"):
            return SITE + href
        if href.startswith("http"):
            return href
    return None


# ---- date & time parsing ----------------------------------------------------

def month_number(name):
    name = name.lower()[:3]
    for i, m in enumerate(MONTH_NAMES, 1):
        if m.startswith(name):
            return i
    return None


# (?!\d) keeps a bare "Nov 2027" from reading as day 20 of November.
DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(\d{1,2})(?!\d)(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?", re.I)
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")


def parse_fuzzy_date(text, today, context_years=()):
    """Best-effort 'Oct. 15', 'October 15, 2026' or '10/15' → date.

    Without an explicit year: try years mentioned elsewhere in the text
    (e.g. 'The 2026 Transfer Fair ... on May 5'), preferring the one that
    lands nearest today; otherwise assume the next occurrence.
    """
    m = DATE_RE.search(text)
    if m:
        month, day = month_number(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
    else:
        m = NUMERIC_DATE_RE.search(text)
        if not m:
            return None
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        if year and year < 100:
            year += 2000
    if not month or not (1 <= day <= 31):
        return None
    try:
        if year:
            return date(year, month, day)
        candidates = []
        for y in list(context_years) + [today.year, today.year + 1]:
            try:
                candidates.append(date(int(y), month, day))
            except ValueError:
                continue
        # Nearest to today, but not more than a month in the past.
        future = [d for d in candidates if d >= today - timedelta(days=31)]
        pool = future or candidates
        return min(pool, key=lambda d: abs((d - today).days))
    except ValueError:
        return None


MERIDIEM_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?\.?m\b", re.I)
RANGE_SHARED_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(?:-|–|—|to|until)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?\.?m\b", re.I)


def to_24h(hour, minute, meridiem):
    hour = hour % 12
    if meridiem.lower() == "p":
        hour += 12
    return hour, minute


def parse_times(text):
    """'8:00 am to 2:00 pm', '9 a.m.', '3-5 pm' → ((h, m), (h, m) | None) | None."""
    text = text.lower().replace("noon", "12:00 pm").replace("midnight", "12:00 am")
    hits = MERIDIEM_TIME_RE.findall(text)
    if len(hits) >= 2:
        (h1, m1, mer1), (h2, m2, mer2) = hits[0], hits[1]
        return (to_24h(int(h1), int(m1 or 0), mer1),
                to_24h(int(h2), int(m2 or 0), mer2))
    shared = RANGE_SHARED_RE.search(text)
    if shared:
        h1, m1, h2, m2, mer = shared.groups()
        start = to_24h(int(h1), int(m1 or 0), mer)
        end = to_24h(int(h2), int(m2 or 0), mer)
        # '11-1 pm' style ranges cross noon: an am start makes more sense.
        if start >= end:
            start = to_24h(int(h1), int(m1 or 0), "a")
        return (start, end)
    if len(hits) == 1:
        h, m, mer = hits[0]
        return (to_24h(int(h), int(m or 0), mer), None)
    return None


def iso_utc(day, hm):
    local = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=CAMPUS_TZ)
    return local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def make_event(id_seed, title, details, category, day, times, location, url):
    """Build one feed entry; all-day events use bare YYYY-MM-DD dates."""
    if times:
        start = iso_utc(day, times[0])
        end = iso_utc(day, times[1]) if times[1] else None
    else:
        start, end = day.isoformat(), None
    return {
        "id": slugify(id_seed),
        "title": title,
        "details": details,
        "category": category,
        "start": start,
        "end": end,
        "location": location or None,
        "url": url or None,
    }


# ---- source: campus events calendar (month view) ----------------------------

def scrape_calendar(today):
    events = []
    year, month = today.year, today.month
    for _ in range(MONTHS_AHEAD):
        url = f"{SITE}/events/month.html?m={month:02d}&y={year}"
        page = fetch(url, fixture=f"month-{year}-{month:02d}.html")
        events.extend(parse_month(page))
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return events


def parse_month(page):
    events = []
    # Day cells: <td class="day..."> <time datetime="YYYY-MM-DD"> then zero
    # or more <div class="event ..."> blocks.
    for cell in re.split(r'<td class="day', page)[1:]:
        date_match = re.search(r'<time datetime="(\d{4}-\d{2}-\d{2})"', cell)
        if not date_match:
            continue
        day = date.fromisoformat(date_match.group(1))
        for chunk in re.split(r'<div class="event', cell)[1:]:
            title_m = re.search(r"<h3>(.*?)</h3>", chunk, re.S)
            if not title_m:
                continue
            title = strip_tags(title_m.group(1))
            desc_m = re.search(r'<div class="desc[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
            details = strip_tags(desc_m.group(1)) if desc_m else ""
            loc_m = re.search(r'<div class="location"[^>]*>(.*?)</div>', chunk, re.S)
            location = strip_tags(loc_m.group(1)) if loc_m else None
            time_m = re.search(r'<div class="datetime"[^>]*>(.*?)</div>', chunk, re.S)
            times = parse_times(strip_tags(time_m.group(1))) if time_m else None
            link_m = re.search(r'<div class="link"[^>]*>\s*([^<\s]+)\s*</div>', chunk)
            url = SITE + link_m.group(1) if link_m and link_m.group(1).startswith("/") else None
            event_id = re.search(r"id=(\d+)", link_m.group(1)).group(1) if link_m and "id=" in link_m.group(1) else slugify(title)
            events.append(make_event(
                f"cal-{event_id}-{day.isoformat()}", title, details,
                categorize(title, details), day, times, location, url,
            ))
    return events


# ---- source: ISP events & workshops page -------------------------------------

ISP_URL = f"{SITE}/international/workshops/"
ORIENTATION_URL = f"{SITE}/international/new-students/orientation.html"


def scrape_isp(today):
    page = fetch(ISP_URL, fixture="workshops.html")
    events = scrape_isp_table(page, today)
    events.extend(scrape_isp_sections(page, today))
    # The orientation page is a separate fetch; its failure must not cost
    # the workshop events.
    try:
        events.extend(scrape_orientation(fetch(ORIENTATION_URL, fixture="orientation.html"), today))
    except Exception as exc:
        print(f"SKIP orientation page: {exc}", file=sys.stderr)
    return events


def scrape_orientation(page, today):
    """ISP posts each quarter's mandatory new-student orientation here only
    2–3 weeks ahead of the session. Pull any future date whose surrounding
    text mentions orientation; while the date is unannounced this yields
    nothing, and the event appears the week ISP publishes it."""
    events = []
    text = strip_tags(page.split("l-content", 1)[-1])
    for match in list(DATE_RE.finditer(text)) + list(NUMERIC_DATE_RE.finditer(text)):
        window_start = max(0, match.start() - 160)
        context = text[window_start:match.end() + 80]
        if "orientation" not in context.lower():
            continue
        # Dates belonging to "classes start Sept. 21" or "registration
        # date is August 10" aren't the orientation's own date.
        lead_in = text[max(0, match.start() - 40):match.start()].lower()
        if any(word in lead_in for word in
               ("start", "begin", "classes", "quarter", "registration", "deadline")):
            continue
        day = parse_fuzzy_date(match.group(0), today,
                               re.findall(r"\b(20\d{2})\b", context))
        if not day or day < today:
            continue
        # The window can open and close mid-word; trim to word boundaries.
        details = context if window_start == 0 else re.sub(r"^\S+\s+", "", context)
        details = re.sub(r"\s+\S*$", "", details.strip()[:280])
        events.append(make_event(
            f"isp-orientation-{day.isoformat()}",
            "International Student Orientation",
            details, "international",
            day, parse_times(context), None, ORIENTATION_URL,
        ))
    return events


def scrape_isp_table(page, today):
    """The quarterly schedule: WORKSHOP | DATE | TIME | LOCATION | REG LINK."""
    events = []
    table_m = re.search(r"<table.*?</table>", page, re.S)
    if not table_m:
        return events
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(0), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue  # header row or the "check back" placeholder
        title = strip_tags(cells[0])
        day = parse_fuzzy_date(strip_tags(cells[1]), today)
        if not title or not day:
            continue
        times = parse_times(strip_tags(cells[2]))
        location = strip_tags(cells[3]) or None
        url = first_link(cells[4]) if len(cells) > 4 else None
        details = f"International Student Programs workshop. {('Register in advance: ' + url) if url else ''}".strip()
        events.append(make_event(
            f"isp-{title}-{day.isoformat()}", title, details,
            isp_category(title, details), day, times, location, url or ISP_URL,
        ))
    return events


def scrape_isp_sections(page, today):
    """Special events under <h3> headings (transfer fair, graduation, ...)."""
    events = []
    after_table = page.split("</table>", 1)[-1]
    for chunk in re.split(r"<hr\s*/?>", after_table):
        title_m = re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        if not title_m:
            continue
        title = strip_tags(title_m.group(1))
        if not title or "instagram" in title.lower() or "follow us" in title.lower():
            continue
        body = strip_tags(chunk)
        context_years = re.findall(r"\b(20\d{2})\b", body)
        # Prefer an explicit "Date:" label, then a date in the heading,
        # then anywhere in the section.
        day = None
        label_m = re.search(r"Date:\s*(.{0,40})", body)
        if label_m:
            day = parse_fuzzy_date(label_m.group(1), today, context_years)
        day = day or parse_fuzzy_date(title, today, context_years) \
                  or parse_fuzzy_date(body, today, context_years)
        if not day:
            continue  # e.g. "International Education Week Nov 2027" — no day yet
        time_label = re.search(r"Time:\s*(.{0,40})", body)
        times = parse_times(time_label.group(1)) if time_label else parse_times(body)
        loc_m = re.search(r"Location:\s*([^.•📅🕚]{3,60})", body)
        location = loc_m.group(1).strip() if loc_m else None
        details = body[:300].strip()
        events.append(make_event(
            f"isp-{title}-{day.isoformat()}", title, details,
            isp_category(title, details), day, times, location,
            first_link(chunk) or ISP_URL,
        ))
    return events


# ---- source: academic dates & deadlines --------------------------------------

DEADLINES_URL = f"{SITE}/calendar/dates-and-deadlines.html"


def quarter_start_year(season, year, month):
    """A quarter's list mixes years: 'Winter 2027' includes Nov 2026
    registration dates. Late-year months before a winter/spring quarter
    belong to the previous calendar year."""
    if season == "Winter" and month >= 5:
        return year - 1
    if season == "Spring" and month >= 7:
        return year - 1
    return year


DT_RANGE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2})(?:\s*-\s*(\d{1,2}))?")


def scrape_deadlines(today):
    # The include that fills the quarter lists is flaky server-side: the
    # page sometimes arrives with empty <dl> blocks. Retry a couple of
    # times before giving up (main() then falls back to the prior feed).
    page = fetch(DEADLINES_URL, fixture="deadlines.html")
    month_dt = r"<dt>\s*(January|February|March|April|May|June|July|August|September|October|November|December)"
    for _ in range(2):
        if re.search(month_dt, page) or os.environ.get("DASH_FIXTURES"):
            break
        time.sleep(5)
        page = fetch(DEADLINES_URL, fixture="deadlines.html")
    events = []
    quarters = re.finditer(
        r"<h3[^>]*>\s*(Fall|Winter|Spring|Summer)\s+(\d{4})\s*</h3>(.*?)</dl>",
        page, re.S)
    for quarter in quarters:
        season, year_text, block = quarter.groups()
        year = int(year_text)
        quarter_name = f"{season} {year}"
        group_days = []
        for dt_html, dd_html in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", block, re.S):
            dt_text, dd_text = strip_tags(dt_html), strip_tags(dd_html)
            range_m = DT_RANGE_RE.search(dt_text)
            month = month_number(range_m.group(1)) if range_m else None
            if not range_m or not month:
                continue
            actual_year = quarter_start_year(season, year, month)
            try:
                start_day = date(actual_year, month, int(range_m.group(2)))
                end_day = date(actual_year, month, int(range_m.group(3))) if range_m.group(3) else None
            except ValueError:
                continue
            # Collapse the per-group registration rows into one entry.
            if re.search(r"group \d+ registration|registration opens based on", dd_text, re.I):
                group_days.append(start_day)
                continue
            category = "general" if "holiday" in dd_text.lower() else categorize(dd_text, "")
            if category == "general" and "holiday" not in dd_text.lower():
                category = "deadline"
            events.append(make_event(
                f"ddl-{quarter_name}-{dd_text}", f"{quarter_name}: {dd_text}",
                f"{quarter_name} — from the academic Dates and Deadlines calendar.",
                category, start_day,
                None, None, first_link(dd_html) or DEADLINES_URL,
            ) | ({"end": end_day.isoformat()} if end_day else {}))
        if group_days:
            first, last = min(group_days), max(group_days)
            events.append(make_event(
                f"ddl-{quarter_name}-priority-registration",
                f"{quarter_name} Priority Registration Opens",
                (f"Registration opens by priority group, starting {first.strftime('%B %-d')} "
                 f"and continuing through {last.strftime('%B %-d')}. Find your group's "
                 "date and time ticket in MyPortal, and register as early as you can."),
                "deadline", first, None, None,
                f"{SITE}/apply-and-register/register/") | {"end": last.isoformat()})
    return events


# ---- source: iCal (kept from the original design) -----------------------------

def unfold_ics(raw):
    return re.sub(r"\r?\n[ \t]", "", raw)


def parse_ics_date(value):
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
        except ValueError:
            continue
    return None


def parse_ics(raw):
    events = []
    for block in unfold_ics(raw).split("BEGIN:VEVENT")[1:]:
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            fields[key.split(";", 1)[0]] = val.strip()
        title = fields.get("SUMMARY")
        start = parse_ics_date(fields.get("DTSTART", ""))
        if not title or start is None:
            continue
        end = parse_ics_date(fields.get("DTEND", "")) if fields.get("DTEND") else None
        details = fields.get("DESCRIPTION", "").replace("\\n", " ").replace("\\,", ",").strip()
        location = fields.get("LOCATION", "").replace("\\,", ",").strip() or None
        uid = fields.get("UID") or slugify(f"{title}-{start.isoformat()}")
        events.append({
            "id": slugify(uid),
            "title": title.replace("\\,", ",").strip(),
            "details": details,
            "category": categorize(title, details),
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if end else None,
            "location": location,
            "url": fields.get("URL") or None,
        })
    return events


# ---- assembly -----------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def start_day(event):
    return event["start"][:10]


def dedupe_slot(event):
    """Same happening, same day — regardless of which source worded it.
    A leading quarter label ('Fall 2026: ...') doesn't make it distinct."""
    title = re.sub(r"^(fall|winter|spring|summer)\s+\d{4}:?\s*", "",
                   event["title"], flags=re.I)
    return (slugify(title), start_day(event))


def main():
    today = datetime.now(CAMPUS_TZ).date()
    events = list(load_json(os.path.join(ROOT, "manual_events.json"), []))
    print(f"manual        : {len(events)}")

    # The previously published feed backstops each scraped source: when a
    # source errors or comes back empty (the site's data includes go down
    # now and then), its events from the last successful run are kept
    # instead of vanishing for a week. Ids are prefixed per source.
    prior = load_json(OUT, {}).get("events", [])

    sources = [
        ("ISP workshops", "isp-", lambda: scrape_isp(today)),
        ("campus events", "cal-", lambda: scrape_calendar(today)),
        ("deadlines    ", "ddl-", lambda: scrape_deadlines(today)),
    ]
    for name, prefix, scraper in sources:
        try:
            found = scraper()
        except Exception as exc:  # a broken source must not kill the feed
            print(f"{name}: FAILED — {exc}", file=sys.stderr)
            found = []
        if not found:
            found = [e for e in prior if e["id"].startswith(prefix)]
            if found:
                print(f"{name}: source empty — carried {len(found)} from previous feed")
                events.extend(found)
                continue
        events.extend(found)
        print(f"{name}: {len(found)}")

    for url in load_json(os.path.join(ROOT, "sources.json"), []):
        try:
            events.extend(parse_ics(fetch(url)))
            print(f"OK   {url}")
        except Exception as exc:
            print(f"SKIP {url}: {exc}", file=sys.stderr)

    # De-dupe by id, then by same title on the same day (the deadlines page
    # and the events calendar both list e.g. "Fall classes begin"). Keep
    # only events inside [today, today + HORIZON_DAYS], sorted by start.
    horizon = (today + timedelta(days=HORIZON_DAYS)).isoformat()
    today_iso = today.isoformat()
    seen_ids, seen_slots, kept = set(), set(), []
    for event in sorted(events, key=lambda e: e["start"]):
        slot = dedupe_slot(event)
        end_day = (event.get("end") or event["start"])[:10]
        if event["id"] in seen_ids or slot in seen_slots:
            continue
        if end_day < today_iso or start_day(event) > horizon:
            continue
        seen_ids.add(event["id"])
        seen_slots.add(slot)
        kept.append(event)

    updated = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "events": kept}, f, indent=2)
    print(f"Wrote {len(kept)} upcoming events to {os.path.basename(OUT)}")


if __name__ == "__main__":
    main()
