"""
Fotos diarias de Siesa — la historia que Siesa no guarda (Fase 0 de analítica).

Bajo la Ley 1116 la caja vale más, y la analítica tiene que reconstruir el
recorrido pedido → caja con historia. **Siesa casi no tiene historia**: contesta
cómo están las cosas ahora. Lo que no se fotografía cada día no existe mañana.

Tres fotos, una vez al día, **solo lectura** contra Siesa:

| Foto | Fuente | Clave | Contrato |
|---|---|---|---|
| `foto_ventas_lineas` | `API_v2_Ventas_Facturas_DesdePedido`, por CO y día | `f470_rowid` | `docs/siesa-specs/45 API_v2_...docx` |
| `foto_stock_diaria` | `stock_siesa` + costo de `API_v2_Inventarios_InvFecha` | día × bodega × SKU | `API_v2_Inventarios_InvFecha.docx` |
| `foto_cartera_diaria` | `API_v2_CxC_General`, cuentas 1305 abiertas | día × `f353_rowid` | `API_v2_CxC_General.pdf` |

## Las reglas

1. **Nace apagado.** Sin `FOTOS_SIESA=true` no lee ni escribe nada: un cron que
   escribe no se enciende solo. El interruptor vive en `correr_fotos`, la
   función que escribe — no en el scheduler (dos sitios donde apagar lo mismo
   garantizan que se olvide uno).
2. **Nunca un cero por una lectura fallida.** Toda lectura es una corrida
   (`fotos_siesa_corridas`) con veredicto `completa` y motivo. Una corrida
   incompleta **no pisa filas existentes**: solo agrega las que no estaban,
   marcadas `completa=False`. Los totales salen de `filas_vigentes`, que
   devuelve `None` —hueco declarado— si no hay corrida completa.
3. **Rechazo ≠ página vacía.** `[{'alerta': …}]` es Siesa diciendo que no
   (`_exigir_datos`); la corrida queda incompleta con el texto de la alerta.
4. **Truncado ≠ completo.** Solo una página corta cierra una lectura. Agotar el
   tope de páginas es `completa=False`. Filas repetidas entre páginas
   (paginación inestable, ya medida en `API_v2_Inventarios_InvFecha`) piden una
   segunda pasada; si también repite, `completa=False`.
5. **El día es el de Bogotá** (`app/utils/fecha.py`). La foto de ventas usa el
   día del DOCUMENTO; la de stock y cartera, el día en que se tomó.
6. **Solo dentro de 7:00–19:30 Bogotá** (Regla 14: Siesa no opera después de
   ~8 p. m.). Si la ventana se cierra a mitad de camino, lo que falta no corre
   y se declara.
7. **Filtros con `lit`/`lit_fecha`** (Regla 15). `tamPag=100` (Regla 10).

## Lo que NO fotografía (declarado, no olvidado)

- **Kardex**: la consulta `…_KardexWMS` está bloqueada en QA.
- **Ventas POS de tienda**: no hay consulta que las traiga. Esta foto es de
  facturas **desde pedido**; lo que se vende por caja en tienda no aparece.
- **Notas crédito y recibos de caja** como documentos propios: la cartera los
  refleja en `total_cr`, no se fotografían línea a línea.
"""
import json
import logging
import os
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.fotos_siesa import (FotoCarteraDiaria, FotoCorrida,
                                    FotoStockDiaria, FotoVentaLinea, TipoFoto)
from app.services.siesa_filtro import lit, lit_fecha
from app.utils.fecha import ahora_bogota, dia_operativo, dia_operativo_de

logger = logging.getLogger(__name__)

API_VENTAS = 'API_v2_Ventas_Facturas_DesdePedido'
API_CARTERA = 'API_v2_CxC_General'
API_INVENTARIO = 'API_v2_Inventarios_InvFecha'

#: Regla 10: ≥ 500 trae registros fantasma. 100 es el máximo seguro.
TAM_PAG = 100
#: Medido en QA el 2026-09-24: cartera 1305 abierta = 47 páginas; InvFecha
#: NC1 = 69. Doscientas es holgura, no expectativa: agotarla es truncado.
MAX_PAGINAS = 200

VENTANA = (time(7, 0), time(19, 30))

#: Una fila de `stock_siesa` que no se refrescó en la última descarga de su
#: bodega (su `updated_at` quedó más de esto por detrás del de la bodega).
#: `_guardar_stock_en_bd` escribe una bodega entera en segundos.
_REZAGO_STOCK = timedelta(hours=1)

#: Las cuentas de cartera de clientes. Constante, no dato de usuario: no pasa
#: por `lit`, que rechaza `%` a propósito (es comodín de LIKE).
_FILTRO_CARTERA = "f353_fecha_cancelacion IS NULL AND f253_id LIKE ''1305%''"

