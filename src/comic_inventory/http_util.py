"""urllib wrapper that returns HTTP errors instead of raising them."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

USER_AGENT = "comic-inventory/0.1 (cgc-listing)"


class HttpResult:
    def __init__(self, status: int, body: bytes, content_type: str) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode())


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: int = 60,
) -> HttpResult:
    merged = {"User-Agent": USER_AGENT}
    if headers:
        merged.update(headers)
    req = urllib.request.Request(url, data=data, headers=merged, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            return HttpResult(resp.status, resp.read(), content_type)
    except urllib.error.HTTPError as exc:
        content_type = ""
        if exc.headers is not None:
            content_type = exc.headers.get("Content-Type", "")
        return HttpResult(exc.code, exc.read(), content_type)
