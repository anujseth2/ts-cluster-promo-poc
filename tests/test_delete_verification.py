"""L1: delete-then-verify contract (ps-internal 2026-09-22: 204 does not mean deleted)."""
from services.ts_client import TSClient


class _R:
    """Minimal response stand-in: status plus an empty body."""
    def __init__(self, status): self.status_code, self.text = status, ""
    def json(self): raise ValueError("no json")


class _Fake(TSClient):
    """`survives` is what the object does; `visible` is whether this account can see it at all.

    Both are needed. A real deletion is visible-then-gone, and gone-all-along is an account that
    could never see it — those look identical if you only check afterwards, which was the bug.
    """
    def __init__(self, status, survives, visible=True):
        self._status, self._survives, self._visible = status, survives, visible
        self.calls = []

    def _delete_once(self, metadata_type, identifier):
        self.calls.append(("delete", metadata_type, identifier))
        return _R(self._status)

    def object_exists(self, obj_type, identifier):
        self.calls.append(("check", obj_type, identifier))
        if not self._visible:
            return False                     # invisible reads as absent, before AND after
        return self._survives if self.calls.count(("delete", obj_type, identifier)) else True


def test_204_that_deleted_nothing_is_reported_as_failure():
    # The exact ps-internal behaviour: a caller without rights gets 204 and the object survives.
    ok, status, detail = _Fake(204, survives=True).delete_metadata_verified("ANSWER", "g1")
    assert ok is False and status == 204
    assert "still there" in detail and "rights" in detail


def test_real_deletion_is_reported_as_success():
    # Visible first, gone after. That pair is the only evidence a deletion actually happened.
    f = _Fake(204, survives=False, visible=True)
    ok, _s, detail = f.delete_metadata_verified("ANSWER", "g1")
    assert ok is True and detail == "deleted"
    assert f.calls == [("check", "ANSWER", "g1"), ("delete", "ANSWER", "g1"),
                       ("check", "ANSWER", "g1")]


def test_http_error_never_verifies_afterwards_and_never_claims_success():
    f = _Fake(403, survives=False, visible=True)
    ok, status, detail = f.delete_metadata_verified("ANSWER", "g1")
    assert ok is False and status == 403
    assert "rights" in detail and "admin" in detail
    # One check, the one BEFORE the delete. Nothing is verified after a hard failure, because
    # there is nothing to verify: the request never got as far as changing anything.
    assert f.calls == [("check", "ANSWER", "g1"), ("delete", "ANSWER", "g1")]


class _BodyResp:
    def __init__(self, status, body): self.status_code, self._b, self.text = status, body, ""
    def json(self): return self._b


def test_a_rejected_metadata_type_is_called_a_bug_not_a_permission_problem():
    # ps-internal 2026-09-23: the dependency listing reports QUESTION_ANSWER_BOOK, but
    # metadata/delete only accepts ANSWER and answers 400 "got invalid value" otherwise. Reporting
    # that as "you lack rights" sent the operator to an admin for a bug in this tool.
    class _C(TSClient):
        def __init__(self): self.seen = []
        def _delete_once(self, metadata_type, identifier):
            self.seen.append(metadata_type)
            return _BodyResp(400, {"error": {"message": {"debug":
                             'Variable "$metadata" got invalid value "QUESTION_ANSWER_BOOK"'}}})
        def __post_init__(self): pass
        def object_exists(self, *a):
            self.seen.append("check")
            return True
    c = _C()
    ok, status, detail = c.delete_metadata_verified("QUESTION_ANSWER_BOOK", "g1")
    assert ok is False and status == 400
    assert "bug in this tool" in detail and "not a permission problem" in detail
    assert [x for x in c.seen if x != "check"] == ["ANSWER"], \
        "the dependency type must be normalised before the call"
    assert c.seen.count("check") == 1, "no verification after a hard failure; only the pre-check"


def test_object_not_found_is_not_reported_as_a_rights_problem():
    class _C(TSClient):
        def __init__(self): self.checks = 0
        def _delete_once(self, metadata_type, identifier):
            return _BodyResp(400, {"error": {"message": {"debug": {"code": 13003}}}})
        def object_exists(self, *a):
            self.checks += 1
            return True
    c = _C()
    ok, _s, detail = c.delete_metadata_verified("ANSWER", "gone")
    assert ok is False
    assert "no such object" in detail and "rights" not in detail
    assert c.checks == 1, "no verification after a hard failure; only the pre-check"


def test_dependency_types_map_to_the_v2_metadata_types():
    assert TSClient.metadata_type_for("QUESTION_ANSWER_BOOK") == "ANSWER"
    assert TSClient.metadata_type_for("PINBOARD_ANSWER_BOOK") == "LIVEBOARD"
    assert TSClient.metadata_type_for("LOGICAL_TABLE") == "LOGICAL_TABLE"
    assert TSClient.metadata_type_for("answer") == "ANSWER"



