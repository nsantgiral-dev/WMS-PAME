"""Ningún ajuste de conteo sale solo: todo espera en «Ajustes esperando
decisión» (decisión del dueño, 2026-09-30).

«Quisiera que la generación del documento no fuera automática, sino que
tuviéramos que aprobarlo desde Ajustes esperando decisión; así vemos el costo
antes de aprobarlo.» Pedido después de que PAPELSP7879 (ADI-00000046) y
PAPELSP11310 salieran solos con CC1 == CC2.

`conteo_politica.ajuste_automatico()` es el interruptor: apagado salvo
`CONTEO_AJUSTE_AUTOMATICO=true` literal. La suite lo enciende en
`tests/conftest.py` para seguir midiendo el camino automático; estos tests lo
apagan (borrando la variable), que es lo que corre en QA y producción.
"""
import pytest

from tests.test_conteo_teorico_pos import SKU, _cc1, _cc2, _jobs, _un_job, siesa, tienda  # noqa: F401


@pytest.fixture(autouse=True)
def _por_defecto(monkeypatch):
    monkeypatch.delenv('CONTEO_AJUSTE_AUTOMATICO', raising=False)


class TestElInterruptor:

    @pytest.mark.parametrize('valor,esperado', [
        (None, False), ('', False), ('false', False), ('si', False), ('1', False),
        ('true', True), (' TRUE ', True)])
    def test_solo_true_literal_lo_enciende(self, monkeypatch, valor, esperado):
        from app.services import conteo_politica
        if valor is None:
            monkeypatch.delenv('CONTEO_AJUSTE_AUTOMATICO', raising=False)
        else:
            monkeypatch.setenv('CONTEO_AJUSTE_AUTOMATICO', valor)
        assert conteo_politica.ajuste_automatico() is esperado


class TestCC1IgualCC2EsperaAprobacion:

    def _cadena(self, siesa, tienda, fisico=15):
        siesa.poner(existencia=10, costo=1000.0)
        cc1_id, r1 = _cc1(tienda, fisico)
        r2 = _cc2(tienda, r1, fisico)
        return cc1_id, r2

    def test_no_se_encola_nada(self, db, siesa, tienda):
        from app.models.conteo import SesionConteo
        cc1_id, r2 = self._cadena(siesa, tienda)
        assert r2['auto_encolado'] is False, r2
        assert _jobs(cc1_id) == [], 'con CC1 == CC2 no sale ningún documento solo'
        assert db.session.get(SesionConteo, cc1_id).estado == 'DESCUADRE'

    def test_dice_por_que_con_su_valor(self, db, siesa, tienda):
        from app.models.conteo import SesionConteo
        from app.services.conteo_service import ConteoService
        cc1_id, _ = self._cadena(siesa, tienda)
        m = ConteoService.motivo_no_sale_solo(db.session.get(SesionConteo, cc1_id))
        assert m['codigo'] == 'APROBACION_OBLIGATORIA'
        assert '$5.000' in m['mensaje'], m   # 5 und × $1.000

    def test_aparece_en_ajustes_esperando_decision_con_su_costo(self, db, siesa, tienda):
        from app.services import tablero_lider_conteo
        cc1_id, _ = self._cadena(siesa, tienda)
        filas = tablero_lider_conteo._ajustes(tienda['almacen'].id)['aprobables']['filas']
        fila = next(f for f in filas if f['id'] == cc1_id)
        assert (fila['unidades'], fila['costo_unitario'], fila['valor']) == (5, 1000.0, 5000.0)

    def test_al_aprobarlo_sale(self, db, siesa, tienda):
        from app.services.conteo_service import ConteoService
        cc1_id, _ = self._cadena(siesa, tienda)
        ConteoService.confirmar_ajuste(cc1_id, tienda['supervisor'].id)
        p = _un_job(cc1_id)
        assert (p['motivo_codigo'], p['cantidad']) == ('AJ-ENT', 5)

    def test_con_el_interruptor_encendido_sale_solo_como_antes(self, db, siesa, tienda, monkeypatch):
        monkeypatch.setenv('CONTEO_AJUSTE_AUTOMATICO', 'true')
        cc1_id, r2 = self._cadena(siesa, tienda)
        assert r2['auto_encolado'] is True, r2
        assert len(_jobs(cc1_id)) == 1


class TestLaToleranciaTampocoSaleSola:

    def _dentro_de_tolerancia(self, db, siesa, tienda):
        """Un conteo del plan, clase C, con 1 und de diferencia sobre 100:
        dentro de tolerancia (C: 1 und / 5 %). Antes se ajustaba solo, sin 2º
        conteo."""
        from app.models.conteo import SesionConteo
        from app.services.conteo_service import ConteoService
        from tests.test_conteo_teorico_pos import contar_primero
        siesa.poner(existencia=100, costo=1000.0)
        creado = ConteoService.crear_conteo_manual(tienda['almacen'].id, SKU)
        s = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one()
        s.tipo, s.clasificacion_abc = 'DIARIO_ABC', 'C'
        db.session.commit()
        ConteoService.obtener_tarea_operario(s.id, tienda['a'].id)
        r = contar_primero(s.id, tienda['a'].id, 99)
        return s.id, r

    def test_dentro_de_tolerancia_queda_para_aprobar(self, db, siesa, tienda):
        from app.models.conteo import SesionConteo
        from app.services.conteo_service import ConteoService
        sid, r = self._dentro_de_tolerancia(db, siesa, tienda)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert _jobs(sid) == []
        s = db.session.get(SesionConteo, sid)
        assert s.estado == 'DESCUADRE'
        assert ConteoService.motivo_no_sale_solo(s)['codigo'] == 'APROBACION_OBLIGATORIA'

    def test_con_el_interruptor_encendido_sale_sola(self, db, siesa, tienda, monkeypatch):
        monkeypatch.setenv('CONTEO_AJUSTE_AUTOMATICO', 'true')
        sid, r = self._dentro_de_tolerancia(db, siesa, tienda)
        assert r['resultado'] == 'DENTRO_TOLERANCIA', r
        assert len(_jobs(sid)) == 1
