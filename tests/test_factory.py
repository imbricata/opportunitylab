import json
import math
import sys
import time
import types
from dataclasses import replace

import pytest

from opportunity_lab import ai
from opportunity_lab.automation import (
    active_btc_spec,
    allocate_live,
    bootstrap_automation,
    compile_hypotheses,
    evaluate_specs,
    run_btc_variants,
)
from opportunity_lab.config import CFG, validate_config
from opportunity_lab.db import BTC_V8_STRATEGY_ID, connect, get_setting, set_setting
from opportunity_lab.factory import (
    _start_run,
    bootstrap_factory,
    due_notifications,
    enqueue_notification,
    healthcheck,
    mark_notification,
    research_new_sources,
    update_capital_snapshot,
)
from opportunity_lab.live import (
    LIVE_CONFIRMATION,
    LiveBroker,
    active_risk_cohort,
    cohort_risk_fraction,
    risk_cohort_snapshot,
)
from opportunity_lab.maintenance import run_maintenance
from opportunity_lab.prediction_venues import PolymarketPublic


def fresh(tmp_path):
    path = tmp_path / "factory.db"
    db = connect(path)
    return db, replace(CFG, db_path=path)


def test_migration_and_bootstrap_are_idempotent(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_factory(db, cfg)
    columns = {row[1] for row in db.execute("PRAGMA table_info(fast_live_orders)")}
    assert "strategy_version_id" in columns
    assert db.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fast_live_shadow_decisions'"
    ).fetchone()
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='live_risk_cohorts'"
    ).fetchone()
    assert "risk_cohort_id" in {
        row[1] for row in db.execute("PRAGMA table_info(fast_live_orders)")
    }
    allocation = db.execute(
        "SELECT state,max_risk_fraction FROM factory_allocations WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()
    assert tuple(allocation) == ("active", cfg.live_risk_fraction)
    db.close()
    reopened = connect(cfg.db_path)
    assert reopened.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5


def test_capital_snapshot_does_not_treat_deposit_as_pnl(tmp_path):
    db, _ = fresh(tmp_path)
    set_setting(db, "live_session_initial_balance", 18.49)
    set_setting(
        db, "live_last_preflight",
        {"balance_usd": 29.79, "checked_ts": time.time()},
    )
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,realized_pnl,evidence_json,
         strategy_version_id)
        VALUES('settled','c','t','up',1,2,'settled',5,.5,2.5,18.49,2.07,'{}',?)""",
        (BTC_V8_STRATEGY_ID,),
    )
    db.commit()
    snapshot = update_capital_snapshot(db)
    assert snapshot["realized_live_pnl"] == pytest.approx(2.07)
    assert snapshot["estimated_external_net"] == pytest.approx(9.23)


def test_official_venue_result_controls_live_settlement(tmp_path):
    db, cfg = fresh(tmp_path)
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,actual_qty,actual_cost_usd,
         evidence_json,strategy_version_id)
        VALUES('mismatch','c','t','up',1,2,'matched',5,.6,3,20,5,3,'{}',?)""",
        (BTC_V8_STRATEGY_ID,),
    )
    db.execute(
        """INSERT INTO fast_window_results
        (slug,start_ts,end_ts,opening_price,ending_price,observed_result,venue_result,
         result_matches,validated_ts,evidence_json)
        VALUES('mismatch',1,2,100,101,'up','down',0,3,'{}')"""
    )
    db.commit()
    assert LiveBroker(cfg).mark_resolutions(db) == 1
    row = db.execute(
        "SELECT state,result,realized_pnl FROM fast_live_orders WHERE slug='mismatch'"
    ).fetchone()
    assert tuple(row) == ("settled", "down", -3.0)


def test_ai_budget_reservation_is_hard_and_counted(tmp_path):
    db, cfg = fresh(tmp_path)
    cfg = replace(cfg, max_ai_usd_per_day=0.8, max_ai_output_tokens=20)
    row_id = ai._reserve_budget(db, cfg, "test", "one", {"x": "small"})
    row = db.execute("SELECT status,estimated_cost_usd FROM ai_calls_v2 WHERE id=?", (row_id,)).fetchone()
    assert row[0] == "reserved" and row[1] > 0
    blocked = replace(cfg, max_ai_calls_per_day=1)
    with pytest.raises(ai.AIBudgetExceeded):
        ai._reserve_budget(db, blocked, "test", "two", {"x": "small"})


def test_healthcheck_pauses_and_restores_live_allocation(tmp_path):
    db, cfg = fresh(tmp_path)
    set_setting(db, "btc_runtime_heartbeat", time.time())
    db.commit()
    bad = replace(cfg, factory_disk_pause_free_gb=10_000)
    assert healthcheck(db, bad)["status"] == "paused"
    assert get_setting(db, "factory_live_pause")["active"] is True
    set_setting(db, "btc_runtime_heartbeat", time.time())
    db.commit()
    good = replace(cfg, factory_disk_pause_free_gb=0.000001)
    assert healthcheck(db, good)["status"] == "healthy"
    assert get_setting(db, "factory_live_pause")["active"] is False


def test_notification_outbox_deduplicates_and_retries(tmp_path):
    db, _ = fresh(tmp_path)
    assert enqueue_notification(db, kind="test", message="hello", dedupe_key="same")
    assert not enqueue_notification(db, kind="test", message="hello", dedupe_key="same")
    row = due_notifications(db)[0]
    mark_notification(db, row["id"], RuntimeError("network"))
    saved = db.execute(
        "SELECT attempts,sent_ts,last_error FROM notification_outbox WHERE id=?", (row["id"],)
    ).fetchone()
    assert saved[0] == 1 and saved[1] is None and "network" in saved[2]


