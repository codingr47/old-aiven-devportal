"""Integration tests use actual headless Chromium and a local compatible API."""

import copy
import json
import os
import pickle
import re
import tempfile
import threading
import unittest
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from docutils import nodes
from docutils.utils import new_document

from tools.golden_dataset.browser import Browser
from tools.golden_dataset.engine import run
from tools.golden_dataset.model import Client, Config, InvalidOutput, Paused, load_config, parse_json
from tools.golden_dataset.reporting import accepted, audit_result, export, report, select_audit
from tools.golden_dataset.review import decide, make_server
from tools.golden_dataset.snapshot import UNIT_CHARS, digest, inventory, verify_snapshot
from tools.golden_dataset.storage import Store
from tools.golden_dataset.validation import RUBRIC, create_candidate, review_candidate, validate

RST = '''Kafka operations
================

The cleaner is enabled by default.

.. SOURCE_ONLY_CANARY must never enter generator observations.

Enable compaction
-----------------

Set cleanup.policy to compact.
This affects all topics.

See also
--------

`Other page <docs/other.rst>`_
'''
PAGE = '''<!doctype html><html><body><nav>
<button aria-expanded="false" onclick="this.setAttribute('aria-expanded','true');document.getElementById('menu').hidden=false">Documentation</button>
<div id="menu" hidden><a href="docs/other.html">Limits</a></div><a href="search.html">Search</a>
</nav><main><article><section id="start"><h1>Kafka operations</h1><p>The cleaner is enabled by default.</p>
<section id="compact"><h2>Enable compaction</h2><p>Set cleanup.policy to compact.</p><p>This affects all topics.</p></section>
<section id="navigation"><h2>See also</h2><a href="docs/other.html">Other page</a></section>
</section></article></main></body></html>'''
OTHER = '<main><article><section id="limits"><h1>Limits</h1><p>Compaction requires a keyed record.</p></section></article></main>'
SEARCH = '''<main><h1>Search</h1><label>Query<input aria-label="Query" id="q"></label>
<button onclick="document.getElementById('result').hidden=false">Find</button>
<div id="result" hidden><a href="docs/orphan.html">Unlinked guide</a></div></main>'''


def tree_for(repo, build, path, definitions):
    source = str(repo / path)
    text = Path(source).read_text(encoding='utf-8')
    tree = new_document(source)
    parent = tree
    heading_cursor = 0
    for i, (anchor, title, paragraphs) in enumerate(definitions):
        section = nodes.section(ids=[anchor])
        section.source = source
        heading_offset = text.index(title, heading_cursor)
        heading_cursor = heading_offset + len(title)
        section.line = text[:heading_offset].count('\n') + (1 if path.endswith('.md') else 2)
        section += nodes.title('', title)
        if i == 0:
            tree += section
            parent = section
        else:
            parent += section
        for raw, visible in [(p, p) if isinstance(p, str) else p for p in paragraphs]:
            paragraph = nodes.paragraph(raw, visible)
            paragraph.source = source
            paragraph.line = text[:text.find(raw)].count('\n') + 1 if raw in text else 1
            section += paragraph
    target = build / '.doctrees' / Path(path).with_suffix('.doctree')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pickle.dumps(tree))


def start_server(server):
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    return thread


def raw_candidate(sid, observation_id, excerpt, question='How can I enable compaction?'):
    return {'target_section': sid, 'question': question, 'reference_answer': excerpt,
            'required_facts': [excerpt], 'persona': 'application_engineer', 'question_type': 'procedural',
            'difficulty': 'easy', 'evidence_groups': [{'fact_indices': [0], 'alternatives': [
                {'observation_id': observation_id, 'excerpt': excerpt}]}], 'related_links': []}


