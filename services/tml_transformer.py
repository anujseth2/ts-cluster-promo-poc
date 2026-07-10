"""
TML transformation logic — pure dict/JSON operations, no thoughtspot_tml library.

Cross-cluster promotion keeps object names IDENTICAL across clusters (identity is
carried by obj_id, not by name). The only rewrites are on the data layer:

  1. FQN stripping on table refs inside models (force obj_id resolution on import)
  2. Connection name remap   source -> target   (clusters point at different connections)
  3. db / schema remap        source -> target   (clusters point at different warehouses)

Any value that is already a ThoughtSpot variable token (e.g. ${dc_db}) is left
untouched, so a future move to parameterised TML is a no-op here.
"""

import json
import yaml
from typing import Dict, List, Tuple, Optional


# Top-level TML type keys
TML_TYPE_KEYS = ("liveboard", "answer", "model", "worksheet", "table")


def _tml_type(doc: dict) -> Optional[str]:
    for key in TML_TYPE_KEYS:
        if key in doc:
            return key
    return None


def _is_var(v) -> bool:
    """True if the value is a ThoughtSpot variable token like ${dc_db} — leave those alone."""
    return isinstance(v, str) and v.strip().startswith("${")


# ── Dependency extraction ──────────────────────────────────────────────────────

def extract_model_refs(doc: dict) -> List[str]:
    """
    Return the names of models/worksheets referenced by a liveboard or answer.
    Liveboard: liveboard.visualizations[].answer.tables[].name
    Answer:    answer.tables[].name
    """
    refs = []
    for viz in doc.get("liveboard", {}).get("visualizations", []):
        for tbl in viz.get("answer", {}).get("tables", []):
            name = tbl.get("name") or tbl.get("alias")
            if name:
                refs.append(name)
    for tbl in doc.get("answer", {}).get("tables", []):
        name = tbl.get("name") or tbl.get("alias")
        if name:
            refs.append(name)
    return list(set(refs))


def extract_table_refs(doc: dict) -> List[str]:
    """
    Return the names of physical/logical tables referenced by a model or worksheet.
    Used to walk the dependency chain down to the tables a model sits on.
    """
    refs = []
    typ = _tml_type(doc)
    if typ in ("model", "worksheet"):
        node = doc[typ]
        # Worksheets use `tables`; Models (10.12+) use `model_tables`.
        for key in ("tables", "model_tables"):
            for t in node.get(key, []):
                name = t.get("name") or t.get("alias")
                if name:
                    refs.append(name)
    return list(set(refs))


# ── Data-layer remap helpers ────────────────────────────────────────────────────

def _remap_connection(node: dict, source_connection: str, target_connection: str):
    """Point a table/model's connection reference at the target cluster's connection."""
    conn = node.get("connection")
    if not isinstance(conn, dict) or not conn.get("name") or _is_var(conn["name"]):
        return
    if not source_connection or conn["name"] == source_connection:
        # The source connection's GUID/obj_id are meaningless on the target cluster, so
        # drop them and let the import resolve the connection by name on the target.
        conn.pop("fqn", None)
        conn.pop("obj_id", None)
        if target_connection:
            conn["name"] = target_connection


def _remap_location(node: dict, db_map: dict, schema_map: dict):
    """Rewrite db / schema to the target warehouse's names (skip variable tokens)."""
    for key, mapping in (("db", db_map), ("schema", schema_map)):
        val = node.get(key)
        if val and not _is_var(val) and mapping and val in mapping:
            node[key] = mapping[val]


# ── Single-document transform ──────────────────────────────────────────────────

