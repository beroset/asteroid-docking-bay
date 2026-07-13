# SPDX-License-Identifier: GPL-3.0-only
"""Wiring integrity of the served page: handlers, element ids, API paths.

Three UI bugs shipped that no endpoint test could catch: a toast() call
targeting an element that was never in the page (killed the screenshot
action), a menu item wired to a dead confirm-reveal instead of the action
(14 power-off clicks did nothing), and both were found by humans clicking.
These tests read the template the way the browser will."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from asteroid_docking_bay.webtemplate import _WEB_TEMPLATE

JS = max(re.findall(r"<script>(.*?)</script>", _WEB_TEMPLATE, re.S), key=len)
DEFINED_FUNCS = set(re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", JS))


def _handler_names():
    """Every function name the page wires to a click, wherever it's wired:
    static onclick attributes, onclick inside JS-built HTML strings, and the
    menu-item builder's action argument."""
    names = set()
    # onclick="fn(..." — in raw HTML and in template-literal-built HTML
    names |= set(re.findall(r'onclick=\\?"\$?\{?([A-Za-z_]\w*)\(', _WEB_TEMPLATE))
    # mi(cls, label, "fn(...)") — the action is a string that becomes onclick
    names |= set(re.findall(r"mi\([^,]+,[^,]+,\s*[`'\"]([A-Za-z_]\w*)\(", JS))
    return names


def test_every_click_handler_is_defined():
    missing = sorted(_handler_names() - DEFINED_FUNCS)
    assert not missing, (
        f"onclick wires to undefined function(s): {missing} — "
        "clicking these does nothing (the doHalt/power-off bug class)")


def test_every_literal_element_id_exists():
    wanted = set(re.findall(r"getElementById\('([A-Za-z][\w-]*)'\)", JS))
    static_ids = set(re.findall(r'id="([\w-]+)"', _WEB_TEMPLATE))
    # ids the JS itself creates at runtime (t.id='toast' pattern)
    created = set(re.findall(r"\.id\s*=\s*'([\w-]+)'", JS))
    missing = sorted(wanted - static_ids - created)
    assert not missing, (
        f"getElementById targets that exist nowhere: {missing} — "
        "the missing-#toast bug class (handler throws, action dies)")


def _route_patterns():
    """Every route the server registers: the _JSON_ROUTES table (imported —
    it IS the contract) plus the explicit @app routes in webapp.py."""
    from asteroid_docking_bay.webapp import _JSON_ROUTES
    src = (Path(__file__).resolve().parent.parent
           / "asteroid_docking_bay" / "webapp.py").read_text()
    paths = [spec[1] for spec in _JSON_ROUTES]
    paths += re.findall(r'@app\.(?:get|post)\("([^"]+)"\)', src)
    return [(p, re.compile("^" + re.sub(r"<[^>]+>", "[^/]+", p) + "$"))
            for p in paths]


def test_every_js_api_call_hits_a_route():
    routes = _route_patterns()
    # fetch('/api/x/y/'+... and EventSource('/api/...') — take the literal
    # prefix and check it can still complete into some registered route.
    calls = re.findall(r"(?:fetch|EventSource)\('(/api/[^']*)'", JS)
    unmatched = []
    for prefix in calls:
        probe = prefix + ("X" if prefix.endswith("/") else "")
        # complete the probe with path segments until it matches or gives up
        ok = False
        for _, rx in routes:
            candidate = probe
            for _ in range(4):
                if rx.match(candidate):
                    ok = True
                    break
                candidate += "/X"
            if ok:
                break
        if not ok:
            unmatched.append(prefix)
    assert not unmatched, (
        f"JS calls API paths no route serves: {unmatched} — "
        "route/JS drift (renamed or removed endpoint)")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_served_js_parses():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(JS)
        path = f.name
    r = subprocess.run(["node", "--check", path], capture_output=True,
                       text=True)
    assert r.returncode == 0, f"served JS has a syntax error:\n{r.stderr}"
