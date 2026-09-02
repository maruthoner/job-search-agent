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

Runs **once a day at 9:15am New York time**. The free RapidAPI plan allows about
200 requests a month, and 30 runs a month leaves room for **6 requests per run**
(186/month), so each run searches 3 of the 6 title themes across both locations.

The themes rotate daily in two groups of three — product/program/project on
one day, transformation/chief of staff/AI enablement on the next — so every
theme is searched every other day. Because each search looks back 3 days, no posting can slip through between
a theme's turns.

`agent.py` hard-stops at 195 requests in a calendar month, so the plan can never
go into overage.

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
