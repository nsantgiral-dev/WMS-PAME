"""
Existencias verdaderas: `stock_siesa` baja a cero lo que Siesa ya no tiene
(P0-1 de la auditoría de compras, 2026-09-27).

## La clase

*Un hueco de lectura se rellena con el valor viejo y se re-sella.* La consulta
de existencias (`papeleriamedellin_WMS_Stock_Bodega_v2`) solo trae filas con
existencia > 0. El WMS la leía con `tamPag=1000` (Regla 10), cuya paginación
no era determinista, y para no perder filas **mezclaba lo leído con lo
guardado** en `stock_siesa`. Un SKU que se agota desaparece de la respuesta y
la mezcla lo rellenaba con su último positivo, sellado «de hoy». Medido en
producción el 2026-09-27: 17.971 filas (31 %), 3,8 M de unidades que Siesa ya
no tenía; 6 de 6 verificadas en 0 contra InvFecha. La misma mezcla alimentaba
la carga física de las 7:00 (el picker iba a un hueco vacío), la
reconciliación, los traslados, el Armador y la temporada.

## Ahora

Una lectura de a 100 con su prueba de completitud (`total_registros` = N,
`LineaRegistro` 1…N, sin repetidos). **Solo con la lectura completa** lo que
no vino queda en cero (upsert a 0 con `updated_at` y `ausente_desde`, nunca
un borrado). Incompleta: no se escribe nada, el caché queda degradado y el
sello no avanza. La carga física lee la lectura, no una mezcla.

## Los trinquetes (AST)

1. Toda llamada a `_guardar_stock_en_bd` le pasa una lectura de Siesa
   (`_descargar_una_pasada_custom()`), no un diccionario armado.
2. Nadie escribe `stock_siesa` fuera de `_guardar_stock_en_bd`, y esa función
   pregunta `lectura_completa` antes.
3. `_leer_stock_de_bd` (lo guardado) solo se llama desde funciones que no
   persisten, o dentro de la rama degradada.
"""
import ast
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import inventario_siesa_service as iss

RAIZ = pathlib.Path(__file__).resolve().parents[1]


# ═════════════════════════════════════════════════════════════════════════════
# Siesa falsa con la forma REAL del endpoint dinámico
# ═════════════════════════════════════════════════════════════════════════════

def fila(bodega, ref, existencia=5.0, comprometido=0.0, salida=0.0):
    return {'f150_id': bodega, 'f120_referencia': ref,
            'f400_cant_existencia_1': existencia,
            'f400_cant_comprometida_1': comprometido,
            'f400_cant_salida_sin_conf_1': salida}


