"""Generate TML promotions across the whole input space, so tests cover cases nobody has hit yet.

The corpus test (test_error_corpus.py) measures how much of what we HAVE seen we can act on. That
is a floor, not coverage: on 2026-09-11 the corpus read 100% and the very next run produced a
message shape we had never seen. Past failures cannot tell you about the failure you have not had.

So this models the INPUT domain instead, the way a test lead would: enumerate the dimensions a
promotion actually varies along, then generate combinations of them. The dimensions here are the
ones that have teeth in this tool:

  object shape      table / model(+worksheet alias) / multi-table model with joins
  column kind       physical · formula · formula-on-formula · join key · viz reference
  warehouse state   present · different casing · absent · VOID · same type · within-family
                    drift · cross-family drift · unmappable type (struct/array)
  operator action   nothing · drop (bare) · drop (table-scoped) · realign · recase
  structure at risk table emptied · model table left with no selected columns · join orphaned ·
                    formula orphaned · formula-on-formula orphaned

Tests then assert INVARIANTS that must hold for every generated case (see test_tml_properties.py),
rather than expected outputs for specific ones. Invariants are what scale: one property covers the
whole generated space, including the combinations we never thought to write down.
"""

import json
import random
import string

# The warehouse states a column can be in, named so a failure message says which one broke.
WH_STATES = ("match", "case_drift", "absent", "void",
             "type_within_family", "type_cross_family", "type_unmappable")

_TYPES = {
    "match":              ("bigint", "INT64"),
    "case_drift":         ("bigint", "INT64"),
    "absent":             (None,     "VARCHAR"),
    "void":               ("void",   "VARCHAR"),
    "type_within_family": ("bigint", "DOUBLE"),      # the PATIENT_AGE shape
    "type_cross_family":  ("string", "INT64"),
    "type_unmappable":    ("struct<a:int>", "VARCHAR"),
}


def _name(rng, prefix):
    return prefix + "_" + "".join(rng.choice(string.ascii_lowercase) for _ in range(5))


def make_case(seed):
    """Build one synthetic promotion. Returns (items, warehouse_map, facts) where facts describes
    what was generated so a test can assert against intent rather than re-deriving it."""
    rng = random.Random(seed)
    n_tables = rng.randint(1, 4)
    tables, wh, facts = [], {}, {"tables": [], "columns": [], "formulas": [], "joins": [],
                                 "case_map": {}}

    for ti in range(n_tables):
        tname = _name(rng, f"t{ti}")
        cols, whcols = [], {}
        n_cols = rng.randint(1, 5)
        # Every table gets a join key, so multi-table models can actually join.
        specs = [("JOIN_KEY", "match")] + [
            (f"COL_{ci}", rng.choice(WH_STATES)) for ci in range(n_cols)]
        for cname, state in specs:
            wh_type, tml_type = _TYPES[state]
            cols.append({
                "name": cname.replace("_", " ").title(),
                "db_column_name": cname,
                "db_column_properties": {"data_type": tml_type},
            })
            if wh_type is not None:
                # Type maps are keyed lowercase by every reader, so casing drift lives in the
                # separate CASE map: the warehouse's real spelling of the column.
                whcols[cname.lower()] = wh_type
                facts["case_map"].setdefault(tname.lower(), {})[cname.lower()] = (
                    cname.title() if state == "case_drift" else cname)
            facts["columns"].append({"table": tname, "column": cname, "state": state})
        wh[tname.lower()] = whcols
        facts["tables"].append(tname)
        tables.append({"info": {"name": tname}, "edoc": json.dumps({"table": {
            "name": tname, "db": "hive_metastore", "schema": "s", "db_table": tname,
            "columns": cols}})})

    # One model over those tables: surfaced columns, joins, and a formula chain.
    key = rng.choice(("model", "worksheet"))
    mname = _name(rng, "m")
    model_tables, mcols = [], []
    for i, tname in enumerate(facts["tables"]):
        mt = {"name": tname}
        if i > 0:
            mt["joins"] = [{"name": f"j{i}", "with": facts["tables"][0],
                            "on": f"[{tname}::JOIN_KEY] = [{facts['tables'][0]}::JOIN_KEY]"}]
            facts["joins"].append((tname, facts["tables"][0]))
        model_tables.append(mt)
        # Surface a random, possibly empty, subset — an empty one is the "no columns selected" case
        for c in [f for f in facts["columns"] if f["table"] == tname]:
            if rng.random() < 0.6:
                mcols.append({"name": c["column"].replace("_", " ").title(),
                              "column_id": f"{tname}::{c['column']}"})

    formulas = []
    if mcols and rng.random() < 0.8:
        src = rng.choice(mcols)
        f1 = _name(rng, "f")
        formulas.append({"name": f1, "expr": f"sum([{src['column_id']}])"})
        facts["formulas"].append({"name": f1, "depends_on": src["column_id"]})
        mcols.append({"name": f1, "column_id": f"formula_{f1}"})
        if rng.random() < 0.5:                      # formula built on a formula
            f2 = _name(rng, "f")
            formulas.append({"name": f2, "expr": f"[formula_{f1}] * 2"})
            facts["formulas"].append({"name": f2, "depends_on": f"formula_{f1}"})
            mcols.append({"name": f2, "column_id": f"formula_{f2}"})

    node = {"name": mname, "model_tables": model_tables, "columns": mcols}
    if formulas:
        node["formulas"] = formulas
    items = tables + [{"info": {"name": mname}, "edoc": json.dumps({key: node})}]
    return items, wh, facts


def drop_candidates(facts, rng):
    """A random, table-scoped drop set drawn from the generated columns."""
    pool = [f"{c['table']}::{c['column']}" for c in facts["columns"]]
    if not pool:
        return set()
    return set(rng.sample(pool, rng.randint(1, len(pool))))
