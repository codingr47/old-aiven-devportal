# Documentation golden dataset

A Python CLI and localhost review application for generating an evidence-backed
benchmark from **this repository's documentation snapshot**. It does not query a
live Aiven service, browse external documentation, or run RAG experiments.

Run commands from the repository root. Python 3.9+ is supported. The repository's
Sphinx environment must also be installed to build a fresh snapshot.

```powershell
venv/Scripts/python.exe -m pip install -r tools/golden_dataset/requirements.txt
venv/Scripts/python.exe -m tools.golden_dataset --help
```

## Start a run

Copy `.env.example` to `.env` in the repository root and set `GOLDEN_BASE_URL`,
`GOLDEN_GENERATOR_MODEL`, and `GOLDEN_REVIEWER_MODEL` for your provider. The base URL includes the
API prefix (for example `http://localhost:8001/v1`); the client appends
`/chat/completions`. Set `GOLDEN_API_KEY` in `.env`; local endpoints that do not
require authentication can leave it blank. Environment variables override file
values. The key stays in memory and is excluded from configuration fingerprints
and checkpoints. `.env` and `.env.*` files are ignored by Git, except `.env.example`.

```powershell
Copy-Item tools/golden_dataset/.env.example .env
# Edit .env before generation. Keep an existing .env; add the GOLDEN_ settings to it.
venv/Scripts/python.exe -m tools.golden_dataset inventory
venv/Scripts/python.exe -m tools.golden_dataset generate
venv/Scripts/python.exe -m tools.golden_dataset status
venv/Scripts/python.exe -m tools.golden_dataset review
```

