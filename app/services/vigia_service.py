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
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo

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


# Nombres plausibles de la columna de ítem en el export de ventas. Se busca
# por CABECERA y no por índice: escribir row[7] porque parece razonable sería
# exactamente el error que este proyecto lleva una semana persiguiendo.
# EN ORDEN DE PRIORIDAD, no de posición en el archivo.
#
# El export real trae DOS columnas candidatas: "Item" (0000008, código interno
# de Siesa) y "Referencia" (P100176). La clave de cruce con el resto del
# sistema es REFERENCIA: KardexMovimiento.referencia y Producto.codigo_siesa
# vienen ambas de f120_referencia.
#
# Buscar columna por columna habría devuelto "Item" —aparece antes— y el cruce
# habría fallado en silencio: precios calculados que no empatan con ningún SKU.
CABECERAS_ITEM = ('referencia', 'sku', 'codigo', 'código', 'cod. item',
                  'cod item', 'item', 'ítem', 'producto', 'articulo', 'artículo')


def detectar_columna_item(header):
    """Índice de la columna de ítem, por nombre de cabecera y POR PRIORIDAD.

    Se recorre la lista de candidatos en orden de preferencia y, para cada uno,
    se busca en todas las columnas. Así "Referencia" gana a "Item" aunque
    aparezca después en el archivo.

    Devuelve (indice, nombre) o (None, None). NO adivina: si no reconoce
    ninguna cabecera, quien llama debe declararlo — nunca caer en silencio al
    margen supuesto (Regla 0).
    """
    if not header:
        return None, None
    limpias = [(i, (c or '').strip().lower()) for i, c in enumerate(header)]
    for cand in CABECERAS_ITEM:
        for i, limpio in limpias:
            if limpio and (limpio == cand or limpio.startswith(cand)):
                return i, (header[i] or '').strip()
    return None, None


# Un costo mayor que este múltiplo del valor de venta es un COSTO FANTASMA:
# error de unidad de empaque, no un negocio a pérdida. Medido sobre el export
# real, 0.5% de las filas arrastraban el margen agregado de +35.5% a -190%.
FACTOR_COSTO_FANTASMA = 3.0


def _lunes_de_semana(fecha):
    """Retorna el lunes de la semana a la que pertenece la fecha."""
    return fecha - timedelta(days=fecha.weekday())


# ══════════════════════════════════════════════════════════════════════════
# POLÍTICA ÚNICA DE ZONA — docs/canones/zona_horaria.json
#
# Toda derivación de fecha de negocio se hace en America/Bogota. Nunca desde
# un timestamp UTC. Una serie semanal sin zona de corte declarada no es una
# serie: son dos series distintas según dónde corra el servidor.
#
# Se corrige AHORA porque serie_vigia está vacía: cero backfill, cero
# reconciliación. Cada semana que el ensayo escriba filas bajo definición UTC
# convierte un cambio de dos líneas en una migración de datos.
#
# Colombia no tiene horario de verano — el desfase es fijo, UTC-5 siempre.
# ══════════════════════════════════════════════════════════════════════════
TZ_BOGOTA = ZoneInfo('America/Bogota')


def hoy_bogota():
    """Fecha de negocio de hoy. NUNCA _dia_operativo(), que da la del servidor."""
    return datetime.now(TZ_BOGOTA).date()


def fecha_negocio(ts):
    """Fecha de negocio de un timestamp.

    Los timestamps del WMS se guardan con datetime.utcnow(), que produce un
    naive en UTC — parece local y no lo es. Aquí se asume UTC cuando viene sin
    zona, que es lo que de hecho son, y se convierte a Bogotá antes de derivar
    la fecha.
    """
    if ts is None:
        return None
    if isinstance(ts, date) and not isinstance(ts, datetime):
        return ts  # ya es fecha de negocio (ej. f350_fecha de Siesa)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(TZ_BOGOTA).date()


