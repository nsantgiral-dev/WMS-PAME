"""El paquete de un producto — **una política, una función** (Regla 0).

¿En qué paquete viene este producto y cuántas unidades trae? Antes había dos
respuestas:

· `traslado_service._resolver_empaque` miraba `Producto.factor_conversion` y
  caía a `producto_empaques`.
· `mobile_service` (el HUD del picking) miraba **solo** `Producto`, cuyo
  `factor_conversion` está en 1 para casi todo el catálogo: medido en
  producción el 2026-10-02, 6 productos con factor > 1 contra 234 con su
  paquete en `producto_empaques`. El picking casi nunca mostraba un paquete
  aunque el dato existiera.

Ahora esta es la única que contesta. El orden:

1. `producto_empaques` activo, con factor > 1, que vino de Siesa (no un LPN del
   WMS: una paca de recepción es un bulto físico, no la unidad comercial, y su
   unidad no existe en Siesa). Si hay varios, gana el que coincide con la
   unidad de empaque que Siesa declara para el ítem (`Producto.unidad_empaque`,
   `f120_id_unidad_empaque`); si ninguno coincide, el de menor factor.
2. Si no hay ninguno, `Producto.unidad_empaque` + `Producto.factor_conversion`
   (el dato viejo del producto) cuando el factor es > 1.
3. Si no, el producto no viene en paquete: `None`.

**El paquete solo cambia cómo se ve y cómo se pide.** Existencias, Siesa,
conteo y picking siguen en unidades. `unidades_de_linea` convierte lo que la
persona eligió (paquetes + sueltas) a unidades con el factor vigente, y es la
única que lo hace en el servidor.

Lo que **no** pasa por acá, a propósito: el escaneo del EAN del empaque en
`mobile_service._es_escaneo_empaque`/`_unidades_del_escaneo` y la heurística de
packing. Esos leen la PAREJA código de barras + factor guardada en el producto
—el código escaneado define su propio factor—, no deciden «en qué paquete viene».
Cambiarles la fuente cambiaría lo que cuenta un escaneo. Están declarados en
`tests/test_empaque_una_politica.py`.
"""
from dataclasses import dataclass

from app.utils.entero_json import entero_no_negativo as _entero

#: `producto_empaques.origen` de las pacas que crea el WMS en recepción.
ORIGEN_LPN = 'WMS_LPN'

FUENTE_EMPAQUES = 'PRODUCTO_EMPAQUES'
FUENTE_PRODUCTO = 'PRODUCTO'

#: Postgres admite ~32k parámetros; 900 deja margen y sirve igual en sqlite.
_LOTE_IN = 900


@dataclass(frozen=True)
class Empaque:
    unidad: str
    factor: int
    fuente: str

    def a_dict(self) -> dict:
        return {'unidad': self.unidad, 'factor': self.factor, 'fuente': self.fuente}


def _norm(unidad) -> str:
    return (unidad or '').strip().upper()


def _elegir(producto, candidatos):
    """`candidatos`: [(unidad, factor)] de producto_empaques ya filtrados."""
    declarada = _norm(getattr(producto, 'unidad_empaque', None))
    if candidatos:
        preferidos = [c for c in candidatos if c[0] == declarada] or candidatos
        unidad, factor = min(preferidos, key=lambda c: (c[1], c[0]))
        return Empaque(unidad, factor, FUENTE_EMPAQUES)
    factor_producto = getattr(producto, 'factor_conversion', None) or 1
    if declarada and declarada != 'UND' and factor_producto > 1:
        return Empaque(declarada, int(factor_producto), FUENTE_PRODUCTO)
    return None


def empaques_de(productos) -> dict:
    """`{producto_id: Empaque}` para varios productos, en una consulta por lote.

    Los productos sin paquete no aparecen en el dict. Acepta objetos `Producto`
    (no los vuelve a leer)."""
    from app.extensions import db
    from app.models.producto_empaque import ProductoEmpaque

    prods = {p.id: p for p in productos if p is not None and p.id is not None}
    if not prods:
        return {}
    candidatos = {}
    ids = list(prods)
    for i in range(0, len(ids), _LOTE_IN):
        filas = (db.session.query(ProductoEmpaque.producto_id,
                                  ProductoEmpaque.unidad_medida,
                                  ProductoEmpaque.factor_conversion)
                 .filter(ProductoEmpaque.producto_id.in_(ids[i:i + _LOTE_IN]),
                         ProductoEmpaque.activo.is_(True),
                         ProductoEmpaque.factor_conversion > 1,
                         ProductoEmpaque.origen != ORIGEN_LPN)
                 .all())
        for pid, unidad, factor in filas:
            u = _norm(unidad)
            if u and u != 'UND':
                candidatos.setdefault(pid, set()).add((u, int(factor)))
    resultado = {}
    for pid, prod in prods.items():
        emp = _elegir(prod, sorted(candidatos.get(pid, ())))
        if emp:
            resultado[pid] = emp
    return resultado


