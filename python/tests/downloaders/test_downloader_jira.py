"""req.compops-snapshot-builder -- the moved JIRA downloader, with no network.

A fake ``jira`` module stands in for the CERN client, so the test drives the
real fetch, record shaping and cache writing, then reads the cache back with
the archi JIRA reader.
"""
from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace as NS

import pytest

from archi.downloaders import jira as download_jira
from archi.sources.jira import JiraIssueSource


def _issue(key, *, summary, assignee, comments=()):
    fields = NS(
        summary=summary,
        description=f"Details for {key}",
        status="Open",
        priority="Major",
        issuetype="Bug",
        assignee=NS(displayName=assignee),
        reporter=NS(displayName="Grace Hopper"),
        created="2026-08-01T10:00:00.000+0000",
        updated="2026-08-02T10:00:00.000+0000",
        labels=["ops"],
        components=[NS(name="transfers")],
        resolution=None,
        fixVersions=[],
        comment=NS(
            total=len(comments),
            comments=[
                NS(author=NS(displayName="Ada Lovelace"), created="2026-08-03", body=b)
                for b in comments
            ],
        ),
        issuelinks=[NS(outwardIssue=NS(key="CMSPROD-1"), type="Relates")],
        subtasks=[],
        parent=None,
        environment="prod",
        duedate=None,
    )
    return NS(key=key, fields=fields)


class FakeJIRA:
    calls: list[tuple] = []

    def __init__(self, server, token_auth):
        self.server = server
        self.token = token_auth

    def search_issues(self, jql, startAt, maxResults, fields):
        FakeJIRA.calls.append((jql, startAt, maxResults))
        issues = [
            _issue("CMSPROD-2", summary="Stuck transfers", assignee="Ada Lovelace",
                   comments=["Retrying now"]),
            _issue("CMSPROD-3", summary="Site in downtime", assignee="Enrico Fermi"),
        ]
        return issues[startAt:startAt + maxResults]

    def comments(self, key, startAt, maxResults):  # pragma: no cover - not paged here
        return []


@pytest.fixture
def fake_jira(monkeypatch):
    FakeJIRA.calls = []
    monkeypatch.setitem(sys.modules, "jira", types.SimpleNamespace(JIRA=FakeJIRA))
    monkeypatch.setenv("CERN_JIRA_TOKEN", "test-token-not-a-secret")


def test_download_writes_a_cache_the_reader_accepts(tmp_path, fake_jira, capsys):
    out = tmp_path / "data" / "jira"
    assert download_jira.main(
        ["--output-dir", str(out), "--project", "CMSPROD", "--page-size", "1"]
    ) == 0
    records = json.loads((out / "records.json").read_text())
    meta = json.loads((out / "meta.json").read_text())
    assert [r["key"] for r in records] == ["CMSPROD-2", "CMSPROD-3"]
    assert records[0]["recent_comments"][0]["body"] == "Retrying now"
    assert records[0]["issue_links"] == [
        {"key": "CMSPROD-1", "type": "Relates", "direction": "outward"}
    ]
    assert meta["record_count"] == 2 and meta["projects"] == ["CMSPROD"]
    # Paged one issue at a time until a short page.
    assert [c[1] for c in FakeJIRA.calls] == [0, 1, 2]
    assert json.loads(capsys.readouterr().out)["record_count"] == 2

    run = JiraIssueSource(base=str(tmp_path)).run("r", mode="scope_complete")
    assert list(run.facts)
    assert run.completed_scope is True and run.health.status == "ok"
    assert run.health.record_count == 2


def test_default_output_follows_archi_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHI_DATA_ROOT", str(tmp_path))
    assert download_jira.default_output_dir() == tmp_path / "data" / "jira"
    monkeypatch.delenv("ARCHI_DATA_ROOT")
    monkeypatch.chdir(tmp_path)
    assert download_jira.default_output_dir() == tmp_path / "data" / "jira"


def test_probe_only_writes_nothing(tmp_path, fake_jira, capsys):
    out = tmp_path / "jira"
    assert download_jira.main(
        ["--output-dir", str(out), "--project", "CMSPROD", "--max-issues", "1", "--probe-only"]
    ) == 0
    assert not out.exists()
    report = json.loads(capsys.readouterr().out)
    assert report == {"ok": True, "record_count": 1, "first_key": "CMSPROD-2", "projects": ["CMSPROD"]}


def test_missing_token_stops_before_any_request(tmp_path, monkeypatch):
    for name in ("CERN_JIRA_TOKEN", "JIRA_CERN_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="missing JIRA token env"):
        download_jira.main(["--output-dir", str(tmp_path)])


def test_missing_jira_package_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("CERN_JIRA_TOKEN", "test-token-not-a-secret")
    monkeypatch.setitem(sys.modules, "jira", None)  # import raises ImportError
    with pytest.raises(SystemExit, match="'jira' Python package is not installed"):
        download_jira.main(["--output-dir", str(tmp_path)])
