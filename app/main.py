"""Puente de archivos — pasa archivos y texto entre dos máquinas sin copy-paste compartido.

Ambos lados solo necesitan llegar al servidor por HTTP(S):
  - Lado solo-navegador: sube arrastrando a la página, baja con un clic.
  - Lado local: además un watcher (sync/drop-sync.py) o la app de escritorio sincronizan solos.

Protegido con un token (DROP_TOKEN). Los archivos viejos se borran solos.
"""
import hmac
import json
import mimetypes
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

DATA_DIR = Path(os.environ.get("DROP_DATA_DIR", "/data"))
FILES_DIR = DATA_DIR / "files"
FILES_DIR.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get("DROP_TOKEN", "")
RETENTION_DAYS = int(os.environ.get("DROP_RETENTION_DAYS", "7"))
MAX_BYTES = int(os.environ.get("DROP_MAX_MB", "2048")) * 1024 * 1024
MAX_TEXT = 100_000  # caracteres por clip de texto

app = FastAPI(title="Puente")


# ----------------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------------
def _presented_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get("drop_token") or request.query_params.get("token", "")


def require_token(request: Request) -> None:
    if not TOKEN:
        raise HTTPException(500, "El servidor no tiene DROP_TOKEN configurado.")
    if not hmac.compare_digest(_presented_token(request), TOKEN):
        raise HTTPException(401, "Token inválido.")


# ----------------------------------------------------------------------------
# Almacenamiento: cada archivo es <id>.bin + <id>.json (metadatos) en FILES_DIR
# ----------------------------------------------------------------------------
def _meta_path(fid: str) -> Path:
    return FILES_DIR / f"{fid}.json"


def _blob_path(fid: str) -> Path:
    return FILES_DIR / f"{fid}.bin"


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "").strip() or "archivo"
    return re.sub(r'[\r\n\t"/\\]', "_", name)[:200]


def _load_meta(fid: str) -> dict | None:
    mp = _meta_path(fid)
    if not mp.exists():
        return None
    try:
        return json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _list_meta() -> list[dict]:
    out = []
    for mp in FILES_DIR.glob("*.json"):
        try:
            out.append(json.loads(mp.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda m: m.get("uploaded_at", 0))
    return out


def _delete(fid: str) -> None:
    for p in (_meta_path(fid), _blob_path(fid)):
        try:
            p.unlink()
        except OSError:
            pass


def sweep_expired() -> int:
    if RETENTION_DAYS <= 0:
        return 0
    cutoff = time.time() - RETENTION_DAYS * 86400
    n = 0
    for m in _list_meta():
        if m.get("uploaded_at", 0) < cutoff:
            _delete(m["id"])
            n += 1
    return n


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...), source: str = Form("web")):
    require_token(request)
    fid = uuid.uuid4().hex
    blob = _blob_path(fid)
    size = 0
    try:
        with blob.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    out.close()
                    blob.unlink(missing_ok=True)
                    raise HTTPException(413, f"Archivo supera el límite de {MAX_BYTES // 1024 // 1024} MB.")
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as e:
        blob.unlink(missing_ok=True)
        raise HTTPException(500, f"No se pudo guardar: {e}")

    meta = {
        "id": fid,
        "kind": "file",
        "name": _safe_name(file.filename),
        "size": size,
        "source": "local" if source == "local" else "web",
        "uploaded_at": time.time(),
    }
    _meta_path(fid).write_text(json.dumps(meta), encoding="utf-8")
    return meta


@app.post("/api/clip")
async def clip(request: Request, text: str = Form(...), source: str = Form("web")):
    require_token(request)
    text = text[:MAX_TEXT]
    if not text.strip():
        raise HTTPException(400, "Texto vacío.")
    fid = uuid.uuid4().hex
    first = text.strip().splitlines()[0][:48] if text.strip() else "texto"
    meta = {
        "id": fid,
        "kind": "text",
        "name": first,
        "text": text,
        "size": len(text.encode("utf-8")),
        "source": "local" if source == "local" else "web",
        "uploaded_at": time.time(),
    }
    _meta_path(fid).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


