"""Tests run on trimmed copies of real Lix API responses in tests/fixtures."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import signal_stack as ss

FIXTURES = Path(__file__).parent / "fixtures"
TODAY = datetime(2026, 11, 13, tzinfo=timezone.utc)


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def cfg(tmp_path):
    c = json.loads((Path(__file__).parent.parent / "config.example.json").read_text())
    c["output_dir"] = str(tmp_path)
    return c


@pytest.fixture
def jobs():
    return [ss.parse_enriched_job(load(f"enrich_{n}.json")) for n in ("9fin", "tradeweb", "lorien")]


def test_parse_search_page():
    jobs = ss.parse_search_page(load("search_jobs.json"), "AI platform engineer")
    assert [j["company"] for j in jobs] == ["9fin", "Tradeweb", "Lorien"]
    assert jobs[0]["job_id"] == "4436231421"


def test_parse_enriched_job(jobs):
    ninefin = jobs[0]
    assert ninefin["company"] == "9fin"
    assert ninefin["company_id"] == "15241220"
    assert ninefin["industries"] == ["Financial Services"]
    assert ninefin["listed_at"] == "2026-11-12"
    assert "Memory, artifacts" in ninefin["description"]


def test_junk_staff_count_falls_back_to_range(jobs):
    # Tradeweb's real response says staffCount 1 with a 1001-5000 range
    assert jobs[1]["staff_count"] == 1001


def test_recruiters_are_dropped(cfg, jobs):
    ranked, dropped = ss.score_companies(cfg, jobs, TODAY)
    assert "Lorien" not in [c["company"] for c in ranked]
    assert dropped == {"recruiter or excluded industry": 1}


def test_evidence_quotes_the_ad(cfg, jobs):
    ranked, _ = ss.score_companies(cfg, jobs, TODAY)
    ninefin = next(c for c in ranked if c["company"] == "9fin")
    assert ninefin["themes"]["agent_platform"]["phrase"] == "one home for every agent"
    assert "Memory, artifacts and event-driven agents" in ninefin["themes"]["memory_and_context"]["evidence"]


def test_ranking_counts_independent_themes(cfg, jobs):
    ranked, _ = ss.score_companies(cfg, jobs, TODAY)
    assert [c["company"] for c in ranked] == ["9fin", "Tradeweb"]
    ninefin, tradeweb = ranked
    # 9fin hits all three themes: 3x2 + 1 ad + 1 for a recent ad
    assert set(ninefin["themes"]) == {"agent_platform", "memory_and_context", "governance"}
    assert ninefin["score"] == 8
    # Tradeweb talks about agents but never in agent-platform terms
    assert set(tradeweb["themes"]) == {"memory_and_context", "governance"}
    assert tradeweb["score"] == 6
    assert ninefin["newest_ad_days"] == 1


def test_phrases_match_whole_words_only():
    compiled = ss.compile_signals({"memory_and_context": ["RAG"]})
    assert ss.match_signals("Experience with storage and leverage", compiled) == {}
    assert "memory_and_context" in ss.match_signals("Build RAG pipelines", compiled)


def test_ad_without_signals_is_dropped(cfg, jobs):
    jobs[0]["description"] = "We sell kitchens."
    ranked, dropped = ss.score_companies(cfg, jobs[:1], TODAY)
    assert ranked == []
    assert dropped == {"no signal phrases": 1}


def test_run_score_writes_csv_and_report(cfg, jobs):
    ss.run_score(cfg, jobs, today=TODAY)
    out = Path(cfg["output_dir"])
    csv_text = (out / "companies.csv").read_text()
    assert "9fin" in csv_text and "Lorien" not in csv_text
    report = (out / "report.md").read_text()
    assert "3 job ads checked, 2 companies with signals" in report
    assert "1 recruiter or excluded industry" in report


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code, self.ok = status, status < 400
        self.payload, self.text = payload, json.dumps(payload)

    def json(self):
        return self.payload


def test_payment_required_stops_the_run(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss.requests, "get", lambda *a, **k: FakeResponse(402, {"error": "no credits"}))
    with pytest.raises(ss.OutOfCredits):
        ss.LixClient("key", 10).get("/v1/enrich/job", {"job_id": "1"})


def test_request_cap(monkeypatch):
    monkeypatch.setattr(ss.time, "sleep", lambda s: None)
    monkeypatch.setattr(ss.requests, "get", lambda *a, **k: FakeResponse(200, {}))
    client = ss.LixClient("key", 1)
    client.get("/x", {})
    with pytest.raises(ss.OutOfCredits):
        client.get("/x", {})


def test_enrich_resumes_and_caps_ads_per_company(cfg, monkeypatch):
    responses = {"4436231421": load("enrich_9fin.json")}
    calls = []

    class Client:
        def get(self, endpoint, params):
            calls.append(params["job_id"])
            return responses.get(params["job_id"])

    cfg["max_ads_per_company"] = 1
    jobs = [
        {"job_id": "4436231421", "company": "9fin", "query": "q"},
        {"job_id": "999", "company": "9fin", "query": "q"},
    ]
    ss.run_enrich(cfg, Client(), jobs)
    assert calls == ["4436231421"]  # second 9fin ad skipped by the cap

    calls.clear()
    enriched = ss.run_enrich(cfg, Client(), jobs)
    assert calls == []  # resumed from jobs_enriched.jsonl
    assert len(enriched) == 1


def test_single_theme_companies_are_dropped(cfg, jobs):
    # Mirrors La Fosse in the first real run: "release governance" and nothing else
    jobs[0]["description"] = "Stronger release governance and automated deployments."
    ranked, dropped = ss.score_companies(cfg, jobs[:1], TODAY)
    assert ranked == []
    assert dropped == {"fewer than 2 themes": 1}


def test_search_pages_through_the_linkedin_url(cfg):
    page = load("search_jobs.json")
    urls = []

    class Client:
        def get(self, endpoint, params):
            urls.append(params["url"])
            # Return the same jobs every time, like the API did when paging was wrong
            return {**page, "paging": {"count": 3}}

    cfg["searches"] = ["AI platform engineer"]
    cfg["pages_per_search"] = 3
    jobs = ss.run_search(cfg, Client())
    assert len(jobs) == 3
    assert "&start=" not in urls[0]
    assert urls[1].endswith("&start=3")
    assert len(urls) == 2  # page 2 had nothing new, so paging stopped
