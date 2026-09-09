from __future__ import annotations

import http.client
import json
from typing import Any


class VoiceGatewayError(RuntimeError):
    pass


class VoiceGateway:
    """Small stdlib proxy to the voice process running in its own environment."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.host = host
        self.port = int(port)

    def status(self) -> dict[str, Any]:
        status, _headers, payload = self._request("GET", "/v1/status", timeout=1.5)
        value = self._decode_json(payload)
        if status != 200:
            raise VoiceGatewayError(str(value.get("error") or f"HTTP {status}"))
        return value

    def start_session(self, language: str = "zh") -> dict[str, Any]:
        payload = json.dumps({"language": language}, ensure_ascii=False).encode("utf-8")
        status, _headers, payload = self._request(
            "POST",
            "/v1/start",
            body=payload,
            timeout=2.0,
        )
        value = self._decode_json(payload)
        if status not in (200, 202):
            raise VoiceGatewayError(str(value.get("message") or value.get("error") or f"HTTP {status}"))
        return value

    def set_local_speech(self, enabled: bool) -> dict[str, Any]:
        status, _headers, payload = self._request(
            "POST", "/v1/local-speech",
            body=json.dumps({"enabled": enabled}).encode(), timeout=5.0,
        )
        value = self._decode_json(payload)
        if status not in (200, 202):
            raise VoiceGatewayError(str(value.get("message") or value.get("error") or f"HTTP {status}"))
        return value

    def finish_input(self) -> dict[str, Any]:
        status, _headers, payload = self._request(
            "POST",
            "/v1/finish-input",
            body=b"{}",
            timeout=2.0,
        )
        value = self._decode_json(payload)
        if status not in (200, 202):
            raise VoiceGatewayError(str(value.get("message") or value.get("error") or f"HTTP {status}"))
        return value

    def submit_text(self, text: str, language: str = "zh") -> dict[str, Any]:
        payload = json.dumps(
            {"text": text, "language": language},
            ensure_ascii=False,
        ).encode("utf-8")
        status, _headers, payload = self._request(
            "POST",
            "/v1/text",
            body=payload,
            timeout=2.0,
        )
        value = self._decode_json(payload)
        if status not in (200, 202):
            raise VoiceGatewayError(str(value.get("message") or value.get("error") or f"HTTP {status}"))
        return value

    def cancel(self) -> dict[str, Any]:
        status, _headers, payload = self._request(
            "POST",
            "/v1/cancel",
            body=b"{}",
            timeout=2.0,
        )
        value = self._decode_json(payload)
        if status not in (200, 202):
            raise VoiceGatewayError(str(value.get("message") or value.get("error") or f"HTTP {status}"))
        return value

    def audio(self, audio_id: int) -> bytes:
        status, _headers, payload = self._request(
            "GET",
            f"/v1/audio/{int(audio_id)}.wav",
            timeout=5.0,
        )
        if status != 200:
            value = self._decode_json(payload)
            raise VoiceGatewayError(str(value.get("error") or f"HTTP {status}"))
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        timeout: float,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        except (OSError, http.client.HTTPException) as exc:
            raise VoiceGatewayError(f"小达语音服务不可用：{exc}") from exc
        finally:
            connection.close()

    @staticmethod
    def _decode_json(payload: bytes) -> dict[str, Any]:
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VoiceGatewayError("小达语音服务返回了无效数据") from exc
        if not isinstance(value, dict):
            raise VoiceGatewayError("小达语音服务返回格式错误")
        return value