@app.get("/api/files")
def files(request: Request):
    require_token(request)
    sweep_expired()
    return {"files": _list_meta(), "retention_days": RETENTION_DAYS}


@app.get("/api/files/{fid}")
def download(request: Request, fid: str, inline: int = 0):
    require_token(request)
    meta = _load_meta(fid)
    blob = _blob_path(fid)
    if not meta or not blob.exists():
        raise HTTPException(404, "No existe.")
    if inline:  # abrir en el navegador (vista previa) en vez de descargar
        ctype = mimetypes.guess_type(meta["name"])[0] or "application/octet-stream"
        return FileResponse(blob, filename=meta["name"], media_type=ctype,
                            content_disposition_type="inline")
    return FileResponse(blob, filename=meta["name"], media_type="application/octet-stream")


@app.delete("/api/files/{fid}")
def delete(request: Request, fid: str):
    require_token(request)
    if not _load_meta(fid):
        raise HTTPException(404, "No existe.")
    _delete(fid)
    return {"ok": True}


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


# ----------------------------------------------------------------------------
# UI (la página es un cascarón; los datos los pide con el token por fetch)
# ----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Puente</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 760px; margin: 0 auto; padding: 24px 16px; line-height: 1.5; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .muted { opacity: .65; font-size: .85rem; }
  #drop { border: 2px dashed currentColor; border-radius: 12px; padding: 40px 16px;
          text-align: center; opacity: .55; cursor: pointer; margin: 20px 0;
          transition: opacity .15s, background .15s; }
  #drop.hot { opacity: 1; background: rgba(125,125,125,.12); }
  table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  td, th { text-align: left; padding: 8px 6px; border-bottom: 1px solid rgba(125,125,125,.25); font-size: .9rem; }
  th { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; opacity: .6; }
  a.dl { text-decoration: none; font-weight: 600; }
  .tag { font-size: .68rem; padding: 1px 7px; border-radius: 999px; border: 1px solid rgba(125,125,125,.4); opacity: .8; }
  button { font: inherit; cursor: pointer; border: 1px solid rgba(125,125,125,.4);
           background: transparent; color: inherit; border-radius: 8px; padding: 4px 10px; }
  .row-act { opacity: .6; }
  #login { display: flex; gap: 8px; margin: 16px 0; }
  #login input { flex: 1; font: inherit; padding: 8px 10px; border-radius: 8px;
                 border: 1px solid rgba(125,125,125,.4); background: transparent; color: inherit; }
  #err { color: #c0392b; font-size: .85rem; min-height: 1.2em; }
  #toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 4px 0 2px; }
  #toolbar input, #toolbar select { font: inherit; padding: 6px 9px; border-radius: 8px;
     border: 1px solid rgba(125,125,125,.4); background: transparent; color: inherit; }
  #search { flex: 1; min-width: 160px; }
  #cliprow { display: flex; gap: 8px; align-items: stretch; margin: 0 0 10px; }
  #cliptext { flex: 1; font: inherit; padding: 8px 10px; border-radius: 8px; resize: vertical;
     border: 1px solid rgba(125,125,125,.4); background: transparent; color: inherit; }
  #toolbar label { font-size: .72rem; text-transform: uppercase; letter-spacing: .04em; opacity: .55; }
  .grp { margin: 18px 0 2px; font-size: .78rem; font-weight: 700; text-transform: uppercase;
         letter-spacing: .04em; opacity: .7; display: flex; justify-content: space-between; }
  .grp .cnt { font-weight: 400; opacity: .6; }
  .type-ic { display: inline-block; width: 1.4em; text-align: center; margin-right: 6px; opacity: .85; }
  .empty { opacity: .55; padding: 24px 6px; text-align: center; }
  .hidden { display: none; }