class MockProvider:
    def __init__(self):
        self.calls, self.seen, self.asked = [], set(), set()
        self.fail_code = self.fail_review = None
        self.bad_generation = self.force_finish = self.cross_page = False
        self.navigation_policy = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append(request)
                task = request['messages'][0]['content']
                payload = json.loads(request['messages'][1]['content'])
                if owner.fail_code or (owner.fail_review and 'Independently assess' in task):
                    self.send_error(owner.fail_code or 503)
                    return
                bad = False
                if 'Connectivity check' in task:
                    result = {'ok': True}
                elif 'Independently assess' in task:
                    result = {'checks': {key: True for key in RUBRIC}, 'notes': 'Synthetic review: sources support the supplied claims.'}
                else:
                    view = payload['browser']
                    owner.seen.add(view['url'].split('#')[0])
                    custom = owner.navigation_policy(view, payload) if owner.navigation_policy else None
                    if custom:
                        result = custom
                    elif owner.force_finish:
                        result = {'tool': 'finish'}
                    elif view['scope'] == 'page':
                        result = {'tool': 'observe', 'arguments': {'scope': 'main'}}
                    elif view.get('question_required'):
                        sid = view['section_id']
                        excerpt = re.split(r'(?<=[.!?])\s', view['text'])[0]
                        candidate = raw_candidate(sid, view['observation_id'], excerpt,
                                                  'What should I do about ' + view['heading_path'][-1] + '?')
                        other = next((m for m in payload.get('memory', []) if m['url'].split('#')[0] != view['url'].split('#')[0]), None)
                        if owner.cross_page and other:
                            extra = re.split(r'(?<=[.!?])\s', other['text'])[0]
                            candidate['question_type'] = 'cross_page'
                            candidate['reference_answer'] += ' ' + extra
                            candidate['required_facts'].append(extra)
                            candidate['evidence_groups'].append({'fact_indices': [1], 'alternatives': [
                                {'observation_id': other['observation_id'], 'excerpt': extra}]})
                        result = {'tool': 'draft_question', 'arguments': candidate}
                        bad = owner.bad_generation
                    elif view.get('remaining_chunks', 0) > 1:
                        result = {'tool': 'observe', 'arguments': {'scope': 'main'}}
                    else:
                        link = next((c for c in view['controls'] if c.get('url', '').split('#')[0] not in owner.seen and 'url' in c), None)
                        if link:
                            result = {'tool': 'click', 'arguments': {'ref': link['ref']}}
                        elif view.get('frontier'):
                            result = {'tool': 'navigate', 'arguments': {'url': view['frontier'][0]['url']}}
                        else:
                            result = {'tool': 'finish'}
                data = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': '{broken' if bad else json.dumps(result)}}],
                                   'usage': {'prompt_tokens': 50, 'completion_tokens': 30}}).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = start_server(self.server)
        self.config = Config(base_url='http://127.0.0.1:' + str(self.server.server_port) + '/v1',
                             generator_model='generator', reviewer_model='reviewer', parameters={'max_tokens': 1000},
                             retries=1, headless=True, browser_timeout_seconds=2, browser_max_actions=40)
        self.config.api_key = 'synthetic-local-key'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repo, self.build = self.root / 'repo', self.root / 'build'
        (self.repo / 'docs').mkdir(parents=True)
        (self.build / 'docs').mkdir(parents=True)
        (self.repo / 'index.rst').write_text(RST, encoding='utf-8')
        (self.repo / 'docs/other.rst').write_text('Limits\n======\n\nCompaction requires a keyed record.\n')
        (self.repo / 'docs/orphan.rst').write_text('Unlinked guide\n==============\n\nUse a dedicated topic.\n')
        (self.repo / '_toc.yml').write_text('root: index\nentries:\n - file: docs/other\n')
        (self.build / 'index.html').write_text(PAGE, encoding='utf-8')
        (self.build / 'docs/other.html').write_text(OTHER, encoding='utf-8')
        (self.build / 'docs/orphan.html').write_text('<main><article><section id="unlinked"><h1>Unlinked guide</h1><p>Use a dedicated topic.</p></section></article></main>')
        (self.build / 'search.html').write_text(SEARCH)
        tree_for(self.repo, self.build, 'index.rst', [('start', 'Kafka operations', ['The cleaner is enabled by default.']),
                 ('compact', 'Enable compaction', ['Set cleanup.policy to compact.', 'This affects all topics.']),
                 ('navigation', 'See also', [('`Other page <docs/other.rst>`_', 'Other page')])])
        tree_for(self.repo, self.build, 'docs/other.rst', [('limits', 'Limits', ['Compaction requires a keyed record.'])])
        tree_for(self.repo, self.build, 'docs/orphan.rst', [('unlinked', 'Unlinked guide', ['Use a dedicated topic.'])])

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *_):
                pass
        self.app = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(self.build)))
        self.app_thread = start_server(self.app)
        self.provider = MockProvider()
        self.provider.config['docs_url'] = 'http://127.0.0.1:' + str(self.app.server_port)
        self.reset_inventory()

    def reset_inventory(self, selection=None):
        self.state = inventory(self.repo, self.build, selection, build=False)
        self.assertEqual(self.state['inventory_errors'], [])
        self.store = Store(self.root / ('run-' + str(len(list(self.root.glob('*.sqlite3')))) + '.sqlite3'))
        self.store.initialize(self.state)

    def tearDown(self):
        self.provider.close()
        self.app.shutdown()
        self.app.server_close()
        self.app_thread.join()
        self.temp.cleanup()

    def read_root(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            for _ in range(10):
                browser.consume(view)
                if all(u['read'] for s in self.state['sections'].values() if s['page'] == 'index.html' for u in s['units']):
                    break
                view = browser.observe()

    def candidate(self, sid='index.html#compact', question='How can I enable compaction?', excerpt=None):
        self.read_root()
        obs = next(o for o in self.state['observations'].values() if o['section_id'] == sid)
        excerpt = excerpt or re.split(r'(?<=[.!?])\s', obs['text'])[0]
        self.state['sections'][sid]['attempts'] += 1
        candidate = create_candidate(raw_candidate(sid, obs['id'], excerpt, question), self.state, sid, self.provider.config)
        self.state['candidates'].append(candidate)
        client = Client(self.provider.config, self.state, lambda: None)
        self.assertTrue(review_candidate(candidate, self.state, client), candidate.get('validation'))
        return candidate

    def test_inventory_is_metadata_only_and_excludes_orphans(self):
        self.assertEqual(self.state['schema_version'], '3.0')
        self.assertEqual(self.state['expected_pages'], ['docs/other.html', 'index.html'])
        self.assertEqual(self.state['coverage_audit']['excluded'], ['docs/orphan.html'])
        self.assertNotIn('SOURCE_ONLY_CANARY', json.dumps(self.state))
        self.assertTrue(all(s['text'] == '' and s['source'] is None for s in self.state['sections'].values()))

    def test_generated_table_of_contents_links_prevent_false_orphan_exclusions(self):
        html = PAGE.replace('</article>', '<div class="toctree-wrapper"><a href="docs/orphan.html">Unlinked guide</a></div></article>')
        (self.build / 'index.html').write_text(html)
        self.reset_inventory()
        self.assertIn('docs/orphan.html', self.state['expected_pages'])
        self.assertNotIn('docs/orphan.html', self.state['coverage_audit']['orphans'])
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            link = next(c for c in view['controls'] if c['name'] == 'Unlinked guide')
            browser.tool('click', {'ref': link['ref']})
            self.assertEqual(browser.page_id(), 'docs/orphan.html')

    def test_real_browser_navigation_controls_and_boundary(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe('page')
            self.assertFalse(any(c['name'] == 'Limits' for c in view['controls']))
            with self.assertRaisesRegex(ValueError, 'not been discovered'):
                browser.tool('navigate', {'url': self.provider.config['docs_url'] + '/docs/orphan.html'})
            menu = next(c for c in view['controls'] if c['name'] == 'Documentation')
            browser.tool('click', {'ref': menu['ref']})
            view = browser.observe('page')
            limits = next(c for c in view['controls'] if c['name'] == 'Limits')
            browser.tool('click', {'ref': limits['ref']})
            self.assertEqual(browser.page_id(), 'docs/other.html')
            browser.tool('back', {})
            self.assertEqual(browser.page_id(), 'index.html')
            for url in ('https://example.org/', self.provider.config['docs_url'] + '/_sources/index.rst.txt'):
                with self.assertRaises(ValueError):
                    browser.tool('navigate', {'url': url})

    def test_real_search_typing_and_orphan_promotion(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe('page')
            search = next(c for c in view['controls'] if c['name'] == 'Search')
            view = browser.tool('click', {'ref': search['ref']})
            query = next(c for c in view['controls'] if c['role'] == 'textbox')
            view = browser.tool('type', {'ref': query['ref'], 'text': 'dedicated topic'})
            button = next(c for c in view['controls'] if c['name'] == 'Find')
            view = browser.tool('click', {'ref': button['ref']})
            result = next(c for c in view['controls'] if c['name'] == 'Unlinked guide')
            view = browser.tool('click', {'ref': result['ref']})
            browser.consume(view)
            self.assertIn('docs/orphan.html', self.state['expected_pages'])
            self.assertIn('docs/orphan.html', self.state['coverage_audit']['promoted'])
            browser.tool('keypress', {'key': 'PageDown'})
            browser.tool('scroll', {'pixels': 200})

    def test_search_request_failure_is_recorded_without_filesystem_fallback(self):
        (self.build / 'search.html').write_text(SEARCH.replace("document.getElementById('result').hidden=false", "fetch('/missing-search-api')"))
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe('page')
            search = next(c for c in view['controls'] if c['name'] == 'Search')
            view = browser.tool('click', {'ref': search['ref']})
            button = next(c for c in view['controls'] if c['name'] == 'Find')
            browser.tool('click', {'ref': button['ref']})
            browser.page.wait_for_timeout(100)
            self.assertTrue(self.state['browser']['search_failures'])
            self.assertTrue(browser.observe('page')['search_status']['failed'])
            with self.assertRaises(ValueError):
                browser.tool('search_snapshot', {'query': 'secret'})

    def test_hidden_tabs_and_accordions_require_browser_interaction(self):
        controls = '''<button role="tab" aria-selected="true">Basic</button>
<button role="tab" aria-selected="false" onclick="this.setAttribute('aria-selected','true');document.getElementById('advanced').hidden=false">Advanced</button>
<p id="advanced" hidden>Records require keys.</p>
<button aria-expanded="false" onclick="this.setAttribute('aria-expanded','true');document.getElementById('details').hidden=false">Details</button>
<p id="details" hidden>Retention can delay deletion.</p>'''
        (self.build / 'index.html').write_text(PAGE.replace('<p>This affects all topics.</p>', '<p>This affects all topics.</p>' + controls))
        (self.repo / 'index.rst').write_text(RST.replace('This affects all topics.', 'This affects all topics.\nRecords require keys.\nRetention can delay deletion.'))
        self.reset_inventory()
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            browser.consume(view)
            self.assertNotIn('Records require keys.', json.dumps(self.state['observations']))
            self.assertTrue(report(self.state)['unresolved_controls'])
            tab = next(c for c in view['controls'] if c['name'] == 'Advanced')
            view = browser.tool('click', {'ref': tab['ref']})
            self.assertIn('Records require keys.', json.dumps(self.state['observations']))
            detail = next(c for c in view['controls'] if c['name'] == 'Details')
            browser.tool('click', {'ref': detail['ref']})
            self.assertIn('Retention can delay deletion.', json.dumps(self.state['observations']))
            self.assertFalse(report(self.state)['browsing_complete'])

    def test_stale_reference_refreshes_observation_and_disallows_arbitrary_code(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            link = next(c for c in view['controls'] if c['name'] == 'Other page')
            browser.page.get_by_role('link', name='Other page').evaluate('(element) => element.remove()')
            fresh = browser.tool('click', {'ref': link['ref']})
            self.assertIn('action_error', fresh)
            self.assertEqual(fresh['scope'], 'page')
            with self.assertRaises(ValueError):
                browser.tool('evaluate', {'code': 'document.body.innerText'})

    def test_clickable_generic_navigation_label_is_supported(self):
        toggle = '''<input id="nav-toggle" type="checkbox" style="display:none">
<label for="nav-toggle" style="cursor:pointer;display:block;width:30px;height:30px"
 onclick="document.getElementById('menu').hidden=false"><div style="position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;overflow:hidden">Documentation</div><i aria-hidden="true">+</i></label>'''
        html = re.sub(r'<button.*?>Documentation</button>', toggle, PAGE, count=1, flags=re.S)
        (self.build / 'index.html').write_text(html)
        self.reset_inventory()
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe('page')
            self.assertTrue(any(c['role'] == 'generic' and 'Documentation' in c['name'] for c in view['controls']), view['snapshot'])
            toggle = next(c for c in view['controls'] if c['role'] == 'generic' and 'Documentation' in c['name'])
            browser.tool('click', {'ref': toggle['ref']})
            view = browser.observe('page')
            self.assertTrue(any(c['name'] == 'Limits' for c in view['controls']))

    def test_only_consumed_browser_observations_count_as_read(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            self.assertFalse(any(u['read'] for s in self.state['sections'].values() for u in s['units']))
            browser.consume(view)
            self.assertTrue(any(u['read'] for s in self.state['sections'].values() for u in s['units']))
            self.assertFalse(report(self.state)['browsing_complete'])

    def test_app_chrome_before_first_heading_is_not_document_evidence(self):
        (self.build / 'index.html').write_text(PAGE.replace('<main>', '<main><div>Toggle table of contents sidebar</div>'))
        self.reset_inventory()
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            self.assertTrue(view['text'].startswith('The cleaner is enabled by default.'))
            self.assertNotIn('Toggle table of contents sidebar', view['text'])

    def test_long_observations_are_not_silently_truncated(self):
        text = 'Long documentation content. ' * 500
        (self.repo / 'index.rst').write_text('Kafka operations\n================\n\n' + text)
        (self.build / 'index.html').write_text('<main><article><section id="start"><h1>Kafka operations</h1><p>' + text + '</p></section></article></main>')
        tree_for(self.repo, self.build, 'index.rst', [('start', 'Kafka operations', [text])])
        self.reset_inventory(['index.html'])
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            self.assertGreater(view['chunks'], 1)
            self.assertLessEqual(len(view['text']), UNIT_CHARS)
            browser.consume(view)
            self.assertFalse(report(self.state)['browsing_complete'])
            for _ in range(view['chunks']):
                view = browser.observe()
                browser.consume(view)
            self.assertTrue(report(self.state)['browsing_complete'])

    def test_repeated_accessible_headings_keep_distinct_source_sections(self):
        (self.repo / 'index.rst').write_text('Kafka operations\n================\n\nOverview\n--------\n\nFirst topic.\n\nOverview\n--------\n\nSecond topic.\n')
        (self.build / 'index.html').write_text('<main><article><section id="start"><h1>Kafka operations</h1><section id="first"><h2>Overview</h2><p>First topic.</p></section><section id="second"><h2>Overview</h2><p>Second topic.</p></section></section></article></main>')
        tree_for(self.repo, self.build, 'index.rst', [('start', 'Kafka operations', []), ('first', 'Overview', ['First topic.']), ('second', 'Overview', ['Second topic.'])])
        self.reset_inventory(['index.html'])
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            browser.consume(view)
            view = browser.observe()
            browser.consume(view)
            first = next(o for o in self.state['observations'].values() if o['section_id'] == 'index.html#first')
            second = next(o for o in self.state['observations'].values() if o['section_id'] == 'index.html#second')
            self.assertEqual(first['text'], 'First topic.')
            self.assertEqual(second['text'], 'Second topic.')
            candidate = create_candidate(raw_candidate('index.html#second', second['id'], 'Second topic.'), self.state, 'index.html#second', self.provider.config)
            self.assertTrue(review_candidate(candidate, self.state, Client(self.provider.config, self.state, lambda: None)))
            self.assertEqual(self.state['sections']['index.html#second']['source_section_id'], 'index.rst::L9')

    def test_complete_real_browser_workflow_resume_audit_export(self):
        partial = run(self.store, self.provider.config, max_sections=1)
        self.assertEqual(partial['runtime']['status'], 'paused')
        self.assertEqual(partial['accepted'], 1)
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'], result)
        self.assertEqual(result['question_coverage']['rate'], 1)
        self.assertFalse(result['ready'])
        count = len(self.provider.calls)
        run(self.store, self.provider.config)
        self.assertEqual(len(self.provider.calls), count)
        state = self.store.load()
        select_audit(state)
        for c in accepted(state):
            decide(state, c['id'], 'approve', 'Synthetic pipeline test')
        result = export(state, self.root / 'export', ready_only=True)
        self.assertTrue(result['ready'])
        manifest = json.loads((self.root / 'export/manifest.json').read_text())
        self.assertEqual(manifest['schema_version'], '3.0')
        records = [json.loads(line) for line in (self.root / 'export/questions.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), len({c['id'] for c in records}))
        for c in records:
            self.assertNotIn('.html', c['target_section'])
            for group in c['evidence_groups']:
                for e in group['alternatives']:
                    self.assertIn(e['observation_id'], state['observations'])
                    for span in e['source_spans']:
                        text = (self.repo / span['source_path']).read_text(encoding='utf-8-sig')
                        self.assertEqual(text[span['start']:span['end']], span['excerpt'])

    def test_generator_never_receives_source_or_inventory_prose(self):
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'])
        for request in self.provider.calls:
            if request['model'] == 'generator':
                data = json.dumps(request)
                self.assertNotIn('SOURCE_ONLY_CANARY', data)
                self.assertNotIn('source_mappings', data)
                self.assertNotIn('source_spans', data)
                self.assertNotIn('rendered_blocks', data)
        self.assertTrue(all(c['verification_stage'] == 'reviewed' for c in accepted(self.store.load())))

    def test_early_finish_cannot_override_coverage(self):
        self.provider.force_finish = True
        config = {**self.provider.config, 'browser_max_actions': 3}
        result = run(self.store, config)
        self.assertEqual(result['runtime']['status'], 'paused')
        self.assertFalse(result['browsing_complete'])
        self.assertFalse(result['ready'])

    def test_budget_failures_and_pending_review_resume(self):
        config = {**self.provider.config, 'max_tokens': 1}
        self.assertEqual(run(self.store, config)['runtime']['status'], 'paused')
        self.assertEqual(self.provider.calls, [])
        self.provider.fail_review = True
        result = run(self.store, self.provider.config)
        self.assertEqual(result['runtime']['status'], 'paused')
        self.assertEqual(self.store.load()['candidates'][0]['status'], 'pending_revalidation')
        self.provider.fail_review = False
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'], result)
        self.assertEqual(len(self.store.load()['candidates']), result['accepted'])

    def test_bad_generation_three_attempts_and_explicit_retry(self):
        self.provider.bad_generation = True
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'], result)
        self.assertTrue(all(g['attempts'] == 3 for g in result['gaps']))
        self.provider.bad_generation = False
        result = run(self.store, self.provider.config, retry_gaps=True)
        self.assertEqual(result['question_coverage']['rate'], 1)

    def test_config_section_limit_and_cli_override(self):
        config = {**self.provider.config, 'max_sections': 1}
        self.assertEqual(run(self.store, config)['accepted'], 1)
        result = run(self.store, config, max_sections=10)
        self.assertTrue(result['browsing_complete'], result)

    def test_browser_quote_offsets_sources_and_duplicates(self):
        c = self.candidate()
        self.assertEqual(validate(c, self.state), [])
        bad = copy.deepcopy(c)
        bad['evidence_groups'][0]['alternatives'][0]['start'] = 9
        self.assertIn('Browser quote offsets do not match', validate(bad, self.state))
        bad = copy.deepcopy(c)
        bad['id'] = 'another'
        self.assertIn('duplicate_question', validate(bad, self.state))
        bad = copy.deepcopy(c)
        bad['evidence_groups'][0]['alternatives'][0]['source_spans'][0]['line_start'] = 999
        self.assertIn('Invalid source offsets or lines', validate(bad, self.state))

    def test_rst_include_points_to_actual_included_file(self):
        (self.repo / 'includes').mkdir()
        include = self.repo / 'includes/snippet.rst'
        include.write_text('Set cleanup.policy to compact.\n')
        (self.repo / 'index.rst').write_text(RST.replace('Set cleanup.policy to compact.', '.. include:: includes/snippet.rst'))
        path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(path.read_bytes())
        for node in tree.traverse(nodes.paragraph):
            if node.astext() == 'Set cleanup.policy to compact.':
                node.source, node.line = str(include), 1
        path.write_bytes(pickle.dumps(tree))
        self.reset_inventory()
        c = self.candidate()
        span = c['evidence_groups'][0]['alternatives'][0]['source_spans'][0]
        self.assertEqual(span['source_path'], 'includes/snippet.rst')
        self.assertEqual(span['line_start'], 1)

    def test_literalinclude_origin_overrides_directive_file(self):
        (self.repo / 'code').mkdir()
        code = self.repo / 'code/example.py'
        code.write_text('Set cleanup.policy to compact.\n')
        path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(path.read_bytes())
        for node in tree.traverse(nodes.paragraph):
            if node.astext() == 'Set cleanup.policy to compact.':
                node['source'] = str(code)
        path.write_bytes(pickle.dumps(tree))
        self.reset_inventory()
        c = self.candidate()
        self.assertEqual(c['evidence_groups'][0]['alternatives'][0]['source_spans'][0]['source_path'], 'code/example.py')

    def test_markdown_source_preserves_raw_markup(self):
        (self.repo / 'docs/tls.md').write_text('# TLS\n\nUse **TLS** for connections.\n')
        (self.build / 'docs/tls.html').write_text('<main><article><section id="tls"><h1>TLS</h1><p>Use <strong>TLS</strong> for connections.</p></section></article></main>')
        tree_for(self.repo, self.build, 'docs/tls.md', [('tls', 'TLS', [('Use **TLS** for connections.', 'Use TLS for connections.')])])
        (self.build / 'index.html').write_text(PAGE.replace('</nav>', '<a href="docs/tls.html">TLS guide</a></nav>'))
        self.reset_inventory(['docs/tls.html'])
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe('page')
            link = next(c for c in view['controls'] if c['name'] == 'TLS guide')
            view = browser.tool('click', {'ref': link['ref']})
            browser.consume(view)
            c = create_candidate(raw_candidate(view['section_id'], view['observation_id'], view['text']), self.state, view['section_id'], self.provider.config)
            self.state['candidates'].append(c)
            self.assertTrue(review_candidate(c, self.state, Client(self.provider.config, self.state, lambda: None)))
            span = c['evidence_groups'][0]['alternatives'][0]['source_spans'][0]
            self.assertEqual(span['source_path'], 'docs/tls.md')
            self.assertEqual(span['excerpt'], 'Use **TLS** for connections.')
            self.assertEqual(span['line_start'], 3)

    def test_browser_resolved_link_maps_back_to_raw_rst_reference(self):
        path = self.build / 'index.html'
        path.write_text(PAGE.replace('The cleaner is enabled by default.', 'The cleaner is enabled by default. Read Limits.'))
        raw = 'The cleaner is enabled by default. Read :doc:`docs/other`.'
        (self.repo / 'index.rst').write_text(RST.replace('The cleaner is enabled by default.', raw))
        tree_path = self.build / '.doctrees/index.doctree'
        tree = pickle.loads(tree_path.read_bytes())
        first = next(iter(tree.traverse(nodes.paragraph)))
        first.rawsource = raw
        first.children = [nodes.Text('The cleaner is enabled by default. Read docs/other.')]
        tree_path.write_bytes(pickle.dumps(tree))
        self.reset_inventory()
        c = self.candidate('index.html#start', excerpt='The cleaner is enabled by default. Read Limits.')
        span = c['evidence_groups'][0]['alternatives'][0]['source_spans'][0]
        self.assertEqual(span['mapping_method'], 'transformed_block')
        self.assertEqual(span['excerpt'], raw)

    def test_invalid_candidate_shapes_cannot_bypass_validation(self):
        self.read_root()
        for raw in ({}, {'question': 123, 'evidence_groups': 'wrong'}, {'evidence_groups': [{'alternatives': [{'observation_id': []}]}]}):
            c = create_candidate(raw, self.state, 'index.html#start', self.provider.config)
            self.assertFalse(review_candidate(c, self.state, Client(self.provider.config, self.state, lambda: None)))
            self.assertEqual(c['status'], 'rejected')

    def test_persistent_http_failure_pauses_and_usage_is_retained(self):
        self.provider.fail_code = 401
        result = run(self.store, self.provider.config)
        self.assertEqual(result['runtime']['status'], 'paused')
        self.assertIn('http_401', result['runtime']['error'])
        self.assertEqual(result['usage']['calls'], 1)
        self.assertGreater(result['usage']['tokens'], 0)

    def test_cross_page_questions_need_separate_required_groups(self):
        self.provider.cross_page = True
        result = run(self.store, self.provider.config)
        self.assertTrue(result['browsing_complete'], result)
        state = self.store.load()
        cross = next(c for c in accepted(state) if c['question_type'] == 'cross_page')
        self.assertEqual(validate(cross, state), [])
        cross['evidence_groups'][0]['alternatives'] += cross['evidence_groups'][1]['alternatives']
        cross['evidence_groups'].pop()
        self.assertTrue(any('Cross-page' in x for x in validate(cross, state)))

    def test_quote_from_unconsumed_observation_is_rejected(self):
        with Browser(self.state, self.provider.config, lambda: None) as browser:
            view = browser.observe()
            c = create_candidate(raw_candidate(view['section_id'], view['observation_id'], view['text']), self.state, view['section_id'], self.provider.config)
            self.assertFalse(review_candidate(c, self.state, Client(self.provider.config, self.state, lambda: None)))

    def test_app_build_mismatch_and_snapshot_drift(self):
        (self.build / 'index.html').write_text(PAGE + '<!-- changed -->')
        with self.assertRaisesRegex(ValueError, 'Rendered snapshot changed'):
            verify_snapshot(self.state)
        self.state['snapshot']['rendered_hashes']['index.html'] = 'incorrect-registered-hash'
        with self.assertRaisesRegex(Paused, 'differs from the registered build'):
            with Browser(self.state, self.provider.config, lambda: None):
                pass

    def test_source_changes_and_legacy_schema_are_rejected(self):
        (self.repo / 'docs/other.rst').write_text('Changed')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            verify_snapshot(self.state)
        self.state['schema_version'] = '2.0'
        with self.assertRaisesRegex(ValueError, 'Legacy checkpoint'):
            verify_snapshot(self.state)

    def test_navigation_suffix_and_missing_mapping(self):
        (self.repo / '_toc.yml').write_text('root: index\nentries:\n # - file: docs/missing\n - file: docs/other.rst\n')
        state = inventory(self.repo, self.build, build=False)
        self.assertEqual(state['inventory_errors'], [])
        (self.build / '.doctrees/index.doctree').unlink()
        state = inventory(self.repo, self.build, build=False)
        self.assertTrue(state['inventory_errors'])

    def test_edit_invalidates_approval_and_audit_expands(self):
        c = self.candidate()
        select_audit(self.state)
        decide(self.state, c['id'], 'edit', 'Clarify', {'question': 'Which setting enables topic compaction?'})
        self.assertEqual(c['status'], 'pending_revalidation')
        with self.assertRaises(ValueError):
            decide(self.state, c['id'], 'approve')
        self.assertTrue(self.state['audit']['expanded_categories'])
        self.assertTrue(review_candidate(c, self.state, Client(self.provider.config, self.state, lambda: None)))
        decide(self.state, c['id'], 'approve')
        self.assertFalse(audit_result(self.state)['passed'])
        select_audit(self.state, new_round=True)
        self.assertEqual(len(self.state['audit_history']), 1)
        decide(self.state, c['id'], 'approve')
        self.assertTrue(audit_result(self.state)['passed'])

    def test_stratified_sample_is_reproducible_and_frozen(self):
        c = self.candidate()
        for i in range(2100):
            other = copy.deepcopy(c)
            other.update(id=str(i), product=('kafka', 'postgresql')[i % 2], difficulty=('easy', 'medium', 'hard')[i % 3])
            self.state['candidates'].append(other)
        second = copy.deepcopy(self.state)
        sample = select_audit(self.state)
        self.assertEqual(sample['members'], select_audit(second)['members'])
        self.assertEqual(len(sample['members']), 106)
        with self.assertRaises(ValueError):
            select_audit(self.state)
        with self.assertRaises(ValueError):
            select_audit(self.state, new_round=True)

    def test_local_review_routes_and_csrf(self):
        c = self.candidate()
        self.store.save(self.state)
        server = make_server(self.store, 0)
        thread = start_server(server)
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urlopen(base) as response:
                html = response.read().decode()
            token = html.split("const token='")[1].split("'")[0]
            with urlopen(base + '/api/candidates/' + c['id']) as response:
                detail = json.load(response)
                self.assertIn('index.rst', detail['evidence'][0]['source_spans'][0]['source_path'])
            request = Request(base + '/api/decision', data=json.dumps({'id': c['id'], 'action': 'approve'}).encode(),
                              headers={'X-Review-Token': token, 'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(self.store.load()['candidates'][0]['human_status'], 'approved')
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + '/api/audit', data=b'{}'))
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_env_defaults_overrides_and_private_credentials(self):
        path = self.root / '.env'
        path.write_text('GOLDEN_BASE_URL=http://localhost:8001/v1\nGOLDEN_GENERATOR_MODEL=gen\nGOLDEN_REVIEWER_MODEL=judge\nGOLDEN_API_KEY=private-${LITERAL}-key\nGOLDEN_PARAMETERS=\'{"max_completion_tokens":1024}\'\n')
        with patch.dict(os.environ, {'GOLDEN_GENERATOR_MODEL': 'override'}, clear=True):
            config = load_config(path)
            self.assertEqual(config['generator_model'], 'override')
            self.assertFalse(config['headless'])
            self.assertEqual(config['browser_max_actions'], 200)
            self.assertEqual(config.api_key, 'private-${LITERAL}-key')
            self.assertNotIn('private-', json.dumps(config))
            self.assertNotIn('GOLDEN_API_KEY', os.environ)
            before = digest(config)
            config.api_key = 'new-key'
            self.assertEqual(digest(config), before)
            self.assertNotIn('max_tokens', config['parameters'])

    def test_env_limits_pricing_and_invalid_values(self):
        path = self.root / '.env'
        base = 'GOLDEN_BASE_URL=http://localhost:8001/v1\nGOLDEN_GENERATOR_MODEL=gen\nGOLDEN_REVIEWER_MODEL=judge\n'
        with patch.dict(os.environ, {}, clear=True):
            for value, expected in [('', None), ('3', 3)]:
                path.write_text(base + 'GOLDEN_MAX_SECTIONS=' + value)
                self.assertEqual(load_config(path)['max_sections'], expected)
            for extra in ('GOLDEN_MAX_SECTIONS=0', 'GOLDEN_MAX_SECTIONS=-1', 'GOLDEN_MAX_SECTIONS=1.5',
                          'GOLDEN_HEADLESS=invalid', 'GOLDEN_BROWSER_MAX_ACTIONS=0', 'GOLDEN_BROWSER_TIMEOUT_SECONDS=0',
                          'GOLDEN_RETRIES=oops', 'GOLDEN_PARAMETERS=[]', 'GOLDEN_MAX_COST=1'):
                path.write_text(base + extra)
                with self.assertRaises(ValueError, msg=extra):
                    load_config(path)
            path.write_text(base + 'GOLDEN_GENERATOR_INPUT_PER_MILLION=1\nGOLDEN_GENERATOR_OUTPUT_PER_MILLION=4\nGOLDEN_REVIEWER_INPUT_PER_MILLION=2\nGOLDEN_REVIEWER_OUTPUT_PER_MILLION=6\nGOLDEN_MAX_COST=20\n')
            self.assertEqual(load_config(path)['pricing']['judge']['output_per_million'], 6)
            with self.assertRaises(ValueError):
                load_config(self.root / 'missing')
        self.assertEqual(parse_json('```json\n{"ok":true}\n```'), {'ok': True})
        for text in ('broken', '[]'):
            with self.assertRaises(InvalidOutput):
                parse_json(text)


if __name__ == '__main__':
    unittest.main()
