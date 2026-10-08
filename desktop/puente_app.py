r"""Puente — app de escritorio (local, Windows).

Vive en la bandeja del sistema. Arrastrá archivos a la ventana para mandarlos a la VDI;
lo que llega de la VDI se baja solo a inbox\. Botones Abrir / Mostrar en carpeta / Borrar,
y barra de progreso por transferencia.

Reemplaza al watcher headless (sync/drop-sync.py): hace el mismo sync, pero con GUI.

Dependencias:  pip install requests pystray Pillow tkinterdnd2
Arranque oculto:  pythonw puente_app.py
"""
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import requests
from PIL import Image, ImageDraw, ImageGrab
from tkinterdnd2 import DND_FILES, TkinterDnD
import pystray

TMP = Path(os.environ.get("TEMP", ".")) / "puente"

LOGP = Path(__file__).with_name("puente-app.log")


def logline(msg: str) -> None:
    try:
        with LOGP.open("a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


KEYS_FILE = Path.home() / ".keys"


def read_key(name: str, default: str = "") -> str:
    """Lee una variable de entorno o, si no está, de ~/.keys (CLAVE=valor)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    if KEYS_FILE.exists():
        for line in KEYS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{name}=") and "=" in line:
                return line.split("=", 1)[1].strip()
    return default


URL = read_key("DROP_URL", "http://localhost:8091")
BASE = Path(os.environ.get("DROP_BASE", str(Path.home() / "Downloads" / "puente")))
INBOX, OUTBOX, SENT = BASE / "inbox", BASE / "outbox", BASE / "outbox" / "_sent"
STATE_FILE = Path(__file__).resolve().parent.parent / "sync" / "state.json"
POLL = 3.0


def load_token() -> str:
    return read_key("DROP_TOKEN")


def human(b: float) -> str:
    u = ["B", "KB", "MB", "GB"]
    i = 0
    while b >= 1024 and i < 3:
        b /= 1024
        i += 1
    return f"{b:.1f} {u[i]}" if (b < 10 and i) else f"{b:.0f} {u[i]}"


def unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, i = path.stem, path.suffix, 1
    while (cand := path.with_name(f"{stem} ({i}){suf}")).exists():
        i += 1
    return cand


class ProgressReader:
    """Envoltorio de archivo que reporta cuánto se leyó (para progreso de subida)."""
    def __init__(self, path: Path, cb):
        self.f = open(path, "rb")
        self.total = path.stat().st_size
        self.read_n = 0
        self.cb = cb

    def read(self, size=-1):
        chunk = self.f.read(size)
        self.read_n += len(chunk)
        self.cb(self.read_n, self.total)
        return chunk

    def __len__(self):
        return self.total

    def close(self):
        self.f.close()


class Puente:
    def __init__(self):
        for d in (INBOX, OUTBOX, SENT):
            d.mkdir(parents=True, exist_ok=True)
        self.token = load_token()
        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

        self.seen, self.local_paths = self._load_state()  # seen + id->ruta local
        self._marked = set()           # ids ya reconciliados como bajados en el server
        self.files = []                # último listado del server
        self.jobs = queue.Queue()      # rutas a subir
        self.paused = False
        self.stop = False

        self._build_gui()
        self.root.report_callback_exception = lambda exc, val, tb: logline(
            "TK:\n" + "".join(traceback.format_exception(exc, val, tb)))
        self._build_tray()

        threading.Thread(target=self._worker_safe, daemon=True).start()

    def _worker_safe(self):
        try:
            self._worker()
        except Exception:
            logline("WORKER CRASH:\n" + traceback.format_exc())

    # ---------------- estado ----------------
    def _load_state(self):
        try:
            import json
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return set(d.get("seen", [])), dict(d.get("paths", {}))
        except (OSError, ValueError):
            return set(), {}

    def _save_state(self):
        try:
            import json
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(
                json.dumps({"seen": sorted(self.seen), "paths": self.local_paths}),
                encoding="utf-8")
        except OSError:
            pass

    # ---------------- GUI ----------------
    def _build_gui(self):
        self.root = TkinterDnD.Tk()
        self.root.title("Puente")
        self.root.geometry("640x460")
        self.root.minsize(520, 380)

        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="Puente de archivos", font=("Segoe UI", 13, "bold")).pack(side="left")
        self.conn = ttk.Label(top, text="…", foreground="#888")
        self.conn.pack(side="right")

        drop = tk.Frame(self.root, height=92, bg="#2b2b2b", highlightthickness=2,
                        highlightbackground="#555")
        drop.pack(fill="x", padx=12, pady=(0, 8))
        drop.pack_propagate(False)
        self.drop_lbl = tk.Label(drop, text="⬇  Arrastrá archivos acá para enviarlos a la VDI",
                                 bg="#2b2b2b", fg="#ddd", font=("Segoe UI", 10))
        self.drop_lbl.pack(expand=True)
        for w in (drop, self.drop_lbl):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self._on_drop)
        drop.bind("<Button-1>", lambda e: self._choose())
        self.drop_lbl.bind("<Button-1>", lambda e: self._choose())

        clip = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        clip.pack(fill="x")
        self.cliptext = tk.Text(clip, height=2, wrap="word")
        self.cliptext.pack(side="left", fill="x", expand=True)
        ttk.Button(clip, text="Enviar texto", command=self._send_text).pack(side="left", padx=(8, 0))

        cols = ("name", "size", "src", "estado")
        self.tree = ttk.Treeview(self.root, columns=cols, show="headings", height=10)
        self.tree.heading("name", text="Archivo")
        self.tree.heading("size", text="Tamaño")
        self.tree.heading("src", text="Origen")
        self.tree.heading("estado", text="Estado")
        self.tree.column("name", width=320)
        self.tree.column("size", width=80, anchor="e")
        self.tree.column("src", width=80, anchor="center")
        self.tree.column("estado", width=90, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<Double-1>", lambda e: self._open_sel())

        btns = ttk.Frame(self.root, padding=(12, 8))
        btns.pack(fill="x")
        ttk.Button(btns, text="Elegir archivos…", command=self._choose).pack(side="left")
        ttk.Button(btns, text="Copiar", command=self._copy_sel).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Abrir", command=self._open_sel).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Mostrar en carpeta", command=self._reveal_sel).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Borrar", command=self._delete_sel).pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Carpeta inbox", command=lambda: os.startfile(INBOX)).pack(side="right")

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        self.pb = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.pb.pack(fill="x")
        self.status = ttk.Label(bar, text="Listo.", foreground="#888")
        self.status.pack(anchor="w", pady=(4, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._hide)
        self.root.bind("<Unmap>", lambda e: self._hide() if self.root.state() == "iconic" else None)
        self.root.bind_all("<Control-v>", self._on_paste)
        self.root.bind_all("<Control-V>", self._on_paste)

    def _set_status(self, text, pct=None):
        self.status.config(text=text)
        if pct is not None:
            self.pb["value"] = pct

    def gui(self, fn, *a):
        self.root.after(0, lambda: fn(*a))

    # ---------------- tray ----------------
    def _icon_img(self):
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([6, 6, 58, 58], radius=12, fill=(45, 120, 220, 255))
        d.polygon([(32, 14), (44, 30), (36, 30), (36, 50), (28, 50), (28, 30), (20, 30)],
                  fill=(255, 255, 255, 255))
        return img

    def _build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem("Abrir ventana", lambda: self.gui(self._show), default=True),
            pystray.MenuItem("Abrir carpeta inbox", lambda: os.startfile(INBOX)),
            pystray.MenuItem(lambda i: "Reanudar sync" if self.paused else "Pausar sync",
                             lambda: self._toggle_pause()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir", lambda: self._quit()),
        )
        self.tray = pystray.Icon("puente", self._icon_img(), "Puente", menu)
        self.tray.run_detached()

    def _toggle_pause(self):
        self.paused = not self.paused
        self.gui(self._set_status, "Pausado." if self.paused else "Reanudado.")

    def _show(self):
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()

    def _hide(self):
        self.root.withdraw()

    def _quit(self):
        self.stop = True
        try:
            self.tray.stop()
        except Exception:
            pass
        self.gui(self.root.destroy)

    # ---------------- acciones ----------------
    def _on_drop(self, event):
        for p in self.root.tk.splitlist(event.data):
            path = Path(p)
            if path.is_file():
                self.jobs.put(path)
        self._set_status("En cola para enviar…")

    def _choose(self):
        paths = filedialog.askopenfilenames(title="Elegir archivos para enviar a la VDI")
        for p in paths:
            self.jobs.put(Path(p))
        if paths:
            self._set_status("En cola para enviar…")

    def _sel_meta(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return next((f for f in self.files if f["id"] == sel[0]), None)

    def _send_text(self):
        txt = self.cliptext.get("1.0", "end").rstrip("\n")
        if not txt.strip():
            return
        self.cliptext.delete("1.0", "end")
        self._set_clipboard(txt)  # lo enviado queda también en tu portapapeles
        threading.Thread(target=lambda: self._post_clip(txt), daemon=True).start()

    def _post_clip(self, txt):
        try:
            up = self.session.post(f"{URL}/api/clip", data={"text": txt, "source": "local"}, timeout=30)
            if up.ok:
                self.seen.add(up.json()["id"])
                self._save_state()
                self.gui(self._set_status, "Texto enviado.", 100)
            else:
                self.gui(self._set_status, f"Error enviando texto ({up.status_code})", 0)
        except requests.RequestException as e:
            self.gui(self._set_status, f"Error enviando texto: {e}", 0)

    def _set_clipboard(self, text, label=""):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            self._set_status(f"Texto recibido y copiado al portapapeles: {label[:40]}" if label
                             else "Copiado al portapapeles.")
        except tk.TclError:
            pass

    def _copy_sel(self):
        m = self._sel_meta()
        if not m:
            return
        if m.get("kind") == "text":
            self._set_clipboard(m.get("text", ""))
        else:
            self._set_status("Seleccioná un texto para copiar (para archivos usá Abrir).")

    def _on_paste(self, event=None):
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            data = None
        if isinstance(data, Image.Image):
            TMP.mkdir(parents=True, exist_ok=True)
            p = TMP / f"captura-{int(time.time()*1000)}.png"
            try:
                data.save(p, "PNG")
                self.jobs.put(p)
                self._set_status("Imagen pegada, enviando…")
            except OSError:
                pass
            return "break"
        if isinstance(data, list):  # rutas de archivo en el portapapeles
            for fp in data:
                if Path(fp).is_file():
                    self.jobs.put(Path(fp))
            self._set_status("En cola para enviar…")
            return "break"
        if event is not None and event.widget is self.cliptext:
            return  # texto pegado dentro de la caja: comportamiento normal
        try:
            txt = self.root.clipboard_get()
        except tk.TclError:
            txt = ""
        if txt.strip():
            threading.Thread(target=lambda: self._post_clip(txt), daemon=True).start()
            self._set_status("Texto pegado, enviando…")
            return "break"

    def _open_sel(self):
        m = self._sel_meta()
        if not m:
            return
        if m.get("kind") == "text":
            self._copy_sel()
            return
        p = self.local_paths.get(m["id"])
        if p and Path(p).exists():
            os.startfile(p)
        else:
            self.gui(self._set_status, "Bajando para abrir…")
            threading.Thread(target=lambda: self._download(m, then_open=True), daemon=True).start()

    def _reveal_sel(self):
        m = self._sel_meta()
        if not m:
            return
        p = self.local_paths.get(m["id"])
        if p and Path(p).exists():
            os.system(f'explorer /select,"{p}"')
        else:
            os.startfile(INBOX)

    def _delete_sel(self):
        m = self._sel_meta()
        if not m:
            return
        try:
            self.session.delete(f"{URL}/api/files/{m['id']}", timeout=15)
        except requests.RequestException:
            pass
        self._refresh_now()

    # ---------------- worker / red ----------------
    def _render_list(self, files):
        self.files = files
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for f in sorted(files, key=lambda x: x.get("uploaded_at", 0), reverse=True):
            src = "local" if f.get("source") == "local" else "VDI/web"
            label = ("📝 " if f.get("kind") == "text" else "") + f["name"]
            if f.get("source") == "local":
                est = "—"
            elif f.get("downloaded"):
                est = "bajado ✓"
            else:
                est = "sin bajar"
            self.tree.insert("", "end", iid=f["id"], values=(label, human(f["size"]), src, est))
            if f["id"] in sel:
                self.tree.selection_add(f["id"])

    def _refresh_now(self):
        threading.Thread(target=self._poll_once, daemon=True).start()

    def _poll_once(self):
        try:
            r = self.session.get(f"{URL}/api/files", timeout=20)
            if r.status_code == 401:
                self.gui(self.conn.config, {"text": "token inválido", "foreground": "#c0392b"})
                return None
            r.raise_for_status()
            files = r.json().get("files", [])
            self.gui(self.conn.config, {"text": "conectado", "foreground": "#2e8b57"})
            self.gui(self._render_list, files)
            return files
        except requests.RequestException:
            self.gui(self.conn.config, {"text": "sin conexión", "foreground": "#c0392b"})
            return None

    def _mark_downloaded(self, fid):
        try:
            self.session.post(f"{URL}/api/files/{fid}/downloaded", timeout=15)
        except requests.RequestException:
            pass

    def _download(self, meta, then_open=False):
        dest = unique(INBOX / meta["name"])
        try:
            with self.session.get(f"{URL}/api/files/{meta['id']}", timeout=300, stream=True) as dl:
                dl.raise_for_status()
                total = int(dl.headers.get("Content-Length", meta.get("size", 0))) or 1
                done = 0
                with dest.open("wb") as out:
                    for chunk in dl.iter_content(256 * 1024):
                        out.write(chunk)
                        done += len(chunk)
                        self.gui(self._set_status, f"Bajando {meta['name']}…  {human(done)} / {human(total)}",
                                 done * 100 / total)
            self.seen.add(meta["id"])
            self.local_paths[meta["id"]] = str(dest)
            self._save_state()
            self._mark_downloaded(meta["id"])
            self.gui(self._set_status, f"Bajado: {dest.name}", 100)
            if then_open:
                os.startfile(dest)
        except requests.RequestException as e:
            self.gui(self._set_status, f"Error al bajar: {e}", 0)

    def _upload(self, path: Path):
        def cb(done, total):
            self.gui(self._set_status, f"Enviando {path.name}…  {human(done)} / {human(total)}",
                     done * 100 / max(total, 1))
        reader = ProgressReader(path, cb)
        try:
            up = self.session.post(f"{URL}/api/upload",
                                   files={"file": (path.name, reader)},
                                   data={"source": "local"}, timeout=600)
            reader.close()
            if up.ok:
                fid = up.json()["id"]
                self.seen.add(fid)
                dest = unique(SENT / path.name)
                try:
                    path.rename(dest)
                except OSError:
                    dest = path
                self.local_paths[fid] = str(dest)
                self._save_state()
                self.gui(self._set_status, f"Enviado: {path.name}", 100)
            else:
                self.gui(self._set_status, f"Error al enviar ({up.status_code})", 0)
        except requests.RequestException as e:
            reader.close()
            self.gui(self._set_status, f"Error al enviar: {e}", 0)

    def _worker(self):
        if not self.token:
            self.gui(self.conn.config, {"text": "falta DROP_TOKEN", "foreground": "#c0392b"})
        last_poll = 0
        while not self.stop:
            # subir lo que haya en cola (drag-drop / elegir)
            try:
                while True:
                    path = self.jobs.get_nowait()
                    if path.is_file():
                        self._upload(path)
            except queue.Empty:
                pass
            # también lo que aparezca en outbox\
            if not self.paused:
                for p in sorted(OUTBOX.iterdir()):
                    if p.is_file():
                        self._upload(p)
            # bajar lo nuevo
            if not self.paused and time.time() - last_poll >= POLL:
                last_poll = time.time()
                files = self._poll_once()
                for f in (files or []):
                    if f["id"] in self.seen:
                        # ya procesado antes: si lo bajamos pero el server no lo sabe, marcarlo
                        if (f.get("source") != "local" and f.get("kind") != "text"
                                and not f.get("downloaded") and f["id"] not in self._marked):
                            self._marked.add(f["id"])
                            self._mark_downloaded(f["id"])
                        continue
                    if f.get("source") == "local":
                        self.seen.add(f["id"])
                        continue
                    if f.get("kind") == "text":   # texto recibido: copiar solo al portapapeles
                        self.gui(self._set_clipboard, f.get("text", ""), f.get("name", ""))
                        self.seen.add(f["id"])
                        self._mark_downloaded(f["id"])
                        continue
                    self._download(f)
            time.sleep(0.4)

    def run(self):
        self.root.mainloop()


def main():
    try:
        Puente().run()
    except Exception:
        logline("FATAL:\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
