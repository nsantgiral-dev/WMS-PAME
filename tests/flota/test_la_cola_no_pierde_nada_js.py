"""La cola del conductor no pierde nada — el teléfono, EJECUTADO en Node (T2).

Mismo arnés que `test_mi_camion_hoy_js.py` (`util.js` real, `_condDB` en
memoria, la red guionada). Lo que se afirma, por la auditoría del 2026-09-27:

1. **Un rechazo no borra nada**: la operación queda con sus fotos, marcada, con
   tres salidas (Reintentar · Descartar con confirmación en pantalla · Avisar a
   control de flota). Un rechazo no arrastra lo de atrás, salvo lo de la misma
   placa detrás de un RECIBO rechazado (la dependencia declarada).
2. **Sale sola**: espera creciente tras un intento fallido, el temporizador y
   el regreso de la app al frente; cada envío con tiempo máximo.
3. **Después de sincronizar se vuelve a cargar**, no solo a pintar: la tarjeta
   ya no pide «Recibir» sobre un recibo que entró.
4. **La caché de otro día no se lee como de hoy.**
5. **La placa del turno la manda la cola** (otro camión que el sugerido).
6. **Con la ruta cerrada y el turno abierto, el paso es «Entregar».**
7. **Teléfono compartido**: lo de otra persona no sale con esta sesión, y
   `salir()` avisa.
8. **Un `null` de `_condDB.get` no pisa la cola.**
9. **Las sedes se guardan** para entregar sin señal.
10. **Un solo mensaje por rechazo**, sin «..» ni «Hágalo de nuevo».

Y el trinquete de la clase: *«la cola saca una operación sin que se haya
mandado o sin que el conductor lo decida»* — todo `flotaColaMutar` que filtra
vive en una función del inventario.
"""
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from tests.flota.test_mi_camion_hoy_js import _correr, _tarjeta, _turno, _visible

RAIZ = Path(__file__).resolve().parents[2]
PWA = RAIZ / 'app' / 'static' / 'pwa'


def _op(clave, que='dano', placa='THP696', tipo=None, **extra):
    tipos = {'recibo': 'traspaso', 'entrega': 'traspaso', 'dano': 'hallazgo',
             'inspeccion': 'inspeccion', 'tanqueo': 'tanqueo'}
    o = {'clave': clave, 'tipo': tipo or tipos[que], 'que': que, 'placa': placa,
         'creado': '2026-09-27T10:30:00.000Z', 'intentos': 0, 'ultimo_error': None,
         'cuerpo': {'km': 1, 'clave_idempotencia': clave,
                    'fotos': [{'data_url': 'data:image/jpeg;base64,AAAA'}]}}
    o.update(extra)
    return o


def _cola(ops):
    return f'ALMACEN_INICIAL = {json.dumps(ops)};'


_SEMBRAR = 'await _condDB.set("flota_cola", ALMACEN_INICIAL);'
_DEVOLVER = ' return {cola: await _condDB.get("flota_cola"), envios: __ENVIOS};'


def _sync(tmp_path, ops, respuestas, antes='', despues=''):
    return _correr(tmp_path, f'(async () => {{ {_SEMBRAR} {antes} await flotaColaSincronizar();'
                             f' {despues} {_DEVOLVER} }})()',
                   semilla=_cola(ops), respuestas=respuestas)


_OK = {'status': 201, 'json': {'id': 1}}
_409 = {'status': 409, 'json': {'error': 'el odómetro no puede decrecer'}}


# ═══════════════════════════════════════════════════════════════════════════
# 1 · Un rechazo no borra nada, y no arrastra lo que no depende
# ═══════════════════════════════════════════════════════════════════════════

