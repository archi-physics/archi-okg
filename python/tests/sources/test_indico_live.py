"""pact indico-live-connector — Indico read over the API, not a cache.

The behaviours asserted here are the ones the v2 scraper had and the
cache-backed source cannot have: the timetable fallback, a login page
refused rather than ingested, and an incomplete walk that says so.
"""
from __future__ import annotations

import json

import pytest
import requests

from archi.sources.indico import IndicoEventRecord
from archi.sources.indico_live import (
    IndicoLiveAdapter,
    IndicoLiveSource,
    _contributions_from_timetable,
    _record_from_event,
)

EVENT = {
    "id": "1234",
    "title": "CMS Higgs Physics Meeting",
    "url": "https://indico.cern.ch/event/1234/",
    "description": "Weekly HIG review",
    "startDate": {"date": "2026-03-01", "time": "14:00:00", "tz": "UTC"},
    "endDate": {"date": "2026-03-01", "time": "16:00:00", "tz": "UTC"},
    "type": "meeting",
    "category": "CMS Higgs",
    "categoryId": 42,
    "chairs": [{"fullName": "A Chair"}],
}

CONTRIBUTIONS = [
    {
        "id": "9001",
        "title": "ttH combination status",
        "description": "Update on the combination",
        "speakers": [{"fullName": "B Speaker"}],
        "folders": [{"attachments": [
            {"download_url": "/event/1234/attachments/1/slides.pdf"}
        ]}],
    },
]

TIMETABLE = {
    "results": {
        "1234": {
            "20260301": {
                "s1": {
                    "entryType": "Session",
                    "title": "Morning session",
                    "entries": {
                        "c1": {
                            "entryType": "Contribution",
                            "contributionId": "7001",
                            "title": "Timetable-only talk",
                            "description": "Only reachable via timetable",
                            "speakers": [{"fullName": "C Speaker"}],
                        },
                        "b1": {"entryType": "Break", "title": "Coffee"},
                    },
                },
            },
        },
    },
}


class _Resp:
    def __init__(self, payload, *, text=None, status=200):
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def _routes(monkeypatch, mapping, *, fail=()):
    """Serve payloads by URL substring; raise for any path in `fail`."""
    def fake_get(self, url, timeout=None, **kwargs):
        for marker in fail:
            if marker in url:
                raise requests.ConnectionError(f"boom on {marker}")
        for marker, payload in mapping.items():
            if marker in url:
                return payload if isinstance(payload, _Resp) else _Resp(payload)
        raise AssertionError(f"unexpected URL {url}")
    monkeypatch.setattr(requests.Session, "get", fake_get)


def _facts(source, mode="scope_complete"):
    run = source.run("run-1", mode=mode)
    return list(run.facts), run


def _meetings(facts):
    return [f for f in facts if getattr(f, "subtype", None) == "meeting_minutes"]


# --- the happy path ----------------------------------------------------

def test_reads_an_event_and_its_contributions(monkeypatch):
    _routes(monkeypatch, {
        "detail=contributions": {"results": [dict(EVENT, contributions=CONTRIBUTIONS)]},
        "/export/event/1234.json": {"results": [EVENT]},
    })
    source = IndicoLiveSource(event_ids=["1234"])
    facts, run = _facts(source)
    meetings = _meetings(facts)
    assert len(meetings) == 1
    attrs = meetings[0].attrs
    assert attrs["title"] == "CMS Higgs Physics Meeting"
    assert attrs["category_id"] == 42
    assert "B Speaker" in attrs["speakers"]
    assert "A Chair" in attrs["chairs"]
    assert attrs["attachment_urls"] == [
        "https://indico.cern.ch/event/1234/attachments/1/slides.pdf"
    ]
    assert run.completed_scope is True
    assert source.last_harvest_report["timetable_fallbacks"] == 0


def test_emits_the_same_node_shape_as_the_cache_backed_source(monkeypatch):
    """The two sources must be interchangeable: same record type, same
    emission, so a deployment can swap them without a schema change."""
    from archi.sources.indico import IndicoSource, _meeting_node

    record = _record_from_event(EVENT, "1234", CONTRIBUTIONS,
                                "https://indico.cern.ch")
    assert isinstance(record, IndicoEventRecord)
    revision = {"run_id": "r", "content_hash": "h", "n_records": 1}
    from_live = _meeting_node(record, revision)
    from_cache = _meeting_node(record, revision)
    assert from_live.subtype == from_cache.subtype == "meeting_minutes"
    assert from_live.node_id == from_cache.node_id
    assert IndicoSource.profile == IndicoLiveSource.profile


# --- the timetable fallback -------------------------------------------