#: `f350_ind_estado` de un documento anulado: sus líneas no son venta.
ESTADO_DOCTO_ANULADO = 9


# ──────────────────────────────────────────────────────────────────────────────
# Interruptores
# ──────────────────────────────────────────────────────────────────────────────

def encendido() -> bool:
    """`FOTOS_SIESA=true`. **Nace apagado.**"""
    return os.getenv('FOTOS_SIESA', 'false').strip().lower() == 'true'


def costo_encendido() -> bool:
    """`FOTOS_SIESA_COSTO` (por defecto `true`): el costo de InvFecha cuesta
    ~50-70 páginas por bodega (≈ 1 min cada una, medido en QA). Apagado, el
    costo queda `NULL` — declarado, nunca cero."""
    return os.getenv('FOTOS_SIESA_COSTO', 'true').strip().lower() == 'true'


def dias_ventas() -> int:
    """Cuántos días hacia atrás se re-fotografían las ventas además de hoy.

    Una factura de las 7:45 p. m. no alcanza la foto del día; una anulación de
    ayer cambia el estado de una línea ya fotografiada. Re-leer los últimos
    días lo recoge (el upsert por `f470_rowid` es idempotente).
    """
    try:
        return max(0, int(os.getenv('FOTOS_SIESA_DIAS_VENTAS', '3')))
    except ValueError:
        return 3


def ventana_abierta(momento=None) -> bool:
    """¿Estamos entre las 7:00 y las 19:30 de Bogotá?"""
    momento = momento or ahora_bogota()
    return VENTANA[0] <= momento.time() <= VENTANA[1]


def cos_operados() -> list:
    """Los CO de las bodegas que el WMS opera (`_BODEGAS_PV`), vía `co_de_bodega`."""
    from app.services.bodegas import co_de_bodega
    from app.services.inventario_siesa_service import _BODEGAS_PV
    return sorted({c for c in (co_de_bodega(b) for b in _BODEGAS_PV) if c})


# ──────────────────────────────────────────────────────────────────────────────
# Conversión de campos
# ──────────────────────────────────────────────────────────────────────────────

def _txt(v, largo=None):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    return s[:largo] if largo else s


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _dec(v):
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _fecha(v):
    s = str(v or '').strip()
    try:
        if len(s) >= 10 and s[4] == '-':
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], '%Y%m%d').date()
    except ValueError:
        return None
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Lectura paginada — la única de este módulo
# ──────────────────────────────────────────────────────────────────────────────

class Lectura:
    """Lo que devolvió Siesa y si alcanza para ser un total."""

    def __init__(self, filas, paginas, completa, motivo=None):
        self.filas = filas
        self.paginas = paginas
        self.completa = completa
        self.motivo = None if completa else (motivo or 'lectura incompleta')


def _una_pasada(gateway, api, filtro, max_paginas):
    from app.services.connekta_gateway import _exigir_datos
    filas = []
    pag = 0
    try:
        for pag in range(1, max_paginas + 1):
            res = gateway._get(api, {'paginacion': f'numPag={pag}|tamPag={TAM_PAG}',
                                     'parametros': filtro})
            if res is None:
                return Lectura(filas, pag - 1, False,
                               f'Siesa no respondió en la página {pag} '
                               f'(circuit breaker abierto o respuesta vacía)')
            detalle = res.get('detalle') if isinstance(res, dict) else None
            rows = detalle.get('Table', []) if isinstance(detalle, dict) else []
            rows = _exigir_datos(rows, api, filtro)
            filas.extend(rows)
            if len(rows) < TAM_PAG:
                return Lectura(filas, pag, True)
        return Lectura(filas, max_paginas, False,
                       f'truncada: se agotaron {max_paginas} páginas sin una página corta')
    except Exception as e:
        return Lectura(filas, max(pag - 1, 0), False, f'página {pag}: {str(e)[:300]}')


def leer_paginado(gateway, api, filtro, clave, max_paginas=None) -> Lectura:
    """Lee todas las páginas y deduplica por `clave(fila)`.

    Filas repetidas en una pasada = paginación inestable: las que se repiten
    empujaron a otras fuera. Se hace una segunda pasada; si viene limpia se usa
    esa, si no se unen las dos y la lectura queda **incompleta**.
    """
    max_paginas = max_paginas or MAX_PAGINAS

    def _dedup(filas):
        vistas = {}
        for f in filas:
            vistas.setdefault(clave(f), f)
        return vistas

    l1 = _una_pasada(gateway, api, filtro, max_paginas)
    u1 = _dedup(l1.filas)
    if not l1.completa or len(u1) == len(l1.filas):
        return Lectura(list(u1.values()), l1.paginas, l1.completa, l1.motivo)

    logger.warning('[FOTOS] %s: %d filas repetidas en la paginación — segunda pasada',
                   api, len(l1.filas) - len(u1))
    l2 = _una_pasada(gateway, api, filtro, max_paginas)
    u2 = _dedup(l2.filas)
    if l2.completa and len(u2) == len(l2.filas):
        return Lectura(list(u2.values()), l2.paginas, True)
    todas = {**u2, **u1}
    return Lectura(list(todas.values()), max(l1.paginas, l2.paginas), False,
                   f'paginación inestable: filas repetidas en dos pasadas '
                   f'({len(l1.filas) - len(u1)} y {len(l2.filas) - len(u2)})')


