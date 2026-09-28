"""The bundle's sources must run through the substrate's adapter contract.

Since the connector migration the readers return ``ConnectorRun``, which the
substrate runner cannot consume: it reads ``next_cursor``, a field that result
deliberately omits. A registry that names a reader directly fails every run
with ``AttributeError: 'ConnectorRun' object has no attribute 'next_cursor'``,
which is what an installed instance did until the bundle named adapters.

These tests hold that line: every source this bundle ships names a
``ConnectorAdapter`` whose declared profile matches the registry entry, and the
three cache-backed sources run from their shipped parameters and return a
result carrying the substrate's own fields.
"""

import ast
import hashlib
import importlib
import inspect
import json
import os
import re
import textwrap
from pathlib import Path

import pytest
import yaml
from okg.deployment import ConnectorAdapter, EdgeFact, NodeFact

SOURCE_DEFAULTS = (
    Path(__file__).resolve().parents[2] / "bundles" / "cern-team" / "source-defaults"
)


def _bundle_entries():
    for path in sorted(SOURCE_DEFAULTS.iterdir()):
        if not path.is_file():
            continue
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, entry in body.items():
            if isinstance(entry, dict) and str(entry.get("module", "")).startswith(
                "archi."
            ):
                yield path.name, name, entry


ENTRIES = list(_bundle_entries())
ENTRY_IDS = [f"{filename}-{name}" for filename, name, _ in ENTRIES]


def _all_bundle_entries():
    """Every source this bundle ships, whoever owns the adapter.

    Unlike `_bundle_entries` this does NOT filter to `archi.` modules: the
    profile-tuple contract is the substrate's, so it binds the entries that
    name a substrate class (github_repo, gitlab_repo) exactly as it binds
    ours. `.yaml.example` files are included — an operator enables one by
    renaming it, and an illegal tuple blocks their install just the same.
    """
    for path in sorted(SOURCE_DEFAULTS.iterdir()):
        if not path.is_file():
            continue
        body = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for name, entry in body.items():
            if isinstance(entry, dict) and entry.get("module"):
                yield path.name, name, entry


ALL_ENTRIES = list(_all_bundle_entries())
ALL_ENTRY_IDS = [f"{filename}-{name}" for filename, name, _ in ALL_ENTRIES]


def _adapter_class(entry):
    module = importlib.import_module(entry["module"])
    return getattr(module, entry["class"])


@pytest.mark.parametrize(("filename", "name", "entry"), ENTRIES, ids=ENTRY_IDS)
def test_every_bundle_source_names_a_runnable_adapter(filename, name, entry):
    cls = _adapter_class(entry)
    assert issubclass(cls, ConnectorAdapter), (
        f"{filename}: {name} names {entry['class']}, which the substrate runner "
        "cannot drive; register the reader's ConnectorAdapter instead"
    )
    # The substrate reads both off the class, without constructing it.
    assert inspect.getattr_static(cls, "profile") == entry["source_class"]
    assert isinstance(inspect.getattr_static(cls, "change_probe_kind"), str)


def _class_level_constant(source_path, class_name, attr):
    """Return a class-level ``attr`` only when it is a string LITERAL.

    This is the substrate's own acceptance rule, reimplemented so the test
    proves it without depending on a private okg symbol: okg's
    ``substrate/deployment_lint.py`` reads ``change_probe_kind`` by parsing
    this file (``_class_level_str_attr``), and accepts only an
    ``ast.Constant`` whose value is a ``str``. Anything else -- notably
    ``profile = SomeReader.profile``, which parses as an ``ast.Attribute`` --
    reads as absent and fails the source-registry lint with
    ``deployment.source_registry.probe_missing``.
    """
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == class_name):
            continue
        for stmt in node.body:
            targets = []
            if isinstance(stmt, ast.Assign):
                targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                targets = [stmt.target]
            if not any(t.id == attr for t in targets):
                continue
            value = stmt.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            raise AssertionError(
                f"{class_name}.{attr} is {ast.dump(value)}, not a string literal; "
                "the substrate parses this file and accepts only a literal, so a "
                "reference to the reader's attribute reads as absent"
            )
    raise AssertionError(f"{class_name} declares no class-level {attr}")


