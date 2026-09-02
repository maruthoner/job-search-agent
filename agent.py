#!/usr/bin/env python3
"""
Job search agent - JSearch (RapidAPI) -> filtered job list -> HTML dashboard.

Runs once a day at 9:15am New York time. Each run spends 6 API requests
(3 title themes x 2 locations), which is 180/month on a 31-day month and stays
inside the ~200 request/month free tier.

Usage:
    python3 agent.py            # normal scheduled run (exits quietly outside the window)
    python3 agent.py --force    # run right now regardless of the clock
    python3 agent.py --all      # search all five themes (10 requests) - use sparingly
    python3 agent.py --render   # rebuild the HTML from saved data, no API calls
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HTML_FILE = os.path.join(DOCS_DIR, "index.html")

ET = ZoneInfo("America/New_York")
API_HOST = "jsearch.p.rapidapi.com"
API_PATH = "/search-v2"   # /search was retired by the provider

# ---------------------------------------------------------------- settings

SALARY_FLOOR = 215_000
MAX_AGE_HOURS = 25             # only admit postings this fresh
# The provider's own date_posted filter returns zero results, so it is not sent.
# Freshness is enforced here against each posting's real timestamp.
EMPLOYMENT_TYPES = "FULLTIME,CONTRACTOR"
RETAIN_DAYS = 30               # how long a job stays on the dashboard
NEW_FOR_HOURS = 36             # how long a job wears the NEW badge
MONTHLY_REQUEST_CAP = 195      # fallback cap if the API stops reporting quota
QUOTA_RESERVE = 5              # stop this many requests short of the real limit

RUN_AT = (9, 15)               # 9:15am New York time
WINDOW_MINUTES = 75            # a late GitHub Actions start still counts

# The five title themes. Three are searched each day, rotating, so every theme
# is searched at least every other day - comfortably inside the 3-day lookback,
# so no posting can slip through between its turns.
THEMES = [
    "product manager",
    "program manager",
    "project manager",
    "transformation lead",
    "chief of staff",
    "AI enablement",
]
THEMES_PER_RUN = 3

# A job is kept only if its title looks like one of these.
TITLE_PATTERNS = [
    # program
    r"\bprogram(me)?\s+manager\b", r"\bprogram(me)?\s+lead\b",
    r"\bprogram(me)?\s+director\b", r"\bprogram(me)?\s+management\b",
    # project
    r"\bproject\s+manager\b", r"\bproject\s+management\b", r"\bproject\s+lead\b",
    r"\bproject\s+director\b", r"\bdirector\s+of\s+projects?\b",
    # product
    r"\bproduct\s+manager\b", r"\bproduct\s+management\b", r"\bproduct\s+lead\b",
    r"\bproduct\s+owner\b", r"\bdirector\s+of\s+product\b",
    r"\bhead\s+of\s+product\b", r"\bgroup\s+product\b", r"\bproduct\s+director\b",
    # transformation / change
    r"\btransformation\b", r"\bchange\s+management\b", r"\bchange\s+lead\b",
    r"\bbusiness\s+change\b",
    # portfolio / PMO / chief of staff
    # AI enablement / adoption
    r"\b(ai|a\.i\.|artificial\s+intelligence|gen\s?ai|generative\s+ai)\s*"
    r"(enablement|adoption|transformation|readiness|strategy|program|programme)\b",
    r"\benablement\s+(lead|leader|manager|director|head)\b",
    r"\bhead\s+of\s+ai\b", r"\bdirector\s+of\s+ai\b",
    r"\bportfolio\s+director\b", r"\bchief\s+of\s+staff\b",
    r"\bdelivery\s+manager\b", r"\bdelivery\s+lead\b",
]
TITLE_RE = re.compile("|".join(TITLE_PATTERNS), re.I)

# Titles that match the patterns above but are not roles Ruth wants.
# "technical" is excluded outright, which also rules out Technical Program
# Manager, Technical Project Manager and TPM.
TITLE_EXCLUDE_RE = re.compile(
    r"\b(technical|technically|"
    r"engineer|engineering|developer|architect|scientist|designer|"
    r"construction|constructions|builder|contracting|"
    r"clinical|clinician|preclinical|"
    r"intern|internship|apprentice|assistant to|coordinator|"
    r"junior|entry[- ]level|associate product manager|graduate|"
    r"trainee|analyst i)\b", re.I)

# For jobs with no posted salary: only keep them if the title reads senior.
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|principal|staff|lead|leader|director|head\s+of|"
    r"vp\b|vice\s+president|group|chief|executive|global|distinguished|"
    r"manager\s+ii|manager\s+iii|iii|iv)\b", re.I)

PERIOD_MULTIPLIER = {
    "HOUR": 2080, "HOURLY": 2080,
    "DAY": 260, "DAILY": 260,
    "WEEK": 52, "WEEKLY": 52,
    "MONTH": 12, "MONTHLY": 12,
    "YEAR": 1, "YEARLY": 1, "ANNUAL": 1,
}

# ---------------------------------------------------------------- helpers


def log(msg):
    print(f"[{datetime.now(ET):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


def now_utc():
    return datetime.now(timezone.utc)


def in_window():
    """True if the New York clock is inside today's run window."""
    now = datetime.now(ET)
    start = now.replace(hour=RUN_AT[0], minute=RUN_AT[1], second=0, microsecond=0)
    return start <= now < start + timedelta(minutes=WINDOW_MINUTES)


