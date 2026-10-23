"""ThoughtSpot content assignment sweep.

Shares each team's content with that team's change-managers group, so a promotion run by a
member of the group can clear a blocking dependent without administrator rights:

  * models, liveboards and answers in scope get the group at edit access (MODIFY);
  * the tables underneath them get the group at view access (READ_ONLY), because under strict
    object security a grant on the content alone leaves it invisible.

What it will and won't do:

  * It only ever adds the one group configured for a scope. It never removes, lowers or
    reassigns a grant, never changes ownership and never deletes anything.
  * Scope is an explicit allowlist (tags, authors, authors' groups). An empty scope selects
    nothing; there is no "everything" default. ThoughtSpot system content is excluded.
  * A run is a dry run unless --apply is given. Every write is re-read and confirmed.
  * It reads metadata and permissions only, never data, and talks to nothing but the
    ThoughtSpot host it is given.

Endpoints used (ThoughtSpot REST API v2), and nothing else:
  POST auth/token/full                       sign in as the service account (trusted auth)
  POST metadata/search                       find content in scope and the tables under it
  POST groups/search                         confirm the group exists; members of author groups
  POST security/metadata/fetch-permissions   read the grants already defined on each object
  POST security/metadata/share               add the group (the only write)

Run:  python sweep/content_sweep.py --config sweep.yaml            (dry run)
      python sweep/content_sweep.py --config sweep.yaml --apply    (writes)

Credentials come from the environment, never the config file, and are never logged:
  TS_HOST, and either TS_USERNAME + TS_SECRET_KEY (trusted auth) or TS_TOKEN (bearer).
  Optional: TS_ORG_ID, TS_PROXY (or HTTPS_PROXY), TS_CA_BUNDLE.
"""
import argparse
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

LEVELS = {"NO_ACCESS": 0, "READ_ONLY": 1, "MODIFY": 2}
CONTENT_CAP, TABLE_CAP = "MODIFY", "READ_ONLY"   # the most the sweep will ever grant
CONTENT_TYPES = ("LIVEBOARD", "ANSWER", "MODEL")
MODEL_HEADER_TYPES = {"WORKSHEET", "MODEL"}       # LOGICAL_TABLE headers that are models
PAGE = 200
SHARE_BATCH = 50


class ConfigError(ValueError):
    pass


# ── configuration ───────────────────────────────────────────────────────────────────────────

def load_config(text: str) -> dict:
    """Parse and check the config. Raises ConfigError with a plain reason on anything unsafe."""
    cfg = yaml.safe_load(text) or {}
    content_level = str(cfg.get("content_level", CONTENT_CAP)).upper()
    table_level = str(cfg.get("table_level", TABLE_CAP)).upper()
    if content_level not in ("READ_ONLY", "MODIFY"):
        raise ConfigError(f"content_level must be READ_ONLY or MODIFY, not {content_level}")
    if table_level != "READ_ONLY":
        raise ConfigError(f"table_level can only be READ_ONLY, not {table_level}")
    scopes = cfg.get("scopes") or []
    if not scopes:
        raise ConfigError("no scopes configured; the sweep does nothing without an allowlist")
    seen, out = set(), []
    for i, sc in enumerate(scopes, 1):
        name = (sc.get("name") or f"scope {i}").strip()
        group = (sc.get("group") or "").strip()
        if not group:
            raise ConfigError(f"scope '{name}' has no group")
        if name.lower() in seen:
            raise ConfigError(f"scope '{name}' is listed twice")
        seen.add(name.lower())
        sel = {k: [str(v).strip() for v in (sc.get(k) or []) if str(v).strip()]
               for k in ("tags", "authors", "author_groups")}
        if not any(sel.values()):
            raise ConfigError(f"scope '{name}' selects nothing: give tags, authors or author_groups")
        out.append({"name": name, "group": group, **sel})
    return {
        "content_level": content_level,
        "table_level": table_level,
        "exclude_authors": [a.lower() for a in (cfg.get("exclude_authors") or ["system"])],
        "audit_dir": cfg.get("audit_dir") or "sweep_audit",
        "scopes": out,
    }


# ── pure logic (no I/O) ─────────────────────────────────────────────────────────────────────

def object_kind(row: dict, searched_type: str) -> str:
    """LIVEBOARD, ANSWER, MODEL or TABLE for a metadata/search row."""
    if searched_type != "LOGICAL_TABLE":
        return searched_type
    t = ((row.get("metadata_header") or {}).get("type") or "").upper()
    return "MODEL" if t in MODEL_HEADER_TYPES else "TABLE"


