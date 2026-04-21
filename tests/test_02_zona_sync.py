"""
Test 02 — Sync de Ubicaciones desde Siesa.
Valida que los campos certificados del PDF se mapean correctamente
y que stock_minimo/stock_maximo NO se sobreescriben en el sync.
"""
import pytest


class TestInferirTipoZona:
    """La lógica de prefijos es el corazón de la arquitectura de zonas."""

    def test_pik_es_picking(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('PIK-01-A') == 'PICKING'
        assert _inferir_tipo_zona('PIK-99') == 'PICKING'
        assert _inferir_tipo_zona('pik-01') == 'PICKING'  # case insensitive

    def test_res_es_reserva(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('RES-01-A') == 'RESERVA'
        assert _inferir_tipo_zona('res-02') == 'RESERVA'

    def test_otros_son_general(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('A-01-01') == 'GENERAL'
        assert _inferir_tipo_zona('DOCK-1') == 'GENERAL'
        assert _inferir_tipo_zona('') == 'GENERAL'
        assert _inferir_tipo_zona('B001') == 'GENERAL'


class TestSyncUbicaciones:
    """Simula el JSON que devuelve API_v2_Ubicaciones (campos certificados del PDF)."""

    def _run_sync_con_datos(self, db, almacen, filas):
        """Helper: ejecuta _run_sync con datos mockeados."""
        import unittest.mock as mock
        from app.services import ubicaciones_sync_service as svc

        # Mockear connekta para devolver las filas en pag 1 y vacío en pag 2
        def fake_get_ubicaciones(bodega_id=None, pagina=1):
            if pagina == 1:
                return {'detalle': {'Table': filas}}
            return {'detalle': {'Table': []}}

        almacen.bodega_siesa_id = 'NB1'
        db.session.commit()

        with mock.patch.object(
            svc.connekta, 'get_ubicaciones_siesa', side_effect=fake_get_ubicaciones
        ):
            from flask import current_app
            svc._run_sync(current_app._get_current_object(), bodega_id='NB1')

    def test_crea_ubicacion_con_campos_correctos(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1',
            'f155_id_cia': 1,
            'f155_id': 'PIK-03-A',
            'f155_descripcion': 'Pasillo 3 Nivel A',
            'f155_ind_estado': 1,
        }])

        ub = Ubicacion.query.filter_by(codigo='PIK-03-A').first()
        assert ub is not None
        assert ub.tipo_zona == 'PICKING'
        assert ub.activo is True
        assert ub.zona == 'Pasillo 3 Nivel A'

    def test_sincroniza_estado_inactivo(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'RES-INACTIVA', 'f155_descripcion': '',
            'f155_ind_estado': 0,
        }])

        ub = Ubicacion.query.filter_by(codigo='RES-INACTIVA').first()
        assert ub is not None
        assert ub.activo is False

    def test_NO_sobreescribe_stock_minimo_en_update(self, app, db, almacen, ub_picking):
        """stock_minimo configurado en WMS debe sobrevivir al sync."""
        ub_picking.stock_minimo = 99
        ub_picking.codigo = 'PIK-01-A'  # ya existe
        db.session.commit()

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'PIK-01-A',
            'f155_descripcion': 'Zona Picking Principal',
            'f155_ind_estado': 1,
        }])

        from app.models.ubicacion import Ubicacion
        ub = Ubicacion.query.filter_by(codigo='PIK-01-A').first()
        assert ub.stock_minimo == 99, 'El sync NO debe tocar stock_minimo'

    def test_fila_sin_codigo_se_omite(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion
        count_antes = Ubicacion.query.count()

        self._run_sync_con_datos(db, almacen, [
            {'f150_id': 'NB1', 'f155_id': '', 'f155_descripcion': 'Sin código', 'f155_ind_estado': 1},
        ])

        assert Ubicacion.query.count() == count_antes

    def test_crea_reserva_correctamente(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'RES-05-C',
            'f155_descripcion': 'Estantería Alta C',
            'f155_ind_estado': 1,
        }])

        ub = Ubicacion.query.filter_by(codigo='RES-05-C').first()
        assert ub.tipo_zona == 'RESERVA'
