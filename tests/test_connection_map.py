"""L1: services/connection_map + the transformer's per-connection remap (pure logic)."""
import json

from services.connection_map import (
    connections_in, saved_map, effective_map, unmapped, target_for,
)
from services.tml_transformer import transform_doc, transform_items


def _items(make_table_doc, make_item, *conns):
    return [make_item(make_table_doc(name=f"t{i}", connection=c, obj_id=f"t{i}"), f"t{i}")
            for i, c in enumerate(conns)]


def test_connections_in_lists_every_table_connection(make_table_doc, make_item):
    items = _items(make_table_doc, make_item, "DB Dev", "SF Dev", "DB Dev")
    assert connections_in(items) == ["DB Dev", "SF Dev"]


def test_connections_in_reads_model_table_entries_and_skips_variables():
    model = {"model": {"name": "m", "model_tables": [
        {"name": "a", "connection": {"name": "DB Dev"}},
        {"name": "b", "connection": {"name": "${conn}"}},
        {"name": "c"}]}}
    assert connections_in([{"edoc": json.dumps(model)}]) == ["DB Dev"]


def test_saved_map_prefers_the_mapping():
    cfg = {"connection_map": {"DB Dev": "DB Prod"},
           "source_connection": "X", "target_connection": "Y"}
    assert saved_map(cfg, ["DB Dev"]) == {"DB Dev": "DB Prod"}


def test_saved_map_reads_an_older_single_pair():
    cfg = {"source_connection": "DB Dev", "target_connection": "DB Prod"}
    assert saved_map(cfg, ["DB Dev", "SF Dev"]) == {"DB Dev": "DB Prod"}


def test_saved_map_older_blank_source_meant_every_connection():
    cfg = {"source_connection": "", "target_connection": "DB Prod"}
    assert saved_map(cfg, ["DB Dev", "SF Dev"]) == {"DB Dev": "DB Prod", "SF Dev": "DB Prod"}


def test_saved_map_after_save_the_pair_is_blank_and_ignored():
    cfg = {"connection_map": {}, "source_connection": "", "target_connection": ""}
    assert saved_map(cfg, ["DB Dev"]) == {}


def test_effective_map_defaults_to_a_same_named_target_connection():
    eff = effective_map({}, ["DB", "SF Dev"], target_names=["DB", "SF Prod"])
    assert eff == {"DB": "DB"}
    assert unmapped(["DB", "SF Dev"], eff) == ["SF Dev"]


def test_effective_map_saved_choice_beats_the_same_name():
    eff = effective_map({"connection_map": {"DB": "DB Prod"}}, ["DB"], target_names=["DB", "DB Prod"])
    assert eff == {"DB": "DB Prod"}


def test_effective_map_assumes_nothing_when_the_target_list_was_unreadable():
    eff = effective_map({}, ["DB"], target_names=None)
    assert eff == {}
    assert unmapped(["DB"], eff) == ["DB"]


def test_target_for_falls_back_to_its_own_name():
    assert target_for("DB Dev", {"DB Dev": "DB Prod"}) == "DB Prod"
    assert target_for("SF Dev", {"DB Dev": "DB Prod"}) == "SF Dev"


def test_transform_remaps_each_connection_on_its_own(make_table_doc, make_item):
    items = _items(make_table_doc, make_item, "DB Dev", "SF Dev")
    out, _ = transform_items(items, connection_map={"DB Dev": "DB Prod", "SF Dev": "SF Prod"})
    conns = [json.loads(i["edoc"])["table"]["connection"] for i in out]
    assert [c["name"] for c in conns] == ["DB Prod", "SF Prod"]


def test_transform_map_drops_source_ids_only_on_remapped_connections(make_table_doc):
    doc = make_table_doc(connection="DB Dev")
    doc["table"]["connection"].update({"fqn": "src-guid", "obj_id": "db_dev"})
    other = make_table_doc(connection="SF Dev")
    other["table"]["connection"].update({"fqn": "sf-guid", "obj_id": "sf_dev"})
    out, _ = transform_doc(doc, connection_map={"DB Dev": "DB Prod"})
    untouched, _ = transform_doc(other, connection_map={"DB Dev": "DB Prod"})
    assert out["table"]["connection"] == {"name": "DB Prod"}
    assert untouched["table"]["connection"] == {"name": "SF Dev", "fqn": "sf-guid", "obj_id": "sf_dev"}


def test_transform_map_remaps_model_table_entries():
    model = {"model": {"name": "m", "model_tables": [
        {"name": "a", "fqn": "x", "connection": {"name": "DB Dev", "fqn": "g"}}]}}
    out, _ = transform_doc(model, connection_map={"DB Dev": "DB Prod"})
    assert out["model"]["model_tables"][0]["connection"] == {"name": "DB Prod"}


def test_transform_without_a_map_keeps_the_older_pair_behaviour(make_table_doc):
    doc = make_table_doc(connection="Anything")
    out, _ = transform_doc(doc, source_connection="", target_connection="DB Prod")
    assert out["table"]["connection"]["name"] == "DB Prod"
