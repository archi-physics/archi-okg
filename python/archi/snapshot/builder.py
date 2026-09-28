"""Build and verify a checksummed cache snapshot for the archi readers.

``build`` reads one source directory per group (a config maps each group to
its directory and collection date, so one snapshot can mix collection dates),
and for every group:

1. checks each file the reader reads: present, valid JSON of the expected
   shape, every record well-formed by the reader's own skip rule. Any failure
   refuses the group with :class:`GroupRefused`, which names the group, the
   file and the record;
2. removes every email address from every string (JSON keys and values, and
   whole text files) with :func:`archi.enrichment.anonymizer.redact_email_addresses`;
3. drops every field the reader does not read (``groups.GroupSpec.schema``),
   plus the group's configured ``drop_fields``, keeping its ``keep_fields``;
4. runs the group's archi reader over the result: it must report health
   ``ok`` and a completed scope, and its facts must equal the facts it emits
   from the step-2 copy (so step 3 dropped nothing the reader reads).

If any group is refused, nothing is written. Otherwise each group becomes one
deterministic ``<group>.tar.zst`` (sorted members, fixed owner, mode and
mtime, PAX format, ``zstd -T1`` at a fixed level) and ``snapshot.lock.yaml``
records, per group, its collection date, archive SHA-256 and size, file
count, record count (as the reader counts records) and a digest of the
unpacked contents.

``verify`` checks archives against a lock file on a machine that has only the
archives: size and SHA-256 of each archive, then its members (count, paths
inside the group's directory, contents digest).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Optional

import yaml

from archi.enrichment.anonymizer import redact_email_addresses
from archi.snapshot.groups import GROUPS, GroupSpec, JsonFile, Schema

LOCK_NAME = "snapshot.lock.yaml"
LOCK_VERSION = 1
#: Every member's mtime (2000-01-01T00:00:00Z). Any fixed value works; zero
#: makes some tar tools warn about implausibly old files.
FIXED_MTIME = 946684800
DEFAULT_ZSTD_LEVEL = 19
VALIDATION_RUN_ID = "snapshot-validate"
_DATE = r"\d{4}-\d{2}-\d{2}"
_COLLECTED_RE = re.compile(rf"^({_DATE})(?:\.\.({_DATE}))?$")
_GROUP_KEYS = {"path", "collected", "drop_fields", "keep_fields"}
_CONFIG_KEYS = {"snapshot", "zstd_level", "groups"}


class SnapshotError(Exception):
    """The config, an archive or a lock file is unusable."""


class GroupRefused(SnapshotError):
    """One group's cache cannot go into a snapshot."""

    def __init__(self, group: str, reason: str) -> None:
        super().__init__(f"group {group!r} refused: {reason}")
        self.group = group
        self.reason = reason


class BuildRefused(SnapshotError):
    """One or more groups were refused; nothing was written."""

    def __init__(self, refusals: list[GroupRefused]) -> None:
        lines = "\n".join(f"  - {r}" for r in refusals)
        super().__init__(
            f"{len(refusals)} group(s) refused; no snapshot written:\n{lines}"
        )
        self.refusals = refusals


# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class GroupConfig:
    name: str
    path: Path
    collected: str
    drop_fields: tuple[str, ...] = ()
    keep_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SnapshotConfig:
    snapshot: str
    groups: tuple[GroupConfig, ...]
    zstd_level: int = DEFAULT_ZSTD_LEVEL


def load_config(path: str | Path) -> SnapshotConfig:
    """Read a build config. Relative group paths resolve against its directory."""
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SnapshotError(f"{config_path}: cannot read config: {exc}") from exc
    return parse_config(raw, base=config_path.parent)


