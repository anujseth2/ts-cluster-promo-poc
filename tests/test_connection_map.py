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


# ── database.schema per source connection ────────────────────────────────────────────────────

from services.connection_map import (
    saved_locations, locations_by_connection, target_copies, effective_locations,
)

_SEL = {
    "a": {"name": "a", "connection": "DB Dev", "db": "hive", "schema": "dev_s",
          "target": {"connection": "DB Prod", "db": "hive", "schema": "prod_s"}},
    "b": {"name": "b", "connection": "DB Dev", "db": "hive", "schema": "dev_s",
          "target": {"connection": "DB Prod", "db": "hive", "schema": "prod_s"}},
    "c": {"name": "c", "connection": "DB Dev", "db": "hive", "schema": "other", "target": None},
    "d": {"name": "d", "connection": "SF Dev", "db": "ANALYTICS", "schema": "PUBLIC",
          "target": {"connection": "SF Prod", "db": "ANALYTICS", "schema": "PUBLIC"}},
}


def test_saved_locations_reads_the_pair_list():
    cfg = {"location_map": [{"source_db": "hive", "source_schema": "dev_s",
                             "target_db": "hive", "target_schema": "prod_s"}]}
    assert saved_locations(cfg) == {("hive", "dev_s"): ("hive", "prod_s")}


def test_locations_are_grouped_by_connection_then_pair():
    by = locations_by_connection(_SEL)
    assert by["DB Dev"] == {("hive", "dev_s"): ["a", "b"], ("hive", "other"): ["c"]}
    assert by["SF Dev"] == {("ANALYTICS", "PUBLIC"): ["d"]}


def test_target_copies_reports_connection_location_and_missing():
    tc = target_copies(_SEL, "DB Dev")
    assert tc["connections"] == {"DB Prod"} and tc["missing"] == ["c"]
    assert target_copies(_SEL, "DB Dev", ("hive", "dev_s"))["locations"] == {("hive", "prod_s")}


def test_effective_locations_follow_existing_target_tables_unless_saved():
    eff = effective_locations({}, _SEL)
    assert eff == {("hive", "dev_s"): ("hive", "prod_s")}       # identity pairs aren't listed
    saved = {("hive", "dev_s"): ("hive", "chosen")}
    assert effective_locations(saved, _SEL)[("hive", "dev_s")] == ("hive", "chosen")


def test_transform_maps_database_and_schema_as_a_pair(make_table_doc):
    one = make_table_doc(db="hive", schema="dev_s")
    other = make_table_doc(db="other_db", schema="dev_s")      # same schema name, other database
    lm = {("hive", "dev_s"): ("hive", "prod_s")}
    out, _ = transform_doc(one, location_map=lm)
    untouched, _ = transform_doc(other, location_map=lm)
    assert (out["table"]["db"], out["table"]["schema"]) == ("hive", "prod_s")
    assert (untouched["table"]["db"], untouched["table"]["schema"]) == ("other_db", "dev_s")


def test_pair_mapping_wins_over_the_older_separate_renames(make_table_doc):
    doc = make_table_doc(db="hive", schema="dev_s")
    out, _ = transform_doc(doc, db_map={"hive": "x"}, schema_map={"dev_s": "y"},
                           location_map={("hive", "dev_s"): ("hive", "prod_s")})
    assert (out["table"]["db"], out["table"]["schema"]) == ("hive", "prod_s")
