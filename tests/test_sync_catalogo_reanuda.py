"""
El sync de catálogo declaraba que se cortaba — y la siguiente corrida volvía a la página 1.

Medido en producción el 2026-09-25 (`registros_sync`, tipo `catalogo`): cada
corrida duraba 5:00 exactos, se cortaba entre la página 151 y la 228 con
`paginacion_completa: false`, y la siguiente arrancaba otra vez por la 1. La
cola del catálogo no se leía nunca: **3.231 productos con el nombre de relleno
`Producto <referencia>`**, entre ellos el S2209346 que salió en picking.

`tests/test_sync_catalogo_completitud.py` prueba que el corte se **declara**.
Este prueba que se **continúa**: una corrida truncada deja `reanudar_en`, la
siguiente arranca ahí, y una que llega al final devuelve el cursor a la 1.

Se mockea solo la frontera HTTP (`connekta.get_items_catalogo`) y se registra
qué páginas pidió el servicio: el cursor se mide por lo que el sync le pregunta
a Siesa, no por lo que el sync dice de sí mismo.
"""
import time
import unittest.mock as mock

import pytest

from tests.test_sync_catalogo_completitud import _fila, _pagina_llena


def _correr(app, paginas, falla_en=None, demora_por_pagina=0.0):
    """Corre `_run_sync` y devuelve `(resultado, páginas pedidas en orden)`."""
    from app.services import siesa_sync_service as svc
    pedidas = []

    def fake_get_items(pagina=1):
        pedidas.append(pagina)
        if demora_por_pagina:
            time.sleep(demora_por_pagina)
        if falla_en is not None and pagina == falla_en:
            raise RuntimeError('Connekta 429 Too Many Requests')
        return {'detalle': {'Table': paginas.get(pagina, [])}}

    with mock.patch.object(svc.connekta, 'get_items_catalogo',
                           side_effect=fake_get_items):
        svc._run_sync(app)
    return svc._sync_estado['ultimo_resultado'], pedidas


def _catalogo(n_llenas, cola=('COLA-1',)):
    """`n_llenas` páginas de 100 y una última corta con las referencias de `cola`."""
    paginas = {n: _pagina_llena(f'P{n}') for n in range(1, n_llenas + 1)}
    paginas[n_llenas + 1] = [_fila(ref) for ref in cola]
    return paginas


def _registrar(db, ok, resultado=None):
    """Una fila de `registros_sync` de tipo catálogo, cerrada, como la dejaría `_cerrar`."""
    import json
    from datetime import datetime
    from app.models.registro_sync import RegistroSync
    r = RegistroSync(tipo='catalogo', inicio=datetime.utcnow(),
                     fin=datetime.utcnow(), ok=ok,
                     resultado=json.dumps(resultado) if resultado is not None else None,
                     error=None if ok else 'reventó')
    db.session.add(r)
    db.session.commit()
    time.sleep(0.01)  # `inicio` distinto: `ultimo_ok` ordena por él
    return r


@pytest.fixture(autouse=True)
def _entorno_del_sync(monkeypatch):
    from app.services import siesa_sync_service as svc
    monkeypatch.setenv('SYNC_PAGE_DELAY_S', '0')
    limpio = {'en_curso': False, 'ultimo_inicio': None,
              'ultimo_resultado': None, 'ultimo_error': None}
    svc._sync_estado.update(limpio)
    yield
    svc._sync_estado.update(limpio)


# ---------------------------------------------------------------------------
# 1 · La vuelta continúa donde se cortó
# ---------------------------------------------------------------------------

