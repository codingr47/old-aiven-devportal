# Browser-driven documentation golden dataset

The agent controls a real Chromium browser, reads accessibility observations,
clicks navigation and links, types into the app's search, and explores documentation
as a user. Question generation receives browser observations only. A separate
post-draft verifier reads repository sources to attach precise `.rst`, `.md`,
include, and code-file citations. This tool does not run RAG experiments.

## Run it step by step: Windows PowerShell

These steps start with a small Kafka test, then explain how to run the full
corpus. **Run every command from the repository root**, the folder containing
`conf.py`, `requirements.txt`, and `tools/`. Python 3.11 is recommended.

You will use three terminals:

| Terminal | Purpose | Keep it running? |
| --- | --- | --- |
| 1 | Serves the documentation at `http://127.0.0.1:8000` | Yes, while generating/resuming |
| 2 | Installs, inventories, generates, checks status, and exports | Commands return when finished or paused |
| 3 | Serves the review interface at `http://127.0.0.1:8765` | Yes, while reviewing |

The model API is a separate service supplied by your provider or local model
server. The tool starts Chromium, but **does not start the model API or the
documentation server**.

### 1. Install dependencies — Terminal 2

If the `venv` folder already exists, keep it. Otherwise create it with Python 3.11:

```powershell
if (-not (Test-Path venv/Scripts/python.exe)) { py -3.11 -m venv venv }
venv/Scripts/python.exe -m pip install -r requirements.txt -r tools/golden_dataset/requirements.txt
venv/Scripts/python.exe -m playwright install chromium
```

No environment activation is necessary: all commands explicitly use the virtual
environment's Python. Wait for the Chromium installation to finish before continuing.

### 2. Configure your model and manual-test limits — Terminal 2

Create `.env` only if you do not already have one:

```powershell
if (-not (Test-Path .env)) { Copy-Item tools/golden_dataset/.env.example .env }
```

Open `.env` in your editor. Set the following values, replacing the API URL,
model names, and key with your actual provider settings:

```dotenv
GOLDEN_BASE_URL=http://localhost:8001/v1
GOLDEN_API_KEY=replace-with-your-provider-key
GOLDEN_GENERATOR_MODEL=replace-with-your-generator-model
GOLDEN_REVIEWER_MODEL=replace-with-your-reviewer-model
GOLDEN_DOCS_URL=http://127.0.0.1:8000
GOLDEN_HEADLESS=false
GOLDEN_BROWSER_TIMEOUT_SECONDS=30
GOLDEN_BROWSER_MAX_ACTIONS=200
GOLDEN_MAX_SECTIONS=3
```

`GOLDEN_BASE_URL` is the **model API prefix**, such as your provider's `/v1` URL;
the tool appends `/chat/completions`. It is different from `GOLDEN_DOCS_URL`, which
is the documentation website Chromium will browse. The `8001` URL above is only
an example; use it only if you actually run a compatible model server there.
Leave `GOLDEN_API_KEY` blank only for an endpoint that does not require a key.
Both model names must be available on that endpoint. A text-only model is sufficient.

For this test, `GOLDEN_MAX_SECTIONS=3` limits each invocation to three completed
section work items. `GOLDEN_HEADLESS=false` lets you watch the browser.

### 3. Build and inventory a small test run — Terminal 2

This creates a fresh database and documentation build. Copy the entire command:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 inventory --build-dir _build/manual-test/site --page docs/products/kafka/howto/configure-log-cleaner.html --page docs/products/kafka/concepts/log-compaction.html
```

Wait for Sphinx's `build succeeded` message and the inventory JSON. Check that
`inventory_errors` is empty. No model API calls happen during this step.

Sphinx builds the whole site so its links work, while this run's required coverage
is limited to the two selected Kafka pages. The database stores progress; the
`site` directory contains what the browser must see, plus source-mapping metadata.

Run inventory **once per new run**. If these paths already contain a previous
run, continue that run with `resume`, or choose a new folder name consistently in
the commands below. Inventory will not overwrite an existing checkpoint or build.

### 4. Start the documentation website — Terminal 1

Open another PowerShell terminal in the repository root:

```powershell
venv/Scripts/python.exe -m http.server 8000 --bind 127.0.0.1 --directory _build/manual-test/site
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser and confirm the
Aiven documentation appears. **Leave Terminal 1 running.** If port 8000 is already
in use, either use that server with its exact registered build, or choose another
port here and update `GOLDEN_DOCS_URL` to match.

The served HTML must match the registered build byte for byte. For this workflow,
serve `_build/manual-test/site` exactly as shown. A live-reload server that injects
scripts into responses will fail the snapshot check.

### 5. Start the agent, check progress, and resume — Terminal 2

