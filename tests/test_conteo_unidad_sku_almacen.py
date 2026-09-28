"""
La unidad de un conteo contra Siesa es **SKU × almacén** (P0-1, 2026-09-27).

## El defecto (reproducido por la auditoría de la noche del 2026-09-27)

La foto de Siesa contra la que se compara un conteo es por ítem × bodega
(`API_v2_Inventarios_InvFecha`, 0 filas con ubicación en NB1 y NS1). La tarea
era por (producto, ubicación): un SKU en dos huecos daba dos cadenas, y cada
una comparaba SU hueco contra el TOTAL de la bodega. Con el estante contado
bien (10 + 90 = 100 = Siesa) salían **dos ajustes automáticos, AJ-SAL 10 y
AJ-SAL 90**: Siesa en 0 con 100 unidades en el estante. Y las guardas
anti-doble-ajuste miraban el hueco, así que la segunda cadena no se frenaba con
la primera. En producción, 14 SKUs de NB1 tienen stock en más de un hueco; el
día que se active layout PICKING/RESERVA, todos.

## La clase

*Una puerta abre —o una guarda deja pasar— una cadena comparable contra Siesa
por una llave que no es la de la foto (el hueco en vez del SKU × almacén).*

Trinquetes (AST): `raiz_con_cadena_viva` se llama solo desde
`SesionConteo.cadenas_vivas_del_almacen`; la raíz de una cadena solo la
construye `ConteoService.nueva_raiz`; toda función que abre una raíz pregunta
antes por una cadena viva del SKU en el almacén (inventario de excepciones que
solo encoge); ningún filtro de sesiones compara `SesionConteo.ubicacion_id`
fuera de lo declarado. Meta-tests de lo que ven y de lo sano que no marcan,
pisos.
"""
import ast
import importlib.util
import json
import pathlib

import pytest

from tests.test_conteo_teorico_pos import SKU, contar_primero, siesa, tienda  # noqa: F401

RAIZ = pathlib.Path(__file__).resolve().parents[1]
APP = RAIZ / 'app'


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _sin_env(monkeypatch):
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    for n in VARIABLES_DE_ENTORNO:
        monkeypatch.delenv(n, raising=False)


def _ubicacion(db, almacen_id, codigo):
    from app.models.ubicacion import Ubicacion
    u = Ubicacion(codigo=codigo, almacen_id=almacen_id, tipo_zona='GENERAL',
                  stock_minimo=0, stock_maximo=9999, secuencia_ruteo=2, activo=True)
    db.session.add(u)
    db.session.flush()
    return u


def _stock(db, ubicacion, producto_id, cantidad):
    from app.models.inventario import UbicacionProducto
    db.session.add(UbicacionProducto(ubicacion_id=ubicacion.id, producto_id=producto_id,
                                     cantidad=cantidad, reservado=0, bloqueado=0))
    db.session.flush()


@pytest.fixture
def dos_huecos(db, tienda):
    """El SKU del fixture `tienda` con 10 en POS-UB y 90 en CROSS-DOCK, y un
    tercer hueco vacío (A-01-01, cantidad 0) — el caso de producción."""
    cd = _ubicacion(db, tienda['almacen'].id, 'CROSS-DOCK')
    _stock(db, cd, tienda['producto'].id, 90)
    vacio = _ubicacion(db, tienda['almacen'].id, 'A-01-01')
    _stock(db, vacio, tienda['producto'].id, 0)
    db.session.commit()
    return {**tienda, 'cd': cd, 'vacio': vacio}