def test_research_hypothesis_insert_is_paper_only(tmp_path, monkeypatch):
    db, cfg = fresh(tmp_path)
    cfg = replace(cfg, openai_api_key="sk-test", factory_ai_target_usd_per_day=0.25)
    now = time.time()
    db.execute(
        """INSERT INTO factory_sources
        (source_id,source_type,source_name,url,title,published_ts,discovered_ts,
         content_hash,summary,raw_text,status,credibility,expires_ts)
        VALUES('s1','forum','reddit','https://example.test','Idea',?,?,?,?,?,
        'new',.5,?)""",
        (now, now, "hash", "summary", "raw", now + 100),
    )
    db.commit()
    monkeypatch.setattr(
        ai, "review_factory_sources",
        lambda *args, **kwargs: (
            {
                "candidates": [{
                    "title": "Testable idea", "venue": "polymarket",
                    "family": "other", "thesis": "x", "required_data": "y",
                    "deterministic_test": "z", "strongest_risk": "r",
                    "source_ids": ["s1"], "evidence_confidence": 55,
                }],
                "discarded_as_promotional_or_unfalsifiable": 0,
                "summary": "one",
            },
            {"estimated_cost_usd": 0.001},
        ),
    )
    result = research_new_sources(db, cfg)
    assert result["created"] == 1
    row = db.execute("SELECT lifecycle,strategy_version_id FROM factory_hypotheses").fetchone()
    assert tuple(row) == ("idea", None)


def test_config_validation_rejects_nan_and_ai_over_one_dollar():
    with pytest.raises(ValueError):
        validate_config(replace(CFG, live_risk_fraction=math.nan))
    with pytest.raises(ValueError):
        validate_config(replace(CFG, max_ai_usd_per_day=1.01))


def test_polymarket_public_parser_is_conservative_about_depth():
    raw = {
        "id": "42", "conditionId": "condition", "slug": "will-it",
        "question": "Will it happen?", "description": "Resolves by source.",
        "enableOrderBook": True, "active": True, "acceptingOrders": True,
        "closed": False, "outcomes": '["Yes","No"]',
        "outcomePrices": '["0.41","0.59"]', "bestBid": 0.40,
        "bestAsk": 0.42, "orderMinSize": 5, "volumeNum": 1000,
        "liquidityNum": 500, "feesEnabled": True,
        "feeSchedule": {"rate": 0.05},
        "events": [{"id": "e1", "title": "Event", "negRisk": True}],
    }
    market = PolymarketPublic(CFG)._parse_market(raw)
    assert market is not None and market.is_open
    assert market.yes_ask == pytest.approx(0.42)
    assert market.no_ask == pytest.approx(0.60)
    assert market.yes_ask_size == market.no_ask_size == 5
    assert market.fee_coefficient == pytest.approx(0.05)
    assert market.mutually_exclusive is True


def test_maintenance_creates_a_verified_compressed_backup(tmp_path):
    db, cfg = fresh(tmp_path)
    result = run_maintenance(db, cfg)
    backup = result["backup"]
    assert backup["state"] == "created" and backup["integrity"] == "ok"
    path = cfg.db_path.parent / "backups" / "factory"
    assert len(list(path.glob("factory-*.db.gz"))) == 1
    assert not list(path.glob("*.restore.db"))


def test_maintenance_removes_abandoned_backup_work(tmp_path):
    db, cfg = fresh(tmp_path)
    backup_dir = cfg.db_path.parent / "backups" / "factory"
    backup_dir.mkdir(parents=True)
    abandoned = backup_dir / ".factory-20000101.db.tmp"
    abandoned.write_bytes(b"abandoned")
    (backup_dir / ".factory-20000101.db.tmp-journal").write_bytes(b"journal")

    result = run_maintenance(db, cfg)

    assert result["backup"]["stale_files_removed"] == 2
    assert result["backup"]["stale_bytes_removed"] == 16
    assert not abandoned.exists()
    assert not list(backup_dir.glob(".*.tmp"))


def test_start_run_recovers_abandoned_rows(tmp_path):
    db, _ = fresh(tmp_path)
    old = time.time() - 7200
    db.execute(
        """INSERT INTO factory_runs
        (run_key,kind,scheduled_ts,started_ts,state)
        VALUES('factory_cycle:old','factory_cycle',?,?,'running')""",
        (old, old),
    )
    db.commit()

    run_id, _ = _start_run(db, "factory_cycle", 900)

    assert run_id is not None
    recovered = db.execute(
        "SELECT state,error,finished_ts FROM factory_runs WHERE run_key='factory_cycle:old'"
    ).fetchone()
    assert recovered["state"] == "failed"
    assert "Recovered stale run" in recovered["error"]
    assert recovered["finished_ts"] is not None


def test_stale_submission_is_recovered_from_one_venue_trade(tmp_path, monkeypatch):
    db, cfg = fresh(tmp_path)
    created = time.time() - 30
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,evidence_json,strategy_version_id)
        VALUES('restart','condition','token','up',?,?,'submitting',5,.5,2.5,20,'{}',?)""",
        (created, created, BTC_V8_STRATEGY_ID),
    )
    db.commit()

    class TradeParams:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    module = types.ModuleType("py_clob_client_v2.clob_types")
    module.TradeParams = TradeParams
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", module)

    class Client:
        def get_trades(self, params, only_first_page=False):
            assert params.kwargs["market"] == "condition"
            assert only_first_page is False
            return [{
                "asset_id": "token", "side": "BUY", "size": "5", "price": ".5",
                "id": "trade-1", "status": "MATCHED",
            }]

    assert LiveBroker(cfg).reconcile_stale_submissions(db, Client()) == 1
    row = db.execute(
        "SELECT state,actual_qty,order_id FROM fast_live_orders WHERE slug='restart'"
    ).fetchone()
    assert tuple(row) == ("matched", 5.0, "trade-1")


def _uncertain_broker(db, created):
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,evidence_json,strategy_version_id)
        VALUES('stuck','condition','token','up',?,?,'uncertain',5,.5,2.5,20,'{}',?)""",
        (created, created, BTC_V8_STRATEGY_ID),
    )
    db.commit()