# ──────────────────────────────────────────────────────────────────────────────
# Corridas
# ──────────────────────────────────────────────────────────────────────────────

def _abrir_corrida(run_id, tipo, alcance, dia):
    c = FotoCorrida(run_id=run_id, tipo=tipo, alcance=alcance, dia_operativo=dia,
                    iniciada_at=datetime.utcnow(), completa=False, filas=0, paginas=0)
    db.session.add(c)
    return c


def _cerrar_corrida(corrida, lectura_completa, filas, paginas, motivo=None, detalle=None):
    corrida.completa = bool(lectura_completa)
    corrida.motivo = None if lectura_completa else (motivo or 'lectura incompleta')
    corrida.filas = filas
    corrida.paginas = paginas
    corrida.terminada_at = datetime.utcnow()
    if detalle is not None:
        corrida.detalle = json.dumps(detalle, default=str, sort_keys=True)


def _nuevo_run_id():
    return uuid.uuid4().hex


def _gateway(gateway):
    if gateway is None:
        from app.services.connekta_gateway import connekta
        return connekta
    return gateway


# ──────────────────────────────────────────────────────────────────────────────
# Foto de ventas
# ──────────────────────────────────────────────────────────────────────────────

def _campos_venta(r: dict) -> dict:
    from app.services.cadena_pedido import clave_pedido
    ped_co = _txt(r.get('f430_id_co'))
    ped_tipo = _txt(r.get('f430_id_tipo_docto'))
    ped_consec = _int(r.get('f430_consec_docto'))
    dscto = None
    dl, dg = _dec(r.get('f470_vlr_dscto_linea')), _dec(r.get('f470_vlr_dscto_global'))
    if dl is not None or dg is not None:
        dscto = (dl or Decimal(0)) + (dg or Decimal(0))
    return {
        'co': _txt(r.get('f350_id_co')),
        'f350_rowid': _int(r.get('f350_rowid')),
        'tipo_docto': _txt(r.get('f350_id_tipo_docto')),
        'consec_docto': _int(r.get('f350_consec_docto')),
        'clase_docto': _int(r.get('f350_id_clase_docto')),
        'estado_docto': _int(r.get('f350_ind_estado')),
        'pedido_tipo': ped_tipo,
        'pedido_consec': ped_consec,
        'pedido_co': ped_co,
        'pedido_clave': clave_pedido(ped_co, ped_tipo, ped_consec),
        'cliente_nit': _txt(r.get('f200_nit_fact')) or _txt(r.get('f200_id_fact')),
        'cliente_razon_social': _txt(r.get('f200_razon_social_fact'), 200),
        'cliente_sucursal': _txt(r.get('f461_id_sucursal_fact')),
        'vendedor_codigo': _txt(r.get('f210_codigo_vendedor')),
        'vendedor_id': _txt(r.get('f200_id_vendedor')),
        'cond_pago': _txt(r.get('f461_id_cond_pago')),
        'bodega': _txt(r.get('f150_id')),
        'item_id': _int(r.get('f120_id')),
        'referencia': _txt(r.get('f120_referencia'), 60),
        'concepto': _int(r.get('f470_id_concepto')),
        'motivo': _txt(r.get('f470_id_motivo')),
        'causal_devolucion': _txt(r.get('f470_id_causal_devol')),
        'naturaleza': _int(r.get('f470_ind_naturaleza')),
        'unidad_negocio': _txt(r.get('f470_id_un_movto')) or _txt(r.get('f281_id')),
        'cantidad': _dec(r.get('f470_cant_base')),
        'precio_uni': _dec(r.get('f470_precio_uni')),
        'vlr_bruto': _dec(r.get('f470_vlr_bruto')),
        'vlr_dscto': dscto,
        'vlr_imp': _dec(r.get('f470_vlr_imp')),
        'vlr_neto': _dec(r.get('f470_vlr_neto')),
        'costo_prom_tot': _dec(r.get('f470_costo_prom_tot')),
    }


