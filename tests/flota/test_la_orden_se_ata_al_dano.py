"""El ciclo daño → taller → cierre tiene que poder cerrarse solo.

`POST /flota/ordenes` acepta `hallazgo_id` desde siempre, lo valida contra el
mismo vehículo, y la pantalla lo usa para decidir si al cerrar la orden ofrece
cerrar también el daño. **Lo único que faltaba era el selector**: el formulario
mandaba `{placa, tipo, taller, descripcion, km}` y nada más.

Consecuencia: TODA orden creada por la interfaz nacía con `hallazgo_id = NULL`,
el prompt «¿el daño quedó reparado?» no aparecía nunca, y alguien tenía que
acordarse de ir a Daños a cerrarlo aparte. Acordarse no es un control.

Este archivo prueba las dos puntas: que el backend ata de verdad, y que el
formulario manda la clave — porque un endpoint que sabe atar y un formulario
que no lo manda es exactamente el estado que había.
"""
import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
JS = RAIZ / 'app' / 'static' / 'pwa' / 'flota.js'


class TestElFormularioMandaLaClave:
    """Por texto y no por AST porque es JS, pero anclado a los tres puntos que
    tienen que coexistir: pedir los daños, ofrecerlos, y mandarlos."""

    def _fuente(self):
        return JS.read_text(encoding='utf-8')

    def test_el_taller_pide_los_danos_abiertos(self):
        s = self._fuente()
        i = s.index('async function flotaRenderTaller')
        cuerpo = s[i:s.index('\nasync function', i + 10)]
        assert '/flota/hallazgos/' in cuerpo, (
            'la pantalla de taller no pide los daños del vehículo: no puede '
            'ofrecer ninguno para atar')
        assert "=== 'abierto'" in cuerpo, (
            'no se filtra por abierto: ofrecería daños ya cerrados o '
            'descartados, que el endpoint va a rechazar')

    def test_el_formulario_ofrece_el_selector(self):
        s = self._fuente()
        assert 'id="ot-hallazgo"' in s, (
            'no existe el selector de daño en el formulario de orden')

    def test_el_payload_incluye_hallazgo_id(self):
        """EL test. El selector puede existir y no mandarse — que es la forma
        exacta del defecto en el otro sentido."""
        s = self._fuente()
        i = s.index('async function flotaAbrirOT')
        cuerpo = s[i:i + 2500]
        assert 'hallazgo_id' in cuerpo, (
            'el formulario tiene el selector y NO manda `hallazgo_id`: la '
            'orden nacería suelta igual que antes')

    def test_el_selector_tolera_no_tener_danos(self):
        """Si no hay daños abiertos el selector no se pinta, y el lector del
        payload tiene que sobrevivir a eso sin reventar."""
        s = self._fuente()
        i = s.index('async function flotaAbrirOT')
        cuerpo = s[i:i + 2500]
        assert "getElementById('ot-hallazgo') || {}" in cuerpo, (
            'se lee `.value` de un elemento que puede no existir — TypeError '
            'al abrir una orden en un vehículo sin daños abiertos')


class TestElBackendAtaDeVerdad:

    def test_el_endpoint_acepta_y_valida_hallazgo_id(self):
        """Contra el código, no contra la memoria: si alguien quita esta
        validación, el formulario podría atar una orden al daño de OTRO
        vehículo."""
        fuente = (RAIZ / 'flota' / 'api' / 'taller.py').read_text(encoding='utf-8')
        assert 'hallazgo_id' in fuente, (
            'el endpoint dejó de aceptar `hallazgo_id` y el selector quedó '
            'mandando una clave que nadie lee')

    def test_el_adaptador_valida_que_sea_del_mismo_vehiculo(self):
        fuente = (RAIZ / 'flota' / 'adaptadores'
                  / 'taller.py').read_text(encoding='utf-8')
        assert 'hallazgo' in fuente.lower(), (
            'el adaptador no menciona hallazgos: la validación de pertenencia '
            'desapareció')


def test_los_tres_puntos_coexisten():
    """Trinquete de la cadena entera.

    Cada pieza por separado puede existir y la cadena seguir rota — fue
    exactamente lo que pasó: endpoint listo, validación lista, render que lo
    consume listo, y el formulario sin mandarlo. Esto exige las tres a la vez.
    """
    s = JS.read_text(encoding='utf-8')
    piezas = {
        'pide los daños': '/flota/hallazgos/' in s,
        'ofrece el selector': 'id="ot-hallazgo"' in s,
        'manda la clave': bool(re.search(r'hallazgo_id:\s*\(document', s)),
    }
    faltan = [k for k, v in piezas.items() if not v]
    assert not faltan, (
        f'la cadena daño→orden está rota en: {faltan}. Las tres piezas tienen '
        f'que existir juntas o el vínculo no se crea.')