@pytest.mark.parametrize(("filename", "name", "entry"), ENTRIES, ids=ENTRY_IDS)
def test_adapter_authority_is_a_literal_that_matches_its_reader(filename, name, entry):
    """Both attributes must be literals, and must not drift from the reader.

    A literal cannot be computed from the reader, so nothing but this test
    keeps the two in step: if a reader's profile or probe kind changes and the
    adapter's literal does not, the registry entry and the reader disagree and
    the substrate refuses the source (``source_class_profile_mismatch``) or
    probes the wrong way.
    """
    cls = _adapter_class(entry)
    source_path = Path(importlib.import_module(entry["module"]).__file__)
    reader = inspect.getattr_static(cls, "reader_class")
    for attr in ("profile", "change_probe_kind"):
        literal = _class_level_constant(source_path, entry["class"], attr)
        assert literal == getattr(reader, attr), (
            f"{filename}: {entry['class']}.{attr} is {literal!r} but "
            f"{reader.__name__}.{attr} is {getattr(reader, attr)!r}"
        )
        # And the literal is what the class actually exposes.
        assert inspect.getattr_static(cls, attr) == literal


def test_every_shipped_source_default_declares_a_legal_profile_tuple():
    """No source-default may ship a tuple the substrate would refuse.

    `indico.yaml.example` shipped `source_class: discovery_crawl` with
    `record_identity_kind: remote_id` — a crossbreed of jira's identity half
    and docsite's revision half. The substrate allows `remote_id` only under
    `mutable_api`, so `okg install` refused with
    `deployment.source_registry.profile_invalid` and an operator who enabled
    Indico could not install at all. Nothing caught it because the tuple only
    reaches lint once the file has been renamed to `.yaml`.

    This calls the substrate's own `validate_profile_tuple` — the same
    function `deployment_lint` calls — rather than restating the matrix, so
    the test tracks the SDK instead of drifting from it. It is checked in one
    body rather than parametrized so a bad tuple reports every offender at
    once, not just the first.
    """
    from okg.substrate.sources.profiles import (
        PROFILE_NAMES,
        validate_profile_tuple,
    )

    failures = []
    for filename, name, entry in ALL_ENTRIES:
        source_class = entry.get("source_class")
        try:
            if source_class not in PROFILE_NAMES:
                raise ValueError(
                    f"unknown source_class {source_class!r}; "
                    f"valid profiles: {sorted(PROFILE_NAMES)}"
                )
            validate_profile_tuple(
                source_class=str(source_class),
                record_identity_kind=entry.get("record_identity_kind"),
                source_revision_kind=entry.get("source_revision_kind"),
                deletion_semantics=entry.get("deletion_semantics"),
                publication_mode=entry.get("publication_mode"),
            )
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"{filename} [{name}]: {exc}")
    assert not failures, (
        "these source-defaults would fail `okg install` with "
        "deployment.source_registry.profile_invalid:\n\n"
        + "\n\n".join(failures)
    )


def test_every_placeholder_has_an_install_answer_behind_it():
    """A `${...}` with no init_question makes its source uninstallable.

    `build_plan` interpolates every `*.yaml` in source-defaults/ against the
    validated answers, and `interpolate` refuses an unknown reference
    outright (okg `substrate/catalog/profile.py` `replace_match`) rather than
    leaving it verbatim. So a placeholder the profile never asks about does
    not degrade -- it aborts the whole install with `ProfileError: undefined
    variable reference`. That is what `cmssw_releases_frozen.yaml.example`
    did: it referenced `${cmssw_map_path}` and `${cmssw_map_digest}`, which
    no init_question declared, so enabling frozen mode was impossible without
    hand-editing the shipped file.

    The `.example` files are checked too: renaming one is exactly how an
    operator enables it, and that is when the reference has to resolve.

    `deployment_name` and `HOME` are injected by `build_plan` itself rather
    than declared as questions.
    """
    profile = yaml.safe_load(
        (SOURCE_DEFAULTS.parent / "profile.yaml").read_text(encoding="utf-8")
    )
    known = {q["id"] for q in profile["init_questions"]} | {"deployment_name", "HOME"}
    pattern = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    missing = {}
    for path in sorted(SOURCE_DEFAULTS.iterdir()):
        if not path.is_file():
            continue
        unknown = sorted(set(pattern.findall(path.read_text(encoding="utf-8"))) - known)
        if unknown:
            missing[path.name] = unknown
    assert not missing, (
        "these source-defaults reference install answers the profile never "
        f"asks for, so enabling them aborts `okg install`: {missing}"
    )