def fotografiar_ventas(co: str, dia, gateway=None, run_id: str = None) -> dict:
    """Las líneas de factura de un CO en un día. Ver las reglas del módulo."""
    gateway = _gateway(gateway)
    run_id = run_id or _nuevo_run_id()
    corrida = _abrir_corrida(run_id, TipoFoto.VENTAS, str(co), dia)
    try:
        filtro = (f'f350_id_co = {lit(co, "f350_id_co")} '
                  f'AND f350_fecha >= {lit_fecha(dia)} AND f350_fecha <= {lit_fecha(dia)}')
    except ValueError as e:
        _cerrar_corrida(corrida, False, 0, 0, f'filtro inválido: {e}')
        db.session.commit()
        return corrida.to_dict()

    lectura = leer_paginado(gateway, API_VENTAS, filtro,
                            clave=lambda f: f.get('f470_rowid'))
    ahora = datetime.utcnow()
    rowids = [_int(f.get('f470_rowid')) for f in lectura.filas]
    rowids = [r for r in rowids if r is not None]
    existentes = {}
    for i in range(0, len(rowids), 500):
        for fila in FotoVentaLinea.query.filter(
                FotoVentaLinea.f470_rowid.in_(rowids[i:i + 500])).all():
            existentes[fila.f470_rowid] = fila

    escritas = sin_rowid = 0
    for r in lectura.filas:
        rowid = _int(r.get('f470_rowid'))
        if rowid is None:
            sin_rowid += 1
            continue
        campos = _campos_venta(r)
        campos['co'] = campos['co'] or str(co)
        dia_doc = _fecha(r.get('f350_fecha')) or dia
        fila = existentes.get(rowid)
        if fila is not None and not lectura.completa:
            continue            # regla 2: una lectura incompleta no pisa
        if fila is None:
            fila = FotoVentaLinea(f470_rowid=rowid, capturada_at=ahora)
            db.session.add(fila)
            existentes[rowid] = fila
        for k, v in campos.items():
            setattr(fila, k, v)
        fila.dia_operativo = dia_doc
        fila.run_id = run_id
        fila.completa = lectura.completa
        fila.actualizada_at = ahora
        escritas += 1

    motivo = lectura.motivo
    completa = lectura.completa
    if completa and sin_rowid:
        completa = False
        motivo = f'{sin_rowid} fila(s) sin f470_rowid: no se pueden identificar'
    _cerrar_corrida(corrida, completa, escritas, lectura.paginas, motivo)
    db.session.commit()
    if not completa:
        logger.error('[FOTOS] ventas CO %s %s INCOMPLETA: %s', co, dia, motivo)
    return corrida.to_dict()


def completar_valor_factura() -> dict:
    """Llena `tareas_packing.valor_factura` desde la foto de ventas.

    `valor_factura` solo se llenaba cuando un conductor abría las paradas
    (`RutaService._valor_y_cond_pago`). Un despacho que nunca llegó a esa
    pantalla quedaba sin valor para siempre. La respuesta del 142943 no trae
    el valor (`{'codigo': 0, 'mensaje': …, 'detalle': 'Importacion exitosa'}`),
    así que al facturar no se puede anotar sin otra consulta; la foto diaria
    ya la hizo.

    Emparejamiento, en orden, y **solo con filas de corridas completas**:
      1. La FE ya resuelta en la tarea (`fe_tipo`/`fe_consec`).
      2. Si no, el pedido (`pedido_clave`), **solo si** ese pedido tiene
         exactamente UNA factura en toda la foto y exactamente UN packing
         despachado. Con dos parciales no se sabe cuál es cuál (Regla 0).
    Facturas anuladas (estado 9) no cuentan. Solo toca tareas con
    `valor_factura` NULL: lo que ya anotó la pantalla del conductor manda.
    """
    from app.models.packing import TareaPacking
    res = {'por_factura': 0, 'por_pedido': 0, 'ambiguos': 0, 'sin_foto': 0}
    try:
        pendientes = TareaPacking.query.filter(
            TareaPacking.valor_factura.is_(None),
            TareaPacking.siesa_triggered.is_(True),
            TareaPacking.estado != 'CANCELADO').all()
        if not pendientes:
            return res
        claves = {t.pedido_clave for t in pendientes if t.pedido_clave}
        fes = {(t.fe_tipo, _int(t.fe_consec)) for t in pendientes
               if t.fe_tipo and _int(t.fe_consec) is not None}

        lineas = []
        base = FotoVentaLinea.query.filter(FotoVentaLinea.completa.is_(True))
        if claves:
            lineas += base.filter(FotoVentaLinea.pedido_clave.in_(sorted(claves))).all()
        for tipo, consec in fes:
            lineas += base.filter(FotoVentaLinea.tipo_docto == tipo,
                                  FotoVentaLinea.consec_docto == consec).all()
        documentos, pedidos, vistas = {}, {}, set()
        for f in lineas:
            if f.f470_rowid in vistas:
                continue
            vistas.add(f.f470_rowid)
            if f.estado_docto == ESTADO_DOCTO_ANULADO or f.vlr_neto is None:
                continue
            doc = (f.tipo_docto, f.consec_docto)
            documentos[doc] = documentos.get(doc, Decimal(0)) + f.vlr_neto
            if f.pedido_clave:
                pedidos.setdefault(f.pedido_clave, set()).add(doc)

        packings_por_clave = {}
        if claves:
            for tid, clave in db.session.query(TareaPacking.id, TareaPacking.pedido_clave).filter(
                    TareaPacking.siesa_triggered.is_(True),
                    TareaPacking.estado != 'CANCELADO',
                    TareaPacking.pedido_clave.in_(sorted(claves))).all():
                packings_por_clave.setdefault(clave, set()).add(tid)

        for t in pendientes:
            if t.fe_tipo and _int(t.fe_consec) is not None:
                v = documentos.get((t.fe_tipo, _int(t.fe_consec)))
                if v is None:
                    res['sin_foto'] += 1
                elif v >= 0:
                    t.valor_factura = v
                    res['por_factura'] += 1
                continue
            if not t.pedido_clave:
                continue
            docs = pedidos.get(t.pedido_clave) or set()
            n_pack = len(packings_por_clave.get(t.pedido_clave, ()))
            if not docs:
                res['sin_foto'] += 1
            elif len(docs) == 1 and n_pack == 1:
                v = documentos[next(iter(docs))]
                if v >= 0:
                    t.valor_factura = v
                    res['por_pedido'] += 1
            else:
                res['ambiguos'] += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning('[FOTOS] completar valor_factura falló: %s', e)
        res['error'] = str(e)[:300]
    return res


