"""L1: tools/new_error.py — the scaffolding harness for a newly-seen platform error.

The harness exists because the slow part of handling a new error is noticing it, pinning the
verbatim bytes, and picking a stable anchor. It deliberately does NOT write the remedy, so these
tests cover the mechanical parts only.
"""
import importlib.util
import pathlib
import re

_SPEC = importlib.util.spec_from_file_location(
    "new_error", pathlib.Path(__file__).parent.parent / "tools" / "new_error.py")
ne = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ne)


def test_anchor_is_the_stable_prose_not_the_names():
    # Anchoring on the names would make the rule stop matching the next time the names differ,
    # which is every time. Anchor on the sentence the platform always emits.
    a = ne._stable_prefix("Deleted columns have dependents.<br/>- <b>O Clerk</b>"
                          "<ul><li>Some Answer</li></ul>")
    assert a == "Deleted columns have dependents"
    assert "O Clerk" not in a and "Some Answer" not in a
    b = ne._stable_prefix('Error: Tables do not exist. <br/>- <b>fact_x</b>')
    assert "fact_x" not in b and b.lower().startswith("error")


def test_generated_pattern_tolerates_whitespace_drift():
    # Platform messages reflow between releases; a pattern that breaks on a double space is a
    # pattern that silently stops matching and sends the error back to "unknown".
    pat = re.compile(ne._anchor_pattern("Deleted columns have dependents"), re.I)
    assert pat.search("Deleted columns have dependents.")
    assert pat.search("deleted  columns\thave   dependents"), "whitespace must not break it"
    assert not pat.search("Deleted columns are fine")


def test_identifiers_are_pulled_out_of_a_real_message():
    idents = dict((v, k) for k, v in ne._identifiers(
        'Unable to save. <b>fact_sales</b> column "REGION_CD" in db.sch.fact_sales.REGION_CD '
        'incident 8f74b135-e3d2-48c9-ae25-4a7ae658d649'))
    assert idents.get("fact_sales") == "bolded object"
    assert idents.get("REGION_CD") == "quoted name"
    assert "db.sch.fact_sales.REGION_CD" in idents
    assert any(k == "guid" for k in idents.values())


def test_a_message_naming_nothing_yields_no_identifiers():
    # This is the signal that a message may legitimately stay unclassified.
    assert ne._identifiers("Schema validation failed.") == []


def test_slug_is_a_usable_python_identifier():
    s = ne._slug("Deleted columns have dependents")
    assert s.isidentifier() and s.islower()
    assert ne._slug("!!! ???") == "new_error"


def test_ui_sections_are_read_from_app_not_duplicated():
    # If this list were copied into the tool it would drift and start reporting problems that
    # were already fixed. It is parsed out of app.py's _handled_kinds.
    sections = ne._ui_sections()
    assert "missing_in_target_warehouse" in sections
    assert "drop_blocked_by_dependents" in sections


def test_scaffold_includes_verbatim_bytes_and_refuses_to_write_the_remedy():
    out = ne.scaffold({"error": "Deleted columns have dependents.<br/>- <b>O Clerk</b>",
                       "status": "ERROR", "first_seen": "2026-09-22T00:00:00"})
    assert "VERBATIM" in out and "O Clerk" in out
    assert "TODO action" in out, "the remedy must be left to a human"
    assert "VERIFY THIS ON A CLUSTER" in out
