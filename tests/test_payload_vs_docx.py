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

Que las secciones del payload tengan **los mismos campos** que la plantilla
JSON del DOCX, y en el mismo orden. No los valores — esos dependen del caso.

## ⚠️ Una premisa que este archivo daba por probada y NO lo está

Se repitió toda la semana —y está escrita en varios sitios— que «en un plano
posicional el orden de las claves ES la posición». **Eso no está demostrado.**

Lo que el episodio del 142888 demuestra es que **omitir** campos acorta el
registro y desalinea lo que sigue. No demuestra que el orden de las claves del
JSON fije el desplazamiento: Connekta podría mapear por nombre y armar el plano
en el orden que él sabe.

Y hay **contraevidencia en producción**: dos conectores mandan sus secciones en
un orden distinto al de su spec —uno de ellos con seis campos extra
intercalados— y los dos corrieron en vivo con éxito.

**Consecuencia práctica.** La comprobación de campos faltantes es sólida y hay
que conservarla: el defecto del 142888 fue real y costó que ningún recibo de
caja llegara nunca. La de ORDEN se conserva por precaución —cuesta poco y
alinea con el spec— pero **no debe usarse para justificar trabajo de
reordenamiento** hasta que el proveedor confirme si el mapeo es por nombre o
por posición. Es una pregunta de una línea.

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
        # El nombre de sección admite espacios y puntos: el 251126 los usa
        # (`"Docto. ventas comercial"`). Un `\w+` los deja fuera y el parser
        # devuelve secciones vacías — que es el modo de fallo peligroso, porque
        # comparar contra una lista vacía pasa siempre.
        m = re.match(r'"([\w. ]+)":\s*\[', l)
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


#: Tipos de dato del DOCX. Sirven de ancla: en la tabla de campos, el nombre
#: siempre viene seguido de su tipo.
_TIPOS_DOCX = {'Entero', 'Alfanumerico', 'Alfanumérico', 'Decimal', 'Fecha',
               'DecimalConSigno'}


def _tabla_docx(nombre_archivo: str) -> dict:
    """`{seccion: [(campo, obligatorio, pos, ancho)]}` desde la tabla del DOCX.

    Segundo formato de spec. El 142888 y el 142882 traen una plantilla JSON; el
    142946 trae una **tabla con posición y ancho por campo**, que es más
    informativa —permite calcular el largo del registro— y no se puede leer con
    el mismo parser.

    Que existan dos formatos no es un detalle: es la razón por la que «verificar
    contra el spec» se hacía a ojo y terminaba siendo «verificar contra lo que
    ya escribimos».
    """
    ruta = _SPECS / nombre_archivo
    assert ruta.exists(), f'falta el spec {nombre_archivo}'
    xml = zipfile.ZipFile(ruta).read('word/document.xml').decode('utf-8')
    texto = unescape(re.sub(r'<[^>]+>', '', re.sub(r'</w:p>', '\n', xml)))
    L = [re.sub(r'\s+', ' ', l).strip() for l in texto.split('\n') if l.strip()]

    try:
        inicio = L.index('Request Body') + 1
    except ValueError:
        inicio = 0

    # Una sección es una palabra sola, capitalizada, de 5+ letras. Descarta
    # 'Dep' (3) y las descripciones, que llevan espacios.
    def es_seccion(l):
        return bool(re.fullmatch(r'[A-Z][a-zA-Z]{4,}', l)) and l not in _TIPOS_DOCX

    secciones, actual, i = {}, None, inicio
    while i < len(L):
        if es_seccion(L[i]):
            actual = L[i]
            secciones.setdefault(actual, [])
            i += 1
            continue
        if (actual and re.fullmatch(r'[A-Za-z0-9_]+', L[i])
                and i + 1 < len(L) and L[i + 1] in _TIPOS_DOCX):
            resto = L[i + 2:i + 8]
            nums = [x for x in resto if re.fullmatch(r'\d+', x)]
            oblig = next((x for x in resto if x in ('Si', 'Sí', 'No')), '?')
            secciones[actual].append((
                L[i], oblig,
                int(nums[0]) if nums else None,
                int(nums[1]) if len(nums) > 1 else None))
            i += 2
            continue
        i += 1
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
            '\nEl orden no coincide con el DOCX. Se mantiene el orden del spec '
            'por precaución, NO porque esté probado que importe — ver la nota '
            'del encabezado de este archivo.')

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


