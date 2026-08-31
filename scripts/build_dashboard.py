"""
build_dashboard.py — reads config.yaml + data/events.json, writes dashboard/index.html

Run it:
    python scripts/build_dashboard.py

Self-contained output: inline CSS and JS, no CDN, no npm, no build step. Works
with no network, per BUILD.md.

STATUS: all of BUILD.md's original "Build these" (step 5 included) is now
built — week strip, month view, deadlines, day detail, quick add, countdown
panel, quick links, feed status panel, and a working refresh button — plus
everything documented further down BUILD.md past that original spec
(grades, directions, exam reminders, class contact info, the workload
heatmap, recurring to-dos, and more). Consult BUILD.md's table of sections,
not this comment, for what's current.

encoding="utf-8" on every read and write. Windows defaults to cp1252 and a
single em-dash or checkmark raises UnicodeEncodeError. Said twice in BUILD.md
on purpose, said here too.
"""

from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"
EVENTS_FILE = ROOT / "data" / "events.json"
GRADES_FILE = ROOT / "data" / "grades.json"
OUT_FILE = ROOT / "dashboard" / "index.html"
MANIFEST_FILE = ROOT / "dashboard" / "manifest.webmanifest"

DEADLINE_WINDOW_DAYS = 14
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def load_events() -> dict:
    if not EVENTS_FILE.exists():
        return {"generated": None, "feeds": {}, "events": []}
    return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))


def load_grades() -> dict:
    if not GRADES_FILE.exists():
        return {"generated": None, "classes": {}}
    return json.loads(GRADES_FILE.read_text(encoding="utf-8"))


def workspace_for_source(source: str, config: dict) -> dict | None:
    """Map an event's feed source ('Brightspace', 'Outlook', ...) to its
    configured workspace dict, via feed_workspaces -> workspaces."""
    feed_workspaces = config.get("feed_workspaces", {}) or {}
    workspaces = {w["id"]: w for w in (config.get("workspaces") or [])}
    ws_id = feed_workspaces.get(source)
    if ws_id and ws_id in workspaces:
        return workspaces[ws_id]
    for w in workspaces.values():
        if w.get("default"):
            return w
    return None


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def build_week_strip(today: date, events: list[dict]) -> list[dict]:
    """One dict per day of the CURRENT week (Mon-first). This is the server
    -rendered first paint; the matching JS renderWeek() takes over for
    navigation and for merging in live to-do counts.
    """
    start = monday_of(today)
    by_day: dict[str, int] = {}
    for e in events:
        by_day[e["day"]] = by_day.get(e["day"], 0) + 1

    days = []
    for i in range(7):
        d = start + timedelta(days=i)
        iso = d.isoformat()
        days.append({
            "date": iso,
            "weekday": WEEKDAY_NAMES[i],
            "is_today": d == today,
            "event_count": by_day.get(iso, 0),
        })
    return days


def build_deadlines(today: date, events: list[dict], config: dict) -> list[dict]:
    """
    Next DEADLINE_WINDOW_DAYS days of course-tagged deadline events.

    Definition, since Brightspace hasn't published Fall 2026 data yet and
    there's no real due-date sample to check this against: any event
    carrying a class tag, sourced from a feed whose workspace lists 'due'
    among its panels (config.yaml -> workspaces[].panels), due within the
    window. Revisit once real Brightspace assignment data exists — see
    BUILD.md's "Class tagging — verify before you build on it".
    """
    window_end = today + timedelta(days=DEADLINE_WINDOW_DAYS)
    out = []
    for e in events:
        if not e.get("tag"):
            continue
        ws = workspace_for_source(e.get("source", ""), config)
        if not ws or "due" not in (ws.get("panels") or []):
            continue
        try:
            day = date.fromisoformat(e["day"])
        except (KeyError, ValueError):
            continue
        if today <= day <= window_end:
            out.append(e)
    out.sort(key=lambda e: (e["day"], e.get("start") or ""))
    return out


def group_by_tag(deadlines: list[dict]) -> list[tuple[str, list[dict]]]:
    """Preserves the sort order of `deadlines`, so the group whose earliest
    item is most urgent appears first — 'grouped by tag' without losing
    'sorted by urgency'."""
    order: list[str] = []
    groups: dict[str, list[dict]] = {}
    for e in deadlines:
        tag = e["tag"]
        if tag not in groups:
            groups[tag] = []
            order.append(tag)
        groups[tag].append(e)
    return [(tag, groups[tag]) for tag in order]


def accent_for(source: str, config: dict) -> str:
    ws = workspace_for_source(source, config)
    return (ws or {}).get("accent", "#666666")


def days_until(day_iso: str, today: date) -> int:
    return (date.fromisoformat(day_iso) - today).days


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_timestamp_display(value: str | None) -> str:
    """Friendly display for an ISO datetime string ('Aug 23, 2:26 PM'
    instead of '2026-08-23T14:26:03-04:00') -- added 2026-08-23 alongside
    the 12-hour-clock change (Charles: "all times in general to standard
    clock, not military time"). Falls back to the raw value untouched if it
    doesn't parse (e.g. the literal "never") -- a footer timestamp
    shouldn't be able to crash the whole page. Builds the string by hand
    rather than strftime's %-d/%#d for a non-padded day -- that flag isn't
    portable between Windows and POSIX, one of BUILD.md's own gotchas."""
    if not value or value == "never":
        return value or "never"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    hour12 = dt.hour % 12 or 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{MONTH_ABBR[dt.month - 1]} {dt.day}, {hour12}:{dt.minute:02d} {ampm}"


def heat_level(count: int) -> int:
    """Workload heatmap tier from a day's total item count (events + open
    to-dos): 0 none, 1 low, 2 medium, 3 high. Kept identical to heatLevel()
    in the generated JS -- see that function for why two copies exist (this
    one renders the server-side first paint from event counts alone; JS
    recomputes once to-dos load)."""
    if count <= 0:
        return 0
    if count <= 2:
        return 1
    if count <= 4:
        return 2
    return 3


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def render_day_cell(day: dict) -> str:
    today_class = " today" if day["is_today"] else ""
    weekday_num = date.fromisoformat(day["date"]).day
    # Heat from event count only -- todos aren't known at build time (they
    # live in data/todos.json, fetched client-side). The JS re-render that
    # follows immediately after page load overwrites this with the fuller
    # events+todos count; see heatLevel() in the generated JS.
    heat = heat_level(day["event_count"])
    return (
        f'    <button type="button" class="day-cell{today_class}" '
        f'data-date="{day["date"]}" data-heat="{heat}">\n'
        f'      <span class="weekday">{day["weekday"]}</span>\n'
        f'      <span class="date-num">{weekday_num}</span>\n'
        f'      <span class="counts">{day["event_count"]} event'
        f'{"s" if day["event_count"] != 1 else ""}</span>\n'
        f'    </button>'
    )


def render_deadlines(grouped: list[tuple[str, list[dict]]], today: date,
                      config: dict, tag_names: dict[str, str],
                      tag_brightspace: dict[str, str]) -> str:
    if not grouped:
        return (
            '<p class="empty">No assignment due dates in the next '
            f'{DEADLINE_WINDOW_DAYS} days.</p>'
        )

    parts = ['<div class="deadline-groups">']
    for tag, items in grouped:
        accent = accent_for(items[0]["source"], config)
        display_name = tag_names.get(tag, tag)
        # Brightspace's calendar feed has no per-assignment URL (checked
        # against the real feed — no VEVENT carries a URL field), so this
        # links to the course's Brightspace page, not the specific
        # assignment. Only rendered when brightspace_url is actually set —
        # never guessed. One link per group (the class), not per item.
        bs_url = tag_brightspace.get(tag)
        bs_link = (
            f' <a class="directions-link" href="{esc(bs_url)}" target="_blank" '
            f'rel="noopener noreferrer">Open in Brightspace</a>'
            if bs_url else ""
        )
        parts.append(f'  <div class="deadline-group" style="--tone-raw:{esc(accent)}">')
        parts.append(f'    <h3 class="chip">{esc(display_name)}</h3>{bs_link}')
        parts.append('    <ul class="deadline-list">')
        for e in items:
            n = days_until(e["day"], today)
            if n == 0:
                urgency = "today"
            elif n == 1:
                urgency = "tomorrow"
            else:
                urgency = f"in {n} days"
            parts.append(
                '      <li>'
                f'<span class="due-title">{esc(e["title"])}</span> '
                f'<span class="due-when">{esc(urgency)} &middot; {esc(e["day"])}</span>'
                '</li>'
            )
        parts.append('    </ul>')
        parts.append('  </div>')
    parts.append('</div>')
    return "\n".join(parts)


def render_quick_links(config: dict) -> str:
    workspaces = config.get("workspaces") or []
    if not workspaces:
        return '<p class="empty">No workspaces configured.</p>'

    parts = ['<div class="link-groups">']
    for ws in workspaces:
        ws_id = ws.get("id", "")
        ws_name = ws.get("name", ws_id)
        accent = ws.get("accent", "#666666")
        # Links with no url set yet are placeholders in config.yaml — don't
        # render a dead link for them (the core design rule: never show
        # something the page can't actually back up).
        links = [link for link in (ws.get("links") or []) if (link.get("url") or "").strip()]

        parts.append(f'  <div class="link-group" style="--tone-raw:{esc(accent)}">')
        parts.append(f'    <h3 class="chip">{esc(ws_name)}</h3>')
        if links:
            parts.append('    <div class="link-chips">')
            for link in links:
                label = link.get("label", "")
                url = link.get("url", "")
                parts.append('      <div class="link-chip">')
                parts.append(f'        <a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(label)}</a>')
                parts.append(f'        <button type="button" class="link-remove" '
                              f'data-workspace="{esc(ws_id)}" data-label="{esc(label)}" '
                              f'aria-label="Remove {esc(label)}" title="Remove">&times;</button>')
                parts.append('      </div>')
            parts.append('    </div>')
        else:
            parts.append('    <p class="empty">No links yet.</p>')
        parts.append(f'    <form class="add-link-form" data-workspace="{esc(ws_id)}">')
        parts.append('      <input type="text" name="label" placeholder="Label" required>')
        parts.append('      <input type="url" name="url" placeholder="https://..." required>')
        parts.append('      <button type="submit">Add</button>')
        parts.append('    </form>')
        parts.append('  </div>')
    parts.append('</div>')
    return "\n".join(parts)


# Classes have no workspace field of their own in config.yaml -- they're
# implicitly "school" via feed_workspaces (Brightspace -> school). Reuse that
# indirection rather than hardcoding the id "school".
def school_accent(config: dict) -> str:
    workspaces = {w["id"]: w for w in (config.get("workspaces") or [])}
    ws_id = (config.get("feed_workspaces") or {}).get("Brightspace")
    return workspaces.get(ws_id, {}).get("accent", "#666666")


# Status strings fetch_grades.py can write per class -- kept in sync with
# result_template() there. "connected" isn't listed: its row shows the
# actual grade_text instead of one of these labels.
GRADE_STATUS_LABELS = {
    "not_configured": "not connected",
    "login_required": "not connected — Brightspace session expired, run "
                       "fetch_grades.py --login",
    "parser_not_ready": "connected — parser not finished yet (see BUILD.md)",
}


def render_countdowns(config: dict, today: date) -> str:
    """Days-remaining list from config.yaml's `milestones` -- spec'd in the
    original BUILD.md ('Countdown panel') but never actually rendered until
    2026-08-23, even though the config data was already real. Shows every
    configured milestone, past ones included (dimmed via .countdown-passed)
    rather than silently dropped -- it's real config data, not invented."""
    milestones = config.get("milestones") or []
    items = []
    for m in milestones:
        label = m.get("label") or ""
        try:
            d = date.fromisoformat(str(m.get("date") or ""))
        except ValueError:
            continue
        items.append((d, label))
    if not items:
        return '<p class="empty">No milestones configured.</p>'
    items.sort(key=lambda pair: pair[0])

    parts = ['<ul class="countdown-list">']
    for d, label in items:
        n = (d - today).days
        passed_class = " countdown-passed" if n < 0 else ""
        if n > 0:
            when = f"in {n} day{'s' if n != 1 else ''}"
        elif n == 0:
            when = "today"
        else:
            when = f"{abs(n)} day{'s' if abs(n) != 1 else ''} ago"
        parts.append(
            f'  <li class="countdown-row{passed_class}">'
            f'<span class="countdown-label">{esc(label)}</span>'
            f'<span class="countdown-when">{esc(when)} &middot; {esc(d.isoformat())}</span>'
            '</li>'
        )
    parts.append('</ul>')
    return "\n".join(parts)