def _lunes_semana_actual():
    """Lunes de la semana en curso EN BOGOTÁ — esta semana NUNCA se evalúa.

    Con _dia_operativo() el guard se abría cinco horas antes: entre las 7 p.m. del
    domingo y medianoche en Bogotá, el servidor UTC ya está en lunes y la
    semana que aún no cierra se vuelve evaluable con el domingo incompleto.
    Un conteo bajo es exactamente lo que el CUSUM lee como colapso — falsa
    alarma S⁻ en el instrumento cuya credibilidad depende de no darlas.
    """
    return _lunes_de_semana(hoy_bogota())


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
        # Precio realizado: valor/cantidad por SKU y por C.O. Mismo archivo,
        # misma pasada — solo falta saber en qué columna viene el ítem.
        realizado = defaultdict(lambda: {'valor': 0.0, 'cantidad': 0.0, 'lineas': 0})
        procesados = 0
        errores = 0

        with open(filepath, 'r', encoding=encoding, errors='replace') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader, None)
            if not header:
                return {'error': 'Archivo vacío'}

            col_item, nombre_col_item = detectar_columna_item(header)
            if col_item is None:
                # Regla 0: no caer en silencio al margen supuesto. Se declara.
                logger.error(
                    '[VIGIA] NO se reconoció la columna de ítem. Cabeceras: %s. '
                    'El precio realizado NO se calculará y Cu seguirá con margen '
                    'SUPUESTO. Añadir el nombre real a CABECERAS_ITEM.', header)

            for row in reader:
                try:
                    if len(row) < 18:
                        continue
                    fecha_str = row[0].strip()
                    nro_doc = row[1].strip()
                    co = row[3].strip()
                    cantidad_str = row[14].strip().replace('.', '').replace(',', '.')
                    valor_str = row[17].strip().replace('$', '').replace('.', '').replace(',', '.')
                    ref_item = (row[col_item].strip()
                                if col_item is not None and col_item < len(row) else '')

                    try:
                        fecha = datetime.strptime(fecha_str, '%d/%m/%Y').date()
                    except ValueError:
                        errores += 1
                        continue

                    lunes = _lunes_de_semana(fecha)
                    cantidad = float(cantidad_str) if cantidad_str else 0
                    valor = float(valor_str) if valor_str else 0

                    # Precio realizado: por SKU y por C.O. Se acumula valor y
                    # cantidad; el cociente se calcula al final, que es lo
                    # correcto — el promedio de cocientes no es el cociente de
                    # los totales.
                    if ref_item and cantidad > 0:
                        for clave in ((ref_item, co), (ref_item, None)):
                            acc = realizado[clave]
                            acc['valor'] += valor
                            acc['cantidad'] += cantidad
                            acc['lineas'] += 1

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

        # Precio realizado por SKU y por C.O. — valor/cantidad sobre ventas reales
        precios_creados = 0
        if col_item is not None:
            from app.models.precio_realizado import PrecioRealizado
            for (ref_i, co_i), acc in realizado.items():
                if acc['cantidad'] <= 0:
                    continue
                precio = acc['valor'] / acc['cantidad']
                fila = PrecioRealizado.query.filter_by(
                    referencia=ref_i, centro_operacion=co_i, periodo='TOTAL').first()
                if fila is None:
                    fila = PrecioRealizado(referencia=ref_i, centro_operacion=co_i,
                                           periodo='TOTAL')
                    db.session.add(fila)
                    precios_creados += 1
                fila.valor_total = acc['valor']
                fila.cantidad_total = acc['cantidad']
                fila.precio_realizado = precio
                fila.lineas = acc['lineas']

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
            'columna_item': nombre_col_item,
            'precio_realizado_calculado': col_item is not None,
            'precios_realizados_creados': precios_creados,
            'advertencia_item': None if col_item is not None else (
                f'NO se reconoció la columna de ítem entre las cabeceras {header}. '
                f'El precio realizado no se calculó: Cu sigue con margen SUPUESTO. '
                f'Añadir el nombre real a CABECERAS_ITEM en vigia_service.'
            ),
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
        # Se traen los timestamps crudos y se agrupa en Python: agrupar en SQL
        # por func.date() daría la fecha UTC, y el desfase a Bogotá no es
        # portable entre SQLite y Postgres. El volumen es picks de 12 meses,
        # perfectamente manejable.
        picks = (
            db.session.query(
                TareaPicking.fecha_creacion.label('ts'),
                TareaPicking.cantidad_solicitada.label('solicitado'),
                TareaPicking.cantidad_recogida.label('recogido'),
            )
            .filter(TareaPicking.estado.in_(['COMPLETADO', 'BLOQUEADO', 'AUDITADO']))
            .all()
        )

        # Agregar por semana DE NEGOCIO
        semanas = defaultdict(lambda: {'picks': 0, 'solicitado': 0, 'recogido': 0})
        for p in picks:
            d = fecha_negocio(p.ts)
            if not d:
                continue
            lunes = _lunes_de_semana(d)
            if lunes >= lunes_actual:
                continue  # Guard: no evaluar semana en curso
            semanas[lunes]['picks'] += 1
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
    def alimentar_series_facturacion(semana=None, cos=None) -> dict:
        """Alimenta hacia adelante las TRES series que venían solo del TXT.

        `despachos_{co}`, `facturacion_{co}` y `facturas_{co}` tenían línea base
        histórica y **ninguna ingesta viva**. El CUSUM vigilaba dos series de
        cinco: la adopción del picking, y nada del negocio.

        ── COMPARABILIDAD, que es lo único que hace válido el CUSUM ──────────

        La línea base salió de un export de FACTURACIÓN de Siesa. Si la ingesta
        hacia adelante se calculara desde la operación del WMS, mediría otra
        cosa —el WMS no ve las ventas de mostrador— y el CUSUM leería esa
        diferencia estructural como un desplome. **Un detector que dispara por
        cambiar de fuente es peor que no tener detector.**

        Por eso se consulta la misma fuente y se replica la agregación EXACTA
        del cargador TXT:

            despachos_{co}   = suma de cantidad por línea      (no cuenta líneas)
            facturacion_{co} = suma de valor neto
            facturas_{co}    = documentos únicos

        (El docstring del cargador dice "líneas despachadas" y el código suma
        `cantidad`. El contrato es el código: con él se construyó la base.)

        ── LO QUE ESTA FUNCIÓN NO HACE, A PROPÓSITO ─────────────────────────

        · **No escribe la semana en curso.** Una semana a medias parece un
          desplome. Solo semanas cerradas.
        · **No escribe 0 cuando Siesa no responde.** Un cero es "no se vendió";
          un hueco es "no sabemos". Escribir 0 ante un fallo de red dispararía
          una alarma de colapso operativo que no ocurrió — el error más caro
          que puede cometer un detector.
        · **No toca las filas HISTORICO.** La línea base es irreemplazable: sin
          las 26 semanas de referencia el CUSUM queda ciego ~6 meses.
        """
        from datetime import timedelta

        from app.services.connekta_gateway import ConnektaGateway

        lunes_actual = _lunes_semana_actual()
        semana = semana or (lunes_actual - timedelta(days=7))

        if semana >= lunes_actual:
            return {'error': 'semana en curso — una semana a medias parece un '
                             'desplome. Solo se alimentan semanas cerradas.',
                    'semana': semana.isoformat()}

        connekta = ConnektaGateway()
        if connekta.modo_simulacion:
            return {'error': 'modo simulación — no se escriben series con datos '
                             'inventados (regla 8)', 'semana': semana.isoformat()}

        cos = cos or VigiaService._cos_a_vigilar()
        domingo = semana + timedelta(days=6)

        escritas, huecos, detalle = 0, [], {}
        for co in cos:
            filas = VigiaService._facturas_de_semana(connekta, co, semana, domingo)
            if filas is None:
                # Regla 0: dato ausente se declara, no se rellena con cero.
                huecos.append(co)
                logger.error(
                    '[VIGIA] CO %s semana %s: Siesa no respondió. NO se escribe '
                    '0 — un cero acá es una alarma de colapso que no ocurrió.',
                    co, semana)
                continue

            cantidad = sum(f['cantidad'] for f in filas)
            valor = sum(f['valor'] for f in filas)
            documentos = len({f['documento'] for f in filas})

            for serie, val, regs in (
                (f'despachos_{co}',   cantidad,   len(filas)),
                (f'facturacion_{co}', valor,      len(filas)),
                (f'facturas_{co}',    documentos, documentos),
            ):
                if VigiaService._upsert_serie(serie, semana, val, regs):
                    escritas += 1
            detalle[co] = {'lineas': len(filas), 'cantidad': cantidad,
                           'valor': round(valor, 2), 'documentos': documentos}
            VigiaService._escribir_precio_realizado(co, semana, filas)

        db.session.commit()
        return {
            'semana': semana.isoformat(),
            'cos_procesados': len(cos) - len(huecos),
            'series_escritas': escritas,
            # Los huecos van en la respuesta, no solo en el log: si un CO deja
            # de responder tres semanas seguidas, eso ES el hallazgo.
            'cos_sin_dato': huecos,
            'detalle': detalle,
        }

    #: Ventana del precio realizado VIVO. Doce semanas: suficiente para promediar
    #: el ruido de una semana rara, corto para que un cambio de lista se note.
    VENTANA_PRECIO_REALIZADO_SEMANAS = 12

    @staticmethod
    def _escribir_precio_realizado(co, semana, filas):
        """Precio realizado de la semana, por SKU. Idempotente por construcción.

        ── POR QUÉ NO SE ACUMULA SOBRE `TOTAL` ──────────────────────────────

        El cargador TXT escribe `periodo='TOTAL'` con toda la historia, y
        `costo_service._precios_realizados` lee ESA fila. Sumarle la semana
        encima tendría dos problemas, y el segundo es el grave:

          · No sería idempotente: correr la misma semana dos veces contaría el
            doble.
          · `_precios_realizados` arma un dict por referencia SIN filtrar
            periodo. Con varias filas por SKU, cuál gana es **arbitrario**.

        Por eso cada semana es su propia fila (`S-YYYY-MM-DD`), y el promedio
        vivo se RECALCULA desde ellas — recalcular es idempotente, acumular no.

        `TOTAL` queda intacto: es la historia del TXT y no se puede reconstruir.
        """
        from app.models.precio_realizado import PrecioRealizado

        acumulado = defaultdict(lambda: {'valor': 0.0, 'cantidad': 0.0, 'lineas': 0})
        for f in filas:
            ref = f.get('referencia')
            if not ref or f['cantidad'] <= 0:
                continue
            # Por C.O. y agregado de red, igual que el TXT.
            for clave in ((ref, co), (ref, None)):
                a = acumulado[clave]
                a['valor'] += f['valor']
                a['cantidad'] += f['cantidad']
                a['lineas'] += 1

        etiqueta = f'S-{semana.isoformat()}'
        for (ref, centro), a in acumulado.items():
            fila = PrecioRealizado.query.filter_by(
                referencia=ref, centro_operacion=centro, periodo=etiqueta).first()
            if fila is None:
                fila = PrecioRealizado(referencia=ref, centro_operacion=centro,
                                       periodo=etiqueta)
                db.session.add(fila)
            fila.valor_total = a['valor']
            fila.cantidad_total = a['cantidad']
            # El cociente de los totales, no el promedio de cocientes — son
            # cosas distintas y la segunda pesa igual una venta de 1 unidad que
            # una de 500.
            fila.precio_realizado = a['valor'] / a['cantidad'] if a['cantidad'] else 0
            fila.lineas = a['lineas']

        VigiaService._recalcular_precio_vivo(
            {ref for ref, _ in acumulado}, semana)

    @staticmethod
    def _recalcular_precio_vivo(refs, hasta_semana):
        """Promedio de las últimas N semanas → `periodo='VIVO'`.

        Se RECALCULA desde las filas semanales, no se acumula: correr dos veces
        la misma semana da el mismo resultado.

        `costo_service` prefiere esta fila sobre `TOTAL` cuando existe, y el
        motivo es de negocio: un precio de las últimas doce semanas describe
        mejor lo que hoy se cobra que un promedio de toda la historia, que
        arrastra listas viejas.
        """
        from datetime import timedelta

        from app.models.precio_realizado import PrecioRealizado

        desde = f'S-{(hasta_semana - timedelta(weeks=VigiaService.VENTANA_PRECIO_REALIZADO_SEMANAS)).isoformat()}'
        hasta = f'S-{hasta_semana.isoformat()}'

        for ref in refs:
            for centro in (None,):   # el agregado de red es el que lee costo_service
                semanales = (PrecioRealizado.query
                             .filter(PrecioRealizado.referencia == ref,
                                     PrecioRealizado.centro_operacion.is_(centro),
                                     PrecioRealizado.periodo >= desde,
                                     PrecioRealizado.periodo <= hasta,
                                     PrecioRealizado.periodo.like('S-%'))
                             .all())
                valor = sum(float(x.valor_total or 0) for x in semanales)
                cant = sum(float(x.cantidad_total or 0) for x in semanales)
                if cant <= 0:
                    continue
                vivo = PrecioRealizado.query.filter_by(
                    referencia=ref, centro_operacion=centro, periodo='VIVO').first()
                if vivo is None:
                    vivo = PrecioRealizado(referencia=ref, centro_operacion=centro,
                                           periodo='VIVO')
                    db.session.add(vivo)
                vivo.valor_total = valor
                vivo.cantidad_total = cant
                vivo.precio_realizado = valor / cant
                vivo.lineas = sum(int(x.lineas or 0) for x in semanales)

    @staticmethod
    def comparar_con_linea_base(semana, cos=None) -> dict:
        """Calcula la semana con la fuente VIVA y la compara con el TXT.

        **Sin escribir nada.** Es la pregunta que decide si el cron se puede
        encender: ¿la agregación viva reproduce la histórica?

        Si no coincide, el CUSUM leería esa diferencia de método como un
        desplome del negocio y mandaría a alguien a investigar una caída que
        nunca ocurrió. Encender la ingesta sin haber corrido esto es confiar en
        que dos cálculos coinciden sin haberlo mirado.
        """
        from datetime import timedelta

        from app.services.connekta_gateway import ConnektaGateway

        connekta = ConnektaGateway()
        if connekta.modo_simulacion:
            return {'error': 'modo simulación — la comparación no significaría nada'}

        cos = cos or VigiaService._cos_a_vigilar()
        domingo = semana + timedelta(days=6)
        filas_out, iguales, distintas, sin_base = [], 0, 0, 0

        for co in cos:
            filas = VigiaService._facturas_de_semana(connekta, co, semana, domingo)
            if filas is None:
                filas_out.append({'co': co, 'estado': 'SIN_DATO'})
                continue
            vivo = {
                f'despachos_{co}': sum(f['cantidad'] for f in filas),
                f'facturacion_{co}': sum(f['valor'] for f in filas),
                f'facturas_{co}': len({f['documento'] for f in filas}),
            }
            for serie, valor_vivo in vivo.items():
                base = SerieVigia.query.filter_by(serie=serie, semana=semana).first()
                if base is None or base.fuente != 'HISTORICO':
                    sin_base += 1
                    filas_out.append({'serie': serie, 'vivo': round(valor_vivo, 2),
                                      'historico': None, 'estado': 'SIN_BASE'})
                    continue
                hist = float(base.valor)
                # 1% de tolerancia: redondeos de Siesa, no diferencias de método.
                ok = abs(valor_vivo - hist) <= max(abs(hist) * 0.01, 0.01)
                iguales += ok
                distintas += (not ok)
                filas_out.append({
                    'serie': serie, 'vivo': round(valor_vivo, 2),
                    'historico': round(hist, 2),
                    'desvio_pct': round((valor_vivo - hist) / hist * 100, 2) if hist else None,
                    'estado': 'OK' if ok else 'DIFIERE',
                })

        return {
            'semana': semana.isoformat(),
            'coinciden': iguales,
            'difieren': distintas,
            'sin_linea_base': sin_base,
            # El veredicto explícito. Un reporte que hay que interpretar es un
            # reporte que se interpreta mal bajo presión.
            'apto_para_encender': distintas == 0 and iguales > 0,
            'filas': filas_out,
        }

    @staticmethod
    def _cos_a_vigilar():
        """Los CO con serie histórica. Vigilar uno sin línea base no sirve:
        el CUSUM necesita μ_ref y σ_ref para poder comparar contra algo."""
        filas = (db.session.query(SerieVigia.serie)
                 .filter(SerieVigia.serie.like('facturacion_%'))
                 .distinct().all())
        return sorted({f[0].split('_', 1)[1] for f in filas if '_' in f[0]})

    @staticmethod
    def _facturas_de_semana(connekta, co, desde, hasta):
        """Líneas de factura del CO en el rango. `None` si Siesa no respondió.

        La distinción entre `[]` y `None` es la del módulo entero: una lista
        vacía es "no se facturó", `None` es "no sabemos". La primera se
        escribe; la segunda se declara como hueco.
        """
        filas = []
        try:
            for pag in range(1, 100):
                res = connekta._get('API_v2_Ventas_Facturas_DesdePedido', {
                    'paginacion': f'numPag={pag}|tamPag=100',
                    'parametros': (
                        f"f350_id_co = ''{co}'' "
                        f"AND f350_fecha >= {desde.strftime('%Y%m%d')} "
                        f"AND f350_fecha <= {hasta.strftime('%Y%m%d')}"
                    ),
                })
                if res is None:
                    return None          # breaker abierto o respuesta no-200
                rows = res.get('detalle', {}).get('Table', [])
                if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
                    break
                for r in rows:
                    # Anuladas fuera: en Siesa estado 9 es anulado, y sumarlas
                    # inflaría la facturación de la semana.
                    if str(r.get('f350_ind_estado', '')) == '9':
                        continue
                    filas.append({
                        'cantidad': float(r.get('f470_cant_base') or 0),
                        'valor': float(r.get('f470_vlr_neto') or 0),
                        'documento': f"{r.get('f350_id_tipo_docto', '')}"
                                     f"-{r.get('f350_consec_docto', '')}",
                        # Misma consulta, segunda salida — igual que hace el
                        # cargador TXT: "mismo archivo, misma pasada".
                        'referencia': (r.get('f120_referencia') or '').strip(),
                    })
                if len(rows) < 100:
                    break
        except Exception as e:
            logger.error('[VIGIA] CO %s: fallo consultando facturas: %s', co, e)
            return None
        return filas

    @staticmethod
    def _upsert_serie(serie, semana, valor, registros):
        """Escribe o actualiza una fila PRODUCCION. Devuelve True si es nueva.

        **Nunca pisa una fila HISTORICO.** La línea base entró por el TXT una
        sola vez y no se puede reconstruir: sin las 26 semanas de referencia el
        CUSUM queda ciego seis meses.
        """
        existente = SerieVigia.query.filter_by(serie=serie, semana=semana).first()
        if existente is not None:
            if existente.fuente == 'HISTORICO':
                logger.warning(
                    '[VIGIA] %s %s es HISTORICO — no se sobrescribe con producción',
                    serie, semana)
                return False
            existente.valor = valor
            existente.registros = registros
            return False
        db.session.add(SerieVigia(serie=serie, semana=semana, valor=valor,
                                  registros=registros, fuente='PRODUCCION'))
        return True

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
            func.max(StockSiesa.updated_at)
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
            delta_dias = (hoy_bogota() - ultima_serie).days
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