def row_record(row: dict, kind: str) -> dict:
    h = row.get("metadata_header") or {}
    return {"id": row.get("metadata_id"), "name": row.get("metadata_name") or h.get("name"),
            "kind": kind, "author": h.get("authorName") or "",
            "author_display": h.get("authorDisplayName") or h.get("authorName") or ""}


def excluded(rec: dict, exclude_authors) -> bool:
    return (rec.get("author") or "").lower() in set(exclude_authors)


def tables_in_scope(table_rows, content_ids):
    """Tables with at least one dependent (model, answer or liveboard) in scope, from table search
    rows that carry include_dependent_objects. Returns (tables, truncated) where `truncated` names
    tables whose dependents came back incomplete, so a miss there is reported, not hidden."""
    content_ids = set(content_ids)
    hits, truncated = [], []
    for row in table_rows:
        deps = ((row.get("dependent_objects") or {}).get("dependents") or {}).get(row.get("metadata_id")) or {}
        dep_ids = {o.get("id") for objs in deps.values() for o in (objs or [])}
        if dep_ids & content_ids:
            hits.append(row)
        elif row.get("dependent_objects_is_last_batch") is False:
            truncated.append(row.get("metadata_name"))
    return hits, truncated


def current_level(perm_detail: dict, group_id: str, group_name: str) -> str:
    """The level defined directly for the group on one object (NO_ACCESS when absent)."""
    best = "NO_ACCESS"
    for info in perm_detail.get("principal_permission_info") or []:
        for p in info.get("principal_permissions") or []:
            if p.get("principal_id") == group_id or \
               (p.get("principal_name") or "").lower() == group_name.lower():
                lvl = (p.get("permission") or "NO_ACCESS").upper()
                if LEVELS.get(lvl, 0) > LEVELS[best]:
                    best = lvl
    return best


def needs(current: str, wanted: str) -> bool:
    """True when the group's grant is below the level wanted. Never true for a higher grant, so
    the sweep cannot lower anything."""
    return LEVELS.get(current, 0) < LEVELS[wanted]


# ── ThoughtSpot client (the five endpoints above, nothing else) ─────────────────────────────

class Client:
    def __init__(self, host, token=None, username=None, secret_key=None, org_id=None,
                 proxy=None, ca_bundle=None):
        self.host = host.rstrip("/")
        if "://" not in self.host:
            self.host = "https://" + self.host
        self.s = requests.Session()
        self.s.headers["Accept"] = "application/json"
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})
        if ca_bundle:
            self.s.verify = ca_bundle
        self.actor = username or "(bearer token)"
        if not token:
            body = {"username": username, "secret_key": secret_key, "validity_time_in_sec": 3600}
            if org_id:
                body["org_id"] = int(org_id)
            r = self.s.post(f"{self.host}/api/rest/2.0/auth/token/full", json=body, timeout=60)
            if not r.ok:
                raise RuntimeError(f"sign-in failed (HTTP {r.status_code}): {r.text[:300]}")
            token = r.json()["token"]
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _post(self, path, body):
        r = self.s.post(f"{self.host}/api/rest/2.0/{path}", json=body, timeout=180)
        if not r.ok:
            raise RuntimeError(f"{path} failed (HTTP {r.status_code}): {r.text[:500]}")
        return r.json() if r.text else None

    def search(self, meta_type, **extra):
        """All rows of one metadata type matching the filters, paged."""
        rows, offset = [], 0
        while True:
            body = {"metadata": [{"type": meta_type}], "record_size": PAGE,
                    "record_offset": offset, **extra}
            batch = self._post("metadata/search", body) or []
            rows.extend(batch)
            if len(batch) < PAGE:
                return rows
            offset += PAGE

    def group(self, name):
        rows = self._post("groups/search", {"group_identifier": name}) or []
        return rows[0] if rows else None

    def permissions(self, objects):
        """{object_id: permission detail} for [(id, api_type)], read as DEFINED grants."""
        out = {}
        for i in range(0, len(objects), 50):
            chunk = objects[i:i + 50]
            d = self._post("security/metadata/fetch-permissions", {
                "metadata": [{"identifier": oid, "type": t} for oid, t in chunk],
                "permission_type": "DEFINED"}) or {}
            for m in d.get("metadata_permission_details") or []:
                out[m.get("metadata_id")] = m
        return out

    def share(self, api_type, ids, group_id, level, message):
        self._post("security/metadata/share", {
            "metadata_type": api_type, "metadata_identifiers": ids,
            "permissions": [{"principal": {"identifier": group_id, "type": "USER_GROUP"},
                             "share_mode": level}],
            "notify_on_share": False, "message": message})


