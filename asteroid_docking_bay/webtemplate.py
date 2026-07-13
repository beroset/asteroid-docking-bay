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
  <title>asteroid-docking-bay</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Archivo+Narrow:wght@400;700&display=swap" rel="stylesheet">
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0d1117;color:#c9d1d9;font:13px/1.6 'Cascadia Code','Fira Mono',monospace;padding:24px}
    h1{font:700 22px/1.4 'Archivo Narrow',sans-serif;color:#58a6ff;margin-bottom:4px;letter-spacing:1px}
    .hdim{color:#30363d;font-weight:400;font-size:16px;letter-spacing:3px}
    .htxt{letter-spacing:3px}
    .meta{color:#6e7681;font-size:11px;margin-bottom:20px}
    /* Fixed top bar: left/right pinned so varying string lengths (the
       update stamp) can never reposition their neighbours. */
    .topbar{display:flex;justify-content:space-between;color:#6e7681;font-size:11px;margin-bottom:2px}
    .hdr{text-align:center}
    /* Control Center overlay */
    .cn{cursor:pointer;border-bottom:1px dotted #4d5561}
    .cn:hover{color:#58a6ff;border-bottom-color:#58a6ff}
    .cc{position:fixed;z-index:100;display:none;width:auto;min-width:340px;max-width:94vw;background:#161b22;border:1px solid #30363d;border-radius:8px;box-shadow:0 10px 34px rgba(0,0,0,.6);font-size:12px;overflow:hidden}
    .cc-cols{display:flex;flex-wrap:wrap}
    .cc-col{flex:1 1 210px;min-width:200px}
    .cc-sec{padding:8px 14px}
    .cc-sech{font-size:10px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px;border-bottom:1px solid #21262d;padding-bottom:3px;margin-bottom:5px}
    .cc-hd{padding:8px 30px 8px 12px;background:#0d1117;border-bottom:1px solid #30363d;font-weight:700;color:#58a6ff;position:relative}
    .cc-hd .dim{font-weight:400}
    .cc-x{position:absolute;right:10px;top:6px;cursor:pointer;color:#6e7681;font-weight:400;font-size:16px;line-height:1}
    .cc-x:hover{color:#fff}
    .cc-grid{display:grid;grid-template-columns:auto 1fr;gap:3px 10px}
    .cc-act.mini{width:auto;flex:1;padding:6px}
    .cc-k{color:#6e7681}
    .cc-v{color:#c9d1d9;text-align:right;font-variant-numeric:tabular-nums;word-break:break-all}
    .cc-tgls{display:flex;flex-wrap:wrap;gap:8px;padding:2px 12px 10px}
    .cc-tgl{flex:1;padding:7px 4px;border-radius:6px;border:1px solid #30363d;background:transparent;cursor:pointer;font:inherit;color:#8b949e}
    .cc-tgl.on{border-color:#3fb950;color:#3fb950}
    .cc-tgl.busy{opacity:.5;cursor:progress}
    .cc-tgl:hover{background:#0d1117}
    .cc-acts{padding:0 12px 12px}
    .cc-act{width:100%;padding:8px;border-radius:6px;border:1px solid #388bfd;background:transparent;color:#388bfd;cursor:pointer;font:inherit}
    .cc-act:hover{background:#0d1f3a}
    .cc-act.done{border-color:#3fb950;color:#3fb950}
    /* Row action floating menus */
    .btn.pw{border-color:#f0883e;color:#f0883e}
    .btn.pw:hover{background:#2a1a0e}
    .menu{position:fixed;z-index:110;display:none;min-width:172px;background:#161b22;border:1px solid #30363d;border-radius:7px;box-shadow:0 10px 30px rgba(0,0,0,.6);padding:5px}
    .menu-item{display:block;width:100%;text-align:left;padding:6px 10px;margin:1px 0;border-radius:5px;border:1px solid transparent;background:transparent;color:#c9d1d9;cursor:pointer;font:inherit;white-space:nowrap}
    .menu-item:hover:not(:disabled){background:#0d1117;border-color:#30363d}
    .menu-item:disabled{opacity:.38;cursor:default}
    .menu-item.ch{color:#3fb950}.menu-item.dr{color:#58a6ff}.menu-item.ht{color:#f0883e}
    .menu-item.wbx{color:#a371f7}.menu-item.dng{color:#f85149}
    .menu-sep{height:1px;background:#30363d;margin:4px 2px}
    .menu-hd{padding:3px 10px 5px;font-size:10px;color:#6e7681}
    #toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(20px);background:#161b22;border:1px solid #30363d;color:#c9d1d9;padding:9px 16px;border-radius:7px;font-size:12px;opacity:0;pointer-events:none;transition:.2s;z-index:200}
    #toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
    table{width:100%;border-collapse:collapse}
    th{color:#6e7681;text-align:left;padding:5px 12px;border-bottom:1px solid #21262d;font-size:11px;text-transform:uppercase;letter-spacing:1px;font-weight:normal}
    th:first-child,td.tc{width:22px;padding-right:0}
    td{padding:7px 12px;border-bottom:1px solid #161b22;vertical-align:middle}
    .wr:hover td{background:#161b22}
    .hub-hdr td{background:#0d1420;color:#6e7681;padding:9px 12px 4px;border-top:1px solid #21262d;border-bottom:1px solid #21262d;font-size:11px;letter-spacing:1px}
    .hub-hdr:first-child td{border-top:none;padding-top:0}
    .hl{color:#58a6ff;font-weight:bold;margin-right:8px}
    td.tc{color:#30363d;font-size:12px;user-select:none}
    tr.empty td{color:#6e7681}
    tr.empty:hover td{background:#0a0d13}
    .on{color:#3fb950}.off{color:#6e7681}.warn{color:#d29922}.err{color:#f85149}.dim{color:#6e7681}
    .dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;vertical-align:middle}
    .don{background:#3fb950}.doff{background:#30363d}
    .tgl{display:inline-flex;align-items:center;gap:4px;background:none;border:1px solid;padding:3px 9px 3px 6px;border-radius:20px;cursor:pointer;font:12px monospace;vertical-align:middle;margin-right:3px;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:background .12s,transform .12s}
    .tgl-on{border-color:#3fb950;color:#3fb950}.tgl-on:hover{background:#0f2a18}
    .tgl-off{border-color:#30363d;color:#6e7681}.tgl-off:hover{background:#161b22}
    .tgl:active{transform:scale(.92);transition:transform 55ms ease-out}
    .ico{background:none;border:1px solid #30363d;color:#6e7681;padding:2px 6px;border-radius:4px;cursor:pointer;font:13px monospace;vertical-align:middle;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:background .12s,transform .12s}
    .ico:hover{background:#21262d;color:#c9d1d9}
    .ico:active{transform:scale(.88);transition:transform 55ms ease-out}
    .tgl:disabled,.ico:disabled{opacity:.35;cursor:default;pointer-events:none}
    .btn{background:none;color:#c9d1d9;border:1px solid #30363d;padding:3px 9px;border-radius:4px;cursor:pointer;font:12px monospace;margin-right:3px;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:background .12s,transform .12s}
    .btn:hover{background:#21262d}
    .btn:active{transform:scale(.92);transition:transform 55ms ease-out}
    .fl{border-color:#58a6ff;color:#58a6ff}.fl:hover{background:#111d2e}
    .ch{border-color:#3fb950;color:#3fb950}.ch:hover{background:#0f2a18}
    .ht{border-color:#6e7681;color:#6e7681}.ht:hover{background:#1c1c1c}
    .hcut{border-color:#f85149;color:#f85149}.hcut:hover{background:#2a0d0b}
    .hrb{border-color:#d29922;color:#d29922}.hrb:hover{background:#2a2113}
    .hbl{border-color:#58a6ff;color:#58a6ff}.hbl:hover{background:#111d2e}
    .btn:disabled{opacity:.35;cursor:default;pointer-events:none}
    .btn.dr{border-color:#388bfd;color:#388bfd}
    .btn.wb{border-color:#bc8cff;color:#bc8cff}.btn.wb:hover{background:#1d1430}
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
    .wr.refreshing:hover td{background:transparent!important;animation:rpulse 1.1s ease-in-out infinite}
    .btn-ref.pulsing{animation:bpulse .85s ease-in-out infinite!important;border-color:#58a6ff!important;color:#58a6ff!important}
    @keyframes pwrwarn{0%,100%{background:transparent}40%{background:rgba(248,81,73,.12)}}
    .wr.pwr-warn td{animation:pwrwarn 1.8s ease-in-out 2}
  </style>
</head>
<body>
  <div class="topbar"><span id="ts">loading&hellip;</span><span id="ver"></span></div>
  <div class="hdr">
  <h1><span class="hdim">&#x2728;  &#x22C6;  &#x02DA; </span>&#x2726;<span class="htxt">  asteroid-docking-bay  </span>&#x2726;<span class="hdim"> &#x02DA;  &#x22C6;  &#x2728;</span></h1>
  <p class="meta"><a href="#" id="histlink" onclick="toggleHistory();return false" style="color:#388bfd;text-decoration:none">show drain history</a> &nbsp;&middot;&nbsp; <a href="#" id="hidlink" onclick="toggleShowHidden();return false" style="color:#6e7681;text-decoration:none">show all ports</a></p>
  </div>
  <table>
    <thead><tr>
      <th></th><th>Watch</th><th>Port</th><th>Power</th><th>Smart</th>
      <th>Connection</th><th style="min-width:7em">Battery</th><th>Actions</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
  <div id="hist" style="display:none"></div>
  <div id="cc" class="cc" onmouseleave="ccLeave()" onmouseenter="ccEnter()"></div>
  <div id="menu" class="menu" onmouseleave="menuLeave()" onmouseenter="menuEnter()"></div>
<script>
const srcs={};
const chargeEnd={};
let countdownRunning=false;
let showHidden=false;
const refreshing=new Set();
function mkhide(slot,excluded){
  return `<a href="#" class="hidebtn" onclick="doHidePort('${slot}');return false" title="${excluded?'un-hide this row':'hide this row'}">${excluded?'&#x2295;':'&#x2296;'}</a>`;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function mkpwr(v){return v===true?'<span class="dot don"></span><span class="on">ON</span>':v===false?'<span class="dot doff"></span><span class="off">OFF</span>':'<span class="dim">---</span>'}
function mksmt(v){return v===true?'<span class="on">yes</span>':v===false?'<span class="err">NO!</span>':'<span class="warn">?</span>'}
function mkbat(v,lo,hi){
  if(v==null)return '<span class="dim">&mdash;</span>';
  const cls=v<lo?'err':v<=hi?'on':'dim';
  return `<span class="${cls}">${v}%</span>`;
}
function mkport(p){
  let s = p.socket!=null
    ? `<b style="color:#c9d1d9">socket ${p.socket}</b> <span class="dim" style="font-size:10px">p${p.port}</span>`
    : `<span class="dim">p${p.port}</span>`;
  if(p.excluded) s = `<span class="err" title="${esc(p.excluded)}">&#9888; avoid</span> ` + s;
  return s;
}
const AOSLOGO='<svg viewBox="0 0 2000 2000" width="13" height="13" style="vertical-align:-2px;margin-right:5px" shape-rendering="crispEdges" xmlns="http://www.w3.org/2000/svg"><defs><rect id="T" width="2" height="2"/></defs><g transform="matrix(100 100 -100 100 1000 0)"><g><use href="#T" style="fill:#be3729"/><use href="#T" id="b" x="2" style="fill:#dc2919"/><use href="#T" id="c" x="4" style="fill:#e54b3a"/><use href="#T" id="d" x="6" style="fill:#e56934"/><use href="#T" id="e" x="8" style="fill:#e57c21"/></g><g transform="translate(-2,2)"><use href="#b"/><use href="#c"/><use href="#T" id="f" x="10" style="fill:#e58a21"/></g><g transform="translate(-4,4)"><use href="#c"/><use href="#e"/><use href="#T" id="g" x="12" style="fill:#f19a11"/></g><g transform="translate(-6,6)"><use href="#d"/><use href="#e"/><use href="#f"/><use href="#T" id="h" x="14" style="fill:#f0ae0e"/></g><g transform="translate(-8,8)"><use href="#e"/><use href="#f"/><use href="#g"/><use href="#h"/><use href="#T" x="16" style="fill:#f0c30e"/></g></g></svg>';
function mkadb(adb,fbprod,os){
  if(adb==='device'){
    if(os==='asteroidos')return `${AOSLOGO}<span class="on" title="AsteroidOS on ADB">ADB</span>`;
    if(os&&os!=='unknown')return `<span class="on" title="${esc(os)} on ADB">ADB <span class="dim">${esc(os)}</span></span>`;
    return '<span class="on">ADB</span>';
  }
  if(adb==='ssh')return `${os==='asteroidos'?AOSLOGO:''}<span class="warn" title="watch is in SSH/developer USB mode — reachable over SSH, no ADB functions">SSH</span>`;
  if(adb==='fastboot'){const l=fbprod?`fastboot: ${esc(fbprod)}`:'fastboot';return `<span class="warn">${l}</span>`;}
  if(adb)return `<span class="dim">${esc(adb)}</span>`;
  return '<span class="dim">&mdash;</span>';
}
function mkadbrow(p){
  if(p.adb===null&&p.not_enumerating)
    return '<span class="err" title="port is powered and the hub sees a connection, but the device never enumerates — flat battery bootloop or bad cable. Tip: holding the watch in fastboot draws less than booting and lets a flat battery charge past the boot threshold.">&#9888; not enumerating</span>';
  if(p.adb===null&&p.power===true&&p.connected===false)
    return '<span class="warn" title="port is powered but nothing is electrically connected — watch not docked, or dead cable/contact">&#9888; not docked</span>';
  return mkadb(p.adb,null,p.os);
}
function render(data){
  const tb=document.getElementById('tb');
  const hubs=(data&&data.hubs)||[];
  const lo=(data&&data.thresholds&&data.thresholds.low)||40;
  const hi=(data&&data.thresholds&&data.thresholds.high)||80;
  const floor=(data&&data.drain_floor)||15;
  const wearH=(data&&data.wearable_min_hours)||24;
  if(!hubs.length){tb.innerHTML='<tr><td colspan="8" class="dim">No watches configured. Run: asteroid-docking-bay map</td></tr>';return}
  const rows=[];
  hubs.forEach(hub=>{
    if(hub.hidden&&!showHidden)return;
    const hubHideBtn=`<a href="#" class="hidebtn" onclick="doHideHub('${esc(hub.location)}');return false" title="${hub.hidden?'un-hide this hub':'hide this whole hub'}">${hub.hidden?'&#x1F441; show':'&#x1F648; hide'}</a>`;
    rows.push(`<tr class="hub-hdr${hub.hidden?' hiddenrow':''}"><td colspan="8"><span class="hl">${esc(hub.location)}</span><span class="dim">${esc(hub.description)}</span> ${hubHideBtn}</td></tr>`);
    const visPorts=hub.ports.filter(p=>showHidden||!p.excluded);
    visPorts.forEach((p,i)=>{
      const tree=i===visPorts.length-1?'&#x2514;&#x2500;':'&#x251c;&#x2500;';
      if(p.empty){
        const slot=p.slot_loc+':'+p.port;
        const busy=!!(srcs[slot]||p.flashing);
        const d=(busy||p.excluded)?' disabled':'';
        const fbLabel=p.fastboot_product?`fastboot: ${esc(p.fastboot_product)}`:(p.adb==='fastboot'?'fastboot':'');
        const nameCell=p.unmapped
          ?`<span class="dim">${esc(p.codename)} <span style="font-size:.8em;opacity:.6">(click Onboard)</span></span>`
          :(p.fastboot_product?`<span class="warn">${esc(p.fastboot_product)}</span>`:'<span class="dim">&mdash;</span>');
        const adbCell=p.adb==='fastboot'?`<span class="warn">${fbLabel}</span>`
          :mkadbrow(p);
        const pwrCls=p.power===true?'tgl tgl-on':'tgl tgl-off';
        const pwrLbl=p.power===true?'<span class="dot don"></span>ON':'<span class="dot doff"></span>OFF';
        const pwrFn=p.power===true?`doOff('${slot}')`:`doOn('${slot}')`;
        const onboardBtn=p.excluded?'':`<button class="btn ob"${d} onclick="doRemap('${slot}')" title="power the port on, wait for the watch to boot (cycles once if it fails to enumerate), then identify and map it">&#x2795; Onboard</button>`;
        rows.push(
          `<tr class="wr empty${p.excluded?' excl':''}" id="wr-${slot}">` +
          `<td class="tc">${tree}</td>` +
          `<td>${nameCell}</td>` +
          `<td>${mkport(p)}</td>` +
          `<td><button class="${pwrCls}"${d} onclick="${pwrFn}">${pwrLbl}</button><button class="ico"${d} onclick="doCy('${slot}')" title="Power-cycle port">&#x21BA;</button></td>` +
          `<td>${mksmt(p.smart)}</td>` +
          `<td>${adbCell}</td>` +
          `<td class="dim">&mdash;</td>` +
          `<td>`+onboardBtn+mkhide(slot,p.excluded)+`</td>` +
          `</tr>` +
          `<tr class="lr" id="lr-${slot}"><td colspan="8"><div class="log${busy?' show':''}" id="log-${slot}"></div></td></tr>`
        );
      }else{
        const slot=p.slot_loc+':'+p.port;
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
        const d=(busy||p.excluded)?' disabled':'';
        const noSw=p.smart===false;
        const dp=(busy||noSw||p.excluded)?' disabled':'';
        const noSwT=noSw?' title="port cannot switch power (not smart)"':'';
        const adb=mkadbrow(p);
        let bat;
        if(wb){
          const w=p.workbench;
          const pct=w.pct!=null?w.pct+'% ':'';
          bat=`<span class="warn" title="workbench: battery held in the ${lo}–${hi}% band while you work over WiFi/SSH${w.blind?' (battery unreadable — blind duty cycle)':''}">&#128295; ${pct}${esc(w.phase||'')}</span>`;
        }else if(charging){
          if(p.charge_losing){bat=`<span class="err" title="battery is DROPPING while charging — losing power despite the charge attempt. Check contacts / cable / port (the dirty-contact failure).">&#9888; ${p.charge_pct!=null?p.charge_pct:'?'}% &#8595; losing power!</span>`;}
          else if(p.charge_target!=null){bat=`<span class="warn">&#9889; ${p.charge_pct!=null?p.charge_pct:'?'}% &rarr; ${p.charge_target}%</span>`;}
          else if(chargeEnd[slot]){const rem=Math.max(0,Math.round((chargeEnd[slot]-Date.now())/1000));const m=Math.floor(rem/60),s=rem%60;bat=`<span class="warn">&#9889; ${m}m${String(s).padStart(2,'0')}s</span>`;}
          else{bat='<span class="warn">&#9889; starting&hellip;</span>';}
        }
        else if(draining){
          const dr=p.drain;
          let txt=(dr.last_pct!==null?dr.last_pct+'%':'?%')+' &#x2193;';
          if(dr.drain_rate!==null&&dr.drain_rate>0){
            txt=`${dr.last_pct}% &minus;${dr.drain_rate.toFixed(1)}%/h`;
            if(dr.last_pct>floor){const estH=(dr.last_pct-floor)/dr.drain_rate;txt+=` (~${fmtDur(estH)})`;}
          }
          bat=`<span class="warn">${txt}</span>`;
        }else if(p.drain&&p.drain.done&&p.drain.last_pct!==null){
          const dr=p.drain;
          const summary=dr.drain_rate!==null?` &minus;${dr.drain_rate.toFixed(1)}%/h`:'';
          bat=`${mkbat(p.battery,lo,hi)}<span class="dim" style="font-size:10px"> (test: ${dr.last_pct}%${summary})</span>`;
        }else{
          bat=mkbat(p.battery,lo,hi);
          if(p.drain_last&&p.drain_last.est_h!=null){
            const ok=p.drain_last.est_h>=wearH;
            const when=new Date(p.drain_last.ts*1000).toLocaleDateString();
            const tip=ok
              ?`holds ~${fmtDur(p.drain_last.est_h)} standby (100&rarr;15%, drain test ${when}) — wearable`
              :`holds only ~${fmtDur(p.drain_last.est_h)} standby (100&rarr;15%, drain test ${when}) — below ${wearH}h wearable threshold: battery swap candidate / dev watch`;
            bat+=` <span class="${ok?'dim':'err'}" style="font-size:10px" title="${tip}">${ok?'&#8986;':'&#129707;'}~${fmtDur(p.drain_last.est_h)}</span>`;
          }
        }
        const pwrFn=p.power===true?`doOff('${slot}')`:`doOn('${slot}')`;
        const pwrCls=p.power===true?'tgl tgl-on':'tgl tgl-off';
        const pwrLbl=p.power===true?'<span class="dot don"></span>ON':'<span class="dot doff"></span>OFF';
        const isRef=refreshing.has(slot);
        rows.push(
          `<tr class="wr${isRef?' refreshing':''}${p.excluded?' excl':''}" id="wr-${slot}">` +
          `<td class="tc">${tree}</td>` +
          `<td>`+(p.adb==='device'
            ?`<b class="cn" onclick="openCC('${p.serial}','${p.codename}',event)" title="open Control Center">${esc(p.codename)}</b>`
            :`<b>${esc(p.codename)}</b>`)+`</td>` +
          `<td>${mkport(p)}</td>` +
          `<td><button class="${pwrCls}"${dp}${noSwT} onclick="${pwrFn}">${pwrLbl}</button><button class="ico"${dp} onclick="doCy('${slot}')" title="Power-cycle port">&#x21BA;</button></td>` +
          `<td>${mksmt(p.smart)}</td>` +
          `<td>${adb}</td>` +
          `<td id="bat-${slot}">${bat}</td>` +
          `<td id="act-${slot}">` +
          `<button class="ico${isRef?' pulsing':''}"${d} onclick="doRefresh('${slot}')" title="refresh / re-identify this port">&#x21BB;</button>` +
          (!isFb?`<button class="btn pw"${p.excluded?' disabled':''} onclick="menuPower(event,'${slot}',${charging},${draining},${p.power===true},${noSw})" title="power / charge / drain / reboot">&#x23FB; Power &#9662;</button>`:'')+
          (!isFb?`<button class="btn wb"${p.excluded?' disabled':''} onclick="menuWorkbench(event,'${slot}','${p.serial}',${wb},${p.adb==='device'})" title="attended actions — watch stays on">&#128295; Workbench &#9662;</button>`:'')+
          `<button class="btn fl"${d} onclick="menuFlash(event,'${slot}')" title="backup / restore / flash">&#128190; Flash &#9662;</button>` +
          `</td></tr>` +
          `<tr class="lr" id="lr-${slot}"><td colspan="8"><div class="log${logActive?' show':''}" id="log-${slot}"></div></td></tr>`
        );
      }
    });
  });
  tb.innerHTML=rows.join('');
  Object.keys(srcs).forEach(c=>{const b=document.getElementById('log-'+c);if(b)b.classList.add('show');});
  if(Object.keys(chargeEnd).length>0&&!countdownRunning)tickCountdown();
}
// ── Control Center overlay ──────────────────────────────────────────────────
let ccSerial=null, ccName=null, ccTimer=null, ccAX=0, ccAY=0;
function fmtUp(sec){sec=Math.floor(+sec||0);const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);return (d?d+'d ':'')+(h||d?h+'h ':'')+m+'m';}
function ccPlace(){
  // Anchor to the click; flip ABOVE the anchor if the panel would run off the
  // bottom (its height only known after the async data renders). No page scroll.
  const cc=document.getElementById('cc'), h=cc.offsetHeight, w=cc.offsetWidth;
  let left=Math.min(ccAX, window.innerWidth-w-8);
  let top=ccAY+10;
  if(top+h>window.innerHeight-8) top=ccAY-h-10;
  cc.style.left=Math.max(8,left)+'px'; cc.style.top=Math.max(8,top)+'px';
}
function openCC(serial,name,ev){
  ev.stopPropagation();
  ccSerial=serial; ccName=name; ccAX=ev.clientX; ccAY=ev.clientY;
  const cc=document.getElementById('cc');
  cc.innerHTML=`<div class="cc-hd">${name} <span class="dim">loading&hellip;</span></div>`;
  cc.style.display='block'; ccPlace();
  ccFetch();
}
function ccFetch(){
  const s=ccSerial;
  fetch('/api/watch/'+encodeURIComponent(s)).then(r=>r.json()).then(d=>{if(ccSerial===s)renderCC(d)}).catch(()=>{
    const cc=document.getElementById('cc');cc.innerHTML=`<div class="cc-hd">${ccName} <span class="err">unreachable</span><span class="cc-x" onclick="closeCC()">&times;</span></div>`;
  });
}
function renderCC(d){
  const cc=document.getElementById('cc');
  if(!d||!d.kernel){cc.innerHTML=`<div class="cc-hd">${ccName} <span class="err">no data (watch offline?)</span><span class="cc-x" onclick="closeCC()">&times;</span></div>`;ccPlace();return;}
  const kv=(k,v)=>`<div class="cc-k">${k}</div><div class="cc-v">${esc(v==null||v===''?'—':String(v))}</div>`;
  const sec=(t,r)=>`<div class="cc-sec"><div class="cc-sech">${t}</div><div class="cc-grid">${r}</div></div>`;
  const num=x=>(x==null||x===''||isNaN(+x))?null:+x;
  const mt=+d.memtotal,mf=+d.memfree,memU=mt?Math.round((mt-mf)/1024):null,memT=mt?Math.round(mt/1024):null;
  const bv=num(d.bat_volt),ba=num(d.bat_curr),bt=num(d.bat_temp),uv=num(d.usb_volt),freq=num(d.cpufreq);
  const dfp=(d.df||'').trim().split(/[ \t]+/);
  const storage=dfp.length>=5?`${dfp[2]} / ${dfp[1]} (${dfp[4]})`:null;
  const mb=x=>{const n=num(x);return n==null?null:(n/1048576).toFixed(2)+' MB';};
  const phone=(+d.btcount>0)?(d.btmac||'connected'):'none';
  // Real unicode arrows, not entities: this value passes through esc(),
  // which would render an entity as literal "&#9660;" text.
  const cur=ba==null?null:`${(ba/1000).toFixed(0)} mA ${ba<-5?'\\u25bc':ba>5?'\\u25b2':''}`;
  const sys=sec('System',
    kv('Kernel',d.kernel)+kv('Qt',d.qt)+kv('SoC',(d.soc||'').trim())+
    kv('CPU',freq?(freq/1000).toFixed(0)+' MHz':null)+
    kv('Uptime',fmtUp(d.uptime))+kv('Boot',d.bootreason)+
    kv('Load',d.load)+kv('Threads',d.threads)+
    kv('Memory',memU!=null?`${memU} / ${memT} MB`:null)+kv('Storage',storage));
  const bat=sec('Battery',
    kv('Charge',d.bat_cap!=null&&d.bat_cap!==''?d.bat_cap+'%':null)+kv('Status',d.bat_status)+
    kv('Health',d.bat_health)+kv('Tech',d.bat_tech)+
    kv('Voltage',bv?(bv/1e6).toFixed(3)+' V':null)+kv('Current',cur)+
    kv('Temp',bt!=null?(bt/10).toFixed(1)+' °C':null)+kv('Cycles',d.bat_cycles)+
    kv('USB in',uv!=null&&uv>0?(uv/1e6).toFixed(2)+' V':(+d.usb_online?'online':null)));
  const net=sec('Network &amp; links',
    kv('WiFi',d.wifi==null?null:(d.wifi?'on':'off'))+kv('IP',d.ip)+
    kv('RX / TX',(mb(d.net_rx)||'0')+' / '+(mb(d.net_tx)||'0'))+
    kv('Bluetooth',d.bluetooth==null?null:(d.bluetooth?'on':'off'))+kv('Phone',phone)+
    kv('Timezone',d.tz)+kv('Clock',d.datetime)+kv('WLAN MAC',d.wlanmac)+kv('Serial',d.serial));
  const tgl=(t,l,on)=>`<button class="cc-tgl${on?' on':''}" onclick="ccToggle('${t}',${on?0:1})">${l}: ${on?'ON':'OFF'}</button>`;
  cc.innerHTML=
    `<div class="cc-hd">${esc(ccName)} <span class="dim">${esc(d.os||'')}</span><span class="cc-x" onclick="closeCC()">&times;</span></div>`+
    `<div class="cc-cols"><div class="cc-col">${sys}</div><div class="cc-col">${bat}</div><div class="cc-col">${net}</div></div>`+
    `<div class="cc-tgls">${tgl('wifi','WiFi',d.wifi)}${tgl('bluetooth','BT',d.bluetooth)}`+
      `<button class="cc-tgl" onclick="ccBuzz()" title="vibrate to locate in the dock">&#128243; Buzz</button>`+
      `<button class="cc-tgl" onclick="ccScreen(true)" title="force the screen on (mce demo mode — stays on until released!)">&#128161; On</button>`+
      `<button class="cc-tgl" onclick="ccScreen(false)" title="release the forced screen">&#128161; Off</button>`+
      `<button class="cc-tgl" onclick="doScreenshot('${d.serial}')" title="screenshot in a new tab">&#128247; Shot</button></div>`+
    `<div class="cc-acts"><button class="cc-act" id="cc-time" onclick="ccSyncTime()">&#x21BB; Sync time from host</button></div>`;
  ccPlace();
}
function ccBuzz(){fetch('/api/watch/'+encodeURIComponent(ccSerial)+'/buzz',{method:'POST'}).then(()=>toast('buzzed'));}
function ccScreen(on){fetch('/api/watch/'+encodeURIComponent(ccSerial)+'/screen/'+(on?'on':'off'),{method:'POST'}).then(()=>toast(on?'screen forced on \u2014 release it when done!':'screen released'));}
function ccToggle(tech,on){
  document.querySelectorAll('.cc-tgl').forEach(b=>b.classList.add('busy'));
  fetch('/api/watch/'+encodeURIComponent(ccSerial)+'/toggle/'+tech+'/'+(on?'on':'off'),{method:'POST'})
    .then(()=>setTimeout(ccFetch,1600)).catch(()=>ccFetch());
}
function ccSyncTime(){
  const b=document.getElementById('cc-time');if(b)b.textContent='syncing…';
  fetch('/api/watch/'+encodeURIComponent(ccSerial)+'/settime',{method:'POST'})
    .then(()=>setTimeout(()=>{const bb=document.getElementById('cc-time');if(bb){bb.textContent='✓ synced';bb.classList.add('done');}ccFetch();},700));
}
function closeCC(){const cc=document.getElementById('cc');cc.style.display='none';ccSerial=null;if(ccTimer){clearTimeout(ccTimer);ccTimer=null;}}
function ccLeave(){ccTimer=setTimeout(closeCC,600);}
function ccEnter(){if(ccTimer){clearTimeout(ccTimer);ccTimer=null;}}
// ── Row action floating menus ───────────────────────────────────────────────
let menuTimer=null;
function openMenu(ev,html){
  ev.stopPropagation();
  const m=document.getElementById('menu');
  m.innerHTML=html; m.style.left='-9999px'; m.style.top='0px'; m.style.display='block';
  const r=ev.currentTarget.getBoundingClientRect(), mw=m.offsetWidth, mh=m.offsetHeight;
  let left=r.left, top=r.bottom+3;
  if(left+mw>window.innerWidth-8)left=window.innerWidth-8-mw;
  if(top+mh>window.innerHeight-8)top=Math.max(8,r.top-mh-3);
  m.style.left=Math.max(8,left)+'px'; m.style.top=top+'px';
}
function closeMenu(){document.getElementById('menu').style.display='none';}
function menuLeave(){menuTimer=setTimeout(closeMenu,450);}
function menuEnter(){if(menuTimer){clearTimeout(menuTimer);menuTimer=null;}}
function mi(cls,label,fn,dis){return `<button class="menu-item ${cls}"${dis?' disabled title="not available yet"':` onclick="${fn};closeMenu()"`}>${label}</button>`;}
function menuPower(ev,slot,charging,draining,powered,noSw){
  openMenu(ev,
    (charging?mi('ch','&#9632; Stop charge',`doStopCharge('${slot}')`):mi('ch','&#9889; Charge',`doCharge('${slot}')`,noSw))+
    (draining?mi('dr','&#9632; Stop drain test',`doStopDrain('${slot}')`):mi('dr','&#128201; Drain test',`doDrain('${slot}')`,noSw))+
    '<div class="menu-sep"></div>'+
    (powered?mi('ht','&#x23FB; Power off',`doPoweroff('${slot}')`):'')+
    mi('ht','&#x21BB; Reboot',`doReboot('${slot}')`)+
    mi('dng','&#128295; Bootloader',`doBootloader('${slot}')`));
}
function menuWorkbench(ev,slot,serial,wb,online){
  openMenu(ev,
    '<div class="menu-hd">watch stays on — power off when done</div>'+
    (wb?mi('wbx','&#8617; End checkout',`doStopWb('${slot}')`):mi('wbx','&#128295; Checkout (hold band)',`doWb('${slot}')`))+
    '<div class="menu-sep"></div>'+
    mi('','&#x21BB; Set time from host',`doSetTime('${serial}')`,!online)+
    mi('','&#128247; Screenshot',`doScreenshot('${serial}')`,!online)+
    mi('','&#128276; Test notification',`doNotify('${serial}')`,!online));
}
function menuFlash(ev,slot){
  openMenu(ev,
    mi('','&#128190; Backup data',`doBackup('${slot}')`,true)+
    mi('','&#8617; Restore data',`doRestore('${slot}')`,true)+
    '<div class="menu-sep"></div>'+
    mi('','&#9889; Flash nightly',`doFl('${slot}')`)+
    mi('',"Flash 2.1",`doFlV('${slot}','2.1')`,true)+
    mi('',"Flash 2.0",`doFlV('${slot}','2.0')`,true));
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
function doFlV(s,v){toast('Flash '+v+' — not released yet');}
function doBackup(s){} function doRestore(s){}
document.addEventListener('click',e=>{
  const cc=document.getElementById('cc');if(cc.style.display==='block'&&!cc.contains(e.target)&&!e.target.classList.contains('cn'))closeCC();
  const m=document.getElementById('menu');if(m.style.display==='block'&&!m.contains(e.target))closeMenu();
});
function refresh(){
  fetch('/api/status').then(r=>r.json()).then(d=>{render(d);document.getElementById('ts').textContent='updated '+new Date().toLocaleTimeString();if(d.version)document.getElementById('ver').textContent='v'+d.version}).catch(()=>{document.getElementById('ts').textContent='connection error'});
}
function _api(s){return s.replace(':','/');}
function doRefresh(c){
  if(c){refreshing.add(c);setTimeout(()=>refreshing.delete(c),10000);}
  refresh();
}
function _pwrFlash(c){
  const r=document.getElementById('wr-'+c);
  if(!r)return;
  r.classList.add('pwr-warn');
  setTimeout(()=>{r.classList.remove('pwr-warn');refresh();},3800);
}
function doOn(c){
  fetch('/api/on/'+_api(c),{method:'POST'}).then(rr=>rr.json()).then(d=>{
    if(d.confirmed===false)_pwrFlash(c);else setTimeout(refresh,2000);
  });
}
function doOff(c){
  fetch('/api/off/'+_api(c),{method:'POST'}).then(rr=>rr.json()).then(d=>{
    if(d.confirmed===false)_pwrFlash(c);else refresh();
  });
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
function doCy(c){
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  fetch('/api/cycle/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,7000));
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
      cell.innerHTML=`<span class="warn">&#9889; ${m}m${String(s).padStart(2,'0')}s</span>`;
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
        :estH>=wh?'<span class="on">&#8986; wearable</span>'
        :'<span class="err">&#129707; swap candidate</span>';
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
      `&#8986; = holds &ge;${wh}h standby (wearable_min_hours)</p>`+
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
function doHidePort(c){
  fetch('/api/hide/'+_api(c),{method:'POST'}).then(()=>refresh());
}
function doHideHub(loc){
  fetch('/api/hide-hub/'+loc,{method:'POST'}).then(()=>refresh());
}
function doWb(c){
  fetch('/api/workbench/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,2200));
}
function doStopWb(c){
  fetch('/api/workbench/stop/'+_api(c),{method:'POST'}).then(()=>setTimeout(refresh,2200));
}
function doFl(c){
  if(srcs[c])return;
  const box=document.getElementById('log-'+c);
  if(!box)return;
  box.textContent='';box.classList.add('show');
  const r=document.getElementById('wr-'+c);
  if(r)r.querySelectorAll('button').forEach(b=>b.disabled=true);
  const es=new EventSource('/api/flash/'+_api(c));
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
refresh();setInterval(refresh,15000);
</script>
</body>
</html>
"""


