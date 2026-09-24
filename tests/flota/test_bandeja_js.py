"""La bandeja de flota, EJECUTADA en Node con `util.js` real — no leída.

Un aserto sobre el texto de `flota_bandeja.js` pasa con la pantalla entera
desconectada. Acá corren los cuatro archivos del tab Flota, en el orden de
`index.html`, contra una respuesta sembrada de `/flota/bandeja`, y se mira lo
que quedó pintado, qué se pidió y qué se mandó.

Lo que se verifica, y por qué ninguna es cosmética:

· **Un solo viaje.** La pestaña hacía seis `fetch` en serie y pedía
  `/flota/health` dos veces. Ahora entrar pide `/flota/bandeja` y nada más;
  el health se pide una vez, al abrir Analítica o el Diagnóstico.
· **En un `onclick` solo viajan posiciones.** Una placa o un texto dentro de un
  atributo `onclick` no lo protege `esc()`: el navegador decodifica la entidad
  antes de correr el JS.
· **Decidir un daño es de gestión** y la pantalla lo respeta con lo que dice el
  servidor (`puede_decidir`), no con una lista de roles copiada.
· **Las señales proponen, no culpan**, y la pantalla lo dice encima de ellas.
"""
import json

import pytest

from tests.flota.test_ficha_primera_vez_js import ARCHIVOS_FLOTA, correr


def _fila(pos, placa, color='ambar', **kw):
    f = {
        'pos': pos, 'vehiculo_id': pos + 1, 'placa': placa, 'tipo': 'NHR',
        'capacidad_kg': 3500.0,
        'custodio': {'custodia_id': 40 + pos, 'tipo': 'conductor', 'nombre': 'Ana',
                     'desde': '2026-09-24T11:05:00+00:00',
                     'texto': 'turno de Ana desde hoy a las 06:05',
                     'linea_base': False},
        'donde': {'codigo': 'con_el_conductor',
                  'texto': 'con el conductor (no se registra dónde)'},
        'km': {'valor': 10000, 'confianza': 'declarada', 'ts': None,
               'en_duda': False,
               'texto': '10.000 km · declarado, sin verificar · hoy a las 06:05'},
        'inspeccion': {'estado': 'sin_hacer', 'texto': 'sin inspección hoy',
                       'inspeccion_id': None},
        'rutas_hoy': [], 'ficha': 'completa', 'danos_abiertos': 0,
        'semaforo': {'color': color, 'porque': [
            {'nivel': color, 'texto': f'motivo {color} de {placa}'}]},
        'pendientes': 0, 'senales': 0,
    }
    f.update(kw)
    return f


def _dano(placa='AMB001', hid=7):
    return {'clase': 'dano', 'placa': placa, 'urgencia': 'ambar',
            'texto': 'fuga de aceite', 'detalle': 'mayor · lleva 2 día(s)',
            'accion': {'tipo': 'decidir_dano', 'hallazgo_id': hid,
                       'pestana': 'danos'},
            'desde': None, 'hallazgo_id': hid, 'criticidad': 'mayor',
            'vencido': False, 'dias_abierto': 2, 'aplazado_veces': 0}


def _senal(placa='KMR001'):
    return {'clase': 'km_sin_ruta', 'placa': placa,
            'titulo': '120 km en días sin ruta',
            'texto': 'Entre el 20/09 y el 21/09 el odómetro pasó de 20.300 a 20.420 km.',
            'contexto': 'turno a nombre de Eva',
            'propone': 'Preguntar a qué se fue.',
            'evidencia': {'km_desde': 20300, 'km_hasta': 20420, 'km': 120,
                          'dias': ['2026-09-20', '2026-09-21'],
                          'lectura_desde_id': 3},
            'caso': {'tipo': 'expediente', 'pestana': 'resumen'}}


