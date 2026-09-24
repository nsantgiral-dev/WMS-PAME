"""
Salud del dato y bitácora legible — Analítica, Fase 1 (2026-09-24).

## La pregunta

**«¿Puedo confiar en los números de hoy?»** Todo lo demás del módulo 📈
Analítica (recorrido, fugas, KPI) lee tablas que alimentan crons, syncs y
fotos. Si una de esas fuentes está apagada, atrasada o a medias, el número
se ve igual de firme que uno sano. Esta pieza lo dice antes de que alguien
decida sobre él.

## Reutiliza, no reimplementa

| Fuente | Lo que ya existía y se lee acá |
|---|---|
| Sync de pedidos | `registro_sync_service.estado_persistido('pedidos')` |
| Existencias de Siesa | `stock_siesa.updated_at` por bodega (lo escribe `_guardar_stock_en_bd`) |
| Fotos diarias | `fotos_siesa_corridas` (la corrida ES el veredicto de completitud) |
| Kardex | `kardex_service.salud_kardex()` — su veredicto, traducido |
| Vigía | `serie_vigia` por semana y fuente |
| Crons | `SCHEDULERS_ACTIVOS` / `_OMITIDOS` — lo mismo que publica `/api/health/siesa` |
| Invariantes | `auditoria.auditar(flujo)` — con tope, ver abajo |
| Cola de Siesa | `siesa_jobs` por estado |
| Claves de la cadena | `pedido_clave` en `tareas_picking` / `tareas_packing` |

**Cero llamadas a Siesa**: todo sale de la base del WMS.

## Cinco veredictos por fuente, y ninguno se pone verde por omisión

`AL_DIA` · `ATRASADA` · `INCOMPLETA` · `APAGADA` · `SIN_DATOS`.

`_NIVEL_DE_VEREDICTO` es **la** traducción veredicto → nivel (ok /
advertencia / crítico). `APAGADA` y `SIN_DATOS` nunca son `ok`: un cron
apagado no falla, se calla, y callarse es indistinguible de «todo bien».
Si la fuente es crítica para los números del día (pedidos, existencias),
apagada o sin datos es **crítico**.

El veredicto global (`CONFIABLE` / `CON_RESERVAS` / `NO_CONFIABLE`) sale de
**todos** los niveles: fuentes, invariantes, cola de Siesa y cobertura de
claves. Una sola fuente que no esté `AL_DIA` alcanza para que no diga
«confiable».

## La web y el worker son procesos distintos

Esta respuesta la contesta UN proceso (normalmente la web). Sus crons
(`SCHEDULERS_ACTIVOS`) se ven; los del worker (`HEAVY_SCHEDULERS=true`:
fotos, refresco de existencias, alertas) **no**. Por eso el veredicto de cada
fuente sale de la **frescura del dato en la base**, no de si el cron figura
en este proceso: el dato fresco es la única evidencia de que el cron del
otro proceso corrió. Lo mismo con los interruptores (`FOTOS_SIESA`,
`VIGIA_INGESTA_FACTURACION`): se leen en ESTE proceso, y Railway le da a
cada servicio sus propias variables. Con dato fresco, manda el dato; con
dato viejo e interruptor apagado acá, se declara `APAGADA` (probable).

## El tope de la auditoría

Correr los 39 invariantes cuesta. `resumen_auditoria` guarda el resultado
en el proceso `AUDITORIA_TTL` (10 min) y, al recalcular, deja de empezar
flujos nuevos pasado `AUDITORIA_PRESUPUESTO_S` (20 s). Lo que no alcanzó se
devuelve en `flujos_no_evaluados` y el global no puede decir «confiable» con
un flujo sin mirar. Cada invariante trae además sus propios topes de filas
(`consultas_truncadas`); esos también se declaran.

## Tiempo operativo, no tiempo de reloj

Siesa no opera de noche (Regla 14). Un sync de pedidos cuya última corrida
fue a las 8:55 p. m. no está «atrasado» a las 6 a. m. `tiempo_operativo`
cuenta solo el tiempo dentro de la ventana de cada fuente.
"""
import logging
import time as _time_mod
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA, dia_operativo_de, rango_dia_operativo_utc

logger = logging.getLogger(__name__)

_UTC = ZoneInfo('UTC')

# ──────────────────────────────────────────────────────────────────────────────
# Vocabulario y umbrales — declarados, no enterrados en el código
# ──────────────────────────────────────────────────────────────────────────────

AL_DIA, ATRASADA, INCOMPLETA, APAGADA, SIN_DATOS = (
    'AL_DIA', 'ATRASADA', 'INCOMPLETA', 'APAGADA', 'SIN_DATOS')
VEREDICTOS = (AL_DIA, ATRASADA, INCOMPLETA, APAGADA, SIN_DATOS)

OK, ADVERTENCIA, CRITICO = 'ok', 'advertencia', 'critico'

#: LA traducción veredicto → nivel. `APAGADA` y `SIN_DATOS` no son `ok`:
#: el silencio no es evidencia de salud.
_NIVEL_DE_VEREDICTO = {
    AL_DIA: OK,
    ATRASADA: ADVERTENCIA,
    INCOMPLETA: ADVERTENCIA,
    APAGADA: ADVERTENCIA,
    SIN_DATOS: ADVERTENCIA,
}
#: En una fuente crítica, estos veredictos suben a crítico.
_CRITICO_SI_FUENTE_CRITICA = (APAGADA, SIN_DATOS)

TEXTO_VEREDICTO = {
    AL_DIA: 'Al día',
    ATRASADA: 'Atrasada',
    INCOMPLETA: 'Incompleta',
    APAGADA: 'Apagada',
    SIN_DATOS: 'Sin datos',
}

CONFIABLE, CON_RESERVAS, NO_CONFIABLE = 'CONFIABLE', 'CON_RESERVAS', 'NO_CONFIABLE'
TEXTO_GLOBAL = {
    CONFIABLE: 'Podés confiar en los números de hoy',
    CON_RESERVAS: 'Los números sirven, con reservas: mirá qué fuente falla antes de decidir',
    NO_CONFIABLE: 'No decidas con estos números todavía',
}

#: Ventana en que cada fuente PUEDE refrescarse (Bogotá). El sync de pedidos
#: corre `hour='7-20'` → hasta las 20:59; Siesa deja de operar ~8 p. m.
VENTANA_PEDIDOS = (time(7, 0), time(21, 0))
VENTANA_SIESA = (time(7, 0), time(20, 0))

#: El sync de pedidos corre cada minuto; 15 minutos operativos sin una
#: corrida completa es que dejó de correr, no que está lento.
TOLERANCIA_PEDIDOS = timedelta(minutes=15)
#: El refresco de existencias corre cada 45 min (`_REFRESH_INTERVALO`): dos
#: ciclos perdidos y un margen.
TOLERANCIA_STOCK = timedelta(hours=2)
#: Una corrida de pedidos abierta más de esto murió (deploy, OOM).
CORRIDA_ABIERTA_MUERTA = timedelta(minutes=30)
#: Las fotos corren a las 18:00; pasada la ventana (19:30) la de hoy ya debe estar.
HORA_FOTO_DEL_DIA = time(19, 30)
#: La serie de adopción de Vigía corre los lunes 05:30.
HORA_VIGIA_LUNES = time(6, 0)

#: Cola de Siesa: un job pendiente más de esto (tiempo operativo) está atascado.
TOLERANCIA_JOB_PENDIENTE = timedelta(hours=1)
#: Un job en PROCESANDO más de esto quedó colgado.
TOLERANCIA_JOB_PROCESANDO = timedelta(minutes=30)

#: Cobertura de `pedido_clave`: por debajo, la cadena pedido → caja pierde
#: tareas que no se pueden unir.
UMBRAL_COBERTURA_OK = 0.95
UMBRAL_COBERTURA_CRITICO = 0.80

