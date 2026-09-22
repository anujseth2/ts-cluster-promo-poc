"""L1: delete-then-verify contract (ps-internal 2026-09-22: 204 does not mean deleted)."""
from services.ts_client import TSClient


class _R:
    """Minimal response stand-in: status plus an empty body."""
    def __init__(self, status): self.status_code, self.text = status, ""
    def json(self): raise ValueError("no json")


class _Fake(TSClient):
    def __init__(self, status, survives):
        self._status, self._survives = status, survives
        self.calls = []

    def _delete_once(self, metadata_type, identifier):
        self.calls.append(("delete", metadata_type, identifier))
        return _R(self._status)

    def object_exists(self, obj_type, identifier):
        self.calls.append(("check", obj_type, identifier))
        return self._survives


def test_204_that_deleted_nothing_is_reported_as_failure():
    # The exact ps-internal behaviour: a caller without rights gets 204 and the object survives.
    ok, status, detail = _Fake(204, survives=True).delete_metadata_verified("ANSWER", "g1")
    assert ok is False and status == 204
    assert "still there" in detail and "rights" in detail


def test_real_deletion_is_reported_as_success():
    f = _Fake(204, survives=False)
    ok, _s, detail = f.delete_metadata_verified("ANSWER", "g1")
    assert ok is True and detail == "deleted"
    assert f.calls == [("delete", "ANSWER", "g1"), ("check", "ANSWER", "g1")]


def test_http_error_never_checks_and_never_claims_success():
    f = _Fake(403, survives=False)
    ok, status, detail = f.delete_metadata_verified("ANSWER", "g1")
    assert ok is False and status == 403
    assert "rights" in detail and "admin" in detail
    assert ("check", "ANSWER", "g1") not in f.calls


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
        def object_exists(self, *a): raise AssertionError("must not check after a hard failure")
    c = _C()
    ok, status, detail = c.delete_metadata_verified("QUESTION_ANSWER_BOOK", "g1")
    assert ok is False and status == 400
    assert "bug in this tool" in detail and "not a permission problem" in detail
    assert c.seen == ["ANSWER"], "the dependency type must be normalised before the call"


def test_object_not_found_is_not_reported_as_a_rights_problem():
    class _C(TSClient):
        def __init__(self): pass
        def _delete_once(self, metadata_type, identifier):
            return _BodyResp(400, {"error": {"message": {"debug": {"code": 13003}}}})
        def object_exists(self, *a): raise AssertionError("must not check")
    ok, _s, detail = _C().delete_metadata_verified("ANSWER", "gone")
    assert ok is False
    assert "no such object" in detail and "rights" not in detail


def test_dependency_types_map_to_the_v2_metadata_types():
    assert TSClient.metadata_type_for("QUESTION_ANSWER_BOOK") == "ANSWER"
    assert TSClient.metadata_type_for("PINBOARD_ANSWER_BOOK") == "LIVEBOARD"
    assert TSClient.metadata_type_for("LOGICAL_TABLE") == "LOGICAL_TABLE"
    assert TSClient.metadata_type_for("answer") == "ANSWER"
