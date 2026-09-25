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
from datetime import date, datetime, timedelta
from collections import defaultdict
from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo


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

# Lead times: los defaults DECLARADOS (y sus variables de entorno
# `ROP_LT_NACIONAL_DIAS` / `ROP_SIGMA_LT_NACIONAL`) viven en `compras_fuentes`,
# y quien decide el lead time de un SKU es `compras_fuentes.lead_time`
# (proveedor → origen → default, con la regla D9). Se re-exportan acá solo por
# compatibilidad de import; ningún cálculo de este módulo los lee (trinquete:
# tests/test_compras_fuentes_trinquetes.py).
from app.services.compras_fuentes import (  # noqa: E402,F401
    LT_NACIONAL_DIAS, SIGMA_LT_NACIONAL, LT_CHINA_DIAS, SIGMA_LT_CHINA,
)

# Periodo de revisión. China se revisa por trimestre (un contenedor cada ~90d),
# así que la exposición al riesgo es LT + R, no solo LT. Nacional se revisa de
# continuo: R = 0.
R_CHINA_DIAS = int(os.environ.get('ROP_R_CHINA_DIAS', '90'))
R_NACIONAL_DIAS = 0

# ── Ciclo de compra NACIONAL: hasta dónde se pide (2026-09-25) ────────────────
#
# El punto de pedido nacional es de revisión continua (R = 0): dice CUÁNDO
# pedir. Faltaba CUÁNTO. Pedir hasta el punto de pedido deja la posición justo
# en el umbral y al día siguiente hay que volver a pedir; la cantidad sale de
# un NIVEL OBJETIVO que cubre el lead time MÁS el ciclo de compra (cada cuánto
# se vuelve a mirar la bandeja), con la misma fórmula que el S de China
# (`nivel_objetivo`, una función para los dos regímenes).
#
# El ciclo es una DECISIÓN del dueño (¿se compra a nacionales cada semana?):
# default 7 días, declarado como supuesto; `ROP_CICLO_NACIONAL_DIAS` lo fija.
CICLO_NACIONAL_DIAS_DEFAULT = 7
VAR_CICLO_NACIONAL = 'ROP_CICLO_NACIONAL_DIAS'


def ciclo_pedido_nacional() -> dict:
    """Cada cuánto se compra a un proveedor nacional, con su procedencia.

    Returns: {'dias': int, 'fuente': 'CONFIGURADO' | 'DEFAULT_SUPUESTO', 'nota'}
    Un valor ilegible o fuera de 1..90 cae al default y lo dice."""
    crudo = (os.environ.get(VAR_CICLO_NACIONAL) or '').strip()
    if crudo:
        try:
            n = int(float(crudo))
            if 1 <= n <= 90:
                return {'dias': n, 'fuente': 'CONFIGURADO', 'nota': None}
        except ValueError:
            pass
        return {'dias': CICLO_NACIONAL_DIAS_DEFAULT, 'fuente': 'DEFAULT_SUPUESTO',
                'nota': f'{VAR_CICLO_NACIONAL}={crudo!r} no es un número de días '
                        f'entre 1 y 90: se usó el default.'}
    return {'dias': CICLO_NACIONAL_DIAS_DEFAULT, 'fuente': 'DEFAULT_SUPUESTO',
            'nota': 'Ciclo de compra nacional supuesto (una vez por semana): '
                    f'se fija con {VAR_CICLO_NACIONAL}.'}


def nivel_objetivo(d_avg, sigma_d, lt_dias, sigma_lt, r_dias, z):
    """Hasta dónde se pide: S = d·(LT + R) + z·σ_LT+R (§M0.4).

    UNA función para los dos regímenes: China con R = el ciclo del contenedor
    (`R_CHINA_DIAS`), nacional con R = el ciclo de compra
    (`ciclo_pedido_nacional`). La reserva de seguridad es el segundo término."""
    return (float(d_avg) * (float(lt_dias) + float(r_dias))
            + z * sigma_ltd(lt_dias, sigma_d, d_avg, sigma_lt, r_dias=r_dias))

# Estimador de sigma_d activo. INTERINO: sigma empírica de la serie
# descensurada. DEFINITIVO (pendiente): RMSE de un paso adelante del TSB —
# el colchón debe absorber lo que el modelo NO vio venir, no la varianza cruda.
ESTIMADOR_SIGMA_D = 'SIGMA_EMPIRICA_DESCENSURADA'

# Restricción anti-500-días — SOLO para el RELLENO (lo que se mete al
# contenedor sin déficit, por margen/CBM). NO topa el S objetivo del déficit:
# con LT+R = 195 días en China, un tope de 180 cortaba TODO S objetivo por
# debajo de lo que la política (R, S) necesita para cubrir su exposición, y el
# nivel de servicio implícito caía del 95% al 16,7% (D3).
MAX_COBERTURA_RELLENO_DIAS = 180

#: Cómo se lee `FichaImportacion.moq_cajas` (D13). La ficha no dice si el MOQ
#: es un MÍNIMO por pedido o un LOTE (múltiplo), y leerlo como múltiplo
#: convertía un déficit de 60 u (5 cajas, MOQ 4) en 8 cajas = 96 u: +60% sobre
#: lo que hace falta, en el lado irreversible. Mientras la ficha no lo diga, se
#: lee como MÍNIMO y se redondea a caja completa. Declarado en cada fila.
MOQ_INTERPRETACION = 'MINIMO_POR_PEDIDO_REDONDEADO_A_CAJA'


