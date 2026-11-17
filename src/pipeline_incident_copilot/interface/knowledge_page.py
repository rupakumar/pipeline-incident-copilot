# ruff: noqa: E501  (HTML, CSS and JS templates read better unwrapped)
"""The console's Knowledge page (http://localhost:8787/knowledge): teach the copilot about the product.

GET  /knowledge                 upload area, the library of uploaded documents, "ask the knowledge base"
POST /knowledge/upload          raw file body (X-Form-Key, X-File-Name headers; type/title/failure_class in the query)
                                → JSON: the indexed document, or why it was refused
POST /knowledge/search          JSON: what the knowledge agent would find for a question
POST /knowledge/<id>/delete     remove a document from the library and the index

Documents go through KnowledgeLibrary (convert, mask secrets, refuse prompt injection, chunk, embed, index).
"""

import hmac
import json
from html import escape
from urllib.parse import unquote

from pipeline_incident_copilot.interface.console_ui import frame
from pipeline_incident_copilot.rag.ingestion.document_converter import MAX_BYTES, UnsupportedDocument
from pipeline_incident_copilot.rag.ingestion.knowledge_library import (
    DOC_TYPES,
    FAILURE_CLASSES,
    KnowledgeLibrary,
    RefusedDocument,
)

JSON = "application/json"
TYPE_HELP = {
    "architecture": "System design, data flows, dependencies. Helps the copilot see what a failure affects.",
    "runbook": "Step-by-step fixes your team follows. Gets a reserved place in every diagnosis.",
    "product": "How the product and its platforms behave, limits, configuration.",
    "postmortem": "Past incident write-ups: symptoms, cause, what fixed it.",
}
TYPE_ICON = {
    "architecture": '<path d="M4 5h16v5H4zM4 14h7v5H4zM13 14h7v5h-7z"/>',
    "runbook": '<path d="M6 3h10l4 4v14H6zM9 9h7M9 13h7M9 17h4"/>',
    "product": '<path d="M12 3 3 8l9 5 9-5-9-5ZM3 13l9 5 9-5"/>',
    "postmortem": '<path d="M12 3a9 9 0 1 0 9 9M12 7v5l3 2M17 3v4h4"/>',
}


