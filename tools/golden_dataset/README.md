# Browser-driven documentation golden dataset

The agent controls a real Chromium browser, reads accessibility observations,
clicks navigation and links, types into the app's search, and explores documentation
as a user. Question generation receives browser observations only. A separate
post-draft verifier reads repository sources to attach precise `.rst`, `.md`,
include, and code-file citations. This tool does not run RAG experiments.

## Setup and existing local app

Use Python 3.10+ in the documentation project's environment:

```powershell
venv/Scripts/python.exe -m pip install -r tools/golden_dataset/requirements.txt
venv/Scripts/python.exe -m playwright install chromium
```

Copy `.env.example` to the repository-root `.env` if there is no existing `.env`.
Otherwise add its `GOLDEN_` settings to the existing file. Set the compatible API
base URL, generator and reviewer model names, and optional API key. Neither native
tool calling nor a vision model is required. `GOLDEN_PARAMETERS` accepts optional
provider-specific Chat Completions parameters as a JSON object.

Configure the already running documentation app:

```dotenv
GOLDEN_BASE_URL=http://localhost:8001/v1
GOLDEN_API_KEY=
GOLDEN_GENERATOR_MODEL=your-generator-model
GOLDEN_REVIEWER_MODEL=your-reviewer-model
GOLDEN_DOCS_URL=http://localhost:8000
GOLDEN_HEADLESS=false
GOLDEN_BROWSER_TIMEOUT_SECONDS=30
GOLDEN_BROWSER_MAX_ACTIONS=200
GOLDEN_MAX_SECTIONS=3
```

The browser is visible by default. Set `GOLDEN_HEADLESS=true` for background runs.
The runner connects to the app; it does not start or replace your server. Serve
the exact registered Sphinx build directory. For example, after `make html`, a
static preview can be started separately with:

```powershell
venv/Scripts/python.exe -m http.server 8000 --bind 127.0.0.1 --directory _build/html
```

