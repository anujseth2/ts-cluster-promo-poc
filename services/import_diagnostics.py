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
_VIZ_ERR = re.compile(r"Visualization\s*<b>\s*(.*?)\s*</b>\s*has following errors", re.I | re.S)
_FORMULA = re.compile(r"Formula:\s*([^,<]+)", re.I)


def classify_import_errors(results):
    """results: [{'name','type','status','error'}] from TSClient.import_tml.
    Returns findings: list of {kind, object, ...}:
      missing_in_target_warehouse  -> column, column_fqn, connection
      drop_blocked_by_dependents   -> columns[], dependents[]
      type_mismatch                -> column, column_fqn, source_type, connection
      viz_error                    -> vizzes[], formulas[], error   (liveboard/answer viz fails to load)
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
        viz_ids = _VIZ_ERR.findall(msg)
        if viz_ids:
            matched = True
            findings.append({"kind": "viz_error",
                             "object": r.get("name"),
                             "vizzes": [v.strip() for v in viz_ids],
                             "formulas": [f.strip() for f in _FORMULA.findall(msg)],
                             "error": msg.strip()})
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


def _parse_edoc(item):
    edoc = item.get("edoc", "{}")
    if not isinstance(edoc, str):
        return edoc
    return json.loads(edoc) if edoc.strip().startswith("{") else _yaml_load(edoc)


def _yaml_load(s):
    import yaml
    return yaml.safe_load(s)


def _iter_strings(obj):
    """Yield every string anywhere in a nested structure. The join condition key `on`
    parses as the boolean True in YAML 1.1, so we can't rely on key names — scan values."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _iter_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_strings(v)


def _expr_refs(obj, targets, display_targets):
    """True if any `[table::Col]` / `[Display Name]` reference inside obj hits a target."""
    for expr in _iter_strings(obj):
        for inner in _BRACKET_REF.findall(expr):
            tail = inner.split("::")[-1].strip().lower()
            if tail in targets or inner.strip().lower() in display_targets:
                return True
    return False


def _resolve_display_names(docs, targets):
    """Display names of model/worksheet columns whose physical name is in `targets`, so leaf
    answers/liveboards (which reference the column by its model display name) can be matched."""
    disp = set()
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if _col_name(c) in targets:
                    nm = (c.get("name") or "").strip().lower()
                    if nm:
                        disp.add(nm)
    return disp


