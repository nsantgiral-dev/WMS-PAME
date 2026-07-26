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

    __table_args__ = (
        db.Index('ix_kardex_ref_bod_fecha', 'referencia', 'bodega', 'fecha'),
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


class KardexService:

    @staticmethod
    def descargar_kardex(fecha_desde: str, fecha_hasta: str = None) -> dict:
        """
        Descarga movimientos del kardex de Siesa via consulta dinámica.

        Args:
            fecha_desde: YYYYMMDD — inicio del período
            fecha_hasta: YYYYMMDD — fin (default: hoy)

        Returns: {total_descargados, paginas, errores}
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
        pagina = 1
        errores = 0
        filtrados = 0
        inicio = datetime.utcnow()
        MAX_MINUTOS = 30

        while True:
            elapsed = (datetime.utcnow() - inicio).total_seconds()
            if elapsed > MAX_MINUTOS * 60:
                logger.warning('[KARDEX] Descarga abortada tras %.0fs', elapsed)
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

                # Log de descubrimiento: mostrar keys del primer registro
                if pagina == 1 and rows:
                    first = rows[0] if isinstance(rows[0], dict) else {}
                    logger.info('[KARDEX] Keys del primer registro: %s', list(first.keys()))
                    logger.info('[KARDEX] Primer registro completo: %s', first)
                if not rows:
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

                    mov = KardexMovimiento(
                        fecha=fecha,
                        tipo_docto=(row.get('f350_id_tipo_docto') or '').strip(),
                        bodega=bodega,
                        referencia=ref,
                        concepto=int(row.get('f470_id_concepto', 0)),
                        naturaleza=naturaleza,
                        cantidad=abs(float(row.get('f470_cant_base', 0))),
                        costo_promedio=float(row.get('f470_costo_prom_uni', 0)),
                    )
                    db.session.add(mov)
                    total += 1

                db.session.commit()
                logger.info('[KARDEX] Página %d: %d movimientos', pagina, len(rows))

                if len(rows) < 100:
                    break

                pagina += 1

                # Throttle: respiro entre páginas
                import time
                time.sleep(float(os.environ.get('KARDEX_PAGE_DELAY_S', '1')))

            except Exception as e:
                logger.error('[KARDEX] Error página %d: %s', pagina, e)
                errores += 1
                db.session.rollback()
                break

        logger.info(
            '[KARDEX] Descarga completa: %d movimientos, %d páginas, %d errores',
            total, pagina, errores
        )
        return {'total_descargados': total, 'paginas': pagina, 'errores': errores}

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
            # Días con stock = denominador descensurado. No puede ser menor que
            # los días en que hubo movimiento (no se vende sin existencias):
            # si lo es, el StockDiario está incompleto y mandan los movimientos.
            n = max(int(dias_stock.get(key, 0)), a['dias_mov'])
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
            }

        return salida

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
            dias_stock = dias_stock_red.get(ref, dias_ventana)

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
        Pronóstico TSB para items con demanda intermitente/grumosa.

        TSB es Croston mejorado — corrige el sesgo positivo de Croston
        usando suavizamiento exponencial separado para:
          p = intervalo entre demandas (períodos)
          z = tamaño de la demanda cuando ocurre

        Pronóstico TSB = (z / p) * (1 - alpha/2)
        La corrección (1 - alpha/2) es lo que distingue TSB de Croston.

        Spec §2.M0.3: TSB debe ganar a media móvil 8 sem en MASE.

        Args:
            ventana_meses: ventana de datos (default 12)
            alpha: parámetro de suavizamiento (0.05-0.30, default 0.15)
            solo_cuadrantes: filtrar por cuadrantes S-B (default: INTERMITENTE+GRUMOSA)

        Returns: {total, pronosticos: [{referencia, tsb, croston, media_movil_8, mase_tsb, mase_mm8}]}
        """
        from collections import defaultdict
        from sqlalchemy import func

        if solo_cuadrantes is None:
            solo_cuadrantes = ['INTERMITENTE', 'GRUMOSA']

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        # Obtener demanda diaria agregada a RED (todas las bodegas)
        demanda_diaria = (
            db.session.query(
                KardexMovimiento.referencia,
                KardexMovimiento.fecha,
                func.sum(KardexMovimiento.cantidad).label('cantidad')
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)
            .group_by(KardexMovimiento.referencia, KardexMovimiento.fecha)
            .all()
        )

        # Agrupar por referencia
        ref_series = defaultdict(list)
        for row in demanda_diaria:
            ref = row.referencia.strip() if row.referencia else ''
            if not ref:
                continue
            ref_series[ref].append((row.fecha, float(row.cantidad)))

        # Obtener clasificación S-B para filtrar solo los cuadrantes solicitados
        clasificacion = KardexService.clasificar_syntetos_boylan(ventana_meses)
        refs_objetivo = {
            c['referencia'] for c in clasificacion['clasificacion']
            if c['cuadrante'] in solo_cuadrantes
        }

        pronosticos = []
        tsb_gana = 0
        total_evaluados = 0

        for ref in refs_objetivo:
            eventos = sorted(ref_series.get(ref, []), key=lambda x: x[0])
            if len(eventos) < 3:
                continue

            cantidades = [e[1] for e in eventos]
            fechas = [e[0] for e in eventos]

            # Calcular intervalos entre demandas (en días)
            intervalos = []
            for i in range(1, len(fechas)):
                delta = (fechas[i] - fechas[i - 1]).days
                intervalos.append(delta)

            if not intervalos:
                continue

            # TSB: suavizamiento exponencial doble con corrección de sesgo
            z = cantidades[0]  # tamaño de demanda suavizado
            p = intervalos[0] if intervalos else 1  # intervalo suavizado

            for i in range(1, len(cantidades)):
                z = alpha * cantidades[i] + (1 - alpha) * z
                if i < len(intervalos):
                    p = alpha * intervalos[i] + (1 - alpha) * p

            p = max(p, 1)  # Guard: intervalo mínimo 1 día
            tsb_forecast = (z / p) * (1 - alpha / 2)  # Corrección TSB
            croston_forecast = z / p  # Sin corrección (Croston original)

            # Media móvil 8 semanas (56 días) como benchmark ingenuo
            dias_56 = date.today() - timedelta(days=56)
            demanda_8sem = sum(c for f, c in eventos if f >= dias_56)
            mm8_forecast = demanda_8sem / 56 if demanda_8sem > 0 else 0

            # MASE (Mean Absolute Scaled Error) — holdout last 20% de eventos
            n_holdout = max(1, len(cantidades) // 5)
            if len(cantidades) > n_holdout + 3:
                train = cantidades[:-n_holdout]
                test = cantidades[-n_holdout:]

                # Naive forecast = media del train
                naive = sum(train) / len(train) if train else 1
                mae_naive = sum(abs(t - naive) for t in test) / len(test) if naive > 0 else 1

                # MAE del TSB (forecast diario * intervalo promedio entre test)
                mae_tsb = sum(abs(t - tsb_forecast * p) for t in test) / len(test)
                mae_mm8 = sum(abs(t - mm8_forecast * 56 / len(test)) for t in test) / len(test)

                mase_tsb = round(mae_tsb / mae_naive, 3) if mae_naive > 0 else None
                mase_mm8 = round(mae_mm8 / mae_naive, 3) if mae_naive > 0 else None
            else:
                mase_tsb = None
                mase_mm8 = None

            if mase_tsb is not None and mase_mm8 is not None:
                total_evaluados += 1
                if mase_tsb < mase_mm8:
                    tsb_gana += 1

            pronosticos.append({
                'referencia': ref,
                'tsb_diario': round(tsb_forecast, 4),
                'tsb_semanal': round(tsb_forecast * 7, 2),
                'tsb_mensual': round(tsb_forecast * 30, 1),
                'croston_diario': round(croston_forecast, 4),
                'media_movil_8sem_diario': round(mm8_forecast, 4),
                'z_suavizado': round(z, 2),
                'p_suavizado': round(p, 1),
                'eventos': len(cantidades),
                'mase_tsb': mase_tsb,
                'mase_mm8': mase_mm8,
                'tsb_mejor': mase_tsb < mase_mm8 if mase_tsb and mase_mm8 else None,
            })

        pronosticos.sort(key=lambda x: x['tsb_mensual'], reverse=True)

        return {
            'total': len(pronosticos),
            'alpha': alpha,
            'cuadrantes': solo_cuadrantes,
            'ventana_meses': ventana_meses,
            'backtest': {
                'evaluados': total_evaluados,
                'tsb_gana': tsb_gana,
                'porcentaje_tsb_gana': round(tsb_gana / total_evaluados * 100, 1) if total_evaluados > 0 else 0,
                'nota': 'TSB debe ganar a media móvil 8 sem en MASE — spec §2.M0.3',
            },
            'pronosticos': pronosticos,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # M0.5 — Newsvendor para compra de temporada escolar
    # ══════════════════════════════════════════════════════════════════════════

    @staticmethod
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

            n_temporadas = len(ventas)
            mu = sum(ventas) / n_temporadas
            if n_temporadas > 1:
                sigma = math.sqrt(sum((v - mu) ** 2 for v in ventas) / (n_temporadas - 1))
            else:
                # Solo 1 temporada: inflar incertidumbre × 1.5
                sigma = mu * 0.30 * 1.5  # CV asumido 30%, inflado 50%

            # Q* via aproximación normal: Q* = mu + z_cr * sigma
            # z_cr = inversa de la normal estándar del ratio crítico
            z_cr = _norm_ppf(ratio_critico)
            q_optimo = max(0, round(mu + z_cr * sigma))

            # Rango de confianza 80%
            q_bajo = max(0, round(mu + _norm_ppf(0.10) * sigma))
            q_alto = max(0, round(mu + _norm_ppf(0.90) * sigma))

            resultados.append({
                'referencia': ref,
                'q_optimo': q_optimo,
                'demanda_esperada': round(mu, 1),
                'sigma': round(sigma, 1),
                'n_temporadas': n_temporadas,
                'ratio_critico': round(ratio_critico, 3),
                'z_critico': round(z_cr, 3),
                'rango_80': [q_bajo, q_alto],
                'costo_unitario': costo,
                'inversion_optima': round(q_optimo * costo) if costo else None,
                'advertencia_1_temporada': n_temporadas == 1,
            })

        resultados.sort(key=lambda x: x.get('inversion_optima') or 0, reverse=True)

        return {
            'ratio_critico': round(ratio_critico, 3),
            'margen_pct': margen_pct,
            'costo_exceso_pct': costo_exceso_pct,
            'total_items': len(resultados),
            'total_inversion': sum(r.get('inversion_optima') or 0 for r in resultados),
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
