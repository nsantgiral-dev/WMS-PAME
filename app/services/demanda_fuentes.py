"""
De dónde sale la demanda de compras — «Siesa calcula, el WMS decide» (2026-09-27).

Compras necesita de Siesa una sola cosa para proponer: **cuánto se vendió, por
SKU y día, en toda la red** (NB1 por pedido + las tiendas por caja). Hasta hoy
la única fuente era el kardex movimiento a movimiento (`…_KardexWMS`): 401 en QA
**y en producción** (medido el 2026-09-27 por los dos endpoints y los dos
nombres), ~17.000 páginas, y un orden de paginación que no es estable. Con eso
la bandeja no proponía ninguna compra.

Este módulo es **la única política** que contesta «¿de qué fuente sale la
demanda y cuánto vale lo que dice?» (`fuente_de_demanda`). La cascada, en orden
de preferencia:

| Fuente | Qué es | Cubre la caja (POS) | Apta para |
|---|---|---|---|
| `SIESA_VENTAS_DIA` | La consulta dinámica nueva: Siesa suma la T470 por día × bodega × SKU (`SQL_VENTAS_DIA`) | sí, lo acumulado | todo |
| `KARDEX` | Lo que ya hay en `kardex_movimientos` | sí | todo, si cubre la ventana |
| `VENTAS_DESDE_PEDIDO` | La foto de facturas desde pedido (`foto_ventas_lineas`) | **no** | la bandeja, como **cota inferior** declarada; nunca el contenedor ni el bloqueo |
| `NINGUNA` | — | — | nada: la bandeja lo dice y no propone |

Una fuente **se usa** si tiene al menos `DIAS_MINIMOS_PARA_PROPONER` días
seguidos observados dentro de la ventana; si no, se pasa a la siguiente. **Se
elige una por corrida**, no por SKU: mezclar fuentes haría que dos SKU iguales
se midan distinto. Lo que vale cada decisión (bandeja, contenedor, temporada,
bloqueo) lo dice `apta_para`, y quien decide lo lee de acá.

**Un día no observado no es un día sin venta** (Regla 0). La cobertura de cada
fuente es su tramo contiguo de días observados que termina en el más reciente:
un hueco adentro corta la ventana (se declara), porque contarlo como cero
diluye la demanda.

La numeración de la demanda sigue siendo UNA: `kardex_service.serie_demanda`
(lee la fuente elegida) y `ventana_observada` (recorta a su cobertura). Los
modelos (ROP, contenedor, S-B, TSB, temporada, bloqueo) no cambiaron.
"""
import logging
import os
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

FUENTE_SIESA = 'SIESA_VENTAS_DIA'
FUENTE_KARDEX = 'KARDEX'
FUENTE_PEDIDOS = 'VENTAS_DESDE_PEDIDO'
FUENTE_NINGUNA = 'NINGUNA'
#: El orden ES la política: la primera que se pueda usar.
CASCADA = (FUENTE_SIESA, FUENTE_KARDEX, FUENTE_PEDIDOS)

NOMBRE_FUENTE = {
    FUENTE_SIESA: 'venta diaria sumada por Siesa (incluye la caja de las tiendas)',
    FUENTE_KARDEX: 'kardex de Siesa descargado',
    FUENTE_PEDIDOS: 'facturas desde pedido (sin la venta de caja de las tiendas)',
    FUENTE_NINGUNA: 'ninguna',
}

#: Días seguidos observados para que una fuente se pueda usar. Con menos, una
#: semana mala o buena decide la cantidad.
DIAS_MINIMOS_PARA_PROPONER = 28
#: El contenedor es irreversible 120 días (Regla 0): pide medio año observado.
DIAS_MINIMOS_CONTENEDOR = 180
#: La temporada y el bloqueo de recompra hablan del año: piden el año.
DIAS_MINIMOS_ANIO = 360

CONSULTA_VENTAS_DIA_DEFAULT = 'papeleriamedellin_WMS_Ventas_Dia'
CONSULTA_VENTAS_DIA_RECIENTE_DEFAULT = 'papeleriamedellin_WMS_Ventas_Dia_Reciente'
TAM_PAG = 100            # Regla 10

#: Columnas que devuelve la consulta (alias fijos: el lector depende de ellos).
COLUMNAS_VENTAS_DIA = ('orden', 'total_filas', 'ventana_desde', 'ventana_hasta',
                       'fecha', 'bodega', 'referencia', 'vendido', 'devuelto')