def _bandeja(**kw):
    b = {
        'dia_operativo': '2026-09-24', 'calculado_ts': '2026-09-24T15:00:00+00:00',
        'filtro': None, 'puede_decidir': True, 'tu_rol': 'admin',
        'hoy': [_fila(0, 'VRD001', 'verde'), _fila(1, 'ROJ001', 'rojo'),
                _fila(2, 'AMB001', 'ambar')],
        'pendientes': [
            _dano(),
            {'clase': 'km_dudoso', 'placa': 'AMB001', 'urgencia': 'ambar',
             'texto': 'kilometraje 30.050 en duda', 'detalle': 'sin foto',
             'accion': {'tipo': 'verificar_km', 'lectura_id': 9},
             'desde': None, 'lectura_id': 9},
            {'clase': 'documento', 'placa': 'ROJ001', 'urgencia': 'rojo',
             'texto': 'SOAT vencido hace 5 día(s)', 'detalle': 'Cargá el nuevo.',
             'accion': {'tipo': 'expediente', 'pestana': 'documentos'},
             'desde': '2026-09-19'},
        ],
        'senales': [_senal()],
        'senales_no_evaluables': [
            {'clase': 'galones', 'placa': 'GAL002', 'casos': 2,
             'motivo': 'el rendimiento del vehículo todavía no se puede sostener'}],
        'umbrales': {'km_tolerancia_sin_ruta': '10', 'min_recorridos_ruta': '5',
                     'factor_km_ruta': '1.5', 'tolerancia_galones': '1.25',
                     'min_tanqueos_precio': '5', 'factor_precio': '1.15',
                     'ventana_senales_dias': '30'},
        'base': 'x',
    }
    b.update(kw)
    return b


def _pintar(tmp_path, pestana='hoy', bandeja=None, operario=None, extra_rutas=None):
    rutas = {'/flota/bandeja': bandeja or _bandeja()}
    rutas.update(extra_rutas or {})
    r = correr(tmp_path, {
        'rutas': rutas,
        'operario': operario or {'rol': 'admin'},
        'globales': {'FLOTA_SUBTAB': pestana},
        'pasos': [{'fn': 'cargarFlota'}],
        'leer': ['flota-contenido', 'flota-n-pendientes', 'flota-n-senales'],
    })
    return r


class TestUnSoloViaje:

    def test_entrar_pide_la_bandeja_y_nada_mas(self, tmp_path):
        """Seis `fetch` en serie y el health dos veces, antes."""
        r = _pintar(tmp_path)
        assert r['pedidas'] == ['/flota/bandeja']

    def test_cambiar_de_pestana_no_vuelve_a_pedir(self, tmp_path):
        r = correr(tmp_path, {
            'rutas': {'/flota/bandeja': _bandeja()},
            'operario': {'rol': 'admin'},
            'pasos': [{'fn': 'cargarFlota'},
                      {'fn': 'flotaSubtab', 'args': ['pendientes']},
                      {'fn': 'flotaSubtab', 'args': ['senales']}],
            'leer': ['flota-contenido'],
        })
        # `cargarFlota` fuerza la lectura; cambiar de pestaña repinta la que
        # ya se leyó. Y el health no aparece.
        assert r['pedidas'] == ['/flota/bandeja']
        assert 'Una señal propone dónde mirar' in r['html']['flota-contenido']

    def test_analitica_pide_el_health_una_sola_vez(self, tmp_path):
        r = correr(tmp_path, {
            'rutas': {'/flota/health': {'documentos_vencidos': 2},
                      '/flota/bandeja': _bandeja()},
            'pasos': [{'fn': 'flotaSubtab', 'args': ['analitica']}],
            'leer': ['flota-analitica'],
        })
        assert r['pedidas'].count('/flota/health') == 1
        # La salud se mudó acá, con el MISMO health.
        assert '2 documento(s) VENCIDOS' in r['html']['flota-analitica']

    def test_un_fallo_se_dice_en_la_pestana(self, tmp_path):
        r = _pintar(tmp_path, bandeja={'__error__': '503 falta flota_hallazgo'})
        html = r['html']['flota-contenido']
        assert 'No se pudo leer la flota' in html
        assert '503 falta flota_hallazgo' in html