def _jobs():
    from app.models.siesa_job import SiesaJob
    return [json.loads(j.payload) for j in SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO').all()]


# ─────────────────────────────────────────────────────────────────────────────
# 1 · La reproducción de la auditoría (tests/test_zz_aud_multihueco.py)
# ─────────────────────────────────────────────────────────────────────────────

class TestLaReproduccion:

    def test_contando_bien_los_dos_huecos_no_hay_ningun_ajuste(
            self, db, siesa, dos_huecos, monkeypatch):
        _sin_env(monkeypatch)
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=100, costo=100.0)
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        assert r['tareas_creadas'] == 1, (
            f'una cadena por SKU × almacén, no una por hueco: {r["codigos"]}')
        (s,) = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).all()
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        # Quien cuenta suma todos los lugares: 10 + 90.
        r1 = contar_primero(s.id, dos_huecos['a'].id, 100, cero_confirmado=True)
        assert r1['resultado'] == 'MATCH', r1
        assert _jobs() == [], f'ajustes falsos encolados: {_jobs()}'

    def test_un_faltante_real_sale_una_sola_vez(self, db, siesa, dos_huecos, monkeypatch):
        """Faltan 5 en total: un solo AJ-SAL de 5, no uno por hueco."""
        _sin_env(monkeypatch)
        from app.models.conteo import SesionConteo
        siesa.poner(existencia=100, costo=100.0)
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        (s,) = SesionConteo.query.filter(SesionConteo.codigo.in_(r['codigos'])).all()
        _svc().obtener_tarea_operario(s.id, dos_huecos['a'].id)
        r1 = contar_primero(s.id, dos_huecos['a'].id, 95, cero_confirmado=True)
        assert r1['resultado'] == 'SEGUNDO_CONTEO'
        _svc().obtener_tarea_operario(r1['segundo_conteo_id'], dos_huecos['b'].id)
        _svc().registrar_conteo(r1['segundo_conteo_id'], dos_huecos['b'].id, 95)
        assert [(j['motivo_codigo'], j['cantidad']) for j in _jobs()] == [('AJ-SAL', 5)]


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Las puertas que abren cadenas
# ─────────────────────────────────────────────────────────────────────────────

class TestLasPuertas:

    def test_el_conteo_manual_no_abre_una_segunda_cadena_del_sku(self, db, dos_huecos):
        r1 = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        r2 = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        assert (r1['tareas_nuevas'], r2['tareas_nuevas'], r2['omitidas_ya_activas']) == (1, 0, 1)

    def test_la_cadena_va_a_la_ubicacion_general_si_el_almacen_la_tiene(self, db, dos_huecos):
        from app.models.conteo import SesionConteo
        general = _ubicacion(db, dos_huecos['almacen'].id, 'SIESA-GENERAL')
        db.session.commit()
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        s = SesionConteo.query.filter_by(codigo=r['codigos'][0]).one()
        assert s.ubicacion_id == general.id

    def test_sin_ubicacion_general_la_del_hueco_con_mas_unidades(self, db, dos_huecos):
        from app.models.conteo import SesionConteo
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        s = SesionConteo.query.filter_by(codigo=r['codigos'][0]).one()
        assert s.ubicacion_id == dos_huecos['cd'].id, 'CROSS-DOCK tiene 90, POS-UB 10'

    def test_otro_almacen_no_bloquea(self, db, dos_huecos):
        """La guarda es del SKU EN el almacén: una cadena del mismo SKU en otra
        bodega no dice nada de esta (mutación: quitar el filtro por almacén)."""
        from app.models.almacen import Almacen
        otro = Almacen(codigo='ALM-OTRO', nombre='Otro', bodega_siesa_id='NC1', activo=True)
        db.session.add(otro)
        db.session.flush()
        ub = _ubicacion(db, otro.id, 'OTRO-01')
        _stock(db, ub, dos_huecos['producto'].id, 7)
        db.session.commit()
        assert _svc().crear_conteo_manual(otro.id, SKU)['tareas_nuevas'] == 1
        assert _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)['tareas_nuevas'] == 1

    def test_la_auditoria_por_faltante_reusa_la_cadena_de_otro_hueco(self, db, dos_huecos):
        from app.models.conteo import SesionConteo
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        manual = SesionConteo.query.filter_by(codigo=r['codigos'][0]).one()
        aud = _svc().generar_auditoria_por_excepcion(
            tarea_picking_id=None, ubicacion_id=dos_huecos['ubicacion'].id,
            producto_id=dos_huecos['producto'].id, almacen_id=dos_huecos['almacen'].id)
        assert aud.id == manual.id, 'la auditoría abrió una segunda cadena del SKU'

    def test_reabrir_un_bloqueado_con_otra_cadena_del_sku_se_niega(self, db, dos_huecos):
        from app.models.conteo import EstadoConteo, SesionConteo
        viejo = SesionConteo(codigo='VIEJO-1', tipo='DIARIO_ABC', estado=EstadoConteo.BLOQUEADO,
                             ubicacion_id=dos_huecos['ubicacion'].id,
                             almacen_id=dos_huecos['almacen'].id,
                             producto_id=dos_huecos['producto'].id, es_segundo_conteo=False)
        db.session.add(viejo)
        db.session.flush()
        nueva = SesionConteo(codigo='NUEVA-1', tipo='MANUAL', estado=EstadoConteo.PENDIENTE,
                             ubicacion_id=dos_huecos['cd'].id,
                             almacen_id=dos_huecos['almacen'].id,
                             producto_id=dos_huecos['producto'].id, es_segundo_conteo=False)
        db.session.add(nueva)
        db.session.commit()
        with pytest.raises(ValueError, match='Reabrir este dejaría dos'):
            _svc().reabrir_bloqueado(viejo.id, dos_huecos['supervisor'].id)


