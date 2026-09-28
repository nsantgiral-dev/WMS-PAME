"""
La bandera sola no es «hecho» — tampoco en la retención ni en las notas
crédito (validación de la plata, 2026-09-26, P1).

## La clase

*Un documento de plata cerrado como hecho por su bandera de pre-envío, sin
desenlace confirmado.* La regla se cerró para el RC (P0-4) y no para el DC ni
las NC: la retención que falló sin respuesta dejaba su cuenta PUC marcada y el
job FALLIDO; «Reintentar» lo volvía a la cola y el ejecutor lo cerraba
COMPLETADO «idempotente» por la marca, sin que nadie hubiera mirado Siesa. La
pantalla pintaba «DC ✓».

## Ahora

- El ejecutor: la marca sola es «no sé» → FALLIDO sin reintento
  (`_ResultadoDesconocido`); idempotente solo con el desenlace anotado
  (`politica_cobro.dc_llego` / `nc_llego`; la NC de devolución, con la
  respuesta de Siesa guardada).
- `siesa_job_service.TIPOS_CON_PREFLAG` incluye la retención y las dos NC:
  «Reintentar» se niega y la salida es «¿Está en Siesa?» — en Recuperación
  (admin) y en Liquidación (`POST …/recaudos/<id>/resolver-documento`, quien
  liquida), FORZAR.
- «DC ✓» con señal positiva (`llego`), como «RC ✓».

## El trinquete (AST)

En los ejecutores de los documentos de plata, todo `return {'idempotente':
True, …}` está bajo un `if` que pregunta por el desenlace confirmado.
"""
import ast
import json
import pathlib

import pytest

from tests.test_cartera_retencion import _jwt, _usuario
from tests.test_parada_tardia import mundo  # noqa: F401 — fixture
from tests.test_parada_de_oficina import _url

RAIZ = pathlib.Path(__file__).resolve().parents[1]
PUC = '13551501'


def _confirmada(app, client, db, f, uc):
    from app.models.packing import TareaPacking
    from app.models.recaudo_entrega import RecaudoEntrega
    v = float(db.session.get(TareaPacking, f.packing_id).valor_factura or 0) or 1000.0
    r = client.post(_url(f), headers=_jwt(app, uc), json={
        'estado_entrega': 'ENTREGADO', 'forma_pago': 'EFECTIVO', 'monto_cobrado': v,
        'version_formulario': 4, 'observaciones': 'x'})
    assert r.status_code == 200, r.get_json()
    return RecaudoEntrega.query.filter_by(ruta_id=f.ruta_id).one()


def _dc_sin_verificar(db, rec):
    from app.models.siesa_job import SiesaJob
    rec.marcar_puc_enviada(PUC)
    rec.anotar_documento_siesa('DC', 'SIN_VERIFICAR', cuenta_puc=PUC)
    job = SiesaJob(tipo='DOCUMENTO_CONTABLE_RET', estado='FALLIDO',
                   payload=json.dumps({'recaudo_id': rec.id, 'cuenta_puc': PUC,
                                       'tipo_retencion': 'RETEFUENTE_2.5'}),
                   referencia_tipo='RecaudoEntrega', referencia_id=rec.id,
                   error_ultimo='sin respuesta clara')
    db.session.add(job)
    db.session.commit()
    return job


