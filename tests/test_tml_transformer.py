"""L1: services/tml_transformer — data-layer transform + file mapping (pure logic)."""
import json

from services.tml_transformer import (
    transform_doc, items_to_files, extract_model_refs, extract_table_refs,
)


def test_transform_remaps_connection_on_table(make_table_doc):
    doc = make_table_doc(connection="Sisense Migration - Databricks")
    out, warnings = transform_doc(
        doc, source_connection="Sisense Migration - Databricks",
        target_connection="Prod Databricks")
    assert out["table"]["connection"]["name"] == "Prod Databricks"


def test_transform_remaps_db_and_schema(make_table_doc):
    doc = make_table_doc(db="workspace", schema="athoz")
    out, _ = transform_doc(doc, db_map={"workspace": "prod_ws"},
                           schema_map={"athoz": "analytics"})
    assert out["table"]["db"] == "prod_ws"
    assert out["table"]["schema"] == "analytics"


def test_transform_recases_db_column_name_only(make_table_doc):
    # Warehouse casing differs; recase the physical db_column_name, leave logical name alone.
    doc = make_table_doc(columns=[("revenue", "DOUBLE")])
    out, _ = transform_doc(doc, column_case_map={"commerce": {"revenue": "REVENUE"}})
    col = out["table"]["columns"][0]
    assert col["db_column_name"] == "REVENUE"     # recased to warehouse
    assert col["name"] == "revenue"               # logical name untouched


def test_transform_no_recase_when_map_empty(make_table_doc):
    # Approve-first (Increment 3): with no approved recasings the map is empty, so the physical
    # db_column_name keeps its SOURCE casing — nothing is recased silently.
    doc = make_table_doc(columns=[("revenue", "DOUBLE")])
    src_case = doc["table"]["columns"][0]["db_column_name"]
    out, _ = transform_doc(doc, column_case_map={})
    assert out["table"]["columns"][0]["db_column_name"] == src_case


def test_transform_preserves_obj_id(commerce_table_doc):
    out, _ = transform_doc(commerce_table_doc)
    assert out["obj_id"] == "commerce_tbl"        # identity carried cross-cluster


def test_items_to_files_routes_by_type(model_item, liveboard_item, commerce_table_item):
    files = items_to_files([model_item, liveboard_item, commerce_table_item])
    paths = set(files)
    assert any(p.startswith("models/") and p.endswith(".model.tml") for p in paths)
    assert any(p.startswith("liveboards/") and p.endswith(".liveboard.tml") for p in paths)
    assert any(p.startswith("tables/") and p.endswith(".table.tml") for p in paths)
    # content round-trips as JSON
    for c in files.values():
        json.loads(c)


def test_extract_refs_from_real_liveboard(liveboard_doc):
    # The liveboard is built on the PS-ECommerce model.
    model_refs = extract_model_refs(liveboard_doc)
    assert "PS-ECommerce" in model_refs


def test_extract_table_refs_from_model(model_doc):
    refs = extract_table_refs(model_doc)
    assert {"commerce", "brand", "category", "country"} <= set(refs)
