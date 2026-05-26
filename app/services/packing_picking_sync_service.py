"""
Sincroniza cantidad_esperada de un packing con lo que el picker realmente recogió.

El packing se crea con cantidades de Siesa (antes de picking). Si el picker
reporta faltantes, este servicio ajusta cantidad_esperada a cantidad_recogida
real antes de que el empacador empiece a contar.
"""
from app.extensions import db
from app.models.packing import TareaPacking, ItemPacking
from app.models.picking import TareaPicking


class PackingPickingSyncService:

    @staticmethod
    def sincronizar(packing_id: int) -> dict:
        """
        Ajusta cantidad_esperada de cada ItemPacking al total recogido en picking.
        Solo actúa si el picking del pedido tiene tareas terminadas (COMPLETADO o
        BLOQUEADO con cantidad_recogida > 0). Si no hay picking asociado, no cambia nada.

        Retorna un dict con el resumen de cambios aplicados.
        """
        tarea = TareaPacking.query.get(packing_id)
        if not tarea:
            raise ValueError('Tarea de packing no encontrada')

        if tarea.estado not in ('PENDIENTE', 'EN_PROCESO'):
            raise ValueError(
                f'Solo se puede sincronizar en estado PENDIENTE o EN_PROCESO, '
                f'actual: {tarea.estado}'
            )

        # Sumar cantidad_recogida por producto para el pedido
        pickings = TareaPicking.query.filter(
            TareaPicking.referencia_documento == tarea.numero_pedido_siesa,
            db.or_(
                TareaPicking.estado == 'COMPLETADO',
                db.and_(
                    TareaPicking.estado == 'BLOQUEADO',
                    TareaPicking.cantidad_recogida > 0
                )
            )
        ).all()

        if not pickings:
            return {'sincronizado': False, 'razon': 'Sin tareas de picking completadas para este pedido'}

        recogido_por_producto = {}
        for tp in pickings:
            recogido_por_producto[tp.producto_id] = (
                recogido_por_producto.get(tp.producto_id, 0) + (tp.cantidad_recogida or 0)
            )

        cambios = []
        for item in tarea.items:
            cantidad_picking = recogido_por_producto.get(item.producto_id)
            if cantidad_picking is not None:
                # Normalizar a UND: si picking reportó en unidades de empaque (cant < factor),
                # convertir antes de actualizar cantidad_esperada del packing.
                from app.models.producto import Producto as _ProdSync
                _prod_s = _ProdSync.query.get(item.producto_id)
                _fc_s = (_prod_s.factor_conversion or 1) if _prod_s else 1
                if _fc_s > 1 and 0 < cantidad_picking < _fc_s:
                    cantidad_picking = cantidad_picking * _fc_s

                if item.cantidad_esperada != cantidad_picking:
                    cambios.append({
                        'producto_id': item.producto_id,
                        'anterior': item.cantidad_esperada,
                        'nuevo': cantidad_picking
                    })
                    item.cantidad_esperada = cantidad_picking
                    # Resetear conteo si cambia la cantidad esperada para evitar
                    # mezcla de unidades (ej. cantReal en PQ vs cantEsp en UND).
                    item.cantidad_real = 0
                    item.verificado = False

        db.session.commit()

        return {
            'sincronizado': True,
            'packing_id': packing_id,
            'cambios': cambios,
            'items_ajustados': len(cambios)
        }
