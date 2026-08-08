"""
Descartar jobs de ajuste: que no mienta sobre lo que pasó ni sobre lo que dejó.

Contexto (2026-08-08): 159 jobs en FALLIDO, 103 de tipo AJUSTE_CONTEO. Antes de
apretar el botón de descartar se leyó qué hace, y tenía dos defectos:

  1. Escribía `estado = COMPLETADO` y metía `descartado: true` dentro del JSON
     de `resultado`. Toda consulta filtra por estado, ninguna abre el JSON: 103
     ajustes que nunca salieron del WMS quedaban contados como enviados a Siesa.
     `EstadoDevolucion` ya tenía DESCARTADO desde antes — la palabra existía en
     el repo, a este enum le faltaba.

  2. Respondía `{descartados, sesiones_reset}`. Con 103 jobs y —digamos— 12
     sesiones reseteadas, nada decía dónde quedaron las otras 91. Y una de las
     formas de quedar es invisible: una sesión `AJUSTANDO` con
     `siesa_triggered=True` no la resetea el descarte (filtra por
     `not siesa_triggered`) **y** la ignora el barrido de sesiones atascadas de
     `siesa_job_service` (filtra por `siesa_triggered == False`). Al quedarse
     sin job no la recoge nadie y no aparece en ningún contador.

El preview existe por (2): la acción es masiva, no se deshace, y su parte
peligrosa no se ve después de ejecutarla.
"""
import json

import pytest

from app.models.conteo import EstadoConteo, SesionConteo
from app.models.siesa_job import EstadoSiesaJob, SiesaJob

_PREVIEW = '/api/conteo/descartar-fallos/preview'
_DESCARTAR = '/api/conteo/descartar-fallos'


def _sesion(db, ub, almacen, producto, codigo, estado, triggered=False):
    s = SesionConteo(
        codigo=codigo,
        tipo='MANUAL',
        clasificacion_abc='C',
        ubicacion_id=ub.id,
        almacen_id=almacen.id,
        producto_id=producto.id,
        producto_codigo_siesa=producto.codigo_siesa,
        maneja_lote=False,
        estado=estado,
    )
    s.siesa_triggered = triggered
    db.session.add(s)
    db.session.commit()
    return s


def _job_fallido(db, sesion_id):
    j = SiesaJob(
        tipo='AJUSTE_CONTEO',
        payload=json.dumps({'sesion_id': sesion_id} if sesion_id else {}),
        referencia_tipo='SesionConteo',
        referencia_id=sesion_id,
        estado=EstadoSiesaJob.FALLIDO,
    )
    db.session.add(j)
    db.session.commit()
    return j


@pytest.fixture
def h(jwt_token_admin):
    return {'Authorization': f'Bearer {jwt_token_admin}'}


class TestUnJobDescartadoNoEsUnJobExitoso:

    def test_queda_en_DESCARTADO_no_en_COMPLETADO(
            self, client, db, ub_picking, almacen, producto, h):
        s = _sesion(db, ub_picking, almacen, producto, 'CC-D1', EstadoConteo.AJUSTANDO)
        j = _job_fallido(db, s.id)

        assert client.post(_DESCARTAR, headers=h).status_code == 200

        db.session.refresh(j)
        assert j.estado == EstadoSiesaJob.DESCARTADO, (
            'un job descartado marcado COMPLETADO hace que todo contador de '
            'envíos exitosos a Siesa incluya envíos que nunca ocurrieron')
        assert j.estado != EstadoSiesaJob.COMPLETADO

    def test_el_enum_distingue_terminar_de_abandonar(self):
        assert EstadoSiesaJob.DESCARTADO not in EstadoSiesaJob.ACTIVOS
        assert EstadoSiesaJob.DESCARTADO in EstadoSiesaJob.TERMINALES
        assert EstadoSiesaJob.COMPLETADO in EstadoSiesaJob.TERMINALES

    def test_no_lo_cuenta_el_health_como_fallido(
            self, client, db, ub_picking, almacen, producto, h):
        """Descartar sí tiene que sacarlo del contador de fallidos — ese es el
        propósito. Lo que no puede es entrar al de exitosos."""
        s = _sesion(db, ub_picking, almacen, producto, 'CC-D2', EstadoConteo.AJUSTANDO)
        _job_fallido(db, s.id)
        client.post(_DESCARTAR, headers=h)

        from app.services.siesa_job_service import get_jobs_fallidos
        assert get_jobs_fallidos() == []


