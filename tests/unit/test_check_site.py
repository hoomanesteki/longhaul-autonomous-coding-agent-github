"""scripts/check_site.py: the check of the rendered docs site that CI runs after each render."""

import ast
import importlib.util
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_site.py"
PREFIX = "/longhaul-autonomous-coding-agent-github/"
SITE_URL = "https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_site", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules, so register it before running it.
    sys.modules["check_site"] = module
    spec.loader.exec_module(module)
    return module


check_site = _load()

ALT = '<meta property="og:image:alt" content="The card">'
TWITTER_ALT = '<meta name="twitter:image:alt" content="The card">'

INDEX = """<!doctype html>
<html><head>
<title>Longhaul</title>
<link href="./styles.css" rel="stylesheet">
<link href="https://fonts.googleapis.com" rel="preconnect">
<meta property="og:image" content="{site_url}card.png">
<meta property="og:image:alt" content="The card">
</head><body>
<h1 id="top">Longhaul</h1>
<a href="guide/setup.html#install">setup</a>
<a href="#top">top</a>
<a href="#">menu</a>
<a href="https://example.com/x">elsewhere</a>
<a href="mailto:someone@example.com">mail</a>
<img src="card.png" alt="The card">
<script>var text = "a dash \u2014 in code is fine";</script>
</body></html>
"""

SETUP = """<!doctype html>
<html><head><title>Setup | Longhaul</title></head><body>
<h2 id="install">Install</h2>
<a href="../index.html#top">home</a>
<a href="../">home again</a>
</body></html>
"""


def _site(root: Path, **extra: str) -> Path:
    """A small rendered site that has no problem, plus the pages in `extra`."""
    pages = {
        "index.html": INDEX.format(site_url=SITE_URL),
        "guide/setup.html": SETUP,
        **extra,
    }
    for name, text in pages.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / "styles.css").write_text("", encoding="utf-8")
    (root / "card.png").write_bytes(b"png")
    return root


def _problems(root: Path) -> list[str]:
    return [str(p) for p in check_site.Site(root, PREFIX, SITE_URL).problems()]


def _page(body: str) -> str:
    return f"<!doctype html><html><head><title>Page</title></head><body>{body}</body></html>"


def test_a_site_whose_links_all_resolve_has_no_problems(tmp_path: Path) -> None:
    assert _problems(_site(tmp_path)) == []


def test_a_broken_link_is_caught(tmp_path: Path) -> None:
    site = _site(tmp_path, **{"broken.html": _page('<a href="guide/missing.html">x</a>')})
    assert _problems(site) == ["broken.html: guide/missing.html: no such file"]


def test_a_broken_anchor_is_caught(tmp_path: Path) -> None:
    body = '<a href="guide/setup.html#configure">x</a><a href="#nowhere">y</a>'
    site = _site(tmp_path, **{"anchors.html": _page(body)})
    assert _problems(site) == [
        "anchors.html: guide/setup.html#configure: no element with id 'configure' in "
        "guide/setup.html",
        "anchors.html: #nowhere: no element with id 'nowhere' in anchors.html",
    ]


def test_a_link_from_the_host_root_leaves_the_site_under_its_prefix(tmp_path: Path) -> None:
    # The page exists, but GitHub Pages serves it under the repository name, so /guide/... is a
    # page of another site. This is the mistake that renders fine and breaks once published.
    site = _site(tmp_path, **{"rooted.html": _page('<a href="/guide/setup.html">x</a>')})
    (problem,) = _problems(site)
    assert problem.startswith("rooted.html: /guide/setup.html: leaves the site")


def test_a_link_with_the_prefix_resolves(tmp_path: Path) -> None:
    # The 404 page links this way, because GitHub Pages serves it at any path.
    body = f'<a href="{PREFIX}guide/setup.html#install">x</a><img src="{PREFIX}card.png" alt="c">'
    assert _problems(_site(tmp_path, **{"404.html": _page(body)})) == []


