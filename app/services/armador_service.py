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
import os
from datetime import date, timedelta
from collections import defaultdict
from app.extensions import db


def sigma_ltd(lt_dias, sigma_d, d_avg, sigma_lt, r_dias=0):
    """
    §M0.4 — desviación de la demanda durante la exposición al riesgo.

        sigma_LTD = sqrt( (LT + R) * sigma_d^2  +  d_avg^2 * sigma_LT^2 )

    Se suman las VARIANZAS, no las desviaciones: por eso va en cuadratura.

    sigma_LT multiplica solo a LT, nunca a R: el periodo de revisión es el
    propio ciclo de compra y es determinístico — no tiene incertidumbre.

    El `sqrt(LT)` del docstring viejo es la versión simplificada, válida solo
    con lead time constante. Buenaventura no lo es: con LT=105 y sigma_LT=15,
    el término portuario aporta más varianza que el comercial.

    UNIDADES: todo en días. d_avg y sigma_d en unidades/día, LT/R/sigma_LT en
    días. Convertir en la frontera, nunca aquí dentro.
    """
    exposicion = max(float(lt_dias) + float(r_dias), 0.0)
    var_demanda = exposicion * float(sigma_d) ** 2
    var_leadtime = float(d_avg) ** 2 * float(sigma_lt) ** 2
    return math.sqrt(max(var_demanda + var_leadtime, 0.0))

logger = logging.getLogger(__name__)

# Parámetros de contenedor (configurables)
# cbm_util = volumen interno aprovechable, ya descontado el desperdicio de
# estiba. Es el número contra el que se arma; el objetivo real es
# cbm_util * FACTOR_UTILIZACION.
CONTENEDOR_PARAMS = {
    '20STD': {'etiqueta': "20' STD",  'cbm_util': 33.0, 'payload_kg': 28000},
    '40STD': {'etiqueta': "40' STD",  'cbm_util': 67.0, 'payload_kg': 26500},
    '40HC':  {'etiqueta': "40' HQ",   'cbm_util': 76.0, 'payload_kg': 26500},
}


def tipos_contenedor():
    """Catálogo para el selector del panel — una sola fuente de verdad."""
    return [
        {
            'tipo': tipo,
            'etiqueta': p['etiqueta'],
            'cbm_util': p['cbm_util'],
            'payload_kg': p['payload_kg'],
            'cbm_objetivo': round(p['cbm_util'] * FACTOR_UTILIZACION, 1),
        }
        for tipo, p in CONTENEDOR_PARAMS.items()
    ]
FACTOR_UTILIZACION = 0.90  # objetivo de armado: 90% del CBM útil

# Lead times conservadores (se actualizan con datos reales)
LT_NACIONAL_DIAS = 5
SIGMA_LT_NACIONAL = 2
LT_CHINA_DIAS = 105
SIGMA_LT_CHINA = 15  # conservador — se actualiza con ≥6 contenedores

# Periodo de revisión. China se revisa por trimestre (un contenedor cada ~90d),
# así que la exposición al riesgo es LT + R, no solo LT. Nacional se revisa de
# continuo: R = 0.
R_CHINA_DIAS = int(os.environ.get('ROP_R_CHINA_DIAS', '90'))
R_NACIONAL_DIAS = 0

