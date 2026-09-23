"""L1: planning the cascade a dropped column forces on the TARGET.

Verified on ps-internal 2026-09-23: dropping a shared column blocks the import and ThoughtSpot
names EVERY dependent, including a model belonging to another team. Deleting that model to
unblock an import would take all of its own answers and liveboards with it, so each kind is
handled the way that costs least — strip a model, take only the impacted tiles off a liveboard,
and delete an answer because an answer is a single visualisation.
"""
import json

from services.target_cascade import plan_cascade, plan_summary


def _model(name, cols):
    return json.dumps({"model": {"name": name, "model_tables": [{"name": "t"}],
                                 "columns": [{"name": c, "column_id": f"t::{c}"} for c in cols]}})


def _board(name, tiles):
    return json.dumps({"liveboard": {"name": name, "visualizations": [
        {"id": vid, "answer": {"name": vid, "answer_columns": [{"name": c}]}}
        for vid, c in tiles], "layout": {"tiles": [{"visualization_id": v} for v, _ in tiles]}}})


def _answer(name, col):
    return json.dumps({"answer": {"name": name, "answer_columns": [{"name": col}]}})


def test_the_whole_tree_is_walked_and_each_kind_handled_differently():
    tml = {
        "m_other": _model("Other Team Model", ["gender", "city"]),
        "lb1":     _board("Their Board", [("Viz_1", "gender"), ("Viz_2", "city")]),
        "a1":      _answer("Their Answer", "gender"),
    }
    deps = {"m_other": [{"id": "lb1", "name": "Their Board", "type": "PINBOARD_ANSWER_BOOK"},
                        {"id": "a1", "name": "Their Answer", "type": "QUESTION_ANSWER_BOOK"}]}
    actions, blocked = plan_cascade(
        [{"id": "m_other", "name": "Other Team Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert blocked == []
    by = {a["id"]: a for a in actions}
    assert by["m_other"]["action"] == "strip_columns", "a model is edited, never deleted"
    assert by["lb1"]["action"] == "remove_tiles" and "Viz_1" in by["lb1"]["detail"]
    assert "1 left on the board" in by["lb1"]["detail"], "the other tile survives"
    assert by["a1"]["action"] == "delete"
    # the model really loses the column, and keeps the one nobody asked about
    left = [c["name"] for c in json.loads(by["m_other"]["new_edoc"])["model"]["columns"]]
    assert left == ["city"]


def test_an_object_that_cannot_be_read_blocks_the_whole_cascade():
    # GSK has no RBAC/OMS, so part of the tree is routinely invisible. A half-applied cascade
    # leaves the target inconsistent AND the import still blocked.
    actions, blocked = plan_cascade(
        [{"id": "hidden", "name": "Someone Else's Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, lambda i: None, lambda i: [])
    assert actions == []
    assert blocked and "cannot see it" in blocked[0]["reason"]


def test_a_board_whose_every_tile_uses_the_column_is_refused_not_emptied():
    tml = {"lb": _board("All Affected", [("Viz_1", "gender"), ("Viz_2", "gender")])}
    actions, blocked = plan_cascade(
        [{"id": "lb", "name": "All Affected", "type": "LIVEBOARD"}],
        {"gender"}, tml.get, lambda i: [])
    assert actions == []
    assert "empty board" in blocked[0]["reason"]


def test_objects_the_promotion_is_updating_are_never_touched():
    tml = {"mine": _model("Sales Customers Model", ["gender"])}
    actions, blocked = plan_cascade(
        [{"id": "mine", "name": "Sales Customers Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: [], skip_ids=["mine"])
    assert (actions, blocked) == ([], [])


def test_a_cycle_or_repeat_does_not_loop_forever():
    tml = {"m1": _model("M1", ["gender"]), "m2": _model("M2", ["gender"])}
    deps = {"m1": [{"id": "m2", "name": "M2"}], "m2": [{"id": "m1", "name": "M1"}]}
    actions, blocked = plan_cascade([{"id": "m1", "name": "M1"}], {"gender"},
                                    tml.get, lambda i: deps.get(i, []))
    assert sorted(a["id"] for a in actions) == ["m1", "m2"] and blocked == []


def test_an_object_that_does_not_use_the_column_produces_no_action():
    tml = {"m": _model("Unrelated", ["city"])}
    actions, blocked = plan_cascade([{"id": "m", "name": "Unrelated"}], {"gender"},
                                    tml.get, lambda i: [])
    assert (actions, blocked) == ([], [])


def test_summary_reads_as_a_plan():
    lines = plan_summary(
        [{"id": "m", "name": "M", "type": "LOGICAL_TABLE", "action": "strip_columns",
          "detail": "remove t::gender"}],
        [{"id": "x", "name": "X", "reason": "cannot see it"}])
    assert "**M** (logical_table) — remove t::gender" in lines[0]
    assert "CANNOT PROCEED" in lines[1]


def test_the_plan_is_dry_run_before_anything_is_written():
    from services.target_cascade import dry_run_plan
    actions = [{"id": "a", "name": "Good", "new_edoc": "{}"},
               {"id": "b", "name": "Bad", "new_edoc": "{}"},
               {"id": "c", "name": "Deleted", "new_edoc": None}]
    seen = []

    def _validate(edoc):
        seen.append(edoc)
        return (len(seen) != 2), "the target rejected it"

    problems = dry_run_plan(actions, _validate)
    assert [p["name"] for p in problems] == ["Bad"]
    assert len(seen) == 2, "a delete has no TML to validate"


def test_a_validator_that_raises_is_a_problem_not_a_crash():
    from services.target_cascade import dry_run_plan
    def _boom(_e):
        raise RuntimeError("connection reset")
    out = dry_run_plan([{"id": "a", "name": "A", "new_edoc": "{}"}], _boom)
    assert out[0]["error"] == "connection reset"


def test_every_object_is_snapshotted_before_it_changes():
    from services.target_cascade import snapshot_plan
    saved = {}
    paths = snapshot_plan(
        [{"id": "g1", "name": "Their Board"}, {"id": "g2", "name": "M/odd:name"}],
        lambda i: f"tml-for-{i}",
        lambda name, text: (saved.__setitem__(name, text), name)[1])
    assert len(paths) == 2
    assert saved["g1__Their_Board.tml"] == "tml-for-g1"
    assert any(k.startswith("g2__M_odd_name") for k in saved), "unsafe chars are made filename-safe"


def test_a_snapshot_that_cannot_be_taken_stops_everything():
    from services.target_cascade import snapshot_plan
    try:
        snapshot_plan([{"id": "g", "name": "X"}], lambda i: None, lambda n, t: n)
        assert False, "must raise"
    except RuntimeError as e:
        assert "nothing has been changed" in str(e)
