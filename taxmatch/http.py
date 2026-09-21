"""Κοινός HTTP client: User-Agent, timeouts, retries με backoff."""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import APP_NAME, __version__

USER_AGENT = (
    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {APP_NAME}/{__version__} (+https://github.com/scanmydata/TaxMatch-by-Scanmydata)"
)
DEFAULT_TIMEOUT = 30


def make_session(retries: int = 3) -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"})
    retry = Retry(total=retries, connect=retries, read=retries, backoff_factor=1.0,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
