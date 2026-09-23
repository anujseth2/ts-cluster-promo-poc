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


def test_realign_column_types_sets_scoped_type_only():
    from services.import_diagnostics import realign_column_types
    # Two tables both have HCP_ID; realign ONLY tableA's HCP_ID (VARCHAR -> BIGINT).
    def _t(name):
        return {"edoc": json.dumps({"table": {"name": name, "columns": [
            {"name": "HCP_ID", "db_column_name": "HCP_ID",
             "db_column_properties": {"data_type": "VARCHAR"}}]}})}
    items = [_t("tableA"), _t("tableB")]
    out, n = realign_column_types(items, {"tableA::HCP_ID": "BIGINT"})
    assert n == 1
    a = json.loads(out[0]["edoc"])["table"]["columns"][0]["db_column_properties"]["data_type"]
    b = json.loads(out[1]["edoc"])["table"]["columns"][0]["db_column_properties"]["data_type"]
    assert a == "BIGINT" and b == "VARCHAR"      # scoped: tableB untouched


def test_source_audit_realign_resolves_stale_hcp_id():
    # The Source Audit (2b) realign contract: a stale HCP_ID (TML VARCHAR, source warehouse bigint)
    # is flagged, the realign token comes from the SOURCE type (bigint -> INT64, never 'bigint'),
    # and after realigning it no longer flags (INT64 vs bigint are the same family).
    from services.import_diagnostics import (
        warehouse_type_findings, warehouse_type_to_ts, realign_column_types)
    doc = {"table": {"name": "fact_x", "db": "d", "schema": "s", "db_table": "fact_x", "columns": [
        {"name": "HCP ID", "db_column_name": "HCP_ID",
         "db_column_properties": {"data_type": "VARCHAR"}}]}}
    src_types = {"fact_x": {"hcp_id": "bigint"}}
    found = warehouse_type_findings([{"edoc": json.dumps(doc)}], src_types)
    assert [(f["object"], f["column"]) for f in found] == [("fact_x", "HCP_ID")]
    tok = warehouse_type_to_ts(src_types["fact_x"]["hcp_id"])
    assert tok == "INT64"                                   # NOT the raw 'bigint' (would break import)
    sk = f"{found[0]['object']}::{found[0]['column']}"
    out, n = realign_column_types([{"edoc": json.dumps(doc)}], {sk: tok})
    assert n == 1
    # after realign the column is INT64, and it no longer flags against the same source read
    assert warehouse_type_findings(out, src_types) == []


def test_recase_columns_inplace_matches_transformer_rule():
    # In-place recasing (Source Audit "Apply recasings", no re-export) rewrites db_column_name to
    # the warehouse casing and leaves the logical name alone — same rule as the export transform.
    from services.import_diagnostics import recase_columns
    doc = {"table": {"name": "commerce", "columns": [
        {"name": "revenue", "db_column_name": "revenue",
         "db_column_properties": {"data_type": "DOUBLE"}}]}}
    out, n = recase_columns([{"edoc": json.dumps(doc)}], {"commerce": {"revenue": "REVENUE"}})
    col = json.loads(out[0]["edoc"])["table"]["columns"][0]
    assert n == 1
    assert col["db_column_name"] == "REVENUE"        # recased to warehouse casing
    assert col["name"] == "revenue"                  # logical name untouched
    # idempotent: applying the same casing again changes nothing
    out2, n2 = recase_columns(out, {"commerce": {"revenue": "REVENUE"}})
    assert n2 == 0


def test_warehouse_type_findings_one_pass():
    from services.import_diagnostics import warehouse_type_findings
    doc = {"table": {"name": "t", "db": "d", "schema": "s", "db_table": "t", "columns": [
        {"db_column_name": "hcp_id", "db_column_properties": {"data_type": "VARCHAR"}},   # stale -> flag
        {"db_column_name": "amount", "db_column_properties": {"data_type": "DOUBLE"}},    # matches -> ok
        {"db_column_name": "opas",   "db_column_properties": {"data_type": "VARCHAR"}},   # void -> flag
        {"db_column_name": "extra",  "db_column_properties": {"data_type": "VARCHAR"}},   # not in wh -> skip
    ]}}
    type_map = {"t": {"hcp_id": "bigint", "amount": "double", "opas": "void"}}
    found = warehouse_type_findings([{"edoc": json.dumps(doc)}], type_map, connection="c")
    cols = sorted(f["column"] for f in found)
    assert cols == ["hcp_id", "opas"]          # amount matches (double==DOUBLE); extra not in warehouse
    assert all(f["kind"] == "type_mismatch" and f["source_type"] for f in found)