def test_the_frozen_cmssw_example_replaces_the_live_default():
    """Both files declare one source id, so enabling frozen is a swap.

    The installer keys source-defaults by the id inside the file, not by
    filename, so leaving both in place lets one silently win and discards the
    other. Nothing in okg warns about it. This test pins the collision as a
    known fact and fails if someone renames one of the ids and leaves the
    'replaces, does not add' instructions in the header saying otherwise.
    """
    live = yaml.safe_load(
        (SOURCE_DEFAULTS / "cmssw_releases.yaml").read_text(encoding="utf-8")
    )
    frozen_text = (
        SOURCE_DEFAULTS / "cmssw_releases_frozen.yaml.example"
    ).read_text(encoding="utf-8")
    frozen = yaml.safe_load(frozen_text)
    assert set(live) == set(frozen) == {"cmssw_releases"}
    assert "REPLACES cmssw_releases.yaml" in frozen_text, (
        "the collision is only survivable if the header says so"
    )
    # Frozen mode means reviewed bytes, never a live fetch.
    assert frozen["cmssw_releases"]["params"]["fetch"] is False
    assert live["cmssw_releases"]["params"]["fetch"] is True


def test_the_indico_record_key_is_the_locator_it_declares():
    """The identity kind and the emitted record key must agree.

    Declaring `scoped_locator` while emitting the upstream event id would
    fix the lint and keep the lie. The reader emits a scope-relative path,
    so this holds the two together.
    """
    from archi.sources.indico import IndicoEventRecord, _meeting_node

    record = IndicoEventRecord(
        event_id="654321", title="Weekly ops", url="", description="",
        date="", end_date="", event_type="", category="", category_id=None,
    )
    node = _meeting_node(record, {"run_id": "r1"})
    assert node.source_record_id == {"path": "event/654321"}
    # The node id is NOT keyed by the locator: it stays event-id shaped, so
    # this change does not re-identify any graph node.
    assert node.node_id == "meeting_minutes:654321"


def test_the_adapter_forwards_nothing_the_readers_do_not_all_define():
    """``cache_paths`` is a reader detail the substrate never reads.

    Two wrapped readers (``TwikiCrawlSource``, ``TwikiEOSSource``) do not
    define it, so a forwarding property on the shared base raised
    ``AttributeError`` for them. Nothing in the substrate reads the attribute.
    """
    from archi.sources._sdk_adapter import ReaderAdapter

    assert not hasattr(ReaderAdapter, "cache_paths")


def _entry(source_name, filename):
    """One bundle entry, named by its file so the live and frozen CMSSW
    templates (both `cmssw_releases`) are never confused for each other."""
    for candidate_file, name, entry in ENTRIES:
        if name == source_name and candidate_file == filename:
            return entry
    raise AssertionError(f"{source_name} is not declared in {filename}")


def _params(source_name, filename, data_root, extra=None):
    """The shipped parameters, with this bundle's placeholders filled in."""
    params = dict(_entry(source_name, filename)["params"])
    replacements = {"${archi_data_root}": str(data_root), "${deployment_name}": "test"}
    for key, value in list(params.items()):
        if isinstance(value, str):
            for placeholder, actual in {**replacements, **(extra or {})}.items():
                value = value.replace(placeholder, actual)
            params[key] = value
    return params


