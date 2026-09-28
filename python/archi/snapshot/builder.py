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
import errno
import fnmatch
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
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator, Mapping, Optional

import yaml

from archi.enrichment.anonymizer import _INVISIBLE as _REDACTOR_INVISIBLE
from archi.enrichment.anonymizer import (
    email_address_spans,
    obfuscated_email_address_spans,
    redact_email_addresses,
    redact_email_addresses_with_count,
    redact_obfuscated_email_addresses,
    redact_obfuscated_email_addresses_with_count,
)
from archi.snapshot.groups import (
    GROUPS,
    SIGNATURE_SHARE,
    GroupSpec,
    JsonFile,
    Schema,
)

LOCK_NAME = "snapshot.lock.yaml"
LOCK_VERSION = 1
#: Every member's mtime (2000-01-01T00:00:00Z). Any fixed value works; zero
#: makes some tar tools warn about implausibly old files.
FIXED_MTIME = 946684800
DEFAULT_ZSTD_LEVEL = 19
VALIDATION_RUN_ID = "snapshot-validate"
_DATE = r"\d{4}-\d{2}-\d{2}"
_COLLECTED_RE = re.compile(rf"^({_DATE})(?:\.\.({_DATE}))?$")
_GROUP_KEYS = {"path", "collected", "file_dates", "note", "drop_fields", "keep_fields"}
#: A group note is a line or two for the lock, not a document.
NOTE_MAX_CHARS = 300
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
    #: Dates of single files that differ from ``collected`` (for example a
    #: file fetched earlier than the rest), by file name in the group
    #: directory. Carried into the lock.
    file_dates: tuple[tuple[str, str], ...] = ()
    #: Free text carried into the lock (for example how the dates were read).
    note: Optional[str] = None


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
        file_dates=_file_dates(name, raw.get("file_dates")),
        note=_note(name, raw.get("note")),
    )


def _note(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SnapshotError(f"group {name!r}: 'note' must be non-empty text")
    if len(value) > NOTE_MAX_CHARS:
        raise SnapshotError(
            f"group {name!r}: 'note' is {len(value)} characters; "
            f"the limit is {NOTE_MAX_CHARS}"
        )
    return value


def _file_dates(name: str, value: Any) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict) or not value:
        raise SnapshotError(
            f"group {name!r}: 'file_dates' must map file names to dates"
        )
    return tuple(
        sorted(
            (str(file), _parse_collected(name, date, field=f"file_dates[{file!r}]"))
            for file, date in value.items()
        )
    )


def _parse_collected(name: str, value: Any, *, field: str = "collected") -> str:
    """``YYYY-MM-DD`` or a range ``YYYY-MM-DD..YYYY-MM-DD``, as a string.

    ``field`` names the config entry in errors (``collected`` or a
    ``file_dates`` entry).
    """
    if isinstance(value, dt.date) and not isinstance(value, dt.datetime):
        return value.isoformat()
    match = _COLLECTED_RE.match(str(value))
    if not match:
        raise SnapshotError(
            f"group {name!r}: {field!r} must be YYYY-MM-DD or "
            f"YYYY-MM-DD..YYYY-MM-DD, got {value!r}"
        )
    try:
        start = dt.date.fromisoformat(match.group(1))
        end = dt.date.fromisoformat(match.group(2)) if match.group(2) else start
    except ValueError as exc:
        raise SnapshotError(f"group {name!r}: {field!r} {value!r}: {exc}") from exc
    if end < start:
        raise SnapshotError(f"group {name!r}: {field!r} range ends before it starts")
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
    addresses_removed: int
    obfuscated_removed: int
    ansi_stripped: int
    dropped_fields: tuple[str, ...]
    kept_fields: tuple[str, ...]
    deep_dropped_keys: tuple[str, ...] = ()
    input_file: Optional[str] = None
    #: For groups whose text may fall back on invalid bytes (TWiki):
    #: pages, pages with fallback runs, and fallback bytes per decoding.
    text_stats: Optional[dict[str, Any]] = None
    file_dates: tuple[tuple[str, str], ...] = ()
    note: Optional[str] = None


