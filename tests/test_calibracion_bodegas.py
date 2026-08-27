"""
Fase 1 de calibración de tiendas (2026-08-27): NS1 y NC1 pasan a tener stock
físico (`UbicacionProducto`) sincronizado desde Siesa, igual que NB1 — antes
solo NB1 lo tenía porque `_run_carga_inicial` estaba cableada a una sola
bodega (`_get_almacen()` resolvía `connekta.bodega` sin parámetro).

Generalizar esa función a "cualquier bodega" tenía una trampa real: varios
de sus estados compartidos eran GLOBALES a propósito cuando solo existía un
almacén (`_estado_carga`, `_cache_inventario_siesa`, la clave de idempotencia
`SIESA-INI-{prod.id}-{fecha}`, los guards de picking/packing activos). Sin
aislarlos por bodega, cargar NS1 después de NB1 el mismo día:
  - se habría visto bloqueada por picking activo en NB1 sin relación alguna,
  - habría saltado productos ya marcados "cargados hoy" por NB1 (mismo
    producto, mismo día, clave de idempotencia sin la bodega),
  - habría pisado el resultado/estado que el admin ve para NB1.

Estos tests cubren el aislamiento por bodega, no el pipeline de Siesa
completo (eso ya lo cubre — indirectamente — la operación diaria de NB1;
mockearlo de punta a punta para NS1/NC1 solo probaría los mocks).
"""
from app.models.almacen import Almacen
from app.models.picking import TareaPicking
from app.models.packing import TareaPacking
from app.services import inventario_siesa_service as inv_service


class TestGetAlmacenPorBodegaNoAdivinaAlOtroLado:

    def test_resuelve_el_almacen_correcto_por_bodega_siesa_id(self, db):
        db.session.add(Almacen(codigo='NB1', nombre='Bodega Neiva', bodega_siesa_id='NB1', activo=True))
        db.session.add(Almacen(codigo='NS1', nombre='Neiva Sur Principal', bodega_siesa_id='NS1', activo=True))
        db.session.commit()

        a = inv_service._get_almacen('NS1')
        assert a.bodega_siesa_id == 'NS1'
        assert a.nombre == 'Neiva Sur Principal'

    def test_bodega_explicita_sin_almacen_no_cae_a_cualquiera(self, db):
        # Solo existe NB1 — pedir NC1 explícitamente NO debe devolver NB1 por
        # fallback. Ese fallback ("cualquier almacén activo") solo tiene
        # sentido para el caso legado de un único almacén en todo el WMS;
        # con una bodega explícita, devolver el almacén equivocado escribiría
        # el stock de una tienda encima de otra.
        db.session.add(Almacen(codigo='NB1', nombre='Bodega Neiva', bodega_siesa_id='NB1', activo=True))
        db.session.commit()

        assert inv_service._get_almacen('NC1') is None

    def test_sin_bodega_preserva_el_fallback_legado(self, db, monkeypatch):
        from app.services.connekta_gateway import connekta
        monkeypatch.setattr(connekta, 'bodega', 'NO_EXISTE')
        db.session.add(Almacen(codigo='NB1', nombre='Bodega Neiva', bodega_siesa_id='NB1', activo=True))
        db.session.commit()

        # Sin argumento (comportamiento histórico): si connekta.bodega no
        # matchea ningún almacén, cae al único activo que exista.
        a = inv_service._get_almacen()
        assert a.bodega_siesa_id == 'NB1'


class TestEstadoCargaAisladoPorBodega:

    def test_mutar_una_bodega_no_toca_otra(self):
        inv_service._estado_carga.clear()
        e_ns1 = inv_service._estado_carga_bodega('NS1')
        e_ns1['en_curso'] = True
        e_ns1['ultimo_error'] = 'boom'

        e_nc1 = inv_service._estado_carga_bodega('NC1')

        assert e_nc1['en_curso'] is False
        assert e_nc1['ultimo_error'] is None

    def test_estado_carga_inventario_expone_bodega_correcta(self):
        inv_service._estado_carga.clear()
        inv_service._estado_carga_bodega('NS1')['ultimo_resultado'] = {'cargados': 7}

        assert inv_service.estado_carga_inventario('NS1') == {
            'bodega': 'NS1', 'en_curso': False,
            'ultimo_inicio': None, 'ultimo_resultado': {'cargados': 7}, 'ultimo_error': None,
        }
        # NC1 no se contaminó con el resultado de NS1
        assert inv_service.estado_carga_inventario('NC1')['ultimo_resultado'] is None


class TestSiesaGeneralPorAlmacenNoGlobal:
    """m016: `ubicaciones.codigo` era único GLOBAL — descubierto en vivo al
    calibrar NS1 (2026-08-27): crear su propio SIESA-GENERAL chocó con el de
    NB1 (`UniqueViolation: ubicaciones_codigo_key`). Nunca se notó porque
    hasta entonces SIESA-GENERAL solo se creaba para NB1."""

    def test_dos_almacenes_pueden_tener_siesa_general(self, db):
        from app.models.ubicacion import Ubicacion

        nb1 = Almacen(codigo='NB1', nombre='NB1', bodega_siesa_id='NB1', activo=True)
        ns1 = Almacen(codigo='NS1', nombre='NS1', bodega_siesa_id='NS1', activo=True)
        db.session.add_all([nb1, ns1])
        db.session.commit()

        ub_nb1 = inv_service._get_o_crear_ubicacion_general(nb1.id)
        ub_ns1 = inv_service._get_o_crear_ubicacion_general(ns1.id)
        db.session.commit()

        assert ub_nb1.codigo == ub_ns1.codigo == Ubicacion.CODIGO_GENERAL
        assert ub_nb1.almacen_id != ub_ns1.almacen_id
        assert ub_nb1.id != ub_ns1.id


