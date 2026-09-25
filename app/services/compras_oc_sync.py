"""
Espejo de las órdenes de compra de Siesa → `oc_linea_siesa` y `proveedores`.

Fuente: `API_v2_Compras_Ordenes`, contrato completo en `docs/siesa-specs/`
(89 campos). Es la única lectura de compras que cumple la Regla 1 sin
descubrimiento en vivo. El maestro de proveedores sale de
`API_v2_Proveedores` (una fila por sucursal; **sin contrato** en
`docs/siesa-specs/`: campos descubiertos en vivo el 2026-09-25 — el `.docx`
`API_v1_Proveedores` es el del POST). Antes se leía `API_v2_Terceros`, que no
tiene tipo de proveedor ni moneda y trae las dos compañías: mezclaba la 1 y la
2 (ganaba la última fila) y metía EPS, fondos de pensiones y cédulas.

## Tres corridas

| Qué | Filtro | Para qué |
|---|---|---|
| `sincronizar_ocs`        | `f420_ind_estado = 1` y `= 2` (Aprobada, Parcial) | «en camino» |
| `sincronizar_historial`  | `f420_ind_estado = 3 AND f420_fecha >= ''AAAAMMDD''` | lead time y precio |
| `sincronizar_proveedores` | `API_v2_Proveedores`: `f202_ind_estado = 1`; en Python, compañía `SIESA_ID_CIA` y tipo en `COMPRAS_TIPOS_PROVEEDOR` | maestro de proveedores de mercancía |

## Reglas

- **Nace apagado** (`COMPRAS_OC_SYNC=true` lo enciende). Apagado no lee ni escribe.
- **Solo en la ventana de Siesa** (7:00–19:30 Bogotá, Regla 14).
- **Lock del registro** (`LOCK_COMPRAS_OC_SYNC`).
- **tamPag = 100** (Regla 10). Se pagina hasta una página corta.
- **La paginación incompleta no cierra nada.** Una línea abierta que no
  apareció solo se da por cerrada si el barrido fue COMPLETO: una página que
  falló, el tope de páginas, el circuito abierto o un rowid repetido dentro de
  la misma enumeración (orden inestable, lo que ya se midió en el kardex) dejan
  la corrida INCOMPLETA y **no se toca ninguna fila no vista**. Es la regla del
  hallazgo K del sync de pedidos, que borraba lo de las páginas no leídas.
- **Nada se borra.** Cerrar es `abierta=False` + `cerrada_en` + motivo.
- Filtros de texto con doble comilla simple (Regla 15), vía `siesa_filtro.lit`.
- **Solo la compañía propia** (`SIESA_ID_CIA`, Regla 2): las APIs de Siesa de
  este ambiente devuelven las compañías 1 y 2. Una línea de OC o un proveedor de
  otra compañía no se escribe y se cuenta (`otra_compania`).
"""
import logging
import os
import time
from collections import Counter
from datetime import datetime, timedelta

from app.extensions import db

logger = logging.getLogger(__name__)

TAM_PAG = 100          # Regla 10: ≥500 fabrica filas fantasma
REGISTRO = 'compras_oc'
REGISTRO_HISTORIAL = 'compras_oc_historial'

ESTADOS_ABIERTOS = (1, 2)   # Aprobado, Parcial (f420_desc_estado del spec)
ESTADO_CUMPLIDO = 3

MOTIVO_NO_APARECE = 'NO_APARECE_EN_ABIERTAS'
MOTIVO_CUMPLIDA = 'CUMPLIDA_EN_HISTORIAL'


def encendido() -> bool:
    """`COMPRAS_OC_SYNC=true`. **Nace apagado**: un cron que escribe no se
    enciende solo."""
    return os.getenv('COMPRAS_OC_SYNC', 'false').strip().lower() == 'true'


def _max_paginas() -> int:
    try:
        return max(1, int(os.getenv('COMPRAS_OC_MAX_PAGINAS', '60')))
    except ValueError:
        return 60


def _pausa_default() -> float:
    try:
        return max(0.0, float(os.getenv('COMPRAS_OC_PAUSA_S', '0.5')))
    except ValueError:
        return 0.5