def prepare_group(
    group: GroupConfig, *, tmp_dir: str | Path | None = None
) -> PreparedGroup:
    """Validate, redact, prune and reader-check one group, or raise GroupRefused.

    An unreadable file (permission denied, an I/O error) or JSON nested too
    deeply to check refuses the group like any other defect, instead of
    ending the build with a traceback.
    """
    try:
        return _prepare_group(group, tmp_dir)
    except GroupRefused:
        raise
    except OSError as exc:
        raise GroupRefused(
            group.name, f"cannot read its cache: {type(exc).__name__}: {exc}"
        ) from exc
    except RecursionError as exc:
        raise GroupRefused(
            group.name, "a JSON value is nested too deeply to check"
        ) from exc


#: Rounds of (addresses, then spelled-out addresses) before a string must
#: be a fixed point of both; removing one token can join text into another.
_REDACTION_ROUNDS = 4


class _Counter:
    def __init__(self) -> None:
        self.removed = 0
        self.obfuscated = 0
        self.ansi = 0

    def strip_ansi(self, text: str) -> str:
        clean, count = _CSI_RE.subn("", text)
        self.ansi += count
        return clean

    def redact(self, text: str) -> str:
        """Remove addresses, then spelled-out addresses (``jdoe[at]cern.ch``),
        repeating until neither changes the text (the post-check refuses a
        string that is still not clean)."""
        for _ in range(_REDACTION_ROUNDS):
            clean, removed = redact_email_addresses_with_count(text)
            clean, obfuscated = redact_obfuscated_email_addresses_with_count(clean)
            self.removed += removed
            self.obfuscated += obfuscated
            if clean == text:
                break
            text = clean
        return text


def _select_variant(spec: GroupSpec, source: Path) -> GroupSpec:
    if not spec.variants:
        return spec
    for variant in spec.variants:
        assert variant.primary_input is not None
        candidate = source / variant.primary_input
        if candidate.exists() or candidate.is_symlink():
            return variant
    names = " nor ".join(v.primary_input or "?" for v in spec.variants)
    raise GroupRefused(spec.name, f"neither {names} is in {source}")


def _prepare_group(group: GroupConfig, tmp_dir: str | Path | None) -> PreparedGroup:
    if not group.path.is_dir():
        raise GroupRefused(group.name, f"source directory {group.path} does not exist")
    # The configured directory may itself be a link; nothing inside it may be.
    source = group.path.resolve(strict=True)
    spec = _select_variant(GROUPS[group.name], source)
    deep = set(spec.deep_drop_keys)
    drop = set(spec.default_drop_fields) | set(group.drop_fields) | deep
    keep = (set(spec.default_keep_fields) | set(group.keep_fields)) - drop
    if (group.drop_fields or group.keep_fields) and spec.record_file is None:
        raise GroupRefused(
            group.name, "drop_fields/keep_fields need a JSON records file"
        )

    counter = _Counter()
    redacted: dict[str, bytes] = {}
    final: dict[str, bytes] = {}
    for json_file in spec.json_files:
        path = source / json_file.name
        if not (path.exists() or path.is_symlink()):
            if json_file.required:
                raise GroupRefused(group.name, f"{json_file.name} is missing in {source}")
            continue
        payload = _load_json(group.name, _safe_file(group.name, source, json_file.name))
        _check_shape(group.name, json_file, payload)
        clean = _redact_json(group.name, json_file.name, payload, counter)
        # Again after redaction: a record whose identity was only an address
        # is one the reader would now skip or drop.
        _check_shape(group.name, json_file, clean)
        archive_path = f"{spec.archive_dir}/{json_file.name}"
        redacted[archive_path] = _dump_json(clean)
        record_level = json_file is spec.record_file
        pruned = _prune_file(
            json_file,
            clean,
            drop=drop if record_level else set(),
            keep=keep if record_level else set(),
        )
        final[archive_path] = _dump_json(_drop_keys_deep(pruned, deep))
    text_stats: Optional[dict[str, Any]] = None
    if spec.text is not None:
        fallback = spec.text.fallback_invalid_bytes
        if fallback:
            text_stats = {
                "text_pages": 0,
                "pages_valid_utf8": 0,
                "pages_with_fallback_runs": 0,
                "pages_bytes_dropped": 0,
                "fallback_bytes": {"cp1252": 0, "latin-1": 0},
            }
        for rel, path in _text_files(spec, source):
            page = _read_text(group.name, rel, path, fallback=fallback)
            text = page.text
            if text_stats is not None:
                text_stats["text_pages"] += 1
                if page.fallback_bytes:
                    text_stats["pages_with_fallback_runs"] += 1
                    for kind, count in page.fallback_bytes.items():
                        text_stats["fallback_bytes"][kind] += count
                    if page.bytes_dropped:
                        text_stats["pages_bytes_dropped"] += 1
                else:
                    text_stats["pages_valid_utf8"] += 1
            _refuse_hidden_address(group.name, rel, text)
            data = counter.redact(counter.strip_ansi(text)).encode("utf-8")
            archive_path = f"{spec.archive_dir}/{rel}"
            redacted[archive_path] = data
            final[archive_path] = data
    if not final:
        raise GroupRefused(group.name, f"no cache files found in {source}")

    for archive_path in final:
        if redact_email_addresses(archive_path) != archive_path:
            raise GroupRefused(
                group.name, f"a file path contains an email address: {archive_path}"
            )
    for file_name, _date in group.file_dates:
        if f"{spec.archive_dir}/{file_name}" not in final:
            raise GroupRefused(
                group.name,
                f"file_dates names {file_name}, which the group does not archive",
            )
    _check_no_addresses(group.name, final)
    if group.note is not None:
        _check_string(group.name, "note", group.note)
    record_count = _reader_check(spec, redacted, final, tmp_dir)
    return PreparedGroup(
        name=group.name,
        collected=group.collected,
        files=final,
        record_count=record_count,
        addresses_removed=counter.removed,
        obfuscated_removed=counter.obfuscated,
        ansi_stripped=counter.ansi,
        dropped_fields=tuple(sorted(drop - deep)),
        kept_fields=tuple(sorted(keep)),
        deep_dropped_keys=tuple(sorted(deep)),
        input_file=spec.primary_input,
        text_stats=text_stats,
        file_dates=group.file_dates,
        note=group.note,
    )