def transform_doc(
    doc: dict,
    source_connection: str = "",
    target_connection: str = "",
    db_map: Optional[dict] = None,
    schema_map: Optional[dict] = None,
    table_remap: Optional[dict] = None,
    column_case_map: Optional[dict] = None,
) -> Tuple[dict, List[str]]:
    """
    Apply the data-layer transforms to a single TML document dict.
    Names are preserved. Returns (transformed_doc, warnings).

    table_remap: {source_table_name_lower -> {"db","schema","db_table"}} captured from the
    table matcher. When a physical table is a matched pair, its binding is repointed to the
    TARGET table's actual db/schema/db_table (so a renamed physical table still binds). This
    wins over the static db_map/schema_map; unmatched tables fall back to those.

    column_case_map: {source_table_name_lower -> {db_column_name_lower -> target_actual_case}}.
    Some warehouses bind external columns case-sensitively, so a source column CID cannot import
    against a target warehouse column cid. When the two match case-insensitively we recase ONLY
    the physical db_column_name to the target's casing; the logical `name` is left untouched so
    joins, formulas, and visualizations that reference it by name are unaffected.
    """
    warnings = []
    db_map    = db_map or {}
    schema_map = schema_map or {}
    table_remap = table_remap or {}
    column_case_map = column_case_map or {}

    # Feedback (Spotter reference questions + business terms) has no data layer. Keep obj_id
    # for cross-cluster identity, drop the cluster-local guid, and remap nothing — its column
    # references (search_tokens like [Job Id]) are by name, which is preserved across clusters.
    if "nls_feedback" in doc:
        doc.pop("guid", None)
        return doc, warnings

    typ = _tml_type(doc)
    if not typ:
        return doc, ["Unknown TML type — skipped"]

    obj = doc[typ]

    if typ == "table":
        # Physical table: this is where db / schema / db_table / connection actually live.
        obj.pop("fqn", None)
        _remap_connection(obj, source_connection, target_connection)
        tr = table_remap.get((obj.get("name", "") or "").strip().lower())
        if tr:
            # Matched pair: repoint to the target's physical table (disclosed on the matcher
            # screen, so not re-surfaced as a transform warning here).
            for k in ("db", "schema", "db_table"):
                if tr.get(k):
                    obj[k] = tr[k]
        else:
            _remap_location(obj, db_map, schema_map)

        # Align column casing to the target warehouse (case-sensitive binding). Recase only
        # db_column_name; leave the logical `name` alone so references by name still resolve.
        cc = column_case_map.get((obj.get("name", "") or "").strip().lower())
        if cc:
            for col in obj.get("columns", []) or []:
                dbn = col.get("db_column_name")
                if dbn:
                    tgt = cc.get(dbn.strip().lower())
                    if tgt and tgt != dbn:
                        col["db_column_name"] = tgt

    elif typ in ("model", "worksheet"):
        # Model references tables by name + fqn; strip fqn so import resolves by obj_id.
        # Worksheets use `tables`; Models (10.12+) use `model_tables`.
        for key in ("tables", "model_tables"):
            for t in obj.get(key, []):
                t.pop("fqn", None)
                _remap_connection(t, source_connection, target_connection)
                _remap_location(t, db_map, schema_map)

    elif typ == "answer":
        # An answer references its MODEL via tables[] carrying a source-cluster fqn. That fqn
        # is invalid on the target, so strip it to force obj_id resolution (same rule as the
        # model->table refs above). Names/columns are preserved cross-cluster.
        _strip_model_ref_fqns(obj)

    elif typ == "liveboard":
        # A liveboard embeds a full answer per visualization; strip the model-ref fqn in each.
        for viz in obj.get("visualizations", []):
            ans = viz.get("answer")
            if isinstance(ans, dict):
                _strip_model_ref_fqns(ans)

    return doc, warnings


def _strip_model_ref_fqns(answer: dict):
    """Strip the source-cluster fqn from an answer's model references (answer.tables[]) so the
    import resolves the model by obj_id on the target. Mirrors the model->table fqn strip; the
    leaf->model link carries obj_id, and a source fqn does not exist on the target cluster."""
    for t in answer.get("tables", []) or []:
        if isinstance(t, dict):
            t.pop("fqn", None)


# ── Batch transform ─────────────────────────────────────────────────────────────

def transform_items(
    items: List[dict],
    source_connection: str = "",
    target_connection: str = "",
    db_map: Optional[dict] = None,
    schema_map: Optional[dict] = None,
    table_remap: Optional[dict] = None,
    column_case_map: Optional[dict] = None,
) -> Tuple[List[dict], List[dict]]:
    """
    Transform a list of raw API items (each with 'edoc' string + 'info' dict).
    Returns (transformed_items, all_warnings).
    """
    result = []
    all_warnings = []

    for item in items:
        info = item.get("info", {})
        name = info.get("name", "unknown")
        edoc = item.get("edoc", "{}")
        doc  = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)

        doc, warns = transform_doc(doc, source_connection, target_connection,
                                   db_map, schema_map, table_remap, column_case_map)
        for w in warns:
            all_warnings.append({"object": name, "issue": w})

        result.append({**item, "edoc": json.dumps(doc)})

    return result, all_warnings


# ── Issue detection ─────────────────────────────────────────────────────────────

def detect_issues(items: List[dict]) -> List[dict]:
    """Flag objects with no obj_id — required for cross-cluster identity matching."""
    issues = []
    for item in items:
        info = item.get("info", {})
        name = info.get("name", "")
        typ  = info.get("type", "")
        edoc = item.get("edoc", "{}")
        doc  = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
        if not doc.get("obj_id"):
            issues.append({
                "object": name,
                "type":   typ,
                "issue":  "No obj_id set — required for cross-cluster identity matching",
            })
    return issues


