"""
Linux backend - manages applications as systemd services.

Two service scopes are supported:

``user``    the unit lives in ``~/.config/systemd/user`` and is driven
            with ``systemctl --user``. No root privileges are needed.
            For the app to start at *boot* (not just at login) the user
            session needs to be "lingering", which is enabled with
            ``loginctl enable-linger <user>``.

``system``  the unit lives in ``/etc/systemd/system`` and is driven with
            ``sudo -n systemctl``. The panel user needs passwordless sudo
            for systemctl (see the README for the one-liner).

If an application has no unit file yet, the panel can write one for it
from the registry declaration.
"""

from __future__ import annotations

import os
import time

from .base import Backend
from .models import (
    SCOPE_SYSTEM,
    SCOPE_USER,
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STOPPED,
    STATE_STARTING,
    STATE_UNKNOWN,
    ActionResult,
    AppSpec,
    AppStatus,
    PortInfo,
)
from .probes import run, parse_ss_lnpt, tail


def _systemd_scope_args(app: AppSpec) -> list[str]:
    """The systemctl prefix for the app's service scope."""
    if app.service_scope == SCOPE_SYSTEM:
        return ["sudo", "-n", "systemctl"]
    return ["systemctl", "--user"]


def _systemctl(args: list[str], timeout: float = 25.0):
    return run(args, timeout=timeout)


def _parse_property_block(text: str) -> dict[str, str]:
    properties = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if "=" in line:
            key, _, value = line.partition("=")
            properties[key.strip()] = value.strip()
    return properties


def _systemd_timestamp_to_epoch(value: str) -> int | None:
    """
    Convert a systemd timestamp ("Mon 2026-11-11 10:26:38 IST") into a
    unix timestamp. Returns None when the value is not parseable.
    """
    if not value:
        return None
    from datetime import datetime

    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return int(parsed.timestamp())
        except ValueError:
            continue
    return None