class TestLaSiguienteCorridaReanuda:

    def test_la_corrida_truncada_deja_el_cursor_en_la_primera_pagina_sin_leer(self, app, db):
        resultado, pedidas = _correr(app, _catalogo(4), falla_en=3)

        assert pedidas == [1, 2, 3]
        assert resultado['paginacion_completa'] is False
        assert resultado['reanudar_en'] == 3, (
            'la página 3 falló y no quedó escrita: es la que hay que reintentar')
        assert resultado['pagina_inicial'] == 1

    def test_la_siguiente_arranca_en_el_cursor_y_no_en_la_1(self, app, db):
        """**El defecto.** Sin cursor la segunda corrida vuelve a pedir la 1."""
        paginas = _catalogo(4)
        _correr(app, paginas, falla_en=3)

        resultado, pedidas = _correr(app, paginas)

        assert pedidas[0] == 3, (
            f'la corrida siguiente pidió primero la página {pedidas[0]}: volvió '
            f'a empezar y la cola del catálogo no se lee nunca')
        assert pedidas == [3, 4, 5]
        assert resultado['pagina_inicial'] == 3
        assert resultado['paginacion_completa'] is True
        assert resultado['reanudar_en'] is None

    def test_al_llegar_al_final_la_vuelta_siguiente_empieza_en_la_1(self, app, db):
        """El otro sentido: un cursor que nunca vuelve a la 1 deja sin releer la cabeza."""
        paginas = _catalogo(4)
        _correr(app, paginas, falla_en=3)
        _correr(app, paginas)

        _, pedidas = _correr(app, paginas)

        assert pedidas[0] == 1

    def test_el_producto_de_la_cola_recupera_su_nombre(self, app, db):
        """El caso de producción: nombre de relleno en un ítem que está después del corte."""
        from app.models.producto import Producto
        db.session.add(Producto(codigo='S2209346', codigo_siesa='S2209346',
                                nombre='Producto S2209346', activo=True))
        db.session.commit()
        paginas = _catalogo(3, cola=('S2209346',))
        paginas[4][0]['f120_descripcion'] = 'COLOR PAPER MATE DP 12X24 3MM'

        _correr(app, paginas, falla_en=2)
        assert Producto.query.filter_by(codigo_siesa='S2209346').one().nombre == \
            'Producto S2209346'

        _correr(app, paginas)

        assert Producto.query.filter_by(codigo_siesa='S2209346').one().nombre == \
            'COLOR PAPER MATE DP 12X24 3MM'

    def test_la_cota_temporal_tambien_deja_cursor(self, app, db, monkeypatch):
        """La salida corta que se ve en producción: el tiempo, no una página caída."""
        from app.services import siesa_sync_service as svc
        monkeypatch.setattr(svc, '_MAX_MINUTOS_PAGINACION', 0.2 / 60)
        paginas = {n: _pagina_llena(f'P{n}') for n in range(1, 11)}

        resultado, pedidas = _correr(app, paginas, demora_por_pagina=0.15)

        assert 'cota temporal' in resultado['motivo_incompleta']
        assert resultado['reanudar_en'] == pedidas[-1] + 1, (
            'el cursor tiene que apuntar a la primera página que no se pidió')

        monkeypatch.setattr(svc, '_MAX_MINUTOS_PAGINACION', 5)
        _, siguientes = _correr(app, paginas)
        assert siguientes[0] == pedidas[-1] + 1

    def test_el_tope_de_paginas_cuenta_desde_el_cursor(self, app, db, monkeypatch):
        """Un tope absoluto con el cursor pasado de él daría un rango vacío para siempre."""
        from app.services import siesa_sync_service as svc
        monkeypatch.setattr(svc, '_MAX_PAGINAS', 2)
        _registrar(db, True, {'reanudar_en': 5})
        paginas = {n: _pagina_llena(f'P{n}') for n in range(1, 10)}

        resultado, pedidas = _correr(app, paginas)

        assert pedidas == [5, 6]
        assert 'tope' in resultado['motivo_incompleta']
        assert resultado['reanudar_en'] == 7

    def test_un_catalogo_que_se_encogio_bajo_el_cursor_cierra_la_vuelta(self, app, db):
        """Cursor en la 9, catálogo de 2 páginas: la 9 viene vacía y es el final."""
        _registrar(db, True, {'reanudar_en': 9})

        resultado, pedidas = _correr(app, _catalogo(1))

        assert pedidas == [9]
        assert resultado['paginacion_completa'] is True
        assert resultado['reanudar_en'] is None


# ---------------------------------------------------------------------------
# 2 · De dónde sale el cursor
# ---------------------------------------------------------------------------

class TestPaginaDeArranque:

    def test_sin_corridas_previas_arranca_en_la_1(self, app, db):
        from app.services.siesa_sync_service import _pagina_de_arranque
        assert _pagina_de_arranque() == 1

    def test_una_corrida_anterior_al_cursor_arranca_en_la_1(self, app, db):
        """Las filas de producción de antes del arreglo no traen la clave."""
        from app.services.siesa_sync_service import _pagina_de_arranque
        _registrar(db, True, {'paginacion_completa': False, 'paginas_leidas': 180})
        assert _pagina_de_arranque() == 1

    def test_una_corrida_fallida_no_borra_el_cursor_de_la_anterior(self, app, db):
        """La que murió por excepción no deja `resultado`: se usa la última exitosa."""
        from app.services.siesa_sync_service import _pagina_de_arranque
        _registrar(db, True, {'reanudar_en': 181})
        _registrar(db, False)
        assert _pagina_de_arranque() == 181

    @pytest.mark.parametrize('cursor', [None, 0, -3, '181', True, 2.5])
    def test_un_cursor_que_no_es_pagina_arranca_en_la_1(self, app, db, cursor):
        from app.services.siesa_sync_service import _pagina_de_arranque
        _registrar(db, True, {'reanudar_en': cursor})
        assert _pagina_de_arranque() == 1

    def test_si_no_se_puede_leer_la_tabla_arranca_en_la_1(self, app, db):
        from app.services import registro_sync_service as reg
        from app.services.siesa_sync_service import _pagina_de_arranque
        with mock.patch.object(reg, 'ultimo_ok',
                               return_value={'_error_lectura': 'conexión cerrada'}):
            assert _pagina_de_arranque() == 1