# ══════════════════════════════════════════════════════════════════════════════
# EL SQL — para registrar en Generic Transfer (Consultas dinámicas)
# ══════════════════════════════════════════════════════════════════════════════
#
# Lo que se midió en producción el 2026-09-27 y por qué el SQL tiene esta forma:
#
# · «Estructura invalida, el query a ejecutar no maneja parametros» (400) al
#   mandar `parametros` a una dinámica: la ventana de fechas va DENTRO del SQL
#   (como ya hace `papeleriamedellin_WMS_Remision_DesdePedido`), y por eso hay
#   dos registros: el histórico (400 días) y el reciente (14 días, el de todos
#   los días). Si el consultor sabe declarar parámetros en la consulta, una
#   sola basta — la lectura no cambia.
# · «The ORDER BY clause is invalid in views, inline functions, derived tables,
#   subqueries, and common table expressions, unless TOP, OFFSET or FOR XML is
#   also specified» (500, lo que hoy tiene `…_descubrir_tablas` en producción):
#   Connekta ENVUELVE el SQL en una subconsulta para paginar. Un ORDER BY
#   solo va con `OFFSET 0 ROWS`. Y como el orden de la subconsulta no está
#   garantizado afuera, el SQL numera sus filas (`orden`, ROW_NUMBER por la
#   clave del agregado) y declara cuántas son (`total_filas`): el WMS verifica
#   que llegaron 1…N, cada una una vez. La paginación inestable se VE; no se
#   supone arreglada.
# · Orden DESCENDENTE por fecha: lo más reciente primero. Una lectura que se
#   corta a la mitad igual deja los últimos meses completos, que es lo que el
#   punto de pedido necesita primero.
# · En el SQL las fechas llevan UNA comilla (es el texto de la consulta). La
#   doble comilla simple (Regla 15) es solo para `parametros`.
# · Sin `WITH` (una CTE no puede ir dentro de la subconsulta de Connekta).
#
# Tablas y columnas: t470 movimientos, t350 encabezado, t121/t120 ítem, t150
# bodega. **Los nombres de las columnas de enlace (`f470_rowid_docto`,
# `f470_rowid_item_ext`, `f470_rowid_bodega`) no están verificados**: el paso 0
# de la validación (`SQL_VALIDACION['esquema']`) los confirma antes de
# registrar nada.
SQL_VENTAS_DIA = """SELECT
    ROW_NUMBER() OVER (ORDER BY v.fecha DESC, v.bodega, v.referencia) AS orden,
    COUNT(*) OVER ()                                               AS total_filas,
    v.ventana_desde, v.ventana_hasta,
    v.fecha, v.bodega, v.referencia, v.vendido, v.devuelto, v.lineas
FROM (
    SELECT
        CAST(d.f350_fecha AS date)                                 AS fecha,
        RTRIM(b.f150_id)                                           AS bodega,
        RTRIM(i.f120_referencia)                                   AS referencia,
        SUM(CASE WHEN m.f470_id_concepto = 501 AND m.f470_ind_naturaleza = 2
                 THEN m.f470_cant_base ELSE 0 END)                 AS vendido,
        SUM(CASE WHEN m.f470_id_concepto = 502 AND m.f470_ind_naturaleza = 1
                 THEN m.f470_cant_base ELSE 0 END)                 AS devuelto,
        COUNT(*)                                                   AS lineas,
        MIN(w.desde)                                               AS ventana_desde,
        MIN(w.hasta)                                               AS ventana_hasta
    FROM t470_cm_movto_invent m
    INNER JOIN t350_co_docto_contable d     ON d.f350_rowid = m.f470_rowid_docto
    INNER JOIN t121_mc_items_extensiones e  ON e.f121_rowid = m.f470_rowid_item_ext
    INNER JOIN t120_mc_items i              ON i.f120_rowid = e.f121_rowid_item
    INNER JOIN t150_mc_bodegas b            ON b.f150_rowid = m.f470_rowid_bodega
    CROSS JOIN (SELECT CAST(DATEADD(DAY, -{dias}, GETDATE()) AS date) AS desde,
                       CAST(GETDATE() AS date)                       AS hasta) w
    WHERE d.f350_id_cia = 1
      AND d.f350_ind_estado = 1
      AND m.f470_id_concepto IN (501, 502)
      AND d.f350_fecha >= w.desde
    GROUP BY CAST(d.f350_fecha AS date), RTRIM(b.f150_id), RTRIM(i.f120_referencia)
) v
WHERE v.vendido <> 0 OR v.devuelto <> 0
ORDER BY v.fecha DESC, v.bodega, v.referencia
OFFSET 0 ROWS"""

DIAS_HISTORICO = 400
DIAS_RECIENTE = 14

