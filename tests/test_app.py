"""
Test suite for the PsygaBio Production System.

All tests run against an isolated in-memory SQLite database.
The `setup_method` hook performs a one-time test fixture install and
logs in a technician + admin user so HTTP tests can bypass the
authentication gate and exercise the math/audit-trail behavior.

V5.0: every POST is CSRF protected. The `client` fixture returns a thin
wrapper that fetches a token once and injects it into every POST, so the
test bodies stay focused on behaviour rather than on the token plumbing.
Tests that assert the protection itself use the raw client.
"""

import math
import os
import re
import sys
from datetime import datetime, timezone

import pytest
import yaml

# Ensure the project root is on the import path regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    BatchLog,
    User,
    app,
    calculate_cultivation,
    calculate_extraction,
    create_app,
    db,
    generate_sequence_number,
    load_config,
    register_routes,
    sop_doc_number,
)
from flask_login import login_user  # noqa: E402


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def test_app():
    """Build a fresh app with an in-memory DB and routes registered."""
    overrides = {
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
    }
    flask_app = create_app(overrides)
    register_routes(flask_app)
    yield flask_app
    # Clean up the in-memory DB
    with flask_app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def reset_db(test_app):
    """
    Rebuild the schema between tests for full isolation.

    V5.0 note: the ledger tables carry SQLite triggers that make the rows
    append-only, so the tables cannot simply be emptied with DELETE.
    Dropping and recreating them (and reinstalling the triggers) is the
    supported way to reset the fixture state.
    """
    from app import _install_immutability_triggers
    with test_app.app_context():
        db.session.remove()
        db.drop_all()
        db.create_all()
        _install_immutability_triggers()
        # Seed a known admin + technician for every test.
        admin = User(username="admin", role="admin")
        admin.set_password("secureadminpassword")
        tech = User(username="tech1", role="technician")
        tech.set_password("techpass")
        db.session.add_all([admin, tech])
        db.session.commit()
    yield


@pytest.fixture
def client(test_app):
    """A test client that transparently satisfies the CSRF protection."""
    return CsrfTestClient(test_app.test_client())


class CsrfTestClient:
    """
    Wraps a Flask test client and injects a valid CSRF token into every
    POST. The token is fetched once from a rendered form and then reused
    for the life of the client (it lives in the session cookie).
    """

    def __init__(self, flask_client):
        self._flask_client = flask_client
        self._token = None

    def token(self):
        if self._token is None:
            html = self._flask_client.get("/login").get_data(as_text=True)
            match = re.search(r'name="_csrf_token" value="([^"]+)"', html)
            self._token = match.group(1) if match else ""
        return self._token

    def get(self, *args, **kwargs):
        return self._flask_client.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        data = kwargs.get("data")
        if data is not None:
            data = dict(data)
            data.setdefault("_csrf_token", self.token())
            kwargs["data"] = data
        return self._flask_client.post(*args, **kwargs)

    def raw(self):
        """The unwrapped client, for tests that need to bypass the helper."""
        return self._flask_client

    def __getattr__(self, name):
        return getattr(self._flask_client, name)


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


# ---------------------------------------------------------------------------
# TestCultivationMath
# ---------------------------------------------------------------------------
class TestCultivationMath:
    """Unit tests for the pure cultivation calculation engine."""

    def _form(self, **overrides):
        base = {
            "strain": "A. cubensis",
            "cult_lot": "CL-2024-001",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        }
        base.update(overrides)
        return base

    def test_monotub_full_flow(self):
        cfg = load_config()
        result = calculate_cultivation(cfg, self._form())
        assert result["dry_yield_per_container"] == 35.0
        assert result["base_containers"] == 29
        assert result["total_containers"] == 32
        assert result["total_substrate"] == 67200
        assert result["substrate_bucket_count"] == 11  # ceil(67200/6300), SOP recipe
        assert result["total_spawn"] == 22400
        assert result["grain_batch_count"] == 3

    def test_4liter_box_full_flow(self):
        cfg = load_config()
        result = calculate_cultivation(
            cfg, self._form(avg_yield_fresh="400", target_dry_weight="500",
                            container_type="4liter_box")
        )
        assert round(result["dry_yield_per_container"], 2) == 28.0
        assert result["total_containers"] == 20
        # Values driven by current config (single source of truth):
        box = cfg["containers"]["4liter_box"]
        assert result["total_substrate"] == 20 * box["substrate"]
        assert result["total_spawn"] == 20 * box["spawn"]
        # V6.0: LC is dosed by spawn mass, not per container:
        #   LC = total spawn / 200 g * 5 mL
        assert result["total_lc"] == pytest.approx(
            result["total_spawn"] / 200 * 5
        )
        assert result["lc_needed_ml"] == result["total_lc"]
        assert result["bag_size_spawn"] == box["spawn"]
        # Substrate buckets = ceil(total_substrate / 6300) (SOP recipe)
        assert result["substrate_bucket_count"] == math.ceil(
            20 * box["substrate"] / 6300
        )

    def test_invalid_container_type_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_cultivation(cfg, self._form(container_type="bag"))

    def test_zero_yield_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_cultivation(cfg, self._form(avg_yield_fresh="0"))

    def test_substrate_bucket_uses_6300g_yield(self):
        """
        V4.0 SOP substrate recipe (FORM-CULT-004):
            coco_coir 780g + vermiculite 660g + gypsum 60g + water 4800ml
            = 6300g per bucket.
        The number of buckets is total_substrate / 6300, ceil().
        """
        cfg = load_config()
        # 32 monotubs * 2100g = 67200g substrate
        # 67200 / 6300 = 10.67 -> ceil = 11 buckets
        result = calculate_cultivation(
            cfg, self._form(avg_yield_fresh="500", target_dry_weight="1000")
        )
        assert result["substrate_bucket_count"] == 11
        assert result["total_substrate_water"] == 11 * 4800
        assert result["total_coir"] == 11 * 780
        assert result["total_vermiculite"] == 11 * 660
        assert result["total_gypsum"] == 11 * 60

    def test_substrate_recipe_in_config_matches_sop(self):
        cfg = load_config()
        assert cfg["substrate_recipe"]["water"] == 4800
        assert cfg["substrate_recipe"]["coco_coir"] == 780
        assert cfg["substrate_recipe"]["vermiculite"] == 660
        assert cfg["substrate_recipe"]["gypsum"] == 60
        assert sum(cfg["substrate_recipe"].values()) == 6300
        assert cfg["grain_batch"] == {"grain": 5000, "water": 10000}


# ---------------------------------------------------------------------------
# TestExtractionMath
# ---------------------------------------------------------------------------
class TestExtractionMath:
    """Unit tests for the pure extraction calculation engine."""

    def _form(self, **overrides):
        base = {
            "strain": "A. cubensis",
            "dry_lot_number": "DMP-2024-007",
            "solvent_type": "100%",
            "dried_weight_available": "240",
        }
        base.update(overrides)
        return base

    def test_100pct_meoh_math(self):
        cfg = load_config()
        r = calculate_extraction(cfg, self._form())
        # 2 bottles of 120g = 240g processed, 0g unused
        assert r["bottles_count"] == 2
        assert r["processed_weight"] == 240
        assert r["unused_weight"] == 0
        # 100% MeOH: take out 100 (2500 - 2400), put back 0
        assert r["single_bottle_meoh_remove_ml"] == 100.0
        assert r["single_bottle_water_add_ml"] == 0.0
        # Totals scale 2x
        assert r["total_meoh_remove_ml"] == 200.0
        assert r["total_water_add_ml"] == 0.0
        assert r["total_bottles_needed"] == 2
        # Yield: 240 / 4.0 = 60g
        assert r["expected_output_g"] == 60.0
        # Resuspension water: driven by config
        res_per_bottle = cfg["extraction_parameters"][
            "resuspension_water_per_bottle_ml"
        ]
        assert r["total_resuspension_water_ml"] == 2 * res_per_bottle

    def test_90pct_meoh_math(self):
        cfg = load_config()
        r = calculate_extraction(cfg, self._form(solvent_type="90%"))
        # 90% MeOH: take out 340, put back 240
        #   MeOH   = 2500 - (2400 x 0.90)       = 340
        #   water  = 2400 x (1 - 0.90)          = 240
        assert r["single_bottle_meoh_remove_ml"] == 340.0
        assert r["single_bottle_water_add_ml"] == 240.0

    def test_75pct_meoh_math(self):
        cfg = load_config()
        r = calculate_extraction(cfg, self._form(solvent_type="75%"))
        # 75% MeOH: take out 700, put back 600
        assert r["single_bottle_meoh_remove_ml"] == 700.0
        assert r["single_bottle_water_add_ml"] == 600.0

    def test_per_bottle_rule_is_config_driven(self):
        """The per-bottle volumes come from config.yaml, not from code."""
        cfg = load_config()
        params = cfg["extraction_parameters"]
        working = params["solvent_volume_per_bottle_ml"]
        stock = params.get("stock_bottle_volume_ml", 2500)
        for solvent in ("100%", "90%", "75%"):
            pct = float(solvent.rstrip("%"))
            r = calculate_extraction(cfg, self._form(solvent_type=solvent))
            assert r["single_bottle_water_add_ml"] == pytest.approx(
                working * (1 - pct / 100.0)
            )
            assert r["single_bottle_meoh_remove_ml"] == pytest.approx(
                stock - working * (pct / 100.0)
            )
            # The prepared volume is the working volume, at the right strength
            assert stock - r["single_bottle_meoh_remove_ml"] \
                + r["single_bottle_water_add_ml"] == pytest.approx(working)

    def test_partial_bottle_below_minimum_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_extraction(cfg, self._form(dried_weight_available="100"))

    def test_partial_bottle_rounds_down(self):
        cfg = load_config()
        # 250g = 2 full bottles (240g) + 10g unused
        r = calculate_extraction(cfg, self._form(dried_weight_available="250"))
        assert r["bottles_count"] == 2
        assert r["processed_weight"] == 240
        assert r["unused_weight"] == 10

    def test_invalid_solvent_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_extraction(cfg, self._form(solvent_type="50%"))


