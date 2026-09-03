"""La foto del tablero cuelga de la LECTURA, no solo de la custodia.

## Qué costaba

`LecturaOdometro.foto_id` existe desde la tanda 1, con su clave foránea a
`flota_foto`, y **nadie la escribía**: en producción son **26 de 26 lecturas con
`foto_id` NULL** (medido el 2026-09-01). La foto del tablero colgaba únicamente
de `custodia_inicio`.

La consecuencia es de auditoría, que es justo para lo que la foto existe: para
verificar un kilometraje había que ir de la lectura a su custodia por la marca
de tiempo, de ahí a las fotos de esa custodia, y **confiar en que la foto y el
número eran del mismo gesto**. Con diez lecturas compartiendo segundo en
producción, esa confianza no está fundada.

La regla 7 del módulo dice que *«toda foto nace atada a un ID de dominio»*, y
`EntidadFoto.ODOMETRO` está en el vocabulario desde el principio, sin usarse. Es
la misma familia de defecto que el resto de este módulo: la pieza construida y
desconectada.

## Dos decisiones que se tomaron acá

**El padre de la foto no se toca.** Sigue siendo `custodia_inicio` y no pasa a
`odometro`, porque `GET /flota/custodia/<id>/fotos` filtra por
`entidad_tipo IN ('custodia_inicio','custodia_fin')` (`api/custodia.py:216`) y
moverla la haría desaparecer del recibo. El vínculo es **aditivo**: una foto,
dos preguntas que puede contestar.

**Se elige por CLASE, no por ángulo.** `foto_dato` es lo que el propio sistema ya
usa para identificar el tablero *«sin adivinar por el orden»*, y el ángulo puede
venir vacío — en producción hay fotos con `angulo` NULL.

## Y el orden importa, porque el invariante lo obliga

La tabla es append-only: `UPDATE` está bloqueado por trigger. La primera versión
de este cambio escribía la lectura y después le seteaba `foto_id`, y **el trigger
la frenó** con `lectura_odometro es append-only`. La lectura tiene que nacer con
el vínculo puesto, o no tenerlo. Es el invariante haciendo exactamente lo suyo.
"""
import base64
import io

import pytest

_AUTH = lambda t: {'Authorization': f'Bearer {t}'}    # noqa: E731


def _jpeg_url(ancho=1600, alto=1200):
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (ancho, alto), (17, 17, 17)).save(buf, 'JPEG', quality=85)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def mundo(db):
    from app.models.almacen import Almacen
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo
    v = Vehiculo(placa='FOTOLEC1', tipo='NHR', activo=True)
    a = Almacen(codigo='FL-1', nombre='Sede foto')
    db.session.add_all([v, a])
    db.session.flush()
    c = Conductor(nombre='Conductor Foto', cedula='FL-7001', activo=True)
    db.session.add(c)
    db.session.commit()
    return {'placa': v.placa, 'veh': v.id, 'con': c.id, 'sede': a.id}


def _traspaso(client, token, mundo, fotos, km=500):
    return client.post('/flota/custodia/traspaso',
                       json={'placa': mundo['placa'], 'km': km,
                             'custodio_tipo': 'conductor',
                             'custodio_conductor_id': mundo['con'],
                             'fotos_inicio': fotos},
                       headers=_AUTH(token))


