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

import pytest

from services.import_diagnostics import classify_import_errors

CORPUS = pathlib.Path(__file__).parent / "corpus" / "validate_errors.jsonl"

# Ratchet. Raise as coverage improves; never lower it to go green.
MIN_COVERAGE = 1.0


def _corpus():
    if not CORPUS.exists():
        pytest.skip(f"no corpus at {CORPUS}")
    return [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]


def _classify(rec):
    return classify_import_errors([{"name": rec.get("name") or "unknown",
                                    "type": rec.get("type"),
                                    "status": "ERROR",
                                    "error": rec["error"]}])


def test_corpus_is_present_and_real():
    rows = _corpus()
    assert len(rows) >= 33, "corpus shrank — messages should only ever be added"
    assert all(r.get("error") and r.get("first_seen") for r in rows)


def test_every_real_error_classifies():
    rows = _corpus()
    unmatched = [r for r in rows
                 if any(f["kind"] == "other" for f in _classify(r))]
    coverage = (len(rows) - len(unmatched)) / len(rows)
    detail = "\n".join(f"  [{r['first_seen']}] {r['error'][:200]}" for r in unmatched)
    assert coverage >= MIN_COVERAGE, (
        f"classifier coverage {coverage:.0%} is below the {MIN_COVERAGE:.0%} floor. "
        f"{len(unmatched)} message(s) would render as an unactionable 'unknown':\n{detail}")


def test_classified_findings_name_an_object():
    # A finding the reviewer can't tie to an object is barely better than "unknown". Every
    # classified finding must name the thing to go fix — a table, a column, a formula or a viz.
    rows = _corpus()
    anonymous = []
    for r in rows:
        for f in _classify(r):
            if f["kind"] == "other":
                continue
            named = any(f.get(k) for k in ("column", "table", "tables", "formula",
                                           "formulas", "vizzes", "columns", "model"))
            obj = (f.get("object") or "").strip().lower()
            if not named and obj in ("", "unknown", "none"):
                anonymous.append((f["kind"], r["error"][:120]))
    assert not anonymous, f"findings that name nothing actionable: {anonymous}"
