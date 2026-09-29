"""
Validación crítica de la tanda C1-C3 de conteo (2026-09-27, `val-conteo-c123`
@ 9797847f). Cada test reproducía un P1 como xfail estricto; arreglados el
2026-09-29 y sin marca:

- VAL-1: sin foto de Siesa, el TOTAL contado se comparaba contra UN hueco.
- VAL-2: el ajuste del WMS aplicaba el delta del total a un hueco con piso 0.
- VAL-3: `/editar` dejaba a una persona definir la cifra y firmarla.
- VAL-11: un conteo que cuadraba con Siesa no corregía el WMS (fantasmas).
- VAL-12: la entrada contra una fila sintética en cero (af2e2f2d) salía sin
  costo y la firmaba un supervisor de cualquier monto.
"""
import json

import pytest

from tests.test_conteo_teorico_pos import SKU, contar_primero, siesa, tienda  # noqa: F401
from tests.test_conteo_unidad_sku_almacen import _stock, _ubicacion, dos_huecos  # noqa: F401


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


@pytest.fixture(autouse=True)
def _sin_env(monkeypatch):
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    for n in VARIABLES_DE_ENTORNO:
        monkeypatch.delenv(n, raising=False)


def _raiz_manual(dos_huecos):
    from app.models.conteo import SesionConteo
    r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
    (s,) = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).all()
    return s


class TestFallbackSinFotoComparaContraUnHueco:

    def test_sin_foto_el_total_contado_se_compara_con_el_total_wms(
            self, db, siesa, dos_huecos, monkeypatch):
        # Siesa caída: foto None → fallback al WMS. Desde af2e2f2d «Siesa
        # contestó que no hay fila» es existencia 0, no «no sé».
        siesa.caida = True
        s = _raiz_manual(dos_huecos)
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        # El WMS lo tiene en POS-UB 10 + CROSS-DOCK 90; quien cuenta suma 100.
        r = contar_primero(s.id, dos_huecos['a'].id, 100, cero_confirmado=True)
        assert r['resultado'] == 'MATCH', r


class TestElAjusteWmsConPisoCeroEscondeUnidades:

    def test_tras_el_ajuste_el_wms_del_sku_es_lo_contado(
            self, db, siesa, dos_huecos, monkeypatch):
        from app.models.siesa_job import SiesaJob
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.siesa_job_service import _ejecutar_job
        # Como NB1 en producción: SIESA-GENERAL 10 + CROSS-DOCK 90 (POS-UB en 0).
        general = _ubicacion(db, dos_huecos['almacen'].id, 'SIESA-GENERAL')
        _stock(db, general, dos_huecos['producto'].id, 10)
        dos_huecos['registro'].cantidad = 0
        db.session.commit()
        siesa.poner(existencia=100)
        monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                            lambda self, **kw: {'codigo': 0})
        s = _raiz_manual(dos_huecos)
        assert s.ubicacion_id == general.id
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        r1 = contar_primero(s.id, dos_huecos['a'].id, 60)   # faltan 40, en CROSS-DOCK
        assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
        _svc().obtener_tarea_operario(r1['segundo_conteo_id'], dos_huecos['b'].id)
        _svc().registrar_conteo(r1['segundo_conteo_id'], dos_huecos['b'].id, 60)
        job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=s.id).one()
        assert (json.loads(job.payload)['motivo_codigo'],
                json.loads(job.payload)['cantidad']) == ('AJ-SAL', 40)
        _ejecutar_job(job)
        # Siesa queda en 60. El WMS: GENERAL max(0, 10 − 40) = 0, CROSS-DOCK 90 → 90.
        assert _svc().existencia_wms_del_sku(
            dos_huecos['producto'].id, dos_huecos['almacen'].id) == 60


