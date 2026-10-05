from typer.testing import CliRunner

from veena import __version__
from veena.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_spec_validate_prints_canonical_yaml(spec_dir):
    result = runner.invoke(app, ["spec", "validate", str(spec_dir / "customer_spend_30d.yaml")])
    assert result.exit_code == 0
    assert "asset_id: customer_spend_30d" in result.output
    assert "semantic:purchase_amount_usd@4" in result.output


def test_spec_compare_reports_operations(spec_dir):
    result = runner.invoke(
        app,
        [
            "spec",
            "compare",
            str(spec_dir / "customer_spend_daily.yaml"),
            str(spec_dir / "customer_spend_90d.yaml"),
        ],
    )
    assert result.exit_code == 0
    assert "serves_with" in result.output
    assert "needs window" in result.output


def test_spec_compare_exits_two_when_it_cannot_serve(spec_dir):
    result = runner.invoke(
        app,
        [
            "spec",
            "compare",
            str(spec_dir / "customer_spend_30d.yaml"),
            str(spec_dir / "customer_spend_90d.yaml"),
        ],
    )
    assert result.exit_code == 2
    assert "cannot: window differs" in result.output


def test_asset_request_then_registry_assets(spec_dir, tmp_path):
    registry = tmp_path / "registry.sqlite"
    result = runner.invoke(
        app,
        [
            "asset",
            "request",
            str(spec_dir / "customer_spend_30d.yaml"),
            "--consumer",
            "app:sales",
            "--text",
            "spend over the previous 30 days",
            "--registry",
            str(registry),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "revision 3" in result.output
    listed = runner.invoke(app, ["registry", "assets", "--registry", str(registry)])
    assert "customer_spend_30d" in listed.output
    assert "specified" in listed.output
    log = runner.invoke(app, ["registry", "log", "--registry", str(registry)])
    assert log.output.count("\n") == 3


def test_scenario_build(tmp_path):
    target = tmp_path / "scenario.duckdb"
    result = runner.invoke(app, ["scenario", "build", "--path", str(target), "--customers", "40"])
    assert result.exit_code == 0, result.output
    assert "customers: 40" in result.output
    assert target.exists()


def _prepare(tmp_path, spec_dir):
    registry = tmp_path / "registry.sqlite"
    scenario = tmp_path / "scenario.duckdb"
    assert runner.invoke(app, ["scenario", "build", "--path", str(scenario)]).exit_code == 0
    result = runner.invoke(
        app,
        [
            "asset",
            "request",
            str(spec_dir / "customer_spend_30d.yaml"),
            "--consumer",
            "app:sales",
            "--registry",
            str(registry),
        ],
    )
    assert result.exit_code == 0, result.output
    return registry, scenario


def test_asset_plan_prints_steps_and_sql(spec_dir, tmp_path):
    registry, _ = _prepare(tmp_path, spec_dir)
    result = runner.invoke(
        app,
        [
            "asset",
            "plan",
            "customer_spend_30d",
            "--registry",
            str(registry),
            "--as-of",
            "2026-10-01T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "1. scan purchases" in result.output
    assert "6. left join customers" in result.output
    assert "TIMESTAMPTZ '2026-09-01 00:00:00+00'" in result.output


def test_asset_activate_verify_and_show(spec_dir, tmp_path):
    registry, scenario = _prepare(tmp_path, spec_dir)
    common = [
        "--registry",
        str(registry),
        "--scenario",
        str(scenario),
        "--as-of",
        "2026-10-01T00:00:00+00:00",
    ]
    result = runner.invoke(app, ["asset", "activate", "customer_spend_30d", *common])
    assert result.exit_code == 0, result.output
    assert "ok   reconcile_spend_usd" in result.output
    assert "activated customer_spend_30d via customer_spend_30d_d1" in result.output
    assert "grade reconciliation" in result.output

    listed = runner.invoke(app, ["registry", "assets", "--registry", str(registry)])
    assert "active" in listed.output
    assert "progress=purchases@" in listed.output

    verified = runner.invoke(app, ["asset", "verify", "customer_spend_30d", *common])
    assert verified.exit_code == 0, verified.output
    assert "passed; grade reconciliation" in verified.output

    shown = runner.invoke(app, ["asset", "show", "customer_spend_30d", "--registry", str(registry)])
    assert shown.exit_code == 0, shown.output
    for line in (
        "asset customer_spend_30d  status=active",
        "spec customer_spend_30d@1",
        "commitment ",
        "deployment customer_spend_30d_d1 (active)",
        "proposal customer_spend_30d_p1",
        "  verification customer_spend_30d_p1_v1: passed",
    ):
        assert line in shown.output, line


def test_asset_verify_needs_an_active_implementation(spec_dir, tmp_path):
    registry, scenario = _prepare(tmp_path, spec_dir)
    result = runner.invoke(
        app,
        [
            "asset",
            "verify",
            "customer_spend_30d",
            "--registry",
            str(registry),
            "--scenario",
            str(scenario),
        ],
    )
    assert result.exit_code == 1
    assert "no active implementation" in result.output
