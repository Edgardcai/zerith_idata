"""Share configured YOLO defaults with the manual frame-screening entry point."""
import os
from pathlib import Path


def configure_yolo_environment(cfg, runtime):
    conventional = Path(runtime)/'config/yolo-thresholds.json'
    thresholds = cfg.get('yolo_thresholds_path') or (str(conventional) if conventional.is_file() else '')
    os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_MODEL', cfg['yolo_path'])
    # Empty explicitly selects the global confidence, without a required JSON file.
    os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_THRESHOLDS', thresholds)
    os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_CONFIDENCE', str(cfg.get('yolo_confidence', .25)))
    os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_DEVICE', str(cfg.get('device', '0')))