class TestEditarLaCifraEsDefinirLa:

    def _cadena(self, db, siesa, tienda, cc2=5):
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=10, costo=30000.0)
        r = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        (s,) = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).all()
        _svc().obtener_tarea_operario(s.id, tienda['a'].id)
        r1 = contar_primero(s.id, tienda['a'].id, 5)
        _svc().obtener_tarea_operario(r1['segundo_conteo_id'], tienda['b'].id)
        _svc().registrar_conteo(r1['segundo_conteo_id'], tienda['b'].id, cc2)
        return db.session.get(SesionConteo, s.id)

    def test_la_cifra_verificada_por_el_doble_ciego_no_se_corrige(self, db, siesa, tienda):
        """CC1 = CC2 = 5 contra 10 ($150.000): el supervisor ya no puede bajarla a
        8 ($60.000) y firmarla, ni subirla a 10 y cerrarla en MATCH sin firma."""
        raiz = self._cadena(db, siesa, tienda)
        assert raiz.estado == 'DESCUADRE'          # −5 × $30.000 = $150.000 > tope
        for cifra in (8, 10):
            with pytest.raises(ValueError, match='segundo conteo de otra persona'):
                _svc().corregir_cantidad(raiz, cifra, usuario_id=tienda['supervisor'].id)
        db.session.rollback()
        assert (raiz.cantidad_fisica, raiz.estado) == (5, 'DESCUADRE')

    def test_la_ruta_dice_por_que(self, app, client, db, siesa, tienda):
        from tests.test_conteo_pool_sin_dueno import _token
        raiz = self._cadena(db, siesa, tienda)
        r = client.put(f'/api/conteo/{raiz.id}/editar',
                       json={'cantidad_fisica': 8, 'motivo_edicion': 'me equivoqué'},
                       headers=_token(app, tienda['supervisor']))
        assert r.status_code == 409 and 'segundo conteo' in r.get_json()['error']
        assert raiz.to_dict()['no_se_corrige_cantidad']

    def _sin_verificar(self, db, siesa, tienda):
        """Un 1er conteo cerrado sin segundo (MATCH) — lo único que queda corregible."""
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=10, costo=1000.0)
        r = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        (s,) = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).all()
        _svc().obtener_tarea_operario(s.id, tienda['a'].id)
        assert contar_primero(s.id, tienda['a'].id, 10)['resultado'] == 'MATCH'
        return db.session.get(SesionConteo, s.id)

    def test_quien_corrige_un_conteo_sin_verificar_no_lo_firma(self, db, siesa, tienda):
        raiz = self._sin_verificar(db, siesa, tienda)
        sup = tienda['supervisor']
        _svc().corregir_cantidad(raiz, 9, usuario_id=sup.id)       # 1 und × $1.000
        db.session.commit()
        assert raiz.estado == 'DESCUADRE' and raiz.cantidad_corregida_por_id == sup.id
        assert 'corrigió a mano' in (_svc().motivo_no_puede_aprobar(sup, raiz) or '')
        from werkzeug.security import generate_password_hash
        from app.models.usuario import Usuario
        otro = Usuario(nombre='sup-otro', email='sup-otro-val3@test.com', rol='supervisor',
                       password_hash=generate_password_hash('x'), activo=True,
                       almacen_id=tienda['almacen'].id)
        db.session.add(otro)
        db.session.commit()
        assert _svc().motivo_no_puede_aprobar(otro, raiz) is None

    def test_corregir_hasta_la_cifra_de_siesa_no_cierra_sin_firma(self, db, siesa, tienda):
        raiz = self._sin_verificar(db, siesa, tienda)
        _svc().corregir_cantidad(raiz, 9, usuario_id=tienda['supervisor'].id)
        db.session.commit()
        with pytest.raises(ValueError, match='sin que nadie firme'):
            _svc().corregir_cantidad(raiz, 10, usuario_id=tienda['supervisor'].id)


