import json
import os
import re
from pathlib import Path

BASE = Path(os.environ.get("DATAQC_HOME", str(Path(__file__).resolve().parents[1] / "runtime")))
VAR = BASE / "var"
EXPORTS = BASE / "exports"
CONFIG = BASE / "config/settings.json"
REAL_SOURCE_ROOT = Path(os.environ.get('DATAQC_REAL_ROOT', '/data/zerith_data')).expanduser()
SIM_SOURCE_ROOT = Path(os.environ.get('DATAQC_SIM_ROOT', '/data/sim_data')).expanduser()
DEFAULTS = dict(
    robot="zerith",
    api_model="gpt-5.6-terra",
    vlm_enabled=False,
    motion_batch_size=10,
    motion_batch_concurrency=2,
    reasoning_effort="none",
    max_output_tokens=2500,
    vlm_token_budget=250000,
    vision_version="motion_category_v4",
    yolo_frame_offset=40,
    yolo_confidence=0.25,
    yolo_thresholds_path="",
    stationary_frames=40,
    yolo_path=str(BASE / "models/products.pt"),
    api_file=str(BASE / "config/api.txt"),
    vlm_fps=2,
    window_seconds=4,
    overlap_seconds=1,
    api_timeout=180,
    max_requests_per_episode=40,
    yolo_fps=5,
    device="0",
    export_grades=["A", "B"],
)


def settings():
    return DEFAULTS | (json.loads(CONFIG.read_text()) if CONFIG.exists() else {})


def api_config(cfg=None):
    s = Path((cfg or settings())["api_file"]).read_text()

    def value(name):
        m = re.search(r'(?m)["\']?' + name + r'["\']?\s*[:=]\s*["\']([^"\']+)["\']', s)
        return m.group(1) if m else None

    url, model, key = value("base_url"), (cfg or settings()).get("api_model") or value("model"), value("OPENAI_API_KEY")
    if not all((url, model, key)):
        raise ValueError("API 配置缺少地址、模型或密钥")
    return url.rstrip("/"), model, key