def column_usage(items, column):
    """Column-PRECISE attribution: which objects in `items` actually reference `column`,
    and where. `items` should include the model(s) so leaf answers/liveboards resolve the
    column's display name. Returns [{name, kind, where:[...]}], one entry per object that
    references it (objects that only touch the table via OTHER columns are excluded)."""
    targets = {column.lower()}
    docs = [_parse_edoc(it) for it in items]
    display_targets = _resolve_display_names(docs, targets)
    match_set = targets | display_targets

    out = []
    for doc in docs:
        where = []
        # model / worksheet: the column itself, joins, formulas
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            if any(_col_name(c) in targets for c in node.get("columns", []) or []):
                where.append("column")
            for mt in (node.get("model_tables") or node.get("tables") or []):
                for j in mt.get("joins", []) or []:
                    if _expr_refs(j, targets, display_targets):
                        where.append(f"join {mt.get('name','')}->{j.get('with','')}".strip())
            for fdef in node.get("formulas", []) or []:
                if _expr_refs(fdef, targets, display_targets):
                    where.append(f"formula:{fdef.get('name','?')}")
        # liveboard: which vizzes reference it
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                ac = [(c.get("name", "") or "").strip().lower()
                      for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(a in match_set for a in ac) or _expr_refs(viz, targets, display_targets):
                    where.append(f"viz:{viz.get('id') or viz.get('viz_id') or 'viz'}")
        # saved answer
        ans = doc.get("answer")
        if ans:
            ac = [(c.get("name", "") or "").strip().lower() for c in ans.get("answer_columns", [])]
            if any(a in match_set for a in ac) or _expr_refs(ans, targets, display_targets):
                where.append("uses column")

        if where:
            typ = next((k for k in ("liveboard", "answer", "model", "worksheet", "table") if k in doc), "object")
            name = (doc.get(typ) or {}).get("name", "") if isinstance(doc.get(typ), dict) else ""
            seen, w = set(), []
            for x in where:
                if x not in seen:
                    seen.add(x)
                    w.append(x)
            out.append({"name": name or "(unnamed)", "kind": typ, "where": w})
    return out


def column_dependents(items, columns):
    """Read-only preview of what references the given columns across the promotion set,
    so a reviewer sees the blast radius BEFORE choosing to drop. columns are matched on
    the physical column name (column_id last segment) and the resolved model display name.

    Returns {model_columns, joins, formulas, vizzes}. Joins and formulas reference columns
    by `[table::Col]` / `[Display Name]`, so dropping a column they use leaves a dangling
    reference -> the drop path must treat those as manual-cleanup, not auto-removable.
    """
    targets = {c.lower() for c in columns}
    docs = [_parse_edoc(item) for item in items]
    display_targets = _resolve_display_names(docs, targets)
    match_set = targets | display_targets

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
                    if _expr_refs(j, targets, display_targets):
                        deps["joins"].append(j.get("name") or f"{mt.get('name','')} -> {j.get('with','')}")
            for f in node.get("formulas", []) or []:
                if _expr_refs(f, targets, display_targets):
                    deps["formulas"].append(f.get("name") or "(unnamed formula)")
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                acols = [(c.get("name", "") or "").lower()
                         for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(any(t in ac or ac in t for t in match_set) for ac in acols):
                    deps["vizzes"].append(viz.get("id") or viz.get("viz_id") or "(viz)")
        ans = doc.get("answer")
        if ans:
            acols = [(c.get("name", "") or "").lower() for c in ans.get("answer_columns", [])]
            if any(any(t in ac or ac in t for t in match_set) for ac in acols):
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


def drop_vizzes(items, viz_ids):
    """Remove specific visualizations (by id) from any liveboard in `items`, and prune any
    layout tiles that referenced them (flat layout.tiles or tabbed layout.tabs[].tiles).
    Used to drop a viz that fails to load (e.g. a formula that won't compile) so the rest of
    the liveboard imports. Returns (new_items, dropped_count)."""
    targets = {str(v).strip() for v in viz_ids}
    dropped = 0
    out = []
    for item in items:
        edoc = item.get("edoc", "{}")
        doc = json.loads(edoc) if isinstance(edoc, str) and edoc.strip().startswith("{") \
            else (_yaml_load(edoc) if isinstance(edoc, str) else edoc)
        lb = doc.get("liveboard")
        if lb and lb.get("visualizations") is not None:
            kept, kept_ids = [], set()
            for viz in lb["visualizations"]:
                vid = str(viz.get("id") or viz.get("viz_id") or "")
                if vid in targets:
                    dropped += 1
                else:
                    kept.append(viz)
                    kept_ids.add(vid)
            lb["visualizations"] = kept

            layout = lb.get("layout") or {}

            def _prune(tiles):
                return [t for t in tiles if str(t.get("visualization_id", "")) in kept_ids]

            if isinstance(layout.get("tiles"), list):
                layout["tiles"] = _prune(layout["tiles"])
            if isinstance(layout.get("tabs"), list):
                for tab in layout["tabs"]:
                    if isinstance(tab.get("tiles"), list):
                        tab["tiles"] = _prune(tab["tiles"])

        out.append({**item, "edoc": json.dumps(doc)})
    return out, dropped


def _refs_table_prefix(obj, table_targets):
    """True if any `[table::col]` reference inside obj names a table in table_targets
    (matched on the part BEFORE `::`, i.e. the table, not the column)."""
    for expr in _iter_strings(obj):
        for inner in _BRACKET_REF.findall(expr):
            if "::" in inner and inner.split("::")[0].strip().lower() in table_targets:
                return True
    return False


def _model_table_columns(docs, table_targets):
    """Column keys (db-name + display, lowercased) for every model column belonging to a
    target table (matched by column_id prefix), plus the display names for reporting."""
    keys, display = set(), []
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                cid = c.get("column_id", "") or ""
                if "::" in cid and cid.split("::")[0].strip().lower() in table_targets:
                    keys.add(cid.split("::")[-1].strip().lower())
                    nm = (c.get("name") or "").strip()
                    keys.add(nm.lower() if nm else cid.split("::")[-1].strip().lower())
                    display.append(nm or cid.split("::")[-1])
    return keys, display


def _refs_formula(obj, formula_ids):
    """True if any `[formula_x]` reference inside obj points at a formula id in formula_ids."""
    for expr in _iter_strings(obj):
        for inner in _BRACKET_REF.findall(expr):
            if inner.strip().lower() in formula_ids:
                return True
    return False


def _formula_id(f):
    return (f.get("id") or ("formula_" + (f.get("name", "") or ""))).strip().lower()


def _table_drop_plan(items, table_names):
    """Full transitive closure of what pruning `table_names` removes from the model(s): the
    tables + attaching joins + their physical columns, then by FIXPOINT every formula that
    references those columns (or another dropped formula), the formula-backed columns those
    formulas back, and dependent leaf vizzes. Returns removal sets (targets, dropped_fids,
    dropped_col_names) plus human-readable lists (columns, joins, formulas, vizzes)."""
    targets = {t.strip().lower() for t in table_names}
    docs = [_parse_edoc(it) for it in items]
    phys_keys, phys_display = _model_table_columns(docs, targets)

    all_formulas = []
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if node:
                all_formulas += (node.get("formulas") or [])

    dropped_fids = set()
    changed = True
    while changed:
        changed = False
        for f in all_formulas:
            fid = _formula_id(f)
            if fid in dropped_fids:
                continue
            if _refs_table_prefix(f, targets) or _refs_formula(f, dropped_fids):
                dropped_fids.add(fid)
                changed = True

    dropped_formula_names = [f.get("name") or f.get("id") for f in all_formulas
                             if _formula_id(f) in dropped_fids]

    formula_col_names = []
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for c in node.get("columns", []) or []:
                if (c.get("formula_id", "") or "").strip().lower() in dropped_fids:
                    formula_col_names.append(c.get("name") or c.get("formula_id"))

    dropped_col_names = set(phys_keys) | {(n or "").strip().lower() for n in formula_col_names if n}

    joins = []
    for doc in docs:
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            for mt in (node.get("model_tables") or []):
                for j in mt.get("joins", []) or []:
                    if ((mt.get("name", "") or "").strip().lower() in targets
                            or (j.get("with", "") or "").strip().lower() in targets
                            or _refs_table_prefix(j, targets)):
                        joins.append(j.get("name") or f"{mt.get('name','')} -> {j.get('with','')}")

    vizzes = []
    for doc in docs:
        lb = doc.get("liveboard")
        if lb:
            for viz in lb.get("visualizations", []) or []:
                ac = [(c.get("name", "") or "").strip().lower()
                      for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(a in dropped_col_names for a in ac) or _expr_refs(viz, dropped_col_names, dropped_col_names):
                    vizzes.append(viz.get("id") or viz.get("viz_id") or "(viz)")
        ans = doc.get("answer")
        if ans:
            ac = [(c.get("name", "") or "").strip().lower() for c in ans.get("answer_columns", [])]
            if any(a in dropped_col_names for a in ac) or _expr_refs(ans, dropped_col_names, dropped_col_names):
                vizzes.append(ans.get("name") or "(answer)")

    return {
        "targets":           targets,
        "dropped_fids":      dropped_fids,
        "dropped_col_names": dropped_col_names,
        "columns":  sorted(set(phys_display) | {n for n in formula_col_names if n}),
        "joins":    sorted(set(joins)),
        "formulas": sorted(set(dropped_formula_names)),
        "vizzes":   list(dict.fromkeys(vizzes)),
    }


def table_drop_preview(items, table_name):
    """Read-only preview of what pruning `table_name` out of the model(s) would remove — the
    table's columns, attaching joins, formulas that use it (transitively), the formula-backed
    columns those formulas back, and dependent leaf vizzes."""
    plan = _table_drop_plan(items, [table_name])
    return {k: plan[k] for k in ("columns", "joins", "formulas", "vizzes")}


def drop_tables(items, table_names):
    """Prune whole tables out of the model(s): remove their model_tables entry + attaching
    joins + physical columns + (transitively) formulas that use them + the formula-backed
    columns those formulas back, and drop dependent leaf vizzes (and layout tiles). Use when a
    table is EXCLUDED but NOT on the target, so the model must stop referencing it. Returns
    (new_items, summary counts)."""
    plan = _table_drop_plan(items, table_names)
    targets, dropped_fids, dcn = plan["targets"], plan["dropped_fids"], plan["dropped_col_names"]
    summary = {"tables": 0, "columns": 0, "joins": 0, "formulas": 0, "vizzes": 0}
    if not targets:
        return items, summary
    out = []
    for item in items:
        doc = _parse_edoc(item)
        for key in ("model", "worksheet"):
            node = doc.get(key)
            if not node:
                continue
            mts = node.get("model_tables")
            if isinstance(mts, list):
                kept_mt = []
                for mt in mts:
                    if (mt.get("name", "") or "").strip().lower() in targets:
                        summary["tables"] += 1
                        continue   # drop the whole table entry (its joins go with it)
                    if isinstance(mt.get("joins"), list):
                        kj = []
                        for j in mt["joins"]:
                            if (j.get("with", "") or "").strip().lower() in targets or _refs_table_prefix(j, targets):
                                summary["joins"] += 1
                            else:
                                kj.append(j)
                        mt["joins"] = kj
                    kept_mt.append(mt)
                node["model_tables"] = kept_mt
            if isinstance(node.get("formulas"), list):
                before = len(node["formulas"])
                node["formulas"] = [f for f in node["formulas"] if _formula_id(f) not in dropped_fids]
                summary["formulas"] += before - len(node["formulas"])
            if isinstance(node.get("columns"), list):
                before = len(node["columns"])
                node["columns"] = [
                    c for c in node["columns"]
                    if not (("::" in (c.get("column_id", "") or "")
                             and (c.get("column_id", "")).split("::")[0].strip().lower() in targets)
                            or (c.get("formula_id", "") or "").strip().lower() in dropped_fids)
                ]
                summary["columns"] += before - len(node["columns"])
        lb = doc.get("liveboard")
        if lb and isinstance(lb.get("visualizations"), list):
            kept, kept_ids = [], set()
            for viz in lb["visualizations"]:
                ac = [(c.get("name", "") or "").strip().lower()
                      for c in viz.get("answer", {}).get("answer_columns", [])]
                if any(a in dcn for a in ac) or _expr_refs(viz, dcn, dcn):
                    summary["vizzes"] += 1
                else:
                    kept.append(viz)
                    kept_ids.add(str(viz.get("id") or viz.get("viz_id") or ""))
            lb["visualizations"] = kept
            layout = lb.get("layout") or {}

            def _prune(tiles):
                return [t for t in tiles if str(t.get("visualization_id", "")) in kept_ids]

            if isinstance(layout.get("tiles"), list):
                layout["tiles"] = _prune(layout["tiles"])
            if isinstance(layout.get("tabs"), list):
                for tab in layout["tabs"]:
                    if isinstance(tab.get("tiles"), list):
                        tab["tiles"] = _prune(tab["tiles"])
        out.append({**item, "edoc": json.dumps(doc)})
    return out, summary
