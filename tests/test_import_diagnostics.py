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
    assert f[0]["object"] == "commerce"   # table resolved from the FQN, not the "unknown" header
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


def test_classify_drop_blocked_names_the_table_not_columns():
    # Real platform format (error 14544): names only the blocked TABLE, never the column/dependent.
    err = ("Unable to import tml due to following errors:<br/>- "
           "<b>fact_time_out_details_respbio_br</b>: Deleted columns have dependents.<br/><br/>"
           "<b>SOLUTION:</b><br/>Either fix the error objects or remove those objects.<br/>")
    f = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": err}])
    blocked = [x for x in f if x["kind"] == "drop_blocked_by_dependents"]
    assert len(blocked) == 1
    assert blocked[0]["object"] == "fact_time_out_details_respbio_br"
    assert blocked[0]["columns"] == [] and blocked[0]["dependents"] == []


def test_classify_drop_blocked_multiple_tables():
    err = ("Unable to import tml due to following errors:<br/>"
           "- <b>tbl_a</b>: Deleted columns have dependents.<br/>"
           "- <b>tbl_b</b>: Deleted columns have dependents.<br/><b>SOLUTION:</b> fix.")
    f = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": err}])
    tables = sorted(x["object"] for x in f if x["kind"] == "drop_blocked_by_dependents")
    assert tables == ["tbl_a", "tbl_b"]


def test_classify_drop_blocked_column_level_names_column_and_deps():
    # Format B (also 14544): names the deleted COLUMN and its dependent object(s).
    err = ("Deleted columns have dependents.<br/>- <b>NUCALA_POTENTIAL</b></br>"
           "<ul><li>Respbio Subnational Performance </li></ul><br/><b>SOLUTION:</b> fix.")
    f = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": err}])
    blocked = [x for x in f if x["kind"] == "drop_blocked_by_dependents"]
    assert len(blocked) == 1
    assert blocked[0].get("column") == "NUCALA_POTENTIAL"
    assert blocked[0]["dependents"] == ["Respbio Subnational Performance"]


def test_classify_join_unresolved_14540_named_table_not_other():
    err = ("Error while translating <b>1st</b> join of <b>dim_cid_targets_respbio_br</b>. "
           "<br/>No matches found for table dim_cid_targets_respbio_br.<br/>")
    f = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": err}])
    assert [x["kind"] for x in f] == ["join_unresolved"]           # not "other"
    assert "dim_cid_targets_respbio_br" in f[0]["tables"]


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


def test_drop_columns_qualified_scopes_to_one_table():
    # Two tables both have column X and both join on it. Dropping "A::X" (qualified) must remove
    # ONLY A's X and the A-side join ref — B's X and B's own usage stay. Prevents the CID over-drop.
    doc = {"model": {"name": "M",
        "columns": [{"name": "A X", "column_id": "A::X"}, {"name": "B X", "column_id": "B::X"}],
        "model_tables": [
            {"name": "A", "joins": [{"with": "hub", "on": "[A::X] = [hub::X]"}]},
            {"name": "B", "joins": [{"with": "hub", "on": "[B::X] = [hub::X]"}]},
            {"name": "hub"}]}}
    tblA = {"table": {"name": "A", "columns": [{"name": "X", "db_column_name": "X"}]}}
    tblB = {"table": {"name": "B", "columns": [{"name": "X", "db_column_name": "X"}]}}
    items = [{"edoc": json.dumps(doc), "info": {"name": "M"}},
             {"edoc": json.dumps(tblA), "info": {"name": "A"}},
             {"edoc": json.dumps(tblB), "info": {"name": "B"}}]
    fixed, man = drop_columns(items, {"A::X"})   # qualified: only table A
    m = json.loads(fixed[0]["edoc"])["model"]
    colids = {c["column_id"] for c in m["columns"]}
    assert "A::X" not in colids and "B::X" in colids          # only A's model column gone
    a_join = next(mt for mt in m["model_tables"] if mt["name"] == "A").get("joins", [])
    b_join = next(mt for mt in m["model_tables"] if mt["name"] == "B").get("joins", [])
    assert a_join == [] and len(b_join) == 1                   # only A's join dropped, B's kept
    a_cols = json.loads(fixed[1]["edoc"])["table"]["columns"]
    b_cols = json.loads(fixed[2]["edoc"])["table"]["columns"]
    assert a_cols == [] and len(b_cols) == 1                   # only A's physical X gone


def test_drop_columns_bare_still_hits_all_tables():
    # Back-compat: a BARE name still drops from every table (existing behaviour).
    doc = {"model": {"name": "M",
        "columns": [{"name": "A X", "column_id": "A::X"}, {"name": "B X", "column_id": "B::X"}]}}
    fixed, man = drop_columns([{"edoc": json.dumps(doc), "info": {"name": "M"}}], {"X"})
    m = json.loads(fixed[0]["edoc"])["model"]
    assert {c["column_id"] for c in m["columns"]} == set()     # both A::X and B::X gone


