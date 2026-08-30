"""A stand-in for the network, so the demo and the tests run offline.

Only the two functions that actually reach out are replaced - `fetch.get` and
`sources.search.search`. Everything above them is the real thing: the same HTML
parser, the same row binder, the same normaliser, the same scoring. That is the
point. A demo that fakes the extraction proves nothing about the extraction.
"""

from __future__ import annotations

from contextlib import contextmanager

from reachout import fetch
from reachout.sources import search as websearch


class FakeWeb:
    """A fixed corpus of pages, plus canned search results."""

    def __init__(self, pages: dict, results: dict | None = None):
        # `pages` maps a URL to its body. API endpoints live here too, as JSON
        # text - the GitHub client goes through the same `get`, so there is no
        # second kind of fixture to keep in step.
        self.pages = dict(pages)
        self.results = dict(results or {})
        self.requested: list[str] = []
        self.searched: list[str] = []

    # ------------------------------------------------------------------- HTTP
    def get(self, url: str, headers=None, use_cache=True, check_robots=True):
        self.requested.append(url)
        key = url.rstrip("/") or url
        body = self.pages.get(url, self.pages.get(key))
        if body is None:
            return fetch.Response(url=url, status=404, text="", error="HTTP 404")
        return fetch.Response(url=url, status=200, text=body)

    # ----------------------------------------------------------------- search
    def search(self, query: str, count: int = 10):
        self.searched.append(query)
        hits = []
        for needle, entries in self.results.items():
            if needle.lower() in query.lower():
                hits.extend(websearch.Hit(title=t, url=u, snippet=s, source="fake")
                            for t, u, s in entries)
        return hits[:count]


@contextmanager
def install(web: FakeWeb):
    """Patch the network boundary for the duration of the block.

    `search` is reached through the module object everywhere, so rebinding the
    attribute is enough. `get` was imported by name into each source module, so
    those bindings have to be replaced one at a time.
    """
    from reachout.sources import directory, documents, github, website

    holders = [(fetch, "get"), (website, "get"), (directory, "get"),
               (documents, "get"), (github, "get"), (websearch, "search")]
    saved = [(mod, attr, getattr(mod, attr)) for mod, attr in holders]
    for mod, attr in holders:
        setattr(mod, attr, web.search if attr == "search" else web.get)
    try:
        yield web
    finally:
        for mod, attr, value in saved:
            setattr(mod, attr, value)