class TestNotaCredito251126:
    """El conector que de verdad usa Devolución de Cliente — **no el 142946**.

    Los dos flujos de nota crédito usan conectores dinámicos, no el estándar:

        NOTA_CREDITO_FACTURA            (liquidación) → 250696
        NOTA_CREDITO_DEVOLUCION_CLIENTE (devolución)  → 251126

    Importa porque las respuestas del consultor sobre «el 142946 autogenera la
    Devolución de Ventas y reversa inventario y costo» son sobre un conector
    que no disparamos en ningún flujo. Lo que sí se puede afirmar del 251126 es
    lo que dice su propio spec, y su sección `Movimientos` **declara destino de
    inventario**: `f470_id_bodega`, `f470_id_ubicacion_aux`, `f470_id_lote`,
    `f470_id_motivo`, `f470_id_causal_devol`.

    Una nota crédito puramente financiera no necesitaría saber a qué bodega ni
    a qué ubicación vuelve la mercancía. Esta sí lo lleva, y nosotros lo
    mandamos — verificado 2026-08-13, 34/34 campos.

    Como el 142946, el encabezado se arma en helpers compartidos; buscarlo solo
    en el cuerpo del trigger da un falso «faltan 3».
    """

    _DOCX = '251126 - PapeleriaMedellin_NotaCredito_CrearCruzar_WMS_v2.docx'

    #: Los campos que hacen que esta NC afecte inventario y no solo cartera.
    #: Si alguno desaparece, la mercancía devuelta deja de tener destino
    #: declarado — y eso no se ve en cartera, se ve en el inventario meses
    #: después.
    _CAMPOS_DE_INVENTARIO = (
        'f470_id_bodega', 'f470_id_ubicacion_aux', 'f470_id_lote',
        'f470_id_motivo', 'f470_id_causal_devol', 'f470_rowid_movto',
    )

    @pytest.fixture(scope='class')
    def spec(self):
        return _plantilla_docx(self._DOCX)

    @pytest.fixture(scope='class')
    def fuente(self):
        t = _GATEWAY.read_text(encoding='utf-8')

        def cuerpo(nombre):
            i = t.find(f'    def {nombre}')
            assert i != -1, f'no existe {nombre} — ¿se renombró?'
            j = t.find('\n    def ', i + 10)
            return t[i:j if j > 0 else i + 6000]

        return (cuerpo('trigger_nota_factura_crear_cruzar')
                + cuerpo('_build_header_docto_ventas_nc')
                + cuerpo('_build_transportador_vacio'))

    @pytest.mark.parametrize('seccion',
                             ['Docto. ventas comercial', 'Cuotas CxC', 'Movimientos'])
    def test_no_falta_ningun_campo(self, spec, fuente, seccion):
        faltan = [c for c in spec[seccion] if f"'{c}'" not in fuente]
        assert not faltan, f'\n{seccion}: faltan {len(faltan)} campos del DOCX: {faltan}'

    def test_la_NC_declara_destino_de_inventario(self, fuente):
        """Lo que distingue una NC que devuelve mercancía de una que solo
        mueve plata."""
        faltan = [c for c in self._CAMPOS_DE_INVENTARIO if f"'{c}'" not in fuente]
        assert not faltan, (
            f'\nLa NC dejó de declarar destino de inventario: {faltan}\n'
            f'Sin bodega/ubicación, la mercancía devuelta no vuelve a ningún '
            f'lado en Siesa — y el costo queda debitado sin venta.')

    def test_el_spec_trae_las_tres_secciones(self, spec):
        """Si el parser dejara de reconocer nombres con espacios y puntos
        —`Docto. ventas comercial`— devolvería secciones vacías y los tests de
        arriba compararían contra nada."""
        assert set(spec) >= {'Docto. ventas comercial', 'Cuotas CxC', 'Movimientos'}
        assert len(spec['Movimientos']) == 19


