"""
Una bandera puesta sin desenlace es «no sé», no «ya enviado» (validación
2026-09-26, P1-4).

**La clase:** *el pre-flag leído como prueba de envío.* ENTRADA_OC,
AJUSTE_CONTEO y el traslado a averías por `TareaDevolucion` ponen la bandera
ANTES del POST (Regla 6). Si el proceso muere entre la marca y el POST (un
deploy, un job PROCESANDO reseteado a los 10 min), la guarda leía la bandera
sola como «ya enviado» y cerraba el job COMPLETADO sin que el documento
hubiera salido. El traslado a averías por movimiento ya decía «no sé»
(ENVIANDO sin desenlace).

Ahora, **una política** para todos: `siesa_job_service.estado_del_preflag`
(LIBRE · ENVIADO · SIN_DESENLACE). ENVIADO exige la respuesta guardada
(`siesa_response`; el movimiento, `siesa_sync = 'ENVIADO'`), que
`_ejecutar_con_preflag` escribe al salir bien y la resolución humana («sí está
en Siesa») escribe a mano. Sin desenlace: FALLIDO sin reintento y «¿Está en
Siesa?». Migración m050inv2: `tareas_devolucion.siesa_response`.

Trinquete AST: toda rama de `_ejecutar_job` de un tipo con pre-flag pregunta la
política y ninguna decide leyendo `.siesa_triggered`; todo `_ejecutar_con_preflag`
vive en un tipo de `TIPOS_CON_PREFLAG`.
"""
import ast
import json
import pathlib
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.siesa_job import EstadoSiesaJob, SiesaJob
from app.services import siesa_job_service as sjs
from app.services.connekta_gateway import ConnektaResultadoDesconocido

RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _recepcion(db, almacen, *, bandera, respuesta=None):
    from app.models.recepcion import RecepcionMercancia
    rec = RecepcionMercancia(codigo=f'REC-P-{uuid.uuid4().hex[:6]}', numero_oc_siesa='OC9',
                             almacen_id=almacen.id, estado='CONFIRMADA')
    rec.siesa_triggered = bandera
    rec.siesa_triggered_at = datetime.utcnow() - timedelta(minutes=15) if bandera else None
    rec.siesa_response = respuesta
    db.session.add(rec)
    db.session.commit()
    job = SiesaJob.encolar('ENTRADA_OC', {'recepcion_id': rec.id},
                           referencia_tipo='RecepcionMercancia', referencia_id=rec.id)
    db.session.commit()
    return rec, job


def _tarea_dev(db, almacen, producto, *, bandera, respuesta=None):
    from app.models.devolucion import TareaDevolucion
    t = TareaDevolucion(codigo=f'DEV-P-{uuid.uuid4().hex[:6]}', producto_id=producto.id,
                        almacen_id=almacen.id, cantidad_diferencia=2, es_averiado=True,
                        siesa_triggered=bandera, siesa_response=respuesta)
    db.session.add(t)
    db.session.commit()
    job = SiesaJob.encolar('TRASLADO_AVERIAS',
                           {'tarea_id': t.id, 'item_codigo': 'X', 'cantidad': 2},
                           referencia_tipo='TareaDevolucion', referencia_id=t.id)
    db.session.commit()
    return t, job


class TestLaPolitica:

    @pytest.mark.parametrize('obj,esperado', [
        (SimpleNamespace(siesa_triggered=False, siesa_response=None), sjs.PREFLAG_LIBRE),
        (SimpleNamespace(siesa_triggered=True, siesa_response=None), sjs.PREFLAG_SIN_DESENLACE),
        (SimpleNamespace(siesa_triggered=True, siesa_response='{"codigo":0}'), sjs.PREFLAG_ENVIADO),
        (SimpleNamespace(siesa_sync='ENVIANDO'), sjs.PREFLAG_SIN_DESENLACE),
        (SimpleNamespace(siesa_sync='ENVIADO'), sjs.PREFLAG_ENVIADO),
        (SimpleNamespace(siesa_sync='PENDIENTE'), sjs.PREFLAG_LIBRE),
        (None, sjs.PREFLAG_LIBRE),
    ])
    def test_tres_desenlaces(self, obj, esperado):
        assert sjs.estado_del_preflag(obj) == esperado

    def test_el_envio_que_sale_bien_deja_su_desenlace(self, db):
        obj = SimpleNamespace(siesa_triggered=False, siesa_triggered_at=None, siesa_response=None)
        sjs._ejecutar_con_preflag(obj, lambda: {'codigo': 0, 'mensaje': 'ok'})
        assert sjs.estado_del_preflag(obj) == sjs.PREFLAG_ENVIADO

    def test_en_ensayo_no_queda_desenlace(self, db):
        obj = SimpleNamespace(siesa_triggered=False, siesa_triggered_at=None, siesa_response=None)
        sjs._ejecutar_con_preflag(obj, lambda: {'modo_ensayo': True})
        assert sjs.estado_del_preflag(obj) == sjs.PREFLAG_LIBRE


