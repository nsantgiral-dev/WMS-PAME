"""
Candado anti-producción local (2026-09-25, tanda 2 · E).

**Qué pasaba.** El `DATABASE_URL` de una sesión de desarrollo apunta a la base
de producción en Railway (`metro.proxy.rlwy.net`, ver «El script no verificaba
contra qué base borraba»). Y `create_app()` arranca los schedulers según
`SYNC_SCHEDULER` (default `true`): un script local que solo quería leer algo
levantó la DLQ, el sync de pedidos, el barrido de cartera… **contra
producción**, dos segundos, hasta que el script terminó. Nadie lo pidió y nada
lo decía.

**Ahora.** Un proceso **fuera de Railway** (sin `RAILWAY_ENVIRONMENT_NAME`)
cuya base es **de Railway** (`*.rlwy.net`, `*.railway.internal`) no arranca
ningún scheduler ni el hilo automático de la DLQ, y lo dice en grande en el
log y en `SCHEDULERS_OMITIDOS` (que publica `/api/health/siesa`).

No hay variable para saltarlo, a propósito: quien de verdad quiera correr los
crons contra una base de Railway lo hace desde Railway, donde la variable
existe. Un desarrollador que necesita un cron puntual lo llama a mano.

Una función (`motivo_candado`), sin red y sin base: mira solo el entorno.
Trinquete: `tests/test_candado_produccion_local.py`.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

#: Sufijos de host que son bases de Railway: el proxy público y la red interna.
HOSTS_RAILWAY = ('rlwy.net', 'railway.internal')


def _host_de(url: str) -> str:
    try:
        return (urlparse(url).hostname or '').lower()
    except (ValueError, TypeError):
        return ''


def base_es_de_railway(url: str) -> bool:
    """¿La URL de la base apunta a Railway? Si no se puede leer el host, se
    busca el sufijo en la cadena entera: ante la duda, se trata como Railway
    (Regla 0 — el lado conservador es no arrancar)."""
    url = (url or '').strip()
    if not url:
        return False
    host = _host_de(url)
    if host:
        return any(host == h or host.endswith('.' + h) for h in HOSTS_RAILWAY)
    bajo = url.lower()
    return any(h in bajo for h in HOSTS_RAILWAY)


def motivo_candado(env=None) -> str | None:
    """`None` si este proceso puede arrancar schedulers e hilos automáticos; si
    no, el porqué (para el log y la salud).

    `env`: un mapeo tipo `os.environ` (los tests pasan un dict)."""
    env = os.environ if env is None else env
    if (env.get('RAILWAY_ENVIRONMENT_NAME') or '').strip():
        return None
    url = env.get('DATABASE_URL') or ''
    if not base_es_de_railway(url):
        return None
    return (f'CANDADO ANTI-PRODUCCIÓN LOCAL: este proceso corre fuera de Railway '
            f'(sin RAILWAY_ENVIRONMENT_NAME) y su base es de Railway '
            f'({_host_de(url) or "host ilegible"}). No arranca ningún scheduler ni '
            f'hilo automático.')
