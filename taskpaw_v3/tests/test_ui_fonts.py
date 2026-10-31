"""The UI serves its fonts from the bundle, with no runtime Google request (#379, AC 3).

`index.html` links the local `fonts.css`; every font file it names exists and is a
woff2; both OFL notices ship beside them; and the Tauri CSP allows `font-src 'self'`
while naming no Google host.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

V3 = Path(__file__).resolve().parents[1]
UI = V3 / "ui"
FONTS = UI / "public" / "fonts"


def test_index_html_has_no_external_reference() -> None:
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html
    assert 'href="/fonts/fonts.css"' in html


def test_fonts_css_names_local_woff2_files() -> None:
    css = (FONTS / "fonts.css").read_text(encoding="utf-8")
    urls = re.findall(r'url\("?([^")]+)"?\)', css)
    assert urls
    for url in urls:
        assert "//" not in url and not url.startswith("/")
        assert (FONTS / url).read_bytes()[:4] == b"wOF2"
    assert 'font-family: "Fira Sans"' in css
    assert 'font-family: "Fira Code"' in css


def test_ofl_notices_ship_beside_the_fonts() -> None:
    for name in ("OFL-FiraSans.txt", "OFL-FiraCode.txt"):
        assert "SIL OPEN FONT LICENSE" in (FONTS / name).read_text(encoding="utf-8")


def test_csp_allows_self_fonts_and_no_google_host() -> None:
    conf = json.loads(
        (V3 / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    csp = conf["app"]["security"]["csp"]
    assert "googleapis" not in csp and "gstatic" not in csp
    directives = {
        part.split()[0]: part.split()[1:] for part in csp.split(";") if part.strip()
    }
    assert "'self'" in directives["font-src"]
