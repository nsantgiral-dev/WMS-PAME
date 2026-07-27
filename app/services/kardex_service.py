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
            fecha_hasta = date.today().strftime('%Y%m%d')

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
        # que se rinde justo donde nadie mira.
        MAX_MINUTOS = int(max_minutos or os.environ.get('KARDEX_MAX_MINUTOS', '25'))

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
                # distribución. Se buscan los nombres plausibles.
                if total_declarado is None:
                    for k in ('totalRegistros', 'TotalRegistros', 'total',
                              'Total', 'totalFilas', 'cantidadRegistros'):
                        v = (res or {}).get(k) or detalle.get(k)
                        if v not in (None, ''):
                            try:
                                total_declarado = int(v)
                                logger.info('[KARDEX] Siesa declara %d registros', total_declarado)
                            except (TypeError, ValueError):
                                pass
                            break

                # Log de descubrimiento: mostrar keys del primer registro
                if pagina == 1 and rows:
                    first = rows[0] if isinstance(rows[0], dict) else {}
                    logger.info('[KARDEX] Keys del primer registro: %s', list(first.keys()))
                    logger.info('[KARDEX] Primer registro completo: %s', first)
                if not rows:
                    if pagina == max(1, int(pagina_inicial or 1)):
                        estado = 'SIN_DATOS'
                        detalle_estado = 'La primera página vino vacía — ¿credenciales o consulta?'
                    break

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    ref = (row.get('f120_referencia') or '').strip()
                    bodega = (row.get('f150_id') or row.get('f470_id_bodega') or '').strip()
                    if not ref or not bodega:
                        continue

                    # Fecha: puede venir como 'f450_id_fecha ' (con espacio) o ISO format
                    fecha_str = str(
                        row.get('f450_id_fecha ') or row.get('f450_id_fecha') or
                        row.get('f350_fecha') or ''
                    ).strip()
                    try:
                        # Soporta YYYYMMDD y YYYY-MM-DDTHH:MM:SS
                        fecha = datetime.strptime(fecha_str[:10], '%Y-%m-%d').date() if '-' in fecha_str else datetime.strptime(fecha_str[:8], '%Y%m%d').date()
                    except (ValueError, TypeError):
                        errores += 1
                        continue

                    # Filtro por rango de fechas (en Python, no en Connekta)
                    if fecha < fecha_desde_dt or fecha > fecha_hasta_dt:
                        filtrados += 1
                        continue

                    # Naturaleza: Connekta devuelve 'Entrada'/'Salida' (string), no 1/2
                    nat_raw = row.get('f470_ind_naturaleza', '')
                    if isinstance(nat_raw, str):
                        naturaleza = 1 if 'ntrada' in nat_raw else 2  # Entrada=1, Salida=2
                    else:
                        naturaleza = int(nat_raw) if nat_raw else 0

                    # Clave natural del documento, si viene. Los nombres varían
                    # entre consultas, así que se prueban los plausibles.
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
                    concepto = int(row.get('f470_id_concepto', 0))
                    cantidad = abs(float(row.get('f470_cant_base', 0)))

                    h = hash_movimiento(fecha, tipo_docto, bodega, ref, concepto,
                                        naturaleza, cantidad, consec, nro_reg)
                    # Idempotencia: reanudar o repetir la descarga NO duplica.
                    if h in vistos_lote or h in vistos_bd:
                        duplicados += 1
                        continue
                    vistos_lote.add(h)

                    mov = KardexMovimiento(
                        fecha=fecha,
                        tipo_docto=tipo_docto,
                        bodega=bodega,
                        referencia=ref,
                        concepto=concepto,
                        naturaleza=naturaleza,
                        cantidad=cantidad,
                        costo_promedio=float(row.get('f470_costo_prom_uni', 0)),
                        consec_docto=consec or None,
                        nro_registro=nro_reg,
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

                if len(rows) < 100:
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
            return [hash_movimiento(
                None, r.get('f350_id_tipo_docto'), r.get('f470_id_bodega') or r.get('f150_id'),
                r.get('f120_referencia'), r.get('f470_id_concepto'),
                r.get('f470_ind_naturaleza'), r.get('f470_cant_base'),
                nro_registro=r.get('LineaRegistro'),
            ) for r in rows if isinstance(r, dict)]

        base = _firmas(_traer(pagina))
        if not base:
            return {'veredicto': None, 'error': f'La página {pagina} vino vacía.'}

        # 1) Intervalo CORTO — distingue deriva de no-determinismo
        time.sleep(5)
        corto = _firmas(_traer(pagina))
        iguales_corto = sum(1 for a, b in zip(base, corto) if a == b)

        # 2) Página CONSECUTIVA — ¿se solapan páginas contiguas?
        vecina = _firmas(_traer(pagina + 1))
        solape = len(set(base) & set(vecina))

        # 3) Intervalo LARGO
        time.sleep(max(1, int(espera_s)))
        largo = _firmas(_traer(pagina))
        iguales_largo = sum(1 for a, b in zip(base, largo) if a == b)

        n = len(base)
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

        Returns: {referencias_procesadas, dias_generados}
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
        UMBRAL_NEGATIVO = 0.10  # 10% — por encima, dato insuficiente

        from collections import defaultdict

        for ref, bod in combos:
            saldo_actual = KardexService._obtener_saldo_actual(ref, bod)

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

            saldo = float(saldo_actual)
            fechas_ordenadas = sorted(dias_mov.keys(), reverse=True)
            dias_negativos = 0
            total_dias = len(fechas_ordenadas)

            for fecha in fechas_ordenadas:
                stock_cierre = saldo

                # Marcar saldos negativos (dato podrido — no corregir, solo marcar)
                if stock_cierre < 0:
                    dias_negativos += 1

                existing = StockDiario.query.filter_by(
                    referencia=ref, bodega=bod, fecha=fecha
                ).first()
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

                for m in dias_mov[fecha]:
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
            '%d con saldos negativos (%d dato insuficiente)',
            refs_procesadas, dias_generados, len(calidad), datos_insuficientes
        )

        return {
            'referencias_procesadas': refs_procesadas,
            'dias_generados': dias_generados,
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

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        # ── 1. Reconciliación en UNIDADES × mes × bodega ──────────
        ventas_mes_bodega = (
            db.session.query(
                func.to_char(KardexMovimiento.fecha, 'YYYY-MM').label('mes'),
                KardexMovimiento.bodega,
                func.sum(KardexMovimiento.cantidad).label('unidades'),
                func.count().label('registros'),
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)
            .group_by(
                func.to_char(KardexMovimiento.fecha, 'YYYY-MM'),
                KardexMovimiento.bodega,
            )
            .order_by(
                func.to_char(KardexMovimiento.fecha, 'YYYY-MM'),
                KardexMovimiento.bodega,
            )
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
                'mes': r.mes,
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

        Demanda neta = ventas(501) - devoluciones(502) por SKU.
        Denominador = solo días donde tuvo_stock=True.
        NO es "demanda real" — es demanda SERVIDA corregida por quiebres.
        La cobertura de rutas sigue siendo parámetro exógeno.

        Args:
            ventana_meses: ventana móvil (default 12)
            nivel: 'bodega' para reposición por nodo, 'red' para clasificación S-B

        Returns: {total_skus, con_demanda, sin_demanda, tasas: [...]}
        """
        from sqlalchemy import func

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

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        if nivel == 'red':
            # Clasificación: agregar toda la red, no por bodega
            group_key = KardexMovimiento.referencia
            stock_group_key = StockDiario.referencia
        else:
            # Reposición: por bodega
            group_key = KardexMovimiento.referencia + '|' + KardexMovimiento.bodega
            stock_group_key = StockDiario.referencia + '|' + StockDiario.bodega

        # Días con stock
        dias_stock = dict(
            db.session.query(stock_group_key, func.count())
            .filter(StockDiario.fecha >= fecha_limite)
            .filter(StockDiario.tuvo_stock == True)
            .group_by(stock_group_key)
            .all()
        )

        # Ventas brutas (501, salidas)
        ventas = dict(
            db.session.query(group_key, func.sum(KardexMovimiento.cantidad))
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)
            .group_by(group_key)
            .all()
        )

        # Devoluciones (502, entradas — restan demanda)
        devoluciones = dict(
            db.session.query(group_key, func.sum(KardexMovimiento.cantidad))
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_DEVOLUCION))
            .filter(KardexMovimiento.naturaleza == 1)
            .group_by(group_key)
            .all()
        )

        tasas = []
        for key, dias in dias_stock.items():
            venta_bruta = float(ventas.get(key, 0))
            devolucion = float(devoluciones.get(key, 0))
            demanda_neta = max(venta_bruta - devolucion, 0)
            dias_int = int(dias)
            tasa = round(demanda_neta / dias_int, 4) if dias_int > 0 else 0

            entry = {
                'referencia': key.split('|')[0] if '|' in key else key,
                'demanda_bruta': venta_bruta,
                'devoluciones': devolucion,
                'demanda_neta': demanda_neta,
                'dias_con_stock': dias_int,
                'tasa_servida_corregida': tasa,
                'velocity_cero': demanda_neta == 0,
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
            'fecha_limite': fecha_limite.isoformat(),
            'nota': (
                'tasa_servida_corregida = demanda servida corregida por quiebres. '
                'NO es demanda real — la cobertura de rutas es parámetro exógeno.'
            ),
            'tasas': tasas,
        }

    @staticmethod
    def demanda_descensurada(ventana_meses: int = 12, nivel: str = 'red') -> dict:
        """
        M0.2 → M0.4. Demanda diaria descensurada POR SKU, con su sigma.

        Este es el cable que faltaba: el ROP calculaba d_avg = total / 365 días
        calendario, metiendo los días sin stock en el denominador con demanda
        cero. Un SKU agotado 62 de 365 días quedaba subestimado ~20%, el ROP
        bajaba, se agotaba más, y la estimación bajaba otra vez. Bucle endógeno:
        el sistema aprendía a comprar poco de lo que siempre faltó.

        UNIDAD CANÓNICA: día. Todo lo que salga de aquí es unidades/día.

        sigma_d se calcula sobre los días CON stock, contando como demanda cero
        los días con stock y sin venta — que es lo que hace grumosa a la demanda
        rural. Con suma y suma de cuadrados sobre los días con movimiento basta:
        los días en cero no aportan ni a una ni a otra.

            media = suma / N
            var   = (suma_cuadrados - N * media^2) / (N - 1)

        Returns: {referencia: {d_avg, sigma_d, dias_con_stock, dias_ventana,
                               demanda_neta, factor_censura}}
        """
        from sqlalchemy import func

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)
        dias_ventana = (date.today() - fecha_limite).days

        if nivel == 'red':
            k_kardex = KardexMovimiento.referencia
            k_stock = StockDiario.referencia
        else:
            k_kardex = KardexMovimiento.referencia + '|' + KardexMovimiento.bodega
            k_stock = StockDiario.referencia + '|' + StockDiario.bodega

        dias_stock = dict(
            db.session.query(k_stock, func.count())
            .filter(StockDiario.fecha >= fecha_limite)
            .filter(StockDiario.tuvo_stock == True)  # noqa: E712
            .group_by(k_stock)
            .all()
        )

        def _por_dia(conceptos, naturaleza):
            return (
                db.session.query(k_kardex, KardexMovimiento.fecha,
                                 func.sum(KardexMovimiento.cantidad))
                .filter(KardexMovimiento.fecha >= fecha_limite)
                .filter(KardexMovimiento.concepto.in_(conceptos))
                .filter(KardexMovimiento.naturaleza == naturaleza)
                .group_by(k_kardex, KardexMovimiento.fecha)
                .all()
            )

        # Demanda neta por (sku, día): ventas menos devoluciones del mismo día
        neto = defaultdict(float)
        for key, fecha, cant in _por_dia(CONCEPTOS_VENTA, 2):
            neto[(key, fecha)] += float(cant or 0)
        for key, fecha, cant in _por_dia(CONCEPTOS_DEVOLUCION, 1):
            neto[(key, fecha)] -= float(cant or 0)

        acum = defaultdict(lambda: {'suma': 0.0, 'suma_cuad': 0.0, 'dias_mov': 0})
        for (key, _fecha), valor in neto.items():
            v = max(valor, 0.0)
            if v <= 0:
                continue
            a = acum[key]
            a['suma'] += v
            a['suma_cuad'] += v * v
            a['dias_mov'] += 1

        salida = {}
        for key, a in acum.items():
            # Política única — ver dias_expuestos()
            n, censurado = dias_expuestos(dias_stock.get(key, 0), dias_ventana)
            if n <= 0:
                continue

            media = a['suma'] / n
            if n > 1:
                var = (a['suma_cuad'] - n * media * media) / (n - 1)
            else:
                var = 0.0
            sigma = math.sqrt(var) if var > 0 else 0.0

            # Cuánto sube la estimación por corregir la censura. 1.0 = nunca
            # se agotó; 1.25 = se estimaba 25% por debajo.
            factor = (dias_ventana / n) if n > 0 else 1.0

            salida[key] = {
                'd_avg': round(media, 6),
                'sigma_d': round(sigma, 6),
                'dias_con_stock': n,
                'dias_ventana': dias_ventana,
                'demanda_neta': round(a['suma'], 2),
                'factor_censura': round(factor, 4),
                # False = descensurado de verdad. True = no había StockDiario y
                # el número quedó censurado (subestima). Reconstruir stock diario.
                'censurado': censurado,
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

        Rejilla regular (semanas ISO) en vez de eventos irregulares. Es lo que
        hace posible un MASE de verdad: el naive de un paso necesita periodos
        consecutivos comparables, y "el evento anterior" no lo es cuando los
        intervalos varían.

        Cada semana se descensura con sus PROPIOS días con stock: una semana en
        que el SKU estuvo agotado 4 de 7 días vendió lo que pudo en 3, y esa
        tasa proyectada a 7 es la demanda que hubo.

        Returns: {referencia: [(lunes, valor_descensurado), ...]} ordenado.
        """
        from sqlalchemy import func

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        def _lunes(f):
            return f - timedelta(days=f.weekday())

        def _por_dia(conceptos, naturaleza):
            return (
                db.session.query(KardexMovimiento.referencia,
                                 KardexMovimiento.fecha,
                                 func.sum(KardexMovimiento.cantidad))
                .filter(KardexMovimiento.fecha >= fecha_limite)
                .filter(KardexMovimiento.concepto.in_(conceptos))
                .filter(KardexMovimiento.naturaleza == naturaleza)
                .group_by(KardexMovimiento.referencia, KardexMovimiento.fecha)
                .all()
            )

        neto = defaultdict(float)
        for ref, f, c in _por_dia(CONCEPTOS_VENTA, 2):
            neto[(ref, f)] += float(c or 0)
        for ref, f, c in _por_dia(CONCEPTOS_DEVOLUCION, 1):
            neto[(ref, f)] -= float(c or 0)

        # Días con stock por (ref, semana) — el denominador de cada semana
        dias_stock = defaultdict(int)
        for ref, f in (db.session.query(StockDiario.referencia, StockDiario.fecha)
                       .filter(StockDiario.fecha >= fecha_limite)
                       .filter(StockDiario.tuvo_stock == True).all()):  # noqa: E712
            dias_stock[(ref, _lunes(f))] += 1

        crudo = defaultdict(float)
        for (ref, f), v in neto.items():
            if not ref:
                continue
            crudo[(ref.strip(), _lunes(f))] += max(v, 0.0)

        series = defaultdict(list)
        for (ref, semana), valor in crudo.items():
            n, _cens = dias_expuestos(dias_stock.get((ref, semana), 0), 7)
            series[ref].append((semana, round(valor * (7.0 / n), 4)))

        return {ref: sorted(puntos) for ref, puntos in series.items()}

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
        2. Estacionales se EXCLUYEN — se leen de tabla producto_clasificacion_abc
           (campo rol='ESTACIONAL') + parámetro extra para override
        3. La clasificación PROPONE sentencias, no ejecuta bloqueos automáticos
        4. Si un constitucional cae en 'grumosa', es señal de alarma

        Cuadrantes:
        - Suave: ADI ≤ 1.32 y CV² ≤ 0.49 → reposición automática
        - Errática: ADI ≤ 1.32 y CV² > 0.49 → colchón + revisión mensual
        - Intermitente: ADI > 1.32 y CV² ≤ 0.49 → mín-máx simple
        - Grumosa: ADI > 1.32 y CV² > 0.49 → PROPUESTA cola/remate

        Returns: {clasificacion: [...], resumen: {...}, alertas: [...]}
        """
        from sqlalchemy import func

        # Leer estacionales de tabla persistida (no parámetro volátil)
        estacionales_set = set()
        try:
            from app.models.producto import Producto
            from app.models.producto_clasificacion_abc import ProductoClasificacionABC
            est_db = (
                db.session.query(Producto.codigo_siesa)
                .join(ProductoClasificacionABC, Producto.id == ProductoClasificacionABC.producto_id)
                .filter(ProductoClasificacionABC.clasificacion == 'ESTACIONAL')
                .all()
            )
            estacionales_set = {r[0].strip() for r in est_db if r[0]}
        except Exception as e:
            logger.warning('[KARDEX] No se pudo leer estacionales de DB: %s', e)

        # Override: estacionales adicionales pasados explícitamente
        if estacionales_extra:
            estacionales_set.update(estacionales_extra)

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)
        dias_ventana = ventana_meses * 30

        # Demanda diaria por SKU a nivel RED (agregando todas las bodegas)
        demanda_diaria = (
            db.session.query(
                KardexMovimiento.referencia,
                KardexMovimiento.fecha,
                func.sum(KardexMovimiento.cantidad).label('cantidad')
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)  # salidas
            .group_by(KardexMovimiento.referencia, KardexMovimiento.fecha)
            .all()
        )

        # Agrupar por referencia: lista de cantidades por día con demanda
        from collections import defaultdict
        ref_demandas = defaultdict(list)  # ref → [(fecha, cantidad)]
        for row in demanda_diaria:
            ref = row.referencia.strip() if row.referencia else ''
            if not ref or ref in estacionales_set:
                continue
            ref_demandas[ref].append((row.fecha, float(row.cantidad)))

        # Días con stock a nivel RED (al menos una bodega con stock)
        dias_stock_red = dict(
            db.session.query(
                StockDiario.referencia,
                func.count(func.distinct(StockDiario.fecha))
            )
            .filter(StockDiario.fecha >= fecha_limite)
            .filter(StockDiario.tuvo_stock == True)
            .group_by(StockDiario.referencia)
            .all()
        )

        # Calcular ADI y CV² por SKU
        ADI_CORTE = 1.32
        CV2_CORTE = 0.49

        clasificacion = []
        alertas = []

        for ref, eventos in ref_demandas.items():
            dias_con_demanda = len(eventos)
            cantidades = [e[1] for e in eventos]
            # Política única — la misma que usa la descensura (ver dias_expuestos)
            dias_stock, sb_censurado = dias_expuestos(
                dias_stock_red.get(ref, 0), dias_ventana)

            if dias_con_demanda == 0:
                continue

            # ADI: días promedio entre demandas (sobre días con stock, no calendario)
            adi = dias_stock / dias_con_demanda if dias_con_demanda > 0 else 999

            # CV²: varianza relativa del tamaño de cada demanda
            media = sum(cantidades) / len(cantidades) if cantidades else 0
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
                'demanda_total': sum(cantidades),
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

        # Alertas: constitucionales que caen en grumosa (dato censurado o rol incorrecto)
        # (Las referencias constitucionales se pasan externamente para cruce)

        clasificacion.sort(key=lambda x: ('SUAVE ERRATICA INTERMITENTE GRUMOSA'.split().index(x['cuadrante']), -x['demanda_total']))

        return {
            'total_clasificados': len(clasificacion),
            'estacionales_excluidos': len(estacionales_set),
            'resumen': resumen,
            'nota': (
                'Clasificación a nivel de RED (todas las bodegas agregadas). '
                'Estacionales excluidos — evaluar dentro de su ventana. '
                'Cuadrante GRUMOSA es PROPUESTA, no bloqueo automático.'
            ),
            'clasificacion': clasificacion,
        }

    @staticmethod
    def _obtener_saldo_actual(referencia: str, bodega: str) -> float:
        """Obtiene saldo actual de stock_siesa o ubicacion_producto."""
        from app.models.stock_siesa import StockSiesa
        reg = StockSiesa.query.filter_by(
            bodega=bodega, codigo_siesa=referencia
        ).first()
        if reg:
            return float(reg.existencia or 0)
        return 0

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

        # Costo unitario para ponderar por importancia economica
        from app.models.producto import Producto
        costos = dict(
            db.session.query(Producto.codigo_siesa, Producto.precio_compra)
            .filter(Producto.codigo_siesa.isnot(None)).all()
        )

        pronosticos = []
        tsb_gana = 0
        total_evaluados = 0
        peso_gana = 0.0
        peso_total = 0.0

        for ref in refs_objetivo:
            puntos = series.get(ref, [])
            if len(puntos) < 12:
                continue  # sin semanas suficientes no hay backtest honesto

            # Rejilla completa: las semanas SIN demanda valen cero y cuentan.
            # Omitirlas es exactamente el sesgo que TSB existe para evitar.
            inicio, fin = puntos[0][0], puntos[-1][0]
            mapa = dict(puntos)
            semanas, cur = [], inicio
            while cur <= fin:
                semanas.append(mapa.get(cur, 0.0))
                cur += timedelta(days=7)

            if len(semanas) < 12:
                continue

            n_test = max(4, len(semanas) // 5)
            train, test = semanas[:-n_test], semanas[-n_test:]
            if len(train) < 8:
                continue

            # TSB sobre el train
            z = next((v for v in train if v > 0), 0.0)
            p = 1.0 if train and train[0] > 0 else 0.5
            for y in train:
                hubo = 1.0 if y > 0 else 0.0
                p = alpha * hubo + (1 - alpha) * p
                if hubo:
                    z = alpha * y + (1 - alpha) * z
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
            mase_tsb = KardexService.mase(test, tsb_semanal)
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

        Q* = F⁻¹(ratio_critico) donde ratio_critico = margen / (margen + costo_exceso)

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

        ratio_critico = margen_pct / (margen_pct + costo_exceso_pct)

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
            if cu is not None and co is not None and (float(cu) + float(co)) > 0:
                ratio_item = float(cu) / (float(cu) + float(co))
            else:
                ratio_item = ratio_critico

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
            'ratio_critico': round(ratio_critico, 3),
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
                'ratio_critico = margen/(margen+costo_exceso). '
                'Items con 1 sola temporada tienen sigma inflado ×1.5 por factor de ignorancia.'
            ),
            'items': resultados,
        }


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
