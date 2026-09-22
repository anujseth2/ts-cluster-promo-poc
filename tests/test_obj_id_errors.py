"""L1: update-obj-id failures must name the object, not blame privileges.

ps-internal 2026-09-22: asking for an obj_id another object already holds returns HTTP 500 with
code 14009 / DUPLICATE_CUSTOM_OBJECT_ID. The app reported every failure as "account needs
DATAMANAGEMENT or ADMINISTRATION", which sent the operator to check rights that were fine.
"""
from services.ts_client import TSClient

_REAL_DUP = {"error": {"message": {"debug": {
    "code": 14009, "incident_id_guid": "e67e6cce-278d-4bbe-80c8-ac0aa85997aa",
    "debug": "[\"Error Code: FAILED_TO_COMMIT Incident Id: e67e6cce\\nError Message: "
             "com.thoughtspot.atlas.AtlasException:  code\\u003dDUPLICATE_CUSTOM_OBJECT_ID, "
             "id\\u003d5e1e64d9-e87c-443b-a5f2-fa526f1de7a3 customObjectId\\u003dzz_probe_other\"]"}}}}


class _Resp:
    def __init__(self, status, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def test_duplicate_obj_id_is_named_and_not_called_a_privilege_problem():
    msg = TSClient._obj_id_error(_Resp(500, _REAL_DUP))
    assert "`zz_probe_other`" in msg, "name the obj_id that is taken"
    assert "already held by a different object" in msg
    assert "DATAMANAGEMENT" not in msg, "a 500 collision is not a rights problem"


def test_a_real_privilege_failure_still_says_so():
    msg = TSClient._obj_id_error(_Resp(403, None, "forbidden"))
    assert "DATAMANAGEMENT" in msg and "ADMINISTRATION" in msg


def test_an_unrecognised_failure_shows_the_status_and_body():
    msg = TSClient._obj_id_error(_Resp(502, None, "upstream exploded"))
    assert "502" in msg and "upstream exploded" in msg


class _Client(TSClient):
    """Batch fails; individual calls succeed except the one holding a duplicate."""
    def __init__(self, bad_obj_id):
        self._bad = bad_obj_id
        self.singles = 0

    def _update_obj_id_once(self, mappings):
        if len(mappings) > 1:
            return _Resp(500, _REAL_DUP)
        self.singles += 1
        m = mappings[0]
        return _Resp(500, _REAL_DUP) if m["new_obj_id"] == self._bad else _Resp(204)


def test_a_failed_batch_is_retried_one_by_one_to_name_the_culprit():
    # The API takes the whole batch in one call, so one bad mapping fails all of them and says
    # nothing about which. Without the per-object retry a 20-object fix is an opaque 500.
    c = _Client("taken_id")
    maps = [{"identifier": "g1", "new_obj_id": "fine_a"},
            {"identifier": "g2", "new_obj_id": "taken_id"},
            {"identifier": "g3", "new_obj_id": "fine_b"}]
    try:
        c.update_obj_ids(maps)
        assert False, "must raise"
    except RuntimeError as e:
        msg = str(e)
    assert c.singles == 3, "every mapping is probed"
    assert "2 of 3" in msg and "1 failed" in msg
    assert "taken_id" in msg and "zz_probe_other" in msg
    assert "fine_a" not in msg, "do not blame the ones that worked"


def test_a_clean_batch_never_retries():
    class _Ok(TSClient):
        def __init__(self): self.calls = 0
        def _update_obj_id_once(self, mappings):
            self.calls += 1
            return _Resp(204)
    c = _Ok()
    assert c.update_obj_ids([{"identifier": "g", "new_obj_id": "x"}]) is True
    assert c.calls == 1
    assert c.update_obj_ids([]) is True and c.calls == 1


def test_find_objects_by_name_sends_the_original_spelling():
    # metadata/search's `identifier` is CASE-SENSITIVE. Lowercasing the query before sending it
    # returns zero rows, so the blocking object looks "not visible" when it is right there — which
    # would tell the operator to go chase an owner for something they could delete themselves.
    sent = []

    class _C(TSClient):
        def __init__(self): pass
        def _post(self, path, payload):
            ident = payload["metadata"][0]["identifier"]
            sent.append(ident)
            if ident != "DEP TEST Answer (uses O Clerk)":
                return []
            return [{"metadata_id": "g1", "metadata_name": "DEP TEST Answer (uses O Clerk)",
                     "metadata_type": "ANSWER",
                     "metadata_header": {"authorName": "anuj.seth"}}]

    got = _C().find_objects_by_name(["DEP TEST Answer (uses O Clerk)", "Ghost Object"])
    assert all(s == s.strip() and s != s.lower() or s == "Ghost Object" for s in sent), sent
    assert "DEP TEST Answer (uses O Clerk)" in sent, "query must use the original spelling"
    hit = got["dep test answer (uses o clerk)"]
    assert hit == {"id": "g1", "type": "ANSWER",
                   "name": "DEP TEST Answer (uses O Clerk)", "author": "anuj.seth"}
    assert "ghost object" not in got, "a name that resolves to nothing is simply absent"
