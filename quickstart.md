# PsygaBio Production System — Quickstart (V5.0)

Authenticated Flask application with three GMP batch calculators
(cultivation, extraction, capsulation). Every generated document is
persisted to an append-only SQLite ledger with a daily sequence number
(`WP-` / `EX-` / `CP-`).

## Installation

The virtual environment is pre-installed. To recreate it, from the
project directory:

```bash
uv venv
uv pip install -r requirements.txt
```

## Running

Use a launcher — the Windows ones call `.venv\Scripts\python.exe`
directly, so no `activate` step (and no PowerShell execution-policy
issue). On Ubuntu, `run_ubuntu.sh` additionally bootstraps the
environment on first run:

```bat
run.bat
```

```powershell
.\run.ps1
```

```bash
./run.sh          # git-bash / WSL, existing venv
./run_ubuntu.sh   # Ubuntu, creates .venv + installs requirements if missing;
                  # serves on :5001 (override with PORT=xxxx) so it can run
                  # alongside an earlier copy still on :5000
```

Open `http://127.0.0.1:5000` (Windows) or `:5001` (Ubuntu). On first launch against an empty database
a default `admin` user is seeded — change that password immediately
after first login.

### Ubuntu deploy (test server)

```bash
# 1. Copy the folder to the server (exclude .venv/, instance/,
#    config_backups/, __pycache__/) — or clone it.
chmod +x run_ubuntu.sh
./run_ubuntu.sh          # creates .venv + installs requirements if missing
                         # serves on :5001 (PORT=xxxx to override)
./run_ubuntu.sh --background   # detached, no terminal needed (log: app.log)
```

Needs Python 3.11+ (`sudo apt install python3 python3-venv python3-full`
if missing). The script is POSIX `sh` compatible, so `sh run_ubuntu.sh`
also works. Stop a background instance with `pkill -f app.py`. `config.yaml` is resolved relative to `app.py`, so the
app starts from any working directory. The database is created at
`instance/psyga_gmp.db` on first boot; log in as the seeded `admin`,
then create your users (Settings → User Management, or
`.venv/bin/python create_users.py USERNAME ROLE PASSWORD`) and change
the seeded password.

The app binds `0.0.0.0`, so it's reachable from other computers as
`http://<server-ip>:5001`. If it doesn't load remotely, open the
port: `sudo ufw allow 5001/tcp` (find the IP with `hostname -I`).
`HOST=127.0.0.1` restricts it to the server itself.

### Roles & users

Available roles: **Facility Manager**, **Cultivation Manager**,
**Cultivation Operator**, **Production Manager**, **Production Operator**,
**Quality Manager**, **User**, **Administrator**. Only Administrator
can open Settings, manage users, edit the config, and view/reprint
history; every role can use the calculators and change its own
password. Legacy `admin`/`technician` values in existing databases
keep working (treated as Administrator / User).

## Tab 1 — Cultivation (`/`, prefix `WP`)

Inputs: strain, culture lot #, container type (any option listed under
`containers:` in `config.yaml` — ships with `monotub`, `4liter_box`,
`2liter_box`), avg fresh yield (g/container), target dry weight (g),
plus the optional **Spawn Bag Fill Weight (g)** and **LC Rate
(mL per 200 g spawn)** (both default to the controlled values in
`config.yaml`).

Calculation flow:

1. **Dry yield/container** = avg_yield_fresh × `dry_matter_ratio` (0.07)
2. **Base containers** = target_dry_weight / dry yield
3. **Total containers** = ceil(base × (1 + `contamination_buffer` = 0.10))
4. **Total substrate** = total_containers × substrate/container
5. **Buckets** = ceil(total_substrate / bucket yield), where bucket yield
   = sum of `substrate_recipe` = 780 + 660 + 60 + 4800 = **6300 g** (SOP FORM-CULT-004)
   (single source of truth — there is no hardcoded divisor)
6. **Grain batches** = ceil(total_spawn / 10000 g)
7. **Spawn bags to fill** = ceil(total_spawn / spawn bag fill weight). Bags
   are filled continuously from the grain batches, so the last bag of the
   run may be a partial fill and the per-batch bag counts add up to the
   lot total exactly.
8. **LC per bag** = bag spawn weight ÷ 200 g × 5 mL
   (`media_parameters.lc_rate_ml`, `lc_rate_basis_g`); **LC needed** =
   the sum over all bags; **LC to prepare** = LC needed + 20 % backup
   (the floor is enforced); **media to prepare** = LC to prepare + 30 %
   media excess.