class TestNotaFactura142946:
    """El único de los tres que **sí se ejercitó en vivo** — y el único completo.

    Verificado el 2026-08-11: 22/22 en `Doctoventascomercial`, 27/27 en
    `Movimientos`. Sin defectos.

    Eso no es casualidad y es la lección del episodio: 142946 se probó contra
    Siesa real (NCE-00000050 sobre FEW-1463, y después el 251126 con FEW-1466).
    Los otros dos —142888 y 142882— nunca corrieron, y a los dos les faltaban
    campos. **«Verificado contra el spec» sin una corrida real no significaba
    nada**, porque el test que decía verificarlo comparaba el código consigo
    mismo.

    Se fija acá para que siga completo, no porque haya algo que arreglar.

    El encabezado se arma en helpers compartidos (`_build_header_docto_ventas_nc`,
    `_build_transportador_vacio`) y no inline — buscarlo solo en el cuerpo del
    trigger da un falso «faltan 21 campos». La primera medición dio eso; el dato
    era mío, no del código.
    """

    _DOCX = '142946 - API_v1_Ventas_Comercial_NotaFactura 428509.docx'

    @pytest.fixture(scope='class')
    def spec(self):
        return _tabla_docx(self._DOCX)

    @pytest.fixture(scope='class')
    def fuente(self):
        t = _GATEWAY.read_text(encoding='utf-8')

        def cuerpo(nombre):
            i = t.find(f'    def {nombre}')
            assert i != -1, f'no existe {nombre} — ¿se renombró?'
            j = t.find('\n    def ', i + 10)
            return t[i:j if j > 0 else i + 6000]

        return (cuerpo('trigger_nota_factura(')
                + cuerpo('_build_header_docto_ventas_nc')
                + cuerpo('_build_transportador_vacio'))

    @pytest.mark.parametrize('seccion', ['Doctoventascomercial', 'Movimientos'])
    def test_no_falta_ningun_campo(self, spec, fuente, seccion):
        faltan = [(c, ob, p, a) for c, ob, p, a in spec[seccion]
                  if f"'{c}'" not in fuente]
        assert not faltan, (
            f'\n{seccion}: faltan {len(faltan)} campos del DOCX:\n'
            + '\n'.join(f'  {"OBLIGATORIO" if ob in ("Si", "Sí") else "opcional"}  '
                        f'{c} (pos {p}, ancho {a})' for c, ob, p, a in faltan))

    def test_el_spec_trae_posiciones_y_anchos(self, spec):
        """Este DOCX permite calcular el largo del registro. Si el parser
        dejara de leer posiciones, los tests seguirían pasando sobre listas
        vacías."""
        campos = spec['Doctoventascomercial']
        assert len(campos) == 22
        assert all(p and a for _, _, p, a in campos), 'hay campos sin pos/ancho'
        fin = max(p + a - 1 for _, _, p, a in campos)
        assert fin == 522, f'el registro terminaba en 522 y ahora en {fin}'


