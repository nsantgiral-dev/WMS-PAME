"""
El sync de catálogo no declaraba si había terminado — y no podía terminar.

Medido con Postgres real, catálogo de 28.000 ítems, latencia HTTP 0 y
`SYNC_PAGE_DELAY_S=0`: **138 páginas de 280 en 307 s**, y la corrida abortó por
la cota temporal de 5 min. En producción el delay entre páginas rige por su
default (0,5 s × 280 = +140 s) y la latencia real de Connekta suma otros 56 a
140 s: **la corrida real aborta siempre, hacia la página 210-225, con el 75-80%
del catálogo**.

Y el `resultado` no traía **ningún campo de completitud**: `total_procesados`,
`creados`, `errores` y `sin_estado_en_origen` describen lo que se leyó, ninguno
dice cuánto quedó sin leer. `/api/siesa/sync-estado` publicaba una corrida
truncada como exitosa — el panel de sincronizadores la pinta verde.

Es el hallazgo K por tercera vez. En `pedidos_sync_service` el barrido
incompleto ya se declara (`paginacion_completa` + `motivo_incompleta`), y
`tests/test_oc_paginacion.py` existe para prohibirlo en las OC. Acá reapareció.

## Las tres causas de un barrido incompleto son tres, no una

«Se agotaron las páginas» (completo), «se acabó el tiempo» (incompleto) y «la
API falló» (incompleto) devolvían todas lo mismo: un `resultado` sin campo, o
—en el caso de la excepción— ningún `resultado` y un `ultimo_error` que no
menciona que 13.800 productos SÍ se escribieron.

La tercera vía era la peor y estaba escrita como una optimización:

    if not rows or (len(rows) == 1 and 'alerta' in (rows[0] or {})): break

Una fila `{'alerta': ...}` es Siesa **rechazando la consulta** con HTTP 200
(`ConnektaConsultaRechazada`, documentado en `connekta_gateway`). El `break` la
leía como fin de catálogo: «tu consulta fue rechazada» se convertía en «no hay
más productos», y el sync reportaba éxito con lo que hubiera alcanzado.

## Por qué el N+1 es parte del mismo defecto

Declarar la incompletitud sin arreglar el costo daría un sync que declara,
correctamente, que nunca termina. `Producto.query.filter_by(...).first()` dos
veces por ítem son 100-200 SELECT por página — 2,5 sentencias SQL por ítem,
2,2 s por página de puro trabajo de base. `TestElCostoEnSQLNoCreceConLosItems`
mide la propiedad que importa: **el costo por página no puede depender de
cuántos ítems trae la página.**

## La mina del estado vacío

`trae_estado = 'f120_ind_estado' in row` pregunta si la **clave** llegó, no si
llegó un **dato**. Con `''` o `None` la clave está, `str(...) == '1'` es
`False`, y el update escribe `prod.activo = False` **en todas las filas**: el
catálogo entero desactivado, sin un solo error. «La clave llegó vacía» es el
mismo hueco de la Regla 0 con otra forma.
"""
import time
import unittest.mock as mock
from contextlib import contextmanager

import pytest
from sqlalchemy import event


# ---------------------------------------------------------------------------
# Arnés
# ---------------------------------------------------------------------------

def _fila(referencia, **extra):
    """Una fila como las que manda Siesa: los campos del contrato y nada más."""
    fila = {
        'f120_id_cia': 1,
        'f120_referencia': referencia,
        'f120_descripcion': f'Descripción de {referencia}',
        'f120_id_tipo_inv_serv': 'INV',
        'f120_id_unidad_inventario': 'UND',
        'f120_id_unidad_empaque': 'UND',
        'f120_ind_tipo_item': 1,
    }
    fila.update(extra)
    return fila


def _pagina_llena(prefijo):
    """100 filas — el tamaño que hace que la paginación pida otra página."""
    return [_fila(f'{prefijo}-{i:03d}') for i in range(100)]