def test_drop_qualified_shared_join_key_keeps_unrelated_joins():
    # THE HCP_ID bug: a shared join key whose model DISPLAY name equals the physical ref tail.
    # Dropping tableA::HCP_ID must NOT sever the tableB<->tableC join on HCP_ID (a different table).
    # The earlier qualified test used distinct display names ("A X"/"B X") and so missed this.
    model = {"model": {"name": "M",
        "columns": [{"name": "HCP_ID", "column_id": "tablea::hcp_id"}],
        "model_tables": [
            {"name": "tableA", "joins": [{"name": "j_AC", "on": "[tableA::HCP_ID] = [tableC::HCP_ID]"}]},
            {"name": "tableB", "joins": [{"name": "j_BC", "on": "[tableB::HCP_ID] = [tableC::HCP_ID]"}]}]}}
    tbl = lambda n: {"table": {"name": n, "columns": [{"name": "HCP_ID", "db_column_name": "HCP_ID"}]}}
    items = [{"edoc": json.dumps(model), "info": {"name": "M"}}] + \
            [{"edoc": json.dumps(tbl(n)), "info": {"name": n}} for n in ("tableA", "tableB", "tableC")]
    fixed, man = drop_columns(items, {"tableA::HCP_ID"})
    m = json.loads(fixed[0]["edoc"])["model"]
    joins = {j["name"] for mt in m["model_tables"] for j in mt.get("joins", [])}
    assert joins == {"j_BC"}          # only tableA's join gone; tableB<->tableC survives
    cols = {json.loads(fixed[i]["edoc"])["table"]["name"]:
            [c["db_column_name"] for c in json.loads(fixed[i]["edoc"])["table"]["columns"]]
            for i in (1, 2, 3)}
    assert cols["tableA"] == [] and cols["tableB"] == ["HCP_ID"] and cols["tableC"] == ["HCP_ID"]


def test_drop_qualified_does_not_touch_same_name_column_in_other_model():
    # Two models each surface HCP_ID from their OWN table. Dropping tableA::HCP_ID (model MA) must
    # not drop model MB's HCP_ID column (which surfaces tableB, untargeted) via the shared name.
    mA = {"model": {"name": "MA", "columns": [{"name": "HCP_ID", "column_id": "tablea::hcp_id"}],
          "model_tables": [{"name": "tableA", "joins": [{"name": "jA", "on": "[tableA::HCP_ID] = [tableC::HCP_ID]"}]}]}}
    mB = {"model": {"name": "MB", "columns": [{"name": "HCP_ID", "column_id": "tableb::hcp_id"}],
          "model_tables": [{"name": "tableB", "joins": [{"name": "jB", "on": "[tableB::HCP_ID] = [tableC::HCP_ID]"}]}]}}
    tbl = lambda n: {"table": {"name": n, "columns": [{"name": "HCP_ID", "db_column_name": "HCP_ID"}]}}
    items = [{"edoc": json.dumps(mA), "info": {"name": "MA"}},
             {"edoc": json.dumps(mB), "info": {"name": "MB"}}] + \
            [{"edoc": json.dumps(tbl(n)), "info": {"name": n}} for n in ("tableA", "tableB", "tableC")]
    fixed, man = drop_columns(items, {"tableA::HCP_ID"})
    mb = json.loads(fixed[1]["edoc"])["model"]
    assert [c["column_id"] for c in mb["columns"]] == ["tableb::hcp_id"]   # MB's HCP_ID untouched
    assert {j["name"] for mt in mb["model_tables"] for j in mt.get("joins", [])} == {"jB"}


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


# ── Increment 2: the SAME diff pointed at the SOURCE warehouse map (out-of-sync TML) ──

def _tbl_item(name, cols):
    doc = {"table": {"name": name, "db": "d", "schema": "s", "db_table": name,
                     "columns": [{"db_column_name": c} for c in cols]}}
    return {"edoc": json.dumps(doc)}


def test_source_absent_flags_only_out_of_sync_column_case_insensitively():
    # respbio_fact has an out-of-sync column (opus_priority_account) gone from the source CDW;
    # CID (upper in TML) matches cid in the warehouse case-insensitively and must NOT be flagged;
    # a table the source read couldn't cover (ghost) is skipped -> no false positive.
    items = [_tbl_item("respbio_fact", ["amount", "opus_priority_account"]),
             _tbl_item("dim_cid", ["CID"]),
             _tbl_item("ghost", ["x"])]
    src_map = {"respbio_fact": {"amount": "amount"}, "dim_cid": {"cid": "cid"}}
    found = warehouse_missing_findings(items, src_map, connection="src")
    assert sorted((f["object"], f["column"]) for f in found) == \
        [("respbio_fact", "opus_priority_account")]


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
