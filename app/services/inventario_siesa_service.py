"""
Sincronización de inventario Siesa ↔ WMS.

Dos operaciones:

1. CARGA INICIAL (cargar_inventario_siesa)
   - Descarga todas las existencias de Siesa (API_v2_Inventarios_InvFecha)
     filtrando por bodega (NB1 o lo que diga CONNEKTA_BODEGA).
   - Para cada producto con existencia > 0:
       a. Busca el producto en WMS por f120_referencia.
       b. Crea o actualiza UbicacionProducto en la ubicación SIESA-GENERAL.
       c. Registra MovimientoInventario tipo CARGA_INICIAL_SIESA (idempotente por día).
   - Corre en hilo de fondo (puede tardar minutos con 5000 productos).
   - Idempotente: correr dos veces el mismo día no duplica nada.

2. RECONCILIACIÓN (reconciliar_inventario)
   - Descarga existencias de Siesa (mismo proceso).
   - Compara con totales WMS por producto (SUM de todas las ubicaciones).
   - NO modifica nada. Solo informa diferencias.
   - Retorna lista de discrepancias ordenada por diferencia absoluta.
   - El admin decide: "aceptar Siesa" (ajuste WMS) o "hacer conteo físico".

Regla de oro: funciona en producción con 5000+ productos y 200 pedidos/día.
"""
import logging
import threading
from datetime import datetime, timezone
from sqlalchemy import func
from app.extensions import db
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.almacen import Almacen
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_CODIGO_UBICACION_GENERAL = 'SIESA-GENERAL'

# Estado compartido del proceso en background
_estado_carga = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


# ─────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────

def _get_almacen(app_context=True):
    """Resuelve el almacén que corresponde a la bodega Connekta."""
    almacen = Almacen.query.filter_by(codigo=connekta.bodega, activo=True).first()
    if not almacen:
        almacen = Almacen.query.filter_by(activo=True).first()
    return almacen


def _get_o_crear_ubicacion_general(almacen_id: int) -> Ubicacion:
    """
    Devuelve la ubicación SIESA-GENERAL del almacén.
    La crea si no existe — representa el stock sin ubicación asignada todavía.
    """
    ub = Ubicacion.query.filter_by(codigo=_CODIGO_UBICACION_GENERAL,
                                    almacen_id=almacen_id).first()
    if not ub:
        ub = Ubicacion(
            codigo=_CODIGO_UBICACION_GENERAL,
            almacen_id=almacen_id,
            zona='GENERAL',
            tipo='estanteria',
            activo=True
        )
        db.session.add(ub)
        db.session.flush()
    return ub


def _descargar_inventario_siesa():
    """
    Descarga existencias de Siesa SIN filtro en la API (el API rechaza f150_id
    como parámetro igual que en OCs). Filtra por bodega en Python.

    Retorna dict {codigo_producto: {existencia, comprometido, ubicacion_aux}}
    agregado por producto (un producto puede aparecer en múltiples lotes/ubicaciones).
    Cubre catálogos de hasta 50 000 filas de inventario.
    """
    api = 'API_v2_Inventarios_InvFecha'
    inventario = {}
    bodega = connekta.bodega  # filtro Python

    for pag in range(1, 501):  # hasta 50 000 filas (500 págs × 100)
        resp = connekta._get(api, {
            'paginacion': f'numPag={pag}|tamPag=100'
        })
        rows = resp.get('detalle', {}).get('Table', [])
        if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})):
            break

        for row in rows:
            # Filtrar por bodega en Python
            if (row.get('f150_id') or '').strip() != bodega:
                continue

            codigo = (row.get('f120_referencia') or '').strip()
            if not codigo:
                continue

            existencia = float(row.get('f400_cant_existencia_1') or 0)
            comprometido = float(row.get('f400_cant_comprometida_1') or 0)
            ubicacion_aux = (row.get('f400_id_ubicacion_aux') or '').strip()

            if codigo not in inventario:
                inventario[codigo] = {'existencia': 0.0, 'comprometido': 0.0, 'ubicacion_aux': ubicacion_aux}
            inventario[codigo]['existencia'] += existencia
            inventario[codigo]['comprometido'] += comprometido

        logger.info(f'[INV-SIESA] Página {pag}: {len(rows)} filas totales · {len(inventario)} productos en {bodega}')
        if len(rows) < 100:
            break

    logger.info(f'[INV-SIESA] Total descargado: {len(inventario)} productos en bodega {connekta.bodega}')
    return inventario


# ─────────────────────────────────────────────
# 1. CARGA INICIAL
# ─────────────────────────────────────────────

