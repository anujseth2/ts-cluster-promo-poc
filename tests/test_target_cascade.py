"""L1: planning the cascade a dropped column forces on the TARGET.

Verified on ps-internal 2026-09-23: dropping a shared column blocks the import and ThoughtSpot
names EVERY dependent, including a model belonging to another team. Deleting that model to
unblock an import would take all of its own answers and liveboards with it, so each kind is
handled the way that costs least — strip a model, take only the impacted tiles off a liveboard,
and delete an answer because an answer is a single visualisation.
"""
import json
import pathlib

from services.target_cascade import (
    apply_order, dry_run_plan, plan_cascade, plan_summary, planned_names,
)


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


def test_a_board_whose_every_tile_uses_the_column_is_deleted_whole():
    # One rule for both leaf kinds: delete when EVERY visualisation is impacted, trim when only
    # some are. Trimming this board would leave an empty page, and the platform does not even
    # keep a gap to mark why, so there is nothing left worth being proportionate about.
    tml = {"lb": _board("All Affected", [("Viz_1", "gender"), ("Viz_2", "gender")])}
    actions, blocked = plan_cascade(
        [{"id": "lb", "name": "All Affected", "type": "LIVEBOARD"}],
        {"gender"}, tml.get, lambda i: [])
    assert blocked == []
    assert [(a["action"], a["type"]) for a in actions] == [("delete", "LIVEBOARD")]
    assert "WHOLE BOARD" in actions[0]["detail"], "deleting a board must not read like a trim"


def test_a_board_with_one_untouched_tile_is_trimmed_not_deleted():
    # The boundary of the same rule: one survivor is enough to keep the board.
    tml = {"lb": _board("Mostly Affected", [("Viz_1", "gender"), ("Viz_2", "gender"),
                                            ("Viz_3", "city")])}
    actions, _b = plan_cascade(
        [{"id": "lb", "name": "Mostly Affected", "type": "LIVEBOARD"}],
        {"gender"}, tml.get, lambda i: [])
    assert [a["action"] for a in actions] == ["remove_tiles"]
    assert actions[0]["verify"]["viz_count"] == 1


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


# ── order, and the one failure the dry run is right to forgive ───────────────────────────────

