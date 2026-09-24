"""La cola sin señal del conductor: reenviar no puede duplicar.

## La clase

*«Una operación que se reenvía después de una respuesta perdida se ejecuta dos
veces.»* El caso que la destapó es el recibo de turno: el conductor lo registra
a las 5 a.m. sin señal, el teléfono lo manda a las 7, la respuesta se pierde, y
el reenvío de las 7:05 —fuera de la ventana de 90 s de `traspaso.py`— cerraba
la custodia recién abierta y abría otra con los mismos kilómetros.

La misma forma vale para las otras tres operaciones que la pantalla encola:
daño, inspección y tanqueo. Un tanqueo duplicado es plata contada dos veces en
el costo por kilómetro.

## El trinquete

`TestInventarioDeLaCola`: toda operación que la cola del conductor reenvía
(inventario declarado en `OPERACION_IDEMPOTENTE`) tiene su endpoint marcado con
`@idempotente`, y ningún endpoint lleva la marca sin estar en el inventario. Se
lee del objeto vivo (`url_map` + la marca del decorador), no del texto.
"""
import base64
import json

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

_JPEG_B64 = (
    '/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof'
    'Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwh'
    'MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAAR'
    'CAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA'
    'AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkK'
    'FhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWG'
    'h4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl'
    '5ufo6erx8vP09fb3+Pn6/9oACAEBAAA/APn+iiigD//Z'
)
_DATA_URL = f'data:image/jpeg;base64,{_JPEG_B64}'


def _auth(t):
    return {'Authorization': f'Bearer {t}'}


def _foto(clase='evidencia_estado', angulo=None):
    return {'clase': clase, 'angulo': angulo, 'data_url': _DATA_URL,
            'ancho': 1, 'alto': 1, 'mime': 'image/jpeg'}


@pytest.fixture
def mundo(db, app, almacen, tmp_path, monkeypatch):
    from app.models.conductor import Conductor
    from app.models.usuario import Usuario
    from app.models.vehiculo import Vehiculo

    monkeypatch.setenv('FLOTA_FOTOS_DIR', str(tmp_path))
    veh = Vehiculo(placa='COL100', tipo='NHR', activo=True)
    u = Usuario(nombre='Cond Cola', email='cola_cond@test.com',
                password_hash=generate_password_hash('x'), rol='conductor',
                almacen_id=almacen.id, activo=True)
    otro = Usuario(nombre='Otro Cola', email='cola_otro@test.com',
                   password_hash=generate_password_hash('x'), rol='conductor',
                   almacen_id=almacen.id, activo=True)
    flota = Usuario(nombre='Control Cola', email='cola_flota@test.com',
                    password_hash=generate_password_hash('x'),
                    rol='control_flota', almacen_id=almacen.id, activo=True)
    db.session.add_all([veh, u, otro, flota])
    db.session.flush()
    c = Conductor(nombre='Cond Cola', cedula='COLA-1', usuario_id=u.id,
                  activo=True, disponible=True)
    c2 = Conductor(nombre='Otro Cola', cedula='COLA-2', usuario_id=otro.id,
                   activo=True, disponible=True)
    db.session.add_all([c, c2])
    db.session.commit()
    with app.app_context():
        return {
            'placa': veh.placa, 'vehiculo_id': veh.id, 'conductor_id': c.id,
            'otro_conductor_id': c2.id,
            't_cond': create_access_token(identity=str(u.id)),
            't_otro': create_access_token(identity=str(otro.id)),
            't_flota': create_access_token(identity=str(flota.id)),
        }


def _recibo(mundo, clave=None, km=1000, **extra):
    cuerpo = {'placa': mundo['placa'], 'km': km, 'custodio_tipo': 'conductor',
              'custodio_conductor_id': mundo['conductor_id'],
              'fotos_inicio': [_foto('evidencia_estado', 'frontal'),
                               _foto('foto_dato', 'tablero')]}
    if clave:
        cuerpo['clave_idempotencia'] = clave
    cuerpo.update(extra)
    return cuerpo


def _custodias(vehiculo_id):
    from flota.adaptadores.modelos import Custodia
    return Custodia.query.filter_by(vehiculo_id=vehiculo_id).count()


