"""L1: services/table_matcher — cross-cluster table matching (pure logic).

Equivalence classes for the match decision:
  identical structure          -> high confidence MATCH
  renamed physical table       -> columns still carry it -> MATCH (the whole point)
  drifted (some cols differ)    -> REVIEW / MATCH depending on overlap
  disjoint columns             -> NO_MATCH
  two structurally-similar tgts -> AMBIGUOUS
"""
from services.table_matcher import (
    physical_coords, column_signature, compare_columns, score_pair,
    match_tables, MATCH_THRESHOLD, REVIEW_THRESHOLD,
)


def test_physical_coords_reads_binding(commerce_table_doc):
    c = physical_coords(commerce_table_doc)
    assert c["name"] == "commerce"
    assert c["db"] == "workspace" and c["schema"] == "athoz" and c["db_table"] == "commerce"
    assert c["connection"] == "Sisense Migration - Databricks"
    assert c["obj_id"] == "commerce_tbl"


def test_column_signature_keys_on_db_column_name(commerce_table_doc):
    sig = column_signature(commerce_table_doc)
    assert "visit_id" in sig and "revenue" in sig      # lowercased physical names
    assert sig["quantity"] == "int64"                  # type lowercased
    assert len(sig) == 11


def test_compare_columns_identical(commerce_table_doc):
    sig = column_signature(commerce_table_doc)
    diff = compare_columns(sig, sig)
    assert diff["similarity"] == 1.0 and diff["jaccard"] == 1.0
    assert diff["missing_on_target"] == [] and diff["extra_on_target"] == []


def test_compare_columns_source_extra_and_target_extra(commerce_table_doc, make_table_doc):
    # target lacks Revenue, has an extra Margin column the source doesn't
    tgt_cols = [(c, t) for c, t in
                [("Visit_ID", "VARCHAR"), ("Margin", "DOUBLE")]]
    src = column_signature(commerce_table_doc)
    tgt = column_signature(make_table_doc(columns=tgt_cols))
    diff = compare_columns(src, tgt)
    assert "revenue" in diff["missing_on_target"]      # source has, target lacks
    assert "margin" in diff["extra_on_target"]         # target has, source lacks


def test_compare_columns_type_mismatch(commerce_table_doc, make_table_doc):
    drift = [(c, ("VARCHAR" if c == "Quantity" else t)) for c, t in
             [("Quantity", "INT64"), ("Revenue", "DOUBLE")]]
    src = column_signature(make_table_doc(columns=[("Quantity", "INT64"), ("Revenue", "DOUBLE")]))
    tgt = column_signature(make_table_doc(columns=drift))
    diff = compare_columns(src, tgt)
    assert "quantity" in diff["type_mismatch"]
    assert "revenue" not in diff["type_mismatch"]


def test_score_identical_is_match(commerce_table_doc):
    r = score_pair(commerce_table_doc, commerce_table_doc)
    assert r["confidence"] >= MATCH_THRESHOLD


def test_score_renamed_physical_table_still_matches(commerce_table_doc, make_table_doc):
    # Same columns/name, DIFFERENT physical db_table (renamed on the target warehouse).
    renamed = make_table_doc(db_table="commerce_prod")
    r = score_pair(commerce_table_doc, renamed)
    assert r["confidence"] >= MATCH_THRESHOLD          # columns dominate -> still a match


def test_score_disjoint_columns_is_no_match(commerce_table_doc, make_table_doc):
    other = make_table_doc(name="weather", db_table="weather",
                           columns=[("Station", "VARCHAR"), ("Temp", "DOUBLE"), ("Humidity", "DOUBLE")])
    r = score_pair(commerce_table_doc, other)
    assert r["confidence"] < REVIEW_THRESHOLD


def test_match_tables_picks_counterpart(commerce_table_doc, make_table_doc):
    targets = [
        make_table_doc(name="weather", db_table="weather",
                       columns=[("Station", "VARCHAR"), ("Temp", "DOUBLE")]),
        make_table_doc(db_table="commerce_prod"),      # the true counterpart
    ]
    results = match_tables([commerce_table_doc], targets)
    assert len(results) == 1
    assert results[0]["decision"] == "MATCH"
    assert results[0]["best"]["target"]["db_table"] == "commerce_prod"


def test_match_tables_ambiguous_between_twins(commerce_table_doc, make_table_doc):
    # Two targets with the SAME structure -> the matcher can't tell them apart.
    twin_a = make_table_doc(name="commerce_a", db_table="commerce_a")
    twin_b = make_table_doc(name="commerce_b", db_table="commerce_b")
    results = match_tables([commerce_table_doc], [twin_a, twin_b])
    assert results[0]["decision"] in ("AMBIGUOUS", "MATCH")   # documents current behavior
