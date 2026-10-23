# Content assignment sweep

Shares each team's ThoughtSpot content with that team's change-managers group, so whoever runs a
promotion can clear a blocking dependent with edit access instead of administrator rights.

- Models, liveboards and answers in a team's scope get the team's group at **edit** (MODIFY).
- The tables underneath them get the group at **view** (READ_ONLY). Under strict object security
  a grant on the content alone leaves it invisible.

It runs inside your environment, under your own service account, on your schedule. It reads
metadata and permissions only and talks to nothing but your ThoughtSpot host.

## What it will never do

| | |
|---|---|
| Remove or lower a grant | The only write adds a group. A grant already at or above the level is left as it is. |
| Share with anyone else | Each scope shares only with its one configured group. |
| Touch content outside scope | Scopes are an allowlist of tags, authors and authors' groups. A scope that selects nothing is refused. |
| Touch ThoughtSpot's own content | Content authored by `system` is excluded (configurable). |
| Read data | Only metadata and permission endpoints are called. |
| Write without being asked | Every run is a dry run unless `--apply` is given. |

## Running it

Python 3.9+ with `requests` and `PyYAML` (both already in the promotion tool's requirements).

Set credentials in the environment, never in the config file:

| Variable | |
|---|---|
| `TS_HOST` | e.g. `https://gsk-test.thoughtspot.cloud` |
| `TS_USERNAME` and `TS_SECRET_KEY` | the service account, signed in with trusted authentication |
| `TS_TOKEN` | alternatively, a bearer token |
| `TS_ORG_ID` | optional, the org to work in |
| `TS_PROXY` or `HTTPS_PROXY` | optional, your outbound proxy |
| `TS_CA_BUNDLE` | optional, a CA bundle for TLS inspection |

```
python sweep/content_sweep.py --config sweep.yaml             # dry run: report only
python sweep/content_sweep.py --config sweep.yaml --apply     # add the missing shares
python sweep/content_sweep.py --config sweep.yaml --scope Respbio
```

Copy `sweep.example.yaml` to `sweep.yaml` and fill in each team's group and selectors.

Exit code 0 means the run finished cleanly, 1 that a scope or a share failed (named in the output
and the audit record), 2 a configuration problem.

## What a run does

1. For each scope, confirms the group exists. If it doesn't, nothing is shared for that scope.
2. Finds the models, liveboards and answers matching the scope's tags, authors and authors' group
   members, excluding system content.
3. Finds every table with at least one of those as a dependent.
4. Reads the grants already defined on each object for the group.
5. Lists what is missing: content below the content level, tables below the table level.
6. With `--apply`, adds the group at that level, then reads every grant back and marks each one
   confirmed or failed. A success response alone is not taken as proof.
7. Writes `sweep-<run>-report.csv` (one line per object: current grant, what was or would be
   added, confirmed) and `sweep-<run>.json` (run id, times, actor, mode, scopes, every object
   before and after, failures with ThoughtSpot's own error text) to `audit_dir`.

Re-running is safe: objects that already carry the grant are reported as "no change".

## Endpoints

| Endpoint | Use | |
|---|---|---|
| `POST /api/rest/2.0/auth/token/full` | sign in as the service account | read |
| `POST /api/rest/2.0/metadata/search` | content in scope; tables and their dependents | read |
| `POST /api/rest/2.0/groups/search` | confirm each group exists; members of authors' groups | read |
| `POST /api/rest/2.0/security/metadata/fetch-permissions` | grants already defined, before and after | read |
| `POST /api/rest/2.0/security/metadata/share` | add the group (the only write) | write |

## Known limits

- **A model owned by another team.** If a liveboard in scope is built on a model outside every
  scope, the liveboard and its tables are shared but the model isn't. Under strict object security
  the group may then not see the liveboard fully. Put the model in a scope, or tell us and we'll
  add view access for such models.
- **Group membership.** An authors' group contributes the users listed directly in it. The sweep
  doesn't follow its sub-groups.
