"""
CostoService — resuelve costo y precio por SKU, con procedencia declarada.

Una política, una función. Igual que dias_expuestos() para la censura.

POR QUÉ EXISTE: `Producto.precio_compra` y `precio_venta` NUNCA se pueblan.
La sincronización desde Siesa crea productos sin ellos (siesa_sync_service:138)
y solo `POST /api/productos` los escribe, con default 0. Resultado: todo SKU
sincronizado tiene costo 0, el newsvendor los excluye a todos por "costo
fantasma", y la cobertura del comité no es 40% — es CERO.

JERARQUÍA DE COSTO — de más a menos confiable, y el criterio es económico:
  1. ACUERDO_VIGENTE   precio pactado hoy con el proveedor
  2. COTIZACION        cotización reciente del proveedor
  3. KARDEX_PROMEDIO   costo promedio ponderado de los movimientos
  4. MAESTRO           Producto.precio_compra, si alguien lo cargó a mano

Los dos primeros son costo HACIA ADELANTE: la plata en riesgo es la que se va
a gastar, no la que se gastó. El tercero mira hacia atrás y por eso lleva
bandera de antigüedad.

EL SESGO NO ES NEUTRO. Con ~500 días de inventario, el costo promedio
ponderado refleja compras de hace ~1,5 años. En importados con devaluación de
por medio, SUBESTIMA el costo de reposición. Y subestimar el costo infla Cu y
desinfla Co — las dos empujan el ratio crítico hacia arriba, es decir, hacia
COMPRAR MÁS. En una empresa con 82% de excedente y caja comprometida, ese es
el lado peligroso.

Regla 0 aplicada: ante duda entre dos costos, el MÁS ALTO Y MÁS RECIENTE, y
declarado en la fila.

PRECIO DE VENTA: hoy NO existe en ninguna fuente. El kardex captura
`f470_costo_prom_uni` pero ningún valor de venta, y `Producto.precio_venta` es
0. Se busca un campo de valor defensivamente en la descarga —igual que el
total declarado— para que la primera corrida revele si viene. Mientras tanto
Cu se deriva de un margen supuesto sobre el costo, DECLARADO como supuesto y
no disfrazado de dato.
"""
import logging
from datetime import date, timedelta

from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)

# Un costo del kardex más viejo que esto se marca como añejo. No lo descarta:
# lo declara, para que el comité vea cuál Q* se apoya en un costo de hace año
# y medio y cuál en una cotización de este trimestre.
DIAS_COSTO_ANEJO = 180

# ══════════════════════════════════════════════════════════════════════════
# CONVENCIÓN DE MARGEN — nombrada a propósito.
#
# "Margen" prometía una afirmación y significaba la vecina. La versión anterior
# hacía precio = costo * (1 + m), que es MARKUP SOBRE COSTO. Un margen de 43.5%
# sobre PRECIO equivale a 77% de markup sobre costo — no a 43.5%.
#
# El error no era cosmético: con m=0.435 y Co=0.55*costo, el ratio crítico caía
# de 0.583 a 0.442, z pasaba de +0.21 a -0.146, y el modelo recomendaba pedir
# POR DEBAJO de la media. La palabra decidiendo la compra.
#
#   MARGEN SOBRE PRECIO:  m = (precio - costo) / precio   →  precio = costo/(1-m)
#   MARKUP SOBRE COSTO:   k = (precio - costo) / costo    →  precio = costo*(1+k)
#
# Se usa MARGEN SOBRE PRECIO porque es como el negocio cotiza sus márgenes.
# El nombre de la variable lo dice para que no vuelva a confundirse.
# ══════════════════════════════════════════════════════════════════════════
MARGEN_SOBRE_PRECIO_DEFAULT = 0.40

# Un solo margen global miente sobre catálogos distintos: el importado y el
# nacional no tienen la misma estructura. `origen` ya existe en el maestro.
MARGEN_SOBRE_PRECIO_POR_ORIGEN = {
    'CHINA': 0.435,      # importado — referencia QUIROND
    'IMPORTADO': 0.435,
    'NACIONAL': 0.30,
}


