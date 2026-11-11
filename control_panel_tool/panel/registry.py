"""
The application registry - the YAML file that declares every application
the panel manages.

The file is meant to be hand-editable and also writable from the panel's
"Add application" form. Writes are atomic so a crash cannot leave a
half-written registry behind.
"""

from __future__ import annotations

import os
import tempfile
from typing import Iterable

import yaml

from .models import AppSpec


DEFAULT_REGISTRY = """\
# Server Control Panel - managed applications
#
# Every entry describes one Python application the panel can show, start,
# stop, restart and schedule to start automatically. Add them from the
# "Applications" page, or copy an example from apps.example.yaml.
#
#   name           unique id (also the systemd unit / scheduled task name)
#   description    shown on the dashboard
#   working_dir    where the app lives
#   start_command  argv list used to start it
#   env            environment variables for the process
#                  PROCESS_MATCH / PID_FILE / LOG_FILE are understood by
#                  the Windows backend
#   ports          ports the app is expected to listen on
#   health_url     URL probed for liveness
#   service_name   systemd unit / scheduled task name (defaults to name)
#   service_scope  "user" (systemctl --user, no root) or "system" (sudo)
#   autostart      start automatically after a reboot
#
apps: []
"""


class Registry:
    """Loads and saves the list of managed applications."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)

    # -- reading -----------------------------------------------------------
    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> list[AppSpec]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{self.path}: top level must be a mapping")
        entries = data.get("apps") or []
        if not isinstance(entries, list):
            raise ValueError(f"{self.path}: 'apps' must be a list")
        apps = [AppSpec.from_dict(entry) for entry in entries]
        self._check_unique(apps)
        return apps

    def get(self, name: str) -> AppSpec | None:
        for app in self.load():
            if app.name == name:
                return app
        return None

    # -- writing -----------------------------------------------------------
    def save(self, apps: Iterable[AppSpec]) -> None:
        apps = list(apps)
        self._check_unique(apps)
        payload = {
            "apps": [app.to_dict() for app in apps],
        }
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        handle, tmp = tempfile.mkstemp(
            dir=directory, prefix=".registry-", suffix=".yaml"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write("# Server Control Panel - managed applications\n")
                yaml.safe_dump(
                    payload, fh, sort_keys=False, allow_unicode=True,
                    default_flow_style=False,
                )
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def upsert(self, app: AppSpec) -> None:
        apps = [a for a in self.load() if a.name != app.name]
        apps.append(app)
        apps.sort(key=lambda a: a.name.lower())
        self.save(apps)

    def remove(self, name: str) -> bool:
        apps = self.load()
        remaining = [a for a in apps if a.name != name]
        if len(remaining) == len(apps):
            return False
        self.save(remaining)
        return True

    def create_default(self, overwrite: bool = False) -> None:
        if self.exists() and not overwrite:
            return
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(DEFAULT_REGISTRY)

    # -- validation --------------------------------------------------------
    @staticmethod
    def _check_unique(apps: list[AppSpec]) -> None:
        seen: set[str] = set()
        for app in apps:
            if app.name in seen:
                raise ValueError(f"duplicate application name: {app.name!r}")
            seen.add(app.name)
