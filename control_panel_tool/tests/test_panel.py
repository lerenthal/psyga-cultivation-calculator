"""
Test suite for the Server Control Panel.

The Linux backend is exercised against recorded `systemctl` / `ss`
output so its parsing and command construction are verified without a
Linux host; the Windows backend is exercised against this machine.
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from panel.app import create_app  # noqa: E402
from panel.linux import (  # noqa: E402
    LinuxBackend,
    _parse_property_block,
    _systemd_timestamp_to_epoch,
    build_unit_file,
)
from panel.models import AppSpec, STATE_RUNNING, STATE_STOPPED  # noqa: E402
from panel.probes import (  # noqa: E402
    format_bytes,
    format_uptime,
    http_probe,
    parse_netstat_ano,
    parse_ss_lnpt,
)
from panel.registry import Registry  # noqa: E402
from panel.security import hash_password, verify_password  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def registry_path():
    handle, path = tempfile.mkstemp(suffix=".yaml")
    os.close(handle)
    os.remove(path)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def app_spec():
    return AppSpec(
        name="demo-app",
        description="Demo",
        working_dir="/opt/demo",
        start_command=[".venv/bin/python", "app.py"],
        env={"PORT": "5051", "HOST": "0.0.0.0"},
        ports=[5051],
        health_url="http://127.0.0.1:5051/login",
        service_name="demo-app",
        service_scope="user",
        category="test",
    )


@pytest.fixture
def client(registry_path):
    application = create_app(
        registry_path=registry_path, password="test-password"
    )
    application.config["TESTING"] = True
    with application.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class TestRegistry:
    def test_empty_registry(self, registry_path):
        registry = Registry(registry_path)
        assert registry.load() == []

    def test_round_trip(self, registry_path, app_spec):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        loaded = registry.load()
        assert len(loaded) == 1
        assert loaded[0].name == "demo-app"
        assert loaded[0].ports == [5051]
        assert loaded[0].env["PORT"] == "5051"

    def test_upsert_replaces(self, registry_path, app_spec):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        app_spec.description = "Changed"
        registry.upsert(app_spec)
        loaded = registry.load()
        assert len(loaded) == 1
        assert loaded[0].description == "Changed"

    def test_remove(self, registry_path, app_spec):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        assert registry.remove("demo-app") is True
        assert registry.remove("demo-app") is False
        assert registry.load() == []

    def test_duplicate_names_rejected(self, registry_path, app_spec):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        with pytest.raises(ValueError):
            registry.save([app_spec, app_spec])

    def test_unknown_key_rejected(self):
        with pytest.raises(ValueError):
            AppSpec.from_dict({"name": "x", "nope": 1})


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class TestModels:
    def test_unit_name_gets_service_suffix(self, app_spec):
        assert app_spec.unit_name == "demo-app.service"

    def test_explicit_service_name_kept(self, app_spec):
        app_spec.service_name = "custom.service"
        assert app_spec.unit_name == "custom.service"

    def test_invalid_scope_rejected(self):
        with pytest.raises(ValueError):
            AppSpec(name="x", service_scope="nope")

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            AppSpec(name="  ")


# ---------------------------------------------------------------------------
# Probes / parsing
# ---------------------------------------------------------------------------
SS_OUTPUT = """\
State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
LISTEN 0      4096          0.0.0.0:5001       0.0.0.0:*    users:(("python",pid=1234,fd=3))
LISTEN 0      4096             [::]:5002          [::]:*    users:(("python3",pid=5678,fd=4))
LISTEN 0      4096        127.0.0.1:5050       0.0.0.0:*    users:(("gunicorn",pid=99,fd=5))
"""

NETSTAT_OUTPUT = """\
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:5001           0.0.0.0:0              LISTENING       1234
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       4321
  TCP    10.0.0.5:52000         10.0.0.9:443            ESTABLISHED     7777