def _kb(n: int) -> str:
    return f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class KnowledgePage:
    def __init__(self, library: KnowledgeLibrary, form_key: str):
        self.library = library
        self.form_key = form_key

    def get(self, path: str, query: dict[str, list[str]]):
        if path.rstrip("/") == "/knowledge":
            return 200, self.render()
        return 404, frame("Not found", '<div class="banner bad">Not found.</div>', "knowledge")

    def upload(self, headers, query: dict[str, list[str]], data: bytes):
        if not hmac.compare_digest(headers.get("X-Form-Key", ""), self.form_key):
            return 403, json.dumps({"ok": False, "message": "the page expired; reload it"}), JSON
        q = {k: v[0] for k, v in query.items()}
        try:
            doc = self.library.add(
                unquote(headers.get("X-File-Name", "document")),
                data,
                q.get("type", ""),
                q.get("title", ""),
                q.get("failure_class", ""),
                by="console",
            )
        except (UnsupportedDocument, RefusedDocument) as e:
            return 200, json.dumps({"ok": False, "message": str(e)}), JSON
        return 200, json.dumps({"ok": True, "document": doc}), JSON

    def post(self, path: str, form: dict[str, list[str]]):
        value = {k: v[0] for k, v in form.items()}
        if not hmac.compare_digest(value.get("form_key", ""), self.form_key):
            return 403, frame("Form expired", '<div class="banner bad">Reload the page and try again.</div>', "")
        if path == "/knowledge/search":
            question = value.get("q", "").strip()[:500]
            return 200, json.dumps({"results": self.library.search(question) if question else []}), JSON
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[2] == "delete" and self.library.get(parts[1]):
            self.library.remove(parts[1])
            return 303, "/knowledge"
        return 404, frame("Not found", '<div class="banner bad">Not found.</div>', "knowledge")

    def _row(self, d: dict) -> str:
        label = DOC_TYPES[d["doc_type"]][2]
        fc = d["failure_class"].replace("_", " ") if d["failure_class"] else "—"
        key = f'<input type="hidden" name="form_key" value="{escape(self.form_key, quote=True)}">'
        return (
            f'<tr><td><b>{escape(d["title"])}</b><div class="small muted mono">{escape(d["filename"])}</div></td>'
            f'<td><span class="pill info type-{escape(d["doc_type"])}">{escape(label)}</span></td><td>{escape(fc)}</td>'
            f'<td class="num">{d["sections"]}</td><td class="num">{d["chunks"]}</td><td class="num">{_kb(d["bytes"])}</td>'
            f'<td class="small muted">{escape(d["uploaded_at"][:16].replace("T", " "))}</td>'
            f'<td><form method="post" action="/knowledge/{escape(d["doc_id"], quote=True)}/delete" '
            f"onsubmit=\"return confirm('Remove this document from the knowledge base?')\">{key}"
            '<button class="btn danger small">Remove</button></form></td></tr>'
        )

    def render(self) -> str:
        docs = self.library.all()
        types = "".join(
            f'<label class="dtype"><input type="radio" name="doctype" value="{key}"{" checked" if key == "architecture" else ""}>'
            f'<span class="d-icon"><svg viewBox="0 0 24 24" aria-hidden="true">{TYPE_ICON[key]}</svg></span>'
            f"<span><b>{escape(label)}</b><span class='small muted'>{escape(TYPE_HELP[key])}</span></span></label>"
            for key, (_, _, label) in DOC_TYPES.items()
        )
        classes = "".join(f'<option value="{c}">{c.replace("_", " ")}</option>' for c in FAILURE_CLASSES)
        rows = "".join(self._row(d) for d in docs) or (
            '<tr><td colspan="8" class="muted" style="padding:18px 8px">No company documents yet. Upload an '
            "architecture document or a runbook to start.</td></tr>"
        )
        chunks = sum(d["chunks"] for d in docs)
        body = f"""<style>{CSS}</style>
<div class="head"><div><div class="eyebrow">Knowledge</div><h1>Teach the copilot your product</h1>
<p class="sub">Upload architecture documents, runbooks and product docs. They are indexed into the knowledge base the
diagnosis searches, so tickets are resolved with your own documentation as evidence.</p></div>
<div class="stats"><div class="stat"><b id="n-docs">{len(docs)}</b><span>documents</span></div>
<div class="stat"><b id="n-chunks">{chunks}</b><span>searchable passages</span></div></div></div>

<div class="kgrid">
<section class="card up">
<h2>Upload</h2>
<div class="types" role="radiogroup" aria-label="Document type">{types}</div>
<div class="row opts"><div class="field" id="fc-field"><label for="fc">Failure class <span class="muted small">optional</span></label>
<select id="fc"><option value="">any</option>{classes}</select></div>
<div class="field" style="flex:1"><label for="ttl">Title <span class="muted small">optional, one file</span></label>
<input id="ttl" type="text" placeholder="taken from the document's first heading"></div></div>
<label class="drop" id="drop" for="files"><input id="files" type="file" multiple accept=".pdf,.docx,.md,.markdown,.txt,.html,.htm">
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3"/></svg>
<b>Drop files here or choose them</b><span class="small muted">PDF, Word, Markdown, text or HTML · up to {MAX_BYTES // 1024 // 1024} MB each</span></label>
<ul class="queue" id="queue" aria-live="polite"></ul>
</section>

<aside class="card how"><h2>What happens to a document</h2>
<ol class="flow">
<li><b>Read</b><span>PDF, Word or HTML converted to text; headings kept as sections.</span></li>
<li><b>Guard</b><span>Tokens and passwords masked. Text that tries to instruct the AI is refused.</span></li>
<li><b>Chunk</b><span>One passage per section, each starting with the document and section name.</span></li>
<li><b>Embed</b><span>Meaning (bge-small) and keywords (BM25), stored in the local Qdrant index.</span></li>
<li><b>Cite</b><span>The knowledge agent finds it for matching failures and cites it in the diagnosis.</span></li>
</ol>
<p class="small muted">Files stay on this Mac (data/runtime, never committed to git). Removing a document takes it out
of the index right away.</p></aside>
</div>

<section class="card ask"><div class="row" style="justify-content:space-between"><h2>Ask the knowledge base</h2>
<span class="small muted">What the knowledge agent would find for an error or a question</span></div>
<form id="ask" class="row" style="margin-top:12px"><input type="hidden" name="form_key" value="{escape(self.form_key, quote=True)}">
<input type="text" name="q" id="q" style="flex:1" placeholder="e.g. gold_aggregate ran out of memory after debug_sample_rows changed">
<button class="btn primary">Search</button></form><div id="answers" class="answers"></div></section>

<section class="card lib"><h2>Library</h2><div class="c-table"><table><thead><tr><th>Document</th><th>Type</th><th>Failure class</th>
<th class="num">Sections</th><th class="num">Passages</th><th class="num">Size</th><th>Uploaded (UTC)</th><th></th></tr></thead>
<tbody id="lib">{rows}</tbody></table></div></section>"""
        return frame("Knowledge", body, "knowledge", JS.replace("__KEY__", json.dumps(self.form_key)))