class TestElPlanYElWatchdog:

    def _clasificar(self, db, producto, almacen, clase):
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC
        db.session.add(ProductoClasificacionABC(producto_id=producto.id, almacen_id=almacen.id,
                                                clasificacion=clase))
        db.session.commit()

    def test_el_generador_crea_una_por_sku_y_no_repite(self, db, dos_huecos, monkeypatch):
        _sin_env(monkeypatch)
        from app.models.conteo import SesionConteo
        from app.services.abc_service import ABCService
        self._clasificar(db, dos_huecos['producto'], dos_huecos['almacen'], 'A')
        r = ABCService.generar_tareas_conteo_diario(dos_huecos['almacen'].id)
        assert r['tareas_creadas'] == 1 and r['candidatos'] == 1
        r2 = ABCService.generar_tareas_conteo_diario(dos_huecos['almacen'].id)
        assert r2['tareas_creadas'] == 0 and r2['omitidos_por_pendiente'] == 1
        assert SesionConteo.query.count() == 1

    def test_contado_en_cualquier_hueco_queda_al_dia(self, db, dos_huecos, monkeypatch):
        """El «último conteo» es del SKU en el almacén: una cadena cerrada con
        la ubicación de un hueco deja al día el SKU entero."""
        _sin_env(monkeypatch)
        from datetime import datetime
        from app.models.conteo import SesionConteo
        from app.services.abc_service import ABCService
        self._clasificar(db, dos_huecos['producto'], dos_huecos['almacen'], 'A')
        db.session.add(SesionConteo(codigo='HECHO', tipo='DIARIO_ABC', estado='MATCH',
                                    ubicacion_id=dos_huecos['ubicacion'].id,
                                    almacen_id=dos_huecos['almacen'].id,
                                    producto_id=dos_huecos['producto'].id,
                                    fecha_cierre=datetime.utcnow(), es_segundo_conteo=False))
        db.session.commit()
        r = ABCService.generar_tareas_conteo_diario(dos_huecos['almacen'].id)
        assert (r['tareas_creadas'], r['omitidos_por_intervalo']) == (0, 1)

    def test_el_watchdog_crea_un_override_por_sku(self, db, dos_huecos, monkeypatch):
        _sin_env(monkeypatch)
        from app.services.abc_service import ABCService
        from tests.test_watchdog_por_almacen import _picks_completados
        self._clasificar(db, dos_huecos['producto'], dos_huecos['almacen'], 'C')
        _picks_completados(db, dos_huecos['producto'], dos_huecos['almacen'],
                           dos_huecos['ubicacion'], 12, 'WD')
        informe = ABCService.watchdog_con_informe(dos_huecos['almacen'].id)
        assert len(informe['overrides']) == 1, informe
        informe2 = ABCService.watchdog_con_informe(dos_huecos['almacen'].id)
        assert informe2['overrides'] == [] and informe2['omitidos_ya_activos'] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Las guardas del ajuste
# ─────────────────────────────────────────────────────────────────────────────

