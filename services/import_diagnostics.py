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

  type_mismatch — a column exists on BOTH sides but its declared type (carried
                  from the SOURCE warehouse) does not match the TARGET warehouse's
                  physical type. Import HARD-FAILS at VALIDATE_ONLY:
                    "DataType <T> does not match CDW DataType for column with name
                     <db.schema.table.col> in connection <conn>."
                  The message names the SOURCE-declared type (<T>) but NOT the
                  target's; the target's actual type comes from the target table's
                  column_signature (compare_columns -> type_mismatch).
                  Reviewer choice: retype the promoted column to the target's type
                  (column + dependents survive), align the target warehouse (data
                  team), or drop it + its dependents.
"""

import json
import re

from services.table_matcher import column_signature, compare_columns

_MISSING_WH = re.compile(r"External column with name:\s*(\S+?)\s+does not exist in connection\s+(.+?)\.", re.I)
_TYPE_MISMATCH = re.compile(
    r"DataType\s+(\S+)\s+does not match CDW DataType for column with name\s+(\S+?)\s+in connection\s+(.+?)\.",
    re.I)
_DEP_HEADER = re.compile(r"Deleted columns have dependents", re.I)
_BOLD = re.compile(r"<b>(.*?)</b>", re.I | re.S)
_LI = re.compile(r"<li>(.*?)</li>", re.I | re.S)


def classify_import_errors(results):
    """results: [{'name','type','status','error'}] from TSClient.import_tml.
    Returns findings: list of {kind, object, ...}:
      missing_in_target_warehouse  -> column, column_fqn, connection
      drop_blocked_by_dependents   -> columns[], dependents[]
      type_mismatch                -> column, column_fqn, source_type, connection
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
        for src_type, col_fqn, conn in _TYPE_MISMATCH.findall(msg):
            matched = True
            findings.append({"kind": "type_mismatch",
                             "object": r.get("name"),
                             "column": col_fqn.split(".")[-1],
                             "column_fqn": col_fqn,
                             "source_type": src_type.strip(),
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


_BRACKET_REF = re.compile(r"\[([^\]]+)\]")


def column_dependents(items, columns):
    """Read-only preview of what references the given columns across the promotion set,
    so a reviewer sees the blast radius BEFORE choosing to drop. columns are matched on
    the physical column name (column_id last segment) and the resolved model display name.

    Returns {model_columns, joins, formulas, vizzes}. Joins and formulas reference columns
    by `[table::Col]` / `[Display Name]`, so dropping a column they use leaves a dangling
    reference -> the drop path must treat those as manual-cleanup, not auto-removable.
    """
    targets = {c.lower() for c in columns}
    docs = []
    display_targets = set()
    for item in items:
        edoc = item.get("edoc", "{}")
        doc = json.loads(edoc) if isinstance(edoc, str) else edoc
        docs.append(doc)
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if _col_name(c) in targets:
                    nm = (c.get("name") or "").strip().lower()
                    if nm:
                        display_targets.add(nm)

    def _strings(obj):
        """Yield every string anywhere in a nested join/formula dict. The join condition
        key is `on`, which YAML 1.1 parses as the boolean True, so we can't rely on the
        key name — we scan all values for `[table::Col]` references instead."""
        if isinstance(obj, str):
            yield obj
        elif isinstance(obj, dict):
            for v in obj.values():
                yield from _strings(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from _strings(v)

    def _refs_target(obj):
        for expr in _strings(obj):
            for inner in _BRACKET_REF.findall(expr):
                tail = inner.split("::")[-1].strip().lower()
                if tail in targets or inner.strip().lower() in display_targets:
                    return True
        return False

    deps = {"model_columns": [], "joins": [], "formulas": [], "vizzes": []}
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if _col_name(c) in targets:
                    deps["model_columns"].append(c.get("name") or _col_name(c))
            for mt in (node.get("model_tables") or node.get("tables") or []):
                for j in mt.get("joins", []) or []:
                    if _refs_target(j):
                        deps["joins"].append(j.get("name") or f"{mt.get('name','')} -> {j.get('with','')}")
            for f in node.get("formulas", []) or []:
                if _refs_target(f):
                    deps["formulas"].append(f.get("name") or "(unnamed formula)")
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                acols = [(c.get("name", "") or "").lower()
                         for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(any(t in ac or ac in t for t in (targets | display_targets)) for ac in acols):
                    deps["vizzes"].append(viz.get("id") or viz.get("viz_id") or "(viz)")
        ans = doc.get("answer")
        if ans:
            acols = [(c.get("name", "") or "").lower() for c in ans.get("answer_columns", [])]
            if any(any(t in ac or ac in t for t in (targets | display_targets)) for ac in acols):
                deps["vizzes"].append(ans.get("name") or "(answer)")
    for k in deps:
        seen, out = set(), []
        for v in deps[k]:
            if v not in seen:
                seen.add(v)
                out.append(v)
        deps[k] = out
    return deps


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