</style>
</head>
<body>
  <h1>Puente de archivos</h1>
  <div class="muted" id="sub">Pasá archivos entre el VDI y tu máquina.</div>

  <div id="login">
    <input id="tok" type="password" placeholder="Token" autocomplete="current-password">
    <button id="enter">Entrar</button>
  </div>
  <div id="err"></div>

  <div id="main" class="hidden">
    <div id="drop">Arrastrá archivos acá, o hacé clic para elegir
      <input id="picker" type="file" multiple class="hidden">
    </div>

    <div id="cliprow">
      <textarea id="cliptext" rows="2" placeholder="Compartir texto…  (o pegá una imagen con Ctrl+V en cualquier parte de la página)"></textarea>
      <button id="sendtext">Enviar texto</button>
    </div>

    <div id="toolbar">
      <input id="search" type="search" placeholder="Buscar por nombre…">
      <label>Agrupar</label>
      <select id="groupby">
        <option value="date">Fecha</option>
        <option value="type">Tipo</option>
        <option value="source">Origen</option>
        <option value="none">Sin agrupar</option>
      </select>
      <label>Orden</label>
      <select id="sortby">
        <option value="new">Recientes</option>
        <option value="old">Antiguos</option>
        <option value="name">Nombre</option>
        <option value="size">Tamaño</option>
      </select>
    </div>

    <div id="groups"></div>
    <p class="muted" id="foot"></p>
  </div>

<script>
const $ = s => document.querySelector(s);
let token = localStorage.getItem("drop_token") || "";

function setCookie(t) { document.cookie = "drop_token=" + encodeURIComponent(t) + ";path=/;max-age=2592000;samesite=strict"; }
function hdr() { return { "Authorization": "Bearer " + token }; }
function fmtSize(b) { const u=["B","KB","MB","GB"]; let i=0; while(b>=1024&&i<3){b/=1024;i++;} return b.toFixed(b<10&&i>0?1:0)+" "+u[i]; }
function esc(s){ const d=document.createElement("div"); d.textContent=s; return d.innerHTML; }

let allFiles = [], retentionDays = 7;

const TYPES = [
  { cat:"Imágenes",        ic:"🖼️", ext:["jpg","jpeg","png","gif","bmp","webp","svg","heic","tif","tiff"] },
  { cat:"Documentos",      ic:"📄", ext:["pdf","doc","docx","txt","rtf","odt","md","pages"] },
  { cat:"Hojas de cálculo",ic:"📊", ext:["xls","xlsx","csv","tsv","ods"] },
  { cat:"Presentaciones",  ic:"📽️", ext:["ppt","pptx","odp","key"] },
  { cat:"Comprimidos",     ic:"🗜️", ext:["zip","rar","7z","tar","gz","bz2","xz"] },
  { cat:"Código",          ic:"💻", ext:["js","ts","py","cs","java","sql","json","xml","html","css","sh","ps1","bat","yml","yaml","cshtml","vue","go","rb","php"] },
  { cat:"Audio",           ic:"🎵", ext:["mp3","wav","flac","m4a","ogg","aac"] },
  { cat:"Video",           ic:"🎬", ext:["mp4","mov","avi","mkv","webm","wmv"] },
];
function extOf(name){ const i=name.lastIndexOf("."); return i>0 ? name.slice(i+1).toLowerCase() : ""; }
function typeOf(name){ const e=extOf(name); return TYPES.find(t=>t.ext.includes(e)) || { cat:"Otros", ic:"📦" }; }
function typeOfItem(f){ return f.kind==="text" ? { cat:"Texto", ic:"📝" } : typeOf(f.name); }

function dateBucket(ts){
  const d=new Date(ts*1000), now=new Date();
  const day=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate());
  const diff=Math.round((day(now)-day(d))/86400000);
  if(diff<=0) return {k:"0",label:"Hoy"};
  if(diff===1) return {k:"1",label:"Ayer"};
  if(diff<7)  return {k:"2",label:"Últimos 7 días"};
  if(diff<30) return {k:"3",label:"Este mes"};
  return {k:"4",label:"Más viejo"};
}
function fmtDate(ts){
  const d=new Date(ts*1000);
  return d.toLocaleDateString("es-UY")+" "+d.toLocaleTimeString("es-UY",{hour:"2-digit",minute:"2-digit"});
}

