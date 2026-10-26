"""Browser observation integrity, source verification, and independent review."""

from difflib import SequenceMatcher
from pathlib import Path
import re

from .snapshot import digest
from .sources import resolve_browser_evidence
from .storage import now

PERSONAS = ('new_developer', 'application_engineer', 'platform_operator')
TYPES = ('factual', 'procedural', 'troubleshooting', 'cross_page')
DIFFICULTIES = ('easy', 'medium', 'hard')
PROMPT_VERSION = 'golden-browser-v3'
RUBRIC = ('answerable', 'supported', 'facts_complete', 'unambiguous', 'realistic', 'evidence_necessary')


def question_key(question):
    return ' '.join(re.findall(r'\w+', question.lower())) if isinstance(question, str) else ''


def validate(candidate, state, require_read=True, check_duplicates=True):
    errors = []
    for field in ('question', 'reference_answer'):
        if not isinstance(candidate.get(field), str) or not candidate[field].strip():
            errors.append(field + ' must be nonempty text')
    facts = candidate.get('required_facts')
    if not isinstance(facts, list) or not facts or not all(isinstance(f, str) and f.strip() for f in facts):
        facts = []
        errors.append('required_facts must be nonempty text entries')
    for field, allowed in (('persona', PERSONAS), ('question_type', TYPES), ('difficulty', DIFFICULTIES)):
        if candidate.get(field) not in allowed:
            errors.append('Invalid ' + field)
    if not isinstance(candidate.get('related_links'), list) or not all(isinstance(x, str) for x in candidate['related_links']):
        errors.append('related_links must be a list of strings')
    groups = candidate.get('evidence_groups')
    if not isinstance(groups, list) or not groups:
        errors.append('evidence_groups must be nonempty')
        groups = []
    covered, sections, required_pages = set(), set(), []
    for group in groups:
        if not isinstance(group, dict):
            errors.append('Invalid evidence group')
            continue
        indices = group.get('fact_indices')
        if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(facts) for i in indices):
            errors.append('Invalid fact indices')
        else:
            covered.update(indices)
        alternatives = group.get('alternatives')
        if not isinstance(alternatives, list) or not alternatives:
            errors.append('Missing evidence alternatives')
            continue
        pages = set()
        for e in alternatives:
            if not isinstance(e, dict):
                errors.append('Invalid evidence alternative')
                continue
            ident = e.get('observation_id')
            obs = state.get('observations', {}).get(ident) if isinstance(ident, str) else None
            excerpt, start, end = e.get('excerpt'), e.get('start'), e.get('end')
            if not obs or not isinstance(excerpt, str) or not excerpt.strip():
                errors.append('Unknown browser observation or empty quote')
                continue
            if type(start) is not int or type(end) is not int or start < 0 or end != start + len(excerpt) or obs['text'][start:end] != excerpt:
                errors.append('Browser quote offsets do not match')
                continue
            if require_read and not any(u['read'] and u['start'] <= start and u['end'] >= end for u in obs['chunks']):
                errors.append('Browser observation was not consumed')
            if e.get('section_id') != obs['section_id']:
                errors.append('Evidence section does not match browser observation')
            if obs['page'] not in state['expected_pages']:
                errors.append('Evidence page is outside dataset scope')
            spans = e.get('source_spans')
            if not isinstance(spans, list) or not spans:
                errors.append('Missing verified source spans')
            else:
                for span in spans:
                    try:
                        path = (Path(state['repo']) / span['source_path']).resolve()
                        path.relative_to(Path(state['repo']))
                        raw = path.read_text(encoding='utf-8-sig')
                        a, b = span['start'], span['end']
                        if (type(a) is not int or type(b) is not int or a < 0 or b <= a or raw[a:b] != span['excerpt'] or
                            raw.count('\n', 0, a) + 1 != span['line_start'] or raw.count('\n', 0, b - 1) + 1 != span['line_end']):
                            errors.append('Invalid source offsets or lines')
                        if digest(path.read_bytes()) != state['snapshot']['source_hashes'].get(span['source_path']):
                            errors.append('Source snapshot changed')
                    except (OSError, ValueError, KeyError, TypeError):
                        errors.append('Invalid source location')
            sections.add(obs['section_id'])
            pages.add(obs['page'])
        required_pages.append(pages)
    if covered != set(range(len(facts))):
        errors.append('Not every required fact has evidence')
    if candidate.get('target_section') not in sections:
        errors.append('Question does not cover its target section')
    if candidate.get('question_type') == 'cross_page' and not any(a and b and a.isdisjoint(b) for i, a in enumerate(required_pages) for b in required_pages[i + 1:]):
        errors.append('Cross-page questions require necessary groups on different pages')
    key = question_key(candidate.get('question'))
    for other in state['candidates'] if check_duplicates else []:
        if other['id'] == candidate.get('id') or other['status'] != 'accepted' or other['human_status'] == 'rejected':
            continue
        previous = question_key(other.get('question'))
        match = SequenceMatcher(None, key, previous)
        if key == previous or (match.real_quick_ratio() >= .93 and match.quick_ratio() >= .93 and match.ratio() >= .93):
            errors.append('duplicate_question')
            break
    return errors


