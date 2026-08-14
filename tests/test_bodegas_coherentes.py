"""
Las nueve copias del maestro de bodegas tienen que decir lo mismo.

El 2026-08-10, armando los almacenes que faltaban, apareció que la relación
bodega ↔ CO ↔ nombre está escrita en nueve sitios y ninguno es la fuente:

  · `almacenes` (tabla) — la única con autoridad real, y a propósito
    incompleta: solo los PV que ya operan.
  · `tienda_oc._BODEGA_CO_MAP` — el ÚNICO con el mapeo CO completo de las 10.
  · `traslado_service._BODEGAS_PREWARM` — a cuáles se les calienta el stock.
  · `inventario_siesa_service._BODEGAS_PV`.
  · cinco mapas de nombres en el JS, uno de ellos inline en un `onchange`.

Este archivo **no arregla la duplicación**. Unificarlas es un refactor sobre
código que hoy funciona, y hacerlo justo antes de un corte a producción es la
lección de «validar contra producción real» al revés. Lo que hace es que la
próxima divergencia se vea en el build en vez de en un traslado rechazado.

El caso real que lo motivó: `PT1` y `FN1` estaban en `_BODEGA_CO_MAP` y en los
mapas del JS desde hacía meses, pero **no existían como `almacen`**. La app
sabía a qué CO pertenecían y aun así no se podía operar con ellas.
"""
import json
import re
from pathlib import Path

_RAIZ = Path(__file__).resolve().parents[1]
_PWA = _RAIZ / 'app' / 'static' / 'pwa'

#: **Dos listas, no una.** Confundirlas fue el error del 2026-08-10.
#:
#: `NS2` (NEIVA SUR FUNDACIÓN) se sacó de los nueve sitios «tras verificar cero
#: usuarios asignados». El criterio respondía *¿alguien trabaja ahí?* cuando la
#: pregunta era *¿algo se mueve por ahí?* — y NS2 es la **bodega de parqueo de
#: licitaciones**: lo que sale por relación de entrega antes de que exista
#: contrato se traslada ahí para que NB1 no quede con inventario que
#: físicamente no está. Tenía stock (6 SKU / 121 und) y el traslado se venía
#: haciendo a mano porque el WMS ya no la conocía.
#:
#: La preocupación original **sigue siendo válida**, solo que era sobre la otra
#: lista: una bodega de parqueo en el desplegable de punto de venta es una
#: opción que alguien elige por error y queda facturando contra una sede que
#: nadie mira.

#: Bodegas que el WMS OPERA: stock, traslados, reconciliación. Incluye las de
#: parqueo. NO incluye AV1/TRA1/BC99 (servicio) ni FD1/ND1/PD1 (duplicadas).
BODEGAS_OPERADAS = frozenset({
    'NB1', 'NS1', 'NS2', 'NC1', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1',
})

#: Bodegas que son PUNTO DE VENTA — las que se le pueden asignar a un usuario.
#: Un subconjunto: NS2 mueve inventario pero nadie vende desde ahí.
BODEGAS_PV = frozenset({
    'NB1', 'NS1', 'NC1', 'FC1', 'PC1', 'PT1', 'FF1', 'FN1', 'FP1',
})

#: Bodega → CO. Fuente: maestro de Siesa + `CO PAME.xlsx`, confirmado contra
#: `tienda_oc._BODEGA_CO_MAP` que ya lo tenía completo.
BODEGA_CO = {
    'NS1': '001', 'NS2': '001', 'NC1': '002', 'NB1': '003', 'PC1': '004',
    'PT1': '005', 'FC1': '006', 'FN1': '007', 'FP1': '008', 'FF1': '009',
}


def _lista_py(archivo: Path, nombre: str) -> set:
    """Los strings de una lista literal `NOMBRE = [...]` en un .py."""
    texto = archivo.read_text(encoding='utf-8')
    m = re.search(rf'^{nombre}\s*=\s*\[(.*?)\]', texto, re.M | re.S)
    assert m, f'no está {nombre} en {archivo.name} — ¿lo renombraron?'
    return set(re.findall(r"'([A-Z0-9]+)'", m.group(1)))


