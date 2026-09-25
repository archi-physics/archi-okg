"""pact twiki-physics-filter — opt-in physics scoping for twiki_eos.

The filter is off unless a deployment asks for it, so the first test
here is the one that matters to every existing archi consumer: default
construction ingests the same records it did before the option existed.
"""
import pytest

from archi.sources._twiki_physics import (
    classify_page_type,
    compute_keep_set,
    filter_records,
    is_compops_topic,
    passes_allow_list,
)
from archi.sources.twiki import TwikiEOSSource, TwikiRecord


def _topic(body_parent: str = "") -> str:
    parent = (
        f'%META:TOPICPARENT{{name="{body_parent}"}}%\n' if body_parent else ""
    )
    return (
        '%META:TOPICINFO{author="jdoe" date="1700000000" version="3"}%\n'
        f"{parent}"
        "---+ Heading\nBody text.\n"
    )


def _snapshot(tmp_path):
    """A snapshot spanning every stage: a PAG-coded seed, a child that
    only qualifies through its parent chain, a CompOps page, a howto,
    and a page that qualifies on nothing."""
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "HIG-19-001.txt").write_text(_topic())
    (root / "StatisticsCommittee.txt").write_text(_topic("HIG-19-001"))
    (root / "GrandChildPage.txt").write_text(_topic("StatisticsCommittee"))
    (root / "CompOpsTransferTeam.txt").write_text(_topic())
    (root / "HowToRunCrab.txt").write_text(_topic())
    (root / "RandomUnrelatedPage.txt").write_text(_topic())
    return root


def _titles(source):
    run = source.run("run-1", mode="scope_complete")
    facts = list(run.facts)
    return {
        fact.attrs["title"]
        for fact in facts
        if getattr(fact, "subtype", None) == "documentation_page"
    }, run


# --- the default path is untouched -------------------------------------------

def test_filter_is_off_by_default(tmp_path):
    root = _snapshot(tmp_path)
    titles, run = _titles(TwikiEOSSource(eos_root=str(root)))
    assert titles == {
        "HIG-19-001",
        "StatisticsCommittee",
        "GrandChildPage",
        "CompOpsTransferTeam",
        "HowToRunCrab",
        "RandomUnrelatedPage",
    }
    assert run.health.record_count == 6
    assert TwikiEOSSource(eos_root=str(root)).physics_filter is False


def test_default_source_reports_no_filter_activity(tmp_path):
    root = _snapshot(tmp_path)
    source = TwikiEOSSource(eos_root=str(root))
    list(source.run("run-1", mode="scope_complete").facts)
    assert source.last_physics_report is None


# --- the opt-in path ---------------------------------------------------------

def test_filter_on_scopes_the_snapshot_to_physics(tmp_path):
    root = _snapshot(tmp_path)
    titles, run = _titles(
        TwikiEOSSource(eos_root=str(root), physics_filter=True)
    )
    # Seed (stage 1), its closure children (stage 3), and a howto kept on
    # page type (stage 4). CompOps dropped (stage 2); the unrelated page
    # qualifies on nothing.
    assert titles == {
        "HIG-19-001",
        "StatisticsCommittee",
        "GrandChildPage",
        "HowToRunCrab",
    }
    assert run.health.record_count == 4
    assert run.health.status == "ok"


def test_filter_report_carries_the_stage_counts(tmp_path):
    root = _snapshot(tmp_path)
    source = TwikiEOSSource(eos_root=str(root), physics_filter=True)
    list(source.run("run-1", mode="scope_complete").facts)
    report = source.last_physics_report
    assert report is not None
    assert report.input_total == 6
    assert report.blacklist_dropped == 1
    assert report.seed_count == 1
    assert report.closure_count == 3
    assert report.kept_count == 4
    assert report.dropped_topics == ("RandomUnrelatedPage",)
    assert report.as_dict()["dropped_count"] == 1


