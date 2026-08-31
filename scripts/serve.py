"""
serve.py — serves the dashboard, owns the to-do file, and backs the AI chat
bubble (to-dos + config.yaml only — it never touches this file or the other
Python scripts; see build_system_prompt() for the boundary the model is told).

This exists for exactly one reason: a browser page cannot write to your disk.
That is a security boundary, not a setting. So this ~100 lines of standard
library stands between the page and data/todos.json.

    python scripts/serve.py     ->  http://127.0.0.1:8787

Durability, since that was the requirement:
  - writes go to a temp file, then os.replace() renames it over the real one.
    Rename is atomic on Windows and POSIX, so a crash or power cut mid-write
    leaves you with either the old file or the new one, never a half-written one.
  - the previous version is kept as todos.backup.json before every write.
    config.yaml gets the same treatment before the chat assistant edits it.

Bound to 0.0.0.0 so it's reachable over Tailscale (your private device mesh —
see the phone-access setup), but a Windows Firewall rule restricts inbound
connections on this port to Tailscale's own address range only
(100.64.0.0/10). Nothing on your regular home network or the public internet
can reach it — only devices signed into your own Tailscale account.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anthropic
import yaml
from anthropic import beta_tool
from dotenv import load_dotenv
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials as GoogleCredentials
from googleapiclient.discovery import build as build_calendar_service
from googleapiclient.errors import HttpError
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DASHBOARD_DIR = ROOT / "dashboard"
TODO_FILE = DATA_DIR / "todos.json"
BACKUP_FILE = DATA_DIR / "todos.backup.json"
CONFIG_FILE = ROOT / "config.yaml"
CONFIG_BACKUP = ROOT / "config.backup.yaml"

HOST = "0.0.0.0"  # firewall rule (see setup) restricts this to Tailscale only
PORT = 8787
CHAT_MODEL = "claude-opus-5"

load_dotenv(ROOT / ".env")

# Google Calendar reminders — see BUILD.md, 'Google Calendar reminders' for
# the one-time Cloud Console setup that produces these three values.
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
GOOGLE_CALENDAR_CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "")
GOOGLE_CALENDAR_CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "")
GOOGLE_CALENDAR_REFRESH_TOKEN = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN", "")

_write_lock = threading.Lock()
_config_lock = threading.Lock()
_chat_lock = threading.Lock()
_chat_history: list = []  # in-memory only — resets when you restart this server

# ruamel.yaml, not PyYAML, for any edit that loads config.yaml as structured
# data and writes it back (add_link/remove_link below). PyYAML's safe_load +
# safe_dump round-trip silently drops every comment in the file — and this
# file's comments are the schema documentation. ruamel preserves them.
_yaml_rt = YAML()
_yaml_rt.preserve_quotes = True
_yaml_rt.width = 100000  # don't auto-wrap long URLs onto a second line


def load_config_rt():
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return _yaml_rt.load(f)


def save_config_rt(data) -> None:
    if CONFIG_FILE.exists():
        shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
    with CONFIG_FILE.open("w", encoding="utf-8") as f:
        _yaml_rt.dump(data, f)


def rebuild_dashboard() -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_dashboard.py")],
        capture_output=True, text=True, timeout=30, cwd=str(ROOT),
    )
    return result.returncode == 0, result.stdout + result.stderr


def add_link(workspace_id: str, label: str, url: str) -> dict:
    with _config_lock:
        try:
            cfg = load_config_rt()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Could not read config.yaml: {exc}"}

        workspace = next((w for w in cfg.get("workspaces", []) if w.get("id") == workspace_id), None)
        if workspace is None:
            return {"error": f"No workspace with id {workspace_id!r}."}

        links = workspace.get("links")
        if links is None:
            links = []
            workspace["links"] = links
        links.append({"label": label, "url": url})

        save_config_rt(cfg)
        ok, log = rebuild_dashboard()
        if not ok:
            if CONFIG_BACKUP.exists():
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
            return {"error": f"Rebuild failed, rolled back: {log}"}
    return {"ok": True}


def remove_link(workspace_id: str, label: str) -> dict:
    with _config_lock:
        try:
            cfg = load_config_rt()
        except Exception as exc:  # noqa: BLE001
            return {"error": f"Could not read config.yaml: {exc}"}

        workspace = next((w for w in cfg.get("workspaces", []) if w.get("id") == workspace_id), None)
        if workspace is None:
            return {"error": f"No workspace with id {workspace_id!r}."}

        links = workspace.get("links") or []
        before = len(links)
        workspace["links"] = [l for l in links if l.get("label") != label]
        if len(workspace["links"]) == before:
            return {"error": f"No link labeled {label!r} in workspace {workspace_id!r}."}

        save_config_rt(cfg)
        ok, log = rebuild_dashboard()
        if not ok:
            if CONFIG_BACKUP.exists():
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
            return {"error": f"Rebuild failed, rolled back: {log}"}
    return {"ok": True}


def read_todos() -> list[dict]:
    if not TODO_FILE.exists():
        return []
    try:
        return json.loads(TODO_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt file: fall back to the backup rather than silently losing work.
        if BACKUP_FILE.exists():
            try:
                return json.loads(BACKUP_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return []


def write_todos(todos: list[dict]) -> None:
    """Atomic write. See the module docstring for why this matters."""
    with _write_lock:
        DATA_DIR.mkdir(exist_ok=True)
        if TODO_FILE.exists():
            shutil.copy2(TODO_FILE, BACKUP_FILE)
        tmp = TODO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, TODO_FILE)


def _reminders_config() -> dict:
    try:
        cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a bad config shouldn't break to-do saving
        return {}
    return cfg.get("reminders") or {}


def _calendar_id() -> str:
    return (_reminders_config().get("calendar_id") or "primary").strip() or "primary"


def _calendar_enabled() -> bool:
    return bool(_reminders_config().get("enabled"))


def _parse_hhmm(raw: str) -> dt_time | None:
    try:
        hh, mm = raw.strip().split(":")
        return dt_time(int(hh), int(mm))
    except (ValueError, AttributeError):
        return None


def _default_send_time() -> dt_time:
    """`reminders.send_time` from config.yaml, e.g. "08:00" — the fallback
    used by any to-do that doesn't set its own remind_time. Malformed or
    missing values fall back to 08:00 rather than breaking the scheduler."""
    return _parse_hhmm(str(_reminders_config().get("send_time") or "")) or dt_time(8, 0)


def _todo_send_time(todo: dict) -> dt_time:
    """A to-do's own `remind_time` (24h "HH:MM"), if it set one, else the
    global default. This is what makes 'remind me Friday at 3pm' land at
    3pm specifically instead of everyone sharing one send_time."""
    return _parse_hhmm(str(todo.get("remind_time") or "")) or _default_send_time()


def _format_12h(t: dt_time) -> str:
    """12-hour clock for display ('8:30 AM', '6:00 PM'), added 2026-08-23
    per Charles's request -- everywhere a time is actually shown to a
    person, not the stored/API format. That stays 24h "HH:MM" everywhere it
    always was (remind_time on disk, the Calendar API, the <input
    type="time"> value, add_todo/update_todo's tool params) -- only display
    text changes. Mirrored by formatTime12h() in the generated JS."""
    hour12 = t.hour % 12 or 12
    ampm = "AM" if t.hour < 12 else "PM"
    return f"{hour12}:{t.minute:02d} {ampm}"


def _class_address(tag: str | None) -> str | None:
    """The `address` configured for a class tag in config.yaml, if any. This
    is what lets a class-tagged to-do or schedule block get directions
    without the address being typed in every time — one config entry, reused
    everywhere that tag shows up. Returns None if the tag doesn't match a
    class, or the class has no address set (never guess one)."""
    if not tag:
        return None
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a bad config shouldn't break sending
        return None
    for c in (config.get("classes") or []):
        if c.get("tag") == tag:
            addr = (c.get("address") or "").strip()
            return addr or None
    return None


def _calendar_creds() -> GoogleCredentials | None:
    """Loads and refreshes Google Calendar OAuth credentials from the three
    .env values produced by the one-time `scripts/google_calendar_auth.py`
    setup (see BUILD.md, 'Google Calendar reminders'). Returns None if
    they're missing or refreshing fails — callers treat that as 'not
    configured' rather than raising, same as every other integration here."""
    if not (GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET and GOOGLE_CALENDAR_REFRESH_TOKEN):
        return None
    try:
        creds = GoogleCredentials(
            None,
            refresh_token=GOOGLE_CALENDAR_REFRESH_TOKEN,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CALENDAR_CLIENT_ID,
            client_secret=GOOGLE_CALENDAR_CLIENT_SECRET,
            scopes=CALENDAR_SCOPES,
        )
        creds.refresh(GoogleAuthRequest())
        return creds
    except Exception:  # noqa: BLE001 - "not configured" is the safe fallback
        return None


def _event_start(todo: dict) -> datetime:
    """The to-do's due date at its own remind_time (or config.yaml's
    default send_time) as a local-timezone-aware datetime. `.astimezone()`
    on a naive datetime assumes it means local time and attaches the
    correct offset — no IANA timezone database needed, which matters on
    Windows (see BUILD.md gotchas)."""
    due_date = date.fromisoformat(todo["due"])
    naive = datetime.combine(due_date, _todo_send_time(todo))
    return naive.astimezone()


def _event_body(todo: dict) -> dict:
    """The Calendar API event body for one to-do. `location`, if set, is
    what gives the phone notification a native 'get directions' tap target
    — Google Calendar (and Apple Calendar, if it's synced in) render a
    plain address in this field as a tappable map, no custom maps link
    needed. An explicit todo.location wins; otherwise fall back to the
    tagged class's configured address (see _class_address)."""
    start = _event_start(todo)
    end = start + timedelta(minutes=30)
    location = (todo.get("location") or "").strip() or _class_address(todo.get("tag")) or ""

    description_lines = []
    if todo.get("tag"):
        description_lines.append(f"Class: {todo['tag']}")
    if todo.get("notes"):
        description_lines.append(todo["notes"])

    # Fires exactly at the scheduled moment, not some default lead time —
    # matches how remind_time / send_time are described everywhere else.
    # An exam gets two extra heads-up popups, 1 and 2 weeks before, on top of
    # the day-of one — added the same event so it's still one Calendar item,
    # not three. Google allows overrides up to 28 days out, so 14 days is safe.
    overrides = [{"method": "popup", "minutes": 0}]
    if todo.get("is_exam"):
        overrides.append({"method": "popup", "minutes": 7 * 24 * 60})
        overrides.append({"method": "popup", "minutes": 14 * 24 * 60})

    body = {
        "summary": todo.get("title") or "To-do",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
        "reminders": {"useDefault": False, "overrides": overrides},
    }
    if location:
        body["location"] = location
    if description_lines:
        body["description"] = "\n".join(description_lines)
    return body


def create_calendar_event(todo: dict) -> tuple[str | None, str]:
    """Creates a Calendar event for a dated to-do. Returns (event_id, detail);
    event_id is None on failure. Never raises — a Calendar problem must
    never block saving the to-do itself."""
    creds = _calendar_creds()
    if creds is None:
        return None, ("Google Calendar not configured — see BUILD.md, "
                       "'Google Calendar reminders'")
    try:
        service = build_calendar_service("calendar", "v3", credentials=creds, cache_discovery=False)
        event = service.events().insert(calendarId=_calendar_id(), body=_event_body(todo)).execute()
        return event["id"], "created"
    except Exception as exc:  # noqa: BLE001 - surfaced to caller, never raised
        return None, f"{type(exc).__name__}: {exc}"


def update_calendar_event(todo: dict) -> tuple[bool, str]:
    """Updates the existing Calendar event for a to-do. detail is the
    literal string "404" when the event is gone (deleted on the Google
    Calendar side) — callers fall back to create_calendar_event on that."""
    creds = _calendar_creds()
    if creds is None:
        return False, ("Google Calendar not configured — see BUILD.md, "
                        "'Google Calendar reminders'")
    try:
        service = build_calendar_service("calendar", "v3", credentials=creds, cache_discovery=False)
        service.events().update(
            calendarId=_calendar_id(), eventId=todo["calendar_event_id"], body=_event_body(todo)
        ).execute()
        return True, "updated"
    except HttpError as exc:
        if exc.resp.status == 404:
            return False, "404"
        return False, f"HttpError {exc.resp.status}: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def delete_calendar_event(event_id: str | None) -> None:
    """Best-effort delete — a stale/already-gone event isn't harmful, so
    failures here are swallowed rather than surfaced."""
    creds = _calendar_creds()
    if creds is None or not event_id:
        return
    try:
        service = build_calendar_service("calendar", "v3", credentials=creds, cache_discovery=False)
        service.events().delete(calendarId=_calendar_id(), eventId=event_id).execute()
    except Exception:  # noqa: BLE001
        pass


def reconcile_todo_calendar_event(old: dict | None, new: dict) -> str | None:
    """Creates, updates, or deletes `new`'s Calendar event as needed, and
    writes the resulting id back onto `new["calendar_event_id"]`. Returns
    an error string on failure, or None on success/no-op.

    A to-do wants an event only while it's dated and not done — marking one
    done or clearing its due date deletes the event, since there's nothing
    left to be reminded about. Skips the API call entirely when nothing
    calendar-relevant changed, so routine saves (an unrelated field edit,
    toggling a *different* to-do's `done`) don't re-sync every dated to-do
    on every save.
    """
    wants_event = bool(new.get("due")) and not new.get("done") and _calendar_enabled()
    has_event = bool(new.get("calendar_event_id"))

    if not wants_event:
        if has_event:
            delete_calendar_event(new["calendar_event_id"])
            new["calendar_event_id"] = None
        return None

    fields = ("due", "remind_time", "title", "notes", "tag", "location", "done", "is_exam")
    changed = old is None or any(old.get(f) != new.get(f) for f in fields)
    if not changed and has_event:
        return None

    if has_event:
        ok, detail = update_calendar_event(new)
        if ok:
            return None
        if detail != "404":
            return detail
        # The event was deleted on the Calendar side — fall through to create
        # a fresh one rather than leaving this to-do unreminded.
    event_id, detail = create_calendar_event(new)
    if event_id:
        new["calendar_event_id"] = event_id
        return None
    return detail


def read_events() -> dict:
    events_file = DATA_DIR / "events.json"
    if not events_file.exists():
        return {"generated": None, "feeds": {}, "events": [],
                "error": "No events.json yet. Run: python scripts/fetch_brightspace.py"}
    try:
        return json.loads(events_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"generated": None, "feeds": {}, "events": [],
                "error": f"events.json is unreadable: {exc}"}


def refresh_events() -> dict:
    """
    Re-run the fetch script, then rebuild the dashboard so the refreshed week
    strip / deadlines / feed status panel are in the HTML the next page load
    serves — same pattern as refresh_grades() below, added 2026-08-23
    alongside the dashboard's new "Refresh events" button (previously this
    only updated events.json; a plain page reload wouldn't have picked
    anything up, since the client's event data is baked into the HTML at
    build time, not fetched live).
    """
    script = ROOT / "scripts" / "fetch_brightspace.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=120, cwd=str(ROOT),
        )
        fetch_error = None if result.returncode == 0 else result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        fetch_error = "Fetch timed out after 120s."

    ok, log = rebuild_dashboard()
    payload = read_events()
    if fetch_error:
        payload["refresh_error"] = fetch_error
    elif not ok:
        payload["refresh_error"] = f"Dashboard rebuild failed: {log}"
    return payload


