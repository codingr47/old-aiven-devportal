import argparse
import json
import sys
from pathlib import Path

from .engine import run
from .model import load_config
from .reporting import export, report, select_audit
from .review import serve
from .snapshot import inventory, verify_snapshot
from .storage import Store
from .tracing import trace, traced


def main(argv=None):
    parser = argparse.ArgumentParser(description="Browser-driven documentation golden dataset generator")
    parser.add_argument("--db", default="_build/golden-dataset/run.sqlite3", help="Checkpoint database")
    subs = parser.add_subparsers(dest="command", required=True)
    inv = subs.add_parser("inventory", help="Build/register a Sphinx snapshot and audit navigation/source metadata")
    inv.add_argument("--repo", default=".")
    inv.add_argument("--build-dir", default="_build/golden-snapshot")
    inv.add_argument("--use-existing-build", action="store_true", help="Explicitly use an existing build (not verified fresh)")
    inv.add_argument("--page", action="append", help="Exact relative HTML page; repeat for a scoped smoke run")
    for name in ("generate", "resume"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--env-file", default=".env", help="Environment file (default: .env); environment variables override file values")
        cmd.add_argument("--max-sections", type=int, help="Target sections per invocation; overrides GOLDEN_MAX_SECTIONS")
        cmd.add_argument("--retry-gaps", action="store_true", help="Explicitly retry exhausted sections for three more attempts")
        if name == 'generate':
            cmd.add_argument('--build-dir', help='Existing documentation build (default: _build from make livehtml)')
            cmd.add_argument('--page', action='append', help='Limit a new run to selected relative HTML pages')
    subs.add_parser("status")
    audit = subs.add_parser("audit", help="Freeze the reproducible audit sample")
    audit.add_argument("--new-round", action="store_true", help="Re-audit after every previous required review and defect is resolved")
    review = subs.add_parser("review")
    review.add_argument("--port", type=int, default=8765)
    exp = subs.add_parser("export")
    exp.add_argument("--output", required=True)
    exp.add_argument("--ready-only", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {'generate', 'resume', 'inventory'}:
        trace('startup', f'{args.command}; database={Path(args.db).resolve()}')
    try:
        store = Store(args.db)
        if args.command == "inventory":
            with store.writer():
                # Fail before an expensive build when the database is already initialized.
                with store.connect() as db:
                    if db.execute("SELECT 1 FROM checkpoint").fetchone():
                        raise ValueError("Database already has a snapshot; use a new --db")
                with traced('inventory', f'Auditing documentation build: {Path(args.build_dir).resolve()}'):
                    state = inventory(args.repo, args.build_dir, args.page, not args.use_existing_build)
                store.initialize(state)
            output = report(state)
        elif args.command in {"generate", "resume"}:
            if args.max_sections is not None and args.max_sections <= 0:
                raise ValueError("--max-sections must be positive")
            config = load_config(args.env_file)
            trace('config', f'Loaded {args.env_file}; headless={config["headless"]}')
            if args.command == 'generate':
                with store.writer():
                    with store.connect() as db:
                        initialized = db.execute('SELECT 1 FROM checkpoint').fetchone() is not None
                    if not initialized:
                        build_dir = args.build_dir or '_build'
                        if not (Path(build_dir) / 'index.html').is_file():
                            raise ValueError('Documentation build not ready. Start make livehtml, wait for the server, then run generate again.')
                        with traced('inventory', f'Registering existing documentation build: {Path(build_dir).resolve()}'):
                            state = inventory('.', build_dir, args.page, build=False)
                            verify_snapshot(state)
                        store.initialize(state)
                    elif args.page:
                        raise ValueError('This run already has a scope; use a new --db to select pages.')
                    elif args.build_dir and Path(args.build_dir).resolve() != Path(store.load()['build_dir']):
                        raise ValueError('This run already has a build directory; use a new --db to change it.')
            output = run(store, config, args.max_sections, args.retry_gaps)
        elif args.command == "status":
            state = store.load()
            if state.get('schema_version') != '3.0':
                raise ValueError('Legacy checkpoint: create a new browser inventory for schema 3.0.')
            output = report(state)
            try:
                verify_snapshot(state)
                output["snapshot_valid"] = True
            except ValueError as exc:
                output.update(snapshot_valid=False, ready=False, browsing_complete=False, snapshot_error=str(exc))
        elif args.command == "review":
            verify_snapshot(store.load())
            serve(store, args.port)
            return 0
        elif args.command == "audit":
            with store.writer():
                with store.transaction() as state:
                    verify_snapshot(state)
                    output = select_audit(state, args.new_round)
        else:
            with store.writer():
                output = export(store.load(), args.output, args.ready_only)
        print(json.dumps(output, indent=2, ensure_ascii=False))
        return 2 if output.get("runtime", {}).get("status") == "paused" else 0
    except (ValueError, OSError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