def _run_carga_inicial(app):
    """Lógica real de la carga inicial — corre en hilo de fondo."""
    global _estado_carga

    with app.app_context():
        fecha_hoy = datetime.utcnow().strftime('%Y%m%d')
        cargados = 0
        actualizados = 0
        sin_producto_wms = 0
        errores = 0

        try:
            almacen = _get_almacen()
            if not almacen:
                raise ValueError('No hay almacén activo en WMS — crea uno primero')

            ub_general = _get_o_crear_ubicacion_general(almacen.id)
            db.session.commit()

            inventario_siesa = _descargar_inventario_siesa()

            for codigo, datos in inventario_siesa.items():
                existencia_siesa = int(round(datos['existencia']))
                if existencia_siesa <= 0:
                    continue

                try:
                    prod = (Producto.query.filter_by(codigo_siesa=codigo).first()
                            or Producto.query.filter_by(codigo=codigo).first())

                    if not prod:
                        sin_producto_wms += 1
                        continue

                    # Intentar usar la ubicación real de Siesa si existe en WMS
                    codigo_ub = datos.get('ubicacion_aux') or _CODIGO_UBICACION_GENERAL
                    ub = (Ubicacion.query.filter_by(codigo=codigo_ub, almacen_id=almacen.id).first()
                          or ub_general)

                    reg = UbicacionProducto.query.filter_by(
                        ubicacion_id=ub.id,
                        producto_id=prod.id,
                        lote=None
                    ).first()

                    # Idempotencia: clave única por producto + día
                    ikey = f'SIESA-INI-{prod.id}-{fecha_hoy}'
                    movimiento_existente = MovimientoInventario.query.filter_by(
                        idempotency_key=ikey
                    ).first()

                    if movimiento_existente:
                        continue  # Ya se cargó hoy

                    saldo_antes = reg.cantidad if reg else 0

                    if reg:
                        reg.cantidad = existencia_siesa
                        reg.row_version += 1
                        actualizados += 1
                    else:
                        reg = UbicacionProducto(
                            ubicacion_id=ub.id,
                            producto_id=prod.id,
                            cantidad=existencia_siesa,
                            fecha_ingreso=datetime.utcnow()
                        )
                        db.session.add(reg)
                        db.session.flush()
                        cargados += 1

                    movimiento = MovimientoInventario(
                        producto_id=prod.id,
                        ubicacion_id=ub.id,
                        almacen_id=almacen.id,
                        tipo='CARGA_INICIAL_SIESA',
                        cantidad=existencia_siesa,
                        saldo_antes=saldo_antes,
                        saldo_despues=existencia_siesa,
                        motivo=f'Carga inicial desde Siesa {fecha_hoy} · bodega {connekta.bodega}',
                        numero_documento='CARGA-SIESA',
                        idempotency_key=ikey
                    )
                    db.session.add(movimiento)

                    # Commit cada 200 productos para no acumular transacciones enormes
                    if (cargados + actualizados) % 200 == 0:
                        db.session.commit()
                        logger.info(f'[INV-SIESA] Commit parcial: {cargados} cargados · {actualizados} actualizados')

                except Exception as e:
                    logger.warning(f'[INV-SIESA] Error en producto {codigo}: {e}')
                    db.session.rollback()
                    errores += 1

            db.session.commit()

        except Exception as e:
            logger.error(f'[INV-SIESA] Error en carga inicial: {e}')
            db.session.rollback()
            _estado_carga['ultimo_error'] = str(e)
            _estado_carga['en_curso'] = False
            return

        resultado = {
            'timestamp': datetime.utcnow().isoformat(),
            'cargados': cargados,
            'actualizados': actualizados,
            'sin_producto_wms': sin_producto_wms,
            'errores': errores,
            'total_siesa': len(inventario_siesa)
        }
        logger.info(f'[INV-SIESA] Carga inicial completada: {resultado}')
        _estado_carga['ultimo_resultado'] = resultado
        _estado_carga['ultimo_error'] = None
        _estado_carga['en_curso'] = False


