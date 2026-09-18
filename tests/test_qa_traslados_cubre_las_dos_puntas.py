"""El script de QA de traslados afirma cubrir cada bodega «como origen y como
destino». Este trinquete exige que sea cierto.

No lo era. `CADENA` recorría las 10 bodegas operadas en una cadena **abierta**,
y en una cadena abierta la primera bodega solo se ejercita como ORIGEN y la
última solo como DESTINO. El docstring del script decía lo contrario, y ese
desacuerdo costó caro: la punta que faltaba era **NB1 como destino**, que es
exactamente la dirección que recorre un traslado de averías (punto → CD). Se
construyó el flujo entero sin que ese sentido se hubiera probado nunca contra
Siesa.

Es la forma de `nombre-que-miente`: un identificador —acá, una frase del
docstring— que promete una afirmación y hace otra. No falla; engaña con
confianza.
"""
import ast
import re
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1]
          / 'scripts' / 'qa_traslados_gateway_real.py')


def _cadena():
    """La lista `CADENA` del script, leída por AST.

    Por AST y no importando el módulo: el script tiene efectos de arranque
    (carga `.env.qa`, construye el gateway) y un test no debería dispararlos
    para leer una constante.
    """
    arbol = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    for nodo in arbol.body:
        if isinstance(nodo, ast.Assign):
            for t in nodo.targets:
                if isinstance(t, ast.Name) and t.id == 'CADENA':
                    return ast.literal_eval(nodo.value)
    pytest.fail('No se encontró CADENA en el script')


def _bodegas_operadas():
    """Las bodegas que el WMS opera, desde la fuente del repo."""
    from app.services.inventario_siesa_service import _BODEGAS_PV
    return set(_BODEGAS_PV)


def test_cada_bodega_operada_se_ejercita_como_origen():
    cadena = _cadena()
    origenes = set(cadena[:-1])
    faltan = sorted(_bodegas_operadas() - origenes)
    assert not faltan, (
        f'{faltan} nunca sale como ORIGEN en la cadena de QA. '
        f'Cadena: {" → ".join(cadena)}')


def test_cada_bodega_operada_se_ejercita_como_destino():
    """EL test de este archivo.

    NB1 era la que faltaba, y es la que recibe todos los traslados de averías.
    """
    cadena = _cadena()
    destinos = set(cadena[1:])
    faltan = sorted(_bodegas_operadas() - destinos)
    assert not faltan, (
        f'{faltan} nunca llega como DESTINO en la cadena de QA. '
        f'Si falta NB1, el tramo punto→CD de averías queda sin probar contra '
        f'Siesa. Cadena: {" → ".join(cadena)}')


def test_la_cadena_no_deja_ninguna_bodega_operada_afuera():
    """Un salto que se borre por conveniencia no puede pasar callado."""
    cadena = _cadena()
    faltan = sorted(_bodegas_operadas() - set(cadena))
    assert not faltan, f'{faltan} no aparece en la cadena de QA'


def test_la_cadena_no_visita_bodegas_de_servicio():
    """AV1 y TRA1 no son destino de un traslado ordinario: AV1 no tiene almacén
    WMS ni quién reciba, y TRA1 es un estado contable, no una bodega
    direccionable. Meterlas acá emitiría documentos que Siesa acepta y que
    nadie puede deshacer."""
    from app.services.inventario_siesa_service import _BODEGAS_SERVICIO
    coladas = sorted(set(_cadena()) & set(_BODEGAS_SERVICIO))
    assert not coladas, f'bodegas de servicio en la cadena de QA: {coladas}'


def test_ningun_salto_va_de_una_bodega_a_si_misma():
    """Un traslado de X a X no mueve nada y ensucia el kardex con un par
    STS/ETS que se cancelan."""
    cadena = _cadena()
    bobos = [(a, b) for a, b in zip(cadena, cadena[1:]) if a == b]
    assert not bobos, f'saltos de una bodega a sí misma: {bobos}'


def test_el_docstring_sigue_prometiendo_lo_que_el_test_verifica():
    """Si alguien reescribe el docstring y borra la promesa, este archivo deja
    de tener sujeto — y un trinquete sin sujeto es peor que ninguno, porque
    sigue en verde.
    """
    texto = SCRIPT.read_text(encoding='utf-8')
    encabezado = texto[:texto.index('import')] if 'import' in texto else texto
    assert re.search(r'como\s+origen\s+y\s+como\s+destino', encabezado), (
        'El script ya no promete cubrir cada bodega como origen y como '
        'destino. Si la promesa cambió, este trinquete tiene que cambiar con '
        'ella — no quedarse en verde sobre una afirmación que ya no existe.')
