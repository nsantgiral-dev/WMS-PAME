"""
Cuatro defectos del flujo de conteo cíclico, encontrados al diseñar sus
estadísticas (2026-09-23): medir la exactitud de un doble ciego que no se
cumplía era medir un proceso que no existe.

## Los casos

1. **Doble ajuste por aprobar un hijo.** `PUT /api/conteo/<id>/ajustar` aceptaba
   un CC2/CC3 en DESCUADRE. Su observación ya había salido como ajuste desde la
   raíz, y la idempotencia es por sesión: el mismo faltante, dos veces en Siesa.
2. **El despachador de NB1 repartía sin regla.** `filter_by(estado='PENDIENTE',
   operario_id=None)`: un CC3 (de supervisión) le caía a un operario, un CC2
   liberado le volvía a quien hizo el CC1, y un conteo de otra bodega también.
3. **«Omitir» dejaba vivo el CC3.** Con la raíz en TERCER_CONTEO el CC2 ya está
   en DESCUADRE, y el CC3 solo se cancelaba si su padre seguía vivo.
4. **Dos cadenas del mismo hueco.** El generador ABC miraba solo
   PENDIENTE/EN_PROCESO/SEGUNDO_CONTEO: con una raíz en DESCUADRE esperando al
   supervisor abría otra, que ajustaba sola la misma diferencia.

## Las clases

- *«Una puerta reparte conteos sin dueño con su propio filtro»*: toda puerta usa
  `ConteoService.filtros_pool_sin_dueno`. Trinquete por AST, con inventario
  declarado de las consultas que solo LEEN el pool:
  `TestNingunaPuertaArmaSuPropioPool`.
- *«Un ajuste sale de algo que no es la observación vigente del hueco»*: un hijo
  (caso 1) o una cadena que otra dejó vieja (caso 4). Las dos guardas viven en
  el servicio (`_exigir_raiz_para_ajustar`, `motivo_bloqueo_ajuste` caso 6),
  que es el único sitio por donde pasa todo ajuste.
"""
import ast
import pathlib
from datetime import datetime, timedelta

import pytest
from flask_jwt_extended import create_access_token
from werkzeug.security import generate_password_hash

from tests.test_conteo_teorico_pos import SKU, _jobs, siesa, tienda  # noqa: F401 (fixtures)

