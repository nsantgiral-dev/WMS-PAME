"""Re-validación de llevar_wms_a_lo_contado (71ff1ee5): el mismo defecto de
«una sola pata» en el traspaso de Layout desde SIESA-GENERAL."""
import pytest

from tests.flujo.test_e2e_conteo_escenarios import _politica_por_defecto, m, siesa  # noqa: F401
from tests.flujo.test_zz_val_mios2 import _faltante_encolado


class TestLayoutDespuesDelConteo:

    @pytest.mark.xfail(strict=True, reason=(
        'VAL-E4: layout_service.asignar_producto suma al hueco (ASIGNACION_LAYOUT, con '
        'saldo) y resta de SIESA-GENERAL en _traspasar_desde_general SIN movimiento: '
        'el total no cambia, pero movido_despues_del_conteo ve +20 y el WMS termina con '
        '20 de más (65 en vez de 45).'))
    def test_asignar_a_picking_entre_el_conteo_y_el_job(self, m):
        from app.services import layout_service
        pik = m.ub(m.nb1, 'PIK-9', 'PICKING')
        p, sid, _ = _faltante_encolado(m, {'SIESA-GENERAL': 50}, 45)
        layout_service.asignar_producto(pik.id, p.id, 20, usuario_id=m.sofi.id, capacidad_maxima=100)
        assert m.wms(p) == 50, m.por_lugar(p)          # traspaso: el total no cambió
        m.ejecutar_ajustes()
        assert m.wms(p) == 45, m.por_lugar(p)
