"""
ENCONTRADO_COMPLETO / ENCONTRADO_PARCIAL en la auditoría de picking bloqueado
(`PickingService.auditar_tarea`) ahora ajustan a Siesa con la MISMA política
que un conteo cíclico (`ConteoService.ajustar_desde_auditoria_picking`),
enfocada en el SKU puntual — decisión de negocio 2026-09-18: antes esos dos
resultados solo tocaban el WMS local y Siesa nunca se enteraba.

Regla 0: el ajuste nunca sale a ciegas contra el WMS — si Siesa no responde
la existencia, la auditoría entera falla (ValueError) sin persistir nada.
"""
import pytest
from unittest.mock import patch

from app.models.picking import TareaPicking, EstadoPicking
from app.models.inventario import UbicacionProducto
from app.models.ubicacion import Ubicacion
from app.models.conteo import SesionConteo, EstadoConteo
from app.models.siesa_job import SiesaJob
from app.services.conteo_service import ConteoService
from app.services.picking_service import PickingService


def _tarea_bloqueada(db, almacen, producto, *, cantidad_solicitada, cantidad_recogida,
                      cantidad_wms, bloqueado):
    ub = Ubicacion(codigo=f'UB-AUD-{cantidad_wms}-{bloqueado}', almacen_id=almacen.id,
                    zona='A', tipo='estanteria', activo=True)
    db.session.add(ub)
    db.session.flush()

    inv = UbicacionProducto(
        ubicacion_id=ub.id, producto_id=producto.id,
        cantidad=cantidad_wms, reservado=0, bloqueado=bloqueado,
    )
    db.session.add(inv)

    tarea = TareaPicking(
        codigo=f'PICK-AUD-{ub.id}', producto_id=producto.id,
        cantidad_solicitada=cantidad_solicitada, cantidad_recogida=cantidad_recogida,
        ubicacion_id=ub.id, almacen_id=almacen.id,
        estado=EstadoPicking.BLOQUEADO, motivo_bloqueo='FALTANTE',
    )
    db.session.add(tarea)
    db.session.commit()
    return tarea, inv


class TestEncontradoCompletoAjustaComoConteo:

    def test_cuadra_con_siesa_no_encola_nada(self, app, db, almacen, producto, usuario_admin):
        """Físico == Siesa → MATCH, sin AJUSTE_CONTEO — igual que un conteo
        cíclico que cuadra no manda documento a Siesa."""
        tarea, inv = _tarea_bloqueada(
            db, almacen, producto,
            cantidad_solicitada=5, cantidad_recogida=0,
            cantidad_wms=20, bloqueado=5,
        )

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=20.0):
            PickingService.auditar_tarea(
                tarea.id, admin_id=usuario_admin.id, resultado='ENCONTRADO_COMPLETO',
            )

        sesion = SesionConteo.query.filter_by(tarea_picking_id=tarea.id).one()
        assert sesion.tipo == 'EXCEPCION_PICKING'
        assert sesion.estado == EstadoConteo.MATCH
        assert sesion.diferencia == 0
        assert sesion.cantidad_fisica == 20
        assert sesion.existencia_siesa == 20
        assert not SiesaJob.query.filter_by(referencia_tipo='SesionConteo', referencia_id=sesion.id).count()

    def test_discrepancia_con_siesa_encola_ajuste_focalizado(
        self, app, db, almacen, producto, usuario_admin,
    ):
        """Físico != Siesa → DESCUADRE + AJUSTE_CONTEO con el connector 142951,
        el mismo job que usa el conteo cíclico normal, enfocado en este SKU."""
        almacen.centro_op_siesa = '003'
        db.session.commit()

        tarea, inv = _tarea_bloqueada(
            db, almacen, producto,
            cantidad_solicitada=5, cantidad_recogida=0,
            cantidad_wms=20, bloqueado=5,
        )

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=15.0):
            PickingService.auditar_tarea(
                tarea.id, admin_id=usuario_admin.id, resultado='ENCONTRADO_COMPLETO',
            )

        sesion = SesionConteo.query.filter_by(tarea_picking_id=tarea.id).one()
        # DESCUADRE es transitorio — `_encolar_ajuste_fisico` la deja en
        # AJUSTANDO en cuanto el job queda en la cola (misma transición que
        # usa el conteo cíclico normal).
        assert sesion.estado == EstadoConteo.AJUSTANDO
        assert sesion.diferencia == 5          # WMS(20) - Siesa(15)
        assert sesion.motivo_codigo == 'AJ-ENT'
        assert sesion.aprobador_id == usuario_admin.id

        job = SiesaJob.query.filter_by(
            referencia_tipo='SesionConteo', referencia_id=sesion.id, tipo='AJUSTE_CONTEO',
        ).one()
        payload = job.get_payload()
        assert payload['item_codigo'] == producto.codigo_siesa
        assert payload['cantidad'] == 5
        assert payload['motivo_codigo'] == 'AJ-ENT'
        assert payload['tarea_picking_id'] == tarea.id

    def test_siesa_no_responde_no_persiste_nada(
        self, app, db, almacen, producto, usuario_admin,
    ):
        """Regla 0 — sin existencia real de Siesa, la auditoría completa falla
        y la tarea sigue BLOQUEADA: no se ajusta a ciegas contra el WMS."""
        tarea, inv = _tarea_bloqueada(
            db, almacen, producto,
            cantidad_solicitada=5, cantidad_recogida=0,
            cantidad_wms=20, bloqueado=5,
        )

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=None):
            with pytest.raises(ValueError, match='no respondió'):
                PickingService.auditar_tarea(
                    tarea.id, admin_id=usuario_admin.id, resultado='ENCONTRADO_COMPLETO',
                )

        db.session.rollback()
        db.session.refresh(tarea)
        db.session.refresh(inv)
        assert tarea.estado == EstadoPicking.BLOQUEADO
        assert inv.bloqueado == 5
        assert SesionConteo.query.filter_by(tarea_picking_id=tarea.id).count() == 0


