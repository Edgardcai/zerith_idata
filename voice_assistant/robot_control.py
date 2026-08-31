from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
from typing import Any, Protocol


class RobotControlError(RuntimeError):
    pass


@dataclass(frozen=True)
class RobotCommandIntent:
    name: str
    arguments: dict[str, Any]


class RobotControlPort(Protocol):
    @property
    def available(self) -> bool: ...

    def tool_definitions(self) -> list[dict[str, Any]]: ...

    def execute(self, intent: RobotCommandIntent) -> dict[str, Any]: ...


class DisabledRobotControl:
    """Safe default boundary between language understanding and real motion.

    A future adapter must call the existing web controller/sole H1Robot owner.
    It must not construct a second SDK client from the voice process.
    """

    @property
    def available(self) -> bool:
        return False

    def tool_definitions(self) -> list[dict[str, Any]]:
        return []

    def execute(self, intent: RobotCommandIntent) -> dict[str, Any]:
        raise RobotControlError(f"机器人动作接口尚未启用，已拒绝命令：{intent.name}")


class WebRobotControl:
    """Loopback adapter to the web process that exclusively owns H1Robot."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8766) -> None:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("机器人语音控制只允许连接回环地址")
        self.host = host
        self.port = int(port)

    @property
    def available(self) -> bool:
        try:
            return bool(self.status().get("enabled"))
        except RobotControlError:
            return False

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/v1/status", timeout=1.0)

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "execute_robot_motion",
                "description": "执行网页明确开启的受限机器人动作",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "enum": [
                                "stop",
                                "forward",
                                "backward",
                                "turn_left",
                                "turn_right",
                                "turn_around",
                                "wave",
                                "handshake",
                            ],
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            }
        ]

    def execute(self, intent: RobotCommandIntent) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/execute",
            {"command": intent.name},
            timeout=3.0,
        )

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> dict[str, Any]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Connection": "close"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RobotControlError("机器人控制桥返回了无效数据") from exc
            if not isinstance(value, dict):
                raise RobotControlError("机器人控制桥返回格式错误")
            if response.status not in (200, 202):
                raise RobotControlError(str(value.get("error") or f"HTTP {response.status}"))
            return value
        except (OSError, http.client.HTTPException) as exc:
            raise RobotControlError(f"机器人语音控制桥不可用：{exc}") from exc
        finally:
            connection.close()


__all__ = [
    "DisabledRobotControl",
    "RobotCommandIntent",
    "RobotControlError",
    "RobotControlPort",
    "WebRobotControl",
]
