#!/usr/bin/env python3
"""Builds docs/index.html from the saved job store."""

import html
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NEW_FOR_HOURS = 36
SALARY_FLOOR = 215_000


def _money(n):
    return f"${n:,.0f}"


def salary_text(job):
    if not job.get("salary_posted"):
        return "Salary not listed"
    lo, hi = job.get("salary_min"), job.get("salary_max")
    if lo and hi and lo != hi:
        return f"{_money(lo)} – {_money(hi)}"
    return _money(hi or lo)


def relative(iso):
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - then
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return "just now"
    if hours < 24:
        return f"{int(hours)}h ago"
    days = int(hours // 24)
    return "yesterday" if days == 1 else f"{days} days ago"


def render_dashboard(store, state, out_path):
    now_utc = datetime.now(timezone.utc)
    new_cutoff = now_utc - timedelta(hours=NEW_FOR_HOURS)

    jobs = []
    for job in store.values():
        try:
            first_seen = datetime.fromisoformat(job["first_seen"])
        except (KeyError, ValueError):
            first_seen = now_utc
        item = dict(job)
        item["is_new"] = first_seen >= new_cutoff
        item["salary_text"] = salary_text(job)
        item["meets_floor"] = bool(
            job.get("salary_posted")
            and (job.get("salary_max") or job.get("salary_min") or 0) >= SALARY_FLOOR)
        item["sort_salary"] = (job.get("salary_max") or job.get("salary_min") or 0)
        item["posted_rel"] = relative(job.get("posted_at"))
        age_d = age_h = None
        if job.get("posted_at"):
            try:
                posted = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
                age_h = (now_utc - posted).total_seconds() / 3600
                age_d = (now_utc - posted).days
            except ValueError:
                age_d = age_h = None
        item["age_days"] = age_d
        item["age_hours"] = None if age_d is None else round(age_h, 1)
        item["age_label"] = ("Posted date unknown" if age_d is None
                             else f"Posted {item['posted_rel']}")
        item["age_stale"] = age_d is not None and age_d >= 7
        item["sources"] = {"adzuna": "Adzuna", "jsearch": "JSearch"}.get(
            job.get("source", ""), "")
        item["found_rel"] = relative(job.get("first_seen"))
        jobs.append(item)

    # The same posting often comes back from both the New York and the remote
    # search under different ids. Show it once, marked as both.
    merged = {}
    for item in jobs:
        key = (item["company"].strip().lower(), item["title"].strip().lower())
        prior = merged.get(key)
        if prior is None:
            merged[key] = item
            continue
        if item["bucket"] != prior["bucket"]:
            prior["both_locations"] = True
        if item["sources"] and item["sources"] not in prior["sources"]:
            prior["sources"] = f"{prior['sources']} + {item['sources']}"
        # keep whichever copy actually carries a salary
        if item["meets_floor"] and not prior["meets_floor"]:
            item["both_locations"] = prior.get("both_locations", False)
            merged[key] = item
    jobs = list(merged.values())

    jobs.sort(key=lambda j: (not j["is_new"], -(j["sort_salary"] or 0),
                             j.get("first_seen") or ""), reverse=False)

    total = len(jobs)
    n_new = sum(1 for j in jobs if j["is_new"])
    n_floor = sum(1 for j in jobs if j["meets_floor"])
    n_unlisted = total - n_floor
    n_nyc = sum(1 for j in jobs if j["bucket"] == "nyc")
    n_remote = sum(1 for j in jobs if j["bucket"] == "remote")
    n_fresh = sum(1 for j in jobs
                  if j["age_hours"] is not None and j["age_hours"] <= 25)

    runs = (state or {}).get("runs", [])
    last_run = runs[-1]["at"] if runs else None
    last_run_txt = "never"
    if last_run:
        last_run_txt = datetime.fromisoformat(last_run).astimezone(ET).strftime(
            "%b %-d, %Y at %-I:%M %p ET")
    month = f"{datetime.now(ET):%Y-%m}"
    used = (state or {}).get("requests", {}).get(month, 0)
    quota = (state or {}).get("quota") or {}
    quota_txt = (f"{quota['remaining']} of {quota.get('limit', 200)} API requests left"
                 if "remaining" in quota else f"{used} API requests used this month")
    last = runs[-1] if runs else {}
    rejected = last.get("rejected") or {}
    if last:
        parts = [f"scanned <b>{last.get('raw', 0)}</b> listings across "
                 f"{last.get('searches', 0)} searches"]
        if rejected:
            bits = ", ".join(f"{c} {r}" for r, c in
                             sorted(rejected.items(), key=lambda kv: -kv[1]))
            parts.append(f"set aside {bits}")
        parts.append(f"added <b>{last.get('added', 0)}</b> new")
        funnel = "Last run " + " &middot; ".join(parts) + "."
    else:
        funnel = "No run has completed yet."
    last_themes = last.get("themes") if runs else None
    themes_txt = (", ".join(last_themes) if last_themes
                  else "product, program, project, transformation, chief of staff and AI enablement roles")

    payload = json.dumps(jobs, ensure_ascii=False)

    doc = TEMPLATE.format(
        payload=payload,
        total=total, n_new=n_new, n_floor=n_floor, n_unlisted=n_unlisted,
        n_nyc=n_nyc, n_remote=n_remote, n_fresh=n_fresh,
        last_run=html.escape(last_run_txt),
        quota_txt=html.escape(quota_txt),
        funnel=funnel,
        themes_txt=html.escape(themes_txt),
        generated=datetime.now(ET).strftime("%b %-d, %Y at %-I:%M %p ET"),
    )
    with open(out_path, "w") as fh:
        fh.write(doc)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noarchive">
<title>Job Board</title>
<style>
:root {{
  --bg:#faf9f7; --surface:#ffffff; --surface-2:#f3f1ed;
  --text:#1b1a18; --muted:#6d6862; --faint:#a09a92;
  --border:#e5e1db; --accent:#0f6b5c; --accent-soft:#e2f1ed;
  --new:#b4541a; --new-soft:#fdece1;
  --shadow:0 1px 2px rgba(28,26,24,.06), 0 8px 24px -12px rgba(28,26,24,.14);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg:#141618; --surface:#1c1f22; --surface-2:#24282c;
    --text:#eceae6; --muted:#9d9891; --faint:#726d67;
    --border:#2c3135; --accent:#5ecfb8; --accent-soft:#123430;
    --new:#f0a373; --new-soft:#3a2314;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 8px 24px -12px rgba(0,0,0,.6);
  }}
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 ui-sans-serif,-apple-system,"SF Pro Text","Segoe UI",Inter,system-ui,sans-serif;
  -webkit-font-smoothing:antialiased;
}}
.wrap {{ max-width:940px; margin:0 auto; padding:36px 20px 80px; }}

