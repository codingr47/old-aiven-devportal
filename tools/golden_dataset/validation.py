"""Provider-independent labels, integrity checks, and independent model rubric."""

import re
from difflib import SequenceMatcher

from .snapshot import digest
from .sources import evidence_sources
from .storage import now

PERSONAS = ("new_developer", "application_engineer", "platform_operator")
TYPES = ("factual", "procedural", "troubleshooting", "cross_page")
DIFFICULTIES = ("easy", "medium", "hard")
PROMPT_VERSION = "golden-v2"
RUBRIC = ("answerable", "supported", "facts_complete", "unambiguous", "realistic", "evidence_necessary")

CANDIDATE_TASK = """Create one realistic standalone English question and reference answer for the target section.
Use the persona and choose a suitable type: factual, procedural, troubleshooting, cross_page.
Prefer a cross_page question only when the supplied linked evidence is necessary to answer the task.
No external knowledge. Do not ask about the documentation itself or invent troubleshooting causes.
Return {"question":"...","reference_answer":"...","required_facts":["..."],
"persona":"new_developer|application_engineer|platform_operator","question_type":"...",
"difficulty":"easy|medium|hard","evidence_groups":[{"fact_indices":[0],"alternatives":[
{"section_id":"page.html#anchor","excerpt":"exact contiguous excerpt copied from supplied text"}]}],
"related_links":[]}.
Each required fact index must have supporting evidence. Each group is necessary evidence; alternatives are interchangeable.
Include target-section evidence. Excerpts must be verbatim normalized text, not summaries or ellipses.
Only cite text actually provided through a reading unit. Rendered section IDs are browsing handles only.
Raw source mappings supplied with evidence identify the actual .rst/.md documents or included files.
Choose evidence supported by those raw source excerpts, not generated UI labels. Return no other keys."""


def question_key(question):
    if not isinstance(question, str):
        return ""
    return " ".join(re.findall(r"\w+", question.lower()))


def validate(candidate, state, require_read=True, check_duplicates=True):
    errors = []
    for field in ("question", "reference_answer"):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            errors.append(field + " must be nonempty text")
    facts = candidate.get("required_facts")
    if not isinstance(facts, list) or not facts or not all(isinstance(f, str) and f.strip() for f in facts):
        errors.append("required_facts must be a nonempty list of text")
        facts = []
    for field, values in (("persona", PERSONAS), ("question_type", TYPES), ("difficulty", DIFFICULTIES)):
        if candidate.get(field) not in values:
            errors.append("Invalid " + field)
    links = candidate.get("related_links")
    if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
        errors.append("related_links must be a list of link strings")
    groups = candidate.get("evidence_groups")
    covered_facts, evidence_sections, pages = set(), set(), set()
    if not isinstance(groups, list) or not groups:
        errors.append("evidence_groups must be nonempty")
        groups = []
    for group in groups:
        if not isinstance(group, dict):
            errors.append("Invalid evidence group")
            continue
        indices = group.get("fact_indices")
        if not isinstance(indices, list) or not indices or any(type(i) is not int or i < 0 or i >= len(facts) for i in indices):
            errors.append("Evidence must reference valid required fact indices")
        else:
            covered_facts.update(indices)
        alternatives = group.get("alternatives")
        if not isinstance(alternatives, list) or not alternatives:
            errors.append("Evidence group has no alternatives")
            continue
        for evidence in alternatives:
            if not isinstance(evidence, dict):
                errors.append("Invalid evidence alternative")
                continue
            sid = evidence.get("section_id")
            section = state["sections"].get(sid) if isinstance(sid, str) else None
            excerpt = evidence.get("excerpt")
            if not section or not isinstance(excerpt, str) or not excerpt.strip():
                errors.append("Unknown section or empty excerpt")
                continue
            start = section["text"].find(excerpt)
            if start < 0:
                errors.append("Excerpt does not match normalized section text")
                continue
            end = start + len(excerpt)
            if "start" in evidence and (evidence["start"] != start or evidence.get("end") != end):
                errors.append("Evidence offsets do not match excerpt")
            if require_read and not any(u["read"] and u["start"] <= start and u["end"] >= end for u in section["units"]):
                errors.append("Evidence was not read in a complete reading unit")
            supplied = candidate.get("provenance", {}).get("reading_units")
            if supplied is not None and not any(u["id"] in supplied and u["start"] <= start and u["end"] >= end for u in section["units"]):
                errors.append("Evidence was not supplied to this generation attempt")
            evidence.update(start=start, end=end, page=section["page"], anchor=section["anchor"])
            spans = evidence_sources(section, start, end)
            if not spans:
                errors.append("Rendered evidence has no verified source mapping")
            if "source_spans" in evidence and evidence["source_spans"] != spans:
                errors.append("Source spans do not match the browsed evidence")
            evidence["source_spans"] = spans
            evidence_sections.add(section["id"])
            pages.add(section["page"])
    if covered_facts != set(range(len(facts))):
        errors.append("Not every required fact has evidence")
    if candidate.get("target_section") not in evidence_sections:
        errors.append("Question does not cover its target section")
    # At least two groups on distinct required pages, not interchangeable pages.
    if candidate.get("question_type") == "cross_page":
        required_pages = [set(e.get("page") for e in g["alternatives"] if isinstance(e, dict) and isinstance(e.get("page"), str))
                          for g in groups if isinstance(g, dict) and isinstance(g.get("alternatives"), list)]
        if len(pages) < 2 or not any(a.isdisjoint(b) for i, a in enumerate(required_pages) for b in required_pages[i + 1:]):
            errors.append("Cross-page questions require separate necessary evidence groups on different pages")
    key = question_key(candidate.get("question", ""))
    for other in state["candidates"] if check_duplicates else []:
        if other.get("id") == candidate.get("id") or other.get("status") != "accepted" or other.get("human_status") == "rejected":
            continue
        other_key = question_key(other.get("question", ""))
        matcher = SequenceMatcher(None, key, other_key)
        if key == other_key or (matcher.real_quick_ratio() >= 0.93 and matcher.quick_ratio() >= 0.93 and matcher.ratio() >= 0.93):
            errors.append("duplicate_question")
            break
    return errors