# Estimador de sigma_d activo. INTERINO: sigma empírica de la serie
# descensurada. DEFINITIVO (pendiente): RMSE de un paso adelante del TSB —
# el colchón debe absorber lo que el modelo NO vio venir, no la varianza cruda.
ESTIMADOR_SIGMA_D = 'SIGMA_EMPIRICA_DESCENSURADA'

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

        # CABLE M0.2 → M0.4. La demanda entra DESCENSURADA: d_avg sobre días con
        # stock, no sobre días calendario. Antes se dividía por 365 con los días
        # agotados aportando cero — el sistema aprendía a comprar poco justo de
        # lo que siempre faltaba.
        demanda_por_sku = KardexService.demanda_descensurada(
            ventana_meses=12, nivel='red')
        dias_ventana = 360

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

        # Delta agregado — el "backtest" posible del ROP: no se puede certificar
        # una fórmula contra la historia, pero sí cuantificar el salto.
        delta = {'skus': 0, 'ss_antes': 0.0, 'ss_despues': 0.0,
                 'topados_por_cobertura': 0, 'censurados': 0}

        for ref, dem in demanda_por_sku.items():
            ref = (ref or '').strip()
            if not ref:
                continue

            d_avg = dem['d_avg']        # u/día sobre días CON stock
            sigma_d = dem['sigma_d']    # u/día
            if d_avg <= 0:
                continue

            origen = (productos_origen.get(ref) or '').upper()
            es_china = origen == 'CHINA' or any(
                m in (productos_origen.get(ref) or '') for m in MARCAS_CHINA
            )

            stock_actual = float(stock.get(ref, 0) or 0)
            qty_transito = float(transito.get(ref, 0) or 0)
            posicion = stock_actual + qty_transito

            lt = lt_china if es_china else LT_NACIONAL_DIAS
            sigma_lt = sigma_lt_china if es_china else SIGMA_LT_NACIONAL
            r = R_CHINA_DIAS if es_china else R_NACIONAL_DIAS

            s_ltd = sigma_ltd(lt, sigma_d, d_avg, sigma_lt, r_dias=0)
            safety_stock = z * s_ltd
            rop = d_avg * lt + safety_stock

            # Fórmula anterior, solo para el reporte de delta
            ss_anterior = z * d_avg * math.sqrt(sigma_lt)
            delta['skus'] += 1
            delta['ss_antes'] += ss_anterior
            delta['ss_despues'] += safety_stock
            if dem.get('censurado'):
                delta['censurados'] += 1

            cobertura = posicion / d_avg if d_avg > 0 else 999

            fila = {
                'referencia': ref,
                'd_avg_diaria': round(d_avg, 4),
                'sigma_d_diaria': round(sigma_d, 4),
                'rop': round(rop),
                'safety_stock': round(safety_stock),
                'sigma_ltd': round(s_ltd, 2),
                'stock_actual': round(stock_actual),
                'cobertura_dias': round(cobertura, 1),
                'lt_dias': lt,
                'sigma_lt': sigma_lt,
                # Procedencia: el comprador tiene que poder auditar el número
                'dias_con_stock': dem['dias_con_stock'],
                'factor_censura': dem['factor_censura'],
                'censurado': dem.get('censurado', False),
                'ss_formula_anterior': round(ss_anterior),
            }

            if es_china:
                # Revisión periódica: la exposición es LT + R. sigma_LT sigue
                # aplicando SOLO a LT — R es el propio ciclo, es determinístico.
                s_ltr = sigma_ltd(lt, sigma_d, d_avg, sigma_lt, r_dias=r)
                s_objetivo = d_avg * (lt + r) + z * s_ltr

                # Baranda dura: nada por encima de MAX_COBERTURA_RELLENO_DIAS.
                tope = d_avg * MAX_COBERTURA_RELLENO_DIAS
                topado = s_objetivo > tope
                if topado:
                    s_objetivo = tope
                    delta['topados_por_cobertura'] += 1

                fila.update({
                    'r_dias': r,
                    'sigma_ltr': round(s_ltr, 2),
                    's_objetivo': round(s_objetivo),
                    'en_transito': round(qty_transito),
                    'posicion': round(posicion),
                    'deficit': round(max(0, s_objetivo - posicion)),
                    'topado_por_cobertura': topado,
                })
                resultados_chi.append(fila)
            else:
                fila['bajo_rop'] = posicion < rop
                resultados_nac.append(fila)

        resultados_nac.sort(key=lambda x: x['cobertura_dias'])
        resultados_chi.sort(key=lambda x: x['cobertura_dias'])

        mult = (delta['ss_despues'] / delta['ss_antes']) if delta['ss_antes'] > 0 else 0

        return {
            'nivel_servicio': nivel_servicio,
            'z_score': round(z, 3),
            # Procedencia del cálculo — sin esto el número no es auditable
            'estimador_sigma_d': ESTIMADOR_SIGMA_D,
            'formula': 'sigma_LTD = sqrt((LT+R)*sigma_d^2 + d^2*sigma_LT^2)  §M0.4',
            'unidad_canonica': 'dias',
            'cobertura_max_dias': MAX_COBERTURA_RELLENO_DIAS,
            # Reporte de delta: cuánto salta el colchón al corregir la fórmula
            'delta_vs_formula_anterior': {
                'skus': delta['skus'],
                'safety_stock_antes': round(delta['ss_antes']),
                'safety_stock_despues': round(delta['ss_despues']),
                'multiplicador': round(mult, 2),
                'topados_por_cobertura': delta['topados_por_cobertura'],
                'skus_censurados': delta['censurados'],
                'aviso_censura': (
                    f"{delta['censurados']} SKU(s) sin StockDiario: su demanda esta "
                    f"CENSURADA (subestima). Correr POST /api/kardex/reconstruir."
                ) if delta['censurados'] else None,
                'nota': ('La fórmula anterior usaba z*d*sqrt(sigma_LT): sin sigma_d '
                         'y con la raíz sobre la desviación del lead time en vez de '
                         'sobre la exposición. Subestimaba el colchón.'),
            },
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
                'r_dias': R_CHINA_DIAS,
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

        # Sin fallback silencioso: armar contra el contenedor equivocado produce
        # una propuesta plausible y falsa. Mejor reventar.
        params = CONTENEDOR_PARAMS.get(tipo_contenedor)
        if params is None:
            raise ValueError(
                f'Tipo de contenedor desconocido: {tipo_contenedor}. '
                f'Válidos: {", ".join(CONTENEDOR_PARAMS)}')
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
            # Los productos del ranking, en UNA consulta. Antes era un
            # `filter_by(codigo_siesa=ref).first()` por iteración — hasta 500
            # consultas para armar un contenedor.
            _refs = [i['referencia'] for i in items_china]
            _prods = {p.codigo_siesa: p for p in
                      Producto.query.filter(Producto.codigo_siesa.in_(_refs)).all()} \
                if _refs else {}

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
                prod = _prods.get(ref)
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
            'contenedor_etiqueta': params['etiqueta'],
            'contenedor_cbm_util': params['cbm_util'],
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
