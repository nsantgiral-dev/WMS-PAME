"""
Validación crítica de la tanda C1-C3 de conteo (2026-09-27, `val-conteo-c123`
@ 9797847f). Cada test reproducía un P1 como xfail estricto; arreglados el
2026-09-29 y sin marca:

- VAL-1: sin foto de Siesa, el TOTAL contado se comparaba contra UN hueco.
- VAL-2: el ajuste del WMS aplicaba el delta del total a un hueco con piso 0.
- VAL-3: `/editar` dejaba a una persona definir la cifra y firmarla.
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
        # Siesa no respondió: foto None → fallback al WMS. Se stubea la foto (y no
        # la fila) a propósito: desde af2e2f2d «Siesa contestó que no hay fila»
        # es existencia 0, no «no sé»; lo que cae al WMS es Siesa caído.
        from app.services.conteo_service import ConteoService
        monkeypatch.setattr(ConteoService, 'consultar_foto_siesa',
                            staticmethod(lambda **kw: None))
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
