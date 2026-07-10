"""
Cross-cluster table matching — the general case where the target ALREADY has
tables (you cannot delete them). Identity across clusters is obj_id, not GUID
(GUIDs are cluster-local and not settable), so the job is: for each source table,
decide whether a counterpart exists on the target, with a confidence score and the
evidence behind it, so import can update-in-place instead of creating a duplicate.

Schemas drift across environments, so a match must tolerate missing / additional
columns: we score on physical coordinates AND column overlap, and always return a
column diff alongside the score.

Pure logic — no network. Feed it parsed table TML dicts; the CLI wires it to a
cluster (scripts/match_tables.py).
"""

import re

# Each signal is scored in [0,1]; confidence = 100 * weighted sum.
#
# The column structure is the identity: it is the one thing that survives a move to a
# different warehouse, and it does not depend on naming conventions. So columns carry
# the weight, scored type-aware (a shared column whose type also matches counts more
# than a shared name whose type drifted).
#
# db_table drifts across environments (prefix/suffix like commerce -> commerce_prod),
# so it is matched FUZZILY (token overlap) at a low weight — useful only as a
# tie-breaker between two structurally-similar target tables.
#
# Display name carries a little. schema and db are NOT scored: across clusters they are
# EXPECTED to differ (that is what db_map/schema_map remap). All four name-ish fields
# are still computed for context / human tie-breaking.
CONFIDENCE_WEIGHTS = {
    "columns":  0.85,
    "name":     0.10,
    "db_table": 0.05,
}

MATCH_THRESHOLD  = 85   # >= -> confident MATCH
REVIEW_THRESHOLD = 60   # >= -> LIKELY (confirm); below -> NO_MATCH (create new)
AMBIGUOUS_MARGIN = 10   # runner-up within this of the best -> AMBIGUOUS (manual pick)


def _lower(s):
    return (s or "").strip().lower()


def _apply_map(value, mapping):
    """Apply a db_map / schema_map remap so source coords compare against the target."""
    return mapping.get(value, value) if mapping else value


def _token_overlap(a, b):
    """Fuzzy identifier similarity by token sets — tolerates prefix/suffix drift
    (e.g. 'commerce' vs 'commerce_prod' vs 'dim_commerce')."""
    ta = {x for x in re.split(r"[^a-z0-9]+", _lower(a)) if x}
    tb = {x for x in re.split(r"[^a-z0-9]+", _lower(b)) if x}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def physical_coords(doc):
    """Pull the physical identity of a table from its TML dict."""
    t = doc.get("table", {}) or {}
    conn = t.get("connection", {}) or {}
    return {
        "name":       t.get("name", ""),
        "db":         t.get("db", ""),
        "schema":     t.get("schema", ""),
        "db_table":   t.get("db_table", ""),
        "connection": conn.get("name", ""),
        "obj_id":     doc.get("obj_id", ""),
    }


def column_signature(doc, casefold=True):
    """{physical_column_name -> data_type}, keyed on the warehouse column name
    (db_column_name) so edited display names don't break matching.

    casefold=True (default) lowercases the column-name key — right for the fuzzy MATCHER,
    which must find a renamed/drifted counterpart regardless of case. casefold=False keeps the
    key case-exact — right for the prune/add DECISION, so a column that differs only in case
    (Revenue vs revenue) is treated as a genuine difference, not silently conflated. The data
    type is always compared case-insensitively (types like INT64 are not case-significant)."""
    keyf = _lower if casefold else (lambda s: (s or "").strip())
    sig = {}
    for c in (doc.get("table", {}) or {}).get("columns", []) or []:
        col = keyf(c.get("db_column_name") or c.get("name"))
        if not col:
            continue
        sig[col] = _lower((c.get("db_column_properties") or {}).get("data_type", ""))
    return sig