def precio_desde_margen(costo, margen_sobre_precio):
    """precio = costo / (1 - m). NUNCA costo * (1 + m), que es markup."""
    m = float(margen_sobre_precio)
    if costo <= 0:
        return 0.0
    if not 0 < m < 1:
        raise ValueError(
            f'margen_sobre_precio debe estar en (0,1), llegó {m}. '
            f'Un valor >=1 delata que se está pasando un markup.'
        )
    return costo / (1 - m)

FUENTES = ('ACUERDO_VIGENTE', 'COTIZACION', 'KARDEX_PROMEDIO', 'MAESTRO', 'SIN_COSTO')


def _costos_acuerdo_vigente(refs):
    """Precio pactado hoy. Costo hacia adelante, la fuente más confiable."""
    from app.models.acuerdo_marco import AcuerdoMarco
    from app.models.producto import Producto

    hoy = _dia_operativo()
    filas = (
        db.session.query(Producto.codigo_siesa, AcuerdoMarco.precio_unitario,
                         AcuerdoMarco.vigencia_hasta)
        .join(Producto, AcuerdoMarco.producto_id == Producto.id)
        .filter(Producto.codigo_siesa.in_(refs))
        .filter(AcuerdoMarco.vigencia_desde <= hoy)
        .filter(AcuerdoMarco.vigencia_hasta >= hoy)
        .all()
    )
    # Ante varios acuerdos vigentes, el MÁS ALTO. Regla 0: subestimar el costo
    # empuja a comprar de más, que es el lado irreversible.
    salida = {}
    for ref, precio, hasta in filas:
        p = float(precio or 0)
        if p <= 0:
            continue
        if ref not in salida or p > salida[ref]['costo']:
            salida[ref] = {'costo': p, 'fuente': 'ACUERDO_VIGENTE',
                           'vigencia_hasta': hasta.isoformat() if hasta else None}
    return salida


def _costos_cotizacion(refs, meses=6):
    """Cotización reciente. También mira hacia adelante."""
    from app.models.acuerdo_marco import PrecioProveedor
    from app.models.producto import Producto

    desde = _dia_operativo() - timedelta(days=meses * 30)
    filas = (
        db.session.query(Producto.codigo_siesa, PrecioProveedor.precio_unitario,
                         PrecioProveedor.fecha)
        .join(Producto, PrecioProveedor.producto_id == Producto.id)
        .filter(Producto.codigo_siesa.in_(refs))
        .filter(PrecioProveedor.fecha >= desde)
        .all()
    )
    salida = {}
    for ref, precio, fecha in filas:
        p = float(precio or 0)
        if p <= 0:
            continue
        # La más RECIENTE; a igualdad de fecha, la más alta.
        prev = salida.get(ref)
        if (prev is None or fecha > prev['_fecha']
                or (fecha == prev['_fecha'] and p > prev['costo'])):
            salida[ref] = {'costo': p, 'fuente': 'COTIZACION',
                           'fecha_costo': fecha.isoformat(), '_fecha': fecha}
    for v in salida.values():
        v.pop('_fecha', None)
    return salida


def _costos_kardex(refs):
    """Costo promedio ponderado de los movimientos, con antigüedad.

    Mira hacia atrás: con 500 días de inventario refleja compras de ~1,5 años.
    Por eso lleva bandera — no se descarta, se declara.
    """
    from sqlalchemy import func
    from app.services.kardex_service import KardexMovimiento

    filas = (
        db.session.query(
            KardexMovimiento.referencia,
            func.sum(KardexMovimiento.costo_promedio * KardexMovimiento.cantidad),
            func.sum(KardexMovimiento.cantidad),
            func.max(KardexMovimiento.fecha),
        )
        .filter(KardexMovimiento.referencia.in_(refs))
        .filter(KardexMovimiento.costo_promedio > 0)
        .group_by(KardexMovimiento.referencia)
        .all()
    )
    hoy = _dia_operativo()
    salida = {}
    for ref, valor, cant, ultima in filas:
        c = float(cant or 0)
        if c <= 0:
            continue
        dias = (hoy - ultima).days if ultima else None
        salida[ref] = {
            'costo': float(valor or 0) / c,
            'fuente': 'KARDEX_PROMEDIO',
            'fecha_costo': ultima.isoformat() if ultima else None,
            'dias_antiguedad': dias,
            'anejo': dias is not None and dias > DIAS_COSTO_ANEJO,
        }
    return salida


