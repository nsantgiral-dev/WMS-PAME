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


def proxima_temporada(hoy=None) -> dict:
    """La temporada escolar que viene (o la que está corriendo), por fecha.

    Del 1-mar al 30-nov la próxima empieza el 1-dic de este año; de dic a feb
    la temporada ESTÁ corriendo (`en_curso`). Reemplaza la etiqueta fija
    `'2026-27'` y los plazos escritos a mano («comité del 7 de agosto»), que
    envejecían solos."""
    hoy = hoy or _dia_operativo()
    if hoy.month == 12:
        inicio = date(hoy.year, 12, 1)
    elif hoy.month in (1, 2):
        inicio = date(hoy.year - 1, 12, 1)
    else:
        inicio = date(hoy.year, 12, 1)
    import calendar
    fin = date(inicio.year + 1, 2, calendar.monthrange(inicio.year + 1, 2)[1])
    return {'etiqueta': temporada_de(inicio), 'inicio': inicio, 'fin': fin,
            'en_curso': inicio <= hoy <= fin,
            'dias_para_empezar': max(0, (inicio - hoy).days)}


def fechas_limite_pedido(hoy=None) -> dict:
    """¿Hasta cuándo se puede pedir para que llegue ANTES de que empiece la
    temporada? — por origen, con el lead time de `compras_fuentes.lead_time`
    (la única función que lo decide).

        fecha límite = inicio de temporada − (lead time + σ del lead time)

    Con una σ de margen llega a tiempo en ~5 de cada 6 casos (normal a una
    cola). Es un supuesto declarado (`criterio`), no un dato: el dueño puede
    pedir más margen.

    Returns: {temporada, inicio, en_curso, por_origen: {NACIONAL|CHINA:
              {fecha_limite, dias_restantes, vencida, lt_dias, sigma_lt, fuente}},
              criterio}"""
    from datetime import timedelta
    from app.services import compras_fuentes
    hoy = hoy or _dia_operativo()
    t = proxima_temporada(hoy)
    por_origen = {}
    for origen in (compras_fuentes.ORIGEN_NACIONAL, compras_fuentes.ORIGEN_CHINA):
        lt = compras_fuentes.lead_time(origen=origen)
        margen = int(round(float(lt['lt_dias']) + float(lt['sigma_lt'])))
        limite = t['inicio'] - timedelta(days=margen)
        por_origen[origen] = {
            'fecha_limite': limite.isoformat(),
            'dias_restantes': (limite - hoy).days,
            'vencida': limite < hoy,
            'lt_dias': lt['lt_dias'], 'sigma_lt': lt['sigma_lt'],
            'fuente': lt['fuente'],
        }
    return {'temporada': t['etiqueta'], 'inicio': t['inicio'].isoformat(),
            'fin': t['fin'].isoformat(), 'en_curso': t['en_curso'],
            'por_origen': por_origen,
            'criterio': 'inicio de temporada − (lead time + una σ)'}