CANON_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'docs', 'canon_florencia.json')


def cargar_canon(path=None):
    """Lee el canon de reproducción desde el repo. None si no existe."""
    import json
    try:
        with open(path or CANON_PATH, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError) as e:
        logger.warning('[VIGIA] No se pudo leer el canon: %s', e)
        return None


def sha256_archivo(path):
    """SHA-256 de un archivo, en bloques (los TXT de Siesa pueden ser grandes)."""
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for bloque in iter(lambda: f.read(65536), b''):
            h.update(bloque)
    return h.hexdigest()


def _verificar_parametros(canon):
    """Prueba 0 — Los parámetros de producción son los de la corrida certificada.

    Va antes que todo: con k o ventana distintos, una divergencia en S- no dice
    nada sobre la tubería. Se compara primero para no acusar al código de algo
    que causó la configuración.
    """
    esperados = (canon or {}).get('parametros') or {}
    actuales = {
        'k': CUSUM_K,
        'h_alarma': CUSUM_H_ALARMA,
        'h_aviso': CUSUM_H_AVISO,
        'ventana_referencia_semanas': VENTANA_REF,
    }

    fallos = []
    for nombre, esperado in esperados.items():
        if nombre.startswith('_') or nombre not in actuales:
            continue
        if float(actuales[nombre]) != float(esperado):
            fallos.append(f'{nombre}: canon {esperado}, produccion {actuales[nombre]}')

    return {
        'prueba': '0. Parámetros',
        'ok': not fallos,
        'observado': actuales,
        'esperado': {k: v for k, v in esperados.items() if not k.startswith('_')} or None,
        'fallos': fallos,
    }


