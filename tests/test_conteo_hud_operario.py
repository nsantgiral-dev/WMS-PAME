"""
El HUD de conteo del operario: cantidad declarada, idempotente y con factor;
«no lo encontré» no es un cero; un bloqueado tiene salida (P0-HUD, 2026-09-23).

## Los defectos (verificados en origin/qa antes de tocar nada)

1. **«No escaneé nada» = 0.** `MobileService.confirmar_tarea` (rama CONTEO)
   hacía `cantidad_fisica if … is not None else 0`. Sin layout —todo NB1 vive
   en `SIESA-GENERAL`— «no lo encontré» es lo normal: CC1 = 0, CC2 = 0 →
   coinciden → **ajuste automático a cero en Siesa**.
2. **La caja contaba 1.** `procesarScan` mandaba `cantidad: 1` para CONTEO y el
   servidor sumaba `cantidad` sin mirar si el código era el EAN del empaque.
3. **No se podía teclear.** El botón manual estaba oculto para conteo y el
   servidor ignoraba `cantidad_manual` en CONTEO. En el Conteo Definitivo el
   «manual» se pintaba y NO viajaba: se confirmaba lo último escaneado.
4. **Reintento de red = doble conteo.** CONTEO no mandaba `total_acumulado`,
   el servidor hacía `+=`, y el debounce de 5 s que decía cubrirlo solo se
   ESCRIBE en la rama de picking.
5. **«UBICACIÓN: SIESA-GENERAL»** en grande, y un «conteo pendiente AQUÍ»
   intercalado sobre esa ubicación virtual.
6. **«Reportar problema» dejaba BLOQUEADO sin salida**: `/cancelar` no lo
   aceptaba y la raíz bloqueada no trababa el hueco.

## La clase, no el caso

*«Un conteo se confirma sin que alguien diga cuánto contó.»* Todo cierre pasa
por `ConteoService.registrar_conteo`, que exige la cantidad declarada
(`exigir_cantidad_declarada`). Y ningún sitio que llama `registrar_conteo`
puede derivar la cantidad de un default: trinquete por AST con inventario
declarado de los que llaman (`TestNingunCierreDeConteoDerivaLaCantidad`), con
meta-tests (lo que marca, lo que no, piso).

Las mutaciones (quitar el rechazo, volver a `cantidad: 1`, volver a `+=`,
«no lo encontré» cerrando en cero) se hicieron y verificaron — ver el commit.
"""
import ast
import json
import pathlib
import shutil
import subprocess

import pytest

from tests.test_conteo_teorico_pos import SKU, _jobs, siesa, tienda  # noqa: F401 (fixtures)

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PWA = RAIZ / 'app' / 'static' / 'pwa'

CAJA = '7700000000123'     # EAN del empaque de 12
UNIDAD = '7700000000017'   # EAN de la unidad


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _mob():
    from app.services.mobile_service import MobileService
    return MobileService


def _sesion(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


@pytest.fixture
def conteo(db, siesa, tienda):
    """Un CC1 abierto por el operario `a`, con Siesa diciendo 10 (sin POS) y
    el producto vendido en cajas de 12."""
    from app.models.conteo import SesionConteo
    p = tienda['producto']
    p.codigo_barras = UNIDAD
    p.codigo_barras_empaque = CAJA
    p.factor_conversion = 12
    p.unidad_empaque = 'cja'
    db.session.commit()
    siesa.poner(existencia=10, pos=0)
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
    sid = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id
    _svc().obtener_tarea_operario(sid, tienda['a'].id)
    return sid


def _auth(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Un conteo no se cierra sin que alguien diga cuánto contó
# ─────────────────────────────────────────────────────────────────────────────

class TestUnConteoNoSeCierraSinCantidadDeclarada:

    def test_sin_total_se_rechaza_y_la_sesion_sigue_abierta(self, db, tienda, conteo):
        with pytest.raises(ValueError, match='Falta la cantidad contada'):
            _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO')
        s = _sesion(db, conteo)
        assert s.estado == 'EN_PROCESO' and s.hijo_conteo is None
        assert not _jobs(conteo)

    def test_lo_guardado_no_es_una_declaracion(self, db, tienda, conteo):
        """Escaneó 5 y confirmó sin total (PWA vieja): tampoco. Lo guardado es
        un borrador, no lo que el operario dijo al cerrar."""
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 5)
        with pytest.raises(ValueError, match='Falta la cantidad contada'):
            _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO')
        assert _sesion(db, conteo).estado == 'EN_PROCESO'

    def test_cero_sin_confirmar_se_rechaza(self, db, tienda, conteo):
        with pytest.raises(ValueError, match='confirmá que NO hay ninguna'):
            _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO', total_contado=0)
        assert _sesion(db, conteo).estado == 'EN_PROCESO'

    def test_cero_confirmado_es_un_dato(self, db, tienda, conteo):
        r = _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO',
                                   total_contado=0, cero_confirmado=True)
        # Un cero contra un teórico de 10 está fuera de tolerancia: el mismo
        # operario recuenta. El cero quedó registrado como lo que se contó.
        assert r['resultado'] == 'RECONTAR_TU'
        assert _sesion(db, conteo).lista_conteos_descartados()[-1]['cantidad_fisica'] == 0
        r = _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO',
                                   total_contado=0, cero_confirmado=True)
        assert r['resultado'] == 'SEGUNDO_CONTEO'
        assert _sesion(db, conteo).cantidad_fisica == 0

    def test_manda_lo_declarado_no_lo_guardado(self, db, tienda, conteo):
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 3)
        r = _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO', total_contado=10)
        assert r['resultado'] == 'MATCH'
        assert _sesion(db, conteo).cantidad_fisica == 10

    @pytest.mark.parametrize('malo', [-1, True, 2.5, 'diez'])
    def test_cantidad_invalida(self, db, tienda, conteo, malo):
        with pytest.raises(ValueError):
            _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO', total_contado=malo)

    def test_la_api_directa_tambien_lo_exige(self, db, tienda, conteo):
        """`registrar_conteo` es por donde pasa TODO cierre: la regla vive ahí."""
        with pytest.raises(ValueError, match='Falta la cantidad contada'):
            _svc().registrar_conteo(conteo, tienda['a'].id, None)
        with pytest.raises(ValueError, match='confirmá'):
            _svc().registrar_conteo(conteo, tienda['a'].id, 0)

    def test_por_http(self, app, client, db, tienda, conteo):
        h = _auth(app, tienda['a'])
        r = client.post('/api/mobile/confirmar', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'items_escaneados': []}, headers=h)
        assert r.status_code == 400 and 'Falta la cantidad' in r.get_json()['error']
        r = client.post('/api/mobile/confirmar', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'total_contado': 0,
            'cero_confirmado': 'si'}, headers=h)
        assert r.status_code == 400, 'un «si» no es un true: el cero no quedó confirmado'
        r = client.post('/api/mobile/confirmar', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'total_contado': 10}, headers=h)
        assert r.status_code == 200 and r.get_json()['resultado'] == 'MATCH'

    def test_la_confirmacion_offline_viaja_con_el_total(self, app, client, db, tienda, conteo):
        h = _auth(app, tienda['a'])
        r = client.post('/api/mobile/sync', json={'cola': [
            {'tarea_id': conteo, 'tipo': 'CONTEO', 'items_escaneados': [],
             'total_contado': 10, '_qid': 'q1'}]}, headers=h)
        assert r.get_json()['sincronizados'] == 1
        assert _sesion(db, conteo).estado == 'MATCH'


