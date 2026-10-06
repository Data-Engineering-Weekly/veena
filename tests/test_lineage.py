"""Lineage export in OpenLineage and W3C PROV (R6.17)."""

import json

import pytest

from tests.conftest import AS_OF, bundled_spec
from tests.test_coordinator_share import request
from veena.backends import DuckDBBackend
from veena.coordinator import Coordinator
from veena.lineage import (
    export_lineage,
    openlineage_events,
    prov_document,
    validate_openlineage,
    validate_prov,
)
from veena.registry import MemoryEventStore, Registry


@pytest.fixture
def shared(scenario_conn, semantic):
    registry = Registry(MemoryEventStore())
    backend = DuckDBBackend(scenario_conn)
    request(registry, bundled_spec("customer_spend_30d"), "app:sales")
    Coordinator(registry, backend, semantic).activate("customer_spend_30d", as_of=AS_OF)
    request(registry, bundled_spec("customer_spend_90d"), "agent:support")
    Coordinator(registry, backend, semantic).activate("customer_spend_90d", as_of=AS_OF)
    return registry, backend, semantic


def test_export_writes_both_files_and_they_validate(shared, tmp_path):
    registry, _, _ = shared
    ol_path, prov_path, problems = export_lineage(registry.state, tmp_path / "lineage")
    assert problems == ()
    events = json.loads(ol_path.read_text())
    prov = json.loads(prov_path.read_text())
    assert len(events) == 4
    assert {e["eventType"] for e in events} == {"COMPLETE"}
    names = {e["job"]["name"] for e in events}
    assert names == {"customer_spend_30d", "customer_spend_90d", "customer_spend_daily"}
    ninety = next(e for e in events if e["job"]["name"] == "customer_spend_90d")
    assert [i["name"] for i in ninety["inputs"]] == ["customer_spend_daily", "customers"]
    assert ninety["outputs"][0]["facets"]["schema"]["fields"] == [
        {"name": "customer_id"},
        {"name": "spend_usd"},
    ]
    impl_id = ninety["job"]["facets"]["veena_implementation"]["implementation_id"]
    assert f"veena:implementation/{impl_id}" in prov["entity"]
    assert "veena:deployment/customer_spend_daily_d1" in prov["activity"]
    assert "veena:agent/engine" in prov["agent"]
    derived = prov["wasDerivedFrom"].values()
    assert any(
        r["prov:generatedEntity"] == f"veena:implementation/{impl_id}"
        and r["prov:usedEntity"] == "veena:asset/customer_spend_daily"
        for r in derived
    )


def test_rollback_and_decisions_appear(shared):
    registry, backend, semantic = shared
    coordinator = Coordinator(registry, backend, semantic)
    coordinator.rollback("customer_spend_daily_d1", reason="bad", as_of=AS_OF)
    events = openlineage_events(registry.state)
    assert {
        e["eventType"]
        for e in events
        if e["run"]["facets"]["veena_deployment"]["deployment_id"] == "customer_spend_daily_d1"
    } == {"FAIL"}
    prov = prov_document(registry.state)
    assert prov["activity"]["veena:deployment/customer_spend_daily_d1"]["veena:rolled_back"] is True
    assert validate_openlineage(events) == () and validate_prov(prov) == ()


def test_validators_name_structural_problems():
    bad = [{"eventType": "DONE", "run": {"runId": "x"}, "job": {"namespace": "", "name": "a"}}]
    problems = validate_openlineage(bad)
    assert any("missing eventTime" in p for p in problems)
    assert any("eventType 'DONE'" in p for p in problems)
    assert any("not a UUID" in p for p in problems)
    assert any("job needs namespace" in p for p in problems)
    prov = {
        "entity": {"e": {}},
        "used": {"_:r1": {"prov:activity": "missing", "prov:entity": "e"}},
        "bogus": {},
    }
    problems = validate_prov(prov)
    assert any("unknown top-level key 'bogus'" in p for p in problems)
    assert any("'missing' is not a known id" in p for p in problems)
