"""
Estadísticas del conteo cíclico — `app/services/metricas/conteo.py`,
`GET /api/conteo/estadisticas` y el resumen del CSV de `/api/conteo/exportar`.

## Qué protege

1. **La unidad es la cadena** (CC1 + CC2 + CC3), no la fila. Una fila hija
   contada como conteo infla justo el error: solo existe cuando hubo diferencia.
2. **Un veredicto, una función** (`veredicto_cadena`). El CSV contaba «acierto»
   por el estado de la fila raíz; si el CSV y el reporte decidieran cada uno,
   dirían números distintos del mismo conteo. Trinquete por AST: nadie más
   compara contra MATCH en el reporte ni en el CSV.
3. **El día de una cadena es el día en que se confirmó el conteo que resolvió**,
   en Bogotá — no la hora del POST del DLQ, no la fecha UTC.
4. **El costo viaja con la foto** (m030): ausente es `None`, nunca 0, y la raíz
   lo hereda del conteo que resolvió.
5. **Cero Siesa** al calcular.

## El mundo dorado

Nueve cadenas sobre un hueco, construidas con los SERVICIOS reales
(`crear_conteo_manual`, `obtener_tarea_operario`, `registrar_conteo`,
`confirmar_ajuste`, el job real del DLQ, las rutas de omitir y cancelar) y una
Siesa falsa a nivel de la respuesta cruda de InvFecha. Cubren los seis
veredictos con ajuste y sin él, un bloqueo, una omisión, una cancelación, una
pendiente y un recuento. Cada número del reporte se afirma.
"""
import ast
import pathlib
from datetime import date, datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token

from tests.test_conteo_teorico_pos import SKU, siesa, tienda  # noqa: F401 (fixtures)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
METRICAS = RAIZ / 'app' / 'services' / 'metricas' / 'conteo.py'
RUTAS = RAIZ / 'app' / 'routes' / 'conteo.py'


_SIN_DECIR = object()


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _m():
    from app.services.metricas import conteo
    return conteo


def _poner(siesa, existencia, pos=0, salida_sin_conf=None, costo=_SIN_DECIR):
    """`costo` sin decir → el de la Siesa de mentira; `None` → la fila no lo trae."""
    if costo is _SIN_DECIR:
        siesa.poner(existencia=existencia, pos=pos, salida_sin_conf=salida_sin_conf)
    else:
        siesa.poner(existencia=existencia, pos=pos, salida_sin_conf=salida_sin_conf,
                    costo=costo)


def _nuevo_cc1(tienda, sku=SKU):
    from app.models.conteo import SesionConteo
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, sku)
    return SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id


def _hueco(db, tienda, n):
    """Un producto clase C con su propio hueco en la tienda. Cada cadena del
    mundo dorado va en un hueco distinto: desde b69bc78 una cadena viva o un
    ajuste en vuelo bloquea abrir otra sobre el mismo hueco, y una
    observación más reciente deja vieja a la anterior. Apiladas en un solo
    hueco, el mundo mediría esas reglas y no el reporte."""
    from app.models.inventario import UbicacionProducto
    from app.models.producto import Producto
    from app.models.producto_clasificacion_abc import ProductoClasificacionABC
    from app.models.ubicacion import Ubicacion
    sku = f'EST{n}'
    prod = Producto(codigo=sku, nombre=f'Item {n}', codigo_siesa=sku,
                    unidad_negocio_id='001', activo=True)
    db.session.add(prod)
    db.session.flush()
    ub = Ubicacion(codigo=f'EST-UB-{n}', almacen_id=tienda['almacen'].id, tipo_zona='GENERAL',
                   stock_minimo=0, stock_maximo=9999, secuencia_ruteo=1, activo=True)
    db.session.add(ub)
    db.session.flush()
    db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=prod.id,
                                     cantidad=10, reservado=0, bloqueado=0))
    db.session.add(ProductoClasificacionABC(producto_id=prod.id,
                                            almacen_id=tienda['almacen'].id,
                                            clasificacion='C'))
    db.session.commit()
    return sku


def _abrir_y_contar(sid, actor, fisico):
    _svc().obtener_tarea_operario(sid, actor.id)
    return _svc().registrar_conteo(sid, actor.id, fisico)


