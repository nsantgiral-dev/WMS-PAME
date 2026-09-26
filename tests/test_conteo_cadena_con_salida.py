"""
Ninguna cadena de conteo queda trabada ni pierde trabajo.

## La clase

«Una operación sobre un miembro de la cadena (CC1 → CC2 → CC3) deja a la
cadena en un estado sin salida». La cadena avanza por propagación: el hijo que
resuelve mueve a la raíz (`ConteoService.registrar_conteo`). Toda operación que
cambie el estado de UN miembro sin mirar a los otros puede dejar:

- una raíz en SEGUNDO/TERCER_CONTEO esperando un eslabón que ya no va a llegar
  —y como ese estado está en `EstadoConteo.CADENA_EN_CURSO`, el generador no
  vuelve a programar el hueco nunca—; o
- un eslabón vivo trabajando para una raíz que ya cerró.

Las operaciones revisadas: cancelar (el caso), editar (corregir la raíz hasta
MATCH con un hijo vivo), descartar-fallos (una sesión AJUSTANDO sin job),
omitir-segundo, reabrir, liberar zombis y cancelar el rezago (estas tres no
dejaban salidas trabadas). Y `CNT-09` ve la forma rota venga de donde venga.

## Y dos defectos vecinos

- **Los zombis borraban el avance**: se liberaba por antigüedad
  (`fecha_inicio` > 2 h) aunque el operario siguiera escaneando. Ahora se
  libera por inactividad y lo parcial queda en `conteos_descartados`.
- **Empaque cancelado sin remisión**: la guarda de mercancía en proceso no
  tenía fecha que juzgar y sacaba el SKU del plan para siempre. Ahora el líder
  declara el regreso al estante y la guarda lo juzga como las otras salidas.

Todo con los servicios reales; lo único escrito a mano es la forma rota que ya
no se puede producir por ningún camino (para que el detector la vea).
"""
import ast
import json
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.test_conteo_teorico_pos import (  # noqa: F401 (fixtures)
    SKU, _jobs, contar_primero, siesa, tienda)
from tests.test_conteo_mercancia_en_proceso import (  # noqa: F401
    PEDIDO, _contar_cc1_cc2, _empacar, _exigir_bloqueado, _picking, _raiz)

RAIZ_REPO = pathlib.Path(__file__).resolve().parents[1]


def _svc():
    from app.services.conteo_service import ConteoService
    return ConteoService


def _mob():
    from app.services.mobile_service import MobileService
    return MobileService


def _s(db, sid):
    from app.models.conteo import SesionConteo
    db.session.expire_all()
    return db.session.get(SesionConteo, sid)


def _auth(app, usuario):
    from flask_jwt_extended import create_access_token
    with app.app_context():
        return {'Authorization': f'Bearer {create_access_token(identity=str(usuario.id))}'}


def _abrir_y_contar(sid, operario, fisico):
    """Con su recuento propio si cae fuera de tolerancia (`contar_primero`):
    estos tests miden la salida de la cadena, no la tolerancia."""
    _svc().obtener_tarea_operario(sid, operario.id)
    return contar_primero(sid, operario.id, fisico, cero_confirmado=True)


def _cc1(tienda):
    from app.models.conteo import SesionConteo
    creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
    return SesionConteo.query.filter_by(codigo=creado['codigos'][0]).one().id


def _en_segundo(siesa, tienda):
    """CC1 = 7 contra Siesa 10 → raíz SEGUNDO_CONTEO con su CC2 PENDIENTE."""
    siesa.poner(existencia=10, pos=0)
    cc1 = _cc1(tienda)
    r1 = _abrir_y_contar(cc1, tienda['a'], 7)
    assert r1['resultado'] == 'SEGUNDO_CONTEO', r1
    return cc1, r1['segundo_conteo_id']


