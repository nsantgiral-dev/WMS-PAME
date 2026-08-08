"""
TemporadaService — el llamador del newsvendor.

El newsvendor (kardex_service) es una calculadora pura: recibe ventas_pasadas y
devuelve Q*. Nunca tuvo quien le armara la entrada, así que el modelo existía
sin decidir nada. Esto es ese llamador.

Lo que hace, en orden:
  1. Identifica SKUs de temporada por concentración histórica dic–feb.
  2. Trae su demanda DESCENSURADA por temporada (no ventas crudas: lo que se
     agotó en enero no puede leerse como "no se vendía").
  3. Excluye lista negra y separa costo saneado de costo fantasma.
  4. Arma Cu/Co por SKU y llama al newsvendor.

REPORTE DE COBERTURA: la salida dice siempre qué fracción de la decisión cubre
el modelo. Las referencias con costo fantasma se concentran en importados —
que es exactamente el catálogo escolar. Si el modelo cubre el 40% del pedido,
hay que saberlo antes del comité, no descubrirlo en la sala.
"""
import logging
from datetime import date

from app.extensions import db
# La política única del denominador cuando falta StockDiario.
# Se importa acá y no dentro de un método: `_descensurar` la usa y es
# estático — un import local la escondería de quien lea la firma.
from app.services.kardex_service import dias_expuestos
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)

# Ventana de temporada escolar: 1-dic a 28-feb.
TEMPORADA_INICIO = (12, 1)
TEMPORADA_FIN = (2, 28)

# Un SKU es de temporada si concentra al menos esta fracción de su demanda
# anual en la ventana. 0.40 sobre una ventana que es el 25% del año ya es
# concentración clara.
UMBRAL_CONCENTRACION = 0.40

# Co = costo × (capital inmovilizado + pérdida al liquidar)
TASA_CAPITAL = 0.30
TASA_LIQUIDACION = 0.60


def temporada_de(fecha):
    """Etiqueta de temporada de una fecha. Dic-2025 y ene-2026 son la misma.

    Devuelve None fuera de la ventana.
    """
    if fecha.month == 12:
        return f'{fecha.year}-{str(fecha.year + 1)[2:]}'
    if fecha.month in (1, 2):
        return f'{fecha.year - 1}-{str(fecha.year)[2:]}'
    return None


