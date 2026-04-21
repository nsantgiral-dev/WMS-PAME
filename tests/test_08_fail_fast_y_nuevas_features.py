"""
Test 08 — Fail-Fast sync, zona AVERIAS y campo puede_abastecer.

Cubre los cambios del consultor:
  - AVE-* → tipo_zona AVERIAS
  - Prefijo inválido → cuarentena en ubicaciones_huerfanas (nunca GENERAL)
  - Usuario.puede_abastecer boolean (no es rol nuevo)
  - Conector 173066 en lugar de 173076
"""
import pytest
import unittest.mock as mock


class TestInferirTipoZonaActualizado:
    """La función ahora reconoce AVE- además de PIK- y RES-."""

    def test_ave_es_averias(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('AVE-01-A') == 'AVERIAS'
        assert _inferir_tipo_zona('ave-02') == 'AVERIAS'
        assert _inferir_tipo_zona('AVE-DOCK') == 'AVERIAS'

    def test_pik_sigue_siendo_picking(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('PIK-01-A') == 'PICKING'

    def test_res_sigue_siendo_reserva(self):
        from app.services.ubicaciones_sync_service import _inferir_tipo_zona
        assert _inferir_tipo_zona('RES-01-A') == 'RESERVA'


class TestPrefijosValidosRegex:
    """El regex _PREFIJO_VALIDO solo aprueba PIK-|RES-|AVE-."""

    def test_validos(self):
        from app.services.ubicaciones_sync_service import _PREFIJO_VALIDO
        for codigo in ['PIK-01-A', 'RES-99-Z', 'AVE-DOCK', 'pik-01', 'res-02', 'ave-03']:
            assert _PREFIJO_VALIDO.match(codigo), f'{codigo} debería ser válido'

    def test_invalidos(self):
        from app.services.ubicaciones_sync_service import _PREFIJO_VALIDO
        for codigo in ['A-01-01', 'DOCK-1', 'GEN-01', 'B001', '', 'PICKING-1',
                       'RESERVA-01', 'PIK01', 'RES01', 'AVE01']:
            assert not _PREFIJO_VALIDO.match(codigo), f'{codigo} debería ser inválido'


class TestFailFastCuarentena:
    """Ubicaciones con prefijo inválido van a ubicaciones_huerfanas, nunca a ubicaciones."""

    def _run_sync_con_datos(self, db, almacen, filas):
        import unittest.mock as mock
        from app.services import ubicaciones_sync_service as svc

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

    def test_codigo_invalido_va_a_cuarentena(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion
        from app.models.ubicacion_huerfana import UbicacionHuerfana

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'A-01-01',  # sin prefijo válido
            'f155_descripcion': 'Pasillo genérico',
            'f155_ind_estado': 1,
        }])

        # No debe crear ubicación normal
        assert Ubicacion.query.filter_by(codigo='A-01-01').first() is None

        # Sí debe crear entrada en cuarentena
        huerfana = UbicacionHuerfana.query.filter_by(codigo_siesa='A-01-01').first()
        assert huerfana is not None
        assert huerfana.bodega_id == 'NB1'
        assert huerfana.veces_detectada == 1

    def test_codigo_valido_no_va_a_cuarentena(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion
        from app.models.ubicacion_huerfana import UbicacionHuerfana

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'PIK-99-Z',
            'f155_descripcion': 'Picking Zona Z',
            'f155_ind_estado': 1,
        }])

        assert Ubicacion.query.filter_by(codigo='PIK-99-Z').first() is not None
        assert UbicacionHuerfana.query.count() == 0

    def test_ave_crea_ubicacion_averias(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion

        self._run_sync_con_datos(db, almacen, [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'AVE-01-A',
            'f155_descripcion': 'Zona averías dock',
            'f155_ind_estado': 1,
        }])

        ub = Ubicacion.query.filter_by(codigo='AVE-01-A').first()
        assert ub is not None
        assert ub.tipo_zona == 'AVERIAS'

    def test_cuarentena_incrementa_contador_en_sync_repetido(self, app, db, almacen):
        from app.models.ubicacion_huerfana import UbicacionHuerfana

        fila = [{
            'f150_id': 'NB1', 'f155_id_cia': 1,
            'f155_id': 'DOCK-1',
            'f155_descripcion': 'Dock de carga',
            'f155_ind_estado': 1,
        }]

        self._run_sync_con_datos(db, almacen, fila)
        self._run_sync_con_datos(db, almacen, fila)

        huerfana = UbicacionHuerfana.query.filter_by(codigo_siesa='DOCK-1').first()
        assert huerfana is not None
        assert huerfana.veces_detectada == 2
        assert UbicacionHuerfana.query.count() == 1  # no duplicados

    def test_mix_validos_e_invalidos_en_mismo_sync(self, app, db, almacen):
        from app.models.ubicacion import Ubicacion
        from app.models.ubicacion_huerfana import UbicacionHuerfana

        self._run_sync_con_datos(db, almacen, [
            {'f150_id': 'NB1', 'f155_id_cia': 1, 'f155_id': 'PIK-01-B',
             'f155_descripcion': 'Picking B', 'f155_ind_estado': 1},
            {'f150_id': 'NB1', 'f155_id_cia': 1, 'f155_id': 'MUELLE-2',
             'f155_descripcion': 'Muelle de carga', 'f155_ind_estado': 1},
            {'f150_id': 'NB1', 'f155_id_cia': 1, 'f155_id': 'RES-02-B',
             'f155_descripcion': 'Reserva nivel B', 'f155_ind_estado': 1},
        ])

        # Dos válidas creadas, una en cuarentena
        assert Ubicacion.query.filter_by(codigo='PIK-01-B').first() is not None
        assert Ubicacion.query.filter_by(codigo='RES-02-B').first() is not None
        assert Ubicacion.query.filter_by(codigo='MUELLE-2').first() is None
        assert UbicacionHuerfana.query.count() == 1