def iniciar_carga_inventario(app):
    """Arranca la carga inicial en background. Retorna estado inmediatamente."""
    global _estado_carga

    if _estado_carga['en_curso']:
        return {'en_curso': True, 'mensaje': 'Carga ya en proceso — espera que termine'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    _estado_carga['en_curso'] = True
    _estado_carga['ultimo_inicio'] = datetime.now(timezone.utc)

    hilo = threading.Thread(target=_run_carga_inicial, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Carga de inventario iniciada — refresca en ~60 seg'}


def estado_carga_inventario():
    return {
        'en_curso': _estado_carga['en_curso'],
        'ultimo_inicio': _estado_carga['ultimo_inicio'].isoformat() if _estado_carga['ultimo_inicio'] else None,
        'ultimo_resultado': _estado_carga['ultimo_resultado'],
        'ultimo_error': _estado_carga['ultimo_error'],
    }


# ─────────────────────────────────────────────
# 2. RECONCILIACIÓN (background — puede tardar 2+ min)
# ─────────────────────────────────────────────

_estado_reconciliacion = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}


def _run_reconciliacion(app):
    """Lógica real de reconciliación — corre en hilo de fondo."""
    global _estado_reconciliacion

    with app.app_context():
        try:
            # Stock WMS: una sola query bulk (no N+1)
            stock_wms_rows = (
                db.session.query(
                    UbicacionProducto.producto_id,
                    func.sum(UbicacionProducto.cantidad).label('total')
                )
                .group_by(UbicacionProducto.producto_id)
                .all()
            )
            stock_wms = {row.producto_id: int(row.total) for row in stock_wms_rows}
            productos_ids = set(stock_wms.keys())

            inventario_siesa = _descargar_inventario_siesa()

            codigos_siesa = list(inventario_siesa.keys())
            prods_siesa = (
                Producto.query
                .filter(
                    db.or_(
                        Producto.codigo_siesa.in_(codigos_siesa),
                        Producto.codigo.in_(codigos_siesa)
                    )
                )
                .all()
            )
            mapa_codigo = {}
            for p in prods_siesa:
                if p.codigo_siesa:
                    mapa_codigo[p.codigo_siesa] = p
                mapa_codigo[p.codigo] = p

            discrepancias = []

            for codigo, datos in inventario_siesa.items():
                existencia_siesa = int(round(datos['existencia']))
                prod = mapa_codigo.get(codigo)
                if not prod:
                    continue
                total_wms = stock_wms.get(prod.id, 0)
                diferencia = total_wms - existencia_siesa
                if diferencia != 0:
                    discrepancias.append({
                        'producto_id': prod.id,
                        'codigo': prod.codigo,
                        'nombre': prod.nombre,
                        'stock_wms': total_wms,
                        'stock_siesa': existencia_siesa,
                        'diferencia': diferencia,
                        'diferencia_abs': abs(diferencia),
                        'estado': 'WMS_MAYOR' if diferencia > 0 else 'SIESA_MAYOR'
                    })
                productos_ids.discard(prod.id)

            for prod_id in productos_ids:
                total_wms = stock_wms.get(prod_id, 0)
                if total_wms == 0:
                    continue
                prod = Producto.query.get(prod_id)
                if not prod:
                    continue
                discrepancias.append({
                    'producto_id': prod_id,
                    'codigo': prod.codigo,
                    'nombre': prod.nombre,
                    'stock_wms': total_wms,
                    'stock_siesa': 0,
                    'diferencia': total_wms,
                    'diferencia_abs': total_wms,
                    'estado': 'SOLO_WMS'
                })

            discrepancias.sort(key=lambda x: x['diferencia_abs'], reverse=True)

            ts = datetime.utcnow().isoformat()

            # Disparar creación automática de tareas de logística inversa
            try:
                from app.services.devolucion_service import crear_tareas_desde_discrepancias
                almacen = _get_almacen()
                if almacen:
                    resumen_dev = crear_tareas_desde_discrepancias(discrepancias, almacen.id, ts)
                    logger.info(f'[RECONCILIACION] Tareas devolución: {resumen_dev}')
            except Exception as e_dev:
                logger.warning(f'[RECONCILIACION] Error creando tareas devolución: {e_dev}')

            _estado_reconciliacion['ultimo_resultado'] = {
                'timestamp': ts,
                'total_productos_siesa': len(inventario_siesa),
                'total_discrepancias': len(discrepancias),
                'discrepancias': discrepancias[:100]
            }
            _estado_reconciliacion['ultimo_error'] = None

        except Exception as e:
            logger.error(f'[RECONCILIACION] Error: {e}')
            _estado_reconciliacion['ultimo_error'] = str(e)
            _estado_reconciliacion['ultimo_resultado'] = None

        _estado_reconciliacion['en_curso'] = False


def iniciar_reconciliacion(app):
    """Arranca la reconciliación en background. Retorna estado inmediatamente."""
    global _estado_reconciliacion

    if connekta.modo_simulacion:
        return {'simulado': True}

    if _estado_reconciliacion['en_curso']:
        return {'en_curso': True, 'mensaje': 'Reconciliación ya en proceso — espera que termine'}

    _estado_reconciliacion['en_curso'] = True
    _estado_reconciliacion['ultimo_inicio'] = datetime.now(timezone.utc)
    _estado_reconciliacion['ultimo_resultado'] = None
    _estado_reconciliacion['ultimo_error'] = None

    hilo = threading.Thread(target=_run_reconciliacion, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Reconciliación iniciada — refresca en ~2 min'}


def estado_reconciliacion():
    return {
        'en_curso': _estado_reconciliacion['en_curso'],
        'ultimo_inicio': _estado_reconciliacion['ultimo_inicio'].isoformat() if _estado_reconciliacion['ultimo_inicio'] else None,
        'ultimo_resultado': _estado_reconciliacion['ultimo_resultado'],
        'ultimo_error': _estado_reconciliacion['ultimo_error'],
    }