Output (Facility Working Protocol): container prep, bulk substrate
(coir, vermiculite, gypsum, water), grain spawn (grain, water),
inoculation/bagging, Technician + QA + Date signature lines — followed
by the printable SOP forms: FORM-CULT-003 (spawn), FORM-CULT-003
(incubation), **FORM-CULT-003A (G5 inoculation)**, FORM-CULT-004
(substrate), FORM-CULT-009 (media).

### FORM-CULT-003A — G5 Inoculation Record (V6.0)

Recreated from the controlled Word form `FORM-CULT-003A v3.0`. It shares
the **SP-YYYY-NNN** number with FORM-CULT-003 (it continues the same
spawn batch) and has its own **Print G5 inoculation form** button.

Its per-bag inoculation log is **automatically sized to the number of
bags planned** — one row per bag, carrying that bag's spawn weight and
the exact LC volume for it. The log is printed 30 bags per sheet (15 in
the left block, 15 in the right) with continuation sheets, exactly like
the paper form. It is not printed in reverse mode, where there are no
bags to inoculate.

### Producing the forms (V6.1)

The cultivation tab ends with a **Produce a form** panel listing every
document with its form number and document number:

| Form | Form no. | Document # |
|---|---|---|
| Working Protocol | FORM-CULT-001 | `WP-YYYYMMDD-NNN` |
| Spawn Batch Production Record | FORM-CULT-003 | `SP-YYYY-NNN` |
| Spawn Incubation & Release Log | FORM-CULT-003 | `SP-YYYY-NNN` |
| G5 Inoculation Record | FORM-CULT-003A | `SP-YYYY-NNN` |
| Substrate Batch Production Record | FORM-CULT-004 | `SUB-YYYY-NNN` |
| Liquid Media Preparation Batch Record | FORM-CULT-009 | `MED-YYYY-NNN` |

Each **Produce** button prints (or saves as PDF) that document on its
own, so every document carries **its own page numbering** (Page 1 of N)
and its own running header. There is no "print everything" button —
printing the whole page in one go would give one continuous page count
across all six documents.

Every signature carries its own date field, so a protocol signed by the
bench operator and released by QA can be dated independently.

### Reverse calculation (spawn-first, V3.0)

Optional inputs: **Spawn Bags Colonized** + **Spawn Bag Size (g)**.
Leave both blank for the standard flow above. Fill **both** in and the
colonized spawn on hand becomes the limiting resource:

1. **Spawn on hand** = bags × bag size (e.g. 13 × 800 = 10,400 g)
2. **Containers** = ⌊spawn on hand ÷ spawn/container⌋ from
   `config.yaml` (13 × 800 g into `2liter_box` at 200 g/container =
   **52 containers**); leftover spawn (spawn that cannot fill a
   further container) is reported on the printout
3. **Substrate** = containers × substrate/container, buckets and
   ingredients exactly as in forward mode (52 × 600 = 31,200 g →
   5 buckets at current config)
4. **Grain prep and liquid culture = not required** (spawn already
   colonized) — shown as 0 / "Not required" on the printout
5. Target dry weight and the contamination buffer do **not** apply;
   avg fresh yield (if given) only feeds the expected-dry-yield
   estimate (500 × 0.07 × 52 = 1,820 g in the example)

Validation: both spawn fields together or neither; bags must be a
whole number; spawn must cover at least one full container or no
document is issued. The printout carries a dedicated
**REVERSE CALCULATION — COLONIZED SPAWN ON HAND** section, and the
inputs are stored in the ledger like any other form field (reprint
reproduces the reverse document exactly).

### Raw-material SOP forms (V4.0)

Every WP printout is followed by the raw-material production record
forms, recreated from the SOP Word forms in the app’s print format
(SOP header + form ID on each, `Page X of Y` numbering throughout):

- **FORM-CULT-003 Spawn** (`SP-YYYY-NNN`, SOP-CULT-004) — bag fill
  weight, # bags, grain batches/totals pre-filled; HP1 checks, 20-row
  incubation log, release criteria, deviations.
- **FORM-CULT-004 Substrate** (`SUB-YYYY-NNN`, SOP-CULT-005) — recipe
  totals **plus a per-bucket breakdown table** (Bucket 1…N, each
  bucket’s 780g coir / 660g vermiculite / 60g gypsum / 4800ml water),
  process, HP1/HP2 hold points, sign-off.
