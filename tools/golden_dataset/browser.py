"""Real Chromium interaction. Generator inputs originate in accessibility snapshots."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import yaml
from playwright.sync_api import Error as BrowserError, sync_playwright

from .model import Paused
from .snapshot import UNIT_CHARS, digest, normalize
from .storage import now


def role_name(key):
    match = re.match(r'^(\w+)(?: ("(?:[^"\\]|\\.)*"))?', str(key))
    if not match:
        return '', ''
    name = json.loads(match[2]) if match[2] else ''
    return match[1], name


def chunks(text):
    return [(i, min(i + UNIT_CHARS, len(text))) for i in range(0, len(text), UNIT_CHARS)] or [(0, 0)]


def accessible_label(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return normalize(' '.join(accessible_label(child) for child in value))
    if isinstance(value, dict):
        return normalize(' '.join(role_name(key)[1] or accessible_label(child)
                                  for key, child in value.items() if not str(key).startswith('/')))
    return ''


class Browser:
    def __init__(self, state, config, checkpoint):
        self.state, self.config, self.checkpoint = state, config, checkpoint
        self.base = config.get('docs_url', 'http://localhost:8000').rstrip('/') + '/'
        self.allowed = {}
        self.latest = None
        self.driver = self.browser = self.context = self.page = None

    def __enter__(self):
        try:
            self.driver = sync_playwright().start()
            self.browser = self.driver.chromium.launch(headless=self.config.get('headless', False))
            self.context = self.browser.new_context(viewport={'width': 1280, 'height': 900})
            self.context.set_default_timeout(self.config.get('browser_timeout_seconds', 30) * 1000)
            self.context.route('**/*', self.route)
            self.page = self.context.new_page()
            self.page.on('response', self.response)
            self.page.on('requestfailed', self.request_failed)
            url = self.state['browser'].get('last_url') or self.base
            self.state['browser']['discovered'].setdefault(self.base, {'label': 'Documentation home', 'from_url': None})
            self.page.goto(url, wait_until='domcontentloaded')
            self.check_page()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.context:
            self.context.close()
        if self.browser:
            self.browser.close()
        if self.driver:
            self.driver.stop()
        self.context = self.browser = self.driver = None

    def canonical(self, url):
        parsed = urlsplit(urljoin(self.base, url))
        base = urlsplit(self.base)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or not parsed.path.startswith(base.path):
            raise ValueError('Navigation must stay in the documentation app')
        path = unquote(parsed.path[len(base.path):])
        if any(part in {'..', '_sources'} for part in path.split('/')) or Path(path).suffix.lower() not in {'', '.html'}:
            raise ValueError('Raw source views are reserved for verification')
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment))

    def page_id(self, url=None):
        parsed = urlsplit(url or self.page.url)
        path = unquote(parsed.path[len(urlsplit(self.base).path):])
        if not path or path.endswith('/'):
            path += 'index.html'
        elif not Path(path).suffix:
            path += '.html'
        return path

    def route(self, route):
        request = route.request
        if request.is_navigation_request():
            try:
                self.canonical(request.url)
            except ValueError:
                route.abort()
                return
        route.continue_()

    def response(self, response):
        if response.status >= 400 and response.request.resource_type in {'xhr', 'fetch'} and self.page_id() == 'search.html':
            self.state['browser']['search_failures'].append({'time': now(), 'status': response.status})

    def request_failed(self, request):
        if self.page and request.resource_type in {'xhr', 'fetch'} and self.page_id() == 'search.html':
            self.state['browser']['search_failures'].append({'time': now(), 'error': 'Search request failed'})

    def check_page(self):
        self.canonical(self.page.url)
        page = self.page_id()
        if page in self.state['snapshot']['rendered_hashes']:
            response = self.context.request.get(self.page.url)
            if response.status != 200 or digest(response.body()) != self.state['snapshot']['rendered_hashes'][page]:
                raise Paused('Served documentation differs from the registered build: ' + page)
            info = self.state['pages'][page]
            info['visited'] = True
            info['pending_error'] = None
            if not self.state['snapshot']['selection'] and page not in self.state['expected_pages']:
                self.state['expected_pages'].append(page)
                self.state['expected_pages'].sort()
                self.state['coverage_audit']['promoted'].append(page)
        self.state['browser']['last_url'] = self.page.url

    def parse_snapshot(self, snapshot):
        tree = yaml.safe_load(snapshot) or []
        page_id = self.page_id()
        known = [self.state['sections'][sid] for sid in self.state['pages'].get(page_id, {}).get('sections', [])]
        active = known[0]['id'] if known and not known[0]['anchor'] else None
        segments, controls, blocks, prose = {}, [], {}, set()
        headings = []
        heading_stack, heading_occurrences = [], {}

        def append(value, meaningful=True):
            if active and isinstance(value, (str, int, float)) and str(value).strip():
                segments.setdefault(active, []).append(str(value))
                if meaningful:
                    prose.add(active)

        def walk(value, main=False):
            nonlocal active, headings
            if isinstance(value, list):
                for child in value:
                    walk(child, main)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if str(key).startswith('/'):
                        continue
                    role, name = role_name(key)
                    inside = main or role == 'main'
                    previous_sid = active
                    before = len(normalize(' '.join(segments.get(active, []))))
                    ref = re.search(r'\[ref=([^\]]+)\]', str(key))
                    if ref and (role in {'link', 'button', 'textbox', 'searchbox', 'tab', 'combobox', 'checkbox'} or
                                (role in {'generic', 'label'} and 'cursor=pointer' in str(key))):
                        href = next((str(x['/url']) for x in child if isinstance(x, dict) and '/url' in x), None) if isinstance(child, list) else None
                        control = {'ref': ref[1], 'role': role, 'name': name or accessible_label(child), 'main': inside}
                        if href:
                            try:
                                control['url'] = self.canonical(urljoin(self.page.url, href))
                            except ValueError:
                                pass
                        controls.append(control)
                        if inside and page_id in self.state['pages'] and (role == 'tab' or (role == 'button' and 'expanded=' in str(key))):
                            ident = role + ':' + name
                            visited = 'selected=true' in str(key) if role == 'tab' else 'expanded=true' in str(key)
                            previous = self.state['pages'][page_id]['controls'].get(ident, {})
                            self.state['pages'][page_id]['controls'][ident] = {'visited': visited or previous.get('visited', False),
                                                                                 'observation_consumed': previous.get('observation_consumed', False)}
                    if inside and role == 'heading':
                        level_match = re.search(r'\[level=(\d+)\]', str(key))
                        level = int(level_match[1]) if level_match else 1
                        heading_stack[:] = [(depth, title) for depth, title in heading_stack if depth < level]
                        heading_stack.append((level, name))
                        observed_path = [normalize(title) for _, title in heading_stack]
                        matches = [s for s in known if normalize(s['headings'][-1] if s['headings'] else '') == normalize(name)]
                        if matches:
                            exact = [s for s in matches if [normalize(title) for title in s['headings']] == observed_path]
                            matches = exact or matches
                            identity = tuple(observed_path)
                            occurrence = heading_occurrences.get(identity, 0)
                            matched = matches[min(occurrence, len(matches) - 1)]
                            heading_occurrences[identity] = occurrence + 1
                            active = matched['id']
                            headings = matched['headings']
                        return_to_children = False
                    else:
                        return_to_children = True
                        if inside and name and role not in {'button', 'tab', 'textbox', 'searchbox', 'combobox', 'checkbox'}:
                            append(name, role != 'link')
                        if inside and isinstance(child, (str, int, float)):
                            append(child)
                    if return_to_children and isinstance(child, (list, dict)):
                        walk(child, inside)
                    if inside and role in {'paragraph', 'code'} and active == previous_sid:
                        text = normalize(' '.join(segments.get(active, [])))
                        start = before + bool(before)
                        if len(text) > start:
                            blocks.setdefault(active, []).append({'tag': 'pre' if role == 'code' else 'p',
                                                                  'start': start, 'end': len(text), 'text': text[start:]})
            elif isinstance(value, str) and re.match(r'^(heading|button|link|textbox|searchbox|tab|combobox|checkbox|cell|rowheader|columnheader|generic|text)(?: |\[|$)', value):
                walk({value: None}, main)
            elif main:
                append(value)

        walk(tree)
        return {sid: normalize(' '.join(parts)) for sid, parts in segments.items()}, controls, blocks, prose

    def observe(self, scope='main', cursor=None):
        if scope not in {'main', 'page'}:
            raise ValueError('Observation scope must be main or page')
        self.check_page()
        snapshot = self.page.locator('body').aria_snapshot(mode='ai')
        segments, controls, blocks, prose = self.parse_snapshot(snapshot)
        page = self.page_id()
        # Full snapshots are durable artifacts. Only delivered chunks count as read.
        snapshot_id = digest([self.page.url, snapshot])
        self.state.setdefault('accessibility_snapshots', {})[snapshot_id] = {'url': self.page.url, 'text': snapshot}
        available = []
        for sid, text in segments.items():
            section = self.state['sections'][sid]
            section['observed'] = True
            section['text'] = text
            section['substantive'] = sid in prose
            section['exclusion_reason'] = None if sid in prose else 'navigation-only'
            observation_id = digest([sid, text])
            observation = self.state['observations'].setdefault(observation_id, {
                'id': observation_id, 'section_id': sid, 'page': page, 'url': self.page.url,
                'text': text, 'snapshot_id': snapshot_id, 'blocks': blocks.get(sid, []), 'chunks': []})
            if not observation['chunks']:
                for start, end in chunks(text):
                    unit = {'id': digest([observation_id, start]), 'observation_id': observation_id,
                            'start': start, 'end': end, 'read': False}
                    observation['chunks'].append(unit)
                    section['units'].append(unit.copy())
            for unit in observation['chunks']:
                available.append((section, observation, unit))
        for sid in self.state['pages'].get(page, {}).get('sections', []):
            if not self.state['sections'][sid]['substantive']:
                self.state['sections'][sid]['observed'] = True
        completed = {c['target_section'] for c in self.state['candidates'] if c['status'] == 'accepted' and c['human_status'] != 'rejected'}
        if scope == 'page' or not available:
            ranges = chunks(snapshot)
            index = int(cursor or 0)
            if not 0 <= index < len(ranges):
                raise ValueError('Observation cursor outside snapshot')
            start, end = ranges[index]
            delivered = snapshot[start:end]
            visible_controls = [c for c in controls if '[ref=' + c['ref'] + ']' in delivered]
            view = {'url': self.page.url, 'scope': 'page', 'snapshot': delivered, 'snapshot_id': snapshot_id,
                    'cursor': index, 'chunks': len(ranges), 'controls': visible_controls}
        else:
            choices = [x for x in available if not x[2]['read']]
            choices += [x for x in available if x[0]['id'] not in completed and x[0]['attempts'] < 3]
            chosen = available[int(cursor)] if cursor is not None else (choices or available)[0]
            section, observation, unit = chosen
            visible_controls = [c for c in controls if c['main']][:40]
            view = {'url': self.page.url, 'scope': 'main', 'observation_id': observation['id'], 'section_id': section['id'],
                    'heading_path': section['headings'], 'chunk_id': unit['id'], 'start': unit['start'], 'end': unit['end'],
                    'text': observation['text'][unit['start']:unit['end']], 'controls': visible_controls,
                    'remaining_chunks': sum(not x[2]['read'] for x in available), 'chunks': len(available),
                    'question_required': section['substantive'] and section['id'] not in completed and section['attempts'] < 3}
        self.allowed = {c['ref']: c for c in visible_controls}
        for control in visible_controls:
            if 'url' in control:
                self.state['browser']['discovered'][control['url']] = {'label': control['name'], 'from_url': self.page.url}
        def needs_work(ident):
            if ident not in self.state['expected_pages']:
                return False
            page_info = self.state['pages'][ident]
            return (not page_info['visited'] or any(not v['visited'] or not v['observation_consumed'] for v in page_info['controls'].values()) or
                    any(not self.state['sections'][sid]['observed'] or any(not u['read'] for u in self.state['sections'][sid]['units']) or
                        (self.state['sections'][sid]['substantive'] and sid not in completed and self.state['sections'][sid]['attempts'] < 3)
                        for sid in page_info['sections']))
        view['frontier'] = [{'url': url, 'label': info['label']} for url, info in self.state['browser']['discovered'].items()
                            if self.page_id(url) != page and needs_work(self.page_id(url))][:40]
        view['current_page_sections'] = [{'section_id': sid, 'heading_path': self.state['sections'][sid]['headings'],
                                          'question_accepted': sid in completed, 'attempts': self.state['sections'][sid]['attempts']}
                                         for sid in segments]
        view['in_dataset_scope'] = page in self.state['expected_pages']
        if page == 'search.html' and self.state['browser']['search_failures']:
            view['search_status'] = {'failed': True, 'instruction': 'The app search request failed; continue using navigation and content links'}
        self.latest = view
        self.checkpoint()
        return view

    def consume(self, view):
        if view.get('observation_id'):
            obs = self.state['observations'][view['observation_id']]
            for unit in obs['chunks'] + self.state['sections'][obs['section_id']]['units']:
                if unit['id'] == view['chunk_id']:
                    unit['read'] = True
            for control in self.state['pages'][obs['page']]['controls'].values():
                if control['visited']:
                    control['observation_consumed'] = True
        self.checkpoint()

    def tool(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError('Browser arguments must be an object')
        if name == 'observe':
            return self.observe(arguments.get('scope', 'main'), arguments.get('cursor'))
        try:
            if name == 'navigate':
                url = self.canonical(arguments['url'])
                if url != self.base and url not in self.state['browser']['discovered']:
                    raise ValueError('URL has not been discovered in a browser observation')
                self.page.goto(url, wait_until='domcontentloaded')
            elif name == 'back':
                self.page.go_back(wait_until='domcontentloaded')
            elif name in {'click', 'type'}:
                ref = arguments['ref']
                if ref not in self.allowed:
                    raise ValueError('Reference was not present in the delivered observation; observe again')
                control = self.allowed[ref]
                if name == 'click' and control['role'] == 'link' and 'url' not in control:
                    raise ValueError('This link is outside documentation browsing scope')
                locator = self.page.get_by_ref(ref)
                if name == 'click':
                    locator.click()
                else:
                    if control['role'] not in {'textbox', 'searchbox', 'combobox'}:
                        raise ValueError('Type requires an observed text input')
                    locator.fill(str(arguments['text']))
            elif name == 'keypress':
                if arguments['key'] not in {'Enter', 'Tab', 'Escape', 'PageDown', 'PageUp', 'ArrowDown', 'ArrowUp', 'Home', 'End'}:
                    raise ValueError('Unsupported browser key')
                self.page.keyboard.press(arguments['key'])
            elif name == 'scroll':
                self.page.mouse.wheel(0, max(-1500, min(1500, int(arguments.get('pixels', 700)))))
            else:
                raise ValueError('Unknown browser action')
            self.page.wait_for_load_state('domcontentloaded')
            for popup in list(self.context.pages):
                if popup is not self.page:
                    try:
                        self.canonical(popup.url)
                        self.page = popup
                    except ValueError:
                        popup.close()
            return self.observe()
        except BrowserError as exc:
            errors = self.state['browser'].setdefault('action_errors', [])
            errors.append({'time': now(), 'action': name, 'url': self.page.url, 'error': str(exc)[:1000]})
            del errors[:-50]
            # Refresh refs and let the model replan instead of replaying a stale action.
            result = self.observe('page')
            result['action_error'] = ('A page overlay blocked the action; inspect the dialog controls before retrying'
                                      if 'intercepts pointer events' in str(exc) else
                                      'Browser action failed or reference became stale; fresh observation supplied')
            return result