def compare_columns(src_sig, tgt_sig):
    """Set diff of two column signatures + overlap scores.
    jaccard    = name overlap only.
    similarity = type-aware: a shared column counts 1.0 if its type also matches
                 (or a type is unknown), 0.5 if the type drifted — penalising missing
                 and extra columns via the union denominator."""
    s, t = set(src_sig), set(tgt_sig)
    shared, union = s & t, s | t
    sim_num = sum(1.0 if (src_sig[c] == tgt_sig[c] or not (src_sig[c] and tgt_sig[c])) else 0.5
                  for c in shared)
    return {
        "shared":            sorted(shared),
        "missing_on_target": sorted(s - t),   # source has, target lacks -> promotion ADDS it to target; needs
                                              # the column in the target WAREHOUSE, else FLAG for the reviewer
                                              # (add it / decide to exclude) — never silently drop from the model
        "extra_on_target":   sorted(t - s),   # target has, source lacks -> dropped from the TARGET table when
                                              # the source TML overwrites it (check downstream dependents)
        "type_mismatch":     sorted(c for c in shared
                                    if src_sig[c] and tgt_sig[c] and src_sig[c] != tgt_sig[c]),
        "jaccard":           (len(shared) / len(union)) if union else 0.0,
        "similarity":        (sim_num / len(union)) if union else 0.0,
    }


def score_pair(src_doc, tgt_doc, db_map=None, schema_map=None):
    """Confidence (0-100) that src and tgt are the same table, + signals + column diff."""
    sp, tp = physical_coords(src_doc), physical_coords(tgt_doc)
    cols = compare_columns(column_signature(src_doc), column_signature(tgt_doc))
    scored = {
        "columns":  cols["similarity"],
        "name":     1.0 if sp["name"] and _lower(sp["name"]) == _lower(tp["name"]) else 0.0,
        "db_table": _token_overlap(sp["db_table"], tp["db_table"]),
    }
    confidence = round(100 * sum(CONFIDENCE_WEIGHTS[k] * v for k, v in scored.items()))
    # Context only (NOT scored): whether schema/db line up after remap. Differing is
    # expected across clusters; shown so a human can break ties / sanity-check.
    context = {
        "schema_match": bool(tp["schema"] and _lower(_apply_map(sp["schema"], schema_map)) == _lower(tp["schema"])),
        "db_match":     bool(tp["db"] and _lower(_apply_map(sp["db"], db_map)) == _lower(tp["db"])),
    }
    return {"confidence": confidence, "signals": scored, "context": context,
            "columns": cols, "target": tp}


STRUCTURAL_FLOOR = 0.85   # very high column overlap -> never silently "create new"


def _decide(best, runner):
    if not best:
        return "NO_MATCH"
    conf = best["confidence"]
    if conf < REVIEW_THRESHOLD:
        # Low score usually means "no counterpart -> create new". But if the column
        # structure strongly matches (e.g. only the physical table name differs),
        # don't silently create a duplicate — surface it for a human to decide.
        return "REVIEW" if best["columns"]["similarity"] >= STRUCTURAL_FLOOR else "NO_MATCH"
    if (runner and runner["confidence"] >= REVIEW_THRESHOLD
            and conf - runner["confidence"] < AMBIGUOUS_MARGIN):
        return "AMBIGUOUS"
    return "MATCH" if conf >= MATCH_THRESHOLD else "REVIEW"


def match_tables(source_docs, target_docs, db_map=None, schema_map=None,
                 source_connection="", target_connection=""):
    """
    For each source table, score it against every target candidate (optionally
    filtered to the relevant connections) and return the best match + decision.

    decision: MATCH (align obj_id, import updates in place) | REVIEW | AMBIGUOUS
              (manual pick) | NO_MATCH (no counterpart -> created fresh on import).
    """
    def _scope(docs, conn):
        if not conn:
            return docs
        out = []
        for d in docs:
            c = physical_coords(d)["connection"]
            if not c or _lower(c) == _lower(conn):
                out.append(d)
        return out

    targets = _scope(target_docs, target_connection)
    results = []
    for sd in _scope(source_docs, source_connection):
        scored = sorted(
            (score_pair(sd, td, db_map, schema_map) for td in targets),
            key=lambda x: x["confidence"], reverse=True,
        )
        best   = scored[0] if scored else None
        runner = scored[1] if len(scored) > 1 else None
        results.append({
            "source":    physical_coords(sd),
            "best":      best,
            "runner_up": runner,
            "decision":  _decide(best, runner),
        })
    return results
