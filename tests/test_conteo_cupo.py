"""
La capacidad fija el ritmo del conteo cíclico (P0-4 y P0-5, 2026-09-23).

## Lo que pasaba

- El generador ABC creaba cada noche `ceil(productos_de_la_clase / frecuencia)`
  conteos sin mirar si el lote anterior se había contado: el rezago crecía sin
  techo. «Forzar todo» creaba todo lo elegible — 8.527 tareas el 24-abr, 4.825
  siguen PENDIENTES en NB1 y saturan todos los huecos.
- El despachador de NB1 repartía solo por antigüedad: una auditoría por
  faltante esperaba detrás de miles de conteos del plan. El de tienda ordenaba
  A→B→C y dejaba las auditorías (sin clase) AL FINAL.
- El watchdog reabría cada noche el mismo SKU mientras siguiera rotando,
  contado o no, y sin cupo.
- «Limpiar cola» era un DELETE físico de toda PENDIENTE: borraba CC2 (su raíz
  quedaba huérfana en SEGUNDO_CONTEO), conteos MANUAL y auditorías.

## Las clases y sus trinquetes

- *«Se crean conteos sin mirar la capacidad»*: todo lo que crea conteos del
  plan (generador, watchdog) pasa por `conteo_politica.tope_de_generacion`.
- *«Cada puerta del pool tiene su propio orden»*: `orden_de_reparto`, una
  definición; `TestUnSoloOrdenDeReparto` lo exige por AST.
- *«La política está escrita en varios sitios»*: `TestUnSoloSitioLeeLaPolitica`
  — nadie fuera de `conteo_politica` lee sus variables ni escribe un mapa
  clase→número (así divergieron 15/90/180, «semanal/mensual/trimestral» y
  «≈ N÷15/día»).
"""
import ast
import json
import pathlib
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

RAIZ = pathlib.Path(__file__).resolve().parents[1]
POLITICA = RAIZ / 'app' / 'services' / 'conteo_politica.py'


# ─────────────────────────────────────────────────────────────────────────────
# Mundo: NB1 con una ubicación virtual SIESA-GENERAL, como producción
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _config_limpia(monkeypatch):
    """Ninguna variable del entorno del desarrollador se cuela en los tests."""
    for nombre in ('CONTEO_CUPO_DIARIO', 'CONTEO_CUPO_POR_BODEGA',
                   'CONTEO_INTERVALOS_DIAS', 'CONTEO_WATCHDOG_DIAS_SIN_REABRIR'):
        monkeypatch.delenv(nombre, raising=False)


class Mundo:
    def __init__(self, db, almacen):
        from app.models.ubicacion import Ubicacion
        self.db = db
        self.almacen = almacen
        self.ub = Ubicacion(codigo='SIESA-GENERAL', almacen_id=almacen.id, tipo_zona='GENERAL',
                            stock_minimo=0, stock_maximo=999999, secuencia_ruteo=1, activo=True)
        db.session.add(self.ub)
        db.session.commit()
        self.productos = {}

    def producto(self, codigo, clase, stock=10):
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC
        p = Producto(codigo=codigo, nombre=f'Producto {codigo}', codigo_siesa=codigo,
                     activo=True, clasificacion_abc=clase)
        self.db.session.add(p)
        self.db.session.flush()
        if clase:
            self.db.session.add(ProductoClasificacionABC(
                producto_id=p.id, almacen_id=self.almacen.id, clasificacion=clase))
        self.db.session.add(UbicacionProducto(ubicacion_id=self.ub.id, producto_id=p.id,
                                              cantidad=stock, reservado=0, bloqueado=0))
        self.db.session.commit()
        self.productos[codigo] = p
        return p

    def contado_hace(self, codigo, dias, estado='MATCH'):
        """Historia: un conteo cerrado hace `dias` días (lo que lee
        `ultimo_conteo_por_hueco`)."""
        from app.models.conteo import SesionConteo
        p = self.productos[codigo]
        s = SesionConteo(codigo=f'HIST-{codigo}-{dias}', tipo='DIARIO_ABC',
                         clasificacion_abc=p.clasificacion_abc, ubicacion_id=self.ub.id,
                         almacen_id=self.almacen.id, producto_id=p.id,
                         producto_codigo_siesa=codigo, estado=estado,
                         fecha_creacion=datetime.utcnow() - timedelta(days=dias + 1),
                         fecha_cierre=datetime.utcnow() - timedelta(days=dias))
        self.db.session.add(s)
        self.db.session.commit()
        return s

    def sesion(self, codigo_producto, **kw):
        """Una sesión en un estado que el test necesita y que ningún servicio
        deja en un solo paso (una raíz en SEGUNDO_CONTEO con su CC2, p. ej.)."""
        from app.models.conteo import SesionConteo
        p = self.productos[codigo_producto]
        datos = dict(codigo=f'S-{codigo_producto}-{len(kw)}-{kw.get("estado", "")}-{kw.get("es_segundo_conteo", False)}',
                     tipo='DIARIO_ABC', clasificacion_abc=p.clasificacion_abc,
                     ubicacion_id=self.ub.id, almacen_id=self.almacen.id, producto_id=p.id,
                     producto_codigo_siesa=codigo_producto, estado='PENDIENTE')
        datos.update(kw)
        s = SesionConteo(**datos)
        self.db.session.add(s)
        self.db.session.commit()
        return s

    def operario(self, email, rol='operario', almacen=None):
        from app.models.usuario import Usuario
        u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                    rol=rol, puede_picar=True, almacen_id=(almacen or self.almacen).id,
                    activo=True)
        self.db.session.add(u)
        self.db.session.commit()
        return u

    def picks(self, codigo, cuantos):
        from app.models.picking import TareaPicking
        p = self.productos[codigo]
        hace_dos_dias = datetime.utcnow() - timedelta(days=2)
        for i in range(cuantos):
            self.db.session.add(TareaPicking(
                codigo=f'PK-{codigo}-{i:03d}', producto_id=p.id, cantidad_solicitada=1,
                cantidad_recogida=1, ubicacion_id=self.ub.id, almacen_id=self.almacen.id,
                estado='COMPLETADO', fecha_completado=hace_dos_dias))
        self.db.session.commit()