class TestUnRechazoNoBorraNiArrastra:

    def test_lo_que_viene_detras_de_un_rechazo_sigue(self, tmp_path):
        s = _sync(tmp_path, [_op('k1', 'inspeccion'), _op('k2', 'dano'),
                             _op('k3', 'tanqueo')], [_409, _OK, _OK])
        assert [e['body']['clave_idempotencia'] for e in s['envios']] == ['k1', 'k2', 'k3']
        (op,) = s['cola']
        assert op['clave'] == 'k1' and op['rechazo']

    def test_detras_de_un_recibo_rechazado_espera_lo_de_esa_placa(self, tmp_path):
        """La única dependencia declarada: sin turno, el servidor rechazaría en
        cascada todo lo de esa placa. Lo de OTRA placa sale."""
        s = _sync(tmp_path, [_op('r1', 'recibo'), _op('d1', 'dano'),
                             _op('t1', 'tanqueo', placa='TGZ653')], [_409, _OK])
        assert [e['body']['clave_idempotencia'] for e in s['envios']] == ['r1', 't1']
        assert [o['clave'] for o in s['cola']] == ['r1', 'd1']
        assert s['cola'][1].get('rechazo') is None, 'lo que espera no es un rechazo'

    def test_un_recibo_nuevo_de_esa_placa_levanta_la_espera(self, tmp_path):
        rechazado = _op('r1', 'recibo', rechazo={'mensaje': 'x', 'ayuda': False})
        s = _sync(tmp_path, [rechazado, _op('r2', 'recibo'), _op('d1', 'dano')], [_OK, _OK])
        assert [e['body']['clave_idempotencia'] for e in s['envios']] == ['r2', 'd1']
        assert [o['clave'] for o in s['cola']] == ['r1']

    def test_un_rechazado_no_se_manda_solo(self, tmp_path):
        s = _sync(tmp_path, [_op('k1', rechazo={'mensaje': 'x', 'ayuda': False})], [])
        assert s['envios'] == []


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Las tres salidas de un rechazo
# ═══════════════════════════════════════════════════════════════════════════

def _con_tarjeta(expr):
    """Pinta la tarjeta (llena `FLOTA_COLA_VISTA`) y después ejecuta `expr`."""
    return (f'(async () => {{ {_SEMBRAR} FLOTA_COLA_ITEMS = await _condDB.get("flota_cola");'
            f' FLOTA_COND = {json.dumps(_turno(tiene_turno_abierto=True))};'
            f' flotaCondTarjetaHTML(FLOTA_COND, FLOTA_COLA_ITEMS, []); {expr}'
            f' {_DEVOLVER} }})()')


class TestLasTresSalidas:
    RECH = _op('k1', rechazo={'mensaje': 'el odómetro no puede decrecer.', 'ayuda': False})

    def test_reintentar_lo_vuelve_a_mandar_con_la_misma_clave(self, tmp_path):
        s = _correr(tmp_path, _con_tarjeta('await flotaColaReintentar(0);'),
                    semilla=_cola([self.RECH]), respuestas=[_OK],
                    get={'/flota/conductor/mi-turno': _turno(tiene_turno_abierto=True)})
        assert s['envios'][0]['body']['clave_idempotencia'] == 'k1'
        assert s['cola'] == []

    def test_descartar_pregunta_en_pantalla_y_avisa(self, tmp_path):
        s = _correr(tmp_path, _con_tarjeta('await flotaColaDescartar(0);'),
                    semilla=_cola([self.RECH]), respuestas=[{'status': 200, 'json': {}}])
        assert s['cola'] == []
        (aviso,) = s['envios']
        assert aviso['url'] == '/flota/conductor/rechazos/k1'
        assert aviso['body']['accion'] == 'descartar'
        assert aviso['body']['operacion'] == 'hallazgo'

    def test_si_no_confirma_no_se_borra(self, tmp_path):
        s = _correr(tmp_path, _con_tarjeta('await flotaColaDescartar(0);'),
                    semilla=_cola([self.RECH]) + 'confirm = () => false;')
        assert [o['clave'] for o in s['cola']] == ['k1'] and s['envios'] == []

    def test_descartar_no_usa_el_dialogo_del_sistema(self):
        """Sin diálogos nativos: `_modalConfirmar`, nunca `confirm(`."""
        js = (PWA / 'flota.js').read_text(encoding='utf-8')
        i = js.index('async function flotaColaDescartar(')
        cuerpo = js[i:js.index('\n}\n', i)]
        assert '_modalConfirmar(' in cuerpo and not re.search(r'(?<![\w.])confirm\(', cuerpo)

    def test_avisar_a_control_de_flota(self, tmp_path):
        s = _correr(tmp_path, _con_tarjeta(
            'await flotaColaPedirAyuda(0);'
            ' const html = flotaCondTarjetaHTML(FLOTA_COND, FLOTA_COLA_ITEMS, []);'
            ' __ENVIOS.push({html});'),
            semilla=_cola([self.RECH]), respuestas=[{'status': 200, 'json': {}}])
        (op,) = s['cola']
        assert op['rechazo']['ayuda'] is True
        assert s['envios'][0]['body']['accion'] == 'ayuda'
        assert 'Control de flota ya fue avisado' in _visible(s['envios'][-1]['html'])

    def test_sin_senal_el_aviso_se_guarda_y_sale_despues(self, tmp_path):
        s = _correr(tmp_path, _con_tarjeta(
            'await flotaColaPedirAyuda(0);'
            ' const guardados = await _condDB.get("flota_cola_avisos");'
            ' await flotaColaEnviarAvisos();'
            ' __ENVIOS.push({guardados, quedan: await _condDB.get("flota_cola_avisos")});'),
            semilla=_cola([self.RECH]), respuestas=['SIN_RED', {'status': 200, 'json': {}}])
        fin = s['envios'][-1]
        assert len(fin['guardados']) == 1 and fin['quedan'] == []


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Lo que se lee en la tarjeta
# ═══════════════════════════════════════════════════════════════════════════

