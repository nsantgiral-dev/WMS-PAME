"""
Un picking de un pedido cuya caja ya no recibe líneas (validación 2026-09-26, P1-2 / H7).

**La clase:** *nace (o vuelve al pool) un picking de pedido cuya caja ya no
puede llevar lo que se recoja.* `reabrir_picking` con la caja DESPACHADA (RM +
FE) o VERIFICADA (cerrada, retenida por cartera) creaba una tarea por el
faltante: el picker la recogía, el hueco se descontaba, y ninguna caja podía
llevarla (una caja por pedido; `exigir_pedido_sin_documento`). Unidades fuera
del hueco sin destino — la clase de `test_inventario_no_se_resta_sin_destino`.

**Decisión:** si la caja no recibe, reabrir **no devuelve nada al pool**. Lo
recogido queda en esa caja (la original COMPLETADO por eso; sin nada recogido,
CANCELADO) y el faltante es un pedido nuevo o un backorder, dicho en la
respuesta y en la bitácora. Nada se resta.

Una política: `PickingService.motivo_caja_no_recibe`. Trinquete AST: toda
función que crea un `TareaPicking` o lo devuelve a PENDIENTE la pregunta o
está declarada con su porqué.
"""
import ast
import pathlib
from datetime import datetime

import pytest

from tests.test_inventario_no_se_resta_sin_destino import _en_hueco, _escenario

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _caja(db, almacen, t, ref, *, estado, documento=False):
    from app.services.packing_service import PackingService
    caja = PackingService.crear_desde_picking(
        tareas_picking_ids=[t.id], numero_pedido_siesa=ref, almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=ref[2:])
    caja.estado = estado
    if documento:
        caja.siesa_triggered = True
        caja.rm_tipo, caja.rm_consec = 'RM', 55
        caja.fe_confirmada_at = datetime.utcnow()
    db.session.commit()
    return caja


def _pendientes(ref):
    from app.models.picking import TareaPicking
    return TareaPicking.query.filter_by(referencia_documento=ref, estado='PENDIENTE').count()


# ─────────────────────────────────────────────────────────────────────────────
# H7 del validador fiscal (val-fiscal 2401e90d), copiado a mano
# ─────────────────────────────────────────────────────────────────────────────

class TestReabrirConLaCajaDespachada:

    def test_no_nace_un_faltante_para_una_caja_que_ya_salio(self, db, almacen):
        from app.models.picking import TareaPicking
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7101')
        caja = PackingService.crear_desde_picking(
            tareas_picking_ids=[t.id], numero_pedido_siesa='PD7101',
            almacen_id=almacen.id, tipo_docto_pedido_siesa='PD',
            consec_docto_pedido_siesa='7101')
        caja.estado, caja.siesa_triggered = 'DESPACHADO', True
        caja.rm_tipo, caja.rm_consec = 'RM', 55
        caja.fe_confirmada_at = datetime.utcnow()
        db.session.commit()
        try:
            PickingService.reabrir_picking(t.id, sup.id, motivo='apareció el resto')
        except ValueError:
            return  # arreglado: se niega
        vivas = TareaPicking.query.filter_by(referencia_documento='PD7101',
                                             estado='PENDIENTE').count()
        assert vivas == 0, 'nació un picking por el faltante de una caja que ya salió'