class SiesaExistencias:
    """Pagina `filas` como el endpoint dinámico de producción (medido el
    2026-09-27): `detalle = {'tamaño_página', 'página_actual', 'total_páginas',
    'total_registros', 'Datos': [... {'LineaRegistro': k}]}`.

    Perillas para romperla: `falla` (páginas que levantan), `total_en` (el
    total que declara cada página), `repite_linea`, `sin_linea`, `sin_total`,
    `total_final` (el total de la relectura final de la página 1)."""

    def __init__(self, filas, tam=100, falla=(), total_en=None, repite_linea=None,
                 sin_linea=False, sin_total=False, total_final=None, primera_linea=1):
        self.filas = [dict(f, LineaRegistro=i + primera_linea) for i, f in enumerate(filas)]
        self.tam = tam
        self.falla = set(falla)
        self.total_en = total_en or {}
        self.repite_linea = repite_linea
        self.sin_linea = sin_linea
        self.sin_total = sin_total
        self.total_final = total_final
        self.pedidas = []

    def __call__(self, api, params, url=None, timeout=30):
        assert api == iss.CONSULTA_EXISTENCIAS
        pag = int(params['paginacion'].split('numPag=')[1].split('|')[0])
        tam = int(params['paginacion'].split('tamPag=')[1])
        assert tam <= 100, 'Regla 10: tamPag máximo 100'
        self.pedidas.append(pag)
        if pag in self.falla:
            raise RuntimeError('Connekta rate-limit (429)')
        n = len(self.filas)
        datos = [dict(f) for f in self.filas[(pag - 1) * tam: pag * tam]]
        if self.repite_linea and datos and pag == self.repite_linea:
            datos[-1]['LineaRegistro'] = datos[0]['LineaRegistro']
        if self.sin_linea:
            for d in datos:
                d.pop('LineaRegistro', None)
        total = self.total_en.get(pag, n)
        if pag == 1 and self.total_final is not None and self.pedidas.count(1) > 1:
            total = self.total_final
        det = {'tamaño_página': tam, 'página_actual': pag,
               'total_páginas': (n + tam - 1) // tam, 'Datos': datos}
        if not self.sin_total:
            det['total_registros'] = total
        return {'codigo': 0, 'detalle': det}


def universo(n_nb1=80, extra=()):
    """80 SKU en NB1 (pasa el guard de «respuesta sospechosamente pequeña»),
    más filas de bodegas que el WMS no toca (se cuentan para la prueba de
    completitud y no se guardan)."""
    filas = [fila('NB1', f'SKU{i:03d}') for i in range(n_nb1)]
    filas += [fila('BC99', 'CONTRATO1', 9), fila('FD1', 'DUPLICADA1', 4)]
    filas += list(extra)
    return filas


@pytest.fixture
def cache_limpio():
    orig = dict(iss._cache_inventario_multibodega)
    iss._cache_inventario_multibodega.update(data=None, ts=None, degradado=False,
                                             bodegas_frescas=frozenset(),
                                             completa=False, motivo='')
    orig_mono = dict(iss._cache_inventario_siesa)
    iss._cache_inventario_siesa.clear()
    yield iss._cache_inventario_multibodega
    iss._cache_inventario_multibodega.clear()
    iss._cache_inventario_multibodega.update(orig)
    iss._cache_inventario_siesa.clear()
    iss._cache_inventario_siesa.update(orig_mono)


def leer(siesa):
    with patch.object(iss.connekta, '_get', side_effect=siesa), patch('time.sleep'):
        return iss._descargar_una_pasada_custom()


def refrescar(siesa):
    with patch.object(iss.connekta, '_get', side_effect=siesa), patch('time.sleep'):
        return iss._descargar_inventario_siesa_raw(forzar=True)


def sembrar(db, bodega, ref, existencia, dias=10, **kw):
    from app.models.stock_siesa import StockSiesa
    r = StockSiesa(bodega=bodega, codigo_siesa=ref, existencia=existencia,
                   comprometido=kw.get('comprometido', 0), salida_sin_conf=0,
                   updated_at=datetime.utcnow() - timedelta(days=dias),
                   ausente_desde=kw.get('ausente_desde'))
    db.session.add(r)
    db.session.commit()
    return r


def fila_bd(bodega, ref):
    from app.models.stock_siesa import StockSiesa
    from app.extensions import db
    db.session.expire_all()
    return StockSiesa.query.filter_by(bodega=bodega, codigo_siesa=ref).one()


# ═════════════════════════════════════════════════════════════════════════════
# 1 · La prueba de completitud
# ═════════════════════════════════════════════════════════════════════════════

class TestLaLecturaSeProbaCompleta:

    def test_completa_con_la_forma_real(self):
        s = SiesaExistencias(universo())
        r = leer(s)
        assert iss.lectura_completa(r), r.motivo
        assert r.filas == 82 and r.total_declarado == 82
        assert set(r['NB1']) == {f'SKU{i:03d}' for i in range(80)}
        assert 'BC99' not in r and 'FD1' not in r, 'el WMS no guarda esas bodegas'
        # Una lectura completa sin filas de una bodega del universo la declara
        # vacía: es una bodega en cero, no una desconocida.
        assert 'FF1' in r.bodegas_sin_filas and r['FF1'] == {}

    def test_pide_de_a_100_y_relee_la_primera_al_final(self):
        s = SiesaExistencias(universo(250))
        r = leer(s)
        assert iss.lectura_completa(r)
        assert s.pedidas == [1, 2, 3, 1]

    def test_una_pagina_perdida_la_deja_incompleta(self):
        r = leer(SiesaExistencias(universo(250), falla={2}))
        assert not iss.lectura_completa(r)
        assert 'página 2' in r.motivo

    def test_el_total_que_cambia_mientras_se_lee(self):
        r = leer(SiesaExistencias(universo(250), total_en={2: 251}))
        assert not iss.lectura_completa(r) and 'cambió' in r.motivo

    def test_el_total_que_cambio_al_terminar(self):
        """La relectura final de la página 1 ve un cambio posterior a la
        última página."""
        r = leer(SiesaExistencias(universo(250), total_final=251))
        assert not iss.lectura_completa(r) and 'al terminar' in r.motivo

    def test_linea_repetida(self):
        r = leer(SiesaExistencias(universo(250), repite_linea=2))
        assert not iss.lectura_completa(r) and 'dos veces' in r.motivo

    def test_la_numeracion_no_es_1_a_n(self):
        """N filas distintas, sin repetidos, pero numeradas 2…N+1: no es la
        numeración que prueba que llegaron todas."""
        r = leer(SiesaExistencias(universo(), primera_linea=2))
        assert not iss.lectura_completa(r) and 'no es 1' in r.motivo

    def test_misma_bodega_y_referencia_dos_veces(self):
        filas = universo()
        filas.append(fila('NB1', 'SKU000', 3))
        r = leer(SiesaExistencias(filas))
        assert not iss.lectura_completa(r) and 'NB1·SKU000' in r.motivo

    def test_sin_linea_registro_no_se_prueba(self):
        r = leer(SiesaExistencias(universo(), sin_linea=True))
        assert not iss.lectura_completa(r) and 'LineaRegistro' in r.motivo

    def test_sin_total_declarado_no_se_prueba(self):
        r = leer(SiesaExistencias(universo(), sin_total=True))
        assert not iss.lectura_completa(r) and 'total_registros' in r.motivo

    def test_menos_filas_que_las_declaradas(self):
        """Siesa declara 90 y la última página llega corta con 82."""
        r = leer(SiesaExistencias(universo(), total_en={1: 90}, total_final=90))
        assert not iss.lectura_completa(r) and '90' in r.motivo

    def test_tope_de_paginas(self, monkeypatch):
        monkeypatch.setenv('INV_SIESA_MAX_PAGINAS', '2')
        r = leer(SiesaExistencias(universo(250)))
        assert not iss.lectura_completa(r) and 'tope' in r.motivo

    def test_rechazo_alerta(self):
        def _get(api, params, url=None, timeout=30):
            return {'detalle': {'Datos': [{'alerta': 'sin permiso'}]}}
        r = leer(_get)
        assert not iss.lectura_completa(r) and 'rechazó' in r.motivo

    def test_un_dict_suelto_no_es_completo(self):
        assert not iss.lectura_completa({'NB1': {'X': {}}})
        assert not iss.lectura_completa(None)


# ═════════════════════════════════════════════════════════════════════════════
# 2 · Lo que no vino queda en cero, con fecha — solo con lectura completa
# ═════════════════════════════════════════════════════════════════════════════

class TestLoQueNoVinoQuedaEnCero:

    def test_el_agotado_queda_en_cero_con_sello_nuevo(self, db, cache_limpio):
        """El caso de producción: PAPELSP1003 se agota en NB1, Siesa deja de
        reportarlo, el WMS decía 1.508 «de hoy»."""
        sembrar(db, 'NB1', 'PAPELSP1003', 1508, comprometido=3)
        antes = datetime.utcnow()
        refrescar(SiesaExistencias(universo()))
        r = fila_bd('NB1', 'PAPELSP1003')
        assert (r.existencia, r.comprometido, r.salida_sin_conf) == (0, 0, 0)
        assert r.updated_at >= antes and r.ausente_desde >= antes

    def test_lo_reportado_se_escribe(self, db, cache_limpio):
        sembrar(db, 'NB1', 'SKU001', 99)
        refrescar(SiesaExistencias(universo()))
        r = fila_bd('NB1', 'SKU001')
        assert r.existencia == 5 and r.ausente_desde is None

    def test_una_bodega_sin_filas_queda_en_cero(self, db, cache_limpio):
        """FF1: Siesa no trae ninguna fila (feria cerrada). Con la lectura
        completa, sus filas guardadas son fantasmas."""
        sembrar(db, 'FF1', 'PAPELSP20', 117)
        refrescar(SiesaExistencias(universo()))
        assert fila_bd('FF1', 'PAPELSP20').existencia == 0

    def test_nada_se_borra(self, db, cache_limpio):
        from app.models.stock_siesa import StockSiesa
        sembrar(db, 'NB1', 'FANTASMA', 7)
        refrescar(SiesaExistencias(universo()))
        assert StockSiesa.query.filter_by(codigo_siesa='FANTASMA').count() == 1

    def test_ausente_desde_es_la_primera_vez(self, db, cache_limpio):
        primera = datetime.utcnow() - timedelta(days=3)
        sembrar(db, 'NB1', 'FANTASMA', 0, ausente_desde=primera)
        refrescar(SiesaExistencias(universo()))
        r = fila_bd('NB1', 'FANTASMA')
        assert r.ausente_desde == primera
        assert r.updated_at > primera, 'la lectura completa lo confirma hoy'

    def test_lo_que_vuelve_a_venir_se_recupera(self, db, cache_limpio):
        sembrar(db, 'NB1', 'SKU002', 0, ausente_desde=datetime.utcnow() - timedelta(days=2))
        refrescar(SiesaExistencias(universo()))
        r = fila_bd('NB1', 'SKU002')
        assert r.existencia == 5 and r.ausente_desde is None

    def test_las_bodegas_que_no_son_del_universo_no_se_tocan(self, db, cache_limpio):
        sembrar(db, 'BC99', 'VIEJA', 40)
        refrescar(SiesaExistencias(universo()))
        assert fila_bd('BC99', 'VIEJA').existencia == 40

    def test_queda_registrada_con_sus_cifras(self, db, cache_limpio):
        from app.services import registro_sync_service as reg
        sembrar(db, 'NB1', 'FANTASMA', 1508)
        refrescar(SiesaExistencias(universo()))
        u = reg.ultimo('existencias_siesa')
        assert u['ok'] is True
        assert u['resultado']['a_cero'] == 1 and u['resultado']['unidades_a_cero'] == 1508
        assert u['resultado']['total_declarado'] == 82


class TestConLecturaIncompletaNoSeTocaNada:

    def test_pagina_perdida_no_escribe_ni_resella(self, db, cache_limpio):
        from app.services import registro_sync_service as reg
        vieja = sembrar(db, 'NB1', 'FANTASMA', 1508).updated_at
        sembrar(db, 'NB1', 'SKU001', 99)
        refrescar(SiesaExistencias(universo(250), falla={2}))
        r = fila_bd('NB1', 'FANTASMA')
        assert r.existencia == 1508 and r.updated_at == vieja and r.ausente_desde is None
        assert fila_bd('NB1', 'SKU001').existencia == 99, 'ni lo leído se escribe'
        assert cache_limpio['degradado'] and cache_limpio['ts'] is None
        assert 'página 2' in cache_limpio['motivo']
        u = reg.ultimo('existencias_siesa')
        assert u['ok'] is False and 'página 2' in u['error']

    def test_degradado_responde_con_lo_guardado_sin_los_ceros(self, db, cache_limpio):
        sembrar(db, 'NB1', 'VIVO', 4)
        sembrar(db, 'NB1', 'EN-CERO', 0, ausente_desde=datetime.utcnow())
        inv = refrescar(SiesaExistencias(universo(), falla={1}))
        assert 'VIVO' in inv['NB1'] and 'EN-CERO' not in inv['NB1']

    def test_completa_pero_sospechosamente_chica_no_cerea(self, db, cache_limpio):
        """Defensa en profundidad: una lectura completa de 10 SKU en NB1 no pone
        en cero 100 filas."""
        for i in range(100):
            sembrar(db, 'NB1', f'B{i}', 3)
        refrescar(SiesaExistencias([fila('NB1', f'B{i}', 9) for i in range(10)]))
        assert fila_bd('NB1', 'B50').existencia == 3
        assert cache_limpio['degradado'] and 'pequeña' in cache_limpio['motivo']

    def test_guardar_con_un_dict_suelto_no_escribe(self, db):
        from app.models.stock_siesa import StockSiesa
        iss._guardar_stock_en_bd({'NB1': {'A1': {
            'existencia': 10, 'comprometido': 0, 'salida_sin_conf': 0}}})
        assert StockSiesa.query.count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# 3 · Los lectores ven la verdad
# ═════════════════════════════════════════════════════════════════════════════

class TestLosLectoresVenLaVerdad:

    def test_cache_y_bd_tienen_la_misma_forma(self, db, cache_limpio):
        sembrar(db, 'NB1', 'FANTASMA', 1508)
        inv = refrescar(SiesaExistencias(universo()))
        assert 'FANTASMA' not in inv['NB1']
        bd, _ = iss._leer_stock_de_bd('NB1')
        assert set(bd) == set(inv['NB1'])

    def test_el_armador_ve_cero(self, db, cache_limpio):
        from app.services.armador_service import posicion_inventario
        sembrar(db, 'NB1', 'PAPELSP1003', 1508)
        refrescar(SiesaExistencias(universo()))
        pos, _ = posicion_inventario({'PAPELSP1003'})
        assert pos['PAPELSP1003']['existencia'] == 0
        assert pos['PAPELSP1003']['frescura'] > datetime.utcnow() - timedelta(minutes=5)

    def test_el_ancla_del_kardex_es_cero_con_fuente(self, db, cache_limpio):
        from app.services.kardex_service import KardexService
        sembrar(db, 'NB1', 'PAPELSP1003', 1508)
        refrescar(SiesaExistencias(universo()))
        assert KardexService._obtener_saldo_actual('PAPELSP1003', 'NB1') == (0.0, 'STOCK_SIESA')

    def test_la_frescura_de_la_bodega_avanza_con_los_ceros(self, db, cache_limpio):
        sembrar(db, 'FF1', 'X', 5)
        refrescar(SiesaExistencias(universo()))
        f = iss.frescura_stock_siesa(['FF1'])
        assert f['por_bodega']['FF1']['actualizada'] > datetime.utcnow() - timedelta(minutes=5)

    def test_la_reconciliacion_no_usa_la_foto_vieja(self, db, cache_limpio):
        with patch.object(iss.connekta, '_get', side_effect=SiesaExistencias(
                universo(), falla={1})), patch('time.sleep'):
            with pytest.raises(ValueError, match='no quedó completa'):
                iss._descargar_inventario_multibodega_para_reconciliar()

    def test_salud_no_pide_descargar_una_bodega_que_siesa_dice_vacia(self, db, cache_limpio):
        from app.services import analitica_salud
        refrescar(SiesaExistencias(universo()))
        f = analitica_salud.fuente_stock(datetime.utcnow())
        assert f['veredicto'] == analitica_salud.AL_DIA, f
        assert 'FF1' in f['detalle']['bodegas_en_cero_segun_siesa']

    def test_sin_lectura_completa_no_se_escribe_inventario(self, cache_limpio):
        """Aunque nadie la haya marcado degradada: `completa` es la licencia."""
        cache_limpio.update(data={'NB1': {'X': {}}}, ts=datetime.utcnow(),
                            degradado=False, bodegas_frescas=frozenset({'NB1'}),
                            completa=False, motivo='')
        assert 'no quedó completa' in iss.fuente_para_escribir('NB1')
        cache_limpio['completa'] = True
        assert iss.fuente_para_escribir('NB1') == ''

    def test_salud_declara_la_lectura_incompleta(self, db, cache_limpio):
        from app.services import analitica_salud
        refrescar(SiesaExistencias(universo()))
        refrescar(SiesaExistencias(universo(), falla={1}))
        f = analitica_salud.fuente_stock(datetime.utcnow())
        assert f['veredicto'] == analitica_salud.INCOMPLETA, f
        assert 'no quedó completa' in f['motivo']


# ═════════════════════════════════════════════════════════════════════════════
# 4 · La carga física de las 7:00 no escribe lo guardado
# ═════════════════════════════════════════════════════════════════════════════

class TestLaCargaFisicaLeeLaLectura:

    def test_el_fantasma_no_llega_al_hueco(self, app, db, almacen, producto, cache_limpio):
        """El SKU que Siesa ya no reporta y `stock_siesa` tenía en 1.508: la
        carga lo deja en 0 en el WMS (antes lo escribía con 1.508)."""
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        sembrar(db, 'NB1', producto.codigo_siesa, 1508)
        for i in range(80):
            db.session.add(Producto(codigo=f'SKU{i:03d}', nombre='x',
                                    codigo_siesa=f'SKU{i:03d}', activo=True))
        gen = Ubicacion(codigo=Ubicacion.CODIGO_GENERAL, almacen_id=almacen.id,
                        zona='GENERAL', activo=True)
        db.session.add(gen)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=gen.id, producto_id=producto.id,
                                         cantidad=1508))
        db.session.commit()
        with patch.object(iss.connekta, '_get', side_effect=SiesaExistencias(universo())), \
                patch.object(iss.connekta, 'bodega', 'NB1'), patch('time.sleep'):
            iss._run_carga_inicial(app, 'NB1')
        db.session.expire_all()
        up = UbicacionProducto.query.filter_by(ubicacion_id=gen.id,
                                               producto_id=producto.id).one()
        assert up.cantidad == 0


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Trinquetes (AST)
# ═════════════════════════════════════════════════════════════════════════════

