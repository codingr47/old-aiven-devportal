"""Map browsed sections back to exact repository text using Sphinx source metadata.

Doctrees are Python pickles: only read builds produced locally by this tool, or
an explicitly selected trusted local Sphinx build. Never accept uploaded builds.
"""

import pickle
import re
from difflib import SequenceMatcher
from pathlib import Path


def compact(text):
    return re.sub(r"\s+", "", text)


def attach_sources(repo, build_dir, page, info, sections, source_path):
    tree_path = build_dir / ".doctrees" / Path(page).with_suffix(".doctree")
    if not tree_path.is_file():
        raise ValueError("Source mapping requires the trusted local Sphinx .doctrees directory")
    with tree_path.open("rb") as stream:
        tree = pickle.load(stream)
    nodes = list(tree.traverse())
    section_nodes = [n for n in nodes if getattr(n, "tagname", "") == "section"]
    by_anchor = {anchor: n for n in section_nodes for anchor in n.get("ids", [])}
    files = {}

    def read(path):
        path = Path(path)
        if not path.is_absolute():
            path = repo / path
        path = path.resolve()
        try:
            relative = path.relative_to(repo).as_posix()
        except ValueError:
            raise ValueError("Source mapping points outside the repository") from None
        if relative not in files:
            # Offsets are Unicode character indices in UTF-8 decoded text with LF newlines.
            files[relative] = path.read_text(encoding="utf-8-sig")
        return relative, files[relative]

    def location(path, start, end):
        relative, text = read(path)
        return {"source_path": relative, "start": start, "end": end,
                "line_start": text.count("\n", 0, start) + 1,
                "line_end": text.count("\n", 0, max(start, end - 1)) + 1,
                "excerpt": text[start:end]}

    def heading_start(node):
        relative, text = read(getattr(node, "source", None) or source_path)
        lines = text.splitlines(keepends=True)
        line = max(0, min((getattr(node, "line", None) or 1) - 1, len(lines) - 1))
        # Docutils RST section lines often point to the underline, Markdown to the title.
        if line > 0 and re.fullmatch(r"\s*([=~`^\-:+*#_])\1{2,}\s*", lines[line]):
            line -= 1
        return relative, sum(len(x) for x in lines[:line])

    def owned_section(node):
        parent = getattr(node, "parent", None)
        while parent is not None and getattr(parent, "tagname", "") != "section":
            parent = getattr(parent, "parent", None)
        return parent

    for index, section in enumerate(sections):
        node = by_anchor.get(section["anchor"]) if section["anchor"] else tree
        if node is None:
            raise ValueError("No source section for rendered anchor: " + section["anchor"])
        path, start = heading_start(node)
        _, text = read(path)
        following = [offset for other in section_nodes if other is not node
                     for other_path, offset in [heading_start(other)] if other_path == path and offset > start]
        end = min(following) if following else len(text)
        section["source_section_id"] = f"{path}::L{text.count(chr(10), 0, start) + 1}"
        section["document_path"] = source_path
        section["source"] = {**location(path, start, end), "heading_path": section["headings"]}
        # Align complete semantic blocks with rendered text, keeping exact raw
        # excerpts separately. Whitespace-only differences from HTML spans do not
        # change source identity or source offsets.
        rendered = section["text"]
        positions = [i for i, ch in enumerate(rendered) if not ch.isspace()]
        flattened = "".join(rendered[i] for i in positions)
        cursor = 0
        mappings = []
        for child in node.traverse():
            if getattr(child, "tagname", "") not in {"paragraph", "literal_block", "doctest_block", "line"}:
                continue
            if owned_section(child) is not (node if node is not tree else None):
                continue
            visible = compact(child.astext())
            found = flattened.find(visible, cursor) if visible else -1
            mapping_method = "exact_display"
            if found < 0:
                mapping_method = "transformed_block"
                # Sphinx resolves implicit cross-reference titles and wraps math
                # after storing the doctree. Match an actual rendered block, never
                # fabricate a source line or replace its raw text with display text.
                expected_tag = "pre" if child.tagname in {"literal_block", "doctest_block"} else "p"
                choices = [(SequenceMatcher(None, visible, compact(b["text"])).ratio(), b)
                           for b in section["rendered_blocks"] if b["tag"] == expected_tag and
                           (not positions or b["start"] >= positions[min(cursor, len(positions) - 1)])]
                score, block = max(choices, key=lambda x: x[0]) if choices else (0, None)
                if score < 0.35:
                    continue
                rendered_start, rendered_end = block["start"], block["end"]
                cursor = sum(i < rendered_end for i in positions)
            else:
                cursor = found + len(visible)
                rendered_start, rendered_end = positions[found], positions[cursor - 1] + 1
            origin = child.get("source") or getattr(child, "source", None)
            raw = getattr(child, "rawsource", "")
            if not origin:
                origin = path
            origin_path, origin_text = read(origin)
            candidates = [m.start() for m in re.finditer(re.escape(raw), origin_text)] if raw else []
            expected_line = getattr(child, "line", None) or 1
            expected_offset = sum(len(x) for x in origin_text.splitlines(keepends=True)[:expected_line - 1])
            if candidates:
                raw_start = min(candidates, key=lambda offset: abs(offset - expected_offset))
                raw_end = raw_start + len(raw)
                precision = "exact_block"
            elif origin_path == path:
                raw_start, raw_end = start, end
                precision = "section"
            else:
                # For transformed literalinclude content use the actual included
                # file, never invent line precision from the include directive.
                raw_start, raw_end = 0, len(origin_text)
                precision = "file"
            mappings.append({"rendered_start": rendered_start, "rendered_end": rendered_end,
                             "mapping_method": mapping_method, "precision": precision,
                             **location(origin_path, raw_start, raw_end)})
        section["source_mappings"] = mappings
        if section["substantive"] and not mappings:
            raise ValueError("No source-backed content blocks for " + section["id"])
    info["source_path"] = source_path