function render(){
  const q = $("#search").value.trim().toLowerCase();
  const groupBy = $("#groupby").value, sortBy = $("#sortby").value;
  localStorage.setItem("drop_group", groupBy); localStorage.setItem("drop_sort", sortBy);

  let files = allFiles.filter(f => !q || f.name.toLowerCase().includes(q));
  const sorters = {
    new:  (a,b)=> b.uploaded_at - a.uploaded_at,
    old:  (a,b)=> a.uploaded_at - b.uploaded_at,
    name: (a,b)=> a.name.localeCompare(b.name,"es"),
    size: (a,b)=> b.size - a.size,
  };
  files.sort(sorters[sortBy]);

  // armar grupos
  let groups; // [{key, label, items}]
  if(groupBy==="none"){
    groups = [{key:"all", label:"", items:files}];
  } else if(groupBy==="date"){
    const m=new Map();
    for(const f of files){ const b=dateBucket(f.uploaded_at); if(!m.has(b.k)) m.set(b.k,{key:b.k,label:b.label,items:[]}); m.get(b.k).items.push(f); }
    groups=[...m.values()].sort((a,b)=>a.key.localeCompare(b.key));
  } else if(groupBy==="type"){
    const m=new Map();
    for(const f of files){ const t=typeOfItem(f); if(!m.has(t.cat)) m.set(t.cat,{key:t.cat,label:t.ic+" "+t.cat,items:[]}); m.get(t.cat).items.push(f); }
    groups=[...m.values()].sort((a,b)=>a.key.localeCompare(b.key,"es"));
  } else { // source
    const m=new Map([["web",{key:"web",label:"Desde el VDI / web",items:[]}],["local",{key:"local",label:"Desde el local",items:[]}]]);
    for(const f of files){ (m.get(f.source==="local"?"local":"web")).items.push(f); }
    groups=[...m.values()].filter(g=>g.items.length);
  }

  const box=$("#groups"); box.innerHTML="";
  if(!files.length){ box.innerHTML=`<div class="empty">${allFiles.length?"Nada coincide con la búsqueda.":"No hay archivos todavía."}</div>`; }
  for(const g of groups){
    if(g.label){
      const h=document.createElement("div"); h.className="grp";
      h.innerHTML=`<span>${esc(g.label)}</span><span class="cnt">${g.items.length}</span>`;
      box.appendChild(h);
    }
    const table=document.createElement("table");
    table.innerHTML="<tbody></tbody>";
    const tb=table.querySelector("tbody");
    for(const f of g.items){
      const t=typeOfItem(f), isText=f.kind==="text";
      const nameCell = isText
        ? `<span class="type-ic">${t.ic}</span><span title="${esc(f.text||"")}">${esc(f.name)}</span>`
        : `<span class="type-ic">${t.ic}</span><a class="dl" href="/api/files/${f.id}?token=${encodeURIComponent(token)}">${esc(f.name)}</a>`;
      const actions = isText
        ? `<button class="copy">copiar</button> <button class="del">borrar</button>`
        : `<button class="open">abrir</button> <button class="del">borrar</button>`;
      const tr=document.createElement("tr");
      tr.innerHTML=`<td>${nameCell}</td>
        <td class="muted" style="white-space:nowrap">${fmtSize(f.size)}</td>
        <td class="muted" style="white-space:nowrap" title="${fmtDate(f.uploaded_at)}">${fmtDate(f.uploaded_at)}</td>
        <td><span class="tag">${f.source==="local"?"local":"VDI/web"}</span></td>
        <td class="row-act" style="white-space:nowrap">${actions}</td>`;
      if(isText){
        tr.querySelector(".copy").onclick=async(e)=>{
          try{ await navigator.clipboard.writeText(f.text||""); e.target.textContent="copiado ✓"; setTimeout(()=>e.target.textContent="copiar",1200); }
          catch{ e.target.textContent="error"; }
        };
      } else {
        tr.querySelector(".open").onclick=()=> window.open(`/api/files/${f.id}?inline=1&token=${encodeURIComponent(token)}`,"_blank");
      }
      tr.querySelector(".del").onclick=async()=>{ await fetch("/api/files/"+f.id,{method:"DELETE",headers:hdr()}); refresh(); };
      tb.appendChild(tr);
    }
    box.appendChild(table);
  }
  $("#foot").textContent = allFiles.length + " archivo(s)" + (q?` · ${files.length} en el filtro`:"") + " · se borran a los " + retentionDays + " días";
}

