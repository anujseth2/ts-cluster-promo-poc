"""L1: services/feedback_replace.replace_undo — put a model's obj_id back when the import that
should have replaced it failed."""
from services.feedback_replace import replace_undo

_PREPPED = [{"name": "Sales Model", "obj_id": "sales_model", "old_guid": "g-old", "real_deps": []}]


class _Target:
    def __init__(self, rename_ok=True, resolves_to="g-old"):
        self.rename_ok, self.resolves_to, self.renames = rename_ok, resolves_to, []

    def update_obj_ids(self, mappings):
        self.renames.extend(mappings)
        if not self.rename_ok:
            raise RuntimeError("update-obj-id refused")
        return True

    def find_by_obj_id(self, obj_id):
        return self.resolves_to


def test_undo_renames_the_old_model_back_and_confirms_it():
    t = _Target()
    assert replace_undo(t, _PREPPED) == [{"model": "Sales Model", "restored": True, "detail": ""}]
    assert t.renames == [{"identifier": "g-old", "new_obj_id": "sales_model"}]


def test_undo_does_not_trust_a_rename_it_cannot_read_back():
    rep = replace_undo(_Target(resolves_to=None), _PREPPED)
    assert rep[0]["restored"] is False and "does not resolve" in rep[0]["detail"]


def test_undo_reports_a_refused_rename_instead_of_raising():
    rep = replace_undo(_Target(rename_ok=False), _PREPPED)
    assert rep[0]["restored"] is False and "refused" in rep[0]["detail"]
