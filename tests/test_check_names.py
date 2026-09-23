"""L1: the NameError checker for app.py.

app.py is one long Streamlit script, so a name bound inside one step's block is not in scope in
another — and py_compile accepts it happily, so the failure only appears when someone clicks
through to that page. `_scan_names` lives in the Git Operations block and was used on the TML
Validation page; it blew up mid-run in front of the user on 2026-09-23.
"""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "check_names", pathlib.Path(__file__).parent.parent / "tools" / "check_names.py")
cn = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cn)


def _write(tmp_path, src):
    f = tmp_path / "sample.py"
    f.write_text(src)
    return f


def test_it_catches_the_scan_names_shape(tmp_path):
    # THE bug: used at module level in one block, bound later in another.
    f = _write(tmp_path, "if a:\n    x = f(_scan_names)\nif b:\n    _scan_names = {1}\n")
    found = cn.suspect_names(f)
    assert [n for n, _l, _a in found] == ["_scan_names"]


def test_a_name_never_bound_at_all_is_caught(tmp_path):
    f = _write(tmp_path, "y = _never_defined + 1\n")
    assert [n for n, _l, _a in cn.suspect_names(f)] == ["_never_defined"]


def test_a_function_called_before_its_def_is_fine(tmp_path):
    # Inside a function body the ordering rule must not apply: by call time everything exists.
    f = _write(tmp_path, "def _a():\n    return _b()\ndef _b():\n    return 1\n_a()\n")
    assert cn.suspect_names(f) == []


def test_bindings_the_checker_must_understand(tmp_path):
    f = _write(tmp_path, (
        "import os as _os\n"
        "from json import loads as _loads\n"
        "def _fn(_arg, *_args, **_kw):\n"
        "    return _arg, _args, _kw\n"
        "class _K: pass\n"
        "for _i in range(3):\n"
        "    print(_i)\n"
        "try:\n"
        "    pass\n"
        "except ValueError as _e:\n"
        "    print(_e)\n"
        "with open('x') as _fh:\n"
        "    print(_fh)\n"
        "print(_os, _loads, _fn, _K)\n"))
    assert cn.suspect_names(f) == [], "no false positives on ordinary bindings"


def test_app_py_is_clean():
    app = pathlib.Path(__file__).parent.parent / "app.py"
    found = cn.suspect_names(app)
    assert found == [], f"NameError(s) waiting in app.py: {found}"