# ──────────────────────────────────────────────────────────────────────────────
# Foto de stock
# ──────────────────────────────────────────────────────────────────────────────

def _costos_de_bodega(gateway, bodega) -> tuple:
    """`({referencia: (costo_uni, costo_tot)}, Lectura)` desde InvFecha.

    InvFecha trae una fila por ítem × lote × ubicación: se suman existencia y
    costo total por referencia y el unitario es tot / existencia. Sin
    existencia no hay unitario: `None`, no cero.
    """
    filtro = f'f150_id = {lit(bodega, "f150_id")} AND f400_cant_existencia_1 <> 0'
    lectura = leer_paginado(
        gateway, API_INVENTARIO, filtro,
        clave=lambda f: ((f.get('f120_referencia') or '').strip(),
                         (f.get('f400_id_lote') or '').strip(),
                         (f.get('f400_id_ubicacion_aux') or '').strip()))
    acum = {}
    for f in lectura.filas:
        ref = (f.get('f120_referencia') or '').strip()
        if not ref:
            continue
        ex, tot = _dec(f.get('f400_cant_existencia_1')), _dec(f.get('f400_costo_prom_tot'))
        if ex is None or tot is None:
            acum[ref] = None           # una fila sin dato envenena la suma
            continue
        if ref in acum and acum[ref] is None:
            continue
        e0, t0 = acum.get(ref) or (Decimal(0), Decimal(0))
        acum[ref] = (e0 + ex, t0 + tot)
    costos = {}
    for ref, par in acum.items():
        if par is None:
            costos[ref] = (None, None)
            continue
        ex, tot = par
        costos[ref] = ((tot / ex).quantize(Decimal('0.0001')) if ex else None, tot)
    return costos, lectura


