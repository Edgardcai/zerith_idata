"""VLM-based scene description via OpenAI-compatible API.

Compatible with any OpenAI-format provider: AIHubMix, OpenAI, Azure, etc.
The openai package is a soft dependency — it is only imported when VLM is
actually invoked, so the rest of quality_pipeline stays lightweight.

Configuration (in robot_profiles/g2.yaml):

    processing:
      annotations:
        vlm:
          enabled: true
          base_url: https://aihubmix.com/v1
          model: qwen3-vl-flash          # any vision-capable model on the platform
          api_key_env: AIHUBMIX_API_KEY # env-var name that holds the key
          max_tokens: 256
          language: zh                  # zh | en — language for scene description
          timeout_sec: 30

Usage:
    client = VLMClient.from_profile(profile_raw)
    desc = client.describe_scene(image_path, detections, instruction)
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from .base import Detection

# System prompt templates
_SYSTEM_ZH = (
    "你是一个机器人视觉标注助手。根据提供的机器人头部相机图像和检测到的目标对象，"
    "用2-3句话描述当前场景：目标物品的位置、相对关系和周边环境。"
    "描述要具体、简洁，聚焦于任务执行相关的空间信息。"
    "检测框和结构化位置提示是空间关系的主要依据；不要把整张图的上/中/下直接当成货架层级。"
    "可以根据图像整体视觉弱判断目标约在左侧或右侧货架区域，但必须使用“约在/视觉上”等不确定措辞。"
    "不要枚举非目标商品名称，不要推断购物车是否为空或是否已有物品，除非输入明确要求。"
    "不要描述机器人本身。必须用中文回答。"
)
_SYSTEM_ALOHA_TABLE_ZH = (
    "你是一个机器人视觉标注助手。根据提供的桌面相机图像和检测到的饮料目标，"
    "只用一句中文描述桌子上饮料的数量和左右位置。"
    "必须严格使用模板：“桌子有<N>瓶饮料，饮料<A>放在左边，饮料<B>放在右边。”"
    "如果只能确认一瓶饮料，则使用：“桌子有<N>瓶饮料，饮料<A>放在<左边/中间/右边>。”"
    "如果有多于两瓶饮料，只描述最左边和最右边的饮料。"
    "不要描述货架、购物车、机器人、机械臂或其它无关环境。"
)
_SYSTEM_EN = (
    "You are a robot vision annotation assistant. Given a robot head-camera image "
    "and detected target objects, write 2-3 sentences describing the current scene: "
    "positions of target items, their spatial relationships, and the surrounding environment. "
    "Use detections and structured position hints as the main evidence. Do not treat "
    "full-image upper/middle/lower coordinates as shelf levels. Be specific and concise, "
    "focusing on task-relevant spatial information. You may weakly describe whether a "
    "target appears to be in the left or right shelf area from the image, but use approximate wording. "
    "Do not enumerate non-target product "
    "names and do not infer whether the cart is empty or already contains items unless "
    "explicitly requested. "
    "Do not describe the robot itself."
)

_HAND_TARGET_SYSTEM_ZH = (
    "你是一个机器人操作视频标注助手。你的任务是根据抓取瞬间的图像，"
    "识别机器人左手或右手实际抓取/接触的是哪个目标物体。"
    "必须只依据图像证据判断，不要根据任务目标顺序猜测。"
    "回答必须是严格 JSON，不要输出解释性文字。"
)
_HAND_TARGET_SYSTEM_EN = (
    "You are a robot manipulation annotation assistant. Given images from grasp "
    "moments, identify which target object the robot left or right hand is actually "
    "grasping or touching. Use visual evidence only; do not infer from task target "
    "order. Return strict JSON without explanatory prose."
)


class VLMClient:
    """Calls a VLM through an OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://aihubmix.com/v1",
        max_tokens: int = 256,
        language: str = "zh",
        timeout: float = 30.0,
        scene_layout: dict[str, Any] | None = None,
        scene_prompt_style: str = "default",
    ) -> None:
        try:
            import openai  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "openai package is required for VLM scene description. "
                "Install it inside a venv/conda env, for example: "
                "python3 -m venv .venv-data && source .venv-data/bin/activate && "
                "python -m pip install -r quality_pipeline/requirements-vision.txt"
            ) from exc
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self._model = model
        self._max_tokens = max_tokens
        self._language = language
        self._scene_layout = scene_layout or {}
        self._scene_prompt_style = scene_prompt_style
        self._system = _system_prompt(language, scene_prompt_style)

    @classmethod
    def from_profile(cls, profile_raw: dict[str, Any]) -> "VLMClient":
        """Build from profile processing.annotations.vlm config block."""
        annotations_cfg = (
            profile_raw.get("processing", {})
            .get("annotations", {})
        )
        cfg = annotations_cfg.get("vlm", {})
        scene_prompt_style = str(cfg.get("scene_prompt_style") or "").strip()
        if not scene_prompt_style and _is_aloha_profile(profile_raw):
            scene_prompt_style = "aloha_table"
        key_env = str(cfg.get("api_key_env") or "AIHUBMIX_API_KEY")
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise EnvironmentError(
                f"VLM API key not found. Set the environment variable: {key_env}"
            )
        _validate_api_key(api_key, key_env)
        return cls(
            api_key=api_key,
            model=_normalize_model_id(str(cfg.get("model") or "qwen3-vl-flash")),
            base_url=str(cfg.get("base_url") or "https://aihubmix.com/v1"),
            max_tokens=int(cfg.get("max_tokens") or 256),
            language=str(cfg.get("language") or "zh"),
            timeout=float(cfg.get("timeout_sec") or 30.0),
            scene_layout=dict(cfg.get("scene_layout") or annotations_cfg.get("scene_layout") or {}),
            scene_prompt_style=scene_prompt_style or "default",
        )

    def describe_scene(
        self,
        image_path: Path,
        detections: list[Detection],
        instruction: str = "",
        target_labels: list[str] | None = None,
    ) -> str:
        """Call the VLM and return a natural-language scene description.

        Args:
            image_path:  path to the JPEG/PNG key frame
            detections:  YOLO detections on this frame (may be empty)
            instruction: the episode task instruction for context
        """
        b64 = _encode_image(image_path)
        if b64 is None:
            return "not_generated"

        user_text = _build_user_prompt(
            detections,
            instruction,
            scene_layout=self._scene_layout,
            language=self._language,
            target_labels=target_labels,
            scene_prompt_style=self._scene_prompt_style,
        )

        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": self._system},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{b64}",
                                    "detail": "low",   # cheaper, fast
                                },
                            },
                            {"type": "text", "text": user_text},
                        ],
                    },
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            return text if text else "not_generated"
        except Exception as exc:
            import warnings
            warnings.warn(f"VLM scene description failed: {exc}", stacklevel=2)
            return "not_generated"

    def identify_hand_targets(
        self,
        grasp_events: list[dict[str, Any]],
        instruction: str = "",
        candidate_labels: list[str] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Identify target object per hand from grasp-event images.

        ``grasp_events`` items should include ``hand`` (left/right),
        ``frame_idx``, and either ``image_path`` or ``images`` entries. The
        return shape is ``{hand: {target_object, confidence, evidence}}``.
        """
        content = _build_hand_target_content(
            grasp_events,
            instruction=instruction,
            candidate_labels=candidate_labels,
            language=self._language,
        )
        if not content:
            return {}
        system = _HAND_TARGET_SYSTEM_ZH if self._language == "zh" else _HAND_TARGET_SYSTEM_EN
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": content},
                ],
            )
            text = (response.choices[0].message.content or "").strip()
            return _parse_hand_target_response(text, candidate_labels=candidate_labels)
        except Exception as exc:
            import warnings
            warnings.warn(f"VLM hand-target assignment failed: {exc}", stacklevel=2)
            return {}

    def close(self) -> None:
        pass


def _build_hand_target_content(
    grasp_events: list[dict[str, Any]],
    instruction: str,
    candidate_labels: list[str] | None,
    language: str,
) -> list[dict[str, Any]]:
    prompt = _build_hand_target_prompt(grasp_events, instruction, candidate_labels, language)
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    image_count = 0
    for event in grasp_events:
        event_id = str(event.get("event_id") or f"{event.get('hand', 'unknown')}_{event.get('frame_idx', '')}")
        hand = str(event.get("hand") or "unknown")
        frame_idx = event.get("frame_idx")
        grasp_start = event.get("grasp_start_frame")
        sample_source = event.get("sample_source")
        for camera, image_path in _iter_event_images(event):
            b64 = _encode_image(image_path)
            if b64 is None:
                continue
            camera_text = f", camera={camera}" if camera else ""
            sample_text = (
                f", grasp_start_frame={grasp_start}, sample_source={sample_source}"
                if grasp_start is not None or sample_source
                else ""
            )
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"event_id={event_id}, hand={hand}, frame_idx={frame_idx}"
                        f"{sample_text}{camera_text}"
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64}",
                        "detail": "low",
                    },
                }
            )
            image_count += 1
    return content if image_count else []


def _build_hand_target_prompt(
    grasp_events: list[dict[str, Any]],
    instruction: str,
    candidate_labels: list[str] | None,
    language: str,
) -> str:
    zh = language == "zh"
    candidates = [label for label in (candidate_labels or []) if label]
    event_lines = []
    for event in grasp_events:
        event_id = str(event.get("event_id") or f"{event.get('hand', 'unknown')}_{event.get('frame_idx', '')}")
        sample_source = event.get("sample_source")
        grasp_start = event.get("grasp_start_frame")
        sample_text = (
            f", grasp_start_frame={grasp_start}, sample_source={sample_source}"
            if grasp_start is not None or sample_source
            else ""
        )
        event_lines.append(
            f"  - event_id={event_id}, hand={event.get('hand')}, "
            f"frame_idx={event.get('frame_idx')}{sample_text}"
        )
    if zh:
        lines = [
            "请识别每个抓取事件中指定 hand 实际抓取或接触的目标物。",
            "不要根据任务目标顺序或左右手先验猜测；只根据图像中手、夹爪和物体的接触关系判断。",
            "frame_idx 是夹爪闭合后的采样帧，优先判断该帧中是否已经稳定持物。",
            "如果提供 hand_left_color 或 hand_right_color，它们分别是左腕/右腕相机；对应 hand 的腕部相机是主要证据，head_color 只作场景辅助。",
            "如果看不清或无法确定，target_object 必须为 null。",
        ]
        if instruction:
            lines.append(f"任务指令: {instruction}")
        if candidates:
            lines.append("候选目标物体（target_object 必须逐字使用其中之一）: " + ", ".join(candidates))
        if event_lines:
            lines.append("抓取事件:")
            lines.extend(event_lines)
        lines.append(
            "只返回严格 JSON，格式为: "
            '{"assignments":[{"hand":"left|right","target_object":"物体名或null",'
            '"confidence":0.0,"evidence":"简短依据"}]}'
        )
        return "\n".join(lines)
    lines = [
        "Identify the target object actually grasped or touched by the specified hand in each event.",
        "Do not infer from task target order or hand priors; use only visual contact evidence.",
        "frame_idx is sampled after the gripper-close event; prefer evidence that the object is already stably held in that frame.",
        "hand_left_color and hand_right_color are left/right wrist cameras; the wrist camera matching the requested hand is primary evidence, while head_color is context only.",
        "If uncertain, set target_object to null.",
    ]
    if instruction:
        lines.append(f"Task instruction: {instruction}")
    if candidates:
        lines.append("Candidate target objects (target_object must exactly match one): " + ", ".join(candidates))
    if event_lines:
        lines.append("Grasp events:")
        lines.extend(event_lines)
    lines.append(
        "Return strict JSON only: "
        '{"assignments":[{"hand":"left|right","target_object":"object name or null",'
        '"confidence":0.0,"evidence":"short visual evidence"}]}'
    )
    return "\n".join(lines)


def _iter_event_images(event: dict[str, Any]) -> list[tuple[str, Path]]:
    raw_images = event.get("images")
    if raw_images is None:
        raw_images = event.get("image_paths")
    if raw_images is None and event.get("image_path") is not None:
        raw_images = [event.get("image_path")]
    out: list[tuple[str, Path]] = []
    for item in raw_images or []:
        camera = ""
        path_value: Any = item
        if isinstance(item, dict):
            camera = str(item.get("camera") or "")
            path_value = item.get("path") or item.get("image_path")
        if not path_value:
            continue
        out.append((camera, Path(str(path_value))))
    return out


def _parse_hand_target_response(
    text: str,
    candidate_labels: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    payload = _load_json_object(text)
    if not isinstance(payload, dict):
        return {}

    raw_assignments: Any = payload.get("assignments")
    if raw_assignments is None:
        raw_assignments = [
            {"hand": hand, "target_object": value}
            for hand, value in payload.items()
            if hand in {"left", "right"}
        ]
    if not isinstance(raw_assignments, list):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for item in raw_assignments:
        if not isinstance(item, dict):
            continue
        hand = str(item.get("hand") or "").lower().strip()
        if hand not in {"left", "right"}:
            continue
        target = _canonical_target_label(item.get("target_object"), candidate_labels)
        out[hand] = {
            "target_object": target,
            "confidence": _as_float(item.get("confidence"), default=0.0),
            "evidence": str(item.get("evidence") or ""),
        }
    return out


def _load_json_object(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None


def _canonical_target_label(value: Any, candidate_labels: list[str] | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or raw.lower() in {"null", "none", "unknown", "uncertain", "不确定", "未知"}:
        return None
    candidates = [label for label in (candidate_labels or []) if label]
    if not candidates:
        return raw
    raw_norm = _normalise_label(raw)
    for candidate in candidates:
        cand_norm = _normalise_label(candidate)
        if raw_norm == cand_norm:
            return candidate
    for candidate in candidates:
        cand_norm = _normalise_label(candidate)
        if raw_norm and cand_norm and (raw_norm in cand_norm or cand_norm in raw_norm):
            return candidate
    return None


def _normalise_label(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _encode_image(path: Path) -> str | None:
    """Read an image file and return its base64-encoded content."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _validate_api_key(api_key: str, key_env: str) -> None:
    """Fail early with a readable error before httpx encodes request headers."""
    placeholders = {"your_key", "your-api-key", "sk-xxx", "...", "你的key"}
    if api_key.lower() in placeholders or "你的" in api_key:
        raise EnvironmentError(
            f"{key_env} still looks like a placeholder. Set it to the real API key."
        )
    try:
        api_key.encode("ascii")
    except UnicodeEncodeError as exc:
        raise EnvironmentError(
            f"{key_env} contains non-ASCII characters. API keys must be plain ASCII "
            "tokens, for example sk-...."
        ) from exc


def _is_aloha_profile(profile_raw: dict[str, Any]) -> bool:
    profile_id = str(profile_raw.get("profile_id") or "").lower()
    adapter = str(profile_raw.get("adapter") or "").lower()
    return profile_id == "aloha" or adapter in {"aloha", "aloha_stationary"}


def _system_prompt(language: str, scene_prompt_style: str) -> str:
    if scene_prompt_style == "aloha_table" and language == "zh":
        return _SYSTEM_ALOHA_TABLE_ZH
    return _SYSTEM_ZH if language == "zh" else _SYSTEM_EN


def _normalize_model_id(model: str) -> str:
    """Provider model IDs are case-sensitive; AIHubMix publishes lowercase IDs."""
    aliases = {
        "Qwen3-VL-Flash": "qwen3-vl-flash",
        "qwen3-VL-Flash": "qwen3-vl-flash",
    }
    model = model.strip()
    return aliases.get(model, model)


def _build_user_prompt(
    detections: list[Detection],
    instruction: str,
    scene_layout: dict[str, Any] | None = None,
    language: str = "zh",
    target_labels: list[str] | None = None,
    scene_prompt_style: str = "default",
) -> str:
    """Build the text part of the user message."""
    scene_layout = scene_layout or {}
    zh = language == "zh"
    if scene_prompt_style == "aloha_table" and zh:
        return _build_aloha_table_user_prompt(detections, instruction, target_labels)
    lines: list[str] = []
    if instruction:
        lines.append(f"任务指令: {instruction}" if zh else f"Task instruction: {instruction}")
    if target_labels:
        target_text = ", ".join(target_labels)
        if zh:
            lines.append(f"任务目标物体: {target_text}")
            lines.append("只描述这些目标物体和必要环境；不要列出其它商品名称。")
            lines.append(
                "输出必须只返回一个句子，严格套用这个模板，不要补充解释："
                "“<目标A> 和 <目标B> 位于<左/右>侧货架的第<N>排饮料区域，"
                "其中<目标A>位于<目标B>的<左/右>侧。”"
                "示例：“ENONE Juice 和 Itoen Barley Tea 均位于右侧货架的第2排饮料区域，"
                "其中ENONE Juice位于Itoen Barley Tea的左侧。”"
                "如果只有一个目标，使用："
                "“<目标A> 位于<左/右>侧货架的第<N>排饮料区域。”"
            )
        else:
            lines.append(f"Target objects: {target_text}")
            lines.append("Describe only these target objects and necessary context; do not list other product names.")
            lines.append(
                "Return exactly one sentence using this template and do not add explanations: "
                "\"<A> and <B> are both in drink row <N> of the <left/right> shelf, "
                "where <A> is to the <left/right> of <B>.\" "
                "If there is only one target, use: "
                "\"<A> is in drink row <N> of the <left/right> shelf.\""
            )
    layout_note = _layout_note(scene_layout, zh)
    if layout_note:
        lines.append(layout_note)
    else:
        lines.append(
            "\n未提供货架布局配置：位置提示只表示画面/检测框的粗略相对位置，"
            "可以按视觉直观弱描述目标约在左侧或右侧货架区域，但不要判断第几层货架或第几排饮料。"
            if zh
            else "\nNo shelf layout configuration is provided: position hints are only coarse image/detection-relative locations. You may weakly describe whether targets appear in the left or right shelf area, but do not infer physical shelf levels or drink rows."
        )
    if detections:
        lines.append("\n检测到的物体（bbox 是整张图归一化坐标）:" if zh else "\nDetected objects in this frame (bbox is normalized full-image coordinates):")
        for d in detections:
            x1, y1, x2, y2 = d.bbox
            cx = round((x1 + x2) / 2, 2)
            cy = round((y1 + y2) / 2, 2)
            w = round(x2 - x1, 2)
            h = round(y2 - y1, 2)
            position = _position_hint(d, detections, scene_layout, zh)
            if zh:
                lines.append(
                    f"  - {d.label}: bbox [{x1:.4f}, {y1:.4f}, {x2:.4f}, {y2:.4f}], "
                    f"中心 ({cx}, {cy}), 尺寸 {w}×{h}, 位置提示: {position}, "
                    f"置信度: {d.conf:.0%}"
                )
            else:
                lines.append(
                    f"  - {d.label}: bbox [{x1:.4f}, {y1:.4f}, {x2:.4f}, {y2:.4f}], "
                    f"center ({cx}, {cy}), size {w}×{h}, position hint: {position}, "
                    f"confidence: {d.conf:.0%}"
                )
    if zh:
        lines.append(
            "\n请基于图像和上述检测信息填充模板。货架横向先分为左侧货架、右侧货架；"
            "每一侧货架内部再按左边、中间、右边三段描述物体横向位置。"
            "如果目标检测不完整，可以结合图像和任务指令判断相对位置，但输出仍必须保持模板句式。"
            "不要描述非目标商品清单，也不要说购物车为空或已有物品。"
        )
    else:
        lines.append(
            "\nDescribe the scene based on the image and detections. Only mention left/right, "
            "above/below, or same-row relations when supported by the bounding boxes; if uncertain, "
            "say approximate instead of forcing a shelf-level claim. Do not list non-target "
            "products, and do not say whether the cart is empty or contains items."
        )
    return "\n".join(lines)


def _build_aloha_table_user_prompt(
    detections: list[Detection],
    instruction: str,
    target_labels: list[str] | None = None,
) -> str:
    lines = [
        "请根据图像和检测框描述桌子上饮料的数量和左右位置。",
        "输出必须只返回一句中文，严格使用以下模板之一：",
        "1. 桌子有<N>瓶饮料，饮料<A>放在左边，饮料<B>放在右边。",
        "2. 桌子有<N>瓶饮料，饮料<A>放在<左边/中间/右边>。",
        "如果有多于两瓶饮料，只描述最左边和最右边的饮料；<N>仍填写桌面上可见饮料总数。",
        "如果检测结果和图像不一致，以图像中可见饮料为准；如果无法确认名称，使用“未知饮料”。",
        "不要输出解释，不要描述货架、购物车、机器人或机械臂。",
    ]
    if instruction:
        lines.append(f"任务指令: {instruction}")
    if target_labels:
        lines.append("任务目标饮料: " + ", ".join(label for label in target_labels if label))
    if detections:
        lines.append("\n检测到的饮料候选（bbox 是整张图归一化坐标，x 越小越靠左）:")
        sorted_detections = sorted(detections, key=lambda item: (item.bbox[0] + item.bbox[2]) / 2)
        for d in sorted_detections:
            x1, y1, x2, y2 = d.bbox
            cx = round((x1 + x2) / 2, 3)
            side = "左边" if cx < 0.4 else "右边" if cx > 0.6 else "中间"
            lines.append(
                f"  - {d.label}: bbox [{x1:.4f}, {y1:.4f}, {x2:.4f}, {y2:.4f}], "
                f"中心x={cx}, 位置提示={side}, 置信度={d.conf:.0%}"
            )
    else:
        lines.append("\n未提供检测框，请直接根据图像判断桌面可见饮料数量和左右位置。")
    return "\n".join(lines)


def _layout_note(scene_layout: dict[str, Any], zh: bool) -> str:
    shelf = scene_layout.get("shelf") if isinstance(scene_layout.get("shelf"), dict) else {}
    physical_levels = int(shelf.get("physical_levels") or 0)
    drink_rows = int(shelf.get("drink_rows") or 0)
    row_ranges = _valid_ranges(shelf.get("row_y_ranges"))
    x_ranges = _valid_ranges(shelf.get("x_ranges"))
    source = scene_layout.get("source") or shelf.get("source")
    if not physical_levels and not drink_rows and not row_ranges:
        return ""
    if zh:
        parts = ["\n货架布局约束:"]
        if source in {"auto_yolo_rows", "auto_yolo_layout", "profile_with_auto_rows"}:
            parts.append("饮料排范围由第一帧 YOLO 检测框自动估计，排描述应按“约在”理解。")
        if physical_levels:
            parts.append(f"货架有 {physical_levels} 个物理层板。")
        if drink_rows:
            parts.append(f"当前任务区只有 {drink_rows} 排饮料；位置提示里的“第1/2/3排饮料”表示从上到下的饮料排，不等同于物理层板编号。")
        if row_ranges:
            parts.append("饮料排的纵向范围来自检测框聚类。")
        if x_ranges:
            parts.append("左右货架的横向范围来自检测框聚类；每侧再均分为左边、中间、右边。")
        parts.append("可以按图像视觉弱描述目标约在左侧或右侧货架区域，但不要把它当成确定结构标签。")
        parts.append("不要用整张图的 upper/middle/lower 代替货架层级。")
        return "".join(parts)
    parts = ["\nShelf layout constraints:"]
    if source in {"auto_yolo_rows", "auto_yolo_layout", "profile_with_auto_rows"}:
        parts.append(" drink-row ranges are auto-estimated from first-frame YOLO detections and should be treated as approximate.")
    if physical_levels:
        parts.append(f" the shelf has {physical_levels} physical levels.")
    if drink_rows:
        parts.append(f" The task area has {drink_rows} drink-bearing rows; row_1/row_2/row_3 mean drink rows from top to bottom, not physical shelf IDs.")
    if row_ranges:
        parts.append(" Drink-row y ranges come from detection clustering.")
    if x_ranges:
        parts.append(" Left/right shelf x ranges come from detection clustering; each side is split into left/middle/right.")
    parts.append(" You may weakly describe whether targets appear in the left or right shelf area, but do not treat that as a deterministic structured label.")
    parts.append(" Do not use full-image upper/middle/lower as shelf levels.")
    return "".join(parts)


def _position_hint(
    detection: Detection,
    detections: list[Detection],
    scene_layout: dict[str, Any],
    zh: bool,
) -> str:
    """Map normalized bboxes to shelf-relative hints instead of full-image bins."""
    x1, y1, x2, y2 = detection.bbox
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    centers = [
        ((d.bbox[0] + d.bbox[2]) / 2, (d.bbox[1] + d.bbox[3]) / 2)
        for d in detections
        if len(d.bbox) == 4
    ]
    ys = [p[1] for p in centers]
    shelf = scene_layout.get("shelf") if isinstance(scene_layout.get("shelf"), dict) else {}
    drink_rows = int(shelf.get("drink_rows") or 0)
    row_ranges = _valid_ranges(shelf.get("row_y_ranges"))
    has_layout = bool(drink_rows or row_ranges)

    if row_ranges:
        row = _row_from_ranges(cy, row_ranges, zh)
    elif drink_rows >= 2:
        row_labels_zh = tuple(f"第{i}排饮料" for i in range(1, drink_rows + 1))
        row_labels_en = tuple(f"drink_row_{i}" for i in range(1, drink_rows + 1))
        row = _bucket_hint(cy, ys, row_labels_zh if zh else row_labels_en)
    else:
        row = _bucket_hint(cy, ys, ("上方", "中部", "下方") if zh else ("upper", "middle", "lower"))
    shelf_side, side_position = _horizontal_shelf_position(cx, shelf, zh)
    if zh:
        row_part = f"饮料排位置: {row}" if has_layout else f"画面纵向位置: {row}"
        return f"{row_part}; 横向位置: {shelf_side}{side_position}"
    row_part = f"drink-row: {row}" if has_layout else f"image-vertical: {row}"
    return f"{row_part}; horizontal: {shelf_side} {side_position}"


def _horizontal_shelf_position(cx: float, shelf: dict[str, Any], zh: bool) -> tuple[str, str]:
    ranges = _valid_ranges(shelf.get("x_ranges"))
    if len(ranges) >= 2:
        side_idx, left, right = _side_from_ranges(cx, ranges)
        side = "左侧货架" if side_idx == 0 else "右侧货架"
        span = max(1e-6, right - left)
        rel = (cx - left) / span
    else:
        side_idx = 0 if cx < 0.5 else 1
        side = "左侧货架" if side_idx == 0 else "右侧货架"
        rel = cx / 0.5 if side_idx == 0 else (cx - 0.5) / 0.5
    rel = min(0.999999, max(0.0, rel))
    if rel < 1 / 3:
        pos = "左边"
    elif rel < 2 / 3:
        pos = "中间"
    else:
        pos = "右边"
    if zh:
        return side, pos
    return (
        "left shelf" if side_idx == 0 else "right shelf",
        {"左边": "left", "中间": "middle", "右边": "right"}[pos],
    )


def _side_from_ranges(cx: float, ranges: list[tuple[float, float]]) -> tuple[int, float, float]:
    best_idx = 0
    best_distance = float("inf")
    for idx, (left, right) in enumerate(ranges[:2]):
        if left <= cx <= right:
            return idx, left, right
        distance = min(abs(cx - left), abs(cx - right))
        if distance < best_distance:
            best_idx = idx
            best_distance = distance
    left, right = ranges[best_idx]
    return best_idx, left, right


def _row_from_ranges(value: float, ranges: list[tuple[float, float]], zh: bool) -> str:
    if not ranges:
        return "未知排" if zh else "unknown_row"
    best_idx = 0
    best_distance = float("inf")
    for idx, (top, bottom) in enumerate(ranges):
        if top <= value <= bottom:
            best_idx = idx
            break
        distance = min(abs(value - top), abs(value - bottom))
        if distance < best_distance:
            best_distance = distance
            best_idx = idx
    return f"第{best_idx + 1}排饮料" if zh else f"drink_row_{best_idx + 1}"


def _valid_ranges(value: Any) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    if not isinstance(value, list):
        return ranges
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            top = float(item[0])
            bottom = float(item[1])
        except (TypeError, ValueError):
            continue
        if bottom < top:
            top, bottom = bottom, top
        ranges.append((top, bottom))
    return ranges


def _bucket_hint(value: float, values: list[float], labels: tuple[str, ...]) -> str:
    if not labels:
        return "unknown"
    if not values:
        return labels[len(labels) // 2]
    lo = min(values)
    hi = max(values)
    span = hi - lo
    if span < 1e-6:
        return labels[len(labels) // 2]
    rel = min(0.999999, max(0.0, (value - lo) / span))
    idx = min(len(labels) - 1, int(rel * len(labels)))
    return labels[idx]
