#!/home/robot/miniconda3/envs/zerith/bin/python
"""Code entry point for the Zerith inference executor.

This launcher intentionally controls the executor through the local 8080
service.  The web page and this CLI therefore share one RobotService SDK owner,
one safety latch, and one emergency-stop path instead of opening the hardware
SDK from competing Python processes.

Run ``python3 inference_executor.py --help`` for the available metadata,
reconnect, dry-run, continuous-run, stop, and status commands.
"""

from web_control.pi05_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