def conciliar_con_contenedor(filas_temporada, items_contenedor) -> dict:
    """Una cifra por SKU cuando la temporada y el contenedor hablan del mismo.

    El pedido de temporada (newsvendor: lo que hay que TENER al empezar) y el
    déficit del contenedor (S objetivo de reposición) son dos modelos. Para un
    SKU de temporada que además va en la propuesta de contenedor, **manda la
    temporada**: es el modelo de la demanda escolar, y el que el comité firma.
    La cifra del contenedor se muestra como lo que HOY propone y la diferencia
    como el ajuste a hacerle — no como un segundo número a pedir.

    Returns: {ref: {cifra, fuente='TEMPORADA', en_contenedor_unidades,
                    ajuste_contenedor_unidades}}"""
    en_cont = {}
    for it in items_contenedor or []:
        r = it.get('referencia')
        if r:
            en_cont[r] = en_cont.get(r, 0) + int(it.get('unidades') or 0)
    salida = {}
    for f in filas_temporada or []:
        r = f.get('referencia')
        if r not in en_cont or f.get('pedir') is None:
            continue
        salida[r] = {'cifra': int(f['pedir']), 'fuente': 'TEMPORADA',
                     'en_contenedor_unidades': en_cont[r],
                     'ajuste_contenedor_unidades': int(f['pedir']) - en_cont[r]}
    return salida


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
        from collections import defaultdict
        from datetime import timedelta

        from app.services.kardex_service import (
            dias_en, inicio_cobertura_kardex, intervalos_con_stock, serie_demanda)

        hasta = _dia_operativo()
        try:
            desde = hasta.replace(year=hasta.year - anios)
        except ValueError:          # 29 de febrero
            desde = date(hasta.year - anios, 2, 28)
        cobertura = inicio_cobertura_kardex()
        if cobertura is None or cobertura > hasta:
            return {}
        # Un día anterior a la cobertura del kardex no es un día sin venta: no
        # se observó. Ni se cuenta en la demanda ni en el denominador.
        desde = max(desde, cobertura)

        # EL NUMERADOR Y EL DENOMINADOR DE SIEMPRE (ver kardex_service): la
        # demanda neta por día y los días con stock contados sobre el escalón,
        # no sobre los días con movimiento. Antes esta función leía los
        # conceptos y `StockDiario` por su cuenta, y como `StockDiario` solo
        # tenía filas en días con movimiento, «días con stock en la temporada»
        # era «días con venta»: un SKU que vendió cada tres días con el estante
        # lleno salía con demanda ×3.
        serie = serie_demanda(desde, hasta, 'red')
        tramos = intervalos_con_stock(desde, hasta, 'red')

        dem_temp = defaultdict(lambda: defaultdict(float))
        dem_anual = defaultdict(float)
        for ref, s in serie.items():
            for f, v in s['por_dia'].items():
                if v <= 0:
                    continue
                dem_anual[ref] += v
                t = temporada_de(f)
                if t:
                    dem_temp[ref][t] += v

        def _limites(t):
            anio = int(t.split('-')[0])
            a = date(anio, TEMPORADA_INICIO[0], TEMPORADA_INICIO[1])
            # Hasta el último día de febrero (29 en bisiesto: temporada_de lo
            # cuenta dentro).
            return a, date(anio + 1, 3, 1) - timedelta(days=1)

        def _dias_de(t):
            """(inicio, fin) observados de la temporada `t` dentro de la ventana."""
            a, b = _limites(t)
            return max(a, desde), min(b, hasta)

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
                    t: (dias_en(tramos.get(ref), *_dias_de(t)) if ref in tramos else 0)
                    for t in dem_temp[ref]
                },
                # Los días que el kardex OBSERVÓ de cada temporada: una
                # temporada recortada por la cobertura (o la que está en curso)
                # no tiene 90 días, y proyectarla a 90 la inventaría.
                'dias_observados_por_temporada': {
                    t: (lambda ab: (ab[1] - ab[0]).days + 1)(_dias_de(t))
                    for t in dem_temp[ref]
                },
                'dias_totales_por_temporada': {
                    t: (lambda ab: (ab[1] - ab[0]).days + 1)(_limites(t))
                    for t in dem_temp[ref]
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

        items, excluidos = [], {'lista_negra': [], 'costo_fantasma': [], 'sin_producto': [],
                                'sin_temporada_completa': []}

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
            temporadas_incompletas = []
            for t, dem in sorted(info['por_temporada'].items()):
                dias = info['dias_stock_por_temporada'].get(t, 0)
                observados = info.get('dias_observados_por_temporada', {}).get(t, 90)
                totales = info.get('dias_totales_por_temporada', {}).get(t, observados)
                if observados < totales:
                    # Una temporada que el kardex vio a medias (la cobertura
                    # empieza adentro, o está en curso) no es una observación de
                    # la demanda de una temporada: es un pedazo. Proyectarla a
                    # la temporada entera supondría demanda pareja, y enero no
                    # vende como diciembre. Fuera, y dicho.
                    temporadas_incompletas.append(t)
                    continue
                corregida, censurada = TemporadaService._descensurar(
                    dem, dias, dias_ventana=observados)
                ventas_pasadas.append(round(corregida, 1))
                if censurada:
                    temporadas_censuradas.append(t)

            if not ventas_pasadas:
                excluidos['sin_temporada_completa'].append(ref)
                continue

            # **Sin `or`.** `costo_service` calcula `cu = max(venta − costo, 0)`,
            # así que `cu == 0` no es dato ausente: es «este SKU se vende a
            # pérdida, no compres más». El `or` lo leía como hueco y fabricaba
            # `costo * margen_pct`, que además es **markup sobre costo** — el
            # error que `costo_service.py:53-67` documenta con veinte líneas por
            # haber movido el ratio crítico de 0,583 a 0,442.
            #
            # Con m=0,40 daba 0,400×costo contra 0,667×costo canónico (1,67×), y
            # con el margen de CHINA (0,435), 1,92×. El efecto sobre la decisión
            # es que el ratio crítico pasa de 0 —Q* ≈ 0, no comprar— a 0,308,
            # o sea Q* = μ − 0,5σ: **se compra el SKU que pierde plata**, en el
            # lado irreversible.
            #
            # Que la clave exista está garantizado: una referencia sin costo de
            # ninguna fuente ya salió por `costo_fantasma` unas líneas arriba, y
            # `resolver_costos` siempre escribe `cu` cuando devuelve una fila.
            cu = float(info_costo['cu'])
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
                'origen': (info_costo.get('origen') or '').strip().upper() or None,
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
                'temporadas_incompletas': temporadas_incompletas,
                'precio_es_supuesto': info_costo.get('precio_es_supuesto', False),
                'margen_supuesto': info_costo.get('margen_supuesto'),
                'cu_rango': info_costo.get('cu_rango'),
                'convencion_margen': info_costo.get('convencion_margen'),
            })

        resultado = KardexService.newsvendor(items, margen_pct=margen_pct,
                                             costo_exceso_pct=tasa_capital + tasa_liquidacion)

        # ── TENER, HAY, VIENE, PEDIR (D2) ─────────────────────────────────
        #
        # Q* es cuánto hay que TENER al empezar la temporada, no cuánto pedir.
        # Antes se publicaba Q* como pedido: un SKU con 800 en bodega y Q* de
        # 1.000 salía «pedir 1.000». La posición es la MISMA función del
        # Armador (`posicion_inventario`): disponible en bodegas operadas
        # menos lo comprometido, más lo que ya viene.
        from app.services.armador_service import posicion_inventario
        refs_items = [i['referencia'] for i in items]
        posiciones, info_camino = (posicion_inventario(refs_items)
                                   if refs_items else ({}, {'hay_dato': False}))

        # Devolver el nombre y la concentración a cada fila (el calculador es
        # agnóstico y solo conoce referencias)
        meta = {i['referencia']: i for i in items}
        for fila in resultado.get('items', []):
            m = meta.get(fila['referencia'], {})
            fila['nombre'] = m.get('nombre', '')
            fila['concentracion'] = m.get('concentracion')
            fila['precio_venta'] = m.get('precio_venta')
            for k in ('fuente_costo', 'origen', 'costo_confiable', 'costo_anejo',
                      'dias_antiguedad_costo', 'precio_es_supuesto',
                      'margen_supuesto', 'cu_rango', 'convencion_margen',
                      'demanda_censurada', 'temporadas_censuradas',
                      'temporadas_incompletas'):
                fila[k] = m.get(k)

            pos = posiciones.get(fila['referencia'])
            tener = fila.get('q_optimo')
            if tener is None:
                continue
            hay = pos['disponible'] if pos else 0.0
            viene = pos['en_camino'] if pos else 0.0
            # tener − (hay + viene): la posición de `posicion_inventario`, la
            # misma que ve el Armador.
            pedir = max(0, round(tener - (pos['posicion'] if pos else 0.0)))
            costo = float(fila.get('costo_unitario') or 0)
            fila.update({
                'tener': tener,
                'hay': round(hay),
                'viene': round(viene),
                'pedir': pedir,
                'inversion_pedido': round(pedir * costo) if costo else None,
                # Sin ninguna fila de stock no es «hay 0»: es no saber.
                'hay_sin_dato': pos is None,
                'stock_frescura': (pos['frescura'].isoformat()
                                   if pos and pos.get('frescura') else None),
            })

        filas_ = resultado.get('items', [])
        resultado['total_pedir_unidades'] = sum(f.get('pedir') or 0 for f in filas_)
        resultado['total_inversion_pedido'] = sum(
            f.get('inversion_pedido') or 0 for f in filas_)
        resultado['posicion'] = {
            'fuente': 'armador_service.posicion_inventario',
            'en_camino': info_camino,
            'filas_sin_stock_conocido': sum(1 for f in filas_ if f.get('hay_sin_dato')),
            'nota': ('pedir = max(0, tener − hay − viene). «hay» es el disponible '
                     'de HOY: lo que se venda entre hoy y el inicio de la temporada '
                     'no está descontado.'),
        }
        ratios = [f['ratio_critico'] for f in filas_ if f.get('ratio_critico') is not None]
        # La cabecera ya no muestra un ratio que ninguna fila usa (D12): cada
        # fila trae el suyo (Cu/Co propios) y acá va el rango.
        resultado['ratio_critico_filas'] = (
            {'min': min(ratios), 'max': max(ratios)} if ratios else None)

        total = len(estacionales)
        cubiertos = len(items)
        resultado['cobertura'] = {
            'skus_temporada': total,
            'cubiertos_por_modelo': cubiertos,
            'pct_cubierto': round(cubiertos / total * 100, 1) if total else 0,
            'excluidos_lista_negra': len(excluidos['lista_negra']),
            'excluidos_costo_fantasma': len(excluidos['costo_fantasma']),
            'excluidos_sin_producto': len(excluidos['sin_producto']),
            'excluidos_sin_temporada_completa': len(excluidos['sin_temporada_completa']),
            'detalle_excluidos': excluidos,
            'advertencia': (
                f'El modelo cubre {cubiertos} de {total} SKUs de temporada. '
                f'El resto se decide sin modelo — la lista paralela gobierna ahí.'
            ) if cubiertos < total else None,
        }
        # Cobertura POR FUENTE, no binaria
        resultado['cobertura']['costo'] = resumen_por_fuente(costos)
        # La temporada a la que corresponde este pedido (por fecha, no una
        # etiqueta escrita a mano): los juicios del comité se guardan con ella.
        resultado['temporada'] = proxima_temporada()['etiqueta']
        resultado['parametros'] = {
            'margen_pct': margen_pct,
            'tasa_capital': tasa_capital,
            'tasa_liquidacion': tasa_liquidacion,
            'umbral_concentracion': umbral,
            'ventana': 'dic 1 – feb 28',
            'demanda': 'DESCENSURADA por días con stock de cada temporada',
        }
        return resultado
