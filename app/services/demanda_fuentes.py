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

CONSULTA_VENTAS_DIA_RECIENTE_DEFAULT = 'papeleriamedellin_WMS_Ventas_Dia_Reciente'
#: Prefijo de las consultas del histórico, una por período: el nombre completo
#: es `<prefijo>_<etiqueta>` (`papeleriamedellin_WMS_Ventas_2026T3`; por mes,
#: `…_202609`). Ver `ventanas_historicas`.
PREFIJO_CONSULTA_PERIODO_DEFAULT = 'papeleriamedellin_WMS_Ventas'
#: El histórico relativo de 400 días (una sola consulta) se retiró el
#: 2026-09-27 sin llegar a registrarse: siempre volvía a la página 1 y nunca
#: completaba el año (P1-2). Se conserva el nombre para leer registros viejos.
CONSULTA_VENTAS_DIA_DEFAULT = 'papeleriamedellin_WMS_Ventas_Dia'
TAM_PAG = 100            # Regla 10

#: Columnas que devuelve la consulta (alias fijos: el lector depende de ellos).
#: Las seis últimas (valor y costo, 2026-09-27) son opcionales al leer: una
#: consulta registrada antes las omite y el valor queda desconocido, no cero.
COLUMNAS_VENTAS_DIA = ('orden', 'total_filas', 'ventana_desde', 'ventana_hasta',
                       'fecha', 'bodega', 'referencia', 'vendido', 'devuelto')
COLUMNAS_VALOR = ('valor_vendido', 'valor_devuelto', 'costo_vendido', 'costo_devuelto',
                  'lineas_sin_valor', 'lineas_sin_costo')

# ══════════════════════════════════════════════════════════════════════════════
# EL SQL — para registrar en Generic Transfer (Consultas dinámicas)
# ══════════════════════════════════════════════════════════════════════════════
#
# Lo que se midió en producción el 2026-09-27 y por qué el SQL tiene esta forma:
#
# · «Estructura invalida, el query a ejecutar no maneja parametros» (400) al
#   mandar `parametros` a una dinámica: la ventana de fechas va DENTRO del SQL.
#   Por eso hay UNA consulta por ventana: la reciente (relativa, 14 días, la de
#   todos los días) y una por PERÍODO del histórico, con fechas FIJAS
#   (`ventanas_historicas`). Si el consultor sabe declarar parámetros en la
#   consulta, una sola basta — la lectura no cambia.
# · «The ORDER BY clause is invalid in views, inline functions, derived tables,
#   subqueries, and common table expressions, unless TOP, OFFSET or FOR XML is
#   also specified» (500): Connekta ENVUELVE el SQL en una subconsulta para
#   paginar. Un ORDER BY solo va con `OFFSET 0 ROWS`. Y como el orden de la
#   subconsulta no está garantizado afuera, el SQL numera sus filas (`orden`,
#   ROW_NUMBER por la clave del agregado) y declara cuántas son
#   (`total_filas`): el WMS verifica que llegaron 1…N, cada una una vez.
# · Orden DESCENDENTE por fecha: lo más reciente primero. Una lectura cortada
#   deja completos los días más nuevos, y la cobertura crece hacia atrás desde
#   la reciente sin huecos (`_tramo_final` exige el tramo contiguo).
# · Los períodos tienen desde y hasta FIJOS (el hasta del período en curso es
#   el día anterior a copiar el SQL; lo que sigue lo cubre la reciente). Con
#   fechas fijas la numeración no cambia de un día a otro, y **la lectura se
#   retoma donde quedó** (`DemandaVentanaLectura`) en vez de volver a la página
#   1 (P1-2). Antes de retomar se relee la última fila guardada: si cambió (un
#   documento anulado o fechado atrás), el período se relee entero.
# · Medido en producción el 2026-09-27 con `API_v2_Ventas_Facturas_DesdePedido`
#   (lo único de ventas que se puede leer hoy): una página tarda 1–4 s con la
#   caché caliente, **casi igual para 1, 7, 30 o 90 días y para la página 1 o
#   la 90**; la PRIMERA consulta en frío tardó 108 s (7 días). Por eso cada
#   página tiene tres intentos y un tope de 150 s, y el costo de un período lo
#   pone su cantidad de páginas, no su largo: trimestres (5 consultas para 400
#   días) y no meses (14).
# · En el SQL las fechas llevan UNA comilla (es el texto de la consulta). La
#   doble comilla simple (Regla 15) es solo para `parametros`.
# · Sin `WITH` (una CTE no puede ir dentro de la subconsulta de Connekta).
#
# Tablas y columnas: t470 movimientos, t350 encabezado, t121/t120 ítem, t150
# bodega. **Los nombres de las columnas de enlace (`f470_rowid_docto`,
# `f470_rowid_item_ext`, `f470_rowid_bodega`) y las de valor y costo
# (`f470_vlr_neto`, `f470_vlr_imp`, `f470_vlr_bruto`, `f470_vlr_dscto_linea`,
# `f470_vlr_dscto_global`, `f470_costo_prom_tot`) no están verificados contra
# la T470**: las de valor son los alias con que `API_v2_Ventas_Facturas_
# DesdePedido` las devuelve (vistos en producción). El paso 0 de la validación
# (`SQL_VALIDACION['esquema']`) los confirma antes de registrar nada.
#
# Valor = SIN impuesto, la misma regla de `vigia_service.valor_linea_sin_impuesto`
# (neto − impuesto; si falta, bruto − descuentos): no se inventa una tasa de IVA.
# Costo = `f470_costo_prom_tot`, el costo promedio de la línea. Una línea sin
# valor (o sin costo) no suma cero en silencio: se cuenta en `lineas_sin_valor`
# (`lineas_sin_costo`) y el WMS lo declara.

SQL_VALOR_SIN_IMPUESTO = ('COALESCE(m.f470_vlr_neto - m.f470_vlr_imp, m.f470_vlr_bruto '
                          '- COALESCE(m.f470_vlr_dscto_linea, 0) '
                          '- COALESCE(m.f470_vlr_dscto_global, 0))')
SQL_COSTO = 'm.f470_costo_prom_tot'
_ES_VENTA = 'm.f470_id_concepto = 501 AND m.f470_ind_naturaleza = 2'
_ES_DEVOLUCION = 'm.f470_id_concepto = 502 AND m.f470_ind_naturaleza = 1'