API_TYPE = {"LIVEBOARD": "LIVEBOARD", "ANSWER": "ANSWER", "MODEL": "LOGICAL_TABLE",
            "TABLE": "LOGICAL_TABLE"}


# ── one scope ───────────────────────────────────────────────────────────────────────────────

def resolve_content(client, scope, exclude_authors):
    """Models, liveboards and answers selected by the scope's tags, authors and author groups."""
    authors = list(scope["authors"])
    for g in scope["author_groups"]:
        grp = client.group(g)
        if not grp:
            raise RuntimeError(f"author group '{g}' not found")
        authors += [u.get("name") for u in (grp.get("users") or []) if u.get("name")]
    found = {}
    for searched in ("LIVEBOARD", "ANSWER", "LOGICAL_TABLE"):
        queries = []
        if scope["tags"]:
            queries.append({"tag_identifiers": scope["tags"]})
        if authors:
            queries.append({"created_by_user_identifiers": sorted(set(authors))})
        for q in queries:
            for row in client.search(searched, **q):
                kind = object_kind(row, searched)
                if kind == "TABLE":
                    continue   # tables come in through the content that uses them
                rec = row_record(row, kind)
                if not excluded(rec, exclude_authors):
                    found[rec["id"]] = rec
    return found


def all_tables(client):
    """Every table with its dependents, read once per run and shared by all scopes."""
    # dependent_objects_record_size -1: all of each table's dependents. The default stops at 50,
    # which on a busy table silently dropped the very content that put it in scope.
    rows = client.search("LOGICAL_TABLE", include_dependent_objects=True,
                         dependent_object_version="V2", dependent_objects_record_size=-1)
    return [r for r in rows if object_kind(r, "LOGICAL_TABLE") == "TABLE"]


def plan_scope(client, scope, cfg, table_rows):
    """What one scope needs: rows of {object, current, wanted, action}, plus notes."""
    notes = []
    grp = client.group(scope["group"])
    if not grp:
        raise RuntimeError(f"group '{scope['group']}' not found; nothing is shared until it exists")
    gid, gname = grp["id"], grp.get("name") or scope["group"]
    content = resolve_content(client, scope, cfg["exclude_authors"])
    hits, truncated = tables_in_scope(table_rows, content.keys())
    tables = {}
    for r in hits:
        rec = row_record(r, "TABLE")
        if not excluded(rec, cfg["exclude_authors"]):
            tables[rec["id"]] = rec
    if truncated:
        notes.append("dependents came back incomplete for: " + ", ".join(truncated)
                     + ". If one of them sits under content in scope it was not checked.")
    objs = list(content.values()) + list(tables.values())
    perms = client.permissions([(o["id"], API_TYPE[o["kind"]]) for o in objs])
    rows = []
    for o in sorted(objs, key=lambda x: (x["kind"], (x["name"] or "").lower())):
        wanted = cfg["table_level"] if o["kind"] == "TABLE" else cfg["content_level"]
        cur = current_level(perms.get(o["id"], {}), gid, gname)
        rows.append({**o, "scope": scope["name"], "group": gname, "group_id": gid,
                     "current": cur, "wanted": wanted,
                     "action": "add" if needs(cur, wanted) else "no change"})
    return rows, notes


def apply_rows(client, rows, run_id):
    """Share the group onto every row marked "add", then re-read to confirm. Mutates rows."""
    todo = [r for r in rows if r["action"] == "add"]
    batches = {}
    for r in todo:
        batches.setdefault((API_TYPE[r["kind"]], r["group_id"], r["wanted"]), []).append(r)
    for (api_type, gid, level), group_rows in batches.items():
        for i in range(0, len(group_rows), SHARE_BATCH):
            chunk = group_rows[i:i + SHARE_BATCH]
            try:
                client.share(api_type, [r["id"] for r in chunk], gid, level,
                             f"Shared by the content assignment sweep (run {run_id}).")
            except Exception as e:
                for r in chunk:
                    r["error"] = str(e)
    # A success response is not proof: read every grant back.
    after = client.permissions([(r["id"], API_TYPE[r["kind"]]) for r in todo if not r.get("error")])
    for r in todo:
        if r.get("error"):
            r["verified"] = False
            continue
        r["after"] = current_level(after.get(r["id"], {}), r["group_id"], r["group"])
        r["verified"] = not needs(r["after"], r["wanted"])
        if not r["verified"]:
            r["error"] = f"share returned success but the grant reads back as {r['after']}"


# ── run ─────────────────────────────────────────────────────────────────────────────────────