header.top {{ margin-bottom:26px; }}
h1 {{
  font-size:27px; line-height:1.15; letter-spacing:-.02em;
  margin:0 0 6px; font-weight:640;
}}
.sub {{ color:var(--muted); font-size:13.5px; margin:0; }}
.sub b {{ color:var(--text); font-weight:560; }}

.tiles {{
  display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin:22px 0 20px;
}}
.tile {{
  background:var(--surface); border:1px solid var(--border); border-radius:12px;
  padding:13px 14px 12px;
}}
.tile .n {{ font-size:25px; font-weight:660; letter-spacing:-.02em; line-height:1.1; }}
.tile .l {{ font-size:11.5px; color:var(--muted); margin-top:3px;
  text-transform:uppercase; letter-spacing:.06em; }}
.tile.hl .n {{ color:var(--new); }}

.funnel {{
  margin:-6px 0 16px; font-size:12.5px; color:var(--muted);
  background:var(--surface-2); border:1px solid var(--border);
  border-radius:9px; padding:9px 12px; line-height:1.5;
}}
.funnel b {{ color:var(--text); font-weight:600; }}

.controls {{
  display:flex; flex-wrap:wrap; gap:7px; align-items:center;
  padding:12px 0 16px; border-bottom:1px solid var(--border); margin-bottom:18px;
}}
.chip {{
  appearance:none; cursor:pointer; font:inherit; font-size:13px;
  background:var(--surface); color:var(--muted);
  border:1px solid var(--border); border-radius:999px; padding:5px 12px;
  transition:background .12s,color .12s,border-color .12s;
}}
.chip:hover {{ color:var(--text); }}
.chip[aria-pressed="true"] {{
  background:var(--accent); border-color:var(--accent); color:#fff; font-weight:520;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) .chip[aria-pressed="true"] {{ color:#0d1f1c; }}
}}
.spacer {{ flex:1 1 auto; }}
select {{
  font:inherit; font-size:13px; color:var(--text); background:var(--surface);
  border:1px solid var(--border); border-radius:8px; padding:5px 8px;
}}

