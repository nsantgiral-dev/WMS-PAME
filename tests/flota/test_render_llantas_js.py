"""
La pantalla de llantas, EJECUTADA — no leída.

Un aserto de substring sobre el texto de `flota.js` pasa con la función entera
desconectada: ya pasó en este repo con un `|| true` que anuló el pintado y
sobrevivió a la suite. Acá el `flota.js` real corre en Node y se mira **el HTML
que quedó**.

Se prueban dos cosas distintas:

1. **El bloque de salud** — los cuatro campos nuevos del health. Reusa el arnés
   de `test_render_salud_js.py` en vez de copiarlo por cuarta vez (regla 0).
2. **El expediente de llantas** — con el arnés de `test_render_gastos_js.py`,
   porque lo que hay que leer no es el valor devuelto sino lo que quedó en
   `#flota-recibo`.

## Lo que se verifica, y por qué nada de esto es cosmético

· **`vigente` NO se pinta como 0 km.** Una llanta montada con «0 km» aparecería
  como la que menos dura del parque, que es exactamente al revés. Es la regla 4
  en el renglón que alguien lee.
· **`sin_dato` tampoco es cero**, y viene con el motivo y con dónde se arregla.
· **Las posiciones sin llanta no se anuncian como falla mecánica.** Casi siempre
  significan que la llanta está puesta y nadie la registró; un texto que sonara
  a rueda faltante mandaría a alguien a mirar un camión que está bien, y a la
  tercera vez el bloque se deja de leer.
· **El motivo de desmontaje no trae ninguna opción marcada** (regla 1), y
  `desgaste_irregular` sale marcado en la lista porque es la única respuesta que
  no habla de la llanta sino del eje.
· **Ningún umbral de vida útil** (regla 13): con pocas vidas medidas se dice
  cuántas faltan, no un kilometraje de cambio.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.flota.test_render_gastos_js import HARNESS as HARNESS_EXPEDIENTE
from tests.flota.test_render_salud_js import _pintar, _sano

RAIZ = Path(__file__).resolve().parents[2]
FLOTA_JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'


# ══════════════════════════════════════════════════════════════════════════
# 1 — El bloque de salud
# ══════════════════════════════════════════════════════════════════════════

def _llantas(**extra):
    """Una flota sana **y sin una sola llanta registrada**: el estado de hoy."""
    base = {'posiciones_sin_llanta': 0, 'vehiculos_sin_posiciones_llanta': 0,
            'llantas_montadas': 0, 'km_por_posicion': []}
    base.update(extra)
    return _sano(**base)


class TestLasLlantasNoGritanCuandoNoHayNada:

    def test_sin_nada_registrado_no_pinta(self, tmp_path):
        """La disciplina de los otros bloques: un tablero que siempre muestra
        algo se deja de mirar — la lección de los 639 avisos conocidos."""
        assert _pintar(tmp_path, _llantas()) == ''

    def test_un_health_sin_los_campos_nuevos_no_revienta(self, tmp_path):
        """Un despliegue a medias no puede dejar al administrador sin tablero."""
        assert _pintar(tmp_path, _sano()) == ''


class TestLasPosicionesSinLlantaNoSeAnuncianComoFallaMecanica:

    def test_se_ven_y_dicen_lo_que_de_verdad_significan(self, tmp_path):
        html = _pintar(tmp_path, _llantas(posiciones_sin_llanta=10))
        assert '10 posición(es) de llanta sin registrar' in html
        assert 'no es que el camión ande sin rueda' in html.lower()

    def test_los_que_no_se_pudieron_revisar_tienen_su_PROPIA_linea(self, tmp_path):
        """**El campo que impide que el detector se apague en silencio.**

        Un parque entero sin ficha da `posiciones_sin_llanta = 0`, igual que uno
        con las 24 llantas registradas. Sin este renglón, los dos se ven verdes.
        Es la forma exacta de `tanqueos_sin_capacidad_declarada`."""
        html = _pintar(tmp_path, _llantas(vehiculos_sin_posiciones_llanta=2))
        assert '2 vehículo(s) sin ficha' in html
        assert 'no se miraron' in html.lower()

    def test_sin_posiciones_libres_no_se_pinta_esa_linea(self, tmp_path):
        """La otra dirección: un renglón incondicional pasaría el primer test y
        marcaría una flota completa."""
        html = _pintar(tmp_path, _llantas(vehiculos_sin_posiciones_llanta=2))
        assert 'de llanta sin registrar' not in html

    def test_las_montadas_se_cuentan_aparte_y_no_como_porcentaje(self, tmp_path):
        """«0 de 0» (una flota sin ficha) y «0 de 24» (nadie registró nada) dan
        el mismo porcentaje y son los dos estados que hay que distinguir."""
        html = _pintar(tmp_path, _llantas(llantas_montadas=6))
        assert '6 llanta(s) montadas' in html
        assert '%' not in html


class TestLaVidaPorPosicionSePublicaComoHechoYSinUmbral:

    def test_con_pocas_vidas_dice_cuantas_faltan_y_ningun_kilometraje_de_cambio(
            self, tmp_path):
        """Regla 13: **no hay una sola llanta medida en esta flota.** Un «se
        cambia a los X km» escrito hoy sería a ojo, y un detector que dispara
        sobre operación sana se apaga en una semana."""
        html = _pintar(tmp_path, _llantas(km_por_posicion=[
            {'placa': 'TGZ653', 'posicion': 1, 'n': 2, 'km': [38000, 40000],
             'mediana_km': 'sin_dato', 'faltan': 4}]))
        assert 'TGZ653 pos 1: 2 de 6' in html
        assert 'faltan' in html.lower()
        assert 'se cambia a los' not in html.lower()

    def test_con_suficientes_vidas_SI_publica_la_mediana(self, tmp_path):
        """La otra dirección: el bloque no puede negarse para siempre, o el campo
        no sirve para fijar nada."""
        html = _pintar(tmp_path, _llantas(km_por_posicion=[
            {'placa': 'TGZ653', 'posicion': 1, 'n': 6,
             'km': [30000, 32000, 34000, 36000, 38000, 40000],
             'mediana_km': '35000', 'faltan': 0}]))
        assert '35.000 km' in html
        assert 'mediana' in html.lower()

    def test_no_compara_posiciones_entre_si(self, tmp_path):
        """Una direccional y una de tracción no duran lo mismo y la diferencia no
        dice nada — el mismo criterio que el canon del CPK sobre vehículos."""
        html = _pintar(tmp_path, _llantas(km_por_posicion=[
            {'placa': 'TGZ653', 'posicion': 1, 'n': 6, 'km': [1, 2, 3, 4, 5, 6],
             'mediana_km': '3', 'faltan': 0}]))
        assert 'no se compara' in html.lower()
        assert 'promedio' not in html.lower()

    def test_una_lista_vacia_no_pinta_el_renglon(self, tmp_path):
        """`[]` es «todavía no se desmontó una sola llanta», no una vida de
        cero."""
        assert _pintar(tmp_path, _llantas(km_por_posicion=[])) == ''

    def test_ninguna_palabra_imputa_un_delito(self, tmp_path):
        html = _pintar(tmp_path, _llantas(
            posiciones_sin_llanta=9, vehiculos_sin_posiciones_llanta=3,
            llantas_montadas=12)).lower()
        for palabra in ('culpable', 'negligencia', 'sanción', 'robo', 'descuido'):
            assert palabra not in html


# ══════════════════════════════════════════════════════════════════════════
# 2 — El expediente de llantas del vehículo
# ══════════════════════════════════════════════════════════════════════════

def _expediente(tmp_path, payload) -> str:
    """Corre `flotaRenderLlantas()` del `flota.js` real y devuelve lo pintado."""
    if not shutil.which('node'):
        pytest.skip('node no disponible en este entorno')
    h = tmp_path / 'h.mjs'
    h.write_text(HARNESS_EXPEDIENTE, encoding='utf-8')
    g = tmp_path / 'g.json'
    g.write_text(json.dumps({'fn': 'flotaRenderLlantas', 'placa': 'TGZ653',
                             'rutas': {'/flota/llantas/': payload}}),
                 encoding='utf-8')
    proc = subprocess.run(['node', str(h), str(FLOTA_JS), str(g)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f'la pantalla reventó:\n{proc.stderr}'
    return json.loads(proc.stdout)['html']


def _montaje(**extra):
    base = {
        'id': 1, 'llanta_id': 1, 'codigo': 'LL-001', 'marca_llanta': 'Michelin',
        'medida': '215/75R17.5', 'posicion': 1,
        'inicio': '2026-03-01T10:00:00', 'fin': None, 'vigente': True,
        'km_inicio': 1000, 'km_fin': None, 'km': 'vigente',
        'km_marca': 'vigente', 'motivo_desmontaje': None, 'gasto_id': None,
        'observacion': None,
    }
    base.update(extra)
    return base


def _payload(**extra):
    base = {
        'placa': 'TGZ653', 'posiciones_declaradas': 6,
        'posiciones_libres': [], 'montajes': [], 'km_por_posicion': [],
        'disponibles': [],
        'motivos_desmontaje': ['desgaste_normal', 'desgaste_irregular',
                               'pinchazo', 'corte_flanco', 'reencauche',
                               'rotacion', 'sin_dato'],
        'vidas_para_fijar_util': 6,
    }
    base.update(extra)
    return base


class TestLosTresEstadosDelKilometrajeSeVenDistinto:

    def test_una_llanta_montada_NO_dice_cero_km(self, tmp_path):
        """Regla 4 en la línea que alguien lee. Con «0 km» esa llanta aparecería
        como la que menos dura del parque, que es exactamente al revés."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje()]))
        assert 'montada' in html
        assert 'todavía no terminó' in html
        # El cero literal, no cualquier «…0 km» de un odómetro real: `41.000 km`
        # contiene «0 km» y un aserto crudo se volvería un falso positivo — que
        # es cómo un guard con ruido se termina desactivando.
        assert '<b>0 km</b>' not in html

    def test_sin_dato_se_dice_con_palabras_y_con_donde_se_arregla(self, tmp_path):
        """No es cero: es que los dos extremos del tramo son kilometrajes que
        nadie puede respaldar. Y el renglón dice dónde se resuelve, porque un
        `sin dato` sin salida se lee como un defecto del sistema."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000,
            km='sin_dato', km_marca='sin_dato',
            motivo_desmontaje='desgaste_normal')]))
        assert 'sin dato' in html
        assert 'Verificar kilometrajes' in html
        assert '<b>0 km</b>' not in html

    def test_un_numero_sale_con_su_marca_cuando_un_extremo_esta_en_duda(
            self, tmp_path):
        """«Se publica con su marca, nunca como firme»."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000,
            km=40000, km_marca='dudosa', motivo_desmontaje='desgaste_normal')]))
        assert '40.000 km' in html
        assert 'en duda' in html

    def test_un_numero_verificado_sale_limpio(self, tmp_path):
        """La otra dirección: si todo saliera marcado, la marca no distinguiría
        nada."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000,
            km=40000, km_marca='verificada',
            motivo_desmontaje='desgaste_normal')]))
        assert '40.000 km' in html
        assert 'en duda' not in html


class TestElDesgasteIrregularSeSeñalaPorqueNoHablaDeLaLlanta:

    def test_sale_marcado_y_apunta_al_eje(self, tmp_path):
        """Es el modo de fallo caro del plan: no que se gasten, sino que se
        gasten MAL. Sin señalarlo, esa llanta y una que cumplió su vida se ven
        iguales en la lista."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000, km=40000,
            km_marca='verificada', motivo_desmontaje='desgaste_irregular')]))
        assert 'alineación' in html
        assert 'eso no lo explica la llanta' in html.lower()

    def test_un_desgaste_normal_NO_lo_dispara(self, tmp_path):
        """La otra dirección: un aviso incondicional marcaría todas las llantas
        del parque y en una semana nadie lo leería."""
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000, km=40000,
            km_marca='verificada', motivo_desmontaje='desgaste_normal')]))
        assert 'alineación' not in html

    def test_ninguna_palabra_del_expediente_imputa_un_delito(self, tmp_path):
        html = _expediente(tmp_path, _payload(montajes=[_montaje(
            vigente=False, fin='2026-06-01T10:00:00', km_fin=41000, km=40000,
            km_marca='verificada',
            motivo_desmontaje='desgaste_irregular')])).lower()
        for palabra in ('culpable', 'negligencia', 'sanción', 'robo'):
            assert palabra not in html