class TestElPreviewNoEscribeNada:

    def test_no_toca_el_job(self, client, db, ub_picking, almacen, producto, h):
        s = _sesion(db, ub_picking, almacen, producto, 'CC-P1', EstadoConteo.AJUSTANDO)
        j = _job_fallido(db, s.id)

        r = client.get(_PREVIEW, headers=h)
        assert r.status_code == 200
        assert r.get_json()['ejecutado'] is False

        db.session.refresh(j)
        db.session.refresh(s)
        assert j.estado == EstadoSiesaJob.FALLIDO
        assert s.estado == EstadoConteo.AJUSTANDO

    def test_preview_y_post_calculan_el_mismo_plan(
            self, client, db, ub_picking, almacen, producto, h):
        """Un preview que estima por su cuenta miente el día que uno de los dos
        cambie. Comparten `_plan_descarte_ajustes`; esto lo comprueba desde
        afuera, no leyendo el código."""
        s1 = _sesion(db, ub_picking, almacen, producto, 'CC-P2', EstadoConteo.AJUSTANDO)
        s2 = _sesion(db, ub_picking, almacen, producto, 'CC-P3',
                     EstadoConteo.AJUSTANDO, triggered=True)
        _job_fallido(db, s1.id)
        _job_fallido(db, s2.id)

        antes = client.get(_PREVIEW, headers=h).get_json()
        despues = client.post(_DESCARTAR, headers=h).get_json()

        assert antes['resumen'] == despues['resumen']
        assert antes['huerfanas'] == despues['huerfanas']
        assert antes['jobs_fallidos'] == despues['jobs_fallidos']


class TestDeclaraLoQueNoToca:

    def test_una_sesion_AJUSTANDO_sin_marca_vuelve_a_DESCUADRE(
            self, client, db, ub_picking, almacen, producto, h):
        s = _sesion(db, ub_picking, almacen, producto, 'CC-R1', EstadoConteo.AJUSTANDO)
        _job_fallido(db, s.id)

        r = client.post(_DESCARTAR, headers=h).get_json()

        db.session.refresh(s)
        assert s.estado == EstadoConteo.DESCUADRE
        assert r['resumen'].get('reset_a_descuadre') == 1

    def test_una_sesion_AJUSTANDO_con_marca_se_declara_huerfana(
            self, client, db, ub_picking, almacen, producto, h):
        """El caso que hay que ver ANTES de apretar el botón: ni el descarte ni
        el barrido la tocan, y sin job no la recoge nadie."""
        s = _sesion(db, ub_picking, almacen, producto, 'CC-H1',
                    EstadoConteo.AJUSTANDO, triggered=True)
        _job_fallido(db, s.id)

        prev = client.get(_PREVIEW, headers=h).get_json()

        assert prev['huerfanas'] == [s.id]
        assert 'advertencia' in prev
        assert str(s.id) in prev['advertencia']
        assert prev['resumen'].get('QUEDA_HUERFANA') == 1

    def test_la_huerfana_no_se_resetea_al_ejecutar(
            self, client, db, ub_picking, almacen, producto, h):
        s = _sesion(db, ub_picking, almacen, producto, 'CC-H2',
                    EstadoConteo.AJUSTANDO, triggered=True)
        client_job = _job_fallido(db, s.id)
        client.post(_DESCARTAR, headers=h)

        db.session.refresh(s)
        db.session.refresh(client_job)
        assert s.estado == EstadoConteo.AJUSTANDO, (
            'resetearla borraría la marca de que el ajuste pudo haber llegado a '
            'Siesa — un doble ajuste de inventario es peor que una sesión trabada')
        assert client_job.estado == EstadoSiesaJob.DESCARTADO

    def test_una_sesion_ya_ajustada_se_declara_intacta(
            self, client, db, ub_picking, almacen, producto, h):
        s = _sesion(db, ub_picking, almacen, producto, 'CC-A1', EstadoConteo.AJUSTADO)
        _job_fallido(db, s.id)

        r = client.post(_DESCARTAR, headers=h).get_json()

        db.session.refresh(s)
        assert s.estado == EstadoConteo.AJUSTADO
        assert r['resumen'].get('sin_tocar_ya_ajustada') == 1

    def test_un_job_sin_sesion_en_payload_se_declara(
            self, client, db, h):
        _job_fallido(db, None)
        r = client.get(_PREVIEW, headers=h).get_json()
        assert r['resumen'].get('job_sin_sesion_en_payload') == 1

    def test_todo_job_aparece_en_el_plan(
            self, client, db, ub_picking, almacen, producto, h):
        """Anti-silencio: la suma del resumen tiene que dar el total. Si una
        categoría nueva no se declarara, los jobs se perderían del reporte sin
        que nada lo diga."""
        _sesion(db, ub_picking, almacen, producto, 'CC-T1', EstadoConteo.AJUSTANDO)
        for est, trig in ((EstadoConteo.AJUSTANDO, False),
                          (EstadoConteo.AJUSTANDO, True),
                          (EstadoConteo.AJUSTADO, False),
                          (EstadoConteo.CANCELADO, False)):
            s = _sesion(db, ub_picking, almacen, producto,
                        f'CC-T-{est}-{trig}', est, triggered=trig)
            _job_fallido(db, s.id)
        _job_fallido(db, 999999)          # sesión inexistente

        r = client.get(_PREVIEW, headers=h).get_json()
        assert sum(r['resumen'].values()) == r['jobs_fallidos'] == 5
        assert len(r['items']) == 5


class TestPermisos:

    def test_sin_token_no_pasa(self, client):
        assert client.get(_PREVIEW).status_code == 401
        assert client.post(_DESCARTAR).status_code == 401

    def test_un_operario_no_puede_previsualizar(self, client, jwt_token):
        r = client.get(_PREVIEW, headers={'Authorization': f'Bearer {jwt_token}'})
        assert r.status_code == 403
