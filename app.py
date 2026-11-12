"""
PsygaBio Production System V6.0 - GMP Batch Calculators
=======================================================
A GMP-compliant three-tab Flask application providing:

  Tab 1 (Cultivation):  Substrate + grain-spawn batch scaling calculator
                        (Facility Working Protocol), with an optional
                        spawn-first reverse calculation (V3.0), plus
                        printable raw-material SOP forms (V4.0):
                        FORM-CULT-003 spawn (SP-YYYY-NNN),
                        FORM-CULT-003 incubation,
                        FORM-CULT-003A G5 inoculation (V6.0),
                        FORM-CULT-004 substrate (SUB-YYYY-NNN),
                        FORM-CULT-009 media (MED-YYYY-NNN).
  Tab 2 (Extraction):  Portion-based extraction planner (V5.0): the lot is
                        broken into whole 120 g portions (one portion per
                        2.5 L extraction bottle), with a per-bottle solvent
                        preparation protocol, lot-level reagent totals and
                        a calendar-day timeline
                        (Facility Extraction Batch Record).
  Tab 3 (Capsulation): Dosage / blend / capsule-fill calculator
                        (Capsulation Batch Record).

V2.0: every calculation option (containers, solvent strengths, dosage
modes, capsule sizes, batch sizes, molecular weights, ...) lives in
config.yaml and is validated on load. An admin adds or changes an
option by editing config.yaml only — no code change, no restart
(the file is re-read on every request), or from the Settings GUI.

V3.0: the Cultivation tab accepts optional colonized-spawn inputs
(spawn bags + bag size). When both are filled, the calculator
reverse-engineers the substrate needed from the spawn on hand
(spawn-limited container count; grain/LC not required).

V4.0: recipes aligned to the SOP forms (substrate 780/660/60/4800 =
6300 g per bucket; grain 5 kg : 10 L), and the WP printout includes
the raw-material production record forms populated with the
calculated amounts, numbered per SOP (SP/SUB/MED-YYYY-NNN, linked to
the WP master).

V5.0: the Extraction tab is re-based on whole 120 g portions instead of
storage packs. The "Dry Mushroom Powder Lot #" and "Pack Size" inputs
are gone (the production lot identifies the material), and every bottle
is now documented with an explicit per-bottle solvent preparation
protocol (how much MeOH to remove and how much water to add, per
bottle), lot-level bottle/reagent totals and a calendar-day timeline.

V5.0 hardening (see README "V5.0 changes"):
  * the append-only audit ledger is enforced by SQLite triggers, so bulk
    deletes and raw SQL cannot rewrite history either;
  * SECRET_KEY comes from the environment (or a persisted random key),
    never from the source tree;
  * debug mode is OFF unless PSYGA_DEBUG=1 is set, so the Werkzeug
    debugger is never exposed on a shared host;
  * CSRF tokens protect every state-changing POST;
  * SQLite runs in WAL mode with foreign keys enforced.

V6.0: the spawn bag fill weight is an operator input on the cultivation
form (defaulting to the container's own spawn weight) and it decides how
many spawn bags have to be filled. Liquid culture is dosed by spawn
mass - 5 mL per 200 g of spawn by default - instead of per container,
with a 20 % bench backup on the prepared volume. The new printable
FORM-CULT-003A "G5 Inoculation Record" logs the inoculation of every
bag, with one row per bag and 30 bags per printed sheet.

V6.1: every printed form is produced on its own ("Produce a form"
panel), so each document keeps its own page numbering and its own
running header; and every signature carries its own date field.

Records of every generated document are persisted to a local SQLite
database as an immutable ledger (BatchLog), each row stamped with a
sequential document number (WP-YYYYMMDD-NNN / EX-YYYYMMDD-NNN /
CP-YYYYMMDD-NNN) for GxP audit-trail purposes.
"""

import hmac
import json
import math
import os
import secrets
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone

import yaml
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash


# ---------------------------------------------------------------------------
# Application factory & extensions
# ---------------------------------------------------------------------------
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message_category = "warning"

# Lock guarding the read-then-write sequence of generate_sequence_number().
# SQLite in WAL mode is safe for concurrent reads, but the count-then-insert
# step must be atomic so two simultaneous requests do not pick the same NNN.
# The lock only covers threads of one process; the UNIQUE constraint on
# BatchLog.doc_number plus the retry loop in _persist_batch() is what
# actually protects the sequence across processes.
_sequence_lock = threading.Lock()

# HTTP methods that must not change state and therefore need no CSRF token.
CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Version tag written into every calculated payload. The reprint route uses
# it to reproduce a stored document exactly as it was issued.
CALCULATOR_VERSION = "6"


def _env_flag(name, default=False):
    """Read a boolean environment variable ("1"/"true"/"yes"/"on")."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_secret_key(app):
    """
    Resolve the Flask session secret.

    Order: PSYGA_SECRET_KEY env var -> persisted random key in the instance
    folder -> freshly generated + persisted key. A hard-coded default is
    never used: a known secret lets anyone forge a session cookie and
    impersonate an administrator.
    """
    env_key = os.environ.get("PSYGA_SECRET_KEY")
    if env_key and env_key.strip():
        return env_key.strip()

    key_file = os.path.join(app.instance_path, "secret_key")
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as f:
                stored = f.read().strip()
            if stored:
                return stored
        except OSError:
            pass

    generated = secrets.token_hex(32)
    try:
        os.makedirs(app.instance_path, exist_ok=True)
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(generated)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass  # not supported on this filesystem
    except OSError:
        pass  # a random per-process key is still better than a known one
    return generated


@event.listens_for(Engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    """
    Per-connection SQLite settings.

    * WAL          - readers no longer block the writer, which is what makes
                     concurrent access to the ledger safe.
    * foreign_keys - SQLite ignores FK constraints unless asked; without this
                     the batch_logs.user_id reference is not enforced.
    * busy_timeout - wait instead of failing immediately when another
                     process holds the write lock.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass  # e.g. in-memory databases do not support WAL
    finally:
        cursor.close()