LECTURA_DE_SIESA = '_descargar_una_pasada_custom'
GUARDAR = '_guardar_stock_en_bd'
LEER_GUARDADO = '_leer_stock_de_bd'
CAMPOS_STOCK = {'existencia', 'comprometido', 'salida_sin_conf', 'updated_at',
                'ausente_desde'}

#: Los que escriben `stock_siesa`, con su motivo. Uno, y se queda así.
ESCRITORES_STOCK_SIESA = {
    ('app/services/inventario_siesa_service.py', '_guardar_stock_en_bd'):
        'la única: escribe una lectura completa y pone en cero lo que no vino',
}

#: Los que leen lo guardado, con su motivo. Solo encoge.
LECTORES_DE_LO_GUARDADO = {
    ('app/services/inventario_siesa_service.py', '_descargar_inventario_siesa_raw'):
        'solo en la rama degradada, que no persiste nada',
    ('app/services/inventario_siesa_service.py', 'obtener_stock_bodega'):
        'lectura para la pantalla de Pedir/traslados; no escribe',
}


def _funciones(arbol):
    """Funciones de primer nivel y métodos: el cuerpo propio de cada una, sin
    las funciones anidadas (una hija no cuenta como el padre)."""
    for n in ast.walk(arbol):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n


def _cuerpo(fn):
    """Los nodos del cuerpo de `fn` sin entrar en funciones anidadas."""
    pila = list(fn.body)
    while pila:
        n = pila.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield n
        pila.extend(ast.iter_child_nodes(n))