def cajas_a_pedir(deficit_unidades, unidades_por_caja, moq_cajas):
    """Cajas para cubrir un déficit: redondeo a caja completa y el MOQ como
    MÍNIMO (no como múltiplo). Ver `MOQ_INTERPRETACION`."""
    u = max(1, int(unidades_por_caja or 1))
    moq = max(1, int(moq_cajas or 1))
    necesarias = math.ceil(max(0.0, float(deficit_unidades)) / u)
    return max(necesarias, moq) if necesarias > 0 else 0


def pedido_en_empaques(deficit_unidades, unidades_por_empaque, moq_empaques):
    """Un déficit en unidades → lo que se pide de verdad: empaques completos y
    el MOQ como mínimo (`cajas_a_pedir`, la misma regla del contenedor).

    Returns: {empaques, unidades, unidades_por_empaque, moq_empaques,
              redondeo_unidades (lo que se pide de más por empaque/MOQ),
              moq_interpretacion}"""
    u = max(1, int(unidades_por_empaque or 1))
    moq = max(1, int(moq_empaques or 1))
    deficit = max(0.0, float(deficit_unidades or 0))
    empaques = cajas_a_pedir(deficit, u, moq)
    unidades = empaques * u
    return {
        'empaques': empaques,
        'unidades': unidades,
        'unidades_por_empaque': u,
        'moq_empaques': moq,
        'redondeo_unidades': max(0, unidades - math.ceil(deficit)) if empaques else 0,
        'moq_interpretacion': MOQ_INTERPRETACION,
    }


def composicion_por_proveedor(items):
    """La propuesta de contenedor agrupada por proveedor chino, con sus
    subtotales (cajas, unidades, CBM, kg, FOB USD). Solo agrupa y suma lo que
    `armar_contenedor` ya calculó por línea.

    Returns: [{proveedor, lineas, cajas, unidades, cbm, peso_kg, fob_usd,
               lineas_sin_fob}] — el de más CBM primero."""
    grupos = {}
    for it in items or []:
        prov = (it.get('proveedor_china') or '').strip() or None
        g = grupos.setdefault(prov, {'proveedor': prov, 'lineas': [], 'cajas': 0,
                                     'unidades': 0, 'cbm': 0.0, 'peso_kg': 0.0,
                                     'fob_usd': 0.0, 'lineas_sin_fob': 0})
        g['lineas'].append(it)
        g['cajas'] += int(it.get('cajas') or 0)
        g['unidades'] += int(it.get('unidades') or 0)
        g['cbm'] += float(it.get('cbm') or 0)
        g['peso_kg'] += float(it.get('peso_kg') or 0)
        if it.get('costo_fob_usd'):
            g['fob_usd'] += float(it['costo_fob_usd'])
        else:
            g['lineas_sin_fob'] += 1
    salida = list(grupos.values())
    for g in salida:
        g['cbm'] = round(g['cbm'], 3)
        g['peso_kg'] = round(g['peso_kg'], 1)
        g['fob_usd'] = round(g['fob_usd'], 2)
    salida.sort(key=lambda g: (g['proveedor'] is None, -g['cbm']))
    return salida


# Marcas China conocidas: CÓDIGOS del criterio de marca de Siesa (se cruzan con
# `producto.marca_codigo`; ver rop_dual)
MARCAS_CHINA = {'M003', 'M009', 'M175'}


def posicion_inventario(refs=None):
    """¿Cuánto hay y cuánto viene?, por referencia. La del Armador y la del
    pedido de temporada: una función (D2).

        disponible = existencia − comprometido − salida sin confirmar
                     (solo bodegas OPERADAS, `_BODEGAS_PV`: lista blanca)
        posicion   = disponible + en_camino

    Con signo, sin `max(0, ...)`: más comprometido que stock es mercancía
    vendida que todavía se debe (ver el comentario de `rop_dual`).

    «Lo que viene» es `compras_fuentes.en_camino` —la ÚNICA función que lo
    calcula: OCs abiertas de Siesa + contenedores sin recibir, sin contar dos
    veces lo mismo—; acá solo se suma.

    Returns: ({ref: {existencia, comprometido, salida_sin_conf, disponible,
                     en_camino, posicion, frescura}}, declaracion_en_camino)
    """
    from sqlalchemy import func
    from app.models.stock_siesa import StockSiesa
    from app.services.compras_fuentes import en_camino
    from app.services.inventario_siesa_service import _BODEGAS_PV

    q = (db.session.query(
            StockSiesa.codigo_siesa,
            func.sum(StockSiesa.existencia),
            func.sum(StockSiesa.comprometido),
            func.sum(StockSiesa.salida_sin_conf),
            func.min(StockSiesa.updated_at),
        )
        .filter(StockSiesa.bodega.in_(_BODEGAS_PV)))
    if refs is not None:
        q = q.filter(StockSiesa.codigo_siesa.in_(list(refs)))
    filas = q.group_by(StockSiesa.codigo_siesa).all()

    camino = en_camino(skus=set(refs) if refs is not None else None)
    salida = {}
    for ref, ex, comp, sal, fres in filas:
        r = (ref or '').strip()
        if not r:
            continue
        # Dos códigos crudos que difieren en espacios son la MISMA referencia
        # (Siesa manda algunas con espacios a la derecha): se suman.
        d = salida.setdefault(r, {'existencia': 0.0, 'comprometido': 0.0,
                                  'salida_sin_conf': 0.0, 'disponible': 0.0,
                                  'frescura': None, 'en_camino': 0.0})
        ex, comp, sal = float(ex or 0), float(comp or 0), float(sal or 0)
        d['existencia'] += ex
        d['comprometido'] += comp
        d['salida_sin_conf'] += sal
        d['disponible'] += ex - comp - sal
        if fres is not None and (d['frescura'] is None or fres < d['frescura']):
            d['frescura'] = fres
    for r, qv in camino['por_sku'].items():
        salida.setdefault(r, {'existencia': 0.0, 'comprometido': 0.0,
                              'salida_sin_conf': 0.0, 'disponible': 0.0,
                              'frescura': None, 'en_camino': 0.0})
        salida[r]['en_camino'] = qv
    for d in salida.values():
        d['posicion'] = d['disponible'] + d['en_camino']
    return salida, camino['declaracion']