def _run(adapter, mode="scope_complete"):
    run = adapter.run("run-1", mode=mode)
    # `next_cursor` is the substrate-only field the runner reads off every run.
    # A `ConnectorRun` has no such attribute, which is the defect these
    # adapters exist to prevent.
    assert hasattr(run, "next_cursor"), (
        "the run result must carry the substrate's fields, not the SDK's"
    )
    return run, list(run.facts)


def test_documentation_source_runs_from_its_shipped_parameters(tmp_path):
    (tmp_path / "docsite").mkdir()
    (tmp_path / "docsite" / "records.json").write_text(
        json.dumps(
            [
                {
                    "url": "https://docs.example.org/guide",
                    "title": "Transfer guide",
                    "body": "How the team runs transfers.",
                    "site_name": "docs.example.org",
                }
            ]
        )
    )
    (tmp_path / "jira").mkdir()
    (tmp_path / "jira" / "records.json").write_text(json.dumps([]))
    entry = _entry("docsite", "docsite.yaml.example")
    adapter = _adapter_class(entry)(**_params("docsite", "docsite.yaml.example", tmp_path))
    run, facts = _run(adapter)
    assert run.completed_scope is True
    assert [f for f in facts if isinstance(f, NodeFact) and f.subtype == "documentation_page"]


def test_jira_source_runs_from_its_shipped_parameters(tmp_path):
    (tmp_path / "jira").mkdir()
    (tmp_path / "jira" / "records.json").write_text(
        json.dumps(
            [
                {
                    "key": "CMSPROD-1",
                    "summary": "Transfers stalled",
                    "description": "Queue drained overnight.",
                    "status": "Closed",
                    "updated": "2026-01-02T03:04:05.000+0000",
                }
            ]
        )
    )
    (tmp_path / "jira" / "meta.json").write_text(
        json.dumps(
            {
                "record_count": 1,
                "fetched_at": "2026-01-02T03:04:05+00:00",
                "projects": ["CMSPROD"],
            }
        )
    )
    entry = _entry("jira", "jira.yaml.example")
    adapter = _adapter_class(entry)(**_params("jira", "jira.yaml.example", tmp_path))
    _run_result, facts = _run(adapter)
    assert [f for f in facts if isinstance(f, NodeFact) and f.subtype == "jira_issue"]


def test_cmssw_source_runs_from_its_shipped_frozen_parameters(tmp_path):
    catalogue = tmp_path / "cmssw" / "releases.map"
    catalogue.parent.mkdir()
    catalogue.write_text(
        "architecture=el8_amd64_gcc12;label=CMSSW_14_0_1;type=Production;state=Announced;\n"
        "architecture=el8_amd64_gcc12;label=CMSSW_14_0_2;type=Production;state=Announced;\n"
    )
    digest = "sha256:" + hashlib.sha256(catalogue.read_bytes()).hexdigest()
    # The frozen example, not the live default: that one fetches the real
    # catalogue over the network, which no test here may do.
    frozen = "cmssw_releases_frozen.yaml.example"
    params = _params(
        "cmssw_releases",
        frozen,
        tmp_path,
        extra={"${cmssw_map_path}": str(catalogue), "${cmssw_map_digest}": digest},
    )
    assert params["fetch"] is False
    adapter = _adapter_class(_entry("cmssw_releases", frozen))(**params)
    run, facts = _run(adapter)
    assert run.completed_scope is True
    releases = [f for f in facts if isinstance(f, NodeFact) and f.subtype == "cmssw_release"]
    # The reader emits one node per release plus the family node they share.
    assert {node.attrs["label"] for node in releases} == {
        "CMSSW_14_0_1",
        "CMSSW_14_0_2",
        "CMSSW_14_0_X",
    }
    assert [f for f in facts if isinstance(f, EdgeFact) and f.edge_type == "supersedes"]


