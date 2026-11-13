#!/usr/bin/env python3
"""
Build demo/jobs_enriched.jsonl from a real run's output/jobs_enriched.jsonl.

The demo should behave like a real run without republishing job ads or a
prospect list, so each ad is cut down to the text around each configured signal
phrase (the same window the report quotes), and every company is renamed to its industry
plus a letter ("Financial Services B").

Usage: python scripts/make_demo_data.py output/jobs_enriched.jsonl
"""

import json
import re
import string
import sys

sys.path.insert(0, ".")
import signal_stack as ss  # noqa: E402

WINDOW = ss.EVIDENCE_WINDOW + 20
NO_SIGNAL = "(This ad had none of the configured signal phrases.)"


def excerpt(text: str, patterns) -> str:
    spans = sorted((max(0, m.start() - WINDOW), min(len(text), m.end() + WINDOW))
                   for p in patterns for m in p.finditer(text))
    merged = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return "\n...\n".join(" ".join(text[lo:hi].split()) for lo, hi in merged) or NO_SIGNAL


def main(src: str):
    cfg = json.load(open("config.example.json"))
    compiled = ss.compile_signals(cfg["signals"])
    patterns = [p for theme in compiled.values() for _, p, _ in theme]

    rows = [json.loads(line) for line in open(src)]
    labels: dict[str, str] = {}
    per_industry: dict[str, int] = {}
    for row in rows:
        if row["company"] in labels:
            continue
        industry = (row["industries"] or ["Company"])[0]
        n = per_industry.get(industry, 0)
        per_industry[industry] = n + 1
        labels[row["company"]] = f"{industry} {string.ascii_uppercase[n % 26]}"

    out = []
    for i, row in enumerate(rows):
        label = labels[row["company"]]
        text = excerpt(row["description"], patterns)
        if row["company"]:
            text = re.sub(rf"\b{re.escape(row['company'])}\b", label, text)
        out.append({
            **row,
            "job_id": f"demo{i:03d}",
            "company": label,
            "company_url": "",
            "company_id": f"demo-{label}",
            "description": text,
        })

    with open("demo/jobs_enriched.jsonl", "w") as f:
        for row in out:
            f.write(json.dumps(row) + "\n")
    print(f"Wrote {len(out)} ads from {len(labels)} companies to demo/jobs_enriched.jsonl")


if __name__ == "__main__":
    main(sys.argv[1])
