"""req.compops-snapshot-builder -- build and verify a cache snapshot, offline.

Every fixture is synthetic and tiny. Each text-bearing group carries one
planted address (``PLANTED``) that must not survive into any archive.
"""
from __future__ import annotations

import dataclasses
import io
import json
import subprocess
import tarfile
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
        {"MIT-CE1": {"corepower": 11.0, "state": "ACTIVE"}},
    )
    _write_json(
        dirs["cric"] / "responsibilities.json",
        {"result": [["adalove", "MIT Bates", "Site Executive"]], "status": "ok"},
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
        "result": [["adalove", "MIT Bates", "Site Executive"]]
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
    assert lock["groups"]["wmstats"]["dropped_fields"] == ["RequestorDN"]
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
    assert lock["groups"]["wmstats"]["dropped_fields"] == ["Requestor", "RequestorDN"]


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
    path = sources["jira"] / "records.json"
    records = json.loads(path.read_text()) + [{"key": "bob@cern.ch", "summary": "x"}]
    path.write_text(json.dumps(records))
    with pytest.raises(BuildRefused, match="no key / issue_key"):
        build(load_config(write_config(tmp_path, sources, only=["jira"])), tmp_path / "out")


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


def test_non_utf8_twiki_page_is_stored_as_the_reader_decodes_it(tmp_path, sources):
    (sources["twiki-eos"] / "Latin.txt").write_bytes(b"---+ Caf\xe9 page\nText.\n")
    out = tmp_path / "out"
    lock = build(load_config(write_config(tmp_path, sources, only=["twiki-eos"])), out)
    page = read_archive(out / "twiki-eos.tar.zst")["data/twiki-eos/Latin.txt"]
    assert page == "---+ Caf� page\nText.\n".encode("utf-8")
    assert lock["groups"]["twiki-eos"]["files_decoded_with_replacement"] == 1


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
