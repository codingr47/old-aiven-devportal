"""Sphinx snapshot inventory and non-overlapping section extraction."""

import hashlib
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, SoupStrainer

from .storage import now
from .sources import attach_sources

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


def extract_page(path, page):
    html = path.read_text(encoding="utf-8")
    # Furo's repeated sidebar dominates page size. Avoid allocating that tree.
    soup = BeautifulSoup(html, "html.parser", parse_only=SoupStrainer("article"))
    article = soup.select_one("article")
    if article is None:
        soup = BeautifulSoup(html, "html.parser")
        article = soup.select_one("div.article-container")
    if article is None:
        raise ValueError("No documentation article element")
    for item in article.select(".toctree-wrapper, .headerlink, .back-to-top, .content-icon-container, script, style"):
        item.decompose()
    sections = []
    containers = [article] + list(article.select("section[id], div.section[id]"))
    for container in containers:
        anchor = container.get("id", "") if container is not article else ""
        # Only direct-owned text: child sections are extracted separately.
        pieces = []
        for node in container.descendants:
            if not isinstance(node, NavigableString) or isinstance(node, Comment):
                continue
            owner = node.parent
            heading = False
            while owner is not None and owner is not container:
                if owner.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                    heading = True
                if owner.name == "section" or (owner.name == "div" and "section" in owner.get("class", [])):
                    break
                owner = owner.parent
            if owner is container and not heading:
                pieces.append(str(node))
        text = normalize(" ".join(pieces))
        headings = []
        current = container
        while current is not None and current is not article:
            h = current.find(re.compile(r"^h[1-6]$"), recursive=False)
            if h:
                headings.insert(0, normalize(h.get_text(" ")))
            current = current.find_parent(["section", "div"])
        if container is article and not text:
            continue
        # A section made exclusively of link labels is navigation, not answer evidence.
        owned_links = []
        for a in container.select("a[href]"):
            parent = a.parent
            while parent is not None and parent is not container:
                if parent.name == "section" or (parent.name == "div" and "section" in parent.get("class", [])):
                    break
                parent = parent.parent
            if parent is container:
                owned_links.append(a)
        residue = text
        for a in owned_links:
            residue = residue.replace(normalize(a.get_text(" ")), "", 1)
        substantive = bool(normalize(residue).strip("•-:|,.; "))
        sid = page + "#" + anchor
        blocks = []
        block_cursor = 0
        for block in container.select("p, pre"):
            parent = block.parent
            while parent is not None and parent is not container:
                if parent.name == "section" or (parent.name == "div" and "section" in parent.get("class", [])):
                    break
                parent = parent.parent
            if parent is not container or "admonition-title" in block.get("class", []):
                continue
            block_text = normalize(block.get_text(" "))
            offset = text.find(block_text, block_cursor) if block_text else -1
            if offset >= 0:
                blocks.append({"tag": block.name, "start": offset, "end": offset + len(block_text), "text": block_text})
                block_cursor = offset + len(block_text)
        units = [{"id": f"{sid}@{i}", "start": start, "end": min(start + UNIT_CHARS, len(text)), "read": False}
                 for i, start in enumerate(range(0, len(text), UNIT_CHARS))]
        sections.append({"id": sid, "page": page, "anchor": anchor, "headings": headings,
                         "text": text, "substantive": substantive,
                         "exclusion_reason": None if substantive else ("empty" if not text else "navigation-only"),
                         "units": units, "attempts": 0, "error": None, "rendered_blocks": blocks})
    links = [{"href": a["href"], "label": normalize(a.get_text(" ")),
              "target": link_target(page, a["href"])} for a in article.select("a[href]")]
    title = soup.find("h1")
    return {"id": page, "title": normalize(title.get_text(" ")) if title else page,
            "sections": [s["id"] for s in sections], "links": links}, sections


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
    expected = list(source_pages)
    if selection:
        unknown = set(selection) - set(expected)
        if unknown:
            raise ValueError(f"Unknown selected pages: {sorted(unknown)}")
        expected = [p for p in expected if p in selection]
    pages, sections, errors = {}, {}, []
    for page in expected:
        try:
            info, items = extract_page(build_dir / page, page)
            attach_sources(repo, build_dir, page, info, items, source_pages[page])
            pages[page] = info
            sections.update({s["id"]: s for s in items})
        except (OSError, ValueError) as exc:
            errors.append({"page": page, "error": type(exc).__name__ + ": " + str(exc)})
    rendered = {p: digest((build_dir / p).read_bytes()) for p in expected if (build_dir / p).is_file()}
    toc = (repo / "_toc.yml").read_text(encoding="utf-8") if (repo / "_toc.yml").exists() else ""
    navigation = []
    for entry in re.findall(r"(?m)^\s*(?:-\s*)?(?:root|file):\s*([^\s#]+)", toc):
        entry = entry.strip("\"'")
        if entry.endswith((".rst", ".md")):
            entry = entry.rsplit(".", 1)[0]
        navigation.append(entry if entry.endswith(".html") else entry + ".html")
    missing_navigation = [p for p in navigation if p not in expected and (not selection or p in selection)]
    errors.extend({"page": p, "error": "Navigation entry has no source document"} for p in missing_navigation)
    metadata = {"commit": commit(repo), "source_hashes": before, "rendered_hashes": rendered,
                "build_command": command, "selection": selection, "normalization": "whitespace-v1",
                "unit_chars": UNIT_CHARS, "build_fresh": build, "source_mapping": "sphinx-doctree-v1"}
    return {"schema_version": "2.0", "snapshot_id": digest(metadata), "snapshot": metadata,
            "created_at": now(), "repo": str(repo), "build_dir": str(build_dir),
            "expected_pages": expected, "navigation": navigation, "pages": pages, "sections": sections,
            "inventory_errors": errors, "candidates": [], "journey": [], "api_calls": [],
            "audit": None, "runtime": {"status": "inventoried", "error": None}}


def verify_snapshot(state):
    if state.get("schema_version") != "2.0":
        raise ValueError("Legacy HTML-only checkpoint: create a new inventory for source-grounded schema 2.0.")
    repo, build_dir = Path(state["repo"]), Path(state["build_dir"])
    if commit(repo) != state["snapshot"]["commit"] or source_hashes(repo) != state["snapshot"]["source_hashes"]:
        raise ValueError("Repository snapshot changed; create a new inventory.")
    for page, expected in state["snapshot"]["rendered_hashes"].items():
        path = build_dir / page
        if not path.is_file() or digest(path.read_bytes()) != expected:
            raise ValueError("Rendered snapshot changed: " + page)
    if state["inventory_errors"]:
        raise ValueError("Inventory has unresolved extraction/missing-page errors; create a corrected inventory.")
