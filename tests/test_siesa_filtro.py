"""
Ningún filtro de Siesa se vuelve a armar a mano.

La Regla 15 pide `campo = ''valor''`. Eso estaba escrito **37 veces en 4
archivos**, y el saneamiento existía en **2** de esas 37. La política estaba
bien escrita y no cubría a sus llamadores — la misma forma del mapa bodega→CO
(tres copias, la del medio rota), de la fórmula de retención (tres, la tercera
equivocada) y del match de cartera (tres, la que decidía buscaba mal).

Ahora hay una función, `siesa_filtro.lit()`, y este archivo impide que aparezca
la segunda.

## Por qué AST y no texto

Un detector de texto que busque `''{` se atrapa **en este propio docstring** —
acaba de pasar, dos líneas más arriba. En este repo pasó **siete veces en una
semana**, y la séptima fue el regex que medía la regla de los detectores
ciegos. El único que no se atrapa solo es el que lee la estructura.
"""
import ast
import pathlib

import pytest

#: El único archivo autorizado a producir el literal: es quien lo define.
_AUTORIZADO = 'app/services/siesa_filtro.py'

_RAIZ = pathlib.Path(__file__).resolve().parent.parent


def _fuentes():
    for base in ('app', 'flota'):
        for p in sorted((_RAIZ / base).rglob('*.py')):
            rel = p.relative_to(_RAIZ).as_posix()
            if rel != _AUTORIZADO:
                yield rel, p.read_text()


def _literales_a_mano(fuente: str):
    """Los f-strings que delimitan un valor con `''…''` **alrededor** de una
    interpolación. Devuelve las líneas.

    ## La adyacencia importa, y la primera versión de esto se equivocó

    Medía «¿aparece `''` en el texto fijo?», concatenando antes todas las
    partes constantes. Eso **inventa adyacencias que el código no tiene**:

        f"Código '{codigo}' no encontrado"

    tiene las constantes `"Código '"` y `"' no encontrado"`, y al pegarlas sale
    un `''` que en el fuente no existe — las dos comillas están en lados
    opuestos de la interpolación. Marcó 9 mensajes de error y DDL de SQLite
    como si fueran filtros de Siesa.

    Un trinquete que sobra-dispara se apaga con un `# noqa` y deja de medir, y
    ahí el costo es el mismo que el de no tenerlo. La propiedad de verdad es la
    **secuencia**: constante que termina en `''`, interpolación, constante que
    empieza con `''`.
    """
    hallados = []
    for nodo in ast.walk(ast.parse(fuente)):
        if not isinstance(nodo, ast.JoinedStr):
            continue
        v = nodo.values
        for i in range(len(v) - 2):
            izq, medio, der = v[i], v[i + 1], v[i + 2]
            if (isinstance(izq, ast.Constant) and isinstance(izq.value, str)
                    and izq.value.endswith("''")
                    and isinstance(medio, ast.FormattedValue)
                    and isinstance(der, ast.Constant) and isinstance(der.value, str)
                    and der.value.startswith("''")):
                hallados.append(nodo.lineno)
                break
    return hallados


class TestNingunFiltroSeArmaAMano:
    def test_ningun_archivo_delimita_por_su_cuenta(self):
        culpables = []
        for rel, fuente in _fuentes():
            for linea in _literales_a_mano(fuente):
                culpables.append(f'{rel}:{linea}')
        assert not culpables, (
            f'{len(culpables)} filtro(s) de Siesa armados a mano: '
            f'{culpables}.\nUsar `from app.services.siesa_filtro import lit`: '
            f'f"campo = {{lit(valor)}}". El saneo a mano ya estuvo en 2 de 37 '
            f'sitios y esa es exactamente la forma que se quiere impedir.')

    def test_el_detector_ve_una_reintroduccion(self):
        """**Detector ciego.** Sin esto, «0 hallazgos» no significa nada: un
        detector roto marca todo limpio y se lee igual que un repo sano."""
        recaida = "params = f\"f120_referencia = ''{referencia}''\"\n"
        assert _literales_a_mano(recaida) == [1], (
            'el detector no vio un filtro armado a mano — está midiendo otra '
            'cosa')

    def test_el_detector_no_marca_la_forma_correcta(self):
        """Y el otro lado: si marcara la forma buena, el trinquete se apagaría
        con un `# noqa` y dejaría de medir."""
        buena = "params = f'f120_referencia = {lit(referencia)}'\n"
        assert _literales_a_mano(buena) == []

    @pytest.mark.parametrize('fuente,por_que', [
        ("""msg = f"Código '{codigo}' no encontrado"\n""",
         'comillas a lados OPUESTOS de la interpolación — es un mensaje de '
         'error, no un filtro. Concatenar las constantes las pega y fabrica '
         'un `\'\'` que el fuente no tiene'),
        ("""sql = '(%s)' % ', '.join(f"'{x}'" for x in TIPOS)\n""",
         'una lista SQL local, una comilla por lado'),
        ("""err = f"'{codigo}' no existe en el maestro"\n""",
         'empieza con comilla simple y sigue con la interpolación'),
    ])
    def test_no_confunde_una_comilla_por_lado_con_el_delimitador(self, fuente,
                                                                 por_que):
        """Los tres casos reales que la primera versión marcó mal.

        Nueve falsos positivos —mensajes de error de `routes/siesa.py` y DDL
        de SQLite de `flota/`— por medir presencia en vez de adyacencia. Un
        trinquete que manda a arreglar lo que no está roto se apaga, y apagado
        cuesta lo mismo que no existir.
        """
        assert _literales_a_mano(fuente) == [], por_que

    def test_el_detector_no_se_atrapa_en_un_docstring(self):
        """La séptima vez que un detector de texto se atrapó solo fue el que
        medía esta misma clase de regla. Por AST, un docstring es una
        constante: no tiene partes formateadas."""
        doc = '"""Ejemplo: f\\"campo = \'\'{valor}\'\'\\" era la forma vieja."""\n'
        assert _literales_a_mano(doc) == []


