# BUILD.md — UVM planner

**How to use this file:** open this folder in Claude Code and say
*"Read BUILD.md and build what's missing."*

Some of this repo is already written and tested. Your job is the part that
isn't. Read "What already exists" before writing anything.

---

## What this is

One HTML dashboard, served locally, showing a week at a time. Click any day to
see that day's classes, assignment due dates, and to-dos. To-dos are added and
edited in the page and stored in a real JSON file on disk.

Adapted from a working system built by someone else on Canvas + Gmail. Their
architecture is sound; their LMS and mail stack are not mine. The differences
are called out below and they are not cosmetic.

## The core design rule

**The page never invents data.** If a feed isn't connected, its panel says
"not connected" in plain language. No placeholder rows, no sample events, no
greyed-out fake data. A dashboard that quietly shows stale or fabricated rows
is worse than no dashboard — I will trust it, and it will be wrong.

`data/events.json` carries a `feeds` object with per-feed status. Render from
that, not from assumptions.

---

## What already exists — do not rewrite these

| File | Status |
|---|---|
| `scripts/fetch_brightspace.py` | Done. Pulls feeds, expands recurrence, tags classes, writes `data/events.json`. |
| `scripts/serve.py` | Done. Serves `dashboard/` and owns `data/todos.json`. |
| `config.yaml` | Structure done, values need filling in by me. |
| `.env.example` | Done. |
| `run.bat` | Done. |

If you find a bug in these, fix it and tell me what you changed. Don't
reimplement them.

### Why `recurring-ical-events` is a dependency

The source document this is adapted from insists on a hand-written ~60-line
iCal parser with no dependencies. **That advice does not apply here** and
following it will break this build.

It's correct for assignment due dates, which happen once. It is wrong for class
meeting times. A Tue/Thu lecture is stored in the ICS file *once*, with an
RRULE saying "repeat weekly until December." Expanding that is most of RFC
5545 — DST transitions, EXDATE exceptions, RDATE additions, COUNT vs UNTIL.
Hand-write it and assignments will work perfectly while every class appears
exactly once, in August.

Keep the library.

---

## Build these

### 1. `scripts/build_dashboard.py`

Reads `config.yaml` + `data/events.json` → writes `dashboard/index.html`.

Self-contained output: inline CSS and JS, no CDN, no npm, no build step. It
must work with no network.

`encoding="utf-8"` on **every** read and write. Windows defaults to cp1252 and
a single em-dash or checkmark raises `UnicodeEncodeError`. This passes silently
on macOS and fails on mine.

### 2. `dashboard/index.html` (generated — never hand-edit)

**Week strip.** Seven day cells, Monday-first. Each shows weekday, date, and a
count of events and open to-dos. Today is marked. Clicking a day selects it.
Prev/next week buttons, and a "today" button.

**Day detail**, for the selected day:
- *Schedule* — timed events in order, then all-day items. Show source and
  location. Course-tagged events get a colored chip using the workspace accent.
- *To-dos* — checkbox, title, notes, delete. An add form defaulting to the
  selected day.

**Deadlines panel.** Next 14 days of assignment due dates, sorted by urgency,
grouped by class tag. This is the single highest-value panel — build it first
and get it right before anything else.

**Countdown panel.** `milestones` from config, showing days remaining.

**Quick links bar.** Per workspace, from config. Cheapest win in the project;
five minutes of config, used daily.

**Status panel.** One row per feed: connected with a count, or the actual error
string. Plus the `generated` timestamp so I can see how stale the data is.

**Refresh button** → `GET /api/refresh`, then re-render.

### 3. Read-only vs editable

Events from feeds are **read-only in the UI**. Clicking one says "edit this in
Brightspace" or "edit this in Outlook." Two sources of truth means silent
drift. Only to-dos are editable here.

---

## To-do persistence — this is the requirement that shaped the architecture

To-dos live in `data/todos.json`. Not `localStorage`. A browser page cannot
write to disk, which is why `serve.py` exists.

- `GET /api/todos` → array
- `POST /api/todos` → whole array, atomic write

Client rules:
- Load from `/api/todos` on page load. **Never** seed from `localStorage`.
- After any change, POST the full array and show a save indicator.
- If a POST fails, say so visibly and do not silently drop the edit.

To-do shape:

```json
{
  "id": "a1b2c3d4",
  "title": "Read Kandel ch. 12",
  "due": "2026-08-06",
  "notes": "pages 240-270",
  "tag": "NSCI-1100",
  "done": false,
  "created": "2026-08-05T14:22:00-04:00",
  "remind_time": null,
  "location": null,
  "is_exam": false,
  "repeat_group": null,
  "calendar_event_id": "abc123xyz"
}
```

`repeat_group` (added 2026-08-23) is null on an ordinary to-do; on a
recurring one it's an id shared by every occurrence in that series — see
"Recurring to-dos," below.

`due: null` means unscheduled — show those in a sidebar, not on a day.

`remind_time` (24h `HH:MM`) overrides `config.yaml`'s default reminder time
for this to-do specifically. `location` is an optional destination — see
"Directions on reminders and schedule blocks," below. `is_exam` (added
2026-08-21) adds two extra Google Calendar popup reminders — 1 week and 2
weeks before `due` — on top of the normal day-of one, for any exam/midterm/
final regardless of class; see "Exam reminders," below. `calendar_event_id`
is set and cleared automatically by the Google Calendar reminders bridge
(below); nothing else should touch it.

