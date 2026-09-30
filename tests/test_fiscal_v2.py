"""
Fiscal v2 (2026-09-26) — lo que encontró la validación crítica del frente
fiscal, cerrado por clase.

| Clase | Instancia | Trinquete |
|---|---|---|
| *una decisión «Siesa no responde» que no deja probar al circuito* | el cierre y la DLQ leían `_cb_state == 'OPEN'` (H1) | nadie compara `_cb_state` con `'OPEN'` fuera del gateway |
| *deshacer una caja sin preguntar si su envío está en la cola* | `resetear_siesa` borraba los bultos de una caja en cola (H2) | todo sitio que cancela, reabre o borra bultos llama `exigir_sin_envio_vivo` |
| *un carril que emite sin el candado del pedido* | `/facturar-remision`, `/facturar-rm-manual` (H3) | toda llamada que emite va dentro de `with emision_exclusiva(...)`; los POST solo desde `DespachoParialService` |

Por AST, nunca por texto; con meta-tests (lo que ve, lo sano que no marca,
docstrings no cuentan) y pisos.
"""
import ast
import pathlib
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tests.test_documento_fiscal import (  # noqa: F401 — fixtures y helpers
    APP, RAIZ, _archivos, _caja_para_cerrar, _funciones, _llamadas, _sitios,
    _tarea, _toca_una_caja, siesa, siesa_real,
)


def _crea_una_caja(fn) -> bool:
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == 'TareaPacking' for n in ast.walk(fn))


# ═════════════════════════════════════════════════════════════════════════════
# H2 · Nadie deshace una caja con su envío en la cola
# ═════════════════════════════════════════════════════════════════════════════

#: Sitios que deshacen una caja sin preguntar por su envío vivo, con su porqué.
#: Solo encoge.
EXENTOS_ENVIO = {
    'app/services/traslado_service.py::TrasladoService.confirmar_picking_traslado':
        'crea la caja del traslado (no deshace nada) y su documento es el STS',
}


class TestNadieDeshaceUnaCajaEnCola:

    def _deshacen(self):
        return {s: fn for s, fn in _sitios().items()
                if _toca_una_caja(fn) and not _crea_una_caja(fn)}

    def test_todo_sitio_que_deshace_pregunta_por_el_envio(self):
        faltan = {s for s, fn in self._deshacen().items()
                  if 'exigir_sin_envio_vivo' not in _llamadas(fn)} - set(EXENTOS_ENVIO)
        assert not faltan, (
            f'{sorted(faltan)} cancelan, reabren o borran bultos de una caja sin '
            '`documento_fiscal.exigir_sin_envio_vivo`: con el DESPACHO_F470 en la '
            'cola, la emisión sale sobre una caja sin bultos (H2).')

    def test_exentos_solo_encoge(self):
        sitios = _sitios()
        for s, motivo in EXENTOS_ENVIO.items():
            assert s in sitios, f'{s} ya no existe: sacalo'
            assert 'exigir_sin_envio_vivo' not in _llamadas(sitios[s]), f'{s} ya pregunta'
            assert len(motivo) > 30

    def test_piso(self):
        assert len(self._deshacen()) >= 3, 'el detector se rompió'

    def test_resetear_confirmar_y_cancelar_preguntan(self):
        d = self._deshacen()
        for nombre in ('resetear_siesa', 'cancelar', 'confirmar_packing'):
            s = f'app/services/packing_service.py::PackingService.{nombre}'
            assert s in d and 'exigir_sin_envio_vivo' in _llamadas(d[s]), s

    def test_confirmar_se_niega_con_la_caja_en_cola(self, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.services.documento_fiscal import EnvioEnCurso
        from app.services.packing_service import PackingService
        t = _tarea(db, almacen)
        SiesaJob.encolar(tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
                         referencia_id=t.id, payload={'tarea_id': t.id})
        db.session.commit()
        with pytest.raises(EnvioEnCurso):
            PackingService.confirmar_packing(t.id, forzar=True)


# ═════════════════════════════════════════════════════════════════════════════
# H3 · Todo carril que emite toma el candado del pedido
# ═════════════════════════════════════════════════════════════════════════════

#: Métodos de `DespachoParialService` que postean (244328/142945/142943) o
#: quitan el pre-flag de la RM.
EMITEN = {'despachar_parcial', 'facturar_remision_existente', 'facturar_rm_con_consec',
          'declarar_rm_inexistente'}
#: POST crudos del gateway: solo `DespachoParialService` los llama.
POSTS = {'trigger_despacho', 'trigger_factura_desde_remision',
         'trigger_comprometer_pedido', 'trigger_factura'}

#: El candado lo toma el que llama, y se verifica abajo.
CANDADO_EN_EL_LLAMADOR = {
    'app/services/siesa_job_service.py::_emitir_despacho':
        ('app/services/siesa_job_service.py::_ejecutar_job', '_emitir_despacho'),
}


def _nombre(call):
    f = call.func
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)


