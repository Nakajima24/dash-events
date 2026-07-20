#!/usr/bin/env python3
"""
Builds events.json for the DASH app's Events page.

Design goal: always produce a valid feed with zero fragile scraping as the
baseline. Two sources are merged:

  1. manual_events.json  — hand-maintained entries you fully control. Use
     this for international-student deadlines (CPT/OPT, SEVIS, tuition) that
     are easiest to enter by hand. This alone makes the feature work.

  2. sources.json        — a list of iCal (.ics) calendar URLs. De Anza and
     most colleges publish event calendars as .ics; iCal is a stable,
     structured format, so parsing it doesn't break when the website's HTML
     changes. Anything found here is added to the manual entries.

The app reads the resulting events.json from this repo's raw URL. A GitHub
Action runs this weekly and commits the file, so every install syncs.

Standard library only — no pip install, so the Action stays simple.
"""

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
OUT = ROOT / "events.json"

# Keyword → category rules, checked in order; first match wins. Tune these
# to how De Anza titles its events.
CATEGORY_RULES = [
    ("international", ["international", "f-1", "f1", "cpt", "opt", "sevis", "visa", "isp", "i-20", "i20"]),
    ("deadline",      ["deadline", "last day", "due", "registration opens", "add/drop", "tuition", "payment", "apply by"]),
    ("career",        ["career", "job fair", "internship", "employer", "resume", "hiring"]),
    ("workshop",      ["workshop", "tutoring", "advising", "orientation", "info session", "seminar"]),
    ("wellness",      ["health", "wellness", "counsel", "clinic", "mental", "flu shot", "vaccine"]),
    ("social",        ["club", "fair", "mixer", "festival", "celebration", "social", "party", "meetup"]),
]


def categorize(title, details):
    text = f"{title} {details}".lower()
    for category, keywords in CATEGORY_RULES:
        if any(keyword in text for keyword in keywords):
            return category
    return "general"


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


# ---- iCal parsing (minimal, stdlib only) -----------------------------------

def unfold_ics(raw):
    """iCal folds long lines with a leading space/tab on the continuation."""
    return re.sub(r"\r?\n[ \t]", "", raw)


def parse_ics_date(value):
    """Handles 'YYYYMMDDTHHMMSSZ', 'YYYYMMDDTHHMMSS', and all-day 'YYYYMMDD'."""
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
            key = key.split(";", 1)[0]  # drop params like DTSTART;VALUE=DATE
            fields[key] = val.strip()
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


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dash-events-bot"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def main():
    events = list(load_json(ROOT / "manual_events.json", []))

    for url in load_json(ROOT / "sources.json", []):
        try:
            events.extend(parse_ics(fetch(url)))
            print(f"OK   {url}")
        except Exception as exc:  # keep going; a bad source shouldn't fail the run
            print(f"SKIP {url}: {exc}", file=sys.stderr)

    # De-dupe by id, keep only events that haven't ended, sort by start.
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    seen, kept = set(), []
    for event in sorted(events, key=lambda e: e["start"]):
        if event["id"] in seen:
            continue
        if (event.get("end") or event["start"]) < now:
            continue
        seen.add(event["id"])
        kept.append(event)

    OUT.write_text(json.dumps({"updated": now, "events": kept}, indent=2))
    print(f"Wrote {len(kept)} upcoming events to {OUT.name}")


if __name__ == "__main__":
    main()
