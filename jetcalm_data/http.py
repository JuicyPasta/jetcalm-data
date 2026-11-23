"""Polite HTTP: one User-Agent, kept-alive connections, timeouts, retries with backoff."""
import time

import requests

from . import USER_AGENT

_session = None


class NotFound(Exception):
    pass


def session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers["User-Agent"] = USER_AGENT
    return _session


def get(url, *, byte_range=None, retries=5, timeout=60, backoff=3.0, sleep=time.sleep):
    """GET url -> bytes. 404 raises NotFound at once; other errors retry.

    Connecting is given 10 s (AWC sometimes drops a connection attempt; a retry
    on a fresh connection works), reading `timeout` seconds.
    """
    headers = {}
    if byte_range is not None:
        start, end = byte_range
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    last = None
    for attempt in range(retries):
        try:
            r = session().get(url, headers=headers, timeout=(10, timeout))
            if r.status_code in (403, 404):
                raise NotFound(f"{r.status_code} {url}")
            if r.status_code not in (200, 206):
                raise IOError(f"HTTP {r.status_code}")
            data = r.content
            if byte_range is not None:
                if r.status_code != 206:
                    raise IOError("server ignored the byte range")
                if byte_range[1] is not None and len(data) != byte_range[1] - byte_range[0] + 1:
                    raise IOError(f"short range read: {len(data)} bytes")
            return data
        except NotFound:
            raise
        except (requests.RequestException, IOError) as e:
            last = e
        if attempt < retries - 1:
            sleep(backoff * (attempt + 1))
    raise IOError(f"GET failed after {retries} tries: {url}: {last}")