def _precios_realizados(refs):
    """Precio al que de verdad se vende: valor/cantidad de las ventas reales.

    Reemplaza el margen supuesto por margen MEDIDO. Neto de descuentos y de la
    escalera clandestina — el margen que sale de aquí es el que se obtiene.
    """
    from app.models.precio_realizado import PrecioRealizado

    filas = (PrecioRealizado.query
             .filter(PrecioRealizado.referencia.in_(refs))
             .filter(PrecioRealizado.centro_operacion.is_(None))
             .filter(PrecioRealizado.periodo.in_(('VIVO', 'TOTAL')))
             .all())

    # VIVO (últimas 12 semanas, alimentado por la ingesta del Vigía) gana sobre
    # TOTAL (toda la historia del export TXT). El motivo es de negocio: un
    # precio del último trimestre describe mejor lo que hoy se cobra que un
    # promedio que arrastra listas de hace dos años.
    #
    # **Sin ingesta viva, esto se comporta EXACTAMENTE como antes**: si no hay
    # fila VIVO, gana TOTAL, que es la única que existía. Es la garantía de que
    # encender la ingesta no cambia nada hasta que haya datos que lo justifiquen.
    #
    # Y el filtro por periodo no es cosmético: sin él la consulta traía TODAS
    # las filas del SKU —incluidas las semanales— y cuál ganaba en el dict era
    # arbitrario.
    salida = {}
    for f in filas:
        precio = float(f.precio_realizado or 0)
        if precio <= 0:
            continue
        anterior = salida.get(f.referencia)
        if anterior is None or (f.periodo == 'VIVO'):
            salida[f.referencia] = precio
    return salida


def dispersion_precio_por_co(ref):
    """La escalera de precios, medida.

    La dispersión del precio realizado de un MISMO SKU entre C.O. es la
    escalera que existe y nunca se decidió formalmente. No sirve al newsvendor:
    sirve a la política de precios, que hoy se toma a ciegas.
    """
    from app.models.precio_realizado import PrecioRealizado
    filas = (PrecioRealizado.query
             .filter_by(referencia=ref)
             .filter(PrecioRealizado.centro_operacion.isnot(None))
             .all())
    precios = {f.centro_operacion: float(f.precio_realizado or 0)
               for f in filas if float(f.precio_realizado or 0) > 0}
    if len(precios) < 2:
        return {'referencia': ref, 'por_co': precios, 'dispersion_pct': None,
                'nota': 'Se necesitan al menos dos C.O. con ventas para medir la escalera.'}
    lo, hi = min(precios.values()), max(precios.values())
    return {
        'referencia': ref,
        'por_co': precios,
        'minimo': lo, 'maximo': hi,
        'dispersion_pct': round((hi - lo) / lo * 100, 1) if lo > 0 else None,
        'nota': ('Diferencia entre el C.O. que más cobra y el que menos, para el '
                 'mismo producto. Eso es la escalera — no una hipótesis.'),
    }


