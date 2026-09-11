"""Invariants that must hold for EVERY promotion, checked against generated inputs.

Example-based tests answer "does this known case work?". Properties answer "is there any input for
which this breaks?" — which is the question that matters when the failure you are trying to prevent
is one you have not seen yet. Each test below runs the whole generated space (tests/tml_factory.py)
and reports the seed of the first counterexample, so a failure is reproducible with one number.

If one of these ever goes red, do not weaken the property. The property is the specification.
"""

import json
import random

import pytest

from services.import_diagnostics import (
    drop_columns, dangling_reference_findings, table_cleanup_findings,
    model_tables_without_columns, realign_column_types, recase_columns,
    warehouse_missing_findings, warehouse_type_findings, _parse_edoc, prune_tables_whole,
)
from tests.tml_factory import make_case, drop_candidates

SEEDS = range(200)


def _cols_of(items):
    """{table: [db_column_name, …]} for every table doc in the set."""
    out = {}
    for it in items:
        d = _parse_edoc(it)
        t = d.get("table")
        if t and t.get("name"):
            out[t["name"]] = [c.get("db_column_name") for c in t.get("columns") or []]
    return out


def _model_refs(items):
    """Every column_id / formula name a model still surfaces."""
    refs = set()
    for it in items:
        d = _parse_edoc(it)
        for key in ("model", "worksheet"):
            node = d.get(key)
            if not node:
                continue
            for c in node.get("columns") or []:
                refs.add((c.get("column_id") or "").lower())
            for f in node.get("formulas") or []:
                refs.add(("formula_" + (f.get("name") or "")).lower())
    return refs


@pytest.mark.parametrize("seed", SEEDS)
def test_drop_leaves_no_dangling_reference(seed):
    # After ANY drop set, nothing may still point at something that was removed. A dangling
    # reference is the class ThoughtSpot reports only as an opaque "Schema validation failed",
    # so it must never leave the tool.
    items, _wh, facts = make_case(seed)
    drops = drop_candidates(facts, random.Random(seed))
    out, _man = drop_columns(items, drops)
    assert dangling_reference_findings(out) == [], f"dangling ref after dropping {sorted(drops)}"


@pytest.mark.parametrize("seed", SEEDS)
def test_drop_is_idempotent(seed):
    # Applying the same drop set twice must change nothing the second time. A non-idempotent drop
    # is how a re-export silently removes more than the operator approved (the "40 columns" bug).
    items, _wh, facts = make_case(seed)
    drops = drop_candidates(facts, random.Random(seed))
    once, man1 = drop_columns(items, drops)
    twice, man2 = drop_columns(once, drops)
    assert _cols_of(once) == _cols_of(twice)
    assert _model_refs(once) == _model_refs(twice)
    assert man2["columns"] == 0 and man2["formulas"] == []


@pytest.mark.parametrize("seed", SEEDS)
def test_scoped_drop_never_touches_another_table(seed):
    # `t1::COL` must never remove COL from t2. Shared join keys (HCP_ID, CID) live or die on this:
    # an unscoped drop once severed them across seven tables.
    items, _wh, facts = make_case(seed)
    if len(facts["tables"]) < 2:
        pytest.skip("needs 2+ tables")
    victim = facts["tables"][0]
    out, _man = drop_columns(items, {f"{victim}::JOIN_KEY"})
    after = _cols_of(out)
    assert "JOIN_KEY" not in (after.get(victim) or [])
    for other in facts["tables"][1:]:
        assert "JOIN_KEY" in after[other], f"scoped drop leaked onto {other}"


@pytest.mark.parametrize("seed", SEEDS)
def test_realign_changes_types_only(seed):
    # Realign rewrites a data_type token and nothing else. It must never add, remove or rename a
    # column — that is what makes it the safe alternative to dropping.
    items, _wh, facts = make_case(seed)
    target = {f"{c['table']}::{c['column']}": "INT64" for c in facts["columns"]}
    out, _n = realign_column_types(items, target)
    assert _cols_of(out) == _cols_of(items)
    for it in out:
        t = _parse_edoc(it).get("table")
        for c in (t or {}).get("columns") or []:
            assert c["db_column_properties"]["data_type"] == "INT64"


@pytest.mark.parametrize("seed", SEEDS)
def test_recase_changes_physical_names_only(seed):
    # Recasing touches db_column_name. Logical names are what every formula, viz and answer refers
    # to by, so if recasing moved them the whole promotion would dangle.
    items, _wh, facts = make_case(seed)
    case_map = {t.lower(): {c["column"].lower(): c["column"].upper()
                            for c in facts["columns"] if c["table"] == t}
                for t in facts["tables"]}
    out, _n = recase_columns(items, case_map)
    before = {it["info"]["name"]: [c.get("name") for c in
                                   (_parse_edoc(it).get("table") or {}).get("columns") or []]
              for it in items if _parse_edoc(it).get("table")}
    after = {it["info"]["name"]: [c.get("name") for c in
                                  (_parse_edoc(it).get("table") or {}).get("columns") or []]
             for it in out if _parse_edoc(it).get("table")}
    assert before == after, "recasing moved a logical name"