# ── Serialisation helpers ────────────────────────────────────────────────────────

def items_to_files(items: List[dict]) -> Dict[str, str]:
    """
    Convert transformed API items → {relative_path: json_string} for git commit.
    Structure:  tables/<name>.table.tml
                models/<name>.model.tml
                liveboards/<name>.liveboard.tml
                answers/<name>.answer.tml
    """
    TYPE_FOLDER = {
        "model":     "models",
        "worksheet": "models",
        "liveboard": "liveboards",
        "answer":    "answers",
        "table":     "tables",
    }
    files = {}
    for item in items:
        info     = item.get("info", {})
        name     = info.get("name", "unknown")
        edoc     = item.get("edoc", "{}")
        doc      = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
        if "nls_feedback" in doc:
            # Feedback carries no name in the doc; use the associated model's name (info.name).
            safe_fb = name.replace(" ", "_").replace("/", "-")
            files[f"feedback/{safe_fb}.feedback.tml"] = json.dumps(doc, indent=2)
            continue
        typ      = _tml_type(doc)
        if not typ:
            continue
        obj_name  = doc[typ].get("name", name)
        folder    = TYPE_FOLDER.get(typ, typ + "s")
        safe_name = obj_name.replace(" ", "_").replace("/", "-")
        path      = f"{folder}/{safe_name}.{typ}.tml"
        files[path] = json.dumps(doc, indent=2)
    return files


def files_to_tml_strings(files: Dict[str, str]) -> List[str]:
    """
    Convert {path: content} from git back to a list of TML strings for import,
    ordered so dependencies import first: tables -> models -> feedback -> liveboards/answers.
    Feedback imports after its model (it references the model's columns by name).
    """
    tables, models, feedback, others = [], [], [], []
    for path, content in files.items():
        if path.startswith("tables/"):
            tables.append(content)
        elif path.startswith("models/"):
            models.append(content)
        elif path.startswith("feedback/"):
            feedback.append(content)
        else:
            others.append(content)
    return tables + models + feedback + others


# ── Feedback (Spotter reference questions + business terms) granular selection ────

def _feedback_phrase(entry: dict) -> str:
    """Human-readable label for one feedback entry (robust to field naming)."""
    return (entry.get("feedback_phrase")
            or entry.get("parent_question")
            or entry.get("search_tokens")
            or "").strip()


def feedback_key(model: str, type_: str, phrase: str) -> Tuple[str, str, str]:
    """Stable identity for one feedback entry across re-exports."""
    return (model, type_, phrase)


def parse_feedback_items(items: List[dict]) -> List[dict]:
    """
    Flatten raw FEEDBACK API items into selectable picker entries. One dict per
    nls_feedback.feedback[] entry: {model, type, phrase, tokens}. Non-feedback items
    are ignored. The stable selection key is feedback_key(model, type, phrase).
    """
    out = []
    for item in items:
        edoc = item.get("edoc", "") or ""
        if not edoc.strip():
            continue
        doc = json.loads(edoc) if edoc.strip().startswith("{") else yaml.safe_load(edoc)
        if not isinstance(doc, dict) or "nls_feedback" not in doc:
            continue
        model = item.get("info", {}).get("name", "unknown")
        for e in (doc.get("nls_feedback", {}) or {}).get("feedback", []) or []:
            out.append({
                "model":  model,
                "type":   e.get("type", ""),
                "phrase": _feedback_phrase(e),
                "tokens": (e.get("search_tokens") or "").strip(),
            })
    return out


def filter_feedback(items: List[dict], selected_keys) -> List[dict]:
    """
    Keep only feedback entries whose feedback_key is in selected_keys. Feedback docs
    left with no selected entries are dropped entirely; non-feedback items pass through
    untouched. selected_keys=None -> no filtering (promote all feedback, back-compat).
    """
    if selected_keys is None:
        return items
    result = []
    for item in items:
        edoc = item.get("edoc", "") or ""
        doc = (json.loads(edoc) if edoc.strip().startswith("{")
               else yaml.safe_load(edoc)) if edoc.strip() else {}
        if not isinstance(doc, dict) or "nls_feedback" not in doc:
            result.append(item)
            continue
        model   = item.get("info", {}).get("name", "unknown")
        entries = (doc.get("nls_feedback", {}) or {}).get("feedback", []) or []
        kept = [e for e in entries
                if feedback_key(model, e.get("type", ""), _feedback_phrase(e)) in selected_keys]
        if not kept:
            continue
        doc["nls_feedback"]["feedback"] = kept
        result.append({**item, "edoc": json.dumps(doc)})
    return result
