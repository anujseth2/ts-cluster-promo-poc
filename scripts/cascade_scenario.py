#!/usr/bin/env python3
"""Build (or rebuild) the target-cascade test scenario on ps-internal.

The blocked-dependents cascade only shows itself when the TARGET holds objects that use a column
the promotion is dropping, and the interesting part is that they are not all the same KIND. So
this lays down one of each on Anuj Git Prod, all on the shared `sales_customers` table:

    Regional Ops Model         another team's model on the same table  -> stripped, then WALKED
      Regional Ops Board         3 tiles, one uses Gender              -> that one tile removed
      Regional Ops Gender Split  an answer on that model               -> deleted
    Customer Gender Mix        an answer on the model being promoted   -> deleted

and tags the two source objects on Anuj Git Dev so the picker shows two rows instead of forty.

Already on prod and deliberately left alone: `Test dev`, whose ONLY tile uses gender. It makes
the cascade refuse rather than hollow out a board, which is worth seeing once.

    python3 scripts/cascade_scenario.py --build     put it all back (safe to re-run)
    python3 scripts/cascade_scenario.py --status    what is there now, and what would block
    python3 scripts/cascade_scenario.py --teardown  remove the scenario objects

--build is idempotent because every object carries an obj_id: a second run updates in place
rather than minting duplicates. Run it after a cascade to reset for the next attempt.
"""
import argparse
import json
import os
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")

from services.ts_client import TSClient                       # noqa: E402

TAG = "cascade-test"
TABLE_OBJ_ID = "sales_customers"
PROMO_OBJ_ID = "sales_customers_model"
SRC_TAGGED = [("sales_customers", "eb16e7dc-e254-4cb1-af94-3a2489832a3f"),
              ("Sales Customers Model", "1c0e57c5-c385-4268-b4af-a975cab23aac")]
# obj_id -> (display name, metadata type) for teardown, children before parents.
SCENARIO = [("customer_gender_mix",       "Customer Gender Mix",       "ANSWER"),
            ("regional_ops_gender_split", "Regional Ops Gender Split", "ANSWER"),
            ("regional_ops_board",        "Regional Ops Board",        "LIVEBOARD"),
            ("regional_ops_model",        "Regional Ops Model",        "LOGICAL_TABLE")]


def clients():
    return (TSClient(os.getenv("TS_SOURCE_HOST"), token=os.getenv("TS_SOURCE_TOKEN")),
            TSClient(os.getenv("TS_TARGET_HOST"), token=os.getenv("TS_TARGET_TOKEN")))


def _col(name, cid, kind="ATTRIBUTE"):
    return {"name": name, "column_id": cid, "properties": {"column_type": kind}}


def _answer(name, tables, cols, query):
    return {"name": name, "tables": tables, "search_query": query,
            "answer_columns": [{"name": c} for c in cols],
            "table": {"table_columns": [{"column_id": c} for c in cols],
                      "ordered_column_ids": list(cols)}}


def build(src, tgt):
    r = src._session.post(f"{src.host}/api/rest/2.0/tags/create",
                          json={"name": TAG, "color": "#F2A62C"}, timeout=30)
    print(f"tag {TAG!r}: {'created' if r.ok else 'already there'}")
    r = src._session.post(f"{src.host}/api/rest/2.0/tags/assign",
                          json={"metadata": [{"identifier": g, "type": "LOGICAL_TABLE"}
                                             for _, g in SRC_TAGGED],
                                "tag_identifiers": [TAG]}, timeout=30)
    print(f"tagged {len(SRC_TAGGED)} source object(s): HTTP {r.status_code}")

    other = [{"id": "Regional Ops Model", "name": "Regional Ops Model",
              "obj_id": "regional_ops_model"}]
    promo = [{"id": "Sales Customers Model", "name": "Sales Customers Model",
              "obj_id": PROMO_OBJ_ID}]
    objs = [
        {"obj_id": "regional_ops_model",
         "model": {"name": "Regional Ops Model",
                   "model_tables": [{"name": "sales_customers", "obj_id": TABLE_OBJ_ID}],
                   "columns": [_col("Gender", "sales_customers::gender"),
                               _col("City", "sales_customers::city"),
                               _col("Country", "sales_customers::country"),
                               _col("State", "sales_customers::state")],
                   "properties": {"is_bypass_rls": False, "join_progressive": False}}},
        {"obj_id": "regional_ops_gender_split",
         "answer": _answer("Regional Ops Gender Split", other, ["Gender", "Country"],
                           "[Gender] [Country]")},
        {"obj_id": "regional_ops_board",
         "liveboard": {"name": "Regional Ops Board", "visualizations": [
             {"id": "Viz_1", "answer": _answer("Customers by Gender", other, ["Gender"],
                                               "[Gender]")},
             {"id": "Viz_2", "answer": _answer("Customers by City", other, ["City"], "[City]")},
             {"id": "Viz_3", "answer": _answer("Customers by State", other, ["State"],
                                               "[State]")}],
             "layout": {"tiles": [
                 {"visualization_id": "Viz_1", "x": 0, "y": 0, "height": 8, "width": 4},
                 {"visualization_id": "Viz_2", "x": 4, "y": 0, "height": 8, "width": 4},
                 {"visualization_id": "Viz_3", "x": 8, "y": 0, "height": 8, "width": 4}]}}},
        {"obj_id": "customer_gender_mix",
         "answer": _answer("Customer Gender Mix", promo, ["Gender", "City"], "[Gender] [City]")},
    ]
    for res in tgt.import_tml([json.dumps(o) for o in objs], policy="ALL_OR_NONE"):
        print(f"  {res['status']:<6} {res['name']!r:<28} {res['error'][:80]}")


def status(src, tgt):
    print("SOURCE  Anuj Git Dev — what the picker shows for this team")
    for row in src.search_by_tags([TAG], types=["LOGICAL_TABLE", "LIVEBOARD", "ANSWER"]):
        print(f"  {row['type']:<6} {row['name']!r}")
    print("\nTARGET  Anuj Git Prod — dropping `gender` is blocked by")
    raw = tgt.export_tml([_guid(tgt, TABLE_OBJ_ID, 'LOGICAL_TABLE')])
    doc = json.loads((raw if isinstance(raw, list) else raw["object"])[0]["edoc"])
    doc["table"]["columns"] = [c for c in doc["table"]["columns"]
                               if (c.get("name") or "").lower() != "gender"]
    for res in tgt.import_tml([json.dumps(doc)], policy="VALIDATE_ONLY"):
        print(f"  {res['error'] or '(nothing — the scenario is not in place)'}")


def _guid(client, obj_id, obj_type):
    g = client.find_by_obj_id(obj_id, obj_type)
    if not g:
        raise SystemExit(f"could not find {obj_id!r} on the target")
    return g


def teardown(_src, tgt):
    for obj_id, name, mtype in SCENARIO:
        guid = tgt.find_by_obj_id(obj_id, mtype)
        if not guid:
            print(f"  gone   {name!r}")
            continue
        ok, _s, detail = tgt.delete_metadata_verified(mtype, guid)
        print(f"  {'PASS' if ok else 'FAIL':<6} delete {name!r}: {detail}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--teardown", action="store_true")
    args = ap.parse_args()
    if not (args.build or args.status or args.teardown):
        ap.print_help()
        raise SystemExit(0)
    s, t = clients()
    if args.teardown:
        teardown(s, t)
    if args.build:
        build(s, t)
    if args.status:
        status(s, t)