def es_de_china(origen=None, marca=None, marca_codigo=None) -> bool:
    """¿Este SKU se compra en el régimen China? UNA regla para el ROP y la
    franja de confianza: origen declarado CHINA, o una marca China conocida
    (`MARCAS_CHINA` son CÓDIGOS del criterio de Siesa: se comparan contra
    `marca_codigo`, y contra el texto de `marca_siesa` solo por compatibilidad
    con las cargas por archivo anteriores a m047, que ponían el código ahí)."""
    if (origen or '').strip().upper() == 'CHINA':
        return True
    if (marca_codigo or '').strip().upper() in MARCAS_CHINA:
        return True
    texto = (marca or '').upper()
    return any(m.upper() in texto for m in MARCAS_CHINA if m)


def insumo_origen(productos_origen=None, productos_marca=None,
                  productos_marca_cod=None) -> dict:
    """¿Hay con qué distinguir lo que viene de China? — la declaración del
    régimen China, UNA función: la publican el ROP (`insumo_origen`) y la franja
    de confianza de Compras.

    `Producto.origen` y `marca_siesa` se cargan desde 🧾 Fuentes; sin ninguno
    de los dos, todo SKU se calcula como nacional y la propuesta de contenedor
    sale vacía por construcción, no porque no haga falta comprar."""
    if productos_origen is None or productos_marca is None:
        from app.models.producto import Producto
        filas = (db.session.query(Producto.codigo_siesa, Producto.origen,
                                  Producto.marca_siesa, Producto.marca_codigo)
                 .filter(Producto.codigo_siesa.isnot(None)).all())
        productos_origen = {f[0]: f[1] for f in filas}
        productos_marca = {f[0]: (f[2] or f[3]) for f in filas}
        productos_marca_cod = {f[0]: f[3] for f in filas}
    productos_marca_cod = productos_marca_cod or {}
    con_origen = sum(1 for v in productos_origen.values() if (v or '').strip())
    con_marca = sum(1 for v in productos_marca.values() if (v or '').strip())
    china = sum(1 for r, v in productos_origen.items()
                if es_de_china(v, productos_marca.get(r), productos_marca_cod.get(r)))
    return {
        'skus_con_origen': con_origen,
        'skus_con_marca': con_marca,
        'skus_totales': len(productos_origen),
        'skus_sin_origen': len(productos_origen) - con_origen,
        'skus_china': china,
        'regimen_china_operativo': bool(con_origen or con_marca),
        'nota': (
            'Ningún producto tiene `origen` ni `marca_siesa`: el régimen '
            'China no puede activarse y TODOS los SKU se calcularon con '
            'lead time nacional y R=0. La propuesta de contenedor sale '
            'vacía por construcción, no porque no haga falta comprar.'
            if not (con_origen or con_marca) else None),
    }