# ─────────────────────────────────────────────────────────────────────────────
# 2 · «No lo encontré» no es un cero
# ─────────────────────────────────────────────────────────────────────────────

class TestNoLoEncontreNoEsUnCero:

    def test_el_caso_caro_ya_no_ajusta(self, db, tienda, conteo):
        """Antes: CC1 «nada» = 0 → CC2; CC2 «nada» = 0 → coinciden → AJ-SAL 10.
        Ahora: se bloquea, no hay CC2, no hay ajuste."""
        r = _svc().bloquear_conteo(conteo, tienda['a'].id, 'NO_ENCONTRADO')
        assert r['ok'] and r['motivo'] == 'NO_ENCONTRADO'
        s = _sesion(db, conteo)
        assert s.estado == 'BLOQUEADO' and s.motivo_bloqueo == 'NO_ENCONTRADO'
        assert s.bloqueado_en is not None
        assert s.cantidad_fisica is None, 'un «no lo encontré» no escribe un cero'
        assert s.hijo_conteo is None, 'no genera segundo conteo'
        assert not _jobs(conteo), 'no genera ajuste'

    def test_despues_de_bloquear_no_se_confirma_ni_se_ajusta(self, db, tienda, conteo):
        _svc().bloquear_conteo(conteo, tienda['a'].id, 'NO_ENCONTRADO')
        with pytest.raises(ValueError):
            _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO',
                                   total_contado=0, cero_confirmado=True)
        with pytest.raises(ValueError):
            _svc().confirmar_ajuste(conteo, tienda['supervisor'].id)
        assert _sesion(db, conteo).estado == 'BLOQUEADO' and not _jobs(conteo)

    def test_la_raiz_bloqueada_traba_el_hueco(self, db, tienda, conteo):
        from app.models.conteo import SesionConteo
        _svc().bloquear_conteo(conteo, tienda['a'].id, 'NO_ENCONTRADO')
        vivas = SesionConteo.query.filter(
            SesionConteo.raiz_con_cadena_viva(incluye_descuadre=False)).all()
        assert [s.id for s in vivas] == [conteo]
        r = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        assert r['tareas_creadas'] == 0, 'abrió otra cadena al lado de la bloqueada'

    def test_otro_problema_exige_decir_cual(self, db, tienda, conteo):
        with pytest.raises(ValueError, match='Contá qué pasó'):
            _svc().bloquear_conteo(conteo, tienda['a'].id, 'OTRO', '  ')
        _svc().bloquear_conteo(conteo, tienda['a'].id, 'OTRO', 'estante roto')
        assert _sesion(db, conteo).motivo_edicion == '[OTRO] estante roto'

    def test_solo_el_dueno_y_motivos_conocidos(self, app, client, db, tienda, conteo):
        with pytest.raises(PermissionError):
            _svc().bloquear_conteo(conteo, tienda['b'].id, 'NO_ENCONTRADO')
        with pytest.raises(ValueError, match='desconocido'):
            _svc().bloquear_conteo(conteo, tienda['a'].id, 'INVENTADO')
        r = client.post('/api/mobile/reportar-problema', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'motivo': 'NO_ENCONTRADO'},
            headers=_auth(app, tienda['b']))
        assert r.status_code == 403

    def test_la_pwa_vieja_sigue_pudiendo_reportar(self, app, client, db, tienda, conteo):
        """El modal viejo mandaba UBICACION_VACIA: se acepta y se guarda tal cual."""
        r = client.post('/api/mobile/reportar-problema', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'motivo': 'UBICACION_VACIA'},
            headers=_auth(app, tienda['a']))
        assert r.status_code == 200, r.get_json()
        assert _sesion(db, conteo).motivo_bloqueo == 'UBICACION_VACIA'


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Un BLOQUEADO tiene salida: el líder reabre o cancela
# ─────────────────────────────────────────────────────────────────────────────