class TestPuedeAbastecer:
    """Usuario.puede_abastecer — capacidad adicional sobre rol base."""

    def test_default_false(self, db, usuario):
        assert usuario.puede_abastecer is False or usuario.puede_abastecer is None

    def test_to_dict_incluye_puede_abastecer(self, db, usuario):
        d = usuario.to_dict()
        assert 'puede_abastecer' in d

    def test_activar_puede_abastecer(self, db, usuario):
        usuario.puede_abastecer = True
        db.session.commit()

        from app.models.usuario import Usuario
        u = Usuario.query.get(usuario.id)
        assert u.puede_abastecer is True
        assert u.rol == 'operario'  # no cambia el rol base

    def test_puede_abastecer_y_puede_picar_independientes(self, db, almacen):
        from app.models.usuario import Usuario
        from werkzeug.security import generate_password_hash

        # Operario que solo abastece (no pica)
        u = Usuario(
            nombre='Abastecedor Puro',
            email='abast@test.com',
            password_hash=generate_password_hash('x'),
            rol='operario',
            almacen_id=almacen.id,
            puede_picar=False,
            puede_abastecer=True,
        )
        db.session.add(u)
        db.session.commit()

        u2 = Usuario.query.get(u.id)
        assert u2.puede_picar is False
        assert u2.puede_abastecer is True


class TestUbicacionHuerfanaModelo:
    """Modelo UbicacionHuerfana — persistencia y to_dict."""

    def test_crear_huerfana(self, app, db):
        from app.models.ubicacion_huerfana import UbicacionHuerfana
        h = UbicacionHuerfana(
            codigo_siesa='MUELLE-99',
            bodega_id='NB1',
            descripcion='Muelle de carga',
            activo_siesa=True,
        )
        db.session.add(h)
        db.session.commit()

        assert h.id is not None
        assert h.veces_detectada == 1

    def test_to_dict_campos_completos(self, app, db):
        from app.models.ubicacion_huerfana import UbicacionHuerfana
        h = UbicacionHuerfana(
            codigo_siesa='B-01-X',
            bodega_id='NC1',
            descripcion='Sin zona',
            activo_siesa=False,
        )
        db.session.add(h)
        db.session.commit()

        d = h.to_dict()
        assert d['codigo_siesa'] == 'B-01-X'
        assert d['bodega_id'] == 'NC1'
        assert d['activo_siesa'] is False
        assert d['veces_detectada'] == 1
        assert 'fecha_detectada' in d

    def test_constraint_unico_codigo_bodega(self, app, db):
        import sqlalchemy.exc
        from app.models.ubicacion_huerfana import UbicacionHuerfana

        db.session.add(UbicacionHuerfana(codigo_siesa='DUP-01', bodega_id='NB1'))
        db.session.commit()
        db.session.add(UbicacionHuerfana(codigo_siesa='DUP-01', bodega_id='NB1'))
        with pytest.raises(Exception):  # IntegrityError en PostgreSQL, OperationalError en SQLite
            db.session.commit()
        db.session.rollback()


class TestConector173066:
    """Verifica que transferir_entre_ubicaciones usa 173066, no 173076."""

    def test_usa_conector_transferencia_directa(self):
        from app.services.connekta_gateway import connekta
        # El gateway debe apuntar a 173066 para transferencias entre ubicaciones
        assert connekta.conector_transferencia_directa in ('173066',)

    def test_payload_no_tiene_docto_alterno(self):
        """f450_docto_alterno es exclusivo de 173076 — NO debe aparecer en 173066."""
        from app.services.connekta_gateway import connekta

        calls = []

        def fake_post(conector, nombre, payload):
            calls.append({'conector': conector, 'payload': payload})
            return {'ok': True}

        with mock.patch.object(connekta, '_post', side_effect=fake_post):
            connekta.transferir_entre_ubicaciones(
                bodega_id='NB1',
                ubicacion_origen='RES-01-A',
                ubicacion_destino='PIK-01-B',
                referencia_item='PROD-001',
                cantidad=100,
                nota='Test reposición',
            )

        assert len(calls) == 1
        call = calls[0]

        # Conector correcto
        assert call['conector'] == connekta.conector_transferencia_directa

        # Documentos deben usar f350_* (no f470_* en header)
        doc = call['payload']['Documentos'][0]
        assert 'f350_id_co' in doc
        assert 'f450_id_bodega_salida' in doc
        assert 'f450_id_bodega_entrada' in doc
        # Misma bodega salida y entrada
        assert doc['f450_id_bodega_salida'] == 'NB1'
        assert doc['f450_id_bodega_entrada'] == 'NB1'
        # Sin f450_docto_alterno (exclusivo de 173076)
        assert 'f450_docto_alterno' not in doc

        # Movimientos con ubicaciones correctas
        mov = call['payload']['Movimientos'][0]
        assert mov['f470_id_ubicacion_aux'] == 'RES-01-A'
        assert mov['f470_id_ubicacion_aux_ent'] == 'PIK-01-B'
        assert mov['f470_cant_base'] == 100
