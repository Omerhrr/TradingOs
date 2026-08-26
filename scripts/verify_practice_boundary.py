"""Verify that the local API still rejects the only real-mode route."""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1"
try:
    with urlopen(f"{base_url}/health", timeout=10) as response:
        health = json.load(response)
    request = Request(f"{base_url}/mode/enable-real", method="POST")
    try:
        urlopen(request, timeout=10)
        raise SystemExit("Boundary verification failed: real-mode endpoint did not reject the request.")
    except HTTPError as exc:
        if exc.code != 403:
            raise SystemExit(f"Boundary verification failed: expected 403 but received {exc.code}.") from exc
except URLError as exc:
    raise SystemExit(f"Boundary verification failed: {exc}") from exc

if health.get("live_execution") is not False:
    raise SystemExit("Boundary verification failed: health reports live execution enabled.")
print("Practice boundary verified: live execution disabled and real-mode endpoint rejected with 403.")
