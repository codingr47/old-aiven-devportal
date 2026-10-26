# Golden dataset reference

For running the tool, start with the [quickstart](README.md). This page describes
browser behavior, evidence formats, auditing, and evaluation metrics.

## Optional run scope and inventory commands

The normal `generate` command registers the existing `_build` automatically.
For a new, narrowly scoped run, use:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/kafka-test.sqlite3 generate --page docs/products/kafka/howto/configure-log-cleaner.html --page docs/products/kafka/concepts/log-compaction.html
```

The original `make livehtml` server keeps running throughout. Scope is fixed when
the checkpoint is created; use the same `--db` for later `resume`/`review` commands.
The explicit `inventory` command remains available for advanced snapshot setup,
but is not needed for normal startup. `generate --build-dir PATH` registers a
different existing build directory on the first run only.

## Actual browser behavior and coverage

The agent browses like a reader. Its actions are `click`, `back`, `navigate`,
`type`, `keypress` (Enter, Tab, Escape), `read_more`, `next_page`,
`draft_questions`, and `finish`. Interaction uses current accessibility element
references. There is no arbitrary JavaScript action, filesystem search, direct
section reader, or source-file view available to it. Scrolling is not an action:
the accessibility snapshot always covers the whole page. Stale references and
blocked clicks return a fresh view so the model can replan.

Every action returns one view of the current page:

- `content`: the whole article, section by section, with `observation_id`s to
  quote. Articles over about 12,000 characters are split into stable
  `content_parts`; `unread_parts` lists those not yet read (`read_more(part=N)`).
  Sections read earlier in the session are stubbed as `already_read` while their
  text is still in the generator's memory.
- `nav`: the visible sidebar menu with depth, `visited` flags, and expanders for
  collapsed groups. Expanding a group returns the menu with its new links.
- `content_links`, `page_controls` (tabs and disclosures), and `site` (search box).
- `progress` and `questions_on_page`, so the model knows what is left.

Content counts as read once a model response has seen it. A section's progress
follows its latest rendering, so switching a tab replaces rather than adds work.
`next_page` follows the first unvisited in-scope article link, then menu link, then
opens a collapsed menu group, and only then navigates to a discovered frontier URL.
Memory carries roughly the last 16,000 characters read, which makes cross-page
questions possible.

Production pretty URLs (`docs/products/kafka`) are redirected to the built
`.html` page, because the local preview only serves `.html` files. A link that
still returns an HTTP error is recorded under `broken_links`, the browser goes
back, and the model receives the error. Off-origin requests are blocked except
rendering libraries (Mermaid from unpkg.com): the OneTrust consent banner would
cover every page, and analytics would count the crawler as visits.

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

A page is read when it was visited, every rendered section's current text was
delivered, and discovered tabs/disclosures were opened and their content consumed.
Sections whose headings never render are listed as `unobserved_sections` instead
of blocking the crawl. Hidden panel text cannot support a question before the
browser reveals it. The model's `finish` request cannot override these checks.

Questions are drafted where a reader would ask them: 0-3 per page, none for
link-only hub pages. `question_coverage` is the share of substantive pages with an
accepted question. `gaps` lists the pages without one; gaps are reported but do
not block completion or release.

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
There are three draft attempts per section. `resume --retry-gaps` reopens pages
without an accepted question (marking them unvisited and unread, with fresh
attempts) so the crawl returns to them. Rejections remain inspectable. A
malformed reviewer reply is retried once; the draft then stays pending and is
revalidated on resume without regenerating the question. Truncated generator
replies come back as `output_truncated` feedback asking for fewer, shorter
questions. Three malformed replies in a row pause the run. Reasoning models need
`GOLDEN_OUTPUT_TOKENS` well above 4096 to draft question batches.

Schema **3.1** retains source-based evaluation identities:

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