class TestLoQueLitGarantiza:
    def test_pone_la_doble_comilla_simple(self):
        from app.services.siesa_filtro import lit
        assert lit('ARTESA898') == "''ARTESA898''"

    def test_rechaza_la_comilla_en_vez_de_limpiarla(self):
        """Limpiar en silencio **cambia la pregunta sin decirlo**: se consulta
        por un código que no es el que pidió el llamador y el número vuelve
        con cara de bueno. `get_inventario_fecha` alimenta el ajuste de
        inventario que se manda por el 142951 — y un ajuste no lo reclama
        nadie."""
        from app.services.siesa_filtro import lit
        with pytest.raises(ValueError, match='carácter inválido'):
            lit("ARTESA898'' OR 1=1 --")

    def test_rechaza_el_vacio(self):
        """`campo = ''''` no es «buscá el que no tiene código»: es un filtro
        que no filtra, y devuelve un universo que nadie pidió."""
        from app.services.siesa_filtro import lit
        for vacio in ('', '   ', None):
            with pytest.raises(ValueError, match='sin valor'):
                lit(vacio)

    def test_rechaza_control_chars(self):
        from app.services.siesa_filtro import lit
        with pytest.raises(ValueError, match='carácter inválido'):
            lit('ABC\ndef')

    def test_rechaza_el_comodin_porcentaje(self):
        """**Ensancha la consulta sin usar ninguna comilla.** Estos filtros
        aceptan `LIKE` (lecciones del Gestor de Cartera, 2026-08-19), así que
        `f120_referencia LIKE ''%''` trae el catálogo entero. La primera
        versión de `lit()` solo miraba la comilla y lo dejaba pasar.
        """
        from app.services.siesa_filtro import lit
        with pytest.raises(ValueError, match='carácter inválido'):
            lit('ARTESA%')

    def test_el_guion_bajo_SI_pasa_aunque_sea_comodin(self):
        """`_` también es comodín de `LIKE` —de un solo carácter— y aun así se
        deja pasar: **es un carácter legítimo de identificador**, y está en la
        lista blanca del propio Gestor (`^[A-Za-z0-9_.\\-]{1,64}$`).

        Prohibirlo cambiaría un riesgo teórico —hoy ningún filtro del WMS usa
        `LIKE`, se verificó— por uno real: rechazar códigos que existen. Es la
        misma cuenta que hace `%` al revés, donde ningún código legítimo lo
        lleva.
        """
        from app.services.siesa_filtro import lit
        assert lit('ARTESA_898') == "''ARTESA_898''"

    def test_el_mensaje_nombra_el_campo_cuando_se_lo_dan(self):
        from app.services.siesa_filtro import lit
        with pytest.raises(ValueError, match='f120_referencia'):
            lit('', campo='f120_referencia')

    def test_no_recorta_lo_legitimo(self):
        """Siesa admite códigos de barras alfanuméricos libres — el maestro
        real tiene 'F1P' como código del ítem ARTESA898. Un saneo que
        recortara caracteres válidos rompería el escaneo."""
        from app.services.siesa_filtro import lit
        for bueno in ('F1P', '7702001234567', 'PAPEL-SP.9218', 'ARTESA898'):
            assert lit(bueno) == f"''{bueno}''"


class TestLaFuncionMuertaNoVuelve:
    def test_get_item_por_barras_sigue_borrada(self):
        """No tenía ningún llamador: el escaneo resuelve contra el catálogo
        local, no contra Siesa en vivo. Se conserva el test invertido para que
        reaparecer sea rojo y no silencio."""
        fuente = (_RAIZ / 'app/services/connekta_gateway.py').read_text()
        arbol = ast.parse(fuente)
        nombres = {n.name for n in ast.walk(arbol)
                   if isinstance(n, ast.FunctionDef)}
        assert 'get_item_por_barras' not in nombres, (
            'volvió `get_item_por_barras`. Si algo la necesita de verdad, '
            'revisar primero por qué el catálogo local no alcanza.')
