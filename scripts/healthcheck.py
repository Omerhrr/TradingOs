"""Local service health probe used before and after moving TradingOS to a VDS."""

from __future__ import annotations

import json
import sys
from urllib.error import URLError
from urllib.request import urlopen


url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/api/v1/health"
try:
    with urlopen(url, timeout=10) as response:
        payload = json.load(response)
except URLError as exc:
    raise SystemExit(f"TradingOS health check failed: {exc}") from exc

if payload.get("status") != "ok":
    raise SystemExit(f"TradingOS returned an unhealthy payload: {payload}")
if payload.get("live_execution") is not False:
    raise SystemExit("TradingOS health check refused: live execution must remain disabled.")
print(json.dumps(payload, indent=2))
