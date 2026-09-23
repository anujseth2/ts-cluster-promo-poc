#!/usr/bin/env python3
"""Scaffold the handling of a ThoughtSpot error we don't yet recognise.

The slow part of adding an error is never the typing. It is noticing a new shape exists, pinning
the VERBATIM bytes so a test can't drift from reality, and working out which fragment of the
message is stable enough to match on. This does those three, and stops exactly where judgement
starts: it will not write the "what to do about it" line, because that comes from a live
experiment, not from the text. Every time it has been guessed in this project it was wrong.

Usage
  tools/new_error.py --bundle ~/Downloads/re.zip        # debug bundle (or a directory of logs)
  tools/new_error.py --message "Deleted columns ..."    # one pasted message
  pbpaste | tools/new_error.py                          # from the clipboard
  ... --add-to-corpus                                   # also append the new shapes to the corpus

Stdlib only, runs on /usr/bin/python3 (3.9).
"""
import argparse
import json
import pathlib
import re
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
CORPUS = ROOT / "tests" / "corpus" / "validate_errors.jsonl"

from services.import_diagnostics import classify_import_errors, _clean  # noqa: E402

# Identifiers worth pulling out of a message, in the order a human would notice them.
_IDENT = [
    ("bolded object",   re.compile(r"<b>([^<]+)</b>")),
    ("table::column",   re.compile(r"\b([A-Za-z0-9_\-. ]+?)\s*::\s*([A-Za-z0-9_\-. ]+)\b")),
    ("db.schema.table", re.compile(r"\b(\w+(?:\.\w+){2,})\b")),
    ("guid",            re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                                   r"[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("quoted name",     re.compile(r"[\"“]([^\"”]{2,60})[\"”]")),
    ("list item",       re.compile(r"<li>([^<]+)</li>")),
]


def _messages_from_bundle(path):
    """Every distinct non-OK message in a debug bundle (.zip) or a logs directory."""
    p = pathlib.Path(path).expanduser()
    if not p.exists():
        sys.exit(f"no such bundle: {p}\n"
                 "give the debug-bundle .zip, a logs/ directory, or a validate_runs.jsonl")
    texts = []
    if p.is_dir():
        texts = [f.read_text(errors="replace") for f in p.glob("**/validate_runs.jsonl")]
    elif p.suffix.lower() == ".zip":
        with zipfile.ZipFile(p) as z:
            texts = [z.read(n).decode("utf-8", "replace") for n in z.namelist()
                     if n.endswith("validate_runs.jsonl")]
    else:
        texts = [p.read_text(errors="replace")]
    out = {}
    for t in texts:
        for line in t.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            for r in rec.get("results") or []:
                st = (r.get("status") or "").upper()
                if st != "OK" and r.get("error"):
                    out.setdefault(r["error"].strip(), {
                        "name": r.get("name") or "unknown", "type": r.get("type"),
                        "status": st, "error": r["error"].strip(),
                        "first_seen": rec.get("ts", "")})
    return list(out.values())


def _stable_prefix(msg):
    """The part of the message a pattern should anchor on: the leading prose, before the first
    identifier. Anchoring here is what keeps a rule working when the specific names change."""
    cleaned = _clean(msg)
    first = cleaned.split("\n")[0].strip()
    first = re.split(r"[:\-–]\s", first)[0].strip()
    # drop a trailing name in quotes or brackets so the anchor stays generic
    first = re.sub(r"\s*[\"“][^\"”]+[\"”]\s*$", "", first).strip(" .")
    return first


def _anchor_pattern(anchor):
    """A regex for the anchor that tolerates whitespace drift: escape the specials, then let any
    run of spaces match any run of whitespace. Platform messages reflow between releases, and a
    pattern that breaks on a double space is a pattern that silently stops matching."""
    return r"\s+".join(re.escape(w) for w in anchor.split() if w)


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return "_".join(s.split("_")[:6]) or "new_error"


def _identifiers(msg):
    found = []
    for label, pat in _IDENT:
        for m in pat.findall(msg):
            val = "::".join(m) if isinstance(m, tuple) else m
            val = val.strip()
            if val and (label, val) not in found:
                found.append((label, val))
    return found