def read_grades() -> dict:
    grades_file = DATA_DIR / "grades.json"
    if not grades_file.exists():
        return {"generated": None, "classes": {},
                "error": "No grades.json yet. Run: python scripts/fetch_grades.py"}
    try:
        return json.loads(grades_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"generated": None, "classes": {},
                "error": f"grades.json is unreadable: {exc}"}


def refresh_grades() -> dict:
    """
    Re-run the grades scraper, then rebuild the dashboard so the refreshed
    panel is actually in the HTML the next page load serves — see
    render_grades() in build_dashboard.py, which reads grades.json at BUILD
    time, not client-side, same as the deadlines and quick-links panels.

    Headless only (no --login): a browser request can't sit at a terminal
    prompt. If the saved Brightspace session has expired, fetch_grades.py
    reports "login_required" per class instead of hanging, and you re-run
    `python scripts/fetch_grades.py --login` yourself at the keyboard.
    """
    script = ROOT / "scripts" / "fetch_grades.py"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT),
        )
        fetch_error = None if result.returncode == 0 else result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        fetch_error = "Grades fetch timed out after 180s."

    ok, log = rebuild_dashboard()
    payload = read_grades()
    if fetch_error:
        payload["refresh_error"] = fetch_error
    elif not ok:
        payload["refresh_error"] = f"Dashboard rebuild failed: {log}"
    return payload


