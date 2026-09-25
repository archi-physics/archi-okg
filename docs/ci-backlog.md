# CI: retired, and what it needs to come back

`.github/workflows/ci.yml` was deleted on 2026-09-25. It had not run
successfully since before the composition-root work: every invocation
failed at the first step with

    ##[error]OKG_REPO_TOKEN is not available; refusing to run a partial suite

A workflow that always fails is worse than none — it trains reviewers to
ignore a red tick, and it made "the suite is green" unverifiable for
this repository. The tests themselves pass locally (434 as of the
physics-filter change); nothing was wrong with them.

## Why it could not run

1. **The token is gone.** The job needs read access to the okg substrate,
   which is private. `OKG_REPO_TOKEN` is not set in this repository's
   secrets.
2. **It pointed at a personal fork.** The pin was
   `lucalavezzo/okg`, not `mitdbg/okg`, because the old token was scoped
   to that account — the workflow's own comment records that
   `mitdbg/okg` returned 403. A shared repository's CI should not depend
   on one person's fork.
3. **It triggered on a dead branch.** `push: [archi_v3]`; trunk is
   `main`.

## What bringing it back requires

- A fine-grained PAT with **read-only** access to `mitdbg/okg`, stored as
  an **organisation** secret in `archi-physics` so both this repository
  and cms-kb can see it. An org secret also survives one person's
  account changes, which the fork pin did not.
- Rewriting the workflow to check out `mitdbg/okg` directly at the pin
  this repository tests against, and to trigger on `main`.
- Keeping the fork-PR guard that is already there: a secret exposed to a
  `pull_request` run from a fork is readable by anyone who can open a
  PR. A fork PR should report *skipped*, never a green tick for tests
  that never ran.

## What it should check

Two jobs, both cheap:

- **tests** — checkout, install okg + this package, `pytest`. About two
  minutes.
- **deploy smoke** — the check that actually matters for a distribution
  of connectors: install a profile against a Postgres service container,
  ingest a fixture corpus, publish, and query it back. That is the
  failure mode local unit tests cannot catch, and cms-kb hit three
  instances of it by hand during the composition-root work
  (`bridge_subtype_unknown`, `ProducerPolicyViolation`, and a missing
  `archetype` annotation). The Postgres image is okg's `ops/pg`, which
  builds TimescaleDB + pg_textsearch from source; publishing it once to
  the org's GHCR and pulling it in CI is far cheaper than building per
  run.

cms-kb has no CI at all and needs the same two jobs.
