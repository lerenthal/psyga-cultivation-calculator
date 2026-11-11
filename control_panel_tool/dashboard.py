#!/usr/bin/env python3
"""
Server Control Panel - entry point.

    python dashboard.py                # serves on 0.0.0.0:5050
    PORT=5050 python dashboard.py

The module-level ``app`` object also works under any WSGI server:

    gunicorn --bind 0.0.0.0:5050 dashboard:app
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from panel.app import create_app  # noqa: E402

app = create_app()


def main() -> None:
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5050"))
    debug = os.environ.get("PANEL_DEBUG", "0").strip().lower() in (
        "1", "true", "yes", "on",
    )
    print(f"  Server Control Panel on http://{host}:{port}", flush=True)
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
