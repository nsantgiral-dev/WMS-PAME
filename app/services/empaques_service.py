"""
Servicio central de empaques y LPN.

Tres responsabilidades:
  1. scan_barcode()          → dado un código escaneado, retorna producto + factor + ambigüedad
  2. descomponer_en_empaques() → dado N UNDs, retorna la combinación óptima (pacas + sueltas)
  3. generar_lpn()           → crea un LPN físico para una paca sin código único
"""
import logging
from datetime import datetime
from app.extensions import db
from app.models.lpn import EstadoLPN
from app.models.producto import Producto
from app.models.producto_empaque import ProductoEmpaque
from app.models.lpn import LPN

logger = logging.getLogger(__name__)


# ── 1. SCAN ───────────────────────────────────────────────────────────────────

def scan_barcode(codigo_barras: str, almacen_id: int = None):
    """
    Dado un código escaneado, resuelve qué es y qué representa.

    Retorna un dict con:
      tipo: 'GS1_UNICO' | 'GS1_AMBIGUO' | 'LPN' | 'EAN_BASE' | 'NO_ENCONTRADO'
      producto: dict del producto
      empaque: dict del empaque (si aplica)
      factor: int — cuántas UNDs base representa este escaneo
      ambiguos: lista de empaques si tipo='GS1_AMBIGUO'

    Lógica:
      1. ¿Es un LPN? → lookup directo en tabla lpn
      2. ¿Está en producto_empaques? → puede ser único o ambiguo
      3. ¿Está en productos.codigo_barras? → EAN-13 base (factor=1)
      4. No encontrado
    """
    codigo = codigo_barras.strip()

    # ── Paso 1: ¿Es un LPN conocido? ──────────────────────────────────────────
    if codigo.startswith('LPN-'):
        lpn = LPN.query.filter_by(codigo=codigo).first()
        if lpn and lpn.estado == EstadoLPN.ACTIVO:
            return {
                'tipo': 'LPN',
                'producto': lpn.producto.to_dict() if lpn.producto else None,
                'lpn': lpn.to_dict(),
                'factor': lpn.cantidad_actual,
                'empaque': lpn.empaque.to_dict() if lpn.empaque else None,
            }
        return {'tipo': 'NO_ENCONTRADO', 'codigo': codigo, 'razon': 'LPN no existe o no está ACTIVO'}

    # ── Paso 2: ¿Está en producto_empaques? ───────────────────────────────────
    empaques = ProductoEmpaque.buscar_todos_por_barcode(codigo)

    if len(empaques) == 1:
        emp = empaques[0]
        if emp.factor_conversion == 1:
            return {
                'tipo': 'EAN_BASE',
                'producto': emp.producto.to_dict() if emp.producto else None,
                'empaque': emp.to_dict(),
                'factor': 1,
            }
        return {
            'tipo': 'GS1_UNICO',
            'producto': emp.producto.to_dict() if emp.producto else None,
            'empaque': emp.to_dict(),
            'factor': emp.factor_conversion,
        }

    if len(empaques) > 1:
        # Mismo código en múltiples productos o múltiples empaques → ambigüedad
        return {
            'tipo': 'GS1_AMBIGUO',
            'codigo': codigo,
            'ambiguos': [e.to_dict() for e in empaques],
            'factor': None,
        }

    # ── Paso 3: ¿Está en productos.codigo_barras? ─────────────────────────────
    prod = Producto.query.filter_by(codigo_barras=codigo, activo=True).first()
    if prod:
        return {
            'tipo': 'EAN_BASE',
            'producto': prod.to_dict(),
            'empaque': None,
            'factor': 1,
        }

    # ── No encontrado ──────────────────────────────────────────────────────────
    return {'tipo': 'NO_ENCONTRADO', 'codigo': codigo, 'razon': 'Código no registrado en WMS'}


# ── 2. DESCOMPOSICIÓN ─────────────────────────────────────────────────────────