class TestHoy:

    def test_una_fila_por_vehiculo_con_semaforo_en_palabras(self, tmp_path):
        html = _pintar(tmp_path)['html']['flota-contenido']
        for placa in ('VRD001', 'ROJ001', 'AMB001'):
            assert placa in html
        assert 'Atender hoy' in html and 'Sin pendientes conocidos' in html
        assert 'turno de Ana desde hoy a las 06:05' in html

    def test_lo_rojo_va_arriba(self, tmp_path):
        html = _pintar(tmp_path)['html']['flota-contenido']
        assert html.index('ROJ001') < html.index('AMB001') < html.index('VRD001')

    def test_la_fila_abre_el_expediente_por_posicion(self, tmp_path):
        html = _pintar(tmp_path)['html']['flota-contenido']
        assert 'onclick="flotaExpediente(1, 0)"' in html
        assert "flotaExpediente('" not in html

    def test_el_dato_va_escapado(self, tmp_path):
        """Un texto de la base no se ejecuta: ni en la placa ni en el porqué."""
        ataque = '<img src=x onerror=alert(1)>'
        b = _bandeja(hoy=[_fila(0, ataque, 'rojo', semaforo={
            'color': 'rojo', 'porque': [{'nivel': 'rojo', 'texto': ataque}]})])
        html = _pintar(tmp_path, bandeja=b)['html']['flota-contenido']
        assert ataque not in html
        assert '&lt;img src=x onerror=alert(1)&gt;' in html

    def test_el_diagnostico_va_plegado_y_no_se_pide_solo(self, tmp_path):
        r = _pintar(tmp_path)
        assert '<details>' in r['html']['flota-contenido']
        assert 'Diagnóstico' in r['html']['flota-contenido']
        assert '/flota/avisos' not in r['pedidas']

    def test_sin_vehiculos_manda_a_la_pestana_que_el_rol_ve(self, tmp_path):
        """control_flota no ve Rutas: mandarlo ahí es darle una instrucción
        imposible (hallazgo 5 del contrato)."""
        admin = _pintar(tmp_path, bandeja=_bandeja(hoy=[]))
        assert 'Rutas → Alta de vehículos' in admin['html']['flota-contenido']
        flota = _pintar(tmp_path, bandeja=_bandeja(hoy=[]),
                        operario={'rol': 'control_flota'})
        assert 'Rutas' not in flota['html']['flota-contenido']
        assert 'un administrador' in flota['html']['flota-contenido']

    def test_los_contadores_de_las_pestanas(self, tmp_path):
        """Por `textContent`: un número nunca pasa por `innerHTML`."""
        r = _pintar(tmp_path)
        assert r['texto']['flota-n-pendientes'] == ' (3)'
        assert r['texto']['flota-n-senales'] == ' (1)'
        assert r['html']['flota-n-pendientes'] == ''


class TestPendientes:

    def test_los_danos_son_una_cola_con_los_tres_botones(self, tmp_path):
        html = _pintar(tmp_path, 'pendientes')['html']['flota-contenido']
        assert 'Daños por decidir (1)' in html
        for boton in ('Reparado', 'Aplazar 7 días', 'No era nada'):
            assert boton in html
        assert 'onclick="flotaBandejaDecidir(0, 2)"' in html

    def test_quien_no_decide_no_ve_los_botones_y_sabe_por_que(self, tmp_path):
        html = _pintar(tmp_path, 'pendientes', bandeja=_bandeja(puede_decidir=False),
                       operario={'rol': 'control_flota'})['html']['flota-contenido']
        assert 'Reparado' not in html and 'No era nada' not in html
        assert 'lo decide gestión' in html
        assert 'Ver daños' in html

    def test_kilometrajes_con_su_boton_de_verificar(self, tmp_path):
        html = _pintar(tmp_path, 'pendientes')['html']['flota-contenido']
        assert 'Kilometrajes por verificar (1)' in html
        assert 'flotaBandejaVerificar()' in html

    def test_papeles_con_su_accion_en_palabras(self, tmp_path):
        html = _pintar(tmp_path, 'pendientes')['html']['flota-contenido']
        assert 'SOAT vencido hace 5 día(s)' in html
        assert 'Cargar papeles' in html

    def test_el_historial_de_turnos_va_plegado(self, tmp_path):
        r = _pintar(tmp_path, 'pendientes')
        assert 'Historial de turnos' in r['html']['flota-contenido']
        assert '/flota/custodia/cierres-forzados' not in r['pedidas']

    def test_sin_pendientes_no_dice_que_todo_esta_bien(self, tmp_path):
        html = _pintar(tmp_path, 'pendientes', bandeja=_bandeja(pendientes=[]))
        html = ' '.join(html['html']['flota-contenido'].split())
        assert 'No quiere decir que la flota esté bien' in html