class TestLaObservacionViejaEsDelSku:

    def test_un_conteo_posterior_de_otro_hueco_deja_vieja_la_foto(self, db, dos_huecos):
        from datetime import datetime, timedelta
        from app.models.conteo import EstadoConteo, SesionConteo
        antes = datetime.utcnow() - timedelta(hours=2)
        vieja = SesionConteo(codigo='V-1', tipo='DIARIO_ABC', estado=EstadoConteo.DESCUADRE,
                             ubicacion_id=dos_huecos['ubicacion'].id,
                             almacen_id=dos_huecos['almacen'].id,
                             producto_id=dos_huecos['producto'].id, es_segundo_conteo=False,
                             foto_siesa_at=antes, cantidad_fisica=10)
        nueva = SesionConteo(codigo='N-1', tipo='MANUAL', estado=EstadoConteo.MATCH,
                             ubicacion_id=dos_huecos['cd'].id,
                             almacen_id=dos_huecos['almacen'].id,
                             producto_id=dos_huecos['producto'].id, es_segundo_conteo=False,
                             foto_siesa_at=antes + timedelta(hours=1), cantidad_fisica=100)
        db.session.add_all([vieja, nueva])
        db.session.commit()
        motivo = _svc().observacion_que_la_vuelve_vieja(vieja)
        assert motivo and 'N-1' in motivo and 'posterior' in motivo


class TestLaAuditoriaDePickingCuentaElTotal:

    def test_la_cantidad_es_la_del_sku_en_el_almacen(self, db, dos_huecos):
        assert _svc().existencia_wms_del_sku(
            dos_huecos['producto'].id, dos_huecos['almacen'].id) == 100

    def test_el_llamador_pasa_el_total(self):
        """`auditar_tarea` le pasa a `ajustar_desde_auditoria_picking` la
        existencia del SKU en el almacén, no la del hueco (por AST)."""
        arbol = ast.parse((APP / 'services' / 'picking_service.py').read_text(encoding='utf-8'))
        llamadas = [n for n in ast.walk(arbol) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == 'ajustar_desde_auditoria_picking']
        assert len(llamadas) == 1
        kw = {k.arg: k.value for k in llamadas[0].keywords}
        v = kw['cantidad_fisica']
        assert (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == 'existencia_wms_del_sku'), ast.unparse(v)


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Lo que ve quien cuenta
# ─────────────────────────────────────────────────────────────────────────────

class TestLosLugaresDelHud:

    def test_todos_los_lugares_sin_cantidades_y_sin_los_vacios(self, db, dos_huecos):
        from app.models.conteo import SesionConteo
        general = _ubicacion(db, dos_huecos['almacen'].id, 'SIESA-GENERAL')
        _stock(db, general, dos_huecos['producto'].id, 3)
        db.session.commit()
        r = _svc().crear_conteo_manual(dos_huecos['almacen'].id, SKU)
        s = SesionConteo.query.filter_by(codigo=r['codigos'][0]).one()
        v = _svc().vista_hud(s)
        assert v['lugares'] == [{'codigo': 'CROSS-DOCK', 'fisica': True},
                                {'codigo': 'POS-UB', 'fisica': True},
                                {'codigo': None, 'fisica': False}]
        assert 'A-01-01' not in json.dumps(v), 'un hueco en 0 no se nombra'
        assert not any(k in json.dumps(v) for k in ('"cantidad"', '"stock"', '90', '100'))

    def test_el_hud_pinta_los_lugares_y_pide_un_total(self, tmp_path):
        from tests.test_conteo_hud_operario import _correr_hud
        r = _correr_hud(tmp_path, {'tarea': {
            'id': 7, 'tipo': 'CONTEO', 'producto_nombre': 'x',
            'ubicacion': 'SIESA-GENERAL', 'ubicacion_fisica': False,
            'lugares': [{'codigo': 'CROSS-DOCK', 'fisica': True},
                        {'codigo': 'A<b>1', 'fisica': True},
                        {'codigo': None, 'fisica': False}]}})
        html = r['html']
        assert 'Un solo total' in html
        assert '<b>CROSS-DOCK</b>, <b>A&lt;b&gt;1</b> y el resto del almacén' in html
        assert 'A<b>1' not in html

    def test_sin_lugares_marcados_lo_dice(self, tmp_path):
        from tests.test_conteo_hud_operario import _correr_hud
        r = _correr_hud(tmp_path, {'tarea': {'id': 7, 'tipo': 'CONTEO', 'producto_nombre': 'x',
                                             'lugares': []}})
        assert 'no le tiene un lugar marcado' in r['html']


# ─────────────────────────────────────────────────────────────────────────────
# 5 · La migración
# ─────────────────────────────────────────────────────────────────────────────

