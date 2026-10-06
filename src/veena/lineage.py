"""Lineage export: OpenLineage for what ran, W3C PROV for who decided what.

The two documents share asset and implementation ids, so a reader can go from a run event to
the decision that set the definition it used. Validation here checks the structural rules of
both formats that the engine relies on. It does not fetch the published JSON schemas.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

from veena import __version__

if TYPE_CHECKING:
    from pathlib import Path

    from veena.registry.state import RegistryState

PRODUCER = f"https://github.com/ananthdurai/veena/tree/{__version__}"
OPENLINEAGE_SCHEMA = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
NAMESPACE = "veena"
_UUID_NAMESPACE = uuid.UUID("7f2c7f5a-6d1e-4e3b-9d4a-1b8e4e6c2a10")


def openlineage_events(state: RegistryState) -> list[dict[str, Any]]:
    """One RunEvent per asset per deployment. Rolled-back deployments end with FAIL."""
    events: list[dict[str, Any]] = []
    for deployment in sorted(state.deployments.values(), key=lambda d: d.seq):
        for asset_id, impl_id in deployment.activated.items():
            impl = state.implementations[impl_id]
            run_id = str(uuid.uuid5(_UUID_NAMESPACE, f"{deployment.deployment_id}/{asset_id}"))
            inputs = [
                {
                    "namespace": NAMESPACE,
                    "name": d.name,
                    "facets": {"veena_version": {"version": d.version, "kind": d.kind}},
                }
                for d in impl.dependencies
            ]
            fields = [{"name": c} for c in impl.plan.output_columns]
            events.append(
                {
                    "eventType": "FAIL" if deployment.rolled_back else "COMPLETE",
                    "eventTime": deployment.effective_at.isoformat(),
                    "run": {
                        "runId": run_id,
                        "facets": {
                            "veena_deployment": {
                                "deployment_id": deployment.deployment_id,
                                "proposal_id": deployment.proposal_id,
                                "verification_id": deployment.verifications.get(
                                    asset_id, deployment.verification_id
                                ),
                                "assumed_revision": deployment.assumed_revision,
                                "rolled_back": deployment.rolled_back,
                            }
                        },
                    },
                    "job": {
                        "namespace": NAMESPACE,
                        "name": asset_id,
                        "facets": {
                            "veena_implementation": {
                                "implementation_id": impl_id,
                                "spec_version": impl.spec_version,
                                "backend": impl.backend,
                                "semantic_fingerprint": impl.plan.semantic_fingerprint,
                            },
                            "sql": {"query": impl.sql, "_producer": PRODUCER},
                        },
                    },
                    "inputs": inputs,
                    "outputs": [
                        {
                            "namespace": NAMESPACE,
                            "name": asset_id,
                            "facets": {"schema": {"fields": fields}},
                        }
                    ],
                    "producer": PRODUCER,
                    "schemaURL": OPENLINEAGE_SCHEMA,
                }
            )
    return events


def prov_document(state: RegistryState) -> dict[str, Any]:
    """Decision lineage as PROV-JSON: entities, activities, agents, and their relations."""
    entity: dict[str, Any] = {}
    activity: dict[str, Any] = {}
    agent: dict[str, Any] = {}
    generated: dict[str, Any] = {}
    used: dict[str, Any] = {}
    associated: dict[str, Any] = {}
    derived: dict[str, Any] = {}
    informed: dict[str, Any] = {}
    counter = 0

    def rel(table: dict[str, Any], body: dict[str, str]) -> None:
        nonlocal counter
        counter += 1
        table[f"_:r{counter}"] = body

    def agent_id(actor: str) -> str:
        key = f"veena:agent/{actor}"
        agent.setdefault(
            key, {"prov:type": "prov:SoftwareAgent" if actor == "engine" else "prov:Person"}
        )
        return key

    for asset in state.assets.values():
        entity[f"veena:asset/{asset.asset_id}"] = {"veena:status": asset.status.value}
        if asset.spec is not None:
            spec_id = f"veena:spec/{asset.spec.ref}"
            entity[spec_id] = {
                "veena:asset_id": asset.asset_id,
                "veena:spec_version": asset.spec.spec_version,
            }
            rel(
                derived,
                {
                    "prov:generatedEntity": f"veena:asset/{asset.asset_id}",
                    "prov:usedEntity": spec_id,
                },
            )
            for key, origin in asset.spec.provenance.items():
                if origin.origin_kind == "decision":
                    rel(
                        derived,
                        {
                            "prov:generatedEntity": spec_id,
                            "prov:usedEntity": f"veena:decision/{origin.name}",
                            "veena:requirement": key,
                        },
                    )
    for impl in state.implementations.values():
        entity[f"veena:implementation/{impl.implementation_id}"] = {
            "veena:asset_id": impl.asset_id,
            "veena:backend": impl.backend,
        }
        for d in impl.dependencies:
            used_id = (
                f"veena:asset/{d.name}"
                if d.kind == "asset"
                else f"veena:source/{d.name}@{d.version}"
            )
            entity.setdefault(used_id, {"veena:kind": d.kind})
            rel(
                derived,
                {
                    "prov:generatedEntity": f"veena:implementation/{impl.implementation_id}",
                    "prov:usedEntity": used_id,
                },
            )
    for proposal in state.proposals.values():
        pid = f"veena:proposal/{proposal.proposal_id}"
        activity[pid] = {
            "prov:startTime": proposal.opened_at.isoformat(),
            "veena:kind_of_change": proposal.kind_of_change,
            "veena:status": proposal.status.value,
        }
        rel(associated, {"prov:activity": pid, "prov:agent": agent_id(proposal.actor or "engine")})
        for impl_id in proposal.implementations:
            rel(generated, {"prov:entity": f"veena:implementation/{impl_id}", "prov:activity": pid})
        for asset_id in proposal.affected_assets:
            asset = state.assets[asset_id]
            if asset.spec is not None:
                rel(used, {"prov:activity": pid, "prov:entity": f"veena:spec/{asset.spec.ref}"})
    for v in state.verifications.values():
        vid = f"veena:verification/{v.verification_id}"
        entity[vid] = {
            "veena:passed": v.passed,
            "veena:grade": v.grade.value,
            "veena:replay": v.replay,
        }
        rel(
            derived,
            {
                "prov:generatedEntity": vid,
                "prov:usedEntity": f"veena:implementation/{v.implementation_id}",
            },
        )
    for dep in state.deployments.values():
        did = f"veena:deployment/{dep.deployment_id}"
        activity[did] = {
            "prov:startTime": dep.effective_at.isoformat(),
            "veena:rolled_back": dep.rolled_back,
            "veena:assumed_revision": dep.assumed_revision,
        }
        rel(associated, {"prov:activity": did, "prov:agent": agent_id(dep.actor or "engine")})
        rel(informed, {"prov:informed": did, "prov:informant": f"veena:proposal/{dep.proposal_id}"})
        for asset_id, impl_id in dep.activated.items():
            rel(used, {"prov:activity": did, "prov:entity": f"veena:implementation/{impl_id}"})
            rel(generated, {"prov:entity": f"veena:asset/{asset_id}", "prov:activity": did})
    for q in state.questions.values():
        qid = f"veena:question/{q.question_id}"
        activity[qid] = {"veena:slot": q.slot, "veena:asset_id": q.asset_id}
        rel(associated, {"prov:activity": qid, "prov:agent": agent_id(q.actor or "engine")})
    for dec in state.decisions.values():
        decid = f"veena:decision/{dec.decision_id}"
        entity[decid] = {"veena:selected": dec.selected, "veena:scope": dec.scope}
        rel(generated, {"prov:entity": decid, "prov:activity": f"veena:question/{dec.question_id}"})
        rel(
            associated,
            {
                "prov:activity": f"veena:question/{dec.question_id}",
                "prov:agent": agent_id(dec.actor),
            },
        )
    return {
        "prefix": {
            "veena": "https://github.com/ananthdurai/veena/ns#",
            "prov": "http://www.w3.org/ns/prov#",
        },
        "entity": entity,
        "activity": activity,
        "agent": agent,
        "wasGeneratedBy": generated,
        "used": used,
        "wasAssociatedWith": associated,
        "wasDerivedFrom": derived,
        "wasInformedBy": informed,
    }


def validate_openlineage(events: list[dict[str, Any]]) -> tuple[str, ...]:
    """Structural problems against the RunEvent rules the export relies on, empty when none."""
    problems: list[str] = []
    for index, event in enumerate(events):
        where = f"event {index}"
        for key in ("eventType", "eventTime", "run", "job", "producer", "schemaURL"):
            if key not in event:
                problems.append(f"{where}: missing {key}")
        if event.get("eventType") not in {"START", "RUNNING", "COMPLETE", "ABORT", "FAIL", "OTHER"}:
            problems.append(f"{where}: eventType {event.get('eventType')!r} is not allowed")
        run_id = event.get("run", {}).get("runId", "")
        try:
            uuid.UUID(str(run_id))
        except ValueError:
            problems.append(f"{where}: run.runId {run_id!r} is not a UUID")
        job = event.get("job", {})
        if not job.get("namespace") or not job.get("name"):
            problems.append(f"{where}: job needs namespace and name")
        for side in ("inputs", "outputs"):
            for dataset in event.get(side, []):
                if not dataset.get("namespace") or not dataset.get("name"):
                    problems.append(f"{where}: {side} dataset needs namespace and name")
    return tuple(problems)


def validate_prov(document: dict[str, Any]) -> tuple[str, ...]:
    """Structural problems against PROV-JSON: known tables, and relations that name known ids."""
    problems: list[str] = []
    known = (
        set(document.get("entity", {}))
        | set(document.get("activity", {}))
        | set(document.get("agent", {}))
    )
    checks = {
        "wasGeneratedBy": ("prov:entity", "prov:activity"),
        "used": ("prov:activity", "prov:entity"),
        "wasAssociatedWith": ("prov:activity", "prov:agent"),
        "wasDerivedFrom": ("prov:generatedEntity", "prov:usedEntity"),
        "wasInformedBy": ("prov:informed", "prov:informant"),
    }
    allowed = {"prefix", "entity", "activity", "agent", *checks}
    for key in document:
        if key not in allowed:
            problems.append(f"unknown top-level key {key!r}")
    for table, fields in checks.items():
        for rel_id, body in document.get(table, {}).items():
            for field in fields:
                target = body.get(field)
                if target is None:
                    problems.append(f"{table} {rel_id}: missing {field}")
                elif target not in known:
                    problems.append(f"{table} {rel_id}: {field} {target!r} is not a known id")
    return tuple(problems)


def export_lineage(state: RegistryState, out_dir: Path) -> tuple[Path, Path, tuple[str, ...]]:
    """Write `openlineage.json` and `prov.json` and return their paths and any problems found."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events = openlineage_events(state)
    prov = prov_document(state)
    problems = (*validate_openlineage(events), *validate_prov(prov))
    ol_path = out_dir / "openlineage.json"
    prov_path = out_dir / "prov.json"
    ol_path.write_text(json.dumps(events, indent=2, default=str) + "\n")
    prov_path.write_text(json.dumps(prov, indent=2, default=str) + "\n")
    return ol_path, prov_path, problems


__all__ = [
    "export_lineage",
    "openlineage_events",
    "prov_document",
    "validate_openlineage",
    "validate_prov",
]
