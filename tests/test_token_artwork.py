from xml.etree import ElementTree as ET

import pytest

from pumpwizard_runner.token_artwork import SVG, sanitize_token_svg


def test_static_svg_keeps_logo_shapes_and_removes_active_or_external_content():
    body = b'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
      width="1000" height="1000" viewBox="0 0 100 100" onload="attack()">
      <script>attack()</script><foreignObject><div>unsafe</div></foreignObject>
      <image href="https://evil.example/image.png" />
      <defs><linearGradient id="shade"><stop offset="0%" stop-color="#fff" /></linearGradient></defs>
      <g style="fill:#ff00aa; filter:url(https://evil.example/filter)">
        <path d="M0 0 L100 0 L50 100 Z" onclick="attack()" />
        <circle id="mark" cx="50" cy="50" r="10" fill="url(#shade)" />
        <use xlink:href="https://evil.example/remote.svg" />
        <use xlink:href="#mark" />
      </g></svg>'''
    cleaned = sanitize_token_svg(body)
    root = ET.fromstring(cleaned)
    assert root.get('viewBox') == '0 0 100 100'
    assert (root.get('width'), root.get('height')) == ('64', '64')
    assert root.find(f'{{{SVG}}}g').get('fill') == '#ff00aa'
    assert root.find(f'.//{{{SVG}}}path') is not None
    assert root.find(f'.//{{{SVG}}}circle').get('fill') == 'url(#shade)'
    assert b'evil.example' not in cleaned and b'attack' not in cleaned and b'foreignObject' not in cleaned
    assert root.findall(f'.//{{{SVG}}}use')[-1].get('href') == '#mark'


@pytest.mark.parametrize('body', [
    b'<!DOCTYPE svg [<!ENTITY x "expansion">]><svg><path d="M0 0 L1 1" /></svg>',
    b'<svg xmlns="https://evil.example/namespace"><path d="M0 0 L1 1" /></svg>',
    b'<svg><image href="https://evil.example/logo.png" /></svg>',
    b'<svg>' + b'<g>' * 1000 + b'<path d="M0 0 L1 1" />' + b'</g>' * 1000 + b'</svg>',
    b'<svg>' + b' ' * 1_000_000 + b'</svg>',
], ids=['entities', 'foreign-namespace', 'external-image-only', 'excessive-depth', 'excessive-size'])
def test_unsupported_or_unbounded_svg_is_rejected(body):
    with pytest.raises(ValueError):
        sanitize_token_svg(body)
