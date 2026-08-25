# ts-cluster-promo-poc — session handoff

Paste this (or say "read HANDOFF.md") into the new chat to pick up with full context.
Detailed background is in auto-memory (`MEMORY.md` + the `project_*/reference_*` files); this doc
covers the live state that isn't obvious from memory alone.

## What the tool is
Streamlit app that promotes ThoughtSpot content (tables, models, answers, liveboards, Spotter
feedback, NL instructions) cross-cluster / cross-org via a **Git bridge** (`dev` → PR → `main`,
then REST `import_tml` from `main` to target) using **obj_id** for stable identity.
Repo: `/Users/anuj.seth/projects/ts-cluster-promo-poc` · remote `github.com/anujseth2/ts-cluster-promo-poc`.

## How we work on it (standing rule — important)
Changes go on a **branch/fork**, verified via a **live inter-org promotion on ps-internal**
(AnujSeth org `1417628299` → Anuj Git Dev org `790309399`), and only merged to shared `main`
**after** verification. Never push unverified code to the PoC Ashish pulls.

## Current repo state (uncommitted, on `main`, last commit `adfa520`)
These are LOCAL edits from the GSK debugging session — treat as **hygiene, not verified, NOT the
GSK fix**. Decide keep/commit/revert per the workflow above:
- `services/ts_client.py` — `connection_column_cases` read timeout raised 120s → 600s (now a param).
- `app.py` — export block reordered: try fast `table_column_cases` (target logical tables) BEFORE the
  slow `connection_column_cases` (live warehouse fetch); connection fetch only for tables not on target.
- Untracked `docs/git_operations_*` — plain-language Git-ops overview deliverables for Ashish (SVG/MD/HTML/PDF). Keep.

## Paused investigation — GSK connection/search COLUMN 504 (resume later with Ashish)
Full trail in memory `project_gsk_sp_column_504.md`. Short version:
- GSK's Databricks connection: catalog/schema/table **listing works**, but **COLUMN introspection 504s**
  (ThoughtSpot's own 300s gateway timeout).
- **Ruled out:** SP-auth-type; 5086-table schema size; SELECT-as-the-504-cause; admin-vs-non-admin;
  user/privilege/sharing (verified: a non-admin with `DATAMANAGEMENT` + the connection shared at
  **MODIFY/edit** browses AND reads columns fully on ps-internal — GSK's exact setup, works here).
- **Still open (UNVERIFIED):** the mechanism. Leading *hypothesis* (not proven): `hive_metastore`
  column reads need a warehouse query (`DESCRIBE`) while Unity Catalog serves them compute-free, and
  GSK's warehouse can't return it inside 300s. The playground can't test hive (legacy hive metastore is
  disabled by design on the PS Databricks — new account).
- **Definitive next step (on GSK, needs Ashish):** run ONE COLUMN fetch on GSK connection `6ac650a5`
  while watching the SQL-warehouse **Query History** — does a `DESCRIBE`/`SHOW COLUMNS` query fire, and
  does it arrive / queue / error? That names the cause (and may disprove the hypothesis). Also `whoami`
  the GSK-box `TS_TARGET_TOKEN`.

## Cleanup owed (test artifacts to remove)
- ps-internal (AnujSeth org): users `dm_test`, `ccec_e366e1`; roles `dm_probe_role`, `ccec_e366e1_role`;
  groups `dm_probe_grp`, `ccec_e366e1_grp`; connections **"GSK test"** (`05be8a26` on ps-internal,
  `ff4967f2` on gsk-test).
- Databricks (PS workspace): service principal **GSK_test** + its OBO token.
- **Revoke the admin PAT** that was pasted into chat during the session.

## Environment gotchas (also in memory)
- AnujSeth-org objects are invisible to other-org tokens (400/13003). Mint an AnujSeth token via
  `auth/token/full` + trusted-auth secret_key (`username=anuj.seth`, `org_identifier=1417628299`).
  See memory `reference_anujseth_org_auth`.
- ps-internal has RBAC/Roles: verify a user's **effective** privileges via `auth/session/user`
  (role definitions can misreport what a user actually has).
- `connection/search` returns 0 at all levels unless the caller has access to the connection
  (admin, or the connection shared at `MODIFY`/edit).

## To start the new session
Paste the improvements list. Work against the repo per the fork/verify workflow; GSK stays paused
until the Ashish session.
