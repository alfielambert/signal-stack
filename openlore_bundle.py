"""
OpenLore memory for Signal Stack.

Each run reads and writes a folder of Markdown that OpenLore serves to every
agent on the team (https://github.com/aakarim/OpenLore). The folder is an Open
Knowledge Format bundle, so `lore validate` passes on it:

  index.md               every company, best score first
  log.md                 one line per run, newest date first
  guide.md               how agents should read and update the bundle
  companies/<slug>.md    one page per company: evidence, ads, score history
  runs/<date>.md         what changed in each run

Signal Stack owns the frontmatter and the generated sections of each company
page. Two things belong to people and agents, and are kept across runs:

  outreach:   in the frontmatter (new, contacted, replied, meeting, customer, not-a-fit)
  ## Notes    everything under that heading
"""

import os
import re
from datetime import datetime

import yaml

OKF_VERSION = "0.2"
NOTES_HEADING = "## Notes"
DEFAULT_IN_PLAY = ["contacted", "replied", "meeting", "customer", "not-a-fit"]
FM_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot make a file name from company {name!r}")
    return slug


def _plain(value):
    """YAML reads an unquoted 2026-11-13 as a date. Keep dates as ISO strings."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = FM_RE.match(text)
    if not m:
        return {}, text
    return _plain(yaml.safe_load(m.group(1)) or {}), m.group(2)


def notes_section(body: str) -> str:
    """The ## Notes section, verbatim, or an empty one for new pages."""
    i = body.find(NOTES_HEADING)
    return body[i:].rstrip() + "\n" if i >= 0 else f"{NOTES_HEADING}\n\n"


def load(bundle_dir: str) -> dict[str, dict]:
    """Existing company pages, keyed by company key: {key: {"meta", "notes", "path"}}."""
    known = {}
    folder = os.path.join(bundle_dir, "companies")
    if not os.path.isdir(folder):
        return known
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(folder, name)
        with open(path, encoding="utf-8") as f:
            meta, body = split_frontmatter(f.read())
        key = meta.get("company_key")
        if not key:
            raise ValueError(f"{path} has no company_key in its frontmatter")
        known[key] = {"meta": meta, "notes": notes_section(body), "path": path}
    return known


def annotate(ranked: list[dict], known: dict[str, dict], in_play: list[str]) -> list[dict]:
    """Mark each company new or returning, carry its outreach state, and split
    out the ones already in play. Returns the companies that are in play."""
    in_play = set(in_play)
    playing = []
    for c in ranked:
        prev = known.get(c["key"])
        c["outreach"] = prev["meta"].get("outreach", "new") if prev else "new"
        c["is_new"] = prev is None
        c["previous_score"] = prev["meta"].get("score") if prev else None
        c["first_seen"] = prev["meta"].get("first_seen") if prev else None
        if c["outreach"] in in_play:
            playing.append(c)
    ranked[:] = [c for c in ranked if c["outreach"] not in in_play]
    return playing


def _sources(prev_meta: dict, ads: list[dict]) -> list[dict]:
    """Every job ad ever seen for the company, as OKF sources."""
    seen = {s["resource"]: s for s in prev_meta.get("sources") or []}
    for ad in ads:
        if ad["url"] and ad["url"] not in seen:
            seen[ad["url"]] = {"resource": ad["url"], "title": ad["title"]}
            if ad["listed_at"]:
                seen[ad["url"]]["last_modified"] = ad["listed_at"]
    return list(seen.values())


def company_page(c: dict, prev: dict | None, run_date: str, now: str) -> tuple[str, str]:
    """Render one company page. Returns (relative path, text)."""
    prev_meta = prev["meta"] if prev else {}
    history = [h for h in prev_meta.get("score_history") or [] if h.get("date") != run_date]
    history.append({"date": run_date, "score": c["score"], "themes": sorted(c["themes"])})

    meta = {
        "type": "company",
        "title": c["company"],
        "company_key": c["key"],
        "linkedin": c["company_url"] or None,
        "industries": c["industries"] or None,
        "staff_count": c["staff_count"] or None,
        "outreach": prev_meta.get("outreach", "new"),
        "score": c["score"],
        "themes": sorted(c["themes"]),
        "first_seen": prev_meta.get("first_seen", run_date),
        "last_seen": run_date,
        "score_history": history,
        "sources": _sources(prev_meta, c["ads"]),
        "generated": {"by": "signal-stack", "at": now},
    }
    meta = {k: v for k, v in meta.items() if v not in (None, [])}

    lines = [f"# {c['company']}", "", f"## Evidence ({run_date})", ""]
    for theme, hit in c["themes"].items():
        lines.append(f"- **{theme}** (\"{hit['phrase']}\" in {hit['ad']}): {hit['evidence']}")
    lines += ["", "## Job ads", ""]
    for s in meta.get("sources", []):
        when = f" ({s['last_modified']})" if s.get("last_modified") else ""
        lines.append(f"- [{s.get('title', s['resource'])}]({s['resource']}){when}")
    if not meta.get("sources"):
        lines.append("- No ad links (demo data)")
    lines += ["", "## Score history", "", "| Run | Score | Themes |", "|---|---|---|"]
    for h in reversed(history):
        lines.append(f"| {h['date']} | {h['score']} | {', '.join(h['themes'])} |")
    lines += ["", prev["notes"] if prev else f"{NOTES_HEADING}\n\n"]

    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"companies/{slugify(c['company'])}.md", f"---\n{front}---\n\n" + "\n".join(lines).rstrip() + "\n"