class TestElVinculoExiste:
    def test_la_lectura_queda_apuntando_a_la_foto_del_tablero(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        """**Lo que no pasaba.** Antes del 2026-09-01 esto daba `None`."""
        from flota.adaptadores.modelos import Foto, LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'evidencia_estado', 'angulo': 'frontal',
             'data_url': _jpeg_url(800, 600), 'ancho': 800, 'alto': 600},
            {'clase': 'foto_dato', 'angulo': 'tablero',
             'data_url': _jpeg_url(1600, 1200), 'ancho': 1600, 'alto': 1200},
        ])
        assert r.status_code in (200, 201), r.get_json()

        lec = LecturaOdometro.query.filter_by(vehiculo_id=mundo['veh']).one()
        assert lec.foto_id is not None, (
            'la lectura quedó sin foto: para auditar el kilometraje hay que '
            'cruzar a mano por la custodia y confiar en el timestamp')

        foto = Foto.query.get(lec.foto_id)
        assert foto.clase == 'foto_dato', (
            f'la lectura apunta a una foto de clase {foto.clase!r}: se ató la '
            f'foto equivocada')

    def test_la_foto_sigue_colgando_de_la_custodia(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        """El vínculo es aditivo. Si el padre cambiara a `odometro`, la foto
        desaparecería de `GET /flota/custodia/<id>/fotos`, que filtra por
        `custodia_inicio`/`custodia_fin`."""
        from flota.adaptadores.modelos import Foto, LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'foto_dato', 'angulo': 'tablero',
             'data_url': _jpeg_url(), 'ancho': 1600, 'alto': 1200},
        ])
        cid = r.get_json()['custodia_id']
        lec = LecturaOdometro.query.one()
        foto = Foto.query.get(lec.foto_id)
        assert foto.entidad_tipo == 'custodia_inicio'
        assert foto.entidad_id == cid

        d = client.get(f'/flota/custodia/{cid}/fotos',
                       headers=_AUTH(jwt_token_admin)).get_json()
        assert any(f['clase'] == 'foto_dato' for f in d['fotos']), (
            'la foto del tablero se salió del listado del recibo')

    def test_se_elige_por_clase_y_no_por_orden(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        """La `foto_dato` va last en el payload: si se eligiera por posición,
        se ataría la de evidencia."""
        from flota.adaptadores.modelos import Foto, LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'evidencia_estado', 'angulo': 'frontal',
             'data_url': _jpeg_url(800, 600), 'ancho': 800, 'alto': 600},
            {'clase': 'evidencia_estado', 'angulo': 'trasera',
             'data_url': _jpeg_url(800, 601), 'ancho': 800, 'alto': 601},
            {'clase': 'foto_dato', 'data_url': _jpeg_url(), 'ancho': 1600,
             'alto': 1200},
        ])
        lec = LecturaOdometro.query.one()
        assert Foto.query.get(lec.foto_id).clase == 'foto_dato'

    def test_funciona_aunque_la_foto_no_traiga_angulo(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        """En producción hay fotos con `angulo` NULL. Elegir por ángulo habría
        dejado esas lecturas sin evidencia."""
        from flota.adaptadores.modelos import LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'foto_dato', 'data_url': _jpeg_url(), 'ancho': 1600,
             'alto': 1200},
        ])
        assert LecturaOdometro.query.one().foto_id is not None


class TestSinFotoNoSeInventa:
    """Regla 4: `None` es «no hay evidencia», no un cero ni un vínculo falso.
    Y regla 5: no se traba al conductor por una foto que faltó."""

    def test_un_traspaso_sin_foto_dato_deja_la_lectura_sin_foto(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        from flota.adaptadores.modelos import LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        r = _traspaso(client, jwt_token_admin, mundo, [
            {'clase': 'evidencia_estado', 'angulo': 'frontal',
             'data_url': _jpeg_url(800, 600), 'ancho': 800, 'alto': 600},
        ])
        assert r.status_code in (200, 201), (
            'el traspaso se trabó por falta de foto del tablero: el conductor '
            'no puede quedarse en el patio')
        assert LecturaOdometro.query.one().foto_id is None

    def test_un_traspaso_sin_ninguna_foto_sigue_entrando(
            self, client, jwt_token_admin, db, mundo, tmp_path, monkeypatch):
        """El arranque en frío y la red mala del patio."""
        from flota.adaptadores.modelos import LecturaOdometro
        monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))

        r = _traspaso(client, jwt_token_admin, mundo, [])
        assert r.status_code in (200, 201), r.get_json()
        assert LecturaOdometro.query.one().foto_id is None


class TestElOrdenQueElInvarianteObliga:
    """La tabla es append-only. Este archivo existe en parte para dejar dicho
    por qué la lectura se escribe DESPUÉS de las fotos."""

    def test_no_se_puede_actualizar_una_lectura_ya_escrita(self, app, db, mundo):
        """La primera versión del arreglo seteaba `foto_id` sobre la fila ya
        insertada y el trigger la frenó. Si alguien lo reintenta, esto explica
        por qué no se puede."""
        from sqlalchemy.exc import IntegrityError, OperationalError
        from app.models.usuario import Usuario
        from flota.adaptadores.modelos import LecturaOdometro

        u = Usuario(email='ord_ct@test.com', nombre='CT', rol='admin', activo=True)
        u.set_password('x')
        db.session.add(u)
        db.session.commit()

        lec = LecturaOdometro(vehiculo_id=mundo['veh'], valor_km=100,
                              origen='entrega', autor_usuario_id=u.id)
        db.session.add(lec)
        db.session.commit()

        lec.foto_id = None if lec.foto_id else 1
        with pytest.raises((IntegrityError, OperationalError), match='append-only'):
            db.session.commit()
        db.session.rollback()
