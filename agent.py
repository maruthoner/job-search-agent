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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DOCS_DIR = os.path.join(ROOT, "docs")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
HTML_FILE = os.path.join(DOCS_DIR, "index.html")

ET = ZoneInfo("America/New_York")

# Adzuna is the primary source: every listing carries a real posting date and
# the free tier is generous, so it runs daily with a true 25-hour window.
ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/us/search/1"
ADZUNA_MAX_AGE_HOURS = 25
ADZUNA_PER_PAGE = 50

# JSearch is the secondary source. Its index runs 9+ days behind and leaves
# 40% of listings undated, but it surfaces large-employer roles Adzuna misses,
# so it sweeps once a week on the "new to this agent" basis instead.
API_HOST = "jsearch.p.rapidapi.com"
API_PATH = "/search-v2"   # /search was retired by the provider
JSEARCH_WEEKDAY = 0       # Monday
JSEARCH_MAX_AGE_DAYS = 30

# ---------------------------------------------------------------- settings

SALARY_FLOOR = 215_000

EMPLOYMENT_TYPES = "FULLTIME,CONTRACTOR"
RETAIN_DAYS = 30               # how long a job stays on the dashboard
NEW_FOR_HOURS = 36             # how long a job wears the NEW badge
MONTHLY_REQUEST_CAP = 195      # fallback cap if the API stops reporting quota
QUOTA_RESERVE = 5              # stop this many requests short of the real limit

RUN_AT = (9, 15)               # earliest time of day the run may happen (ET)
# There is deliberately no "window". GitHub's scheduler drops and delays cron
# firings, so the rule is simply: if today's run has not happened yet and it is
# past 9:15am in New York, run now. The workflow fires several times through the
# morning; the first one GitHub actually delivers does the work and the rest
# exit in seconds without spending a single API call.

# All themes are searched on every run. Adzuna's free tier allows hundreds of
# calls a day, so there is no need to rotate them.
THEMES = [
    "product manager",
    "program manager",
    "project manager",
    "transformation lead",
    "chief of staff",
    "AI enablement",
]

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
    # building trades - these carry $215k+ project manager titles that are not
    # the kind of programme or product work Ruth is looking for
    r"high[-\s]?rise|civil|mep|hvac|plumbing|electrical|mechanical|"
    r"structural|geotechnical|survey|surveyor|surveying|superintendent|"
    r"masonry|concrete|roofing|drywall|facilities|capital\s+projects|"
    r"built\s+environment|site\s+safety|ground\s+up|"
    r"clinical|clinician|preclinical|"
    r"intern|internship|apprentice|assistant to|coordinator|"
    r"junior|entry[- ]level|associate product manager|graduate|"
    r"trainee|analyst i)\b", re.I)

# Some employers are themselves the disqualifier: a construction manager or a
# commercial property firm posts "Senior Project Manager" with no hint in the
# title. Matched against the employer name, not the title.
COMPANY_EXCLUDE_RE = re.compile(
    r"\b(construction|constructors|contracting|contractors|builders|"
    r"realty|real\s+estate|properties|property\s+group|roofing|"
    r"millichap|masonry|paving|excavat\w*)\b", re.I)


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


def due(state):
    """Is today's run still outstanding? Returns (should_run, why_not)."""
    now = datetime.now(ET)
    today = f"{now:%Y-%m-%d}"
    if state.get("last_run_date") == today:
        return False, "already ran today"
    earliest = now.replace(hour=RUN_AT[0], minute=RUN_AT[1],
                           second=0, microsecond=0)
    if now < earliest:
        return False, (f"before today's {RUN_AT[0]}:{RUN_AT[1]:02d}am ET "
                       f"start time")
    return True, ""


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

def adzuna_search(theme, bucket, state):
    """One Adzuna request. Returns a list of raw job dicts, or None on failure."""
    app_id = os.environ.get("ADZUNA_APP_ID", "").strip()
    app_key = os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not (app_id and app_key):
        log("  ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY are not set.")
        return None

    params = {
        "app_id": app_id, "app_key": app_key,
        "results_per_page": str(ADZUNA_PER_PAGE),
        "what": theme if bucket == "nyc" else f"{theme} remote",
        "sort_by": "date",
        "max_days_old": "2",          # a day of slack; the real cut is 25 hours
        "content-type": "application/json",
    }
    if bucket == "nyc":
        params["where"] = "New York, New York"

    url = ADZUNA_URL + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=45) as r:
                payload = json.loads(r.read().decode())
            month = f"{datetime.now(ET):%Y-%m}"
            counter = state.setdefault("adzuna_requests", {})
            counter[month] = counter.get(month, 0) + 1
            results = payload.get("results") or []
            log(f"  [adzuna] {theme!r} {bucket} -> {len(results)} listings")
            return results
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"
            if exc.code < 500:
                break
        except Exception as exc:                      # noqa: BLE001
            last_err = repr(exc)
        time.sleep(3 * (attempt + 1))
    log(f"  [adzuna] FAILED: {last_err}")
    return None


