"""Un conteo con la forma equivocada daba 500 con el cuerpo `{"error": "'id'"}`.

    recibidos_map = {i['id']: i['cantidad_recibida'] for i in items_recibidos}

El suscrito crudo convierte cualquier otra forma en `KeyError`, y el
`except Exception` de `confirmar_recepcion` (`routes/traslados.py`) lo devuelve
como **500 con el nombre de la clave como único mensaje**. Sin campo, sin
ítem, sin decir qué se esperaba.

Y eso ocurre **tres líneas debajo** de dos validaciones que explican todo:

    'La recepción exige las cantidades contadas. Sin ellas el sistema tendría
     que suponer que llegó todo lo que salió…'
    'Faltan las cantidades contadas de N ítem(s) (…). Un ítem sin conteo no se
     puede dar por recibido completo…'

La inconsistencia es el defecto: quien manda mal el conteo lee «error interno»
—que dice «el problema es del servidor, esperá»— cuando el problema es suyo y
tiene arreglo inmediato.

## No es un bug vivo del PWA, y conviene decirlo

Los dos clientes reales mandan la forma correcta: `recepcion.js:1271` y
`tienda.js:906`, los dos `{id, cantidad_recibida}`. Se encontró ejercitando el
flujo completo de averías contra una copia restaurada de producción el
2026-09-21, con un payload construido a mano. Lo que cubre este arreglo es el
reintento con un payload viejo, un script, y el próximo cliente.
"""
import pytest

from app.models.traslado import SolicitudTraslado
from app.services.traslado_service import TrasladoService


def _en_transito(db, usuario_admin, almacen, producto):
    """Un traslado listo para recibir, por la vía del servicio."""
    from app.models.traslado import ItemSolicitudTraslado
    import uuid
    s = SolicitudTraslado(
        codigo=f'ST-MAL-{uuid.uuid4().hex[:6]}',
        bodega_origen_siesa='NB1', bodega_destino_siesa='NC1',
        estado='EN_TRANSITO', modo_transferencia='EN_TRANSITO',
        bodega_transito_siesa='TRA1', solicitante_id=usuario_admin.id)
    db.session.add(s)
    db.session.flush()
    db.session.add(ItemSolicitudTraslado(
        solicitud_id=s.id, producto_id=producto.id,
        cantidad_solicitada=5, cantidad_enviada=5))
    db.session.commit()
    return s


class TestDiceQueFaltaYCual:

    @pytest.mark.parametrize('payload, falta', [
        ([{'item_id': 1, 'cantidad_recibida': 5}], 'id'),
        ([{'id': 1}], 'cantidad_recibida'),
        ([{'cantidad_recibida': 5}], 'id'),
        ([{}], 'ambos'),
    ])
    def test_una_forma_equivocada_es_ValueError_no_KeyError(
            self, app, db, usuario_admin, almacen, producto, payload, falta):
        """EL test. `ValueError` es lo que la ruta traduce a 400; `KeyError`
        cae al `except Exception` y sale 500."""
        with app.app_context():
            s = _en_transito(db, usuario_admin, almacen, producto)
            with pytest.raises(ValueError) as e:
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                    items_recibidos=payload)
            msg = str(e.value)
            assert 'id' in msg and 'cantidad_recibida' in msg, (
                f'el mensaje no nombra los dos campos que hacen falta: {msg!r}')
            assert 'interno' not in msg.lower()

    def test_una_lista_de_no_diccionarios_tampoco_revienta(
            self, app, db, usuario_admin, almacen, producto):
        """Un cliente que manda los ids sueltos en vez de objetos."""
        with app.app_context():
            s = _en_transito(db, usuario_admin, almacen, producto)
            with pytest.raises(ValueError):
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                    items_recibidos=[1, 2, 3])

    def test_el_mensaje_muestra_lo_que_llego(
            self, app, db, usuario_admin, almacen, producto):
        """Sin ver el payload, quien depura no sabe si mandó `item_id`,
        `producto_id` o nada."""
        with app.app_context():
            s = _en_transito(db, usuario_admin, almacen, producto)
            with pytest.raises(ValueError) as e:
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                    items_recibidos=[{'item_id': 77, 'cantidad_recibida': 5}])
            assert 'item_id' in str(e.value), (
                'el mensaje no muestra la forma recibida')


class TestLasOtrasTresValidacionesSiguenIntactas:
    """Detector en las dos direcciones: el guard nuevo no puede comerse a sus
    vecinos, que son los que de verdad protegen el inventario."""

    def test_sin_conteo_sigue_exigiendolo(self, app, db, usuario_admin, almacen, producto):
        with app.app_context():
            s = _en_transito(db, usuario_admin, almacen, producto)
            with pytest.raises(ValueError, match='exige las cantidades'):
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                    items_recibidos=None)

    def test_un_item_sin_contar_sigue_frenando(self, app, db, usuario_admin, almacen, producto):
        """Forma correcta, ítem que no existe: la validación de faltantes."""
        with app.app_context():
            s = _en_transito(db, usuario_admin, almacen, producto)
            with pytest.raises(ValueError, match='Faltan las cantidades'):
                TrasladoService.confirmar_recepcion(
                    solicitud_id=s.id, usuario_id=usuario_admin.id,
                    items_recibidos=[{'id': 999999, 'cantidad_recibida': 5}])


def test_la_forma_correcta_pasa_el_guard(app, db, usuario_admin, almacen, producto):
    """Un guard que rechaza todo prueba la mitad. La forma que mandan
    `recepcion.js` y `tienda.js` tiene que atravesarlo."""
    with app.app_context():
        s = _en_transito(db, usuario_admin, almacen, producto)
        it = s.items[0]
        try:
            TrasladoService.confirmar_recepcion(
                solicitud_id=s.id, usuario_id=usuario_admin.id,
                items_recibidos=[{'id': it.id, 'cantidad_recibida': 5}])
        except ValueError as e:
            assert 'no traen' not in str(e), (
                f'el guard de forma rechazó el payload que mandan los dos '
                f'clientes reales: {e}')


def test_los_dos_clientes_del_pwa_mandan_la_forma_correcta():
    """El otro lado del contrato. Si alguien cambia el JS a `item_id`, esto
    avisa antes de que el guard nuevo empiece a rechazar operación real."""
    from pathlib import Path
    pwa = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'pwa'
    for archivo in ('recepcion.js', 'tienda.js'):
        txt = (pwa / archivo).read_text(encoding='utf-8')
        i = txt.find('items_recibidos:')
        assert i > 0, f'{archivo} dejó de mandar items_recibidos'
        bloque = txt[i:i + 260]
        assert 'id:' in bloque, f'{archivo} no manda `id` en el conteo'
        assert 'cantidad_recibida:' in bloque, (
            f'{archivo} no manda `cantidad_recibida` en el conteo')