def _en_tercero(siesa, tienda):
    """CC1 = 7, CC2 = 8 → raíz TERCER_CONTEO, CC2 DESCUADRE, CC3 PENDIENTE."""
    cc1, cc2 = _en_segundo(siesa, tienda)
    r2 = _abrir_y_contar(cc2, tienda['b'], 8)
    assert r2['resultado'] == 'TERCER_CONTEO', r2
    return cc1, cc2, r2['tercer_conteo_id']


def _cancelar(app, client, quien, sid, motivo='no vale'):
    return client.put(f'/api/conteo/{sid}/cancelar', json={'motivo': motivo},
                      headers=_auth(app, quien))


# ─────────────────────────────────────────────────────────────────────────────
# 1 · Cancelar cualquier miembro cancela la cadena
# ─────────────────────────────────────────────────────────────────────────────

class TestCancelarCualquierMiembroCancelaLaCadena:

    def test_cancelar_el_cc2_cancela_la_raiz_y_libera_el_hueco(
            self, app, client, db, siesa, tienda):
        """El caso: la raíz quedaba en SEGUNDO_CONTEO para siempre."""
        cc1, cc2 = _en_segundo(siesa, tienda)
        r = _cancelar(app, client, tienda['supervisor'], cc2, 'se contó mal')
        assert r.status_code == 200, r.get_json()
        assert (_s(db, cc1).estado, _s(db, cc2).estado) == ('CANCELADO', 'CANCELADO')
        assert 'se contó mal' in _s(db, cc1).motivo_edicion
        assert _s(db, cc1).editado_por == tienda['supervisor'].id
        creado = _svc().crear_conteo_manual(tienda['almacen'].id, SKU)
        assert creado['tareas_nuevas'] == 1, 'el hueco se puede volver a contar'

    def test_cancelar_el_cc3_cancela_la_raiz(self, app, client, db, siesa, tienda):
        cc1, cc2, cc3 = _en_tercero(siesa, tienda)
        assert _cancelar(app, client, tienda['supervisor'], cc3).status_code == 200
        assert _s(db, cc1).estado == 'CANCELADO'
        assert _s(db, cc3).estado == 'CANCELADO'
        assert _s(db, cc2).estado == 'DESCUADRE', 'una observación resuelta no se reescribe'

    def test_cancelar_el_cc2_resuelto_cancela_lo_vivo(self, app, client, db, siesa, tienda):
        cc1, cc2, cc3 = _en_tercero(siesa, tienda)
        assert _cancelar(app, client, tienda['supervisor'], cc2).status_code == 200
        assert [_s(db, x).estado for x in (cc1, cc2, cc3)] == ['CANCELADO'] * 3

    def test_cancelar_la_raiz_en_tercer_conteo_cancela_el_cc3(
            self, app, client, db, siesa, tienda):
        """Antes: 409 sobre la raíz en TERCER_CONTEO. Y si se forzaba, el CC3
        quedaba en la cola del Conteo Definitivo contando para nada."""
        cc1, _cc2, cc3 = _en_tercero(siesa, tienda)
        assert _cancelar(app, client, tienda['supervisor'], cc1).status_code == 200
        assert (_s(db, cc1).estado, _s(db, cc3).estado) == ('CANCELADO', 'CANCELADO')
        assert _svc().listar_definitivos(tienda['almacen'].id) == []

    def test_con_el_ajuste_en_vuelo_no_se_cancela_nada(
            self, app, client, db, siesa, tienda):
        """AJUSTANDO puede haber llegado a Siesa (Regla 3): no se cancela."""
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is True, r2
        cc2 = r2['sesion_id']
        r = _cancelar(app, client, tienda['supervisor'], cc2)
        assert r.status_code == 409, r.get_json()
        # El 409 saldría igual por «nada abierto»; lo que se exige es que el
        # líder sepa POR QUÉ: el ajuste está en vuelo, no la cadena cerrada.
        assert 'en vuelo' in r.get_json()['error']
        assert (_s(db, cc1).estado, _s(db, cc2).estado) == ('AJUSTANDO', 'DESCUADRE')

    def test_una_cadena_cerrada_no_se_cancela(self, app, client, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        cc1 = _cc1(tienda)
        assert _abrir_y_contar(cc1, tienda['a'], 10)['resultado'] == 'MATCH'
        r = _cancelar(app, client, tienda['supervisor'], cc1)
        assert r.status_code == 409
        assert _s(db, cc1).estado == 'MATCH'

    def test_sin_motivo_o_sin_rol_no(self, app, client, db, siesa, tienda):
        cc1, cc2 = _en_segundo(siesa, tienda)
        assert _cancelar(app, client, tienda['supervisor'], cc2, '  ').status_code == 400
        assert _cancelar(app, client, tienda['a'], cc2).status_code == 403
        assert (_s(db, cc1).estado, _s(db, cc2).estado) == ('SEGUNDO_CONTEO', 'PENDIENTE')

    def test_la_politica_vive_en_el_servicio(self, db, siesa, tienda):
        """Un guard en la ruta protege esa ruta; en el servicio, la operación."""
        _cc1_id, cc2 = _en_segundo(siesa, tienda)
        with pytest.raises(PermissionError):
            _svc().cancelar_cadena(cc2, tienda['a'].id, 'x')
        _svc().cancelar_cadena(cc2, tienda['supervisor'].id, 'x')
        assert _s(db, _cc1_id).estado == 'CANCELADO'


# ─────────────────────────────────────────────────────────────────────────────
# 2 · Las demás operaciones sobre un miembro
# ─────────────────────────────────────────────────────────────────────────────

class TestEditarNoDejaHijosContandoParaNada:

    def test_no_se_corrige_la_raiz_con_un_cc2_vivo(self, app, client, db, siesa, tienda):
        """Corregir la raíz hasta que cuadrara la pasaba a MATCH y el CC2 vivo
        contaba para nada (la propagación exige la raíz en SEGUNDO_CONTEO)."""
        cc1, cc2 = _en_segundo(siesa, tienda)
        r = client.put(f'/api/conteo/{cc1}/editar',
                       json={'cantidad_fisica': 10, 'motivo_edicion': 'tipeo'},
                       headers=_auth(app, tienda['supervisor']))
        assert r.status_code == 409, r.get_json()
        assert 'cancele la cadena' in r.get_json()['error']
        assert (_s(db, cc1).estado, _s(db, cc1).cantidad_fisica) == ('SEGUNDO_CONTEO', 7)
        assert _s(db, cc2).estado == 'PENDIENTE'

    def test_sin_hijos_vivos_se_corrige_como_siempre(self, app, client, db, siesa, tienda):
        cc1, _r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        raiz = _s(db, cc1)
        raiz.estado = 'DESCUADRE'           # como la deja «omitir» o un bloqueo
        db.session.commit()
        r = client.put(f'/api/conteo/{cc1}/editar',
                       json={'cantidad_fisica': 10, 'motivo_edicion': 'recontado'},
                       headers=_auth(app, tienda['supervisor']))
        assert r.status_code == 200, r.get_json()
        assert _s(db, cc1).estado == 'MATCH'


class TestSoloSeCorrigeLaRaizYaContada:
    """`/editar` aceptaba la cantidad en cualquier estado salvo AJUSTADO y
    AJUSTANDO, y `reconciliar_cantidad` pone MATCH si cuadra: un PENDIENTE
    cerraba sin que nadie contara, un CANCELADO resucitaba y un CC2 cambiaba
    sin mover a la raíz que se aprueba. Una política
    (`motivo_no_se_corrige_cantidad`) para el servidor y la pantalla."""

    def _editar(self, app, client, tienda, sid, n):
        return client.put(f'/api/conteo/{sid}/editar',
                          json={'cantidad_fisica': n, 'motivo_edicion': 'prueba'},
                          headers=_auth(app, tienda['supervisor']))

    def test_un_pendiente_no_se_cierra_sin_contar(self, app, client, db, siesa, tienda):
        siesa.poner(existencia=10, pos=0)
        cc1 = _cc1(tienda)
        _s(db, cc1).existencia_siesa = 10          # con referencia: reconciliaría
        db.session.commit()
        r = self._editar(app, client, tienda, cc1, 10)
        assert r.status_code == 409, r.get_json()
        assert 'PENDIENTE' in r.get_json()['error']
        assert (_s(db, cc1).estado, _s(db, cc1).cantidad_fisica) == ('PENDIENTE', None)
        assert 'PENDIENTE' in _s(db, cc1).to_dict()['no_se_corrige_cantidad']

    def test_un_cancelado_no_resucita(self, app, client, db, siesa, tienda):
        cc1, _r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        _s(db, cc1).estado = 'CANCELADO'
        db.session.commit()
        r = self._editar(app, client, tienda, cc1, 10)
        assert r.status_code == 409, r.get_json()
        assert _s(db, cc1).estado == 'CANCELADO'

    def test_un_cc2_se_corrige_en_la_raiz(self, app, client, db, siesa, tienda):
        cc1, cc2, _cc3 = _en_tercero(siesa, tienda)
        assert _s(db, cc2).estado == 'DESCUADRE'
        r = self._editar(app, client, tienda, cc2, 10)
        assert r.status_code == 409, r.get_json()
        assert 'Corrija la raíz' in r.get_json()['error']
        assert _s(db, cc2).cantidad_fisica == 8

    def test_la_raiz_contada_si_y_la_pantalla_lo_sabe(self, app, client, db, siesa, tienda):
        cc1, _r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        raiz = _s(db, cc1)
        raiz.estado = 'DESCUADRE'
        db.session.commit()
        assert _s(db, cc1).to_dict()['no_se_corrige_cantidad'] is None
        assert self._editar(app, client, tienda, cc1, 10).status_code == 200


class TestDescartarNoDejaAjustandoSinJob:

    def test_la_posiblemente_enviada_sale_por_una_persona_sin_reenviar(
            self, app, client, db, siesa, tienda):
        """Su job queda FALLIDO sin verificar. Hasta la tanda 2 (2026-09-25)
        «Reintentar» la cerraba como AJUSTADO sin preguntarle a nadie si el
        ajuste llegó — la rama P4 leía la bandera sola como «entró». Ahora
        reintentar la salta (`sin_verificar`) y la salida es una persona que
        dice «está en Siesa»: entonces sí se cierra sin volver a llamar."""
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        from app.services.siesa_job_service import (_ejecutar_job,
                                                    resolver_preflag_sin_verificar)
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is True
        (job,) = _jobs(cc1)
        s = _s(db, cc1)
        s.siesa_triggered = True            # el POST salió y no se supo el resultado
        job.estado = EstadoSiesaJob.FALLIDO
        db.session.commit()
        h = _auth(app, tienda['supervisor'])

        r = client.post('/api/conteo/descartar-fallos', headers=h).get_json()
        assert r['huerfanas'] == [cc1] and r['descartados'] == 0
        assert db.session.get(SiesaJob, job.id).estado == EstadoSiesaJob.FALLIDO

        r = client.post('/api/conteo/reintentar-fallos', headers=h).get_json()
        assert r['reencolados'] == 0 and r['sin_verificar'] == [job.id]
        assert db.session.get(SiesaJob, job.id).estado == EstadoSiesaJob.FALLIDO

        resolver_preflag_sin_verificar(job.id, usuario_id=tienda['supervisor'].id,
                                       entro=True, motivo='El ADI está en Siesa')
        db.session.commit()
        with patch('app.services.connekta_gateway.ConnektaGateway.enviar_ajuste_inventario',
                   side_effect=AssertionError('no se reenvía')):
            _ejecutar_job(db.session.get(SiesaJob, job.id))
        db.session.commit()
        assert _s(db, cc1).estado == 'AJUSTADO'

    def test_si_no_llego_se_envia_de_nuevo(self, app, client, db, siesa, tienda):
        from app.models.siesa_job import EstadoSiesaJob
        from app.services.siesa_job_service import (_ejecutar_job,
                                                    resolver_preflag_sin_verificar)
        cc1, _r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        (job,) = _jobs(cc1)
        _s(db, cc1).siesa_triggered = True
        job.estado = EstadoSiesaJob.FALLIDO
        db.session.commit()
        resolver_preflag_sin_verificar(job.id, usuario_id=tienda['supervisor'].id,
                                       entro=False, motivo='No aparece el ADI')
        db.session.commit()
        assert _s(db, cc1).siesa_triggered is False
        with patch('app.services.connekta_gateway.ConnektaGateway.enviar_ajuste_inventario',
                   return_value={'codigo': 0}) as env:
            _ejecutar_job(job)
        env.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# 3 · CNT-09: la forma rota, venga de donde venga
# ─────────────────────────────────────────────────────────────────────────────

def _cnt09():
    from app.services import auditoria
    r = auditoria.auditar('conteo')
    return next(x for x in r['resultados'] if x['codigo'] == 'CNT-09')


class TestCNT09:

    def test_una_cadena_sana_no_marca(self, db, siesa, tienda):
        _en_segundo(siesa, tienda)
        assert _cnt09()['total'] == 0

    def test_una_cadena_cancelada_por_la_regla_no_marca(self, db, siesa, tienda):
        _c1, _c2, cc3 = _en_tercero(siesa, tienda)
        _svc().cancelar_cadena(cc3, tienda['supervisor'].id, 'x')
        assert _cnt09()['total'] == 0

    def test_ve_la_raiz_esperando_un_cc2_cancelado(self, db, siesa, tienda):
        """Exactamente lo que escribía la ruta vieja: el CC2 CANCELADO y la
        raíz sin tocar."""
        cc1, cc2 = _en_segundo(siesa, tienda)
        _s(db, cc2).estado = 'CANCELADO'
        db.session.commit()
        r = _cnt09()
        assert r['total'] == 1, r
        assert r['severidad'] == 'BLOQUEA'

    def test_ve_la_raiz_esperando_un_cc3_que_no_esta(self, db, siesa, tienda):
        _c1, _c2, cc3 = _en_tercero(siesa, tienda)
        _s(db, cc3).estado = 'CANCELADO'
        db.session.commit()
        assert _cnt09()['total'] == 1

    def test_ve_un_cc2_vivo_bajo_una_raiz_cerrada(self, db, siesa, tienda):
        """Lo que escribía `editar` al corregir la raíz hasta MATCH."""
        cc1, _cc2 = _en_segundo(siesa, tienda)
        _s(db, cc1).estado = 'MATCH'
        db.session.commit()
        assert _cnt09()['total'] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 4 · Los zombis se liberan por inactividad y no pierden el rastro
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def abierto(db, siesa, tienda):
    """CC1 abierto por `a` hace 3 horas."""
    siesa.poner(existencia=10, pos=0)
    sid = _cc1(tienda)
    _svc().obtener_tarea_operario(sid, tienda['a'].id)
    s = _s(db, sid)
    s.fecha_inicio = datetime.utcnow() - timedelta(hours=3)
    db.session.commit()
    return sid


class TestLosZombisSeLiberanPorInactividad:

    def test_quien_sigue_contando_no_pierde_nada(self, db, tienda, abierto):
        """El caso: abierto hace 3 h, escaneando ahora. Antes perdía lo contado."""
        _mob().fijar_total_conteo(tienda['a'].id, abierto, 5)
        assert _svc().liberar_tareas_zombi() == 0
        s = _s(db, abierto)
        assert (s.estado, s.operario_id, s.cantidad_fisica) == ('EN_PROCESO', tienda['a'].id, 5)

    def test_el_escaneo_tambien_es_actividad(self, db, tienda, abierto):
        _mob().procesar_escaneo(tienda['a'].id, abierto, 'CONTEO', SKU,
                                cantidad=1, total_previo=0)
        assert _s(db, abierto).ultima_actividad_at is not None
        assert _svc().liberar_tareas_zombi() == 0

    def test_el_inactivo_vuelve_a_la_cola_y_lo_parcial_queda(self, db, tienda, abierto):
        _mob().fijar_total_conteo(tienda['a'].id, abierto, 5)
        s = _s(db, abierto)
        s.ultima_actividad_at = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        assert _svc().liberar_tareas_zombi() == 1
        s = _s(db, abierto)
        assert (s.estado, s.operario_id, s.cantidad_fisica, s.fecha_inicio,
                s.ultima_actividad_at, s.foto_inicio_at) == (
            'PENDIENTE', None, None, None, None, None)
        (desc,) = s.lista_conteos_descartados()
        assert (desc['motivo'], desc['cantidad_fisica'], desc['operario_id']) == (
            'INACTIVIDAD', 5, tienda['a'].id)
        assert desc['inicio']['existencia'] == 10, 'la foto de apertura queda en el rastro'

    def test_quien_la_toma_empieza_de_cero(self, db, tienda, abierto):
        """Conteo ciego: no se hereda un parcial ajeno."""
        _mob().fijar_total_conteo(tienda['a'].id, abierto, 5)
        s = _s(db, abierto)
        s.ultima_actividad_at = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        _svc().liberar_tareas_zombi()
        vista = _svc().obtener_tarea_operario(abierto, tienda['b'].id)
        assert vista['cantidad_contada'] == 0
        assert _s(db, abierto).foto_inicio_at is not None, 'relee la foto al abrir'

    def test_sin_actividad_se_mide_desde_la_apertura(self, db, tienda, abierto):
        assert _svc().liberar_tareas_zombi() == 1

    def test_la_inactividad_no_es_un_recuento_por_venta(self, db, tienda, abierto):
        """Las métricas de recuento cuentan solo los descartes por movimiento de
        Siesa; los de la cola no dicen nada de Siesa."""
        from app.services.metricas.conteo import _descartes
        _mob().fijar_total_conteo(tienda['a'].id, abierto, 5)
        s = _s(db, abierto)
        s.ultima_actividad_at = datetime.utcnow() - timedelta(hours=3)
        s.conteos_descartados = json.dumps([{'descartado_at': datetime.utcnow().isoformat()}])
        db.session.commit()
        _svc().liberar_tareas_zombi()
        s = _s(db, abierto)
        assert len(s.lista_conteos_descartados()) == 2
        assert len(list(_descartes(s))) == 1, 'la entrada vieja (sin motivo) sí es de movimiento'


# ─────────────────────────────────────────────────────────────────────────────
# 5 · Trinquete: nadie devuelve un conteo a la cola a mano
# ─────────────────────────────────────────────────────────────────────────────

#: Las únicas funciones que pueden borrar lo contado, con su porqué.
PUEDEN_BORRAR_LO_CONTADO = {
    ('app/services/conteo_service.py', 'devolver_al_pool'):
        'la política: deja lo parcial en conteos_descartados antes de borrarlo',
    ('app/services/conteo_service.py', '_descartar_conteo'):
        'recuento (por movimiento de Siesa o fuera de tolerancia): lo deja en '
        'conteos_descartados y la MISMA sesión sigue con el mismo operario',
}


def _es_estado_conteo_pendiente(nodo) -> bool:
    return (isinstance(nodo, ast.Attribute) and nodo.attr == 'PENDIENTE'
            and isinstance(nodo.value, ast.Name)
            and ('Conteo' in nodo.value.id or nodo.value.id == '_EC'))


def _devoluciones_a_mano(fuente: str) -> list:
    """Funciones que borran `cantidad_fisica` o que devuelven una sesión de
    conteo a la cola (`X.estado = EstadoConteo.PENDIENTE` y `X.operario_id =
    None` sobre el mismo objeto). `[(funcion, que)]`."""
    halladas = []
    for fn in ast.walk(ast.parse(fuente)):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a_pendiente, sin_dueno = set(), set()
        for nodo in ast.walk(fn):
            if not isinstance(nodo, ast.Assign):
                continue
            for t in nodo.targets:
                if not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)):
                    continue
                valor_none = isinstance(nodo.value, ast.Constant) and nodo.value.value is None
                if t.attr == 'cantidad_fisica' and valor_none:
                    halladas.append((fn.name, 'borra cantidad_fisica'))
                if t.attr == 'operario_id' and valor_none:
                    sin_dueno.add(t.value.id)
                if t.attr == 'estado' and _es_estado_conteo_pendiente(nodo.value):
                    a_pendiente.add(t.value.id)
        for nombre in a_pendiente & sin_dueno:
            halladas.append((fn.name, f'devuelve {nombre} a la cola'))
    return halladas


