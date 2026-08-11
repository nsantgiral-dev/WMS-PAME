"""
El payload contra el DOCX del conector, campo por campo y en orden.

La Regla 1 del proyecto dice «LEER EL DOCX DEL CONECTOR ANTES DE CODIFICAR —
costo de no hacerlo: 5+ rondas de prueba-error». Estaba escrita, se cumplía a
mano, y nadie la automatizó. El 2026-08-11 costó esto:

    POST 142888 HTTP 400 — "El tamaño del registro no corresponde al exigido.
    Tamaño del registro = 430. Tamaño registro exigido = 596."

El encabezado del recibo de caja mandaba **22 campos y el DOCX exige 33**.
Faltaban los diez `F351_*` de ajuste y otros ingresos, más `F357_REFERENCIA`.
La sección Caja mandaba 15 de 20.

**Ningún recibo de caja llegó nunca a Siesa.** No es que fallara a veces: la
liquidación de ruta nunca cerró el ciclo, y el defecto estaba en el primer POST
que se hacía.

## Por qué la omisión no es lo mismo que el vacío

Connekta convierte el JSON en un archivo plano posicional. Cada campo ocupa su
ancho aunque vaya vacío. Omitir uno **no lo deja en blanco: acorta la línea y
corre todo lo que sigue**. Por eso el error señalaba
`F357_IND_VALIDA_MEDPAGO` como «obligatorio y numérico» en la posición 596
cuando sí lo mandábamos — aterrizaba en la 430.

Es el mismo mecanismo que la Regla 8 (`f470_desc_varible`, el typo
intencional): un campo mal nombrado u omitido desalinea el registro entero.

## Qué mide este archivo

Que las secciones del payload tengan **los mismos campos, en el mismo orden**
que la plantilla JSON del DOCX. No los valores — esos dependen del caso. La
estructura, que es lo que el plano posicional exige y lo que se rompió.

Se lee el `.docx` en cada corrida en vez de copiar la lista acá: una copia
diverge del original, y la divergencia entre dos copias de la misma verdad ya
costó bastante en este repo.
"""
import re
import zipfile
from html import unescape
from pathlib import Path

import pytest

_RAIZ = Path(__file__).resolve().parents[1]
_SPECS = _RAIZ / 'docs' / 'siesa-specs'
_GATEWAY = _RAIZ / 'app' / 'services' / 'connekta_gateway.py'


def _plantilla_docx(nombre_archivo: str) -> dict:
    """`{seccion: [campos en orden]}` desde la plantilla JSON del DOCX."""
    ruta = _SPECS / nombre_archivo
    assert ruta.exists(), f'falta el spec {nombre_archivo}'
    xml = zipfile.ZipFile(ruta).read('word/document.xml').decode('utf-8')
    texto = unescape(re.sub(r'<[^>]+>', '', re.sub(r'</w:p>', '\n', xml)))
    lineas = [re.sub(r'\s+', ' ', l).strip() for l in texto.split('\n') if l.strip()]

    secciones, actual = {}, None
    for l in lineas:
        m = re.match(r'"(\w+)":\s*\[', l)
        if m:
            actual = m.group(1)
            secciones[actual] = []
            continue
        if actual is not None:
            c = re.match(r'"([A-Za-z0-9_]+)":', l)
            if c:
                secciones[actual].append(c.group(1))
            elif l.startswith('}') and secciones.get(actual):
                actual = None          # cerró la sección
    return secciones


def _campos_del_dict(fuente: str, nombre_var: str, desde: int = 0) -> list:
    """Los keys de un dict literal `nombre_var = {...}`, en orden de escritura.

    En orden y no como conjunto: el plano es posicional. Dos payloads con los
    mismos campos en distinto orden producen documentos distintos, y uno de los
    dos está mal.
    """
    i = fuente.find(f'{nombre_var} = {{', desde)
    assert i != -1, f'no se encontró el dict {nombre_var}'
    j = fuente.find('\n        }', i)
    return re.findall(r"'([A-Za-z0-9_]+)':", fuente[i:j])


