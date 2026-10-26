"""Private metadata audit: no article content is supplied to generation."""

import pickle
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, SoupStrainer


def metadata(repo, build_dir, page, source_path):
    tree_path = build_dir / '.doctrees' / Path(page).with_suffix('.doctree')
    if not tree_path.is_file():
        raise ValueError('Source mapping requires a trusted local Sphinx .doctrees directory')
    with tree_path.open('rb') as stream:
        tree = pickle.load(stream)
    sections = []
    for node in tree.traverse():
        if getattr(node, 'tagname', '') != 'section':
            continue
        ids = node.get('ids', [])
        if not ids:
            continue
        headings = []
        current = node
        while current is not None:
            if getattr(current, 'tagname', '') == 'section':
                title = next((c for c in current.children if getattr(c, 'tagname', '') == 'title'), None)
                if title is not None:
                    headings.insert(0, title.astext())
            current = getattr(current, 'parent', None)
        # Only structural presence is retained here; source prose stays private.
        has_content = False
        for child in node.traverse():
            if getattr(child, 'tagname', '') not in {'paragraph', 'literal_block', 'doctest_block', 'math_block', 'table'}:
                continue
            owner = getattr(child, 'parent', None)
            while owner is not None and getattr(owner, 'tagname', '') != 'section':
                owner = getattr(owner, 'parent', None)
            if owner is node:
                has_content = True
                break
        sections.append({'id': page + '#' + ids[0], 'page': page, 'anchor': ids[0], 'headings': headings,
                         'document_path': source_path, 'text': '', 'substantive': has_content,
                         'exclusion_reason': None if has_content else 'empty-or-navigation-only',
                         'units': [], 'attempts': 0, 'error': None, 'observed': False,
                         'source_section_id': None, 'source': None})
    if not sections:
        sections.append({'id': page + '#', 'page': page, 'anchor': '', 'headings': [], 'document_path': source_path,
                         'text': '', 'substantive': True, 'exclusion_reason': None, 'units': [],
                         'attempts': 0, 'error': None, 'observed': False, 'source_section_id': None, 'source': None})
    # Link destinations only; this graph measures required coverage, never feeds a reader.
    soup = BeautifulSoup((build_dir / page).read_text(encoding='utf-8'), 'html.parser', parse_only=SoupStrainer('article'))
    links = [a['href'] for a in soup.select('a[href]')]
    return {'id': page, 'source_path': source_path, 'sections': [s['id'] for s in sections],
            'links': links, 'visited': False, 'controls': {}, 'pending_error': None}, sections


def reachable(pages, navigation, link_target):
    edges = {}
    for page, info in pages.items():
        targets = set()
        for href in info['links']:
            url = urlsplit(href)
            if url.hostname == 'docs.aiven.io':
                href = url.path
            target = link_target(page, href)
            if target and target['page'] in pages:
                targets.add(target['page'])
        edges[page] = targets
    seen = ({'index.html'} | set(navigation)) & pages.keys()
    queue = deque(sorted(seen))
    while queue:
        for page in edges[queue.popleft()]:
            if page not in seen:
                seen.add(page)
                queue.append(page)
    return seen
