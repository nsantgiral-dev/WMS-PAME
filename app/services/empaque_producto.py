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
    """Como `empaques_de`, partiendo de ids (lee los productos)."""
    from app.extensions import db
    from app.models.producto import Producto

    ids = sorted({int(i) for i in producto_ids if i})
    prods = []
    for i in range(0, len(ids), _LOTE_IN):
        prods.extend(db.session.query(Producto)
                     .filter(Producto.id.in_(ids[i:i + _LOTE_IN])).all())
    return empaques_de(prods)


def empaque_de(producto):
    """El paquete de UN producto, o `None` si no viene en paquete."""
    if producto is None:
        return None
    return empaques_de([producto]).get(producto.id)


def descomponer(unidades: int, factor: int):
    """`(paquetes completos, sueltas)` de una cantidad en unidades."""
    unidades = int(unidades or 0)
    if not factor or factor <= 1 or unidades <= 0:
        return 0, max(unidades, 0)
    return unidades // factor, unidades % factor


def _entero(valor, campo: str) -> int:
    if isinstance(valor, bool) or valor is None:
        raise ValueError(f'{campo} debe ser un número entero')
    if isinstance(valor, float) and valor.is_integer():
        valor = int(valor)
    if isinstance(valor, str) and valor.strip().isdigit():
        valor = int(valor.strip())
    if not isinstance(valor, int) or valor < 0:
        raise ValueError(f'{campo} debe ser un número entero mayor o igual a 0')
    return valor


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

    con_paquete = (db.session.query(ProductoEmpaque.producto_id)
                   .filter(ProductoEmpaque.activo.is_(True),
                           ProductoEmpaque.factor_conversion > 1,
                           ProductoEmpaque.origen != ORIGEN_LPN))
    declarados = (Producto.query
                  .filter(Producto.activo.is_(True),
                          Producto.unidad_empaque.isnot(None),
                          db.func.upper(db.func.trim(Producto.unidad_empaque)) != 'UND',
                          db.func.trim(Producto.unidad_empaque) != '',
                          Producto.id.notin_(con_paquete))
                  .order_by(Producto.codigo))
    total_declarados = declarados.count()
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
                          for p in declarados.limit(limite)],
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