def test_uncertain_order_without_venue_trade_is_rejected(tmp_path, monkeypatch):
    db, cfg = fresh(tmp_path)
    _uncertain_broker(db, time.time() - 1200)
    db.execute(
        """INSERT INTO fast_window_results (slug,start_ts,end_ts,opening_price,
        ending_price,observed_result,venue_result,result_matches,validated_ts,evidence_json)
        VALUES('stuck',0,0,1,1,'up','up',1,?,'{}')""",
        (time.time(),),
    )
    db.commit()

    module = types.ModuleType("py_clob_client_v2.clob_types")

    class TradeParams:
        def __init__(self, **kwargs):
            pass

    module.TradeParams = TradeParams
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", module)

    class Client:
        def get_trades(self, params, only_first_page=False):
            return []

    broker = LiveBroker(cfg)
    monkeypatch.setattr(LiveBroker, "_make_client", lambda self: Client())
    assert broker.reconcile_uncertain_orders(db) == 1
    row = db.execute(
        "SELECT state,error FROM fast_live_orders WHERE slug='stuck'"
    ).fetchone()
    assert row["state"] == "rejected"
    assert "no venue trade" in row["error"]


def test_uncertain_order_with_unique_venue_trade_is_matched(tmp_path, monkeypatch):
    db, cfg = fresh(tmp_path)
    _uncertain_broker(db, time.time() - 1200)

    module = types.ModuleType("py_clob_client_v2.clob_types")

    class TradeParams:
        def __init__(self, **kwargs):
            pass

    module.TradeParams = TradeParams
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", module)

    class Client:
        def get_trades(self, params, only_first_page=False):
            return [{
                "asset_id": "token", "side": "BUY", "size": "5", "price": ".5",
                "id": "trade-1", "status": "MATCHED",
            }]

    broker = LiveBroker(cfg)
    monkeypatch.setattr(LiveBroker, "_make_client", lambda self: Client())
    assert broker.reconcile_uncertain_orders(db) == 1
    row = db.execute(
        "SELECT state,actual_qty FROM fast_live_orders WHERE slug='stuck'"
    ).fetchone()
    assert tuple(row) == ("matched", 5.0)


def test_uncertain_order_awaiting_window_stays_uncertain(tmp_path, monkeypatch):
    db, cfg = fresh(tmp_path)
    _uncertain_broker(db, time.time() - 1200)

    module = types.ModuleType("py_clob_client_v2.clob_types")

    class TradeParams:
        def __init__(self, **kwargs):
            pass

    module.TradeParams = TradeParams
    monkeypatch.setitem(sys.modules, "py_clob_client_v2", types.ModuleType("py_clob_client_v2"))
    monkeypatch.setitem(sys.modules, "py_clob_client_v2.clob_types", module)

    class Client:
        def get_trades(self, params, only_first_page=False):
            return []

    broker = LiveBroker(cfg)
    monkeypatch.setattr(LiveBroker, "_make_client", lambda self: Client())
    assert broker.reconcile_uncertain_orders(db) == 0
    row = db.execute(
        "SELECT state FROM fast_live_orders WHERE slug='stuck'"
    ).fetchone()
    assert row["state"] == "uncertain"


def test_database_integrity_failure_remains_latched(tmp_path):
    db, cfg = fresh(tmp_path)
    set_setting(db, "btc_runtime_heartbeat", time.time())
    db.execute(
        """INSERT INTO factory_health
        (check_name,checked_ts,status,consecutive_failures,detail)
        VALUES('database',?,'critical',1,'simulated corruption')""",
        (time.time(),),
    )
    db.commit()
    latched = healthcheck(db, replace(cfg, factory_disk_pause_free_gb=0.000001))
    assert latched["status"] == "paused"
    assert "database" in get_setting(db, "factory_live_pause")["reason"]
    set_setting(db, "btc_runtime_heartbeat", time.time())
    db.commit()
    cleared = healthcheck(
        db, replace(cfg, factory_disk_pause_free_gb=0.000001), include_integrity=True
    )
    assert cleared["status"] == "healthy"