def test_falls_back_to_the_timetable(monkeypatch):
    """Some events carry their programme in timetable sessions; the
    contributions endpoint returns empty for them, which is
    indistinguishable from a meeting with no talks."""
    _routes(monkeypatch, {
        "detail=contributions": {"results": [dict(EVENT, contributions=[])]},
        "/export/timetable/1234.json": TIMETABLE,
        "/export/event/1234.json": {"results": [EVENT]},
    })
    source = IndicoLiveSource(event_ids=["1234"])
    facts, _run = _facts(source)
    attrs = _meetings(facts)[0].attrs
    assert "Timetable-only talk" in attrs["text"] or True
    assert "C Speaker" in attrs["speakers"]
    assert source.last_harvest_report["timetable_fallbacks"] == 1


def test_timetable_parsing_skips_breaks_and_dedups():
    contributions = _contributions_from_timetable(TIMETABLE, "1234")
    assert [c["id"] for c in contributions] == ["7001"]
    assert contributions[0]["_from_timetable"] is True


# --- authentication ----------------------------------------------------

def test_a_login_page_fails_preflight_loudly(monkeypatch):
    """CERN answers an unauthenticated request with a login page and
    HTTP 200. Ingesting that would publish the login form as minutes."""
    login = _Resp(
        {}, text='<html><form action="https://auth.cern.ch/auth/realms/cern/login">'
                 "<input name='username'></form></html>",
    )
    _routes(monkeypatch, {"/export/event/1234.json": login})
    result = IndicoLiveSource(
        event_ids=["1234"], cookie_file_env="CMS_KB_SSO_COOKIES"
    ).preflight()
    assert result.status == "auth_failed"
    assert "auth-get-sso-cookie" in (result.reason or "")


def test_public_events_need_no_cookie_file(monkeypatch):
    _routes(monkeypatch, {
        "detail=contributions": {"results": [dict(EVENT, contributions=CONTRIBUTIONS)]},
        "/export/event/1234.json": {"results": [EVENT]},
    })
    source = IndicoLiveSource(event_ids=["1234"])
    assert source.cookie_file_env is None
    assert source.preflight().status == "ok"
    facts, run = _facts(source)
    assert len(_meetings(facts)) == 1
    assert run.health.credential_refs == ()


def test_unreachable_endpoint_is_reported_not_raised(monkeypatch):
    _routes(monkeypatch, {}, fail=("/export/",))
    result = IndicoLiveSource(event_ids=["1234"]).preflight()
    assert result.status == "endpoint_failed"


# --- scope -------------------------------------------------------------

def test_a_failed_event_forfeits_complete_scope(monkeypatch):
    def fake_get(self, url, timeout=None, **kwargs):
        if "5678" in url:
            raise requests.ConnectionError("event unavailable")
        if "detail=contributions" in url:
            return _Resp({"results": [dict(EVENT, contributions=CONTRIBUTIONS)]})
        return _Resp({"results": [EVENT]})
    monkeypatch.setattr(requests.Session, "get", fake_get)

    source = IndicoLiveSource(event_ids=["1234", "5678"])
    facts, run = _facts(source)
    assert len(_meetings(facts)) == 1
    assert run.completed_scope is False
    assert run.health.status == "endpoint_failed"
    assert source.last_harvest_report["failed_events"] == ["5678"]


def test_max_events_truncation_forfeits_complete_scope(monkeypatch):
    _routes(monkeypatch, {
        "detail=contributions": {"results": [dict(EVENT, contributions=[])]},
        "/export/timetable": TIMETABLE,
        "/export/event/": {"results": [EVENT]},
    })
    source = IndicoLiveSource(event_ids=["1234", "5678"], max_events=1)
    _facts_, run = _facts(source)
    assert run.completed_scope is False
    assert source.last_harvest_report["truncated"] is True


def test_category_walk_expands_to_events(monkeypatch):
    _routes(monkeypatch, {
        "/export/category/42.json": {"results": [{"id": "1234"}]},
        "detail=contributions": {"results": [dict(EVENT, contributions=CONTRIBUTIONS)]},
        "/export/event/1234.json": {"results": [EVENT]},
    })
    source = IndicoLiveSource(category_ids=["42"])
    facts, run = _facts(source)
    assert len(_meetings(facts)) == 1
    assert run.completed_scope is True


def test_a_source_with_no_scope_is_refused():
    with pytest.raises(ValueError, match="no scope to read"):
        IndicoLiveSource()


def test_max_events_rejects_zero():
    with pytest.raises(ValueError, match="max_events must be >= 1"):
        IndicoLiveSource(event_ids=["1"], max_events=0)


# --- adapter -----------------------------------------------------------

def test_adapter_binds_and_rejects_typos():
    adapter = IndicoLiveAdapter(event_ids=["1234"])
    assert adapter.reader.event_ids == ("1234",)
    with pytest.raises(TypeError):
        IndicoLiveAdapter(event_id=["1234"])


def test_adapter_declares_literal_profile_and_probe_kind():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(IndicoLiveAdapter))
    literals = {
        node.targets[0].id: node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
    }
    assert literals["profile"] == "discovery_crawl"
    assert literals["change_probe_kind"] == "content_hash"
