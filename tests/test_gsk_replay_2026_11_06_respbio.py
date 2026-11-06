"""Replay: GSK, 2026-11-06 07:32, RespBio (30 files). Real platform text, real table and columns.

Validation took five passes where September runs of the same promotion took one, because the
columns the target warehouse lacks were not dropped first and ThoughtSpot reports only one missing
column per table per validation (fact_subnational_patient_features_respbio_br lacks four). The
issue count also included ThoughtSpot's table-level echoes of the blocked columns.
"""
import json

from services.import_diagnostics import (
    classify_import_errors, distinct_causes, set_aside_warehouse_missing,
)

# Verbatim from GSK's logs/validate_raw.jsonl, 07:34:00.
TABLE_ECHO = (
    "Unable to import tml due to following errors:<br/>- <b>fact_subnational_patient_bridge_"
    "respbio_br</b>: Deleted columns have dependents.<br/><br/><b>SOLUTION:</b><br/>Either fix the "
    "error objects or remove those objects from import and then re-import.<br/>")
COLUMN_BLOCKED = (
    "Deleted columns have dependents.<br/>- <b>PATIENT_AGE</b></br><ul><li>OE test - Respbio "
    "Subnational Performance </li></ul><br/><b>SOLUTION:</b><br/>Either replace the deleted "
    "columns, or remove the dependencies.<br/>")


def test_a_table_level_echo_is_not_counted_on_top_of_the_named_column():
    found = classify_import_errors([
        {"name": "unknown", "status": "ERROR", "error": TABLE_ECHO},
        {"name": "unknown", "status": "ERROR", "error": COLUMN_BLOCKED}])
    assert len(found) == 2
    (cause,) = distinct_causes(found)
    assert cause.get("columns") == ["PATIENT_AGE"]


def test_a_table_level_notice_alone_still_counts():
    assert len(distinct_causes(classify_import_errors(
        [{"name": "unknown", "status": "ERROR", "error": TABLE_ECHO}]))) == 1


FEATURES = "fact_subnational_patient_features_respbio_br"
MISSING = ["SUA_PATIENTS", "COPD_PATIENTS", "ASTHMA_COPD_COMORBID", "SUA_BIO_ELIGIBLE"]


def _table(cols):
    return {"edoc": json.dumps({"table": {"name": FEATURES, "db": "gsk", "schema": "respbio",
            "db_table": FEATURES, "columns": [{"name": c, "db_column_name": c} for c in cols]}})}


def test_every_column_the_warehouse_lacks_is_set_aside_before_the_first_validation():
    items = [_table(["HCP_ID"] + MISSING)]
    out, found = set_aside_warehouse_missing(items, {FEATURES: {"hcp_id": "HCP_ID"}})
    assert sorted(f["column"] for f in found) == sorted(MISSING), "all four in one go"
    cols = [c["name"] for c in json.loads(out[0]["edoc"])["table"]["columns"]]
    assert cols == ["HCP_ID"]


def test_nothing_is_set_aside_on_a_guess():
    items = [_table(["HCP_ID"] + MISSING)]
    out, found = set_aside_warehouse_missing(items, {})   # warehouse not read
    assert found == [] and out == items
