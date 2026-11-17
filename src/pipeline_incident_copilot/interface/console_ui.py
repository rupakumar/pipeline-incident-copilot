# ruff: noqa: E501  (HTML, CSS and JS templates read better unwrapped)
"""The console's page frame for the connections and settings pages: theme (light + dark), header, shared widgets.

Widgets (plain JS, no libraries, works offline):
- adapter: the platform mark and the copilot mark joined by a cable; data-state idle | testing | ok | fail
- switch: a button role="switch" that keeps a hidden input in sync
- chips: an email list input (Enter or comma adds, × removes) backed by a hidden input
"""

from html import escape

from pipeline_incident_copilot.interface.brand_logos import LOGOS

CSS = """
:root{color-scheme:light;
--bg:#F3F5F7;--surface:#FFFFFF;--sunken:#EEF1F4;--ink:#17212B;--muted:#5A6675;--line:#DCE2E8;
--accent:#2F6690;--accent-ink:#FFFFFF;--accent-soft:#E6EEF5;--good:#1F7A63;--good-soft:#E5F2EC;
--bad:#B42318;--bad-soft:#FBEBE9;--warn:#8A6A12;--warn-soft:#F7F0DC;--shadow:0 1px 2px rgba(23,33,43,.06),0 8px 24px rgba(23,33,43,.06)}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--bg:#0E141A;--surface:#161F28;--sunken:#1C2732;--ink:#E5ECF2;--muted:#94A2B1;--line:#2A3743;
--accent:#6FA8DC;--accent-ink:#0E141A;--accent-soft:#1D2F40;--good:#4CC3A0;--good-soft:#173129;
--bad:#F0806F;--bad-soft:#3A1F1C;--warn:#D9B45A;--warn-soft:#332A16;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25)}}
:root[data-theme="dark"]{color-scheme:dark;
--bg:#0E141A;--surface:#161F28;--sunken:#1C2732;--ink:#E5ECF2;--muted:#94A2B1;--line:#2A3743;
--accent:#6FA8DC;--accent-ink:#0E141A;--accent-soft:#1D2F40;--good:#4CC3A0;--good-soft:#173129;
--bad:#F0806F;--bad-soft:#3A1F1C;--warn:#D9B45A;--warn-soft:#332A16;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.25)}
@view-transition{navigation:auto}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
Helvetica,Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--accent)}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px}
.top{background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.top .in{max-width:1120px;margin:0 auto;padding:0 20px;height:56px;display:flex;align-items:center;gap:22px}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.01em;color:var(--ink);text-decoration:none}
.nav{display:flex;gap:4px;flex:1}
.nav a{padding:6px 12px;border-radius:8px;color:var(--muted);text-decoration:none;font-weight:600;font-size:14px}
.nav a:hover{color:var(--ink);background:var(--sunken)} .nav a[aria-current]{color:var(--ink);background:var(--accent-soft)}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:26px;letter-spacing:-.02em;margin:0;text-wrap:balance} h2{font-size:17px;margin:0;letter-spacing:-.01em}
.sub{color:var(--muted);margin:6px 0 0;max-width:68ch}
.eyebrow{font-size:11.5px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.muted{color:var(--muted)} .small{font-size:13px}
.head{display:flex;flex-wrap:wrap;gap:16px;align-items:flex-end;justify-content:space-between;margin-bottom:22px}
.stats{display:flex;gap:18px;flex-wrap:wrap}
.stat b{display:block;font-size:22px;font-variant-numeric:tabular-nums;line-height:1.1} .stat span{font-size:12px;color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.btn{font:inherit;font-size:14px;font-weight:600;padding:8px 14px;border-radius:9px;border:1px solid var(--line);
background:var(--surface);color:var(--ink);cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;gap:6px}
.btn:hover{border-color:var(--muted)} .btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.danger{color:var(--bad)} .btn[disabled]{opacity:.5;cursor:not-allowed}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pill{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:700;padding:2px 9px;border-radius:999px}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.pill.good{color:var(--good);background:var(--good-soft)} .pill.bad{color:var(--bad);background:var(--bad-soft)}
.pill.warn{color:var(--warn);background:var(--warn-soft)} .pill.off{color:var(--muted);background:var(--sunken)}
.pill.info{color:var(--accent);background:var(--accent-soft)}
.banner{padding:12px 16px;border-radius:12px;margin:0 0 18px;font-size:14px;border:1px solid}
.banner.ok{border-color:var(--good);color:var(--good);background:var(--good-soft)}
.banner.bad{border-color:var(--bad);color:var(--bad);background:var(--bad-soft)}
.banner.info{border-color:var(--line);color:var(--ink);background:var(--sunken)}
/* logos */
.mark{display:grid;place-items:center;border-radius:14px;color:#fff;flex:none}
.mark svg{width:56%;height:56%;fill:currentColor} .mark .mono-l{font-weight:800;letter-spacing:-.02em}
/* switch */
.switch{position:relative;width:44px;height:26px;border-radius:999px;border:0;background:var(--line);cursor:pointer;
flex:none;transition:background .2s}
.switch::after{content:"";position:absolute;top:3px;left:3px;width:20px;height:20px;border-radius:50%;background:#fff;
box-shadow:0 1px 3px rgba(0,0,0,.25);transition:transform .2s}
.switch[aria-checked="true"]{background:var(--good)} .switch[aria-checked="true"]::after{transform:translateX(18px)}
.switch[disabled]{opacity:.5;cursor:not-allowed}
.toggle-row{display:flex;align-items:center;gap:12px}
/* fields */
.field{display:grid;gap:6px;margin:0 0 16px} .field label{font-size:13.5px;font-weight:600}
.field .help{font-size:12.5px;color:var(--muted)}
input[type=text],input[type=url],input[type=password],input[type=email],input[type=number],textarea{width:100%;font:inherit;
font-size:14px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--ink)}
input:disabled,textarea:disabled{background:var(--sunken);color:var(--muted)}
.source{font-size:11.5px;font-weight:700;padding:1px 7px;border-radius:6px;margin-left:6px;vertical-align:1px}
.source.console{color:var(--good);background:var(--good-soft)} .source.env{color:var(--warn);background:var(--warn-soft)}
.source.none{color:var(--muted);background:var(--sunken)}
.chips{display:flex;flex-wrap:wrap;gap:6px;padding:6px;border:1px solid var(--line);border-radius:9px;background:var(--surface);
min-height:44px;align-items:center}
.chips input{border:0!important;padding:6px!important;flex:1 1 180px;min-width:140px;background:transparent!important}
.chips input:focus{outline:none} .chips:focus-within{outline:2px solid var(--accent);outline-offset:1px}
.chip{display:inline-flex;align-items:center;gap:4px;background:var(--accent-soft);color:var(--ink);border-radius:7px;
padding:3px 4px 3px 9px;font-size:13px}
.chip button{border:0;background:transparent;color:var(--muted);cursor:pointer;font-size:15px;line-height:1;padding:0 4px}
.chip.bad{background:var(--bad-soft);color:var(--bad)}
/* adapter */
.adapter{display:flex;align-items:center;gap:0;padding:18px 20px}
.adapter .node{display:grid;justify-items:center;gap:6px;font-size:12px;font-weight:600;color:var(--muted);width:92px}
.adapter .cable{flex:1;position:relative;height:56px;min-width:90px}
.adapter .cable svg{position:absolute;inset:0;width:100%;height:100%;overflow:visible}
.cable .wire{stroke:var(--line);stroke-width:3;fill:none;stroke-dasharray:6 8;stroke-linecap:round}
.cable .pulse{fill:var(--accent);opacity:0}
.cable .plug{fill:var(--surface);stroke:var(--line);stroke-width:2}
.adapter .state{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:var(--surface);
border:1px solid var(--line);border-radius:999px;padding:3px 12px;font-size:12px;font-weight:700;color:var(--muted);white-space:nowrap}
.adapter[data-state="testing"] .wire{stroke:var(--accent);animation:flow .6s linear infinite}
.adapter[data-state="testing"] .pulse{opacity:1;animation:travel 1.1s ease-in-out infinite}
.adapter[data-state="testing"] .state{color:var(--accent)}
.adapter[data-state="ok"] .wire{stroke:var(--good);stroke-dasharray:none}
.adapter[data-state="ok"] .plug{stroke:var(--good);fill:var(--good-soft)}
.adapter[data-state="ok"] .state{color:var(--good);border-color:var(--good);animation:pop .35s ease-out}
.adapter[data-state="fail"] .wire{stroke:var(--bad);stroke-dasharray:40 18}
.adapter[data-state="fail"] .plug{stroke:var(--bad)}
.adapter[data-state="fail"] .state{color:var(--bad);border-color:var(--bad);animation:shake .35s}
.adapter[data-state="paused"] .wire{stroke:var(--line)} .adapter[data-state="paused"] .state{color:var(--muted)}
@keyframes flow{to{stroke-dashoffset:-28}}
@keyframes travel{0%{transform:translateX(0)}100%{transform:translateX(var(--run,200px))}}
@keyframes pop{0%{transform:translate(-50%,-50%) scale(.8)}70%{transform:translate(-50%,-50%) scale(1.08)}
100%{transform:translate(-50%,-50%) scale(1)}}
@keyframes shake{0%,100%{transform:translate(-50%,-50%)}25%{transform:translate(calc(-50% - 5px),-50%)}
75%{transform:translate(calc(-50% + 5px),-50%)}}
@keyframes rise{from{transform:translateY(8px)}to{transform:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media (max-width:640px){.top .in{gap:10px}.brand span{display:none}.adapter .node{width:70px}}
"""

