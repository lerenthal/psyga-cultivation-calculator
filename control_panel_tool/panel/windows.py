"""
Windows backend - manages applications as plain background processes.

There is no systemd on Windows, so this backend does what an operator
would do by hand:

* find the running process by matching its command line against the
  registry entry (``process_match``),
* read the ports it listens on from ``netstat``/``Get-NetTCPConnection``,
* start it detached with ``Start-Process`` (or a plain ``Popen``),
* stop it with ``taskkill /T``,
* and make it start on logon with a scheduled task.

The same registry fields drive both backends, so an app declared once
works on both a Windows workstation and an Ubuntu server.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from .base import Backend
from .models import (
    STATE_FAILED,
    STATE_RUNNING,
    STATE_STOPPED,
    STATE_UNKNOWN,
    ActionResult,
    AppSpec,
    AppStatus,
    PortInfo,
)
from .probes import parse_netstat_ano, run, tail


def _powershell(script: str, timeout: float = 25.0):
    """Run a PowerShell snippet and return its stdout."""
    return run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


def _process_table() -> list[dict]:
    """
    All running processes with their command lines.

    Returns a list of {"pid": int, "name": str, "cmdline": str}.
    """
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = _powershell(script)
    if not result.ok or not result.stdout.strip():
        return []
    import json

    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    processes = []
    for item in data:
        try:
            processes.append({
                "pid": int(item.get("ProcessId")),
                "name": str(item.get("Name") or ""),
                "cmdline": str(item.get("CommandLine") or ""),
            })
        except (TypeError, ValueError):
            continue
    return processes


def _find_processes(app: AppSpec) -> list[dict]:
    """
    Every process that belongs to an app.

    On Windows a virtualenv launch spawns the real interpreter as a
    child, so an app usually shows up as two processes that both match.
    The one that matches the start command is listed first - it is the
    parent, and killing its tree stops the whole app.
    """
    needle = app.env.get("PROCESS_MATCH", "") or ""
    if not needle and app.start_command:
        needle = app.start_command[-1]
    if not needle:
        return []

    processes = _process_table()
    needle_l = needle.lower()
    matches = [p for p in processes if needle_l in p["cmdline"].lower()]

    if app.start_command:
        # Prefer the process whose command line starts with the app's own
        # interpreter - that is the launcher/parent.
        launcher = os.path.basename(
            _resolve_command(app.start_command, app.working_dir)[0]
        ).lower()
        matches.sort(key=lambda p: 0 if launcher in p["cmdline"].lower() else 1)
    return matches


def _find_process(app: AppSpec) -> dict | None:
    """
    Find the process belonging to an app.

    Matching order:
      1. an explicit ``pid_file`` written by the app,
      2. a substring match on the command line (``process_match``),
      3. the start command itself.
    """
    pid_file = app.env.get("PID_FILE", "")
    if pid_file and os.path.exists(pid_file):
        try:
            with open(pid_file, "r", encoding="utf-8") as handle:
                pid = int(handle.read().strip())
            for proc in _process_table():
                if proc["pid"] == pid:
                    return proc
        except (OSError, ValueError):
            pass

    matches = _find_processes(app)
    return matches[0] if matches else None


def _listening_ports() -> list[dict]:
    """Every listening TCP socket, from netstat (fast) or PowerShell."""
    result = run(["netstat", "-ano", "-p", "TCP"], timeout=20.0)
    if result.ok and result.stdout.strip():
        return parse_netstat_ano(result.stdout)

    script = (
        "Get-NetTCPConnection -State Listen | "
        "Select-Object LocalAddress,LocalPort,OwningProcess | "
        "ConvertTo-Json -Compress"
    )
    ps = _powershell(script)
    if not ps.ok or not ps.stdout.strip():
        return []
    import json

    try:
        data = json.loads(ps.stdout)
    except (ValueError, TypeError):
        return []
    if isinstance(data, dict):
        data = [data]
    rows = []
    for item in data:
        try:
            rows.append({
                "address": str(item.get("LocalAddress")),
                "port": int(item.get("LocalPort")),
                "pid": int(item.get("OwningProcess")),
                "process": "",
            })
        except (TypeError, ValueError):
            continue
    return rows


class WindowsBackend(Backend):
    platform = "windows"
    supports_autostart = True

    # -- Backend API -------------------------------------------------------
    def status(self, app: AppSpec) -> AppStatus:
        status = AppStatus(name=app.name)
        proc = _find_process(app)

        if proc:
            status.pid = proc["pid"]
            status.state = STATE_RUNNING
            status.detail = proc["cmdline"][:400]
            created = _process_start_time(proc["pid"])
            if created:
                status.uptime_seconds = max(0, int(time.time()) - created)
            status.memory_bytes = _process_memory(proc["pid"])
        else:
            status.state = STATE_STOPPED
            status.detail = "no matching process"

        rows = _listening_ports()
        ports = []
        for row in rows:
            if app.ports:
                # The registry says which ports this app owns - trust it.
                # (On Windows the socket often belongs to a child process
                # of the one we started, so a PID filter would hide it.)
                if row["port"] not in app.ports:
                    continue
            elif status.pid and row["pid"] != status.pid:
                continue
            ports.append(PortInfo(
                port=row["port"],
                address=row["address"],
                pid=row["pid"],
                process=row["process"],
            ))
        status.ports = ports

        enabled, detail = self._autostart_state(app)
        status.autostart = enabled
        status.autostart_detail = detail

        self._health(app, status)
        return status

    def start(self, app: AppSpec) -> ActionResult:
        if not app.start_command:
            return ActionResult(False, f"{app.name} has no start command")
        if _find_process(app):
            return ActionResult(False, f"{app.name} is already running")

        env = dict(os.environ)
        env.update(app.env)
        cwd = app.working_dir or os.getcwd()
        command = _resolve_command(app.start_command, cwd)

        # Keep the app's output somewhere useful if a log file is set,
        # otherwise discard it - a detached process has no console.
        log_file = app.env.get("LOG_FILE", "")
        output = subprocess.DEVNULL
        if log_file:
            try:
                output = open(log_file, "a", encoding="utf-8", errors="replace")
            except OSError:
                output = subprocess.DEVNULL

        try:
            subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT if output != subprocess.DEVNULL
                else subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except (OSError, ValueError) as exc:
            return ActionResult(False, f"could not start {app.name}", str(exc))
        finally:
            if output != subprocess.DEVNULL:
                try:
                    output.close()
                except OSError:
                    pass
        return ActionResult(True, f"{app.name} started")

    def stop(self, app: AppSpec) -> ActionResult:
        matches = _find_processes(app)
        if not matches:
            return ActionResult(True, f"{app.name} was not running")
        errors = []
        # Kill the whole matching tree - a virtualenv launcher and the
        # interpreter it spawned are both part of the app.
        for proc in matches:
            result = run(
                ["taskkill", "/PID", str(proc["pid"]), "/T", "/F"], timeout=25.0
            )
            if not result.ok:
                errors.append(result.output.strip())
        if errors:
            return ActionResult(
                False, f"could not stop {app.name}", "; ".join(errors)[:500]
            )
        return ActionResult(True, f"{app.name} stopped")

    def restart(self, app: AppSpec) -> ActionResult:
        stopped = self.stop(app)
        # Give the old process time to release its port before starting.
        for _ in range(20):
            if not _find_processes(app):
                break
            time.sleep(0.5)
        started = self.start(app)
        if started.ok:
            return ActionResult(True, f"{app.name} restarted", stopped.message)
        return ActionResult(False, "restart failed", started.detail)

    def set_autostart(self, app: AppSpec, enabled: bool) -> ActionResult:
        """
        Start (or stop) the app automatically when this user logs in.

        This uses the per-user Startup folder, which needs no elevation.
        Creating a real Windows scheduled task (``schtasks``) requires
        administrator rights, which the panel does not assume it has.
        The trade-off is that the app starts at *logon* rather than at
        boot - the same trade-off as a systemd ``--user`` unit without
        lingering.
        """
        startup = _startup_folder()
        launcher = os.path.join(startup, f"ServerControlPanel-{_sanitize(app.name)}.vbs")

        if enabled:
            try:
                os.makedirs(startup, exist_ok=True)
                with open(launcher, "w", encoding="utf-8") as handle:
                    handle.write(_startup_script(app))
            except OSError as exc:
                return ActionResult(
                    False, f"could not write the startup entry", str(exc)
                )
            return ActionResult(
                True,
                f"{app.name} will start automatically when you log in",
                f"startup entry: {launcher}",
            )

        if os.path.exists(launcher):
            try:
                os.remove(launcher)
            except OSError as exc:
                return ActionResult(
                    False, f"could not remove the startup entry", str(exc)
                )
        return ActionResult(
            True, f"{app.name} will no longer start automatically"
        )

    def logs(self, app: AppSpec, lines: int = 80) -> list[str]:
        log_file = app.env.get("LOG_FILE", "")
        if log_file and os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8", errors="replace") as fh:
                return tail(fh.read(), lines)
        return []

    # -- helpers -----------------------------------------------------------
    def _autostart_state(self, app: AppSpec) -> tuple[bool, str]:
        launcher = os.path.join(
            _startup_folder(), f"ServerControlPanel-{_sanitize(app.name)}.vbs"
        )
        if os.path.exists(launcher):
            return True, "starts at logon (startup folder)"
        return False, "not scheduled"


def _startup_folder() -> str:
    """The per-user Startup folder (no elevation needed to write here)."""
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(
        appdata,
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )


def _startup_script(app: AppSpec) -> str:
    """
    A small VBScript that launches the app hidden when the user logs in.

    WScript.Shell.Run's last argument carries the environment, so the
    app gets the same PORT/HOST it would get from the dashboard.
    """
    command = _resolve_command(app.start_command, app.working_dir)
    exe = command[0]
    arguments = " ".join(
        f'"{part}"' if " " in part else part for part in command[1:]
    )
    env = "\n".join(f"{key}={value}" for key, value in sorted(app.env.items()))
    log_file = app.env.get("LOG_FILE", "")

    run_target = f'""{exe}" {arguments}"' if arguments else f'""{exe}""'
    if log_file:
        run_target = f'cmd.exe /c ""{exe}" {arguments} >> "{log_file}" 2>&1"'

    return (
        "' Auto-generated by the Server Control Panel.\n"
        "' Starts the application when this user logs in.\n"
        "Option Explicit\n"
        "Dim sh\n"
        "Set sh = CreateObject(\"WScript.Shell\")\n"
        f"sh.CurrentDirectory = \"{app.working_dir}\"\n"
        f"sh.Run {run_target}, 0, False"
        + (f', "{env}"' if env else "")
        + "\n"
    )


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)


def _resolve_command(command: list[str], working_dir: str) -> list[str]:
    """
    Turn a start command into something runnable on this host.

    The executable is always resolved to an absolute path. On Windows the
    relative path in a command line is resolved against the *parent's*
    working directory, not the ``cwd`` passed to Popen - so a relative
    ".venv/Scripts/python.exe" would silently launch a completely
    different interpreter.
    """
    resolved = list(command)
    if not resolved:
        return resolved
    first = resolved[0]

    # A path that exists inside the app's own folder wins.
    candidate = os.path.abspath(
        os.path.join(working_dir, first.replace("/", os.sep))
    )
    if os.path.exists(candidate):
        resolved[0] = candidate
        return resolved

    # A bare interpreter name: prefer the project's virtualenv.
    if first in ("python", "python3", "python.exe"):
        for interpreter in (
            os.path.join(working_dir, ".venv", "Scripts", "python.exe"),
            os.path.join(working_dir, ".venv", "bin", "python"),
        ):
            if os.path.exists(interpreter):
                resolved[0] = interpreter
                break
        return resolved

    if first.startswith("./"):
        resolved[0] = os.path.join(working_dir, first[2:])
    return resolved


def _quoted_start_command(app: AppSpec) -> str:
    parts = []
    for index, part in enumerate(app.start_command):
        if index == 0:
            parts.append(_resolve_command(app.start_command, app.working_dir)[0])
        else:
            parts.append(f'"{part}"' if " " in part else part)
    return " ".join(parts)


def _process_start_time(pid: int) -> int | None:
    """
    Unix timestamp of when the process started.

    The start time is formatted as ISO 8601 by PowerShell because the
    default rendering is culture dependent ("Wednesday, November 11,
    2026 10:27:28 AM"), which no parser can rely on. Note that
    ``ToUnixTimeSeconds()`` does not exist in the .NET Framework that
    ships with Windows PowerShell, so the timestamp is formatted instead
    of converted.
    """
    result = _powershell(
        f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue)"
        f'.StartTime.ToString("yyyy-MM-ddTHH:mm:ss")'
    )
    if not result.ok or not result.stdout.strip():
        return None
    try:
        from datetime import datetime

        text = result.stdout.strip().splitlines()[0]
        return int(datetime.fromisoformat(text).timestamp())
    except (ValueError, IndexError):
        return None


def _process_memory(pid: int) -> int | None:
    result = _powershell(
        f"(Get-Process -Id {pid} -ErrorAction SilentlyContinue).WorkingSet64"
    )
    if not result.ok or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
