"""Phase 6 through the CLI: the operator view, brakes, live checks, retirement, lineage."""

import json
from pathlib import Path

from typer.testing import CliRunner

from veena.cli import app

runner = CliRunner()
SPECS = Path(__file__).resolve().parents[1] / "src" / "veena" / "scenario" / "specs"
AS_OF = ["--as-of", "2026-10-01T00:00:00+00:00"]


def _shared(tmp_path):
    registry = tmp_path / "registry.sqlite"
    scenario = tmp_path / "scenario.duckdb"
    assert runner.invoke(app, ["scenario", "build", "--path", str(scenario)]).exit_code == 0
    common = ["--registry", str(registry), "--scenario", str(scenario)]
    for spec, consumer in (
        ("customer_spend_30d", "app:sales"),
        ("customer_spend_90d", "agent:support"),
    ):
        r = runner.invoke(
            app, ["asset", "request", str(SPECS / f"{spec}.yaml"), "--consumer", consumer, *common]
        )
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["asset", "activate", spec, *common, *AS_OF])
        assert r.exit_code == 0, r.output
    return common


def test_ops_view_has_the_four_sections_then_lags_and_brakes(tmp_path):
    common = _shared(tmp_path)
    result = runner.invoke(app, ["ops", *common, *AS_OF])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == "operator view as of 2026-10-01T00:00:00+00:00"
    assert lines[2] == "unmet commitments" and lines[3] == "  none"
    order = [
        lines.index(h)
        for h in (
            "unmet commitments",
            "affected consumers",
            "recent deployments touching them",
            "what the engine proposes next",
            "lag per asset and source (source lag | incorporation lag)",
            "active brakes",
        )
    ]
    assert order == sorted(order)
    assert "customer_spend_daily_d1" in result.output
    assert "customer_spend_90d         purchases" in result.output


def test_drift_check_repair_pin_trail_and_brakes(tmp_path):
    common = _shared(tmp_path)
    drift = runner.invoke(app, ["scenario", "drift", "mapping_change", *common[2:]])
    assert drift.exit_code == 0 and "moved from" in drift.output

    escalated = runner.invoke(
        app,
        ["asset", "check", "customer_spend_90d", *common, *AS_OF, "--min-probability", "0.9"],
    )
    assert escalated.exit_code == 3, escalated.output
    assert "FAIL reconcile_spend_usd" in escalated.output
    assert "triage identity_mapping_change p=0.75, strategy escalate [rules]" in escalated.output
    assert "opened question customer_spend_90d_q1; the commitment is degraded" in escalated.output

    view = runner.invoke(app, ["ops", *common, *AS_OF])
    assert "customer_spend_90d:agent:support   verification [degraded]" in view.output
    assert "agent:support            customer_spend_90d:agent:support" in view.output
    assert "degraded assets" in view.output
    assert "consumers read customer_spend_90d__v1__i1" in view.output
    assert "question customer_spend_90d_q1" in view.output

    trail = runner.invoke(app, ["ops", "trail", "customer_spend_90d:agent:support", *common[:2]])
    assert trail.exit_code == 0, trail.output
    headings = [line for line in trail.output.splitlines() if not line.startswith(" ")]
    assert headings == ["deployment", "proposal", "verification", "specification", "decisions"]
    assert len(trail.output.splitlines()) <= 30
    assert "by engine (policy:routine_activation@1)" in trail.output
    narrated = runner.invoke(
        app, ["ops", "trail", "customer_spend_90d:agent:support", "--narrate", *common[:2]]
    )
    assert "narration: no drafter configured" in narrated.output

    pinned = runner.invoke(
        app,
        [
            "ops",
            "pin",
            "customer_spend_30d",
            "customer_spend_30d__v1__i1",
            "--reason",
            "safe",
            *common,
        ],
    )
    assert pinned.exit_code == 0, pinned.output
    repaired = runner.invoke(app, ["asset", "check", "customer_spend_90d", *common, *AS_OF])
    assert repaired.exit_code == 0, repaired.output
    assert "strategy re_plan_join [rules]" in repaired.output
    assert "activated customer_spend_90d via customer_spend_daily_d2 (repair)" in repaired.output

    brakes = runner.invoke(app, ["ops", "brakes", *common[:2], *AS_OF])
    assert "pin    customer_spend_30d" in brakes.output and "user:oncall" in brakes.output
    unpinned = runner.invoke(
        app, ["ops", "unpin", "customer_spend_30d", "--reason", "verified", *common]
    )
    assert "interface at customer_spend_30d__v1__i3" in unpinned.output

    frozen = runner.invoke(
        app, ["ops", "freeze", "customer_spend_daily", "--subgraph", "--reason", "close", *common]
    )
    assert "frozen customer_spend_daily and its dependents" in frozen.output
    view = runner.invoke(app, ["ops", *common, *AS_OF])
    assert "freeze customer_spend_daily" in view.output.replace("  ", " ")
    assert "[customer_spend_daily_d2 activated after this question]" in view.output
    assert runner.invoke(app, ["ops", "unfreeze", "customer_spend_daily", *common]).exit_code == 0

    clean = runner.invoke(app, ["asset", "check", "customer_spend_90d", *common, *AS_OF])
    assert clean.exit_code == 0 and "passed; grade reconciliation" in clean.output