# ── the write half of a planned cascade: import, then RE-READ and check ──────────────────────
#
# Same rule as deletion, for the same reason. A 200 from the import API proved nothing: the
# update-in-place notice read as success for weeks while the target never changed.
#
# What is checked matters as much as that it is checked. ps-internal 2026-09-23: removing Viz_1
# from a two-tile board came back with ONE tile, correctly the survivor, wearing the id Viz_1 —
# ThoughtSpot renumbers tiles on import. Verifying "Viz_1 is gone" therefore called a perfectly
# good removal a failure and stopped the cascade. Tile ids are positions, not identities.

import json as _json


class _FakeApply(TSClient):
    def __init__(self, results, after):
        self._results, self._after = results, after
        self.imported = []

    def import_tml(self, tml_strings, policy="PARTIAL"):
        self.imported.append((policy, list(tml_strings)))
        return self._results

    def export_tml(self, object_ids):
        if self._after is None:
            raise RuntimeError("export blew up")
        return [{"edoc": _json.dumps(self._after)}]


_OK = [{"status": "OK", "error": ""}]


def _board(tiles):
    """A liveboard as the cluster hands it back: ids renumbered from 1, whatever was removed."""
    return {"liveboard": {"visualizations": [
        {"id": f"Viz_{i}", "answer": {"name": f"{c} tile", "search_query": f"[{c}]",
                                      "answer_columns": [{"name": c}]}}
        for i, c in enumerate(tiles, start=1)]}}


def test_a_trimmed_board_passes_even_though_the_survivor_took_the_removed_tiles_id():
    # The exact ps-internal result: Gender tile removed, City tile survives AS Viz_1.
    f = _FakeApply(_OK, _board(["City"]))
    ok, detail = f.apply_tml_verified("lb1", "edited", columns=["gender"], viz_count=1)
    assert ok is True, detail
    assert f.imported == [("ALL_OR_NONE", ["edited"])]


def test_a_board_that_still_shows_the_dropped_column_is_a_failure():
    f = _FakeApply(_OK, _board(["gender"]))
    ok, detail = f.apply_tml_verified("lb1", "edited", columns=["gender"], viz_count=1)
    assert ok is False and "gender" in detail


def test_a_board_left_with_the_wrong_number_of_tiles_is_a_failure():
    # Count is the guard against an import that was accepted but did not land as planned.
    f = _FakeApply(_OK, _board(["City", "gender"]))
    ok, detail = f.apply_tml_verified("lb1", "edited", columns=[], viz_count=1)
    assert ok is False and "2 tile(s)" in detail and "1" in detail


def test_a_column_still_on_the_model_is_a_failure():
    f = _FakeApply(_OK, {"model": {"columns": [{"name": "Gender",
                                                "column_id": "sales_customers::gender"}]}})
    ok, detail = f.apply_tml_verified("m1", "edited", columns=["sales_customers::gender"])
    assert ok is False and "gender" in detail


def test_a_stripped_model_is_reported_as_applied():
    f = _FakeApply(_OK, {"model": {"columns": [{"name": "City",
                                                "column_id": "sales_customers::city"}]}})
    ok, _d = f.apply_tml_verified("m1", "edited", columns=["sales_customers::gender"])
    assert ok is True


def test_a_rejected_import_never_re_reads_and_never_claims_success():
    f = _FakeApply([{"status": "ERROR", "error": "Deleted columns have dependents."}], None)
    ok, detail = f.apply_tml_verified("m1", "edited", columns=["sales_customers::gender"])
    assert ok is False and "dependents" in detail


def test_an_object_that_cannot_be_re_read_is_unconfirmed_not_successful():
    f = _FakeApply(_OK, None)
    ok, detail = f.apply_tml_verified("m1", "edited", columns=["sales_customers::gender"])
    assert ok is False and "unconfirmed" in detail


# ── "not found" is not "deleted" when you could never see it ─────────────────────────────────
#
# ps-internal 2026-09-25: a non-admin issued a delete against an answer that was never shared
# with it. The API returned 204, the object was untouched, and the post-delete existence check
# said "not found" because that account could never see it in the first place. So the guard
# against a lying 204 reported success — on exactly the accounts most likely to lack rights.

class _FakeInvisible(TSClient):
    """An object this account cannot see, before or after; the delete changes nothing."""
    def __init__(self, visible):
        self._visible = visible
        self.calls = []

    def _delete_once(self, metadata_type, identifier):
        self.calls.append("delete")
        return _R(204)

    def object_exists(self, obj_type, identifier):
        self.calls.append("check")
        return self._visible


def test_a_delete_of_something_never_visible_is_unconfirmed_not_success():
    ok, status, detail = _FakeInvisible(visible=False).delete_metadata_verified("ANSWER", "g1")
    assert ok is False and status == 204
    assert "UNCONFIRMED" in detail and "could not see" in detail


def test_the_object_is_looked_for_before_the_delete_not_only_after():
    f = _FakeInvisible(visible=False)
    f.delete_metadata_verified("ANSWER", "g1")
    assert f.calls[0] == "check", "the pre-check is what makes the post-check mean anything"
    assert f.calls == ["check", "delete", "check"]