class TestLaCajaQueNoRecibe:

    def test_caja_cerrada_retenida_tampoco_recibe(self, db, almacen):
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7201')
        _caja(db, almacen, t, 'PD7201', estado='VERIFICADO')
        antes = _en_hueco(db, ub, p)
        cant, bloq = antes.cantidad, antes.bloqueado
        r = PickingService.reabrir_picking(t.id, sup.id, motivo='llegó reposición')
        assert r.id == t.id and r.estado == 'COMPLETADO' and r.cantidad_solicitada == 7
        assert r.faltante_sin_caja == 3 and 'cerrada' in r.aviso_reapertura
        assert _pendientes('PD7201') == 0
        h = _en_hueco(db, ub, p)
        assert h.cantidad == cant, 'se restó sin caja'
        assert h.bloqueado == bloq - 3, 'el faltante congelado no se liberó'

    def test_sin_nada_recogido_y_caja_despachada_se_cancela(self, db, almacen):
        from app.models.bitacora import BitacoraAccion
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7202', encontrada=0)
        from app.services.packing_service import PackingService
        caja = PackingService.crear_manual(
            numero_pedido_siesa='PD7202', almacen_id=almacen.id,
            items=[{'producto_id': p.id, 'cantidad': 10}],
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='7202')
        caja.estado, caja.siesa_triggered = 'DESPACHADO', True
        caja.rm_tipo, caja.rm_consec = 'RM', 56
        caja.fe_confirmada_at = datetime.utcnow()
        db.session.commit()
        r = PickingService.reabrir_picking(t.id, sup.id, motivo='no estaba')
        assert r.estado == 'CANCELADO' and r.faltante_sin_caja == 10
        assert _pendientes('PD7202') == 0 and _en_hueco(db, ub, p).cantidad == 20
        b = BitacoraAccion.query.filter_by(entidad_id=t.id, accion='CANCELAR').one()
        assert b.despues['faltante_sin_caja'] == 10 and b.motivo == 'no estaba'

    def test_la_caja_abierta_si_recibe_y_nace_el_faltante(self, db, almacen):
        """Lo sano no cambia: con la caja PENDIENTE el faltante sale en otra tarea."""
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7203')
        _caja(db, almacen, t, 'PD7203', estado='EN_PROCESO')
        nueva = PickingService.reabrir_picking(t.id, sup.id, motivo='apareció')
        assert nueva.id != t.id and nueva.estado == 'PENDIENTE'
        assert getattr(nueva, 'aviso_reapertura', None) is None

    def test_caja_abierta_con_documento_no_recibe(self, db, almacen):
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7204')
        caja = _caja(db, almacen, t, 'PD7204', estado='EN_PROCESO')
        caja.rm_enviada_at = datetime.utcnow()          # 142945 enviado sin confirmar
        db.session.commit()
        r = PickingService.reabrir_picking(t.id, sup.id, motivo='x')
        assert r.estado == 'COMPLETADO' and _pendientes('PD7204') == 0
        assert 'Siesa' in r.aviso_reapertura

    def test_caja_cancelada_con_documento_no_recibe(self, db, almacen):
        from app.services.picking_service import PickingService
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7205')
        _caja(db, almacen, t, 'PD7205', estado='CANCELADO', documento=True)
        r = PickingService.reabrir_picking(t.id, sup.id, motivo='x')
        assert r.estado == 'COMPLETADO' and _pendientes('PD7205') == 0
        assert 'cancelada' in r.aviso_reapertura

    def test_la_ruta_lo_dice(self, client, db, almacen):
        from flask_jwt_extended import create_access_token
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7206')
        _caja(db, almacen, t, 'PD7206', estado='DESPACHADO', documento=True)
        h = {'Authorization': f'Bearer {create_access_token(identity=str(sup.id))}'}
        r = client.put(f'/api/picking/{t.id}/reabrir', json={'motivo': 'x'}, headers=h)
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['faltante_sin_caja'] == 3
        assert 'No se creó tarea por el faltante' in r.get_json()['mensaje']

    def test_crear_picking_manual_para_una_caja_despachada_es_409(self, client, db, almacen):
        from flask_jwt_extended import create_access_token
        t, p, ub, op, sup = _escenario(db, almacen, ref='PD7207')
        _caja(db, almacen, t, 'PD7207', estado='DESPACHADO', documento=True)
        h = {'Authorization': f'Bearer {create_access_token(identity=str(sup.id))}'}
        r = client.post('/api/picking/crear', json={
            'producto_id': p.id, 'cantidad': 3, 'almacen_id': almacen.id,
            'referencia_documento': 'PD7207'}, headers=h)
        assert r.status_code == 409, r.get_json()
        assert _pendientes('PD7207') == 0

    def test_devolver_a_la_cola_un_picking_a_medio_recoger_tampoco(self, db, almacen):
        """«Devolver a la cola» (asignación n1) es otra puerta al pool: con la
        caja del pedido despachada se niega y la tarea sigue donde estaba."""
        from app.models.picking import TareaPicking
        from app.models.usuario import Usuario
        from app.services.packing_service import PackingService
        from app.services.picking_service import PickingService
        from tests.flujo import conductor_de_flujo as cf
        op = Usuario(nombre='Op cola', email='op-cola-7208@t.co', rol='operario',
                     almacen_id=almacen.id, activo=True)
        sup = Usuario(nombre='Sup cola', email='sup-cola-7208@t.co', rol='supervisor',
                      almacen_id=almacen.id, activo=True)
        op.set_password('x')
        sup.set_password('x')
        db.session.add_all([op, sup])
        db.session.commit()
        p = cf.sembrar_catalogo(db, almacen, n=1, con_stock=20)[0][0]
        t = PickingService.crear_tareas(producto_id=p.id, cantidad=5, almacen_id=almacen.id,
                                        referencia_documento='PD7208', tipo_documento='PEDIDO')[0]
        PickingService.iniciar_picking(t.id, op.id)
        caja = PackingService.crear_manual(
            numero_pedido_siesa='PD7208', almacen_id=almacen.id,
            items=[{'producto_id': p.id, 'cantidad': 5}],
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='7208')
        caja.estado, caja.siesa_triggered = 'DESPACHADO', True
        caja.rm_tipo, caja.rm_consec = 'RM', 57
        caja.fe_confirmada_at = datetime.utcnow()
        op.ultima_senal_at = None                      # el dueño no está
        db.session.commit()
        with pytest.raises(ValueError, match='despachada'):
            PickingService.devolver_en_curso_a_la_cola(t.id, sup.id, 'se fue a medio recoger')
        db.session.rollback()
        db.session.expire_all()
        t = db.session.get(TareaPicking, t.id)
        assert (t.estado, t.operario_id) == ('EN_PROCESO', op.id)
        assert _pendientes('PD7208') == 0

    def test_pwa_pide_el_motivo_y_muestra_el_mensaje(self):
        src = (RAIZ / 'app/static/pwa/app.js').read_text(encoding='utf-8')
        cuerpo = src[src.index('async function reabrirTareaPicking'):]
        cuerpo = cuerpo[:cuerpo.index('\n}\n')]
        assert '_modalTexto' in cuerpo and '{ motivo }' in cuerpo and 'r.mensaje' in cuerpo


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete
# ═════════════════════════════════════════════════════════════════════════════

