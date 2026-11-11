# Server Control Panel

A small web dashboard for the Python applications installed on a machine.
It shows whether each app is running, what port and IP it is serving on,
and gives you a one-click **Start / Stop / Restart** plus a toggle that
makes an app **come back automatically after a reboot**.

It is designed to be the control dashboard for *all* your Python apps —
Flask, Django, Streamlit, FastAPI, anything that runs as a process — and
it works on both **Ubuntu (systemd)** and **Windows**, with the same
registry file driving both.

> This tool ships as part of the
> [PsygaBio Production System](..) repository, in `control_panel_tool/`.
> It is completely independent of that application — the PsygaBio app is
> simply one entry in its registry, and the panel manages any other
> Python application on the same machine in exactly the same way. Its
> overview is also described in the
> [top-level README](../README.md#server-control-panel-auxiliary-tool).

```
┌──────────────────────────────────────────────────────────┐
│  Server Control Panel   (web, port 5050)                │
│  ├─ psyga-bio      running   :5001   http://10.0.0.5:5001│
│  │   [Start] [Stop] [Restart]  Autostart: ON             │
│  ├─ my-django-app  stopped   :8000   —                   │
│  └─ dashboards    running   :8501   http://10.0.0.5:8501│
└──────────────────────────────────────────────────────────┘
```

## Why it is safe to expose

* The panel can only act on applications **declared in `apps.yaml`**.
  There is deliberately **no "run this command" endpoint** — the only
  commands it can ever run are the fixed ones the backend defines.
* Everything is behind a **password** (session cookie, `HttpOnly` +
  `SameSite=Lax`), and every state-changing request needs a **CSRF
  token**.
* The panel itself holds no application secrets; it reads the registry
  and asks the OS what is running.

## Quick start (Ubuntu)

```bash
git clone <this repo> server_control_panel
cd server_control_panel
./deploy/install_ubuntu.sh          # user-level systemd, no root needed
```

The first start prints a generated password. Then open
**http://<server-ip>:5050**, sign in, and add your applications under
**Applications** (or copy entries from `apps.example.yaml` into
`apps.yaml`).

To run it as a system service instead:

```bash
sudo ./deploy/install_ubuntu.sh --system
```

## Quick start (Windows)

```powershell
uv venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
$env:PANEL_PASSWORD = "choose-something"
.\.venv\Scripts\python.exe dashboard.py
```

`run.bat` / `run.ps1` do the same thing without the `activate` dance.

## The registry — `apps.yaml`

One entry per application. This is the whole configuration surface.

| Field | Meaning |
|---|---|
| `name` | unique id; also the systemd unit / scheduled task name |
| `description` | shown on the dashboard |
| `working_dir` | where the app lives (the panel starts it here) |
| `start_command` | argv list, e.g. `[".venv/bin/python", "app.py"]` |
| `env` | environment for the process. On Windows the panel also reads `PROCESS_MATCH` (a substring of the command line used to find the process), `PID_FILE` and `LOG_FILE` |
| `ports` | the ports the app is expected to listen on — the panel shows the **bound address** for each one |
| `health_url` | URL probed for liveness; the dashboard shows the HTTP status and latency |
| `service_name` | systemd unit / scheduled task name (defaults to `name`) |
| `service_scope` | Linux only: `user` (`systemctl --user`, no root) or `system` (`sudo systemctl`) |
| `autostart` | start automatically after a reboot |
| `category` | free grouping label shown on the card |

A complete example for each framework is in **`apps.example.yaml`**.

## How it works on Ubuntu

The panel drives **systemd**, so the apps it manages are ordinary
services with ordinary logs.

* **Status** — `systemctl show <unit>` for the state, PID, uptime and
  memory, `systemctl is-enabled` for the boot state, `ss -lntp` for the
  listening sockets, and an HTTP probe of `health_url`.
* **Start / Stop / Restart** — `systemctl start|stop|restart <unit>`.
* **Autostart** — `systemctl --user enable <unit>` (or `disable`).
  With the `user` scope, also run `sudo loginctl enable-linger <user>`
  once so the service starts at *boot* and not only after a login.
* **Install service** — the panel writes a unit file for an app that
  does not have one yet, from the registry entry:

  ```ini
  [Service]
  Type=simple
  WorkingDirectory=/opt/psyga/ver5
  Environment="PORT=5001"
  Environment="HOST=0.0.0.0"
  ExecStart=.venv/bin/python app.py
  Restart=on-failure
  ```

* **Logs** — `systemctl status`, falling back to `journalctl`.

For the `system` scope the panel runs `sudo -n systemctl`, so grant it
passwordless rights once:

```bash
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/systemctl, /usr/bin/tee, /usr/bin/cp" \
  | sudo tee /etc/sudoers.d/server-control-panel
```

## How it works on Windows

There is no systemd, so the panel does what an operator would do by hand:

* finds the process by matching its command line against `PROCESS_MATCH`
  (or the start command), preferring the launcher process over the
  interpreter it spawned,
* reads the listening ports from `netstat -ano`,
* starts the app detached with the project's own virtualenv
  interpreter (always resolved to an absolute path - a relative path in
  a Windows command line would otherwise launch the wrong Python),
* stops it with `taskkill /PID <pid> /T /F`, killing the whole tree,
* and makes it start automatically at logon by writing a small VBScript
  into the **per-user Startup folder**.

The Startup folder is used rather than a Windows scheduled task because
creating a scheduled task needs administrator rights, which the panel
does not assume it has. The trade-off is that the app starts at *logon*
rather than at *boot* — the same trade-off as a systemd `--user` unit
without lingering. The generated entry is a single file you can see and
delete yourself:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
    ServerControlPanel-<app>.vbs
```

## Security notes for a remote server

* Put it behind **nginx + TLS** — `deploy/nginx-control-panel.conf` is
  a ready-made site. Use a certificate from certbot.
* Prefer `PANEL_PASSWORD` from the environment (or `EnvironmentFile=`
  in the systemd unit) over the generated password file.
* The panel binds `0.0.0.0` so it is reachable from the network. If you
  only want it locally, set `PANEL_HOST=127.0.0.1` and reach it over an
  SSH tunnel:
  `ssh -L 5050:127.0.0.1:5050 user@server`.
* `PANEL_SECRET_KEY` pins the session signing key across restarts.

| Variable | Purpose |
|---|---|
| `PANEL_PASSWORD` | the panel login password (generated if unset) |
| `PANEL_SECRET_KEY` | session signing key (persisted if unset) |
| `PANEL_HOST` / `PORT` | where the panel listens (default `0.0.0.0:5050`) |
| `PANEL_REGISTRY` | path to `apps.yaml` |
| `PANEL_INSTANCE_DIR` | where the generated password and secret live |
| `PANEL_DEBUG` | `1` enables the Flask debugger (development only) |

## API

All endpoints need a session; the action endpoints also need the CSRF
token.

| Endpoint | Purpose |
|---|---|
| `GET /` | the dashboard |
| `GET /api/status` | JSON snapshot of every application (the page polls this) |
| `POST /api/action/<name>` | `action` = `start` / `stop` / `restart` / `autostart_on` / `autostart_off` / `install_service` |
| `GET /api/logs/<name>` | the last N log lines |
| `GET /apps` | registry editor |
| `GET /healthz` | liveness probe (public, no login) |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

38 tests covering the registry, the `ss`/`netstat` parsers, the systemd
unit generation, the password handling, the CSRF enforcement and the web
layer.

## Layout

```
dashboard.py          entry point (also works as a WSGI module: dashboard:app)
panel/
  app.py              Flask app, routes, auth and CSRF wiring
  models.py           AppSpec / AppStatus / ActionResult
  registry.py         reading and writing apps.yaml
  linux.py            systemd backend
  windows.py          Windows process backend
  probes.py           ss/netstat parsing, HTTP health, uptime formatting
  security.py         password hashing, secret key, backend selection
templates/            dashboard, registry editor, login
static/               panel.css, panel.js (live refresh + log viewer)
apps.yaml             the registry (yours)
apps.example.yaml     ready-made examples for Flask / Django / Streamlit
deploy/               systemd units, nginx site, Ubuntu installer
tests/                pytest suite
```
