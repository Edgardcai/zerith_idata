"""Read-only battery telemetry, independent of SDK ownership."""

import json
import math
import threading
import time
from urllib.request import ProxyHandler, build_opener


class BatteryMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._value = None
        self._received = 0.0
        self._opener = build_opener(ProxyHandler({}))
        self._thread = threading.Thread(target=self._run, name="battery-monitor", daemon=True)
        self._thread.start()

    @staticmethod
    def normalize(data):
        if not isinstance(data, dict) or data.get("fresh") is not True:
            return None
        percent = data.get("percent")
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            return None
        if not math.isfinite(percent) or not 0 <= percent <= 100:
            return None
        return {"percent": percent, "charging": data.get("charging") is True}

    def _run(self):
        while not self._stop.is_set():
            try:
                with self._opener.open("http://127.0.0.1:25120/battery", timeout=1.5) as response:
                    value = self.normalize(json.load(response))
            except Exception:
                value = None
            with self._lock:
                self._value = value
                self._received = time.monotonic()
            self._stop.wait(1)

    def snapshot(self):
        with self._lock:
            if self._value is None or time.monotonic() - self._received > 4:
                return None
            return dict(self._value)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
