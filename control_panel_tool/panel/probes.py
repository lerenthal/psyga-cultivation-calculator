"""
Small helpers shared by the backends: running commands, parsing the
output of ``ss`` / ``netstat``, probing HTTP health and formatting
uptimes.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout or "") + (self.stderr or "")


def run(
    args: list[str],
    timeout: float = 20.0,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """
    Run a command and capture its output. Never raises on a non-zero exit
    code - the caller decides what that means.
    """
    merged_env = None
    if env:
        merged_env = dict(os.environ)
        merged_env.update({str(k): str(v) for k, v in env.items()})
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
            env=merged_env,
            shell=False,
        )
    except FileNotFoundError:
        return CommandResult(127, "", f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"command timed out after {timeout}s")
    return CommandResult(proc.returncode, proc.stdout or "", proc.stderr or "")


# ---------------------------------------------------------------------------
# Listening-port parsing
# ---------------------------------------------------------------------------
# ss -lntp example lines:
#   LISTEN 0      4096   0.0.0.0:5001     0.0.0.0:*    users:(("python",pid=1234,fd=3))
# (with "tcp" as the first column when the protocol is requested)
# netstat -ano example lines:
#   TCP    0.0.0.0:5001     0.0.0.0:0     LISTENING     1234
_SS_RE = re.compile(
    r"^(?:(?P<proto>tcp|udp)\s+)?"
    r"(?P<state>\S+)\s+(?P<recvq>\S+)\s+(?P<sendq>\S+)\s+"
    r"(?P<local>\S+)\s+(?P<peer>\S+)\s*(?P<process>.*)$"
)
_NETSTAT_RE = re.compile(
    r"^\s*TCP\s+(?P<local>\S+)\s+\S+\s+LISTENING\s+(?P<pid>\d+)\s*$"
)


def _split_local(local: str) -> tuple[str, int]:
    """Split '0.0.0.0:5001' or '[::]:5001' into (address, port)."""
    if local.startswith("["):
        host, _, port = local.rpartition("]:")
        return host.lstrip("["), int(port)
    host, _, port = local.rpartition(":")
    return host, int(port)


def parse_ss_lnpt(text: str) -> list[dict]:
    """Parse the output of ``ss -lntp`` into listening socket dicts."""
    found: list[dict] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(("State", "Netid")):
            continue
        match = _SS_RE.match(line)
        if not match:
            continue
        if match.group("state").upper() != "LISTEN":
            continue
        local = match.group("local")
        try:
            address, port = _split_local(local)
        except (ValueError, IndexError):
            continue
        process_block = match.group("process") or ""
        pid_match = re.search(r"pid=(\d+)", process_block)
        name_match = re.search(r'\("([^"]+)"', process_block)
        found.append({
            "address": address,
            "port": port,
            "pid": int(pid_match.group(1)) if pid_match else None,
            "process": name_match.group(1) if name_match else "",
        })
    return found


def parse_netstat_ano(text: str) -> list[dict]:
    """Parse the output of ``netstat -ano`` into listening socket dicts."""
    found: list[dict] = []
    for line in (text or "").splitlines():
        match = _NETSTAT_RE.match(line)
        if not match:
            continue
        try:
            address, port = _split_local(match.group("local"))
        except (ValueError, IndexError):
            continue
        found.append({
            "address": address,
            "port": port,
            "pid": int(match.group("pid")),
            "process": "",
        })
    return found


# ---------------------------------------------------------------------------
# HTTP health
# ---------------------------------------------------------------------------
@dataclass
class ProbeResult:
    status: int | None = None
    latency_ms: int | None = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400


def http_probe(url: str, timeout: float = 5.0) -> ProbeResult:
    """
    GET a URL and report the status code and latency. Any HTTP response
    (even 4xx/5xx) counts as "the app answered".
    """
    if not url:
        return ProbeResult(error="no health URL configured")
    started = time.perf_counter()
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", "server-control-panel/1.0")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(2048)
            elapsed = int((time.perf_counter() - started) * 1000)
            return ProbeResult(status=response.status, latency_ms=elapsed)
    except urllib.error.HTTPError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        # An HTTP error still proves something is listening and answering.
        return ProbeResult(status=exc.code, latency_ms=elapsed)
    except Exception as exc:  # noqa: BLE001 - any failure is "unreachable"
        elapsed = int((time.perf_counter() - started) * 1000)
        return ProbeResult(
            latency_ms=elapsed,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
def format_uptime(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 0:
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_bytes(size: int | None) -> str:
    if size is None:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def local_ip_addresses() -> list[str]:
    """
    Best-effort list of this host's non-loopback IPv4 addresses. Used by
    the dashboard to show the URL other machines should use.
    """
    addresses: list[str] = []
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = info[4][0]
            if not address.startswith("127.") and address not in addresses:
                addresses.append(address)
    except OSError:
        pass

    # A UDP "connect" does not send anything but reveals the outbound
    # interface address - the usual way to find the LAN IP.
    if not addresses:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 53))
            address = sock.getsockname()[0]
            if not address.startswith("127."):
                addresses.append(address)
        except OSError:
            pass
        finally:
            sock.close()
    return addresses


def which(program: str) -> str | None:
    return shutil.which(program)


def tail(text: str, lines: int = 80) -> list[str]:
    if not text:
        return []
    return text.rstrip().splitlines()[-lines:]