def _verificar_insumos(canon, insumos):
    """Prueba 0b — Los TXT son los mismos del backtest original.

    Condición esencial: la prueba de reproducción solo juzga la tubería si los
    insumos coinciden. Con archivos distintos una divergencia es legítima y se
    reporta como 'insumos distintos', nunca como fallo de tubería.
    """
    registro = (canon or {}).get('insumos') or {}
    esperados = {a['sha256']: a.get('archivo') for a in registro.get('archivos', [])}

    if not registro.get('registrado') or not esperados:
        return {
            'prueba': '0b. Insumos',
            'ok': True,
            'no_verificable': True,
            'observado': None,
            'fallos': [],
            'aviso': ('El canon no tiene SHA-256 registrados. Regístralos con '
                      'scripts/registrar_canon_insumos.py sobre los TXT originales; '
                      'sin ellos la prueba 2 no puede distinguir un fallo de tubería '
                      'de un cambio de insumos.'),
        }

    if not insumos:
        return {
            'prueba': '0b. Insumos',
            'ok': True,
            'no_verificable': True,
            'observado': None,
            'fallos': [],
            'aviso': ('Canon con insumos registrados, pero no se pasaron archivos '
                      'para comparar. Usa --insumos <archivos.txt>.'),
        }

    hallados, desconocidos = {}, []
    for path in insumos:
        try:
            h = sha256_archivo(path)
        except OSError as e:
            desconocidos.append(f'{path}: {e}')
            continue
        hallados[h] = os.path.basename(path)

    faltantes = [f'{nombre or "?"} ({h[:12]}...)'
                 for h, nombre in esperados.items() if h not in hallados]
    sobrantes = [f'{nombre} ({h[:12]}...)'
                 for h, nombre in hallados.items() if h not in esperados]

    fallos = list(desconocidos)
    coinciden = not faltantes and not sobrantes

    return {
        'prueba': '0b. Insumos',
        'ok': coinciden and not fallos,
        'insumos_distintos': not coinciden,
        'observado': {'archivos_comparados': len(hallados),
                      'esperados': len(esperados),
                      'faltantes': faltantes, 'sobrantes': sobrantes},
        'fallos': fallos,
        'aviso': ('Los insumos NO son los del backtest original. Una divergencia en '
                  'la prueba 2 sería legítima y no prueba nada sobre la tubería.')
        if not coinciden else None,
    }


