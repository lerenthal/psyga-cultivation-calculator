"""
Data model for the Server Control Panel.

Everything the panel knows about a managed application is described by an
``AppSpec`` (the static, user-editable declaration) and an ``AppStatus``
(the live snapshot the panel computes for it).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


# Service scopes.
#   "user"   - systemd --user units (no root needed)
#   "system" - system systemd units (needs sudo rights for the panel user)
SCOPE_USER = "user"
SCOPE_SYSTEM = "system"

# Canonical states shown on the dashboard.
STATE_RUNNING = "running"
STATE_STOPPED = "stopped"
STATE_FAILED = "failed"
STATE_STARTING = "starting"
STATE_UNKNOWN = "unknown"

STATE_ORDER = {
    STATE_RUNNING: 0,
    STATE_STARTING: 1,
    STATE_FAILED: 2,
    STATE_STOPPED: 3,
    STATE_UNKNOWN: 4,
}


@dataclass
class AppSpec:
    """The declaration of one managed application."""

    name: str
    description: str = ""
    working_dir: str = ""
    # How to start the app: argv list, e.g. [".venv/bin/python", "app.py"].
    start_command: list[str] = field(default_factory=list)
    # Extra environment for the process, e.g. {"PORT": "5001"}.
    env: dict[str, str] = field(default_factory=dict)
    # Ports this app is expected to listen on.
    ports: list[int] = field(default_factory=list)
    # URL used for the HTTP health probe.
    health_url: str = ""
    # systemd unit / scheduled task name. Defaults to "<name>".
    service_name: str = ""
    # "user" or "system" (Linux only; ignored on Windows).
    service_scope: str = SCOPE_USER
    # Whether the app should come back automatically after a reboot.
    autostart: bool = False
    # Optional grouping shown on the dashboard.
    category: str = ""
    # Free-form notes.
    notes: str = ""

    def __post_init__(self):
        self.name = str(self.name).strip()
        if not self.name:
            raise ValueError("AppSpec.name must not be empty")
        self.start_command = [str(x) for x in (self.start_command or [])]
        self.env = {str(k): str(v) for k, v in (self.env or {}).items()}
        self.ports = [int(p) for p in (self.ports or [])]
        if self.service_scope not in (SCOPE_USER, SCOPE_SYSTEM):
            raise ValueError(
                f"AppSpec.service_scope must be '{SCOPE_USER}' or "
                f"'{SCOPE_SYSTEM}' (got {self.service_scope!r})"
            )
        if not self.service_name:
            self.service_name = self.name

    @property
    def unit_name(self) -> str:
        """systemd unit / scheduled task identifier for this app."""
        name = self.service_name
        return name if name.endswith(".service") else f"{name}.service"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppSpec":
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(
                f"unknown app keys: {', '.join(sorted(unknown))}"
            )
        return cls(**data)


@dataclass
class PortInfo:
    """A listening TCP socket."""

    port: int
    address: str = ""
    pid: int | None = None
    process: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class AppStatus:
    """A live snapshot of one managed application."""

    name: str
    state: str = STATE_UNKNOWN
    pid: int | None = None
    uptime_seconds: int | None = None
    memory_bytes: int | None = None
    cpu_percent: float | None = None
    ports: list[PortInfo] = field(default_factory=list)
    http_status: int | None = None
    http_latency_ms: int | None = None
    health_error: str = ""
    autostart: bool = False
    autostart_supported: bool = True
    autostart_detail: str = ""
    log_tail: list[str] = field(default_factory=list)
    detail: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["ports"] = [p.to_dict() for p in self.ports]
        return data


@dataclass
class ActionResult:
    """The outcome of a start/stop/restart/enable action."""

    ok: bool
    message: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)