class TestLaTarjetaLoDice:

    def test_el_rechazo_trae_su_motivo_y_sus_tres_botones(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True),
                        cola=[_op('k1', rechazo={'mensaje': 'No puede registrar el daño '
                                                'sobre el THP696: no está en su turno.',
                                                'ayuda': False})])
        v = _visible(html)
        assert 'No se pudo registrar el daño del THP696' in v
        assert 'Sigue guardado en el teléfono' in v
        for b in ('Reintentar', 'Descartar', 'Avisar a control de flota'):
            assert b in v
        # En los onclick solo viajan posiciones.
        for m in re.findall(r'onclick="(flotaCola\w+)\(([^)]*)\)"', html):
            assert re.fullmatch(r'\d*', m[1]), m

    def test_un_solo_mensaje_sin_punto_doble_ni_hagalo_de_nuevo(self, tmp_path):
        """«…reciba el turno primero.. No se pudo registrar: pídaselo al
        encargado de flota. Hágalo de nuevo.» — el texto de antes."""
        s = _correr(tmp_path, 'flotaTextoRechazo(403, {error: "No puede registrar el daño sobre '
                              'el THP696: no está en su turno. Reciba el turno primero.", '
                              'motivo: "sin_derecho"})')
        assert '..' not in s and 'Hágalo de nuevo' not in s
        assert s.count('No se pudo registrar') == 0 and s.endswith('.')
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True),
                        cola=[_op('k1', rechazo={'mensaje': s, 'ayuda': False})])
        v = _visible(html)
        assert v.count('No se pudo registrar') == 1 and '..' not in v

    def test_el_item_trabado_dice_sus_intentos_y_su_ultimo_error(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True),
                        cola=[_op('k1', intentos=4,
                                  ultimo_error='El servidor no respondió bien.')])
        v = _visible(html)
        assert '4 intentos' in v and 'El servidor no respondió bien' in v

    def test_lo_que_espera_al_recibo_lo_dice(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(),
                        cola=[_op('r1', 'recibo', rechazo={'mensaje': 'x.', 'ayuda': False}),
                              _op('d1', 'dano')])
        assert 'en espera del recibo' in _visible(html)

    def test_el_mensaje_del_servidor_llega_escapado(self, tmp_path):
        html = _tarjeta(tmp_path, _turno(tiene_turno_abierto=True),
                        cola=[_op('k1', rechazo={'mensaje': '<img src=x onerror=alert(1)>',
                                                'ayuda': False})])
        assert '<img src=x' not in html and '&lt;img' in html


# ═══════════════════════════════════════════════════════════════════════════
# 4 · Sale sola: espera creciente, temporizador, regreso al frente, tiempo máximo
# ═══════════════════════════════════════════════════════════════════════════