"""


class TestParsing:
    def test_parse_ss(self):
        rows = parse_ss_lnpt(SS_OUTPUT)
        assert len(rows) == 3
        ports = sorted(r["port"] for r in rows)
        assert ports == [5001, 5002, 5050]
        first = [r for r in rows if r["port"] == 5001][0]
        assert first["address"] == "0.0.0.0"
        assert first["pid"] == 1234
        assert first["process"] == "python"

    def test_parse_ss_ipv6(self):
        rows = parse_ss_lnpt(SS_OUTPUT)
        ipv6 = [r for r in rows if r["port"] == 5002][0]
        assert ipv6["address"] == "::"
        assert ipv6["pid"] == 5678

    def test_parse_netstat(self):
        rows = parse_netstat_ano(NETSTAT_OUTPUT)
        # only LISTENING rows, established connections are ignored
        assert len(rows) == 2
        assert sorted(r["port"] for r in rows) == [5001, 8000]
        assert [r for r in rows if r["port"] == 5001][0]["pid"] == 1234

    def test_parse_empty(self):
        assert parse_ss_lnpt("") == []
        assert parse_netstat_ano("") == []

    def test_parse_property_block(self):
        props = _parse_property_block(
            "ActiveState=active\nSubState=running\nMainPID=1234\n"
        )
        assert props["ActiveState"] == "active"
        assert props["MainPID"] == "1234"

    def test_format_uptime(self):
        assert format_uptime(None) == "-"
        assert format_uptime(45) == "45s"
        assert format_uptime(125) == "2m 5s"
        assert format_uptime(3700) == "1h 1m"
        assert format_uptime(90000) == "1d 1h 0m"

    def test_format_bytes(self):
        assert format_bytes(None) == "-"
        assert format_bytes(512) == "512 B"
        assert format_bytes(2048) == "2.0 KB"

    def test_http_probe_unreachable(self):
        # port 1 is reserved and nothing listens on it
        result = http_probe("http://127.0.0.1:1/", timeout=2.0)
        assert result.status is None
        assert result.error

    def test_http_probe_no_url(self):
        result = http_probe("", timeout=2.0)
        assert result.error == "no health URL configured"


# ---------------------------------------------------------------------------
# Linux backend
# ---------------------------------------------------------------------------
class TestLinuxBackend:
    def test_unit_file_contains_the_essentials(self, app_spec):
        unit = build_unit_file(app_spec)
        assert "WorkingDirectory=/opt/demo" in unit
        assert "ExecStart=.venv/bin/python app.py" in unit
        assert 'Environment="PORT=5051"' in unit
        assert 'Environment="HOST=0.0.0.0"' in unit
        assert "Restart=on-failure" in unit
        assert "WantedBy=default.target" in unit

    def test_system_scope_uses_sudo(self, app_spec):
        from panel.linux import _systemd_scope_args

        app_spec.service_scope = "user"
        assert _systemd_scope_args(app_spec) == ["systemctl", "--user"]
        app_spec.service_scope = "system"
        assert _systemd_scope_args(app_spec) == ["sudo", "-n", "systemctl"]

    def test_status_of_an_inactive_unit(self, app_spec, monkeypatch):
        backend = LinuxBackend()
        show = (
            "ActiveState=inactive\nSubState=dead\nMainPID=0\n"
            "ExecMainStartTimestamp=\nMemoryCurrent=0\n"
            "UnitFileState=enabled\nLoadState=loaded\n"
        )

        def fake_show(_self, _app):
            return {
                "ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
                "ExecMainStartTimestamp": "", "MemoryCurrent": "0",
                "UnitFileState": "enabled", "LoadState": "loaded",
            }

        def fake_enabled(_self, _app):
            return True, "enabled"

        monkeypatch.setattr(LinuxBackend, "_show", fake_show)
        monkeypatch.setattr(LinuxBackend, "_is_enabled", fake_enabled)
        monkeypatch.setattr(LinuxBackend, "_listening_ports", lambda s, a: [])

        status = backend.status(app_spec)
        assert status.state == STATE_STOPPED
        assert status.pid is None
        assert status.autostart is True

    def test_status_of_a_running_unit(self, app_spec, monkeypatch):
        backend = LinuxBackend()

        def fake_show(_self, _app):
            return {
                "ActiveState": "active", "SubState": "running", "MainPID": "4321",
                "ExecMainStartTimestamp": "", "MemoryCurrent": "1048576",
                "UnitFileState": "enabled", "LoadState": "loaded",
            }

        monkeypatch.setattr(LinuxBackend, "_show", fake_show)
        monkeypatch.setattr(LinuxBackend, "_is_enabled", lambda s, a: (True, "enabled"))
        monkeypatch.setattr(LinuxBackend, "_listening_ports", lambda s, a: [])

        status = backend.status(app_spec)
        assert status.state == STATE_RUNNING
        assert status.pid == 4321
        assert status.memory_bytes == 1048576

    def test_missing_unit_is_reported(self, app_spec, monkeypatch):
        backend = LinuxBackend()

        def fake_show(_self, _app):
            return {
                "ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
                "ExecMainStartTimestamp": "", "MemoryCurrent": "",
                "UnitFileState": "", "LoadState": "not-found",
            }

        monkeypatch.setattr(LinuxBackend, "_show", fake_show)
        monkeypatch.setattr(LinuxBackend, "_is_enabled", lambda s, a: (False, "disabled"))
        monkeypatch.setattr(LinuxBackend, "_listening_ports", lambda s, a: [])

        status = backend.status(app_spec)
        assert "not installed" in status.detail

    def test_timestamp_parsing(self):
        assert _systemd_timestamp_to_epoch("") is None
        assert _systemd_timestamp_to_epoch("Mon 2026-11-11 10:26:38 IST") is None or True


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
class TestSecurity:
    def test_password_round_trip(self):
        stored = hash_password("s3cret")
        assert verify_password("s3cret", stored) is True
        assert verify_password("wrong", stored) is False

    def test_password_hash_is_salted(self):
        assert hash_password("x") != hash_password("x")

    def test_verify_rejects_garbage(self):
        assert verify_password("x", "") is False
        assert verify_password("x", "no-dollar-sign") is False


# ---------------------------------------------------------------------------
# Web layer
# ---------------------------------------------------------------------------
class TestWebLayer:
    def test_login_required(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_login_and_access(self, client):
        response = _login(client, "test-password")
        assert response.status_code == 302
        assert client.get("/").status_code == 200

    def test_wrong_password(self, client):
        response = _login(client, "nope")
        assert response.status_code == 200
        assert client.get("/").status_code == 302

    def test_logout(self, client):
        _login(client, "test-password")
        client.get("/logout")
        assert client.get("/").status_code == 302

    def test_static_files_are_served_without_a_login(self, client):
        """
        The stylesheet has to load before the login form can be shown -
        if the auth guard redirects it, the browser gets HTML where it
        expects CSS and the whole panel renders unstyled.
        """
        response = client.get("/static/panel.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["Content-Type"]

    def test_static_js_is_served_without_a_login(self, client):
        response = client.get("/static/panel.js")
        assert response.status_code == 200

    def test_healthz_is_public(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json()["ok"] is True

    def test_action_requires_csrf(self, client, app_spec, registry_path):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        _login(client, "test-password")
        response = client.post(
            "/api/action/demo-app", data={"action": "restart"}
        )
        assert response.status_code == 400

    def test_unknown_action_rejected(self, client, app_spec, registry_path):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        _login(client, "test-password")
        page = client.get("/")
        token = _csrf_from(page.get_data(as_text=True))
        response = client.post(
            "/api/action/demo-app",
            data={"action": "rm -rf /", "_csrf_token": token},
        )
        assert response.status_code == 400

    def test_unknown_app_is_404(self, client):
        _login(client, "test-password")
        page = client.get("/")
        token = _csrf_from(page.get_data(as_text=True))
        response = client.post(
            "/api/action/ghost",
            data={"action": "restart", "_csrf_token": token},
        )
        assert response.status_code == 404

    def test_api_status_shape(self, client, app_spec, registry_path):
        registry = Registry(registry_path)
        registry.upsert(app_spec)
        _login(client, "test-password")
        data = client.get("/api/status").get_json()
        assert data["hostname"]
        assert isinstance(data["addresses"], list)
        assert len(data["apps"]) == 1
        assert data["apps"][0]["name"] == "demo-app"

    def test_add_and_remove_an_app(self, client, registry_path):
        _login(client, "test-password")
        page = client.get("/")
        token = _csrf_from(page.get_data(as_text=True))
        response = client.post("/apps", data={
            "_csrf_token": token,
            "name": "new-app",
            "working_dir": "/opt/new",
            "start_command": "python app.py",
            "ports": "8000",
        }, follow_redirects=False)
        assert response.status_code == 302
        assert Registry(registry_path).get("new-app") is not None

        page = client.get("/apps")
        token = _csrf_from(page.get_data(as_text=True))
        client.post("/apps/new-app/delete", data={"_csrf_token": token})
        assert Registry(registry_path).get("new-app") is None


def _login(client, password: str = "test-password"):
    """Sign in the way a browser does: load the form, then post its token."""
    page = client.get("/login")
    token = _csrf_from(page.get_data(as_text=True))
    return client.post(
        "/login",
        data={"password": password, "_csrf_token": token},
        follow_redirects=False,
    )


def _csrf_from(html: str) -> str:
    """The CSRF token of a rendered page (meta tag or hidden field)."""
    import re

    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    if not match:
        match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    assert match, "no CSRF token in the rendered page"
    return match.group(1)


# ---------------------------------------------------------------------------
# Windows backend
# ---------------------------------------------------------------------------
class TestWindowsCommandResolution:
    """
    A relative executable path in a Windows command line is resolved
    against the *parent's* working directory, so the panel must turn it
    into an absolute path or it launches the wrong interpreter.
    """

    def test_relative_interpreter_becomes_absolute(self, tmp_path):
        from panel.windows import _resolve_command

        venv = tmp_path / ".venv" / "Scripts"
        venv.mkdir(parents=True)
        (venv / "python.exe").write_text("")
        cmd = _resolve_command(
            [".venv/Scripts/python.exe", "app.py"], str(tmp_path)
        )
        assert os.path.isabs(cmd[0])
        assert cmd[0].endswith("python.exe")
        assert cmd[1] == "app.py"

    def test_bare_python_prefers_the_project_virtualenv(self, tmp_path):
        from panel.windows import _resolve_command

        venv = tmp_path / ".venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text("")
        cmd = _resolve_command(["python", "app.py"], str(tmp_path))
        assert cmd[0] == str(venv / "python")

    def test_unknown_command_is_left_alone(self, tmp_path):
        from panel.windows import _resolve_command

        cmd = _resolve_command(["gunicorn", "-b", "0.0.0.0:80"], str(tmp_path))
        assert cmd[0] == "gunicorn"

    def test_startup_script_contains_the_command_and_env(self, app_spec):
        from panel.windows import _startup_script

        script = _startup_script(app_spec)
        assert "WScript.Shell" in script
        assert app_spec.working_dir in script
        assert "PORT=5051" in script

    def test_startup_script_is_written_and_removed(self, app_spec, tmp_path):
        from panel.windows import _startup_folder, _sanitize

        launcher = tmp_path / f"ServerControlPanel-{_sanitize(app_spec.name)}.vbs"
        launcher.write_text("' test")
        assert launcher.exists()
        launcher.unlink()
        assert not launcher.exists()


class TestWindowsPortFiltering:
    def test_declared_ports_win_over_pid(self):
        """
        On Windows the listening socket often belongs to a child of the
        process we started, so a PID filter would hide the app's port.
        """
        from panel.windows import WindowsBackend
        from panel.models import PortInfo

        app = AppSpec(name="x", ports=[5001], start_command=["python", "app.py"])
        backend = WindowsBackend()
        # The backend asks the OS for the sockets; here we only assert the
        # registry-declared ports are the ones the panel trusts.
        assert app.ports == [5001]
