"""
Las copias del maestro de bodegas tienen que decir lo mismo.

El 2026-08-10, armando los almacenes que faltaban, apareció que la relación
bodega ↔ CO ↔ nombre está escrita en muchos sitios y ninguno es la fuente:

  · `almacenes` (tabla) — la única con autoridad real, y a propósito
    incompleta: solo los PV que ya operan.
  · `services/bodegas.BODEGA_CO` — el maestro certificado, desde 2026-08-14.
  · `traslado_service._BODEGAS_PREWARM` — a cuáles se les calienta el stock.
  · `inventario_siesa_service._BODEGAS_PV`.
  · cinco mapas de nombres en el JS, uno de ellos inline en un `onchange`.

## Lo que este archivo NO vio, y por qué (2026-08-14)

La versión anterior decía que `tienda_oc._BODEGA_CO_MAP` era «el ÚNICO con el
mapeo CO completo de las 10». Era el único **completo**; no el único que
**existía**. Había tres diccionarios con ese mismo nombre:

    app/routes/tienda_oc.py          10   ← el que este test leía
    app/routes/traslados.py           9   ← sin FP1
    app/services/traslado_service.py  8   ← sin FP1 ni NS2

Diez tests en verde sobre dos copias rotas, y la de 8 es la que usa
`TrasladoService.confirmar_recepcion` — la vía viva del ETS 173079. Un traslado
a NS2 o FP1 salía con CO 003 y lo rechazaba Siesa.

**La lección no es que faltaba mirar dos archivos más.** Es que el test medía
*una copia* cuando la propiedad era *todas coinciden*: mientras el detector
tenga escrita a mano la lista de sitios que revisa, el sitio nuevo no entra.
Por eso ahora el maestro vive en un solo módulo y lo que se vigila es que
**no exista un segundo**, descubriéndolo por AST sobre todo el árbol.

Por AST y no por texto: los detectores de texto de este repo ya se atraparon
cinco veces en sus propios docstrings.

El caso real que lo motivó todo: `PT1` y `FN1` estaban en los mapas desde hacía
meses pero **no existían como `almacen`**. La app sabía a qué CO pertenecían y
aun así no se podía operar con ellas.
"""
import ast
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

    def test_el_maestro_de_CO_esta_completo_y_de_acuerdo(self):
        """`services.bodegas.BODEGA_CO` es el maestro. Si diverge de acá, una
        tienda factura contra el centro de operación equivocado y eso es un
        error contable, no un bug de pantalla.

        La copia de este archivo se escribe a mano **a propósito**: un test que
        importa la constante que verifica no verifica nada.
        """
        from app.services.bodegas import BODEGA_CO as real
        assert real == BODEGA_CO, (
            f'\nen el código: {real}\nesperado:     {BODEGA_CO}')

    def test_toda_bodega_operada_tiene_su_CO(self):
        """Las dos listas tienen que cubrirse: una bodega que el WMS opera y no
        tiene CO produce `co_destino=None`, y el gateway cae al CO por defecto
        (003). El documento sale con la bodega de una sede y el CO de otra, que
        es justo lo que Siesa rechaza (46089/46090)."""
        from app.services.bodegas import BODEGA_CO as real
        faltan = BODEGAS_OPERADAS - set(real)
        assert not faltan, (
            f'bodegas que el WMS opera y no tienen CO: {sorted(faltan)} — '
            f'sus traslados salen con el CO equivocado')