# ---------------------------------------------------------------------------
# AI chat bubble — tool functions.
#
# Deliberately scoped to data/todos.json and config.yaml ONLY. It cannot edit
# this file or the other Python scripts, and it cannot run shell commands —
# see build_system_prompt(). That boundary is what keeps a bad model response
# from breaking the server that's running it.
# ---------------------------------------------------------------------------

def _new_todo_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


REPEAT_FREQUENCIES = ("daily", "weekly", "biweekly", "monthly")
MAX_RECURRENCE_OCCURRENCES = 104  # 2 years weekly -- a generous cap, not a real limit


def _add_months(d: date, k: int) -> date:
    """`d`'s day-of-month, `k` months later, clamped to that month's last day
    (e.g. Jan 31 + 1 month -> Feb 28). Always anchored to `d`'s ORIGINAL day,
    not chained off a previous (possibly already-clamped) occurrence -- so a
    31st-of-the-month to-do still lands on the 31st in Jan/Mar/May/... even
    though Feb clamped it to 28. Chaining from the prior occurrence instead
    would drift every later occurrence down to the 28th permanently, which
    is the bug this anchoring avoids."""
    import calendar as _calendar
    total = d.month - 1 + k
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = _calendar.monthrange(year, month)[1]
    return d.replace(year=year, month=month, day=min(d.day, last_day))


