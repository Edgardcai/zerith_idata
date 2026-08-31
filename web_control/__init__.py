"""Local web console for the ZERITH H1 PRO.

Importing this package never loads the vendor SDK and never connects to the
robot.  The SDK is created lazily only after an explicit takeover request.
"""

__all__ = ["robot_service", "camera_service", "server"]