def themes_for_today(day=None):
    """Rotate THEMES_PER_RUN themes per day so all five stay covered."""
    day = day or date.today()
    start = (day.toordinal() * THEMES_PER_RUN) % len(THEMES)
    return [THEMES[(start + i) % len(THEMES)] for i in range(THEMES_PER_RUN)]

# ---------------------------------------------------------------- api


def record_quota(state, headers):
    """RapidAPI reports the true remaining allowance on every billed response."""
    remaining = headers.get("x-ratelimit-requests-remaining")
    limit = headers.get("x-ratelimit-requests-limit")
    if remaining is None:
        return
    try:
        state["quota"] = {
            "remaining": int(remaining),
            "limit": int(limit) if limit is not None else None,
            "checked_at": now_utc().isoformat(timespec="seconds"),
        }
    except ValueError:
        pass


def out_of_quota(state):
    """True if the real allowance is nearly spent. Falls back to local counting."""
    quota = state.get("quota") or {}
    if "remaining" in quota:
        if quota["remaining"] <= QUOTA_RESERVE:
            log(f"  SKIP: only {quota['remaining']} requests left on the plan "
                f"(reserve is {QUOTA_RESERVE})")
            return True
        return False
    month = f"{datetime.now(ET):%Y-%m}"
    used = state.get("requests", {}).get(month, 0)
    if used >= MONTHLY_REQUEST_CAP:
        log(f"  SKIP: local request cap reached ({used}/{MONTHLY_REQUEST_CAP})")
        return True
    return False


def api_search(api_key, query, extra, state):
    """One JSearch request. Returns a list of raw job dicts, or None on failure."""
    month = f"{datetime.now(ET):%Y-%m}"
    if out_of_quota(state):
        return None

    params = {
        "query": query,
        "employment_types": EMPLOYMENT_TYPES,
        "country": "us",
        "language": "en",
    }
    params.update(extra)
    url = f"https://{API_HOST}{API_PATH}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": API_HOST,
    })

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode())
                record_quota(state, resp.headers)
            counter = state.setdefault("requests", {})
            counter[month] = counter.get(month, 0) + 1
            data = payload.get("data") or {}
            data = data.get("jobs", []) if isinstance(data, dict) else data
            left = (state.get("quota") or {}).get("remaining", "?")
            log(f"  {query!r}{' [remote]' if extra else ''} -> {len(data)} results "
                f"({left} requests left on the plan)")
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            last_err = f"HTTP {exc.code}: {body}"
            record_quota(state, exc.headers)
            # RapidAPI does not bill 401s or 404s - only count what it charges for.
            if exc.code not in (401, 404):
                counter = state.setdefault("requests", {})
                counter[month] = counter.get(month, 0) + 1
            if exc.code == 404:
                log("  The plan on this API key does not include this endpoint.")
                return None
            if exc.code in (429, 403):
                log(f"  RATE LIMIT / AUTH problem: {last_err}")
                return None
            if exc.code < 500:
                break
        except Exception as exc:                      # noqa: BLE001
            last_err = repr(exc)
        time.sleep(3 * (attempt + 1))
    log(f"  FAILED: {last_err}")
    return None

# ---------------------------------------------------------------- filtering


SALARY_STRING_RE = re.compile(
    r"(?P<cur>[$£€])?\s*(?P<num>\d[\d,]*(?:\.\d+)?)\s*(?P<k>[KkMm])?")
