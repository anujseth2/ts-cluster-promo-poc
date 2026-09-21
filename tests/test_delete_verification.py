"""L1: delete-then-verify contract (ps-internal 2026-09-22: 204 does not mean deleted)."""
from services.ts_client import TSClient


class _Fake(TSClient):
    def __init__(self, status, survives):
        self._status, self._survives = status, survives
        self.calls = []

    def delete_metadata(self, obj_type, identifier):
        self.calls.append(("delete", obj_type, identifier))
        return self._status

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
    assert ok is False and status == 403 and "403" in detail
    assert ("check", "ANSWER", "g1") not in f.calls