def _cc2_bloqueado(db, tienda, conteo):
    """CC1 = 7 (≠ 10, fuera de tolerancia: `a` recuenta 7) → CC2 para `b`,
    que no lo encuentra."""
    r1 = _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO', total_contado=7)
    assert r1['resultado'] == 'RECONTAR_TU', r1
    r1 = _mob().confirmar_tarea(tienda['a'].id, conteo, 'CONTEO', total_contado=7)
    cc2 = r1['segundo_conteo_id']
    _svc().obtener_tarea_operario(cc2, tienda['b'].id)
    _svc().bloquear_conteo(cc2, tienda['b'].id, 'NO_ENCONTRADO')
    return cc2


class TestElBloqueadoTieneSalida:

    def test_el_lider_los_lista_con_su_motivo(self, app, client, db, tienda, conteo):
        # La cola del líder vive en el tablero (📥 Por decidir); el
        # `GET /bloqueados` que la duplicaba se retiró el 2026-09-24.
        cc2 = _cc2_bloqueado(db, tienda, conteo)
        url = f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}'
        r = client.get(url, headers=_auth(app, tienda['supervisor']))
        filas = r.get_json()['decisiones']['bloqueados']['filas']
        assert [(f['id'], f['nivel'], f['motivo_bloqueo']) for f in filas] == [
            (cc2, 'CC2', 'NO_ENCONTRADO')]
        assert 'cantidad_fisica' not in filas[0]
        r = client.get(url, headers=_auth(app, tienda['a']))
        assert r.status_code == 403

    def test_reabrir_vuelve_a_la_cola_desde_cero(self, db, tienda, conteo):
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 4)
        _svc().bloquear_conteo(conteo, tienda['a'].id, 'NO_ENCONTRADO')
        _svc().reabrir_bloqueado(conteo, tienda['supervisor'].id, nota='mirá el fondo')
        s = _sesion(db, conteo)
        assert (s.estado, s.operario_id, s.cantidad_fisica, s.motivo_bloqueo) == (
            'PENDIENTE', None, None, None)
        assert s.foto_inicio_at is None, 'la foto de inicio se relee al abrirlo'
        assert 'mirá el fondo' in s.motivo_edicion and 'NO_ENCONTRADO' in s.motivo_edicion

    def test_reabrir_no_dobla_el_doble_ciego(self, db, tienda, conteo):
        cc2 = _cc2_bloqueado(db, tienda, conteo)
        with pytest.raises(ValueError, match='Doble ciego'):
            _svc().reabrir_bloqueado(cc2, tienda['supervisor'].id, operario_id=tienda['a'].id)
        assert _sesion(db, cc2).estado == 'BLOQUEADO'
        _svc().reabrir_bloqueado(cc2, tienda['supervisor'].id)
        assert _sesion(db, cc2).estado == 'PENDIENTE'
        assert _sesion(db, conteo).estado == 'SEGUNDO_CONTEO'

    def test_un_operario_no_decide(self, app, client, db, tienda, conteo):
        _svc().bloquear_conteo(conteo, tienda['a'].id, 'NO_ENCONTRADO')
        h = _auth(app, tienda['a'])
        assert client.post(f'/api/conteo/{conteo}/reabrir', json={}, headers=h).status_code == 403
        with pytest.raises(PermissionError):
            _svc().cancelar_bloqueado(conteo, tienda['a'].id, 'x')

    def test_cancelar_cancela_la_cadena(self, app, client, db, tienda, conteo):
        """Cancelar solo el CC2 dejaba la raíz en SEGUNDO_CONTEO para siempre."""
        cc2 = _cc2_bloqueado(db, tienda, conteo)
        h = _auth(app, tienda['supervisor'])
        r = client.put(f'/api/conteo/{cc2}/cancelar', json={}, headers=h)
        assert r.status_code == 400, 'sin motivo no se cancela'
        r = client.put(f'/api/conteo/{cc2}/cancelar', json={'motivo': 'no existe'}, headers=h)
        assert r.status_code == 200, r.get_json()
        assert _sesion(db, cc2).estado == 'CANCELADO'
        assert _sesion(db, conteo).estado == 'CANCELADO'
        assert not _jobs(conteo)
        r = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        assert r['tareas_creadas'] == 1, 'cancelada la cadena, el hueco se puede volver a contar'

    def test_reabrir_no_abre_una_segunda_cadena(self, db, tienda, conteo):
        """Un bloqueado de antes de m031hud pudo quedar con otra cadena al lado."""
        from app.models.conteo import SesionConteo
        s = _sesion(db, conteo)
        s.estado = 'BLOQUEADO'
        otra = SesionConteo(codigo='CC-OTRA', tipo='MANUAL', ubicacion_id=s.ubicacion_id,
                            almacen_id=s.almacen_id, producto_id=s.producto_id,
                            estado='PENDIENTE')
        db.session.add(otra)
        db.session.commit()
        with pytest.raises(ValueError, match='Ya hay otro conteo vivo'):
            _svc().reabrir_bloqueado(conteo, tienda['supervisor'].id)

    def test_omitir_segundo_no_deja_el_bloqueado_colgado(self, app, client, db, tienda, conteo):
        cc2 = _cc2_bloqueado(db, tienda, conteo)
        from app.models.usuario import Usuario
        admin = Usuario(nombre='adm', email='adm-hud@test.com', rol='admin', activo=True,
                        almacen_id=tienda['almacen'].id)
        admin.set_password('x')
        db.session.add(admin)
        db.session.commit()
        r = client.post(f'/api/conteo/{conteo}/omitir-segundo', headers=_auth(app, admin))
        assert r.status_code == 200, r.get_json()
        assert _sesion(db, cc2).estado == 'CANCELADO'
        assert _svc().listar_bloqueados() == []