def _llama(nodos, nombre):
    return [n for n in nodos if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id == nombre)
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre))]


def guardados_con_otra_cosa(src: str, archivo: str) -> set:
    """(archivo, función) que llama `_guardar_stock_en_bd` con algo que no es
    una lectura de Siesa: el primer argumento tiene que ser la llamada misma o
    un nombre cuya ÚNICA asignación en la función es esa llamada."""
    malos = set()
    for fn in _funciones(ast.parse(src)):
        nodos = list(_cuerpo(fn))
        for call in _llama(nodos, GUARDAR):
            if not call.args:
                malos.add((archivo, fn.name))
                continue
            arg = call.args[0]
            if _llama([arg], LECTURA_DE_SIESA):
                continue
            if isinstance(arg, ast.Name):
                asign = [n for n in nodos if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                         and any(isinstance(t, ast.Name) and t.id == arg.id
                                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
                mutado = [n for n in nodos if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Attribute)
                          and isinstance(n.func.value, ast.Name)
                          and n.func.value.id == arg.id
                          and n.func.attr in ('update', 'setdefault', '__setitem__')]
                if (len(asign) == 1 and isinstance(asign[0], ast.Assign)
                        and _llama([asign[0].value], LECTURA_DE_SIESA) and not mutado):
                    continue
            malos.add((archivo, fn.name))
    return malos


def escritores_de_stock_siesa(src: str, archivo: str) -> dict:
    """(archivo, función) → ¿pregunta `lectura_completa`? para toda función
    que construye `StockSiesa(...)`, o que nombra `StockSiesa` y asigna uno de
    sus campos, o hace `update`/`delete` sobre su consulta."""
    out = {}
    for fn in _funciones(ast.parse(src)):
        nodos = list(_cuerpo(fn))
        nombra = any((isinstance(n, ast.Name) and n.id == 'StockSiesa')
                     or (isinstance(n, ast.Attribute) and n.attr == 'StockSiesa')
                     for n in nodos)
        if not nombra:
            continue
        construye = bool(_llama(nodos, 'StockSiesa'))
        # Un objeto que en esta función se construye con OTRO modelo (la foto
        # diaria copia `f.existencia = s.existencia`) no es una fila de
        # stock_siesa.
        de_otro_modelo = {
            t.id for n in nodos if isinstance(n, ast.Assign)
            and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name)
            and n.value.func.id[:1].isupper() and n.value.func.id != 'StockSiesa'
            for t in n.targets if isinstance(t, ast.Name)}
        asigna = any(isinstance(n, (ast.Assign, ast.AugAssign))
                     and any(isinstance(t, ast.Attribute) and t.attr in CAMPOS_STOCK
                             and not (isinstance(t.value, ast.Name)
                                      and t.value.id in de_otro_modelo)
                             for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))
                     for n in nodos)
        en_bloque = bool(_llama(nodos, 'update') or _llama(nodos, 'delete'))
        if construye or asigna or en_bloque:
            out[(archivo, fn.name)] = bool(_llama(nodos, 'lectura_completa'))
    return out


