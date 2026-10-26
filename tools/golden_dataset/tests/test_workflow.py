import copy
import json
import os
import pickle
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from docutils import nodes
from docutils.utils import new_document

from tools.golden_dataset.engine import Browser, run
from tools.golden_dataset.model import Client, InvalidOutput, Paused, load_config, parse_json
from tools.golden_dataset.reporting import accepted, audit_result, export, report, select_audit
from tools.golden_dataset.review import decide, make_server
from tools.golden_dataset.snapshot import UNIT_CHARS, digest, extract_page, inventory, verify_snapshot
from tools.golden_dataset.storage import Store
from tools.golden_dataset.validation import RUBRIC, create_candidate, review_candidate, validate
from tools.golden_dataset.sources import evidence_sources


PAGE = '''<html><body><nav>Noise</nav><article><section id="start"><h1>Kafka operations</h1>
<div class="toctree-wrapper">Navigation noise</div><p>The cleaner is enabled by default.</p>
<section id="compact"><h2>Enable compaction<a class="headerlink">¶</a></h2><p>Set cleanup.policy to compact.</p>
<div class="warning"><p>This affects all topics.</p></div><pre>cleanup.policy=compact</pre>
<table><tr><th>Setting</th><td>compact</td></tr></table><p>Included snippet content.</p>
<a href="docs/other.html#limits">Related limits</a><a href="https://example.org/">External</a></section>
<section id="navigation"><h2>See also</h2><a href="docs/other.html">Other page</a></section>
</section></article><footer>Noise</footer></body></html>'''
OTHER = '<article><section id="limits"><h1>Limits</h1><p>Compaction requires a keyed record.</p></section></article>'

RST = '''Kafka operations
================

The cleaner is enabled by default.

Enable compaction
-----------------

Set cleanup.policy to compact.
This affects all topics.
cleanup.policy=compact
Setting compact
Included snippet content.
`Related limits <docs/other.rst#limits>`_ `External <https://example.org/>`_

See also
--------

`Other page <docs/other.rst>`_
'''


def write_fixture_tree(repo, build):
    def make(page, definitions):
        source = str(repo / page.replace('.html', '.rst'))
        text = Path(source).read_text()
        tree = new_document(source)
        parent = tree
        for i, (anchor, title, paragraphs) in enumerate(definitions):
            section = nodes.section(ids=[anchor])
            section.source = source
            section.line = text[:text.index(title)].count('\n') + 2
            section += nodes.title('', title)
            if i == 0:
                tree += section
                parent = section
            else:
                parent += section
            for visible in paragraphs:
                paragraph = nodes.paragraph(visible, visible)
                paragraph.source = source
                paragraph.line = text[:text.find(visible)].count('\n') + 1 if visible in text else 1
                section += paragraph
        path = build / '.doctrees' / Path(page).with_suffix('.doctree')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pickle.dumps(tree))
    make('index.html', [('start', 'Kafka operations', ['The cleaner is enabled by default.']),
                        ('compact', 'Enable compaction', ['Set cleanup.policy to compact.', 'This affects all topics.',
                         'cleanup.policy=compact', 'Setting compact', 'Included snippet content.', 'Related limitsExternal']),
                        ('navigation', 'See also', ['Other page'])])
    make('docs/other.html', [('limits', 'Limits', ['Compaction requires a keyed record.'])])