# ─────────────────────────────────────────────────────────────────────────────
# 4 · El escaneo: idempotente, y la caja vale su factor
# ─────────────────────────────────────────────────────────────────────────────

class TestEscaneoIdempotenteYConFactor:

    def _scan(self, tienda, sid, codigo, previo, cantidad=1):
        return _mob().procesar_escaneo(tienda['a'].id, sid, 'CONTEO', codigo,
                                       cantidad=cantidad, total_previo=previo)

    def test_sin_total_previo_se_rechaza(self, db, tienda, conteo):
        """Una PWA vieja en caché sumaba a ciegas. Ahora se le dice qué hacer."""
        with pytest.raises(ValueError, match='desactualizada'):
            _mob().procesar_escaneo(tienda['a'].id, conteo, 'CONTEO', UNIDAD, cantidad=1)
        assert _sesion(db, conteo).cantidad_fisica is None

    def test_un_reintento_no_suma_dos_veces(self, db, tienda, conteo):
        r1 = self._scan(tienda, conteo, UNIDAD, 0)
        r2 = self._scan(tienda, conteo, UNIDAD, 0)       # la respuesta se perdió
        assert r1['cantidad_contada'] == r2['cantidad_contada'] == 1
        assert _sesion(db, conteo).cantidad_fisica == 1

    def test_la_caja_suma_su_factor(self, db, tienda, conteo):
        r = self._scan(tienda, conteo, CAJA, 3)
        assert (r['cantidad_contada'], r['unidades_este_scan'], r['es_empaque']) == (15, 12, True)
        assert '+12' in r['mensaje']

    def test_el_empaque_ya_resuelto_por_el_pwa(self, db, tienda, conteo):
        """GS1 resuelto en `/api/empaques/scan`: llega el código base con la
        cantidad en unidades — no se multiplica otra vez."""
        r = self._scan(tienda, conteo, SKU, 0, cantidad=12)
        assert r['cantidad_contada'] == 12 and r['es_empaque'] is False

    def test_producto_ajeno_y_sesion_ajena(self, db, tienda, conteo):
        with pytest.raises(ValueError, match='Producto incorrecto'):
            self._scan(tienda, conteo, 'OTRA-COSA', 0)
        with pytest.raises(ValueError, match='no está asignada'):
            _mob().procesar_escaneo(tienda['b'].id, conteo, 'CONTEO', UNIDAD, total_previo=0)

    def test_teclear_fija_el_total(self, db, tienda, conteo):
        r = _mob().fijar_total_conteo(tienda['a'].id, conteo, 400)
        assert r['cantidad_contada'] == 400
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 400)   # reintento
        assert _sesion(db, conteo).cantidad_fisica == 400
        with pytest.raises(ValueError):
            _mob().fijar_total_conteo(tienda['a'].id, conteo, -3)
        with pytest.raises(ValueError, match='no está asignada'):
            _mob().fijar_total_conteo(tienda['b'].id, conteo, 1)

    def test_por_http(self, app, client, db, tienda, conteo):
        h = _auth(app, tienda['a'])
        r = client.post('/api/mobile/escanear', json={
            'tarea_id': conteo, 'tipo': 'CONTEO', 'codigo': CAJA, 'cantidad': 1,
            'total_previo': 0}, headers=h)
        assert r.status_code == 200 and r.get_json()['cantidad_contada'] == 12
        r = client.post('/api/mobile/conteo/total', json={
            'tarea_id': conteo, 'total_acumulado': 30}, headers=h)
        assert r.status_code == 200 and r.get_json()['cantidad_contada'] == 30


class TestPickingNoCambia:
    """Picking comparte `procesar_escaneo` y ahora `_unidades_del_escaneo`."""

    def test_el_empaque_de_picking_sigue_multiplicando(self, db, tienda):
        from app.models.picking import TareaPicking
        p = tienda['producto']
        p.codigo_barras_empaque = CAJA
        p.factor_conversion = 12
        t = TareaPicking(codigo='PK-HUD-1', producto_id=p.id, cantidad_solicitada=30,
                         ubicacion_id=tienda['ubicacion'].id, almacen_id=tienda['almacen'].id,
                         operario_id=tienda['a'].id, estado='EN_PROCESO')
        db.session.add(t)
        db.session.commit()
        r = _mob().procesar_escaneo(tienda['a'].id, t.id, 'PICKING', CAJA)
        assert (r['cantidad_actual'], r['unidades_este_scan'], r['es_empaque']) == (12, 12, True)
        assert r['empaques_escaneados'] == 1
        r = _mob().procesar_escaneo(tienda['a'].id, t.id, 'PICKING', SKU,
                                    cantidad=1, total_acumulado=5)
        assert r['cantidad_actual'] == 12, 'total_acumulado de picking sigue siendo MAX'


