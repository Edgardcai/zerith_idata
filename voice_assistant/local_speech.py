"""Explicit, on-demand control of the two user-level GPU speech services."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from urllib.request import ProxyHandler, build_opener


UNITS = ("zerith-chinese-asr.service", "zerith-chinese-tts.service")


class LocalSpeechServices:
    def __init__(self, asr_url: str, tts_url: str, *, enabled: bool = True,
                 cloud_configured: bool = True) -> None:
        self._urls = (asr_url, tts_url)
        self._enabled = enabled
        self._lock = threading.RLock()
        self._commands = threading.Lock()
        self._closed = threading.Event()
        self._thread = None
        self._starting_at = None
        self._status = dict(phase="checking", ready=False, asr_ready=False,
                            tts_ready=False, managed=enabled,
                            cloud_configured=cloud_configured, error="")

    def start_monitor(self) -> None:
        # Monitoring never starts a model or changes systemd configuration.
        self._thread = threading.Thread(target=self._monitor, daemon=True,
                                        name="local-speech-status")
        self._thread.start()

    def close(self) -> None:
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=4)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._status)

    def ready(self) -> bool:
        return bool(self.snapshot()["ready"])

    @staticmethod
    def _systemctl(*args: str) -> str:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True,
                                text=True, timeout=3, check=False)
        if result.returncode:
            raise RuntimeError(result.stderr.strip()[:240] or "无法管理本地语音服务")
        return result.stdout

    @staticmethod
    def _healthy(url: str) -> bool:
        try:
            with build_opener(ProxyHandler({})).open(url.rstrip("/") + "/health", timeout=0.3) as response:
                data = json.loads(response.read(65536))
                return response.status == 200 and isinstance(data, dict) and data.get("ok") is True
        except Exception:
            return False

    def set_enabled(self, enabled: bool) -> tuple[bool, str]:
        if not self._enabled:
            return False, "本地语音管理未启用"
        with self._commands:
            try:
                # --no-block returns after queueing; model loading is observed by health checks.
                self._systemctl("--no-block", "start" if enabled else "stop", *UNITS)
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                with self._lock:
                    self._status.update(error=str(exc), ready=False, phase="error")
                return False, str(exc)
            with self._lock:
                self._starting_at = time.monotonic() if enabled else None
                self._status.update(phase="starting" if enabled else "stopping",
                                    ready=False, asr_ready=False, tts_ready=False, error="")
        return True, "正在加载本地语音，期间继续使用云端" if enabled else "正在停止本地语音并释放显存"

    def _refresh(self) -> None:
        with self._commands:
            if not self._enabled:
                with self._lock:
                    self._status.update(phase="off", ready=False)
                return
            raw = self._systemctl("show", "--property=ActiveState", "--value", *UNITS)
            states = raw.split()
            asr, tts = (self._healthy(url) for url in self._urls)
            ready = asr and tts and states == ["active", "active"]
            with self._lock:
                error = self._status["error"]
                if ready:
                    self._starting_at = None
                    error = ""
                    phase = "ready"
                elif self._starting_at is not None:
                    if time.monotonic() - self._starting_at > 180:
                        self._systemctl("--no-block", "stop", *UNITS)
                        self._starting_at = None
                        error = "本地模型加载超过 180 秒，已请求停止；继续使用云端语音"
                        phase = "error"
                    else:
                        phase = "starting"
                elif "deactivating" in states or self._status["phase"] == "stopping" and any(s == "active" for s in states):
                    phase = "stopping"
                elif "failed" in states or error:
                    phase = "error"
                    error = error or "本地语音服务启动失败；继续使用云端语音"
                elif any(s in {"active", "activating", "reloading"} for s in states):
                    phase = "starting"
                else:
                    phase = "off"
                self._status.update(phase=phase, ready=ready, asr_ready=asr,
                                    tts_ready=tts, error=error)

    def _monitor(self) -> None:
        while not self._closed.is_set():
            try:
                self._refresh()
            except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
                with self._lock:
                    self._status.update(phase="error", ready=False, error=str(exc))
            self._closed.wait(1)