def render_feed_status(events_payload: dict, config: dict) -> str:
    """Per-feed connected/error status -- spec'd in the original BUILD.md
    ('Status panel') but never actually rendered until 2026-08-23, even
    though fetch_brightspace.py already writes real status/count/error per
    feed into events.json every run. Before this, a dead feed just meant
    deadlines quietly stopped updating with nothing on the page saying why."""
    top_level_error = events_payload.get("error")
    if top_level_error:
        return f'<p class="feed-row-error">{esc(top_level_error)}</p>'

    feeds = events_payload.get("feeds") or {}
    if not feeds:
        return '<p class="empty">No feeds configured yet — see .env.example.</p>'

    parts = ['<ul class="feed-list">']
    for name, info in feeds.items():
        accent = accent_for(name, config)
        status = info.get("status", "unknown")
        if status == "connected":
            count = info.get("count", 0)
            value_html = f'<span class="feed-value">{count} event{"s" if count != 1 else ""}</span>'
        elif status == "error":
            value_html = f'<span class="feed-status-text err">error: {esc(info.get("error") or "unknown")}</span>'
        else:
            value_html = f'<span class="feed-status-text">{esc(status)}</span>'
        parts.append(
            f'  <li class="feed-row" style="--tone-raw:{esc(accent)}">'
            f'<span class="chip">{esc(name)}</span>{value_html}</li>'
        )
    parts.append('</ul>')
    return "\n".join(parts)


def render_grades(config: dict, grades_payload: dict, tag_names: dict[str, str]) -> str:
    classes = config.get("classes") or []
    if not classes:
        return '<p class="empty">No classes configured.</p>'

    per_class = grades_payload.get("classes", {}) or {}
    accent = school_accent(config)

    # One row per REAL course, not per configured tag. A class with
    # same_class_as set (e.g. CHEM-1400-EVE) isn't a separate Brightspace
    # enrollment -- just a second weekly meeting time for a tag already
    # shown -- so it gets no row of its own; see config.yaml's schema
    # comment and BUILD.md, "Same-class aliasing." CHEM-1400-L51 (the lab)
    # has no same_class_as -- confirmed 2026-08-23 to be a genuinely
    # separate Brightspace course -- so it still gets its own row.
    parts = ['<ul class="grade-list">']
    for course in classes:
        tag = course.get("tag")
        if not tag or course.get("same_class_as"):
            continue

        display_name = tag_names.get(tag, tag)
        result = per_class.get(tag) or {"status": "not_configured"}
        status = result.get("status", "not_configured")

        if status == "connected" and result.get("grade_text"):
            value_html = f'<span class="grade-value">{esc(result["grade_text"])}</span>'
        elif status == "error":
            value_html = f'<span class="grade-status err">error: {esc(result.get("error") or "unknown")}</span>'
        else:
            label = GRADE_STATUS_LABELS.get(status, status)
            value_html = f'<span class="grade-status">{esc(label)}</span>'

        parts.append(
            f'  <li class="grade-row" style="--tone-raw:{esc(accent)}">'
            f'<span class="chip">{esc(display_name)}</span>'
            f'{value_html}</li>'
        )
    parts.append('</ul>')
    return "\n".join(parts)


CSS = """
:root {
  --bg: #f7f7f5;
  --surface: #ffffff;
  --text: #1a1a1a;
  --text-dim: #666666;
  --border: #dddddd;
  --accent: #007155;
  --ok: #1a7a3c;
  --err: #a3312a;
  --heat: #c9752c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #16171a;
    --surface: #201f23;
    --text: #ececec;
    --text-dim: #9a9a9a;
    --border: #3a3a3e;
    --ok: #4fbf75;
    --err: #e07068;
    --heat: #e0a35c;
  }
}
/* !important is deliberate here: several elements below (.chat-panel,
   .week-cells, .add-todo-form) declare their own `display` LATER in this
   stylesheet with equal selector specificity to [hidden], so without
   !important the later rule silently wins and `hidden` stops working —
   exactly the cascade trap BUILD.md's gotcha list calls out. */
[hidden] { display: none !important; }
button:disabled { opacity: 0.6; cursor: default; }

* { box-sizing: border-box; }
body {
  margin: 0;
  padding: 1.5rem;
  background: var(--bg);
  color: var(--text);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.4;
}
.page-header h1 { margin: 0 0 0.15rem 0; font-size: 1.5rem; }
.page-header .subtitle { margin: 0; color: var(--text-dim); }

/* Shown only when a real /api/todos fetch just failed -- see the "offline
   cache + pending-toggle queue" comment near loadTodos() in the generated
   JS. --heat reused here rather than a new token: same "needs attention,
   not an error" register as the workload heatmap, distinct from --err. */
.offline-banner {
  background: color-mix(in srgb, var(--heat) 18%, var(--surface));
  border: 1px solid var(--heat);
  border-radius: 8px;
  padding: 0.6rem 0.9rem;
  margin-top: 1rem;
  font-size: 0.9rem;
}
.offline-banner strong { color: var(--heat); }

/* Fixed, unlike .offline-banner -- this fires once right after page load,
   before the user has necessarily scrolled to wherever the element sits in
   normal flow, so it has to be visible without scrolling to do its job. */
.pwa-status-banner {
  position: fixed;
  top: 1rem;
  left: 50%;
  transform: translateX(-50%);
  z-index: 1000;
  max-width: min(90vw, 26rem);
  text-align: center;
}

.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 1rem;
  margin-top: 1.25rem;
}
.panel h2 { margin: 0 0 0.75rem 0; font-size: 1.05rem; }
.panel h3 { margin: 0 0 0.5rem 0; font-size: 0.95rem; }
.window-note { color: var(--text-dim); font-weight: normal; font-size: 0.85em; }

.view-controls {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}
.nav-controls, .view-toggle { display: flex; gap: 0.4rem; }
.view-controls button {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.7rem;
  cursor: pointer;
  font-size: 0.9rem;
}
.view-controls button:hover { border-color: var(--accent); }
.view-btn.active { border-color: var(--accent); color: var(--accent); font-weight: 600; }

.week-cells {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.5rem;
}
.day-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.15rem;
  /* Grid/flex items default to min-width: auto, which floors their width
     at their content's min-content size -- for a text-heavy cell like this
     one, that can exceed the 1fr share .week-cells actually gave it on a
     narrow phone, pushing the whole row wider than the screen. min-width: 0
     lets it actually honor that share; .counts below (the long "N events,
     N to-dos" text) is what needed the room, so it gets an ellipsis instead
     of forcing the cell wide. */
  min-width: 0;
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 0.6rem 0.3rem;
  cursor: pointer;
  color: var(--text);
  font: inherit;
}
.day-cell:hover { border-color: var(--accent); }
.day-cell.today { border-color: var(--accent); border-width: 2px; }
.day-cell.selected { background: color-mix(in srgb, var(--accent) 15%, var(--bg)); }
.day-cell .weekday { font-size: 0.75rem; color: var(--text-dim); }
.day-cell .date-num { font-size: 1.1rem; font-weight: 600; }
.day-cell .counts {
  font-size: 0.7rem;
  color: var(--text-dim);
  max-width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* Workload heatmap: a bottom accent stripe scaled by how busy the day is
   (events + open to-dos), not a full background tint -- painted with
   box-shadow so it layers cleanly over .today/.selected instead of fighting
   them for the background property. data-heat is set server-side from event
   counts alone (todos aren't known at build time) and recomputed client-side
   once to-dos load -- see heatLevel() in the generated JS. */
.day-cell[data-heat="1"], .month-cell[data-heat="1"] {
  box-shadow: inset 0 -3px 0 0 color-mix(in srgb, var(--heat) 45%, transparent);
}
.day-cell[data-heat="2"], .month-cell[data-heat="2"] {
  box-shadow: inset 0 -3px 0 0 color-mix(in srgb, var(--heat) 70%, transparent);
}
.day-cell[data-heat="3"], .month-cell[data-heat="3"] {
  box-shadow: inset 0 -3px 0 0 var(--heat);
}

.month-label { margin: 0 0 0.5rem 0; font-weight: 600; }
.month-weekday-header {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.35rem;
  margin-bottom: 0.35rem;
}
.month-weekday-header span {
  text-align: center;
  font-size: 0.75rem;
  color: var(--text-dim);
}
.month-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.35rem;
}
.month-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.1rem;
  min-height: 3.2rem;
  min-width: 0; /* see .day-cell's comment above -- same grid/flex overflow fix */
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  color: var(--text);
  font: inherit;
}
.month-cell.outside { opacity: 0.35; }
.month-cell:hover { border-color: var(--accent); }
.month-cell.today { border-color: var(--accent); border-width: 2px; }
.month-cell.selected { background: color-mix(in srgb, var(--accent) 15%, var(--bg)); }
.month-cell .date-num { font-size: 0.95rem; font-weight: 600; }
.month-cell .counts {
  font-size: 0.65rem;
  color: var(--text-dim);
  max-width: 100%;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.empty { color: var(--text-dim); font-style: italic; margin: 0; }

.deadline-groups { display: flex; flex-direction: column; gap: 0.9rem; }
.deadline-group { --tone: var(--tone-raw); }
.chip {
  display: inline-block;
  margin: 0 0.4rem 0.4rem 0;
  padding: 0.15rem 0.6rem;
  border-radius: 999px;
  font-size: 0.85rem;
  font-weight: 600;
  background: color-mix(in srgb, var(--tone) 18%, transparent);
  color: var(--tone);
  border: 1px solid color-mix(in srgb, var(--tone) 45%, transparent);
}
@media (prefers-color-scheme: dark) {
  .chip { color: color-mix(in srgb, var(--tone) 60%, white); }
}
.deadline-list { list-style: none; margin: 0; padding: 0; }
.deadline-list li {
  padding: 0.35rem 0;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.deadline-list li:first-child { border-top: none; }
.due-when { color: var(--text-dim); font-size: 0.85em; white-space: nowrap; }

.grade-list, .feed-list { list-style: none; margin: 0; padding: 0; }
.grade-row, .feed-row {
  --tone: var(--tone-raw);
  padding: 0.4rem 0;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.grade-row:first-child, .feed-row:first-child { border-top: none; }
.grade-value, .feed-value { font-weight: 600; }
.grade-status, .feed-status-text { color: var(--text-dim); font-size: 0.9em; }
.grade-status.err, .feed-status-text.err { color: var(--err); }
.feed-row-error { color: var(--err); margin: 0; }
/* Shared by the grades panel's "Refresh grades" row and the status panel's
   "Refresh events" row -- same layout, same generated-timestamp style. */
.refresh-row { margin-top: 0.75rem; display: flex; align-items: center; gap: 0.6rem; }
.refresh-row button {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.35rem 0.7rem;
  cursor: pointer;
  font-size: 0.9rem;
}
.refresh-row button:hover { border-color: var(--accent); }
.refresh-row button:disabled { cursor: default; opacity: 0.6; }
.panel-timestamp { color: var(--text-dim); font-size: 0.85em; }

.countdown-list { list-style: none; margin: 0; padding: 0; }
.countdown-row {
  padding: 0.35rem 0;
  border-top: 1px solid var(--border);
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.countdown-row:first-child { border-top: none; }
.countdown-row.countdown-passed { opacity: 0.5; }
.countdown-label { font-weight: 500; }
.countdown-when { color: var(--text-dim); font-size: 0.85em; white-space: nowrap; }

.schedule-list, .todo-list { list-style: none; margin: 0 0 1rem 0; padding: 0; }
.schedule-item {
  padding: 0.4rem 0;
  border-top: 1px solid var(--border);
  cursor: pointer;
  display: flex;
  gap: 0.75rem;
  align-items: baseline;
  flex-wrap: wrap;
}
.schedule-item:first-child { border-top: none; }
.sched-time { color: var(--text-dim); font-size: 0.85em; min-width: 4.5rem; }
.sched-main { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.readonly-note {
  flex-basis: 100%;
  font-size: 0.8em;
  color: var(--text-dim);
  font-style: italic;
}
.class-info {
  flex-basis: 100%;
  font-size: 0.85em;
  color: var(--text);
  margin-top: 0.3rem;
  padding-top: 0.3rem;
  border-top: 1px dashed var(--border);
}
.class-info div { margin-top: 0.15rem; }
.class-info div:first-child { margin-top: 0; }
.class-info .instructor-name { font-weight: 600; }
.class-info a { color: var(--accent); }

.todo-item {
  padding: 0.4rem 0;
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.6rem;
}
.todo-item:first-child { border-top: none; }
.todo-item.done .todo-title { text-decoration: line-through; color: var(--text-dim); }
.todo-title { font-weight: 500; }
.todo-notes { color: var(--text-dim); font-size: 0.85em; flex: 1; }
.todo-remind-time {
  color: var(--text-dim);
  font-size: 0.8em;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.15rem 0.5rem;
  white-space: nowrap;
}
.todo-exam-tag {
  color: var(--accent);
  font-size: 0.8em;
  border: 1px solid var(--accent);
  border-radius: 6px;
  padding: 0.15rem 0.5rem;
  white-space: nowrap;
  font-weight: 600;
}
.todo-exam-label {
  font-size: 0.85em;
  color: var(--text-dim);
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  white-space: nowrap;
}
.todo-repeat-tag {
  color: var(--text-dim);
  font-size: 0.8em;
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.15rem 0.5rem;
  white-space: nowrap;
}
.todo-delete, .todo-help {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.8em;
  padding: 0.15rem 0.5rem;
  color: var(--text);
  font-family: inherit;
}
.todo-delete { color: var(--err); }
.todo-delete:hover { border-color: var(--err); }
.todo-help:hover { border-color: var(--accent); color: var(--accent); }
.todo-help:disabled { opacity: 0.6; cursor: default; }
.directions-link {
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 0.8em;
  padding: 0.15rem 0.5rem;
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
}
.directions-link:hover { border-color: var(--accent); color: var(--accent); }
.todo-location-control { display: inline-flex; align-items: center; gap: 0.3rem; }
.todo-location-add, .todo-location-edit, .todo-location-save {
  background: none;
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.8em;
  padding: 0.15rem 0.5rem;
  color: var(--text);
  font-family: inherit;
}
.todo-location-edit { padding: 0.15rem 0.4rem; color: var(--text-dim); }
.todo-location-add:hover, .todo-location-edit:hover, .todo-location-save:hover {
  border-color: var(--accent); color: var(--accent);
}
.todo-location-input {
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.15rem 0.5rem;
  font: inherit;
  font-size: 0.85em;
  width: 12rem;
  max-width: 40vw;
}

.add-todo-form, .quick-add-form {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-top: 0.5rem;
}
.add-todo-form input:not([type=checkbox]), .add-todo-form select, .quick-add-form input {
  flex: 1;
  min-width: 8rem;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.6rem;
  font: inherit;
}
.add-todo-form button, .quick-add-form button {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.4rem 0.9rem;
  cursor: pointer;
  font: inherit;
}
.quick-add-feedback { margin: 0.5rem 0 0 0; font-size: 0.9em; color: var(--ok); }
.quick-add-feedback.error { color: var(--err); }
.quick-add-hint { margin: 0.5rem 0 0 0; font-size: 0.78em; color: var(--text-dim); }

.save-indicator {
  position: fixed;
  bottom: 1rem;
  right: 1rem;
  padding: 0.5rem 0.9rem;
  border-radius: 8px;
  font-size: 0.85rem;
  color: white;
  max-width: 20rem;
  box-shadow: 0 2px 8px rgba(0,0,0,0.25);
}
.save-indicator.saving { background: var(--text-dim); }
.save-indicator.saved { background: var(--ok); }
.save-indicator.failed { background: var(--err); }

.meta-footer { margin-top: 1.5rem; color: var(--text-dim); font-size: 0.8rem; }

.chat-bubble {
  position: fixed;
  bottom: 1.25rem;
  right: 1.25rem;
  width: 3.2rem;
  height: 3.2rem;
  border-radius: 50%;
  border: none;
  background: var(--accent);
  color: white;
  font-size: 1.4rem;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(0,0,0,0.3);
  z-index: 50;
}
.chat-bubble:hover { filter: brightness(1.1); }

.chat-panel {
  position: fixed;
  bottom: 1.25rem;
  right: 1.25rem;
  width: min(22rem, calc(100vw - 2.5rem));
  height: min(28rem, calc(100vh - 2.5rem));
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  box-shadow: 0 4px 24px rgba(0,0,0,0.35);
  z-index: 50;
  overflow: hidden;
}
.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.6rem 0.9rem;
  background: var(--accent);
  color: white;
  font-weight: 600;
  font-size: 0.9rem;
}
.chat-header button {
  background: none;
  border: none;
  color: white;
  font-size: 1.1rem;
  cursor: pointer;
  line-height: 1;
  padding: 0.1rem 0.3rem;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.chat-msg {
  max-width: 85%;
  padding: 0.45rem 0.7rem;
  border-radius: 10px;
  font-size: 0.85rem;
  line-height: 1.35;
  white-space: pre-wrap;
  word-break: break-word;
}
.chat-msg.user {
  align-self: flex-end;
  background: var(--accent);
  color: white;
}
.chat-msg.assistant {
  align-self: flex-start;
  background: var(--bg);
  border: 1px solid var(--border);
}
.chat-msg.error {
  align-self: flex-start;
  background: color-mix(in srgb, var(--err) 15%, var(--bg));
  border: 1px solid var(--err);
  color: var(--err);
}
.chat-form {
  display: flex;
  gap: 0.4rem;
  padding: 0.6rem;
  border-top: 1px solid var(--border);
}
.chat-form input {
  flex: 1;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.4rem 0.6rem;
  font: inherit;
  font-size: 0.85rem;
}
.chat-form button {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.4rem 0.8rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.85rem;
}

.chat-msg p { margin: 0 0 0.4rem 0; }
.chat-msg p:last-child { margin-bottom: 0; }
.chat-msg ul { margin: 0.3rem 0; padding-left: 1.1rem; }
.chat-msg li { margin: 0.15rem 0; }
.chat-msg code {
  background: color-mix(in srgb, var(--text) 12%, transparent);
  padding: 0.05rem 0.3rem;
  border-radius: 4px;
  font-size: 0.9em;
}
.chat-msg pre {
  background: color-mix(in srgb, var(--text) 8%, transparent);
  padding: 0.4rem 0.5rem;
  border-radius: 6px;
  overflow-x: auto;
  margin: 0.3rem 0;
}
.chat-msg pre code { background: none; padding: 0; }
.chat-msg a { color: inherit; text-decoration: underline; }

.link-groups { display: flex; flex-direction: column; gap: 0.9rem; }
.link-group { --tone: var(--tone-raw); }
.link-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-bottom: 0.5rem; }
.link-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.15rem;
  background: color-mix(in srgb, var(--tone) 12%, var(--bg));
  border: 1px solid color-mix(in srgb, var(--tone) 40%, transparent);
  border-radius: 999px;
  padding: 0.25rem 0.3rem 0.25rem 0.75rem;
}
.link-chip a { color: var(--text); text-decoration: none; font-size: 0.85rem; }
.link-chip a:hover { text-decoration: underline; }
.link-remove {
  background: none;
  border: none;
  color: var(--text-dim);
  cursor: pointer;
  font-size: 1rem;
  line-height: 1;
  padding: 0.1rem 0.45rem;
  border-radius: 50%;
}
.link-remove:hover { color: var(--err); background: color-mix(in srgb, var(--err) 15%, transparent); }
.add-link-form { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.add-link-form input {
  flex: 1;
  min-width: 6rem;
  background: var(--bg);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 0.3rem 0.5rem;
  font: inherit;
  font-size: 0.82rem;
}
.add-link-form button {
  background: var(--accent);
  color: white;
  border: none;
  border-radius: 6px;
  padding: 0.3rem 0.7rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.82rem;
}
"""