def scaffold(rec):
    msg = rec["error"]
    anchor = _stable_prefix(msg)
    slug = _slug(anchor)
    idents = _identifiers(msg)
    kind = slug if len(slug) < 40 else slug[:40]
    lines = []
    A = lines.append
    A("=" * 78)
    A(f"UNHANDLED  [{rec.get('status','ERROR')}]  first seen {rec.get('first_seen') or '(now)'}")
    A("-" * 78)
    A("VERBATIM:")
    A("  " + repr(msg))
    A("")
    A("RENDERS AS:")
    for ln in _clean(msg).split("\n"):
        A("  | " + ln)
    A("")
    A(f"SUGGESTED ANCHOR (stable prose, not the names): {anchor!r}")
    if idents:
        A("IDENTIFIERS IT CARRIES (these are what the operator needs told back):")
        for label, val in idents[:12]:
            A(f"  - {label:16} {val}")
    else:
        A("IDENTIFIERS: none — this message names nothing, so it may legitimately stay")
        A("  unclassified and be handled by the static detectors / per-file isolation.")
    A("")
    A("--- 1. services/import_diagnostics.py: regex, near the other patterns ---")
    A(f'_{kind.upper()} = re.compile(r"{_anchor_pattern(anchor)}(.*)$", re.I | re.S)')
    A("")
    A("--- 2. services/import_diagnostics.py: in classify_import_errors ---")
    A(f"        _m = _{kind.upper()}.search(msg)")
    A("        if _m:")
    A("            matched = True")
    A("            # TODO pull the identifiers above out of _m.group(1) and name them here")
    A(f'            findings.append({{"kind": "{kind}", "object": r.get("name"),')
    A('                             "error": msg.strip()})')
    A("")
    A("--- 3. services/import_diagnostics.py: finding_key, so passes dedupe ---")
    A(f'    if k == "{kind}":')
    A('        return (k, obj, ...)   # the identity of this finding')
    A("")
    A("--- 4. services/import_diagnostics.py: _ERROR_RULES, plain language ---")
    A(f'    (re.compile(r"{_anchor_pattern(anchor)}", re.I),')
    A('     lambda m: ("TODO headline: name the object and say what is blocked",')
    A('                "TODO action: VERIFY THIS ON A CLUSTER FIRST. What actually clears it?")),')
    A("")
    A("--- 5. app.py: render it, and add the kind to _handled_kinds ---")
    A("    (a new kind falls through to Other validation errors until you give it a section)")
    A("")
    A("--- 6. tests/test_import_diagnostics.py: pin the VERBATIM bytes ---")
    A(f"def test_{kind}_is_named_not_unknown():")
    A("    from services.import_diagnostics import classify_import_errors")
    A(f"    msg = {msg!r}")
    A('    found = classify_import_errors([{"name": "unknown",')
    A(f'                                     "status": "{rec.get("status","ERROR")}",')
    A('                                     "error": msg}])')
    A(f'    assert [f["kind"] for f in found] == ["{kind}"]')
    A("    # assert the identifiers above come out")
    A("")
    A("REMINDER: step 4's action line is the one thing this cannot write for you. Confirm the")
    A("remedy against a real cluster before wording it — a confident wrong action is worse than")
    A("raw platform text, because the operator acts on it.")
    return "\n".join(lines)


def _ui_sections():
    """Kinds app.py renders in their own section. Those explain themselves in context, so a
    missing plain-language rule is cosmetic for them. Read from app.py rather than duplicated
    here, so this cannot drift into reporting a problem that was fixed."""
    try:
        src = (ROOT / "app.py").read_text()
        block = re.search(r"_handled_kinds\s*=\s*\{(.*?)\}", src, re.S).group(1)
        return set(re.findall(r'"([a-z_]+)"', block))
    except Exception:
        return set()