class TestElFormularioDeDesmontajeNoTraeOpcionMarcada:

    def test_el_desplegable_arranca_vacio(self, tmp_path):
        """Regla 1 aplicada al campo del que depende que el análisis exista: si
        viniera marcado `desgaste_normal`, esa sería la respuesta del 90% y
        `desgaste_irregular` no aparecería nunca en los datos."""
        if not shutil.which('node'):
            pytest.skip('node no disponible en este entorno')
        html = _expediente(tmp_path, _payload(montajes=[_montaje()]))
        # La pantalla de desmontaje se abre desde el botón del montaje vigente.
        assert 'flotaAbrirDesmontaje(1' in html

    def test_los_motivos_salen_del_servidor_y_no_de_una_lista_del_JS(self, tmp_path):
        """Si el JS llevara su propia copia, el día que se agregue un motivo el
        desplegable no lo ofrecería y esa causa de desmontaje no existiría nunca
        en los datos — sin error y sin aviso. Regla 0 con consecuencia."""
        js = FLOTA_JS.read_text(encoding='utf-8')
        # El vocabulario NO está escrito en el JS: solo se lee de la respuesta.
        assert "'corte_flanco'" not in js
        assert 'motivos_desmontaje' in js


class TestLaPantallaDiceLoQueFaltaSinInventarNada:

    def test_las_posiciones_libres_se_explican_como_inventario_incompleto(
            self, tmp_path):
        html = _expediente(tmp_path, _payload(posiciones_libres=[2, 5]))
        assert 'posición 2, 5' in html
        assert 'nadie la registró' in html

    def test_sin_ficha_dice_sin_dato_y_no_todas_cubiertas(self, tmp_path):
        """**El corazón del detector.** Una lista vacía se leería como «están
        todas cubiertas»; lo que pasa es que no se sabe cuántas hay, y un
        vehículo sin ficha saldría limpio para siempre."""
        html = _expediente(tmp_path, _payload(
            posiciones_libres='sin_dato', posiciones_declaradas=None))
        assert 'no tiene ficha técnica' in html
        assert 'no es que estén' in html.lower()

    def test_todas_cubiertas_lo_dice_sin_alarma(self, tmp_path):
        """La otra dirección: un vehículo completo no puede pintar amarillo."""
        html = _expediente(tmp_path, _payload(posiciones_libres=[]))
        assert 'tienen su llanta registrada' in html

    def test_sin_vidas_medidas_lo_dice_y_no_pinta_un_cero(self, tmp_path):
        html = _expediente(tmp_path, _payload(km_por_posicion=[]))
        assert 'no hay con qué medirla' in html
        assert 'No es cero' in html

    def test_el_desplegable_de_montaje_muestra_el_ultimo_motivo(self, tmp_path):
        """Hoy no hay baja de llanta (regla 12), así que una desmontada por corte
        de flanco sigue en la lista. Que quien elige lo vea es lo único que
        impide que vuelva al camión sin que nadie se entere."""
        html = _expediente(tmp_path, _payload(disponibles=[{
            'id': 7, 'codigo': 'LL-007', 'marca': 'Michelin',
            'medida': '215/75R17.5', 'km_acumulado': '40000',
            'km_marca': 'verificada', 'montajes': 1,
            'ultimo_motivo': 'corte_flanco'}]))
        assert 'LL-007' in html
        assert 'salió por corte_flanco' in html