You may use `localStorage` for **view state only**: selected week, collapsed
panels, hidden layers. Never for data I typed.

---

## Google Calendar reminders

Added 2026-08-18 (superseding an earlier email-to-Reminders-app bridge — see
below). A **dated** to-do (`due` is set) gets a Google Calendar event with a
popup reminder, created the moment the to-do is saved rather than on a
timer. Google's own servers deliver the phone notification, so this works
even when this PC is off, asleep, or `serve.py` isn't running — the thing
the old email bridge could never do, since a local Python process has no
way to wake itself up.

**Why it replaced the iPhone Reminders bridge.** That version emailed a
to-do to Gmail, relying on a phone-side iOS Shortcut to turn the email into
a Reminders-app item, and a local scheduler thread to decide *when* to
send. It worked, but only while `serve.py` happened to be running at the
right moment — no good for a phone-primary workflow. Google Calendar's own
infrastructure owns the "fire at the right time" job now, so there's no
local scheduler at all.

**How it works (`serve.py`).** `reconcile_todo_calendar_event(old, new)` is
the single place that creates, updates, or deletes a to-do's event —
called from `add_todo`, `update_todo`, `delete_todo`, and the whole-array
`/api/todos` POST handler (the manual UI's save path) alike, so every way
of editing a to-do stays in sync. A to-do wants an event only while it's
dated *and not done*; marking one done, or clearing its due date, deletes
the event — there's nothing left to be reminded about. The event's start
is the due date at `remind_time` (or `reminders.send_time` from
`config.yaml`, default `08:00`), with a popup override at 0 minutes so it
fires exactly then, not some default lead time. `calendar_event_id` on the
to-do is how it finds the same event again on a later edit — the reconcile
function skips the API call entirely when nothing calendar-relevant
changed, so routine saves don't needlessly touch Calendar.

**One subtlety that matters:** the whole-array POST handler never trusts
`calendar_event_id` from the client — the browser never learns it back, so
it would always look "new" and create a duplicate on every save otherwise.
The server always resolves it from its own previous copy of the to-do by
id before reconciling.

**Directions come along for free.** If a to-do's `location` is set (or it's
tagged with a class that has an `address` in `config.yaml` — see
"Directions" below), that becomes the Calendar event's `location` field.
Google Calendar (and Apple Calendar, if it's synced in) render a plain
address there as a tappable "get directions" map — no custom maps link
needed, unlike the old email version.

### One-time setup (Google Cloud Console + this repo)

Do this once. Nothing here touches your calendar data — it only produces
the three values `serve.py` needs to authenticate.

1. **Google Cloud Console** → [console.cloud.google.com](https://console.cloud.google.com)
   → create a new project (or pick an existing personal one) → its name
   doesn't matter.
2. **APIs & Services → Library** → search "Google Calendar API" → **Enable**.
3. **APIs & Services → OAuth consent screen** → User Type: **External**
   (Internal isn't available on a personal account) → fill in an app name
   (e.g. "UVM Planner") and your own email for the two required contacts →
   Save.
   - **Click "Publish App."** Newer Cloud Console projects don't surface a
     "Test users" list at all — just "Publish app" / "Make internal" — in
     which case Testing mode won't authorize you even against your own
     account, and it has a separate problem regardless: Google expires
     refresh tokens after 7 days for apps left in Testing, which would
     silently break reminders every week. Publishing avoids both. This is
     safe and doesn't need Google's review — `calendar.events` isn't one
     of the "restricted" scopes that requires it, only the standard
     "Google hasn't verified this app" warning, which you click through
     yourself (see step 6).
   - If your project *does* show a Test users list (older-style setup),
     adding your own address there works too and skips the warning screen
     entirely — either path is fine.
4. **APIs & Services → Credentials** → **Create Credentials** → **OAuth
   client ID** → Application type: **Desktop app** → name it anything →
   **Create**. A dialog shows a **Client ID** and **Client Secret** — copy
   both.
5. In `.env`, set:
   ```
   GOOGLE_CALENDAR_CLIENT_ID=<the client ID>
   GOOGLE_CALENDAR_CLIENT_SECRET=<the client secret>
   ```
6. Run the one-time auth script (from inside the `dashboard` folder, using
   the project's own venv — a bare `python` may point at a different
   install without the required packages):
   ```powershell
   cd C:\Users\smutl\dashboard
   .venv\Scripts\python.exe scripts\google_calendar_auth.py
   ```
   It opens a browser and asks you to sign in and consent. Expect a
   **"Google hasn't verified this app"** warning screen (normal for your
   own published-but-unreviewed app) — click **Advanced**, then **Go to
   [your app name] (unsafe)** to continue. "Unsafe" just means Google
   hasn't manually reviewed it; it's still your own app and account.
   A **hard block** reading "hasn't completed the Google verification
   process," with no way through, means the app is still in Testing and
   isn't authorizing your account — go back to step 3 and publish it.
   After consenting, the script prints a `GOOGLE_CALENDAR_REFRESH_TOKEN`
   line. Paste that into `.env` too.
7. Restart `serve.py` (or the always-on process running it — see "Running
   always-on," below). The startup log confirms whether Calendar reminders
   are active.

If you ever need to redo this (revoked access, lost refresh token): go to
[myaccount.google.com/permissions](https://myaccount.google.com/permissions),
remove the app's access, and re-run step 6.

### Running always-on (Windows Startup)

The dashboard server itself should still run continuously so the page and
chat bubble are available — Calendar reminders no longer depend on this,
but everything else in the app does. This repo's actual mechanism is
`scripts/run_hidden.vbs` (launched via a shortcut in the Windows Startup
folder, `shell:startup`), which starts `pythonw.exe scripts\serve.py` with
no console window. `run.bat` is the interactive alternative — it shows the
window and opens the browser, for when you're at the keyboard.

## Offline support (PWA)

Added 2026-08-29, from Charles's direct request: the phone needs the page
even with zero signal (walking to class), not just when Tailscale can reach
this PC. Two separate problems, two separate mechanisms:

**1. The page itself has to open with no network at all.** This is a PWA
now — `dashboard/manifest.webmanifest` (generated by `build_dashboard.py`,
alongside `index.html`, so it can carry the real owner name) plus
`dashboard/service-worker.js` and `dashboard/icons/icon-192.png` /
`icon-512.png` (static, hand-authored — they don't depend on
config.yaml/events.json, so nothing regenerates them; simple green "P"
mark, generated once via .NET `System.Drawing` from PowerShell since
Pillow isn't a project dependency — replace with real artwork anytime,
same filenames). **Bug found and fixed 2026-08-29:** the icon files were
never actually created despite being referenced by manifest and service
worker — `service-worker.js`'s `install` handler does
`caches.open(...).then(cache => cache.addAll(SHELL_URLS))`, and
`cache.addAll` rejects the *entire* call if even one URL 404s. With the
icons missing, install always failed, so the service worker never
activated and nothing was ever cached — meaning offline (PC/Tailscale
unreachable) had zero fallback and the phone just got the browser's own
blank/failed-load screen. Registration also swallows errors
(`.catch(() => {})`), so this failed silently with no visible symptom
while online. If `SHELL_URLS` in `service-worker.js` ever gains another
URL, verify that file actually exists on disk before assuming the PWA
works — `addAll`'s all-or-nothing failure mode makes this an easy silent
break.

**Second bug, found and fixed the same day after the icons fix:** even
with the service worker correctly installed and active (confirmed via the
`#pwa-status-banner` diagnostic below), a cold launch of the Home Screen
app while genuinely offline still failed intermittently — worked if
reopened within about a second of force-quitting, failed if reopened any
later. Root cause: a race between iOS's own "can't connect" interstitial
and this service worker's fetch handler on a fresh top-level navigation.
The plain network-first `fetch()` had no time bound, so however long the
*real* connection attempt took to fail (Tailscale DERP-relay negotiation,
DNS state, etc. — inherently variable) was however long iOS gave our JS to
respond before showing its own native offline page instead, which bypasses
this fetch handler entirely. Fixed by wrapping the network attempt in an
`AbortController` with a fixed `NETWORK_TIMEOUT_MS` (3000ms) in
`service-worker.js`, so the fallback to cache always fires within 3
seconds regardless of how slow the real failure would have been —
confirmed working on a real cold launch, PC closed, 2026-08-29. If offline
loads ever regress again, check this race first before assuming the cache
itself is the problem — the cache can be perfectly correct and still lose
this race.

Also added while debugging this: `#pwa-status-banner` (fixed top-center,
`build_dashboard.py`'s `render_manifest`-adjacent registration script) now
shows "Offline mode ready on this phone" briefly on successful activation,
or a persistent error banner with the actual message on failure — replacing
the old `.catch(() => {})` that silently swallowed exactly the kind of
failure that caused the icons bug in the first place. Useful for
diagnosing this class of problem on a phone with no attached Mac for
remote Web Inspector.

The service worker caches the page shell network-first
(always tries a fresh fetch so an online visit gets today's actually-baked-in
events/deadlines; only falls back to the cached copy when that fetch fails)
and deliberately never touches `/api/*` — to-do data has its own separate
online/offline handling (below), and a cached API response in the service
worker would silently race that logic.

**Service workers require HTTPS.** Browsers refuse to register one on plain
HTTP for anything other than `localhost` — which the phone, as a different
device, never is. Phone access therefore moved off
`http://<tailscale-ip>:8787` (plain HTTP, worked but can't run a service
worker) onto Tailscale's own HTTPS feature:

1. **One-time, in the Tailscale admin console:**
   [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns) →
   enable **HTTPS Certificates**. Nothing to configure beyond the toggle.
2. **One-time, on this PC** (done 2026-08-29 — already running):
   ```powershell
   tailscale serve --bg http://127.0.0.1:8787
   ```
   (Older Tailscale versions wanted `tailscale serve --bg https / http://...`
   — that syntax is gone as of 1.102; the CLI now infers `https` on `/`.)
   This makes Tailscale itself terminate HTTPS (its own managed,
   auto-renewing cert — no cert files for `serve.py` to load, no code
   changes there) and reverse-proxy to the plain-HTTP server already
   running locally. `tailscale serve status` shows the current config;
   `tailscale serve --https=443 off` removes it. This is tailnet-private
   `serve`, not public `funnel` — only devices on your own Tailscale
   account can reach it, same boundary as before. Persists across reboots
   on its own (it's Tailscale's own background state, not tied to
   `serve.py` or any particular terminal session).
3. **New phone URL:** `https://<device>.<tailnet>.ts.net` (MagicDNS name,
   `tailscale status` shows it — no port needed, defaults to 443) instead of
   the IP:port. Re-bookmark / re-"Add to Home Screen" once switched over.

**2. Once the page is open, to-do data itself needs to survive `/api/todos`
being unreachable** (signal but no path to the PC — Tailscale hiccup, PC
asleep — or genuinely zero signal). Scoped to exactly what was asked:
**viewing and checking off to-dos work fully offline; adding, editing,
deleting, and location edits require a live connection** and are visibly
greyed out (with a "Reconnect to..." tooltip) rather than attempted, since
there's no merge logic written for those yet and guessing at one risks
silently losing an edit — worse than asking to wait.

This is the one deliberate, LABELED exception to the to-do persistence
rule above ("**Never** seed from `localStorage`"). That rule exists to stop
the page from ever passing off stale data as if it were live; this does the
opposite on purpose: `loadTodos()` in the generated JS falls back to a
localStorage cache (`offlineTodosCache_v1`) *only* after a real fetch to
`/api/todos` has just failed, and every time cached data is shown instead of
a fresh fetch, a persistent banner (`showOfflineBanner`) says so — "Offline —
showing to-dos as of 8:42 AM" — never silently.

Checking a box while offline records the intent immediately to
`localStorage` (`offlinePendingToggles_v1`, an `{id: done}` map) *before*
attempting the network call — so a killed/reloaded tab (phone locks, app
backgrounded and evicted) doesn't lose it — then, once `/api/todos` is
reachable again (`loadTodos()` on the next successful load, a
`window.addEventListener("online", ...)` firing, or a 30-second retry timer
while known offline), `flushPendingToggles()` fetches the CURRENT server
copy and applies only the pending toggles onto it, rather than blindly
POSTing the stale offline array — so anything changed elsewhere (the PC
itself) while the phone was offline isn't clobbered.

`isOffline` (generated JS) reflects whether the last real `/api/todos`
attempt actually succeeded — not `navigator.onLine`, which only knows the
network interface is up, not whether the PC/Tailscale is actually reachable.
That distinction is what makes the 30-second retry matter: the phone can
have full signal the whole time while only the PC is unreachable, a case
the browser's own `online` event never fires for.

## Exam reminders

Added 2026-08-21. A to-do with `is_exam: true` gets its Calendar event built
with three popup overrides instead of one — 0 minutes (the normal day-of
reminder), 7 days, and 14 days before `due` — all on the *same* Calendar
event, not three separate ones. `_event_body()` in `serve.py` builds the
`overrides` list conditionally; `reconcile_todo_calendar_event`'s change-
detection includes `is_exam` in its watched fields, so flipping the flag on
an existing to-do updates its event's reminders on the next save. This
applies to any class, not just one — the AI (`add_todo`/`update_todo`, chat
bubble and quick-add alike) is told to set `is_exam=True` whenever a to-do
is clearly an exam/midterm/final, and the manual add-todo form has its own
"Exam" checkbox for the same thing.

**Only to-dos get Calendar reminders — feed events (e.g. Brightspace exam
dates) don't**, per the read-only-events rule above. So an exam date that's
only known from a live feed (like Chem's Exam 1/2/3/Final, which show up as
real Brightspace calendar events) needs a matching to-do created too, with
`is_exam: true`, before it'll actually remind you — the feed event alone is
display-only. When Charles hands over a syllabus with concrete exam dates,
add both: nothing changes about the read-only feed display, but a mirrored
to-do is what makes the phone buzz.

## Class contact info & synthesized schedule blocks

Added 2026-08-21. Two more optional per-class fields in `config.yaml`,
alongside `address`/`grades_url` — same "blank by default, never guessed"
rule:

```yaml
instructor:
  name: "Dr. Erik Ruggles"
  email: "Erik.Ruggles@uvm.edu"
  office: "Innovation E333"
  office_hours: "Mon/Wed/Fri 10:00am-12:30pm, Tue/Thu 3:00-4:00pm"
```

`build_dashboard.py` passes this through as `tagInstructors` in the
generated JS's `data` object. Clicking a schedule item in day detail (which
already showed a "read-only, edit this in Brightspace" note) now also shows
the instructor's name, a `mailto:` link, office, and office hours
underneath, when set for that class's tag — see `renderScheduleItem` and
the `.class-info` CSS block.

**Schedule blocks are now synthesized from `meets`/`time`/`room` too**, not
only from live feed events. Previously (see the 2026-08-19 note below) a
class only appeared on a given day if Brightspace had actually published a
recurring meeting event for it — which, this early in the semester, most
classes hadn't. `classMeetingsForDay(iso)` in the generated JS builds a
pseudo-event straight from each class's config for any weekday in its
`meets` list, using `parseTimeRange()` to turn `"1:10 pm - 2:00 pm"` into
24h start/end for correct chronological sorting alongside real events.
These are marked `isConfigSchedule: true` so `renderScheduleItem` shows
"Regular meeting time, from config.yaml" instead of a feed name, and reuse
the same directions-pill/instructor-info click behavior as real events. Add
`"Class schedule": school` (or whichever workspace) to `feed_workspaces` so
these get a sensible accent color via `accentFor()`.

**Known, accepted overlap:** once Brightspace does start publishing a
class's actual recurring meeting as a live feed event, that real event and
the synthesized one will both appear on the same day — a cosmetic double
listing, not a fabrication (both reflect something true), and easy to
collapse later (e.g. suppress the synthesized block for a tag once a real
timed feed event with that tag exists that day) once it's actually
observed happening.

## Get help (per to-do advice)

Each non-done to-do has a "Get help" button. It doesn't add a new backend —
it opens the existing chat bubble, fills in a canned prompt describing that
to-do, and submits it through the same `/api/chat` path a typed message
would use. `build_system_prompt()` in `serve.py` tells the model what to do
with that: if the to-do involves contacting or emailing someone, use
`web_search` for public info about them first, then propose a plain-text
email draft for the user to copy — never claim to send or save one, since
there's no tool for that on purpose (see "just show text" — no Gmail
integration here). If there isn't enough to draft something real, ask
clarifying questions instead of inventing details about a real person.

## Directions on reminders and schedule blocks

Added 2026-08-18. Two sources of "where to go," each surfaced two ways: on
the dashboard page itself (a "📍 Directions" pill, an Apple Maps deep link —
`https://maps.apple.com/?daddr=<address>`, no forced transport mode, so
Maps offers walk/drive/transit like a normal search), and on the phone
notification (the plain address in the Google Calendar event's `location`
field — see "Google Calendar reminders," above — which Calendar apps
already render as a tappable map natively, no custom link needed there).

1. **Ad-hoc places** — a to-do's own optional `location` field. Set via the
   manual add-todo form, or by the AI (chat bubble / quick add) when the
   user's prompt names a destination for that to-do — never invented, never
   inferred. Two kinds, both just free text in the same field:
   - a **specific place** ("Pacific Pipe", "Monterey, California") — routes
     there directly, same as always.
   - a **type of place** ("a hardware store", "a pharmacy") — added
     2026-08-20, so "pick up a hammer from a hardware store" doesn't force
     naming one. The category text is passed through verbatim, unchanged,
     to both the on-page Apple Maps link and the Calendar event's
     `location`; neither `daddr=<address>` nor a plain-text Calendar
     location requires a literal geocoded address — a business-type phrase
     resolves through the same search Maps' own search box would do,
     centered on wherever the phone actually is *when the link is tapped*,
     which is what makes this "closest," not a fixed store picked in
     advance (and correctly reflects that "closest" can only be known at
     tap time, not when the to-do was created). The AI is told explicitly
     not to ask "which one?" for a category — that ambiguity is the point.
     **Not yet verified against a real iPhone** — worth a live test
     (`daddr=hardware+store` from the dashboard) to confirm Maps resolves
     it the way this assumes rather than erroring on an unparseable address.

2. **Class locations** — an optional `address` per class in `config.yaml`
   (blank by default; the page never guesses one — fill in real, mapable
   addresses yourself). Any event or to-do tagged with that class picks it
   up automatically: a "📍 Directions" pill on the class's schedule blocks in
   day detail, and — for a class-tagged to-do with no `location` of its own —
   the same fallback on its Calendar event. Set once per class, not typed
   per to-do. This is separate from a calendar *event's* own `location`
   field as read from the Brightspace/Outlook feed (whatever free text the
   source put there, shown as plain text next to the event title) — that's
   often not a real geocodable address, so it's never used to build a
   directions link.

**Timezone is whatever this PC's Windows clock says**, deliberately — `_event_start()` uses `.astimezone()` on a naive local datetime, so a to-do's `remind_time` fires at that wall-clock moment on *this machine*, not a fixed zone. This is Charles's explicit choice: he's Pacific until 2026-08-31, then Eastern for the UVM semester (with brief trips back for breaks), and wants reminders to track wherever the computer actually is rather than being pinned to one zone. If Windows' "Set time zone automatically" doesn't catch a travel change on its own, it needs a manual flip in Settings — otherwise every reminder that day/week is off by the zone difference.

`_class_address()` in `serve.py` resolves a class's configured address
server-side (used when building a Google Calendar event's `location`);
`appleMapsUrl()` / `makeDirectionsLink()` in `build_dashboard.py`'s
generated JS build the on-page "📍 Directions" pills. Keep both in sync if
the on-page link format ever changes.

## Grades panel

Added 2026-08-20, and **scaffolding only** as of that date — the plumbing
exists end-to-end, but nothing has a real grade to show yet, on purpose:
Fall 2026 classes hadn't started (first day 2026-08-31) and Brightspace had
no grades posted when this was built.

**Why scraping, not an API.** UVM doesn't publish a grades feed. UVM IT's
own guidance for this (given to Charles directly) is to read the
Brightspace "My Grades" page the way a logged-in browser does — a local
script using your real, already-authenticated NetID/Duo session, never a
stored password, never automating the Duo step itself.

**How it works.** `scripts/fetch_grades.py` drives Chrome via Selenium
against a *dedicated* profile at `.grades_chrome_profile/` (gitignored —
never your daily Chrome profile, since Selenium locks whatever profile it
opens). One-time, and again whenever a run reports `login_required`:

```powershell
.venv\Scripts\python.exe scripts\fetch_grades.py --login
```

Opens a real, visible Chrome window; sign in by hand (NetID + Duo) and the
script detects completion on its own by polling the URL — no terminal
keypress needed, so this also works launched from a non-interactive shell.
Every other run is headless and reads `grades_url` per class from
`config.yaml` — blank by default, same "never guessed" rule as `address`.
Writes `data/grades.json`, one entry per class tag, status one of
`not_configured` / `login_required` / `parser_not_ready` / `connected` /
`error`. `build_dashboard.py`'s Grades panel renders straight from that
file, same as the deadlines and quick-links panels — a "Refresh grades"
button hits `serve.py`'s `/api/refresh-grades`, which reruns the scraper
headless then rebuilds the dashboard.

**Session handling was rebuilt 2026-08-29 after three separate bugs, found
in order while actually turning a `grades_url` on for the first time
(CHEM-1400-L51):**

1. `looks_like_login_redirect()` only checked the URL's *hostname* against
   a marker list (`login`, `sso`, `duosecurity`, ...). Two real login pages
   slipped past it from opposite directions: Brightspace's own expired-
   session redirect (`brightspace.uvm.edu/d2l/login` — same host as
   everything else, marker's in the path) and UVM's Shibboleth IdP
   (`idp.uvm.edu/idp/profile/SAML2/.../SSO` — marker's in the path there
   too, and the host itself doesn't contain any marker substring). Fixed by
   checking the full URL (host + path + query) at once — see
   `LOGIN_URL_MARKERS`.
2. Even fixed, a *blacklist* of login-page keywords is the wrong shape for
   a multi-hop SSO redirect (idp.uvm.edu → a Duo callback on
   duosecurity.com → back to brightspace.uvm.edu) — it passes through
   transitional URLs that don't match any keyword without being real
   content either, and `do_login()`'s polling loop kept declaring false
   success on one of those, before the redirect chain had actually
   finished. Fixed by switching to `looks_signed_in()` — a *whitelist*:
   only true once the host is actually `brightspace.uvm.edu` again, on a
   non-login page. A 2026-08-30 note in case this ever needs revisiting:
   this is specific to how UVM's Brightspace happens to redirect; a
   different Brightspace tenant could look different.
3. Even with (1) and (2) fixed, sign-in kept "succeeding" on screen but
   `data/grades.json` still reported `login_required` on the very next
   headless run. Root cause, confirmed by inspecting a live, successfully
   authenticated session directly: D2L's actual login state is 4 cookies
   (`d2lSessionVal`, `d2lSecureSessionVal`, two `d2lSameSiteCanary*`
   cookies) on `brightspace.uvm.edu`, and **none of them have an
   Expires/Max-Age** — true session cookies, which Chrome never writes to
   its on-disk profile at all, by design (that's what "session cookie"
   means). No amount of settle-delay before `driver.quit()` was ever going
   to fix that — the values were always going to be discarded the moment
   the browser process closed. The actual fix: `save_session_cookies()`
   captures those 4 values itself (via `driver.get_cookies()`, which reads
   the live cookie jar, session cookies included) the moment `do_login()`
   confirms success, into `.grades_chrome_profile/session_cookies.json`
   (also gitignored — same sensitivity as a saved password). Every headless
   run calls `inject_session_cookies()` first, which re-injects those exact
   values via `driver.add_cookie()` before visiting any `grades_url`.
   Verified working end-to-end: a completely fresh headless process, given
   only those 4 injected values and nothing captured from Chrome's own
   profile, loaded a real grades page.

**The real limitation this doesn't remove:** those cookie values are only
good for as long as Brightspace's own server-side session stays alive —
shorter than, and independent of, Duo's "remember this device" window.
Expect `--login` to be needed somewhat more often than "only when Duo's
trust lapses" — a `login_required` status already surfaces this honestly
either way, same as before.

**The one piece still not built: actual parsing.** `parse_grade_page()` in
`fetch_grades.py` is a stub that returns `None` — the core design rule says
never guess a selector and quietly show the wrong number. Login now works
end-to-end (confirmed 2026-08-29 against CHEM-1400-L51 — the debug HTML has
real, authenticated Brightspace navigation in it, not a login page), but
that class's actual grade content wasn't easy to find in the raw page
source on first pass — likely rendered by Brightspace's newer web-component
UI (shadow DOM), which `driver.page_source` doesn't capture the contents
of, and Fall 2026 classes hadn't started yet (first day 2026-08-31) so
there may be nothing graded to find regardless. Needs a closer look at
`data/grades_debug_CHEM-1400-L51.html` (or a fresh `--inspect` once real
grades exist) before finishing this for real.

**Next steps, in order:** (1) ~~set one class's `grades_url`~~ done for
CHEM-1400-L51; set the rest as their grades pages exist, (2) ~~run
`--login` once~~ done, working, (3) once real grades are posted, run
`--inspect CHEM-1400-L51` again and dig into the shadow-DOM structure (or
look for a JSON API endpoint the page itself calls — often more robust than
scraping rendered markup) to finish `parse_grade_page()` against real
markup.

## Workload heatmap

Added 2026-08-23. Each day cell on the week strip and month grid gets a
`data-heat` attribute (0–3: none/low/medium/high) from its total item count
— events plus open to-dos due that day. Rendered as a bottom accent stripe
via `box-shadow` (`--heat` custom property, dark-mode variant included)
rather than a full background tint, deliberately, so it layers over
`.today`/`.selected` instead of fighting them for the `background` property
— see the CSS comment above the `[data-heat]` rules.

Two copies of the same tiering exist by necessity: `heat_level()` in
`build_dashboard.py` renders the server-side first paint from event counts
alone (to-dos live in `data/todos.json`, fetched client-side, not known at
build time); `heatLevel()` in the generated JS recomputes from events +
to-dos the moment `loadTodos()` resolves. Keep the two thresholds in sync if
they ever change.

## Recurring to-dos

Added 2026-08-23. A to-do can repeat daily/weekly/biweekly/monthly through
an inclusive end date. Per this app's existing philosophy (see "Exam
reminders" and the class-schedule-block synthesis above) a recurring to-do
is materialized as several real, independent to-do rows sharing one
`repeat_group` id — **not** a virtual repeating rule computed at render
time. Each occurrence gets its own id and its own Google Calendar event
through the normal `reconcile_todo_calendar_event` path, so completing or
deleting one occurrence never touches the others.

- **Manual add-todo form**: a "Repeat" dropdown + an until-date input
  (hidden until a frequency is picked). On submit, the JS calls
  `expandRecurrence()` to build the occurrence list client-side, then one
  `addTodos()` batch call — a single `/api/todos` POST for the whole series,
  not one per occurrence.
- **AI (chat bubble + quick-add)**: `add_todo`'s `repeat_freq` /
  `repeat_until` params do the same thing server-side via
  `expand_recurrence()` in `serve.py`. Both system prompts tell the model to
  infer `repeat_until` from context (a semester end, "until finals") rather
  than always asking.
- **Deleting a series**: a "Delete series" button appears next to the
  normal per-occurrence Delete on any to-do with a `repeat_group`, and
  removes every occurrence at once (client-side: filters `todos` by
  `repeat_group`, one `saveTodos()` call — the existing whole-array POST
  handler already deletes each removed to-do's Calendar event, so no new
  server logic was needed there). The AI has the equivalent `delete_series`
  tool.
- Capped at 104 occurrences (`MAX_RECURRENCE_OCCURRENCES`, both copies) as a
  safety limit against a bad end date, not a real-world constraint.

**Monthly needs care.** `_add_months()` (serve.py) / `addMonthsJS()`
(generated JS) always compute an occurrence from the *original* due date
plus a step count `k`, never by chaining off the previous occurrence.
Chaining would drift a 31st-of-the-month to-do down to the 28th permanently
the first time it crossed February (Jan 31 → Feb 28 → Mar 28 → ... instead
of the correct Mar 31) — caught by a unit test against `expand_recurrence`
during this build, before it shipped.

## Class quick links for assignments — scoped down from what was asked

Added 2026-08-23. The original ask was a link straight to a specific
assignment from its deadline entry. Checked against the real Brightspace,
Outlook, and Personal feeds directly (raw iCal fields, not just what
`--inspect` prints) — no VEVENT carries a `URL` property anywhere.
Brightspace's calendar export just doesn't expose per-assignment links, so
that specific feature isn't buildable from real data without guessing a URL
scheme, which the core design rule forbids.

What shipped instead: an optional `brightspace_url` per class in
`config.yaml` (blank by default, same pattern as `address`/`grades_url`) —
that class's Brightspace course page. Shows as an "Open in Brightspace"
link on the class's deadline group heading and, if the schedule block is
clicked, its class-info popup (alongside instructor info). Gets you to the
right course in one tap; not the specific assignment.

## Countdown panel, feed status panel, and a real "Refresh events" button

Added 2026-08-23. All three were in the ORIGINAL BUILD.md spec ("Build
these," item 2) but never actually got built past step 4 of "Order of
work" — the data for all three already existed and was correct, this was
purely a `build_dashboard.py` rendering gap, caught in a later session
while reviewing the codebase for new feature ideas.

- **Countdown panel** (`render_countdowns`): `config.yaml`'s `milestones`
  list, sorted by date, each showing days remaining. Shows every configured
  milestone, past ones included — dimmed via `.countdown-passed` rather
  than dropped, since it's real config data.
- **Feed status panel** (`render_feed_status`): one row per feed from
  `events.json`'s `feeds` object (already written by
  `fetch_brightspace.py` every run) — connected-with-a-count, or the actual
  error string. Each row's accent color reuses `accent_for()` /
  `workspace_for_source()`, the same feed→workspace mapping the deadlines
  panel already uses, so a feed's row matches its own events' color.
- **"Refresh events" button**: previously only grades had a working
  refresh button. `GET /api/refresh` existed but only re-wrote
  `events.json` — it never rebuilt `dashboard/index.html`, and since a
  browser's event data is baked into the HTML at build time (not fetched
  live via `/api/events`, which exists but nothing calls), a plain reload
  after hitting that endpoint wouldn't have shown anything new anyway.
  `refresh_events()` in `serve.py` now rebuilds the dashboard after
  fetching, same shape as `refresh_grades()`; the button follows the exact
  same disable/reload pattern as "Refresh grades."

**Auto-select today on page load.** The day detail panel used to start on
"Select a day above" until you clicked one. `selectDay(isoDate(new
Date()))` now runs right after the initial `refreshView()` (so today's
cell already exists in the DOM to mark selected) and before `loadTodos()`;
`loadTodos()` re-renders day detail a second time once real to-dos arrive,
since the first render happens before that fetch resolves.

## Same-class aliasing

Added 2026-08-23, from Charles's direct correction: CHEM-1400 (the MWF
lecture) and CHEM-1400-EVE ("Chem Lecture," the Monday evening session) are
the same Brightspace enrollment and the same grade — CHEM-1400-EVE only
exists as a separate `classes:` entry because this app has no way to give
one class two `meets`/`time`/`room` blocks, so a second entry is how a
second weekly meeting time gets its own synthesized schedule block.
CHEM-1400-L51 (the lab) is confirmed genuinely separate — its own
Brightspace course — so it does NOT get this treatment.

`same_class_as: CHEM-1400` on CHEM-1400-EVE's config.yaml entry marks that,
with three effects:

- **`fetch_grades.py`** skips scraping any class with `same_class_as` set
  entirely — no wasted Selenium hit against the same URL twice. Its
  `grades.json` entry gets `status: "same_as"` with the aliased tag, not
  `"not_configured"` (which would wrongly imply nothing's set up).
- **The Grades panel** (`render_grades`) shows no row for it — one row per
  real course, not per config entry.
- **The Brightspace link** (`tag_brightspace`, built in `render_html`)
  inherits from the aliased class when the aliased entry hasn't set its own
  `brightspace_url` — so CHEM-1400-EVE's own schedule block still gets a
  working "Open in Brightspace" link once CHEM-1400's is set, without
  needing the same URL typed twice.

Deadlines and schedule blocks are untouched by this — CHEM-1400-EVE keeps
its own synthesized Monday-evening schedule block (that's real, distinct
information: a different meeting time), and in practice can never receive
its own live-feed-tagged deadline anyway, since `tag_for()` in
`fetch_brightspace.py` matches classes in list order and CHEM-1400 (listed
first, with an overlapping `match` string) always wins.

## 12-hour clock for display

Added 2026-08-23, per Charles: "all times in general to standard clock,
not military time." Every place a time reaches a person now goes through a
display-only formatter — schedule block times (`formatTime12h` in the
generated JS), a to-do's remind-time chip, `add_todo`'s confirmation text
and the `serve.py` startup log (`_format_12h`), and the "Data last
generated"/"Last checked" timestamps (`format_timestamp_display`, which
also drops the raw ISO string down to "Aug 23, 2:00 PM"). The stored/API
format is untouched everywhere — `remind_time` on disk, the Calendar API
body, the `<input type="time">` value, and every AI tool parameter all stay
24h "HH:MM", exactly as before; only display text changed. Two copies of
the core HH:MM→12h conversion exist by necessity (Python renders some
things server-side, JS renders others client-side) — keep them in sync if
either ever changes.

## Class tagging — verify before you build on it

`config.yaml` maps a course code to match strings. The fetcher searches both
`SUMMARY` and `CATEGORIES`.

The source document says the course name lives in `CATEGORIES`. **That is
Canvas behavior and is unverified for Brightspace**, which often folds it into
`SUMMARY` instead.

Before wiring anything to tags, have me run:

```
python scripts/fetch_brightspace.py --inspect
```

and show you the output. Fix the `match` values in `config.yaml` to whatever
the feed actually contains. Do not guess.

---

## Gotchas inherited from the source document

These cost someone else real time. They are worth taking literally.

1. **`\2713` inside a Python string is an OCTAL ESCAPE.** `content:"\2713"` in
   a CSS block built in Python becomes garbage before CSS ever sees it. Use the
   literal `✓` character or a raw string.
2. **CSS custom properties resolve where declared, not where used.** Computing
   `--tone: var(--tone-raw)` at `:root` fails when `--tone-raw` is set further
   down the tree. Compute derived tokens on the section selector. Dark mode:
   `color-mix(in srgb, var(--tone-raw) 60%, white)`.
3. **`hidden` loses to any author `display` rule.** Add `[hidden]{display:none}`.
4. **`display:contents` breaks grid auto-placement.** If a wrapper uses it,
   place every child explicitly with `grid-area`.
5. **`encoding="utf-8"` on every file operation.** Said twice on purpose.

**Screenshot the rendered page in light and dark mode before telling me it
works.** Encoding and cascade bugs are invisible in source — that's how #1 and
#2 were found.

---

## Explicitly out of scope

Cut from the source document, do not build:

- The entire Gmail filter system. My school mail is Outlook; there's no
  equivalent, and the M365 connector is read-only anyway.
- Any inbox analysis. That was their inbox.
- Supabase / external database sync.
- GroupMe.
- A `work` workspace.

The school mail panel is a **links bar only**. UVM IT blocks the OAuth consent
needed for the M365 connector. That's policy, not a bug — render it as "not
connected" and move on.

---

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

copy .env.example .env
# then paste feed URLs into .env

python scripts\fetch_brightspace.py --inspect   # look at real data first
python scripts\fetch_brightspace.py
python scripts\build_dashboard.py
python scripts\serve.py
```

If PowerShell blocks activation:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

Python 3.11+.

---

## Order of work

Ask me before starting. Then:

1. Confirm the fetcher runs against my real feed and show me `--inspect` output.
2. Fix `config.yaml` match strings based on what we actually see.
3. Build the deadlines panel and the week strip. Stop. Show me.
4. Add day detail, to-dos, and the save round-trip. Verify a to-do survives
   killing the server and restarting.
5. Everything else — countdowns, links, status panel, styling.

Don't build all five and hand me a wall of code. I want to catch a wrong
assumption at step 2, not step 5.
