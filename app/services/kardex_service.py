"""
KardexService — Reconstrucción de stock diario desde el kardex transaccional de Siesa.

Consume la consulta dinámica papeleriamedellin_API_custom_KardexWMS que expone:
  T470 (movimientos) + T350 (cabecera) + T120 (maestro items)

Campos: f350_fecha, f350_id_tipo_docto, f350_ind_estado,
        f470_id_bodega, f470_id_item, f470_cant_base, f470_costo_prom_uni,
        f470_id_concepto, f470_ind_naturaleza, f120_referencia

Reglas de Connekta:
  - Endpoint dinámico: /api/connekta/v3/ejecutarconsulta
  - Response: detalle.Datos (NO detalle.Table)
  - tamPag máximo 100 (>=500 genera NULLs fantasma)
  - Fechas YYYYMMDD sin guiones
  - Strings en filtros con doble comilla simple ('')
  - f470_ind_naturaleza: 1=Entrada(suma), 2=Salida(resta)
  - f470_cant_base siempre positivo, naturaleza define signo
"""
import logging
import math
import os
from collections import defaultdict
from datetime import datetime, timedelta, date
from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)

NOMBRE_CONSULTA = os.getenv(
    'KARDEX_CONSULTA_NOMBRE',
    'papeleriamedellin_papeleriamedellin_API_custom_KardexWMS'
)

# ══════════════════════════════════════════════════════════════════════════════
# TABLA DE DEFINICIÓN DE DEMANDA POR CONCEPTO
# Firmada: cada concepto Siesa está clasificado explícitamente.
# Esta tabla es la fuente de verdad — no se adivina.
# ══════════════════════════════════════════════════════════════════════════════
CONCEPTO_DEFINICION = {
    # Concepto → (cuenta_como_demanda, signo, descripción)
    501: (True,  -1, 'Ventas POS y remisiones — DEMANDA REAL'),
    502: (True,  +1, 'Devoluciones de venta — RESTAN demanda (venta 100, dev 30 = demanda 70)'),
    601: (False,  0, 'Entradas por compra — logística, NO demanda'),
    602: (False,  0, 'Salidas directas — NO demanda (mermas, bajas)'),
    603: (False,  0, 'Ajustes sobrantes/faltantes — NO demanda (correcciones)'),
    607: (False,  0, 'Transferencias entre bodegas — logística interna, NUNCA demanda'),
    699: (False,  0, 'Saldos iniciales — NO demanda'),
}

# Conceptos que SÍ cuentan como demanda (positiva o negativa)
CONCEPTOS_DEMANDA = {k for k, v in CONCEPTO_DEFINICION.items() if v[0]}
# Solo salidas de venta (501) — la demanda bruta
CONCEPTOS_VENTA = {501}
# Devoluciones (502) — restan demanda
CONCEPTOS_DEVOLUCION = {502}

# ══════════════════════════════════════════════════════════════════════════════
# CONCEPTOS DE COMPRA — los únicos que dicen QUÉ COSTÓ COMPRAR el SKU.
#
# Vive aquí, al lado de CONCEPTO_DEFINICION, y no en costo_service: el mismo
# concepto clasificado en dos archivos diverge, y esa vez nadie está comparando
# (Regla 0, corolario «una política, una función»).
#
# QUÉ COSTABA NO TENERLO: `costo_service._costos_kardex` ponderaba
# `costo_promedio * cantidad` sobre TODAS las filas del SKU. Entraban los saldos
# iniciales (699, costo viejo con cantidades enormes), los ajustes (603), las
# ventas (501 — la mayoría de las filas del kardex) y los traslados (607, que
# aportan DOS filas por las MISMAS unidades físicas: salida en origen y entrada
# en destino, o sea doble peso por mercancía que nunca se compró dos veces).
# En el caso construido de `tests/test_costo_compra_y_ancla.py` el promedio salía
# 659,09 en vez de 1.000 — un 34,1% por debajo. Y `KARDEX_PROMEDIO` es el nivel 3
# de la jerarquía de costo: subestimar el costo infla Cu y desinfla Co, y las dos
# empujan el ratio crítico del newsvendor hacia COMPRAR MÁS, que es el lado
# irreversible.
#
# FUNDAMENTO DE LA ELECCIÓN — la tabla de arriba, leída literal: 601 es el único
# concepto cuya descripción declara una entrada por compra. De los demás, cada
# descripción dice explícitamente que es otra cosa (venta, devolución de venta,
# salida directa, ajuste, transferencia «logística interna, NUNCA demanda»,
# saldo inicial). No se inventa ningún concepto que la tabla no clasifique: uno
# que no esté en CONCEPTO_DEFINICION es además uno que la compuerta de conceptos
# desconocidos (`reconciliar_kardex`) no vigila.
CONCEPTOS_COMPRA = {601}

# f470_ind_naturaleza: 1=Entrada (suma al stock), 2=Salida (resta).
# El costo ponderado de compra suma ENTRADAS: una fila de compra en salida es
# una devolución al proveedor y no describe lo que costó reponer.
NATURALEZA_ENTRADA = 1
NATURALEZA_SALIDA = 2


class KardexMovimiento(db.Model):
    """Log crudo de movimientos de inventario descargados de Siesa."""
    __tablename__ = 'kardex_movimientos'

    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, index=True)
    tipo_docto = db.Column(db.String(10))
    bodega = db.Column(db.String(10), nullable=False, index=True)
    referencia = db.Column(db.String(30), nullable=False, index=True)
    concepto = db.Column(db.Integer, nullable=False)
    naturaleza = db.Column(db.Integer, nullable=False)  # 1=entrada, 2=salida
    cantidad = db.Column(db.Numeric(14, 4), nullable=False)
    costo_promedio = db.Column(db.Numeric(14, 4), default=0)
    descargado_en = db.Column(db.DateTime, default=datetime.utcnow)

    # Clave natural del documento, si Siesa la envía. Es lo que identifica un
    # movimiento unívocamente: CO + tipo + consecutivo + línea.
    consec_docto = db.Column(db.String(20))
    nro_registro = db.Column(db.Integer)

    # Identidad de origen — SHA-256 sobre la tupla que identifica el movimiento.
    # LA INGESTA ERA UN INSERT PLANO CON ÍNDICE NO ÚNICO: pulsar "Descargar" dos
    # veces duplicaba el kardex entero, y reanudar con orden inestable duplicaba
    # el solape. La duplicación es PEOR que la omisión porque el perfil mensual
    # no la delata — un mes con 8% de filas de más se ve plausible — y
    # movimientos duplicados inflan la demanda, que infla el ROP, que infla el
    # contenedor. El bug de 25x por otra puerta.
    hash_origen = db.Column(db.String(64))

    __table_args__ = (
        db.Index('ix_kardex_ref_bod_fecha', 'referencia', 'bodega', 'fecha'),
        db.Index('ix_kardex_hash_origen', 'hash_origen', unique=True),
    )