# ─────────────────────────────────────────────────────────────────────────────
# 5 · El HUD: producto en grande, la ubicación solo si es física
# ─────────────────────────────────────────────────────────────────────────────

class TestLoQuePintaElHud:

    def test_siesa_general_no_es_una_ubicacion(self, db, tienda, conteo):
        from app.models.ubicacion import Ubicacion
        assert Ubicacion(codigo=Ubicacion.CODIGO_GENERAL, almacen_id=1).es_fisica is False
        assert Ubicacion(codigo='A-01-02', almacen_id=1).es_fisica is True
        s = _sesion(db, conteo)
        s.ubicacion.codigo = Ubicacion.CODIGO_GENERAL
        v = _svc().vista_hud(s)
        assert v['ubicacion_fisica'] is False
        assert (v['factor_conversion'], v['unidad_empaque'], v['producto_codigo_barras']) == (
            12, 'CJA', UNIDAD)

    def test_la_tarea_del_dispensador_trae_la_vista(self, db, tienda, conteo):
        d = _mob()._conteo_a_dict(_sesion(db, conteo))
        assert {'ubicacion_fisica', 'factor_conversion', 'producto_codigo_barras'} <= set(d)
        assert 'existencia_siesa' not in d and 'teorico_siesa' not in d

    def test_el_definitivo_retoma_lo_suyo_sin_ver_lo_ajeno(self, db, tienda, conteo):
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 6)
        t = _svc().obtener_tarea_operario(conteo, tienda['a'].id)
        assert t['cantidad_contada'] == 6
        assert 'existencia_siesa' not in t and 'teorico_siesa' not in t

    def test_no_se_intercala_un_conteo_sobre_siesa_general(self, db, tienda, conteo):
        from app.models.picking import TareaPicking
        from app.models.ubicacion import Ubicacion
        s = _sesion(db, conteo)
        s.operario_id = None
        s.estado = 'PENDIENTE'
        s.ubicacion.codigo = Ubicacion.CODIGO_GENERAL
        t = TareaPicking(codigo='PK-HUD-2', producto_id=s.producto_id, cantidad_solicitada=1,
                         ubicacion_id=s.ubicacion_id, almacen_id=s.almacen_id,
                         estado='PENDIENTE', tipo_documento='PEDIDO', referencia_documento='PDX')
        db.session.add(t)
        db.session.commit()
        d = _mob().get_tarea_actual(tienda['b'].id)
        assert d['tipo'] == 'PICKING' and d['conteo_intercalado'] is None
        assert _sesion(db, conteo).operario_id is None, 'le colgó el conteo igual'


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Mercancía sin código: nota para el líder, no toca el conteo
# ─────────────────────────────────────────────────────────────────────────────

class TestMercanciaSinCodigo:

    def test_no_toca_el_conteo_y_el_lider_la_resuelve(self, app, client, db, tienda, conteo):
        _mob().fijar_total_conteo(tienda['a'].id, conteo, 2)
        r = client.post('/api/mobile/conteo/sin-codigo', json={
            'tarea_id': conteo, 'descripcion': '3 cajas sin etiqueta al fondo'},
            headers=_auth(app, tienda['a']))
        assert r.status_code == 200, r.get_json()
        s = _sesion(db, conteo)
        assert (s.estado, s.cantidad_fisica) == ('EN_PROCESO', 2)
        hs = _auth(app, tienda['supervisor'])
        url = f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}'
        filas = client.get(url, headers=hs).get_json()['decisiones']['novedades']['filas']
        assert [f['descripcion'] for f in filas] == ['3 cajas sin etiqueta al fondo']
        assert filas[0]['producto_en_conteo'] == SKU
        nid = filas[0]['id']
        assert client.post(f'/api/conteo/novedades/{nid}/resolver', json={},
                           headers=hs).status_code == 400
        assert client.post(f'/api/conteo/novedades/{nid}/resolver', json={'nota': 'era X'},
                           headers=hs).status_code == 200
        assert client.get(url, headers=hs).get_json()['decisiones']['novedades']['total'] == 0

    def test_ajeno_y_vacio_rechazados(self, app, client, db, tienda, conteo):
        with pytest.raises(PermissionError):
            _svc().registrar_novedad_sin_codigo(conteo, tienda['b'].id, 'algo raro')
        with pytest.raises(ValueError):
            _svc().registrar_novedad_sin_codigo(conteo, tienda['a'].id, ' ')
        assert client.get(f'/api/conteo/lider/tablero?almacen_id={tienda["almacen"].id}',
                          headers=_auth(app, tienda['a'])).status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# 7 · Trinquete: ningún cierre de conteo deriva la cantidad de un default
# ─────────────────────────────────────────────────────────────────────────────
#
# Por AST, sobre `app/`: cada llamada a `registrar_conteo` y la expresión que
# le llega como cantidad (tercer posicional o `cantidad_fisica=`). Si es un
# nombre, se miran TODAS sus asignaciones en la función. Se marca:
#   · un literal numérico (`0`);
#   · un ternario o un `or` con un literal (`x if … else 0`, `x or 0`);
#   · un `.get(…, default)`;
#   · leer `.cantidad_fisica` — lo guardado es un borrador, no una declaración.