#: Tope de la auditoría de invariantes.
AUDITORIA_TTL = timedelta(minutes=10)
AUDITORIA_PRESUPUESTO_S = 20.0

#: Tope de filas para el patrón por hora de la bitácora.
BITACORA_TOPE_HORAS = 20000


def _iso(dt):
    return dt.isoformat() if dt else None


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _a_bogota(momento_utc):
    return momento_utc.replace(tzinfo=_UTC).astimezone(TZ_BOGOTA)


def tiempo_operativo(desde_utc, hasta_utc, ventana=VENTANA_SIESA) -> timedelta:
    """Tiempo entre dos instantes UTC naive **contando solo la ventana** de
    cada día (Bogotá). Fuera de la ventana el reloj no corre: la fuente no
    podía refrescarse.

    Más de 60 días de distancia se devuelven como tiempo de reloj — a esa
    altura el veredicto es el mismo y no vale la pena iterar.
    """
    if desde_utc is None:
        return None
    if hasta_utc <= desde_utc:
        return timedelta(0)
    if hasta_utc - desde_utc > timedelta(days=60):
        return hasta_utc - desde_utc
    a, b = _a_bogota(desde_utc), _a_bogota(hasta_utc)
    total = timedelta(0)
    dia = a.date()
    while dia <= b.date():
        ini = datetime.combine(dia, ventana[0], tzinfo=TZ_BOGOTA)
        fin = datetime.combine(dia, ventana[1], tzinfo=TZ_BOGOTA)
        solape = min(fin, b) - max(ini, a)
        if solape > timedelta(0):
            total += solape
        dia += timedelta(days=1)
    return total


