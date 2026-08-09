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


def global_simple():
    """JS that swaps reconcileRows for the plain innerHTML path in render tests."""
    return "\nreconcileRows=function(tb,h){tb.innerHTML=h.join('');};\n"
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


def test_no_function_is_declared_twice():
    """A second `function foo(){}` silently shadows the first — JS hoists the
    LAST declaration, so a stray empty stub after the real one makes the whole
    feature a no-op with no error anywhere. That is exactly how the Dump
    mmcblk0 menu item died: an empty `function doDump(s){}` sat below the real
    one. DEFINED_FUNCS is a set, so the handler-defined test cannot see this."""
    names = re.findall(r"function\s+([A-Za-z_]\w*)\s*\(", JS)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, (
        f"function(s) declared more than once, the later one wins and shadows "
        f"the real body: {dupes}")


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


# Representative status doc exercising render's branches: a mapped on-adb watch
# with a wearable drain result + forced screen, a draining watch with a
# swap-candidate result, an empty port. Enough to run mkthumb/mkstrip/mkbat and
# the charge/drain paths.
_SAMPLE = {
    "version": "test", "thresholds": {"low": 40, "high": 80},
    "drain_floor": 15, "wearable_min_hours": 24,
    "hubs": [{"location": "1-2", "description": "Hub", "hidden": False, "ports": [
        {"port": 1, "codename": "skipjack", "serial": "S1", "slot_loc": "1-2",
         "power": True, "smart": True, "connected": True, "adb": "device",
         "battery": 83, "os": "asteroidos", "screen_forced": True,
         "charge_status": "Charging",
         "geometry": {"round": True, "resolution": "360x360",
                      "width": 360, "height": 360},
         "drain_last": {"est_h": 300, "ts": 1783900000}, "drain": None,
         "charging_active": False, "socket": 1},
        {"port": 2, "codename": "bass", "serial": "S2", "slot_loc": "1-2",
         "power": False, "smart": True, "connected": False, "adb": None,
         "battery": None, "drain": {"active": True, "last_pct": 50, "drain_rate": 0.9},
         "drain_last": {"est_h": 90, "ts": 1783900000}, "socket": 2},
        {"port": 3, "codename": None, "slot_loc": "1-2", "power": False,
         "empty": True, "adb": None, "socket": 3},
        {"port": 4, "codename": "casio", "serial": "S4", "slot_loc": "1-2",
         "power": False, "smart": True, "connected": False, "adb": None,
         "battery": None, "battery_cached": 62, "last_live_ts": 1783900000,
         "drain": None, "drain_last": None, "socket": 4},
    ]}],
}

_DOM_STUBS = r"""
function el(){return{style:{},classList:{add(){},remove(){},contains:()=>false,toggle(){}},
  innerHTML:'',textContent:'',value:'',querySelectorAll:()=>[],querySelector:()=>null,contains:()=>false,
  appendChild(){},removeChild(){},remove(){},setAttribute(){},getAttribute:()=>null,offsetHeight:100,offsetWidth:100};}
global.__h={};
global.document={getElementById:()=>el(),createElement:()=>el(),addEventListener(t,f){global.__h[t]=f;},body:el(),documentElement:el()};
global.window={innerWidth:1200,innerHeight:800,addEventListener(){},open(){},location:{href:''}};
global.fetch=()=>Promise.resolve({json:()=>Promise.resolve({}),text:()=>Promise.resolve('')});
global.EventSource=function(){this.close=function(){}};
global.localStorage={getItem:()=>null,setItem(){}};global.navigator={};
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hole_detection_finds_interior_transparency(tmp_path):
    """holeBoxFromAlpha must return the enclosed screen-cutout box and ignore
    the render's transparent background (border-connected transparency)."""
    import json
    harness = r"""
      function grid(w,h,f){const a=new Uint8Array(w*h);
        for(let y=0;y<h;y++)for(let x=0;x<w;x++)a[y*w+x]=f(x,y);return a;}
      // opaque body, a 2x2 hole at (2,2) in a 6x6 image
      const b1=holeBoxFromAlpha(grid(6,6,(x,y)=>(x>=2&&x<=3&&y>=2&&y<=3)?0:255),6,6);
      // transparent BACKGROUND ring + opaque body + a 1px interior hole at (3,3)
      const b2=holeBoxFromAlpha(grid(7,7,(x,y)=>
        (x===0||y===0||x===6||y===6)?0:((x===3&&y===3)?0:255)),7,7);
      // no interior transparency at all -> null
      const b3=holeBoxFromAlpha(grid(5,5,(x,y)=>(x===0||y===0||x===4||y===4)?0:255),5,5);
      console.log(JSON.stringify({b1,b2,b3}));
      process.exit(0);
    """
    h = tmp_path / "hole.js"
    h.write_text(_DOM_STUBS + JS + "\n" + harness)
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:600]
    out = json.loads(r.stdout)
    b1 = out["b1"]
    assert abs(b1["x"] - 2/6) < 1e-6 and abs(b1["w"] - 2/6) < 1e-6
    b2 = out["b2"]                                    # background ring excluded
    assert abs(b2["x"] - 3/7) < 1e-6 and abs(b2["w"] - 1/7) < 1e-6
    assert out["b3"] is None


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_render_runs_without_throwing(tmp_path):
    """render() must execute against a real-shaped status doc. The parse test
    can't catch a runtime throw — e.g. a helper (mkstrip) referencing a
    render-local const (wearH), which silently surfaced as 'connection error'
    because the throw lands in the status fetch's .catch."""
    import json
    h = tmp_path / "harness.js"
    # Render twice: the first pass is the initial load (firstStatus true), the
    # second exercises the post-load path — the newly-plugged-row flash compares
    # against the serials seen on the first pass.
    h.write_text(_DOM_STUBS + JS + global_simple() +
                 f"\ntry{{const S={json.dumps(_SAMPLE)};render(S);render(S);"
                 f"console.log('RENDER_OK');}}"
                 f"catch(e){{console.error('THREW '+e);process.exit(1);}}\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0 and "RENDER_OK" in r.stdout, (
        f"render() threw when run headless:\n{r.stderr[:600]}")


# Caching DOM: getElementById must return the *same* object across calls so an
# innerHTML written during render is still readable afterwards. The plain
# _DOM_STUBS mint a fresh element each call, which is fine for "did it throw"
# but discards everything render() produced.
_DOM_CAPTURE = _DOM_STUBS.replace(
    "global.document={getElementById:()=>el(),",
    "global.__els={};global.document={getElementById:(i)=>(global.__els[i]=global.__els[i]||el()),")