- **FORM-CULT-009 Media** (`MED-YYYY-NNN`, SOP-CULT-003) — per-L
  recipe table, total LC volume pre-filled, pH/sterilization,
  release criteria, sign-off.

Batch numbers follow the SOP format `PREFIX-YYYY-NNN` and share the
WP master’s sequence ordinal (e.g. `WP-20261109-003` →
`SP-2026-003`, `SUB-2026-003`, `MED-2026-003`), so all forms for one
run trace to one master batch. They are stored in the ledger payload
and preserved on reprint. Print all together, or use the per-form
print buttons (WP / Spawn / Substrate / Media). In reverse mode only
the substrate form is printed (spawn already colonized, no LC).

Recipes now follow the SOPs: substrate **780 + 660 + 60 + 4800 =
6300 g per bucket**, grain **5 kg : 10 L** per batch.

## Tab 2 — Extraction (`/extraction`, prefix `EX`)

**Portion-based** multi-day / multi-round planner (V5.0). Inputs:
production lot, strain, solvent (`75%` | `90%` | `100%`), total dried
weight, bottles per extraction day, grinder-loss budget per round,
optional yield override.

The unit of production is one whole **120 g portion per 2.5 L extraction
bottle** (`extraction_parameters.grams_per_bottle`). The V4 *Pack Size*
and *Dry Mushroom Powder Lot #* inputs are gone — the production lot
identifies the material.

- Total bottles = ⌊total weight ÷ 120 g⌋; the leftover is documented and
  held — **no partial-bottle extraction** (GMP rule)
- Extraction days = ⌈total bottles ÷ bottles per day⌋; 2 calendar days
  per round: extraction day + rotary-evaporation day
- Per-bottle solvent preparation: the bench stock is a **2.5 L bottle of
  100% MeOH**; the 2.4 L working volume is prepared from it by taking
  MeOH out and putting water back in:
  `MeOH to take out = 2500 − (2400 × %/100)`,
  `water to put back = 2400 × (1 − %/100)`.
  At 90 % MeOH that is **take out 340 mL, put back 240 mL** → 2400 mL at
  90 %; at 75 % → 700 mL out / 600 mL back; at 100 % → 100 mL out / 0 back.
  One extraction bottle is charged with **120 g of dried mushroom +
  2400 mL** of that working volume — a **1:20** mushroom : solvent ratio
- Expected extract = grams processed ÷ yield factor. Default factor is
  **4.0 (1:4)** from `config.yaml`; a positive supervisor override
  replaces it and is flagged on the document
- Guard: the lot must cover at least one whole portion (120 g)

Output: lot summary, **per-bottle solvent preparation** (the formula and
the resulting volumes for one bottle), **bottle and reagent totals**
(incl. how many 2.5 L stock bottles to have on the bench), a
**calendar-day timeline**, and Operator + QA + Date signature
lines. The legacy single-shot `calculate_extraction` engine is retained
for back-compatibility and uses the same per-bottle rule.

Reference case — 400 g, 2 bottles per day: 3 bottles (360 g processed),
extraction day 1 = 2 bottles (240 g), extraction day 2 = 1 bottle
(120 g), 40 g left over.

## Tab 3 — Capsulation (`/capsulation`, prefix `CP`)

Inputs: production lot, grams of extract, psilocybin %, psilocin %,
anti-caking % (default 5) and agent name, dosage mode, and **at least
one source EX document number** (free-text cross-tab traceability —
required, not a foreign key).

- **TPE %** = psilocybin % + psilocin % × 1.3914
  (MW ratio 284.25 / 204.27)
- Dosage mode `psilocybin_only` uses psilocybin mass only; mode `tpe`
  uses the combined TPE mass
- Anti-caking is % of **blend**: `anti = extract × pct / (100 − pct)`,
  `blend = extract + anti` (so a 5% blend dose = 5.26% of extract —
  the inverse is shown on the document for sanity-checking)
- Capsule table for types 0 / 1 / 2 (10 / 5 / 2 mg): blend per capsule
  and max capsules from the lot

Output: source traceability section, active-mass math, capsule table,
Operator + QA + Date signature lines.

## Audit ledger & admin pages

