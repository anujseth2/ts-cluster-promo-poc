"""Direct Databricks column-casing fetch — bypasses ThoughtSpot's connection/search.

ThoughtSpot introspects a Databricks connection's columns by querying `<catalog>.information_schema`,
which Unity Catalog has but the legacy `hive_metastore` catalog does NOT — so column reads against a
hive_metastore table fail (TABLE_OR_VIEW_NOT_FOUND, surfacing as a 504/opaque error). `SHOW COLUMNS`
reads the metastore directly, so it works for hive_metastore AND Unity Catalog.

This talks straight to the Databricks SQL Statement Execution API with a PAT/token — no ThoughtSpot
in the path. Returns {table_name.lower(): {col_name.lower(): actual_db_column_name}} — the SAME shape
as connection_column_cases, so it drops straight into the transform's column_case_map.
"""
import time
import requests


def _run_statement(session, host, warehouse_id, statement, timeout=180):
    """Submit a SQL statement and poll to completion. Returns (state, response_json)."""
    body = {"warehouse_id": (warehouse_id or "").strip(), "statement": statement, "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY"}
    r = session.post(f"{host}/api/2.0/sql/statements", json=body, timeout=60).json()
    sid = r.get("statement_id")
    state = (r.get("status") or {}).get("state")
    waited = 0
    while state in ("PENDING", "RUNNING") and waited < timeout:
        time.sleep(3)
        waited += 3
        r = session.get(f"{host}/api/2.0/sql/statements/{sid}", timeout=60).json()
        state = (r.get("status") or {}).get("state")
    return state, r


def _session(token, proxy=""):
    s = requests.Session()
    proxy = (proxy or "").strip()
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    s.headers.update({"Authorization": f"Bearer {(token or '').strip()}", "Content-Type": "application/json"})
    return s


def list_tables(host, warehouse_id, token, catalog, schema, proxy=""):
    """Every table in <catalog>.<schema> (via SHOW TABLES). Returns [table_name, ...]."""
    host = (host or "").strip().rstrip("/")
    s = _session(token, proxy)
    state, r = _run_statement(s, host, warehouse_id, f"SHOW TABLES IN {catalog}.{schema}")
    if state != "SUCCEEDED":
        return []
    # SHOW TABLES columns: database, tableName, isTemporary
    return [row[1] for row in ((r.get("result") or {}).get("data_array") or []) if len(row) > 1]


def hive_column_cases(host, warehouse_id, token, tables, proxy="", debug=None):
    """tables: iterable of {"name" (TS logical name), "database", "schema", "table" (db_table)}.
    For each, run SHOW COLUMNS and map the TS logical name -> the warehouse's true column casing.
    Tables that can't be read are skipped (recorded in `debug` if a list is given).
    Returns {name.lower(): {col.lower(): actual_case}}."""
    host = (host or "").strip().rstrip("/")
    if not (host and warehouse_id and token) or not tables:
        return {}
    s = _session(token, proxy)
    out = {}
    for t in tables:
        db, sch, tbl = t.get("database", ""), t.get("schema", ""), t.get("table", "")
        name = t.get("name") or tbl
        if not tbl:
            continue
        fqn = ".".join(x for x in (db, sch, tbl) if x)
        state, r = _run_statement(s, host, warehouse_id, f"SHOW COLUMNS IN {fqn}")
        rec = {"table": name, "fqn": fqn, "state": state}
        if state == "SUCCEEDED":
            cols = [row[0] for row in ((r.get("result") or {}).get("data_array") or []) if row]
            if cols:
                out[name.strip().lower()] = {c.strip().lower(): c for c in cols}
            rec["columns"] = len(cols)
        else:
            rec["error"] = str((r.get("status") or {}).get("error", ""))[:200]
        if debug is not None:
            debug.append(rec)
    return out