POLITICA = 'motivo_caja_no_recibe'

#: (archivo, función) → por qué no pregunta. Solo encoge.
DECLARADOS = {
    ('app/services/picking_service.py', 'crear_tareas'):
        'el creador base (FEFO): la pregunta la hacen sus llamadores',
    ('app/services/picking_service.py', 'crear_tareas_con_compromiso'):
        'solo la llama iniciar-despacho, que rechaza todo pedido con caja viva o '
        'con documento (más estricto que la política)',
    ('app/services/picking_service.py', '_crear_bloqueada_sin_stock'):
        'nace BLOQUEADA, no entra al pool; su salida es reabrir, que pregunta',
    ('app/routes/siesa.py', '_iniciar_despacho_tras_cartera'):
        'solo la llama iniciar_despacho, que rechaza todo pedido con caja viva o '
        'con documento',
    ('app/services/traslado_service.py', '_crear_picking_tasks'):
        'traslado: su documento es el STS, otro circuito (la política exime TRASLADO)',
    ('app/services/traslado_service.py', '_crear_picking_tienda'):
        'traslado desde tienda: su documento es el STS, otro circuito',
}


def _crea_o_devuelve(nodo) -> bool:
    if isinstance(nodo, ast.Call):
        f = nodo.func
        if isinstance(f, ast.Name) and f.id == 'TareaPicking':
            return True
        if isinstance(f, ast.Attribute) and f.attr in ('crear_tareas', 'crear_tareas_con_compromiso'):
            return True
    if isinstance(nodo, ast.Assign):
        v = nodo.value
        return (isinstance(v, ast.Attribute) and v.attr == 'PENDIENTE'
                and isinstance(v.value, ast.Name) and v.value.id == 'EstadoPicking'
                and any(isinstance(t, ast.Attribute) and t.attr == 'estado' for t in nodo.targets))
    return False


def sitios(src: str, archivo: str) -> dict:
    """(archivo, función) → ¿pregunta la política en la MISMA función?"""
    out = {}

    def recorrer(func):
        crea = pregunta = False
        pila = list(ast.iter_child_nodes(func))
        while pila:
            n = pila.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue          # una función anidada es otra función
            if _crea_o_devuelve(n):
                crea = True
            if isinstance(n, ast.Call):
                f = n.func
                nombre = f.attr if isinstance(f, ast.Attribute) else getattr(f, 'id', None)
                if nombre == POLITICA:
                    pregunta = True
            pila.extend(ast.iter_child_nodes(n))
        if crea:
            out[(archivo, func.name)] = pregunta

    for n in ast.walk(ast.parse(src)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            recorrer(n)
    return out


def _todos():
    out = {}
    for p in sorted((RAIZ / 'app').rglob('*.py')):
        out.update(sitios(p.read_text(encoding='utf-8'), p.relative_to(RAIZ).as_posix()))
    return out


class TestTodoPickingDePedidoPreguntaPorLaCaja:

    def test_ninguno_sin_preguntar_ni_declarar(self):
        malos = {k for k, pregunta in _todos().items() if not pregunta} - set(DECLARADOS)
        assert not malos, (f'{sorted(malos)}: crea (o devuelve al pool) un picking sin '
                           f'preguntar PickingService.{POLITICA}. Pregúntela o declare por qué.')

    def test_el_inventario_solo_encoge(self):
        vivos = {k for k, pregunta in _todos().items() if not pregunta}
        assert not (set(DECLARADOS) - vivos), sorted(set(DECLARADOS) - vivos)

    def test_cada_declarado_dice_por_que(self):
        assert all(len(m) > 30 for m in DECLARADOS.values())

    def test_piso(self):
        t = _todos()
        assert len(t) >= 8
        assert t[('app/services/picking_service.py', 'reabrir_picking')] is True
        assert t[('app/routes/picking.py', 'crear_tarea')] is True

    def test_ve_las_tres_formas_y_no_lo_sano(self):
        src = ('def a():\n    TareaPicking(x=1)\n'
               'def b(s):\n    s.crear_tareas(1)\n'
               'def c(t):\n    t.estado = EstadoPicking.PENDIENTE\n'
               'def d(t):\n    PickingService.motivo_caja_no_recibe(1)\n    TareaPicking()\n'
               'def e():\n    """TareaPicking(x)"""\n    # s.crear_tareas()\n    return 1\n'
               'def f():\n    def g():\n        motivo_caja_no_recibe(1)\n    TareaPicking()\n')
        assert sitios(src, 'x.py') == {('x.py', 'a'): False, ('x.py', 'b'): False,
                                       ('x.py', 'c'): False, ('x.py', 'd'): True,
                                       ('x.py', 'f'): False}
