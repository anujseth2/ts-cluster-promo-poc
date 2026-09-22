"""L1: an IMPORT must always leave a record. A validate need only do so when it fails.

2026-09-23: a promotion left the target's columns unchanged and reported its objects as "created",
and there was no way to tell what the platform had done — nine VALIDATE_ONLY responses were on
disk and not a single import. A validate is a dry run you can repeat; an import writes to the
target and cannot be reconstructed afterwards.
"""
import json
import pathlib

from services.ts_client import TSClient


class _C(TSClient):
    def __init__(self, log_path):
        self.host = "https://example.thoughtspot.cloud"
        self.debug_raw_log = str(log_path)


def _records(path):
    p = pathlib.Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_a_clean_import_is_logged(tmp_path):
    c = _C(tmp_path / "validate_raw.jsonl")
    c._append_raw_log("ALL_OR_NONE", 2, 200, [{"response": {"status": {"status_code": "OK"}}}])
    recs = _records(tmp_path / "import_raw.jsonl")
    assert len(recs) == 1, "a successful import must still be recorded"
    assert recs[0]["policy"] == "ALL_OR_NONE" and recs[0]["had_error"] is False
    assert not _records(tmp_path / "validate_raw.jsonl"), "imports go to their own file"


def test_a_clean_validate_is_not_logged(tmp_path):
    c = _C(tmp_path / "validate_raw.jsonl")
    c._append_raw_log("VALIDATE_ONLY", 2, 200, [{"response": {"status": {"status_code": "OK"}}}])
    assert not _records(tmp_path / "validate_raw.jsonl"), "a clean dry run is noise"
    assert not _records(tmp_path / "import_raw.jsonl")


def test_a_failing_validate_is_still_logged(tmp_path):
    c = _C(tmp_path / "validate_raw.jsonl")
    c._append_raw_log("VALIDATE_ONLY", 1, 200,
                      [{"response": {"status": {"status_code": "ERROR", "error_message": "boom"}}}])
    recs = _records(tmp_path / "validate_raw.jsonl")
    assert len(recs) == 1 and recs[0]["had_error"] is True


def test_the_import_record_says_what_was_sent(tmp_path):
    # Without the obj_id of each TML the log cannot answer "why was this created, not updated".
    c = _C(tmp_path / "validate_raw.jsonl")
    tml = json.dumps({"obj_id": "sales_customers",
                      "table": {"name": "sales_customers", "columns": [{"name": "a"}]}})
    c._append_raw_log("ALL_OR_NONE", 1, 200, [], files=TSClient._describe_tmls([tml]))
    sent = _records(tmp_path / "import_raw.jsonl")[0]["files"][0]
    assert sent == {"kind": "table", "name": "sales_customers",
                    "obj_id": "sales_customers", "guid": None, "columns": 1}


def test_unparseable_tml_never_breaks_the_import():
    out = TSClient._describe_tmls(["{not json", ""])
    assert len(out) == 2 and all(o["kind"] == "?" for o in out)


def test_logging_failure_never_raises(tmp_path):
    c = _C(tmp_path / "nope" / "deep" / "validate_raw.jsonl")
    c._append_raw_log("ALL_OR_NONE", 1, 200, object())      # unserialisable, unwritable