def _texto_duracion(td):
    if td is None:
        return 'nunca'
    minutos = int(td.total_seconds() // 60)
    if minutos < 60:
        return f'{minutos} min'
    horas = minutos // 60
    if horas < 48:
        return f'{horas} h {minutos % 60} min'
    return f'{horas // 24} días'


# ──────────────────────────────────────────────────────────────────────────────
# Una fuente
# ──────────────────────────────────────────────────────────────────────────────

def nivel_de(veredicto, critica=False):
    """El nivel de un veredicto. **Una política, una función.**"""
    if critica and veredicto in _CRITICO_SI_FUENTE_CRITICA:
        return CRITICO
    return _NIVEL_DE_VEREDICTO[veredicto]


def _fuente(clave, nombre, veredicto, motivo, que_hacer, *, alimenta, critica=False,
            ultima=None, completa=None, cron=None, detalle=None, activos=()):
    assert veredicto in VEREDICTOS, veredicto
    cron_info = None
    if cron:
        tag, proceso = cron
        cron_info = {
            'tag': tag,
            'proceso': proceso,
            'en_este_proceso': tag in activos,
            'nota': ('Corre en el worker (HEAVY_SCHEDULERS=true): no se ve desde '
                     'este proceso. La frescura del dato es la evidencia.'
                     if proceso == 'worker' else
                     'Corre en la web (esencial). Si no figura en este proceso, '
                     'buscalo en el otro servicio.'),
        }
    return {
        'clave': clave,
        'nombre': nombre,
        'veredicto': veredicto,
        'veredicto_texto': TEXTO_VEREDICTO[veredicto],
        'nivel': nivel_de(veredicto, critica),
        'critica': critica,
        'motivo': motivo,
        'que_hacer': que_hacer if veredicto != AL_DIA else None,
        'ultima_actualizacion': ultima,
        'completa': completa,
        'alimenta': alimenta,
        'cron': cron_info,
        'detalle': detalle or {},
    }


def _fuente_ilegible(clave, nombre, error, alimenta, critica=False):
    return _fuente(clave, nombre, SIN_DATOS,
                   f'No se pudo leer: {str(error)[:160]}',
                   'Revisar la base: una fuente que no se puede leer no se puede afirmar sana.',
                   alimenta=alimenta, critica=critica)


# ──────────────────────────────────────────────────────────────────────────────
# Pedidos de Siesa (sync cada minuto)
# ──────────────────────────────────────────────────────────────────────────────

def fuente_pedidos(ahora_utc, activos=()):
    nombre = 'Pedidos de Siesa (sincronización)'
    alimenta = 'Pedidos pendientes, recorrido del pedido, venta perdida'
    try:
        from app.services import registro_sync_service as reg
        est = reg.estado_persistido('pedidos')
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible('pedidos', nombre, e, alimenta, critica=True)

    kw = dict(alimenta=alimenta, critica=True,
              cron=('[PEDIDOS_SCHEDULER]', 'web'), activos=activos)
    ult, ok = est.get('ultima_corrida'), est.get('ultima_exitosa')
    for r in (ult, ok):
        if r and r.get('_error_lectura'):
            return _fuente_ilegible('pedidos', nombre, r['_error_lectura'], alimenta, True)
    if ult is None:
        return _fuente('pedidos', nombre, SIN_DATOS,
                       'Nunca se registró una sincronización de pedidos.',
                       'Verificar que el servicio web tenga el cron de pedidos '
                       '([PEDIDOS_SCHEDULER]) y credenciales de Siesa.', **kw)

    fin_ok = _parse_iso((ok or {}).get('fin'))
    detalle = {'ultima_corrida': ult, 'ultima_completa_fin_utc': _iso(fin_ok)}
    inicio_ult = _parse_iso(ult.get('inicio'))
    abierta = ult.get('ok') is None
    muerta = abierta and inicio_ult and (ahora_utc - inicio_ult) > CORRIDA_ABIERTA_MUERTA

    if ok is None:
        return _fuente('pedidos', nombre, INCOMPLETA,
                       'Ninguna sincronización terminó completa: no se sabe qué '
                       'pedidos siguen vivos.',
                       'Revisar el error de la última corrida (Siesa caída, token, '
                       'paginación).', detalle=detalle, **kw)
    if ult.get('ok') is False:
        return _fuente('pedidos', nombre, INCOMPLETA,
                       'La última sincronización no leyó todas las páginas: '
                       + str(ult.get('error') or 'sin detalle')[:200],
                       'Un barrido incompleto no borra ni agrega salidas: esperar la '
                       'próxima corrida; si se repite, revisar Siesa.',
                       ultima=_iso(fin_ok), completa=False, detalle=detalle, **kw)
    if muerta:
        return _fuente('pedidos', nombre, INCOMPLETA,
                       f'La última corrida quedó abierta desde {inicio_ult.isoformat()} '
                       '(UTC): el proceso murió sin cerrarla.',
                       'Revisar reinicios del servicio web.',
                       ultima=_iso(fin_ok), completa=False, detalle=detalle, **kw)

    edad = tiempo_operativo(fin_ok, ahora_utc, VENTANA_PEDIDOS)
    detalle['edad_operativa_min'] = int(edad.total_seconds() // 60)
    if edad > TOLERANCIA_PEDIDOS:
        return _fuente('pedidos', nombre, ATRASADA,
                       f'La última sincronización completa fue hace '
                       f'{_texto_duracion(edad)} de horario operativo '
                       f'(tolerancia {_texto_duracion(TOLERANCIA_PEDIDOS)}).',
                       'Verificar que el cron de pedidos siga corriendo en la web.',
                       ultima=_iso(fin_ok), completa=True, detalle=detalle, **kw)
    return _fuente('pedidos', nombre, AL_DIA,
                   f'Última sincronización completa hace {_texto_duracion(edad)} '
                   '(tiempo operativo).', None,
                   ultima=_iso(fin_ok), completa=True, detalle=detalle, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Existencias de Siesa (`stock_siesa`)
# ──────────────────────────────────────────────────────────────────────────────

def _bodega_de_almacen(almacen_id):
    if not almacen_id:
        return None
    from app.models.almacen import Almacen
    alm = db.session.get(Almacen, int(almacen_id))
    return (alm.bodega_siesa_id or '').strip() or None if alm else None


def fuente_stock(ahora_utc, almacen_id=None, activos=()):
    nombre = 'Existencias de Siesa por bodega'
    alimenta = 'Stock, valorización, Armador (compras), reconciliación'
    try:
        from sqlalchemy import func
        from app.models.stock_siesa import StockSiesa
        from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
        esperadas = list(_BODEGAS_INVENTARIO)
        bod = _bodega_de_almacen(almacen_id)
        if almacen_id:
            esperadas = [bod] if bod else []
        filas = (db.session.query(StockSiesa.bodega, func.max(StockSiesa.updated_at),
                                  func.count(StockSiesa.id))
                 .group_by(StockSiesa.bodega).all())
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible('stock_siesa', nombre, e, alimenta, critica=True)

    kw = dict(alimenta=alimenta, critica=True,
              cron=('[INV_SIESA_REFRESH]', 'worker'), activos=activos)
    por_bodega = {(b or '').strip(): (u, n) for b, u, n in filas}
    if almacen_id and not esperadas:
        return _fuente('stock_siesa', nombre, SIN_DATOS,
                       'El almacén elegido no tiene bodega de Siesa asignada.',
                       'Asignar la bodega de Siesa al almacén.', **kw)
    presentes = [b for b in esperadas if b in por_bodega]
    if not presentes:
        return _fuente('stock_siesa', nombre, SIN_DATOS,
                       'No hay existencias de Siesa guardadas'
                       + (f' para {esperadas[0]}' if almacen_id else '') + '.',
                       'Descargar existencias (el worker las refresca cada 45 min '
                       'con HEAVY_SCHEDULERS=true).', **kw)

    bodegas = []
    for b in esperadas:
        u, n = por_bodega.get(b, (None, 0))
        edad = tiempo_operativo(u, ahora_utc) if u else None
        bodegas.append({'bodega': b, 'actualizada_utc': _iso(u), 'filas': n,
                        'edad_operativa_min': (int(edad.total_seconds() // 60)
                                               if edad is not None else None),
                        'atrasada': edad is None or edad > TOLERANCIA_STOCK})
    faltan = [x['bodega'] for x in bodegas if not x['filas']]
    atrasadas = [x['bodega'] for x in bodegas if x['filas'] and x['atrasada']]
    mas_vieja = min((por_bodega[b][0] for b in presentes if por_bodega[b][0]), default=None)
    detalle = {'bodegas': bodegas, 'tolerancia_operativa_min':
               int(TOLERANCIA_STOCK.total_seconds() // 60)}
    if faltan:
        return _fuente('stock_siesa', nombre, INCOMPLETA,
                       f'Sin existencias guardadas para {len(faltan)} bodega(s): '
                       + ', '.join(faltan) + '.',
                       'Descargar existencias de esas bodegas; mientras tanto sus '
                       'totales no existen (no son cero).',
                       ultima=_iso(mas_vieja), completa=False, detalle=detalle, **kw)
    if atrasadas:
        return _fuente('stock_siesa', nombre, ATRASADA,
                       f'{len(atrasadas)} bodega(s) sin refrescar hace más de '
                       f'{_texto_duracion(TOLERANCIA_STOCK)} operativas: '
                       + ', '.join(atrasadas) + '.',
                       'Verificar el refresco de existencias en el worker '
                       '([INV_SIESA_REFRESH]).',
                       ultima=_iso(mas_vieja), completa=True, detalle=detalle, **kw)
    return _fuente('stock_siesa', nombre, AL_DIA,
                   f'{len(presentes)} bodega(s) refrescadas dentro de la tolerancia.',
                   None, ultima=_iso(mas_vieja), completa=True, detalle=detalle, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Fotos diarias de Siesa
# ──────────────────────────────────────────────────────────────────────────────

_FOTOS = {
    'VENTAS': ('foto_ventas', 'Foto diaria de ventas (facturas)',
               'Venta real por CO, fugas de facturación, valor de la factura'),
    'STOCK': ('foto_stock', 'Foto diaria de existencias y costo',
              'Historia de stock e inventario valorizado'),
    'CARTERA': ('foto_cartera', 'Foto diaria de cartera',
                'Cartera abierta, ciclo de caja'),
}


def _alcances_esperados(tipo):
    from app.services import fotos_siesa_service as fotos
    from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
    if tipo == 'VENTAS':
        return list(fotos.cos_operados())
    if tipo == 'STOCK':
        return list(_BODEGAS_INVENTARIO)
    return ['TODAS']


def _dia_foto_esperado(ahora_utc):
    local = _a_bogota(ahora_utc)
    hoy = local.date()
    return hoy if local.time() >= HORA_FOTO_DEL_DIA else hoy - timedelta(days=1)


def fuente_foto(tipo, ahora_utc, activos=()):
    import json
    clave, nombre, alimenta = _FOTOS[tipo]
    try:
        from app.models.fotos_siesa import FotoCorrida
        from app.services import fotos_siesa_service as fotos
        encendido = fotos.encendido()
        esperados = _alcances_esperados(tipo)
        corridas = (FotoCorrida.query.filter(FotoCorrida.tipo == tipo)
                    .order_by(FotoCorrida.dia_operativo.desc(), FotoCorrida.id.desc())
                    .limit(500).all())
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible(clave, nombre, e, alimenta)

    kw = dict(alimenta=alimenta, cron=('[FOTOS_SIESA]', 'worker'), activos=activos)
    esperado = _dia_foto_esperado(ahora_utc)
    interruptor = ('FOTOS_SIESA está apagado en este proceso' if not encendido
                   else 'FOTOS_SIESA está encendido en este proceso')
    base = {'dia_esperado': esperado.isoformat(), 'interruptor_en_este_proceso': encendido,
            'alcances_esperados': esperados}
    if not corridas:
        if not encendido:
            return _fuente(clave, nombre, APAGADA,
                           'Nunca corrió y ' + interruptor + '.',
                           'Encender FOTOS_SIESA=true en el worker (con '
                           'HEAVY_SCHEDULERS=true) después de verificar con '
                           'scripts/qa_fotos_siesa_real.py.', detalle=base, **kw)
        return _fuente(clave, nombre, SIN_DATOS,
                       'Encendida pero sin ninguna corrida registrada.',
                       'Verificar que el worker tenga HEAVY_SCHEDULERS=true y que '
                       '[FOTOS_SIESA] arrancó.', detalle=base, **kw)

    completas = {}
    for c in corridas:
        if c.completa:
            completas.setdefault(c.alcance, c)       # la de día más reciente
    if not completas:
        ult = corridas[0]
        return _fuente(clave, nombre, INCOMPLETA,
                       'Hubo corridas pero ninguna completa. Última: '
                       + str(ult.motivo or 'sin motivo')[:200],
                       'Revisar el motivo: sin una corrida completa no hay total '
                       '(es un hueco, no un cero).',
                       ultima=ult.dia_operativo.isoformat(), completa=False,
                       detalle=base, **kw)

    ultimo_dia = max(c.dia_operativo for c in completas.values())
    faltan = [a for a in esperados
              if a not in completas or completas[a].dia_operativo < ultimo_dia]
    base.update({
        'ultimo_dia_completo': ultimo_dia.isoformat(),
        'alcances_sin_corrida_completa_ese_dia': faltan,
        'ultima_por_alcance': {a: c.to_dict() for a, c in completas.items()},
    })
    ultima = ultimo_dia.isoformat()
    if ultimo_dia < esperado:
        if not encendido:
            return _fuente(clave, nombre, APAGADA,
                           f'La última foto completa es del {ultima} y {interruptor}.',
                           'Encender FOTOS_SIESA=true en el worker. Los días sin foto '
                           'no se recuperan: Siesa no guarda la historia.',
                           ultima=ultima, completa=True, detalle=base, **kw)
        return _fuente(clave, nombre, ATRASADA,
                       f'La última foto completa es del {ultima}; se esperaba la '
                       f'del {esperado.isoformat()}.',
                       'Revisar el log [FOTOS_SIESA] del worker (ventana de Siesa, '
                       'credenciales, lock).', ultima=ultima, completa=True,
                       detalle=base, **kw)
    if faltan:
        return _fuente(clave, nombre, INCOMPLETA,
                       f'El {ultima} quedaron sin corrida completa: '
                       + ', '.join(faltan[:12]) + ('…' if len(faltan) > 12 else '') + '.',
                       'Esos alcances no tienen total ese día (hueco, no cero). '
                       'Revisar el motivo de su corrida.',
                       ultima=ultima, completa=False, detalle=base, **kw)
    if tipo == 'STOCK':
        sin_costo = []
        from app.services.inventario_siesa_service import _BODEGAS_PV
        for a, c in completas.items():
            if a not in _BODEGAS_PV:
                continue
            try:
                det = json.loads(c.detalle or '{}')
            except ValueError:
                det = {}
            if not (det.get('costo') or {}).get('completo'):
                sin_costo.append(a)
        if sin_costo:
            base['costo_incompleto_en'] = sorted(sin_costo)
            return _fuente(clave, nombre, INCOMPLETA,
                           f'Cantidades completas; el costo quedó incompleto en '
                           f'{len(sin_costo)} bodega(s): ' + ', '.join(sorted(sin_costo))
                           + '. Los valores en pesos del inventario no son totales.',
                           'Pedir al consultor un ORDER BY estable en '
                           'API_v2_Inventarios_InvFecha (paginación inestable).',
                           ultima=ultima, completa=False, detalle=base, **kw)
    return _fuente(clave, nombre, AL_DIA,
                   f'Foto completa del {ultima} en {len(esperados)} alcance(s).',
                   None, ultima=ultima, completa=True, detalle=base, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Kardex — el veredicto de `salud_kardex`, traducido
# ──────────────────────────────────────────────────────────────────────────────

#: Código de `salud_kardex` → veredicto de esta pieza.
_KARDEX_A_VEREDICTO = {
    'AL_DIA': AL_DIA,
    'SIN_DATOS': SIN_DATOS,
    'SIN_LECTURA_DEL_REGISTRO': SIN_DATOS,
    'DESACTUALIZADO': ATRASADA,
    'DESCARGA_EN_CURSO': INCOMPLETA,
    'DESCARGA_INTERRUMPIDA': INCOMPLETA,
    'ULTIMA_DESCARGA_INCOMPLETA': INCOMPLETA,
    'SIN_DESCARGA_REGISTRADA': INCOMPLETA,
    'CONCEPTOS_SIN_CLASIFICAR': INCOMPLETA,
    'STOCK_DIARIO_ATRASADO': INCOMPLETA,
}


def fuente_kardex(activos=()):
    nombre = 'Kardex (movimientos de inventario)'
    alimenta = 'Demanda para compras (Armador), costo, temporada'
    try:
        from app.services.kardex_service import salud_kardex
        s = salud_kardex()
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible('kardex', nombre, e, alimenta)
    # Un código nuevo en salud_kardex que esta tabla no conoce NO se lee como
    # al día: se declara incompleto.
    veredicto = _KARDEX_A_VEREDICTO.get(s.get('veredicto'), INCOMPLETA)
    problemas = s.get('problemas') or []
    mov = s.get('movimientos') or {}
    motivo = (problemas[0]['titulo'] if problemas else
              f"Último movimiento del {mov.get('ultima_fecha')}.")
    que = problemas[0]['que_hacer'] if problemas else None
    return _fuente('kardex', nombre, veredicto, motivo,
                   (que or '') + ' Nada lo actualiza solo: se descarga a mano.',
                   alimenta=alimenta, ultima=mov.get('ultima_fecha'),
                   completa=bool(s.get('confiable')),
                   detalle={'veredicto_kardex': s.get('veredicto'),
                            'problemas': problemas, 'movimientos': mov,
                            'actualizacion_automatica': s.get('actualizacion_automatica')},
                   activos=activos)


# ──────────────────────────────────────────────────────────────────────────────
# Vigía
# ──────────────────────────────────────────────────────────────────────────────

_SERIES_ADOPCION = ('adopcion_picking', 'brecha_picking')
_PREFIJOS_FACTURACION = ('facturacion_', 'despachos_', 'facturas_')


def _semana_vigia_esperada(ahora_utc):
    """El lunes de la última semana CERRADA que el cron ya debió cargar.

    Mismo criterio que `vigia_service._lunes_semana_actual` (lunes Bogotá),
    calculado sobre `ahora_utc` para que se pueda probar a cualquier hora.
    """
    local = _a_bogota(ahora_utc)
    lunes = local.date() - timedelta(days=local.weekday())
    corrio = local.date() > lunes or local.time() >= HORA_VIGIA_LUNES
    return lunes - timedelta(days=7 if corrio else 14)


def fuente_vigia_adopcion(ahora_utc, activos=()):
    nombre = 'Vigía — adopción del picking (semanal)'
    alimenta = 'Alarma CUSUM de adopción del WMS'
    try:
        from sqlalchemy import func
        from app.services.vigia_service import SerieVigia
        ultima = (db.session.query(func.max(SerieVigia.semana))
                  .filter(SerieVigia.serie.in_(_SERIES_ADOPCION)).scalar())
        esperada = _semana_vigia_esperada(ahora_utc)
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible('vigia_adopcion', nombre, e, alimenta)
    kw = dict(alimenta=alimenta, cron=('[VIGIA_SCHEDULER]', 'web'), activos=activos,
              detalle={'semana_esperada': esperada.isoformat()})
    if ultima is None:
        return _fuente('vigia_adopcion', nombre, SIN_DATOS,
                       'La serie de adopción nunca se alimentó.',
                       'Vigía → «Alimentar adopción» o esperar el cron de los lunes 05:30.',
                       **kw)
    if ultima < esperada:
        return _fuente('vigia_adopcion', nombre, ATRASADA,
                       f'Última semana {ultima.isoformat()}; se esperaba la del '
                       f'{esperada.isoformat()}.',
                       'Verificar [VIGIA_SCHEDULER] en la web o alimentar a mano.',
                       ultima=ultima.isoformat(), completa=True, **kw)
    return _fuente('vigia_adopcion', nombre, AL_DIA,
                   f'Semana del {ultima.isoformat()} cargada.', None,
                   ultima=ultima.isoformat(), completa=True, **kw)


def fuente_vigia_facturacion(ahora_utc, activos=()):
    import os
    nombre = 'Vigía — facturación por CO (semanal)'
    alimenta = 'Alarma CUSUM de desplome de facturación'
    try:
        from sqlalchemy import func, or_
        from app.services.vigia_service import SerieVigia
        filtro = or_(*[SerieVigia.serie.like(p + '%') for p in _PREFIJOS_FACTURACION])
        filas = (db.session.query(SerieVigia.fuente, func.max(SerieVigia.semana))
                 .filter(filtro).group_by(SerieVigia.fuente).all())
        esperada = _semana_vigia_esperada(ahora_utc)
    except Exception as e:                                    # noqa: BLE001
        return _fuente_ilegible('vigia_facturacion', nombre, e, alimenta)
    encendida = os.getenv('VIGIA_INGESTA_FACTURACION', '').lower() == 'true'
    por_fuente = {f: s for f, s in filas}
    ultima = max(por_fuente.values(), default=None)
    kw = dict(alimenta=alimenta, cron=('[VIGIA_SCHEDULER]', 'web'), activos=activos,
              detalle={'semana_esperada': esperada.isoformat(),
                       'ultima_por_fuente': {k: v.isoformat() for k, v in por_fuente.items()},
                       'ingesta_encendida_en_este_proceso': encendida})
    if ultima is not None and ultima >= esperada:
        return _fuente('vigia_facturacion', nombre, AL_DIA,
                       f'Semana del {ultima.isoformat()} cargada.', None,
                       ultima=ultima.isoformat(), completa=True, **kw)
    if not encendida:
        return _fuente('vigia_facturacion', nombre, APAGADA,
                       'La ingesta semanal está apagada (VIGIA_INGESTA_FACTURACION)'
                       + (f'; lo último es del {ultima.isoformat()}' if ultima else
                          ' y no hay línea base cargada') + '.',
                       'Vigía → «Verificar ingesta de facturación» sobre un lunes ya '
                       'cargado; si da apto, encender la variable.',
                       ultima=_iso(ultima), **kw)
    if ultima is None:
        return _fuente('vigia_facturacion', nombre, SIN_DATOS,
                       'Encendida pero sin ninguna semana cargada.',
                       'Cargar la línea base (TXT) y revisar el log [VIGIA_SCHEDULER].',
                       **kw)
    return _fuente('vigia_facturacion', nombre, ATRASADA,
                   f'Última semana {ultima.isoformat()}; se esperaba la del '
                   f'{esperada.isoformat()}.',
                   'Revisar el log [VIGIA_SCHEDULER]: un CO sin dato deja hueco, no cero.',
                   ultima=ultima.isoformat(), completa=True, **kw)


# ──────────────────────────────────────────────────────────────────────────────
# Crons de este proceso
# ──────────────────────────────────────────────────────────────────────────────

def crons_del_proceso():
    """Lo mismo que `/api/health/siesa` publica en `schedulers`, más quién es
    este proceso. **No** dice nada del otro proceso: no lo puede ver."""
    import os
    try:
        from flask import current_app
        activos = list(current_app.config.get('SCHEDULERS_ACTIVOS') or [])
        omitidos = list(current_app.config.get('SCHEDULERS_OMITIDOS') or [])
    except Exception:                                         # noqa: BLE001
        activos, omitidos = [], []
    heavy = os.getenv('HEAVY_SCHEDULERS', 'false').lower() == 'true'
    sin_esenciales = os.getenv('WORKER_SKIP_ESSENTIAL', 'false').lower() == 'true'
    rol = ('worker' if heavy and sin_esenciales else
           'web + worker (todo en uno)' if heavy else 'web')
    return {
        'rol_de_este_proceso': rol,
        'heavy_schedulers': heavy,
        'worker_skip_essential': sin_esenciales,
        'activos': activos,
        'omitidos': omitidos,
        'alertas_por_correo': '[ALERTAS_SCHEDULER]' in activos,
        'nota': ('La web y el worker son procesos distintos. Esta lista es la del '
                 'proceso que contestó (normalmente la web: DLQ, pedidos, Vigía, '
                 'reposición). Los crons del worker (fotos, refresco de '
                 'existencias, alertas por correo, sync de catálogo) no se ven '
                 'desde acá: su evidencia es la frescura de su dato, arriba. '
                 '`/api/health/siesa` en cada servicio publica su propia lista.'),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Invariantes de auditoría — con tope
# ──────────────────────────────────────────────────────────────────────────────

_CACHE_AUDITORIA = {'resultado': None, 'calculado_utc': None}


def _resumen_de_flujo(flujo, rep):
    rot = [r for r in rep['resultados'] if r['total'] or r['error']]
    peso = {'BLOQUEA': 0, 'AVISA': 1, 'OBSERVA': 2}
    rot.sort(key=lambda r: (peso.get(r['severidad'], 3), -r['total']))
    return {
        'flujo': flujo,
        'invariantes': rep['invariantes_corridos'],
        'rotos': rep['invariantes_rotos'],
        'bloqueantes': rep['bloqueantes'],
        'avisos': sum(r['total'] for r in rep['resultados'] if r['severidad'] == 'AVISA'),
        'observaciones': sum(r['total'] for r in rep['resultados']
                             if r['severidad'] == 'OBSERVA'),
        'errores': rep['errores'],
        'consultas_truncadas': rep['consultas_truncadas'],
        'peores': [{
            'codigo': r['codigo'], 'severidad': r['severidad'], 'frontera': r['frontera'],
            'consecuencia': r['consecuencia'], 'total': r['total'],
            'error': r['error'],
            'ejemplos': [h['referencia'] for h in r['hallazgos'][:3]],
        } for r in rot[:5]],
    }


def resumen_auditoria(ahora_utc=None, forzar=False, reloj=_time_mod.monotonic):
    """Los invariantes BLOQUEA/AVISA vigentes, por flujo.

    Tope declarado: resultado guardado `AUDITORIA_TTL` en el proceso; al
    recalcular, no se empieza un flujo nuevo pasado `AUDITORIA_PRESUPUESTO_S`.
    """
    ahora_utc = ahora_utc or datetime.utcnow()
    c = _CACHE_AUDITORIA
    if (not forzar and c['resultado'] is not None
            and ahora_utc - c['calculado_utc'] < AUDITORIA_TTL):
        res = dict(c['resultado'])
        res['desde_cache'] = True
        return res

    from app.services import auditoria as aud
    inicio = reloj()
    por_flujo, no_evaluados = [], []
    try:
        todos = aud.flujos()
    except Exception as e:                                    # noqa: BLE001
        return {'error': f'{type(e).__name__}: {e}'[:200], 'por_flujo': [],
                'flujos_no_evaluados': [], 'bloqueantes': None, 'avisos': None,
                'nivel': ADVERTENCIA, 'calculado_utc': _iso(ahora_utc), 'tope': _tope()}
    for f in todos:
        if reloj() - inicio > AUDITORIA_PRESUPUESTO_S:
            no_evaluados.append(f)
            continue
        try:
            por_flujo.append(_resumen_de_flujo(f, aud.auditar(f)))
        except Exception as e:                                # noqa: BLE001
            por_flujo.append({'flujo': f, 'invariantes': 0, 'rotos': 0,
                              'bloqueantes': 0, 'avisos': 0, 'observaciones': 0,
                              'errores': [f'{type(e).__name__}: {e}'[:200]],
                              'consultas_truncadas': [], 'peores': []})
    bloq = sum(x['bloqueantes'] for x in por_flujo)
    avisos = sum(x['avisos'] for x in por_flujo)
    errores = [e for x in por_flujo for e in x['errores']]
    truncadas = [q for x in por_flujo for q in x['consultas_truncadas']]
    if bloq:
        nivel = CRITICO
    elif avisos or errores or no_evaluados or truncadas:
        nivel = ADVERTENCIA
    else:
        nivel = OK
    res = {
        'por_flujo': por_flujo,
        'flujos_no_evaluados': no_evaluados,
        'bloqueantes': bloq,
        'avisos': avisos,
        'errores': errores,
        'consultas_truncadas': truncadas,
        'nivel': nivel,
        'calculado_utc': _iso(ahora_utc),
        'segundos': round(reloj() - inicio, 2),
        'desde_cache': False,
        'tope': _tope(),
        'nota': ('Los invariantes miran el estado ACTUAL de la base, no el rango '
                 'de fechas ni el almacén elegidos.'),
    }
    c['resultado'], c['calculado_utc'] = res, ahora_utc
    return dict(res)


def _tope():
    return {'ttl_min': int(AUDITORIA_TTL.total_seconds() // 60),
            'presupuesto_s': AUDITORIA_PRESUPUESTO_S,
            'texto': (f'Resultado guardado {int(AUDITORIA_TTL.total_seconds() // 60)} '
                      f'min; al recalcular no se empieza un flujo nuevo pasados '
                      f'{int(AUDITORIA_PRESUPUESTO_S)} s. Los flujos que no alcanzaron '
                      'se listan.')}


# ──────────────────────────────────────────────────────────────────────────────
# Cola de Siesa
# ──────────────────────────────────────────────────────────────────────────────

def cola_siesa(ahora_utc):
    from sqlalchemy import func
    from app.models.siesa_job import EstadoSiesaJob as E, SiesaJob
    try:
        por_estado = dict(db.session.query(SiesaJob.estado, func.count(SiesaJob.id))
                          .group_by(SiesaJob.estado).all())
        fallidos_tipo = (db.session.query(SiesaJob.tipo, func.count(SiesaJob.id),
                                          func.min(SiesaJob.fecha_creacion))
                         .filter(SiesaJob.estado == E.FALLIDO)
                         .group_by(SiesaJob.tipo).all())
        vivos = (E.PENDIENTE, E.REINTENTANDO, E.PROCESANDO)
        mas_viejo = (db.session.query(func.min(SiesaJob.fecha_creacion))
                     .filter(SiesaJob.estado.in_(vivos)).scalar())
        colgados = (SiesaJob.query.filter(
            SiesaJob.estado == E.PROCESANDO,
            SiesaJob.fecha_procesando < ahora_utc - TOLERANCIA_JOB_PROCESANDO).count())
    except Exception as e:                                    # noqa: BLE001
        return {'error': str(e)[:200], 'nivel': ADVERTENCIA,
                'texto': 'No se pudo leer la cola de Siesa.'}
    fallidos = int(por_estado.get(E.FALLIDO, 0))
    pendientes = sum(int(por_estado.get(x, 0)) for x in vivos)
    edad = tiempo_operativo(mas_viejo, ahora_utc) if mas_viejo else None
    atascada = edad is not None and edad > TOLERANCIA_JOB_PENDIENTE
    if fallidos:
        nivel = CRITICO
        texto = (f'{fallidos} envío(s) a Siesa en FALLIDO: documentos (factura, '
                 'recibo, ajuste…) que el WMS dejó de intentar.')
        que = 'Revisar y reintentar o descartar desde el panel de la DLQ.'
    elif colgados or atascada:
        nivel = ADVERTENCIA
        texto = (f'{pendientes} envío(s) pendientes; el más viejo lleva '
                 f'{_texto_duracion(edad)} operativas'
                 + (f', {colgados} colgado(s) en PROCESANDO' if colgados else '') + '.')
        que = 'Verificar que la DLQ corra en la web ([DLQ_SCHEDULER]) y que Siesa responda.'
    else:
        nivel = OK
        texto = (f'{pendientes} envío(s) en cola, ninguno fallido.' if pendientes
                 else 'Cola de Siesa vacía, ninguno fallido.')
        que = None
    return {
        'fallidos': fallidos,
        'pendientes': pendientes,
        'colgados_procesando': colgados,
        'pendiente_mas_viejo_utc': _iso(mas_viejo),
        'edad_operativa_min': int(edad.total_seconds() // 60) if edad is not None else None,
        'por_estado': {k: int(v) for k, v in por_estado.items()},
        'fallidos_por_tipo': [{'tipo': t, 'n': int(n), 'desde_utc': _iso(d)}
                              for t, n, d in sorted(fallidos_tipo, key=lambda x: -x[1])],
        'nivel': nivel,
        'texto': texto,
        'que_hacer': que,
        'tolerancias': {'pendiente_min': int(TOLERANCIA_JOB_PENDIENTE.total_seconds() // 60),
                        'procesando_min': int(TOLERANCIA_JOB_PROCESANDO.total_seconds() // 60)},
    }


# ──────────────────────────────────────────────────────────────────────────────
# Cobertura de claves de la cadena
# ──────────────────────────────────────────────────────────────────────────────

def cobertura_claves(desde, hasta, almacen_id=None):
    """% de tareas de PEDIDO (picking y packing) creadas en el rango que
    llevan `pedido_clave`. Sin clave la tarea no se puede unir a su pedido,
    a su factura ni a su cobro: queda fuera del recorrido."""
    from sqlalchemy import func
    from app.models.packing import TareaPacking
    from app.models.picking import TareaPicking
    from app.services.cadena_pedido import TIPOS_DE_PEDIDO
    ini, fin = rango_dia_operativo_utc(desde, hasta)
    out = {}
    for clave, M in (('picking', TareaPicking), ('packing', TareaPacking)):
        q = (db.session.query(func.count(M.id), func.count(M.pedido_clave))
             .filter(M.tipo_documento.in_(TIPOS_DE_PEDIDO),
                     M.fecha_creacion >= ini, M.fecha_creacion < fin))
        if almacen_id:
            q = q.filter(M.almacen_id == int(almacen_id))
        n, con = q.one()
        n, con = int(n or 0), int(con or 0)
        out[clave] = {'n': n, 'con_clave': con, 'sin_clave': n - con,
                      'pct': (con / n) if n else None}
    n = sum(v['n'] for v in out.values())
    con = sum(v['con_clave'] for v in out.values())
    pct = (con / n) if n else None
    if pct is None:
        nivel = OK
        texto = 'Sin tareas de pedido en el rango: no hay nada que unir (no es 0 %).'
    elif pct < UMBRAL_COBERTURA_CRITICO:
        nivel = CRITICO
        texto = (f'Solo {round(pct * 100)} % de {n} tareas de pedido tienen clave: '
                 'el recorrido pedido → caja pierde las demás.')
    elif pct < UMBRAL_COBERTURA_OK:
        nivel = ADVERTENCIA
        texto = (f'{round(pct * 100)} % de {n} tareas de pedido tienen clave '
                 f'(esperado ≥ {round(UMBRAL_COBERTURA_OK * 100)} %).')
    else:
        nivel = OK
        texto = f'{round(pct * 100)} % de {n} tareas de pedido tienen clave.'
    return {**out, 'n': n, 'con_clave': con, 'pct': pct, 'nivel': nivel, 'texto': texto,
            'que_hacer': (None if nivel == OK else
                          'Las tareas sin clave vienen de pedidos cuyo CO no se pudo '
                          'resolver (pedido en varios CO o almacén sin centro de '
                          'operación). Revisar almacenes.centro_op_siesa.'),
            'umbrales': {'ok': UMBRAL_COBERTURA_OK, 'critico': UMBRAL_COBERTURA_CRITICO}}


# ──────────────────────────────────────────────────────────────────────────────
# El veredicto global
# ──────────────────────────────────────────────────────────────────────────────

def veredicto_global(fuentes, auditoria, cola, cobertura):
    """`CONFIABLE` solo si TODO está en `ok`. **Una política, una función.**"""
    razones = []
    for f in fuentes:
        if f['nivel'] != OK:
            razones.append({'nivel': f['nivel'], 'seccion': 'fuente', 'clave': f['clave'],
                            'texto': f"{f['nombre']}: {f['veredicto_texto'].lower()} — "
                                     f"{f['motivo']}",
                            'que_hacer': f['que_hacer']})
    if auditoria.get('nivel') != OK:
        partes = []
        if auditoria.get('bloqueantes'):
            partes.append(f"{auditoria['bloqueantes']} hallazgo(s) que bloquean")
        if auditoria.get('avisos'):
            partes.append(f"{auditoria['avisos']} aviso(s)")
        if auditoria.get('flujos_no_evaluados'):
            partes.append('flujos sin mirar: ' + ', '.join(auditoria['flujos_no_evaluados']))
        if auditoria.get('errores'):
            partes.append(f"{len(auditoria['errores'])} invariante(s) con error")
        if auditoria.get('consultas_truncadas'):
            partes.append('consultas que llegaron a su tope')
        if auditoria.get('error'):
            partes.append('la auditoría no corrió')
        razones.append({'nivel': auditoria.get('nivel', ADVERTENCIA), 'seccion': 'auditoria',
                        'clave': 'auditoria',
                        'texto': 'Invariantes entre etapas: ' + ('; '.join(partes) or 'con reservas'),
                        'que_hacer': 'Abrir el detalle por flujo y resolver primero lo que bloquea.'})
    if cola.get('nivel') != OK:
        razones.append({'nivel': cola['nivel'], 'seccion': 'cola_siesa', 'clave': 'cola_siesa',
                        'texto': cola.get('texto'), 'que_hacer': cola.get('que_hacer')})
    if cobertura.get('nivel') != OK:
        razones.append({'nivel': cobertura['nivel'], 'seccion': 'cobertura',
                        'clave': 'cobertura', 'texto': cobertura.get('texto'),
                        'que_hacer': cobertura.get('que_hacer')})
    niveles = {r['nivel'] for r in razones}
    if CRITICO in niveles:
        v = NO_CONFIABLE
    elif niveles:
        v = CON_RESERVAS
    else:
        v = CONFIABLE
    razones.sort(key=lambda r: 0 if r['nivel'] == CRITICO else 1)
    return {'veredicto': v, 'texto': TEXTO_GLOBAL[v], 'razones': razones}


def salud_del_dato(desde, hasta, almacen_id=None, ahora_utc=None):
    """La respuesta de `GET /api/analitica/salud`."""
    ahora_utc = ahora_utc or datetime.utcnow()
    crons = crons_del_proceso()
    activos = crons['activos']
    fuentes = [
        fuente_pedidos(ahora_utc, activos),
        fuente_stock(ahora_utc, almacen_id, activos),
        fuente_foto('VENTAS', ahora_utc, activos),
        fuente_foto('STOCK', ahora_utc, activos),
        fuente_foto('CARTERA', ahora_utc, activos),
        fuente_kardex(activos),
        fuente_vigia_adopcion(ahora_utc, activos),
        fuente_vigia_facturacion(ahora_utc, activos),
    ]
    auditoria = resumen_auditoria(ahora_utc)
    cola = cola_siesa(ahora_utc)
    cobertura = cobertura_claves(desde, hasta, almacen_id)
    glob = veredicto_global(fuentes, auditoria, cola, cobertura)
    al_dia = sum(1 for f in fuentes if f['veredicto'] == AL_DIA)
    return {
        'global': glob,
        'resumen': {
            'fuentes_al_dia': al_dia,
            'fuentes_total': len(fuentes),
            'bloqueantes': auditoria.get('bloqueantes'),
            'avisos': auditoria.get('avisos'),
            'jobs_fallidos': cola.get('fallidos'),
            'cobertura_clave_pct': cobertura.get('pct'),
            'cobertura_clave_n': cobertura.get('n'),
        },
        'fuentes': fuentes,
        'crons': crons,
        'auditoria': auditoria,
        'cola_siesa': cola,
        'cobertura_claves': cobertura,
        'meta': {
            'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
            'almacen_id': almacen_id,
            'calculado_en': ahora_utc.isoformat(),
            'hoy_bogota': dia_operativo_de(ahora_utc).isoformat(),
            'respeta_filtros': {
                'rango': ['cobertura_claves'],
                'almacen': ['cobertura_claves', 'stock_siesa'],
                'nota': ('La salud de una fuente es de HOY, no del rango: una fuente '
                         'apagada hoy no se enciende mirando el mes pasado.'),
            },
            'fuentes': {f['clave']: {'veredicto': f['veredicto'],
                                     'ultima_actualizacion': f['ultima_actualizacion'],
                                     'completa': f['completa']} for f in fuentes},
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Bitácora legible
# ══════════════════════════════════════════════════════════════════════════════

#: El verbo en pasado, como se lee en la lista.
VERBOS = {
    'ELIMINAR': 'eliminó', 'ANULAR': 'anuló', 'CANCELAR': 'canceló',
    'REABRIR': 'reabrió', 'EDITAR': 'editó', 'REASIGNAR': 'reasignó',
    'DESASIGNAR': 'desasignó', 'REINTENTAR': 'reintentó', 'DESCARTAR': 'descartó',
    'FORZAR': 'forzó el cierre de', 'LIQUIDAR': 'liquidó',
    'DESACTIVAR': 'desactivó', 'BLOQUEAR': 'bloqueó',
}

#: Palabras de bodega, no clases del modelo. Lo que no está acá se muestra
#: con su nombre partido («Mapeo unidades»): nunca se oculta.
ENTIDADES = {
    'TareaPicking': 'el picking', 'TareaPacking': 'el packing',
    'ItemPacking': 'la línea de packing', 'Bulto': 'el bulto',
    'SiesaJob': 'el envío a Siesa', 'RutaDespacho': 'la ruta',
    'RutaMaestra': 'la ruta maestra', 'ParadaRutaMaestra': 'la parada de ruta maestra',
    'RecaudoEntrega': 'la entrega', 'Producto': 'el producto',
    'Conductor': 'el conductor', 'Vehiculo': 'el vehículo',
    'Ubicacion': 'la ubicación', 'UbicacionProducto': 'la asignación de ubicación',
    'SesionConteo': 'el conteo', 'RecepcionMercancia': 'la recepción',
    'ItemRecepcion': 'la línea de recepción', 'SolicitudTraslado': 'el traslado',
    'TareaReposicion': 'la reposición', 'DevolucionCliente': 'la devolución de cliente',
    'LineaDevolucionCliente': 'la línea de devolución', 'TareaDevolucion': 'la tarea de devolución',
    'JuicioTemporada': 'el juicio de temporada', 'SiesaMapeoUnidades': 'el mapeo de unidades',
    'LPN': 'el LPN', 'MovimientoInventario': 'el movimiento de inventario',
    'Usuario': 'el usuario',
}

_CAMPOS_PEDIDO = ('pedido_clave', 'numero_pedido_siesa', 'referencia_doc',
                  'referencia_documento')


def nombre_entidad(entidad):
    if entidad in ENTIDADES:
        return ENTIDADES[entidad]
    import re
    partes = re.sub(r'(?<!^)(?=[A-Z])', ' ', entidad or '').lower()
    return partes or 'el registro'


def _valor_legible(v):
    if v is None or v == '':
        return '—'
    if isinstance(v, bool):
        return 'sí' if v else 'no'
    if isinstance(v, (dict, list)):
        import json
        s = json.dumps(v, ensure_ascii=False, default=str)
    else:
        s = str(v)
    return s if len(s) <= 120 else s[:117] + '…'


def _cambios(antes, despues, accion):
    antes = antes if isinstance(antes, dict) else {}
    despues = despues if isinstance(despues, dict) else {}
    out = []
    for k in list(antes.keys()) + [k for k in despues.keys() if k not in antes]:
        a, d = antes.get(k), despues.get(k)
        if accion != 'ELIMINAR' and a == d:
            continue
        out.append({'campo': k.replace('_', ' '), 'antes': _valor_legible(a),
                    'despues': ('(borrado)' if accion == 'ELIMINAR' and k not in despues
                                else _valor_legible(d))})
    return out[:15], max(0, len(out) - 15)


def _documento(accion_dict, vivos):
    """El pedido (o traslado) al que pertenece la acción, o `None`.

    Primero lo que la propia fila guardó (sobrevive al borrado); después la
    tarea viva, si la entidad es una tarea y todavía existe.
    """
    for fuente in (accion_dict.get('antes'), accion_dict.get('despues')):
        if not isinstance(fuente, dict):
            continue
        tipo = str(fuente.get('tipo_documento') or '').upper()
        for c in _CAMPOS_PEDIDO:
            v = fuente.get(c)
            if v:
                return {'tipo': 'traslado' if tipo == 'TRASLADO' else 'pedido',
                        'codigo': str(v)}
    viva = vivos.get((accion_dict.get('entidad'), accion_dict.get('entidad_id')))
    return viva


def _tareas_vivas(filas):
    from app.models.packing import TareaPacking
    from app.models.picking import TareaPicking
    out = {}
    ids = {'TareaPicking': set(), 'TareaPacking': set()}
    for f in filas:
        if f.get('entidad') in ids and f.get('entidad_id'):
            ids[f['entidad']].add(f['entidad_id'])
    if ids['TareaPicking']:
        for t in TareaPicking.query.filter(TareaPicking.id.in_(ids['TareaPicking'])).all():
            cod = t.pedido_clave or t.referencia_documento
            if cod:
                out[('TareaPicking', t.id)] = {
                    'tipo': 'traslado' if (t.tipo_documento or '').upper() == 'TRASLADO'
                    else 'pedido', 'codigo': cod}
    if ids['TareaPacking']:
        for t in TareaPacking.query.filter(TareaPacking.id.in_(ids['TareaPacking'])).all():
            cod = t.pedido_clave or t.numero_pedido_siesa or t.referencia_doc
            if cod:
                out[('TareaPacking', t.id)] = {
                    'tipo': 'traslado' if (t.tipo_documento or '').upper() == 'TRASLADO'
                    else 'pedido', 'codigo': cod}
    return out


def _nombres(modelo, ids):
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {r.id: r.nombre for r in modelo.query.filter(modelo.id.in_(ids)).all()}


def describir_acciones(filas):
    """Agrega a cada `to_dict()` de la bitácora lo que una persona lee:
    quién (nombre), qué (frase), de qué pedido, cuándo (hora Bogotá), dónde
    (almacén) y qué cambió (antes → después campo por campo, sin JSON crudo).

    **Solo lee**: no toca la lógica de registro.
    """
    from app.models.almacen import Almacen
    from app.models.usuario import Usuario
    usuarios = _nombres(Usuario, [f.get('usuario_id') for f in filas])
    almacenes = _nombres(Almacen, [f.get('almacen_id') for f in filas])
    vivos = _tareas_vivas(filas)
    for f in filas:
        uid = f.get('usuario_id')
        if uid is None:
            quien = 'El sistema'
        else:
            quien = usuarios.get(uid) or f'Usuario #{uid} (ya no existe)'
        doc = _documento(f, vivos)
        cod = f.get('entidad_codigo') or (f"#{f['entidad_id']}" if f.get('entidad_id') else '')
        frase = f"{quien} {VERBOS.get(f.get('accion'), (f.get('accion') or '').lower())} " \
                f"{nombre_entidad(f.get('entidad'))}"
        if cod:
            frase += f' {cod}'
        if doc and doc['codigo'] != f.get('entidad_codigo'):
            frase += f" del {doc['tipo']} {doc['codigo']}"
        momento = _parse_iso(f.get('ocurrido_en'))
        cambios, mas = _cambios(f.get('antes'), f.get('despues'), f.get('accion'))
        f.update({
            'usuario_nombre': quien,
            'almacen_nombre': (almacenes.get(f.get('almacen_id'))
                               if f.get('almacen_id') is not None else None),
            'entidad_nombre': nombre_entidad(f.get('entidad')),
            'verbo': VERBOS.get(f.get('accion')),
            'documento': doc,
            'frase': frase,
            'hora_bogota': _a_bogota(momento).strftime('%H:%M') if momento else None,
            'cambios': cambios,
            'cambios_omitidos': mas,
        })
    return filas


def _query_bitacora(desde, hasta, almacen_id=None, accion=None, entidad=None,
                    usuario_id=None):
    from app.models.bitacora import BitacoraAccion as B
    q = B.query.filter(B.dia_operativo >= desde, B.dia_operativo <= hasta)
    if almacen_id:
        q = q.filter(B.almacen_id == int(almacen_id))
    if accion:
        q = q.filter(B.accion == accion)
    if entidad:
        q = q.filter(B.entidad == entidad)
    if usuario_id is not None:
        q = q.filter(B.usuario_id == int(usuario_id))
    return q


def patrones_bitacora(desde, hasta, almacen_id=None, accion=None, entidad=None,
                      usuario_id=None):
    """Por persona, por acción, por entidad y por hora del día (Bogotá), cada
    uno con su `n`. `opciones` se calcula solo con rango y almacén, para que
    los selectores no se encojan al filtrar."""
    from sqlalchemy import func
    from app.models.bitacora import BitacoraAccion as B
    from app.models.usuario import Usuario
    from app.services.bitacora import ACCIONES

    base = _query_bitacora(desde, hasta, almacen_id)
    filtrada = _query_bitacora(desde, hasta, almacen_id, accion, entidad, usuario_id)

    def _grupo(q, col):
        return q.with_entities(col, func.count(B.id)).group_by(col).all()

    total = filtrada.count()
    sin_motivo = filtrada.filter((B.motivo.is_(None)) | (B.motivo == '')).count()
    por_persona_accion = (filtrada.with_entities(B.usuario_id, B.accion, func.count(B.id))
                          .group_by(B.usuario_id, B.accion).all())
    base_personas = _grupo(base, B.usuario_id)
    nombres = _nombres(Usuario, [u for u, _, _ in por_persona_accion] +
                       [u for u, _ in base_personas])

    def _nombre(uid):
        if uid is None:
            return 'El sistema'
        return nombres.get(uid) or f'Usuario #{uid} (ya no existe)'

    personas = {}
    for uid, acc, n in por_persona_accion:
        p = personas.setdefault(uid, {'usuario_id': uid, 'nombre': _nombre(uid), 'n': 0,
                                      'acciones': {}})
        p['n'] += n
        p['acciones'][acc] = n
    por_persona = sorted(personas.values(), key=lambda p: (-p['n'], p['nombre']))

    por_accion = sorted(({'accion': a, 'verbo': VERBOS.get(a), 'n': n}
                         for a, n in _grupo(filtrada, B.accion)), key=lambda x: -x['n'])
    por_entidad = sorted(({'entidad': e, 'nombre': nombre_entidad(e), 'n': n}
                          for e, n in _grupo(filtrada, B.entidad)), key=lambda x: -x['n'])

    horas = [0] * 24
    momentos = (filtrada.with_entities(B.ocurrido_en)
                .order_by(B.ocurrido_en.desc()).limit(BITACORA_TOPE_HORAS + 1).all())
    truncado = len(momentos) > BITACORA_TOPE_HORAS
    for (m,) in momentos[:BITACORA_TOPE_HORAS]:
        if m is not None:
            horas[_a_bogota(m).hour] += 1

    opciones = {
        'acciones': [{'accion': a, 'verbo': VERBOS.get(a)} for a in ACCIONES],
        'entidades': sorted(({'entidad': e, 'nombre': nombre_entidad(e), 'n': n}
                             for e, n in _grupo(base, B.entidad)), key=lambda x: -x['n']),
        'personas': sorted(({'usuario_id': u, 'nombre': _nombre(u), 'n': n}
                            for u, n in base_personas if u is not None),
                           key=lambda x: (-x['n'], x['nombre'])),
    }
    ultima = filtrada.with_entities(func.max(B.ocurrido_en)).scalar()
    return {
        'total': total,
        'sin_motivo': sin_motivo,
        'por_persona': por_persona,
        'por_accion': por_accion,
        'por_entidad': por_entidad,
        'por_hora': [{'hora': h, 'n': horas[h]} for h in range(24)],
        'por_hora_n': min(len(momentos), BITACORA_TOPE_HORAS),
        'opciones': opciones,
        'tope': {'filas_por_hora': BITACORA_TOPE_HORAS, 'truncado': truncado},
        'meta': {
            'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
            'almacen_id': almacen_id, 'accion': accion, 'entidad': entidad,
            'usuario_id': usuario_id,
            'calculado_en': datetime.utcnow().isoformat(),
            'fuentes': {'bitacora_acciones': {
                'ultima_accion_utc': _iso(ultima),
                'completa': True,
                'nota': ('Registra desde el deploy de m035bitacora; lo anterior no '
                         'tiene autor ni bitácora. Conteo y flota no escriben acá.'),
            }},
        },
    }


__all__ = [
    'VEREDICTOS', 'nivel_de', 'tiempo_operativo', 'salud_del_dato', 'veredicto_global',
    'resumen_auditoria', 'cola_siesa', 'cobertura_claves', 'crons_del_proceso',
    'describir_acciones', 'patrones_bitacora', 'VERBOS', 'ENTIDADES',
]