_SQL_PLANTILLA = """SELECT
    ROW_NUMBER() OVER (ORDER BY v.fecha {dir}, v.bodega, v.referencia) AS orden,
    COUNT(*) OVER ()                                               AS total_filas,
    v.ventana_desde, v.ventana_hasta,
    v.fecha, v.bodega, v.referencia, v.vendido, v.devuelto, v.lineas,
    v.valor_vendido, v.valor_devuelto, v.costo_vendido, v.costo_devuelto,
    v.lineas_sin_valor, v.lineas_sin_costo
FROM (
    SELECT
        CAST(d.f350_fecha AS date)                                 AS fecha,
        RTRIM(b.f150_id)                                           AS bodega,
        RTRIM(i.f120_referencia)                                   AS referencia,
        SUM(CASE WHEN {venta} THEN m.f470_cant_base ELSE 0 END)    AS vendido,
        SUM(CASE WHEN {devol} THEN m.f470_cant_base ELSE 0 END)    AS devuelto,
        COUNT(*)                                                   AS lineas,
        SUM(CASE WHEN {venta} THEN {valor} ELSE 0 END)             AS valor_vendido,
        SUM(CASE WHEN {devol} THEN {valor} ELSE 0 END)             AS valor_devuelto,
        SUM(CASE WHEN {venta} THEN {costo} ELSE 0 END)             AS costo_vendido,
        SUM(CASE WHEN {devol} THEN {costo} ELSE 0 END)             AS costo_devuelto,
        SUM(CASE WHEN {valor} IS NULL THEN 1 ELSE 0 END)           AS lineas_sin_valor,
        SUM(CASE WHEN {costo} IS NULL THEN 1 ELSE 0 END)           AS lineas_sin_costo,
        MIN(w.desde)                                               AS ventana_desde,
        MIN(w.hasta)                                               AS ventana_hasta
    FROM t470_cm_movto_invent m
    INNER JOIN t350_co_docto_contable d     ON d.f350_rowid = m.f470_rowid_docto
    INNER JOIN t121_mc_items_extensiones e  ON e.f121_rowid = m.f470_rowid_item_ext
    INNER JOIN t120_mc_items i              ON i.f120_rowid = e.f121_rowid_item
    INNER JOIN t150_mc_bodegas b            ON b.f150_rowid = m.f470_rowid_bodega
    CROSS JOIN ({ventana}) w
    WHERE d.f350_id_cia = 1
      AND d.f350_ind_estado = 1
      AND m.f470_id_concepto IN (501, 502)
      AND d.f350_fecha >= w.desde
      AND d.f350_fecha < DATEADD(DAY, 1, w.hasta)
    GROUP BY CAST(d.f350_fecha AS date), RTRIM(b.f150_id), RTRIM(i.f120_referencia)
) v
WHERE v.vendido <> 0 OR v.devuelto <> 0
ORDER BY v.fecha {dir}, v.bodega, v.referencia
OFFSET 0 ROWS"""

#: La ventana relativa (la reciente): de hoy − N días a HOY (hoy entra; el
#: lector no guarda el día en curso, que todavía no está cerrado).
_VENTANA_RELATIVA = ("SELECT CAST(DATEADD(DAY, -{dias}, GETDATE()) AS date) AS desde, "
                     "CAST(GETDATE() AS date) AS hasta")
#: La ventana de un período, con las dos fechas FIJAS: la numeración no se
#: mueve de un día a otro y la lectura se puede retomar.
_VENTANA_FIJA = "SELECT CAST('{desde}' AS date) AS desde, CAST('{hasta}' AS date) AS hasta"


def _sql(ventana_sql: str, direccion: str) -> str:
    return (_SQL_PLANTILLA.replace('{venta}', _ES_VENTA).replace('{devol}', _ES_DEVOLUCION)
            .replace('{valor}', SQL_VALOR_SIN_IMPUESTO).replace('{costo}', SQL_COSTO)
            .replace('{ventana}', ventana_sql).replace('{dir}', direccion))


#: La reciente, con `{dias}` por reemplazar (`sql_ventas_dia`).
SQL_VENTAS_DIA = _sql(_VENTANA_RELATIVA, 'DESC')

DIAS_HISTORICO = 400
DIAS_RECIENTE = 14


def sql_ventas_dia(dias: int) -> str:
    """El SQL de la consulta reciente, con su ventana (`DIAS_RECIENTE`)."""
    return SQL_VENTAS_DIA.replace('{dias}', str(int(dias)))


def sql_ventas_periodo(desde: date, hasta: date) -> str:
    """El SQL de la consulta de un período del histórico, con sus fechas fijas."""
    return _sql(_VENTANA_FIJA.replace('{desde}', desde.strftime('%Y%m%d'))
                .replace('{hasta}', hasta.strftime('%Y%m%d')), 'DESC')


#: Lo que se pega en `papeleriamedellin_pame_descubrir_tablas` para VALIDAR (una
#: a la vez; el script `qa_demanda_fuentes_real.py` reconoce cuál está puesta
#: por sus columnas). **Solo para descubrir: nunca una fuente de negocio.**
SQL_VALIDACION = {
    # 0 · ¿Existen las columnas de enlace, de valor y de costo con esos nombres?
    'esquema': """SELECT t.name AS tabla, c.name AS columna, ty.name AS tipo
FROM sys.tables t
INNER JOIN sys.columns c ON c.object_id = t.object_id
INNER JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE t.name IN ('t470_cm_movto_invent', 't350_co_docto_contable',
                 't121_mc_items_extensiones', 't120_mc_items', 't150_mc_bodegas')
  AND (c.name LIKE 'f470_rowid%' OR c.name LIKE 'f470_id_c%' OR c.name LIKE 'f470_ind_nat%'
       OR c.name LIKE 'f470_cant_base%' OR c.name LIKE 'f470_vlr%' OR c.name LIKE 'f470_costo%'
       OR c.name IN ('f350_rowid', 'f350_fecha',
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
    # 2 · Volumen: cuántas filas devolvería la consulta de ventas, por mes (de
    #     eso sale cuántas páginas lee cada período).
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
      AND d.f350_fecha >= '20250801'
    GROUP BY CAST(d.f350_fecha AS date), RTRIM(b.f150_id), RTRIM(i.f120_referencia)
) v
GROUP BY YEAR(v.fecha), MONTH(v.fecha)""",
    # 3 · Un SKU en un mes, por día y bodega, con valor y costo: para cuadrar
    #     contra las facturas desde pedido del mismo mes y contra InvFecha.
    'sku_mes': sql_ventas_dia(40).replace(
        'AND d.f350_fecha < DATEADD(DAY, 1, w.hasta)',
        "AND d.f350_fecha < DATEADD(DAY, 1, w.hasta) AND i.f120_referencia = 'PAPELSP9218'"),
}


