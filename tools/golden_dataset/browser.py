"""Real Chromium interaction. Generator inputs originate in accessibility snapshots."""

import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import yaml
from playwright.sync_api import Error as BrowserError, sync_playwright

from .model import Paused
from .snapshot import UNIT_CHARS, digest, normalize, served_html_matches
from .storage import now
from .tracing import trace, traced

# Article text per view; longer articles continue with read_more(part).
CONTENT_CHARS = 12000
# Recently read text the generator can still quote, e.g. for cross-page questions.
MEMORY_CHARS = 16000
KEYS = {'Enter', 'Tab', 'Escape'}
# Third-party hosts that render documentation content (Mermaid diagrams); all others are blocked.
RENDERING_HOSTS = {'unpkg.com'}
STATES = re.compile(r'\[(expanded|selected|checked|pressed)(?:=(\w+))?\]')


class BrokenLink(ValueError):
    pass


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
        self.latest = self.delivered = self.nav_status = None
        self.expanded_menus, self.session_read = set(), set()
        self.driver = self.browser = self.context = self.page = None

    def __enter__(self):
        try:
            with traced('browser', 'Starting Playwright driver'):
                self.driver = sync_playwright().start()
            with traced('browser', f'Launching Chromium; headless={self.config.get("headless", False)}'):
                self.browser = self.driver.chromium.launch(
                    headless=self.config.get('headless', False),
                    timeout=self.config.get('browser_timeout_seconds', 30) * 1000)
            self.context = self.browser.new_context(viewport={'width': 1280, 'height': 900})
            self.context.set_default_timeout(self.config.get('browser_timeout_seconds', 30) * 1000)
            self.context.route('**/*', self.route)
            self.page = self.context.new_page()
            if not self.config.get('headless', False):
                self.page.bring_to_front()
            self.page.on('response', self.response)
            self.page.on('requestfailed', self.request_failed)
            for key in ('discovered', 'search_failures', 'broken_links', 'recent_observations'):
                self.state['browser'].setdefault(key, {} if key == 'discovered' else [])
            url = self.state['browser'].get('last_url') or self.base
            self.state['browser']['discovered'].setdefault(self.base, {'label': 'Documentation home', 'from_url': None})
            with traced('browser', f'Opening documentation: {url}'):
                self.page.goto(url, wait_until='domcontentloaded')
                try:
                    self.check_page()
                except BrokenLink:
                    self.nav_status = None
                    self.page.goto(self.base, wait_until='domcontentloaded')
                    self.check_page()
            trace('browser', 'Documentation loaded; browser session ready')
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.driver:
            trace('browser', 'Closing Chromium session')
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
        target = parsed.path
        # Production serves pretty URLs (docs/products/kafka); the local preview only serves .html files.
        if path and not path.endswith('/') and not Path(path).suffix:
            target += '.html'
        return urlunsplit((parsed.scheme, parsed.netloc, target, parsed.query, parsed.fragment))

    def key(self, url):
        return self.canonical(url).split('#')[0]

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
                target = self.canonical(request.url)
            except ValueError:
                route.abort()
                return
            if target.split('#')[0] != request.url.split('#')[0]:
                route.fulfill(status=302, headers={'Location': target})
                return
        elif urlsplit(request.url).netloc not in {urlsplit(self.base).netloc, *RENDERING_HOSTS}:
            # Consent banners and analytics would cover every page and report crawler traffic as visits.
            route.abort()
            return
        route.continue_()

    def response(self, response):
        if response.request.is_navigation_request() and self.page and response.frame == self.page.main_frame:
            self.nav_status = response.status
        if response.status >= 400 and response.request.resource_type in {'xhr', 'fetch'} and self.page_id() == 'search.html':
            self.state['browser']['search_failures'].append({'time': now(), 'status': response.status})

    def request_failed(self, request):
        if self.page and request.resource_type in {'xhr', 'fetch'} and self.page_id() == 'search.html':
            self.state['browser']['search_failures'].append({'time': now(), 'error': 'Search request failed'})

    def check_page(self):
        if self.nav_status is not None and self.nav_status >= 400:
            raise BrokenLink(f'Link target returned HTTP {self.nav_status}')
        self.canonical(self.page.url)
        page = self.page_id()
        if page in self.state['snapshot']['rendered_hashes']:
            response = self.context.request.get(self.page.url)
            if response.status != 200:
                raise BrokenLink(f'Link target returned HTTP {response.status}')
            if not served_html_matches(response.body(), self.state['snapshot']['rendered_hashes'][page]):
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
        last_link = ''

        def append(value, meaningful=True):
            if active and isinstance(value, (str, int, float)) and str(value).strip():
                segments.setdefault(active, []).append(str(value))
                if meaningful:
                    prose.add(active)

        def walk(value, main=False, nav=False, depth=0):
            nonlocal active, headings, last_link
            if isinstance(value, list):
                for child in value:
                    walk(child, main, nav, depth)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if str(key).startswith('/'):
                        continue
                    role, name = role_name(key)
                    inside = main or role == 'main'
                    in_nav = not inside and (nav or role in {'complementary', 'navigation'})
                    level = depth + (role == 'list')
                    previous_sid = active
                    before = len(normalize(' '.join(segments.get(active, []))))
                    ref = re.search(r'\[ref=([^\]]+)\]', str(key))
                    if ref and (role in {'link', 'button', 'textbox', 'searchbox', 'tab', 'combobox', 'checkbox'} or
                                (role in {'generic', 'label'} and 'cursor=pointer' in str(key))):
                        href = next((str(x['/url']) for x in child if isinstance(x, dict) and '/url' in x), None) if isinstance(child, list) else None
                        control = {'ref': ref[1], 'role': role, 'name': name or accessible_label(child), 'main': inside,
                                   'region': 'main' if inside else 'nav' if in_nav else 'site', 'depth': level,
                                   'states': {state: flag in ('', 'true') for state, flag in STATES.findall(str(key))}}
                        if href:
                            try:
                                control['url'] = self.canonical(urljoin(self.page.url, href))
                            except ValueError:
                                pass
                        if role == 'link':
                            last_link = control['name']
                        elif in_nav and role in {'generic', 'label'}:
                            # Furo's "Toggle child pages in navigation" label belongs to the preceding menu link.
                            control['expands'] = last_link
                        controls.append(control)
                    if inside and role == 'heading':
                        level_match = re.search(r'\[level=(\d+)\]', str(key))
                        heading_level = int(level_match[1]) if level_match else 1
                        heading_stack[:] = [(d, title) for d, title in heading_stack if d < heading_level]
                        heading_stack.append((heading_level, name))
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
                        walk(child, inside, in_nav, level)
                    if inside and role in {'paragraph', 'code'} and active == previous_sid:
                        text = normalize(' '.join(segments.get(active, [])))
                        start = before + bool(before)
                        if len(text) > start:
                            blocks.setdefault(active, []).append({'tag': 'pre' if role == 'code' else 'p',
                                                                  'start': start, 'end': len(text), 'text': text[start:]})
            elif isinstance(value, str) and re.match(r'^(heading|button|link|textbox|searchbox|tab|combobox|checkbox|cell|rowheader|columnheader|generic|text)(?: |\[|$)', value):
                walk({value: None}, main, nav, depth)
            elif main:
                append(value)

        walk(tree)
        # A menu expander is open when the next menu link is nested deeper than the expander.
        nav = [c for c in controls if c['region'] == 'nav']
        for index, control in enumerate(nav):
            if 'expands' in control:
                following = next((c for c in nav[index + 1:] if c['role'] == 'link'), None)
                control['states']['expanded'] = bool(following and following['depth'] > control['depth'])
        return {sid: normalize(' '.join(parts)) for sid, parts in segments.items()}, controls, blocks, prose

    def register_controls(self, page, controls):
        """Tabs and disclosures hide article text until a user interacts with them."""
        for control in controls:
            # Accessibility snapshots only print [expanded] when true; collapsed disclosures look like plain buttons.
            if control['role'] == 'button':
                try:
                    expanded = self.page.get_by_ref(control['ref']).get_attribute('aria-expanded', timeout=1000)
                except BrowserError:
                    expanded = None
                if expanded is not None:
                    control['states']['expanded'] = expanded == 'true'
        if page not in self.state['pages']:
            return
        for control in controls:
            if not control['main'] or control['role'] not in {'tab', 'button'}:
                continue
            if control['role'] == 'button':
                if 'expanded' not in control['states']:
                    continue
                visited = control['states']['expanded']
            else:
                visited = control['states'].get('selected', False)
            ident = control['role'] + ':' + control['name']
            previous = self.state['pages'][page]['controls'].get(ident, {})
            self.state['pages'][page]['controls'][ident] = {'visited': visited or previous.get('visited', False),
                                                             'observation_consumed': previous.get('observation_consumed', False)}

    def memory(self):
        """Most recently read text, newest first within budget, returned oldest first."""
        items, size = [], 0
        for ident in reversed(self.state['browser'].get('recent_observations', [])):
            observation = self.state['observations'].get(ident)
            if not observation:
                continue
            for unit in reversed([u for u in observation['chunks'] if u['read']]):
                length = unit['end'] - unit['start']
                if items and size + length > MEMORY_CHARS:
                    return items[::-1]
                size += length
                item = {'observation_id': ident, 'page': observation['page'],
                        'heading_path': self.state['sections'][observation['section_id']]['headings'],
                        'text': observation['text'][unit['start']:unit['end']]}
                if unit['start']:
                    item['start'] = unit['start']
                items.append(item)
        return items[::-1]

    def snapshot(self):
        # A navigation can swap the document mid-read, leaving no <body> for a moment.
        for attempt in range(4):
            try:
                self.page.wait_for_load_state('domcontentloaded')
                return self.page.locator('body').aria_snapshot(mode='ai')
            except BrowserError:
                if attempt == 3:
                    raise
                self.page.wait_for_timeout(250 * (attempt + 1))

    def observe(self, part=None):
        self.check_page()
        with traced('observe', 'Reading accessibility observation'):
            snapshot = self.snapshot()
        segments, controls, blocks, prose = self.parse_snapshot(snapshot)
        page = self.page_id()
        self.register_controls(page, controls)
        # Full snapshots are durable artifacts. Only delivered content counts as read.
        snapshot_id = digest([self.page.url, snapshot])
        self.state.setdefault('accessibility_snapshots', {})[snapshot_id] = {'url': self.page.url, 'text': snapshot}
        units = []
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
                    observation['chunks'].append({'id': digest([observation_id, start]), 'observation_id': observation_id,
                                                  'start': start, 'end': end, 'read': False})
            # Reading progress follows the section's current rendering, e.g. after a tab switch.
            if section.get('observation_id') != observation_id:
                section['observation_id'] = observation_id
                section['units'] = [unit.copy() for unit in observation['chunks']]
            units.extend((section, observation, unit) for unit in observation['chunks'])
        for sid in self.state['pages'].get(page, {}).get('sections', []):
            if not self.state['sections'][sid]['substantive']:
                self.state['sections'][sid]['observed'] = True

        # Text read in this session and still in memory is not resent; after a resume the page is shown in full.
        remembered = {m['observation_id'] for m in self.memory()} & self.session_read
        pending = lambda unit, observation: not (unit['read'] and observation['id'] in remembered)
        parts, size = [[]], 0
        for item in units:
            length = item[2]['end'] - item[2]['start']
            if parts[-1] and size + length > CONTENT_CHARS:
                parts.append([])
                size = 0
            parts[-1].append(item)
            size += length
        unread_parts = [i for i, items in enumerate(parts) if any(pending(u, o) for _, o, u in items)]
        if part is None:
            part = (unread_parts or [0])[0]
        try:
            part = int(part)
        except (TypeError, ValueError):
            raise ValueError('read_more part must be an integer') from None
        if not 0 <= part < len(parts):
            raise ValueError(f'This page has content parts 0..{len(parts) - 1}')
        delivered = {unit['id'] for section, observation, unit in parts[part] if pending(unit, observation)}
        content, stubbed = [], set()
        for section, observation, unit in parts[part]:
            if unit['id'] in delivered:
                item = {'section_id': section['id'], 'observation_id': observation['id'], 'heading_path': section['headings'],
                        'text': observation['text'][unit['start']:unit['end']]}
                if unit['start']:
                    item['start'] = unit['start']
                content.append(item)
            elif section['id'] not in stubbed:
                stubbed.add(section['id'])
                content.append({'section_id': section['id'], 'heading_path': section['headings'], 'already_read': True})

        def link(control):
            item = {'ref': control['ref'], 'label': control['name'][:160]}
            if 'url' in control:
                target = self.page_id(control['url'])
                item['page'] = target
                if target == page:
                    item['current'] = True
                elif target in self.state['pages']:
                    item['visited'] = self.state['pages'][target]['visited']
                    if target not in self.state['expected_pages'] and self.state['snapshot']['selection']:
                        item['in_dataset_scope'] = False
            return item

        nav = []
        for control in controls:
            if control['region'] != 'nav':
                continue
            if control['role'] == 'link':
                if 'url' in control:
                    nav.append({**link(control), 'depth': control['depth']})
            elif 'expands' in control:
                nav.append({'ref': control['ref'], 'expand': control['expands'] or control['name'],
                            'expanded': control['states'].get('expanded', False), 'depth': control['depth']})
            else:
                nav.append({'ref': control['ref'], 'control': control['role'], 'label': control['name'][:160],
                            **({'expanded': control['states']['expanded']} if 'expanded' in control['states'] else {})})
        content_links = [link(c) for c in controls if c['region'] == 'main' and c['role'] == 'link' and
                         'url' in c and self.page_id(c['url']) != page]
        page_controls = [{'ref': c['ref'], 'role': c['role'], 'label': c['name'][:160], **c['states']} for c in controls
                         if c['region'] == 'main' and c['role'] != 'link' and not re.match(r'copy', c['name'], re.I)]
        site = [{**link(c), 'role': c['role']} for c in controls if c['region'] == 'site' and
                ('url' in c or c['role'] in {'textbox', 'searchbox', 'combobox'})]

        self.allowed = {c['ref']: c for c in controls}
        discovered = self.state['browser']['discovered']
        for control in controls:
            if 'url' in control:
                discovered.setdefault(self.key(control['url']), {'label': control['name'], 'from_url': self.page.url})
        expected = self.state['expected_pages']
        frontier = [{'url': url, 'label': info['label']} for url, info in discovered.items()
                    if self.page_id(url) in expected and not self.state['pages'][self.page_id(url)]['visited']]
        on_page = [c for c in self.state['candidates'] if c['status'] == 'accepted' and c['human_status'] != 'rejected'
                   and self.state['sections'][c['target_section']]['page'] == page]
        view = {'url': self.page.url, 'page': page, 'snapshot_id': snapshot_id,
                'in_dataset_scope': page in expected, 'content': content,
                'content_part': part, 'content_parts': len(parts), 'unread_parts': [i for i in unread_parts if i != part],
                'content_links': content_links,
                'page_controls': page_controls, 'nav': nav, 'site': site,
                'questions_on_page': [c['question'] for c in on_page],
                'progress': {'pages_visited': sum(self.state['pages'][p]['visited'] for p in expected),
                             'pages_in_scope': len(expected), 'unvisited_discovered': len(frontier),
                             'frontier_sample': frontier[:10]}}
        if page == 'search.html' and self.state['browser']['search_failures']:
            view['search_status'] = {'failed': True, 'instruction': 'The app search request failed; continue using navigation and content links'}
        self.latest = view
        self.delivered = (view, [(observation['id'], unit['id']) for _, observation, unit in parts[part] if unit['id'] in delivered])
        self.checkpoint()
        return view

    def consume(self, view):
        if not self.delivered or self.delivered[0] is not view:
            return
        recent = self.state['browser'].setdefault('recent_observations', [])
        for observation_id, unit_id in self.delivered[1]:
            observation = self.state['observations'][observation_id]
            for unit in observation['chunks'] + self.state['sections'][observation['section_id']]['units']:
                if unit['id'] == unit_id:
                    unit['read'] = True
            self.session_read.add(observation_id)
            if observation_id in recent:
                recent.remove(observation_id)
            recent.append(observation_id)
            for control in self.state['pages'][observation['page']]['controls'].values():
                if control['visited']:
                    control['observation_consumed'] = True
        del recent[:-40]
        self.checkpoint()

    def next_page(self):
        """Move on like a reader: article links first, then the menu, then a collapsed menu group."""
        expected = set(self.state['expected_pages'])
        current = self.page_id()

        def unvisited(control):
            if control['role'] != 'link' or 'url' not in control:
                return False
            target = self.page_id(control['url'])
            return target != current and target in expected and not self.state['pages'][target]['visited']

        controls = list(self.allowed.values())
        for region in ('main', 'nav', 'site'):
            link = next((c for c in controls if c['region'] == region and unvisited(c)), None)
            if link:
                return self.tool('click', {'ref': link['ref']})
        for control in controls:
            ident = (current, control.get('expands'))
            if 'expands' in control and not control['states'].get('expanded') and ident not in self.expanded_menus:
                self.expanded_menus.add(ident)
                return self.tool('click', {'ref': control['ref']})
        for url in self.state['browser']['discovered']:
            target = self.page_id(url)
            if target in expected and not self.state['pages'][target]['visited']:
                return self.tool('navigate', {'url': url})
        raise ValueError('No unvisited documentation link is known; expand menu groups or use search')

    def tool(self, name, arguments):
        if not isinstance(arguments, dict):
            raise ValueError('Browser arguments must be an object')
        if name == 'observe':
            return self.observe()
        if name == 'read_more':
            return self.observe(arguments.get('part'))
        self.nav_status = None
        try:
            if name == 'navigate':
                url = self.canonical(arguments['url'])
                if self.key(url) != self.base and self.key(url) not in self.state['browser']['discovered']:
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
                if name == 'click' and 'url' in control and self.key(control['url']) != self.key(self.page.url):
                    # Wait for the new document; otherwise the old page still reports itself as loaded.
                    with self.page.expect_navigation(wait_until='domcontentloaded'):
                        locator.click()
                elif name == 'click':
                    locator.click()
                else:
                    if control['role'] not in {'textbox', 'searchbox', 'combobox'}:
                        raise ValueError('Type requires an observed text input')
                    locator.fill(str(arguments['text']))
            elif name == 'keypress':
                if arguments['key'] not in KEYS:
                    raise ValueError('Unsupported browser key; use Enter, Tab, or Escape')
                self.page.keyboard.press(arguments['key'])
            else:
                raise ValueError('Unknown browser action')
            self.page.wait_for_load_state('domcontentloaded')
            for popup in list(self.context.pages):
                if popup is not self.page:
                    try:
                        self.canonical(popup.url)
                        self.page = popup
                        popup.on('response', self.response)
                    except ValueError:
                        popup.close()
            return self.observe()
        except BrokenLink as exc:
            broken = self.state['browser'].setdefault('broken_links', [])
            broken.append({'time': now(), 'url': self.page.url, 'error': str(exc)})
            del broken[:-50]
            self.nav_status = None
            # last_url still names the page before the broken link.
            if self.page.go_back(wait_until='domcontentloaded') is None:
                self.page.goto(self.state['browser'].get('last_url') or self.base, wait_until='domcontentloaded')
            self.nav_status = None
            result = self.observe()
            result['action_error'] = str(exc) + '; returned to the previous page'
            return result
        except BrowserError as exc:
            errors = self.state['browser'].setdefault('action_errors', [])
            errors.append({'time': now(), 'action': name, 'url': self.page.url, 'error': str(exc)[:1000]})
            del errors[:-50]
            # Refresh refs and let the model replan instead of replaying a stale action.
            result = self.observe()
            result['action_error'] = ('A page overlay blocked the action; inspect the dialog controls before retrying'
                                      if 'intercepts pointer events' in str(exc) else
                                      'Browser action failed or reference became stale; fresh observation supplied')
            return result