def _inventario():
    hallazgos = []
    for ruta in sorted((RAIZ_REPO / 'app').rglob('*.py')):
        rel = ruta.relative_to(RAIZ_REPO).as_posix()
        for fn, que in _devoluciones_a_mano(ruta.read_text(encoding='utf-8')):
            hallazgos.append((rel, fn, que))
    return hallazgos


class TestNadieDevuelveAlPoolAMano:
    """Cinco bucles devolvían un conteo a la cola: dos borraban lo contado sin
    rastro y uno lo dejaba para que lo heredara el siguiente operario. Todos
    pasan ahora por `ConteoService.devolver_al_pool`."""

    def test_solo_las_funciones_declaradas(self):
        fuera = [h for h in _inventario() if (h[0], h[1]) not in PUEDEN_BORRAR_LO_CONTADO]
        assert not fuera, (
            'Devolver un conteo a la cola (o borrar lo contado) a mano pierde el '
            'rastro o deja el parcial para el siguiente: usá '
            f'ConteoService.devolver_al_pool. Encontrado: {fuera}')

    def test_cada_declaracion_dice_por_que(self):
        assert all(len(v) > 20 for v in PUEDEN_BORRAR_LO_CONTADO.values())

    def test_piso_minimo(self):
        """Si el escáner se rompe devuelve cero y todo pasa: las dos declaradas
        tienen que seguir apareciendo."""
        vistas = {(h[0], h[1]) for h in _inventario()}
        assert set(PUEDEN_BORRAR_LO_CONTADO) <= vistas

    def test_ve_las_dos_formas(self):
        fuente = (
            'def a(s):\n    s.cantidad_fisica = None\n'
            'def b(c):\n    c.operario_id = None\n    c.estado = EstadoConteo.PENDIENTE\n'
            'def c(x):\n    x.estado = _EC.PENDIENTE\n    x.operario_id = None\n')
        assert sorted(f for f, _ in _devoluciones_a_mano(fuente)) == ['a', 'b', 'c']

    def test_no_marca_lo_que_no_es(self):
        fuente = (
            'def a(s):\n    s.cantidad_fisica = 5\n'
            'def b(t):\n    t.operario_id = None\n    t.estado = EstadoPicking.PENDIENTE\n'
            'def c(x, y):\n    x.estado = EstadoConteo.PENDIENTE\n    y.operario_id = None\n')
        assert _devoluciones_a_mano(fuente) == []