def expand_recurrence(due: str, freq: str, until: str,
                       max_count: int = MAX_RECURRENCE_OCCURRENCES) -> list[str]:
    """Materializes concrete occurrence dates from `due` (inclusive) through
    `until` (inclusive), stepping by `freq`. Recurring to-dos in this app are
    real, independent rows -- one per occurrence, each with its own id and
    Calendar event -- not a virtual repeating rule, so marking or deleting
    one occurrence never touches the others. Returns just [due] if freq/until
    don't make sense (bad freq, until before due), same 'never invent, never
    silently do something unexpected' spirit as the rest of this file.

    Every occurrence is computed from `start` directly (step count k, not a
    running `current` reassigned each loop) so there's no drift accumulation
    -- see _add_months for why that matters specifically for monthly."""
    if freq not in REPEAT_FREQUENCIES:
        return [due]
    try:
        start = date.fromisoformat(due)
        end = date.fromisoformat(until)
    except (ValueError, TypeError):
        return [due]
    if end < start:
        return [due]

    step_days = {"daily": 1, "weekly": 7, "biweekly": 14}
    out = [start.isoformat()]
    k = 1
    while len(out) < max_count:
        current = _add_months(start, k) if freq == "monthly" \
            else start + timedelta(days=step_days[freq] * k)
        if current > end:
            break
        out.append(current.isoformat())
        k += 1
    return out


def _new_repeat_group_id() -> str:
    import uuid
    return uuid.uuid4().hex[:8]


@beta_tool
def list_todos() -> str:
    """List every to-do currently stored: id, title, due date (or null if
    unscheduled), notes, course tag, and done status."""
    return json.dumps(read_todos(), ensure_ascii=False)


@beta_tool
def add_todo(title: str, due: str | None = None, notes: str = "", tag: str | None = None,
             remind_time: str | None = None, location: str | None = None,
             is_exam: bool = False, repeat_freq: str | None = None,
             repeat_until: str | None = None) -> str:
    """Add a new to-do.

    Args:
        title: Short description of the to-do.
        due: Due date as YYYY-MM-DD. Omit for an unscheduled to-do.
        notes: Optional extra detail.
        tag: Optional course tag (e.g. "PSYS-2100") matching a class tag in config.yaml.
        remind_time: Optional 24h "HH:MM" time for the Google Calendar reminder
            ON the due date — use this whenever the user names a specific time
            ("remind me Friday at 3pm" -> due=that Friday, remind_time="15:00").
            Ignored if due isn't set. If omitted, falls back to config.yaml's
            reminders.send_time.
        is_exam: Set True for an exam/midterm/final, of ANY class — this adds
            two EXTRA Google Calendar popup reminders, 1 week and 2 weeks
            before the due date, on top of the normal day-of one, so there's
            real time to study rather than a same-day surprise. Always set
            this for exams; leave False for ordinary to-dos and assignments.
        location: Where this to-do needs to happen, ONLY when the user
            actually named a destination — either a SPECIFIC place ("rock
            climbing at Pacific Pipe", "drive down to Monterey California":
            pass the exact text they gave, place name plus city/state if
            they said it) or a general TYPE of place ("pick up a hammer
            from a hardware store", "grab my prescription from a
            pharmacy": pass the category verbatim, e.g. "hardware store" —
            do NOT ask which one or invent a specific store name; the
            directions link resolves to the closest match live, wherever
            the phone actually is when it's tapped, which is the point of
            leaving it generic). Never invent or guess either kind. This
            becomes the Calendar event's Location, which phone calendar
            apps show as a tappable "get directions" map. Leave unset for
            anything without a real destination, and for classes — tagging
            with `tag` already gets a class's address automatically from
            config.yaml, no need to repeat it here.
        repeat_freq: Set to make this a RECURRING to-do — one of "daily",
            "weekly", "biweekly", "monthly". Requires both `due` (the first
            occurrence) and `repeat_until`. Use this whenever the user
            describes a repeating pattern ("every Monday", "weekly reading
            until finals", "the 1st of every month"). This creates several
            independent to-do rows, one per occurrence (each with its own
            Calendar reminder) — NOT one repeating rule, so completing or
            deleting one occurrence never affects the others. Omit for a
            one-off to-do.
        repeat_until: Last possible due date (YYYY-MM-DD, inclusive) for a
            recurring to-do — required together with repeat_freq. Only ask
            the user for this if they haven't implied an end point (a
            semester end date, "until the final", a specific date);
            otherwise infer it from what they said. Capped internally at 104
            occurrences as a safety limit, not something to mention unless
            it's actually hit.
    """
    todos = read_todos()
    repeat_group = None
    if repeat_freq and repeat_until and due:
        occurrence_dates = expand_recurrence(due, repeat_freq, repeat_until)
        if len(occurrence_dates) > 1:
            repeat_group = _new_repeat_group_id()
    else:
        occurrence_dates = [due] if due else [None]

    new_todos = []
    cal_errors = []
    for occ_due in occurrence_dates:
        t = {
            "id": _new_todo_id(),
            "title": title,
            "due": occ_due,
            "notes": notes,
            "tag": tag,
            "done": False,
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "remind_time": remind_time,
            "location": location,
            "is_exam": is_exam,
            "repeat_group": repeat_group,
            "calendar_event_id": None,
        }
        err = reconcile_todo_calendar_event(None, t)
        if err:
            cal_errors.append(err)
        new_todos.append(t)

    todos.extend(new_todos)
    write_todos(todos)

    if len(new_todos) > 1:
        first, last = new_todos[0]["due"], new_todos[-1]["due"]
        result = (f"Added {len(new_todos)} occurrences of {title!r} "
                  f"({repeat_freq}, {first} through {last}).")
        if cal_errors:
            result += f" {len(cal_errors)} Google Calendar sync(s) failed."
        return result

    new_todo = new_todos[0]
    suffix = f" due {due}." if due else " (unscheduled)."
    if due and _calendar_enabled():
        if new_todo.get("calendar_event_id"):
            t = _todo_send_time(new_todo)
            suffix += f" Added to Google Calendar with a reminder at {_format_12h(t)}."
        elif cal_errors:
            suffix += f" Google Calendar sync failed: {cal_errors[0]}"
    return f"Added to-do {new_todo['id']}: {title!r}" + suffix