def empaques_por_id(producto_ids) -> dict:
    """Como `empaques_de`, partiendo de ids.

    Lee del producto **solo** las tres columnas que la política usa (`id`,
    `unidad_empaque`, `factor_conversion`), no la fila ORM completa: la lista
    de Pedir de una tienda trae ~4.000 productos en cada apertura."""
    from app.extensions import db
    from app.models.producto import Producto

    ids = sorted({int(i) for i in producto_ids if i})
    prods = []
    for i in range(0, len(ids), _LOTE_IN):
        prods.extend(db.session.query(Producto.id, Producto.unidad_empaque,
                                      Producto.factor_conversion)
                     .filter(Producto.id.in_(ids[i:i + _LOTE_IN])).all())
    return empaques_de(prods)


def empaque_de(producto):
    """El paquete de UN producto, o `None` si no viene en paquete."""
    if producto is None:
        return None
    return empaques_de([producto]).get(producto.id)


def unidad_para_siesa(producto):
    """`(unidad, factor)` con que el STS/ETS mueve este producto en Siesa, o
    `('', 1)` (en unidades).

    **No es `empaque_de`, a propósito.** `empaque_de` contesta cómo se ve y
    cómo se pide; esta, en qué unidad viaja el documento. Al unificarlas el
    2026-10-02, los paquetes sin código de barras que el sync empezó a guardar
    (Paso E) pasaron también al payload: más productos salían como PQ
    fraccionado (29 und → PQ 2,4167) sin haberse probado nunca contra Siesa, y
    al dueño se le había dicho que lo que va a Siesa no cambiaba. Esta es la
    regla de antes, idéntica:

    1. `Producto.unidad_empaque` con `factor_conversion` > 1;
    2. si no, la fila activa de `producto_empaques` de menor factor > 1 **con
       código de barras** (las sin código no existían antes del 2026-10-02).

    Única diferencia deliberada con la de antes: una paca del WMS (`WMS_LPN`)
    no cuenta — su unidad (PACA) es un bulto de recepción, no una unidad que el
    ítem tenga en Siesa.

    Las cantidades no cambian por la unidad: el payload divide por el factor y
    Siesa multiplica. Lo que sigue sin probar en vivo es el redondeo de la
    fracción (5 und de un PQ × 12 → 0,4167): prueba pendiente del dueño.
    """
    if producto is None:
        return '', 1
    if producto.unidad_empaque and (producto.factor_conversion or 1) > 1:
        return producto.unidad_empaque, producto.factor_conversion
    from app.models.producto_empaque import ProductoEmpaque
    emp = (ProductoEmpaque.query
           .filter(ProductoEmpaque.producto_id == producto.id,
                   ProductoEmpaque.factor_conversion > 1,
                   ProductoEmpaque.activo.is_(True),
                   ProductoEmpaque.codigo_barras.isnot(None),
                   ProductoEmpaque.origen != ORIGEN_LPN)
           .order_by(ProductoEmpaque.factor_conversion, ProductoEmpaque.id)
           .first())
    if emp:
        return emp.unidad_medida, emp.factor_conversion
    return '', 1


