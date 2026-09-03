# Job search agent

Searches the JSearch API (RapidAPI) once a day and rebuilds an HTML
dashboard of matching roles.

**Dashboard:** see the GitHub Pages link in the repo settings.

## What it looks for

| | |
|---|---|
| Locations | New York, NY (office / hybrid / remote) and United States (remote only) |
| Salary floor | $215,000/year. Hourly, monthly and weekly pay is annualized first. |
| No salary posted | Kept only if the title reads senior (Senior, Principal, Director, Head of, VP, Lead, Chief, Group...) |
| Titles | product management, program management, project management, transformation, chief of staff, AI enablement — plus close relatives: director or head of product, group product manager, product owner, PMO, portfolio manager, delivery lead, change management lead, AI adoption/readiness |
| Titles excluded | anything containing **technical** (which also rules out Technical Program Manager, Technical Product Manager and TPM), **construction**, or **clinical** — plus intern, coordinator, junior, entry-level, associate product manager, trainee |
| Recency | Posted in the last 3 days |
| Job types | Full-time and contract |
| Retention | A job stays on the board for 30 days after it was last seen; anything found in the last 36 hours wears a NEW badge |

## Schedule and API budget

Aims for **once a day, from 9:15am New York time**.

GitHub's cron scheduler is not reliable - it delays firings and sometimes drops
them entirely, which is exactly what happened on 3 September 2026 when no run
occurred at all. So the workflow is scheduled **six times a day** and the script
decides whether to act:

> If today's run has not happened yet, and it is past 9:15am in New York, run now.

The first firing GitHub actually delivers does the work. Every later one exits in
about ten seconds having spent no API calls. A late run is always better than no
run, so there is no cut-off time.

| Source | Cadence | Requests | Freshness rule |
|---|---|---|---|
| Adzuna | every day | 12 (free tier allows hundreds/day) | posted in the last 25 hours, date verified |
| JSearch | Mondays only | 12 of the 200/month | admitted on first sighting; rejected if known to be over 30 days old |

JSearch costs roughly 48-60 requests a month, well inside its 200 limit. The
remaining allowance covers manual runs. `agent.py` reads the true remaining
quota from RapidAPI's own response headers rather than counting locally.

## Files

- `agent.py` — decides whether it is a run window, calls the API, filters, saves
- `render.py` — turns the saved data into `docs/index.html`
- `data/jobs.json` — every job currently on the board
- `data/state.json` — run history and the monthly request counter
- `.github/workflows/jobs.yml` — the schedule

## Running it by hand

On GitHub: **Actions → Job search → Run workflow**. Tick *all six themes* for a
wider sweep (costs 10 requests instead of 6).

Locally:

```bash
export RAPIDAPI_KEY=your_key
python3 agent.py --force      # search now, today's 3 themes
python3 agent.py --all        # search all six themes
python3 agent.py --render     # rebuild the page only, no API calls
```

## Changing the search

Everything tunable lives at the top of `agent.py`: `SALARY_FLOOR`,
`DATE_POSTED`, `EMPLOYMENT_TYPES`, `RETAIN_DAYS`, `RUN_AT`, `THEMES` and the
title patterns.