@beta_tool
def delete_series(repeat_group: str) -> str:
    """Delete every occurrence of a recurring to-do series (all to-dos
    sharing this repeat_group, done or not), removing their Google Calendar
    events too. Get repeat_group from list_todos — it's null on ordinary,
    non-recurring to-dos."""
    if not repeat_group:
        return "No repeat_group given — that's not a recurring to-do."
    todos = read_todos()
    matched = [t for t in todos if t.get("repeat_group") == repeat_group]
    if not matched:
        return f"No to-dos found with repeat_group {repeat_group!r}."
    for t in matched:
        if t.get("calendar_event_id"):
            delete_calendar_event(t["calendar_event_id"])
    write_todos([t for t in todos if t.get("repeat_group") != repeat_group])
    return f"Deleted {len(matched)} occurrence(s) of the series."


@beta_tool
def update_todo(id: str, title: str | None = None, due: str | None = None,
                 notes: str | None = None, tag: str | None = None,
                 done: bool | None = None, remind_time: str | None = None,
                 location: str | None = None, is_exam: bool | None = None) -> str:
    """Update fields on an existing to-do by id. Only the arguments you pass are
    changed — omit anything you don't want to touch.

    Args:
        id: The to-do's id, from list_todos.
        title: New title.
        due: New due date as YYYY-MM-DD, or the literal string "null" to unschedule it.
        notes: New notes.
        tag: New course tag.
        done: New done status.
        remind_time: New 24h "HH:MM" reminder time for this to-do specifically,
            or the literal string "null" to go back to using config.yaml's
            default send_time. Only meaningful while the to-do still has a
            due date.
        location: New destination for the Calendar event's Location (see
            add_todo), or the literal string "null" to remove it.
        is_exam: Set True to turn on the extra 1-week/2-week-out Calendar
            reminders (see add_todo); False to turn them back off.
    """
    todos = read_todos()
    for t in todos:
        if t["id"] == id:
            old = dict(t)
            if title is not None:
                t["title"] = title
            if due is not None:
                t["due"] = None if due == "null" else due
            if notes is not None:
                t["notes"] = notes
            if tag is not None:
                t["tag"] = tag
            if done is not None:
                t["done"] = done
            if remind_time is not None:
                t["remind_time"] = None if remind_time == "null" else remind_time
            if location is not None:
                t["location"] = None if location == "null" else location
            if is_exam is not None:
                t["is_exam"] = is_exam
            cal_error = reconcile_todo_calendar_event(old, t)
            write_todos(todos)
            return f"Updated to-do {id}." + (f" Google Calendar sync failed: {cal_error}" if cal_error else "")
    return f"No to-do found with id {id}."


@beta_tool
def delete_todo(id: str) -> str:
    """Delete a to-do by id."""
    todos = read_todos()
    deleted = next((t for t in todos if t["id"] == id), None)
    if deleted is None:
        return f"No to-do found with id {id}."
    if deleted.get("calendar_event_id"):
        delete_calendar_event(deleted["calendar_event_id"])
    write_todos([t for t in todos if t["id"] != id])
    return f"Deleted to-do {id}."


@beta_tool
def list_events(day: str | None = None) -> str:
    """List calendar events. READ-ONLY — these come from live feeds (Brightspace,
    Outlook, etc.) and cannot be edited here or anywhere in this app; if asked
    to change one, say to edit it in the source calendar instead.

    Args:
        day: Optional YYYY-MM-DD to filter to a single day. Omit for all events.
    """
    events = read_events().get("events", [])
    if day:
        events = [e for e in events if e.get("day") == day]
    return json.dumps(events, ensure_ascii=False)


@beta_tool
def get_config() -> str:
    """Read the full current contents of config.yaml — classes, workspaces,
    links, milestones, owner info. Always call this before update_config."""
    if not CONFIG_FILE.exists():
        return "config.yaml does not exist yet."
    return CONFIG_FILE.read_text(encoding="utf-8")