def normalize_adzuna(job, bucket, theme):
    """Map an Adzuna record onto the same shape as a JSearch one."""
    predicted = str(job.get("salary_is_predicted", "0")) == "1"
    lo, hi = job.get("salary_min"), job.get("salary_max")
    title = job.get("title") or ""
    desc = re.sub(r"<[^>]+>", " ", job.get("description") or "")
    location = (job.get("location") or {}).get("display_name") or "United States"
    blob = f"{title} {desc} {location}".lower()
    has_field_salary = bool(not predicted and (lo is not None or hi is not None))
    source = "employer" if has_field_salary else None
    if not has_field_salary:
        found = find_salary_in_text(desc)
        if found:
            lo, hi, source = found[0], found[1], "text"
    return {
        "id": f"adzuna:{job.get('id')}",
        "source": "adzuna",
        "title": title,
        "company": (job.get("company") or {}).get("display_name") or "Unknown",
        "logo": None,
        "location": location,
        "is_remote": "remote" in blob or "work from home" in blob,
        "bucket": bucket,
        "theme": theme,
        "employment_type": (job.get("contract_time") or
                            job.get("contract_type") or "").replace("_", "-").title(),
        # A predicted salary is Adzuna's estimate, not the employer's figure.
        # It must never satisfy the salary floor, so it is recorded as unlisted.
        "salary_min": lo if source else None,
        "salary_max": hi if source else None,
        "salary_currency": "USD",
        "salary_posted": has_field_salary,
        "salary_source": source,
        "salary_estimated": predicted,
        "posted_at": job.get("created"),
        "apply_link": job.get("redirect_url"),
        "publisher": "Adzuna",
        "snippet": re.sub(r"\s+", " ", desc).strip()[:420],
        "senior": bool(SENIOR_RE.search(title)),
        "date_unknown": not job.get("created"),
    }


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


# --- reading pay out of the posting text -------------------------------------
# Used when the source gives no structured salary. Requires a pay-related word
# near the figure, so budget and revenue numbers ("a $2M budget", "$5M in new
# revenue") are not mistaken for compensation.
_AMOUNT = r"\$\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d{1,2})?)\s*([KkMm])?"
TEXT_RANGE_RE = re.compile(
    _AMOUNT + r"\s*(?:-|\u2013|\u2014|to|through)\s*" + _AMOUNT, re.I)
TEXT_SINGLE_RE = re.compile(_AMOUNT)
PAY_CONTEXT_RE = re.compile(
    r"(salary|salaries|compensation|base\s+pay|base\s+salary|pay\s+range|"
    r"pay\s+rate|hiring\s+range|target\s+pay|annual|annually|per\s+year|"
    r"per\s+annum|/\s*yr|a\s+year|per\s+hour|/\s*hr|hourly|an\s+hour|"
    r"total\s+cash|remuneration)", re.I)
PLAUSIBLE_MIN = 30_000
PLAUSIBLE_MAX = 2_000_000


def _amount(num, suffix):
    try:
        val = float(num.replace(",", ""))
    except ValueError:
        return None
    if (suffix or "").upper() == "K":
        val *= 1_000
    elif (suffix or "").upper() == "M":
        val *= 1_000_000
    return val


def _period_near(text):
    low = text.lower()
    if re.search(r"per\s+hour|/\s*hr|hourly|an\s+hour", low):
        return "HOUR"
    if re.search(r"per\s+month|/\s*mo|monthly|a\s+month", low):
        return "MONTH"
    return "YEAR"


