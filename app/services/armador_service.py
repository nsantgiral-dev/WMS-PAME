"""
ArmadorService — ROP dual (M0.4) + Armador de Contenedor.

ROP dual:
  Nacional: LT = 5d, σ_LT = 2d (conservador)
  China: LT = 105d, σ_LT = 15d (conservador) → se actualiza con datos reales de contenedores

Armador de contenedor:
  Calcula déficit por SKU China, aplica gatillo dual (acumulación + peligro constitucional),
  rellena por margen/CBM, respeta doble restricción física (CBM + peso) y restricción de caja
  (presupuesto tesorería). Genera borrador de OC, NUNCA OC directa (G4 vigente).

  Corre en modo shadow hasta que G5 esté completo.
"""
import logging
import math
from datetime import date, timedelta
from collections import defaultdict
from app.extensions import db

logger = logging.getLogger(__name__)

# Parámetros de contenedor (configurables)
CONTENEDOR_PARAMS = {
    '40STD': {'cbm_util': 67.0, 'payload_kg': 26500},
    '40HC': {'cbm_util': 76.0, 'payload_kg': 26500},
}
FACTOR_UTILIZACION = 0.90  # objetivo de armado: 90% del CBM útil

# Lead times conservadores (se actualizan con datos reales)
LT_NACIONAL_DIAS = 5
SIGMA_LT_NACIONAL = 2
LT_CHINA_DIAS = 105
SIGMA_LT_CHINA = 15  # conservador — se actualiza con ≥6 contenedores

# Restricción anti-500-días
MAX_COBERTURA_RELLENO_DIAS = 180

# Marcas China conocidas (se cruzan con producto.marca_siesa)
MARCAS_CHINA = {'M003', 'M009', 'M175'}