def _dias_historial() -> int:
    try:
        return max(30, int(os.getenv('COMPRAS_OC_HISTORIAL_DIAS', '365')))
    except ValueError:
        return 365


def id_cia() -> int:
    """La compañía propia en Siesa (`SIESA_ID_CIA`, default 1 — Regla 2: nunca
    el tenant 8215). Las consultas de este ambiente traen la 1 y la 2."""
    try:
        return int(str(os.getenv('SIESA_ID_CIA', '1')).strip())
    except ValueError:
        return 1


def _de_otra_compania(fila, *campos) -> bool:
    """True si la fila declara una compañía distinta de la propia. Una fila sin
    el campo no se descarta (el contrato lo trae; faltar no es evidencia)."""
    for c in campos:
        v = _int(fila.get(c))
        if v is not None:
            return v != id_cia()
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Conversión
# ──────────────────────────────────────────────────────────────────────────────

def _txt(v, n=None):
    s = '' if v is None else str(v).strip()
    return (s[:n] if n else s) or None


def _int(v):
    try:
        return int(float(v)) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _dt(v):
    """`2023-10-25T14:30:00` → datetime (hora local de Siesa, sin zona)."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip().replace('Z', '')
    try:
        return datetime.fromisoformat(s[:19])
    except ValueError:
        try:
            return datetime.strptime(s[:8], '%Y%m%d')
        except ValueError:
            return None


def _d(v):
    x = _dt(v)
    return x.date() if x else None


# ──────────────────────────────────────────────────────────────────────────────
# Descarga
# ──────────────────────────────────────────────────────────────────────────────

def _clave_por_rowid(r):
    """La clave de una fila para detectar la paginación inestable: la línea de
    OC (`f421_rowid`) o el tercero (`f200_rowid`)."""
    return r.get('f421_rowid', r.get('f200_rowid'))


def _descargar(gateway, nombre_api, parametros, pausa_s, url=None, clave=None,
               tope=None):
    """Todas las páginas de UNA consulta. Nunca levanta.

    `clave(fila)` identifica la fila: la misma clave dos veces en la misma
    enumeración es paginación inestable. **Tiene que ser única por fila de
    esa consulta**: `API_v2_Proveedores` trae una fila por SUCURSAL, así que
    ahí `f200_rowid` solo se repite con dos sucursales del mismo tercero y lo
    confundía con una página repetida (medido en vivo: se cortaba en la
    página 1). `tope` = máximo de páginas (default `COMPRAS_OC_MAX_PAGINAS`).

    Returns: (filas, completa: bool, motivo | None, paginas)
    """
    from app.services.connekta_gateway import _exigir_datos

    clave = clave or _clave_por_rowid
    filas, vistos = [], set()
    tope = tope or _max_paginas()
    for pag in range(1, tope + 1):
        params = {'parametros': parametros,
                  'paginacion': f'numPag={pag}|tamPag={TAM_PAG}'}
        try:
            resp = (gateway._get(nombre_api, params, url=url) if url
                    else gateway._get(nombre_api, params))
        except Exception as e:  # red, 429, 5xx, 401: la corrida no está completa
            return filas, False, f'página {pag}: {str(e)[:300]}', pag - 1
        if resp is None:
            return filas, False, f'página {pag}: circuito de Siesa abierto', pag - 1
        detalle = resp.get('detalle') if isinstance(resp, dict) else None
        rows = (detalle or {}).get('Table') if isinstance(detalle, dict) else None
        if rows is None:
            return filas, False, f'página {pag}: respuesta sin detalle.Table', pag - 1
        try:
            _exigir_datos(rows, nombre_api, parametros)
        except Exception as e:
            return filas, False, str(e)[:300], pag - 1
        for r in rows:
            rid = clave(r)
            if rid is not None and rid in vistos:
                # El mismo registro en dos páginas de la misma enumeración: el
                # orden no es estable y pudo saltarse otros. No se puede afirmar
                # que se vio todo.
                return filas, False, (f'página {pag}: registro {rid} repetido — '
                                      'paginación inestable'), pag
            if rid is not None:
                vistos.add(rid)
            filas.append(r)
        if len(rows) < TAM_PAG:
            return filas, True, None, pag
        if pausa_s:
            time.sleep(pausa_s)
    return filas, False, (f'tope de {tope} páginas con la última llena: hay más '
                          'del otro lado'), tope


# ──────────────────────────────────────────────────────────────────────────────
# Escritura
# ──────────────────────────────────────────────────────────────────────────────

def _upsert_proveedores(pares, fuente, ahora):
    """`pares`: {codigo: (nit, nombre)} → {codigo: Proveedor}. Siesa es el
    maestro de la razón social; el contacto y el país que alguien cargó a mano
    no se tocan."""
    from app.models.acuerdo_marco import Proveedor
    if not pares:
        return {}, 0, 0
    existentes = {p.codigo: p for p in
                  Proveedor.query.filter(Proveedor.codigo.in_(list(pares))).all()}
    nuevos = actualizados = 0
    for codigo, (nit, nombre) in pares.items():
        p = existentes.get(codigo)
        if p is None:
            p = Proveedor(codigo=codigo, nombre=nombre or codigo, nit=nit,
                          fuente=fuente, sincronizado_en=ahora, activo=True)
            db.session.add(p)
            existentes[codigo] = p
            nuevos += 1
            continue
        cambio = False
        if nombre and p.nombre != nombre:
            p.nombre = nombre
            cambio = True
        if nit and p.nit != nit:
            p.nit = nit
            cambio = True
        if p.fuente is None:
            p.fuente = fuente
        p.sincronizado_en = ahora
        actualizados += int(cambio)
    db.session.flush()
    return existentes, nuevos, actualizados


def _aplicar_lineas(filas, ahora, *, abierta: bool):
    """Upsert por `f421_rowid`. Devuelve (vistos, contadores)."""
    from app.models.compras_fuentes import OcLineaSiesa
    from app.services.compras_fuentes import pendiente_de_linea

    cont = Counter()
    por_rowid = {}
    for f in filas:
        if _de_otra_compania(f, 'f420_id_cia', 'f421_id_cia'):
            cont['otra_compania'] += 1
            continue
        rid = _int(f.get('f421_rowid'))
        if rid is None:
            cont['sin_rowid'] += 1
            continue
        por_rowid[rid] = f

    provs = {}
    for f in por_rowid.values():
        c = _txt(f.get('f200_id_prov'), 20)
        if c:
            provs[c] = (_txt(f.get('f200_nit_prov'), 20),
                        _txt(f.get('f200_razon_social_prov'), 200))
    mapa_prov, n_prov_nuevos, n_prov_act = _upsert_proveedores(provs, 'SIESA_OC', ahora)
    cont['proveedores_nuevos'] = n_prov_nuevos
    cont['proveedores_actualizados'] = n_prov_act

    ids = list(por_rowid)
    existentes = {}
    for i in range(0, len(ids), 500):
        for l in OcLineaSiesa.query.filter(
                OcLineaSiesa.rowid_linea.in_(ids[i:i + 500])).all():
            existentes[l.rowid_linea] = l

    for rid, f in por_rowid.items():
        l = existentes.get(rid)
        if l is None:
            l = OcLineaSiesa(rowid_linea=rid, primera_vista_en=ahora)
            db.session.add(l)
            cont['nuevas'] += 1
        else:
            cont['actualizadas'] += 1
        pendiente, como = pendiente_de_linea(f)
        if pendiente is None:
            cont['sin_unidad_base'] += 1
        prov = _txt(f.get('f200_id_prov'), 20)
        l.rowid_oc = _int(f.get('f420_rowid'))
        l.co = _txt(f.get('f420_id_co'), 10)
        l.tipo_docto = _txt(f.get('f420_id_tipo_docto'), 10)
        l.consec_docto = _int(f.get('f420_consec_docto'))
        l.fecha_oc = _d(f.get('f420_fecha'))
        l.estado_oc = _int(f.get('f420_ind_estado'))
        l.fecha_aprobacion = _dt(f.get('f420_fecha_ts_aprobacion'))
        l.fecha_parcial = _dt(f.get('f420_fecha_ts_parcial'))
        l.fecha_cumplido = _dt(f.get('f420_fecha_ts_cumplido'))
        l.moneda = _txt(f.get('f420_id_moneda_docto'), 5)
        l.tasa_conv = f.get('f420_tasa_conv')
        l.proveedor_codigo = prov
        l.proveedor_nit = _txt(f.get('f200_nit_prov'), 20)
        l.proveedor_nombre = _txt(f.get('f200_razon_social_prov'), 200)
        l.proveedor_sucursal = _txt(f.get('f202_id_sucursal_prov'), 10)
        l.proveedor_id = mapa_prov[prov].id if prov in mapa_prov else None
        l.referencia = _txt(f.get('f120_referencia'), 50)
        l.bodega = _txt(f.get('f150_id'), 10)
        l.co_movto = _txt(f.get('f421_id_co_movto'), 10)
        l.estado_linea = _int(f.get('f421_ind_estado'))
        l.ind_obsequio = _int(f.get('f421_ind_obsequio'))
        l.unidad_medida = _txt(f.get('f421_id_unidad_medida'), 10)
        l.factor = f.get('f421_factor')
        l.cant_pedida = f.get('f421_cant_pedida')
        l.cant_entrada = f.get('f421_cant_entrada')
        l.cant_pedida_base = f.get('f421_cant_pedida_base')
        l.cant_entrada_base = f.get('f421_cant_entrada_base')
        l.cant_importacion_base = f.get('f421_cant_importacion_base')
        l.pendiente_base = pendiente
        l.precio_unitario = f.get('f421_precio_unitario')
        l.fecha_entrega = _d(f.get('f421_fecha_entrega'))
        l.vista_en = ahora
        if abierta:
            l.abierta = True
            l.cerrada_en = None
            l.motivo_cierre = None
        else:
            if l.abierta or l.cerrada_en is None:
                l.cerrada_en = ahora
            l.abierta = False
            l.motivo_cierre = MOTIVO_CUMPLIDA
    return set(por_rowid), cont


# ──────────────────────────────────────────────────────────────────────────────
# Las corridas
# ──────────────────────────────────────────────────────────────────────────────

def _gateway(gateway):
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    return gateway


def sincronizar_ocs(gateway=None, pausa_s=None, ahora=None) -> dict:
    """Las OCs abiertas (Aprobada + Parcial) → `oc_linea_siesa`.

    No mira el interruptor ni la ventana (eso es `correr`): es también lo que
    corre el botón de la pantalla. Deja su fila en `registros_sync`.
    """
    from app.models.compras_fuentes import OcLineaSiesa
    from app.services import registro_sync_service as _reg

    gw = _gateway(gateway)
    if getattr(gw, 'modo_simulacion', False):
        return {'omitido': 'Connekta en modo simulación: no hay OCs reales que leer'}
    pausa = _pausa_default() if pausa_s is None else pausa_s
    ahora = ahora or datetime.utcnow()
    registro = _reg.abrir(REGISTRO)

    filas, completa, motivos, paginas = [], True, [], 0
    for estado in ESTADOS_ABIERTOS:
        f, ok, motivo, pags = _descargar(gw, gw.api_ordenes,
                                         f'f420_ind_estado = {estado}', pausa)
        filas.extend(f)
        paginas += pags
        if not ok:
            completa = False
            motivos.append(f'estado {estado}: {motivo}')

    # Cero OCs abiertas cuando el espejo tenía abiertas no es «se cerraron
    # todas»: es más probable un filtro que Siesa contestó con «sin registros»
    # (la fecha sin comillas ya lo hizo) o un ambiente vacío. Cerrarlas todas
    # pondría «en camino» en 0 y subiría el déficit, que es el lado
    # irreversible (Regla 0). Se declara y no se cierra nada.
    if completa and not filas and OcLineaSiesa.query.filter(
            OcLineaSiesa.abierta.is_(True)).count():
        completa = False
        motivos.append('Siesa devolvió CERO OCs abiertas y el espejo tenía abiertas: '
                       'no se cierra nada (¿filtro o ambiente?)')

    try:
        vistos, cont = _aplicar_lineas(filas, ahora, abierta=True)
        cerradas = 0
        if completa:
            for l in OcLineaSiesa.query.filter(OcLineaSiesa.abierta.is_(True)).all():
                if l.rowid_linea not in vistos:
                    l.abierta = False
                    l.cerrada_en = ahora
                    l.motivo_cierre = MOTIVO_NO_APARECE
                    cerradas += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error('[COMPRAS_OC] no se pudo escribir: %s', e, exc_info=True)
        _reg.cerrar_error(registro, f'escritura: {e}')
        return {'ok': False, 'error': str(e)[:300]}

    resultado = {
        'ok': completa,
        'paginacion_completa': completa,
        'motivo_incompleta': '; '.join(motivos) or None,
        'paginas': paginas,
        'filas_recibidas': len(filas),
        'lineas_vistas': len(vistos),
        'lineas_cerradas': cerradas,
        'cierre_omitido_por_incompleta': not completa,
        'otra_compania': 0,
        **dict(cont),
    }
    if completa:
        _reg.cerrar_ok(registro, resultado)
    else:
        # Lo que sí llegó se guardó (upsert): una línea vista es cierta. Lo que
        # NO se hizo es cerrar las no vistas — eso exige haber visto todo.
        _reg.cerrar_error(registro, 'paginación incompleta: '
                          + (resultado['motivo_incompleta'] or ''), resultado)
    logger.info('[COMPRAS_OC] %s', resultado)
    return resultado


def sincronizar_historial(gateway=None, desde=None, pausa_s=None, ahora=None) -> dict:
    """OCs CUMPLIDAS desde `desde` (date; default hoy − COMPRAS_OC_HISTORIAL_DIAS).

    Alimenta el lead time medido (fecha OC → entrada) y el precio de compra.
    Solo agrega o actualiza: no cierra ni reabre nada que no haya visto.

    El filtro de fecha va por `siesa_filtro.lit_fecha` (`''AAAAMMDD''`, con
    comillas): verificado en vivo sobre otra consulta estándar el 2026-09-24 —
    sin comillas Siesa contesta «no hay registros» en vez de rechazar—. Sobre
    ESTA consulta **no está verificado**; `qa_compras_fuentes_real.py` lo prueba.
    """
    from app.services import registro_sync_service as _reg
    from app.services.siesa_filtro import lit_fecha
    from app.utils.fecha import dia_operativo

    gw = _gateway(gateway)
    if getattr(gw, 'modo_simulacion', False):
        return {'omitido': 'Connekta en modo simulación'}
    pausa = _pausa_default() if pausa_s is None else pausa_s
    ahora = ahora or datetime.utcnow()
    desde = desde or (dia_operativo() - timedelta(days=_dias_historial()))
    registro = _reg.abrir(REGISTRO_HISTORIAL)
    parametros = (f'f420_ind_estado = {ESTADO_CUMPLIDO} AND '
                  f'f420_fecha >= {lit_fecha(desde)}')
    filas, completa, motivo, paginas = _descargar(gw, gw.api_ordenes, parametros, pausa)
    try:
        vistos, cont = _aplicar_lineas(filas, ahora, abierta=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        _reg.cerrar_error(registro, f'escritura: {e}')
        return {'ok': False, 'error': str(e)[:300]}
    resultado = {'ok': completa, 'paginacion_completa': completa,
                 'motivo_incompleta': motivo, 'desde': desde.isoformat(),
                 'paginas': paginas, 'lineas_vistas': len(vistos), **dict(cont)}
    if completa:
        _reg.cerrar_ok(registro, resultado)
    else:
        _reg.cerrar_error(registro, f'paginación incompleta: {motivo}', resultado)
    return resultado


#: Fuente de los proveedores que vienen del maestro (`API_v2_Proveedores`).
FUENTE_PROVEEDORES = 'SIESA_PROVEEDORES'
#: La fuente del sync anterior (`API_v2_Terceros`): mezclaba compañías y traía
#: EPS, pensiones y cédulas. Sus filas que no son proveedores de mercancía se
#: desactivan (con bitácora) en el primer barrido completo.
FUENTE_TERCEROS_VIEJA = 'SIESA_TERCEROS'


def tipos_proveedor():
    """`COMPRAS_TIPOS_PROVEEDOR` = los `f202_id_tipo_prov` que son proveedores
    de MERCANCÍA (p. ej. `0001,0002,0018`). **Sin default: lo decide el
    dueño.** Maestro «Tipo de proveedor» (`docs/siesa-specs/Tipo de
    proveedor.xlsx`): 0001 PROVEEDORES NACIONALES, 0002 DEL EXTRANJERO, 0017
    GASTOS DIVERSOS, **0018 NÓMINA**, 0019 aportes y seguridad social (las
    EPS y los fondos de pensiones). Proveedores de OCs en QA por tipo: 0001
    (17), 0018 (6), 0017 (3), 0002 (1). Sin la variable → `None`: solo se
    sincronizan los proveedores que aparecen en OCs."""
    v = (os.getenv('COMPRAS_TIPOS_PROVEEDOR') or '').strip()
    tipos = {t.strip() for t in v.split(',') if t.strip()}
    return tipos or None


def _clave_proveedor(r):
    return (r.get('f200_id_cia'), r.get('f200_rowid'),
            (r.get('f202_id_sucursal') or '').strip())


def _unico(valores):
    """El valor si todas las sucursales coinciden; `None` si difieren o no hay
    ninguno (no se elige uno: se declara)."""
    vs = {v for v in valores if v}
    return next(iter(vs)) if len(vs) == 1 else None


def sincronizar_proveedores(gateway=None, pausa_s=None, ahora=None) -> dict:
    """Proveedores de mercancía del maestro de Siesa (`API_v2_Proveedores`).

    - **Solo la compañía propia** (`SIESA_ID_CIA`): el resto se cuenta.
    - **Qué es un proveedor de mercancía** lo dice `COMPRAS_TIPOS_PROVEEDOR`
      (tipos de proveedor de Siesa). Sin ella solo se toca a quien aparece en
      una OC del espejo — nunca se crea un proveedor desde el maestro.
    - Una fila por SUCURSAL: el proveedor (`f200_id`) junta sus sucursales
      activas; moneda, tipo y condición de pago se guardan si coinciden entre
      ellas (si no, `None`, contado en `*_ambigua`).
    - A un proveedor que ya existe no se le toca el nombre ni el NIT (los
      escribe el sync de OCs con la razón social del tercero; el maestro solo
      trae la descripción de la sucursal).
    - Con barrido completo, las filas del sync viejo (`SIESA_TERCEROS`) que no
      son proveedores de mercancía se desactivan, con bitácora. Nada se borra.
    """
    from app.models.acuerdo_marco import Proveedor
    from app.models.compras_fuentes import OcLineaSiesa
    from app.services.bitacora import registrar_accion

    gw = _gateway(gateway)
    if getattr(gw, 'modo_simulacion', False):
        return {'omitido': 'Connekta en modo simulación'}
    pausa = _pausa_default() if pausa_s is None else pausa_s
    ahora = ahora or datetime.utcnow()
    nombre = os.getenv('CONNEKTA_API_PROVEEDORES', 'API_v2_Proveedores')
    filas, completa, motivo, paginas = _descargar(
        gw, nombre, 'f202_ind_estado = 1', pausa, clave=_clave_proveedor)
    tipos = tipos_proveedor()

    cont = Counter()
    sucursales = {}
    for f in filas:
        if _de_otra_compania(f, 'f200_id_cia'):
            cont['otra_compania'] += 1
            continue
        c = _txt(f.get('f200_id'), 20)
        if c:
            sucursales.setdefault(c, []).append(f)

    de_ocs = {c for (c,) in db.session.query(OcLineaSiesa.proveedor_codigo).distinct() if c}
    universo = set(de_ocs)
    if tipos:
        universo |= {c for c, fs in sucursales.items()
                     if any(_txt(x.get('f202_id_tipo_prov')) in tipos for x in fs)}
    cont['fuera_de_tipo'] = sum(1 for c in sucursales if c not in universo)
    cont['de_oc_sin_maestro'] = sum(1 for c in de_ocs if c not in sucursales)

    try:
        existentes = {p.codigo: p for p in Proveedor.query.all()}
        for c in sorted(universo & set(sucursales)):
            fs = sorted(sucursales[c], key=lambda x: _txt(x.get('f202_id_sucursal')) or '')
            moneda = _unico(_txt(x.get('f202_id_moneda'), 5) for x in fs)
            tipos_c = sorted({_txt(x.get('f202_id_tipo_prov')) for x in fs} - {None})
            cond = _unico(_txt(x.get('f202_id_cond_pago'), 20) for x in fs)
            if moneda is None and any(x.get('f202_id_moneda') for x in fs):
                cont['moneda_ambigua'] += 1
            p = existentes.get(c)
            if p is None:
                p = Proveedor(codigo=c,
                              nombre=_txt(fs[0].get('f202_descripcion_sucursal'), 200) or c,
                              nit=_txt(fs[0].get('f200_nit'), 20),
                              fuente=FUENTE_PROVEEDORES, activo=True)
                db.session.add(p)
                existentes[c] = p
                cont['proveedores_nuevos'] += 1
            else:
                cont['proveedores_actualizados'] += 1
                if p.fuente in (None, FUENTE_TERCEROS_VIEJA):
                    p.fuente = FUENTE_PROVEEDORES
            p.moneda = moneda
            p.tipo_proveedor = ','.join(tipos_c)[:20] or None
            if cond:
                p.condicion_pago_default = cond
            p.sincronizado_en = ahora

        if completa:
            for p in existentes.values():
                if (p.fuente == FUENTE_TERCEROS_VIEJA and p.activo
                        and p.codigo not in universo):
                    p.activo = False
                    registrar_accion(
                        'DESACTIVAR', p, motivo=(
                            'Lo había creado el sync de API_v2_Terceros, que mezclaba '
                            'compañías y traía EPS, pensiones y cédulas: no es proveedor '
                            f'de mercancía de la compañía {id_cia()}'
                            + (f' (tipos {",".join(sorted(tipos))})' if tipos else
                               ' (sin COMPRAS_TIPOS_PROVEEDOR: solo los que aparecen en OCs)')),
                        antes={'activo': True}, despues={'activo': False},
                        origen='compras_oc_sync.sincronizar_proveedores')
                    cont['desactivados_sync_viejo'] += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {'ok': False, 'error': str(e)[:300]}
    return {'ok': completa, 'paginacion_completa': completa, 'motivo_incompleta': motivo,
            'paginas': paginas, 'filas_leidas': len(filas),
            'compania': id_cia(), 'tipos': sorted(tipos) if tipos else None,
            'criterio': ('tipos de proveedor configurados + los de las OCs' if tipos else
                         'sin COMPRAS_TIPOS_PROVEEDOR: solo los proveedores de las OCs'),
            'proveedores_del_maestro': len(sucursales),
            **{k: 0 for k in ('proveedores_nuevos', 'proveedores_actualizados', 'otra_compania',
                              'moneda_ambigua', 'desactivados_sync_viejo')},
            **dict(cont)}


def _en_ventana(reloj=None):
    from app.services.fotos_siesa_service import VENTANA, ventana_abierta
    return ventana_abierta(reloj() if reloj else None), VENTANA


def correr(gateway=None, reloj=None) -> dict:
    """El cron de cada 30 min: interruptor + ventana + OCs abiertas."""
    if not encendido():
        return {'omitido': 'COMPRAS_OC_SYNC no está en true — nace apagado'}
    abierta, v = _en_ventana(reloj)
    if not abierta:
        return {'omitido': f'fuera de la ventana de Siesa ({v[0]}–{v[1]} Bogotá)'}
    return sincronizar_ocs(gateway=gateway)


def correr_diario(gateway=None, reloj=None) -> dict:
    """El cron de las 7:20: historial de cumplidas + maestro de proveedores."""
    if not encendido():
        return {'omitido': 'COMPRAS_OC_SYNC no está en true — nace apagado'}
    abierta, v = _en_ventana(reloj)
    if not abierta:
        return {'omitido': f'fuera de la ventana de Siesa ({v[0]}–{v[1]} Bogotá)'}
    return {'historial': sincronizar_historial(gateway=gateway),
            'proveedores': sincronizar_proveedores(gateway=gateway)}


def _con_lock(fn, etiqueta):
    from app.utils.lock import LOCK_COMPRAS_OC_SYNC, advisory_lock
    with advisory_lock(LOCK_COMPRAS_OC_SYNC, 'compras_oc_sync') as tomado:
        if not tomado:
            logger.info('[COMPRAS_OC] %s: otro worker ya sincroniza', etiqueta)
            return {'omitido': 'otro proceso ya está sincronizando las OCs'}
        try:
            return fn()
        except Exception as e:
            db.session.rollback()
            logger.error('[COMPRAS_OC] %s falló: %s', etiqueta, e, exc_info=True)
            return {'ok': False, 'error': str(e)[:300]}


def init_scheduler(app):
    """Cada 30 min en la ventana 7–19:30 (OCs abiertas) y 7:20 (historial +
    proveedores). Nace apagado: sin `COMPRAS_OC_SYNC=true`, `correr` devuelve su
    motivo sin tocar Siesa."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[COMPRAS_OC] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            logger.info('[COMPRAS_OC] %s', _con_lock(correr, 'abiertas'))

    def _job_diario():
        with app.app_context():
            logger.info('[COMPRAS_OC] diario %s', _con_lock(correr_diario, 'diario'))

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(func=con_latido('compras_oc_abiertas', _job),
                      trigger=CronTrigger(hour='7-19', minute='10,40',
                                          timezone='America/Bogota'),
                      id='compras_oc_abiertas', replace_existing=True,
                      max_instances=1, misfire_grace_time=900)
    scheduler.add_job(func=con_latido('compras_oc_diario', _job_diario),
                      trigger=CronTrigger(hour='7', minute='20',
                                          timezone='America/Bogota'),
                      id='compras_oc_diario', replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    scheduler.start()
    logger.info('[COMPRAS_OC] Scheduler 7–19:30 Bogotá (encendido=%s)', encendido())
    return scheduler


def disparar_en_segundo_plano(app, que: str = 'abiertas'):
    """El botón de la pantalla. Corre en un hilo con el mismo lock del cron.
    No mira el interruptor (es una decisión humana explícita) pero SÍ la
    ventana: la Regla 14 no la dobla un botón."""
    import threading

    abierta, v = _en_ventana()
    if not abierta:
        return {'ok': False,
                'error': f'Fuera de la ventana de Siesa ({v[0]}–{v[1]} Bogotá). Regla 14.'}
    fn = sincronizar_ocs if que == 'abiertas' else (
        lambda: {'historial': sincronizar_historial(),
                 'proveedores': sincronizar_proveedores()})

    def _run():
        with app.app_context():
            _con_lock(fn, f'manual {que}')

    threading.Thread(target=_run, daemon=True).start()
    return {'ok': True, 'mensaje': 'Sincronización iniciada en segundo plano.'}


def estado() -> dict:
    """Para `/api/health/siesa` y la pantalla. Nunca levanta."""
    try:
        from sqlalchemy import func
        from app.models.compras_fuentes import OcLineaSiesa
        from app.models.acuerdo_marco import Proveedor
        from app.services import registro_sync_service as _reg
        from app.services.compras_fuentes import frescura_oc
        from app.services.compras_fuentes import resumen_lineas_abiertas
        lin = resumen_lineas_abiertas()
        por_fuente = dict(db.session.query(Proveedor.fuente, func.count(Proveedor.id))
                          .group_by(Proveedor.fuente).all())
        hist = _reg.ultimo(REGISTRO_HISTORIAL)
        return {
            'encendido': encendido(),
            'lineas_total': OcLineaSiesa.query.count(),
            'lineas_abiertas': lin['abiertas'],
            'lineas_abiertas_con_pendiente': lin['con_pendiente'],
            'lineas_abiertas_sin_unidad_base': lin['sin_unidad_base'],
            'proveedores_por_fuente': {str(k): v for k, v in por_fuente.items()},
            'sync_abiertas': frescura_oc(),
            'ultimo_historial': ({k: hist.get(k) for k in ('inicio', 'ok', 'error')}
                                 if isinstance(hist, dict) else None),
        }
    except Exception as e:
        return {'encendido': encendido(), 'error': str(e)[:200]}