def _verificar_conteo(esperado):
    """Prueba 1 — Conteo. Semanas, C.O.s y totales de la carga histórica."""
    from sqlalchemy import func

    filas = db.session.query(
        SerieVigia.serie,
        func.count(SerieVigia.id).label('semanas'),
        func.min(SerieVigia.semana).label('desde'),
        func.max(SerieVigia.semana).label('hasta'),
        func.sum(SerieVigia.valor).label('total'),
    ).filter(SerieVigia.fuente == 'HISTORICO').group_by(SerieVigia.serie).all()

    # Los C.O. van como sufijo de la serie: facturas_006, despachos_006...
    cos, semanas = set(), set()
    for f in filas:
        if '_' in f.serie:
            cos.add(f.serie.rsplit('_', 1)[1])
    for (s,) in db.session.query(SerieVigia.semana).filter(
            SerieVigia.fuente == 'HISTORICO').distinct().all():
        semanas.add(s)

    detalle = [{
        'serie': f.serie,
        'semanas': f.semanas,
        'desde': f.desde.isoformat() if f.desde else None,
        'hasta': f.hasta.isoformat() if f.hasta else None,
        'total': float(f.total or 0),
    } for f in filas]

    obs = {'semanas_distintas': len(semanas), 'cos_distintos': len(cos),
           'series': len(filas), 'cos': sorted(cos)}

    fallos = []
    if esperado.get('semanas') and len(semanas) != esperado['semanas']:
        fallos.append(f"semanas: esperadas {esperado['semanas']}, halladas {len(semanas)}")
    if esperado.get('cos') and len(cos) != esperado['cos']:
        fallos.append(f"C.O.s: esperados {esperado['cos']}, hallados {len(cos)}")
    if not filas:
        fallos.append('no hay ninguna serie marcada HISTORICO — el TXT no se cargó')

    return {
        'prueba': '1. Conteo',
        'ok': not fallos,
        'observado': obs,
        'esperado': {k: v for k, v in esperado.items() if k in ('semanas', 'cos')} or None,
        'fallos': fallos,
        'detalle': detalle,
    }