def _migracion():
    ruta = RAIZ / 'migrations' / 'versions' / 'm051conteo_unidad_sku_almacen.py'
    spec = importlib.util.spec_from_file_location('m051conteo', ruta)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestLaMigracion:
    """Contra una base SQLite PROPIA (no la de la suite, que es de sesión: un
    DROP INDEX ahí se auto-comitea y la deja sin índice para todos)."""

    @pytest.fixture
    def base(self, app):
        import sqlalchemy as sa
        from app.extensions import db as _db
        motor = sa.create_engine('sqlite://')
        with app.app_context():
            _db.metadata.create_all(motor, tables=[_db.metadata.tables['sesiones_conteo']])
        with motor.begin() as conn:
            conn.execute(sa.text('DROP INDEX ix_sesion_conteo_sku_activa_unica'))
        yield motor
        motor.dispose()

    def _raiz(self, conn, codigo, estado, ubicacion_id):
        import sqlalchemy as sa
        conn.execute(sa.text(
            "INSERT INTO sesiones_conteo (codigo, tipo, ubicacion_id, almacen_id, producto_id,"
            " estado, es_segundo_conteo) VALUES (:c, 'DIARIO_ABC', :u, 1, 1, :e, 0)"),
            {'c': codigo, 'u': ubicacion_id, 'e': estado})

    def _upgrade(self, conn):
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        with Operations.context(MigrationContext.configure(conn)):
            _migracion().upgrade()

    def test_cancela_la_pendiente_sobrante_y_crea_el_indice(self, base):
        import sqlalchemy as sa
        with base.begin() as conn:
            self._raiz(conn, 'EN-CURSO', 'EN_PROCESO', 1)
            self._raiz(conn, 'SOBRA', 'PENDIENTE', 2)
            self._upgrade(conn)
            filas = dict(conn.execute(sa.text(
                'SELECT codigo, estado FROM sesiones_conteo')).fetchall())
            motivo = conn.execute(sa.text(
                "SELECT motivo_edicion FROM sesiones_conteo WHERE codigo='SOBRA'")).scalar()
            indices = {r[1] for r in conn.execute(sa.text("PRAGMA index_list('sesiones_conteo')"))}
        assert filas == {'EN-CURSO': 'EN_PROCESO', 'SOBRA': 'CANCELADO'}
        assert 'm051conteo' in motivo
        assert 'ix_sesion_conteo_sku_activa_unica' in indices

    def test_sin_duplicados_no_toca_nada(self, base):
        import sqlalchemy as sa
        with base.begin() as conn:
            self._raiz(conn, 'SOLA', 'PENDIENTE', 1)
            self._upgrade(conn)
            assert conn.execute(sa.text('SELECT estado FROM sesiones_conteo')).scalar() == 'PENDIENTE'

    def test_dos_en_curso_detienen_el_despliegue(self, base):
        with base.begin() as conn:
            self._raiz(conn, 'C-1', 'EN_PROCESO', 1)
            self._raiz(conn, 'C-2', 'SEGUNDO_CONTEO', 2)
            with pytest.raises(RuntimeError, match='dos conteos EN CURSO'):
                self._upgrade(conn)


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Trinquetes (AST) — la clase, no el caso
# ─────────────────────────────────────────────────────────────────────────────

def _funciones(arbol):
    """(nombre, nodo) de toda función, con su clase si la tiene."""
    salida = []
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ClassDef):
            for hijo in nodo.body:
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    salida.append((f'{nodo.name}.{hijo.name}', hijo))
    en_clase = {id(n) for _, n in salida}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(nodo) not in en_clase:
            salida.append((nodo.name, nodo))
    return salida


def _propias(fn):
    """Los nodos de `fn` sin entrar en funciones anidadas."""
    anidada = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    pila = [n for n in fn.body if not isinstance(n, anidada)]
    while pila:
        n = pila.pop()
        yield n
        for h in ast.iter_child_nodes(n):
            if not isinstance(h, anidada):
                pila.append(h)


def _llama(fn, nombre):
    return any(isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Attribute) and n.func.attr == nombre)
        or (isinstance(n.func, ast.Name) and n.func.id == nombre)) for n in _propias(fn))


def _fuentes():
    for ruta in sorted(APP.rglob('*.py')):
        yield str(ruta.relative_to(RAIZ)), ruta.read_text(encoding='utf-8')


