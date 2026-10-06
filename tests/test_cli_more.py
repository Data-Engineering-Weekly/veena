"""CLI paths the walkthrough tests do not reach: proposals, deployments, questions, imports."""

from pathlib import Path

from typer.testing import CliRunner

from veena.cli import app

runner = CliRunner()
SPECS = Path(__file__).resolve().parents[1] / "src" / "veena" / "scenario" / "specs"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "shoply"
AS_OF = ["--as-of", "2026-10-01T00:00:00+00:00"]


def _world(tmp_path):
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
    assert (
        runner.invoke(app, ["asset", "activate", "customer_spend_30d", *common, *AS_OF]).exit_code
        == 0
    )
    return registry, scenario, common


def test_share_flow_with_approval_and_rollback_through_the_cli(tmp_path):
    registry, _, common = _world(tmp_path)
    pending = runner.invoke(
        app, ["asset", "activate", "customer_spend_90d", "--require-approval", *common, *AS_OF]
    )
    assert pending.exit_code == 0, pending.output
    assert "waits for approval" in pending.output
    listed = runner.invoke(app, ["proposal", "list", "--registry", str(registry)])
    assert (
        "customer_spend_daily_p1" in listed.output
        and "share" in listed.output
        and "open" in listed.output
    )
    shown = runner.invoke(
        app, ["proposal", "show", "customer_spend_daily_p1", "--registry", str(registry)]
    )
    assert (
        shown.exit_code == 0
        and "fact rows processed per refresh" in shown.output
        and "considered:" in shown.output
    )
    approved = runner.invoke(
        app,
        [
            "proposal",
            "approve",
            "customer_spend_daily_p1",
            "--reason",
            "owner agreed",
            *common,
            *AS_OF,
        ],
    )
    assert approved.exit_code == 0, approved.output
    assert "(share)" in approved.output
    deployments = runner.invoke(app, ["deployment", "list", "--registry", str(registry)])
    assert "customer_spend_daily_d1" in deployments.output and "atomic" in deployments.output
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
    assert "rolled back customer_spend_daily_d1" in rolled.output
    after = runner.invoke(app, ["registry", "assets", "--registry", str(registry)])
    assert "customer_spend_30d__v1__i1" in after.output
    missing = runner.invoke(app, ["proposal", "show", "nope", "--registry", str(registry)])
    assert missing.exit_code == 1 and "unknown proposal" in missing.output


def test_reject_and_shadow_autonomy_through_the_cli(tmp_path):
    _, _, common = _world(tmp_path)
    pending = runner.invoke(
        app, ["asset", "activate", "customer_spend_90d", "--require-approval", *common, *AS_OF]
    )
    assert pending.exit_code == 0
    rejected = runner.invoke(
        app, ["proposal", "reject", "customer_spend_daily_p1", "--reason", "not now", *common]
    )
    assert rejected.exit_code == 0 and "rejected" in rejected.output
    shadow = runner.invoke(
        app, ["asset", "activate", "customer_spend_90d", "--autonomy", "shadow", *common, *AS_OF]
    )
    assert shadow.exit_code == 1 and "shadow" in shadow.output
    bad = runner.invoke(
        app, ["asset", "activate", "customer_spend_90d", "--autonomy", "yolo", *common, *AS_OF]
    )
    assert bad.exit_code == 1 and "autonomy must be" in bad.output


def test_misc_commands(tmp_path):
    registry, scenario, common = _world(tmp_path)
    assert runner.invoke(app, ["spec", "examples"]).exit_code == 0
    semantic = runner.invoke(app, ["scenario", "semantic"])
    assert semantic.exit_code == 0 and "semantic:customer@1" in semantic.output
    log = runner.invoke(app, ["registry", "log", "--registry", str(registry), "--after", "3"])
    assert log.exit_code == 0 and "asset_requested" in log.output
    bad_spec = runner.invoke(app, ["spec", "validate", str(tmp_path / "missing.yaml")])
    assert bad_spec.exit_code == 1 and "error:" in bad_spec.output
    no_asset = runner.invoke(app, ["asset", "show", "nope", "--registry", str(registry)])
    assert no_asset.exit_code == 1
    no_plan = runner.invoke(app, ["asset", "plan", "nope", "--registry", str(registry)])
    assert no_plan.exit_code == 1
    missing_consumer = runner.invoke(
        app, ["asset", "request", str(SPECS / "customer_spend_30d.yaml"), *common]
    )
    assert missing_consumer.exit_code == 2 and "--consumer" in missing_consumer.output
    missing_text = runner.invoke(app, ["asset", "request", "--consumer", "x", *common])
    assert missing_text.exit_code == 1 and "--text and --asset-id" in missing_text.output
    bad_clf = runner.invoke(
        app,
        [
            "asset",
            "request",
            "--consumer",
            "x",
            "--asset-id",
            "a_30d",
            "--text",
            "customer spend 30 days",
            "--classifier",
            "nope",
            *common,
        ],
    )
    assert bad_clf.exit_code == 1 and "unknown classifier" in bad_clf.output
    existing = runner.invoke(app, ["scenario", "build", "--path", str(scenario)])
    assert existing.exit_code == 1 and "already exists" in existing.output