def _verificar_backtest(esperado):
    """Prueba 2 — Reproducción del canon. backtest_florencia debe dar el mismo número.

    Los valores canónicos (semana y S-) NO viven en el código: se pasan como
    esperado. Si no se pasan, la prueba reporta lo observado y queda en
    'pendiente' — nunca inventa un valor de referencia.
    """
    resultado = VigiaService.backtest_florencia()
    if 'error' in resultado:
        return {'prueba': '2. Reproducción del canon', 'ok': False,
                'fallos': [resultado['error']], 'observado': None, 'esperado': None}

    primera = resultado.get('primera_alarma_baja')
    obs = {
        'aprobado': resultado.get('aprobado'),
        'semana': (primera or {}).get('semana'),
        's_minus': (primera or {}).get('s_minus'),
        'severidad': (primera or {}).get('severidad'),
        'mu_ref': resultado.get('mu_ref'),
        'sigma_ref': resultado.get('sigma_ref'),
        'total_semanas': resultado.get('total_semanas'),
    }

    fallos, pendiente = [], False
    if not resultado.get('aprobado'):
        fallos.append('el CUSUM no detectó ninguna alarma BAJA en facturas_006')

    sem_esp, s_esp = esperado.get('semana_alarma'), esperado.get('s_minus')
    tol = esperado.get('tolerancia_s_minus', 0.05)
    if sem_esp and obs['semana'] != sem_esp:
        fallos.append(f"semana de alarma: esperada {sem_esp}, obtenida {obs['semana']}")
    if s_esp is not None and obs['s_minus'] is not None:
        # Tolerancia por punto flotante, no por permisividad estadística.
        # Si falla, se investiga la diferencia — jamás se afloja el criterio.
        if abs(float(obs['s_minus']) - float(s_esp)) > float(tol):
            fallos.append(f"S-: esperado {s_esp} (±{tol}), obtenido {obs['s_minus']}")
    if not sem_esp and s_esp is None:
        pendiente = True

    return {
        'prueba': '2. Reproducción del canon',
        'ok': not fallos,
        'pendiente_valor_referencia': pendiente,
        'observado': obs,
        'esperado': {'semana_alarma': sem_esp, 's_minus': s_esp},
        'fallos': fallos,
    }


