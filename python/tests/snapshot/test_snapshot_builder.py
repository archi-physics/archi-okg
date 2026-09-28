"""req.compops-snapshot-builder -- build and verify a cache snapshot, offline.

Every fixture is synthetic and tiny. Each text-bearing group carries one
planted address (``PLANTED``) that must not survive into any archive.
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import signal
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import pytest
import yaml

from archi.snapshot import groups as groups_module
from archi.snapshot.__main__ import main as cli_main
from archi.snapshot.builder import (
    LOCK_NAME,
    BuildRefused,
    SnapshotError,
    build,
    load_config,
    normalize_fact,
    parse_config,
    read_archive,
    verify,
)

PLANTED = {
    "cric": "site.admin@cern.ch",
    "cric-core": "core.ops@fnal.gov",
    "jira": "jane.doe@cern.ch",
    "indico": "chair.person@cern.ch",
    "dqm": "dqm.shifter@cern.ch",
    "gocdb-downtimes": "grid.admin@example.org",
    "gitlab-docs": "doc.writer@cern.ch",
    "docsite": "web.master@cern.ch",
    "twiki-eos": "twiki.editor@cern.ch",
    "conddb-global-tags": "alca.contact@cern.ch",
    "wmstats": "pdmv.operator@cern.ch",
    "dbs": "dbs.contact@cern.ch",
}
DN = "/DC=ch/DC=cern/OU=Organic Units/OU=Users/CN=jdoe/CN=123456/CN=Jane Doe"

JIRA_RECORDS = [
    {
        "key": "CMSPROD-101",
        "summary": "Transfer failures at T2_US_MIT",
        "description": f"Stuck transfers; mail {PLANTED['jira']} or see CMSPROD-100.",
        "project": "CMSPROD",
        "status": "Open",
        "priority": "Critical",
        "issue_type": "Bug",
        "assignee": "Ada Lovelace",
        "reporter": "Grace Hopper",
        "created": "2026-01-05T10:00:00.000+0000",
        "updated": "2026-02-01T09:30:00.000+0000",
        "labels": ["ops"],
        "components": ["transfers"],
        "comment_count": 1,
        "recent_comments": [
            {
                "author": "Ada Lovelace",
                "created": "2026-01-06T08:00:00.000+0000",
                "body": f"Retrying; cc {PLANTED['jira']}",
            }
        ],
        "issue_links": [{"key": "CMSPROD-100", "type": "Relates", "direction": "outward"}],
        "subtasks": [],
        "parent_key": "",
        "environment": "prod",
        "duedate": "2026-03-01",
    },
    {
        "key": "CMSPROD-100",
        "fields": {
            "summary": "Baseline transfer issue",
            "description": "Original report.",
            "project": {"key": "CMSPROD", "name": "CMS Production"},
            "status": {"name": "Closed"},
            "issuetype": {"name": "Task"},
            "assignee": {
                "displayName": "Ada Lovelace",
                "emailAddress": "ada.lovelace@cern.ch",
                "avatarUrls": {"48x48": "https://its.cern.ch/avatar/1"},
            },
            "reporter": {"displayName": "Enrico Fermi"},
            "created": "2025-12-01T10:00:00.000+0000",
            "updated": "2025-12-20T10:00:00.000+0000",
            "comment": {"comments": [], "total": 0},
            "watches": {"watchCount": 3},
        },
    },
]


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def write_sources(root: Path) -> dict[str, Path]:
    """One directory per group, holding exactly the files its reader reads."""
    dirs = {name: root / name for name in groups_module.GROUPS}
    _write_json(
        dirs["cric"] / "sites.json",
        {
            "T2_US_MIT": {
                "tier_level": 2,
                "country": "United States",
                "facility": "MIT",
                "state": "ACTIVE",
                "sitedb_title": "MIT Bates",
                "computeunits": {"MIT-CE1": {}},
                "contact_email": PLANTED["cric"],
            }
        },
    )
    _write_json(
        dirs["cric"] / "facilities.json",
        {
            "MIT": {
                "country": "United States",
                "fullname": f"MIT (admin {PLANTED['cric']})",
                "cmssites": [{"name": "T2_US_MIT"}],
            }
        },
    )
    _write_json(
        dirs["cric"] / "storage_units.json",
        {"MIT-SE": {"type": "DISK", "pledged-CMS": 4200.0, "site": {"name": "T2_US_MIT"}}},
    )
    _write_json(
        dirs["cric"] / "compute_units.json",
        {"MIT-CE1": {"corepower": 11.0, "potential_max": 20000.0, "state": "ACTIVE"}},
    )
    _write_json(
        dirs["cric"] / "responsibilities.json",
        {
            "desc": {"columns": ["username", "site_name", "role"]},
            "result": [
                ["adalove", "MIT Bates", "Site Executive"],
                ["ghopper", None, "Site Admin"],  # a null site, as in real exports
            ],
        },
    )
    _write_json(
        dirs["cric-core"] / "services.json",
        {
            "mit-ce.mit.edu": {
                "type": "CE",
                "endpoint": "https://mit-ce.mit.edu:9619",
                "rcsite": "MIT",
                "contact": PLANTED["cric-core"],
            }
        },
    )
    _write_json(
        dirs["cric-core"] / "rcsites.json",
        {"MIT": {"country": "US", "sites": [{"name": "T2_US_MIT", "vo_name": "cms"}]}},
    )
    _write_json(
        dirs["cric-core"] / "federations.json",
        {
            "US-MIT": {
                "accounting_name": f"US MIT ({PLANTED['cric-core']})",
                "vos": ["cms"],
                "rcsites": ["MIT"],
                "pledges": {},
            }
        },
    )
    releases = dirs["cmssw-releases"]
    releases.mkdir(parents=True)
    (releases / "releases.map").write_text(
        "architecture=el8_amd64_gcc12;label=CMSSW_14_0_1;type=Production;state=Announced;prodarch=1;\n"
        "architecture=el8_amd64_gcc12;label=CMSSW_14_0_2;type=Production;state=Announced;prodarch=1;\n",
        encoding="utf-8",
    )
    _write_json(dirs["jira"] / "records.json", JIRA_RECORDS)
    _write_json(
        dirs["jira"] / "meta.json",
        {"record_count": 2, "server": "https://its.cern.ch/jira", "fetched_at": 1.0},
    )
    _write_json(
        dirs["indico"] / "records.json",
        [
            {
                "id": "1234",
                "title": "Computing operations weekly",
                "type": "meeting",
                "category": "CMS Computing",
                "categoryId": 42,
                "startDate": {"date": "2026-06-12", "time": "15:00:00", "tz": "Europe/Zurich"},
                "endDate": {"date": "2026-06-12", "time": "16:00:00", "tz": "Europe/Zurich"},
                "description": f"<p>Questions to {PLANTED['indico']}</p>",
                "chairs": [
                    {
                        "fullName": "Doe, Jane",
                        "email": PLANTED["indico"],
                        "affiliation": "CERN",
                        "id": "77",
                    }
                ],
                "folders": [
                    {
                        "title": "Slides",
                        "attachments": [
                            {"download_url": "https://indico.cern.ch/event/1234/a.pdf", "size": 10}
                        ],
                    }
                ],
                "_contributions_text": f"Site report ({PLANTED['indico']})",
                "_pdf_texts": [
                    {
                        "title": "Slides",
                        "text": f"Contact {PLANTED['indico']} for access.",
                        "url": "https://indico.cern.ch/event/1234/a.pdf",
                    }
                ],
            }
        ],
    )
    _write_json(
        dirs["dqm"] / "records.json",
        [
            {
                "cert_name": "Cert_Collisions2024_378981_386951_Golden.json",
                "filename": f"golden.json (owner {PLANTED['dqm']})",
                "run_range": [378981, 386951],
                "datasets": ["/Muon0/Run2024C-PromptReco-v1/MINIAOD"],
                "num_lumi_sections": 1000,
            }
        ],
    )
    _write_json(
        dirs["gocdb-downtimes"] / "records.json",
        [
            {
                "downtime_id": 35001,
                "primary_key": "35001G0",
                "hostname": "mit-ce.mit.edu",
                "hosted_by": "MIT",
                "service_type": "CE",
                "severity": "OUTAGE",
                "classification": "SCHEDULED",
                "description": f"Kernel upgrade; contact {PLANTED['gocdb-downtimes']}",
                "start_date": "2026-08-01 08:00",
                "end_date": "2026-08-01 12:00",
            }
        ],
    )
    for group, repo in (("gitlab-docs", "cmsdmops/Documentation"), ("docsite", "")):
        _write_json(
            dirs[group] / "records.json",
            [
                {
                    "title": "Operations guide",
                    "url": f"https://example.cern.ch/{group}/guide",
                    "body": f"Page owner: {PLANTED[group]}. Restart the agent first.",
                    "site_name": "example.cern.ch",
                    "repo": repo,
                    "file_path": "guide.md",
                    "last_editor_email": PLANTED[group],
                }
            ],
        )
    twiki = dirs["twiki-eos"]
    (twiki / "Sub").mkdir(parents=True)
    (twiki / "CompOpsGuide.txt").write_text(
        '%META:TOPICINFO{author="JaneDoe" date="1700000000"}%\n'
        "---+ Comp ops guide\n"
        f"Ask {PLANTED['twiki-eos']} before draining a site. See [[SubTopicPage]].\n",
        encoding="utf-8",
    )
    (twiki / "Sub" / "SubTopicPage.txt").write_text(
        "---+ Sub topic\nDrain procedure details.\n", encoding="utf-8"
    )
    (twiki / "Sub" / "diagram.png").write_bytes(b"\x89PNG not read by the reader")
    _write_json(
        dirs["conddb-global-tags"] / "records.json",
        [
            {
                "name": "140X_dataRun3_Prompt_v3",
                "release": "CMSSW_14_0_2",
                "scenario": "prompt",
                "description": f"Prompt GT; owner {PLANTED['conddb-global-tags']}",
                "snapshot_time": "2026-05-01",
                "insertion_author": PLANTED["conddb-global-tags"],
            }
        ],
    )
    _write_json(
        dirs["wmstats"] / "records.json",
        [
            {
                "RequestName": "pdmvserv_task_TOP-Run3Summer23-00001",
                "RequestType": "TaskChain",
                "RequestStatus": "running-open",
                "Campaign": f"Run3Summer23 ({PLANTED['wmstats']})",
                "PrepID": "TOP-Run3Summer23-00001",
                "RequestPriority": 85000,
                "CMSSWVersion": "CMSSW_13_0_13",
                "InputDataset": "/TTto2L2Nu/Run3Summer23-v1/GEN-SIM",
                "OutputDatasets": ["/TTto2L2Nu/Run3Summer23-v1/AODSIM"],
                "RequestDate": "2026-01-15",
                "Requestor": "pdmvserv",
                "RequestorDN": DN,
                "RequestTransition": [{"Status": "new", "DN": DN}],
            }
        ],
    )
    _write_json(
        dirs["dbs"] / "records.json",
        [
            {
                "dataset": "/TTto2L2Nu/Run3Summer23-v1/AODSIM",
                "data_tier_name": "AODSIM",
                "physics_group_name": f"Top ({PLANTED['dbs']})",
                "nevents": 100,
                "create_by": PLANTED["dbs"],
            }
        ],
    )
    return dirs


# Aug 31 for five groups and a June range for five others, as in the first
# snapshot Jason chose; the three optional groups carry their own dates.
COLLECTED = {
    "cric": "2026-08-31",
    "cric-core": "2026-08-31",
    "jira": "2026-08-31",
    "gitlab-docs": "2026-08-31",
    "twiki-eos": "2026-08-31",
    "indico": "2026-06-12..2026-06-16",
    "dqm": "2026-06-12..2026-06-16",
    "gocdb-downtimes": "2026-06-12..2026-06-16",
    "cmssw-releases": "2026-06-12..2026-06-16",
    "conddb-global-tags": "2026-06-12..2026-06-16",
    "docsite": "2026-08-31",
    "wmstats": "2026-08-31",
    "dbs": "2026-08-31",
}


def write_config(tmp_path: Path, dirs: dict[str, Path], only=None, extra=None) -> Path:
    names = only or sorted(dirs)
    groups = {name: {"path": str(dirs[name]), "collected": COLLECTED[name]} for name in names}
    for name, fields in (extra or {}).items():
        groups[name].update(fields)
    config = tmp_path / "snapshot.yaml"
    config.write_text(
        yaml.safe_dump({"snapshot": "cms-cache-test", "groups": groups}), encoding="utf-8"
    )
    return config


@pytest.fixture
def sources(tmp_path):
    return write_sources(tmp_path / "raw")


@pytest.fixture
def built(tmp_path, sources):
    out = tmp_path / "snapshot"
    lock = build(load_config(write_config(tmp_path, sources)), out, built_by="test")
    return out, lock


def _all_bytes(out: Path, group: str) -> bytes:
    members = read_archive(out / f"{group}.tar.zst")
    return b"\n".join(members[name] for name in sorted(members))


# --- build, lock, verify -----------------------------------------------------


def test_build_writes_every_group_with_lock_rows(built):
    out, lock = built
    assert sorted(lock["groups"]) == sorted(groups_module.GROUPS)
    on_disk = yaml.safe_load((out / LOCK_NAME).read_text(encoding="utf-8"))
    assert on_disk == lock
    for name, row in lock["groups"].items():
        assert row["collected"] == COLLECTED[name]
        assert (out / row["archive"]).stat().st_size == row["bytes"]
        assert len(row["sha256"]) == 64
        assert row["record_count"] > 0 and row["file_count"] > 0
    # Records as each reader counts them: 2 JIRA issues, 2 TWiki pages, one
    # of everything else in these fixtures.
    assert lock["groups"]["jira"]["record_count"] == 2
    assert lock["groups"]["jira"]["file_count"] == 2
    assert lock["groups"]["twiki-eos"]["record_count"] == 2
    assert lock["groups"]["twiki-eos"]["file_count"] == 2  # the .png is not read
    assert lock["archive_format"]["compressor"] == "zstd"
    assert verify(out / LOCK_NAME, out).ok


def test_archives_unpack_to_the_reader_layout(built):
    out, _ = built
    members = read_archive(out / "jira.tar.zst")
    assert sorted(members) == ["data/jira/meta.json", "data/jira/records.json"]
    twiki = read_archive(out / "twiki-eos.tar.zst")
    assert sorted(twiki) == [
        "data/twiki-eos/CompOpsGuide.txt",
        "data/twiki-eos/Sub/SubTopicPage.txt",
    ]


@pytest.mark.parametrize("group", sorted(PLANTED))
def test_planted_address_is_removed_from_every_text_group(built, sources, group):
    out, _ = built
    raw = b"".join(p.read_bytes() for p in sorted(sources[group].rglob("*")) if p.is_file())
    assert PLANTED[group].encode() in raw  # the fixture really plants it
    data = _all_bytes(out, group)
    assert PLANTED[group].encode() not in data
    assert b"@" not in data.replace(b"@2x", b"")


def test_text_around_a_removed_address_is_kept(built):
    out, _ = built
    records = json.loads(read_archive(out / "jira.tar.zst")["data/jira/records.json"])
    assert records[0]["description"] == "Stuck transfers; mail  or see CMSPROD-100."
    twiki = read_archive(out / "twiki-eos.tar.zst")["data/twiki-eos/CompOpsGuide.txt"]
    assert b"Ask  before draining a site." in twiki


def test_fields_the_readers_never_read_are_dropped(built):
    out, _ = built
    jira = json.loads(read_archive(out / "jira.tar.zst")["data/jira/records.json"])
    assert "duedate" not in jira[0]
    assert set(jira[1]["fields"]["assignee"]) == {"displayName"}
    assert "watches" not in jira[1]["fields"]
    indico = json.loads(read_archive(out / "indico.tar.zst")["data/indico/records.json"])
    assert indico[0]["chairs"] == [{"fullName": "Doe, Jane"}]
    assert indico[0]["startDate"] == {"date": "2026-06-12"}
    assert indico[0]["folders"] == [
        {"attachments": [{"download_url": "https://indico.cern.ch/event/1234/a.pdf"}]}
    ]
    meta = json.loads(read_archive(out / "jira.tar.zst")["data/jira/meta.json"])
    assert meta == {"record_count": 2}
    docs = json.loads(read_archive(out / "docsite.tar.zst")["data/docsite/records.json"])
    assert "last_editor_email" not in docs[0]
    cric = read_archive(out / "cric.tar.zst")
    assert "contact_email" not in json.loads(cric["data/cric/sites.json"])["T2_US_MIT"]
    assert json.loads(cric["data/cric/responsibilities.json"]) == {
        "result": [["adalove", "MIT Bates", "Site Executive"], ["ghopper", None, "Site Admin"]]
    }


def test_wmstats_requestor_dn_never_appears_and_requestor_is_kept(built, sources):
    out, lock = built
    assert DN.encode() in (sources["wmstats"] / "records.json").read_bytes()
    data = _all_bytes(out, "wmstats")
    assert DN.encode() not in data
    assert b"RequestorDN" not in data and b"CN=" not in data
    record = json.loads(read_archive(out / "wmstats.tar.zst")["data/wmstats-workflows/records.json"])[0]
    assert record["Requestor"] == "pdmvserv"
    assert "RequestTransition" not in record
    assert lock["groups"]["wmstats"]["dropped_keys_at_any_depth"] == ["DN", "RequestorDN"]
    assert lock["groups"]["wmstats"]["kept_extra_fields"] == ["Requestor"]


def test_wmstats_dn_drop_cannot_be_turned_off_by_config(tmp_path, sources):
    config = write_config(
        tmp_path, sources, only=["wmstats"], extra={"wmstats": {"keep_fields": ["RequestorDN"]}}
    )
    out = tmp_path / "out"
    build(load_config(config), out)
    assert DN.encode() not in _all_bytes(out, "wmstats")


def test_configured_drop_of_an_unread_field(tmp_path, sources):
    config = write_config(
        tmp_path, sources, only=["wmstats"], extra={"wmstats": {"drop_fields": ["Requestor"]}}
    )
    out = tmp_path / "out"
    lock = build(load_config(config), out)
    record = json.loads(read_archive(out / "wmstats.tar.zst")["data/wmstats-workflows/records.json"])[0]
    assert "Requestor" not in record
    assert lock["groups"]["wmstats"]["dropped_fields"] == ["Requestor"]


def test_configured_drop_of_a_field_the_reader_reads_is_refused(tmp_path, sources):
    config = write_config(
        tmp_path, sources, only=["jira"], extra={"jira": {"drop_fields": ["description"]}}
    )
    with pytest.raises(BuildRefused, match="changed what the reader emits"):
        build(load_config(config), tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_a_schema_missing_a_read_field_refuses_the_group(tmp_path, sources, monkeypatch):
    spec = groups_module.GROUPS["dqm"]
    records = spec.json_files[0]
    schema = {k: v for k, v in records.schema.items() if k != "datasets"}
    broken = dataclasses.replace(
        spec, json_files=(dataclasses.replace(records, schema=schema),)
    )
    monkeypatch.setitem(groups_module.GROUPS, "dqm", broken)
    config = write_config(tmp_path, sources, only=["dqm"])
    with pytest.raises(BuildRefused) as info:
        build(load_config(config), tmp_path / "out")
    assert [r.group for r in info.value.refusals] == ["dqm"]
    assert "misses a field the reader reads" in str(info.value)


# --- the JIRA reader sees the same issues --------------------------------------


def _jira_facts(root: Path) -> list[str]:
    from archi.sources.jira import JiraIssueSource

    source = JiraIssueSource(
        records_path="data/jira/records.json", meta_path="data/jira/meta.json", base=str(root)
    )
    return [normalize_fact(f) for f in source.run("r", mode="scope_complete").facts]


def test_jira_facts_from_raw_and_redacted_caches_are_equal(built, sources, tmp_path):
    out, _ = built
    raw_root = tmp_path / "raw-root"
    (raw_root / "data").mkdir(parents=True)
    (raw_root / "data" / "jira").symlink_to(sources["jira"], target_is_directory=True)
    unpacked = tmp_path / "unpacked"
    for name, data in read_archive(out / "jira.tar.zst").items():
        (unpacked / name).parent.mkdir(parents=True, exist_ok=True)
        (unpacked / name).write_bytes(data)
    raw_facts = _jira_facts(raw_root)
    redacted_facts = _jira_facts(unpacked)
    assert raw_facts and raw_facts == redacted_facts
    # The reader removes the address itself at ingest, so neither side has it.
    assert not any(PLANTED["jira"] in fact for fact in raw_facts)
    # Guard against a vacuous pass: the raw cache does hold the address.
    assert PLANTED["jira"].encode() in (sources["jira"] / "records.json").read_bytes()


# --- malformed input refuses its group ----------------------------------------

MALFORMED = {
    "jira": ("records.json", lambda p: p + [{"summary": "no key"}], "record 2 is malformed"),
    "indico": ("records.json", lambda p: p + ["not an object"], "record 1 is malformed"),
    "dqm": ("records.json", lambda p: p + [{"filename": "x"}], "no cert_name"),
    "gocdb-downtimes": (
        "records.json",
        lambda p: p + [{"downtime_id": "abc"}],
        "downtime_id is not numeric",
    ),
    "gitlab-docs": ("records.json", lambda p: p + [{"title": "no url"}], "no url"),
    "docsite": ("records.json", lambda p: p + [{"url": ""}], "no url"),
    "conddb-global-tags": ("records.json", lambda p: p + [{"release": "x"}], "no name / tag_name"),
    "wmstats": ("records.json", lambda p: p + [{"Campaign": "x"}], "RequestName"),
    "dbs": ("records.json", lambda p: p + [{"nevents": 1}], "dataset_name / dataset"),
    "cric": ("sites.json", lambda p: {**p, "T2_XX_Bad": "not an object"}, "entry 'T2_XX_Bad'"),
    "cric-core": ("services.json", lambda p: {**p, "bad": []}, "entry 'bad'"),
}


@pytest.mark.parametrize("group", sorted(MALFORMED))
def test_a_malformed_record_refuses_its_group_and_writes_nothing(tmp_path, sources, group):
    file_name, mutate, expected = MALFORMED[group]
    path = sources[group] / file_name
    path.write_text(json.dumps(mutate(json.loads(path.read_text()))), encoding="utf-8")
    config = write_config(tmp_path, sources)  # every group, one of them broken
    out = tmp_path / "out"
    with pytest.raises(BuildRefused) as info:
        build(load_config(config), out)
    assert [r.group for r in info.value.refusals] == [group]
    assert expected in info.value.refusals[0].reason
    assert not out.exists()
    assert not list(tmp_path.glob(".out.*"))


def test_an_address_only_jira_key_refuses_the_group(tmp_path, sources):
    # Refused before redaction: an address is not an issue key.
    path = sources["jira"] / "records.json"
    records = json.loads(path.read_text()) + [{"key": "bob@cern.ch", "summary": "x"}]
    path.write_text(json.dumps(records))
    with pytest.raises(BuildRefused, match="only 2 of 3 records carry a key / issue_key matching"):
        build(load_config(write_config(tmp_path, sources, only=["jira"])), tmp_path / "out")


def test_an_identity_that_is_only_an_address_is_refused_after_redaction(tmp_path, sources):
    # Passes the first check (an id is present), then redaction empties it:
    # the reader would skip that event, so the group is refused.
    path = sources["indico"] / "records.json"
    records = json.loads(path.read_text()) + [
        {"id": "bob@cern.ch", "title": "x", "_contributions_text": ""}
    ]
    path.write_text(json.dumps(records))
    with pytest.raises(BuildRefused, match="record 1 is malformed: record has no id / event_id"):
        build(load_config(write_config(tmp_path, sources, only=["indico"])), tmp_path / "out")


@pytest.mark.parametrize(
    "group, file_name, content, expected",
    [
        ("jira", "records.json", "{not json", "not valid JSON"),
        ("jira", "records.json", '{"key": "CMSPROD-1"}', "expected a JSON list"),
        ("cric", "responsibilities.json", '{"status": "error"}', "expected a 'result' list"),
        ("cmssw-releases", "releases.map", "label=NOT_A_RELEASE;type=x;\n", "did not accept"),
    ],
)
def test_unreadable_files_refuse_their_group(tmp_path, sources, group, file_name, content, expected):
    (sources[group] / file_name).write_text(content, encoding="utf-8")
    with pytest.raises(BuildRefused) as info:
        build(load_config(write_config(tmp_path, sources, only=[group])), tmp_path / "out")
    assert expected in info.value.refusals[0].reason


def test_jira_meta_count_mismatch_refuses_the_group(tmp_path, sources):
    _write_json(sources["jira"] / "meta.json", {"record_count": 5})
    with pytest.raises(BuildRefused, match="reports record_count=5"):
        build(load_config(write_config(tmp_path, sources, only=["jira"])), tmp_path / "out")


def test_a_missing_required_file_refuses_the_group(tmp_path, sources):
    (sources["cric"] / "facilities.json").unlink()
    with pytest.raises(BuildRefused, match="facilities.json is missing"):
        build(load_config(write_config(tmp_path, sources, only=["cric"])), tmp_path / "out")


@pytest.mark.parametrize(
    "content",
    [
        "jdoe@cern.ch wrote this".encode("utf-16-le"),  # NUL between letters
        b"---+ Page\nText\x00with a NUL.\n",
        b"---+ Caf\xe9 page\x00\n",  # Latin-1 does not excuse a NUL
    ],
    ids=["utf16", "nul", "latin1-with-nul"],
)
def test_text_with_nul_bytes_refuses_the_group(tmp_path, sources, content):
    (sources["twiki-eos"] / "Odd.txt").write_bytes(content)
    with pytest.raises(BuildRefused) as info:
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")
    reason = info.value.refusals[0].reason
    assert reason == "Odd.txt contains NUL bytes (not UTF-8 text)"


def test_non_utf8_text_without_a_fallback_refuses_the_group(tmp_path, sources):
    path = sources["cmssw-releases"] / "releases.map"
    path.write_bytes(path.read_bytes() + b"architecture=x;label=CMSSW_14_0_3;type=Caf\xe9;\n")
    with pytest.raises(BuildRefused, match="releases.map is not UTF-8"):
        build(
            load_config(write_config(tmp_path, sources, only=["cmssw-releases"])),
            tmp_path / "out",
        )


#: Plain text that keeps one control character under the 1% binary limit.
PADDING = b"Ordinary page text. " * 10 + b"\n"

REVIEWER_MIXED_PAGE = (
    "---+ Contacts\nCaf\u00e9 team: j\u00e9r\u00f4me.dupont@cern.ch, jdoe@c\u00e9rn.ch "
    "and jdoe\uff20cern\uff0ech.\n"
).encode("utf-8") + b"A stray Windows quote: \x93quoted.\n"


def test_a_stray_byte_does_not_garble_the_valid_utf8_around_it(tmp_path, sources):
    # The second review's page: mostly UTF-8, three addresses, one stray
    # cp1252 byte. Decoding the whole page as cp1252 leaked "j\u00c3\u00a9r\u00c3\u00b4".
    (sources["twiki-eos"] / "Mixed.txt").write_bytes(REVIEWER_MIXED_PAGE)
    out = tmp_path / "out"
    lock = build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), out)
    page = read_archive(out / "twiki-eos.tar.zst")["data/twiki-eos/Mixed.txt"].decode("utf-8")
    assert page == (
        "---+ Contacts\nCaf\u00e9 team: ,  and .\n"
        "A stray Windows quote: \u201cquoted.\n"
    )
    for fragment in ("dupont", "j\u00e9r\u00f4me", "jdoe", "c\u00e9rn", "\u00c3"):
        assert fragment not in page
    row = lock["groups"]["twiki-eos"]
    assert row["text_pages"] == 3
    assert row["pages_valid_utf8"] == 2
    assert row["pages_with_fallback_runs"] == 1
    assert row["fallback_bytes"] == {"cp1252": 1, "latin-1": 0}
    assert row["addresses_removed"] == 4  # three here, one planted elsewhere


def test_cp1252_and_latin1_runs_are_decoded_and_counted(tmp_path, sources):
    twiki = sources["twiki-eos"]
    # A whole Latin-1 page: its address has an accented local part.
    (twiki / "LatinPage.txt").write_bytes(
        b"---+ Contacts\nWrite to j\xe9r\xf4me.dupont@cern.ch today.\n"
    )
    # cp1252 smart quotes and an en dash, and one byte cp1252 leaves undefined.
    (twiki / "QuotePage.txt").write_bytes(
        b"---+ Quotes\nThe \x93golden\x94 JSON \x96 see notes \x81here.\n" + PADDING
    )
    out = tmp_path / "out"
    lock = build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), out)
    members = read_archive(out / "twiki-eos.tar.zst")
    assert members["data/twiki-eos/LatinPage.txt"].decode("utf-8") == (
        "---+ Contacts\nWrite to  today.\n"
    )
    assert members["data/twiki-eos/QuotePage.txt"].decode("utf-8") == (
        "---+ Quotes\nThe \u201cgolden\u201d JSON \u2013 see notes \x81here.\n"
        + PADDING.decode("ascii")
    )
    row = lock["groups"]["twiki-eos"]
    assert row["pages_with_fallback_runs"] == 2
    assert row["fallback_bytes"] == {"cp1252": 5, "latin-1": 1}


def test_an_address_split_by_a_c1_control_refuses_the_group(tmp_path, sources):
    # 0x81 is undefined in cp1252, so it decodes as the C1 control U+0081,
    # which hides a.b@cd.ch from the redactor.
    (sources["twiki-eos"] / "Split.txt").write_bytes(
        b"---+ Page\nMail a.b@c\x81d.ch now.\n" + PADDING
    )
    with pytest.raises(BuildRefused, match="an address is split by a control character"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_an_address_split_by_a_control_in_json_refuses_the_group(tmp_path, sources):
    path = sources["dqm"] / "records.json"
    records = json.loads(path.read_text())
    records[0]["filename"] = "owner a.b@c\u0001d.ch"
    path.write_text(json.dumps(records))
    with pytest.raises(BuildRefused, match="split by a control character"):
        build(load_config(write_config(tmp_path, sources, only=["dqm"])), tmp_path / "out")


@pytest.mark.parametrize(
    "content",
    [b"---+ Page\n" + b"\x01\x02 data " * 20, b"---+ Page\n" + b"\x81\x8d\x8f" * 10],
    ids=["c0-controls", "c1-from-fallback"],
)
def test_a_page_that_is_mostly_control_characters_is_refused_as_binary(
    tmp_path, sources, content
):
    (sources["twiki-eos"] / "Blob.txt").write_bytes(content)
    with pytest.raises(BuildRefused, match=r"Blob.txt looks binary: \d+ of \d+ characters"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_a_few_control_characters_are_allowed(tmp_path, sources):
    (sources["twiki-eos"] / "Tabs.txt").write_bytes(
        b"---+ Page\n" + b"x" * 200 + b"\x0c\n\ttab\r\n"
    )
    build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_utf8_only_twiki_reports_no_fallback(built):
    _, lock = built
    row = lock["groups"]["twiki-eos"]
    assert row["pages_valid_utf8"] == row["text_pages"] == 2
    assert row["pages_with_fallback_runs"] == 0
    assert row["fallback_bytes"] == {"cp1252": 0, "latin-1": 0}


def test_cmssw_group_accepts_records_json_when_the_map_is_absent(tmp_path, sources):
    group = sources["cmssw-releases"]
    (group / "releases.map").unlink()
    _write_json(
        group / "records.json",
        [
            {"label": "CMSSW_14_0_1", "type": "Production", "state": "Announced",
             "architecture": ["el8_amd64_gcc12"], "release_notes": "Notes by rel.manager@cern.ch",
             "download_count": 7},
            {"label": "CMSSW_14_0_2", "type": "Production", "state": "Announced"},
        ],
    )
    (group / "meta.json").write_text('{"record_count": 2}')
    out = tmp_path / "out"
    lock = build(
        load_config(write_config(tmp_path, sources, only=["cmssw-releases"])), out
    )
    row = lock["groups"]["cmssw-releases"]
    assert row["input_file"] == "records.json"
    assert row["record_count"] == 2 and row["addresses_removed"] == 1
    members = read_archive(out / "cmssw-releases.tar.zst")
    assert sorted(members) == ["data/cmssw-releases/records.json"]
    records = json.loads(members["data/cmssw-releases/records.json"])
    assert "download_count" not in records[0]
    assert verify(out / LOCK_NAME, out).ok


def test_cmssw_group_prefers_the_map_when_both_exist(tmp_path, sources):
    _write_json(sources["cmssw-releases"] / "records.json", [{"label": "CMSSW_1_0_0"}])
    out = tmp_path / "out"
    lock = build(
        load_config(write_config(tmp_path, sources, only=["cmssw-releases"])), out
    )
    assert lock["groups"]["cmssw-releases"]["input_file"] == "releases.map"
    assert sorted(read_archive(out / "cmssw-releases.tar.zst")) == [
        "data/cmssw-releases/releases.map"
    ]


def test_cmssw_group_with_neither_input_is_refused(tmp_path, sources):
    (sources["cmssw-releases"] / "releases.map").unlink()
    with pytest.raises(BuildRefused, match="neither releases.map nor records.json"):
        build(
            load_config(write_config(tmp_path, sources, only=["cmssw-releases"])),
            tmp_path / "out",
        )


def test_malformed_cmssw_records_refuse_the_group(tmp_path, sources):
    (sources["cmssw-releases"] / "releases.map").unlink()
    _write_json(sources["cmssw-releases"] / "records.json", [{"type": "Production"}])
    with pytest.raises(BuildRefused, match="record 0 is malformed: record has no label"):
        build(
            load_config(write_config(tmp_path, sources, only=["cmssw-releases"])),
            tmp_path / "out",
        )


def test_wmstats_dn_is_removed_at_any_depth_even_when_its_field_is_kept(tmp_path, sources):
    config = write_config(
        tmp_path,
        sources,
        only=["wmstats"],
        extra={"wmstats": {"keep_fields": ["RequestTransition", "DN", "RequestorDN"]}},
    )
    out = tmp_path / "out"
    build(load_config(config), out)
    record = json.loads(
        read_archive(out / "wmstats.tar.zst")["data/wmstats-workflows/records.json"]
    )[0]
    assert record["RequestTransition"] == [{"Status": "new"}]
    assert "RequestorDN" not in record
    assert DN.encode() not in _all_bytes(out, "wmstats")


# --- what the builder will not read ---------------------------------------------


def test_a_symlinked_text_file_refuses_the_group(tmp_path, sources):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret page\n")
    (sources["twiki-eos"] / "link.txt").symlink_to(outside)
    with pytest.raises(BuildRefused, match="link.txt: symbolic link"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_a_symlinked_directory_refuses_the_group(tmp_path, sources):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "Page.txt").write_text("page\n")
    (sources["twiki-eos"] / "Linked").symlink_to(other, target_is_directory=True)
    with pytest.raises(BuildRefused, match="Linked: symbolic link"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_a_symlink_that_is_not_text_still_refuses_the_group(tmp_path, sources):
    (sources["twiki-eos"] / "image.png").symlink_to(tmp_path / "missing.png")
    with pytest.raises(BuildRefused, match="image.png: symbolic link"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


@pytest.mark.parametrize(
    "rel", [".cookies/hypernews.txt", ".Hidden.txt", "Sub/.git/HEAD"]
)
def test_a_dot_path_refuses_the_group(tmp_path, sources, rel):
    path = sources["twiki-eos"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Netscape HTTP Cookie File\n")
    with pytest.raises(BuildRefused, match="hidden path"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_a_symlinked_json_file_refuses_the_group(tmp_path, sources):
    real = tmp_path / "real-records.json"
    (sources["dqm"] / "records.json").rename(real)
    (sources["dqm"] / "records.json").symlink_to(real)
    with pytest.raises(BuildRefused, match="records.json: symbolic link"):
        build(load_config(write_config(tmp_path, sources, only=["dqm"])), tmp_path / "out")


def test_a_dangling_json_symlink_is_refused_not_called_missing(tmp_path, sources):
    (sources["jira"] / "meta.json").unlink()
    (sources["jira"] / "meta.json").symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(BuildRefused, match="meta.json: symbolic link"):
        build(load_config(write_config(tmp_path, sources, only=["jira"])), tmp_path / "out")


def test_a_symlinked_single_text_file_refuses_the_group(tmp_path, sources):
    real = tmp_path / "releases.map"
    (sources["cmssw-releases"] / "releases.map").rename(real)
    (sources["cmssw-releases"] / "releases.map").symlink_to(real)
    with pytest.raises(BuildRefused, match="releases.map: symbolic link"):
        build(
            load_config(write_config(tmp_path, sources, only=["cmssw-releases"])),
            tmp_path / "out",
        )


def test_the_configured_directory_itself_may_be_a_link(tmp_path, sources):
    alias = tmp_path / "dqm-alias"
    alias.symlink_to(sources["dqm"], target_is_directory=True)
    config = tmp_path / "c.yaml"
    config.write_text(
        yaml.safe_dump(
            {"snapshot": "s", "groups": {"dqm": {"path": str(alias), "collected": "2026-06-12"}}}
        )
    )
    build(load_config(config), tmp_path / "out")


def test_an_address_in_a_file_name_refuses_the_group(tmp_path, sources):
    (sources["twiki-eos"] / "jdoe@cern.ch.txt").write_text("---+ Page\nText.\n")
    with pytest.raises(BuildRefused, match="a file path contains an email address"):
        build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), tmp_path / "out")


def test_an_unreadable_file_is_a_named_refusal_and_other_groups_are_reported(
    tmp_path, sources, capsys
):
    records = sources["dqm"] / "records.json"
    records.chmod(0)
    try:
        if os.access(records, os.R_OK):
            pytest.skip("running with privileges that ignore file modes")
        _write_json(sources["indico"] / "records.json", [{"title": "no id"}])
        config = write_config(tmp_path, sources, only=["dqm", "indico", "jira"])
        code = cli_main(["build", "--config", str(config), "--out", str(tmp_path / "o")])
    finally:
        records.chmod(0o644)
    err = capsys.readouterr().err
    assert code == 2
    assert "2 group(s) refused" in err
    assert "group 'dqm' refused: cannot read its cache: PermissionError" in err
    assert "group 'indico' refused" in err
    assert "Traceback" not in err


def test_json_nested_too_deeply_is_a_named_refusal(tmp_path, sources):
    depth = 100_000
    (sources["dqm"] / "records.json").write_text("[" * depth + "]" * depth)
    with pytest.raises(BuildRefused, match="nested too deeply"):
        build(load_config(write_config(tmp_path, sources, only=["dqm"])), tmp_path / "out")


def test_lock_counts_the_addresses_removed(built):
    _, lock = built
    counts = {name: row["addresses_removed"] for name, row in lock["groups"].items()}
    # jira: 2 planted + 1 emailAddress field; indico: chair email + 3 texts;
    # docs: body + last_editor_email; cric: facility name + contact_email.
    assert counts["jira"] == 3
    assert counts["indico"] == 4
    assert counts["gitlab-docs"] == counts["docsite"] == 2
    assert counts["cric"] == 2
    assert counts["twiki-eos"] == 1
    assert counts["cmssw-releases"] == 0
    assert all(isinstance(v, int) for v in counts.values())


# --- determinism and verification ---------------------------------------------


def test_archives_are_byte_identical_across_two_runs(tmp_path, sources):
    config = load_config(write_config(tmp_path, sources))
    first = build(config, tmp_path / "one")
    second = build(config, tmp_path / "two")
    for name in groups_module.GROUPS:
        a = (tmp_path / "one" / f"{name}.tar.zst").read_bytes()
        b = (tmp_path / "two" / f"{name}.tar.zst").read_bytes()
        assert a == b, name
    assert first["groups"] == second["groups"]


def test_archive_members_have_fixed_metadata(built):
    out, _ = built
    raw = subprocess.run(
        ["zstd", "-d", "-q", "-c", str(out / "jira.tar.zst")], capture_output=True, check=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        infos = tar.getmembers()
    assert [i.name for i in infos] == sorted(i.name for i in infos)
    assert {(i.mtime, i.uid, i.gid, i.uname, i.gname, i.mode) for i in infos} == {
        (946684800, 0, 0, "", "", 0o644)
    }


def test_verify_fails_on_one_flipped_byte(built):
    out, _ = built
    archive = out / "dqm.tar.zst"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0x01
    archive.write_bytes(bytes(data))
    result = verify(out / LOCK_NAME, out)
    assert not result.ok
    failed = [line for line in result.lines if line.startswith("FAIL")]
    assert len(failed) == 1 and failed[0].startswith("FAIL dqm:") and "SHA-256" in failed[0]


def test_verify_reports_missing_and_resized_archives(built):
    out, _ = built
    (out / "dbs.tar.zst").unlink()
    with (out / "jira.tar.zst").open("ab") as handle:
        handle.write(b"\0")
    result = verify(out / LOCK_NAME, out)
    assert not result.ok
    assert "FAIL dbs: dbs.tar.zst is missing" in result.lines
    assert any(line.startswith("FAIL jira:") and "bytes" in line for line in result.lines)


def test_verify_checks_unpacked_contents_against_the_lock(built):
    out, lock = built
    lock["groups"]["indico"]["contents_sha256"] = "0" * 64
    (out / LOCK_NAME).write_text(yaml.safe_dump(lock), encoding="utf-8")
    result = verify(out / LOCK_NAME, out)
    assert "FAIL indico: unpacked contents do not match the lock's contents_sha256" in result.lines


def test_build_refuses_a_non_empty_output_directory(tmp_path, sources):
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(SnapshotError, match="not empty"):
        build(load_config(write_config(tmp_path, sources, only=["dqm"])), out)


# --- config -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "groups, expected",
    [
        ({"jira": {"path": "x"}}, "'collected' (the collection date) is required"),
        ({"jira": {"path": "x", "collected": "Aug 31"}}, "must be YYYY-MM-DD"),
        ({"jira": {"path": "x", "collected": "2026-06-16..2026-06-12"}}, "ends before"),
        ({"jira": {"path": "x", "collected": "2026-02-30"}}, "day is out of range"),
        ({"hypernews": {"path": "x", "collected": "2026-08-31"}}, "unknown group"),
        ({"jira": {"path": "x", "collected": "2026-08-31", "dropfields": []}}, "unknown keys"),
        ({"jira": {"collected": "2026-08-31"}}, "'path' is required"),
        ({"jira": {"path": "x", "collected": "2026-08-31", "drop_fields": "a"}}, "list of field"),
        ({}, "at least one group"),
    ],
)
def test_config_errors_are_named(groups, expected):
    with pytest.raises(SnapshotError, match=expected.replace("(", r"\(").replace(")", r"\)")):
        parse_config({"snapshot": "s", "groups": groups}, base=Path("/tmp"))


def test_config_accepts_yaml_dates_and_relative_paths(tmp_path):
    config = tmp_path / "c.yaml"
    config.write_text(
        "snapshot: cms-cache-20260928\n"
        "groups:\n"
        "  jira: {path: raw/jira, collected: 2026-08-31}\n"
        "  indico: {path: /abs/indico, collected: '2026-06-12..2026-06-16'}\n",
        encoding="utf-8",
    )
    parsed = load_config(config)
    rows = {g.name: g for g in parsed.groups}
    assert rows["jira"].collected == "2026-08-31"
    assert rows["jira"].path == tmp_path / "raw" / "jira"
    assert rows["indico"].collected == "2026-06-12..2026-06-16"


# --- command line -------------------------------------------------------------


def test_cli_build_then_verify_then_tamper(tmp_path, sources, capsys):
    config = write_config(tmp_path, sources, only=["jira", "indico"])
    out = tmp_path / "cli-out"
    assert cli_main(["build", "--config", str(config), "--out", str(out)]) == 0
    assert "jira: 2 records, 2 files, collected 2026-08-31" in capsys.readouterr().out
    assert cli_main(["verify", "--lock", str(out / LOCK_NAME), "--archives", str(out)]) == 0
    archive = out / "indico.tar.zst"
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    assert cli_main(["verify", "--lock", str(out / LOCK_NAME), "--archives", str(out)]) == 1
    assert "FAIL indico" in capsys.readouterr().out


def test_cli_build_refusal_exits_2_and_names_the_group(tmp_path, sources, capsys):
    _write_json(sources["dqm"] / "records.json", [{"filename": "x"}])
    config = write_config(tmp_path, sources, only=["dqm", "jira"])
    assert cli_main(["build", "--config", str(config), "--out", str(tmp_path / "o")]) == 2
    err = capsys.readouterr().err
    assert "group 'dqm' refused" in err and "no snapshot written" in err


def test_the_counting_redactor_matches_the_plain_one():
    from archi.enrichment.anonymizer import (
        redact_email_addresses,
        redact_email_addresses_with_count,
    )

    for text, count in (
        ("no address here, AT&amp;T", 0),
        ("mail a@cern.ch or b@fnal.gov", 2),
        ("|bob@cern.ch|alice@fnal.gov|", 2),
        ("numpy@1.26.4 stays", 0),
    ):
        clean, removed = redact_email_addresses_with_count(text)
        assert clean == redact_email_addresses(text)
        assert removed == count


# --- a valid-looking file that is really another export --------------------------

SITES_COMPAT = {
    "desc": {"columns": ["site_name", "tier_level", "tier", "country", "usage"]},
    "result": [["T2_US_MIT", 2, "T2", "US", "production"]],
}


def test_a_sites_compat_export_as_responsibilities_refuses_cric(tmp_path, sources):
    _write_json(sources["cric"] / "responsibilities.json", SITES_COMPAT)
    with pytest.raises(BuildRefused) as info:
        build(load_config(write_config(tmp_path, sources, only=["cric"])), tmp_path / "out")
    [refusal] = info.value.refusals
    assert refusal.group == "cric"
    assert refusal.reason.startswith("responsibilities.json: columns are ['site_name'")
    assert "sites-compat" in refusal.reason


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([["T2_US_MIT", 2, "T2", "US", "production"]], "row 0 has 5 fields, expected 3"),
        ([["adalove", "MIT Bates"]], "row 0 has 2 fields"),
        ([{"username": "adalove"}], "row 0 has dict fields"),
        ([["adalove", 7, "Site Admin"]], "site title must be a string or null"),
        ([[None, "MIT Bates", "Site Admin"]], "username and role must be strings"),
    ],
)
def test_responsibility_rows_of_the_wrong_shape_refuse_cric(tmp_path, sources, rows, expected):
    # No header, so only the rows can show the file is wrong.
    _write_json(sources["cric"] / "responsibilities.json", {"result": rows})
    with pytest.raises(BuildRefused, match=expected):
        build(load_config(write_config(tmp_path, sources, only=["cric"])), tmp_path / "out")


CRIC_EXPORTS = {
    ("cric", "sites.json"): "sitedb_title",
    ("cric", "storage_units.json"): "pledged-CMS",
    ("cric", "compute_units.json"): "potential_max",
    ("cric", "facilities.json"): "cmssites",
    ("cric-core", "services.json"): "rcsite",
    ("cric-core", "rcsites.json"): "sites",
    ("cric-core", "federations.json"): "accounting_name",
}
SIBLING_SWAPS = [
    (target, donor) for target in CRIC_EXPORTS for donor in CRIC_EXPORTS if donor != target
]


@pytest.mark.parametrize(
    "target, donor", SIBLING_SWAPS, ids=[f"{t[1]}<-{d[1]}" for t, d in SIBLING_SWAPS]
)
def test_every_cric_file_swapped_for_any_sibling_export_is_refused(
    tmp_path, sources, target, donor
):
    group, name = target
    (sources[group] / name).write_bytes((sources[donor[0]] / donor[1]).read_bytes())
    key = CRIC_EXPORTS[target]
    with pytest.raises(BuildRefused) as info:
        build(load_config(write_config(tmp_path, sources, only=[group])), tmp_path / "out")
    assert info.value.refusals[0].reason.startswith(
        f"{name}: only 0 of 1 records carry a {key} key (95% needed)"
    )


def _services(count_with, count_without):
    services = {
        f"ce{i}.example.org": {"type": "CE", "endpoint": f"ce{i}.example.org", "rcsite": "MIT"}
        for i in range(count_with)
    }
    services.update(
        {f"odd{i}.example.org": {"type": "CE", "endpoint": "x"} for i in range(count_without)}
    )
    return services


def test_a_few_records_without_the_signature_key_are_accepted(tmp_path, sources):
    _write_json(sources["cric-core"] / "services.json", _services(19, 1))  # 95%
    build(load_config(write_config(tmp_path, sources, only=["cric-core"])), tmp_path / "out")


def test_too_many_records_without_the_signature_key_are_refused(tmp_path, sources):
    _write_json(sources["cric-core"] / "services.json", _services(18, 2))  # 90%
    with pytest.raises(BuildRefused, match="only 18 of 20 records carry a rcsite key"):
        build(load_config(write_config(tmp_path, sources, only=["cric-core"])), tmp_path / "out")


def test_an_empty_export_is_refused(tmp_path, sources):
    _write_json(sources["cric-core"] / "services.json", {})
    with pytest.raises(BuildRefused, match="services.json holds no records"):
        build(load_config(write_config(tmp_path, sources, only=["cric-core"])), tmp_path / "out")


@pytest.mark.parametrize(
    "group, record, expected",
    [
        ("jira", {"key": "not an issue key", "summary": "x"}, "only 2 of 3 records carry a key"),
        ("indico", {"id": "9", "title": "no derived text"}, "_contributions_text / _pdf_texts"),
        ("conddb-global-tags", {"name": "GT", "description": "x"}, "release / scenario"),
        ("dbs", {"dataset": "not-a-dataset-path"}, "dataset_name / dataset matching"),
    ],
)
def test_a_record_from_another_export_is_refused(tmp_path, sources, group, record, expected):
    path = sources[group] / "records.json"
    path.write_text(json.dumps(json.loads(path.read_text()) + [record]))
    if group == "jira":
        _write_json(sources["jira"] / "meta.json", {"record_count": 3})
    with pytest.raises(BuildRefused, match=expected):
        build(load_config(write_config(tmp_path, sources, only=[group])), tmp_path / "out")


def test_cmssw_records_with_non_release_labels_are_refused(tmp_path, sources):
    (sources["cmssw-releases"] / "releases.map").unlink()
    _write_json(sources["cmssw-releases"] / "records.json", [{"label": "ECALTBH4_0_2_2"}])
    with pytest.raises(BuildRefused, match=r"only 0 of 1 records carry a label matching CMSSW_"):
        build(
            load_config(write_config(tmp_path, sources, only=["cmssw-releases"])),
            tmp_path / "out",
        )


# --- temporary copies -------------------------------------------------------------


@pytest.fixture
def system_tmp(tmp_path, monkeypatch):
    """A stand-in for the system temp dir, to prove nothing lands there."""
    sentinel = tmp_path / "system-tmp"
    sentinel.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(sentinel))
    return sentinel


def test_reader_check_copies_go_to_tmp_dir_and_are_removed(tmp_path, sources, system_tmp):
    work = tmp_path / "work-tmp"
    work.mkdir()
    seen = []
    real = tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        handle = real(*args, **kwargs)
        seen.append(Path(handle.name))
        return handle

    import archi.snapshot.builder as builder_module

    original = builder_module.tempfile.TemporaryDirectory
    builder_module.tempfile.TemporaryDirectory = spy
    try:
        build(load_config(write_config(tmp_path, sources)), tmp_path / "out", tmp_dir=work)
    finally:
        builder_module.tempfile.TemporaryDirectory = original
    assert len(seen) == len(groups_module.GROUPS)
    assert all(path.parent == work for path in seen)
    assert list(work.iterdir()) == []
    assert list(system_tmp.iterdir()) == []


def test_tmp_dir_is_emptied_on_refusal(tmp_path, sources, system_tmp):
    work = tmp_path / "work-tmp"
    work.mkdir()
    config = write_config(
        tmp_path, sources, only=["jira", "dqm"], extra={"jira": {"drop_fields": ["description"]}}
    )
    with pytest.raises(BuildRefused, match="changed what the reader emits"):
        build(load_config(config), tmp_path / "out", tmp_dir=work)
    assert list(work.iterdir()) == []
    assert list(system_tmp.iterdir()) == []
    assert not (tmp_path / "out").exists()


def _spy_tempdirs(monkeypatch):
    import archi.snapshot.builder as builder_module

    seen = []
    real = tempfile.TemporaryDirectory

    def spy(*args, **kwargs):
        handle = real(*args, **kwargs)
        seen.append(Path(handle.name))
        return handle

    monkeypatch.setattr(builder_module.tempfile, "TemporaryDirectory", spy)
    return seen


def test_tmp_dir_defaults_to_the_system_temp_dir(tmp_path, sources, system_tmp, monkeypatch):
    seen = _spy_tempdirs(monkeypatch)
    build(load_config(write_config(tmp_path, sources, only=["dqm", "jira"])), tmp_path / "out")
    assert len(seen) == 2
    assert all(path.parent == system_tmp for path in seen)
    assert list(system_tmp.iterdir()) == []


def test_a_full_tmp_dir_is_reported_as_a_write_failure(tmp_path, sources, monkeypatch):
    import errno as errno_module

    import archi.snapshot.builder as builder_module

    def full(root, files):
        raise OSError(errno_module.ENOSPC, "No space left on device")

    monkeypatch.setattr(builder_module, "_stage", full)
    with pytest.raises(BuildRefused) as info:
        build(load_config(write_config(tmp_path, sources, only=["dqm"])), tmp_path / "out")
    reason = info.value.refusals[0].reason
    assert reason.startswith("cannot write the reader-check copy in ")
    assert "(disk full?)" in reason and "cannot read its cache" not in reason


def _sigterm_during(monkeypatch, attribute):
    import archi.snapshot.builder as builder_module

    real = getattr(builder_module, attribute)
    fired = []

    def wrapper(*args, **kwargs):
        if not fired:
            fired.append(True)
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # the handler raises before this returns
        return real(*args, **kwargs)

    monkeypatch.setattr(builder_module, attribute, wrapper)


@pytest.mark.parametrize("attribute", ["_run_reader", "write_archive"])
def test_sigterm_cleans_tmp_dir_and_output_staging(
    tmp_path, sources, system_tmp, monkeypatch, attribute
):
    work = tmp_path / "work-tmp"
    work.mkdir()
    _sigterm_during(monkeypatch, attribute)
    config = write_config(tmp_path, sources, only=["dqm", "jira"])
    before = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit) as info:
        cli_main(["build", "--config", str(config), "--out", str(tmp_path / "o"),
                  "--tmp-dir", str(work)])
    assert info.value.code == 128 + signal.SIGTERM
    assert list(work.iterdir()) == []
    assert list(system_tmp.iterdir()) == []
    assert not (tmp_path / "o").exists()
    assert not list(tmp_path.glob(".o.*"))  # the output staging directory
    assert signal.getsignal(signal.SIGTERM) is before


def test_cli_tmp_dir_must_exist(tmp_path, sources, capsys):
    config = write_config(tmp_path, sources, only=["dqm"])
    code = cli_main(
        ["build", "--config", str(config), "--out", str(tmp_path / "o"),
         "--tmp-dir", str(tmp_path / "no-such-dir")]
    )
    assert code == 2
    assert "is not a directory" in capsys.readouterr().err


# --- per-file dates ---------------------------------------------------------------


def test_file_dates_are_carried_into_the_lock(tmp_path, sources):
    # Jason, 2026-09-28: the June CRIC set, files dated 06-12, facilities
    # fetched 04-09; the lock records both.
    config = write_config(
        tmp_path,
        sources,
        only=["cric"],
        extra={"cric": {"collected": "2026-06-12", "file_dates": {"facilities.json": "2026-04-09"}}},
    )
    lock = build(load_config(config), tmp_path / "out")
    row = lock["groups"]["cric"]
    assert row["collected"] == "2026-06-12"
    assert row["file_dates"] == {"facilities.json": "2026-04-09"}
    on_disk = yaml.safe_load((tmp_path / "out" / LOCK_NAME).read_text())
    assert on_disk["groups"]["cric"]["file_dates"] == {"facilities.json": "2026-04-09"}


def test_file_dates_must_name_a_file_the_group_archives(tmp_path, sources):
    config = write_config(
        tmp_path, sources, only=["cric"], extra={"cric": {"file_dates": {"meta.json": "2026-04-09"}}}
    )
    with pytest.raises(BuildRefused, match="file_dates names meta.json"):
        build(load_config(config), tmp_path / "out")


@pytest.mark.parametrize("value", [{"facilities.json": "April 9"}, [], {}])
def test_file_dates_must_be_dates(value):
    with pytest.raises(SnapshotError, match="file_dates|YYYY-MM-DD"):
        parse_config(
            {"snapshot": "s", "groups": {"cric": {"path": "x", "collected": "2026-06-12",
                                                    "file_dates": value}}},
            base=Path("/tmp"),
        )