def test_isolation_pass_does_not_relabel_warnings_as_errors():
    # GSK 2026-09-16. Validating each file alone surfaces that file's WARNING too. The isolation
    # pass collected any non-OK result as a "failure" and then classified it with a hardcoded
    # status of ERROR, so two accepted realignments came back as blocking issues — counted twice,
    # once under "Accepted with a warning" and again under "Other validation errors".
    from services.import_diagnostics import is_blocking_result, classify_import_errors, blocking
    per_file = [{"name": "fact_subnational_respbio_br", "status": "WARNING",
                 "error": "DataType is being changed for column having name CALLS and "
                          "db_column_name CALLS."},
                {"name": "some_model", "status": "ERROR", "error": "Schema validation failed."}]
    # only the real error counts as a file failure ...
    assert [r["name"] for r in per_file if is_blocking_result(r)] == ["some_model"]
    # ... and carrying the real status through keeps the warning non-blocking
    assert blocking(classify_import_errors(
        [{"name": r["name"], "status": r.get("status") or "ERROR",
          "error": r["error"]} for r in per_file if is_blocking_result(r)])) != []
    assert blocking(classify_import_errors(per_file[:1])) == []


def test_invalid_column_property_names_the_column_and_the_property():
    # VERBATIM bytes from the GSK run of 2026-09-16T22:52:08 (debug bundle), not a reconstruction.
    # Note the shapes that broke an earlier parse: the column sits after a literal "- " rather than
    # inside <li>, the property is "* calendar" inside <li>, and SOLUTION is bolded.
    from services.import_diagnostics import classify_import_errors
    msg = ("Model/Worksheet columns have invalid properties.<br/>- <b>"
           "fact_subnational_day_bridge_respbio_br::DAY_DATE_FIELD</b><br/><ul><li>* calendar</li>"
           "</ul><br/><b>SOLUTION:</b><br/>Use one of the valid properties.<br/>")
    found = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": msg}])
    assert [f["kind"] for f in found] == ["invalid_column_property"]
    assert found[0]["columns"] == ["fact_subnational_day_bridge_respbio_br::DAY_DATE_FIELD"]
    assert found[0]["properties"] == ["calendar"]


def test_dropping_a_column_property_keeps_the_column():
    # The remedy for the above: the property goes, the column and its identity stay. Dropping the
    # COLUMN here would lose data for what is only a schema-vocabulary difference between clusters.
    from services.import_diagnostics import drop_column_properties
    doc = {"model": {"name": "m", "columns": [
        {"name": "Day Date", "column_id": "fact_day::DAY_DATE_FIELD", "calendar": "fiscal_2026"},
        {"name": "Other", "column_id": "fact_day::OTHER", "calendar": "fiscal_2026"}]}}
    items = [{"info": {"name": "m"}, "edoc": json.dumps(doc)}]
    out, removed = drop_column_properties(items, {"fact_day::DAY_DATE_FIELD": ["calendar"]})
    cols = json.loads(out[0]["edoc"])["model"]["columns"]
    assert removed == [("fact_day::DAY_DATE_FIELD", "calendar")]
    assert "calendar" not in cols[0] and cols[0]["column_id"] == "fact_day::DAY_DATE_FIELD"
    assert cols[0]["name"] == "Day Date"                 # column intact
    assert cols[1]["calendar"] == "fiscal_2026"          # scoped: the other column is untouched
    # nested form, and idempotence
    doc2 = {"model": {"name": "m", "columns": [
        {"column_id": "t::C", "properties": {"calendar": "x", "keep": 1}}]}}
    out2, rm2 = drop_column_properties([{"info": {"name": "m"}, "edoc": json.dumps(doc2)}],
                                       {"t::C": ["calendar"]})
    props = json.loads(out2[0]["edoc"])["model"]["columns"][0]["properties"]
    assert props == {"keep": 1} and rm2 == [("t::C", "calendar")]
    assert drop_column_properties(out2, {"t::C": ["calendar"]})[1] == []


def test_nested_list_markup_does_not_run_onto_one_line():
    # <li> always starts a line. Otherwise a nested item joins its parent
    # ("...DAY_DATE_FIELD**- calendar"), which parses wrong and reads wrong on screen.
    from services.import_diagnostics import _clean
    out = _clean("<ul><li><b>parent</b><ul><li>child</li></ul></li></ul>")
    lines = [l.strip() for l in out.split("\n") if l.strip()]
    assert lines == ["- **parent**", "- child"]