The default database is `_build/golden-dataset/run.sqlite3`. The fresh Sphinx HTML
snapshot goes to `_build/golden-snapshot/`, which must be empty before inventory.
Use the global `--db PATH` **before** the subcommand to manage separate runs:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/smoke/run.sqlite3 inventory --build-dir _build/smoke/html --page docs/products/kafka/howto/configure-log-cleaner.html --page docs/products/kafka/concepts/log-compaction.html
venv/Scripts/python.exe -m tools.golden_dataset --db _build/smoke/run.sqlite3 generate --max-sections 2
venv/Scripts/python.exe -m tools.golden_dataset --db _build/smoke/run.sqlite3 resume
```

`generate` and `resume` load `.env` by default. Use `--env-file PATH` to select
another file. Files support dotenv quoting and comments; interpolation is disabled
so literal characters in credentials are preserved. The loader does not modify
the process environment. Missing files or invalid settings fail before API calls.

For a small manual test, set this optional value in `.env`:

```dotenv
GOLDEN_MAX_SECTIONS=3
```

Each `generate` or `resume` invocation processes at most three queued target
sections, checkpoints, and pauses with exit code `2` if work remains. Run `resume`
for the next batch. Leave the value blank or remove it to process the full queue;
`--max-sections N` overrides the environment setting for that invocation.
The limit applies to target sections, not pages, questions, or API requests;
linked evidence may also be read during a section's browsing journey. Inventory
still covers the full selected corpus, and limited runs retain incomplete
coverage rather than claiming the dataset is ready.

`--page` restricts the inventory and its coverage denominator; a scoped run never
claims to cover the full repository. A scoped run still builds the full Sphinx
site. `inventory --use-existing-build --build-dir PATH` explicitly skips building;
the manifest records that freshness has not been established. Normal runs should
use a fresh build. Sphinx warnings are emitted and do not abort a successful
build; missing pages and extraction errors block generation. Existing builds must
include their `.doctrees` directory. Only use trusted local Sphinx builds: these
are Python pickle files, not an upload format. Markdown pages are included when
the documentation project's Sphinx configuration has a Markdown parser enabled.

Snapshot identity includes the Git commit, source/rendering-input hashes, build
command, rendered HTML hashes, normalization version, and selected pages. Resume,
review writes, and export reject changed source or rendered content. Create a
new database/build directory for changed documentation. Existing databases and
exports are never overwritten implicitly.

## Reasoning and coverage

The agent alternates persona-driven exploration with a deterministic work queue.
Its JSON action protocol exposes `list_navigation`, `open_page`, `read_section`,
`follow_internal_link`, `search_snapshot`, and `remaining_coverage`. These are
local functions, not shell or arbitrary filesystem access. A model can finish a
journey, but cannot finish the run while unprocessed content remains.

Every section owns its direct text, excluding text belonging to nested sections.
Normalization collapses whitespace for browsing, preserving text from tables,
warnings, included snippets, and code. Browsing offsets refer to that displayed
section text. Exported source offsets instead refer to exact repository file text,
decoded as UTF-8 with LF newlines. Image interpretation is outside this version.
Empty and link-only sections have explicit exclusion reasons. External links are
recorded but never fetched.

Long sections split into contiguous 6,000-character reading units. Each unit is
sent to the generator for a factual reading summary and checkpointed only after
a valid response. Question generation gets bounded primary/linked evidence;
summaries are not valid evidence. The queue aims for one accepted question per
substantive section. Cross-page questions are requested when a linked task makes
multiple necessary sources useful, without a fixed quota.

Candidates pass deterministic evidence/schema/duplicate checks followed by an
independent reviewer call. Separate model names are configurable; using a
different reviewer model reduces correlated errors, but remains a model-based
quality check. The reviewer sees question content and evidence context, never
generator summaries or journey decisions. There are three generation attempts
per section. Rejections remain visible. Exhausted sections appear as gaps;
`resume --retry-gaps` explicitly grants another group of attempts.

## Interruptions, budgets, and provider compatibility

`resume` continues the same database after Ctrl+C, a request failure, or a budget
pause. Completed reading units and accepted questions are not regenerated.
Candidates awaiting independent review resume at that stage. A crash during a
request can repeat that request; an already reserved usage entry is retained
conservatively because its actual billing is unknown. Writes use an OS lock and
SQLite transactions. Review reads remain available during generation; review
writes require generation to finish or pause.

The compatible protocol uses plain Chat Completions messages and JSON text;
native function calling or structured-output support is not required. Both
models pass a connectivity/JSON preflight. Set provider-specific reasoning or
output options in `GOLDEN_PARAMETERS` as a JSON object, with a positive `max_tokens` or
`max_completion_tokens` when overriding the default. Streaming, multiple choices, and overriding the managed
messages/model are not supported. Default concurrency is one request.

`GOLDEN_MAX_TOKENS` is an optional **run-wide** token budget;
`GOLDEN_OUTPUT_TOKENS` is the **per-response** output limit (default 4096), unless
`GOLDEN_PARAMETERS` specifies an output limit directly. Run budgets are
cumulative across resumes; increase the configured limit to continue. Requests
reserve UTF-8 input bytes plus overhead and the output limit before sending.
Provider usage replaces reservations when returned. Missing usage or failed
requests retain conservative estimates. Custom tokenizers/provider billing can
exceed estimates, so these are operational limits, not a provider billing guarantee.

`GOLDEN_MAX_COST` requires prices for both models, in currency units per million tokens:

```dotenv
GOLDEN_GENERATOR_INPUT_PER_MILLION=1.0
GOLDEN_GENERATOR_OUTPUT_PER_MILLION=4.0
GOLDEN_REVIEWER_INPUT_PER_MILLION=1.0
GOLDEN_REVIEWER_OUTPUT_PER_MILLION=4.0
GOLDEN_MAX_COST=20
```

These are illustrative numbers, not provider prices. Without configured pricing,
reported cost is zero/unknown; token usage is still tracked. Transient HTTP/network
errors retry with bounded backoff; persistent failures pause with an incomplete
status. Provider error bodies and authentication headers are never persisted.
Exit codes: `0` command completed, `1` configuration/snapshot error, `2` run paused.
Always inspect the report's `ready` field; successful browsing does not imply release.

API request shape reference: [official Chat Completions documentation](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create).

## Human review and release

Open the printed `http://127.0.0.1:8765` URL. The local interface shows progress,
coverage gaps, question filters, reference answers, required facts, highlighted
evidence, automated validation, and review history. Approve, reject, or edit with
notes. Edits invalidate approval; run `resume` to revalidate, then approve again.
Serving raw documentation HTML or arbitrary local files is deliberately avoided.
Writes require a session token and loopback Host/Origin checks.