def _get(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


def _tok(app, usuario):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


# ─────────────────────────────────────────────────────────────────────────────
# El mundo dorado
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def dorado(app, db, client, siesa, tienda, monkeypatch):
    """Las nueve cadenas, una por hueco (clase C). Devuelve los ids de las raíces."""
    from app.models.siesa_job import SiesaJob
    from app.services.connekta_gateway import ConnektaGateway
    from app.services.siesa_job_service import _ejecutar_job

    monkeypatch.setattr(ConnektaGateway, 'enviar_ajuste_inventario',
                        lambda self, **kw: {'codigo': 0})
    a, b, sup = tienda['a'], tienda['b'], tienda['supervisor']
    ids = {}
    huecos = iter(_hueco(db, tienda, n) for n in range(1, 10))

    # 1 · OK_CC1, con un recuento: se vende 1 por caja mientras CC1 cuenta.
    _poner(siesa, 10)
    ids['ok_cc1'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    _svc().obtener_tarea_operario(cc1, a.id)
    _poner(siesa, 10, pos=1)
    assert _svc().registrar_conteo(cc1, a.id, 9)['resultado'] == 'RECONTAR'
    assert _svc().registrar_conteo(cc1, a.id, 9)['resultado'] == 'MATCH'

    # 2 · OK_CC2
    _poner(siesa, 10)
    ids['ok_cc2'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(cc1, a, 9)
    assert _abrir_y_contar(r['segundo_conteo_id'], b, 10)['resultado'] == 'MATCH'

    # 3 · OK_CC3 — CC1 −1, CC2 −2, el definitivo del supervisor cuadra.
    _poner(siesa, 10)
    ids['ok_cc3'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(cc1, a, 9)
    r = _abrir_y_contar(r['segundo_conteo_id'], b, 8)
    assert r['resultado'] == 'TERCER_CONTEO'
    assert _abrir_y_contar(r['tercer_conteo_id'], sup, 10)['resultado'] == 'MATCH'

    # 4 · ERROR_CONFIRMADO, auto-ajustado y llevado a AJUSTADO por el job real.
    #     El costo cambia entre CC1 y CC2: la raíz se queda con el de CC2.
    _poner(siesa, 10, costo=1400)
    ids['error_auto'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(cc1, a, 9)
    _poner(siesa, 10, costo=1500)
    r2 = _abrir_y_contar(r['segundo_conteo_id'], b, 9)
    assert r2['auto_encolado'] is True, r2
    job = SiesaJob.query.filter_by(tipo='AJUSTE_CONTEO', referencia_id=cc1).one()
    _ejecutar_job(job)
    assert _get(db, cc1).estado == 'AJUSTADO'

    # 5 · ERROR_CC3 aprobado por el supervisor (queda AJUSTANDO, en vuelo).
    _poner(siesa, 10, costo=1000)
    ids['error_cc3'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(cc1, a, 9)
    r = _abrir_y_contar(r['segundo_conteo_id'], b, 8)
    _poner(siesa, 10, costo=2000)
    r3 = _abrir_y_contar(r['tercer_conteo_id'], sup, 12)
    assert r3['resultado'] == 'DESCUADRE' and not r3['ajuste_bloqueado'], r3
    _svc().confirmar_ajuste(cc1, sup.id)
    assert _get(db, cc1).estado == 'AJUSTANDO'

    # 6 · ERROR_CONFIRMADO bloqueado: salidas sin confirmar que no son POS.
    _poner(siesa, 10, pos=2, salida_sin_conf=5)
    ids['error_bloqueado'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    r = _abrir_y_contar(cc1, a, 9)
    r2 = _abrir_y_contar(r['segundo_conteo_id'], b, 9)
    assert r2['ajuste_bloqueado'], r2
    assert _get(db, cc1).estado == 'DESCUADRE'

    # 7 · Omitida: el supervisor salta el CC2.
    _poner(siesa, 10)
    ids['omitida'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    _abrir_y_contar(cc1, a, 9)
    resp = client.post(f'/api/conteo/{cc1}/omitir-segundo', headers=_tok(app, sup))
    assert resp.status_code == 200, resp.get_json()

    # 8 · Cancelada antes de contarse.
    ids['cancelada'] = cc1 = _nuevo_cc1(tienda, next(huecos))
    resp = client.put(f'/api/conteo/{cc1}/cancelar', json={'motivo': 'prueba'},
                      headers=_tok(app, sup))
    assert resp.status_code == 200, resp.get_json()

    # 9 · Pendiente: creada, nadie la abrió.
    ids['pendiente'] = _nuevo_cc1(tienda, next(huecos))
    db.session.expire_all()
    return ids


def _hoy():
    from app.utils.fecha import dia_operativo_de
    return dia_operativo_de(datetime.utcnow())


def _reporte(**kw):
    hoy = _hoy()
    return _m().calcular_estadisticas_conteo(hoy - timedelta(days=6), hoy, **kw)


class TestMundoDorado:

    def test_veredictos(self, db, dorado):
        m = _m()
        esperado = {
            'ok_cc1': m.OK_CC1, 'ok_cc2': m.OK_CC2, 'ok_cc3': m.OK_CC3,
            'error_auto': m.ERROR_CONFIRMADO, 'error_cc3': m.ERROR_CC3,
            'error_bloqueado': m.ERROR_CONFIRMADO, 'omitida': m.SIN_VEREDICTO,
            'cancelada': m.SIN_VEREDICTO, 'pendiente': m.SIN_VEREDICTO,
        }
        for nombre, v in esperado.items():
            assert m.veredicto_cadena(_get(db, dorado[nombre])) == v, nombre
        assert m.motivo_sin_veredicto(_get(db, dorado['omitida'])) == 'omitida'
        assert m.motivo_sin_veredicto(_get(db, dorado['cancelada'])) == 'cancelada'
        assert m.motivo_sin_veredicto(_get(db, dorado['pendiente'])) == 'pendiente'

    def test_volumen_y_flujo(self, db, dorado):
        v = _reporte()['volumen']
        assert v['iniciadas'] == 9
        assert v['cerradas'] == 6
        assert v['skus_cerrados'] == 6
        assert v['por_veredicto'] == {'OK_CC1': 1, 'OK_CC2': 1, 'OK_CC3': 1,
                                      'ERROR_AUDITORIA': 0, 'ERROR_CC3': 1,
                                      'ERROR_CONFIRMADO': 2}
        assert (v['a_cc2']['numerador'], v['a_cc2']['denominador']) == (5, 6)
        assert (v['a_cc3']['numerador'], v['a_cc3']['denominador']) == (2, 6)
        assert v['omitidas'] == 1
        assert v['sin_veredicto'] == {'cancelada': 1, 'omitida': 1, 'pendiente': 1}
        assert v['por_tipo'] == {'MANUAL': {'iniciadas': 9, 'cerradas': 6}}
        # Recuentos: 1 descartado; intentos medibles = 14 conteos confirmados
        # con foto de inicio (raíces incluidas: la raíz ES el CC1) + el descarte.
        assert (v['recuentos']['numerador'], v['recuentos']['denominador']) == (1, 15)
        (semana,) = v['por_semana']
        assert (semana['cerradas'], semana['ok'], semana['error'],
                semana['ajustes'], semana['unidades_ajustadas']) == (6, 3, 3, 2, 3)

    def test_ajustes_y_valor(self, db, dorado):
        a = _reporte()['ajustes']
        assert a['cantidad'] == 2
        assert a['en_vuelo'] == 1
        assert (a['automaticos'], a['aprobados_por_supervisor']) == (1, 1)
        assert (a['unidades_ent'], a['unidades_sal'], a['unidades_neto']) == (2, 1, 1)
        # +2 × 2000 (costo de la foto del CC3) y −1 × 1500 (del CC2), no los de CC1.
        assert a['valor'] == {'etiqueta': _m().ETIQUETA_VALOR, 'ent': 4000.0,
                              'sal': 1500.0, 'neto': 2500.0,
                              'ajustes_valorizados': 2, 'ajustes_sin_costo': 0}
        assert a['bloqueados_hoy'] == {'total': 1, 'por_motivo': {'SALIDAS_NO_POS': 1},
                                       'descuadres_aprobables': 1}
        assert a['jobs_fallidos_hoy'] == 0
        assert a['excluidos'] == {}

    def test_exactitud_no_publica_porcentaje_con_n_chico(self, db, dorado):
        e = _reporte()['exactitud']
        g = e['por_clase']['C']['OTROS']
        assert (g['numerador'], g['denominador'], g['porcentaje']) == (3, 6, None)
        assert '< 30' in g['sin_porcentaje_por']
        assert e['excluidos'] == {}

    def test_productos_problema(self, db, dorado):
        p = _reporte()['productos_problema']
        difs = [(f['codigo'].split('-')[0], f['diferencia'], f['veredicto'])
                for f in p['por_diferencia']]
        assert [d[1] for d in difs] == [2, -1, 1]
        assert [d[2] for d in difs] == ['ERROR_CC3', 'ERROR_CONFIRMADO', 'ERROR_CONFIRMADO']
        assert sorted(f['producto_codigo'] for f in p['por_ajustes']) == ['EST4', 'EST5']
        assert all(f['n'] == 1 for f in p['por_ajustes'])
        assert [(f['producto_codigo'], f['n']) for f in p['por_recuentos']] == [('EST1', 1)]

    def test_por_operario_solo_volumen_y_sin_ranking(self, db, dorado, tienda):
        f = {x['nombre']: x for x in _reporte()['por_operario']['filas']}
        assert (f['pos-a@test.com']['cadenas'], f['pos-a@test.com']['conteos']) == (6, 6)
        assert (f['pos-b@test.com']['cadenas'], f['pos-b@test.com']['conteos']) == (5, 5)
        assert (f['pos-sup@test.com']['cadenas'], f['pos-sup@test.com']['conteos']) == (2, 2)
        for fila in f.values():
            assert set(fila) == {'operario_id', 'nombre', 'cadenas', 'conteos'}, (
                'por operario va SOLO volumen: ni exactitud ni tasa de acierto')
        nombres = [x['nombre'] for x in _reporte()['por_operario']['filas']]
        assert nombres == sorted(nombres), 'ordenado por nombre, no por volumen (sin ranking)'

    def test_carga_y_cobertura(self, db, dorado, tienda):
        filas = {f['clase']: f for f in _reporte()['carga_cobertura']['filas']}
        assert set(filas) == {'A', 'B', 'C'}
        assert filas['A']['universo_huecos'] == 0 and filas['A']['exigencia_diaria'] == 0
        fila = filas['C']
        assert fila['frecuencia_dias'] == 600   # intervalo C vigente (conteo_politica)
        # Nueve huecos clase C (el POSITEM del fixture `tienda` no tiene ABC).
        assert (fila['universo_productos'], fila['universo_huecos']) == (9, 9)
        # Al día = MATCH o AJUSTADO (la regla del generador): OK_CC1, OK_CC2,
        # OK_CC3 y el ajuste auto que el DLQ llevó a AJUSTADO. El AJUSTANDO y
        # los DESCUADRE todavía no cuentan como contados para el plan.
        assert (fila['contados_en_frecuencia']['numerador'],
                fila['contados_en_frecuencia']['denominador']) == (4, 9)
        assert (fila['sin_contar_en_ventana'], fila['nunca_contados']) == (5, 5)
        assert fila['exigencia_diaria'] == 1          # ceil(9 / 600)
        assert fila['ritmo']['cadenas_cerradas'] == 6
        assert fila['dias_para_cerrar_ciclo'] == round(5 / (6 / 28), 1)

    def test_rezago(self, db, dorado):
        r = _reporte()['rezago']
        assert r['pendiente'] == {'0-2': 1, '3-7': 0, '8-30': 0, '>30': 0}
        assert r['total_en_curso'] == 0

    def test_filtros(self, db, dorado):
        assert _reporte(tipo='DIARIO_ABC')['volumen']['iniciadas'] == 0
        assert _reporte(clase='C')['volumen']['iniciadas'] == 9
        assert _reporte(clase='A')['volumen']['iniciadas'] == 0

    def test_el_reporte_no_habla_con_siesa(self, db, dorado, monkeypatch):
        """Cero HTTP: se rompe TODA salida de red antes de calcular."""
        import requests
        from app.services.connekta_gateway import ConnektaGateway

        def _prohibido(*a, **k):
            raise AssertionError('el reporte de conteo llamó a la red')
        for nombre in ('get', 'post', 'request'):
            monkeypatch.setattr(requests, nombre, _prohibido)
        monkeypatch.setattr(requests.Session, 'request', _prohibido)
        for metodo in ('_get', '_post', 'get_inventario_fecha'):
            monkeypatch.setattr(ConnektaGateway, metodo, _prohibido)
        assert _reporte()['volumen']['cerradas'] == 6


# ─────────────────────────────────────────────────────────────────────────────
# Unidad = cadena; fecha de atribución
# ─────────────────────────────────────────────────────────────────────────────

def _fila(db, tienda, **kw):
    from app.models.conteo import SesionConteo
    import uuid
    base = dict(codigo=f'T-{uuid.uuid4().hex[:8]}', tipo='MANUAL', clasificacion_abc='C',
                ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
                producto_id=tienda['producto'].id, producto_codigo_siesa=SKU,
                es_segundo_conteo=False)
    base.update(kw)
    s = SesionConteo(**base)
    db.session.add(s)
    db.session.flush()
    return s


class TestUnidadEsLaCadena:

    def test_una_fila_hija_nunca_cuenta_como_cadena(self, db, tienda):
        """Una raíz con CC2 y CC3 es UNA cadena, no tres."""
        t = datetime(2026, 9, 10, 15, 0)
        raiz = _fila(db, tienda, estado='MATCH', fecha_creacion=t, foto_siesa_at=t,
                     teorico_siesa=10, fuente_existencia='SIESA')
        hijo = _fila(db, tienda, estado='DESCUADRE', es_segundo_conteo=True,
                     sesion_origen_id=raiz.id, fecha_creacion=t, foto_siesa_at=t)
        _fila(db, tienda, estado='MATCH', es_segundo_conteo=True, sesion_origen_id=hijo.id,
              fecha_creacion=t, foto_siesa_at=t, teorico_siesa=10, fuente_existencia='SIESA')
        db.session.commit()
        r = _m().calcular_estadisticas_conteo(date(2026, 9, 10), date(2026, 9, 10))
        assert r['volumen']['iniciadas'] == 1
        assert r['volumen']['cerradas'] == 1
        assert r['volumen']['por_veredicto']['OK_CC3'] == 1


class TestDiaDeAtribucion:

    def test_confirmado_a_las_8pm_de_bogota_es_de_ese_dia(self, db, tienda):
        """01:00 UTC del 10 = 20:00 del 9 en Bogotá. Es del 9."""
        momento = datetime(2026, 9, 10, 1, 0)
        raiz = _fila(db, tienda, estado='MATCH', fecha_creacion=momento,
                     foto_siesa_at=momento, fecha_cierre=momento)
        db.session.commit()
        assert _m().dia_de_atribucion(raiz) == date(2026, 9, 9)
        el_9 = _m().calcular_estadisticas_conteo(date(2026, 9, 9), date(2026, 9, 9))
        el_10 = _m().calcular_estadisticas_conteo(date(2026, 9, 10), date(2026, 9, 10))
        assert el_9['volumen']['cerradas'] == 1
        assert el_10['volumen']['cerradas'] == 0

    def test_nunca_la_fecha_de_cierre_de_una_raiz_ajustada(self, db, tienda):
        """La fecha del POST del DLQ no es la del conteo: sin foto, se declara."""
        raiz = _fila(db, tienda, estado='AJUSTADO', motivo_codigo='AJ-SAL', diferencia=-1,
                     tarea_picking_id=None, fecha_creacion=datetime(2026, 9, 1, 15),
                     fecha_cierre=datetime(2026, 9, 5, 15), siesa_triggered=True)
        hijo = _fila(db, tienda, estado='DESCUADRE', es_segundo_conteo=True,
                     sesion_origen_id=raiz.id, fecha_creacion=datetime(2026, 9, 1, 15))
        db.session.commit()
        assert _m().dia_de_atribucion(raiz) is None
        r = _m().calcular_estadisticas_conteo(date(2026, 9, 1), date(2026, 9, 30))
        assert r['volumen']['cerradas'] == 0
        assert r['volumen']['excluidos'] == {'cerradas_sin_fecha_de_confirmacion': 1}
        assert r['ajustes']['excluidos'] == {'sin_fecha_de_confirmacion': 1}
        assert hijo.id  # la fila hija existe y no se contó aparte

    def test_ni_cuando_la_raiz_misma_es_la_que_cuenta(self, db, tienda):
        """Omitida y después ajustada: sin veredicto, el día sale de la raíz. Sin
        foto, su `fecha_cierre` (la del DLQ) tampoco vale — solo en MATCH."""
        raiz = _fila(db, tienda, estado='AJUSTADO', motivo_codigo='AJ-SAL', diferencia=-1,
                     fecha_creacion=datetime(2026, 9, 1, 15),
                     fecha_cierre=datetime(2026, 9, 5, 15), siesa_triggered=True)
        _fila(db, tienda, estado='CANCELADO', es_segundo_conteo=True,
              sesion_origen_id=raiz.id, fecha_creacion=datetime(2026, 9, 1, 15),
              fecha_cierre=datetime(2026, 9, 2, 15))
        db.session.commit()
        assert _m().veredicto_cadena(raiz) == _m().SIN_VEREDICTO
        assert _m().dia_de_atribucion(raiz) is None
        a = _m().calcular_estadisticas_conteo(date(2026, 9, 1), date(2026, 9, 30))['ajustes']
        assert a['cantidad'] == 0
        assert a['excluidos'] == {'sin_fecha_de_confirmacion': 1}

    def test_ensayo_no_cuenta_como_ajuste(self, db, tienda):
        t = datetime(2026, 9, 10, 15)
        raiz = _fila(db, tienda, estado='AJUSTADO', motivo_codigo='AJ-ENT', diferencia=3,
                     fecha_creacion=t, siesa_triggered=False)
        _fila(db, tienda, estado='DESCUADRE', es_segundo_conteo=True,
              sesion_origen_id=raiz.id, fecha_creacion=t, foto_siesa_at=t)
        db.session.commit()
        a = _m().calcular_estadisticas_conteo(date(2026, 9, 10), date(2026, 9, 10))['ajustes']
        assert a['cantidad'] == 0
        assert a['excluidos'] == {'modo_ensayo': 1}


class TestVeredictoAuditoria:

    def test_auditoria_de_picking_con_diferencia_es_error(self, db, tienda):
        """Sin este veredicto la auditoría que cuadra contaba como OK y la que no
        desaparecía del denominador."""
        from app.models.picking import TareaPicking
        tp = TareaPicking(codigo='PK-EST-1', producto_id=tienda['producto'].id,
                          cantidad_solicitada=1, ubicacion_id=tienda['ubicacion'].id,
                          almacen_id=tienda['almacen'].id, estado='BLOQUEADO')
        db.session.add(tp)
        db.session.flush()
        t = datetime(2026, 9, 10, 15)
        raiz = _fila(db, tienda, tipo='EXCEPCION_PICKING', estado='DESCUADRE',
                     tarea_picking_id=tp.id, diferencia=-2, fecha_creacion=t, foto_siesa_at=t)
        db.session.commit()
        assert _m().veredicto_cadena(raiz) == _m().ERROR_AUDITORIA
        raiz.diferencia = 0
        raiz.estado = 'MATCH'
        assert _m().veredicto_cadena(raiz) == _m().OK_CC1

    def test_bloqueado_sin_motivo_registrado_no_rompe(self, db, tienda):
        """Un BLOQUEADO anterior a m031hud: sin `motivo_bloqueo`."""
        raiz = _fila(db, tienda, estado='BLOQUEADO', fecha_creacion=datetime(2026, 4, 1))
        db.session.commit()
        assert _m().veredicto_cadena(raiz) == _m().SIN_VEREDICTO
        assert _m().motivo_sin_veredicto(raiz) == 'bloqueado'

    def test_no_lo_encontre_se_distingue_y_no_es_un_veredicto(self, db, tienda):
        """«No lo encontré» no es un cero ni un error: sin veredicto, con su
        propio motivo — sea la raíz o el CC2 el bloqueado."""
        raiz = _fila(db, tienda, estado='BLOQUEADO', fecha_creacion=datetime(2026, 4, 1))
        raiz.motivo_bloqueo = 'NO_ENCONTRADO'
        db.session.commit()
        assert _m().veredicto_cadena(raiz) == _m().SIN_VEREDICTO
        assert _m().motivo_sin_veredicto(raiz) == 'no_encontrado'


class TestCoberturaSinRitmo:

    def test_sin_cadenas_cerradas_no_hay_estimacion(self, db, tienda):
        """Ritmo cero: ni infinito ni cero días — `None` y el porqué."""
        from app.models.producto_clasificacion_abc import ProductoClasificacionABC
        db.session.add(ProductoClasificacionABC(producto_id=tienda['producto'].id,
                                                almacen_id=tienda['almacen'].id,
                                                clasificacion='A'))
        db.session.commit()
        (fila,) = _m().calcular_estadisticas_conteo()['carga_cobertura']['filas'][:1]
        assert fila['clase'] == 'A'
        assert (fila['universo_huecos'], fila['nunca_contados'], fila['sin_contar_en_ventana']) == (1, 1, 1)
        assert fila['exigencia_diaria'] == 1
        assert fila['dias_para_cerrar_ciclo'] is None
        assert 'sin ritmo medible' in fila['sin_estimacion_por']

    def test_la_cobertura_usa_las_funciones_del_generador(self):
        """Una política, una función: la cobertura no reimplementa el universo."""
        arbol = ast.parse(METRICAS.read_text(encoding='utf-8'))
        llamadas = {n.func.id for n in ast.walk(arbol)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for f in ('universo_conteo_ciclico', 'huecos_con_stock',
                  'ultimo_conteo_por_hueco', 'umbral_al_dia'):
            assert f in llamadas, f
        gen = ast.parse((RAIZ / 'app' / 'services' / 'abc_service.py').read_text(encoding='utf-8'))
        fn = next(n for n in ast.walk(gen) if isinstance(n, ast.FunctionDef)
                  and n.name == 'generar_tareas_conteo_diario')
        del_gen = {n.func.id for n in ast.walk(fn)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for f in ('universo_conteo_ciclico', 'huecos_con_stock',
                  'ultimo_conteo_por_hueco', 'umbral_al_dia'):
            assert f in del_gen, f'el generador dejó de usar {f}: dos definiciones'


# ─────────────────────────────────────────────────────────────────────────────
# Costo de la foto (m030)
# ─────────────────────────────────────────────────────────────────────────────

class TestCostoDeLaFoto:

    def test_ausente_es_none_no_cero(self, db, siesa, tienda):
        _poner(siesa, 10, costo=None)  # la fila no trae f400_costo_prom_uni
        foto = _svc().consultar_foto_siesa(SKU, 'NS1')
        assert foto is not None and foto['costo_prom_uni'] is None
        cc1 = _nuevo_cc1(tienda)
        _abrir_y_contar(cc1, tienda['a'], 10)
        assert _get(db, cc1).costo_prom_uni_siesa is None

    @pytest.mark.parametrize('crudo', ['abc', float('nan'), {}])
    def test_ilegible_es_none_y_no_rompe_la_foto(self, db, siesa, tienda, crudo):
        _poner(siesa, 10, costo=None)
        siesa.fila['f400_costo_prom_uni'] = crudo
        foto = _svc().consultar_foto_siesa(SKU, 'NS1')
        assert foto is not None, 'la falta de costo nunca bloquea un conteo'
        assert foto['costo_prom_uni'] is None

    def test_cero_o_negativo_se_guarda_y_no_se_valoriza(self, db, siesa, tienda):
        _poner(siesa, 10, costo=0)
        assert _svc().consultar_foto_siesa(SKU, 'NS1')['costo_prom_uni'] == 0.0

    def test_la_raiz_hereda_el_costo_del_cc2(self, db, siesa, tienda):
        _poner(siesa, 10, costo=100)
        cc1 = _nuevo_cc1(tienda)
        r = _abrir_y_contar(cc1, tienda['a'], 9)
        assert float(_get(db, cc1).costo_prom_uni_siesa) == 100
        _poner(siesa, 10, costo=250)
        _abrir_y_contar(r['segundo_conteo_id'], tienda['b'], 9)
        assert float(_get(db, cc1).costo_prom_uni_siesa) == 250

    def test_la_raiz_hereda_el_costo_del_cc3(self, db, siesa, tienda):
        _poner(siesa, 10, costo=100)
        cc1 = _nuevo_cc1(tienda)
        r = _abrir_y_contar(cc1, tienda['a'], 9)
        r = _abrir_y_contar(r['segundo_conteo_id'], tienda['b'], 8)
        _poner(siesa, 10, costo=777)
        _abrir_y_contar(r['tercer_conteo_id'], tienda['supervisor'], 12)
        assert float(_get(db, cc1).costo_prom_uni_siesa) == 777

    def test_to_dict_lo_expone(self, db, siesa, tienda):
        _poner(siesa, 10, costo=12.5)
        cc1 = _nuevo_cc1(tienda)
        _abrir_y_contar(cc1, tienda['a'], 10)
        assert _get(db, cc1).to_dict()['costo_prom_uni_siesa'] == 12.5


# ─────────────────────────────────────────────────────────────────────────────
# Motivos de bloqueo agrupados
# ─────────────────────────────────────────────────────────────────────────────

class TestBloqueadosSeAgrupan:
    """El resumen lee la prosa de `motivo_bloqueo_ajuste`. Si la prosa cambia,
    el motivo cae en OTRO: este test lo pone rojo antes de que el tablero
    empiece a decir «OTRO» sin explicación."""

    def test_cada_motivo_real_tiene_su_clave(self, db, tienda):
        from app.services.conteo_service import ConteoService
        t = datetime(2026, 9, 10, 15)
        casos = {
            'SIN_FOTO_CIERRE': dict(fuente_existencia='WMS'),
            'SALIDAS_NO_POS': dict(fuente_existencia='SIESA', teorico_siesa=8,
                                   cant_pos_siesa=2, salida_sin_conf_siesa=5, foto_siesa_at=t),
            'SIN_FOTO_APERTURA': dict(fuente_existencia='SIESA', teorico_siesa=8,
                                      cant_pos_siesa=2, salida_sin_conf_siesa=2, foto_siesa_at=t),
            'MOVIMIENTO_DURANTE_CONTEO': dict(
                fuente_existencia='SIESA', teorico_siesa=8, existencia_siesa=10,
                cant_pos_siesa=2, salida_sin_conf_siesa=2, foto_siesa_at=t,
                existencia_inicio_siesa=10, cant_pos_inicio_siesa=1,
                salida_sin_conf_inicio_siesa=1, foto_inicio_at=t),
        }
        vistos = set()
        for clave, campos in casos.items():
            s = _fila(db, tienda, estado='DESCUADRE', fecha_creacion=t, **campos)
            motivo = ConteoService.motivo_bloqueo_ajuste(s)
            assert motivo, clave
            assert _m().resumir_motivo_bloqueo(motivo) == clave, motivo
            vistos.add(clave)
            s.estado = 'CANCELADO'   # que no deje vieja a la siguiente
            db.session.flush()

        # CONTEO_VIEJO: un conteo limpio del hueco, y otro posterior con resultado.
        limpia = dict(fuente_existencia='SIESA', teorico_siesa=8, existencia_siesa=10,
                      cant_pos_siesa=2, salida_sin_conf_siesa=2,
                      existencia_inicio_siesa=10, cant_pos_inicio_siesa=2,
                      salida_sin_conf_inicio_siesa=2)
        vieja = _fila(db, tienda, estado='DESCUADRE', fecha_creacion=t, foto_siesa_at=t,
                      foto_inicio_at=t, cantidad_fisica=7, diferencia=-1, **limpia)
        assert ConteoService.motivo_bloqueo_ajuste(vieja) is None, 'debe llegar limpia'
        _fila(db, tienda, estado='MATCH', fecha_creacion=t, cantidad_fisica=8,
              foto_siesa_at=t + timedelta(hours=1), foto_inicio_at=t + timedelta(hours=1),
              **limpia)
        motivo = ConteoService.motivo_bloqueo_ajuste(vieja)
        assert _m().resumir_motivo_bloqueo(motivo) == 'CONTEO_VIEJO', motivo
        vistos.add('CONTEO_VIEJO')
        assert _m().resumir_motivo_bloqueo(
            'Había un traslado entrando a esta bodega …') == 'TRASLADO_ENTRANTE'
        assert _m().resumir_motivo_bloqueo('algo que nadie escribió') == 'OTRO'
        assert len(vistos) >= 5


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestEndpoint:

    def test_supervisor_lo_lee(self, app, db, client, tienda):
        r = client.get('/api/conteo/estadisticas?desde=2026-09-01&hasta=2026-09-07',
                       headers=_tok(app, tienda['supervisor']))
        assert r.status_code == 200, r.get_json()
        body = r.get_json()
        assert body['parametros']['desde'] == '2026-09-01'
        assert body['fuente'].startswith('solo base del WMS')

    def test_operario_no(self, app, db, client, tienda):
        r = client.get('/api/conteo/estadisticas', headers=_tok(app, tienda['a']))
        assert r.status_code == 403

    @pytest.mark.parametrize('qs', ['desde=2026-13-01', 'hasta=ayer', 'almacen_id=x',
                                    'clase=Z', 'desde=2026-09-10&hasta=2026-09-01'])
    def test_parametro_invalido_es_400_no_se_ignora(self, app, db, client, tienda, qs):
        r = client.get(f'/api/conteo/estadisticas?{qs}', headers=_tok(app, tienda['supervisor']))
        assert r.status_code == 400, (qs, r.get_json())

    def test_sin_fechas_son_las_ultimas_cuatro_semanas(self, app, db, client, tienda):
        body = client.get('/api/conteo/estadisticas',
                          headers=_tok(app, tienda['supervisor'])).get_json()
        desde = date.fromisoformat(body['parametros']['desde'])
        hasta = date.fromisoformat(body['parametros']['hasta'])
        assert (hasta - desde).days == 27


# ─────────────────────────────────────────────────────────────────────────────
# CSV
# ─────────────────────────────────────────────────────────────────────────────

class TestCSV:

    def test_columnas_nuevas_y_sin_exactitud_por_operario(self, app, db, client, dorado, tienda):
        r = client.get('/api/conteo/exportar', headers=_tok(app, tienda['supervisor']))
        assert r.status_code == 200
        texto = r.get_data(as_text=True)
        cabecera = texto.splitlines()[0].split(',')
        for col in ('Existencia Siesa', 'Teorico Siesa', 'POS', 'Fuente', 'Costo unit.'):
            assert col in cabecera, col
        assert 'Stock WMS' not in cabecera
        assert 'Accuracy %' not in texto and 'Operario,Total Conteos' not in texto
        assert 'TRUNCADO' not in texto
        # Resumen por clase, por cadena: 8 raíces exportables (la pendiente no
        # entra por estado); 3 OK, 3 ERROR y 2 sin veredicto (omitida, cancelada).
        fila_c = next(l for l in texto.splitlines() if l.startswith('C,'))
        assert fila_c.split(',')[:5] == ['C', '8', '3', '3', '2']

    def test_declara_el_truncado(self, app, db, client, tienda, monkeypatch):
        """No se fabrican 5.001 filas: se baja el límite del módulo real."""
        import app.routes.conteo as rutas
        t = datetime(2026, 9, 10, 15)
        for _ in range(3):
            _fila(db, tienda, estado='CANCELADO', fecha_creacion=t)
        db.session.commit()
        monkeypatch.setattr(rutas, 'LIMITE_EXPORTAR', 2)
        texto = client.get('/api/conteo/exportar',
                           headers=_tok(app, tienda['supervisor'])).get_data(as_text=True)
        assert 'TRUNCADO' in texto
        assert len([l for l in texto.splitlines() if l.startswith('T-')]) == 2
        monkeypatch.setattr(rutas, 'LIMITE_EXPORTAR', 3)
        texto = client.get('/api/conteo/exportar',
                           headers=_tok(app, tienda['supervisor'])).get_data(as_text=True)
        assert 'TRUNCADO' not in texto, 'exactamente en el límite no hubo corte'


# ─────────────────────────────────────────────────────────────────────────────
# Una política, una función — por AST
# ─────────────────────────────────────────────────────────────────────────────

def _funcion(ruta, nombre):
    arbol = ast.parse(ruta.read_text(encoding='utf-8'))
    return next(n for n in ast.walk(arbol)
                if isinstance(n, ast.FunctionDef) and n.name == nombre)


def _llama(fn, nombre):
    return any(isinstance(n, ast.Call) and (
        (isinstance(n.func, ast.Name) and n.func.id == nombre)
        or (isinstance(n.func, ast.Attribute) and n.func.attr == nombre))
        for n in ast.walk(fn))


def _compara_con_match(fn):
    """Sitios que deciden «acertó» por su cuenta: una comparación contra MATCH
    (literal o `EstadoConteo.MATCH`). El filtro de consulta `.in_([...])` es una
    llamada, no una comparación, y no cuenta."""
    sitios = []
    for n in ast.walk(fn):
        if not isinstance(n, ast.Compare):
            continue
        for lado in [n.left, *n.comparators]:
            elementos = lado.elts if isinstance(lado, (ast.Tuple, ast.List, ast.Set)) else [lado]
            for e in elementos:
                if (isinstance(e, ast.Constant) and e.value == 'MATCH') or (
                        isinstance(e, ast.Attribute) and e.attr == 'MATCH'):
                    sitios.append(n.lineno)
    return sitios


#: Las funciones del módulo de métricas que PUEDEN mirar MATCH, con su porqué.
#: Solo encoge.
MIRAN_MATCH = {
    'veredicto_cadena': 'es LA definición del veredicto',
    'dia_de_atribucion': 'fecha_cierre solo vale en MATCH (la escribe reconciliar_cantidad al confirmar)',
}


class TestUnVeredictoUnaFuncion:

    def test_el_csv_usa_veredicto_cadena(self):
        fn = _funcion(RUTAS, 'exportar_conteos')
        assert _llama(fn, 'veredicto_cadena')
        assert _compara_con_match(fn) == [], 'el CSV decide aciertos por su cuenta'

    def test_el_endpoint_delega_en_el_modulo(self):
        fn = _funcion(RUTAS, 'estadisticas_conteo')
        assert _llama(fn, 'calcular_estadisticas_conteo')
        assert _compara_con_match(fn) == []

    def test_el_modulo_decide_con_veredicto_cadena(self):
        assert _llama(_funcion(METRICAS, '_cadena'), 'veredicto_cadena')
        arbol = ast.parse(METRICAS.read_text(encoding='utf-8'))
        funciones = [n for n in arbol.body if isinstance(n, ast.FunctionDef)]
        assert len(funciones) >= 15, 'piso: el escáner dejó de ver las funciones del módulo'
        fuera = {f.name: _compara_con_match(f) for f in funciones
                 if f.name not in MIRAN_MATCH and _compara_con_match(f)}
        assert fuera == {}, f'reimplementan el veredicto: {fuera}'

    def test_la_lista_declara_su_porque(self):
        assert all(len(v) > 20 for v in MIRAN_MATCH.values())
        nombres = {n.name for n in ast.walk(ast.parse(METRICAS.read_text(encoding='utf-8')))
                   if isinstance(n, ast.FunctionDef)}
        assert set(MIRAN_MATCH) <= nombres, 'la lista nombra funciones que ya no existen'

    def test_el_detector_muerde(self):
        """Meta: el CSV viejo —`if s.estado == 'MATCH'`— se habría detectado;
        un `.in_(['MATCH'])` de consulta no."""
        viejo = ast.parse("def f(s):\n    if s.estado == 'MATCH':\n        pass\n").body[0]
        assert _compara_con_match(viejo) == [2]
        tupla = ast.parse("def f(s):\n    return s.estado in ('MATCH', 'X')\n").body[0]
        assert _compara_con_match(tupla) == [2]
        attr = ast.parse("def f(s):\n    return s.estado == EstadoConteo.MATCH\n").body[0]
        assert _compara_con_match(attr) == [2]
        consulta = ast.parse("def f(q):\n    return q.filter(E.estado.in_(['MATCH']))\n").body[0]
        assert _compara_con_match(consulta) == []
        sin_llamada = ast.parse("def f(s):\n    return s.estado\n").body[0]
        assert not _llama(sin_llamada, 'veredicto_cadena')


# ─────────────────────────────────────────────────────────────────────────────
# La pantalla escapa todo dato — ejecutando el render real, no contando esc()
# ─────────────────────────────────────────────────────────────────────────────

_ARNES_XSS = r"""
const fs = require('fs'); const vm = require('vm');
// `node -e` deja los argumentos desde argv[1]; el `--` se descarta.
const args = process.argv.slice(1).filter(a => a !== '--');
const base = args[0];
const ctx = { console, document: { getElementById: () => null }, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(base + '/util.js', 'utf8'), ctx);
vm.runInContext(fs.readFileSync(base + '/conteo.js', 'utf8'), ctx);
if (args[1] === 'esc-roto') vm.runInContext('esc = (x) => String(x);', ctx);
const X = '<img src=x onerror=alert(1)>';
const m = { numerador: 1, denominador: 2, porcentaje: null, sin_porcentaje_por: X, excluidos: { [X]: 3 } };
const d = {
  parametros: { desde: X, hasta: X }, fuente: X,
  carga_cobertura: { al_dia_operativo: X, nota: X, excluidos: { [X]: 1 },
    por_almacen: [{ almacen: X, bodega_siesa: X, cupo_diario: 60, ritmo_real_por_dia: 1, dias_ventana: 28,
      exigencia_diaria_plan: 20, pendientes_vivas: 5, dias_de_cupo_pendientes: 0.1, mensaje_generador: X }],
    filas: [{
    almacen: X, clase: X, frecuencia_dias: 15, universo_productos: 1, universo_huecos: 1,
    contados_en_frecuencia: m, nunca_contados: 1, sin_contar_en_ventana: 1, exigencia_diaria: 1,
    ritmo: { por_dia: 0, cadenas_cerradas: 0, dias_ventana: 28 }, dias_para_cerrar_ciclo: null, sin_estimacion_por: X }] },
  rezago: { pendiente: { [X]: 1 }, en_curso: {}, total_pendiente: 1, total_en_curso: 0, excluidos: {} },
  volumen: { iniciadas: 1, cerradas: 1, skus_cerrados: 1, a_cc2: m, a_cc3: m, sin_veredicto: { [X]: 1 },
    recuentos: m, excluidos: {}, unidad: X, por_semana: [{ semana: X, cerradas: 1, ok: 1, error: 0, ajustes: 0, unidades_ajustadas: 0 }] },
  ajustes: { cantidad: 1, automaticos: 1, aprobados_por_supervisor: 0, en_vuelo: 0, unidades_ent: 1, unidades_sal: 0, unidades_neto: 1,
    valor: { etiqueta: X, ent: 1, sal: 0, neto: 1, ajustes_valorizados: 1, ajustes_sin_costo: 0 },
    bloqueados_hoy: { total: 1, por_motivo: { [X]: 1 }, descuadres_aprobables: 0 }, jobs_fallidos_hoy: 0,
    motivos_auditoria_picking: { por_motivo: { [X]: 1 } }, excluidos: { [X]: 1 } },
  exactitud: { definicion: X, min_n: 30, por_clase: { [X]: { [X]: m } }, excluidos: {} },
  productos_problema: { por_diferencia: [{ producto_codigo: X, producto_nombre: X, diferencia: 1, dia: X }],
    por_ajustes: [{ producto_codigo: X, n: 1 }], por_recuentos: [] },
  por_operario: { nota: X, filas: [{ operario_id: 1, nombre: X, cadenas: 1, conteos: 1 }], excluidos: {} },
};
const html = vm.runInContext('_ceRender', ctx)(d);
const crudos = (html.match(/<img/g) || []).length;
const escapados = (html.match(/&lt;img/g) || []).length;
console.log(JSON.stringify({ crudos, escapados, largo: html.length }));
"""


def _render_malicioso(modo=''):
    import json
    import shutil
    import subprocess
    if not shutil.which('node'):
        pytest.skip('sin node')
    pwa = RAIZ / 'app' / 'static' / 'pwa'
    r = subprocess.run(['node', '-e', _ARNES_XSS, '--', str(pwa), modo],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


class TestLaPantallaEscapaTodoDato:
    """CLAUDE.md, «Todo dato que se pinta va con esc()»: el guard de texto mide
    interpolaciones; esto carga `util.js` y `conteo.js` DE VERDAD en Node,
    pinta el reporte con `<img onerror>` en cada texto y cuenta lo que queda."""

    def test_ningun_texto_llega_crudo(self):
        r = _render_malicioso()
        assert r['crudos'] == 0, r
        assert r['escapados'] >= 25, f'piso: el arnés dejó de pintar el reporte ({r})'

    def test_el_arnes_muerde_con_esc_roto(self):
        """Meta: con un esc que no escapa, el mismo render deja pasar el ataque."""
        assert _render_malicioso('esc-roto')['crudos'] >= 25