@pytest.fixture
def nb1(db, almacen):
    return Mundo(db, almacen)


def _abc():
    from app.services.abc_service import ABCService
    return ABCService


def _creados(nb1, tipo='DIARIO_ABC'):
    """Códigos de producto con un conteo PENDIENTE de ese tipo."""
    from app.models.conteo import SesionConteo
    return sorted(s.producto_codigo_siesa for s in SesionConteo.query.filter_by(
        almacen_id=nb1.almacen.id, tipo=tipo, estado='PENDIENTE').all())


def _pendientes(nb1):
    from app.models.conteo import SesionConteo
    return SesionConteo.query.filter_by(almacen_id=nb1.almacen.id, estado='PENDIENTE').count()


# ─────────────────────────────────────────────────────────────────────────────
# 1 · El cupo manda
# ─────────────────────────────────────────────────────────────────────────────

class TestElCupoManda:

    def test_crea_como_maximo_el_cupo(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '4')
        for i in range(10):
            nb1.producto(f'A{i:02d}', 'A')
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['tareas_creadas'] == 4, r
        assert r['omitidos_por_cupo'] == 6, 'lo que no entró tiene que declararse'
        assert 'esperan cupo' in r['mensaje']
        assert _pendientes(nb1) == 4

    def test_la_segunda_corrida_no_suma_otro_lote(self, nb1, monkeypatch):
        """El defecto de origen: cada noche otro lote encima del que nadie contó."""
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '4')
        for i in range(10):
            nb1.producto(f'A{i:02d}', 'A')
        _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['tareas_creadas'] == 0
        assert r['cupo']['motivo'] == 'CUPO_CUBIERTO', r['cupo']
        assert _pendientes(nb1) == 4

    def test_descuenta_las_pendientes_de_cualquier_tipo(self, nb1, monkeypatch):
        from app.services.conteo_service import ConteoService
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '5')
        for i in range(10):
            nb1.producto(f'A{i:02d}', 'A')
        nb1.producto('M1', 'B')
        nb1.producto('M2', 'B')
        ConteoService.crear_conteo_manual(nb1.almacen.id, 'M1')
        ConteoService.crear_conteo_manual(nb1.almacen.id, 'M2')
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['cupo']['pendientes_vivas'] == 2
        assert r['tareas_creadas'] == 3, r

    def test_con_dos_dias_de_rezago_no_genera_y_lo_dice(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '3')
        for i in range(12):
            nb1.producto(f'A{i:02d}', 'A')
        for i in range(6):
            nb1.sesion(f'A{i:02d}')          # 6 pendientes = 2 días de cupo
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['tareas_creadas'] == 0
        assert r['cupo']['motivo'] == 'REZAGO', r['cupo']
        assert r['mensaje'].startswith('no se generó: hay 6 pendientes = 2.0 días de cupo'), r['mensaje']

    def test_cupo_por_bodega_gana_y_cero_no_programa(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '10')
        monkeypatch.setenv('CONTEO_CUPO_POR_BODEGA', json.dumps({'nb1': 2}))
        for i in range(5):
            nb1.producto(f'A{i:02d}', 'A')
        assert _abc().generar_tareas_conteo_diario(nb1.almacen.id)['tareas_creadas'] == 2
        monkeypatch.setenv('CONTEO_CUPO_POR_BODEGA', json.dumps({'NB1': 0}))
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['tareas_creadas'] == 0 and r['cupo']['motivo'] == 'SIN_CUPO', r

    def test_configuracion_ilegible_usa_el_defecto_y_lo_declara(self, nb1, monkeypatch):
        from app.services import conteo_politica as pol
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', 'sesenta')
        monkeypatch.setenv('CONTEO_INTERVALOS_DIAS', '{"A": 0, "Z": 3')
        nb1.producto('A00', 'A')
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['cupo']['cupo_diario'] == pol.CUPO_DIARIO_DEFECTO
        assert r['intervalos_dias'] == pol.INTERVALO_DIAS_DEFECTO
        assert any('CONTEO_CUPO_DIARIO' in a for a in r['advertencias_configuracion'])
        assert any('CONTEO_INTERVALOS_DIAS' in a for a in r['advertencias_configuracion'])

    def test_intervalos_parciales_desde_el_entorno(self, monkeypatch):
        from app.services import conteo_politica as pol
        monkeypatch.setenv('CONTEO_INTERVALOS_DIAS', '{"a": 90}')
        assert pol.intervalos_dias() == {'A': 90, 'B': 300, 'C': 600}
        assert pol.intervalo_dias(None) == 90, 'clase desconocida → el más exigente'


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Qué se elige primero
# ─────────────────────────────────────────────────────────────────────────────

