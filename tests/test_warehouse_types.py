"""
L1 tests for the warehouse column-TYPE reads that back the type-mismatch (14536) panel.

Both paths are pure once the network boundary is stubbed:
  - databricks_direct.hive_column_types → Databricks DESCRIBE (the hive_metastore path GSK uses,
    because connection/search 504s on hive).
  - TSClient.connection_column_types  → connection/search COLUMN (Unity Catalog / Snowflake / …).

These lock in the parsing that turns a raw warehouse response into {table: {col: type}} — the
DESCRIBE partition-section break and the connection field-name coalescing.
"""
import services.databricks_direct as dd
from services.ts_client import TSClient


# ── hive_column_types (DESCRIBE) ────────────────────────────────────────────
def _stub_describe(monkeypatch, data_array, state="SUCCEEDED"):
    def fake_run(session, host, wh, stmt, timeout=180):
        assert stmt.startswith("DESCRIBE TABLE "), stmt
        return state, {"result": {"data_array": data_array}}
    monkeypatch.setattr(dd, "_run_statement", fake_run)
    monkeypatch.setattr(dd, "_session", lambda tok, proxy="": object())


_TBLS = [{"name": "fact_resp", "database": "db", "schema": "sch", "table": "fact_resp"}]


def test_hive_types_parses_and_stops_at_partition_section(monkeypatch):
    _stub_describe(monkeypatch, [
        ["cid", "string", ""],
        ["amount", "decimal(10,2)", ""],
        ["hcp_id", "bigint", None],
        ["", None, None],                    # blank separator → stop
        ["# Partition Information", None, None],
        ["# col_name", "data_type", ""],
        ["region", "string", ""],            # after the break → must be ignored
    ])
    got = dd.hive_column_types("https://h", "wh1", "tok", _TBLS)
    assert got == {"fact_resp": {"cid": "string", "amount": "decimal(10,2)", "hcp_id": "bigint"}}


def test_hive_types_records_debug_and_column_count(monkeypatch):
    _stub_describe(monkeypatch, [["cid", "string", ""], ["amount", "double", ""]])
    dbg = []
    dd.hive_column_types("https://h", "wh1", "tok", _TBLS, debug=dbg)
    assert dbg and dbg[0]["state"] == "SUCCEEDED" and dbg[0]["columns"] == 2


def test_hive_types_failed_statement_is_skipped(monkeypatch):
    _stub_describe(monkeypatch, [], state="FAILED")
    dbg = []
    got = dd.hive_column_types("https://h", "wh1", "tok", _TBLS, debug=dbg)
    assert got == {}
    assert dbg[0]["state"] == "FAILED" and "columns" not in dbg[0]


def test_hive_types_guards_on_missing_inputs():
    assert dd.hive_column_types("", "", "", _TBLS) == {}
    assert dd.hive_column_types("https://h", "wh1", "tok", []) == {}


# ── connection_column_types (connection/search COLUMN) ──────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def json(self):
        return self._p


def _client(monkeypatch, payload):
    c = TSClient("https://cluster")
    c._connection_meta = lambda ident: ("conn-guid", "PERSONAL_ACCESS_TOKEN")
    c._session.post = lambda url, json=None, timeout=None: _Resp(payload)
    return c


_CONN_TBLS = [{"name": "fact_resp", "database": "db", "schema": "sch", "table": "fact_resp"}]


def _wrap(columns):
    return [{"data_warehouse_objects": {"databases": [{"schemas": [{"tables": [
        {"name": "fact_resp", "columns": columns}]}]}]}}]


def test_connection_types_parse_and_field_coalescing(monkeypatch):
    c = _client(monkeypatch, _wrap([
        {"name": "CID", "type": "STRING"},
        {"name": "AMOUNT", "data_type": "DECIMAL(10,2)"},   # alternate field name
        {"name": "NOTYPE"},                                  # no type → skipped
    ]))
    got = c.connection_column_types("conn", _CONN_TBLS)
    assert got == {"fact_resp": {"cid": "STRING", "amount": "DECIMAL(10,2)"}}


def test_connection_types_debug_column_count(monkeypatch):
    c = _client(monkeypatch, _wrap([{"name": "CID", "type": "STRING"}]))
    dbg = []
    c.connection_column_types("conn", _CONN_TBLS, debug=dbg)
    assert dbg and dbg[0]["columns_found"] == 1 and dbg[0]["has_objects"] is True


def test_connection_types_no_tables_returns_empty(monkeypatch):
    c = _client(monkeypatch, _wrap([]))
    assert c.connection_column_types("conn", []) == {}
