"""
The Server Control Panel - a small web dashboard that shows the state of
the Python applications installed on a machine and lets an administrator
start, stop, restart and schedule them.

It is deliberately narrow on purpose: the panel can only act on
applications declared in the registry, and the only commands it ever
runs are the ones defined by the backend for that platform. There is no
"run this command" endpoint.
"""

from __future__ import annotations

import os
import platform
import socket
import time

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .base import Backend
from .models import AppSpec, AppStatus
from .probes import format_bytes, format_uptime, local_ip_addresses
from .registry import Registry
from .security import (
    CSRF_SAFE_METHODS,
    SESSION_KEY_NAME,
    constant_time_equals,
    hash_password,
    resolve_panel_password,
    resolve_secret_key,
    select_backend,
    verify_password,
)

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)


def create_app(
    registry_path: str | None = None,
    backend: Backend | None = None,
    password: str | None = None,
) -> Flask:
    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, "templates"),
                static_folder=os.path.join(PROJECT_ROOT, "static"))
    app.config["SECRET_KEY"] = resolve_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = 12 * 3600

    registry = Registry(
        registry_path
        or os.environ.get("PANEL_REGISTRY")
        or os.path.join(PROJECT_ROOT, "apps.yaml")
    )
    registry.create_default()

    panel_password = password or resolve_panel_password()
    app.config["PANEL_PASSWORD_HASH"] = hash_password(panel_password)

    backend = backend or select_backend()
    app.config["BACKEND"] = backend
    app.config["REGISTRY"] = registry

    _register_auth(app)
    _register_csrf(app)
    _register_routes(app)
    return app


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _register_auth(app: Flask) -> None:
    @app.before_request
    def _require_login():
        if request.path in ("/login", "/healthz"):
            return None
        # The stylesheet and the little helper scripts must load before
        # the login form can be shown - redirecting them to /login would
        # serve HTML where a CSS file is expected.
        if request.endpoint == "static":
            return None
        if not session.get("authenticated"):
            return redirect(url_for("login", next=request.path))
        return None

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response


def _register_csrf(app: Flask) -> None:
    def issue_token() -> str:
        token = session.get(SESSION_KEY_NAME)
        if not token:
            import secrets

            token = secrets.token_urlsafe(32)
            session[SESSION_KEY_NAME] = token
        return token

    @app.before_request
    def _csrf_protect():
        if request.method in CSRF_SAFE_METHODS:
            return None
        expected = session.get(SESSION_KEY_NAME)
        sent = request.form.get("_csrf_token") or request.headers.get(
            "X-CSRF-Token", ""
        )
        if not expected or not sent or not constant_time_equals(sent, expected):
            abort(400, description="Invalid or missing CSRF token.")
        return None

    @app.context_processor
    def _inject():
        return {"csrf_token": issue_token, "panel_backend": app.config["BACKEND"]}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def _collect_status(backend: Backend, registry: Registry) -> list[dict]:
    """Build the dashboard payload for every registered application."""
    payload = []
    for spec in registry.load():
        try:
            status = backend.status(spec)
        except Exception as exc:  # noqa: BLE001 - never break the dashboard
            status = AppStatus(name=spec.name, state="unknown")
            status.error = f"{type(exc).__name__}: {exc}"
        data = status.to_dict()
        data["spec"] = spec.to_dict()
        data["uptime_text"] = format_uptime(status.uptime_seconds)
        data["memory_text"] = format_bytes(status.memory_bytes)
        payload.append(data)
    return payload


def _register_routes(app: Flask) -> None:
    registry: Registry = app.config["REGISTRY"]
    backend: Backend = app.config["BACKEND"]

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            supplied = request.form.get("password", "")
            stored = app.config["PANEL_PASSWORD_HASH"]
            if verify_password(supplied, stored):
                session.clear()
                session["authenticated"] = True
                session.permanent = True
                target = request.args.get("next") or url_for("dashboard")
                if not target.startswith("/"):
                    target = url_for("dashboard")
                return redirect(target)
            flash("Wrong password.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "backend": backend.platform})

    @app.route("/")
    def dashboard():
        entries = _collect_status(backend, registry)
        addresses = local_ip_addresses()
        return render_template(
            "dashboard.html",
            entries=entries,
            addresses=addresses,
            hostname=socket.gethostname(),
            platform_name=f"{platform.system()} {platform.release()}",
            backend=backend,
            panel_port=os.environ.get("PORT", "5050"),
        )

    @app.route("/api/status")
    def api_status():
        entries = _collect_status(backend, registry)
        return jsonify({
            "hostname": socket.gethostname(),
            "addresses": local_ip_addresses(),
            "platform": platform.system(),
            "backend": backend.platform,
            "apps": entries,
            "generated_at": int(time.time()),
        })

    @app.route("/api/action/<name>", methods=["POST"])
    def api_action(name):
        action = request.form.get("action", "").strip()
        spec = registry.get(name)
        if spec is None:
            abort(404, description=f"no such application: {name}")

        if action == "start":
            result = backend.start(spec)
        elif action == "stop":
            result = backend.stop(spec)
        elif action == "restart":
            result = backend.restart(spec)
        elif action == "autostart_on":
            result = backend.set_autostart(spec, True)
        elif action == "autostart_off":
            result = backend.set_autostart(spec, False)
        elif action == "install_service":
            result = backend.install_service(spec)
        else:
            abort(400, description=f"unknown action: {action!r}")

        if not result.ok:
            flash(f"{spec.name}: {result.message}", "danger")
            if result.detail:
                flash(result.detail, "info")
        else:
            flash(f"{spec.name}: {result.message}", "success")
            if result.detail:
                flash(result.detail, "info")
        return redirect(url_for("dashboard"))

    @app.route("/api/logs/<name>")
    def api_logs(name):
        spec = registry.get(name)
        if spec is None:
            abort(404, description=f"no such application: {name}")
        lines = request.args.get("lines", 80, type=int)
        lines = max(10, min(lines, 500))
        return jsonify({
            "name": name,
            "logs": backend.logs(spec, lines),
        })

    @app.route("/apps", methods=["GET", "POST"])
    def apps():
        if request.method == "POST":
            try:
                spec = AppSpec(
                    name=request.form.get("name", "").strip(),
                    description=request.form.get("description", "").strip(),
                    working_dir=request.form.get("working_dir", "").strip(),
                    start_command=[
                        part for part in
                        request.form.get("start_command", "").split() if part
                    ],
                    ports=[
                        int(p) for p in
                        request.form.get("ports", "").replace(",", " ").split()
                        if p.strip().isdigit()
                    ],
                    health_url=request.form.get("health_url", "").strip(),
                    service_name=request.form.get("service_name", "").strip(),
                    service_scope=request.form.get("service_scope", "user"),
                    category=request.form.get("category", "").strip(),
                )
                registry.upsert(spec)
                flash(f"{spec.name} added to the registry", "success")
                return redirect(url_for("dashboard"))
            except (ValueError, TypeError) as exc:
                flash(f"Could not add the application: {exc}", "danger")
        return render_template("apps.html", apps=registry.load())

    @app.route("/apps/<name>/delete", methods=["POST"])
    def delete_app(name):
        if registry.remove(name):
            flash(f"{name} removed from the registry", "success")
        else:
            flash(f"{name} was not in the registry", "info")
        return redirect(url_for("apps"))
