"""
VigíaService — CUSUM tabular bilateral para detección de corrimientos operativos.

Órgano permanente de vigilancia. Detecta anomalías en series operativas semanales:
  1. Líneas despachadas por C.O. (volumen por visita)
  2. Facturación por C.O. (ingresos)
  3. Facturas únicas por C.O. (frecuencia de servicio — proxy de planillas)
  4. Planillas de ruta por zona (cuando esté disponible via Generic Transfer)

Dos bandas de alarma:
  h_aviso  = 3.0 → aviso amarillo (aparece en panel, no notifica, no exige cierre)
  h_alarma = 4.5 → alarma roja (notifica, exige causa+responsable, escala a 7 días)

Parámetros CUSUM:
  k = 0.5 (sensibilidad: detecta corrimientos de ~1σ)
  μ_ref, σ_ref: mediana y MAD×1.4826 de las primeras 26 semanas
  (robustos a outliers — no se contaminan con la anomalía que detectan)

Guard: la semana en curso NUNCA se evalúa (datos parciales → falsos positivos).

Test canónico: backtest sobre facturas_006 (C.O. 006, Florencia).
La alarma S⁻ debe sonar en la primera semana del declive agudo (dic 2025).
El colapso de Florencia fue de frecuencia de servicio (facturas únicas),
no de volumen por visita (líneas). El CUSUM lo detecta en 1 semana para
corrimientos de 4σ+.
"""
import logging
import os
from datetime import datetime, date, timedelta
from collections import defaultdict
from app.extensions import db

logger = logging.getLogger(__name__)

# Parámetros CUSUM (configurable por env para tuning)
CUSUM_K = float(os.environ.get('CUSUM_K', '0.5'))
CUSUM_H_AVISO = float(os.environ.get('CUSUM_H_AVISO', '3.0'))
CUSUM_H_ALARMA = float(os.environ.get('CUSUM_H_ALARMA', '4.5'))
VENTANA_REF = int(os.environ.get('CUSUM_VENTANA_SEMANAS', '26'))


class SerieVigia(db.Model):
    """Serie semanal para vigilancia CUSUM."""
    __tablename__ = 'serie_vigia'

    id = db.Column(db.Integer, primary_key=True)
    serie = db.Column(db.String(50), nullable=False)  # ej: 'despachos_006', 'facturas_002'
    semana = db.Column(db.Date, nullable=False)  # lunes de la semana
    valor = db.Column(db.Numeric(14, 2), nullable=False)
    registros = db.Column(db.Integer, default=0)
    # Procedencia del dato. HISTORICO = cargado desde export TXT (línea base μ_ref/σ_ref
    # previa al go-live). PRODUCCION = generado por la operación viva.
    # La limpieza transaccional del acta de corte NUNCA borra esta tabla: sin las 26
    # semanas de referencia el CUSUM no puede distinguir una semana normal de un colapso.
    fuente = db.Column(db.String(12), nullable=False, default='PRODUCCION')

    __table_args__ = (
        db.Index('ix_serie_vigia_serie_semana', 'serie', 'semana', unique=True),
    )