def test_warning_status_is_not_an_issue_to_resolve():
    # GSK 2026-09-15. Realigning CALLS and primary_target made ThoughtSpot return status WARNING
    # with "DataType is being changed ..." — the platform CONFIRMING the realign it applied. Every
    # non-OK status counted as an error, so a promotion with nothing wrong reported "2 issue(s) to
    # resolve before import" and the probe never converged.
    from services.import_diagnostics import classify_import_errors, blocking, warnings_only
    res = [{"name": "fact_subnational_respbio_br", "type": "LOGICAL_TABLE", "status": "WARNING",
            "error": "DataType is being changed for column having name CALLS and "
                     "db_column_name CALLS."},
           {"name": "copd_targets_to_trelegy", "type": "LOGICAL_TABLE", "status": "WARNING",
            "error": "DataType is being changed for column having name primary_target and "
                     "db_column_name primary_target. This change may break the dependents "
                     "models/worksheets, answers, etc of this column."},
           {"name": "ok_table", "status": "OK", "error": ""}]
    found = classify_import_errors(res)
    assert blocking(found) == [], "a WARNING must never be an issue to resolve"
    warns = warnings_only(found)
    assert [f["kind"] for f in warns] == ["type_changed_notice"] * 2
    assert sorted(f["column"] for f in warns) == ["CALLS", "primary_target"]
    # and the table is named, so it is never an "unknown"
    assert all(f["object"] and f["object"] != "unknown" for f in warns)



def _unused_authorization_failure_names_the_object_not_the_connection():
    # The hint used to send the operator to check the CONNECTION, which is not what the payload
    # says. It names a pre-existing target LOGICAL_TABLE, and checking connection sharing finds
    # nothing wrong — which is how this error survived two GSK sessions.
    from services.import_diagnostics import friendly_error
    msg = ('Unable to save worksheet. Error Code: AUTHORIZATION_FAILURE Incident Id: 8f74b135 '
           'Error Message: No permission to update objects: {   "LOGICAL_TABLE": [     '
           '"7c4f181a-1b69-4de2-a73b-766aa0ff9a11"   ] } or to read child securable objects')
    headline, action, _raw = friendly_error(msg)
    assert "7c4f181a-1b69-4de2-a73b-766aa0ff9a11" in headline
    assert "LOGICAL_TABLE" in headline
    assert "not the connection" in action.lower()


def test_pruning_a_table_takes_its_formula_column_with_it():
    # Found by the property suite (test_tml_properties.py), not in the field. drop_tables removed a
    # formula that referenced a pruned table, but left the model column that surfaced it, because it
    # only recognised a `formula_id` binding — real TML also binds via column_id `formula_<name>`,
    # or by name alone. The leftover column then dangles, which ThoughtSpot reports as "Unable to
    # create model column(s) ... incorrect" or as an opaque schema validation failure.
    from services.import_diagnostics import prune_tables_whole, dangling_reference_findings
    tbl = {"table": {"name": "t_gone", "columns": [
        {"name": "Amount", "db_column_name": "AMOUNT",
         "db_column_properties": {"data_type": "DOUBLE"}}]}}
    for binding in ({"column_id": "formula_Total"}, {"name": "Total"}, {"formula_id": "formula_total"}):
        model = {"model": {"name": "m", "model_tables": [{"name": "t_gone"}],
                           "formulas": [{"name": "Total", "expr": "sum([t_gone::AMOUNT])"}],
                           "columns": [{"name": "Total", **binding}]}}
        items = [{"info": {"name": "t_gone"}, "edoc": json.dumps(tbl)},
                 {"info": {"name": "m"}, "edoc": json.dumps(model)}]
        out, _s = prune_tables_whole(items, {"t_gone"})
        left = json.loads(out[0]["edoc"])["model"]
        assert left["formulas"] == [], f"formula survived for binding {binding}"
        assert left["columns"] == [], f"orphaned formula column survived for binding {binding}"
        assert dangling_reference_findings(out) == []


def test_model_column_unresolved_is_named_not_unknown():
    # GSK 2026-09-11, the second error on screen. ThoughtSpot sends no object name for it, so it
    # rendered as "unknown" under Other validation errors with nothing to act on — even though the
    # body names the culprit exactly. It is the downstream half of the PATIENT_AGE type failure.
    from services.import_diagnostics import classify_import_errors
    msg = ("Unable to create model column(s). These column_id/formula_id values are incorrect:"
           "<br/>fact_subnational_patient_bridge_respbio_br::PATIENT_AGE")
    found = classify_import_errors([{"name": "unknown", "type": None,
                                     "status": "ERROR", "error": msg}])
    assert [f["kind"] for f in found] == ["model_column_unresolved"]
    assert found[0]["object"] == "fact_subnational_patient_bridge_respbio_br"
    assert found[0]["column"] == "PATIENT_AGE"
    # several columns in one message are each named
    many = ("Unable to create model column(s). These column_id/formula_id values are incorrect:"
            "<br/>t_one::COL_A<br/>t_two::COL_B")
    got = classify_import_errors([{"name": "m", "status": "ERROR", "error": many}])
    assert sorted((f["object"], f["column"]) for f in got) == [("t_one", "COL_A"),
                                                              ("t_two", "COL_B")]