class TestOrdenDeSeleccion:

    def test_nunca_contados_de_a_primero(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '1')
        nb1.producto('C-NUNCA', 'C')
        nb1.producto('B-NUNCA', 'B')
        nb1.producto('A-VIEJO', 'A')
        nb1.contado_hace('A-VIEJO', 900)          # 6 intervalos de A de atraso
        nb1.producto('A-NUNCA', 'A')
        _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert _creados(nb1) == ['A-NUNCA']

    def test_despues_el_mayor_atraso_relativo_no_la_clase(self, nb1, monkeypatch):
        """A contado hace 400 d = 2,67 intervalos; B hace 900 d = 3,0; C hace
        700 d = 1,17. Gana B: el atraso se mide contra el intervalo de su clase."""
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '1')
        for codigo, clase, dias in (('A1', 'A', 400), ('B1', 'B', 900), ('C1', 'C', 700)):
            nb1.producto(codigo, clase)
            nb1.contado_hace(codigo, dias)
        _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert _creados(nb1) == ['B1']
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '2')
        _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert _creados(nb1) == ['A1', 'B1']

    def test_lo_que_esta_al_dia_no_se_programa(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '10')
        nb1.producto('A-RECIEN', 'A')
        nb1.contado_hace('A-RECIEN', 10)
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id)
        assert r['tareas_creadas'] == 0 and r['omitidos_por_intervalo'] == 1, r