CSS = """
.kgrid{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;align-items:start}
.up,.how,.ask,.lib{padding:20px 22px} .ask,.lib{margin-top:16px}
.types{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0}
.dtype{display:flex;gap:12px;align-items:flex-start;padding:12px;border:1px solid var(--line);border-radius:12px;cursor:pointer;
transition:border-color .15s,background .15s} .dtype input{position:absolute;opacity:0;pointer-events:none}
.dtype b{display:block;font-size:14px} .dtype:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.dtype:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:2px}
.d-icon{display:grid;place-items:center;width:36px;height:36px;border-radius:10px;background:var(--sunken);flex:none}
.d-icon svg{width:20px;height:20px;fill:none;stroke:var(--accent);stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.opts{gap:14px;align-items:flex-end} .opts .field{margin:0}
select{font:inherit;font-size:14px;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:var(--surface);color:var(--ink)}
.drop{display:grid;justify-items:center;gap:6px;margin-top:16px;padding:30px 16px;border:2px dashed var(--line);border-radius:14px;
cursor:pointer;text-align:center;transition:border-color .15s,background .15s,transform .15s}
.drop input{position:absolute;opacity:0;width:1px;height:1px} .drop svg{width:34px;height:34px;fill:none;stroke:var(--accent);
stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round} .drop:hover,.drop.over{border-color:var(--accent);background:var(--accent-soft)}
.drop.over{transform:scale(1.01)} .drop:focus-within{outline:2px solid var(--accent);outline-offset:2px}
.queue{list-style:none;margin:14px 0 0;padding:0;display:grid;gap:10px} .queue:empty{display:none}
.q{border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.q-top{display:flex;gap:10px;align-items:center;justify-content:space-between} .q-name{font-weight:600;overflow-wrap:anywhere}
.stages{display:flex;gap:4px;margin:8px 0 4px} .stages i{flex:1;height:5px;border-radius:3px;background:var(--line);transition:background .3s}
.q[data-stage="1"] .stages i:nth-child(-n+1),.q[data-stage="2"] .stages i:nth-child(-n+2),.q[data-stage="3"] .stages i:nth-child(-n+3),
.q[data-stage="4"] .stages i:nth-child(-n+4),.q[data-stage="5"] .stages i{background:var(--accent)}
.q[data-stage="2"] .stages i:nth-child(3),.q[data-stage="3"] .stages i:nth-child(4){animation:blink 1s ease-in-out infinite}
.q.done .stages i{background:var(--good)} .q.bad .stages i{background:var(--bad)}
.q-msg{font-size:12.5px;color:var(--muted)} .q.bad .q-msg{color:var(--bad)} .q.done .q-msg{color:var(--good)}
@keyframes blink{50%{background:var(--accent-soft)}}
.flow{list-style:none;counter-reset:s;margin:14px 0;padding:0;display:grid;gap:0}
.flow li{counter-increment:s;display:grid;grid-template-columns:28px 1fr;column-gap:12px;position:relative;padding-bottom:14px}
.flow li::before{content:counter(s);display:grid;place-items:center;width:26px;height:26px;border-radius:50%;background:var(--accent-soft);
color:var(--accent);font-weight:700;font-size:12px;grid-row:1/3}
.flow li:not(:last-child)::after{content:"";position:absolute;left:13px;top:28px;bottom:2px;width:2px;background:var(--line)}
.flow b{font-size:14px} .flow span{font-size:13px;color:var(--muted)}
.answers{display:grid;gap:10px;margin-top:14px} .answers:empty{display:none}
.ans{border:1px solid var(--line);border-radius:12px;padding:10px 14px;animation:rise .3s ease-out both}
.ans.mine{border-color:var(--accent);background:var(--accent-soft)} .ans p{margin:6px 0 0;font-size:13px;color:var(--muted);white-space:pre-wrap}
.c-table{overflow-x:auto;margin-top:10px} table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{padding:8px;border-top:1px solid var(--line);text-align:left;vertical-align:top} th{font-size:11px;letter-spacing:.06em;
text-transform:uppercase;color:var(--muted)} .num{text-align:right;font-variant-numeric:tabular-nums} .btn.small{padding:5px 10px;font-size:13px}
td form{margin:0}
#ask{flex-wrap:nowrap} #ask input[type=text]{width:auto;min-width:0}
@media (max-width:860px){.kgrid{grid-template-columns:1fr}.types{grid-template-columns:1fr}}
"""

