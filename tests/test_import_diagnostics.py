"""L1: services/import_diagnostics — error classification, warehouse diff, drops (pure logic).

Error strings below are the exact shapes ThoughtSpot emits (see the module docstring), so this
locks the classifier to reality.
"""
import json

from services.import_diagnostics import (
    classify_import_errors, warehouse_missing_findings, friendly_error,
    drop_columns, drop_vizzes, drop_tables, column_usage, column_dependents,
    column_drop_cascade, dangling_reference_findings, table_cleanup_findings,
)


def test_table_cleanup_flags_empty_table():
    # all columns dropped -> 0 left -> import "0 columns. Not allowed." -> must drop the table.
    item = {"edoc": json.dumps({"table": {"name": "lupus_x", "columns": []}}), "info": {"name": "t"}}
    f = table_cleanup_findings([item])
    assert len(f) == 1 and f[0]["kind"] == "drop_table" and f[0]["reason"] == "empty"
    assert f[0]["table"] == "lupus_x"


def test_table_cleanup_flags_disconnected_table():
    # C has no join in or out (its join key was dropped) -> unreachable -> drop it. A<->B stay.
    doc = {"model": {"name": "M", "model_tables": [
        {"name": "A", "joins": [{"with": "B", "on": "[A::k] = [B::k]"}]},
        {"name": "B"},
        {"name": "C"}]}}
    f = table_cleanup_findings([{"edoc": json.dumps(doc), "info": {"name": "m"}}])
    assert {x["table"] for x in f if x["reason"] == "disconnected"} == {"C"}


def test_table_cleanup_single_table_model_not_flagged():
    doc = {"model": {"name": "M", "model_tables": [{"name": "solo"}]}}
    assert table_cleanup_findings([{"edoc": json.dumps(doc), "info": {"name": "m"}}]) == []


def test_dangling_reference_findings_flags_formula_pointing_at_removed_formula():
    # "Reach" references [formula_Target Count] which is NOT among the model's formulas — the exact
    # class ThoughtSpot reports only as opaque "Schema validation failed". Detector must name it.
    doc = {"model": {"name": "M",
        "columns": [{"name": "Reach", "column_id": "formula_Reach"},
                    {"name": "Region", "column_id": "t::Region"}],
        "formulas": [{"id": "formula_Reach", "name": "Reach",
                      "expr": "[formula_Called On] / [formula_Target Count]"},
                     {"id": "formula_Called On", "name": "Called On", "expr": "count([t::HCP])"}]}}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    found = dangling_reference_findings([item])
    assert len(found) == 1
    f = found[0]
    assert f["kind"] == "dangling_ref" and f["name"] == "Reach"
    assert "formula_Target Count" in f["missing"]


def test_dangling_reference_findings_conservative_ignores_non_formula_refs():
    # A bare [Display] ref (no formula_ prefix) resolves to a column/parameter we don't enumerate —
    # must NOT be flagged, or we'd wrongly drop valid objects.
    doc = {"model": {"name": "M", "columns": [],
        "formulas": [{"id": "formula_A", "name": "A", "expr": "[Some Display Column] * 2"}]}}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    assert dangling_reference_findings([item]) == []


def test_dangling_reference_findings_clean_when_all_resolve():
    doc = {"model": {"name": "M", "columns": [],
        "formulas": [{"id": "formula_A", "name": "A", "expr": "sum([t::x])"},
                     {"id": "formula_B", "name": "B", "expr": "[formula_A] + 1"}]}}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    assert dangling_reference_findings([item]) == []

# ── classify_import_errors ─────────────────────────────────────────────────────

def test_classify_ok_rows_ignored():
    assert classify_import_errors([{"name": "x", "status": "OK", "error": ""}]) == []


def test_classify_missing_in_warehouse_14536():
    err = ("External column with name: workspace.athoz.commerce.Ghost does not exist in "
           "connection Sisense Migration - Databricks.")
    f = classify_import_errors([{"name": "commerce", "status": "ERROR", "error": err}])
    assert len(f) == 1 and f[0]["kind"] == "missing_in_target_warehouse"
    assert f[0]["column"] == "Ghost"
    assert f[0]["connection"] == "Sisense Migration - Databricks"


def test_classify_reports_all_missing_in_one_message():
    # findall -> every column in a single message is captured (not just the first).
    err = ("External column with name: db.s.t.A does not exist in connection C. "
           "External column with name: db.s.t.B does not exist in connection C.")
    f = classify_import_errors([{"name": "t", "status": "ERROR", "error": err}])
    assert {x["column"] for x in f} == {"A", "B"}


def test_classify_type_mismatch():
    err = ("DataType INT64 does not match CDW DataType for column with name "
           "workspace.athoz.commerce.Quantity in connection Sisense Migration - Databricks.")
    f = classify_import_errors([{"name": "commerce", "status": "ERROR", "error": err}])
    assert f[0]["kind"] == "type_mismatch" and f[0]["source_type"] == "INT64"
    assert f[0]["column"] == "Quantity"