@pytest.mark.parametrize("seed", SEEDS)
def test_structural_hazards_are_detected_after_any_drop(seed):
    # Whatever a drop breaks structurally, SOME detector must see it. The tool is allowed to leave
    # a broken structure for the operator to resolve; it is not allowed to leave one it cannot name,
    # because unnamed is what reaches the screen as "unknown".
    items, _wh, facts = make_case(seed)
    drops = drop_candidates(facts, random.Random(seed + 7919))
    out, _man = drop_columns(items, drops)
    for it in out:
        d = _parse_edoc(it)
        t = d.get("table")
        if t and t.get("columns") == []:
            named = {f["table"].lower() for f in table_cleanup_findings(out)}
            assert t["name"].lower() in named, f"emptied table {t['name']} not detected"
    for it in out:
        d = _parse_edoc(it)
        for key in ("model", "worksheet"):
            node = d.get(key)
            if not node:
                continue
            used = {(c.get("column_id") or "").split("::")[0].lower()
                    for c in node.get("columns") or [] if "::" in (c.get("column_id") or "")}
            bare = [mt["name"] for mt in node.get("model_tables") or []
                    if mt.get("name") and mt["name"].lower() not in used]
            if bare:
                found = {t.lower() for f in model_tables_without_columns(out)
                         for t in f.get("tables", [])}
                assert set(b.lower() for b in bare) <= found, \
                    f"model table(s) {bare} left with no columns and NOT detected"


@pytest.mark.parametrize("seed", SEEDS)
def test_cleanup_converges_to_a_shippable_set(seed):
    # The property that decides whether a run finishes or dead-ends: after a drop, repeatedly
    # pruning what the drop broke must reach a FIXED POINT with no empty and no disconnected
    # tables left. If this ever loops or stalls, the operator sees a probe that keeps re-validating
    # and never clears — which is what "the tool hung" looks like from the room.
    items, _wh, facts = make_case(seed)
    drops = drop_candidates(facts, random.Random(seed + 104729))
    work, _man = drop_columns(items, drops)
    for _pass in range(10):
        hazards = table_cleanup_findings(work)
        if not hazards:
            break
        work, _s = prune_tables_whole(work, {f["table"] for f in hazards})
        # pruning a table can orphan formulas/columns elsewhere; clear those too
        work, _m2 = drop_columns(work, set())
    else:
        pytest.fail(f"cleanup did not converge in 10 passes (seed {seed}, drops {sorted(drops)})")
    assert table_cleanup_findings(work) == []
    assert dangling_reference_findings(work) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_recase_resolves_generated_casing_drift(seed):
    # Every column the factory planted with casing drift must end up spelled the warehouse's way
    # after recasing, and nothing else may move.
    items, _wh, facts = make_case(seed)
    out, _n = recase_columns(items, facts["case_map"])
    for it in out:
        t = _parse_edoc(it).get("table")
        if not t:
            continue
        want = facts["case_map"].get(t["name"].lower(), {})
        for c in t.get("columns") or []:
            expect = want.get((c.get("db_column_name") or "").lower())
            if expect:
                assert c["db_column_name"] == expect, \
                    f"{t['name']}.{c['db_column_name']} not recased to {expect}"


@pytest.mark.parametrize("seed", SEEDS)
def test_warehouse_findings_never_target_an_unread_table(seed):
    # A table the warehouse could not answer for must produce NO findings. Over-dropping on an
    # empty read is the nightmare case: it would strip every column of every table.
    items, _wh, _facts = make_case(seed)
    assert warehouse_missing_findings(items, {}) == []
    assert warehouse_type_findings(items, {}) == []


@pytest.mark.parametrize("seed", SEEDS)
def test_every_generated_column_state_is_classified_or_clean(seed):
    # For each column the factory planted in a known warehouse state, the detectors must agree with
    # that intent: absent -> missing finding, within/cross-family drift -> type finding, void ->
    # type finding, match -> silence. This is the "all-around" check: it covers every state in the
    # matrix on every seed, not only the states that have failed us in the field.
    items, wh, facts = make_case(seed)
    missing = {(f["object"].lower(), f["column"].lower())
               for f in warehouse_missing_findings(items, wh)}
    typed = {(f["object"].lower(), f["column"].lower())
             for f in warehouse_type_findings(items, wh)}
    for c in facts["columns"]:
        k = (c["table"].lower(), c["column"].lower())
        st = c["state"]
        if st == "absent":
            assert k in missing, f"{k} is absent from the warehouse but not flagged"
        elif st in ("type_within_family", "type_cross_family", "void"):
            assert k in typed, f"{k} is a {st} but not flagged"
        elif st == "match":
            assert k not in missing and k not in typed, f"{k} matches but was flagged ({st})"
        elif st == "type_unmappable":
            # We only report drift we can name; an unmappable warehouse type is left alone.
            assert k not in typed
