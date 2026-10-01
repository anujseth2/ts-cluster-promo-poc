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


# ── a lookup that could not run is not a lookup that found nothing ───────────────────────────
#
# GSK 2026-09-30: the asset picker reported "0 already on target, 31 not on target" while all 31
# were plainly present on the target cluster. _resolve_names_to_ids swallowed the per-name
# HTTPError and returned {}, which the caller could not tell apart from a genuine empty result.
# `identifier` is an exact, case-sensitive match and a missing name returns HTTP 200 with zero
# rows (verified on-cluster), so an HTTPError there is never "not found" — it is systemic.

import requests as _requests


class _FakeResolve(TSClient):
    def __init__(self, raise_status=None, rows=None):
        self._raise, self._rows = raise_status, rows or []

    def _post(self, path, payload):
        if self._raise is not None:
            resp = _requests.Response()
            resp.status_code = self._raise
            raise _requests.HTTPError(f"{self._raise} error", response=resp)
        return {"metadata": self._rows}


def test_a_refused_lookup_raises_instead_of_looking_like_an_empty_result():
    for code in (401, 403, 500):
        try:
            _FakeResolve(raise_status=code)._resolve_names_to_ids(["orders"])
        except RuntimeError as e:
            assert "nothing was checked" in str(e).lower()
            assert str(code) in str(e)
        else:
            raise AssertionError(f"HTTP {code} was swallowed and reported as 'not found'")


def test_an_expired_token_says_so():
    try:
        _FakeResolve(raise_status=401)._resolve_names_to_ids(["orders"])
    except RuntimeError as e:
        assert "expired" in str(e).lower()


def test_a_name_that_genuinely_does_not_exist_is_simply_absent():
    # Zero rows with HTTP 200 is the real "not found", and must stay quiet.
    assert _FakeResolve(rows=[])._resolve_names_to_ids(["nope"]) == {}


def test_a_name_that_exists_resolves():
    got = _FakeResolve(rows=[{"metadata_name": "orders", "metadata_id": "g1"}]
                       )._resolve_names_to_ids(["orders"])
    assert got == {"orders": "g1"}


# ── resolving a blocker by name: an exact search is not the last word ────────────────────────
#
# GSK 2026-10-01: the panel said "Not visible to this account" about a model the operator owned
# and could see in the UI. metadata/search `identifier` is an EXACT, case-sensitive match, and
# the name being searched comes out of ThoughtSpot's own 14544 HTML, which pads its list items.
# One stray space and the search misses an object sitting in plain view.

class _FakeFind(TSClient):
    def __init__(self, exact_rows=None, listing=None, raise_status=None):
        self._exact = exact_rows or []
        self._listing = listing or {}
        self._raise = raise_status
        self.listed = []

    def _post(self, path, payload):
        if self._raise is not None:
            resp = _requests.Response(); resp.status_code = self._raise
            raise _requests.HTTPError("nope", response=resp)
        ident = payload["metadata"][0]["identifier"]
        return {"metadata": [r for r in self._exact if r["metadata_name"] == ident]}

    def list_metadata(self, obj_type):
        self.listed.append(obj_type)
        return self._listing.get(obj_type, [])


def test_a_name_that_differs_only_by_padding_still_resolves():
    # The stored name is clean; the name in the error carries a trailing space and a double space.
    f = _FakeFind(listing={"LOGICAL_TABLE": [
        {"id": "g1", "name": "OE test - Respbio Subnational Performance",
         "type": "LOGICAL_TABLE", "author": "anuj.seth"}]})
    got = f.find_objects_by_name(["OE test -  Respbio Subnational Performance "])
    assert list(got.values())[0]["id"] == "g1"


def test_a_typographic_dash_does_not_defeat_the_match():
    f = _FakeFind(listing={"LOGICAL_TABLE": [
        {"id": "g2", "name": "OE test - Respbio", "type": "LOGICAL_TABLE", "author": "a"}]})
    assert list(f.find_objects_by_name(["OE test – Respbio"]).values())[0]["id"] == "g2"


def test_the_exact_search_is_still_tried_first_and_avoids_the_listing():
    f = _FakeFind(exact_rows=[{"metadata_name": "Clean Name", "metadata_id": "g3",
                               "metadata_type": "ANSWER", "metadata_header": {}}])
    got = f.find_objects_by_name(["Clean Name"])
    assert got["clean name"]["id"] == "g3"
    assert f.listed == [], "no listing scan when the exact search already resolved it"


def test_a_name_that_genuinely_is_not_there_stays_unresolved():
    assert _FakeFind().find_objects_by_name(["Nothing Like This"]) == {}


def test_a_refused_search_raises_rather_than_looking_like_invisibility():
    try:
        _FakeFind(raise_status=401).find_objects_by_name(["Anything"])
    except RuntimeError as e:
        assert "expired" in str(e).lower() and "no conclusion" in str(e).lower()
    else:
        raise AssertionError("a 401 was reported as 'not visible to this account'")


# ── the sweep: a refusal is never an empty result ────────────────────────────────────────────

class _FakeNL(TSClient):
    def __init__(self, status): self._status = status

    def _post(self, path, payload):
        resp = _requests.Response(); resp.status_code = self._status
        raise _requests.HTTPError("refused", response=resp)


def test_unreadable_nl_instructions_raise_rather_than_reading_as_none():
    # VERIFIED on-cluster: a model with no instructions answers HTTP 200 with an empty list, so an
    # error is never "none". Returning [] here was destructive: `set` is a FULL REPLACE, so a
    # refused read turned MERGE into REPLACE and wiped the target's coaching text.
    for code in (401, 403, 500):
        try:
            _FakeNL(code).get_nl_instruction_blocks("some-model")
        except RuntimeError as e:
            assert "must not be treated as absent" in str(e)
        else:
            raise AssertionError(f"HTTP {code} was reported as 'no instructions'")


def test_a_model_whose_instructions_cannot_be_read_is_skipped_not_overwritten():
    from services import nl_instructions

    class _Tgt:
        def __init__(self): self.written = []
        def find_by_obj_id(self, obj_id, obj_type="LOGICAL_TABLE"): return "tgt-guid"
        def get_nl_instruction_blocks(self, guid): raise RuntimeError("refused (HTTP 403)")
        def set_nl_instruction_blocks(self, guid, blocks):
            self.written.append(blocks); return True

    class _Src:
        def get_nl_instructions(self, guid): return ["always answer in GBP"]

    tgt = _Tgt()
    report = nl_instructions.promote(_Src(), tgt,
                                     [{"name": "M", "obj_id": "m", "source_guid": "s"}],
                                     mode="merge")
    assert tgt.written == [], "a model with an unreadable target must never be written to"
    assert "skipped" in report[0]["status"] and "403" in report[0]["status"]


def test_obj_id_search_refusal_raises_instead_of_reporting_no_obj_id():
    class _F(TSClient):
        def __init__(self): pass
        def _post(self, path, payload):
            resp = _requests.Response(); resp.status_code = 403
            raise _requests.HTTPError("refused", response=resp)
    try:
        _F().search_obj_ids(["Orders"])
    except RuntimeError as e:
        assert "no conclusion" in str(e).lower()
    else:
        raise AssertionError("a refused obj_id lookup looked like 'this object has no obj_id'")
