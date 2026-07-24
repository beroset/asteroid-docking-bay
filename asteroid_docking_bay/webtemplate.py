# SPDX-License-Identifier: GPL-3.0-only
# SPDX-FileCopyrightText: 2026 Timo Könnecke (moWerk) <mo@mowerk.net>
# SPDX-FileCopyrightText: 2023 Ed Beroset <beroset@ieee.org>
"""The single-page web UI (HTML/CSS/JS), served verbatim by webapp."""

from __future__ import annotations

_WEB_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>asteroid-docking-bay</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Archivo+Narrow:wght@400;700&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    /* Side margin scales with the viewport: ~0 up to a 960px (half-FHD/tablet)
       width so the table uses the whole screen, then grows on larger displays
       so an FHD view is not stretched edge to edge. */
    body{background:#0d1117;color:#c9d1d9;font:13px/1.6 'Cascadia Code','Fira Mono',monospace;padding:14px max(0px,calc((100vw - 960px) * 0.17)) 24px}
    .topbar,.berr,.alert,.hdr{padding-left:10px;padding-right:10px}
    h1{font:700 22px/1.4 'Archivo Narrow',sans-serif;color:#58a6ff;margin-bottom:4px;letter-spacing:1px}
    .hdim{color:#30363d;font-weight:400;font-size:16px;letter-spacing:3px}
    .htxt{letter-spacing:3px}
    .meta{color:#6e7681;font-size:11px;margin-bottom:20px}
    /* Fixed top bar: left/right pinned so varying string lengths (the
       update stamp) can never reposition their neighbours. */
    /* Seeded starfield backdrop (moWerk's Depth Drift): a fixed full-viewport
       layer behind everything, so the header and the side margins are painted
       with drifting stars. The table sits on its own solid background. */
    @keyframes drift{from{transform:translateX(-5px)}to{transform:translateX(5px)}}
    #stars{position:fixed;inset:0;z-index:-1;overflow:hidden;pointer-events:none}
    #stars span{position:absolute;line-height:1}
    .topbar{display:flex;justify-content:space-between;color:#6e7681;font-size:11px;margin-bottom:2px}
    .berr{color:#f85149;font-size:12px;margin-bottom:6px}
    .berr:empty{display:none}
    .alert{color:#d29922;font-size:12px;margin-bottom:6px;min-height:1.2em}
    .alert a{color:#58a6ff;text-decoration:none}
    .scrn{cursor:pointer;color:#d29922;margin-left:6px;animation:bpulse 1.4s infinite;-webkit-tap-highlight-color:transparent}
    .scrn:hover{color:#f0b429}
    .hdr{text-align:center}
    /* Control Center overlay */
    .cn{cursor:pointer;border-bottom:1px dotted #4d5561}
    .cn:hover{color:#58a6ff;border-bottom-color:#58a6ff}
    /* A disconnected watch's name dims well down, so the connected (full-white)
       ones stand out at a glance. */
    .offname{opacity:.6}
    .cc{position:fixed;z-index:100;display:none;width:auto;min-width:340px;max-width:94vw;background:#161b22;border:1px solid #30363d;border-radius:8px;box-shadow:0 10px 34px rgba(0,0,0,.6);font-size:12px;overflow:hidden}
    .cc-cols{display:flex;flex-wrap:wrap}
    .cc-col{flex:1 1 210px;min-width:200px}
    .cc-sec{padding:8px 14px}
    .cc-sech{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid #21262d;padding-bottom:3px;margin-bottom:5px}
    .cc-hd{padding:8px 30px 8px 12px;background:#0d1117;border-bottom:1px solid #30363d;font-weight:700;color:#58a6ff;position:relative;cursor:move;user-select:none}
    .cc-hd .dim{font-weight:400}
    .cc-x{position:absolute;right:10px;top:6px;cursor:pointer;color:#6e7681;font-weight:400;font-size:16px;line-height:1}
    .cc-x:hover{color:#fff}
    .cc-tabs{display:flex;background:#0d1117;border-bottom:1px solid #30363d}
    .cc-tab{flex:1;padding:6px 4px;border:0;background:transparent;color:#8b949e;cursor:pointer;font:inherit;font-size:11px;border-bottom:2px solid transparent}
    .cc-tab:hover{color:#c9d1d9;background:#161b22}
    .cc-tab.on{color:#58a6ff;border-bottom-color:#58a6ff}
    .cc-grid{display:grid;grid-template-columns:auto 1fr;gap:3px 10px}
    /* Rows a touch taller to seat the inline live graph beside the value. */
    .cc-grid .cc-v{min-height:15px;display:flex;align-items:center;justify-content:flex-end;gap:7px}
    .spark{flex:0 0 auto;vertical-align:middle}
    .cc-act.mini{width:auto;flex:1;padding:6px}
    .cc-k{color:#6e7681}
    .cc-v{color:#c9d1d9;text-align:right;font-variant-numeric:tabular-nums;word-break:break-all}
    .cc-tgls{display:flex;flex-wrap:wrap;gap:8px;padding:2px 12px 10px}
    .cc-tgl{flex:1;padding:7px 4px;border-radius:6px;border:1px solid #30363d;background:transparent;cursor:pointer;font:inherit;color:#8b949e}
    .cc-tgl.on{border-color:#3fb950;color:#3fb950}
    /* Screen/demo-mode toggle when active: bright warning yellow (not the
       benign green of wifi/bt) — a forced-on screen is a drain, so its ON
       state should read as an alert. Off state is the plain greyed pill. */
    .cc-tgl.scrnon{border-color:#f0b429;color:#f0b429;background:rgba(240,180,41,.15);font-weight:700}
    .cc-tgl:hover{background:#0d1117}
    .set-tgl{flex:0 0 auto;padding:3px 10px;min-width:46px;font-size:11px}
    .spins{display:flex;gap:5px;align-items:flex-end;justify-content:center;padding:4px 0 10px}
    .spin{display:flex;flex-direction:column;align-items:center;gap:2px;user-select:none}
    .spin-b{border:1px solid #30363d;background:transparent;color:#8b949e;cursor:pointer;font-size:8px;line-height:1;padding:2px 7px;border-radius:4px}
    .spin-b:hover{background:#0d1117;color:#c9d1d9}
    .spin-v{font-variant-numeric:tabular-nums;font-size:15px;color:#c9d1d9;padding:1px 3px;min-width:24px;text-align:center}
    .spin-l{font-size:9px;color:#6e7681;text-transform:uppercase;letter-spacing:.4px}
    .spin-sep{width:8px}
    .qp{display:flex;flex-wrap:wrap;gap:8px;padding:6px 2px 10px}
    .qpb{width:38px;height:38px;border-radius:50%;background:#30363d;border:0;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;color:#fff;opacity:.4}
    .qpb.on{opacity:1}
    .qpb:hover{background:#3a4149}
    .qpi{width:60%;height:60%;fill:#fff}
    .wx-row{display:flex;align-items:center;gap:11px;padding:2px 12px 7px}
    .wxi{width:34px;height:34px;flex:0 0 auto;fill:#c9d1d9}
    .wx-t{flex:1;min-width:0}
    .wx-temp{font-size:15px;color:#c9d1d9;font-variant-numeric:tabular-nums}
    .wx-city{font-size:11px;color:#8b949e;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .wx-none{padding:2px 12px 6px;color:#8b949e;font-size:12px}
    .wx-onwatch{padding:1px 12px 4px;font-size:12px;color:#c9d1d9}
    .wx-set{display:flex;gap:6px;padding:2px 12px 10px}
    .wx-in{flex:1;min-width:0;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:5px 8px;font:inherit;font-size:12px}
    .cc-acts{padding:0 12px 12px}
    .cc-act{width:100%;padding:8px;border-radius:6px;border:1px solid #388bfd;background:transparent;color:#388bfd;cursor:pointer;font:inherit}
    .cc-act:hover{background:#0d1f3a}
    .cc-act.done{border-color:#3fb950;color:#3fb950}
    /* Row action floating menus */
    .menu{position:fixed;z-index:110;display:none;min-width:172px;max-height:calc(100vh - 24px);overflow-y:auto;background:#161b22;border:1px solid #30363d;border-radius:7px;box-shadow:0 10px 30px rgba(0,0,0,.6);padding:5px}
    /* Menu items are slim text links (not chunky buttons), coloured by action,
       indented under their category header. */
    .menu-item{display:block;text-align:left;padding:2px 10px;border:none;background:none;color:#c9d1d9;cursor:pointer;font:inherit;line-height:1.6;white-space:nowrap}
    .menu-item:hover:not(:disabled){text-decoration:underline;filter:brightness(1.35)}
    .menu-item:disabled{opacity:.38;cursor:default}
    .menu-item.ch{color:#3fb950}
    .menu-item.dr{color:#d29922}
    .menu-item.po{color:#f85149}
    .menu-item.rb{color:#f0883e}
    .menu-item.bl{color:#d2a8ff}
    .menu-item.wbx{color:#a371f7}
    .menu-item.info{color:#58a6ff}
    .menu-sep{height:1px;background:#30363d;margin:4px 2px}
    .menu-hd{padding:3px 10px 5px;font-size:10px;color:#6e7681}
    /* Wear is the one item that stays a button — pink, the off-rig action. */
    .menu-wear{display:inline-flex;align-items:center;height:var(--pill-h);margin:3px 10px;padding:0 12px;border-radius:var(--pill-r);border:1px solid #e08a9e;background:none;color:#e0a5b5;cursor:pointer;font:inherit}
    .menu-wear:hover{background:#2a1a1f}
    .menu-wear.on{background:#e08a9e;color:#1a1416}
    /* Execute menu: former buttons become group headers, items indented under. */
    .exgrp-hd{padding:7px 10px 3px;font-size:10px;font-weight:700;color:#8b949e;
      text-transform:uppercase;letter-spacing:.6px;border-top:1px solid #21262d;margin-top:3px}
    .exgrp-hd:first-child{border-top:none;margin-top:0}
    .exgrp{padding-left:9px}
    /* Prominent, non-clickable IP banner at the top of the workbench menu —
       the address you actually need to reach the watch over SSH/WiFi. */
    #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:9px 16px;border-radius:7px;font-size:12px;opacity:0;pointer-events:none;transition:.2s;z-index:200}
    #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    /* Watch product-photo thumbnail + click-to-enlarge overlay */
    td.thumb{width:34px;padding:2px 2px 2px 0}
    .thumbwrap{position:relative;display:inline-block;line-height:0;vertical-align:middle}
    .thumbfill{position:absolute;z-index:0;background:#000}
    .wthumb{width:30px;height:30px;object-fit:contain;cursor:pointer;vertical-align:middle;border-radius:4px;transition:transform .1s;position:relative;z-index:1}
    .wthumb:hover{transform:scale(1.12)}
    .svgi{width:15px;height:15px;fill:currentColor;vertical-align:-2px}
    td.stats{min-width:52px;white-space:nowrap}   /* >=2 icons wide so the base pair never wraps to two rows */
    td.stats .strip{margin-left:0}
    .strip{margin-left:8px;display:inline-flex;gap:6px;align-items:center;vertical-align:middle}
    /* Every stat is a dot — a glyph in a circle — for one visual language with
       the power dot and the charging circle. The last-seen age, being text, is
       a matching pill rather than a dot. */
    .sdot{display:inline-flex;align-items:center;justify-content:center;box-sizing:border-box;
      width:var(--pill-h);height:var(--pill-h);border-radius:50%;border:1px solid;font-size:var(--pill-fs);
      line-height:1;vertical-align:middle;flex:none}
    .sdot .svgi{width:14px;height:14px;vertical-align:0}
    .sdot .pwri{width:15px;height:15px}
    .sdot.on{border-color:#3fb950;background:#3fb950;color:#0d1117}   /* powered: solid lit green (a thin outline washed out in sun glare) */
    .sdot.err{border-color:#f85149;color:#f85149}
    .sdot.warn{border-color:#d29922;color:#d29922}
    .sdot.dim{border-color:#3d4756;color:#8b949e}
    .sdot.chg{border-color:#238636;background:#238636;color:#f2cc60}   /* charging: yellow bolt on green */
    .sdot.drain{border-color:#3d4756;color:#8b949e;animation:drainpulse 1.4s ease-in-out infinite}
    .sdot[onclick]{cursor:pointer}
    .sdot.spark:hover,.sdot[onclick]:hover{background:rgba(88,166,255,.12)}
    @keyframes drainpulse{0%,100%{opacity:.3}50%{opacity:.85}}
    /* Last-seen age is not a pill — it trails the Stats dots as plain text. */
    .lastseen{color:#6e7681;font-size:11px;white-space:nowrap}
    .spark-svg{display:block;padding:2px 8px 8px;background:#0d1117}
    .wimg{position:fixed;z-index:120;display:none;
          background:#161b22;border:1px solid #30363d;border-radius:10px;
          box-shadow:0 12px 40px rgba(0,0,0,.6);padding:14px;max-width:94vw;max-height:92vh;overflow:auto}
    .wimg-hd{display:flex;justify-content:space-between;align-items:baseline;gap:20px;margin-bottom:10px;
             color:#58a6ff;font-weight:700;cursor:move;user-select:none}
    .wimg-hd .dim{font-weight:400;font-size:11px}
    .wimg-x{cursor:pointer;color:#6e7681;font-size:18px;line-height:1}
    .wimg-x:hover{color:#fff}
    /* Product photo (left) and live screenshot (right) side by side at one
       shared height, so both read as the same size whatever the screen aspect. */
    .wimg-body{display:flex;gap:18px;align-items:flex-start;flex-wrap:nowrap;justify-content:center}
    .device{display:inline-block}
    .dev-frame{position:relative;display:inline-block;line-height:0}
    .dev-prod{display:block;height:230px;width:auto;max-width:44vw;position:relative;z-index:2}
    .device.cut .dev-prod{max-width:none;height:auto}   /* JS (sizeComposite) sets the width, aspect-safe */
    .dev-shot{position:absolute;z-index:1;object-fit:contain}   /* preserve aspect (no squish) and never over-scale past the cutout */
    .dev-fill{position:absolute;z-index:0;background:#000}
    .dev-hands{position:absolute;z-index:1;pointer-events:none}   /* over the shot, under the bezel */
    .hmodes{gap:4px;align-items:center}
    .hmode{background:#0d1420;border:1px solid #30363d;color:#8b949e;border-radius:6px;padding:2px 9px;font-size:12px;cursor:pointer;font-family:inherit}
    .hmode.on{border-color:#a78bfa;color:#d6c7ff;background:rgba(167,139,250,.12)}
    .hchoreo,.hcal{display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;margin-left:6px}
    .av-sl{display:flex;align-items:center;gap:8px}
    .av-range{flex:1;accent-color:#58a6ff;min-width:90px}
    .av-val{min-width:38px;text-align:right;font-variant-numeric:tabular-nums;color:#c9d1d9;font-size:12px}
    .wimg-ctl{display:flex;flex-direction:column;gap:6px;align-items:center;padding:2px 6px 8px}
    .wimg-ctl-r{display:flex;gap:10px;align-items:flex-end;justify-content:center;flex-wrap:wrap}
    .wimg-shot{height:230px;width:auto;max-width:44vw;object-fit:contain;background:#000}
    .wimg-cap{color:#6e7681;font-size:10px;text-transform:uppercase;letter-spacing:.5px;text-align:center;margin-top:5px}
    /* Fluid: columns follow the page width with a minimal content margin, so
       the table always fits the viewport (no forced horizontal scroll). Column
       positions may shift slightly with string length — that's fine. */
    /* Milk glass: the table frosts the starfield behind it and rows are only
       semi-opaque, so the stars faintly shine through. Hub headers are a touch
       more transparent than the watch rows; the hover stays light enough not to
       hide the stars. */
    .tblwrap{overflow-x:auto;backdrop-filter:blur(2px);-webkit-backdrop-filter:blur(2px)}
    table{width:100%;border-collapse:collapse}
    th{color:#6e7681;text-align:left;padding:5px 12px;border-bottom:1px solid #21262d;font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:normal}
    td{padding:7px 8px;border-bottom:1px solid #161b22;vertical-align:middle}
    .wr td{background:rgba(13,17,23,.72)}
    /* Port folds into the Power cell: the informative s#/p# label, then the
       toggle — kept on one line. */
    .pcell{white-space:nowrap}
    .pcell .tgl{margin-right:8px}
    /* Smart pills vary in width (ppps / NO! / cycle), so centre them in a
       slightly tighter column. */
    /* Centered columns (Smart/Connection/Battery/Actions); Port/Stats/Watch
       stay left-aligned. */
    .smtc,.batc,.connc,.actc{text-align:center}
    td.smtc{padding-left:6px;padding-right:6px}
    .wr:hover td{background:rgba(32,41,54,.6)}
    .hub-hdr td{background:rgba(13,20,32,.5);color:#6e7681;padding:9px 12px 4px;border-top:1px solid #21262d;border-bottom:1px solid #21262d;font-size:11px;letter-spacing:1px}
    .hub-hdr:first-child td{border-top:none;padding-top:0}
    .hl{color:#58a6ff;font-weight:bold;margin-right:8px}
    .orbit-hdr .hl{color:#a78bfa}
    .orbit-add{float:right;font-weight:400;letter-spacing:0}
    .orbit-add input{width:118px;background:#0d1420;border:1px solid #30363d;color:#c9d1d9;border-radius:5px;padding:2px 7px;font-size:12px;margin-right:5px}
    .orbit-add input:focus{border-color:#a78bfa;outline:none}
    .orbitglyph{opacity:.75;font-size:14px}
    .wifiok{color:#3fb950;font-weight:600;font-size:11px;letter-spacing:.5px}
    .orbit-ip{font-size:11px;margin-left:6px}
    .orbit-row.offrow{opacity:.72}
    tr.empty td{color:#6e7681}
    tr.empty:hover td{background:#0a0d13}
    .on{color:#3fb950}.off{color:#6e7681}.warn{color:#d29922}.err{color:#f85149}.dim{color:#6e7681}
    .stale{color:#a1793a}.stale .agec{opacity:.7;font-size:10px}
    .shot-stale{opacity:.55;filter:grayscale(.3)}
    tr.justplugged>td{animation:plug 2s ease-out}
    @keyframes plug{0%{background:rgba(31,111,235,.4)}100%{background:transparent}}
    .wimg-shot.shape-round{border-radius:50%}.wimg-shot.shape-rect{border-radius:4px}
    .cc.stale-cc{border-color:#7a5b1e}.cc.stale-cc .cc-hd{background:#241d0e}
    .cc.stale-cc .cc-tgl,.cc.stale-cc .cc-act{opacity:.4;pointer-events:none}   /* offline: controls do nothing, so block + dim them */
    .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
    .don{background:#3fb950}.doff{background:#30363d}
    /* One set of tokens for every in-row pill, glyph-dot and the port toggle,
       so they all share a height (widths stay content-driven) and a look.
       --pill-h is the port-toggle height — the toggle is the reference size and
       reads it back below. Single-line pills meet this height; a pill whose
       content is too long still wraps to two inner lines and grows. */
    :root{--pill-h:24px;--pill-r:12px;--pill-px:9px;--pill-fs:12px}
    /* Connection-column badges for the abnormal USB modes, so a watch sitting
       in the bootloader or SSH/developer mode stands out from a normal ADB row. */
    /* Pills are inline-block, not flex, so long content (Connection, Battery)
       wraps to a second inner line instead of forcing the column — and the
       table — wider than the viewport. min-height (not height) holds the
       single-line size; a wrapped pill grows past it. */
    .cbadge{display:inline-block;box-sizing:border-box;min-height:var(--pill-h);padding:2px var(--pill-px);border-radius:var(--pill-r);font-size:var(--pill-fs);line-height:1.5;border:1px solid;vertical-align:middle;background:transparent;font-family:inherit}
    .cbadge.fb{border-color:#f0883e;color:#f0883e}
    .cbadge.adb{border-color:#3fb950;color:#3fb950}
    .cbadge.ssh{border-color:#d29922;color:#d29922}
    .cbadge.drain{border-color:#d29922;color:#d29922}
    .cbadge.wifi{border-color:#39c5cf;color:#39c5cf}
    .cbadge.bat{border-color:#6e7681;color:#c9d1d9}
    .orbit-hint{color:#a78bfa;font-size:.85em;opacity:.85}
    .drain-cfg{color:#a78bfa;font-size:10px;letter-spacing:.3px}
    .regmask{position:fixed;inset:0;background:rgba(2,6,14,.6);z-index:40}
    .regpanel{position:fixed;top:5vh;left:50%;transform:translateX(-50%);width:min(880px,94vw);max-height:88vh;z-index:41;background:rgba(13,20,32,.97);border:1px solid #30363d;border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.5);display:flex;flex-direction:column}
    .reg-hd{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid #21262d}
    .reg-hd b{color:#58a6ff}
    .reg-x{margin-left:auto;color:#8b949e;text-decoration:none;font-size:20px;line-height:1}
    .reg-body{overflow:auto}
    .reg-t{width:100%;border-collapse:collapse;font-size:12px}
    .reg-t th{text-align:left;color:#6e7681;font-weight:400;padding:6px 12px;position:sticky;top:0;background:rgba(13,20,32,.99);border-bottom:1px solid #21262d}
    .reg-t td{padding:6px 12px;border-bottom:1px solid #161b22;vertical-align:top}
    .reg-row.has-log{cursor:pointer}
    .reg-row.has-log:hover td{background:rgba(56,139,253,.08)}
    .reg-chev{display:inline-block;width:12px;color:#6e7681}
    .mono{font-family:ui-monospace,monospace;font-size:11px}
    .reg-log{padding:4px 10px 8px 24px}
    .reg-le{padding:5px 0;border-top:1px dashed #21262d;display:grid;grid-template-columns:82px 44px 1fr;gap:8px;align-items:start;font-size:12px}
    .reg-when{color:#a78bfa}
    .smt{display:inline-block;box-sizing:border-box;min-height:var(--pill-h);padding:2px var(--pill-px);border-radius:var(--pill-r);font-size:var(--pill-fs);line-height:1.5;border:1px solid;background:transparent;font-family:inherit;vertical-align:middle}
    /* Smart type is blue, not green — green is reserved for the power/charge
       states so it keeps its weight. The known type (ppps) is the brighter
       tone; the untested cycle is a darker shade of the same blue (it is an
       action, so deliberately NOT orange — orange means ambiguous/stale here). */
    .smt.ppps{border-color:#58a6ff;color:#58a6ff}
    .smt.no{border-color:#f85149;color:#f85149}
    .smt.unk{border-color:#1f6feb;color:#388bfd;cursor:pointer}
    .smt.unk:hover:not(:disabled){background:#0d2136}
    .smt.unk:disabled{opacity:.35;cursor:default}
    .cbadge.life{margin-left:6px;letter-spacing:.3px}
    .cbadge.life.down{border-color:#3d4756;color:#8b98a5}
    .cbadge.life.worn{border-color:#d98ca0;color:#e0a5b5}
    .cbadge.life.booting{border-color:#c9d1d9;color:#f0f6fc;animation:bootpulse 1.2s ease-in-out infinite}
    .cbadge.life.bootfail{border-color:#f85149;color:#f85149;animation:bootfail .5s ease-in-out infinite}
    @keyframes bootpulse{0%,100%{opacity:1}50%{opacity:.3}}
    @keyframes bootfail{0%,100%{background:transparent;color:#f85149}50%{background:rgba(248,81,73,.55);color:#fff}}
    .cbadge.bat.ok{border-color:#3fb950;color:#3fb950}
    .cbadge.bat.warn{border-color:#d29922;color:#d29922}
    .cbadge.bat.low{border-color:#f85149;color:#f85149}
    button.cbadge.bat:hover{background:rgba(255,255,255,.05)}
    /* Battery gauge: fixed width (~the column title), grey outline, a fill that
       grows left→right. The fill is coloured only when connected; grey when off. */
    .batw{position:relative;display:inline-block;box-sizing:border-box;width:68px;height:var(--pill-h);
      border-radius:var(--pill-r);border:1px solid rgba(240,246,252,.55);background:none;overflow:hidden;
      cursor:pointer;font:var(--pill-fs) monospace;color:#c9d1d9;vertical-align:middle;padding:0}
    .batw:hover{filter:brightness(1.18)}
    .batfill{position:absolute;top:0;left:0;bottom:0;width:0;background:rgba(120,130,145,.28);transition:width .4s ease}
    .batw.high .batfill{background:rgba(63,185,80,.32)}
    .batw.ok .batfill{background:rgba(210,153,34,.30)}
    .batw.low .batfill{background:rgba(248,81,73,.34)}
    .batw.off .batfill{background:rgba(120,130,145,.28)}
    .batlbl{position:relative;z-index:1;display:flex;align-items:center;justify-content:center;height:100%}
    /* Clickable badges are real <button>s so the cursor is a pointer, not a
       text caret; the non-clickable ones stay <span>s. */
    button.cbadge{cursor:pointer}
    button.cbadge.ssh:hover{background:#2a2113}
    button.cbadge.adb:hover{background:#122117}
    /* The flat dot-toggle: a coloured dot + ON/OFF, in the page's language. The
       in-flight EXEC state (added on click, cleared when the row rebuilds
       confirmed) dim-phases the whole toggle amber and grows/shrinks the dot —
       a livelier version of the plain cmd-pulse. */
    .tgl{display:inline-flex;align-items:center;justify-content:flex-start;gap:4px;box-sizing:border-box;width:54px;min-height:var(--pill-h);background:none;border:1px solid;padding:2px 9px 2px 6px;border-radius:var(--pill-r);cursor:pointer;font:var(--pill-fs) monospace;vertical-align:middle;margin-right:3px;transition:background .12s,transform .12s}
    .tgl-on{border-color:#3fb950;color:#3fb950}.tgl-on:hover{background:#0f2a18}
    .tgl-off{border-color:#30363d;color:#6e7681}.tgl-off:hover{background:#161b22}
    .tgl:active{transform:scale(.92);transition:transform 55ms ease-out}
    .tgl:disabled{opacity:.35;cursor:default;pointer-events:none}
    .tgl .dot{transition:transform .2s}
    .tgl.pending{border-color:#d29922;color:#d29922;animation:tglexec .9s ease-in-out infinite}
    .tgl.pending .dot{background:#d29922!important;animation:tgldot .9s ease-in-out infinite}
    @keyframes tglexec{0%,100%{opacity:1}50%{opacity:.4}}
    @keyframes tgldot{0%,100%{transform:scale(1)}50%{transform:scale(2)}}
    .btn{background:none;color:#c9d1d9;border:1px solid #30363d;padding:3px 9px;border-radius:4px;cursor:pointer;font:12px monospace;margin:0 .36em;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:background .12s,transform .12s}
    .btn:hover{background:#21262d}
    .btn:active{transform:scale(.92);transition:transform 55ms ease-out}
    .fl{border-color:#58a6ff;color:#fff}.fl:hover{background:#111d2e}
    .ch{border-color:#3fb950;color:#3fb950}.ch:hover{background:#0f2a18}
    .ht{border-color:#6e7681;color:#6e7681}.ht:hover{background:#1c1c1c}
    .hcut{border-color:#f85149;color:#f85149}.hcut:hover{background:#2a0d0b}
    .hrb{border-color:#d29922;color:#d29922}.hrb:hover{background:#2a2113}
    .hbl{border-color:#58a6ff;color:#58a6ff}.hbl:hover{background:#111d2e}
    .btn:disabled{opacity:.35;cursor:default;pointer-events:none}
    .btn.ex{border-radius:12px;padding:3px 15px;border-color:#58a6ff;color:#58a6ff}.btn.ex:hover{background:#122132}
    /* A worn row dims but stays. */
    .wr.worn td{opacity:.5}
    .wr.worn:hover td{opacity:.62}
    /* Instant feedback: a clicked action element pulses until the next update
       cycle confirms the new state (which rebuilds the row without this class). */
    .cmd-pending{animation:cmdpulse .8s ease-in-out infinite}
    @keyframes cmdpulse{0%,100%{opacity:1}50%{opacity:.38}}
    /* Failure: switch the pending pulse to a red flash, 3× at double the rate. */
    .cmd-fail{animation:cmdfail .4s ease-in-out 3!important}
    @keyframes cmdfail{0%,100%{background:transparent}50%{background:rgba(248,81,73,.6);border-color:#f85149;color:#fff}}
    .btn.ob{border-color:#1f6b39;color:#2c8a4c}.btn.ob:hover{background:#0d1f13}
    .hidebtn{color:#6e7681;text-decoration:none;font-size:15px;line-height:1;margin-left:6px;cursor:pointer;vertical-align:middle}
    .hidebtn:hover{color:#fff}
    tr.hiddenrow td,.wr.excl td{opacity:.5}
    .lr td{padding:0 12px 8px}
    .log{display:none;background:#010409;border:1px solid #21262d;border-radius:4px;padding:10px;font-size:12px;color:#8b949e;max-height:200px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
    .log.show{display:block}
    @keyframes bpulse{0%,100%{opacity:1}50%{opacity:.18}}
    @keyframes rpulse{0%,100%{background:transparent}50%{background:rgba(88,166,255,.06)}}
    .wr.refreshing td{animation:rpulse 1.1s ease-in-out infinite}
    /* Hovering a refreshing row must not hide the pulse. A plain `background`
       declaration would lose to :hover, and an !important one beats the
       animation itself (important declarations outrank keyframes) — pinning
       the row and killing the hint. So the hovered row gets its own keyframe
       that pulses *from* the hover colour instead. */
    @keyframes rpulsehover{0%,100%{background:#161b22}50%{background:rgba(88,166,255,.16)}}
    .wr.refreshing:hover td{animation:rpulsehover 1.1s ease-in-out infinite}
    .btn-ref.pulsing{animation:bpulse .85s ease-in-out infinite!important;border-color:#58a6ff!important;color:#58a6ff!important}
    @keyframes pwrwarn{0%,100%{background:transparent}40%{background:rgba(248,81,73,.12)}}
    .wr.pwr-warn td{animation:pwrwarn 1.8s ease-in-out 2}
    /* Phones: stack each row into a slim card — one labelled line per field —
       instead of a wide table that scrolls sideways. The desktop column order
       is the fleet's ground-truth order (port → power → … → watch); a card
       reads better name-first, so the card is a flex column that pulls the
       thumbnail and codename to the top with `order`, and the field labels
       come from :nth-child renumbered to the new column positions. */
    @media (max-width:720px){
      /* One card per screen is expected, so size up for legibility and touch —
         desktop's 11-13px is unreadable on a phone. */
      body{padding:12px;font-size:16px}
      .topbar,.meta{font-size:13px}
      .tblwrap{overflow-x:visible}
      table,tbody,tr,td{display:block;width:auto}
      thead{display:none}
      .hub-hdr td{padding:14px 4px 4px;font-size:13px}
      .wr{border:1px solid #21262d;border-radius:8px;margin:0 0 12px;padding:4px 14px;
          display:flex;flex-direction:column}
      .wr:hover td{background:transparent}
      .wr td{border:none;padding:9px 0;display:flex;justify-content:space-between;
             align-items:center;gap:14px;text-align:right;font-size:16px}
      .wr td:nth-child(4){order:-2;display:block;margin:8px 0 0;padding:0;border:none}  /* thumb, card top */
      .wr td:nth-child(4) .wthumb{width:44px;height:44px}
      .wr td:nth-child(5){order:-1;display:block;text-align:left;font-weight:700;font-size:20px;
                          padding:12px 0;border-bottom:1px solid #161b22;overflow:hidden}  /* codename title */
      .wr td.stats:empty{display:none}                           /* no stats read yet → no blank row */
      .wr td:nth-child(1)::before{content:"Port"}
      .wr td:nth-child(2)::before{content:"Smart"}
      .wr td:nth-child(3)::before{content:"Connection"}
      .wr td:nth-child(6)::before{content:"Stats"}
      .wr td:nth-child(7)::before{content:"Battery"}
      .wr td::before{color:#8b949e;font-size:13px;text-transform:uppercase;
                     letter-spacing:.5px;flex:none;font-weight:400}
      .wr td:nth-child(8){order:1;display:block;text-align:left;padding-top:10px}  /* actions span the card, last */
      /* Bigger, tappable controls (the toggle keeps its fixed 54px width). */
      .wr .btn{font-size:15px;padding:9px 13px;margin:3px .3em}
      .wr .cbadge,.wr .scrn{font-size:14px;padding:3px 9px}
      .lr td{padding:0}
    }
  </style>
</head>
<body>
  <div id="stars"></div>
  <div class="topbar"><span id="ts">loading&hellip;</span><span id="ver"></span></div>
  <div id="berr" class="berr"></div>
  <div id="alert" class="alert"></div>
  <div class="hdr">
  <h1><span class="hdim">&#x2728;  &#x22C6;  &#x02DA; </span>&#x2726;<span class="htxt">  asteroid-docking-bay  </span>&#x2726;<span class="hdim"> &#x02DA;  &#x22C6;  &#x2728;</span></h1>
  <p class="meta"><a href="#" id="histlink" onclick="toggleHistory();return false" style="color:#388bfd;text-decoration:none">show drain history</a> &nbsp;&middot;&nbsp; <a href="#" id="hidlink" onclick="toggleShowHidden();return false" style="color:#6e7681;text-decoration:none">show all ports</a> &nbsp;&middot;&nbsp; <a href="#" id="usbpreflink" onclick="toggleUsbPref();return false" style="color:#6e7681;text-decoration:none" title="Fleet USB-mode preference — how a watch that comes up on its own in the wrong mode is auto-corrected:&#10;&#10;• prefer ADB (standard): a stray SSH watch is switched back to adb — faster, and how a stock flash enumerates&#10;• prefer SSH: a stray watch is given its own SSH IP so several can run SSH at once — needed for WiFi/workbench work, but updates are slower&#10;&#10;A watch you switched by hand is left alone. Click to switch.">prefer ADB</a> &nbsp;&middot;&nbsp; <a href="#" id="reglink" onclick="openRegistry();return false" style="color:#6e7681;text-decoration:none" title="the Fleet Registry — every watch the rig has ever seen (docked or in orbit), its identity, first/last sighting, and a Log of what changed (kernel, Qt, MACs, resolution) over time">fleet registry</a> &nbsp;&middot;&nbsp; <a href="#" id="btlink" onclick="openBtScan();return false" style="color:#6e7681;text-decoration:none" title="scan for AsteroidOS watches over Bluetooth (they advertise their codename) and pair them — the first rung of Bluetooth in the Orbit port">scan bt</a></p>
  </div>
  <div class="tblwrap">
  <table>
    <thead><tr>
      <th>Port</th><th class="smtc">Smart</th><th class="connc">Connection</th>
      <th></th><th>Watch</th><th>Stats</th><th class="batc">Battery</th><th class="actc">Actions</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
  </div>
  <div id="hist" style="display:none"></div>
  <div id="cc" class="cc"></div>
  <div id="menu" class="menu"></div>
  <div id="wimg" class="wimg"></div>
  <div id="regmask" class="regmask" style="display:none" onclick="closeRegistry()"></div>
  <div id="reg" class="regpanel" style="display:none"></div>
  <div id="btmask" class="regmask" style="display:none" onclick="closeBt()"></div>
  <div id="bt" class="regpanel" style="display:none"></div>
<script>
const srcs={};
const chargeEnd={};
let countdownRunning=false;
let showHidden=false;
const refreshing=new Set();
// Serials seen enumerated on the last render, to flash a row when a watch is
// freshly plugged in. firstStatus suppresses the flash on the initial load.
let seenSerials=new Set();
let firstStatus=true;
function mkhide(slot,excluded){
  return `<a href="#" class="hidebtn" onclick="doHidePort('${slot}');return false" title="${excluded?'un-hide this row':'hide this row'}">${excluded?'&#x2295;':'&#x2296;'}</a>`;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function mkpwr(v){return v===true?'<span class="dot don"></span><span class="on">ON</span>':v===false?'<span class="dot doff"></span><span class="off">OFF</span>':'<span class="dim">---</span>'}
function mksmart(p,slot,dis){
  // Smart = can the port switch VBUS. A known verdict is a pill (green yes /
  // red NO!). Untested shows the power-cycle in its place, because the cycle
  // IS the test — one click cuts and restores power and records the verdict —
  // so the control lives exactly where its result will land.
  if(p.smart===true)return '<span class="smt ppps" title="PPPS — this port switches its own VBUS (per-port power switching)">ppps</span>';
  if(p.smart===false)return '<span class="smt no" title="port cannot switch its own power (not smart)">NO!</span>';
  return `<button class="smt unk"${dis} onclick="pulseSelf(this);doCy('${slot}')" title="smart capability not tested — click to power-cycle the port and detect it">&#x21BA;</button>`;
}
function pulseSelf(el){
  // Give a clicked action button instant feedback while the command is in
  // flight. The status refresh that reflects the new state rebuilds the row
  // (reconcile only rebuilds CHANGED rows) and the fresh button has no pulse —
  // so it self-clears on confirmation. The timeout is only a safety net for a
  // command that changes nothing (a no-op or a failure), where the row is
  // reused and the class would otherwise linger.
  if(!el)return;
  el.classList.add('cmd-pending');
  setTimeout(()=>{try{el.classList.remove('cmd-pending');}catch(e){}},8000);
}
function flashFail(el){
  // Direct feedback that a command FAILED: stop any pending state and flash
  // the element red three times. Used where the backend tells us the action
  // did not take (a port that would not switch, a refused mode switch).
  if(!el)return;
  el.classList.remove('cmd-pending','pending');
  el.classList.add('cmd-fail');
  setTimeout(()=>{try{el.classList.remove('cmd-fail');}catch(e){}},1300);
}
// Port-toggle click: switch to the opposite of the current state. Add the
// animated EXEC state while the command is in flight; on confirm, refresh so
// the row rebuilds into the new state (a brief delay lets the exec animation be
// seen). A refused switch flashes the toggle red.
function pwrGo(el,slot){
  if(el.classList.contains('pending'))return;
  const on=!el.classList.contains('tgl-on');
  el.classList.add('pending');
  fetch((on?'/api/on/':'/api/off/')+_api(slot),{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.confirmed===false){flashFail(el);_pwrFlash(slot);return;}
    setTimeout(refresh,700);
  }).catch(()=>flashFail(el));
  setTimeout(()=>{try{el.classList.remove('pending');}catch(e){}},8000);
}
function connPill(serial){
  // The connection cell carries the id; flash the badge INSIDE it, not the whole
  // cell, so a failed switch reddens only the pill.
  const td=serial?document.getElementById('conn-'+serial):null;
  return td?(td.querySelector('.cbadge')||td):null;
}
// The power symbol as a stroked ionicon (the same style AsteroidOS uses), not
// the ⏻ Unicode glyph — thinner, crisp at any scale, and centred by its viewBox.
const POWERSVG='<svg class="pwri" viewBox="0 0 512 512" fill="none" stroke="currentColor" stroke-width="38" stroke-linecap="round" stroke-linejoin="round"><path d="M378.09 92.42a201.31 201.31 0 11-244.18 0"/><path d="M256 32v192"/></svg>';
function pdot(p){
  // Power state as the first Stats dot: the power icon in a circle, recoloured
  // by what we can positively assert. green = powered (the port is delivering
  // power); grey = safely down (a confirmed graceful shutdown, port off, not
  // draining); orange = ambiguous (off with no graceful-shutdown marker — a raw
  // port cut that could equally be off or still running on battery).
  const st=p.power===true?'on':(p.lifecycle==='down'?'down':'amb');
  const tip=(st==='on'?'powered — the port is delivering power'
    :st==='down'?'safely powered down — gracefully halted, port off, not draining'
    :'power state ambiguous — port off with no graceful-shutdown marker; could be off, or still running on battery after a raw cut')
    +' · click for power actions';
  const slot=p.slot_loc+':'+p.port;
  const clk=`menuPwr(event,'${slot}',${p.adb==='fastboot'},${!!p.charging_active},${!!(p.drain&&p.drain.active)},${p.power===true},${p.smart===false})`;
  return sdot(st==='on'?'on':st==='down'?'dim':'warn',POWERSVG,tip,clk);
}
function mklife(p){
  // Worn (off-rig via the wear toggle) is a marker on the name, so it keeps its
  // own pink pill beside the codename; the power state lives in the Stats dot.
  return p.lifecycle==='worn'?`<span class="cbadge life worn" title="worn — off the rig via the wear toggle; port held for re-docking">worn</span>`:'';
}
function batBand(v,lo,hi){return v==null?'':(v<lo?'low':v<=hi?'ok':'');}
function batPill(p,cls,inner,title){
  // The battery cell as a pill: the charge percent, plus one line of appended
  // detail (charge state, drain rate, …) in dim — like the mode badges carry
  // the serial/IP. Clicking opens the Battery Info window; a watch with no
  // serial (never seen) is a plain non-clickable pill.
  const t=title?` title="${esc(title)}"`:'';
  if(!p.serial)return `<span class="cbadge bat ${cls||''}"${t}>${inner}</span>`;
  return `<button class="cbadge bat ${cls||''}" onclick="openBI('${esc(p.serial)}','${esc(p.codename||p.serial)}',event)"${t}>${inner}</button>`;
}
function fmtAge(ts){
  // Compact "how long ago" for a last-live timestamp (seconds since epoch).
  if(!ts)return '';
  const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<3600)return Math.floor(s/60)+'m';
  if(s<86400)return Math.floor(s/3600)+'h';
  return Math.floor(s/86400)+'d';
}
function mkbatCell(p,lo,hi){
  // Prefer the live reading; when the watch is off the bus fall back to the
  // last-seen value shown stale (amber) with its age, not a blank cell.
  // A battery gauge: a fixed-width cell with a fill that grows left→right by
  // charge level. Light grey by default; the fill is coloured by the real
  // charge (red/amber/green) ONLY when the watch is connected. Offline shows
  // the last level in grey — a level, not a colour claim. The charge STATE is
  // carried by the Stats charge dot, so it is not repeated here.
  const connected=p.battery!=null;
  const pct=connected?p.battery:p.battery_cached;
  if(pct==null)return '<span class="dim">&mdash;</span>';
  const band=connected?(batBand(p.battery,lo,hi)||'high'):'off';
  const age=fmtAge(p.last_live_ts);
  const tip=connected?'battery — click for details'
    :('watch off the bus — last reading'+(age?' '+age+' ago':''));
  const clk=p.serial?` onclick="openBI('${esc(p.serial)}','${esc(p.codename||p.serial)}',event)"`:'';
  const w=Math.max(0,Math.min(100,pct));
  return `<button class="batw ${band}"${clk} title="${tip}"><span class="batfill" style="width:${w}%"></span><span class="batlbl">${pct}%</span></button>`;
}
function mkthumb(p){
  // Product photo thumbnail; removes itself if the watch has no image (404).
  if(!p.codename)return '';
  const g=p.geometry||{};
  const oc=`openWatchImg('${esc(p.codename)}','${esc(p.serial||'')}',event,${g.round?1:0},'${g.resolution?esc(g.resolution):''}')`;
  // Wrapped so a cut-out product image gets a black fill behind its
  // transparent screen — otherwise the row would shine through the hole.
  return `<span class="thumbwrap"><img class="wthumb" loading="lazy" alt="" onload="onThumbLoad(this,'${esc(p.codename)}',${g.round?1:0})" onerror="this.closest('.thumbwrap').remove()" src="/api/watch-image/${encodeURIComponent(p.codename)}" onclick="${oc}"></span>`;
}
function onThumbLoad(img,codename,round){
  // Fill the transparent screen with black in the row thumbnail (once cut),
  // clipped to a circle for round screens so no black corners leak.
  const box=holeFor(codename,img), wrap=img.closest('.thumbwrap');
  if(!box||!wrap)return;
  const f=document.createElement('div'); f.className='thumbfill';
  const pct=v=>(v*100).toFixed(2)+'%';
  f.style.cssText=`left:${pct(box.x)};top:${pct(box.y)};width:${pct(box.w)};height:${pct(box.h)}`+(round?';border-radius:50%':'');
  wrap.insertBefore(f,img);
}
const ICONS={watch:'<path d=\"M127.9 376c0-2 .7-4 2.2-5.5 3.1-3.2 8.1-3.3 11.3-.2 20.9 20 46.8 30.8 79.3 32.8 19 1.2 27.1 5.8 35 10.3 9.3 5.3 18.9 10.7 54.2 10.7 71.7 0 122-59.2 122-132v-56c0-24.7-3-48.9-16.1-69.8-12.8-20.4-26.9-37-48.3-47.9-3.9-2-5.5-6.8-3.5-10.8 2-3.9 6.8-5.5 10.8-3.5 24 12.2 40.2 30.8 54.6 53.6 14.8 23.5 18.5 50.6 18.5 78.3v56c0 81.6-57.5 148-138 148-39.4 0-51.4-6.8-62-12.8-7.2-4.1-12.8-7.3-28.2-8.2-36.4-2.3-65.6-14.4-89.3-37.2-1.6-1.6-2.5-3.7-2.5-5.8z\"/><path d=\"M272.7 402c0-.4 0-.9.1-1.3.7-4.4 4.8-7.3 9.2-6.6 35.5 5.8 66.1-2.4 88.5-23.9 3.2-3.1 8.3-2.9 11.3.2 3.1 3.2 2.9 8.3-.2 11.3-26.2 25.1-61.5 34.8-102.1 28.1-4-.6-6.8-4-6.8-7.8zM64 292v-56c0-27.7 3.8-54.8 18.5-78.3 14.3-22.8 30.6-41.4 54.6-53.6 3.9-2 8.8-.4 10.8 3.5s.4 8.8-3.5 10.8c-21.4 10.9-35.5 27.5-48.3 47.9-13.2 20.8-16.2 45-16.2 69.7v56c0 34.8 9 70.1 38.8 96.9 30.3 27.4 71 43.1 111.6 43.1 4.4 0 8 3.6 8 8s-3.6 8-8 8c-44.5 0-89-17.2-122.3-47.2-33.1-29.9-44-69.5-44-108.8z\"/><path d=\"M375.3 129c-1.9.6-3.9 1-6.1 1-10.5 0-19-8.5-19-19s8.5-19 19-19c5.7 0 10.7 2.4 14.2 6.3-3-19.4-19.8-34.3-40-34.3h-175c-19.6 0-36.1 14-39.8 32.7 3.4-3 7.8-4.7 12.6-4.7 10.5 0 19 8.5 19 19s-8.5 19-19 19c-1.5 0-2.9-.2-4.3-.5 7.4 8.9 18.8 14.5 31.5 14.5h175c12.9 0 24.6-5.8 31.9-15zm-98.1-25c0-14.9 12.1-27 27-27s27 12.1 27 27-12.1 27-27 27c-14.7 0-27-12.1-27-27z\"/>',batterydead:'<path d=\"M384 144H80c-17.6 0-32 14.4-32 32v160c0 17.6 14.4 32 32 32h304c17.6 0 32-14.4 32-32V176c0-17.6-14.4-32-32-32zm16 192c0 8.8-7.2 16-16 16H80c-8.8 0-16-7.2-16-16V176c0-8.8 7.2-16 16-16h304c8.8 0 16 7.2 16 16v160zm32-135.4v110.8c19.1-11.1 32-31.7 32-55.4s-12.9-44.3-32-55.4z\"/>',flash:'<path d=\"M302.7 64 143 288h95.8l-29.5 160L369 224h-95.8l29.5-160z\"/>',moon:'<path d=\"M246.9 64c-12.6 1.4-24.9 4-36.6 7.7C132.4 96.4 76 169.3 76 255.4 76 361.8 162 448 268.2 448c58.7 0 111.2-26.4 146.5-67.9 8.1-9.5 15.2-19.8 21.4-30.8-11.4 2.8-23.1 4.5-35 5.1-2.9.1-5.9.2-8.8.2-48.4 0-94-18.9-128.2-53.2-34.3-34.3-53.1-80-53.1-128.5 0-27.6 6.1-54.3 17.7-78.5 4.9-10.7 11-20.9 18.2-30.4z\"/>',trend:'<path d=\"M472 128H360c-4.4 0-8 3.6-8 8s3.6 8 8 8h92L287.6 308.4l-83.9-84c-1.5-1.5-3.5-2.3-5.7-2.3-2.1 0-4.2.8-5.7 2.3L34.1 382.6c-1.6 1.6-2.1 3.7-2.1 5.9 0 2.1.6 3.9 2.1 5.5 1.6 1.6 3.6 2.3 5.7 2.3 2 0 4.1-.8 5.7-2.3L198 241.3l83.9 84c3.1 3.1 8.2 3.1 11.3 0L464 156v92c0 4.4 3.6 8 8 8s8-3.6 8-8V136c0-4.4-3.6-8-8-8z\"/>'};
function svgicon(n){return `<svg class="svgi" viewBox="0 0 512 512">${ICONS[n]}</svg>`;}
function sdot(cls,inner,title,click){
  return `<span class="sdot ${cls}"${title?` title="${title}"`:''}${click?` onclick="${click}"`:''}>${inner}</span>`;
}
function mkstrip(p,wearH){
  let out='';
  // 0. power state — first, so it reads at the same spot on every row.
  if(p.codename)out+=pdot(p);
  const biClk=p.serial?`openBI('${p.serial}','${esc(p.codename||'')}',event)`:'';
  const slot=p.slot_loc+':'+p.port;
  const wearClk=`menuWear(event,'${slot}',${!!(p.drain&&p.drain.active)},'${esc(p.serial||'')}',${p.wear?1:0})`;
  // 1. wearable verdict from the last drain test; an untested watch shows a
  //    grey "?" (like the battery-graph dot is grey with no history yet).
  //    Clicking it opens the drain-test / wear actions.
  const dl=p.drain_last;
  if(dl&&dl.est_h!=null){
    const ok=dl.est_h>=wearH;
    const when=new Date(dl.ts*1000).toLocaleDateString();
    const tip=`holds ~${fmtDur(dl.est_h)} standby (100&rarr;15%, drain test ${when})`+(ok?' — wearable':` — below ${wearH}h: battery swap candidate`)+' · click for drain/wear';
    out+=sdot(ok?'on':'err',svgicon(ok?'watch':'batterydead'),tip,wearClk);
  }else if(p.codename){
    out+=sdot('dim','?','never drain-tested — click to run a drain test',wearClk);
  }
  // 2. battery-graph dot — an always-present indicator that history exists;
  //    clicking it opens the same Battery Info panel as the battery gauge.
  if(p.serial)out+=sdot('dim spark',svgicon('trend'),'battery info + history',biClk);
  // 3. charge state — last of the dots, because it only appears conditionally:
  //    an active dock op (charging = yellow bolt on a green disc; drain test =
  //    a dim pulse), else the watch-side charge state (ground truth). Like the
  //    gauge and graph dot, clicking it opens Battery Info.
  if(p.charging_active){
    out+=sdot('chg',svgicon('flash'),'charging to target',biClk);
  }else if(p.drain&&p.drain.active){
    out+=sdot('drain',svgicon('batterydead'),'drain test running',biClk);
  }else if(p.adb==='device'&&p.charge_status){
    const cs=p.charge_status;
    if(cs==='Charging')out+=sdot('on',svgicon('flash'),'charging (delivered power confirmed)',biClk);
    else if(cs==='Full')out+=sdot('on','&#10003;','battery full',biClk);
    else if(cs==='Discharging')out+=sdot('err','&#8595;','DISCHARGING while docked — on ADB but not taking charge (dirty contact / bad cable)',biClk);
  }
  // 4. last-seen age when the watch is off the bus — plain trailing text.
  if(p.adb!=='device'&&p.last_live_ts)out+=`<span class="lastseen" title="last live ${fmtAge(p.last_live_ts)} ago">${fmtAge(p.last_live_ts)}</span>`;
  return out?`<span class="strip">${out}</span>`:'';
}
function sparkSvg(pts){
  const W=260,H=90,pad=6;
  const ts=pts.map(p=>p.ts),t0=Math.min(...ts),t1=Math.max(...ts),tr=(t1-t0)||1;
  const x=t=>pad+(t-t0)/tr*(W-2*pad),y=v=>pad+(100-v)/100*(H-2*pad);
  const d=pts.map((p,i)=>(i?'L':'M')+x(p.ts).toFixed(1)+' '+y(p.pct).toFixed(1)).join(' ');
  return `<svg class="spark-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"><path d="${d}" fill="none" stroke="#58a6ff" stroke-width="1.5"/></svg>`;
}
// Battery history for the Battery tab: fetched once when the tab opens and
// stored per serial, so the Battery body can append the chart at its foot.
const biHist={};
function biHistFetch(serial){
  fetch('/api/watch/'+encodeURIComponent(serial)+'/timeline').then(r=>r.json()).then(d=>{
    if(ctlSerial!==serial)return;
    biHist[serial]=d;
    if(ctlTab==='bat'&&ctlCache[serial])renderControl(ctlCache[serial]);
  }).catch(()=>{});
}
function mkport(p){
  let s = p.socket!=null
    ? `<b style="color:#c9d1d9">s${p.socket}</b> <span class="dim" style="font-size:10px">p${p.port}</span>`
    : `<span class="dim">p${p.port}</span>`;
  if(p.excluded) s = `<span class="err" title="${esc(p.excluded)}">avoid</span> ` + s;
  return s;
}
const AOSLOGO='<svg viewBox="0 0 2000 2000" width="13" height="13" style="vertical-align:-2px;margin-right:5px" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"><defs><rect id="T" width="2" height="2"/></defs><g transform="matrix(100 100 -100 100 1000 0)"><g><use href="#T" style="fill:#be3729"/><use href="#T" id="b" x="2" style="fill:#dc2919"/><use href="#T" id="c" x="4" style="fill:#e54b3a"/><use href="#T" id="d" x="6" style="fill:#e56934"/><use href="#T" id="e" x="8" style="fill:#e57c21"/></g><g transform="translate(-2,2)"><use href="#b"/><use href="#c"/><use href="#T" id="f" x="10" style="fill:#e58a21"/></g><g transform="translate(-4,4)"><use href="#c"/><use href="#e"/><use href="#T" id="g" x="12" style="fill:#f19a11"/></g><g transform="translate(-6,6)"><use href="#d"/><use href="#e"/><use href="#f"/><use href="#T" id="h" x="14" style="fill:#f0ae0e"/></g><g transform="translate(-8,8)"><use href="#e"/><use href="#f"/><use href="#g"/><use href="#h"/><use href="#T" x="16" style="fill:#f0c30e"/></g></g></svg>';
function mkadb(adb,fbprod,os,serial,sshIp,name){
  const nm=esc(name||serial||'');
  if(adb==='device'){
    // Clicking the badge opens the Network Center (addresses, links, the USB
    // mode toggle) rather than switching mode inline — an inline toggle here
    // was too easy to misclick. A real <button> for the pointer cursor; a
    // known non-AsteroidOS watch (e.g. WearOS) stays a plain status span.
    // Shows the serial — the ADB address — mirroring the SSH pill's IP.
    const known=os&&os!=='asteroidos'&&os!=='unknown';
    const logo=os==='asteroidos'?AOSLOGO:'';
    const ser=serial?` <span class="dim">${esc(serial)}</span>`:'';
    const ttl=`ADB mode${os==='asteroidos'?' — AsteroidOS':(known?' — '+esc(os):'')}`;
    if(!known&&serial)
      return `<button class="cbadge adb" onclick="openNC('${esc(serial)}','${nm}',event,'${esc(sshIp||'')}','device')" title="${ttl} — click for network details">${logo}ADB${ser}</button>`;
    return `<span class="cbadge adb" title="${ttl}">${logo}ADB${ser}</span>`;
  }
  if(adb==='ssh'){const ipl=sshIp?` <span class="dim">${esc(sshIp)}</span>`:'';return `<button class="cbadge ssh" onclick="openNC('${esc(serial||'')}','${nm}',event,'${esc(sshIp||'')}','ssh')" title="SSH/developer USB mode at ${esc(sshIp||'192.168.2.15')} — click for network details">${AOSLOGO}SSH${ipl}</button>`;}
  if(adb==='fastboot'){const l=fbprod?`fastboot: ${esc(fbprod)}`:'fastboot';return `<span class="cbadge fb" title="watch is in the bootloader (fastboot) — flash/backup only, no ADB or watch functions">${l}</span>`;}
  if(adb)return `<span class="dim">${esc(adb)}</span>`;
  return '<span class="dim">&mdash;</span>';
}
function mkadbrow(p){
  // Highest-priority warning: the watch is almost certainly awake and
  // draining right now, and nothing else on the row can show it. Cutting VBUS
  // does not stop a watch in the bootloader — it keeps running on battery,
  // invisible, until flat. That is how sturgeon reached 0%.
  if(p.fb_draining)
    return '<span class="err" title="last seen in FASTBOOT, port now unpowered — a watch in the bootloader does NOT stop when power is cut, it keeps running on battery until flat and is invisible while it does. Power the port back on, then either boot it or power it off from the on-screen fastboot menu.">draining in fastboot?</span>';
  // A running standby drain test: the watch is deliberately off the bus (port
  // cut, on battery), so without this the row falls through to a bare dash /
  // "no link". Name it AND the feature combo under test, so the connection
  // column reads the run at a glance (drainCfg: idle = baseline, all off).
  if(p.drain&&p.drain.active){
    const feat=drainCfg(p.drain.features);
    return `<span class="cbadge drain" title="standby drain test running${feat?' — consumers: '+feat:''}; port cut, watch on battery">drain test${feat?' '+esc(feat):''}</span>`;
  }
  // A boot we deliberately triggered: white pulse while it is expected up,
  // then a red-flashing "boot failed?" once the ~40s window lapses. Both beat
  // the generic no-link/not-enumerating messages below — we have positive
  // evidence a boot is under way, so we name it.
  if(p.adb===null&&p.lifecycle==='booting')
    return '<span class="cbadge life booting" title="just powered on / rebooted — waiting for it to come up (~40s)">booting up</span>';
  if(p.adb===null&&p.lifecycle==='reconnecting')
    return '<span class="cbadge life booting" title="port power was cut and restored on a running watch — it kept running on battery and is re-enumerating on the bus (not a reboot)">reconnecting</span>';
  if(p.adb===null&&p.lifecycle==='bootfail')
    return '<span class="cbadge life bootfail" title="triggered a boot but it has not come up in time — it may have failed to boot, or is simply not enumerating (flat battery, contact/cable)">boot failed?</span>';
  if(p.adb===null&&p.not_enumerating)
    return '<span class="err" title="a watch is mapped to this powered port but never enumerated — flat-battery bootloop, bad contact, or the watch was removed. Tip: holding the watch in fastboot draws less than booting and lets a flat battery charge past the boot threshold.">not enumerating</span>';
  if(p.adb===null&&p.power===true&&p.connected===false)
    return '<span class="warn" title="port is powered but nothing is electrically connected — no watch docked, or a dead cable/contact. No claim which: the plug being pulled and a bad contact look identical from here.">no link</span>';
  // A safely-down or worn watch is offline on purpose — say so here rather than
  // a bare dash, so the connection column reads as intentional, not a fault.
  if(p.adb===null&&p.lifecycle==='down')
    return '<span class="cbadge life down" title="shelved — gracefully powered down, port off, not draining (a deliberate, safe off)">shelved</span>';
  if(p.adb===null&&p.lifecycle==='worn')
    return '<span class="cbadge life worn" title="worn — off the rig via the wear toggle; port held for re-docking">worn</span>';
  return mkadb(p.adb,null,p.os,p.serial,p.ssh_ip,p.codename);
}
// Keyed row reconcile: replacing the whole tbody innerHTML every refresh
// destroyed and recreated every product <img>, so each thumbnail reloaded and
// blanked briefly — a visible flicker and wasted decode of the full-size
// images. Instead, key each row (by its slot, or a hub header by location) and
// only rebuild rows whose HTML actually changed; unchanged rows keep their
// exact DOM node — moved, not recreated — so their images never reload.
const _rowSig={};
function _rowKey(html){
  const m=html.match(/id="wr-([^"]+)"/);
  if(m)return 'row:'+m[1];
  const h=html.match(/class="hl">([^<]*)</);
  if(h)return 'hub:'+h[1];
  return 'x:'+html.length;
}
function reconcileRows(tb, htmls){
  const existing={};
  for(const el of Array.from(tb.children)){
    const k=el.getAttribute('data-k'); if(k!==null)existing[k]=el;
  }
  const seen=new Set(), out=[];
  for(const html of htmls){
    const key=_rowKey(html);
    if(seen.has(key))continue;
    seen.add(key);
    let el=existing[key];
    if(!(el && _rowSig[key]===html)){          // new or changed → build fresh
      const tmp=document.createElement('tbody');
      tmp.innerHTML=html;
      el=tmp.firstElementChild;
      if(el){el.setAttribute('data-k',key); _rowSig[key]=html;}
    }
    if(el)out.push(el);                          // unchanged → reuse the node
  }
  for(const k in _rowSig)if(!seen.has(k))delete _rowSig[k];
  tb.replaceChildren(...out);
}
function drainCfg(f){
  // The standby consumers a drain run captured at start — for per-feature
  // attribution ("this run had WiFi on, that one didn't"). "idle" = all off.
  if(!f)return '';
  const on=[]; if(f.wifi)on.push('wifi'); if(f.bt)on.push('BT'); if(f.aod)on.push('AoD');
  return on.length?on.join('·'):'idle';
}
function orbitBadge(p){
  // The connection column for an orbit row: a WiFi pill with the IP (the wire it
  // rides), physical-first grammar shared with adb/ssh. Offline → an honest note
  // with the last-live age. A BT pill (+serial) will join this when BT lands.
  if(p.reachable)return `<span class="cbadge wifi" title="reachable over WiFi at ${esc(p.ip||'')}">WiFi <span class="dim">${esc(p.ip||'')}</span></span>`;
  const age=fmtAge(p.last_live_ts);
  return `<span class="dim" title="off WiFi — last live ${age||'unknown'} ago">offline${age?' &middot; '+age:''}</span>`;
}
function renderOrbit(hub,rows,lo,hi){
  // The Orbit section: a virtual hub of watches reached over the air. Same row
  // grammar as a physical hub minus power/port/smart, so it reads as one fleet.
  // The header carries a Launch-by-IP box; its row HTML is constant, so
  // reconcileRows reuses the node and never wipes what is being typed.
  rows.push(
    `<tr class="hub-hdr orbit-hdr"><td colspan="8">`+
    `<span class="hl">&#x1F6F0; Orbit</span><span class="dim">${esc(hub.description)}</span>`+
    `<span class="orbit-add"><input id="orbip" type="text" placeholder="watch IP on WiFi" `+
      `spellcheck="false" autocomplete="off" onkeydown="if(event.key==='Enter')launchOrbit()">`+
    `<button class="btn" onclick="launchOrbit()" title="SSH-probe this address and launch the watch into orbit">Launch</button></span>`+
    `</td></tr>`
  );
  if(!hub.ports.length){
    rows.push(`<tr class="wr" id="wr-orbit-none"><td colspan="8" class="dim">Nothing in orbit yet — launch a watch by its WiFi IP above.</td></tr>`);
    return;
  }
  hub.ports.forEach(p=>{
    rows.push(
      `<tr class="wr orbit-row${p.reachable?'':' offrow'}" id="wr-orbit-${esc(p.serial)}">`+
      `<td class="pcell"><span class="orbitglyph" title="in orbit — reached over the air, not on a USB port">&#x1F6F0;</span></td>`+
      `<td class="smtc"></td>`+
      `<td class="connc">${orbitBadge(p)}</td>`+
      `<td class="thumb">${mkthumb(p)}</td>`+
      `<td><b class="cn${p.reachable?'':' offname'}" onclick="openCC('${p.serial}','${p.codename}',event)" title="open Control Center over WiFi (stale if offline)">${esc(p.codename)}</b> <span class="dim orbit-ip">${esc(p.ip||'')}</span></td>`+
      `<td class="stats"></td>`+
      `<td class="batc" id="bat-orbit-${esc(p.serial)}">${mkbatCell(p,lo,hi)}</td>`+
      `<td class="actc"><button class="btn" onclick="deorbit('${p.serial}','${p.codename}')" title="remove from Orbit — the watch itself is untouched">de-orbit</button></td>`+
      `</tr>`
    );
  });
}
function launchOrbit(){
  const el=document.getElementById('orbip');if(!el)return;
  const ip=(el.value||'').trim();if(!ip){el.focus();return;}
  el.disabled=true;
  fetch('/api/orbit/launch/'+encodeURIComponent(ip),{method:'POST'})
    .then(r=>r.json()).then(d=>{
      el.disabled=false;
      if(d&&d.ok){toast('launched '+(d.member.codename||d.member.serial)+' into orbit');el.value='';refresh();}
      else{toast((d&&d.error)||'launch failed — is the watch on WiFi in SSH mode?');el.focus();}
    }).catch(()=>{el.disabled=false;toast('launch failed');el.focus();});
}
function deorbit(serial,name){
  if(!confirm('De-orbit '+name+'? The watch itself is untouched - this only forgets how to reach it over the air.'))return;
  fetch('/api/orbit/deorbit/'+encodeURIComponent(serial),{method:'POST'})
    .then(r=>r.json()).then(d=>{if(d&&d.ok){toast('de-orbited '+name);refresh();}else toast('de-orbit failed');})
    .catch(()=>toast('de-orbit failed'));
}
function render(data){
  const tb=document.getElementById('tb');
  const hubs=(data&&data.hubs)||[];
  // Catch a forgotten screen-force-on (mcetool -D on) anywhere in the fleet:
  // it drains the watch invisibly, so surface it loudly with a release-all.
  const forced=[];hubs.forEach(h=>h.ports.forEach(p=>{if(p.screen_forced)forced.push(p.codename||p.serial)}));
  const al=document.getElementById('alert');
  al.innerHTML=forced.length?`screen forced ON, draining: <b>${forced.map(esc).join(', ')}</b> `+
    `<a href="#" onclick="releaseAllScreens();return false">release all</a>`:'';
  const lo=(data&&data.thresholds&&data.thresholds.low)||40;
  const hi=(data&&data.thresholds&&data.thresholds.high)||80;
  const floor=(data&&data.drain_floor)||15;
  const wearH=(data&&data.wearable_min_hours)||24;
  usbPref=(data&&data.usb_mode_preference)==='ssh'?'ssh':'adb';
  const upl=document.getElementById('usbpreflink');
  if(upl)upl.textContent=usbPref==='ssh'?'prefer SSH':'prefer ADB';
  if(!hubs.length){tb.innerHTML='<tr><td colspan="8" class="dim">No watches configured. Run: asteroid-docking-bay map</td></tr>';return}
  const rows=[];
  const present=new Set();   // serials enumerated this render, for the plug flash
  hubs.forEach(hub=>{
    if(hub.hidden&&!showHidden)return;
    if(hub.location==='orbit'){renderOrbit(hub,rows,lo,hi);return;}
    const hubHideBtn=`<a href="#" class="hidebtn" onclick="doHideHub('${esc(hub.location)}');return false" title="${hub.hidden?'un-hide this hub':'hide/show this hub'}">${hub.hidden?'&#x2295;':'&#x2296;'}</a>`;
    rows.push(`<tr class="hub-hdr${hub.hidden?' hiddenrow':''}"><td colspan="8"><span class="hl">${esc(hub.location)}</span><span class="dim">${esc(hub.description)}</span> ${hubHideBtn}</td></tr>`);
    const visPorts=hub.ports.filter(p=>showHidden||!p.excluded);
    visPorts.forEach((p,i)=>{
      if(p.empty){
        const slot=p.slot_loc+':'+p.port;
        const busy=!!(srcs[slot]||p.flashing);
        const d=(busy||p.excluded)?' disabled':'';
        const fbLabel=p.fastboot_product?`fastboot: ${esc(p.fastboot_product)}`:(p.adb==='fastboot'?'fastboot':'');
        const nameCell=p.unmapped
          ?`<span class="dim">${esc(p.codename)} <span style="font-size:.8em;opacity:.6">(click Onboard)</span></span>`
          :(p.orbited_codename
            ?`<span class="dim" title="this port's watch left the cradle and is now in Orbit (on WiFi) — the port is free">${esc(p.orbited_codename)} <span class="orbit-hint">&#8599; orbit</span></span>`
            :(p.fastboot_product?`<span class="warn">${esc(p.fastboot_product)}</span>`:'<span class="dim">&mdash;</span>'));
        const adbCell=p.adb==='fastboot'?`<span class="warn">${fbLabel}</span>`
          :mkadbrow(p);
        const pwrCls=p.power===true?'tgl tgl-on':'tgl tgl-off';
        const pwrLbl=p.power===true?'<span class="dot don"></span>ON':'<span class="dot doff"></span>OFF';
        const pwrFn=`pwrGo(this,'${slot}')`;
        const onboardBtn=p.excluded?'':`<button class="btn ob"${d} onclick="doRemap('${slot}')" title="power on, boot, then identify and map this watch">Onboard</button>`;
        rows.push(
          `<tr class="wr empty${p.excluded?' excl':''}" id="wr-${slot}">` +
          `<td class="pcell"><button class="${pwrCls}"${d} title="${p.power===true?'power the port off':'power the port on'}" onclick="${pwrFn}">${pwrLbl}</button>${mkport(p)}</td>` +
          `<td class="smtc">${mksmart(p,slot,d)}</td>` +
          `<td class="connc">${adbCell}</td>` +
          `<td class="thumb">${mkthumb(p)}</td>` +
          `<td>${nameCell}</td>` +
          `<td class="stats">${mkstrip(p,wearH)}</td>` +
          `<td class="dim batc">&mdash;</td>` +
          `<td class="actc">`+onboardBtn+mkhide(slot,p.excluded)+`</td>` +
          `</tr>` +
          `<tr class="lr" id="lr-${slot}"><td colspan="8"><div class="log${busy?' show':''}" id="log-${slot}"></div></td></tr>`
        );
      }else{
        const slot=p.slot_loc+':'+p.port;
        // A watch that just enumerated (absent last render) flashes its row.
        const enumd=p.serial&&p.adb==='device';
        if(enumd)present.add(p.serial);
        const isNew=enumd&&!firstStatus&&!seenSerials.has(p.serial);
        // Only a FUTURE end time is a countdown: accepting a stale/past one
        // creates a tick->expire->refresh->re-add loop that hammers the API.
        if(p.charge_end_ts&&p.charge_end_ts*1000>Date.now()&&!chargeEnd[slot])chargeEnd[slot]=p.charge_end_ts*1000;
        if(!p.charging_active&&chargeEnd[slot])delete chargeEnd[slot];
        const charging=!!p.charging_active;
        const draining=!!(p.drain&&p.drain.active);
        const wb=!!(p.workbench&&p.workbench.active);
        const isFb=p.adb==='fastboot';
        const logActive=!!(srcs[slot]||p.flashing);
        const busy=!!(logActive||charging||draining||wb);
        const noSw=p.smart===false;
        // Refresh doubles as "power on and identify" only where that is both
        // possible and wanted: a switchable port that is currently off and not
        // excluded (excluded ports are opted out of automatic power entirely).
        const needPwr=(p.power!==true&&!noSw&&!p.excluded);
        const dp=(busy||noSw||p.excluded)?' disabled':'';
        const adb=mkadbrow(p);
        let bat;
        if(wb){
          const w=p.workbench;
          const pct=w.pct!=null?w.pct+'% ':'';
          // Name the holder: on a rig several sessions share, "workbench
          // active" does not tell you whether to wait or take over.
          const who=w.owner?` — held by ${esc(w.owner)}`:'';
          bat=batPill(p,'warn',`${pct}<span class="dim">${esc(w.phase||'')}${w.owner?' ᴋ':''}</span>`,
                      `workbench: battery held in the ${lo}–${hi}% band while you work over WiFi/SSH${w.blind?' (battery unreadable — blind duty cycle)':''}${who}`);
        }else if(charging){
          if(p.charge_losing){bat=batPill(p,'low',`${p.charge_pct!=null?p.charge_pct:'?'}% <span class="dim">&#8595; losing</span>`,'battery is DROPPING while charging — losing power despite the charge attempt. Check contacts / cable / port (the dirty-contact failure).');}
          else if(p.charge_target!=null){bat=batPill(p,'warn',`${p.charge_pct!=null?p.charge_pct:'?'}% <span class="dim">&rarr; ${p.charge_target}%</span>`,'charging');}
          else if(chargeEnd[slot]){const rem=Math.max(0,Math.round((chargeEnd[slot]-Date.now())/1000));const m=Math.floor(rem/60),s=rem%60;bat=batPill(p,'warn',`<span class="dim">${m}m${String(s).padStart(2,'0')}s</span>`,'charging');}
          else{bat=batPill(p,'warn','<span class="dim">starting&hellip;</span>','charging');}
        }
        else if(draining){
          const dr=p.drain;
          let txt=(dr.last_pct!==null?dr.last_pct+'%':'?%')+' <span class="dim">&#x2193;</span>';
          if(dr.drain_rate!==null&&dr.drain_rate>0){
            txt=`${dr.last_pct}% <span class="dim">&minus;${dr.drain_rate.toFixed(1)}%/h`;
            if(dr.last_pct>floor){const estH=(dr.last_pct-floor)/dr.drain_rate;txt+=` (~${fmtDur(estH)})`;}
            txt+='</span>';
          }
          const cfg=drainCfg(dr.features);   // the WiFi/BT/AoD config this run captured
          if(cfg)txt+=` <span class="drain-cfg" title="consumers on for this run (per-feature drain attribution)">${cfg}</span>`;
          bat=batPill(p,'warn',txt,'drain test running'+(cfg?' — consumers: '+cfg:''));
        }else if(p.drain&&p.drain.done&&p.drain.last_pct!==null){
          const dr=p.drain;
          const summary=dr.drain_rate!==null?` &minus;${dr.drain_rate.toFixed(1)}%/h`:'';
          bat=batPill(p,batBand(p.battery,lo,hi),`${p.battery!=null?p.battery+'%':'—'}<span class="dim"> (test: ${dr.last_pct}%${summary})</span>`,'battery — click for details');
        }else{
          bat=mkbatCell(p,lo,hi);
        }
        const pwrFn=`pwrGo(this,'${slot}')`;
        const pwrCls=p.power===true?'tgl tgl-on':'tgl tgl-off';
        const pwrLbl=p.power===true?'<span class="dot don"></span>ON':'<span class="dot doff"></span>OFF';
        const isRef=refreshing.has(slot);
        rows.push(
          `<tr class="wr${isRef?' refreshing':''}${p.excluded?' excl':''}${isNew?' justplugged':''}${p.lifecycle==='worn'?' worn':''}" id="wr-${slot}">` +
          `<td class="pcell"><button class="${pwrCls}"${dp} title="${noSw?'port cannot switch power (not smart)':(p.power===true?'power the port off':'power the port on')}" onclick="${pwrFn}">${pwrLbl}</button>${mkport(p)}</td>` +
          `<td class="smtc">${mksmart(p,slot,dp)}</td>` +
          `<td class="connc"${p.serial?` id="conn-${esc(p.serial)}"`:''}>${adb}</td>` +
          `<td class="thumb">${mkthumb(p)}</td>` +
          `<td>`+(p.serial
            ?`<b class="cn${p.adb?'':' offname'}" onclick="openCC('${p.serial}','${p.codename}',event)" title="open Control Center (stale if offline)">${esc(p.codename)}</b>`
            :`<b class="${p.adb?'':'offname'}">${esc(p.codename)}</b>`)+mklife(p)+(p.screen_forced?`<span class="scrn" onclick="releaseScreen('${p.serial}')" title="screen forced ON (draining) — click to release">screen</span>`:'')+`</td>` +
          `<td class="stats">${mkstrip(p,wearH)}</td>` +
          `<td class="batc" id="bat-${slot}">${bat}</td>` +
          `<td class="actc" id="act-${slot}">` +
          `<button class="btn ex${isRef?' pulsing':''}"${p.excluded?' disabled':''} onclick="menuExecute(event,'${slot}',${isFb},${charging},${draining},${p.power===true},${noSw},'${p.serial||''}',${wb},'${p.adb||''}','${p.ssh_ip||''}',${p.wear?1:0},${needPwr})" title="refresh · power/charge/drain · flash/backup · workbench · wear">menu</button>` +
          `</td></tr>` +
          `<tr class="lr" id="lr-${slot}"><td colspan="8"><div class="log${logActive?' show':''}" id="log-${slot}"></div></td></tr>`
        );
      }
    });
  });
  reconcileRows(tb, rows);
  seenSerials=present; firstStatus=false;
  Object.keys(srcs).forEach(c=>{const b=document.getElementById('log-'+c);if(b)b.classList.add('show');});
  if(Object.keys(chargeEnd).length>0&&!countdownRunning)tickCountdown();
}
// ── Control Center — one tabbed window ──────────────────────────────────────
// System, Network and Battery were three separate overlays in 0.8, but all
// three fetched the SAME /api/watch/<serial> blob and shared one graph store —
// so they fold into a single window whose tabs swap the body. One serial, one
// cache, one poll: switching tabs re-renders the cached blob with NO refetch
// and NO graphReset, so every tab's graph keeps filling across a switch.
let ctlSerial=null, ctlName=null, ctlAX=0, ctlAY=0;
let ctlTab='sys', ctlSshIp=null, ctlMode=null;
let ctlMoved=false, ctlPlaced=false, _drag=null;   // manual pos, placed-once, active drag
// The tab bar. Order is System → Network → Battery here; Settings and Live join
// in later steps, landing the final System · Settings · Network · Battery · Live.
const CTL_TABS=[['sys','System'],['set','Settings'],['net','Network'],['bat','Battery']];
// Last-fetched payload per serial, so re-opening paints instantly from the
// previous values while the fresh fetch is in flight — and a self-cancelling
// poll keeps the open window live (important over SSH, where a fetch is slow).
const ctlCache={};
const ctlSettings={};   // per-serial mirrored settings rows (or an error)
let ctlDate=null;       // the Settings-tab clock spinners' dialled value
let ctlDateTouched=false;   // once the user dials a spinner, stop tracking now
const ctlPending=new Set(); // Settings-tab writes in flight — keys pulse until confirmed
let ctlPoll=null;
// Shared cell/section builders — one definition for every tab body (each used
// to redefine its own identical copy).
const _kv=(k,v)=>`<div class="cc-k">${k}</div><div class="cc-v">${esc(v==null||v===''?'\\u2014':String(v))}</div>`;
const _kvg=(k,v,g)=>`<div class="cc-k">${k}</div><div class="cc-v">${esc(v==null||v===''?'\\u2014':String(v))}${g||''}</div>`;
const _sec=(t,r)=>`<div class="cc-sec"><div class="cc-sech">${t}</div><div class="cc-grid">${r}</div></div>`;
const _num=x=>(x==null||x===''||isNaN(+x))?null:+x;
// adb is a warm channel (poll briskly); SSH pays a handshake per call, so a
// 3s poll would never keep up — pace it to 10s. The panel header shows which.
function panelPollMs(d){return (d&&d.transport==='ssh')?10000:3000;}
function pollTag(d){return d?` <span class="dim" title="live refresh interval">&middot; ${d.transport==='ssh'?'10s &middot; ssh':'3s'}</span>`:'';}

// ── live btop-style graphs ──────────────────────────────────────────────────
// A temporary history that lives only while a panel is open — one shared store
// (only one panel is ever open), reset on every open so each graph starts empty
// and fills from the right. Each poll appends one sample per metric; we keep the
// last GRAPH_N. Bars are filled blocks, height = the value on a FIXED per-metric
// scale, colour green→red toward the metric's "bad" end (high battery is green,
// high load/temp is red). Newest bar sits at the right by the value, rolling left.
const GRAPH_N=20;
let graphData={}, graphPrev={};
function graphReset(){graphData={}; graphPrev={};}
function graphPush(id,v){
  if(v==null||isNaN(v))return;
  (graphData[id]=graphData[id]||[]).push(+v);
  if(graphData[id].length>GRAPH_N)graphData[id].shift();
}
function graphPushRate(id,cumulative){        // for counters (rx/tx bytes) → per-second rate
  const v=+cumulative, now=Date.now();
  const p=graphPrev[id];
  if(p&&now>p.t&&v>=p.v)graphPush(id,(v-p.v)/((now-p.t)/1000));
  graphPrev[id]={v:v,t:now};
}
function spark(id,min,max,bad){
  const a=graphData[id]||[];
  if(!a.length)return '';
  const bw=3,gap=1,H=13,W=GRAPH_N*(bw+gap);
  let bars='';
  for(let i=0;i<a.length;i++){
    let n=(a[i]-min)/(max-min); n=n<0?0:n>1?1:n;
    const h=Math.max(1,Math.round(n*H));
    const red=bad==='low'?1-n:n;            // fraction of the way to "bad"
    const hue=Math.round(120*(1-red));      // 120=green … 0=red, through amber
    const x=(GRAPH_N-a.length+i)*(bw+gap);  // right-aligned; newest at the far right
    bars+=`<rect x="${x}" y="${H-h}" width="${bw}" height="${h}" fill="hsl(${hue},68%,48%)"/>`;
  }
  return `<svg class="spark" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">${bars}</svg>`;
}
function _ncpu(d){const m=(d.cores||'').match(/([0-9]+) *$/);return m?(+m[1]+1):1;}
function _memPct(d){const t=+d.memtotal;return t?Math.round((t-(+d.memfree||0))/t*100):null;}
function _load1(d){const x=parseFloat((d.load||'').split(/ +/)[0]);return isNaN(x)?null:x;}
let wimgAX=0, wimgAY=0, _wimgDrag=null, _wimgMoved=false;
let _compo=null;   // {boxW, target, aspect} for an open composite, else null
function sizeComposite(){
  // Set the product width so the screen hole shows the screenshot at `target`
  // px (2/3 of native — the screenshot is heavily JPEG-compressed, so full
  // size shows artefacts). Only width is set (height:auto), so the aspect
  // ratio is always kept; the width is bounded by BOTH viewport dimensions so
  // a small/squished window can't stretch or overflow it. Re-run on resize.
  const prod=document.getElementById('prodimg');
  if(!prod||!_compo)return;
  let w=_compo.target/_compo.boxW;
  w=Math.min(w, window.innerWidth*0.9, window.innerHeight*0.82*_compo.aspect);
  prod.style.width=Math.round(w)+'px'; prod.style.height='auto';
  wimgPlace();
}
window.addEventListener('resize',sizeComposite);
function fmtUp(sec){sec=Math.floor(+sec||0);const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);return (d?d+'d ':'')+(h||d?h+'h ':'')+m+'m';}
function placeOverlay(el,ax,ay){
  // Anchor to the click; flip ABOVE the anchor if the panel would run off the
  // bottom (its height only known after the async data renders). No page scroll.
  const h=el.offsetHeight, w=el.offsetWidth;
  let left=Math.min(ax, window.innerWidth-w-8);
  let top=ay+10;
  if(top+h>window.innerHeight-8) top=ay-h-10;
  el.style.left=Math.max(8,left)+'px'; el.style.top=Math.max(8,top)+'px';
}
// Place the window once per open (locked when real content lands), then leave
// it put: re-placing on every tab switch and poll made it hop around as the tab
// bodies differ in size (mo). A drag pins it the same way.
function ctlPlace(lock){
  if(!ctlMoved&&!ctlPlaced)placeOverlay(document.getElementById('cc'),ctlAX,ctlAY);
  if(lock)ctlPlaced=true;
}
// Drag the window by its title bar to park it beside a toggle. The header is
// rebuilt every render, so drag-start is an inline handler on it; a manual drag
// sets ctlMoved, and ctlPlace() then leaves the window put across tab switches
// and polls. mousemove/mouseup live on the document for the drag's duration.
function ctlDragStart(e){
  if(e.target.classList&&e.target.classList.contains('cc-x'))return;   // not the close X
  const cc=document.getElementById('cc'), r=cc.getBoundingClientRect();
  _drag={dx:e.clientX-r.left, dy:e.clientY-r.top}; ctlMoved=true; e.preventDefault();
}
document.addEventListener('mousemove',e=>{
  // One handler drags both floating windows: the Control Center (_drag) and the
  // live view (_wimgDrag). A single listener keeps them from shadowing each other.
  const d=_drag||_wimgDrag; if(!d)return;
  const o=document.getElementById(_drag?'cc':'wimg'), w=o.offsetWidth, h=o.offsetHeight;
  o.style.left=Math.min(Math.max(0,e.clientX-d.dx),window.innerWidth-w)+'px';
  o.style.top=Math.min(Math.max(0,e.clientY-d.dy),window.innerHeight-h)+'px';
});
document.addEventListener('mouseup',()=>{_drag=null;_wimgDrag=null;});
// First open of a watch has no client cache, so instead of a "loading…" wait
// paint the server's last-known values immediately — the /stale endpoint reads
// them with no device I/O, so it returns at once (amber, marked stale). The
// slow live fetch then follows and replaces it. cacheHas() guards the race: if
// the live fetch already populated the cache, the late stale paint is dropped.
function paintStale(serial,curSerial,cacheHas,renderFn){
  fetch('/api/watch/'+encodeURIComponent(serial)+'/stale').then(r=>r.json()).then(d=>{
    if(curSerial()===serial && !cacheHas() && d && d.kernel)renderFn(d);
  }).catch(()=>{});
}
function openControl(serial,name,ev,tab,sshIp,mode){
  ev.stopPropagation(); graphReset();      // fresh graphs for a fresh watch, not per tab
  ctlSerial=serial; ctlName=name; ctlAX=ev.clientX; ctlAY=ev.clientY;
  ctlTab=tab||'sys'; ctlMoved=false; ctlPlaced=false;   // a new open re-anchors
  // Reset the click's USB context each open — a codename/battery open carries
  // none, and a stale value from a previous open is exactly what made the
  // Network tab show adb/.2.15 for an SSH watch. bodyNet falls back to the
  // authoritative d.transport/d.ssh_ip when these are null.
  ctlSshIp=(sshIp!=null?sshIp:null); ctlMode=(mode!=null?mode:null);
  ctlDate=null; ctlDateTouched=false; ctlPending.clear();   // fresh clock + no pending writes
  const cc=document.getElementById('cc');
  cc.classList.remove('stale-cc');
  cc.style.display='block';
  if(ctlTab==='bat')biHistFetch(serial);
  if(ctlTab==='set')settingsFetch(serial);
  if(ctlTab==='sys'&&wxData===null)wxFetch();
  if(ctlTab==='sys'&&ctlSerial&&wxOnWatch[ctlSerial]===undefined)wxFetchOnWatch(ctlSerial);
  if(ctlCache[serial])renderControl(ctlCache[serial]);   // instant, from the last open
  else{cc.innerHTML=ctlChrome(null,`<div class="cc-sec"><span class="dim">loading&hellip;</span></div>`);ctlPlace();
       paintStale(serial,()=>ctlSerial,()=>!!ctlCache[serial],renderControl);}
  ctlFetch();
}
// The row triggers still open the window on the tab that matches what was
// clicked — codename→System, battery pill→Battery, network badge→Network.
function openCC(s,n,ev){openControl(s,n,ev,'sys');}
function openNC(s,n,ev,sshIp,mode){openControl(s,n,ev,'net',sshIp,mode);}
function openBI(s,n,ev){openControl(s,n,ev,'bat');}
function ctlTabTo(tab){
  if(!ctlSerial)return;
  ctlTab=tab;                              // no refetch, no graphReset: the poll
  if(tab==='bat')biHistFetch(ctlSerial);   // keeps every metric filling regardless
  if(tab==='set')settingsFetch(ctlSerial);
  if(tab==='sys'&&wxData===null)wxFetch();
  if(tab==='sys'&&ctlSerial&&wxOnWatch[ctlSerial]===undefined)wxFetchOnWatch(ctlSerial);
  renderControl(ctlCache[ctlSerial]||null);
}
function ctlFetch(){
  const s=ctlSerial;
  fetch('/api/watch/'+encodeURIComponent(s)).then(r=>r.json()).then(d=>{
    if(ctlSerial!==s)return;
    ctlCache[s]=d;
    // Push EVERY tab's metrics on every poll, so a tab's graph is already full
    // the instant you switch to it — the continuity a single window buys.
    // BUT only for a live poll: a stale poll is the same last-known values
    // repeated (watch off the bus), and pushing those advances the graph with
    // fake live motion. Freeze while stale — keep the real history already drawn.
    if(!d.stale){
      graphPush('load',_load1(d)); graphPush('mem',_memPct(d));
      graphPushRate('rx',d.net_rx); graphPushRate('tx',d.net_tx);
      graphPush('bcap',d.bat_cap==null?null:+d.bat_cap);
      graphPush('bvolt',d.bat_volt?+d.bat_volt/1e6:null);
      graphPush('bcur',d.bat_curr?+d.bat_curr/1000:null);
      graphPush('btemp',d.bat_temp==null?null:+d.bat_temp/10);
    }
    renderControl(d);
    clearTimeout(ctlPoll); ctlPoll=setTimeout(ctlFetch,panelPollMs(d));   // keep live while open
  }).catch(()=>{
    if(ctlSerial!==s)return;
    document.getElementById('cc').innerHTML=ctlChrome(null,`<div class="cc-sec"><span class="err">unreachable</span></div>`);
  });
}
// Shared window frame: title, the tab row, the active tab's body. Every tab
// renders into the same chrome, so the header and tabs never move on a switch.
function ctlChrome(d,body){
  const stale=!!(d&&d.stale);
  const tabs=CTL_TABS.map(([id,label])=>`<button class="cc-tab${ctlTab===id?' on':''}" onclick="ctlTabTo('${id}')">${label}</button>`).join('');
  return `<div class="cc-hd" id="cc-hd" onmousedown="ctlDragStart(event)">${esc(ctlName)} <span class="dim">${esc((d&&d.os)||'')}</span>${pollTag(d)}`+
      (stale?` <span class="warn" title="watch is off the bus — these are the last-known values">stale &middot; last live ${fmtAge(d.last_live_ts)} ago</span>`:'')+
      `<span class="cc-x" onclick="closeControl()">&times;</span></div>`+
    `<div class="cc-tabs">${tabs}</div>`+
    `<div class="cc-body">${body}</div>`;
}
function renderControl(d){
  const cc=document.getElementById('cc');
  // Never rebuild the panel out from under a text field being typed in (the
  // weather city input): a 3s poll mid-type dropped focus and the text (mo).
  // The graphs still fill (ctlFetch pushed them already); only the DOM refresh
  // is deferred until the field is blurred.
  const a=document.activeElement;
  if(a&&a.tagName==='INPUT'&&cc.contains(a))return;
  cc.classList.toggle('stale-cc',!!(d&&d.stale));
  const body=ctlTab==='set'?bodySet(d):ctlTab==='net'?bodyNet(d):ctlTab==='bat'?bodyBat(d):bodySys(d);
  cc.innerHTML=ctlChrome(d,body);
  ctlPlace(true);
}
// ── System tab ──────────────────────────────────────────────────────────────
function bodySys(d){
  if(!d||!d.kernel)return `<div class="cc-sec"><span class="err">no data (watch offline?)</span></div>`;
  const mt=+d.memtotal,mf=+d.memfree,memU=mt?Math.round((mt-mf)/1024):null,memT=mt?Math.round(mt/1024):null;
  const freq=_num(d.cpufreq);
  const dfp=(d.df||'').trim().split(/[ \t]+/);
  const storage=dfp.length>=5?`${dfp[2]} / ${dfp[1]} (${dfp[4]})`:null;
  const sys=_sec('System',
    _kv('Kernel',d.kernel)+_kv('Qt',d.qt)+_kv('SoC',(d.soc||'').trim())+
    _kv('CPU',freq?(freq/1000).toFixed(0)+' MHz':null)+
    _kv('Uptime',fmtUp(d.uptime))+_kv('Boot',d.bootreason)+
    _kvg('Load',d.load,spark('load',0,_ncpu(d),'high'))+_kv('Threads',d.threads)+
    _kvg('Memory',memU!=null?`${memU} / ${memT} MB`:null,spark('mem',0,100,'high'))+_kv('Storage',storage)+
    _kv('Resolution',d.resolution)+_kv('Timezone',d.tz)+_kv('Clock',d.datetime)+
    _kv('Machine (image)',d.geometry&&d.geometry.machine)+
    // The bootloader version string names the true hardware, which is the only
    // thing that distinguishes watches sharing an image (rover vs rubyfish).
    // Worth showing verbatim: it is the field the porting community reads to
    // identify a device, so a human can check our detection against it.
    _kv('Bootloader',d.geometry&&d.geometry.bootloader));
  return `<div class="cc-cols"><div class="cc-col">${sys}</div></div>`+
    `<div class="cc-tgls">`+
      `<button class="cc-tgl" onclick="ccBuzz()" title="vibrate to locate in the dock">Buzz</button>`+
      `<button class="cc-tgl${d.screen_forced?' scrnon':''}${ctlPending.has('sys:screen')?' cmd-pending':''}" onclick="ccScreen(${d.screen_forced?0:1})" title="${d.screen_forced?'demo mode is ON — the screen is forced on and draining. Click to release.':'force the screen on (mce demo mode — stays on and drains until released!)'}">Screen: ${d.screen_forced?'ON':'OFF'}</button>`+
      `<button class="cc-tgl" onclick="doScreenshot('${d.serial}')" title="screenshot in a new tab">Shot</button></div>`+
    bodyWeather();
}
function ccBuzz(){fetch('/api/watch/'+encodeURIComponent(ctlSerial)+'/buzz',{method:'POST'}).then(()=>toast('buzzed'));}
function ccScreen(on){ctlPending.add('sys:screen');renderControl(ctlCache[ctlSerial]||{});setTimeout(()=>{ctlPending.delete('sys:screen');},2600);fetch('/api/watch/'+encodeURIComponent(ctlSerial)+'/screen/'+(on?'on':'off'),{method:'POST'}).then(()=>{toast(on?'screen forced on \u2014 release it when done!':'screen released');ctlFetch();refresh();});}
function releaseScreen(s){fetch('/api/watch/'+encodeURIComponent(s)+'/screen/off',{method:'POST'}).then(()=>{toast('screen released');refresh()});}
function releaseAllScreens(){fetch('/api/screen/release-all',{method:'POST'}).then(r=>r.json()).then(d=>{toast('released '+((d.released||[]).length)+' screen(s)');refresh()});}
function ccSyncTime(){
  const b=document.getElementById('cc-time');if(b)b.textContent='syncing…';
  fetch('/api/watch/'+encodeURIComponent(ctlSerial)+'/settime',{method:'POST'})
    .then(()=>setTimeout(()=>{const bb=document.getElementById('cc-time');if(bb){bb.textContent='✓ synced';bb.classList.add('done');}ctlFetch();},700));
}
// ── Weather (fleet-wide location, host-fetched, synced to a watch) ───────────
// One location for the fleet; the host fetches Open-Meteo and can write it to a
// watch's weather dconf. Icons are the watch's own ios-* weather art mapped from
// the OWM condition code. wxData is global (weather is not per-watch), fetched
// once on demand and cached.
const WXICONS={
 sunny:'<path d="M248 400h16v64h-16zm0-352h16v64h-16zM48 248h64v16H48zm352 0h64v16h-64zM148.452 352.163l11.313 11.314-45.254 45.254-11.314-11.313zM397.49 103.262l11.313 11.313-45.255 45.255-11.313-11.314zM159.905 148.52l-11.314 11.313-45.254-45.254 11.313-11.314zM408.67 397.421l-11.313 11.314-45.255-45.255 11.314-11.313zM256 160c-52.9 0-96 43.1-96 96s43.1 96 96 96 96-43.1 96-96-43.1-96-96-96z"/>',
 partlysunny:'<path d="M160 64h16v54h-16zM16 208h55v16H16zm43.5-90.6 11-11.1 31.4 31.5-11 11.1zm179.9 30.5-11-11.1 31.3-31.5 11.1 11.1zM72.5 320.7l-11-11.1 31.4-31.5 11 11.1zM165 138.3c-40.5 0-73.3 32.8-73.3 73.3 0 36.8 27.1 67.3 62.5 72.5 0 0-1.2-42.9 18.9-72.9s51.8-42 51.8-42c-13.4-18.7-35.2-30.9-59.9-30.9z"/><path d="M403.3 259.2h-2.4c-3.1 0-6.1 0-9 .4-11.3-50.3-56.1-88.2-109.7-88.2-14.6 0-28.6 2.8-41.4 7.9-5.1 2-10 4.4-14.7 7.1-32 18.5-54.1 52.4-56.2 91.6-.1 2.1-.2 4.1-.2 6.2 0 3.4.2 6.8.5 10.1 0 .4.1.8.1 1.1-37.9 3.4-67.6 37.1-67.6 76 0 41.1 33.3 76.7 74.3 76.7h226.4c51.2 0 92.7-43.4 92.7-94.8-.1-51.4-41.6-94.1-92.8-94.1z"/>',
 cloudy:'<path d="M236 96c-70 0-127.8 59.7-127.8 130.8 0 4.3.3 8.6.8 12.8-43.2 3.9-77 44-77 88.4 0 47 37.9 88 84.6 88h257.8c58.3 0 105.6-49.4 105.6-108s-47.3-108.8-105.6-108.8c-2.3 0-4.8-.2-7.2-.2-2.1 0-4.2 0-6.1.1C349.3 145.6 306 96 236 96z"/>',
 rainy:'<path d="m374.4 143.2-13.3-.1C349.3 89.6 306 48 236 48S108.2 99.7 108.2 170.8l.3 4.8C66.2 181.2 32 220.1 32 264.5c0 47 37.9 88.5 84.6 88.5h10.6l-37.4 50.7c-2.6 3.6-1.8 8.3 1.8 10.9 1.3 1 2.9 1.4 4.4 1.4 2.3 0 5.1-.6 6.8-2.9L147 353h61.4l-72.3 99c-2.6 3.6-2.2 8 1.4 10.6 1.3 1 3.3 1.4 4.8 1.4 3.7 0 6.1-1.3 7.8-3.6l78-107.4h61.1l-37.3 50.7c-2.6 3.6-1.8 8.3 1.8 10.9 1.3 1 2.9 1.4 4.4 1.4 2.3 0 5.1-.6 6.8-2.9L309 353h61.4l-72.3 99c-2.6 3.6-2.1 7.8 1.5 10.3 1.3 1 3.2 1.7 4.7 1.7 2.3 0 5.1-.8 6.8-3.1l80.1-110.3c50.4-8.4 88.9-53.7 88.9-106.6-.1-58.6-47.4-100.8-105.7-100.8z"/>',
 snow:'<path d="m435.7 341.5-29.1-17c10.7-10.4 22.7-15.4 22.8-15.5 8.3-3.3 12.6-12.6 9.8-21-2.1-6.5-8.2-10.9-15-10.9-2.1 0-4.1.4-6 1.2-2.5 1-23.5 9.9-40.3 29.5L290.1 256l87.9-51.8c17.1 20.1 39.2 29.1 40.3 29.6 1.9.8 4 1.2 6 1.2 6.8 0 12.8-4.4 15-10.9 2.8-8.5-1.5-17.7-9.8-21-.1-.1-12.2-5.1-22.9-15.5l29.1-17c7.9-4.6 10.6-14.8 6.1-22.8-3-5.2-8.5-8.4-14.4-8.4-2.9 0-5.8.8-8.3 2.3l-29 16.9c-3.5-14.5-1.8-27.5-1.8-27.6 1.3-8.9-4.5-17.3-13.2-19.1-1.1-.2-2.2-.3-3.3-.3-7.8 0-14.3 5.6-15.6 13.4l-.1.3c-2.4 10.4-3.1 30.8 3.5 50.9L273 227.3V123.7c25-4.7 41.8-16.3 44.4-18.4 4.2-3.3 6.9-8.1 7.4-12.8.3-3.8-.9-7.5-3.3-10.3-3.2-3.6-8.4-5.6-14.3-5.6-4.4 0-8.4 1.2-11.4 3.4-1.4.9-8.8 6.5-22.8 10.5V56.7c0-9-7.8-16.7-17-16.7s-17 7.6-17 16.7v33.7c-11-3.7-18.6-8.7-22.7-11.4-4.1-2.8-9.1-4.2-12.1-4.2-2.9 0-9.8.1-13.7 6.6-3 4.9-2.8 9.2-2.4 11.8.5 2.9 1.9 6.3 5.5 10.2 3.6 3.9 23.4 16.1 45.4 20.3v103l-91.6-51.3c9.4-26 7.4-49.9 7.4-50.2-1.2-8.2-7-13.7-14.6-13.7-1.1 0-2.2.1-3.2.3-8.5 1.8-14 10-12.7 19.1.1.6 1.9 13.3-1.6 27.6l-29.8-16.9c-2.5-1.5-5.4-2.3-8.3-2.3-5.9 0-11.4 3.2-14.4 8.4-4.5 7.9-1.8 18.1 6.1 22.8l29.1 17c-10.7 10.3-22.7 15.4-22.8 15.5-8.3 3.3-12.6 12.6-9.8 21 2.1 6.5 8.2 10.9 15 10.9 2.1 0 4.1-.4 6-1.2 1-.4 23.1-9.5 40.3-29.6l89.9 51.8-89.9 51.8c-16.7-19.7-37.7-28.5-40.3-29.5-1.9-.8-4-1.2-6-1.2-6.8 0-12.8 4.4-15 10.9-2.8 8.5 1.5 17.7 9.7 21 .1.1 12.2 5.2 22.9 15.5l-29.1 17c-7.9 4.6-10.6 14.8-6.1 22.8 3 5.2 8.5 8.4 14.4 8.4 2.9 0 5.8-.8 8.3-2.3l29-16.9c3.5 14.5 1.8 27.5 1.8 27.6-1.3 8.9 4.5 17.2 13.2 19.1 1.1.2 2.2.3 3.3.3 7.9 0 14.5-5.8 15.6-13.7.5-3.4 3.2-26.8-5.4-50.2l88.6-51.3v103c-21 4.2-39.8 16.4-45.4 21.3l-.1.1c-2.9 2.3-4.6 5.6-4.9 9.3-.4 4.6 1.4 9.6 5.1 13.7 1.2 1.4 5 5.6 10.8 5.6 3.1 0 6.1-1.2 9.2-3.6l.5-.4c1-.9 13-8.8 25-12.7v33.7c0 9 7.8 16.7 17 16.7s17-7.6 17-16.7v-33.9c15 4 22.2 10.6 23.8 11.6 2.9 2.2 6.8 3.3 10.9 3.3 5.6 0 10.6-2 13.7-5.6 2.3-2.7 3.5-6.1 3.2-9.6-.4-4.7-3.5-9.7-8.1-13.4-.2-.2-16.5-14.4-43.5-19.4V285.7l86.6 51.1c-7.2 21.3-4.8 41.6-3.3 49.8v.2c1.2 7.8 6.3 13.6 14.5 13.6 1.1 0 2.2-.1 3.3-.3 8.8-1.8 14.9-10.3 13.7-19-.1-.8-1.4-13.6 2-27.8l29.1 17.1c2.5 1.5 5.4 2.5 8.3 2.5 6 0 11.5-3.4 14.4-8.6 4.5-7.8 1.8-18.2-6.1-22.8z"/>',
 thunderstorm:'<path d="m374.4 141.9-13.3-.1C349.4 88.2 306 48 236 48S108.2 98.4 108.2 169.5l.3 4.8C66.3 179.9 32 219.6 32 264c0 47 37.9 88 84.7 88h96.8l8.6-32h-70.9l4.3-19.5 32-144 2.8-12.5h135.9l-6.2 20.6-17.8 59.4H370l-15.4 24.5L289.4 352H367c72 0 113-52 113-110 0-58.6-47.3-100.1-105.6-100.1z"/><path d="M341 240h-60.3l24-80H203l-32 144h72l-42.9 160z"/>'
};
function wxIcon(id){id=+id;
  if(id>=200&&id<300)return 'thunderstorm';
  if(id===511||(id>=600&&id<700))return 'snow';
  if(id>=300&&id<600)return 'rainy';
  if(id>=700&&id<800)return 'cloudy';
  if(id===800)return 'sunny';
  if(id===801)return 'partlysunny';
  return 'cloudy';
}
let wxData=null, wxOnWatch={};   // wxData = incoming forecast (global); wxOnWatch = per-serial stored
function wxFetch(){
  fetch('/api/weather').then(r=>r.json()).then(d=>{
    wxData=d;
    if(ctlSerial&&ctlTab==='sys')renderControl(ctlCache[ctlSerial]||{});
  }).catch(()=>{});
}
function wxFetchOnWatch(serial){
  wxOnWatch[serial]=null;   // "reading…" until it lands
  fetch('/api/watch/'+encodeURIComponent(serial)+'/weather-on-watch').then(r=>r.json()).then(d=>{
    wxOnWatch[serial]=(d&&d.ok)?d.weather:{};
    if(ctlSerial===serial&&ctlTab==='sys')renderControl(ctlCache[serial]||{});
  }).catch(()=>{wxOnWatch[serial]={};});
}
function wxSetLocation(){
  const inp=document.getElementById('wxcity'); if(!inp||!inp.value.trim())return;
  const city=inp.value.trim(); inp.blur();   // let the panel re-render with the result
  toast('locating '+city+'\\u2026');
  fetch('/api/weather/location/'+encodeURIComponent(city),{method:'POST'}).then(r=>r.json()).then(d=>{
    if(d.ok){toast('location: '+d.location.city);wxData=null;wxFetch();}
    else toast(d.error||'city not found');
  }).catch(()=>toast('set location failed'));
}
function wxSync(serial){
  toast('syncing weather\\u2026');
  fetch('/api/watch/'+encodeURIComponent(serial)+'/weather-sync',{method:'POST'}).then(r=>r.json()).then(d=>{
    toast(d.ok?('weather synced'+(d.city?': '+d.city:'')):('weather sync failed'+(d.error?' \\u2014 '+d.error:'')));
    if(d.ok)wxFetchOnWatch(serial);   // refresh the on-watch line to what we just wrote
  }).catch(()=>toast('weather sync failed'));
}
function _wxOnWatchLine(){
  const w=ctlSerial?wxOnWatch[ctlSerial]:undefined;
  if(w===undefined)return '';
  if(w===null)return `<div class="wx-onwatch dim">on watch: reading&hellip;</div>`;
  const has=w.city||(w.days&&w.days.length);
  if(!has)return `<div class="wx-onwatch dim">on watch: nothing stored yet</div>`;
  const d0=(w.days&&w.days[0])||{};
  const t=(d0.min_k!=null&&d0.max_k!=null)?` ${d0.min_k-273}\\u00b0 / ${d0.max_k-273}\\u00b0`:'';
  const age=w.timestamp?` <span class="dim">&middot; ${fmtAge(w.timestamp)} old</span>`:'';
  return `<div class="wx-onwatch">on watch: <b>${esc(w.city||'?')}</b>${t}${age}</div>`;
}
function bodyWeather(){
  if(!wxData)return '';   // not fetched yet — the System tab triggers wxFetch
  const loc=wxData.location, days=wxData.days||[];
  const setter=`<div class="wx-set"><input id="wxcity" class="wx-in" placeholder="set city\\u2026" onkeydown="if(event.key==='Enter')wxSetLocation()"><button class="cc-act mini" onclick="wxSetLocation()">Set</button></div>`;
  if(!loc||!days.length){
    return `<div class="cc-sec"><div class="cc-sech">Weather</div>`+
      `<div class="wx-none">${loc?esc(loc.city)+' \\u2014 no forecast':'no location set'}</div>${_wxOnWatchLine()}${setter}</div>`;
  }
  const d0=days[0], icon=`<svg class="wxi" viewBox="0 0 512 512">${WXICONS[wxIcon(d0.id)]||''}</svg>`;
  return `<div class="cc-sec"><div class="cc-sech">Weather</div>`+
    `<div class="wx-row">${icon}<div class="wx-t"><div class="wx-temp">${d0.min_c}\\u00b0 / ${d0.max_c}\\u00b0</div>`+
      `<div class="wx-city">${esc(loc.city)} <span class="dim">will sync</span></div></div>`+
      `<button class="cc-act mini" onclick="wxSync('${ctlSerial}')" title="write this forecast to the watch">Sync to watch</button></div>`+
    `${_wxOnWatchLine()}${setter}</div>`;
}

// ── Network tab ─────────────────────────────────────────────────────────────
// Addresses, links, the WiFi/BT toggles and the USB IP/mode switch — the detail
// that would crowd the System view. The USB-mode toggle lives here, a deliberate
// click rather than the misclick-prone inline badge.
function bodyNet(d){
  d=d||{};
  const mb=x=>{const n=_num(x);return n==null?null:(n/1048576).toFixed(2)+' MB';};
  const phone=(+d.btcount>0)?(d.btmac||'connected'):'none';
  // The link that answered (d.transport) is the watch's real USB gadget mode;
  // ctlMode/ctlSshIp only override it transiently right after a manual switch.
  const mode=ctlMode||d.transport||'adb', isSsh=mode==='ssh';
  const usbip=ctlSshIp||d.ssh_ip||(isSsh?'192.168.13.37':'192.168.2.15');
  const net=_sec('Addresses &amp; links',
    _kv('USB IP',usbip)+_kv('USB mode',isSsh?'SSH (developer)':'ADB')+
    _kv('WiFi',d.wifi==null?null:(d.wifi?'on':'off'))+_kv('WiFi IP',d.ip)+
    _kvg('RX / TX',(mb(d.net_rx)||'0')+' / '+(mb(d.net_tx)||'0'),spark('rx',0,500000,'high')+spark('tx',0,500000,'high'))+
    _kv('Bluetooth',d.bluetooth==null?null:(d.bluetooth?'on':'off'))+_kv('Phone',phone)+
    _kv('WLAN MAC',d.wlanmac)+_kv('BT MAC',d.btmac_self)+_kv('Serial',d.serial));
  const tgl=(t,l,on)=>`<button class="cc-tgl${on?' on':''}${ctlPending.has('net:'+t)?' cmd-pending':''}" onclick="ncToggle('${t}',${on?0:1})">${l}: ${on?'ON':'OFF'}</button>`;
  const modeToggle=isSsh
    ? `<button class="cc-tgl" onclick="switchAdb('${esc(d.serial||ctlSerial)}')" title="switch this watch's USB gadget back to ADB">USB &#8594; ADB</button>`
    : `<button class="cc-tgl" onclick="switchSsh('${esc(d.serial||ctlSerial)}')" title="switch this watch's USB gadget to SSH/developer mode">USB &#8594; SSH</button>`;
  return `<div class="cc-cols"><div class="cc-col">${net}</div></div>`+
    `<div class="cc-tgls">${tgl('wifi','WiFi',d.wifi)}${tgl('bluetooth','BT',d.bluetooth)}${modeToggle}</div>`;
}
function ncToggle(tech,on){
  ctlPending.add('net:'+tech); renderControl(ctlCache[ctlSerial]||{});   // pulse until the read reflects it
  setTimeout(()=>{ctlPending.delete('net:'+tech);},2600);
  fetch('/api/watch/'+encodeURIComponent(ctlSerial)+'/toggle/'+tech+'/'+(on?'on':'off'),{method:'POST'})
    .then(()=>setTimeout(ctlFetch,1600)).catch(()=>ctlFetch());
}

// ── Battery tab ─────────────────────────────────────────────────────────────
// There is nothing to *control* about a battery, so this is read-only detail:
// voltage, current, temperature, cycles, health, measured standby drain, and
// the fetched-once history chart at its foot.
function bodyBat(d){
  d=d||{};
  const bv=_num(d.bat_volt),ba=_num(d.bat_curr),bt=_num(d.bat_temp),uv=_num(d.usb_volt);
  const cur=ba==null?null:`${(ba/1000).toFixed(0)} mA ${ba<-5?'\\u25bc':ba>5?'\\u25b2':''}`;
  const bat=_sec('Battery',
    _kvg('Charge',d.bat_cap!=null&&d.bat_cap!==''?d.bat_cap+'%':null,spark('bcap',0,100,'low'))+_kv('Status',d.bat_status)+
    _kv('Health',d.bat_health)+_kv('Tech',d.bat_tech)+
    _kvg('Voltage',bv?(bv/1e6).toFixed(3)+' V':null,spark('bvolt',3.2,4.35,'low'))+_kvg('Current',cur,spark('bcur',-600,600,'low'))+
    _kvg('Temp',bt!=null?(bt/10).toFixed(1)+' °C':null,spark('btemp',15,50,'high'))+_kv('Cycles',d.bat_cycles)+
    _kv('USB in',uv!=null&&uv>0?(uv/1e6).toFixed(2)+' V':(+d.usb_online?'online':null))+
    _kv('Standby',d.standby_measured!=null?`${d.standby_measured} %/h · ~${fmtDur(85/d.standby_measured)}`:null));
  const hist=biHist[ctlSerial], histPts=(hist&&hist.points)||[];
  const histSec=histPts.length>=2
    ? `<div class="cc-sec"><div class="cc-sech">Battery history`
        +(hist.rate?` <span class="dim">~${(+hist.rate).toFixed(2)}%/h standby</span>`:'')
        +`</div>${sparkSvg(histPts)}</div>`
    : '';
  return `<div class="cc-cols"><div class="cc-col">${bat}</div></div>`+histSec;
}
// ── Settings tab ────────────────────────────────────────────────────────────
// A mirror of the watch's own settings, limited to what the other tabs don't
// already control (mo): the boolean prefs are live toggles that write dconf;
// watchface/launcher/wallpaper show read-only (a fleet manager rarely sets them
// remotely). Fetched on demand like the battery history, cached per serial.
let avData={};   // per-serial {brightness, has_speaker, volume, muted}
function settingsFetch(serial){
  fetch('/api/watch/'+encodeURIComponent(serial)+'/settings').then(r=>r.json()).then(d=>{
    if(ctlSerial!==serial)return;
    ctlSettings[serial]=d;
    ctlPending.clear();   // the fresh state reflects any writes — stop their pulse
    if(ctlTab==='set')renderControl(ctlCache[serial]||{});
  }).catch(()=>{
    if(ctlSerial!==serial)return;
    ctlSettings[serial]={ok:false,error:'unreachable'};
    if(ctlTab==='set')renderControl(ctlCache[serial]||{});
  });
  fetch('/api/watch/'+encodeURIComponent(serial)+'/av').then(r=>r.json()).then(a=>{
    if(ctlSerial!==serial||!a||!a.ok)return;
    avData[serial]=a;
    if(ctlTab==='set')renderControl(ctlCache[serial]||{});
  }).catch(()=>{});
}
function settingsWrite(key,on){
  const s=ctlSerial;
  ctlPending.add('set:'+key); renderControl(ctlCache[s]||{});   // pulse until confirmed
  fetch('/api/watch/'+encodeURIComponent(s)+'/setting/'+(on?'on':'off')+key,{method:'POST'})
    .then(r=>r.json()).then(d=>{if(!d.ok)toast('setting write failed');setTimeout(()=>settingsFetch(s),400);})
    .catch(()=>{toast('setting write failed');settingsFetch(s);});
}
// Quick-panel toggle mirror: each toggle is an icon in a grey circle, dimmed
// when the toggle is disabled in the watch's quick panel and full when enabled;
// a click flips it (the backend rewrites the whole dconf dict). Icons are the
// watch's own ios-* art (asteroid-icons-ion, 512-grid fill paths, the clean
// filled sibling where the settings icon was Inkscape-messy); tooltips carry
// the toggle name (mo).
const QPICONS={
 lockButton:'<path d="M256 304c-8.822 0-16 7.178-16 16s7.178 16 16 16 16-7.178 16-16-7.178-16-16-16z"/><path d="M168 224v-72c0-48.523 39.484-88 88.016-88C304.531 64 344 103.477 344 152v8h16v-8c0-57.43-46.562-104-103.984-104C198.562 48 152 94.57 152 152v72H96v240h320V224H168zm96 126.992V384h-16v-33.008c-13.802-3.553-24-16.082-24-30.992 0-17.673 14.327-32 32-32s32 14.327 32 32c0 14.91-10.198 27.439-24 30.992z"/>',
 settingsButton:'<path d="M411.1 256c0-23.9 14.8-42.8 36.9-55.8-4-13.3-9.3-26.2-15.8-38.2-24.9 6.5-45-3.2-62-20.2-16.9-16.9-22.1-37.1-15.6-62-12-6.5-24.8-11.8-38.2-15.8-13 22.2-36.4 36.9-60.4 36.9-23.9 0-47.4-14.7-60.4-36.9-13.4 4-26.2 9.3-38.2 15.8 6.5 24.9 1.3 45-15.6 62-16.9 16.9-37.1 26.7-61.9 20.2-6.6 12-11.9 24.8-15.9 38.2 22.2 13 37 31.9 37 55.8s-14.8 47.4-37 60.4c4 13.4 9.3 26.2 15.8 38.2 24.9-6.5 45-1.3 61.9 15.6 17 16.9 22.1 37.1 15.6 62 12.1 6.5 24.8 11.8 38.2 15.8 13-22.2 36.5-36.9 60.4-36.9s47.4 14.7 60.4 36.9c13.4-4 26.2-9.3 38.2-15.8-6.5-24.9-1.3-45 15.6-62 16.9-16.9 37.1-26.7 62-20.2 6.5-12.1 11.8-24.9 15.8-38.2-22.1-13-36.8-31.9-36.8-55.8zM256 352c-52.9 0-96-43-96-96s43-96 96-96 96 43 96 96-43 96-96 96z"/>',
 brightnessToggle:'<path d="M248 400h16v64h-16zm0-352h16v64h-16zM48 248h64v16H48zm352 0h64v16h-64zM148.452 352.163l11.313 11.314-45.254 45.254-11.314-11.313zM397.49 103.262l11.313 11.313-45.255 45.255-11.313-11.314zM159.905 148.52l-11.314 11.313-45.254-45.254 11.313-11.314zM408.67 397.421l-11.313 11.314-45.255-45.255 11.314-11.313zM256 160c-52.9 0-96 43.1-96 96s43.1 96 96 96 96-43.1 96-96-43.1-96-96-96z"/>',
 bluetoothToggle:'<path d="m286 256 98-87L255.8 32H240v180l-89.4-77-22.6 25 112 96-112 96 22.6 25.8L240 299v181h15.8l.2-.4L384 344l-98-88zm51.8 88.5L272 415V287.2l65.8 57.3zM272 225.6V97.1l65.8 71.2-65.8 57.3z"/>',
 hapticsToggle:'<path d="M364.172 293.613c11.988-34.529 6.316-70.638-11.91-98.966l14.166-82.325L258.106 74.71l-39.9 73.389c-31.861 10.937-58.691 35.76-70.68 70.289-11.988 34.525-6.315 70.633 11.911 98.965l-14.167 82.324 108.322 37.613 39.9-73.391c31.862-10.938 58.692-35.76 70.68-70.286zm-189.565-65.821c15.515-44.685 64.771-68.547 109.452-53.034 44.68 15.515 68.548 64.768 53.031 109.453-15.514 44.68-64.77 68.547-109.45 53.031-44.68-15.513-68.547-64.771-53.033-109.45z"/>',
 wifiToggle:'<path d="M256 112c72.3 0 146.5 29.1 201.4 78.4L442 206.7c-22.9-20.4-48.7-36.8-77-48.7-34.5-14.6-71.2-22-109-22s-74.5 7.4-109 22c-28.3 12-54.1 28.3-77 48.7l-15.4-16.3C109.5 141.1 183.7 112 256 112m0-16c-83 0-166.1 35.8-224 93.7l37.3 39.6c24.3-24.3 52.5-43.3 83.9-56.6C185.8 159 220.3 152 256 152s70.2 7 102.7 20.7c31.4 13.3 59.7 32.3 83.9 56.6l37.3-39.6C422.1 131.8 339 96 256 96z"/><path d="M256 225c45.6 0 88.9 15.9 123.4 44.9l-17 17c-29.9-24.6-67.2-38-106.4-38s-76.5 13.4-106.4 38l-17-17c34.5-29 77.8-44.9 123.4-44.9m0-16c-57.2 0-109 23.1-146.6 60.4L149 309c28.7-28.4 66.6-44 107-44 40.4 0 78.3 15.6 107 44l39.6-39.6C365 232.1 313.2 209 256 209zm0 128c15.4 0 29.9 5.4 41.3 15.1L256 393.4l-41.3-41.3c11.4-9.7 25.9-15.1 41.3-15.1m0-16c-25.9 0-48.9 12.3-63.6 31.4L256 416l63.6-63.6C304.9 333.3 281.9 321 256 321z"/>',
 soundToggle:'<path d="m374.1 128-13.6 10.3C384.6 171.2 399 211.9 399 256c0 44.1-14.4 84.8-38.6 117.7L374 384c26.3-35.7 41.9-80 41.9-128s-15.5-92.3-41.8-128zM320 351.8c20-26.8 32-59.9 32-95.8s-12-69-32-95.8l-13.6 10.1c17.9 24 28.6 53.6 28.6 85.7s-10.7 61.7-28.6 85.7l13.6 10.1zm-46.9-31.9C286.8 302 295 280 295 256s-8.2-46-21.9-63.9l-13.5 9.8c11.6 15.1 18.5 33.8 18.5 54.1s-6.9 38.9-18.5 54.1l13.5 9.8zM153.9 216H96v80h57.9l70.1 56V160z"/>',
 cinemaToggle:'<path d="M56 88v336h400V88H56zm72 320H72v-48h56v48zm0-64H72v-48h56v48zm0-64H72v-48h56v48zm0-64H72v-48h56v48zm0-64H72v-48h56v48zm240 256H144V264h224v144zm0-160H144V104h224v144zm72 160h-56v-48h56v48zm0-64h-56v-48h56v48zm0-64h-56v-48h56v48zm0-64h-56v-48h56v48zm0-64h-56v-48h56v48z"/>',
 aodToggle:'<path d="M337.254 336.707c25.746-25.944 36.253-60.953 32.076-94.378l48.387-68.094-81.39-80.772-67.722 48.905c-33.457-3.923-68.385 6.85-94.131 32.794-25.745 25.941-36.25 60.95-32.075 94.378l-48.387 68.093 81.389 80.772 67.723-48.906c33.458 3.922 68.385-6.85 94.13-32.792z"/><path d="M61.547 248a8 8 0 0 0-8 8 8 8 0 0 0 8 8h53.86a8 8 0 0 0 8-8 8 8 0 0 0-8-8zm335.047 0a8 8 0 0 0-8 8 8 8 0 0 0 8 8h53.86a8 8 0 0 0 8-8 8 8 0 0 0-8-8zM256 388.594a8 8 0 0 0-8 8v53.86a8 8 0 0 0 8 8 8 8 0 0 0 8-8v-53.86a8 8 0 0 0-8-8m0-335.047a8 8 0 0 0-8 8v53.86a8 8 0 0 0 8 8 8 8 0 0 0 8-8v-53.86a8 8 0 0 0-8-8m99.416 293.869a8 8 0 0 0-5.658 2.342 8 8 0 0 0 0 11.314l38.086 38.084a8 8 0 0 0 11.312 0 8 8 0 0 0 0-11.312l-38.084-38.086a8 8 0 0 0-5.656-2.342M118.5 110.5a8 8 0 0 0-5.656 2.344 8 8 0 0 0 0 11.312l38.084 38.086a8 8 0 0 0 11.314 0 8 8 0 0 0 0-11.314l-38.086-38.084a8 8 0 0 0-5.656-2.344"/>',
 powerOffToggle:'<path d="M390.7 99.7c-2.8-2.4-6.4-3.7-10.2-3.7-4.6 0-9 2-11.9 5.5-2.7 3.2-4 7.2-3.7 11.4.3 4.2 2.3 8 5.5 10.7 39.5 33.5 62.2 82.1 62.2 133.3 0 96.9-79.2 175.8-176.6 175.8S79.4 353.8 79.4 256.9c0-51.3 22.7-99.9 62.2-133.3 3.2-2.7 5.2-6.5 5.5-10.7.3-4.2-1-8.2-3.7-11.4-3-3.5-7.3-5.5-11.9-5.5-3.7 0-7.3 1.3-10.2 3.7C74.7 139.1 48 196.4 48 256.9 48 371.1 141.3 464 256 464s208-92.9 208-207.1c0-60.5-26.7-117.8-73.3-157.2z"/><path d="M257 272c8.8 0 16-7.2 16-16V64c0-8.8-7.2-16-16-16s-16 7.2-16 16v192c0 8.8 7.2 16 16 16z"/>',
 rebootToggle:'<path d="M256 384.1c-70.7 0-128-57.3-128-128.1s57.3-128.1 128-128.1V84l96 64-96 55.7v-55.8c-59.6 0-108.1 48.5-108.1 108.1 0 59.6 48.5 108.1 108.1 108.1S364.1 316 364.1 256H384c0 71-57.3 128.1-128 128.1z"/>',
 musicButton:'<path d="M406.3 48.2c-4.7.9-202 39.2-206.2 40-4.2.8-8.1 3.6-8.1 8v240.1c0 1.6-.1 7.2-2.4 11.7-3.1 5.9-8.5 10.2-16.1 12.7-3.3 1.1-7.8 2.1-13.1 3.3-24.1 5.4-64.4 14.6-64.4 51.8 0 31.1 22.4 45.1 41.7 47.5 2.1.3 4.5.7 7.1.7 6.7 0 24-1.3 39.2-11.2 11-7.2 24.1-21.4 24.1-47.8V186l192-39v140.7c0 4.1-.2 8.9-2.5 13.4-3.1 5.9-8.5 10.2-16.2 12.7-3.3 1.1-7.8 2.1-13.1 3.3-24.1 5.4-64.4 14.5-64.4 51.7 0 33.7 26.3 45.6 41.8 47.3 1.2.1 2.6.1 4.1.1 10.2 0 25.7-2.5 38.8-10 17.9-10.3 27.5-26.8 27.5-48.2V55.9c-.1-4.4-3.8-8.9-9.8-7.7zM192 404.8c0 15.5-5.6 27.3-16.8 34.6-11.9 7.8-26.3 8.7-30.5 8.7-1.9 0-3.6-.4-5.1-.6-2.3-.3-10.5-1.9-17.3-7.4-6.9-5.5-10.4-13.7-10.4-24.2 0-22.6 24.6-30 51.9-36.2 5.7-1.3 10.6-2.4 14.6-3.7 5.5-1.8 9.5-4.2 13.5-6.9v35.7zm208-47c0 15.5-6.6 26.9-19.4 34.3-10.4 5.9-23.1 7.9-30.9 7.9-1.3 0-2.1.1-2.4 0-4.6-.5-27.6-4.2-27.6-31.2 0-22.6 24.6-30 51.9-36.2 5.7-1.3 10.7-2.4 14.7-3.7 5.5-1.8 9.6-4.2 13.6-6.9v35.8zm0-226.9-192 38v-66l192-37.2v65.2z"/>',
 flashlightButton:'<path d="M400 188.8C400 110.9 333.9 48 256 48s-144 62.9-144 140.8c0 31.1 13.2 59.1 30.2 83.1h-.3c10.9 15 21.4 27.7 31.5 45 22 37.8 18.6 74.3 18.7 81.5v1.5h128v-1.5c0-8.9-3.6-43.7 18.4-81.5 10.1-17.3 20.6-30 31.5-45h-.1c16.9-23.9 30.1-52 30.1-83.1zm-49 81.5c-.6.8-1.1 1.5-1.7 2.3-8.1 10.9-16.5 22.2-24.7 36.2-17.3 29.7-20.4 58.2-20.8 75.2H288V271.9l32-63.9h-16.6L272 271.9V384h-32V271.9L208.6 208H192l32 63.9V384h-15.9c-.5-17-3.9-45.7-20.9-75-4.5-7.7-9.1-15-13.7-21h.2l-18.6-25.6c-15.8-21.6-27.1-47.1-27.1-73.6 0-33.4 16-64.9 39.6-88.5 23.6-23.6 55-36.5 88.4-36.5s64.8 12.8 88.4 36.4c23.6 23.6 39.6 55 39.6 88.4 0 26.5-11.3 51.9-27.1 73.6l-5.9 8.1zM224 448h64v16h-64zm-16-32h96v16h-96z"/>'
};
function bodyQuickpanel(qp){
  if(!qp||!qp.length)return '';
  const btns=qp.map(t=>`<button class="qpb${t.enabled?' on':''}${ctlPending.has('qp:'+t.id)?' cmd-pending':''}" title="${esc(t.label)}" onclick="quickpanelSet('${t.id}',${t.enabled?0:1})"><svg class="qpi" viewBox="0 0 512 512">${QPICONS[t.id]||''}</svg></button>`).join('');
  return `<div class="cc-sec"><div class="cc-sech">Quick panel</div><div class="qp">${btns}</div></div>`;
}
function quickpanelSet(id,on){
  const s=ctlSerial;
  ctlPending.add('qp:'+id); renderControl(ctlCache[s]||{});   // pulse until confirmed
  fetch('/api/watch/'+encodeURIComponent(s)+'/quickpanel/'+id+'/'+(on?'on':'off'),{method:'POST'})
    .then(r=>r.json()).then(d=>{if(!d.ok)toast('quickpanel write failed');setTimeout(()=>settingsFetch(s),400);})
    .catch(()=>{toast('quickpanel write failed');settingsFetch(s);});
}
// ── Clock (arbitrary time) — the top of the Settings tab ────────────────────
// Spinners for hour/min and day/month/year, each reacting to the mouse wheel
// and to its ▲▼, matching the watch's own spinner UI. The dialled value lives
// in ctlDate so a 3s poll re-render can't reset it mid-adjust. Set clock applies
// it; Sync from host (moved here from the System tab) resets it to the host.
function _dateNow(){const t=new Date();return {y:t.getFullYear(),mo:t.getMonth()+1,d:t.getDate(),h:t.getHours(),mi:t.getMinutes()};}
function _daysInMonth(y,mo){return new Date(y,mo,0).getDate();}
function ctlDateAdj(f,delta){
  const D=ctlDate; if(!D)return;
  ctlDateTouched=true;                     // the user is dialing — stop tracking now
  if(f==='h')D.h=(D.h+delta+24)%24;
  else if(f==='mi')D.mi=(D.mi+delta+60)%60;
  else if(f==='mo')D.mo=(D.mo+delta+11)%12+1;
  else if(f==='y')D.y=Math.min(2099,Math.max(1970,D.y+delta));
  else if(f==='d'){const dim=_daysInMonth(D.y,D.mo);D.d=(D.d-1+delta+dim)%dim+1;}
  const dim=_daysInMonth(D.y,D.mo); if(D.d>dim)D.d=dim;   // clamp after a shorter month
  renderControl(ctlCache[ctlSerial]||{});
}
function ctlDateWheel(e,f){e.preventDefault();ctlDateAdj(f,e.deltaY<0?1:-1);}
function ctlDateApply(){
  const s=ctlSerial,z=n=>String(n).padStart(2,'0'),D=ctlDate;
  const when=`${D.y}-${z(D.mo)}-${z(D.d)} ${z(D.h)}:${z(D.mi)}:00`;
  fetch('/api/watch/'+encodeURIComponent(s)+'/datetime/'+encodeURIComponent(when),{method:'POST'})
    .then(r=>r.json()).then(d=>toast(d.ok?'clock set: '+when:'set clock failed'))
    .catch(()=>toast('set clock failed'));
}
function bodyClock(d){
  // Track the live clock until the user dials a spinner, then hold their pick —
  // so the preselected time is "now" by default and freezes only once grabbed.
  if(ctlDate===null||!ctlDateTouched)ctlDate=_dateNow();
  const z=n=>String(n).padStart(2,'0'), D=ctlDate;
  const spin=(f,val,lbl)=>`<div class="spin" onwheel="ctlDateWheel(event,'${f}')" title="scroll or use the arrows to change the ${lbl}">`+
    `<button class="spin-b" tabindex="-1" onclick="ctlDateAdj('${f}',1)">&#9650;</button>`+
    `<div class="spin-v">${val}</div>`+
    `<button class="spin-b" tabindex="-1" onclick="ctlDateAdj('${f}',-1)">&#9660;</button>`+
    `<div class="spin-l">${lbl}</div></div>`;
  const spins=spin('h',z(D.h),'hr')+spin('mi',z(D.mi),'min')+`<div class="spin-sep"></div>`+
    spin('d',z(D.d),'day')+spin('mo',z(D.mo),'mon')+spin('y',D.y,'year');
  return `<div class="cc-sec"><div class="cc-sech">Clock</div>`+
    `<div class="spins">${spins}</div>`+
    `<div class="cc-tgls">`+
      `<button class="cc-act mini" onclick="ctlDateApply()" title="set the watch clock to the dialled time">Set clock</button>`+
      `<button class="cc-act mini" id="cc-time" onclick="ccSyncTime()" title="reset the watch clock + timezone to the host">Sync from host</button>`+
    `</div></div>`;
}
function bodySet(d){return bodyClock(d)+bodyAV()+bodyQuickpanel((ctlSettings[ctlSerial]||{}).quickpanel)+bodySetGroups();}
function bodyAV(){
  const av=avData[ctlSerial]; if(!av)return '';
  const snap=(v,d)=>v!=null?Math.round(v/10)*10:d;   // to the nearest 10 (the slider step)
  const slider=(val,min,fn)=>`<input type="range" class="av-range" min="${min}" max="100" step="10" value="${val}" `+
    `oninput="this.nextElementSibling.textContent=this.value+'%'" onchange="${fn}(this.value)">`+
    `<span class="av-val">${val}%</span>`;
  let items=`<div class="cc-k">Brightness</div><div class="cc-v av-sl">${slider(snap(av.brightness,50),10,'avBright')}</div>`;
  if(av.has_speaker){
    items+=`<div class="cc-k">Volume</div><div class="cc-v av-sl">${slider(snap(av.volume,50),0,'avVol')}</div>`;
    const m=!!av.muted;
    items+=`<div class="cc-k">Mute</div><div class="cc-v">`+
      `<button class="cc-tgl set-tgl${m?' on':''}${ctlPending.has('av:mute')?' cmd-pending':''}" onclick="avMute(${m?0:1})">${m?'ON':'OFF'}</button></div>`;
  }
  if(av.has_mic){
    items+=`<div class="cc-k">Microphone</div><div class="cc-v av-mic" id="av-mic">`+
      `<button class="cc-act mini" onclick="avRecord()" title="record 5s of mic audio, then play it back / download">Record 5s</button></div>`;
  }
  return `<div class="cc-sec"><div class="cc-sech">Display &amp; Sound</div><div class="cc-grid">${items}</div></div>`;
}
function avRecord(){
  const box=document.getElementById('av-mic'), s=ctlSerial; if(!box)return;
  box.innerHTML='<span class="dim">recording 5s&hellip;</span>';
  fetch('/api/watch/'+encodeURIComponent(s)+'/record/5',{method:'POST'}).then(r=>r.json()).then(d=>{
    if(document.getElementById('av-mic')!==box)return;
    if(d&&d.ok){
      const u='/api/watch/'+encodeURIComponent(s)+'/recording.wav?t='+Date.now();
      box.innerHTML='<audio controls src="'+u+'" style="height:30px;max-width:190px;vertical-align:middle"></audio>'+
        '<a class="cc-act mini" href="'+u+'" download="'+esc(s)+'.wav">download</a>'+
        '<button class="cc-act mini" onclick="avRecord()">re-record</button>';
    }else{
      box.innerHTML='<span class="err">record failed</span> <button class="cc-act mini" onclick="avRecord()">retry</button>';
      toast('record failed'+(d&&d.error?': '+d.error:''));
    }
  }).catch(()=>{box.innerHTML='<span class="err">record failed</span> <button class="cc-act mini" onclick="avRecord()">retry</button>';});
}
function _avPost(path,fn){
  const s=ctlSerial;
  fetch('/api/watch/'+encodeURIComponent(s)+'/'+path,{method:'POST'})
    .then(r=>r.json()).then(fn).catch(()=>toast('failed'));
}
function avBright(v){_avPost('brightness/'+v,d=>{if(avData[ctlSerial])avData[ctlSerial].brightness=+v;if(!d||!d.ok)toast('brightness failed');});}
function avVol(v){_avPost('volume/'+v,d=>{if(avData[ctlSerial])avData[ctlSerial].volume=+v;if(!d||!d.ok)toast('volume failed');});}
function avMute(on){
  ctlPending.add('av:mute'); renderControl(ctlCache[ctlSerial]||{});
  _avPost('mute/'+(on?'on':'off'),d=>{ctlPending.delete('av:mute');
    if(d&&d.ok&&avData[ctlSerial])avData[ctlSerial].muted=!!on; else toast('mute failed');
    renderControl(ctlCache[ctlSerial]||{});});
}
function bodySetGroups(){
  const st=ctlSettings[ctlSerial];
  if(!st)return `<div class="cc-sec"><span class="dim">loading&hellip;</span></div>`;
  if(!st.ok)return `<div class="cc-sec"><span class="err">${esc(st.error||'unreachable')}</span></div>`;
  const rows=st.settings||[], order=[], byGroup={};
  rows.forEach(r=>{if(!(r.group in byGroup)){byGroup[r.group]=[];order.push(r.group);}byGroup[r.group].push(r);});
  return order.map(g=>{
    const items=byGroup[g].map(r=>{
      if(r.type==='bool'){
        const on=!!r.value, def=r.is_set?'':' <span class="dim">(default)</span>';
        return `<div class="cc-k">${esc(r.label)}${def}</div><div class="cc-v">`+
          `<button class="cc-tgl set-tgl${on?' on':''}${ctlPending.has('set:'+r.key)?' cmd-pending':''}" onclick="settingsWrite('${r.key}',${on?0:1})">${on?'ON':'OFF'}</button></div>`;
      }
      const v=r.value?String(r.value):'', base=v?v.split('/').pop():'\\u2014';
      return `<div class="cc-k">${esc(r.label)}</div><div class="cc-v"><span title="${esc(v)}">${esc(base)}</span></div>`;
    }).join('');
    return `<div class="cc-sec"><div class="cc-sech">${esc(g)}</div><div class="cc-grid">${items}</div></div>`;
  }).join('');
}
function closeControl(){const cc=document.getElementById('cc');cc.style.display='none';ctlSerial=null;if(ctlPoll){clearTimeout(ctlPoll);ctlPoll=null;}}
// ── Row action floating menus ───────────────────────────────────────────────
let _menuAnchor=null;
function openMenu(ev,html){
  ev.stopPropagation();
  _menuAnchor=ev.currentTarget.getBoundingClientRect();
  document.getElementById('menu').innerHTML=html;
  placeMenu();
}
// Position the menu against its anchor, flipping above/below and clamping to
// the viewport. Kept separate from openMenu so async content (the sparkline,
// which loads after the box opens) can re-place once its real size is known.
function placeMenu(){
  const m=document.getElementById('menu'); if(!_menuAnchor)return;
  m.style.left='-9999px'; m.style.top='0px'; m.style.display='block';
  const r=_menuAnchor, mw=m.offsetWidth, mh=m.offsetHeight;
  let left=r.left, top=r.bottom+3;
  if(left+mw>window.innerWidth-8)left=window.innerWidth-8-mw;
  if(top+mh>window.innerHeight-8)top=Math.max(8,r.top-mh-3);
  m.style.left=Math.max(8,left)+'px'; m.style.top=top+'px';
}
function closeMenu(){document.getElementById('menu').style.display='none';}
// ── Transparent-screen cutout detection ─────────────────────────────────────
// A product PNG whose screen is cut to transparent alpha lets us composite the
// live screenshot behind it (bezel + hands occlude). We only need the bounding
// box of the ENCLOSED transparent region: flood-fill transparency inward from
// the border (that is the render's transparent background) and take whatever
// transparency is left — the screen hole. Robust to the hole being split by
// opaque foreground (narwhal's hands): we union all interior-transparent px.
function holeBoxFromAlpha(alpha,w,h,thr){
  thr=thr||128;
  const isT=i=>alpha[i]<thr;
  const bg=new Uint8Array(w*h), stack=[];
  const seed=(x,y)=>{if(x<0||x>=w||y<0||y>=h)return;const i=y*w+x;if(!bg[i]&&isT(i)){bg[i]=1;stack.push(i);}};
  for(let x=0;x<w;x++){seed(x,0);seed(x,h-1);}
  for(let y=0;y<h;y++){seed(0,y);seed(w-1,y);}
  while(stack.length){const i=stack.pop(),x=i%w,y=(i/w)|0;seed(x-1,y);seed(x+1,y);seed(x,y-1);seed(x,y+1);}
  let x0=w,y0=h,x1=-1,y1=-1;
  for(let y=0;y<h;y++)for(let x=0;x<w;x++){const i=y*w+x;if(isT(i)&&!bg[i]){if(x<x0)x0=x;if(x>x1)x1=x;if(y<y0)y0=y;if(y>y1)y1=y;}}
  if(x1<0)return null;
  return {x:x0/w,y:y0/h,w:(x1-x0+1)/w,h:(y1-y0+1)/h};
}
function detectHole(img){
  const w=img.naturalWidth,h=img.naturalHeight;
  if(!w||!h)return null;
  const c=document.createElement('canvas');c.width=w;c.height=h;
  const ctx=c.getContext('2d');ctx.drawImage(img,0,0);
  let d;try{d=ctx.getImageData(0,0,w,h).data;}catch(e){return null;}  // taint guard (same-origin, shouldn't fire)
  const a=new Uint8Array(w*h);
  for(let i=0;i<w*h;i++)a[i]=d[i*4+3];
  return holeBoxFromAlpha(a,w,h);
}
const _holeCache={};
function holeFor(codename,img){
  if(codename in _holeCache)return _holeCache[codename];
  return (_holeCache[codename]=detectHole(img));
}
function openWatchImg(codename,serial,ev,isRound,res){
  if(ev){ev.stopPropagation();wimgAX=ev.clientX;wimgAY=ev.clientY;}
  _wimgMoved=false;   // a fresh open re-anchors to the click, like the Control Center
  // Load the product photo in a device frame; onProdLoad then decides the
  // layout once we can inspect the image for a transparent screen cutout.
  const o=document.getElementById('wimg');
  o.innerHTML=
    `<div class="wimg-hd" onmousedown="wimgDragStart(event)"><span>${esc(codename)}</span><span class="wimg-x" onclick="closeWatchImg()">&times;</span></div>`+
    `<div class="wimg-body" id="wimg-body">`+
      `<div class="device" id="device"><div class="dev-frame" id="devframe">`+
        `<img class="dev-prod" id="prodimg" alt="" onerror="closeWatchImg()" `+
          `onload="onProdLoad('${esc(codename)}','${esc(serial||'')}',${isRound?1:0},'${res?esc(res):''}')" `+
          `src="/api/watch-image/${encodeURIComponent(codename)}"></div></div>`+
    `</div>`+
    `<div class="wimg-ctl" id="wimghands"></div>`;
  o.style.display='block';
  wimgPlace();
}
function onProdLoad(codename,serial,isRound,res){
  const prod=document.getElementById('prodimg'); if(!prod)return;
  const dev=document.getElementById('device'), frame=document.getElementById('devframe');
  const box=holeFor(codename,prod);
  if(box){
    // Cutout present → composite: the product's transparent screen reveals the
    // screenshot behind it (bezel + hands occlude); a black fill under that so
    // an off / not-yet-loaded screen reads as an off panel. Positions are % of
    // the frame, which is exactly the image (caption lives outside it).
    dev.classList.add('cut');
    // Remember what sizeComposite() needs so it can re-fit on window resize.
    const nw=parseInt((res||'').split('x')[0],10);
    _compo=(nw&&box.w>0)?{boxW:box.w,target:nw*2/3,aspect:prod.naturalWidth/prod.naturalHeight}:null;
    const pct=v=>(v*100).toFixed(3)+'%';
    // Round screens: clip the fill+screenshot to a circle so the square hole
    // bounding box can't shine black corners past the bezel.
    const clip=isRound?';border-radius:50%':'';
    const css=`left:${pct(box.x)};top:${pct(box.y)};width:${pct(box.w)};height:${pct(box.h)}${clip}`;
    const fill=document.createElement('div'); fill.className='dev-fill'; fill.style.cssText=css;
    frame.insertBefore(fill,prod);
    if(serial){
      const shot=document.createElement('img'); shot.className='dev-shot'; shot.id='shotimg';
      shot.style.cssText=css; frame.insertBefore(shot,prod);
      // Physical-hands overlay (narwhal): angled rectangles over the screen,
      // revealed through the product's transparent face like the screenshot.
      const hands=document.createElement('div'); hands.className='dev-hands'; hands.id='devhands';
      hands.style.cssText=css; frame.insertBefore(hands,prod);
      loadHands(serial,codename);
    }
    const cap=document.createElement('div'); cap.className='wimg-cap'; cap.id='shotcap';
    cap.textContent=serial?'loading…':('screen off'+(res?' · '+res:''));
    dev.appendChild(cap);
    if(serial)loadShot(serial,res);
    sizeComposite();
  }else{
    // No cutout yet → product beside a shape-masked screenshot (prior look).
    const cap=document.createElement('div'); cap.className='wimg-cap'; cap.textContent='product';
    dev.appendChild(cap);
    if(serial){
      const sb=document.createElement('div'); sb.id='shotbox';
      sb.innerHTML=`<img class="wimg-shot ${isRound?'shape-round':'shape-rect'}" id="shotimg" alt="" onload="wimgPlace()"><div class="wimg-cap" id="shotcap">loading&hellip;</div>`;
      document.getElementById('wimg-body').appendChild(sb);
      loadShot(serial,res);
    }
  }
  wimgPlace();
}
// narwhal (hands watch): draw where the physical hands point AND offer a control
// to move them. `position` is two values tracking roughly hour:minute (value%60*6
// ≈ degrees — a first-cut mapping to confirm with dodo). The movement is set by
// writing a datetime to /sys/devices/sop716/time (dodoradio's hands-timesync
// convention), so Sync-to-now corrects drift and the dial poses a time. All a
// silent no-op on a watch with no movement.
// ── narwhal hands: mode-based control (Time / Free / Calibrate) ──────────────
// The hands are driven by motor_move_all "minute:hour", each 0..179 = an absolute
// angle at 180 steps/turn (2 deg/step). A hand-drag is ambiguous, so a MODE says
// what it means: Time = read-only clock; Free = drag drives the real hand; the
// motor-zero offset (cal) maps a clock angle to a motor value. Hand art 80% of
// the frame (mo eyeballed 100% as ~20% too big).
const HANDS_SIZE=80;
let _handsSerial=null, _handsCodename=null, _handsDevEl=null, _handsDrag=null;
let handsMode='time';
let handsCal={min_deg:102,hr_deg:108};   // physical degrees at motor value 0, per hand
let handsVal={min:0,hr:0};               // current motor values (0..179)
let handsMatch={min:0,hr:0};             // Calibrate: web angles the user drags onto the real hands
// motor value (0..179, 2 deg/step) <-> clock angle (deg, 12 o'clock = 0)
function valToAngle(v,off){return ((off+v*2)%360+360)%360;}
function angleToVal(a,off){return ((Math.round(((((a-off)%360)+360)%360)/2))%180+180)%180;}
function handsTimeVals(){
  const t=new Date();
  const minA=(t.getMinutes()+t.getSeconds()/60)*6;
  const hrA=((t.getHours()%12)+t.getMinutes()/60)*30;
  return {min:angleToVal(minA,handsCal.min_deg), hr:angleToVal(hrA,handsCal.hr_deg)};
}
function loadHands(serial,codename){
  _handsSerial=serial; if(codename)_handsCodename=codename;
  fetch('/api/watch/'+encodeURIComponent(serial)+'/hands').then(r=>r.json()).then(d=>{
    const hd=d&&d.hands; if(!hd)return;
    if(d.cal)handsCal=d.cal;
    const cn=_handsCodename, frame=document.getElementById('devframe');
    const prod=document.getElementById('prodimg'), el=document.getElementById('devhands');
    if(frame&&prod&&el&&cn){
      // A hands watch: opaque hands-removed base, the real hour/minute art ON TOP,
      // centred and rotated about centre. Drop the screenshot-through-hole layers.
      const shot=document.getElementById('shotimg'); if(shot)shot.remove();
      const fill=frame.querySelector('.dev-fill'); if(fill)fill.remove();
      prod.onload=null;   // swapping src must not re-run the hole composite
      prod.src='/api/watch-hand/'+encodeURIComponent(cn)+'/base';
      frame.appendChild(el);   // lift the hands layer above the product image
      _handsDevEl=el;
      if(handsMode==='time'&&!_handsDrag)handsVal=handsTimeVals();
      _renderHands();
    }
    _renderHandsPanel(hd.position);
  }).catch(()=>{});
}
function _handsDispAngle(which){
  // Calibrate shows where the user is matching (handsMatch); Time/Free show the
  // current motor value mapped through the offset.
  if(handsMode==='calibrate')return handsMatch[which];
  return valToAngle(handsVal[which], which==='hr'?handsCal.hr_deg:handsCal.min_deg);
}
function _renderHands(){
  const el=_handsDevEl, cn=_handsCodename; if(!el||!cn)return;
  const drag=handsMode==='free'||handsMode==='calibrate';
  el.style.cssText='position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;z-index:2';
  const hand=(part,which)=>`<img class="hand-svg" alt="" draggable="false" onerror="this.remove()" `+
    `src="/api/watch-hand/${encodeURIComponent(cn)}/${part}" onpointerdown="handsDown(event,'${which}')" `+
    `style="position:absolute;left:50%;top:50%;width:${HANDS_SIZE}%;height:${HANDS_SIZE}%;`+
    `transform:translate(-50%,-50%) rotate(${_handsDispAngle(which).toFixed(1)}deg);transform-origin:center center;`+
    `pointer-events:${drag?'auto':'none'};cursor:${drag?'grab':'default'};touch-action:none">`;
  el.innerHTML=hand('hour','hr')+hand('minute','min');
}
function handsDown(ev,which){
  if(handsMode!=='free'&&handsMode!=='calibrate')return;
  ev.preventDefault(); ev.stopPropagation();
  _handsDrag=which;
  document.addEventListener('pointermove',handsMoveDrag);
  document.addEventListener('pointerup',handsUpDrag);
}
function _handsAngleAt(ev){
  const f=document.getElementById('devframe'); if(!f)return null;
  const r=f.getBoundingClientRect(), cx=r.left+r.width/2, cy=r.top+r.height/2;
  return (Math.atan2(ev.clientY-cy,ev.clientX-cx)*180/Math.PI+90+360)%360;   // 12 o'clock = 0
}
function handsMoveDrag(ev){
  if(!_handsDrag)return;
  const a=_handsAngleAt(ev); if(a===null)return;
  if(handsMode==='calibrate'){
    handsMatch[_handsDrag]=(Math.round(a/2)*2)%360;   // web-only: match the real hand (snap 2 deg)
  }else{
    const off=_handsDrag==='hr'?handsCal.hr_deg:handsCal.min_deg;
    handsVal[_handsDrag]=angleToVal(a,off);           // Free: drive the motor (snap 2 deg)
  }
  _renderHands();
}
function handsUpDrag(){
  document.removeEventListener('pointermove',handsMoveDrag);
  document.removeEventListener('pointerup',handsUpDrag);
  const wasFree=handsMode==='free';
  if(_handsDrag){_handsDrag=null; if(wasFree)handsCommit();}   // Calibrate drag never moves the motor
}
function handsCommit(){
  if(!_handsSerial)return;
  fetch('/api/watch/'+encodeURIComponent(_handsSerial)+'/hands-move/'+handsVal.min+'/'+handsVal.hr,{method:'POST'})
    .then(r=>r.json()).then(d=>{if(!d||!d.ok)toast('hands move failed'+(d&&d.error?' - '+d.error:''));})
    .catch(()=>toast('hands move failed'));
}
function handsSetMode(m){
  handsMode=m;
  if(m==='time'&&!_handsDrag)handsVal=handsTimeVals();
  else if(m==='calibrate')handsCalRef();
  _renderHands(); _renderHandsPanel();
}
function handsToTime(){handsVal=handsTimeVals(); _renderHands(); handsCommit(); toast('hands set to current time');}
// Calibrate: command a known spread reference (minute 0, hour 90) so the drag can
// teach the per-hand offset. The web hands start at the ASSUMED position; the user
// drags/nudges them onto the real hands, then Match learns the offset.
function handsCalRef(){
  handsVal={min:0,hr:90};
  handsMatch={min:valToAngle(0,handsCal.min_deg), hr:valToAngle(90,handsCal.hr_deg)};
  handsCommit();
}
function handsCalNudge(which,delta){
  handsMatch[which]=((handsMatch[which]+delta)%360+360)%360;   // web-only fine adjust
  _renderHands();
}
function handsCalSave(){
  // We commanded minute=0, hour=90; the user matched the web to the physical
  // hands, so physical_angle = offset + value*2  ->  offset = matched - value*2.
  const minOff=((handsMatch.min-0)%360+360)%360;
  const hrOff=((handsMatch.hr-180)%360+360)%360;
  fetch('/api/watch/'+encodeURIComponent(_handsSerial)+'/hands-cal/'+minOff.toFixed(1)+'/'+hrOff.toFixed(1),{method:'POST'})
    .then(r=>r.json()).then(d=>{
      if(d&&d.ok){handsCal=d.cal; toast('hands calibrated'); handsSetMode('time');}
      else toast('calibrate save failed');
    }).catch(()=>toast('calibrate save failed'));
}
// Choreography — presets in the calibrated space; each drives motor_move_all.
function handsGoto(minA,hrA){
  handsVal={min:angleToVal(minA,handsCal.min_deg), hr:angleToVal(hrA,handsCal.hr_deg)};
  _renderHands(); handsCommit();
}
function handsOverlap(){handsGoto(0,0); toast('hands overlapped at 12');}
function handsOppose(){handsGoto(0,180); toast('hands opposed (12-6 line)');}
function handsPark(){handsGoto(180,180); toast('hands parked at 6');}
function handsSpin(){
  let step=0; const start={min:handsVal.min,hr:handsVal.hr};
  const iv=setInterval(()=>{
    step++;
    handsVal={min:(start.min+step*30)%180, hr:(start.hr+step*30)%180};
    _renderHands(); handsCommit();
    if(step>=6)clearInterval(iv);   // 6 x 30 = 180 steps = one full turn, back to start
  },500);
}
function _renderHandsPanel(position){
  const box=document.getElementById('wimghands'); if(!box)return;
  const tab=(m,l,t)=>`<button class="hmode${handsMode===m?' on':''}" title="${t}" onclick="handsSetMode('${m}')">${l}</button>`;
  const cur=position!=null?`<span class="dim mono" title="driver step counter (raw; re-synced on any move)">${esc(position)}</span>`:'';
  const act=(fn,l,t)=>`<button class="cc-act mini" onclick="${fn}" title="${t}">${l}</button>`;
  let body='';
  if(handsMode==='time')
    body=`<span class="dim">the web mirrors the clock</span>`+act('handsToTime()','Set watch to time','drive the physical hands to the current time');
  else if(handsMode==='free')
    body=`<span class="dim">grab a hand and rotate</span>`+
      `<span class="hchoreo">`+act('handsOverlap()','Overlap','both hands to 12')+
      act('handsOppose()','Oppose','a straight 12-6 line')+
      act('handsPark()','Park','both hands to 6, clearing the top of the dial')+
      act('handsSpin()','Spin','both hands sweep a full turn')+`</span>`;
  else
    body=`<span class="dim">drag each web hand onto your watch&rsquo;s real hand, then Match</span>`+
      `<span class="hcal">min<button class="spin-b" onclick="handsCalNudge('min',-2)">&minus;</button>`+
      `<button class="spin-b" onclick="handsCalNudge('min',2)">+</button> `+
      `hr<button class="spin-b" onclick="handsCalNudge('hr',-2)">&minus;</button>`+
      `<button class="spin-b" onclick="handsCalNudge('hr',2)">+</button>`+
      act('handsCalSave()','Match','learn the motor offset from your match')+`</span>`;
  box.innerHTML=`<div class="wimg-ctl-r hmodes">${tab('time','Time','read-only: the web shows the clock')}`+
    `${tab('free','Free','grab and rotate a hand to move the real hand')}`+
    `${tab('calibrate','Calibrate','teach the web where the real hands are')}${cur}</div>`+
    `<div class="wimg-ctl-r">${body}</div>`;
}
function wimgPlace(){
  // Anchor to the click and flip above if it would run off the bottom, like
  // the Control Center — images load async, so this is called again on each
  // image's onload once the real panel size is known.
  const o=document.getElementById('wimg');
  if(o.style.display!=='block'||_wimgMoved)return;   // don't re-anchor a user-dragged window
  const h=o.offsetHeight, w=o.offsetWidth;
  let left=Math.min(wimgAX, window.innerWidth-w-8);
  let top=wimgAY+10;
  if(top+h>window.innerHeight-8) top=wimgAY-h-10;
  o.style.left=Math.max(8,left)+'px'; o.style.top=Math.max(8,top)+'px';
}
// Drag the live view by its title bar, like the Control Center — handy for
// calibration (park the window beside the physical watch). Sets _wimgMoved so
// wimgPlace() stops re-anchoring it on image loads.
function wimgDragStart(e){
  if(e.target.classList&&e.target.classList.contains('wimg-x'))return;   // not the close X
  const o=document.getElementById('wimg'), r=o.getBoundingClientRect();
  _wimgDrag={dx:e.clientX-r.left, dy:e.clientY-r.top}; _wimgMoved=true; e.preventDefault();
}
function loadShot(serial,res){
  const suffix=res?' · '+res:'';
  fetch('/api/watch/'+encodeURIComponent(serial)+'/screenshot.jpg?t='+Date.now())
    .then(r=>{if(!r.ok)throw 0;const st=r.headers.get('X-Screenshot-Stale');
      const ts=+r.headers.get('X-Screenshot-Ts')||0;
      return r.blob().then(b=>({b,st,ts}));})
    .then(({b,st,ts})=>{const img=document.getElementById('shotimg'),
      cap=document.getElementById('shotcap'); if(!img)return;
      img.onload=wimgPlace; img.src=URL.createObjectURL(b);
      if(st){img.classList.add('shot-stale');cap.className='wimg-cap warn';
        cap.textContent='stale screen'+(ts?' · '+fmtAge(ts)+' ago':'')+suffix;}
      else{cap.textContent='live screen'+suffix;}})
    .catch(()=>{
      const box=document.getElementById('shotbox');
      if(box){box.remove();return;}                      // side-by-side: drop the box
      const s=document.getElementById('shotimg');if(s)s.remove();   // composite: keep black fill
      const c=document.getElementById('shotcap');if(c){c.className='wimg-cap';c.textContent='screen off';}
    });
}
function closeWatchImg(){document.getElementById('wimg').style.display='none';_compo=null;_handsDrag=null;_wimgDrag=null;_handsDevEl=null;handsMode='time';}
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeWatchImg();closeControl();closeMenu();closeRegistry();closeBt();}});
// ── Fleet Registry: every watch ever seen, with a Log of what changed ────────
function openRegistry(){
  const p=document.getElementById('reg'),m=document.getElementById('regmask');
  if(!p)return;
  p.style.display='block';m.style.display='block';
  p.innerHTML='<div class="reg-hd"><b>Fleet Registry</b><span class="dim">loading&hellip;</span><a href="#" class="reg-x" onclick="closeRegistry();return false">&times;</a></div>';
  fetch('/api/registry').then(r=>r.json()).then(renderRegistry).catch(()=>{
    p.querySelector('.dim').textContent='could not load';});
}
function closeRegistry(){
  const p=document.getElementById('reg'),m=document.getElementById('regmask');
  if(p)p.style.display='none';if(m)m.style.display='none';
}
let _regData=null,_regOpen={};   // cached payload; serial -> expanded?
function renderRegistry(d){
  const p=document.getElementById('reg');if(!p)return;
  if(d)_regData=d;
  const ws=(_regData&&_regData.watches)||[];
  const F=(r,k)=>(r.fields&&r.fields[k]!=null)?r.fields[k]:null;
  let rows='';
  ws.forEach(r=>{
    const cn=F(r,'codename')||'<span class="dim">unknown</span>';
    const src=r.last_source||'?';
    const seen=r.last_seen?fmtAge(r.last_seen)+' ago':'';
    const kv=[F(r,'kernel')?'kernel '+esc(F(r,'kernel')):'',F(r,'qt')?'Qt '+esc(F(r,'qt')):''].filter(Boolean).join(' &middot; ');
    const nlog=(r.log||[]).length;
    const open=!!_regOpen[r.serial];
    const chevron=nlog?`<span class="reg-chev">${open?'&#9660;':'&#9654;'}</span>`:'';
    rows+=`<tr class="reg-row${nlog?' has-log':''}"${nlog?` onclick="toggleRegLog('${esc(r.serial)}')"`:''}>`+
      `<td>${chevron}<b class="cn">${typeof cn==='string'&&cn[0]!=='<'?esc(cn):cn}</b></td>`+
      `<td class="dim mono">${esc(r.serial)}</td>`+
      `<td><span class="cbadge ${src==='orbit'?'wifi':(src==='ssh'?'ssh':'adb')}">${esc(src)}</span> <span class="dim">${seen}</span></td>`+
      `<td class="dim">${kv||'&mdash;'}</td>`+
      `<td class="dim">${nlog?nlog+' change'+(nlog>1?'s':''):'&mdash;'}</td></tr>`;
    if(open&&nlog){
      const entries=(r.log||[]).slice().reverse().map(e=>{
        const ch=Object.entries(e.changes||{}).map(([k,v])=>`${esc(k)}: <span class="dim">${esc(String(v[0]))}</span> &rarr; ${esc(String(v[1]))}`).join('<br>');
        return `<div class="reg-le"><span class="reg-when" title="${new Date(e.ts*1000).toLocaleString()}">${fmtAge(e.ts)} ago</span><span class="dim">${esc(e.source||'')}</span><div>${ch}</div></div>`;
      }).join('');
      rows+=`<tr class="reg-logrow"><td colspan="5"><div class="reg-log">${entries}</div></td></tr>`;
    }
  });
  p.innerHTML=
    `<div class="reg-hd"><b>Fleet Registry</b><span class="dim">${ws.length} hull${ws.length===1?'':'s'} on record</span>`+
    `<a href="#" class="reg-x" onclick="closeRegistry();return false">&times;</a></div>`+
    `<div class="reg-body">`+(ws.length
      ?`<table class="reg-t"><thead><tr><th>codename</th><th>serial</th><th>last seen</th><th>latest</th><th>log</th></tr></thead><tbody>${rows}</tbody></table>`
      :'<p class="dim" style="padding:16px">No watches on record yet — connect or launch one and it appears here.</p>')+
    `</div>`;
}
function toggleRegLog(serial){_regOpen[serial]=!_regOpen[serial];renderRegistry();}
// ── Bluetooth scan + pair (Orbit port, rung 1-2) ─────────────────────────────
let _btData=null;
function openBtScan(){
  const p=document.getElementById('bt'),m=document.getElementById('btmask');
  if(!p)return; p.style.display='block'; m.style.display='block';
  if(_btData)renderBt(); else btScan();
}
function closeBt(){const p=document.getElementById('bt'),m=document.getElementById('btmask');if(p)p.style.display='none';if(m)m.style.display='none';}
function btScan(){
  const p=document.getElementById('bt'); if(!p)return;
  p.innerHTML='<div class="reg-hd"><b>&#x1F50D; Bluetooth scan</b><span class="dim">scanning 8s&hellip;</span><a href="#" class="reg-x" onclick="closeBt();return false">&times;</a></div>';
  fetch('/api/bt/scan/8',{method:'POST'}).then(r=>r.json()).then(d=>{_btData=d;renderBt();})
    .catch(()=>{const s=p.querySelector('.dim');if(s)s.textContent='scan failed';});
}
function renderBt(){
  const p=document.getElementById('bt'); if(!p)return;
  const ds=(_btData&&_btData.devices)||[];
  const rows=ds.map(d=>{
    const nm=d.in_fleet?`<b class="cn">${esc(d.codename||d.name)}</b>`:`<span class="dim">${esc(d.name||d.mac)}</span>`;
    const badge=d.in_fleet?' <span class="cbadge wifi">fleet</span>':'';
    const act=d.paired?'<span class="cbadge adb">paired</span>'
      :`<button class="btn" onclick="btPair('${esc(d.mac)}','${esc(d.codename||d.name||d.mac)}')">Pair</button>`;
    return `<tr class="reg-row"><td>${nm}${badge}</td><td class="dim mono">${esc(d.mac)}</td><td class="dim">${d.rssi!=null?d.rssi+' dBm':''}</td><td>${act}</td></tr>`;
  }).join('');
  const fleet=ds.filter(d=>d.in_fleet).length;
  p.innerHTML=`<div class="reg-hd"><b>&#x1F50D; Bluetooth scan</b><span class="dim">${ds.length} found &middot; ${fleet} in fleet</span>`+
    `<a href="#" onclick="btScan();return false" style="margin-left:auto;color:#8b949e;text-decoration:none;font-size:12px" title="scan again">&#x21bb; rescan</a>`+
    `<a href="#" class="reg-x" onclick="closeBt();return false">&times;</a></div>`+
    `<div class="reg-body"><table class="reg-t"><thead><tr><th>device</th><th>mac</th><th>rssi</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
function btPair(mac,name){
  toast('pairing '+name+' — confirm on the watch');
  const p=document.getElementById('bt'),hd=p&&p.querySelector('.reg-hd .dim');
  if(hd)hd.textContent='pairing '+name+' — confirm on the watch…';
  fetch('/api/bt/pair/'+encodeURIComponent(mac),{method:'POST'}).then(r=>r.json()).then(d=>{
    toast(d&&d.ok?('paired '+name):('pair failed'+(d&&d.error?': '+d.error:'')));
    _btData=null; btScan();
  }).catch(()=>toast('pair failed'));
}
function mi(cls,label,fn,dis,title){return `<button class="menu-item ${cls}"${dis?` disabled title="${title||'not available yet'}"`:` onclick="${fn};closeMenu()"`}>${label}</button>`;}
// The row's actions fold into one Execute menu: each former button becomes a
// group header, its items listed indented beneath, all visible at once (no
// nested submenus). Each group is a content-builder returning just its items;
// menuExecute composes them under headers. grpHd is a header, grpBox indents a
// group's items beneath it.
function grpHd(label){return `<div class="exgrp-hd">${label}</div>`;}
function grpBox(items){return `<div class="exgrp">${items}</div>`;}
function grpPower(slot,charging,draining,powered,noSw){
  return (charging?mi('ch','Stop charge',`doStopCharge('${slot}')`):mi('ch','Charge',`doCharge('${slot}')`,noSw))+
    (draining?mi('dr','Stop drain test',`doStopDrain('${slot}')`):mi('dr','Drain test',`doDrain('${slot}')`,noSw))+
    '<div class="menu-sep"></div>'+
    (powered?mi('po','Power off',`doPoweroff('${slot}')`):'')+
    mi('rb','Reboot',`doReboot('${slot}')`)+
    mi('bl','Bootloader',`doBootloader('${slot}')`);
}
// A watch in the bootloader used to get no Power menu at all — a dead end in
// the UI exactly where the watch needs steering. The same intents apply, they
// just travel over fastboot; charge and drain are omitted because both need
// battery reads the bootloader does not serve.
function grpPowerFb(slot,powered){
  return '<div class="menu-hd">in bootloader — fastboot actions</div>'+
    mi('rb','Continue boot',`doContinue('${slot}')`)+
    mi('rb','Reboot',`doReboot('${slot}')`)+
    '<div class="menu-sep"></div>'+
    mi('bl','Cycle bootloader',`doBootloader('${slot}')`)+
    mi('bl','Recovery',`doRecovery('${slot}')`)+
    // Deliberately disabled, not hidden: the watch CAN power off from the
    // bootloader, just not over the wire. rover and rubyfish have no `oem
    // poweroff` command at all, and cutting VBUS does not stop a fastboot
    // watch — it keeps running on battery until flat (measured). The
    // on-screen menu item works because a key press calls LK's shutdown
    // directly. Showing it greyed with the manual route is honest; hiding it
    // would imply the watch cannot be powered off, which is false.
    (powered?'<div class="menu-sep"></div>'+mi('po','Power off',null,true,
      'unavailable — select and confirm "Power off" in the fastboot on-screen menu'):'');
}
function grpWorkbench(slot,serial,wb,mode,sshIp){
  const online=mode==='device';
  // (The USB IP used to be shown here as a banner; it now lives in the
  // Connection column's Network Center, which is a better place to find it.)
  // USB-mode toggle. Workbench work happens over WiFi/SSH, so switching the
  // watch's USB gadget between adb and SSH/developer mode belongs here. The
  // item flips with the current mode: on adb it offers SSH (delivered over
  // adb, needs the watch online); in SSH mode it offers ADB (delivered over
  // the watch's rndis link, which is up precisely because it's in SSH mode).
  // Either switch re-enumerates the gadget and drops the current link.
  let usbToggle;
  if(mode==='ssh')
    usbToggle=mi('info','Switch USB to ADB',`switchAdb('${serial}')`);
  else
    usbToggle=mi('info','Switch USB to SSH',`switchSsh('${serial}')`,!online,
                 'watch must be on ADB to switch it to SSH mode');
  return '<div class="menu-hd">watch stays on — power off when done</div>'+
    (wb?mi('wbx','End checkout',`doStopWb('${slot}')`):mi('wbx','Checkout (hold band)',`doWb('${slot}')`))+
    '<div class="menu-sep"></div>'+
    usbToggle+
    '<div class="menu-sep"></div>'+
    mi('info','Set time from host',`doSetTime('${serial}')`,!online)+
    mi('info','Screenshot',`doScreenshot('${serial}')`,!online)+
    mi('info','Test notification',`doNotify('${serial}')`,!online)+
    mi('info','Collect diagnostics',`doDiag('${slot}')`,!online);
}
function grpFlash(slot){
  return mi('','Backup data',`doBackup('${slot}')`)+
    mi('','Restore data',`doRestore('${slot}')`)+
    mi('info','Fastboot report',`doFbReport('${slot}')`)+
    '<div class="menu-sep"></div>'+
    mi('','Flash nightly',`doFl('${slot}')`)+
    mi('',"Flash 2.1",`doFlV('${slot}','2.1')`)+
    mi('',"Flash 2.0",`doFlV('${slot}','2.0')`)+
    '<div class="menu-sep"></div>'+
    mi('','Dump mmcblk0',`doDump('${slot}')`,true,'not yet implemented')+
    mi('','Restore from dump',`doRestoreDump('${slot}')`,true,'not yet implemented');
}
function menuExecute(ev,slot,isFb,charging,draining,powered,noSw,serial,wb,mode,sshIp,wear,needPwr){
  openMenu(ev,
    (!isFb&&serial?grpHd('Wear')+grpBox(wearItem(slot,wear)):'')+
    grpHd('Power')+grpBox(isFb?grpPowerFb(slot,powered):grpPower(slot,charging,draining,powered,noSw))+
    grpHd('Flashing')+grpBox(grpFlash(slot))+
    (!isFb?grpHd('Workbench')+grpBox(grpWorkbench(slot,serial,wb,mode,sshIp)):'')+
    grpHd('Refresh')+grpBox(mi('','Re-identify / power on',`doRefresh('${slot}',${needPwr})`)));
}
// Wear is the one menu item that stays a button — pink, the deliberate off-rig
// action, distinct from the plain text links around it.
function wearItem(slot,wear){
  return `<button class="menu-wear${wear?' on':''}" onclick="pulseSelf(this);doWear('${slot}',${wear?0:1});closeMenu()" title="${wear?'wear armed — click to release and free the port':'top up and hold this port so the watch is ready to take off the rig'}">${wear?'Release wear':'Arm wear (hold band)'}</button>`;
}
// Contextual mini-menus reachable from the Stats dots — the same builders as
// the full row menu, scoped to what each dot is about. The power dot opens just
// the Power group; the wearability dot opens Drain test + a Wear button.
function menuPwr(ev,slot,isFb,charging,draining,powered,noSw){
  openMenu(ev,grpHd('Power')+grpBox(isFb?grpPowerFb(slot,powered):grpPower(slot,charging,draining,powered,noSw)));
}
function menuWear(ev,slot,draining,serial,wear){
  openMenu(ev,
    grpHd('Drain test')+grpBox(draining?mi('dr','Stop drain test',`doStopDrain('${slot}')`):mi('dr','Drain test',`doDrain('${slot}')`))+
    (serial?grpHd('Wear')+grpBox(wearItem(slot,wear)):''));
}
function toast(msg){
  // Created on first use — every menu action toasts, and a missing element
  // here threw and silently killed the action itself (screenshot bug).
  let t=document.getElementById('toast');
  if(!t){t=document.createElement('div');t.id='toast';document.body.appendChild(t);}
  t.textContent=msg;t.classList.add('show');
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2400);
}
function doSetTime(s){toast('syncing time…');fetch('/api/watch/'+encodeURIComponent(s)+'/settime',{method:'POST'}).then(()=>toast('time synced from host'));}
function doNotify(s){fetch('/api/watch/'+encodeURIComponent(s)+'/notify',{method:'POST'}).then(r=>r.json()).then(d=>toast(d.ok?'notification sent to watch':'notify failed'));}
function doScreenshot(s){toast('capturing…');window.open('/api/watch/'+encodeURIComponent(s)+'/screenshot.jpg?t='+Date.now(),'_blank');}
function doFlV(s,v){if(!confirm('Flash AsteroidOS '+v+' to this watch?\\nThis wipes its data — back up first if you need it.'))return;doFl(s,v);}
function switchAdb(serial){toast('switching to ADB…');fetch('/api/switch-adb'+(serial?'/'+encodeURIComponent(serial):''),{method:'POST'}).then(r=>r.json()).then(d=>{toast(d.ok?'switching — watch re-enumerating on ADB…':('Switch to ADB failed — '+(d.error||'unknown')));if(d.ok){ctlSet(serial,'adb',null);setTimeout(refresh,5000);}else flashFail(connPill(serial))});}
function switchSsh(serial){toast('switching to SSH…');fetch('/api/switch-ssh/'+encodeURIComponent(serial),{method:'POST'}).then(r=>r.json()).then(d=>{toast(d.ok?'switching — watch re-enumerating as SSH…':('Switch to SSH failed — '+(d.error||'unknown')));if(d.ok){ctlSet(serial,'ssh',d.ip);setTimeout(refresh,6000);}else flashFail(connPill(serial))});}
// Keep an open Network tab in sync with a USB-mode switch made from it: the
// mode and assigned IP change immediately, before the watch re-enumerates.
function ctlSet(serial,mode,ip){
  if(ctlSerial!==serial)return;
  ctlMode=mode; if(ip)ctlSshIp=ip;
  if(ctlTab==='net'&&ctlCache[serial])renderControl(ctlCache[serial]);
}
function doDiag(c){toast('collecting diagnostics…');fetch('/api/diagnostics/'+_api(c),{method:'POST'}).then(r=>r.json()).then(d=>{
  if(d.name){
    toast(d.ok?'diagnostics ready — downloading':'diagnostics partial — downloading what we have');
    const a=document.createElement('a');a.href='/api/diagnostics/download/'+encodeURIComponent(d.name);
    a.download=d.name;document.body.appendChild(a);a.click();a.remove();
  }else{toast(d.error||'diagnostics failed');}
}).catch(()=>toast('diagnostics failed'));}
function doFbReport(c){toast('reading bootloader…');fetch('/api/fbreport/'+_api(c),{method:'POST'}).then(r=>r.json()).then(d=>{
  if(d.name){
    toast('fastboot report ('+d.lines+' lines) — downloading');
    const a=document.createElement('a');a.href='/api/diagnostics/download/'+encodeURIComponent(d.name);
    a.download=d.name;document.body.appendChild(a);a.click();a.remove();
  }else{toast(d.error||'fastboot report failed');}
}).catch(()=>toast('fastboot report failed'));}
function doBackup(c){toast('backing up…');fetch('/api/backup/'+_api(c),{method:'POST'}).then(r=>r.json()).then(d=>toast(d.ok?'backup saved':'backup incomplete — see log')).catch(()=>toast('backup failed'));}
function doRestore(c){if(!confirm('Restore backed-up data onto this watch?\\nOverwrites its current settings + WiFi credentials with the last backup.'))return;toast('restoring…');fetch('/api/restore/'+_api(c),{method:'POST'}).then(r=>r.json()).then(d=>toast(d.ok?'restore done — reconnecting WiFi':(d.error||'restore incomplete — see log'))).catch(()=>toast('restore failed'));}
function doDump(s){} function doRestoreDump(s){}
// One floating window at a time, each persisting until a click lands OUTSIDE
// it. Handled on mousedown in the CAPTURE phase, so it runs before any
// trigger's onclick: the very click that opens a new window first closes
// whatever it landed outside of. This one check gives both behaviours — the
// outside-click close and mutual exclusivity — so openers need do nothing, and
// there is no hover-close to make a window vanish when the pointer drifts off.
document.addEventListener('mousedown',e=>{
  const overlays=[['cc',closeControl],['menu',closeMenu],['wimg',closeWatchImg]];
  for(const [id,close] of overlays){
    const el=document.getElementById(id);
    if(el&&el.style.display==='block'&&!el.contains(e.target))close();
  }
},true);
function showBackendError(msg){
  // Split mode: the page is served but the backend RPC failed, so status.get
  // came back as an {ok:false,error} envelope with no hubs. Keep the last table
  // on screen (don't blank it) and say clearly that it's stale.
  const b=document.getElementById('berr');
  if(b)b.innerHTML='backend unreachable &mdash; showing last known state <span class="dim">'+esc(msg||'')+'</span>';
  document.getElementById('ts').textContent='stale (backend down)';
}
function clearBackendError(){const b=document.getElementById('berr');if(b&&b.innerHTML)b.innerHTML='';}
function refresh(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    if(d&&d.error&&!d.hubs){showBackendError(d.error);return;}
    clearBackendError();
    render(d);document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString();if(d.version)document.getElementById('ver').textContent='v'+d.version
  }).catch(()=>{document.getElementById('ts').textContent='connection error'});
}
function _api(s){return s.replace(':','/');}
function doRefresh(c,needPwr){
  // A powered-down port has nothing to identify — a watch plugged in while the
  // port was down stays invisible, and the row keeps showing whoever sat there
  // last. So when the port is off, bring VBUS up first and give the watch time
  // to boot and enumerate; the pulse runs for that whole window, not just the
  // instant client-side re-read that a live port needs.
  if(c){refreshing.add(c);setTimeout(()=>refreshing.delete(c),needPwr?45000:10000);}
  if(!needPwr){refresh();return;}
  fetch('/api/on/'+_api(c),{method:'POST'})
    .then(()=>{[0,5000,15000,30000,44000].forEach(t=>setTimeout(refresh,t));})
    .catch(()=>refresh());
}
function _pwrFlash(c){
  const r=document.getElementById('wr-'+c);
  if(!r)return;
  r.classList.add('pwr-warn');
  setTimeout(()=>{r.classList.remove('pwr-warn');refresh();},3800);
}
function doPoweroff(c){
  fetch('/api/poweroff/'+_api(c),{method:'POST'}).then(rr=>rr.json()).then(d=>{
    // adb_shutdown false = the watch never got the command (it keeps
    // running on battery even though the port is now off) — flag it.
    if(d.confirmed===false||d.adb_shutdown===false)_pwrFlash(c);
    else setTimeout(refresh,4000);
  });
}
function doReboot(c){
  fetch('/api/reboot/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,3000));
}
function doBootloader(c){
  fetch('/api/bootloader/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,3000));
}
function doRecovery(c){
  fetch('/api/recovery/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,4000));
}
function doContinue(c){
  // Resuming the boot chain takes the watch all the way to the OS, so give
  // adb time to come up before re-reading rather than showing a bare gap.
  fetch('/api/continue/'+_api(c),{method:'POST'}).then(()=>{
    [3000,15000,30000].forEach(t=>setTimeout(refresh,t));
  });
}
function doCy(c){
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  toast('power-cycling — testing port switching…');
  fetch('/api/cycle/'+_api(c),{method:'POST'}).then(rr=>rr.json()).then(d=>{
    if(d.smart===true)toast('port switches power (smart ✓)');
    else if(d.smart===false)toast('port does NOT cut power (not smart)');
    else if(d.ok)toast('power-cycled — smart still unverified');
    else toast(d.error||'cycle failed');
    setTimeout(refresh,2500);
  }).catch(()=>setTimeout(refresh,2500));
}
function doCharge(c){
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  fetch('/api/charge/'+_api(c),{method:'POST'}).then(rr=>rr.json()).then(d=>{
    // Server state (charging_active + pct/target or end_ts) drives the row;
    // wait out the status cache before re-rendering.
    if(d.ok)setTimeout(refresh,2200);
    else{if(r)r.querySelectorAll('button').forEach(b=>b.disabled=false);}
  });
}
function tickCountdown(){
  countdownRunning=true;
  const now=Date.now();let any=false;
  Object.keys(chargeEnd).forEach(c=>{
    const rem=Math.max(0,Math.round((chargeEnd[c]-now)/1000));
    const cell=document.getElementById('bat-'+c);
    if(!cell)return;
    if(rem>0){
      any=true;
      const m=Math.floor(rem/60),s=rem%60;
      cell.innerHTML=`<span class="warn">${m}m${String(s).padStart(2,'0')}s</span>`;
    }else{delete chargeEnd[c];refresh();}
  });
  if(any){setTimeout(tickCountdown,1000);}else{countdownRunning=false;}
}
function doStopCharge(c){
  fetch('/api/charge/stop/'+_api(c),{method:'POST'}).then(()=>refresh());
}
function fmtDur(h){
  if(h<1)return Math.round(h*60)+'m';
  const d=Math.floor(h/24),hh=Math.floor(h%24);
  return d>0?`${d}d ${hh}h`:`${hh}h`;
}
function doDrain(c){
  fetch('/api/drain/'+_api(c),{method:'POST'}).then(()=>refresh());
}
let histShown=false;
function toggleHistory(){
  const el=document.getElementById('hist');
  histShown=!histShown;
  const l=document.getElementById('histlink');
  if(l)l.textContent=histShown?'hide drain history':'show drain history';
  if(!histShown){el.style.display='none';return;}
  el.style.display='block';
  el.innerHTML='<p class="dim" style="margin-top:14px">loading&hellip;</p>';
  fetch('/api/drain/history').then(r=>r.json()).then(d=>{
    if(!d.tests.length){
      el.innerHTML='<p class="dim" style="margin-top:14px">No drain tests recorded yet &mdash; results land here after the first Drain test finishes.</p>';
      return;
    }
    const wh=d.wearable_min_hours||24;
    const rows=d.tests.map(t=>{
      const dur=(t.end_ts&&t.start_ts&&t.end_ts>t.start_ts)?fmtDur((t.end_ts-t.start_ts)/3600):'&mdash;';
      const rate=t.rate!=null?t.rate.toFixed(2)+'%/h':'&mdash;';
      const estH=(t.rate!=null&&t.rate>0)?85/t.rate:null;
      const est=estH!=null?'~'+fmtDur(estH):'&mdash;';
      const verdict=estH==null?'<span class="dim">&mdash;</span>'
        :estH>=wh?'<span class="on">wearable</span>'
        :'<span class="err">swap candidate</span>';
      const dt=t.start_ts?new Date(t.start_ts*1000).toLocaleString([],{dateStyle:'medium',timeStyle:'short'}):'&mdash;';
      return `<tr><td><b>${esc(t.codename)}</b></td><td class="dim">${dt}</td>`+
        `<td>${t.start_pct}% &rarr; ${t.end_pct!=null?t.end_pct+'%':'?'}</td>`+
        `<td class="dim">${dur}</td><td>${rate}</td><td class="dim">${est}</td>`+
        `<td>${verdict}</td>`+
        `<td class="dim">${t.stopped?'stopped':'done'} (${t.samples} samples)</td></tr>`;
    }).join('');
    el.innerHTML=
      `<h1 style="font-size:15px;margin:24px 0 4px">drain test history</h1>`+
      `<p class="meta">standby drain per test &mdash; a rising rate across months means battery wear; `+
      `wearable = holds &ge;${wh}h standby (wearable_min_hours)</p>`+
      `<table><thead><tr><th>Watch</th><th>Date</th><th>Charge</th><th>Duration</th>`+
      `<th>Rate</th><th>Est. 100&rarr;15%</th><th>Verdict</th><th>Result</th></tr></thead><tbody>${rows}</tbody></table>`;
  });
}
function doStopDrain(c){
  fetch('/api/drain/stop/'+_api(c),{method:'POST'}).then(()=>refresh());
}
function toggleShowHidden(){
  showHidden=!showHidden;
  const l=document.getElementById('hidlink');
  if(l)l.textContent=showHidden?'hide avoided ports':'show all ports';
  refresh();
}
let usbPref='adb';   // fleet USB-mode preference, mirrored from status
function toggleUsbPref(){
  const next=usbPref==='ssh'?'adb':'ssh';
  fetch('/api/usb-preference/'+next,{method:'POST'}).then(()=>refresh());
}
function doHidePort(c){
  fetch('/api/hide/'+_api(c),{method:'POST'}).then(()=>refresh());
}
function doHideHub(loc){
  fetch('/api/hide-hub/'+loc,{method:'POST'}).then(()=>refresh());
}
function doWb(c){
  fetch('/api/workbench/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,2200));
}
function doWear(c,on){
  toast(on?'wear armed — topping up, port held':'wear released — port freed');
  const url=on?('/api/wear/on/'+_api(c)):('/api/wear/off/'+_api(c));
  fetch(url,{method:'POST'}).then(()=>setTimeout(refresh,1500));
}
function doStopWb(c){
  fetch('/api/workbench/stop/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,2200));
}
function doFl(c,channel){
  if(srcs[c])return;
  const box=document.getElementById('log-'+c);
  if(!box)return;
  box.textContent='';box.classList.add('show');
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  const es=new EventSource('/api/flash/'+_api(c)+(channel?('?channel='+encodeURIComponent(channel)):''));
  srcs[c]=es;
  es.onmessage=ev=>{box.textContent+=ev.data+'\\n';box.scrollTop=box.scrollHeight};
  es.addEventListener('done',()=>{box.textContent+='\\n\\u2500\\u2500 done \\u2500\\u2500\\n';box.scrollTop=box.scrollHeight;es.close();delete srcs[c];refresh()});
  es.onerror=()=>{box.textContent+='\\n\\u2500\\u2500 connection lost \\u2500\\u2500\\n';es.close();delete srcs[c];refresh()};
}
function doRemap(c){
  if(srcs[c])return;
  const box=document.getElementById('log-'+c);
  if(!box)return;
  box.textContent='';box.classList.add('show');
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  const es=new EventSource('/api/remap/'+_api(c));
  srcs[c]=es;
  es.onmessage=ev=>{box.textContent+=ev.data+'\\n';box.scrollTop=box.scrollHeight};
  es.addEventListener('done',()=>{box.textContent+='\\n\\u2500\\u2500 done \\u2500\\u2500\\n';box.scrollTop=box.scrollHeight;es.close();delete srcs[c];setTimeout(refresh,1000)});
  es.onerror=()=>{box.textContent+='\\n\\u2500\\u2500 connection lost \\u2500\\u2500\\n';es.close();delete srcs[c];refresh()};
}
// Seeded starfield (mulberry32 PRNG → same field every load), painted once into
// the fixed backdrop. Ported from moWerk's Depth Drift generator: 150 stars,
// size/opacity/drift-speed by depth for parallax.
function seedStars(){
  const seed=33,density=1,speed=1;
  let a=seed>>>0;
  const rng=()=>{a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return ((t^t>>>14)>>>0)/4294967296;};
  const pal=()=>{const t=rng();return t<0.08?'#e3b341':t<0.26?'#539bf5':t<0.6?'#8b96a5':'#5b6470';};
  const chars=['·','·','⋆','˚','.','✦'];
  // Count scales with viewport AREA so the density is constant regardless of
  // screen size (a fixed count spread over a full page reads far too sparse).
  const area=(typeof window!=='undefined'&&window.innerWidth)?window.innerWidth*window.innerHeight:1e6;
  const N=Math.round(area/2125*density);let html='';
  for(let i=0;i<N;i++){
    const x=(rng()*100).toFixed(2),y=(rng()*100).toFixed(2),depth=rng();
    const ch=chars[Math.floor(rng()*chars.length)],c=pal();
    const fs=(7.8+depth*9.6).toFixed(1),o=Math.min(1,0.3+depth*0.72).toFixed(2);
    const an='drift '+((28+(1-depth)*45)/speed).toFixed(0)+'s ease-in-out '+(rng()*10).toFixed(1)+'s infinite alternate';
    html+=`<span style="left:${x}%;top:${y}%;font-size:${fs}px;color:${c};opacity:${o};animation:${an}">${ch}</span>`;
  }
  const el=document.getElementById('stars');if(el)el.innerHTML=html;
}
seedStars();
refresh();setInterval(refresh,15000);
</script>
</body>
</html>
"""


