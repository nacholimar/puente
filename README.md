# Puente

Puente de archivos y portapapeles entre dos máquinas que **no comparten copy‑paste ni
arrastrar/soltar** — por ejemplo una VDI/escritorio remoto (donde solo tenés el navegador) y
tu máquina local. Un servidor liviano en el medio (self‑hosted) hace de intermediario: subís
de un lado, aparece del otro.

- **Archivos** en ambos sentidos (drag‑and‑drop en la web; carpetas `inbox/` y `outbox/` que
  se sincronizan solas del lado local).
- **Portapapeles**: compartís **texto** (con botón *Copiar*, y autocopiado al portapapeles en
  la app de escritorio) e **imágenes** (pegar con `Ctrl+V`).
- Protegido con un **token**. Los archivos viejos se borran solos (retención configurable).
- La web agrupa por fecha / tipo / origen, con buscador y vista previa.

## Arquitectura

```
  Lado A (solo navegador)          Servidor (Docker)              Lado B (máquina local)
  ───────────────────────          ─────────────────              ──────────────────────
  página web  ──sube/baja──►  FastAPI :8000  ◄──app/watcher──►  Downloads/puente/
                              /data (volumen)                     inbox/   outbox/
```

Tres piezas:

| Carpeta | Qué es |
|---------|--------|
| `app/` | Backend FastAPI + la página web (API de archivos y de texto, con token). |
| `desktop/` | App de escritorio (Windows): bandeja del sistema, arrastrar y soltar, portapapeles, barra de progreso. |
| `sync/` | Watcher headless alternativo a la app de escritorio (sin GUI). |

## Servidor (Docker)

```bash
cp .env.example .env
# generar un token y pegarlo en .env como DROP_TOKEN=...
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

docker compose up -d --build
curl -s localhost:8091/health   # -> {"ok":true}
```

El servicio escucha en `127.0.0.1:8091`. Exponelo por detrás de un proxy/túnel con HTTPS
(Cloudflare Tunnel, Caddy, nginx, etc.) sobre el dominio que quieras.

Variables (en `.env`): `DROP_TOKEN` (obligatoria), `DROP_RETENTION_DAYS` (default 7),
`DROP_MAX_MB` (default 2048).

## Cliente local

Configurá dónde está el servidor y el token. Lo más cómodo es un archivo `~/.keys`
(`CLAVE=valor`, una por línea), que los clientes leen automáticamente:

```
DROP_URL=https://tu-dominio-del-puente
DROP_TOKEN=el-mismo-token-del-servidor
```

(También se pueden pasar por variables de entorno, o `--url` en el watcher.)

### App de escritorio (Windows)

```bash
pip install -r desktop/requirements.txt
pythonw desktop/puente_app.py          # queda en la bandeja del sistema
```

Arrastrá archivos a la ventana para enviarlos; `Ctrl+V` pega imágenes/texto; los archivos que
llegan se bajan a `Downloads/puente/inbox/` y el texto recibido se copia solo al portapapeles.
Para que arranque con el sistema, poné un acceso directo a `pythonw desktop/puente_app.py`
en la carpeta de Inicio.

### Watcher sin GUI (opcional)

```bash
pip install requests
python sync/drop-sync.py
```

Baja lo nuevo a `inbox/` y sube lo que dejes en `outbox/`.

## Endpoints

| Método | Ruta | Qué hace |
|--------|------|----------|
| GET | `/` | Página web (drag‑and‑drop, texto, lista) |
| POST | `/api/upload` | Sube un archivo (multipart; `source=web\|local`) |
| POST | `/api/clip` | Comparte un texto (`text`, `source`) |
| GET | `/api/files` | Lista todo (y limpia lo vencido) |
| GET | `/api/files/{id}` | Baja un archivo (`?inline=1` para vista previa) |
| DELETE | `/api/files/{id}` | Borra un elemento |
| GET | `/health` | Healthcheck |

Todo salvo `/health` y `/` exige el token (header `Authorization: Bearer`, cookie o `?token=`).

## Nota

Cuando el bloqueo de copy‑paste de la VDI es un control de la empresa (DLP), usá esta
herramienta solo para tus propios archivos de trabajo, no para datos sensibles del cliente.

## Licencia

MIT.
