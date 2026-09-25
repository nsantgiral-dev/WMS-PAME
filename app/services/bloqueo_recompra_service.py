"""
BloqueoRecompraService — Lista de bloqueo de recompra.

Reglas del consultor estratégico:
1. Bloqueo opera en nacimiento de OC, no en recepción
2. Recepción de SKU bloqueado → cuarentena + registro de fuga (no rechazo)
3. Velocity=0 con ventana de 12 meses móviles (captura estacionalidad)
4. Desbloqueadores enumerados (solo admin/gerente) + cantidad + vigencia
5. Vista en pesos: SKUs bloqueados × costo × existencia = capital inmovilizado
"""
import logging
from datetime import datetime, date, timedelta
from app.extensions import db
from app.models.producto_bloqueado import ProductoBloqueado, FugaRecompra
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)

# Personas autorizadas para desbloquear (roles)
ROLES_DESBLOQUEADORES = ('admin', 'gerente')


def capital_inmovilizado(refs_stock):
    """Σ costo × stock de [(referencia, stock)], con el costo de la jerarquía.

    Returns: (capital_conocido, skus_sin_costo). Un SKU sin costo de ninguna
    fuente NO suma cero en silencio: se cuenta aparte (Regla 0).
    """
    from app.services.costo_service import resolver_costos
    refs_stock = [(r, s) for r, s in refs_stock if r]
    if not refs_stock:
        return 0, 0
    costos = resolver_costos([r for r, _s in refs_stock])
    total, sin_costo = 0.0, 0
    for r, stock in refs_stock:
        c = float((costos.get(r) or {}).get('costo') or 0)
        if c <= 0:
            sin_costo += 1
            continue
        total += c * stock
    return total, sin_costo


