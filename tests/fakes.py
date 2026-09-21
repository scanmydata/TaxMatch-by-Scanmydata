"""Ψεύτικο HTTP session για tests: δρομολογεί ανά URL, καταγράφει τα αιτήματα."""
from __future__ import annotations

import json as _json
from typing import Any, Callable, Optional

import requests


class FakeResponse:
    def __init__(self, content: bytes | str = b"", status: int = 200, headers: Optional[dict] = None, json_data: Any = None):
        if json_data is not None:
            content = _json.dumps(json_data).encode()
            headers = {"content-type": "application/json", **(headers or {})}
        self.content = content.encode("utf-8") if isinstance(content, str) else content
        self.status_code = status
        self.headers = {"content-type": "text/xml", **(headers or {})}

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self):
        self.routes: list[tuple[str, Callable[..., FakeResponse]]] = []
        self.calls: list[tuple[str, str, dict]] = []
        self.headers: dict = {}

    def route(self, substring: str, handler: Callable[..., FakeResponse] | FakeResponse):
        self.routes.append((substring, handler if callable(handler) else (lambda *a, **k: handler)))
        return self

    def _dispatch(self, method: str, url: str, **kw) -> FakeResponse:
        self.calls.append((method, url, kw))
        for sub, handler in self.routes:
            if sub in url:
                return handler(url, **kw)
        return FakeResponse(b"<html>not found</html>", 404, {"content-type": "text/html"})

    def get(self, url, **kw):
        return self._dispatch("GET", url, **kw)

    def post(self, url, **kw):
        return self._dispatch("POST", url, **kw)

    def mount(self, *a, **k):
        pass
