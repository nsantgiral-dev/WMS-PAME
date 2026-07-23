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

# Conceptos que representan demanda real (salidas por venta)
CONCEPTOS_DEMANDA = {501}  # 501 = Ventas POS y remisiones

# Conceptos que NO son demanda (no contaminan velocity)
# 502=devoluciones, 601=entradas compra, 602=salidas, 603=ajustes, 607=traslados
CONCEPTOS_EXCLUIR_DEMANDA = {502, 601, 602, 603, 607}


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

        parametros = (
            f"f054_id_estado_docto=1 "
            f"AND f450_id_fecha>=''{fecha_desde}'' "
            f"AND f450_id_fecha<=''{fecha_hasta}''"
        )

        total = 0
        pagina = 1
        errores = 0
        inicio = datetime.utcnow()
        MAX_MINUTOS = 10

        while True:
            elapsed = (datetime.utcnow() - inicio).total_seconds()
            if elapsed > MAX_MINUTOS * 60:
                logger.warning('[KARDEX] Descarga abortada tras %.0fs', elapsed)
                break

            try:
                res = connekta._get(
                    NOMBRE_CONSULTA,
                    params_extra={
                        'paginacion': f'numPag={pagina}|tamPag=100',
                        'parametros': parametros,
                    },
                    url=connekta.url_get_dinamico,
                    timeout=60,
                )

                # Consultas dinámicas usan "Datos", no "Table"
                rows = (res or {}).get('detalle', {}).get('Datos', [])
                if not rows:
                    break

                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    ref = (row.get('f120_referencia') or '').strip()
                    bodega = (row.get('f150_id') or row.get('f470_id_bodega') or '').strip()
                    if not ref or not bodega:
                        continue

                    fecha_str = str(row.get('f450_id_fecha') or row.get('f350_fecha') or '').strip()
                    try:
                        fecha = datetime.strptime(fecha_str[:8], '%Y%m%d').date()
                    except (ValueError, TypeError):
                        errores += 1
                        continue

                    mov = KardexMovimiento(
                        fecha=fecha,
                        tipo_docto=(row.get('f350_id_tipo_docto') or '').strip(),
                        bodega=bodega,
                        referencia=ref,
                        concepto=int(row.get('f470_id_concepto', 0)),
                        naturaleza=int(row.get('f470_ind_naturaleza', 0)),
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
    def calcular_tasa_censurada(ventana_meses: int = 12) -> dict:
        """
        Calcula velocity censurada: picks reales / días con stock.

        Solo usa concepto 501 (ventas) como demanda.
        Solo cuenta días donde tuvo_stock=True como denominador.
        Ventana: últimos N meses.

        Returns: {total_skus, con_demanda, sin_demanda, tasas: [{referencia, bodega, ...}]}
        """
        from sqlalchemy import func

        fecha_limite = date.today() - timedelta(days=ventana_meses * 30)

        # Días con stock por (referencia, bodega)
        dias_stock = dict(
            db.session.query(
                StockDiario.referencia + '|' + StockDiario.bodega,
                func.count()
            )
            .filter(StockDiario.fecha >= fecha_limite)
            .filter(StockDiario.tuvo_stock == True)
            .group_by(StockDiario.referencia, StockDiario.bodega)
            .all()
        )

        # Demanda (concepto 501) por (referencia, bodega)
        demanda = dict(
            db.session.query(
                KardexMovimiento.referencia + '|' + KardexMovimiento.bodega,
                func.sum(KardexMovimiento.cantidad)
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_DEMANDA))
            .filter(KardexMovimiento.naturaleza == 2)  # Solo salidas
            .group_by(KardexMovimiento.referencia, KardexMovimiento.bodega)
            .all()
        )

        tasas = []
        for key, dias in dias_stock.items():
            ref, bod = key.split('|', 1)
            dem = float(demanda.get(key, 0))
            dias_int = int(dias)
            tasa = round(dem / dias_int, 4) if dias_int > 0 else 0

            tasas.append({
                'referencia': ref,
                'bodega': bod,
                'demanda_total': dem,
                'dias_con_stock': dias_int,
                'tasa_diaria_censurada': tasa,
                'velocity_cero': dem == 0,
            })

        con_demanda = sum(1 for t in tasas if not t['velocity_cero'])
        sin_demanda = sum(1 for t in tasas if t['velocity_cero'])

        tasas.sort(key=lambda x: x['tasa_diaria_censurada'], reverse=True)

        return {
            'total_skus': len(tasas),
            'con_demanda': con_demanda,
            'sin_demanda': sin_demanda,
            'ventana_meses': ventana_meses,
            'fecha_limite': fecha_limite.isoformat(),
            'tasas': tasas,
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