class TestReciboCaja142888:
    """El que falló. 33 y 20 campos, no 22 y 15."""

    _DOCX = '142888 API_v1_ReciboCaja.docx'

    @pytest.fixture(scope='class')
    def spec(self):
        return _plantilla_docx(self._DOCX)

    @pytest.fixture(scope='class')
    def fuente(self):
        t = _GATEWAY.read_text(encoding='utf-8')
        i = t.find('    def trigger_recibo_caja')
        j = t.find('    def trigger_documento_contable')
        assert i != -1 and j > i
        return t[i:j]

    def test_el_encabezado_tiene_los_33_campos_del_spec(self, spec, fuente):
        esperado = spec['RCyotrosingresos']
        real = _campos_del_dict(fuente, 'header')
        faltan = [c for c in esperado if c not in real]
        assert not faltan, (
            f'\nAl encabezado (357 subtipo 0) le faltan {len(faltan)} campos '
            f'del DOCX:\n  {faltan}\n\n'
            f'Connekta arma un plano POSICIONAL: omitir un campo no lo deja '
            f'vacío, acorta la línea y corre todo lo que sigue. Siesa responde '
            f'"Tamaño del registro = N. Tamaño registro exigido = M".')

    def test_el_encabezado_respeta_el_orden_del_spec(self, spec, fuente):
        real = _campos_del_dict(fuente, 'header')
        assert real == spec['RCyotrosingresos'], (
            '\nEl orden no coincide con el DOCX. En un plano posicional el '
            'orden ES la posición: un campo correcto en el offset equivocado '
            'escribe su valor sobre otro campo.')

    def test_la_seccion_caja_tiene_los_20_campos(self, spec, fuente):
        esperado = spec['Caja']
        real = _campos_del_dict(fuente, 'caja')
        faltan = [c for c in esperado if c not in real]
        assert not faltan, f'a la sección Caja (358) le faltan: {faltan}'

    def test_la_seccion_caja_respeta_el_orden(self, spec, fuente):
        assert _campos_del_dict(fuente, 'caja') == spec['Caja']

    def test_el_campo_de_la_posicion_596_va_ultimo_y_numerico(self, spec, fuente):
        """El que Siesa señalaba: «obligatorio y debe ser numérico», pos 596.

        Sí se mandaba — aterrizaba en la 430 porque faltaban los once de
        antes. Que vaya último no es cosmético: es su posición.
        """
        real = _campos_del_dict(fuente, 'header')
        assert real[-1] == 'F357_IND_VALIDA_MEDPAGO'
        assert re.search(r"'F357_IND_VALIDA_MEDPAGO':\s*0\b", fuente), \
            'tiene que ir 0 numérico, no "" — Siesa lo exige numérico'


class TestDocumentoContable142882:
    """El mismo defecto, en un conector que todavía no falló porque nunca corrió.

    Le faltaban `f350_id_mandato`, `F351_ID_FE`, `F351_DOCTO_BANCO` y
    `F351_NRO_DOCTO_BANCO`. Se arregló desde el spec el 2026-08-11, **sin una
    corrida real que lo confirme** — la primera liquidación con retención lo
    ejercita.

    Solo se comprueban las secciones que el payload MANDA. Una sección que no se
    envía (Entidades, Diferidos, Caja, MovimientoCxP) no es un registro corto:
    es un registro ausente, y eso el conector lo admite.
    """

    _DOCX = '142882 - API_v1_DocumentoContable 428272.docx'
    _SECCIONES_QUE_MANDAMOS = ('Documentocontable', 'Movimientocontable', 'MovimientoCxC')

    @pytest.fixture(scope='class')
    def spec(self):
        return _plantilla_docx(self._DOCX)

    @pytest.fixture(scope='class')
    def fuente(self):
        t = _GATEWAY.read_text(encoding='utf-8')
        i = t.find('    def trigger_documento_contable')
        assert i != -1
        return t[i:i + 14000]

    @pytest.mark.parametrize('seccion', _SECCIONES_QUE_MANDAMOS)
    def test_no_falta_ningun_campo_del_spec(self, spec, fuente, seccion):
        i = fuente.find(f"'{seccion}': [{{")
        assert i != -1, f'el payload dejó de mandar la sección {seccion}'
        reales = set(re.findall(r"'([A-Za-z0-9_]+)':", fuente[i:fuente.find('}]', i)]))
        faltan = [c for c in spec[seccion] if c not in reales]
        assert not faltan, (
            f'\n{seccion}: faltan {len(faltan)} campos del DOCX: {faltan}\n'
            f'En un plano posicional eso acorta el registro y Siesa lo rechaza.')

    def test_las_secciones_declaradas_son_las_que_se_mandan(self, fuente):
        """Si el payload empieza a mandar una sección nueva, este test la
        marca — y hay que agregarla arriba para que se verifique. Una sección
        nueva sin verificar es exactamente el estado del que venimos."""
        enviadas = set(re.findall(r"'(\w+)':\s*\[\{", fuente)) - {'Inicial', 'Final'}
        nuevas = enviadas - set(self._SECCIONES_QUE_MANDAMOS)
        assert not nuevas, (
            f'secciones nuevas sin verificar contra el DOCX: {sorted(nuevas)}')


class TestElDetectorNoEstaCiego:
    """Si el lector del DOCX dejara de encontrar secciones, los tests de arriba
    compararían listas vacías y pasarían para siempre."""

    def test_el_docx_se_lee_y_trae_las_cinco_secciones(self):
        spec = _plantilla_docx('142888 API_v1_ReciboCaja.docx')
        assert set(spec) >= {'RCyotrosingresos', 'Caja', 'CxC'}
        assert len(spec['RCyotrosingresos']) == 33, (
            f'el DOCX dejó de reportar 33 campos en el encabezado '
            f'(leyó {len(spec["RCyotrosingresos"])}) — o cambió el spec, o el '
            f'lector se rompió')
        assert len(spec['Caja']) == 20
        assert len(spec['CxC']) == 15

    def test_el_lector_de_dicts_encuentra_campos(self):
        fuente = _GATEWAY.read_text(encoding='utf-8')
        i = fuente.find('    def trigger_recibo_caja')
        assert len(_campos_del_dict(fuente[i:], 'header')) >= 33