@pytest.mark.parametrize(("filename", "name", "entry"), ENTRIES, ids=ENTRY_IDS)
def test_every_entry_binds_against_its_adapter_signature(filename, name, entry):
    """Each adapter must expose the parameters its registry entry authors.

    The adapter takes ``**params``, which the substrate refuses under strict
    admission -- ``source_param_unconsumed``, "**kwargs is not proof of
    consumption" -- and which also stops a misspelled parameter being caught
    when the adapter is bound. The adapters therefore publish the wrapped
    reader's signature, and this binds the shipped parameters against it.
    """
    cls = _adapter_class(entry)
    signature = inspect.signature(cls)
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    ), f"{filename}: {entry['class']} still exposes **kwargs to the substrate"
    # Shipped params only; placeholders are strings either way, and binding
    # does not read the values.
    signature.bind(**(entry.get("params") or {}))


def test_a_misspelled_parameter_fails_when_the_adapter_is_bound():
    from archi.sources.jira import JiraIssueAdapter

    with pytest.raises(TypeError) as excinfo:
        JiraIssueAdapter(records_pathh="/tmp/records.json")
    assert "records_pathh" in str(excinfo.value)


#: Entries that strict admission still refuses, and why. NOT about the
#: adapter signature: these two author `source_name` in `params`, which is a
#: substrate-owned key (`RESERVED_ADAPTER_PARAM_NAMES`), so the substrate
#: refuses them with `source_param_reserved` before it ever looks at the
#: constructor. Pre-existing and dormant -- cern-team ships on the legacy
#: admission contract -- and fixing it changes which name the readers emit,
#: so it is tracked separately rather than bundled in here. Remove an entry
#: from this set when it is fixed; the test will tell you if you forget.
STRICT_PATH_KNOWN_REFUSALS = {
    ("docsite.yaml.example", "docsite"): "source_param_reserved",
    ("twiki_crawl.yaml.example", "twiki_crawl"): "source_param_reserved",
}


def test_the_substrate_strict_admission_check_consumes_every_parameter():
    """Exercise okg's own refusal, not a restatement of it.

    `source_adapter_init_params` is the function that raises
    `source_param_unconsumed`. It takes the strict path only for an entry whose
    admission contract is `strict_v1`, so the entry is relabelled here; nothing
    else about it changes and no database is touched. cern-team ships on the
    legacy contract today, which is why the refusal was dormant rather than
    absent.

    The assertion is specifically that no entry fails for an unconsumed
    parameter. Entries with a different known refusal are listed above with
    their reason, so this cannot quietly become a test that asserts nothing.
    """
    import dataclasses

    from okg.substrate.ingest.adapter_factory import source_adapter_init_params
    from okg.substrate.sources.registry import (
        STRICT_ADMISSION_CONTRACT,
        admit_source_registry_document,
    )

    for filename, name, raw_entry in ENTRIES:
        admission = admit_source_registry_document(
            {"sources": {name: raw_entry}}, registry_path=Path(filename)
        )
        strict = dataclasses.replace(
            admission.entries[name], admission_contract=STRICT_ADMISSION_CONTRACT
        )
        expected = STRICT_PATH_KNOWN_REFUSALS.get((filename, name))
        try:
            params = source_adapter_init_params(
                strict,
                dsn="postgresql://localhost/unused",
                deployment="unused",
                adapter_class=_adapter_class(raw_entry),
            )
        except ValueError as exc:
            code = str(exc).split(":", 1)[0]
            assert code != "source_param_unconsumed", (
                f"{filename}: {name} — {exc}"
            )
            assert code == expected, (
                f"{filename}: {name} hits an unlisted strict refusal {code!r}: "
                f"{exc}"
            )
            continue
        assert expected is None, (
            f"{filename}: {name} no longer fails with {expected!r}; remove it "
            "from STRICT_PATH_KNOWN_REFUSALS"
        )
        assert set(raw_entry.get("params") or {}) <= set(params), (
            f"{filename}: {name} lost an authored parameter on the strict path"
        )


