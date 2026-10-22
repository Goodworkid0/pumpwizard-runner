import copy
import json
from html import escape

import pytest

from pumpwizard_runner import __version__
from pumpwizard_runner.release_notes import note_for, note_url, verify_html


def test_current_build_has_versioned_community_note():
    post = note_for(__version__)
    assert note_url(__version__) == 'https://pumpwizard.fun/community#' + post['id']
    assert __version__ in post['title']


def test_missing_note_and_duplicate_ids_block_build(tmp_path):
    path = tmp_path / 'notes.json'
    path.write_text('[]')
    with pytest.raises(ValueError, match='requires its own'):
        note_for(__version__, path)
    post = note_for(__version__)
    path.write_text(json.dumps([post, post]))
    with pytest.raises(ValueError, match='Duplicate'):
        note_for(__version__, path)


def published_html(post):
    # Static website article shape; escape text as the website renderer does.
    return ('<article id="' + post['id'] + '" data-update-kind="patch-notes"><h3>'
            + escape(post['title']) + '</h3><p>' + escape(post['summary']) + '</p><details>'
            + ''.join('<p>' + escape(t) + '</p>' for t in post['paragraphs'])
            + '<ul>' + ''.join('<li>' + escape(t) + '</li>' for t in post['bullets'])
            + '</ul></details></article>')


def test_matching_live_article_is_required_not_a_link_or_title():
    post = note_for(__version__)
    html = published_html(post)
    verify_html(post, html)
    for invalid in ('<a href="#' + post['id'] + '">' + post['title'] + '</a>',
                    html.replace(post['id'], 'different-build'), html + html,
                    html.replace('data-update-kind="patch-notes"', 'data-update-kind="development"')):
        with pytest.raises(ValueError, match='missing or differs'):
            verify_html(post, invalid)
    stale = copy.deepcopy(post)
    stale['bullets'][-1] = 'An older description'
    with pytest.raises(ValueError):
        verify_html(post, published_html(stale))
    # Matching text outside the target post does not establish publication.
    with pytest.raises(ValueError):
        verify_html(post, '<div>' + html.replace('<article', '<section').replace('</article>', '</section>') + '</div>')


def test_untrusted_version_cannot_change_link_destination():
    for version in ('../community', 'https://example.test', '0.2.0.dev45#other', ''):
        with pytest.raises(ValueError):
            note_url(version)


def test_live_check_identifies_client_and_verifies_response(monkeypatch):
    import urllib.request
    from pumpwizard_runner.release_notes import COMMUNITY_URL, verify_live

    post = note_for(__version__)
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return COMMUNITY_URL

        def read(self, maximum):
            return published_html(post).encode()[:maximum]

    def fetch(request, timeout):
        requests.append(request)
        assert timeout == 20
        return Response()

    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    verify_live(post)
    assert requests[0].full_url == COMMUNITY_URL
    assert requests[0].get_header('User-agent') == 'PumpWizardRunner-release-check/1.0'