class TestUnReenvioNoDuplicaElTurno:

    def test_el_mismo_recibo_dos_veces_abre_una_sola_custodia(self, client, mundo):
        r1 = client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-recibo-1'),
                         headers=_auth(mundo['t_cond']))
        assert r1.status_code == 201, r1.get_json()
        r2 = client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-recibo-1'),
                         headers=_auth(mundo['t_cond']))
        assert r2.status_code == 200, r2.get_json()
        assert r2.get_json()['repetida'] is True
        assert r2.get_json()['custodia_id'] == r1.get_json()['custodia_id']
        assert _custodias(mundo['vehiculo_id']) == 1

    def test_fuera_de_la_ventana_de_90_segundos_tambien(self, client, mundo, monkeypatch):
        """El caso de la cola: el reenvío llega horas después. Se anula la
        ventana de `traspaso.py` para que lo único que proteja sea la clave."""
        from flota.adaptadores import traspaso
        monkeypatch.setattr(traspaso, 'VENTANA_IDEMPOTENCIA_S', 0)
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-tarde'),
                    headers=_auth(mundo['t_cond']))
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-tarde'),
                    headers=_auth(mundo['t_cond']))
        assert _custodias(mundo['vehiculo_id']) == 1, (
            'el reenvío tardío cerró el turno y abrió otro con los mismos km')

    def test_sin_clave_se_comporta_como_antes(self, client, mundo, monkeypatch):
        """La otra dirección: el panel de escritorio no manda clave y sigue igual.
        Sin la ventana, dos recibos del mismo custodio son el no-op de siempre
        (cierra y abre) — la clave es lo que lo distingue, no un cambio de regla."""
        from flota.adaptadores import traspaso
        monkeypatch.setattr(traspaso, 'VENTANA_IDEMPOTENCIA_S', 0)
        client.post('/flota/custodia/traspaso', json=_recibo(mundo),
                    headers=_auth(mundo['t_cond']))
        client.post('/flota/custodia/traspaso', json=_recibo(mundo),
                    headers=_auth(mundo['t_cond']))
        assert _custodias(mundo['vehiculo_id']) == 2

    def test_la_clave_guarda_la_hora_del_telefono_sin_creerle(self, client, mundo):
        from flota.adaptadores.modelos import Custodia, OperacionIdempotente
        client.post('/flota/custodia/traspaso',
                    json=_recibo(mundo, 'k-hora', ts_dispositivo='2026-09-24T10:05:00Z'),
                    headers=_auth(mundo['t_cond']))
        op = OperacionIdempotente.query.filter_by(clave='k-hora').one()
        assert op.ts_dispositivo.hour == 10 and op.ts_dispositivo.minute == 5
        c = Custodia.query.get(op.entidad_id)
        assert c.inicio_ts != op.ts_dispositivo, (
            'el turno quedó con la hora del teléfono: la hora la pone el servidor')