#: Lo que se pega en `papeleriamedellin_pame_descubrir_tablas` para VALIDAR (una
#: a la vez; el script `qa_demanda_fuentes_real.py` reconoce cuál está puesta
#: por sus columnas). **Solo para descubrir: nunca una fuente de negocio.**
SQL_VALIDACION = {
    # 0 · ¿Existen las columnas de enlace con esos nombres?
    'esquema': """SELECT t.name AS tabla, c.name AS columna, ty.name AS tipo
FROM sys.tables t
INNER JOIN sys.columns c ON c.object_id = t.object_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE t.name IN ('t470_cm_movto_invent', 't350_co_docto_contable',
                 't121_mc_items_extensiones', 't120_mc_items', 't150_mc_bodegas')
  AND (c.name LIKE 'f470_rowid%' OR c.name LIKE 'f470_id_c%' OR c.name LIKE 'f470_ind_nat%'
       OR c.name LIKE 'f470_cant_base%' OR c.name IN ('f350_rowid', 'f350_fecha',
       'f350_ind_estado', 'f350_id_cia', 'f350_id_tipo_docto', 'f121_rowid',
       'f121_rowid_item', 'f120_rowid', 'f120_referencia', 'f150_rowid', 'f150_id'))""",
    # 1 · Un día: ¿con qué tipo de documento y concepto entra la venta de caja?
    'conceptos_dia': """SELECT RTRIM(b.f150_id) AS bodega, d.f350_id_tipo_docto AS tipo_docto,
       m.f470_id_concepto AS concepto, m.f470_ind_naturaleza AS naturaleza,
       d.f350_ind_estado AS estado, COUNT(*) AS lineas, SUM(m.f470_cant_base) AS unidades
FROM t470_cm_movto_invent m
INNER JOIN t350_co_docto_contable d ON d.f350_rowid = m.f470_rowid_docto
INNER JOIN t150_mc_bodegas b        ON b.f150_rowid = m.f470_rowid_bodega
WHERE d.f350_id_cia = 1 AND d.f350_fecha >= '20260925' AND d.f350_fecha < '20260926'
GROUP BY RTRIM(b.f150_id), d.f350_id_tipo_docto, m.f470_id_concepto,
         m.f470_ind_naturaleza, d.f350_ind_estado""",
    # 2 · Volumen: cuántas filas devolvería la consulta de ventas, por mes.
    'volumen_mes': """SELECT YEAR(v.fecha) AS anio, MONTH(v.fecha) AS mes, COUNT(*) AS filas,
       COUNT(DISTINCT v.referencia) AS referencias, COUNT(DISTINCT v.bodega) AS bodegas,
       SUM(v.vendido) AS vendido
FROM (
    SELECT CAST(d.f350_fecha AS date) AS fecha, RTRIM(b.f150_id) AS bodega,
           RTRIM(i.f120_referencia) AS referencia,
           SUM(CASE WHEN m.f470_id_concepto = 501 AND m.f470_ind_naturaleza = 2
                    THEN m.f470_cant_base ELSE 0 END) AS vendido
    FROM t470_cm_movto_invent m
    INNER JOIN t350_co_docto_contable d     ON d.f350_rowid = m.f470_rowid_docto
    INNER JOIN t121_mc_items_extensiones e  ON e.f121_rowid = m.f470_rowid_item_ext
    INNER JOIN t120_mc_items i              ON i.f120_rowid = e.f121_rowid_item
    INNER JOIN t150_mc_bodegas b            ON b.f150_rowid = m.f470_rowid_bodega
    WHERE d.f350_id_cia = 1 AND d.f350_ind_estado = 1
      AND m.f470_id_concepto IN (501, 502)
      AND d.f350_fecha >= '20250901'
    GROUP BY CAST(d.f350_fecha AS date), RTRIM(b.f150_id), RTRIM(i.f120_referencia)
) v
GROUP BY YEAR(v.fecha), MONTH(v.fecha)""",
    # 3 · Un SKU en un mes, por día y bodega: para cuadrar contra las facturas
    #     desde pedido del mismo mes y contra InvFecha.
    'sku_mes': SQL_VENTAS_DIA.replace('{dias}', '40').replace(
        'AND d.f350_fecha >= w.desde',
        "AND d.f350_fecha >= w.desde AND i.f120_referencia = 'PAPELSP9218'"),
}


def sql_ventas_dia(dias: int) -> str:
    """El SQL para registrar, con su ventana: `DIAS_HISTORICO` o `DIAS_RECIENTE`."""
    return SQL_VENTAS_DIA.replace('{dias}', str(int(dias)))


def consulta_ventas_dia(reciente: bool = False) -> str:
    if reciente:
        return (os.getenv('CONNEKTA_CONSULTA_VENTAS_DIA_RECIENTE')
                or CONSULTA_VENTAS_DIA_RECIENTE_DEFAULT)
    return os.getenv('CONNEKTA_CONSULTA_VENTAS_DIA') or CONSULTA_VENTAS_DIA_DEFAULT


def _entero_env(nombre, defecto, minimo, maximo):
    try:
        return max(minimo, min(maximo, int(os.getenv(nombre, defecto))))
    except (TypeError, ValueError):
        return defecto


def dias_frescura() -> int:
    """Hasta cuántos días puede tener el último día observado para decir que la
    fuente está al día (`DEMANDA_DIAS_FRESCURA`, 4: cubre un fin de semana
    largo)."""
    return _entero_env('DEMANDA_DIAS_FRESCURA', 4, 1, 60)


# ══════════════════════════════════════════════════════════════════════════════
# Lectura de una fila de la consulta
# ══════════════════════════════════════════════════════════════════════════════

def _fecha(v):
    if v in (None, ''):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    s = str(v).strip()
    try:
        if '-' in s[:10]:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        return datetime.strptime(s[:8], '%Y%m%d').date()
    except ValueError:
        return None


