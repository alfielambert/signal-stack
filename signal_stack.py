#!/usr/bin/env python3
"""
Signal Stack
Finds companies whose job ads describe the problem you solve, and shows the
evidence for each one.

Pipeline:
  search  search LinkedIn jobs for each query (Lix API)
  enrich  pull the full ad and company details for each job
  score   match your signal phrases, drop recruiters, rank companies with evidence
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

import requests

log = logging.getLogger("signal_stack")

API_BASE = "https://api.lix-it.com"
RATE_LIMIT_SLEEP = 3.5
EVIDENCE_WINDOW = 140  # characters either side of a matched phrase
COMPANY_KEY = "com.linkedin.voyager.deco.jobs.web.shared.WebJobPostingCompany"


class OutOfCredits(Exception):
    pass


# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    cfg["api_key"] = os.environ.get("LIX_API_KEY") or cfg.get("api_key")
    if not cfg.get("signals"):
        sys.exit("Config needs a 'signals' section: theme name -> list of phrases.")
    if not cfg.get("searches"):
        sys.exit("Config needs a 'searches' list of job search queries.")
    cfg.setdefault("output_dir", "./output")
    os.makedirs(cfg["output_dir"], exist_ok=True)
    return cfg


def require_api_key(cfg: dict):
    if not cfg.get("api_key"):
        sys.exit("No Lix API key. Set LIX_API_KEY or add api_key to your config.")


def setup_logging(output_dir: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(output_dir, "run.log"), mode="a"),
        ],
    )


# ─────────────────────────────────────────────
# Lix API
# ─────────────────────────────────────────────

class LixClient:
    def __init__(self, api_key: str, max_requests: int):
        self.api_key = api_key
        self.max_requests = max_requests
        self.calls = 0

    def get(self, endpoint: str, params: dict) -> dict | None:
        if self.calls >= self.max_requests:
            raise OutOfCredits(f"Hit max_requests cap ({self.max_requests})")
        self.calls += 1
        time.sleep(RATE_LIMIT_SLEEP)
        resp = requests.get(
            f"{API_BASE}{endpoint}",
            headers={"Authorization": self.api_key},
            params=params,
            timeout=30,
        )
        if resp.status_code in (401, 402, 403):
            # Out of credits or bad key: every later call fails too, so stop now
            # instead of leaving holes in the data.
            raise OutOfCredits(f"{resp.status_code} from {endpoint}: {resp.text[:200]}")
        if not resp.ok:
            log.error(f"{resp.status_code} from {endpoint}: {resp.text[:200]}")
            return None
        return resp.json()


# ─────────────────────────────────────────────
# Step 1: search
# ─────────────────────────────────────────────

def build_search_url(query: str, location: str, days_back: int) -> str:
    url = f"https://www.linkedin.com/jobs/search/?keywords={quote(query, safe='')}"
    if location:
        url += f"&location={quote(location, safe='')}"
    return url + f"&f_TPR=r{days_back * 86400}&sortBy=DD"


def parse_search_page(data: dict, query: str) -> list[dict]:
    jobs = []
    for job in data.get("jobPostings") or []:
        org = job.get("organisation") or {}
        jobs.append({
            "job_id": str(job.get("id", "")),
            "title": (job.get("title") or "").strip(),
            "company": (org.get("name") or "").strip(),
            "location": job.get("location") or "",
            "date_posted": job.get("datePosted") or "",
            "query": query,
        })
    return [j for j in jobs if j["job_id"]]


def run_search(cfg: dict, client: LixClient) -> list[dict]:
    seen, jobs = set(), []
    for query in cfg["searches"]:
        url = build_search_url(query, cfg.get("location", ""), cfg.get("days_back", 30))
        sequence_id, start = None, 0
        for page in range(cfg.get("pages_per_search", 2)):
            # Paging goes in the LinkedIn URL. A separate start param is ignored
            # and silently returns page 1 again.
            params = {"url": url + (f"&start={start}" if start else "")}
            if sequence_id:
                params["sequence_id"] = sequence_id
            data = client.get("/v1/li/linkedin/search/jobs", params)
            if not data:
                break
            sequence_id = (data.get("meta") or {}).get("sequenceId") or sequence_id
            page_jobs = parse_search_page(data, query)
            new = [j for j in page_jobs if j["job_id"] not in seen]
            seen.update(j["job_id"] for j in new)
            jobs.extend(new)
            log.info(f'"{query}" page {page + 1}: {len(page_jobs)} jobs, {len(new)} new')
            page_size = (data.get("paging") or {}).get("count") or 25
            start += page_size
            if len(page_jobs) < page_size or not new:
                break

    path = os.path.join(cfg["output_dir"], "jobs_raw.json")
    with open(path, "w") as f:
        json.dump(jobs, f, indent=2)
    log.info(f"Search done: {len(jobs)} unique jobs -> {path}")
    return jobs


# ─────────────────────────────────────────────
# Step 2: enrich
# ─────────────────────────────────────────────

def parse_enriched_job(data: dict) -> dict:
    """Flatten a /v1/enrich/job response into the fields we score on."""
    posting = data.get("jobPosting") or {}
    company = ((posting.get("companyDetails") or {}).get(COMPANY_KEY) or {}).get(
        "companyResolutionResult"
    ) or {}
    listed_ms = posting.get("listedAt") or posting.get("originalListedAt")
    return {
        "job_id": str(posting.get("jobPostingId") or ""),
        "title": posting.get("title") or "",
        "location": posting.get("formattedLocation") or "",
        "listed_at": (
            datetime.fromtimestamp(int(listed_ms) / 1000, tz=timezone.utc).date().isoformat()
            if listed_ms else ""
        ),
        "description": (posting.get("description") or {}).get("text") or "",
        "company": company.get("name") or "",
        "company_url": company.get("url") or "",
        "company_id": (company.get("entityUrn") or "").split(":")[-1],
        "industries": company.get("industries") or [],
        # staffCount is sometimes junk (one trading platform comes back as 1 with a range of
        # 1001-5000), so never report fewer than the bottom of the range.
        "staff_count": max(
            company.get("staffCount") or 0,
            (company.get("staffCountRange") or {}).get("start") or 0,
        ),
    }


def run_enrich(cfg: dict, client: LixClient, jobs: list[dict]) -> list[dict]:
    """Enrich each job once. Results append to a JSONL file so a rerun resumes."""
    path = os.path.join(cfg["output_dir"], "jobs_enriched.jsonl")
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                row = json.loads(line)
                done[row["job_id"]] = row

    per_company = defaultdict(int)
    for row in done.values():
        per_company[row["company"].lower()] += 1

    max_per_company = cfg.get("max_ads_per_company", 3)
    todo = [j for j in jobs if j["job_id"] not in done]
    log.info(f"Enrich: {len(done)} already done, {len(todo)} to go")

    with open(path, "a") as f:
        for i, job in enumerate(todo, 1):
            key = job["company"].lower()
            if per_company[key] >= max_per_company:
                continue
            data = client.get("/v1/enrich/job", {"job_id": job["job_id"]})
            if not data:
                continue
            row = parse_enriched_job(data)
            row["job_id"] = row["job_id"] or job["job_id"]
            row["query"] = job["query"]
            f.write(json.dumps(row) + "\n")
            f.flush()
            done[row["job_id"]] = row
            per_company[key] += 1
            log.info(f"  [{i}/{len(todo)}] {row['company']}: {row['title']}")

    return list(done.values())


# ─────────────────────────────────────────────
# Step 3: score
# ─────────────────────────────────────────────

STRONG_WEIGHT, WEAK_WEIGHT = 3, 1


def phrase_pattern(phrase: str) -> re.Pattern:
    """Whole-word, case-insensitive match that also survives LinkedIn's joined
    bullet points ("semantic retrievalKnowledge graphsBuilding ...").
    A lowercase letter running into a capital counts as a word boundary."""
    start = r"(?:(?<![A-Za-z0-9])|(?<=[a-z])(?=[A-Z]))"
    end = r"(?![a-z0-9])"
    return re.compile(rf"{start}(?i:{re.escape(phrase)}){end}")


def compile_signals(signals: dict) -> dict[str, list[tuple[str, re.Pattern, int]]]:
    """Theme -> phrases, strong first. Each theme is {"strong": [...], "weak": [...]}."""
    compiled = {}
    for theme, tiers in signals.items():
        if not isinstance(tiers, dict) or not (tiers.get("strong") or tiers.get("weak")):
            sys.exit(f'Signal theme "{theme}" needs "strong" and/or "weak" phrase lists.')
        compiled[theme] = [
            (p, phrase_pattern(p), weight)
            for tier, weight in (("strong", STRONG_WEIGHT), ("weak", WEAK_WEIGHT))
            for p in tiers.get(tier, [])
        ]
    return compiled


def evidence_snippet(text: str, start: int, end: int) -> str:
    lo, hi = max(0, start - EVIDENCE_WINDOW), min(len(text), end + EVIDENCE_WINDOW)
    snippet = " ".join(text[lo:hi].split())
    return ("..." if lo else "") + snippet + ("..." if hi < len(text) else "")


def match_signals(text: str, compiled: dict) -> dict[str, dict]:
    """Return the strongest match per theme: {theme: {phrase, weight, evidence}}."""
    hits = {}
    for theme, patterns in compiled.items():
        for phrase, pattern, weight in patterns:
            m = pattern.search(text)
            if m:
                hits[theme] = {
                    "phrase": phrase,
                    "weight": weight,
                    "evidence": evidence_snippet(text, m.start(), m.end()),
                }
                break
    return hits


def is_excluded(job: dict, cfg: dict) -> str:
    """Return the reason a job is dropped, or '' if it stays."""
    excluded = {i.lower() for i in cfg.get("exclude_industries", [])}
    if any(i.lower() in excluded for i in job["industries"]):
        return "recruiter or excluded industry"
    min_staff = cfg.get("min_staff", 0)
    if job["staff_count"] and job["staff_count"] < min_staff:
        return f"under {min_staff} staff"
    if not job["description"]:
        return "no description"
    return ""


def days_since(iso_date: str, today: datetime) -> int | None:
    if not iso_date:
        return None
    return (today.date() - datetime.fromisoformat(iso_date).date()).days


def score_companies(cfg: dict, jobs: list[dict], today: datetime) -> tuple[list[dict], dict]:
    compiled = compile_signals(cfg["signals"])
    recent_days = cfg.get("recent_days", 14)
    companies: dict[str, dict] = {}
    dropped = defaultdict(int)

    for job in jobs:
        reason = is_excluded(job, cfg)
        if reason:
            dropped[reason] += 1
            continue
        hits = match_signals(job["description"], compiled)
        if not hits:
            dropped["no signal phrases"] += 1
            continue

        key = job["company_id"] or job["company"].lower()
        c = companies.setdefault(key, {
            "company": job["company"],
            "company_url": job["company_url"],
            "industries": ", ".join(job["industries"]),
            "staff_count": job["staff_count"],
            "themes": {},
            "ads": [],
        })
        age = days_since(job["listed_at"], today)
        c["ads"].append({
            "title": job["title"],
            # Demo ads carry no real LinkedIn ID, so they get no link
            "url": f"https://www.linkedin.com/jobs/view/{job['job_id']}/" if job["job_id"].isdigit() else "",
            "listed_at": job["listed_at"],
            "days_old": age,
        })
        for theme, hit in hits.items():
            # Keep the strongest evidence for each theme across all the company's ads
            current = c["themes"].get(theme)
            if not current or hit["weight"] > current["weight"]:
                c["themes"][theme] = {**hit, "ad": job["title"]}

    # One theme on its own is usually a buzzword ("release governance" from a
    # recruiter ad), so a company needs several to count.
    min_themes = cfg.get("min_themes", 2)
    ranked = []
    for c in companies.values():
        if len(c["themes"]) < min_themes:
            dropped[f"fewer than {min_themes} themes"] += 1
            continue
        ages = [a["days_old"] for a in c["ads"] if a["days_old"] is not None]
        newest = min(ages) if ages else None
        # Specific phrases count most: "one home for every agent" (3) beats a
        # passing "agentic" (1), and a spread of themes beats repeated buzzwords.
        c["score"] = (
            sum(t["weight"] for t in c["themes"].values())
            + len(c["ads"])
            + (1 if newest is not None and newest <= recent_days else 0)
        )
        c["newest_ad_days"] = newest
        ranked.append(c)

    ranked.sort(key=lambda c: (-c["score"], c["newest_ad_days"] if c["newest_ad_days"] is not None else 999))
    return ranked, dict(dropped)


# ─────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────

CSV_FIELDS = [
    "rank", "company", "score", "themes", "ad_count", "newest_ad_days",
    "staff_count", "industries", "company_url", "top_ad", "top_ad_url",
]


def write_csv(path: str, ranked: list[dict], themes: list[str]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS + [f"evidence_{t}" for t in themes])
        w.writeheader()
        for i, c in enumerate(ranked, 1):
            row = {
                "rank": i,
                "company": c["company"],
                "score": c["score"],
                "themes": ", ".join(c["themes"]),
                "ad_count": len(c["ads"]),
                "newest_ad_days": c["newest_ad_days"],
                "staff_count": c["staff_count"],
                "industries": c["industries"],
                "company_url": c["company_url"],
                "top_ad": c["ads"][0]["title"],
                "top_ad_url": c["ads"][0]["url"],
            }
            for t in themes:
                row[f"evidence_{t}"] = (c["themes"].get(t) or {}).get("evidence", "")
            w.writerow(row)


def render_report(ranked: list[dict], dropped: dict, total_jobs: int, limit: int = 25) -> str:
    lines = [
        "# Signal Stack report",
        "",
        f"{total_jobs} job ads checked, {len(ranked)} companies with signals.",
        "",
        "Dropped: " + (", ".join(f"{n} {r}" for r, n in sorted(dropped.items())) or "none"),
        "",
    ]
    for i, c in enumerate(ranked[:limit], 1):
        days = c["newest_ad_days"]
        age = "date unknown" if days is None else f"{days} day{'' if days == 1 else 's'} ago"
        lines += [
            f"## {i}. {c['company']} (score {c['score']})",
            "",
            f"{c['staff_count'] or '?'} staff · {c['industries'] or 'industry unknown'} · newest ad {age}",
            "",
        ]
        for theme, hit in c["themes"].items():
            lines.append(f"- **{theme}** (\"{hit['phrase']}\" in {hit['ad']}): {hit['evidence']}")
        lines.append("")
        for ad in c["ads"]:
            lines.append(f"- [{ad['title']}]({ad['url']})" if ad["url"] else f"- {ad['title']}")
        lines.append("")
    return "\n".join(lines)


def run_score(cfg: dict, jobs: list[dict], today: datetime | None = None) -> list[dict]:
    today = today or datetime.now(timezone.utc)
    ranked, dropped = score_companies(cfg, jobs, today)
    out = cfg["output_dir"]
    write_csv(os.path.join(out, "companies.csv"), ranked, list(cfg["signals"]))
    with open(os.path.join(out, "report.md"), "w") as f:
        f.write(render_report(ranked, dropped, len(jobs)))
    log.info(f"Scored {len(jobs)} ads: {len(ranked)} companies. Dropped: {dropped}")
    log.info(f"Wrote {out}/companies.csv and {out}/report.md")
    return ranked


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def load_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"No {path}. Run the enrich step first.")
    with open(path) as f:
        return [json.loads(line) for line in f]


DEMO_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo", "jobs_enriched.jsonl")
DEMO_DATE = datetime(2026, 11, 13, tzinfo=timezone.utc)  # the day the demo ads were pulled


def print_top(ranked: list[dict], n: int = 5):
    print(f"\nTop {min(n, len(ranked))} of {len(ranked)} companies:\n")
    for i, c in enumerate(ranked[:n], 1):
        print(f"{i}. {c['company']}  (score {c['score']})")
        for theme, hit in c["themes"].items():
            print(f'   {theme}: "{hit["phrase"]}"')
        print()


def run_demo():
    """Score a real run's ads (anonymised and trimmed) with no API key."""
    cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json"))
    cfg["output_dir"] = os.path.join("output", "demo")
    os.makedirs(cfg["output_dir"], exist_ok=True)
    setup_logging(cfg["output_dir"])
    ranked = run_score(cfg, load_jsonl(DEMO_DATA), today=DEMO_DATE)
    print_top(ranked)
    print(f"Full report with quotes: {cfg['output_dir']}/report.md")


