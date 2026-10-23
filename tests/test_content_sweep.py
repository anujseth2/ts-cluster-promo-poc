"""L1: sweep/content_sweep — the parts that decide what gets shared (pure logic, no I/O)."""
import pytest

from sweep.content_sweep import (
    ConfigError, load_config, object_kind, row_record, excluded, tables_in_scope,
    current_level, needs,
)


# ── configuration refuses anything unsafe ───────────────────────────────────────────────────

def test_config_defaults_and_scopes():
    cfg = load_config("scopes:\n  - {name: Respbio, group: rb_cm, tags: [respbio]}\n")
    assert cfg["content_level"] == "MODIFY" and cfg["table_level"] == "READ_ONLY"
    assert cfg["exclude_authors"] == ["system"]
    assert cfg["scopes"][0] == {"name": "Respbio", "group": "rb_cm", "tags": ["respbio"],
                                "authors": [], "author_groups": []}


@pytest.mark.parametrize("text,reason", [
    ("scopes: []", "no scopes"),
    ("scopes:\n  - {name: x, tags: [t]}", "no group"),
    ("scopes:\n  - {name: x, group: g}", "selects nothing"),
    ("scopes:\n  - {name: x, group: g, tags: []}", "selects nothing"),
    ("table_level: MODIFY\nscopes:\n  - {name: x, group: g, tags: [t]}", "only be READ_ONLY"),
    ("content_level: CAN_ADMIN\nscopes:\n  - {name: x, group: g, tags: [t]}", "content_level"),
    ("scopes:\n  - {name: x, group: g, tags: [t]}\n  - {name: X, group: h, tags: [u]}", "twice"),
])
def test_config_refusals(text, reason):
    with pytest.raises(ConfigError, match=reason):
        load_config(text)


# ── what counts as content, tables and excluded authors ─────────────────────────────────────

def _row(mid, name, header_type="", author="anuj.seth", deps=None, last=True):
    r = {"metadata_id": mid, "metadata_name": name,
         "metadata_header": {"type": header_type, "authorName": author}}
    if deps is not None:
        r["dependent_objects"] = {"dependents": {mid: deps}}
        r["dependent_objects_is_last_batch"] = last
    return r


def test_logical_tables_split_into_models_and_tables():
    assert object_kind(_row("m", "M", "WORKSHEET"), "LOGICAL_TABLE") == "MODEL"
    assert object_kind(_row("t", "T", "ONE_TO_ONE_LOGICAL"), "LOGICAL_TABLE") == "TABLE"
    assert object_kind(_row("a", "A"), "ANSWER") == "ANSWER"


def test_system_content_is_excluded():
    assert excluded(row_record(_row("x", "X", author="system"), "LIVEBOARD"), ["system"])
    assert not excluded(row_record(_row("x", "X", author="anuj.seth"), "LIVEBOARD"), ["system"])


def test_a_table_is_in_scope_only_through_content_in_scope():
    in_scope = _row("t1", "employee_skills", "ONE_TO_ONE_LOGICAL",
                    deps={"LOGICAL_TABLE": [{"id": "model1"}], "PINBOARD_ANSWER_BOOK": [{"id": "lb9"}]})
    unrelated = _row("t2", "other", "ONE_TO_ONE_LOGICAL", deps={"QUESTION_ANSWER_BOOK": [{"id": "a7"}]})
    no_deps = _row("t3", "lonely", "ONE_TO_ONE_LOGICAL", deps={})
    hits, truncated = tables_in_scope([in_scope, unrelated, no_deps], {"model1"})
    assert [h["metadata_name"] for h in hits] == ["employee_skills"] and truncated == []


def test_incomplete_dependents_are_reported_not_hidden():
    busy = _row("t4", "SnowFlake", "ONE_TO_ONE_LOGICAL", deps={"QUESTION_ANSWER_BOOK": [{"id": "z"}]},
                last=False)
    hits, truncated = tables_in_scope([busy], {"model1"})
    assert hits == [] and truncated == ["SnowFlake"]


# ── reading grants and deciding what to add ─────────────────────────────────────────────────

def _perm(*entries):
    return {"principal_permission_info": [{"principal_type": "USER_GROUP", "principal_permissions": [
        {"principal_id": pid, "principal_name": name, "permission": lvl} for pid, name, lvl in entries]}]}


def test_current_level_reads_only_the_group_in_question():
    p = _perm(("g1", "zz sweep target", "READ_ONLY"), ("g2", "zz sweep bystander", "MODIFY"))
    assert current_level(p, "g1", "zz sweep target") == "READ_ONLY"
    assert current_level(p, "gx", "Zz Sweep Bystander") == "MODIFY"      # by name, any case
    assert current_level({}, "g1", "zz sweep target") == "NO_ACCESS"


@pytest.mark.parametrize("current,wanted,adds", [
    ("NO_ACCESS", "MODIFY", True), ("READ_ONLY", "MODIFY", True), ("MODIFY", "MODIFY", False),
    ("NO_ACCESS", "READ_ONLY", True), ("READ_ONLY", "READ_ONLY", False),
    ("MODIFY", "READ_ONLY", False),          # a higher grant is never lowered
])
def test_needs_only_ever_raises(current, wanted, adds):
    assert needs(current, wanted) is adds
