"""
fetch_brightspace.py — pull calendar feeds and write data/events.json

Run it:
    python scripts/fetch_brightspace.py

Inspect a feed before trusting it (dumps raw fields so you can see where the
course name actually lives in YOUR feed):
    python scripts/fetch_brightspace.py --inspect

Every feed in .env ending in _ICS gets pulled. Failures are recorded in the
output rather than raised, so one dead feed never blanks the whole dashboard.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import icalendar
import recurring_ical_events
import requests
import yaml
from dotenv import load_dotenv

LOCAL_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.yaml"

DAYS_BACK = 30
DAYS_AHEAD = 240

load_dotenv(ROOT / ".env")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    # encoding="utf-8" on EVERY read and write. On Windows the default is
    # cp1252, and one em-dash or checkmark in your config crashes the script.
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def get_feeds() -> dict[str, str]:
    """Any env var ending in _ICS becomes a feed. Add one line to .env, done."""
    feeds = {}
    for key, value in os.environ.items():
        if key.endswith("_ICS") and value.strip():
            feeds[key[:-4].replace("_", " ").title()] = value.strip()
    return feeds


def normalize_url(url: str) -> str:
    """Calendar apps hand out webcal:// links; requests only speaks http(s)."""
    return "https://" + url[len("webcal://"):] if url.startswith("webcal://") else url


def download(url: str) -> icalendar.Calendar:
    response = requests.get(normalize_url(url), timeout=30)
    response.raise_for_status()
    return icalendar.Calendar.from_ical(response.content)


def to_local(value: datetime) -> datetime:
    return value.replace(tzinfo=LOCAL_TZ) if value.tzinfo is None else value.astimezone(LOCAL_TZ)


def tag_for(title: str, categories: str, location: str, classes: list[dict]) -> str | None:
    """
    Attach a course code to an event.

    Canvas puts the course in CATEGORIES. Brightspace may not put it in
    SUMMARY or CATEGORIES at all — --inspect on a real UVM Brightspace feed
    showed it living in LOCATION instead (e.g. "STAT1870A: Intro to Data
    Science"). So we search all three, plus every 'match' string you list
    for the class in config.yaml.
    """
    haystack = f"{title} {categories} {location}".lower()
    for course in classes:
        needles = [course.get("tag", "")] + list(course.get("match", []) or [])
        for needle in needles:
            if needle and needle.lower() in haystack:
                return course.get("tag")
    return None


def extract_events(name: str, calendar: icalendar.Calendar, classes: list[dict],
                   start: date, end: date) -> list[dict]:
    """
    Expand the calendar into concrete dated events.

    recurring_ical_events does the part that is NOT worth hand-writing: a
    Tue/Thu lecture is stored in the file ONCE with a 'repeat weekly until
    December' rule. Expanding that correctly is most of RFC 5545, not 60 lines.
    Without it your assignments work and your classes appear once, in August.
    """
    events = []
    for component in recurring_ical_events.of(calendar).between(start, end + timedelta(days=1)):
        dtstart_field = component.get("DTSTART")
        if dtstart_field is None:
            continue
        dtstart = dtstart_field.dt
        dtend_field = component.get("DTEND")
        dtend = dtend_field.dt if dtend_field is not None else None

        # A datetime IS a date in Python, so test for datetime first.
        # A bare date means the feed is saying "all-day thing."
        all_day = not isinstance(dtstart, datetime)

        if all_day:
            day, start_s, end_s = dtstart, None, None
        else:
            start_dt = to_local(dtstart)
            end_dt = to_local(dtend) if isinstance(dtend, datetime) else None
            day = start_dt.date()
            start_s = start_dt.strftime("%H:%M")
            end_s = end_dt.strftime("%H:%M") if end_dt else None

        title = str(component.get("SUMMARY", "(untitled)"))
        categories = str(component.get("CATEGORIES", ""))
        location = str(component.get("LOCATION", "")).strip()

        events.append({
            "title": title,
            "day": day.isoformat(),
            "start": start_s,
            "end": end_s,
            "all_day": all_day,
            "location": location,
            "description": str(component.get("DESCRIPTION", "")).strip()[:500],
            "source": name,
            "tag": tag_for(title, categories, location, classes),
        })
    return events


def inspect_feeds(feeds: dict[str, str], limit: int = 5) -> None:
    """
    Dump raw fields from a few real events.

    Do this BEFORE building anything that depends on class tagging. You need to
    see with your own eyes whether your course code lands in CATEGORIES or
    SUMMARY, because Brightspace and Canvas differ here.
    """
    for name, url in feeds.items():
        print(f"\n{'=' * 60}\nFEED: {name}\n{'=' * 60}")
        try:
            calendar = download(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            continue

        shown = 0
        for component in calendar.walk("VEVENT"):
            if shown >= limit:
                break
            shown += 1
            print(f"\n  --- event {shown} ---")
            for field in ("SUMMARY", "CATEGORIES", "DTSTART", "DTEND",
                          "LOCATION", "RRULE", "UID", "DESCRIPTION"):
                if field in component:
                    value = str(component.get(field))
                    print(f"  {field:12} {value[:110]}")
        if shown == 0:
            print("  No VEVENTs found. Feed is reachable but empty.")
            print("  In early August that is NORMAL — fall courses may not be published yet.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true",
                        help="print raw fields from a few events and exit")
    args = parser.parse_args()

    feeds = get_feeds()
    if not feeds:
        print("No feeds found. Add lines ending in _ICS to your .env file.")
        print("If .env exists, check it is not actually named .env.txt — Windows hides extensions.")
        return 1

    if args.inspect:
        inspect_feeds(feeds)
        return 0

    config = load_config()
    classes = config.get("classes", []) or []

    today = date.today()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_AHEAD)

    all_events: list[dict] = []
    status: dict[str, dict] = {}

    for name, url in feeds.items():
        try:
            calendar = download(url)
            events = extract_events(name, calendar, classes, start, end)
            all_events.extend(events)
            status[name] = {"status": "connected", "count": len(events), "error": None}
            print(f"  {name}: {len(events)} events")
        except Exception as exc:  # noqa: BLE001
            status[name] = {"status": "error", "count": 0,
                            "error": f"{type(exc).__name__}: {exc}"}
            print(f"  {name}: FAILED — {exc}")

    all_events.sort(key=lambda e: (e["day"], e["start"] or ""))

    payload = {
        "generated": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "feeds": status,
        "events": all_events,
    }

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / "events.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {len(all_events)} events to {out}")

    untagged = sum(1 for e in all_events if e["tag"] is None)
    if all_events and untagged == len(all_events) and classes:
        print("\nWARNING: nothing matched a class tag.")
        print("Run with --inspect to see where your course code actually appears,")
        print("then fix the 'match' values in config.yaml.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