def _decimal(v):
    if v in (None, ''):
        return Decimal(0)
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _entero(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def leer_fila_venta_dia(row):
    """Una fila cruda → sus campos, o `None` si no se puede leer entera.

    Una fila ilegible NO se salta en silencio: quien lee la cuenta, y una
    lectura con filas ilegibles no está completa."""
    if not isinstance(row, dict):
        return None
    orden, total = _entero(row.get('orden')), _entero(row.get('total_filas'))
    fecha = _fecha(row.get('fecha'))
    ref = str(row.get('referencia') or '').strip()
    bod = str(row.get('bodega') or '').strip().upper()
    vend, dev = _decimal(row.get('vendido')), _decimal(row.get('devuelto'))
    desde, hasta = _fecha(row.get('ventana_desde')), _fecha(row.get('ventana_hasta'))
    if None in (orden, total, fecha, vend, dev, desde, hasta) or not ref or not bod:
        return None
    return {'orden': orden, 'total_filas': total, 'fecha': fecha, 'bodega': bod,
            'referencia': ref, 'vendido': vend, 'devuelto': dev,
            'lineas': _entero(row.get('lineas')),
            'ventana_desde': desde, 'ventana_hasta': hasta}


#: Compatibilidad con el borrador del script de medición.
leer_fila_venta_diaria = leer_fila_venta_dia
COLUMNAS_VENTAS_DIARIAS = COLUMNAS_VENTAS_DIA
CONSULTA_VENTAS_DIARIAS_DEFAULT = CONSULTA_VENTAS_DIA_DEFAULT
SQL_VALIDACION_UN_DIA = SQL_VALIDACION['conceptos_dia']


# ══════════════════════════════════════════════════════════════════════════════
# Qué días quedaron completos — puro, sin base
# ══════════════════════════════════════════════════════════════════════════════

def dias_completos(por_orden: dict, total: int, ultimo_cerrado: date):
    """Del conjunto de filas leídas (por `orden`), el tramo de días que se puede
    afirmar COMPLETO. Puro.

    El SQL ordena por fecha DESCENDENTE. Si llegaron las filas 1…k sin hueco, y
    la fila k+1 (también leída) es de un día anterior al de la k, entonces todo
    día posterior al de la k+1 está entero — incluidos los días SIN fila entre
    los dos, que son ceros verdaderos. Con k = total, todo día de la ventana
    está entero.

    Returns: (desde, hasta) o `None` — `hasta` recortado al último día CERRADO
    (hoy no: la caja de hoy todavía no está acumulada).
    """
    if not por_orden or not total:
        return None
    k = 0
    while (k + 1) in por_orden:
        k += 1
    if k == 0:
        return None
    ejemplo = por_orden[1]
    v_desde, v_hasta = ejemplo['ventana_desde'], ejemplo['ventana_hasta']
    hasta = min(v_hasta, ultimo_cerrado)
    if k >= total:
        desde = v_desde
    else:
        siguiente = por_orden.get(k + 1)
        if siguiente is None:
            # La k+1 no llegó: el día de la k puede tener filas pendientes.
            desde = por_orden[k]['fecha'] + timedelta(days=1)
        else:
            desde = siguiente['fecha'] + timedelta(days=1)
            if siguiente['fecha'] == por_orden[k]['fecha']:
                desde = por_orden[k]['fecha'] + timedelta(days=1)
    if desde > hasta:
        return None
    return desde, hasta


# ══════════════════════════════════════════════════════════════════════════════
# La descarga — solo GET, en segundo plano
# ══════════════════════════════════════════════════════════════════════════════

def descargar_ventas_dia(reciente: bool = False, gateway=None, max_paginas=None,
                         max_minutos=None, pausa_s=None, reloj=None) -> dict:
    """Lee la consulta de venta diaria y guarda **los días que quedaron
    completos**. Nunca levanta: el resultado dice qué pasó.

    - Toda página se pide por el endpoint dinámico, `tamPag` 100.
    - Una fila que llega dos veces con otro contenido, un `total_filas` que
      cambia a mitad, una ventana que cambia, una fila ilegible: la lectura
      queda INCOMPLETA y se dice por qué.
    - Una fila que falta (paginación inestable) se busca otra vez en su página
      (hasta 3 veces). Si sigue faltando, se guarda solo hasta el día anterior
      al hueco.
    - Cero filas no es «no hubo ventas»: es INCOMPLETA (una consulta mal
      registrada también contesta vacío).
    """
    from app.services import registro_sync_service as rs
    from app.services.connekta_gateway import _exigir_datos, connekta
    from app.services.kardex_service import totales_declarados
    from app.utils.fecha import dia_operativo

    gw = gateway or connekta
    consulta = consulta_ventas_dia(reciente)
    max_paginas = max_paginas or _entero_env('DEMANDA_MAX_PAGINAS', 12000, 1, 30000)
    max_minutos = max_minutos or _entero_env('DEMANDA_MAX_MINUTOS', 50, 1, 240)
    pausa = (pausa_s if pausa_s is not None
             else float(os.getenv('DEMANDA_PAUSA_S', '0.3') or 0.3))
    reloj = reloj or time.monotonic
    inicio = reloj()
    tipo_reg = 'demanda_siesa'
    reg_id = rs.abrir(tipo_reg)

    por_orden, problemas = {}, []
    total, total_declarado, ilegibles, paginas = None, None, 0, 0

    def _pedir(pag):
        res = gw._get(consulta, {'paginacion': f'numPag={pag}|tamPag={TAM_PAG}'},
                      url=gw.url_get_dinamico, timeout=90)
        if res is None:
            raise RuntimeError('circuito de Siesa abierto')
        det = res.get('detalle') if isinstance(res, dict) else None
        if not isinstance(det, dict):
            raise RuntimeError(f'respuesta sin detalle: {str(res)[:200]}')
        rows = det.get('Datos') if det.get('Datos') is not None else det.get('Table')
        if rows is None:
            raise RuntimeError('respuesta sin filas (ni Datos ni Table)')
        _exigir_datos(rows, consulta)
        return rows, totales_declarados(res)[0]

    def _absorber(rows):
        nonlocal total, ilegibles
        for r in rows:
            f = leer_fila_venta_dia(r)
            if f is None:
                ilegibles += 1
                continue
            if total is None:
                total = f['total_filas']
            elif f['total_filas'] != total:
                problemas.append(f'total_filas cambió de {total} a {f["total_filas"]} '
                                 'a mitad de la lectura (Siesa recalculó: datos nuevos)')
            previa = por_orden.get(f['orden'])
            if previa is not None and (previa['fecha'], previa['bodega'],
                                       previa['referencia']) != (f['fecha'], f['bodega'],
                                                                  f['referencia']):
                problemas.append(f'la fila {f["orden"]} llegó dos veces con otro '
                                 'contenido: el orden no es estable')
                continue
            if (por_orden and f['ventana_desde'] !=
                    next(iter(por_orden.values()))['ventana_desde']):
                problemas.append('la ventana de la consulta cambió a mitad (pasó la '
                                 'medianoche en Siesa)')
            por_orden.setdefault(f['orden'], f)

    motivo = None
    pag = 0
    try:
        for pag in range(1, max_paginas + 1):
            if (reloj() - inicio) / 60.0 > max_minutos:
                motivo = f'se acabó el tiempo ({max_minutos} min) en la página {pag}'
                break
            rows, declarado = _pedir(pag)
            paginas = pag
            if declarado is not None:
                total_declarado = declarado
            _absorber(rows)
            if problemas:
                break
            if total is not None and len(por_orden) >= total:
                break
            if len(rows) < TAM_PAG:
                break
            if pausa:
                time.sleep(pausa)
        else:
            motivo = f'se agotaron {max_paginas} páginas sin llegar al final'
        # Lo que falta: se busca en su página (la paginación inestable se ve acá).
        if not problemas and total:
            for _intento in range(3):
                faltan = [o for o in range(1, total + 1) if o not in por_orden]
                if not faltan:
                    break
                for p in sorted({(o - 1) // TAM_PAG + 1 for o in faltan[:300]}):
                    if (reloj() - inicio) / 60.0 > max_minutos:
                        break
                    rows, _d = _pedir(p)
                    paginas += 1
                    _absorber(rows)
    except Exception as e:                                    # noqa: BLE001
        motivo = f'página {pag}: {str(e)[:300]}'

    ultimo_cerrado = dia_operativo() - timedelta(days=1)
    faltantes = (total - len(por_orden)) if total else None
    if total_declarado is not None and total is not None and total_declarado != total:
        problemas.append(f'Siesa declaró {total_declarado} registros y la consulta '
                         f'dice {total}')
    if ilegibles:
        problemas.append(f'{ilegibles} fila(s) ilegibles (¿el SQL registrado tiene las '
                         f'columnas {", ".join(COLUMNAS_VENTAS_DIA)}?)')
    if total is None and not motivo:
        motivo = ('la consulta no devolvió filas: no se sabe si no hubo ventas o si '
                  'está mal registrada')
    completa = (not problemas and not motivo and total is not None
                and faltantes == 0)
    tramo = dias_completos(por_orden, total or 0, ultimo_cerrado) if not problemas else None

    guardadas = 0
    if tramo:
        guardadas = _guardar_lectura(por_orden.values(), tramo, reg_id, consulta)

    resultado = {
        'consulta': consulta, 'completa': completa, 'paginas': paginas,
        'filas_declaradas': total, 'filas_leidas': len(por_orden),
        'faltantes': faltantes, 'ilegibles': ilegibles,
        'dias_guardados': ({'desde': tramo[0].isoformat(), 'hasta': tramo[1].isoformat()}
                           if tramo else None),
        'filas_guardadas': guardadas,
        'motivo': motivo or ('; '.join(problemas[:5]) if problemas else None),
        'minutos': round((reloj() - inicio) / 60.0, 1),
    }
    if completa:
        rs.cerrar_ok(reg_id, resultado)
    else:
        rs.cerrar_error(reg_id, resultado['motivo'] or f'faltan {faltantes} filas',
                        resultado)
    logger.info('[DEMANDA] %s: %s', consulta, resultado)
    return resultado


def _guardar_lectura(filas, tramo, registro_id, consulta) -> int:
    """Upsert de las filas del tramo completo + los días como cubiertos.

    Una fila que ya estaba y esta lectura (completa para ese día) no trae —una
    factura anulada— queda en cero: no se borra (Regla de la bitácora)."""
    from app.models.demanda_siesa import DemandaDiaCubierto, DemandaDiaSiesa

    desde, hasta = tramo
    ahora = datetime.utcnow()
    nuevas = {(f['fecha'], f['bodega'], f['referencia']): f for f in filas
              if desde <= f['fecha'] <= hasta}
    existentes = {(e.fecha, e.bodega, e.referencia): e for e in
                  DemandaDiaSiesa.query.filter(DemandaDiaSiesa.fecha >= desde,
                                               DemandaDiaSiesa.fecha <= hasta).all()}
    for clave, f in nuevas.items():
        e = existentes.get(clave)
        if e is None:
            e = DemandaDiaSiesa(fecha=clave[0], bodega=clave[1], referencia=clave[2])
            db.session.add(e)
        e.vendido, e.devuelto, e.lineas = f['vendido'], f['devuelto'], f['lineas']
        e.registro_id, e.actualizada_en = registro_id, ahora
    for clave, e in existentes.items():
        if clave not in nuevas and (e.vendido or e.devuelto):
            e.vendido, e.devuelto, e.lineas = 0, 0, 0
            e.registro_id, e.actualizada_en = registro_id, ahora
    cubiertos = {c.fecha: c for c in DemandaDiaCubierto.query.filter(
        DemandaDiaCubierto.fecha >= desde, DemandaDiaCubierto.fecha <= hasta).all()}
    d = desde
    while d <= hasta:
        c = cubiertos.get(d) or DemandaDiaCubierto(fecha=d)
        if d not in cubiertos:
            db.session.add(c)
        c.registro_id, c.consulta, c.leido_en = registro_id, consulta, ahora
        d += timedelta(days=1)
    db.session.commit()
    return len(nuevas)


def _hilo(app, lock, etiqueta, fn, lanzar=None):
    import threading

    def _run():
        from app.utils.lock import advisory_lock
        with app.app_context():
            with advisory_lock(lock, etiqueta) as tomado:
                if not tomado:
                    logger.info('[DEMANDA] %s: otro proceso ya está leyendo', etiqueta)
                    return
                try:
                    fn()
                except Exception as e:                    # noqa: BLE001 — un hilo no tiene a quién avisar
                    db.session.rollback()
                    logger.error('[DEMANDA] %s falló: %s', etiqueta, e, exc_info=True)

    (lanzar or (lambda f: threading.Thread(target=f, daemon=True).start()))(_run)


def disparar_descarga(app, reciente=False, lanzar=None) -> dict:
    """El botón «Leer de Siesa»: corre en un hilo con `LOCK_DEMANDA_SIESA`."""
    from app.services.ventana_siesa import texto_ventana, ventana_abierta
    from app.utils.lock import LOCK_DEMANDA_SIESA
    if not ventana_abierta():
        return {'ok': False, 'codigo': 409,
                'error': f'Fuera de la ventana de Siesa ({texto_ventana()}).'}
    _hilo(app, LOCK_DEMANDA_SIESA, 'demanda_siesa',
          lambda: descargar_ventas_dia(reciente=reciente), lanzar)
    return {'ok': True, 'codigo': 202,
            'mensaje': ('Lectura de la venta diaria de Siesa iniciada en segundo plano. '
                        'El resultado queda en el estado de la demanda.')}


def rellenar_ventas_pedido(dias: int = 90, gateway=None, hasta=None) -> dict:
    """Fotografía las facturas desde pedido de los últimos `dias` días, por CO y
    día (la foto de siempre, `fotos_siesa_service.fotografiar_ventas`). Es el
    respaldo parcial de la demanda mientras la consulta agregada no exista.
    Medido en producción el 2026-09-27: el CO 003 son ~5 páginas por día hábil
    y el CO 004 ~2; los demás CO no venden por pedido (0 filas, 1 página)."""
    from app.services import registro_sync_service as rs
    from app.services.fotos_siesa_service import cos_operados, fotografiar_ventas
    from app.utils.fecha import dia_operativo
    hasta = hasta or (dia_operativo() - timedelta(days=1))
    dias = max(1, min(int(dias), 400))
    reg_id = rs.abrir('demanda_pedidos')
    completas, incompletas = 0, []
    for i in range(dias):
        d = hasta - timedelta(days=i)
        for co in cos_operados():
            r = fotografiar_ventas(co, d, gateway=gateway)
            if r.get('completa'):
                completas += 1
            else:
                incompletas.append(f"{co} {d.isoformat()}: {r.get('motivo')}")
    resultado = {'dias': dias, 'hasta': hasta.isoformat(), 'corridas_completas': completas,
                 'incompletas': incompletas[:50], 'n_incompletas': len(incompletas)}
    if incompletas:
        rs.cerrar_error(reg_id, f'{len(incompletas)} corrida(s) incompletas', resultado)
    else:
        rs.cerrar_ok(reg_id, resultado)
    return resultado


def disparar_relleno_pedidos(app, dias=90, lanzar=None) -> dict:
    from app.services.ventana_siesa import texto_ventana, ventana_abierta
    from app.utils.lock import LOCK_DEMANDA_PEDIDOS
    if not ventana_abierta():
        return {'ok': False, 'codigo': 409,
                'error': f'Fuera de la ventana de Siesa ({texto_ventana()}).'}
    _hilo(app, LOCK_DEMANDA_PEDIDOS, 'demanda_pedidos',
          lambda: rellenar_ventas_pedido(dias), lanzar)
    return {'ok': True, 'codigo': 202,
            'mensaje': f'Fotografiando las facturas desde pedido de {int(dias)} días en '
                       'segundo plano.'}


# ══════════════════════════════════════════════════════════════════════════════
# Cobertura de cada fuente
# ══════════════════════════════════════════════════════════════════════════════

def _tramo_final(dias_observados, hasta_max=None) -> dict:
    """El tramo contiguo que termina en el día observado más reciente.

    Un hueco adentro corta el tramo (`dias_antes_del_hueco` se declara):
    contarlo como cero diluiría la demanda."""
    obs = sorted({d for d in dias_observados if d and (hasta_max is None or d <= hasta_max)})
    if not obs:
        return {'desde': None, 'hasta': None, 'dias': 0, 'dias_antes_del_hueco': 0}
    hasta = obs[-1]
    desde = hasta
    i = len(obs) - 2
    while i >= 0 and obs[i] == desde - timedelta(days=1):
        desde = obs[i]
        i -= 1
    return {'desde': desde, 'hasta': hasta, 'dias': (hasta - desde).days + 1,
            'dias_antes_del_hueco': i + 1}


def cobertura_siesa() -> dict:
    from sqlalchemy import func
    from app.models.demanda_siesa import DemandaDiaCubierto
    fechas = [r[0] for r in db.session.query(DemandaDiaCubierto.fecha).all()]
    t = _tramo_final(fechas)
    t['leido_en'] = db.session.query(func.max(DemandaDiaCubierto.leido_en)).scalar()
    return t


def cobertura_kardex(desde_ventana: date, hoy: date) -> dict:
    """El kardex con su regla de siempre (`ventana_observada`): observa desde su
    primer movimiento hasta hoy — la descarga declara si está completa, y eso
    lo juzga `salud_kardex`. Se puede usar si su último movimiento cae dentro
    de la ventana: el de producción (enero de 2024, 26 días) no cubre nada de
    hoy y no se usa."""
    from sqlalchemy import func
    from app.services.kardex_service import KardexMovimiento
    f_min, f_max, n = db.session.query(
        func.min(KardexMovimiento.fecha), func.max(KardexMovimiento.fecha),
        func.count(KardexMovimiento.id)).one()
    if not n or f_max is None or f_max < desde_ventana:
        return {'desde': f_min, 'hasta': f_max, 'dias': 0, 'movimientos': int(n or 0)}
    desde = max(f_min, desde_ventana)
    return {'desde': f_min, 'hasta': f_max, 'dias': (hoy - desde).days + 1,
            'movimientos': int(n)}


def cobertura_pedidos() -> dict:
    """Días en que TODO CO operado tiene su foto de ventas COMPLETA."""
    from app.models.fotos_siesa import FotoCorrida, TipoFoto
    from app.services.fotos_siesa_service import cos_operados
    por_dia = {}
    for alcance, dia in (db.session.query(FotoCorrida.alcance, FotoCorrida.dia_operativo)
                         .filter(FotoCorrida.tipo == TipoFoto.VENTAS,
                                 FotoCorrida.completa.is_(True)).distinct().all()):
        por_dia.setdefault(dia, set()).add(str(alcance))
    if not por_dia:
        return {'desde': None, 'hasta': None, 'dias': 0, 'dias_antes_del_hueco': 0,
                'cos': []}
    cos = set(cos_operados())
    t = _tramo_final([d for d, s in por_dia.items() if cos and cos <= s])
    t['cos'] = sorted(cos)
    return t


# ══════════════════════════════════════════════════════════════════════════════
# LA POLÍTICA — de qué fuente sale la demanda
# ══════════════════════════════════════════════════════════════════════════════

def fuente_de_demanda(hoy: date = None, ventana_meses: int = 12) -> dict:
    """**La única** respuesta a «¿de dónde sale la demanda y qué tan buena es?».

    Recorre `CASCADA` y elige la primera fuente con al menos
    `DIAS_MINIMOS_PARA_PROPONER` días seguidos observados dentro de la ventana.
    `apta_para` dice qué decisiones se pueden tomar con ella; `no_apta_por`,
    por qué no las otras. Nunca levanta: una fuente que no se pudo leer se
    declara y se pasa a la siguiente.
    """
    from app.services.kardex_service import ventana_demanda
    from app.utils.fecha import dia_operativo

    hoy = hoy or dia_operativo()
    ayer = hoy - timedelta(days=1)
    desde_v, _h = ventana_demanda(ventana_meses, hoy)
    fresc = dias_frescura()
    candidatas = []

    def _lee(fuente, fn):
        try:
            return fn()
        except Exception as e:                                # noqa: BLE001
            db.session.rollback()
            logger.warning('[DEMANDA] no se pudo leer la cobertura de %s: %s', fuente, e)
            return {'desde': None, 'hasta': None, 'dias': 0, 'error': str(e)[:200]}

    cob = {
        FUENTE_SIESA: _lee(FUENTE_SIESA, cobertura_siesa),
        FUENTE_KARDEX: _lee(FUENTE_KARDEX, lambda: cobertura_kardex(desde_v, hoy)),
        FUENTE_PEDIDOS: _lee(FUENTE_PEDIDOS, cobertura_pedidos),
    }
    for f in CASCADA:
        c = cob[f]
        hasta = c.get('hasta')
        if f == FUENTE_KARDEX:
            dias_util = c.get('dias') or 0
            usable = dias_util > 0
        else:
            # Solo cuenta lo que cae dentro de la ventana.
            dias_util = (((hasta - max(c['desde'], desde_v)).days + 1)
                         if hasta and hasta >= desde_v else 0)
            usable = dias_util >= DIAS_MINIMOS_PARA_PROPONER
        atraso = (ayer - hasta).days if hasta else None
        tolerancia = _frescura_kardex() if f == FUENTE_KARDEX else fresc
        candidatas.append({
            'fuente': f, 'nombre': NOMBRE_FUENTE[f],
            'usable': usable,
            'dias_en_ventana': dias_util,
            'desde': c.get('desde').isoformat() if c.get('desde') else None,
            'hasta': hasta.isoformat() if hasta else None,
            'dias_de_atraso': atraso,
            'al_dia': atraso is not None and atraso <= tolerancia,
            'incluye_caja': f != FUENTE_PEDIDOS,
            'error': c.get('error'),
            'detalle': {k: v for k, v in c.items()
                        if k in ('dias_antes_del_hueco', 'movimientos', 'cos')},
        })

    elegida = next((c for c in candidatas if c['usable']), None)
    if elegida is None:
        return {
            'fuente': FUENTE_NINGUNA, 'nombre': NOMBRE_FUENTE[FUENTE_NINGUNA],
            'hay_dato': False, 'parcial': False, 'incluye_caja': False,
            'cobertura': {'desde': None, 'hasta': None, 'dias': 0},
            'al_dia': False,
            'apta_para': {k: False for k in ('bandeja', 'contenedor', 'temporada',
                                             'bloqueo')},
            'no_apta_por': {k: _texto_sin_fuente() for k in ('bandeja', 'contenedor',
                                                             'temporada', 'bloqueo')},
            'texto': _texto_sin_fuente(),
            'que_hacer': _que_hacer(candidatas),
            'candidatas': candidatas,
        }

    f = elegida['fuente']
    dias = elegida['dias_en_ventana']
    caja = elegida['incluye_caja']
    al_dia = elegida['al_dia']
    no_apta = {}
    if not al_dia:
        no_apta['bandeja'] = (f'La última venta observada es del {elegida["hasta"]}: '
                              f'{elegida["dias_de_atraso"]} días de atraso.')
    if not caja:
        motivo = ('Solo se ven las facturas desde pedido: falta la venta de caja de '
                  'las tiendas.')
        no_apta['contenedor'] = motivo
        no_apta['temporada'] = motivo
        no_apta['bloqueo'] = motivo
    if dias < DIAS_MINIMOS_CONTENEDOR:
        no_apta.setdefault('contenedor', f'Solo {dias} días observados; el contenedor '
                                         f'pide {DIAS_MINIMOS_CONTENEDOR}.')
    if dias < DIAS_MINIMOS_ANIO:
        no_apta.setdefault('temporada', f'Solo {dias} días observados; la temporada '
                                        'pide el año anterior.')
        no_apta.setdefault('bloqueo', f'Solo {dias} días observados: no se puede '
                                      'afirmar que algo no se vende en el año.')
    if not al_dia:
        for k in ('contenedor', 'bloqueo'):
            no_apta.setdefault(k, no_apta['bandeja'])
    apta = {k: k not in no_apta for k in ('bandeja', 'contenedor', 'temporada', 'bloqueo')}
    parcial = not caja
    texto = (f'Ventas de {NOMBRE_FUENTE[f]}: {dias} días observados, hasta el '
             f'{elegida["hasta"]}.')
    if parcial:
        texto += (' Es una COTA INFERIOR: sin la venta de caja de las tiendas, las '
                  'cantidades salen cortas.')
    return {
        'fuente': f, 'nombre': NOMBRE_FUENTE[f], 'hay_dato': True,
        'parcial': parcial, 'incluye_caja': caja,
        'cobertura': {'desde': elegida['desde'], 'hasta': elegida['hasta'], 'dias': dias},
        'al_dia': al_dia, 'dias_de_atraso': elegida['dias_de_atraso'],
        'apta_para': apta, 'no_apta_por': no_apta, 'texto': texto,
        'que_hacer': _que_hacer(candidatas) if (parcial or not al_dia or f != FUENTE_SIESA)
        else None,
        'candidatas': candidatas,
    }


def _frescura_kardex():
    from app.services.kardex_service import dias_frescura as _df
    return _df()


def _texto_sin_fuente():
    return ('No hay ventas cargadas de ninguna fuente con al menos '
            f'{DIAS_MINIMOS_PARA_PROPONER} días seguidos: no se puede calcular '
            'cuánto se vende.')


def _que_hacer(candidatas):
    siesa = next(c for c in candidatas if c['fuente'] == FUENTE_SIESA)
    if not siesa['usable']:
        return ('Registrar en Siesa la consulta de venta diaria '
                f'({CONSULTA_VENTAS_DIA_DEFAULT}, con el SQL de «Compras: de dónde sale '
                'la demanda») y leerla en 🧾 Fuentes → Demanda. Mientras tanto, '
                'fotografiar las facturas desde pedido de los últimos 90 días da una '
                'demanda parcial.')
    return 'Leer la venta reciente de Siesa en 🧾 Fuentes → Demanda.'


def ventana_de_la_fuente(fuente: dict, desde, hasta):
    """(desde, hasta) recortados a lo que la fuente observó — `None` si nada.

    El kardex conserva su regla de siempre (desde su primer movimiento hasta
    hoy: `ventana_observada`); las otras dos, su tramo contiguo."""
    f = (fuente or {}).get('fuente')
    cob = (fuente or {}).get('cobertura') or {}
    if f in (FUENTE_SIESA, FUENTE_PEDIDOS) and cob.get('desde'):
        c_desde = date.fromisoformat(cob['desde'])
        c_hasta = date.fromisoformat(cob['hasta'])
        d, h = max(desde, c_desde), min(hasta, c_hasta)
        return (d, h) if d <= h else (None, h)
    return None