def test_classify_drop_blocked_by_dependents():
    err = ("Deleted columns have dependents.<br/>- <b>Revenue</b><ul><li>Revenue by Brand</li></ul>")
    f = classify_import_errors([{"name": "commerce", "status": "ERROR", "error": err}])
    assert f[0]["kind"] == "drop_blocked_by_dependents"
    assert "Revenue" in f[0]["columns"] and "Revenue by Brand" in f[0]["dependents"]


def test_classify_invalid_formula_ids():
    err = ("Model/Worksheet columns use invalid formula IDs.<br/>- <b>Bio Pen (COPD)</b>"
           "<ul><li>* formula_Bio Pen (COPD)</li></ul>- <b>Nucala Target Count (HCP)</b>"
           "<ul><li>* formula_Nucala Target Count (HCP)</li></ul><b>SOLUTION:</b> fix them.")
    f = classify_import_errors([{"name": "M", "status": "ERROR", "error": err}])
    assert len(f) == 1 and f[0]["kind"] == "invalid_formula_ids"
    assert set(f[0]["formulas"]) == {"Bio Pen (COPD)", "Nucala Target Count (HCP)"}


def test_drop_columns_by_formula_name_removes_formula_and_column():
    doc = {"model": {"name": "M",
                     "columns": [{"name": "Bio Pen (COPD)", "column_id": "formula_Bio Pen (COPD)"},
                                 {"name": "Region", "column_id": "t::Region"}],
                     "formulas": [{"name": "Bio Pen (COPD)", "expr": "sum([x])"}]}}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    fixed, man = drop_columns([item], {"Bio Pen (COPD)"})   # drop by formula name
    out = json.loads(fixed[0]["edoc"])["model"]
    assert {c["name"] for c in out["columns"]} == {"Region"}   # surfacing column gone
    assert out["formulas"] == []                                # formula gone
    assert "Bio Pen (COPD)" in man["formulas"]


def test_drop_cascades_formula_that_references_a_dropped_formula():
    # A formula that references ANOTHER formula does so by its `formula_<name>` id form:
    #   "Nucala Target Reach (HCP)"  ->  [formula_Nucala Target Count (HCP)]
    # Dropping the referenced formula must also drop the referencing one (and its surfacing
    # column), else it dangles as a "Schema validation failed" on import. Regression for the
    # GSK Respbio model: _refs_any matched bare names but not the `formula_` prefix. (grounded)
    doc = {"model": {"name": "M",
        "columns": [
            {"name": "Nucala Target Count (HCP)", "column_id": "formula_Nucala Target Count (HCP)"},
            {"name": "Nucala Target Reach (HCP)", "column_id": "formula_Nucala Target Reach (HCP)"},
            {"name": "Region", "column_id": "t::Region"},
        ],
        "formulas": [
            {"id": "formula_Nucala Target Count (HCP)", "name": "Nucala Target Count (HCP)",
             "expr": "count([t::HCP])"},
            {"id": "formula_Nucala Target Reach (HCP)", "name": "Nucala Target Reach (HCP)",
             "expr": "[formula_Nucala Target Count Called on (HCP)] / [formula_Nucala Target Count (HCP)]"},
        ]}}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    fixed, man = drop_columns([item], {"Nucala Target Count (HCP)"})   # drop the referenced formula
    out = json.loads(fixed[0]["edoc"])["model"]
    fnames = {f["name"] for f in out["formulas"]}
    cnames = {c["name"] for c in out["columns"]}
    assert "Nucala Target Count (HCP)" not in fnames         # the dropped formula
    assert "Nucala Target Reach (HCP)" not in fnames         # references it via formula_ id -> cascaded
    assert "Nucala Target Reach (HCP)" not in cnames         # its surfacing column too
    assert "Region" in cnames                                 # unrelated column kept


def test_classify_unrecognised_is_other():
    f = classify_import_errors([{"name": "x", "status": "ERROR", "error": "kaboom"}])
    assert f[0]["kind"] == "other" and f[0]["error"] == "kaboom"


# ── warehouse_missing_findings (CDW-sourced, the new single source of truth) ────

def test_warehouse_missing_verified_lists_all_at_once(commerce_table_item):
    # CDW has only 2 of the 11 commerce columns -> the other 9 are missing, all verified.
    cdw = {"commerce": {"visit_id": "Visit_ID", "revenue": "Revenue"}}
    findings = warehouse_missing_findings([commerce_table_item], cdw)
    cols = {f["column"] for f in findings}
    assert "Quantity" in cols and "Gender" in cols and "Visit_ID" not in cols
    assert len(findings) == 9
    assert all(f["verified"] for f in findings)


def test_warehouse_missing_fallback_is_unverified(commerce_table_item):
    # No CDW map for the table -> fall back to org-modeled map, flagged unverified.
    org = {"commerce": {"visit_id": "Visit_ID"}}
    findings = warehouse_missing_findings([commerce_table_item], {}, fallback_map=org)
    assert findings and all(not f["verified"] for f in findings)
    assert all("caveat" in f for f in findings)