Every POST that calculates successfully writes one immutable
`BatchLog` row: doc number, batch type, UTC timestamp, operator,
strain/lot, the calculated payload (JSON), and the raw form inputs
(JSON). Rows cannot be updated or deleted — corrections require a new
document. **V5.0** enforces this in the database itself with SQLite
triggers, so a bulk `Query.delete()`, a raw `UPDATE`/`DELETE`, or any
other tool attached to the file is refused too — not just the ORM path.

Admin-only (`/admin/history`, `/admin/history/view/<doc>`,
`/admin/history/reprint/<doc>`): master log, single-record audit view
(operator + original inputs), and reprint. **V5.0** reprint re-renders
the *stored payload* — the exact document that was signed — so a later
config change can never silently rewrite a reprinted record. It keeps
the **original** doc number, creates **no** new ledger row, and shows a
REPRINT banner. Records issued by an older calculator are reproduced
from their stored payload and marked as legacy documents.

User management (`/admin/settings` → User Management, also at
`/admin/users`): create users (8 roles: Facility Manager, Cultivation
Manager, Cultivation Operator, Production Manager, Production Operator,
Quality Manager, User, Administrator), reset passwords, change roles,
delete users. Only Administrator can manage users. The last remaining
admin cannot be demoted or deleted, and you cannot delete yourself.
Any logged-in user can change their own password at
`/account/password` (min 6 characters). Users can also be created
from the command line: `.venv/bin/python create_users.py USERNAME ROLE PASSWORD`.

## Printing

**Print / Save as PDF** on any result page hides the form and
on-screen panels (print CSS) — only the protocol document and
signature lines print.

## Configuration

`config.yaml` is the single source of truth for every calculation
option. It is re-read on every request (resolved relative to `app.py`),
so admin changes take effect on the next page load — **no code change,
no restart**. Every load is validated; a broken entry produces a
message naming the exact key (e.g. `containers.2liter_box.spawn must
be a positive number`) instead of a stack trace.

```yaml
contamination_buffer: 0.10    # 10% extra containers for loss
dry_matter_ratio: 0.07         # 7% dry yield from fresh weight

substrate_recipe:              # yields 6300g total per bucket (SOP FORM-CULT-004)
  coco_coir: 780
  vermiculite: 660             # grade 2/3
  gypsum: 60
  water: 4800

grain_batch:                   # SOP FORM-CULT-003: 5 kg : 10 L
  grain: 5000
  water: 10000

grain_batch_size_g: 10000      # spawn grams per grain batch

containers:                    # <-- add a block to add a container
  monotub:
    substrate: 2100            # grams
    spawn: 700                 # grams
    liquid_culture: 20         # ml
  4liter_box:
    substrate: 1600
    spawn: 500
    liquid_culture: 12
  2liter_box:
    substrate: 600
    spawn: 200
    liquid_culture: 6

extraction_parameters:
  grams_per_bottle: 120
  solvent_volume_per_bottle_ml: 2400
  stock_bottle_volume_ml: 2500
  concentration_factor: 4.0    # 1x4 (25% yield)
  resuspension_water_per_bottle_ml: 150
  allowed_solvent_types:       # <-- add a line to add a solvent strength
    - "100%"
    - "90%"
    - "75%"

capsulation_parameters:
  dosage_modes:                # labels/order editable; the calculation
    - id: psilocybin_only      # rules (psilocybin_only, tpe) live in app.py
      label: "Psilocybin only"
    - id: tpe
      label: "TPE (psilocybin + 1.39 x psilocin)"
  capsule_specs:               # <-- add a block to add a capsule size
    - type: "0"
      dose_mg: 10
      label: "Type 0 / 10mg dose"
    - type: "1"
      dose_mg: 5
      label: "Type 1 / 5mg dose"
    - type: "2"
      dose_mg: 2
      label: "Type 2 / 2mg dose"
  default_anti_caking_pct: 5.0
  mw_psilocybin: 284.25
  mw_psilocin: 204.27
```

## Adding options (worked example)

To add a new container such as `2liter_box` (already present in the
shipped `config.yaml`):

1. Open `config.yaml` (or **Settings → Configuration** in the app) and
   set the values under `containers:` (substrate g, spawn g, liquid
   culture ml per container).
2. Save (in the GUI) or just reload the Cultivation page (file edit) —
   `2liter_box` is in the dropdown. Nothing else to do.