def evidence_sources(section, start, end):
    """Resolve displayed evidence to actual raw source blocks, including includes."""
    mappings = [m for m in section.get("source_mappings", []) if m["rendered_start"] < end and m["rendered_end"] > start]
    covered = set()
    for m in mappings:
        covered.update(range(max(start, m["rendered_start"]), min(end, m["rendered_end"])))
    # Unmapped generated UI labels must not become source-backed gold evidence.
    if any(ch.isalnum() and start + i not in covered for i, ch in enumerate(section["text"][start:end])):
        return []
    spans = []
    for m in mappings:
        span = {k: v for k, v in m.items() if k not in {"rendered_start", "rendered_end"}}
        if span not in spans:
            spans.append(span)
    return spans


def public_section_id(state, sid):
    return state["sections"][sid]["source_section_id"]


def public_candidate(state, record):
    import copy
    result = copy.deepcopy(record)
    result["target_section"] = public_section_id(state, record["target_section"])
    result["document_path"] = state["sections"][record["target_section"]]["document_path"]
    result["browsing"] = {"target_section": record["target_section"], "related_links": result.get("related_links")}
    links = state["pages"][state["sections"][record["target_section"]]["page"]]["links"]
    related = record.get("related_links") or []
    result["related_links"] = sorted({state["pages"][link["target"]["page"]]["source_path"] for link in links
                                      if link["href"] in related and link.get("target") and link["target"]["page"] in state["pages"]})
    for group in result.get("evidence_groups") or []:
        if not isinstance(group, dict) or not isinstance(group.get("alternatives"), list):
            continue
        for evidence in group["alternatives"]:
            if not isinstance(evidence, dict) or not isinstance(evidence.get("section_id"), str) or evidence["section_id"] not in state["sections"]:
                continue
            section = state["sections"][evidence["section_id"]]
            evidence["section_id"] = section["source_section_id"]
            evidence["document_path"] = section["document_path"]
            evidence["heading_path"] = section["headings"]
            evidence["browsing"] = {k: evidence.pop(k) for k in ("page", "anchor", "excerpt", "start", "end") if k in evidence}
    return result
