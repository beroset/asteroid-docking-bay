# SPDX-License-Identifier: GPL-3.0-only
"""Static integrity of the package: no undefined names, no unused imports.

These exist because both failure classes have shipped: a mechanical move
once silently truncated a function (compiled fine, failed at runtime), and
refactors leave import lists lying about what a module needs. Every module
is checked on every test run, so a stale caller or a half-finished move
fails CI instead of a fleet.
"""

import ast
import builtins
import symtable
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "asteroid_docking_bay"
MODULES = sorted(PKG.glob("*.py"))
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                 "__package__", "__spec__", "__loader__",
                                 # interpreter-generated annotation scaffolding
                                 "__annotations__",
                                 "__conditional_annotations__"}


def module_ids(paths):
    return [p.name for p in paths]


def _module_level_names(tree: ast.Module) -> set:
    """Names a module defines at top level (defs, classes, assignments,
    imports) — what a global reference legitimately resolves to."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for t in targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try)):
            # conditional defs (rare) — recurse one level
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.ClassDef)):
                    names.add(sub.name)
                elif isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for a in sub.names:
                        names.add((a.asname or a.name).split(".")[0])
    return names


def _global_refs(table: symtable.SymbolTable, out: set) -> None:
    """Every symbol any scope resolves as a global reference."""
    for sym in table.get_symbols():
        if sym.is_referenced() and sym.is_global():
            out.add(sym.get_name())
    for child in table.get_children():
        _global_refs(child, out)


@pytest.mark.parametrize("path", MODULES, ids=module_ids(MODULES))
def test_no_undefined_names(path):
    src = path.read_text()
    tree = ast.parse(src)
    defined = _module_level_names(tree) | BUILTINS
    refs: set = set()
    _global_refs(symtable.symtable(src, path.name, "exec"), refs)
    # Names defined later at module level count (Python resolves at call
    # time), so only names in no namespace at all are failures.
    undefined = sorted(refs - defined)
    assert not undefined, (
        f"{path.name} references undefined name(s): {undefined} — "
        "a moved or renamed callee left a stale caller behind")


@pytest.mark.parametrize("path", MODULES, ids=module_ids(MODULES))
def test_no_unused_imports(path):
    src = path.read_text()
    tree = ast.parse(src)
    imported: dict = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue   # `from __future__ import annotations` has no use site
            for a in node.names:
                imported[a.asname or a.name] = node.lineno
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value,
                                                            ast.Name):
            used.add(node.value.id)
        # string annotations reference names the walker can't see
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for name in imported:
                if name in node.value:
                    used.add(name)
    unused = sorted(n for n in imported if n not in used)
    assert not unused, (
        f"{path.name} imports but never uses: {unused} — "
        "either a leftover from a refactor or a missing call")