class TestUnCrashEntreLaMarcaYElPost:

    def test_entrada_oc_sin_desenlace_no_se_da_por_hecha(self, db, almacen):
        rec, job = _recepcion(db, almacen, bandera=True)
        with pytest.raises(ConnektaResultadoDesconocido) as e:
            sjs._ejecutar_job(job)
        assert str(e.value).startswith(sjs.MARCA_SIN_VERIFICAR)

    def test_entrada_oc_con_desenlace_es_idempotente(self, db, almacen):
        rec, job = _recepcion(db, almacen, bandera=True, respuesta='{"codigo": 0}')
        assert sjs._ejecutar_job(job)['idempotente'] is True

    def test_averia_por_tarea_sin_desenlace_tampoco(self, db, almacen, producto):
        t, job = _tarea_dev(db, almacen, producto, bandera=True)
        with pytest.raises(ConnektaResultadoDesconocido):
            sjs._ejecutar_job(job)

    def test_ajuste_de_conteo_sin_desenlace_tampoco(self, db, almacen, producto):
        from app.models.conteo import EstadoConteo, SesionConteo
        from app.models.ubicacion import Ubicacion
        ub = Ubicacion(codigo='UB-PF', almacen_id=almacen.id, activo=True)
        db.session.add(ub)
        db.session.flush()
        s = SesionConteo(codigo=f'CNT-PF-{uuid.uuid4().hex[:5]}', tipo='MANUAL',
                         ubicacion_id=ub.id, almacen_id=almacen.id, producto_id=producto.id,
                         estado=EstadoConteo.AJUSTANDO, siesa_triggered=True)
        db.session.add(s)
        db.session.commit()
        job = SiesaJob(tipo='AJUSTE_CONTEO', payload=json.dumps({
            'sesion_id': s.id, 'ubicacion_id': ub.id, 'producto_id': producto.id,
            'cantidad': 3, 'motivo_codigo': 'AJ-SAL'}))
        db.session.add(job)
        db.session.commit()
        with pytest.raises(ConnektaResultadoDesconocido):
            sjs._ejecutar_job(job)
        db.session.refresh(s)
        assert s.estado == EstadoConteo.AJUSTANDO, 'se dio por ajustado sin constancia'


class TestLaSalidaHumana:

    def _fallido(self, db, job, e):
        job.estado = EstadoSiesaJob.FALLIDO
        job.error_ultimo = str(e)
        db.session.commit()

    def test_sin_verificar_se_ofrece_y_reintentar_se_niega(self, db, almacen):
        rec, job = _recepcion(db, almacen, bandera=True)
        self._fallido(db, job, 'x')
        assert sjs.preflag_sin_verificar(job)
        with pytest.raises(ValueError):
            sjs.exigir_no_sin_verificar(job)

    def test_si_esta_en_siesa_cierra_sin_post(self, db, almacen):
        from app.models.usuario import Usuario
        u = Usuario(email=f'a{uuid.uuid4().hex[:5]}@t.co', nombre='A', rol='admin', activo=True)
        u.set_password('x')
        db.session.add(u)
        rec, job = _recepcion(db, almacen, bandera=True)
        self._fallido(db, job, 'x')
        sjs.resolver_preflag_sin_verificar(job.id, usuario_id=u.id, entro=True,
                                           motivo='la vi en Siesa: EA-77')
        db.session.commit()
        assert sjs.estado_del_preflag(rec) == sjs.PREFLAG_ENVIADO
        assert not sjs.preflag_sin_verificar(job)
        assert sjs._ejecutar_job(job)['idempotente'] is True

    def test_un_fallido_con_desenlace_no_es_sin_verificar(self, db, almacen):
        """Entró, y el job falló después (p. ej. al encolar las averías)."""
        rec, job = _recepcion(db, almacen, bandera=True, respuesta='{"codigo": 0}')
        self._fallido(db, job, 'x')
        assert not sjs.preflag_sin_verificar(job)


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete
# ═════════════════════════════════════════════════════════════════════════════

POLITICA = {'_exigir_preflag_con_desenlace', 'estado_del_preflag'}

#: (tipo, línea aproximada) → por qué lee `.siesa_triggered` en una condición.
LECTURAS_DECLARADAS = {
    'AJUSTE_CONTEO': 'estado inconsistente AJUSTADO sin bandera: la corrige (y le pone su '
                     'desenlace), no decide un envío',
}


