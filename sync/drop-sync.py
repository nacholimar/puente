r"""Watcher local del puente — corre en tu máquina física (Windows).

  - Baja solo lo nuevo que llegó del VDI/web  ->  <base>\inbox\
  - Sube solo lo que pongas en              ->  <base>\outbox\   (luego lo mueve a outbox\_sent\)

El token se lee de ~/.keys (DROP_TOKEN) o de la variable de entorno DROP_TOKEN.
Dependencia: requests  ->  pip install requests

Uso:
    python drop-sync.py
    python drop-sync.py --base "D:\\puente" --url https://tu-dominio-del-puente --interval 3
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Instalá con:  pip install requests")

KEYS_FILE = Path.home() / ".keys"
LOG_FILE = Path(__file__).with_name("puente.log")

try:
    sys.stdout.reconfigure(encoding="utf-8")  # consola normal (puente.bat)
except (AttributeError, ValueError):
    pass


def log(msg: str) -> None:
    """Loguea a archivo (UTF-8) y, si hay consola, a stdout — sin crashear nunca."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except (OSError, ValueError, UnicodeError):
        pass
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


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


def load_token() -> str:
    tok = read_key("DROP_TOKEN")
    if not tok:
        sys.exit("No encontré DROP_TOKEN (ni en el entorno ni en ~/.keys).")
    return tok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=read_key("DROP_URL", "http://localhost:8091"))
    ap.add_argument("--base", default=os.environ.get("DROP_BASE", str(Path.home() / "Downloads" / "puente")))
    ap.add_argument("--interval", type=float, default=3.0)
    args = ap.parse_args()

    token = load_token()
    base = Path(args.base)
    inbox, outbox, sent = base / "inbox", base / "outbox", base / "outbox" / "_sent"
    for d in (inbox, outbox, sent):
        d.mkdir(parents=True, exist_ok=True)

    state_file = Path(__file__).with_name("state.json")
    try:
        seen = set(json.loads(state_file.read_text(encoding="utf-8")).get("seen", []))
    except (OSError, ValueError):
        seen = set()

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"

    def save_state():
        state_file.write_text(json.dumps({"seen": sorted(seen)}), encoding="utf-8")

    log(f"Puente activo. servidor={args.url} inbox={inbox} outbox={outbox}")

    while True:
        try:
            # --- bajar lo que vino del VDI/web ---
            r = s.get(f"{args.url}/api/files", timeout=20)
            if r.status_code == 401:
                log("Token inválido (401). Revisá DROP_TOKEN. Reintento en 30s.")
                time.sleep(30); continue
            r.raise_for_status()
            for f in r.json().get("files", []):
                if f["id"] in seen:
                    continue
                if f.get("source") == "local":        # no bajar lo que subí yo
                    seen.add(f["id"]); continue
                dest = _unique(inbox / f["name"])
                with s.get(f"{args.url}/api/files/{f['id']}", timeout=120, stream=True) as dl:
                    dl.raise_for_status()
                    with dest.open("wb") as out:
                        for chunk in dl.iter_content(1024 * 256):
                            out.write(chunk)
                seen.add(f["id"]); save_state()
                try:
                    s.post(f"{args.url}/api/files/{f['id']}/downloaded", timeout=15)
                except requests.RequestException:
                    pass
                log(f"  bajado: {dest.name}")

            # --- subir lo que haya en outbox ---
            for p in sorted(outbox.iterdir()):
                if p.is_dir() or _still_writing(p):
                    continue
                with p.open("rb") as fh:
                    up = s.post(f"{args.url}/api/upload",
                                files={"file": (p.name, fh)},
                                data={"source": "local"}, timeout=300)
                if up.ok:
                    seen.add(up.json()["id"]); save_state()
                    p.rename(_unique(sent / p.name))
                    log(f"  subido: {p.name}")
                else:
                    log(f"  ! error subiendo {p.name}: {up.status_code}")

        except requests.RequestException as e:
            log(f"  (sin conexión: {e})")
        except KeyboardInterrupt:
            log("Detenido."); return
        time.sleep(args.interval)


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, i = path.stem, path.suffix, 1
    while True:
        cand = path.with_name(f"{stem} ({i}){suf}")
        if not cand.exists():
            return cand
        i += 1


def _still_writing(p: Path, wait: float = 0.6) -> bool:
    """True si el archivo cambió de tamaño en 'wait' seg (aún se está copiando)."""
    try:
        a = p.stat().st_size
        time.sleep(wait)
        return p.stat().st_size != a
    except OSError:
        return True


if __name__ == "__main__":
    main()
