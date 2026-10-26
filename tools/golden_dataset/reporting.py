"""Coverage, stratified audit membership, release gates, and portable exports."""

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from .snapshot import digest, verify_snapshot
from .storage import now
from .validation import validate
from .sources import public_candidate, public_section_id


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else 1.0}


def accepted(state):
    return [c for c in state["candidates"] if c["status"] == "accepted" and c["human_status"] != "rejected"]


def covered_sections(state):
    # An alternative source is relevant, but only the target has been explicitly
    # generated and reviewed as a section-coverage item.
    return {c["target_section"] for c in accepted(state)}


def audit_fingerprint(state):
    return digest(sorted(c["id"] for c in accepted(state)))


def select_audit(state, new_round=False):
    previous = state.get("audit")
    if previous:
        if not new_round:
            raise ValueError("Audit sample is already frozen; resolve required reviews before requesting --new-round.")
        if audit_result(state)["unresolved"]:
            raise ValueError("Resolve all audit defects and required category reviews before starting a new round.")
    records = accepted(state)
    if not records:
        raise ValueError("No accepted questions to audit")
    size = min(len(records), max(100, math.ceil(len(records) * 0.05)))
    strata = defaultdict(list)
    for c in records:
        strata[(c["product"], c["question_type"], c["difficulty"])].append(c["id"])
    for values in strata.values():
        values.sort(key=lambda ident: digest([state["snapshot_id"], len(state.get("audit_history", [])) + bool(previous), ident]))
    # Largest-remainder proportional allocation; deterministic across restarts.
    allocations = {key: len(values) * size // len(records) for key, values in strata.items()}
    remaining = size - sum(allocations.values())
    order = sorted(strata, key=lambda key: (-(len(strata[key]) * size % len(records)), key))
    for key in order[:remaining]:
        allocations[key] += 1
    members = sorted(ident for key, values in strata.items() for ident in values[:allocations[key]])
    if previous:
        state.setdefault("audit_history", []).append(previous)
    state["audit"] = {"created_at": now(), "fingerprint": audit_fingerprint(state), "members": members,
                      "population": sorted(c["id"] for c in records),
                      "history_offsets": {c["id"]: len(c["review_history"]) for c in records},
                      "sample_size": size, "strata": {"/".join(k): allocations[k] for k in sorted(strata)},
                      "failures": [], "expanded_categories": []}
    for c in records:
        if c["id"] in members:
            c["human_status"] = "pending"
    return state["audit"]


def audit_result(state):
    audit = state.get("audit")
    if not audit:
        return {"passed": False, "reason": "Audit sample has not been selected", "required": []}
    records = {c["id"]: c for c in state["candidates"]}
    members = [records[i] for i in audit["members"] if i in records]
    # Count the original human outcome even after a defect has been repaired.
    outcomes = {}
    for c in members:
        offset = audit.get("history_offsets", {}).get(c["id"], 0)
        first = next((r for r in c["review_history"][offset:] if r["action"] in {"approve", "reject", "edit"}), None)
        if first:
            outcomes[c["id"]] = first["action"] == "approve"
    required = set(audit["members"])
    categories = {tuple(x) for x in audit["expanded_categories"]}
    for c in accepted(state):
        if (c["product"], c["question_type"]) in categories:
            required.add(c["id"])
    unresolved = [ident for ident in sorted(required) if ident not in records or
                  not (records[ident]["human_status"] == "rejected" or
                       (records[ident]["human_status"] == "approved" and records[ident]["status"] == "accepted"))]
    rate = ratio(sum(outcomes.values()), len(audit["members"]))
    # Removed/repaired sample members legitimately change membership. New unseen
    # records need a new sample; removal of known defects does not discard failures.
    sampled_at_creation = set(audit.get("population", []))
    new_records = set(c["id"] for c in accepted(state)) - sampled_at_creation if sampled_at_creation else set()
    required.update(new_records)
    unresolved.extend(i for i in sorted(new_records) if records[i]["human_status"] != "approved")
    return {"passed": bool(members) and not unresolved and rate["rate"] >= 0.95,
            "initial_pass_rate": rate, "reviewed": len(outcomes), "required": sorted(required),
            "unresolved": sorted(set(unresolved)), "new_records_require_review": len(new_records),
            "expanded_categories": sorted(categories)}


def report(state):
    sections = list(state["sections"].values())
    substantive = [s for s in sections if s["substantive"]]
    covered = covered_sections(state)
    read = lambda s: all(u["read"] for u in s["units"])
    pages_read = sum(all(read(state["sections"][sid]) for sid in p["sections"]) for p in state["pages"].values())
    records = accepted(state)
    integrity = sum(not validate(c, state, check_duplicates=False) for c in records)
    candidates = state["candidates"]
    auto_passed = sum(c["status"] == "accepted" for c in candidates)
    duplicate = sum("duplicate_question" in c.get("validation", {}).get("deterministic_errors", []) for c in candidates)
    gaps = [{"section_id": s["id"], "attempts": s["attempts"], "error": s["error"]}
            for s in substantive if s["id"] not in covered]
    runtime_error = state["runtime"].get("error")
    browsing_complete = (pages_read == len(state["expected_pages"]) and not state["inventory_errors"] and
                         not runtime_error and all(read(s) for s in sections))
    audit = audit_result(state)
    ready = browsing_complete and bool(records) and not gaps and integrity == len(records) and audit["passed"]
    return {"snapshot_id": state["snapshot_id"], "browsing_complete": browsing_complete, "ready": ready,
            "runtime": state["runtime"], "inventory_errors": state["inventory_errors"],
            "page_coverage": ratio(pages_read, len(state["expected_pages"])),
            "section_reading_coverage": ratio(sum(read(s) for s in sections), len(sections)),
            "reading_unit_coverage": ratio(sum(u["read"] for s in sections for u in s["units"]), sum(len(s["units"]) for s in sections)),
            "question_coverage": ratio(sum(s["id"] in covered for s in substantive), len(substantive)),
            "evidence_integrity": ratio(integrity, len(records)),
            "automated_acceptance_rate": ratio(auto_passed, len(candidates)),
            "duplicate_rate": ratio(duplicate, len(candidates)), "audit": audit,
            "distribution": {key: dict(Counter(c[key] for c in records)) for key in ("product", "question_type", "persona", "difficulty")},
            "accepted": len(records), "candidates": len(candidates), "gaps": gaps,
            "exclusions": [{"section_id": s["id"], "reason": s["exclusion_reason"]} for s in sections if not s["substantive"]],
            "usage": {"calls": len(state["api_calls"]), "tokens": sum(c["tokens"] for c in state["api_calls"]),
                      "cost": sum(c["cost"] for c in state["api_calls"]),
                      "cost_known": bool(state["api_calls"]) and all(c.get("cost_known", False) for c in state["api_calls"]),
                      "estimated_calls": sum(c["usage_estimated"] for c in state["api_calls"])}}


def export(state, destination, ready_only=False):
    verify_snapshot(state)
    metrics = report(state)
    if ready_only and not metrics["ready"]:
        raise ValueError("Dataset is not ready: resolve coverage, validation, and audit gates first.")
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Export directory must be empty; use a new version directory.")
    def write(name, value):
        (target / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    source_sections = [{"id": s["source_section_id"], "document_path": s["document_path"],
                        "heading_path": s["headings"], "source": s["source"],
                        "text": s["source"]["excerpt"], "substantive": s["substantive"],
                        "exclusion_reason": s["exclusion_reason"],
                        "browsing": {"section_id": s["id"], "page": s["page"], "anchor": s["anchor"],
                                     "text": s["text"], "units": s["units"], "source_mappings": s["source_mappings"]}}
                       for s in state["sections"].values()]
    for name, records in (("questions.jsonl", (public_candidate(state, c) for c in accepted(state))),
                          ("candidates.jsonl", (public_candidate(state, c) for c in state["candidates"])),
                          ("sections.jsonl", source_sections), ("journey.jsonl", state["journey"])):
        with (target / name).open("w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    import copy
    public_metrics = copy.deepcopy(metrics)
    for item in public_metrics["gaps"] + public_metrics["exclusions"]:
        item["section_id"] = public_section_id(state, item["section_id"])
    write("coverage.json", public_metrics)
    write("manifest.json", {"schema_version": state["schema_version"], "snapshot_id": state["snapshot_id"],
                            "snapshot": state["snapshot"], "created_at": now(),
                            "release_status": "audited_ready" if metrics["ready"] else "candidate_incomplete",
                            "documents": sorted({p["source_path"] for p in state["pages"].values()}),
                            "browsing": {"pages": state["pages"], "expected_pages": state["expected_pages"]},
                            "audit": state["audit"], "audit_history": state.get("audit_history", []), "api_calls": state["api_calls"],
                            "files": {p.name: digest(p.read_bytes()) for p in sorted(target.iterdir())}})
    return metrics
