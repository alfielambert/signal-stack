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


# ── Team use ──────────────────────────────────────────────

def test_owner_is_kept_and_reported(cfg):
    ss.run_score(cfg, jobs(), today=WEEK1)
    edit(cfg, "9fin", "outreach: new", "outreach: new\nowner: adil")
    ss.run_score(cfg, jobs(), today=WEEK2)

    assert ob.split_frontmatter(page(cfg, "9fin"))[0]["owner"] == "adil"
    report = Path(cfg["output_dir"], "report.md").read_text()
    assert "9fin (score 9 · seen since 2026-11-13 · owner adil)" in report
    assert "adil" in Path(cfg["output_dir"], "companies.csv").read_text()
    run = Path(cfg["openlore"]["dir"], "runs", "2026-11-20.md").read_text()
    by_owner = run.split("## By owner")[1].split("## Unclaimed")[0]
    unclaimed = run.split("## Unclaimed")[1]
    assert "### adil" in by_owner and "9fin" in by_owner
    assert "Tradeweb" in unclaimed and "9fin" not in unclaimed
    assert "owner adil" in Path(cfg["openlore"]["dir"], "index.md").read_text()


def test_edit_made_during_a_run_is_kept(cfg, monkeypatch, caplog):
    ss.run_score(cfg, jobs(), today=WEEK1)
    real_annotate = ob.annotate

    def teammate_edits_mid_run(ranked, known, in_play):
        playing = real_annotate(ranked, known, in_play)
        # After the run has read the folder, Adil claims 9fin and adds a note
        edit(cfg, "9fin", "outreach: new", "outreach: new\nowner: adil")
        edit(cfg, "9fin", "## Notes", "## Notes\n\n2026-11-20 Adil: emailed Dinesh.")
        return playing

    monkeypatch.setattr(ob, "annotate", teammate_edits_mid_run)
    with caplog.at_level("INFO", logger="signal_stack"):
        ss.run_score(cfg, jobs(), today=WEEK2)

    text = page(cfg, "9fin")
    assert ob.split_frontmatter(text)[0]["owner"] == "adil"
    assert "emailed Dinesh" in text
    assert ob.split_frontmatter(text)[0]["last_seen"] == "2026-11-20"  # the run's own update landed too
    assert "kept edits made during the run (owner None to adil, notes)" in caplog.text


def test_write_retries_when_the_page_changes_underneath(cfg, monkeypatch):
    ss.run_score(cfg, jobs(), today=WEEK1)
    path = os.path.join(cfg["openlore"]["dir"], "companies", "9fin.md")
    real_read = ob.read_page
    calls = {"n": 0}

    def changes_once(p):
        if p == path:
            calls["n"] += 1
        # This page is read when the run starts, again just before writing,
        # and once more to check nothing moved. A teammate saves before the check.
        if p == path and calls["n"] == 3:
            Path(p).write_text(Path(p).read_text().replace("outreach: new", "outreach: contacted"))
        return real_read(p)

    monkeypatch.setattr(ob, "read_page", changes_once)
    c = next(c for c in ss.run_score(cfg, jobs(), today=WEEK2) if c["company"] == "Tradeweb")
    assert c  # the run finished
    assert calls["n"] >= 5  # the check failed, so the write went round again
    assert ob.split_frontmatter(Path(path).read_text())[0]["outreach"] == "contacted"


def test_page_that_never_settles_fails_loudly(cfg, monkeypatch):
    ss.run_score(cfg, jobs(), today=WEEK1)
    real_read = ob.read_page
    flip = {"n": 0}

    def always_changing(p):
        page_, text = real_read(p)
        flip["n"] += 1
        return page_, (text or "") + str(flip["n"])

    monkeypatch.setattr(ob, "read_page", always_changing)
    with pytest.raises(RuntimeError, match="kept changing"):
        ss.run_score(cfg, jobs(), today=WEEK2)
