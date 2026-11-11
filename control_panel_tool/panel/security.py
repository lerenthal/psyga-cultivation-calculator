"""
Backend selection and the panel's own security helpers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sys

from .base import Backend

CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
SESSION_KEY_NAME = "_panel_csrf"


def select_backend() -> Backend:
    """Pick the backend that matches the host we are running on."""
    if sys.platform.startswith("linux"):
        from .linux import LinuxBackend

        return LinuxBackend()
    if sys.platform.startswith("win"):
        from .windows import WindowsBackend

        return WindowsBackend()
    raise RuntimeError(
        f"the control panel does not support this platform: {sys.platform!r}"
    )


def resolve_panel_password() -> str:
    """
    The password that unlocks the panel.

    Order: PANEL_PASSWORD environment variable -> the secret file in the
    instance folder -> a freshly generated password (printed once).
    """
    env_password = os.environ.get("PANEL_PASSWORD")
    if env_password:
        return env_password

    instance = os.environ.get("PANEL_INSTANCE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
    )
    os.makedirs(instance, exist_ok=True)
    password_file = os.path.join(instance, "panel_password.txt")

    if os.path.exists(password_file):
        try:
            with open(password_file, "r", encoding="utf-8") as handle:
                stored = handle.read().strip()
            if stored:
                return stored
        except OSError:
            pass

    generated = secrets.token_urlsafe(12)
    try:
        with open(password_file, "w", encoding="utf-8") as handle:
            handle.write(generated)
        try:
            os.chmod(password_file, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    print(
        "\n  Control panel password (generated, stored in "
        f"{password_file}):\n      {generated}\n"
        "  Set PANEL_PASSWORD to choose it yourself.\n",
        flush=True,
    )
    return generated


def resolve_secret_key() -> str:
    """Session signing key: environment, then a persisted random key."""
    env_key = os.environ.get("PANEL_SECRET_KEY")
    if env_key:
        return env_key
    instance = os.environ.get("PANEL_INSTANCE_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance"
    )
    os.makedirs(instance, exist_ok=True)
    key_file = os.path.join(instance, "panel_secret_key")
    if os.path.exists(key_file):
        try:
            with open(key_file, "r", encoding="utf-8") as handle:
                stored = handle.read().strip()
            if stored:
                return stored
        except OSError:
            pass
    generated = secrets.token_hex(32)
    try:
        with open(key_file, "w", encoding="utf-8") as handle:
            handle.write(generated)
        try:
            os.chmod(key_file, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return generated


def hash_password(password: str, salt: str | None = None) -> str:
    """Salted SHA-256, used only for comparing the panel password."""
    salt = salt or secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time comparison against a stored ``salt$digest`` string."""
    try:
        salt, _, digest = stored.partition("$")
    except AttributeError:
        return False
    if not salt or not digest:
        return False
    candidate = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return hmac.compare_digest(candidate, digest)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(str(a), str(b))