JS = r"""
document.querySelectorAll('[data-switch]').forEach(sw=>{
  const input=document.getElementById(sw.dataset.switch);
  sw.addEventListener('click',()=>{if(sw.disabled)return;const on=sw.getAttribute('aria-checked')!=='true';
    sw.setAttribute('aria-checked',on);if(input)input.value=on?'on':'';sw.dispatchEvent(new Event('change',{bubbles:true}));});
});
const EMAIL=/^[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+$/;
document.querySelectorAll('[data-chips]').forEach(box=>{
  const hidden=document.getElementById(box.dataset.chips), entry=box.querySelector('input');
  const values=()=>hidden.value.split(',').map(s=>s.trim()).filter(Boolean);
  const draw=()=>{box.querySelectorAll('.chip').forEach(c=>c.remove());
    values().forEach(v=>{const c=document.createElement('span');c.className='chip'+(EMAIL.test(v)?'':' bad');
      c.textContent=v;const x=document.createElement('button');x.type='button';x.textContent='×';
      x.setAttribute('aria-label','remove '+v);x.onclick=()=>{hidden.value=values().filter(e=>e!==v).join(', ');draw();
      box.dispatchEvent(new Event('change',{bubbles:true}));};c.appendChild(x);box.insertBefore(c,entry);});};
  const add=()=>{const parts=entry.value.split(/[,;\s]+/).map(s=>s.trim()).filter(Boolean);if(!parts.length)return;
    hidden.value=[...new Set([...values(),...parts])].join(', ');entry.value='';draw();
    box.dispatchEvent(new Event('change',{bubbles:true}));};
  entry.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===','){e.preventDefault();add();}
    else if(e.key==='Backspace'&&!entry.value){const v=values();v.pop();hidden.value=v.join(', ');draw();}});
  entry.addEventListener('blur',add);box.addEventListener('click',e=>{if(e.target===box)entry.focus();});
  entry.form&&entry.form.addEventListener('submit',add);draw();
});
function sizeCables(){document.querySelectorAll('.cable').forEach(c=>c.style.setProperty('--run',(c.clientWidth-16)+'px'));}
sizeCables();addEventListener('resize',sizeCables);
function setAdapter(el,state,text){el.dataset.state=state;el.querySelector('.state').textContent=text;}
async function postForm(url,form){const r=await fetch(url,{method:'POST',body:new URLSearchParams(new FormData(form)),
  headers:{'Accept':'application/json'}});return r.json();}
"""