def build_unit_file(app: AppSpec) -> str:
    """
    Render a systemd unit for an application from its registry entry.
    """
    working_dir = app.working_dir or "/"
    exec_start = " ".join(app.start_command) if app.start_command else \
        f"/bin/true  # {app.name}: no start command configured"

    env_lines = "\n".join(
        f'Environment="{key}={value}"' for key, value in sorted(app.env.items())
    )
    env_block = f"{env_lines}\n" if env_lines else ""

    return f"""[Unit]
Description={app.description or app.name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={working_dir}
{env_block}ExecStart={exec_start}
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


class LinuxBackend(Backend):
    platform = "linux"
    supports_autostart = True

    # -- introspection -----------------------------------------------------
    def _unit_path(self, app: AppSpec) -> str:
        if app.service_scope == SCOPE_SYSTEM:
            return f"/etc/systemd/system/{app.unit_name}"
        return os.path.expanduser(f"~/.config/systemd/user/{app.unit_name}")

    def _show(self, app: AppSpec) -> dict[str, str]:
        result = _systemctl(
            _systemd_scope_args(app)
            + ["show", app.unit_name,
               "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,"
               "MemoryCurrent,UnitFileState,LoadState"]
        )
        return _parse_property_block(result.stdout)

    def _is_enabled(self, app: AppSpec) -> tuple[bool, str]:
        result = _systemctl(
            _systemd_scope_args(app) + ["is-enabled", app.unit_name]
        )
        state = (result.stdout or result.stderr or "").strip().splitlines()
        value = state[0].strip() if state else ""
        if value in ("enabled", "enabled-runtime", "static", "indirect"):
            return True, value
        return False, value or "unknown"

    def _listening_ports(self, app: AppSpec) -> list[PortInfo]:
        result = run(["ss", "-lntp"], timeout=10.0)
        if not result.ok:
            result = run(["netstat", "-lntp"], timeout=10.0)
            rows = []
            from .probes import parse_netstat_ano

            rows = parse_netstat_ano(result.stdout)
        else:
            rows = parse_ss_lnpt(result.stdout)

        ports: list[PortInfo] = []
        for row in rows:
            if app.ports and row["port"] not in app.ports:
                continue
            ports.append(PortInfo(
                port=row["port"],
                address=row["address"],
                pid=row["pid"],
                process=row["process"],
            ))
        return ports

    # -- Backend API -------------------------------------------------------
    def status(self, app: AppSpec) -> AppStatus:
        status = AppStatus(name=app.name)
        show = self._show(app)

        active = show.get("ActiveState", "")
        sub = show.get("SubState", "")
        if active == "active":
            if sub == "starting":
                status.state = STATE_STARTING
            else:
                status.state = STATE_RUNNING
        elif active in ("failed", "activating", "deactivating"):
            status.state = STATE_FAILED
        elif active == "inactive":
            status.state = STATE_STOPPED
        else:
            status.state = STATE_UNKNOWN

        pid = show.get("MainPID", "")
        if pid and pid.isdigit() and int(pid) > 0:
            status.pid = int(pid)

        started = _systemd_timestamp_to_epoch(
            show.get("ExecMainStartTimestamp", "")
        )
        if started:
            status.uptime_seconds = max(0, int(time.time()) - started)

        memory = show.get("MemoryCurrent", "")
        if memory and memory.isdigit():
            status.memory_bytes = int(memory)

        enabled, detail = self._is_enabled(app)
        status.autostart = enabled
        status.autostart_detail = detail

        status.ports = self._listening_ports(app)
        if show.get("LoadState") == "not-found":
            status.detail = (
                f"unit {app.unit_name} is not installed - use "
                f"\"Install service\" to create it"
            )
        status.error = status.detail

        self._health(app, status)
        return status

    def start(self, app: AppSpec) -> ActionResult:
        result = _systemctl(_systemd_scope_args(app) + ["start", app.unit_name])
        if result.ok:
            return ActionResult(True, f"{app.name} started")
        return ActionResult(
            False, f"could not start {app.name}", result.output.strip()
        )

    def stop(self, app: AppSpec) -> ActionResult:
        result = _systemctl(_systemd_scope_args(app) + ["stop", app.unit_name])
        if result.ok:
            return ActionResult(True, f"{app.name} stopped")
        return ActionResult(
            False, f"could not stop {app.name}", result.output.strip()
        )

    def restart(self, app: AppSpec) -> ActionResult:
        result = _systemctl(_systemd_scope_args(app) + ["restart", app.unit_name])
        if result.ok:
            return ActionResult(True, f"{app.name} restarted")
        return ActionResult(
            False, f"could not restart {app.name}", result.output.strip()
        )

    def set_autostart(self, app: AppSpec, enabled: bool) -> ActionResult:
        verb = "enable" if enabled else "disable"
        result = _systemctl(_systemd_scope_args(app) + [verb, app.unit_name])
        if not result.ok:
            return ActionResult(
                False, f"could not {verb} {app.name}", result.output.strip()
            )
        message = f"{app.name} will start automatically on boot"
        detail = ""
        if enabled and app.service_scope == SCOPE_USER:
            linger = run(["loginctl", "enable-linger"], timeout=15.0)
            if not linger.ok:
                detail = (
                    "Note: run `loginctl enable-linger` as root so the app "
                    "also starts before anyone logs in."
                )
        if not enabled:
            message = f"{app.name} will no longer start automatically"
        return ActionResult(True, message, detail)

    def logs(self, app: AppSpec, lines: int = 80) -> list[str]:
        result = _systemctl(
            _systemd_scope_args(app)
            + ["status", app.unit_name, "--no-pager", "--lines", str(lines)]
        )
        text = result.output
        if not text.strip():
            # `systemctl status` prints nothing for an inactive unit; fall
            # back to the journal so the last run is still visible.
            journal = run(
                ["journalctl", "--user", "-u", app.unit_name, "-n", str(lines),
                 "--no-pager"]
                if app.service_scope == SCOPE_USER
                else ["journalctl", "-u", app.unit_name, "-n", str(lines),
                      "--no-pager"],
                timeout=15.0,
            )
            text = journal.output
        return tail(text, lines)

    def install_service(self, app: AppSpec) -> ActionResult:
        """Write the unit file and (re)load systemd so it takes effect."""
        unit_path = self._unit_path(app)
        content = build_unit_file(app)
        try:
            os.makedirs(os.path.dirname(unit_path), exist_ok=True)
            if app.service_scope == SCOPE_SYSTEM:
                # Needs root - write through a temp file and sudo cp.
                result = _write_with_sudo(unit_path, content)
                if not result.ok:
                    return ActionResult(
                        False,
                        f"could not write {unit_path}",
                        result.output.strip()
                        or "sudo needs passwordless rights for this command",
                    )
            else:
                with open(unit_path, "w", encoding="utf-8") as handle:
                    handle.write(content)
        except OSError as exc:
            return ActionResult(False, f"could not write the unit file: {exc}")

        reload_args = _systemd_scope_args(app) + ["daemon-reload"]
        run(reload_args, timeout=25.0)
        if app.service_scope == SCOPE_SYSTEM:
            run(["sudo", "-n", "systemctl", "daemon-reload"], timeout=25.0)

        return ActionResult(
            True,
            f"service installed at {unit_path}",
            "Enable it with the autostart toggle.",
        )


def _write_with_sudo(path: str, content: str):
    """Write a file through ``sudo cp`` without invoking a shell."""
    import tempfile

    handle, tmp = tempfile.mkstemp(prefix="unit-", suffix=".service")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(content)
        return run(["sudo", "-n", "cp", tmp, path], timeout=20.0)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