class TestDecidirDesdeLaCola:

    def _decidir(self, tmp_path, k, prompt):
        return correr(tmp_path, {
            'rutas': {'/flota/bandeja': _bandeja()},
            'operario': {'rol': 'admin'},
            'prompt': prompt,
            'pasos': [{'fn': 'cargarFlota'},
                      {'fn': 'flotaBandejaDecidir', 'args': [0, k]}],
        })

    def test_descartar_manda_el_motivo_a_la_url_del_dano(self, tmp_path):
        r = self._decidir(tmp_path, 2, 'era una mancha vieja')
        post = [e for e in r['enviados'] if e.get('metodo') == 'POST']
        assert len(post) == 1
        assert post[0]['url'].endswith('/flota/hallazgos/7/descartar')
        assert json.loads(post[0]['cuerpo']) == {'motivo': 'era una mancha vieja'}
        # Después de decidir, la bandeja se vuelve a leer: el daño ya no está.
        assert r['pedidas'].count('/flota/bandeja') == 2

    def test_sin_motivo_no_se_manda(self, tmp_path):
        r = self._decidir(tmp_path, 1, '')
        assert not [e for e in r['enviados'] if e.get('metodo') == 'POST']
        assert any('sin razón anotada' in e.get('alerta', '') for e in r['enviados'])

    def test_reparado_va_a_cerrar(self, tmp_path):
        r = self._decidir(tmp_path, 0, 'se cambió el retenedor')
        post = [e for e in r['enviados'] if e.get('metodo') == 'POST']
        assert post[0]['url'].endswith('/flota/hallazgos/7/cerrar')


class TestSenales:

    def test_la_pantalla_dice_que_proponen_no_culpan(self, tmp_path):
        html = ' '.join(_pintar(tmp_path, 'senales')['html']['flota-contenido'].split())
        assert 'no dice que alguien hizo algo mal' in html

    def test_cada_senal_con_evidencia_y_su_caso(self, tmp_path):
        html = _pintar(tmp_path, 'senales')['html']['flota-contenido']
        assert '120 km en días sin ruta' in html
        assert 'turno a nombre de Eva' in html
        assert 'km desde' in html and '20300' in html
        assert 'onclick="flotaSenalAbrir(0)"' in html
        # Los ids internos no son evidencia para una persona.
        assert 'lectura desde id' not in html

    def test_lo_que_no_se_pudo_revisar_se_dice(self, tmp_path):
        html = _pintar(tmp_path, 'senales')['html']['flota-contenido']
        assert 'Lo que no se pudo revisar (1)' in html
        assert 'GAL002' in html and '(2 casos)' in html

    def test_la_vara_se_publica(self, tmp_path):
        html = _pintar(tmp_path, 'senales')['html']['flota-contenido']
        assert 'Con qué vara se juzga' in html
        assert 'km que un vehículo puede moverse en días sin ruta' in html

    def test_sin_senales_manda_a_mirar_lo_no_evaluado(self, tmp_path):
        html = _pintar(tmp_path, 'senales', bandeja=_bandeja(senales=[]))
        html = ' '.join(html['html']['flota-contenido'].split())
        assert '«ninguna señal» puede ser «nada que mirar»' in html


