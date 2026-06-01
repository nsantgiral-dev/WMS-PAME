"""
MuelleService — lógica de negocio del monitor de muelle.
Flujo: siesa_triggered → bultos PENDIENTE aparecen en muelle → scan-to-truck → CARGADO.
"""
import logging
from datetime import datetime, date
from sqlalchemy.orm import selectinload
from sqlalchemy import func
from app.extensions import db
from app.models.bulto import Bulto, EstadoBulto
from app.models.packing import TareaPacking, EstadoPacking
from app.models.ruta_despacho import RutaDespacho, EstadoRutaDespacho

logger = logging.getLogger(__name__)


class ConflictError(ValueError):
    """Señala un conflicto de estado (HTTP 409) — bulto ya asignado a otra ruta."""


class MuelleService:

    @staticmethod
    def listar_bultos_listos() -> dict:
        bultos = (
            Bulto.query
            .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
            .filter(
                TareaPacking.siesa_triggered == True,
                TareaPacking.estado != EstadoPacking.CANCELADO,
                Bulto.estado == EstadoBulto.PENDIENTE,
                Bulto.ruta_despacho_id.is_(None),
            )
            .options(selectinload(Bulto.tarea))
            .order_by(TareaPacking.fecha_despachado.asc(), Bulto.numero.asc())
            .all()
        )
        grupos: dict = {}
        for b in bultos:
            destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
            grupos.setdefault(destino, []).append(b.to_dict())

        return {
            'grupos': [
                {'destino': destino, 'bultos': lista, 'total': len(lista)}
                for destino, lista in sorted(grupos.items())
            ],
            'total_bultos': len(bultos),
        }

    @staticmethod
    def asignar_a_ruta(ruta_id: int, bultos_ids: list = None, pedido_siesa: str = None) -> dict:
        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_CARGUE:
            raise ValueError(f'La ruta ya está {ruta.estado}')

        query = Bulto.query.filter(Bulto.estado == EstadoBulto.PENDIENTE)
        if pedido_siesa:
            query = query.join(TareaPacking).filter(TareaPacking.numero_pedido_siesa == pedido_siesa)
        elif bultos_ids:
            query = query.filter(Bulto.id.in_(bultos_ids))
        else:
            raise ValueError('Debe enviar bultos_ids o pedido_siesa')

        bultos = query.with_for_update().all()
        if not bultos:
            raise LookupError('No se encontraron bultos PENDIENTE con los criterios indicados')

        ya_asignados = [b.id for b in bultos if b.ruta_despacho_id and b.ruta_despacho_id != ruta_id]
        if ya_asignados:
            otra_ruta = next(b.ruta_despacho_id for b in bultos if b.ruta_despacho_id and b.ruta_despacho_id != ruta_id)
            raise ConflictError(
                f'Los bultos {ya_asignados} ya están asignados a la ruta #{otra_ruta}. Desasígnalos primero.'
            )

        for b in bultos:
            b.ruta_despacho_id = ruta_id
        db.session.commit()
        return {'ok': True, 'mensaje': f'{len(bultos)} bultos asignados a la ruta {ruta_id}'}

    @staticmethod
    def desasignar_de_ruta(bulto_id: int) -> dict:
        bulto = Bulto.query.get(bulto_id)
        if not bulto:
            raise LookupError('Bulto no encontrado')
        if bulto.estado == EstadoBulto.CARGADO:
            raise ValueError('No se puede desasignar un bulto que ya fue cargado físicamente')
        bulto.ruta_despacho_id = None
        db.session.commit()
        return {'ok': True, 'mensaje': 'Bulto desasignado de la ruta'}

    @staticmethod
    def cargar_bulto(codigo_barras: str, ruta_id: int) -> dict:
        # Algunos escáneres con layout español envían apóstrofe en lugar de guion
        codigo_normalizado = codigo_barras.upper().replace("'", "-")
        bulto = (
            Bulto.query
            .options(selectinload(Bulto.tarea))
            .filter_by(codigo_barras=codigo_normalizado)
            .with_for_update()
            .first()
        )
        if not bulto:
            raise LookupError(f'Bulto {codigo_normalizado} no encontrado')
        if not bulto.tarea.siesa_triggered:
            raise ValueError('Este pedido aún no fue procesado por Siesa')
        if bulto.ruta_despacho_id != ruta_id:
            if bulto.ruta_despacho_id:
                raise ValueError(f'Bulto planificado para ruta #{bulto.ruta_despacho_id}, no para #{ruta_id}')
            else:
                raise ValueError('Bulto no ha sido asignado a ninguna ruta. Asígnalo manualmente primero.')

        if bulto.estado == EstadoBulto.CARGADO:
            return {
                'mensaje': 'Bulto ya fue verificado y cargado',
                'codigo_barras': codigo_barras,
                'ya_cargado': True,
                'ruta_despacho_id': bulto.ruta_despacho_id,
            }

        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_CARGUE:
            raise ValueError(f'La ruta #{ruta_id} ya está {ruta.estado}')

        _tarea_id      = bulto.tarea_id
        _bulto_id      = bulto.id
        _bulto_tipo    = bulto.tipo
        _bulto_numero  = bulto.numero
        _bulto_total   = bulto.total
        _bulto_ruta_id = bulto.ruta_despacho_id

        bulto.estado = EstadoBulto.CARGADO
        bulto.fecha_cargado = datetime.utcnow()
        db.session.commit()

        pendientes_ruta = Bulto.query.filter_by(
            tarea_id=_tarea_id,
            ruta_despacho_id=ruta_id,
            estado=EstadoBulto.PENDIENTE,
        ).count()
        tarea = TareaPacking.query.get(_tarea_id)

        return {
            'ok': True,
            'id': _bulto_id,
            'codigo_barras': codigo_barras,
            'tipo': _bulto_tipo,
            'numero': _bulto_numero,
            'total': _bulto_total,
            'numero_pedido_siesa': tarea.numero_pedido_siesa if tarea else None,
            'cliente': tarea.cliente or '' if tarea else '',
            'municipio': tarea.municipio or '' if tarea else '',
            'ruta_despacho_id': _bulto_ruta_id,
            'pedido_completo_en_ruta': pendientes_ruta == 0,
            'bultos_pendientes_pedido_ruta': pendientes_ruta,
        }

    @staticmethod
    def obtener_manifiesto() -> dict:
        hoy = date.today()
        bultos = (
            Bulto.query
            .options(selectinload(Bulto.tarea))
            .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
            .filter(
                Bulto.estado == EstadoBulto.CARGADO,
                func.date(Bulto.fecha_cargado) == hoy,
            )
            .order_by(TareaPacking.municipio, TareaPacking.cliente, Bulto.numero)
            .all()
        )
        grupos: dict = {}
        for b in bultos:
            destino = b.tarea.municipio or b.tarea.cliente or 'Sin destino'
            if destino not in grupos:
                grupos[destino] = {}
            pedido = b.tarea.numero_pedido_siesa
            if pedido not in grupos[destino]:
                grupos[destino][pedido] = {
                    'numero_pedido': pedido,
                    'cliente': b.tarea.cliente or '',
                    'bultos': [],
                }
            grupos[destino][pedido]['bultos'].append({
                'codigo_barras': b.codigo_barras,
                'tipo': b.tipo,
                'numero': b.numero,
                'total': b.total,
            })

        resultado = []
        for destino, pedidos in sorted(grupos.items()):
            parada = {'destino': destino, 'pedidos': []}
            for p in pedidos.values():
                resumen: dict = {}
                for bul in p['bultos']:
                    resumen[bul['tipo']] = resumen.get(bul['tipo'], 0) + 1
                p['resumen'] = resumen
                p['total_bultos'] = len(p['bultos'])
                parada['pedidos'].append(p)
            parada['total_bultos'] = sum(p['total_bultos'] for p in parada['pedidos'])
            resultado.append(parada)

        return {
            'manifiesto': resultado,
            'fecha': hoy.isoformat(),
            'total_bultos': len(bultos),
        }

    @staticmethod
    def obtener_historial() -> list:
        bultos = (
            Bulto.query
            .options(selectinload(Bulto.tarea))
            .filter_by(estado=EstadoBulto.CARGADO)
            .order_by(Bulto.fecha_cargado.desc())
            .limit(100)
            .all()
        )
        return [b.to_dict() for b in bultos]