def fotografiar_stock(bodega: str, dia=None, gateway=None, run_id: str = None,
                      costo: bool = None) -> dict:
    """Copia `stock_siesa` de una bodega al día, con el costo de InvFecha."""
    from app.models.stock_siesa import StockSiesa
    from app.services.inventario_siesa_service import _BODEGAS_PV

    gateway = _gateway(gateway)
    dia = dia or dia_operativo()
    run_id = run_id or _nuevo_run_id()
    costo = costo_encendido() if costo is None else costo
    corrida = _abrir_corrida(run_id, TipoFoto.STOCK, bodega, dia)
    vendible = bodega in _BODEGAS_PV

    filas = StockSiesa.query.filter(StockSiesa.bodega == bodega).all()
    detalle = {'vendible': vendible}
    if not filas:
        _cerrar_corrida(corrida, False, 0, 0,
                        f'stock_siesa no tiene filas para {bodega}: no hay qué fotografiar',
                        detalle)
        db.session.commit()
        return corrida.to_dict()

    ultima = max((f.updated_at for f in filas if f.updated_at), default=None)
    completa = ultima is not None and dia_operativo_de(ultima) == dia
    motivo = None if completa else (
        f'stock_siesa de {bodega} no se descarga desde '
        f'{ultima.isoformat() if ultima else "nunca"} (UTC): la foto copiaría un dato viejo')
    detalle['stock_actualizado_hasta'] = ultima.isoformat() if ultima else None

    costos, lectura_costo = {}, None
    if costo and vendible:
        costos, lectura_costo = _costos_de_bodega(gateway, bodega)
        detalle['costo'] = {'completo': lectura_costo.completa,
                            'motivo': lectura_costo.motivo,
                            'paginas': lectura_costo.paginas,
                            'referencias': len(costos)}
    else:
        detalle['costo'] = {'completo': False,
                            'motivo': ('FOTOS_SIESA_COSTO apagado' if vendible
                                       else 'bodega de servicio: no se costea')}

    existentes = {f.codigo_siesa: f for f in FotoStockDiaria.query.filter(
        FotoStockDiaria.dia_operativo == dia, FotoStockDiaria.bodega == bodega).all()}
    ahora = datetime.utcnow()
    escritas = rezagadas = 0
    for s in filas:
        if not ((s.existencia or 0) or (s.comprometido or 0) or (s.salida_sin_conf or 0)):
            continue            # en una corrida completa, ausente = cero
        f = existentes.get(s.codigo_siesa)
        if f is not None and not completa:
            continue            # regla 2
        if f is None:
            f = FotoStockDiaria(dia_operativo=dia, bodega=bodega, codigo_siesa=s.codigo_siesa)
            db.session.add(f)
        rez = bool(ultima and s.updated_at and s.updated_at < ultima - _REZAGO_STOCK)
        rezagadas += rez
        cu, ct = costos.get(s.codigo_siesa, (None, None))
        f.run_id = run_id
        f.completa = completa
        f.vendible = vendible
        f.existencia = s.existencia
        f.comprometido = s.comprometido
        f.salida_sin_conf = s.salida_sin_conf
        f.stock_actualizado_at = s.updated_at
        f.rezagada = rez
        f.costo_prom_uni = cu
        f.costo_prom_tot = ct
        f.capturada_at = ahora
        escritas += 1
    detalle['filas_rezagadas'] = rezagadas
    _cerrar_corrida(corrida, completa, escritas,
                    lectura_costo.paginas if lectura_costo else 0, motivo, detalle)
    db.session.commit()
    return corrida.to_dict()


# ──────────────────────────────────────────────────────────────────────────────
# Foto de cartera
# ──────────────────────────────────────────────────────────────────────────────

def fotografiar_cartera(dia=None, gateway=None, run_id: str = None) -> dict:
    """Saldo abierto por documento, cuentas 1305. Una corrida para toda la cartera."""
    gateway = _gateway(gateway)
    dia = dia or dia_operativo()
    run_id = run_id or _nuevo_run_id()
    corrida = _abrir_corrida(run_id, TipoFoto.CARTERA, 'TODAS', dia)
    lectura = leer_paginado(gateway, API_CARTERA, _FILTRO_CARTERA,
                            clave=lambda f: f.get('f353_rowid'))
    existentes = {f.f353_rowid: f for f in FotoCarteraDiaria.query.filter(
        FotoCarteraDiaria.dia_operativo == dia).all()}
    ahora = datetime.utcnow()
    escritas = sin_rowid = 0
    for r in lectura.filas:
        rowid = _int(r.get('f353_rowid'))
        if rowid is None:
            sin_rowid += 1
            continue
        f = existentes.get(rowid)
        if f is not None and not lectura.completa:
            continue
        if f is None:
            f = FotoCarteraDiaria(dia_operativo=dia, f353_rowid=rowid)
            db.session.add(f)
            existentes[rowid] = f
        db_, cr = _dec(r.get('f353_total_db')), _dec(r.get('f353_total_cr'))
        vcto = _fecha(r.get('f353_fecha_vcto'))
        f.run_id = run_id
        f.completa = lectura.completa
        f.co = _txt(r.get('f353_id_co_cruce'))
        f.tipo_docto = _txt(r.get('f353_id_tipo_docto_cruce'))
        f.consec_docto = _int(r.get('f353_consec_docto_cruce'))
        f.nro_cuota = _int(r.get('f353_nro_cuota_cruce'))
        f.tercero_id = _txt(r.get('f200_id'))
        f.sucursal = _txt(r.get('f201_id_sucursal'))
        f.cuenta = _txt(r.get('f253_id'))
        f.unidad_negocio = _txt(r.get('f353_id_un_cruce'))
        f.fecha_docto = _fecha(r.get('f353_fecha'))
        f.fecha_vcto = vcto
        f.total_db = db_
        f.total_cr = cr
        f.saldo = (db_ - cr) if (db_ is not None and cr is not None) else None
        f.dias_vencido = (dia - vcto).days if vcto else None
        f.capturada_at = ahora
        escritas += 1
    completa, motivo = lectura.completa, lectura.motivo
    if completa and sin_rowid:
        completa, motivo = False, f'{sin_rowid} fila(s) sin f353_rowid'
    _cerrar_corrida(corrida, completa, escritas, lectura.paginas, motivo)
    db.session.commit()
    if not completa:
        logger.error('[FOTOS] cartera %s INCOMPLETA: %s', dia, motivo)
    return corrida.to_dict()