def parse_config(raw: Any, *, base: Path) -> SnapshotConfig:
    if not isinstance(raw, dict):
        raise SnapshotError("config must be a mapping")
    unknown = set(raw) - _CONFIG_KEYS
    if unknown:
        raise SnapshotError(f"unknown config keys: {sorted(unknown)}")
    snapshot = raw.get("snapshot")
    if not isinstance(snapshot, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", snapshot):
        raise SnapshotError(
            "config 'snapshot' must be a name of letters, digits, '.', '_' or '-'"
        )
    level = raw.get("zstd_level", DEFAULT_ZSTD_LEVEL)
    if not isinstance(level, int) or isinstance(level, bool) or not 1 <= level <= 19:
        raise SnapshotError("config 'zstd_level' must be an integer from 1 to 19")
    groups_raw = raw.get("groups")
    if not isinstance(groups_raw, dict) or not groups_raw:
        raise SnapshotError("config 'groups' must map at least one group name")
    groups = []
    for name in sorted(groups_raw):
        if name not in GROUPS:
            raise SnapshotError(
                f"unknown group {name!r}; known groups: {sorted(GROUPS)}"
            )
        groups.append(_parse_group(name, groups_raw[name], base))
    return SnapshotConfig(snapshot=snapshot, groups=tuple(groups), zstd_level=level)


def _parse_group(name: str, raw: Any, base: Path) -> GroupConfig:
    if not isinstance(raw, dict):
        raise SnapshotError(f"group {name!r}: expected a mapping")
    unknown = set(raw) - _GROUP_KEYS
    if unknown:
        raise SnapshotError(f"group {name!r}: unknown keys {sorted(unknown)}")
    if not raw.get("path"):
        raise SnapshotError(f"group {name!r}: 'path' is required")
    if "collected" not in raw or raw["collected"] in (None, ""):
        raise SnapshotError(
            f"group {name!r}: 'collected' (the collection date) is required"
        )
    collected = _parse_collected(name, raw["collected"])
    path = Path(str(raw["path"])).expanduser()
    if not path.is_absolute():
        path = base / path
    return GroupConfig(
        name=name,
        path=path,
        collected=collected,
        drop_fields=_field_list(name, raw, "drop_fields"),
        keep_fields=_field_list(name, raw, "keep_fields"),
    )


def _parse_collected(name: str, value: Any) -> str:
    """``YYYY-MM-DD`` or a range ``YYYY-MM-DD..YYYY-MM-DD``, as a string."""
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    match = _COLLECTED_RE.match(str(value))
    if not match:
        raise SnapshotError(
            f"group {name!r}: 'collected' must be YYYY-MM-DD or "
            f"YYYY-MM-DD..YYYY-MM-DD, got {value!r}"
        )
    try:
        start = dt.date.fromisoformat(match.group(1))
        end = dt.date.fromisoformat(match.group(2)) if match.group(2) else start
    except ValueError as exc:
        raise SnapshotError(f"group {name!r}: 'collected' {value!r}: {exc}") from exc
    if end < start:
        raise SnapshotError(f"group {name!r}: 'collected' range ends before it starts")
    return str(value)


def _field_list(name: str, raw: dict, key: str) -> tuple[str, ...]:
    value = raw.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise SnapshotError(f"group {name!r}: {key!r} must be a list of field names")
    return tuple(sorted(set(value)))


# --- per-group preparation ---------------------------------------------------


@dataclass
class PreparedGroup:
    """A validated group: final file bytes by archive path, plus its lock row."""

    name: str
    collected: str
    files: dict[str, bytes]
    record_count: int
    dropped_fields: tuple[str, ...]
    kept_fields: tuple[str, ...]
    replaced_decoding: int = 0
    notes: list[str] = field(default_factory=list)


def prepare_group(group: GroupConfig) -> PreparedGroup:
    """Validate, redact, prune and reader-check one group, or raise GroupRefused."""
    spec = GROUPS[group.name]
    source = group.path
    if not source.is_dir():
        raise GroupRefused(group.name, f"source directory {source} does not exist")
    drop = set(spec.default_drop_fields) | set(group.drop_fields)
    keep = (set(spec.default_keep_fields) | set(group.keep_fields)) - drop
    if (group.drop_fields or group.keep_fields) and spec.record_file is None:
        raise GroupRefused(
            group.name, "drop_fields/keep_fields need a JSON records file"
        )

    redacted: dict[str, bytes] = {}
    final: dict[str, bytes] = {}
    replaced = 0
    for json_file in spec.json_files:
        path = source / json_file.name
        if not path.is_file():
            if json_file.required:
                raise GroupRefused(group.name, f"{json_file.name} is missing in {source}")
            continue
        payload = _load_json(group.name, path)
        _check_shape(group.name, json_file, payload)
        clean = _redact_json(group.name, json_file.name, payload)
        # Again after redaction: a record whose identity was only an address
        # is one the reader would now skip or drop.
        _check_shape(group.name, json_file, clean)
        archive_path = f"{spec.archive_dir}/{json_file.name}"
        redacted[archive_path] = _dump_json(clean)
        record_level = json_file is spec.record_file
        final[archive_path] = _dump_json(
            _prune_file(
                json_file,
                clean,
                drop=drop if record_level else set(),
                keep=keep if record_level else set(),
            )
        )
    if spec.text is not None:
        for rel, path in _text_files(spec, source):
            text, lossy = _read_text(group.name, rel, path, spec.text.errors)
            replaced += lossy
            data = redact_email_addresses(text).encode("utf-8")
            archive_path = f"{spec.archive_dir}/{rel}"
            redacted[archive_path] = data
            final[archive_path] = data
    if not final:
        raise GroupRefused(group.name, f"no cache files found in {source}")

    _check_no_addresses(group.name, final)
    record_count = _reader_check(spec, redacted, final)
    return PreparedGroup(
        name=group.name,
        collected=group.collected,
        files=final,
        record_count=record_count,
        dropped_fields=tuple(sorted(drop)),
        kept_fields=tuple(sorted(keep)),
        replaced_decoding=replaced,
    )


def _load_json(group: str, path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise GroupRefused(group, f"{path.name} is not UTF-8: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise GroupRefused(group, f"{path.name} is not valid JSON: {exc}") from exc


def _check_shape(group: str, spec: JsonFile, payload: Any) -> None:
    if spec.shape == "list":
        if not isinstance(payload, list):
            raise GroupRefused(group, f"{spec.name}: expected a JSON list")
        items: Iterable[tuple[str, Any]] = (
            (f"record {index}", item) for index, item in enumerate(payload)
        )
    elif spec.shape == "mapping":
        if not isinstance(payload, dict):
            raise GroupRefused(group, f"{spec.name}: expected a JSON object")
        items = ((f"entry {key!r}", value) for key, value in payload.items())
    else:
        if not isinstance(payload, dict):
            raise GroupRefused(group, f"{spec.name}: expected a JSON object")
        if spec.name == "responsibilities.json" and not isinstance(
            payload.get("result"), list
        ):
            raise GroupRefused(group, f"{spec.name}: expected a 'result' list")
        return
    if spec.identity is None:
        return
    for where, item in items:
        reason = spec.identity(item)
        if reason:
            raise GroupRefused(group, f"{spec.name} {where} is malformed: {reason}")


def _redact_json(group: str, file_name: str, value: Any) -> Any:
    if isinstance(value, str):
        return redact_email_addresses(value)
    if isinstance(value, list):
        return [_redact_json(group, file_name, item) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = redact_email_addresses(key)
            if clean_key in out:
                raise GroupRefused(
                    group,
                    f"{file_name}: removing addresses makes two keys equal "
                    f"({clean_key!r})",
                )
            out[clean_key] = _redact_json(group, file_name, item)
        return out
    return value


def _prune(value: Any, schema: Schema) -> Any:
    if schema is None:
        return value
    if isinstance(value, dict):
        return {k: _prune(v, schema[k]) for k, v in value.items() if k in schema}
    if isinstance(value, list):
        return [_prune(item, schema) for item in value]
    return value


def _prune_file(spec: JsonFile, payload: Any, *, drop: set[str], keep: set[str]) -> Any:
    """Keep the fields the schema names (plus ``keep``), then remove ``drop``.

    ``drop`` and ``keep`` name top-level fields of each record.
    """
    schema: Schema = spec.schema
    if schema is not None and keep:
        schema = {**{name: None for name in keep}, **schema}

    def record(item: Any) -> Any:
        pruned = _prune(item, schema)
        if drop and isinstance(pruned, dict):
            pruned = {k: v for k, v in pruned.items() if k not in drop}
        return pruned

    if spec.shape == "list":
        return [record(item) for item in payload]
    if spec.shape == "mapping":
        return {key: record(item) for key, item in payload.items()}
    return record(payload)


def _dump_json(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _text_files(spec: GroupSpec, source: Path) -> list[tuple[str, Path]]:
    assert spec.text is not None
    if spec.text.recursive:
        paths = [p for p in source.rglob(spec.text.pattern) if p.is_file()]
    else:
        candidate = source / spec.text.pattern
        if not candidate.is_file():
            raise GroupRefused(spec.name, f"{spec.text.pattern} is missing in {source}")
        paths = [candidate]
    return sorted(
        ((p.relative_to(source).as_posix(), p) for p in paths), key=lambda t: t[0]
    )


def _read_text(group: str, rel: str, path: Path, errors: str) -> tuple[str, int]:
    data = path.read_bytes()
    try:
        return data.decode("utf-8"), 0
    except UnicodeDecodeError as exc:
        if errors != "replace":
            raise GroupRefused(group, f"{rel} is not UTF-8: {exc}") from exc
        # The reader decodes with errors="replace"; store what it would read.
        return data.decode("utf-8", errors="replace"), 1


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def _check_no_addresses(group: str, files: Mapping[str, bytes]) -> None:
    """Every string must be a fixed point of the redactor."""
    for path, data in files.items():
        text = data.decode("utf-8")
        strings = _strings(json.loads(text)) if path.endswith(".json") else [text]
        for value in strings:
            if redact_email_addresses(value) != value:
                raise GroupRefused(
                    group, f"{path}: an address survived redaction"
                )


def _reader_check(
    spec: GroupSpec, redacted: Mapping[str, bytes], final: Mapping[str, bytes]
) -> int:
    with tempfile.TemporaryDirectory(prefix=f"snapshot-{spec.name}-") as tmp:
        full_root = Path(tmp) / "redacted"
        final_root = Path(tmp) / "final"
        for root, files in ((full_root, redacted), (final_root, final)):
            _stage(root, spec.validation_stubs)
            _stage(root, files)
        full_facts, _ = _run_reader(spec, full_root)
        final_facts, record_count = _run_reader(spec, final_root)
    if full_facts != final_facts:
        detail = _first_difference(full_facts, final_facts)
        raise GroupRefused(
            spec.name,
            "dropping unread fields changed what the reader emits, so the "
            f"group's schema misses a field the reader reads ({detail})",
        )
    return record_count


def _stage(root: Path, files: Mapping[str, bytes]) -> None:
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _run_reader(spec: GroupSpec, root: Path) -> tuple[list[str], int]:
    try:
        reader = spec.make_reader(root)
        run = reader.run(VALIDATION_RUN_ID, mode="scope_complete")
        facts = [normalize_fact(fact) for fact in run.facts]
    except Exception as exc:  # the reader's own refusal, reported verbatim
        raise GroupRefused(
            spec.name, f"the archi reader failed: {type(exc).__name__}: {exc}"
        ) from exc
    health = run.health
    status = getattr(health, "status", None)
    if status != "ok" or not run.completed_scope:
        reason = getattr(health, "reason", "") or "no reason given"
        raise GroupRefused(
            spec.name,
            "the archi reader did not accept the whole cache "
            f"(health {status!r}, completed_scope={run.completed_scope}): {reason}",
        )
    count = getattr(health, "record_count", None)
    if not isinstance(count, int) or count <= 0:
        raise GroupRefused(
            spec.name, f"the archi reader reported {count!r} records"
        )
    return facts, count


def normalize_fact(fact: Any) -> str:
    """A fact as canonical JSON, without its run-specific revision."""
    if dataclasses.is_dataclass(fact):
        body = dataclasses.asdict(fact)
    else:
        body = dict(vars(fact))
    body.pop("source_revision", None)
    body["fact_type"] = type(fact).__name__
    return json.dumps(body, sort_keys=True, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, tuple):
        return list(value)
    return str(value)


def _first_difference(left: list[str], right: list[str]) -> str:
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return f"fact {index}: {a[:200]} != {b[:200]}"
    return f"{len(left)} facts before pruning, {len(right)} after"


# --- archives ----------------------------------------------------------------


def zstd_version() -> str:
    try:
        out = subprocess.run(
            ["zstd", "--version"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SnapshotError(
            "the zstd command is required to build or verify a snapshot"
        ) from exc
    match = re.search(r"v\d+\.\d+\.\d+", out)
    return match.group(0) if match else out.strip()


def write_archive(files: Mapping[str, bytes], target: Path, *, level: int) -> None:
    """Write ``files`` as a deterministic tar compressed by ``zstd -T1``."""
    with target.open("wb") as out:
        proc = subprocess.Popen(
            ["zstd", f"-{level}", "-T1", "-q", "-c", "--no-progress", "-"],
            stdin=subprocess.PIPE,
            stdout=out,
        )
        assert proc.stdin is not None
        try:
            with tarfile.open(
                fileobj=proc.stdin, mode="w|", format=tarfile.PAX_FORMAT
            ) as tar:
                for name in sorted(files):
                    data = files[name]
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mtime = FIXED_MTIME
                    info.mode = 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.type = tarfile.REGTYPE
                    tar.addfile(info, io.BytesIO(data))
        finally:
            proc.stdin.close()
            code = proc.wait()
    if code != 0:
        raise SnapshotError(f"zstd exited {code} while writing {target.name}")


def read_archive(path: Path) -> dict[str, bytes]:
    """Decompress and read every member; refuses non-file or escaping members."""
    proc = subprocess.run(
        ["zstd", "-d", "-q", "-c", str(path)], capture_output=True
    )
    if proc.returncode != 0:
        raise SnapshotError(
            f"{path.name}: zstd cannot decompress it: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    members: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r:") as tar:
            for info in tar:
                name = info.name
                pure = PurePosixPath(name)
                if not info.isreg() or pure.is_absolute() or ".." in pure.parts:
                    raise SnapshotError(f"{path.name}: unsafe member {name!r}")
                if name in members:
                    raise SnapshotError(f"{path.name}: duplicate member {name!r}")
                handle = tar.extractfile(info)
                assert handle is not None
                members[name] = handle.read()
    except tarfile.TarError as exc:
        raise SnapshotError(f"{path.name}: not a readable tar: {exc}") from exc
    return members


def contents_digest(files: Mapping[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(files[name]).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- build and verify --------------------------------------------------------


def build(
    config: SnapshotConfig,
    out_dir: str | Path,
    *,
    built_by: Optional[str] = None,
) -> dict[str, Any]:
    """Build every group, or raise BuildRefused having written nothing."""
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise SnapshotError(f"{out} exists and is not empty; refusing to overwrite")
    version = zstd_version()
    prepared: list[PreparedGroup] = []
    refusals: list[GroupRefused] = []
    for group in config.groups:
        try:
            prepared.append(prepare_group(group))
        except GroupRefused as exc:
            refusals.append(exc)
    if refusals:
        raise BuildRefused(refusals)

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}.", dir=out.parent))
    try:
        groups_lock: dict[str, Any] = {}
        for group in prepared:
            archive = staging / f"{group.name}.tar.zst"
            write_archive(group.files, archive, level=config.zstd_level)
            row: dict[str, Any] = {
                "collected": group.collected,
                "archive": archive.name,
                "sha256": _sha256_file(archive),
                "bytes": archive.stat().st_size,
                "file_count": len(group.files),
                "record_count": group.record_count,
                "contents_sha256": contents_digest(group.files),
                "archive_dir": GROUPS[group.name].archive_dir,
                "dropped_fields": list(group.dropped_fields),
                "kept_extra_fields": list(group.kept_fields),
            }
            if group.replaced_decoding:
                row["files_decoded_with_replacement"] = group.replaced_decoding
            groups_lock[group.name] = row
        lock = {
            "lock_version": LOCK_VERSION,
            "snapshot": config.snapshot,
            "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "built_by": built_by or os.environ.get("USER") or "unknown",
            "built_on": socket.gethostname(),
            "archi": _archi_provenance(),
            "archive_format": {
                "tar": "pax",
                "mtime": FIXED_MTIME,
                "compressor": "zstd",
                "zstd_version": version,
                "zstd_level": config.zstd_level,
            },
            "groups": groups_lock,
        }
        (staging / LOCK_NAME).write_text(
            yaml.safe_dump(lock, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        if out.exists():
            out.rmdir()
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return lock


def _archi_provenance() -> dict[str, Any]:
    from archi import __version__ as archi_version

    package_dir = Path(__file__).resolve().parents[1]
    info: dict[str, Any] = {"version": archi_version}
    try:
        head = subprocess.run(
            ["git", "-C", str(package_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(package_dir), "status", "--porcelain", "--", "."],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        info["commit"] = head
        info["tree_dirty"] = bool(dirty)
    except (OSError, subprocess.CalledProcessError):
        info["commit"] = None
        info["commit_note"] = "archi is not running from a git checkout"
    return info


@dataclass
class VerifyResult:
    ok: bool
    lines: list[str]


def verify(lock_path: str | Path, archive_dir: str | Path) -> VerifyResult:
    """Check every archive a lock names; report every problem, not just the first."""
    lock_file = Path(lock_path)
    directory = Path(archive_dir)
    try:
        lock = yaml.safe_load(lock_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SnapshotError(f"{lock_file}: cannot read lock: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("lock_version") != LOCK_VERSION:
        raise SnapshotError(f"{lock_file}: not a version-{LOCK_VERSION} snapshot lock")
    groups = lock.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise SnapshotError(f"{lock_file}: lock names no groups")
    lines: list[str] = []
    ok = True
    for name in sorted(groups):
        problem = _verify_group(name, groups[name], directory)
        if problem:
            ok = False
            lines.append(f"FAIL {name}: {problem}")
        else:
            lines.append(f"ok   {name}")
    return VerifyResult(ok=ok, lines=lines)


def _verify_group(name: str, row: Any, directory: Path) -> Optional[str]:
    if not isinstance(row, dict):
        return "lock row is not a mapping"
    archive_name = row.get("archive")
    if not isinstance(archive_name, str) or "/" in archive_name:
        return f"lock names no valid archive ({archive_name!r})"
    archive = directory / archive_name
    if not archive.is_file():
        return f"{archive_name} is missing"
    size = archive.stat().st_size
    if size != row.get("bytes"):
        return f"{archive_name} is {size} bytes, lock says {row.get('bytes')}"
    sha = _sha256_file(archive)
    if sha != row.get("sha256"):
        return f"{archive_name} SHA-256 {sha} does not match lock {row.get('sha256')}"
    try:
        members = read_archive(archive)
    except SnapshotError as exc:
        return str(exc)
    if len(members) != row.get("file_count"):
        return f"{len(members)} files, lock says {row.get('file_count')}"
    prefix = f"{row.get('archive_dir')}/"
    outside = sorted(m for m in members if not m.startswith(prefix))
    if outside:
        return f"member outside {prefix}: {outside[0]}"
    if contents_digest(members) != row.get("contents_sha256"):
        return "unpacked contents do not match the lock's contents_sha256"
    return None