def _escanear(fuentes):
    """Las cuatro formas de la clase, por (archivo, función)."""
    guardas, raices, abren_sin_guarda, por_hueco = set(), set(), set(), set()
    for archivo, src in fuentes:
        arbol = ast.parse(src)
        for nombre, fn in _funciones(arbol):
            clave = (archivo, nombre)
            nodos = list(_propias(fn))
            if _llama(fn, 'raiz_con_cadena_viva'):
                guardas.add(clave)
            for n in nodos:
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == 'SesionConteo'):
                    kw = {k.arg: k.value for k in n.keywords}
                    segundo = kw.get('es_segundo_conteo')
                    if not (isinstance(segundo, ast.Constant) and segundo.value is True):
                        raices.add(clave)
                if isinstance(n, ast.Compare):
                    for lado in [n.left] + list(n.comparators):
                        if (isinstance(lado, ast.Attribute) and lado.attr == 'ubicacion_id'
                                and isinstance(lado.value, ast.Name)
                                and lado.value.id == 'SesionConteo'):
                            por_hueco.add(clave)
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == 'in_' and isinstance(n.func.value, ast.Attribute)
                        and n.func.value.attr == 'ubicacion_id'
                        and isinstance(n.func.value.value, ast.Name)
                        and n.func.value.value.id == 'SesionConteo'):
                    por_hueco.add(clave)
            if _llama(fn, 'nueva_raiz') and not (
                    _llama(fn, 'cadenas_vivas_del_almacen') or _llama(fn, 'skus_con_cadena_viva')):
                abren_sin_guarda.add(clave)
    return guardas, raices, abren_sin_guarda, por_hueco


#: Quién puede llamar a `raiz_con_cadena_viva` (el predicado de ESTADO, sin
#: llave): solo la función que le pone la llave SKU × almacén.
GUARDAS_PERMITIDAS = {('app/models/conteo.py', 'SesionConteo.cadenas_vivas_del_almacen')}

#: Quién construye una raíz (`SesionConteo(...)` sin `es_segundo_conteo=True`).
RAICES_PERMITIDAS = {('app/services/conteo_service.py', 'ConteoService.nueva_raiz')}

#: Quién abre una raíz sin preguntar antes por una cadena viva del SKU.
#: Solo encoge; cada entrada dice por qué.
ABREN_SIN_GUARDA = {
    ('app/services/conteo_service.py', 'ConteoService.ajustar_desde_auditoria_picking'):
        'La auditoría de picking es un conteo que se resuelve en el acto (MATCH o '
        'DESCUADRE, nunca viva): no deja una cadena abierta que se duplique. Que su '
        'ajuste no pise el de otra cadena lo decide `observacion_que_la_vuelve_vieja`, '
        'por SKU × almacén.',
}

#: Quién filtra sesiones por hueco. Ninguno es una guarda de «¿ya hay una
#: cadena de este producto?». Solo encoge.
POR_HUECO = {
    ('app/services/mobile_service.py', 'MobileService.get_tarea_actual'):
        'El intercalado ofrece al picker un conteo del hueco donde está pickeando: '
        'es reparto por cercanía, no una guarda contra Siesa (C5 lo revisa con la '
        'asignación por presencia).',
    ('app/services/conteo_service.py', 'ConteoService._crear_conteo_verificacion'):
        'Excluye del CC2 a quien ya tiene una tarea del mismo producto y hueco: es '
        'reparto de personas, no una guarda. La asignación por presencia (v3) lo '
        'reemplaza entero.',
    ('app/services/dashboard_service.py', 'DashboardService.productividad_operarios'):
        'Un JOIN para pintar el código de la ubicación, no un filtro.',
}


