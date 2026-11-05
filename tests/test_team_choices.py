"""L1: services/team_choices — remembering a team's choices between runs."""
import json

from services import team_choices as tc


def test_missing_file_reads_as_no_choices(tmp_path):
    assert tc.load(tmp_path / "none.json", "A") == tc.empty()


def test_save_and_load_round_trip_per_team(tmp_path):
    p = tmp_path / "choices.json"
    a = {"drop_columns": ["t::legacy_code"], "prune_tables": ["old_t"],
         "realign_types": {"t::amount": "VARCHAR"}, "recase_source": {"t::cid": "CID"},
         "recase_target": {"t::Region": "REGION"}}
    tc.save(p, "A", a)
    tc.save(p, "B", {"drop_columns": ["x::y"]})
    assert tc.load(p, "A") == {**tc.empty(), **a}
    assert tc.load(p, "B")["drop_columns"] == ["x::y"]


def test_saving_nothing_removes_the_team(tmp_path):
    p = tmp_path / "choices.json"
    tc.save(p, "A", {"drop_columns": ["t::c"]})
    tc.save(p, "A", tc.empty())
    assert "A" not in json.loads(p.read_text())


def test_a_corrupt_file_reads_as_no_choices_rather_than_crashing(tmp_path):
    p = tmp_path / "choices.json"
    p.write_text("{not json")
    assert tc.load(p, "A") == tc.empty()


def test_describe_says_what_will_be_applied():
    lines = tc.describe({"drop_columns": ["t::legacy_code"], "realign_types": {"t::amount": "VARCHAR"},
                         "recase_target": {"t::Region": "REGION"}})
    assert lines == ["Drop `t.legacy_code`", "Realign `t.amount` to VARCHAR",
                     "Recase `t.Region` to `REGION` (target casing)"]
    assert tc.is_empty(tc.empty()) and not tc.is_empty({"prune_tables": ["x"]})


def test_a_target_realignment_is_recognised_whatever_the_case():
    ch = {"realign_types": {"pt_region_sales::amount": "VARCHAR"}}
    assert tc.realigned_for_target(ch, "PT_Region_Sales", "AMOUNT", "varchar")
    assert not tc.realigned_for_target(ch, "pt_region_sales", "amount", "DOUBLE")
    assert not tc.realigned_for_target(ch, "other", "amount", "VARCHAR")


def test_a_target_recase_counts_only_while_the_column_carries_it():
    ch = {"recase_target": {"pt_region_sales::Region": "REGION"}}
    assert tc.recased_for_target(ch, "pt_region_sales", "REGION")
    assert not tc.recased_for_target(ch, "pt_region_sales", "Region")
    assert not tc.recased_for_target({}, "pt_region_sales", "REGION")