@pytest.mark.parametrize(
    "answer",
    [
        r"C:\reviewed\releases.map",
        "/a path/with spaces/releases.map",
        "/plain/releases.map",
    ],
    ids=["backslashes", "spaces", "plain"],
)
def test_an_install_answer_reaches_the_source_byte_for_byte(tmp_path, answer):
    """Answers are substituted as text, so the quoting style is load-bearing.

    `build_plan` interpolates each source-default's TEXT and parses the result
    as YAML, and the substitution escapes nothing (okg
    `substrate/catalog/profile.py`, `replace_match` returns `str(v)` as-is).
    A double-quoted placeholder therefore hands the answer to YAML's escape
    rules: the Windows path below came back with a carriage return where its
    `\\r` had been, and the only symptom was an unexplained cache_missing.
    Single quotes take the bytes literally.
    """
    import shutil

    from okg.substrate.deployment_bootstrap.profile_init import (
        build_plan,
        discover_profile,
    )

    profiles = tmp_path / "profiles"
    shutil.copytree(SOURCE_DEFAULTS.parent.parent, profiles)
    sd = profiles / "cern-team" / "source-defaults"
    (sd / "cmssw_releases.yaml").unlink()
    (sd / "cmssw_releases_frozen.yaml.example").rename(sd / "cmssw_releases.yaml")
    os.environ["OKG_PROFILES_DIR"] = str(profiles)
    digest = "sha256:" + "0" * 64
    plan = build_plan(
        profile=discover_profile("cern-team"),
        deployment_name="qt",
        deployments_root=tmp_path / "deployments",
        raw_answers={
            "deployment_name": "qt",
            "postgres_dsn": "postgresql://localhost/qt",
            "cmssw_map_path": answer,
            "cmssw_map_digest": digest,
        },
    )
    body = yaml.safe_load(plan.source_defaults["cmssw_releases"])
    params = body["cmssw_releases"]["params"]
    assert params["map_cache_path"] == answer
    assert params["map_cache_digest"] == digest


# --- comp-ops readers with no bundle entry ---------------------------------
#
# These readers ship no source-default: an operator registers one by copying
# the registry-entry template out of the reader module's docstring. Before
# they had adapters that template named the bare reader, so every run failed
# on `next_cursor` exactly as the bundle's own sources once did. The template
# is what an operator copies, so the test reads it rather than restating it.

#: source id -> the files its template's params name, as a tiny valid cache.
#: The shapes follow the readers' own tests under tests/sources/.
_COMPOPS_CACHES = {
    "cric": {
        "sites_path": {
            "T2_US_MIT": {
                "tier_level": 2,
                "facility": "MIT",
                "sitedb_title": "MIT Bates",
                "computeunits": {"MIT-CE1": {}},
            }
        },
        "storage_units_path": {
            "MIT-SE": {"type": "DISK", "site": {"name": "T2_US_MIT"}}
        },
        "compute_units_path": {"MIT-CE1": {"corepower": 11.0}},
        "facilities_path": {"MIT": {"cmssites": [{"name": "T2_US_MIT"}]}},
        "responsibilities_path": {
            "result": [["adalove", "MIT Bates", "Site Executive"]]
        },
    },
    "cric_core": {
        "services_path": {
            "reqmgr2-cmsweb.cern.ch": {
                "type": "webservice",
                "endpoint": "cmsweb.cern.ch/reqmgr2",
                "rcsite": "CERN-PROD",
            }
        },
        "rcsites_path": {
            "CERN-PROD": {"sites": [{"name": "T0_CH_CERN", "vo_name": "cms"}]}
        },
        "federations_path": {
            "CH-CERN": {"vos": ["cms"], "rcsites": ["CERN-PROD"], "pledges": {}}
        },
    },
    "dqm": {
        "records_path": [
            {
                "filename": "Cert_Collisions2024_378981_385194_Golden.json",
                "cert_name": "Cert_Collisions2024_378981_385194_Golden",
                "run_range": [378981, 385194],
                "datasets": ["/Muon0/Run2024C-PromptReco-v1/DQMIO"],
            }
        ],
    },
    "gocdb_downtimes": {
        "records_path": [
            {
                "downtime_id": 101,
                "severity": "OUTAGE",
                "classification": "SCHEDULED",
                "start_date": "2026-08-01T06:00:00",
                "end_date": "2026-08-01T18:00:00",
                "hosted_by": "T2_US_MIT",
                "service_type": "CE",
                "hostname": "ce01.cmsaf.mit.edu",
            }
        ],
        "sites_path": {"T2_US_MIT": {}},
        "services_path": {"reqmgr2": {"endpoint": "cmsweb.cern.ch/reqmgr2"}},
    },
    "conddb_global_tags": {
        "records_path": [{"name": "140X_dataRun3_v2", "release": "CMSSW_14_0_X"}],
        "cmssw_records_path": [{"label": "CMSSW_14_0_1"}],
    },
    "dbs_datasets": {
        "records_path": [
            {
                "dataset": "/TTto2L2Nu/Run3Summer23-v1/AODSIM",
                "data_tier_name": "AODSIM",
                "primary_ds_name": "TTto2L2Nu",
                "processed_ds_name": "Run3Summer23-v1",
            }
        ],
    },
    "wmstats_workflows": {
        "records_path": [
            {
                "RequestName": "pdmvserv_task_TOP-Run3Summer23-00001",
                "RequestType": "TaskChain",
                "RequestStatus": "running-open",
                "OutputDatasets": ["/TTto2L2Nu/Run3Summer23-v1/AODSIM"],
            }
        ],
    },
    # Reads no cache: its template authors no parameters, and the reader
    # falls back to its configured default repository list.
    "github_repos": {},
}