.list {{ display:flex; flex-direction:column; gap:12px; }}

.card {{
  background:var(--surface); border:1px solid var(--border); border-radius:14px;
  padding:16px 18px; box-shadow:var(--shadow); position:relative;
}}
.card.new {{ border-color:color-mix(in srgb, var(--new) 45%, var(--border)); }}
.card-head {{ display:flex; gap:14px; align-items:flex-start; }}
.logo {{
  width:38px; height:38px; border-radius:9px; object-fit:contain; flex:0 0 auto;
  background:var(--surface-2); border:1px solid var(--border); padding:3px;
}}
.logo.ph {{
  display:flex; align-items:center; justify-content:center;
  font-weight:640; font-size:15px; color:var(--muted);
}}
.headtext {{ flex:1 1 auto; min-width:0; }}
.title {{
  font-size:16.5px; font-weight:610; letter-spacing:-.01em; margin:0 0 2px;
  line-height:1.3;
}}
.title a {{ color:inherit; text-decoration:none; }}
.title a:hover {{ text-decoration:underline; text-underline-offset:2px; }}
.company {{ font-size:13.5px; color:var(--muted); }}
.company b {{ color:var(--text); font-weight:540; }}

.badges {{ display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }}
.badge {{
  font-size:11.5px; letter-spacing:.02em; padding:3px 9px; border-radius:999px;
  background:var(--surface-2); color:var(--muted); border:1px solid var(--border);
  white-space:nowrap;
}}
.badge.sal {{ background:var(--accent-soft); color:var(--accent);
  border-color:color-mix(in srgb, var(--accent) 30%, transparent); font-weight:580; }}
.badge.sal.unlisted {{ background:var(--surface-2); color:var(--muted);
  border-color:var(--border); font-weight:450; }}
.badge.age.stale {{ background:var(--new-soft); color:var(--new);
  border-color:color-mix(in srgb, var(--new) 30%, transparent); font-weight:560; }}
.badge.age.unknown {{ font-style:italic; }}
.badge.src {{ font-size:10.5px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--faint); }}
.badge.newb {{ background:var(--new-soft); color:var(--new);
  border-color:color-mix(in srgb, var(--new) 35%, transparent); font-weight:600;
  letter-spacing:.07em; text-transform:uppercase; font-size:10.5px; }}

.snippet {{
  margin:11px 0 0; font-size:13.5px; color:var(--muted); line-height:1.6;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical;
  overflow:hidden;
}}
.card.open .snippet {{ -webkit-line-clamp:unset; }}

.foot {{
  display:flex; align-items:center; gap:14px; margin-top:13px;
  padding-top:12px; border-top:1px solid var(--border); flex-wrap:wrap;
}}
.apply {{
  font-size:13px; font-weight:560; text-decoration:none; padding:6px 14px;
  border-radius:8px; background:var(--accent); color:#fff;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) .apply {{ color:#0d1f1c; }}
}}
.apply:hover {{ filter:brightness(1.07); }}
.more {{
  appearance:none; background:none; border:none; cursor:pointer; font:inherit;
  font-size:13px; color:var(--muted); padding:0; text-decoration:underline;
  text-underline-offset:2px;
}}
.more:hover {{ color:var(--text); }}
.meta {{ font-size:12.5px; color:var(--faint); margin-left:auto; }}

