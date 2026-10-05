# Product requirements

One document per phase, plus the vision that every phase serves. Read [00-vision.md](00-vision.md) first.

| Doc | Phase | Status |
|---|---|---|
| [00-vision.md](00-vision.md) | What veena is, who it serves, what it will not do | Agreed Oct 5 2026 |
| [01-foundation.md](01-foundation.md) | Registry, specification IR, semantic model, scenario, CLI | Done |
| [02-planner-verification.md](02-planner-verification.md) | Spec to implementation, independent verification, first activation | Done |
| [03-overlap-reorganization.md](03-overlap-reorganization.md) | Shared intermediates, rewiring, the activation guard | Next |
| [04-request-resolution.md](04-request-resolution.md) | Natural language to spec, questions and decisions | Planned |
| [05-shadow-mode.md](05-shadow-mode.md) | Import a real warehouse, propose without acting, first backend | Planned |
| [06-operations-retirement.md](06-operations-retirement.md) | Operator view, brakes, repair, retirement | Planned |

## How these docs work

* Each requirement has an id like `R2.4`: phase 2, requirement 4. Code, tests, and commits may cite the id.
* Each requirement has an acceptance line. It names the test or observable result that settles it. If a requirement cannot be settled by a test, it says what evidence counts instead.
* "Out of scope" lists what the phase deliberately leaves alone, so nobody argues about it mid-build.
* Status moves through Planned, Next, In progress, Done. Only a passing acceptance moves a requirement to Done.
* Vocabulary follows [../AGENTS.md](../AGENTS.md). One name per concept.

## Dependencies between phases

```
01 foundation ──► 02 planner + verification ──► 03 overlap + reorganization ──► 06 operations + retirement
                                 │                                                     ▲
                                 └──► 04 request resolution                            │
                                 └──► 05 shadow mode ──────────────────────────────────┘
```

Phase 4 and phase 5 both need only phase 2. They can run in parallel. Phase 6 needs 3 and 5 because the operator view reads real deployments on a real backend.
