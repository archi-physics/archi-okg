"""Run modes in which a source run covers its whole scope.

okg passes a source its run mode and trusts the source's
``completed_scope`` claim, so a reader must know which modes complete a
scope. okg keeps the table in ``okg.substrate.source_run_modes``
(``COMPLETED_SCOPE_RUN_MODES``), but a connector may not import
``okg.substrate``: ``okg lint deployments`` reports DEPLOYMENT-001 for
it. So archi keeps this copy, and
``tests/sources/test_run_modes.py`` asserts it equals okg's set at the
pinned okg commit, which catches drift at every pin bump.

Once the okg pin includes ``okg.deployment.COMPLETED_SCOPE_RUN_MODES``
(okg branch ``claude/connector-complete-scope-modes``), readers should
import that instead and this module should go.

Per profile, at okg ``ac078aabd``: ``discovery_crawl`` completes in
``scope_complete``, ``mutable_api`` in ``reconcile``, and
``reference_catalog`` in ``release_new`` and ``release_unchanged``.
"""

COMPLETED_SCOPE_RUN_MODES: frozenset[str] = frozenset(
    {"scope_complete", "reconcile", "release_new", "release_unchanged"}
)