class TestNoHayUnaSegundaCopiaDelMaestro:
    """EL TRINQUETE. Lo que falló el 2026-08-14 no fue el contenido: fue que el
    detector tenía escrita a mano la lista de sitios que revisaba.

    Tres diccionarios `_BODEGA_CO_MAP` con 10, 9 y 8 entradas, y el test leía
    solo el de 10. Mientras el guard enumere sitios, el sitio nuevo no entra.

    Este busca la **forma** del dato —claves de bodega, valores de CO— en todo
    el árbol, así que encuentra la copia aunque se llame distinto, viva en otro
    módulo o la escriba alguien que no leyó nada de esto.
    """

    #: `'NB1'` — dos letras y un dígito.
    _CLAVE_BODEGA = re.compile(r'^[A-Z]{2}\d$')
    #: `'003'` — CO Siesa, tres dígitos.
    _VALOR_CO = re.compile(r'^\d{3}$')

    #: El único archivo autorizado a contener el maestro.
    _MAESTRO = ('app', 'services', 'bodegas.py')

    def _es_mapa_de_co(self, nodo):
        """¿Este dict literal es un mapa bodega→CO?

        Exige ≥3 pares para no confundirse con un dict cualquiera que tenga una
        clave con esa forma. Un mapa parcial de 2 sería un falso negativo, pero
        también sería inofensivo: el daño de las copias fue tener casi todas.
        """
        if not isinstance(nodo, ast.Dict) or len(nodo.keys) < 3:
            return False
        for k, v in zip(nodo.keys, nodo.values):
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return False
            if not (isinstance(v, ast.Constant) and isinstance(v.value, str)):
                return False
            if not self._CLAVE_BODEGA.match(k.value):
                return False
            if not self._VALOR_CO.match(v.value):
                return False
        return True

    def _copias(self):
        maestro = _RAIZ.joinpath(*self._MAESTRO)
        fuera = []
        for py in sorted((_RAIZ / 'app').rglob('*.py')):
            if py == maestro:
                continue
            try:
                arbol = ast.parse(py.read_text(encoding='utf-8'))
            except SyntaxError:
                continue
            for nodo in ast.walk(arbol):
                if self._es_mapa_de_co(nodo):
                    fuera.append(f'{py.relative_to(_RAIZ)}:{nodo.lineno}')
        return fuera

    def test_el_maestro_vive_en_un_solo_archivo(self):
        copias = self._copias()
        assert not copias, (
            '\nHay un mapa bodega→CO fuera de app/services/bodegas.py:\n'
            + '\n'.join(f'  · {c}' for c in copias)
            + '\n\nUsar `co_de_bodega()`. Una copia diverge sin que nadie lo '
              'note, y el resultado es un documento con la bodega de una sede '
              'y el CO de otra — que Siesa rechaza, dejando la mercancía en la '
              'bodega de tránsito.')

    def test_el_detector_ve_un_mapa_de_verdad(self):
        """Un detector que dejó de encontrar pasa vacío para siempre.

        Se le da el maestro real: si no lo reconoce, tampoco reconocería una
        copia, y el test de arriba estaría verde por ceguera.
        """
        maestro = _RAIZ.joinpath(*self._MAESTRO)
        arbol = ast.parse(maestro.read_text(encoding='utf-8'))
        encontrados = [n for n in ast.walk(arbol) if self._es_mapa_de_co(n)]
        assert encontrados, (
            'el detector no reconoce ni el maestro que vive en bodegas.py — '
            'si BODEGA_CO cambió de forma, este test hay que reescribirlo, no '
            'borrarlo')


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


class TestCoDeBodegaResuelve:
    """El comportamiento, no la tabla. Los tests de arriba comparan literales;
    estos ejercen la función que arma los documentos."""

    def test_NS2_y_FP1_resuelven_sin_almacen_configurado(self, app, db):
        """LA REGRESIÓN. Las dos que faltaban en la copia de 8.

        Sin almacén cargado, la versión vieja devolvía `None` y el gateway caía
        a `centro_op` (003): el ETS 173079 salía con CO 003 y bodega_entrada
        NS2. Siesa valida que coincidan y lo rechaza — la mercancía se queda en
        la bodega de tránsito y nadie reclama, porque una tienda que no recibió
        un traslado que no pidió no llama.
        """
        from app.services.bodegas import co_de_bodega
        assert co_de_bodega('NS2') == '001'
        assert co_de_bodega('FP1') == '008'

    def test_ninguna_bodega_operada_cae_al_CO_por_defecto(self, app, db):
        """El modo de fallo era silencioso: devolver el CO de NB1 para todas.
        Acá se comprueba de una vez que ninguna se resuelve al default."""
        from app.services.bodegas import co_de_bodega
        centinela = 'XXX'
        cayeron = {b for b in BODEGAS_OPERADAS
                   if co_de_bodega(b, por_defecto=centinela) == centinela}
        assert not cayeron, f'sin CO resoluble: {sorted(cayeron)}'

    def test_la_tabla_almacenes_manda_sobre_el_certificado(self, app, db):
        """`almacenes` es la autoridad — el certificado es el respaldo mientras
        la tabla termine de cargarse."""
        from app.models.almacen import Almacen
        from app.services.bodegas import co_de_bodega
        db.session.add(Almacen(codigo='PV-FP1-T', nombre='Feria Pitalito',
                               bodega_siesa_id='FP1', centro_op_siesa='777'))
        db.session.commit()
        assert co_de_bodega('FP1') == '777'

    def test_una_bodega_desconocida_no_inventa_un_CO(self, app, db):
        """Devolver un CO plausible para una bodega que nadie conoce es peor
        que no devolver nada: produce un documento que parece bien armado."""
        from app.services.bodegas import co_de_bodega
        assert co_de_bodega('ZZ9') is None
        assert co_de_bodega('') is None

    def test_el_gateway_usa_la_misma_politica(self, app, db):
        """El defecto más difícil de ver: un solo payload 173079 traía el CO
        base resuelto desde `almacenes` y el CO de entrada desde un diccionario
        literal que no coincidía. Una pregunta, dos políticas, mismo documento.
        """
        from app.services.bodegas import co_de_bodega
        from app.services.connekta_gateway import connekta
        for b in sorted(BODEGAS_OPERADAS):
            assert connekta._co_de_bodega(b) == co_de_bodega(
                b, por_defecto=connekta.centro_op_traslado), b