def test_leaves_are_written_before_the_model_they_hang_off():
    # plan_cascade walks DOWN; writing in that order would strip the model while its own answers
    # still reference the column, which is the exact 14544 block the cascade exists to clear.
    tml = {"m": _model("M", ["gender"]), "a1": _answer("A", "gender")}
    deps = {"m": [{"id": "a1", "name": "A", "type": "QUESTION_ANSWER_BOOK"}]}
    actions, _ = plan_cascade([{"id": "m", "name": "M", "type": "LOGICAL_TABLE"}],
                              {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert [a["id"] for a in actions] == ["m", "a1"]
    assert [a["id"] for a in apply_order(actions)] == ["a1", "m"]


def test_a_model_blocked_only_by_objects_this_plan_removes_still_validates():
    # The real 14544 shape, names and trailing space included (tests/corpus/validate_errors.jsonl).
    err = ("Deleted columns have dependents.<br/>- <b>gender</b></br><ul>"
           "<li>Their Answer </li></ul><br/><b>SOLUTION:</b><br/>Either replace the deleted "
           "columns, or remove the dependencies.<br/>")
    actions = [{"id": "m", "name": "Other Team Model", "new_edoc": "x"},
               {"id": "a1", "name": "Their Answer", "new_edoc": None}]
    problems = dry_run_plan(actions, lambda e: (False, err), planned_names(actions))
    assert problems == [], "the plan removes the only blocker, so this is the plan working"


def test_a_model_blocked_by_something_outside_the_plan_stops_everything():
    err = ("Deleted columns have dependents.<br/>- <b>gender</b></br><ul>"
           "<li>Their Answer </li><li>Somebody Elses Board</li></ul><br/>")
    actions = [{"id": "m", "name": "Other Team Model", "new_edoc": "x"},
               {"id": "a1", "name": "Their Answer", "new_edoc": None}]
    problems = dry_run_plan(actions, lambda e: (False, err), planned_names(actions))
    assert [p["id"] for p in problems] == ["m"]


def test_a_different_failure_is_never_forgiven():
    # Only the dependents block is expected. Anything else is a genuine reason not to start.
    err = "Unable to import tml: <b>PATIENT_AGE</b>: DataType mismatch for column."
    actions = [{"id": "m", "name": "M", "new_edoc": "x"}]
    assert dry_run_plan(actions, lambda e: (False, err), planned_names(actions))


def test_a_dependents_block_that_names_nobody_is_not_forgiven():
    # The table-only shape tells us WHICH table, never who is holding it. With no names to check
    # against the plan there is no evidence the plan clears it, so it stops the cascade.
    err = ("Unable to import tml due to following errors:<br/>- <b>sales_customers</b>: "
           "Deleted columns have dependents.<br/>")
    actions = [{"id": "m", "name": "M", "new_edoc": "x"}]
    assert dry_run_plan(actions, lambda e: (False, err), planned_names(actions))


def test_the_promoted_model_is_skipped_even_when_found_mid_walk():
    # It is discovered as a dependent, not selected, so its guid is not known up front — only
    # its name is. Stripping it would undo the very update being promoted.
    tml = {"m": _model("Other Team Model", ["gender"]),
           "promoted": _model("Sales Customers Model", ["gender"])}
    deps = {"m": [{"id": "promoted", "name": "Sales Customers Model", "type": "LOGICAL_TABLE"}]}
    actions, blocked = plan_cascade(
        [{"id": "m", "name": "Other Team Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []),
        skip_names={"sales customers model"})
    assert blocked == []
    assert [a["id"] for a in actions] == ["m"]


def test_each_action_records_what_must_be_gone_afterwards():
    # The write half re-reads the object and checks for these; without them a 200 would pass
    # as proof, which is how the tool once reported success while the target never changed.
    tml = {"m": _model("M", ["gender", "city"]),
           "lb": _board("B", [("Viz_1", "gender"), ("Viz_2", "city")]),
           "a1": _answer("A", "gender")}
    deps = {"m": [{"id": "lb", "name": "B", "type": "PINBOARD_ANSWER_BOOK"},
                  {"id": "a1", "name": "A", "type": "QUESTION_ANSWER_BOOK"}]}
    by_id = {a["id"]: a for a in plan_cascade(
        [{"id": "m", "name": "M", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []))[0]}
    assert by_id["m"]["removed"] == ["t::gender"]
    assert by_id["lb"]["removed"] == ["Viz_1"]
    assert by_id["a1"]["removed"] == []


# ── the sequence the UI actually runs ────────────────────────────────────────────────────────

def test_plan_then_snapshot_then_dry_run_then_apply_leaves_first(tmp_path):
    """End to end over the real functions, in the order the blocked-dependents panel calls them.

    The shape is the one verified on ps-internal 2026-09-23: another team's model on the shared
    table, that model's own board and answer, and the model being promoted turning up as a
    dependent of its own drop.
    """
    tml = {
        "m_other": _model("ZZ Other Team Model", ["gender", "city"]),
        "lb1":     _board("Their Board", [("Viz_1", "gender"), ("Viz_2", "city")]),
        "a1":      _answer("Their Answer", "gender"),
        "promoted": _model("Sales Customers Model", ["gender"]),
    }
    deps = {"m_other": [{"id": "lb1", "name": "Their Board", "type": "PINBOARD_ANSWER_BOOK"},
                        {"id": "a1", "name": "Their Answer", "type": "QUESTION_ANSWER_BOOK"},
                        {"id": "promoted", "name": "Sales Customers Model",
                         "type": "LOGICAL_TABLE"}]}
    actions, blocked = plan_cascade(
        [{"id": "m_other", "name": "ZZ Other Team Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []),
        skip_names={"sales customers model"})

    # Nothing unplannable, and the promotion's own model is left alone.
    assert blocked == []
    assert sorted(a["id"] for a in actions) == ["a1", "lb1", "m_other"]

    # Every object's current TML is on disk before a single write.
    from services.target_cascade import snapshot_plan
    def _write(name, text):
        (tmp_path / name).write_text(text)
        return str(tmp_path / name)

    written = snapshot_plan(actions, tml.get, _write)
    assert len(written) == 3
    assert all(pathlib.Path(w).read_text() for w in written)

    # The model cannot validate while its own answer still uses the column; that is the plan
    # working. Everything else validates outright.
    err = ("Deleted columns have dependents.<br/>- <b>gender</b></br><ul>"
           "<li>Their Answer </li></ul><br/>")
    seen_validate = []

    def _validate(edoc):
        seen_validate.append(edoc)
        return (False, err) if "ZZ Other Team Model" in edoc else (True, "")

    assert dry_run_plan(actions, _validate, planned_names(actions)) == []
    assert len(seen_validate) == 2, "the answer is a delete, so there is nothing to validate"

    # Written children-first, so the model loses the column only once nothing below it uses it.
    assert [a["id"] for a in apply_order(actions)] == ["a1", "lb1", "m_other"]


# ── the forest: roots are the only sensible unit of selection ────────────────────────────────

from services.target_cascade import plan_tree, subtree_actions, tree_lines   # noqa: E402


def _scenario():
    """The shape on prod: another team's model, its board and answer, plus two independents."""
    tml = {"m":   _model("Regional Ops Model", ["gender", "city"]),
           "lb":  _board("Regional Ops Board", [("Viz_1", "gender"), ("Viz_2", "city")]),
           "a1":  _answer("Regional Ops Gender Split", "gender"),
           "a2":  _answer("Customer Gender Mix", "gender"),
           "lb2": _board("Test dev", [("Viz_1", "gender")])}
    deps = {"m": [{"id": "lb", "name": "Regional Ops Board", "type": "PINBOARD_ANSWER_BOOK"},
                  {"id": "a1", "name": "Regional Ops Gender Split",
                   "type": "QUESTION_ANSWER_BOOK"}]}
    return tml, deps


def test_a_child_of_another_blocker_is_not_offered_as_its_own_row():
    # The platform lists all five flat. Only three are decisions.
    tml, deps = _scenario()
    cands = [{"id": i, "name": n, "type": t} for i, n, t in [
        ("a2", "Customer Gender Mix", "ANSWER"),
        ("m", "Regional Ops Model", "LOGICAL_TABLE"),
        ("lb", "Regional Ops Board", "LIVEBOARD"),
        ("a1", "Regional Ops Gender Split", "ANSWER"),
        ("lb2", "Test dev", "LIVEBOARD")]]
    roots, nodes, blocked, disputed = plan_tree(cands, {"gender"}, tml.get,
                                               lambda i: deps.get(i, []))
    assert blocked == []
    assert roots == ["a2", "m", "lb2"], "the board and answer hang off the model"
    assert sorted(nodes["m"]["children"]) == ["a1", "lb"]
    assert nodes["a2"]["children"] == [] and nodes["lb2"]["children"] == []


def test_a_child_ticked_before_its_parent_is_still_a_child():
    # Order-dependence would be a real bug: the walk records the edge even when it reaches a
    # child it has already processed, so a child listed first does not become its own root.
    tml, deps = _scenario()
    cands = [{"id": i, "name": i, "type": "x"} for i in ("lb", "a1", "m")]
    roots, nodes, _b, _d = plan_tree(cands, {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert roots == ["m"]
    assert sorted(nodes["m"]["children"]) == ["a1", "lb"]


def test_selecting_a_root_takes_its_whole_subtree():
    tml, deps = _scenario()
    cands = [{"id": i, "name": i, "type": "x"} for i in ("m", "a2", "lb2")]
    _r, nodes, _b, _d = plan_tree(cands, {"gender"}, tml.get, lambda i: deps.get(i, []))
    picked = subtree_actions(nodes, ["m"])
    assert [a["id"] for a in picked] == ["m", "lb", "a1"], "parents before children"
    assert all("children" not in a for a in picked), "actions stay plain action dicts"
    assert [a["id"] for a in subtree_actions(nodes, ["a2"])] == ["a2"]


def test_selecting_nothing_plans_nothing():
    tml, deps = _scenario()
    _r, nodes, _b, _d = plan_tree([{"id": "m", "name": "m", "type": "x"}], {"gender"},
                                  tml.get, lambda i: deps.get(i, []))
    assert subtree_actions(nodes, []) == []


def test_the_forest_renders_children_indented_and_untickable():
    tml, deps = _scenario()
    cands = [{"id": "m", "name": "Regional Ops Model", "type": "LOGICAL_TABLE"},
             {"id": "lb2", "name": "Test dev", "type": "LIVEBOARD"}]
    roots, nodes, _b, _d = plan_tree(cands, {"gender"}, tml.get, lambda i: deps.get(i, []))
    lines = tree_lines(roots, nodes)
    assert [(l["id"], l["depth"], l["tickable"]) for l in lines] == [
        ("m", 0, True), ("lb", 1, False), ("a1", 1, False), ("lb2", 0, True)]
    assert "Regional Ops Model" in lines[0]["text"]


def test_a_cycle_still_offers_something_to_tick():
    # Every node having a parent would otherwise leave the operator an empty list and no way in.
    tml = {"m1": _model("M1", ["gender"]), "m2": _model("M2", ["gender"])}
    deps = {"m1": [{"id": "m2", "name": "M2", "type": "LOGICAL_TABLE"}],
            "m2": [{"id": "m1", "name": "M1", "type": "LOGICAL_TABLE"}]}
    roots, nodes, _b, _d = plan_tree([{"id": "m1", "name": "M1", "type": "LOGICAL_TABLE"}],
                                     {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert roots and set(roots) <= set(nodes)
    assert len(subtree_actions(nodes, roots)) == len(nodes)


def test_the_order_children_are_listed_in_is_stable():
    # A set here made the rendered plan reorder itself between runs, so the confirmation an
    # operator reads would not match the one they read a moment before. Platform order, kept.
    tml, deps = _scenario()
    cands = [{"id": "m", "name": "Regional Ops Model", "type": "LOGICAL_TABLE"}]
    runs = {tuple(a["id"] for a in subtree_actions(
        plan_tree(cands, {"gender"}, tml.get, lambda i: deps.get(i, []))[1], ["m"]))
        for _ in range(5)}
    assert runs == {("m", "lb", "a1")}, f"unstable ordering: {runs}"


# ── an answer has to EARN its deletion, and a disagreement has to be said out loud ───────────

def test_an_answer_reached_by_the_walk_is_not_deleted_unless_it_uses_the_column():
    # The bug this pins: the answer branch deleted anything the walk reached, so stripping a
    # model took out every answer hanging off it, including ones built on other columns
    # entirely. The other two branches always checked; this one did not.
    tml = {"m":  _model("Regional Ops Model", ["gender", "city"]),
           "a1": _answer("Gender Split", "gender"),
           "a2": _answer("City Report", "city")}
    deps = {"m": [{"id": "a1", "name": "Gender Split", "type": "QUESTION_ANSWER_BOOK"},
                  {"id": "a2", "name": "City Report", "type": "QUESTION_ANSWER_BOOK"}]}
    actions, blocked = plan_cascade(
        [{"id": "m", "name": "Regional Ops Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert blocked == []
    assert [a["id"] for a in actions] == ["m", "a1"], "City Report never mentions gender"


def test_an_answer_the_platform_named_is_still_deleted():
    # The check must not swing the other way: a named answer that does use the column goes.
    tml = {"a1": _answer("Gender Split", "gender")}
    actions, _b = plan_cascade([{"id": "a1", "name": "Gender Split", "type": "ANSWER"}],
                               {"gender"}, tml.get, lambda i: [])
    assert [(a["id"], a["action"]) for a in actions] == [("a1", "delete")]


def test_a_named_blocker_we_find_no_reference_in_is_reported_not_dropped():
    # ThoughtSpot says it blocks; our scan of its TML disagrees. One of us is wrong and it is
    # likelier to be us, so it must not silently vanish from the plan.
    tml = {"a2": _answer("City Report", "city")}
    roots, nodes, blocked, disputed = plan_tree(
        [{"id": "a2", "name": "City Report", "type": "ANSWER"}],
        {"gender"}, tml.get, lambda i: [])
    assert roots == [] and nodes == {} and blocked == []
    assert [(d["id"], d["kind"]) for d in disputed] == [("a2", "answer")]


def test_an_object_found_by_recursion_with_no_reference_is_not_a_disagreement():
    # Nobody claimed it was blocking, so leaving it alone is the correct, silent outcome.
    tml = {"m":  _model("Regional Ops Model", ["gender"]),
           "a2": _answer("City Report", "city")}
    deps = {"m": [{"id": "a2", "name": "City Report", "type": "QUESTION_ANSWER_BOOK"}]}
    _r, _n, _b, disputed = plan_tree(
        [{"id": "m", "name": "Regional Ops Model", "type": "LOGICAL_TABLE"}],
        {"gender"}, tml.get, lambda i: deps.get(i, []))
    assert disputed == []


def test_a_named_board_with_no_matching_tile_is_disputed_too():
    tml = {"lb": _board("Untouched Board", [("Viz_1", "city"), ("Viz_2", "state")])}
    _r, _n, _b, disputed = plan_tree([{"id": "lb", "name": "Untouched Board", "type": "LIVEBOARD"}],
                                     {"gender"}, tml.get, lambda i: [])
    assert [(d["id"], d["kind"]) for d in disputed] == [("lb", "liveboard")]
