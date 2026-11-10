# PsygaBio Production System V5.0 — GMP Batch Calculators

Authenticated Flask application for mycology production planning. Three
calculator tabs generate printable, audit-stamped working documents, and
every issuance is written to an append-only SQLite ledger.

| Tab | Route | Prefix | Document |
|---|---|---|---|
| Cultivation | `/` | `WP-YYYYMMDD-NNN` | Facility Working Protocol |
| Extraction | `/extraction` | `EX-YYYYMMDD-NNN` | Facility Extraction Batch Record |
| Capsulation | `/capsulation` | `CP-YYYYMMDD-NNN` | Capsulation batch record |

## V5.0 changes

**Extraction is now portion-based.** The unit of production is one whole
**120 g portion per 2.5 L extraction bottle**. The V4 pack-based
indirection (Pack Size, Dry Mushroom Powder Lot #) is gone — the
production lot identifies the material. Only whole portions are extracted;
a leftover portion is documented and held for QC (GMP: no partial-bottle
extraction).

*Reference case* — 400 g at 2 bottles per extraction day:

| | |
|---|---|
| Total bottles (portions) | 3 (3 × 120 g = 360 g processed) |
| Extraction day 1 | 2 bottles = 240 g |
| Extraction day 2 | 1 bottle = 120 g |
| Leftover (held, not extracted) | 40 g |

**The printed document now carries three new things:**

1. **Solvent preparation — per bottle.** The bench stock is a **2.5 L
   bottle of 100% MeOH**; the 2.4 L working volume is prepared from it by
   taking MeOH out and putting water back in:
   *MeOH to take out = 2500 − (2400 × %/100)*,
   *water to put back = 2400 × (1 − %/100)*.
   At 90 % MeOH that is **take out 340 mL, put back 240 mL** → 2400 mL at
   90 %; at 75 % → 700 mL out / 600 mL back; at 100 % → 100 mL out / 0 back.
   One extraction bottle is then charged with **120 g of dried mushroom +
   2400 mL** of that working volume — a **1:20** mushroom : solvent ratio.
2. **Bottles & reagent totals** — bottles, powder processed, leftover,
   total solvent charged, how many 2.5 L stock bottles to have on the
   bench, total MeOH to remove, total water to add, expected extract.
3. **Timeline** — one row per calendar day, each round being one
   extraction day plus one rotary-evaporation day, with per-bottle and
   per-day volumes.

**Hardening.** V5.0 also closes the gaps found in the V4.0 review:

| Was | Now |
|---|---|
| Ledger immutability enforced only by ORM events (a bulk `Query.delete()` or raw SQL could rewrite history) | SQLite **triggers** block `UPDATE` and `DELETE` on `batch_logs` and `config_changes` — no code path, in this process or any other tool, can rewrite the record |
| Hard-coded `SECRET_KEY` in the source tree | Taken from `PSYGA_SECRET_KEY`, else a random key generated once and persisted to `instance/secret_key` |
| `debug=True` always on — the Werkzeug debugger was reachable on the LAN | Debug is **off** unless `PSYGA_DEBUG=1`; the app prints a warning if it is enabled |
| No CSRF protection on any form | Every POST requires a session-bound CSRF token (`_csrf_token`); requests without one get HTTP 400 |
| Comment claimed WAL, but the database was in `delete` journal mode | WAL is now actually enabled, together with `foreign_keys=ON` and `busy_timeout` |
| Document numbers could collide between two processes | The insert retries on the `UNIQUE` constraint, so the sequence is safe across processes |
| Seeded `admin` / `secureadminpassword` | A random password is generated, printed once and written to `instance/initial_admin_password.txt` (or set `PSYGA_ADMIN_PASSWORD`) |

**Reprints now reproduce the issued document exactly.** V4 re-derived the
result from the captured form inputs, which silently rewrote history
whenever a recipe changed. V5.0 re-renders the stored ledger payload, so
a reprint is the document that was signed. Records issued by an older
calculator are still reproduced from their stored payload and are marked
as legacy documents.

**V4.0 headline feature:** recipes and batch numbers follow the
SOP forms, and the Cultivation printout includes the raw-material
production records pre-filled with the calculated amounts:
**FORM-CULT-003** Spawn (`SP-YYYY-NNN`), **FORM-CULT-004** Substrate
(`SUB-YYYY-NNN`, with a per-bucket breakdown table — Bucket 1…N with
each bucket's ingredient weights), and **FORM-CULT-009** Media
(`MED-YYYY-NNN`). Substrate is **780/660/60/4800 = 6300g per
bucket** and grain **5kg : 10L** per SOP-CULT-004/005. Print all
together or use the per-form print buttons.

**V3.0 headline feature:** the Cultivation tab can reverse-engineer a
workplan from colonized spawn you already have. Fill in the optional
**Spawn Bags Colonized** + **Spawn Bag Size (g)** fields and the spawn
on hand becomes the limiting resource: containers = ⌊total spawn ÷
spawn per container⌋, substrate is calculated for exactly those
containers, and grain/liquid-culture prep is shown as not required.
Leave both blank for the standard target-driven calculation.

**V2.0 feature (still in V5.0):** every calculation option is
config-driven. An admin adds or changes options (containers, solvent
strengths, capsule sizes, dosage modes, batch sizes, ...) by editing
`config.yaml` — in the file or in the Settings GUI — no code change
and no restart (the file is re-read on every request). Invalid
entries are rejected with a clear message naming the exact key to fix.

## Adding / changing options (admin — config file only)

| Want to… | Edit in `config.yaml` |
|---|---|
| Add a container (e.g. `2liter_box`) | Copy a block under `containers:`, set `substrate` / `spawn` / `liquid_culture` |
| Add a solvent strength (e.g. `80%`) | Add `- "80%"` under `extraction_parameters.allowed_solvent_types` |
| Add a capsule size | Copy a block under `capsulation_parameters.capsule_specs` |
| Change a dosage-mode label / order | Edit `capsulation_parameters.dosage_modes` |
| Change batch sizes / buffers / recipes | Edit the matching top-level values |

The new option appears in the calculator dropdown on the next page
load. See `quickstart.md` § "Adding options" for worked examples.
Note: a dosage mode added to config still needs a calculation rule in
`app.py` to be usable (`psilocybin_only` and `tpe` are the supported
rules) — the app says so explicitly if you try.

> ⚠️ The `2liter_box` values in `config.yaml` (600g substrate / 200g
> spawn / 6ml LC) are editable in Settings — confirm they match the
> approved production parameters before using them in a GMP batch
> document.

## Running

From the project directory, run the launcher for your shell (the Windows
launchers call `.venv\Scripts\python.exe` directly — no `activate` step;
on Ubuntu use `run_ubuntu.sh`, which also creates the venv and installs
requirements on first run):

```bat
run.bat
```

```powershell
.\run.ps1
```

To run with **no console window** on Windows, double-click
`run_hidden.vbs` (starts the app detached and hidden; output goes to
`app.log`, stop via Task Manager → end `python.exe`, or reboot).

```bash
./run.sh          # git-bash / WSL, existing venv
./run_ubuntu.sh   # Ubuntu, bootstraps .venv if missing; serves on :5001
                  # (override with PORT=xxxx) so it can run alongside an
                  # earlier copy still on :5000
```

Then open `http://127.0.0.1:5000` (Windows) or `:5001` (Ubuntu). On first
launch against an empty database a default `admin` user is seeded with a
**random** password, which is printed to the console and written to
`instance/initial_admin_password.txt` — change it immediately after the
first login (Account → Change Password) and delete that file. Set
`PSYGA_ADMIN_PASSWORD` before the first launch to choose the password
yourself. Admin-only pages are **Admin Master Logs** (`/admin/history`)
and **Settings** (`/admin/settings`, also at `/admin/users`) — the
Configuration editor and User Management.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PSYGA_SECRET_KEY` | random, persisted to `instance/secret_key` | Session cookie signing key. Set it explicitly on production hosts so the key survives a redeploy. |
| `PSYGA_DEBUG` | `0` | Set to `1` to enable the Werkzeug debugger. **Development only** — it is remote code execution. |
| `PSYGA_ADMIN_PASSWORD` | random | Password for the seeded `admin` account on a fresh database. |
| `HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` to keep the app on the machine itself. |
| `PORT` | `5000` | Listen port. |

## Roles & users

Available roles: **Facility Manager**, **Cultivation Manager**,
**Cultivation Operator**, **Production Manager**, **Production Operator**,
**Quality Manager**, **User**, **Administrator**. Only the
Administrator role can open Settings, manage users, edit the config,
and view/reprint history; every role can use the calculators and
change its own password.

Create users from Settings → User Management, or from the command line
(passwords are typed at deploy time and never stored in the repo):

```bash
.venv/bin/python create_users.py USERNAME ROLE PASSWORD
# e.g. .venv/bin/python create_users.py ido cultivation_manager '...'
```

The seeded `admin` account remains for first boot; create
your real users (e.g. a Cultivation Manager, a Production Manager, and
an Administrator) and change/remove the seeded password.

## Ubuntu deploy (test server)

1. Copy/rsync the folder (exclude `.venv/`, `instance/`,
   `config_backups/`, `__pycache__/`) to the server, or clone it.
2. `chmod +x run_ubuntu.sh` (once), then `./run_ubuntu.sh`.
   It creates `.venv` if missing (`uv` preferred, else `python3 -m venv`),
   installs `requirements.txt`, and serves on **:5001**
   (`PORT=xxxx ./run_ubuntu.sh` to override). Needs Python 3.11+
   (`sudo apt install python3 python3-venv python3-full` if missing).
   The script is POSIX `sh` compatible, so `sh run_ubuntu.sh` also
   works (Ubuntu's default `sh` is dash, which rejects bash-only
   options like `set -o pipefail`).
   To run detached without a terminal: `./run_ubuntu.sh --background`
   (logs to `app.log`; stop with `kill <PID>` from the printed message,
   or `pkill -f app.py`).
3. Open `http://<server>:5001/login` (from any computer on the
   network — the app binds `0.0.0.0`), log in as the seeded `admin`,
   create your users (Settings → User Management, or
   `create_users.py`), and change the seeded password.
   If the page doesn't load from another computer, open the port in
   the Ubuntu firewall: `sudo ufw allow 5001/tcp` (and check the
   server IP with `hostname -I`). Use `HOST=127.0.0.1` to restrict
   the app to the server itself.
4. `config.yaml` is resolved relative to `app.py`, so the app can be
   started from any working directory; `run_ubuntu.sh` still `cd`s to
   the project dir. The database is created at
   `instance/psyga_gmp.db` on first boot.

To recreate the environment:

```bash
uv venv
uv pip install -r requirements.txt
```

## Tests

```bat
test.bat
```

or, from the project directory:

```bash
.venv/Scripts/python.exe -m pytest tests -v
```

Currently **183 tests passing** (pure calculator math, sequence
generation, Flask integration, user management, form capture & reprint,
config loading, the V2.0 config-driven-options suite and the V5.0
hardening suite — CSRF, DB-level ledger immutability, secret handling,
SQLite pragmas). Tests run against an in-memory SQLite database and
never touch `instance/psyga_gmp.db`. (`config.yaml` is resolved
relative to `app.py`, so the working directory no longer matters in
V2.0 — but run from the project directory anyway for consistency.)

## How it works

See `quickstart.md` for the calculation flow of each tab, the audit
ledger, and configuration reference.

## Layout

```
app.py            # factory, models, calculator engines, routes
config.yaml       # production parameters (containers, recipes, extraction)
templates/        # base + one template per page; print CSS hides forms
tests/test_app.py # pytest suite (in-memory DB)
instance/         # psyga_gmp.db (git-ignored)
```