# ─────────────────────────────────────────────────────────────────────────────
# 6 · Recogido sin despachar: la salida con fecha de la guarda
# ─────────────────────────────────────────────────────────────────────────────

def _pk():
    from app.services.picking_service import PickingService
    return PickingService


class TestRecogidoSinDespacharTieneSalida:

    def test_empaque_cancelado_se_lista_y_se_declara(self, db, siesa, tienda):
        from app.services.packing_service import PackingService
        packing = _empacar(tienda, _picking(tienda))
        PackingService.cancelar(packing.id, motivo='pedido anulado')
        (fila,) = _pk().recogido_sin_despachar(tienda['almacen'].id)
        assert (fila['pedido'], fila['situacion']) == (PEDIDO, 'EMPAQUE_CANCELADO')
        assert fila['productos'][0]['cantidad_recogida'] == 3

        _pk().declarar_devuelto_al_estante(PEDIDO, tienda['almacen'].id,
                                           tienda['supervisor'].id, nota='al estante 3')
        assert _pk().recogido_sin_despachar(tienda['almacen'].id) == []
        assert _svc().procesos_en_curso(tienda['almacen'].id,
                                        producto_id=tienda['producto'].id) == []

    def test_nunca_empacado_tambien(self, db, siesa, tienda):
        _picking(tienda)
        (fila,) = _pk().recogido_sin_despachar(tienda['almacen'].id)
        assert fila['situacion'] == 'NUNCA_EMPACADO'

    def test_despues_del_regreso_el_conteo_ajusta(self, db, siesa, tienda):
        from app.services.packing_service import PackingService
        packing = _empacar(tienda, _picking(tienda))
        PackingService.cancelar(packing.id, motivo='pedido anulado')
        _pk().declarar_devuelto_al_estante(PEDIDO, tienda['almacen'].id, tienda['supervisor'].id)
        _cc1_id, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is True, r2

    def test_un_conteo_de_antes_del_regreso_sigue_bloqueado(self, db, siesa, tienda):
        """Se juzga el instante del conteo: el regreso no cubre lo de antes."""
        from app.services.packing_service import PackingService
        packing = _empacar(tienda, _picking(tienda))
        PackingService.cancelar(packing.id, motivo='pedido anulado')
        cc1, r2 = _contar_cc1_cc2(tienda, siesa, 7, existencia=10)
        assert r2['auto_encolado'] is False
        _pk().declarar_devuelto_al_estante(PEDIDO, tienda['almacen'].id, tienda['supervisor'].id)
        assert _svc().mercancia_en_proceso(_raiz(db, cc1))

    def test_no_se_declara_con_un_empaque_vivo_ni_con_remision(self, db, siesa, tienda):
        _empacar(tienda, _picking(tienda))
        assert _pk().recogido_sin_despachar(tienda['almacen'].id) == []
        with pytest.raises(LookupError):
            _pk().declarar_devuelto_al_estante(PEDIDO, tienda['almacen'].id,
                                               tienda['supervisor'].id)

    def test_solo_supervision(self, db, siesa, tienda):
        _picking(tienda)
        with pytest.raises(PermissionError):
            _pk().declarar_devuelto_al_estante(PEDIDO, tienda['almacen'].id, tienda['a'].id)

    def test_el_mensaje_de_la_guarda_dice_donde_declararlo(self, db, siesa, tienda):
        from app.services.packing_service import PackingService
        packing = _empacar(tienda, _picking(tienda))
        PackingService.cancelar(packing.id, motivo='pedido anulado')
        (h,) = _svc().procesos_en_curso(tienda['almacen'].id, producto_id=tienda['producto'].id)
        assert 'Recogido sin despachar' in h['accion']

    def test_las_rutas(self, app, client, db, siesa, tienda):
        _picking(tienda)
        h = _auth(app, tienda['supervisor'])
        r = client.get('/api/conteo/recogido-sin-despachar', headers=h)
        assert r.status_code == 200 and r.get_json()['total'] == 1
        assert client.get('/api/conteo/recogido-sin-despachar',
                          headers=_auth(app, tienda['a'])).status_code == 403
        r = client.post('/api/conteo/recogido-sin-despachar/devuelto', headers=h,
                        json={'pedido': PEDIDO, 'almacen_id': tienda['almacen'].id})
        assert r.status_code == 200, r.get_json()
        r = client.post('/api/conteo/recogido-sin-despachar/devuelto', headers=h,
                        json={'pedido': PEDIDO, 'almacen_id': tienda['almacen'].id})
        assert r.status_code == 404, 'ya declarado'
