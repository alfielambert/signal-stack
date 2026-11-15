"""OpenLore memory: runs read and write an OKF bundle of company pages."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import openlore_bundle as ob
import signal_stack as ss

FIXTURES = Path(__file__).parent / "fixtures"
WEEK1 = datetime(2026, 11, 13, tzinfo=timezone.utc)
WEEK2 = datetime(2026, 11, 20, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    c = json.loads((Path(__file__).parent.parent / "config.example.json").read_text())
    c["output_dir"] = str(tmp_path / "out")
    c["openlore"] = {"dir": str(tmp_path / "kb")}
    os.makedirs(c["output_dir"])
    return c


def jobs():
    return [ss.parse_enriched_job(json.loads((FIXTURES / f"enrich_{n}.json").read_text()))
            for n in ("9fin", "tradeweb", "lorien")]


def page(cfg, slug):
    return Path(cfg["openlore"]["dir"], "companies", f"{slug}.md").read_text()


def edit(cfg, slug, old, new):
    p = Path(cfg["openlore"]["dir"], "companies", f"{slug}.md")
    p.write_text(p.read_text().replace(old, new))


def test_first_run_writes_a_bundle(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    kb = Path(cfg["openlore"]["dir"])
    assert sorted(p.name for p in (kb / "companies").iterdir()) == ["9fin.md", "tradeweb.md"]
    meta, body = ob.split_frontmatter(page(cfg, "9fin"))
    assert meta["type"] == "company" and meta["outreach"] == "new"
    assert meta["first_seen"] == "2026-11-13" and meta["score"] == 9
    assert meta["sources"][0]["resource"] == "https://www.linkedin.com/jobs/view/4436231421/"
    assert "one home for every agent" in body and body.rstrip().endswith("## Notes")
    assert (kb / "runs" / "2026-11-13.md").exists()
    assert "okf_version" in (kb / "index.md").read_text()


def test_outreach_and_notes_survive_the_next_run(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    edit(cfg, "9fin", "outreach: new", "outreach: replied")
    edit(cfg, "tradeweb", "## Notes", "## Notes\n\nCarol is the right person. Call after Christmas.")
    ranked = ss.run_score(cfg, jobs(), today=WEEK2)

    assert ob.split_frontmatter(page(cfg, "9fin"))[0]["outreach"] == "replied"
    assert "Carol is the right person" in page(cfg, "tradeweb")
    # A company the team is already talking to leaves the ranking
    assert [c["company"] for c in ranked] == ["Tradeweb"]
    report = Path(cfg["output_dir"], "report.md").read_text()
    assert "## Already in play" in report and "9fin (replied, score 9)" in report
    assert "9fin" not in Path(cfg["output_dir"], "companies.csv").read_text()


def test_history_new_and_rising(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    week2 = jobs()
    week2[1]["description"] += " You will own the agent platform."  # Tradeweb gains a theme
    ranked = ss.run_score(cfg, week2, today=WEEK2)

    tradeweb = next(c for c in ranked if c["company"] == "Tradeweb")
    assert tradeweb["is_new"] is False and tradeweb["previous_score"] == 6 and tradeweb["score"] == 9
    meta = ob.split_frontmatter(page(cfg, "tradeweb"))[0]
    assert [h["date"] for h in meta["score_history"]] == ["2026-11-13", "2026-11-20"]
    assert meta["first_seen"] == "2026-11-13" and meta["last_seen"] == "2026-11-20"
    run = Path(cfg["openlore"]["dir"], "runs", "2026-11-20.md").read_text()
    assert "Tradeweb](../companies/tradeweb.md) (6 to 9)" in run
    report = Path(cfg["output_dir"], "report.md").read_text()
    assert "Tradeweb (score 9 · seen since 2026-11-13, was 6)" in report


def test_rerun_on_the_same_day_does_not_duplicate_history(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    ss.run_score(cfg, jobs(), today=WEEK1)
    meta = ob.split_frontmatter(page(cfg, "9fin"))[0]
    assert len(meta["score_history"]) == 1


def test_log_is_newest_first(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    ss.run_score(cfg, jobs(), today=WEEK2)
    log = Path(cfg["openlore"]["dir"], "log.md").read_text()
    dates = re.findall(r"^## (\d{4}-\d{2}-\d{2})$", log, re.M)
    assert dates == ["2026-11-20", "2026-11-13"]
    assert not log.startswith("---")  # OKF: log.md carries no frontmatter


def test_every_page_is_okf_shaped_and_links_resolve(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    ss.run_score(cfg, jobs(), today=WEEK2)
    kb = Path(cfg["openlore"]["dir"])
    for p in kb.rglob("*.md"):
        text = p.read_text()
        if p.name == "log.md":
            continue
        meta, _ = ob.split_frontmatter(text)
        if p.name == "index.md":
            assert meta["okf_version"] == ob.OKF_VERSION
        else:
            assert meta.get("type"), p
            # OKF reserves status for draft/stable/deprecated, so outreach lives elsewhere
            assert "status" not in meta
        for target in re.findall(r"\]\(([^)#:]+\.md)\)", text):
            assert (p.parent / target).resolve().exists(), f"{p}: {target}"


def test_unquoted_dates_written_by_a_person_still_load(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    edit(cfg, "9fin", "first_seen: '2026-11-13'", "first_seen: 2026-11-13")
    ss.run_score(cfg, jobs(), today=WEEK2)
    assert ob.split_frontmatter(page(cfg, "9fin"))[0]["first_seen"] == "2026-11-13"


def test_page_without_company_key_fails_loudly(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    edit(cfg, "9fin", "company_key:", "renamed_key:")
    with pytest.raises(ValueError, match="no company_key"):
        ss.run_score(cfg, jobs(), today=WEEK2)


def test_without_openlore_nothing_changes(cfg):
    del cfg["openlore"]
    ranked = ss.run_score(cfg, jobs(), today=WEEK1)
    assert "is_new" not in ranked[0]
    assert "seen since" not in Path(cfg["output_dir"], "report.md").read_text()
