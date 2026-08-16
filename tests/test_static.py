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


def test_compound_shell_commands_are_quoted_whole():
    """Transport.shell() interpolates its argument into a HOST command line
    (`adb -s X shell {cmd}` / `ssh root@ip {cmd}`). So a command containing
    &&, ||, ;, | or a redirect must be quoted as a whole, or the shell splits
    it: the first command runs on the watch and the REST RUNS ON THE HOST.

    This is not hypothetical. provision_wifi() shipped with an unquoted
    `mkdir && mv && chown -R root:root ...` chain; the mkdir ran on the watch,
    the mv ran on the host and failed, and only that failure stopped a
    `chown -R` from running against the host's filesystem. The tell was an
    error message in the host's language from a watch that answers in English.

    Planted-bug: drop the shlex.quote() around that chain and this fails.
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "asteroid_docking_bay"
    metachars = ("&&", "||", ";", "|", ">", "<")
    offenders = []

    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute) and fn.attr == "shell"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            # Already wrapped in shlex.quote(...) — the correct form.
            if (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == "quote"):
                continue
            # Reconstruct the literal parts of the command, ignoring the
            # interpolated values (a filename cannot introduce an operator
            # that matters here — the operators are written in the source).
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                text = arg.value
            elif isinstance(arg, ast.JoinedStr):
                text = "".join(v.value for v in arg.values
                               if isinstance(v, ast.Constant)
                               and isinstance(v.value, str))
            else:
                continue
            # A command the author quoted by hand with embedded double quotes
            # is already safe.
            if text.startswith('"') and text.rstrip().endswith('"'):
                continue
            if any(m in text for m in metachars):
                offenders.append(f"{path.name}:{node.lineno}: {text[:70]}")

    assert not offenders, (
        "compound shell command(s) not quoted whole — these would run their "
        "tail on the HOST:\n  " + "\n  ".join(offenders))


def test_web_template_has_no_raw_newline_escapes():
    """webtemplate's page is a NON-RAW Python string, so a `\\n` written for a
    JS string literal becomes a REAL newline the moment the module is
    imported. That leaves the JS string unterminated and takes the whole UI
    down — not just the feature that added it.

    This has now happened twice, both in a confirm() message, and both times it
    failed ~50 unrelated UI tests instead of pointing at itself.

    The invariant: inside the template there is no single-backslash-n at all.
    A JS newline must be written `\\\\n` so it survives Python's unescaping.
    (An earlier version of this test scanned for real line breaks in the
    SOURCE, where the escape is still two characters — so it passed happily
    against the very bug it was written to catch.)

    Planted-bug: write a single backslash-n anywhere in the template and this
    fails.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "asteroid_docking_bay" / "webtemplate.py").read_text()
    tpl = src[src.index('_WEB_TEMPLATE = """'):]
    hits = []
    for m in re.finditer(r"(?<!\\)\\n", tpl):
        line = tpl[:m.start()].count("\n") + 1
        hits.append(f"line {line}: {tpl[max(0, m.start() - 70):m.start() + 12].splitlines()[-1].strip()[:90]}")
    assert not hits, (
        "single-backslash-n in the template — this becomes a real newline at "
        "import and breaks the JS string containing it:\n  " + "\n  ".join(hits))


def test_install_escalates_exactly_once():
    """install.sh must ask for the password ONCE, in one block up front.

    mo's requirement for the "noob" install: everything set up including udev
    rules, "one sudo block up front, not scattered escalations". Scattered
    `sudo` calls are how an install stops halfway through asking for a
    password the user did not expect, and the older script did not escalate at
    all — it printed the udev and group commands and left them undone, which
    is why fresh installs silently ran in the slow fallback path.

    Counts real invocations only; the word also appears in printed guidance
    for the manual path (--no-root, no-sudo-installed)."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "install.sh").read_text()
    calls = [ln.strip() for ln in src.splitlines()
             if re.search(r"(^|[;&|(]|\bthen\b|\bif\b)\s*sudo\s", ln)
             and not ln.strip().startswith("#")
             and "echo" not in ln]
    assert len(calls) == 1, (
        f"install.sh escalates {len(calls)} times, must be exactly one block: {calls}")
    assert "bash -s" in calls[0], (
        "the single escalation should run one root block, not one command — "
        "otherwise the next privileged step becomes a second password prompt")

    # ...and it must actually DO the two privileged things, not just print them
    assert "udevadm control --reload-rules" in src and "usermod -aG users" in src, (
        "install.sh no longer performs the udev/group setup it exists to do")


def test_no_child_process_inherits_stdin():
    """`adb shell` READS STDIN. A child that inherits it will swallow the
    caller's remaining input — when a-d-b is driven from a script or a
    heredoc, the script then dies half-executed with no error whatsoever.

    Every subprocess this package spawns must close stdin. Planted-bug: drop
    stdin=subprocess.DEVNULL from _run and this fails.
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "asteroid_docking_bay"
    offenders = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "attr", None)
            if name not in ("run", "Popen"):
                continue
            mod = getattr(getattr(fn, "value", None), "id", None)
            if mod != "subprocess":
                continue
            if not any(k.arg == "stdin" for k in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "subprocess call(s) inheriting stdin — adb shell will eat the "
        "caller's input:\n  " + "\n  ".join(offenders))


def test_every_module_is_in_the_architecture_map():
    """docs/ARCHITECTURE.md is the map a new contributor reads first, and mo's
    rule is that it moves in the same commit that structure does.

    It did not. Between 0.5 and 1.0 twenty of thirty-six modules appeared
    without ever being described — including `rpcops`, the op table that the
    container split treats as its security boundary, and `oplock`, the
    cross-process lock. Nothing failed, because no test owned the document.

    A missing entry is not pedantry here: the map is where someone looks to
    find out whether the thing they need already exists, which is exactly how
    duplicated code gets written."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs" / "ARCHITECTURE.md").read_text()
    mods = {p.stem for p in (root / "asteroid_docking_bay").glob("*.py")
            if p.stem != "__init__"}
    missing = sorted(m for m in mods
                     if not re.search(rf"\b{re.escape(m)}\.py\b|`{re.escape(m)}`", doc))
    assert not missing, (
        f"modules absent from docs/ARCHITECTURE.md: {missing} — add them to the "
        f"Layout block in the same commit that adds the module")