def _verificar_alarma_persiste():
    """Prueba 3 — La alarma de Florencia queda abierta en producción.

    No es residuo del backtest: es el activo que espera su ritual de cierre.
    """
    abiertas = AlarmaVigia.query.filter_by(
        serie='facturas_006', tipo='BAJA', cerrada=False).all()

    historicas = SerieVigia.query.filter_by(
        serie='facturas_006', fuente='HISTORICO').count()

    fallos = []
    if not abiertas:
        fallos.append('no hay alarma BAJA abierta en facturas_006')
    if historicas == 0:
        fallos.append('facturas_006 no tiene semanas marcadas HISTORICO')

    return {
        'prueba': '3. La alarma persiste',
        'ok': not fallos,
        'observado': {
            'alarmas_baja_abiertas': len(abiertas),
            'semanas_historicas_facturas_006': historicas,
            'alarmas': [{'id': a.id, 'semana': a.semana.isoformat(),
                         's_valor': float(a.s_valor), 'severidad': a.severidad}
                        for a in abiertas],
        },
        'fallos': fallos,
    }


def verificar_carga_historica(semanas=None, cos=None, semana_alarma=None,
                              s_minus=None, insumos=None, canon_path=None):
    """
    Arnés de verificación de la carga histórica.

    Se corre UNA VEZ, después de subir los TXT y antes de devolver
    VIGIA_CARGAR_TXT a false. Si pasa, la línea base está certificada y la
    ingesta Connekta puede construirse encima sabiendo qué convenciones heredar.

    Orden deliberado — se descartan primero las causas que NO son la tubería:
      0.  Parámetros — k, h, ventana iguales a los de la corrida certificada
      0b. Insumos    — SHA-256 de los TXT iguales a los del backtest original
      1.  Conteo     — semanas, C.O.s y totales de lo marcado HISTORICO
      2.  Canon      — misma semana de alarma y mismo S- (±tolerancia)
      3.  Alarma     — la alarma de Florencia queda abierta en producción

    El canon (valores, parámetros, procedencia, hashes) se lee de
    docs/canon_florencia.json. Los argumentos explícitos lo sobreescriben.
    """
    canon = cargar_canon(canon_path)
    c_esp = (canon or {}).get('esperado') or {}

    esperado = {
        'semanas': semanas,
        'cos': cos,
        'semana_alarma': semana_alarma or c_esp.get('semana_alarma'),
        's_minus': s_minus if s_minus is not None else c_esp.get('s_minus'),
        'tolerancia_s_minus': c_esp.get('tolerancia_s_minus', 0.05),
    }

    def _seguro(nombre, fn, *a):
        """Una prueba que revienta debe reportarse, no tumbar el arnés.

        Corre en un momento tenso: un traceback crudo cuesta minutos que no hay.
        """
        try:
            return fn(*a)
        except Exception as e:
            msg = str(e)
            if 'no such table' in msg or 'does not exist' in msg:
                # Causa conocida: no merece traceback, el reporte ya lo explica
                logger.warning('[VIGIA_VERIFICACION] %s: tablas ausentes', nombre)
                msg = ('las tablas del Vigía no existen en esta base — '
                       'falta correr flask db upgrade')
            else:
                logger.exception('[VIGIA_VERIFICACION] %s falló: %s', nombre, e)
            return {'prueba': nombre, 'ok': False, 'observado': None,
                    'fallos': [msg]}

    p_param = _verificar_parametros(canon)
    p_insumos = _seguro('0b. Insumos', _verificar_insumos, canon, insumos)

    pruebas = [
        p_param,
        p_insumos,
        _seguro('1. Conteo', _verificar_conteo, esperado),
        _seguro('2. Reproducción del canon', _verificar_backtest, esperado),
        _seguro('3. La alarma persiste', _verificar_alarma_persiste),
    ]

    # Una divergencia con parámetros o insumos distintos no acusa a la tubería
    contexto_valido = p_param['ok'] and not p_insumos.get('insumos_distintos')

    return {
        'certificado': all(p['ok'] for p in pruebas),
        'contexto_comparable': contexto_valido,
        'canon': {
            'cargado': canon is not None,
            'procedencia': (canon or {}).get('procedencia'),
        },
        'pruebas': pruebas,
        'nota': ('Certificado = todas las pruebas en verde. Solo entonces: '
                 'VIGIA_CARGAR_TXT=false y arranque de la ingesta Connekta. '
                 'Si la prueba 2 falla con contexto comparable, se investiga la '
                 'diferencia — no se afloja el criterio.'),
    }


def _con_evaluacion(resultado):
    """Alimentar y evaluar son dos pasos, y el segundo faltaba.

    Se envuelve para que **ninguna salida del cron pueda saltárselo**: había un
    `return` temprano por la variable de la ingesta de facturación que dejaba
    sin evaluar hasta las series que sí se habían alimentado.

    Si la evaluación revienta no se pierde lo alimentado — eso ya está
    commiteado y vale por sí solo.
    """
    salida = dict(resultado or {})
    try:
        salida['evaluacion'] = evaluar_series_vivas()
    except Exception as e:
        logger.exception('[VIGIA_SCHEDULER] la evaluación falló entera: %s', e)
        salida['evaluacion'] = {'error': str(e)}
    return salida


