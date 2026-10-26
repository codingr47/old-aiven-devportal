# 🔒 Former Aiven Documentation

> [!IMPORTANT]  
> This repository used to be the main documentation of Aiven.
>
> The content has been migrated to https://github.com/aiven/aiven-docs.

## Building the docs

1. We recommend using a [virtual environment](https://docs.python.org/3/library/venv.html):

   ```bash
   python3 -m venv venv
   ```

1. Activate your virtual environment:

   ```bash
   source venv/bin/activate
   ```

1. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

1. Build a local preview:

   ```bash
   make livehtml
   ```

See the output on [http://localhost:8000](http://localhost:8000).

## Golden dataset generator

Generate evidence-backed documentation questions using a real Chromium browser, track browsing coverage,
and audit candidates in a local review interface.

For a small manual test on **Windows PowerShell**, run these commands from this
repository's root. Use Python 3.11. The [full runbook](tools/golden_dataset/README.md#run-it-step-by-step-windows-powershell)
includes expected output, troubleshooting, auditing, and full-corpus runs.

1. Install the documentation and agent dependencies, then Chromium:

   ```powershell
   if (-not (Test-Path venv/Scripts/python.exe)) { py -3.11 -m venv venv }
   venv/Scripts/python.exe -m pip install -r requirements.txt -r tools/golden_dataset/requirements.txt
   venv/Scripts/python.exe -m playwright install chromium
   ```

2. Create `.env` if needed, then open it in your editor:

   ```powershell
   if (-not (Test-Path .env)) { Copy-Item tools/golden_dataset/.env.example .env }
   ```

   Set `GOLDEN_BASE_URL`, `GOLDEN_API_KEY`, `GOLDEN_GENERATOR_MODEL`, and
   `GOLDEN_REVIEWER_MODEL` to your actual model-provider settings. Set
   `GOLDEN_DOCS_URL=http://127.0.0.1:8000` and `GOLDEN_MAX_SECTIONS=3` for this test.
   The model API is a separate service; the documentation server below does not
   provide it.

3. Build the site and create a checkpoint scoped to two Kafka pages:

   ```powershell
   venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 inventory --build-dir _build/manual-test/site --page docs/products/kafka/howto/configure-log-cleaner.html --page docs/products/kafka/concepts/log-compaction.html
   ```

   Wait for `build succeeded` and an empty `inventory_errors` list. Run inventory
   only once for this database; use a new run folder if these paths already exist.

4. In a second terminal, also at the repository root, start the documentation server:

   ```powershell
   venv/Scripts/python.exe -m http.server 8000 --bind 127.0.0.1 --directory _build/manual-test/site
   ```

   Open [http://127.0.0.1:8000](http://127.0.0.1:8000) and leave this terminal running.

5. Back in the first terminal, launch the browser agent:

   ```powershell
   venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 generate
   ```

   Watch Chromium. The section/action cap will save progress and pause the run;
   exit code `2` is expected. For later batches, use the same checkpoint:

   ```powershell
   venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 resume
   ```

6. In a third terminal, start review and open [http://127.0.0.1:8765](http://127.0.0.1:8765):

   ```powershell
   venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 review --port 8765
   ```

   Pause generation before saving review decisions. To export the test's current
   candidates, run this in the first terminal after generation has stopped:

   ```powershell
   venv/Scripts/python.exe -m tools.golden_dataset --db _build/manual-test/run.sqlite3 export --output _build/manual-test/export-candidates-v1
   ```

For an already running app, follow [registering its served build](tools/golden_dataset/README.md#using-a-build-you-already-serve).
For a full dataset, follow [the full-run and audit steps](tools/golden_dataset/README.md#8-run-the-full-corpus-and-produce-an-audited-export).