def lectores_de_lo_guardado(src: str, archivo: str) -> dict:
    """(archivo, función) → ¿todas las llamadas a `_leer_stock_de_bd` viven
    dentro de un `if` que pregunta por `_degradado`?"""
    out = {}
    for fn in _funciones(ast.parse(src)):
        if fn.name == LEER_GUARDADO:
            continue
        nodos = list(_cuerpo(fn))
        llamadas = _llama(nodos, LEER_GUARDADO)
        if not llamadas:
            continue
        protegidas = set()
        for n in nodos:
            if isinstance(n, ast.If) and any(isinstance(x, ast.Name) and x.id == '_degradado'
                                             for x in ast.walk(n.test)):
                for x in ast.walk(ast.Module(body=n.body, type_ignores=[])):
                    protegidas.add(id(x))
        out[(archivo, fn.name)] = all(id(c) in protegidas for c in llamadas)
    return out


def _escanear(fn_detector, carpetas=('app', 'scripts')):
    out = {}
    for c in carpetas:
        for p in sorted((RAIZ / c).rglob('*.py')):
            r = fn_detector(p.read_text(encoding='utf-8'), p.relative_to(RAIZ).as_posix())
            if isinstance(r, dict):
                out.update(r)
            else:
                out.setdefault('_', set()).update(r)
    return out


