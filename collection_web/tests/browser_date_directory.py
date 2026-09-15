"""Compatibility entry point: date behavior now follows applied scene directories."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).with_name('browser_scene_directory.py')),run_name='__main__')
