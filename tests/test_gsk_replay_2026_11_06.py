"""Replay: GSK, 2026-11-06, Source Of Business. Real platform text and the real model shape.

The operator skipped fact_sob_weekly_monthly_sbu::DIAGNOSIS_CATEGORY, a key in two alias joins.
The drop removed both joins, the cut-off dim was pruned, and two things went wrong:
  * the prune left the alias-qualified model columns behind (14547 on the model), and
  * a 14544 naming two columns was read as naming only the first.
"""
import json

from services.import_diagnostics import (
    classify_import_errors, drop_columns, prune_tables_whole, table_cleanup_findings,
)

# Verbatim from GSK's logs/validate_raw.jsonl, 06:13:10.
TWO_COLUMN_14544 = (
    "Deleted columns have dependents.<br/>- <b>Diagnosis</b></br><ul><li>Source Of Business</li>"
    "</ul><br/>- <b>DIAGNOSIS_CATEGORY</b></br><ul><li>Source Of Business</li><li>fact_sob_weekly_"
    "monthly_sbu_to_dim_launch_date_alignment_sob_s6GEJGn</li><li>fact_sob_weekly_monthly_sbu_to_"
    "dim_launch_date_alignment_sob_WipoHWB</li></ul><br/><b>SOLUTION:</b><br/>Either replace the "
    "deleted columns, or remove the dependencies.")
TABLE_ONLY_14544 = (
    "Unable to import tml due to following errors:<br/>- <b>fact_sob_weekly_monthly_sbu</b>: "
    "Deleted columns have dependents.<br/><br/><b>SOLUTION:</b><br/>Either fix the error objects "
    "or remove those objects from import and then re-import.")


def test_every_column_in_one_14544_is_read():
    found = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": TWO_COLUMN_14544}])
    cols = {f["column"]: f["dependents"] for f in found
            if f["kind"] == "drop_blocked_by_dependents" and f.get("column")}
    assert set(cols) == {"Diagnosis", "DIAGNOSIS_CATEGORY"}
    assert len(cols["DIAGNOSIS_CATEGORY"]) == 3


def test_the_table_only_form_still_names_just_the_table():
    (f,) = classify_import_errors([{"name": "unknown", "status": "ERROR", "error": TABLE_ONLY_14544}])
    assert f["object"] == "fact_sob_weekly_monthly_sbu" and not f.get("column")


FACT, DIM = "fact_sob_weekly_monthly_sbu", "dim_launch_date_alignment_sob"
KEY = "[" + DIM + "::DIAGNOSIS] = [" + FACT + "::DIAGNOSIS_CATEGORY]"
MODEL = {"guid": "99515b8e", "obj_id": "sob_mdl", "model": {"name": "Source Of Business", "model_tables": [
    {"name": FACT, "obj_id": FACT + "_tbl"},
    {"name": "dim_sob_weekly_monthly_sbu", "alias": "dim_sob_weekly_monthly (weekly)",
     "obj_id": "dim_sob_weekly_monthly_sbu_tbl",
     "joins": [{"with": FACT, "on": "[dim_sob_weekly_monthly_sbu::Date_ID] = [" + FACT + "::Week_ID]",
                "type": "OUTER", "cardinality": "ONE_TO_MANY"}]},
    {"name": DIM, "alias": DIM + " (Monthly)", "obj_id": DIM + "_tbl",
     "joins": [{"with": FACT, "on": "[" + DIM + "::445_MONTH_ID] = [" + FACT + "::Month_ID] AND " + KEY,
                "type": "INNER", "cardinality": "ONE_TO_MANY"}]},
    {"name": DIM, "alias": DIM + " (weekly)", "obj_id": DIM + "_tbl",
     "joins": [{"with": FACT, "on": "[" + DIM + "::Week_ID] = [" + FACT + "::Week_ID] AND " + KEY,
                "type": "INNER", "cardinality": "ONE_TO_MANY"}]},
], "formulas": [
    {"id": "formula_Launch Gap", "name": "Launch Gap",
     "expr": "[" + DIM + " (weekly)::LAUNCH_WEEK_NUMBER] - 1"},
], "columns": [
    {"name": "Diagnosis Category", "column_id": FACT + "::DIAGNOSIS_CATEGORY",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Total Rx Weekly", "column_id": FACT + "::Total_Rx_Weekly",
     "properties": {"column_type": "MEASURE"}},
    {"name": "Week", "column_id": "dim_sob_weekly_monthly (weekly)::Date_ID",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Launch Month Number", "column_id": DIM + " (Monthly)::LAUNCH_MONTH_NUMBER",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Launch Week Number", "column_id": DIM + " (weekly)::LAUNCH_WEEK_NUMBER",
     "properties": {"column_type": "ATTRIBUTE"}},
    {"name": "Launch Gap", "formula_id": "formula_Launch Gap",
     "properties": {"column_type": "MEASURE"}},
]}}


def test_pruning_a_cut_off_aliased_table_leaves_nothing_pointing_at_it():
    items = [{"edoc": json.dumps(MODEL)}]
    fixed, _ = drop_columns(items, {FACT + "::diagnosis_category"})
    cut = {f["table"] for f in table_cleanup_findings(fixed)}
    assert cut == {DIM}
    pruned, _ = prune_tables_whole(fixed, cut)
    model = json.loads(pruned[0]["edoc"])["model"]
    left = json.dumps(model)
    assert DIM not in left, left            # no entry, join, column or formula refers to it
    assert {c["name"] for c in model["columns"]} == {"Total Rx Weekly", "Week"}