def create_app(config_overrides=None):
    """Application factory. Tests inject an in-memory database here."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = _resolve_secret_key(app)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///psyga_gmp.db"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # Debug mode exposes the Werkzeug debugger, which is a remote code
    # execution surface. It is off unless explicitly requested.
    app.config["DEBUG"] = _env_flag("PSYGA_DEBUG", False)
    if config_overrides:
        app.config.update(config_overrides)

    db.init_app(app)
    login_manager.init_app(app)
    _register_csrf_protection(app)

    @app.template_filter("g")
    def _format_g(value):
        """
        Render a number without a pointless trailing ".0"
        (40.0 -> 40, 0.5 -> 0.5), so printed documents read like a
        bench protocol rather than a float dump.
        """
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return value

    with app.app_context():
        db.create_all()
        _seed_default_admin()
        _apply_migrations()
        _install_immutability_triggers()

    return app


def _register_csrf_protection(app):
    """
    Add CSRF protection to every state-changing request.

    A random token is issued per session and embedded in every form as a
    hidden ``_csrf_token`` field. A POST whose token is missing or does not
    match the session token is rejected with HTTP 400 before any handler
    runs, so a third-party site cannot drive a logged-in operator's browser
    into issuing documents or changing accounts.
    """
    def issue_token():
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    @app.before_request
    def _csrf_protect():
        if request.method in CSRF_SAFE_METHODS:
            return None
        expected = session.get("_csrf_token")
        sent = request.form.get("_csrf_token") or request.headers.get(
            "X-CSRF-Token", ""
        )
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            abort(
                400,
                description=(
                    "Invalid or missing CSRF token. Reload the page and "
                    "submit the form again."
                ),
            )
        return None

    @app.context_processor
    def _inject_csrf_token():
        # The value is resolved here, not handed to Jinja as a callable:
        # Jinja renders a callable as its repr, it does not call it.
        return {"csrf_token": issue_token()}


def _apply_migrations():
    """
    Additive-only schema migrations. Each migration is a single SQL DDL
    wrapped in a try/except so that re-runs are idempotent. New columns
    added after v1 are appended here; never edit or remove a migration.
    """
    from sqlalchemy import text
    migrations = [
        # v2: capture original form inputs alongside calculated payload
        "ALTER TABLE batch_logs ADD COLUMN form_data TEXT",
    ]
    for ddl in migrations:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except Exception:
            # Column already exists, or table missing (create_all will
            # handle the missing-table case next startup). Either way,
            # this migration is a no-op and we move on.
            db.session.rollback()


def _install_immutability_triggers():
    """
    Enforce the append-only contract at the DATABASE level.

    The ORM events in _block_batch_update / _block_batch_delete only fire
    for instance-level operations: a bulk ``Query.delete()`` or a raw
    ``UPDATE`` statement issued against the engine bypasses them entirely.
    SQLite triggers close that gap, so history cannot be rewritten by any
    code path in this process or any other tool attached to the file.
    """
    from sqlalchemy import text
    triggers = [
        """
        CREATE TRIGGER IF NOT EXISTS batch_logs_block_update
        BEFORE UPDATE ON batch_logs
        BEGIN
            SELECT RAISE(ABORT, 'batch_logs rows are immutable under GMP:
            create a new record instead of updating an existing one.');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS batch_logs_block_delete
        BEFORE DELETE ON batch_logs
        BEGIN
            SELECT RAISE(ABORT, 'batch_logs rows are immutable under GMP:
            deletion of ledger records is not permitted.');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS config_changes_block_update
        BEFORE UPDATE ON config_changes
        BEGIN
            SELECT RAISE(ABORT, 'config_changes rows are immutable under GMP:
            create a new record instead of updating an existing one.');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS config_changes_block_delete
        BEFORE DELETE ON config_changes
        BEGIN
            SELECT RAISE(ABORT, 'config_changes rows are immutable under GMP:
            deletion of audit records is not permitted.');
        END
        """,
    ]
    for ddl in triggers:
        try:
            db.session.execute(text(ddl))
            db.session.commit()
        except Exception:
            db.session.rollback()  # trigger already present


# ---------------------------------------------------------------------------
# Roles (V4)
# ---------------------------------------------------------------------------
# Stored role values. Only "administrator" (and the legacy "admin")
# grants admin access (Settings, user management, history, reprint);
# every other role uses the calculators and can change its own
# password. The legacy values "admin"/"technician" are still accepted
# for existing databases and are treated as administrator / user.
ROLES = (
    ("facility_manager", "Facility Manager"),
    ("cultivation_manager", "Cultivation Manager"),
    ("cultivation_operator", "Cultivation Operator"),
    ("production_manager", "Production Manager"),
    ("production_operator", "Production Operator"),
    ("quality_manager", "Quality Manager"),
    ("user", "User"),
    ("administrator", "Administrator"),
)
ROLE_VALUES = tuple(value for value, _label in ROLES)
ROLE_LABELS = dict(ROLES)
LEGACY_ROLES = ("admin", "technician")
ADMIN_ROLES = ("administrator", "admin")


def role_label(role):
    """Human label for a stored role value (legacy roles included)."""
    if role in ROLE_LABELS:
        return ROLE_LABELS[role]
    return {"admin": "Administrator", "technician": "User"}.get(role, role)


def is_valid_role(role):
    return role in ROLE_VALUES or role in LEGACY_ROLES


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_admin(self):
        return self.role in ADMIN_ROLES


class BatchLog(db.Model):
    __tablename__ = "batch_logs"

    id = db.Column(db.Integer, primary_key=True)
    doc_number = db.Column(db.String(40), unique=True, nullable=False, index=True)
    batch_type = db.Column(db.String(20), nullable=False, index=True)
    timestamp = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    strain = db.Column(db.String(120))
    lot_number = db.Column(db.String(120))
    payload_data = db.Column(db.Text, nullable=False)
    # Raw form inputs that produced the payload. Nullable for legacy rows
    # written before this column existed - the UI shows a graceful fallback.
    form_data = db.Column(db.Text, nullable=True)

    user = db.relationship("User", backref=db.backref("batch_logs", lazy=True))

    def to_payload(self):
        return json.loads(self.payload_data) if self.payload_data else {}

    def to_form(self):
        return json.loads(self.form_data) if self.form_data else None


class ConfigChange(db.Model):
    """
    Audit record for a config.yaml edit made through the Settings GUI.

    One row per successful save: who saved it, when, the backup file
    created from the previous config, and the full saved text (so the
    exact configuration behind any batch document can be reconstructed
    later). Rows are append-only like BatchLog — there is no UI to
    edit or delete them.
    """
    __tablename__ = "config_changes"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    username = db.Column(db.String(80), nullable=False)  # snapshot, survives user deletion
    backup_file = db.Column(db.String(120), nullable=True)
    config_text = db.Column(db.Text, nullable=False)


# Enforce immutability: Block UPDATE statements on batch_logs at the engine
# level. The audit-trail contract is "append-only" - corrections require a
# new row, never an edit to an existing one.
@event.listens_for(BatchLog, "before_update", propagate=True)
def _block_batch_update(mapper, connection, target):  # pragma: no cover - safety net
    raise RuntimeError(
        "BatchLog rows are immutable; create a new record instead of updating."
    )


@event.listens_for(BatchLog, "before_delete", propagate=True)
def _block_batch_delete(mapper, connection, target):  # pragma: no cover - safety net
    raise RuntimeError(
        "BatchLog rows are immutable; deletion is not permitted under GMP."
    )


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def _seed_default_admin():
    """
    Seed the first administrator if no users exist.

    V5.0 no longer ships a well-known default password. The password is
    taken from PSYGA_ADMIN_PASSWORD when set; otherwise a random one is
    generated, written to instance/initial_admin_password.txt and printed
    to the console once, so it can be changed immediately after install.
    """
    if db.session.query(User).count() == 0:
        password = os.environ.get("PSYGA_ADMIN_PASSWORD")
        generated = False
        if not password:
            password = secrets.token_urlsafe(12)
            generated = True
        admin = User(username="admin", role="administrator")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()

        if generated and not current_app.config.get("TESTING"):
            notice = (
                "Initial administrator created.\n"
                "    username: admin\n"
                f"    password: {password}\n"
                "  Change it from Settings > Change Password, then delete "
                "instance/initial_admin_password.txt."
            )
            try:
                path = os.path.join(current_app.instance_path,
                                    "initial_admin_password.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(notice)
            except OSError:
                path = None
            print("\n" + notice + "\n", flush=True)


# ---------------------------------------------------------------------------
# Configuration loading (V2.0: validated, config-driven options)
# ---------------------------------------------------------------------------
# config.yaml is the single source of truth for every calculation
# option. It is re-read on every request, so an admin change takes
# effect on the next page load — no restart, no code change.
#
# Backward compatibility: sections that existed in V1 keep working if
# a V2.0-only section is absent (sensible defaults are used), so a
# V1 config.yaml can be dropped in without modification.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "config.yaml")

# Defaults matching the V1 hardcoded values.
_DEFAULT_SOLVENT_TYPES = ["100%", "90%", "75%"]
_DEFAULT_DOSAGE_MODES = [
    {"id": "psilocybin_only", "label": "Psilocybin only"},
    {"id": "tpe", "label": "TPE (psilocybin + 1.39 x psilocin)"},
]
_DEFAULT_CAPSULE_SPECS = [
    {"type": "0", "dose_mg": 10, "label": "Type 0 / 10mg dose"},
    {"type": "1", "dose_mg": 5, "label": "Type 1 / 5mg dose"},
    {"type": "2", "dose_mg": 2, "label": "Type 2 / 2mg dose"},
]


def validate_config(config):
    """
    Check the loaded config has every option the engines need.

    Raises ValueError with a human-readable message naming the exact
    section/key to fix (e.g. "containers.2liter_box.spawn must be a
    positive number"). Called by load_config() on every request, so a
    broken config.yaml surfaces as a clear message on the calculator
    page instead of a KeyError stack trace.
    """
    if not isinstance(config, dict):
        raise ValueError("config.yaml: top level must be a mapping")

    containers = config.get("containers")
    if not isinstance(containers, dict) or not containers:
        raise ValueError(
            "config.yaml: 'containers' must be a non-empty mapping "
            "(at least one container option is required)"
        )
    for name, spec in containers.items():
        if not isinstance(spec, dict):
            raise ValueError(f"config.yaml: containers.{name} must be a mapping")
        for key in ("substrate", "spawn", "liquid_culture"):
            val = spec.get(key)
            if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
                raise ValueError(
                    f"config.yaml: containers.{name}.{key} must be a "
                    f"positive number (got {val!r})"
                )

    # Extraction options (with V1 defaults when the section is absent).
    solvents = _allowed_solvent_types(config)
    if not solvents:
        raise ValueError(
            "config.yaml: extraction_parameters.allowed_solvent_types "
            "must be a non-empty list (e.g. [\"100%\", \"90%\"])"
        )
    for entry in solvents:
        try:
            pct = float(str(entry).rstrip("%"))
        except (TypeError, ValueError):
            raise ValueError(
                f"config.yaml: invalid solvent type {entry!r} "
                "(expected a percentage like \"80%\")"
            )
        if not (0 < pct <= 100):
            raise ValueError(
                f"config.yaml: solvent type {entry!r} out of range "
                "(must be > 0% and <= 100%)"
            )

    # V5.0 extraction volumes: the per-bottle solvent protocol is derived
    # from these, so they must be present and sane.
    extraction_params = config.get("extraction_parameters") or {}
    for key in ("grams_per_bottle", "solvent_volume_per_bottle_ml",
                "concentration_factor"):
        val = extraction_params.get(key)
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val <= 0:
            raise ValueError(
                f"config.yaml: extraction_parameters.{key} must be a "
                f"positive number (got {val!r})"
            )
    # The stock bottle must hold at least the working volume, otherwise
    # the "take MeOH out, put water back" rule cannot produce it.
    stock_volume = extraction_params.get("stock_bottle_volume_ml", 2500)
    if not isinstance(stock_volume, (int, float)) or isinstance(stock_volume, bool) \
            or stock_volume <= 0:
        raise ValueError(
            "config.yaml: extraction_parameters.stock_bottle_volume_ml must "
            f"be a positive number (got {stock_volume!r})"
        )
    if stock_volume < extraction_params["solvent_volume_per_bottle_ml"]:
        raise ValueError(
            "config.yaml: extraction_parameters.stock_bottle_volume_ml "
            f"({stock_volume}) must be at least "
            "solvent_volume_per_bottle_ml "
            f"({extraction_params['solvent_volume_per_bottle_ml']})"
        )

    # Capsulation options.
    cap = _capsulation_params(config)
    if not cap["dosage_modes"]:
        raise ValueError(
            "config.yaml: capsulation_parameters.dosage_modes must be "
            "a non-empty list"
        )
    if not cap["capsule_specs"]:
        raise ValueError(
            "config.yaml: capsulation_parameters.capsule_specs must be "
            "a non-empty list"
        )
    for spec in cap["capsule_specs"]:
        dose = spec.get("dose_mg")
        if not isinstance(dose, (int, float)) or isinstance(dose, bool) or dose <= 0:
            raise ValueError(
                "config.yaml: each capsulation_parameters.capsule_specs "
                f"entry needs a positive 'dose_mg' (got {spec!r})"
            )
    return config


def _allowed_solvent_types(config):
    """Solvent strengths offered in the Extraction dropdown (config-driven)."""
    params = config.get("extraction_parameters") or {}
    types = params.get("allowed_solvent_types") or _DEFAULT_SOLVENT_TYPES
    return [str(t) for t in types]


def _capsulation_params(config):
    """
    Normalized capsulation options with V1 defaults for any missing
    section, so a V1 config.yaml keeps working unchanged in V2.0.
    """
    cap = config.get("capsulation_parameters") or {}
    dosage_modes = []
    for mode in cap.get("dosage_modes") or _DEFAULT_DOSAGE_MODES:
        if isinstance(mode, dict):
            dosage_modes.append({
                "id": str(mode.get("id", "")),
                "label": str(mode.get("label", mode.get("id", ""))),
            })
        else:  # plain string form: "- psilocybin_only"
            dosage_modes.append({"id": str(mode), "label": str(mode)})
    capsule_specs = []
    for spec in cap.get("capsule_specs") or _DEFAULT_CAPSULE_SPECS:
        if isinstance(spec, dict):
            capsule_specs.append({
                "type": str(spec.get("type", "")),
                "dose_mg": spec.get("dose_mg"),
                "label": str(spec.get("label", "")),
            })
    return {
        "dosage_modes": dosage_modes,
        "capsule_specs": capsule_specs,
        "default_anti_caking_pct": float(
            cap.get("default_anti_caking_pct", 5.0)
        ),
        "mw_psilocybin": float(cap.get("mw_psilocybin", 284.25)),
        "mw_psilocin": float(cap.get("mw_psilocin", 204.27)),
    }


def load_config():
    # encoding="utf-8" is explicit: the Windows locale default (cp1252)
    # would corrupt non-ASCII characters (e.g. em dashes in comments).
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return validate_config(config)


def read_config_text():
    """Raw config.yaml text for the Settings editor ('' if missing)."""
    if not os.path.exists(_CONFIG_PATH):
        return ""
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _save_config_text(text, username, user_id):
    """
    Validate and atomically save a new config.yaml from the GUI.

    Steps: YAML-parse -> validate_config() -> back up the current file
    into config_backups/config-<timestamp>.bak -> atomic replace ->
    append a ConfigChange audit row. Raises ValueError (leaving the
    current file untouched) for empty/invalid YAML or invalid values.
    Returns the backup file name ('' if there was no previous file).
    """
    if not text or not text.strip():
        raise ValueError("Config is empty — nothing was saved.")
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}")
    # validate_config raises ValueError naming the exact broken key.
    validate_config(parsed)

    config_dir = os.path.dirname(os.path.abspath(_CONFIG_PATH))
    backup_name = ""
    if os.path.exists(_CONFIG_PATH):
        backup_dir = os.path.join(config_dir, "config_backups")
        os.makedirs(backup_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup_name = f"config-{stamp}.bak"
        with open(_CONFIG_PATH, "r", encoding="utf-8") as src, open(
            os.path.join(backup_dir, backup_name), "w", encoding="utf-8"
        ) as dst:
            dst.write(src.read())

    # Atomic replace: write a temp file in the same directory, then
    # rename over the old one, so a crash cannot leave a half-written
    # config behind.
    fd, tmp_path = tempfile.mkstemp(
        dir=config_dir, prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp_path, _CONFIG_PATH)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    db.session.add(
        ConfigChange(
            user_id=user_id,
            username=username,
            backup_file=backup_name or None,
            config_text=text,
        )
    )
    db.session.commit()
    return backup_name


# ---------------------------------------------------------------------------
# Sequence number generator
# ---------------------------------------------------------------------------
def generate_sequence_number(prefix):
    """
    Thread-safe, daily-incrementing sequence number.

    Format: PREFIX-YYYYMMDD-NNN (e.g. WP-20260608-001).
    Filters BatchLog rows by current UTC day + matching batch_type prefix
    (WP* vs EX*).
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    today_prefix = f"{prefix}-{today}-"

    with _sequence_lock:
        count = (
            db.session.query(BatchLog)
            .filter(BatchLog.doc_number.like(f"{today_prefix}%"))
            .count()
        )
        return f"{today_prefix}{count + 1:03d}"


def sop_doc_number(prefix, wp_doc_number):
    """
    SOP batch number for a raw-material form, linked to its WP master
    document. Format per SOP forms FORM-CULT-003/004/009:
    PREFIX-YYYY-NNN, where YYYY and NNN come from the WP doc number
    (WP-YYYYMMDD-NNN) so every form for one production run shares the
    same sequence ordinal and traces back to the master batch.
    Example: WP-20261109-003 -> SP-2026-003, SUB-2026-003, MED-2026-003.
    """
    year = wp_doc_number[3:7]
    seq = wp_doc_number[-3:]
    return f"{prefix}-{year}-{seq}"


# ---------------------------------------------------------------------------
# Pure calculation engines (DB-free, unit-testable)
# ---------------------------------------------------------------------------
def calculate_cultivation(config, form):
    """
    Cultivation calculator. Returns a dict of computed values or raises
    ValueError on invalid input.

    Two modes (V3.0):
      forward (default): target dry weight drives the container count,
        with the contamination buffer applied.
      reverse: optional spawn_bags_colonized + spawn_bag_size_g filled —
        colonized spawn on hand is the limiting resource. Containers =
        floor(total spawn / spawn per container); the contamination
        buffer and target dry weight do not apply; grain prep and
        liquid culture are not required (spawn already exists).
    """
    strain = form.get("strain", "").strip()
    cult_lot = form.get("cult_lot", "").strip()
    container_type = form.get("container_type", "").strip()

    # Optional numeric inputs: blank means "not provided".
    def _opt_float(key):
        raw = form.get(key, "")
        raw = "" if raw is None else str(raw).strip()
        return float(raw) if raw else None

    avg_yield_fresh = _opt_float("avg_yield_fresh")
    target_dry_weight = _opt_float("target_dry_weight")

    # ---- Reverse-mode inputs (both blank = forward mode) -----------------
    bags_raw = str(form.get("spawn_bags_colonized", "") or "").strip()
    size_raw = str(form.get("spawn_bag_size_g", "") or "").strip()
    if bool(bags_raw) != bool(size_raw):
        raise ValueError(
            "spawn_bags_colonized and spawn_bag_size_g must be filled "
            "together (or both left blank for the standard calculation)"
        )
    reverse_mode = bool(bags_raw)
    spawn_bags_colonized = None
    spawn_bag_size_g = None
    if reverse_mode:
        try:
            spawn_bags_colonized = int(bags_raw)
        except ValueError:
            raise ValueError(
                "spawn_bags_colonized must be a whole number of bags"
            )
        spawn_bag_size_g = _opt_float("spawn_bag_size_g")
        if spawn_bags_colonized <= 0:
            raise ValueError("spawn_bags_colonized must be positive")
        if spawn_bag_size_g is None or spawn_bag_size_g <= 0:
            raise ValueError("spawn_bag_size_g must be positive")

    if container_type not in config["containers"]:
        raise ValueError(f"Unknown container_type: {container_type}")
    if not reverse_mode:
        # Forward mode needs both drivers; reverse mode treats them as
        # optional (avg yield only feeds the expected-yield estimate).
        if avg_yield_fresh is None or avg_yield_fresh <= 0:
            raise ValueError("avg_yield_fresh must be positive")
        if target_dry_weight is None or target_dry_weight <= 0:
            raise ValueError("target_dry_weight must be positive")
    elif avg_yield_fresh is not None and avg_yield_fresh < 0:
        raise ValueError("avg_yield_fresh must not be negative")

    container = config["containers"][container_type]
    substrate_per_container = container["substrate"]
    spawn_per_container = container["spawn"]
    lc_per_container = container["liquid_culture"]

    # ---- V6.0 forward-mode bag + LC inputs -------------------------------
    # The spawn bag fill weight drives how many bags have to be filled,
    # and the LC rate drives the volume each bag receives. Both default
    # to the controlled values from config.yaml.
    forward_bag_size_g = None
    forward_lc_rate_ml = None
    if not reverse_mode:
        forward_bag_size_g = _opt_float("spawn_bag_fill_weight_g")
        if forward_bag_size_g is None:
            forward_bag_size_g = spawn_per_container
        elif forward_bag_size_g <= 0:
            raise ValueError("spawn_bag_fill_weight_g must be positive")

        forward_lc_rate_ml = _opt_float("lc_rate_ml")
        if forward_lc_rate_ml is not None and forward_lc_rate_ml < 0:
            raise ValueError("lc_rate_ml must not be negative")

    dry_yield_per_container = (
        (avg_yield_fresh or 0) * config["dry_matter_ratio"]
    )

    # ---- Container count -------------------------------------------------
    total_spawn_on_hand_g = None
    leftover_spawn_g = None
    if reverse_mode:
        total_spawn_on_hand_g = spawn_bags_colonized * spawn_bag_size_g
        total_containers = math.floor(
            total_spawn_on_hand_g / spawn_per_container
        )
        if total_containers < 1:
            raise ValueError(
                f"Insufficient colonized spawn for even one "
                f"{container_type} container: need at least "
                f"{spawn_per_container}g, have {total_spawn_on_hand_g:g}g "
                f"({spawn_bags_colonized} bags x {spawn_bag_size_g:g}g)"
            )
        leftover_spawn_g = (
            total_spawn_on_hand_g - total_containers * spawn_per_container
        )
        base_containers = total_containers  # spawn-limited: no buffer
        calculation_mode = "reverse"
    else:
        base_containers = target_dry_weight / dry_yield_per_container
        total_containers = math.ceil(
            base_containers * (1 + config["contamination_buffer"])
        )
        calculation_mode = "forward"

    total_substrate = total_containers * substrate_per_container
    # Per-bucket net substrate weight = sum of all ingredients in
    # config["substrate_recipe"]. Single source of truth in config.yaml.
    bucket_yield_g = sum(config["substrate_recipe"].values())
    substrate_bucket_count = math.ceil(total_substrate / bucket_yield_g)

    substrate_recipe = config["substrate_recipe"]
    total_coir = substrate_bucket_count * substrate_recipe["coco_coir"]
    total_vermiculite = substrate_bucket_count * substrate_recipe["vermiculite"]
    total_gypsum = substrate_bucket_count * substrate_recipe["gypsum"]
    total_substrate_water = substrate_bucket_count * substrate_recipe["water"]

    total_spawn = total_containers * spawn_per_container

    # V6.0: liquid culture is dosed by spawn MASS, not by container count.
    # The rates live in config.yaml and can be overridden per batch from
    # the cultivation form.
    media_params = config.get("media_parameters", {}) or {}
    lc_rate_ml = float(media_params.get("lc_rate_ml", 5))
    if forward_lc_rate_ml is not None:
        lc_rate_ml = forward_lc_rate_ml
    lc_rate_basis_g = float(media_params.get("lc_rate_basis_g", 200))
    lc_excess_pct = float(media_params.get("lc_excess_pct", 20))
    # V6.0: the backup over the calculated LC is never less than 20 %.
    lc_excess_pct = max(20.0, lc_excess_pct)

    if reverse_mode:
        # Spawn already colonized: no new grain or liquid culture, and
        # no bags to inoculate - FORM-CULT-003A does not apply.
        grain_batch_count = 0
        total_grain = 0
        total_grain_water = 0
        total_bags = spawn_bags_colonized
        bag_size_spawn = spawn_bag_size_g
        total_lc = 0
        lc_needed_ml = 0
        lc_prepare_ml = 0
        spawn_bags = []
    else:
        # Grain batch size is config-driven (V1 hardcoded 10000 g).
        grain_batch_size_g = float(config.get("grain_batch_size_g", 10000))
        grain_batch_count = math.ceil(total_spawn / grain_batch_size_g)
        grain_recipe = config["grain_batch"]
        total_grain = grain_batch_count * grain_recipe["grain"]
        total_grain_water = grain_batch_count * grain_recipe["water"]

        # V6.0: the spawn bag fill weight is an operator input (it
        # defaults to the container's own spawn weight) and it drives how
        # many bags have to be filled.
        bag_size_spawn = forward_bag_size_g
        spawn_bags = []
        remaining = total_spawn
        index = 1
        while remaining > 0:
            weight = min(bag_size_spawn, remaining)
            spawn_bags.append({
                "index": index,
                "weight_g": int(round(weight)),
                "is_partial": weight < bag_size_spawn - 1e-9,
            })
            remaining -= weight
            index += 1
        total_bags = len(spawn_bags)

        # LC per bag from the spawn weight, then the lot total.
        for bag in spawn_bags:
            bag["lc_ml"] = round(
                bag["weight_g"] / lc_rate_basis_g * lc_rate_ml, 2
            )
        lc_needed_ml = round(sum(b["lc_ml"] for b in spawn_bags), 2)
        lc_prepare_ml = math.ceil(lc_needed_ml * (1 + lc_excess_pct / 100))
        total_lc = lc_needed_ml

    # V4/V6: per-grain-batch breakdown for the spawn form. Each batch is
    # grain_batch_size_g of spawn (10 kg); the last batch may be partial.
    # V6.0: bags are attributed to the batch the spawn was drawn from, so
    # the per-batch bag counts always add up to the lot bag count (V4
    # rounded each batch up independently and over-filled the last one).
    grain_batches = []
    if not reverse_mode and grain_batch_count:
        bag_cursor = 0
        remaining = total_spawn
        for i in range(1, grain_batch_count + 1):
            batch_spawn = min(grain_batch_size_g, remaining)
            # Bags that start inside this batch belong to it.
            bags_in_batch = 0
            filled_in_batch = 0
            while bag_cursor < len(spawn_bags) and filled_in_batch < batch_spawn:
                bags_in_batch += 1
                filled_in_batch += spawn_bags[bag_cursor]["weight_g"]
                bag_cursor += 1
            grain_batches.append({
                "index": i,
                "grain_g": grain_recipe["grain"],
                "water_ml": grain_recipe["water"],
                "spawn_g": int(batch_spawn),
                "bags": bags_in_batch,
                "bag_weight_g": int(bag_size_spawn),
            })
            remaining -= batch_spawn

    # V4: liquid media is prepared with an excess over the LC needed
    # for inoculation (media bottles in excess, default +30%).
    media_excess_pct = float(media_params.get("excess_pct", 30))
    media_prep_volume_ml = (
        math.ceil(lc_prepare_ml * (1 + media_excess_pct / 100))
        if lc_prepare_ml else 0
    )

    expected_dry_weight_g = round(
        dry_yield_per_container * total_containers, 2
    )

    return {
        "strain": strain,
        "cult_lot": cult_lot,
        "container_type": container_type,
        "avg_yield_fresh": avg_yield_fresh or 0,
        "target_dry_weight": target_dry_weight or 0,
        "calculation_mode": calculation_mode,
        "spawn_bags_colonized": spawn_bags_colonized,
        "spawn_bag_size_g": spawn_bag_size_g,
        "total_spawn_on_hand_g": total_spawn_on_hand_g,
        "leftover_spawn_g": leftover_spawn_g,
        "expected_dry_weight_g": expected_dry_weight_g,
        "dry_yield_per_container": round(dry_yield_per_container, 2),
        "base_containers": math.ceil(base_containers),
        "contamination_buffer_pct": int(config["contamination_buffer"] * 100),
        "total_containers": total_containers,
        "total_substrate": total_substrate,
        "substrate_bucket_count": substrate_bucket_count,
        "total_coir": total_coir,
        "total_vermiculite": total_vermiculite,
        "total_gypsum": total_gypsum,
        "total_substrate_water": total_substrate_water,
        "total_spawn": total_spawn,
        "grain_batch_count": grain_batch_count,
        "total_grain": total_grain,
        "total_grain_water": total_grain_water,
        "total_bags": total_bags,
        "bag_size_spawn": bag_size_spawn,
        "total_lc": total_lc,
        "lc_needed_ml": lc_needed_ml,
        "lc_prepare_ml": lc_prepare_ml,
        "lc_rate_ml": lc_rate_ml,
        "lc_rate_basis_g": lc_rate_basis_g,
        "lc_excess_pct": lc_excess_pct,
        "lc_per_container_nominal": lc_per_container,
        "spawn_bags": spawn_bags,
        "grain_batches": grain_batches,
        "media_excess_pct": media_excess_pct,
        "media_prep_volume_ml": media_prep_volume_ml,
    }


def calculate_extraction_plan(config, form):
    """
    V5.0 portion-based extraction planner.

    The V4 planner worked in storage packs: the operator declared a pack
    weight (50 g), the app counted packs and then re-cut them into bottles.
    That indirection is gone. The unit of production is now the **portion**:
    one whole 120 g portion of dried mushroom per 2.5 L extraction bottle.

    Models the workflow:
        1. The production lot is split into whole portions of
           `grams_per_bottle` (120 g). A partial portion is never
           extracted (GMP: no partial-bottle extraction) - the remainder is
           documented and held.
        2. Each extraction day loads `daily_bottle_count` bottles. Every
           round is 2 calendar days: an extraction day (grind, load,
           solvent, soak) and a rotary-evaporation day.
        3. Each bottle is prepared with the same per-bottle solvent
           protocol, so the document states the exact volumes for one
           bottle and the lot totals are simply that value x bottle count.
        4. Yield prediction defaults to a 4x concentration factor; a
           supervisor override replaces it for lots with historical data.

    Solvent preparation (per bottle):
        meoh_to_take_out = stock_bottle_volume_ml
                           - working_volume_per_bottle x (pct/100)
        water_to_put_back = working_volume_per_bottle x (1 - pct/100)

    The bench stock is a 2.5 L bottle of 100% MeOH; the 2.4 L working
    volume is prepared from it by taking MeOH out and putting water back
    in. With the controlled defaults (2500 mL stock, 2400 mL working
    volume) and 90% MeOH this is 340 mL of MeOH taken out and 240 mL of
    water put back, giving 2400 mL at 90%. At 75% it is 700 mL out and
    600 mL back.

    One extraction bottle is charged with 120 g of dried mushroom plus
    2400 mL of that working volume - a mushroom : solvent ratio of 1:20.

    Example (the reference case): 400 g total, 2 bottles per day ->
        total bottles      3   (3 x 120 g = 360 g processed)
        extraction day 1   2 bottles = 240 g
        extraction day 2   1 bottle  = 120 g
        leftover          40 g (held, not extracted)
    """
    params = config["extraction_parameters"]
    grams_per_bottle = float(params["grams_per_bottle"])
    working_volume_ml = float(params["solvent_volume_per_bottle_ml"])
    stock_bottle_volume_ml = float(params.get("stock_bottle_volume_ml", 2500))
    default_yield_factor = float(params["concentration_factor"])

    # ---- Required inputs -------------------------------------------------
    production_lot_number = form.get("production_lot_number", "").strip()
    strain = form.get("strain", "").strip()
    solvent_type = form.get("solvent_type", "").strip()
    dried_weight_total = float(form["dried_weight_total_g"])
    daily_bottle_count = int(form["daily_bottle_count"])
    grinder_loss_per_round = float(form.get("grinder_loss_per_round_g", 0) or 0)

    if solvent_type not in _allowed_solvent_types(config):
        raise ValueError(
            f"Unsupported solvent_type: {solvent_type!r} "
            f"(allowed: {', '.join(_allowed_solvent_types(config))} — "
            "edit allowed_solvent_types in config.yaml to add one)"
        )
    if daily_bottle_count <= 0:
        raise ValueError("daily_bottle_count must be positive")
    if grams_per_bottle <= 0:
        raise ValueError("grams_per_bottle must be positive")
    if dried_weight_total <= 0:
        raise ValueError("dried_weight_total_g must be positive")

    # ---- Yield factor: override or default -------------------------------
    override_raw = form.get("yield_override_factor", "").strip()
    if override_raw:
        try:
            yield_factor = float(override_raw)
        except ValueError:
            raise ValueError(f"Invalid yield_override_factor: {override_raw!r}")
        if yield_factor <= 0:
            raise ValueError("yield_override_factor must be positive")
    else:
        yield_factor = default_yield_factor

    # ---- Portions (whole 120 g units only) --------------------------------
    total_bottles = int(math.floor(dried_weight_total / grams_per_bottle))
    if total_bottles < 1:
        raise ValueError(
            f"Insufficient material: at least {grams_per_bottle:g}g is required "
            f"to fill one bottle (have {dried_weight_total:g}g)."
        )
    processed_weight_g = total_bottles * grams_per_bottle
    leftover_weight_g = round(dried_weight_total - processed_weight_g, 3)

    # ---- Rounds: one extraction day + one evaporation day each ------------
    total_rounds = math.ceil(total_bottles / daily_bottle_count)
    total_procedure_days = total_rounds * 2

    # ---- Per-bottle solvent preparation --------------------------------------
    # The bench stock is a 2.5 L bottle of 100% MeOH. The working volume
    # charged into an extraction bottle (2.4 L) is prepared from it by
    # taking MeOH out of the stock bottle and putting the matching volume
    # of water back in:
    #
    #     MeOH to take out = stock_volume - working_volume * (pct/100)
    #     water to put back = working_volume * (1 - pct/100)
    #
    # 90% MeOH: take out 340 mL, put back 240 mL -> 2400 mL at 90%.
    # 75% MeOH: take out 700 mL, put back 600 mL -> 2400 mL at 75%.
    pct = float(solvent_type.rstrip("%"))
    meoh_remove_per_bottle = (
        stock_bottle_volume_ml - (working_volume_ml * (pct / 100.0))
    )
    water_add_per_bottle = working_volume_ml * (1.0 - (pct / 100.0))

    # Mushroom : solvent ratio charged into the extraction bottle.
    mushroom_to_solvent_ratio = working_volume_ml / grams_per_bottle

    # The single source of truth for the per-bottle solvent preparation.
    # The document prints exactly these numbers - no second copy of the
    # rule, no step list to fall out of sync with them.
    per_bottle = {
        "mushroom_g": grams_per_bottle,
        "stock_solvent_ml": working_volume_ml,
        "stock_bottle_volume_ml": stock_bottle_volume_ml,
        "solvent_strength_pct": pct,
        "meoh_remove_ml": round(meoh_remove_per_bottle, 1),
        "water_add_ml": round(water_add_per_bottle, 1),
        # The prepared working volume that is charged into the bottle.
        "final_liquid_ml": round(working_volume_ml, 1),
        "final_working_volume_ml": round(working_volume_ml, 1),
        # Mushroom : solvent ratio (grams per mL of working volume).
        "ratio_label": f"1:{mushroom_to_solvent_ratio:g}",
    }

    # ---- Calendar timeline ------------------------------------------------
    # One row per calendar day: an extraction day, then the rotary
    # evaporation day that closes the round.
    timeline = []
    remaining = total_bottles
    calendar_day = 1
    round_index = 1
    while remaining > 0:
        bottles_this_round = min(daily_bottle_count, remaining)
        mushroom_this_round = bottles_this_round * grams_per_bottle
        timeline.append({
            "calendar_day": calendar_day,
            "round": round_index,
            "phase": "Extraction",
            "phase_detail": (
                f"Grind and load {bottles_this_round} bottle(s) "
                f"({mushroom_this_round:g} g), add solvent, soak."
            ),
            "bottles": bottles_this_round,
            "mushroom_g": mushroom_this_round,
            "solvent_ml": bottles_this_round * working_volume_ml,
            "meoh_remove_ml": round(bottles_this_round * meoh_remove_per_bottle, 1),
            "water_add_ml": round(bottles_this_round * water_add_per_bottle, 1),
            "per_bottle_meoh_remove_ml": round(meoh_remove_per_bottle, 1),
            "per_bottle_water_add_ml": round(water_add_per_bottle, 1),
            "grinder_loss_budget_g": grinder_loss_per_round,
            "expected_extract_g": round(mushroom_this_round / yield_factor, 2),
        })
        calendar_day += 1
        timeline.append({
            "calendar_day": calendar_day,
            "round": round_index,
            "phase": "Evaporation",
            "phase_detail": (
                "Rotary-evaporate the extract from the bottles loaded on "
                f"calendar day {calendar_day - 1}. No materials consumed."
            ),
            "bottles": 0,
            "mushroom_g": 0,
            "solvent_ml": 0,
            "meoh_remove_ml": 0.0,
            "water_add_ml": 0.0,
            "per_bottle_meoh_remove_ml": round(meoh_remove_per_bottle, 1),
            "per_bottle_water_add_ml": round(water_add_per_bottle, 1),
            "grinder_loss_budget_g": 0,
            "expected_extract_g": 0.0,
        })
        calendar_day += 1
        remaining -= bottles_this_round
        round_index += 1

    # ---- Lot totals -------------------------------------------------------
    total_solvent_ml = total_bottles * working_volume_ml
    stock_bottles_required = math.ceil(total_solvent_ml / stock_bottle_volume_ml)
    total_meoh_remove_ml = round(total_bottles * meoh_remove_per_bottle, 1)
    total_water_add_ml = round(total_bottles * water_add_per_bottle, 1)
    total_expected_extract_g = round(processed_weight_g / yield_factor, 2)
    total_grinder_loss = grinder_loss_per_round * total_rounds

    totals = {
        "bottles": total_bottles,
        "portions": total_bottles,
        "mushroom_processed_g": processed_weight_g,
        "leftover_g": leftover_weight_g,
        "stock_solvent_ml": total_solvent_ml,
        "stock_bottles_required": stock_bottles_required,
        "meoh_remove_ml": total_meoh_remove_ml,
        "water_add_ml": total_water_add_ml,
        "expected_extract_g": total_expected_extract_g,
        "grinder_loss_budget_g": total_grinder_loss,
        "extraction_days": total_rounds,
        "total_procedure_days": total_procedure_days,
    }

    return {
        "schema_version": CALCULATOR_VERSION,
        "production_lot_number": production_lot_number,
        "strain": strain,
        "solvent_type": solvent_type,
        "dried_weight_total_g": dried_weight_total,
        "daily_bottle_count": daily_bottle_count,
        "grams_per_bottle": grams_per_bottle,
        "portion_size_g": grams_per_bottle,
        "total_bottles": total_bottles,
        "processed_weight_g": processed_weight_g,
        "leftover_weight_g": leftover_weight_g,
        "total_rounds": total_rounds,
        "extraction_days": total_rounds,
        "total_procedure_days": total_procedure_days,
        "per_bottle": per_bottle,
        "totals": totals,
        "timeline": timeline,
        # Flat aliases kept so the ledger payload stays easy to query and
        # so older tooling that reads these names keeps working.
        "total_solvent_ml": total_solvent_ml,
        "stock_bottles_required": stock_bottles_required,
        "total_meoh_remove_ml": total_meoh_remove_ml,
        "total_water_add_ml": total_water_add_ml,
        "total_expected_extract_g": total_expected_extract_g,
        "total_grinder_loss_budget_g": total_grinder_loss,
        "grinder_loss_per_round_g": grinder_loss_per_round,
        "yield_factor": yield_factor,
        "yield_override_used": bool(override_raw),
        "daily_checklist": timeline,
    }


def calculate_capsulation(config, form):
    """
    Capsulation calculator with two dosage modes:

      mode = "psilocybin_only":
        active_mass = extract_g * psilocybin_pct / 100
      mode = "tpe":
        TPE_pct = psilocybin_pct + (psilocin_pct * 1.39)
        active_mass = extract_g * TPE_pct / 100
        (1.39 = MW_psilocybin / MW_psilocin = 284.25 / 204.27)

    Anti-caking agent is applied to the BLEND at `anti_caking_pct`
    (e.g. 5% of final blend), per pharmaceutical excipient convention:
        anti_caking_g = extract_g * pct / (100 - pct)
        blend_g = extract_g + anti_caking_g
    The doc also shows the inverse (anti-caking as % of extract) so
    operators can sanity-check the math.
    """
    # All capsulation options are config-driven (V1 hardcoded them).
    cap_params = _capsulation_params(config)
    dosage_mode_ids = [m["id"] for m in cap_params["dosage_modes"]]
    production_lot_number = form.get("production_lot_number", "").strip()
    grams_of_extract = float(form["grams_of_extract"])
    psilocybin_pct = float(form["psilocybin_pct"])
    psilocin_pct = float(form["psilocin_pct"])
    anti_raw = form.get("anti_caking_pct", "")
    anti_caking_pct = (
        float(anti_raw) if str(anti_raw).strip() != ""
        else cap_params["default_anti_caking_pct"]
    )
    anti_caking_agent = form.get("anti_caking_agent_name", "").strip()
    # Default to the first configured dosage mode when the form is blank.
    dosage_mode = form.get("dosage_mode") or (
        dosage_mode_ids[0] if dosage_mode_ids else "psilocybin_only"
    )
    source_ex_doc_numbers = form.get(
        "source_ex_doc_numbers", ""
    ).strip()

    if dosage_mode not in dosage_mode_ids:
        raise ValueError(
            f"Invalid dosage_mode: {dosage_mode!r} "
            f"(allowed: {', '.join(dosage_mode_ids)} — edit "
            "capsulation_parameters.dosage_modes in config.yaml to add one)"
        )
    if dosage_mode not in ("psilocybin_only", "tpe"):
        # A mode can be listed in config without an engine rule; fail
        # loudly here instead of silently using the wrong active mass.
        raise ValueError(
            f"dosage_mode {dosage_mode!r} is listed in config.yaml but "
            "has no calculation rule in app.py (supported: "
            "psilocybin_only, tpe)"
        )
    if grams_of_extract <= 0:
        raise ValueError("grams_of_extract must be positive")
    if psilocybin_pct < 0 or psilocin_pct < 0:
        raise ValueError("active-ingredient percentages cannot be negative")
    if not (0 <= anti_caking_pct < 100):
        raise ValueError("anti_caking_pct must be between 0 and 100")
    if not source_ex_doc_numbers:
        # Cross-tab linking: an EX source must be recorded for traceability.
        raise ValueError(
            "At least one source EX doc number is required (cross-tab "
            "traceability)."
        )

    # Molecular weights come from config (V1 hardcoded 284.25 / 204.27).
    MW_PSILOCYBIN = cap_params["mw_psilocybin"]
    MW_PSILOCIN = cap_params["mw_psilocin"]
    MW_RATIO = round(MW_PSILOCYBIN / MW_PSILOCIN, 4)  # 1.3914

    psilocybin_mass_g = grams_of_extract * psilocybin_pct / 100.0
    psilocin_mass_g = grams_of_extract * psilocin_pct / 100.0
    tpe_pct = psilocybin_pct + (psilocin_pct * MW_RATIO)
    tpe_mass_g = grams_of_extract * tpe_pct / 100.0

    if dosage_mode == "psilocybin_only":
        active_mass_g = psilocybin_mass_g
        active_pct = psilocybin_pct
        active_label = "psilocybin"
    else:
        active_mass_g = tpe_mass_g
        active_pct = tpe_pct
        active_label = "TPE (psilocybin + 1.39 x psilocin)"

    # Anti-caking: 5% of BLEND mass means:
    #   anti_caking = blend * pct/100
    #   blend = extract + anti_caking
    # => anti_caking = extract * pct / (100 - pct)
    anti_caking_g = grams_of_extract * anti_caking_pct / (100.0 - anti_caking_pct)
    blend_g = grams_of_extract + anti_caking_g
    # Inverse: anti-caking as percent of extract (for sanity check)
    anti_caking_of_extract_pct = (
        (anti_caking_g / grams_of_extract) * 100.0 if grams_of_extract else 0
    )

    active_mg = active_mass_g * 1000.0
    blend_g_total = blend_g  # alias for clarity
    # mg active per g blend
    mg_active_per_g_blend = active_mg / blend_g_total if blend_g_total else 0

    # Capsule sizes come from config (V1 hardcoded 0/1/2). Add a block
    # in config.yaml to add a size — no code change.
    capsule_specs = [
        (spec["type"], spec["dose_mg"], spec["label"])
        for spec in cap_params["capsule_specs"]
    ]
    capsule_table = []
    for cap_type, dose_mg, label in capsule_specs:
        # mg of blend needed per capsule to deliver `dose_mg` of active.
        # dose_mg [mg] / (mg_active_per_g_blend [mg/g]) = mass [g].
        # Multiply by 1000 to express the answer in mg.
        blend_per_capsule_g = (
            dose_mg / mg_active_per_g_blend if mg_active_per_g_blend else 0
        )
        blend_per_capsule_mg = blend_per_capsule_g * 1000.0
        # Maximum capsules if 100% of the lot went to this type
        max_capsules = (
            int((blend_g_total * 1000.0) // blend_per_capsule_mg)
            if blend_per_capsule_mg else 0
        )
        capsule_table.append({
            "type": cap_type,
            "label": label,
            "target_dose_mg": dose_mg,
            "blend_per_capsule_mg": round(blend_per_capsule_mg, 2),
            "max_capsules_from_lot": max_capsules,
        })

    return {
        "production_lot_number": production_lot_number,
        "grams_of_extract": grams_of_extract,
        "psilocybin_pct": psilocybin_pct,
        "psilocin_pct": psilocin_pct,
        "mw_ratio": MW_RATIO,
        "tpe_pct": round(tpe_pct, 4),
        "tpe_mass_g": round(tpe_mass_g, 4),
        "psilocybin_mass_g": round(psilocybin_mass_g, 4),
        "psilocin_mass_g": round(psilocin_mass_g, 4),
        "dosage_mode": dosage_mode,
        "active_label": active_label,
        "active_pct": active_pct,
        "active_mass_g": round(active_mass_g, 4),
        "active_mg": round(active_mg, 2),
        "anti_caking_pct": anti_caking_pct,
        "anti_caking_agent": anti_caking_agent,
        "anti_caking_g": round(anti_caking_g, 4),
        "anti_caking_of_extract_pct": round(anti_caking_of_extract_pct, 4),
        "blend_g": round(blend_g, 4),
        "mg_active_per_g_blend": round(mg_active_per_g_blend, 4),
        "capsule_table": capsule_table,
        "source_ex_doc_numbers": source_ex_doc_numbers,
    }


def calculate_extraction(config, form):
    """
    Single-shot extraction helper kept for the EX -> CP cross-link use
    case and backward-compatibility. The main extraction UI now uses
    calculate_extraction_plan.

    V5.0: uses the same per-bottle solvent rule as the planner, so the
    two paths can never disagree.
    """
    params = config["extraction_parameters"]
    grams_per_bottle = float(params["grams_per_bottle"])
    if form.get("solvent_type") not in _allowed_solvent_types(config):
        raise ValueError(f"Unsupported solvent_type: {form.get('solvent_type')}")
    dried_weight_available = float(form["dried_weight_available"])
    if dried_weight_available < grams_per_bottle:
        raise ValueError(
            f"Minimum {grams_per_bottle}g required to fill a single bottle."
        )

    bottles_count = math.floor(dried_weight_available / grams_per_bottle)
    if bottles_count == 0:
        raise ValueError(
            f"Insufficient material: at least {grams_per_bottle}g is required."
        )
    processed_weight = bottles_count * grams_per_bottle
    unused_weight = dried_weight_available - processed_weight

    pct = float(form["solvent_type"].rstrip("%"))
    solvent_volume_ml = float(params["solvent_volume_per_bottle_ml"])
    stock_volume_ml = float(params.get("stock_bottle_volume_ml", 2500))
    # Same rule as the planner: dilute the 2.5 L 100% MeOH stock bottle
    # down to the 2.4 L working volume at the selected strength.
    single_bottle_meoh_remove = (
        stock_volume_ml - (solvent_volume_ml * (pct / 100.0))
    )
    single_bottle_water_add = solvent_volume_ml * (1.0 - (pct / 100.0))

    total_meoh_remove = single_bottle_meoh_remove * bottles_count
    total_water_add = single_bottle_water_add * bottles_count
    total_solvent_volume = solvent_volume_ml * bottles_count

    concentration_factor = params["concentration_factor"]
    expected_output_g = processed_weight / concentration_factor
    total_resuspension_water_ml = (
        bottles_count * params["resuspension_water_per_bottle_ml"]
    )

    return {
        "schema_version": CALCULATOR_VERSION,
        "strain": form.get("strain", "").strip(),
        "dry_lot_number": form.get("dry_lot_number", "").strip(),
        "solvent_type": form["solvent_type"],
        "dried_weight_available": dried_weight_available,
        "grams_per_bottle": grams_per_bottle,
        "bottles_count": bottles_count,
        "processed_weight": processed_weight,
        "unused_weight": unused_weight,
        "single_bottle_meoh_remove_ml": round(single_bottle_meoh_remove, 1),
        "single_bottle_water_add_ml": round(single_bottle_water_add, 1),
        "total_meoh_remove_ml": round(total_meoh_remove, 1),
        "total_water_add_ml": round(total_water_add, 1),
        "total_bottles_needed": bottles_count,
        "total_solvent_volume_ml": total_solvent_volume,
        "concentration_factor": concentration_factor,
        "expected_output_g": round(expected_output_g, 2),
        "total_resuspension_water_ml": total_resuspension_water_ml,
    }


# ---------------------------------------------------------------------------
# Persistence helper
# ---------------------------------------------------------------------------
def _persist_batch(batch_type, prefix, strain, lot_number, result, form_data,
                   doc_number=None):
    """
    Allocate a doc number (or use the supplied one), write the ledger
    row, return the doc number.

    `form_data` is the raw request.form (as a dict) that produced
    `result`. Captured verbatim so the audit trail can later reproduce
    the exact operator inputs - critical for GMP traceability.

    The calculated `result` is stored in full as the ledger payload, so a
    reprint reproduces the document exactly as it was issued - independent
    of any later config.yaml change. The payload also carries the
    calculator version that produced it.

    The sequence number is allocated inside the insert loop: if another
    process claimed the same number in the meantime, the UNIQUE constraint
    rejects the insert and the next candidate is tried. That closes the
    race that the in-process lock alone cannot cover.
    """
    from sqlalchemy.exc import IntegrityError

    payload = dict(result)
    payload.setdefault("schema_version", CALCULATOR_VERSION)

    attempts = 5
    last_error = None
    for _ in range(attempts):
        candidate = doc_number or generate_sequence_number(prefix)
        entry = BatchLog(
            doc_number=candidate,
            batch_type=batch_type,
            user_id=current_user.id,
            strain=strain,
            lot_number=lot_number,
            payload_data=json.dumps(payload),
            form_data=json.dumps(dict(form_data)) if form_data else None,
        )
        db.session.add(entry)
        try:
            db.session.commit()
            return candidate
        except IntegrityError as exc:
            db.session.rollback()
            last_error = exc
            if doc_number is not None:
                # The caller supplied a specific number; a conflict is a
                # real error, not a sequence race.
                raise
            continue
    raise RuntimeError(
        "Could not allocate a unique document number after "
        f"{attempts} attempts: {last_error}"
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def register_routes(app):
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("index"))
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = db.session.query(User).filter_by(username=username).first()
            if user and user.check_password(password):
                login_user(user)
                next_url = request.args.get("next") or url_for("index")
                return redirect(next_url)
            flash("Invalid username or password.", "danger")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        flash("Logged out.", "info")
        return redirect(url_for("login"))

    @app.route("/", methods=["GET", "POST"])
    @login_required
    def index():
        config = load_config()
        result = None
        doc_number = None
        container_types = list(config["containers"].keys())
        media_params = config.get("media_parameters", {}) or {}

        if request.method == "POST":
            try:
                result = calculate_cultivation(config, request.form)
                # V4.0: allocate the WP number first so the raw-material
                # SOP forms (SP/SUB/MED-YYYY-NNN) can be derived from it
                # and stored in the ledger payload.
                doc_number = generate_sequence_number("WP")
                result["spawn_doc_number"] = sop_doc_number("SP", doc_number)
                result["substrate_doc_number"] = sop_doc_number("SUB", doc_number)
                result["media_doc_number"] = sop_doc_number("MED", doc_number)
                _persist_batch(
                    batch_type="cultivation",
                    prefix="WP",
                    strain=result.get("strain"),
                    lot_number=result.get("cult_lot"),
                    result=result,
                    form_data=request.form,
                    doc_number=doc_number,
                )
                result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                result["doc_number"] = doc_number
            except (ValueError, KeyError) as exc:
                flash(f"Calculation error: {exc}", "danger")

        return render_template(
            "index.html",
            result=result,
            container_types=container_types,
            doc_number=doc_number,
            container_defaults=config["containers"],
            lc_basis_g=float(media_params.get("lc_rate_basis_g", 200)),
            lc_default_rate=float(media_params.get("lc_rate_ml", 5)),
        )

    @app.route("/extraction", methods=["GET", "POST"])
    @login_required
    def extraction():
        config = load_config()
        result = None
        doc_number = None
        error = None
        extraction_params = config["extraction_parameters"]

        if request.method == "POST":
            try:
                result = calculate_extraction_plan(config, request.form)
                doc_number = _persist_batch(
                    batch_type="extraction",
                    prefix="EX",
                    strain=result.get("strain"),
                    # V5.0: the dry-lot field is gone, so the production lot
                    # identifies the material in the master log.
                    lot_number=result.get("production_lot_number"),
                    result=result,
                    form_data=request.form,
                )
                result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                result["doc_number"] = doc_number
            except (ValueError, KeyError) as exc:
                error = str(exc)
                flash(error, "danger")

        return render_template(
            "extraction.html",
            result=result,
            error=error,
            doc_number=doc_number,
            solvent_types=_allowed_solvent_types(config),
            portion_size_g=float(extraction_params["grams_per_bottle"]),
            stock_bottle_volume_ml=float(
                extraction_params.get("stock_bottle_volume_ml", 2500)
            ),
        )

    @app.route("/capsulation", methods=["GET", "POST"])
    @login_required
    def capsulation():
        config = load_config()
        result = None
        doc_number = None
        error = None

        if request.method == "POST":
            try:
                result = calculate_capsulation(config, request.form)
                doc_number = _persist_batch(
                    batch_type="capsulation",
                    prefix="CP",
                    strain=None,
                    lot_number=result.get("production_lot_number"),
                    result=result,
                    form_data=request.form,
                )
                result["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                result["doc_number"] = doc_number
            except (ValueError, KeyError) as exc:
                error = str(exc)
                flash(error, "danger")

        return render_template(
            "capsulation.html",
            result=result,
            error=error,
            doc_number=doc_number,
            dosage_modes=_capsulation_params(config)["dosage_modes"],
        )

    @app.route("/admin/history")
    @login_required
    def admin_history():
        if not current_user.is_admin():
            abort(403)
        entries = (
            db.session.query(BatchLog)
            .order_by(BatchLog.timestamp.desc())
            .all()
        )
        return render_template("history.html", entries=entries)

    @app.route("/admin/history/view/<doc_number>")
    @login_required
    def admin_history_view(doc_number):
        if not current_user.is_admin():
            abort(403)
        entry = (
            db.session.query(BatchLog).filter_by(doc_number=doc_number).first()
        )
        if not entry:
            abort(404)
        return render_template("history_view.html", entry=entry)

    @app.route("/admin/history/reprint/<doc_number>")
    @login_required
    def admin_history_reprint(doc_number):
        """
        Re-render the original protocol document for a stored batch record.
        Read-only reproduction - this does NOT create a new ledger row; the
        doc number on the reprinted page is the original one and the
        audit-metadata footer is appended so the printout is unambiguously a
        copy, not a fresh issuance.

        V5.0: the stored ledger payload is the source of truth. It is the
        exact set of values that was printed and signed at issuance, so
        re-rendering it reproduces the original document even if
        config.yaml has changed since. Re-deriving from the form inputs
        (the V4 behaviour) would silently rewrite history whenever a
        recipe changed - not acceptable for an audit trail. Re-derivation
        is kept only as a fallback for rows with an empty payload.
        """
        if not current_user.is_admin():
            abort(403)
        entry = (
            db.session.query(BatchLog).filter_by(doc_number=doc_number).first()
        )
        if not entry:
            abort(404)

        config = load_config()
        stored = entry.to_payload()
        form = entry.to_form()

        if stored:
            result = dict(stored)
        else:
            # Fallback for a row with no payload: rebuild from the captured
            # form inputs.
            try:
                if entry.batch_type == "cultivation" and form is not None:
                    result = calculate_cultivation(config, form)
                elif entry.batch_type == "extraction" and form is not None:
                    result = calculate_extraction_plan(config, form)
                elif entry.batch_type == "capsulation" and form is not None:
                    result = calculate_capsulation(config, form)
                else:
                    result = {}
            except (ValueError, KeyError, ZeroDivisionError, TypeError):
                result = {}

        # Stamp with the original doc number + timestamp; the audit-metadata
        # footer in the template makes the reprint status explicit.
        result["doc_number"] = entry.doc_number
        result["timestamp"] = entry.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        result["strain"] = result.get("strain") or entry.strain
        # V4.0: raw-material SOP numbers live in the stored payload;
        # re-derivation above does not regenerate them.
        for key in ("spawn_doc_number", "substrate_doc_number", "media_doc_number"):
            if key in stored:
                result[key] = stored[key]
        result["_reprint_of"] = entry.doc_number
        result["_reprint_operator"] = entry.user.username
        # Records issued by an earlier calculator keep their original shape;
        # the template marks them as a legacy document.
        result["_calculator_version"] = stored.get("schema_version", "4")
        result["_is_legacy"] = stored.get("schema_version") != CALCULATOR_VERSION

        if entry.batch_type == "cultivation":
            container_types = list(config["containers"].keys())
            media_params = config.get("media_parameters", {}) or {}
            return render_template(
                "index.html",
                result=result,
                container_types=container_types,
                doc_number=entry.doc_number,
                reprint=True,
                container_defaults=config["containers"],
                lc_basis_g=float(media_params.get("lc_rate_basis_g", 200)),
                lc_default_rate=float(media_params.get("lc_rate_ml", 5)),
            )
        elif entry.batch_type == "extraction":
            extraction_params = config["extraction_parameters"]
            return render_template(
                "extraction.html",
                result=result,
                error=None,
                doc_number=entry.doc_number,
                reprint=True,
                solvent_types=_allowed_solvent_types(config),
                portion_size_g=float(extraction_params["grams_per_bottle"]),
                stock_bottle_volume_ml=float(
                    extraction_params.get("stock_bottle_volume_ml", 2500)
                ),
            )
        elif entry.batch_type == "capsulation":
            return render_template(
                "capsulation.html",
                result=result,
                error=None,
                doc_number=entry.doc_number,
                reprint=True,
                dosage_modes=_capsulation_params(config)["dosage_modes"],
            )
        else:
            abort(400)

    # -----------------------------------------------------------------------
    # Settings (admin): user management + config.yaml editor.
    # The old /admin/users URL is kept as an alias for the same view.
    # -----------------------------------------------------------------------
    @app.route("/admin/settings", methods=["GET", "POST"])
    @app.route("/admin/users", methods=["GET", "POST"])
    @login_required
    def admin_settings():
        if not current_user.is_admin():
            abort(403)

        config_text_override = None  # preserves a rejected edit for fixing

        if request.method == "POST":
            action = request.form.get("action")

            # ---- Config editor -------------------------------------------
            if action == "save_config":
                submitted = request.form.get("config_text", "")
                try:
                    backup_name = _save_config_text(
                        submitted,
                        username=current_user.username,
                        user_id=current_user.id,
                    )
                    flash(
                        "Config saved — takes effect on the next page load. "
                        f"Previous version backed up to "
                        f"config_backups/{backup_name}."
                        if backup_name else
                        "Config saved — takes effect on the next page load.",
                        "success",
                    )
                    return redirect(url_for("admin_settings"))
                except ValueError as exc:
                    # Keep the admin's text on screen so it can be fixed;
                    # the file on disk was not touched.
                    flash(f"Config not saved: {exc}", "danger")
                    config_text_override = submitted

            # ---- User management -----------------------------------------
            target_id = request.form.get("user_id", type=int)
            target = db.session.get(User, target_id) if target_id else None

            if action == "create":
                username = request.form.get("username", "").strip()
                password = request.form.get("password", "")
                role = request.form.get("role", "user")
                if not username or not password:
                    flash("Username and password are required.", "danger")
                elif db.session.query(User).filter_by(username=username).first():
                    flash(f"Username '{username}' already exists.", "danger")
                elif not is_valid_role(role):
                    flash(
                        "Role must be one of: "
                        + ", ".join(ROLE_VALUES)
                        + ".",
                        "danger",
                    )
                else:
                    u = User(username=username, role=role)
                    u.set_password(password)
                    db.session.add(u)
                    db.session.commit()
                    flash(f"Created user '{username}' ({role}).", "success")

            elif action == "reset_password" and target:
                new_password = request.form.get("password", "")
                if not new_password:
                    flash("New password cannot be empty.", "danger")
                else:
                    target.set_password(new_password)
                    db.session.commit()
                    flash(
                        f"Password reset for '{target.username}'.", "success"
                    )

            elif action == "change_role" and target:
                new_role = request.form.get("role", "")
                if not is_valid_role(new_role):
                    flash("Invalid role.", "danger")
                else:
                    # System invariant: at least one admin must always remain.
                    # Check this BEFORE the self-demotion guard so the user
                    # always understands the true reason for the block.
                    if (
                        target.role in ADMIN_ROLES
                        and new_role not in ADMIN_ROLES
                        and db.session.query(User)
                        .filter(User.role.in_(ADMIN_ROLES), User.id != target.id)
                        .count()
                        == 0
                    ):
                        flash(
                            "Cannot demote the last remaining admin.",
                            "danger",
                        )
                    elif target.id == current_user.id and new_role not in ADMIN_ROLES:
                        flash(
                            "You cannot demote your own admin account while "
                            "logged in. Ask another admin to do it.",
                            "danger",
                        )
                    else:
                        target.role = new_role
                        db.session.commit()
                        flash(
                            f"Role for '{target.username}' set to "
                            f"'{new_role}'.",
                            "success",
                        )

            elif action == "delete" and target:
                if target.id == current_user.id:
                    flash("You cannot delete your own account.", "danger")
                elif (
                    target.role in ADMIN_ROLES
                    and db.session.query(User)
                    .filter(User.role.in_(ADMIN_ROLES), User.id != target.id)
                    .count()
                    == 0
                ):
                    flash(
                        "Cannot delete the last remaining admin.", "danger"
                    )
                else:
                    username = target.username
                    db.session.delete(target)
                    db.session.commit()
                    flash(f"Deleted user '{username}'.", "info")

            # A rejected config edit falls through to the render below
            # (with the submitted text preserved) instead of redirecting.
            if config_text_override is None:
                return redirect(url_for("admin_settings"))

        users = db.session.query(User).order_by(User.username).all()
        config_changes = (
            db.session.query(ConfigChange)
            .order_by(ConfigChange.timestamp.desc())
            .limit(20)
            .all()
        )
        return render_template(
            "settings.html",
            users=users,
            roles=ROLES,
            role_label=role_label,
            config_text=(
                config_text_override
                if config_text_override is not None
                else read_config_text()
            ),
            config_changes=config_changes,
        )

    @app.route("/account/password", methods=["GET", "POST"])
    @login_required
    def account_password():
        if request.method == "POST":
            current_pw = request.form.get("current_password", "")
            new_pw = request.form.get("new_password", "")
            confirm_pw = request.form.get("confirm_password", "")
            if not current_user.check_password(current_pw):
                flash("Current password is incorrect.", "danger")
            elif not new_pw or len(new_pw) < 6:
                flash(
                    "New password must be at least 6 characters.", "danger"
                )
            elif new_pw != confirm_pw:
                flash("New password and confirmation do not match.", "danger")
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash("Password updated.", "success")
                return redirect(url_for("account_password"))
        return render_template("change_password.html")


# ---------------------------------------------------------------------------
# Module-level app instance for `python app.py`
# ---------------------------------------------------------------------------
app = create_app()
register_routes(app)


if __name__ == "__main__":
    # Port/host are configurable so a second copy (e.g. a newer version
    # on Ubuntu) can run alongside an existing one without a clash:
    #   PORT=5001 ./run_ubuntu.sh   (run_ubuntu.sh defaults to 5001)
    # Host defaults to 0.0.0.0 so the app is reachable from other
    # computers on the network (e.g. http://<server-ip>:5001);
    # override with HOST=127.0.0.1 for local-only access.
    #
    # V5.0: debug mode is OFF unless PSYGA_DEBUG=1 is exported. The
    # Werkzeug debugger is a remote code execution surface, so it must
    # never be enabled on a host that other people can reach.
    if app.config.get("DEBUG"):
        print(
            "WARNING: PSYGA_DEBUG is on - the Werkzeug debugger is exposed. "
            "Do NOT run this on a shared or networked host.",
            flush=True,
        )
    app.run(
        debug=app.config.get("DEBUG", False),
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
    )