def _es_candado(w: ast.With) -> bool:
    return any(isinstance(i.context_expr, ast.Call) and _nombre(i.context_expr) == 'emision_exclusiva'
               for i in w.items)


def _llamadas_fuera_del_candado(fn, nombres) -> list:
    """Las llamadas a `nombres` que NO están dentro de `with emision_exclusiva(...)`."""
    fuera = []

    def visitar(n, bajo):
        if isinstance(n, ast.With) and _es_candado(n):
            bajo = True
        if isinstance(n, ast.Call) and _nombre(n) in nombres and not bajo:
            fuera.append((_nombre(n), n.lineno))
        for h in ast.iter_child_nodes(n):
            if isinstance(h, (ast.FunctionDef, ast.AsyncFunctionDef)) and h is not fn:
                continue
            visitar(h, bajo)
    visitar(fn, False)
    return fuera


def _carriles():
    """Funciones de `app/` fuera de `DespachoParialService` que emiten."""
    return {s: fn for s, fn in _sitios().items()
            if '::DespachoParialService.' not in s
            and any(isinstance(n, ast.Call) and _nombre(n) in EMITEN for n in ast.walk(fn))}


class TestTodoCarrilDeEmisionTomaElCandado:

    def test_toda_emision_va_bajo_el_candado(self):
        malos = {}
        for s, fn in _carriles().items():
            if s in CANDADO_EN_EL_LLAMADOR:
                continue
            fuera = _llamadas_fuera_del_candado(fn, EMITEN)
            if fuera:
                malos[s] = fuera
        assert not malos, (
            f'{malos} emiten (244328/142945/142943) sin `with emision_exclusiva(...)`: '
            'con el DESPACHO_F470 del mismo pedido corriendo, factura duplicada (H3).')

    def test_el_candado_del_llamador_existe(self):
        sitios = _sitios()
        for s, (llamador, nombre) in CANDADO_EN_EL_LLAMADOR.items():
            assert s in sitios, f'{s} ya no existe: sacalo'
            assert not _llamadas_fuera_del_candado(sitios[llamador], {nombre}), (
                f'{llamador} llama {nombre} fuera del candado')
            assert any(isinstance(n, ast.Call) and _nombre(n) == nombre
                       for n in ast.walk(sitios[llamador])), f'{llamador} ya no llama {nombre}'
            quienes = [k for k, fn in sitios.items() if k != s and any(
                isinstance(n, ast.Call) and _nombre(n) == nombre for n in ast.walk(fn))]
            assert quienes == [llamador], f'{nombre} tiene otros llamadores: {quienes}'

    def test_los_post_solo_desde_el_servicio(self):
        fuera = sorted(s for s, fn in _sitios().items()
                       if '::DespachoParialService.' not in s
                       and not s.startswith('app/services/connekta_')
                       and any(isinstance(n, ast.Call) and _nombre(n) in POSTS
                               for n in ast.walk(fn)))
        assert not fuera, f'{fuera} postean 142945/142943/244328 por fuera del servicio'

    def test_piso(self):
        c = _carriles()
        assert len(c) >= 4, f'solo {sorted(c)}: el detector se rompió'
        assert 'app/services/siesa_job_service.py::_emitir_despacho' in c

    def test_meta(self):
        def una(src):
            return _funciones(src)[0][1]
        assert _llamadas_fuera_del_candado(una(
            'def f(t):\n    return S.facturar_rm_con_consec(t, "RM", 1)\n'), EMITEN)
        assert not _llamadas_fuera_del_candado(una(
            'def f(t):\n    with emision_exclusiva(t, "x") as p:\n'
            '        if p:\n            return S.facturar_rm_con_consec(t, "RM", 1)\n'), EMITEN)
        # otro `with` no es el candado; un docstring no es una llamada
        assert _llamadas_fuera_del_candado(una(
            'def f(t):\n    with advisory_lock(1) as p:\n'
            '        return S.despachar_parcial(t, {})\n'), EMITEN)
        assert not _llamadas_fuera_del_candado(una(
            'def f(t):\n    """S.despachar_parcial(t, {})"""\n    return 1\n'), EMITEN)


