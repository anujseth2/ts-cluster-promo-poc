"""Classifier coverage against REAL ThoughtSpot validation errors.

tests/corpus/validate_errors.jsonl holds every distinct failure message the tool has actually
received from a cluster (captured live in logs/validate_runs.jsonl, deduped, oldest first). It is
the honest measure of how much of what the platform says we can act on: a message that classifies
as "other" is a message the reviewer sees as an unactionable "unknown" on screen, which is exactly
how the GSK 2026-09-11 demo broke.

Two rules keep this useful rather than decorative:

  1. Coverage may not regress. The floor below is a ratchet — raise it when new patterns land,
     never lower it to make a failing run pass.
  2. Every new field failure gets added here. Download the debug bundle from the validation page,
     append any new message, and the classifier is judged against it from then on.
"""

import json
import pathlib
import re

import pytest

from services.import_diagnostics import classify_import_errors

CORPUS = pathlib.Path(__file__).parent / "corpus" / "validate_errors.jsonl"

# Ratchet. Raise as coverage improves; never lower it to go green.
MIN_COVERAGE = 1.0

# Extractable identifiers: a bolded object, a dotted FQN, a table::column, or a guid. A message
# that contains NONE of these genuinely names nothing ("Schema validation failed.") and is allowed
# to stay unclassified — that is what the static detectors and per-file isolation are for. A message
# that DOES name something must classify, because the name is the actionable part and dropping it on
# the floor is how an error reaches the operator as an unactionable "unknown".
_NAMES_SOMETHING = re.compile(
    r"<b>[^<]+</b>"                                   # bolded object
    r"|\b\w+(?:\.\w+){2,}\b"                          # db.schema.table(.col)
    r"|\w+\s*::\s*\w+"                                # table::column
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",   # guid
    re.I)


def _corpus():
    if not CORPUS.exists():
        pytest.skip(f"no corpus at {CORPUS}")
    return [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]


def _classify(rec):
    # Replay the REAL status. A WARNING must not be reported as work to do.
    return classify_import_errors([{"name": rec.get("name") or "unknown",
                                    "type": rec.get("type"),
                                    "status": rec.get("status") or "ERROR",
                                    "error": rec["error"]}])


def test_corpus_is_present_and_real():
    rows = _corpus()
    assert len(rows) >= 95, "corpus shrank — messages should only ever be added"
    assert all(r.get("error") and r.get("first_seen") for r in rows)


def test_every_error_that_names_something_classifies():
    rows = _corpus()
    unmatched = [r for r in rows if any(f["kind"] == "other" for f in _classify(r))]
    # Only a message that names nothing may go unclassified.
    wrongly = [r for r in unmatched if _NAMES_SOMETHING.search(r["error"])]
    detail = "\n".join(f"  [{r['first_seen']}] {r['error'][:200]}" for r in wrongly)
    assert not wrongly, (
        f"{len(wrongly)} message(s) name an object but fall into 'other', so the operator sees "
        f"an unactionable error:\n{detail}")
    actionable = (len(rows) - len(unmatched)) / len(rows)
    assert actionable >= 0.95, f"classified {actionable:.0%} of real messages, floor is 95%"


def test_warnings_in_the_corpus_are_never_blocking():
    # Replayed at their real severity, no WARNING may present as an issue to resolve.
    from services.import_diagnostics import blocking
    for rec in _corpus():
        if (rec.get("status") or "").upper() == "WARNING":
            assert blocking(_classify(rec)) == [], \
                f"a WARNING is being reported as blocking: {rec['error'][:120]}"


# Kinds where ThoughtSpot provably sends NO identifier and a static detector supplies it instead:
#   table_zero_columns  — "Attempting to create a table with 0 columns" names no table;
#                         table_cleanup_findings() finds it from the TML.
#   join_unresolved     — the 14540 text can carry an empty table name ("No matches found for
#                         table ."); the same static scan names the disconnected table.
_NAMED_STATICALLY = {"table_zero_columns", "join_unresolved"}
_NAME_KEYS = ("column", "table", "tables", "formula", "formulas", "vizzes", "columns",
              "model", "guids", "objects")


def test_classified_findings_name_an_object():
    # A finding the reviewer can't tie to an object is barely better than "unknown". Every
    # classified finding must name the thing to go fix, unless the platform sent no name at all
    # and a static detector is responsible for supplying it.
    rows = _corpus()
    anonymous = []
    for r in rows:
        for f in _classify(r):
            if f["kind"] == "other" or f["kind"] in _NAMED_STATICALLY:
                continue
            named = any(f.get(k) for k in _NAME_KEYS)
            obj = (f.get("object") or "").strip().lower()
            if not named and obj in ("", "unknown", "none"):
                anonymous.append((f["kind"], r["error"][:120]))
    assert not anonymous, f"findings that name nothing actionable: {anonymous}"


def test_the_nameless_exemption_is_not_a_dumping_ground():
    # Guard the exemption list: a kind earns its place only while the corpus actually contains a
    # nameless instance of it. If ThoughtSpot starts naming every one, the entry must be deleted
    # rather than left behind to silently excuse a future kind that should have been named.
    rows = _corpus()
    for kind in sorted(_NAMED_STATICALLY):
        nameless = any(f["kind"] == kind and not any(f.get(k) for k in _NAME_KEYS)
                       and (f.get("object") or "").strip().lower() in ("", "unknown", "none")
                       for rec in rows for f in _classify(rec))
        assert nameless, (f"'{kind}' is exempt from needing a name, but every instance in the "
                          f"corpus now carries one — remove it from _NAMED_STATICALLY")