class TestNadiePersisteUnaMezcla:

    def test_guardar_solo_recibe_una_lectura_de_siesa(self):
        malos = _escanear(guardados_con_otra_cosa).get('_', set())
        assert not malos, f'{sorted(malos)}: persiste en stock_siesa algo que no es una lectura'

    def test_un_solo_escritor_y_pregunta_si_la_lectura_es_completa(self):
        esc = _escanear(escritores_de_stock_siesa, carpetas=('app',))
        assert set(esc) == set(ESCRITORES_STOCK_SIESA), (
            f'escritores de stock_siesa: {sorted(esc)} — se esperaba solo '
            f'{sorted(ESCRITORES_STOCK_SIESA)}')
        assert all(esc.values()), 'el escritor no pregunta lectura_completa'

    def test_lo_guardado_solo_se_lee_donde_no_se_persiste(self):
        lec = _escanear(lectores_de_lo_guardado, carpetas=('app',))
        assert set(lec) <= set(LECTORES_DE_LO_GUARDADO), (
            f'lectores nuevos de lo guardado: {sorted(set(lec) - set(LECTORES_DE_LO_GUARDADO))}')
        raw = ('app/services/inventario_siesa_service.py', '_descargar_inventario_siesa_raw')
        assert lec[raw], 'la descarga lee lo guardado fuera de la rama degradada'

    def test_cada_excepcion_dice_por_que(self):
        for d in (ESCRITORES_STOCK_SIESA, LECTORES_DE_LO_GUARDADO):
            assert all(len(v) > 20 for v in d.values())

    def test_pisos(self):
        assert len(_escanear(escritores_de_stock_siesa, carpetas=('app',))) >= 1
        assert len(_escanear(lectores_de_lo_guardado, carpetas=('app',))) >= 2
        src = (RAIZ / 'app/services/inventario_siesa_service.py').read_text(encoding='utf-8')
        assert len(_llama(list(ast.walk(ast.parse(src))), GUARDAR)) >= 1