#: Quién llama `registrar_conteo` en `app/`, y de dónde sale la cantidad.
LLAMADORES_DECLARADOS = {
    'app/services/mobile_service.py::MobileService.confirmar_tarea': (
        'El HUD de conteo (operario y Conteo Definitivo): `total_contado`, lo '
        'que el operario declaró al apretar «Ya revisé todo — contar N».'),
    'app/routes/conteo.py::registrar_conteo': (
        'POST /api/conteo/<id>/registrar (sin UI, DEUDA_SIN_UI): '
        '`int(data["cantidad_fisica"])`, exigida en el cuerpo.'),
}


def _es_default(expr) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) \
                and not isinstance(n.value, bool):
            return True
        if isinstance(n, ast.Attribute) and n.attr == 'cantidad_fisica':
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == 'get' and len(n.args) >= 2:
            return True
    return False


def _cantidad_de(call):
    for kw in call.keywords:
        if kw.arg == 'cantidad_fisica':
            return kw.value
    return call.args[2] if len(call.args) >= 3 else None


def _llamadas_a_registrar(fuentes):
    """`{archivo::Clase.funcion: [(linea, derivada_de_default)]}`."""
    hallados = {}
    for rel, src in fuentes.items():
        arbol = ast.parse(src)

        def visitar(nodo, pila, funcion):
            for hijo in ast.iter_child_nodes(nodo):
                if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visitar(hijo, pila + [hijo.name], hijo)
                    continue
                if isinstance(hijo, ast.ClassDef):
                    visitar(hijo, pila + [hijo.name], funcion)
                    continue
                if isinstance(hijo, ast.Call):
                    f = hijo.func
                    nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                    if nombre == 'registrar_conteo' and funcion is not None:
                        expr = _cantidad_de(hijo)
                        malo = expr is None or _es_default(expr)
                        if isinstance(expr, ast.Name):
                            for a in ast.walk(funcion):
                                if isinstance(a, (ast.Assign, ast.AnnAssign)) and a.value is not None:
                                    objetivos = a.targets if isinstance(a, ast.Assign) else [a.target]
                                    if any(isinstance(t, ast.Name) and t.id == expr.id
                                           for t in objetivos) and _es_default(a.value):
                                        malo = True
                        clave = f'{rel}::{".".join(pila)}'
                        hallados.setdefault(clave, []).append((hijo.lineno, malo))
                visitar(hijo, pila, funcion)

        visitar(arbol, [], None)
    return hallados


def _fuentes_app():
    return {str(f.relative_to(RAIZ)): f.read_text(encoding='utf-8')
            for f in sorted((RAIZ / 'app').rglob('*.py'))}


class TestNingunCierreDeConteoDerivaLaCantidad:

    def test_ningun_llamador_deriva_la_cantidad_de_un_default(self):
        malos = {k: [ln for ln, m in v if m] for k, v in _llamadas_a_registrar(_fuentes_app()).items()}
        malos = {k: v for k, v in malos.items() if v}
        assert not malos, (
            f'\nUn cierre de conteo deriva la cantidad de un default: {malos}\n'
            'La cantidad la DECLARA quien contó. Si no contó, el cierre es '
            '«No lo encontré» (bloquear_conteo), no un cero.')

    def test_los_llamadores_son_los_declarados(self):
        """La lista no crece sin decisión, y solo encoge."""
        assert set(_llamadas_a_registrar(_fuentes_app())) == set(LLAMADORES_DECLARADOS)

    def test_cada_llamador_dice_de_donde_sale_la_cantidad(self):
        assert not [k for k, v in LLAMADORES_DECLARADOS.items() if len(v) < 40]

    def test_el_servicio_rechaza_sin_cantidad(self):
        """La otra mitad de la clase: aunque un llamador pase None, no pasa."""
        with pytest.raises(ValueError):
            _svc().exigir_cantidad_declarada(None)
        with pytest.raises(ValueError):
            _svc().exigir_cantidad_declarada(0)
        assert _svc().exigir_cantidad_declarada(0, cero_confirmado=True) == 0

    # ── meta-tests: el detector muerde ──────────────────────────────────────

    def _mira(self, src):
        return _llamadas_a_registrar({'x.py': src})

    def test_ve_el_defecto_original(self):
        src = ('class M:\n'
               '    def confirmar(s, sesion):\n'
               '        cantidad = sesion.cantidad_fisica if sesion.cantidad_fisica is not None else 0\n'
               '        return C.registrar_conteo(sesion_id=1, operario_id=2, cantidad_fisica=cantidad)\n')
        assert self._mira(src) == {'x.py::M.confirmar': [(4, True)]}

    @pytest.mark.parametrize('expr', [
        '0', 'total or 0', "data.get('cantidad', 0)", 'sesion.cantidad_fisica',
        'x if x is not None else 0'])
    def test_ve_cada_forma_de_default(self, expr):
        src = f'def f(data, total, sesion, x):\n    return S.registrar_conteo(1, 2, {expr})\n'
        assert self._mira(src) == {'x.py::f': [(2, True)]}

    def test_no_marca_lo_declarado(self):
        src = ('def f(data, total_contado):\n'
               "    c = int(data['cantidad_fisica'])\n"
               '    S.registrar_conteo(1, 2, c)\n'
               '    S.registrar_conteo(1, 2, cantidad_fisica=total_contado, cero_confirmado=True)\n')
        assert self._mira(src) == {'x.py::f': [(3, False), (4, False)]}

    def test_piso(self):
        """Un escáner que se rompe devuelve cero y parece verde."""
        assert sum(len(v) for v in _llamadas_a_registrar(_fuentes_app()).values()) >= 2


