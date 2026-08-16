"""Driver factory: pick the execution layer from configuration."""

from __future__ import annotations

import sys
from typing import Callable, Optional

from computer_control.config import Config
from computer_control.drivers.base import BaseDriver, CapturePayload, DriverError


def create_driver(cfg: Config, log: Optional[Callable[[str], None]] = None) -> BaseDriver:
    """Instantiate the driver named by ``cfg.platform.name``.

    - "dry-run": recording driver, nothing touches the hardware
    - "windows": real driver (SendInput / UIA)
    - "auto": windows on win32, dry-run otherwise is NOT acceptable silently,
      so on other platforms auto raises with a clear message.
    """
    name = cfg.platform.name
    if name == "dry-run":
        from computer_control.drivers.dummy import NullDriver

        return NullDriver(enable_a11y=True)
    if name == "windows":
        from computer_control.drivers.windows import WindowsDriver

        return WindowsDriver(cfg, log=log)
    if name == "auto":
        if sys.platform == "win32":
            from computer_control.drivers.windows import WindowsDriver

            return WindowsDriver(cfg, log=log)
        raise DriverError(
            "platform 'auto' resolved to %r, but this plugin ships drivers for "
            "Windows and the dry-run mode only. Implement a driver for the "
            "platform contract in computer_control.drivers to extend it." % sys.platform,
            code="backend_unavailable",
        )
    raise DriverError("unknown platform %r" % name, code="backend_unavailable")


__all__ = ["BaseDriver", "CapturePayload", "DriverError", "create_driver"]
