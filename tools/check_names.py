#!/usr/bin/env python3
"""Catch NameErrors in app.py before the operator does.

app.py is one long Streamlit script, so a name defined inside one step's block is NOT in scope in
another — but `python -m py_compile` happily accepts it and the failure only appears when someone
clicks through to that page. That is how `_scan_names`, which lives in the Git Operations block,
reached the TML Validation page and blew up mid-run on 2026-09-23.

This walks the AST and reports any local-looking name (leading underscore, our convention for
step-scoped temporaries) that is USED before it is ever ASSIGNED anywhere earlier in the file.
It is deliberately simple and errs toward silence: it only looks at module level, where the
Streamlit step blocks live, and only at our own `_name` convention.

    tools/check_names.py [file ...]      # exits non-zero if anything is suspect
"""
import ast
import sys
import pathlib


def suspect_names(path):
    tree = ast.parse(pathlib.Path(path).read_text(), filename=str(path))
    assigned_at = {}          # name -> earliest line it is bound
    used_at = []              # (name, line, at_module_level)

    class V(ast.NodeVisitor):
        # Inside a function body the ordering rule does not apply: the call happens later, by
        # which time every module-level def exists. Only code that RUNS as the module executes —
        # which is every Streamlit step block — can use a name before it is bound.
        depth = 0

        def visit_Name(self, node):
            nm = node.id
            if not nm.startswith("_") or nm.startswith("__"):
                return
            if isinstance(node.ctx, (ast.Store,)):
                assigned_at.setdefault(nm, node.lineno)
                assigned_at[nm] = min(assigned_at[nm], node.lineno)
            elif isinstance(node.ctx, ast.Load):
                used_at.append((nm, node.lineno, self.depth == 0))
            self.generic_visit(node)

        def visit_FunctionDef(self, node):
            # the function's OWN name is a binding too, as are its parameters
            if node.name.startswith("_"):
                assigned_at.setdefault(node.name, node.lineno)
                assigned_at[node.name] = min(assigned_at[node.name], node.lineno)
            for a in (list(node.args.args) + list(node.args.kwonlyargs)
                      + list(getattr(node.args, "posonlyargs", []))):
                if a.arg.startswith("_"):
                    assigned_at.setdefault(a.arg, node.lineno)
            if node.args.vararg and node.args.vararg.arg.startswith("_"):
                assigned_at.setdefault(node.args.vararg.arg, node.lineno)
            if node.args.kwarg and node.args.kwarg.arg.startswith("_"):
                assigned_at.setdefault(node.args.kwarg.arg, node.lineno)
            self.depth += 1
            self.generic_visit(node)
            self.depth -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            if node.name.startswith("_"):
                assigned_at.setdefault(node.name, node.lineno)
            self.generic_visit(node)

        def visit_Import(self, node):
            for a in node.names:
                nm = (a.asname or a.name).split(".")[0]
                if nm.startswith("_"):
                    assigned_at.setdefault(nm, node.lineno)
            self.generic_visit(node)

        def visit_ImportFrom(self, node):
            for a in node.names:
                nm = a.asname or a.name
                if nm.startswith("_"):
                    assigned_at.setdefault(nm, node.lineno)
            self.generic_visit(node)

        def visit_Lambda(self, node):
            for a in (list(node.args.args) + list(node.args.kwonlyargs)):
                if a.arg.startswith("_"):
                    assigned_at.setdefault(a.arg, node.lineno)
            self.generic_visit(node)

        def visit_ExceptHandler(self, node):
            if node.name and node.name.startswith("_"):
                assigned_at.setdefault(node.name, node.lineno)
            self.generic_visit(node)

        def visit_comprehension(self, node):
            self.generic_visit(node)

    V().visit(tree)
    bad = []
    for nm, line, module_level in used_at:
        first = assigned_at.get(nm)
        if first is None:
            bad.append((nm, line, None))
        elif module_level and first > line:
            bad.append((nm, line, first))
    return sorted(set(bad))


def main():
    files = sys.argv[1:] or ["app.py"]
    total = 0
    for f in files:
        for nm, line, first in suspect_names(f):
            where = "never assigned" if first is None else f"first assigned at line {first}"
            print(f"{f}:{line}: `{nm}` used here but {where}")
            total += 1
    if total:
        print(f"\n{total} suspect name use(s). Each is a NameError waiting for whoever "
              f"clicks that step.")
        return 1
    print(f"no suspect names in {', '.join(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