.empty {{
  text-align:center; padding:60px 20px; color:var(--muted);
  border:1px dashed var(--border); border-radius:14px;
}}
.empty h3 {{ margin:0 0 6px; font-size:16px; color:var(--text); font-weight:600; }}

footer {{
  margin-top:40px; padding-top:18px; border-top:1px solid var(--border);
  font-size:12.5px; color:var(--faint); line-height:1.7;
}}

@media (max-width:640px) {{
  .wrap {{ padding:24px 14px 60px; }}
  .tiles {{ grid-template-columns:repeat(2,1fr); }}
  h1 {{ font-size:23px; }}
  .meta {{ margin-left:0; width:100%; }}
}}
</style>
</head>
<body>
<div class="wrap">

<header class="top">
  <h1>Job Board</h1>
  <p class="sub">Product &middot; Program &middot; Project &middot; Transformation &middot;
     Chief of Staff &middot; AI Enablement<br>
     New York &amp; US-remote &nbsp;|&nbsp; $215k+ &nbsp;|&nbsp;
     last run <b>{last_run}</b></p>
</header>

<div class="tiles">
  <div class="tile"><div class="n">{total}</div><div class="l">On the board</div></div>
  <div class="tile hl"><div class="n">{n_new}</div><div class="l">New to you</div></div>
  <div class="tile"><div class="n">{n_floor}</div><div class="l">$215k+ listed</div></div>
  <div class="tile"><div class="n">{n_unlisted}</div><div class="l">Pay not listed</div></div>
</div>

<p class="funnel">{funnel}</p>

<div class="controls">
  <button class="chip" data-filter="all" aria-pressed="true">All {total}</button>
  <button class="chip" data-filter="new" aria-pressed="false">New {n_new}</button>
  <button class="chip" data-filter="nyc" aria-pressed="false">New York {n_nyc}</button>
  <button class="chip" data-filter="remote" aria-pressed="false">Remote {n_remote}</button>
  <button class="chip" data-filter="floor" aria-pressed="false">$215k+ {n_floor}</button>
  <button class="chip" data-filter="fresh" aria-pressed="false">Posted in last 25h {n_fresh}</button>
  <span class="spacer"></span>
  <select id="sort">
    <option value="new">Newest first</option>
    <option value="salary">Highest salary</option>
    <option value="posted">Newest posting date</option>
    <option value="company">Company A–Z</option>
  </select>
</div>

<div class="list" id="list"></div>

<footer>
  Built {generated} &middot; {quota_txt}.<br>
  Searches run once a day at 9:15am ET across {themes_txt}.
  <b>Adzuna</b> runs daily and only admits roles posted in the last 25 hours &mdash;
  every one carries a verified posting date. <b>JSearch</b> sweeps each Monday for
  large-employer roles Adzuna does not carry; its index runs over a week behind and
  leaves many listings undated, so those are admitted on first sighting and show
  their real age where known. Salary estimates are never allowed to satisfy the
  $215k floor &mdash; only figures stated by the employer. Admitted roles stay
  for 30 days. Full-time
  and contract, New York City or US-remote. Roles with no posted salary are included
  only when the title reads senior &mdash; verify the pay before applying. Titles
  containing <em>technical</em>, <em>construction</em> or <em>clinical</em> are
  filtered out.
</footer>

</div>
<script>
const JOBS = {payload};
const list = document.getElementById('list');
let filter = 'all', sort = 'new';