class TemporadaService:

    @staticmethod
    def identificar_skus_temporada(umbral: float = UMBRAL_CONCENTRACION,
                                   anios: int = 3) -> dict:
        """
        SKUs cuya demanda se concentra en dic–feb.

        Mide sobre demanda descensurada por día, no sobre ventas crudas: un SKU
        que se agotó todo enero tiene ventas bajas y demanda alta, y es
        precisamente el que más importa acertar.

        Returns: {referencia: {concentracion, demanda_temporada, demanda_anual}}
        """
        from app.services.kardex_service import (
            KardexMovimiento, StockDiario, CONCEPTOS_VENTA, CONCEPTOS_DEVOLUCION)
        from sqlalchemy import func
        from collections import defaultdict

        desde = _dia_operativo().replace(year=_dia_operativo().year - anios)

        def _por_dia(conceptos, naturaleza):
            return (
                db.session.query(KardexMovimiento.referencia,
                                 KardexMovimiento.fecha,
                                 func.sum(KardexMovimiento.cantidad))
                .filter(KardexMovimiento.fecha >= desde)
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

        # Días con stock por (ref, temporada) y por (ref, año) — el denominador
        # de la descensura tiene que ser del mismo periodo que el numerador.
        dias_stock = defaultdict(int)
        for ref, f in db.session.query(StockDiario.referencia, StockDiario.fecha) \
                .filter(StockDiario.fecha >= desde) \
                .filter(StockDiario.tuvo_stock == True).all():  # noqa: E712
            dias_stock[(ref, temporada_de(f))] += 1

        dem_temp = defaultdict(lambda: defaultdict(float))
        dem_anual = defaultdict(float)
        for (ref, f), v in neto.items():
            v = max(v, 0.0)
            if v <= 0:
                continue
            dem_anual[ref] += v
            t = temporada_de(f)
            if t:
                dem_temp[ref][t] += v

        salida = {}
        for ref, total_anual in dem_anual.items():
            if total_anual <= 0:
                continue
            en_temporada = sum(dem_temp[ref].values())
            conc = en_temporada / total_anual
            if conc < umbral:
                continue
            salida[ref] = {
                'concentracion': round(conc, 3),
                'demanda_temporada': round(en_temporada, 1),
                'demanda_anual': round(total_anual, 1),
                'por_temporada': {t: round(v, 1) for t, v in sorted(dem_temp[ref].items())},
                'dias_stock_por_temporada': {
                    t: dias_stock.get((ref, t), 0) for t in dem_temp[ref]
                },
            }
        return salida

    @staticmethod
    def _descensurar(demanda, dias_con_stock, dias_ventana=90):
        """Escala la demanda de una temporada a ventana completa.

        Si un SKU tuvo stock 40 de los 90 días, lo vendido en esos 40 días es
        la tasa real: proyectarla a 90 es la demanda que hubo, no la servida.

        Devuelve `(demanda_corregida, censurado)`.

        **El denominador sale de `dias_expuestos`, no de un `min` propio.** Esta
        función reimplementaba la política —el mismo `min(dias, ventana)` y el
        mismo caso de cero— y el CLAUDE.md ya había escrito qué pasa con eso:
        *"si un fallback se parchea en dos sitios, la tercera implementación
        divergirá y esa vez nadie estará comparando"*. Era la tercera.

        Numéricamente coincidían. Lo que se perdía era **el segundo valor**:
        `dias_expuestos` devuelve si el número quedó CENSURADO, y acá se
        descartaba. Con `dias_con_stock = 0` las dos devuelven la demanda sin
        corregir —conservador, correcto— pero solo una lo dice. Ese número
        entraba al newsvendor subestimado y sin una sola marca, en una fila que
        sí declara la procedencia del costo.
        """
        n, censurado = dias_expuestos(dias_con_stock, dias_ventana)
        if n <= 0:
            return demanda, True
        return demanda * (dias_ventana / n), censurado

    @staticmethod
    def preparar_pedido_temporada(margen_pct: float = 0.40,
                                  tasa_capital: float = TASA_CAPITAL,
                                  tasa_liquidacion: float = TASA_LIQUIDACION,
                                  umbral: float = UMBRAL_CONCENTRACION) -> dict:
        """
        Arma la entrada del newsvendor y lo corre. Devuelve Q* + cobertura.

        Cu = precio_venta − costo   (margen que se pierde si falta)
        Co = costo × (capital + liquidación)   (lo que cuesta que sobre)
        """
        from app.models.producto import Producto
        from app.models.producto_bloqueado import ProductoBloqueado
        from app.services.kardex_service import KardexService

        estacionales = TemporadaService.identificar_skus_temporada(umbral=umbral)
        if not estacionales:
            return {
                'error': 'Sin SKUs de temporada identificados — ¿está cargado el kardex?',
                'cobertura': {'skus_temporada': 0},
            }

        refs = list(estacionales)
        productos = {
            p.codigo_siesa: p for p in
            Producto.query.filter(Producto.codigo_siesa.in_(refs)).all()
            if p.codigo_siesa
        }

        bloqueados = {
            p.codigo_siesa
            for p, _b in db.session.query(Producto, ProductoBloqueado)
            .join(ProductoBloqueado, ProductoBloqueado.producto_id == Producto.id)
            .filter(ProductoBloqueado.activo == True).all()  # noqa: E712
            if p.codigo_siesa
        }

        items, excluidos = [], {'lista_negra': [], 'costo_fantasma': [], 'sin_producto': []}

        # CABLE DE COSTO Y PRECIO. Antes se leía Producto.precio_compra, que la
        # sincronización de Siesa NUNCA puebla: todos los SKU salían con costo 0
        # y el newsvendor los excluía a todos. La cobertura no era 40%, era CERO.
        from app.services.costo_service import resolver_costos, resumen_por_fuente
        costos = resolver_costos(list(estacionales), margen_supuesto=margen_pct)

        for ref, info in estacionales.items():
            if ref in bloqueados:
                excluidos['lista_negra'].append(ref)
                continue

            prod = productos.get(ref)
            if prod is None:
                excluidos['sin_producto'].append(ref)
                continue

            info_costo = costos.get(ref) or {}
            costo = float(info_costo.get('costo') or 0)
            venta = float(info_costo.get('precio_venta') or 0)
            # Sin costo de NINGUNA fuente no hay Cu/Co, y sin Cu/Co el Q* sería
            # una opinión con decimales.
            if costo <= 0:
                excluidos['costo_fantasma'].append(ref)
                continue

            ventas_pasadas = []
            temporadas_censuradas = []
            for t, dem in sorted(info['por_temporada'].items()):
                dias = info['dias_stock_por_temporada'].get(t, 0)
                corregida, censurada = TemporadaService._descensurar(dem, dias)
                ventas_pasadas.append(round(corregida, 1))
                if censurada:
                    temporadas_censuradas.append(t)

            if not ventas_pasadas:
                continue

            cu = float(info_costo.get('cu') or max(venta - costo, costo * margen_pct))
            co = costo * (tasa_capital + tasa_liquidacion)

            items.append({
                'referencia': ref,
                'nombre': prod.nombre,
                'ventas_pasadas': ventas_pasadas,
                'costo_unitario': costo,
                'precio_venta': venta,
                'cu': round(cu, 2),
                'co': round(co, 2),
                'concentracion': info['concentracion'],
                # PROCEDENCIA POR FILA: un Q* sobre cotización vigente y uno
                # sobre promedio de hace 18 meses no valen lo mismo.
                'fuente_costo': info_costo.get('fuente'),
                'costo_confiable': info_costo.get('confiable', False),
                'costo_anejo': info_costo.get('anejo', False),
                'dias_antiguedad_costo': info_costo.get('dias_antiguedad'),
                # PROCEDENCIA DE LA DEMANDA, no solo del costo. La fila ya
                # declaraba si el costo era supuesto o añejo; la demanda entraba
                # sin decir nada. Un Q* sobre demanda descensurada de verdad y
                # uno sobre demanda censurada —sin StockDiario— se veían
                # idénticos, y el segundo SUBESTIMA. Es la misma regla que el
                # comentario de arriba aplica al costo.
                'demanda_censurada': bool(temporadas_censuradas),
                'temporadas_censuradas': temporadas_censuradas,
                'precio_es_supuesto': info_costo.get('precio_es_supuesto', False),
                'margen_supuesto': info_costo.get('margen_supuesto'),
                'cu_rango': info_costo.get('cu_rango'),
                'convencion_margen': info_costo.get('convencion_margen'),
            })

        resultado = KardexService.newsvendor(items, margen_pct=margen_pct,
                                             costo_exceso_pct=tasa_capital + tasa_liquidacion)

        # Devolver el nombre y la concentración a cada fila (el calculador es
        # agnóstico y solo conoce referencias)
        meta = {i['referencia']: i for i in items}
        for fila in resultado.get('items', []):
            m = meta.get(fila['referencia'], {})
            fila['nombre'] = m.get('nombre', '')
            fila['concentracion'] = m.get('concentracion')
            fila['precio_venta'] = m.get('precio_venta')
            for k in ('fuente_costo', 'costo_confiable', 'costo_anejo',
                      'dias_antiguedad_costo', 'precio_es_supuesto',
                      'margen_supuesto', 'cu_rango', 'convencion_margen'):
                fila[k] = m.get(k)

        total = len(estacionales)
        cubiertos = len(items)
        resultado['cobertura'] = {
            'skus_temporada': total,
            'cubiertos_por_modelo': cubiertos,
            'pct_cubierto': round(cubiertos / total * 100, 1) if total else 0,
            'excluidos_lista_negra': len(excluidos['lista_negra']),
            'excluidos_costo_fantasma': len(excluidos['costo_fantasma']),
            'excluidos_sin_producto': len(excluidos['sin_producto']),
            'detalle_excluidos': excluidos,
            'advertencia': (
                f'El modelo cubre {cubiertos} de {total} SKUs de temporada. '
                f'El resto se decide sin modelo — la lista paralela gobierna ahí.'
            ) if cubiertos < total else None,
        }
        # Cobertura POR FUENTE, no binaria
        resultado['cobertura']['costo'] = resumen_por_fuente(costos)
        resultado['parametros'] = {
            'margen_pct': margen_pct,
            'tasa_capital': tasa_capital,
            'tasa_liquidacion': tasa_liquidacion,
            'umbral_concentracion': umbral,
            'ventana': 'dic 1 – feb 28',
            'demanda': 'DESCENSURADA por días con stock de cada temporada',
        }
        return resultado
