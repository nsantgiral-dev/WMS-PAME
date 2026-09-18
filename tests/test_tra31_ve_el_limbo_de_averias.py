"""TRA-31: la avería que llegó y nadie dictaminó.

Este invariante nació de auditar mi propio trabajo. El commit que construyó el
veredicto lo describió como un tri-estado donde `None` significa «nadie miró» y
«tiene su propio reloj» — y después no había NADIE leyendo ese reloj. Cero
referencias a `averia_veredicto` fuera del modelo, el servicio, la ruta y los
tests.

Es exactamente la forma del defecto de `invariante-ausente-no-falla`: el guard
que no existe no da error, deja pasar. Y acá lo que deja pasar no es un hueco
de registro: mientras nadie dictamine, el sync repuebla esas unidades desde la
existencia de Siesa como stock vendible, y el CD puede despachar a un cliente
mercancía que alguien ya declaró rota.

Se prueba en las DOS direcciones. Un detector que solo se prueba cuando debe
disparar prueba la mitad: el que grita sobre operación sana se apaga a los tres
días y entonces no detecta nada.
"""
from datetime import datetime, timedelta

import pytest

from app.models.traslado import (ClaseTraslado, EstadoTraslado,
                                 SolicitudTraslado)
from app.models.usuario import Usuario
from app.services.auditoria.traslados import (
    toda_averia_recibida_termina_dictaminada as TRA31)


@pytest.fixture
def usuario(db):
    u = Usuario(nombre='T', email='t31@t.co', rol='tienda', bodega_siesa_id='NC1')
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _traslado(db, usuario, codigo, *, clase=ClaseTraslado.AVERIAS,
              estado=EstadoTraslado.ENTREGADA, veredicto=None, dias=3,
              con_fecha=True):
    kw = {}
    if clase == ClaseTraslado.AVERIAS:
        kw['averia_evidencia'] = 'fotos del punto'
        if veredicto is not None:
            kw['averia_veredicto'] = veredicto
            kw['averia_veredicto_at'] = datetime.utcnow()
    s = SolicitudTraslado(
        codigo=codigo, bodega_origen_siesa='NC1', bodega_destino_siesa='NB1',
        estado=estado, solicitante_id=usuario.id, clase_traslado=clase,
        fecha_entrega=(datetime.utcnow() - timedelta(days=dias)) if con_fecha else None,
        **kw)
    db.session.add(s)
    db.session.commit()
    return s


class TestDisparaCuandoDebe:

    def test_una_averia_entregada_sin_dictaminar_aparece(self, db, usuario):
        _traslado(db, usuario, 'ST-A1')
        h = TRA31()
        assert len(h) == 1
        assert h[0].datos['total'] == 1
        assert 'ST-A1' in [p['codigo'] for p in h[0].datos['pendientes']]

    def test_dice_hace_cuanto_llego(self, db, usuario):
        """Sin la edad, uno atascado hace un mes se ve igual que el de ayer —
        que es el defecto que TRA-30 ya tuvo que corregir."""
        _traslado(db, usuario, 'ST-A2', dias=12)
        h = TRA31()
        assert h[0].datos['dias_mas_viejo'] == 12
        assert '12 día' in h[0].detalle

    def test_varias_pendientes_dan_UN_hallazgo_no_uno_por_cada_una(self, db, usuario):
        """La pregunta es una sola: «¿hay averías sin decidir, y desde cuándo?».
        Veinte líneas repitiéndola entierran al que importa."""
        for i in range(5):
            _traslado(db, usuario, f'ST-A3-{i}', dias=i + 1)
        h = TRA31()
        assert len(h) == 1
        assert h[0].datos['total'] == 5
        assert h[0].datos['dias_mas_viejo'] == 5

    def test_sin_fecha_de_entrega_igual_aparece(self, db, usuario):
        """No saber hace cuánto llegó no la vuelve menos pendiente. Reportarla
        sin edad es peor que no reportarla, pero mucho mejor que perderla."""
        _traslado(db, usuario, 'ST-A4', con_fecha=False)
        h = TRA31()
        assert h[0].datos['total'] == 1
        assert h[0].datos['dias_mas_viejo'] is None


class TestNoDisparaSobreOperacionSana:
    """La mitad del detector que normalmente no se escribe."""

    def test_sin_averias_no_dice_nada(self, db, usuario):
        """Es el estado de producción HOY: cero traslados de clase AVERIAS.
        Si este invariante emitiera algo, ensuciaría el canal desde el día uno."""
        assert TRA31() == []

    def test_un_traslado_NORMAL_entregado_no_es_asunto_suyo(self, db, usuario):
        """Son 174 en producción. Si entraran acá, el aviso sería inleíble."""
        _traslado(db, usuario, 'ST-N1', clase=ClaseTraslado.NORMAL)
        assert TRA31() == []

    def test_una_averia_CONFIRMADA_ya_no_es_pendiente(self, db, usuario):
        _traslado(db, usuario, 'ST-A5', veredicto=True)
        assert TRA31() == []

    def test_una_averia_dictaminada_NO_AVERIADA_tampoco(self, db, usuario):
        """EL test de este archivo.

        `False` es un dictamen dado: alguien miró y decidió. Con
        `if s.averia_veredicto:` —truthiness en vez de `is not None`— este
        invariante gritaría para siempre sobre traslados ya resueltos, y un
        canal donde el aviso nunca se apaga deja de leerse.
        """
        _traslado(db, usuario, 'ST-A6', veredicto=False)
        assert TRA31() == []

    @pytest.mark.parametrize('estado', [
        EstadoTraslado.BORRADOR, EstadoTraslado.ENVIADA,
        EstadoTraslado.EN_PICKING, EstadoTraslado.EN_PACKING,
        EstadoTraslado.PREPARADO, EstadoTraslado.EN_TRANSITO,
        EstadoTraslado.RECHAZADA, EstadoTraslado.CANCELADA,
        EstadoTraslado.REVERTIDA,
    ])
    def test_una_averia_que_todavia_no_llego_no_se_puede_dictaminar(self, db,
                                                                    usuario,
                                                                    estado):
        """Reclamar un dictamen sobre mercancía que no llegó —o que se
        canceló, se rechazó o se revirtió— es pedir una decisión imposible.
        El backend la rechaza; el detector no debería ni nombrarla."""
        _traslado(db, usuario, f'ST-A7-{estado}', estado=estado)
        assert TRA31() == []


def test_el_invariante_esta_registrado_en_el_catalogo():
    """Un invariante que no está en el catálogo no corre, y no correr da el
    mismo verde que no encontrar nada (memoria: `mutacion-que-no-corrio`)."""
    from app.services.auditoria.base import registrados
    # `registrados()` y no el registro interno: dispara `_cargar()`, que es el
    # camino real por el que el panel descubre los invariantes. Leer la lista a
    # mano probaría que el decorador corrió en ESTE proceso, no que el panel lo
    # va a encontrar.
    inv = registrados('traslados')
    codigos = {i.codigo for i in inv}
    assert 'TRA-31' in codigos, f'TRA-31 no está en el catálogo: {sorted(codigos)}'
    tra31 = next(i for i in inv if i.codigo == 'TRA-31')
    assert 'vendible' in tra31.consecuencia.lower(), (
        'La consecuencia declarada no dice lo que de verdad pasa: hasta que '
        'alguien dictamine, esas unidades se ofrecen para venta.')