#: source id -> (module, a subtype a successful run must emit).
_COMPOPS_READERS = {
    "cric": ("archi.sources.cric", "site"),
    "cric_core": ("archi.sources.cric", "infrastructure_service"),
    "dqm": ("archi.sources.dqm", "data_certification"),
    "gocdb_downtimes": ("archi.sources.gocdb", "downtime"),
    "conddb_global_tags": ("archi.sources.conddb", "global_tag"),
    "dbs_datasets": ("archi.sources.dbs", "dataset"),
    "wmstats_workflows": ("archi.sources.wmstats", "workflow"),
    "github_repos": ("archi.sources.github_repos", "software_repository"),
}


def _docstring_template(module_name, source_id):
    """The registry entry for ``source_id`` in the module's docstring.

    Each reader module ends its docstring with a literal YAML block after a
    ``::`` line; that block is what an operator pastes into a registry.
    """
    module = importlib.import_module(module_name)
    _head, sep, block = (module.__doc__ or "").rpartition("::\n")
    assert sep, f"{module_name} has no registry-entry template"
    templates = yaml.safe_load(textwrap.dedent(block))
    assert source_id in templates, f"{module_name} template has no {source_id}"
    entry = templates[source_id]
    assert entry["module"] == module_name
    return entry


@pytest.mark.parametrize("source_id", sorted(_COMPOPS_READERS))
def test_compops_template_names_an_adapter_that_runs(tmp_path, source_id):
    module_name, subtype = _COMPOPS_READERS[source_id]
    entry = _docstring_template(module_name, source_id)
    module = importlib.import_module(module_name)
    cls = getattr(module, entry["class"], None)
    assert cls is not None, f"{module_name} defines no {entry['class']}"
    assert inspect.isclass(cls) and issubclass(cls, ConnectorAdapter), (
        f"the {source_id} template names {entry['class']}, which the substrate "
        "runner cannot drive; it must name the reader's ConnectorAdapter"
    )

    # Authority: string literals the substrate can read, equal to the
    # reader's own values and to what the template declares.
    reader = inspect.getattr_static(cls, "reader_class")
    source_path = Path(module.__file__)
    for attr in ("profile", "change_probe_kind"):
        literal = _class_level_constant(source_path, entry["class"], attr)
        assert literal == getattr(reader, attr)
        assert inspect.getattr_static(cls, attr) == literal
    assert inspect.getattr_static(cls, "profile") == entry["source_class"]

    # The template's own parameters bind against the adapter's signature.
    signature = inspect.signature(cls)
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()
    )
    params = dict(entry.get("params") or {})
    signature.bind(**params)

    # And it runs: write a tiny cache at the template's paths, then drive the
    # adapter the way the substrate runner does.
    caches = _COMPOPS_CACHES[source_id]
    assert set(caches) == set(params), "the fixture must cover every template path"
    for key, payload in caches.items():
        path = tmp_path / params[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    if "base" in signature.parameters:
        params["base"] = str(tmp_path)
    adapter = cls(**params)
    assert adapter.preflight().status == "ok"
    run, facts = _run(adapter)
    assert run.completed_scope is True
    primary = [f for f in facts if isinstance(f, NodeFact) and f.subtype == subtype]
    assert primary
    # The declared identity fields are the keys the reader actually emits for
    # its primary records, so the declaration describes the real record key.
    for fact in primary:
        assert set(fact.source_record_id) == set(entry["record_identity_fields"]), (
            f"{source_id}: {fact.node_id} is keyed by {sorted(fact.source_record_id)}"
            f" but the template declares {entry['record_identity_fields']}"
        )


def test_compops_template_profile_tuples_are_all_accepted():
    """Call the substrate's own validator on each template, as install does.

    Each template once declared a combination `okg install` refused with
    `deployment.source_registry.profile_invalid`: `record_identity_kind:
    remote_id` under discovery_crawl or reference_catalog, and `content_hash`
    revisions under mutable_api for WMStats. The fix is in the declaration
    only: the readers' emitted record keys and node ids are unchanged, and
    match what the okg-deployments cms registry declared for the same code.
    """
    from okg.substrate.sources.profiles import validate_profile_tuple

    refused = {}
    for source_id, (module_name, _subtype) in sorted(_COMPOPS_READERS.items()):
        entry = _docstring_template(module_name, source_id)
        try:
            validate_profile_tuple(
                source_class=entry["source_class"],
                record_identity_kind=entry.get("record_identity_kind"),
                source_revision_kind=entry.get("source_revision_kind"),
                deletion_semantics=entry.get("deletion_semantics"),
                publication_mode=entry.get("publication_mode"),
            )
        except ValueError as exc:
            refused[source_id] = str(exc)
    assert not refused, (
        "these templates would fail `okg install` with "
        "deployment.source_registry.profile_invalid:\n\n"
        + "\n\n".join(f"{name}: {why}" for name, why in sorted(refused.items()))
    )


@pytest.mark.parametrize("source_id", sorted(_COMPOPS_READERS))
def test_compops_template_passes_strict_registry_admission(source_id):
    """Admit the pasted template the way a registry load does, then bind it.

    Uses okg's own admission and `source_adapter_init_params` on the strict
    contract, as the bundle test above does. This is what caught the GitHub
    template's bare `params:` (null), which strict admission refuses with
    `source_params_not_mapping`.
    """
    import dataclasses

    from okg.substrate.ingest.adapter_factory import source_adapter_init_params
    from okg.substrate.sources.registry import (
        STRICT_ADMISSION_CONTRACT,
        admit_source_registry_document,
    )

    module_name, _subtype = _COMPOPS_READERS[source_id]
    entry = _docstring_template(module_name, source_id)
    admission = admit_source_registry_document(
        {"sources": {source_id: entry}}, registry_path=Path("registry.yaml")
    )
    strict = dataclasses.replace(
        admission.entries[source_id], admission_contract=STRICT_ADMISSION_CONTRACT
    )
    params = source_adapter_init_params(
        strict,
        dsn="postgresql://localhost/unused",
        deployment="unused",
        adapter_class=getattr(importlib.import_module(module_name), entry["class"]),
    )
    assert set(entry["params"]) <= set(params)


def test_a_bare_compops_reader_lacks_the_field_the_runner_reads(tmp_path):
    """The defect the adapters exist for, shown on one reader.

    If a reader's run result ever grows ``next_cursor`` the adapters become
    redundant; this test says so rather than letting them rot silently.
    """
    from archi.sources.dqm import DQMSource

    path = tmp_path / "data" / "dqm" / "records.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_COMPOPS_CACHES["dqm"]["records_path"]))
    run = DQMSource(base=str(tmp_path)).run("run-1", mode="scope_complete")
    assert not hasattr(run, "next_cursor")