def create_candidate(raw, state, section_id, config):
    if section_id not in state['sections']:
        raise ValueError('Unknown observed target section')
    keys = ('question', 'reference_answer', 'required_facts', 'persona', 'question_type', 'difficulty', 'evidence_groups', 'related_links')
    record = {key: raw.get(key) for key in keys}
    page = state['sections'][section_id]['page']
    parts = page.split('/')
    product = parts[2] if len(parts) > 3 and parts[1] == 'products' else (parts[1] if len(parts) > 2 else 'overview')
    record.update(id=digest([state['snapshot_id'], section_id, state['sections'][section_id]['attempts'], question_key(raw.get('question'))]),
                  snapshot_id=state['snapshot_id'], target_section=section_id, product=product, created_at=now(),
                  status='pending_revalidation', verification_stage='drafted', human_status='pending', review_history=[],
                  provenance={'execution_mode': 'browser_accessibility', 'generator_model': config['generator_model'],
                              'reviewer_model': config['reviewer_model'], 'prompt_version': PROMPT_VERSION,
                              'config_id': digest(config), 'journey_end': len(state['journey'])})
    return record


def review_candidate(candidate, state, client):
    try:
        groups = candidate.get('evidence_groups')
        if not isinstance(groups, list):
            raise ValueError('Invalid evidence groups')
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('alternatives'), list):
                raise ValueError('Invalid alternatives')
            for evidence in group['alternatives']:
                if not isinstance(evidence, dict):
                    raise ValueError('Invalid evidence')
                resolve_browser_evidence(state, evidence)
    except (ValueError, TypeError, KeyError, OSError):
        candidate.update(status='rejected', verification_stage='source_failed',
                         validation={'deterministic_errors': ['browser_source_verification_failed']})
        return False
    candidate['verification_stage'] = 'source_verified'
    errors = validate(candidate, state)
    candidate['validation'] = {'deterministic_errors': errors}
    if errors:
        candidate['status'] = 'rejected'
        return False
    result = client.ask('reviewer', 'Independently assess the standalone English question, reference answer, and required facts. '
                        'Check support in BOTH consumed browser observations and verified raw source_spans, fact completeness, '
                        'ambiguity, realism, and necessity of every evidence group. Each alternative independently supports its indexed facts. '
                        'Return {"checks":{' + ','.join('"' + key + '":true' for key in RUBRIC) + '},"notes":"brief justification"}.',
                        {'candidate': {k: candidate[k] for k in ('question', 'reference_answer', 'required_facts', 'question_type', 'evidence_groups')},
                         'browser_evidence': [{'observation_id': e['observation_id'], 'text': e['excerpt']}
                                              for group in candidate['evidence_groups'] for e in group['alternatives']]})
    checks = result.get('checks')
    passed = isinstance(checks, dict) and all(checks.get(key) is True for key in RUBRIC)
    candidate['validation']['model_review'] = result
    candidate.update(status='accepted' if passed else 'rejected', verification_stage='reviewed')
    return passed
