"""Live Indico connector — events read from the API, not from a cache.

archi's other Indico source (:mod:`archi.sources.indico`) reads a
``records.json`` that something else must produce. Nothing does: not
archi, not okg-deployments' cms deployment, not its cms_tools. This
reader closes that gap by talking to Indico directly.

PORTED FROM ARCHI V2, ``archi-physics/archi`` at ``dev``,
``src/data_manager/collectors/scrapers/integrations/indico_scraper.py``
(1,287 lines). What came across is the part worth keeping: the
``/export/event/<id>.json`` metadata call, the
``?detail=contributions`` call, the **timetable fallback** for events
whose contributions endpoint returns empty, and the category walk.

WHAT DID NOT COME ACROSS is v2's authentication. It logs in with
Playwright, driving a browser through CERN SSO and harvesting cookies
mid-run. v3 does not run an interactive login in-process -- see
``archi/auth/cookies.py`` -- and reads a Netscape cookie file produced
out of band by CERN's Kerberos-backed ``auth-get-sso-cookie``, exactly
as the TWiki crawl and the SSO docs source already do. One consequence
is worth stating: this connector never holds a credential. Only the
NAME of the environment variable holding the cookie file's PATH travels
through configuration.

Public Indico categories need no cookie file at all, and the connector
does not demand one.

EMISSION IS NOT REIMPLEMENTED. The reader builds the same
``IndicoEventRecord`` objects the cache-backed source builds and hands
them to the same ``_meeting_node`` / ``_pdf_document_facts`` functions,
so a deployment can swap one source for the other without a schema
change and neither can drift from the other. Only the acquisition
differs, which is the whole point.

Attachment text extraction (v2's MarkItDown slide conversion) is NOT
ported here: it pulls a document-conversion dependency into the
substrate path for a gain this connector can add later, and the
cache-backed source already carries the ``pdf_texts`` field for when it
does. Attachment URLs are emitted; their contents are not fetched.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator

import requests

from okg.deployment import (
    ConnectorHealth,
    ConnectorRun,
    ContentHashProbe,
    PreflightResult,
)

from archi.auth.cookies import load_cookie_jar_from_env, looks_like_login_page
from archi.sources._sdk_adapter import ReaderAdapter
from archi.sources.indico import (
    DEFAULT_CHUNKER_NAME,
    IndicoEventRecord,
    _meeting_node,
    _pdf_document_facts,
)

DEFAULT_BASE_URL = "https://indico.cern.ch"
DEFAULT_TIMEOUT = 30.0


class IndicoLiveSource:
    """Read Indico events and their contributions over the REST API."""

    name = "indico_live"
    profile = "discovery_crawl"
    change_probe_kind = "content_hash"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        event_ids: list[str] | tuple[str, ...] = (),
        category_ids: list[str] | tuple[str, ...] = (),
        cookie_file_env: str | None = None,
        required: bool = False,
        max_events: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        chunker_name: str = DEFAULT_CHUNKER_NAME,
        records: list[IndicoEventRecord] | None = None,
        base: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.event_ids = tuple(str(e) for e in event_ids)
        self.category_ids = tuple(str(c) for c in category_ids)
        if not self.event_ids and not self.category_ids and records is None:
            raise ValueError(
                "configure event_ids or category_ids: a live Indico source "
                "with neither has no scope to read"
            )
        self.cookie_file_env = cookie_file_env
        self.required = required
        if max_events is not None and max_events < 1:
            raise ValueError(
                f"max_events must be >= 1 (or None for unlimited), "
                f"got {max_events}"
            )
        self.max_events = max_events
        self.timeout = timeout
        self.chunker_name = chunker_name
        self._records = records
        self.base = base
        #: Counters from the last run. This module logs nothing, matching
        #: the rest of archi.sources.
        self.last_harvest_report: dict[str, Any] | None = None
        self.change_probe = ContentHashProbe(
            content_items=self._probe_content_items,
            config={
                "base_url": self.base_url,
                "event_ids": list(self.event_ids),
                "category_ids": list(self.category_ids),
                "max_events": self.max_events,
            },
            emit_targets=IndicoLiveSource,
        )

    # ── session ────────────────────────────────────────────────────────

    def _session(self) -> requests.Session:
        session = requests.Session()
        session.headers["User-Agent"] = "archi-okg/indico-live"
        if self.cookie_file_env:
            jar = load_cookie_jar_from_env(self.cookie_file_env)
            if jar is not None:
                session.cookies = jar
        return session

    def _get_json(self, session: requests.Session, path: str) -> Any:
        response = session.get(
            f"{self.base_url}{path}", timeout=self.timeout
        )
        response.raise_for_status()
        if looks_like_login_page(response.text):
            # CERN answers an unauthenticated request with a login page
            # and HTTP 200. Parsing it as JSON would fail; parsing it as
            # an event would publish the login form as meeting minutes.
            raise _LoginRedirect(path)
        return response.json()

    # ── lifecycle ──────────────────────────────────────────────────────

    def preflight(self, mode: str = "live") -> PreflightResult:
        if self._records is not None:
            return PreflightResult(
                source_name=self.name, status="ok", mode="fixture",
                required=self.required, record_count=len(self._records),
                reason="fixture records supplied",
            )
        probe = self.event_ids[0] if self.event_ids else None
        path = (
            f"/export/event/{probe}.json" if probe
            else f"/export/category/{self.category_ids[0]}.json"
        )
        try:
            self._get_json(self._session(), path)
        except _LoginRedirect:
            return PreflightResult(
                source_name=self.name, status="auth_failed", mode="live",
                required=self.required, endpoint=self.base_url,
                credential_refs=(
                    (self.cookie_file_env,) if self.cookie_file_env else ()
                ),
                reason=(
                    "Indico answered with a login page: the cookie file is "
                    "missing, expired, or for another host. Refresh it with "
                    "CERN's auth-get-sso-cookie (Kerberos-backed); this "
                    "connector never logs in itself"
                ),
            )
        except requests.RequestException as exc:
            return PreflightResult(
                source_name=self.name, status="endpoint_failed", mode="live",
                required=self.required, endpoint=self.base_url,
                reason=f"Indico unreachable: {exc}",
            )
        return PreflightResult(
            source_name=self.name, status="ok", mode="live",
            required=self.required, endpoint=self.base_url,
            credential_refs=(
                (self.cookie_file_env,) if self.cookie_file_env else ()
            ),
            reason="Indico API reachable",
        )

    def run(self, run_id: str, *, mode: str = "cursor") -> ConnectorRun:
        if self._records is not None:
            records = list(self._records)
            report = {"events": len(records), "failed_events": [],
                      "truncated": False, "timetable_fallbacks": 0}
        else:
            records, report = self._harvest()
        self.last_harvest_report = report

        complete = not report["failed_events"] and not report["truncated"]
        revision = {
            "run_id": run_id,
            "content_hash": _records_hash(records),
            "n_records": len(records),
            "base_url": self.base_url,
        }

        def _facts() -> Iterator[Any]:
            for record in records:
                yield _meeting_node(record, revision)
                yield from _pdf_document_facts(
                    record, revision, self.chunker_name
                )

        if not complete:
            # An event that failed to read, or a bound that truncated the
            # walk, means this run saw less than its declared scope.
            # Claiming completion would retract every event it missed.
            failed = report["failed_events"]
            reason = (
                f"{len(failed)} event(s) failed to read, e.g. "
                f"{', '.join(failed[:3])}"
                if failed else
                f"walk truncated at max_events={self.max_events}"
            )
            return ConnectorRun(
                facts=_facts(), completed_scope=False, run_mode=mode,
                health=ConnectorHealth(
                    status="endpoint_failed" if failed else "ok",
                    mode="live", endpoint=self.base_url,
                    record_count=len(records),
                    reason=f"{reason}; no complete scope claimed",
                ),
            )
        return ConnectorRun(
            facts=_facts(),
            completed_scope=(mode in {"scope_complete", "reconcile"}),
            run_mode=mode,
            health=ConnectorHealth(
                status="ok",
                mode="fixture" if self._records is not None else "live",
                endpoint=self.base_url,
                record_count=len(records),
                content_hash=revision["content_hash"],
                credential_refs=(
                    (self.cookie_file_env,) if self.cookie_file_env else ()
                ),
                reason=f"Indico live read of {self.base_url}",
            ),
        )

    # ── harvest ────────────────────────────────────────────────────────

    def _probe_content_items(self) -> list[tuple[str, Any]]:
        if self._records is not None:
            return [
                (r.event_id, json.dumps({"t": r.title, "d": r.date}).encode())
                for r in self._records
            ]
        # The scope's identity: which events, and when each last changed.
        # Reading the metadata call only (not contributions) keeps the
        # probe far cheaper than the run it guards.
        items: list[tuple[str, Any]] = []
        session = self._session()
        for event_id in self._event_ids_in_scope(session, probe=True):
            try:
                payload = self._get_json(
                    session, f"/export/event/{event_id}.json"
                )
            except (requests.RequestException, _LoginRedirect):
                continue
            event = _first_result(payload)
            items.append((
                event_id,
                json.dumps(
                    {"title": event.get("title", ""),
                     "modified": event.get("modificationDate", "")},
                    sort_keys=True, default=str,
                ).encode("utf-8"),
            ))
        return items

    def _event_ids_in_scope(
        self, session: requests.Session, *, probe: bool = False
    ) -> list[str]:
        ids = list(self.event_ids)
        for category_id in self.category_ids:
            try:
                payload = self._get_json(
                    session, f"/export/category/{category_id}.json"
                )
            except (requests.RequestException, _LoginRedirect):
                if probe:
                    continue
                raise
            for event in payload.get("results", []) or []:
                event_id = str(event.get("id", "")).strip()
                if event_id and event_id not in ids:
                    ids.append(event_id)
        return ids

    def _harvest(self) -> tuple[list[IndicoEventRecord], dict[str, Any]]:
        session = self._session()
        failed: list[str] = []
        fallbacks = 0
        records: list[IndicoEventRecord] = []

        try:
            event_ids = self._event_ids_in_scope(session)
        except (requests.RequestException, _LoginRedirect):
            return [], {"events": 0, "failed_events": list(self.category_ids),
                        "truncated": False, "timetable_fallbacks": 0}

        truncated = False
        if self.max_events is not None and len(event_ids) > self.max_events:
            event_ids = event_ids[:self.max_events]
            truncated = True

        for event_id in event_ids:
            try:
                payload = self._get_json(
                    session, f"/export/event/{event_id}.json"
                )
            except (requests.RequestException, _LoginRedirect):
                failed.append(event_id)
                continue
            event = _first_result(payload)
            contributions, from_timetable = self._contributions(
                session, event_id
            )
            fallbacks += int(from_timetable)
            records.append(_record_from_event(
                event, event_id, contributions, self.base_url
            ))

        return records, {
            "events": len(records),
            "failed_events": failed,
            "truncated": truncated,
            "timetable_fallbacks": fallbacks,
        }

    def _contributions(
        self, session: requests.Session, event_id: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Contributions for an event, falling back to the timetable.

        Some events carry their programme in timetable sessions rather
        than as direct contributions, and the contributions endpoint
        returns an empty list for them. v2 handled this and so does
        this: an empty answer is indistinguishable from a meeting with
        no talks unless you look.
        """
        try:
            payload = self._get_json(
                session, f"/export/event/{event_id}.json?detail=contributions"
            )
            contributions = _first_result(payload).get("contributions") or []
        except (requests.RequestException, _LoginRedirect):
            contributions = []
        if contributions:
            return list(contributions), False

        try:
            payload = self._get_json(
                session, f"/export/timetable/{event_id}.json"
            )
        except (requests.RequestException, _LoginRedirect):
            return [], False
        return _contributions_from_timetable(payload, event_id), True