class TestElCandadoMuerde:

    def test_la_dlq_espera_si_otro_carril_emite_el_pedido(self, db, almacen, siesa, monkeypatch):
        from contextlib import contextmanager

        import app.utils.lock as lock_mod
        from app.services.siesa_job_service import DependenciaPendiente, _ejecutar_job
        from tests.test_documento_fiscal import _job_despacho

        @contextmanager
        def ocupado(clave, nombre=''):
            yield False
        monkeypatch.setattr(lock_mod, 'advisory_lock', ocupado)
        t = _tarea(db, almacen)
        with pytest.raises(DependenciaPendiente) as e:
            _ejecutar_job(_job_despacho(db, t))
        assert e.value.espera_minutos == 2
        assert siesa.posts_142945 == 0 and siesa.posts_244328 == 0

    def test_la_clave_es_por_pedido(self, db, almacen):
        from app.services.documento_fiscal import _clave_emision
        from app.utils.lock import RANGO_EMISION_PEDIDO
        a = _tarea(db, almacen, pedido_clave='003-PD-1')
        b = _tarea(db, almacen, pedido_clave='003-PD-1')
        c = _tarea(db, almacen, pedido_clave='003-PD-2')
        assert _clave_emision(a) == _clave_emision(b) != _clave_emision(c)
        base, tam = RANGO_EMISION_PEDIDO
        assert base <= _clave_emision(a) < base + tam


# ═════════════════════════════════════════════════════════════════════════════
# H1 · Nadie decide «Siesa no responde» sin dejar probar al circuito
# ═════════════════════════════════════════════════════════════════════════════

def _comparaciones_open(fuente: str) -> list:
    out = []
    for n in ast.walk(ast.parse(fuente)):
        if isinstance(n, ast.Compare):
            partes = [n.left, *n.comparators]
            if any(isinstance(p, ast.Attribute) and p.attr == '_cb_state' for p in partes) \
                    and any(isinstance(p, ast.Constant) and p.value == 'OPEN' for p in partes):
                out.append(n.lineno)
    return out