Register the directory your app actually serves, including its `.doctrees`:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset inventory --use-existing-build --build-dir _build/html
venv/Scripts/python.exe -m tools.golden_dataset generate
venv/Scripts/python.exe -m tools.golden_dataset status
venv/Scripts/python.exe -m tools.golden_dataset resume
venv/Scripts/python.exe -m tools.golden_dataset review
```

`inventory` without `--use-existing-build` builds a fresh snapshot into the empty
directory given by `--build-dir` (default `_build/golden-snapshot`). Your app must
serve that build before generation. Served page bytes must match registered HTML;
servers that inject scripts or rewrite responses need an unmodified preview.

The default checkpoint database is `_build/golden-dataset/run.sqlite3`. For a new
run, use global `--db PATH` **before** each subcommand. Schema 1.0 and 2.0 databases
are preserved but cannot resume as browser runs; create a new inventory/database.
Source files and rendered content are hashed; changes invalidate the snapshot.
Only select trusted local builds: Sphinx doctrees are Python pickle files.

Environment variables override `.env`. `--env-file PATH` selects another file for
`generate`/`resume`. Quoting/comments are supported, interpolation is disabled, and
loading does not mutate process environment. API keys stay in memory, outside
configuration hashes, checkpoints, and exports. `.env` and `.env.*` are ignored
by Git except `.env.example`.

## Actual browser behavior and coverage

The generator's actions are `observe`, `click`, `type`, `keypress`, `scroll`,
`back`, `navigate`, `draft_question`, and `finish`. Interaction uses current
accessibility element references. There is no arbitrary JavaScript action,
filesystem search, direct section reader, or source-file view available to it.
Stale references refresh the observation so the model can replan.

`observe(scope="page", cursor=N)` reads chunks of the accessibility representation
including navigation. `observe(scope="main", cursor=N)` reads documentation
content derived from the browser's accessible main region. Large observations
have tracked chunks. A chunk counts as read only after a model response consumes
it. Observations from the same content are reused across resume; browser handles
and element references are always refreshed.

The coverage audit privately inspects navigation/link destinations and source
metadata, not article prose for generation. Its reachable pages are mandatory.
Unlinked pages are reported separately; opening one through the app promotes it
into full-run coverage. A scoped inventory (`--page relative/path.html`, repeatable)
retains its selected denominator. Audit URLs are never used to bypass browser
discovery. Address-bar navigation is limited to the app root and URLs actually
revealed in delivered browser observations.

Generated table-of-contents links count as real navigation. The current repository
has 789 documentation pages: 785 reachable and 4 navigation/link orphans. An older
18-page audit incorrectly omitted these generated links. The audit's `orphans`
field is independent of a run's selected-scope `excluded` list.

The agent uses the app's search UI. Failed search requests are recorded and it
continues through navigation; there is no hidden source-search fallback. Search
snippets are not evidence: it must open the documentation result. External pages
and raw source views are outside browsing scope. Included source dependencies
remain available to verification regardless of navigation reachability.

Pages require all expected documentation sections and observed content chunks to
be read, with discovered tabs/accordions visited and their content consumed.
Hidden panel text cannot support a question before the browser reveals it.
Unvisited routes, unread content states, or pending failures keep coverage
incomplete. The model's `finish` request cannot override these checks.

`GOLDEN_MAX_SECTIONS` limits completed target-section work per invocation: an
accepted question or three exhausted attempts finishes that work item. Blank is
unlimited. `--max-sections N` overrides it. A journey can incidentally read linked
evidence. `GOLDEN_BROWSER_MAX_ACTIONS` is an additional per-invocation action cap;
the old `GOLDEN_AGENT_STEPS` setting is obsolete and ignored. Caps checkpoint and
pause instead of forcing a premature draft. `resume` continues the saved run.

## Evidence, validation, and source precision

Each draft cites `observation_id` plus an exact excerpt from a consumed browser
chunk. Optional `start` is a zero-based character offset in that observation's
text, and `end` is end-exclusive. Unknown, unread, off-scope, or invented evidence
is rejected. Every required fact must belong to a necessary evidence group;
alternatives within a group must independently support its indexed facts.

The draft is persisted **before** the source verifier runs. Verification maps its
browser evidence to Sphinx source blocks, checking exact source text and snapshot
hashes. Unmappable evidence fails; only a concise failure category goes back to
the generator. The generator never receives verifier snippets or source mappings.
The independent reviewer receives both browser evidence and verified raw source
spans to assess support, completeness, ambiguity, realism, and evidence necessity.
There are three draft attempts per section; `resume --retry-gaps` grants three
more. Rejections remain inspectable. Pending independent reviews resume without
regenerating the question.

Schema **3.0** retains source-based evaluation identities:

```json
{
  "fact_indices": [0],
  "alternatives": [{
    "section_id": "docs/products/kafka/howto/configure-log-cleaner.rst::L6",
    "document_path": "docs/products/kafka/howto/configure-log-cleaner.rst",
    "heading_path": ["Configure log cleaner for topic compaction", "Enable log compaction for all topics"],
    "observation_id": "saved-browser-observation-id",
    "source_spans": [{
      "source_path": "docs/products/kafka/howto/configure-log-cleaner.rst",
      "line_start": 13,
      "line_end": 13,
      "excerpt": "Set the value to ``compact``.",
      "start": 927,
      "end": 956,
      "precision": "exact_block",
      "mapping_method": "exact_display"
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

Offsets above are illustrative. Source offsets are Unicode character indices in
UTF-8 decoded files with LF newlines; source lines are 1-based and inclusive.
`document_path` identifies the browsed page's source; `source_spans` identify the
actual supporting files, including includes and literal code includes. Multiple
spans jointly support an alternative. Precision explicitly distinguishes an exact
block, section, or file fallback. Browser URLs remain provenance, not RAG source
identities. Unverified sections in incomplete exports have an explicit
`::unverified:` identifier, never a fabricated source line.

## Audit, limits, and export

The localhost review interface shows browser text with highlighted evidence,
source excerpts and locations, validation results, filters, and review history.
Approving requires automatic acceptance. Edits invalidate it: run `resume` to
revalidate and approve again. Writes use loopback Host/Origin checks and a session
token. Review reads are available during generation; writes require it to pause.

Select an audit through the interface or `audit`. The reproducible stratified
sample is 5%, rounded up, with a minimum of 100 or the entire accepted population
if smaller. Strata are product/type/difficulty with proportional largest-remainder
allocation. At least 95% must pass initial human review. Every defect must be
fixed/revalidated/approved or removed; a failure expands review to its remaining
product/type category. New accepted records after selection require human review.
An audit round cannot be reset to hide failures. After all required defects and
reviews are resolved, `audit --new-round` retains history and requires a new audit.

Token/cost limits remain cumulative across resumes. `GOLDEN_MAX_TOKENS` and
`GOLDEN_MAX_COST` are optional; `GOLDEN_OUTPUT_TOKENS` is the per-response limit.
Cost limits require generator/reviewer input/output prices per million tokens
from `.env.example`. Request reservations use conservative input-byte/output
estimates; provider usage replaces them when available. Failed/uncertain calls
retain estimates. These are operational limits, not a billing guarantee.
Transient HTTP failures retry; persistent failures checkpoint and pause.

Exit codes: `0` command completed, `1` invalid configuration/snapshot, `2` run paused.
Completion is distinct from readiness: readiness also requires complete question
coverage, evidence integrity, and the passing human audit gate.

```powershell
venv/Scripts/python.exe -m tools.golden_dataset export --output _build/browser-dataset-candidates
venv/Scripts/python.exe -m tools.golden_dataset export --output _build/browser-dataset-v1 --ready-only
```

Exports require empty destinations. They contain questions, all candidates,
source sections, browser observations, accessibility snapshots, action journeys,
coverage metrics, and a manifest with source/build hashes and audit history.
Unaudited/incomplete exports are labeled `candidate_incomplete`.

Dataset metrics include page/section/chunk reading coverage, question coverage,
evidence integrity, automated acceptance, duplicates, audit pass rate, and product,
type, persona, and difficulty distributions. Excluded pages, promoted pages,
unresolved controls, and search failures are explicit.

Future RAG metrics (not implemented here): source Hit@k/MRR and evidence-group
Recall@k; source-section/span Hit@k and Recall@k; required-fact correctness; claim
faithfulness; citation precision and completeness. Match repository-relative
paths and source spans, independent of retrieval chunks or HTML anchors.

## Verification

```powershell
venv/Scripts/python.exe -m unittest discover -s tools/golden_dataset/tests -v
```

Tests run actual headless Chromium against local fixtures and a mock compatible
API. Missing Playwright/browser dependencies fail explicitly; there is no silent
fallback to parsed HTML or skipped browser tests. Tests establish pipeline
behavior, while a real-provider run is needed to assess model quality. All browser
and test-server processes owned by the tool are closed at completion.
