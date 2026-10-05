# Contributing

Thanks for looking at veena. This file is the short version; [AGENTS.md](AGENTS.md) holds the vocabulary and the writing and code rules every change follows, and [DEVELOPERS.md](DEVELOPERS.md) explains how the pieces fit.

## Setup

```bash
uv sync --all-groups
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

All three checks must pass. CI runs the same commands on Python 3.14.

## Making a change

1. Find the requirement in [prd/](prd/README.md). Every behavior has an id like `R3.6` and an acceptance line. If your change has no requirement, open an issue first so we can write one.
2. Add or change the test that settles the acceptance before you change the code.
3. Keep one name per concept. The vocabulary table in AGENTS.md is the source of truth. A new concept means a new row.
4. Library code never prints and never calls a model outside the call sites listed in `prd/00-vision.md`.
5. Write the pull request description the way AGENTS.md says to write prose: answer first, short sentences, what failed or is uncertain and what would settle it.

## What we will say no to

- A model output that authorizes an action. Classifier probabilities feed policy tables; drafter text feeds validators. Neither reaches activation on its own.
- Verification that reads the candidate's SQL.
- A new backend-specific field on the plan. Backends render; the plan stays neutral.
- Silent defaults. A requirement a request did not state is filled from policy and says so in provenance.

## Reporting a bug

Open an issue with the command you ran, the output, and what you expected. If the registry is involved, attach `veena registry log` for the asset. Registry files contain your specs and model outputs; check them before posting.

## Security

See [SECURITY.md](SECURITY.md).
