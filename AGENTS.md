# archi-okg — working rules

Archi is the **HEP distribution of OKG**: connectors, enrichers, skills,
schemas and bundles, shipped as the pip wheel `archi` built from
[`python/`](python/). It contains no credentials, no site config and no
running services. It is installed onto an OKG deployment through a
bundle; instances live in their own repositories and carry the
configuration and secrets.

| | |
|---|---|
| `python/archi/` | The wheel: sources, enrichers, schemas. |
| `python/tests/` | The suite. 1,673 tests. |
| `bundles/` | Install bundles (e.g. `cern-team`). |
| `skills/` | Playbooks for agents operating over the graph. |
| `pact/changes/` | The change record. |
| `docs/okg-alignment.md` | The okg-side program's window into this repo. |

> **`CLAUDE.md` was removed.** It described a repository layout that no
> longer exists — a branch, runbooks and PACT paths that are all gone
> (see "What the old file got wrong" at the end). Claude Code and Codex
> both read `AGENTS.md`; this is the one place working rules live. Do
> not re-create `CLAUDE.md`.

## Where work lands

**PRs target `main`.** It is the only branch on the remote and the
repository default.

The v3 program spec (`docs/adr/0001-archi-v3-program-spec.md`) says all
Archi work lands on `archi_v3`, branched from `main`, and never on
`main`. That was true of the **predecessor repository**,
`archi-physics/archi`, where `main` served live v2 deployments. That
program migrated here, and this repository *is* the v3 distribution —
there is no v2 to protect and no `archi_v3` branch to protect it with.
The ADR is kept as history; read its branch and cutover instructions as
describing the old repo, not this one.

Everything merged since this repo took over has gone to `main`. If you
find yourself about to open a PR against anything else, that is the
stale instruction talking.

## Standing rules

- **This repository is public; the okg substrate is private.** Nothing
  here may carry a credential, a site hostname or an instance's
  configuration. The tests import `okg`, so CI installs it from a
  read-only deploy key — which is why a fork PR reports *skipped*
  rather than *passed*: no green tick is claimed for tests that never
  ran. A maintainer re-runs it from a branch in this repo before merge.
- **A consumer's variant is an option here, off by default.** When a
  deployment needs a source to behave differently, add the option to
  the source with a default that preserves existing behaviour, and let
  the deployment turn it on in its profile. `physics_filter` and
  `page_index_path` on `TwikiEOSSource` are both this shape. A second
  copy of a source in a downstream repo is what this distribution
  exists to prevent.
- **Connectors are kept line-comparable** with the frozen canonical
  `okg-deployments/cms` copies, so an upstream bug report cites matching
  lines. That is why the format check is report-only: enforcing `black`
  would reformat 58 files and destroy the correspondence.
- **`docs/okg-alignment.md` is a contract, not a status page.** Any
  change that touches `python/archi`'s okg imports, bumps the okg
  commit tested against, or moves program state must update its
  "Current state" section and its pins **in the same PR**.
  `python/tests/test_alignment_page.py` enforces the import-surface
  half mechanically; the prose half is on you. The okg pin in
  `.github/workflows/ci.yml` must equal the commit in that page's
  *Last updated* line, or the page describes a build that never ran.

## Running the suite

```bash
pip install -e "./python[dev]"
python -m pytest python/tests -q
```

That is what CI runs. From a checkout that already has `okg` importable
(a cms-kb venv, for instance), `cd python && PYTHONPATH=. python -m
pytest tests -q` does the same thing without installing.

## PACT

Changes are recorded in `pact/changes/<id>/pact.yaml`, edited directly —
the CLI is reactive. The corpus here is **v5**, git-native, and takes
the git toplevel as its root:

```bash
okg pact <verb>        # from any venv carrying okg
```

Verbs: `new`, `view`, `evidence attach`, `check`, `approve`, `digest`.
**`check` is the one gate** — it validates, binds receipts, verifies
approval and computes the tier in one call. `approve` records approval
as a `Pact-Approved` git commit trailer, not a ledger row.

Unlike cms-kb, **`check` needs no `--base` here**: its default base
resolves against this repo's `main`.

```bash
okg pact check <id>
```

Two things that will otherwise waste your time:

- **A v5 task has no status field** — only `id`, `requirement` and
  `verification`. Completion is derived from receipts bound to a
  commit, so evidence whose `head` predates the base reads as
  `evidence_head_before_base` even when it is otherwise correct. Attach
  evidence *after* committing, and rebind it if you amend.
- **`pact/project.yaml` declares `pact_version: 4` on purpose.** The v4
  compatibility reader still uses it and v5 never reads it. Do not
  "fix" it.

Every change gets a manifest, before the branch has commits on it. The
check that catches a missing one costs a command:

```bash
git diff --name-only origin/main...HEAD | grep '^pact/changes/' || echo "NO PACT"
```

## Reporting

PR bodies and closeouts, in order: (1) what this changes, one plain
sentence a reader who is not a substrate expert can follow;
(2) behaviour before → after; (3) findings, ordered by severity, each
with a `file:line`; (4) how I verified — the command and its result.

No narration of how the work went, no invented nouns, no undefined
jargon unless glossed on first use. A correct diff with an
unintelligible description is not done.

## What the old file got wrong

`CLAUDE.md` is deleted rather than edited because most of what it
pointed at is gone. Recorded here so nobody restores it from memory:

| it said | actually |
|---|---|
| PRs target `archi_v3`, never `main` | `main` is the only branch on the remote; every merged PR targets it |
| `docs/runbooks/agent-*.md` are the backing contracts | `docs/runbooks/` is empty |
| `pact/AGENTS.md` holds the PACT contract | no such file |
| `pact/project.md` is human context | no such file |
| `.github/PULL_REQUEST_TEMPLATE.md` hard-codes the section order | no such file |
| `docs/runbooks/plain-language-reporting.md` is the how | no such file |
| skills are installed under `.claude/skills/` from `src/okg/substrate/pact/skills/` | skills live in `skills/`; neither path exists |
| use `uv run okg` | there is no `uv run okg` here |
| `okg pact gate` / `decide` / `hooks` close a task | suppressed in v5; `check` is the gate |

Most of those came from a managed `okg-pact-install` block — okg's
generic boilerplate, written for okg's own layout. It is **not**
reproduced here: a block of dead paths is worse than no block. If
anyone runs `okg pact install` in this repo, check its output against
the table above before trusting it.