def test_a_missing_image_or_stylesheet_is_caught(tmp_path: Path) -> None:
    head = '<link href="site_libs/missing.css" rel="stylesheet">'
    site = _site(tmp_path, **{"assets.html": _page(head + '<img src="media/gone.png" alt="g">')})
    assert _problems(site) == [
        "assets.html: site_libs/missing.css: no such file",
        "assets.html: media/gone.png: no such file",
    ]


def test_the_social_card_image_must_exist_on_the_site(tmp_path: Path) -> None:
    meta = f'<meta property="og:image" content="{SITE_URL}adr/card.png">{ALT}'
    site = _site(tmp_path, **{"card.html": _page(meta)})
    assert _problems(site) == [f"card.html: {SITE_URL}adr/card.png: no such file"]


def test_a_card_image_with_no_alt_text_is_caught(tmp_path: Path) -> None:
    # A page that sets its own card image loses the alt text the site gives the default one.
    meta = f'<meta property="og:image" content="{SITE_URL}card.png">'
    assert _problems(_site(tmp_path, **{"card.html": _page(meta)})) == [
        "card.html: og:image: a card image with no og:image:alt"
    ]


def test_a_double_slash_in_a_site_url_is_caught(tmp_path: Path) -> None:
    meta = f'<meta name="twitter:image" content="{SITE_URL}/card.png">{TWITTER_ALT}'
    (problem,) = _problems(_site(tmp_path, **{"card.html": _page(meta)}))
    assert problem.endswith("the path has an empty part (//)")


def test_a_dash_in_the_text_a_reader_sees_is_caught(tmp_path: Path) -> None:
    # Pandoc turns "--window" into an en dash, and Quarto joins a page title and the site name
    # with one in the social card. The dash in the script of the index page is not text.
    meta = '<meta property="og:title" content="Runbook \u2013 Longhaul">'
    body = "<p>run it with \u2013window 8000</p>"
    page = f"<!doctype html><html><head>{meta}</head><body>{body}</body></html>"
    problems = _problems(_site(tmp_path, **{"dash.html": page}))
    assert len(problems) == 2
    assert all(p.startswith("dash.html: ") and "en or em dash" in p for p in problems)
    assert "Runbook \u2013 Longhaul" in problems[0]


def test_an_image_with_no_alt_attribute_is_caught(tmp_path: Path) -> None:
    # An empty alt marks an image as decoration, which is a choice. A missing one is not.
    body = '<img src="card.png"><img src="card.png" alt="">'
    assert _problems(_site(tmp_path, **{"figure.html": _page(body)})) == [
        "figure.html: card.png: an image with no alt attribute"
    ]


def test_external_links_are_listed_and_not_checked_offline(tmp_path: Path) -> None:
    site = check_site.Site(_site(tmp_path), PREFIX, SITE_URL)
    assert site.external_links() == {"https://example.com/x": ["index.html"]}


class _Handler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:  # noqa: N802 (the name http.server calls)
        self.send_response(200 if self.path == "/ok" else 404)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self.do_HEAD()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_answer_asks_an_external_link(server: str) -> None:
    assert check_site.answer(f"{server}/ok") is None
    assert check_site.answer(f"{server}/gone") == "HTTP 404"


def test_main_fails_on_a_problem_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site = _site(tmp_path, **{"broken.html": _page('<a href="nope.html">x</a>')})
    assert check_site.main([str(site), "--prefix", PREFIX, "--site-url", SITE_URL]) == 1
    out = capsys.readouterr().out
    assert "broken.html: nope.html: no such file" in out
    assert out.strip().endswith("3 pages, 1 problems")


def test_main_passes_a_clean_site(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_site.main([str(_site(tmp_path)), "--prefix", PREFIX]) == 0
    assert "0 problems" in capsys.readouterr().out


def test_main_asks_for_a_render_first(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert check_site.main([str(tmp_path)]) == 2
    assert "Render the site first" in capsys.readouterr().err


def test_the_script_stays_valid_on_python_3_9() -> None:
    # CI runs it with the runner's python3, outside the uv environment.
    # The future import keeps `X | None` annotations from being evaluated.
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source, feature_version=(3, 9))
    assert "from __future__ import annotations" in source
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)