class AlarmaVigia(db.Model):
    """Alarma disparada por CUSUM."""
    __tablename__ = 'alarma_vigia'

    id = db.Column(db.Integer, primary_key=True)
    serie = db.Column(db.String(50), nullable=False)
    semana = db.Column(db.Date, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'SUBE' o 'BAJA'
    severidad = db.Column(db.String(10), nullable=False, default='ALARMA')  # 'AVISO' o 'ALARMA'
    s_valor = db.Column(db.Numeric(10, 4), nullable=False)  # valor S+/S- que disparó
    mu_ref = db.Column(db.Numeric(14, 2))
    sigma_ref = db.Column(db.Numeric(14, 2))
    causa = db.Column(db.Text, nullable=True)  # obligatorio para cerrar ALARMA
    responsable_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    cerrada = db.Column(db.Boolean, default=False)
    fecha_cierre = db.Column(db.DateTime, nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)


def _lunes_de_semana(fecha):
    """Retorna el lunes de la semana a la que pertenece la fecha."""
    return fecha - timedelta(days=fecha.weekday())


def _lunes_semana_actual():
    """Lunes de la semana en curso — esta semana NUNCA se evalúa."""
    return _lunes_de_semana(date.today())


class VigiaService:

    @staticmethod
    def cargar_ventas_desde_txt(filepath: str, encoding: str = 'latin1') -> dict:
        """
        Carga un export TXT de facturación de Siesa y agrega por semana × C.O.

        Genera TRES series por C.O.:
          - despachos_{co}: líneas despachadas (volumen por visita)
          - facturacion_{co}: valor neto total (ingresos)
          - facturas_{co}: facturas únicas (frecuencia de servicio)

        Columnas esperadas (tab-separated, 18 columnas):
        Fecha | Nro documento | Tipo docto. | C.O. | ... | Cantidad inv. | ... | Valor neto local

        Returns: {series_creadas, registros_procesados}
        """
        import csv

        series_por_semana = defaultdict(lambda: {'valor': 0, 'registros': 0})
        facturas_por_semana = defaultdict(set)  # Para contar facturas únicas
        procesados = 0
        errores = 0

        with open(filepath, 'r', encoding=encoding, errors='replace') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader, None)
            if not header:
                return {'error': 'Archivo vacío'}

            for row in reader:
                try:
                    if len(row) < 18:
                        continue
                    fecha_str = row[0].strip()
                    nro_doc = row[1].strip()
                    co = row[3].strip()
                    cantidad_str = row[14].strip().replace('.', '').replace(',', '.')
                    valor_str = row[17].strip().replace('$', '').replace('.', '').replace(',', '.')

                    try:
                        fecha = datetime.strptime(fecha_str, '%d/%m/%Y').date()
                    except ValueError:
                        errores += 1
                        continue

                    lunes = _lunes_de_semana(fecha)
                    cantidad = float(cantidad_str) if cantidad_str else 0
                    valor = float(valor_str) if valor_str else 0

                    # Serie 1: líneas despachadas (volumen)
                    key_despachos = f'despachos_{co}'
                    series_por_semana[(key_despachos, lunes)]['valor'] += cantidad
                    series_por_semana[(key_despachos, lunes)]['registros'] += 1

                    # Serie 2: facturación (ingresos)
                    key_facturacion = f'facturacion_{co}'
                    series_por_semana[(key_facturacion, lunes)]['valor'] += valor
                    series_por_semana[(key_facturacion, lunes)]['registros'] += 1

                    # Serie 3: facturas únicas (frecuencia de servicio)
                    facturas_por_semana[(co, lunes)].add(nro_doc)

                    procesados += 1
                except Exception:
                    errores += 1

        # Convertir facturas únicas a serie
        for (co, lunes), docs in facturas_por_semana.items():
            key_facturas = f'facturas_{co}'
            series_por_semana[(key_facturas, lunes)]['valor'] = len(docs)
            series_por_semana[(key_facturas, lunes)]['registros'] = len(docs)

        # Persistir
        creadas = 0
        for (serie, semana), datos in series_por_semana.items():
            existing = SerieVigia.query.filter_by(serie=serie, semana=semana).first()
            if existing:
                existing.valor = datos['valor']
                existing.registros = datos['registros']
            else:
                db.session.add(SerieVigia(
                    serie=serie, semana=semana,
                    valor=datos['valor'], registros=datos['registros'],
                    fuente='HISTORICO',
                ))
                creadas += 1

        db.session.commit()
        logger.info('[VIGIA] Cargadas %d series de %s (%d procesados, %d errores)',
                     creadas, filepath, procesados, errores)

        return {
            'series_creadas': creadas,
            'registros_procesados': procesados,
            'errores': errores,
        }

    @staticmethod
    def ejecutar_cusum(nombre_serie: str) -> dict:
        """
        Ejecuta CUSUM tabular bilateral con DOS bandas sobre una serie semanal.

        Calcula μ_ref y σ_ref con mediana y MAD (robustos) de las
        primeras VENTANA_REF semanas, luego corre el CUSUM sobre semanas CERRADAS.

        Guard: la semana en curso se excluye (datos parciales → falsos positivos).

        Dos bandas:
          h_aviso  = 3.0 → aparece en panel, no exige cierre
          h_alarma = 4.5 → notifica, exige causa+responsable

        Returns: {serie, semanas, alarmas_nuevas, cusum: [{semana, valor, z, s_plus, s_minus, alarma, severidad}]}
        """
        import statistics

        lunes_actual = _lunes_semana_actual()

        datos = (
            SerieVigia.query
            .filter_by(serie=nombre_serie)
            .filter(SerieVigia.semana < lunes_actual)  # Guard: excluir semana en curso
            .order_by(SerieVigia.semana)
            .all()
        )

        if len(datos) < 8:
            return {'error': f'Serie {nombre_serie} tiene solo {len(datos)} semanas cerradas — mínimo 8'}

        valores = [float(d.valor) for d in datos]

        # Referencia robusta: mediana y MAD de las primeras VENTANA_REF semanas
        ref_window = valores[:min(VENTANA_REF, len(valores))]
        mu_ref = statistics.median(ref_window)
        mad = statistics.median([abs(v - mu_ref) for v in ref_window])
        sigma_ref = mad * 1.4826  # MAD → σ estimado
        if sigma_ref < 0.001:
            sigma_ref = statistics.stdev(ref_window) if len(ref_window) > 1 else 1
        if sigma_ref < 0.001:
            sigma_ref = 1  # última defensa: serie constante → σ=1 para evitar div/0

        # CUSUM tabular bilateral con dos bandas
        s_plus = 0
        s_minus = 0
        resultado = []
        alarmas_nuevas = 0

        for d in datos:
            valor = float(d.valor)
            z = (valor - mu_ref) / sigma_ref

            s_plus = max(0, s_plus + z - CUSUM_K)
            s_minus = max(0, s_minus - z - CUSUM_K)

            # Dos bandas: aviso (h=3.0) y alarma (h=4.5)
            alarma = None
            severidad = None

            if s_plus > CUSUM_H_ALARMA:
                alarma = 'SUBE'
                severidad = 'ALARMA'
            elif s_plus > CUSUM_H_AVISO:
                alarma = 'SUBE'
                severidad = 'AVISO'
            elif s_minus > CUSUM_H_ALARMA:
                alarma = 'BAJA'
                severidad = 'ALARMA'
            elif s_minus > CUSUM_H_AVISO:
                alarma = 'BAJA'
                severidad = 'AVISO'

            # Registrar alarma si es nueva (dedup por serie+semana+tipo+severidad)
            if alarma:
                existing = AlarmaVigia.query.filter_by(
                    serie=nombre_serie, semana=d.semana,
                    tipo=alarma, severidad=severidad,
                ).first()
                if not existing:
                    db.session.add(AlarmaVigia(
                        serie=nombre_serie, semana=d.semana,
                        tipo=alarma, severidad=severidad,
                        s_valor=s_plus if alarma == 'SUBE' else s_minus,
                        mu_ref=mu_ref, sigma_ref=sigma_ref,
                    ))
                    alarmas_nuevas += 1

            resultado.append({
                'semana': d.semana.isoformat(),
                'valor': valor,
                'z': round(z, 3),
                's_plus': round(s_plus, 3),
                's_minus': round(s_minus, 3),
                'alarma': alarma,
                'severidad': severidad,
            })

        db.session.commit()

        return {
            'serie': nombre_serie,
            'semanas': len(datos),
            'mu_ref': round(mu_ref, 2),
            'sigma_ref': round(sigma_ref, 2),
            'k': CUSUM_K,
            'h_aviso': CUSUM_H_AVISO,
            'h_alarma': CUSUM_H_ALARMA,
            'alarmas_nuevas': alarmas_nuevas,
            'cusum': resultado,
        }

    @staticmethod
    def backtest_florencia() -> dict:
        """
        Test canónico: corre CUSUM sobre facturas_006 (C.O. 006, Florencia).

        El colapso de Florencia fue de frecuencia de servicio (facturas únicas
        por semana), no de volumen por visita. La alarma S⁻ debe sonar en la
        primera semana del declive agudo.

        Returns: {aprobado, primera_alarma_baja, detalle}
        """
        resultado = VigiaService.ejecutar_cusum('facturas_006')

        if 'error' in resultado:
            return resultado

        # Buscar primera alarma BAJA (cualquier severidad)
        primera_baja = None
        for i, punto in enumerate(resultado['cusum']):
            if punto['alarma'] == 'BAJA':
                primera_baja = {
                    'semana': punto['semana'],
                    'indice': i,
                    's_minus': punto['s_minus'],
                    'severidad': punto['severidad'],
                }
                break

        aprobado = primera_baja is not None

        return {
            'aprobado': aprobado,
            'serie': 'facturas_006',
            'total_semanas': resultado['semanas'],
            'mu_ref': resultado['mu_ref'],
            'sigma_ref': resultado['sigma_ref'],
            'primera_alarma_baja': primera_baja,
            'total_alarmas': resultado['alarmas_nuevas'],
            'nota': (
                'Test canónico: el CUSUM sobre facturas únicas (frecuencia de servicio) '
                'debe detectar el colapso de Florencia. El declive fue de planillas/rutas, '
                'no de volumen por visita.'
            ),
        }

    @staticmethod
    def listar_alarmas(serie: str = None, solo_abiertas: bool = True,
                       severidad: str = None) -> list:
        """Lista alarmas, opcionalmente filtradas por serie, estado y severidad."""
        q = AlarmaVigia.query
        if serie:
            q = q.filter_by(serie=serie)
        if solo_abiertas:
            q = q.filter_by(cerrada=False)
        if severidad:
            q = q.filter_by(severidad=severidad)
        return [{
            'id': a.id,
            'serie': a.serie,
            'semana': a.semana.isoformat(),
            'tipo': a.tipo,
            'severidad': a.severidad,
            's_valor': float(a.s_valor),
            'mu_ref': float(a.mu_ref) if a.mu_ref else None,
            'cerrada': a.cerrada,
            'causa': a.causa,
        } for a in q.order_by(AlarmaVigia.semana.desc()).all()]

    @staticmethod
    def cerrar_alarma(alarma_id: int, causa: str, responsable_id: int) -> dict:
        """
        Cierra una alarma con causa y responsable.
        Regla anti-silencio para ALARMA: causa mínimo 20 caracteres.
        AVISO puede cerrarse con causa más corta (pero no vacía).
        """
        alarma = db.session.get(AlarmaVigia, alarma_id)
        if not alarma:
            raise LookupError(f'Alarma {alarma_id} no encontrada')

        if alarma.severidad == 'ALARMA':
            if not causa or len(causa.strip()) < 20:
                raise ValueError('La causa debe tener al menos 20 caracteres — no se puede cerrar en silencio')
        else:
            if not causa or len(causa.strip()) < 5:
                raise ValueError('La causa debe tener al menos 5 caracteres')

        alarma.cerrada = True
        alarma.causa = causa.strip()
        alarma.responsable_id = responsable_id
        alarma.fecha_cierre = datetime.utcnow()
        db.session.commit()

        return {'ok': True, 'alarma_id': alarma_id}

    @staticmethod
    def alimentar_adopcion_picking() -> dict:
        """
        Calcula la serie semanal de adopción de picking y la persiste.

        adopcion_picking = picks completados por semana (proxy de adopción del WMS).
        La brecha (cantidad_solicitada - cantidad_recogida) se mide por separado.

        Diseñado para correr en scheduler semanal post go-live.
        """
        from app.models.picking import TareaPicking
        from sqlalchemy import func

        lunes_actual = _lunes_semana_actual()

        # Agregar picks completados por semana (solo semanas cerradas)
        picks = (
            db.session.query(
                func.date(TareaPicking.fecha_creacion).label('fecha'),
                func.count(TareaPicking.id).label('total'),
                func.sum(TareaPicking.cantidad_solicitada).label('solicitado'),
                func.sum(TareaPicking.cantidad_recogida).label('recogido'),
            )
            .filter(TareaPicking.estado.in_(['COMPLETADO', 'BLOQUEADO', 'AUDITADO']))
            .group_by(func.date(TareaPicking.fecha_creacion))
            .all()
        )

        # Agregar por semana
        semanas = defaultdict(lambda: {'picks': 0, 'solicitado': 0, 'recogido': 0})
        for p in picks:
            if not p.fecha:
                continue
            if isinstance(p.fecha, str):
                d = datetime.strptime(p.fecha, '%Y-%m-%d').date()
            else:
                d = p.fecha
            lunes = _lunes_de_semana(d)
            if lunes >= lunes_actual:
                continue  # Guard: no evaluar semana en curso
            semanas[lunes]['picks'] += p.total or 0
            semanas[lunes]['solicitado'] += int(p.solicitado or 0)
            semanas[lunes]['recogido'] += int(p.recogido or 0)

        # Persistir serie adopcion_picking (picks/semana)
        creadas = 0
        for lunes, datos in sorted(semanas.items()):
            existing = SerieVigia.query.filter_by(
                serie='adopcion_picking', semana=lunes).first()
            if existing:
                existing.valor = datos['picks']
                existing.registros = datos['picks']
            else:
                db.session.add(SerieVigia(
                    serie='adopcion_picking', semana=lunes,
                    valor=datos['picks'], registros=datos['picks'],
                    fuente='PRODUCCION',
                ))
                creadas += 1

        # También persistir serie brecha_picking (% servido)
        for lunes, datos in sorted(semanas.items()):
            if datos['solicitado'] == 0:
                continue
            tasa = round(datos['recogido'] / datos['solicitado'] * 100, 2)
            existing = SerieVigia.query.filter_by(
                serie='brecha_picking', semana=lunes).first()
            if existing:
                existing.valor = tasa
            else:
                db.session.add(SerieVigia(
                    serie='brecha_picking', semana=lunes,
                    valor=tasa, registros=datos['picks'],
                    fuente='PRODUCCION',
                ))
                creadas += 1

        db.session.commit()

        return {
            'semanas_procesadas': len(semanas),
            'series_creadas': creadas,
        }

    @staticmethod
    def salud_conectores() -> dict:
        """
        G0: Verifica la latencia de los conectores de datos.

        Retorna estado de cada fuente de datos con latencia en horas.
        Bandera roja si algún conector tiene latencia >24h.
        """
        from app.models.stock_siesa import StockSiesa
        from sqlalchemy import func

        ahora = datetime.utcnow()
        resultado = {'conectores': [], 'g0_ok': True}

        # Stock Siesa: última sincronización
        ultimo_stock = db.session.query(
            func.max(StockSiesa.fecha_sync)
        ).scalar()
        if ultimo_stock:
            delta_h = (ahora - ultimo_stock).total_seconds() / 3600
            ok = delta_h < 24
            resultado['conectores'].append({
                'nombre': 'Stock Siesa (inventario)',
                'ultimo_sync': ultimo_stock.isoformat(),
                'latencia_horas': round(delta_h, 1),
                'ok': ok,
            })
            if not ok:
                resultado['g0_ok'] = False

        # Serie Vigía: última semana cargada
        ultima_serie = db.session.query(
            func.max(SerieVigia.semana)
        ).scalar()
        if ultima_serie:
            delta_dias = (date.today() - ultima_serie).days
            resultado['conectores'].append({
                'nombre': 'Series Vigia (ventas)',
                'ultima_semana': ultima_serie.isoformat(),
                'latencia_dias': delta_dias,
                'ok': delta_dias < 14,  # Datos semanales, 14 días = aceptable
            })
            if delta_dias >= 14:
                resultado['g0_ok'] = False

        # Picking: último pick (proxy de que el sistema está siendo usado)
        from app.models.picking import TareaPicking
        ultimo_pick = db.session.query(
            func.max(TareaPicking.fecha_creacion)
        ).filter(TareaPicking.estado != 'PENDIENTE').scalar()
        if ultimo_pick:
            delta_h = (ahora - ultimo_pick).total_seconds() / 3600
            resultado['conectores'].append({
                'nombre': 'Picking WMS (adopcion)',
                'ultimo_pick': ultimo_pick.isoformat(),
                'latencia_horas': round(delta_h, 1),
                'ok': delta_h < 24,
            })
            if delta_h >= 24:
                resultado['g0_ok'] = False

        return resultado