class TestLasListasDePythonCoinciden:

    def test_prewarm_cubre_exactamente_los_puntos_de_venta(self):
        """Una bodega de más gasta una consulta a Siesa cada 4 minutos. Una de
        menos deja ese PV con stock frío y el operario esperando."""
        prewarm = _lista_py(
            _RAIZ / 'app' / 'services' / 'traslado_service.py', '_BODEGAS_PREWARM')
        # OPERADAS, no PV: el prewarm calienta stock, y NS2 tiene stock aunque
        # nadie venda desde ahí.
        assert prewarm == set(BODEGAS_OPERADAS), (
            f'sobran: {prewarm - set(BODEGAS_OPERADAS)} · '
            f'faltan: {set(BODEGAS_OPERADAS) - prewarm}')

    def test_bodegas_pv_de_inventario_coincide(self):
        # El nombre de la constante dice `_BODEGAS_PV` pero lo que hace es
        # descargar STOCK. Se compara contra OPERADAS: dejar NS2 fuera hace
        # invisible su inventario, que es justo lo que se acaba de corregir.
        pv = _lista_py(
            _RAIZ / 'app' / 'services' / 'inventario_siesa_service.py', '_BODEGAS_PV')
        assert pv == set(BODEGAS_OPERADAS)

    def test_el_mapa_de_CO_esta_completo_y_de_acuerdo(self):
        """`_BODEGA_CO_MAP` es el único sitio del código con las 10 → CO. Si
        diverge de acá, una tienda factura contra el centro de operación
        equivocado y eso es un error contable, no un bug de pantalla."""
        texto = (_RAIZ / 'app' / 'routes' / 'tienda_oc.py').read_text(encoding='utf-8')
        m = re.search(r'_BODEGA_CO_MAP\s*=\s*\{(.*?)\}', texto, re.S)
        assert m, 'no está _BODEGA_CO_MAP en tienda_oc.py'
        real = dict(re.findall(r"'([A-Z0-9]+)'\s*:\s*'(\d+)'", m.group(1)))
        assert real == BODEGA_CO, (
            f'\nen el código: {real}\nesperado:     {BODEGA_CO}')


class TestLosMapasDelJSCoinciden:
    """Cinco mapas de nombres en el JS, tres de ellos en el mismo archivo.

    **Se mide cada mapa por separado, no el archivo.** La primera versión de
    este test escaneaba `app.js` entero, y por eso no vio que
    `_USR_NOMBRES_BOD` no tenía `NB1`: los otros dos mapas del mismo archivo sí
    lo tenían y el conjunto daba completo. Un guard que mide el agregado
    cuando el defecto vive en una parte es un guard en verde sobre algo roto —
    el mismo error que ya costó tres veces en este repo.

    Un PV que falte en un mapa se muestra con su código crudo —`FN1`— donde los
    demás dicen el nombre: el operario no reconoce su propia tienda.
    """

    #: (archivo, etiqueta, patrón que aísla ESE mapa). El patrón recorta el
    #: bloque; después se buscan códigos de bodega solo adentro.
    _MAPAS = (
        ('traslados.js', '_REQ_BODEGA_NOMBRES', r'_REQ_BODEGA_NOMBRES\s*=\s*\{(.*?)\}'),
        ('tienda.js', '_BODEGAS_ORIGEN', r'_BODEGAS_ORIGEN\s*=\s*\[(.*?)\]'),
        ('app.js', '_USR_NOMBRES_BOD', r'_USR_NOMBRES_BOD\s*=\s*\{(.*?)\}'),
        ('app.js', 'onchange nombres={}', r'const nombres=\{(.*?)\}'),
        ('app.js', '<option> punto de venta', r'(<option value="[A-Z]{2}\d".*?u-bodega|id="u-bodega-siesa".*?</select>)'),
    )

    def _bodegas_del_mapa(self, archivo: str, patron: str) -> set:
        texto = (_PWA / archivo).read_text(encoding='utf-8')
        m = re.search(patron, texto, re.S)
        assert m, f'no se encontró el mapa {patron[:30]!r} en {archivo}'
        return set(re.findall(r'\b([NPF][A-Z]\d)\b', m.group(1))) & BODEGAS_OPERADAS

    def test_cada_mapa_por_separado_conoce_las_nueve(self):
        incompletos = {}
        for archivo, etiqueta, patron in self._MAPAS:
            faltan = BODEGAS_PV - self._bodegas_del_mapa(archivo, patron)
            if faltan:
                incompletos[f'{archivo} › {etiqueta}'] = sorted(faltan)
        assert not incompletos, (
            '\nMapas del PWA a los que les faltan bodegas:\n'
            + '\n'.join(f'  · {k}: {v}' for k, v in incompletos.items())
            + '\n\nEse PV se muestra con su código crudo en vez del nombre.')

    def test_ningun_mapa_ofrece_una_bodega_que_no_se_opera(self):
        """El otro lado: un mapa no puede ofrecer una bodega que el WMS no
        opera — sería una opción que lleva a facturar contra una sede que nadie
        mira.

        Se compara contra `BODEGAS_OPERADAS`, que incluye las de parqueo. Lo
        que NO puede aparecer es una bodega de parqueo en el desplegable de
        **punto de venta** de un usuario, y eso lo vigila
        `test_el_desplegable_de_usuario_solo_ofrece_puntos_de_venta`.
        """
        sobrantes = {}
        for archivo, etiqueta, patron in self._MAPAS:
            texto = (_PWA / archivo).read_text(encoding='utf-8')
            m = re.search(patron, texto, re.S)
            de_mas = set(re.findall(r'\b([NPF][A-Z]\d)\b', m.group(1))) - BODEGAS_OPERADAS
            if de_mas:
                sobrantes[f'{archivo} › {etiqueta}'] = sorted(de_mas)
        assert not sobrantes, (
            '\nBodegas ofrecidas que el WMS no opera:\n'
            + '\n'.join(f'  · {k}: {v}' for k, v in sobrantes.items()))

    def test_el_detector_ve_cada_mapa(self):
        """Un patrón que deja de encontrar pasa vacío para siempre."""
        for archivo, etiqueta, patron in self._MAPAS:
            n = len(self._bodegas_del_mapa(archivo, patron))
            assert n >= 9, f'{archivo} › {etiqueta}: solo detectó {n} bodegas'