class TestLaUnidadEnElCodigo:

    def test_solo_una_funcion_pregunta_por_cadenas_vivas(self):
        guardas, _, _, _ = _escanear(_fuentes())
        assert guardas == GUARDAS_PERMITIDAS, (
            f'Llamadas a raiz_con_cadena_viva fuera de su llave: '
            f'{sorted(guardas - GUARDAS_PERMITIDAS)}. Use '
            'SesionConteo.cadenas_vivas_del_almacen(almacen_id, producto_ids, ...).')

    def test_solo_nueva_raiz_construye_una_raiz(self):
        _, raices, _, _ = _escanear(_fuentes())
        assert raices == RAICES_PERMITIDAS, (
            f'Raíces construidas fuera de ConteoService.nueva_raiz: '
            f'{sorted(raices - RAICES_PERMITIDAS)}')

    def test_toda_puerta_que_abre_pregunta_antes(self):
        _, _, abren, _ = _escanear(_fuentes())
        assert abren <= set(ABREN_SIN_GUARDA), (
            f'Abren una cadena sin preguntar por una viva del SKU en el almacén: '
            f'{sorted(abren - set(ABREN_SIN_GUARDA))}')

    def test_nadie_filtra_sesiones_por_hueco_sin_decir_por_que(self):
        _, _, _, por_hueco = _escanear(_fuentes())
        assert por_hueco <= set(POR_HUECO), sorted(por_hueco - set(POR_HUECO))

    def test_los_inventarios_solo_encogen(self):
        _, _, abren, por_hueco = _escanear(_fuentes())
        assert set(ABREN_SIN_GUARDA) <= abren, (
            f'Ya no abren sin guarda, sáquelas: {sorted(set(ABREN_SIN_GUARDA) - abren)}')
        assert set(POR_HUECO) <= por_hueco, (
            f'Ya no filtran por hueco, sáquelas: {sorted(set(POR_HUECO) - por_hueco)}')
        assert len(ABREN_SIN_GUARDA) <= 1 and len(POR_HUECO) <= 3
        assert all(len(m) >= 40 for m in list(ABREN_SIN_GUARDA.values()) + list(POR_HUECO.values()))

    def test_piso(self):
        """Un escáner roto devuelve cero: las puertas conocidas tienen que aparecer."""
        n = sum(1 for _ in _fuentes())
        assert n >= 200
        arbol = ast.parse((APP / 'services' / 'abc_service.py').read_text(encoding='utf-8'))
        con_nueva_raiz = [nm for nm, fn in _funciones(arbol) if _llama(fn, 'nueva_raiz')]
        assert {'ABCService.generar_tareas_conteo_diario',
                'ABCService.watchdog_con_informe'} <= set(con_nueva_raiz)


class TestElDetectorMuerde:

    def _uno(self, src):
        return _escanear([('app/x.py', src)])

    def test_ve_la_guarda_por_hueco(self):
        src = ('def f(p, u):\n'
               '    return SesionConteo.query.filter(SesionConteo.ubicacion_id == u,'
               ' SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True)).first()\n')
        guardas, _, _, por_hueco = self._uno(src)
        assert guardas == {('app/x.py', 'f')} and por_hueco == {('app/x.py', 'f')}

    def test_ve_el_in_por_hueco(self):
        _, _, _, por_hueco = self._uno(
            'def f(us):\n    return SesionConteo.ubicacion_id.in_(us)\n')
        assert por_hueco == {('app/x.py', 'f')}

    def test_ve_la_raiz_construida_a_mano(self):
        _, raices, _, _ = self._uno('def f():\n    return SesionConteo(codigo="x")\n')
        assert raices == {('app/x.py', 'f')}

    def test_no_marca_el_segundo_conteo(self):
        _, raices, _, _ = self._uno(
            'def f():\n    return SesionConteo(codigo="x", es_segundo_conteo=True)\n')
        assert raices == set()

    def test_ve_la_puerta_sin_guarda_y_no_la_que_pregunta(self):
        _, _, abren, _ = self._uno(
            'def abre():\n    ConteoService.nueva_raiz(producto=p)\n'
            'def pregunta():\n'
            '    SesionConteo.query.filter(SesionConteo.cadenas_vivas_del_almacen(a, p,'
            ' incluye_descuadre=True))\n'
            '    ConteoService.nueva_raiz(producto=p)\n')
        assert abren == {('app/x.py', 'abre')}

    def test_docstrings_comentarios_y_anidadas_no_cuentan(self):
        src = ('def f():\n'
               '    """SesionConteo.ubicacion_id == u y raiz_con_cadena_viva()"""\n'
               '    # SesionConteo(codigo=1)\n'
               '    def g():\n'
               '        return SesionConteo(codigo="x")\n'
               '    return 1\n')
        guardas, raices, abren, por_hueco = self._uno(src)
        assert (guardas, por_hueco) == (set(), set())
        assert raices == {('app/x.py', 'g')}, 'la anidada se juzga sola, no como su madre'