def test_within_family_type_drift_is_flagged_not_swallowed():
    # GSK 2026-09-11 regression. PATIENT_AGE was DOUBLE in the TML and an integer in Databricks.
    # Both are the "num" family, so the old family-level rule stayed silent — the drift table
    # rendered EMPTY while ThoughtSpot hard-failed ("DataType DOUBLE does not match CDW DataType"),
    # taking the table down and cascading into the model that used the column.
    from services.import_diagnostics import warehouse_type_findings
    doc = {"table": {"name": "fact_subnational_patient_bridge_respbio_br",
                     "db": "hive_metastore", "schema": "us_speciality_analytics",
                     "db_table": "fact_subnational_patient_bridge_respbio_br", "columns": [
        {"db_column_name": "PATIENT_AGE", "db_column_properties": {"data_type": "DOUBLE"}}]}}
    # bigint is what Databricks DESCRIBE returns; INT64 is what the connection/search COLUMN path
    # and the modeled fallback return for the same column. Both must flag against DOUBLE.
    for _cdw in ("int", "bigint", "smallint", "INT64"):
        found = warehouse_type_findings(
            [{"edoc": json.dumps(doc)}],
            {"fact_subnational_patient_bridge_respbio_br": {"patient_age": _cdw}})
        assert [f["column"] for f in found] == ["PATIENT_AGE"], f"{_cdw} vs DOUBLE must flag"
        assert found[0]["source_type"] == "DOUBLE"


def test_scan_names_cover_every_spelling_of_a_dropped_column():
    # The drop set is keyed table::PHYSICAL_COL, an answer refers to the DISPLAY name, and the
    # cascade log writes physical columns dotted. Missing a spelling makes the dependency scan
    # report nothing, which reads as "safe to drop" — the most dangerous way for this to fail.
    from services.import_diagnostics import scan_names_for_drops, dependents_using_columns
    names = scan_names_for_drops(
        {"fact_subnational_respbio_br::CALLS"},
        {"Calls", "fact_subnational_respbio_br.CALLS", "Total Calls Formula"})
    assert "calls" in names                                   # physical, unqualified
    assert "fact_subnational_respbio_br::calls" in names      # as scoped in the drop set
    assert "total calls formula" in names                     # a cascaded formula name
    # and those names actually catch an answer that refers to the column by display name
    ans = {"id": "a1", "name": "Calls by Rep", "type": "ANSWER",
           "tml": json.dumps({"answer": {"answer_columns": [{"name": "Calls"}]}})}
    assert [f["id"] for f in dependents_using_columns([ans], names)] == ["a1"]
    assert scan_names_for_drops(set(), set()) == set()


def test_dependents_are_narrowed_to_the_dropped_column():
    # list_dependents answers "what depends on this TABLE", which implicates every answer built on
    # a 45-column table when one column goes. Dropping a column must only implicate the dependents
    # that actually reference THAT column, or the operator is handed a list they cannot act on.
    from services.import_diagnostics import dependents_using_columns
    uses = {"id": "a1", "name": "Calls by Rep", "type": "ANSWER",
            "tml": json.dumps({"answer": {"name": "Calls by Rep", "answer_columns": [
                {"name": "Calls"}, {"name": "Rep"}]}})}
    other = {"id": "a2", "name": "Patients by Brand", "type": "ANSWER",
             "tml": json.dumps({"answer": {"answer_columns": [{"name": "Patients"}]}})}
    formula = {"id": "a3", "name": "Derived", "type": "ANSWER",
               "tml": json.dumps({"answer": {"formulas": [
                   {"name": "f", "expr": "sum([fact_x::CALLS]) / 2"}]}})}
    unreadable = {"id": "a4", "name": "Broken export", "type": "LIVEBOARD", "tml": None}
    found = dependents_using_columns([uses, other, formula, unreadable], {"Calls", "CALLS"})
    by_id = {f["id"]: f for f in found}
    assert set(by_id) == {"a1", "a3", "a4"}, "only the dependents that use the column, plus unknowns"
    assert by_id["a1"]["columns"] == ["calls"] and by_id["a1"]["certain"]
    assert by_id["a3"]["certain"], "a formula reference counts, qualified or not"
    assert by_id["a4"]["certain"] is False, "an unreadable dependent is surfaced, not dropped"
    assert "a2" not in by_id, "a dependent of the same table that uses other columns is not implicated"
    # no drop set => nothing is implicated at all
    assert dependents_using_columns([uses], set()) == []


