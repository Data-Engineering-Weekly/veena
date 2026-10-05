# Security

Report a vulnerability privately to apackkildurai@gmail.com with "veena security" in the subject. You will get an acknowledgement within three working days.

## What to know before deploying

- veena renders SQL from identifiers declared in semantic models, specifications, and imported dbt manifests. Treat those files as code: only owners should write them. Identifiers are validated against a strict pattern before they reach a backend.
- The registry stores specifications, questions, decisions, and every model output, including request text. It may contain business definitions you consider sensitive. Protect the SQLite or Postgres store accordingly.
- Model adapters read credentials from environment variables (`ANTHROPIC_API_KEY`, `JEV_API_KEY`, `JEV_BASE_URL`) and never write them to the registry or logs.
- The Snowflake and Postgres integrations run with whatever privileges the connection you supply has. Use a role limited to the schemas veena manages.