class TestFacturaDesdeRemision142943:
    """El único conector en producción cuyo spec no estaba en el repo.

    Llegó el 2026-08-13, después de que una prueba manual en el escritorio de
    Siesa fallara con **47891 (caja)** sobre un pedido C01. Trae dos cosas que
    no se sabían.

    ## 1. `f462_id_caja` existe, y lo mandamos en `None`

    El spec lo declara «Obligatoria si hay recaudos» (Dep, 3 chars, pos 2982).
    Una FE de contado **tiene recaudos** — por eso el escritorio la pidió. El
    escritorio la resuelve con las preferencias del usuario; el conector no es
    un usuario y no tiene preferencias.

    El valor ya existe en esta misma clase: `_co_caja_map` (línea ~151), que
    usa el 142888. **No se cambia todavía a propósito** — mandar la caja hace
    que Siesa registre el recaudo al facturar, y el WMS vuelve a registrarlo en
    la liquidación con el 142888. Cuál de los dos sobra es una decisión de
    diseño, no un campo faltante. Ver `docs/arranque_produccion.md`.

    ## 2. No hay sección `Movimientos` — no se puede facturar parcial

    Secciones: Inicial, Doctoventascomercial, RelacionDoctos, CuotasCxC, Final.
    `RelacionDoctos` referencia la remisión **por encabezado** (CO + tipo +
    consecutivo); en ningún lado hay línea ni cantidad.

    Eso decide una discusión abierta: «FE por lo entregado» **no es posible con
    este conector**. La FE factura la remisión completa. Facturar menos exige
    que la remisión sea menor —moverla a la liquidación, y con ella la descarga
    de inventario— o un documento de devolución de remisión que hoy no tiene
    conector. `F460_ID_TIPO_DOCTO` dice «remision **o devolucion**» y
    `RelacionDoctos` es una lista, así que el camino existe en el spec; lo que
    no existe es el conector que cree esa devolución.
    """

    _DOCX = '142943.docx'
    _SECCIONES = ('Doctoventascomercial', 'RelacionDoctos', 'CuotasCxC')

    @pytest.fixture(scope='class')
    def spec(self):
        return _plantilla_docx(self._DOCX)

    @pytest.fixture(scope='class')
    def fuente(self):
        t = _GATEWAY.read_text(encoding='utf-8')
        i = t.find('    def trigger_factura_desde_remision')
        assert i != -1, 'no existe trigger_factura_desde_remision — ¿se renombró?'
        j = t.find('\n    def ', i + 10)
        return t[i:j if j > 0 else len(t)]

    @pytest.mark.parametrize('seccion', _SECCIONES)
    def test_no_falta_ningun_campo(self, spec, fuente, seccion):
        faltan = [c for c in spec[seccion] if f"'{c}'" not in fuente]
        assert not faltan, f'\n{seccion}: faltan {len(faltan)} campos del DOCX: {faltan}'

    def test_el_spec_no_tiene_movimientos(self, spec):
        """Si algún día aparece, «FE por lo entregado» pasa a ser posible y hay
        que volver a leer el spec entero — no seguir asumiendo que no se puede.

        Falla también si el parser deja de leer el DOCX: sin las otras cuatro
        secciones, «no hay Movimientos» sería cierto por vacío.
        """
        assert set(spec) == {'Inicial', 'Doctoventascomercial',
                             'RelacionDoctos', 'CuotasCxC', 'Final'}, (
            f'\nCambiaron las secciones del 142943: {sorted(spec)}\n'
            f'Si apareció `Movimientos`, este conector ya puede facturar '
            f'cantidades parciales y el rediseño del flujo de contado cambia.')
        assert len(spec['Doctoventascomercial']) == 50
        # El vínculo con la remisión es de encabezado: 4 campos del documento
        # nuevo + 3 del referenciado. Ninguna cantidad.
        assert len(spec['RelacionDoctos']) == 7

    def test_la_caja_no_se_hardcodea(self, fuente):
        """`f462_id_caja` puede dejar de ser `None` — pero no con un literal.

        La caja por CO ya está en `_co_caja_map` y la usa el 142888. Una
        segunda tabla de cajas es exactamente el patrón que la Regla 0 prohíbe:
        el mismo concepto en dos sitios diverge, y acá divergir significa que
        el recaudo de la factura y el del recibo caen en cajas distintas.
        """
        m = re.search(r"'f462_id_caja':\s*([^,\n]+)", fuente)
        assert m, 'desapareció f462_id_caja del payload del 142943'
        valor = m.group(1).strip()
        assert valor == 'None' or '_co_caja_map' in valor, (
            f'\nf462_id_caja = {valor}\n'
            f'Si ya se decidió mandar caja, tiene que salir de `_co_caja_map` '
            f'—el mismo mapa que usa el 142888— no de un literal ni de un mapa '
            f'nuevo.')


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