class TestNadieLeeElCircuitoSinDejarloProbar:

    def test_fuera_del_gateway_nadie_compara_con_open(self):
        malos = {}
        for p in _archivos():
            rel = p.relative_to(RAIZ).as_posix()
            if rel.endswith(('connekta_gateway.py', 'connekta_circuit_breaker.py')):
                continue
            ls = _comparaciones_open(p.read_text(encoding='utf-8'))
            if ls:
                malos[rel] = ls
        assert not malos, (f'{malos}: usar `connekta.circuito_admite_intento()` — leer '
                           '`_cb_state == "OPEN"` no deja probar al circuito (H1)')

    def test_meta(self):
        assert _comparaciones_open("if connekta._cb_state == 'OPEN':\n    pass\n")
        assert _comparaciones_open("x = 'OPEN' != gw._cb_state\n")
        assert not _comparaciones_open("x = connekta._cb_state != 'CLOSED'\n")
        assert not _comparaciones_open('"""connekta._cb_state == \'OPEN\'"""\n')

    def test_sin_vencer_el_intervalo_se_niega(self, siesa_real, monkeypatch):
        import time
        from app.services.documento_fiscal import siesa_disponible_para_facturar
        monkeypatch.setattr(siesa_real, '_cb_state', 'OPEN')
        monkeypatch.setattr(siesa_real, '_cb_last_probe', time.monotonic())
        ok, motivo = siesa_disponible_para_facturar()
        assert not ok and 'no responde' in motivo

    def test_admite_intento_no_consume(self):
        import time
        from app.services.connekta_circuit_breaker import ConnektaCircuitBreaker
        cb = ConnektaCircuitBreaker(probe_interval=60)
        cb.state, cb.last_probe = 'OPEN', time.monotonic() - 120
        assert cb.admite_intento() and cb.admite_intento()
        assert cb.state == 'OPEN'
        assert cb.consumir_permiso() and cb.state == 'HALF_OPEN'
        assert not cb.admite_intento()

    def test_el_precheck_con_circuito_por_probar_va_en_serie_y_lo_cierra(
            self, db, almacen, producto, siesa_real, monkeypatch):
        """Las dos consultas a la vez: la segunda la niega el breaker y el
        precheck falla justo cuando Siesa volvió."""
        import time
        from app.services.closing.pedido_closer import PedidoPackingCloser
        from app.services.connekta_gateway import ConnektaGateway

        def estado(self, t, c):
            import time as _t
            assert self._cb_consumir_permiso(), 'el probe tenía que salir'
            _t.sleep(0.3)      # el probe tarda: en paralelo la factura sale antes de que cierre
            self._cb_record_success()
            return 3

        def factura(self, t, c):
            assert self._cb_consumir_permiso(), 'la factura salió antes que el probe'
            return []
        monkeypatch.setattr(ConnektaGateway, 'get_estado_pedido', estado)
        monkeypatch.setattr(ConnektaGateway, 'get_factura_desde_pedido', factura)
        monkeypatch.setattr(siesa_real, '_cb_state', 'OPEN')
        monkeypatch.setattr(siesa_real, '_cb_last_probe', time.monotonic() - 3600)
        t = _caja_para_cerrar(db, almacen, producto)
        r = PedidoPackingCloser().ejecutar_cierre(t.id, [{'tipo': 'Caja', 'cantidad': 1}], 1)
        assert r.exitoso, r.mensaje
        assert siesa_real._cb_state == 'CLOSED'


# ═════════════════════════════════════════════════════════════════════════════
# H4 · La remisión: CO + tipo + consecutivo, y el corte al encontrar
# ═════════════════════════════════════════════════════════════════════════════

def _gw():
    from app.services.connekta_gateway import ConnektaGateway
    g = ConnektaGateway()
    g.modo_simulacion = False
    g._cb_state = 'CLOSED'
    return g


def _pag(filas):
    return {'codigo': 0, 'detalle': {'Datos': filas}}


class TestLaRemisionConReglaDieciocho:

    def test_corta_en_la_primera_pagina(self):
        gw = _gw()
        filas = [{'consec_pd': 9000 + i, 'tipo_rm': 'RM', 'consec_rm': 1 + i} for i in range(100)]
        filas[5] = {'consec_pd': 4321, 'tipo_rm': 'RM', 'consec_rm': 77}
        with patch.object(gw, '_get', return_value=_pag(filas)) as m:
            assert gw.get_remision_desde_pedido('PD', 4321) == {'tipo': 'RM', 'consec': 77}
        assert m.call_count == 1

    def test_si_trae_el_co_tiene_que_coincidir(self):
        gw = _gw()
        filas = [{'consec_pd': 4321, 'co_pd': '001', 'tipo_rm': 'RM', 'consec_rm': 5},
                 {'consec_pd': 4321, 'co_pd': '003', 'tipo_rm': 'RM', 'consec_rm': 7}]
        with patch.object(gw, '_get', return_value=_pag(filas)):
            assert gw.get_remision_desde_pedido('PD', 4321, co='003') == {'tipo': 'RM', 'consec': 7}

    def test_si_trae_el_tipo_tiene_que_coincidir(self):
        gw = _gw()
        filas = [{'consec_pd': 4321, 'tipo_pd': 'PV', 'tipo_rm': 'RM', 'consec_rm': 5}]
        with patch.object(gw, '_get', return_value=_pag(filas)):
            assert gw.get_remision_desde_pedido('PD', 4321) is None

    def test_sin_co_dos_del_mismo_consecutivo_es_no_se(self):
        from app.services.connekta_gateway import RemisionBarridoIncompleto
        gw = _gw()
        filas = [{'consec_pd': 4321, 'tipo_rm': 'RM', 'consec_rm': 5},
                 {'consec_pd': 4321, 'tipo_rm': 'RM', 'consec_rm': 7}]
        with patch.object(gw, '_get', return_value=_pag(filas)):
            with pytest.raises(RemisionBarridoIncompleto, match='Regla 18'):
                gw.get_remision_desde_pedido('PD', 4321)

    def test_solo_la_primera_pagina_sin_encontrar_no_es_no_existe(self):
        from app.services.connekta_gateway import RemisionBarridoIncompleto
        gw = _gw()
        filas = [{'consec_pd': 9000 + i, 'tipo_rm': 'RM', 'consec_rm': 1 + i} for i in range(100)]
        with patch.object(gw, '_get', return_value=_pag(filas)):
            with pytest.raises(RemisionBarridoIncompleto):
                gw.get_remision_desde_pedido('PD', 4321, max_paginas=1)

    def test_la_red_no_es_barrido_incompleto(self):
        from app.services.connekta_gateway import RemisionBarridoIncompleto, RemisionNoDisponible
        gw = _gw()
        with patch.object(gw, '_get', side_effect=RuntimeError('timeout')):
            with pytest.raises(RemisionNoDisponible) as e:
                gw.get_remision_desde_pedido('PD', 4321)
        assert not isinstance(e.value, RemisionBarridoIncompleto)