# ─────────────────────────────────────────────────────────────────────────────
# 8 · El HUD ejecutado en Node: util.js y conteo.js reales
# ─────────────────────────────────────────────────────────────────────────────
#
# Se carga `util.js` de verdad (un `esc` stubbeado probaría el stub) y
# `conteo.js` entero; de `app.js` se toman LITERALES `resolverEscaneoEmpaque` y
# `postConReintento` —la resolución de empaques y el reintento son parte de lo
# que se prueba—. Lo demás de `app.js` (get/post, alerta, modales) se stubea.

HARNESS = r"""
const fs = require('fs'); const vm = require('vm');
const [UTIL, CONTEO, APP, GUION] = process.argv.slice(2);
const guion = JSON.parse(fs.readFileSync(GUION, 'utf-8'));
function el(id) { return { id, innerHTML: '', textContent: '', value: '', style: {}, disabled: false,
  blur() {}, focus() {}, remove() {}, appendChild() {}, addEventListener() {},
  querySelector() { return null; }, querySelectorAll() { return []; } }; }
const els = {};
const envios = []; const alertas = [];
let fallarUna = guion.fallarRed || 0;
const ctx = {
  console, setTimeout: (f) => { Promise.resolve().then(f); return 0; }, clearTimeout() {}, Promise, JSON, Number, String, Math, Object, Array, RegExp,
  document: { getElementById(id) { return els[id] || (els[id] = el(id)); }, createElement: () => el('x'),
              body: { appendChild() {} } },
  OPERARIO: { puede_usar_camara: false }, TAREA_ACTUAL: null,
  alerta: (m, t) => alertas.push([String(m), t]), beepOk() {}, beepError() {}, beepDone() {},
  vibrar() {}, flash() {}, pedirTarea() {}, guardarOffline(p) { envios.push(['OFFLINE', p]); },
  _modalConfirmar: async () => guion.confirmar, _modalTexto: async () => guion.texto,
  get: async (url) => guion.scan,
  post: async (url, body) => {
    if (fallarUna > 0) { fallarUna--; envios.push(['CAIDO', url, body]); throw new Error('red'); }
    envios.push([url, body]);
    if (url === '/api/mobile/escanear') return { cantidad_contada: body.total_previo + (guion.unidades || body.cantidad), es_empaque: !!guion.esEmpaque, unidad_empaque: 'CJA' };
    if (url === '/api/mobile/conteo/total') return { cantidad_contada: body.total_acumulado };
    return guion.respuesta || { resultado: 'MATCH', mensaje: 'ok' };
  },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(UTIL, 'utf-8'), ctx);
const app = fs.readFileSync(APP, 'utf-8');
function extraer(nombre) {
  const i = app.indexOf('async function ' + nombre);
  const j = app.indexOf('\n}\n', i);
  if (i < 0 || j < 0) throw new Error('no encontré ' + nombre + ' en app.js');
  return app.slice(i, j + 3);
}
vm.runInContext(extraer('resolverEscaneoEmpaque') + extraer('postConReintento'), ctx);
vm.runInContext(fs.readFileSync(CONTEO, 'utf-8'), ctx);
vm.runInContext(`TAREA_ACTUAL = ${JSON.stringify(guion.tarea)};`, ctx);
(async () => {
  ctx.__t = guion.tarea;
  vm.runInContext("conteoHudAbrir(__t, 'OPERARIO', 'contenido-tarea')", ctx);
  const html = els['contenido-tarea'].innerHTML;
  for (const paso of guion.pasos) {
    if (paso.scan) await vm.runInContext(`conteoHudScan(${JSON.stringify(paso.scan)})`, ctx);
    if (paso.teclear) { els['chud-cant'] = els['chud-cant'] || el('chud-cant'); els['chud-cant'].value = paso.teclear;
                        await vm.runInContext('conteoHudSumarTecleado()', ctx); }
    if (paso.deshacer) await vm.runInContext('conteoHudDeshacer()', ctx);
    if (paso.confirmar) await vm.runInContext('conteoHudConfirmar()', ctx);
    if (paso.noEncontrado) await vm.runInContext('conteoHudNoEncontrado()', ctx);
  }
  const h = vm.runInContext('CONTEO_HUD', ctx);
  process.stdout.write(JSON.stringify({ html, envios, alertas,
    total: h ? h.total : null, ultimo: h ? h.ultimo : null }));
})().catch(e => { console.error(e); process.exit(1); });
"""