def test_warehouse_missing_no_map_skips(commerce_table_item):
    assert warehouse_missing_findings([commerce_table_item], {}, fallback_map={}) == []


# ── friendly_error (humanised messages) ─────────────────────────────────────────

def test_friendly_error_suspended_warehouse():
    h, a, _ = friendly_error("Failed to initialize pool: Your free trial has ended and all of "
                             "your virtual warehouses have been suspended.")
    assert h and "warehouse" in h.lower() and a


def test_friendly_error_permission():
    h, _, _ = friendly_error("Error code 10086: not authorized")
    assert h and "permission" in h.lower()


def test_friendly_error_unknown_returns_none_headline():
    h, a, raw = friendly_error("totally novel error")
    assert h is None and a is None and raw == "totally novel error"


# ── drops, on the REAL model + liveboard ────────────────────────────────────────

def test_drop_columns_removes_from_model_and_dependent_viz(model_item, liveboard_item):
    # 'Brand' is a model column and feeds the "Revenue by Brand" viz on the liveboard.
    fixed, man = drop_columns([model_item, liveboard_item], {"Brand"})
    assert man["columns"] >= 1
    assert man["vizzes"] >= 1                      # the Brand viz goes with it


def test_drop_columns_cascades_join(model_item):
    # Brand_ID feeds the commerce->brand join; dropping it must remove that join (not dangle).
    fixed, man = drop_columns([model_item], {"Brand_ID"})
    assert man["joins"] >= 1
    doc = json.loads(fixed[0]["edoc"])
    for mt in doc["model"]["model_tables"]:
        for j in mt.get("joins", []):
            assert "Brand_ID" not in j.get("on", "")   # no dangling reference left


def test_drop_columns_cascade_removes_dependent_viz_and_prunes_tile(model_item, liveboard_item):
    fixed, man = drop_columns([model_item, liveboard_item], {"Brand"})
    doc = json.loads(fixed[1]["edoc"])
    viz_ids = {v["id"] for v in doc["liveboard"]["visualizations"]}
    tile_ids = {t["visualization_id"] for t in doc["liveboard"]["layout"]["tiles"]}
    assert "Viz_1" not in viz_ids            # Revenue by Brand removed
    assert "Viz_1" not in tile_ids           # and its layout tile pruned


def test_drop_columns_removes_formula_and_its_surfacing_column():
    # A model column 'Bio Pen' surfaces a formula that references dropped column CID. Dropping CID
    # must remove BOTH the formula AND the column that surfaces it (column_id 'formula_Bio Pen'),
    # else that column dangles as an "invalid formula ID" on import.
    doc = {"model": {
        "name": "M",
        "columns": [
            {"name": "CID", "column_id": "t::CID"},
            {"name": "Bio Pen", "column_id": "formula_Bio Pen"},
            {"name": "Region", "column_id": "t::Region"},
        ],
        "formulas": [{"name": "Bio Pen", "expr": "count([CID])"}],
    }}
    item = {"edoc": json.dumps(doc), "info": {"name": "M"}}
    fixed, man = drop_columns([item], {"CID"})
    out = json.loads(fixed[0]["edoc"])["model"]
    names = {c["name"] for c in out["columns"]}
    assert "CID" not in names           # the dropped column
    assert "Bio Pen" not in names       # its formula-surfacing column — no longer dangles
    assert "Region" in names            # unrelated column kept
    assert out["formulas"] == []        # the formula went too
    assert "Bio Pen" in man["formulas"]


def test_column_drop_cascade_is_dry_run(model_item):
    before = json.loads(model_item["edoc"])
    man = column_drop_cascade([model_item], {"Brand_ID"})
    after = json.loads(model_item["edoc"])
    assert man["joins"] >= 1                  # reports what would go
    assert before == after                    # but mutates nothing


def test_column_usage_finds_liveboard_dependents(model_item, liveboard_item):
    usage = column_usage([model_item, liveboard_item], "Brand")
    kinds = {u["kind"] for u in usage}
    assert "liveboard" in kinds                   # the Brand viz on the liveboard


def test_column_dependents_reports_blast_radius(model_item):
    deps = column_dependents([model_item], ["Brand_ID"])
    # Brand_ID feeds the commerce->brand join in the model.
    assert deps["joins"] or deps["formulas"] or deps["model_columns"]


def test_drop_vizzes_prunes_layout_tiles(liveboard_item):
    fixed, dropped = drop_vizzes([liveboard_item], ["Viz_1"])
    assert dropped == 1
    import json
    doc = json.loads(fixed[0]["edoc"])
    ids = {v["id"] for v in doc["liveboard"]["visualizations"]}
    tiles = {t["visualization_id"] for t in doc["liveboard"]["layout"]["tiles"]}
    assert "Viz_1" not in ids and "Viz_1" not in tiles


def test_drop_tables_prunes_dimension_from_model(model_item):
    fixed, summary = drop_tables([model_item], {"country"})
    import json
    doc = json.loads(fixed[0]["edoc"])
    tbls = {mt["name"] for mt in doc["model"]["model_tables"]}
    assert "country" not in tbls
    assert summary["tables"] == 1