PERIOD_WORD_RE = re.compile(
    r"\b(?:per|an?|/)\s*(hour|hr|year|yr|annum|month|mo|week|wk|day)\b", re.I)
PERIOD_WORD = {"hour": "HOUR", "hr": "HOUR", "year": "YEAR", "yr": "YEAR",
               "annum": "YEAR", "month": "MONTH", "mo": "MONTH",
               "week": "WEEK", "wk": "WEEK", "day": "DAY"}


def parse_salary_string(text):
    """'$150K - $200K a year' -> (150000, 200000, 'YEAR', 'USD'). None if unusable."""
    if not text:
        return None
    if "£" in text or "€" in text:
        return None                                  # not USD, skip
    numbers = []
    for m in SALARY_STRING_RE.finditer(text):
        raw = m.group("num").replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        suffix = (m.group("k") or "").upper()
        if suffix == "K":
            val *= 1_000
        elif suffix == "M":
            val *= 1_000_000
        if val >= 10:                                # ignore stray small numbers
            numbers.append(val)
    if not numbers:
        return None
    period_match = PERIOD_WORD_RE.search(text)
    period = PERIOD_WORD.get(period_match.group(1).lower(), "YEAR") if period_match else None
    if period is None:
        # no period word: infer from magnitude
        period = "HOUR" if max(numbers) < 1_000 else "YEAR"
    lo, hi = min(numbers), max(numbers)
    return lo, hi, period, "USD"


def annual_salary(job):
    """Return (min_annual, max_annual, currency, was_posted)."""
    lo = job.get("job_min_salary")
    hi = job.get("job_max_salary")
    period = (job.get("job_salary_period") or "YEAR").upper()
    cur = job.get("job_salary_currency") or "USD"
    if lo is None and hi is None:
        sal = job.get("job_salary")
        if isinstance(sal, dict):
            lo, hi = sal.get("min"), sal.get("max")
            period = (sal.get("period") or period).upper()
            cur = sal.get("currency") or cur
    if lo is None and hi is None:
        parsed = parse_salary_string(job.get("job_salary_string"))
        if parsed:
            lo, hi, period, cur = parsed
    if lo is None and hi is None:
        return None, None, cur, False
    mult = PERIOD_MULTIPLIER.get(period, 1)
    lo_a = round(lo * mult) if isinstance(lo, (int, float)) else None
    hi_a = round(hi * mult) if isinstance(hi, (int, float)) else None
    return lo_a, hi_a, cur, True


def location_label(job):
    if job.get("job_location"):
        return job["job_location"]
    bits = [job.get("job_city"), job.get("job_state")]
    label = ", ".join(b for b in bits if b)
    return label or (job.get("job_country") or "United States")


def normalize(job, bucket, theme=None):
    lo, hi, cur, posted = annual_salary(job)
    title = job.get("job_title") or ""
    desc = (job.get("job_description") or "").strip()
    return {
        "id": job.get("job_id"),
        "title": title,
        "company": job.get("employer_name") or "Unknown",
        "logo": job.get("employer_logo"),
        "location": location_label(job),
        "is_remote": bool(job.get("job_is_remote")),
        "bucket": bucket,                       # "nyc" or "remote"
        "theme": theme,
        "employment_type": (job.get("job_employment_type")
                            or (job.get("job_employment_types") or [""])[0]).title(),
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": cur,
        "salary_posted": posted,
        "posted_at": job.get("job_posted_at_datetime_utc"),
        "apply_link": (job.get("job_apply_link")
                       or (job.get("apply_options") or [{}])[0].get("apply_link")),
        "publisher": job.get("job_publisher"),
        "snippet": re.sub(r"\s+", " ", desc)[:420],
        "senior": bool(SENIOR_RE.search(title)),
        "date_unknown": not job.get("job_posted_at_datetime_utc"),
    }


def age_hours(job):
    """Hours since the job was posted, or None if the API did not say."""
    stamp = job.get("posted_at")
    if not stamp:
        return None
    try:
        posted = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now_utc() - posted).total_seconds() / 3600


def too_old(job):
    """Postings with no date are admitted and flagged, not silently dropped."""
    hours = age_hours(job)
    if hours is None:
        return False
    return hours > MAX_AGE_HOURS