def _ramas(src: str) -> dict:
    """tipo → nodo `If` de `_ejecutar_job` cuyo test es `job.tipo == 'X'`."""
    arbol = ast.parse(src)
    fn = next(n for n in ast.walk(arbol) if isinstance(n, ast.FunctionDef)
              and n.name == '_ejecutar_job')
    out = {}
    for n in ast.walk(fn):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and isinstance(n.test.left, ast.Attribute) and n.test.left.attr == 'tipo'
                and len(n.test.comparators) == 1
                and isinstance(n.test.comparators[0], ast.Constant)):
            out[n.test.comparators[0].value] = n
    return out


def analizar(src: str, tipos) -> dict:
    """tipo → {'pregunta': bool, 'lee_bandera': bool, 'preflag': bool}."""
    out = {}
    for tipo, rama in _ramas(src).items():
        pregunta = lee = pre = False
        for n in ast.walk(rama):
            if isinstance(n, ast.Call):
                nombre = getattr(n.func, 'id', None) or getattr(n.func, 'attr', None)
                pregunta |= nombre in POLITICA
                pre |= nombre == '_ejecutar_con_preflag'
            if isinstance(n, (ast.If, ast.IfExp)):
                lee |= any(isinstance(a, ast.Attribute) and a.attr == 'siesa_triggered'
                           for a in ast.walk(n.test))
        out[tipo] = {'pregunta': pregunta, 'lee_bandera': lee, 'preflag': pre,
                     'con_preflag': tipo in tipos}
    return out


def _real():
    src = (RAIZ / 'app/services/siesa_job_service.py').read_text(encoding='utf-8')
    return analizar(src, sjs.TIPOS_CON_PREFLAG)


class TestUnaPoliticaParaTodoPreflag:

    def test_toda_rama_con_preflag_pregunta(self):
        faltan = [t for t, v in _real().items() if v['con_preflag'] and not v['pregunta']]
        assert not faltan, f'{faltan}: decide el envío sin estado_del_preflag'

    def test_ninguna_decide_leyendo_la_bandera(self):
        malos = {t for t, v in _real().items() if v['con_preflag'] and v['lee_bandera']}
        assert not (malos - set(LECTURAS_DECLARADAS)), malos - set(LECTURAS_DECLARADAS)

    def test_declaradas_solo_encogen(self):
        vivos = {t for t, v in _real().items() if v['con_preflag'] and v['lee_bandera']}
        assert not (set(LECTURAS_DECLARADAS) - vivos)

    def test_todo_preflag_esta_en_la_lista(self):
        fuera = [t for t, v in _real().items() if v['preflag'] and not v['con_preflag']]
        assert not fuera, f'{fuera}: usa _ejecutar_con_preflag y no está en TIPOS_CON_PREFLAG'

    def test_piso(self):
        r = _real()
        assert len(r) >= 10
        assert all(r[t]['con_preflag'] for t in sjs.TIPOS_CON_PREFLAG)

    def test_ve_lo_que_debe_y_no_lo_sano(self):
        src = ('def _ejecutar_job(job):\n'
               '    if job.tipo == "A":\n        if rec.siesa_triggered:\n            return 1\n'
               '        _ejecutar_con_preflag(rec, f)\n'
               '    if job.tipo == "B":\n        """if rec.siesa_triggered"""\n'
               '        if _exigir_preflag_con_desenlace(rec, job, "x"):\n            return 1\n'
               '        _ejecutar_con_preflag(rec, f)\n')
        r = analizar(src, ('A', 'B'))
        assert r['A'] == {'pregunta': False, 'lee_bandera': True, 'preflag': True, 'con_preflag': True}
        assert r['B'] == {'pregunta': True, 'lee_bandera': False, 'preflag': True, 'con_preflag': True}


class TestLasPantallasNoAfirmanLoQueNoSaben:
    """P2-8: «✓ Sincronizada con Siesa» con la bandera puesta y el POST «no sé»."""

    def test_la_recepcion_sin_desenlace_dice_sin_verificar_y_quien(self, db, almacen):
        rec, _job = _recepcion(db, almacen, bandera=True)
        e = rec.to_dict()['siesa_envio']
        assert e['estado'] == sjs.PREFLAG_SIN_DESENLACE and 'Sin verificar' in e['texto']
        assert 'Recuperación' in e['quien_resuelve']

    def test_con_desenlace_si_esta_sincronizada(self, db, almacen, producto):
        rec, _ = _recepcion(db, almacen, bandera=True, respuesta='{"codigo": 0}')
        assert rec.to_dict()['siesa_envio']['texto'] == 'Sincronizada con Siesa'
        t, _ = _tarea_dev(db, almacen, producto, bandera=True)
        assert t.to_dict()['siesa_envio']['estado'] == sjs.PREFLAG_SIN_DESENLACE

    def test_recepcion_js_no_decide_con_la_bandera(self):
        src = (RAIZ / 'app/static/pwa/recepcion.js').read_text(encoding='utf-8')
        assert 'siesa_triggered' not in src
        assert src.count('recSiesaBadge(') >= 4