def test_promotion_plan_distinguishes_the_four_cases():
    # The Select page's promotion set moved from a stack of checkboxes to one table. The four
    # outcomes of leaving something out differ in what they DESTROY, so they are tested here
    # rather than left tangled up with widget state.
    from services.import_diagnostics import promotion_plan
    id2name = {"m_on": "ModelOnTarget", "m_off": "ModelMissing",
               "t_on": "TableOnTarget", "t_off": "TableMissing"}
    present = {"ModelOnTarget", "TableOnTarget"}
    models, tables = ["m_on", "m_off"], ["t_on", "t_off"]

    # everything ticked: nothing excluded, nothing destroyed
    p = promotion_plan(models, tables, id2name, present, set(id2name))
    assert p == {"excluded": set(), "unsafe": [], "safe_skips": [], "prune": []}

    # nothing ticked: each case lands in its own bucket
    p = promotion_plan(models, tables, id2name, present, set())
    assert p["excluded"] == {"m_on", "m_off", "t_on", "t_off"}
    assert p["unsafe"] == ["ModelMissing"]        # a model not on target CANNOT be left out
    assert p["safe_skips"] == ["TableOnTarget"]   # binds to the target's copy, nothing dropped
    assert p["prune"] == ["TableMissing"]         # must be pruned from the model (destructive)

    # a model that IS on the target may be left out safely — it is not "unsafe"
    p = promotion_plan(models, tables, id2name, present, {"m_off", "t_on", "t_off"})
    assert p["unsafe"] == [] and p["excluded"] == {"m_on"}
    # and a ticked table is never queued for pruning even when missing from the target
    p = promotion_plan(models, tables, id2name, present, {"m_on", "m_off", "t_off"})
    assert p["prune"] == [] and p["safe_skips"] == ["TableOnTarget"]


def test_bundle_self_heals_a_type_it_should_not_have_changed():
    # 2026-09-17: CALLS was STILL being changed on 2c after the apply-step fix, because that fix
    # only runs during a fresh export. The bundle in memory already carried the rewritten type and
    # the validation page never re-exports, so nothing undid it. Repairing against the raw source
    # TML fixes the items in place, which is the only thing that helps mid-session.
    from services.import_diagnostics import restore_unneeded_type_changes
    def _tbl(calls, hcp):
        return {"edoc": json.dumps({"table": {"name": "fact_x", "columns": [
            {"name": "Calls", "db_column_name": "CALLS",
             "db_column_properties": {"data_type": calls}},
            {"name": "Hcp Id", "db_column_name": "HCP_ID",
             "db_column_properties": {"data_type": hcp}}]}})}
    source = [_tbl("INT64", "VARCHAR")]
    bundle = [_tbl("INT32", "INT64")]        # both were realigned under the old rule
    out, restored = restore_unneeded_type_changes(bundle, source)
    cols = {c["db_column_name"]: c["db_column_properties"]["data_type"]
            for c in json.loads(out[0]["edoc"])["table"]["columns"]}
    assert cols["CALLS"] == "INT64", "width-only rewrite must be reverted to the source type"
    assert cols["HCP_ID"] == "INT64", "a cross-class realignment must survive"
    assert restored == [("fact_x", "CALLS", "INT32", "INT64")]
    # idempotent, and a clean bundle is left completely alone
    assert restore_unneeded_type_changes(out, source)[1] == []
    assert restore_unneeded_type_changes(source, source)[1] == []