class ArmadorService:

    @staticmethod
    def calcular_sigma_lt_real() -> dict:
        """
        Calcula σ_LT real de contenedores con fechas completas.
        Con ≥6 observaciones, reemplaza el default conservador.
        """
        from app.models.importacion import Contenedor
        import statistics

        contenedores = Contenedor.query.filter(
            Contenedor.fecha_oc.isnot(None),
            Contenedor.fecha_recepcion_cedi.isnot(None),
        ).all()

        lead_times = [c.lead_time_real for c in contenedores if c.lead_time_real]

        if len(lead_times) < 3:
            return {
                'n': len(lead_times),
                'lt_medio': LT_CHINA_DIAS,
                'sigma_lt': SIGMA_LT_CHINA,
                'fuente': 'DEFAULT_CONSERVADOR',
                'nota': f'Solo {len(lead_times)} contenedores con fechas completas — usando defaults',
            }

        lt_medio = statistics.mean(lead_times)
        sigma_lt = statistics.stdev(lead_times) if len(lead_times) > 1 else SIGMA_LT_CHINA
        fuente = 'MEDIDO' if len(lead_times) >= 6 else 'PARCIAL'

        return {
            'n': len(lead_times),
            'lt_medio': round(lt_medio, 1),
            'sigma_lt': round(sigma_lt, 1),
            'lead_times': lead_times,
            'fuente': fuente,
        }

    @staticmethod
    def rop_dual(nivel_servicio: float = 0.95) -> dict:
        """
        M0.4 — ROP dual: punto de reorden por SKU según origen.

        Nacional: ROP = d_avg × LT_nac + z × σ_d × √LT_nac
        China:    ROP = d_avg × LT_chi + z × σ_d × √LT_chi + en_transito_ajuste

        El en_transito se resta de la posición de inventario, no del ROP.
        posicion = stock_actual + en_transito - backorders

        Args:
            nivel_servicio: 0.95 = z_score ~1.645

        Returns: {nacional: [...], china: [...], sigma_lt_china}
        """
        from app.services.kardex_service import KardexService, KardexMovimiento, CONCEPTOS_VENTA
        from app.models.producto import Producto
        from app.models.importacion import ItemEnTransito
        from sqlalchemy import func

        # z-score para nivel de servicio
        from app.services.kardex_service import _norm_ppf
        z = _norm_ppf(nivel_servicio)

        # σ_LT China (medido o conservador)
        sigma_lt_info = ArmadorService.calcular_sigma_lt_real()
        lt_china = sigma_lt_info['lt_medio']
        sigma_lt_china = sigma_lt_info['sigma_lt']

        # Demanda diaria promedio y σ por SKU (últimos 12 meses, nivel RED)
        fecha_limite = date.today() - timedelta(days=365)
        dias_ventana = (date.today() - fecha_limite).days

        demanda_por_sku = (
            db.session.query(
                KardexMovimiento.referencia,
                func.sum(KardexMovimiento.cantidad).label('total'),
                func.count(func.distinct(KardexMovimiento.fecha)).label('dias_con_demanda'),
            )
            .filter(KardexMovimiento.fecha >= fecha_limite)
            .filter(KardexMovimiento.concepto.in_(CONCEPTOS_VENTA))
            .filter(KardexMovimiento.naturaleza == 2)
            .group_by(KardexMovimiento.referencia)
            .all()
        )

        # En tránsito por producto
        transito = dict(
            db.session.query(
                Producto.codigo_siesa,
                func.sum(ItemEnTransito.cantidad),
            )
            .join(Producto, ItemEnTransito.producto_id == Producto.id)
            .filter(ItemEnTransito.estado.in_(['NAVEGANDO', 'EN_PUERTO', 'NACIONALIZACION', 'EN_RUTA_CEDI']))
            .group_by(Producto.codigo_siesa)
            .all()
        )

        # Stock actual
        from app.models.stock_siesa import StockSiesa
        stock = dict(
            db.session.query(
                StockSiesa.codigo_siesa,
                func.sum(StockSiesa.existencia),
            )
            .group_by(StockSiesa.codigo_siesa)
            .all()
        )

        # Productos con origen
        productos_origen = dict(
            db.session.query(Producto.codigo_siesa, Producto.origen)
            .filter(Producto.codigo_siesa.isnot(None))
            .all()
        )

        resultados_nac = []
        resultados_chi = []

        for row in demanda_por_sku:
            ref = row.referencia.strip() if row.referencia else ''
            if not ref:
                continue

            d_total = float(row.total or 0)
            d_avg = d_total / dias_ventana  # demanda diaria promedio

            # σ_d estimada (asumiendo distribución uniforme como proxy)
            dias_demanda = int(row.dias_con_demanda or 1)
            d_por_evento = d_total / dias_demanda if dias_demanda > 0 else 0

            origen = (productos_origen.get(ref) or '').upper()
            es_china = origen == 'CHINA' or any(
                m in (productos_origen.get(ref) or '') for m in MARCAS_CHINA
            )

            stock_actual = float(stock.get(ref, 0) or 0)
            qty_transito = float(transito.get(ref, 0) or 0)
            posicion = stock_actual + qty_transito

            if es_china:
                lt = lt_china
                sigma_lt = sigma_lt_china
                rop = d_avg * lt + z * d_avg * math.sqrt(sigma_lt)
                safety_stock = z * d_avg * math.sqrt(sigma_lt)
                s_objetivo = rop + d_avg * lt  # Base-stock (review period = LT)
                cobertura = posicion / d_avg if d_avg > 0 else 999
                deficit = max(0, s_objetivo - posicion)

                resultados_chi.append({
                    'referencia': ref,
                    'd_avg_diaria': round(d_avg, 4),
                    'rop': round(rop),
                    'safety_stock': round(safety_stock),
                    's_objetivo': round(s_objetivo),
                    'stock_actual': round(stock_actual),
                    'en_transito': round(qty_transito),
                    'posicion': round(posicion),
                    'deficit': round(deficit),
                    'cobertura_dias': round(cobertura, 1),
                    'lt_dias': lt,
                    'sigma_lt': sigma_lt,
                })
            else:
                lt = LT_NACIONAL_DIAS
                rop = d_avg * lt + z * d_avg * math.sqrt(SIGMA_LT_NACIONAL)
                safety_stock = z * d_avg * math.sqrt(SIGMA_LT_NACIONAL)
                cobertura = posicion / d_avg if d_avg > 0 else 999

                resultados_nac.append({
                    'referencia': ref,
                    'd_avg_diaria': round(d_avg, 4),
                    'rop': round(rop),
                    'safety_stock': round(safety_stock),
                    'stock_actual': round(stock_actual),
                    'cobertura_dias': round(cobertura, 1),
                    'bajo_rop': posicion < rop,
                })

        resultados_nac.sort(key=lambda x: x['cobertura_dias'])
        resultados_chi.sort(key=lambda x: x['cobertura_dias'])

        return {
            'nivel_servicio': nivel_servicio,
            'z_score': round(z, 3),
            'nacional': {
                'lt_dias': LT_NACIONAL_DIAS,
                'sigma_lt': SIGMA_LT_NACIONAL,
                'total': len(resultados_nac),
                'bajo_rop': sum(1 for r in resultados_nac if r.get('bajo_rop')),
                'items': resultados_nac,
            },
            'china': {
                'lt_dias': lt_china,
                'sigma_lt': sigma_lt_china,
                'sigma_lt_fuente': sigma_lt_info['fuente'],
                'total': len(resultados_chi),
                'con_deficit': sum(1 for r in resultados_chi if r['deficit'] > 0),
                'items': resultados_chi,
            },
        }

    @staticmethod
    def armar_contenedor(tipo_contenedor: str = '40STD',
                         presupuesto_cop: float = None) -> dict:
        """
        Armador de Contenedor — propone la mejor composición.

        Lógica:
        1. Calcula déficit por SKU China (S_objetivo - posición)
        2. Convierte a cajas (respetando MOQ)
        3. Aplica gatillo dual (acumulación 90% CBM | peligro constitucional)
        4. Si incompleto, rellena por margen/CBM
        5. Aplica restricción anti-500-días (max 180 días cobertura)
        6. Aplica doble restricción física (CBM + peso)
        7. Aplica restricción de caja (presupuesto tesorería)

        Corre en MODO SHADOW si G5 no está completo.

        Returns: {modo, gatillo, contenedor, items, excluidos, barras, ...}
        """
        from app.models.importacion import FichaImportacion, ItemEnTransito
        from app.models.producto import Producto
        from sqlalchemy import func

        params = CONTENEDOR_PARAMS.get(tipo_contenedor, CONTENEDOR_PARAMS['40STD'])
        cbm_objetivo = params['cbm_util'] * FACTOR_UTILIZACION
        payload_kg = params['payload_kg']

        # Verificar G5: ¿hay fichas suficientes?
        total_fichas = FichaImportacion.query.filter(
            FichaImportacion.fuente != 'ESTIMADO'
        ).count()
        total_china = Producto.query.filter(Producto.origen == 'CHINA').count()
        cobertura_fichas = total_fichas / total_china * 100 if total_china > 0 else 0

        modo = 'SHADOW' if cobertura_fichas < 90 else 'ACTIVO'

        # ROP dual para obtener déficits China
        rop = ArmadorService.rop_dual()
        items_china = rop['china']['items']

        # Cruzar con fichas de importación
        fichas = {}
        for f in FichaImportacion.query.join(Producto).all():
            ref = f.producto.codigo_siesa
            if ref:
                fichas[ref.strip()] = f

        # Construir lista de déficit con cubicaje
        items_deficit = []
        excluidos = []

        for item in items_china:
            ref = item['referencia']
            ficha = fichas.get(ref)

            if not ficha:
                excluidos.append({'referencia': ref, 'motivo': 'SIN_FICHA'})
                continue
            if ficha.fuente == 'ESTIMADO':
                excluidos.append({'referencia': ref, 'motivo': 'FICHA_ESTIMADA'})
                continue

            deficit_u = item['deficit']
            if deficit_u <= 0:
                continue

            u_por_caja = ficha.unidades_por_caja or 1
            moq_cajas = ficha.moq_cajas or 1
            cajas_necesarias = math.ceil(deficit_u / u_por_caja)
            # Redondear a MOQ
            if cajas_necesarias % moq_cajas != 0:
                cajas_necesarias = math.ceil(cajas_necesarias / moq_cajas) * moq_cajas

            cbm = cajas_necesarias * float(ficha.cbm_por_caja or 0)
            peso = cajas_necesarias * float(ficha.peso_kg_por_caja or 0)
            costo_fob = cajas_necesarias * u_por_caja * float(ficha.costo_fob_usd or 0)

            items_deficit.append({
                'referencia': ref,
                'deficit_unidades': deficit_u,
                'cajas': cajas_necesarias,
                'cbm': round(cbm, 3),
                'peso_kg': round(peso, 1),
                'costo_fob_usd': round(costo_fob, 2),
                'cobertura_dias': item['cobertura_dias'],
                'd_avg': item['d_avg_diaria'],
                'tipo': 'DEFICIT',
            })

        # Ordenar por urgencia (menor cobertura primero)
        items_deficit.sort(key=lambda x: x['cobertura_dias'])

        # Gatillo dual
        cbm_total_deficit = sum(i['cbm'] for i in items_deficit)
        lt_china = rop['china']['lt_dias']
        sigma_lt = rop['china']['sigma_lt']
        umbral_peligro = lt_china + 1.0 * sigma_lt

        gatillo_a = cbm_total_deficit >= cbm_objetivo
        gatillo_b = any(i['cobertura_dias'] < umbral_peligro for i in items_deficit)
        gatillo = 'ACUMULACION' if gatillo_a else ('PELIGRO_CONSTITUCIONAL' if gatillo_b else 'NINGUNO')

        # Armar contenedor: llenar hasta restricción física
        contenedor_items = []
        cbm_acum = 0.0
        peso_acum = 0.0

        for item in items_deficit:
            if cbm_acum + item['cbm'] > cbm_objetivo:
                break
            if peso_acum + item['peso_kg'] > payload_kg:
                break
            contenedor_items.append(item)
            cbm_acum += item['cbm']
            peso_acum += item['peso_kg']

        # Relleno inteligente (si gatillo B y queda espacio)
        items_relleno = []
        if gatillo == 'PELIGRO_CONSTITUCIONAL' and cbm_acum < cbm_objetivo:
            # Obtener productos China con demanda y margen para ranking
            for item in items_china:
                ref = item['referencia']
                ficha = fichas.get(ref)
                if not ficha or ficha.fuente == 'ESTIMADO':
                    continue
                if ref in {i['referencia'] for i in contenedor_items}:
                    continue
                if item['deficit'] <= 0:
                    continue

                u_por_caja = ficha.unidades_por_caja or 1
                cbm_caja = float(ficha.cbm_por_caja or 0)
                peso_caja = float(ficha.peso_kg_por_caja or 0)
                costo_fob = float(ficha.costo_fob_usd or 0)

                # Proxy de margen: precio_venta - costo (simplificado)
                prod = Producto.query.filter_by(codigo_siesa=ref).first()
                margen_u = (prod.precio_venta or 0) - (prod.precio_compra or 0) if prod else 0
                margen_cbm = (margen_u * u_por_caja / cbm_caja) if cbm_caja > 0 else 0

                # Restricción anti-500-días
                d_avg = item['d_avg_diaria']
                unidades_propuestas = u_por_caja  # 1 caja mínimo
                cobertura_post = (item['posicion'] + unidades_propuestas) / d_avg if d_avg > 0 else 999
                if cobertura_post > MAX_COBERTURA_RELLENO_DIAS:
                    continue

                items_relleno.append({
                    'referencia': ref,
                    'cajas': 1,
                    'cbm': round(cbm_caja, 3),
                    'peso_kg': round(peso_caja, 1),
                    'costo_fob_usd': round(costo_fob * u_por_caja, 2),
                    'margen_por_cbm': round(margen_cbm, 2),
                    'cobertura_post': round(cobertura_post, 1),
                    'tipo': 'RELLENO',
                })

            # Ordenar por margen/CBM (la métrica reina)
            items_relleno.sort(key=lambda x: x['margen_por_cbm'], reverse=True)

            # Llenar espacio restante
            for item in items_relleno:
                if cbm_acum + item['cbm'] > cbm_objetivo:
                    continue
                if peso_acum + item['peso_kg'] > payload_kg:
                    continue
                contenedor_items.append(item)
                cbm_acum += item['cbm']
                peso_acum += item['peso_kg']

        # Restricción de caja (presupuesto tesorería)
        valor_fob_total = sum(i['costo_fob_usd'] for i in contenedor_items)
        factor_nac = 1.45  # estimado nacionalización (flete+arancel+IVA)
        valor_nac_estimado = valor_fob_total * factor_nac * 4200  # USD→COP aprox

        recorte_sugerido = []
        if presupuesto_cop and valor_nac_estimado > presupuesto_cop:
            # Recortar relleno en orden inverso de ranking
            rellenos = [i for i in contenedor_items if i['tipo'] == 'RELLENO']
            rellenos.reverse()
            for item in rellenos:
                if valor_nac_estimado <= presupuesto_cop:
                    break
                contenedor_items.remove(item)
                recorte_sugerido.append(item)
                cbm_acum -= item['cbm']
                peso_acum -= item['peso_kg']
                valor_fob_total -= item['costo_fob_usd']
                valor_nac_estimado = valor_fob_total * factor_nac * 4200

        # Ventana de llegada
        eta_min = date.today() + timedelta(days=int(lt_china - sigma_lt))
        eta_max = date.today() + timedelta(days=int(lt_china + sigma_lt))

        return {
            'modo': modo,
            'cobertura_fichas_pct': round(cobertura_fichas, 1),
            'gatillo': gatillo,
            'tipo_contenedor': tipo_contenedor,
            'barras': {
                'cbm_acumulado': round(cbm_acum, 2),
                'cbm_objetivo': round(cbm_objetivo, 2),
                'cbm_pct': round(cbm_acum / cbm_objetivo * 100, 1) if cbm_objetivo > 0 else 0,
                'peso_acumulado': round(peso_acum, 1),
                'peso_limite': payload_kg,
                'peso_pct': round(peso_acum / payload_kg * 100, 1) if payload_kg > 0 else 0,
                'restriccion_activa': 'PESO' if peso_acum / payload_kg > cbm_acum / cbm_objetivo else 'CBM',
            },
            'items': contenedor_items,
            'total_items': len(contenedor_items),
            'items_deficit': sum(1 for i in contenedor_items if i['tipo'] == 'DEFICIT'),
            'items_relleno': sum(1 for i in contenedor_items if i['tipo'] == 'RELLENO'),
            'excluidos': excluidos,
            'recorte_presupuesto': recorte_sugerido,
            'valor_fob_usd': round(valor_fob_total, 2),
            'valor_nacionalizado_cop_estimado': round(valor_nac_estimado),
            'presupuesto_cop': presupuesto_cop,
            'ventana_llegada': {
                'desde': eta_min.isoformat(),
                'hasta': eta_max.isoformat(),
                'nota': f'Ventana basada en LT={lt_china}d +/- σ={sigma_lt}d ({rop["china"]["sigma_lt_fuente"]})',
            },
            'sigma_lt': {
                'valor': sigma_lt,
                'fuente': rop['china']['sigma_lt_fuente'],
                'n_contenedores': ArmadorService.calcular_sigma_lt_real()['n'],
            },
        }

    @staticmethod
    def verificar_g5() -> dict:
        """Verifica el estado de todas las sub-compuertas G5."""
        from app.models.importacion import FichaImportacion, Contenedor, ItemEnTransito
        from app.models.producto import Producto

        total_china = Producto.query.filter(Producto.origen == 'CHINA').count()
        fichas_verificadas = FichaImportacion.query.filter(
            FichaImportacion.fuente != 'ESTIMADO').count()
        contenedores_completos = Contenedor.query.filter(
            Contenedor.fecha_oc.isnot(None),
            Contenedor.fecha_recepcion_cedi.isnot(None),
        ).count()
        items_transito = ItemEnTransito.query.filter(
            ItemEnTransito.estado != 'RECIBIDO').count()

        g51 = fichas_verificadas / total_china * 100 if total_china > 0 else 0
        g52 = contenedores_completos >= 3

        return {
            'g5_ok': g51 >= 90 and g52,
            'g5_1': {
                'nombre': 'Maestro cubicaje ≥90%',
                'valor': round(g51, 1),
                'ok': g51 >= 90,
                'detalle': f'{fichas_verificadas}/{total_china} fichas verificadas',
            },
            'g5_2': {
                'nombre': '≥3 contenedores con fechas completas',
                'valor': contenedores_completos,
                'ok': g52,
            },
            'g5_3': {
                'nombre': 'Items en tránsito registrados',
                'valor': items_transito,
                'ok': items_transito > 0 or total_china == 0,
            },
            'total_skus_china': total_china,
        }