def test_filter_applies_to_fixture_records_too():
    records = [
        TwikiRecord(
            page_id="CMS/HIG-19-001",
            web_name="CMS",
            title="HIG-19-001",
            url="https://twiki.cern.ch/twiki/bin/view/CMS/HIG-19-001",
            source_path="HIG-19-001.txt",
            body="text",
        ),
        TwikiRecord(
            page_id="CMS/CompOpsTransferTeam",
            web_name="CMS",
            title="CompOpsTransferTeam",
            url="https://twiki.cern.ch/twiki/bin/view/CMS/CompOpsTransferTeam",
            source_path="CompOpsTransferTeam.txt",
            body="text",
        ),
    ]
    source = TwikiEOSSource(records=list(records), physics_filter=True)
    run = source.run("run-1", mode="scope_complete")
    assert run.health.record_count == 1
    assert source.last_physics_report.kept_count == 1


def test_filter_composes_with_seed_topics(tmp_path):
    root = _snapshot(tmp_path)
    source = TwikiEOSSource(
        eos_root=str(root),
        seed_topics=["CompOpsTransferTeam"],
        physics_filter=True,
    )
    run = source.run("run-1", mode="scope_complete")
    # The walk reads the seed; the filter then drops it as CompOps. The
    # seed was present, so scope is still complete.
    assert run.health.record_count == 0
    assert run.completed_scope is True


def test_flag_is_part_of_the_change_probe_config(tmp_path):
    """Flipping the flag changes the scope, so it must change the probe's
    config hash — otherwise a re-run would report the corpus unchanged
    while ingesting a different set of pages."""
    root = _snapshot(tmp_path)
    off = TwikiEOSSource(eos_root=str(root)).change_probe
    on = TwikiEOSSource(eos_root=str(root), physics_filter=True).change_probe
    assert off._config_hash != on._config_hash


# --- the stages, directly ----------------------------------------------------

@pytest.mark.parametrize(
    "topic,expected",
    [
        ("HIG-19-001", True),
        ("SUS_20_004", True),
        ("MuonPOGRecipes", True),
        ("WorkBookGenerators", True),
        ("TopQuarkPhysics", True),
        ("TopicOne", False),
        ("SiteReadinessOverview", False),
    ],
)
def test_allow_list(topic, expected):
    assert passes_allow_list(topic) is expected


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("CompOpsTransferTeam", True),
        ("WMCoreAgent", True),
        ("SiteStatusBoard", True),
        ("HIG-19-001", False),
    ],
)
def test_blacklist(topic, expected):
    assert is_compops_topic(topic) is expected


def test_closure_tolerates_cycles():
    topics = ["HIG-19-001", "A", "B", "Orphan"]
    parents = {"A": "B", "B": "A", "Orphan": "", "HIG-19-001": ""}
    seeds, kept = compute_keep_set(topics, parents)
    assert seeds == {"HIG-19-001"}
    assert kept == {"HIG-19-001"}


def test_closure_reaches_through_a_chain():
    topics = ["HIG-19-001", "Child", "GrandChild"]
    parents = {"Child": "HIG-19-001", "GrandChild": "Child"}
    _seeds, kept = compute_keep_set(topics, parents)
    assert kept == {"HIG-19-001", "Child", "GrandChild"}


def test_web_qualified_parents_do_not_close_the_chain():
    """Carried limitation, asserted so a future fix is a visible change:
    a web-qualified TOPICPARENT does not match a bare topic key."""
    topics = ["HIG-19-001", "Child"]
    parents = {"Child": "CMS.HIG-19-001"}
    _seeds, kept = compute_keep_set(topics, parents)
    assert kept == {"HIG-19-001"}


@pytest.mark.parametrize(
    "title,parent,expected",
    [
        ("HowToRunCrab", "", "howto"),
        ("MuonFAQ", "", "faq"),
        ("TriggerGlossary", "", "glossary"),
        ("HIG-19-001", "", "analysis"),
        ("SomePage", "WeeklyMeetings", "meeting_minutes"),
        ("RandomUnrelatedPage", "", "other"),
    ],
)
def test_page_type_classification(title, parent, expected):
    assert classify_page_type(title, parent) == expected


def test_filter_records_preserves_order():
    records = [
        TwikiRecord(
            page_id=f"CMS/{title}",
            web_name="CMS",
            title=title,
            url="",
            source_path=f"{title}.txt",
            body="",
        )
        for title in ("HIG-19-001", "HowToRunCrab", "SUS-20-004")
    ]
    kept, _report = filter_records(records)
    assert [record.title for record in kept] == [
        "HIG-19-001",
        "HowToRunCrab",
        "SUS-20-004",
    ]