With the documentation server and model API available, run:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 generate
```

A Chromium window opens. The agent navigates the actual app and drafts questions;
source verification and independent review follow each draft. The terminal can be
quiet during browsing: it prints the result JSON when the invocation ends.

After the section or action limit is reached, expect `runtime.status: "paused"`
and exit code `2`. This is a saved checkpoint, not a completed dataset. A section
work item ends after an accepted question or three rejected attempts, so the
accepted-question count can be smaller than the section limit.

Check progress, then run the next batch:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 status
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 resume
```

Repeat `resume` as needed. **Use the same `--db` every time; do not rerun inventory.**
You can interrupt generation with Ctrl+C and resume later. Keep the documentation
server running and keep the repository/build unchanged between batches.

Useful status fields: `accepted`, `gaps`, `page_coverage`, `question_coverage`,
`browsing_complete`, `runtime.error`, and `ready`. Browsing can be complete while
`ready` remains false because human auditing has not passed.

### 6. Inspect questions and evidence — Terminal 3

Open another PowerShell terminal in the repository root:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 review --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Select a question to inspect
its reference answer, saved browser observation, raw source excerpts, and file/line
citations. Use Refresh to see new progress. Leave this terminal running while reviewing.

You can inspect while generation is running, but **pause generation before saving
review decisions, edits, or an audit sample**. The runner holds the write lock.
After editing a question, run `resume` to revalidate it before approving it.

### 7. Export your manual test — Terminal 2

After generation has stopped or paused:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 export --output _build/manual-test/export-candidates-v1
```

Inspect `questions.jsonl`, `observations.jsonl`, `coverage.json`, and `manifest.json`
in that directory. This export is allowed before human auditing and is labeled
`candidate_incomplete`. Each export destination must be empty; choose a new version
directory for the next export.

### 8. Run the full corpus and produce an audited export

Keep Terminal 1 serving the same build; the small test built the entire site.
In `.env`, clear the section limit:

```dotenv
GOLDEN_MAX_SECTIONS=
```

Create a **separate full-run database**, using the existing build and omitting
the `--page` filters. In Terminal 2:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 inventory --use-existing-build --build-dir _build/manual-test/site
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 generate
```

The action cap still applies. When it pauses, continue with:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 resume
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 status
```

If desired, increase the positive integer `GOLDEN_BROWSER_MAX_ACTIONS` to allow
more actions per invocation. Clearing `GOLDEN_MAX_SECTIONS` does not remove the
action cap or cumulative token/cost limits.

Once browsing is complete and question coverage is 100%, select the audit sample:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 audit
```

Stop the manual-test review server in Terminal 3 with Ctrl+C, then open the review
interface for the **full-run database**:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 review --port 8765
```

Review the required audit records, fix/remove failures, and finish any expanded
category reviews. After edits, revalidate with `resume`. Check `status` again.
Only when `ready` is true, create the audited export in Terminal 2:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/full-run/run.sqlite3 export --output _build/full-run/export-v1 --ready-only
```

You can then stop the documentation and review servers with Ctrl+C. Their files
and checkpoints remain on disk.

## Using a build you already serve

If your documentation app is already running, skip rebuilding/starting another
server. Set `GOLDEN_DOCS_URL` to its URL, then register the **exact directory it
serves**, including its `.doctrees` directory. For example, if it serves `_build/html`:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/existing-app-run/run.sqlite3 inventory --use-existing-build --build-dir _build/html
venv/Scripts/python.exe -m tools.golden_dataset --db _build/existing-app-run/run.sqlite3 generate
```

Use that database path for every subsequent command. The runner will not start,
stop, or replace your existing server.

## Common problems

| Symptom | What to do |
| --- | --- |
| `Environment file not found` | Create repository-root `.env`, or pass `--env-file PATH` to `generate`/`resume`. |
| Connection refused / documentation unavailable | Start Terminal 1's server and verify its URL matches `GOLDEN_DOCS_URL`. |
| Served documentation differs from the registered build | Serve the directory registered during inventory. Check for live-reload HTML injection. |
| API `401`, `404`, or preflight failure | Check the model API URL, key, and both model names. The tool appends `/chat/completions` to `GOLDEN_BASE_URL`. Fix `.env`, then resume. |
| `paused`, exit code `2` | Read `runtime.error`; section/action caps are expected pauses. Resume, or adjust the relevant limit first. |
| Token/cost budget exhausted | Increase the cumulative budget in `.env`, then resume the same database. |
| Database already has a snapshot | Use `resume` for that run, or choose a fresh `--db` for a new inventory. |
| Legacy checkpoint / snapshot changed | Create a new run and inventory. Old runs and exports remain available on disk. |
| Review changes cannot be saved | Pause generation first; it holds the database write lock. |
| Export not ready | Inspect `status`: resolve coverage gaps, verification failures, and the human audit gate. Use an ordinary candidate export for debugging. |

Environment variables override `.env` file values. Quoting/comments are supported;
interpolation is disabled. API keys stay in memory, outside configuration hashes,
checkpoints, and exports. Actual `.env` files are ignored by Git. Only use trusted
local Sphinx builds: `.doctrees` files are Python pickles.

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