@beta_tool
def update_config(new_yaml_text: str) -> str:
    """
    Replace config.yaml with new_yaml_text, then rebuild the dashboard so the
    change takes effect on next page refresh.

    Always call get_config first, edit its returned text, and pass back the
    COMPLETE file content here — never a diff or a partial snippet.

    Safety: new_yaml_text is validated as parseable YAML before anything is
    written. A backup of the previous config is kept at config.backup.yaml,
    and if the rebuild itself fails, the backup is restored automatically.

    Args:
        new_yaml_text: The full, updated contents of config.yaml.
    """
    try:
        yaml.safe_load(new_yaml_text)
    except yaml.YAMLError as exc:
        return f"REJECTED — invalid YAML, nothing was written: {exc}"

    with _config_lock:
        if CONFIG_FILE.exists():
            shutil.copy2(CONFIG_FILE, CONFIG_BACKUP)
        CONFIG_FILE.write_text(new_yaml_text, encoding="utf-8")

        ok, log = rebuild_dashboard()
        if not ok:
            if CONFIG_BACKUP.exists():
                shutil.copy2(CONFIG_BACKUP, CONFIG_FILE)
            return ("REJECTED — config.yaml was valid YAML but rebuilding the "
                    f"dashboard failed, so I rolled back to the previous version: {log}")

    return "config.yaml updated and the dashboard was rebuilt. Refresh the page to see it."


CHAT_TOOLS = [
    list_todos, add_todo, update_todo, delete_todo, delete_series, list_events, get_config, update_config,
    {"type": "web_search_20260209", "name": "web_search"},  # Anthropic-hosted, no filesystem access
]


def build_system_prompt() -> str:
    owner_name = "the user"
    try:
        config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        owner_name = (config.get("owner") or {}).get("name") or owner_name
    except Exception:  # noqa: BLE001 - a bad config shouldn't break chat
        pass

    return (
        f"You are the assistant embedded in {owner_name}'s personal UVM planner "
        f"dashboard, as a chat bubble in the corner of the page. Today's date is "
        f"{date.today().isoformat()}.\n\n"
        "You can:\n"
        "- Answer general questions (study tips, explaining a concept, etc.), "
        "using web_search for anything current or specific enough that your "
        "own knowledge might be stale or wrong (e.g. course materials, prices, "
        "recent events) rather than guessing\n"
        "- Manage to-dos: list_todos, add_todo, update_todo, delete_todo, "
        "delete_series. For a repeating pattern ('every Monday', 'weekly "
        "until finals', 'the 1st of every month'), pass add_todo's "
        "repeat_freq ('daily'/'weekly'/'biweekly'/'monthly') and "
        "repeat_until — this creates several independent to-dos, one per "
        "occurrence, each with its own reminder, not one virtual rule; "
        "infer repeat_until from context (semester end, a named date) "
        "rather than asking, unless nothing is implied. delete_series "
        "removes every occurrence of a series at once, by its "
        "repeat_group (from list_todos) — use it when the user wants to "
        "cancel/remove a whole recurring to-do, not just one occurrence. "
        "If Google Calendar is configured in config.yaml, a dated to-do "
        "(due is set) automatically gets a Google Calendar event with a "
        "popup reminder — synced to the user's phone by Google itself, so "
        "it fires even if their computer is off. You don't need to do "
        "anything extra for it. It fires at config.yaml's default send_time "
        "UNLESS the user names a specific time ('remind me Friday at 3pm', "
        "'text me at 9am about X') — then pass that as remind_time "
        "('HH:MM', 24h) so it fires exactly then instead of the default. "
        "Undated to-dos never get a Calendar event — there's no date to put "
        "it on. If the to-do involves going somewhere, pass that as "
        "add_todo/update_todo's location — the Calendar event's Location "
        "field then gives the phone notification a native 'get directions' "
        "tap target. This can be a SPECIFIC place the user named ('Pacific "
        "Pipe') or a general TYPE of place ('a hardware store', 'a "
        "pharmacy', 'the grocery store') — for a type, pass the category "
        "verbatim and do NOT ask which one or invent a specific store "
        "name; the directions link resolves to the closest match live, "
        "wherever the phone actually is when it's tapped, which is the "
        "point of leaving it generic. Either way, only set it from what "
        "the user actually said; never invent a place or a category. Skip "
        "it for classes — tagging with `tag` already pulls that class's "
        "address from config.yaml. If the to-do clearly involves going "
        "somewhere (an activity like climbing or a game, an appointment, "
        "meeting someone, an errand) and no place OR type of place was "
        "named, ASK for it in your reply instead of adding the to-do "
        "without one — one short question, e.g. 'Where's that at?' Don't "
        "ask for to-dos with no real destination (reading, calling someone, "
        "a generic task) — a location wouldn't mean anything there. The "
        "user can always add or edit one later from the to-do's 'Add "
        "location' button on the dashboard, so it's fine to leave it unset "
        "if they don't answer. If the to-do is an exam, midterm, or final "
        "— of ANY class, not just one in particular — pass is_exam=True so "
        "it gets extra 1-week and 2-week-out reminders on top of the normal "
        "day-of one, giving real time to study.\n"
        "- Help make progress on a to-do (via its 'Get help' button, or "
        "asked directly): if it involves contacting, emailing, or messaging "
        "a specific person, use web_search first to find relevant public "
        "information about them (role, department, public contact info) "
        "and say briefly what you found. Then propose a short, plain-text "
        "email draft for the user to copy and send themselves. You have no "
        "ability to send or save an email — never say or imply that you "
        "sent, drafted-and-saved, or scheduled one. If you don't have "
        "enough to write something real (their exact name/role/email, the "
        "tone needed, what outcome the user wants), ask 1-3 concise "
        "clarifying questions instead of inventing facts about them.\n"
        "- Read (never write) calendar events: list_events — these come from "
        "live feeds and are read-only everywhere in this app, chat included; "
        "if asked to change one, say to edit it in the source calendar instead\n"
        "- Read and edit config.yaml: get_config, update_config — this is how "
        "you add or change classes, workspaces, links, and milestones. ALWAYS "
        "call get_config first, edit the full text, and pass the COMPLETE file "
        "back to update_config, never a diff. It validates YAML and rebuilds "
        "the dashboard automatically; tell the user to refresh the page after.\n\n"
        "You cannot edit this server's own code or any other Python script, "
        "and you cannot run shell commands — only the data/config tools above. "
        "Keep replies concise; this is a small chat bubble, not a document."
    )