class TestSoloSeUsanLosDosConectoresRegistrados:
    """Las notas crédito van por 251126 y 251546. Nada más.

    Confirmado con el consultor el 2026-08-13: en Connekta solo están
    registrados esos dos para nota crédito. Ni el 142946 estándar ni el clon
    250696 están en uso.

    Y sin embargo `NOTA_CREDITO_FACTURA` (liquidación de ruta) disparaba
    `trigger_nota_factura` → **250696**, que además SOLO CREA: dejaba la
    cartera sin cruzar y alguien tenía que apretar «Automático» en Siesa por
    cada devolución de ruta.

    Ese camino nunca llegó a hacer POST —el job 440 fallaba antes, resolviendo
    la factura— así que el defecto estaba tapado por otro defecto. Es el
    escenario más incómodo: dos errores donde el primero esconde al segundo, y
    arreglar el primero destapa el segundo en producción.
    """

    _JOBS = _RAIZ / 'app' / 'services' / 'siesa_job_service.py'

    def test_ningun_job_dispara_el_conector_que_solo_crea(self):
        fuente = self._JOBS.read_text(encoding='utf-8')
        llamadas = re.findall(r'connekta\.(trigger_nota_factura\w*)\(', fuente)
        solo_crea = [c for c in llamadas if c == 'trigger_nota_factura']
        assert not solo_crea, (
            '\nUn job volvió a llamar `trigger_nota_factura` (250696), que solo '
            'crea la NC y deja la cartera sin cruzar — además de no estar '
            'registrado en Connekta.\n'
            'Usar `trigger_nota_factura_crear_cruzar` (251126).')

    def test_las_dos_notas_credito_usan_el_mismo_conector(self):
        """Una política, una función. Que liquidación y devolución usaran
        conectores distintos para el mismo hecho contable fue lo que dejó a una
        de las dos sin cruzar la cartera durante meses."""
        fuente = self._JOBS.read_text(encoding='utf-8')
        llamadas = re.findall(r'connekta\.(trigger_nota_factura\w*)\(', fuente)
        assert llamadas, 'no se encontró ninguna llamada — ¿cambió el nombre?'
        assert set(llamadas) == {'trigger_nota_factura_crear_cruzar'}, (
            f'las NC van por conectores distintos: {sorted(set(llamadas))}')

    def test_las_dos_ramas_producen_el_valor_del_cruce(self):
        """`valor_cruce` sale de `f470_vlr_neto_prorrateado`. Si solo lo
        produjera la rama parcial, una NC total cruzaría por CERO y la cartera
        quedaría abierta sin que nada avisara — el mismo silencio que ya costó
        una vez con `F353_VLR_CRUCE` en bruto."""
        fuente = self._JOBS.read_text(encoding='utf-8')
        i = fuente.find('def _construir_lineas_nc')
        j = fuente.find('\ndef ', i + 10)
        cuerpo = fuente[i:j if j > 0 else i + 4000]
        rama_total = cuerpo[cuerpo.find('if es_total:'):cuerpo.find('devueltos_map')]
        assert 'f470_vlr_neto_prorrateado' in rama_total, (
            'la rama es_total dejó de producir el neto — una NC total cruzaría '
            'por 0 y la factura quedaría abierta en cartera')