def run_page(run_date: str, now: str, ranked: list[dict], playing: list[dict],
             dropped: dict, total_jobs: int, slugs: dict[str, str]) -> str:
    new = [c for c in ranked if c["is_new"]]
    rising = [c for c in ranked if not c["is_new"] and c["previous_score"] is not None
              and c["score"] > c["previous_score"]]
    meta = {
        "type": "run",
        "title": f"Signal Stack run {run_date}",
        "ads_checked": total_jobs,
        "companies": len(ranked) + len(playing),
        "new_companies": len(new),
        "generated": {"by": "signal-stack", "at": now},
    }

    def link(c):
        return f"[{c['company']}](../{slugs[c['key']]})"

    lines = [f"# Run {run_date}", "",
             f"{total_jobs} job ads checked, {len(ranked) + len(playing)} companies with signals.", "",
             "Dropped: " + (", ".join(f"{n} {r}" for r, n in sorted(dropped.items())) or "none"), "",
             "## New this run", ""]
    lines += [f"- {link(c)} (score {c['score']})" for c in new] or ["- None"]
    lines += ["", "## Score went up", ""]
    lines += [f"- {link(c)} ({c['previous_score']} to {c['score']})" for c in rising] or ["- None"]
    lines += ["", "## Already in play", ""]
    lines += [f"- {link(c)} ({c['outreach']}, score {c['score']})" for c in playing] or ["- None"]
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True)
    return f"---\n{front}---\n\n" + "\n".join(lines) + "\n"


def index_page(pages: dict[str, dict], runs: list[str]) -> str:
    lines = ["---", f'okf_version: "{OKF_VERSION}"', "title: Signal Stack", "---", "",
             "# Signal Stack", "",
             "Companies whose job ads describe the problem we sell, with the evidence. "
             "Read [guide.md](guide.md) before changing anything.", "",
             "## Companies", ""]
    ordered = sorted(pages.items(), key=lambda kv: (-kv[1].get("score", 0), kv[1]["title"]))
    for path, meta in ordered:
        lines.append(f"- [{meta['title']}]({path}): score {meta.get('score')}, "
                     f"outreach {meta.get('outreach')}, last seen {meta.get('last_seen')}")
    lines += ["", "## Runs", ""]
    lines += [f"- [{r}](runs/{r}.md)" for r in sorted(runs, reverse=True)]
    return "\n".join(lines) + "\n"


GUIDE = """---
type: guide
title: How to use the Signal Stack bundle
---

# How to use the Signal Stack bundle

Signal Stack writes this folder after every run. Each page in `companies/` is
one company whose job ads match our signal themes, with the quotes, the ads
and its score over time.

## Reading it

- Start at [index.md](index.md), sorted by score.
- `runs/` says what changed in each run: new companies, rising scores, and
  companies already in play.
- Query the frontmatter with `lore meta`, for example every company we have
  not contacted yet: `lore meta companies | jq 'select(.outreach == "new")'`.

## Updating it

Signal Stack rewrites everything on a company page except two things, which
belong to you:

- `outreach:` in the frontmatter. Use one of: new, contacted, replied,
  meeting, customer, not-a-fit. Companies at contacted or beyond leave the
  main ranking and show under "Already in play" in the next run.
- Anything under the `## Notes` heading: research, call notes, who to talk to.

Do not edit the other fields by hand. The next run overwrites them.
"""


def update_log(existing: str | None, run_date: str, entry: str) -> str:
    """Add an entry under today's date group, keeping groups newest first."""
    groups: dict[str, list[str]] = {}
    current = None
    for line in (existing or "").splitlines():
        m = re.match(r"^## (\d{4}-\d{2}-\d{2})\s*$", line)
        if m:
            current = m.group(1)
            groups.setdefault(current, [])
        elif current and line.strip():
            groups[current].append(line)
    groups.setdefault(run_date, []).append(entry)
    lines = ["# Signal Stack log", ""]
    for date in sorted(groups, reverse=True):
        lines += [f"## {date}", "", *groups[date], ""]
    return "\n".join(lines)


def _write(path: str, text: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def write(bundle_dir: str, ranked: list[dict], playing: list[dict], known: dict[str, dict],
          dropped: dict, total_jobs: int, today: datetime) -> dict:
    run_date = today.date().isoformat()
    now = datetime.now(today.tzinfo).isoformat(timespec="seconds")
    slugs = {}
    for c in ranked + playing:
        prev = known.get(c["key"])
        path, text = company_page(c, prev, run_date, now)
        if prev:
            # Keep the file a company already has, even if its name has changed
            path = os.path.relpath(prev["path"], bundle_dir).replace(os.sep, "/")
        _write(os.path.join(bundle_dir, path), text)
        slugs[c["key"]] = path

    _write(os.path.join(bundle_dir, "runs", f"{run_date}.md"),
           run_page(run_date, now, ranked, playing, dropped, total_jobs, slugs))

    # The index covers every company ever seen, not only this run's
    pages = {}
    for name in sorted(os.listdir(os.path.join(bundle_dir, "companies"))):
        if name.endswith(".md"):
            with open(os.path.join(bundle_dir, "companies", name), encoding="utf-8") as f:
                pages[f"companies/{name}"] = split_frontmatter(f.read())[0]
    runs = [n[:-3] for n in os.listdir(os.path.join(bundle_dir, "runs")) if n.endswith(".md")]
    _write(os.path.join(bundle_dir, "index.md"), index_page(pages, runs))
    _write(os.path.join(bundle_dir, "guide.md"), GUIDE)

    log_path = os.path.join(bundle_dir, "log.md")
    existing = open(log_path, encoding="utf-8").read() if os.path.exists(log_path) else None
    new_count = sum(1 for c in ranked if c["is_new"])
    entry = (f"- Run: {total_jobs} ads, {len(ranked) + len(playing)} companies, {new_count} new. "
             f"[Details](runs/{run_date}.md)")
    _write(log_path, update_log(existing, run_date, entry))
    return slugs
