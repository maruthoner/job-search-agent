#!/usr/bin/env python3
"""
Job search agent - JSearch (RapidAPI) -> filtered job list -> HTML dashboard.

Runs three times a day. Each run spends exactly 2 API requests (one for the
New York search, one for the US-remote search) for a single rotating title
group, which keeps us inside the ~200 request/month free tier.

Usage:
    python3 agent.py            # normal scheduled run (exits quietly if not a run window)
    python3 agent.py --force    # run right now, pick the nearest slot
    python3 agent.py --slot 1   # run a specific slot
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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HTML_FILE = os.path.join(DOCS_DIR, "index.html")

ET = ZoneInfo("America/New_York")
API_HOST = "jsearch.p.rapidapi.com"

# ---------------------------------------------------------------- settings

SALARY_FLOOR = 215_000
DATE_POSTED = "3days"          # how far back to look
EMPLOYMENT_TYPES = "FULLTIME,CONTRACTOR"
RETAIN_DAYS = 30               # how long a job stays on the dashboard
NEW_FOR_HOURS = 24             # how long a job wears the NEW badge
MONTHLY_REQUEST_CAP = 190      # hard stop so we never exceed the 200/mo plan

# Three run windows in New York time. Each owns one title group.
# A window is open for 75 minutes so a late GitHub Actions start still counts.
SLOTS = [
    {"at": (9, 30),  "label": "9:30am", "query": "product manager"},
    {"at": (13, 0),  "label": "1:00pm", "query": "program manager"},
    {"at": (17, 30), "label": "5:30pm", "query": "transformation lead"},
]
WINDOW_MINUTES = 75

# A job is kept only if its title looks like one of these.
TITLE_PATTERNS = [
    r"\bprogram\s+manager\b", r"\bprogramme\s+manager\b", r"\bprogram\s+lead\b",
    r"\bprogram\s+director\b", r"\btechnical\s+program\s+manager\b", r"\btpm\b",
    r"\bproduct\s+manager\b", r"\bproduct\s+management\b", r"\bproduct\s+lead\b",
    r"\bproduct\s+owner\b", r"\bdirector\s+of\s+product\b", r"\bhead\s+of\s+product\b",
    r"\bgroup\s+product\b", r"\bproduct\s+director\b",
    r"\btransformation\b", r"\bchange\s+management\b", r"\bchange\s+lead\b",
    r"\bpmo\b", r"\bportfolio\s+manager\b", r"\bportfolio\s+lead\b",
    r"\bportfolio\s+director\b", r"\bchief\s+of\s+staff\b",
]
TITLE_RE = re.compile("|".join(TITLE_PATTERNS), re.I)

# Junk that sometimes matches the patterns above.
TITLE_EXCLUDE_RE = re.compile(
    r"\b(intern|internship|apprentice|assistant to|coordinator|"
    r"junior|entry[- ]level|associate product manager|graduate)\b", re.I)

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


def pick_slot(force=False):
    """Return the slot index whose window contains 'now', else None."""
    now = datetime.now(ET)
    for idx, slot in enumerate(SLOTS):
        hh, mm = slot["at"]
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=WINDOW_MINUTES):
            return idx
    if force:
        # nearest slot by clock distance
        mins_now = now.hour * 60 + now.minute
        return min(range(len(SLOTS)),
                   key=lambda i: abs(SLOTS[i]["at"][0] * 60 + SLOTS[i]["at"][1] - mins_now))
    return None

# ---------------------------------------------------------------- api


def api_search(api_key, query, extra, state):
    """One JSearch request. Returns list of raw job dicts."""
    month = f"{datetime.now(ET):%Y-%m}"
    used = state.get("requests", {}).get(month, 0)
    if used >= MONTHLY_REQUEST_CAP:
        log(f"SKIP: monthly request cap reached ({used}/{MONTHLY_REQUEST_CAP})")
        return None

    params = {
        "query": query,
        "page": "1",
        "num_pages": "1",
        "date_posted": DATE_POSTED,
        "employment_types": EMPLOYMENT_TYPES,
        "country": "us",
    }
    params.update(extra)
    url = f"https://{API_HOST}/search?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": API_HOST,
    })

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                payload = json.loads(resp.read().decode())
            state.setdefault("requests", {})[month] = used + 1
            data = payload.get("data") or []
            log(f"  query={query!r} extra={extra} -> {len(data)} results "
                f"(request {used + 1}/{MONTHLY_REQUEST_CAP} this month)")
            return data
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            last_err = f"HTTP {exc.code}: {body}"
            state.setdefault("requests", {})[month] = used + 1  # a 4xx still counts
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


def normalize(job, bucket):
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
        "employment_type": (job.get("job_employment_type") or "").title(),
        "salary_min": lo,
        "salary_max": hi,
        "salary_currency": cur,
        "salary_posted": posted,
        "posted_at": job.get("job_posted_at_datetime_utc"),
        "apply_link": job.get("job_apply_link"),
        "publisher": job.get("job_publisher"),
        "snippet": re.sub(r"\s+", " ", desc)[:420],
        "senior": bool(SENIOR_RE.search(title)),
    }


def keep(job):
    """Apply the title and salary rules. Returns (keep?, reason)."""
    title = job["title"]
    if not TITLE_RE.search(title):
        return False, "title does not match"
    if TITLE_EXCLUDE_RE.search(title):
        return False, "title excluded"
    if job["salary_posted"]:
        top = job["salary_max"] or job["salary_min"] or 0
        if (job["salary_currency"] or "USD").upper() != "USD":
            return False, "non-USD salary"
        if top < SALARY_FLOOR:
            return False, f"salary {top} below floor"
        return True, "salary meets floor"
    if job["senior"]:
        return True, "no salary posted, senior title"
    return False, "no salary posted, title not senior"

# ---------------------------------------------------------------- run


def run(api_key, slot_idx, state, store):
    slot = SLOTS[slot_idx]
    query = slot["query"]
    log(f"Slot {slot_idx} ({slot['label']}) - title group: {query!r}")

    searches = [
        ("nyc", f"{query} in New York, NY", {}),
        ("remote", f"{query} in United States", {"remote_jobs_only": "true"}),
    ]

    raw_count = 0
    kept = 0
    seen_now = now_utc().isoformat(timespec="seconds")

    for bucket, q, extra in searches:
        results = api_search(api_key, q, extra, state)
        if results is None:
            continue
        raw_count += len(results)
        for raw in results:
            job = normalize(raw, bucket)
            if not job["id"]:
                continue
            ok, reason = keep(job)
            if not ok:
                continue
            kept += 1
            existing = store.get(job["id"])
            if existing:
                existing.update(job)
                existing["last_seen"] = seen_now
            else:
                job["first_seen"] = seen_now
                job["last_seen"] = seen_now
                store[job["id"]] = job

    log(f"Searched {raw_count} raw results, {kept} passed the filters.")

    # age out old entries
    cutoff = now_utc() - timedelta(days=RETAIN_DAYS)
    stale = [jid for jid, j in store.items()
             if datetime.fromisoformat(j["last_seen"]) < cutoff]
    for jid in stale:
        del store[jid]
    if stale:
        log(f"Removed {len(stale)} jobs older than {RETAIN_DAYS} days.")

    state.setdefault("runs", [])
    state["runs"].append({
        "at": seen_now,
        "slot": slot_idx,
        "query": query,
        "raw": raw_count,
        "kept": kept,
    })
    state["runs"] = state["runs"][-60:]
    state["last_slot_done"] = {"date": f"{datetime.now(ET):%Y-%m-%d}", "slot": slot_idx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run now, nearest slot")
    ap.add_argument("--slot", type=int, choices=[0, 1, 2])
    ap.add_argument("--render", action="store_true", help="rebuild HTML only")
    args = ap.parse_args()

    state = load_json(STATE_FILE, {})
    store = load_json(JOBS_FILE, {})

    if not args.render:
        slot_idx = args.slot if args.slot is not None else pick_slot(args.force)
        if slot_idx is None:
            log("Not inside a run window - nothing to do. (No API calls spent.)")
            return 0

        done = state.get("last_slot_done") or {}
        if (done.get("date") == f"{datetime.now(ET):%Y-%m-%d}"
                and done.get("slot") == slot_idx and args.slot is None
                and not args.force):
            log(f"Slot {slot_idx} already ran today - skipping to protect the quota.")
            return 0

        api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not api_key:
            log("ERROR: RAPIDAPI_KEY is not set.")
            return 1

        run(api_key, slot_idx, state, store)
        save_json(JOBS_FILE, store)
        save_json(STATE_FILE, state)

    from render import render_dashboard
    render_dashboard(store, state, HTML_FILE)
    log(f"Wrote {HTML_FILE} ({len(store)} jobs on the board).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