def test_a_realignment_that_changes_no_class_is_never_applied():
    # Anuj, 2026-09-17: "CALLS should not even appear in the list, this is a clear regression."
    # Not flagging it any more is not enough: an approval is DURABLE and is re-applied to every
    # export, so one collected under the older, stricter rule keeps rewriting INT64 to INT32
    # forever and keeps earning "DataType is being changed". The apply step itself must refuse a
    # realignment that changes no storage class, so the pipeline self-corrects with no one having
    # to remember to clear anything.
    from services.import_diagnostics import realign_column_types, prune_stale_realignments
    doc = {"table": {"name": "fact_x", "columns": [
        {"name": "Calls", "db_column_name": "CALLS",
         "db_column_properties": {"data_type": "INT64"}},
        {"name": "Hcp Id", "db_column_name": "HCP_ID",
         "db_column_properties": {"data_type": "VARCHAR"}}]}}
    items = [{"info": {"name": "fact_x"}, "edoc": json.dumps(doc)}]
    stale_and_real = {"fact_x::CALLS": "INT32",     # integer width only — must be ignored
                      "fact_x::HCP_ID": "INT64"}    # str -> int — must still apply
    out, n = realign_column_types(items, stale_and_real)
    cols = {c["db_column_name"]: c["db_column_properties"]["data_type"]
            for c in json.loads(out[0]["edoc"])["table"]["columns"]}
    assert cols["CALLS"] == "INT64", "a width-only realignment must not rewrite the TML"
    assert cols["HCP_ID"] == "INT64", "a real class change must still be applied"
    assert n == 1
    # and the durable map prunes itself, so the UI cannot claim a realignment that isn't happening
    kept, dropped = prune_stale_realignments(items, stale_and_real)
    assert kept == {"fact_x::HCP_ID": "INT64"}
    assert dropped == {"fact_x::CALLS": "INT32"}
    # float <-> int is a class change in both directions and survives pruning
    doc2 = {"table": {"name": "t", "columns": [
        {"db_column_name": "AGE", "db_column_properties": {"data_type": "DOUBLE"}}]}}
    k2, d2 = prune_stale_realignments([{"info": {"name": "t"}, "edoc": json.dumps(doc2)}],
                                      {"t::AGE": "INT64"})
    assert k2 == {"t::AGE": "INT64"} and d2 == {}


def test_integer_width_is_never_drift():
    # Anuj, 2026-09-16: "why are CALLS and all other columns with INT to INT32 and INT64 being
    # changed? I don't think that this change is required." He is right, and the corpus proves it:
    # across ~2 months of real runs every hard 14536 was BOOL vs non-bool, VARCHAR vs number, or
    # DOUBLE vs INT64. Not one was an integer WIDTH difference. Flagging those forced a realign the
    # platform never asked for, rewrote the customer's TML, and earned a "may break the dependents"
    # warning for nothing.
    from services.import_diagnostics import warehouse_type_findings, type_class

    def _doc(col, tml):
        return {"table": {"name": "t", "columns": [
            {"db_column_name": col, "db_column_properties": {"data_type": tml}}]}}

    def _flags(col, tml, cdw):
        return bool(warehouse_type_findings([{"edoc": json.dumps(_doc(col, tml))}],
                                            {"t": {col.lower(): cdw}}))

    # integer width, in every spelling: never drift
    for tml, cdw in (("INT64", "int"), ("INT32", "bigint"), ("INT64", "INT32"),
                     ("INT32", "smallint"), ("INT64", "bigint")):
        assert not _flags("CALLS", tml, cdw), f"{tml} vs {cdw} must not be reported as drift"
    # floating point width is likewise not drift
    assert not _flags("amt", "DOUBLE", "decimal(10,2)")
    assert not _flags("amt", "DOUBLE", "float")
    # but every pair that ACTUALLY hard-failed at GSK still flags
    assert _flags("PATIENT_AGE", "DOUBLE", "bigint"), "float vs int hard-fails (PATIENT_AGE)"
    assert _flags("PATIENT_AGE", "DOUBLE", "INT64")
    assert _flags("HCP_ID", "VARCHAR", "bigint"), "str vs int hard-fails (HCP_ID)"
    assert _flags("flag_x", "BOOL", "string"), "bool vs str hard-fails"
    assert _flags("d", "DATE_TIME", "bigint")
    assert type_class("bigint") == type_class("INT32") == "int"
    assert type_class("DOUBLE") == type_class("decimal(18,4)") == "float"


def test_int_vs_int32_stays_quiet():
    # The false positive the family rule was introduced to kill must STAY dead: Databricks `int`
    # and TML INT32 are the same type spelled two ways, so token-compare reports nothing.
    from services.import_diagnostics import warehouse_type_findings
    doc = {"table": {"name": "t", "columns": [
        {"db_column_name": "a", "db_column_properties": {"data_type": "INT32"}},   # int   -> INT32
        {"db_column_name": "b", "db_column_properties": {"data_type": "INT64"}},   # bigint-> INT64
        {"db_column_name": "c", "db_column_properties": {"data_type": "VARCHAR"}}, # string-> VARCHAR
        {"db_column_name": "d", "db_column_properties": {"data_type": "DOUBLE"}},  # decimal->DOUBLE
    ]}}
    type_map = {"t": {"a": "int", "b": "bigint", "c": "string", "d": "decimal(10,2)"}}
    assert warehouse_type_findings([{"edoc": json.dumps(doc)}], type_map) == []