def evaluar_series_vivas() -> dict:
    """Corre el CUSUM sobre **todas** las series con datos. Exige contexto.

    Hasta el 2026-08-15 esto no existía y nadie lo hacía: los dos únicos
    llamadores de `ejecutar_cusum` en producción eran rutas manuales —el clic
    del panel y el backtest—, así que **una serie que nadie clickeaba no se
    evaluaba nunca**.

    El cron semanal solo alimentaba. Vigía no era un detector: era un
    graficador bajo demanda, y un desplome producía cero alarmas hasta que
    alguien entrara al panel y le hiciera clic a esa serie exacta.

    Lo que lo volvía difícil de ver: el test que protege el cron verifica que
    `vigia_service` esté en la lista de schedulers esenciales — mide que el
    **alimentador** arranque, no que el **detector** corra. Un trinquete sobre
    una proxy.

    Una serie que falla no detiene a las demás: el motivo por el que existe
    esto es que el silencio de una no se coma al resto.
    """
    series = [s for (s,) in db.session.query(SerieVigia.serie).distinct().all()]
    evaluadas, alarmas, sin_datos, fallidas = 0, 0, [], []

    for nombre in sorted(series):
        try:
            r = VigiaService.ejecutar_cusum(nombre)
        except Exception as e:
            fallidas.append(f'{nombre}: {e}')
            logger.exception('[VIGIA_SCHEDULER] falló el CUSUM de %s', nombre)
            continue
        if r.get('error'):
            # Sin semanas suficientes todavía. No es un fallo — pero se cuenta,
            # porque «0 alarmas» y «no se pudo evaluar» no pueden verse igual.
            sin_datos.append(nombre)
            continue
        evaluadas += 1
        alarmas += r.get('alarmas_nuevas', 0) or 0

    if alarmas:
        # Hoy este log es el ÚNICO canal: no existe notificación por correo
        # pese a que el docstring del módulo dice «notifica». Mientras eso siga
        # así, que al menos grite en el nivel correcto.
        logger.error('[VIGIA_SCHEDULER] %d alarma(s) nueva(s) sobre %d serie(s) '
                     'evaluadas — nadie las notifica todavía, hay que abrir el '
                     'panel', alarmas, evaluadas)
    else:
        logger.info('[VIGIA_SCHEDULER] %d serie(s) evaluadas, sin alarmas nuevas '
                    '(%d sin semanas suficientes)', evaluadas, len(sin_datos))
    if fallidas:
        logger.error('[VIGIA_SCHEDULER] series que no se pudieron evaluar: %s', fallidas)

    return {'series_evaluadas': evaluadas, 'alarmas_nuevas': alarmas,
            'sin_semanas_suficientes': sin_datos, 'fallidas': fallidas}


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

    import os

    with ctx_app.app_context():
        resultado = None
        try:
            resultado = VigiaService.alimentar_adopcion_picking()
            logger.info('[VIGIA_SCHEDULER] Series vivas alimentadas — %d semanas, %d series nuevas',
                        resultado.get('semanas_procesadas', 0), resultado.get('series_creadas', 0))
        except Exception as e:
            logger.exception('[VIGIA_SCHEDULER] Fallo alimentando series vivas: %s', e)

        # Las tres series de negocio: nacen APAGADAS (regla 10).
        #
        # El primer ciclo no es el estado estable: consulta Siesa por CADA C.O.
        # y escribe series que el CUSUM va a evaluar de inmediato. Encenderlo
        # sin haber verificado que la agregación coincide con la línea base
        # convierte una diferencia de método en una alarma de colapso.
        #
        # Se enciende con VIGIA_INGESTA_FACTURACION=true DESPUÉS de correr
        # `alimentar_series_facturacion` a mano sobre una semana ya cargada por
        # el TXT y comprobar que los tres números dan lo mismo.
        if os.getenv('VIGIA_INGESTA_FACTURACION', '').lower() != 'true':
            logger.info('[VIGIA_SCHEDULER] Ingesta de facturación APAGADA '
                        '(VIGIA_INGESTA_FACTURACION != true)')
            # **Se evalúa igual.** Este `return` se llevaba por delante la
            # detección entera: `adopcion_picking` y `brecha_picking` se
            # alimentan siempre y son evaluables, y quedaban sin mirar por una
            # variable que gobierna OTRA cosa.
            return _con_evaluacion(resultado)

        try:
            fact = VigiaService.alimentar_series_facturacion()
            if fact.get('cos_sin_dato'):
                # No es un detalle: un C.O. sin dato tres semanas seguidas es
                # indistinguible de un C.O. que dejó de facturar, y el CUSUM no
                # lo va a ver porque no hay fila que evaluar.
                logger.error('[VIGIA_SCHEDULER] C.O. SIN DATO esta semana: %s — '
                             'no se escribió cero, quedó el hueco declarado',
                             fact['cos_sin_dato'])
            logger.info('[VIGIA_SCHEDULER] Facturación %s — %d C.O., %d series',
                        fact.get('semana'), fact.get('cos_procesados', 0),
                        fact.get('series_escritas', 0))
        except Exception as e:
            logger.exception('[VIGIA_SCHEDULER] Fallo en ingesta de facturación: %s', e)

        return _con_evaluacion(resultado)


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