def resolver_costos(refs, margen_supuesto=MARGEN_SOBRE_PRECIO_DEFAULT):
    """
    Costo y precio por SKU, con la fuente declarada en cada fila.

    Returns: {ref: {costo, fuente, precio_venta, precio_es_supuesto, cu, co, ...}}
    Los SKU sin ninguna fuente salen con fuente='SIN_COSTO' y costo 0 — no se
    omiten en silencio, para que la cobertura se pueda contar por fuente.
    """
    from app.models.producto import Producto

    refs = [r for r in refs if r]
    if not refs:
        return {}

    # Se consultan todas y se elige por jerarquía: una fuente mejor siempre gana
    capas = [_costos_acuerdo_vigente(refs), _costos_cotizacion(refs),
             _costos_kardex(refs)]

    realizados = _precios_realizados(refs)

    maestro = {
        p.codigo_siesa: p for p in
        Producto.query.filter(Producto.codigo_siesa.in_(refs)).all()
        if p.codigo_siesa
    }

    salida = {}
    for ref in refs:
        elegido = None
        for capa in capas:
            if ref in capa:
                elegido = dict(capa[ref])
                break

        prod = maestro.get(ref)
        if elegido is None:
            c = float(getattr(prod, 'precio_compra', 0) or 0)
            elegido = ({'costo': c, 'fuente': 'MAESTRO'} if c > 0
                       else {'costo': 0.0, 'fuente': 'SIN_COSTO'})

        costo = float(elegido['costo'])
        # PRECIO REALIZADO primero: es el que de verdad se cobra. El maestro
        # después. El margen supuesto solo si no hay ninguno de los dos.
        venta = realizados.get(ref, 0.0)
        fuente_precio = 'REALIZADO' if venta > 0 else None
        if venta <= 0:
            venta = float(getattr(prod, 'precio_venta', 0) or 0) if prod else 0.0
            fuente_precio = 'MAESTRO' if venta > 0 else None

        # Cu = margen que se pierde si falta. Sin precio de venta se deriva de
        # un margen SUPUESTO — declarado, no disfrazado de dato.
        precio_supuesto = venta <= 0
        margen_usado = None
        if precio_supuesto:
            # Por ORIGEN cuando se conoce: importado y nacional no comparten
            # estructura de margen, y `origen` ya está en el maestro.
            origen = (getattr(prod, 'origen', '') or '').strip().upper() if prod else ''
            margen_usado = MARGEN_SOBRE_PRECIO_POR_ORIGEN.get(origen, margen_supuesto)
            venta = precio_desde_margen(costo, margen_usado)
        cu = max(venta - costo, 0.0)

        elegido.update({
            'referencia': ref,
            'costo': costo,
            'precio_venta': venta,
            'precio_es_supuesto': precio_supuesto,
            'fuente_precio': fuente_precio or 'MARGEN_SUPUESTO',
            'margen_supuesto': margen_usado,
            'convencion_margen': 'SOBRE_PRECIO' if precio_supuesto else None,
            'origen': (getattr(prod, 'origen', '') or '') if prod else '',
            'cu': round(cu, 2),
            # El supuesto mueve la decisión: ±10 puntos de margen mueven Q* ~24%.
            # Para estas filas el comité no debería ver un punto sino un rango.
            'cu_rango': ([
                round(max(precio_desde_margen(costo, max(0.05, (margen_usado or 0) - 0.10)) - costo, 0), 2),
                round(max(precio_desde_margen(costo, min(0.95, (margen_usado or 0) + 0.10)) - costo, 0), 2),
            ] if precio_supuesto and costo > 0 else None),
            'confiable': elegido['fuente'] in ('ACUERDO_VIGENTE', 'COTIZACION'),
            'nombre': getattr(prod, 'nombre', '') if prod else '',
        })
        salida[ref] = elegido

    return salida


def resumen_por_fuente(costos):
    """Cobertura POR FUENTE, no binaria.

    Un Q* sobre cotización vigente y uno sobre promedio de hace 18 meses no
    tienen la misma calidad, y el comité tiene derecho a ver cuál es cuál.
    """
    conteo = {f: 0 for f in FUENTES}
    anejos = supuestos = 0
    for d in costos.values():
        conteo[d.get('fuente', 'SIN_COSTO')] = conteo.get(d.get('fuente', 'SIN_COSTO'), 0) + 1
        if d.get('anejo'):
            anejos += 1
        if d.get('precio_es_supuesto'):
            supuestos += 1

    total = len(costos)
    con_costo = total - conteo.get('SIN_COSTO', 0)
    hacia_adelante = conteo.get('ACUERDO_VIGENTE', 0) + conteo.get('COTIZACION', 0)
    return {
        'total_skus': total,
        'con_costo': con_costo,
        'sin_costo': conteo.get('SIN_COSTO', 0),
        'por_fuente': conteo,
        'costo_hacia_adelante': hacia_adelante,
        'costo_anejo': anejos,
        'precio_supuesto': supuestos,
        'pct_con_costo': round(con_costo / total * 100, 1) if total else 0,
        'pct_hacia_adelante': round(hacia_adelante / total * 100, 1) if total else 0,
        'nota': (
            'costo_hacia_adelante (acuerdo o cotización) es la plata que se va a '
            'gastar. KARDEX_PROMEDIO mira hacia atrás y con 500 días de inventario '
            'SUBESTIMA el costo de reposición — subestimar el costo infla Cu y '
            'desinfla Co, y ambas empujan a comprar MÁS.'
        ),
        'advertencia_precio': (
            f'{supuestos} SKU(s) usan margen SUPUESTO porque no hay precio de venta '
            f'en ninguna fuente. Su Cu no es un dato: es una hipótesis.'
        ) if supuestos else None,
    }
