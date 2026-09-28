"""
La cola de pedidos muestra la fecha del pedido y el vendedor (2026-09-28).

· Los dos datos salen de `pedidos_historia` (el sync ya los guarda); el nombre
  del vendedor, del caché de `app/services/vendedores.py`, cruzado por NIT.
· La cola **no espera a Siesa**: con el caché vacío responde sin nombre y
  arranca el refresco en segundo plano.
· Un refresco fallido (Siesa devuelve []) no borra los nombres ya conocidos.
"""
import threading
import uuid
from datetime import date
from unittest.mock import patch

import pytest

from app.services import vendedores as V
from app.utils.fecha import dia_operativo

FILAS_VENDEDORES = [
    {'codigo_vendedor': '002', 'f200_nit': '53051164 ', 'f200_nombres': 'ANA',
     'f200_apellido1': 'PEREZ', 'f200_apellido2': '', 'f200_razon_social': 'X'},
    {'codigo_vendedor': '003', 'f200_nit': '900', 'f200_nombres': '',
     'f200_apellido1': '', 'f200_apellido2': '', 'f200_razon_social': 'DISTRIBUIDORA SAS'},
]


@pytest.fixture(autouse=True)
def _cache_limpio():
    V._reiniciar_cache()
    yield
    V._reiniciar_cache()


def _usuario(db):
    from app.models.usuario import Usuario
    u = Usuario(email=f'admin_{uuid.uuid4().hex[:6]}@t.com', nombre='admin', rol='admin',
                activo=True)
    u.set_password('x')
    db.session.add(u)
    db.session.commit()
    return u


def _jwt(app, u):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(u.id))}'}


def _pedido(db, consec, vendedor_id='53051164', fecha=date(2026, 9, 20)):
    from app.models.pedido_historia import PedidoHistoria
    from app.models.pedido_siesa import PedidoSiesa
    db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=consec, centro_op='003',
                               bodega='NB1', numero_pedido=f'PD{consec}', item_codigo='SKU1',
                               cantidad_pedida=5, cantidad_pendiente=5))
    db.session.add(PedidoHistoria(
        linea_rowid=uuid.uuid4().int % 10**12, pedido_clave=f'003-PD-{consec}', co='003',
        tipo_docto='PD', consec_docto=consec, bodega='NB1', item_codigo='SKU1',
        vendedor_id=vendedor_id, fecha_pedido=fecha, cantidad_pedida=5,
        primer_dia_visto=dia_operativo(), ultimo_dia_visto=dia_operativo()))
    db.session.commit()


def _cola(app, client, db):
    from app.services.connekta_gateway import connekta
    with patch.object(connekta, 'modo_simulacion', False):
        r = client.get('/api/siesa/pedidos', headers=_jwt(app, _usuario(db)))
    assert r.status_code == 200
    return {p['numero_pedido']: p for p in r.get_json()['pedidos']}


def _refresco_sincrono(filas):
    """Corre el refresco en el mismo hilo, con `filas` como respuesta de Siesa."""
    from app.services.connekta_gateway import ConnektaGateway
    return patch.object(ConnektaGateway, 'get_vendedor_contacto', lambda self, codigo=None: filas)


class TestLaColaTraeFechaYVendedor:

    def test_con_cache_lleno_trae_fecha_y_nombre(self, app, client, db):
        _pedido(db, 901)
        with _refresco_sincrono(FILAS_VENDEDORES):
            V._refrescar()
        p = _cola(app, client, db)['PD901']
        assert p['fecha_pedido'] == '2026-09-20'
        assert p['vendedor_id'] == '53051164'
        assert p['vendedor_nombre'] == 'ANA PEREZ'

    def test_la_razon_social_cuando_no_hay_nombres(self, app, client, db):
        _pedido(db, 902, vendedor_id='900-1')
        with _refresco_sincrono(FILAS_VENDEDORES):
            V._refrescar()
        assert _cola(app, client, db)['PD902']['vendedor_nombre'] == 'DISTRIBUIDORA SAS'

    def test_sin_historia_no_inventa_nada(self, app, client, db):
        from app.models.pedido_siesa import PedidoSiesa
        db.session.add(PedidoSiesa(tipo_docto='PD', consec_docto=903, centro_op='003',
                                   bodega='NB1', numero_pedido='PD903', item_codigo='SKU1',
                                   cantidad_pedida=5, cantidad_pendiente=5))
        db.session.commit()
        p = _cola(app, client, db)['PD903']
        assert p['fecha_pedido'] is None
        assert p['vendedor_id'] is None and p['vendedor_nombre'] is None

    def test_nit_sin_vendedor_conocido_viaja_sin_nombre(self, app, client, db):
        _pedido(db, 904, vendedor_id='111')
        with _refresco_sincrono(FILAS_VENDEDORES):
            V._refrescar()
        p = _cola(app, client, db)['PD904']
        assert p['vendedor_id'] == '111' and p['vendedor_nombre'] is None


