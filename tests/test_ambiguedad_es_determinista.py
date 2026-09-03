"""Una ambigüedad no se arregla esperando: reintentarla es tiempo perdido.

`LineaDevueltaAmbigua` se levanta cuando la referencia devuelta aparece en DOS
líneas de la factura (el producto de doble unidad, PQ + UND) y el item llegó
sin `f470_rowid`: no se puede saber a cuál corresponde lo devuelto. **Los
mismos datos la producen siempre.** El payload del job no cambia entre
intentos, la factura tampoco.

Los dos handlers de nota crédito la atrapaban y la re-lanzaban como
`raise Exception(...) from _e_amb`. El envoltorio agregaba contexto valioso
—job, devolución, factura— y de paso **destruía el tipo**: al clasificador del
DLQ le llegaba una `Exception` pelada, indistinguible de un rechazo de Siesa,
que sí se puede reintentar. Medido: 4 reintentos inútiles con backoff
5/15/45/120 → **185 minutos (3,1 h) hasta que alguien se entera**, mientras el
operario cree que la nota crédito va en camino.

El precedente correcto está en el mismo archivo: `_ResultadoDesconocido` va a
FALLIDO directo, sin gastar reintentos, con alerta al admin. Y del otro lado,
`DependenciaPendiente` no gasta reintento por la razón inversa.

## Este archivo mide en las dos direcciones

Un detector que solo prueba que dispara prueba la mitad. El DLQ es la red de
seguridad de todo el flujo financiero: lo que **no** puede cambiar es que un
fallo transitorio de verdad (timeout de red, 5xx) siga reintentando con el
mismo backoff y los mismos 5 intentos. Por eso cada clase de fallo se corre por
el mismo simulador de ciclos y se compara la traza completa.
"""
import ast
import json
import pathlib
from datetime import datetime

import pytest

from app.models.siesa_job import EstadoSiesaJob, SiesaJob, _BACKOFF_MINUTOS
from app.services import siesa_job_service as sjs
from app.services.connekta_gateway import (
    ConnektaCircuitOpenError,
    ConnektaResultadoDesconocido,
)


# ── Simulador de ciclos del DLQ ────────────────────────────────────────────
# Mide lo que mide el auditor: cuántos intentos gasta un job y cuánto tiempo
# acumulado pasa hasta que aparece la alerta. Un test que solo mire `isinstance`
# no ve ninguna de las dos cosas.

def _correr_dlq(db, job_id, max_ciclos=12):
    """Corre ciclos del DLQ adelantando el reloj hasta que el job sea terminal.

    Devuelve (job, esperas_en_minutos, ciclos). `esperas` es la lista de
    backoffs que el DLQ impuso — su suma es el tiempo hasta la alerta.
    """
    esperas = []
    for ciclo in range(1, max_ciclos + 1):
        sjs._run_dlq_jobs()
        job = db.session.get(SiesaJob, job_id)
        db.session.refresh(job)
        if job.estado in EstadoSiesaJob.TERMINALES:
            return job, esperas, ciclo
        # Sigue PENDIENTE: anotar lo que hace esperar y adelantar el reloj.
        if job.proximo_intento:
            esperas.append(round(
                (job.proximo_intento - datetime.utcnow()).total_seconds() / 60))
            job.proximo_intento = None
            db.session.commit()
    return job, esperas, max_ciclos


def _job_generico(db, tipo='ENTRADA_OC'):
    job = SiesaJob(tipo=tipo, estado=EstadoSiesaJob.PENDIENTE,
                   referencia_tipo='Recepcion', referencia_id=1, payload='{}')
    db.session.add(job)
    db.session.commit()
    return job.id


def _explota_con(exc):
    def _boom(_job):
        raise exc
    return _boom


# ── El caso real: una factura de doble unidad ──────────────────────────────