def test_unmappable_warehouse_type_is_not_flagged():
    # We only report drift we can name. A warehouse type with no TS token (struct/array/map) is
    # skipped rather than guessed at — guessing here would drop a column on a bad reading.
    from services.import_diagnostics import warehouse_type_findings
    doc = {"table": {"name": "t", "columns": [
        {"db_column_name": "payload", "db_column_properties": {"data_type": "VARCHAR"}}]}}
    assert warehouse_type_findings([{"edoc": json.dumps(doc)}],
                                   {"t": {"payload": "struct<a:int>"}}) == []


def test_warehouse_type_findings_skips_unread_table():
    from services.import_diagnostics import warehouse_type_findings
    doc = {"table": {"name": "t", "columns": [
        {"db_column_name": "x", "db_column_properties": {"data_type": "VARCHAR"}}]}}
    assert warehouse_type_findings([{"edoc": json.dumps(doc)}], {}) == []   # table not in map -> nothing


def test_warehouse_type_to_ts_tokens():
    from services.import_diagnostics import warehouse_type_to_ts
    assert warehouse_type_to_ts("bigint") == "INT64"      # the HCP_ID case — NOT literal 'bigint'
    assert warehouse_type_to_ts("int") == "INT32"         # 32-bit int -> INT32, NOT INT64
    assert warehouse_type_to_ts("integer") == "INT32"
    assert warehouse_type_to_ts("BIGINT") == "INT64"      # case-insensitive
    assert warehouse_type_to_ts("string") == "VARCHAR"
    assert warehouse_type_to_ts("boolean") == "BOOL"
    assert warehouse_type_to_ts("double") == "DOUBLE"
    # TS tokens round-trip, because some type sources report tokens rather than warehouse strings
    for _tok in ("INT32", "INT64", "DOUBLE", "FLOAT", "VARCHAR", "BOOL", "DATE", "DATE_TIME"):
        assert warehouse_type_to_ts(_tok) == _tok
    assert warehouse_type_to_ts("struct<a:int>") == ""     # unmappable stays unmappable
    assert warehouse_type_to_ts("decimal(10,2)") == "DOUBLE"
    assert warehouse_type_to_ts("timestamp") == "DATE_TIME"
    assert warehouse_type_to_ts("void") == ""             # not a real type -> no realign
    assert warehouse_type_to_ts("") == ""


def test_type_family_and_within_family_not_flagged():
    from services.import_diagnostics import type_family, warehouse_type_findings
    # int / INT32 / bigint are all the same family -> within-family, must NOT be a mismatch
    assert type_family("int") == type_family("INT32") == type_family("bigint") == "num"
    assert type_family("varchar(255)") == "str" and type_family("decimal(10,2)") == "num"
    assert type_family("void") == "void" and type_family("weird") == ""
    # warehouse INT vs TML INT32 (both number) -> NOT flagged (the false-positive we fixed);
    # warehouse bigint vs TML VARCHAR (number vs string) -> flagged (real, HCP_ID case).
    doc = {"table": {"name": "t", "db": "d", "schema": "s", "db_table": "t", "columns": [
        {"db_column_name": "call_date", "db_column_properties": {"data_type": "INT32"}},
        {"db_column_name": "hcp_id",    "db_column_properties": {"data_type": "VARCHAR"}}]}}
    found = warehouse_type_findings([{"edoc": json.dumps(doc)}], {"t": {"call_date": "int", "hcp_id": "bigint"}})
    assert [f["column"] for f in found] == ["hcp_id"]


def test_realign_ignores_bare_and_empty():
    from services.import_diagnostics import realign_column_types
    doc = {"edoc": json.dumps({"table": {"name": "t", "columns": [
        {"name": "c", "db_column_name": "c", "db_column_properties": {"data_type": "VARCHAR"}}]}})}
    # bare key (no ::) and empty type are ignored — never a global retype
    out, n = realign_column_types([doc], {"c": "BIGINT", "t::c": ""})
    assert n == 0
    assert json.loads(out[0]["edoc"])["table"]["columns"][0]["db_column_properties"]["data_type"] == "VARCHAR"


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



def test_clean_handles_the_malformed_closing_break():
    # ThoughtSpot emits "</br>" in this payload. Left alone it survives into the rendered text.
    from services.import_diagnostics import _clean
    assert "</br>" not in _clean("a</br>b")