def write_outputs(audit_dir, run, rows):
    Path(audit_dir).mkdir(parents=True, exist_ok=True)
    stem = Path(audit_dir) / f"sweep-{run['run_id']}"
    with open(f"{stem}-report.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scope", "group", "object", "type", "author", "current grant",
                    "would add" if run["mode"] == "dry_run" else "added", "after", "verified",
                    "error"])
        for r in rows:
            w.writerow([r["scope"], r["group"], r["name"], r["kind"], r["author_display"],
                        r["current"], r["wanted"] if r["action"] == "add" else "no change",
                        r.get("after", ""), r.get("verified", ""), r.get("error", "")])
    with open(f"{stem}.json", "w") as fh:
        json.dump({**run, "objects": rows}, fh, indent=1, default=str)
    return f"{stem}-report.csv", f"{stem}.json"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Share each team's content with its change-managers group.")
    ap.add_argument("--config", required=True)
    ap.add_argument("--apply", action="store_true", help="write the shares (default: dry run)")
    ap.add_argument("--scope", help="run only the scope with this name")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(Path(args.config).read_text())
    except (ConfigError, OSError, yaml.YAMLError) as e:
        print(f"Config problem: {e}", file=sys.stderr)
        return 2
    scopes = [s for s in cfg["scopes"] if not args.scope or s["name"].lower() == args.scope.lower()]
    if not scopes:
        print(f"No scope named '{args.scope}'.", file=sys.stderr)
        return 2
    env = os.environ
    if not env.get("TS_HOST") or not (env.get("TS_TOKEN") or (env.get("TS_USERNAME") and env.get("TS_SECRET_KEY"))):
        print("Set TS_HOST and either TS_TOKEN or TS_USERNAME + TS_SECRET_KEY.", file=sys.stderr)
        return 2
    client = Client(env["TS_HOST"], token=env.get("TS_TOKEN"), username=env.get("TS_USERNAME"),
                    secret_key=env.get("TS_SECRET_KEY"), org_id=env.get("TS_ORG_ID"),
                    proxy=env.get("TS_PROXY") or env.get("HTTPS_PROXY"),
                    ca_bundle=env.get("TS_CA_BUNDLE"))
    run = {"run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6],
           "started": datetime.now(timezone.utc).isoformat(), "actor": client.actor,
           "host": client.host, "org_id": env.get("TS_ORG_ID", ""),
           "mode": "apply" if args.apply else "dry_run",
           "content_level": cfg["content_level"], "table_level": cfg["table_level"],
           "scopes": scopes, "notes": [], "failures": []}
    rows, table_rows = [], None
    for sc in scopes:
        try:
            if table_rows is None:
                table_rows = all_tables(client)
            sc_rows, notes = plan_scope(client, sc, cfg, table_rows)
        except Exception as e:
            run["failures"].append({"scope": sc["name"], "error": str(e)})
            print(f"[{sc['name']}] could not be planned, nothing shared: {e}")
            continue
        rows += sc_rows
        run["notes"] += [f"[{sc['name']}] {n}" for n in notes]
    if args.apply:
        apply_rows(client, rows, run["run_id"])
    run["finished"] = datetime.now(timezone.utc).isoformat()
    run["failures"] += [{"scope": r["scope"], "object": r["name"], "id": r["id"],
                         "error": r["error"]} for r in rows if r.get("error")]
    report, audit = write_outputs(cfg["audit_dir"], run, rows)

    adds = [r for r in rows if r["action"] == "add"]
    verb = "added" if args.apply else "would add"
    failed_scopes = {f["scope"] for f in run["failures"] if "object" not in f}
    for sc in scopes:
        if sc["name"] in failed_scopes:
            continue   # already reported as not planned; a summary line would read as "empty"
        mine = [r for r in rows if r["scope"] == sc["name"]]
        n_add = sum(1 for r in mine if r["action"] == "add")
        print(f"[{sc['name']}] group '{sc['group']}': {len(mine)} object(s) in scope, "
              f"{n_add} {verb}, {len(mine) - n_add} already shared")
        for r in mine:
            if r["action"] == "add":
                tail = ("  OK" if r.get("verified") else f"  FAILED: {r.get('error')}") if args.apply else ""
                print(f"    {r['kind']:9} {r['name']}  ({r['current']} -> {r['wanted']}){tail}")
    for n in run["notes"]:
        print("Note:", n)
    print(f"{'Applied' if args.apply else 'Dry run'}: {len(adds)} change(s). "
          f"Report: {report}  Audit: {audit}")
    return 1 if run["failures"] else 0


if __name__ == "__main__":
    sys.exit(main())