def keep(job):
    """Apply the title, freshness and salary rules. Returns (keep?, reason)."""
    title = job["title"]
    if too_old(job):
        return False, "too old"
    if not TITLE_RE.search(title):
        return False, "wrong title"
    if TITLE_EXCLUDE_RE.search(title):
        return False, "excluded title"
    if job["salary_posted"]:
        top = job["salary_max"] or job["salary_min"] or 0
        if (job["salary_currency"] or "USD").upper() != "USD":
            return False, "not USD"
        if top < SALARY_FLOOR:
            return False, "below $215k"
        return True, "salary meets floor"
    if job["senior"]:
        return True, "no salary posted, senior title"
    return False, "no pay, title not senior"

# ---------------------------------------------------------------- run


def run(api_key, themes, state, store):
    log(f"Searching {len(themes)} themes x 2 locations = {len(themes) * 2} requests")
    log(f"Themes today: {', '.join(themes)}")

    raw_count = 0
    kept = 0
    added = 0
    ok_searches = 0
    failed_searches = 0
    rejected = {}
    seen_now = now_utc().isoformat(timespec="seconds")

    for theme in themes:
        for bucket, query, extra in (
            ("nyc", f"{theme} in New York, NY", {}),
            ("remote", f"{theme} in United States", {"work_from_home": "true"}),
        ):
            results = api_search(api_key, query, extra, state)
            if results is None:
                failed_searches += 1
                continue
            ok_searches += 1
            raw_count += len(results)
            for raw in results:
                job = normalize(raw, bucket, theme)
                if not job["id"]:
                    continue
                ok, reason = keep(job)
                if not ok:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue
                kept += 1
                existing = store.get(job["id"])
                if existing:
                    first_seen = existing.get("first_seen", seen_now)
                    existing.update(job)
                    existing["first_seen"] = first_seen
                    existing["last_seen"] = seen_now
                else:
                    job["first_seen"] = seen_now
                    job["last_seen"] = seen_now
                    store[job["id"]] = job
                    added += 1

    log(f"{ok_searches} searches succeeded, {failed_searches} failed. "
        f"Scanned {raw_count} listings, {kept} passed the filters, "
        f"{added} brand new.")
    for reason, count in sorted(rejected.items(), key=lambda kv: -kv[1]):
        log(f"    rejected - {reason}: {count}")

    cutoff = now_utc() - timedelta(days=RETAIN_DAYS)
    stale = [jid for jid, j in store.items()
             if datetime.fromisoformat(j["last_seen"]) < cutoff]
    for jid in stale:
        del store[jid]
    if stale:
        log(f"Removed {len(stale)} jobs older than {RETAIN_DAYS} days.")

    state.setdefault("runs", [])
    state["runs"].append({
        "at": seen_now, "themes": themes,
        "raw": raw_count, "kept": kept, "added": added,
        "rejected": rejected, "searches": ok_searches,
    })
    state["runs"] = state["runs"][-60:]
    state["last_run_date"] = f"{datetime.now(ET):%Y-%m-%d}"

    if ok_searches == 0:
        raise RuntimeError(
            f"Every one of the {failed_searches} searches failed - the dashboard "
            f"was not refreshed. See the errors above.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run now, ignore the clock")
    ap.add_argument("--all", action="store_true", help="search all five themes")
    ap.add_argument("--render", action="store_true", help="rebuild HTML only")
    args = ap.parse_args()

    state = load_json(STATE_FILE, {})
    store = load_json(JOBS_FILE, {})

    if not args.render:
        today = f"{datetime.now(ET):%Y-%m-%d}"
        if not (args.force or args.all):
            if not in_window():
                log("Not inside the 9:15am ET run window - nothing to do. "
                    "(No API calls spent.)")
                return 0
            if state.get("last_run_date") == today:
                log("Already ran today - skipping to protect the API quota.")
                return 0

        api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not api_key:
            log("ERROR: RAPIDAPI_KEY is not set.")
            return 1

        themes = THEMES if args.all else themes_for_today()
        try:
            run(api_key, themes, state, store)
        except RuntimeError as exc:
            save_json(STATE_FILE, state)     # keep whatever quota info we learned
            log(f"ERROR: {exc}")
            return 1
        save_json(JOBS_FILE, store)
        save_json(STATE_FILE, state)

    from render import render_dashboard
    render_dashboard(store, state, HTML_FILE)
    log(f"Wrote {HTML_FILE} ({len(store)} jobs on the board).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