class MockProvider:
    def __init__(self):
        self.calls = []
        self.fail_code = None
        self.bad_generation = False
        self.fail_review = False
        self.explore_links = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append(request)
                if owner.fail_code:
                    self.send_error(owner.fail_code)
                    return
                system = request['messages'][0]['content']
                payload = json.loads(request['messages'][1]['content'])
                if 'Connectivity check' in system:
                    result = {'ok': True}
                elif 'Read this full' in system:
                    result = {'summary': 'Read the complete unit.'}
                elif 'Choose the next action' in system:
                    observation = payload['observation']
                    if owner.explore_links and payload['remaining_steps'] == 3:
                        link = next((x for x in observation.get('links', []) if x['target'] and x['target']['page'] != observation['page']), None)
                        result = {'tool': 'follow_internal_link', 'arguments': {'page': observation['page'], 'href': link['href']},
                                  'decision': 'Read linked prerequisites.'} if link else {'tool': 'generate'}
                    elif owner.explore_links and payload['remaining_steps'] == 2 and observation.get('sections'):
                        result = {'tool': 'read_section', 'arguments': {'section_id': observation['sections'][0]['id'], 'unit': 0},
                                  'decision': 'Read the linked section.'}
                    else:
                        result = {'tool': 'generate', 'arguments': {}, 'decision': 'Ask about the supplied task.'}
                elif 'Independently assess' in system:
                    result = {'checks': {k: True for k in RUBRIC}, 'notes': 'All required claims are supported.'}
                    if owner.fail_review:
                        self.send_error(503)
                        return
                else:
                    sid = payload['target_section']
                    text = payload['evidence'][0]['text']
                    primary = payload['evidence'][0]
                    mapping = next((m for m in primary.get('source_mappings', [])
                                    if m['rendered_start'] >= primary['start'] and m['rendered_end'] <= primary['end']), None)
                    if mapping:
                        text = text[mapping['rendered_start'] - primary['start']:mapping['rendered_end'] - primary['start']]
                    headings = payload['evidence'][0].get('headings') or [sid]
                    result = raw_candidate(sid, text, 'How should I handle ' + headings[-1] + '?')
                    other = next((e for e in payload['evidence'] if e['page'] != payload['evidence'][0]['page']), None)
                    if owner.explore_links and other:
                        result['question_type'] = 'cross_page'
                        result['reference_answer'] += ' ' + other['text']
                        result['required_facts'].append(other['text'])
                        result['evidence_groups'].append({'fact_indices': [1], 'alternatives': [
                            {'section_id': other['section_id'], 'excerpt': other['text']}]})
                content = '{invalid' if owner.bad_generation and 'Create one realistic' in system else json.dumps(result)
                data = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': content}}],
                                   'usage': {'prompt_tokens': 50, 'completion_tokens': 30}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {'base_url': 'http://127.0.0.1:' + str(self.server.server_port) + '/v1',
                       'generator_model': 'generator', 'reviewer_model': 'reviewer',
                       'parameters': {'max_tokens': 1000}, 'retries': 1, 'agent_steps': 1}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def raw_candidate(sid, excerpt, question='How can I enable compaction?'):
    return {'question': question, 'reference_answer': excerpt, 'required_facts': [excerpt],
            'persona': 'application_engineer', 'question_type': 'procedural', 'difficulty': 'easy',
            'evidence_groups': [{'fact_indices': [0], 'alternatives': [{'section_id': sid, 'excerpt': excerpt}]}],
            'related_links': []}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.build = self.root / 'repo', self.root / 'build'
        (self.repo / 'docs').mkdir(parents=True)
        (self.build / 'docs').mkdir(parents=True)
        (self.repo / 'index.rst').write_text(RST)
        (self.repo / 'docs/other.rst').write_text('Limits\n======\n\nCompaction requires a keyed record.\n')
        (self.repo / '_toc.yml').write_text('root: index\nentries:\n - file: docs/other\n')
        (self.build / 'index.html').write_text(PAGE, encoding='utf-8')
        # Fixture's relative links are valid from index.html.
        (self.build / 'docs/other.html').write_text(OTHER, encoding='utf-8')
        write_fixture_tree(self.repo, self.build)
        self.state = inventory(self.repo, self.build, build=False)
        self.store = Store(self.root / 'run.sqlite3')
        self.store.initialize(self.state)
        self.provider = MockProvider()

    def tearDown(self):
        self.provider.close()
        self.temp.cleanup()

    def candidate(self, sid='index.html#compact', question='How can I enable compaction?'):
        for section in self.state['sections'].values():
            for unit in section['units']:
                unit['read'] = True
        text = self.state['sections'][sid]['text']
        c = create_candidate(raw_candidate(sid, text, question), self.state, sid, self.provider.config)
        self.assertEqual(validate(c, self.state), [])
        c.update(status='accepted')
        c['validation'] = {'deterministic_errors': []}
        self.state['candidates'].append(c)
        return c

    def test_extraction_preserves_content_and_ownership(self):
        sections = self.state['sections']
        self.assertEqual(sections['index.html#start']['text'], 'The cleaner is enabled by default.')
        text = sections['index.html#compact']['text']
        for snippet in ('cleanup.policy=compact', 'This affects all topics.', 'Setting compact', 'Included snippet content.'):
            self.assertIn(snippet, text)
        self.assertNotIn('Navigation noise', text)
        self.assertNotIn('¶', text)
        self.assertFalse(sections['index.html#navigation']['substantive'])
        self.assertEqual(sections['index.html#compact']['headings'], ['Kafka operations', 'Enable compaction'])

    def test_long_section_units_and_missing_page(self):
        path = self.build / 'index.html'
        path.write_text('<article><section id="long"><h1>Long</h1><p>' + 'word ' * 4000 + '</p></section></article>')
        _, sections = extract_page(path, 'index.html')
        units = sections[0]['units']
        self.assertGreater(len(units), 1)
        self.assertEqual(units[0]['start'], 0)
        self.assertEqual(units[-1]['end'], len(sections[0]['text']))
        self.assertTrue(all(a['end'] == b['start'] for a, b in zip(units, units[1:])))
        path.write_text(PAGE, encoding='utf-8')
        (self.build / 'docs/other.html').unlink()
        state = inventory(self.repo, self.build, build=False)
        self.assertEqual(len(state['inventory_errors']), 1)
        self.assertFalse(report(state)['browsing_complete'])

    def test_snapshot_changes_block_resume(self):
        verify_snapshot(self.state)
        (self.repo / 'docs/new.rst').write_text('New')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            verify_snapshot(self.state)

    def test_navigation_accepts_explicit_source_suffix(self):
        (self.repo / '_toc.yml').write_text('root: index\nentries:\n # - file: docs/not-included\n - file: docs/other.rst\n')
        state = inventory(self.repo, self.build, build=False)
        self.assertEqual(state['inventory_errors'], [])
        self.assertEqual(state['navigation'], ['index.html', 'docs/other.html'])
        verify_snapshot(state)

    def test_included_content_points_to_actual_source_file(self):
        (self.repo / 'includes').mkdir()
        include = self.repo / 'includes/snippet.rst'
        include.write_text('Included snippet content.\n')
        (self.repo / 'index.rst').write_text(RST.replace('Included snippet content.', '.. include:: includes/snippet.rst'))
        tree_path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(tree_path.read_bytes())
        for node in tree.traverse(nodes.paragraph):
            if node.astext() == 'Included snippet content.':
                node.source = str(include)
                node.line = 1
        tree_path.write_bytes(pickle.dumps(tree))
        state = inventory(self.repo, self.build, build=False)
        self.assertEqual(state['inventory_errors'], [])
        section = state['sections']['index.html#compact']
        start = section['text'].index('Included snippet content.')
        spans = evidence_sources(section, start, start + len('Included snippet content.'))
        self.assertEqual([s['source_path'] for s in spans], ['includes/snippet.rst'])
        self.assertEqual(spans[0]['line_start'], 1)
        self.assertEqual(spans[0]['excerpt'], 'Included snippet content.')

    def test_literalinclude_uses_code_origin_instead_of_directive_location(self):
        (self.repo / 'code').mkdir()
        code = self.repo / 'code/example.py'
        code.write_text('cleanup.policy=compact\n')
        tree_path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(tree_path.read_bytes())
        for node in tree.traverse(nodes.paragraph):
            if node.astext() == 'cleanup.policy=compact':
                node['source'] = str(code)
        tree_path.write_bytes(pickle.dumps(tree))
        state = inventory(self.repo, self.build, build=False)
        section = state['sections']['index.html#compact']
        start = section['text'].index('cleanup.policy=compact')
        spans = evidence_sources(section, start, start + len('cleanup.policy=compact'))
        self.assertEqual(spans[0]['source_path'], 'code/example.py')
        self.assertEqual(spans[0]['line_start'], 1)

    def test_resolved_link_labels_map_to_the_raw_reference_block(self):
        path = self.build / 'index.html'
        path.write_text(PAGE.replace('The cleaner is enabled by default.', 'The cleaner is enabled by default. Read Connection pooling.'), encoding='utf-8')
        source = (self.repo / 'index.rst').read_text().replace('The cleaner is enabled by default.',
                    'The cleaner is enabled by default. Read :doc:`docs/other`.')
        (self.repo / 'index.rst').write_text(source)
        tree_path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(tree_path.read_bytes())
        first = next(iter(tree.traverse(nodes.paragraph)))
        first.rawsource = 'The cleaner is enabled by default. Read :doc:`docs/other`.'
        first.children = [nodes.Text('The cleaner is enabled by default. Read docs/other.')]
        tree_path.write_bytes(pickle.dumps(tree))
        state = inventory(self.repo, self.build, build=False)
        self.assertEqual(state['inventory_errors'], [])
        section = state['sections']['index.html#start']
        spans = evidence_sources(section, 0, len(section['text']))
        self.assertIn(':doc:`docs/other`', spans[0]['excerpt'])
        self.assertEqual(spans[0]['mapping_method'], 'transformed_block')

    def test_markdown_source_keeps_markup_and_source_coordinates(self):
        path = self.repo / 'docs/tls.md'
        path.write_text('# TLS\n\nUse **TLS** for connections.\n', encoding='utf-8')
        (self.build / 'docs/tls.html').write_text('<article><section id="tls"><h1>TLS</h1><p>Use <strong>TLS</strong> for connections.</p></section></article>')
        tree = new_document(str(path))
        section = nodes.section(ids=['tls'])
        section.source, section.line = str(path), 1
        section += nodes.title('', 'TLS')
        paragraph = nodes.paragraph('Use **TLS** for connections.', 'Use TLS for connections.')
        paragraph.source, paragraph.line = str(path), 3
        section += paragraph
        tree += section
        (self.build / '.doctrees/docs/tls.doctree').write_bytes(pickle.dumps(tree))
        state = inventory(self.repo, self.build, ['docs/tls.html'], build=False)
        self.assertEqual(state['inventory_errors'], [])
        section = state['sections']['docs/tls.html#tls']
        self.assertEqual(section['source_section_id'], 'docs/tls.md::L1')
        spans = evidence_sources(section, 0, len(section['text']))
        self.assertEqual(spans[0]['excerpt'], 'Use **TLS** for connections.')
        self.assertEqual(spans[0]['line_start'], 3)
        self.assertEqual(spans[0]['source_path'], 'docs/tls.md')

    def test_missing_source_mapping_and_legacy_checkpoints_fail_closed(self):
        (self.build / '.doctrees/index.doctree').unlink()
        state = inventory(self.repo, self.build, build=False)
        self.assertTrue(any('Source mapping requires' in e['error'] for e in state['inventory_errors']))
        state['schema_version'] = '1.0'
        with self.assertRaisesRegex(ValueError, 'Legacy HTML-only'):
            verify_snapshot(state)

    def test_evidence_offsets_alternatives_and_duplicate(self):
        c = self.candidate()
        evidence = c['evidence_groups'][0]['alternatives'][0]
        self.assertEqual(evidence['start'], 0)
        self.assertEqual(evidence['end'], len(evidence['excerpt']))
        bad = copy.deepcopy(c)
        bad['evidence_groups'][0]['alternatives'][0]['start'] = 9
        self.assertIn('Evidence offsets do not match excerpt', validate(bad, self.state))
        bad = copy.deepcopy(c)
        bad['id'] = 'different'
        self.assertIn('duplicate_question', validate(bad, self.state))
        c['evidence_groups'][0]['alternatives'].append({'section_id': 'missing#x', 'excerpt': 'wrong'})
        self.assertIn('Unknown section or empty excerpt', validate(c, self.state))

    def test_cross_page_requires_necessary_groups(self):
        c = self.candidate()
        other = self.state['sections']['docs/other.html#limits']
        c['question_type'] = 'cross_page'
        alt = {'section_id': other['id'], 'excerpt': other['text']}
        c['evidence_groups'][0]['alternatives'].append(alt)
        self.assertTrue(any('Cross-page' in e for e in validate(c, self.state)))
        c['evidence_groups'][0]['alternatives'].pop()
        c['required_facts'].append(other['text'])
        c['evidence_groups'].append({'fact_indices': [1], 'alternatives': [alt]})
        self.assertEqual(validate(c, self.state), [])

    def test_complete_mock_run_resume_audit_export(self):
        partial = run(self.store, self.provider.config, max_sections=1)
        self.assertFalse(partial['browsing_complete'])
        finished = run(self.store, self.provider.config)
        self.assertTrue(finished['browsing_complete'])
        self.assertEqual(finished['question_coverage']['rate'], 1)
        self.assertFalse(finished['ready'])
        state = self.store.load()
        calls = len(self.provider.calls)
        run(self.store, self.provider.config)
        self.assertEqual(len(self.provider.calls), calls)
        state = self.store.load()
        select_audit(state)
        for c in accepted(state):
            decide(state, c['id'], 'approve', 'Verified against evidence')
        self.assertTrue(report(state)['ready'])
        export(state, self.root / 'export', ready_only=True)
        manifest = json.loads((self.root / 'export/manifest.json').read_text())
        self.assertEqual(manifest['release_status'], 'audited_ready')
        self.assertEqual(manifest['schema_version'], '2.0')
        records = [json.loads(line) for line in (self.root / 'export/questions.jsonl').read_text().splitlines()]
        substantive = {s['source_section_id'] for s in state['sections'].values() if s['substantive']}
        self.assertEqual({c['target_section'] for c in records}, substantive)
        self.assertEqual(len(records), len({c['id'] for c in records}))
        for record in records:
            for group in record['evidence_groups']:
                for alternative in group['alternatives']:
                    self.assertNotIn('.html', alternative['section_id'])
                    self.assertNotIn('page', alternative)
                    for span in alternative['source_spans']:
                        text = (self.repo / span['source_path']).read_text(encoding='utf-8-sig')
                        self.assertEqual(text[span['start']:span['end']], span['excerpt'])

    def test_budget_failure_and_review_resume(self):
        config = {**self.provider.config, 'max_tokens': 1}
        self.assertEqual(run(self.store, config)['runtime']['status'], 'paused')
        self.assertEqual(len(self.provider.calls), 0)
        self.provider.fail_review = True
        run(self.store, self.provider.config)
        state = self.store.load()
        self.assertEqual(state['candidates'][0]['status'], 'pending_revalidation')
        self.provider.fail_review = False
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'])
        self.assertEqual(len(self.store.load()['candidates']), result['accepted'])

    def test_reasoning_journey_reads_linked_page_and_generates_cross_page_labels(self):
        self.provider.explore_links = True
        config = {**self.provider.config, 'agent_steps': 3}
        result = run(self.store, config)
        self.assertTrue(result['browsing_complete'])
        state = self.store.load()
        self.assertTrue(any(j['tool'] == 'follow_internal_link' for j in state['journey']))
        self.assertTrue(any(c['question_type'] == 'cross_page' for c in accepted(state)))

    def test_malformed_generation_exhausts_three_attempts(self):
        self.provider.bad_generation = True
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'])
        self.assertFalse(result['ready'])
        self.assertTrue(all(g['attempts'] == 3 for g in result['gaps']))
        self.assertEqual(result['accepted'], 0)

    def test_review_edit_requires_revalidation_and_expands_audit(self):
        c = self.candidate()
        select_audit(self.state)
        decide(self.state, c['id'], 'edit', 'Clarify question', {'question': 'Which compaction setting should I use?'})
        self.assertEqual(c['status'], 'pending_revalidation')
        self.assertFalse(audit_result(self.state)['passed'])
        with self.assertRaisesRegex(ValueError, 'automatically accepted'):
            decide(self.state, c['id'], 'approve')
        self.assertEqual(self.state['audit']['expanded_categories'], [[c['product'], c['question_type']]])
        client = Client(self.provider.config, self.state, lambda: None)
        self.assertTrue(review_candidate(c, self.state, client))
        decide(self.state, c['id'], 'approve')
        self.assertEqual(c['human_status'], 'approved')
        self.assertEqual(audit_result(self.state)['initial_pass_rate']['rate'], 0)

    def test_audit_sample_is_reproducible_and_frozen(self):
        c = self.candidate()
        for i in range(2100):
            clone = copy.deepcopy(c)
            clone['id'] = str(i)
            clone['difficulty'] = ('easy', 'medium', 'hard')[i % 3]
            clone['product'] = ('kafka', 'postgresql')[i % 2]
            self.state['candidates'].append(clone)
        other = copy.deepcopy(self.state)
        a, b = select_audit(self.state), select_audit(other)
        self.assertEqual(a['members'], b['members'])
        self.assertEqual(len(a['members']), 106)
        self.assertEqual(len(a['strata']), 7)
        with self.assertRaises(ValueError):
            select_audit(self.state)

    def test_failed_audit_round_requires_remediation_and_retains_history(self):
        c = self.candidate()
        select_audit(self.state)
        with self.assertRaisesRegex(ValueError, 'Resolve all'):
            select_audit(self.state, new_round=True)
        decide(self.state, c['id'], 'edit', 'Improve wording', {'question': 'Which setting enables topic compaction?'})
        client = Client(self.provider.config, self.state, lambda: None)
        review_candidate(c, self.state, client)
        decide(self.state, c['id'], 'approve')
        self.assertFalse(audit_result(self.state)['passed'])
        select_audit(self.state, new_round=True)
        self.assertEqual(len(self.state['audit_history']), 1)
        self.assertEqual(c['human_status'], 'pending')
        decide(self.state, c['id'], 'approve')
        self.assertTrue(audit_result(self.state)['passed'])

    def test_invalid_schema_cannot_crash_validation(self):
        sid = 'index.html#compact'
        for raw in ({}, {'question': 12, 'evidence_groups': 'oops'},
                    {**raw_candidate(sid, 'text'), 'evidence_groups': [{'fact_indices': [], 'alternatives': [{'section_id': []}]}]},
                    {**raw_candidate(sid, 'text'), 'question_type': 'cross_page', 'evidence_groups': [{'alternatives': 12}]}):
            c = create_candidate(raw, self.state, sid, self.provider.config)
            self.assertTrue(validate(c, self.state))

    def test_retry_exhausted_gaps_and_http_failure(self):
        self.provider.fail_code = 401
        result = run(self.store, self.provider.config)
        self.assertEqual(result['runtime']['status'], 'paused')
        self.assertIn('http_401', result['runtime']['error'])
        self.provider.fail_code = None
        self.provider.bad_generation = True
        run(self.store, self.provider.config)
        self.provider.bad_generation = False
        result = run(self.store, self.provider.config, retry_gaps=True)
        self.assertEqual(result['question_coverage']['rate'], 1)
        self.assertTrue(result['browsing_complete'])

    def test_browser_boundaries_and_no_read_before_consumption(self):
        browser = Browser(self.state, lambda: None)
        browser.tool('read_section', {'section_id': 'index.html#compact'})
        self.assertFalse(self.state['sections']['index.html#compact']['units'][0]['read'])
        browser.mark_consumed()
        self.assertTrue(self.state['sections']['index.html#compact']['units'][0]['read'])
        with self.assertRaisesRegex(ValueError, 'External'):
            browser.tool('follow_internal_link', {'page': 'index.html', 'href': 'https://example.org/'})
        with self.assertRaises(ValueError):
            browser.tool('open_page', {'page': '../secret.html'})
        self.assertTrue(browser.tool('search_snapshot', {'query': 'cleaner'})['matches'])

    def test_local_review_routes_and_security(self):
        c = self.candidate()
        self.store.save(self.state)
        server = make_server(self.store, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urlopen(base) as response:
                html = response.read().decode()
                self.assertIn('Documentation benchmark review', html)
            token = html.split("const token='")[1].split("'")[0]
            with urlopen(base + '/api/candidates/' + c['id']) as response:
                self.assertIn(c['question'], response.read().decode())
            request = Request(base + '/api/decision', data=json.dumps({'id': c['id'], 'action': 'approve'}).encode(),
                              headers={'Content-Type': 'application/json', 'X-Review-Token': token})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.store.load()['candidates'][0]['human_status'], 'approved')
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + '/api/audit', data=b'{}'))
            self.assertEqual(error.exception.code, 403)
            with self.assertRaises(HTTPError):
                urlopen(base + '/../run.sqlite3')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_config_and_json_errors(self):
        path = self.root / '.env'
        path.write_text('GOLDEN_BASE_URL=http://localhost:8001/v1\nGOLDEN_GENERATOR_MODEL=gen\nGOLDEN_REVIEWER_MODEL=judge\nGOLDEN_MAX_COST=1\n')
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'pricing'):
                load_config(path)
        with self.assertRaises(InvalidOutput):
            parse_json('hello')
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'), {'ok': True})
        with self.assertRaises(InvalidOutput):
            parse_json('[]')

    def test_env_configuration_overrides_defaults_and_keeps_credentials_private(self):
        path = self.root / '.env'
        path.write_text('''# Local model configuration
GOLDEN_BASE_URL="http://localhost:8001/v1"
GOLDEN_GENERATOR_MODEL=gen
GOLDEN_REVIEWER_MODEL=judge
GOLDEN_API_KEY='private-${LITERAL}-key'
GOLDEN_MAX_TOKENS=12000
GOLDEN_MAX_COST=
GOLDEN_PARAMETERS='{"max_completion_tokens":1024,"reasoning_effort":"low"}'
''')
        with patch.dict(os.environ, {'GOLDEN_GENERATOR_MODEL': 'override'}, clear=True):
            config = load_config(path)
            self.assertEqual(config['generator_model'], 'override')
            self.assertEqual(config['reviewer_model'], 'judge')
            self.assertEqual(config['max_tokens'], 12000)
            self.assertIsNone(config['max_cost'])
            self.assertEqual(config['parameters']['max_completion_tokens'], 1024)
            self.assertNotIn('max_tokens', config['parameters'])
            self.assertEqual(config.api_key, 'private-${LITERAL}-key')
            self.assertNotIn('private-', json.dumps(config))
            self.assertNotIn('GOLDEN_API_KEY', os.environ)
            before = digest(config)
            config.api_key = 'different-private-key'
            self.assertEqual(digest(config), before)

    def test_env_numeric_defaults_pricing_and_errors(self):
        path = self.root / '.env'
        base = 'GOLDEN_BASE_URL=http://localhost:8001/v1\nGOLDEN_GENERATOR_MODEL=gen\nGOLDEN_REVIEWER_MODEL=judge\n'
        with patch.dict(os.environ, {}, clear=True):
            path.write_text(base)
            config = load_config(path)
            self.assertEqual(config['parameters']['max_tokens'], 4096)
            path.write_text(base + 'GOLDEN_GENERATOR_INPUT_PER_MILLION=1\nGOLDEN_GENERATOR_OUTPUT_PER_MILLION=4\nGOLDEN_REVIEWER_INPUT_PER_MILLION=2\nGOLDEN_REVIEWER_OUTPUT_PER_MILLION=6\nGOLDEN_MAX_COST=20\n')
            config = load_config(path)
            self.assertEqual(config['pricing']['judge']['output_per_million'], 6)
            for extra, message in [('GOLDEN_RETRIES=nope', 'GOLDEN_RETRIES'), ('GOLDEN_PARAMETERS=[]', 'JSON object')]:
                path.write_text(base + extra)
                with self.assertRaisesRegex(ValueError, message):
                    load_config(path)
            with self.assertRaisesRegex(ValueError, 'not found'):
                load_config(self.root / 'missing.env')


if __name__ == '__main__':
    unittest.main()
