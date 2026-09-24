"""Physics scoping for the TWiki EOS connector — opt-in, never default.

Lifted from cms-kb's ``src/cms_kb/twiki.py`` (``PhysicsTwikiSource``),
itself a fork of okg-deployments' ``cms/cms_sources/twiki_eos.py`` at
``d259d0df``. cms-kb forked the whole connector to add four filter
stages; this module carries only the stages, so the fork can be
deleted and one connector serves both consumers.

The CMS TWiki snapshot is ~43.5k topics, the large majority of them
computing-operations pages. A physics deployment wants the ~8k that
are physics. The filter runs in four stages:

  1. **Allow-list** — topic names that are unambiguously physics
     (PAG-coded paper pages, POG/DPG name roots, physics workbook and
     SWGuide roots). These are the *seeds*.
  2. **Blacklist** — CompOps/WMCore/DataOps topic roots, dropped
     before anything else looks at them.
  3. **Parent-topic closure** — any topic whose ``%META:TOPICPARENT``
     chain reaches a seed. This is what pulls in a physics page whose
     own name says nothing (e.g. ``StatisticsCommittee``).
  4. **Page type** — a topic that survived 1–3 without being kept is
     still kept when its name classifies as documentation worth having
     (howto, faq, tutorial, glossary, ...).

Known limitation, carried verbatim from cms-kb rather than silently
"fixed" here: stage 3 matches ``%META:TOPICPARENT{name="..."}`` against
bare topic names. A parent recorded web-qualified (``CMS.HiggsPhysics``)
does not match a topic keyed ``HiggsPhysics``, so its children do not
enter the closure. The production CMS snapshot records bare names, which
is why the fork never hit this. Changing it would change which pages a
cms-kb build ingests, so it belongs in its own change with a diffed
page count, not in a port whose point is parity.

Nothing here touches the ontology: the filter only ever *selects*
records the connector already produces. Physics-specific vocabulary
(PAG/POG hub nodes and their subtypes) stays with the deployment that
wants it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime only
    from archi.sources.twiki import TwikiRecord


# ── Stage 1: physics allow-list (validated against a 43,537-file snapshot) ──

# PAG codes with digit follow-through — paper-tracking pages.
PAG_CODE_RE = re.compile(
    r"^(HIG|TOP|SUS|SMP|B2G|BPH|BTV|EXO|FTR|HIN|JME|MUO|TAU|TRK|EGM|PPS|FSQ)"
    r"[-_]?[0-9]"
)

# POG / DPG / coordination name roots — physicists prefix filenames with
# the POG name far more often than with the three-letter code.
POG_NAME_RE = re.compile(r"^(Muon|Egamma|JetMET|BTag|Tau|Tracking|Trigger|AlCa)")

# Physics workbook / SWGuide / PhysicsResults roots. ``Top`` is gated on
# ``[A-Z0-9]`` so it cannot collide with TWiki UI stubs.
PHYSICS_ROOT_RE = re.compile(
    r"^(Higgs|SUSY|Exotica|WorkBook|PhysicsResults|StandardModel|HeavyIons"
    r"|HeavyFlavor|SWGuidePhysics|Top[A-Z0-9])"
)


def passes_allow_list(topic: str) -> bool:
    """True when the topic name alone identifies the page as physics."""
    return bool(
        PAG_CODE_RE.match(topic)
        or POG_NAME_RE.match(topic)
        or PHYSICS_ROOT_RE.match(topic)
    )


# ── Stage 2: computing-operations blacklist ─────────────────────────────

COMPOPS_BLACKLIST_RE = re.compile(
    r"^(CompOps|WMCore|DataOps|SiteReadiness|SiteStatus)"
)


def is_compops_topic(topic: str) -> bool:
    """True for the operations topic roots a physics scope excludes."""
    return bool(COMPOPS_BLACKLIST_RE.match(topic))


# ── Stage 4: page-type classification ───────────────────────────────────

_PAGE_TYPE_TITLE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^HowTo[A-Z0-9]"), "howto"),
    (re.compile(r"(FAQ|FrequentlyAsked)", re.I), "faq"),
    (re.compile(r"(Glossary|Acronym|Abbreviat|Terminolog)", re.I), "glossary"),
    (re.compile(r"^Tutorial", re.I), "tutorial"),
    (re.compile(r"^(Overview|Introduction)[A-Z0-9]?"), "overview"),
    (re.compile(r"(SiteMap|TopicMap|TopicTree)", re.I), "topicmap"),
    (re.compile(r"Reference$", re.I), "reference"),
    (
        re.compile(
            r"^(HIG|TOP|SUS|SMP|B2G|BPH|BTV|EXO|FTR|HIN|JME|MUO|TAU|TRK|EGM"
            r"|PPS|FSQ)[\-_]?\d{2,}([\-_]?\d{2,})?"
        ),
        "analysis",
    ),
    (re.compile(r"(Minutes$|Meeting|Weekly|Daily)", re.I), "meeting_minutes"),
    (re.compile(r"^Web(Home|Index|TopicList)$"), "web_page"),
)

KEPT_PAGE_TYPES: frozenset[str] = frozenset({
    "analysis",
    "howto",
    "faq",
    "tutorial",
    "glossary",
    "overview",
    "reference",
    "meeting_minutes",
})


def classify_page_type(title: str, parent_topic: str) -> str:
    """Name-and-parent heuristic for what kind of page this is."""
    for pattern, label in _PAGE_TYPE_TITLE_RULES:
        if pattern.search(title):
            return label
    if parent_topic and re.search(
        r"(Weekly|Daily|Minutes|Agenda)", parent_topic, re.I
    ):
        return "meeting_minutes"
    return "other"


# ── Stage 3: parent-topic transitive closure ────────────────────────────

def compute_keep_set(
    topics: Sequence[str],
    parent_topic_map: dict[str, str],
) -> tuple[set[str], set[str]]:
    """Return ``(seed_set, kept_set)``.

    ``seed_set`` is stage 1. ``kept_set`` is ``seed_set`` plus every
    topic whose parent chain reaches a seed. Cycles are tolerated: each
    topic's answer is memoised, so an ancestor walk visits a topic once.
    """
    seed_set: set[str] = {topic for topic in topics if passes_allow_list(topic)}
    resolved: dict[str, bool] = {}

    def ancestry_hits_seed(topic: str) -> bool:
        walked: list[str] = []
        current: str | None = topic
        while current is not None:
            if current in seed_set:
                outcome = True
                break
            if current in resolved:
                outcome = resolved[current]
                break
            if current in walked:  # cycle
                outcome = False
                break
            walked.append(current)
            current = parent_topic_map.get(current) or None
        else:
            outcome = False
        for topic_name in walked:
            resolved[topic_name] = outcome
        return outcome

    kept_set = set(seed_set)
    for topic in topics:
        if topic in kept_set:
            continue
        if ancestry_hits_seed(topic):
            kept_set.add(topic)
    return seed_set, kept_set


# ── The filter as one pass over parsed records ──────────────────────────

@dataclass(frozen=True)
class PhysicsFilterReport:
    """What the filter did, for the run's counters and the operator."""

    input_total: int
    blacklist_dropped: int
    seed_count: int
    closure_count: int
    kept_count: int
    dropped_topics: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "input_total": self.input_total,
            "blacklist_dropped": self.blacklist_dropped,
            "seed_count": self.seed_count,
            "closure_count": self.closure_count,
            "kept_count": self.kept_count,
            "dropped_count": len(self.dropped_topics),
        }