class TestSaleSola:

    def test_tras_un_intento_sin_senal_espera_y_no_insiste_antes(self, tmp_path):
        s = _correr(tmp_path, (
            f'(async () => {{ {_SEMBRAR} await flotaColaSincronizar();'
            ' const espera = FLOTA_COLA_ESPERA_HASTA - Date.now();'
            ' await flotaColaTick();'
            ' return {espera, fallos: FLOTA_COLA_FALLOS, envios: __ENVIOS.length}; })()'),
            semilla=_cola([_op('k1')]), respuestas=['SIN_RED'])
        assert s['fallos'] == 1 and 10000 < s['espera'] <= 15000
        assert s['envios'] == 1, 'insistió antes de la espera'

    def test_la_espera_crece(self, tmp_path):
        s = _correr(tmp_path, (
            f'(async () => {{ {_SEMBRAR} await flotaColaSincronizar();'
            ' await flotaColaSincronizar(); await flotaColaSincronizar();'
            ' return FLOTA_COLA_ESPERA_HASTA - Date.now(); })()'),
            semilla=_cola([_op('k1')]), respuestas=['SIN_RED'] * 3)
        assert 50000 < s <= 60000

    def test_el_tick_con_ahora_no_espera_y_al_salir_todo_la_espera_vuelve_a_cero(
            self, tmp_path):
        s = _correr(tmp_path, (
            f'(async () => {{ {_SEMBRAR} await flotaColaSincronizar();'
            ' await flotaColaTick(true);'
            ' return {cola: await _condDB.get("flota_cola"), espera: FLOTA_COLA_ESPERA_HASTA,'
            ' fallos: FLOTA_COLA_FALLOS}; })()'),
            semilla=_cola([_op('k1')]), respuestas=['SIN_RED', _OK],
            get={'/flota/conductor/mi-turno': _turno()})
        assert s['cola'] == [] and s['espera'] == 0 and s['fallos'] == 0

    def test_un_envio_que_no_responde_se_corta(self, tmp_path):
        semilla = (_cola([_op('k1')]) +
                   'flotaColaTiempoMaximoMs = () => 30;'
                   'fetch = (url, opts) => new Promise((res, rej) => {'
                   '  opts.signal.addEventListener("abort", () => {'
                   '    const e = new Error("abort"); e.name = "AbortError"; rej(e); }); });')
        s = _correr(tmp_path, f'(async () => {{ {_SEMBRAR} const r = await flotaColaSincronizar();'
                              ' return r.k1; })()', semilla=semilla)
        assert s['estado'] == 'sin_red' and 'a tiempo' in s['mensaje']

    def test_el_tiempo_maximo_crece_con_lo_que_pesa(self, tmp_path):
        s = _correr(tmp_path, '[flotaColaTiempoMaximoMs("x"), '
                              'flotaColaTiempoMaximoMs("x".repeat(1600000)), '
                              'flotaColaTiempoMaximoMs("x".repeat(90000000))]')
        assert s[0] < 21000 and 90000 < s[1] < 110000 and s[2] == 180000

    def test_escucha_el_regreso_al_frente_y_pide_no_ser_borrada(self, tmp_path):
        semilla = ('var __OIDOS = {}; var __PERSISTE = false;'
                   'document.addEventListener = (ev, fn) => { __OIDOS[ev] = fn; };'
                   'navigator.storage = {persist: () => { __PERSISTE = true; return Promise.resolve(true); }};')
        s = _correr(tmp_path, '(() => { flotaColaEscucharRed();'
                              ' return {oye: Object.keys(__OIDOS), persiste: __PERSISTE}; })()',
                    semilla=semilla)
        assert 'visibilitychange' in s['oye'] and s['persiste'] is True

    def test_el_temporizador_del_conductor_reintenta_la_cola(self):
        """`app.js`: el mismo `setInterval` de la pantalla del conductor llama
        `flotaColaTick`. Texto acotado a la rama del conductor."""
        js = (PWA / 'app.js').read_text(encoding='utf-8')
        i = js.index("pantalla('pantalla-conductor');")
        rama = js[i:js.index('} else if (esAdmin)', i)]
        assert re.search(r'setInterval\(\(\) => \{[^}]*flotaColaTick\(\)', rama), rama


# ═══════════════════════════════════════════════════════════════════════════
# 5 · Después de sincronizar, se vuelve a CARGAR
# ═══════════════════════════════════════════════════════════════════════════