COPILOT_MARK = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2 4 5v6c0 5 3.4 9.3 8 11 4.6-1.7 8-6 8-11V5l-8-3Z" '
    'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><path d="M7 12.5h2.6l1.5-3 2 6 '
    '1.5-3H17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>'
)


def mark(spec, size: int = 48) -> str:
    """The platform's brand mark on its colour (monogram when the brand has no free mark)."""
    path = LOGOS.get(spec.logo or "")
    inner = (
        f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{path}"/></svg>'
        if path
        else f'<span class="mono-l" style="font-size:{size * 0.36:.0f}px">'
        f"{escape(''.join(w[0] for w in spec.label.split()[:2]).upper())}</span>"
    )
    radius = max(8, size // 4)
    return (
        f'<span class="mark" style="width:{size}px;height:{size}px;border-radius:{radius}px;background:{spec.color};'
        f'view-transition-name:mark-{escape(spec.platform)}" role="img" aria-label="{escape(spec.label)}">{inner}</span>'
    )


def copilot_mark(size: int = 48) -> str:
    return (
        f'<span class="mark" style="width:{size}px;height:{size}px;border-radius:{max(8, size // 4)}px;'
        f'background:var(--ink);color:var(--bg)" role="img" aria-label="Pipeline Incident Copilot">{COPILOT_MARK}</span>'
    )


def adapter(spec, state: str = "idle", text: str = "not connected yet") -> str:
    return (
        f'<div class="adapter" id="adapter" data-state="{escape(state)}">'
        f'<div class="node">{mark(spec, 56)}<span>{escape(spec.label)}</span></div>'
        '<div class="cable"><svg preserveAspectRatio="none" aria-hidden="true">'
        '<line class="wire" x1="0" y1="50%" x2="100%" y2="50%"/>'
        '<circle class="pulse" cx="8" cy="50%" r="5"/>'
        '<circle class="plug" cx="2" cy="50%" r="6"/><circle class="plug" cx="100%" cy="50%" r="6"/></svg>'
        f'<span class="state" role="status" aria-live="polite">{escape(text)}</span></div>'
        f'<div class="node">{copilot_mark(56)}<span>Copilot</span></div></div>'
    )


def switch(input_id: str, name: str, on: bool, label: str, disabled: bool = False) -> str:
    return (
        f'<input type="hidden" id="{input_id}" name="{escape(name)}" value="{"on" if on else ""}">'
        f'<button type="button" class="switch" role="switch" aria-checked="{"true" if on else "false"}" '
        f'data-switch="{input_id}" aria-label="{escape(label)}"{" disabled" if disabled else ""}></button>'
    )


def chips(input_id: str, name: str, values: list[str], placeholder: str, disabled: bool = False) -> str:
    return (
        f'<input type="hidden" id="{input_id}" name="{escape(name)}" value="{escape(", ".join(values), quote=True)}">'
        f'<div class="chips" data-chips="{input_id}"><input type="email" aria-label="{escape(placeholder)}" '
        f'placeholder="{escape(placeholder, quote=True)}"{" disabled" if disabled else ""}></div>'
    )


def frame(title: str, body: str, active: str, extra_js: str = "") -> str:
    nav = "".join(
        f'<a href="{href}"{" aria-current=page" if key == active else ""}>{label}</a>'
        for key, href, label in (
            ("incidents", "/", "Incidents"),
            ("connections", "/connections", "Connections"),
            ("knowledge", "/knowledge", "Knowledge"),
            ("settings", "/settings", "Settings"),
        )
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Pipeline Incident Copilot</title>
<style>{CSS}</style></head><body>
<header class="top"><div class="in"><a class="brand" href="/">{copilot_mark(28)}<span>Pipeline Incident Copilot</span></a>
<nav class="nav" aria-label="Console">{nav}</nav></div></header>
<main class="wrap">{body}</main><script>{JS}{extra_js}</script></body></html>"""