class TestLaRetencionSinVerificar:

    def test_el_ejecutor_no_la_cierra_por_la_marca(self, app, client, db, mundo):
        from app.services import siesa_job_service as sjs
        f, uc, _ = mundo
        rec = _confirmada(app, client, db, f, uc)
        job = _dc_sin_verificar(db, rec)
        with pytest.raises(sjs._ResultadoDesconocido):
            sjs._ejecutar_job(job)
        rec.anotar_documento_siesa('DC', 'ENVIADO', cuenta_puc=PUC)
        db.session.commit()
        assert sjs._ejecutar_job(job)['idempotente'] is True

    def test_la_pantalla_no_dice_dc_listo(self, app, client, db, mundo):
        f, uc, _ = mundo
        rec = _confirmada(app, client, db, f, uc)
        rec.retenciones_detalle = [{'tipo': 'RETEFUENTE_2.5', 'puc': PUC}]
        _dc_sin_verificar(db, rec)
        [rd] = rec.to_dict()['retenciones_detalle']
        assert rd['siesa_triggered'] is True and rd['llego'] is False

    def test_liquidacion_la_muestra_y_quien_liquida_la_resuelve(self, app, client, db, mundo):
        from app.models.bitacora import BitacoraAccion
        from app.models.siesa_job import SiesaJob
        from app.services.bitacora import FORZADO_PREFLAG_RESUELTO_A_MANO
        f, uc, ad = mundo
        rec = _confirmada(app, client, db, f, uc)
        job = _dc_sin_verificar(db, rec)
        d = client.get(f'/api/rutas/{f.ruta_id}/liquidacion-detalle',
                       headers=_jwt(app, ad)).get_json()
        [doc] = d['recaudos'][0]['documentos_sin_verificar']
        assert doc['job_id'] == job.id and doc['nombre'] == 'Retención'
        url = f'/api/rutas/{f.ruta_id}/recaudos/{rec.id}/resolver-documento'
        lider = _usuario(db, 'lider_cartera')
        assert client.post(url, headers=_jwt(app, lider), json={
            'job_id': job.id, 'entro': False, 'motivo': 'x'}).status_code == 403
        liq = _usuario(db, 'liquidador')
        r = client.post(url, headers=_jwt(app, liq),
                        json={'job_id': job.id, 'entro': False, 'motivo': 'No la encontré'})
        assert r.status_code == 200, r.get_json()
        db.session.refresh(rec)
        assert PUC not in rec.pucs_enviadas()
        assert db.session.get(SiesaJob, job.id).estado == 'PENDIENTE'
        [b] = [x for x in BitacoraAccion.query.filter_by(accion='FORZAR').all()
               if x.despues.get('forzado') == FORZADO_PREFLAG_RESUELTO_A_MANO]
        assert b.usuario_id == liq.id and b.motivo == 'No la encontré'

    def test_si_entro_se_cierra_sin_reenviar(self, app, client, db, mundo):
        from app.services import siesa_job_service as sjs
        f, uc, _ = mundo
        rec = _confirmada(app, client, db, f, uc)
        job = _dc_sin_verificar(db, rec)
        sjs.resolver_preflag_sin_verificar(job.id, usuario_id=1, entro=True, motivo='NI-44')
        db.session.commit()
        assert sjs._ejecutar_job(job)['idempotente'] is True


class TestLaNotaCreditoDeRutaSinVerificar:

    def test_bandera_sin_desenlace_es_no_se(self, app, client, db, mundo):
        from app.models.siesa_job import SiesaJob
        from app.services import siesa_job_service as sjs
        f, uc, _ = mundo
        rec = _confirmada(app, client, db, f, uc)
        rec.siesa_nc_triggered = True
        job = SiesaJob(tipo='NOTA_CREDITO_FACTURA', estado='FALLIDO',
                       payload=json.dumps({'recaudo_id': rec.id, 'tipo_docto_fe': 'FEW',
                                           'consec_fe': '1'}),
                       referencia_tipo='RecaudoEntrega', referencia_id=rec.id)
        db.session.add(job)
        db.session.commit()
        assert sjs.preflag_sin_verificar(job) is True
        with pytest.raises(ValueError, match='sin verificar'):
            sjs.reintentar_job(job.id, usuario_id=1)
        with pytest.raises(sjs._ResultadoDesconocido):
            sjs._ejecutar_job(job)


# ═════════════════════════════════════════════════════════════════════════════
# Trinquete
# ═════════════════════════════════════════════════════════════════════════════