class TestDespuesDeSincronizarSeRecarga:

    def test_no_vuelve_a_pedir_recibir_un_camion_que_ya_entro(self, tmp_path):
        """El `mi-turno` de ANTES decía sin turno; el recibo entra al
        sincronizar; el de DESPUÉS dice turno abierto. La tarjeta final tiene
        que ser la de después."""
        antes = _turno(tiene_turno_abierto=False)
        despues = _turno(tiene_turno_abierto=True)
        semilla = (_cola([_op('r1', 'recibo')]) +
                   f'var __TURNOS = [{json.dumps(antes)}, {json.dumps(despues)}];'
                   'get = async () => JSON.parse(JSON.stringify(__TURNOS.length > 1'
                   ' ? __TURNOS.shift() : __TURNOS[0]));')
        s = _correr(tmp_path, (
            f'(async () => {{ {_SEMBRAR} await flotaCondCargar();'
            ' while (FLOTA_COLA_SINCRONIZANDO) await FLOTA_COLA_SINCRONIZANDO;'
            ' await new Promise(r => setTimeout(r, 20));'
            ' return document.getElementById("cond-flota").innerHTML; })()'),
            semilla=semilla, respuestas=[{'status': 201, 'json': {'custodia_id': 3}}])
        v = _visible(s)
        assert 'Recibir el camión' not in v, 'repintó con el estado de antes de sincronizar'
        assert 'Inspeccionar el camión' in v


# ═══════════════════════════════════════════════════════════════════════════
# 6 · La caché de otro día; la placa del turno; entregar
# ═══════════════════════════════════════════════════════════════════════════

def _ayer():
    from app.utils.fecha import dia_operativo
    return (dia_operativo() - timedelta(days=1)).isoformat()


