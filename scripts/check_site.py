"""Check the rendered docs site the way GitHub Pages serves it.

    python3 scripts/check_site.py site/_site --prefix /longhaul-autonomous-coding-agent-github/

GitHub Pages serves the site under the repository name, not at the root of the host. A link
that starts with "/" therefore leaves the site, and the render does not notice. This script
reads every HTML page under the folder, as a server under `prefix` would serve them, and
reports:

* each link, image, script or stylesheet that does not reach a file of the site
* each "#anchor" that names no element of its page
* each en or em dash in the text a reader sees, which the voice guide bans (docs/style.md).
  Pandoc makes them out of "--" and "---", and Quarto out of a page title and the site name
* each image with no alt attribute, which leaves a screen reader with no name for it. Pandoc
  drops the alt text of a figure whose caption says the same

With --site-url, absolute links to the site itself, such as the image of the social card, are
checked the same way. `--external` also asks every http(s) link outside the site for an answer.
That is slow and depends on the network, so CI leaves it out; `make site-links` runs it.

It runs with whatever python3 is on the PATH (the docs job of CI has no uv environment), so it
uses only the standard library and stays valid on Python 3.9.
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

# The attributes that name another file or page, by tag. srcset is read on its own.
REFERENCE_ATTRS = {
    "a": ("href",),
    "area": ("href",),
    "link": ("href",),
    "img": ("src",),
    "script": ("src",),
    "source": ("src",),
    "iframe": ("src",),
    "video": ("src", "poster"),
    "audio": ("src",),
}
# <meta> tags whose content is a URL (the social card) or text a reader sees in a card.
META_URLS = {"og:image", "og:url", "twitter:image"}
META_TEXT = {"description", "og:title", "og:description", "twitter:title", "twitter:description"}
# Links of these kinds are not files of the site.
SKIPPED_SCHEMES = {"mailto", "tel", "javascript", "data"}
# rel values of <link> that point at other hosts by design.
SKIPPED_RELS = {"preconnect", "dns-prefetch"}
# Elements whose text is code or data and not prose.
NOT_PROSE = {"script", "style", "template"}
DASHES = re.compile("[\u2013\u2014]")
USER_AGENT = "site-link-check/1.0"


@dataclass(frozen=True)
class Problem:
    page: str
    what: str
    reason: str

    def __str__(self) -> str:
        return f"{self.page}: {self.what}: {self.reason}"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.urls: list[str] = []
        self.text: list[str] = []
        self.unnamed_images: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._read(tag, {name: value for name, value in attrs if value is not None})
        if tag in NOT_PROSE:
            self._skipping += 1

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._read(tag, {name: value for name, value in attrs if value is not None})

    def handle_endtag(self, tag: str) -> None:
        if tag in NOT_PROSE and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.text.append(data)

    def _read(self, tag: str, values: dict[str, str]) -> None:
        for name in ("id", "name") if tag == "a" else ("id",):
            if values.get(name):
                self.ids.add(values[name])
        if tag == "link" and set(values.get("rel", "").split()) & SKIPPED_RELS:
            return
        for name in REFERENCE_ATTRS.get(tag, ()):
            if name in values:
                self.urls.append(values[name])
        for candidate in values.get("srcset", "").split(","):
            if candidate.strip():
                self.urls.append(candidate.split()[0])
        if tag == "meta" and values.get("content"):
            kind = values.get("property", values.get("name"))
            if kind in META_URLS:
                self.urls.append(values["content"])
            elif kind in META_TEXT:
                self.text.append(values["content"])
        if values.get("alt"):
            self.text.append(values["alt"])
        elif tag == "img" and "alt" not in values:
            self.unnamed_images.append(values.get("src", ""))


@dataclass
class Site:
    root: Path
    prefix: str = "/"
    site_url: str = ""
    pages: dict[str, _PageParser] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.prefix = "/" + self.prefix.strip("/") + "/" if self.prefix.strip("/") else "/"
        if self.site_url:
            self.site_url = self.site_url.rstrip("/") + "/"
        for path in sorted(self.root.rglob("*.html")):
            parser = _PageParser()
            parser.feed(path.read_text(encoding="utf-8", errors="replace"))
            self.pages[path.relative_to(self.root).as_posix()] = parser

    def local(self, url: str) -> str | None:
        """The URL of a reference into the site, or None when it points elsewhere."""
        parts = urlsplit(url)
        if parts.scheme in SKIPPED_SCHEMES:
            return None
        if parts.scheme or parts.netloc:
            if self.site_url and (url + "/").startswith(self.site_url):
                return self.prefix + url[len(self.site_url) :]
            return None
        return url

    def file_for(self, path: str) -> str | None:
        """The file under the root that a server gives for a URL path, if there is one."""
        if not path.startswith(self.prefix) or "//" in path:
            return None
        relative = unquote(path[len(self.prefix) :])
        candidate = self.root / relative
        if relative == "" or relative.endswith("/") or candidate.is_dir():
            candidate = candidate / "index.html"
            relative = (relative.rstrip("/") + "/index.html").lstrip("/")
        try:
            candidate.resolve().relative_to(self.root.resolve())
        except ValueError:
            return None
        return relative if candidate.is_file() else None

    def check(self, page: str, url: str) -> str | None:
        """Why a reference of `page` does not resolve, or None when it does."""
        target = self.local(url.strip())
        if target is None:
            return None
        parts = urlsplit(urljoin("http://site" + self.prefix + page, target))
        found = self.file_for(parts.path)
        if found is None:
            if not parts.path.startswith(self.prefix):
                return f"leaves the site, which is served under {self.prefix}"
            if "//" in parts.path:
                return "the path has an empty part (//)"
            return "no such file"
        fragment = unquote(parts.fragment)
        if fragment and found in self.pages and fragment not in self.pages[found].ids:
            return f"no element with id {fragment!r} in {found}"
        return None

    def problems(self) -> list[Problem]:
        found = []
        for page, parser in self.pages.items():
            for url in parser.urls:
                reason = self.check(page, url)
                if reason:
                    found.append(Problem(page, url, reason))
            for src in parser.unnamed_images:
                found.append(Problem(page, src, "an image with no alt attribute"))
            for text in parser.text:
                for dash in DASHES.finditer(text):
                    start = max(dash.start() - 30, 0)
                    snippet = " ".join(text[start : dash.end() + 30].split())
                    found.append(Problem(page, repr(snippet), "an en or em dash (docs/style.md)"))
        return found

    def external_links(self) -> dict[str, list[str]]:
        """Each http(s) URL outside the site, without its anchor, and the pages that link it."""
        links: dict[str, list[str]] = defaultdict(list)
        for page, parser in self.pages.items():
            for url in parser.urls:
                if urlsplit(url.strip()).scheme in ("http", "https") and self.local(url) is None:
                    links[url.strip().split("#")[0]].append(page)
        return dict(links)


def answer(url: str, timeout: float = 20.0) -> str | None:
    """Why an external URL does not answer, or None when it does. HEAD first, then GET."""
    last = ""
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                return None
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code}"
        except (urllib.error.URLError, OSError) as error:
            last = str(getattr(error, "reason", error))
    return last


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the rendered docs site.")
    parser.add_argument("site", type=Path, help="the rendered site, for example site/_site")
    parser.add_argument("--prefix", default="/", help="the URL path the site is served under")
    parser.add_argument("--site-url", default="", help="the full URL of the published site")
    parser.add_argument("--external", action="store_true", help="also ask every external link")
    args = parser.parse_args(argv)

    if not (args.site / "index.html").is_file():
        print(f"check_site: no index.html in {args.site}. Render the site first.", file=sys.stderr)
        return 2
    site = Site(args.site, args.prefix, args.site_url)
    problems = site.problems()
    if args.external:
        for url, pages in sorted(site.external_links().items()):
            reason = answer(url)
            if reason:
                problems.append(Problem(pages[0], url, reason))
    for problem in problems:
        print(problem)
    print(f"check_site: {len(site.pages)} pages, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