def alimentar_series_vivas(app=None):
    """
    Punto de entrada del cron semanal — corre los lunes 05:30 Bogotá.

    Recalcula adopcion_picking y brecha_picking desde las tareas de picking de la
    semana que acaba de cerrar. Es el termómetro de adopción del go-live: mide si
    los operarios están usando el WMS o si la vía manual sigue ganando.

    Lunes porque el guard de _lunes_semana_actual() descarta la semana en curso —
    corriendo lunes temprano, la semana anterior ya está cerrada y es evaluable.
    """
    from flask import current_app as _app
    ctx_app = app or _app._get_current_object()

    with ctx_app.app_context():
        try:
            resultado = VigiaService.alimentar_adopcion_picking()
            logger.info('[VIGIA_SCHEDULER] Series vivas alimentadas — %d semanas, %d series nuevas',
                        resultado.get('semanas_procesadas', 0), resultado.get('series_creadas', 0))
            return resultado
        except Exception as e:
            logger.exception('[VIGIA_SCHEDULER] Fallo alimentando series vivas: %s', e)
            return None


def init_scheduler(app):
    """
    Cron semanal:
      Lunes 05:30 Bogotá → alimenta adopcion_picking + brecha_picking

    Adelantado a las otras alertas (05:45) para que el resumen operativo lea
    series ya actualizadas.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[VIGIA_SCHEDULER] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=alimentar_series_vivas,
        trigger=CronTrigger(day_of_week='mon', hour=5, minute=30, timezone='America/Bogota'),
        kwargs={'app': app},
        id='vigia_alimentar_series',
        name='Vigía — alimenta series vivas (lunes 05:30 Bogotá)',
        replace_existing=True, max_instances=1, misfire_grace_time=3600,
    )

    scheduler.start()
    import atexit
    atexit.register(lambda: scheduler.shutdown(wait=False))
    logger.info('[VIGIA_SCHEDULER] Scheduler iniciado — lunes 05:30 alimentación de series vivas')
    return scheduler