JS = """
(function () {
  const dataEl = document.getElementById("planner-data");
  const data = JSON.parse(dataEl.textContent);
  const events = data.events;
  const weekdayNames = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  let todos = [];
  let selectedDate = null;
  let viewMode = "week";
  let weekAnchor = mondayOf(new Date());
  let monthAnchor = firstOfMonth(new Date());

  // Offline support (see the "offline cache + pending-toggle queue" block
  // below, near loadTodos/saveTodos, for the full explanation). isOffline
  // reflects whether the LAST real /api/todos attempt actually reached
  // serve.py -- not navigator.onLine, which only knows about the network
  // interface, not whether the PC/Tailscale is reachable.
  let isOffline = false;
  const TODOS_CACHE_KEY = "offlineTodosCache_v1";
  const PENDING_TOGGLES_KEY = "offlinePendingToggles_v1";

  // ---- date helpers -------------------------------------------------

  function isoDate(d) {
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return y + "-" + m + "-" + day;
  }
  function parseISODate(iso) {
    const [y, m, d] = iso.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  function mondayOf(d) {
    const copy = new Date(d);
    const dow = (copy.getDay() + 6) % 7; // Mon=0..Sun=6
    copy.setDate(copy.getDate() - dow);
    copy.setHours(0, 0, 0, 0);
    return copy;
  }
  function firstOfMonth(d) {
    const copy = new Date(d.getFullYear(), d.getMonth(), 1);
    copy.setHours(0, 0, 0, 0);
    return copy;
  }
  function makeId() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID().slice(0, 8);
    return Math.random().toString(16).slice(2, 10);
  }

  // Workload heatmap tier from a day's total item count (events + open
  // to-dos) -- kept identical to heat_level() in build_dashboard.py, which
  // renders the server-side first paint from event counts alone (todos
  // aren't known at build time). This recomputes once to-dos load.
  function heatLevel(count) {
    if (count <= 0) return 0;
    if (count <= 2) return 1;
    if (count <= 4) return 2;
    return 3;
  }

  // d's day-of-month, k months later, clamped to that month's last day (e.g.
  // Jan 31 + 1 month -> Feb 28) -- mirrors _add_months() in serve.py. Always
  // anchored to d's ORIGINAL day, not chained off a previous occurrence, so
  // a 31st-of-the-month to-do still lands on the 31st in Jan/Mar/May/...
  // even though Feb clamped it to 28 -- see expandRecurrence below, which
  // always calls this with the FIRST occurrence's date, never a running one.
  function addMonthsJS(d, k) {
    const day = d.getDate();
    const target = new Date(d.getFullYear(), d.getMonth() + k, 1);
    const lastDay = new Date(target.getFullYear(), target.getMonth() + 1, 0).getDate();
    target.setDate(Math.min(day, lastDay));
    return target;
  }

  const REPEAT_FREQUENCIES = ["daily", "weekly", "biweekly", "monthly"];
  const MAX_RECURRENCE_OCCURRENCES = 104; // mirrors serve.py's expand_recurrence
  const REPEAT_STEP_DAYS = { daily: 1, weekly: 7, biweekly: 14 };

  // Materializes concrete occurrence dates from dueISO (inclusive) through
  // untilISO (inclusive) -- mirrors expand_recurrence() in serve.py. Real,
  // independent to-do rows come out of this, one per date, not a virtual
  // repeating rule -- see the add-todo-form submit handler below. Every
  // occurrence is computed from `start` directly (step count k), never from
  // a running `current` reassigned each loop, so there's no drift
  // accumulation -- see addMonthsJS for why that matters for monthly.
  function expandRecurrence(dueISO, freq, untilISO, maxCount) {
    maxCount = maxCount || MAX_RECURRENCE_OCCURRENCES;
    if (REPEAT_FREQUENCIES.indexOf(freq) === -1) return [dueISO];
    const start = parseISODate(dueISO);
    const end = parseISODate(untilISO);
    if (isNaN(start.getTime()) || isNaN(end.getTime()) || end < start) return [dueISO];

    const out = [isoDate(start)];
    let k = 1;
    while (out.length < maxCount) {
      const current = freq === "monthly"
        ? addMonthsJS(start, k)
        : new Date(start.getFullYear(), start.getMonth(), start.getDate() + REPEAT_STEP_DAYS[freq] * k);
      if (current > end) break;
      out.push(isoDate(current));
      k += 1;
    }
    return out;
  }

  // ---- static event lookups (events are read-only, computed once) -----

  const eventCounts = {};
  for (const e of events) {
    eventCounts[e.day] = (eventCounts[e.day] || 0) + 1;
  }
  function eventsForDay(iso) {
    return events.filter((e) => e.day === iso);
  }

  // Regular weekly class meetings, synthesized from config.yaml's meets/
  // time/room per class — NOT from a live feed (Brightspace hasn't
  // published Fall 2026 meeting schedules for most classes yet; see
  // BUILD.md). This is real, user-entered data (the same fields shown on
  // day detail's class chips already), just never rendered as a schedule
  // block before now. If a class's actual recurring meeting later shows up
  // as its own live feed event, both may appear on the same day — a minor,
  // known overlap, not a fabrication either one.
  // 12-hour clock for display ("8:30 AM", "6:00 PM"), added 2026-08-23 per
  // Charles's request -- everywhere a time is actually shown to a person.
  // Stored/API format stays 24h "HH:MM" everywhere it always was (todo.due
  // + remind_time on disk, the <input type="time"> value) -- only display
  // text changes. Mirrors _format_12h() in serve.py.
  function formatTime12h(hhmm) {
    const m = /^(\\d{1,2}):(\\d{2})$/.exec(hhmm || "");
    if (!m) return hhmm || "";
    const hour = parseInt(m[1], 10);
    const hour12 = hour % 12 || 12;
    const ampm = hour < 12 ? "AM" : "PM";
    return hour12 + ":" + m[2] + " " + ampm;
  }

  const WEEKDAY_ABBR = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  function parseTimeRange(str) {
    // "1:10 pm - 2:00 pm" -> {start: "13:10", end: "14:00"}. Null if the
    // text in config.yaml doesn't match this shape.
    const m = /^\\s*(\\d{1,2}):(\\d{2})\\s*([ap]m)\\s*-\\s*(\\d{1,2}):(\\d{2})\\s*([ap]m)\\s*$/i.exec(str || "");
    if (!m) return null;
    const to24 = (h, mm, ap) => {
      h = parseInt(h, 10) % 12;
      if (ap.toLowerCase() === "pm") h += 12;
      return String(h).padStart(2, "0") + ":" + mm;
    };
    return { start: to24(m[1], m[2], m[3]), end: to24(m[4], m[5], m[6]) };
  }
  function classMeetingsForDay(iso) {
    // Never show a synthesized meeting before the semester actually starts,
    // even on a day that matches the weekly pattern -- config.yaml's
    // term_start is the lower bound (blank means no bound, don't guess one).
    if (data.termStart && iso < data.termStart) return [];
    const abbr = WEEKDAY_ABBR[parseISODate(iso).getDay()];
    const out = [];
    for (const c of data.classesSchedule || []) {
      if (!c.meets.includes(abbr)) continue;
      const range = parseTimeRange(c.time);
      if (!range) continue;
      out.push({
        title: c.name, day: iso, start: range.start, end: range.end,
        all_day: false, location: c.room || "", source: "Class schedule",
        tag: c.tag, isConfigSchedule: true,
      });
    }
    return out;
  }

  function accentFor(source) {
    const wsId = data.feedWorkspaces[source] || data.defaultWorkspace;
    const ws = data.workspaces[wsId];
    return (ws && ws.accent) || "#666666";
  }
  // One-tap Apple Maps directions link. No transport mode is forced, so
  // Maps offers walk/drive/transit like a normal search would.
  function appleMapsUrl(address) {
    return "https://maps.apple.com/?daddr=" + encodeURIComponent(address);
  }
  function makeDirectionsLink(address) {
    const a = document.createElement("a");
    a.className = "directions-link";
    a.href = appleMapsUrl(address);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "\\ud83d\\udccd Directions";
    a.title = "Open directions to " + address + " in Apple Maps";
    a.addEventListener("click", (ev) => ev.stopPropagation());
    return a;
  }
  // An explicit todo.location (a one-off place named in the prompt, or
  // typed into this control) wins; otherwise fall back to the tagged
  // class's configured address, if any.
  function effectiveLocation(todo) {
    return todo.location || (todo.tag && data.tagAddresses[todo.tag]) || null;
  }
  // A to-do's location, editable in place: a Directions pill + pencil icon
  // when one's set (from the to-do itself or its class), or an "Add
  // location" button when there's none to fall back on — so a to-do added
  // without a place (quick-add, or the AI when nothing was named) always
  // has a way to get one attached afterward, not just at creation.
  function renderLocationControl(todo) {
    const wrap = document.createElement("span");
    wrap.className = "todo-location-control";

    function showDisplay() {
      wrap.innerHTML = "";
      const loc = effectiveLocation(todo);
      if (loc) {
        wrap.appendChild(makeDirectionsLink(loc));
        const edit = document.createElement("button");
        edit.type = "button";
        edit.className = "todo-location-edit";
        edit.textContent = "\\u270e";
        edit.title = isOffline ? "Reconnect to edit this to-do's location" : "Edit this to-do's location";
        edit.disabled = isOffline;
        edit.addEventListener("click", (ev) => { ev.stopPropagation(); showEditor(); });
        wrap.appendChild(edit);
      } else {
        const add = document.createElement("button");
        add.type = "button";
        add.className = "todo-location-add";
        add.textContent = "\\ud83d\\udccd Add location";
        add.title = isOffline ? "Reconnect to add a location" : "Add a destination for a one-tap directions link";
        add.disabled = isOffline;
        add.addEventListener("click", (ev) => { ev.stopPropagation(); showEditor(); });
        wrap.appendChild(add);
      }
    }

    function showEditor() {
      wrap.innerHTML = "";
      const input = document.createElement("input");
      input.type = "text";
      input.placeholder = "Location (optional)";
      input.title = "A specific place (e.g. Pacific Pipe) or a general type of place " +
        "(e.g. hardware store) -- a type routes to whichever match is closest when tapped.";
      input.value = todo.location || "";
      input.className = "todo-location-input";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "todo-location-save";
      save.textContent = "\\u2713";
      save.title = "Save";
      function commit() {
        todo.location = input.value.trim() || null;
        saveTodos();
        showDisplay();
      }
      save.addEventListener("click", (ev) => { ev.stopPropagation(); commit(); });
      input.addEventListener("click", (ev) => ev.stopPropagation());
      input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") { ev.preventDefault(); commit(); }
        if (ev.key === "Escape") { showDisplay(); }
      });
      wrap.append(input, save);
      input.focus();
    }

    showDisplay();
    return wrap;
  }

  // ---- live todo lookups (todos mutate; recompute each render) --------

  function todoCountsByDay() {
    const counts = {};
    for (const t of todos) {
      if (!t.done && t.due) counts[t.due] = (counts[t.due] || 0) + 1;
    }
    return counts;
  }
  function todosForDay(iso) {
    return todos.filter((t) => t.due === iso);
  }
  function unscheduledTodos() {
    return todos.filter((t) => !t.due);
  }

  // ---- persistence: GET/POST /api/todos, never localStorage -------------
  // BUILD.md is explicit that to-do data is never seeded from or saved to
  // localStorage — only /api/todos, which serve.py writes atomically.
  //
  // Offline support (added 2026-08-29) is the one deliberate, LABELED
  // exception to that rule. The original rule exists to stop the page from
  // ever passing off stale data as if it were live. This does the
  // opposite: localStorage is used ONLY after a real fetch to /api/todos
  // has just failed, and every time cached data is shown instead, a
  // persistent "Offline — as of <time>" banner says so (showOfflineBanner
  // below) — never silently. Scoped narrowly to what was actually asked
  // for (viewing + checking off to-dos with zero signal): adding, editing,
  // deleting, and location edits still require a live connection and are
  // greyed out via applyOfflineUI()/isOffline checks in renderTodoItem and
  // renderLocationControl — there's no merge logic for those, and guessing
  // at one risks silently losing an edit, which is worse than asking to
  // reconnect. This pairs with the service worker (service-worker.js),
  // which caches the page shell so it opens at all with zero signal in the
  // first place — requires HTTPS, see BUILD.md "Offline support (PWA)".

  function cacheTodosLocally(list) {
    try {
      localStorage.setItem(TODOS_CACHE_KEY, JSON.stringify({
        todos: list, syncedAt: new Date().toISOString(),
      }));
    } catch (err) { /* private browsing / storage full -- best-effort cache */ }
  }
  function loadCachedTodos() {
    try {
      const raw = localStorage.getItem(TODOS_CACHE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (err) { return null; }
  }
  function getPendingToggles() {
    try {
      const raw = localStorage.getItem(PENDING_TOGGLES_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (err) { return {}; }
  }
  // Recorded BEFORE the network attempt, not after it fails -- so a tab
  // killed or reloaded mid-request (phone locks, app gets backgrounded and
  // evicted) doesn't lose the intent along with it.
  function setPendingToggle(id, done) {
    const pending = getPendingToggles();
    pending[id] = done;
    try { localStorage.setItem(PENDING_TOGGLES_KEY, JSON.stringify(pending)); } catch (err) { /* best-effort */ }
  }
  function clearPendingToggles() {
    try { localStorage.removeItem(PENDING_TOGGLES_KEY); } catch (err) { /* best-effort */ }
  }
  function applyPendingToggles(list) {
    const pending = getPendingToggles();
    if (Object.keys(pending).length === 0) return list;
    return list.map((t) => (
      Object.prototype.hasOwnProperty.call(pending, t.id) ? { ...t, done: pending[t.id] } : t
    ));
  }

  function showOfflineBanner(syncedAtISO) {
    const el = document.getElementById("offline-banner");
    const when = syncedAtISO
      ? new Date(syncedAtISO).toLocaleString(undefined, { hour: "numeric", minute: "2-digit" })
      : "unknown";
    el.innerHTML = "\\ud83d\\udcf4 <strong>Offline</strong> \\u2014 showing to-dos as of " + when +
      ". Checking off still works and syncs automatically once you're back online. " +
      "Adding, editing, and deleting are paused until then.";
    el.hidden = false;
  }
  function hideOfflineBanner() {
    document.getElementById("offline-banner").hidden = true;
  }

  // Greys out the controls that need a live connection and have no offline
  // queue of their own (add/quick-add/chat/refresh) every time isOffline
  // flips. Per-todo buttons (delete, location, help) don't need a pass here
  // -- they check `isOffline` directly inside renderTodoItem/
  // renderLocationControl instead, since those redraw on every render anyway.
  function applyOfflineUI() {
    for (const id of ["quick-add-input", "chat-bubble", "grades-refresh-btn", "events-refresh-btn"]) {
      const el = document.getElementById(id);
      if (el) el.disabled = isOffline;
    }
    const addSubmit = document.querySelector("#add-todo-form button[type=submit]");
    if (addSubmit) addSubmit.disabled = isOffline;
    const quickAddSubmit = document.querySelector("#quick-add-form button[type=submit]");
    if (quickAddSubmit) quickAddSubmit.disabled = isOffline;
  }
  function setOffline(offline) {
    isOffline = offline;
    applyOfflineUI();
  }

  async function loadTodos() {
    try {
      const resp = await fetch("/api/todos");
      if (!resp.ok) throw new Error("server returned " + resp.status);
      todos = await resp.json();
      cacheTodosLocally(todos);
      setOffline(false);
      hideOfflineBanner();
      await flushPendingToggles();
    } catch (err) {
      const cached = loadCachedTodos();
      if (cached) {
        todos = applyPendingToggles(cached.todos);
        setOffline(true);
        showOfflineBanner(cached.syncedAt);
      } else {
        // Never fetched successfully even once on this device -- nothing to
        // fall back to. Same visible-failure behavior as before offline
        // support existed.
        todos = [];
        setOffline(true);
        showSaveIndicator("failed", "Could not load to-dos: " + err.message);
      }
    }
    refreshView();
    renderUnscheduled();
    // Today is auto-selected before to-dos finish loading (see below) so its
    // schedule shows immediately -- re-render it now that real to-dos are
    // in, or it'd be stuck showing "no to-dos" from the empty initial state.
    if (selectedDate) renderDayDetail(selectedDate);
  }

  // Pushes queued offline checkbox toggles back to serve.py once reachable
  // again. Fetches the CURRENT server copy first and applies only the
  // pending {id: done} intents onto it -- never blindly POSTs the stale
  // offline `todos` array -- so anything changed elsewhere (the PC itself)
  // while the phone was offline isn't clobbered.
  async function flushPendingToggles() {
    const pending = getPendingToggles();
    if (Object.keys(pending).length === 0) return;
    try {
      const resp = await fetch("/api/todos");
      if (!resp.ok) throw new Error("server returned " + resp.status);
      const serverTodos = await resp.json();
      for (const t of serverTodos) {
        if (Object.prototype.hasOwnProperty.call(pending, t.id)) t.done = pending[t.id];
      }
      todos = serverTodos;
      const ok = await saveTodos();
      if (!ok) return; // still unreachable -- stay queued, try again later
      clearPendingToggles();
      cacheTodosLocally(todos);
      refreshView();
      renderUnscheduled();
      if (selectedDate) renderDayDetail(selectedDate);
    } catch (err) {
      // Still unreachable -- pending toggles stay queued for the next
      // successful loadTodos(), the 'online' event, or the periodic retry.
    }
  }

  async function saveTodos() {
    showSaveIndicator("saving");
    try {
      const resp = await fetch("/api/todos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(todos),
      });
      if (!resp.ok) throw new Error("server returned " + resp.status);
      const data = await resp.json();
      if (data.calendar_errors && data.calendar_errors.length) {
        // The to-do itself saved fine -- only its Google Calendar sync
        // failed. Say so distinctly rather than reporting the save itself
        // as failed (it didn't), but still visibly, per BUILD.md's rule
        // against silently dropping a real failure.
        showSaveIndicator("failed", "saved, but Google Calendar sync failed: " + data.calendar_errors.join("; "));
      } else {
        showSaveIndicator("saved");
      }
      return true;
    } catch (err) {
      // Per BUILD.md: a failed save must be visible and must NOT silently
      // drop the edit. `todos` already has the change in memory and stays
      // that way; the next successful save will include it. We just don't
      // pretend it reached disk.
      showSaveIndicator("failed", err.message);
      return false;
    }
  }

  function showSaveIndicator(state, detail) {
    const el = document.getElementById("save-indicator");
    el.hidden = false;
    if (state === "saving") {
      el.textContent = "Saving...";
      el.className = "save-indicator saving";
    } else if (state === "saved") {
      el.textContent = "Saved";
      el.className = "save-indicator saved";
      setTimeout(() => {
        if (el.className.indexOf("saved") !== -1) el.hidden = true;
      }, 2000);
    } else {
      el.textContent = "Save failed" + (detail ? ": " + detail : "") +
        " \\u2014 kept locally, not yet on disk. Edit again to retry.";
      el.className = "save-indicator failed";
    }
  }

  // ---- todo mutations ---------------------------------------------------

  async function toggleDone(id) {
    const t = todos.find((t) => t.id === id);
    if (!t) return;
    t.done = !t.done;
    // Recorded before the network attempt (see setPendingToggle) so this
    // survives a killed/reloaded tab even if the POST below never finishes.
    setPendingToggle(id, t.done);
    rerenderTodoViews();

    if (isOffline) {
      // Already known unreachable -- skip the fetch rather than block on a
      // request that's likely to hang before failing. The toggle is
      // already queued and already reflected on screen; flushPendingToggles
      // (via loadTodos, the 'online' event, or the periodic retry) picks it
      // up once reachable again.
      const cached = loadCachedTodos();
      showOfflineBanner(cached ? cached.syncedAt : null);
      return;
    }

    const ok = await saveTodos();
    if (ok) {
      clearPendingToggles();
      cacheTodosLocally(todos);
      setOffline(false);
      hideOfflineBanner();
    } else {
      // saveTodos() already showed a generic "Save failed" -- for a toggle
      // specifically this is very likely just offline/unreachable, not a
      // real server error, so soften that into the dedicated offline
      // banner instead of the scarier red failure state.
      setOffline(true);
      const cached = loadCachedTodos();
      showOfflineBanner(cached ? cached.syncedAt : null);
    }
  }
  function deleteTodo(id) {
    const t = todos.find((t) => t.id === id);
    if (t && !confirm('Delete "' + t.title + '"?')) return;
    todos = todos.filter((t) => t.id !== id);
    saveTodos();
    rerenderTodoViews();
  }
  // Deletes every occurrence of a recurring series at once (all to-dos
  // sharing this repeat_group), not just the one clicked -- each occurrence
  // is otherwise an independent to-do, so a plain Delete only ever removes one.
  function deleteSeries(repeatGroup) {
    const count = todos.filter((t) => t.repeat_group === repeatGroup).length;
    if (!confirm("Delete all " + count + " occurrences in this series?")) return;
    todos = todos.filter((t) => t.repeat_group !== repeatGroup);
    saveTodos();
    rerenderTodoViews();
  }
  function addTodo(todo) {
    addTodos([todo]);
  }
  // One save/rerender for the whole batch -- used for both a single add and
  // a recurring to-do's several materialized occurrences (see the
  // add-todo-form submit handler), so adding a semester's worth of weekly
  // to-dos doesn't fire off a separate POST per occurrence.
  function addTodos(newTodos) {
    todos.push(...newTodos);
    saveTodos();
    rerenderTodoViews();
  }
  function rerenderTodoViews() {
    refreshView();
    renderUnscheduled();
    if (selectedDate) renderDayDetail(selectedDate);
  }

  // ---- rendering: week / month toggle -----------------------------------

  function refreshView() {
    const weekEl = document.getElementById("week-cells");
    const monthEl = document.getElementById("month-view");
    if (viewMode === "week") {
      weekEl.hidden = false;
      monthEl.hidden = true;
      renderWeek(weekAnchor);
    } else {
      weekEl.hidden = true;
      monthEl.hidden = false;
      renderMonth(monthAnchor);
    }
  }

  function renderWeek(monday) {
    const container = document.getElementById("week-cells");
    container.innerHTML = "";
    const today = isoDate(new Date());
    const todoCounts = todoCountsByDay();
    for (let i = 0; i < 7; i++) {
      const d = new Date(monday);
      d.setDate(d.getDate() + i);
      const iso = isoDate(d);
      const evCount = (eventCounts[iso] || 0) + classMeetingsForDay(iso).length;
      const todoCount = todoCounts[iso] || 0;

      const btn = document.createElement("button");
      btn.type = "button";
      let cls = "day-cell";
      if (iso === today) cls += " today";
      if (iso === selectedDate) cls += " selected";
      btn.className = cls;
      btn.dataset.date = iso;
      btn.dataset.heat = String(heatLevel(evCount + todoCount));

      const weekdaySpan = document.createElement("span");
      weekdaySpan.className = "weekday";
      weekdaySpan.textContent = weekdayNames[i];

      const dateSpan = document.createElement("span");
      dateSpan.className = "date-num";
      dateSpan.textContent = String(d.getDate());

      const countsSpan = document.createElement("span");
      countsSpan.className = "counts";
      let label = evCount + " event" + (evCount !== 1 ? "s" : "");
      if (todoCount > 0) label += ", " + todoCount + " to-do" + (todoCount !== 1 ? "s" : "");
      countsSpan.textContent = label;

      btn.append(weekdaySpan, dateSpan, countsSpan);
      btn.addEventListener("click", () => selectDay(iso));
      container.appendChild(btn);
    }
  }

  function renderMonth(anchor) {
    const label = document.getElementById("month-label");
    label.textContent = anchor.toLocaleDateString(undefined, { month: "long", year: "numeric" });

    const grid = document.getElementById("month-grid");
    grid.innerHTML = "";
    const year = anchor.getFullYear();
    const month = anchor.getMonth();
    const startOffset = (new Date(year, month, 1).getDay() + 6) % 7; // Mon-first
    const gridStart = new Date(year, month, 1 - startOffset);

    const today = isoDate(new Date());
    const todoCounts = todoCountsByDay();
    for (let i = 0; i < 42; i++) {
      const d = new Date(gridStart);
      d.setDate(d.getDate() + i);
      const iso = isoDate(d);
      const inMonth = d.getMonth() === month;
      const evCount = (eventCounts[iso] || 0) + classMeetingsForDay(iso).length;
      const todoCount = todoCounts[iso] || 0;

      const cell = document.createElement("button");
      cell.type = "button";
      let cls = "month-cell";
      if (!inMonth) cls += " outside";
      if (iso === today) cls += " today";
      if (iso === selectedDate) cls += " selected";
      cell.className = cls;
      cell.dataset.date = iso;
      cell.dataset.heat = String(heatLevel(evCount + todoCount));
      cell.title = evCount + " event" + (evCount !== 1 ? "s" : "") +
        ", " + todoCount + " to-do" + (todoCount !== 1 ? "s" : "");

      const num = document.createElement("span");
      num.className = "date-num";
      num.textContent = String(d.getDate());
      cell.appendChild(num);

      if (evCount || todoCount) {
        const counts = document.createElement("span");
        counts.className = "counts";
        const bits = [];
        if (evCount) bits.push(evCount + " ev");
        if (todoCount) bits.push(todoCount + " to-do");
        counts.textContent = bits.join(", ");
        cell.appendChild(counts);
      }

      cell.addEventListener("click", () => selectDay(iso));
      grid.appendChild(cell);
    }
  }

  function selectDay(iso) {
    selectedDate = iso;
    document.querySelectorAll(".day-cell, .month-cell").forEach((cell) => {
      cell.classList.toggle("selected", cell.dataset.date === iso);
    });
    renderDayDetail(iso);
  }

  // ---- day detail: schedule (read-only) + to-dos (editable) -------------

  function renderDayDetail(iso) {
    const d = parseISODate(iso);
    document.getElementById("day-detail-title").textContent =
      d.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });

    const scheduleEl = document.getElementById("day-schedule");
    scheduleEl.innerHTML = "";
    const dayEvents = eventsForDay(iso).concat(classMeetingsForDay(iso));
    if (dayEvents.length === 0) {
      scheduleEl.innerHTML = '<p class="empty">No events on this day.</p>';
    } else {
      const timed = dayEvents.filter((e) => !e.all_day)
        .sort((a, b) => (a.start || "").localeCompare(b.start || ""));
      const allDay = dayEvents.filter((e) => e.all_day);
      const list = document.createElement("ul");
      list.className = "schedule-list";
      for (const e of timed.concat(allDay)) list.appendChild(renderScheduleItem(e));
      scheduleEl.appendChild(list);
    }

    const todosEl = document.getElementById("day-todos");
    todosEl.innerHTML = "";
    const dTodos = todosForDay(iso);
    if (dTodos.length === 0) {
      todosEl.innerHTML = '<p class="empty">No to-dos for this day yet.</p>';
    } else {
      const list = document.createElement("ul");
      list.className = "todo-list";
      for (const t of dTodos) list.appendChild(renderTodoItem(t));
      todosEl.appendChild(list);
    }

    const form = document.getElementById("add-todo-form");
    form.hidden = false;
    form.dataset.due = iso;
  }

  function renderScheduleItem(e) {
    const li = document.createElement("li");
    li.className = "schedule-item";

    const time = document.createElement("span");
    time.className = "sched-time";
    time.textContent = e.all_day ? "All day" :
      formatTime12h(e.start) + (e.end ? "\\u2013" + formatTime12h(e.end) : "");

    const main = document.createElement("span");
    main.className = "sched-main";
    if (e.tag) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.setProperty("--tone-raw", accentFor(e.source));
      chip.textContent = data.tagNames[e.tag] || e.tag;
      main.appendChild(chip);
    }
    const titleSpan = document.createElement("span");
    titleSpan.textContent = e.title + (e.location ? " \\u2014 " + e.location : "");
    main.appendChild(titleSpan);

    li.append(time, main);
    // A class's mapable address comes from config.yaml (set once per class,
    // not per event) — separate from e.location above, which is whatever
    // free-text the source calendar put in the event itself and may not be
    // a real address at all.
    const address = e.tag && data.tagAddresses[e.tag];
    if (address) li.appendChild(makeDirectionsLink(address));
    // Events are read-only in this UI (BUILD.md: two sources of truth means
    // silent drift). Clicking reveals where to actually edit it, plus
    // office-hours/contact info for the class, if any is on file.
    li.addEventListener("click", () => {
      const existing = li.querySelector(".readonly-note");
      if (existing) {
        existing.remove();
        const existingInfo = li.querySelector(".class-info");
        if (existingInfo) existingInfo.remove();
        return;
      }
      const note = document.createElement("div");
      note.className = "readonly-note";
      note.textContent = e.isConfigSchedule
        ? "Regular meeting time, from config.yaml \\u2014 edit it there if your schedule changes."
        : "Read-only \\u2014 edit this in " + e.source + ".";
      li.appendChild(note);

      const instr = e.tag && data.tagInstructors[e.tag];
      if (instr) {
        const info = document.createElement("div");
        info.className = "class-info";
        if (instr.name) {
          const nameLine = document.createElement("div");
          nameLine.className = "instructor-name";
          nameLine.textContent = instr.name;
          info.appendChild(nameLine);
        }
        if (instr.email) {
          const emailLine = document.createElement("div");
          const link = document.createElement("a");
          link.href = "mailto:" + instr.email;
          link.textContent = instr.email;
          emailLine.appendChild(link);
          info.appendChild(emailLine);
        }
        if (instr.office) {
          const officeLine = document.createElement("div");
          officeLine.textContent = "Office: " + instr.office;
          info.appendChild(officeLine);
        }
        if (instr.office_hours) {
          const hoursLine = document.createElement("div");
          hoursLine.textContent = "Office hours: " + instr.office_hours;
          info.appendChild(hoursLine);
        }
        li.appendChild(info);
      }

      // Course's Brightspace page, only where config.yaml sets one -- see
      // render_deadlines() in build_dashboard.py for why this links to the
      // course, not a specific assignment (Brightspace's feed carries no
      // per-assignment URL).
      const bsUrl = e.tag && data.tagBrightspace[e.tag];
      if (bsUrl) {
        const info = li.querySelector(".class-info") || (() => {
          const created = document.createElement("div");
          created.className = "class-info";
          li.appendChild(created);
          return created;
        })();
        const bsLine = document.createElement("div");
        const bsLink = document.createElement("a");
        bsLink.href = bsUrl;
        bsLink.target = "_blank";
        bsLink.rel = "noopener noreferrer";
        bsLink.textContent = "Open in Brightspace";
        bsLine.appendChild(bsLink);
        info.appendChild(bsLine);
      }
    });
    return li;
  }

  function renderTodoItem(todo) {
    const li = document.createElement("li");
    li.className = "todo-item" + (todo.done ? " done" : "");

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = todo.done;
    checkbox.addEventListener("change", () => toggleDone(todo.id));

    const title = document.createElement("span");
    title.className = "todo-title";
    title.textContent = todo.title;

    const notes = document.createElement("span");
    notes.className = "todo-notes";
    notes.textContent = todo.notes || "";

    li.append(checkbox, title, notes);

    if (todo.is_exam) {
      const examTag = document.createElement("span");
      examTag.className = "todo-exam-tag";
      examTag.textContent = "\\ud83d\\udcda Exam";
      examTag.title = "Extra Google Calendar reminders 1 and 2 weeks before, on top of the day-of one";
      li.appendChild(examTag);
    }

    if (todo.repeat_group) {
      const repeatTag = document.createElement("span");
      repeatTag.className = "todo-repeat-tag";
      repeatTag.textContent = "\\ud83d\\udd01 series";
      repeatTag.title = "One occurrence of a recurring series -- each is independent; " +
        "completing or deleting this one doesn't touch the others";
      li.appendChild(repeatTag);
    }

    li.appendChild(renderLocationControl(todo));

    if (todo.due && todo.remind_time) {
      const remindTag = document.createElement("span");
      remindTag.className = "todo-remind-time";
      remindTag.textContent = "\\u23f0 " + formatTime12h(todo.remind_time);
      remindTag.title = "Google Calendar reminder at " + formatTime12h(todo.remind_time) + " on " + todo.due
        + " (overrides the config.yaml default)";
      li.appendChild(remindTag);
    }

    if (!todo.done) {
      const help = document.createElement("button");
      help.type = "button";
      help.className = "todo-help";
      help.textContent = "Get help";
      help.title = isOffline
        ? "Reconnect to ask the assistant for help"
        : "Ask the assistant how to make progress on this, with a draft email if it involves someone";
      help.disabled = isOffline;
      help.addEventListener("click", () => requestHelp(todo));
      li.appendChild(help);
    }

    // Delete (and location edits, below) aren't part of offline support --
    // see the "offline cache + pending-toggle queue" comment near
    // loadTodos: only the checkbox has a queue to sync later, so these stay
    // disabled while offline rather than risk a silently lost edit.
    const del = document.createElement("button");
    del.type = "button";
    del.className = "todo-delete";
    del.textContent = "Delete";
    del.disabled = isOffline;
    if (isOffline) del.title = "Reconnect to delete a to-do";
    del.addEventListener("click", () => deleteTodo(todo.id));
    li.appendChild(del);

    if (todo.repeat_group) {
      const delSeries = document.createElement("button");
      delSeries.type = "button";
      delSeries.className = "todo-delete";
      delSeries.textContent = "Delete series";
      delSeries.title = isOffline
        ? "Reconnect to delete a series"
        : "Delete every occurrence of this recurring to-do, not just this one";
      delSeries.disabled = isOffline;
      delSeries.addEventListener("click", () => deleteSeries(todo.repeat_group));
      li.appendChild(delSeries);
    }

    return li;
  }

  // ---- "Get help" -> chat bubble, prefilled and auto-submitted ----------
  // Reuses the existing chat bubble/tools wholesale (see the chat bubble
  // section below and serve.py's build_system_prompt): no new AI plumbing,
  // just a canned prompt handed to the same /api/chat path a typed message
  // would use.

  function requestHelp(todo) {
    const bits = [
      'Help me make progress on this to-do: "' + todo.title + '"' +
        (todo.due ? " (due " + todo.due + ")" : " (no due date)") + ".",
    ];
    if (todo.notes) bits.push("Notes: " + todo.notes);
    bits.push(
      "If this involves contacting, emailing, or messaging someone, use web " +
      "search to find relevant public info about them first, then draft the " +
      "email as plain text for me to copy — don't send or save anything, " +
      "you can't anyway. If you're missing something you'd need (their exact " +
      "name, role, email, the tone, or what I actually want from them), ask " +
      "me first instead of guessing."
    );
    openChat();
    chatInput.value = bits.join(" ");
    chatForm.requestSubmit();
  }

  function renderUnscheduled() {
    const container = document.getElementById("unscheduled-todos");
    container.innerHTML = "";
    const items = unscheduledTodos();
    if (items.length === 0) {
      container.innerHTML = '<p class="empty">Nothing unscheduled.</p>';
      return;
    }
    const list = document.createElement("ul");
    list.className = "todo-list";
    for (const t of items) list.appendChild(renderTodoItem(t));
    container.appendChild(list);
  }

  // ---- quick add: AI-backed, stateless, to-do tools only -----------------
  // The old version was a local regex parser that couldn't summarize and
  // couldn't remove anything -- it just used your typed text as the title
  // verbatim. This calls /api/quick-add, which asks the model to write a
  // clean summarized title (add), or find-and-remove/update an existing
  // to-do, depending on what you typed. One tool call, one confirmation
  // sentence, no persisted conversation (unlike the chat bubble).

  // ---- wire up controls ---------------------------------------------------

  document.getElementById("view-week-btn").addEventListener("click", () => {
    viewMode = "week";
    document.getElementById("view-week-btn").classList.add("active");
    document.getElementById("view-month-btn").classList.remove("active");
    refreshView();
  });
  document.getElementById("view-month-btn").addEventListener("click", () => {
    viewMode = "month";
    document.getElementById("view-month-btn").classList.add("active");
    document.getElementById("view-week-btn").classList.remove("active");
    refreshView();
  });
  document.getElementById("prev-btn").addEventListener("click", () => {
    if (viewMode === "week") weekAnchor.setDate(weekAnchor.getDate() - 7);
    else monthAnchor.setMonth(monthAnchor.getMonth() - 1);
    refreshView();
  });
  document.getElementById("next-btn").addEventListener("click", () => {
    if (viewMode === "week") weekAnchor.setDate(weekAnchor.getDate() + 7);
    else monthAnchor.setMonth(monthAnchor.getMonth() + 1);
    refreshView();
  });
  document.getElementById("today-btn").addEventListener("click", () => {
    weekAnchor = mondayOf(new Date());
    monthAnchor = firstOfMonth(new Date());
    refreshView();
  });

  // Repeat's "until" date only makes sense once a frequency is picked --
  // hidden the rest of the time so the form doesn't ask for it up front.
  const repeatSelect = document.getElementById("todo-repeat");
  const repeatUntilInput = document.getElementById("todo-repeat-until");
  repeatSelect.addEventListener("change", () => {
    repeatUntilInput.hidden = repeatSelect.value === "none";
  });

  document.getElementById("add-todo-form").addEventListener("submit", (e) => {
    e.preventDefault();
    if (isOffline) return; // submit button is disabled offline; this is a defensive backstop
    const form = e.target;
    const titleInput = document.getElementById("todo-title");
    const notesInput = document.getElementById("todo-notes");
    const locationInput = document.getElementById("todo-location");
    const remindTimeInput = document.getElementById("todo-remind-time");
    const isExamInput = document.getElementById("todo-is-exam");
    const title = titleInput.value.trim();
    if (!title) return;

    const due = form.dataset.due || null;
    const repeatFreq = repeatSelect.value;
    const repeatUntil = repeatUntilInput.value || null;

    // Recurring: materialize one independent to-do per occurrence (see
    // expandRecurrence above), sharing a repeat_group id so "Delete series"
    // can find them all later -- not a virtual repeating rule, so each
    // occurrence can be completed or deleted on its own.
    let dueDates = [due];
    let repeatGroup = null;
    if (due && repeatFreq !== "none" && repeatUntil) {
      dueDates = expandRecurrence(due, repeatFreq, repeatUntil);
      if (dueDates.length > 1) repeatGroup = makeId();
    }

    const newTodos = dueDates.map((occDue) => ({
      id: makeId(),
      title: title,
      due: occDue,
      notes: notesInput.value.trim(),
      tag: null,
      done: false,
      created: new Date().toISOString(),
      // Optional per-to-do override of config.yaml's reminders.send_time —
      // blank means "use the default." Only meaningful once `due` is set;
      // serve.py's scheduler ignores it otherwise.
      remind_time: remindTimeInput.value || null,
      // A specific place to go — turns into a one-tap Apple Maps directions
      // link on the reminder email. Blank means none (or, for a class-tagged
      // to-do, whatever address that class has in config.yaml).
      location: locationInput.value.trim() || null,
      // Exams (any class) get extra 1-/2-week-out Calendar reminders on top
      // of the day-of one — see reconcile_todo_calendar_event in serve.py.
      is_exam: isExamInput.checked,
      repeat_group: repeatGroup,
    }));
    addTodos(newTodos);

    titleInput.value = "";
    notesInput.value = "";
    locationInput.value = "";
    remindTimeInput.value = "";
    isExamInput.checked = false;
    repeatSelect.value = "none";
    repeatUntilInput.value = "";
    repeatUntilInput.hidden = true;
  });

  document.getElementById("quick-add-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (isOffline) return; // submit button is disabled offline; this is a defensive backstop
    const input = document.getElementById("quick-add-input");
    const submitBtn = e.target.querySelector("button[type=submit]");
    const feedback = document.getElementById("quick-add-feedback");
    const text = input.value.trim();
    if (!text) return;

    input.disabled = true;
    submitBtn.disabled = true;
    feedback.hidden = false;
    feedback.className = "quick-add-feedback";
    feedback.textContent = "Working on it...";

    try {
      const resp = await fetch("/api/quick-add", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      const data = await resp.json();
      if (data.error) {
        feedback.className = "quick-add-feedback error";
        feedback.textContent = data.error;
      } else {
        feedback.className = "quick-add-feedback";
        feedback.textContent = data.reply;
        input.value = "";
        loadTodos();
        if (selectedDate) renderDayDetail(selectedDate);
      }
    } catch (err) {
      feedback.className = "quick-add-feedback error";
      feedback.textContent = "Couldn't reach the assistant: " + err.message;
    } finally {
      input.disabled = false;
      submitBtn.disabled = false;
      input.focus();
    }
  });

  refreshView();
  // Auto-select today so day detail shows something useful on load instead
  // of starting on "Select a day above" -- added 2026-08-23. Runs after
  // refreshView() so today's cell already exists in the DOM to be marked
  // selected; loadTodos() re-renders day detail again once real to-dos
  // arrive (see the comment in loadTodos above).
  selectDay(isoDate(new Date()));
  loadTodos();

  // ---- offline recovery: retry when connectivity might have come back ---
  // 'online' fires when the network INTERFACE comes back (leaving a dead
  // zone) -- but the phone can also have signal the whole time while just
  // the PC/Tailscale is unreachable (asleep, Tailscale hiccup), which that
  // event never fires for. The periodic retry below is what recovers that
  // case, without needing a manual page reload.
  window.addEventListener("online", () => { loadTodos(); });
  setInterval(() => { if (isOffline) loadTodos(); }, 30000);

  // ---- grades: refresh button --------------------------------------------
  // Runs scripts/fetch_grades.py server-side (a persisted Brightspace
  // session, no page-visible auth) then rebuilds the dashboard -- same
  // "server owns real data, page just re-fetches it" shape as everything
  // else here. A full reload picks up the rebuilt panel, same tradeoff
  // link add/remove below makes.

  const gradesRefreshBtn = document.getElementById("grades-refresh-btn");
  if (gradesRefreshBtn) {
    gradesRefreshBtn.addEventListener("click", async () => {
      gradesRefreshBtn.disabled = true;
      gradesRefreshBtn.textContent = "Refreshing...";
      showSaveIndicator("saving");
      try {
        const resp = await fetch("/api/refresh-grades");
        const data = await resp.json();
        if (data.refresh_error) {
          showSaveIndicator("failed", "grades refresh: " + data.refresh_error);
          gradesRefreshBtn.disabled = false;
          gradesRefreshBtn.textContent = "Refresh grades";
        } else {
          location.reload();
        }
      } catch (err) {
        showSaveIndicator("failed", "Couldn't reach the server: " + err.message);
        gradesRefreshBtn.disabled = false;
        gradesRefreshBtn.textContent = "Refresh grades";
      }
    });
  }

  // ---- events: refresh button --------------------------------------------
  // Same shape as the grades refresh button above: runs fetch_brightspace.py
  // server-side, rebuilds the dashboard, then a full reload picks up the
  // refreshed week strip / deadlines / feed status.

  const eventsRefreshBtn = document.getElementById("events-refresh-btn");
  if (eventsRefreshBtn) {
    eventsRefreshBtn.addEventListener("click", async () => {
      eventsRefreshBtn.disabled = true;
      eventsRefreshBtn.textContent = "Refreshing...";
      showSaveIndicator("saving");
      try {
        const resp = await fetch("/api/refresh");
        const data = await resp.json();
        if (data.refresh_error) {
          showSaveIndicator("failed", "events refresh: " + data.refresh_error);
          eventsRefreshBtn.disabled = false;
          eventsRefreshBtn.textContent = "Refresh events";
        } else {
          location.reload();
        }
      } catch (err) {
        showSaveIndicator("failed", "Couldn't reach the server: " + err.message);
        eventsRefreshBtn.disabled = false;
        eventsRefreshBtn.textContent = "Refresh events";
      }
    });
  }

  // ---- quick links: add/remove, server-rendered from config.yaml ---------
  // Deterministic edits (exact label/url from a form) -- no AI involved,
  // unlike quick-add and the chat bubble. Instant and free. A full page
  // reload after each change is the simplest way to reflect the rebuilt
  // dashboard (same tradeoff as chat's config edits, which need one too).

  document.querySelectorAll(".link-remove").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm('Remove "' + btn.dataset.label + '"?')) return;
      btn.disabled = true;
      try {
        const resp = await fetch("/api/links/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: btn.dataset.workspace, label: btn.dataset.label }),
        });
        const data = await resp.json();
        if (data.error) {
          alert(data.error);
          btn.disabled = false;
        } else {
          location.reload();
        }
      } catch (err) {
        alert("Couldn't reach the server: " + err.message);
        btn.disabled = false;
      }
    });
  });

  document.querySelectorAll(".add-link-form").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const label = form.elements["label"].value.trim();
      const url = form.elements["url"].value.trim();
      if (!label || !url) return;
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      try {
        const resp = await fetch("/api/links/add", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ workspace_id: form.dataset.workspace, label, url }),
        });
        const data = await resp.json();
        if (data.error) {
          alert(data.error);
          btn.disabled = false;
        } else {
          location.reload();
        }
      } catch (err) {
        alert("Couldn't reach the server: " + err.message);
        btn.disabled = false;
      }
    });
  });

  // ---- chat bubble --------------------------------------------------------

  const chatBubble = document.getElementById("chat-bubble");
  const chatPanel = document.getElementById("chat-panel");
  const chatMessages = document.getElementById("chat-messages");
  const chatForm = document.getElementById("chat-form");
  const chatInput = document.getElementById("chat-input");

  // Small hand-rolled markdown renderer — no CDN, so no markdown library.
  // Escapes raw text via the browser's own HTML-escaping (textContent ->
  // innerHTML round trip) BEFORE any markdown substitution, so tags in the
  // model's own reply text can never inject real HTML.
  function escapeHtml(s) {
    const div = document.createElement("div");
    div.textContent = s;
    return div.innerHTML;
  }

  function inlineMarkdown(text) {
    return text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
      .replace(/\\*([^*]+)\\*/g, "<em>$1</em>")
      .replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  }

  function renderMarkdown(raw) {
    const escaped = escapeHtml(raw);

    // Pull fenced code blocks out first (as placeholders) so their contents
    // aren't run through paragraph/list/inline handling below.
    const codeBlocks = [];
    const withPlaceholders = escaped.replace(/```([\\s\\S]*?)```/g, (_, code) => {
      codeBlocks.push(code.replace(/^\\n/, "").replace(/\\n$/, ""));
      return " CODEBLOCK" + (codeBlocks.length - 1) + " ";
    });

    const lines = withPlaceholders.split("\\n");
    const htmlParts = [];
    let listBuffer = [];

    function flushList() {
      if (listBuffer.length) {
        htmlParts.push("<ul>" + listBuffer.map((li) => "<li>" + inlineMarkdown(li) + "</li>").join("") + "</ul>");
        listBuffer = [];
      }
    }

    for (const line of lines) {
      const trimmed = line.trim();
      const blockMatch = trimmed.match(/^ CODEBLOCK(\\d+) $/);
      if (blockMatch) {
        flushList();
        htmlParts.push("<pre><code>" + codeBlocks[Number(blockMatch[1])] + "</code></pre>");
      } else if (/^[-*]\\s+/.test(trimmed)) {
        listBuffer.push(trimmed.replace(/^[-*]\\s+/, ""));
      } else if (trimmed === "") {
        flushList();
      } else {
        flushList();
        htmlParts.push("<p>" + inlineMarkdown(trimmed) + "</p>");
      }
    }
    flushList();
    return htmlParts.join("") || "<p></p>";
  }

  function addChatMessage(role, text) {
    const div = document.createElement("div");
    div.className = "chat-msg " + role;
    if (role === "user") {
      div.textContent = text; // what you typed, shown verbatim, no markdown
    } else {
      div.innerHTML = renderMarkdown(text);
    }
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return div;
  }

  function openChat() {
    chatPanel.hidden = false;
    chatBubble.hidden = true;
    chatInput.focus();
  }
  function closeChat() {
    chatPanel.hidden = true;
    chatBubble.hidden = false;
  }

  chatBubble.addEventListener("click", openChat);
  document.getElementById("chat-close").addEventListener("click", closeChat);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !chatPanel.hidden) closeChat();
  });

  chatForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const text = chatInput.value.trim();
    if (!text) return;
    addChatMessage("user", text);
    chatInput.value = "";
    chatInput.disabled = true;
    const pending = addChatMessage("assistant", "...");

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await resp.json();
      pending.remove();
      if (data.error) {
        addChatMessage("error", data.error);
      } else {
        addChatMessage("assistant", data.reply);
        // The assistant may have added/edited to-dos, or edited config.yaml
        // (which needs a full page refresh to show — it says so in its
        // reply when that happens). Refresh the parts we can update live.
        loadTodos();
      }
    } catch (err) {
      pending.remove();
      addChatMessage("error", "Couldn't reach the assistant: " + err.message);
    } finally {
      chatInput.disabled = false;
      chatInput.focus();
    }
  });
})();
"""