class TestLaSalidaHumanaNoLaTrabaElTope:

    def _sin_identificar(self, db, almacen):
        return _tarea(db, almacen, rm_enviada_at=datetime.utcnow() - timedelta(hours=1))

    def test_con_barrido_incompleto_pide_confirmacion(self, db, almacen, siesa):
        from app.services.connekta_gateway import RemisionBarridoIncompleto
        from app.services.despacho_parcial_service import (ConfirmacionRequerida,
                                                           DespachoParialService)
        t = self._sin_identificar(db, almacen)
        siesa.remision = RemisionBarridoIncompleto('tope')
        with pytest.raises(ConfirmacionRequerida) as e:
            DespachoParialService.declarar_rm_inexistente(t, 1, 'miré en Siesa y no está')
        assert e.value.requiere_confirmacion == t.numero_pedido_siesa
        assert t.rm_enviada_at is not None

    def test_con_la_confirmacion_se_declara_y_queda_forzar(self, db, almacen, siesa,
                                                            usuario_admin):
        from app.models.bitacora import BitacoraAccion
        from app.services.connekta_gateway import RemisionBarridoIncompleto
        from app.services.despacho_parcial_service import DespachoParialService
        t = self._sin_identificar(db, almacen)
        siesa.remision = RemisionBarridoIncompleto('tope')
        r = DespachoParialService.declarar_rm_inexistente(
            t, usuario_admin.id, 'miré en Siesa y no está', sin_barrido_completo=True,
            confirmacion=t.numero_pedido_siesa)
        assert r['pre_flag_quitado'] and r['sin_barrido_completo']
        assert t.rm_enviada_at is None
        fila = BitacoraAccion.query.filter_by(accion='FORZAR').order_by(
            BitacoraAccion.id.desc()).first()
        from app.services.bitacora import FORZADO_RM_INEXISTENTE_SIN_BARRIDO
        assert fila is not None and (fila.despues or {}).get('forzado') == FORZADO_RM_INEXISTENTE_SIN_BARRIDO

    def test_con_siesa_caido_ni_con_confirmacion(self, db, almacen, siesa):
        from app.services.connekta_gateway import RemisionNoDisponible
        from app.services.despacho_parcial_service import DespachoParialService
        t = self._sin_identificar(db, almacen)
        siesa.remision = RemisionNoDisponible('red')
        with pytest.raises(ValueError, match='No se pudo verificar'):
            DespachoParialService.declarar_rm_inexistente(
                t, 1, 'miré', sin_barrido_completo=True, confirmacion=t.numero_pedido_siesa)
        assert t.rm_enviada_at is not None


