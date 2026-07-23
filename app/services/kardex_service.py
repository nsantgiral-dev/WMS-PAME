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
import os
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

        for ref, bod in combos:
            # Saldo actual (última foto conocida)
            saldo_actual = KardexService._obtener_saldo_actual(ref, bod)

            # Movimientos ordenados del más reciente al más antiguo
            movimientos = (
                KardexMovimiento.query
                .filter_by(referencia=ref, bodega=bod)
                .order_by(KardexMovimiento.fecha.desc())
                .all()
            )

            if not movimientos:
                continue

            # Agrupar movimientos por día
            from collections import defaultdict
            dias_mov = defaultdict(list)
            for m in movimientos:
                dias_mov[m.fecha].append(m)

            # Generar stock de cierre por día (hacia atrás)
            saldo = float(saldo_actual)
            fechas_ordenadas = sorted(dias_mov.keys(), reverse=True)

            for fecha in fechas_ordenadas:
                # Stock al cierre de este día = saldo antes de restar los movimientos del día
                stock_cierre = saldo

                # Registrar
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

                # Aritmética hacia atrás: deshacer los movimientos de este día
                for m in dias_mov[fecha]:
                    cant = float(m.cantidad)
                    if m.naturaleza == 1:  # Entrada → restar para ir hacia atrás
                        saldo -= cant
                    elif m.naturaleza == 2:  # Salida → sumar para ir hacia atrás
                        saldo += cant

            refs_procesadas += 1

            # Commit cada 100 referencias para no acumular
            if refs_procesadas % 100 == 0:
                db.session.commit()
                logger.info('[KARDEX] Reconstruido %d referencias...', refs_procesadas)

        db.session.commit()
        logger.info(
            '[KARDEX] Reconstrucción completa: %d referencias, %d días',
            refs_procesadas, dias_generados
        )
        return {'referencias_procesadas': refs_procesadas, 'dias_generados': dias_generados}

    @staticmethod
    def reconciliar_kardex(ventana_meses: int = 12) -> dict:
        """
        COMPUERTA DE COMPLETITUD: cruza salidas del kardex contra
        lo esperado para verificar que la descarga está completa.

        Suma salidas por venta (501) por mes y retorna para cruce
        manual contra facturación de Siesa del mismo mes.
        Si no cuadra ±2%, la descarga está incompleta.
        """
        from sqlalchemy import func, extract

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        # Salidas por venta agrupadas por mes
        ventas_mes = (
            db.session.query(
                func.to_char(KardexMovimiento.fecha, 'YYYY-MM').label('mes'),
                func.sum(KardexMovimiento.cantidad).label('unidades'),
                func.sum(KardexMovimiento.cantidad * KardexMovimiento.costo_promedio).label('valor_costo'),
                func.count().label('registros'),
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)  # salidas
            .group_by(func.to_char(KardexMovimiento.fecha, 'YYYY-MM'))
            .order_by(func.to_char(KardexMovimiento.fecha, 'YYYY-MM'))
            .all()
        )

        # Resumen total del kardex
        total = db.session.query(func.count()).select_from(KardexMovimiento).scalar() or 0
        total_por_concepto = dict(
            db.session.query(
                KardexMovimiento.concepto,
                func.count()
            )
            .group_by(KardexMovimiento.concepto)
            .all()
        )
        bodegas = [r[0] for r in db.session.query(KardexMovimiento.bodega).distinct().all()]

        return {
            'total_registros_kardex': total,
            'bodegas_con_datos': sorted(bodegas),
            'registros_por_concepto': {str(k): v for k, v in sorted(total_por_concepto.items())},
            'ventas_por_mes': [{
                'mes': r.mes,
                'unidades': float(r.unidades or 0),
                'valor_costo': float(r.valor_costo or 0),
                'registros': r.registros,
            } for r in ventas_mes],
            'advertencia': (
                'Cruzar ventas_por_mes contra facturación de Siesa. '
                'Si difieren >2%, la descarga está incompleta.'
            ),
        }

    @staticmethod
    def calcular_tasa_servida_corregida(ventana_meses: int = 12,
                                         nivel: str = 'bodega') -> dict:
        """
        Tasa servida corregida: demanda neta / días con stock.

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
    def clasificar_syntetos_boylan(ventana_meses: int = 12,
                                    estacionales: list = None) -> dict:
        """
        Clasificación Syntetos-Boylan sobre demanda agregada de RED.

        REGLAS DEL CONSULTOR:
        1. Clasificar a nivel de RED (no por bodega) — el rol del SKU es global
        2. Estacionales se EXCLUYEN — se evalúan dentro de su ventana, no contra 365d
        3. La clasificación PROPONE sentencias, no ejecuta bloqueos automáticos
        4. Si un constitucional cae en 'grumosa', es señal de alarma (dato o rol incorrecto)

        Cuadrantes:
        - Suave: ADI ≤ 1.32 y CV² ≤ 0.49 → reposición automática
        - Errática: ADI ≤ 1.32 y CV² > 0.49 → colchón + revisión mensual
        - Intermitente: ADI > 1.32 y CV² ≤ 0.49 → mín-máx simple
        - Grumosa: ADI > 1.32 y CV² > 0.49 → candidata a cola/remate (PROPUESTA, no auto)

        Args:
            ventana_meses: ventana de análisis (default 12)
            estacionales: lista de referencias a EXCLUIR (se evalúan aparte)

        Returns: {clasificacion: [...], resumen: {...}, alertas: [...]}
        """
        from sqlalchemy import func
        import math

        if estacionales is None:
            estacionales = []
        estacionales_set = set(estacionales)

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