def _safe_file(group: str, root: Path, rel: str) -> Path:
    """``root/rel`` if it is a regular file reached without links or dot names."""
    parts = PurePosixPath(rel).parts
    if any(part.startswith(".") for part in parts):
        raise GroupRefused(group, f"{rel}: hidden path (a name starts with '.')")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise GroupRefused(group, f"{rel}: symbolic link")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise GroupRefused(group, f"{rel}: resolves outside {root}")
    if not resolved.is_file():
        raise GroupRefused(group, f"{rel}: not a regular file")
    return resolved


def _check_tree(group: str, root: Path) -> list[str]:
    """Every file below ``root``; refuses on any link or dot-named entry."""

    def fail(exc: OSError) -> None:
        raise exc

    files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail):
        for name in (*dirnames, *filenames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if name.startswith("."):
                raise GroupRefused(group, f"{rel}: hidden path (a name starts with '.')")
            if full.is_symlink():
                raise GroupRefused(group, f"{rel}: symbolic link")
        files.extend(
            (Path(dirpath) / name).relative_to(root).as_posix() for name in filenames
        )
    return files


def _drop_keys_deep(value: Any, keys: set[str]) -> Any:
    if not keys:
        return value
    if isinstance(value, dict):
        return {k: _drop_keys_deep(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [_drop_keys_deep(item, keys) for item in value]
    return value


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
        reason = spec.identity(payload) if spec.identity is not None else None
        if reason:
            raise GroupRefused(group, f"{spec.name}: {reason}")
        return
    records = list(items)
    if spec.identity is not None:
        for where, item in records:
            reason = spec.identity(item)
            if reason:
                raise GroupRefused(
                    group, f"{spec.name} {where} is malformed: {reason}"
                )
    if spec.signature is not None:
        if not records:
            raise GroupRefused(group, f"{spec.name} holds no records")
        carrying = sum(1 for _, item in records if spec.signature.matches(item))
        if carrying < SIGNATURE_SHARE * len(records):
            raise GroupRefused(
                group,
                f"{spec.name}: only {carrying} of {len(records)} records carry "
                f"{spec.signature.description} ({SIGNATURE_SHARE:.0%} needed); "
                "is it another export?",
            )


def _redact_json(group: str, file_name: str, value: Any, counter: _Counter) -> Any:
    if isinstance(value, str):
        _refuse_hidden_address(group, file_name, value)
        return counter.redact(counter.strip_ansi(value))
    if isinstance(value, list):
        return [_redact_json(group, file_name, item, counter) for item in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            _refuse_hidden_address(group, file_name, key)
            clean_key = counter.redact(counter.strip_ansi(key))
            if clean_key in out:
                raise GroupRefused(
                    group,
                    f"{file_name}: removing addresses makes two keys equal "
                    f"({clean_key!r})",
                )
            out[clean_key] = _redact_json(group, file_name, item, counter)
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
        names = [
            rel
            for rel in _check_tree(spec.name, source)
            if fnmatch.fnmatchcase(PurePosixPath(rel).name, spec.text.pattern)
        ]
    else:
        candidate = source / spec.text.pattern
        if not (candidate.exists() or candidate.is_symlink()):
            raise GroupRefused(spec.name, f"{spec.text.pattern} is missing in {source}")
        names = [spec.text.pattern]
    return [(rel, _safe_file(spec.name, source, rel)) for rel in sorted(names)]


#: C0 controls other than tab, newline, vertical tab, form feed and carriage
#: return; DEL; and C1. Counted by the binary guard.
_CONTROL_RE = re.compile("[\x00-\x08\x0e-\x1f\x7f-\x9f]")
#: What the hidden-address check removes: the control characters above and
#: every invisible format character (Unicode category Cf, such as U+200E or
#: U+2068) except the ones the redactor already reads as part of an address
#: (soft hyphen, zero-width space and joiners, word joiner).
_HIDDEN_RE = re.compile(
    "[\x00-\x08\x0e-\x1f\x7f-\x9f"
    + "".join(
        re.escape(chr(code))
        for code in range(0x110000)
        if unicodedata.category(chr(code)) == "Cf"
        and chr(code) not in _REDACTOR_INVISIBLE
    )
    + "]"
)
#: A page is binary when more than this share of its characters, and at
#: least ``CONTROL_MIN_COUNT`` of them, are control characters.
CONTROL_SHARE_LIMIT = 0.01
CONTROL_MIN_COUNT = 16
#: ANSI CSI escape sequences (terminal colours pasted from logs), removed
#: from text after the hidden-address check and before redaction.
_CSI_RE = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
#: Terminal sequences a terminal would not print, for the hidden-address
#: check only (they are not stripped): CSI in its 7-bit and 8-bit (U+009B)
#: forms, ``ESC`` + intermediates + final byte (``ESC ( B`` from
#: ``tput sgr0``), and OSC strings (``ESC ] 0;title BEL``).
_SEQUENCE_RE = re.compile(
    r"(?:\x1b\[|\x9b)[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"
    r"|\x1b[\x20-\x2f]+[\x30-\x7e]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
)


@dataclass
class _Page:
    text: str
    fallback_bytes: dict[str, int]
    bytes_dropped: bool = False


def _fallback_char(byte: int) -> tuple[str, str]:
    """One invalid UTF-8 byte as cp1252, or Latin-1 where cp1252 has none."""
    try:
        return bytes([byte]).decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return chr(byte), "latin-1"


def _redaction_mask(text: str) -> bytearray:
    """Which characters of ``text`` the builder's redaction removes.

    The same steps as :meth:`_Counter.redact` (addresses, then spelled-out
    addresses, repeated until neither changes the text), tracked back to
    positions in ``text``.
    """
    mask = bytearray(len(text))
    current = text
    positions = list(range(len(text)))
    for _ in range(_REDACTION_ROUNDS):
        changed = False
        for spans_of in (email_address_spans, obfuscated_email_address_spans):
            spans = spans_of(current)
            if not spans:
                continue
            changed = True
            drop = bytearray(len(current))
            for start, end in spans:
                drop[start:end] = b"\x01" * (end - start)
                for index in range(start, end):
                    mask[positions[index]] = 1
            current = "".join(c for c, d in zip(current, drop, strict=True) if not d)
            positions = [p for p, d in zip(positions, drop, strict=True) if not d]
        if not changed:
            break
    return mask


def _refuse_hidden_address(group: str, where: str, text: str) -> None:
    """Refuse ``text`` when an address has a hidden character in or next to it.

    Runs on the input, before terminal sequences are stripped. Both
    redactors (addresses, then spelled-out addresses, repeated) match three
    copies of the text:

    - with every hidden character (:data:`_HIDDEN_RE`) removed;
    - also without the second character (``[``, ``(``, ``]``) of each
      terminal sequence (:data:`_SEQUENCE_RE`), so ``jdoe ESC [ @example.org``
      reads ``jdoe@example.org``;
    - also without whole terminal sequences, as a terminal would show it
      (``jdoe ESC ( B x@example.org`` reads ``jdoex@example.org``).

    A matched stretch refuses the group when, in ``text``, it contains or
    directly touches a hidden character or any character of a terminal
    sequence. So ``jdoe<DEL>x@example.org``, ``jdoe@example<ESC>.org`` and
    ``<ESC>[31mjdoe@example.org<ESC>[0m`` all refuse: nothing that sits in
    or next to an address is stripped and re-matched.
    """
    hidden = bytearray(len(text))
    for match in _HIDDEN_RE.finditer(text):
        hidden[match.start()] = 1
    if not any(hidden):
        return
    suspect = bytearray(hidden)
    introducers = bytearray(hidden)
    for match in _SEQUENCE_RE.finditer(text):
        start, end = match.span()
        suspect[start:end] = b"\x01" * (end - start)
        if text[start] == "\x1b":
            introducers[start + 1] = 1
    for removed in (hidden, introducers, suspect):
        kept = [i for i, flag in enumerate(removed) if not flag]
        mask = _redaction_mask("".join(text[i] for i in kept))
        index = 0
        while index < len(mask):
            if not mask[index]:
                index += 1
                continue
            end = index
            while end < len(mask) and mask[end]:
                end += 1
            if any(suspect[max(kept[index] - 1, 0) : kept[end - 1] + 2]):
                raise GroupRefused(
                    group,
                    f"{where}: an address is split by a control character "
                    "or touches one",
                )
            index = end


def _decode_with_fallback(data: bytes) -> _Page:
    """Decode UTF-8, falling back per invalid byte; drop them if that hides
    an address.

    ``surrogateescape`` keeps each invalid byte as one placeholder character,
    so two views of the page line up character for character:

    - the fallback view, where each invalid byte becomes its cp1252 (else
      Latin-1) character, which keeps a stray Windows quote readable;
    - the dropped view, where invalid bytes are simply left out.

    A printable fallback character next to or inside an address (``jean.dupont``,
    a stray 0x93, ``@cern.ch``, or ``jdoe``, 0x93, ``[at]cern.ch``) can hide it
    from the redactor. So when any character that redaction removes from the
    dropped view (addresses and spelled-out addresses) would survive redaction
    of the fallback view, the page uses the dropped view instead.
    """
    raw = data.decode("utf-8", errors="surrogateescape")
    counts = {"cp1252": 0, "latin-1": 0}
    fallback_chars: list[str] = []
    kept_positions: list[int] = []
    for index, char in enumerate(raw):
        if "\udc80" <= char <= "\udcff":
            decoded, kind = _fallback_char(ord(char) - 0xDC00)
            counts[kind] += 1
            fallback_chars.append(decoded)
        else:
            fallback_chars.append(char)
            kept_positions.append(index)
    if not any(counts.values()):
        return _Page(raw, {})
    fallback_view = "".join(fallback_chars)
    dropped_view = "".join(raw[i] for i in kept_positions)
    removed = _redaction_mask(fallback_view)
    found = _redaction_mask(dropped_view)
    survives = any(
        flag and not removed[kept_positions[i]] for i, flag in enumerate(found)
    )
    if survives:
        return _Page(dropped_view, counts, bytes_dropped=True)
    return _Page(fallback_view, counts)


def _read_text(group: str, rel: str, path: Path, *, fallback: bool = False) -> _Page:
    """The file's text, decoded and checked.

    A NUL byte always refuses the group (UTF-16 puts a NUL between the letters
    of an address, hiding it from the redactor). Invalid UTF-8 refuses it
    unless ``fallback`` (see :func:`_decode_with_fallback`). Decoded text
    that is more than 1% control characters, and at least 16 of them, not
    counting ANSI colour sequences, is refused as binary. The text is
    returned with its colour sequences; they are stripped after the
    hidden-address check.
    """
    data = path.read_bytes()
    if b"\0" in data:
        raise GroupRefused(group, f"{rel} contains NUL bytes (not UTF-8 text)")
    if fallback:
        page = _decode_with_fallback(data)
    else:
        try:
            page = _Page(data.decode("utf-8"), {})
        except UnicodeDecodeError as exc:
            raise GroupRefused(group, f"{rel} is not UTF-8: {exc}") from exc
    text = _CSI_RE.sub("", page.text)
    if text:
        controls = len(_CONTROL_RE.findall(text))
        if controls >= CONTROL_MIN_COUNT and controls / len(text) > CONTROL_SHARE_LIMIT:
            raise GroupRefused(
                group,
                f"{rel} looks binary: {controls} of {len(text)} characters "
                f"({100 * controls / len(text):.1f}%) are control characters",
            )
    return page


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
    """Every string must be a fixed point of the redactor, also with its
    hidden characters (:data:`_HIDDEN_RE`) removed: a control character
    inside an address (for example a C1 control from the Latin-1 fallback in
    ``a.b@c<U+0081>d.ch``) hides it from the redactor, so such a string
    refuses the group."""
    for path, data in files.items():
        text = data.decode("utf-8")
        strings = _strings(json.loads(text)) if path.endswith(".json") else [text]
        for value in strings:
            _check_string(group, path, value)


def _check_string(group: str, where: str, value: str) -> None:
    if redact_email_addresses(value) != value:
        raise GroupRefused(group, f"{where}: an address survived redaction")
    if redact_obfuscated_email_addresses(value) != value:
        raise GroupRefused(
            group, f"{where}: a spelled-out address survived redaction"
        )
    if _HIDDEN_RE.search(value):
        bare = _HIDDEN_RE.sub("", value)
        if (
            redact_email_addresses(bare) != bare
            or redact_obfuscated_email_addresses(bare) != bare
        ):
            raise GroupRefused(
                group, f"{where}: an address is split by a control character"
            )


def _reader_check(
    spec: GroupSpec,
    redacted: Mapping[str, bytes],
    final: Mapping[str, bytes],
    tmp_dir: str | Path | None = None,
) -> int:
    """Run the reader over two staged copies in one temporary directory.

    The directory is made under ``tmp_dir`` (default: the system temp dir)
    and removed when this returns or raises. It holds two copies of the group,
    so it needs about twice the group's size.
    """
    with tempfile.TemporaryDirectory(
        prefix=f"snapshot-{spec.name}-", dir=tmp_dir
    ) as tmp:
        full_root = Path(tmp) / "redacted"
        final_root = Path(tmp) / "final"
        try:
            for root, files in ((full_root, redacted), (final_root, final)):
                _stage(root, spec.validation_stubs)
                _stage(root, files)
        except OSError as exc:
            full = exc.errno in (errno.ENOSPC, errno.EDQUOT)
            raise GroupRefused(
                spec.name,
                "cannot write the reader-check copy in "
                f"{tmp}{' (disk full?)' if full else ''}: "
                f"{type(exc).__name__}: {exc}",
            ) from exc
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
    tmp_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Build every group, or raise BuildRefused having written nothing.

    ``tmp_dir`` holds the reader-check copies (default: the system temp dir).
    """
    if tmp_dir is not None and not Path(tmp_dir).is_dir():
        raise SnapshotError(f"--tmp-dir {tmp_dir} is not a directory")
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()):
        raise SnapshotError(f"{out} exists and is not empty; refusing to overwrite")
    version = zstd_version()
    prepared: list[PreparedGroup] = []
    refusals: list[GroupRefused] = []
    for group in config.groups:
        try:
            prepared.append(prepare_group(group, tmp_dir=tmp_dir))
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
                **({"file_dates": dict(group.file_dates)} if group.file_dates else {}),
                **({"note": group.note} if group.note is not None else {}),
                "archive": archive.name,
                "sha256": _sha256_file(archive),
                "bytes": archive.stat().st_size,
                "file_count": len(group.files),
                "record_count": group.record_count,
                "addresses_removed": group.addresses_removed,
                "obfuscated_addresses_removed": group.obfuscated_removed,
                "ansi_sequences_stripped": group.ansi_stripped,
                "contents_sha256": contents_digest(group.files),
                "archive_dir": GROUPS[group.name].archive_dir,
                "dropped_fields": list(group.dropped_fields),
                "kept_extra_fields": list(group.kept_fields),
            }
            if group.deep_dropped_keys:
                row["dropped_keys_at_any_depth"] = list(group.deep_dropped_keys)
            if group.input_file:
                row["input_file"] = group.input_file
            if group.text_stats is not None:
                row.update(group.text_stats)
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
