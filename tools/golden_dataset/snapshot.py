"""Frozen build registration and private coverage metadata; no article reader."""

import hashlib
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


from .storage import now
from .coverage import metadata, reachable

UTILITY = {"search.html", "genindex.html", "404.html"}
UNIT_CHARS = 6000


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def source_hashes(repo):
    """Include rendering inputs, but never environment files or credentials."""
    files = []
    for name in ("docs", "includes", "code", "images", "_static", "_templates"):
        folder = repo / name
        if folder.exists():
            files.extend(p for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    files.extend(p for p in repo.iterdir() if p.is_file() and
                 (p.suffix in {".rst", ".py"} or p.name in {"index.md", "_toc.yml", "requirements.txt", "robots.txt", "_redirects"}))
    return {p.relative_to(repo).as_posix(): digest(p.read_bytes()) for p in sorted(set(files))}


def commit(repo):
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else "unversioned"


def link_target(page, href):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        return None
    path = unquote(parsed.path)
    if not path:
        path = page
    elif path.startswith("/"):
        path = path.lstrip("/")
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(page), path))
    if path.startswith("../"):
        return None
    if not path.endswith(".html"):
        path = path.rstrip("/") + "/index.html" if path.endswith("/") else path + ".html"
    return {"page": path, "anchor": unquote(parsed.fragment)}


def inventory(repo, build_dir, selection=None, build=True):
    repo, build_dir = Path(repo).resolve(), Path(build_dir).resolve()
    selection = sorted(set(selection or []))
    before = source_hashes(repo)
    command = [sys.executable, "-m", "sphinx", "-b", "html", "-E", "-a", str(repo), str(build_dir)]
    if build:
        if build_dir.exists() and any(build_dir.iterdir()):
            raise ValueError("Use an empty build directory for a fresh snapshot.")
        result = subprocess.run(command, cwd=repo)
        if result.returncode:
            raise ValueError("Sphinx build failed; inventory was not created.")
    if source_hashes(repo) != before:
        raise ValueError("Documentation sources changed during the build.")
    sources = [p for p in (repo / "index.rst", repo / "index.md") if p.is_file()]
    sources += sorted(p for p in (repo / "docs").rglob("*") if p.suffix in {".rst", ".md"})
    source_pages = {}
    for path in sources:
        page = path.relative_to(repo).with_suffix(".html").as_posix()
        if page in source_pages:
            raise ValueError("Multiple source files render to the same page: " + page)
        source_pages[page] = path.relative_to(repo).as_posix()
    all_pages = list(source_pages)
    expected = list(all_pages)
    if selection:
        unknown = set(selection) - set(expected)
        if unknown:
            raise ValueError(f"Unknown selected pages: {sorted(unknown)}")
        expected = [p for p in expected if p in selection]
    pages, sections, errors = {}, {}, []
    for page in all_pages:
        try:
            info, items = metadata(repo, build_dir, page, source_pages[page])
            pages[page] = info
            sections.update({s["id"]: s for s in items})
        except (OSError, ValueError) as exc:
            errors.append({"page": page, "error": type(exc).__name__ + ": " + str(exc)})
    rendered = {p: digest((build_dir / p).read_bytes()) for p in all_pages if (build_dir / p).is_file()}
    toc = (repo / "_toc.yml").read_text(encoding="utf-8") if (repo / "_toc.yml").exists() else ""
    navigation = []
    for entry in re.findall(r"(?m)^\s*(?:-\s*)?(?:root|file):\s*([^\s#]+)", toc):
        entry = entry.strip("\"'")
        if entry.endswith((".rst", ".md")):
            entry = entry.rsplit(".", 1)[0]
        navigation.append(entry if entry.endswith(".html") else entry + ".html")
    missing_navigation = [p for p in navigation if p not in expected and (not selection or p in selection)]
    errors.extend({"page": p, "error": "Navigation entry has no source document"} for p in missing_navigation)
    mandatory = set(expected) if selection else reachable(pages, navigation, link_target)
    linked = reachable(pages, navigation, link_target)
    audit = {"reachable": sorted(linked), "orphans": sorted(set(pages) - linked),
             "excluded": sorted(set(pages) - mandatory), "promoted": []}
    snapshot_metadata = {"commit": commit(repo), "source_hashes": before, "rendered_hashes": rendered,
                "build_command": command, "selection": selection, "normalization": "whitespace-v1",
                "unit_chars": UNIT_CHARS, "build_fresh": build, "source_mapping": "browser-evidence-v1"}
    return {"schema_version": "3.0", "execution_mode": "browser_accessibility", "snapshot_id": digest(snapshot_metadata), "snapshot": snapshot_metadata,
            "created_at": now(), "repo": str(repo), "build_dir": str(build_dir),
            "expected_pages": sorted(mandatory), "coverage_audit": audit, "observations": {},
            "browser": {"last_url": None, "discovered": {}, "search_failures": []}, "navigation": navigation, "pages": pages, "sections": sections,
            "inventory_errors": errors, "candidates": [], "journey": [], "api_calls": [],
            "audit": None, "runtime": {"status": "inventoried", "error": None}}


def verify_snapshot(state):
    if state.get("schema_version") != "3.0":
        raise ValueError("Legacy checkpoint: create a new browser inventory for schema 3.0.")
    repo, build_dir = Path(state["repo"]), Path(state["build_dir"])
    if commit(repo) != state["snapshot"]["commit"] or source_hashes(repo) != state["snapshot"]["source_hashes"]:
        raise ValueError("Repository snapshot changed; create a new inventory.")
    for page, expected in state["snapshot"]["rendered_hashes"].items():
        path = build_dir / page
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError("Rendered snapshot changed: " + page)
    if state["inventory_errors"]:
        raise ValueError("Inventory has unresolved extraction/missing-page errors; create a corrected inventory.")
