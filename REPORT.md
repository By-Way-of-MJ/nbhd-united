# Hybrid keyword + semantic journal search for the agent

## What changed

| File | Change |
| --- | --- |
| `apps/journal/document_authoring.py` | Added `document_semantic_search(tenant, base_queryset, query, *, limit)`: embeds `query[:500]` via `generate_embedding(..., seam="journal_search_query_embedding")`, cosine-searches `DocumentChunk` for the tenant under `base_queryset`, takes the nearest 20, filters similarity `< 0.30`, collapses to the best chunk per document, and returns `(document, chunk_text, similarity)` in similarity order (at most `limit`). Empty corpus short-circuits before embedding; all failures are logged without query text and return `[]`. |
| `apps/integrations/runtime_views.py` | `RuntimeJournalSearchView.get` now runs keyword (`document_fts_search`, unchanged) and semantic search, fuses with Reciprocal Rank Fusion (`1 / (60 + rank)`, rank from 1), and returns the top `limit`. Every result has `match: "keyword" \| "semantic" \| "both"`. Semantic-only results use the best chunk text truncated to 300 chars as `snippet` and `rank: 0.0`; keyword and both results keep the existing snippet/rank behavior. `count` is the number of returned results. |
| `apps/integrations/tests_journal_search.py` | Added the two required tests (semantic rescue and embedding-failure fallback), with `generate_embedding` mocked so no network call occurs. No existing test was edited or deleted. |
| `runtime/openclaw/plugins/nbhd-journal-tools/index.js` | Updated the `nbhd_journal_search` description only because it said the search was keyword/full-text; it now adds “matches by meaning as well as keywords”. |
| `REPORT.md` | This report. |

Seam registration: there is no seam allow-list or registration file in this repo. Existing names such as `session_start_query_embedding`, `lesson_search_query_embedding`, and `journal_chunk_index_embedding` are free-form labels passed directly to `generate_embedding`/`redact_known_values`; the new `journal_search_query_embedding` seam follows that same pattern. No extra file needed changing.

## Tests added

1. `test_semantic_search_rescues_meaning_match` — 12 daily Documents mention “photo”; one Document plus its `DocumentChunk` says “Nearest studios in Horie: PADstudio”. The mocked query embedding equals that chunk's vector. Query `photo places Horie` returns that document as result 0 with `match` in `{"semantic", "both"}`.
2. `test_embedding_failure_falls_back_to_keyword_results` — a chunk exists so semantic search actually calls the mocked embedding, which raises. The response is status 200 and exactly equals the keyword-only response captured before the chunk existed.

## Acceptance checks

### Ruff

```text
$ ruff check apps/journal/document_authoring.py apps/integrations/runtime_views.py apps/integrations/tests_journal_search.py
All checks passed!
$ ruff format --check apps/journal/document_authoring.py apps/integrations/runtime_views.py apps/integrations/tests_journal_search.py
3 files already formatted
```

### Targeted test module

```text
$ make test-local TESTS=apps.integrations.tests_journal_search
./scripts/test-local.sh apps.integrations.tests_journal_search
Test DB: test_nbhd_20261020_111110_dsh_nbhd_united_5fd4_1c00e0
DATABASE_URL host: 127.0.0.1; database: postgres
Found 10 test(s).
System check identified no issues (0 silenced).
...
----------------------------------------------------------------------
Ran 10 tests in 0.920s

OK
Destroying test database for alias 'default'...
```

### Full journal app

```text
$ make test-local TESTS=apps.journal
...
----------------------------------------------------------------------
Ran 630 tests in 151.413s

OK
Destroying test database for alias 'default'...
```

The local test database had pgvector available (the `DocumentChunk` vector writes and cosine queries succeeded).

## Grounding probe

`apps/orchestrator/grounding_probe.py` was not touched; it still calls only `document_fts_search`, so it now under-predicts the hybrid endpoint and cannot report a semantic-only match as reachable.
