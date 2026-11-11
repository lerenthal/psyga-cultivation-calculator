"""
The backend contract.

A backend knows how to ask the operating system about a managed
application: is it running, what port is it on, start it, stop it, make
it survive a reboot.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ActionResult, AppSpec, AppStatus


class Backend(ABC):
    """Abstract process manager for a family of applications."""

    #: Short identifier shown on the dashboard ("linux" / "windows").
    platform = "unknown"

    #: Whether this backend can schedule apps to start on boot.
    supports_autostart = False

    @abstractmethod
    def status(self, app: AppSpec) -> AppStatus:
        """Return a live snapshot of the application."""

    @abstractmethod
    def start(self, app: AppSpec) -> ActionResult:
        ...

    @abstractmethod
    def stop(self, app: AppSpec) -> ActionResult:
        ...

    @abstractmethod
    def restart(self, app: AppSpec) -> ActionResult:
        ...

    @abstractmethod
    def set_autostart(self, app: AppSpec, enabled: bool) -> ActionResult:
        ...

    @abstractmethod
    def logs(self, app: AppSpec, lines: int = 80) -> list[str]:
        ...

    def install_service(self, app: AppSpec) -> ActionResult:
        """Create the service definition (unit file / scheduled task)."""
        return ActionResult(
            False, f"{self.platform}: installing a service is not supported"
        )

    # -- helpers shared by the concrete backends ---------------------------
    def _health(self, app: AppSpec, status: AppStatus) -> None:
        """Fill in the HTTP health fields of a status snapshot."""
        from .probes import http_probe

        if not app.health_url:
            status.health_error = "no health URL configured"
            return
        probe = http_probe(app.health_url)
        status.http_status = probe.status
        status.http_latency_ms = probe.latency_ms
        status.health_error = probe.error
