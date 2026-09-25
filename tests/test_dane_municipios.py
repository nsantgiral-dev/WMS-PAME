"""
La tabla DANE del WMS se cruza contra la DIVIPOLA oficial, código por código.

Hasta el 2026-09-25 `app/utils/dane_municipios.py` estaba escrita a mano y
**corrida en tres departamentos**: al Huila le faltaban Acevedo y Agrado al
principio, y cada nombre quedó pegado al código del siguiente. El PD20822 de
Siesa va a Campoalegre (ciudad 169-41-132) y el Monitor de Muelle lo agrupaba
en «Colombia» — otro municipio del Huila, el 41206. Pitalito salía como Rivera,
Garzón como Gigante, La Plata como Nátaga. Santander y Sucre tenían la misma
forma, más códigos intercambiados en Boyacá, Caldas y Norte de Santander, y
dos letras cirílicas dentro de «Entrerríos».

Nada lo detectaba: la tabla tenía 1.117 entradas, «casi» las 1.122, y cada
nombre era un municipio real — solo que de otro código.

La fuente es `docs/divipola_minsalud_2026-09.csv`, bajada de datos.gov.co
(dataset `pqwj-3fi4`, «MinSalud Divipola - Municipios») el 2026-09-25. Trae las
tildes dañadas (`Bogot� D.C.`), así que el cruce trata el carácter dañado
como comodín y compara sin tildes.
"""
import csv
import re
import unicodedata
from pathlib import Path

import pytest

from app.utils.dane_municipios import DANE, resolver_municipio

FUENTE = Path(__file__).resolve().parent.parent / 'docs' / 'divipola_minsalud_2026-09.csv'

#: Nombres que el WMS escribe distinto a la fuente, a propósito. Mismo
#: municipio, mismo código: solo cambia la forma. Cada uno dice por qué.
NOMBRE_DISTINTO_A_PROPOSITO = {
    '11001': 'Bogotá — sin el «D.C.» de la fuente',
    '15407': 'Leyva — la fuente dice «Villa De Leyva»',
    '19418': 'López de Micay — nombre completo; la fuente abrevia «López»',
    '20443': 'Manaure Balcón del Cesar — nombre completo',
    '23670': 'San Andrés de Sotavento — la fuente omite «de»',
    '47161': 'Cerro de San Antonio — la fuente omite «de»',
    '47170': 'Chivolo — la fuente escribe «Chibolo»',
    '52835': 'Tumaco — la fuente dice «San Andres De Tumaco»',
    '54670': 'San Calixto — la fuente trae un typo: «San Cali1to»',
    '91460': 'Mirití-Paraná — con guion y tilde',
    '05664': 'San Pedro de los Milagros — nombre completo',
    '05674': 'San Vicente Ferrer — nombre completo',
}

#: Códigos que el WMS tiene y la fuente no. Cada uno dice por qué.
FUERA_DE_LA_FUENTE = {
    '94885': 'La Guadalupe (Guainía): área no municipalizada que la lista de '
             'MinSalud no trae y el WMS ya tenía',
}


def _sin_tildes(s):
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn').lower()


def _coincide(nombre_fuente, nombre_wms):
    """`�` (tilde dañada en la fuente) vale por cualquier letra."""
    patron = ''.join('.' if c == '�' else re.escape(c)
                     for c in _sin_tildes(nombre_fuente))
    return re.fullmatch(patron, _sin_tildes(nombre_wms)) is not None


def _fuente():
    with FUENTE.open(encoding='utf-8', newline='') as f:
        return {r['codigo_dane']: r['nombre_fuente'] for r in csv.DictReader(f)}


def _diferencias(tabla, fuente):
    """Todo lo que separa `tabla` de la fuente y no está declarado."""
    malos = []
    for codigo, nombre in fuente.items():
        if codigo not in tabla:
            malos.append(f'{codigo} {nombre!r}: falta en el WMS')
        elif (not _coincide(nombre, tabla[codigo])
              and codigo not in NOMBRE_DISTINTO_A_PROPOSITO):
            malos.append(f'{codigo}: la fuente dice {nombre!r} y el WMS {tabla[codigo]!r}')
    for codigo in tabla:
        if codigo not in fuente and codigo not in FUERA_DE_LA_FUENTE:
            malos.append(f'{codigo} {tabla[codigo]!r}: no existe en la fuente')
    return malos


class TestLaTablaEsLaDivipola:

    def test_la_fuente_esta_completa(self):
        """Piso: un CSV truncado o mal leído daría cero diferencias por no comparar nada."""
        fuente = _fuente()
        assert len(fuente) >= 1122
        assert fuente['41132'] == 'Campoalegre'

    def test_cada_codigo_tiene_el_nombre_de_la_fuente(self):
        malos = _diferencias(DANE, _fuente())
        assert not malos, 'La tabla DANE no coincide con la DIVIPOLA:\n' + '\n'.join(malos)

    def test_las_excepciones_declaradas_siguen_siendo_necesarias(self):
        """El inventario solo encoge: una excepción que ya coincide sobra."""
        fuente = _fuente()
        sobran = [c for c in NOMBRE_DISTINTO_A_PROPOSITO
                  if c in DANE and _coincide(fuente[c], DANE[c])]
        sobran += [c for c in FUERA_DE_LA_FUENTE if c in fuente]
        assert not sobran, f'excepciones que ya no hacen falta: {sobran}'

    def test_ningun_nombre_trae_letras_de_otro_alfabeto(self):
        """«Entrерríos» tenía una «е» y una «р» cirílicas: a la vista, idéntico."""
        raros = {k: v for k, v in DANE.items()
                 if any(ord(c) > 0x24F for c in v)}
        assert not raros


class TestLosCasosDeLaOperacion:
    """Los municipios por los que pasa la operación, con el código que manda Siesa."""

    @pytest.mark.parametrize('depto, ciudad, esperado', [
        ('41', '132', 'Campoalegre'),   # PD20822: el caso que lo destapó
        ('41', '206', 'Colombia'),
        ('41', '001', 'Neiva'),
        ('41', '551', 'Pitalito'),
        ('41', '298', 'Garzón'),
        ('41', '396', 'La Plata'),
        ('41', '006', 'Acevedo'),
        ('18', '001', 'Florencia'),
        ('18', '029', 'Albania'),
    ])
    def test_resuelve_el_municipio_real(self, depto, ciudad, esperado):
        assert resolver_municipio(depto, ciudad) == esperado


class TestElCruceMuerde:
    """El detector sobre la forma exacta del defecto: la lista corrida un lugar."""

    def test_detecta_el_huila_corrido(self):
        corrida = dict(DANE)
        huila = sorted(k for k in DANE if k.startswith('41'))
        for a, b in zip(huila, huila[1:]):
            corrida[a] = DANE[b]
        malos = _diferencias(corrida, _fuente())
        assert any(m.startswith('41132') for m in malos)
        assert len(malos) >= 30

    def test_detecta_un_municipio_que_falta(self):
        sin_uno = {k: v for k, v in DANE.items() if k != '41006'}
        assert any('41006' in m and 'falta' in m for m in _diferencias(sin_uno, _fuente()))

    def test_detecta_un_codigo_inventado(self):
        con_extra = dict(DANE, **{'41999': 'Inventado'})
        assert any(m.startswith('41999') for m in _diferencias(con_extra, _fuente()))
