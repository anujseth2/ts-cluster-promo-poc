"""Establish / test the direct Databricks hive-metastore casing path — for ANY tables.

Usage (run from the repo root):
  python dbx_cases.py <catalog> <schema> [table1 table2 ...]   # specific tables
  python dbx_cases.py <catalog> <schema>                       # no tables -> SHOW TABLES, then all

Env: DBX_HOST, DBX_WAREHOUSE, DBX_PAT   (HTTPS_PROXY or TS_PROXY optional, for the GSK box).
Writes hive_casing.json = {table: {col_lower: actual_case}} — the true warehouse casing.
"""
import os
import sys
import json

from services.databricks_direct import hive_column_cases, list_tables

HOST = os.environ["DBX_HOST"].strip()
WH = os.environ["DBX_WAREHOUSE"].strip()
PAT = os.environ["DBX_PAT"].strip()
PROXY = (os.environ.get("HTTPS_PROXY") or os.environ.get("TS_PROXY", "")).strip()

if len(sys.argv) < 3:
    print("usage: python dbx_cases.py <catalog> <schema> [table ...]")
    sys.exit(1)
CAT, SCH = sys.argv[1], sys.argv[2]
tbls = sys.argv[3:]
if not tbls:
    tbls = list_tables(HOST, WH, PAT, CAT, SCH, PROXY)
    print(f"enumerated {len(tbls)} table(s) in {CAT}.{SCH}")

tables = [{"name": t, "database": CAT, "schema": SCH, "table": t} for t in tbls]
dbg = []
casing = hive_column_cases(HOST, WH, PAT, tables, PROXY, debug=dbg)
for d in dbg:
    tail = f"({d.get('columns')} cols)" if d["state"] == "SUCCEEDED" else d.get("error", "")
    print(f"  {d['fqn']}: {d['state']} {tail}")
json.dump(casing, open("hive_casing.json", "w"), indent=2)
print(f"\nsaved hive_casing.json ({len(casing)} table(s) with columns)")
