# Job search agent

Searches the JSearch API (RapidAPI) three times a day and rebuilds an HTML
dashboard of matching roles.

**Dashboard:** see the GitHub Pages link in the repo settings.

## What it looks for

| | |
|---|---|
| Locations | New York, NY (office / hybrid / remote) and United States (remote only) |
| Salary floor | $215,000/year. Hourly, monthly and weekly pay is annualized first. |
| No salary posted | Kept only if the title reads senior (Senior, Principal, Director, Head of, VP, Lead, Chief, Group...) |
| Titles | program manager, product manager, transformation lead and close relatives — technical program manager, director/head of product, group product manager, product owner, PMO, portfolio manager, change management lead, chief of staff |
| Recency | Posted in the last 3 days |
| Job types | Full-time and contract |
| Retention | A job stays on the board for 30 days after it was last seen; anything found in the last 24 hours wears a NEW badge |

## Schedule and API budget

The free RapidAPI plan allows about 200 requests a month. Three runs a day
leaves room for **2 requests per run** (180/month), so each run searches one
title group across both locations, rotating through the day:

| New York time | Title group searched |
|---|---|
| 9:30am | product manager |
| 1:00pm | program manager |
| 5:30pm | transformation lead |

Every group is searched once a day, and the 3-day lookback means nothing is
missed between rotations. `agent.py` also hard-stops at 190 requests in a
calendar month so the plan can never go into overage.

## Files

- `agent.py` — decides whether it is a run window, calls the API, filters, saves
- `render.py` — turns the saved data into `docs/index.html`
- `data/jobs.json` — every job currently on the board
- `data/state.json` — run history and the monthly request counter
- `.github/workflows/jobs.yml` — the schedule

## Running it by hand

On GitHub: **Actions → Job search → Run workflow**, pick a title group.

Locally:

```bash
export RAPIDAPI_KEY=your_key
python3 agent.py --force      # search now
python3 agent.py --render     # rebuild the page only, no API calls
```

## Changing the search

Everything tunable lives at the top of `agent.py`: `SALARY_FLOOR`,
`DATE_POSTED`, `EMPLOYMENT_TYPES`, `RETAIN_DAYS`, `SLOTS` and the title
patterns.
