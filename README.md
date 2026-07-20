# dash-events — the DASH events relay

This small repo generates `events.json`, the weekly feed the DASH app shows
on the mascot's **Events** page. A GitHub Action rebuilds it every Monday
and commits it; the app fetches it from this repo's raw URL, so one weekly
update reaches every user with no App Store release.

## One-time setup

1. Create a **public** GitHub repo named `dash-events` and put these files
   in it (`scrape_events.py`, `manual_events.json`, `sources.json`,
   `.github/workflows/update-events.yml`).
2. In the repo: **Settings → Actions → General → Workflow permissions →
   Read and write permissions** (lets the Action commit the file).
3. Run it once: **Actions → Update events feed → Run workflow**. This
   creates `events.json`.
4. Copy the raw URL of the committed file — it looks like:
   `https://raw.githubusercontent.com/<your-username>/dash-events/main/events.json`
5. In the app, open `DeAnza/Model/EventService.swift` and set `feedURL` to
   that URL. Ship the app once. Done — after that, the feed updates itself.

## Editing the feed

- **`manual_events.json`** — hand-entered events you fully control (best for
  international-student deadlines). Edit and commit; the next run picks them
  up. The two sample entries are placeholders — replace them.
- **`sources.json`** — a list of iCal (`.ics`) calendar URLs. The scraper
  parses these and merges them in. Confirm De Anza's real calendar `.ics`
  URL and put it here; remove the sample if it 404s. (Open the campus
  calendar in a browser and look for a "Subscribe"/iCal link.)

You never have to touch the app to change events — only this repo.

## Event shape

```json
{
  "updated": "2026-07-19T08:00:00Z",
  "events": [
    {
      "id": "unique-stable-id",
      "title": "OPT Application Workshop",
      "details": "Optional longer description.",
      "category": "international",
      "start": "2026-09-15T17:00:00Z",
      "end": "2026-09-15T18:30:00Z",
      "location": "ISP Office",
      "url": "https://www.deanza.edu/international/"
    }
  ]
}
```

`category` is one of: `international`, `deadline`, `workshop`, `social`,
`career`, `wellness`, `general`. Unknown values fall back to `general` in
the app. `end`, `location`, and `url` may be `null`. Dates are ISO-8601
(UTC `Z`); a bare `YYYY-MM-DD` is accepted for all-day items.

## How categories map to notifications

The app schedules a local reminder a day before each event, but only for
categories the user has switched on in **Profile → Event Notifications**.
Choosing the right `category` is what lets users mute, say, social events
while keeping international-student deadlines.