The same pattern adds anything else: a solvent (`- "80%"` under
`allowed_solvent_types`), a capsule size (copy a `capsule_specs`
block), or a dosage-mode label. A V1 `config.yaml` without the new
sections still works unchanged — V1 defaults fill the gaps.

## Settings page (admin)

The nav item formerly "User Management" is now **Settings**
(`/admin/settings`; the old `/admin/users` URL still works). It holds:

- **Configuration editor** — the full `config.yaml` in a text area.
  Save validates first (YAML + `validate_config`); invalid input is
  rejected with the exact key named and the file left untouched.
  Successful saves are atomic, backed up to `config_backups/`, take
  effect on the next page load, and are recorded in the
  **Config Change History** table on the same page (who, when, backup
  file). The full saved text is stored in the `config_changes` table
  for audit reconstruction.
- **User Management** — unchanged: create, reset password, change
  role, delete (last-admin and self-delete guards intact).

## Testing

```bat
test.bat
```

or from the project directory:

```bash
.venv/Scripts/python.exe -m pytest tests -v
```

Expected output: **207 passed**. Tests use an in-memory SQLite
database and never touch `instance/psyga_gmp.db`.

Test coverage:

- Pure math: cultivation (all configured containers incl. 2liter_box,
  6300 g bucket, 4800 ml water), extraction displacement (75/90/100%),
  extraction plan (portions, day allocation, leftover, per-bottle
  solvent protocol, yield override, grinder loss), capsulation (TPE,
  anti-caking, capsule table)
- Config-driven options (V2.0): new container/solvent/capsule via
  config only, validation rejecting broken entries, V1-config
  backward compatibility, dropdowns listing configured options
- Reverse cultivation (V3.0): 13×800g → 52 containers worked example,
  leftover spawn, both-or-neither validation, grain/LC zeroed,
  persistence + reprint of reverse documents
- Design (V3.0): title ribbon on all pages, per-tab guidance boxes,
  optional-field divider, black-and-white templates (no colored hex)
- Print layout (V3.0): left-aligned dark-grey ribbon, automatic user
  stamp, comment line per section, repeating printed header with
  "Page X of Y" numbering, container help topic, printable
  *INDEX calculation-logic section below the signatures
- Raw-material SOP forms (V4.0): SP/SUB/MED-YYYY-NNN numbering linked
  to the WP master, per-bucket substrate breakdown, spawn HP1 +
  incubation log, media recipe/volume, reverse mode prints substrate
  form only, reprint preserves SOP numbers
- Sequence generation (format, increment, per-prefix independence)
- Flask integration (login required, POST → doc number + ledger row,
  admin 403/visibility, capsulation/extraction reprint, REPRINT banner)
- User management (create, duplicate, reset, role, last-admin and
  self-delete guards) and self-service password change
- Form capture & reprint (form_data stored, legacy-row fallback)
- Config loading
- Hardening (V5.0): CSRF token required on every POST, ledger
  immutability enforced by database triggers (bulk delete and raw SQL
  both blocked), no hard-coded secret in the source tree, debug off by
  default, WAL + foreign keys enabled, and the V5 extraction document
  sections (per-bottle protocol, totals, timeline, leftover)

## Project structure

```
order_form_app/ver5/   # V5.0 (portion-based extraction; DB-level ledger immutability, CSRF, no hard-coded secret)
├── app.py              # factory, models, engines, routes
├── create_users.py     # CLI: create/update a user (password via argv, not stored)
├── config.yaml         # ALL calculation options (admin-editable)
├── quickstart.md       # this guide
├── README.md           # overview, running, tests
├── requirements.txt    # Flask, PyYAML, Flask-SQLAlchemy, Flask-Login, pytest
├── run.bat / run.ps1 / run.sh / run_ubuntu.sh / test.bat
├── templates/
│   ├── base.html       # nav, flashes, print styles
│   ├── index.html      # cultivation form + Working Protocol
│   ├── extraction.html # extraction form + batch record (V5 sections)
│   ├── capsulation.html# capsulation form + batch record
│   ├── explain.html    # explanation + REPRINT banner macros
│   ├── history.html / history_view.html
│   ├── settings.html     # Settings: config editor + history + users
│   ├── raw_materials.html# SOP raw-material form macros (V4)
│   ├── change_password.html / login.html
├── tests/test_app.py   # 183 pytest tests
└── instance/           # psyga_gmp.db (git-ignored)
```