# ──────────────────────────────────────────────────────────────────────────────
# Lectura para la analítica
# ──────────────────────────────────────────────────────────────────────────────

_MODELO = {TipoFoto.VENTAS: FotoVentaLinea, TipoFoto.STOCK: FotoStockDiaria,
           TipoFoto.CARTERA: FotoCarteraDiaria}


def corrida_vigente(tipo: str, alcance: str, dia):
    """La última corrida COMPLETA de ese alcance y día, o `None`."""
    return (FotoCorrida.query
            .filter(FotoCorrida.tipo == tipo, FotoCorrida.alcance == str(alcance),
                    FotoCorrida.dia_operativo == dia, FotoCorrida.completa.is_(True))
            .order_by(FotoCorrida.terminada_at.desc(), FotoCorrida.id.desc())
            .first())


def filas_vigentes(tipo: str, alcance: str, dia):
    """Las filas que valen como total de ese día, o **`None`** si no hay una
    corrida completa. `None` no es «cero ventas»: es «no sabemos»."""
    c = corrida_vigente(tipo, alcance, dia)
    if c is None:
        return None
    modelo = _MODELO[tipo]
    q = modelo.query.filter(modelo.run_id == c.run_id)
    if tipo == TipoFoto.VENTAS:
        q = q.filter(modelo.co == str(alcance), modelo.dia_operativo == dia)
    else:
        q = q.filter(modelo.dia_operativo == dia)
        if tipo == TipoFoto.STOCK:
            q = q.filter(modelo.bodega == str(alcance))
    return q.all()


def _corrida_info(c) -> dict:
    return {'run_id': c.run_id,
            'terminada_at': c.terminada_at.isoformat() if c.terminada_at else None}


def ventas_del_dia(co: str, dia, bodega: str = None):
    """Lo facturado desde pedido por el CO ese día, según la última corrida
    COMPLETA. **`None` si no la hay**: hueco, no «cero ventas».

    `bodega` acota a las líneas de esa bodega (la corrida es por CO, y un CO
    puede tener dos bodegas: 001 = NS1 + NS2). Las anuladas no suman; una
    línea sin `vlr_neto` tampoco, y se cuenta en `sin_valor` — con
    `sin_valor > 0` el neto es cota inferior.
    """
    c = corrida_vigente(TipoFoto.VENTAS, co, dia)
    if c is None:
        return None
    filas = filas_vigentes(TipoFoto.VENTAS, co, dia) or []
    neto, lineas, sin_valor, anuladas, docs = Decimal(0), 0, 0, 0, set()
    for f in filas:
        if bodega and (f.bodega or '').strip().upper() != str(bodega).strip().upper():
            continue
        if f.estado_docto == ESTADO_DOCTO_ANULADO:
            anuladas += 1
            continue
        if f.vlr_neto is None:
            sin_valor += 1
            continue
        neto += f.vlr_neto
        lineas += 1
        docs.add((f.tipo_docto, f.consec_docto))
    return {'neto': neto, 'lineas': lineas, 'documentos': len(docs),
            'sin_valor': sin_valor, 'anuladas': anuladas, **_corrida_info(c)}


def cartera_del_dia(dia):
    """Cartera de clientes (1305) abierta y vencida según la foto COMPLETA de
    ese día. **`None` si no hay corrida completa.**

    `vencida` suma el saldo de los documentos con `dias_vencido > 0`. Uno sin
    fecha de vencimiento no se da por vencido ni por al día: va a
    `sin_vencimiento` (y la vencida queda como cota inferior).
    """
    c = corrida_vigente(TipoFoto.CARTERA, 'TODAS', dia)
    if c is None:
        return None
    filas = filas_vigentes(TipoFoto.CARTERA, 'TODAS', dia) or []
    abierta, vencida = Decimal(0), Decimal(0)
    documentos = vencidos = sin_saldo = sin_vencimiento = 0
    for f in filas:
        if f.saldo is None:
            sin_saldo += 1
            continue
        documentos += 1
        abierta += f.saldo
        if f.dias_vencido is None:
            sin_vencimiento += 1
        elif f.dias_vencido > 0:
            vencidos += 1
            vencida += f.saldo
    return {'abierta': abierta, 'vencida': vencida, 'documentos': documentos,
            'vencidos': vencidos, 'sin_saldo': sin_saldo,
            'sin_vencimiento': sin_vencimiento, **_corrida_info(c)}


# ──────────────────────────────────────────────────────────────────────────────
# Orquestación
# ──────────────────────────────────────────────────────────────────────────────