def find_salary_in_text(text):
    """Pull a pay range out of a job description. (lo, hi) annualised, or None."""
    if not text:
        return None
    candidates = []
    for match in TEXT_RANGE_RE.finditer(text):
        window = text[max(0, match.start() - 120): match.end() + 120]
        if not PAY_CONTEXT_RE.search(window):
            continue
        lo = _amount(match.group(1), match.group(2))
        hi = _amount(match.group(3), match.group(4))
        if lo is None or hi is None:
            continue
        mult = PERIOD_MULTIPLIER.get(_period_near(window), 1)
        lo, hi = lo * mult, hi * mult
        if lo > hi:
            lo, hi = hi, lo
        if PLAUSIBLE_MIN <= lo and hi <= PLAUSIBLE_MAX:
            candidates.append((round(lo), round(hi)))
    if candidates:
        return min(c[0] for c in candidates), max(c[1] for c in candidates)

    # no range stated - accept a single figure only if pay words sit right on it
    for match in TEXT_SINGLE_RE.finditer(text):
        window = text[max(0, match.start() - 90): match.end() + 90]
        if not PAY_CONTEXT_RE.search(window):
            continue
        val = _amount(match.group(1), match.group(2))
        if val is None:
            continue
        val *= PERIOD_MULTIPLIER.get(_period_near(window), 1)
        if PLAUSIBLE_MIN <= val <= PLAUSIBLE_MAX:
            candidates.append((round(val), round(val)))
    if candidates:
        return min(c[0] for c in candidates), max(c[1] for c in candidates)
    return None


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
    source = "employer" if posted else None
    if not posted:
        found = find_salary_in_text(desc)
        if found:
            lo, hi, cur, source = found[0], found[1], "USD", "text"
    return {
        "id": job.get("job_id"),
        "source": "jsearch",
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
        "salary_source": source,
        "posted_at": job.get("job_posted_at_datetime_utc"),
        "apply_link": (job.get("job_apply_link")
                       or (job.get("apply_options") or [{}])[0].get("apply_link")),
        "publisher": job.get("job_publisher"),
        "snippet": re.sub(r"\s+", " ", desc)[:420],
        "senior": bool(SENIOR_RE.search(title)),
        "salary_estimated": False,
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
    """Each source gets the freshness rule its data can actually support.

    Adzuna dates every listing, so the real 25-hour window is enforced.
    JSearch leaves 40% undated and runs over a week behind, so those are
    admitted on first sighting with only a backstop against ancient posts.
    """
    hours = age_hours(job)
    if job.get("source") == "adzuna":
        if hours is None:
            return True                              # undated: cannot verify, drop it
        return hours > ADZUNA_MAX_AGE_HOURS
    if hours is None:
        return False
    return hours > JSEARCH_MAX_AGE_DAYS * 24


def keep(job):
    """Apply the title, freshness and salary rules. Returns (keep?, reason)."""
    title = job["title"]
    if too_old(job):
        return False, ("older than 25 hours" if job.get("source") == "adzuna"
                       else f"posted over {JSEARCH_MAX_AGE_DAYS} days ago")
    if not TITLE_RE.search(title):
        return False, "wrong title"
    if TITLE_EXCLUDE_RE.search(title):
        return False, "excluded title"
    if COMPANY_EXCLUDE_RE.search(job.get("company") or ""):
        return False, "trade or property employer"
    if job.get("salary_source"):
        top = job["salary_max"] or job["salary_min"] or 0
        if (job["salary_currency"] or "USD").upper() != "USD":
            return False, "not USD"
        if top < SALARY_FLOOR:
            return False, "below $215k"
        return True, "salary meets floor"
    if job["senior"]:
        return True, "no salary found, senior title"
    return False, "no pay, title not senior"

# ---------------------------------------------------------------- run


def run(state, store, include_jsearch, themes):
    sources = ["Adzuna"] + (["JSearch"] if include_jsearch else [])
    log(f"Sources this run: {', '.join(sources)}")
    log(f"Themes: {', '.join(themes)}")

    raw_count = 0
    kept = 0
    added = 0
    ok_searches = 0
    failed_searches = 0
    rejected = {}
    per_source = {}
    seen_now = now_utc().isoformat(timespec="seconds")

    def role_key(job):
        """Same employer and same title = the same role, however many times a
        job board relists it under a fresh id."""
        return (re.sub(r"\W+", " ", (job.get("company") or "")).strip().lower(),
                re.sub(r"\W+", " ", (job.get("title") or "")).strip().lower())

    # index the board so a relisting updates the role already there
    by_role = {role_key(j): jid for jid, j in store.items()}

    def ingest(job):
        nonlocal kept, added
        if not job["id"]:
            return
        ok, reason = keep(job)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            return
        kept += 1
        per_source[job["source"]] = per_source.get(job["source"], 0) + 1
        existing = store.get(job["id"])
        if existing is None:
            twin_id = by_role.get(role_key(job))
            if twin_id and twin_id in store:
                twin = store[twin_id]
                twin["last_seen"] = seen_now
                # keep whichever copy actually states a salary
                if job["salary_posted"] and not twin.get("salary_posted"):
                    for f in ("salary_min", "salary_max", "salary_posted",
                              "salary_estimated", "apply_link"):
                        twin[f] = job[f]
                return
        if existing:
            first_seen = existing.get("first_seen", seen_now)
            existing.update(job)
            existing["first_seen"] = first_seen
            existing["last_seen"] = seen_now
        else:
            job["first_seen"] = seen_now
            job["last_seen"] = seen_now
            store[job["id"]] = job
            by_role[role_key(job)] = job["id"]
            added += 1

    # ---- Adzuna: every day, real 25-hour window -------------------------
    for theme in themes:
        for bucket in ("nyc", "remote"):
            results = adzuna_search(theme, bucket, state)
            if results is None:
                failed_searches += 1
                continue
            ok_searches += 1
            raw_count += len(results)
            for raw in results:
                ingest(normalize_adzuna(raw, bucket, theme))

    # ---- JSearch: weekly sweep for roles Adzuna does not carry ----------
    if include_jsearch:
        api_key = os.environ.get("RAPIDAPI_KEY", "").strip()
        if not api_key:
            log("  WARNING: RAPIDAPI_KEY not set, skipping the JSearch sweep.")
        else:
            for theme in themes:
                for bucket, query, extra in (
                    ("nyc", f"{theme} in New York, NY", {}),
                    ("remote", f"{theme} in United States",
                     {"work_from_home": "true"}),
                ):
                    results = api_search(api_key, query, extra, state)
                    if results is None:
                        failed_searches += 1
                        continue
                    ok_searches += 1
                    raw_count += len(results)
                    for raw in results:
                        ingest(normalize(raw, bucket, theme))

    log(f"{ok_searches} searches succeeded, {failed_searches} failed. "
        f"Scanned {raw_count} listings, {kept} passed the filters, "
        f"{added} brand new.")
    for source, count in sorted(per_source.items()):
        log(f"    kept from {source}: {count}")
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
        "at": seen_now, "themes": themes, "sources": sources,
        "raw": raw_count, "kept": kept, "added": added,
        "rejected": rejected, "searches": ok_searches,
        "per_source": per_source,
    })
    state["runs"] = state["runs"][-60:]
    state["last_run_date"] = f"{datetime.now(ET):%Y-%m-%d}"

    if ok_searches == 0:
        raise RuntimeError(
            f"Every one of the {failed_searches} searches failed - the dashboard "
            f"was not refreshed. See the errors above.")