class TestLaTablaDeCLAUDEmdSigueAhi:
    """La tabla de CLAUDE.md es lo que lee un humano antes de tocar un traslado.
    Si se borra, vuelve a vivir solo en un `.docx` que nadie puede grepear."""

    def test_estan_las_diez_bodegas_documentadas(self):
        doc = (_RAIZ / 'CLAUDE.md').read_text(encoding='utf-8')
        i = doc.find('### Bodegas y Centros de Operación')
        assert i != -1, 'desapareció la tabla de bodegas de CLAUDE.md'
        seccion = doc[i:i + 4000]
        faltan = [b for b in sorted(BODEGAS_OPERADAS) if f'`{b}`' not in seccion]
        assert not faltan, f'sin documentar en CLAUDE.md: {faltan}'

    def test_declara_que_999_no_lleva_almacen(self):
        doc = (_RAIZ / 'CLAUDE.md').read_text(encoding='utf-8')
        assert '999' in doc and 'ADMINISTRATIVO' in doc


class TestElParqueoNoEsUnPuntoDeVenta:
    """La preocupación que hizo sacar NS2 era correcta — sobre la otra lista.

    `NS2` (Fundación) es la bodega de **parqueo de licitaciones**: mueve
    inventario y hay que poder trasladarle. Lo que no es, es un punto de venta.

    Ofrecerla en el desplegable donde se le asigna sede a un usuario es una
    opción que alguien elige por error y queda facturando contra un sitio que
    nadie mira. Sacarla de TODAS las listas por ese motivo fue arreglar el
    problema correcto en el lugar equivocado: dejó su stock invisible y obligó
    a licitaciones a hacer el traslado a mano.
    """

    def test_el_desplegable_de_usuario_solo_ofrece_puntos_de_venta(self):
        """El `<select>` de sede en el alta de usuario."""
        texto = (_PWA / 'app.js').read_text(encoding='utf-8')
        opciones = set(re.findall(r"<option value=\"([NPF][A-Z]\d)\"", texto))
        assert opciones, 'no se encontró el desplegable de sede — ¿se renombró?'
        de_mas = opciones - BODEGAS_PV
        assert not de_mas, (
            f'\nEl desplegable de sede ofrece {sorted(de_mas)}, que no son '
            f'puntos de venta.\nUna bodega de parqueo elegible como sede deja '
            f'a alguien facturando contra un sitio que nadie mira.')

    def test_las_dos_listas_no_son_la_misma(self):
        """Si alguien las vuelve a fusionar, o se pierde el stock de parqueo o
        se ofrece una sede que no existe. Las dos ya pasaron."""
        assert BODEGAS_PV < BODEGAS_OPERADAS, (
            'BODEGAS_PV tiene que ser un subconjunto ESTRICTO de OPERADAS')
        assert BODEGAS_OPERADAS - BODEGAS_PV == {'NS2'}
