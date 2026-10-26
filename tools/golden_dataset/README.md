# Documentation golden dataset

Use the documentation server you already run with `make livehtml`. The agent
opens Chromium, browses that app, and generates questions from browser accessibility
observations. Source files are checked afterward to attach exact citations.

Run all commands from the repository root. Use the same Python environment as the
documentation project; the commands below use its `venv` directly.

## One-time agent setup

After installing the documentation project's dependencies as described in the
[root README](../../README.md#building-the-docs), install the agent and Chromium:

```powershell
venv/Scripts/python.exe -m pip install -r tools/golden_dataset/requirements.txt
venv/Scripts/python.exe -m playwright install chromium
if (-not (Test-Path .env)) { Copy-Item tools/golden_dataset/.env.example .env }
```

Edit `.env` with your provider's actual settings:

```dotenv
GOLDEN_BASE_URL=http://localhost:8001/v1
GOLDEN_API_KEY=your-provider-key
GOLDEN_GENERATOR_MODEL=your-generator-model
GOLDEN_REVIEWER_MODEL=your-reviewer-model
GOLDEN_MAX_SECTIONS=3
```

The API URL above is an example; use your running provider's API prefix. The tool
appends `/chat/completions`. Leave the key blank only if your endpoint needs no
authentication. This is separate from the documentation server on port 8000.

## Run

**Terminal 1:** start the documentation normally and leave it running:

```bash
make livehtml
```

On Windows, use `make.bat livehtml` in the documentation environment. The equivalent
command without activating that environment is:

```powershell
venv/Scripts/python.exe -m sphinx_autobuild . _build
```

Wait until [http://localhost:8000](http://localhost:8000) shows the documentation.

**Terminal 2:** start the agent:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset generate
```

That's the normal startup. The first `generate` automatically registers the existing
`_build` and creates its checkpoint. No separate site build, static HTTP server,
or manual inventory command is needed. Chromium is visible by default.

`GOLDEN_MAX_SECTIONS=3` limits a manual test to three completed section work items
per invocation. The agent can pause with exit code `2`; its progress is saved.

The terminal prints timestamped startup, inventory, snapshot, browser, action,
validation, and API traces to stderr. Long stages print a heartbeat every 15 seconds.
Chromium opens before API checks and pending reviews, after the local inventory and
snapshot checks pass. Reading accessibility text or
reviewing a draft can leave the visible page unchanged. Repeated identical actions
trigger replanning feedback, then pause after six repetitions to avoid wasting requests.
The final JSON result goes to stdout. To save a combined trace in PowerShell:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset generate 2>&1 | Tee-Object -FilePath golden-run.log
```

## Continue, inspect, and export

Use these commands in Terminal 2 after the agent pauses:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset status
venv/Scripts/python.exe -m tools.golden_dataset resume
```

To inspect questions and source evidence, run:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset review
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Stop review with Ctrl+C when
you want the terminal back. You only need a third terminal if you want to keep
review open alongside generation; pause generation before saving review changes.

Export the current candidates to a new directory:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset export --output _build/golden-export-v1
```

This candidate export is allowed before human auditing. For an evaluation-ready
release, complete the [audit workflow](REFERENCE.md#audit-limits-and-export), then
export with `--ready-only`.

## A few settings

| Setting | Purpose |
| --- | --- |
| `GOLDEN_MAX_SECTIONS=3` | Small batches for testing; leave blank for the full queue. |
| `GOLDEN_DOCS_URL=http://localhost:8000` | Change only if your existing server uses another URL. |
| `GOLDEN_HEADLESS=false` | Set true to hide the browser window. |
| `GOLDEN_BROWSER_MAX_ACTIONS=200` | Actions per invocation before pausing; use `resume` to continue. |

The default checkpoint is `_build/golden-dataset/run.sqlite3`. Later `generate`
calls reuse it, just like `resume`. Keep source documents and the build unchanged
during a run. The server's live-reload script is supported; edits to documentation
still require a new dataset snapshot. Git commits alone do not invalidate a run
when its documentation inputs and rendered files are unchanged.

For a fresh run, choose another checkpoint with the global `--db` option:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/new-run.sqlite3 generate
```

If your existing app serves another build directory, supply it on the first run:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset --db _build/custom-run.sqlite3 generate --build-dir _build/html
```

Use that same `--db` for subsequent status, resume, review, and export commands.
Old schema 1.0/2.0 checkpoints require a fresh run. If the build is missing, start
`make livehtml` and wait for its first build to finish.

See [the reference](REFERENCE.md) for scope filters, browser behavior, evidence
format, coverage metrics, auditing, and tests.
