"""Versioned Community posts and public-release verification; no publishing writes."""

from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.request

COMMUNITY_URL = 'https://pumpwizard.fun/community'
POSTS = Path(__file__).parent / 'data' / 'runner-community-posts.json'


def post_id(version):
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.dev\d+)?', version):
        raise ValueError('Unsupported release version')
    return 'pumpwizard-runner-' + version.replace('.', '-')


def note_for(version, path=POSTS):
    posts = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(posts, list):
        raise ValueError('Community posts must be a list')
    ids = [p['id'] for p in posts]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate Community post ID')
    found = [p for p in posts if p['id'] == post_id(version)]
    if not found:
        raise ValueError(f'Build {version} requires its own Community patch note in {path}')
    post = found[0]
    date.fromisoformat(post['date'])
    if post['kind'] != 'patch-notes' or post['status'] not in ('draft', 'published'):
        raise ValueError('Invalid patch-note kind or status')
    if version not in post['title'] or not post.get('bullets'):
        raise ValueError('Patch note must name its version and describe its changes')
    for value in (post['title'], post['summary'], *post.get('paragraphs', []), *post['bullets']):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Patch-note text cannot be empty')
    return post


def note_url(version):
    return COMMUNITY_URL + '#' + post_id(version)


class PublishedPost(HTMLParser):
    def __init__(self, ident):
        super().__init__()
        self.ident, self.inside, self.count, self.kind, self.parts = ident, False, 0, None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'article' and attrs.get('id') == self.ident:
            self.inside = True
            self.count += 1
            self.kind = attrs.get('data-update-kind')

    def handle_endtag(self, tag):
        if tag == 'article':
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def verify_html(post, html):
    parser = PublishedPost(post['id'])
    parser.feed(html)
    text = ' '.join(' '.join(parser.parts).split())
    expected = [post['title'], post['summary'], *post.get('paragraphs', []), *post['bullets']]
    if parser.count != 1 or parser.kind != 'patch-notes' or any(' '.join(t.split()) not in text for t in expected):
        raise ValueError('The matching Community patch note is missing or differs from this build. Publish it before release.')


def verify_live(post):
    request = urllib.request.Request(COMMUNITY_URL, headers={
        'User-Agent': 'PumpWizardRunner-release-check/1.0', 'Accept': 'text/html'})
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.geturl() != COMMUNITY_URL:
            raise ValueError('Unexpected Community redirect; verify the canonical URL')
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError('Community page exceeds verification limit')
    verify_html(post, body.decode('utf-8'))
