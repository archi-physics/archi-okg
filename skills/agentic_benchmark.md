# CMS CLI Agent Benchmark Mode

Use this skill when a CMS benchmark question should be answered by a real
external CLI agent connected to the CMS OKG MCP server.

## Leakage Boundary

- Accept only sanitized fields: `question_id`, `category`, and `question`.
- Do not read, request, summarize, or infer from comparator answers,
  comparator sources, baseline answer files, or private evaluator reports.
- Keep comparator access in the separate redacted evaluator only.

## Agent Harness

- The harness launches a real provider CLI, not an in-process traversal policy.
- Supported providers are Claude Code CLI and Codex CLI; tests use a fake CLI
  with the same stdin/stdout JSON contract.
- Live provider runs require operator acknowledgement because prompts and MCP
  query results can leave the local machine through the provider.
- The MCP server is named `cms_okg` and must be pinned to the requested
  generation whenever a tool accepts `generation_id`.

## Answer Contract

For every question that needs OKG evidence:

1. Use the minimum sufficient evidence path. A verified exact inspection or
   complete bounded aggregate may stop after one call. A clearly out-of-scope
   question may stop without a graph call.
2. Try another query variant only when the first result is ambiguous,
   incomplete, stale, or from the wrong authority family.
3. Inspect canonical parents for chunk hits. Expand only when the answer needs
   parent, relationship, historical, or multi-hop context.
4. Rerank evidence clusters by canonical source ID, not by chunk rank alone.
5. Return real evidence IDs and any traversal receipts, query variants,
   tool-call count, confidence, and gaps that actually occurred.
6. Classify the operator knowledge type and assemble typed evidence before
   writing the final answer. Include `operator_knowledge_type`,
   `typed_evidence`, and `answer_completeness` when the harness requests them.

For current/latest/status/deployment questions, include one timestamp-ranked
retrieval pass over candidate canonical nodes. Use `updated`, `last_updated`,
`observed_at`, `updated_at`, `created`, or `date` according to subtype, and
record the source dates in evidence notes when they affect source authority.

For a question that needs OKG evidence, early stop is allowed only when the
answer includes a verified canonical ID, the requested value or command, and
a note explaining why further traversal would not change the answer.

## Evidence IDs

Prefer canonical IDs in final answers:

- `jira:*` for JIRA issues.
- `twiki:CMS:*` or other documentation page IDs for docs.
- `site:*`, `se:*`, `monitoring_snapshot:*`, `dataset:*`, and `workflow:*` for
  operational graph evidence.

Chunk IDs are supporting evidence. A chunk-only answer is weak unless no
parent/source ID exists.

`okg.v_*` SQL view names are not evidence IDs. Use them in traversal receipts
or evidence notes to describe how rows were found, but cite the canonical rows
or chunk IDs that were actually observed as evidence.