class TestLaCacheDeAyerNoEsDeHoy:

    def test_de_madrugada_sin_senal_no_dice_dia_cerrado(self, tmp_path):
        d = _turno(dia=_ayer(), ruta_de_hoy_cerrada=True, guardado_a_las=True)
        v = _visible(_tarjeta(tmp_path, d))
        assert 'Día cerrado' not in v and 'Recibir el camión' in v
        assert 'lo último que se supo es del' in v

    def test_la_inspeccion_de_ayer_no_cuenta_hoy(self, tmp_path):
        d = _turno(dia=_ayer(), tiene_turno_abierto=True, guardado_a_las=True,
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        assert 'Inspeccionar el camión' in _visible(_tarjeta(tmp_path, d))

    def test_sin_dia_tampoco_es_de_hoy(self, tmp_path):
        d = _turno(tiene_turno_abierto=True, guardado_a_las=True,
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        del d['dia']
        v = _visible(_tarjeta(tmp_path, d))
        assert 'Inspeccionar el camión' in v and 'un día que no se sabe' in v

    def test_la_de_hoy_si_cuenta(self, tmp_path):
        d = _turno(tiene_turno_abierto=True,
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        assert 'Inspeccionar el camión' not in _visible(_tarjeta(tmp_path, d))


class TestLaPlacaDelTurnoLaMandaLaCola:

    def test_recibio_otro_camion_sin_senal(self, tmp_path):
        """El servidor sugiere THP696 (y dice que ya se inspeccionó); el
        conductor recibió TGZ653 sin señal. La tarjeta es de TGZ653 y pide
        inspeccionarlo."""
        d = _turno(inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        v = _visible(_tarjeta(tmp_path, d, cola=[_op('r1', 'recibo', placa='TGZ653')]))
        assert 'TGZ653' in v and 'Inspeccionar el camión' in v

    def test_el_dano_sale_con_la_placa_del_turno(self, tmp_path):
        s = _correr(tmp_path, '(async () => { await flotaCondReportarDano(); return FLOTA_PLACA; })()',
                    semilla=(f'FLOTA_COND = {json.dumps(_turno())};'
                             f'FLOTA_COLA_ITEMS = {json.dumps([_op("r1", "recibo", placa="TGZ653")])};'))
        assert s == 'TGZ653'

    def test_un_recibo_rechazado_no_es_turno(self, tmp_path):
        v = _visible(_tarjeta(tmp_path, _turno(), cola=[
            _op('r1', 'recibo', placa='TGZ653', rechazo={'mensaje': 'x.', 'ayuda': False})]))
        assert 'Recibir el camión' in v


class TestConLaRutaCerradaElPasoEsEntregar:

    def test_turno_abierto_y_ruta_cerrada(self, tmp_path):
        d = _turno(tiene_turno_abierto=True, ruta_de_hoy_cerrada=True,
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        html = _tarjeta(tmp_path, d)
        assert 'Entregar el camión' in _visible(html)
        assert 'flotaCondAbrirEntrega()' in html

    def test_con_la_ruta_abierta_no(self, tmp_path):
        d = _turno(tiene_turno_abierto=True,
                   inspeccion={'hecha': True, 'veredicto': 'apto', 'habilita_despacho': True})
        assert 'Entregar el camión' not in _visible(_tarjeta(tmp_path, d))


# ═══════════════════════════════════════════════════════════════════════════
# 7 · Teléfono compartido
# ═══════════════════════════════════════════════════════════════════════════

class TestTelefonoCompartido:

    def test_lo_de_otra_persona_no_sale_con_esta_sesion(self, tmp_path):
        s = _correr(tmp_path, f'(async () => {{ {_SEMBRAR} await flotaColaSincronizar(); {_DEVOLVER} }})()',
                    semilla=_cola([_op('ajena', usuario_id=9), _op('mia', usuario_id=5)])
                    + 'OPERARIO = {id: 5};', respuestas=[_OK])
        assert [e['body']['clave_idempotencia'] for e in s['envios']] == ['mia']
        assert [o['clave'] for o in s['cola']] == ['ajena']

    def test_la_tarjeta_lo_dice(self, tmp_path):
        html = _correr(tmp_path, f'flotaCondTarjetaHTML({json.dumps(_turno())}, '
                                 f'{json.dumps([_op("ajena", usuario_id=9)])}, [])',
                       semilla='OPERARIO = {id: 5};')
        assert 'de otra persona' in _visible(html)

    def test_lo_nuevo_se_encola_a_nombre_de_quien_esta(self, tmp_path):
        s = _correr(tmp_path, '(async () => { await flotaColaRegistrar("hallazgo", "dano", "THP696",'
                              ' {placa: "THP696"}); return (await _condDB.get("flota_cola"))[0]; })()',
                    semilla='OPERARIO = {id: 5};', respuestas=['SIN_RED'])
        assert s['usuario_id'] == 5

    def test_salir_pregunta_si_quedan_pendientes(self, tmp_path):
        s = _correr(tmp_path, f'(async () => {{ {_SEMBRAR} return [await flotaColaAntesDeSalir(),'
                              ' await flotaColaPendientesDelUsuario()]; })()',
                    semilla=_cola([_op('mia', usuario_id=5)]) + 'OPERARIO = {id: 5}; confirm = () => false;')
        assert s == [False, 1]

    def test_sin_pendientes_sale_sin_preguntar(self, tmp_path):
        s = _correr(tmp_path, '(async () => await flotaColaAntesDeSalir())()',
                    semilla='OPERARIO = {id: 5}; confirm = () => { throw new Error("preguntó"); };')
        assert s is True

    def test_salir_de_app_js_pasa_por_la_pregunta(self, tmp_path):
        """`salir()` ejecutado de verdad (sacado de app.js): con pendientes y
        «Quedarme», no cierra la sesión; una sesión vencida no pregunta."""
        js = (PWA / 'app.js').read_text(encoding='utf-8')
        fuentes = []
        for nombre in ('function salir(', 'function _salirYa('):
            i = js.index(nombre)
            fuentes.append(js[i:js.index('\n}\n', i) + 2])
        semilla = ('var __SALIO = []; var TAREA_ACTUAL = null;'
                   'var pararTimers = () => {}; var pantalla = (p) => __SALIO.push(p);'
                   'flotaColaAntesDeSalir = async () => false;\n' + '\n'.join(fuentes)
                   + '\nvar TOKEN = "t"; OPERARIO = {id: 5};')
        s = _correr(tmp_path, '(async () => { salir(); await new Promise(r => setTimeout(r, 5));'
                              ' const quedo = __SALIO.length; salir(true);'
                              ' return [quedo, __SALIO.length]; })()', semilla=semilla)
        assert s == [0, 1]


# ═══════════════════════════════════════════════════════════════════════════
# 8 · Un `null` de `_condDB.get` no pisa la cola
# ═══════════════════════════════════════════════════════════════════════════

class TestUnaLecturaFallidaNoPisaLaCola:

    SEMILLA = ('var __LEER_MAL = true; var __get = _condDB.get;'
               '_condDB.get = async (k) => (k === "flota_cola" && __LEER_MAL) ? null : __get(k);')

    def test_registrar_no_pisa_lo_que_habia(self, tmp_path):
        s = _correr(tmp_path, (
            f'(async () => {{ {_SEMBRAR} FLOTA_COLA_ITEMS = ALMACEN_INICIAL;'
            ' const r = await flotaColaRegistrar("hallazgo", "dano", "THP696", {placa: "THP696"});'
            ' __LEER_MAL = false; return {r, cola: await _condDB.get("flota_cola")}; })()'),
            semilla=_cola([_op('vieja')]) + self.SEMILLA, respuestas=['SIN_RED'])
        assert [o['clave'] for o in s['cola']] == ['vieja'], 'la escritura pisó la cola'
        assert s['r']['estado'] == 'perdido', 'fingió que guardó'

    def test_si_sigue_vacia_el_telefono_la_borro_y_se_dice(self, tmp_path):
        s = _correr(tmp_path, (
            '(async () => { FLOTA_COLA_ITEMS = [{clave: "a"}, {clave: "b"}];'
            ' for (let i = 0; i < 3; i++) { try { await flotaColaLeer(); } catch (_) {} }'
            ' return {perdidos: FLOTA_COLA_PERDIDOS,'
            ' html: flotaCondTarjetaHTML(null, [], [])}; })()'),
            semilla=self.SEMILLA)
        assert s['perdidos'] == 2
        assert 'El teléfono borró' in _visible(s['html'])


# ═══════════════════════════════════════════════════════════════════════════
# 9 · Las sedes, guardadas
# ═══════════════════════════════════════════════════════════════════════════

class TestLasSedesSeGuardan:
    SEDES = [{'id': 4, 'codigo': 'NB1', 'nombre': 'Neiva CD'}]

    def test_con_senal_se_guardan_y_sin_senal_se_usan(self, tmp_path):
        s = _correr(tmp_path, (
            '(async () => { await flotaLlenarSedes("sel");'
            ' get = async () => { throw new TypeError("Failed to fetch"); };'
            ' await flotaLlenarSedes("sel2");'
            ' return {html: document.getElementById("sel2").innerHTML, alertas: __ALERTAS}; })()'),
            get={'/api/almacenes/': self.SEDES})
        assert 'NB1 · Neiva CD' in s['html'] and 'lista guardada el' in s['html']
        assert s['alertas'] == []

    def test_sin_lista_guardada_se_dice_sin_el_error_crudo(self, tmp_path):
        s = _correr(tmp_path, '(async () => { await flotaLlenarSedes("sel"); return __ALERTAS; })()')
        (texto, tipo), = s
        assert 'Failed to fetch' not in texto and 'Sin señal' in texto
        assert tipo == 'advertencia'


# ═══════════════════════════════════════════════════════════════════════════
# 10 · TRINQUETE — nada sale de la cola sin mandarse o sin que alguien lo decida
# ═══════════════════════════════════════════════════════════════════════════

#: Las funciones que pueden SACAR una operación de la cola, con su porqué.
SACAN_DE_LA_COLA = {
    'flotaColaSincronizar': 'se mandó y el servidor la tiene (201, o 200 «ya la tenía»)',
    'flotaColaRegistrar': 'sin almacén se manda directo; y el corregido reemplaza al '
                          'rechazado que mandó el MISMO formulario',
    'flotaColaDescartar': 'el conductor lo decidió, con confirmación en pantalla',
}


def _sin_comentarios_ni_textos(js: str) -> str:
    """El JS sin comentarios ni el contenido de las cadenas (las de plantilla
    conservan sus `${…}`, que son código). Un detector que leyera comentarios
    se atraparía en su propio docstring."""
    out, i, n = [], 0, len(js)
    pila = []                       # ` abiertos y profundidad de ${ dentro
    while i < n:
        c = js[i]
        if pila and pila[-1] == '`':
            if c == '\\':
                i += 2
                continue
            if c == '`':
                pila.pop(); out.append('`'); i += 1
                continue
            if js.startswith('${', i):
                pila.append('{'); out.append('${'); i += 2
                continue
            i += 1
            continue
        if js.startswith('//', i):
            j = js.find('\n', i)
            i = n if j < 0 else j
            continue
        if js.startswith('/*', i):
            j = js.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in '\'"':
            j = i + 1
            while j < n and js[j] != c:
                j += 2 if js[j] == '\\' else 1
            out.append(c + c); i = j + 1
            continue
        if c == '`':
            pila.append('`'); out.append('`'); i += 1
            continue
        if c == '{' and pila:
            pila.append('{')
        elif c == '}' and pila and pila[-1] == '{':
            pila.pop()
            if pila and pila[-1] == '`':
                out.append('}'); i += 1
                continue
        out.append(c); i += 1
    return ''.join(out)


def _funciones(js: str) -> dict:
    """{nombre: cuerpo} de toda `function nombre(` de nivel superior o no."""
    limpio = _sin_comentarios_ni_textos(js)
    salida = {}
    for m in re.finditer(r'(?:async\s+)?function\s+(\w+)\s*\(', limpio):
        i = limpio.index('{', m.end())
        prof, j = 0, i
        while j < len(limpio):
            if limpio[j] == '{':
                prof += 1
            elif limpio[j] == '}':
                prof -= 1
                if prof == 0:
                    break
            j += 1
        salida[m.group(1)] = limpio[i:j + 1]
    return salida


def quienes_sacan(js: str) -> set:
    """Funciones que llaman `flotaColaMutar(` con un `.filter(` adentro, o que
    escriben la cola directo (`_condDB.set(FLOTA_COLA_LLAVE`) fuera de
    `flotaColaMutar`."""
    salida = set()
    for nombre, cuerpo in _funciones(js).items():
        for m in re.finditer(r'flotaColaMutar\(', cuerpo):
            prof, j = 1, m.end()
            while j < len(cuerpo) and prof:
                prof += {'(': 1, ')': -1}.get(cuerpo[j], 0)
                j += 1
            if '.filter(' in cuerpo[m.end():j]:
                salida.add(nombre)
        if nombre != 'flotaColaMutar' and re.search(r'_condDB\.set\(\s*FLOTA_COLA_LLAVE\b', cuerpo):
            salida.add(nombre)
    return salida


class TestNadaSaleDeLaColaSinDecidirlo:

    def _js(self):
        return (PWA / 'flota.js').read_text(encoding='utf-8')

    def test_solo_las_del_inventario(self):
        assert quienes_sacan(self._js()) == set(SACAN_DE_LA_COLA), (
            'una función nueva saca operaciones de la cola: o se manda, o lo '
            'decide el conductor — declárela con su porqué o no la saque')

    def test_cada_una_dice_por_que(self):
        for nombre, porque in SACAN_DE_LA_COLA.items():
            assert len(porque) >= 30, nombre

    def test_piso_el_escaner_ve_las_funciones_de_la_cola(self):
        f = _funciones(self._js())
        assert {'flotaColaSincronizar', 'flotaColaMutar', 'flotaColaRegistrar',
                'flotaColaDescartar', 'flotaColaReintentar'} <= set(f)
        assert len(f) > 150

    def test_el_detector_ve_las_dos_formas_y_no_lo_sano(self):
        malo = ('async function borraEnSilencio(op) {\n'
                '  await flotaColaMutar(l => l.filter(x => x.clave !== op.clave));\n}\n'
                'async function pisaLaCola() { await _condDB.set(FLOTA_COLA_LLAVE, []); }\n')
        sano = ('async function marca(op) {\n'
                '  // await flotaColaMutar(l => l.filter(x => 1));\n'
                '  const t = "flotaColaMutar(l => l.filter(x => 1))";\n'
                '  const h = `${op.a} flotaColaMutar(l => l.filter(x => 1))`;\n'
                '  await flotaColaMutar(l => l.map(x => x));\n}\n')
        assert quienes_sacan(malo) == {'borraEnSilencio', 'pisaLaCola'}
        assert quienes_sacan(sano) == set()