class _LoginRedirect(RuntimeError):
    """Indico answered with a login page instead of data."""


def _first_result(payload: Any) -> dict[str, Any]:
    """Indico wraps a single object in a ``results`` list."""
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list) and results:
            first = results[0]
            return first if isinstance(first, dict) else {}
        return payload
    return {}


def _contributions_from_timetable(
    payload: Any, event_id: str
) -> list[dict[str, Any]]:
    """Pull contributions out of a timetable payload.

    v2 scraped the timetable's HTML with BeautifulSoup, and through
    Selenium when the page needed JavaScript. This reads Indico's JSON
    timetable instead: same information, no browser, no HTML parsing,
    and nothing that silently returns zero when a page renders
    differently.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, dict):
        return []
    day_map = results.get(str(event_id)) or next(
        (v for v in results.values() if isinstance(v, dict)), {}
    )
    contributions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for day in (day_map or {}).values():
        if not isinstance(day, dict):
            continue
        for entry in day.values():
            for item in _timetable_items(entry):
                contribution_id = str(
                    item.get("contributionId") or item.get("id") or ""
                ).strip()
                if not contribution_id or contribution_id in seen:
                    continue
                seen.add(contribution_id)
                contributions.append({
                    "id": contribution_id,
                    "title": item.get("title", ""),
                    "description": item.get("description", ""),
                    "speakers": item.get("speakers", []),
                    "folders": item.get("folders", []),
                    "_from_timetable": True,
                })
    return contributions


def _timetable_items(entry: Any) -> Iterator[dict[str, Any]]:
    """Yield contribution-shaped items from one timetable entry.

    A session block nests its talks under ``entries``; a bare
    contribution is the item itself.
    """
    if not isinstance(entry, dict):
        return
    if entry.get("entryType") in {"Contribution", "Break"} or entry.get("contributionId"):
        if entry.get("entryType") != "Break":
            yield entry
    for nested in (entry.get("entries") or {}).values():
        yield from _timetable_items(nested)


def _record_from_event(
    event: dict[str, Any],
    event_id: str,
    contributions: list[dict[str, Any]],
    base_url: str,
) -> IndicoEventRecord:
    """Build the record shape the cache-backed source already emits."""
    speakers: list[str] = []
    attachments: list[str] = []
    contribution_text: list[str] = []

    for contribution in contributions:
        title = (contribution.get("title") or "").strip()
        description = (contribution.get("description") or "").strip()
        if title or description:
            contribution_text.append(
                " — ".join(part for part in (title, description) if part)
            )
        for speaker in contribution.get("speakers") or []:
            name = _person_name(speaker)
            if name and name not in speakers:
                speakers.append(name)
        for folder in contribution.get("folders") or []:
            for attachment in folder.get("attachments") or []:
                url = attachment.get("download_url") or attachment.get("url")
                if url:
                    attachments.append(
                        url if url.startswith("http") else f"{base_url}{url}"
                    )

    chairs = [
        name for name in (
            _person_name(person) for person in event.get("chairs") or []
        ) if name
    ]
    category_id = event.get("categoryId")
    return IndicoEventRecord(
        event_id=str(event_id),
        title=event.get("title", "") or "",
        url=event.get("url", "") or f"{base_url}/event/{event_id}/",
        description=event.get("description", "") or "",
        date=_stamp(event.get("startDate")),
        end_date=_stamp(event.get("endDate")),
        event_type=event.get("type", "") or "",
        category=event.get("category", "") or "",
        category_id=int(category_id) if str(category_id or "").isdigit() else None,
        speakers=tuple(speakers),
        chairs=tuple(chairs),
        attachment_urls=tuple(attachments),
        contributions_text="\n".join(contribution_text),
    )


def _person_name(person: Any) -> str:
    if isinstance(person, str):
        return person.strip()
    if not isinstance(person, dict):
        return ""
    full = (person.get("fullName") or "").strip()
    if full:
        return full
    parts = [
        (person.get("first_name") or person.get("firstName") or "").strip(),
        (person.get("last_name") or person.get("familyName") or "").strip(),
    ]
    return " ".join(part for part in parts if part)


def _stamp(value: Any) -> str:
    """Indico dates arrive as {"date": ..., "time": ..., "tz": ...}."""
    if isinstance(value, dict):
        date = (value.get("date") or "").strip()
        time = (value.get("time") or "").strip()
        return f"{date}T{time}" if date and time else date
    return str(value or "")


def _records_hash(records: list[IndicoEventRecord]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda r: r.event_id):
        digest.update(record.event_id.encode("utf-8"))
        digest.update(record.title.encode("utf-8"))
        digest.update(record.contributions_text.encode("utf-8"))
    return digest.hexdigest()


class IndicoLiveAdapter(ReaderAdapter):
    """Registry entry point for :class:`IndicoLiveSource`."""

    reader_class = IndicoLiveSource
    profile = "discovery_crawl"
    change_probe_kind = "content_hash"
