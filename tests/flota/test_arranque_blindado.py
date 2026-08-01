"""
El WMS arranca aunque `flota/` no se pueda importar.

`app/routes/__init__.py` importa `flota.api`. Sin blindaje eso es un acoplamiento
de arranque entre código nuevo sin estrenar y una aplicación que va a producción:
si `flota/` no llega al contenedor o revienta al importarse, no falla el módulo
—falla el arranque del WMS entero—.

Un mecanismo de seguridad que nunca se ejerció no es un mecanismo, es una
intención. Acá se ejerce: se simula el fallo de import y se comprueba que la app
levanta, que `/flota/*` responde 503, y que el motivo viaja en la respuesta.

No contradice la regla 5 del módulo. Lo prohibido es degradar hacia algo que se
parezca al éxito; un 503 con el motivo declarado es lo contrario.
"""
from flask import Flask

from app.routes import _registrar_flota, _registrar_flota_caida


class TestSustitutoDeclarado:

    def _app_con_flota_caida(self, motivo='ImportError: no module named flota'):
        app = Flask(__name__)
        _registrar_flota_caida(app, motivo)
        return app

    def test_responde_503_y_no_500(self):
        with self._app_con_flota_caida().test_client() as c:
            assert c.get('/flota/health').status_code == 503

    def test_el_motivo_viaja_en_la_respuesta(self):
        """Un 503 sin motivo obliga a ir al log. El motivo va en el cuerpo."""
        with self._app_con_flota_caida('RuntimeError: falta boto3').test_client() as c:
            cuerpo = c.get('/flota/health').get_json()
        assert cuerpo['error'] == 'modulo_flota_no_disponible'
        assert 'boto3' in cuerpo['motivo']

    def test_cubre_cualquier_ruta_de_flota_y_cualquier_metodo(self):
        """Los endpoints de la tanda 1 son POST y PUT, no solo GET."""
        with self._app_con_flota_caida().test_client() as c:
            assert c.post('/flota/custodia/traspaso').status_code == 503
            assert c.put('/flota/vehiculo/TGZ653/ficha').status_code == 503
            assert c.get('/flota/').status_code == 503


class TestArranqueConImportRoto:

    def test_la_app_levanta_aunque_el_import_falle(self, monkeypatch):
        """El fallo se aísla: el WMS arranca, el módulo queda declarado caído."""
        import builtins

        real_import = builtins.__import__

        def import_que_falla(nombre, *args, **kwargs):
            if nombre.startswith('flota'):
                raise ImportError('simulado: flota no llegó al contenedor')
            return real_import(nombre, *args, **kwargs)

        monkeypatch.setattr(builtins, '__import__', import_que_falla)

        app = Flask(__name__)
        _registrar_flota(app)          # no levanta

        with app.test_client() as c:
            respuesta = c.get('/flota/health')
        assert respuesta.status_code == 503
        assert 'simulado' in respuesta.get_json()['motivo']

    def test_con_el_modulo_sano_se_monta_el_health_de_verdad(self, app):
        """La otra mitad: sin fallo, la ruta real está montada y pide sesión."""
        rutas = [str(r) for r in app.url_map.iter_rules() if str(r).startswith('/flota')]
        assert '/flota/health' in rutas
        assert '/flota/<path:ruta>' not in rutas, (
            'El sustituto de caída quedó montado con el módulo sano.'
        )