class TestEncontradoParcialUsaLaCantidadYaCorregida:

    def test_compara_contra_siesa_lo_que_queda_tras_el_ajuste_local(
        self, app, db, almacen, producto, usuario_admin,
    ):
        """El ajuste a Siesa debe basarse en `reg.cantidad` YA corregido por el
        hallazgo parcial, no en el valor previo a la auditoría."""
        almacen.centro_op_siesa = '003'
        db.session.commit()

        # solicitadas=10, recogidas=3 → faltante=7; halladas=2 → diferencia local=5
        tarea, inv = _tarea_bloqueada(
            db, almacen, producto,
            cantidad_solicitada=10, cantidad_recogida=3,
            cantidad_wms=50, bloqueado=7,
        )

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=50.0):
            PickingService.auditar_tarea(
                tarea.id, admin_id=usuario_admin.id, resultado='ENCONTRADO_PARCIAL',
                cantidad_hallada=2,
            )

        db.session.refresh(inv)
        assert inv.cantidad == 45   # 50 - (7 - 2), el mismo ajuste local de siempre

        sesion = SesionConteo.query.filter_by(tarea_picking_id=tarea.id).one()
        assert sesion.cantidad_fisica == 45   # no 50 — la auditoría reconcilia lo YA corregido
        assert sesion.existencia_siesa == 50
        assert sesion.diferencia == -5
        assert sesion.motivo_codigo == 'AJ-SAL'


class TestOtrosResultadosNoDisparanElAjusteFocalizado:
    """NO_ENCONTRADO, AVERIA y DISCREPANCIA_SIESA ya tenían su propio camino
    (local o manual) — no deben crear una SesionConteo nueva de paso."""

    @pytest.mark.parametrize('resultado, kwargs', [
        ('NO_ENCONTRADO', {}),
        ('AVERIA', {'cantidad_hallada': 0}),
        ('DISCREPANCIA_SIESA', {}),
    ])
    def test_no_crea_sesion_conteo(
        self, app, db, almacen, producto, usuario_admin, resultado, kwargs,
    ):
        tarea, inv = _tarea_bloqueada(
            db, almacen, producto,
            cantidad_solicitada=5, cantidad_recogida=0,
            cantidad_wms=20, bloqueado=5,
        )

        with patch.object(ConteoService, 'consultar_existencia_siesa', return_value=999.0):
            PickingService.auditar_tarea(
                tarea.id, admin_id=usuario_admin.id, resultado=resultado, **kwargs,
            )

        assert SesionConteo.query.filter_by(tarea_picking_id=tarea.id).count() == 0