def refilter(store):
    """Re-apply the title, employer and salary rules to the stored board.

    Freshness is deliberately NOT re-applied. It is an *admission* rule, checked
    against incoming API results at the moment a role is found. Re-running it
    over the board would evict roles that were correctly admitted on an earlier
    day - they are meant to stay for RETAIN_DAYS. Use this after changing the
    title, employer or salary rules; never hand-roll the loop.
    """
    removed = []
    for jid in list(store):
        job = store[jid]
        title = job.get("title") or ""
        reason = None
        if not TITLE_RE.search(title):
            reason = "wrong title"
        elif TITLE_EXCLUDE_RE.search(title):
            reason = "excluded title"
        elif COMPANY_EXCLUDE_RE.search(job.get("company") or ""):
            reason = "trade or property employer"
        elif job.get("salary_source"):
            top = job.get("salary_max") or job.get("salary_min") or 0
            if (job.get("salary_currency") or "USD").upper() != "USD":
                reason = "not USD"
            elif top < SALARY_FLOOR:
                reason = "below $215k"
        elif not job.get("senior"):
            reason = "no pay, title not senior"
        if reason:
            removed.append((title, job.get("company"), reason))
            del store[jid]
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run now, ignore the clock")
    ap.add_argument("--jsearch", action="store_true",
                    help="also run the weekly JSearch sweep now")
    ap.add_argument("--adzuna-only", action="store_true",
                    help="skip the JSearch sweep even if it is due")
    ap.add_argument("--render", action="store_true", help="rebuild HTML only")
    ap.add_argument("--refilter", action="store_true",
                    help="re-apply title/employer/salary rules to the stored "
                         "board (never re-applies the freshness rule)")
    args = ap.parse_args()

    state = load_json(STATE_FILE, {})
    store = load_json(JOBS_FILE, {})

    if args.refilter:
        for title, company, why in refilter(store):
            log(f"  removed: {title[:44]:46} {str(company)[:24]:26} ({why})")
        save_json(JOBS_FILE, store)
        log(f"{len(store)} jobs remain on the board.")

    if not (args.render or args.refilter):
        if not args.force:
            should_run, why_not = due(state)
            if not should_run:
                log(f"Nothing to do - {why_not}. (No API calls spent.)")
                return 0

        weekday = datetime.now(ET).weekday()
        include_jsearch = args.jsearch or weekday == JSEARCH_WEEKDAY
        if args.adzuna_only:
            include_jsearch = False
        try:
            run(state, store, include_jsearch, THEMES)
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