def test_replay_deployment_show_rollback_ack(tmp_path):
    common = _shared(tmp_path)
    replay = runner.invoke(
        app,
        [
            "proposal",
            "replay",
            "customer_spend_daily_p1",
            "--at",
            "2026-09-11T00:00:00+00:00",
            *common,
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert replay.output.count("replay of ") == 3
    assert "replay passed; recorded as replay verifications" in replay.output

    shown = runner.invoke(
        app,
        [
            "deployment",
            "show",
            "customer_spend_daily_d1",
            *common[:2],
            "--as-of",
            "2026-12-01T00:00:00+00:00",
        ],
    )
    assert shown.exit_code == 0, shown.output
    assert "backend duckdb" in shown.output
    assert "source position purchases @" in shown.output
    assert (
        "rendered SQL for customer_spend_daily: implementation customer_spend_daily__v1__i1"
        in shown.output
    )
    assert "WARNING retention source:purchases: expired" in shown.output
    assert "retention sql: kept forever" in shown.output
    fresh = runner.invoke(
        app, ["deployment", "show", "customer_spend_daily_d1", *common[:2], *AS_OF]
    )
    assert "WARNING" not in fresh.output

    rolled = runner.invoke(
        app,
        [
            "deployment",
            "rollback",
            "customer_spend_daily_d1",
            "--reason",
            "bad totals",
            *common,
            *AS_OF,
        ],
    )
    assert rolled.exit_code == 0, rolled.output
    view = runner.invoke(app, ["ops", *common, *AS_OF])
    assert "assess impact" in view.output
    assert "agent:support, app:sales may have read rolled-back data" in view.output
    acked = runner.invoke(
        app, ["ops", "ack", "customer_spend_daily_d1", "--reason", "support re-ran", *common]
    )
    assert acked.exit_code == 0, acked.output
    assert "assess impact" not in runner.invoke(app, ["ops", *common, *AS_OF]).output
    again = runner.invoke(
        app, ["ops", "ack", "customer_spend_daily_d1", "--reason", "twice", *common]
    )
    assert again.exit_code == 1


def test_release_retire_and_export(tmp_path):
    common = _shared(tmp_path)
    listed = runner.invoke(app, ["commitment", "list", *common[:2]])
    assert "customer_spend_30d:app:sales" in listed.output and "open" in listed.output
    released = runner.invoke(
        app, ["commitment", "release", "customer_spend_30d:app:sales", *common[:2]]
    )
    assert released.exit_code == 0, released.output

    plan = runner.invoke(app, ["retire", "plan", *common[:2], *AS_OF])
    assert "customer_spend_30d           next: stop_refresh" in plan.output
    assert (
        "waits before retire" in plan.output and "open commitment(s): agent:support" in plan.output
    )
    assert "read by customer_spend_30d, customer_spend_90d" in plan.output

    first = runner.invoke(app, ["retire", "advance", *common, *AS_OF])
    assert "stop_refresh" in first.output
    second = runner.invoke(app, ["retire", "advance", *common, *AS_OF])
    assert (
        "replace_with_view" in second.output and "view over customer_spend_daily" in second.output
    )
    held = runner.invoke(app, ["retire", "advance", *common, *AS_OF])
    assert "no asset is ready" in held.output
    later = runner.invoke(
        app,
        ["retire", "advance", *common, "--as-of", "2026-11-15T00:00:00+00:00", "--until-stable"],
    )
    assert "drop_materialization" in later.output and "retire " in later.output
    shown = runner.invoke(app, ["asset", "show", "customer_spend_30d", *common[:2]])
    assert "status=retired" in shown.output
    assert "spec customer_spend_30d@1" in shown.output
    assert "deployment customer_spend_30d_d1" in shown.output

    assert (
        runner.invoke(
            app, ["commitment", "release", "customer_spend_90d:agent:support", *common[:2]]
        ).exit_code
        == 0
    )
    rest = runner.invoke(
        app,
        ["retire", "advance", *common, "--as-of", "2026-12-20T00:00:00+00:00", "--until-stable"],
    )
    lines = [line.split()[:2] for line in rest.output.splitlines()]
    assert lines[-1] == ["customer_spend_daily", "retire"]
    assert ["customer_spend_90d", "retire"] in lines
    assets = runner.invoke(app, ["registry", "assets", *common[:2]])
    assert assets.output.count("retired") == 3

    out = tmp_path / "lineage"
    exported = runner.invoke(app, ["export", "lineage", *common[:2], "--out", str(out)])
    assert exported.exit_code == 0, exported.output
    assert "both documents pass the structural checks" in exported.output
    events = json.loads((out / "openlineage.json").read_text())
    prov = json.loads((out / "prov.json").read_text())
    assert len(events) == 4 and "veena:asset/customer_spend_daily" in prov["entity"]


def test_calibration_for_the_triage_site(tmp_path):
    result = runner.invoke(
        app,
        [
            "models",
            "calibration",
            "triage_failure",
            "--classifier",
            "rules",
            "--log-dir",
            str(tmp_path / "cal"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "calibration of triage_failure (rules): 6 labeled predictions" in result.output
    assert (tmp_path / "cal" / "triage_failure.jsonl").exists()
    unknown = runner.invoke(app, ["models", "calibration", "nope"])
    assert unknown.exit_code == 1 and "triage_failure exist" in unknown.output


def test_drift_rejects_unknown_kind_and_missing_scenario(tmp_path):
    result = runner.invoke(
        app, ["scenario", "drift", "nope", "--scenario", str(tmp_path / "missing.duckdb")]
    )
    assert result.exit_code == 1 and "unknown drift" in result.output
    result = runner.invoke(
        app, ["scenario", "drift", "mapping_change", "--scenario", str(tmp_path / "missing.duckdb")]
    )
    assert result.exit_code == 1 and "no scenario database" in result.output