def create_candidate(raw, state, section_id, config):
    if not isinstance(raw, dict):
        raise ValueError("Candidate must be an object")
    allowed = ("question", "reference_answer", "required_facts", "persona", "question_type", "difficulty", "evidence_groups", "related_links")
    candidate = {key: raw.get(key) for key in allowed}
    page = section_id.split("#")[0]
    parts = page.split("/")
    product = parts[2] if len(parts) > 3 and parts[1] == "products" else (parts[1] if len(parts) > 2 else "overview")
    candidate.update(id=digest([state["snapshot_id"], question_key(raw.get("question", "")), section_id,
                                state["sections"][section_id]["attempts"]]),
                     snapshot_id=state["snapshot_id"], target_section=section_id, product=product,
                     created_at=now(), status="pending", human_status="pending", review_history=[],
                     provenance={"generator_model": config["generator_model"], "reviewer_model": config["reviewer_model"],
                                 "prompt_version": PROMPT_VERSION, "config_id": digest(config),
                                 "journey_end": len(state["journey"])})
    return candidate


def review_candidate(candidate, state, client):
    errors = validate(candidate, state)
    candidate["validation"] = {"deterministic_errors": errors}
    if errors:
        candidate["status"] = "rejected"
        return False
    evidence = []
    for group in candidate["evidence_groups"]:
        for item in group["alternatives"]:
            section = state["sections"][item["section_id"]]
            # Give the independent reviewer bounded surrounding context, not generator notes.
            evidence.append({**item, "headings": section["headings"],
                             "context": section["text"][max(0, item["start"] - 1500):item["end"] + 1500]})
    result = client.ask("reviewer", "Independently assess the question against the supplied evidence only. "
                        "Check English, standalone phrasing, factual support in the supplied raw source_spans "
                        "(not merely the rendered display), complete required facts, ambiguity, realism, "
                        "and necessity of EVERY evidence group (including cross-page necessity). Each alternative must "
                        "independently support the group's indexed required facts. "
                        "Return {\"checks\":{" + ",".join('"' + k + '":true' for k in RUBRIC) +
                        '},"notes":"brief specific justification"}. Set any failing check to false.',
                        {"candidate": {k: candidate[k] for k in ("question", "reference_answer", "required_facts", "question_type", "evidence_groups")},
                         "evidence": evidence})
    checks = result.get("checks", {})
    passed = isinstance(checks, dict) and all(checks.get(k) is True for k in RUBRIC)
    candidate["validation"]["model_review"] = result
    candidate["status"] = "accepted" if passed else "rejected"
    return passed