class TestLaColaNoEsperaASiesa:

    def test_con_cache_vacio_responde_sin_nombre_y_refresca_aparte(self, app, client, db):
        _pedido(db, 910)
        puede_terminar = threading.Event()
        llamado = threading.Event()

        def _lento(self, codigo=None):
            llamado.set()
            puede_terminar.wait(5)
            return FILAS_VENDEDORES

        from app.services.connekta_gateway import ConnektaGateway
        with patch.object(ConnektaGateway, 'get_vendedor_contacto', _lento):
            p = _cola(app, client, db)['PD910']
            # La respuesta llegó mientras Siesa seguía «respondiendo».
            assert p['vendedor_nombre'] is None
            assert llamado.wait(5), 'el refresco en segundo plano no arrancó'
            puede_terminar.set()
            for _ in range(50):
                if V.nombres_por_nit():
                    break
                threading.Event().wait(0.05)
        assert V.nombres_por_nit().get('53051164') == 'ANA PEREZ'

    def test_un_refresco_fallido_no_borra_lo_conocido(self):
        with _refresco_sincrono(FILAS_VENDEDORES):
            V._refrescar()
        with _refresco_sincrono([]):
            V._refrescar()
        assert V._por_nit.get('53051164') == 'ANA PEREZ'

    def test_tras_un_fallo_no_reintenta_en_cada_carga(self):
        with _refresco_sincrono([]):
            V._refrescar()
        with patch.object(V.threading, 'Thread') as hilo:
            V.nombres_por_nit()
        hilo.assert_not_called()


class TestUnaSolaReglaDelNombre:

    @staticmethod
    def _lecturas_de_apellido(arbol):
        """Llamadas `x.get('f200_apellido1', …)` — armar el nombre a mano."""
        import ast
        return [n for n in ast.walk(arbol)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get' and n.args
                and isinstance(n.args[0], ast.Constant) and n.args[0].value == 'f200_apellido1']

    def test_nadie_fuera_de_vendedores_arma_el_nombre(self):
        import ast
        from pathlib import Path
        raiz = Path(__file__).resolve().parent.parent / 'app'
        propio = raiz / 'services' / 'vendedores.py'
        infractores = [str(p.relative_to(raiz)) for p in raiz.rglob('*.py')
                       if p != propio and self._lecturas_de_apellido(
                           ast.parse(p.read_text(encoding='utf-8')))]
        assert infractores == [], (
            f'{infractores} arman el nombre del vendedor a mano: usar '
            'vendedores.nombre_de_fila')

    def test_el_detector_ve_la_forma_y_no_marca_lo_demas(self):
        import ast
        assert self._lecturas_de_apellido(ast.parse("v.get('f200_apellido1', '')"))
        assert not self._lecturas_de_apellido(ast.parse("v.get('f200_nombres', '')"))
        assert not self._lecturas_de_apellido(ast.parse('"""f200_apellido1 en un docstring"""'))
        # Piso: el propio módulo de la regla sí la contiene.
        propio = V.__file__
        assert self._lecturas_de_apellido(ast.parse(open(propio, encoding='utf-8').read()))

    def test_normalizar_nit(self):
        assert V.normalizar_nit(' 53051164 ') == '53051164'
        assert V.normalizar_nit('53051164-1') == '53051164'
        assert V.normalizar_nit('') is None and V.normalizar_nit(None) is None