FACTURA_DOBLE_UNIDAD = [
    {'f470_rowid': 11, 'f120_referencia': 'PAPELSP6741', 'f470_cant_base': 1,
     'f470_vlr_neto': 24250, 'f470_id_unidad_medida': 'PQ', 'f150_id': 'NB1'},
    {'f470_rowid': 12, 'f120_referencia': 'PAPELSP6741', 'f470_cant_base': 6,
     'f470_vlr_neto': 2425, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1'},
]
ITEM_SIN_ROWID = [{'codigo': 'PAPELSP6741', 'cantidad_devuelta': 1}]


@pytest.fixture
def factura_ambigua(monkeypatch):
    """La factura tiene dos líneas con la misma referencia; el item no trae rowid."""
    from app.services.connekta_gateway import connekta
    monkeypatch.setattr(connekta, 'get_rowids_factura',
                        lambda *a, **k: list(FACTURA_DOBLE_UNIDAD))
    return connekta


def _job_nc_factura(db):
    from app.models.recaudo_entrega import RecaudoEntrega
    r = RecaudoEntrega(ruta_id=1, tarea_id=1, monto_cobrado=1000,
                       estado_entrega='RECHAZADO', forma_pago='EFECTIVO')
    db.session.add(r)
    db.session.flush()
    job = SiesaJob(tipo='NOTA_CREDITO_FACTURA', estado=EstadoSiesaJob.PENDIENTE,
                   referencia_tipo='RecaudoEntrega', referencia_id=r.id,
                   payload=json.dumps({
                       'recaudo_id': r.id, 'tipo_docto_fe': 'FEW',
                       'consec_fe': 1466, 'es_total': False,
                       'items_devueltos': ITEM_SIN_ROWID}))
    db.session.add(job)
    db.session.commit()
    return r, job


def _job_nc_devolucion(db):
    from app.models.devolucion_cliente import DevolucionCliente
    d = DevolucionCliente(codigo='DEV-0042', tarea_packing_id=1,
                          almacen_id=1, tipo_docto_fe='FEW', consec_fe='1466',
                          estado='ABIERTA', es_total=False)
    db.session.add(d)
    db.session.flush()
    job = SiesaJob(tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE',
                   estado=EstadoSiesaJob.PENDIENTE,
                   referencia_tipo='DevolucionCliente', referencia_id=d.id,
                   payload=json.dumps({
                       'devolucion_id': d.id, 'tipo_docto_fe': 'FEW',
                       'consec_fe': '1466',
                       'items_devueltos': ITEM_SIN_ROWID}))
    db.session.add(job)
    db.session.commit()
    return d, job


class TestElTipoLlegaAlClasificador:
    """El envoltorio puede agregar contexto; lo que no puede es borrar el tipo.

    Es la misma convención que `ConnektaPaginacionError` y
    `_ResultadoDesconocido` ya siguen en este flujo: *«se deja pasar con su
    tipo intacto; envolverla en un `Exception` genérico la haría
    indistinguible»*.
    """

    def test_nota_credito_factura(self, db, factura_ambigua):
        _, job = _job_nc_factura(db)
        with pytest.raises(Exception) as exc:
            sjs._ejecutar_job(job)
        assert isinstance(exc.value, sjs.LineaDevueltaAmbigua), (
            f'el clasificador del DLQ ve {type(exc.value).__name__}, no '
            f'LineaDevueltaAmbigua — no la puede distinguir de un rechazo de '
            f'Siesa y la reintenta 5 veces')

    def test_nota_credito_devolucion_cliente(self, db, factura_ambigua):
        _, job = _job_nc_devolucion(db)
        with pytest.raises(Exception) as exc:
            sjs._ejecutar_job(job)
        assert isinstance(exc.value, sjs.LineaDevueltaAmbigua), (
            f'el clasificador del DLQ ve {type(exc.value).__name__}, no '
            f'LineaDevueltaAmbigua')

    def test_el_contexto_no_se_pierde(self, db, factura_ambigua):
        """Preservar el tipo no puede costar el contexto: quien reciba la
        alerta tiene que poder resolverla sin abrir el código.

        Hacen falta la devolución, la referencia y los rowids candidatos — sin
        los rowids el mensaje dice que hay un problema pero no cuál línea."""
        d, job = _job_nc_devolucion(db)
        with pytest.raises(sjs.LineaDevueltaAmbigua) as exc:
            sjs._ejecutar_job(job)
        texto = str(exc.value)
        for esperado in ('DEV-0042', 'PAPELSP6741', '11', '12', 'FEW'):
            assert esperado in texto, (
                f'la alerta no nombra {esperado!r} — no alcanza para resolver '
                f'la ambigüedad a mano: {texto!r}')

    def test_recaudo_tambien_se_nombra(self, db, factura_ambigua):
        r, job = _job_nc_factura(db)
        with pytest.raises(sjs.LineaDevueltaAmbigua) as exc:
            sjs._ejecutar_job(job)
        assert f'recaudo={r.id}' in str(exc.value) or str(r.id) in str(exc.value)
        assert 'PAPELSP6741' in str(exc.value)


class TestNoGastaReintentosNiTresHoras:
    """Lo que costaba el envoltorio, medido en el DLQ y no en un `isinstance`."""

    def test_va_a_fallido_en_el_primer_ciclo(self, db, factura_ambigua):
        _, job = _job_nc_devolucion(db)
        jid = job.id
        job, esperas, ciclos = _correr_dlq(db, jid)

        assert job.estado == EstadoSiesaJob.FALLIDO, (
            f'quedó {job.estado} tras {ciclos} ciclos')
        assert sum(esperas) == 0, (
            f'el DLQ hizo esperar {sum(esperas)} min ({esperas}) antes de '
            f'avisar — la ambigüedad no se arregla esperando, y mientras tanto '
            f'el operario cree que la nota crédito va en camino')
        assert ciclos == 1, (
            f'la ambigüedad tardó {ciclos} ciclos del DLQ en llegar a FALLIDO: '
            f'los mismos datos producen el mismo resultado, reintentar solo '
            f'retrasa la alerta')
        assert job.proximo_intento is None, (
            'quedó reprogramado: el DLQ lo va a volver a correr sobre los '
            'mismos datos')

    def test_no_consume_intentos(self, db, factura_ambigua):
        """`intentos` cuenta fallos que pueden salir distinto la próxima vez.
        Éste no puede. Gastarlos es el mismo «un contador, dos significados»
        que ya costó las banderas de idempotencia."""
        _, job = _job_nc_devolucion(db)
        jid = job.id
        job, _, _ = _correr_dlq(db, jid)
        assert job.intentos == 0, (
            f'gastó {job.intentos} de {job.max_intentos} intentos en un error '
            f'que da lo mismo las 5 veces')

    def test_alerta_al_admin_con_lo_necesario(self, db, factura_ambigua, monkeypatch):
        """Un FALLIDO silencioso es peor que un reintento: nadie mira."""
        alertados = []
        monkeypatch.setattr(sjs, '_crear_alerta_admin', alertados.append)
        _, job = _job_nc_devolucion(db)
        jid = job.id
        job, _, _ = _correr_dlq(db, jid)

        assert [j.id for j in alertados] == [jid], (
            'nadie se enteró: el job quedó FALLIDO sin alerta al admin')
        assert 'PAPELSP6741' in (job.error_ultimo or ''), (
            f'el error guardado no nombra la referencia: {job.error_ultimo!r}')
        assert 'DEV-0042' in (job.error_ultimo or ''), (
            f'el error guardado no nombra la devolución: {job.error_ultimo!r}')

    def test_las_dos_notas_credito_se_comportan_igual(self, db, factura_ambigua):
        """El defecto estaba escrito dos veces. Arreglar una sola deja la otra
        esperando 3 horas — y es la que más corre."""
        _, job_f = _job_nc_factura(db)
        _, job_d = _job_nc_devolucion(db)
        jf, ef, cf = _correr_dlq(db, job_f.id)
        jd, ed, cd = _correr_dlq(db, job_d.id)
        assert (jf.estado, sum(ef), jf.intentos) == \
               (jd.estado, sum(ed), jd.intentos) == \
               (EstadoSiesaJob.FALLIDO, 0, 0)


class TestLoTransitorioSigueReintentandoIgual:
    """La mitad que más importa: el DLQ es la red de seguridad del flujo
    financiero. Un cambio que apure la ambigüedad y de paso deje de reintentar
    un timeout de red **deja de mandar documentos a Siesa** sin que nada avise.
    """

    def test_un_error_de_red_gasta_los_cinco_intentos(self, db, monkeypatch):
        jid = _job_generico(db)
        monkeypatch.setattr(sjs, '_ejecutar_job',
                            _explota_con(ConnectionError('Siesa no responde')))
        job, esperas, ciclos = _correr_dlq(db, jid)

        assert job.estado == EstadoSiesaJob.FALLIDO
        assert job.intentos == job.max_intentos == 5
        assert esperas == _BACKOFF_MINUTOS[:4] == [5, 15, 45, 120], (
            f'el backoff de un fallo transitorio cambió: {esperas}')
        assert sum(esperas) == 185

    def test_un_500_de_siesa_reintenta_igual(self, db, monkeypatch):
        """Un rechazo explícito puede salir distinto mañana (periodo contable
        reabierto, ítem desbloqueado): reintentarlo es correcto."""
        jid = _job_generico(db)
        monkeypatch.setattr(sjs, '_ejecutar_job',
                            _explota_con(Exception('HTTP 500 desde Connekta')))
        job, esperas, _ = _correr_dlq(db, jid)
        assert job.estado == EstadoSiesaJob.FALLIDO
        assert job.intentos == 5
        assert esperas == [5, 15, 45, 120]

    def test_resultado_desconocido_sigue_yendo_a_fallido_sin_reintento(self, db, monkeypatch):
        """Regla 3: un POST no reintenta ante timeout. Estaba antes del cambio
        y tiene que seguir estando."""
        jid = _job_generico(db)
        monkeypatch.setattr(
            sjs, '_ejecutar_job',
            _explota_con(ConnektaResultadoDesconocido('sin respuesta')))
        job, esperas, ciclos = _correr_dlq(db, jid)
        assert (job.estado, job.intentos, esperas, ciclos) == \
               (EstadoSiesaJob.FALLIDO, 0, [], 1)
        assert job.proximo_intento is None

    def test_dependencia_pendiente_sigue_sin_gastar_intento(self, db, monkeypatch):
        """Esperar no es fallar: se reprograma a 30 min y NO consume intento."""
        jid = _job_generico(db)
        monkeypatch.setattr(
            sjs, '_ejecutar_job',
            _explota_con(sjs.DependenciaPendiente('la NC todavía no salió')))
        sjs._run_dlq_jobs()
        job = db.session.get(SiesaJob, jid)
        assert job.estado == EstadoSiesaJob.PENDIENTE
        assert job.intentos == 0
        espera = round((job.proximo_intento - datetime.utcnow()).total_seconds() / 60)
        assert espera == 30, f'la espera de una dependencia cambió a {espera} min'

    def test_circuit_breaker_sigue_sin_gastar_intento(self, db, monkeypatch):
        jid = _job_generico(db)
        monkeypatch.setattr(
            sjs, '_ejecutar_job',
            _explota_con(ConnektaCircuitOpenError('Siesa caído')))
        sjs._run_dlq_jobs()
        job = db.session.get(SiesaJob, jid)
        assert (job.estado, job.intentos, job.proximo_intento) == \
               (EstadoSiesaJob.PENDIENTE, 0, None)

    def test_un_job_sano_sigue_completando(self, db, monkeypatch):
        """El detector en las dos direcciones incluye la operación normal."""
        jid = _job_generico(db)
        monkeypatch.setattr(sjs, '_ejecutar_job', lambda j: {'ok': True})
        sjs._run_dlq_jobs()
        job = db.session.get(SiesaJob, jid)
        assert job.estado == EstadoSiesaJob.COMPLETADO


class TestReglaSeisElPreFlagSigueDespuesDelRaise:
    """La ambigüedad se levanta ANTES de encender ninguna bandera. Si algún día
    se moviera, un job fallido dejaría `siesa_nc_triggered=True` y la nota
    crédito no se emitiría nunca — trabada por su propia guarda de idempotencia.
    """

    def test_la_devolucion_queda_sin_bandera(self, db, factura_ambigua):
        d, job = _job_nc_devolucion(db)
        did = d.id
        with pytest.raises(sjs.LineaDevueltaAmbigua):
            sjs._ejecutar_job(job)
        from app.models.devolucion_cliente import DevolucionCliente
        assert db.session.get(DevolucionCliente, did).siesa_nc_triggered is False

    def test_el_recaudo_queda_sin_bandera(self, db, factura_ambigua):
        r, job = _job_nc_factura(db)
        rid = r.id
        with pytest.raises(sjs.LineaDevueltaAmbigua):
            sjs._ejecutar_job(job)
        from app.models.recaudo_entrega import RecaudoEntrega
        assert db.session.get(RecaudoEntrega, rid).siesa_nc_triggered is False

    def test_por_ast_el_raise_va_antes_del_pre_flag(self):
        """Por AST y no por comentario: lo que importa es el orden en el
        código, no lo que el docstring promete."""
        arbol = ast.parse(pathlib.Path(
            'app/services/siesa_job_service.py').read_text())
        handlers = [n for n in ast.walk(arbol)
                    if isinstance(n, ast.ExceptHandler) and n.type is not None
                    and 'LineaDevueltaAmbigua' in ast.dump(n.type)]
        assert len(handlers) == 2, (
            f'se esperaban 2 handlers de la ambigüedad, hay {len(handlers)}')
        preflags = [n.lineno for n in ast.walk(arbol)
                    if isinstance(n, ast.Attribute)
                    and n.attr == 'siesa_nc_triggered'
                    and isinstance(n.ctx, ast.Store)]
        assert preflags
        for h in handlers:
            posteriores = [ln for ln in preflags if ln > h.end_lineno]
            assert posteriores, (
                f'el handler de la línea {h.lineno} quedó DESPUÉS de todos los '
                f'pre-flags — la ambigüedad ahora falla con la bandera arriba')


class TestTrinqueteElEnvoltorioNoVuelve:
    """El envoltorio genérico se escribió dos veces la misma semana, mientras
    el archivo de al lado documentaba por qué no hacerlo. Un trinquete por AST
    para que la tercera no pase."""

    def test_ningun_except_tipado_relanza_exception_generica(self):
        arbol = ast.parse(pathlib.Path(
            'app/services/siesa_job_service.py').read_text())
        culpables = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.ExceptHandler) or nodo.type is None:
                continue
            capturado = ast.dump(nodo.type)
            if "id='Exception'" in capturado:
                continue   # `except Exception` no tiene tipo que preservar
            for hijo in ast.walk(nodo):
                if not isinstance(hijo, ast.Raise) or hijo.exc is None:
                    continue
                fn = hijo.exc.func if isinstance(hijo.exc, ast.Call) else hijo.exc
                if isinstance(fn, ast.Name) and fn.id in ('Exception', 'RuntimeError'):
                    culpables.append((hijo.lineno, capturado[:60]))
        assert not culpables, (
            f'un `except` tipado vuelve a re-lanzar Exception genérica y le '
            f'borra el tipo al clasificador del DLQ: {culpables}')

    def test_el_clasificador_nombra_la_ambiguedad(self):
        """Si el branch desaparece, la ambigüedad cae al camino genérico y
        vuelven las 3 horas — sin que ningún test de `_construir_lineas_nc` se
        entere."""
        fuente = pathlib.Path('app/services/siesa_job_service.py').read_text()
        arbol = ast.parse(fuente)
        run = next(n for n in ast.walk(arbol)
                   if isinstance(n, ast.FunctionDef) and n.name == '_run_dlq_jobs')
        assert any(isinstance(n, ast.Name) and n.id == 'LineaDevueltaAmbigua'
                   for n in ast.walk(run)), (
            '_run_dlq_jobs no menciona LineaDevueltaAmbigua — el clasificador '
            'la trata como un fallo transitorio cualquiera')