class TestAdelantarRespetaElCupo:

    def test_adelantar_toma_lo_al_dia_pero_nunca_mas_que_el_cupo(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '3')
        for i in range(10):
            nb1.producto(f'A{i:02d}', 'A')
            nb1.contado_hace(f'A{i:02d}', 5 + i)   # todos al día
        assert _abc().generar_tareas_conteo_diario(nb1.almacen.id)['tareas_creadas'] == 0
        r = _abc().generar_tareas_conteo_diario(nb1.almacen.id, adelantar=True)
        assert (r['tareas_creadas'], r['modo']) == (3, 'adelantado'), r
        # Los más atrasados primero: contados hace 14, 13 y 12 días.
        assert _creados(nb1) == ['A07', 'A08', 'A09']

    def test_el_boton_viejo_forzar_todo_tampoco_pasa_el_cupo(self, app, client, nb1, monkeypatch,
                                                             usuario_admin, jwt_token_admin):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '2')
        for i in range(8):
            nb1.producto(f'A{i:02d}', 'A')
        r = client.post('/api/conteo/abc/generar-todas',
                        json={'almacen_id': nb1.almacen.id, 'forzar_todo': True},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 201, r.get_json()
        assert r.get_json()['total_tareas_creadas'] == 2
        assert _pendientes(nb1) == 2


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Los eventos van primero en el reparto
# ─────────────────────────────────────────────────────────────────────────────

def _auditoria(nb1, codigo):
    """Una auditoría por faltante como la abre el picking real."""
    from app.models.picking import TareaPicking
    from app.services.conteo_service import ConteoService
    p = nb1.productos[codigo]
    # La tarea ya se cerró (el picker reportó el faltante): no compite en el
    # pool de picking con el conteo que se está probando.
    t = TareaPicking(codigo=f'PK-FALT-{codigo}', producto_id=p.id, cantidad_solicitada=1,
                     ubicacion_id=nb1.ub.id, almacen_id=nb1.almacen.id, estado='COMPLETADO')
    nb1.db.session.add(t)
    nb1.db.session.commit()
    s = ConteoService.generar_auditoria_por_excepcion(t.id, nb1.ub.id, p.id, nb1.almacen.id)
    nb1.db.session.commit()
    return s


class TestEventosPrimeroEnElPool:

    def _cola(self, nb1, monkeypatch):
        """C y B viejos del plan, A más nuevo, un MANUAL y una auditoría al final."""
        from app.services.conteo_service import ConteoService
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '10')
        for codigo, clase in (('C1', 'C'), ('B1', 'B'), ('A1', 'A'), ('M1', 'B'), ('X1', None)):
            nb1.producto(codigo, clase)
        hace = datetime.utcnow() - timedelta(days=30)
        nb1.sesion('C1', fecha_creacion=hace)
        nb1.sesion('B1', fecha_creacion=hace + timedelta(hours=1))
        nb1.sesion('A1', fecha_creacion=hace + timedelta(days=5))
        ConteoService.crear_conteo_manual(nb1.almacen.id, 'M1')
        _auditoria(nb1, 'X1')

    def test_nb1_reparte_auditoria_manual_y_despues_a_b_c(self, nb1, monkeypatch):
        from app.models.conteo import SesionConteo
        from app.services.mobile_service import MobileService
        self._cola(nb1, monkeypatch)
        orden = []
        for i in range(5):
            op = nb1.operario(f'nb1-{i}@test.com')
            t = MobileService.get_tarea_actual(op.id)
            assert t and t['tipo'] == 'CONTEO', t
            orden.append(SesionConteo.query.get(t['id']).producto_codigo_siesa)
        assert orden == ['X1', 'M1', 'A1', 'B1', 'C1']

    def test_tienda_ya_no_deja_la_auditoria_al_final(self, nb1, monkeypatch):
        from app.models.conteo import SesionConteo
        from app.services.mobile_service import MobileService
        self._cola(nb1, monkeypatch)
        op = nb1.operario('tienda@test.com', rol='picker_traslado')
        d = MobileService._next_conteo_tienda(op.id, nb1.almacen.id)
        assert SesionConteo.query.get(d['id']).tipo == 'EXCEPCION_PICKING'

    def test_un_faltante_sobre_un_conteo_del_plan_pendiente_lo_vuelve_evento(self, nb1, monkeypatch):
        """En producción cada SKU de NB1 tiene un conteo del plan PENDIENTE (las
        4.825 de abril): la auditoría se encontraba con él, lo devolvía tal cual
        y quedaba con la prioridad de su clase, detrás de miles."""
        from app.models.conteo import SesionConteo
        from app.services.mobile_service import MobileService
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '10')
        nb1.producto('A-VIEJO', 'A')
        nb1.producto('C-FALTA', 'C')
        hace = datetime.utcnow() - timedelta(days=30)
        nb1.sesion('A-VIEJO', fecha_creacion=hace)
        plan_c = nb1.sesion('C-FALTA', fecha_creacion=hace + timedelta(days=1))
        s = _auditoria(nb1, 'C-FALTA')
        assert s.id == plan_c.id, 'no abre otra cadena sobre el mismo hueco'
        assert (s.tipo, s.tarea_picking_id is not None) == ('EXCEPCION_PICKING', True)
        t = MobileService.get_tarea_actual(nb1.operario('falta@test.com').id)
        assert SesionConteo.query.get(t['id']).producto_codigo_siesa == 'C-FALTA'

    def test_la_cola_asignada_y_asignar_lote_usan_el_mismo_orden(self, app, client, nb1, monkeypatch,
                                                                 usuario_admin, jwt_token_admin):
        from app.models.conteo import SesionConteo
        self._cola(nb1, monkeypatch)
        op = nb1.operario('lote@test.com')
        r = client.post('/api/conteo/asignar-lote',
                        json={'operario_id': op.id, 'almacen_id': nb1.almacen.id, 'limite': 2},
                        headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code == 200 and r.get_json()['asignadas'] == 2, r.get_json()
        asignadas = sorted(s.producto_codigo_siesa for s in
                           SesionConteo.query.filter_by(operario_id=op.id).all())
        assert asignadas == ['M1', 'X1']
        with app.app_context():
            tok = create_access_token(identity=str(op.id))
        mis = client.get('/api/conteo/mis-tareas', headers={'Authorization': f'Bearer {tok}'}).get_json()
        assert [t['producto_codigo'] for t in mis['tareas']] == ['X1', 'M1']


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Watchdog: no reabre lo recién contado, y cabe en el cupo
# ─────────────────────────────────────────────────────────────────────────────

class TestWatchdogConMemoriaYCupo:

    def test_no_reabre_un_hueco_contado_hace_menos_de_n_dias(self, nb1):
        nb1.producto('C-ROTA', 'C')
        nb1.picks('C-ROTA', 30)
        nb1.contado_hace('C-ROTA', 5)
        inf = _abc().watchdog_con_informe(nb1.almacen.id)
        assert inf['overrides'] == [] and inf['omitidos_recien_contados'] == 1, inf

    def test_reabre_pasado_el_plazo(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_WATCHDOG_DIAS_SIN_REABRIR', '3')
        nb1.producto('C-ROTA', 'C')
        nb1.picks('C-ROTA', 30)
        nb1.contado_hace('C-ROTA', 5)
        assert len(_abc().watchdog_anomalias(nb1.almacen.id)) == 1

    def test_cabe_en_el_cupo_y_declara_lo_que_no(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '1')
        nb1.producto('C-MUCHO', 'C')
        nb1.producto('C-POCO', 'C')
        nb1.picks('C-MUCHO', 40)
        nb1.picks('C-POCO', 12)
        inf = _abc().watchdog_con_informe(nb1.almacen.id)
        assert [o['producto_codigo'] for o in inf['overrides']] == ['C-MUCHO'], 'el más anómalo primero'
        assert inf['omitidos_por_cupo'] == 1

    def test_la_corrida_diaria_da_el_cupo_primero_al_watchdog(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '3')
        nb1.producto('C-ROTA', 'C')
        nb1.picks('C-ROTA', 30)
        for i in range(5):
            nb1.producto(f'A{i:02d}', 'A')
        r = _abc().generar_todas_las_clases(nb1.almacen.id)
        assert r['por_clase']['watchdog']['overrides'] == 1
        assert r['plan']['tareas_creadas'] == 2, 'el plan toma lo que el watchdog dejó'
        assert _pendientes(nb1) == 3


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Cancelar el rezago, no borrarlo
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def rezago(nb1, monkeypatch):
    """Una muestra de todo lo que puede haber PENDIENTE en un almacén. Solo
    las dos primeras se pueden cancelar."""
    from app.services.conteo_service import ConteoService
    monkeypatch.setenv('CONTEO_CUPO_DIARIO', '50')
    nb1.producto('PLAN', 'A')
    nb1.producto('WD', 'C')
    for codigo, clase in (('DUENO', 'A'), ('MAN', 'B'), ('AUD', None), ('CADENA', 'A'),
                          ('CONHIJO', 'B'), ('ENPROC', 'C')):
        nb1.producto(codigo, clase)
    nb1.picks('WD', 30)
    _abc().watchdog_anomalias(nb1.almacen.id)                          # WD → WATCHDOG_ABC
    op = nb1.operario('dueno@test.com')
    nb1.sesion('DUENO', operario_id=op.id)                            # asignada
    ConteoService.crear_conteo_manual(nb1.almacen.id, 'MAN')          # MANUAL
    _auditoria(nb1, 'AUD')                                            # EXCEPCION_PICKING
    raiz = nb1.sesion('CADENA', estado='SEGUNDO_CONTEO')              # raíz con su CC2 vivo
    nb1.sesion('CADENA', es_segundo_conteo=True, sesion_origen_id=raiz.id)
    padre = nb1.sesion('CONHIJO')                                     # PENDIENTE con hijo
    nb1.sesion('CONHIJO', es_segundo_conteo=True, sesion_origen_id=padre.id, estado='CANCELADO')
    nb1.sesion('ENPROC', estado='EN_PROCESO', operario_id=op.id)
    # El plan, con el generador real: solo PLAN está vencido y libre.
    assert _abc().generar_tareas_conteo_diario(nb1.almacen.id)['tareas_creadas'] == 1
    return nb1


def _estados(nb1):
    from app.models.conteo import SesionConteo
    nb1.db.session.expire_all()
    return {(s.producto_codigo_siesa, s.es_segundo_conteo): s.estado
            for s in SesionConteo.query.filter_by(almacen_id=nb1.almacen.id).all()
            if not s.codigo.startswith('HIST-')}


class TestCancelarElRezago:

    def test_solo_cancela_raices_del_plan_sin_dueno_ni_hijos(self, rezago, usuario_admin):
        antes = _estados(rezago)
        r = _abc().cancelar_rezago(rezago.almacen.id, motivo='corte del backlog de abril',
                                   usuario_id=usuario_admin.id)
        assert r['canceladas'] == 2, r
        despues = _estados(rezago)
        cambiaron = sorted(k[0] for k in antes if antes[k] != despues[k])
        assert cambiaron == ['PLAN', 'WD'], cambiaron
        assert despues[('PLAN', False)] == despues[('WD', False)] == 'CANCELADO'
        assert r['no_se_tocan'] == {'verificacion_cc2_cc3': 1, 'auditoria_por_faltante': 1,
                                    'conteo_manual': 1, 'en_proceso': 1,
                                    'asignada_a_un_operario': 1, 'con_cadena_iniciada': 1}, r

    def test_cancela_no_borra_y_deja_rastro(self, rezago, usuario_admin):
        from app.models.conteo import SesionConteo
        total = SesionConteo.query.count()
        _abc().cancelar_rezago(rezago.almacen.id, motivo='corte', usuario_id=usuario_admin.id)
        assert SesionConteo.query.count() == total, 'no se borra ninguna fila'
        s = SesionConteo.query.filter_by(producto_codigo_siesa='PLAN').one()
        assert s.fecha_cierre is not None and s.editado_por == usuario_admin.id
        assert s.motivo_edicion == 'CANCELADO (rezago del plan): corte'

    def test_sin_motivo_no_cancela(self, rezago, usuario_admin):
        with pytest.raises(ValueError):
            _abc().cancelar_rezago(rezago.almacen.id, motivo='  ', usuario_id=usuario_admin.id)
        assert _estados(rezago)[('PLAN', False)] == 'PENDIENTE'

    def test_la_vista_previa_es_lo_que_despues_se_cancela(self, app, client, rezago,
                                                          usuario_admin, jwt_token_admin):
        from app.models.conteo import SesionConteo
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        prev = client.get(f'/api/conteo/abc/limpiar-pendientes/preview?almacen_id={rezago.almacen.id}',
                          headers=h).get_json()
        assert prev['a_cancelar'] == 2 and prev['ejecutado'] is False, prev
        # El watchdog abre con clase A (override): las dos cancelables son A.
        assert prev['por_clase'] == {'A': 2}, prev
        assert prev['por_tipo'] == {'DIARIO_ABC': 1, 'WATCHDOG_ABC': 1}, prev
        assert _estados(rezago)[('PLAN', False)] == 'PENDIENTE', 'la vista previa no toca nada'
        antes = {s.id for s in SesionConteo.query.filter_by(estado='CANCELADO').all()}
        r = client.post('/api/conteo/abc/limpiar-pendientes', headers=h,
                        json={'almacen_id': rezago.almacen.id, 'motivo': 'corte',
                              'esperadas': prev['a_cancelar']})
        assert r.status_code == 200, r.get_json()
        nuevas = {s.id for s in SesionConteo.query.filter_by(estado='CANCELADO').all()} - antes
        plan = _abc().plan_cancelar_rezago(rezago.almacen.id)   # ya vacío
        assert plan['a_cancelar'] == 0
        assert len(nuevas) == prev['a_cancelar'] == r.get_json()['canceladas']
        assert {k: v for k, v in r.get_json().items() if k in ('por_clase', 'por_tipo')} == \
               {k: v for k, v in prev.items() if k in ('por_clase', 'por_tipo')}

    def test_si_el_rezago_cambio_no_cancela_nada(self, app, client, rezago, jwt_token_admin):
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.post('/api/conteo/abc/limpiar-pendientes', headers=h,
                        json={'almacen_id': rezago.almacen.id, 'motivo': 'corte', 'esperadas': 7})
        assert r.status_code == 409, r.get_json()
        assert _estados(rezago)[('PLAN', False)] == 'PENDIENTE'
        r = client.post('/api/conteo/abc/limpiar-pendientes', headers=h,
                        json={'almacen_id': rezago.almacen.id})
        assert r.status_code == 400

    def test_filtra_por_clase(self, rezago, usuario_admin):
        assert _abc().plan_cancelar_rezago(rezago.almacen.id, 'A')['a_cancelar'] == 2
        plan_b = _abc().plan_cancelar_rezago(rezago.almacen.id, 'B')
        assert plan_b['a_cancelar'] == 0
        assert plan_b['no_se_tocan'] == {'conteo_manual': 1, 'con_cadena_iniciada': 1}, plan_b

    def test_un_operario_no_puede(self, app, client, rezago, jwt_token):
        h = {'Authorization': f'Bearer {jwt_token}'}
        assert client.get(f'/api/conteo/abc/limpiar-pendientes/preview?almacen_id={rezago.almacen.id}',
                          headers=h).status_code == 403
        assert client.post('/api/conteo/abc/limpiar-pendientes', headers=h,
                           json={'almacen_id': rezago.almacen.id, 'motivo': 'x'}).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 6 · La pantalla muestra el plan vigente
# ─────────────────────────────────────────────────────────────────────────────

class TestLaPantallaMuestraElPlanVigente:

    def test_resumen_abc_habla_de_los_intervalos_configurados(self, nb1, monkeypatch):
        monkeypatch.setenv('CONTEO_INTERVALOS_DIAS', '{"A": 120}')
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '45')
        r = _abc().resumen_abc(nb1.almacen.id)
        assert r['distribucion_abc']['A']['descripcion'] == 'Alta rotación — contar cada 120 días'
        assert r['distribucion_abc']['C']['intervalo_dias'] == 600
        assert r['plan']['cupo_diario'] == 45
        texto = json.dumps(r, ensure_ascii=False)
        for falso in ('semanal', 'mensual', 'trimestral'):
            assert falso not in texto

    def test_estadisticas_ponen_cupo_contra_ritmo(self, nb1, monkeypatch):
        from app.services.metricas.conteo import calcular_estadisticas_conteo
        monkeypatch.setenv('CONTEO_CUPO_DIARIO', '60')
        nb1.producto('A00', 'A')
        c = calcular_estadisticas_conteo()['carga_cobertura']
        (fila,) = c['por_almacen']
        assert (fila['cupo_diario'], fila['exigencia_diaria_plan']) == (60, 1)
        assert fila['ritmo_real_por_dia'] == 0
        assert c['plan']['intervalos_dias'] == {'A': 150, 'B': 300, 'C': 600}


_ARNES = r"""
const fs = require('fs'); const vm = require('vm');
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0];
const ctx = { console, document: { getElementById: () => null }, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
if (args[1] === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
const X = '<img src=x onerror=alert(1)>';
const resumen = vm.runInContext('_abcResumenHtml', ctx)({
  fuente: X,
  distribucion_abc: { A: { total_productos: 7, intervalo_dias: 150, descripcion: 'contar cada 150 días' },
                      B: { total_productos: X, descripcion: X }, C: { descripcion: X } },
  plan: { cupo_diario: 60, pendientes_vivas: X, dias_de_cupo_pendientes: 2, hora_del_generador: X,
          generaria_hoy: 0, mensaje: X, advertencias: [X] },
});
const preview = vm.runInContext('_limpiarColaPreviewHtml', ctx)({
  a_cancelar: 4825, por_clase: { [X]: 1, A: 3 }, por_antiguedad_dias: { '>30': 4825, [X]: 1 },
  no_se_tocan: { conteo_manual: 2, [X]: 1 },
}, X);
const html = resumen + preview;
console.log(JSON.stringify({
  crudos: (html.match(/<img/g) || []).length,
  escapados: (html.match(/&lt;img/g) || []).length,
  intervalo: resumen.includes('contar cada 150 días'),
  cupo: resumen.includes('>60<'),
  total: preview.includes('4825'),
  manual: preview.includes('conteos manuales'),
}));
"""


def _pintar(modo=''):
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES, '--', str(pwa), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPantallaDelPlanEscapa:

    def test_pinta_lo_del_servidor_y_escapa(self):
        r = _pintar()
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 9, f'piso: el arnés dejó de pintar ({r})'
        assert r['intervalo'] and r['cupo'] and r['total'] and r['manual'], r

    def test_el_arnes_muerde_con_esc_roto(self):
        assert _pintar('esc-roto')['crudos'] >= 9

    def test_la_pantalla_no_escribe_los_intervalos_a_mano(self):
        """Los números del plan vienen del servidor. Antes estaban escritos en
        tres sitios que no coincidían entre sí ni con el generador."""
        html = (RAIZ / 'app' / 'static' / 'pwa' / 'index.html').read_text(encoding='utf-8')
        js = (RAIZ / 'app' / 'static' / 'pwa' / 'conteo.js').read_text(encoding='utf-8')
        for viejo in ('N÷15', 'N÷90', 'N÷180', 'cada 15 días', 'cada 90 días', 'cada 180 días',
                      'Forzar todo'):
            assert viejo not in html and viejo not in js, viejo


# ─────────────────────────────────────────────────────────────────────────────
# Trinquetes — la política vive en un solo sitio, el orden también
# ─────────────────────────────────────────────────────────────────────────────

def _nombra_variable_de_politica(nodo, nombres) -> bool:
    """Un literal que ES el nombre de una variable de la política. Cubre las
    tres escrituras de leerla —`os.environ.get('X')`, `os.getenv('X')`,
    `os.environ['X']`— y también la indirecta, `NOMBRE = 'X'` + `get(NOMBRE)`,
    que un detector que mira solo el primer argumento de `get` no ve (la
    política misma lee así). Igualdad exacta: un docstring o un mensaje que la
    MENCIONA no es una lectura."""
    return isinstance(nodo, ast.Constant) and isinstance(nodo.value, str) and nodo.value in nombres


def _mapa_clase_numero(nodo) -> bool:
    """Un dict literal con claves de clase ABC y valores ENTEROS: la forma de
    `FRECUENCIA_DIAS`, de un umbral por clase o de un rango de reparto."""
    if not isinstance(nodo, ast.Dict) or len(nodo.keys) < 2:
        return False
    claves = [k.value for k in nodo.keys if isinstance(k, ast.Constant)]
    if len(claves) != len(nodo.keys) or not set(claves) <= {'A', 'B', 'C'}:
        return False
    return all(isinstance(v, ast.Constant) and type(v.value) is int for v in nodo.values)


def _violaciones(fuente: str, nombres) -> list:
    arbol = ast.parse(fuente)
    return [n.lineno for n in ast.walk(arbol)
            if _nombra_variable_de_politica(n, nombres) or _mapa_clase_numero(n)]


def _nombres_de_variables():
    from app.services.conteo_politica import VARIABLES_DE_ENTORNO
    return set(VARIABLES_DE_ENTORNO)


class TestUnSoloSitioLeeLaPolitica:

    def test_nadie_fuera_de_la_politica_la_lee_ni_la_escribe(self):
        nombres = _nombres_de_variables()
        fuera = []
        for ruta in sorted((RAIZ / 'app').rglob('*.py')):
            if ruta == POLITICA:
                continue
            for linea in _violaciones(ruta.read_text(encoding='utf-8'), nombres):
                fuera.append(f'{ruta.relative_to(RAIZ).as_posix()}:{linea}')
        assert not fuera, (
            'Leen la configuración del conteo o escriben un mapa clase→número fuera '
            f'de app/services/conteo_politica.py: {fuera}. Así divergieron 15/90/180, '
            '«semanal/mensual/trimestral» y «≈ N÷15/día». Usar conteo_politica.')

    def test_piso_el_escaner_ve_la_politica(self):
        """Si el escáner se desincroniza devuelve cero, y cero se lee igual que
        «nadie la duplica». Sobre la política misma tiene que ver sus mapas
        (intervalos, rango, umbral) y sus lecturas de entorno."""
        arbol = ast.parse(POLITICA.read_text(encoding='utf-8'))
        mapas = sum(1 for n in ast.walk(arbol) if _mapa_clase_numero(n))
        nombres = sum(1 for n in ast.walk(arbol)
                      if _nombra_variable_de_politica(n, _nombres_de_variables()))
        assert mapas >= 3, mapas
        assert nombres >= 4, nombres
        assert len(_nombres_de_variables()) == 4

    @pytest.mark.parametrize('codigo', [
        "x = os.environ.get('CONTEO_CUPO_DIARIO', '60')",
        "x = os.getenv('CONTEO_INTERVALOS_DIAS')",
        "x = os.environ['CONTEO_CUPO_POR_BODEGA']",
        "_ENV = 'CONTEO_WATCHDOG_DIAS_SIN_REABRIR'\nx = os.environ.get(_ENV)",
        "FRECUENCIA_DIAS = {'A': 15, 'B': 90, 'C': 180}",
        "UMBRAL = {'B': 25, 'C': 10}",
        "orden = case({'A': 1, 'B': 2, 'C': 3}, value=x)",
    ])
    def test_el_detector_marca(self, codigo):
        assert _violaciones(codigo, _nombres_de_variables()), codigo

    @pytest.mark.parametrize('codigo', [
        "x = os.environ.get('PEDIDO_LINEAS_PARALELIZABLE', '6')",
        "PORCENTAJE = {'A': 0.20, 'B': 0.12, 'C': 0.08}",
        "texto = {'A': 'Alta', 'B': 'Media', 'C': 'Baja'}",
        "dist = {'A': dist['A'], 'B': dist['B']}",
        "# FRECUENCIA_DIAS = {'A': 15}\nx = 1",
        "def f():\n    \"\"\"Lee CONTEO_CUPO_DIARIO de la política.\"\"\"\n",
        "raise ValueError(f'CONTEO_CUPO_DIARIO={x!r} no es un entero')",
    ])
    def test_el_detector_no_marca(self, codigo):
        assert _violaciones(codigo, _nombres_de_variables()) == [], codigo


def _padres(arbol):
    padres = {}
    for n in ast.walk(arbol):
        for h in ast.iter_child_nodes(n):
            padres[h] = n
    return padres


def _llamadas_de_la_cadena(tope):
    """Los `Call` de una cadena de métodos `a.b(...).c(...).d(...)`."""
    out, n = [], tope
    while isinstance(n, ast.Call):
        out.append(n)
        n = n.func.value if isinstance(n.func, ast.Attribute) else None
    return out


def _cadenas_del_pool_sin_orden(fuente: str) -> list:
    """Consultas que filtran con `filtros_pool_sin_dueno` (reparten) y no
    ordenan con `orden_de_reparto`. Por cadena, no por función: el intercalado
    vive en la misma función que el despachador de NB1."""
    arbol = ast.parse(fuente)
    padres = _padres(arbol)
    malas = []
    for n in ast.walk(arbol):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'filter'
                and any(isinstance(x, ast.Attribute) and x.attr == 'filtros_pool_sin_dueno'
                        for a in n.args for x in ast.walk(a))):
            continue
        tope = n
        while (isinstance(padres.get(tope), ast.Attribute)
               and isinstance(padres.get(padres[tope]), ast.Call)
               and padres[padres[tope]].func is padres[tope]):
            tope = padres[padres[tope]]
        ordena = any(c.func.attr == 'order_by'
                     and any(isinstance(x, (ast.Name, ast.Attribute))
                             and getattr(x, 'id', getattr(x, 'attr', None)) == 'orden_de_reparto'
                             for a in c.args for x in ast.walk(a))
                     for c in _llamadas_de_la_cadena(tope) if isinstance(c.func, ast.Attribute))
        if not ordena:
            malas.append(n.lineno)
    return malas