# A DOM real enough to run reconcileRows itself (the render tests stub it out
# with innerHTML=join, so its own tree logic — the part that dropped the log
# rows — is never otherwise exercised). Models exactly what reconcileRows uses:
# a node whose innerHTML parses top-level <tr> chunks into child nodes,
# .children / .firstElementChild, get/setAttribute, and replaceChildren.
# Layered ON TOP of _DOM_STUBS (loaded first for window/document), overriding
# only createElement so the rest of the template still loads.
_DOM_TREE = r"""
function _node(){return {
  _a:{}, _c:[], _html:'',
  get children(){return this._c;},
  get firstElementChild(){return this._c[0]||null;},
  getAttribute(k){return (k in this._a)?this._a[k]:null;},
  setAttribute(k,v){this._a[k]=v;},
  set innerHTML(h){ this._html=h;
    const parts=h.match(/<tr\b[\s\S]*?<\/tr>/g)||[];
    this._c=parts.map(p=>{const n=_node(); n._html=p;
      const m=p.match(/id="([^"]+)"/); if(m)n._a.id=m[1]; return n;}); },
  get innerHTML(){return this._html;},
  replaceChildren(){ this._c=Array.prototype.slice.call(arguments); }
};}
global.document.createElement=()=>_node();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_held_watch_says_so_in_its_row(tmp_path):
    """The status document has carried `held` per port since the operation lock
    landed, and nothing rendered it: a watch held for a 4-hour dump looked
    completely ordinary, and every action on it was refused only after the
    click — which reads as the UI being broken rather than the watch being busy.

    A wanze run keeps its own pill in the connection column, so it must not be
    doubled up here."""
    import json
    h = tmp_path / "held.js"
    cases = {
        "dump": {"held": {"kind": "dump", "note": "full-disk dump",
                          "since": 1783900000}},
        "wanze": {"held": {"kind": "wanze", "since": 1783900000}},
        "free": {"held": None},
    }
    h.write_text(
        _DOM_STUBS + JS +
        f"\nconst cases={json.dumps(cases)};"
        "\nconst out={};for(const k in cases)out[k]=mkheld(cases[k]);"
        "\nconsole.log(JSON.stringify(out));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "held" in out["dump"] and "dump" in out["dump"], \
        f"a watch held for a dump renders no marker: {out['dump']!r}"
    assert "full-disk dump" in out["dump"], "the reason is not shown to the user"
    assert out["wanze"] == "", "the wanze pill was duplicated by the held badge"
    assert out["free"] == "", "an unheld watch was marked held"

    # And it is actually CALLED from the row, not merely defined — matching
    # "mkheld(p)" alone also matches `function mkheld(p){`, which would let an
    # unwired badge pass. Require a call site outside the definition.
    assert JS.count("mkheld(") >= 2, \
        "mkheld is defined but never called — the badge renders nowhere"
    assert "+mkheld(p)+" in JS, "mkheld is not concatenated into the watch row"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hostile_values_cannot_break_out_of_markup_or_a_click_handler(tmp_path):
    """Not every value rendered here is ours. A watch supplies its own USB
    serial, the icecc scheduler supplies node hostnames, and a Bluetooth device
    supplies its advertised name OVER THE AIR — any radio in range can choose
    it. Those land in attributes and in inline click-handler arguments.

    esc() covers markup. It is NOT enough inside a handler argument, and the
    reason is easy to miss: the HTML parser decodes entities BEFORE the JS
    parser runs, so a quote written as &#39; arrives at JS as a plain quote and
    closes the string anyway. jsq() escapes for JS first, then for HTML."""
    import json
    payloads = [
        "x');alert(1);//",              # close the JS string, run code
        'y" onmouseover="alert(2)',     # close the attribute, add a handler
        "z</b><img src=x onerror=alert(3)>",   # close the element
        "back" + chr(92) + "slash",     # a backslash must not escape the quote
    ]
    h = tmp_path / "esc.js"
    h.write_text(
        _DOM_STUBS + JS +
        f"\nconst payloads={json.dumps(payloads)};"
        "\nconst out=payloads.map(p=>({esc:esc(p), jsq:jsq(p)}));"
        "\nconsole.log(JSON.stringify(out));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    got = json.loads(r.stdout.strip().splitlines()[-1])

    for payload, res in zip(payloads, got):
        # esc(): nothing that can end an attribute or open a tag survives.
        for ch in ('<', '"', "'"):
            assert ch not in res["esc"], (
                f"esc() left {ch!r} in {payload!r} → {res['esc']!r}; it can "
                "break out of the attribute it is rendered into")
        # jsq(): the value must survive as ONE JS string argument. Simulate the
        # browser: HTML-decode the attribute, then check the string still closes
        # exactly where we put its quote.
        # &#39; MUST be decoded here. Leaving it out is what makes this test
        # pass against the very bug it exists to catch: esc() turns ' into
        # &#39;, the browser turns it straight back into ', and the JS string
        # ends there. A simulation that skips this entity proves nothing.
        decoded = (res["jsq"].replace("&quot;", '"').replace("&lt;", "<")
                   .replace("&#39;", "'").replace("&amp;", "&"))
        depth, i = 0, 0
        while i < len(decoded):                 # walk the JS string literal
            c = decoded[i]
            if c == chr(92):
                i += 2                          # escaped char, skip both
                continue
            assert c != "'", (
                f"jsq() let {payload!r} close its JS string early → "
                f"{res['jsq']!r}; the rest would be executed as code")
            i += 1
        assert depth == 0


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reconcile_keeps_both_the_watch_row_and_its_log_row(tmp_path):
    """Each watch entry is two <tr>s in one html string: the visible row and its
    hidden log row carrying id="log-<slot>". reconcileRows kept only
    firstElementChild, dropping every log row — so the flash / onboard streams
    had no box to write to and doFl/doRemap returned at once. Both must survive,
    and a re-render with identical html must REUSE the same nodes (the image
    reload the reconcile exists to prevent)."""
    entry = ('`<tr class="wr" id="wr-1-2:1"><td>x</td></tr>'
             '<tr class="lr" id="lr-1-2:1"><td><div id="log-1-2:1"></div></td></tr>`')
    h = tmp_path / "reconcile.js"
    h.write_text(
        _DOM_STUBS + JS + _DOM_TREE +
        "\nconst tb=_node();"
        f"\nconst html={entry};"
        "\nreconcileRows(tb, [html]);"
        "\nconst first=tb.children.map(n=>n._html).join('');"
        "\nconst ids1=tb.children.map(n=>n._a.id);"
        "\nreconcileRows(tb, [html]);"          # second pass, identical html
        "\nconst reused=tb.children.map(n=>n._html).join('')===first;"
        "\nconsole.log(JSON.stringify({ids:ids1,html:first,reused:reused}));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ids"] == ["wr-1-2:1", "lr-1-2:1"], (
        f"the log row was dropped — flash/onboard have no box to write to: {out['ids']}")
    assert 'id="log-1-2:1"' in out["html"], "the log box never reached the DOM"
    assert out["reused"], "an unchanged row was rebuilt — its thumbnail would reload"


def test_the_dead_menu_parameter_is_gone():
    """needPwr was computed on every row, threaded through menuExecute and
    declared in its signature — and never read. It survived because a test
    asserted its VALUE in the rendered markup, so the argument looked covered
    while validating a no-op; the behaviour it was named for ("refresh powers
    an off switchable port") lives in the backend onboard op.

    It also made the menu's arity a moving target: three separate tests had to
    chase it as arguments were added around it."""
    assert "needPwr" not in _WEB_TEMPLATE, "the dead parameter is back"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_orbit_section_renders_rows_and_controls(tmp_path):
    """The virtual Orbit hub renders its own section: a Launch-by-IP box, a WiFi
    badge for a reachable member, an offline (dimmed) row for an unreachable one,
    the codename opening the Control Center, and de-orbit wiring keyed on serial —
    all without the power/port/smart controls a physical row has."""
    import json
    doc = {
        "version": "test", "thresholds": {"low": 40, "high": 80},
        "drain_floor": 15, "wearable_min_hours": 24,
        "hubs": [
            {"location": "1-2", "description": "Hub", "hidden": False, "ports": [
                {"port": 1, "codename": "skipjack", "serial": "S1", "slot_loc": "1-2",
                 "power": True, "smart": True, "connected": True, "adb": "device",
                 "battery": 83, "socket": 1}]},
            {"location": "orbit", "description": "over the air", "virtual": True,
             "hidden": False, "ports": [
                {"codename": "catfish", "serial": "CAT", "orbit": True, "empty": False,
                 "ip": "10.0.0.9", "adb": "ssh", "reachable": True, "battery": 75,
                 "battery_cached": 75, "last_live_ts": 1783900000,
                 "geometry": {"round": True, "resolution": "400x400"}},
                {"codename": "pike", "serial": "PIKE", "orbit": True, "empty": False,
                 "ip": "10.0.0.8", "adb": None, "reachable": False, "battery": None,
                 "battery_cached": 40, "last_live_ts": 1783900000}]},
        ],
    }
    h = tmp_path / "orbit.js"
    h.write_text(_DOM_CAPTURE + JS + global_simple() +
                 f"\nconst S={json.dumps(doc)};render(S);"
                 "console.log(JSON.stringify(global.__els['tb'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'id="orbip"' in html and "launchOrbit()" in html      # launch-by-IP box
    assert "Orbit" in html                                       # section header
    assert "cbadge wifi" in html and "10.0.0.9" in html          # reachable → WiFi pill + IP
    assert "offrow" in html                                      # pike is offline/dimmed
    assert "deorbit('CAT'" in html and "deorbit('PIKE'" in html  # de-orbit per serial
    assert "openCC('CAT'" in html                                # codename opens CC
    assert 'id="wr-orbit-CAT"' in html and 'id="wr-orbit-PIKE"' in html
    # No power toggle / smart / menuExecute on an orbit row (no wire to act on).
    orbit_part = html[html.index("wr-orbit-CAT"):]
    assert "pwrGo(" not in orbit_part and "menuExecute(" not in orbit_part


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_handed_off_port_renders_available_with_orbit_hint(tmp_path):
    """A port whose watch handed off to orbit renders as an AVAILABLE empty port
    (power toggle + Onboard) with a dim '<codename> ↗ orbit' hint — not the watch,
    which now lives in the Orbit section."""
    import json
    doc = {"version": "t", "thresholds": {"low": 40, "high": 80},
           "drain_floor": 15, "wearable_min_hours": 24,
           "hubs": [{"location": "1-2", "description": "Hub", "hidden": False,
                     "ports": [
               {"port": 1, "codename": None, "slot_loc": "1-2", "power": True,
                "smart": True, "adb": None, "battery": None, "empty": True,
                "orbited_codename": "skipjack", "socket": 1}]}]}
    h = tmp_path / "ho.js"
    h.write_text(_DOM_CAPTURE + JS + global_simple() +
                 f"\nconst S={json.dumps(doc)};render(S);"
                 "console.log(JSON.stringify(global.__els['tb'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "skipjack" in html and "orbit-hint" in html   # whose port, now in orbit
    assert "pwrMenu(event,'1-2:1'" in html             # port is available
    assert "doRemap('1-2:1')" in html                    # Onboard offered


def test_refreshing_row_pulse_survives_hover():
    """The refreshing-row pulse is the only feedback that a re-identify is in
    flight. An !important background on the :hover rule outranks the animation
    itself (important declarations beat keyframes), pinning the row and hiding
    the hint exactly while the pointer is on the row being watched."""
    hover = [ln for ln in _WEB_TEMPLATE.splitlines()
             if ".wr.refreshing:hover" in ln]
    assert hover, "no hover rule for a refreshing row — hover will mask the pulse"
    assert not any("!important" in ln for ln in hover), (
        f"!important on the refreshing-row hover rule kills the pulse "
        f"animation it is meant to preserve: {hover}")
    assert "@keyframes rpulsehover" in _WEB_TEMPLATE, (
        "hovered refreshing rows need their own keyframe pulsing from the "
        "hover colour, else the pulse is invisible under the highlight")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_adb_and_ssh_badges_are_consistent_two_way_toggles(tmp_path):
    """Both USB-mode badges should read as one control: the same pill shape,
    the AsteroidOS logo in front, and a click that toggles to the other mode.
    The ADB pill switches an AsteroidOS watch to SSH; the SSH pill switches
    back to ADB."""
    import json
    h = tmp_path / "badges.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "adb: mkadb('device','', 'asteroidos','S9'),"
                 "ssh: mkadb('ssh','', null,'S9'),"
                 "wear: mkadb('device','', 'WearOS','S9')}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    # Both are pills (cbadge), real <button>s so the cursor is a pointer, and
    # carry the logo (an inline <svg>).
    for k in ("adb", "ssh"):
        assert "cbadge" in out[k], f"{k} badge is not the pill style: {out[k]}"
        assert "<button" in out[k], f"{k} clickable badge is not a real button: {out[k]}"
        assert "<svg" in out[k], f"{k} badge is missing the AsteroidOS logo"
    # Clicking a badge opens the Network Center (not an inline mode toggle,
    # which was too easy to misclick).
    assert "openNC(" in out["adb"], f"ADB pill does not open Network Center: {out['adb']}"
    assert "openNC(" in out["ssh"], f"SSH pill does not open Network Center: {out['ssh']}"
    # The ADB pill shows the serial (its address), like SSH shows the IP.
    assert "S9" in out["adb"], f"ADB pill does not show the serial: {out['adb']}"
    # A known non-AsteroidOS OS is a status pill, not an SSH toggle (usb_moded
    # is AsteroidOS-only) and carries no asteroid logo.
    assert "switchSsh(" not in out["wear"] and "<svg" not in out["wear"], out["wear"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_smart_column_is_pills_with_the_cycle_as_the_untested_state(tmp_path):
    """Smart is a pill — green 'yes' when the port can switch power, red 'NO!'
    when it can't. Untested shows the power-cycle button in place of a bare '?',
    because the cycle IS the test. The Power column keeps only the on/off
    toggle; the standalone cycle icon is gone from it."""
    import json
    h = tmp_path / "smt.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "yes:mksmart({smart:true},'1-2:1',''),"
                 "no:mksmart({smart:false},'1-2:1',''),"
                 "unk:mksmart({smart:null},'1-2:1','')}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="cbadge ppps"' in out["yes"] and ">ppps<" in out["yes"]
    assert 'class="cbadge no"' in out["no"] and "NO!" in out["no"]
    assert 'class="cbadge unk"' in out["unk"] and "doCy('1-2:1')" in out["unk"], out["unk"]
    assert "&#x21BA;" in out["unk"], "untested state must show the cycle glyph"
    # The cycle lives only in the smart cell now — not as a power-column icon.
    # doCy has TWO call sites on purpose since 2026-08-09, with different jobs:
    # the smart pill's cycle IS the switchability test and appears only while
    # the verdict is unknown; the port toggle's menu offers a power cycle as a
    # routine action. Same op, different intent and availability — but still
    # one implementation, which is what "consolidated" protects.
    assert JS.count("function doCy(") == 1, "doCy has more than one implementation"
    assert JS.count("doCy('") == 2, "expected doCy from the smart pill and the port menu only"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_battery_graph_dot_opens_battery_info_with_history(tmp_path):
    """The Stats battery-graph dot opens the same Battery Info panel as the
    battery pill — not a separate sparkline popup — and the panel carries the
    history chart at its foot when history exists."""
    import json
    # (a) the dot wires openBI, and the old openSpark popup is gone.
    strip = _WEB_TEMPLATE
    assert "openSpark" not in strip, "battery-graph dot still opens the old popup"
    h = tmp_path / "bihist.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlName='sk';ctlTab='bat';"
                 "biHist['S9']={points:[{ts:1,pct:90},{ts:2,pct:80},{ts:3,pct:70}],rate:0.5};"
                 "renderControl({bat_cap:80});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Battery history" in html and "spark-svg" in html, "BI panel lost its history chart"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_connection_shame_badge_bands_and_clamps(tmp_path):
    """The badge earns its place by being readable at a glance: green only at
    a genuine zero, and never two digits, which would break the circle."""
    import json
    h = tmp_path / "flaps.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst m=n=>mkstrip({codename:'x',serial:'S9',flaps:n},24);"
                 "\nconsole.log(JSON.stringify({"
                 "clean:m(0),one:m(1),five:m(5),six:m(6),huge:m(37)}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="sdot flaps on"' in out["clean"], "a clean port is not green"
    assert ">0<" in out["clean"], "a clean port does not read 0"
    # One drop is already worth seeing, but it is not yet a pattern.
    assert 'class="sdot flaps warn"' in out["one"]
    assert 'class="sdot flaps warn"' in out["five"], "5 should still be orange"
    assert 'class="sdot flaps err"' in out["six"], "6 must go red"
    # The clamp: the circle shows a single digit, the tooltip carries truth.
    assert ">9<" in out["huge"], "a big count is not clamped to one digit"
    assert "37 reconnects" in out["huge"], "the true count is missing from the tip"
    assert ">37<" not in out["huge"], "two digits would break the circle"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stats_items_are_dots_and_the_age_trails_as_text(tmp_path):
    """Every stat icon is a dot — a glyph in a circle — for one visual language
    with the power dot and the charging circle. The last-seen age is NOT a pill
    or a dot; it trails the dots as plain text. No legacy icon spans survive."""
    import json
    h = tmp_path / "strip.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "charging:mkstrip({codename:'x',charging_active:true,serial:'S9'},24),"
                 "off:mkstrip({codename:'x',adb:null,last_live_ts:1000},24),"
                 "full:mkstrip({codename:'x',adb:'device',charge_status:'Full',serial:'S9'},24)}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="sdot chg' in out["charging"], "charging op is not a dot"
    # The connection shame badge replaced the battery-graph dot in this slot;
    # it is always present on a mapped port and reads 0 (green) when clean.
    assert 'class="sdot flaps on"' in out["charging"], "shame badge is not a dot"
    # Conditional charge state sits last, after the always-present badge.
    assert out["charging"].index("sdot flaps") < out["charging"].index("sdot chg"), \
        "the conditional charge dot must come after the connection badge"
    assert 'class="sdot on"' in out["full"], "full-charge state is not a dot"
    # The charge dot opens Battery Info too — gauge, graph dot and charge dot
    # all lead to the same panel (shame badge + charge dot = two openBI here).
    assert out["full"].count("openBI") >= 2, "charge dot does not open Battery Info"
    # An untested wearability reads grey, not amber.
    assert 'class="sdot dim"' in out["off"], "untested wearability is not grey"
    assert 'class="lastseen"' in out["off"], "last-seen age is not trailing text"
    assert "spill" not in out["off"], "last-seen age is still a pill"
    # The old icon-span classes are gone everywhere.
    for html in out.values():
        assert "svgw" not in html and 'class="ib' not in html, f"legacy icon span left: {html}"


def test_pills_and_dots_share_one_height_token():
    """Every in-row pill and glyph-dot draws its height from one --pill-h token,
    so they line up; change it once and all follow. Pills stay inline-block so
    long content wraps to a second inner line instead of forcing the table wider
    than the viewport. (The orbit-eclipse toggle keeps its own fixed geometry.)"""
    assert "--pill-h:" in _WEB_TEMPLATE, "no shared pill-height token"

    def rule(sel):
        # The declaration block where `sel` starts a rule — not where it appears
        # inside a descendant selector like ".pcell .tgl".
        m = re.search(r"(?:^|[\n;}])\s*" + re.escape(sel) + r"\s*\{([^}]*)\}",
                      _WEB_TEMPLATE)
        assert m, f"no standalone rule for {sel}"
        return m.group(1)

    # .smt was byte-identical to .cbadge and collapsed into it on 2026-08-09.
    for sel in (".cbadge", ".sdot", ".tgl"):
        assert "var(--pill-h)" in rule(sel), f"{sel} does not use the shared height token"
    for sel in (".cbadge",):
        assert "inline-block" in rule(sel), f"{sel} is not inline-block — long content won't wrap"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_power_toggle_uses_the_orbit_eclipse_states(tmp_path):
    """The power toggle is the flat dot pill (tgl-on/tgl-off with a coloured
    dot + ON/OFF). Clicking an OFF toggle adds the animated .pending exec state
    and POSTs the on op for the port."""
    import json
    h = tmp_path / "tgl.js"
    h.write_text(_DOM_CAPTURE + JS + global_simple() +
                 f"\nconst S={json.dumps(_SAMPLE)};render(S);"
                 "const html=global.__els['tb'].innerHTML;"
                 # simulate a click on an OFF toggle (no 'tgl-on' class -> switch on)
                 "let url=null;global.fetch=(u,o)=>{url=u;return new Promise(()=>{});};"
                 "const tel={classList:{_s:new Set(),add(c){this._s.add(c);},remove(){},"
                 "toggle(){},contains(c){return this._s.has(c);}}};"
                 "pwrGo(tel,'1-2:2');"
                 "console.log(JSON.stringify({html,pending:tel.classList.contains('pending'),url}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="tgl tgl-off"' in out["html"] or 'class="tgl tgl-on"' in out["html"], "not the flat toggle"
    assert "dot doff" in out["html"] or "dot don" in out["html"], "toggle dot missing"
    assert "tgl-spin" not in _WEB_TEMPLATE and 'content:"EXEC"' not in _WEB_TEMPLATE, "orbit toggle bits survive"
    assert "@keyframes tgldot" in _WEB_TEMPLATE, "no animated exec state"
    assert out["pending"] is True, "click did not add the exec state"
    assert out["url"] == "/api/on/1-2/2", f"click did not POST the on op: {out['url']}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_starfield_seeds_a_deterministic_field(tmp_path):
    """seedStars paints a fixed, seeded star field into the #stars backdrop —
    the same field every load — with drifting, coloured stars."""
    import json
    assert 'id="stars"' in _WEB_TEMPLATE and "#stars{position:fixed" in _WEB_TEMPLATE
    assert "z-index:-1" in _WEB_TEMPLATE.split("#stars{")[1].split("}")[0], "starfield not behind content"
    h = tmp_path / "stars.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nseedStars();const a=global.__els['stars'].innerHTML;"
                 "seedStars();const b=global.__els['stars'].innerHTML;"
                 "console.log(JSON.stringify({eq:a===b,n:(a.match(/<span/g)||[]).length,drift:a.indexOf('drift')>=0}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["n"] >= 100, f"too few stars: {out['n']}"
    assert out["eq"], "star field is not deterministic for a fixed seed"
    assert out["drift"], "stars are not animated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_registry_panel_renders_roster_and_log(tmp_path):
    """The Fleet Registry panel lists hulls with identity + source + last-seen,
    and an expanded record shows its change Log as old → new."""
    import json
    payload = {"watches": [
        {"serial": "870AX0A150253", "first_seen": 1, "last_seen": 2e9,
         "last_source": "adb",
         "fields": {"codename": "skipjack", "kernel": "3.18.24", "qt": "6.11.2"},
         "log": [{"ts": 1.9e9, "source": "ssh",
                  "changes": {"qt": ["5.15.16", "6.11.2"]}}]},
        {"serial": "720EX8C130737", "first_seen": 1, "last_seen": 1.5e9,
         "last_source": "orbit",
         "fields": {"codename": "catfish", "kernel": "3.18.120"}, "log": []}]}
    h = tmp_path / "reg.js"
    h.write_text(_DOM_CAPTURE + JS + global_simple() +
                 "\nglobal.document.getElementById('reg');"      # cache the node
                 f"renderRegistry({json.dumps(payload)});"
                 "_regOpen['870AX0A150253']=true;renderRegistry();"
                 "console.log(JSON.stringify(global.__els['reg'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "2 hulls on record" in html
    assert "skipjack" in html and "catfish" in html
    assert "870AX0A150253" in html                       # serial shown
    assert "cbadge wifi" in html and "cbadge adb" in html  # orbit vs adb source
    assert "toggleRegLog('870AX0A150253')" in html       # expandable (has a log)
    assert "5.15.16" in html and "6.11.2" in html        # expanded Qt migration
    assert "qt:" in html


def test_menu_trigger_is_a_markerless_pill():
    """The row menu trigger spawns a panel like the badges/battery pills do, so
    it reads as one of them: labelled "menu", a pill, and no dropdown ▾ marker
    (which also stopped it wrapping to two lines in a narrow tiled window)."""
    assert ">menu</button>" in _WEB_TEMPLATE, "menu trigger lost its label or gained a marker"
    assert "&#9662;" not in _WEB_TEMPLATE and "▾" not in _WEB_TEMPLATE
    assert ".btn.ex{border-radius" in _WEB_TEMPLATE, "menu trigger is not pill-shaped"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_stats_dots_open_contextual_mini_menus(tmp_path):
    """The power dot opens a short Power-only menu; the wearability dot opens a
    Drain-test + Wear menu — the same builders as the full row menu, scoped."""
    import json
    assert "menuPwr(event," in JS, "power dot not wired to its menu"
    assert "menuWear(event," in JS, "wearability dot not wired to its menu"
    h = tmp_path / "dm.js"
    ev = ("{stopPropagation(){},currentTarget:{getBoundingClientRect:()=>"
          "({left:0,right:0,top:0,bottom:0})}}")
    h.write_text(_DOM_CAPTURE + JS +
                 f"\nmenuPwr({ev},'1-2:1',false,false,false,true,false);"
                 "const pwr=global.__els['menu'].innerHTML;"
                 f"menuWear({ev},'1-2:1',false,'S9',0);"
                 "const wear=global.__els['menu'].innerHTML;"
                 "console.log(JSON.stringify({pwr,wear}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'exgrp-hd">Power<' in out["pwr"] and "Reboot" in out["pwr"]
    assert "Workbench" not in out["pwr"], "power dot menu should be power-only"
    assert 'exgrp-hd">Drain test<' in out["wear"] and "Drain test" in out["wear"]
    assert 'exgrp-hd">Wear<' in out["wear"] and "menu-wear" in out["wear"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_execute_menu_is_ordered_by_consequence(tmp_path):
    """The Execute menu holds every action for a row, grouped and visible at
    once. What changed on 2026-08-09 is the ORDER and the tiers.

    Flashing used to sit second of five, so every trip to Workbench dragged the
    cursor across three ways to wipe the watch. Now the groups run by
    consequence: Wear and Power, then Workbench, then Capture (produces a file,
    changes nothing on the device), then the wipes LAST behind a red rail.

    The Refresh group is gone entirely — its single item was a port power cycle
    with an identify, and device-driven discovery does the identifying now. The
    manual trigger survives as the empty row's Onboard button."""
    import json
    h = tmp_path / "ex.js"
    ev = ("{stopPropagation(){},currentTarget:{getBoundingClientRect:()=>"
          "({left:0,right:0,top:0,bottom:0})}}")
    h.write_text(_DOM_CAPTURE + JS +
                 f"\nmenuExecute({ev},'1-2:1',false,false,false,true,false,"
                 "'S9',false,'device','192.168.13.37',0,'skipjack','');"
                 "const on=global.__els['menu'].innerHTML;"
                 f"menuExecute({ev},'1-2:1',true,false,false,true,false,"
                 "'S9',false,'fastboot','',0,'skipjack','');"
                 "const fb=global.__els['menu'].innerHTML;"
                 "console.log(JSON.stringify({on,fb}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    on = out["on"]

    for hd in ("Workbench", "Capture", "Wear"):
        assert f'exgrp-hd">{hd}<' in on, f"missing group header {hd}: {on[:200]}"
    assert 'dangerhd">wipes the watch<' in on, "the wipes have no heading of their own"
    assert 'class="exgrp-hd">Refresh<' not in on, \
        "the Refresh group is gone; its item was a power cycle with an identify"
    assert "Re-identify" not in on, "re-identify should live on the Onboard button now"

    # Power items are NOT here any more — they live on the power dot.
    assert "Reboot" not in on, "the Power group is back; it belongs on the dot"
    assert 'exgrp-hd">Power<' not in on
    for item in ("Backup data", "Checkout", "Arm wear",
                 "Flash nightly", "Dump mmcblk0"):
        assert item in on, f"menu lost {item!r}"

    # Consequence order: the destructive group is LAST, below capture.
    assert on.index('exgrp-hd">Wear<') < on.index('exgrp-hd">Workbench<'), "Wear not first"
    assert on.index('exgrp-hd">Capture<') < on.index('dangerhd">wipes'), \
        "capture must sit above the wipes"
    assert on.index('dangerhd">wipes') > on.index('exgrp-hd">Workbench<'), \
        "the wipes are not last — they used to sit second, in the cursor's path"
    assert 'class="menu-wear' in on, "wear should stay a pink button"
    assert 'class="exgrp dangerbox"' in on, "the wipes have no rail"

    # Fastboot: bootloader power group in, watch-only groups out.
    fb = out["fb"]
    assert "Continue boot" in fb and 'class="exgrp-hd">Workbench<' not in fb
    assert 'class="exgrp-hd">Wear<' not in fb


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_onboard_is_the_one_reidentify_path(tmp_path):
    """Re-identify used to be reachable from two buttons: the empty row's
    Onboard pill and a Refresh group in the row menu. The menu copy is gone as
    of 2026-08-09 — on an already-identified row it amounted to a port power
    cycle, and identification is device-driven now (a watch that enumerates is
    picked up by the status pass without being asked).

    What must hold: exactly one code path, reached from the Onboard button, and
    never the old separate doRefresh path."""
    ev = ("{stopPropagation(){},currentTarget:{getBoundingClientRect:()=>"
          "({left:0,right:0,top:0,bottom:0})}}")
    h = tmp_path / "ri.js"
    h.write_text(_DOM_CAPTURE + JS +
                 f"\nmenuExecute({ev},'1-2:1',false,false,false,true,false,"
                 "'S9',false,'device','192.168.13.37',0,'skipjack','');"
                 "console.log(global.__els['menu'].innerHTML);\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    menu = r.stdout
    assert "Re-identify" not in menu, \
        "the row menu carries re-identify again — it is the Onboard button's job"

    # One path, still reached from the Onboard pill, and the old light path
    # stays deleted.
    assert "doRemap(" in _WEB_TEMPLATE and "function doRefresh" not in _WEB_TEMPLATE
    assert "'Onboard'" in _WEB_TEMPLATE, "the Onboard button is the remaining trigger"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_workbench_holds_only_what_has_no_other_home(tmp_path):
    """The USB IP left this menu first (it lives in the Network Center), and on
    2026-08-09 the rest of the duplicates followed: the USB-mode SWITCH to the
    Connect tab, Set time to Settings, Screenshot to the live view, Notify to
    Vitals beside Buzz. Every one of those was a second copy of a control that
    already existed on the surface showing its result.

    What is left is what has no other home: checkout, and the diagnostics
    bundle export."""
    import json
    h = tmp_path / "wb.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst CAP=grpWorkbench('1-2:1','S9',false,'ssh','192.168.13.37');"
                 "console.log(JSON.stringify(CAP));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])

    assert "Checkout" in html and "Collect diagnostics" in html, "workbench lost its own items"
    assert "menu-ip" not in html and "192.168.13.37" not in html, "USB IP still in the menu"
    for moved in ("Switch USB", "Set time", "Screenshot", "Test notification"):
        assert moved not in html, f"{moved!r} is duplicated here again"

    # And each one must still be reachable where it moved to.
    assert "switchAdb(" in _WEB_TEMPLATE and "switchSsh(" in _WEB_TEMPLATE
    assert "ccSyncTime()" in _WEB_TEMPLATE, "Set time lost its Settings home"
    assert "shotRefresh(" in _WEB_TEMPLATE and "shotDownload(" in _WEB_TEMPLATE, \
        "the live view has no screenshot controls"
    assert "doNotify(" in _WEB_TEMPLATE, "Notify was deleted rather than moved"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_network_center_lists_usb_ip_and_mode_toggle(tmp_path):
    """Clicking the badge opens the Network Center. It must carry the USB IP —
    which lives nowhere else — and the deliberate USB-mode toggle that the
    badge no longer does inline."""
    import json
    h = tmp_path / "nc.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSshIp='192.168.13.37';ctlMode='ssh';ctlSerial='S9';ctlName='skipjack';ctlTab='net';"
                 "renderControl({serial:'S9', os:'AsteroidOS', wifi:1, ip:'10.0.0.9', wlanmac:'aa:bb'});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "192.168.13.37" in html, f"Network Center is missing the USB IP: {html[:300]}"
    assert "Host link IP" in html   # the USB section labels it per-device now
    assert "switchAdb(" in html, "SSH-mode Network Center lacks the USB->ADB toggle"
    assert "ncToggle('wifi'" in html, "Network Center lacks the WiFi toggle"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_usb_switch_updates_an_open_network_center(tmp_path):
    """A USB-mode switch made from the Network Center updates the open panel's
    mode and IP immediately (the badge alone updating left the panel stale)."""
    import json
    h = tmp_path / "ncsync.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlName='wren';ctlMode='adb';ctlSshIp='192.168.2.15';ctlTab='net';"
                 "ctlCache['S9']={serial:'S9'};"
                 "ctlSet('S9','ssh','192.168.13.42');"
                 "console.log(JSON.stringify({mode:ctlMode,ip:ctlSshIp,"
                 "html:global.__els['cc'].innerHTML}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["mode"] == "ssh" and out["ip"] == "192.168.13.42"
    assert "192.168.13.42" in out["html"] and "192.168.2.15" not in out["html"], \
        "Network Center still shows the stale USB IP after the switch"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_control_center_no_longer_carries_the_network_section(tmp_path):
    """The network detail moved to the Network Center, freeing the Control
    Center. Its render must no longer emit the old WiFi/BT toggle wiring."""
    import json
    h = tmp_path / "ccnet.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlName='skipjack';ctlSerial='S9';ctlTab='vit';"
                 "renderControl({serial:'S9', kernel:'3.18', os:'AsteroidOS', wifi:1});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "ccToggle(" not in html, "Control Center still wires the moved WiFi/BT toggles"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_battery_gauge_fills_by_level_and_colours_only_when_connected(tmp_path):
    """The battery cell is a gauge: a fixed-width bar whose fill grows with the
    charge level and opens Battery Info on click. Connected → coloured fill (a
    mid-level reads amber .ok); disconnected → grey (.off) fill at the last
    level, a level without a colour claim."""
    import json
    h = tmp_path / "bp.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "live:mkbatCell({battery:60,serial:'S9',codename:'sk'},40,80),"
                 "off:mkbatCell({battery_cached:55,serial:'S9',last_live_ts:1000},40,80)}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="fillpill bat ok"' in out["live"] and "width:60%" in out["live"], out["live"]
    assert "openBI('S9'" in out["live"] and "60%" in out["live"]
    assert 'class="fillpill bat off"' in out["off"] and "width:55%" in out["off"], "offline gauge not grey"
    assert "Charging" not in out["live"], "charge status should not repeat in the gauge"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_battery_info_window_lists_the_detail(tmp_path):
    """Battery Info carries the detail moved out of the Control Center."""
    import json
    h = tmp_path / "bi.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlName='skipjack';ctlTab='bat';"
                 "renderControl({serial:'S9',os:'AsteroidOS',bat_cap:83,bat_status:'Charging',"
                 "bat_volt:3900000,bat_cycles:42,standby_measured:2.5});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Cycles" in html and "42" in html, html[:300]
    assert "Standby" in html and "Voltage" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_control_center_no_longer_carries_the_battery_section(tmp_path):
    """Battery detail moved to Battery Info; the Control Center System section
    stays but the Battery section is gone."""
    import json
    h = tmp_path / "ccbat.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlName='skipjack';ctlSerial='S9';ctlTab='vit';"
                 "renderControl({serial:'S9',kernel:'3.18',os:'AsteroidOS',bat_cap:83,bat_cycles:42});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Vitals" in html, "Control Center lost its Vitals section"
    assert "Cycles" not in html, "Control Center still carries the moved battery detail"
    assert "ccSyncTime(" not in html, "Sync-from-host should have moved to the Settings tab"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_power_dot_is_the_first_stats_dot_coloured_by_state(tmp_path):
    """The power state is the first Stats dot now (same circle language as the
    other stats): green when the port is powered, grey for a confirmed
    graceful-down, orange when ambiguous (off with no down marker) — always
    shown, never blank. Worn stays a pink pill by the codename, not a dot."""
    import json
    h = tmp_path / "life.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "on:pdot({power:true}),"
                 "down:pdot({lifecycle:'down'}),"
                 "amb:pdot({}),"
                 "worn:mklife({lifecycle:'worn'}),"
                 "plain:mklife({power:true})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'class="sdot on"' in out["on"] and 'class="pwri"' in out["on"], "power icon missing"
    assert "&#9211;" not in out["on"], "still the Unicode glyph, not an icon"
    assert 'class="sdot dim"' in out["down"], "safely-down must read grey"
    assert 'class="sdot warn"' in out["amb"], "ambiguous power must read orange"
    assert out["amb"] != "", "the power dot is persistent — never blank"
    # Worn is a name pill; mklife no longer carries the power state at all.
    assert "life worn" in out["worn"] and "sdot" not in out["worn"]
    assert out["plain"] == "", "mklife carries only worn now, not the power state"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_boot_pill_shows_in_connection_column_and_outranks_no_link(tmp_path):
    """A triggered boot paints a white pulsing 'booting up' pill in the
    connection column, escalating to a red-flashing 'boot failed?' once the
    window lapses. Both carry positive evidence a boot is under way, so they
    outrank the generic not-enumerating / no-link messages that would otherwise
    show for a powered port with no adb."""
    import json
    h = tmp_path / "boot.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst base={adb:null,power:true,connected:true,not_enumerating:true};"
                 "console.log(JSON.stringify({"
                 "booting:mkadbrow({...base,lifecycle:'booting'}),"
                 "bootfail:mkadbrow({...base,lifecycle:'bootfail'}),"
                 "plain:mkadbrow({...base,lifecycle:null})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "life booting" in out["booting"] and "booting up" in out["booting"]
    assert "life bootfail" in out["bootfail"] and "boot failed?" in out["bootfail"]
    # 'boot failed?' is hedged (question mark) — it is a suspicion, not a verdict.
    assert out["bootfail"].count("?") >= 1
    # Neither boot pill lets the generic not-enumerating pill through (match the
    # visible label, not the word where it appears inside the boot tooltip).
    assert ">not enumerating<" not in out["booting"]
    assert ">not enumerating<" not in out["bootfail"]
    # With no boot claim, the same row still shows not-enumerating as before.
    assert ">not enumerating<" in out["plain"]


def test_drain_pill_shows_in_connection_column_with_the_feature_combo(tmp_path):
    """A running standby drain test cuts VBUS, so the watch is off the bus by
    design — the row would otherwise fall through to a bare dash / no-link. The
    connection column names the run AND the feature combo under test (idle =
    the all-off baseline), so a glance shows which combination is running."""
    import json
    h = tmp_path / "drain.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst base={adb:null,power:false,connected:false};"
                 "console.log(JSON.stringify({"
                 "wifi:mkadbrow({...base,drain:{active:true,features:{wifi:true,bt:false,aod:false}}}),"
                 "base0:mkadbrow({...base,drain:{active:true,features:{wifi:false,bt:false,aod:false}}}),"
                 "off:mkadbrow({...base,drain:{active:false}})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "cbadge drain" in out["wifi"] and "drain test wifi" in out["wifi"]
    assert "drain test idle" in out["base0"]     # all-off baseline reads 'idle'
    # An inactive drain must NOT paint the pill — the row shows its normal state.
    assert "drain test" not in out["off"]


def test_column_order_is_the_ground_truth_order():
    """Columns run in the fleet's ground-truth order: the port everything
    originates from, its controls (power, smart), then the connection/battery
    state that leads over to the watch, then the watch itself, its stats, and
    actions last. The two blank leading headers are the tree glyph and the
    thumbnail."""
    m = re.search(r"<thead>.*?</thead>", _WEB_TEMPLATE, re.S)
    labels = [t for t in re.findall(r"<th[^>]*>([^<]*)</th>", m.group(0)) if t.strip()]
    # Port folds into the Power cell, so there is no separate Power header; the
    # blank header is the thumbnail.
    assert labels == ["Port", "Smart", "Connection",
                      "Watch", "Stats", "Battery", "Actions"], labels


def test_usb_preference_toggle_is_present_with_a_bullet_tooltip():
    """The situational adb/ssh preference lives as a third top-bar link with a
    tooltip spelling out the consequences in bullets."""
    assert 'id="usbpreflink"' in _WEB_TEMPLATE
    assert "onclick=\"toggleUsbPref()" in _WEB_TEMPLATE
    assert _WEB_TEMPLATE.count("•") >= 2, "tooltip must list consequences as bullets"
    assert "function toggleUsbPref(" in JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_render_labels_the_usb_preference_from_status(tmp_path):
    """render() mirrors data.usb_mode_preference onto the top-bar link — 'prefer
    SSH' when set to ssh, 'prefer ADB' otherwise."""
    import json
    h = tmp_path / "pref.js"
    h.write_text(_DOM_CAPTURE + JS + global_simple() +
                 "\nrender({hubs:[],usb_mode_preference:'ssh'});"
                 "const ssh=global.__els['usbpreflink'].textContent;"
                 "render({hubs:[],usb_mode_preference:'adb'});"
                 "const adb=global.__els['usbpreflink'].textContent;"
                 "console.log(JSON.stringify({ssh,adb}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ssh"] == "prefer SSH" and out["adb"] == "prefer ADB", out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_powered_but_unconnected_says_no_link_not_a_cause(tmp_path):
    """A powered port with nothing electrically connected reads "no link" — a
    neutral statement of the observation. It must NOT say "not docked", which
    claims a specific cause (the plug was pulled) we cannot tell apart from a
    dead contact."""
    import json
    h = tmp_path / "nolink.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify(mkadbrow("
                 "{adb:null,power:true,connected:false})));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert ">no link<" in out, out
    assert "not docked" not in out, "still claims the plug was pulled"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reopening_a_panel_paints_instantly_from_cache(tmp_path):
    """A previously-opened panel must repaint from the cached payload straight
    away — no 'loading…' flash while the (possibly slow, over-SSH) fetch runs."""
    import json
    h = tmp_path / "cache.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"          # never resolves
                 "\nctlCache['S9']={kernel:'3.18',os:'AsteroidOS'};"  # seed a prior open
                 "openCC('S9','skipjack',{stopPropagation(){},clientX:5,clientY:5});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Vitals" in html and "AsteroidOS" in html, f"did not paint from cache: {html[:200]}"
    assert "loading" not in html, "showed a loading flash despite having a cache"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_reconcile_keys_hub_headers_by_address_not_by_label(tmp_path):
    """The reconcile drops a repeated key, so a key must be unique per row.

    Hub headers used to be keyed by their visible LABEL — and auto-naming gives
    every chip in one physical box the same name. The Sabrent is five hub
    entries all labelled "Sabrent", so four of its five headers collided onto
    one key and silently never reached the DOM. Same for the five "A16 #2"
    chips. Symptom on the rig: hiding the box appeared to do nothing, then
    removed rows four at a time.

    Headers key on their address now, which is unique by construction."""
    import json
    h = tmp_path / "key.js"
    same = ('<tr class="hub-hdr" id="hub-1-6.1"><td><span class="hl">Sabrent</span></td></tr>',
            '<tr class="hub-hdr" id="hub-1-6.2"><td><span class="hl">Sabrent</span></td></tr>')
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "row:_rowKey('<tr class=\"wr\" id=\"wr-1-2.3:4\"><td>x</td></tr>'),"
                 f"a:_rowKey('{same[0]}'),b:_rowKey('{same[1]}'),"
                 "orbit:_rowKey('<tr><td><span class=\"hl\">Orbit</span></td></tr>')}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert out["row"] == "row:1-2.3:4", out
    assert out["a"] == "hub:1-6.1" and out["b"] == "hub:1-6.2", out
    assert out["a"] != out["b"], \
        "two chips of one box share a key — four of five headers would vanish"
    # Sections that legitimately have no address still get their own key.
    assert out["orbit"] == "sec:Orbit", out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_spark_bars_scale_and_colour_by_metric_direction(tmp_path):
    """The live graph draws fixed-scale filled bars, coloured green→red toward
    the metric's bad end: a full battery (bad='low') is green, a high load
    (bad='high') is red. Newest sample is right-aligned."""
    import json
    h = tmp_path / "spark.js"
    h.write_text(_DOM_STUBS + JS +
                 "\ngraphData={bcap:[95],load:[3.9]};"
                 "const bat=spark('bcap',0,100,'low');"      # 95% battery -> green
                 "const load=spark('load',0,4,'high');"      # near-max load -> red
                 "const empty=spark('nope',0,100,'low');"
                 "console.log(JSON.stringify({bat,load,empty}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert "<svg" in o["bat"] and "<rect" in o["bat"], o["bat"]
    # hue: 120=green, 0=red. Full battery (bad=low, n=0.95) -> mostly green (hue>90).
    bhue = int(o["bat"].split("hsl(")[1].split(",")[0])
    lhue = int(o["load"].split("hsl(")[1].split(",")[0])
    assert bhue > 90, f"full battery should be green, hue={bhue}"
    assert lhue < 30, f"high load should be red, hue={lhue}"
    assert o["empty"] == "", "no samples yet must draw nothing"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_poll_interval_and_tag_follow_the_transport(tmp_path):
    """adb polls at 3s, SSH at 10s (a 3s poll can't keep up with an SSH
    round-trip), and the panel header shows which."""
    import json
    h = tmp_path / "poll.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "adbMs:panelPollMs({transport:'adb'}),sshMs:panelPollMs({transport:'ssh'}),"
                 "adbTag:pollTag({transport:'adb'}),sshTag:pollTag({transport:'ssh'})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["adbMs"] == 3000 and o["sshMs"] == 10000, o
    assert "3s" in o["adbTag"] and "10s" in o["sshTag"] and "ssh" in o["sshTag"]


def test_stale_endpoint_and_paintstale_are_wired():
    """First open paints instantly from the fast /stale endpoint. It must be a
    registered route, and paintStale must call it."""
    from asteroid_docking_bay.webapp import _JSON_ROUTES
    stale = [r for r in _JSON_ROUTES if r[1].endswith("/stale")]
    assert stale and stale[0][2] == "watch.cc" and stale[0][3] == {"stale": True}, stale
    assert "function paintStale(" in JS
    assert "/stale'" in JS, "paintStale does not hit the /stale endpoint"


def test_action_buttons_give_instant_click_feedback():
    """A clicked in-row control gives instant feedback while the command is in
    flight, since state only updates on the next refresh. The cycle icon pulses
    (pulseSelf); the power toggle shows the orbit-eclipse .pending spinner via
    pwrGo."""
    assert "function pulseSelf(" in JS
    assert "pulseSelf(this);doCy(" in JS, "cycle button lacks instant feedback"
    assert "function pwrGo(" in JS and "classList.add('pending')" in JS, \
        "power toggle lacks its in-flight pending state"
    assert "pwrMenu(event," in JS, "power toggle not wired to its port menu"


def test_failed_actions_flash_red():
    """A failed command flashes its element red 3× — the port toggle flashes
    the button, a refused mode switch flashes the connection pill."""
    assert "function flashFail(" in JS and "cmd-fail" in _WEB_TEMPLATE
    # port toggle: on confirmed===false, flash the clicked button
    assert "if(d.confirmed===false){if(el)flashFail(el)" in JS, "power toggle failure not flashed"
    # mode switch: on !ok, flash the row's connection pill
    assert "flashFail(connPill(serial))" in JS, "mode-switch failure not flashed"
    # the connection cell carries an id so the pill can be found
    assert 'id="conn-${esc(p.serial)}"' in JS


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_mousedown_outside_an_open_panel_closes_it(tmp_path):
    """One document-level mousedown-capture handler enforces both rules at once:
    a click anywhere outside an open panel closes it, and since triggering
    another panel is itself an outside click, only one window is ever up. The
    open() functions must NOT carry their own close-the-others helper — the
    outside-click handler already covers that case (beroset: refactor, don't
    add). The panel must persist on mere hover-out, so no timer/leave close."""
    import json
    h = tmp_path / "one.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 # Open Battery Info, then fire the captured mousedown handler
                 # with a target that no panel contains (contains()=>false).
                 "openBI('S9','sk',{stopPropagation(){},clientX:0,clientY:0});"
                 "global.__els['cc'].style.display='block';"
                 "const biBefore=ctlSerial;"
                 "global.__h.mousedown({target:el()});"
                 "console.log(JSON.stringify({biBefore,biAfter:ctlSerial,"
                 "hasHandler:typeof global.__h.mousedown}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["hasHandler"] == "function", "no document mousedown-capture handler"
    assert o["biBefore"] == "S9", "Battery Info did not open"
    assert o["biAfter"] is None, "outside mousedown did not close Battery Info"
    # The refactor's whole point: no per-open close helper, no hover-close.
    assert "closePanels" not in JS, "openers still call an added close helper"
    assert "ccLeave" not in JS and "onmouseleave" not in _WEB_TEMPLATE, (
        "a hover-out close path survives — panels must persist until a click")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_switching_tabs_keeps_the_graph_history(tmp_path):
    """The single window's promise: switching tabs re-renders the cached blob
    with NO graphReset, so a graph accrued on one tab keeps its history when you
    move to another. A graphReset() inside ctlTabTo would wipe it (the bug this
    pins), and the poll — not the switch — is what refills every tab's metric."""
    import json
    h = tmp_path / "tabswitch.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"          # no live data
                 "openControl('S9','sk',{stopPropagation(){},clientX:0,clientY:0},'sys');"
                 "ctlCache['S9']={kernel:'3.18',bat_cap:80};"       # a cached blob
                 "graphData['bcap']=[70,72,74];"                    # history accrued so far
                 "ctlTabTo('bat');"                                 # switch tabs
                 "console.log(JSON.stringify({n:(graphData['bcap']||[]).length,"
                 "html:global.__els['cc'].innerHTML}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["n"] == 3, "tab switch wiped the graph history (a graphReset on switch)"
    assert "Battery" in o["html"] and "Cycles" in o["html"], "did not switch to the Battery tab"
    assert "cc-tab" in o["html"], "the tab row is missing from the window"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dragging_the_title_bar_parks_the_window(tmp_path):
    """Grabbing the title bar moves the window and pins it there: ctlMoved makes
    ctlPlace() a no-op, so a tab switch or a poll re-render cannot snap a parked
    window back to the click anchor (the bug: ctlPlace re-anchoring over it)."""
    import json
    h = tmp_path / "drag.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "openControl('S9','sk',{stopPropagation(){},clientX:0,clientY:0},'sys');"
                 "global.__els['cc'].getBoundingClientRect=()=>({left:100,top:100});"
                 "ctlDragStart({target:{classList:{contains:()=>false}},"
                 "clientX:150,clientY:130,preventDefault(){}});"
                 "global.__h.mousemove({clientX:300,clientY:260});"   # drag to a new spot
                 "const afterDrag=global.__els['cc'].style.left;"
                 "ctlPlace();"                                        # a re-place must not move it
                 "console.log(JSON.stringify({moved:ctlMoved,afterDrag,"
                 "afterPlace:global.__els['cc'].style.left}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["moved"] is True, "dragging the header did not set the manual-position flag"
    assert o["afterDrag"] == "250px", o           # clientX 300 − grab offset 50
    assert o["afterPlace"] == o["afterDrag"], "ctlPlace re-anchored a parked window"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_window_is_placed_once_not_on_every_tab_switch(tmp_path):
    """The window is positioned once when it opens and then left put — placing
    on every tab switch and poll made it hop around as tab bodies differ in size
    (the bug this pins). After the first placement locks, no further placement."""
    import json
    h = tmp_path / "placeonce.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlCache['S9']={kernel:'3.18',bat_cap:80};"      # cached → places+locks at open
                 "openControl('S9','sk',{stopPropagation(){},clientX:0,clientY:0},'sys');"
                 "let placeN=0; placeOverlay=function(){placeN++;};"  # count placements after the lock
                 "ctlTabTo('bat'); ctlTabTo('net');"                # switching tabs must not re-place
                 "console.log(JSON.stringify({placeN,placed:ctlPlaced}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["placed"] is True, "the window never locked its position"
    assert o["placeN"] == 0, "the window re-places on tab switches (it will hop around)"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_tab_renders_toggles_and_readonly_paths(tmp_path):
    """The Settings tab shows the boolean prefs as live toggles that write, a
    default marker on unset keys, and watchface/launcher/wallpaper read-only —
    a path row must NOT wire a write (they are display-only, mo)."""
    import json
    h = tmp_path / "settings.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlSerial='S9';ctlName='sk';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);"
                 "ctlSettings['S9']={ok:true,settings:["
                 "{group:'Units',key:'/org/asteroidos/settings/use-12h-format',"
                 "label:'12-hour clock',type:'bool',value:true,is_set:true},"
                 "{group:'Units',key:'/org/asteroidos/settings/use-fahrenheit',"
                 "label:'Fahrenheit units',type:'bool',value:false,is_set:false},"
                 "{group:'Appearance',key:'/desktop/asteroid/watchface',label:'Watchface',"
                 "type:'path',value:'file:///a/b/000-default.qml',is_set:true}]};"
                 "renderControl({});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "12-hour clock" in html
    assert "settingsWrite('/org/asteroidos/settings/use-12h-format'" in html, "bool is not a live toggle"
    assert "(default)" in html, "an unset key is not marked as default"
    assert "000-default.qml" in html and "Watchface" in html, "path selection not shown"
    assert "settingsWrite('/desktop/asteroid/watchface'" not in html, "a display-only path wired a write"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_network_tab_reads_mode_and_ip_from_the_server_not_the_click(tmp_path):
    """Reached via another tab, the Network tab has no click USB context, so it
    must take the real mode + IP from the blob (d.transport / d.ssh_ip) — an SSH
    watch shown as ADB/.2.15 was the bug (mo: actual IP .13.40, mode ssh)."""
    import json
    h = tmp_path / "nettransport.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlName='sk';ctlTab='net';ctlMode=null;ctlSshIp=null;"
                 "renderControl({serial:'S9',transport:'ssh',ssh_ip:'192.168.13.40',wifi:1});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "SSH (developer)" in html and "192.168.13.40" in html, "did not use the server's mode/IP"
    assert "switchAdb(" in html, "an SSH watch was not offered the switch-to-ADB toggle"
    assert "192.168.2.15" not in html, "fell back to the default ADB IP for an SSH watch"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_clock_spinners_and_persistence(tmp_path):
    """The Settings tab opens with a Clock section: five scroll-wheel spinners, a
    Set-clock button (set_datetime) and Sync-from-host (moved from System). The
    dialled value must survive a poll re-render — re-deriving it from now each
    render would snap it back mid-adjust (the bug this pins)."""
    import json
    h = tmp_path / "clock.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlSerial='S9';ctlName='sk';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);"
                 "ctlDate={y:2026,mo:7,d:22,h:10,mi:5};ctlDateTouched=true;"
                 "ctlSettings['S9']={ok:true,settings:[]};"
                 "renderControl({});const first=global.__els['cc'].innerHTML;"
                 "ctlDateAdj('h',1);"                        # 10 -> 11, re-renders
                 "renderControl({});"                        # a poll re-render must keep it
                 "console.log(JSON.stringify({h:ctlDate.h,html:global.__els['cc'].innerHTML,"
                 "hasWheel:first.indexOf(\"ctlDateWheel(event,'y')\")>=0,"
                 "hasApply:first.indexOf('ctlDateApply(')>=0,"
                 "hasSync:first.indexOf('ccSyncTime(')>=0,"
                 "spins:(first.match(/class=\"spin-v\"/g)||[]).length}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["hasWheel"], "spinners do not react to the mouse wheel"
    assert o["hasApply"], "no Set-clock button wired to set_datetime"
    assert o["hasSync"], "Sync-from-host was not moved into the Clock section"
    assert o["spins"] == 5, "expected five spinners (hr min day mon year)"
    assert o["h"] == 11 and '<div class="spin-v">11</div>' in o["html"], \
        "the dialled spinner value did not survive a re-render"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_settings_quickpanel_icons_reflect_enable_state(tmp_path):
    """The Quick panel group renders one icon-in-a-circle per toggle, full when
    enabled and dimmed (no .on) when disabled, each with a name tooltip and a
    click that flips it to the opposite state."""
    import json
    h = tmp_path / "qp.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlSerial='S9';ctlName='sk';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);"
                 "ctlSettings['S9']={ok:true,settings:[],quickpanel:["
                 "{id:'wifiToggle',label:'Wifi',enabled:true,is_set:false},"
                 "{id:'musicButton',label:'Music',enabled:false,is_set:true}]};"
                 "renderControl({});"
                 "console.log(JSON.stringify(global.__els['cc'].innerHTML));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Quick panel" in html
    assert 'title="Wifi"' in html and 'title="Music"' in html, "toggle name tooltips missing"
    assert "quickpanelSet('wifiToggle',0)" in html, "enabled toggle should click to off"
    assert "quickpanelSet('musicButton',1)" in html, "disabled toggle should click to on"
    assert 'class="qpb on"' in html, "the enabled toggle is not shown active"
    assert 'class="qpb"' in html, "the disabled toggle is not shown dimmed"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_clock_tracks_now_until_a_spinner_is_dialed(tmp_path):
    """The clock shows the live time by default (re-seeds each render), and stops
    tracking the moment the user dials a spinner — otherwise a dialled arbitrary
    time would snap back to now (mo)."""
    import json
    h = tmp_path / "track.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlSerial='S9';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);ctlSettings['S9']={ok:true,settings:[]};"
                 "ctlDate={y:2000,mo:1,d:1,h:3,mi:3};ctlDateTouched=false;"
                 "renderControl({});"                       # untouched → re-seeds to now
                 "const trackedY=ctlDate.y;"
                 "ctlDateAdj('mi',1);"                       # dial → freeze
                 "const heldY=ctlDate.y, heldMi=ctlDate.mi;"
                 "renderControl({});renderControl({});"      # further polls must hold
                 "console.log(JSON.stringify({trackedY,touched:ctlDateTouched,"
                 "held:(ctlDate.y===heldY&&ctlDate.mi===heldMi)}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["trackedY"] >= 2020, "untouched clock did not track the current time"
    assert o["touched"] is True and o["held"] is True, "dialled time was not held"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_quickpanel_toggle_pulses_until_confirmed(tmp_path):
    """A clicked quick-panel toggle pulses (cmd-pending) until the settings
    refetch confirms the new state — and because the pulse is a render-applied
    pending flag, a full poll re-render can't wipe it mid-flight (the bug: the
    quickpanel toggles didn't pulse during exec)."""
    import json
    h = tmp_path / "qppulse.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"   # write never resolves → stays pending
                 "ctlSerial='S9';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);ctlSettings['S9']={ok:true,settings:[],"
                 "quickpanel:[{id:'wifiToggle',label:'Wifi',enabled:true,is_set:false}]};"
                 "quickpanelSet('wifiToggle',0);"           # pending + re-render
                 "const a=global.__els['cc'].innerHTML;"
                 "renderControl({});"                        # a poll re-render must keep the pulse
                 "const b=global.__els['cc'].innerHTML;"
                 "console.log(JSON.stringify({first:a.indexOf('cmd-pending')>=0,"
                 "afterPoll:b.indexOf('cmd-pending')>=0,pending:ctlPending.has('qp:wifiToggle')}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["first"] and o["pending"], "the toggle did not pulse on click"
    assert o["afterPoll"], "the pulse was wiped by a poll re-render"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_network_toggle_also_pulses_while_in_flight(tmp_path):
    """The Network tab's WiFi/BT toggles pulse in flight too — every Control
    Center toggle got the same pending-pulse, not just the quick panel (mo:
    recheck all buttons)."""
    import json
    h = tmp_path / "netpulse.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.fetch=()=>new Promise(()=>{});"
                 "ctlSerial='S9';ctlTab='net';ctlMode='adb';ctlSshIp='192.168.2.15';"
                 "ctlCache['S9']={serial:'S9',wifi:1,transport:'adb'};"
                 "ncToggle('wifi',0);"
                 "console.log(JSON.stringify({pulsing:global.__els['cc'].innerHTML.indexOf('cmd-pending')>=0,"
                 "pending:ctlPending.has('net:wifi')}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["pulsing"] and o["pending"], "the network toggle did not pulse in flight"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_view_hands_render_over_base_and_are_draggable(tmp_path):
    """A hands watch lays the real hour/minute SVG art over the hands-removed base,
    each rotated by valToAngle(value, offset) = (offset + 2·value) mod 360, and in
    Free mode each hand carries the drag handle. minute 45@100°→190°, hour
    135@110°→20° (380 mod 360)."""
    import json
    h = tmp_path / "hands.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.document.getElementById('devframe');"      # cache nodes
                 "global.document.getElementById('prodimg');"
                 "global.document.getElementById('devhands');"
                 "handsMode='free';handsVal={min:45,hr:135};"
                 "global.fetch=()=>Promise.resolve({json:()=>Promise.resolve("
                 "{ok:true,hands:{position:'45:135'},cal:{min_deg:100,hr_deg:110}})});"
                 "loadHands('S9','narwhal');"
                 "setTimeout(()=>{const el=global.__els['devhands']||{},"
                 "p=global.__els['prodimg']||{};"
                 "console.log(JSON.stringify({html:el.innerHTML||'',base:p.src||''}));"
                 "process.exit(0);},90);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert "rotate(190.0deg)" in o["html"] and "rotate(20.0deg)" in o["html"], o["html"][:300]
    assert "/api/watch-hand/narwhal/hour" in o["html"]
    assert "/api/watch-hand/narwhal/minute" in o["html"]
    assert "handsDown(event,'min')" in o["html"] and "handsDown(event,'hr')" in o["html"]
    assert o["base"].endswith("/api/watch-hand/narwhal/base")   # handless base swapped in


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_control_center_weather_section(tmp_path):
    """The System tab shows a Weather section: the mapped icon, today's high/low
    and city, a Sync-to-watch button and a location setter; no location → setter
    only. Icon id 211 (thunderstorm) → the 'thunderstorm' art."""
    import json
    h = tmp_path / "wx.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlTab='set';ccOpen=new Set(['set-clock','set-sound','set-display','set-nightstand','set-units','set-appearance','set-quickpanel','set-usb','set-weather']);"
                 "wxData={ok:true,location:{city:'Berlin, DE'},days:[{id:211,min_c:14,max_c:19}]};"
                 "renderControl({serial:'S9',kernel:'3.18',os:'AsteroidOS'});"
                 "const withWx=global.__els['cc'].innerHTML;"
                 "wxData={ok:true,location:null,days:[]};"
                 "renderControl({serial:'S9',kernel:'3.18',os:'AsteroidOS'});"
                 "console.log(JSON.stringify({withWx,noLoc:global.__els['cc'].innerHTML}));"
                 "process.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert "Weather" in o["withWx"] and "Berlin, DE" in o["withWx"]
    assert "wxSync('S9')" in o["withWx"], "no Sync-to-watch button"
    assert "14" in o["withWx"] and "19" in o["withWx"], "temps missing"
    assert 'id="wxcity"' in o["withWx"] and 'class="wxi"' in o["withWx"]
    assert "no location set" in o["noLoc"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_view_hands_mode_panel(tmp_path):
    """The live view of a hands watch (narwhal) gets the 3-mode control bar —
    Time / Free / Calibrate — with Time (the default) offering Set-watch-to-time
    and the raw driver counter shown."""
    import json
    h = tmp_path / "handctl.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.document.getElementById('devframe');"
                 "global.document.getElementById('prodimg');"
                 "global.document.getElementById('devhands');"
                 "global.document.getElementById('wimghands');"
                 "global.fetch=()=>Promise.resolve({json:()=>Promise.resolve("
                 "{ok:true,hands:{position:'132:41'},cal:{min_deg:102,hr_deg:108}})});"
                 "loadHands('S9','narwhal');"
                 "setTimeout(()=>{const el=global.__els['wimghands']||{};"
                 "console.log(JSON.stringify({html:el.innerHTML||''}));process.exit(0);},90);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    html = json.loads(r.stdout.strip().splitlines()[-1])["html"]
    assert "handsSetMode('time')" in html and "handsSetMode('free')" in html
    assert "handsSetMode('calibrate')" in html
    assert "handsToTime()" in html          # Time is the default mode
    assert "132:41" in html                 # raw driver counter surfaced


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hands_calibrate_match_learns_the_offset(tmp_path):
    """Calibrate commands the reference minute=0/hour=90, so a matched web angle
    teaches offset = matched - value·2: match {min:100, hr:280} → offsets
    100 / (280-180)=100, POSTed to the hands-cal route."""
    import json
    h = tmp_path / "cal.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nlet _u=[];global.fetch=(u,o)=>{_u.push(u);return Promise.resolve("
                 "{json:()=>Promise.resolve({ok:true,cal:{min_deg:100,hr_deg:100}})});};"
                 "_handsSerial='S9';handsMatch={min:100,hr:280};"
                 "handsCalSave();"
                 "setTimeout(()=>{console.log(JSON.stringify(_u));process.exit(0);},60);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    urls = json.loads(r.stdout.strip().splitlines()[-1])
    assert any("/hands-cal/100.0/100.0" in u for u in urls), urls


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_hands_choreography_commands_the_motors(tmp_path):
    """Choreography presets drive motor_move_all through the calibrated space.
    With offset 100°: Overlap (both→12/0°) = value 130; Oppose (min→12/0°=130,
    hour→6/180°=40)."""
    import json
    h = tmp_path / "chor.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nlet _u=[];global.fetch=(u,o)=>{_u.push(u);return Promise.resolve("
                 "{json:()=>Promise.resolve({ok:true})});};"
                 "_handsSerial='S9';handsCal={min_deg:100,hr_deg:100};"
                 "handsOverlap();handsOppose();"
                 "setTimeout(()=>{console.log(JSON.stringify(_u));process.exit(0);},60);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    urls = json.loads(r.stdout.strip().splitlines()[-1])
    assert any("/hands-move/130/130" in u for u in urls), urls    # Overlap at 12
    assert any("/hands-move/130/40" in u for u in urls), urls     # Oppose 12-6 line


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_view_window_drags_by_its_title(tmp_path):
    """The live-view overlay drags by its title bar like the Control Center — the
    header wires wimgDragStart (handy for parking it beside the watch to calibrate)."""
    import json
    h = tmp_path / "wdrag.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nglobal.document.getElementById('wimg');"
                 "openWatchImg('narwhal','S9',{stopPropagation(){},clientX:10,clientY:10},1,'400x400');"
                 "const html=global.__els['wimg'].innerHTML;"
                 "global.__els['wimg'].getBoundingClientRect=()=>({left:100,top:100});"
                 "wimgDragStart({target:{classList:{contains:()=>false}},"
                 "clientX:150,clientY:130,preventDefault(){}});"
                 "global.__h.mousemove({clientX:300,clientY:260});"   # the shared handler
                 "console.log(JSON.stringify({html,moved:_wimgMoved,"
                 "left:global.__els['wimg'].style.left}));process.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert 'onmousedown="wimgDragStart(event)"' in o["html"]
    assert o["moved"] is True                 # a title drag pins the window
    assert o["left"] == "250px"               # the shared mousemove moved it (300-50)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_panel_not_rebuilt_while_typing_in_a_field(tmp_path):
    """A 3s poll re-render must not rebuild the panel out from under a focused
    text field (the weather city input) — it dropped focus and the typed text
    (mo). renderControl skips while a CC input has focus."""
    import json
    h = tmp_path / "typing.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nctlSerial='S9';ctlTab='vit';"
                 "const ccEl=global.document.getElementById('cc');"
                 "ccEl.innerHTML='TYPING-IN-PROGRESS';ccEl.contains=()=>true;"
                 "global.document.activeElement={tagName:'INPUT'};"   # a focused input in cc
                 "renderControl({serial:'S9',kernel:'3.18',os:'AsteroidOS'});"
                 "const held=global.__els['cc'].innerHTML;"
                 "global.document.activeElement=null;"                 # blurred → poll may render
                 "renderControl({serial:'S9',kernel:'3.18',os:'AsteroidOS'});"
                 "console.log(JSON.stringify({held,after:global.__els['cc'].innerHTML}));"
                 "process.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["held"] == "TYPING-IN-PROGRESS", "panel was rebuilt while an input was focused"
    assert "Vitals" in o["after"], "panel never re-rendered after the field blurred"


def test_cc_top_covers_the_row_and_bottom_matches_when_tall(tmp_path):
    """The Control Center anchors to its ROW: the header (which repeats the
    codename) is centred on the row so it covers it. A panel too tall to fit
    below instead matches its BOTTOM to the row's bottom, rather than flipping
    to an arbitrary gap above (mo). Planted-bug: drop the overflow branch and
    the tall case returns a top that runs off the viewport."""
    import json
    h = tmp_path / "top.js"
    h.write_text(_DOM_CAPTURE + JS +
                 "\nconsole.log(JSON.stringify({"
                 "fits:ccTop(300,32,32,400,900),"          # room below → header on row
                 "tall:ccTop(700,32,32,500,900),"          # would overflow → bottom-match
                 "clamped:ccTop(20,32,32,880,900)}));"     # taller than the room above
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    o = json.loads(r.stdout.strip().splitlines()[-1])
    assert o["fits"] == 300, "header should sit exactly on the row when it fits"
    assert o["tall"] == 700 + 32 - 500, "tall panel should bottom-match the row"
    assert o["clamped"] >= 8, "never place above the viewport top"


def test_every_tab_fetches_the_same_way_however_it_is_reached():
    """A Control Center tab can be reached two ways: opening the panel onto it
    (openControl) or switching to it (ctlTabTo). Both must kick off the same
    fetches, or a tab silently works one way and not the other.

    That is exactly how the WiFi provisioning button vanished: the AP fetch was
    wired into openControl only, so the button appeared when the panel was
    opened from the ADB badge and never when the Network tab was clicked.

    Planted-bug: drop a tab from either function and this fails.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent
           / "asteroid_docking_bay" / "webtemplate.py").read_text()

    def tabs_of(fn_marker, var):
        i = src.index(fn_marker)
        body = src[i:src.index("\nfunction ", i + 10)]
        return set(re.findall(rf"{var}==='(\w+)'", body))

    on_open = tabs_of("cc.style.display='block';", "ctlTab")
    on_switch = tabs_of("function ctlTabTo(tab){", "tab")
    missing = on_open - on_switch
    extra = on_switch - on_open
    assert not missing and not extra, (
        f"tab fetches disagree — only on open: {sorted(missing)}; "
        f"only on switch: {sorted(extra)}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wanze_shows_as_a_bug_in_the_three_places_it_matters(tmp_path):
    """The probe has to be visible without being another row of clutter, so it
    reuses slots that were already redundant: the battery-full tick (the gauge
    beside it already says full) and the trailing last-seen text."""
    import json
    h = tmp_path / "wanze.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst full=p=>mkstrip(Object.assign("
                 "{codename:'x',serial:'S9',adb:'device',charge_status:'Full'},p),24);"
                 "\nconst away=p=>mkstrip(Object.assign("
                 "{codename:'x',serial:'S9',adb:null,last_live_ts:1000},p),24);"
                 "\nconsole.log(JSON.stringify({"
                 "plainFull:full({}),bugFull:full({wanze:true}),"
                 "plainAway:away({}),bugAway:away({wanze_known:true})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    # Without wanze the old behaviour is untouched.
    assert "&#10003;" in out["plainFull"], "the plain full-battery tick is gone"
    assert 'class="lastseen"' in out["plainAway"], "plain last-seen text is gone"

    # With wanze the tick becomes a green bug, and says both things.
    assert "&#10003;" not in out["bugFull"], "the tick should be replaced, not joined"
    assert 'class="sdot on"' in out["bugFull"], "the bug should keep the green dot styling"
    assert "wanze detected" in out["bugFull"] and "battery full" in out["bugFull"]

    # An ABSENT watch carrying the probe shows an amber bug instead of the age,
    # which is exactly when the probe is working and nothing else would say so.
    assert 'class="sdot warn wanze"' in out["bugAway"], "absent+wanze is not an amber bug"
    assert "wanze present, last seen" in out["bugAway"]
    assert 'class="lastseen"' not in out["bugAway"], "the age text should be replaced"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_wanze_run_claims_the_connection_cell(tmp_path):
    """A run in progress must be impossible to miss — using the watch for
    anything else voids hours of measurement."""
    import json
    h = tmp_path / "probing.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "run:mkadbrow({codename:'x',serial:'S9',adb:null,"
                 "wanze_probing:{since:1000}}),"
                 "drain:mkadbrow({codename:'x',serial:'S9',adb:null,"
                 "drain:{active:true}})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert "wanze probing" in out["run"]
    assert "voids the run" in out["run"], "the pill does not say why it matters"
    # The drain pill is untouched by this.
    assert "drain test" in out["drain"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_wanze_run_on_a_drain_test_still_names_the_drain_test(tmp_path):
    """A wanze run is IMPLEMENTED as a drain test, so both pills claim the same
    cell. The wanze one wins the label, but dropping the drain state entirely
    would hide which consumers are under test."""
    import json
    h = tmp_path / "both.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({both:mkadbrow({codename:'x',"
                 "serial:'S9',adb:null,wanze_probing:{since:1000},"
                 "drain:{active:true,features:{}}})}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])["both"]
    assert "wanze probing" in out, "the run must claim the label"
    assert "drain test running" in out, "the drain test vanished from the tooltip"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_ssh_watch_without_an_allocation_reads_as_an_error(tmp_path):
    """We do not have an address for it. It is EXPECTED on the shared default,
    but nothing verified that, and printing the guess would make a row nothing
    can reach look identical to a working one — which is how the fault went
    unnoticed. So the state renders as an error and the guess stays in the
    tooltip, labelled."""
    import json
    h = tmp_path / "sshpill.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconsole.log(JSON.stringify({"
                 "own:mkadb('ssh',null,'asteroidos','S9','192.168.13.40','dory'),"
                 "bad:mkadb('ssh',null,'asteroidos','S9',null,'dory')}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "192.168.13.40" in out["own"] and "its own address" in out["own"]
    assert "noaddr" not in out["own"], "a reachable watch must not read as an error"

    # The broken one must LOOK broken.
    assert 'class="cbadge ssh noaddr"' in out["bad"]
    assert "no address" in out["bad"], "the error state is not stated"
    # The unverified guess must not be presented as the watch's address.
    assert "unverified" in out["bad"], "the guess is not labelled as a guess"
    assert "192.168.2.15" not in out["bad"].split("onclick=")[1].split("title=")[0], \
        "an unverified address was passed to the Network Center as fact"


# ── the Machine Room (icecc compile nodes) ───────────────────────────────────

_MR_LIVE = ("{netname:'asteroid',scheduler:'192.168.176.164',reachable:true,"
            "stale:false,building:false,slots:32,jobs_used:0,uptime_s:3602,"
            "nodes:[{host:'mo-e15-eos',ip:'192.168.176.164',arch:'x86_64',"
            "speed:0.0,jobs_used:0,jobs_max:14,load:118},"
            "{host:'mo-w541-eos',ip:'192.168.176.21',arch:'x86_64',"
            "speed:2.5,jobs_used:6,jobs_max:8,load:900}]}")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_machine_room_is_completely_absent_without_a_cluster(tmp_path):
    import json
    """Most machines running a-d-b have never heard of icecream. A host that is
    not a cluster member must render NOTHING — not an empty frame, not a
    placeholder, not an error strip."""
    h = tmp_path / "mr_absent.js"
    h.write_text(_DOM_STUBS + JS +
                 "\nconst rows=[];"
                 "renderMachineRoom(null,rows);"
                 "renderMachineRoom(undefined,rows);"
                 "renderMachineRoom({nodes:[]},rows);"
                 "console.log(JSON.stringify({n:rows.length}));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    assert json.loads(r.stdout)["n"] == 0, "an empty Machine Room was rendered"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_machine_room_shows_busy_and_idle_nodes_differently(tmp_path):
    import json
    """A node doing work, a node deliberately idle and a node we cannot see are
    three different things. Collapsing them into two is how a cluster that has
    silently stopped distributing still looks fine."""
    h = tmp_path / "mr_live.js"
    h.write_text(_DOM_STUBS + JS +
                 f"\nconst rows=[];renderMachineRoom({_MR_LIVE},rows);"
                 "console.log(JSON.stringify(rows));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    rows = json.loads(r.stdout)
    html = "".join(rows)
    assert len(rows) == 3, "expected a header plus one row per node"
    assert "Machine Room" in rows[0] and "asteroid" in rows[0]
    assert "mo-e15-eos" in html and "mo-w541-eos" in html
    # the busy node is marked building; the idle one is not
    assert "building 6" in html
    assert "idlerow" in rows[1], "the idle node was not distinguished"
    assert "idlerow" not in rows[2], "the working node was dimmed as idle"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_fresh_cluster_does_not_look_broken(tmp_path):
    import json
    """speed stays 0.00 until a node has completed real jobs, so a newly built
    cluster reports zeros everywhere. Printing a bare 0.00 invites reading it
    as a fault on day one."""
    h = tmp_path / "mr_fresh.js"
    h.write_text(_DOM_STUBS + JS +
                 f"\nconst rows=[];renderMachineRoom({_MR_LIVE},rows);"
                 "console.log(JSON.stringify(rows));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    rows = json.loads(r.stdout)
    assert "not rated yet" in rows[1], "speed 0.00 shown bare"
    assert "speed 2.50" in rows[2], "a rated node lost its score"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_unreachable_scheduler_still_shows_the_nodes(tmp_path):
    import json
    """THE STATE THAT MATTERS: unreachable means builds are running local right
    now. Hiding the panel would remove the evidence at the moment it is most
    worth having."""
    h = tmp_path / "mr_dead.js"
    dead = _MR_LIVE.replace("reachable:true", "reachable:false")
    h.write_text(_DOM_STUBS + JS +
                 f"\nconst rows=[];renderMachineRoom({dead},rows);"
                 "console.log(JSON.stringify(rows));"
                 "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    rows = json.loads(r.stdout)
    assert len(rows) == 3, "the cluster vanished when the scheduler went away"
    assert "unreachable" in rows[0]
    assert "deadrow" in rows[1] and "deadrow" in rows[2]
    assert "LOCAL" in "".join(rows), "the practical meaning is not stated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_over_subscribed_node_does_not_look_merely_full(tmp_path):
    """Observed live on 2026-08-08: under load the scheduler hands nodes MORE
    jobs than they advertise — 15/14, 11/10. Computing the bar width as
    used/max overflows, gets clipped, and makes 15/14 render identically to
    8/8. Two different states, one picture."""
    import json
    mr = ("{netname:'asteroid',reachable:true,building:true,slots:22,jobs_used:23,"
          "nodes:[{host:'full',ip:'1.1.1.1',arch:'x86_64',speed:33.1,"
          "jobs_used:8,jobs_max:8,load:485},"
          "{host:'over',ip:'1.1.1.2',arch:'x86_64',speed:46.05,"
          "jobs_used:15,jobs_max:14,load:276}]}")
    h = tmp_path / "mr_over.js"
    h.write_text(_DOM_STUBS + JS +
                 f"\nconst rows=[];renderMachineRoom({mr},rows);"
                 "console.log(JSON.stringify(rows));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    rows = json.loads(r.stdout)
    full, over = rows[1], rows[2]
    assert "over" not in full.split('class="slots')[1][:20], "a full node was flagged over"
    assert "over" in over.split('class="slots')[1][:20], "over-subscription not shown"
    # the bar is clamped, so no width above 100% leaks into the markup
    assert "width:100%" in over and "width:107%" not in over
    # and the honest raw count survives
    assert "15/14" in over


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_intermittently_fed_node_does_not_strobe(tmp_path):
    """Measured live 2026-08-08: 45 one-second samples with the cluster busy in
    every single one, while the w541 alternated 0/1 jobs throughout. Rendering
    each sample literally makes a working node flicker between green and idle,
    which reads as a fault and buries the state that matters.

    Smoothing, not lying: a node that genuinely stops must still go idle."""
    import json
    def mr(jobs):
        return ("{netname:'asteroid',reachable:true,building:true,slots:8,jobs_used:%d,"
                "nodes:[{host:'w541',ip:'1.1.1.1',arch:'x86_64',speed:33.9,"
                "jobs_used:%d,jobs_max:8,load:485}]}" % (jobs, jobs))
    h = tmp_path / "mr_strobe.js"
    h.write_text(_DOM_STUBS + JS + f"""
const out=[];
let rows=[];renderMachineRoom({mr(1)},rows);out.push(rows[1]);   // has a job
rows=[];renderMachineRoom({mr(0)},rows);out.push(rows[1]);       // gap
rows=[];renderMachineRoom({mr(1)},rows);out.push(rows[1]);       // job again
// a node that really stopped: push the remembered time far into the past
mroomLastBusy['w541']=Date.now()-60000;
rows=[];renderMachineRoom({mr(0)},rows);out.push(rows[1]);
console.log(JSON.stringify(out));
process.exit(0);
""")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    busy1, gap, busy2, stopped = json.loads(r.stdout)
    assert "building" in busy1
    assert "idlerow" not in gap, "a one-poll gap strobed the node to idle"
    assert "building" in gap
    assert "building" in busy2
    assert "idlerow" in stopped, "a node that genuinely stopped never went idle"
    # the live count is always the current sample, never the smoothed one
    assert "0/8" in gap


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_node_temperature_only_alarms_at_the_thermal_limit(tmp_path):
    """These are LAPTOPS. mo's own figures: the e15 is fine below ~98C, the
    older w541 is spec'd to 100C, and the p14s drops to 60C on full load once
    its fans engage. A conventional 80C threshold would sit permanently red on
    two of three nodes and never flag the third — an alarm that is always on is
    not an alarm. Only the limit itself, where throttling starts, is coloured."""
    import json
    def node(c):
        return ("{host:'n',ip:'1.1.1.1',arch:'x86_64',speed:40,jobs_used:1,"
                "jobs_max:8,load:300,temp_c:%s,temp_sensor:'coretemp'}" % c)
    h = tmp_path / "mr_temp.js"
    h.write_text(_DOM_STUBS + JS + """
const out=[];
[60,88,97,98,101].forEach(c=>{
  const rows=[];
  renderMachineRoom({netname:'a',reachable:true,building:true,slots:8,jobs_used:1,
                     nodes:[%s]},rows);
  out.push(rows[1]);
});
// a node with no SSH access has no temperature at all
const rows=[];
renderMachineRoom({netname:'a',reachable:true,building:true,slots:8,jobs_used:1,
  nodes:[{host:'n',ip:'1.1.1.1',arch:'x86_64',speed:40,jobs_used:1,jobs_max:8,load:300}]},rows);
out.push(rows[1]);
console.log(JSON.stringify(out));
process.exit(0);
""".replace("%s", "Object.assign({},JSON.parse(JSON.stringify(" + node("c") .replace("temp_c:c","temp_c:0") + ")),{temp_c:c})"))
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, r.stderr[:400]
    cool, warmish, busy97, near98, over101, nossh = json.loads(r.stdout)
    for row in (cool, warmish, busy97):
        assert "mtemp warm" not in row and "mtemp hot" not in row, \
            "a normal laptop compile temperature was coloured as a problem"
    assert "60&deg;C" in cool or "60°C" in cool
    assert "mtemp warm" in near98, "98C did not read as near the limit"
    assert "mtemp hot" in over101, "101C did not read as throttling"
    assert "throttling" in over101
    assert "mtemp" not in nossh, "a node without SSH invented a temperature"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_charge_countdown_updates_inside_the_pill(tmp_path):
    """tickCountdown used to set innerHTML on the whole battery cell every
    second, replacing the clickable pill with a bare span — so the battery
    control was dead during exactly the operation you would want to interrupt,
    and any menu anchored to that node was re-created under itself.

    The countdown must write into the pill, leaving the pill intact."""
    h = tmp_path / "ctdn.js"
    h.write_text(
        _DOM_STUBS + JS +
        # A cell holding a rendered pill, as render() produces it.
        "\nconst cell={_h:'<button class=\"cbadge bat warn\">"
        "<span class=\"dim ctdn\">9m00s</span></button>',"
        "  _t:{textContent:'9m00s'},"
        "  set innerHTML(v){this._h=v;this._wiped=true;},"
        "  get innerHTML(){return this._h;},"
        "  querySelector(sel){return sel==='.ctdn'?this._t:null;}};"
        "\nglobal.document.getElementById=(id)=>id==='bat-1-2:1'?cell:null;"
        "\nchargeEnd['1-2:1']=Date.now()+65000;"
        "\ntickCountdown();"
        "\nconsole.log(JSON.stringify({wiped:!!cell._wiped,txt:cell._t.textContent,"
        "  html:cell._h}));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    import json
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert not out["wiped"], \
        "the countdown replaced the whole battery cell — the pill is destroyed"
    assert "cbadge bat" in out["html"], "the pill did not survive the tick"
    assert out["txt"] != "9m00s", "the countdown never updated"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_every_menu_names_the_watch_it_will_act_on(tmp_path):
    """Four floating menus anchored to 24px dots across 28 dense rows is four
    chances to command the wrong watch, and the menu itself said nothing about
    which one it held. Each opener must lead with an identity strip."""
    import json
    h = tmp_path / "ident.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nconst seen={};"
        "\nopenMenu=function(ev,html){seen[Object.keys(seen).length]=html;};"
        "\nmenuExecute({},'1-2:1',false,false,false,true,false,'S1',false,'device','',0,false,'skipjack');"
        "\nmenuPwr({},'1-2:1',false,false,false,true,false,'skipjack');"
        "\nmenuWear({},'1-2:1',false,'S1',0,'skipjack');"
        "\nmenuFb({},'1-6:3',true);"
        "\nconsole.log(JSON.stringify(seen));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert len(out) == 4, f"expected four menus, got {len(out)}"

    for i, html in out.items():
        assert 'class="menuid"' in html, f"menu {i} opens with no identity strip"
    for i in ("0", "1", "2"):
        assert "skipjack" in out[i], f"menu {i} does not name the watch"
        assert "1-2:1" in out[i], f"menu {i} does not name the slot"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_an_unmapped_bootloader_port_can_still_be_commanded(tmp_path):
    """A watch that no longer boots far enough to be identified is the one that
    most needs Continue boot, Recovery and a fastboot report — and an unmapped
    row's actions were Onboard and hide, nothing else. Those commands address
    the port by PATH, not by serial, so they work with no mapping at all.

    Flash stays out on purpose: it resolves a codename from the mapping."""
    import json
    h = tmp_path / "fbmenu.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nlet menu='';openMenu=function(ev,html){menu=html;};"
        "\nconst btn=fbMenuBtn({adb:'fastboot',power:true},'1-6:3');"
        "\nconst none=fbMenuBtn({adb:null,power:true},'1-6:3');"
        "\nmenuFb({},'1-6:3',true);"
        "\nconsole.log(JSON.stringify({btn:btn,none:none,menu:menu}));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "menuFb(event,'1-6:3'" in out["btn"], \
        "a bootloader watch on an unmapped port still has no way in"
    assert out["none"] == "", "a port with no fastboot device grew a stray button"
    for item in ("Continue boot", "Recovery", "Fastboot report"):
        assert item in out["menu"], f"{item!r} unreachable on an unmapped port"
    assert "Flash" not in out["menu"], \
        "Flash needs a codename mapping and cannot run on an unmapped port"

    # And it must be WIRED into the empty row, not merely defined — asserting
    # on the builder alone lets the button vanish from the render unnoticed,
    # which is exactly how .btn-ref's pulse died.
    assert JS.count("fbMenuBtn(") >= 2, "fbMenuBtn is defined but never called"
    assert "+fbMenuBtn(p,slot)+" in JS, \
        "the unmapped row no longer renders its bootloader menu button"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_held_watch_offers_no_actions_the_server_will_refuse(tmp_path):
    """The badge existed and _refuse_if_busy existed; only the menu never
    consulted them, so a watch held for a dump rendered a fully-enabled menu
    and the refusal arrived as a toast AFTER the click.

    Disabled with the reason, not hidden — hiding would imply the watch cannot
    do these things, which is false."""
    import json
    h = tmp_path / "heldmenu.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nlet menu='';openMenu=function(ev,html){menu=html;};"
        "\nmenuExecute({},'1-2:1',false,false,false,true,false,'S1',false,'device','',0,'nemo','dump');"
        "\nconst held=menu;"
        "\nmenuExecute({},'1-2:1',false,false,false,true,false,'S1',false,'device','',0,'nemo','');"
        "\nconsole.log(JSON.stringify({held:held,free:menu}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "dump" in out["held"], "the menu does not say what holds the watch"
    assert "refused" in out["held"], "the reason is not stated"
    assert "disabled" in out["held"], "held actions are still clickable"
    assert "onclick" not in out["held"], \
        "a held watch still offers a live action the server will reject"

    # The guard must not disable the menu: an unheld watch is unchanged.
    assert "onclick" in out["free"] and "Checkout" in out["free"]


def test_the_row_passes_the_lock_to_the_menu():
    """menuExecute can only honour the lock if the row hands it over — the whole
    bug was a guard that existed on both sides of a value nobody passed."""
    assert "(p.held&&p.held.kind)" in JS, \
        "the row no longer passes the operation lock into the menu"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_a_destructive_item_arms_before_it_commits(tmp_path):
    """Flash nightly wiped a watch on one click with no confirmation, while its
    own siblings 2.1 and 2.0 went through a confirm. Same wipe, opposite
    treatment — and the unguarded one is the daily driver.

    The fix is not a fourth modal. This UI already fires five blocking confirms
    in a session, which is how a MISSING one went unnoticed: a dialog dismissed
    by reflex is not a decision. A destructive item arms on the first click,
    says so on the control, and only commits on the second."""
    import json
    h = tmp_path / "arm.js"
    h.write_text(
        _DOM_STUBS + JS +
        # a button stub that records its own state, plus a menu to close
        "\nlet fired=0;global.__fire=()=>{fired++;};"
        "\nconst sibling={dataset:{},textContent:'Flash 2.1',closest(){return holder;}};"
        "\nconst tgt={dataset:{},textContent:'Flash nightly',closest(){return holder;}};"
        "\nconst holder={querySelectorAll(){return [sibling].filter(b=>b.dataset.armed==='1');}};"
        "\ncloseMenu=function(){};"
        "\narmGo(tgt,'__fire()','wipe + flash nightly');"
        "\nconst afterFirst={fired:fired,label:tgt.textContent,armed:tgt.dataset.armed};"
        "\narmGo(tgt,'__fire()','wipe + flash nightly');"
        "\nconsole.log(JSON.stringify({afterFirst:afterFirst,fired:fired}));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert out["afterFirst"]["fired"] == 0, \
        "the first click wiped the watch — arming did not happen"
    assert out["afterFirst"]["armed"] == "1"
    assert "click again" in out["afterFirst"]["label"], \
        "the armed state is not announced on the control itself"
    assert "wipe" in out["afterFirst"]["label"], "the armed label does not say what it will do"
    assert out["fired"] == 1, "the second click did not commit"


def test_every_wipe_is_armed_and_none_is_a_bare_click():
    """The contract, checked over the built menu rather than trusted: every item
    in the wipes group goes through the arming path. A destructive item wired
    straight to its handler is the defect this closes."""
    import re as _re
    src = _WEB_TEMPLATE
    grp = src[src.index("function grpWipe("):src.index("function menuExecute(")]
    # Each live item must be built with midanger, never plain mi(...) with a fn.
    bare = _re.findall(r"mi\('[^']*',\s*[\"'][^\"']+[\"'],\s*`[^`]+`", grp)
    assert not bare, f"a wipe is wired directly to its handler, unarmed: {bare}"
    for item in ("Flash nightly", "Flash 2.1", "Flash 2.0", "Restore data"):
        assert f"midanger" in grp and item in grp, f"{item} missing from the wipes group"
    # And the old unguarded call must be gone.
    assert "doFlV(" not in src, \
        "doFlV's confirm is superseded by arming; leaving both is two mechanisms"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_sweep_can_be_aborted_without_leaving_the_rig_dark(tmp_path):
    """THE TRAP: declining the sweep's second confirm returned after /prepare
    had already cut VBUS on every socket, so the rig sat fully dark with
    nothing on screen admitting it — and a watch that loses VBUS without a
    delivered poweroff keeps running on battery, invisible to the host. That is
    the sturgeon-to-0% failure, fleet-wide, reachable by pressing Cancel.

    No modal can express "the rig is dark right now". The control carries the
    state instead, and offers the way back."""
    import json
    h = tmp_path / "sweep.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nconst idle=sweepControl();"
        "\nsweepState='armed';sweepPorts=13;"
        "\nconst armed=sweepControl();"
        "\nsweepState='running';"
        "\nconst running=sweepControl();"
        "\nconsole.log(JSON.stringify({idle,armed,running}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "sweepArm()" in out["idle"], "no way to start a sweep"
    # The armed state must SAY the rig is down and offer a way out.
    assert "sockets are OFF" in out["armed"], \
        "the armed state does not admit that every socket is powered down"
    assert "13" in out["armed"], "the armed state does not say how many"
    assert "sweepRestore()" in out["armed"], \
        "no way back from armed — this is the trap that drained the fleet"
    assert "sweepRun()" in out["armed"]
    assert "sweepSkip()" in out["running"]

    # And the old fire-and-forget path is gone for good.
    assert "doOnboardSweep" not in JS, "the fire-and-forget sweep is back"


def test_the_sweep_left_the_status_row_for_the_registry():
    """The top row carries persistent UI state — view toggles and the USB-mode
    policy. A sweep is a rare one-shot operation and does not belong beside
    them; it lives with the other fleet-scope surface, the registry panel."""
    assert 'id="sweeplink"' not in _WEB_TEMPLATE, \
        "the sweep is back in the status row"
    assert "sweepControl()" in _WEB_TEMPLATE and 'class="reg-foot"' in _WEB_TEMPLATE, \
        "the registry panel does not host the sweep"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_port_toggle_opens_a_scoped_menu_instead_of_cutting_power(tmp_path):
    """The port toggle is the biggest, most obvious control in the row and it
    is the UNSAFE one: cutting VBUS does not stop a running watch, it keeps
    draining on battery where the host cannot see it. The small grey dot beside
    it does the graceful halt. Nothing said which was which.

    The toggle now opens a menu like the dot does, offering only PORT-scope
    actions and naming that scope. Cutting power to a watch that is UP arms
    first, because that is the case that strands a watch on battery."""
    import json
    h = tmp_path / "pwr.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nlet menu='';openMenu=function(ev,html){menu=html;};"
        "\nconst ev={stopPropagation(){}};"
        "\npwrMenu(ev,'1-2:1',true,false,'skipjack',true);const live=menu;"
        "\npwrMenu(ev,'1-2:1',true,false,'skipjack',false);const dark=menu;"
        "\npwrMenu(ev,'1-2:1',false,false,'skipjack',false);const off=menu;"
        "\nconsole.log(JSON.stringify({live,dark,off}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    # Scope is stated, and only port actions are offered.
    for key in ("live", "dark", "off"):
        assert "VBUS" in out[key], f"{key}: the menu does not name its scope"
        assert "Cycle power" in out[key], f"{key}: no cycle"
        for watch_action in ("Reboot", "Bootloader", "Charge", "Drain"):
            assert watch_action not in out[key], \
                f"{key}: {watch_action} is a WATCH action and must not be here"

    # Cutting power to a LIVE watch arms; to a dark one it is a plain item.
    assert "armGo(" in out["live"], \
        "cutting VBUS on a running watch commits on one click — it strands it on battery"
    assert "armGo(" not in out["dark"], "arming a port with no live watch is noise"
    assert "Power on" in out["off"] and "Power off" not in out["off"]

    # And the warning names the real hazard.
    assert "keeps draining" in out["live"]


def test_no_css_class_is_styled_but_never_emitted():
    """THE AUDIT'S EXHIBIT. `.btn-ref.pulsing` styled a refresh pulse that
    nothing ever wore: the row emits `btn ex pulsing`, `btn-ref` appeared
    exactly once in the file — inside its own rule — and the pulse had been
    dead since a rename. Nothing failed, because no test owned it.

    A styled class that is never emitted is either dead weight or, worse,
    DECOY vocabulary: `.hrb` was amber-for-reboot while the live
    `.menu-item.rb` is orange-for-reboot, so anyone normalising from the CSS
    would have picked the wrong one.

    Scoped to the single-word state/variant classes this rot appeared in;
    structural and compound selectors are exempt."""
    import re as _re
    css = _WEB_TEMPLATE[:_WEB_TEMPLATE.index("</style>")]
    body = _WEB_TEMPLATE[_WEB_TEMPLATE.index("</style>"):]

    # every class token that appears anywhere as an emitted class or via JS
    emitted = set()
    for m in _re.finditer(r'class="([^"]*)"', _WEB_TEMPLATE):
        for tok in _re.split(r"[\s$]+", m.group(1)):
            tok = tok.strip("{}`'\"+()")
            if tok and not tok.startswith("$"):
                emitted.add(tok)
    for m in _re.finditer(r"classList\.\w+\(\s*'([^']+)'", body):
        emitted.add(m.group(1))
    # note the \s* — classes are often concatenated as ' foo' with a leading
    # space inside the quotes (`${cond?' foo':''}`), which a tight pattern misses
    for m in _re.finditer(r"'\s*((?:[a-z][\w-]*\s+)*[a-z][\w-]*)\s*'", body):
        for tok in m.group(1).split():
            emitted.add(tok)

    # simple single-class rules only: .foo{...} / .foo.bar{...} / .foo:hover{...}
    styled = set()
    for m in _re.finditer(r"^\s*\.([a-z][\w-]*)[^{;}]*\{", css, _re.M):
        styled.add(m.group(1))

    orphans = sorted(c for c in styled - emitted if "-" in c or len(c) <= 6)
    assert not orphans, (
        f"CSS classes styled but never emitted: {orphans} — either dead weight "
        f"or decoy vocabulary that contradicts the live rules")


def test_the_status_row_groups_by_kind_and_carries_no_actions():
    """The row mixed four kinds of control in one bullet-separated list wearing
    one costume: view toggles, a fleet-wide policy that rewrites config, panel
    openers, and the most disruptive action in the app — the onboard sweep,
    styled in the same green that means healthy/connected/charging in fifteen
    other places.

    It now carries persistent UI STATE only, grouped by kind, with no inline
    colours. One-shot actions moved out."""
    hdr = _WEB_TEMPLATE[_WEB_TEMPLATE.index('<p class="meta">'):]
    hdr = hdr[:hdr.index("</p>")]

    assert hdr.count('class="mgrp"') >= 2, "the row is not grouped by kind"
    assert 'class="vtog"' in hdr and 'class="mopen"' in hdr and 'class="mpref"' in hdr, \
        "view toggles, panel openers and the policy share one costume again"
    assert "style=" not in hdr, "inline colours are back in the status row"
    assert "#3fb950" not in hdr, \
        "green is the healthy/on colour and must not appear in the status row"
    assert "onboard sweep" not in hdr, \
        "a one-shot action is back in a row meant for persistent state"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_view_toggles_carry_state_in_the_class_not_the_label(tmp_path):
    """Two adjacent toggles disagreed about what their label meant: one named
    the ACTION ('show drain history' → click → 'hide…'), the USB preference
    beside it named the STATE ('prefer ADB', where clicking switches away from
    the label). One label was a promise, the other a readout.

    The labels are nouns now and the state lives in a class."""
    import json
    h = tmp_path / "vtog.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nconst lnk={_c:{},classList:{toggle(c,v){lnk._c[c]=v;}},textContent:'all ports'};"
        "\nglobal.document.getElementById=()=>lnk;"
        "\ntoggleShowHidden();"
        "\nconsole.log(JSON.stringify({on:lnk._c.on,label:lnk.textContent}));"
        "\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["on"] is True, "the toggle does not mark itself active"
    assert out["label"] == "all ports", \
        "the label was rewritten — it should be a noun that stays put"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_power_lives_on_the_dot_not_in_the_row_menu(tmp_path):
    """The row menu's Power group and the power dot's menu were built from the
    same builder and offered the same five items. The dot sits in the row
    beside the state it changes and is a shorter trip, so the menu copy goes.

    The fastboot variant stays in the menu on purpose: mkstrip only draws a
    power dot for a mapped codename, so a watch in the bootloader would
    otherwise have no route to Continue boot."""
    import json
    h = tmp_path / "pwrgrp.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nlet menu='';openMenu=function(ev,html){menu=html;};"
        "\nconst ev={stopPropagation(){}};"
        "\nmenuExecute(ev,'1-2:1',false,false,false,true,false,'S1',false,'device','',0,'skipjack','');"
        "\nconst adb=menu;"
        "\nmenuExecute(ev,'1-2:1',true,false,false,true,false,'S1',false,'fastboot','',0,'skipjack','');"
        "\nconst fb=menu;"
        "\nmenuPwr(ev,'1-2:1',false,false,false,true,false,'skipjack');"
        "\nconsole.log(JSON.stringify({adb:adb,fb:fb,dot:menu}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    # Gone from the booted row menu...
    # Charge and the reboot family stay on the dot. The DRAIN test deliberately
    # does not: it takes a watch out of service for hours to produce a wear
    # verdict, which is workbench work, not a situational power action.
    for item in ("Charge", "Power off", "Reboot", "Bootloader"):
        assert item not in out["adb"], f"{item!r} is duplicated in the row menu"
    assert "Drain test" in out["adb"], "the drain test lost its Workbench home"
    assert "Drain test" not in out["dot"], "the drain test is still on the power dot"
    # ...still on the dot, which is where it belongs.
    for item in ("Charge", "Reboot", "Bootloader"):
        assert item in out["dot"], f"the power dot lost {item!r}"
    # ...and a bootloader watch keeps its route, since it has no dot.
    assert "Continue boot" in out["fb"], \
        "a watch in the bootloader has no dot and now no menu route either"


def test_amber_means_only_attention():
    """Amber carried seven distinct meanings: warning, SSH mode, command in
    flight, a running measurement, held, over-subscribed and mid battery. A
    watch in SSH mode, a watch mid-command, a watch under an oplock and a watch
    at 40% were all the same colour, so the colour told you nothing.

    Each meaning that is not "something needs attention" now has its own token."""
    css = _WEB_TEMPLATE[:_WEB_TEMPLATE.index("</style>")]

    def rule(sel):
        import re as _re
        m = _re.search(_re.escape(sel) + r"\{([^}]*)\}", css)
        assert m, f"{sel} not found"
        return m.group(1)

    AMBER, BUSY, MODE, LOCKED, PROBE = "#d29922", "#58a6ff", "#f0883e", "#e3b341", "#8957e5"

    # An abnormal but VALID usb mode is not a warning — and SSH and fastboot
    # are the same kind of thing, so they read as siblings.
    assert MODE in rule(".cbadge.ssh"), "SSH mode still reads as a warning"
    assert MODE in rule(".cbadge.fb")
    # A run in progress / a command in flight is its own state.
    assert BUSY in rule(".cbadge.drain"), "a running drain test still reads as a warning"
    assert BUSY in rule(".tgl.pending"), "an in-flight command still reads as a warning"
    # Held means refused, not wrong — and its border used to disagree with its text.
    held = rule(".cbadge.held")
    assert LOCKED in held and AMBER not in held, "held still reads as a warning"
    assert held.count(LOCKED) >= 2, "the held pill's border and text still disagree"
    # The wanze dot should match the wanze pill.
    assert PROBE in rule(".sdot.wanze"), "the wanze dot still reads as a warning"

    # What KEEPS amber is only ever "something needs attention".
    for sel in (".alert", ".scrn", ".sdot.warn"):
        assert AMBER in rule(sel), f"{sel} should stay amber — it is a real warning"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_wanze_item_reflects_what_is_on_the_watch(tmp_path):
    """One item, both directions: Deploy when the probe is absent, Remove when
    it is already on the watch. A button that always says "deploy" makes the
    fleet's most easily-forgotten background job invisible once it is running."""
    import json
    h = tmp_path / "wz.js"
    h.write_text(
        _DOM_STUBS + JS +
        "\nconst absent=grpWorkbench('1-2:1','S9',false,'device','',false,false,false);"
        "\nconst present=grpWorkbench('1-2:1','S9',false,'device','',false,false,true);"
        "\nconsole.log(JSON.stringify({absent,present}));\nprocess.exit(0);\n")
    r = subprocess.run(["node", str(h)], capture_output=True, text=True, timeout=25)
    assert r.returncode == 0, f"harness failed:\n{r.stderr[:600]}"
    out = json.loads(r.stdout.strip().splitlines()[-1])

    assert "Deploy wanze" in out["absent"] and "Remove wanze" not in out["absent"]
    assert "Remove wanze" in out["present"] and "Deploy wanze" not in out["present"]
    assert "doWanze('S9',0)" in out["absent"], "deploy does not install"
    assert "doWanze('S9',1)" in out["present"], "remove does not un-deploy"

    # Both live in Workbench beside the drain test — the two long jobs that
    # occupy a watch rather than change its power state.
    for html in (out["absent"], out["present"]):
        assert "Drain test" in html and "Checkout" in html