def consulta_ventas_dia(reciente: bool = False) -> str:
    """El nombre de la consulta reciente; sin `reciente`, cómo se llaman las
    del histórico (una por período)."""
    if reciente:
        return (os.getenv('CONNEKTA_CONSULTA_VENTAS_DIA_RECIENTE')
                or CONSULTA_VENTAS_DIA_RECIENTE_DEFAULT)
    vs = ventanas_historicas()
    return (f'{prefijo_consulta_periodo()}_<período>, una por {periodo_historico().lower()} '
            f'({vs[0]["consulta"]} … {vs[-1]["consulta"]})' if vs else prefijo_consulta_periodo())


# ══════════════════════════════════════════════════════════════════════════════
# Las ventanas del histórico — una consulta por período, con fechas fijas
# ══════════════════════════════════════════════════════════════════════════════

PERIODO_MES = 'MES'
PERIODO_TRIMESTRE = 'TRIMESTRE'


def periodo_historico() -> str:
    """`DEMANDA_PERIODO_HISTORICO`: TRIMESTRE (por defecto: 5 consultas para
    400 días) o MES (14; solo si una página de un trimestre resulta demasiado
    lenta). Tiene que coincidir con lo registrado en Siesa; un valor ilegible
    es TRIMESTRE, declarado en el log."""
    v = (os.getenv('DEMANDA_PERIODO_HISTORICO') or PERIODO_TRIMESTRE).strip().upper()
    if v not in (PERIODO_MES, PERIODO_TRIMESTRE):
        logger.warning('[DEMANDA] DEMANDA_PERIODO_HISTORICO=%r ilegible: se usa TRIMESTRE', v)
        return PERIODO_TRIMESTRE
    return v


def prefijo_consulta_periodo() -> str:
    return (os.getenv('CONNEKTA_CONSULTA_VENTAS_PERIODO_PREFIJO')
            or PREFIJO_CONSULTA_PERIODO_DEFAULT)


