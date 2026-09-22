"""L1: the update-in-place notice must not silence a real import failure.

VERBATIM from logs/import_raw.jsonl, ps-internal 2026-09-23. The import was REJECTED with 14544
"Deleted columns have dependents", and because the platform appends "Existing guid ... will be
used" to the same message, a substring check treated the whole thing as benign. The UI said
"Import complete", the objects were reported as succeeded, and the target was untouched. A
promotion that silently claims success is the worst failure this tool can have.
"""
from services.ts_client import TSClient

_NOTICE = ("Warning: Existing guid 74615cf3-31d8-47c1-b432-433ba582edf0 corresponding to object "
           "Id sales_customers will be used. <br/>")
_REAL = ("Error: Deleted columns have dependents.<br/>- <b>customerID</b></br><ul><li>Test dev"
         "</li></ul><br/><b>SOLUTION:</b><br/>Either replace the deleted columns, or remove the "
         "dependencies.<br/><br/>" + _NOTICE)


def test_the_notice_alone_is_benign():
    assert TSClient._is_benign_update_notice(_NOTICE) is True
    assert TSClient._is_benign_update_notice(
        "Existing guid abc corresponding to object Id def will be used.") is True
    assert TSClient._is_benign_update_notice("") is True


def test_a_real_error_carrying_the_notice_is_not_benign():
    assert TSClient._is_benign_update_notice(_REAL) is False


def test_a_rejected_import_is_detected_as_an_error():
    raw = [{"response": {"action": "UPDATE",
                         "status": {"status_code": "ERROR", "error_code": 14544,
                                    "error_message": _REAL}}}]
    assert TSClient._raw_has_error(raw) is True, "this is the one that said 'Import complete'"


def test_a_pure_update_in_place_still_reads_as_success():
    raw = [{"response": {"status": {"status_code": "WARNING", "error_message": _NOTICE}}}]
    assert TSClient._raw_has_error(raw) is False


def test_import_results_do_not_rewrite_a_real_failure_to_ok():
    class _C(TSClient):
        def __init__(self):
            self.host = "https://example.thoughtspot.cloud"
            self.debug_raw_log = None
            self._username = self._password = None
        def _retry_post(self, url, payload, timeout=180):
            class _R:
                status_code = 200
                @staticmethod
                def json():
                    return [{"response": {"status": {"status_code": "ERROR",
                                                     "error_message": _REAL},
                                          "header": {"name": "sales_customers"}}},
                            {"response": {"status": {"status_code": "OK",
                                                     "error_message": _NOTICE},
                                          "header": {"name": "Sales Customers Model"}}}]
            return _R()
    out = _C().import_tml(["{}", "{}"], policy="PARTIAL")
    assert out[0]["status"] == "ERROR", "a rejected object must stay ERROR"
    assert "Deleted columns have dependents" in out[0]["error"]
    assert out[1]["status"] == "OK", "a genuine update-in-place stays OK"