class TestRecienEnviadaNoSeBarreEntera:

    def test_tras_el_post_se_mira_una_pagina(self, db, almacen, siesa, monkeypatch):
        from app.services.connekta_gateway import ConnektaGateway
        from app.services.despacho_parcial_service import EsperandoRemision
        vistas = []

        def remision(self, tipo, consec, **k):
            vistas.append(k.get('max_paginas'))
            return None
        monkeypatch.setattr(ConnektaGateway, 'get_remision_desde_pedido', remision)
        t = _tarea(db, almacen)
        from app.services.despacho_parcial_service import DespachoParialService
        with pytest.raises(EsperandoRemision):
            DespachoParialService.despachar_parcial(t, {'SKU1': 10})
        assert vistas == [1]


# ═════════════════════════════════════════════════════════════════════════════
# H2b · El servidor dice en qué va la emisión
# ═════════════════════════════════════════════════════════════════════════════

def _job(db, t, estado='PENDIENTE', error=None):
    from app.models.siesa_job import SiesaJob
    j = SiesaJob.encolar(tipo='DESPACHO_F470', referencia_tipo='TareaPacking',
                         referencia_id=t.id, payload={'tarea_id': t.id})
    j.estado = estado
    j.error_ultimo = error
    db.session.commit()
    return j