JS = r"""
const KEY=__KEY__, drop=document.getElementById('drop'), input=document.getElementById('files'), queue=document.getElementById('queue');
const fcField=document.getElementById('fc-field');
const type=()=>document.querySelector('input[name=doctype]:checked').value;
const syncFc=()=>{fcField.style.display=['runbook','postmortem'].includes(type())?'':'none';};
document.querySelectorAll('input[name=doctype]').forEach(r=>r.addEventListener('change',syncFc));syncFc();
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.add('over');}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{ev.preventDefault();drop.classList.remove('over');}));
drop.addEventListener('drop',ev=>send([...ev.dataTransfer.files]));
input.addEventListener('change',()=>{send([...input.files]);input.value='';});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function send(files){for(const f of files)await one(f,files.length===1);}
function one(file,single){return new Promise(resolve=>{
  const li=document.createElement('li');li.className='q';li.dataset.stage='1';
  li.innerHTML='<div class="q-top"><span class="q-name">'+esc(file.name)+'</span><span class="small muted">'+Math.max(1,Math.round(file.size/1024))+' KB</span></div><div class="stages"><i></i><i></i><i></i><i></i><i></i></div><div class="q-msg">Uploading…</div>';
  queue.prepend(li);const msg=li.querySelector('.q-msg');
  const q=new URLSearchParams({type:type(),failure_class:document.getElementById('fc').value,title:single?document.getElementById('ttl').value:''});
  const x=new XMLHttpRequest();x.open('POST','/knowledge/upload?'+q);x.setRequestHeader('X-Form-Key',KEY);
  x.setRequestHeader('X-File-Name',encodeURIComponent(file.name));x.setRequestHeader('Content-Type','application/octet-stream');
  x.upload.onprogress=e=>{if(e.lengthComputable)msg.textContent='Uploading… '+Math.round(e.loaded/e.total*100)+'%';};
  x.upload.onload=()=>{li.dataset.stage='2';msg.textContent='Reading and checking…';
    setTimeout(()=>{if(!li.classList.contains('done')&&!li.classList.contains('bad')){li.dataset.stage='3';msg.textContent='Chunking and embedding…';}},700);};
  x.onload=()=>{let d;try{d=JSON.parse(x.responseText);}catch(e){d={ok:false,message:'the console answered '+x.status};}
    if(d.ok){const doc=d.document;li.dataset.stage='5';li.classList.add('done');
      msg.textContent='Indexed: '+doc.sections+' sections, '+doc.chunks+' passages. The next diagnosis can cite it.';
      addRow(doc);}else{li.classList.add('bad');msg.textContent='Not indexed: '+d.message;}resolve();};
  x.onerror=()=>{li.classList.add('bad');msg.textContent='The console did not answer.';resolve();};x.send(file);});}
const TYPES={architecture:'Architecture & design',runbook:'Runbook / SOP',product:'Product documentation',postmortem:'Postmortem / past incident'};
function addRow(d){const tb=document.getElementById('lib');if(tb.querySelector('td[colspan]'))tb.innerHTML='';
  const tr=document.createElement('tr');tr.style.animation='rise .4s ease-out both';
  tr.innerHTML='<td><b>'+esc(d.title)+'</b><div class="small muted mono">'+esc(d.filename)+'</div></td><td><span class="pill info">'+TYPES[d.doc_type]+'</span></td><td>'+esc((d.failure_class||'—').replace('_',' '))+'</td><td class="num">'+d.sections+'</td><td class="num">'+d.chunks+'</td><td class="num">'+Math.max(1,Math.round(d.bytes/1024))+' KB</td><td class="small muted">just now</td><td><form method="post" action="/knowledge/'+encodeURIComponent(d.doc_id)+'/delete"><input type="hidden" name="form_key" value="'+esc(KEY)+'"><button class="btn danger small">Remove</button></form></td>';
  tb.prepend(tr);const n=document.getElementById('n-docs'),c=document.getElementById('n-chunks');n.textContent=+n.textContent+1;c.textContent=+c.textContent+d.chunks;}
document.getElementById('ask').addEventListener('submit',async e=>{e.preventDefault();const box=document.getElementById('answers');
  box.innerHTML='<p class="small muted">Searching…</p>';
  try{const d=await postForm('/knowledge/search',e.target);
    box.innerHTML=d.results.map(r=>'<div class="ans'+(r.uploaded?' mine':'')+'"><div class="row" style="justify-content:space-between"><b>'+esc(r.title)+'</b><span class="pill '+(r.uploaded?'good':'off')+'">'+(r.uploaded?'your document':esc(r.source_type.replace('_',' ')))+'</span></div><p>'+esc(r.text)+'</p></div>').join('')||'<p class="small muted">Nothing found.</p>';}
  catch(err){box.innerHTML='<p class="small" style="color:var(--bad)">Search failed: '+esc(err)+'</p>';}});
"""