class TestVehiculos:

    def test_el_catalogo_con_su_semaforo(self, tmp_path):
        html = _pintar(tmp_path, 'vehiculos')['html']['flota-contenido']
        assert 'Vehículos activos (3)' in html
        assert '3500' in html and 'ficha completa' in html
        assert 'onclick="flotaExpediente(2, 0)"' in html


class TestExpediente:

    def test_la_barra_tiene_las_ocho_pestanas_por_posicion(self, tmp_path):
        r = correr(tmp_path, {'pasos': [{'fn': 'flotaExpBarraHtml', 'args': [3, 1]}]})
        html = r['pasos'][0]
        for nombre in ('Resumen', 'Daños', 'Gastos', 'Taller', 'Llantas',
                       'Preventivo', 'Documentos', 'Ficha'):
            assert f'>{nombre}</button>' in html
        for k in range(8):
            assert f'flotaExpediente(3, {k})' in html
        assert html.count('btn-flota ok') == 1

    def test_el_resumen_junta_lo_de_ese_vehiculo(self, tmp_path):
        r = correr(tmp_path, {
            'rutas': {'/flota/bandeja': _bandeja()},
            'operario': {'rol': 'admin'},
            'pasos': [{'fn': 'cargarFlota'},
                      {'fn': 'flotaExpediente', 'args': [2, 0]}],
            'leer': ['flota-recibo'],
        })
        html = r['html']['flota-recibo']
        assert 'Quién lo tiene' in html
        assert 'fuga de aceite' in html               # su pendiente
        assert 'SOAT vencido' not in html             # el de otro vehículo
        assert 'Recibo de turno' in html and 'Registrar kilometraje' in html

    def test_una_pestana_reutiliza_el_modal_de_siempre(self, tmp_path):
        """Daños desde el expediente es el mismo `flotaAbrirDanos`."""
        r = correr(tmp_path, {
            'rutas': {'/flota/bandeja': _bandeja(),
                      '/flota/hallazgos/AMB001': {'hallazgos': [], 'abiertos': 0,
                                                  'vencidos': 0}},
            'operario': {'rol': 'admin'},
            'pasos': [{'fn': 'cargarFlota'},
                      {'fn': 'flotaExpediente', 'args': [2, 1]}],
            'leer': ['flota-recibo'],
        })
        assert '/flota/hallazgos/AMB001' in r['pedidas']
        assert 'Reportar un daño' in r['html']['flota-recibo']


class TestDiagnostico:

    def test_traduce_los_codigos_y_trae_los_avisos(self, tmp_path):
        r = correr(tmp_path, {
            'rutas': {'/flota/health': {'custodias_pendiente_sede': 3,
                                        'lecturas_ts_duplicado': 10,
                                        'ambiente': 'datos_de_prueba',
                                        'datos_reales': False},
                      '/flota/avisos': {'avisos': [], 'encendido': False}},
            'pasos': [{'fn': 'flotaBandejaDiagnostico'}],
            'leer': ['flota-diagnostico'],
        })
        html = r['html']['flota-diagnostico']
        assert 'Turnos cerrados en una sede que no está en el maestro' in html
        assert 'pendiente_sede' not in html
        assert 'NO son de la operación real' in html
        assert 'Los avisos están' in html               # el bloque de avisos
        assert r['pedidas'].count('/flota/health') == 1


class TestLaFichaDeUnVehiculoNuevoSeAbreDesdeLaBandeja:
    """El P0 por el camino real: fila → pestaña Ficha → formulario."""

    def test_pestana_ficha_sin_ficha(self, tmp_path):
        r = correr(tmp_path, {
            'rutas': {'/flota/bandeja': _bandeja(),
                      '/ficha': {'placa': 'AMB001', 'existe': False, 'ficha': None,
                                 'atributos_sin_dato': None, 'completa': None}},
            'operario': {'rol': 'admin'},
            'pasos': [{'fn': 'cargarFlota'},
                      {'fn': 'flotaExpediente', 'args': [2, 7]}],
            'leer': ['flota-recibo'],
        })
        assert 'Guardar ficha' in r['html']['flota-recibo']