PLATA = ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA',
         'NOTA_CREDITO_DEVOLUCION_CLIENTE')
POSITIVAS = {'rc_llego_a_siesa', 'dc_llego', 'nc_llego', 'siesa_nc_response'}


def _idempotentes_sin_desenlace(fuente: str, tipos=PLATA, positivas=POSITIVAS):
    """[(tipo, linea)] de los `return {'idempotente': True}` de los bloques
    `if job.tipo == <tipo>` que no están bajo un `if` que pregunte por el
    desenlace (una llamada a POSITIVAS o el atributo `siesa_nc_response`)."""
    malos = []

    def _nombres(expr):
        out = set()
        for n in ast.walk(expr):
            if isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Name):
                out.add(n.id)
        return out

    def _es_idempotente(n):
        return (isinstance(n, ast.Return) and isinstance(n.value, ast.Dict)
                and any(isinstance(k, ast.Constant) and k.value == 'idempotente'
                        for k in n.value.keys))

    def _recorrer(nodo, tipo, guardado):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.If):
                g = guardado or bool(_nombres(hijo.test) & positivas)
                for b in hijo.body:
                    if _es_idempotente(b) and not g:
                        malos.append((tipo, b.lineno))
                    _recorrer(b, tipo, g)
                for b in hijo.orelse:
                    if _es_idempotente(b) and not guardado:
                        malos.append((tipo, b.lineno))
                    _recorrer(b, tipo, guardado)
            else:
                if _es_idempotente(hijo) and not guardado:
                    malos.append((tipo, hijo.lineno))
                _recorrer(hijo, tipo, guardado)

    for n in ast.walk(ast.parse(fuente)):
        if (isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
                and getattr(n.test.left, 'attr', None) == 'tipo'
                and isinstance(n.test.comparators[0], ast.Constant)
                and n.test.comparators[0].value in tipos):
            tipo = n.test.comparators[0].value
            for b in n.body:
                _recorrer(b, tipo, False)
    return malos


class TestLaMarcaSolaNoEsHecho:

    def test_los_ejecutores_de_plata(self):
        fuente = (RAIZ / 'app/services/siesa_job_service.py').read_text(encoding='utf-8')
        assert _idempotentes_sin_desenlace(fuente) == []
        # Piso: sin reconocer ninguna pregunta, el detector ve el idempotente de
        # cada uno de los cuatro ejecutores (si no, se desincronizó y el verde
        # de arriba no dice nada).
        todos = _idempotentes_sin_desenlace(fuente, positivas=set())
        assert {t for t, _ in todos} == set(PLATA), todos

    def test_toda_la_plata_con_pre_flag_tiene_salida(self):
        from app.services import siesa_job_service as sjs
        from app.services.permisos_liquidacion import TIPOS_JOB_DE_LIQUIDACION
        # El RC tiene la suya (`resolver_recibo_sin_verificar`).
        assert set(TIPOS_JOB_DE_LIQUIDACION) - {'RECIBO_CAJA'} <= set(sjs.TIPOS_CON_PREFLAG)
        assert 'NOTA_CREDITO_DEVOLUCION_CLIENTE' in sjs.TIPOS_CON_PREFLAG

    def test_el_detector_ve_y_no_inventa(self):
        src = '''
def _ejecutar_job(job):
    if job.tipo == 'DOCUMENTO_CONTABLE_RET':
        if rec and puc in rec.pucs_enviadas():
            return {'idempotente': True}
    if job.tipo == 'NOTA_CREDITO_FACTURA':
        if rec.siesa_nc_triggered:
            if nc_llego(rec):
                return {'idempotente': True}
            raise X()
    if job.tipo == 'OTRO':
        return {'idempotente': True}
'''
        assert _idempotentes_sin_desenlace(src) == [('DOCUMENTO_CONTABLE_RET', 5)]
