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

The agent uses the **same documentation server** described above. Keep
`make livehtml` running at [http://localhost:8000](http://localhost:8000).

One-time agent setup, using the documentation virtual environment:

```powershell
venv/Scripts/python.exe -m pip install -r tools/golden_dataset/requirements.txt
venv/Scripts/python.exe -m playwright install chromium
if (-not (Test-Path .env)) { Copy-Item tools/golden_dataset/.env.example .env }
```

Edit `.env` with your model API URL, API key, generator/reviewer model names,
and `GOLDEN_MAX_SECTIONS=3` for a small manual test.

In another terminal at the repository root:

```powershell
venv/Scripts/python.exe -m tools.golden_dataset generate
```

The first run registers the existing `_build` automatically. Chromium opens and
browses the app; no additional documentation server or manual inventory is needed.
Use `resume` for the next batch, or `review` to inspect questions at
[http://127.0.0.1:8765](http://127.0.0.1:8765).

See the [short quickstart](tools/golden_dataset/README.md) for configuration,
continuing a run, and exporting. The [reference](tools/golden_dataset/REFERENCE.md)
covers evidence formats, coverage, auditing, and tests.