def render_manifest(owner_name: str) -> str:
    """dashboard/manifest.webmanifest -- generated (like index.html) so it
    can carry the real owner name, alongside service-worker.js and the
    dashboard/icons/*.png files, which are static and hand-authored (they
    don't depend on config.yaml/events.json, so there's nothing to
    regenerate). See BUILD.md, "Offline support (PWA)"."""
    manifest = {
        "name": f"{owner_name}'s Planner",
        "short_name": "Planner",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#f7f7f5",
        "theme_color": "#007155",
        "icons": [
            {"src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        ],
    }
    return json.dumps(manifest, indent=2)


def render_html(config: dict, events_payload: dict, grades_payload: dict, today: date) -> str:
    events = events_payload.get("events", [])
    owner = config.get("owner", {}) or {}
    owner_name = owner.get("name") or "Your"
    term = owner.get("term", "")
    school = owner.get("school", "")
    generated = events_payload.get("generated") or "never"

    tag_names = {c["tag"]: c.get("name") or c["tag"]
                 for c in (config.get("classes") or [])}
    tag_addresses = {c["tag"]: c["address"] for c in (config.get("classes") or [])
                      if (c.get("address") or "").strip()}
    # Office hours/email/office, only for classes that actually have it set —
    # never guessed, same rule as address/grades_url.
    tag_instructors = {c["tag"]: c["instructor"] for c in (config.get("classes") or [])
                        if c.get("instructor")}
    # Course's Brightspace page, only where actually set — see render_deadlines
    # for why this is the course page, not a specific assignment. A class
    # with same_class_as set (see config.yaml's schema comment) inherits its
    # link from the aliased tag when it hasn't set its own — so e.g.
    # CHEM-1400-EVE's schedule block still gets a working "Open in
    # Brightspace" link once CHEM-1400's is set, without duplicating the URL
    # in config.yaml.
    classes_by_tag = {c["tag"]: c for c in (config.get("classes") or []) if c.get("tag")}
    tag_brightspace: dict[str, str] = {}
    for c in (config.get("classes") or []):
        tag = c.get("tag")
        if not tag:
            continue
        url = (c.get("brightspace_url") or "").strip()
        if not url and c.get("same_class_as"):
            source = classes_by_tag.get(c["same_class_as"])
            url = (source.get("brightspace_url") or "").strip() if source else ""
        if url:
            tag_brightspace[tag] = url
    # Real, config-entered meeting info, used to synthesize a schedule block
    # for classes the live feed hasn't published events for yet (see
    # classMeetingsForDay in the generated JS).
    classes_schedule = [
        {"tag": c["tag"], "name": c.get("name") or c["tag"],
         "meets": c.get("meets") or [], "time": c.get("time") or "",
         "room": c.get("room") or ""}
        for c in (config.get("classes") or [])
        if c.get("meets") and c.get("time")
    ]
    workspaces_list = config.get("workspaces") or []
    workspaces_info = {w["id"]: {"accent": w.get("accent", "#666666")}
                        for w in workspaces_list}
    default_ws_id = next((w["id"] for w in workspaces_list if w.get("default")), None)

    week = build_week_strip(today, events)
    deadlines = build_deadlines(today, events, config)
    grouped = group_by_tag(deadlines)

    week_cells_html = "\n".join(render_day_cell(d) for d in week)
    deadlines_html = render_deadlines(grouped, today, config, tag_names, tag_brightspace)
    grades_html = render_grades(config, grades_payload, tag_names)
    grades_generated = grades_payload.get("generated") or "never"

    client_data = {
        "events": events,
        "tagNames": tag_names,
        "tagAddresses": tag_addresses,
        "tagInstructors": tag_instructors,
        "tagBrightspace": tag_brightspace,
        "classesSchedule": classes_schedule,
        "termStart": (owner.get("term_start") or "").strip() or None,
        "feedWorkspaces": config.get("feed_workspaces", {}) or {},
        "workspaces": workspaces_info,
        "defaultWorkspace": default_ws_id,
    }

    parts = []
    parts.append("<!DOCTYPE html>\n")
    parts.append('<html lang="en">\n<head>\n')
    parts.append('<meta charset="utf-8">\n')
    parts.append('<meta name="viewport" content="width=device-width, initial-scale=1">\n')
    parts.append(f"<title>{esc(owner_name)}'s Planner</title>\n")
    # PWA installability + offline support (see BUILD.md, "Offline support
    # (PWA)") -- manifest.webmanifest is generated alongside this file (see
    # render_manifest/main below); service-worker.js and the icons are
    # static, hand-authored assets in dashboard/. Requires HTTPS to actually
    # register (see BUILD.md) -- these tags are harmless no-ops over plain
    # HTTP, which is why they're unconditional here.
    parts.append('<link rel="manifest" href="/manifest.webmanifest">\n')
    parts.append('<meta name="theme-color" content="#007155">\n')
    parts.append('<link rel="apple-touch-icon" href="/icons/icon-192.png">\n')
    parts.append('<meta name="apple-mobile-web-app-capable" content="yes">\n')
    parts.append('<meta name="apple-mobile-web-app-status-bar-style" content="default">\n')
    parts.append(f'<meta name="apple-mobile-web-app-title" content="{esc(owner_name)} Planner">\n')
    parts.append("<style>\n" + CSS + "\n</style>\n")
    parts.append("</head>\n<body>\n")

    parts.append('<header class="page-header">\n')
    parts.append(f"  <h1>{esc(owner_name)}'s Planner</h1>\n")
    parts.append(f'  <p class="subtitle">{esc(school)} — {esc(term)}</p>\n')
    parts.append("</header>\n")

    # Populated/shown by the generated JS the moment a real /api/todos fetch
    # fails -- see loadTodos(). Empty and hidden here since server-rendered
    # HTML has no way to know connectivity at build time.
    parts.append('<div id="offline-banner" class="offline-banner" hidden></div>\n')

    parts.append('<section class="panel calendar-panel" aria-label="Calendar">\n')
    parts.append('  <div class="view-controls">\n')
    parts.append('    <div class="nav-controls">\n')
    parts.append('      <button id="prev-btn" type="button">&larr; Prev</button>\n')
    parts.append('      <button id="today-btn" type="button">Today</button>\n')
    parts.append('      <button id="next-btn" type="button">Next &rarr;</button>\n')
    parts.append("    </div>\n")
    parts.append('    <div class="view-toggle">\n')
    parts.append('      <button id="view-week-btn" type="button" class="view-btn active">Week</button>\n')
    parts.append('      <button id="view-month-btn" type="button" class="view-btn">Month</button>\n')
    parts.append("    </div>\n")
    parts.append("  </div>\n")
    parts.append('  <div id="week-cells" class="week-cells">\n')
    parts.append(week_cells_html + "\n")
    parts.append("  </div>\n")
    parts.append('  <div id="month-view" class="month-view" hidden>\n')
    parts.append('    <p id="month-label" class="month-label"></p>\n')
    parts.append('    <div class="month-weekday-header">\n')
    parts.append("      " + "".join(f"<span>{w}</span>" for w in WEEKDAY_NAMES) + "\n")
    parts.append("    </div>\n")
    parts.append('    <div id="month-grid" class="month-grid"></div>\n')
    parts.append("  </div>\n")
    parts.append("</section>\n")

    parts.append('<section class="panel day-detail" aria-label="Day detail">\n')
    parts.append('  <h2 id="day-detail-title">Select a day</h2>\n')
    parts.append('  <div id="day-schedule">\n')
    parts.append('    <p class="empty">Click a day above to see its schedule and to-dos.</p>\n')
    parts.append("  </div>\n")
    parts.append('  <div id="day-todos"></div>\n')
    parts.append('  <form id="add-todo-form" class="add-todo-form" hidden>\n')
    parts.append('    <input type="text" id="todo-title" placeholder="To-do title" required>\n')
    parts.append('    <input type="text" id="todo-notes" placeholder="Notes (optional)">\n')
    parts.append('    <input type="text" id="todo-location" placeholder="Location (optional)" '
                  'title="A specific place (e.g. Pacific Pipe) or a general type of place '
                  '(e.g. hardware store) -- adds a one-tap Apple Maps link to the reminder. '
                  'A type routes to whichever match is closest when you tap it, not one '
                  'fixed store.">\n')
    parts.append('    <input type="time" id="todo-remind-time" '
                  'title="Remind me at (optional) — defaults to the reminders.send_time '
                  'in config.yaml if left blank">\n')
    parts.append('    <select id="todo-repeat" title="Repeat this to-do -- creates one '
                  'independent to-do per occurrence, each with its own reminder">\n')
    parts.append('      <option value="none">Does not repeat</option>\n')
    parts.append('      <option value="daily">Daily</option>\n')
    parts.append('      <option value="weekly">Weekly</option>\n')
    parts.append('      <option value="biweekly">Biweekly</option>\n')
    parts.append('      <option value="monthly">Monthly</option>\n')
    parts.append('    </select>\n')
    parts.append('    <input type="date" id="todo-repeat-until" hidden '
                  'title="Last occurrence (inclusive) -- required when Repeat is set">\n')
    parts.append('    <label class="todo-exam-label" title="Adds extra Google Calendar '
                  'reminders 1 and 2 weeks before this due date, on top of the normal '
                  'day-of one.">\n')
    parts.append('      <input type="checkbox" id="todo-is-exam"> Exam\n')
    parts.append("    </label>\n")
    parts.append('    <button type="submit">Add</button>\n')
    parts.append("  </form>\n")
    parts.append("</section>\n")

    parts.append('<section class="panel quick-add" aria-label="Quick add">\n')
    parts.append("  <h2>Quick add</h2>\n")
    parts.append('  <form id="quick-add-form" class="quick-add-form">\n')
    parts.append('    <input type="text" id="quick-add-input" '
                  'placeholder="e.g. &quot;email professor tomorrow&quot; or &quot;remove the CHEM reading&quot;">\n')
    parts.append('    <button type="submit">Add</button>\n')
    parts.append("  </form>\n")
    parts.append('  <p id="quick-add-feedback" class="quick-add-feedback" hidden></p>\n')
    parts.append(
        '  <p class="quick-add-hint">AI-powered (uses your API key) — writes a clean, '
        "summarized title rather than reusing your exact wording, infers a due date "
        "from what you type, and can also remove or update an existing to-do by "
        "description (e.g. &quot;mark the reading done&quot;).</p>\n"
    )
    parts.append("</section>\n")

    parts.append('<section class="panel countdowns" aria-label="Countdowns">\n')
    parts.append("  <h2>Countdown</h2>\n")
    parts.append("  " + render_countdowns(config, today) + "\n")
    parts.append("</section>\n")

    parts.append('<section class="panel deadlines" aria-label="Deadlines">\n')
    parts.append(f'  <h2>Deadlines <span class="window-note">'
                  f'(next {DEADLINE_WINDOW_DAYS} days)</span></h2>\n')
    parts.append("  " + deadlines_html + "\n")
    parts.append("</section>\n")

    parts.append('<section class="panel grades" aria-label="Grades">\n')
    parts.append("  <h2>Grades</h2>\n")
    parts.append("  " + grades_html + "\n")
    parts.append('  <div class="refresh-row">\n')
    parts.append('    <button id="grades-refresh-btn" type="button">Refresh grades</button>\n')
    parts.append(f'    <span class="panel-timestamp">Last checked: {esc(format_timestamp_display(grades_generated))}</span>\n')
    parts.append("  </div>\n")
    parts.append("</section>\n")

    parts.append('<section class="panel feed-status" aria-label="Feed status">\n')
    parts.append("  <h2>Feed status</h2>\n")
    parts.append("  " + render_feed_status(events_payload, config) + "\n")
    parts.append('  <div class="refresh-row">\n')
    parts.append('    <button id="events-refresh-btn" type="button">Refresh events</button>\n')
    parts.append(f'    <span class="panel-timestamp">Last checked: {esc(format_timestamp_display(generated))}</span>\n')
    parts.append("  </div>\n")
    parts.append("</section>\n")

    parts.append('<section class="panel quick-links" aria-label="Quick links">\n')
    parts.append("  <h2>Quick links</h2>\n")
    parts.append("  " + render_quick_links(config) + "\n")
    parts.append("</section>\n")

    parts.append('<section class="panel unscheduled" aria-label="Unscheduled to-dos">\n')
    parts.append("  <h2>Unscheduled</h2>\n")
    parts.append('  <div id="unscheduled-todos"></div>\n')
    parts.append("</section>\n")

    parts.append('<footer class="meta-footer">\n')
    parts.append(f"  <p>Data last generated: {esc(format_timestamp_display(generated))}</p>\n")
    parts.append("</footer>\n")

    parts.append('<div id="save-indicator" class="save-indicator" hidden></div>\n')
    # Visible SW install/activate status -- reuses .offline-banner's styling.
    # Exists because registration below used to swallow every error
    # (`.catch(() => {})`), which is how the 2026-08-29 missing-icons bug
    # went unnoticed: install failed every time with zero visible symptom.
    # See BUILD.md, "Offline support (PWA)".
    parts.append('<div id="pwa-status-banner" class="offline-banner pwa-status-banner" hidden></div>\n')

    parts.append('<button id="chat-bubble" class="chat-bubble" type="button" '
                  'aria-label="Open assistant">AI</button>\n')
    parts.append('<div id="chat-panel" class="chat-panel" hidden>\n')
    parts.append('  <div class="chat-header">\n')
    parts.append('    <span>Assistant</span>\n')
    parts.append('    <button id="chat-close" type="button" aria-label="Close">&times;</button>\n')
    parts.append('  </div>\n')
    parts.append('  <div id="chat-messages" class="chat-messages"></div>\n')
    parts.append('  <form id="chat-form" class="chat-form">\n')
    parts.append('    <input type="text" id="chat-input" '
                  'placeholder="Ask anything, or e.g. &quot;add my CHEM lab to config&quot;" autocomplete="off">\n')
    parts.append('    <button type="submit">Send</button>\n')
    parts.append('  </form>\n')
    parts.append('</div>\n')

    parts.append('<script id="planner-data" type="application/json">')
    parts.append(json.dumps(client_data, ensure_ascii=False))
    parts.append("</script>\n")
    parts.append("<script>\n" + JS + "\n</script>\n")

    # Registration is a no-op over plain HTTP or any origin that isn't a
    # secure context -- browsers refuse to register a service worker there
    # at all -- but any OTHER failure (a shell URL 404ing during install,
    # e.g.) now surfaces on #pwa-status-banner instead of vanishing into
    # `.catch(() => {})`, since that swallowed the 2026-08-29 missing-icons
    # bug with no visible symptom. See BUILD.md, "Offline support (PWA)".
    parts.append(
        "<script>\n"
        'if ("serviceWorker" in navigator) {\n'
        '  window.addEventListener("load", () => {\n'
        '    const banner = document.getElementById("pwa-status-banner");\n'
        "    function showPwaStatus(text, isError) {\n"
        "      if (!banner) return;\n"
        "      banner.textContent = text;\n"
        "      banner.hidden = false;\n"
        "      if (!isError) setTimeout(() => { banner.hidden = true; }, 6000);\n"
        "    }\n"
        "    function watch(worker) {\n"
        "      if (!worker) return;\n"
        '      worker.addEventListener("statechange", () => {\n'
        '        if (worker.state === "activated") {\n'
        '          showPwaStatus("Offline mode ready on this phone.", false);\n'
        '        } else if (worker.state === "redundant") {\n'
        '          showPwaStatus("Offline setup failed -- reload this page once while online to retry.", true);\n'
        "        }\n"
        "      });\n"
        "    }\n"
        '    navigator.serviceWorker.register("/service-worker.js").then((reg) => {\n'
        "      watch(reg.installing || reg.waiting);\n"
        "      if (reg.active && !reg.installing && !reg.waiting) {\n"
        '        showPwaStatus("Offline mode ready on this phone.", false);\n'
        "      }\n"
        '      reg.addEventListener("updatefound", () => watch(reg.installing));\n'
        "    }).catch((err) => {\n"
        '      showPwaStatus("Offline mode couldn\'t start: " + err.message, true);\n'
        "    });\n"
        "  });\n"
        "}\n"
        "</script>\n"
    )

    parts.append("</body>\n</html>\n")
    return "".join(parts)


def main() -> int:
    config = load_config()
    events_payload = load_events()
    grades_payload = load_grades()
    today = date.today()

    html_doc = render_html(config, events_payload, grades_payload, today)

    OUT_FILE.parent.mkdir(exist_ok=True)
    OUT_FILE.write_text(html_doc, encoding="utf-8")
    print(f"Wrote {OUT_FILE}")

    owner_name = (config.get("owner") or {}).get("name") or "Your"
    MANIFEST_FILE.write_text(render_manifest(owner_name), encoding="utf-8")
    print(f"Wrote {MANIFEST_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