def _report_gaps():
    """Classified is only half the job. A kind with no _ERROR_RULES entry still reaches the
    operator as raw platform prose, which is the thing the corpus test cannot see."""
    if not CORPUS.exists():
        sys.exit(f"no corpus at {CORPUS}")
    rows = [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]
    by_kind = {}
    for rec in rows:
        for f in classify_import_errors([{"name": rec.get("name") or "unknown",
                                          "status": rec.get("status") or "ERROR",
                                          "error": rec["error"]}]):
            k = f["kind"]
            has = False   # the hint layer is gone; a kind is served by its UI section
            e = by_kind.setdefault(k, {"n": 0, "friendly": False, "sample": rec["error"]})
            e["n"] += 1
            e["friendly"] = e["friendly"] or has
    sections = _ui_sections()
    # A kind only reaches the operator as raw prose when it has NO plain-language rule AND no
    # section of its own to explain it. Anything with a section is already answered in context.
    missing = {k: v for k, v in by_kind.items()
               if not v["friendly"] and k != "other" and k not in sections}
    cosmetic = {k: v for k, v in by_kind.items()
                if not v["friendly"] and k in sections}
    print(f"{len(rows)} real message(s), {len(by_kind)} kind(s).\n")
    for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]["n"]):
        if v["friendly"]:
            note = "plain-language rule"
        elif k in sections:
            note = "own UI section (explains itself)"
        else:
            note = "RAW platform text  <-- gap"
        print(f"  {k:32} {v['n']:4}  {note}")
    if missing:
        print(f"\n{len(missing)} kind(s) reach the operator as RAW platform text, "
              f"covering {sum(v['n'] for v in missing.values())} real message(s):")
        for k, v in sorted(missing.items(), key=lambda kv: -kv[1]["n"]):
            print(f"\n  {k}  ({v['n']} message(s))")
            print(f"    e.g. {v['sample'][:150]}")
        print("\nEither add an _ERROR_RULES entry or give the kind a section in app.py.")
        print("VERIFY the remedy on a cluster before wording it.")
    else:
        print("\nNo kind reaches the operator as raw text.")
    if cosmetic:
        print(f"\n({len(cosmetic)} kind(s) have no plain-language rule but do have their own "
              f"section, so this is cosmetic: "
              + ", ".join(sorted(cosmetic)) + ")")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bundle", help="debug bundle .zip, a logs directory, or a jsonl file")
    ap.add_argument("--message", help="one pasted error message")
    ap.add_argument("--add-to-corpus", action="store_true",
                    help="append newly seen messages to tests/corpus/validate_errors.jsonl")
    ap.add_argument("--all", action="store_true",
                    help="show every message, not just the unhandled ones")
    ap.add_argument("--gaps", action="store_true",
                    help="list finding kinds in the corpus that classify but still have NO "
                         "plain-language rule, so the operator sees raw platform text")
    args = ap.parse_args()

    if args.gaps:
        return _report_gaps()

    if args.bundle:
        recs = _messages_from_bundle(args.bundle)
    elif args.message:
        recs = [{"name": "unknown", "status": "ERROR", "error": args.message.strip(),
                 "first_seen": ""}]
    elif not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if not raw:
            ap.error("nothing on stdin")
        recs = [{"name": "unknown", "status": "ERROR", "error": raw, "first_seen": ""}]
    else:
        ap.error("give --bundle, --message, or pipe a message on stdin")

    known = set()
    if CORPUS.exists():
        known = {json.loads(l)["error"] for l in CORPUS.read_text().splitlines() if l.strip()}

    unhandled, already = [], 0
    for rec in recs:
        found = classify_import_errors([{"name": rec.get("name") or "unknown",
                                         "status": rec.get("status") or "ERROR",
                                         "error": rec["error"]}])
        is_other = (not found) or any(f["kind"] == "other" for f in found)
        if is_other:
            unhandled.append(rec)
            continue
        already += 1
        if args.all:
            print(f"[handled: {found[0]['kind']}] {rec['error'][:70]!r}")

    print(f"\n{len(recs)} message(s) in, {already} already handled, "
          f"{len(unhandled)} unhandled.\n")
    for rec in unhandled:
        print(scaffold(rec))
        print()

    if args.add_to_corpus and unhandled:
        new = [r for r in unhandled if r["error"] not in known]
        if new:
            with open(CORPUS, "a") as fh:
                for r in new:
                    fh.write(json.dumps({"name": r.get("name") or "unknown",
                                         "type": r.get("type"),
                                         "status": r.get("status") or "ERROR",
                                         "error": r["error"],
                                         "first_seen": r.get("first_seen") or "unrecorded"}) + "\n")
            print(f"Appended {len(new)} message(s) to {CORPUS.relative_to(ROOT)}.")
            print("The corpus test will now FAIL until they classify — that is the point.")
        else:
            print("Nothing new for the corpus; these are already in it.")


if __name__ == "__main__":
    main()