def run_quick_add(user_text: str) -> dict:
    """
    Backs the 'Quick add' box. Deliberately stateless (no persisted history,
    unlike the chat bubble) and scoped to to-do tools only — this is meant to
    be a fast, one-shot way to add/remove/update a to-do from a short phrase,
    not a conversation.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "No ANTHROPIC_API_KEY set in .env — quick add needs one."}

    system = (
        "You manage a to-do list from a single short phrase typed into a "
        "quick-entry box. Make exactly one tool call that satisfies the "
        "request, then reply with one short, professional confirmation "
        "sentence (not a copy of the user's phrase).\n\n"
        "If it describes something to do: call add_todo with a short, "
        "professional, SUMMARIZED title you write yourself — never copy the "
        f"user's raw wording into the title verbatim. Today's date is "
        f"{date.today().isoformat()}; infer a due date if one is implied "
        "(today, tomorrow, a weekday, an explicit date), otherwise leave it "
        "unscheduled. Put extra detail (times, context) in notes. Only set "
        "remind_time (24h 'HH:MM') when the phrase explicitly asks to be "
        "reminded/notified at a particular time ('remind me at 3pm') — an "
        "event's own time ('climbing at 11') is just detail for notes, not "
        "when to send the reminder; leave remind_time unset and it'll use "
        "config.yaml's default. If a place is named, pass it as location "
        "too — it becomes the Google Calendar event's Location, a tappable "
        "'get directions' map on the phone notification. This can be a "
        "specific place ('at Pacific Pipe', 'drive to Monterey California') "
        "or a general type of place ('from a hardware store', 'at a "
        "pharmacy') — for a type, pass the category verbatim, don't guess "
        "which one; the directions link resolves to the closest match live "
        "at tap time. Never invent a location, specific or general, that "
        "wasn't said. If it's an exam, midterm, or final (any class), pass "
        "is_exam=True so it gets extra 1- and 2-week-out reminders on top "
        "of the day-of one. If it describes a repeating pattern ('every "
        "Monday', 'weekly until finals'), pass repeat_freq "
        "('daily'/'weekly'/'biweekly'/'monthly') and repeat_until — infer "
        "the end date from context rather than asking, unless nothing is "
        "implied.\n"
        "If it asks to remove, delete, or cancel a whole recurring series: "
        "call list_todos to find its repeat_group, then delete_series.\n"
        "If it asks to remove, delete, or cancel something: call list_todos "
        "to find the matching id, then delete_todo.\n"
        "If it asks to complete, finish, or check something off: call "
        "list_todos to find the id, then update_todo with done=true.\n"
        "If it asks to change something (a date, a title): call list_todos "
        "to find the id, then update_todo with the new value(s)."
    )

    try:
        runner = anthropic.Anthropic().beta.messages.tool_runner(
            model=CHAT_MODEL,
            max_tokens=1024,
            system=system,
            tools=[list_todos, add_todo, update_todo, delete_todo, delete_series],
            messages=[{"role": "user", "content": user_text}],
        )
        last = None
        for msg in runner:
            last = msg
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}

    if last is None:
        return {"error": "No response from the model."}
    reply_text = "".join(b.text for b in last.content if b.type == "text")
    return {"reply": reply_text or "Done."}


def run_chat_turn(user_message: str) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"error": "No ANTHROPIC_API_KEY set in .env — the chat assistant needs one."}

    with _chat_lock:
        global _chat_history
        client = anthropic.Anthropic()
        messages = _chat_history + [{"role": "user", "content": user_message}]
        try:
            runner = client.beta.messages.tool_runner(
                model=CHAT_MODEL,
                max_tokens=4096,
                system=build_system_prompt(),
                tools=CHAT_TOOLS,
                messages=messages,
            )
            last = None
            for msg in runner:
                messages.append({"role": "assistant", "content": msg.content})
                tool_response = runner.generate_tool_call_response()
                if tool_response is not None:
                    messages.append(tool_response)
                last = msg
        except Exception as exc:  # noqa: BLE001 - surface any API error to the UI
            return {"error": f"{type(exc).__name__}: {exc}"}

        _chat_history = messages
        if last is None:
            return {"error": "No response from the model."}
        reply_text = "".join(b.text for b in last.content if b.type == "text")
        return {"reply": reply_text or "(no text in response)"}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DASHBOARD_DIR), **kwargs)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        if self.path.startswith("/api/todos"):
            self._send_json(read_todos())
        elif self.path.startswith("/api/events"):
            self._send_json(read_events())
        elif self.path.startswith("/api/refresh-grades"):
            # Must be checked before the plain "/api/refresh" prefix below,
            # which would otherwise swallow this path too.
            self._send_json(refresh_grades())
        elif self.path.startswith("/api/refresh"):
            self._send_json(refresh_events())
        else:
            super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.startswith("/api/todos"):
            self._handle_todos_post()
        elif self.path.startswith("/api/chat"):
            self._handle_chat_post()
        elif self.path.startswith("/api/quick-add"):
            self._handle_quick_add_post()
        elif self.path.startswith("/api/links/add"):
            self._handle_link_add_post()
        elif self.path.startswith("/api/links/remove"):
            self._handle_link_remove_post()
        else:
            self._send_json({"error": "unknown endpoint"}, 404)

    def _handle_todos_post(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return

        try:
            todos = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"bad JSON: {exc}"}, 400)
            return

        if not isinstance(todos, list):
            self._send_json({"error": "expected a JSON array"}, 400)
            return

        # Whole-array save (this is the client's normal path — see the JS
        # module docstring). Each to-do's Google Calendar event is reconciled
        # here, per-id against the server's own previous copy — never the
        # client's, since the client never learns calendar_event_id back
        # (the response below doesn't echo the array), so trusting its copy
        # would recreate a duplicate event on every single save.
        old_by_id = {t["id"]: t for t in read_todos() if "id" in t}
        calendar_errors = []
        seen_ids = set()
        for t in todos:
            old = old_by_id.get(t.get("id"))
            t["calendar_event_id"] = old.get("calendar_event_id") if old else None
            seen_ids.add(t.get("id"))
            err = reconcile_todo_calendar_event(old, t)
            if err:
                calendar_errors.append(f"{t.get('title') or 'to-do'}: {err}")
        for old_id, old_t in old_by_id.items():
            if old_id not in seen_ids and old_t.get("calendar_event_id"):
                delete_calendar_event(old_t["calendar_event_id"])

        write_todos(todos)
        self._send_json({"ok": True, "count": len(todos),
                         "saved": datetime.now().isoformat(timespec="seconds"),
                         "calendar_errors": calendar_errors})

    def _handle_link_add_post(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"bad JSON: {exc}"}, 400)
            return

        workspace_id = (body.get("workspace_id") or "").strip()
        label = (body.get("label") or "").strip()
        url = (body.get("url") or "").strip()
        if not workspace_id or not label or not url:
            self._send_json({"error": "workspace_id, label, and url are all required"}, 400)
            return
        self._send_json(add_link(workspace_id, label, url))

    def _handle_link_remove_post(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"bad JSON: {exc}"}, 400)
            return

        workspace_id = (body.get("workspace_id") or "").strip()
        label = (body.get("label") or "").strip()
        if not workspace_id or not label:
            self._send_json({"error": "workspace_id and label are required"}, 400)
            return
        self._send_json(remove_link(workspace_id, label))

    def _handle_quick_add_post(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"bad JSON: {exc}"}, 400)
            return

        text = (body.get("text") or "").strip()
        if not text:
            self._send_json({"error": "empty text"}, 400)
            return

        self._send_json(run_quick_add(text))

    def _handle_chat_post(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            self._send_json({"error": "empty body"}, 400)
            return

        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"error": f"bad JSON: {exc}"}, 400)
            return

        message = (body.get("message") or "").strip()
        if not message:
            self._send_json({"error": "empty message"}, 400)
            return

        self._send_json(run_chat_turn(message))

    def log_message(self, fmt, *args) -> None:
        # The default logs every asset request. Only surface writes and errors.
        #
        # sys.stderr is None when this runs headless under pythonw.exe with
        # no console attached (see run_hidden.vbs / BUILD.md, "Running
        # always-on") — writing to it would raise AttributeError from
        # inside send_response(), which aborts the response mid-flight. The
        # underlying work (e.g. a to-do already written to disk) had already
        # completed by then, so the visible symptom is a request that
        # silently succeeded server-side but reads as "failed to fetch" /
        # "connection closed unexpectedly" to the browser — logging must
        # never be able to do that, so this is entirely best-effort.
        if sys.stderr is None:
            return
        if "POST" in (fmt % args) or "error" in (fmt % args).lower():
            try:
                sys.stderr.write(f"{self.log_date_time_string()}  {fmt % args}\n")
            except Exception:  # noqa: BLE001 - logging must never break the request it logs
                pass


def get_tailscale_ip() -> str | None:
    """Best-effort — returns None (not an error) if Tailscale isn't installed
    or isn't signed in, so a missing Tailscale never breaks local use."""
    tailscale_exe = Path(r"C:\Program Files\Tailscale\tailscale.exe")
    if not tailscale_exe.exists():
        return None
    try:
        result = subprocess.run(
            [str(tailscale_exe), "ip", "-4"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        return ip if result.returncode == 0 and ip else None
    except Exception:  # noqa: BLE001
        return None


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    DASHBOARD_DIR.mkdir(exist_ok=True)

    if not (DASHBOARD_DIR / "index.html").exists():
        print("dashboard/index.html does not exist yet.")
        print("Run: python scripts/build_dashboard.py")
        return

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Planner running on this PC at http://127.0.0.1:{PORT}")
    tailscale_ip = get_tailscale_ip()
    if tailscale_ip:
        print(f"From your phone (via Tailscale): http://{tailscale_ip}:{PORT}")
    print(f"To-dos: {TODO_FILE}")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Note: ANTHROPIC_API_KEY not set — the chat bubble will report an error until it is.")
    if _reminders_config().get("enabled") and not (
        GOOGLE_CALENDAR_CLIENT_ID and GOOGLE_CALENDAR_CLIENT_SECRET and GOOGLE_CALENDAR_REFRESH_TOKEN
    ):
        print("Note: reminders.enabled is true in config.yaml but the Google Calendar OAuth "
              "values aren't set in .env — new to-dos will still save, but no Calendar event "
              "will be created until those are set. See BUILD.md, 'Google Calendar reminders'. "
              "The failure shows up in the save indicator, not silent.")
    elif _calendar_enabled():
        t = _default_send_time()
        print(f"Google Calendar reminders: dated to-dos get a popup notification at "
              f"{_format_12h(t)} on their due date by default — synced by Google, "
              f"so it fires even while this process (or this PC) is off.")
    print("Leave this window open. Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