def _periodos(desde: date, hasta: date, periodo: str):
    """(inicio, fin, etiqueta) de los períodos calendario que tocan [desde, hasta]."""
    meses = 3 if periodo == PERIODO_TRIMESTRE else 1
    m0 = ((desde.month - 1) // meses) * meses + 1
    inicio = date(desde.year, m0, 1)
    while inicio <= hasta:
        y, m = inicio.year, inicio.month + meses
        if m > 12:
            y, m = y + 1, m - 12
        sig = date(y, m, 1)
        fin = sig - timedelta(days=1)
        etiqueta = (f'{inicio.year}T{(inicio.month - 1) // 3 + 1}'
                    if periodo == PERIODO_TRIMESTRE else f'{inicio:%Y%m}')
        yield inicio, fin, etiqueta
        inicio = sig


def ventanas_historicas(hoy: date = None) -> list:
    """Los períodos del histórico que tocan los últimos `DIAS_HISTORICO` días,
    del más reciente al más viejo. Cada uno es una consulta registrada con
    fechas FIJAS: `<prefijo>_<etiqueta>` y su SQL (`sql_ventas_periodo`).

    El `hasta` del período en curso es AYER el día que se copia el SQL: lo que
    viene después lo cubre la reciente, que se lee todos los días. Los
    períodos no cambian (salvo un documento anulado o fechado atrás): se leen
    una vez."""
    from app.utils.fecha import dia_operativo
    hoy = hoy or dia_operativo()
    ayer = hoy - timedelta(days=1)
    per = periodo_historico()
    pref = prefijo_consulta_periodo()
    out = []
    for inicio, fin, etiqueta in _periodos(hoy - timedelta(days=DIAS_HISTORICO), ayer, per):
        out.append({'consulta': f'{pref}_{etiqueta}', 'tipo': 'PERIODO',
                    'desde': inicio, 'fin': fin, 'hasta': min(fin, ayer),
                    'etiqueta': etiqueta})
    return list(reversed(out))


def ventana_reciente() -> dict:
    return {'consulta': consulta_ventas_dia(True), 'tipo': 'RECIENTE',
            'dias': DIAS_RECIENTE}


def sql_de_ventana(v: dict) -> str:
    """El SQL listo para pegar de una ventana (la reciente o un período)."""
    if v['tipo'] == 'RECIENTE':
        return sql_ventas_dia(v.get('dias', DIAS_RECIENTE))
    return sql_ventas_periodo(v['desde'], v['hasta'])


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


def _decimal_o_none(v):
    """Como `_decimal`, pero una columna AUSENTE o NULL es `None`: no se sabe,
    no es cero."""
    if v in (None, ''):
        return None
    return _decimal(v)


def leer_fila_venta_dia(row):
    """Una fila cruda → sus campos, o `None` si no se puede leer entera.

    Una fila ilegible NO se salta en silencio: quien lee la cuenta, y una
    lectura con filas ilegibles no está completa. Las columnas de valor y costo
    (`COLUMNAS_VALOR`) son opcionales: sin ellas (una consulta registrada antes
    del 2026-09-27) el valor queda `None`, desconocido."""
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
    fila = {'orden': orden, 'total_filas': total, 'fecha': fecha, 'bodega': bod,
            'referencia': ref, 'vendido': vend, 'devuelto': dev,
            'lineas': _entero(row.get('lineas')),
            'ventana_desde': desde, 'ventana_hasta': hasta}
    for c in COLUMNAS_VALOR:
        fila[c] = (_entero(row.get(c)) if c.startswith('lineas_')
                   else _decimal_o_none(row.get(c)))
    return fila


#: Compatibilidad con el borrador del script de medición.
leer_fila_venta_diaria = leer_fila_venta_dia
COLUMNAS_VENTAS_DIARIAS = COLUMNAS_VENTAS_DIA
CONSULTA_VENTAS_DIARIAS_DEFAULT = CONSULTA_VENTAS_DIA_DEFAULT
SQL_VALIDACION_UN_DIA = SQL_VALIDACION['conceptos_dia']


# ══════════════════════════════════════════════════════════════════════════════
# Qué días quedaron completos — puro, sin base
# ══════════════════════════════════════════════════════════════════════════════

def dias_completos(por_orden: dict, total: int, ultimo_cerrado: date):
    """Del conjunto de filas leídas (por `orden`) de una consulta ordenada por
    fecha DESCENDENTE (la reciente), el tramo de días que se puede afirmar
    COMPLETO. Puro.

    Si llegaron las filas 1…k sin hueco (y la k+1 no), todo día POSTERIOR al de
    la k está entero —incluidos los días sin fila entre dos filas leídas, que
    son ceros verdaderos—; el de la k no (la k+1 podría ser de ese mismo día).
    Con k = total, todo día de la ventana está entero.

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
        # La k+1 no llegó (si hubiera llegado, el prefijo seguiría): puede ser
        # del mismo día que la k, así que ese día no se da por entero.
        desde = por_orden[k]['fecha'] + timedelta(days=1)
    if desde > hasta:
        return None
    return desde, hasta


def avance(por_orden: dict, total: int, inicio_orden: int, v_desde: date, hasta_previo: date):
    """Hasta dónde avanzó la lectura de una ventana ordenada por fecha
    DESCENDENTE que se RETOMA (un período del histórico). Puro.

    Las filas `1…inicio_orden−1` ya se guardaron (cubren desde `hasta_previo +
    1` hasta el final). Si ahora llegaron `inicio_orden…k` sin hueco, todo día
    POSTERIOR al de la k (y hasta `hasta_previo`) está entero; el de la k no
    —la k+1 puede ser del mismo día—. Con k = total, entero hasta el principio
    de la ventana (`v_desde`).

    Returns: `(tramo | None, orden_hasta, cubierto_desde)` — `tramo` son los
    días nuevos enteros; `orden_hasta`, la última fila de un día entero (donde
    retoma la próxima lectura); `cubierto_desde`, el primer día entero.
    """
    k = inicio_orden - 1
    while (k + 1) in por_orden:
        k += 1
    if k >= total:
        tramo = (v_desde, hasta_previo) if v_desde <= hasta_previo else None
        return tramo, total, v_desde
    if k < inicio_orden:
        return None, inicio_orden - 1, hasta_previo + timedelta(days=1)
    frontera = por_orden[k]['fecha']
    orden_hasta = inicio_orden - 1
    for o in range(inicio_orden, k + 1):
        if por_orden[o]['fecha'] > frontera:
            orden_hasta = o
    desde = frontera + timedelta(days=1)
    tramo = (desde, hasta_previo) if desde <= hasta_previo else None
    return tramo, orden_hasta, min(desde, hasta_previo + timedelta(days=1))


class _SinRegistrar(Exception):
    """401: la consulta no está registrada en Siesa (o sin permiso)."""


def _leer_paginas(gw, consulta, pagina_inicio, orden_minimo, deadline, reloj, max_paginas,
                  pausa, al_leer=None, precargada=None):
    """Lee desde `pagina_inicio` hasta completar el total (o el tiempo, o un
    problema). Devuelve un dict con `por_orden` (solo `orden ≥ orden_minimo`),
    `total`, `total_declarado`, `problemas`, `ilegibles`, `paginas`, `motivo`.

    - Una fila que llega dos veces con otro contenido, un `total_filas` que
      cambia a mitad, una ventana que cambia, una fila ilegible: problema.
    - Una fila que falta (paginación inestable) se busca otra vez en su
      página (hasta 3 veces).
    """
    from app.services.connekta_gateway import _exigir_datos
    from app.services.kardex_service import totales_declarados

    st = {'por_orden': {}, 'por_clave': {}, 'problemas': [], 'total': None,
          'total_declarado': None, 'ilegibles': 0, 'paginas': 0, 'motivo': None,
          'ventana': None, 'crudas': None}

    def _pedir(pag):
        # Tres intentos por página: la primera consulta en frío tardó 108 s en
        # producción (2026-09-27) y la siguiente, 2,5 s. Un 401 no se reintenta.
        for intento in range(3):
            try:
                res = gw._get(consulta, {'paginacion': f'numPag={pag}|tamPag={TAM_PAG}'},
                              url=gw.url_get_dinamico, timeout=150)
                break
            except Exception as e:                            # noqa: BLE001
                if '401' in str(e):
                    raise _SinRegistrar(str(e)) from e
                if intento == 2:
                    raise
                time.sleep((10 if '429' in str(e) else 2) * (intento + 1))
        if res is None:
            raise RuntimeError('circuito de Siesa abierto')
        det = res.get('detalle') if isinstance(res, dict) else None
        if not isinstance(det, dict):
            raise RuntimeError(f'respuesta sin detalle: {str(res)[:200]}')
        rows = det.get('Datos') if det.get('Datos') is not None else det.get('Table')
        if rows is None:
            raise RuntimeError('respuesta sin filas (ni Datos ni Table)')
        _exigir_datos(rows, consulta)
        st['crudas'] = (pag, rows, totales_declarados(res)[0])
        return rows, totales_declarados(res)[0]

    def _absorber(rows):
        for r in rows:
            f = leer_fila_venta_dia(r)
            if f is None:
                st['ilegibles'] += 1
                continue
            if st['total'] is None:
                st['total'] = f['total_filas']
            elif f['total_filas'] != st['total']:
                st['problemas'].append(
                    f'total_filas cambió de {st["total"]} a {f["total_filas"]} a mitad de la '
                    'lectura (Siesa recalculó: datos nuevos)')
            if al_leer is not None:
                al_leer(f)
            if st['ventana'] is None:
                st['ventana'] = (f['ventana_desde'], f['ventana_hasta'])
            elif f['ventana_desde'] != st['ventana'][0]:
                st['problemas'].append('la ventana de la consulta cambió a mitad (pasó la '
                                       'medianoche en Siesa)')
            if f['orden'] < orden_minimo:
                continue
            previa = st['por_orden'].get(f['orden'])
            if previa is not None and (previa['fecha'], previa['bodega'],
                                       previa['referencia']) != (f['fecha'], f['bodega'],
                                                                  f['referencia']):
                st['problemas'].append(f'la fila {f["orden"]} llegó dos veces con otro '
                                       'contenido: el orden no es estable')
                continue
            clave = (f['fecha'], f['bodega'], f['referencia'])
            otro = st['por_clave'].get(clave)
            if otro is not None and otro != f['orden']:
                st['problemas'].append(
                    f'la misma fila ({clave[0]}, {clave[1]}, {clave[2]}) llegó con los números '
                    f'{otro} y {f["orden"]}: el orden no es estable')
                continue
            st['por_orden'].setdefault(f['orden'], f)
            st['por_clave'].setdefault(clave, f['orden'])

    pag = pagina_inicio - 1
    seguir = True
    try:
        if precargada is not None and precargada[0] == pagina_inicio:
            # La página ya se leyó (la del ancla): no se pide dos veces.
            pag, rows, declarado = precargada
            if declarado is not None:
                st['total_declarado'] = declarado
            _absorber(rows)
            if (st['problemas']
                    or (st['total'] is not None
                        and orden_minimo - 1 + len(st['por_orden']) >= st['total'])
                    or len(rows) < TAM_PAG):
                seguir = False
        while seguir:
            pag += 1
            if pag - pagina_inicio >= max_paginas:
                st['motivo'] = f'se agotaron {max_paginas} páginas sin llegar al final'
                break
            if reloj() > deadline:
                st['motivo'] = f'se acabó el tiempo en la página {pag}'
                break
            rows, declarado = _pedir(pag)
            st['paginas'] += 1
            if declarado is not None:
                st['total_declarado'] = declarado
            _absorber(rows)
            if st['problemas']:
                break
            if st['total'] is not None and orden_minimo - 1 + len(st['por_orden']) >= st['total']:
                break
            if len(rows) < TAM_PAG:
                break
            if pausa:
                time.sleep(pausa)
        total = st['total']
        if not st['problemas'] and total:
            for _intento in range(3):
                faltan = [o for o in range(orden_minimo, total + 1) if o not in st['por_orden']]
                if not faltan:
                    break
                for p in sorted({(o - 1) // TAM_PAG + 1 for o in faltan[:300]}):
                    if reloj() > deadline:
                        break
                    rows, _d = _pedir(p)
                    st['paginas'] += 1
                    _absorber(rows)
    except _SinRegistrar:
        raise
    except Exception as e:                                    # noqa: BLE001
        st['motivo'] = f'página {pag}: {str(e)[:300]}'
    if (st['total_declarado'] is not None and st['total'] is not None
            and st['total_declarado'] != st['total']):
        st['problemas'].append(f'Siesa declaró {st["total_declarado"]} registros y la '
                               f'consulta dice {st["total"]}')
    if st['ilegibles']:
        st['problemas'].append(
            f'{st["ilegibles"]} fila(s) ilegibles (¿el SQL registrado tiene las columnas '
            f'{", ".join(COLUMNAS_VENTAS_DIA)}?)')
    return st


def _texto_sin_registrar(consulta):
    return (f'{consulta}: 401 — la consulta no está registrada en Siesa o el usuario de '
            'la integración no tiene permiso (Administración → Permisos servicios → '
            'Consultas dinámicas).')


def descargar_ventana(ventana: dict, gateway=None, max_paginas=None, max_minutos=None,
                      pausa_s=None, reloj=None, deadline=None) -> dict:
    """Lee UNA ventana (la reciente o un período) y guarda **los días que
    quedaron completos**. Nunca levanta: el resultado dice qué pasó.

    La reciente se lee siempre desde la página 1 (lo más reciente primero; se
    relee entera cada día para ver las anulaciones). Un período se **retoma**
    donde quedó la lectura anterior (`DemandaVentanaLectura`): antes de seguir
    se relee la última fila guardada y se exige que sea la misma (el período no
    se movió); si no, se relee desde el principio y se dice. Así el histórico
    avanza de una corrida a la siguiente en vez de volver siempre a la página 1
    (P1-2).
    """
    from app.services import registro_sync_service as rs
    from app.services.connekta_gateway import connekta
    from app.utils.fecha import dia_operativo

    gw = gateway or connekta
    consulta = ventana['consulta']
    periodo = ventana.get('tipo') == 'PERIODO'
    max_paginas = max_paginas or _entero_env('DEMANDA_MAX_PAGINAS', 12000, 1, 30000)
    max_minutos = max_minutos or _entero_env('DEMANDA_MAX_MINUTOS', 50, 1, 240)
    pausa = (pausa_s if pausa_s is not None
             else float(os.getenv('DEMANDA_PAUSA_S', '0.3') or 0.3))
    reloj = reloj or time.monotonic
    inicio = reloj()
    deadline = deadline if deadline is not None else inicio + max_minutos * 60.0
    reg_id = rs.abrir('demanda_siesa')
    ultimo_cerrado = dia_operativo() - timedelta(days=1)

    cursor = _cursor(consulta) if periodo else None
    inicio_orden, hasta_previo, reinicio, total_conocido, v_conocida = 1, None, None, None, None
    pagina_ancla = None
    if periodo and cursor is not None and cursor.orden_hasta and not cursor.sin_registrar:
        verif, total_conocido, v_conocida, pagina_ancla = _verificar_ancla(
            gw, consulta, cursor, deadline, reloj)
        if verif is True:
            inicio_orden = cursor.orden_hasta + 1
            hasta_previo = cursor.cubierto_desde - timedelta(days=1)
        elif verif is None:
            resultado = _resultado(consulta, False, 0, None, 0, None, 0, None, 0,
                                   'no se pudo releer la última fila guardada; se reintenta '
                                   'en la próxima corrida', reloj() - inicio, ventana)
            rs.cerrar_error(reg_id, resultado['motivo'], resultado)
            return resultado
        else:
            reinicio = verif
            cursor = _guardar_cursor(ventana, cursor, reiniciar=True, reg_id=reg_id,
                                     motivo=verif)
    pagina_inicio = (inicio_orden - 1) // TAM_PAG + 1

    try:
        st = _leer_paginas(gw, consulta, pagina_inicio, inicio_orden, deadline, reloj,
                           max_paginas, pausa, precargada=pagina_ancla)
    except _SinRegistrar:
        if periodo:
            _guardar_cursor(ventana, cursor, sin_registrar=True,
                            motivo=_texto_sin_registrar(consulta), reg_id=reg_id)
        resultado = _resultado(consulta, False, 0, None, 0, None, 0, None, 0,
                               _texto_sin_registrar(consulta), reloj() - inicio, ventana,
                               sin_registrar=True)
        rs.cerrar_error(reg_id, resultado['motivo'], resultado)
        return resultado

    por_orden, total, problemas = st['por_orden'], st['total'], st['problemas']
    motivo = st['motivo']
    if total is None and total_conocido is not None and not problemas:
        # Retomada en una página sin filas (todo ya estaba leído): el total es
        # el que se leyó al verificar la ancla.
        total, motivo = total_conocido, None
    if total is None and not motivo:
        motivo = ('la consulta no devolvió filas: no se sabe si no hubo ventas o si '
                  'está mal registrada')
    leidas_hasta = inicio_orden - 1 + len(por_orden)
    faltantes = (total - leidas_hasta) if total is not None else None
    completa = (not problemas and not motivo and total is not None and faltantes == 0)

    tramo, orden_hasta, cubierto_desde = None, None, None
    if not problemas and total is not None:
        v_desde, v_hasta = st['ventana'] or v_conocida or (None, None)
        if periodo and v_desde is not None:
            tope = min(v_hasta, ultimo_cerrado)
            tramo, orden_hasta, cubierto_desde = avance(
                por_orden, total, inicio_orden, v_desde,
                hasta_previo if hasta_previo is not None else tope)
        elif not periodo:
            tramo = dias_completos(por_orden, total, ultimo_cerrado)

    guardadas = 0
    if tramo:
        guardadas = _guardar_lectura(por_orden.values(), tramo, reg_id, consulta)
    if periodo and cubierto_desde is not None:
        ancla = por_orden.get(orden_hasta) if orden_hasta and orden_hasta >= inicio_orden else None
        _guardar_cursor(ventana, cursor, total=total, orden_hasta=orden_hasta,
                        cubierto_desde=cubierto_desde, ancla=ancla, v_desde=v_desde,
                        reg_id=reg_id, paginas=st['paginas'], motivo=motivo)
    elif periodo and problemas:
        _guardar_cursor(ventana, cursor, reiniciar=True, reg_id=reg_id,
                        motivo='; '.join(problemas[:3]))

    texto = motivo or ('; '.join(problemas[:5]) if problemas else None)
    if reinicio:
        texto = f'{reinicio} {texto or ""}'.strip()
    resultado = _resultado(consulta, completa, st['paginas'], total, leidas_hasta, faltantes,
                           st['ilegibles'], tramo, guardadas, texto, reloj() - inicio, ventana,
                           retomada_desde=inicio_orden if inicio_orden > 1 else None)
    if completa:
        rs.cerrar_ok(reg_id, resultado)
    else:
        rs.cerrar_error(reg_id, resultado['motivo'] or f'faltan {faltantes} filas', resultado)
    logger.info('[DEMANDA] %s: %s', consulta, resultado)
    return resultado


def _resultado(consulta, completa, paginas, total, leidas, faltantes, ilegibles, tramo,
               guardadas, motivo, segundos, ventana, sin_registrar=False, retomada_desde=None):
    return {
        'consulta': consulta, 'tipo': ventana.get('tipo'), 'completa': completa,
        'paginas': paginas, 'filas_declaradas': total, 'filas_leidas': leidas,
        'faltantes': faltantes, 'ilegibles': ilegibles,
        'dias_guardados': ({'desde': tramo[0].isoformat(), 'hasta': tramo[1].isoformat()}
                           if tramo else None),
        'filas_guardadas': guardadas, 'motivo': motivo,
        'sin_registrar': sin_registrar, 'retomada_desde_fila': retomada_desde,
        'minutos': round(segundos / 60.0, 1),
    }


def _cursor(consulta):
    from app.models.demanda_siesa import DemandaVentanaLectura
    return DemandaVentanaLectura.query.filter_by(consulta=consulta).first()


def _verificar_ancla(gw, consulta, cursor, deadline, reloj):
    """¿La fila donde quedó la lectura anterior sigue siendo la misma?
    `(True, total, ventana, página_leída)` sí; `(motivo, …)` si no —se relee
    el período desde el principio—; `(None, …)` si no se pudo preguntar. La
    página leída vuelve para no pedirla dos veces."""
    if cursor.ancla_fecha is None or cursor.cubierto_desde is None:
        return 'no había ancla guardada: se lee el período desde el principio.', None, None, None
    pag = (cursor.orden_hasta - 1) // TAM_PAG + 1
    vista = {}

    def _anotar(f):
        vista[f['orden']] = f
    try:
        st = _leer_paginas(gw, consulta, pag, cursor.orden_hasta + TAM_PAG * 10 ** 6,
                           deadline, reloj, 1, 0, al_leer=_anotar)
    except _SinRegistrar:
        return ('la consulta dejó de responder (401): se relee desde el principio.', None, None,
                None)
    f = vista.get(cursor.orden_hasta)
    if not vista:
        return None, None, None, None
    if (f is not None and (f['fecha'], f['bodega'], f['referencia'])
            == (cursor.ancla_fecha, cursor.ancla_bodega, cursor.ancla_referencia)
            and (cursor.total_filas is None or st['total'] == cursor.total_filas)):
        return True, st['total'], st['ventana'], st['crudas']
    return ('el período cambió desde la lectura anterior (un documento anulado o '
            'fechado atrás): se relee desde el principio.'), None, None, None


def _guardar_cursor(ventana, cursor, total=None, orden_hasta=None, cubierto_desde=None,
                    ancla=None, v_desde=None, reg_id=None, paginas=0, motivo=None,
                    sin_registrar=False, reiniciar=False):
    """Dónde quedó la lectura de un período. Solo avanza (hacia atrás en el
    tiempo): si esta lectura no llegó más lejos, se conserva lo de antes
    (salvo `reiniciar`)."""
    from app.models.demanda_siesa import DemandaVentanaLectura
    if cursor is None:
        cursor = DemandaVentanaLectura(consulta=ventana['consulta'])
        db.session.add(cursor)
    cursor.desde, cursor.fin = ventana['desde'], ventana['fin']
    cursor.sin_registrar = bool(sin_registrar)
    cursor.motivo = (motivo or None) and str(motivo)[:500]
    cursor.registro_id = reg_id
    cursor.leida_en = datetime.utcnow()
    cursor.paginas = (cursor.paginas or 0) + (paginas or 0)
    if reiniciar:
        cursor.orden_hasta = cursor.cubierto_desde = cursor.total_filas = None
        cursor.ancla_fecha = cursor.ancla_bodega = cursor.ancla_referencia = None
        cursor.completa = False
    elif total is not None:
        cursor.total_filas = total
        if orden_hasta is not None and orden_hasta >= (cursor.orden_hasta or 0):
            cursor.orden_hasta = orden_hasta
            if ancla is not None:
                cursor.ancla_fecha, cursor.ancla_bodega, cursor.ancla_referencia = (
                    ancla['fecha'], ancla['bodega'], ancla['referencia'])
        if cubierto_desde is not None and (cursor.cubierto_desde is None
                                           or cubierto_desde < cursor.cubierto_desde):
            cursor.cubierto_desde = cubierto_desde
        cursor.completa = bool(cursor.cubierto_desde and v_desde
                               and cursor.cubierto_desde <= v_desde)
    db.session.commit()
    return cursor


def descargar_ventas_dia(reciente: bool = False, gateway=None, max_paginas=None,
                         max_minutos=None, pausa_s=None, reloj=None) -> dict:
    """`reciente=True`: la consulta de los últimos días. Si no, el histórico:
    los períodos que todavía tienen días sin cubrir, del más reciente al más
    viejo, retomando cada uno donde quedó, hasta el tiempo (`DEMANDA_MAX_MINUTOS`)."""
    if reciente:
        return descargar_ventana(ventana_reciente(), gateway=gateway, max_paginas=max_paginas,
                                 max_minutos=max_minutos, pausa_s=pausa_s, reloj=reloj)
    return descargar_historico(gateway=gateway, max_paginas=max_paginas,
                               max_minutos=max_minutos, pausa_s=pausa_s, reloj=reloj)


def ventanas_pendientes(hoy: date = None) -> list:
    """Los períodos con algún día SIN cubrir dentro del histórico (hoy −
    `DIAS_HISTORICO` … ayer), del más reciente al más viejo, con su estado.

    Un período ya leído entero (`completa`) no vuelve a la lista aunque tenga
    días sin cubrir DESPUÉS de su `hasta` registrado: esos días los cubre la
    reciente, y si no los cubrió, ninguna consulta los alcanza — la cobertura
    lo muestra como un hueco (`cobertura_siesa`, `dias_antes_del_hueco`)."""
    from app.utils.fecha import dia_operativo
    hoy = hoy or dia_operativo()
    piso = hoy - timedelta(days=DIAS_HISTORICO)
    ayer = hoy - timedelta(days=1)
    cubiertos = cobertura_siesa(con_dias=True)['dias_cubiertos']
    out = []
    for v in ventanas_historicas(hoy):
        d, h = max(v['desde'], piso), min(v['hasta'], ayer)
        faltan = sum(1 for i in range((h - d).days + 1)
                     if d + timedelta(days=i) not in cubiertos) if d <= h else 0
        if not faltan:
            continue
        c = _cursor(v['consulta'])
        if c is not None and c.completa:
            continue
        out.append(dict(v, dias_sin_cubrir=faltan,
                        sin_registrar=bool(c and c.sin_registrar),
                        retoma_en_fila=(c.orden_hasta + 1) if c and c.orden_hasta else None))
    return out


def descargar_historico(gateway=None, max_paginas=None, max_minutos=None, pausa_s=None,
                        reloj=None, hoy: date = None, deadline=None) -> dict:
    """Los períodos pendientes, del más reciente al más viejo, hasta el
    tiempo. Un período sin registrar (401) se declara y se pasa al siguiente."""
    reloj = reloj or time.monotonic
    max_minutos = max_minutos or _entero_env('DEMANDA_MAX_MINUTOS', 50, 1, 240)
    deadline = deadline if deadline is not None else reloj() + max_minutos * 60.0
    leidas, sin_registrar = [], []
    for v in ventanas_pendientes(hoy):
        if reloj() > deadline:
            break
        r = descargar_ventana(v, gateway=gateway, max_paginas=max_paginas, pausa_s=pausa_s,
                              reloj=reloj, deadline=deadline)
        leidas.append(r)
        if r.get('sin_registrar'):
            sin_registrar.append(v['consulta'])
    pendientes = ventanas_pendientes(hoy)
    return {'ventanas_leidas': leidas, 'sin_registrar': sin_registrar,
            'pendientes': [{'consulta': p['consulta'], 'dias_sin_cubrir': p['dias_sin_cubrir'],
                            'retoma_en_fila': p['retoma_en_fila']} for p in pendientes],
            'completo': not pendientes}


def _guardar_lectura(filas, tramo, registro_id, consulta) -> int:
    """Upsert de las filas del tramo completo + los días como cubiertos.

    Una fila que ya estaba y esta lectura (completa para ese día) no trae —una
    factura anulada— queda en cero: no se borra (Regla de la bitácora). El
    valor y el costo se guardan como vinieron: `None` si la consulta
    registrada no los trae (no se sabe), nunca un cero inventado."""
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
        for c in COLUMNAS_VALOR:
            setattr(e, c, f.get(c))
        e.registro_id, e.actualizada_en = registro_id, ahora
    for clave, e in existentes.items():
        if clave not in nuevas and (e.vendido or e.devuelto):
            e.vendido, e.devuelto, e.lineas = 0, 0, 0
            for c in COLUMNAS_VALOR:
                setattr(e, c, 0)
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


def _en_hilo(app, etiqueta, tomar_lock, fn, lanzar=None):
    """Corre `fn` en un hilo con contexto de app, bajo el lock que da
    `tomar_lock()` (un `advisory_lock(LOCK_…)` escrito en cada llamador: el
    registro de `app/utils/lock.py` exige ver el nombre en la llamada)."""
    import threading

    def _run():
        with app.app_context():
            with tomar_lock() as tomado:
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
    from app.utils.lock import LOCK_DEMANDA_SIESA, advisory_lock
    if not ventana_abierta():
        return {'ok': False, 'codigo': 409,
                'error': f'Fuera de la ventana de Siesa ({texto_ventana()}).'}
    _en_hilo(app, 'demanda_siesa',
             lambda: advisory_lock(LOCK_DEMANDA_SIESA, 'demanda_siesa'),
             lambda: descargar_ventas_dia(reciente=reciente), lanzar)
    return {'ok': True, 'codigo': 202,
            'mensaje': ('Lectura de la venta diaria de Siesa iniciada en segundo plano. '
                        'El resultado queda en el estado de la demanda.')}


def encendido() -> bool:
    return os.getenv('DEMANDA_SIESA', '').strip().lower() == 'true'


def ciclo() -> dict:
    """Lo que hace el cron: primero la reciente (los últimos días, lo que el
    punto de pedido necesita primero); con el tiempo que queda
    (`DEMANDA_MAX_MINUTOS`), el histórico — los períodos con días sin cubrir,
    cada uno retomado donde quedó. Cuando el histórico está completo, solo la
    reciente. Sin `DEMANDA_SIESA` no lee nada."""
    if not encendido():
        return {'omitido': 'DEMANDA_SIESA apagado'}
    reloj = time.monotonic
    deadline = reloj() + _entero_env('DEMANDA_MAX_MINUTOS', 50, 1, 240) * 60.0
    reciente = descargar_ventana(ventana_reciente(), reloj=reloj, deadline=deadline)
    historico = (descargar_historico(reloj=reloj, deadline=deadline)
                 if reloj() < deadline else {'omitido': 'sin tiempo después de la reciente'})
    return {'reciente': reciente, 'historico': historico}


def init_scheduler(app):
    """05:40 Bogotá, todos los días. Nace apagado (`DEMANDA_SIESA`)."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[DEMANDA_SIESA] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import LOCK_DEMANDA_SIESA, advisory_lock
            with advisory_lock(LOCK_DEMANDA_SIESA, 'demanda_siesa') as tomado:
                if not tomado:
                    logger.info('[DEMANDA_SIESA] otro worker ya está leyendo')
                    return
                try:
                    logger.info('[DEMANDA_SIESA] %s', ciclo())
                except Exception as e:                    # noqa: BLE001
                    db.session.rollback()
                    logger.error('[DEMANDA_SIESA] falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido
    from app.services.ventana_siesa import solo_en_ventana_siesa
    scheduler.add_job(func=con_latido('demanda_siesa', solo_en_ventana_siesa(_job)),
                      trigger=CronTrigger(hour=5, minute=40, timezone='America/Bogota'),
                      id='demanda_siesa', replace_existing=True,
                      max_instances=1, misfire_grace_time=1800)
    scheduler.start()
    logger.info('[DEMANDA_SIESA] Scheduler (encendido=%s)', encendido())
    return scheduler


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
    from app.utils.lock import LOCK_DEMANDA_PEDIDOS, advisory_lock
    if not ventana_abierta():
        return {'ok': False, 'codigo': 409,
                'error': f'Fuera de la ventana de Siesa ({texto_ventana()}).'}
    _en_hilo(app, 'demanda_pedidos',
             lambda: advisory_lock(LOCK_DEMANDA_PEDIDOS, 'demanda_pedidos'),
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


def cobertura_siesa(con_dias: bool = False) -> dict:
    """El tramo contiguo que las lecturas completas de Siesa cubrieron. Con
    `con_dias`, además el conjunto de TODOS los días cubiertos (`dias`): lo usan
    los períodos pendientes y el valor realizado — esta es la única que lee la
    cobertura."""
    from sqlalchemy import func
    from app.models.demanda_siesa import DemandaDiaCubierto
    fechas = [r[0] for r in db.session.query(DemandaDiaCubierto.fecha).all()]
    t = _tramo_final(fechas)
    t['leido_en'] = db.session.query(func.max(DemandaDiaCubierto.leido_en)).scalar()
    if con_dias:
        t['dias_cubiertos'] = set(fechas)
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
    try:
        pend = ventanas_pendientes()
    except Exception as e:                                    # noqa: BLE001
        db.session.rollback()
        logger.warning('[DEMANDA] no se pudieron leer los períodos pendientes: %s', e)
        pend = []
    sin_reg = [p['consulta'] for p in pend if p['sin_registrar']]
    if not siesa['usable']:
        return ('Registrar en Siesa la consulta de venta diaria '
                f'({consulta_ventas_dia(True)}) y las del histórico, una por '
                f'{periodo_historico().lower()} ({prefijo_consulta_periodo()}_…; el SQL de '
                'cada una lo imprime `scripts/qa_demanda_fuentes_real.py --sql`), y leerlas '
                'en 🧾 Fuentes → Demanda. Mientras tanto, fotografiar las facturas desde '
                'pedido de los últimos 90 días da una demanda parcial.'
                + (f' Sin registrar todavía: {", ".join(sin_reg)}.' if sin_reg else ''))
    if pend:
        return (f'Faltan {len(pend)} período(s) del histórico por leer'
                + (f' ({len(sin_reg)} sin registrar en Siesa: {", ".join(sin_reg)})'
                   if sin_reg else '')
                + ': se leen solos cada madrugada, retomando donde quedaron, o con '
                  '«Leer el histórico» en 🧾 Fuentes → Demanda.')
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


# ══════════════════════════════════════════════════════════════════════════════
# Valor y costo de lo vendido — para la venta perdida en pesos y el capital
# ══════════════════════════════════════════════════════════════════════════════

def valor_realizado(refs=None, desde: date = None, hasta: date = None, bodegas=None) -> dict:
    """Precio y costo REALIZADOS por SKU sobre los días que una lectura
    completa cubrió: `{ref: {'unidades', 'valor', 'costo', 'precio_unitario',
    'costo_unitario', 'dias', 'valor_incompleto', 'costo_incompleto'}}`.

    Neto de devoluciones (unidades, valor y costo). Si en el tramo hay una
    fila sin valor (consulta registrada sin esas columnas, o líneas sin valor
    en Siesa: `lineas_sin_valor`), el precio de ese SKU es `None` y se declara
    (`valor_incompleto`): un precio calculado sobre parte de las líneas no es el
    precio. Lo mismo el costo. Sin unidades netas > 0, sin precio. Solo lee;
    nadie más lee el valor (`tests/test_demanda_fuentes.py`)."""
    from app.models.demanda_siesa import DemandaDiaSiesa
    cub = cobertura_siesa(con_dias=True)
    if not cub.get('desde'):
        return {}
    d = max(desde or cub['desde'], cub['desde'])
    h = min(hasta or cub['hasta'], cub['hasta'])
    if d > h:
        return {}
    dias_cubiertos = cub['dias_cubiertos']
    q = DemandaDiaSiesa.query.filter(DemandaDiaSiesa.fecha >= d, DemandaDiaSiesa.fecha <= h)
    if refs is not None:
        q = q.filter(DemandaDiaSiesa.referencia.in_(list(refs)))
    if bodegas is not None:
        q = q.filter(DemandaDiaSiesa.bodega.in_(list(bodegas)))
    out = {}
    for f in q.all():
        if f.fecha not in dias_cubiertos:
            continue
        a = out.setdefault(f.referencia, {'unidades': Decimal(0), 'valor': Decimal(0),
                                          'costo': Decimal(0), 'dias': set(),
                                          'valor_incompleto': False,
                                          'costo_incompleto': False})
        a['unidades'] += Decimal(f.vendido or 0) - Decimal(f.devuelto or 0)
        a['dias'].add(f.fecha)
        if f.valor_vendido is None or f.valor_devuelto is None or (f.lineas_sin_valor or 0):
            a['valor_incompleto'] = True
        else:
            a['valor'] += Decimal(f.valor_vendido) - Decimal(f.valor_devuelto)
        if f.costo_vendido is None or f.costo_devuelto is None or (f.lineas_sin_costo or 0):
            a['costo_incompleto'] = True
        else:
            a['costo'] += Decimal(f.costo_vendido) - Decimal(f.costo_devuelto)
    for a in out.values():
        u = a['unidades']
        a['precio_unitario'] = (None if a['valor_incompleto'] or u <= 0
                                else (a['valor'] / u).quantize(Decimal('0.01')))
        a['costo_unitario'] = (None if a['costo_incompleto'] or u <= 0
                               else (a['costo'] / u).quantize(Decimal('0.01')))
        if a['valor_incompleto']:
            a['valor'] = None
        if a['costo_incompleto']:
            a['costo'] = None
        a['dias'] = len(a['dias'])
    return out