async function refresh() {
  const r = await fetch("/api/files", { headers: hdr() });
  if (r.status === 401) { logout("Token inválido."); return; }
  const data = await r.json();
  allFiles = data.files; retentionDays = data.retention_days;
  render();
}

async function upload(files) {
  for (const file of files) {
    const fd = new FormData(); fd.append("file", file); fd.append("source", "web");
    await fetch("/api/upload", { method: "POST", headers: hdr(), body: fd });
  }
  refresh();
}

function login() {
  token = $("#tok").value.trim();
  if (!token) return;
  localStorage.setItem("drop_token", token); setCookie(token);
  fetch("/api/files", { headers: hdr() }).then(r => {
    if (r.ok) { $("#login").classList.add("hidden"); $("#main").classList.remove("hidden"); $("#err").textContent=""; refresh(); }
    else logout("Token inválido.");
  });
}
function logout(msg) {
  token=""; localStorage.removeItem("drop_token"); setCookie("");
  $("#main").classList.add("hidden"); $("#login").classList.remove("hidden"); $("#err").textContent = msg || "";
}

$("#enter").onclick = login;
$("#tok").addEventListener("keydown", e => { if (e.key === "Enter") login(); });

$("#groupby").value = localStorage.getItem("drop_group") || "date";
$("#sortby").value  = localStorage.getItem("drop_sort")  || "new";
$("#search").addEventListener("input", render);
$("#groupby").addEventListener("change", render);
$("#sortby").addEventListener("change", render);

const drop = $("#drop"), picker = $("#picker");
drop.onclick = () => picker.click();
picker.onchange = () => { upload(picker.files); picker.value=""; };
["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("hot"); }));
["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", e => { if (e.dataTransfer.files.length) upload(e.dataTransfer.files); });

async function sendText(){
  const t = $("#cliptext").value;
  if(!t.trim()) return;
  const fd = new FormData(); fd.append("text", t); fd.append("source", "web");
  await fetch("/api/clip", { method:"POST", headers: hdr(), body: fd });
  $("#cliptext").value = ""; refresh();
}
$("#sendtext").onclick = sendText;
$("#cliptext").addEventListener("keydown", e => { if(e.key==="Enter" && (e.ctrlKey||e.metaKey)){ e.preventDefault(); sendText(); }});

// pegar una imagen del portapapeles en cualquier parte -> se sube como archivo
document.addEventListener("paste", e => {
  if(!token || $("#main").classList.contains("hidden")) return;
  const items = e.clipboardData && e.clipboardData.items;
  if(!items) return;
  for(const it of items){
    if(it.type && it.type.startsWith("image/")){
      const blob = it.getAsFile();
      const ext = (it.type.split("/")[1]||"png").replace("jpeg","jpg");
      upload([ new File([blob], `captura-${Date.now()}.${ext}`, {type: it.type}) ]);
      e.preventDefault();
    }
  }
});

if (token) login();
setInterval(() => { if (token && !$("#main").classList.contains("hidden")) refresh(); }, 5000);
</script>
</body>
</html>"""