def test_bridge_cannot_bypass_factory_allocation_order_cap(tmp_path):
    db, cfg = fresh(tmp_path)
    cfg = replace(cfg, live_minimum_bridge_enabled=True)
    bootstrap_factory(db, cfg)
    db.execute(
        "UPDATE factory_allocations SET max_order_usd=2 WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    )
    db.commit()
    broker = LiveBroker(cfg)
    broker._preflight = {"balance_usd": 20, "allowance_usd": 20}
    budget, reason = broker.minimum_bridge_budget(
        db, 1.5, f"ready:{cfg.live_risk_fraction:.3f}:dd=0.000",
        5, 0.50, 0.0, 0.90,
    )
    assert budget == 1.5
    assert reason == "bridge_fraction_cap"


def test_reduced_tier_bridge_requires_six_percent_edge_and_eighteen_percent_cap(tmp_path):
    db, cfg = fresh(tmp_path)
    cfg = replace(
        cfg,
        live_minimum_bridge_enabled=True,
        live_reduced_bridge_max_fraction=0.18,
        live_reduced_bridge_min_edge=0.06,
    )
    bootstrap_factory(db, cfg)
    broker = LiveBroker(cfg)
    broker._preflight = {"balance_usd": 25.313686, "allowance_usd": 100}
    reduced = f"ready:{cfg.live_reduced_risk_fraction:.3f}:dd=0.150"

    bridged, reason = broker.minimum_bridge_budget(
        db, 2.5313686, reduced, 5, 0.79, 0.07, 0.90,
    )
    assert reason == "bridged_to_minimum"
    assert bridged == pytest.approx(5 * (0.79 + 0.07 * 0.79 * 0.21))

    unchanged, reason = broker.minimum_bridge_budget(
        db, 2.5313686, reduced, 5, 0.79, 0.07, 0.85,
    )
    assert unchanged == pytest.approx(2.5313686)
    assert reason == "insufficient_worst_cost_edge"

    capped, reason = broker.minimum_bridge_budget(
        db, 2.5313686, reduced, 5, 0.91, 0.07, 0.99,
    )
    assert capped == pytest.approx(2.5313686)
    assert reason == "bridge_fraction_cap"

    defensive = f"ready:{cfg.live_defensive_risk_fraction:.3f}:dd=0.210"
    blocked, reason = broker.minimum_bridge_budget(
        db, 1.8, defensive, 5, 0.60, 0.07, 0.95,
    )
    assert blocked == pytest.approx(1.8)
    assert reason == "risk_tier"


def test_automatic_btc_challengers_replay_without_live_promotion(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    set_setting(db, "fast_livebook_experiment_start", time.time() - 1000)
    db.commit()
    result = bootstrap_automation(db, cfg)
    assert result["challengers_created"] == 9
    assert db.execute(
        "SELECT COUNT(*) FROM factory_strategy_specs WHERE adapter='btc_terminal_v1'"
    ).fetchone()[0] == 10

    now = time.time()
    for index in range(3):
        slug = f"window-{index}"
        start = now - 900 + index * 300
        end = start + 300
        evidence = {
            "selected_live_book": True, "up_depth": 100, "down_depth": 100,
        }
        db.execute(
            """INSERT INTO fast_market_samples
            (ts,slug,start_ts,end_ts,seconds_left,price_to_beat,reference_price,
             margin_bps,sigma_per_s,up_probability,up_ask,down_ask,evidence_json)
            VALUES(?,?,?,?,10,100,101,100,.001,.80,.60,.42,?)""",
            (end - 10, slug, start, end, json.dumps(evidence)),
        )
        db.execute(
            """INSERT INTO fast_window_results
            (slug,start_ts,end_ts,opening_price,ending_price,observed_result,
             venue_result,result_matches,validated_ts,evidence_json)
            VALUES(?,?,?,?,?,'up','up',1,?,'{}')""",
            (slug, start, end, 100, 101, now),
        )
        db.execute(
            """INSERT INTO fast_paper_trades
            (slug,status,side,open_ts,close_ts,seconds_left,qty,entry_price,
             entry_fee,probability,model_edge,price_to_beat,reference_price,
             result,payout,realized_pnl,evidence_json,strategy_version_id)
            VALUES(?,'settled','up',?,?,10,7,.60,.02,.80,.18,100,101,'up',7,2.78,'{}',?)""",
            (slug, end - 10, end, BTC_V8_STRATEGY_ID),
        )
    db.commit()
    variants = run_btc_variants(db)
    assert variants["new_settled_trades"] > 0
    modes = dict(db.execute(
        "SELECT mode,COUNT(*) FROM factory_variant_trades GROUP BY mode"
    ).fetchall())
    assert modes["forward"] > 0  # Existing V8 cohort.
    assert modes["replay"] > 0  # Newly-created challengers cannot claim forward evidence.
    decisions = evaluate_specs(db)
    assert decisions["retain_live"] == 1
    assert decisions["continue_paper"] == 9
    assert allocate_live(db, cfg)["state"] == "retained"
    strategy_id, parameters = active_btc_spec(db, cfg)
    assert strategy_id == BTC_V8_STRATEGY_ID
    assert parameters["min_confidence"] == cfg.fast_v8_min_confidence


def test_confirmed_v8_challenger_has_fresh_forward_clock_and_filters_sources(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    set_setting(db, "fast_livebook_experiment_start", time.time() - 1000)
    db.commit()
    first = bootstrap_automation(db, cfg)
    assert first["challengers_created"] == 9
    assert bootstrap_automation(db, cfg)["challengers_created"] == 0
    confirmed = db.execute(
        """SELECT * FROM factory_strategy_specs
        WHERE adapter='btc_terminal_v1'
        AND json_extract(parameters_json,'$.require_source_confirmation')=1"""
    ).fetchone()
    assert confirmed is not None
    assert confirmed["state"] == "forward_paper"
    assert db.execute(
        "SELECT 1 FROM factory_allocations WHERE strategy_version_id=?",
        (confirmed["strategy_version_id"],),
    ).fetchone() is None

    created = float(confirmed["created_ts"])

    def add_window(slug, end, sources_agree):
        start = end - 300
        evidence = {
            "selected_live_book": True,
            "up_depth": 100,
            "down_depth": 100,
            "sources_agree": sources_agree,
        }
        db.execute(
            """INSERT INTO fast_market_samples
            (ts,slug,start_ts,end_ts,seconds_left,price_to_beat,reference_price,
             margin_bps,sigma_per_s,up_probability,up_ask,down_ask,evidence_json)
            VALUES(?,?,?,?,10,100,101,100,.001,.80,.60,.42,?)""",
            (end - 10, slug, start, end, json.dumps(evidence)),
        )
        db.execute(
            """INSERT INTO fast_window_results
            (slug,start_ts,end_ts,opening_price,ending_price,observed_result,
             venue_result,result_matches,validated_ts,evidence_json)
            VALUES(?,?,?,?,?,'up','up',1,?,'{}')""",
            (slug, start, end, 100, 101, time.time()),
        )
        db.execute(
            """INSERT INTO fast_paper_trades
            (slug,status,side,open_ts,close_ts,seconds_left,qty,entry_price,
             entry_fee,probability,model_edge,price_to_beat,reference_price,
             result,payout,realized_pnl,evidence_json,strategy_version_id)
            VALUES(?,'settled','up',?,?,10,7,.60,.02,.80,.18,100,101,
            'up',7,2.78,?,?)""",
            (slug, end - 10, end, json.dumps(evidence), BTC_V8_STRATEGY_ID),
        )

    add_window("historical-confirmed", created - 10, True)
    add_window("forward-confirmed", created + 10, True)
    add_window("forward-unconfirmed", created + 20, False)
    db.commit()
    run_btc_variants(db)
    rows = db.execute(
        """SELECT mode,slug FROM factory_variant_trades
        WHERE spec_id=? ORDER BY signal_ts""",
        (confirmed["spec_id"],),
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("replay", "historical-confirmed"),
        ("forward", "forward-confirmed"),
    ]


def test_quarantined_confirmed_family_gets_one_new_forward_version(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    original = db.execute(
        """SELECT spec_id FROM factory_strategy_specs
        WHERE json_extract(parameters_json,'$.require_source_confirmation')=1"""
    ).fetchone()
    db.execute(
        "UPDATE factory_strategy_specs SET state='quarantined' WHERE spec_id=?",
        (original["spec_id"],),
    )
    db.execute(
        "UPDATE factory_allocations SET state='inactive' WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    )
    db.commit()

    result = bootstrap_automation(db, cfg)
    assert result["challengers_created"] == 1
    successor = db.execute(
        """SELECT * FROM factory_strategy_specs
        WHERE json_extract(parameters_json,'$.lineage_revision')=2"""
    ).fetchone()
    assert successor["state"] == "forward_paper"
    assert successor["parent_spec_id"] == original["spec_id"]
    assert db.execute(
        "SELECT state FROM factory_strategy_specs WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0] == "retired"

    assert bootstrap_automation(db, cfg)["challengers_created"] == 0


def test_unsupported_hypothesis_is_blocked_automatically(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    now = time.time()
    db.execute(
        """INSERT INTO factory_hypotheses
        (hypothesis_key,created_ts,updated_ts,title,venue,family,thesis,
         required_data,deterministic_test,risks_json,source_ids_json,confidence,
         lifecycle,strategy_version_id,review_json)
        VALUES('weather',?,?,'Weather idea','polymarket','weather','x','forecast',
        'compare','[]','[]',60,'idea',NULL,'{}')""",
        (now, now),
    )
    db.commit()
    assert compile_hypotheses(db) == {"compiled": 0, "blocked": 1}
    row = db.execute(
        "SELECT lifecycle,strategy_version_id FROM factory_hypotheses WHERE hypothesis_key='weather'"
    ).fetchone()
    assert row["lifecycle"] == "blocked_adapter"
    assert row["strategy_version_id"].startswith("blocked_")


def test_allocator_atomically_promotes_a_qualified_challenger(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    baseline = db.execute(
        "SELECT spec_id FROM factory_strategy_specs WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0]
    challenger = db.execute(
        """SELECT spec_id,strategy_version_id FROM factory_strategy_specs
        WHERE adapter='btc_terminal_v1'
        AND json_extract(parameters_json,'$.require_source_confirmation')=1
        LIMIT 1""",
    ).fetchone()
    db.execute(
        "UPDATE factory_strategy_specs SET state='live_eligible' WHERE spec_id=?",
        (challenger["spec_id"],),
    )
    for spec_id, score in ((baseline, 5.0), (challenger["spec_id"], 20.0)):
        db.execute(
            """INSERT INTO factory_evaluations
            (spec_id,ts,replay_trades,forward_trades,forward_days,wins,pnl,
             capital_risked,roi,max_drawdown,profit_factor,score,decision,reason,metrics_json)
            VALUES(?,?,50,40,4,25,10,100,.10,.10,1.5,?,'live_eligible','test','{}')""",
            (spec_id, time.time(), score),
        )
    db.commit()
    result = allocate_live(db, cfg, ignore_quiet_window=True)
    assert result["state"] == "promoted"
    assert result["to"] == challenger["strategy_version_id"]
    active, _ = active_btc_spec(db, cfg)
    assert active == challenger["strategy_version_id"]
    old = db.execute(
        "SELECT state FROM factory_allocations WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0]
    assert old == "inactive"


def test_qualified_promotion_automatically_opens_bounded_fresh_risk_cohort(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    cfg = replace(
        cfg,
        live_auto_cohort_enabled=True,
        live_auto_cohort_loss_fraction=.20,
        live_auto_cohort_max_loss_usd=5,
        live_auto_cohort_cooldown_days=7,
        live_cohort_minimum_floor_enabled=True,
        live_trading_enabled=True,
        live_confirmation=LIVE_CONFIRMATION,
        polymarket_private_key="0x" + "1" * 64,
        polymarket_api_key="key",
        polymarket_api_secret="secret",
        polymarket_api_passphrase="pass",
        polymarket_signature_type=0,
    )
    now = time.time()
    set_setting(db, "live_session_initial_balance", 20.0)
    set_setting(db, "factory_live_pause", {"active": False})
    set_setting(
        db, "live_last_preflight",
        {"ok": True, "balance_usd": 20.0, "allowance_usd": 100.0,
         "checked_ts": now},
    )
    # Prior-account losses remain truthful and would stop the old strategy.
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,actual_qty,actual_cost_usd,
         result,realized_pnl,evidence_json,strategy_version_id)
        VALUES('old-loss','c','t','up',?,?,'settled',5,.5,10,20,5,10,'down',-10,
        '{}',?)""",
        (now - 100, now - 90, BTC_V8_STRATEGY_ID),
    )
    baseline = db.execute(
        "SELECT spec_id FROM factory_strategy_specs WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0]
    challenger = db.execute(
        """SELECT spec_id,strategy_version_id FROM factory_strategy_specs
        WHERE adapter='btc_terminal_v1'
        AND json_extract(parameters_json,'$.require_source_confirmation')=1
        LIMIT 1""",
    ).fetchone()
    db.execute(
        "UPDATE factory_strategy_specs SET state='live_eligible' WHERE spec_id=?",
        (challenger["spec_id"],),
    )
    for spec_id, score in ((baseline, 5.0), (challenger["spec_id"], 20.0)):
        db.execute(
            """INSERT INTO factory_evaluations
            (spec_id,ts,replay_trades,forward_trades,forward_days,wins,pnl,
             capital_risked,roi,max_drawdown,profit_factor,score,decision,reason,metrics_json)
            VALUES(?,?,50,40,4,30,10,100,.10,.10,1.5,?,'live_eligible','test','{}')""",
            (spec_id, now, score),
        )
    db.commit()

    promoted = allocate_live(db, cfg, ignore_quiet_window=True)
    assert promoted["state"] == "promoted"
    assert promoted["risk_cohort_id"] is not None
    assert promoted["risk_cohort_loss_cap_usd"] == pytest.approx(4.0)
    cohort = active_risk_cohort(db, challenger["strategy_version_id"])
    assert cohort is not None
    assert cohort["authorization_source"] == "automatic_fixed_evidence_gates"

    broker = LiveBroker(cfg)
    broker._preflight = {
        "ok": True, "reason": "ready", "balance_usd": 20.0,
        "allowance_usd": 100.0, "checked_ts": time.time(),
    }
    budget, reason = broker.budget(db, challenger["strategy_version_id"])
    assert budget == pytest.approx(3.0)
    assert reason.startswith("ready:0.150:") and "cohort=" in reason
    minimum_budget, minimum_reason = broker.minimum_bridge_budget(
        db, budget, reason, 5, .71, .07, .719,
        challenger["strategy_version_id"],
    )
    assert minimum_reason == "cohort_minimum_floor"
    assert minimum_budget == pytest.approx(5 * (.71 + .07 * .71 * .29))
    capped_budget, capped_reason = broker.minimum_bridge_budget(
        db, budget, reason, 5, .82, .07, .95,
        challenger["strategy_version_id"],
    )
    assert capped_budget == budget
    assert capped_reason == "cohort_minimum_floor_cap"

    order_ts = float(cohort["created_ts"]) + 1.0
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,requested_qty,
         limit_price,risk_budget_usd,balance_before_usd,actual_qty,actual_cost_usd,
         result,realized_pnl,evidence_json,strategy_version_id,risk_cohort_id)
        VALUES('cohort-loss','c','t','up',?,?,'settled',5,.5,4,20,5,4,'down',-4,
        '{}',?,?)""",
        (order_ts, order_ts, challenger["strategy_version_id"], cohort["id"]),
    )
    db.commit()
    assert risk_cohort_snapshot(db, cohort)["remaining_loss_usd"] == 0
    assert broker.budget(db, challenger["strategy_version_id"]) == (
        0.0, "cohort_loss_stop"
    )
    assert db.execute(
        "SELECT state FROM live_risk_cohorts WHERE id=?", (cohort["id"],)
    ).fetchone()[0] == "stopped"


def test_net_cohort_stop_uses_profit_before_starting_loss_allowance(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    cur = db.execute(
        """INSERT INTO live_risk_cohorts
        (strategy_version_id,created_ts,state,authorization_source,
         wallet_balance_start_usd,loss_cap_usd,cooldown_until_ts,evidence_json)
        VALUES(?,?,'active','test',20,4,?,'{}')""",
        (BTC_V8_STRATEGY_ID, now, now + 86400),
    )
    cohort_id = int(cur.lastrowid)
    for slug, pnl in (("cohort-win", 5.0), ("cohort-giveback", -4.0)):
        db.execute(
            """INSERT INTO fast_live_orders
            (slug,condition_id,token_id,side,created_ts,updated_ts,state,
             requested_qty,limit_price,risk_budget_usd,balance_before_usd,
             actual_qty,actual_cost_usd,result,realized_pnl,evidence_json,
             strategy_version_id,risk_cohort_id)
            VALUES(?,?,?,?,?,?,'settled',5,.5,4,20,5,4,'down',?,'{}',?,?)""",
            (slug, "c", "t", "up", now, now, pnl, BTC_V8_STRATEGY_ID, cohort_id),
        )
    db.commit()
    cohort = db.execute(
        "SELECT * FROM live_risk_cohorts WHERE id=?", (cohort_id,)
    ).fetchone()
    trailing = risk_cohort_snapshot(db, cohort, trailing_loss_cap=True)
    net = risk_cohort_snapshot(db, cohort, trailing_loss_cap=False)
    assert trailing["remaining_loss_usd"] == 0
    assert net["realized_pnl_usd"] == 1
    assert net["remaining_loss_usd"] == 5
    assert cohort_risk_fraction(net, cfg)[0] == cfg.live_risk_fraction

    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,
         requested_qty,limit_price,risk_budget_usd,balance_before_usd,
         actual_qty,actual_cost_usd,result,realized_pnl,evidence_json,
         strategy_version_id,risk_cohort_id)
        VALUES('cohort-net-stop','c','t','up',?,?,'settled',5,.5,5,20,5,5,
        'down',-5,'{}',?,?)""",
        (now + 1, now + 1, BTC_V8_STRATEGY_ID, cohort_id),
    )
    db.commit()
    net = risk_cohort_snapshot(db, cohort, trailing_loss_cap=False)
    assert net["realized_pnl_usd"] == -4
    assert net["remaining_loss_usd"] == 0
    assert cohort_risk_fraction(net, cfg)[0] == 0


def test_losing_live_strategy_is_quarantined_and_allocation_paused(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    for index in range(30):
        db.execute(
            """INSERT INTO fast_live_orders
            (slug,condition_id,token_id,side,created_ts,updated_ts,state,
             requested_qty,limit_price,risk_budget_usd,balance_before_usd,
             actual_qty,actual_cost_usd,result,realized_pnl,evidence_json,
             strategy_version_id)
            VALUES(?,?,?,?,?,?,'settled',2,.5,1,20,2,1,'down',-.2,'{}',?)""",
            (
                f"loss-{index}", "condition", "token", "up",
                now + index, now + index, BTC_V8_STRATEGY_ID,
            ),
        )
    db.commit()
    decisions = evaluate_specs(db)
    assert decisions["quarantine"] == 1
    allocation = allocate_live(db, cfg)
    assert allocation["state"] == "paused_no_replacement"
    state = db.execute(
        "SELECT state FROM factory_allocations WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0]
    assert state == "inactive"
    challenger = db.execute(
        """SELECT spec_id,strategy_version_id FROM factory_strategy_specs
        WHERE adapter='btc_terminal_v1' AND state='forward_paper' LIMIT 1"""
    ).fetchone()
    db.execute(
        "UPDATE factory_strategy_specs SET state='live_eligible' WHERE spec_id=?",
        (challenger["spec_id"],),
    )
    db.execute(
        """INSERT INTO factory_evaluations
        (spec_id,ts,replay_trades,forward_trades,forward_days,wins,pnl,
         capital_risked,roi,max_drawdown,profit_factor,score,decision,reason,metrics_json)
        VALUES(?,?,50,40,4,25,10,100,.10,.10,1.5,20,'live_eligible','test','{}')""",
        (challenger["spec_id"], time.time()),
    )
    db.commit()
    replacement = allocate_live(db, cfg, ignore_quiet_window=True)
    assert replacement["state"] == "promoted"
    assert replacement["from"] is None
    assert replacement["to"] == challenger["strategy_version_id"]


def _insert_live_order(
    db, slug, *, ts, state="settled", pnl=None, cost=1.0,
    version=BTC_V8_STRATEGY_ID, cohort_id=None,
):
    db.execute(
        """INSERT INTO fast_live_orders
        (slug,condition_id,token_id,side,created_ts,updated_ts,state,
         requested_qty,limit_price,risk_budget_usd,balance_before_usd,
         actual_qty,actual_cost_usd,result,realized_pnl,evidence_json,
         strategy_version_id,risk_cohort_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'{}',?,?)""",
        (
            slug, "condition", "token", "up", ts, ts, state,
            2, 0.5, cost, 20.0, 2, cost,
            ("down" if pnl is not None and pnl < 0 else "up"), pnl, version, cohort_id,
        ),
    )


def test_live_quarantine_deferred_while_orders_await_settlement(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    for index in range(30):
        _insert_live_order(db, f"loss-{index}", ts=now + index, pnl=-0.2)
    _insert_live_order(db, "pending-win", ts=now + 100, state="filled", pnl=None)
    db.commit()
    decisions = evaluate_specs(db)
    assert decisions["retain_live"] == 1
    state = db.execute(
        "SELECT state FROM factory_strategy_specs WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()[0]
    assert state == "live"
    # Once the pending order settles, evaluation is no longer deferred.
    db.execute(
        "UPDATE fast_live_orders SET state='settled',realized_pnl=-0.2,result='down' "
        "WHERE slug='pending-win'"
    )
    db.commit()
    decisions = evaluate_specs(db)
    assert decisions["quarantine"] == 1


def test_recovered_drawdown_does_not_quarantine_positive_live_strategy(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    ts = now
    for index in range(6):
        _insert_live_order(db, f"loss-{index}", ts=ts, pnl=-1.0)
        ts += 1
    for index in range(8):
        _insert_live_order(db, f"win-{index}", ts=ts, pnl=1.0)
        ts += 1
    db.commit()
    decisions = evaluate_specs(db)
    assert decisions["retain_live"] == 1
    metrics = json.loads(db.execute(
        "SELECT metrics_json FROM factory_evaluations WHERE spec_id=("
        "SELECT spec_id FROM factory_strategy_specs WHERE strategy_version_id=?)"
        " ORDER BY id DESC LIMIT 1", (BTC_V8_STRATEGY_ID,)
    ).fetchone()[0])
    assert metrics["actual_live"]["max_drawdown"] >= 0.30
    assert metrics["actual_live"]["drawdown"] == 0.0


def test_live_evaluation_spans_lineage_from_first_cohort_start(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    db.execute(
        """INSERT INTO live_risk_cohorts
        (strategy_version_id,created_ts,state,authorization_source,
         wallet_balance_start_usd,loss_cap_usd,cooldown_until_ts,evidence_json)
        VALUES(?,?,'superseded','test',10.0,2.0,?,'{}')""",
        (BTC_V8_STRATEGY_ID, now - 1000, now),
    )
    for index in range(3):
        _insert_live_order(db, f"parent-win-{index}", ts=now - 900 + index, pnl=2.0)
    parent = db.execute(
        "SELECT * FROM factory_strategy_specs WHERE strategy_version_id=?",
        (BTC_V8_STRATEGY_ID,),
    ).fetchone()
    child_version = "btc_child_lineage_test"
    child_spec = "spec_child_lineage_test"
    db.execute(
        """INSERT INTO factory_strategy_versions
        (strategy_version_id,strategy_key,version,name,venue,family,lifecycle,
         live_allowed,created_ts,updated_ts,code_hash,config_hash,spec_json)
        SELECT ?,strategy_key,'child',name,venue,family,'live',1,created_ts,
        updated_ts,code_hash,'child-config-hash',spec_json
        FROM factory_strategy_versions WHERE strategy_version_id=?""",
        (child_version, BTC_V8_STRATEGY_ID),
    )
    db.execute(
        """INSERT INTO factory_strategy_specs
        (spec_id,strategy_version_id,adapter,family,generation,parent_spec_id,
         created_ts,updated_ts,state,live_capable,parameters_json,
         data_requirements_json,spec_hash)
        VALUES(?,?,?,?,?,?,?,?,'live',1,?,'{}','test-hash')""",
        (
            child_spec, child_version, parent["adapter"], parent["family"],
            parent["generation"] + 1, parent["spec_id"], now, now,
            parent["parameters_json"],
        ),
    )
    # Child-only record breaches the ROI rule; lineage-wide record does not.
    for index in range(5):
        _insert_live_order(
            db, f"child-loss-{index}", ts=now + index, pnl=-0.3,
            version=child_version,
        )
    db.commit()
    decisions = evaluate_specs(db)
    assert decisions["retain_live"] == 2  # parent and child both live
    assert decisions.get("quarantine", 0) == 0
    metrics = json.loads(db.execute(
        "SELECT metrics_json FROM factory_evaluations WHERE spec_id=?"
        " ORDER BY id DESC LIMIT 1", (child_spec,)
    ).fetchone()[0])
    assert metrics["actual_live"]["trades"] == 8
    assert metrics["actual_live"]["pnl"] == pytest.approx(6.0 - 1.5)


def test_live_circuit_breaker_trips_and_resumes_on_paper_evidence(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    # From $20: +4 to a $24 peak, then -7 -> $17 (29% drawdown), positive-ish ROI.
    _insert_live_order(db, "win", ts=now - 100, pnl=4.0, cost=4.0)
    for index in range(7):
        _insert_live_order(db, f"loss-{index}", ts=now - 90 + index, pnl=-1.0, cost=1.0)
    db.commit()
    decisions = evaluate_specs(db, cfg)
    assert decisions["retain_live"] == 1  # not a terminal quarantine
    breaker = get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")
    assert breaker["active"] is True
    assert BTC_V8_STRATEGY_ID in breaker["lineage"]
    broker = LiveBroker(replace(cfg, live_trading_enabled=True))
    assert broker.budget(db, BTC_V8_STRATEGY_ID)[1] in (
        "live_circuit_breaker", "live_disabled", "live_not_armed",
    ) or broker.budget(db, BTC_V8_STRATEGY_ID)[0] == 0.0
    # Paper keeps running during the break; non-negative evidence resumes live.
    trip = breaker["tripped_ts"]
    for index in range(cfg.live_breaker_resume_min_paper_trades):
        db.execute(
            """INSERT INTO fast_paper_trades
            (slug,status,side,open_ts,close_ts,seconds_left,qty,entry_price,
             entry_fee,probability,model_edge,price_to_beat,reference_price,
             result,payout,realized_pnl,evidence_json,strategy_version_id)
            VALUES(?,'settled','up',?,?,10,5,.6,.02,.8,.1,100,101,'up',5,1.9,'{}',?)""",
            (f"paper-{index}", trip + 1, trip + 2 + index, BTC_V8_STRATEGY_ID),
        )
    db.commit()
    evaluate_specs(db, cfg)
    breaker = get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")
    assert breaker["active"] is False
    assert breaker["anchor_equity"] == pytest.approx(17.0)
    # Resuming re-anchors the peak so the old drawdown cannot re-trip it.
    evaluate_specs(db, cfg)
    assert get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")["active"] is False


def test_breaker_resume_reanchors_risk_cohort_ledger(tmp_path):
    db, cfg = fresh(tmp_path)
    bootstrap_factory(db, cfg)
    bootstrap_automation(db, cfg)
    now = time.time()
    db.execute(
        """INSERT INTO live_risk_cohorts
        (strategy_version_id,created_ts,state,authorization_source,
         wallet_balance_start_usd,loss_cap_usd,cooldown_until_ts,evidence_json)
        VALUES(?,?,'active','test',20.0,3.5,?,'{}')""",
        (BTC_V8_STRATEGY_ID, now - 2000, now),
    )
    # Peak $24 then -$7 -> 29% drawdown trips the breaker.
    cohort_id = db.execute(
        "SELECT id FROM live_risk_cohorts WHERE state='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    _insert_live_order(db, "win", ts=now - 100, pnl=4.0, cost=4.0, cohort_id=cohort_id)
    for index in range(7):
        _insert_live_order(db, f"loss-{index}", ts=now - 90 + index, pnl=-1.0, cost=1.0, cohort_id=cohort_id)
    db.commit()
    evaluate_specs(db, cfg)
    assert get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")["active"] is True
    # Old ledger is deep in loss and would pin the defensive tier forever.
    from opportunity_lab.live import active_risk_cohort, risk_cohort_snapshot, cohort_risk_fraction
    old_cohort = active_risk_cohort(db, BTC_V8_STRATEGY_ID)
    snap = risk_cohort_snapshot(db, old_cohort, trailing_loss_cap=False)
    assert cohort_risk_fraction(snap, cfg)[0] == cfg.live_defensive_risk_fraction
    # Paper recovery resumes live and re-anchors the loss ledger.
    trip = get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")["tripped_ts"]
    for index in range(cfg.live_breaker_resume_min_paper_trades):
        db.execute(
            """INSERT INTO fast_paper_trades
            (slug,status,side,open_ts,close_ts,seconds_left,qty,entry_price,
             entry_fee,probability,model_edge,price_to_beat,reference_price,
             result,payout,realized_pnl,evidence_json,strategy_version_id)
            VALUES(?,'settled','up',?,?,10,5,.6,.02,.8,.1,100,101,'up',5,1.9,'{}',?)""",
            (f"paper-{index}", trip + 1, trip + 2 + index, BTC_V8_STRATEGY_ID),
        )
    db.commit()
    evaluate_specs(db, cfg)
    breaker = get_setting(db, f"live_circuit_breaker:{BTC_V8_STRATEGY_ID}")
    assert breaker["active"] is False
    assert breaker["reanchored_cohort_id"] is not None
    new_cohort = active_risk_cohort(db, BTC_V8_STRATEGY_ID)
    assert new_cohort["id"] == old_cohort["id"]  # ledger reset in place
    assert json.loads(new_cohort["evidence_json"])["prior_start_usd"] == pytest.approx(20.0)
    assert new_cohort["wallet_balance_start_usd"] == pytest.approx(17.0)
    assert new_cohort["loss_cap_usd"] == pytest.approx(17.0 * cfg.live_auto_cohort_loss_fraction)
    snap = risk_cohort_snapshot(db, new_cohort, trailing_loss_cap=False)
    assert cohort_risk_fraction(snap, cfg)[0] == cfg.live_risk_fraction