def _orden_propio(fuente: str) -> list:
    """Un `case(...)` sobre la clase o el tipo de un conteo: un orden de reparto
    escrito a mano."""
    arbol = ast.parse(fuente)
    return [n.lineno for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and ((isinstance(n.func, ast.Name) and n.func.id.lstrip('_').endswith('case'))
                 or (isinstance(n.func, ast.Attribute) and n.func.attr == 'case'))
            and any(isinstance(x, ast.Attribute) and x.attr in ('clasificacion_abc', 'tipo')
                    and isinstance(x.value, ast.Name) and x.value.id == 'SesionConteo'
                    for x in ast.walk(n))]


class TestUnSoloOrdenDeReparto:

    def _barrer(self):
        sin_orden, propios, usos = [], [], 0
        for ruta in sorted((RAIZ / 'app').rglob('*.py')):
            fuente = ruta.read_text(encoding='utf-8')
            rel = ruta.relative_to(RAIZ).as_posix()
            sin_orden += [f'{rel}:{l}' for l in _cadenas_del_pool_sin_orden(fuente)]
            if ruta != POLITICA:
                propios += [f'{rel}:{l}' for l in _orden_propio(fuente)]
            usos += sum(1 for n in ast.walk(ast.parse(fuente)) if isinstance(n, ast.Call)
                        and getattr(n.func, 'id', getattr(n.func, 'attr', None)) == 'orden_de_reparto')
        return sin_orden, propios, usos

    def test_toda_puerta_que_reparte_ordena_con_la_politica(self):
        sin_orden, _, _ = self._barrer()
        assert not sin_orden, (
            f'Reparten conteos sin `orden_de_reparto`: {sin_orden}. Una auditoría por '
            'faltante tiene que ir primero en toda puerta.')

    def test_nadie_escribe_su_propio_orden(self):
        _, propios, _ = self._barrer()
        assert not propios, f'Orden de reparto escrito a mano: {propios}'

    def test_piso_el_orden_se_usa_donde_se_reparte(self):
        """NB1, intercalado, tienda, cola asignada, asignar-lote, mis-tareas."""
        _, _, usos = self._barrer()
        assert usos >= 6, f'solo {usos} usos de orden_de_reparto: el barrido está ciego'

    def test_el_detector_marca_el_pool_sin_orden(self):
        fuente = ("def f(op, alm):\n"
                  "    return (SesionConteo.query\n"
                  "            .filter(*ConteoService.filtros_pool_sin_dueno(op, alm))\n"
                  "            .order_by(SesionConteo.fecha_creacion.asc()).first())\n")
        assert _cadenas_del_pool_sin_orden(fuente) == [2]
        sin_nada = "def f(op, alm):\n    return Q.filter(*C.filtros_pool_sin_dueno(op, alm)).first()\n"
        assert _cadenas_del_pool_sin_orden(sin_nada) == [2]

    def test_el_detector_no_marca_el_que_ordena(self):
        fuente = ("def f(op, alm):\n"
                  "    q = (SesionConteo.query\n"
                  "         .filter(*ConteoService.filtros_pool_sin_dueno(op, alm))\n"
                  "         .order_by(*orden_de_reparto()))\n"
                  "    return q.limit(3).all()\n")
        assert _cadenas_del_pool_sin_orden(fuente) == []

    def test_el_detector_de_orden_propio_marca_y_no_marca(self):
        assert _orden_propio("p = case({'A': 1}, value=SesionConteo.clasificacion_abc)") == [1]
        assert _orden_propio("p = db.case((SesionConteo.tipo == 'MANUAL', 0), else_=1)") == [1]
        assert _orden_propio("p = case((TareaPicking.prioridad == 1, 0), else_=1)") == []