class TestLaCosturaConSinFilaEsCero:
    """af2e2f2d («Siesa no tiene fila» = existencia 0) sobre la cadena SKU × almacén.
    Producción: 9 SKUs de NB1 en CROSS-DOCK (4.052 und), sin recepción, sin
    traslado y sin un solo movimiento de inventario que los explique."""

    def test_match_en_cero_deja_el_wms_en_cero(self, db, siesa, dos_huecos):
        siesa.fila = None                      # Siesa contesta «no hay registros»
        siesa.en_maestro = True                # y el ítem existe → existencia 0
        s = _raiz_manual(dos_huecos)
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        r = contar_primero(s.id, dos_huecos['a'].id, 0, cero_confirmado=True)
        assert r['resultado'] == 'MATCH', r
        assert _svc().existencia_wms_del_sku(
            dos_huecos['producto'].id, dos_huecos['almacen'].id) == 0
        from app.models.inventario import MovimientoInventario
        assert {m.tipo for m in MovimientoInventario.query.all()} == {'CUADRE_CONTEO'}

    def test_match_con_siesa_normal_tambien_cuadra_el_wms(self, db, siesa, dos_huecos):
        """El WMS tenía 100 (10 + 90), Siesa y el estante 70: el MATCH lo deja en
        70 y no toca Siesa (no hay job)."""
        from app.models.siesa_job import SiesaJob
        siesa.poner(existencia=70)
        s = _raiz_manual(dos_huecos)
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        assert contar_primero(s.id, dos_huecos['a'].id, 70)['resultado'] == 'MATCH'
        assert _svc().existencia_wms_del_sku(
            dos_huecos['producto'].id, dos_huecos['almacen'].id) == 70
        assert SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO').count() == 0

    def test_con_pos_pendiente_el_wms_queda_en_la_existencia(self, db, siesa, dos_huecos):
        """En tienda: contados 90 = existencia 100 − POS 10. El WMS refleja la
        existencia de Siesa (la carga de las 7:00 no lo deshace): 100."""
        siesa.poner(existencia=100, pos=10)
        s = _raiz_manual(dos_huecos)
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        assert contar_primero(s.id, dos_huecos['a'].id, 90)['resultado'] == 'MATCH'
        assert _svc().existencia_wms_del_sku(
            dos_huecos['producto'].id, dos_huecos['almacen'].id) == 100

    def _entrada_sin_fila(self, db, siesa, dos_huecos, cantidad=100):
        from app.models.conteo import SesionConteo
        siesa.fila = None
        siesa.en_maestro = True
        s = _raiz_manual(dos_huecos)
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        r1 = contar_primero(s.id, dos_huecos['a'].id, cantidad)
        _svc().obtener_tarea_operario(r1['segundo_conteo_id'], dos_huecos['b'].id)
        _svc().registrar_conteo(r1['segundo_conteo_id'], dos_huecos['b'].id, cantidad)
        return db.session.get(SesionConteo, s.id)

    def test_la_entrada_de_un_item_sin_existencia_no_la_firma_un_supervisor(
            self, db, siesa, dos_huecos):
        raiz = self._entrada_sin_fila(db, siesa, dos_huecos)
        assert (raiz.estado, raiz.motivo_codigo, raiz.diferencia) == ('DESCUADRE', 'AJ-ENT', 100)
        assert raiz.sin_fila_en_siesa is True and raiz.to_dict()['sin_fila_en_siesa'] is True
        assert 'la aprueba el admin' in (_svc().motivo_no_puede_aprobar(
            dos_huecos['supervisor'], raiz) or '')

    def test_ni_una_unidad_sale_sola(self, db, siesa, dos_huecos):
        raiz = self._entrada_sin_fila(db, siesa, dos_huecos, cantidad=1)
        assert raiz.estado == 'DESCUADRE'
        assert (_svc().motivo_no_sale_solo(raiz) or {}).get('codigo') == 'SIN_FILA_EN_SIESA'

    def test_el_admin_la_firma_y_el_costo_llega_al_142951(self, db, siesa, dos_huecos):
        """La firma es de VAL-12 (el admin); el COSTO es la política de David
        (f74ff44e, `costo_entrada_ajuste`: en vivo contra InvFecha de las otras
        bodegas al aprobar). Sin foto de costo, el tope no la valoriza."""
        from werkzeug.security import generate_password_hash
        from app.models.siesa_job import SiesaJob
        from app.models.usuario import Usuario
        admin = Usuario(nombre='adm-sf', email='adm-sf@test.com', rol='admin', activo=True,
                        password_hash=generate_password_hash('x'),
                        almacen_id=dos_huecos['almacen'].id)
        db.session.add(admin)
        db.session.commit()
        raiz = self._entrada_sin_fila(db, siesa, dos_huecos, cantidad=10)
        assert _svc().valor_del_ajuste(raiz) is None, 'sin costo en la foto, sin valor'
        siesa.otras_bodegas = [('FC1', 5, 250)]
        _svc().confirmar_ajuste(raiz.id, admin.id)
        job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=raiz.id).one()
        assert json.loads(job.payload)['costo_unitario'] == 250


class TestUnSoloAdmin:
    """VAL-7 (lo barato): el único admin que contó un CC3 caro sabe qué hacer."""

    def test_el_mensaje_dice_que_no_hay_otro_admin(self, db, siesa, tienda):
        from werkzeug.security import generate_password_hash
        from app.models.usuario import Usuario
        from tests.test_conteo_pool_sin_dueno import _cadena_con_cc3
        admin = Usuario(nombre='unico', email='unico-adm@test.com', rol='admin', activo=True,
                        password_hash=generate_password_hash('x'), almacen_id=tienda['almacen'].id)
        db.session.add(admin)
        db.session.commit()
        cc1, _, cc3 = _cadena_con_cc3(tienda, siesa)
        siesa.poner(existencia=10, pos=0, costo=200000.0)
        _svc().obtener_tarea_operario(cc3, admin.id)
        _svc().registrar_conteo(cc3, admin.id, 9)
        from app.models.conteo import SesionConteo
        raiz = db.session.get(SesionConteo, cc1)
        assert 'no hay otro admin' in _svc().motivo_no_puede_aprobar(admin, raiz)
        assert 'no hay ninguno' in _svc().motivo_no_puede_aprobar(tienda['supervisor'], raiz)