def correr_fotos(gateway=None, ahora=None, reloj=None) -> dict:
    """Las tres fotos del día. **El interruptor vive acá.**

    `reloj`: función que da la hora Bogotá (para probar el cierre de la
    ventana a mitad de camino). Por defecto, la real; si se pasa `ahora` sin
    reloj, el reloj queda fijo en `ahora`.
    """
    if not encendido():
        return {'omitido': 'FOTOS_SIESA no está en true — nace apagado'}
    gateway = _gateway(gateway)
    if getattr(gateway, 'modo_simulacion', False) is True:
        return {'omitido': 'modo simulación: sin credenciales no hay qué fotografiar'}
    if reloj is None:
        reloj = (lambda: ahora) if ahora is not None else ahora_bogota
    ahora = ahora or reloj()
    if not ventana_abierta(ahora):
        return {'omitido': f'fuera de la ventana de Siesa ({VENTANA[0]}–{VENTANA[1]} '
                           f'Bogotá): {ahora.time().isoformat(timespec="minutes")}'}

    from app.services.inventario_siesa_service import _BODEGAS_INVENTARIO
    run_id = _nuevo_run_id()
    hoy = ahora.date()
    res = {'run_id': run_id, 'ventas': [], 'stock': [], 'cartera': None,
           'no_corrieron': []}

    def _sigue():
        return ventana_abierta(reloj())

    for atras in range(dias_ventas(), -1, -1):
        dia = hoy - timedelta(days=atras)
        for co in cos_operados():
            if not _sigue():
                res['no_corrieron'].append(f'ventas {co} {dia}')
                continue
            res['ventas'].append(fotografiar_ventas(co, dia, gateway, run_id))
    res['valor_factura'] = completar_valor_factura()

    if _sigue():
        res['cartera'] = fotografiar_cartera(hoy, gateway, run_id)
    else:
        res['no_corrieron'].append('cartera')

    for bodega in _BODEGAS_INVENTARIO:
        if not _sigue():
            res['no_corrieron'].append(f'stock {bodega}')
            continue
        res['stock'].append(fotografiar_stock(bodega, hoy, gateway, run_id))

    if res['no_corrieron']:
        logger.error('[FOTOS] la ventana de Siesa se cerró: no corrieron %s',
                     res['no_corrieron'])
    return res


def estado(dias: int = 7) -> dict:
    """Frescura y huecos para `/api/health/siesa`. Nunca levanta."""
    try:
        desde = dia_operativo() - timedelta(days=dias)
        ultimas = {}
        huecos = []
        corridas = (FotoCorrida.query.filter(FotoCorrida.dia_operativo >= desde)
                    .order_by(FotoCorrida.iniciada_at.asc(), FotoCorrida.id.asc()).all())
        vigente = {}
        for c in corridas:
            ultimas[c.tipo] = c
            clave = (c.tipo, c.alcance, c.dia_operativo)
            vigente[clave] = vigente.get(clave, False) or bool(c.completa)
        for (tipo, alcance, dia), ok in sorted(vigente.items(), key=lambda x: (x[0][2], x[0][0], x[0][1])):
            if not ok:
                huecos.append({'tipo': tipo, 'alcance': alcance, 'dia': dia.isoformat()})
        return {
            'encendido': encendido(),
            'costo_encendido': costo_encendido(),
            'ultima_corrida': {t: c.to_dict() for t, c in ultimas.items()},
            'huecos_ultimos_dias': huecos,
            'nota': ('Un hueco es un alcance y día sin ninguna corrida completa: '
                     'no hay total para ese día, no un total en cero.'),
        }
    except Exception as e:
        return {'encendido': encendido(), 'error': str(e)[:200]}


def init_scheduler(app):
    """Cron diario 18:00 Bogotá, dentro de la ventana de Siesa (7:00–19:30).

    18:00 y no más tarde: la foto entera (≈ 10 CO × 4 días de ventas, 47
    páginas de cartera, 10 bodegas × ~1 min de costo) puede tardar 15-20 min,
    y Siesa deja de operar hacia las 8 p. m. Lo que la ventana corte se
    declara. Nace apagado: sin `FOTOS_SIESA=true`, `correr_fotos` devuelve su
    motivo sin tocar Siesa.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[FOTOS_SIESA] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import LOCK_FOTOS_SIESA, advisory_lock
            with advisory_lock(LOCK_FOTOS_SIESA, 'fotos_siesa') as tomado:
                if not tomado:
                    logger.info('[FOTOS_SIESA] otro worker ya corre las fotos')
                    return
                try:
                    r = correr_fotos()
                    logger.info('[FOTOS_SIESA] %s', {k: (v if k != 'ventas' else len(v))
                                                     for k, v in r.items()})
                except Exception as e:
                    db.session.rollback()
                    logger.error('[FOTOS_SIESA] falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(func=con_latido('fotos_siesa_diarias', _job), trigger=CronTrigger(hour=18, minute=0,
                                                     timezone='America/Bogota'),
                      id='fotos_siesa_diarias', replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    scheduler.start()
    logger.info('[FOTOS_SIESA] Scheduler 18:00 Bogotá (encendido=%s)', encendido())
    return scheduler