class BloqueoRecompraService:

    @staticmethod
    def poblar_lista_inicial():
        """
        Genera la lista inicial de bloqueos: velocidad CERO en 12 meses móviles
        + stock > 0.

        LA VELOCIDAD SALE DEL KARDEX, no de `TareaPicking`. Antes «sin picks en
        12 meses» era el criterio, y el picking del WMS solo ve lo que sale por
        pedido: todo lo que se vende por POS en tienda —la mayoría del
        catálogo de mostrador— no tiene una sola `TareaPicking` y salía
        bloqueado en masa como cadáver. La velocidad es la demanda neta de
        `KardexService.demanda_descensurada` (red, todas las bodegas, ventas
        menos devoluciones): la MISMA función que usan el ROP y el contenedor.

        SIN KARDEX NO SE BLOQUEA (Regla 0: no saber no es «no se vende»). Si el
        kardex no cubre los 12 meses completos, o su último movimiento es más
        viejo que `KARDEX_DIAS_FRESCURA`, no se bloquea NADA y se dice por qué.
        Un SKU sin `codigo_siesa` no se puede buscar en el kardex: tampoco.

        Retorna: {bloqueados_nuevos, ya_bloqueados, total_capital_inmovilizado,
                  capital_sin_costo, ...}
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.services.kardex_service import (
            KardexService, KardexMovimiento, dias_frescura, ventana_demanda,
            ventana_observada)
        from sqlalchemy import func

        desde_12m, hasta = ventana_demanda(12)
        desde_obs, _h, cobertura = ventana_observada(12)
        ultimo = db.session.query(func.max(KardexMovimiento.fecha)).scalar()
        motivo_no = None
        if desde_obs is None:
            motivo_no = 'KARDEX_VACIO'
        elif desde_obs > desde_12m:
            motivo_no = 'KARDEX_NO_CUBRE_12_MESES'
        elif ultimo is None or (hasta - ultimo).days > dias_frescura():
            motivo_no = 'KARDEX_DESACTUALIZADO'
        if motivo_no:
            logger.warning('[BLOQUEO] No se bloquea nada: %s (cobertura desde %s, '
                           'último movimiento %s)', motivo_no, cobertura, ultimo)
            return {
                'bloqueados_nuevos': 0,
                'ya_bloqueados': db.session.query(ProductoBloqueado.producto_id)
                                   .filter(ProductoBloqueado.activo == True).count(),  # noqa: E712
                'total_capital_inmovilizado': 0,
                'no_se_bloqueo_por': motivo_no,
                'nota': (
                    'No se bloqueó ningún SKU: sin un kardex que cubra los 12 meses y '
                    'esté al día no se puede afirmar que algo NO se vende. Descargar '
                    'el kardex y volver a correr.'),
                'kardex': {'cobertura_desde': cobertura.isoformat() if cobertura else None,
                           'ultimo_movimiento': ultimo.isoformat() if ultimo else None},
            }

        # Productos con stock > 0 en cualquier ubicación
        productos_con_stock = (
            db.session.query(
                UbicacionProducto.producto_id,
                func.sum(UbicacionProducto.cantidad).label('stock_total')
            )
            .filter(UbicacionProducto.cantidad > 0)
            .group_by(UbicacionProducto.producto_id)
            .all()
        )
        stock_map = {r.producto_id: int(r.stock_total) for r in productos_con_stock}

        # Velocidad de 12 meses por referencia — la demanda neta de la red.
        demanda = KardexService.demanda_descensurada(12, 'red')
        con_venta = {ref for ref, d in demanda.items() if d['demanda_neta'] > 0}

        # Ya bloqueados (para no duplicar)
        ya_bloqueados_ids = set(
            r[0] for r in
            db.session.query(ProductoBloqueado.producto_id)
            .filter(ProductoBloqueado.activo == True)  # noqa: E712
            .all()
        )

        _candidatos = [pid for pid in stock_map if pid not in ya_bloqueados_ids]
        _productos = {p.id: p for p in
                      Producto.query.filter(Producto.id.in_(_candidatos)).all()} \
            if _candidatos else {}

        bloqueados = []
        sin_codigo = 0
        for producto_id, stock in stock_map.items():
            if producto_id in ya_bloqueados_ids:
                continue
            producto = _productos.get(producto_id)
            if not producto:
                continue
            ref = (producto.codigo_siesa or '').strip()
            if not ref:
                sin_codigo += 1       # no se puede buscar en el kardex: no se bloquea
                continue
            if ref in con_venta:
                continue              # se vendió en 12 meses — no bloquear

            db.session.add(ProductoBloqueado(
                producto_id=producto_id,
                motivo='VELOCITY_CERO_12M',
                bloqueado_por_sistema=True,
                notas_bloqueo=(
                    f'Sin venta neta en el kardex (todas las bodegas) desde '
                    f'{desde_12m.isoformat()}. Stock actual: {stock} UND.'
                ),
            ))
            bloqueados.append((ref, stock))

        capital, sin_costo = capital_inmovilizado(bloqueados)
        db.session.commit()

        logger.info(
            '[BLOQUEO] Población inicial: %d nuevos bloqueados, $%.0f capital '
            'inmovilizado (%d sin costo conocido)',
            len(bloqueados), capital, sin_costo
        )

        return {
            'bloqueados_nuevos': len(bloqueados),
            'ya_bloqueados': len(ya_bloqueados_ids),
            'total_capital_inmovilizado': capital,
            'capital_sin_costo': sin_costo,
            'capital_es_cota_inferior': sin_costo > 0,
            'sin_codigo_siesa_no_evaluados': sin_codigo,
            'velocidad_fuente': 'KardexService.demanda_descensurada (red, 12 meses)',
        }

    @staticmethod
    def verificar_oc(items_codigos: list) -> dict:
        """
        Verifica si algún item de una OC está bloqueado.
        Se llama ANTES de generar/transmitir la OC al proveedor.

        Args:
            items_codigos: lista de códigos Siesa de productos

        Returns:
            {permitido: bool, bloqueados: [{codigo, motivo, stock, fecha_bloqueo}]}
        """
        from app.models.producto import Producto

        bloqueados = []
        for codigo in items_codigos:
            producto = Producto.query.filter_by(codigo_siesa=codigo).first()
            if not producto:
                continue

            bloqueo = ProductoBloqueado.query.filter_by(
                producto_id=producto.id, activo=True
            ).first()

            if bloqueo and bloqueo.esta_bloqueado():
                bloqueados.append({
                    'codigo': codigo,
                    'nombre': producto.nombre,
                    'motivo': bloqueo.motivo,
                    'fecha_bloqueo': bloqueo.fecha_bloqueo.isoformat(),
                    'stock_existente': bloqueo.notas_bloqueo or '',
                })

        return {
            'permitido': len(bloqueados) == 0,
            'bloqueados': bloqueados,
        }

    @staticmethod
    def registrar_fuga(producto_id: int, recepcion_id: int = None,
                       oc_siesa: str = '', proveedor: str = '',
                       cantidad: int = 0):
        """
        Registra una fuga: mercancía recibida de un SKU bloqueado.
        NO rechaza la recepción — solo documenta la violación.
        """
        bloqueo = ProductoBloqueado.query.filter_by(
            producto_id=producto_id, activo=True
        ).first()

        if not bloqueo or not bloqueo.esta_bloqueado():
            return None  # no está bloqueado — no es fuga

        fuga = FugaRecompra(
            producto_id=producto_id,
            bloqueo_id=bloqueo.id,
            recepcion_id=recepcion_id,
            oc_siesa=oc_siesa,
            proveedor=proveedor,
            cantidad_recibida=cantidad,
            notas=f'Compra por fuera del sistema — SKU bloqueado por {bloqueo.motivo}',
        )
        db.session.add(fuga)
        db.session.commit()

        logger.warning(
            '[BLOQUEO] FUGA detectada: producto %d, OC %s, proveedor %s, '
            'cantidad %d — bloqueo #%d (%s)',
            producto_id, oc_siesa, proveedor, cantidad,
            bloqueo.id, bloqueo.motivo
        )

        return fuga

    @staticmethod
    def desbloquear(bloqueo_id: int, usuario_id: int, motivo: str,
                    cantidad_autorizada: int, vigencia_dias: int = 30):
        """
        Desbloquea un producto con restricciones.

        Args:
            bloqueo_id: ID del bloqueo a desbloquear
            usuario_id: quién autoriza (debe ser admin/gerente)
            motivo: por qué se desbloquea (obligatorio)
            cantidad_autorizada: máximo a comprar en esta autorización
            vigencia_dias: días de vigencia del desbloqueo (default 30)

        Returns: bloqueo actualizado
        Raises: ValueError si no autorizado o datos incompletos
        """
        from app.models.usuario import Usuario

        usuario = Usuario.query.get(usuario_id)
        if not usuario or usuario.rol not in ROLES_DESBLOQUEADORES:
            raise ValueError(
                f'Solo {", ".join(ROLES_DESBLOQUEADORES)} pueden desbloquear. '
                f'Usuario {usuario_id} tiene rol {usuario.rol if usuario else "N/A"}'
            )

        if not motivo or not motivo.strip():
            raise ValueError('Motivo de desbloqueo es obligatorio')

        if not cantidad_autorizada or cantidad_autorizada <= 0:
            raise ValueError('Cantidad autorizada debe ser > 0')

        bloqueo = ProductoBloqueado.query.get(bloqueo_id)
        if not bloqueo:
            raise LookupError(f'Bloqueo {bloqueo_id} no encontrado')

        bloqueo.desbloqueado_por_id = usuario_id
        bloqueo.motivo_desbloqueo = motivo.strip()
        bloqueo.cantidad_autorizada = cantidad_autorizada
        bloqueo.vigencia_desbloqueo = _dia_operativo() + timedelta(days=vigencia_dias)
        bloqueo.fecha_desbloqueo = datetime.utcnow()

        db.session.commit()

        logger.info(
            '[BLOQUEO] Desbloqueado #%d por usuario %d: %s — '
            'cantidad máx %d, vigencia %d días',
            bloqueo_id, usuario_id, motivo, cantidad_autorizada, vigencia_dias
        )

        return bloqueo

    @staticmethod
    def vista_capital_inmovilizado():
        """
        Vista en pesos para la sesión: SKUs bloqueados × costo × existencia.
        Ordenado por capital inmovilizado descendente (los 10 peores arriba).
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from sqlalchemy import func

        # `joinedload`: el loop de abajo lee `b.producto` por cada bloqueo. Con
        # lazy, son N SELECT extra — uno por SKU bloqueado, que es justo la
        # lista que crece cuando el problema empeora.
        from sqlalchemy.orm import joinedload

        bloqueos = (ProductoBloqueado.query
                    .options(joinedload(ProductoBloqueado.producto))
                    .filter_by(activo=True).all())
        producto_ids = [b.producto_id for b in bloqueos if b.esta_bloqueado()]

        if not producto_ids:
            return {'total_inmovilizado': 0, 'items': [], 'total_skus': 0}

        # Stock por producto
        stocks = (
            db.session.query(
                UbicacionProducto.producto_id,
                func.sum(UbicacionProducto.cantidad).label('stock')
            )
            .filter(UbicacionProducto.producto_id.in_(producto_ids))
            .filter(UbicacionProducto.cantidad > 0)
            .group_by(UbicacionProducto.producto_id)
            .all()
        )
        stock_map = {r.producto_id: int(r.stock) for r in stocks}

        from app.services.costo_service import resolver_costos
        _refs = [b.producto.codigo_siesa for b in bloqueos
                 if b.esta_bloqueado() and b.producto and b.producto.codigo_siesa]
        costos = resolver_costos(_refs) if _refs else {}

        items = []
        total = 0
        sin_costo = 0
        for b in bloqueos:
            if not b.esta_bloqueado():
                continue
            producto = b.producto
            if not producto:
                continue
            stock = stock_map.get(b.producto_id, 0)
            # El costo de la jerarquía (`resolver_costos`). Antes se leía
            # `Producto.costo_unitario`, que NO EXISTE: `hasattr` daba False y
            # el capital inmovilizado era 0 para todos, siempre (D11).
            c = costos.get(producto.codigo_siesa) or {}
            costo = float(c.get('costo') or 0) or None
            capital = costo * stock if costo else None
            if capital is None:
                sin_costo += 1

            items.append({
                'bloqueo_id': b.id,
                'codigo': producto.codigo_siesa,
                'nombre': producto.nombre,
                'motivo': b.motivo,
                'stock': stock,
                'costo_unitario': costo,
                'costo_fuente': c.get('fuente', 'SIN_COSTO'),
                'capital_inmovilizado': capital,
                'fecha_bloqueo': b.fecha_bloqueo.isoformat() if b.fecha_bloqueo else None,
            })
            total += capital or 0

        # Ordenar por capital descendente; los sin costo ADELANTE (Regla 0: el
        # que no se sabe cuánto vale es el que hay que mirar primero).
        items.sort(key=lambda x: (x['capital_inmovilizado'] is not None,
                                  -(x['capital_inmovilizado'] or 0)))

        return {
            'total_inmovilizado': total,
            'total_skus': len(items),
            'skus_sin_costo': sin_costo,
            'total_es_cota_inferior': sin_costo > 0,
            'items': items,
        }

    @staticmethod
    def listar_fugas():
        """Lista todas las fugas registradas (compras por fuera del sistema)."""
        from sqlalchemy.orm import joinedload

        fugas = (FugaRecompra.query
                 .options(joinedload(FugaRecompra.producto))
                 .order_by(FugaRecompra.fecha.desc()).limit(100).all())
        return [{
            'id': f.id,
            'producto_codigo': f.producto.codigo_siesa if f.producto else '',
            'producto_nombre': f.producto.nombre if f.producto else '',
            'oc_siesa': f.oc_siesa,
            'proveedor': f.proveedor,
            'cantidad_recibida': f.cantidad_recibida,
            'fecha': f.fecha.isoformat(),
            'notas': f.notas or '',
        } for f in fugas]