class StockDiario(db.Model):
    """Stock reconstruido por día (resultado de aritmética hacia atrás)."""
    __tablename__ = 'stock_diario'

    id = db.Column(db.Integer, primary_key=True)
    referencia = db.Column(db.String(30), nullable=False)
    bodega = db.Column(db.String(10), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    stock_cierre = db.Column(db.Numeric(14, 4), default=0)
    tuvo_stock = db.Column(db.Boolean, default=False)  # True si stock > 0

    __table_args__ = (
        db.Index('ix_stock_diario_ref_bod', 'referencia', 'bodega', 'fecha', unique=True),
    )


# Muestra mínima del tamiz MASE. NO es 10: con n=10 una moneda al aire supera
# el umbral de 60% en el 37.7% de los intentos por azar binomial puro — el
# filtro no filtra, decora. Con n=100 baja a 2.8%. Se evalúan TODOS los SKUs
# con historia suficiente, que serán cientos.
TSB_N_MINIMO = int(os.getenv('TSB_N_MINIMO', '100'))


def _wilson(exitos, n, z=1.96):
    """Intervalo de Wilson al 95% para una proporción.

    Se usa en vez del intervalo normal porque no se rompe con n pequeño ni con
    proporciones cerca de 0 o 1. Un porcentaje sin intervalo es indistinguible
    del azar, y esa indistinguibilidad es justo lo que convierte una compuerta
    en decoración.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = exitos / n
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    margen = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d
    return (max(0.0, centro - margen), min(1.0, centro + margen))


def dias_expuestos(dias_con_stock, dias_calendario):
    """
    POLÍTICA ÚNICA de denominador cuando falta StockDiario.

    Existe en un solo lugar a propósito. El mismo concepto implementado dos
    veces divergió en tres horas: S-B caía a días calendario (conservador) y la
    descensura caía a días-con-venta (25x de sobreestimación en SKUs grumosos).
    Si esto vuelve a parchearse en dos sitios, la tercera implementación
    divergirá otra vez y esa vez nadie estará comparando.

    LA REGLA — ante dato ausente, fallar hacia el lado conservador y declararlo.

    El motivo NO es que el faltante cueste menos que el sobrante: para la
    canasta constitucional el agotado es carísimo, Florencia lo probó. El motivo
    es la REVERSIBILIDAD. Un sub-pedido declarado es una decisión que un humano
    corrige mañana. Un contenedor embarcado es irreversible 120 días y ya se
    llevó la caja. No "corregir" este sesgo por parecer timorato.

    NUNCA usar días-con-venta como denominador: eso da demanda por día-con-venta,
    no por día. Un SKU que vende 40 unidades cada 25 días saldría a 40/día en
    vez de 1.6.

    Returns: (n_dias, censurado)
    """
    n = int(dias_con_stock or 0)
    if n <= 0:
        return int(dias_calendario), True
    return min(n, int(dias_calendario)), False


def hash_movimiento(fecha, tipo_docto, bodega, referencia, concepto,
                    naturaleza, cantidad, consec_docto=None, nro_registro=None):
    """Identidad de un movimiento del kardex.

    Se incluye la clave natural del documento cuando Siesa la envía
    (consecutivo + línea): sin ella, dos movimientos legítimamente idénticos
    —mismo ítem, misma cantidad, mismo día, mismo concepto— colapsarían en uno.
    Con ella, la identidad es exacta.

    Sin clave natural el hash sigue siendo la mejor defensa disponible contra
    la duplicación, y la limitación queda declarada en vez de supuesta.
    """
    import hashlib
    partes = [
        fecha.isoformat() if fecha else '',
        (tipo_docto or '').strip(),
        (bodega or '').strip(),
        (referencia or '').strip(),
        str(concepto or ''),
        str(naturaleza or ''),
        f'{float(cantidad or 0):.4f}',
        (consec_docto or '').strip(),
        str(nro_registro if nro_registro is not None else ''),
    ]
    return hashlib.sha256('|'.join(partes).encode()).hexdigest()


def _parsear_fila(row):
    """Una fila cruda de la consulta → los campos del movimiento, o `None`.

    UNA SOLA LECTURA DE LA FILA, para la descarga y para el diagnóstico de
    orden. Antes cada uno armaba la identidad a su manera: la descarga con
    `f470_nro_registro`, el diagnóstico con `LineaRegistro` — y `LineaRegistro`
    NO identifica una fila. Medido en vivo contra Siesa QA el 2026-09-24 sobre
    el mismo endpoint dinámico (`papeleriamedellin_WMS_Stock_Bodega_v2`): es el
    número de posición del resultado (la página 50 trae exactamente 4901–5000),
    y dos pedidos de la misma página con 5 s de diferencia le asignaron esos
    mismos números a filas distintas. Meterlo en la identidad hacía que una
    misma fila pareciera otra.

    Devuelve `None` si falta referencia, bodega o una fecha legible: esa fila no
    puede ser un movimiento, y quien llama la cuenta como error.
    """
    if not isinstance(row, dict):
        return None
    ref = (row.get('f120_referencia') or '').strip()
    bodega = (row.get('f150_id') or row.get('f470_id_bodega') or '').strip()
    if not ref or not bodega:
        return None

    # Fecha: puede venir como 'f450_id_fecha ' (con espacio) o ISO format
    fecha_str = str(
        row.get('f450_id_fecha ') or row.get('f450_id_fecha') or
        row.get('f350_fecha') or ''
    ).strip()
    try:
        # Soporta YYYYMMDD y YYYY-MM-DDTHH:MM:SS
        fecha = (datetime.strptime(fecha_str[:10], '%Y-%m-%d').date()
                 if '-' in fecha_str
                 else datetime.strptime(fecha_str[:8], '%Y%m%d').date())
    except (ValueError, TypeError):
        return None

    # Naturaleza: Connekta devuelve 'Entrada'/'Salida' (string), no 1/2
    nat_raw = row.get('f470_ind_naturaleza', '')
    if isinstance(nat_raw, str):
        naturaleza = 1 if 'ntrada' in nat_raw else 2  # Entrada=1, Salida=2
    else:
        naturaleza = int(nat_raw) if nat_raw else 0

    # Clave natural del documento, si viene. Los nombres varían entre
    # consultas, así que se prueban los plausibles.
    consec = str(
        row.get('f350_consec_docto') or row.get('f470_consec_docto') or
        row.get('f350_consec') or ''
    ).strip()
    try:
        nro_reg = int(row.get('f470_nro_registro') or
                      row.get('f470_nro_reg') or 0) or None
    except (TypeError, ValueError):
        nro_reg = None

    tipo_docto = (row.get('f350_id_tipo_docto') or '').strip()
    try:
        concepto = int(row.get('f470_id_concepto', 0) or 0)
        cantidad = abs(float(row.get('f470_cant_base', 0) or 0))
        costo = float(row.get('f470_costo_prom_uni', 0) or 0)
    except (TypeError, ValueError):
        return None

    return {
        'fecha': fecha, 'tipo_docto': tipo_docto, 'bodega': bodega,
        'referencia': ref, 'concepto': concepto, 'naturaleza': naturaleza,
        'cantidad': cantidad, 'costo_promedio': costo,
        'consec_docto': consec or None, 'nro_registro': nro_reg,
        'hash': hash_movimiento(fecha, tipo_docto, bodega, ref, concepto,
                                naturaleza, cantidad, consec, nro_reg),
    }


def totales_declarados(respuesta):
    """(total_registros, total_paginas) que la consulta dinámica declara.

    El sobre REAL, medido en vivo contra Siesa QA el 2026-09-24 en el endpoint
    dinámico (`ejecutarconsulta`) — el mismo que usa el kardex:

        detalle = {'tamaño_página': 5, 'página_actual': 1,
                   'total_páginas': 7121, 'total_registros': 35604,
                   'Datos': [...]}

    Con tilde y eñe. La descarga buscaba `totalRegistros`, `TotalRegistros`,
    `total`, `Total`, `totalFilas`, `cantidadRegistros` — seis nombres
    adivinados y ninguno era el real, así que `total_declarado_por_siesa` salía
    `None` en toda descarga y la prueba de completitud POR CONTEO, la que el
    propio docstring llamaba «superior al perfil mensual», nunca corrió. El test
    que la cuidaba solo pedía que la clave existiera.

    Devuelve `None` en lo que no venga: no saber no es cero.
    """
    det = (respuesta or {}).get('detalle')
    if not isinstance(det, dict):
        return None, None

    def _entero(*claves):
        for k in claves:
            v = det.get(k)
            if v in (None, ''):
                v = (respuesta or {}).get(k)
            if v in (None, ''):
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return None

    return (_entero('total_registros', 'totalRegistros'),
            _entero('total_páginas', 'total_paginas', 'totalPaginas'))


# ══════════════════════════════════════════════════════════════════════════════
# ¿ESTÁ CORRIENDO LA DESCARGA, O MURIÓ?
# ══════════════════════════════════════════════════════════════════════════════

#: Ninguna corrida de descarga dura más que esto. Es un techo y no un default:
#: `tope_minutos` recorta lo que pida la llamada. De que exista un techo
#: depende poder afirmar que un registro abierto hace más tiempo es un proceso
#: muerto y no una descarga lenta.
KARDEX_TOPE_MINUTOS = 120
#: Holgura sobre el techo: la última página (timeout 60 s) y su commit.
MARGEN_INTERRUMPIDA_MIN = 10


def tope_minutos(max_minutos=None) -> int:
    """Minutos que puede durar ESTA corrida: lo pedido, o `KARDEX_MAX_MINUTOS`
    (25), recortado a [1, `KARDEX_TOPE_MINUTOS`]."""
    try:
        v = int(max_minutos or os.environ.get('KARDEX_MAX_MINUTOS', '25'))
    except (TypeError, ValueError):
        v = 25
    return max(1, min(v, KARDEX_TOPE_MINUTOS))


def estado_descarga(ahora=None) -> dict:
    """La última descarga según `registros_sync`, con el caso que faltaba.

    UN REGISTRO ABIERTO NO ES UNA DESCARGA EN CURSO. La descarga corre en un
    hilo del proceso web; un deploy o un reinicio de Railway lo mata sin cerrar
    su fila, y `ok`/`fin` quedan en NULL para siempre. Las tres rutas leían
    «fin es NULL» como «en curso», así que desde ese momento:

      · `/descargar` contestaba «Descarga ya en curso — revisar logs» y no
        arrancaba NUNCA más;
      · `/reconstruir` contestaba 409 «hay una descarga en curso» — también
        para siempre;
      · la pantalla mostraba «● En curso» indefinidamente.

    Y la descarga completa son horas de corridas de 25 minutos: que un deploy
    caiga en medio de una no es un caso raro, es el esperable. Con el techo de
    `KARDEX_TOPE_MINUTOS` un registro abierto hace más de techo + margen no
    puede ser una corrida viva: es una INTERRUMPIDA, y se declara como tal —
    ni en curso ni exitosa.

    Returns: {hay_registro, en_curso, interrumpida, registro, resultado,
              error_lectura}
    """
    from app.services import registro_sync_service as _reg
    ult = _reg.ultimo('kardex')
    salida = {'hay_registro': False, 'en_curso': False, 'interrumpida': False,
              'registro': None, 'resultado': None, 'error_lectura': None}
    if ult is None:
        return salida
    if '_error_lectura' in ult:
        salida['error_lectura'] = ult['_error_lectura']
        return salida

    salida['hay_registro'] = True
    salida['registro'] = {k: ult.get(k) for k in ('id', 'inicio', 'fin', 'ok')}
    if ult.get('fin') is None:
        ahora = ahora or datetime.utcnow()
        try:
            inicio = datetime.fromisoformat(ult['inicio'])
        except (TypeError, ValueError):
            inicio = None
        limite = timedelta(minutes=KARDEX_TOPE_MINUTOS + MARGEN_INTERRUMPIDA_MIN)
        # Sin inicio legible no se puede afirmar que siga viva: Regla 0.
        if inicio is None or ahora - inicio > limite:
            salida['interrumpida'] = True
            salida['resultado'] = {
                'ok': False,
                'estado': 'INTERRUMPIDA',
                'detalle_estado': (
                    'La descarga que empezó '
                    f'{ult.get("inicio") or "(sin hora)"} (UTC) nunca cerró: el '
                    'proceso murió (reinicio o deploy). No se sabe hasta qué '
                    'página llegó. Descargar de nuevo desde la página 1: lo ya '
                    'guardado no se duplica.'),
                'reanudar_desde': 1,
            }
        else:
            salida['en_curso'] = True
        return salida

    if ult.get('ok') is False:
        salida['resultado'] = {'ok': False, 'error': ult.get('error')}
    else:
        salida['resultado'] = ult.get('resultado')
    return salida


# ══════════════════════════════════════════════════════════════════════════════
# ¿ESTÁ AL DÍA EL KARDEX? — un veredicto, calculado acá y en ningún otro sitio
# ══════════════════════════════════════════════════════════════════════════════

#: Días que puede tener el último movimiento guardado antes de declarar el
#: kardex desactualizado. Los modelos promedian doce meses, pero la semana que
#: falta es la más reciente — la que el ROP y la reposición leen primero.
def dias_frescura() -> int:
    try:
        return max(1, int(os.environ.get('KARDEX_DIAS_FRESCURA', '7')))
    except (TypeError, ValueError):
        return 7


def salud_kardex() -> dict:
    """¿Puedo creerle al kardex HOY? — lo que la pantalla muestra, servido.

    Antes, la pestaña Datos mostraba solo el resultado de la última descarga
    (o «Sin descargas en esta sesión del servidor», que dejó de ser cierto
    cuando el estado pasó a `registros_sync`), y la pantalla Modelos decía
    «✓ Kardex completo» con que no hubiera conceptos sin clasificar — lo que
    un kardex VACÍO cumple por construcción. Nadie podía saber, mirando, si el
    kardex tenía datos, de cuándo, ni si la serie de stock diario estaba
    reconstruida sobre la última descarga.

    El veredicto se calcula ACÁ y las pantallas lo pintan: si cada una armara
    el suyo en JS, divergirían (Regla 0, «una política, una función»).

    `confiable` es True solo si NO hay ningún problema: un estado que no se
    puede afirmar bueno no se muestra verde.
    """
    from sqlalchemy import func
    from app.services.connekta_gateway import connekta

    hoy = _dia_operativo()
    umbral = dias_frescura()

    total, f_min, f_max = db.session.query(
        func.count(KardexMovimiento.id),
        func.min(KardexMovimiento.fecha),
        func.max(KardexMovimiento.fecha),
    ).one()
    total = int(total or 0)
    bodegas = sorted(r[0] for r in
                     db.session.query(KardexMovimiento.bodega).distinct().all())
    ultimo_guardado = db.session.query(func.max(KardexMovimiento.descargado_en)).scalar()
    sd_total, sd_max = db.session.query(
        func.count(StockDiario.id), func.max(StockDiario.fecha)).one()
    sd_total = int(sd_total or 0)
    conceptos = {r[0] for r in
                 db.session.query(KardexMovimiento.concepto).distinct().all()}
    sin_clasificar = sorted(conceptos - set(CONCEPTO_DEFINICION.keys()))

    desc = estado_descarga()
    res = desc['resultado'] or {}
    dias_desde = (hoy - f_max).days if f_max else None

    problemas = []

    def _p(codigo, titulo, que_hacer):
        problemas.append({'codigo': codigo, 'titulo': titulo, 'que_hacer': que_hacer})

    if desc['error_lectura']:
        _p('SIN_LECTURA_DEL_REGISTRO',
           'No se pudo leer el registro de descargas.',
           'Revisar la base (registros_sync) antes de confiar en nada de acá.')
    if total == 0:
        _p('SIN_DATOS', 'El kardex está vacío: los modelos no tienen de dónde leer.',
           'Descargar el kardex de Siesa (fuera de horario).')
    if desc['en_curso']:
        _p('DESCARGA_EN_CURSO', 'Hay una descarga corriendo: los datos están a medias.',
           'Esperar a que termine y volver a mirar.')
    elif desc['interrumpida']:
        _p('DESCARGA_INTERRUMPIDA',
           'La última descarga murió sin terminar (reinicio o deploy).',
           'Descargar de nuevo: lo ya guardado no se duplica.')
    elif desc['hay_registro'] and res.get('ok') is not True:
        _p('ULTIMA_DESCARGA_INCOMPLETA',
           'La última descarga no quedó completa ('
           + str(res.get('estado') or ('error: ' + str(res.get('error'))[:120]
                                       if res.get('error') else 'sin estado'))
           + ').',
           'Reanudar o repetir la descarga; no reconstruir sobre datos truncados.')
    elif not desc['hay_registro'] and total > 0 and not desc['error_lectura']:
        _p('SIN_DESCARGA_REGISTRADA',
           f'Hay {total} movimientos pero ninguna descarga registrada: no se '
           'puede afirmar que estén completos.',
           'Descargar de nuevo para dejar constancia de cómo terminó.')
    if sin_clasificar:
        _p('CONCEPTOS_SIN_CLASIFICAR',
           f'{len(sin_clasificar)} concepto(s) sin clasificar: {sin_clasificar}.',
           'Agregarlos a CONCEPTO_DEFINICION antes de calcular.')
    if total > 0 and dias_desde is not None and dias_desde > umbral:
        _p('DESACTUALIZADO',
           f'El último movimiento guardado es del {f_max.isoformat()}: '
           f'{dias_desde} días atrás (tolerancia {umbral}).',
           'Descargar el kardex: nada lo actualiza solo.')
    if total > 0 and (sd_max is None or sd_max < f_max):
        _p('STOCK_DIARIO_ATRASADO',
           'La serie de stock diario no cubre el último movimiento guardado'
           + (f' (llega al {sd_max.isoformat()})' if sd_max else ' (está vacía)')
           + ': la demanda corregida por agotados usa días que faltan.',
           'Reconstruir stock diario después de una descarga completa.')

    return {
        'veredicto': problemas[0]['codigo'] if problemas else 'AL_DIA',
        'confiable': not problemas,
        'problemas': problemas,
        'hoy': hoy.isoformat(),
        'umbral_dias': umbral,
        'movimientos': {
            'total': total,
            'primera_fecha': f_min.isoformat() if f_min else None,
            'ultima_fecha': f_max.isoformat() if f_max else None,
            'dias_desde_ultimo': dias_desde,
            'bodegas': bodegas,
            'ultimo_guardado_utc': ultimo_guardado.isoformat() if ultimo_guardado else None,
        },
        'stock_diario': {
            'dias': sd_total,
            'ultima_fecha': sd_max.isoformat() if sd_max else None,
        },
        'conceptos_sin_clasificar': sin_clasificar,
        'ultima_descarga': {
            'hay_registro': desc['hay_registro'],
            'en_curso': desc['en_curso'],
            'interrumpida': desc['interrumpida'],
            'inicio_utc': (desc['registro'] or {}).get('inicio'),
            'fin_utc': (desc['registro'] or {}).get('fin'),
            'estado': res.get('estado') or ('ERROR' if res.get('error') else None),
            'ok': res.get('ok') if desc['hay_registro'] else None,
            'error': res.get('error'),
            'total_descargados': res.get('total_descargados'),
            'reanudar_desde': res.get('reanudar_desde'),
        },
        # Ningún cron descarga el kardex (ver el trinquete en
        # tests/test_kardex_salud.py). Si alguien lo agenda, esto tiene que
        # cambiar con él.
        'actualizacion_automatica': False,
        'nota_actualizacion': (
            'Nada actualiza el kardex solo: se descarga cuando alguien pulsa '
            '«Descargar». Por eso la fecha del último movimiento es el dato a mirar.'),
        'descargaria_de': {
            'host': connekta.host_siesa,
            'parece_qa': bool(connekta.apunta_a_pruebas),
        },
    }


def perfil_mensual_kardex():
    """Filas por mes en el kardex almacenado.

    RANGO PEDIDO vs TRAÍDO ES NECESARIO Y NO SUFICIENTE: compara la primera y
    la última fecha, y no dice nada del medio. Un fallo de reanudación produce
    un kardex que abarca todo el rango con un agujero en marzo, y esa
    comparación diría COMPLETA.

    Es el mismo error de siempre: el rango es la REPRESENTACIÓN de la
    completitud; la COSA es que estén todas las filas.

    Doce a dieciocho números que un humano reconoce de un vistazo — los picos
    de temporada donde deben estar, ningún mes en cero, ninguno anómalamente
    bajo. Un histograma que se lee en cinco segundos y delata el hueco que el
    rango esconde.

    Returns: [{mes, filas, sospechoso}] ordenado, con la mediana como
    referencia para marcar los meses anómalos.
    """
    from sqlalchemy import func

    # El truncado a mes se hace distinto en SQLite y Postgres
    if db.engine.dialect.name == 'sqlite':
        mes_expr = func.strftime('%Y-%m', KardexMovimiento.fecha)
    else:
        mes_expr = func.to_char(KardexMovimiento.fecha, 'YYYY-MM')

    filas = (
        db.session.query(mes_expr.label('mes'), func.count(KardexMovimiento.id))
        .group_by(mes_expr).order_by(mes_expr).all()
    )
    if not filas:
        return {'meses': [], 'huecos': [], 'nota': 'Kardex vacío.'}

    conteos = sorted(n for _m, n in filas)
    mediana = conteos[len(conteos) // 2]
    # Un mes por debajo del 25% de la mediana es anómalo. No prueba un hueco,
    # pero es donde hay que mirar — y mirar es justo lo que el rango impedía.
    umbral = mediana * 0.25

    meses, huecos = [], []
    for mes, n in filas:
        sospechoso = n < umbral
        meses.append({'mes': mes, 'filas': n, 'sospechoso': sospechoso})
        if sospechoso:
            huecos.append(mes)

    # Meses ausentes por completo dentro del rango cubierto
    from datetime import date as _d
    presentes = {m for m, _ in filas}
    y0, m0 = map(int, filas[0][0].split('-'))
    y1, m1 = map(int, filas[-1][0].split('-'))
    ausentes = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        etiqueta = f'{y:04d}-{m:02d}'
        if etiqueta not in presentes:
            ausentes.append(etiqueta)
        m += 1
        if m > 12:
            y, m = y + 1, 1

    return {
        'meses': meses,
        'mediana_filas': mediana,
        'huecos': huecos,
        'meses_ausentes': ausentes,
        'sin_huecos': not huecos and not ausentes,
        'nota': ('Un mes en cero o muy por debajo de la mediana es el hueco que el '
                 'rango pedido-vs-traído no ve. Los picos de temporada deben estar '
                 'donde se esperan.'),
    }


# ══════════════════════════════════════════════════════════════════════════════
# LA DEMANDA DIARIA — UNA FUNCIÓN PARA EL NUMERADOR, UNA PARA EL DENOMINADOR
#
# (2026-09-24) Toda demanda diaria del sistema —ROP, contenedor, S-B, TSB,
# temporada, tasa servida, bloqueo de recompra— sale de estas dos:
#
#   serie_demanda(desde, hasta, nivel)        el NUMERADOR: ventas netas por día
#   intervalos_con_stock(desde, hasta, nivel) el DENOMINADOR: qué días hubo stock
#
# y del reparto de `dias_expuestos()` cuando falta el denominador. Ningún otro
# sitio lee los conceptos 501/502 para demanda ni cuenta `StockDiario`
# (trinquete AST: tests/test_demanda_una_funcion.py).
#
# QUÉ COSTABA NO TENERLO — el defecto D1 de la auditoría de compras.
# `reconstruir_stock_diario` escribía una fila de `StockDiario` SOLO en los días
# con movimiento, y los consumidores contaban `count(distinct fecha) WHERE
# tuvo_stock`. Un día sin movimiento no existía, así que «días con stock» era
# «días con venta»: el mismo error que `dias_expuestos` prohíbe por escrito
# («NUNCA usar días-con-venta como denominador»), entrado por la tabla en vez
# de por la fórmula. Medido con el reconstructor real: un SKU que vende 40
# cada 25 días con stock siempre salía a 40/día en vez de 1,56 (×25,7), el ROP
# a 332 en vez de 37, S-B lo ponía en SUAVE (ADI 1) y la temporada ×3.
#
# LA CURA: `StockDiario` sigue guardando el cierre de los días con movimiento
# (más el saldo de APERTURA, el día antes del primer movimiento) y se lee como
# lo que es —una FUNCIÓN ESCALÓN—: el stock al cierre del día t es el de la
# última fila con fecha ≤ t. No hace falta una fila por día calendario (serían
# ~20 millones); hace falta contar intervalos.
# ══════════════════════════════════════════════════════════════════════════════

#: Días por mes de la ventana de demanda. La ventana de 12 meses son 360 días.
DIAS_POR_MES = 30

#: «Sin venta reciente»: cuántos días hacia atrás se miran. Configurable.
DIAS_SIN_VENTA_DEFAULT = 90

#: Cuántas ventas habría que esperar en esos días para que cero sea evidencia.
#: Poisson: P(0 ventas | λ ≥ 3) = e⁻³ ≈ 5%. Con λ < 3, no vender es azar.
VENTAS_ESPERADAS_MIN_PARA_DESCONTINUAR = 3.0


def dias_sin_venta():
    """Días sin venta que declaran un SKU «sin venta reciente» (`ROP_DIAS_SIN_VENTA`)."""
    try:
        v = int(os.environ.get('ROP_DIAS_SIN_VENTA', DIAS_SIN_VENTA_DEFAULT))
    except (TypeError, ValueError):
        v = DIAS_SIN_VENTA_DEFAULT
    return max(7, v)


def ventana_demanda(ventana_meses=12, hasta=None):
    """(desde, hasta) de la ventana de demanda, AMBOS INCLUSIVE, día Bogotá.

    `ventana_meses * 30` días exactos que terminan hoy: la ventana de 12 meses
    son 360 días, y 360 es el denominador cuando el stock nunca faltó.
    """
    hasta = hasta or _dia_operativo()
    dias = max(1, int(round(float(ventana_meses) * DIAS_POR_MES)))
    return hasta - timedelta(days=dias - 1), hasta


def inicio_cobertura_kardex():
    """El primer día que el kardex almacenado observa (o `None` si está vacío).

    Antes de ese día no se sabe ni cuánto se vendió ni si había stock: no es
    un día sin venta, es un día no observado. Las ventanas se recortan acá en
    vez de contarlo como cero — contarlo como cero diluye la demanda de toda
    la ventana en la proporción del hueco (un kardex de 3 meses leído sobre 12
    divide la demanda por 4).
    """
    from sqlalchemy import func
    return db.session.query(func.min(KardexMovimiento.fecha)).scalar()


def ventana_observada(ventana_meses=12, hasta=None):
    """La ventana de demanda recortada a la cobertura del kardex.

    Returns: (desde, hasta, cobertura) — `desde` es `None` si el kardex está
    vacío o empieza después de `hasta`.
    """
    desde, hasta = ventana_demanda(ventana_meses, hasta)
    cob = inicio_cobertura_kardex()
    if cob is None or cob > hasta:
        return None, hasta, cob
    return max(desde, cob), hasta, cob


def _clave(ref, bod, nivel):
    ref = (ref or '').strip()
    if nivel == 'red':
        return ref
    return f'{ref}|{(bod or "").strip()}'


def serie_demanda(desde, hasta, nivel='red'):
    """EL NUMERADOR. Demanda neta por día y por clave, ambos extremos inclusive.

    Venta = concepto 501 en salida; devolución = 502 en entrada
    (`CONCEPTO_DEFINICION`). **La devolución se netea contra la venta que
    devuelve, no contra el día en que llega** (D6): se descuenta primero de la
    venta de su mismo día y el resto de los días de venta anteriores, del más
    reciente hacia atrás. Lo que no encuentra venta dentro de la ventana es la
    devolución de una venta anterior a la ventana y NO se resta de la demanda
    de esta ventana (restarla subestimaría una demanda que no la contiene): se
    cuenta aparte en `devolucion_sin_venta`.

    Antes el neteo era por día —«ventas menos devoluciones del MISMO día»—, así
    que una venta de 100 devuelta al otro día en 30 quedaba en 100 en la
    descensura y en 70 en la tasa servida, que neteaba por ventana: el mismo
    concepto con dos políticas.

    Returns: {clave: {'por_dia': {fecha: neto > 0}, 'bruta', 'devuelta',
              'devolucion_sin_venta'}}. Clave: referencia (red) o 'ref|bodega'.
    """
    from sqlalchemy import func

    if desde is None or hasta is None or desde > hasta:
        return {}

    k_bod = KardexMovimiento.bodega

    def _por_dia(conceptos, naturaleza):
        return (
            db.session.query(KardexMovimiento.referencia, k_bod,
                             KardexMovimiento.fecha,
                             func.sum(KardexMovimiento.cantidad))
            .filter(KardexMovimiento.fecha >= desde)
            .filter(KardexMovimiento.fecha <= hasta)
            .filter(KardexMovimiento.concepto.in_(conceptos))
            .filter(KardexMovimiento.naturaleza == naturaleza)
            .group_by(KardexMovimiento.referencia, k_bod, KardexMovimiento.fecha)
            .all()
        )

    ventas = defaultdict(lambda: defaultdict(float))
    devol = defaultdict(lambda: defaultdict(float))
    for ref, bod, fecha, cant in _por_dia(CONCEPTOS_VENTA, NATURALEZA_SALIDA):
        if (ref or '').strip():
            ventas[_clave(ref, bod, nivel)][fecha] += float(cant or 0)
    for ref, bod, fecha, cant in _por_dia(CONCEPTOS_DEVOLUCION, NATURALEZA_ENTRADA):
        if (ref or '').strip():
            devol[_clave(ref, bod, nivel)][fecha] += float(cant or 0)

    salida = {}
    for clave in set(ventas) | set(devol):
        v, d = ventas.get(clave, {}), devol.get(clave, {})
        neto = {}
        orden = []            # días de venta ya vistos, en orden cronológico
        sin_venta = 0.0
        devuelta = 0.0
        for fecha in sorted(set(v) | set(d)):
            q = v.get(fecha, 0.0)
            if q > 0:
                neto[fecha] = q
                orden.append(fecha)
            resto = d.get(fecha, 0.0)
            # Del día más reciente hacia atrás: el mismo día primero.
            i = len(orden) - 1
            while resto > 1e-9 and i >= 0:
                f = orden[i]
                usar = min(resto, neto[f])
                neto[f] -= usar
                resto -= usar
                devuelta += usar
                i -= 1
            sin_venta += max(resto, 0.0)
        salida[clave] = {
            'por_dia': {f: x for f, x in neto.items() if x > 1e-9},
            'bruta': round(sum(v.values()), 4),
            'devuelta': round(devuelta, 4),
            'devolucion_sin_venta': round(sin_venta, 4),
        }
    return salida


def intervalos_con_stock(desde, hasta, nivel='red'):
    """EL DENOMINADOR. Qué días de [desde, hasta] tuvieron stock, por clave.

    `StockDiario` es una función escalón: el stock al cierre del día t es el
    `stock_cierre` de la última fila con fecha ≤ t (el reconstructor escribe
    los días con movimiento y el saldo de apertura, el día antes del primero;
    entre dos movimientos el saldo no cambia). Antes del primer registro de una
    clave vale su primer registro — que es la apertura.

    Un día tiene stock si el cierre es > 0. A nivel red, si alguna bodega lo
    tuvo (unión de intervalos, no suma: 3 bodegas el mismo día son UN día).

    Returns: {clave: [(inicio, fin_exclusivo), ...]} en ordinales de fecha,
    ordenados y fusionados. Una clave SIN ninguna fila de `StockDiario` no
    aparece: no hay denominador medido, y el llamador cae a `dias_expuestos`.
    """
    from sqlalchemy import and_, func

    if desde is None or hasta is None or desde > hasta:
        return {}
    a0, b0 = desde.toordinal(), hasta.toordinal() + 1

    previas = db.session.query(
        StockDiario.referencia.label('r'), StockDiario.bodega.label('b'),
        func.max(StockDiario.fecha).label('f'),
    ).filter(StockDiario.fecha < desde).group_by(
        StockDiario.referencia, StockDiario.bodega).subquery()
    filas_previas = (
        db.session.query(StockDiario.referencia, StockDiario.bodega,
                         StockDiario.stock_cierre)
        .join(previas, and_(StockDiario.referencia == previas.c.r,
                            StockDiario.bodega == previas.c.b,
                            StockDiario.fecha == previas.c.f))
        .all()
    )
    filas = (
        db.session.query(StockDiario.referencia, StockDiario.bodega,
                         StockDiario.fecha, StockDiario.stock_cierre)
        .filter(StockDiario.fecha >= desde, StockDiario.fecha <= hasta)
        .order_by(StockDiario.referencia, StockDiario.bodega, StockDiario.fecha)
        .all()
    )

    inicial = {(r, b): float(s or 0) for r, b, s in filas_previas}
    puntos = defaultdict(list)
    for r, b, f, s in filas:
        puntos[(r, b)].append((f.toordinal(), float(s or 0)))

    por_clave = defaultdict(list)
    for rb in set(inicial) | set(puntos):
        pts = puntos.get(rb, [])
        v0 = inicial[rb] if rb in inicial else pts[0][1]
        escalones = [(a0, v0)] + [p for p in pts if p[0] > a0]
        if pts and pts[0][0] == a0:
            escalones[0] = (a0, pts[0][1])
        for i, (ini, valor) in enumerate(escalones):
            fin = escalones[i + 1][0] if i + 1 < len(escalones) else b0
            if valor > 0 and fin > ini:
                por_clave[_clave(rb[0], rb[1], nivel)].append((ini, fin))

    salida = {}
    for clave, tramos in por_clave.items():
        tramos.sort()
        unidos = [list(tramos[0])]
        for ini, fin in tramos[1:]:
            if ini <= unidos[-1][1]:
                unidos[-1][1] = max(unidos[-1][1], fin)
            else:
                unidos.append([ini, fin])
        salida[clave] = [tuple(t) for t in unidos]
    # Las claves con filas pero sin un solo día con stock existen igual: tienen
    # denominador medido (cero), que no es lo mismo que no tener denominador.
    for rb in set(inicial) | set(puntos):
        salida.setdefault(_clave(rb[0], rb[1], nivel), [])
    return salida


def dias_en(tramos, desde, hasta):
    """Días de [desde, hasta] (inclusive) cubiertos por `tramos`."""
    if not tramos or desde is None or hasta is None or desde > hasta:
        return 0
    a, b = desde.toordinal(), hasta.toordinal() + 1
    return sum(max(0, min(fin, b) - max(ini, a)) for ini, fin in tramos)


def sin_venta_reciente(ventas_recientes, dias_stock_recientes, d_avg, dias_tramo):
    """¿Dejó de venderse? La política de «descontinuado» del ROP (D14).

    Un SKU que vendió 10/día en el primer semestre y nada en los últimos 90
    días con el estante lleno tiene d_avg ≈ 5 sobre la ventana de 12 meses, y
    el ROP lo seguía reponiendo. Sin venta en el tramo reciente NO basta: un
    SKU lento (0,01/día) pasa 90 días sin vender por azar, y uno agotado no
    vende porque no hay. Se exigen las tres:

      · cero ventas en el tramo;
      · stock al menos la mitad del tramo (si no, es agotado, no descontinuado);
      · que con su propia tasa se hubieran esperado ≥ 3 ventas en esos días
        (P(0 | Poisson 3) ≈ 5%: el cero es evidencia, no azar).
    """
    if ventas_recientes > 0 or dias_tramo <= 0:
        return False
    if dias_stock_recientes < dias_tramo / 2.0:
        return False
    return d_avg * dias_stock_recientes >= VENTAS_ESPERADAS_MIN_PARA_DESCONTINUAR


class KardexService:

    @staticmethod
    def descargar_kardex(fecha_desde: str, fecha_hasta: str = None,
                         pagina_inicial: int = 1, max_minutos: int = None) -> dict:
        """
        Descarga movimientos del kardex de Siesa via consulta dinámica.

        UNA DESCARGA PARCIAL ES UN FALLO, NO UNA ADVERTENCIA.

        Antes había tres formas de terminar —fin natural, timeout a los 30
        minutos, y excepción— y las tres caían en el MISMO retorno de éxito.
        Un kardex truncado no produce un error: produce descensura equivocada,
        ROP equivocado y temporada equivocada, todo plausible y sin una sola
        alarma. La aritmética lo hacía probable, no hipotético: ~17.000
        peticiones a ~0.1-0.2 s cada una son 28-57 minutos, y el corte estaba
        DENTRO de ese rango, no cerca.

        POR QUÉ SE TROCEA POR PÁGINA Y NO POR FECHA: la consulta dinámica NO
        acepta filtros de fecha —se filtra en Python después de recibir— así que
        acotar el rango no reduce ni una petición. Lo que sí funciona es
        reanudar: cada corrida avanza lo que puede y devuelve dónde quedó.

        RITMO REAL, MEDIDO EN PRODUCCIÓN (log de Railway, 27-jul-2026):
        18 páginas en 58 segundos = 3.41 s/página (2.41s de latencia + 1s de
        throttle). NO los 0.1-0.2 s/página que se supusieron: es 23x más lento.

            17.000 páginas x 3.41 s = 16 HORAS   (11 sin throttle)

        La descarga completa NO cabe en una sesión. Reanudar deja de ser red de
        seguridad y pasa a ser el único camino: ~40 corridas de 25 minutos. Y
        con ese calendario, la estabilidad del orden de paginación deja de ser
        una curiosidad: es la diferencia entre un kardex íntegro y uno con
        huecos repartidos a lo largo de dos días.

        Args:
            fecha_desde: YYYYMMDD — inicio del período a conservar
            fecha_hasta: YYYYMMDD — fin (default: hoy)
            pagina_inicial: desde qué página seguir (reanudación)
            max_minutos: tope de esta corrida (default KARDEX_MAX_MINUTOS o 25)

        Returns:
            {ok, estado, rango_pedido, rango_traido, reanudar_desde, ...}
            ok=True SOLO si estado == 'COMPLETA'.
        """
        from app.services.connekta_gateway import connekta

        if not fecha_hasta:
            fecha_hasta = _dia_operativo().strftime('%Y%m%d')

        from datetime import date as _date_k
        try:
            fecha_desde_dt = datetime.strptime(fecha_desde[:8], '%Y%m%d').date()
        except (ValueError, TypeError):
            fecha_desde_dt = _date_k(2025, 7, 23)
        try:
            fecha_hasta_dt = datetime.strptime(fecha_hasta[:8], '%Y%m%d').date() if fecha_hasta else _date_k.today()
        except (ValueError, TypeError):
            fecha_hasta_dt = _date_k.today()

        total = 0
        pagina = max(1, int(pagina_inicial or 1))
        errores = 0
        filtrados = 0
        inicio = datetime.utcnow()
        # Por debajo del corte anterior: mejor varias corridas honestas que una
        # que se rinde justo donde nadie mira. Acotado por `tope_minutos`: de
        # ese techo depende saber cuándo un registro abierto es un proceso
        # muerto (ver `estado_descarga`).
        MAX_MINUTOS = tope_minutos(max_minutos)

        # Cómo terminó. Es el dato que faltaba: sin él, truncado y completo son
        # el mismo retorno.
        estado = 'COMPLETA'
        detalle_estado = None
        # Si la paginación no enumera, ninguna descarga puede estar completa —
        # por rápida que sea. Se declara aquí para que el resultado no prometa
        # lo que la tubería no puede dar.
        paginacion_no_enumera = (
            os.environ.get('KARDEX_PAGINACION_NO_ENUMERA', '').lower() == 'true')
        fecha_min = fecha_max = None
        duplicados = 0
        total_declarado = None
        paginas_declaradas = None
        # Lo que ATERRIZÓ en esta corrida, sin filtro de fechas: filas que
        # llegaron y cuántas identidades distintas son. Es el otro lado del
        # conteo que Siesa declara.
        filas_recibidas = 0
        identidades_corrida = set()
        sin_clave_natural = 0

        # Hashes ya presentes: hace la descarga idempotente entre corridas.
        vistos_bd = {h for (h,) in db.session.query(KardexMovimiento.hash_origen)
                     .filter(KardexMovimiento.hash_origen.isnot(None)).all()}
        vistos_lote = set()

        while True:
            elapsed = (datetime.utcnow() - inicio).total_seconds()
            if elapsed > MAX_MINUTOS * 60:
                estado = 'TIMEOUT_PARCIAL'
                detalle_estado = (
                    f'Corte por tiempo tras {elapsed:.0f}s en la página {pagina}. '
                    f'NO es una descarga completa. Reanudar desde esa página.'
                )
                logger.error('[KARDEX] PARCIAL por tiempo — página %d, %.0fs', pagina, elapsed)
                break

            try:
                # Sin parametros de filtro — la consulta dinámica no los soporta
                # Filtramos en Python después de recibir los datos
                res = connekta._get(
                    NOMBRE_CONSULTA,
                    params_extra={
                        'paginacion': f'numPag={pagina}|tamPag=100',
                    },
                    url=connekta.url_get_dinamico,
                    timeout=60,
                )

                # Consultas dinámicas usan "Datos", no "Table"
                detalle = (res or {}).get('detalle', {})
                rows = detalle.get('Datos', []) or detalle.get('Table', []) or []

                # ¿Siesa declara cuántas filas hay? Si lo hace, es la verdad de
                # origen: declaradas vs aterrizadas es una prueba de completitud
                # por CONTEO, superior al perfil mensual, que la infiere por
                # distribución. Ver `totales_declarados` (nombres medidos).
                if total_declarado is None and paginas_declaradas is None:
                    total_declarado, paginas_declaradas = totales_declarados(res)
                    if total_declarado is not None:
                        logger.info('[KARDEX] Siesa declara %d registros en %s páginas',
                                    total_declarado, paginas_declaradas)

                # Log de descubrimiento: mostrar keys del primer registro
                if pagina == 1 and rows:
                    first = rows[0] if isinstance(rows[0], dict) else {}
                    logger.info('[KARDEX] Keys del primer registro: %s', list(first.keys()))
                    logger.info('[KARDEX] Primer registro completo: %s', first)
                if not rows:
                    if pagina == max(1, int(pagina_inicial or 1)):
                        estado = 'SIN_DATOS'
                        detalle_estado = 'La primera página vino vacía — ¿credenciales o consulta?'
                    elif paginas_declaradas and pagina <= paginas_declaradas:
                        # Una página vacía ANTES del final declarado no es el
                        # fin: es un hueco. Cortar ahí y llamarlo COMPLETA era
                        # exactamente el truncamiento que esto existe para ver.
                        estado = 'PAGINA_VACIA_ANTES_DEL_FINAL'
                        detalle_estado = (
                            f'La página {pagina} vino vacía y Siesa declara '
                            f'{paginas_declaradas} páginas.')
                    break

                filas_recibidas += len(rows)
                for row in rows:
                    f = _parsear_fila(row)
                    if f is None:
                        errores += 1
                        continue
                    identidades_corrida.add(f['hash'])
                    if not f['consec_docto'] and f['nro_registro'] is None:
                        sin_clave_natural += 1

                    fecha = f['fecha']
                    # Filtro por rango de fechas (en Python, no en Connekta)
                    if fecha < fecha_desde_dt or fecha > fecha_hasta_dt:
                        filtrados += 1
                        continue

                    h = f['hash']
                    # Idempotencia: reanudar o repetir la descarga NO duplica.
                    if h in vistos_lote or h in vistos_bd:
                        duplicados += 1
                        continue
                    vistos_lote.add(h)

                    mov = KardexMovimiento(
                        fecha=fecha,
                        tipo_docto=f['tipo_docto'],
                        bodega=f['bodega'],
                        referencia=f['referencia'],
                        concepto=f['concepto'],
                        naturaleza=f['naturaleza'],
                        cantidad=f['cantidad'],
                        costo_promedio=f['costo_promedio'],
                        consec_docto=f['consec_docto'],
                        nro_registro=f['nro_registro'],
                        hash_origen=h,
                    )
                    db.session.add(mov)
                    total += 1
                    if fecha_min is None or fecha < fecha_min:
                        fecha_min = fecha
                    if fecha_max is None or fecha > fecha_max:
                        fecha_max = fecha

                db.session.commit()
                logger.info('[KARDEX] Página %d: %d movimientos', pagina, len(rows))

                # EL FIN LO DECLARA SIESA, no el tamaño de la página. Con
                # `len(rows) < 100` como único criterio, una página corta en
                # el medio —o un tope de página más chico que 100 del lado del
                # servidor— terminaba la descarga como COMPLETA.
                if paginas_declaradas:
                    if pagina >= paginas_declaradas:
                        break
                elif len(rows) < 100:
                    break

                pagina += 1

                # Throttle: respiro entre páginas
                import time
                time.sleep(float(os.environ.get('KARDEX_PAGE_DELAY_S', '1')))

            except Exception as e:
                estado = 'ERROR_PARCIAL'
                detalle_estado = f'Excepción en la página {pagina}: {e}'
                logger.error('[KARDEX] PARCIAL por error — página %d: %s', pagina, e)
                errores += 1
                db.session.rollback()
                break

        # LA PRUEBA POR CONTEO. Solo vale para una corrida que recorrió el
        # conjunto desde la página 1: una reanudación no ve lo anterior.
        #
        # Se comparan IDENTIDADES distintas, no filas recibidas. Con un orden no
        # determinista cada página trae sus 100 filas y la suma cuadra igual —
        # medido: la página 50 pedida dos veces no compartió ni una fila—, pero
        # unas se repiten y otras no llegan nunca. Y si faltan identidades
        # porque dos movimientos legítimos son indistinguibles sin la clave
        # natural, el kardex almacenado también tiene menos de lo que Siesa
        # declara: el hash los colapsa en uno. Las dos causas dan un kardex
        # incompleto, y por eso las dos lo declaran.
        faltan_por_conteo = None
        if (estado == 'COMPLETA' and total_declarado is not None
                and int(pagina_inicial or 1) == 1):
            faltan_por_conteo = total_declarado - len(identidades_corrida)
            if faltan_por_conteo != 0:
                estado = 'CONTEO_NO_CUADRA'
                detalle_estado = (
                    f'Siesa declara {total_declarado} registros; llegaron '
                    f'{filas_recibidas} filas y {len(identidades_corrida)} '
                    f'movimientos distintos ({errores} ilegibles). Diferencia: '
                    f'{faltan_por_conteo}. Causas posibles: el orden de la '
                    'consulta no es determinista (filas repetidas y filas '
                    'nunca vistas) o movimientos idénticos sin consecutivo ni '
                    'línea, que la identidad no distingue.')

        completa = estado == 'COMPLETA' and not paginacion_no_enumera
        if estado == 'COMPLETA' and paginacion_no_enumera:
            estado = 'ORDEN_NO_ENUMERA'
            detalle_estado = (
                'Se recorrieron todas las páginas, pero el orden de la consulta '
                'no es determinista: filas repetidas y filas nunca vistas, sin '
                'forma de saber cuáles. Recorrer no es enumerar.')
        resultado = {
            'ok': completa,
            'estado': estado,
            'detalle_estado': detalle_estado,
            # RANGO PEDIDO vs RANGO TRAÍDO — la comparación que delata el truncamiento
            'rango_pedido': {'desde': fecha_desde_dt.isoformat(),
                             'hasta': fecha_hasta_dt.isoformat()},
            'rango_traido': {'desde': fecha_min.isoformat() if fecha_min else None,
                             'hasta': fecha_max.isoformat() if fecha_max else None},
            'total_descargados': total,
            'pagina_inicial': int(pagina_inicial or 1),
            'pagina_final': pagina,
            'reanudar_desde': None if completa else pagina,
            'filtrados_fuera_de_rango': filtrados,
            'duplicados_omitidos': duplicados,
            # Verdad de origen si Siesa la declara: conteo, no distribución.
            'total_declarado_por_siesa': total_declarado,
            'paginas_declaradas_por_siesa': paginas_declaradas,
            'filas_recibidas': filas_recibidas,
            'movimientos_distintos_recibidos': len(identidades_corrida),
            'faltan_por_conteo': faltan_por_conteo,
            # Sin consecutivo ni línea, dos movimientos iguales del mismo día
            # (dos ventas POS de 1 unidad) tienen la misma identidad y el hash
            # guarda uno solo: demanda de menos. Se cuenta para que se vea.
            'filas_sin_clave_natural': sin_clave_natural,
            'errores': errores,
            'segundos': round((datetime.utcnow() - inicio).total_seconds()),
            # El rango no ve los agujeros del medio. Esto sí.
            'perfil_mensual': perfil_mensual_kardex(),
            '_supuesto_de_reanudacion': (
                'Reanudar desde una página asume que el orden del conjunto de '
                'resultados es ESTABLE entre corridas. La consulta dinámica no '
                'recibe parámetro de orden desde aquí — lo define Siesa. Si el '
                'orden no es monótono y estable, la página N de la segunda corrida '
                'no contiene lo mismo y quedan HUECOS. Verificar el perfil mensual '
                'siempre; preferir una sola sesión a varias corridas.'
            ) if not completa else None,
        }
        if not completa:
            resultado['advertencia'] = (
                'DESCARGA INCOMPLETA. No correr /reconstruir ni los modelos con '
                'estos datos: la descensura, el ROP y la temporada heredarían el '
                f'hueco sin avisar. Reanudar desde la página {pagina}.'
            )
        logger.log(
            logging.INFO if completa else logging.ERROR,
            '[KARDEX] %s — %d movimientos, páginas %d-%d, rango traído %s..%s',
            estado, total, resultado['pagina_inicial'], pagina,
            resultado['rango_traido']['desde'], resultado['rango_traido']['hasta'],
        )
        return resultado

    @staticmethod
    def probar_estabilidad_paginacion(pagina: int = 50, espera_s: int = 90) -> dict:
        """Diagnostica SI la paginación por offset puede enumerar el kardex.

        La primera versión solo respondía "estable / inestable". Con el
        resultado real —0 de 100 filas en la misma posición tras 90s— eso no
        alcanza, porque hay DOS causas con remedios OPUESTOS:

          A) DERIVA POR INSERCIÓN: filas nuevas empujan el offset. Ir rápido
             reduce el daño; la descarga en una sola sesión ayuda.
          B) ORDEN NO DETERMINISTA: la consulta no lleva ORDER BY y el motor
             devuelve filas en cualquier orden. Ir rápido NO ayuda en nada y
             la paginación por offset simplemente NO PUEDE enumerar el
             conjunto — ni en una sesión ni en cuarenta.

        Se distinguen midiendo a intervalo CORTO además del largo. Y se añade
        una tercera medida que es la que de verdad importa: si dos páginas
        CONSECUTIVAS pedidas seguidas comparten filas, la paginación está
        perdiendo y repitiendo datos aquí y ahora.

        Returns: {veredicto, causa_probable, se_puede_paginar, ...}
        """
        import time
        from app.services.connekta_gateway import connekta

        def _traer(pag):
            res = connekta._get(
                NOMBRE_CONSULTA,
                params_extra={'paginacion': f'numPag={pag}|tamPag=100'},
                url=connekta.url_get_dinamico, timeout=60,
            )
            det = (res or {}).get('detalle', {})
            return det.get('Datos', []) or det.get('Table', []) or []

        def _firmas(rows):
            # La misma identidad que usa la descarga (`_parsear_fila`), sin
            # `LineaRegistro`: ese número es la POSICIÓN en el resultado, no
            # la fila.
            return [f['hash'] for f in map(_parsear_fila, rows) if f is not None]

        def _en_comun(a, b):
            # CONJUNTOS, no posiciones. Medido el 2026-09-24: dentro de una
            # página las filas no vienen ordenadas (4902, 4906, 4910, … y en
            # el pedido siguiente 4901, 4905, …). Comparar posición por
            # posición marca «0 de 100 iguales» aunque la página traiga las
            # MISMAS filas, y eso se leía como orden no determinista. La
            # pregunta de la paginación es si la página N contiene lo mismo,
            # no en qué orden lo muestra.
            return len(set(a) & set(b))

        base = _firmas(_traer(pagina))
        if not base:
            return {'veredicto': None, 'error': f'La página {pagina} vino vacía.'}

        # 1) Intervalo CORTO — distingue deriva de no-determinismo
        time.sleep(5)
        corto = _firmas(_traer(pagina))
        iguales_corto = _en_comun(base, corto)

        # 2) Página CONSECUTIVA — ¿se solapan páginas contiguas?
        vecina = _firmas(_traer(pagina + 1))
        solape = _en_comun(base, vecina)

        # 3) Intervalo LARGO
        time.sleep(max(1, int(espera_s)))
        largo = _firmas(_traer(pagina))
        iguales_largo = _en_comun(base, largo)

        n = len(set(base))
        estable_corto = iguales_corto >= n * 0.95
        estable_largo = iguales_largo >= n * 0.95

        if estable_corto and estable_largo:
            causa, puede = 'NINGUNA — el orden se mantuvo', True
            veredicto = ('El orden es estable bajo carga real. La reanudación '
                         'multi-sesión es segura.')
        elif estable_corto and not estable_largo:
            causa, puede = 'DERIVA_POR_INSERCION', True
            veredicto = ('El orden aguanta segundos pero no minutos: son filas '
                         'nuevas empujando el offset. La descarga debe ir en UNA '
                         'sesión y lo más rápido posible; hash_origen protege del '
                         'solape. Reanudar entre días NO es seguro.')
        else:
            causa, puede = 'ORDEN_NO_DETERMINISTA', False
            veredicto = (
                'El orden cambia entre peticiones consecutivas. La paginación por '
                'offset NO PUEDE enumerar el kardex — ni en una sesión ni en '
                'cuarenta: cada página se pide sobre un orden distinto, así que '
                'habría filas repetidas y filas nunca vistas, sin forma de saber '
                'cuándo se terminó. NO correr la descarga completa. Hace falta que '
                'la consulta dinámica lleve un ORDER BY por clave monótona '
                '(LineaRegistro o consecutivo de documento) — eso se pide a Nelly.'
            )

        return {
            'pagina': pagina,
            'filas': n,
            'iguales_tras_5s': iguales_corto,
            'iguales_tras_%ds' % int(espera_s): iguales_largo,
            'solape_con_pagina_siguiente': solape,
            'estable_corto': estable_corto,
            'estable_largo': estable_largo,
            'causa_probable': causa,
            'se_puede_paginar': puede,
            'veredicto': veredicto,
            'nota_solape': (
                f'{solape} filas aparecen en la página {pagina} Y en la {pagina + 1}. '
                f'Páginas contiguas no deberían compartir ninguna.'
            ) if solape else None,
        }

    @staticmethod
    def reconstruir_stock_diario(bodega: str = None) -> dict:
        """
        Reconstruye stock diario hacia atrás: saldo actual - movimientos acumulados.

        Para cada (referencia, bodega):
        1. Obtiene saldo actual de stock_siesa o ubicacion_producto
        2. Recorre movimientos del más reciente al más antiguo
        3. Resta entradas, suma salidas (inverso de la naturaleza)
        4. Marca tuvo_stock = True si stock_cierre > 0

        El paso 1 puede no encontrar ancla (hoy solo consulta `stock_siesa`).
        Esas referencias se reconstruyen desde 0 —serie plana, `tuvo_stock`
        False siempre— y quedan CONTADAS en `refs_sin_ancla`: es el denominador
        de la demanda descensurada, y un cero silencioso ahí se lee como
        «agotado» y hace comprar de menos. Ver `_obtener_saldo_actual`.

        Returns: {referencias_procesadas, dias_generados, reporte_calidad,
                  refs_sin_ancla}
        """
        from sqlalchemy import func, distinct

        # Obtener todas las combinaciones (referencia, bodega) con movimientos
        query = db.session.query(
            KardexMovimiento.referencia,
            KardexMovimiento.bodega
        ).distinct()
        if bodega:
            query = query.filter(KardexMovimiento.bodega == bodega)
        combos = query.all()

        refs_procesadas = 0
        dias_generados = 0
        calidad = []  # reporte de calidad por SKU×bodega
        sin_ancla = []  # SKU×bodega reconstruidos sobre un ancla que no existe
        UMBRAL_NEGATIVO = 0.10  # 10% — por encima, dato insuficiente

        from collections import defaultdict

        for ref, bod in combos:
            saldo_actual, fuente_ancla = KardexService._obtener_saldo_actual(ref, bod)
            if fuente_ancla is None:
                sin_ancla.append({'referencia': ref, 'bodega': bod})

            movimientos = (
                KardexMovimiento.query
                .filter_by(referencia=ref, bodega=bod)
                .order_by(KardexMovimiento.fecha.desc())
                .all()
            )

            if not movimientos:
                continue

            dias_mov = defaultdict(list)
            for m in movimientos:
                dias_mov[m.fecha].append(m)

            # ── LA APERTURA Y LAS FILAS QUE YA ESTABAN ─────────────────────
            #
            # `StockDiario` se lee como función escalón (`intervalos_con_stock`):
            # el cierre de un día vale hasta el siguiente registro. Para eso
            # hacen falta dos cosas que antes no estaban:
            #
            #  · el saldo de APERTURA, el cierre del día anterior al primer
            #    movimiento. Sin él, los días entre el inicio de la ventana y el
            #    primer movimiento de este SKU no tenían valor —y un SKU lento
            #    que vendió por primera vez en marzo parecía no haber tenido
            #    stock en enero y febrero, con el estante lleno—;
            #  · que TODA fila existente de esta clave quede con el saldo
            #    correcto de su fecha, también la de un día que dejó de tener
            #    movimiento (la apertura de una reconstrucción anterior, cuando
            #    una descarga trae historia más vieja). No se borra ninguna: se
            #    recalcula su cierre con la misma caminata hacia atrás.
            existentes = {
                r.fecha: r for r in
                StockDiario.query.filter_by(referencia=ref, bodega=bod).all()
            }
            apertura = min(dias_mov) - timedelta(days=1)
            fechas_ordenadas = sorted(
                set(dias_mov) | set(existentes) | {apertura}, reverse=True)

            saldo = float(saldo_actual)
            dias_negativos = 0
            total_dias = len(dias_mov)

            for fecha in fechas_ordenadas:
                stock_cierre = saldo

                # Marcar saldos negativos (dato podrido — no corregir, solo marcar)
                if stock_cierre < 0 and fecha in dias_mov:
                    dias_negativos += 1

                existing = existentes.get(fecha)
                if existing:
                    existing.stock_cierre = stock_cierre
                    existing.tuvo_stock = stock_cierre > 0
                else:
                    db.session.add(StockDiario(
                        referencia=ref, bodega=bod, fecha=fecha,
                        stock_cierre=stock_cierre,
                        tuvo_stock=stock_cierre > 0,
                    ))
                dias_generados += 1

                for m in dias_mov.get(fecha, ()):
                    cant = float(m.cantidad)
                    if m.naturaleza == 1:
                        saldo -= cant
                    elif m.naturaleza == 2:
                        saldo += cant

            refs_procesadas += 1

            pct_negativo = round(dias_negativos / total_dias * 100, 1) if total_dias > 0 else 0
            dato_insuficiente = pct_negativo > UMBRAL_NEGATIVO * 100

            if dato_insuficiente or dias_negativos > 0:
                calidad.append({
                    'referencia': ref,
                    'bodega': bod,
                    'dias_total': total_dias,
                    'dias_negativos': dias_negativos,
                    'pct_negativo': pct_negativo,
                    'dato_insuficiente': dato_insuficiente,
                })

            if refs_procesadas % 100 == 0:
                db.session.commit()
                logger.info('[KARDEX] Reconstruido %d referencias...', refs_procesadas)

        db.session.commit()

        # Ordenar calidad por peor primero
        calidad.sort(key=lambda x: x['pct_negativo'], reverse=True)
        datos_insuficientes = sum(1 for c in calidad if c['dato_insuficiente'])

        logger.info(
            '[KARDEX] Reconstrucción completa: %d referencias, %d días, '
            '%d con saldos negativos (%d dato insuficiente), %d SIN ANCLA',
            refs_procesadas, dias_generados, len(calidad), datos_insuficientes,
            len(sin_ancla)
        )
        if sin_ancla:
            logger.warning(
                '[KARDEX] %d SKU×bodega reconstruidos con ancla 0 por no tener '
                'fila en stock_siesa. Su serie queda plana en cero, se lee como '
                '«agotado» y CENSURA LA DEMANDA HACIA ABAJO (d_avg, sigma_d, ROP, '
                's_objetivo, contenedor). Primeros: %s',
                len(sin_ancla), sin_ancla[:20])

        return {
            'referencias_procesadas': refs_procesadas,
            'dias_generados': dias_generados,
            # EL DENOMINADOR TIENE QUE SER VISIBLE. Mismo patrón que
            # `factor_censura`/`censurado` por fila en la descensura: un SKU sin
            # ancla no falla ni da error — ancla en 0, serie plana, `tuvo_stock`
            # False todos los días, demanda censurada hacia abajo → se compra de
            # menos. Y `reporte_calidad` no puede verlo: mide una PROXY
            # (pct_negativo) que solo dispara si la serie se va a negativo.
            'refs_sin_ancla': {
                'cantidad': len(sin_ancla),
                'detalle': sin_ancla[:50],
                'nota': (
                    'Sin fila en stock_siesa: la reconstrucción ancló en 0 y la '
                    'serie queda plana en cero — indistinguible de un agotado '
                    'legítimo. Su demanda descensurada SUBESTIMA. El docstring de '
                    '_obtener_saldo_actual promete una segunda fuente '
                    '(ubicacion_producto) que todavía no está implementada; '
                    'completarla mueve las series históricas de todo el catálogo y '
                    'es una decisión aparte.'
                ) if sin_ancla else None,
            },
            'reporte_calidad': {
                'total_con_negativos': len(calidad),
                'dato_insuficiente': datos_insuficientes,
                'umbral_pct': UMBRAL_NEGATIVO * 100,
                'nota': (
                    'SKUs con >10% días negativos: dato insuficiente — '
                    'heredan tasa de su categoría, no se finge precisión. '
                    'Mapa de dónde el kardex está podrido → priorizar conteo cíclico.'
                ),
                'detalle': calidad[:50],  # top 50 peores
            },
        }

    @staticmethod
    def reconciliar_kardex(ventana_meses: int = 12) -> dict:
        """
        COMPUERTA DE COMPLETITUD — 2 verificaciones:

        1. Reconciliación en UNIDADES (no pesos) × mes × bodega:
           salidas netas (501-502) para cruzar contra facturas de Siesa.
           En pesos no sirve: kardex valoriza a costo, factura a precio.

        2. Auditoría de conceptos: SELECT DISTINCT concepto del kardex.
           Todo concepto NO clasificado en CONCEPTO_DEFINICION detiene el cálculo.
           Un concepto sin clasificar que se omite es un agujero invisible.
        """
        from sqlalchemy import func

        fecha_limite = _dia_operativo() - timedelta(days=ventana_meses * 30)

        # ── 1. Reconciliación en UNIDADES × mes × bodega ──────────
        # `extract` y no `to_char`: `to_char` es de PostgreSQL y en SQLite
        # revienta con un 500. Eso dejaba este endpoint —la compuerta de
        # completitud, que ahora alimenta el semáforo de la pantalla Modelos—
        # **imposible de ejercer en la suite**: en producción funcionaba y
        # ningún test lo tocaba nunca.
        #
        # `extract('year'|'month')` es SQL estándar y SQLAlchemy lo traduce a
        # los dos motores. El 'YYYY-MM' se compone en Python, donde formatear no
        # depende del dialecto. El resultado es idéntico; lo que cambia es que
        # ahora se puede verificar.
        _anio = func.extract('year', KardexMovimiento.fecha)
        _mes = func.extract('month', KardexMovimiento.fecha)
        ventas_mes_bodega = (
            db.session.query(
                _anio.label('anio'),
                _mes.label('mes_num'),
                KardexMovimiento.bodega,
                func.sum(KardexMovimiento.cantidad).label('unidades'),
                func.count().label('registros'),
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)
            .group_by(_anio, _mes, KardexMovimiento.bodega)
            .order_by(_anio, _mes, KardexMovimiento.bodega)
            .all()
        )

        # ── 2. Auditoría de conceptos desconocidos ────────────────
        conceptos_en_kardex = set(
            r[0] for r in
            db.session.query(KardexMovimiento.concepto).distinct().all()
        )
        conceptos_conocidos = set(CONCEPTO_DEFINICION.keys())
        conceptos_desconocidos = conceptos_en_kardex - conceptos_conocidos

        # Resumen
        total = db.session.query(func.count()).select_from(KardexMovimiento).scalar() or 0
        total_por_concepto = dict(
            db.session.query(KardexMovimiento.concepto, func.count())
            .group_by(KardexMovimiento.concepto)
            .all()
        )
        bodegas = sorted(
            r[0] for r in db.session.query(KardexMovimiento.bodega).distinct().all()
        )

        # Clasificar conceptos con su definición
        conceptos_detalle = {}
        for concepto, count in sorted(total_por_concepto.items()):
            defn = CONCEPTO_DEFINICION.get(concepto)
            conceptos_detalle[str(concepto)] = {
                'registros': count,
                'clasificado': defn is not None,
                'es_demanda': defn[0] if defn else None,
                'descripcion': defn[2] if defn else 'NO CLASIFICADO — CLASIFICAR ANTES DE CALCULAR',
            }

        compuerta_ok = len(conceptos_desconocidos) == 0

        return {
            'compuerta_ok': compuerta_ok,
            'total_registros_kardex': total,
            'bodegas_con_datos': bodegas,
            'conceptos_detalle': conceptos_detalle,
            'conceptos_desconocidos': sorted(conceptos_desconocidos),
            'alerta_conceptos': (
                f'HAY {len(conceptos_desconocidos)} CONCEPTO(S) SIN CLASIFICAR: '
                f'{sorted(conceptos_desconocidos)}. Agregar a CONCEPTO_DEFINICION '
                f'antes de calcular tasas — el sistema NO debe ignorarlos.'
            ) if conceptos_desconocidos else None,
            'ventas_por_mes_bodega': [{
                # Mismo formato que antes: 'YYYY-MM'.
                'mes': f'{int(r.anio):04d}-{int(r.mes_num):02d}',
                'bodega': r.bodega,
                'unidades_vendidas': float(r.unidades or 0),
                'registros': r.registros,
            } for r in ventas_mes_bodega],
            'instruccion': (
                'Cruzar unidades_vendidas por mes × bodega contra líneas de factura '
                'de Siesa (no pesos — el kardex valoriza a costo, la factura a precio). '
                'Si difieren >2% en alguna bodega, la descarga está incompleta.'
            ),
        }

    @staticmethod
    def calcular_tasa_servida_corregida(ventana_meses: int = 12,
                                         nivel: str = 'bodega') -> dict:
        """
        Tasa servida corregida: demanda neta / días con stock.

        PRECONDICIÓN: reconciliar_kardex().compuerta_ok == True.
        Si hay conceptos sin clasificar, DETIENE con error.

        **No calcula nada propio: es una vista de `demanda_descensurada`.** Antes
        era una segunda implementación del mismo concepto, con OTRA política de
        devoluciones (neteaba por ventana mientras la descensura neteaba por
        día): la misma venta devuelta al día siguiente daba 70 acá y 100 allá.
        Ahora las dos leen `serie_demanda` y no pueden divergir.

        NO es "demanda real" — es demanda SERVIDA corregida por quiebres.
        La cobertura de rutas sigue siendo parámetro exógeno.

        Args:
            ventana_meses: ventana móvil (default 12)
            nivel: 'bodega' para reposición por nodo, 'red' para clasificación S-B

        Returns: {total_skus, con_demanda, sin_demanda, tasas: [...]}
        """
        # COMPUERTA: verificar que no hay conceptos sin clasificar
        conceptos_en_kardex = set(
            r[0] for r in db.session.query(KardexMovimiento.concepto).distinct().all()
        )
        conceptos_desconocidos = conceptos_en_kardex - set(CONCEPTO_DEFINICION.keys())
        if conceptos_desconocidos:
            raise ValueError(
                f'HAY {len(conceptos_desconocidos)} CONCEPTO(S) SIN CLASIFICAR: '
                f'{sorted(conceptos_desconocidos)}. Agregar a CONCEPTO_DEFINICION '
                f'antes de calcular. El sistema prefiere no responder a responder con hueco.'
            )

        nivel = 'red' if nivel == 'red' else 'bodega'
        desde, _hasta, cobertura = ventana_observada(ventana_meses)
        dem = KardexService.demanda_descensurada(
            ventana_meses, nivel, incluir_sin_venta=True)

        tasas = []
        for key, d in dem.items():
            entry = {
                'referencia': key.split('|')[0] if '|' in key else key,
                'demanda_bruta': d['demanda_bruta'],
                'devoluciones': d['devoluciones'],
                'demanda_neta': d['demanda_neta'],
                'dias_con_stock': d['dias_con_stock'],
                'tasa_servida_corregida': round(d['d_avg'], 4),
                'velocity_cero': d['demanda_neta'] == 0,
                # False = corregido de verdad. True = no había StockDiario y el
                # número SUBESTIMA. Mismo contrato que la descensura.
                'censurado': d['censurado'],
            }
            if nivel == 'bodega' and '|' in key:
                entry['bodega'] = key.split('|')[1]
            tasas.append(entry)

        con_demanda = sum(1 for t in tasas if not t['velocity_cero'])
        sin_demanda = sum(1 for t in tasas if t['velocity_cero'])

        tasas.sort(key=lambda x: x['tasa_servida_corregida'], reverse=True)

        return {
            'total_skus': len(tasas),
            'con_demanda': con_demanda,
            'sin_demanda': sin_demanda,
            'ventana_meses': ventana_meses,
            'nivel': nivel,
            'fecha_limite': desde.isoformat() if desde else None,
            'cobertura_kardex_desde': cobertura.isoformat() if cobertura else None,
            'censurados': sum(1 for x in tasas if x['censurado']),
            'nota': (
                'tasa_servida_corregida = demanda servida corregida por quiebres. '
                'NO es demanda real — la cobertura de rutas es parámetro exógeno. '
                'Las filas con censurado=True no tienen StockDiario: su tasa '
                'SUBESTIMA y se calculó sobre días calendario.'
            ),
            'tasas': tasas,
        }

    @staticmethod
    def demanda_descensurada(ventana_meses: int = 12, nivel: str = 'red',
                             incluir_sin_venta: bool = False) -> dict:
        """
        LA demanda diaria descensurada por SKU, con su sigma. Única en el sistema.

        Numerador: `serie_demanda` (ventas netas por día, devoluciones neteadas
        contra la venta que devuelven). Denominador: `intervalos_con_stock`
        (días con stock, contando TODOS los días del escalón, no solo los días
        con movimiento), y `dias_expuestos` cuando no hay StockDiario.

        UNIDAD CANÓNICA: día. Todo lo que salga de aquí es unidades/día.

        sigma_d se calcula sobre los días CON stock, contando como demanda cero
        los días con stock y sin venta — que es lo que hace grumosa a la demanda
        rural:

            media = suma / N
            var   = (suma_cuadrados - N * media^2) / (N - 1)

        La ventana es `ventana_meses * 30` días que terminan hoy, recortada al
        inicio de la cobertura del kardex (`ventana_observada`): un día que el
        kardex no observa no es un día sin venta.

        Además, por fila, la política de «sin venta reciente» (`sin_venta_reciente`):
        el ROP no repone lo que dejó de venderse, y lo DECLARA.

        Args:
            incluir_sin_venta: también las claves con StockDiario y sin una sola
                venta en la ventana (d_avg 0) — las pide la tasa servida.

        Returns: {clave: {d_avg, sigma_d, dias_con_stock, dias_ventana,
                          demanda_neta, demanda_bruta, devoluciones,
                          factor_censura, censurado, ventas_recientes,
                          dias_stock_recientes, sin_venta_reciente, desde, hasta}}
        """
        desde, hasta, _cob = ventana_observada(ventana_meses)
        if desde is None:
            return {}
        nivel = 'red' if nivel == 'red' else 'bodega'
        dias_ventana = (hasta - desde).days + 1

        serie = serie_demanda(desde, hasta, nivel)
        tramos = intervalos_con_stock(desde, hasta, nivel)

        n_rec = dias_sin_venta()
        desde_rec = max(desde, hasta - timedelta(days=n_rec - 1))
        dias_tramo_rec = (hasta - desde_rec).days + 1

        claves = set(serie)
        if incluir_sin_venta:
            claves |= set(tramos)

        salida = {}
        for key in claves:
            s = serie.get(key) or {'por_dia': {}, 'bruta': 0.0, 'devuelta': 0.0}
            por_dia = s['por_dia']
            suma = sum(por_dia.values())
            if suma <= 0 and not incluir_sin_venta:
                continue
            suma_cuad = sum(v * v for v in por_dia.values())

            con_tramos = key in tramos
            # Política única — ver dias_expuestos()
            n, censurado = dias_expuestos(
                dias_en(tramos.get(key), desde, hasta), dias_ventana)
            if n <= 0:
                continue

            media = suma / n
            if n > 1:
                var = (suma_cuad - n * media * media) / (n - 1)
            else:
                var = 0.0
            sigma = math.sqrt(var) if var > 0 else 0.0

            # Cuánto sube la estimación por corregir la censura. 1.0 = nunca
            # se agotó; 1.25 = se estimaba 25% por debajo.
            factor = (dias_ventana / n) if n > 0 else 1.0

            ventas_rec = sum(v for f, v in por_dia.items() if f >= desde_rec)
            # Sin StockDiario, el tramo reciente cuenta como expuesto entero:
            # la misma caída a días calendario de `dias_expuestos`, declarada
            # en `censurado`.
            stock_rec = (dias_en(tramos.get(key), desde_rec, hasta)
                         if con_tramos and not censurado else dias_tramo_rec)

            salida[key] = {
                'd_avg': round(media, 6),
                'sigma_d': round(sigma, 6),
                'dias_con_stock': n,
                'dias_ventana': dias_ventana,
                'demanda_neta': round(suma, 2),
                'demanda_bruta': round(float(s['bruta']), 2),
                'devoluciones': round(float(s['devuelta']), 2),
                'factor_censura': round(factor, 4),
                # False = descensurado de verdad. True = no había StockDiario y
                # el número quedó censurado (subestima). Reconstruir stock diario.
                'censurado': censurado,
                'ventas_recientes': round(ventas_rec, 2),
                'dias_stock_recientes': stock_rec,
                'dias_sin_venta_mirados': dias_tramo_rec,
                'sin_venta_reciente': sin_venta_reciente(
                    ventas_rec, stock_rec, media, dias_tramo_rec),
                'desde': desde.isoformat(),
                'hasta': hasta.isoformat(),
            }

        n_cens = sum(1 for v in salida.values() if v['censurado'])
        if n_cens:
            logger.warning(
                '[DESCENSURA] %d de %d SKUs SIN StockDiario — demanda censurada. '
                'Correr POST /api/kardex/reconstruir.', n_cens, len(salida))

        return salida

    @staticmethod
    def serie_semanal_descensurada(ventana_meses: int = 12) -> dict:
        """
        Serie semanal de demanda DESCENSURADA por SKU, a nivel red.

        Rejilla regular (semanas ISO, lunes) que llega HASTA LA SEMANA ACTUAL.
        Antes terminaba en la semana de la ÚLTIMA VENTA: las semanas sin venta
        del final no existían, y un SKU que dejó de venderse hace cinco meses
        seguía con su pronóstico de cuando se vendía — TSB existe justo para
        decaer en esas semanas (D7).

        Cada semana se descensura con sus PROPIOS días con stock (y sus días
        observados: la primera y la actual pueden ser parciales). Una semana en
        que el SKU estuvo agotado 4 de 7 días vendió lo que pudo en 3, y esa
        tasa proyectada a 7 es la demanda que hubo.

        Una semana SIN stock y SIN venta es `None`: no se sabe cuánto se habría
        vendido, y un cero ahí le enseña al modelo que un agotado «no se
        vendía». Un SKU sin ningún StockDiario cae a días calendario (la regla
        de `dias_expuestos`): su rejilla empieza en la semana de su primera
        venta y sus ceros cuentan.

        Returns: {referencia: [(lunes, valor | None), ...]} ordenado.
        """
        desde, hasta, _cob = ventana_observada(ventana_meses)
        if desde is None:
            return {}

        def _lunes(f):
            return f - timedelta(days=f.weekday())

        serie = serie_demanda(desde, hasta, 'red')
        tramos = intervalos_con_stock(desde, hasta, 'red')

        salida = {}
        for ref, s in serie.items():
            por_dia = s['por_dia']
            if not por_dia:
                continue
            crudo = defaultdict(float)
            for f, v in por_dia.items():
                crudo[_lunes(f)] += v
            con_tramos = ref in tramos and dias_en(tramos[ref], desde, hasta) > 0
            semana = _lunes(desde) if con_tramos else _lunes(min(por_dia))
            puntos = []
            while semana <= hasta:
                a = max(semana, desde)
                b = min(semana + timedelta(days=6), hasta)
                observados = (b - a).days + 1
                venta = crudo.get(semana, 0.0)
                if con_tramos:
                    n_stock = dias_en(tramos[ref], a, b)
                    if n_stock <= 0 and venta <= 0:
                        puntos.append((semana, None))
                        semana += timedelta(days=7)
                        continue
                else:
                    n_stock = observados
                n, _cens = dias_expuestos(n_stock, observados)
                puntos.append((semana, round(venta * (7.0 / n), 4)))
                semana += timedelta(days=7)
            salida[ref] = puntos
        return salida

    @staticmethod
    def mase(reales: list, pronostico: float) -> float:
        """
        MASE canónico: MAE del pronóstico / MAE del naive de UN PASO in-sample.

            denominador = media(|y_t - y_{t-1}|)  sobre la serie de entrenamiento

        NO es la media de la serie. Un MASE < 1 significa "mejor que repetir el
        último valor observado", que es la afirmación que la spec exige. Con el
        denominador equivocado el número queda plausible y responde otra
        pregunta — una compuerta mal calculada es peor que no tener compuerta.

        Devuelve None si la serie no tiene variación (denominador cero).
        """
        if len(reales) < 2:
            return None
        difs = [abs(reales[i] - reales[i - 1]) for i in range(1, len(reales))]
        denom = sum(difs) / len(difs)
        if denom <= 0:
            return None
        mae = sum(abs(v - pronostico) for v in reales) / len(reales)
        return round(mae / denom, 4)

    @staticmethod
    def clasificar_syntetos_boylan(ventana_meses: int = 12,
                                    estacionales_extra: list = None) -> dict:
        """
        Clasificación Syntetos-Boylan sobre demanda agregada de RED.

        REGLAS DEL CONSULTOR:
        1. Clasificar a nivel de RED (no por bodega) — el rol del SKU es global
        2. Estacionales se EXCLUYEN — los que `TemporadaService.identificar_skus_temporada`
           declara de temporada (la misma política que arma el pedido escolar)
           + parámetro extra para override
        3. La clasificación PROPONE sentencias, no ejecuta bloqueos automáticos
        4. Si un constitucional cae en 'grumosa', es señal de alarma

        ESTACIONALES (D8). Antes se leían de `ProductoClasificacionABC.clasificacion
        == 'ESTACIONAL'`, una columna `String(1)` donde esa palabra no cabe: el
        filtro no podía dar verdadero nunca y la exclusión estaba muerta. Ahora
        es la política del pedido de temporada — un SKU es de temporada en un
        solo sitio del sistema.

        DEMANDA: `serie_demanda` (neta, D6) sobre días con stock de
        `intervalos_con_stock` (D1). Con el denominador viejo —días con
        movimiento— un SKU que vende cada 25 días daba ADI = 1 y caía en SUAVE.

        Cuadrantes:
        - Suave: ADI ≤ 1.32 y CV² ≤ 0.49 → reposición automática
        - Errática: ADI ≤ 1.32 y CV² > 0.49 → colchón + revisión mensual
        - Intermitente: ADI > 1.32 y CV² ≤ 0.49 → mín-máx simple
        - Grumosa: ADI > 1.32 y CV² > 0.49 → PROPUESTA cola/remate

        Returns: {clasificacion: [...], resumen: {...}, alertas: [...]}
        """
        from app.services.temporada_service import TemporadaService

        estacionales_set = set()
        try:
            estacionales_set = set(TemporadaService.identificar_skus_temporada())
        except Exception as e:   # noqa: BLE001 — se declara abajo, no se calla
            logger.warning('[KARDEX] No se pudo identificar estacionales: %s', e)
            estacionales_error = str(e)
        else:
            estacionales_error = None

        # Override: estacionales adicionales pasados explícitamente
        if estacionales_extra:
            estacionales_set.update(x.strip() for x in estacionales_extra if x)

        desde, hasta, _cob = ventana_observada(ventana_meses)
        if desde is None:
            return {
                'total_clasificados': 0, 'estacionales_excluidos': len(estacionales_set),
                'resumen': {c: {'cantidad': 0, 'porcentaje': 0}
                            for c in ('SUAVE', 'ERRATICA', 'INTERMITENTE', 'GRUMOSA')},
                'nota': 'Kardex vacío: no hay demanda que clasificar.',
                'clasificacion': [],
            }
        dias_ventana = (hasta - desde).days + 1
        serie = serie_demanda(desde, hasta, 'red')
        tramos = intervalos_con_stock(desde, hasta, 'red')

        # Calcular ADI y CV² por SKU
        ADI_CORTE = 1.32
        CV2_CORTE = 0.49

        clasificacion = []
        excluidos = []

        for ref, s in serie.items():
            if ref in estacionales_set:
                excluidos.append(ref)
                continue
            cantidades = list(s['por_dia'].values())
            dias_con_demanda = len(cantidades)
            if dias_con_demanda == 0:
                continue
            # Política única — la misma que usa la descensura (ver dias_expuestos)
            dias_stock, sb_censurado = dias_expuestos(
                dias_en(tramos.get(ref), desde, hasta), dias_ventana)

            # ADI: días promedio entre demandas (sobre días con stock, no calendario)
            adi = dias_stock / dias_con_demanda

            # CV²: varianza relativa del tamaño de cada demanda
            media = sum(cantidades) / len(cantidades)
            if media > 0 and len(cantidades) > 1:
                varianza = sum((c - media) ** 2 for c in cantidades) / len(cantidades)
                cv2 = varianza / (media ** 2)
            else:
                cv2 = 0

            # Clasificar
            if adi <= ADI_CORTE and cv2 <= CV2_CORTE:
                cuadrante = 'SUAVE'
                politica = 'Reposición automática por tasa corregida'
            elif adi <= ADI_CORTE and cv2 > CV2_CORTE:
                cuadrante = 'ERRATICA'
                politica = 'Reposición con colchón, revisión mensual'
            elif adi > ADI_CORTE and cv2 <= CV2_CORTE:
                cuadrante = 'INTERMITENTE'
                politica = 'Mín-máx simple (mín 1 empaque, máx 2), sin pronóstico'
            else:
                cuadrante = 'GRUMOSA'
                politica = 'PROPUESTA: candidata a cola/remate — requiere revisión humana'

            clasificacion.append({
                'referencia': ref,
                'adi': round(adi, 2),
                'cv2': round(cv2, 4),
                'cuadrante': cuadrante,
                'politica': politica,
                'dias_con_demanda': dias_con_demanda,
                'dias_con_stock': dias_stock,
                'censurado': sb_censurado,
                'demanda_total': round(sum(cantidades), 2),
                'demanda_promedio_evento': round(media, 2),
            })

        # Resumen por cuadrante
        resumen = {}
        for c in ['SUAVE', 'ERRATICA', 'INTERMITENTE', 'GRUMOSA']:
            items = [x for x in clasificacion if x['cuadrante'] == c]
            resumen[c] = {
                'cantidad': len(items),
                'porcentaje': round(len(items) / len(clasificacion) * 100, 1) if clasificacion else 0,
            }

        clasificacion.sort(key=lambda x: ('SUAVE ERRATICA INTERMITENTE GRUMOSA'.split().index(x['cuadrante']), -x['demanda_total']))

        return {
            'total_clasificados': len(clasificacion),
            'estacionales_excluidos': len(excluidos),
            'estacionales_detalle': sorted(excluidos)[:200],
            'estacionales_fuente': 'TemporadaService.identificar_skus_temporada',
            'estacionales_error': estacionales_error,
            'resumen': resumen,
            'nota': (
                'Clasificación a nivel de RED (todas las bodegas agregadas). '
                'Estacionales excluidos — evaluar dentro de su ventana. '
                'Cuadrante GRUMOSA es PROPUESTA, no bloqueo automático.'
            ),
            'clasificacion': clasificacion,
        }

    @staticmethod
    def _obtener_saldo_actual(referencia: str, bodega: str):
        """Obtiene saldo actual de stock_siesa o ubicacion_producto.

        ⚠️ EL DOCSTRING PROMETE DOS FUENTES Y HAY UNA. La rama de
        `ubicacion_producto` NO está implementada — se deja escrita a propósito
        porque describe la INTENCIÓN: arreglar el documento en vez del código
        es la forma que este repo ya documenta como recurrente. Mientras no
        exista, un SKU ausente de `stock_siesa` no tiene ancla, y eso ahora se
        DECLARA (ver `refs_sin_ancla` en `reconstruir_stock_diario`) en vez de
        devolverse como un cero.

        POR QUÉ EL CERO ERA CARO. El ancla reconstruye `StockDiario` hacia
        atrás, y `tuvo_stock` es EL DENOMINADOR de la demanda descensurada
        (`tasa_demanda_descensurada`) → d_avg / sigma_d → ROP, s_objetivo, el
        armado del contenedor y el newsvendor de temporada. Anclar en 0 deja
        `tuvo_stock=False` todos los días, la serie se lee como «agotado» y la
        demanda se censura HACIA ABAJO: se compra de menos.

        Y el guard que existía no podía verlo: `pct_negativo` mide una PROXY —
        solo dispara si el ancla queda demasiado BAJA y la serie se va a
        negativo. Un ancla en 0 sobre un SKU con stock real produce una serie
        plana en cero, indistinguible de un agotado legítimo.

        «NO HAY ANCLA» ES UN TERCER ESTADO, no un cero. Un saldo real de 0 y la
        ausencia de fila son hechos distintos y devolvían lo mismo; por eso la
        fuente sale en el retorno y no se infiere del valor.

        Returns: (saldo, fuente) — fuente 'STOCK_SIESA' o None si no hay ancla.
        """
        from app.models.stock_siesa import StockSiesa
        reg = StockSiesa.query.filter_by(
            bodega=bodega, codigo_siesa=referencia
        ).first()
        if reg:
            return float(reg.existencia or 0), 'STOCK_SIESA'
        return 0.0, None

    # ══════════════════════════════════════════════════════════════════════════
    # M0.3 — TSB (Teunter-Syntetos-Babai) para demanda intermitente
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
    def pronostico_tsb(ventana_meses: int = 12, alpha: float = 0.15,
                       solo_cuadrantes: list = None) -> dict:
        """
        Pronóstico TSB (Teunter-Syntetos-Babai) sobre demanda DESCENSURADA.

        TSB corrige el sesgo positivo de Croston actualizando la probabilidad de
        demanda en CADA periodo — también en los de demanda cero — en vez de
        solo cuando ocurre. Eso es lo que lo hace apto para SKUs que dejan de
        moverse: Croston se queda congelado en su última tasa, TSB decae.

            p_t = alpha_p * d_t + (1 - alpha_p) * p_{t-1}      (d_t = 1 si hubo demanda)
            z_t = alpha_z * y_t + (1 - alpha_z) * z_{t-1}      (solo si hubo demanda)
            pronostico = p_t * z_t

        CABLE M0.2 -> M0.3: la serie entra descensurada y en rejilla semanal
        regular. Con ventas crudas el modelo aprende que un SKU agotado "no se
        vendía", que es la misma censura que rompía el ROP.

        COMPUERTA (spec §2.M0.3): TSB debe ganarle a la media móvil de 8 semanas
        en MASE. Mientras no la pase, su salida NO alimenta sigma_d del ROP.

        Returns: {total, backtest: {...}, pronosticos: [...]}
        """
        if solo_cuadrantes is None:
            solo_cuadrantes = ['INTERMITENTE', 'GRUMOSA']

        series = KardexService.serie_semanal_descensurada(ventana_meses)

        clasificacion = KardexService.clasificar_syntetos_boylan(ventana_meses)
        refs_objetivo = {
            c['referencia'] for c in clasificacion['clasificacion']
            if c['cuadrante'] in solo_cuadrantes
        }

        # Costo unitario para ponderar por importancia económica — de la
        # jerarquía de costo (`resolver_costos`), no de `Producto.precio_compra`,
        # que ningún sync puebla: con él todos los pesos valían 0 y el tamiz
        # «gana donde hay plata» se evaluaba sobre una suma de ceros.
        from app.services.costo_service import resolver_costos
        _costos = resolver_costos(sorted(refs_objetivo)) if refs_objetivo else {}
        costos = {r: float(c.get('costo') or 0) for r, c in _costos.items()}
        sin_costo_peso = sorted(r for r in refs_objetivo if costos.get(r, 0) <= 0)

        pronosticos = []
        tsb_gana = 0
        total_evaluados = 0
        peso_gana = 0.0
        peso_total = 0.0

        def _tsb(serie_):
            z_ = next((v for v in serie_ if v > 0), 0.0)
            p_ = 1.0 if serie_ and serie_[0] > 0 else 0.5
            for y_ in serie_:
                hubo_ = 1.0 if y_ > 0 else 0.0
                p_ = alpha * hubo_ + (1 - alpha) * p_
                if hubo_:
                    z_ = alpha * y_ + (1 - alpha) * z_
            return p_, z_

        for ref in refs_objetivo:
            puntos = series.get(ref, [])
            # Rejilla completa hasta la semana actual: las semanas SIN demanda y
            # CON stock valen cero y cuentan —omitirlas es el sesgo que TSB
            # existe para evitar—. Las semanas sin stock y sin venta (`None`)
            # no se saben y no se usan: un agotado no es un «no se vendía».
            semanas = [v for _l, v in puntos if v is not None]
            if len(semanas) < 12:
                continue  # sin semanas suficientes no hay backtest honesto

            n_test = max(4, len(semanas) // 5)
            train, test = semanas[:-n_test], semanas[-n_test:]
            if len(train) < 8:
                continue

            # TSB sobre el train — solo para el backtest.
            p_train, z_train = _tsb(train)
            tsb_train = p_train * z_train
            # EL PRONÓSTICO sale de la serie ENTERA, hasta la semana actual.
            # Antes se publicaba el ajuste del train: el pronóstico ignoraba las
            # últimas n_test semanas, que son justo las que dicen si el SKU
            # dejó de moverse.
            p, z = _tsb(semanas)
            tsb_semanal = p * z

            # Croston: solo actualiza en periodos con demanda (sin decaimiento)
            zc = next((v for v in train if v > 0), 0.0)
            intervalo, cuenta = 1.0, 0
            for y in train:
                cuenta += 1
                if y > 0:
                    zc = alpha * y + (1 - alpha) * zc
                    intervalo = alpha * cuenta + (1 - alpha) * intervalo
                    cuenta = 0
            croston_semanal = zc / max(intervalo, 1.0)

            # Benchmark ingenuo: media móvil de las últimas 8 semanas del train
            mm8_semanal = sum(train[-8:]) / min(8, len(train))

            # MASE real — mismo denominador para ambos, así son comparables
            mase_tsb = KardexService.mase(test, tsb_train)
            mase_mm8 = KardexService.mase(test, mm8_semanal)

            # Peso economico: valor anual movido por ese SKU. Ganar en la cola
            # y perder en los pocos que sostienen el negocio no debe pasar —
            # la democracia entre SKUs es un promedio que esconde lo que importa.
            peso = sum(semanas) * float(costos.get(ref, 0) or 0)

            if mase_tsb is not None and mase_mm8 is not None:
                total_evaluados += 1
                peso_total += peso
                if mase_tsb < mase_mm8:
                    tsb_gana += 1
                    peso_gana += peso

            pronosticos.append({
                'referencia': ref,
                'tsb_semanal': round(tsb_semanal, 3),
                'tsb_diario': round(tsb_semanal / 7, 4),
                'tsb_mensual': round(tsb_semanal * 30 / 7, 1),
                'croston_semanal': round(croston_semanal, 3),
                'media_movil_8sem': round(mm8_semanal, 3),
                'p_probabilidad': round(p, 4),
                'z_tamano': round(z, 2),
                'semanas': len(semanas),
                'semanas_rejilla': len(puntos),
                'semanas_sin_dato': len(puntos) - len(semanas),
                'semanas_test': n_test,
                'mase_tsb': mase_tsb,
                'mase_mm8': mase_mm8,
                'tsb_mejor': (mase_tsb < mase_mm8)
                             if (mase_tsb is not None and mase_mm8 is not None) else None,
            })

        pronosticos.sort(key=lambda x: x['tsb_mensual'], reverse=True)

        pct = round(tsb_gana / total_evaluados * 100, 1) if total_evaluados else 0

        # Intervalo de Wilson al 95% sobre la proporción de victorias. Sin él,
        # un porcentaje es indistinguible del azar: con n=10 una moneda al aire
        # supera el 60% en el 37.7% de los intentos.
        lo, hi = _wilson(tsb_gana, total_evaluados)

        # TAMIZ, no compuerta. El MASE castiga a Croston/TSB por no adivinar
        # CUÁNDO llega el grumo — algo que nunca prometieron: estiman una TASA.
        # Un modelo que dice 1.6/sem frente a 0,0,0,40,0,0 se ve pésimo en MAE y
        # es correcto para efectos de inventario. Sirve para descartar lo
        # obviamente malo, no para dar permiso.
        pct_peso = round(peso_gana / peso_total * 100, 1) if peso_total > 0 else 0

        supera_tamiz = (total_evaluados >= TSB_N_MINIMO
                        and pct >= 60
                        and lo > 0.5           # el azar queda fuera del intervalo
                        and pct_peso >= 60)    # y gana donde hay plata, no solo en la cola

        return {
            'total': len(pronosticos),
            'alpha': alpha,
            'cuadrantes': solo_cuadrantes,
            'ventana_meses': ventana_meses,
            'demanda': 'DESCENSURADA, rejilla semanal (semanas sin venta = 0)',
            'tamiz_mase': {
                'es_compuerta': False,
                '_por_que_no': (
                    'MASE es un juez DEBIL para demanda intermitente: mide error punto '
                    'a punto y Croston/TSB no pronostican CUANDO llega el grumo — estiman '
                    'una tasa. Un pronostico de 1.6/sem contra la serie 0,0,0,40,0,0 se ve '
                    'pesimo en MAE y es correcto para inventario. Sirve para descartar lo '
                    'obviamente malo, no para dar permiso.'
                ),
                'metrica': 'MASE = MAE(pronostico) / MAE(naive un paso in-sample)',
                'evaluados': total_evaluados,
                'n_minimo': TSB_N_MINIMO,
                'tsb_gana': tsb_gana,
                'porcentaje_tsb_gana': pct,
                'ic95_victorias': [round(lo * 100, 1), round(hi * 100, 1)],
                'porcentaje_ponderado_por_valor': pct_peso,
                'valor_evaluado': round(peso_total),
                # Los que pesaron CERO por no tener costo de ninguna fuente: el
                # porcentaje ponderado no los ve, y eso se dice.
                'sin_costo_para_ponderar': len(sin_costo_peso),
                'azar_descartado': lo > 0.5,
                'supera_tamiz': supera_tamiz,
                'criterio': (
                    f'minimo {TSB_N_MINIMO} SKUs, TSB gana en >=60%, y el limite '
                    f'inferior del IC95 por encima del 50% (azar descartado)'
                ),
                'nota_ponderacion': (
                    'porcentaje_tsb_gana trata todos los SKUs por igual; '
                    'porcentaje_ponderado_por_valor pesa cada SKU por el valor anual que '
                    'mueve. El tamiz exige AMBOS >=60%: ganar en la cola y perder en los '
                    'pocos que sostienen el negocio no pasa.'
                ),
                'compuerta_real': (
                    'SIMULACION DE INVENTARIO sobre el kardex historico: aplicar ambas '
                    'politicas de ROP y comparar nivel de servicio contra capital '
                    'inmovilizado. Gana quien alcance el servicio objetivo con menos '
                    'inventario. Pendiente — no es ruta critica del comite.'
                ),
                'nota': 'Spec §2.M0.3 — TSB debe ganarle a la media movil de 8 semanas.',
            },
            'sigma_d_del_rop': (
                'Sigue con el estimador interino (sigma empirica descensurada). '
                'Solo cambia al RMSE del TSB cuando pase la SIMULACION DE INVENTARIO, '
                'no con el tamiz MASE.'
            ),
            'pronosticos': pronosticos,
        }

    def newsvendor(items_temporada: list, margen_pct: float = 0.40,
                   costo_exceso_pct: float = 0.60) -> dict:
        """
        Newsvendor: cantidad óptima de compra para temporada con demanda incierta.

        Q* = F⁻¹(ratio_critico), con ratio_critico = Cu / (Cu + Co) — UNA función,
        `ratio_critico()`, para la fila y para la cabecera (D12).

        El ratio por defecto (filas sin Cu/Co) sale de `ratio_critico_desde_tasas`:
        el margen es sobre PRECIO y el exceso sobre COSTO, así que no se suman
        directo — ver esa función.

        Usa distribución empírica de temporadas pasadas. Si solo hay 1 temporada,
        infla la incertidumbre multiplicando σ × 1.5 (factor de ignorancia).

        DEADLINE: 7 de agosto 2026 — decisión del pedido escolar.

        Args:
            items_temporada: [{referencia, ventas_pasadas: [v1, v2, ...], costo_unitario}]
                ventas_pasadas = unidades vendidas en cada temporada (mín 1)
            margen_pct: margen bruto como fracción del precio (default 40%)
            costo_exceso_pct: % del costo que se pierde si sobra (default 60% — liquidación)

        Returns: {ratio_critico, items: [{referencia, q_optimo, demanda_esperada, ...}]}
        """
        import math

        if not items_temporada:
            return {'error': 'Se requiere al menos un item con ventas_pasadas'}

        ratio_defecto = ratio_critico_desde_tasas(margen_pct, costo_exceso_pct)

        resultados = []

        for item in items_temporada:
            ref = item.get('referencia', '???')
            ventas = item.get('ventas_pasadas', [])
            costo = item.get('costo_unitario', 0)

            if not ventas:
                resultados.append({
                    'referencia': ref,
                    'error': 'Sin datos de temporadas pasadas',
                })
                continue

            # Cu/Co por SKU si vienen: el ratio crítico real depende del margen
            # y del costo de exceso DE ESE producto, no de un promedio global.
            # Cu = margen que se pierde si falta. Co = lo que cuesta que sobre.
            cu = item.get('cu')
            co = item.get('co')
            ratio_item = ratio_critico(cu, co)
            if ratio_item is None:
                ratio_item = ratio_defecto

            n_temporadas = len(ventas)
            mu = sum(ventas) / n_temporadas
            if n_temporadas > 1:
                sigma = math.sqrt(sum((v - mu) ** 2 for v in ventas) / (n_temporadas - 1))
                distribucion = f'Empirica ({n_temporadas} temporadas)'
                incertidumbre = 'MEDIA' if n_temporadas < 4 else 'BAJA'
            else:
                # Una observacion NO es una distribucion. Con n=1 la empirica
                # colapsaria a la observacion misma y el ratio critico dejaria de
                # tener efecto: el modelo diria "pide lo que vendiste", que es la
                # heuristica que vino a reemplazar. Se usa Normal con sigma
                # supuesto e inflado a proposito.
                sigma = mu * 0.30 * 1.5  # CV asumido 30%, inflado x1.5
                distribucion = 'Normal inflada (1 temporada, CV 30% x1.5)'
                incertidumbre = 'ALTA'

            # Q* via aproximación normal: Q* = mu + z_cr * sigma
            # z_cr = inversa de la normal estándar del ratio crítico
            z_cr = _norm_ppf(ratio_item)
            q_optimo = max(0, round(mu + z_cr * sigma))

            # Banda de sensibilidad al ratio crítico (±10 puntos).
            # Protege al comité de una pelea de parámetros: muestra el rango sin
            # que nadie tenga que discutir si la tasa de capital es 30% o 15%.
            cr_bajo = max(0.01, ratio_item - 0.10)
            cr_alto = min(0.99, ratio_item + 0.10)
            q_cr_bajo = max(0, round(mu + _norm_ppf(cr_bajo) * sigma))
            q_cr_alto = max(0, round(mu + _norm_ppf(cr_alto) * sigma))

            # Rango de confianza 80%
            q_bajo = max(0, round(mu + _norm_ppf(0.10) * sigma))
            q_alto = max(0, round(mu + _norm_ppf(0.90) * sigma))

            resultados.append({
                'referencia': ref,
                'q_optimo': q_optimo,
                'demanda_esperada': round(mu, 1),
                'sigma': round(sigma, 1),
                'n_temporadas': n_temporadas,
                'distribucion': distribucion,
                'incertidumbre': incertidumbre,
                'ratio_critico': round(ratio_item, 3),
                'cu': cu,
                'co': co,
                'z_critico': round(z_cr, 3),
                'rango_80': [q_bajo, q_alto],
                # Sensibilidad al ratio crítico: qué cambia si la política de
                # tasas fuera 10 puntos distinta, en unidades y en pesos
                'sensibilidad_cr': {
                    'cr_menos_10': {'cr': round(cr_bajo, 3), 'q': q_cr_bajo,
                                    'inversion': round(q_cr_bajo * costo) if costo else None},
                    'cr_base': {'cr': round(ratio_item, 3), 'q': q_optimo,
                                'inversion': round(q_optimo * costo) if costo else None},
                    'cr_mas_10': {'cr': round(cr_alto, 3), 'q': q_cr_alto,
                                  'inversion': round(q_cr_alto * costo) if costo else None},
                    'exposicion_pesos': round((q_cr_alto - q_cr_bajo) * costo) if costo else None,
                },
                'costo_unitario': costo,
                'inversion_optima': round(q_optimo * costo) if costo else None,
                'advertencia_1_temporada': n_temporadas == 1,
            })

        resultados.sort(key=lambda x: x.get('inversion_optima') or 0, reverse=True)

        def _inv(clave):
            return sum((r.get('sensibilidad_cr') or {}).get(clave, {}).get('inversion') or 0
                       for r in resultados)

        return {
            # El ratio de las filas que NO traen Cu/Co. Las que lo traen usan
            # el suyo, con la misma función.
            'ratio_critico': round(ratio_defecto, 3),
            'ratio_critico_es': 'POR_DEFECTO_PARA_FILAS_SIN_CU_CO',
            'margen_pct': margen_pct,
            'costo_exceso_pct': costo_exceso_pct,
            'total_items': len(resultados),
            'total_inversion': sum(r.get('inversion_optima') or 0 for r in resultados),
            # Banda agregada: cuánta plata está en juego por la POLÍTICA de
            # tasas, no por la demanda. Se ratifica antes de correr el modelo.
            'banda_sensibilidad': {
                'inversion_cr_menos_10': _inv('cr_menos_10'),
                'inversion_base': _inv('cr_base'),
                'inversion_cr_mas_10': _inv('cr_mas_10'),
                'exposicion_pesos': _inv('cr_mas_10') - _inv('cr_menos_10'),
                'nota': ('Cu y Co NO son hechos: son políticas (tasa de capital y de '
                         'liquidación). Ratificarlas por escrito ANTES de correr el '
                         'modelo — si se fijan después de ver los números, el modelo '
                         'deja de ser juez y se vuelve espejo.'),
            },
            'items_1_temporada': sum(1 for r in resultados if r.get('advertencia_1_temporada')),
            'nota': (
                'Q* = cantidad óptima que maximiza utilidad esperada bajo incertidumbre. '
                'ratio_critico = Cu/(Cu+Co); por defecto m/(m + e·(1−m)), con m '
                'margen sobre precio y e exceso sobre costo. '
                'Items con 1 sola temporada tienen sigma inflado ×1.5 por factor de ignorancia.'
            ),
            'items': resultados,
        }


def ratio_critico(cu, co):
    """Cu / (Cu + Co). `None` si falta alguno o la suma no es positiva.

    La única fórmula del ratio crítico del sistema: la usan la fila del
    newsvendor, su cabecera y el pedido de temporada.
    """
    if cu is None or co is None:
        return None
    cu, co = float(cu), float(co)
    if cu + co <= 0:
        return None
    return cu / (cu + co)


def ratio_critico_desde_tasas(margen_sobre_precio, exceso_sobre_costo):
    """El ratio crítico cuando solo se conocen tasas, no pesos (D12).

    `margen_sobre_precio` m está en base PRECIO; `exceso_sobre_costo` e en base
    COSTO. Con precio p y costo c = p·(1−m):

        Cu = p − c = m·p          Co = e·c = e·(1−m)·p
        ratio = m / (m + e·(1−m))

    El POST /newsvendor hacía m / (m + e): sumaba una fracción del precio con
    una del costo. Con m = 0,40 y e = 0,60 daba 0,400 en vez de 0,526, y la
    cabecera del pedido de temporada mostraba un ratio que ninguna fila usaba.
    """
    m, e = float(margen_sobre_precio), float(exceso_sobre_costo)
    if not 0 <= m < 1:
        raise ValueError(f'margen_sobre_precio debe estar en [0,1), llegó {m}')
    return ratio_critico(m, e * (1 - m)) or 0.0


def _norm_ppf(p):
    """Aproximación de la inversa de la normal estándar (Abramowitz & Stegun).
    Suficiente para ratio_critico — no necesitamos scipy."""
    import math
    if p <= 0:
        return -4.0
    if p >= 1:
        return 4.0
    if p == 0.5:
        return 0.0

    if p < 0.5:
        t = math.sqrt(-2 * math.log(p))
    else:
        t = math.sqrt(-2 * math.log(1 - p))

    # Coeficientes Abramowitz & Stegun 26.2.23
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308

    z = t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)

    return z if p >= 0.5 else -z