class TestUnRechazoNoGastaLaClave:

    def test_si_falla_el_reintento_corregido_entra(self, client, mundo):
        from flota.adaptadores.modelos import OperacionIdempotente
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-base', km=1000),
                    headers=_auth(mundo['t_cond']))
        # El odómetro no retrocede: el dominio lo rechaza con 409.
        r = client.post('/flota/custodia/traspaso',
                        json=_recibo(mundo, 'k-corrige', km=500),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 409, r.get_json()
        assert OperacionIdempotente.query.filter_by(clave='k-corrige').count() == 0
        r = client.post('/flota/custodia/traspaso',
                        json=_recibo(mundo, 'k-corrige', km=1200),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()

    def test_un_400_antes_de_tocar_el_adaptador_tampoco(self, client, mundo):
        from flota.adaptadores.modelos import OperacionIdempotente
        r = client.post('/flota/custodia/traspaso',
                        json=_recibo(mundo, 'k-400', ubicacion='sede'),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 400, r.get_json()
        assert OperacionIdempotente.query.filter_by(clave='k-400').count() == 0


class TestLaClaveEsDeQuienLaUso:

    def test_otro_usuario_con_la_misma_clave_no_recibe_el_resultado_ajeno(
            self, client, mundo):
        client.post('/flota/custodia/traspaso', json=_recibo(mundo, 'k-ajena'),
                    headers=_auth(mundo['t_cond']))
        r = client.post('/flota/hallazgos',
                        json={'placa': mundo['placa'], 'criticidad': 'menor',
                              'descripcion': 'x', 'km': 1000,
                              'clave_idempotencia': 'k-ajena'},
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 409
        assert 'custodia_id' not in (r.get_json() or {})


class TestDanoConFoto:

    def test_el_dano_guarda_su_foto_y_no_se_duplica(self, client, mundo):
        from flota.adaptadores.modelos import Foto, Hallazgo
        cuerpo = {'placa': mundo['placa'], 'criticidad': 'mayor',
                  'descripcion': 'Rayón puerta derecha', 'km': 1000,
                  'fotos': [_foto()], 'clave_idempotencia': 'k-dano'}
        r1 = client.post('/flota/hallazgos', json=cuerpo, headers=_auth(mundo['t_flota']))
        assert r1.status_code == 201, r1.get_json()
        r2 = client.post('/flota/hallazgos', json=cuerpo, headers=_auth(mundo['t_flota']))
        assert r2.status_code == 200 and r2.get_json()['repetida'] is True
        assert Hallazgo.query.count() == 1
        hid = r1.get_json()['id']
        assert Foto.query.filter_by(entidad_tipo='hallazgo', entidad_id=hid).count() == 1


class TestTanqueoConFotoDelRecibo:

    def _cuerpo(self, mundo, **extra):
        c = dict(placa=mundo['placa'], fecha='2026-09-24', valor='120000',
                 galones='10', tanque='lleno', estacion='Terpel Neiva', km=1000,
                 proveedor='Terpel Neiva', origen_costo='efectivo_conductor')
        c.update(extra)
        return c

    def test_la_foto_cuelga_del_gasto(self, client, mundo):
        from flota.adaptadores.modelos import Foto
        r = client.post('/flota/tanqueos',
                        json=self._cuerpo(mundo, fotos=[_foto('foto_dato')]),
                        headers=_auth(mundo['t_flota']))
        assert r.status_code == 201, r.get_json()
        fotos = Foto.query.filter_by(entidad_tipo='gasto',
                                     entidad_id=r.get_json()['id']).all()
        assert len(fotos) == 1
        # 1×1 px: por debajo del mínimo de una foto-dato. Se guarda, declarada.
        assert fotos[0].estado == 'pendiente_evidencia'

    def test_reenviar_el_tanqueo_no_lo_cuenta_dos_veces(self, client, mundo):
        from flota.adaptadores.modelos import Gasto
        cuerpo = self._cuerpo(mundo, clave_idempotencia='k-tanqueo')
        client.post('/flota/tanqueos', json=cuerpo, headers=_auth(mundo['t_flota']))
        r = client.post('/flota/tanqueos', json=cuerpo, headers=_auth(mundo['t_flota']))
        assert r.status_code == 200 and r.get_json()['repetida'] is True
        assert Gasto.query.count() == 1


class TestUnaFotoDatoChicaNoRevientaElRecibo:
    """La pantalla prometía «por debajo de 1600 px queda pendiente_evidencia» y
    el servidor la guardaba `ok`: el CHECK la rechazaba y el recibo terminaba
    en 500. Lo destapó la foto del recibo de tanqueo."""

    def test_un_tablero_de_1px_registra_el_turno(self, client, mundo):
        from flota.adaptadores.modelos import Foto
        r = client.post('/flota/custodia/traspaso', json=_recibo(mundo),
                        headers=_auth(mundo['t_cond']))
        assert r.status_code == 201, r.get_json()
        tablero = Foto.query.filter_by(clase='foto_dato').one()
        assert tablero.estado == 'pendiente_evidencia'


class TestInventarioDeLaCola:
    """TRINQUETE — todo lo que la cola reenvía, protegido; nada más marcado."""

    #: operación → (método, ruta) que la pantalla del conductor encola.
    RUTAS = {
        'traspaso': '/flota/custodia/traspaso',
        'hallazgo': '/flota/hallazgos',
        'inspeccion': '/flota/inspeccion',
        'tanqueo': '/flota/tanqueos',
    }

    def _marcadas(self, app):
        salida = {}
        for r in app.url_map.iter_rules():
            if 'POST' not in r.methods:
                continue
            fn = app.view_functions[r.endpoint]
            for _ in range(12):
                if hasattr(fn, 'idempotente_operacion'):
                    salida[str(r.rule)] = fn.idempotente_operacion
                    break
                fn = getattr(fn, '__wrapped__', None)
                if fn is None:
                    break
        return salida

    def test_el_inventario_coincide_con_el_vocabulario_de_la_base(self):
        from flota.adaptadores.modelos import OPERACION_IDEMPOTENTE
        assert set(self.RUTAS) == set(OPERACION_IDEMPOTENTE)

    def test_cada_ruta_de_la_cola_esta_protegida(self, app):
        marcadas = self._marcadas(app)
        for op, ruta in self.RUTAS.items():
            assert marcadas.get(ruta) == op, (
                f'{ruta} viaja por la cola sin señal y un reenvío la ejecuta dos veces')

    def test_ninguna_ruta_lleva_la_marca_sin_estar_declarada(self, app):
        marcadas = self._marcadas(app)
        assert set(marcadas) == set(self.RUTAS.values())

    def test_el_detector_ve_la_marca(self, app):
        """Piso: si el recorrido de `__wrapped__` se rompe, esto se pone rojo en
        vez de devolver un conjunto vacío que se lee como «nada que proteger»."""
        assert len(self._marcadas(app)) >= 4

    def test_la_pantalla_encola_exactamente_esas_rutas(self):
        """Cruce con el JS: las URL que `flotaColaAgregar` recibe son las del
        inventario. Una operación nueva encolada sin marcar en el servidor es la
        forma exacta del defecto."""
        import pathlib
        import re
        js = (pathlib.Path(__file__).resolve().parents[2] / 'app' / 'static'
              / 'pwa' / 'flota.js').read_text(encoding='utf-8')
        i = js.index('function flotaColaUrl(')
        cuerpo = js[i:js.index('\n}\n', i)]
        nombres = set(re.findall(r'return (FLOTA_\w+_URL);', cuerpo))
        assert len(nombres) >= 4, 'no se encontraron las constantes de la cola'
        urls = set()
        for n in nombres:
            m = re.search(rf"const {n} = '(/flota/[a-z/]+)';", js)
            assert m, f'{n} no es una constante literal'
            urls.add(m.group(1))
        assert urls == set(self.RUTAS.values())