RAIZ = pathlib.Path(__file__).resolve().parents[1]


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _sesion(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


def _cc1(tienda, operario, fisico):
    from app.models.conteo import SesionConteo
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
    assert creado['codigos'], creado
    cc1 = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one()
    _svc().obtener_tarea_operario(cc1.id, operario.id)
    return cc1.id, _svc().registrar_conteo(cc1.id, operario.id, fisico)


def _contar(sid, operario, fisico):
    _svc().obtener_tarea_operario(sid, operario.id)
    return _svc().registrar_conteo(sid, operario.id, fisico)


def _cadena_con_cc3(tienda, siesa):
    """CC1 (a) = 7 y CC2 (b) = 8 contra un teórico de 10: discordantes → CC3."""
    siesa.poner(existencia=10, pos=0)
    cc1, r1 = _cc1(tienda, tienda['a'], 7)
    assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
    cc2 = r1['segundo_conteo_id']
    r2 = _contar(cc2, tienda['b'], 8)
    assert r2['resultado'] == 'TERCER_CONTEO', r2
    return cc1, cc2, r2['tercer_conteo_id']


def _liberar_cc2_como_zombi(db, cc2_id, operario):
    """El CC2 lo abre su par y lo abandona: el barrido de zombis lo devuelve al
    pool SIN dueño. Es el camino real por el que un CC2 queda suelto."""
    _svc().obtener_tarea_operario(cc2_id, operario.id)
    s = _sesion(db, cc2_id)
    s.fecha_inicio = datetime.utcnow() - timedelta(hours=5)
    db.session.commit()
    assert _svc().liberar_tareas_zombi(timeout_horas=2) >= 1
    s = _sesion(db, cc2_id)
    assert (s.estado, s.operario_id) == ('PENDIENTE', None)


def _actor(db, almacen_id, email, rol='operario'):
    from app.models.usuario import Usuario
    u = Usuario(nombre=email, email=email, password_hash=generate_password_hash('x'),
                rol=rol, puede_picar=True, almacen_id=almacen_id, activo=True)
    db.session.add(u)
    db.session.commit()
    return u


def _tarea(operario):
    from app.services.mobile_service import MobileService
    return MobileService.get_tarea_actual(operario.id)


def _token(app, usuario):
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


# ─────────────────────────────────────────────────────────────────────────────
# 2 · El despachador reparte con la regla
# ─────────────────────────────────────────────────────────────────────────────

class TestElDespachadorRespetaLaCadena:

    def test_un_cc3_no_le_cae_a_un_operario(self, db, siesa, tienda):
        _, _, cc3 = _cadena_con_cc3(tienda, siesa)
        c = _actor(db, tienda['almacen'].id, 'pool-c@test.com')
        for operario in (tienda['a'], tienda['b'], c):
            t = _tarea(operario)
            assert not (t and t.get('id') == cc3), (
                f'el CC3 le cayó a {operario.email}: el conteo definitivo es de '
                'supervisión, por /api/conteo/definitivos')
        assert _sesion(db, cc3).operario_id is None

    def test_un_cc2_liberado_no_vuelve_a_quien_hizo_el_cc1(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        _, r1 = _cc1(tienda, tienda['a'], 7)
        cc2 = r1['segundo_conteo_id']
        _liberar_cc2_como_zombi(db, cc2, tienda['b'])

        t = _tarea(tienda['a'])
        assert not (t and t.get('id') == cc2), (
            'el CC2 le volvió a quien hizo el CC1: doble ciego roto, y si repite '
            'su propio error CC1 == CC2 ajusta solo')
        t = _tarea(tienda['b'])
        assert t and t.get('id') == cc2, 'un CC2 suelto tiene que poder tomarlo otro'

    def test_no_reparte_conteos_de_otra_bodega(self, db, siesa, tienda):
        from app.models.almacen import Almacen
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        otra = Almacen(codigo='ALM-OTRA', nombre='Otra', bodega_siesa_id='NC1',
                       centro_op_siesa='002', activo=True)
        db.session.add(otra)
        db.session.flush()
        ub = Ubicacion(codigo='OTRA-UB', almacen_id=otra.id, tipo_zona='GENERAL',
                       stock_minimo=0, stock_maximo=999, secuencia_ruteo=1, activo=True)
        db.session.add(ub)
        db.session.flush()
        db.session.add(UbicacionProducto(ubicacion_id=ub.id, producto_id=tienda['producto'].id,
                                         cantidad=5, reservado=0, bloqueado=0))
        db.session.commit()
        from app.models.conteo import SesionConteo
        creado = _svc().crear_conteo_manual(otra.id, SKU)
        ajeno = SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id

        t = _tarea(tienda['a'])
        # Por id: el dict del despachador trae el código en `referencia`, y un
        # assert sobre una clave que no existe no puede fallar nunca.
        assert not (t and t.get('id') == ajeno), 'un operario recibió un conteo de otra bodega'
        assert _sesion(db, ajeno).operario_id is None


class TestLaPoliticaSeAplicaAlAbrir:
    """Un dueño mal puesto —dato viejo, o una reasignación a mano— no vuelve
    válido lo que la regla prohíbe: se revisa también al abrir y al registrar."""

    def test_un_cc2_asignado_a_quien_hizo_el_cc1_no_se_abre(self, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        _, r1 = _cc1(tienda, tienda['a'], 7)
        cc2 = _sesion(db, r1['segundo_conteo_id'])
        cc2.operario_id = tienda['a'].id
        db.session.commit()
        with pytest.raises(ValueError, match='Doble ciego'):
            _svc().obtener_tarea_operario(cc2.id, tienda['a'].id)
        with pytest.raises(ValueError, match='Doble ciego'):
            _svc().registrar_conteo(cc2.id, tienda['a'].id, 7)


class TestLasPuertasManuales:

    def test_asignar_lote_no_reparte_cc3_ni_el_cc2_propio(self, app, db, client, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        _, r1 = _cc1(tienda, tienda['a'], 7)
        cc2 = r1['segundo_conteo_id']
        _liberar_cc2_como_zombi(db, cc2, tienda['b'])
        h = _token(app, tienda['supervisor'])

        r = client.post('/api/conteo/asignar-lote', headers=h,
                        json={'operario_id': tienda['a'].id,
                              'almacen_id': tienda['almacen'].id})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['asignadas'] == 0, 'asignar-lote le dio a A el CC2 de su propio CC1'
        assert _sesion(db, cc2).operario_id is None

        r = client.post('/api/conteo/asignar-lote', headers=h,
                        json={'operario_id': tienda['b'].id,
                              'almacen_id': tienda['almacen'].id})
        assert r.get_json()['asignadas'] == 1

    def test_asignar_lote_no_reparte_el_cc3(self, app, db, client, siesa, tienda):
        _, _, cc3 = _cadena_con_cc3(tienda, siesa)
        c = _actor(db, tienda['almacen'].id, 'lote-c@test.com')
        r = client.post('/api/conteo/asignar-lote', headers=_token(app, tienda['supervisor']),
                        json={'operario_id': c.id, 'almacen_id': tienda['almacen'].id})
        assert r.status_code == 200, r.get_json()
        assert _sesion(db, cc3).operario_id is None

    def test_editar_no_reasigna_el_cc2_a_quien_hizo_el_cc1(self, app, db, client, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        _, r1 = _cc1(tienda, tienda['a'], 7)
        cc2 = r1['segundo_conteo_id']
        admin = _actor(db, tienda['almacen'].id, 'edit-admin@test.com', rol='admin')
        r = client.put(f'/api/conteo/{cc2}/editar', headers=_token(app, admin),
                       json={'operario_id': tienda['a'].id, 'motivo_edicion': 'reasignar a mano'})
        assert r.status_code == 400, r.get_json()
        assert 'Doble ciego' in r.get_json()['error']


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Un ajuste sale solo de la raíz
# ─────────────────────────────────────────────────────────────────────────────

class TestUnHijoNoSeAjusta:

    def test_aprobar_el_cc2_no_encola_un_segundo_ajuste(self, app, db, client, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        cc1, r1 = _cc1(tienda, tienda['a'], 7)
        cc2 = r1['segundo_conteo_id']
        r2 = _contar(cc2, tienda['b'], 7)
        assert r2['auto_encolado'] is True, r2
        assert len(_jobs(cc1)) == 1

        with pytest.raises(ValueError, match='conteo de verificación'):
            _svc().confirmar_ajuste(cc2, tienda['supervisor'].id)
        r = client.put(f'/api/conteo/{cc2}/ajustar', headers=_token(app, tienda['supervisor']))
        assert r.status_code == 400, r.get_json()
        assert _jobs(cc2) == [], 'el hijo encoló un AJUSTE_CONTEO: doble ajuste en Siesa'

    def test_aprobar_el_cc3_tampoco(self, db, siesa, tienda):
        cc1, _, cc3 = _cadena_con_cc3(tienda, siesa)
        r3 = _contar(cc3, tienda['supervisor'], 7)
        assert r3['resultado'] == 'DESCUADRE', r3
        with pytest.raises(ValueError, match='conteo de verificación'):
            _svc().confirmar_ajuste(cc3, tienda['supervisor'].id)
        assert _jobs(cc3) == []
        # La raíz sí se aprueba: el arreglo no le quita la salida legítima.
        _svc().confirmar_ajuste(cc1, tienda['supervisor'].id)
        assert len(_jobs(cc1)) == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3 · Omitir cancela todo lo vivo de la cadena
# ─────────────────────────────────────────────────────────────────────────────

class TestOmitirConElCC3Vivo:

    def test_omitir_en_tercer_conteo_cancela_el_cc3(self, app, db, client, siesa, tienda):
        cc1, _, cc3 = _cadena_con_cc3(tienda, siesa)
        r = client.post(f'/api/conteo/{cc1}/omitir-segundo',
                        headers=_token(app, tienda['supervisor']))
        assert r.status_code == 200, r.get_json()
        assert _sesion(db, cc1).estado == 'DESCUADRE'
        assert _sesion(db, cc3).estado == 'CANCELADO', (
            'el CC3 quedó vivo y huérfano en la cola de Conteo Definitivo')


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Una cadena viva por hueco, y el ajuste de una cadena vieja no sale
# ─────────────────────────────────────────────────────────────────────────────

def _clasificar_a(db, tienda):
    from app.models.producto_clasificacion_abc import ProductoClasificacionABC
    db.session.add(ProductoClasificacionABC(producto_id=tienda['producto'].id,
                                            almacen_id=tienda['almacen'].id,
                                            clasificacion='A'))
    db.session.commit()


def _sesiones_del_hueco(tienda):
    from app.models.conteo import SesionConteo
    return SesionConteo.query.filter_by(producto_id=tienda['producto'].id,
                                        ubicacion_id=tienda['ubicacion'].id,
                                        es_segundo_conteo=False).count()


class TestElGeneradorNoAbreOtraCadena:

    @pytest.mark.parametrize('ssc,estado_raiz', [
        (3, 'DESCUADRE'),   # CC1 == CC2 pero el ajuste bloqueado: espera decisión
        (0, 'AJUSTANDO'),   # CC1 == CC2 y el ajuste en camino a Siesa
    ])
    def test_con_la_raiz_viva_no_genera(self, db, siesa, tienda, ssc, estado_raiz):
        from app.services.abc_service import ABCService
        siesa.poner(existencia=10, pos=0, salida_sin_conf=ssc)
        cc1, r1 = _cc1(tienda, tienda['a'], 7)
        _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert _sesion(db, cc1).estado == estado_raiz
        _clasificar_a(db, tienda)

        antes = _sesiones_del_hueco(tienda)
        ABCService.generar_tareas_conteo_diario(tienda['almacen'].id, 'A', forzar_todo=True)
        assert _sesiones_del_hueco(tienda) == antes, (
            f'el generador abrió otra cadena sobre una raíz en {estado_raiz}')

    def test_el_conteo_manual_si_puede_recontar_un_descuadre(self, db, siesa, tienda):
        """Es lo que el bloqueo del ajuste le pide a un humano: recontar."""
        siesa.poner(existencia=10, pos=0, salida_sin_conf=3)
        cc1, r1 = _cc1(tienda, tienda['a'], 7)
        _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert _sesion(db, cc1).estado == 'DESCUADRE'
        creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        assert creado['codigos'], creado


class TestUnaCadenaViejaNoSeAjusta:

    def _descuadre_aprobable(self, db, siesa, tienda):
        """CC1 7, CC2 8, CC3 7 (supervisor) contra teórico 10: raíz en
        DESCUADRE, aprobable, esperando al supervisor."""
        cc1, _, cc3 = _cadena_con_cc3(tienda, siesa)
        _contar(cc3, tienda['supervisor'], 7)
        raiz = _sesion(db, cc1)
        assert raiz.estado == 'DESCUADRE'
        assert _svc().motivo_bloqueo_ajuste(raiz) is None
        return cc1

    def test_un_recuento_posterior_deja_vieja_la_cadena_anterior(self, db, siesa, tienda):
        """El doble ajuste completo: la cadena vieja espera al supervisor, se
        recuenta, la nueva se ajusta sola, y el supervisor aprueba la vieja."""
        vieja = self._descuadre_aprobable(db, siesa, tienda)
        nueva, r1 = _cc1(tienda, tienda['a'], 7)
        r2 = _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        assert r2['auto_encolado'] is True, r2

        with pytest.raises(ValueError, match='quedó viejo'):
            _svc().confirmar_ajuste(vieja, tienda['supervisor'].id)
        assert _jobs(vieja) == [], 'la misma diferencia salió dos veces a Siesa'
        assert len(_jobs(nueva)) == 1

    def test_un_ajuste_aceptado_despues_de_la_foto_la_deja_vieja(self, db, siesa, tienda):
        vieja = self._descuadre_aprobable(db, siesa, tienda)
        nueva, r1 = _cc1(tienda, tienda['a'], 7)
        _contar(r1['segundo_conteo_id'], tienda['b'], 7)
        foto_vieja = _sesion(db, vieja).foto_siesa_at
        # Toda la cadena nueva contó ANTES que la vieja (si no, la deja vieja
        # por ser posterior, que es el otro caso): lo único que la delata es
        # que su ajuste se aceptó después de la foto de la vieja.
        from app.models.conteo import SesionConteo
        for sid in _svc()._ids_de_la_cadena(_sesion(db, nueva)):
            db.session.get(SesionConteo, sid).foto_siesa_at = foto_vieja - timedelta(minutes=5)
        otra = db.session.get(SesionConteo, nueva)
        otra.estado = 'AJUSTADO'
        otra.fecha_cierre = foto_vieja + timedelta(minutes=5)
        db.session.commit()
        assert 'ya se ajustó' in (_svc().observacion_que_la_vuelve_vieja(_sesion(db, vieja)) or '')

        otra = db.session.get(SesionConteo, nueva)
        otra.fecha_cierre = foto_vieja - timedelta(minutes=1)
        db.session.commit()
        assert _svc().observacion_que_la_vuelve_vieja(_sesion(db, vieja)) is None, (
            'un ajuste aceptado ANTES de la foto ya está en ella: no la deja vieja')

    def test_su_propia_cadena_no_la_deja_vieja(self, db, siesa, tienda):
        vieja = self._descuadre_aprobable(db, siesa, tienda)
        assert _svc().observacion_que_la_vuelve_vieja(_sesion(db, vieja)) is None


# ─────────────────────────────────────────────────────────────────────────────
# Trinquete — ninguna puerta arma su propio pool de conteos sin dueño
# ─────────────────────────────────────────────────────────────────────────────

#: Funciones que consultan conteos sin dueño SIN repartirlos, con su motivo.
#: Solo encoge. Una puerta nueva que reparte no entra acá: usa la política.
LEEN_EL_POOL_SIN_REPARTIR = {
    ('app/routes/conteo.py', 'stats_conteo'):
        'Solo cuenta cuántos conteos PENDIENTE siguen sin dueño para el tablero; '
        'no asigna ninguno, así que no hay regla de reparto que aplicar.',
}

_POLITICA = 'filtros_pool_sin_dueno'


def _es_sesion_conteo(nodo) -> bool:
    return any(isinstance(n, ast.Name) and n.id == 'SesionConteo' for n in ast.walk(nodo))


def _es_operario_id_de_sesion(nodo) -> bool:
    return (isinstance(nodo, ast.Attribute) and nodo.attr == 'operario_id'
            and isinstance(nodo.value, ast.Name) and nodo.value.id == 'SesionConteo')


def _es_none(nodo) -> bool:
    return isinstance(nodo, ast.Constant) and nodo.value is None


def _consulta_pool_sin_dueno(fn) -> bool:
    """¿La función filtra SesionConteo por «sin dueño»? Las tres escrituras de
    la misma condición: `filter_by(operario_id=None)` sobre una consulta de
    SesionConteo, `SesionConteo.operario_id.is_(None)` y `== None`."""
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'filter_by' and _es_sesion_conteo(n.func.value)
                and any(k.arg == 'operario_id' and _es_none(k.value) for k in n.keywords)):
            return True
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'is_' and _es_operario_id_de_sesion(n.func.value)
                and n.args and _es_none(n.args[0])):
            return True
        if (isinstance(n, ast.Compare) and _es_operario_id_de_sesion(n.left)
                and any(isinstance(op, (ast.Eq, ast.Is)) for op in n.ops)
                and any(_es_none(c) for c in n.comparators)):
            return True
    return False


def _usa_la_politica(fn) -> bool:
    return any(isinstance(n, ast.Attribute) and n.attr == _POLITICA for n in ast.walk(fn))


def _funciones(arbol):
    return [n for n in ast.walk(arbol) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _puertas_sin_politica(fuente: str):
    return [fn.name for fn in _funciones(ast.parse(fuente))
            if fn.name != _POLITICA and _consulta_pool_sin_dueno(fn)
            and not _usa_la_politica(fn)]


def _barrer_app():
    sin_politica, usos = [], 0
    for ruta in sorted((RAIZ / 'app').rglob('*.py')):
        rel = ruta.relative_to(RAIZ).as_posix()
        arbol = ast.parse(ruta.read_text(encoding='utf-8'))
        for fn in _funciones(arbol):
            if fn.name == _POLITICA:
                continue
            usos += sum(1 for n in ast.walk(fn) if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute) and n.func.attr == _POLITICA)
        sin_politica += [(rel, nombre) for nombre in _puertas_sin_politica(ast.unparse(arbol))]
    return sin_politica, usos


class TestNingunaPuertaArmaSuPropioPool:

    def test_toda_consulta_al_pool_usa_la_politica_o_esta_declarada(self):
        sin_politica, _ = _barrer_app()
        nuevas = sorted(set(sin_politica) - set(LEEN_EL_POOL_SIN_REPARTIR))
        assert not nuevas, (
            'Consultan conteos sin dueño sin `ConteoService.filtros_pool_sin_dueno`: '
            f'{nuevas}. Si reparten, tienen que usarla (CC3 solo supervisión, doble '
            'ciego, misma bodega). Si solo leen, declararlas con su motivo.')

    def test_el_inventario_solo_encoge(self):
        sin_politica, _ = _barrer_app()
        sobran = sorted(set(LEEN_EL_POOL_SIN_REPARTIR) - set(sin_politica))
        assert not sobran, f'Ya no consultan el pool: sacarlas del inventario: {sobran}'

    def test_cada_entrada_dice_por_que(self):
        for sitio, motivo in LEEN_EL_POOL_SIN_REPARTIR.items():
            assert len(motivo.strip()) > 40, f'{sitio}: el motivo no explica nada'

    def test_piso_la_politica_se_usa_donde_se_reparte(self):
        """Despachador NB1 + intercalado, despachador de tienda, asignar-lote.
        Si el barrido deja de ver los usos, esto se pone rojo antes que
        reportar cero puertas sin política."""
        _, usos = _barrer_app()
        assert usos >= 4, f'solo {usos} usos de la política: el barrido está ciego'


class TestElDetectorDelPoolMuerde:

    @pytest.mark.parametrize('condicion', [
        "SesionConteo.query.filter_by(estado='PENDIENTE', operario_id=None).first()",
        "SesionConteo.query.filter(SesionConteo.operario_id.is_(None)).first()",
        "SesionConteo.query.filter(SesionConteo.operario_id == None).first()",
    ])
    def test_marca_las_tres_escrituras(self, condicion):
        fuente = f"def repartir(op):\n    c = {condicion}\n    c.operario_id = op\n"
        assert _puertas_sin_politica(fuente) == ['repartir']

    def test_no_marca_la_que_usa_la_politica(self):
        fuente = ("def repartir(op, alm):\n"
                  "    c = SesionConteo.query.filter(\n"
                  "        *ConteoService.filtros_pool_sin_dueno(op, alm)).first()\n")
        assert _puertas_sin_politica(fuente) == []

    def test_no_marca_el_pool_de_otra_tabla(self):
        fuente = ("def picking(op):\n"
                  "    return TareaPicking.query.filter(TareaPicking.operario_id.is_(None)).first()\n")
        assert _puertas_sin_politica(fuente) == []
