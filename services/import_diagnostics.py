"""
Classify ThoughtSpot TML import / VALIDATE_ONLY errors into reviewer-actionable
findings, and apply a reviewer's "drop" choice to the promotion set.

Grounded in the exact messages observed live (ps-internal, 2026-06-25):

  source-extra  — a column referenced by the source but absent from the target
                  WAREHOUSE. Import HARD-FAILS (error_code 14536):
                    "External column with name: <db.schema.table.col> does not
                     exist in connection <conn>."
                  Reviewer choice: add the column to the target warehouse and
                  re-run (default), OR drop it from the promotion (+ its vizs).

  target-extra  — a column on the target table that the source TML omits.
    no dependents  -> import SILENTLY drops it (status OK, no message). The
                      platform won't warn; the matcher's pre-import column diff
                      is the only signal.
    with dependents-> import HARD-FAILS, blocked + names the dependents:
                      "Deleted columns have dependents.<br/>- <b>COL</b>
                       <ul><li>DEPENDENT</li></ul> ... SOLUTION: ..."
                      Reviewer choice: preserve the column (carry it through) OR
                      remove the dependents on the target first.
"""

import json
import re

from services.table_matcher import column_signature, compare_columns

_MISSING_WH = re.compile(r"External column with name:\s*(\S+?)\s+does not exist in connection\s+(.+?)\.", re.I)
_DEP_HEADER = re.compile(r"Deleted columns have dependents", re.I)
_BOLD = re.compile(r"<b>(.*?)</b>", re.I | re.S)
_LI = re.compile(r"<li>(.*?)</li>", re.I | re.S)


def classify_import_errors(results):
    """results: [{'name','type','status','error'}] from TSClient.import_tml.
    Returns findings: list of {kind, object, ...}:
      missing_in_target_warehouse  -> column, column_fqn, connection
      drop_blocked_by_dependents   -> columns[], dependents[]
      other                        -> error
    """
    findings = []
    for r in results:
        if (r.get("status") or "").upper() == "OK":
            continue
        msg = r.get("error") or ""
        matched = False
        for col_fqn, conn in _MISSING_WH.findall(msg):
            matched = True
            findings.append({"kind": "missing_in_target_warehouse",
                             "object": r.get("name"),
                             "column": col_fqn.split(".")[-1],
                             "column_fqn": col_fqn,
                             "connection": conn.strip()})
        if _DEP_HEADER.search(msg):
            matched = True
            cols = [b.strip() for b in _BOLD.findall(msg)
                    if b.strip() and not b.strip().endswith(":")]
            deps = [d.strip() for d in _LI.findall(msg) if d.strip()]
            findings.append({"kind": "drop_blocked_by_dependents",
                             "object": r.get("name"), "columns": cols, "dependents": deps})
        if not matched:
            findings.append({"kind": "other", "object": r.get("name"), "error": msg.strip()})
    return findings


def silent_drop_findings(source_table_docs, target_docs_by_name):
    """Pre-import safety net for the SILENT target-extra case the platform never reports.

    source_table_docs   : table TML dicts being promoted.
    target_docs_by_name : {table_name: target's current table TML dict}, for tables that
                          already exist on the target.

    Returns [{table, columns}] where columns exist on the target but are absent from the
    source TML — i.e. they would be dropped on import, silently when they have no
    dependents (with dependents the import errors instead — caught at validation).
    """
    out = []
    for d in source_table_docs:
        t = d.get("table")
        if not t:
            continue
        tgt = target_docs_by_name.get(t.get("name"))
        if not tgt:
            continue   # not on target yet -> created fresh, nothing dropped
        diff = compare_columns(column_signature(d), column_signature(tgt))
        if diff["extra_on_target"]:
            out.append({"table": t.get("name"), "columns": diff["extra_on_target"]})
    return out


def _col_name(model_col):
    cid = model_col.get("column_id", "") or ""
    return (cid.split("::")[-1] if "::" in cid else model_col.get("name", "") or "").lower()


def drop_columns(items, columns):
    """Remove the named columns from every model/table in the promotion set, and any
    liveboard/answer viz that references them. columns may be display or db_column_name.
    Returns (new_items, dropped_columns, dropped_vizs)."""
    targets = {c.lower() for c in columns}
    dropped_cols = dropped_vizs = 0
    out = []
    for item in items:
        edoc = item.get("edoc", "{}")
        doc = json.loads(edoc) if isinstance(edoc, str) else edoc

        for key in ("model", "worksheet"):
            node = doc.get(key)
            if node and node.get("columns") is not None:
                before = len(node["columns"])
                node["columns"] = [c for c in node["columns"] if _col_name(c) not in targets]
                dropped_cols += before - len(node["columns"])

        t = doc.get("table")
        if t and t.get("columns") is not None:
            before = len(t["columns"])
            t["columns"] = [c for c in t["columns"]
                            if (c.get("name", "") or "").lower() not in targets
                            and (c.get("db_column_name", "") or "").lower() not in targets]
            dropped_cols += before - len(t["columns"])

        lb = doc.get("liveboard")
        if lb and lb.get("visualizations") is not None:
            kept = []
            for viz in lb["visualizations"]:
                acols = [(c.get("name", "") or "").lower()
                         for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(any(t_ in ac or ac in t_ for t_ in targets) for ac in acols):
                    dropped_vizs += 1
                else:
                    kept.append(viz)
            lb["visualizations"] = kept

        out.append({**item, "edoc": json.dumps(doc)})
    return out, dropped_cols, dropped_vizs
