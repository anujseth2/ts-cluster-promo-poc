"""
Discover -> pair -> score -> (optionally) align tables across the two clusters,
for the GENERAL case where the target ALREADY has tables and you cannot delete them.

Identity across clusters is obj_id, not GUID. This finds each source table's
counterpart on the target with a confidence % (physical coordinates + column
overlap) and a column diff, so you can decide whether import will update in place
or create a duplicate — then stamp a shared obj_id on the confident matches.

    python scripts/match_tables.py report   # confidence report, makes NO changes
    python scripts/match_tables.py align     # set a shared obj_id on MATCH pairs only

Reuses the app env (.env): TS_SOURCE_* = dev (source), TS_TARGET_* = test (target),
TS_PROXY. Reads connection names + db_map/schema_map from config/teams.json
($TEAM, else the first team). align touches ONLY MATCH-decision pairs; REVIEW /
AMBIGUOUS are left for you, NO_MATCH will be created fresh by the promotion.
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from services.ts_client import TSClient
from services.table_matcher import match_tables

load_dotenv()
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def client(prefix):
    host = os.environ.get(f"{prefix}_HOST", "")
    if not host:
        sys.exit(f"Missing {prefix}_HOST (set TS_SOURCE_* = dev, TS_TARGET_* = test).")
    return TSClient(host,
                    token=os.environ.get(f"{prefix}_TOKEN", ""),
                    username=os.environ.get(f"{prefix}_USERNAME", ""),
                    password=os.environ.get(f"{prefix}_PASSWORD", ""),
                    org_id=os.environ.get(f"{prefix}_ORG", ""),
                    proxy=os.environ.get("TS_PROXY", ""))


def _parse(edoc):
    return json.loads(edoc) if edoc.strip().startswith("{") else __import__("yaml").safe_load(edoc)


def load_team():
    teams = json.loads(open(os.path.join(REPO, "config", "teams.json")).read())
    name = os.environ.get("TEAM") or next(iter(teams))
    return name, teams[name]


def fetch_tables(ts):
    """Return (docs, name->guid). Exports every LOGICAL_TABLE so we get columns + coords."""
    metas = ts.list_metadata("LOGICAL_TABLE")
    if not metas:
        return [], {}
    raw   = ts.export_tml([m["id"] for m in metas])
    items = raw if isinstance(raw, list) else raw.get("object", [])
    docs, name_to_guid = [], {}
    for it in items:
        info = it.get("info", {})
        doc  = _parse(it.get("edoc", "{}"))
        docs.append(doc)
        nm = (doc.get("table", {}) or {}).get("name") or info.get("name")
        if nm:
            name_to_guid[nm] = info.get("id")
    return docs, name_to_guid


def canonical_obj_id(src):
    """Source obj_id is the authority; else derive a deterministic one from coords."""
    if src.get("obj_id"):
        return src["obj_id"]
    base = f"{src['schema']}_{src['db_table']}".lower()
    return re.sub(r"[^a-z0-9_]+", "_", base).strip("_")


def col_summary(cols):
    bits = []
    if cols["missing_on_target"]:
        bits.append(f"missing→{len(cols['missing_on_target'])}")
    if cols["extra_on_target"]:
        bits.append(f"extra→{len(cols['extra_on_target'])}")
    if cols["type_mismatch"]:
        bits.append(f"typemis→{len(cols['type_mismatch'])}")
    return ", ".join(bits) or "columns identical"


def run(do_align):
    team_name, cfg = load_team()
    src_conn, tgt_conn = cfg.get("source_connection", ""), cfg.get("target_connection", "")
    print(f"Team: {team_name}   source_conn='{src_conn}'  target_conn='{tgt_conn}'\n")

    dev, test = client("TS_SOURCE"), client("TS_TARGET")
    src_docs, _src_guids = fetch_tables(dev)
    tgt_docs, tgt_guids  = fetch_tables(test)
    src_guids = _src_guids
    print(f"Discovered {len(src_docs)} source table(s), {len(tgt_docs)} target table(s).\n")

    results = match_tables(src_docs, tgt_docs,
                           db_map=cfg.get("db_map", {}), schema_map=cfg.get("schema_map", {}),
                           source_connection=src_conn, target_connection=tgt_conn)

    for r in results:
        s, best, dec = r["source"], r["best"], r["decision"]
        tgt_name = best["target"]["name"] if best else "—"
        conf     = best["confidence"] if best else 0
        cols     = col_summary(best["columns"]) if best else "—"
        print(f"  {s['name']:16} -> {dec:9} {conf:3}%  best='{tgt_name}'  [{cols}]")

    if not do_align:
        print("\n(report only — no changes made; run `align` to stamp obj_ids on MATCH pairs)")
        return

    print("\nAligning MATCH pairs…")
    for r in results:
        s, best, dec = r["source"], r["best"], r["decision"]
        if dec != "MATCH":
            note = {"NO_MATCH": "no counterpart — will be created on import",
                    "REVIEW": "needs manual confirm", "AMBIGUOUS": "multiple candidates — manual pick"}[dec]
            print(f"  {s['name']:16} skipped ({dec}: {note})")
            continue
        canon    = canonical_obj_id(s)
        src_guid = src_guids.get(s["name"])
        tgt_guid = tgt_guids.get(best["target"]["name"])
        if not src_guid or not tgt_guid:
            print(f"  {s['name']:16} skipped (could not resolve GUID)")
            continue
        if not s.get("obj_id"):
            dev.update_obj_ids([{"identifier": src_guid, "new_obj_id": canon}])
        test.update_obj_ids([{"identifier": tgt_guid, "new_obj_id": canon}])
        print(f"  {s['name']:16} aligned -> obj_id='{canon}' (source + target)")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd not in ("report", "align"):
        sys.exit("usage: match_tables.py [report|align]")
    run(do_align=(cmd == "align"))


if __name__ == "__main__":
    main()
