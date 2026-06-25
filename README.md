# Cross-Cluster Promotion POC (ThoughtSpot, two clusters, GitOps)

Promote tagged team content from a **source cluster** to a **target cluster** (e.g.
`gsk-dev` → `gsk-test`), with **Git as the source of truth** and **obj_id** as the
cross-cluster identity key. This is the two-cluster sibling of:

- `ts-git-migration-poc` — dev → prod across **Orgs on one cluster**
- `ts-area-promo-poc` — Config/Test/Live **areas within one Org**

The engine is the same (export → transform → Git PR → import). What is different
here: **two hosts with independent credentials**, and the clusters point at
**different warehouses**, so the data layer is remapped per cluster.

## What scopes a promotion: tags + dependency walk

Teams all live in one Org and are distinguished by **tags** (`config/teams.json`
maps a team → its tag(s)). Shared objects simply carry **multiple** team tags.

You only tag the **leaves** (liveboards + answers). The tool walks the dependency
chain down to the models and tables they sit on, so the whole stack ships:

```
liveboard / answer   (tagged: team:commercial-sbu)
      └─ model        (resolved by dependency walk)
            └─ tables (resolved by dependency walk)
                  └─ connection  (reference remapped to the target cluster)
```

## The transform (`services/tml_transformer.py`)

Names are **preserved** across clusters (identity is obj_id, not name). The only
rewrites are on the data layer:

1. **strip `fqn`** on model table refs → forces obj_id resolution on import.
2. **connection remap** `source_connection` → `target_connection`.
3. **db / schema remap** via `db_map` / `schema_map` (the target warehouse's names).
4. **`${...}` variable tokens are left untouched** — if GSK's admin later
   parameterises connections/tables with ThoughtSpot Variables, this tool needs no
   change; it just stops needing the remap for those objects.

Structural differences between clusters (a column or table that exists on source
but not target) are **not** something the transform can paper over. The Git
Operations step validates models against the target with `VALIDATE_ONLY` and offers
to **drop the missing columns + their dependent vizs** before import.

## Flow (Streamlit, 5 steps)

1. **Select Assets** — pick a team; fetch its tagged liveboards/answers; the tool
   resolves the model + table dependencies.
2. **obj_id Setup** — confirm every object and its tables have `obj_id` set, and
   that the source and target tables are aligned on the same `obj_id`.
3. **Review** — preview the data-layer remap (connection / db / schema).
4. **Git Operations** — commit to a branch, open a PR, validate models against the
   target, optionally drop columns missing on the target, then merge.
5. **Import Results** — import the merged TML into the target cluster.

## Setup

`.env` (see `.env.example`): source + target **host + creds** (token or
service-account user/pass), plus `GITHUB_TOKEN` / `GITHUB_REPO`.

### Networking note (GSK)

Both clusters are VPN-gated and sit behind the **McAfee Web Gateway** proxy, which
requires Windows-integrated auth. Run this tool from a **Windows box on the GSK
VPN**. If `Invoke-RestMethod`-style 407 errors appear from Python, set the proxy
for the session and rely on OS integrated auth:

```powershell
$env:HTTPS_PROXY = "http://<mcafee-gateway>:<port>"   # or set TS_PROXY in .env
```

For NTLM/Kerberos proxies that need explicit credentials, install
`requests-negotiate-sspi` and the client will pick up your Windows login.

## Run

```bash
# reuses the sibling tool's venv (streamlit, requests, PyGitHub already installed)
../ts-git-migration-poc/venv/bin/streamlit run app.py --server.port 8602
```
