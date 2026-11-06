"""L1: services/joins — model joins named in a dependents list, and drops that remove joins."""
import json

from services import joins as jn

# GSK's Source Of Business shape, cut down: a fact joined to two aliases of one dim on a composite
# key that includes DIAGNOSIS_CATEGORY, plus a dim joined on other columns.
FACT, DIM = "fact_sob_weekly_monthly_sbu", "dim_launch_date_alignment_sob"
ON = ("[" + DIM + "::{k}] = [" + FACT + "::{k}] AND [" + DIM + "::DIAGNOSIS] = ["
      + FACT + "::DIAGNOSIS_CATEGORY]")
MODEL = {"model": {"name": "Source Of Business", "model_tables": [
    {"name": FACT},
    {"name": "dim_sob", "alias": "dim_sob (weekly)",
     "joins": [{"with": FACT, "on": "[dim_sob::Date_ID] = [" + FACT + "::Week_ID]"}]},
    {"name": DIM, "alias": DIM + " (Monthly)", "joins": [{"with": FACT, "on": ON.format(k="Month_ID")}]},
    {"name": DIM, "alias": DIM + " (weekly)", "joins": [{"with": FACT, "on": ON.format(k="Week_ID")}]},
], "columns": [
    {"name": "Diagnosis Category", "column_id": FACT + "::DIAGNOSIS_CATEGORY"},
    {"name": "Launch Month Number", "column_id": DIM + " (Monthly)::LAUNCH_MONTH_NUMBER"},
    {"name": "Launch Week Number", "column_id": DIM + " (weekly)::LAUNCH_WEEK_NUMBER"},
    {"name": "Week", "column_id": "dim_sob (weekly)::Date_ID"},
]}}
ITEMS = [{"edoc": json.dumps(MODEL)}]


def test_generated_join_names_are_recognised_in_either_direction():
    names = [FACT + "_to_" + DIM + "_WipoHWB", FACT + "_to_" + DIM + "_s6GEJGn",
             "Source Of Business", DIM + "_to_" + FACT + "_IsUlmmh"]
    m = jn.match_join_names(names, [MODEL], column="DIAGNOSIS_CATEGORY")
    assert set(m) == {names[0], names[1], names[3]}           # the model itself is not a join
    assert {j["from"] for j in m[names[0]]} == {DIM + " (Monthly)", DIM + " (weekly)"}


def test_a_suffix_that_is_not_a_plain_token_is_not_a_join():
    assert jn.match_join_names([FACT + "_to_" + DIM + " report"], [MODEL]) == {}


def test_table_level_joins_match_on_their_own_name():
    doc = {"table": {"name": "Commerce", "joins_with": [
        {"name": "Commerce_to_Brand", "destination": {"name": "Brand"},
         "on": "[Commerce::Brand ID] = [Brand::Brand ID]"}]}}
    assert list(jn.match_join_names(["Commerce_to_Brand"], [doc])) == ["Commerce_to_Brand"]


def test_dropping_a_join_key_names_the_joins_and_what_is_cut_off():
    (imp,) = jn.join_key_impact(ITEMS, {FACT + "::DIAGNOSIS_CATEGORY"})
    assert imp["model"] == "Source Of Business"
    assert len(imp["joins"]) == 2
    assert imp["cut_off"] == [DIM + " (Monthly)", DIM + " (weekly)"]
    assert imp["lost_columns"] == ["Launch Month Number", "Launch Week Number"]
    (line,) = jn.describe_impact([imp])
    assert "join key" in line and "Launch Week Number" in line


def test_a_column_no_join_uses_has_no_impact():
    assert jn.join_key_impact(ITEMS, {FACT + "::PAY_TYPE"}) == []
    assert jn.join_key_impact(ITEMS, set()) == []


def test_a_scoped_drop_on_another_table_does_not_count():
    assert jn.join_key_impact(ITEMS, {"other_table::DIAGNOSIS_CATEGORY"}) == []


def test_a_join_that_still_leaves_a_path_cuts_nothing_off():
    doc = {"model": {"name": "M", "model_tables": [
        {"name": "f"},
        {"name": "d", "joins": [{"with": "f", "on": "[d::k] = [f::k] AND [d::x] = [f::x]"}]},
        {"name": "e", "joins": [{"with": "f", "on": "[e::k] = [f::k]"},
                                {"with": "d", "on": "[e::y] = [d::y]"}]}],
        "columns": [{"name": "D", "column_id": "d::k"}]}}
    (imp,) = jn.join_key_impact([{"edoc": json.dumps(doc)}], {"f::x"})
    assert imp["cut_off"] == [] and imp["lost_columns"] == []


def test_the_block_explanation_says_what_is_true_for_each_case():
    j = [{"from": DIM + " (Monthly)", "with": FACT}, {"from": DIM + " (weekly)", "with": FACT}]
    names = ["a_to_b_WipoHWB", "a_to_b_s6GEJGn"]
    s = jn.describe_join_block("DIAGNOSIS_CATEGORY", "Source Of Business", names, j, False, False)
    assert "are joins in **Source Of Business**" in s and "no longer joins on it" in s
    assert "source table" in s
    s = jn.describe_join_block("DIAGNOSIS_CATEGORY", "Source Of Business", names[:1], j[:1], None, True)
    assert "is a join" in s and "isn't in this promotion" in s and "This run is dropping" in s


def test_a_lowercased_drop_key_is_shown_as_the_join_spells_it():
    (imp,) = jn.join_key_impact(ITEMS, {FACT + "::diagnosis_category"})
    assert imp["columns"] == [FACT + "::DIAGNOSIS_CATEGORY"]


def test_a_model_outside_the_promotion_points_at_the_sweep():
    j = [{"from": "d (east)", "with": "f"}]
    s = jn.describe_join_block("K", "M", ["d_to_f_abc1234"], j, None, False)
    assert "What the target holds" in s