class TestOperacionesActivasEsPorAlmacenNoGlobal:
    """`TareaPicking.almacen_id` es un campo propio (no derivado de la
    ubicación) — cubre que el guard compartido lo use directamente y no
    mire, por accidente, operaciones de OTRO almacén."""

    def _almacen(self, db, bodega):
        a = Almacen(codigo=bodega, nombre=bodega, bodega_siesa_id=bodega, activo=True)
        db.session.add(a)
        db.session.commit()
        return a

    def test_picking_activo_en_otro_almacen_no_cuenta(self, db, producto):
        from app.models.ubicacion import Ubicacion
        nb1 = self._almacen(db, 'NB1')
        ns1 = self._almacen(db, 'NS1')
        ub_nb1 = Ubicacion(codigo='UB-NB1', almacen_id=nb1.id)
        db.session.add(ub_nb1)
        db.session.commit()
        db.session.add(TareaPicking(
            codigo='PICK-NB1-1', producto_id=producto.id, cantidad_solicitada=5,
            ubicacion_id=ub_nb1.id, almacen_id=nb1.id, estado='PENDIENTE',
        ))
        db.session.commit()

        picks, packs = inv_service._operaciones_activas_en_almacen(ns1.id)
        assert (picks, packs) == (0, 0)

        picks_nb1, _ = inv_service._operaciones_activas_en_almacen(nb1.id)
        assert picks_nb1 == 1

    def test_packing_completado_no_cuenta_como_activo(self, db):
        nb1 = self._almacen(db, 'NB1')
        db.session.add(TareaPacking(
            codigo='PACK-1', tipo_documento='TRASLADO', almacen_id=nb1.id, estado='CARGADO',
        ))
        db.session.commit()

        picks, packs = inv_service._operaciones_activas_en_almacen(nb1.id)
        assert (picks, packs) == (0, 0)


class TestCargaFisicaDiariaEsSecuencialYAcotadaPorBodega:

    def test_omite_solo_la_bodega_con_operaciones_activas(self, db, app, producto, monkeypatch):
        from app.models.ubicacion import Ubicacion
        ns1 = Almacen(codigo='NS1', nombre='NS1', bodega_siesa_id='NS1', activo=True)
        nc1 = Almacen(codigo='NC1', nombre='NC1', bodega_siesa_id='NC1', activo=True)
        db.session.add_all([ns1, nc1])
        db.session.commit()
        ub_ns1 = Ubicacion(codigo='UB-NS1', almacen_id=ns1.id)
        db.session.add(ub_ns1)
        db.session.commit()
        db.session.add(TareaPicking(
            codigo='PICK-NS1-1', producto_id=producto.id, cantidad_solicitada=1,
            ubicacion_id=ub_ns1.id, almacen_id=ns1.id, estado='EN_PROCESO',
        ))
        db.session.commit()

        monkeypatch.setattr(inv_service, '_BODEGAS_CALIBRACION_FISICA', ('NS1', 'NC1'))
        llamadas = []
        monkeypatch.setattr(inv_service, '_run_carga_inicial',
                             lambda app, bodega=None: llamadas.append(bodega))

        inv_service._ejecutar_carga_fisica_diaria(app)

        # NS1 tiene picking activo -> se omite. NC1 no -> se carga.
        assert llamadas == ['NC1']

    def test_flag_apagada_no_llama_a_nadie(self, app, monkeypatch):
        monkeypatch.setenv('CARGA_FISICA_AUTOMATICA', 'false')
        monkeypatch.setattr(inv_service, '_BODEGAS_CALIBRACION_FISICA', ('NS1', 'NC1'))
        llamadas = []
        monkeypatch.setattr(inv_service, '_run_carga_inicial',
                             lambda app, bodega=None: llamadas.append(bodega))

        inv_service._ejecutar_carga_fisica_diaria(app)

        assert llamadas == []

    def test_bodega_sin_almacen_se_omite_y_sigue_con_las_demas(self, db, app, monkeypatch):
        nc1 = Almacen(codigo='NC1', nombre='NC1', bodega_siesa_id='NC1', activo=True)
        db.session.add(nc1)
        db.session.commit()

        monkeypatch.setattr(inv_service, '_BODEGAS_CALIBRACION_FISICA', ('NS1', 'NC1'))
        llamadas = []
        monkeypatch.setattr(inv_service, '_run_carga_inicial',
                             lambda app, bodega=None: llamadas.append(bodega))

        inv_service._ejecutar_carga_fisica_diaria(app)

        # NS1 no tiene Almacen todavia (Fase 2) -> se omite sin tumbar el resto
        assert llamadas == ['NC1']


class TestRutaCargarInventarioLimitaBodegasHabilitadas:

    def test_bodega_no_habilitada_devuelve_400(self, db, client, jwt_token_admin):
        r = client.post('/api/siesa/cargar-inventario?bodega=FP1',
                         headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 400
        assert 'FP1' in r.get_json()['error']

    def test_ns1_habilitada_no_devuelve_400_por_la_lista(self, db, client, jwt_token_admin, monkeypatch):
        # No verificamos que la carga real corra (requeriría todo el pipeline
        # Siesa) — solo que la validación de bodega NO es lo que la bloquea.
        called = {}

        def _fake_iniciar(app, forzar=False, bodega=None):
            called['bodega'] = bodega
            return {'simulado': True}

        monkeypatch.setattr(inv_service, 'iniciar_carga_inventario', _fake_iniciar)
        r = client.post('/api/siesa/cargar-inventario?bodega=NS1',
                         headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 202
        assert called['bodega'] == 'NS1'