def test_import_pipeline_through_the_cli(tmp_path):
    registry = tmp_path / "registry.sqlite"
    out = tmp_path / "dbt"
    wrote = runner.invoke(app, ["scenario", "dbt", "--out", str(out), "--history-days", "3"])
    assert wrote.exit_code == 0 and (out / "manifest.json").exists()
    semantic_out = tmp_path / "semantic.yaml"
    imported = runner.invoke(
        app,
        [
            "import",
            "dbt",
            str(out / "manifest.json"),
            "--history",
            str(out / "query_history.jsonl"),
            "--registry",
            str(registry),
            "--semantic-out",
            str(semantic_out),
        ],
    )
    assert imported.exit_code == 0, imported.output
    assert (
        "41 assets added" in imported.output
        and "semantic gaps recorded as questions: 2" in imported.output
    )
    assert "commitments proposed" in imported.output
    again = runner.invoke(
        app,
        [
            "import",
            "dbt",
            str(out / "manifest.json"),
            "--registry",
            str(registry),
            "--semantic-out",
            str(semantic_out),
        ],
    )
    assert "41 unchanged" in again.output
    inferred = runner.invoke(
        app,
        [
            "import",
            "infer",
            "--registry",
            str(registry),
            "--semantic",
            str(semantic_out),
            "--only",
            "customer_spend_30d",
            "--only",
            "cust_spend_last_30d",
            "--only",
            "customer_spend_30d_v2",
            "--only",
            "stg_orders",
        ],
    )
    assert inferred.exit_code == 0, inferred.output
    assert (
        "4 drafts opened as questions" in inferred.output
        and "open: identity_policy, shape" in inferred.output
    )
    confirmed = runner.invoke(
        app,
        [
            "question",
            "confirm",
            "--above",
            "0.5",
            "--registry",
            str(registry),
            "--semantic",
            str(semantic_out),
        ],
    )
    assert confirmed.exit_code == 0, confirmed.output
    assert "3 specs resolved" in confirmed.output
    report = runner.invoke(app, ["report", "duplicates", "--propose", "--registry", str(registry)])
    assert report.exit_code == 0, report.output
    assert "equivalence set 1: 3 assets" in report.output
    assert "_consolidate_p1 (shadow: recorded, not activated)" in report.output
    usage = runner.invoke(
        app, ["report", "usage", "--asset-id", "customer_spend_30d", "--registry", str(registry)]
    )
    assert (
        usage.exit_code == 0 and "user:svc_sales_app" in usage.output and "rebuilds" in usage.output
    )
    calls = runner.invoke(
        app, ["models", "calls", "--asset-id", "customer_spend_30d", "--registry", str(registry)]
    )
    assert "infer_spec" in calls.output
    question = next(
        line.split()[0]
        for line in runner.invoke(
            app, ["question", "list", "--open", "--registry", str(registry)]
        ).output.splitlines()
        if line.startswith("stg_orders")
    )
    shown = runner.invoke(app, ["question", "show", question, "--registry", str(registry)])
    assert shown.exit_code == 0 and "inferred_spec" in shown.output
    bad_scope = runner.invoke(
        app,
        [
            "question",
            "answer",
            question,
            "--select",
            "reject_draft",
            "--scope",
            "galaxy",
            "--registry",
            str(registry),
        ],
    )
    assert bad_scope.exit_code == 1
    bad_site = runner.invoke(app, ["models", "calibration", "judge_draft"])
    assert bad_site.exit_code == 1 and "no labeled set" in bad_site.output
    yaml_fragment = runner.invoke(
        app,
        ["asset", "show", "customer_spend_30d", "--format", "yaml", "--registry", str(registry)],
    )
    assert yaml_fragment.exit_code == 0
    assert "change_proposal_ref: proposal:" in yaml_fragment.output
    assert "_consolidate_p1" in yaml_fragment.output