def descomponer_en_empaques(producto_id: int, cantidad_solicitada: int, almacen_id: int):
    """
    Dado un producto y una cantidad en UNDs, calcula la combinación óptima
    de LPNs disponibles + unidades sueltas.

    Algoritmo:
      1. Busca el empaque de mayor factor disponible en producto_empaques
      2. Cuenta LPNs ACTIVOS de ese producto en el almacén
      3. Calcula cuántos LPNs completos se pueden usar
      4. El resto va como UNDs sueltas

    Retorna:
      {
        'lpns': [lista de LPN.to_dict()],        # pacas a escanear (PASO 1)
        'sueltas': int,                           # UNDs a tomar de ubicación suelta (PASO 2)
        'factor_empaque': int,
        'unidad_empaque': str,
        'lpns_disponibles_total': int,
        'alcanzable': bool,                       # False si no hay suficiente stock
        'cantidad_solicitada': int,
        'cantidad_alcanzable': int,
      }
    """
    # Busca el empaque de mayor factor (el nivel de empaque más grande disponible)
    empaque = (ProductoEmpaque.query
               .filter_by(producto_id=producto_id, activo=True)
               .filter(ProductoEmpaque.factor_conversion > 1)
               .filter(ProductoEmpaque.origen == 'SIESA_GS1')
               .order_by(ProductoEmpaque.factor_conversion.desc())
               .first())

    lpns_disponibles = LPN.buscar_activos_por_producto(producto_id, almacen_id)

    if not empaque or not lpns_disponibles:
        # Sin empaques o sin LPNs físicos → todo suelto
        return {
            'lpns': [],
            'sueltas': cantidad_solicitada,
            'factor_empaque': 1,
            'unidad_empaque': 'UND',
            'lpns_disponibles_total': len(lpns_disponibles),
            'alcanzable': True,  # el stock suelto lo resuelve inventario normal
            'cantidad_solicitada': cantidad_solicitada,
            'cantidad_alcanzable': cantidad_solicitada,
        }

    factor = empaque.factor_conversion
    pacas_necesarias = cantidad_solicitada // factor
    sueltas_necesarias = cantidad_solicitada % factor

    # No usar más LPNs de los necesarios ni más de los disponibles
    pacas_a_usar = min(pacas_necesarias, len(lpns_disponibles))
    unidades_con_pacas = pacas_a_usar * factor
    sueltas_a_usar = cantidad_solicitada - unidades_con_pacas

    cantidad_alcanzable = unidades_con_pacas + sueltas_a_usar  # siempre == cantidad_solicitada si hay stock suelto

    return {
        'lpns': [lpn.to_dict() for lpn in lpns_disponibles[:pacas_a_usar]],
        'sueltas': sueltas_a_usar,
        'factor_empaque': factor,
        'unidad_empaque': empaque.unidad_medida,
        'lpns_disponibles_total': len(lpns_disponibles),
        'alcanzable': True,
        'cantidad_solicitada': cantidad_solicitada,
        'cantidad_alcanzable': cantidad_alcanzable,
    }


# ── 3. GENERACIÓN DE LPN ──────────────────────────────────────────────────────

def generar_lpn(producto_id: int, cantidad_actual: int, almacen_id: int,
                empaque_id: int = None, recepcion_id: int = None,
                creado_por_id: int = None, notas: str = None):
    """
    Crea un nuevo LPN para una paca física.
    Usado en recepción cuando:
      - El código es ambiguo (mismo barcode en varios niveles)
      - La paca no tiene ningún código de barras
      - El inventario existente necesita etiquetado en primer toque

    Retorna el LPN creado (sin hacer commit — el caller hace commit).
    """
    empaque = ProductoEmpaque.query.get(empaque_id) if empaque_id else None
    factor = empaque.factor_conversion if empaque else cantidad_actual

    codigo = LPN.generar_codigo()

    lpn = LPN(
        codigo=codigo,
        producto_id=producto_id,
        empaque_id=empaque_id,
        factor_conversion=factor,
        cantidad_actual=cantidad_actual,
        estado='ACTIVO',
        almacen_id=almacen_id,
        recepcion_id=recepcion_id,
        creado_por_id=creado_por_id,
        notas=notas,
        fecha_creacion=datetime.utcnow(),
    )

    # [17] Guardar referencia_item con None-check para evitar AttributeError si producto no existe
    _prod_ref = Producto.query.get(producto_id)
    _ref_item = (_prod_ref.codigo_siesa or _prod_ref.codigo) if _prod_ref else ''

    # Crear el empaque en producto_empaques con origen WMS_LPN para que sea escaneable
    empaque_lpn = ProductoEmpaque(
        producto_id=producto_id,
        referencia_item=_ref_item,
        codigo_barras=codigo,
        unidad_medida='PACA',
        factor_conversion=cantidad_actual,
        origen='WMS_LPN',
        activo=True,
    )

    db.session.add(empaque_lpn)
    db.session.flush()  # obtener empaque_lpn.id antes de usarlo
    lpn.empaque_id = empaque_lpn.id
    db.session.add(lpn)

    logger.info(f'[EMPAQUES] LPN generado: {codigo} | producto={producto_id} | qty={cantidad_actual}')
    return lpn


def consumir_lpn(codigo_lpn: str):
    """
    Marca un LPN como CONSUMIDO (la paca fue abierta).
    El caller es responsable de incrementar el stock suelto del producto.
    Retorna (lpn, unidades_liberadas) o lanza ValueError si no es válido.
    """
    lpn = LPN.query.filter_by(codigo=codigo_lpn).first()
    if not lpn:
        raise ValueError(f'LPN {codigo_lpn} no existe')
    if lpn.estado != EstadoLPN.ACTIVO:
        raise ValueError(f'LPN {codigo_lpn} no está ACTIVO (estado actual: {lpn.estado})')

    unidades = lpn.cantidad_actual
    lpn.consumir()
    return lpn, unidades