class ArmadorService:

    @staticmethod
    def calcular_sigma_lt_real() -> dict:
        """
        Lead time de China: delega en `compras_fuentes.lead_time(origen='CHINA')`,
        la única función que decide un lead time. Mide sobre los contenedores
        con fechas completas: con < 3, el default conservador; con 3 a 5, **el
        mayor** entre lo medido y el conservador, para la media y para la σ
        (D9); desde 6, lo medido manda.
        """
        from app.services.compras_fuentes import lead_time
        return lead_time(origen='CHINA')

    @staticmethod
    def rop_dual(nivel_servicio: float = 0.95) -> dict:
        """
        M0.4 — ROP dual: punto de reorden por SKU según origen.

        Nacional: ROP = d_avg × LT_nac + z × σ_d × √LT_nac
        China:    ROP = d_avg × LT_chi + z × σ_d × √LT_chi + en_transito_ajuste

        El en_transito se resta de la posición de inventario, no del ROP.
        posicion = stock_actual + en_transito - backorders

        **Los `backorders` son lo que Siesa ya tiene comprometido**:
        `StockSiesa.comprometido` (`f400_cant_comprometida_1`) más
        `salida_sin_conf` (`f400_cant_salida_sin_conf_1`) — mercancía vendida,
        todavía en la bodega, con dueño. Esta línea del docstring declaraba la
        política correcta desde el primer día y **el código no la restaba**:
        `posicion = stock_actual + qty_transito`, sin más. La palabra
        `comprometido` no aparecía en ninguna línea de este archivo.

        Lo que costaba: `bajo_rop` salía False sobre un SKU con 100 en bodega y
        80 ya vendidos —tiene 20— y el `deficit` de la rama China, que es el
        número que **arma el contenedor**, salía corto exactamente en lo ya
        comprometido. No se repone lo que ya está vendido y pendiente de
        despachar: agotado, con la pantalla en verde. Y un contenedor es
        irreversible 120 días (Regla 0), así que este de los cuatro sitios que
        contestan «¿cuánto hay de verdad?» es el que más pesa.

        Args:
            nivel_servicio: 0.95 = z_score ~1.645

        Returns: {nacional: [...], china: [...], sigma_lt_china}
        """
        from app.services.kardex_service import KardexService
        from app.models.producto import Producto

        # z-score para nivel de servicio
        from app.services.kardex_service import _norm_ppf
        z = _norm_ppf(nivel_servicio)
        _ciclo_nac = ciclo_pedido_nacional()
        _hoy_rop = _dia_operativo()

        # Lead times: UNA función decide (`compras_fuentes.lead_time`:
        # proveedor → origen → default, con la regla D9). Las observaciones se
        # leen una vez y se reusan por SKU; el de cada SKU es el de su
        # proveedor habitual (el de su OC más reciente).
        from app.services import compras_fuentes
        _obs_lt = compras_fuentes.observaciones_lead_time()
        lt_chi = compras_fuentes.lead_time(origen='CHINA', observaciones=_obs_lt)
        lt_nac = compras_fuentes.lead_time(origen='NACIONAL', observaciones=_obs_lt)
        lt_china, sigma_lt_china = lt_chi['lt_dias'], lt_chi['sigma_lt']
        lt_nacional, sigma_lt_nacional = lt_nac['lt_dias'], lt_nac['sigma_lt']
        _proveedor_de = compras_fuentes.proveedor_habitual()
        _lt_cache = {}

        def _lt_de(ref, es_china):
            clave = (_proveedor_de.get(ref), es_china)
            if clave not in _lt_cache:
                _lt_cache[clave] = compras_fuentes.lead_time(
                    proveedor=clave[0], origen='CHINA' if es_china else 'NACIONAL',
                    observaciones=_obs_lt)
            return _lt_cache[clave]

        # CABLE M0.2 → M0.4. La demanda entra DESCENSURADA: d_avg sobre días con
        # stock, no sobre días calendario. Antes se dividía por 365 con los días
        # agotados aportando cero — el sistema aprendía a comprar poco justo de
        # lo que siempre faltaba.
        demanda_por_sku = KardexService.demanda_descensurada(
            ventana_meses=12, nivel='red')

        # Stock actual — existencia VENDIBLE, lo que ya tiene dueño y lo que
        # viene. `posicion_inventario` es la ÚNICA respuesta a «¿cuánto hay y
        # cuánto viene?» para comprar, y la usa también el pedido de temporada.
        #
        # Suma solo las bodegas OPERADAS (`_BODEGAS_PV`, lista blanca): `AV1`
        # (averías) y `TRA1` (tránsito) se persisten en `stock_siesa` igual que
        # un punto de venta, y sumarlas inflaba la posición y achicaba el
        # déficit que ARMA EL CONTENEDOR. Ver el comentario de esa función y
        # CLAUDE.md «Las tres listas de bodegas».
        posiciones, info_en_camino = posicion_inventario()
        stock = {r: d['existencia'] for r, d in posiciones.items()}
        comprometido = {r: d['comprometido'] for r, d in posiciones.items()}
        salida_sin_conf = {r: d['salida_sin_conf'] for r, d in posiciones.items()}
        frescura = {r: d['frescura'] for r, d in posiciones.items()}
        transito = {r: d['en_camino'] for r, d in posiciones.items()}

        # Origen Y marca. **Eran dos defectos apilados.**
        #
        # `productos_origen` mapeaba `codigo_siesa → origen`, y el cruce por
        # marca de abajo preguntaba si `'M003'` era subcadena de **origen**,
        # cuyos valores son 'NACIONAL' y 'CHINA'. `'M003' in 'CHINA'` es
        # False: la rama de marca **no podía darse ni con la columna poblada
        # al 100%**. El comentario de `MARCAS_CHINA` decía «se cruzan con
        # `producto.marca_siesa`» y describía un cruce que el código no hacía
        # — `marca_siesa` no aparecía en ninguna línea ejecutable del módulo.
        #
        # Y desde m047 la marca tiene DOS columnas: `marca_siesa` es el NOMBRE
        # (NORMA, QUIROND) y `marca_codigo` el código del criterio de Siesa
        # (M001, M003). `MARCAS_CHINA` son códigos: se comparan contra
        # `marca_codigo`, y contra `marca_siesa` solo por compatibilidad con
        # las cargas por archivo anteriores, que ponían el código ahí.
        filas_origen = (
            db.session.query(Producto.codigo_siesa, Producto.origen,
                             Producto.marca_siesa, Producto.marca_codigo)
            .filter(Producto.codigo_siesa.isnot(None))
            .all()
        )
        productos_origen = {f[0]: f[1] for f in filas_origen}
        productos_marca = {f[0]: (f[2] or f[3]) for f in filas_origen}
        productos_marca_cod = {f[0]: f[3] for f in filas_origen}

        resultados_nac = []
        resultados_chi = []

        # Delta agregado — el "backtest" posible del ROP: no se puede certificar
        # una fórmula contra la historia, pero sí cuantificar el salto.
        delta = {'skus': 0, 'ss_antes': 0.0, 'ss_despues': 0.0,
                 'topados_por_cobertura': 0, 'censurados': 0,
                 'sin_venta_reciente': 0}

        for ref, dem in demanda_por_sku.items():
            ref = (ref or '').strip()
            if not ref:
                continue

            d_hist = dem['d_avg']        # u/día sobre días CON stock
            if d_hist <= 0:
                continue
            # DESCONTINUADO (D14): sin venta en el tramo reciente con el estante
            # lleno —y con su propia tasa se esperaban ventas— NO se repone. Se
            # publica con d = 0 y dicho, en vez de reponer con la tasa de hace
            # seis meses.
            parado = bool(dem.get('sin_venta_reciente'))
            d_avg = 0.0 if parado else d_hist
            sigma_d = 0.0 if parado else dem['sigma_d']    # u/día
            if parado:
                delta['sin_venta_reciente'] += 1

            origen = (productos_origen.get(ref) or '').upper()
            marca = (productos_marca.get(ref) or '').upper()
            es_china = es_de_china(origen, marca, productos_marca_cod.get(ref))

            stock_actual = float(stock.get(ref, 0) or 0)
            qty_comprometido = float(comprometido.get(ref, 0) or 0)
            qty_salida_sin_conf = float(salida_sin_conf.get(ref, 0) or 0)
            _frescura = frescura.get(ref)
            qty_transito = float(transito.get(ref, 0) or 0)

            # ── POSICIÓN DE INVENTARIO, SIN `max(0, ...)` A PROPÓSITO ───────
            #
            # Los tres sitios que calculan esto aplastan a cero, y hacen bien:
            # contestan «¿cuánto puedo despachar ahora?», y una cantidad
            # asignable negativa no significa nada.
            #
            # Acá la pregunta es otra: «¿en qué punto estoy respecto del
            # objetivo?». Es una posición con signo, y una posición negativa
            # —más comprometido que stock— es un hecho medido, no un dato
            # ausente: hay mercancía vendida que todavía se debe. Aplastarla a
            # cero dejaría `deficit = s_objetivo - 0` en vez de
            # `s_objetivo + sobrevendido`, y el contenedor llegaría corto
            # exactamente en lo que ya se debía. La Regla 0 no aplica: no es
            # incertidumbre, es información.
            #
            # `deficit` sí conserva su `max(0, ...)` — no se puede comprar una
            # cantidad negativa.
            # La posición de `posicion_inventario` — la misma fórmula que ve el
            # pedido de temporada. Un SKU sin ninguna fila de stock ni nada en
            # camino tiene posición 0 (se declara en `frescura_stock: None`).
            posicion = float((posiciones.get(ref) or {}).get('posicion', 0.0))

            _lt_info = _lt_de(ref, es_china)
            lt = _lt_info['lt_dias']
            sigma_lt = _lt_info['sigma_lt']
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
                'd_avg_historica': round(d_hist, 4),
                'sin_venta_reciente': parado,
                'motivo_d_cero': (
                    f"Sin venta en los últimos {dem.get('dias_sin_venta_mirados')} días "
                    f"con stock {dem.get('dias_stock_recientes')} de ellos: se "
                    f"esperaban {d_hist * (dem.get('dias_stock_recientes') or 0):.0f} "
                    f"ventas a su tasa histórica. No se repone; revisar si se descontinuó."
                ) if parado else None,
                'sigma_d_diaria': round(sigma_d, 4),
                'rop': round(rop),
                'safety_stock': round(safety_stock),
                'sigma_ltd': round(s_ltd, 2),
                'stock_actual': round(stock_actual),
                # Procedencia de la posición: sin estos dos el comprador no
                # puede reconstruir por qué su posición no es su existencia, y
                # un número que no se puede auditar se cree a ciegas o se
                # ignora. Las dos cosas salen caras cuando arman un contenedor.
                'comprometido': round(qty_comprometido),
                'salida_sin_conf': round(qty_salida_sin_conf),
                'posicion': round(posicion),
                # Lo que se puede vender hoy: existencia − comprometido −
                # salida sin confirmar (sin lo que viene). De `posicion_inventario`.
                'disponible': round(float((posiciones.get(ref) or {}).get('disponible', 0.0))),
                # Y la tercera pata de la procedencia: **de cuándo es la foto**.
                # `None` = este SKU no tiene ninguna fila en `stock_siesa`, que
                # no es lo mismo que tener cero — otra vez el hueco y el cero
                # confundidos. Los días se calculan acá y no en el JS para que
                # el corte sea el mismo dondequiera que se lea.
                'frescura_stock': (
                    _frescura.isoformat() if _frescura is not None else None),
                'stock_dias_de_antiguedad': (
                    (datetime.utcnow() - _frescura).days
                    if _frescura is not None else None),
                'cobertura_dias': round(cobertura, 1),
                'en_transito': round(qty_transito),
                'lt_dias': lt,
                'sigma_lt': sigma_lt,
                # Procedencia del lead time: de qué nivel salió y con cuántas
                # observaciones (`compras_fuentes.lead_time`).
                'lt_fuente': _lt_info['fuente'],
                'lt_nivel': _lt_info['nivel'],
                'lt_n': _lt_info['n'],
                'lt_proveedor': _lt_info.get('proveedor'),
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
                s_objetivo = nivel_objetivo(d_avg, sigma_d, lt, sigma_lt, r, z)

                # SIN TOPE (D3). La baranda anti-500-días es del RELLENO. Acá
                # topaba el S de la política (R, S) a 180 días de demanda, por
                # debajo de LT + R = 195: cortaba el objetivo de TODO SKU China
                # y el servicio caía del 95% pedido al 16,7%.

                fila.update({
                    'r_dias': r,
                    'sigma_ltr': round(s_ltr, 2),
                    's_objetivo': round(s_objetivo),
                    'cobertura_objetivo_dias': (
                        round(s_objetivo / d_avg, 1) if d_avg > 0 else None),
                    # `posicion` ya viene en la fila base — no se repite acá
                    # para que no haya dos sitios donde cambiarla.
                    'deficit': round(max(0, s_objetivo - posicion)),
                })
                resultados_chi.append(fila)
            else:
                # CUÁNTO pedir (2026-09-25): hasta el nivel objetivo, que cubre
                # el lead time MÁS el ciclo de compra. Mismo `nivel_objetivo`
                # que el S de China; el punto de pedido (R = 0) sigue diciendo
                # CUÁNDO. `deficit` conserva su `max(0, …)`: no se compra una
                # cantidad negativa.
                s_nac = nivel_objetivo(d_avg, sigma_d, lt, sigma_lt,
                                       _ciclo_nac['dias'], z)
                fila.update({
                    'bajo_rop': posicion < rop,
                    'ciclo_dias': _ciclo_nac['dias'],
                    'nivel_objetivo': round(s_nac),
                    'deficit': round(max(0, s_nac - posicion)),
                    # Cuántos días faltan para cruzar el punto de pedido a la
                    # tasa de hoy (negativo = ya lo cruzó). Sin demanda, None.
                    'dias_hasta_punto_de_pedido': (
                        round((posicion - rop) / d_avg, 1) if d_avg > 0 else None),
                    # «Se agota antes de que llegue»: aun pidiendo hoy, lo que
                    # hay y lo que viene no cubre el lead time.
                    'se_agota_antes_de_llegar': bool(d_avg > 0 and posicion < d_avg * lt),
                    # Si se pide hoy, cuándo llegaría (día Bogotá + lead time).
                    'llegaria_el': (_hoy_rop + timedelta(days=math.ceil(lt))).isoformat(),
                })
                resultados_nac.append(fila)

        resultados_nac.sort(key=lambda x: x['cobertura_dias'])
        resultados_chi.sort(key=lambda x: x['cobertura_dias'])

        mult = (delta['ss_despues'] / delta['ss_antes']) if delta['ss_antes'] > 0 else 0

        # ── El régimen China: ¿hay con qué distinguirlo? ────────────────
        # `Producto.origen` **no tiene ningún escritor** en el repo: no lo pone
        # el sync de Siesa, no está en la lista blanca de `actualizar_producto`,
        # no hay ruta ni script. Con la columna vacía, `es_china` es siempre
        # False y **todo SKU recibe régimen nacional** —LT nacional, R=0— y
        # `resultados_chi` sale vacío.
        #
        # Eso no era un error visible: era una pantalla que mostraba el ROP
        # dual como si existiera. Ahora se declara, porque un modelo que corre
        # sobre un insumo ausente y no lo dice es la forma más cara de este
        # repo: el número sale con cara de bueno y alguien compra con él.
        _insumo = insumo_origen(productos_origen, productos_marca, productos_marca_cod)
        if not _insumo['regimen_china_operativo']:
            logger.warning(
                '[ARMADOR] ROP dual sin insumo de origen: %d SKU calculados '
                'todos como nacionales. `Producto.origen` no tiene escritor.',
                len(productos_origen))

        return {
            'nivel_servicio': nivel_servicio,
            'z_score': round(z, 3),
            'insumo_origen': _insumo,
            # EL TÉRMINO EN TRÁNSITO, declarado (`compras_fuentes.en_camino`):
            # fuentes, frescura del espejo de OCs, líneas sin unidad base,
            # solapamientos. Sin fuente con dato vale 0 y eso se dice: «viene
            # 0» y «no sé qué viene» empujan la compra al mismo lado.
            'insumo_en_camino': info_en_camino,
            'lead_time': {'nacional': lt_nac, 'china': lt_chi},
            # Procedencia del cálculo — sin esto el número no es auditable
            'estimador_sigma_d': ESTIMADOR_SIGMA_D,
            'formula': 'sigma_LTD = sqrt((LT+R)*sigma_d^2 + d^2*sigma_LT^2)  §M0.4',
            'unidad_canonica': 'dias',
            # Solo el RELLENO: el S objetivo del déficit ya no se topa (D3).
            'cobertura_max_dias': MAX_COBERTURA_RELLENO_DIAS,
            'cobertura_max_aplica_a': 'RELLENO',
            # Reporte de delta: cuánto salta el colchón al corregir la fórmula
            'delta_vs_formula_anterior': {
                'skus': delta['skus'],
                'safety_stock_antes': round(delta['ss_antes']),
                'safety_stock_despues': round(delta['ss_despues']),
                'multiplicador': round(mult, 2),
                'topados_por_cobertura': delta['topados_por_cobertura'],
                'skus_censurados': delta['censurados'],
                'skus_sin_venta_reciente': delta['sin_venta_reciente'],
                'aviso_censura': (
                    f"{delta['censurados']} SKU(s) sin StockDiario: su demanda esta "
                    f"CENSURADA (subestima). Correr POST /api/kardex/reconstruir."
                ) if delta['censurados'] else None,
                'nota': ('La fórmula anterior usaba z*d*sqrt(sigma_LT): sin sigma_d '
                         'y con la raíz sobre la desviación del lead time en vez de '
                         'sobre la exposición. Subestimaba el colchón.'),
            },
            'nacional': {
                'ciclo': _ciclo_nac,
                'lt_dias': lt_nacional,
                'sigma_lt': sigma_lt_nacional,
                'lt_fuente': lt_nac['fuente'],
                'lt_n': lt_nac['n'],
                'total': len(resultados_nac),
                'bajo_rop': sum(1 for r in resultados_nac if r.get('bajo_rop')),
                'items': resultados_nac,
            },
            'china': {
                'lt_dias': lt_china,
                'sigma_lt': sigma_lt_china,
                'sigma_lt_fuente': lt_chi['fuente'],
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
            cajas_necesarias = cajas_a_pedir(deficit_u, u_por_caja, moq_cajas)

            cbm = cajas_necesarias * float(ficha.cbm_por_caja or 0)
            peso = cajas_necesarias * float(ficha.peso_kg_por_caja or 0)
            costo_fob = cajas_necesarias * u_por_caja * float(ficha.costo_fob_usd or 0)

            items_deficit.append({
                'referencia': ref,
                'nombre': ficha.producto.nombre if ficha.producto else None,
                'proveedor_china': ficha.proveedor_china,
                'costo_fob_usd_unidad': (float(ficha.costo_fob_usd)
                                         if ficha.costo_fob_usd is not None else None),
                'unidades_por_caja': u_por_caja,
                'deficit_unidades': deficit_u,
                'cajas': cajas_necesarias,
                'unidades': cajas_necesarias * u_por_caja,
                'moq_cajas': moq_cajas,
                'moq_interpretacion': MOQ_INTERPRETACION,
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
        margen_cobertura = None
        if gatillo == 'PELIGRO_CONSTITUCIONAL' and cbm_acum < cbm_objetivo:
            # ── EL CABLE DE COSTO Y PRECIO ────────────────────────────────
            #
            # Antes acá se leía `Producto.precio_venta - Producto.precio_compra`.
            # La sincronización de Siesa **NUNCA puebla ninguno de los dos**
            # (declarado en costo_service.py). Los dos valían 0, así que
            # `margen_por_cbm` valía 0 PARA TODOS LOS CANDIDATOS.
            #
            # El efecto no era un error visible: era un ranking que ordenaba
            # por una constante. `sort()` es estable, así que devolvía el orden
            # de entrada con aspecto de decisión — y el recorte por presupuesto,
            # que invierte ese mismo orden, cortaba por lo mismo.
            #
            # `temporada_service` ya había tenido este bug exacto y se corrigió
            # ahí. No se propagó acá: la misma política en dos sitios, arreglada
            # en uno.
            #
            # `resolver_costos` elige el costo por jerarquía —acuerdo vigente >
            # cotización > kardex > maestro— y devuelve `cu` = margen unitario,
            # DECLARANDO de qué fuente salió y si el precio es supuesto.
            from app.services.costo_service import resolver_costos, resumen_por_fuente

            _refs = [i['referencia'] for i in items_china]
            _costos = resolver_costos(_refs) if _refs else {}
            margen_cobertura = resumen_por_fuente(_costos) if _costos else None

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

                # Margen unitario con procedencia. `cu` es lo que se pierde si
                # el ítem falta: precio de venta menos costo.
                _c = _costos.get(ref) or {}
                margen_u = float(_c.get('cu') or 0)
                margen_cbm = (margen_u * u_por_caja / cbm_caja) if cbm_caja > 0 else 0

                # Restricción anti-500-días
                d_avg = item['d_avg_diaria']
                unidades_propuestas = u_por_caja  # 1 caja mínimo
                cobertura_post = (item['posicion'] + unidades_propuestas) / d_avg if d_avg > 0 else 999
                if cobertura_post > MAX_COBERTURA_RELLENO_DIAS:
                    continue

                items_relleno.append({
                    'referencia': ref,
                    'nombre': ficha.producto.nombre if ficha.producto else None,
                    'proveedor_china': ficha.proveedor_china,
                    'costo_fob_usd_unidad': (float(ficha.costo_fob_usd)
                                             if ficha.costo_fob_usd is not None else None),
                    'unidades_por_caja': u_por_caja,
                    'unidades': u_por_caja,
                    'cajas': 1,
                    'cbm': round(cbm_caja, 3),
                    'peso_kg': round(peso_caja, 1),
                    'costo_fob_usd': round(costo_fob * u_por_caja, 2),
                    'margen_por_cbm': round(margen_cbm, 2),
                    # De dónde salió el margen. Un Q* sobre cotización vigente y
                    # uno sobre un margen supuesto no valen lo mismo, y el comité
                    # tiene derecho a ver cuál es cuál en la fila que va a firmar.
                    'margen_fuente': _c.get('fuente', 'SIN_COSTO'),
                    'precio_fuente': _c.get('fuente_precio', 'MARGEN_SUPUESTO'),
                    'precio_es_supuesto': bool(_c.get('precio_es_supuesto')),
                    # ±10 puntos de margen mueven la decisión: para las filas
                    # supuestas se muestra rango, no un punto falsamente preciso.
                    'margen_por_cbm_rango': (
                        [round(v * u_por_caja / cbm_caja, 2) for v in _c['cu_rango']]
                        if _c.get('cu_rango') and cbm_caja > 0 else None),
                    'cobertura_post': round(cobertura_post, 1),
                    'tipo': 'RELLENO',
                })

            # Ordenar por margen/CBM (la métrica reina).
            #
            # Desempate por costo FOB ascendente: entre dos ítems con el MISMO
            # margen —el caso cuando ninguno tiene costo de ninguna fuente—
            # entra primero el más barato. Sin este desempate, `sort` estable
            # devuelve el orden de entrada y el resultado vuelve a parecer una
            # decisión sin serlo.
            items_relleno.sort(
                key=lambda x: (-x['margen_por_cbm'], x['costo_fob_usd']))

            # Llenar espacio restante
            for item in items_relleno:
                if cbm_acum + item['cbm'] > cbm_objetivo:
                    continue
                if peso_acum + item['peso_kg'] > payload_kg:
                    continue
                contenedor_items.append(item)
                cbm_acum += item['cbm']
                peso_acum += item['peso_kg']

        # Restricción de caja (presupuesto tesorería). La moneda sale de UNA
        # función (`costo_service.a_cop`): antes eran `1.45 * 4200` escritos
        # a mano acá (D4).
        from app.services.costo_service import a_cop, trm_cop_por_usd, factor_nacionalizacion

        def _valor_cop(items_):
            fob = sum(i['costo_fob_usd'] for i in items_)
            cop, _det = a_cop(fob, 'USD', es_fob=True)
            return fob, cop

        valor_fob_total, valor_nac_estimado = _valor_cop(contenedor_items)

        recorte_sugerido = []
        presupuesto_insuficiente = False
        if presupuesto_cop and valor_nac_estimado > presupuesto_cop:
            # 1. Recortar RELLENO en orden inverso de ranking.
            # 2. Si aun así no alcanza, el DÉFICIT: del menos urgente (mayor
            #    cobertura) al más urgente. Antes el recorte paraba en el
            #    relleno y devolvía un contenedor que costaba más que el
            #    presupuesto sin decirlo (D13). Ahora se recorta EXPLÍCITO, con
            #    motivo por ítem, y se declara que el presupuesto no alcanza
            #    para cubrir el déficit: esa conversación es de tesorería, no
            #    del algoritmo.
            orden = (list(reversed([i for i in contenedor_items if i['tipo'] == 'RELLENO']))
                     + sorted([i for i in contenedor_items if i['tipo'] == 'DEFICIT'],
                              key=lambda x: -x['cobertura_dias']))
            for item in orden:
                if valor_nac_estimado <= presupuesto_cop:
                    break
                contenedor_items.remove(item)
                if item['tipo'] == 'DEFICIT':
                    presupuesto_insuficiente = True
                recorte_sugerido.append(dict(item, motivo_recorte=(
                    'PRESUPUESTO_DEFICIT' if item['tipo'] == 'DEFICIT'
                    else 'PRESUPUESTO_RELLENO')))
                cbm_acum -= item['cbm']
                peso_acum -= item['peso_kg']
                valor_fob_total, valor_nac_estimado = _valor_cop(contenedor_items)

        deficit_sin_cubrir = [
            {'referencia': i['referencia'], 'deficit_unidades': i['deficit_unidades'],
             'costo_fob_usd': i['costo_fob_usd']}
            for i in recorte_sugerido if i['tipo'] == 'DEFICIT']

        # Ventana de llegada
        eta_min = _dia_operativo() + timedelta(days=int(lt_china - sigma_lt))
        eta_max = _dia_operativo() + timedelta(days=int(lt_china + sigma_lt))

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
            # Cobertura del margen POR FUENTE. Sin esto, un contenedor armado
            # enteramente sobre margen supuesto se ve igual que uno armado
            # sobre cotizaciones vigentes.
            'margen_cobertura': margen_cobertura,
            'recorte_presupuesto': recorte_sugerido,
            'presupuesto_insuficiente': presupuesto_insuficiente,
            'deficit_sin_cubrir_por_presupuesto': deficit_sin_cubrir,
            'valor_fob_usd': round(valor_fob_total, 2),
            'valor_nacionalizado_cop_estimado': round(valor_nac_estimado),
            'conversion_moneda': {'trm': trm_cop_por_usd(),
                                  'factor_nacionalizacion': factor_nacionalizacion()},
            'moq_interpretacion': MOQ_INTERPRETACION,
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
