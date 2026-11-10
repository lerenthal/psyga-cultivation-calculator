#!/usr/bin/env python3
"""
Create or update a user in the PsygaBio Production System database.

Usage (from the project directory, with the app venv active):
    .venv/bin/python create_users.py USERNAME ROLE PASSWORD
    .venv/bin/python create_users.py ido cultivation_manager 'REDACTED_PASSWORD'
    .venv/bin/python create_users.py yaniv administrator 'REDACTED_PASSWORD'

Roles: facility_manager, cultivation_manager, cultivation_operator,
       production_manager, production_operator, quality_manager,
       user, administrator

The password is passed on the command line (never stored in this
file) so it does not end up in git. If the user already exists, the
password and role are updated. The app's seeded default admin is
created automatically on first boot if the database is empty.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import ROLE_VALUES, User, create_app, db  # noqa: E402


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    username, role, password = sys.argv[1], sys.argv[2], sys.argv[3]
    if role not in ROLE_VALUES:
        print(f"Invalid role: {role!r}")
        print("Valid roles: " + ", ".join(ROLE_VALUES))
        return 2
    if len(password) < 6:
        print("Password must be at least 6 characters.")
        return 2
    app = create_app()
    with app.app_context():
        user = db.session.query(User).filter_by(username=username).first()
        if user is None:
            user = User(username=username, role=role)
            db.session.add(user)
            action = "Created"
        else:
            user.role = role
            action = "Updated"
        user.set_password(password)
        db.session.commit()
        print(f"{action} user '{username}' with role '{role}'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