function esc(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

function matches(j) {{
  if (filter === 'new') return j.is_new;
  if (filter === 'nyc') return j.bucket === 'nyc';
  if (filter === 'remote') return j.bucket === 'remote' || j.is_remote;
  if (filter === 'floor') return j.meets_floor;
  if (filter === 'fresh') return j.age_hours !== null && j.age_hours <= 25;
  return true;
}}

function ordered(items) {{
  const c = items.slice();
  if (sort === 'salary') c.sort((a, b) => (b.sort_salary || 0) - (a.sort_salary || 0));
  else if (sort === 'company') c.sort((a, b) => a.company.localeCompare(b.company));
  else if (sort === 'posted') c.sort((a, b) => {{
    if (a.age_days === null && b.age_days === null) return 0;
    if (a.age_days === null) return 1;
    if (b.age_days === null) return -1;
    return a.age_days - b.age_days;
  }});
  else c.sort((a, b) => (b.is_new - a.is_new)
      || String(b.first_seen).localeCompare(String(a.first_seen)));
  return c;
}}

function card(j) {{
  const logo = j.logo
    ? `<img class="logo" src="${{esc(j.logo)}}" alt="" loading="lazy"
        onerror="this.replaceWith(Object.assign(document.createElement('div'),
        {{className:'logo ph',textContent:'${{esc((j.company||'?')[0]).toUpperCase()}}'}}))">`
    : `<div class="logo ph">${{esc((j.company || '?')[0]).toUpperCase()}}</div>`;

  const badges = [
    `<span class="badge sal${{j.meets_floor ? '' : ' unlisted'}}">${{esc(j.salary_text)}}</span>`,
    j.is_new ? '<span class="badge newb">New to you</span>' : '',
    `<span class="badge age${{j.age_stale ? ' stale' : ''}}${{j.age_days === null ? ' unknown' : ''}}">${{esc(j.age_label)}}</span>`,
    `<span class="badge">${{esc(j.location)}}</span>`,
    j.both_locations ? '<span class="badge">New York + Remote</span>'
      : (j.is_remote ? '<span class="badge">Remote</span>' : ''),

    j.employment_type ? `<span class="badge">${{esc(j.employment_type)}}</span>` : '',
    j.sources ? `<span class="badge src">${{esc(j.sources)}}</span>` : '',
  ].filter(Boolean).join('');

  const meta = [j.posted_rel ? 'posted ' + esc(j.posted_rel) : '',
                j.publisher ? 'via ' + esc(j.publisher) : ''].filter(Boolean).join(' &middot; ');

  return `<article class="card${{j.is_new ? ' new' : ''}}">
    <div class="card-head">
      ${{logo}}
      <div class="headtext">
        <h2 class="title"><a href="${{esc(j.apply_link)}}" target="_blank"
          rel="noopener noreferrer">${{esc(j.title)}}</a></h2>
        <div class="company"><b>${{esc(j.company)}}</b></div>
        <div class="badges">${{badges}}</div>
      </div>
    </div>
    ${{j.snippet ? `<p class="snippet">${{esc(j.snippet)}}</p>` : ''}}
    <div class="foot">
      <a class="apply" href="${{esc(j.apply_link)}}" target="_blank"
         rel="noopener noreferrer">View &amp; apply</a>
      ${{j.snippet ? '<button class="more">Show more</button>' : ''}}
      <span class="meta">${{meta}}</span>
    </div>
  </article>`;
}}

function draw() {{
  const items = ordered(JOBS.filter(matches));
  list.innerHTML = items.length
    ? items.map(card).join('')
    : `<div class="empty"><h3>Nothing on the board yet</h3>
       <p>No jobs match this filter. New roles appear here the first run after
       this job source picks them up.</p>
       <p>The line above shows exactly what the last run scanned and why each
       listing was set aside. The next search runs at 9:15am ET.</p></div>`;
}}

document.querySelectorAll('.chip').forEach(btn => {{
  btn.addEventListener('click', () => {{
    filter = btn.dataset.filter;
    document.querySelectorAll('.chip').forEach(b =>
      b.setAttribute('aria-pressed', String(b === btn)));
    draw();
  }});
}});
document.getElementById('sort').addEventListener('change', e => {{
  sort = e.target.value; draw();
}});
list.addEventListener('click', e => {{
  const btn = e.target.closest('.more');
  if (!btn) return;
  const c = btn.closest('.card');
  c.classList.toggle('open');
  btn.textContent = c.classList.contains('open') ? 'Show less' : 'Show more';
}});

draw();
</script>
</body>
</html>
"""
