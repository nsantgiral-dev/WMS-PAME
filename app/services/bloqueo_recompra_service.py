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


class BloqueoRecompraService:

    @staticmethod
    def poblar_lista_inicial():
        """
        Genera la lista inicial de bloqueos basada en:
        1. Velocity=0 en 12 meses móviles + stock>0
        2. Productos genéricos sin conteo físico (si los hay marcados)

        Retorna: {bloqueados_nuevos, ya_bloqueados, total_capital_inmovilizado}
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.models.picking import TareaPicking
        from sqlalchemy import func

        # Fecha límite: 12 meses atrás
        fecha_limite = datetime.utcnow() - timedelta(days=365)

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

        # Productos con al menos 1 pick en los últimos 12 meses
        productos_con_picks = set(
            r[0] for r in
            db.session.query(TareaPicking.producto_id)
            .filter(TareaPicking.fecha_creacion >= fecha_limite)
            .filter(TareaPicking.producto_id.isnot(None))
            .distinct()
            .all()
        )

        # Ya bloqueados (para no duplicar)
        ya_bloqueados_ids = set(
            r[0] for r in
            db.session.query(ProductoBloqueado.producto_id)
            .filter(ProductoBloqueado.activo == True)
            .all()
        )

        bloqueados_nuevos = 0
        capital_inmovilizado = 0

        # Los productos que el loop va a necesitar, en UNA consulta. Antes era
        # `Producto.query.get(id)` por iteración: con 2000 SKUs y 25% sin
        # movimiento, ~500 consultas individuales.
        _candidatos = [pid for pid in stock_map
                       if pid not in ya_bloqueados_ids
                       and pid not in productos_con_picks]
        _productos = {p.id: p for p in
                      Producto.query.filter(Producto.id.in_(_candidatos)).all()} \
            if _candidatos else {}

        for producto_id, stock in stock_map.items():
            if producto_id in ya_bloqueados_ids:
                continue
            if producto_id in productos_con_picks:
                continue  # tiene demanda en 12 meses — no bloquear

            # Velocity=0 + stock>0 → cadáver
            producto = _productos.get(producto_id)
            if not producto:
                continue

            bloqueo = ProductoBloqueado(
                producto_id=producto_id,
                motivo='VELOCITY_CERO_12M',
                bloqueado_por_sistema=True,
                notas_bloqueo=(
                    f'Sin picks en 12 meses (desde {fecha_limite.strftime("%Y-%m-%d")}). '
                    f'Stock actual: {stock} UND.'
                ),
            )
            db.session.add(bloqueo)
            bloqueados_nuevos += 1

            # Capital inmovilizado (costo × stock)
            costo = float(producto.costo_unitario or 0) if hasattr(producto, 'costo_unitario') else 0
            capital_inmovilizado += costo * stock

        db.session.commit()

        logger.info(
            '[BLOQUEO] Población inicial: %d nuevos bloqueados, $%.0f capital inmovilizado',
            bloqueados_nuevos, capital_inmovilizado
        )

        return {
            'bloqueados_nuevos': bloqueados_nuevos,
            'ya_bloqueados': len(ya_bloqueados_ids),
            'total_capital_inmovilizado': capital_inmovilizado,
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

        items = []
        total = 0
        for b in bloqueos:
            if not b.esta_bloqueado():
                continue
            producto = b.producto
            if not producto:
                continue
            stock = stock_map.get(b.producto_id, 0)
            costo = float(producto.costo_unitario or 0) if hasattr(producto, 'costo_unitario') else 0
            capital = costo * stock

            items.append({
                'bloqueo_id': b.id,
                'codigo': producto.codigo_siesa,
                'nombre': producto.nombre,
                'motivo': b.motivo,
                'stock': stock,
                'costo_unitario': costo,
                'capital_inmovilizado': capital,
                'fecha_bloqueo': b.fecha_bloqueo.isoformat() if b.fecha_bloqueo else None,
            })
            total += capital

        # Ordenar por capital descendente
        items.sort(key=lambda x: x['capital_inmovilizado'], reverse=True)

        return {
            'total_inmovilizado': total,
            'total_skus': len(items),
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
