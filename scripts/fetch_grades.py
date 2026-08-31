"""
fetch_grades.py — scrape current grades from Brightspace's "My Grades" pages
using a persistent, dedicated Chrome profile, and write data/grades.json.

SCAFFOLDING NOTICE — see BUILD.md, "Grades panel." parse_grade_page() below
is a stub. Nobody has looked at UVM's real Brightspace grades HTML yet, and
this project's rule is "never guess" — the same rule that made
`fetch_brightspace.py --inspect` mandatory before wiring up class tagging
(see BUILD.md, "Class tagging — verify before you build on it"). This
script has the equivalent:

    python scripts/fetch_grades.py --inspect PSYS-2100

Run that once a class actually has grades posted, then bring the saved
data/grades_debug_PSYS-2100.html into a Claude Code session to finish
parse_grade_page(). Until then every configured class reports status
"parser_not_ready" — connected, page fetched, nothing invented.

Why Selenium with a browser at all, not requests+ICS like fetch_brightspace.py:
grades aren't in any feed UVM publishes, so per UVM IT's own guidance this
reads the page the way a logged-in browser would — using your real,
already-authenticated Brightspace session (NetID + Duo), never a stored
password.

Why a SEPARATE Chrome profile, not your everyday one: Selenium locks
whatever profile directory it launches against for as long as it's open, so
pointing it at your normal Chrome profile would mean fully closing Chrome
before every run. A dedicated profile (this script's own, gitignored) means
this never touches your daily browsing.

Why the session is saved to its OWN file, not just left in that Chrome
profile: found 2026-08-29, the hard way (see SESSION_COOKIE_NAMES below for
the full story) -- D2L's actual login cookies are true session cookies
(no Expires/Max-Age), which Chrome never writes to its on-disk profile at
all, by design. .grades_chrome_profile/session_cookies.json is this
script's own capture of those cookies' live values (via
driver.get_cookies() right after a successful --login), re-injected into
each new headless browser via driver.add_cookie() before it visits any
grades_url. Also gitignored -- same sensitivity as a saved password, even
though it's technically "just cookies."

One-time, and again whenever a run reports "login_required" -- which will
happen somewhat more often than you'd expect from Duo's own "remember this
device" window, since what actually governs it now is Brightspace's OWN
server-side session lifetime (shorter, and not under this script's
control) rather than Duo's device trust:

    python scripts/fetch_grades.py --login

Opens a real, visible Chrome window on the dedicated profile and sends it to
Brightspace. Sign in by hand (NetID + Duo) in that window — the script
polls the URL and returns on its own once you're genuinely back on
brightspace.uvm.edu (not just off a page matching a login/SSO keyword —
see looks_signed_in), so it needs no keyboard input in whatever terminal
launched it (this also means it works when launched from a non-interactive
shell, not just a terminal you're sitting at). Nothing is scraped during
--login — it only captures the session cookies for later runs.

Normal use (headless, one visit per class with a grades_url set in
config.yaml — this is also what the dashboard's "Refresh grades" button
runs server-side via serve.py):

    python scripts/fetch_grades.py

encoding="utf-8" on every read/write — see BUILD.md's gotcha list.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options

LOCAL_TZ = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_FILE = ROOT / "config.yaml"
PROFILE_DIR = ROOT / ".grades_chrome_profile"
# Same directory as the Chrome profile (already gitignored, already
# documented as "holds nothing but a Brightspace session cookie") -- see
# the big comment above SESSION_COOKIE_NAMES for why this file, not
# Chrome's own on-disk cookie store, is what actually carries the session
# between processes now.
SESSION_COOKIES_FILE = PROFILE_DIR / "session_cookies.json"

BRIGHTSPACE_HOME = "https://brightspace.uvm.edu"

# D2L's own session cookies (confirmed 2026-08-29 by inspecting a real,
# live, successfully-authenticated session): 4 cookies on brightspace.uvm.edu,
# none with an Expires/Max-Age -- true session cookies, which Chrome does
# NOT write to its on-disk Cookies database at all, by design (that's what
# "session cookie" means: gone the moment the browser process fully closes).
# Every previous fix in this file (the redirect-detection bugs, then a
# cookie-flush settle delay) addressed real problems but couldn't fix this
# one, because there was nothing to fix in the polling logic -- Selenium's
# own driver.quit() ends the browser process the same way closing the
# window would, so the session cookies were ALWAYS going to be discarded
# right there, no matter how long a delay came first.
#
# The actual fix: capture these cookies' VALUES ourselves via
# driver.get_cookies() (which reads the LIVE in-memory cookie jar, session
# cookies included) the moment do_login() confirms success, save them to
# SESSION_COOKIES_FILE, and re-inject them into each new headless browser
# process via driver.add_cookie() before it visits any grades_url. Verified
# working end-to-end: a completely fresh headless process, given only these
# 4 injected values and nothing else, loaded a real grades page.
#
# The real limit this doesn't remove: these values are only good for as
# long as BRIGHTSPACE's OWN server-side session stays alive (its own idle
# timeout, not under this script's control) -- shorter than Duo's "remember
# this device" window that governs how often the interactive Duo push
# itself is needed. Expect to re-run --login somewhat more often than "only
# when login_required shows up after a long gap" -- effectively whenever
# the saved session has gone idle too long, which a login_required status
# already surfaces honestly either way.
SESSION_COOKIE_NAMES = (
    "d2lSessionVal", "d2lSecureSessionVal", "d2lSameSiteCanaryA", "d2lSameSiteCanaryB",
)

# Tried pointing the interactive --login/--inspect steps at Opera instead of
# Selenium's own managed Chrome (2026-08-29, at Charles's request, since
# Opera is his regular daily browser). Abandoned: Opera's installed-file
# version (134.x) and its actual runtime Chromium engine version (150.x, as
# reported to chromedriver's own negotiation) are two unrelated numbering
# schemes, and Selenium Manager's driver resolution validates against the
# file version -- so it rejects ANY chromedriver version requested,
# auto-detected or explicitly overridden, since neither number is the one
# it checks the binary against. Fixing that means downloading a matching
# chromedriver manually and bypassing Selenium Manager's resolution
# entirely -- not worth the added fragility for a one-time sign-in step.
# Both interactive steps use Selenium's own managed Chrome, which works.

LOGIN_POLL_INTERVAL = 3    # seconds between checks
LOGIN_TIMEOUT = 600        # 10 minutes -- Duo push can take a moment, this is one-time setup

# Checked against the FULL url (host + path + query), not just the
# hostname -- 2026-08-29 found TWO different real login pages a host-only
# check missed, from opposite directions: Brightspace's own expired-session
# redirect is brightspace.uvm.edu/d2l/login (the marker's in the PATH, same
# host as everything else); UVM's Shibboleth IdP is
# idp.uvm.edu/idp/profile/SAML2/Redirect/SSO (the marker's in the path
# there too, and the host itself, idp.uvm.edu, doesn't contain any marker
# substring on its own). Both got scraped as if they were real content and
# silently misreported ("parser_not_ready"/false "Signed in") instead of
# the real "you're not logged in." One full-URL check catches both, and the
# next SSO hop that shows up, without needing a new special case each time.
# "idp.uvm.edu" is listed on its own (not just "idp.") since that's the
# actual confirmed hostname -- a bare "idp." risks matching something
# unrelated later.
LOGIN_URL_MARKERS = ("login", "sso", "duosecurity", "shibboleth", "cas.", "idp.uvm.edu")


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}


def make_driver(headless: bool) -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-data-dir={PROFILE_DIR}")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1280,1600")
    # Selenium 4.6+'s built-in Selenium Manager fetches a matching
    # chromedriver automatically -- no separate driver install needed.
    return webdriver.Chrome(options=options)


def save_session_cookies(driver: webdriver.Chrome) -> int:
    """Captures the live D2L session cookies (see SESSION_COOKIE_NAMES) from
    the CURRENT page's cookie jar and writes them to SESSION_COOKIES_FILE.
    Call this while `driver` is actually sitting on brightspace.uvm.edu,
    signed in -- driver.get_cookies() only returns cookies visible to
    whatever page is currently loaded. Returns how many were found."""
    wanted = {c["name"]: c for c in driver.get_cookies() if c["name"] in SESSION_COOKIE_NAMES}
    SESSION_COOKIES_FILE.write_text(
        json.dumps(list(wanted.values()), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return len(wanted)


def inject_session_cookies(driver: webdriver.Chrome) -> bool:
    """Loads SESSION_COOKIES_FILE (if it exists) and injects each cookie
    into `driver` via add_cookie(). Selenium requires the browser to
    already be on the cookie's own domain before add_cookie() will accept
    it, so this navigates to BRIGHTSPACE_HOME first. Returns False (a
    no-op, not an error) if no saved cookies exist yet -- callers fall
    through to the normal login_required reporting in that case."""
    if not SESSION_COOKIES_FILE.exists():
        return False
    try:
        saved = json.loads(SESSION_COOKIES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    if not saved:
        return False

    driver.get(BRIGHTSPACE_HOME)
    for cookie in saved:
        try:
            driver.add_cookie({k: v for k, v in cookie.items() if k in
                               ("name", "value", "domain", "path", "secure", "httpOnly", "sameSite")})
        except WebDriverException:
            pass  # a single bad cookie shouldn't block the others
    return True


def looks_like_login_redirect(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in LOGIN_URL_MARKERS)


def looks_signed_in(url: str) -> bool:
    """True only once we're actually BACK on brightspace.uvm.edu itself, on
    a real page (not its own /d2l/login route) -- a whitelist, not a
    blacklist of login-page markers. Found 2026-08-29, after three
    do_login() runs each reported false success mid-flow: real UVM sign-in
    is a multi-hop redirect (idp.uvm.edu -> a Duo callback on
    duosecurity.com -> back to brightspace.uvm.edu, which is what actually
    sets Brightspace's own session cookie), and that chain passes through
    transitional URLs that don't match any "this is a login page" keyword
    without being real content either -- confirmed by inspecting the
    profile's cookie database directly after a "successful" run: only
    Duo's OWN trust cookies had been persisted, never one for
    brightspace.uvm.edu, meaning the loop closed the browser before the
    redirect chain ever finished. Requiring the host to actually BE
    brightspace.uvm.edu is a positive, narrow signal a blacklist can't give
    no matter how many marker strings get added to it."""
    host = url.split("//", 1)[-1].split("/", 1)[0].lower()
    return host == "brightspace.uvm.edu" and "/d2l/login" not in url.lower()


def parse_grade_page(html: str) -> dict | None:
    """
    STUB — nobody has seen UVM's real Brightspace grades HTML yet.

    How to finish this:
      1. `python scripts/fetch_grades.py --inspect <tag>` against a class
         with real grades posted. It saves the full rendered page to
         data/grades_debug_<tag>.html and prints the page title.
      2. Bring that file (or the relevant snippet) into a Claude Code
         session and find the element holding the overall/final grade.
      3. Replace this function's body with real parsing (BeautifulSoup, or
         a plain string/regex search over `html`) that pulls that text out
         and returns {"grade_text": "<exactly what Brightspace shows>"}.

    Returns None until then, on purpose — BUILD.md's core design rule is
    "the page never invents data." A guessed selector that quietly returns
    nothing real, or the wrong thing, is worse than an honest
    "parser not built yet."
    """
    return None


def result_template(status: str, **extra) -> dict:
    base = {"status": status, "error": None, "grade_text": None, "debug_file": None,
            "same_as": None}
    base.update(extra)
    return base


def fetch_one(driver: webdriver.Chrome, tag: str, url: str) -> dict:
    try:
        driver.get(url)
        time.sleep(2)  # Brightspace's grades page renders client-side; give it a beat
        if looks_like_login_redirect(driver.current_url):
            return result_template("login_required")

        html = driver.page_source
        debug_file = DATA_DIR / f"grades_debug_{tag}.html"
        debug_file.write_text(html, encoding="utf-8")

        parsed = parse_grade_page(html)
        if parsed is None:
            return result_template("parser_not_ready", debug_file=debug_file.name)
        return result_template("connected", **parsed)
    except WebDriverException as exc:
        return result_template("error", error=f"{type(exc).__name__}: {exc}")


def do_login() -> int:
    """
    Opens a visible browser and waits for you to sign in BY HAND, then
    returns on its own -- no terminal keypress required (see module
    docstring for why: this needs to work even when launched from a shell
    with no keyboard attached, not just one you're sitting at).

    Detection is "we're actually back on brightspace.uvm.edu" (see
    looks_signed_in) -- a positive signal, not "doesn't look like a login
    page anymore." Two consecutive matching polls, one LOGIN_POLL_INTERVAL
    apart, are required before declaring success, as extra insurance
    against catching a single-frame transitional URL mid-redirect.

    The actual session is saved via save_session_cookies() -- see
    SESSION_COOKIE_NAMES above for why that, not Chrome's own on-disk
    profile, is what later headless runs actually depend on.
    """
    PROFILE_DIR.mkdir(exist_ok=True)
    driver = make_driver(headless=False)
    try:
        driver.get(BRIGHTSPACE_HOME)
        print("A Chrome window opened. Log in with your NetID (and Duo) there.")
        print(f"Waiting up to {LOGIN_TIMEOUT // 60} minutes for you to finish...")
        time.sleep(2)
        deadline = time.monotonic() + LOGIN_TIMEOUT
        consecutive_hits = 0
        while time.monotonic() < deadline:
            if looks_signed_in(driver.current_url):
                consecutive_hits += 1
                if consecutive_hits >= 2:
                    count = save_session_cookies(driver)
                    print(f"Signed in. Saved {count} session cookie(s) for later headless runs.")
                    return 0
            else:
                consecutive_hits = 0
            time.sleep(LOGIN_POLL_INTERVAL)
        print("Timed out waiting for sign-in. Run --login again when you're ready.")
        return 1
    finally:
        driver.quit()


def do_inspect(tag: str) -> int:
    config = load_config()
    classes = {c["tag"]: c for c in (config.get("classes") or [])}
    course = classes.get(tag)
    if course is None:
        print(f"No class tagged {tag!r} in config.yaml.")
        return 1
    alias = course.get("same_class_as")
    if alias:
        print(f"{tag} has same_class_as: {alias} -- it's not scraped on its own. "
              f"Run --inspect {alias} instead.")
        return 1
    url = (course.get("grades_url") or "").strip()
    if not url:
        print(f"{tag} has no grades_url set in config.yaml yet.")
        return 1
    if not SESSION_COOKIES_FILE.exists():
        print("No saved Brightspace session yet. Run: python scripts/fetch_grades.py --login")
        return 1

    DATA_DIR.mkdir(exist_ok=True)
    driver = make_driver(headless=False)
    try:
        inject_session_cookies(driver)
        driver.get(url)
        time.sleep(3)
        current_url = driver.current_url
        print(f"Landed on: {current_url}")
        if looks_like_login_redirect(current_url):
            print("That looks like a login page, not grades. Run --login first.")
            return 1
        debug_file = DATA_DIR / f"grades_debug_{tag}.html"
        debug_file.write_text(driver.page_source, encoding="utf-8")
        print(f"Page title: {driver.title!r}")
        print(f"Saved rendered page to {debug_file}")
        print("Bring that file into a Claude Code session to finish parse_grade_page().")
    finally:
        driver.quit()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true",
                        help="open a visible browser to sign into Brightspace once")
    parser.add_argument("--inspect", metavar="TAG",
                        help="open one class's grades page visibly and dump its HTML for inspection")
    args = parser.parse_args()

    if args.login:
        return do_login()
    if args.inspect:
        return do_inspect(args.inspect)

    config = load_config()
    classes = config.get("classes") or []
    # A class with same_class_as set isn't a separate Brightspace course --
    # see config.yaml's schema comment and BUILD.md, "Same-class aliasing."
    # Skip it entirely rather than scraping the same page twice under two
    # tags; its grades.json entry just points at the tag that owns the real
    # grades_url instead.
    aliased = {c["tag"]: c["same_class_as"] for c in classes if c.get("same_class_as")}
    targets = [(c["tag"], (c.get("grades_url") or "").strip())
               for c in classes
               if c["tag"] not in aliased and (c.get("grades_url") or "").strip()]

    DATA_DIR.mkdir(exist_ok=True)
    results: dict[str, dict] = {
        c["tag"]: (result_template("same_as", same_as=aliased[c["tag"]])
                   if c["tag"] in aliased else result_template("not_configured"))
        for c in classes
    }

    if not targets:
        print("No classes have grades_url set in config.yaml yet.")
    elif not SESSION_COOKIES_FILE.exists():
        print("No saved Brightspace session yet. Run: python scripts/fetch_grades.py --login")
        for tag, _ in targets:
            results[tag] = result_template("login_required")
    else:
        driver = make_driver(headless=True)
        try:
            inject_session_cookies(driver)
            for tag, url in targets:
                results[tag] = fetch_one(driver, tag, url)
                print(f"  {tag}: {results[tag]['status']}")
        finally:
            driver.quit()

    payload = {
        "generated": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "classes": results,
    }
    out = DATA_DIR / "grades.json"
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote grades.json ({len(results)} classes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