def filter_records(
    records: Iterable["TwikiRecord"],
) -> tuple[list["TwikiRecord"], PhysicsFilterReport]:
    """Apply stages 2-4 to already-parsed records; stage 1 seeds them.

    Record order is preserved. The returned report carries the stage
    counts the connector logs and the ingest run reports.
    """
    all_records = list(records)
    survivors = [
        record for record in all_records
        if not is_compops_topic(record.title)
    ]
    blacklist_dropped = len(all_records) - len(survivors)

    topics = [record.title for record in survivors]
    parent_map = {record.title: record.parent_topic for record in survivors}
    seed_set, closure_kept = compute_keep_set(topics, parent_map)

    kept: list["TwikiRecord"] = []
    dropped: list[str] = []
    for record in survivors:
        if record.title in closure_kept:
            kept.append(record)
            continue
        page_type = classify_page_type(record.title, record.parent_topic)
        if page_type in KEPT_PAGE_TYPES:
            kept.append(record)
        else:
            dropped.append(record.title)

    report = PhysicsFilterReport(
        input_total=len(all_records),
        blacklist_dropped=blacklist_dropped,
        seed_count=len(seed_set),
        closure_count=len(closure_kept),
        kept_count=len(kept),
        dropped_topics=tuple(dropped),
    )
    return kept, report
