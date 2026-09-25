"""
El latido de los crons — lo que corrió de verdad, y cuándo (P1-11, 2026-09-25).

## El caso

`SCHEDULERS_ACTIVOS` es una lista en memoria que se llena al ARRANCAR el
proceso. Tres cosas que no dice:

· la web y el worker son procesos distintos: `/api/health/siesa` contestado
  desde la web no ve los crons del worker (`alertas_por_correo` salía `false`
  siempre desde la web);
· un cron registrado que revienta en cada corrida seguía «activo»;
· un cron que dejó de correr (el scheduler murió con el hilo) también.

## Ahora

Todo job de APScheduler se registra envuelto: `add_job(func=con_latido('id',
fn), ...)`. Cada corrida escribe su fila de `cron_latido` (por cron × servicio
de Railway): inicio, fin, si terminó sin excepción, el error, fallos
seguidos. La escritura va por una conexión propia (`db.engine.begin()`): no
toca la sesión del job y un fallo al escribir el latido nunca rompe el cron.

`estado()` es lo que leen `/api/health/siesa` y 🩺 Salud del dato: lo que
corrió, dónde y hace cuánto, desde la BASE — no desde el entorno del proceso
que contesta.

Trinquete: `tests/test_cron_latido.py` (AST: todo `add_job` de `app/` y
`flota/` pasa su función por `con_latido`).

## Lo que NO dice

- Un job que atrapa sus propias excepciones y las loguea termina «bien»: el
  latido dice que corrió, no que hizo su trabajo.
- Los hilos que no son APScheduler (la carga de inventario de las 7:00 y el
  refresco de existencias, `threading.Timer`) no pasan por aquí: su rastro es
  `registros_sync`.
"""
import functools
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_APP = {'app': None}


def usar_app(app):
    """La app con la que se escribe el latido (la registra `create_app`)."""
    _APP['app'] = app


def servicio() -> str:
    return (os.getenv('RAILWAY_SERVICE_NAME') or '').strip() or 'local'


def _app_de(args, kwargs):
    from flask import Flask
    for v in list(args) + list(kwargs.values()):
        if isinstance(v, Flask):
            return v
    return _APP['app']


def _escribir(app, nombre, **campos):
    """Upsert de la fila (nombre, servicio). Nunca levanta."""
    if app is None:
        return
    try:
        from sqlalchemy import select
        from app.extensions import db
        from app.models.cron_latido import CronLatido
        t = CronLatido.__table__
        srv = servicio()
        with app.app_context():
            with db.engine.begin() as conn:
                fila = conn.execute(select(t.c.id, t.c.corridas, t.c.fallos_seguidos)
                                    .where(t.c.nombre == nombre, t.c.servicio == srv)).first()
                if fila is None:
                    conn.execute(t.insert().values(nombre=nombre, servicio=srv,
                                                   corridas=0, fallos_seguidos=0))
                    fila = conn.execute(select(t.c.id, t.c.corridas, t.c.fallos_seguidos)
                                        .where(t.c.nombre == nombre, t.c.servicio == srv)).first()
                valores = dict(campos)
                if 'ultimo_ok' in campos:
                    valores['corridas'] = (fila.corridas or 0) + 1
                    valores['fallos_seguidos'] = (0 if campos['ultimo_ok']
                                                  else (fila.fallos_seguidos or 0) + 1)
                conn.execute(t.update().where(t.c.id == fila.id).values(**valores))
    except Exception as e:     # noqa: BLE001 — el latido no rompe el cron
        logger.warning('[LATIDO] no se pudo escribir %s: %s', nombre, e)


def con_latido(nombre: str, fn):
    """Envuelve `fn` para que cada corrida deje su latido."""
    @functools.wraps(fn)
    def _envuelto(*args, **kwargs):
        app = _app_de(args, kwargs)
        _escribir(app, nombre, ultimo_inicio=datetime.utcnow())
        try:
            r = fn(*args, **kwargs)
        except Exception as e:
            _escribir(app, nombre, ultimo_fin=datetime.utcnow(), ultimo_ok=False,
                      ultimo_error=f'{type(e).__name__}: {str(e)[:480]}')
            raise
        ahora = datetime.utcnow()
        _escribir(app, nombre, ultimo_fin=ahora, ultimo_ok=True, ultimo_ok_en=ahora,
                  ultimo_error=None)
        return r
    _envuelto._latido = nombre
    return _envuelto


#: Cada cuánto se espera que corra cada cron, para decir «callado». Un cron
#: que no está acá se muestra sin veredicto de atraso (se ve su última hora).
#: Holgura incluida; de noche los de ventana no corren (7:00–19:30 Bogotá).
ESPERADO = {
    'dlq_siesa_jobs': timedelta(minutes=10),
    'pedidos_siesa_sync': timedelta(minutes=10),
    'reposicion_barrido_stock_picking': timedelta(hours=1, minutes=15),
    'cartera_barrido': timedelta(hours=14),
    'resumen_operativo_diario': timedelta(hours=26),
    'alertas_rutas_sin_liquidar': timedelta(hours=26),
    'analitica_kpi_diario': timedelta(hours=26),
}


#: Los crons que mandan correo.
CRONS_DE_ALERTA = ('alertas_huerfanas_email', 'alertas_stock_critico',
                   'alertas_rutas_sin_liquidar', 'resumen_operativo_diario')


def estado(ahora: datetime = None) -> dict:
    """Lo que corrió de verdad: por cron, la fila más reciente entre servicios.
    Lee la base, no el proceso."""
    ahora = ahora or datetime.utcnow()
    try:
        from app.models.cron_latido import CronLatido
        filas = CronLatido.query.all()
    except Exception as e:     # noqa: BLE001
        return {'error': f'No se pudo leer el latido: {str(e)[:200]}', 'crons': []}
    crons = []
    for f in sorted(filas, key=lambda x: (x.nombre, x.servicio)):
        d = f.to_dict()
        ref = f.ultimo_fin or f.ultimo_inicio
        d['hace_min'] = round((ahora - ref).total_seconds() / 60) if ref else None
        esperado = ESPERADO.get(f.nombre)
        d['callado'] = bool(esperado and ref and ahora - ref > esperado)
        d['fallando'] = f.ultimo_ok is False
        crons.append(d)
    return {
        'crons': crons,
        'fallando': [c['nombre'] for c in crons if c['fallando']],
        'callados': [c['nombre'] for c in crons if c['callado']],
        # ¿Salen alertas por correo de ALGÚN servicio? Algún cron de alertas
        # corrió en las últimas 26 h.
        'alertas_por_correo': any(
            c['nombre'] in CRONS_DE_ALERTA and c['ultimo_inicio']
            and ahora - datetime.fromisoformat(c['ultimo_inicio']) < timedelta(hours=26)
            for c in crons),
        'nota': ('Sale de la base: lo que corrió en CUALQUIER servicio (web o worker), '
                 'no lo que este proceso arrancó. Un cron que atrapa sus propios errores '
                 'figura «bien»: el latido dice que corrió, no que hizo su trabajo.'),
    }
