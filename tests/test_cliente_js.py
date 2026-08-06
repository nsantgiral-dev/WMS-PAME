"""
La lógica de cliente, ejercitada con los payloads reales del servidor.

Los tests de Python verifican dos cosas por separado: que las funciones de la
pantalla EXISTAN, y que el servidor responda las claves que esas funciones leen.
Entre las dos queda una franja sin cubrir — **que la función, alimentada con la
respuesta real, produzca lo correcto** — y ahí vivieron los tres bugs de esta
semana: la placa que se leía de una global mutable, el service worker cacheando
`/flota/`, y los dos rechazos 409 que la pantalla no podía distinguir. Cada
pieza estaba bien; la unión no.

`tests/js/verificar_cliente.js` carga los módulos del PWA en Node con un DOM
mínimo, les da las respuestas que el servidor devuelve de verdad —verificadas
contra PostgreSQL— y comprueba el HTML que producen.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
_GUION = _RAIZ / 'tests' / 'js' / 'verificar_cliente.js'


def test_la_logica_de_cliente_produce_lo_correcto():
    """Si esto falla, el detalle está en la salida: cada línea es un chequeo."""
    if shutil.which('node') is None:
        pytest.fail(
            'node no está instalado. Este test NO se salta: la lógica de '
            'cliente quedaría sin verificar, y un skip en verde es el falso '
            'negativo que ya costó un deploy.')

    r = subprocess.run(['node', str(_GUION)], capture_output=True, text=True,
                       cwd=str(_RAIZ), timeout=60)
    assert r.returncode == 0, (
        '\nFallas en la lógica de cliente:\n' + r.stdout + r.stderr)
    # Que no pase vacío: un guion que no ejecuta nada también sale con 0.
    assert 'verificaciones de cliente OK' in r.stdout, r.stdout
    n = int(r.stdout.split('\n')[-2].strip().split()[0])
    assert n >= 15, f'solo se ejecutaron {n} chequeos — ¿se perdió alguno?'


def test_el_guion_existe_y_carga_los_modulos_reales():
    """Un arnés que cargue una copia no protege nada."""
    fuente = _GUION.read_text(encoding='utf-8')
    assert "/app/static/pwa/compras_ia.js" in fuente
    assert "/app/static/pwa/rutas.js" in fuente