def test_the_model_being_promoted_is_not_a_deletion_candidate():
    # Anuj, 2026-09-23: the dependency panel listed "Sales Customers Model" — the very model being
    # promoted — as a deletable dependent. It depends on its own table, so the scan finds it. But
    # the import REWRITES it; deleting it would remove the object being updated and take its own
    # answers and liveboards with it, most of which never touched the dropped column.
    from services.import_diagnostics import dependents_using_columns
    model = {"id": "m1", "name": "Sales Customers Model", "type": "LOGICAL_TABLE",
             "tml": json.dumps({"model": {"name": "Sales Customers Model",
                                          "columns": [{"name": "customerid"}]}})}
    answer = {"id": "a1", "name": "Sales Model Customer Id Answer", "type": "ANSWER",
              "tml": json.dumps({"answer": {"answer_columns": [{"name": "customerid"}]}})}
    hits = dependents_using_columns([model, answer], {"customerid"})
    assert {h["id"] for h in hits} == {"m1", "a1"}, "the scan finds both — that part is right"

    # the app then marks anything in the promotion, and only the rest may be deleted
    promoted_names = {"sales customers model"}
    for h in hits:
        h["in_promotion"] = h["name"].strip().lower() in promoted_names
    deletable = [h for h in hits if not h["in_promotion"]]
    assert [h["id"] for h in deletable] == ["a1"], \
        "the promoted model must never be offered for deletion"
    assert next(h for h in hits if h["id"] == "m1")["in_promotion"] is True


def test_there_is_no_hint_layer_and_the_raw_text_always_survives():
    # The paraphrasing layer was deleted on 2026-09-23. It restated what classify_import_errors
    # already extracts, and ThoughtSpot ships its own SOLUTION: line written by the people who
    # emit the error. Ours was wrong twice in a week, so the tuple now always yields the platform's
    # words and nothing of our own invention.
    from services import import_diagnostics as I
    assert not hasattr(I, "_ERROR_RULES"), "the rule table must stay deleted"
    assert not hasattr(I, "_RULE_EVIDENCE")
    assert not hasattr(I, "friendly_error_with_evidence")
    for msg in ("Deleted columns have dependents.<br/>- <b>customerID</b></br>"
                "<ul><li>Test dev</li></ul>",
                "Error code 10086: not authorized",
                "totally novel error nobody has seen"):
        h, a, raw = I.friendly_error(msg)
        assert h is None and a is None, "we no longer speak for the platform"
        assert raw and "<br/>" not in raw, "raw text is returned, tidied but complete"
    # and nothing is lost in the tidying
    long = "Error: " + ("x" * 4000) + " <br/>SOLUTION: do the thing."
    assert len(I.friendly_error(long)[2]) > 3900, "never truncated"
    assert "SOLUTION: do the thing." in I.friendly_error(long)[2]


def test_a_blocking_liveboard_names_the_tiles_that_use_the_column():
    # "Test dev is blocked" is not actionable. Naming the visualisation, and how many tiles the
    # board has in total, tells the operator whether losing the board is proportionate — and lets
    # them remove just that tile in the product instead.
    from services.import_diagnostics import dependents_using_columns
    lb = {"id": "lb1", "name": "Test dev", "type": "PINBOARD_ANSWER_BOOK",
          "tml": json.dumps({"liveboard": {"name": "Test dev", "visualizations": [
              {"id": "Viz_1", "answer": {"name": "Customers by id",
                                         "answer_columns": [{"name": "customerid"}]}},
              {"id": "Viz_2", "answer": {"name": "Orders by city",
                                         "answer_columns": [{"name": "city"}]}}]}})}
    answer = {"id": "a1", "name": "An answer", "type": "QUESTION_ANSWER_BOOK",
              "tml": json.dumps({"answer": {"answer_columns": [{"name": "customerid"}]}})}
    by_id = {r["id"]: r for r in dependents_using_columns([lb, answer], {"customerid"})}
    assert by_id["lb1"]["vizzes"] == [{"id": "Viz_1", "name": "Customers by id"}]
    assert by_id["lb1"]["viz_total"] == 2, "say how much of the board is NOT affected"
    # an answer is a single visualisation, so it has no tile breakdown
    assert by_id["a1"]["vizzes"] == [] and by_id["a1"]["viz_total"] == 0
    # a dependent whose TML could not be read still reports, with empty tiles
    unreadable = dependents_using_columns(
        [{"id": "x", "name": "?", "type": "ANSWER", "tml": None}], {"customerid"})
    assert unreadable[0]["vizzes"] == [] and unreadable[0]["certain"] is False