def unidades_de_linea(datos: dict, empaque, campo_cantidad: str, nombre: str = ''):
    """Lo que la persona pidió en una línea, en unidades.

    Devuelve `(total, paquetes, sueltas)`. `paquetes`/`sueltas` son `None` si
    la línea llegó solo en unidades (pantallas viejas, productos sin paquete).

    Si llegan paquetes o sueltas, **el total lo calcula el servidor** con el
    factor vigente. Si la pantalla mandó además el total y no cuadra, se
    rechaza: el factor cambió entre que se pintó la lista y que se envió, o la
    pantalla calculó mal — en los dos casos la persona tiene que ver la cifra
    real antes de pedir.
    """
    etiqueta = f' ({nombre})' if nombre else ''
    trae_paquetes = 'paquetes' in datos and datos.get('paquetes') is not None
    trae_sueltas = 'sueltas' in datos and datos.get('sueltas') is not None
    if not (trae_paquetes or trae_sueltas):
        total = _entero(datos.get(campo_cantidad), campo_cantidad)
        return total, None, None

    paquetes = _entero(datos.get('paquetes') or 0, 'paquetes')
    sueltas = _entero(datos.get('sueltas') or 0, 'sueltas')
    if paquetes and empaque is None:
        raise ValueError(f'El producto{etiqueta} no viene en paquete: '
                         'pídalo en unidades.')
    factor = empaque.factor if empaque else 1
    total = paquetes * factor + sueltas
    declarado = datos.get(campo_cantidad)
    if declarado is not None and _entero(declarado, campo_cantidad) != total:
        raise ValueError(
            f'La cantidad{etiqueta} no cuadra: {paquetes} '
            f'{empaque.unidad if empaque else "paquetes"} × {factor} + {sueltas} '
            f'sueltas son {total} unidades, no {declarado}. Recargue la lista '
            'y vuelva a pedir.')
    return total, paquetes, sueltas


def paquetes_por_revisar(limite: int = 500) -> dict:
    """Lo que compras tiene que completar en Siesa para que un producto se
    pueda pedir por paquete. Solo lee.

    · `declarado_sin_factor`: Siesa dice que el ítem tiene unidad de empaque
      (`f120_id_unidad_empaque`) pero el WMS no conoce cuántas unidades trae.
      Sin factor no se puede pedir en paquetes.
    · `paquete_sin_codigo`: el paquete y su factor sí están, pero no tiene un
      código de barras propio: se puede pedir por paquete, no escanearlo.
    · `sin_factor_ultimo_sync`: códigos de barras de paquete que el último sync
      descartó porque Siesa no les declara factor (en memoria: se pierde al
      reiniciar el servidor).
    """
    from app.extensions import db
    from app.models.producto import Producto
    from app.models.producto_empaque import ProductoEmpaque
    from app.services import empaques_sync_service

    # La misma política que `empaque_de`: un producto que ella ya resuelve
    # (por su fila de producto_empaques o por el dato del producto) se puede
    # pedir por paquete y no se manda a compras a completarlo.
    candidatos = (Producto.query
                  .filter(Producto.activo.is_(True),
                          Producto.unidad_empaque.isnot(None),
                          db.func.upper(db.func.trim(Producto.unidad_empaque)) != 'UND',
                          db.func.trim(Producto.unidad_empaque) != '')
                  .order_by(Producto.codigo)
                  .all())
    resueltos = empaques_de(candidatos)
    declarados = [p for p in candidatos if p.id not in resueltos]
    total_declarados = len(declarados)
    sin_codigo = (db.session.query(ProductoEmpaque, Producto)
                  .join(Producto, Producto.id == ProductoEmpaque.producto_id)
                  .filter(ProductoEmpaque.activo.is_(True),
                          ProductoEmpaque.codigo_barras.is_(None),
                          ProductoEmpaque.factor_conversion > 1)
                  .order_by(Producto.codigo))
    total_sin_codigo = sin_codigo.count()
    ultimo = (empaques_sync_service.get_estado().get('ultimo_resultado') or {})
    return {
        'declarado_sin_factor': {
            'total': total_declarados,
            'productos': [{'codigo': p.codigo, 'codigo_siesa': p.codigo_siesa,
                           'nombre': p.nombre, 'unidad_empaque': p.unidad_empaque}
                          for p in declarados[:limite]],
        },
        'paquete_sin_codigo': {
            'total': total_sin_codigo,
            'productos': [{'codigo': p.codigo, 'codigo_siesa': p.codigo_siesa,
                           'nombre': p.nombre, 'unidad': e.unidad_medida,
                           'factor': e.factor_conversion}
                          for e, p in sin_codigo.limit(limite)],
        },
        'sin_factor_ultimo_sync': ultimo.get('sin_factor_q35_refs'),
        'limite': limite,
    }
