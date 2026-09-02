#!/usr/bin/env python3
"""
Runs the same six searches against JSearch and Adzuna and reports, for each:
how much comes back, how fresh it really is, and how much survives Ruth's
filters. Read-only - it does not touch the live board.
"""

import json
import os
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import agent

NOW = datetime.now(timezone.utc)
THEMES = agent.THEMES
LOCATIONS = [("New York", "nyc"), ("", "remote")]


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}"
    except Exception as e:                                        # noqa: BLE001
        return None, repr(e)


# ------------------------------------------------------------------ jsearch

def jsearch(theme, bucket):
    key = os.environ["RAPIDAPI_KEY"].strip()
    query = (f"{theme} in New York, NY" if bucket == "nyc"
             else f"{theme} in United States")
    params = {"query": query, "country": "us", "language": "en",
              "employment_types": agent.EMPLOYMENT_TYPES}
    if bucket == "remote":
        params["work_from_home"] = "true"
    url = f"https://{agent.API_HOST}{agent.API_PATH}?" + urllib.parse.urlencode(params)
    payload, err = get(url, {"X-RapidAPI-Key": key,
                             "X-RapidAPI-Host": agent.API_HOST})
    if err:
        return [], err
    raw = (payload.get("data") or {}).get("jobs") or []
    return [agent.normalize(j, bucket, theme) for j in raw], None


# ------------------------------------------------------------------- adzuna

def adzuna(theme, bucket):
    app_id = os.environ["ADZUNA_APP_ID"].strip()
    app_key = os.environ["ADZUNA_APP_KEY"].strip()
    params = {
        "app_id": app_id, "app_key": app_key,
        "results_per_page": "50",
        "what": theme if bucket == "nyc" else f"{theme} remote",
        "sort_by": "date",
        "content-type": "application/json",
    }
    if bucket == "nyc":
        params["where"] = "New York, New York"
    url = ("https://api.adzuna.com/v1/api/jobs/us/search/1?"
           + urllib.parse.urlencode(params))
    payload, err = get(url)
    if err:
        return [], err
    out = []
    for j in payload.get("results") or []:
        predicted = str(j.get("salary_is_predicted", "0")) == "1"
        lo, hi = j.get("salary_min"), j.get("salary_max")
        out.append({
            "id": str(j.get("id")),
            "title": j.get("title") or "",
            "company": (j.get("company") or {}).get("display_name") or "Unknown",
            "location": (j.get("location") or {}).get("display_name") or "",
            "bucket": bucket,
            "theme": theme,
            # a predicted salary is Adzuna's guess, not the employer's - treat as unlisted
            "salary_min": None if predicted else lo,
            "salary_max": None if predicted else hi,
            "salary_currency": "USD",
            "salary_posted": not predicted and (lo is not None or hi is not None),
            "salary_predicted": predicted,
            "posted_at": j.get("created"),
            "employment_type": j.get("contract_time") or j.get("contract_type") or "",
            "is_remote": "remote" in (j.get("title", "") + j.get("description", "")).lower(),
            "senior": bool(agent.SENIOR_RE.search(j.get("title") or "")),
            "snippet": (j.get("description") or "")[:200],
            "apply_link": j.get("redirect_url"),
            "publisher": "Adzuna",
        })
    return out, None


# -------------------------------------------------------------------- report

def age_hours(job):
    s = job.get("posted_at")
    if not s:
        return None
    try:
        return (NOW - datetime.fromisoformat(
            s.replace("Z", "+00:00"))).total_seconds() / 3600
    except ValueError:
        return None


def summarise(name, jobs, errors):
    ages = [age_hours(j) for j in jobs]
    dated = [a for a in ages if a is not None]
    kept = [j for j in jobs if agent.keep(j)[0]]
    fresh25 = [a for a in dated if a <= 25]
    fresh7d = [a for a in dated if a <= 24 * 7]
    print(f"\n{'=' * 74}\n{name}\n{'=' * 74}")
    if errors:
        print(f"  errors: {errors[:2]}")
    print(f"  listings returned          {len(jobs)}")
    print(f"  with a real posting date   {len(dated)}"
          f"  ({len(dated) / len(jobs) * 100:.0f}%)" if jobs else "")
    if dated:
        print(f"  freshest                   {min(dated):.0f} hours old")
        print(f"  median age                 {statistics.median(dated) / 24:.1f} days")
        print(f"  posted within 25 hours     {len(fresh25)}")
        print(f"  posted within 7 days       {len(fresh7d)}")
    print(f"  pass title + salary rules  {len(kept)}")
    with_salary = sum(1 for j in jobs if j.get("salary_posted"))
    print(f"  with an employer salary    {with_salary}")
    if any("salary_predicted" in j for j in jobs):
        pred = sum(1 for j in jobs if j.get("salary_predicted"))
        print(f"  (salary was Adzuna's estimate, ignored: {pred})")
    return kept


def main():
    results = {}
    for label, fetch in (("JSearch", jsearch), ("Adzuna", adzuna)):
        jobs, errors = [], []
        for theme in THEMES:
            for _where, bucket in LOCATIONS:
                got, err = fetch(theme, bucket)
                if err:
                    errors.append(f"{theme}/{bucket}: {err}")
                jobs.extend(got)
        seen, unique = set(), []
        for j in jobs:
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            unique.append(j)
        results[label] = summarise(label, unique, errors)

    a = {(j["company"].lower().strip(), j["title"].lower().strip())
         for j in results["JSearch"]}
    b = {(j["company"].lower().strip(), j["title"].lower().strip())
         for j in results["Adzuna"]}
    print(f"\n{'=' * 74}\nOVERLAP OF QUALIFYING ROLES\n{'=' * 74}")
    print(f"  only JSearch  {len(a - b)}")
    print(f"  only Adzuna   {len(b - a)}")
    print(f"  in both       {len(a & b)}")

    print("\n  Roles only Adzuna found:")
    for company, title in sorted(b - a)[:15]:
        print(f"    {title[:52]:52} {company[:24]}")
    print("\n  Roles only JSearch found:")
    for company, title in sorted(a - b)[:15]:
        print(f"    {title[:52]:52} {company[:24]}")


if __name__ == "__main__":
    main()