def _correr_hud(tmp_path, guion):
    if not shutil.which('node'):
        pytest.skip('sin node')
    base = {'tarea': {'id': 7, 'tipo': 'CONTEO', 'producto_codigo': SKU,
                      'producto_nombre': 'Cuaderno <b>x</b>', 'producto_codigo_barras': UNIDAD,
                      'ubicacion': 'SIESA-GENERAL', 'ubicacion_fisica': False,
                      'factor_conversion': 12, 'unidad_empaque': 'CJA',
                      'cantidad_escaneada': 0},
            'scan': {'tipo': 'NO_ENCONTRADO'}, 'pasos': [], 'confirmar': True, 'texto': None}
    base.update(guion)
    (tmp_path / 'h.js').write_text(HARNESS, encoding='utf-8')
    (tmp_path / 'g.json').write_text(json.dumps(base), encoding='utf-8')
    out = subprocess.run(['node', str(tmp_path / 'h.js'), str(PWA / 'util.js'),
                          str(PWA / 'conteo.js'), str(PWA / 'app.js'), str(tmp_path / 'g.json')],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _escaneos(r):
    return [b for u, *rest in r['envios'] for b in rest[-1:] if u == '/api/mobile/escanear']


class TestElHudEjecutado:

    def test_la_caja_resuelta_viaja_con_su_factor(self, tmp_path):
        """GS1 de la caja (factor 12): el PWA manda 12, no 1."""
        r = _correr_hud(tmp_path, {
            'scan': {'tipo': 'GS1_UNICO', 'producto': {'codigo': SKU}, 'factor': 12,
                     'empaque': {'unidad_medida': 'CJA'}},
            'pasos': [{'scan': CAJA}]})
        assert _escaneos(r) == [{'tarea_id': 7, 'tipo': 'CONTEO', 'codigo': SKU,
                                 'cantidad': 12, 'total_previo': 0}]
        assert (r['total'], r['ultimo']) == (12, '+12 (cja)')

    def test_siempre_manda_lo_que_tenia_antes(self, tmp_path):
        """Dos escaneos: el segundo sale con el resultado del primero."""
        r = _correr_hud(tmp_path, {'pasos': [{'scan': UNIDAD}, {'scan': UNIDAD}]})
        assert [e['total_previo'] for e in _escaneos(r)] == [0, 1]
        assert r['total'] == 2

    def test_el_reintento_de_red_repite_el_mismo_total_previo(self, tmp_path):
        r = _correr_hud(tmp_path, {'fallarRed': 1, 'pasos': [{'scan': UNIDAD}]})
        caidos = [b for u, _, b in [e for e in r['envios'] if e[0] == 'CAIDO']]
        assert caidos and caidos[0]['total_previo'] == 0
        assert [e['total_previo'] for e in _escaneos(r)] == [0]
        assert r['total'] == 1

    def test_teclear_una_pila_y_deshacer(self, tmp_path):
        r = _correr_hud(tmp_path, {'pasos': [{'scan': UNIDAD}, {'teclear': '400'}, {'deshacer': True}]})
        totales = [b['total_acumulado'] for u, b in
                   [e for e in r['envios'] if e[0] == '/api/mobile/conteo/total']]
        assert totales == [401, 1]
        assert r['total'] == 1

    def test_un_codigo_de_barras_en_la_caja_de_cantidad_no_se_suma(self, tmp_path):
        r = _correr_hud(tmp_path, {'pasos': [{'teclear': UNIDAD}]})
        assert not [e for e in r['envios'] if e[0] == '/api/mobile/conteo/total']
        assert r['total'] == 0 and 'código de barras' in r['alertas'][-1][0]

    def test_confirmar_manda_siempre_el_total(self, tmp_path):
        r = _correr_hud(tmp_path, {'pasos': [{'teclear': '7'}, {'confirmar': True}]})
        conf = [b for u, b in [e for e in r['envios'] if e[0] == '/api/mobile/confirmar']]
        assert conf == [{'tarea_id': 7, 'tipo': 'CONTEO', 'items_escaneados': [], 'total_contado': 7}]

    def test_el_cero_se_confirma_o_no_sale(self, tmp_path):
        r = _correr_hud(tmp_path, {'confirmar': False, 'pasos': [{'confirmar': True}]})
        assert not [e for e in r['envios'] if e[0] == '/api/mobile/confirmar']
        r = _correr_hud(tmp_path, {'confirmar': True, 'pasos': [{'confirmar': True}]})
        conf = [b for u, b in [e for e in r['envios'] if e[0] == '/api/mobile/confirmar']]
        assert conf[0]['total_contado'] == 0 and conf[0]['cero_confirmado'] is True

    def test_no_lo_encontre_no_confirma_un_cero(self, tmp_path):
        r = _correr_hud(tmp_path, {'pasos': [{'noEncontrado': True}]})
        urls = [e[0] for e in r['envios']]
        assert urls == ['/api/mobile/reportar-problema']
        assert r['envios'][0][1]['motivo'] == 'NO_ENCONTRADO'
        r = _correr_hud(tmp_path, {'pasos': [{'scan': UNIDAD}, {'noEncontrado': True}]})
        assert '/api/mobile/reportar-problema' not in [e[0] for e in r['envios']], (
            'con unidades contadas «no lo encontré» se contradice')

    def test_sin_ubicacion_fisica_manda_el_producto(self, tmp_path):
        r = _correr_hud(tmp_path, {})
        assert 'Búsquelo en toda la bodega' in r['html']
        assert 'SIESA-GENERAL' not in r['html']
        assert 'Cuaderno &lt;b&gt;x&lt;/b&gt;' in r['html'] and '<b>x</b>' not in r['html']
        assert 'CJA de 12 und' in r['html'] and UNIDAD in r['html']
        r = _correr_hud(tmp_path, {'tarea': {'id': 7, 'tipo': 'CONTEO', 'producto_nombre': 'x',
                                             'ubicacion': 'A-01-02', 'ubicacion_fisica': True}})
        assert 'A-01-02' in r['html'] and 'Búsquelo en toda la bodega' not in r['html']
