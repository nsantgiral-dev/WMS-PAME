"""`TRASLADO_USA_RIT` — la RIT 174646 deja de dispararse al confirmar el picking.

Prueba real contra Siesa QA (2026-09-21, ST-20260921-0471): la RIT entró, el
WMS no pudo leer su consecutivo (el 401 de la consulta), el STS salió por el
173076 sin ella, y en Siesa quedó una requisición suelta con 10 unidades
reservadas (comprometida 77 → 87). La RIT solo la consumen el 174720 y el
174930; ninguno corre cuando el consecutivo no se lee.

Se ejerce el servicio de verdad —no se busca el nombre de la función en el
fuente— porque quitar la condición dejaba la llamada intacta en una rama
muerta y un test de texto seguía en verde.
"""
import pytest


@pytest.fixture
def solicitud_en_picking(db, almacen):
    from app.models.usuario import Usuario
    from tests.flujo import conductor_de_flujo as cf

    ids = {}
    for rol, email in (('tienda', 'tienda_sinrit@test.com'),
                       ('admin', 'admin_sinrit@test.com'),
                       ('operario', 'op_sinrit@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        ids[rol] = u.id
    db.session.commit()
    s = cf.flujo_traslado(db, almacen, ids['tienda'], ids['admin'], ids['operario'])
    return s, ids['operario']


@pytest.fixture
def llamadas_rit(monkeypatch):
    from app.services import traslado_service as ts

    llamadas = []

    def _crear_rit(**kw):
        llamadas.append(kw)
        return {'simulado': True}

    monkeypatch.setattr(ts.siesa_traslado, 'crear_rit', _crear_rit)
    return llamadas


class TestLaVariableApagaLaRit:
    def test_apagada_no_llama_a_siesa_y_el_traslado_avanza(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        from app.services.traslado_service import TrasladoService

        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        s, operario = solicitud_en_picking

        TrasladoService.confirmar_picking_traslado(s.id, operario)

        db.session.refresh(s)
        assert llamadas_rit == [], 'se creó una RIT con TRASLADO_USA_RIT=false'
        assert s.estado == 'EN_PACKING'
        assert s.siesa_requisicion_consec is None
        assert s.siesa_error is None, (
            'sin RIT no hay huérfana que avisar: el aviso mandaría a alguien '
            'a cerrar una requisición que nunca se creó')

    @pytest.mark.parametrize('valor', ['false', 'FALSE', '0', 'no', ' False '])
    def test_acepta_las_formas_usuales_de_apagar(self, monkeypatch, valor):
        from app.services.traslado_service import traslado_usa_rit

        monkeypatch.setenv('TRASLADO_USA_RIT', valor)
        assert traslado_usa_rit() is False


class TestPorDefectoNoCambiaNada:
    """Un deploy no puede cambiar el comportamiento por sí solo."""

    def test_sin_variable_sigue_disparando_la_rit(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        from app.services.traslado_service import TrasladoService

        monkeypatch.delenv('TRASLADO_USA_RIT', raising=False)
        s, operario = solicitud_en_picking

        TrasladoService.confirmar_picking_traslado(s.id, operario)

        assert len(llamadas_rit) == 1
        assert llamadas_rit[0]['codigo'] == s.codigo

    @pytest.mark.parametrize('valor', ['true', 'TRUE', '1', 'yes', ''])
    def test_cualquier_otro_valor_la_deja_encendida(self, monkeypatch, valor):
        from app.services.traslado_service import traslado_usa_rit

        monkeypatch.setenv('TRASLADO_USA_RIT', valor)
        assert traslado_usa_rit() is True


class TestElDespachoSigueSinRit:
    def test_sin_rit_el_sts_sale_por_173076_directo(
            self, db, solicitud_en_picking, llamadas_rit, monkeypatch):
        """El camino que se ejercitó en vivo: sin consecutivo de RIT, el
        despacho llama a `registrar_salida_transito` con las cantidades del
        picking."""
        from app.services import traslado_service as ts

        monkeypatch.setenv('TRASLADO_USA_RIT', 'false')
        s, operario = solicitud_en_picking
        # Sin SIESA_BODEGA_TRANSITO en el entorno de tests la solicitud nace
        # DIRECTA; el flujo real (y la prueba en vivo) es EN_TRANSITO.
        s.modo_transferencia = 'EN_TRANSITO'
        s.bodega_transito_siesa = 'TRA1'
        db.session.commit()
        ts.TrasladoService.confirmar_picking_traslado(s.id, operario)

        enviados = []
        monkeypatch.setattr(
            ts.siesa_traslado, 'registrar_salida_transito',
            lambda **kw: enviados.append(kw) or {'simulado': True})
        monkeypatch.setattr(
            ts.siesa_traslado, 'despachar_desde_rit',
            lambda **kw: pytest.fail('con la RIT apagada no puede usarse el 174930'))

        ts.TrasladoService.despachar(s.id)

        assert len(enviados) == 1
        assert enviados[0]['consec_requisicion'] is None
        assert llamadas_rit == []