After generation, select an audit sample in the interface or CLI:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset audit
```

The sample is 5% of accepted records, rounded up, with a minimum of 100 or the
whole population if smaller. Allocation is proportional across product, question
type, and difficulty using largest remainders; tiny strata may receive zero
records when the sample is smaller than the number of strata. Selection and
ordering are reproducible from snapshot identity and audit round.

At least 95% of the sample must pass its initial human review. Every identified
defect must be repaired/revalidated/reapproved or rejected from export. A rejection
or edit requires reviewing every remaining record in its product/question-type
category. New accepted records after sample selection require human review too.
Rejecting the sole question for a section reopens its question-coverage gap.

Samples cannot be reset to conceal failures. If an audit fails its 95% threshold,
first resolve **all** required sample/category reviews and defects, then use
`audit --new-round` for a fresh audit. Previous rounds and decisions remain in the
database and export. The new sample requires fresh decisions.

Browsing completes only at 100% page/reading-unit coverage with no unresolved
inventory/API failures. Readiness additionally requires accepted questions for
100% of substantive sections, 100% evidence integrity, and the passing audit gate.
Both statuses are explicit and independently reported.

## Export and evaluation labels

```powershell
venv/Scripts/python.exe -m tools.golden_dataset export --output _build/dataset-v1-candidates
venv/Scripts/python.exe -m tools.golden_dataset export --output _build/dataset-v1 --ready-only
```

Candidate export is allowed before audit and labeled `candidate_incomplete`.
`--ready-only` fails unless all release gates pass. Each destination must be empty.
Exports contain:

| File | Contents |
| --- | --- |
| `questions.jsonl` | Automatically accepted, human-not-rejected question records |
| `candidates.jsonl` | All candidate versions, including rejections and review history |
| `sections.jsonl` | Source section IDs, raw source text and locations, headings, exclusions, separate browsing metadata |
| `journey.jsonl` | Tool actions and concise decisions |
| `coverage.json` | Quality metrics, gaps, distribution, usage, audit gate |
| `manifest.json` | Schema/snapshot versions, source and artifact hashes, page inventory, API usage, audit rounds |

A question has `id`, `snapshot_id`, `question`, `reference_answer`,
`required_facts`, `persona`, `question_type`, `difficulty`, `product`,
`target_section`, `evidence_groups`, `related_links`, `provenance`, `validation`,
`status`, `human_status`, and `review_history`. IDs are stable across resume;
generation attempts have distinct candidate IDs. Human edits keep the record ID
and append history. Prompt version is `golden-v2`; export schema version is `2.0`.
Schema 1.0 HTML-only checkpoints must be regenerated into a new database;
changing a filename extension cannot recover accurate source provenance.

The agent still browses rendered pages and follows their links. Evaluation labels
identify actual `.rst` or `.md` source files and source section locations, not
HTML artifacts. `section_id` has the form `repository/path.rst::L6`, identifying
the section's heading line within the frozen snapshot; `heading_path` provides
the human-readable hierarchy. The mapping comes from Sphinx's parsed source
metadata, not a filename guess or a heading slug generated from HTML.

Example evidence group:

```json
{
  "fact_indices": [0],
  "alternatives": [{
    "section_id": "docs/products/kafka/howto/configure-log-cleaner.rst::L6",
    "document_path": "docs/products/kafka/howto/configure-log-cleaner.rst",
    "heading_path": ["Configure log cleaner for topic compaction", "Enable log compaction for all topics"],
    "source_spans": [{
      "source_path": "docs/products/kafka/howto/configure-log-cleaner.rst",
      "line_start": 13,
      "line_end": 13,
      "excerpt": "Set the value to ``compact``.",
      "start": 927,
      "end": 956
    }],
    "browsing": {
      "page": "docs/products/kafka/howto/configure-log-cleaner.html",
      "anchor": "enable-log-compaction-for-all-topics",
      "excerpt": "Set the value to compact.",
      "start": 412,
      "end": 437
    }
  }]
}
```

Offsets above are illustrative. Source spans retain original markup and exact
1-based inclusive line ranges and 0-based end-exclusive character offsets.
They map the supporting semantic blocks, which can be larger than the selected
rendered excerpt. Each span's `precision` states whether it resolves an exact
block, a section, or a whole included file; `mapping_method` distinguishes exact
display matches from transformed-block alignment (for example resolved link
titles or math). Broader fallbacks never pretend to be exact sentence locations.
The independent reviewer checks support in the raw source excerpts too.
`document_path` identifies the browsed documentation's source;
`source_spans[].source_path` identifies the file actually holding the supporting
text. Included snippets point to their include files, and literal includes point
to their actual code files. Multiple spans jointly support one alternative.
Rendered URLs and offsets live under `browsing` for traceability; a RAG evaluator
does not need to interpret HTML. Unmappable rendered evidence fails validation.

Every required fact index must belong to an
evidence group. All groups are necessary; alternatives within one group are
interchangeable. `related_links` are not relevance judgments. Section identity is
independent of retrieval chunks. Question coverage credits the explicitly
generated/reviewed target section, not incidental alternative sources.

Dataset metrics are ratios with numerator, denominator, and rate (empty
denominators are 1.0; readiness still requires accepted records):

| Metric | Definition |
| --- | --- |
| Page coverage | Pages with all reading units read / inventoried pages |
| Section reading coverage | Fully read sections / all inventoried sections |
| Question coverage | Substantive target sections with accepted questions / substantive sections |
| Evidence integrity | Accepted records passing deterministic checks / accepted records |
| Automated acceptance | Currently automatically accepted candidates / generated candidates |
| Duplicate rate | Candidates rejected as duplicate questions / generated candidates |
| Human audit pass rate | Sample records passing initial review / sampled records |
| Distribution | Accepted counts by product, type, persona, difficulty |

The following evaluation contract is **documented for a future evaluator**, not
implemented here. Normalize retrieved paths to repository-relative source files.
Map chunks using source line/character spans and heading hierarchy, independently
of HTML anchors. An alternative is fully retrieved when its required source spans
are covered; an evidence group is retrieved when one alternative is retrieved.

| RAG metric | Definition |
| --- | --- |
| Source Hit@k | Fraction of questions with any supporting source file in top k results |
| Source evidence Recall@k | Required evidence groups with all files of one alternative retrieved / required groups |
| Source MRR | Mean reciprocal rank of first supporting source file (zero when absent) |
| Section Hit@k | Fraction of questions with a supporting source section/span in top k |
| Section evidence Recall@k | Required groups with source-span coverage for one alternative / required groups |
| Answer correctness | Supported required facts correctly expressed / required facts |
| Answer faithfulness | Supported factual claims / all generated factual claims |
| Citation precision | Citations supporting their associated claim / citations supplied |
| Citation completeness | Claims with supporting citations / claims requiring support |

Recall, required-fact correctness, and citation completeness expose partial
answers that an any-source hit alone would miss. Answer and claim judgments need
a future semantic judge or human rubric; exact text matching is insufficient.
There are no unanswerable questions or abstention labels in this version.

## Verification

```powershell
venv/Scripts/python.exe -m unittest discover -s tools/golden_dataset/tests -v
```

Tests use a real local HTTP mock provider and temporary SQLite databases. They
cover extraction, ownership, long reading units, missing pages, snapshot changes,
evidence offsets and alternatives, necessary cross-page groups, duplicate
handling, malformed generation, budgets, interrupted independent review, resume,
stratified sampling, edit invalidation, local review routes, and release/export
gates. Mock generation verifies the pipeline, not the semantic quality of a real
model. Run a configured provider on the small Kafka subset before paying for a
complete corpus run.