class TestEstadoDeLaEmision:

    def test_los_estados(self, db, almacen):
        from app.services.documento_fiscal import estado_emision, estados_emision
        viejo = datetime.utcnow() - timedelta(hours=1)
        casos = {
            'EN_COLA': _tarea(db, almacen),
            'FALLIDO': _tarea(db, almacen),
            'SIN_VERIFICAR': _tarea(db, almacen, rm_enviada_at=viejo),
            'REMISION_SIN_FACTURA': _tarea(db, almacen, rm_tipo='RM', rm_consec=9,
                                           rm_enviada_at=viejo),
            'FACTURA_SIN_REMISION': _tarea(db, almacen, siesa_triggered=True, fe_tipo='FE', fe_consec='77'),
            'DESPACHADO': _tarea(db, almacen, siesa_triggered=True, rm_tipo='RM', rm_consec=8,
                                 fe_confirmada_at=viejo, estado='DESPACHADO'),
            None: _tarea(db, almacen),
        }
        _job(db, casos['EN_COLA'])
        _job(db, casos['FALLIDO'], 'FALLIDO')
        _job(db, casos['SIN_VERIFICAR'])        # esperando, con la gracia vencida
        _job(db, casos['REMISION_SIN_FACTURA'], 'FALLIDO')
        lote = estados_emision(list(casos.values()))
        for esperado, t in casos.items():
            assert estado_emision(t) == esperado, (esperado, estado_emision(t))
            assert lote[t.id] == esperado

    def test_la_lista_de_packing_lo_trae(self, client, db, almacen, usuario_admin,
                                         jwt_token_admin):
        t = _tarea(db, almacen)
        _job(db, t)
        r = client.get('/api/packing/?activas=true',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        fila = next(x for x in r.get_json()['tareas'] if x['id'] == t.id)
        assert fila['estado_emision'] == 'EN_COLA'
        d = client.get(f'/api/packing/{t.id}',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        assert d['estado_emision'] == 'EN_COLA'


# ═════════════════════════════════════════════════════════════════════════════
# P2 · Las cajas de antes del control fiscal tienen salida
# ═════════════════════════════════════════════════════════════════════════════

def _bulto_cargado(db, t, ruta):
    from app.models.bulto import Bulto
    b = Bulto(tarea_id=t.id, tipo='Caja', numero=1, total=1, estado='CARGADO',
              codigo_barras=f'B-{t.id}-X', ruta_despacho_id=ruta.id,
              fecha_cargado=datetime.utcnow())
    db.session.add(b)
    db.session.commit()
    return b


class TestBajarDelCamionLoQueNoPuedeSalir:

    def _mundo(self, db, almacen, estado_ruta='EN_CARGUE', **kw):
        from tests.test_documento_fiscal import _ruta
        t = _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO',
                   fe_tipo='FE', fe_consec='55', **kw)
        ruta = _ruta(db, estado_ruta)
        return t, ruta, _bulto_cargado(db, t, ruta)

    def test_sin_forzar_se_niega_y_lo_dice(self, db, almacen):
        from app.services.muelle_service import MuelleService
        _t, _r, b = self._mundo(db, almacen)
        with pytest.raises(ValueError, match='confírmelo con un motivo'):
            MuelleService.desasignar_de_ruta(b.id, usuario_id=1)

    def test_forzado_con_motivo_vuelve_al_muelle_y_queda_forzar(self, db, almacen,
                                                                  usuario_admin):
        from app.models.bitacora import BitacoraAccion
        from app.models.bulto import Bulto
        from app.services.bitacora import FORZADO_BULTO_CARGADO_SACADO
        from app.services.muelle_service import MuelleService
        _t, _r, b = self._mundo(db, almacen)
        MuelleService.desasignar_de_ruta(b.id, usuario_id=usuario_admin.id,
                                         motivo='caja sin remisión', forzar=True)
        b = db.session.get(Bulto, b.id)
        assert b.estado == 'PENDIENTE' and b.ruta_despacho_id is None and b.fecha_cargado is None
        fila = BitacoraAccion.query.filter_by(accion='FORZAR', entidad='Bulto').one()
        assert fila.despues['forzado'] == FORZADO_BULTO_CARGADO_SACADO

    def test_sin_motivo_no(self, db, almacen):
        from app.services.bitacora import MotivoRequerido
        from app.services.muelle_service import MuelleService
        _t, _r, b = self._mundo(db, almacen)
        with pytest.raises(MotivoRequerido):
            MuelleService.desasignar_de_ruta(b.id, usuario_id=1, motivo='  ', forzar=True)

    def test_lo_que_puede_salir_no_se_baja(self, db, almacen):
        from app.services.muelle_service import MuelleService
        _t, _r, b = self._mundo(db, almacen, rm_tipo='RM', rm_consec=4,
                                fe_confirmada_at=datetime.utcnow())
        with pytest.raises(ValueError, match='puede salir'):
            MuelleService.desasignar_de_ruta(b.id, usuario_id=1, motivo='x', forzar=True)

    def test_de_una_ruta_que_ya_salio_no(self, db, almacen):
        from app.services.muelle_service import MuelleService
        _t, _r, b = self._mundo(db, almacen, estado_ruta='EN_TRANSITO')
        with pytest.raises(ValueError, match='sigue en cargue'):
            MuelleService.desasignar_de_ruta(b.id, usuario_id=1, motivo='x', forzar=True)

    def test_por_http(self, client, db, almacen, usuario_admin, jwt_token_admin):
        _t, _r, b = self._mundo(db, almacen)
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        assert client.delete(f'/api/muelle/desasignar/{b.id}', headers=h,
                             json={'forzar': True}).status_code == 400
        r = client.delete(f'/api/muelle/desasignar/{b.id}', headers=h,
                          json={'forzar': True, 'motivo': 'sin remisión'})
        assert r.status_code == 200, r.get_json()


class TestCajasAnterioresAlControlFiscal:

    def test_la_lista_y_la_verificacion(self, client, db, almacen, siesa, usuario_admin,
                                        jwt_token_admin):
        from tests.test_documento_fiscal import _ruta
        vieja = _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO')
        _bulto_cargado(db, vieja, _ruta(db))
        _tarea(db, almacen, siesa_triggered=True, rm_tipo='RM', rm_consec=3,
               fe_confirmada_at=datetime.utcnow(), estado='DESPACHADO')   # sana
        _tarea(db, almacen, siesa_triggered=True, tipo_documento='TRASLADO')  # traslado
        h = {'Authorization': f'Bearer {jwt_token_admin}'}
        r = client.get('/api/despacho_parcial/anteriores-control-fiscal', headers=h).get_json()
        assert [c['id'] for c in r['cajas']] == [vieja.id]
        assert r['cajas'][0]['bultos'] == {'CARGADO': 1}
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': '901'}]
        siesa.remision = {'tipo': 'RM', 'consec': 61}
        v = client.get(f'/api/despacho_parcial/{vieja.id}/verificar-en-siesa', headers=h).get_json()
        assert v['factura'] == 'FE-901' and v['remision'] == {'tipo': 'RM', 'consec': 61}
        assert siesa.posts_142943 == 0 and siesa.posts_142945 == 0

    def test_completar_con_la_fe_en_siesa_no_postea(self, client, db, almacen, siesa,
                                                     usuario_admin, jwt_token_admin):
        from app.services.documento_fiscal import despachable
        vieja = _tarea(db, almacen, siesa_triggered=True, estado='DESPACHADO')
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': '901'}]
        r = client.post(f'/api/despacho_parcial/{vieja.id}/facturar-rm-manual',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'},
                        json={'tipo_rm': 'RM', 'consec_rm': 61, 'confirmacion': 'RM-61',
                              'motivo': 'caja anterior al control fiscal'})
        assert r.status_code == 200, r.get_json()
        db.session.refresh(vieja)
        assert despachable(vieja) and vieja.rm_consec == 61
        assert siesa.posts_142943 == 0

    def test_solo_admin(self, client, db, almacen, jwt_token):
        h = {'Authorization': f'Bearer {jwt_token}'}
        assert client.get('/api/despacho_parcial/anteriores-control-fiscal',
                          headers=h).status_code == 403


# ═════════════════════════════════════════════════════════════════════════════
# P3
# ═════════════════════════════════════════════════════════════════════════════

class TestP3:

    def test_un_429_en_post_es_rate_limit(self):
        import requests  # noqa: F401
        from unittest.mock import MagicMock
        from app.services.connekta_gateway import ConnektaRateLimit, ConnektaRechazado
        gw = _gw()
        r = MagicMock()
        r.status_code, r.ok, r.headers = 429, False, {'Retry-After': '120'}
        with patch('app.services.connekta_gateway.requests.post', return_value=r):
            with pytest.raises(ConnektaRateLimit) as e:
                gw._post('142945', 'X', {})
        assert isinstance(e.value, ConnektaRechazado) and e.value.retry_after_s == 120

    def test_el_dlq_no_gasta_intento_con_429(self, db, almacen, siesa, monkeypatch):
        from app.models.siesa_job import EstadoSiesaJob
        from app.services import siesa_job_service as sjs
        from app.services.connekta_gateway import ConnektaRateLimit
        from tests.test_documento_fiscal import _job_despacho
        monkeypatch.setattr(sjs, '_crear_alerta_admin', lambda job: None)
        siesa.error_142945 = ConnektaRateLimit('429', retry_after_s=90)
        t = _tarea(db, almacen)
        job = _job_despacho(db, t)
        sjs._run_dlq_jobs()
        db.session.refresh(job)
        db.session.refresh(t)
        assert job.estado == EstadoSiesaJob.PENDIENTE and job.intentos == 0
        assert job.proximo_intento > datetime.utcnow() + timedelta(seconds=60)
        assert t.rm_enviada_at is None, 'un 429 es «no entró»: el pre-flag se revierte'

    def test_en_ensayo_el_cierre_no_dice_que_emite(self, db, almacen, producto, monkeypatch):
        from app.services.connekta_gateway import connekta
        from app.services.packing_service import PackingService
        monkeypatch.setattr(connekta, 'modo_ensayo', True)
        monkeypatch.setattr(connekta, 'modo_simulacion', True)
        t = _caja_para_cerrar(db, almacen, producto)
        r = PackingService.cerrar_packing_resultado(t.id, [{'tipo': 'Caja', 'cantidad': 1}], 1)
        assert r['estado_siesa'] == 'ENSAYO' and 'no se envía nada' in r['mensaje']

    def test_reconciliar_sin_remision_no_es_despachado(self, client, db, almacen, siesa,
                                                        usuario_admin, jwt_token_admin):
        t = _tarea(db, almacen)
        siesa.facturas = [{'f350_id_tipo_docto': 'FE', 'f350_consec_docto': '71'}]
        siesa.remision = None
        r = client.post(f'/api/packing/{t.id}/reconciliar',
                        headers={'Authorization': f'Bearer {jwt_token_admin}'}).get_json()
        assert r['ok'] is False and 'remisión no está identificada' in r['mensaje']