def _correr(app, paginas, falla_en=None, demora_por_pagina=0.0):
    """Corre `_run_sync` sirviendo `paginas` y devuelve el `resultado`.

    Se llama al servicio real y se mockea solo la frontera HTTP: un arnés que
    escribiera `Producto(...)` a mano probaría que la base acepta esas filas,
    no que el sync las escribe.
    """
    from app.services import siesa_sync_service as svc

    def fake_get_items(pagina=1):
        if demora_por_pagina:
            time.sleep(demora_por_pagina)
        if falla_en is not None and pagina == falla_en:
            raise RuntimeError('Connekta 429 Too Many Requests')
        return {'detalle': {'Table': paginas.get(pagina, [])}}

    with mock.patch.object(svc.connekta, 'get_items_catalogo',
                           side_effect=fake_get_items):
        svc._run_sync(app)
    return svc._sync_estado['ultimo_resultado']


@contextmanager
def _contar_selects_a_productos(db):
    """Cuenta los SELECT contra `productos` que emite el bloque envuelto.

    Instrumenta el engine, no el servicio: un contador que el propio código
    incrementara mediría lo que el código cree que hace.
    """
    vistos = []

    def _antes(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip()[:6].upper() == 'SELECT' and 'FROM productos' in statement:
            vistos.append(statement)

    event.listen(db.engine, 'before_cursor_execute', _antes)
    try:
        yield vistos
    finally:
        event.remove(db.engine, 'before_cursor_execute', _antes)


@pytest.fixture(autouse=True)
def _entorno_del_sync(monkeypatch):
    """El estado del sync es un dict de módulo — un test no puede leer el de otro.

    Y el throttle entre páginas se apaga: acá se mide la cota temporal contra
    el tiempo que tarda la fuente, no contra un `sleep` fijo.
    """
    from app.services import siesa_sync_service as svc
    monkeypatch.setenv('SYNC_PAGE_DELAY_S', '0')
    limpio = {'en_curso': False, 'ultimo_inicio': None,
              'ultimo_resultado': None, 'ultimo_error': None}
    svc._sync_estado.update(limpio)
    yield
    svc._sync_estado.update(limpio)


# ---------------------------------------------------------------------------
# 1 · La completitud se declara
# ---------------------------------------------------------------------------

class TestElBarridoDeclaraSiTermino:
    """`resultado` tiene que contestar «¿se leyó todo?» y, si no, por qué."""

    def test_un_catalogo_que_cabe_declara_completa(self, app, db):
        """El otro sentido del detector: la corrida sana dice que sí terminó.

        Sin esto, un `paginacion_completa` cableado en `False` pasaría todos
        los tests de truncamiento y no serviría para nada.
        """
        resultado = _correr(app, {1: [_fila('REF-A'), _fila('REF-B')]})

        assert resultado['paginacion_completa'] is True
        assert resultado['motivo_incompleta'] is None
        assert resultado['total_procesados'] == 2

    def test_dos_paginas_la_segunda_corta_tambien_es_completa(self, app, db):
        resultado = _correr(app, {1: _pagina_llena('P1'), 2: [_fila('REF-Z')]})

        assert resultado['paginacion_completa'] is True
        assert resultado['total_procesados'] == 101

    def test_cero_items_es_un_resultado_legitimo_no_un_truncamiento(self, app, db):
        """Un catálogo vacío se leyó entero. Cero no es «no sé»."""
        resultado = _correr(app, {1: []})

        assert resultado['total_procesados'] == 0
        assert resultado['paginacion_completa'] is True, (
            'un catálogo vacío quedó marcado como truncado — el detector '
            'dispara sobre operación sana')

    def test_la_cota_temporal_declara_el_truncamiento(self, app, db, monkeypatch):
        """**El detector ciego.** La fuente sirve más páginas de las que caben.

        Es la corrida real: 280 páginas, presupuesto de 5 min, aborto hacia la
        210. Lo que se publicaba era `total_procesados: 22.000` sin una sola
        pista de que faltaba el resto.
        """
        from app.services import siesa_sync_service as svc
        monkeypatch.setattr(svc, '_MAX_MINUTOS_PAGINACION', 0.2 / 60, raising=False)

        paginas = {n: _pagina_llena(f'P{n}') for n in range(1, 6)}
        resultado = _correr(app, paginas, demora_por_pagina=0.15)

        assert resultado is not None, 'la corrida no dejó resultado'
        assert resultado['paginacion_completa'] is False, (
            f"leyó {resultado['total_procesados']} de 500 ítems y lo publicó "
            f"como corrida exitosa — el tope aborta el barrido y el resultado "
            f"no lo dice")
        assert 'cota temporal' in (resultado['motivo_incompleta'] or ''), (
            'no se distingue «se acabó el tiempo» de «no hay más páginas»')
        assert 0 < resultado['total_procesados'] < 500

    def test_una_pagina_que_falla_declara_incompleto_y_conserva_lo_leido(self, app, db):
        """La API falla en la página 2: lo de la página 1 ya está escrito.

        Antes la excepción salía por el `except` general: rollback, `resultado`
        en `None` y un `ultimo_error` que no menciona que los productos de las
        páginas anteriores SÍ quedaron commiteados. «Falló» y «se leyó el 40%»
        son afirmaciones distintas.
        """
        from app.models.producto import Producto

        paginas = {1: _pagina_llena('OK'), 2: _pagina_llena('NUNCA')}
        resultado = _correr(app, paginas, falla_en=2)

        assert resultado is not None, (
            'la corrida no dejó resultado: los 100 productos de la página 1 '
            'están en la base y nadie lo declara')
        assert resultado['paginacion_completa'] is False
        assert '429' in (resultado['motivo_incompleta'] or ''), (
            'el motivo no dice qué falló')
        assert resultado['total_procesados'] == 100
        assert Producto.query.filter_by(codigo_siesa='OK-000').first() is not None

    def test_una_alerta_de_siesa_no_se_lee_como_fin_de_catalogo(self, app, db):
        """`{'alerta': ...}` es un rechazo con HTTP 200, no una página vacía.

        El `break` lo trataba como fin de catálogo: la consulta rebotó y el
        sync reportó éxito. Es el modo de fallo que `ConnektaConsultaRechazada`
        existe para impedir, escrito a mano en este archivo.
        """
        paginas = {1: _pagina_llena('P1'),
                   2: [{'alerta': 'Por favor verifique los parámetros o '
                                  'filtros enviados en la petición.'}]}
        resultado = _correr(app, paginas)

        assert resultado['paginacion_completa'] is False, (
            'Siesa rechazó la consulta y la corrida se declaró completa — '
            '«tu consulta fue rechazada» se convirtió en «no hay más»')
        assert 'alerta' in (resultado['motivo_incompleta'] or '').lower() or \
               'rechaz' in (resultado['motivo_incompleta'] or '').lower()

    def test_el_tope_de_paginas_agotado_tambien_se_declara(self, app, db, monkeypatch):
        """El tercer camino a un barrido corto: se acabaron las páginas.

        `range(1, 501)` con todas las páginas llenas no significa «se terminó»,
        significa «hay más del otro lado» — la misma lectura que
        `get_ordenes_compra_aprobadas` ya hace en su `else` del `for`.
        """
        from app.services import siesa_sync_service as svc
        monkeypatch.setattr(svc, '_MAX_PAGINAS', 2, raising=False)

        paginas = {n: _pagina_llena(f'P{n}') for n in range(1, 5)}
        resultado = _correr(app, paginas)

        assert resultado['paginacion_completa'] is False
        assert 'páginas' in (resultado['motivo_incompleta'] or '')

    def test_un_barrido_incompleto_no_se_pinta_verde_en_el_panel(self, app, db, monkeypatch):
        """`app.js` pinta «ok» cuando `ultimo_error` viene vacío.

        El panel de sincronizadores no lee `ultimo_resultado`: mira
        `ultimo_error` y con eso decide verde/rojo. Un barrido del 75% con
        `ultimo_error: null` es la corrida truncada publicada como exitosa,
        que es el defecto entero visto desde la pantalla.
        """
        from app.services import siesa_sync_service as svc
        monkeypatch.setattr(svc, '_MAX_PAGINAS', 1, raising=False)

        _correr(app, {1: _pagina_llena('P1'), 2: _pagina_llena('P2')})

        assert svc.estado_sync()['ultimo_error'], (
            'la corrida quedó incompleta y el panel la pinta verde')

    def test_una_corrida_completa_no_deja_error(self, app, db):
        """El otro sentido: no se inventa un error donde no lo hubo."""
        from app.services import siesa_sync_service as svc

        _correr(app, {1: [_fila('REF-OK')]})

        assert svc.estado_sync()['ultimo_error'] is None


# ---------------------------------------------------------------------------
# 2 · El N+1
# ---------------------------------------------------------------------------

class TestElCostoEnSQLNoCreceConLosItems:
    """2,5 sentencias SQL por ítem × 28.000 ítems = la cota temporal."""

    def test_una_pagina_de_50_no_cuesta_mas_sql_que_una_de_5(self, app, db):
        with _contar_selects_a_productos(db) as pocos:
            _correr(app, {1: [_fila(f'CH-{i:03d}') for i in range(5)]})
        with _contar_selects_a_productos(db) as muchos:
            _correr(app, {1: [_fila(f'GR-{i:03d}') for i in range(50)]})

        assert len(muchos) == len(pocos), (
            f'{len(pocos)} SELECT para 5 ítems y {len(muchos)} para 50: el '
            f'costo de una página crece con el número de ítems. Son 100-200 '
            f'SELECT por página sobre un catálogo de 280 páginas')
        assert len(muchos) <= 4, (
            f'{len(muchos)} SELECT para una página — debería alcanzar con uno '
            f'por codigo_siesa y otro por codigo')

    def test_actualizar_cuesta_lo_mismo_que_crear(self, app, db):
        """La segunda corrida encuentra todo hecho: tampoco puede ir por ítem."""
        filas = [_fila(f'UP-{i:03d}') for i in range(50)]
        _correr(app, {1: filas})

        with _contar_selects_a_productos(db) as segunda:
            _correr(app, {1: filas})

        assert len(segunda) <= 4, (
            f'{len(segunda)} SELECT para revisitar 50 productos existentes')

    def test_el_contador_ve_crecer_un_N_mas_1(self, app, db):
        """Canario del instrumento, en el otro sentido.

        Si el contador no viera crecer un N+1 de manual, los dos tests de
        arriba estarían midiendo el vacío y pasarían con cualquier código.
        """
        from app.models.producto import Producto

        with _contar_selects_a_productos(db) as pocos:
            for i in range(5):
                Producto.query.filter_by(codigo=f'X-{i}').first()
        with _contar_selects_a_productos(db) as muchos:
            for i in range(50):
                Producto.query.filter_by(codigo=f'X-{i}').first()

        assert len(pocos) == 5 and len(muchos) == 50, (
            'el contador no ve las consultas — los tests de costo no miden nada')


class TestLosProductosSeEscribenIgualQueAntes:
    """Matar el N+1 no puede cambiar qué producto se encuentra ni qué se escribe."""

    def test_un_alta_mapea_los_mismos_campos(self, app, db):
        from app.models.producto import Producto
        from app.models.siesa_mapeo_unidades import SiesaMapeoUnidades

        db.session.add(SiesaMapeoUnidades(tipo_inv_siesa='INV',
                                          unidad_negocio_id='001',
                                          descripcion='Papelería'))
        db.session.commit()

        resultado = _correr(app, {1: [_fila(
            'REF-FULL',
            f120_descripcion='Cuaderno cosido 100 hojas',
            f120_id_unidad_inventario='CJA',
            f120_id_unidad_empaque='CJA',
            f120_codigo_barras='7701234567890',
            f421_factor='12',
        )]})

        prod = Producto.query.filter_by(codigo_siesa='REF-FULL').first()
        assert (prod.codigo, prod.nombre, prod.unidad_medida) == (
            'REF-FULL', 'Cuaderno cosido 100 hojas', 'CJA')
        assert prod.unidad_negocio_id == '001'
        assert prod.codigo_barras == '7701234567890'
        assert prod.unidad_empaque == 'CJA'
        assert prod.factor_conversion == 12
        assert prod.activo is True
        assert (resultado['creados'], resultado['actualizados']) == (1, 0)

    def test_un_update_sigue_contando_como_actualizado(self, app, db):
        from app.models.producto import Producto

        db.session.add(Producto(codigo='REF-UPD', nombre='Nombre viejo',
                                codigo_siesa='REF-UPD', activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        resultado = _correr(app, {1: [_fila('REF-UPD',
                                            f120_descripcion='Nombre nuevo')]})

        assert Producto.query.filter_by(codigo_siesa='REF-UPD').first().nombre == 'Nombre nuevo'
        assert (resultado['creados'], resultado['actualizados']) == (0, 1)

    def test_se_encuentra_por_codigo_cuando_no_hay_codigo_siesa(self, app, db):
        """La segunda mitad del `or`: producto viejo sin `codigo_siesa`."""
        from app.models.producto import Producto

        db.session.add(Producto(codigo='LEGACY-1', nombre='Sin código Siesa',
                                codigo_siesa=None, activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        resultado = _correr(app, {1: [_fila('LEGACY-1',
                                            f120_descripcion='Ya con Siesa')]})

        assert resultado['creados'] == 0, 'creó un duplicado en vez de encontrarlo'
        prod = Producto.query.filter_by(codigo='LEGACY-1').first()
        assert prod.codigo_siesa == 'LEGACY-1'
        assert prod.nombre == 'Ya con Siesa'

    def test_gana_la_coincidencia_por_codigo_siesa_sobre_la_de_codigo(self, app, db):
        """El `or` tiene un orden y hay que preservarlo.

        Un producto puede coincidir por los dos lados con **filas distintas**.
        Antes ganaba siempre `codigo_siesa`; si el mapa precargado invirtiera
        la precedencia, el sync escribiría sobre el producto equivocado y nada
        fallaría.
        """
        from app.models.producto import Producto

        db.session.add(Producto(codigo='OTRO-COD', nombre='Gana este',
                                codigo_siesa='DOBLE', activo=True,
                                unidad_medida='UND'))
        db.session.add(Producto(codigo='DOBLE', nombre='No se toca',
                                codigo_siesa=None, activo=True,
                                unidad_medida='UND'))
        db.session.commit()

        _correr(app, {1: [_fila('DOBLE', f120_descripcion='Nombre nuevo')]})

        assert Producto.query.filter_by(codigo='OTRO-COD').first().nombre == 'Nombre nuevo'
        assert Producto.query.filter_by(codigo='DOBLE').first().nombre == 'No se toca'

    def test_dos_filas_de_la_misma_pagina_al_mismo_producto(self, app, db):
        """El mapa precargado tiene que ver las escrituras intermedias.

        Con una foto tomada al empezar la página, la segunda fila no encuentra
        el producto que creó la primera y lo crea de nuevo: `codigo` es UNIQUE,
        así que el `commit()` de la página revienta y se pierde la página
        entera — 100 productos por una fila repetida.
        """
        from app.models.producto import Producto

        resultado = _correr(app, {1: [
            _fila('REPE', f120_descripcion='Primera'),
            _fila('REPE', f120_descripcion='Segunda'),
        ]})

        assert Producto.query.filter_by(codigo='REPE').count() == 1
        assert Producto.query.filter_by(codigo='REPE').first().nombre == 'Segunda'
        assert resultado['creados'] == 1
        assert resultado['errores'] == 0

    def test_una_fila_sin_referencia_se_sigue_omitiendo(self, app, db):
        from app.models.producto import Producto

        antes = Producto.query.count()
        resultado = _correr(app, {1: [_fila(''), _fila('CON-REF')]})

        assert Producto.query.count() == antes + 1
        assert resultado['total_procesados'] == 1


# ---------------------------------------------------------------------------
# 3 · La mina del estado vacío
# ---------------------------------------------------------------------------

class TestLaClaveVaciaNoEsUnDato:
    """`'f120_ind_estado' in row` pregunta por la clave, no por el dato."""

    def _con_dos_activos(self, db):
        from app.models.producto import Producto
        db.session.add(Producto(codigo='EST-1', nombre='Uno', codigo_siesa='EST-1',
                                activo=True, unidad_medida='UND'))
        db.session.add(Producto(codigo='EST-2', nombre='Dos', codigo_siesa='EST-2',
                                activo=True, unidad_medida='UND'))
        db.session.commit()

    def test_una_clave_vacia_no_desactiva_el_catalogo(self, app, db):
        """El detector ciego: `''` desactivaba **todas** las filas.

        Un conector que mande la clave con el valor vacío —o Siesa mismo, el
        día que agregue el campo— desactiva el catálogo entero en una corrida,
        sin un error, sin un aviso, y el sync reporta éxito. Todo lo
        desactivado desaparece del picking.
        """
        from app.models.producto import Producto
        self._con_dos_activos(db)

        _correr(app, {1: [_fila('EST-1', f120_ind_estado=''),
                          _fila('EST-2', f120_ind_estado='')]})

        activos = [p.activo for p in Producto.query.filter(
            Producto.codigo.in_(['EST-1', 'EST-2'])).all()]
        assert activos == [True, True], (
            'la clave llegó vacía y el sync desactivó el catálogo: «vacío» no '
            'es «dado de baja», es «la fuente no dijo nada» (Regla 0)')

    def test_una_clave_en_none_tampoco_desactiva(self, app, db):
        from app.models.producto import Producto
        self._con_dos_activos(db)

        _correr(app, {1: [_fila('EST-1', f120_ind_estado=None)]})

        assert Producto.query.filter_by(codigo='EST-1').first().activo is True

    def test_una_clave_en_blancos_tampoco(self, app, db):
        from app.models.producto import Producto
        self._con_dos_activos(db)

        _correr(app, {1: [_fila('EST-1', f120_ind_estado='   ')]})

        assert Producto.query.filter_by(codigo='EST-1').first().activo is True

    def test_la_clave_vacia_cuenta_como_fila_sin_estado(self, app, db):
        """Y queda contada donde ya se cuentan las que no traen la clave.

        Si no se contara, `sin_estado_en_origen: 0` diría «la fuente sí manda
        estado» sobre una corrida en la que no mandó ninguno.
        """
        resultado = _correr(app, {1: [_fila('S-1', f120_ind_estado=''),
                                      _fila('S-2'),
                                      _fila('S-3', f120_ind_estado='1')]})

        assert resultado['sin_estado_en_origen'] == 2
        assert resultado['total_procesados'] == 3

    def test_un_estado_real_sigue_mandando(self, app, db):
        """El otro sentido: dato presente no es dato ausente.

        `'0'` es una baja de verdad y tiene que seguir desactivando — si no,
        el arreglo del vacío se habría comido el campo entero.
        """
        from app.models.producto import Producto
        self._con_dos_activos(db)

        _correr(app, {1: [_fila('EST-1', f120_ind_estado='0'),
                          _fila('EST-2', f120_ind_estado='1')]})

        assert Producto.query.filter_by(codigo='EST-1').first().activo is False
        assert Producto.query.filter_by(codigo='EST-2').first().activo is True