def main():
    parser = argparse.ArgumentParser(description="Find companies whose job ads describe your problem")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--step", choices=["search", "enrich", "score"],
                        help="Run one step only (default: all)")
    parser.add_argument("--demo", action="store_true",
                        help="Score 91 real ads from a saved run. No API key needed.")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return

    cfg = load_config(args.config)
    setup_logging(cfg["output_dir"])
    out = cfg["output_dir"]

    if args.step == "score":
        run_score(cfg, load_jsonl(os.path.join(out, "jobs_enriched.jsonl")))
        return

    require_api_key(cfg)
    client = LixClient(cfg["api_key"], cfg.get("max_requests", 300))
    try:
        if args.step in (None, "search"):
            jobs = run_search(cfg, client)
        else:
            with open(os.path.join(out, "jobs_raw.json")) as f:
                jobs = json.load(f)
        if args.step in (None, "enrich"):
            run_score(cfg, run_enrich(cfg, client, jobs))
    except OutOfCredits as e:
        log.error(f"Stopped: {e}. Progress is saved; run --step enrich to resume.")
        enriched_path = os.path.join(out, "jobs_enriched.jsonl")
        if os.path.exists(enriched_path):
            log.info("Scoring what was enriched before the stop")
            run_score(cfg, load_jsonl(enriched_path))
        sys.exit(1)
    finally:
        log.info(f"API calls this run: {client.calls}")


if __name__ == "__main__":
    main()