class TestLosDetectoresMuerden:

    def test_ve_la_mezcla(self):
        src = (
            'def a():\n'
            '    lectura = _descargar_una_pasada_custom()\n'
            '    merged = dict(_leer_stock_de_bd("NB1"))\n'
            '    merged.update(lectura)\n'
            '    _guardar_stock_en_bd(merged)\n'
            'def b():\n'
            '    lectura = _descargar_una_pasada_custom()\n'
            '    lectura.update({"NB1": {}})\n'
            '    _guardar_stock_en_bd(lectura)\n'
            'def c():\n'
            '    _guardar_stock_en_bd({"NB1": {}})\n')
        assert guardados_con_otra_cosa(src, 'x') == {('x', 'a'), ('x', 'b'), ('x', 'c')}

    def test_no_marca_lo_sano(self):
        src = (
            'def a():\n'
            '    """_guardar_stock_en_bd(merged)"""\n'
            '    lectura = _descargar_una_pasada_custom()\n'
            '    # _guardar_stock_en_bd(otra)\n'
            '    _guardar_stock_en_bd(lectura)\n'
            'def b():\n'
            '    iss._guardar_stock_en_bd(iss._descargar_una_pasada_custom())\n')
        assert guardados_con_otra_cosa(src, 'x') == set()

    def test_ve_las_escrituras_de_stock_siesa(self):
        src = (
            'def a():\n    db.session.add(StockSiesa(bodega="NB1"))\n'
            'def b():\n    r = StockSiesa.query.first()\n    r.existencia = 3\n'
            'def c():\n    StockSiesa.query.filter_by(bodega="X").delete()\n'
            'def d(l):\n    if not lectura_completa(l):\n        return\n'
            '    db.session.add(StockSiesa(bodega="NB1"))\n'
            'def e():\n    """StockSiesa(...)"""\n    return StockSiesa.query.all()\n'
            'def f():\n    def g():\n        StockSiesa(bodega="X")\n    return g\n'
            'def h(s):\n    x = StockSiesa.query.first()\n    f = Foto(dia=1)\n'
            '    f.existencia = x.existencia\n')
        assert escritores_de_stock_siesa(src, 'x') == {
            ('x', 'a'): False, ('x', 'b'): False, ('x', 'c'): False, ('x', 'd'): True,
            ('x', 'g'): False}

    def test_ve_lo_guardado_fuera_de_la_rama_degradada(self):
        src = (
            'def a(_degradado):\n'
            '    if _degradado:\n        x = _leer_stock_de_bd("NB1")\n'
            'def b():\n    x = _leer_stock_de_bd("NB1")\n'
            'def c(_degradado):\n'
            '    x = _leer_stock_de_bd("NB1")\n'
            '    if _degradado:\n        y = _leer_stock_de_bd("NB1")\n')
        assert lectores_de_lo_guardado(src, 'x') == {
            ('x', 'a'): True, ('x', 'b'): False, ('x', 'c'): False}