class TestExtractionPlan:
    """
    V5.0 portion-based extraction planner.

    The unit of production is one whole 120 g portion per bottle. The
    reference case is 400 g at 2 bottles per day: day 1 loads 2 bottles
    (240 g), the second extraction day loads 1 bottle (120 g) and 40 g is
    left over and held.
    """

    def _form(self, **overrides):
        base = {
            "production_lot_number": "LOT-2024-007",
            "strain": "P. cubensis",
            "solvent_type": "100%",
            "dried_weight_total_g": "400",
            "daily_bottle_count": "2",
            "grinder_loss_per_round_g": "10",
            "yield_override_factor": "",  # blank = use default 1:4
        }
        base.update(overrides)
        return base

    # -- the reference case -------------------------------------------------
    def test_reference_case_400g(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        # 400 / 120 = 3 full portions, 40 g left over
        assert r["total_bottles"] == 3
        assert r["processed_weight_g"] == 360.0
        assert r["leftover_weight_g"] == 40.0

    def test_reference_case_day_allocation(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        extraction_rows = [d for d in r["timeline"] if d["bottles"] > 0]
        assert len(extraction_rows) == 2
        # First extraction day: 2 bottles = 240 g
        assert extraction_rows[0]["bottles"] == 2
        assert extraction_rows[0]["mushroom_g"] == 240.0
        # Second extraction day: 1 bottle = 120 g
        assert extraction_rows[1]["bottles"] == 1
        assert extraction_rows[1]["mushroom_g"] == 120.0

    def test_leftover_is_never_extracted(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        loaded = sum(d["mushroom_g"] for d in r["timeline"])
        assert loaded == 360.0
        assert loaded + r["leftover_weight_g"] == 400.0

    # -- the per-bottle solvent protocol ------------------------------------
    def test_per_bottle_solvent_at_90pct(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(solvent_type="90%"))
        pb = r["per_bottle"]
        # 90% MeOH on a 2.5 L stock bottle: take out 340 mL, put back 240 mL
        assert pb["meoh_remove_ml"] == 340.0
        assert pb["water_add_ml"] == 240.0
        assert pb["mushroom_g"] == 120.0
        assert pb["stock_solvent_ml"] == 2400.0
        assert pb["stock_bottle_volume_ml"] == 2500.0

    def test_per_bottle_solvent_at_100pct_and_75pct(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r100 = calculate_extraction_plan(cfg, self._form(solvent_type="100%"))
        assert r100["per_bottle"]["meoh_remove_ml"] == 100.0
        assert r100["per_bottle"]["water_add_ml"] == 0.0
        r75 = calculate_extraction_plan(cfg, self._form(solvent_type="75%"))
        assert r75["per_bottle"]["meoh_remove_ml"] == 700.0
        assert r75["per_bottle"]["water_add_ml"] == 600.0

    def test_no_step_list_is_stored(self):
        """V5.1: the per-bottle section states the formula, so no step
        list is carried in the payload any more."""
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(solvent_type="90%"))
        assert "solvent_protocol" not in r

    def test_final_volume_returns_to_working_volume(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(solvent_type="90%"))
        pb = r["per_bottle"]
        # 2500 stock - 340 taken out + 240 put back = 2400 mL working volume
        assert pb["final_liquid_ml"] == 2400.0
        assert pb["final_working_volume_ml"] == 2400.0
        # 120 g of mushroom in 2400 mL of solvent = 1:20
        assert pb["ratio_label"] == "1:20"

    # -- lot totals ---------------------------------------------------------
    def test_lot_totals_scale_the_per_bottle_values(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(solvent_type="90%"))
        totals = r["totals"]
        assert totals["bottles"] == 3
        assert totals["meoh_remove_ml"] == 3 * 340.0
        assert totals["water_add_ml"] == 3 * 240.0
        assert totals["stock_solvent_ml"] == 3 * 2400.0
        # 7200 mL of stock drawn from 2500 mL bottles -> 3 bottles on the bench
        assert totals["stock_bottles_required"] == 3

    def test_expected_extract_uses_processed_weight(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        # 360 g processed / 4 (default 1:4) = 90 g
        assert r["yield_factor"] == 4.0
        assert r["total_expected_extract_g"] == 90.0
        assert r["totals"]["expected_extract_g"] == 90.0

    def test_default_yield_factor_is_4(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        assert r["yield_factor"] == 4.0

    def test_yield_override(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        # Historical data: 5x concentration (instead of default 4x)
        r = calculate_extraction_plan(
            cfg, self._form(yield_override_factor="5.0")
        )
        # 360 / 5 = 72g total
        assert r["yield_factor"] == 5.0
        assert r["yield_override_used"] is True
        assert r["total_expected_extract_g"] == 72.0

    # -- timeline -----------------------------------------------------------
    def test_total_days_includes_evaporation(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        # 2 extraction days, 2 calendar days per round (extraction +
        # evaporation) -> 4 calendar days
        assert r["extraction_days"] == 2
        assert r["total_procedure_days"] == 4
        assert len(r["timeline"]) == 4
        assert [d["calendar_day"] for d in r["timeline"]] == [1, 2, 3, 4]
        assert [d["phase"] for d in r["timeline"]] == [
            "Extraction", "Evaporation", "Extraction", "Evaporation"
        ]

    def test_day_rows_carry_per_bottle_and_day_totals(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(solvent_type="90%"))
        day1 = r["timeline"][0]
        assert day1["per_bottle_meoh_remove_ml"] == 340.0
        assert day1["per_bottle_water_add_ml"] == 240.0
        assert day1["meoh_remove_ml"] == 680.0   # 2 bottles
        assert day1["water_add_ml"] == 480.0    # 2 bottles
        assert day1["solvent_ml"] == 4800.0     # 2 x 2400
        day3 = r["timeline"][2]
        assert day3["meoh_remove_ml"] == 340.0   # 1 bottle
        assert day3["water_add_ml"] == 240.0    # 1 bottle

    def test_evaporation_row_consumes_nothing(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        evap = r["timeline"][1]
        assert evap["bottles"] == 0
        assert evap["mushroom_g"] == 0
        assert evap["meoh_remove_ml"] == 0.0
        assert evap["water_add_ml"] == 0.0
        assert evap["expected_extract_g"] == 0.0

    def test_grinder_loss_budget_per_round(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        # 10g/round budgeted; 2 rounds = 20g total
        assert r["timeline"][0]["grinder_loss_budget_g"] == 10
        assert r["total_grinder_loss_budget_g"] == 20

    # -- guards -------------------------------------------------------------
    def test_lot_below_one_portion_raises(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_extraction_plan(
                cfg, self._form(dried_weight_total_g="100")
            )

    def test_exactly_one_portion_is_allowed(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(dried_weight_total_g="120"))
        assert r["total_bottles"] == 1
        assert r["leftover_weight_g"] == 0.0

    def test_pack_size_and_dry_lot_are_no_longer_inputs(self):
        """V5.0 removed both fields; passing them must not change anything."""
        from app import calculate_extraction_plan
        cfg = load_config()
        plain = calculate_extraction_plan(cfg, self._form())
        noisy = calculate_extraction_plan(
            cfg, self._form(pack_size_g="50", dry_lot_number="DMP-007")
        )
        assert plain["total_bottles"] == noisy["total_bottles"]
        assert plain["totals"] == noisy["totals"]

    def test_daily_bottle_count_larger_than_lot(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form(daily_bottle_count="10"))
        # 3 bottles but only 1 round, so one extraction day with 3 bottles
        assert r["extraction_days"] == 1
        assert r["total_procedure_days"] == 2
        assert r["timeline"][0]["bottles"] == 3

    def test_payload_is_versioned(self):
        from app import calculate_extraction_plan
        cfg = load_config()
        r = calculate_extraction_plan(cfg, self._form())
        assert r["schema_version"] == "6"


class TestCapsulation:
    """TPE/psilocybin capsulation math."""

    def _form(self, **overrides):
        base = {
            "production_lot_number": "LOT-2024-007",
            "grams_of_extract": "100",
            "psilocybin_pct": "1.2",
            "psilocin_pct": "0.4",
            "anti_caking_pct": "5.0",
            "anti_caking_agent_name": "fumed silica",
            "dosage_mode": "psilocybin_only",
            "source_ex_doc_numbers": "EX-20260609-001, EX-20260610-002",
        }
        base.update(overrides)
        return base

    def test_mw_ratio(self):
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form())
        # 284.25 / 204.27 = 1.3914 (rounded to 4 dp)
        assert abs(r["mw_ratio"] - 1.3914) < 0.001

    def test_tpe_pct_calculation(self):
        """TPE% = psilocybin% + psilocin% * 1.39"""
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form())
        # 1.2 + 0.4 * 1.3914 = 1.7566
        assert abs(r["tpe_pct"] - (1.2 + 0.4 * 1.3914)) < 0.001

    def test_psilocybin_only_mode_uses_psilocybin_mass(self):
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form(dosage_mode="psilocybin_only"))
        # 100g * 1.2% = 1.2g psilocybin = 1200 mg
        assert r["active_mg"] == 1200.0
        assert r["active_label"] == "psilocybin"

    def test_tpe_mode_uses_combined_mass(self):
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form(dosage_mode="tpe"))
        # 100g * (1.2 + 0.4*1.3914)% = 100 * 1.7566% = 1.7566g = 1756.6 mg
        assert abs(r["active_mg"] - 1756.6) < 0.5
        assert "TPE" in r["active_label"]

    def test_anti_caking_5pct_of_blend(self):
        """5% of BLEND mass means:
            anti_caking = extract * 5 / 95
            blend = extract + anti_caking
        """
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form(grams_of_extract="100"))
        # anti_caking = 100 * 5/95 = 5.2632g
        assert abs(r["anti_caking_g"] - 5.2632) < 0.01
        # blend = 105.2632g
        assert abs(r["blend_g"] - 105.2632) < 0.01
        # anti_caking as % of extract = 5.2632%
        assert abs(r["anti_caking_of_extract_pct"] - 5.2632) < 0.01

    def test_capsule_table_three_types(self):
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form())
        assert len(r["capsule_table"]) == 3
        types = [c["type"] for c in r["capsule_table"]]
        assert types == ["0", "1", "2"]
        doses = [c["target_dose_mg"] for c in r["capsule_table"]]
        assert doses == [10, 5, 2]

    def test_capsule_blend_per_capsule_scales_with_dose(self):
        """Higher dose -> more blend needed per capsule, ratio 5:1 between type 0 and type 2."""
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form())
        table = {c["type"]: c for c in r["capsule_table"]}
        # 10mg type 0 should need exactly 4x as much blend as 2.5mg (close enough)
        # and exactly 2x as much as 5mg type 1
        assert abs(table["0"]["blend_per_capsule_mg"]
                   / table["2"]["blend_per_capsule_mg"] - 5.0) < 0.01
        assert abs(table["0"]["blend_per_capsule_mg"]
                   / table["1"]["blend_per_capsule_mg"] - 2.0) < 0.01

    def test_capsule_max_counts(self):
        """Sanity: 100g extract @ 1.2% psilocybin = 1.2g active.
        With 5% anti-caking -> 105.26g blend.
        At 10mg/capsule -> 1.2g * 1000 / 10 = 120 capsules max (type 0).
        """
        from app import calculate_capsulation
        cfg = load_config()
        r = calculate_capsulation(cfg, self._form(dosage_mode="psilocybin_only"))
        table = {c["type"]: c for c in r["capsule_table"]}
        # Each blend capsule of type 0 carries 10mg psilocybin.
        # Total psilocybin = 1.2g = 1200mg / 10 = 120 caps max.
        # Plus a small adjustment for anti-caking dilution (we have a bit
        # more blend so max may be slightly higher).
        assert 120 <= table["0"]["max_capsules_from_lot"] <= 122

    def test_source_ex_required(self):
        """Cross-tab traceability: an EX source must be recorded."""
        from app import calculate_capsulation
        cfg = load_config()
        with pytest.raises(ValueError, match="source EX doc number"):
            calculate_capsulation(
                cfg, self._form(source_ex_doc_numbers="")
            )

    def test_invalid_dosage_mode(self):
        from app import calculate_capsulation
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_capsulation(
                cfg, self._form(dosage_mode="invalid")
            )

    def test_negative_extract_raises(self):
        from app import calculate_capsulation
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_capsulation(
                cfg, self._form(grams_of_extract="0")
            )

    def test_anti_caking_above_100_raises(self):
        from app import calculate_capsulation
        cfg = load_config()
        with pytest.raises(ValueError):
            calculate_capsulation(
                cfg, self._form(anti_caking_pct="150")
            )


# ---------------------------------------------------------------------------
# TestSequenceGeneration
# ---------------------------------------------------------------------------
class TestSequenceGeneration:
    def test_first_doc_of_day_format(self, test_app):
        with test_app.app_context():
            doc = generate_sequence_number("WP")
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            assert doc == f"WP-{today}-001"

    def test_sequence_increments_after_log(self, test_app):
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            db.session.add(BatchLog(
                doc_number=generate_sequence_number("WP"),
                batch_type="cultivation",
                user_id=tech.id,
                strain="x", lot_number="y",
                payload_data="{}",
            ))
            db.session.commit()
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            assert generate_sequence_number("WP") == f"WP-{today}-002"

    def test_sequences_independent_by_prefix(self, test_app):
        with test_app.app_context():
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            assert generate_sequence_number("WP").endswith("-001")
            assert generate_sequence_number("EX") == f"EX-{today}-001"

    def test_cp_prefix_supported(self, test_app):
        with test_app.app_context():
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            assert generate_sequence_number("CP") == f"CP-{today}-001"


# ---------------------------------------------------------------------------
# TestCapsulationIntegration
# ---------------------------------------------------------------------------
class TestCapsulationIntegration:
    """HTTP-level tests for the /capsulation route."""

    def _login_and_post(self, client, **overrides):
        _login(client, "tech1", "techpass")
        base = {
            "production_lot_number": "LOT-INT-001",
            "grams_of_extract": "100",
            "psilocybin_pct": "1.2",
            "psilocin_pct": "0.4",
            "anti_caking_pct": "5.0",
            "anti_caking_agent_name": "fumed silica",
            "dosage_mode": "tpe",
            "source_ex_doc_numbers": "EX-20260609-001, EX-20260610-002",
        }
        base.update(overrides)
        return client.post("/capsulation", data=base)

    def test_get_form_renders(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/capsulation")
        assert r.status_code == 200
        assert b"Capsulation Production Record" in r.data
        assert b"dosage_mode" in r.data

    def test_login_required(self, client):
        r = client.get("/capsulation")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_post_returns_doc_number(self, client):
        r = self._login_and_post(client)
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "CP-" in html
        # The TIN records extract weight and labels
        assert "1.7566" in html or "1.76" in html  # TPE pct

    def test_persists_with_cp_prefix(self, client, test_app):
        self._login_and_post(client)
        with test_app.app_context():
            rows = db.session.query(BatchLog).filter_by(batch_type="capsulation").all()
            assert len(rows) == 1
            assert rows[0].doc_number.startswith("CP-")
            assert rows[0].lot_number == "LOT-INT-001"

    def test_form_data_captured(self, client, test_app):
        import re
        r = self._login_and_post(client)
        doc = re.search(r"CP-(\d{8})-(\d{3})", r.data.decode("utf-8")).group(0)
        with test_app.app_context():
            entry = db.session.query(BatchLog).filter_by(doc_number=doc).first()
            form = entry.to_form()
            assert form["psilocybin_pct"] == "1.2"
            assert form["psilocin_pct"] == "0.4"
            assert form["dosage_mode"] == "tpe"
            assert "EX-20260609-001" in form["source_ex_doc_numbers"]

    def test_missing_source_ex_rejected(self, client):
        r = self._login_and_post(client, source_ex_doc_numbers="")
        assert r.status_code == 200
        # The error is flashed and redirected back to form (no doc rendered)
        assert b"CP-" not in r.data

    def test_admin_history_includes_capsulation(self, client, test_app):
        self._login_and_post(client)
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/history")
        assert r.status_code == 200
        assert b"capsulation" in r.data
        assert b"CP-" in r.data

    def test_capsulation_admin_view(self, client, test_app):
        import re
        r = self._login_and_post(client)
        doc = re.search(r"CP-(\d{8})-(\d{3})", r.data.decode("utf-8")).group(0)
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/view/{doc}")
        assert r.status_code == 200
        assert b"tech1" in r.data
        assert b"Original Form Inputs" in r.data

    def test_capsulation_doc_has_no_gmp_verification_form(self, client):
        """
        v3.1: calculator/guideline only - the printed record must NOT
        include the on-paper 'GMP verification' fill-in lines. Those
        belong on the actual GMP form, not on the calculator output.
        """
        r = self._login_and_post(client)
        html = r.data.decode("utf-8")
        assert "GMP VERIFICATION" not in html
        assert "RECORD AT EXECUTION" not in html
        assert "Anti-Caking Agent Lot #:" not in html
        assert "Encapsulator Equipment ID:" not in html

    def test_capsulation_reprint_works(self, client, test_app):
        """
        v3.1.1: capsulation records must be reprintable from the admin
        master log. Previously the reprint route returned 400 for any
        non-(cultivation,extraction) batch type.
        """
        import re
        r = self._login_and_post(client)
        doc = re.search(r"CP-(\d{8})-(\d{3})", r.data.decode("utf-8")).group(0)
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200, (
            f"reprint must not return 400; got {r.status_code}"
        )
        html = r.data.decode("utf-8")
        # The REPRINT banner is present
        assert "REPRINT" in html
        assert doc in html
        # And a back link
        assert "Back to" in html

    def test_capsulation_reprint_uses_stored_form(self, client, test_app):
        """Re-derive capsulation result from stored form inputs."""
        import re
        r = self._login_and_post(client)
        doc = re.search(r"CP-(\d{8})-(\d{3})", r.data.decode("utf-8")).group(0)
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # Anti-caking field is visible
        assert "Anti-Caking" in html
        # TPE-related text from the v3 result is present
        assert "TPE" in html or "tpe" in html
        # No new ledger row was created
        with test_app.app_context():
            assert (
                db.session.query(BatchLog).filter_by(doc_number=doc).count()
                == 1
            )

    def test_extraction_reprint_re_uses_extraction_plan(self, client, test_app):
        """
        v3.1.1 / V5.0: the extraction reprint route must reproduce the
        stored V5 document - per-bottle solvent protocol, portion totals
        and the calendar timeline - and must not create a new ledger row.
        """
        import re
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-REPRO",
            "strain": "P. cubensis",
            "solvent_type": "75%",
            "dried_weight_total_g": "550",
            "daily_bottle_count": "2",
            "grinder_loss_per_round_g": "10",
            "yield_override_factor": "5.0",
        })
        doc = re.search(r"EX-(\d{8})-(\d{3})", r.data.decode("utf-8")).group(0)
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # 550 g -> 4 whole portions = 480 g processed; /5.0 = 96g extract
        assert "96" in html, "extraction reprint must show the issued yield"
        # V5.0 sections
        assert "Extraction Days" in html
        assert "SOLVENT PREPARATION" in html
        assert "BOTTLES &amp; REAGENT TOTALS" in html
        assert "TIMELINE" in html
        # A reprint must not add a ledger row
        with test_app.app_context():
            rows = db.session.query(BatchLog).filter_by(
                batch_type="extraction"
            ).all()
            assert len(rows) == 1


class TestReprintBanner:
    """All three result templates must render the REPRINT banner when
    invoked via the admin reprint route, with a back link to the audit
    view of the same record."""

    def _login_and_post(self, client, **overrides):
        _login(client, "tech1", "techpass")
        base = {
            "production_lot_number": "LOT-BANNER",
            "grams_of_extract": "100",
            "psilocybin_pct": "1.2",
            "psilocin_pct": "0.4",
            "anti_caking_pct": "5.0",
            "anti_caking_agent_name": "fumed silica",
            "dosage_mode": "tpe",
            "source_ex_doc_numbers": "EX-1",
        }
        base.update(overrides)
        return client.post("/capsulation", data=base)

    def _extract_doc(self, html, prefix):
        import re
        m = re.search(rf"{prefix}-(\d{{8}})-(\d{{3}})", html)
        assert m, f"no {prefix}-NNNNNNNN-NNN token in response"
        return f"{prefix}-{m.group(1)}-{m.group(2)}"

    def test_capsulation_reprint_renders_banner(self, client):
        r = self._login_and_post(client)
        doc = self._extract_doc(r.data.decode("utf-8"), "CP")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "REPRINT" in html
        assert "NOT A NEW DOCUMENT" in html
        assert "No new ledger entry has been created" in html
        # Back link
        assert "Back to" in html
        # Links to the audit view of THIS record
        assert doc in html
        # And operator stamp
        assert "tech1" in html

    def test_extraction_reprint_renders_banner(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-BANNER-EX",
            "strain": "P. cubensis", "dry_lot_number": "DMP",
            "solvent_type": "100%",
            "dried_weight_total_g": "550", "pack_size_g": "50",
            "daily_bottle_count": "2", "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "EX")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "REPRINT" in html
        assert "NOT A NEW DOCUMENT" in html
        assert doc in html

    def test_cultivation_reprint_still_renders_banner(self, client):
        """Regression: banner was working for cultivation; keep it that way."""
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "P", "cult_lot": "CL-B",
            "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "WP")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "REPRINT" in html
        assert "NOT A NEW DOCUMENT" in html
        assert doc in html

    def test_reprint_banner_hidden_on_fresh_issuance(self, client):
        """
        The banner must only appear on REPRINTS, not on freshly
        generated records - otherwise a brand-new doc would be
        mislabeled as a reproduction.
        """
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "P", "cult_lot": "CL-FRESH",
            "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        })
        # Fresh issuance path - no 'reprint' flag set
        assert "REPRINT" not in r.data.decode("utf-8")
        assert "NOT A NEW DOCUMENT" not in r.data.decode("utf-8")

    def test_extraction_doc_has_no_gmp_verification_form(self, client):
        """
        v3.1: calculator/guideline only - the printed record must NOT
        include the on-paper 'GMP verification' fill-in lines.
        """
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-NOFILL",
            "strain": "P. cubensis", "dry_lot_number": "DMP",
            "solvent_type": "100%",
            "dried_weight_total_g": "550", "pack_size_g": "50",
            "daily_bottle_count": "2", "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        html = r.data.decode("utf-8")
        assert "GMP VERIFICATION" not in html
        assert "RECORD AT EXECUTION" not in html
        assert "Methanol Stock Solvent Lot:" not in html
        assert "Rotary Evaporator Equipment ID:" not in html


# ---------------------------------------------------------------------------
# TestFlaskIntegration
# ---------------------------------------------------------------------------
class TestFlaskIntegration:
    def test_login_required_redirects(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_login_then_cultivation(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "A. cubensis",
            "cult_lot": "CL-2024-001",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        })
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # Doc number badge in the page
        assert "WP-" in html
        # Computed values appear
        assert "32" in html  # total_containers
        assert "10" in html  # substrate buckets
        assert "3" in html   # grain batches

    def test_cultivation_persists_ledger_row(self, client, test_app):
        _login(client, "tech1", "techpass")
        client.post("/", data={
            "strain": "A. cubensis",
            "cult_lot": "CL-2024-001",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        })
        with test_app.app_context():
            rows = db.session.query(BatchLog).all()
            assert len(rows) == 1
            assert rows[0].batch_type == "cultivation"
            assert rows[0].doc_number.startswith("WP-")
            assert rows[0].strain == "A. cubensis"

    def test_login_then_extraction(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-2024-007",
            "strain": "P. cubensis",
            "dry_lot_number": "DMP-007",
            "solvent_type": "100%",
            "dried_weight_total_g": "550",
            "pack_size_g": "50",
            "daily_bottle_count": "2",
            "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "EX-" in html
        # 2 full days expected
        assert "2" in html

    def test_extraction_persists_ledger_row(self, client, test_app):
        _login(client, "tech1", "techpass")
        client.post("/extraction", data={
            "production_lot_number": "LOT-PERSIST",
            "strain": "P. cubensis",
            "dry_lot_number": "DMP-007",
            "solvent_type": "90%",
            "dried_weight_total_g": "550",
            "pack_size_g": "50",
            "daily_bottle_count": "2",
            "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        with test_app.app_context():
            rows = db.session.query(BatchLog).filter_by(batch_type="extraction").all()
            assert len(rows) == 1
            assert rows[0].doc_number.startswith("EX-")

    def test_extraction_rejects_below_minimum(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "L", "strain": "x",
            "solvent_type": "100%",
            "dried_weight_total_g": "100",
            "daily_bottle_count": "2",
            "grinder_loss_per_round_g": "0",
        }, follow_redirects=True)
        assert r.status_code == 200
        # V5.0: a lot must hold at least one whole 120 g portion
        assert b"120" in r.data

    def test_admin_history_403_for_technician(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/admin/history")
        assert r.status_code == 403

    def test_admin_history_visible_to_admin(self, client, test_app):
        # Generate a record first
        _login(client, "tech1", "techpass")
        client.post("/extraction", data={
            "production_lot_number": "LOT-VISIBLE",
            "strain": "x", "dry_lot_number": "y",
            "solvent_type": "100%",
            "dried_weight_total_g": "550", "pack_size_g": "50",
            "daily_bottle_count": "2", "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        # Logout, log in as admin
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/history")
        assert r.status_code == 200
        assert b"EX-" in r.data
        # The history page now stamps the operator username on each row
        assert b"tech1" in r.data

    def test_history_view_shows_operator(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-OPS-001",
            "strain": "P. cubensis",
            "dry_lot_number": "DMP-2024-007",
            "solvent_type": "100%",
            "dried_weight_total_g": "550",
            "pack_size_g": "50",
            "daily_bottle_count": "2",
            "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        # Extract the generated doc number from the page
        html = r.data.decode("utf-8")
        import re
        m = re.search(r"EX-(\d{8})-(\d{3})", html)
        assert m
        doc = f"EX-{m.group(1)}-{m.group(2)}"
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/view/{doc}")
        assert r.status_code == 200
        assert b"tech1" in r.data


# ---------------------------------------------------------------------------
# TestFormCaptureAndReprint
# ---------------------------------------------------------------------------
class TestFormCaptureAndReprint:
    """Tests for v2: raw form capture + original-protocol reprint."""

    @staticmethod
    def _extract_doc(html, prefix):
        import re
        m = re.search(rf"{prefix}-(\d{{8}})-(\d{{3}})", html)
        assert m, f"no {prefix}-NNNNNNNN-NNN token in response"
        return f"{prefix}-{m.group(1)}-{m.group(2)}"

    def test_cultivation_captures_form_data(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "P. cubensis",
            "cult_lot": "CL-CAPTURE-001",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "WP")
        with test_app.app_context():
            entry = db.session.query(BatchLog).filter_by(doc_number=doc).first()
            assert entry is not None
            form = entry.to_form()
            assert form is not None
            assert form["strain"] == "P. cubensis"
            assert form["cult_lot"] == "CL-CAPTURE-001"
            assert form["container_type"] == "monotub"
            assert form["avg_yield_fresh"] == "500"
            assert form["target_dry_weight"] == "1000"

    def test_extraction_captures_form_data(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-CAPTURE-007",
            "strain": "P. cubensis",
            "solvent_type": "90%",
            "dried_weight_total_g": "550",
            "daily_bottle_count": "2",
            "grinder_loss_per_round_g": "10",
            "yield_override_factor": "",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "EX")
        with test_app.app_context():
            entry = db.session.query(BatchLog).filter_by(doc_number=doc).first()
            form = entry.to_form()
            assert form is not None
            assert form["solvent_type"] == "90%"
            assert form["dried_weight_total_g"] == "550"
            assert form["production_lot_number"] == "LOT-CAPTURE-007"
            # V5.0 removed the dry-lot and pack-size inputs
            assert "dry_lot_number" not in form
            assert "pack_size_g" not in form
            # The stored payload is the issued document and is versioned
            payload = entry.to_payload()
            assert payload["schema_version"] == "6"
            assert payload["totals"]["bottles"] == 4

    def test_history_view_shows_original_form_inputs(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "P. cubensis",
            "cult_lot": "CL-VISIBLE-001",
            "container_type": "4liter_box",
            "avg_yield_fresh": "400",
            "target_dry_weight": "500",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "WP")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/view/{doc}")
        html = r.data.decode("utf-8")
        assert "Original Form Inputs" in html
        assert "P. cubensis" in html
        assert "CL-VISIBLE-001" in html
        assert "4liter_box" in html

    def test_legacy_row_missing_form_data(self, client, test_app):
        """
        A legacy row with form_data=NULL must render a graceful fallback
        message, not a 500.
        """
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            db.session.add(BatchLog(
                doc_number="WP-LEGACY-001",
                batch_type="cultivation",
                user_id=tech.id,
                strain="x", lot_number="y",
                payload_data='{"total_containers": 32}',
                form_data=None,
            ))
            db.session.commit()
        r = client.get("/admin/history/view/WP-LEGACY-001")
        assert r.status_code == 200
        assert b"Inputs not captured" in r.data

    def test_reprint_cultivation_uses_stored_form(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "P. cubensis",
            "cult_lot": "CL-REPRINT-001",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "WP")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        # The reprint banner
        assert "REPRINT" in html
        # The original doc number
        assert doc in html
        # The values were re-derived from the stored form, so the protocol
        # numbers should be present
        assert "32" in html  # total_containers
        # And no NEW ledger row was created
        with test_app.app_context():
            assert db.session.query(BatchLog).filter_by(doc_number=doc).count() == 1

    def test_reprint_extraction_uses_stored_form(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/extraction", data={
            "production_lot_number": "LOT-REPRINT",
            "strain": "P. cubensis",
            "dry_lot_number": "DMP-REPRINT-007",
            "solvent_type": "75%",
            "dried_weight_total_g": "550",
            "pack_size_g": "50",
            "daily_bottle_count": "2",
            "grams_per_bottle": "120",
            "grinder_loss_per_round_g": "10",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "EX")
        client.get("/logout")
        _login(client, "admin", "secureadminpassword")
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        html = r.data.decode("utf-8")
        assert "REPRINT" in html
        assert "75%" in html  # solvent type
        # V5.1 per-bottle rule at 75%: take out 700 mL per bottle;
        # the first extraction day loads 2 bottles.
        assert "700" in html
        assert "1400" in html  # 2 bottles * 700 mL MeOH take-out on day 1

    def test_reprint_403_for_technician(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data={
            "strain": "x", "cult_lot": "y",
            "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        })
        doc = self._extract_doc(r.data.decode("utf-8"), "WP")
        # tech1 should NOT be able to reprint
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 403

    def test_reprint_404_for_missing(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/history/reprint/WP-NOPE-001")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# TestUserManagement
# ---------------------------------------------------------------------------
class TestUserManagement:
    def test_403_for_technician(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/admin/users")
        assert r.status_code == 403

    def test_admin_can_list_users(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/users")
        assert r.status_code == 200
        assert b"admin" in r.data
        assert b"tech1" in r.data

    def test_admin_can_create_user(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        r = client.post("/admin/users", data={
            "action": "create",
            "username": "newuser",
            "password": "secret123",
            "role": "technician",
        })
        assert r.status_code == 302  # redirect
        with test_app.app_context():
            u = db.session.query(User).filter_by(username="newuser").first()
            assert u is not None
            assert u.role == "technician"
            assert u.check_password("secret123")

    def test_create_user_duplicate_rejected(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        r = client.post("/admin/users", data={
            "action": "create",
            "username": "tech1",  # already exists
            "password": "x123456",
            "role": "technician",
        }, follow_redirects=True)
        assert r.status_code == 200
        assert b"already exists" in r.data

    def test_admin_can_reset_password(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            tech_id = tech.id
        r = client.post("/admin/users", data={
            "action": "reset_password",
            "user_id": str(tech_id),
            "password": "newpass99",
        })
        assert r.status_code == 302
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            assert tech.check_password("newpass99")
            assert not tech.check_password("techpass")

    def test_admin_can_change_role(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            tech_id = tech.id
        r = client.post("/admin/users", data={
            "action": "change_role",
            "user_id": str(tech_id),
            "role": "admin",
        })
        assert r.status_code == 302
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            assert tech.role == "admin"

    def test_cannot_demote_last_admin(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            admin = db.session.query(User).filter_by(username="admin").first()
            admin_id = admin.id
        r = client.post("/admin/users", data={
            "action": "change_role",
            "user_id": str(admin_id),
            "role": "technician",
        }, follow_redirects=True)
        assert b"last remaining admin" in r.data
        with test_app.app_context():
            admin = db.session.query(User).filter_by(username="admin").first()
            assert admin.role == "admin"

    def test_admin_can_delete_technician(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            tech_id = tech.id
        r = client.post("/admin/users", data={
            "action": "delete",
            "user_id": str(tech_id),
        })
        assert r.status_code == 302
        with test_app.app_context():
            assert db.session.query(User).filter_by(username="tech1").first() is None

    def test_cannot_delete_self(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        with test_app.app_context():
            admin = db.session.query(User).filter_by(username="admin").first()
            admin_id = admin.id
        r = client.post("/admin/users", data={
            "action": "delete",
            "user_id": str(admin_id),
        }, follow_redirects=True)
        assert b"cannot delete your own account" in r.data


# ---------------------------------------------------------------------------
# TestSelfServicePassword
# ---------------------------------------------------------------------------
class TestSelfServicePassword:
    def test_get_form_renders(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/account/password")
        assert r.status_code == 200
        assert b"Current Password" in r.data

    def test_change_password_with_wrong_current(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/account/password", data={
            "current_password": "WRONG",
            "new_password": "freshpass1",
            "confirm_password": "freshpass1",
        }, follow_redirects=True)
        assert b"Current password is incorrect" in r.data
        # Original password still works
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            assert tech.check_password("techpass")

    def test_change_password_mismatch(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/account/password", data={
            "current_password": "techpass",
            "new_password": "freshpass1",
            "confirm_password": "DIFFERENT1",
        }, follow_redirects=True)
        assert b"do not match" in r.data

    def test_change_password_too_short(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/account/password", data={
            "current_password": "techpass",
            "new_password": "abc",
            "confirm_password": "abc",
        }, follow_redirects=True)
        assert b"at least 6 characters" in r.data

    def test_change_password_success(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/account/password", data={
            "current_password": "techpass",
            "new_password": "freshpass1",
            "confirm_password": "freshpass1",
        })
        assert r.status_code == 302
        with test_app.app_context():
            tech = db.session.query(User).filter_by(username="tech1").first()
            assert tech.check_password("freshpass1")
            assert not tech.check_password("techpass")

    def test_login_required(self, client):
        r = client.get("/account/password")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# TestConfigLoading
# ---------------------------------------------------------------------------
class TestConfigLoading:
    def test_config_loads_with_extraction_block(self):
        cfg = load_config()
        assert "extraction_parameters" in cfg
        assert cfg["extraction_parameters"]["grams_per_bottle"] == 120
        assert cfg["extraction_parameters"]["stock_bottle_volume_ml"] == 2500
        assert cfg["extraction_parameters"]["concentration_factor"] == 4.0


# ---------------------------------------------------------------------------
# TestConfigDrivenOptionsV2 (V2.0: admin adds options via config.yaml)
# ---------------------------------------------------------------------------
def _cultivation_form(**overrides):
    base = {
        "strain": "P. cubensis",
        "cult_lot": "CL-001",
        "container_type": "2liter_box",
        "avg_yield_fresh": "500",
        "target_dry_weight": "1000",
    }
    base.update(overrides)
    return base


def _extraction_plan_form(**overrides):
    base = {
        "production_lot_number": "LOT-2024-007",
        "strain": "P. cubensis",
        "solvent_type": "80%",
        "dried_weight_total_g": "550",
        "daily_bottle_count": "2",
        "grinder_loss_per_round_g": "10",
        "yield_override_factor": "",
    }
    base.update(overrides)
    return base


def _capsulation_form(**overrides):
    base = {
        "production_lot_number": "LOT-2024-007",
        "grams_of_extract": "100",
        "psilocybin_pct": "1.2",
        "psilocin_pct": "0.4",
        "anti_caking_pct": "5.0",
        "anti_caking_agent_name": "fumed silica",
        "dosage_mode": "psilocybin_only",
        "source_ex_doc_numbers": "EX-20260609-001",
    }
    base.update(overrides)
    return base


class TestConfigDrivenOptionsV2:
    """V2.0: options come from config.yaml, not from hardcoded lists."""

    def test_config_includes_2liter_box(self):
        cfg = load_config()
        assert "2liter_box" in cfg["containers"]
        for key in ("substrate", "spawn", "liquid_culture"):
            assert cfg["containers"]["2liter_box"][key] > 0

    def test_new_container_added_via_config_calculates(self):
        cfg = load_config()
        r = calculate_cultivation(cfg, _cultivation_form())
        assert r["container_type"] == "2liter_box"
        # Expected values come from config itself (admin may tune them).
        spec = cfg["containers"]["2liter_box"]
        assert r["total_substrate"] == r["total_containers"] * spec["substrate"]
        assert r["total_spawn"] == r["total_containers"] * spec["spawn"]

    def test_container_added_in_memory_without_code_change(self):
        cfg = load_config()
        cfg["containers"]["new_box"] = {
            "substrate": 1000, "spawn": 300, "liquid_culture": 8,
        }
        r = calculate_cultivation(
            cfg, _cultivation_form(container_type="new_box")
        )
        assert r["total_substrate"] == r["total_containers"] * 1000

    def test_validate_config_rejects_broken_container(self):
        from app import validate_config

        cfg = load_config()
        cfg["containers"]["broken_box"] = {"substrate": 100, "spawn": 0}
        with pytest.raises(ValueError, match=r"containers\.broken_box"):
            validate_config(cfg)

    def test_validate_config_rejects_empty_containers(self):
        from app import validate_config

        cfg = load_config()
        cfg["containers"] = {}
        with pytest.raises(ValueError, match="containers"):
            validate_config(cfg)

    def test_new_solvent_type_added_via_config(self):
        from app import calculate_extraction_plan

        cfg = load_config()
        cfg["extraction_parameters"]["allowed_solvent_types"] = ["80%"]
        r = calculate_extraction_plan(cfg, _extraction_plan_form())
        assert r["solvent_type"] == "80%"
        # 80%: MeOH  = 2500 - (2400 x 0.80) = 580 mL/bottle;
        #       water = 2400 x 0.20           = 480 mL/bottle
        # 550 g -> 4 whole portions (480 g processed)
        assert r["total_bottles"] == 4
        assert r["per_bottle"]["water_add_ml"] == 480.0
        assert r["per_bottle"]["meoh_remove_ml"] == 580.0
        assert r["total_meoh_remove_ml"] == 580 * 4
        assert r["total_water_add_ml"] == 480 * 4

    def test_solvent_not_in_config_is_rejected(self):
        from app import calculate_extraction_plan

        cfg = load_config()
        with pytest.raises(ValueError, match="Unsupported solvent_type"):
            calculate_extraction_plan(
                cfg, _extraction_plan_form(solvent_type="60%")
            )

    def test_new_capsule_spec_added_via_config(self):
        from app import calculate_capsulation

        cfg = load_config()
        cfg["capsulation_parameters"]["capsule_specs"] = [
            {"type": "3", "dose_mg": 1, "label": "Type 3 / 1mg dose"},
        ]
        r = calculate_capsulation(cfg, _capsulation_form())
        assert len(r["capsule_table"]) == 1
        assert r["capsule_table"][0]["type"] == "3"
        assert r["capsule_table"][0]["target_dose_mg"] == 1

    def test_configured_dosage_mode_without_engine_rule_fails_loudly(self):
        from app import calculate_capsulation

        cfg = load_config()
        cfg["capsulation_parameters"]["dosage_modes"] = [
            {"id": "custom_mode", "label": "Custom"},
        ]
        with pytest.raises(ValueError, match="no calculation rule"):
            calculate_capsulation(
                cfg, _capsulation_form(dosage_mode="custom_mode")
            )

    def test_v1_config_without_new_sections_still_works(self):
        """A V1 config.yaml (no capsulation/solvent sections) validates."""
        from app import validate_config

        cfg = load_config()
        del cfg["capsulation_parameters"]
        del cfg["extraction_parameters"]["allowed_solvent_types"]
        validate_config(cfg)  # must not raise — V1 defaults kick in

    def test_index_page_lists_all_configured_containers(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/")
        assert r.status_code == 200
        for name in load_config()["containers"]:
            assert f'value="{name}"'.encode() in r.data

    def test_extraction_page_lists_configured_solvents(self, client):
        from app import _allowed_solvent_types

        _login(client, "tech1", "techpass")
        r = client.get("/extraction")
        assert r.status_code == 200
        for solvent in _allowed_solvent_types(load_config()):
            assert f'value="{solvent}"'.encode() in r.data

    def test_capsulation_page_lists_configured_dosage_modes(self, client):
        from app import _capsulation_params

        _login(client, "tech1", "techpass")
        r = client.get("/capsulation")
        assert r.status_code == 200
        for mode in _capsulation_params(load_config())["dosage_modes"]:
            assert f'value="{mode["id"]}"'.encode() in r.data

    def test_post_with_2liter_box_persists(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        with test_app.app_context():
            rows = db.session.query(BatchLog).all()
            assert len(rows) == 1
            assert rows[0].batch_type == "cultivation"
            payload = rows[0].to_payload()
            assert payload["container_type"] == "2liter_box"


# ---------------------------------------------------------------------------
# TestSettingsConfigEditor (Settings page + GUI config.yaml editor)
# ---------------------------------------------------------------------------
def _tmp_config(monkeypatch, tmp_path):
    """Point the app at a throwaway copy of the real config.yaml."""
    import shutil

    import app as app_module

    src = load_config()  # noqa: F841 - ensures the real file is valid
    real_path = app_module._CONFIG_PATH
    tmp_file = tmp_path / "config.yaml"
    shutil.copy(real_path, tmp_file)
    monkeypatch.setattr(app_module, "_CONFIG_PATH", str(tmp_file))
    return tmp_file


class TestSettingsConfigEditor:
    def test_settings_page_renders_for_admin(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/settings")
        assert r.status_code == 200
        assert b"Settings" in r.data
        assert b'name="config_text"' in r.data
        assert b"User Management" in r.data

    def test_settings_403_for_technician(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/admin/settings")
        assert r.status_code == 403

    def test_legacy_users_url_still_works(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/users")
        assert r.status_code == 200
        assert b"Settings" in r.data

    def test_nav_label_is_settings_not_user_management(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/")
        assert r.status_code == 200
        assert b">Settings<" in r.data
        assert b"User Management</a>" not in r.data

    def test_save_config_valid_writes_file_backup_and_audit(
        self, client, test_app, monkeypatch, tmp_path
    ):
        tmp_file = _tmp_config(monkeypatch, tmp_path)
        _login(client, "admin", "secureadminpassword")
        new_text = tmp_file.read_text().replace(
            "contamination_buffer: 0.10", "contamination_buffer: 0.20"
        )
        r = client.post(
            "/admin/settings",
            data={"action": "save_config", "config_text": new_text},
        )
        assert r.status_code == 302
        assert "contamination_buffer: 0.20" in tmp_file.read_text()
        assert load_config()["contamination_buffer"] == 0.20
        backups = list((tmp_path / "config_backups").glob("*.bak"))
        assert len(backups) == 1
        with test_app.app_context():
            from app import ConfigChange

            rows = db.session.query(ConfigChange).all()
            assert len(rows) == 1
            assert rows[0].username == "admin"
            assert rows[0].backup_file == backups[0].name

    def test_save_config_invalid_yaml_leaves_file_untouched(
        self, client, monkeypatch, tmp_path
    ):
        tmp_file = _tmp_config(monkeypatch, tmp_path)
        original = tmp_file.read_text()
        _login(client, "admin", "secureadminpassword")
        r = client.post(
            "/admin/settings",
            data={
                "action": "save_config",
                "config_text": "containers: [unclosed,",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Config not saved" in r.data
        assert tmp_file.read_text() == original

    def test_save_config_invalid_values_leaves_file_untouched(
        self, client, monkeypatch, tmp_path
    ):
        tmp_file = _tmp_config(monkeypatch, tmp_path)
        original = tmp_file.read_text()
        _login(client, "admin", "secureadminpassword")
        broken = original.replace("spawn: 700", "spawn: 0")
        r = client.post(
            "/admin/settings",
            data={"action": "save_config", "config_text": broken},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Config not saved" in r.data
        assert b"containers.monotub.spawn" in r.data
        assert tmp_file.read_text() == original

    def test_save_config_403_for_technician(
        self, client, monkeypatch, tmp_path
    ):
        tmp_file = _tmp_config(monkeypatch, tmp_path)
        original = tmp_file.read_text()
        _login(client, "tech1", "techpass")
        r = client.post(
            "/admin/settings",
            data={
                "action": "save_config",
                "config_text": original.replace("0.10", "0.20"),
            },
        )
        assert r.status_code == 403
        assert tmp_file.read_text() == original

    def test_add_container_via_gui_end_to_end(
        self, client, monkeypatch, tmp_path
    ):
        tmp_file = _tmp_config(monkeypatch, tmp_path)
        _login(client, "admin", "secureadminpassword")
        new_text = tmp_file.read_text().replace(
            "  2liter_box:",
            "  gui_box:\n"
            "    substrate: 900\n"
            "    spawn: 300\n"
            "    liquid_culture: 7\n"
            "  2liter_box:",
        )
        r = client.post(
            "/admin/settings",
            data={"action": "save_config", "config_text": new_text},
        )
        assert r.status_code == 302
        # The calculator picks the new option up with no restart.
        r = client.get("/")
        assert r.status_code == 200
        assert b'value="gui_box"' in r.data


# ---------------------------------------------------------------------------
# TestReverseCultivationV3 (V3.0: spawn-first / reverse calculation)
# ---------------------------------------------------------------------------
def _reverse_form(**overrides):
    """13 bags x 800g into 2liter_box — the worked V3.0 example."""
    base = {
        "strain": "P. cubensis",
        "cult_lot": "CL-001",
        "container_type": "2liter_box",
        "avg_yield_fresh": "500",
        "target_dry_weight": "",  # not required in reverse mode
        "spawn_bags_colonized": "13",
        "spawn_bag_size_g": "800",
    }
    base.update(overrides)
    return base


class TestReverseCultivationV3:
    def test_forward_mode_is_default_when_spawn_fields_blank(self):
        cfg = load_config()
        form = _cultivation_form()
        form["spawn_bags_colonized"] = ""
        form["spawn_bag_size_g"] = ""
        r = calculate_cultivation(cfg, form)
        assert r["calculation_mode"] == "forward"
        assert r["spawn_bags_colonized"] is None
        assert r["grain_batch_count"] > 0
        assert r["total_lc"] > 0

    def test_reverse_13_bags_x_800g_into_2liter(self):
        cfg = load_config()
        r = calculate_cultivation(cfg, _reverse_form())
        assert r["calculation_mode"] == "reverse"
        # 13 x 800 = 10400g; 2liter spawn/container is 200g from config.
        assert r["total_spawn_on_hand_g"] == 10400
        assert r["total_containers"] == 52
        assert r["leftover_spawn_g"] == 0
        assert r["total_spawn"] == 10400
        # Substrate: 52 containers x per-container substrate from config
        # -> ceil(total / 6300) buckets (5 buckets at current config).
        spec = cfg["containers"]["2liter_box"]
        assert r["total_substrate"] == 52 * spec["substrate"]
        assert r["substrate_bucket_count"] == 5
        assert r["total_coir"] == 5 * 780
        # Already colonized: no grain, no liquid culture.
        assert r["grain_batch_count"] == 0
        assert r["total_grain"] == 0
        assert r["total_lc"] == 0
        assert r["total_bags"] == 13
        assert r["bag_size_spawn"] == 800
        # Expected dry estimate: 500 x 0.07 x 52 = 1820g.
        assert r["expected_dry_weight_g"] == 1820.0

    def test_reverse_leftover_spawn_reported(self):
        cfg = load_config()
        r = calculate_cultivation(
            cfg, _reverse_form(spawn_bag_size_g="750")
        )
        # 13 x 750 = 9750g -> 48 containers use 9600g, 150g left over.
        assert r["total_containers"] == 48
        assert r["leftover_spawn_g"] == 150
        assert r["total_substrate"] == 48 * cfg["containers"]["2liter_box"]["substrate"]

    def test_reverse_ignores_target_dry_weight(self):
        cfg = load_config()
        r = calculate_cultivation(
            cfg, _reverse_form(target_dry_weight="99999")
        )
        assert r["total_containers"] == 52  # spawn-limited, not target

    def test_reverse_without_avg_yield_has_zero_estimate(self):
        cfg = load_config()
        r = calculate_cultivation(
            cfg, _reverse_form(avg_yield_fresh="")
        )
        assert r["total_containers"] == 52
        assert r["expected_dry_weight_g"] == 0

    def test_reverse_only_one_spawn_field_filled_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError, match="filled together"):
            calculate_cultivation(
                cfg, _reverse_form(spawn_bag_size_g="")
            )
        with pytest.raises(ValueError, match="filled together"):
            calculate_cultivation(
                cfg, _reverse_form(spawn_bags_colonized="")
            )

    def test_reverse_insufficient_spawn_for_one_container_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError, match="Insufficient colonized spawn"):
            calculate_cultivation(
                cfg,
                _reverse_form(
                    spawn_bags_colonized="1", spawn_bag_size_g="100"
                ),
            )

    def test_reverse_non_integer_bags_raises(self):
        cfg = load_config()
        with pytest.raises(ValueError, match="whole number"):
            calculate_cultivation(
                cfg, _reverse_form(spawn_bags_colonized="13.5")
            )

    def test_forward_still_requires_target(self):
        cfg = load_config()
        form = _cultivation_form(target_dry_weight="")
        with pytest.raises(ValueError, match="target_dry_weight"):
            calculate_cultivation(cfg, form)

    def test_reverse_post_persists_and_printout_shows_section(
        self, client, test_app
    ):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_reverse_form())
        assert r.status_code == 200
        assert b"REVERSE CALCULATION" in r.data
        assert b"Colonized Spawn Bags" in r.data
        assert b"Not required" in r.data
        with test_app.app_context():
            rows = db.session.query(BatchLog).all()
            assert len(rows) == 1
            payload = rows[0].to_payload()
            assert payload["calculation_mode"] == "reverse"
            assert payload["total_containers"] == 52
            form = rows[0].to_form()
            assert form["spawn_bags_colonized"] == "13"
            assert form["spawn_bag_size_g"] == "800"

    def test_reverse_reprint_reproduces_reverse_document(
        self, client, test_app
    ):
        _login(client, "admin", "secureadminpassword")
        client.post("/", data=_reverse_form())
        with test_app.app_context():
            doc = db.session.query(BatchLog).first().doc_number
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        assert b"REVERSE CALCULATION" in r.data
        assert b"REPRINT" in r.data

    def test_forward_printout_has_no_reverse_section(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        assert b"REVERSE CALCULATION" not in r.data


# ---------------------------------------------------------------------------
# TestV3Design (V3.0: title ribbon, per-tab guidance, optional-field
# divider, black-and-white design matching the printed report)
# ---------------------------------------------------------------------------
class TestV3Design:
    def test_title_ribbon_on_all_pages(self, client):
        _login(client, "tech1", "techpass")
        for url in ("/", "/extraction", "/capsulation", "/login"):
            r = client.get(url, follow_redirects=True)
            assert r.status_code == 200
            assert b"PsygaBio Production Calculator V6" in r.data
            assert b"title-ribbon" in r.data

    def test_cultivation_tab_guide_and_optional_divider(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/")
        assert r.status_code == 200
        assert b"HOW TO USE THIS TAB" in r.data
        assert b"Mandatory (standard calculation)" in r.data
        assert b"Optional (reverse calculation)" in r.data
        assert b"Expected output" in r.data
        # Divider sits on its own line above the optional spawn fields.
        assert b"form-divider" in r.data
        assert b"Optional \xe2\x80\x94 Reverse Calculation" in r.data
        divider_pos = r.data.index(b'class="form-divider"')
        spawn_pos = r.data.index(b"spawn_bags_colonized")
        target_pos = r.data.index(b"target_dry_weight")
        assert target_pos < divider_pos < spawn_pos

    def test_extraction_tab_guide(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/extraction")
        assert r.status_code == 200
        assert b"HOW TO USE THIS TAB" in r.data
        assert b"Mandatory:" in r.data
        assert b"Optional:" in r.data
        assert b"Expected output:" in r.data
        # V5.0: the minimum lot is one whole 120 g portion
        assert b"120 g" in r.data

    def test_capsulation_tab_guide(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/capsulation")
        assert r.status_code == 200
        assert b"HOW TO USE THIS TAB" in r.data
        assert b"Mandatory:" in r.data
        assert b"Optional:" in r.data
        assert b"Expected output:" in r.data
        assert b"Source EX Doc Numbers" in r.data

    def test_no_colored_hex_in_templates(self):
        # V4: the print documents stay grayscale; the only colour
        # allowed is the portal gold (#c9a843 / #a8892f / #e0c06a)
        # and charcoal (#1a1a1a) ribbon/nav brand.
        import re

        template_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "templates"
        )
        grayscale = {
            "#000000", "#ffffff", "#333333", "#555555",
            "#e6e6e6", "#f0f0f0", "#f2f2f2",
            "#c9a843", "#a8892f", "#e0c06a", "#1a1a1a",  # portal gold/charcoal brand
        }
        for name in os.listdir(template_dir):
            if not name.endswith(".html"):
                continue
            with open(os.path.join(template_dir, name), encoding="utf-8") as fh:
                hexes = set(re.findall(r"#[0-9a-fA-F]{6}", fh.read()))
            assert hexes <= grayscale, (name, hexes - grayscale)


class TestV3PrintLayout:
    def test_ribbon_left_aligned_gold_charcoal(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/")
        assert r.status_code == 200
        assert b".title-ribbon" in r.data
        assert b"background: #1a1a1a" in r.data
        assert b"text-align: left" in r.data
        # Company logo in the header
        assert b"psyga_logo.jpg" in r.data
        assert b"logo-img" in r.data

    def test_container_help_topic(self, client):
        _login(client, "tech1", "techpass")
        r = client.get("/")
        assert r.status_code == 200
        assert b"Need a different container" in r.data
        assert b"config.yaml" in r.data
        assert b"Settings &rarr; Configuration" in r.data

    def test_user_stamp_on_printed_docs(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        assert b"by tech1 (technician)" in r.data
        r = client.post(
            "/extraction",
            data=_extraction_plan_form(grams_per_bottle="120", solvent_type="75%"),
        )
        assert b"by tech1 (technician)" in r.data
        r = client.post("/capsulation", data=_capsulation_form())
        assert b"by tech1 (technician)" in r.data

    def test_comment_line_per_section_and_page_numbers(self, client):
        _login(client, "tech1", "techpass")
        for url, form in (
            ("/", _cultivation_form()),
            ("/extraction", _extraction_plan_form(grams_per_bottle="120", solvent_type="75%")),
            ("/capsulation", _capsulation_form()),
        ):
            r = client.post(url, data=form)
            assert r.status_code == 200
            # Comment line is a CSS-generated .section::after rule.
            assert b".section::after" in r.data
            assert b'content: "Comments:"' in r.data
            # Repeating printed header + page numbering.
            assert b"@page" in r.data
            assert b"counter(page)" in r.data
            assert b"counter(pages)" in r.data
            assert b"@top-center" in r.data

    def test_index_section_printable_below_signatures(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        assert b"*INDEX" in r.data
        assert b"Calculation &amp; Document Logic" in r.data
        assert b"explain-panel" in r.data
        assert b"page-break-before: always" in r.data
        # The index panel must NOT be hidden in print.
        assert b'class="card no-print" style="background:#fff' not in r.data
        # It renders after the signature block in the document.
        sig_pos = r.data.index(b"signature-line")
        index_pos = r.data.index(b"*INDEX")
        assert sig_pos < index_pos


class TestV4RawMaterialForms:
    def test_sop_doc_number_format(self):
        assert sop_doc_number("SP", "WP-20261109-003") == "SP-2026-003"
        assert sop_doc_number("SUB", "WP-20260608-001") == "SUB-2026-001"
        assert sop_doc_number("MED", "WP-20261109-042") == "MED-2026-042"

    def test_forward_post_includes_all_three_sop_forms(self, client, test_app):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        # SOP headers + form IDs
        assert b"FORM-CULT-003" in r.data
        assert b"FORM-CULT-004" in r.data
        assert b"FORM-CULT-009" in r.data
        assert b"SOP-CULT-004" in r.data
        assert b"SOP-CULT-005" in r.data
        assert b"SOP-CULT-003" in r.data
        # SOP batch numbers linked to the WP master (first WP -> 001)
        assert b"SP-2026-001" in r.data
        assert b"SUB-2026-001" in r.data
        assert b"MED-2026-001" in r.data
        # Spawn form populated: monotub defaults in _cultivation_form
        assert b"SPAWN BATCH PRODUCTION RECORD" in r.data
        assert b"INCUBATION LOG" in r.data
        assert b"HP1" in r.data
        # Substrate per-bucket breakdown: 32 x 600g = 19200g -> 4 buckets
        assert b"PER-BUCKET BREAKDOWN" in r.data
        assert b"Bucket 1" in r.data
        assert b"Bucket 4" in r.data
        assert b"Bucket 5" not in r.data
        # Media form: liquid media only, +30% excess
        assert b"LIQUID MEDIA PREPARATION BATCH RECORD" in r.data
        assert b"LMEDP" in r.data
        assert b"Agar" not in r.data
        assert b"Total volume to prepare:" in r.data
        assert b"Media excess (30%):" in r.data
        # Stored in the ledger payload for reprint
        with test_app.app_context():
            payload = db.session.query(BatchLog).first().to_payload()
            assert payload["spawn_doc_number"] == "SP-2026-001"
            assert payload["substrate_doc_number"] == "SUB-2026-001"
            assert payload["media_doc_number"] == "MED-2026-001"

    def test_reverse_post_only_substrate_form(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_reverse_form())
        assert r.status_code == 200
        assert b"SUBSTRATE BATCH PRODUCTION RECORD" in r.data
        # V6.1: in reverse mode only the substrate form is produced, so
        # the other forms are not even named in the Produce panel.
        assert b"SPAWN BATCH PRODUCTION RECORD" not in r.data
        assert b"LIQUID MEDIA PREPARATION BATCH RECORD" not in r.data
        assert b"SPAWN INCUBATION" not in r.data
        assert b"Produce a form" in r.data
        assert b"printForm('substrate')" in r.data
        assert b"printForm('spawn')" not in r.data
        assert b"Bucket 5" in r.data  # 52 x 600g -> 5 buckets at SOP recipe

    def test_reprint_preserves_sop_numbers(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        client.post("/", data=_cultivation_form())
        with test_app.app_context():
            doc = db.session.query(BatchLog).first().doc_number
        r = client.get(f"/admin/history/reprint/{doc}")
        assert r.status_code == 200
        assert b"SP-2026-001" in r.data
        assert b"SUB-2026-001" in r.data
        assert b"REPRINT" in r.data

    def test_selective_print_buttons_present(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        # V6.1: every form is produced from the "Produce a form" panel.
        assert b"Produce a form" in r.data
        assert b"printForm('wp')" in r.data
        assert b"printForm('spawn')" in r.data
        assert b"printForm('incubation')" in r.data
        assert b"printForm('g5')" in r.data
        assert b"printForm('substrate')" in r.data
        assert b"printForm('media')" in r.data

    def test_grain_batches_breakdown_two_10kg_batches(self):
        # 22400g spawn (32 monotubs) -> 3 batches? No: ceil(22400/10000)=3.
        # Use a form needing exactly 2 batches: 15 monotubs = 31500g substrate,
        # spawn 10500g -> 2 batches (10000 + 500).
        cfg = load_config()
        from app import calculate_cultivation
        r = calculate_cultivation(cfg, {
            "strain": "P. cubensis", "cult_lot": "CL-001",
            "container_type": "monotub", "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        })
        # 32 monotubs -> 22400g spawn -> 3 batches 10000/10000/2400
        assert r["grain_batch_count"] == 3
        batches = r["grain_batches"]
        assert [b["spawn_g"] for b in batches] == [10000, 10000, 2400]
        # V6.0: bags are attributed to the batch they were drawn from,
        # so the per-batch counts add up to the lot total exactly.
        assert [b["bags"] for b in batches] == [15, 15, 2]  # 700 g bags
        assert sum(b["bags"] for b in batches) == r["total_bags"]
        assert all(b["grain_g"] == 5000 and b["water_ml"] == 10000 for b in batches)
        # Media excess: total_lc * 1.3 ceil
        assert r["media_excess_pct"] == 30
        # V6.0: media is prepared from the LC volume that already
        # includes the >= 20 % bench backup.
        assert r["lc_excess_pct"] >= 20
        assert r["media_prep_volume_ml"] == math.ceil(
            r["lc_prepare_ml"] * 1.3
        )

    def test_spawn_form_repeats_procedure_per_batch(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form(container_type="monotub"))
        assert r.status_code == 200
        assert b"batch(es) of 10 kg spawn" in r.data
        assert b"Batch 1 of 3" in r.data
        assert b"Batch 3 of 3" in r.data
        assert b"BAGS CREATED BEFORE AUTOCLAVE" in r.data
        assert b"Weight per bag (g)" in r.data
        # Incubation is its own form with its own header + signatures
        assert b"SPAWN INCUBATION &amp; RELEASE LOG" in r.data
        assert b"doc-incubation" in r.data
        assert b"SIGN-OFF" in r.data

    def test_sop_forms_collapsed_by_default(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=_cultivation_form())
        assert r.status_code == 200
        # WP + index visible by default (no collapsed class on them)
        assert b'class="protocol doc-wp"' in r.data
        assert b"explain-panel" in r.data
        # Every SOP form starts collapsed behind a View form button
        # V6.0: five SOP forms (003, 003 incubation, 003A, 004, 009)
        assert r.data.count(b"form-collapsed doc-") == 5
        assert b"View form: Spawn Batch Production Record" in r.data
        assert b"View form: Spawn Incubation" in r.data
        assert b"View form: G5 Inoculation Record" in r.data
        assert b"View form: Substrate Batch Production Record" in r.data
        assert b"View form: Liquid Media Preparation Record" in r.data
        assert b"toggleRawForm" in r.data
        # Print expands them again
        assert b".raw-form.form-collapsed { display: block !important; }" in r.data


class TestRolesV4:
    def test_all_eight_roles_listed_in_settings(self, client):
        _login(client, "admin", "secureadminpassword")
        r = client.get("/admin/settings")
        assert r.status_code == 200
        for label in (b"Facility Manager", b"Cultivation Manager",
                      b"Cultivation Operator", b"Production Manager",
                      b"Production Operator", b"Quality Manager",
                      b"Administrator"):
            assert label in r.data

    def test_create_user_with_new_role(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        r = client.post("/admin/settings", data={
            "action": "create", "username": "ido",
            "password": "testpass1", "role": "cultivation_manager",
        })
        assert r.status_code == 302
        with test_app.app_context():
            u = db.session.query(User).filter_by(username="ido").first()
            assert u is not None
            assert u.role == "cultivation_manager"
            assert u.check_password("testpass1")
            assert not u.is_admin()

    def test_administrator_role_is_admin(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        client.post("/admin/settings", data={
            "action": "create", "username": "yaniv",
            "password": "testpass1", "role": "administrator",
        })
        with test_app.app_context():
            u = db.session.query(User).filter_by(username="yaniv").first()
            assert u.is_admin()

    def test_invalid_role_rejected(self, client, test_app):
        _login(client, "admin", "secureadminpassword")
        client.post("/admin/settings", data={
            "action": "create", "username": "bad",
            "password": "testpass1", "role": "superuser",
        })
        with test_app.app_context():
            assert db.session.query(User).filter_by(username="bad").first() is None

    def test_role_labels(self):
        from app import is_valid_role, role_label
        assert role_label("cultivation_manager") == "Cultivation Manager"
        assert role_label("administrator") == "Administrator"
        assert role_label("admin") == "Administrator"
        assert role_label("technician") == "User"
        assert is_valid_role("facility_manager")
        assert is_valid_role("quality_manager")
        assert not is_valid_role("root")


# ---------------------------------------------------------------------------
# TestV5Hardening (V5.0 security + robustness changes)
# ---------------------------------------------------------------------------
class TestCsrfProtection:
    """V5.0: every state-changing POST must carry a valid CSRF token."""

    def _form(self):
        return {
            "strain": "P. cubensis",
            "cult_lot": "CL-CSRF",
            "container_type": "monotub",
            "avg_yield_fresh": "500",
            "target_dry_weight": "1000",
        }

    def test_post_without_token_is_rejected(self, client):
        _login(client, "tech1", "techpass")
        r = client.raw().post("/", data=self._form())
        assert r.status_code == 400

    def test_post_with_wrong_token_is_rejected(self, client):
        _login(client, "tech1", "techpass")
        r = client.raw().post("/", data=dict(self._form(), _csrf_token="bogus"))
        assert r.status_code == 400

    def test_post_with_valid_token_succeeds(self, client):
        _login(client, "tech1", "techpass")
        r = client.post("/", data=self._form())
        assert r.status_code == 200
        assert b"WP-" in r.data

    def test_login_without_token_is_rejected(self, client):
        r = client.raw().post("/login", data={
            "username": "tech1", "password": "techpass"
        })
        assert r.status_code == 400

    def test_token_is_issued_on_the_login_page(self, client):
        r = client.get("/login")
        html = r.data.decode("utf-8")
        assert 'name="_csrf_token"' in html

    def test_every_form_carries_a_token(self, client):
        _login(client, "admin", "secureadminpassword")
        for path in ("/", "/extraction", "/capsulation", "/admin/settings",
                     "/account/password"):
            html = client.get(path).data.decode("utf-8")
            assert 'name="_csrf_token"' in html, f"{path} has no CSRF token"


class TestLedgerImmutability:
    """V5.0: the append-only contract is enforced by the database itself."""

    def _make_row(self, client, test_app):
        _login(client, "tech1", "techpass")
        client.post("/", data={
            "strain": "P", "cult_lot": "CL-IMM",
            "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        })
        with test_app.app_context():
            return db.session.query(BatchLog).first()

    def test_bulk_query_delete_is_blocked(self, client, test_app):
        self._make_row(client, test_app)
        with test_app.app_context():
            from sqlalchemy.exc import DatabaseError
            with pytest.raises((DatabaseError, RuntimeError)):
                db.session.query(BatchLog).delete()
            db.session.rollback()

    def test_raw_sql_update_is_blocked(self, client, test_app):
        self._make_row(client, test_app)
        with test_app.app_context():
            from sqlalchemy import text
            from sqlalchemy.exc import DatabaseError
            with pytest.raises((DatabaseError, RuntimeError)):
                with db.session.begin():
                    db.session.execute(
                        text("update batch_logs set strain='tampered'")
                    )
            db.session.rollback()

    def test_raw_sql_delete_is_blocked(self, client, test_app):
        self._make_row(client, test_app)
        with test_app.app_context():
            from sqlalchemy import text
            from sqlalchemy.exc import DatabaseError
            with pytest.raises((DatabaseError, RuntimeError)):
                with db.session.begin():
                    db.session.execute(text("delete from batch_logs"))
            db.session.rollback()

    def test_row_survives_a_tamper_attempt(self, client, test_app):
        self._make_row(client, test_app)
        with test_app.app_context():
            from sqlalchemy import text
            from sqlalchemy.exc import DatabaseError
            try:
                with db.session.begin():
                    db.session.execute(
                        text("update batch_logs set strain='tampered'")
                    )
            except (DatabaseError, RuntimeError):
                pass
            db.session.rollback()
            row = db.session.query(BatchLog).first()
            assert row is not None
            assert row.strain != "tampered"

    def test_instance_level_update_is_blocked(self, client, test_app):
        self._make_row(client, test_app)
        with test_app.app_context():
            row = db.session.query(BatchLog).first()
            row.strain = "tampered"
            with pytest.raises(RuntimeError):
                db.session.commit()
            db.session.rollback()

    def test_config_changes_are_append_only(self, client, test_app):
        from app import ConfigChange
        with test_app.app_context():
            from sqlalchemy import text
            from sqlalchemy.exc import DatabaseError
            # A row has to exist: a DELETE trigger only fires per row.
            db.session.add(ConfigChange(
                user_id=1, username="admin", config_text="x: 1"
            ))
            db.session.commit()
            with pytest.raises((DatabaseError, RuntimeError)):
                with db.session.begin():
                    db.session.execute(text("delete from config_changes"))
            db.session.rollback()
            assert db.session.query(ConfigChange).count() == 1


class TestRuntimeHardening:
    """V5.0: secret key, debug mode and SQLite pragmas."""

    def test_secret_key_is_not_the_hardcoded_default(self, test_app):
        assert test_app.config["SECRET_KEY"] != \
            "psyga-gmp-secret-change-in-production"
        assert len(test_app.config["SECRET_KEY"]) >= 32

    def test_debug_is_off_by_default(self, test_app):
        assert test_app.config["DEBUG"] is False

    def test_source_tree_has_no_hardcoded_secret(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "app.py"), encoding="utf-8") as fh:
            source = fh.read()
        assert "psyga-gmp-secret-change-in-production" not in source
        assert "secureadminpassword" not in source

    def test_foreign_keys_pragmas_are_enabled(self, test_app):
        with test_app.app_context():
            from sqlalchemy import text
            fk = db.session.execute(text("PRAGMA foreign_keys")).scalar()
            assert fk == 1

    def test_journal_mode_is_wal_on_a_file_database(self, tmp_path):
        """A file-backed database must run in WAL mode (not the default)."""
        from sqlalchemy import text
        from sqlalchemy import create_engine
        engine = create_engine(f"sqlite:///{tmp_path/'probe.db'}")
        from app import _configure_sqlite
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert mode.lower() == "wal"


class TestConfigValidationV5:
    """V5.0: the new extraction parameters are validated on load."""

    def test_missing_portion_size_is_rejected(self):
        from app import validate_config
        cfg = load_config()
        cfg["extraction_parameters"].pop("grams_per_bottle")
        with pytest.raises(ValueError) as exc:
            validate_config(cfg)
        assert "grams_per_bottle" in str(exc.value)

    def test_invalid_solvent_volume_is_rejected(self):
        from app import validate_config
        cfg = load_config()
        cfg["extraction_parameters"]["solvent_volume_per_bottle_ml"] = 0
        with pytest.raises(ValueError) as exc:
            validate_config(cfg)
        assert "solvent_volume_per_bottle_ml" in str(exc.value)

    def test_stock_bottle_smaller_than_working_volume_is_rejected(self):
        from app import validate_config
        cfg = load_config()
        cfg["extraction_parameters"]["stock_bottle_volume_ml"] = 2000
        with pytest.raises(ValueError) as exc:
            validate_config(cfg)
        assert "stock_bottle_volume_ml" in str(exc.value)

    def test_shipped_config_has_the_v5_extraction_keys(self):
        cfg = load_config()
        params = cfg["extraction_parameters"]
        assert params["grams_per_bottle"] == 120
        assert params["stock_bottle_volume_ml"] == 2500
        assert params["solvent_volume_per_bottle_ml"] == 2400


class TestExtractionDocumentV5:
    """The printed V5 extraction document must carry the new sections."""

    def _post(self, client, **overrides):
        base = {
            "production_lot_number": "LOT-DOC-V5",
            "strain": "P. cubensis",
            "solvent_type": "90%",
            "dried_weight_total_g": "400",
            "daily_bottle_count": "2",
            "grinder_loss_per_round_g": "10",
            "yield_override_factor": "",
        }
        base.update(overrides)
        return client.post("/extraction", data=base)

    def test_document_has_the_new_sections(self, client):
        _login(client, "tech1", "techpass")
        html = self._post(client).data.decode("utf-8")
        assert "SOLVENT PREPARATION" in html
        assert "BOTTLES &amp; REAGENT TOTALS" in html
        assert "TIMELINE" in html
        assert "LOT SUMMARY" in html

    def test_document_shows_the_per_bottle_volumes(self, client):
        _login(client, "tech1", "techpass")
        html = self._post(client).data.decode("utf-8")
        # 90% MeOH, 2.5 L stock bottle: 340 mL out, 240 mL in per bottle
        assert "340" in html
        assert "240" in html

    def test_document_states_the_formula_without_a_step_table(self, client):
        """
        V5.1: section 2 states the solvent preparation formula and the
        applied volumes. The step-by-step table was removed - it repeated
        the numbers and was easy to misread at the bench.
        """
        _login(client, "tech1", "techpass")
        html = self._post(client).data.decode("utf-8")
        assert "SOLVENT PREPARATION" in html
        assert "Formula:" in html
        assert "water to put back" in html
        assert "MeOH to take out" in html
        # No step table any more
        assert "Final volume check" not in html
        assert "Charge the stock solvent" not in html
        # ... and the end state reads 2400 mL
        assert "2400 mL" in html
        # ... and the mushroom : solvent ratio is shown
        assert "1:20" in html

    def test_document_shows_the_leftover(self, client):
        _login(client, "tech1", "techpass")
        html = self._post(client).data.decode("utf-8")
        assert "LEFTOVER" in html
        assert "40g" in html

    def test_form_no_longer_has_the_removed_fields(self, client):
        _login(client, "tech1", "techpass")
        html = client.get("/extraction").data.decode("utf-8")
        assert 'name="dry_lot_number"' not in html
        assert 'name="pack_size_g"' not in html
        assert 'name="grams_per_bottle"' not in html

    def test_ledger_records_the_production_lot(self, client, test_app):
        _login(client, "tech1", "techpass")
        self._post(client, production_lot_number="LOT-LEDGER")
        with test_app.app_context():
            row = db.session.query(BatchLog).filter_by(
                batch_type="extraction"
            ).first()
            assert row.lot_number == "LOT-LEDGER"
"""
V6.0 tests - the spawn-bag model and the FORM-CULT-003A G5 Inoculation Record.
"""

import math
import re

import pytest

from app import calculate_cultivation, load_config


FORM = {
    "strain": "Natal_F6/TATeF2/DJF3_F20",
    "cult_lot": "CL-001",
    "container_type": "monotub",
    "avg_yield_fresh": "500",
    "target_dry_weight": "1000",
}


def _calc(**overrides):
    cfg = load_config()
    form = dict(FORM)
    form.update(overrides)
    return calculate_cultivation(cfg, form)


class TestV6SpawnBagModel:
    """The spawn bag fill weight drives how many bags are filled."""

    def test_default_fill_weight_is_the_container_spawn(self):
        cfg = load_config()
        r = _calc()
        assert r["bag_size_spawn"] == cfg["containers"]["monotub"]["spawn"]
        assert r["total_bags"] == math.ceil(r["total_spawn"] / r["bag_size_spawn"])

    def test_fill_weight_changes_the_bag_count(self):
        r = _calc(spawn_bag_fill_weight_g="500")
        assert r["bag_size_spawn"] == 500
        assert r["total_bags"] == math.ceil(r["total_spawn"] / 500)
        assert r["total_bags"] == 45  # 22400 / 500

    def test_bags_hold_exactly_the_total_spawn(self):
        for fill in ("200", "400", "500", "750", "700"):
            r = _calc(spawn_bag_fill_weight_g=fill)
            held = sum(b["weight_g"] for b in r["spawn_bags"])
            assert held == r["total_spawn"], fill
            assert len(r["spawn_bags"]) == r["total_bags"]

    def test_only_the_last_bag_can_be_partial(self):
        r = _calc(spawn_bag_fill_weight_g="500")
        partial = [b for b in r["spawn_bags"] if b["is_partial"]]
        assert len(partial) <= 1
        if partial:
            assert partial[0]["index"] == r["total_bags"]
            assert partial[0]["weight_g"] < r["bag_size_spawn"]

    def test_per_batch_bags_add_up_to_the_lot_total(self):
        """V6.0: bags are attributed to the batch they were drawn from."""
        for fill in ("200", "500", "750"):
            r = _calc(spawn_bag_fill_weight_g=fill)
            assert sum(b["bags"] for b in r["grain_batches"]) == r["total_bags"]

    def test_non_positive_fill_weight_is_rejected(self):
        with pytest.raises(ValueError):
            _calc(spawn_bag_fill_weight_g="-5")

    def test_fill_weight_is_ignored_in_reverse_mode(self):
        r = calculate_cultivation(
            load_config(),
            dict(FORM, spawn_bags_colonized="13", spawn_bag_size_g="800"),
        )
        assert r["calculation_mode"] == "reverse"
        assert r["spawn_bags"] == []
        assert r["total_lc"] == 0


class TestV6LiquidCultureModel:
    """LC is dosed by spawn mass, with a backup on the prepared total."""

    def test_lc_per_bag_follows_the_rate(self):
        r = _calc(spawn_bag_fill_weight_g="750")
        assert r["lc_rate_ml"] == 5
        assert r["lc_rate_basis_g"] == 200
        for bag in r["spawn_bags"]:
            assert bag["lc_ml"] == pytest.approx(
                bag["weight_g"] / 200 * 5
            )

    def test_lc_needed_is_the_rate_times_the_total_spawn(self):
        r = _calc(spawn_bag_fill_weight_g="500")
        expected = r["total_spawn"] / 200 * 5
        assert r["lc_needed_ml"] == pytest.approx(expected)

    def test_prepared_volume_carries_at_least_20_percent_backup(self):
        r = _calc()
        assert r["lc_excess_pct"] >= 20
        assert r["lc_prepare_ml"] >= math.ceil(r["lc_needed_ml"] * 1.20)

    def test_backup_floor_cannot_be_configured_below_20(self):
        cfg = load_config()
        cfg["media_parameters"]["lc_excess_pct"] = 5
        form = dict(FORM)
        result = calculate_cultivation(cfg, form)
        assert result["lc_excess_pct"] == 20

    def test_lc_rate_can_be_overridden_on_the_form(self):
        r = _calc(lc_rate_ml="10")
        assert r["lc_rate_ml"] == 10
        assert r["lc_needed_ml"] == pytest.approx(r["total_spawn"] / 200 * 10)

    def test_media_is_prepared_with_its_own_excess_on_top(self):
        cfg = load_config()
        r = _calc()
        media_excess = cfg["media_parameters"]["excess_pct"]
        assert r["media_prep_volume_ml"] == math.ceil(
            r["lc_prepare_ml"] * (1 + media_excess / 100)
        )


class TestV6G5InoculationForm:
    """FORM-CULT-003A on the printed Working Protocol."""

    def _html(self, **overrides):
        from app import create_app, register_routes
        from flask import render_template

        application = create_app(
            {"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"}
        )
        register_routes(application)
        cfg = load_config()
        r = calculate_cultivation(cfg, dict(FORM, **overrides))
        r.update(
            spawn_doc_number="SP-2026-003",
            substrate_doc_number="SUB-2026-003",
            media_doc_number="MED-2026-003",
            timestamp="2026-11-11 14:00:00",
            doc_number="WP-20261111-003",
        )
        with application.test_request_context("/"):
            return render_template(
                "index.html",
                result=r,
                container_types=list(cfg["containers"].keys()),
                doc_number=r["doc_number"],
                container_defaults=cfg["containers"],
                lc_basis_g=200.0,
                lc_default_rate=5.0,
            ), r

    def _log_rows(self, html):
        """Count the bag rows rendered in the inoculation log."""
        block = re.search(
            r'3\. PER-BAG INOCULATION LOG.*?(?=<div class="signature-line"|'
            r'<h3>4\. G5 RELEASE SIGN-OFF)',
            html,
            re.S,
        )
        assert block, "the per-bag inoculation log was not rendered"
        return len(re.findall(
            r"<td>\d+</td>\s*<td>\d+</td>\s*<td>[\d.]+</td>", block.group(0)
        ))

    def test_form_is_present_in_forward_mode(self):
        html, _ = self._html()
        assert "FORM-CULT-003A" in html
        assert "G5 INOCULATION RECORD" in html
        assert "G4 source ID (FORM-CULT-002C" in html
        assert "HP3" in html and "G4 LC RELEASE VERIFICATION" in html

    def test_form_has_the_paper_sections(self):
        html, _ = self._html()
        assert "1. HEADER" in html
        assert "2. HP3" in html
        assert "3. PER-BAG INOCULATION LOG" in html
        assert "4. G5 RELEASE SIGN-OFF" in html
        assert "DEVIATIONS" in html
        assert "FORM-DEV-001" in html

    def test_log_has_one_row_per_bag(self):
        html, r = self._html(spawn_bag_fill_weight_g="500")
        assert self._log_rows(html) == r["total_bags"] == 45

    def test_log_paginates_thirty_bags_per_sheet(self):
        html, r = self._html(spawn_bag_fill_weight_g="200")
        assert r["total_bags"] == 112
        assert self._log_rows(html) == 112
        # 112 bags at 30 per sheet = 4 sheets
        assert html.count("PER-BAG INOCULATION LOG") == 1
        assert html.count("Sheet ") >= 4
        assert "continuation sheet" in html.lower()

    def test_a_single_sheet_has_no_continuation_note(self):
        html, r = self._html(spawn_bag_fill_weight_g="750")
        assert r["total_bags"] == 30
        assert self._log_rows(html) == 30
        assert "continuation sheet" not in html.lower()

    def test_per_bag_lc_volume_is_shown(self):
        html, r = self._html(spawn_bag_fill_weight_g="750")
        assert "18.75" in html          # 750 g bag at 5 mL / 200 g
        assert "16.25" in html          # the partial 650 g last bag
        assert "LC dose per bag" in html

    def test_totals_are_printed(self):
        html, r = self._html()
        assert "Total LC needed" in html
        assert "LC to prepare" in html
        assert "560" in html             # 22400 g / 200 * 5
        assert "672" in html             # + 20 % backup

    def test_form_is_absent_in_reverse_mode(self):
        html, r = self._html(
            spawn_bags_colonized="13", spawn_bag_size_g="800"
        )
        assert r["calculation_mode"] == "reverse"
        # The form itself is not printed ...
        assert 'class="protocol raw-form form-collapsed doc-g5"' not in html
        assert "PER-BAG INOCULATION LOG" not in html
        # ... but the INDEX still explains what it is and when it applies.
        assert "G5 INOCULATION RECORD" in html
        assert "not printed in reverse mode" in html

    def test_produce_button_exists(self):
        html, _ = self._html()
        assert "printForm('g5')" in html
        assert "G5 Inoculation Record" in html
        assert "FORM-CULT-003A" in html

    def test_form_shares_the_spawn_batch_number(self):
        html, _ = self._html()
        assert "SP-2026-003" in html

"""
V6.1 tests - per-document production and per-signature dates.
"""

import re


class TestProducePerDocument:
    """
    Each printed form must be produced on its own so that its page
    numbering (Page 1 of N) is independent of the other documents.
    """

    def _html(self, **overrides):
        from app import create_app, register_routes
        from flask import render_template

        application = create_app(
            {"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"}
        )
        register_routes(application)
        from app import calculate_cultivation, load_config

        cfg = load_config()
        form = {
            "strain": "S", "cult_lot": "CL", "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        }
        form.update(overrides)
        r = calculate_cultivation(cfg, form)
        r.update(
            spawn_doc_number="SP-2026-003",
            substrate_doc_number="SUB-2026-003",
            media_doc_number="MED-2026-003",
            timestamp="2026-11-11 14:00:00",
            doc_number="WP-20261111-003",
        )
        with application.test_request_context("/"):
            return render_template(
                "index.html",
                result=r,
                container_types=list(cfg["containers"].keys()),
                doc_number=r["doc_number"],
                container_defaults=cfg["containers"],
                lc_basis_g=200.0,
                lc_default_rate=5.0,
            ), r

    def test_print_all_button_is_gone(self):
        html, _ = self._html()
        assert "Print / Save as PDF (all)" not in html
        assert "onclick=\"window.print()\"" not in html

    def test_produce_panel_exists(self):
        html, _ = self._html()
        assert "Produce a form" in html
        assert 'id="produce-panel"' in html

    def test_one_produce_button_per_form(self):
        html, _ = self._html()
        for kind in ("wp", "spawn", "incubation", "g5", "substrate", "media"):
            assert f"printForm('{kind}')" in html, kind

    def test_produce_panel_lists_every_document_number(self):
        html, _ = self._html()
        assert "WP-20261111-003" in html
        assert "SP-2026-003" in html
        assert "SUB-2026-003" in html
        assert "MED-2026-003" in html

    def test_each_form_has_its_own_running_header(self):
        """
        The running header of the printed page is set per document just
        before printing, so a form is never titled with another form's
        name.
        """
        html, _ = self._html()
        assert "PRINT_FORMS" in html
        assert "setPageHeader" in html
        assert "FACILITY WORKING PROTOCOL" in html
        assert "SPAWN BATCH PRODUCTION RECORD" in html
        assert "G5 INOCULATION RECORD" in html
        assert "SUBSTRATE BATCH PRODUCTION RECORD" in html
        assert "LIQUID MEDIA PREPARATION BATCH RECORD" in html

    def test_the_g5_row_is_hidden_in_reverse_mode(self):
        html, r = self._html(
            spawn_bags_colonized="13", spawn_bag_size_g="800"
        )
        assert r["calculation_mode"] == "reverse"
        assert "printForm('g5')" not in html
        assert "printForm('media')" not in html
        # the substrate form is still produced
        assert "printForm('substrate')" in html


class TestSignatureDates:
    """Every signature carries its own date field."""

    def _wp(self, **overrides):
        from app import create_app, register_routes
        from flask import render_template

        application = create_app(
            {"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"}
        )
        register_routes(application)
        from app import calculate_cultivation, load_config

        cfg = load_config()
        form = {
            "strain": "S", "cult_lot": "CL", "container_type": "monotub",
            "avg_yield_fresh": "500", "target_dry_weight": "1000",
        }
        form.update(overrides)
        r = calculate_cultivation(cfg, form)
        r.update(
            spawn_doc_number="SP-2026-003",
            substrate_doc_number="SUB-2026-003",
            media_doc_number="MED-2026-003",
            timestamp="2026-11-11 14:00:00",
            doc_number="WP-20261111-003",
        )
        with application.test_request_context("/"):
            return render_template(
                "index.html",
                result=r,
                container_types=list(cfg["containers"].keys()),
                doc_number=r["doc_number"],
                container_defaults=cfg["containers"],
                lc_basis_g=200.0,
                lc_default_rate=5.0,
            )

    def test_wp_has_one_date_per_signature(self):
        html = self._wp()
        start = html.find('<div class="signature-line">')
        assert start != -1, "the WP signature block was not rendered"
        markup = html[start:start + 1200]
        assert markup.count('class="date-field"') == 2
        assert markup.count("Signature") == 2
        # the old shared single Date box is gone
        assert '<div class="line">Date</div>' not in markup

    def test_raw_material_signoff_table_has_a_date_column(self):
        html = self._wp()
        # the shared signoff() macro: Role | Name | Signature | Date
        assert "<th>Role</th><th>Name</th><th>Signature</th><th>Date</th>" in html

    def test_hp1_decision_has_a_date(self):
        html = self._wp()
        assert "<th>Init.</th>" in html
        assert "<th>Date</th>" in html

    def test_extraction_and_capsulation_have_one_date_per_signature(self):
        import os
        from app import create_app, register_routes
        from flask import render_template

        application = create_app(
            {"SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"}
        )
        register_routes(application)
        from app import calculate_extraction, calculate_capsulation, load_config

        cfg = load_config()
        ex = calculate_extraction(cfg, {
            "dried_weight_available": "500", "solvent_type": "90%",
        })
        ex.update(doc_number="EX-20261111-001", timestamp="2026-11-11 14:00:00")
        with application.test_request_context("/"):
            html = render_template(
                "extraction.html", result=ex, error=None,
                doc_number=ex["doc_number"],
                solvent_types=["100%", "90%"],
                portion_size_g=120.0, stock_bottle_volume_ml=2500.0,
            )
        assert html.count('class="date-field"') == 2

        cp = calculate_capsulation(cfg, {
            "production_lot_number": "PL", "grams_of_extract": "100",
            "psilocybin_pct": "1.5", "psilocin_pct": "0.3",
            "anti_caking_pct": "5", "dosage_mode": "tpe",
            "source_ex_doc_numbers": "EX-20261111-001",
        })
        cp.update(doc_number="CP-20261111-001", timestamp="2026-11-11 14:00:00")
        with application.test_request_context("/"):
            html = render_template(
                "capsulation.html", result=cp, error=None,
                doc_number=cp["doc_number"],
                dosage_modes=["psilocybin", "tpe"],
            )
        assert html.count('class="date-field"') == 2

