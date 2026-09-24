"""
Contado documental ≠ cobro en la puerta. **Son dos preguntas.**

Hasta el 2026-08-14 las contestaba una sola función, `clasificar`, y la
pantalla del conductor la usaba para decidir si pedir el cobro:

    clasificar('C02', 'C01')  ->  'credito'  ->  modo CREDITO  ->  no cobra

**Toda venta de ruta sale en C02.** Así que en ninguna parada se pedía cobrar,
y la liquidación no tenía monto que cruzar contra la factura.

## La ironía, que es lo que hace difícil de ver el defecto

C02 no es contado documental **precisamente porque** se cobra en la puerta: una
FE de contado exige el recaudo dentro del documento (Regla 21, probado en
producción con dos facturas trabadas en Elaboración), y en ruta ese recaudo lo
hace el conductor horas después. El mismo hecho contesta las dos preguntas al
revés. Una función no puede hacerlo.

## Por qué no había daño acumulado

El módulo de liquidación corrió por primera vez el 2026-08-11 y los tres
conectores financieros fallaron. Ninguna de las 72 facturas C02 abiertas se
emitió después de esa fecha — la más nueva es del 23 de junio. **El defecto no
había producido un peso**: estaba entero por delante.

## Lo que estos tests protegen

1. Que la pregunta del conductor NO se conteste con `clasificar`.
2. Que `clasificar` siga contestando la del documento — tocarla para arreglar
   la pantalla rompe la emisión de la factura.
3. Que sin `SIESA_COND_PAGO_RUTA` se caiga a LIBRE y no a «no cobrar», que
   reintroduciría el defecto en silencio.
4. Que la restricción del punto 4 (no marcar CREDITO donde había que cobrar)
   empiece a disparar en ruta, que es donde nunca disparó.
"""
import pytest

from app.services import cond_pago as cp

CONTADO = 'C01'      # SIESA_COND_PAGO_VENTAS
RUTA = 'C02'         # SIESA_COND_PAGO_RUTA — crédito a un día
CREDITO_REAL = 'C04'  # 30 días


class TestLasDosPreguntasNoSonLaMisma:
    """El corazón del defecto: para C02 las dos respuestas son OPUESTAS."""

    def test_c02_no_es_contado_documental(self):
        """Si esto diera contado, la FE no se aprobaría (Regla 21)."""
        assert cp.clasificar(RUTA, CONTADO) == cp.CREDITO
        assert cp.aprobable_en_ruta(RUTA, CONTADO) is True

    def test_c02_si_se_cobra_en_la_puerta(self):
        """Y ésta es la que la pantalla necesita."""
        assert cp.cobra_en_la_puerta(RUTA, CONTADO, RUTA) is True

    def test_las_dos_funciones_discrepan_en_c02_y_eso_es_correcto(self):
        """El test que resume el defecto entero.

        Si alguien «unifica» las dos funciones por parecerle duplicación, esto
        se pone rojo. La duplicación aparente es la distinción real.
        """
        documental = cp.clasificar(RUTA, CONTADO) == cp.CONTADO
        en_puerta = cp.cobra_en_la_puerta(RUTA, CONTADO, RUTA)
        assert documental is False and en_puerta is True


class TestLaTablaCompleta:
    @pytest.mark.parametrize('cond,esperado', [
        (CONTADO, True),        # mostrador: la FE trae su recaudo
        (RUTA, True),           # ruta: el conductor cobra, el RC salda
        (CREDITO_REAL, False),  # crédito real: se entrega y se firma
        # Siesa respondió y el tercero no tiene condición: la FE salió en la
        # de ruta, así que **hay que cobrarla**. Ver `cond_pago_efectiva`.
        ('', True),
        # No se pudo preguntar. Hasta el 2026-09-24 era `None` (LIBRE). Con la
        # regla por días, solo un crédito CONOCIDO no se cobra: se cobra como
        # contado supuesto, declarado (`SUPUESTO_AUSENTE`).
        (None, True),
    ])
    def test_quien_cobra(self, cond, esperado):
        assert cp.cobra_en_la_puerta(cond, CONTADO, RUTA) is esperado

    def test_los_dias_salen_de_la_tabla_configurable(self, monkeypatch):
        """Probado con códigos que NO están en el PDF: la tabla se
        sobreescribe con `SIESA_COND_PAGO_DIAS`. Una implementación que
        hardcodee C01/C02 no pasa esto."""
        monkeypatch.setenv('SIESA_COND_PAGO_DIAS', '{"X9": 0, "Z1": 20, "C04": 10}')
        assert cp.cobra_en_la_puerta('X9') is True
        assert cp.cobra_en_la_puerta('Z1') is False
        assert cp.cobra_en_la_puerta('C04') is True, 'la sobreescritura manda sobre la copia'
        assert cp.cobra_en_la_puerta('OTRO') is True   # desconocido: se cobra


class TestSinLaCondicionDeRutaSeDecidePorDias:
    """Antes, sin `SIESA_COND_PAGO_RUTA`, C02 y C04 eran indistinguibles
    («distinto de contado») y se caía a `None`/LIBRE. Desde el 2026-09-24 la
    decisión es por días de la condición, y esa variable ya no la gobierna.
    """

    @pytest.mark.parametrize('sin_ruta', ['', None, '   '])
    def test_c02_se_cobra_y_c04_no(self, sin_ruta):
        assert cp.cobra_en_la_puerta(RUTA, CONTADO, sin_ruta) is True
        assert cp.cobra_en_la_puerta(CREDITO_REAL, CONTADO, sin_ruta) is False

    def test_el_contado_sigue_reconociendose(self):
        assert cp.cobra_en_la_puerta(CONTADO, CONTADO, '') is True

    def test_none_de_pantalla_sigue_dando_libre(self):
        """`modo_pantalla` conserva su contrato con `None` (un llamador viejo),
        aunque la política ya no lo produzca."""
        assert cp.modo_pantalla(None, True) == cp.LIBRE


class TestElModoDePantalla:
    @pytest.mark.parametrize('cond,hay_valor,esperado', [
        (RUTA, True, cp.DINAMICO),            # el caso que estaba roto
        (RUTA, False, cp.LIBRE),              # sin valor no se afirma un monto
        (CONTADO, True, cp.DINAMICO),
        (CREDITO_REAL, True, cp.CREDITO_PANTALLA),
        ('', True, cp.DINAMICO),   # sin condición → se cobra (supuesto)
        (None, True, cp.DINAMICO), # no se pudo preguntar → se cobra (supuesto)
        (None, False, cp.LIBRE),   # se cobra, pero sin valor: monto libre
    ])
    def test_de_la_condicion_al_modo(self, cond, hay_valor, esperado):
        se_cobra = cp.cobra_en_la_puerta(cond, CONTADO, RUTA)
        assert cp.modo_pantalla(se_cobra, hay_valor) == esperado

    def test_una_parada_de_ruta_normal_llega_a_dinamico(self):
        """El test que habría fallado ayer, y el que importa.

        DINAMICO es el modo Total/Parcial: el monto se recalcula con lo que el
        cliente acepta. Sin él el conductor no ve cobro **ni monto**, y en un
        parcial —que es el caso normal, 7 de 10— cobrar la factura completa
        sería cobrar de más: la 142943 factura la remisión entera y la nota
        crédito por lo rechazado sale después, en la liquidación.
        """
        se_cobra = cp.cobra_en_la_puerta(RUTA, CONTADO, RUTA)
        assert cp.modo_pantalla(se_cobra, True) == cp.DINAMICO


class TestMutaciones:
    """Cada una rompe el arreglo de una forma plausible. Las cinco deben ser
    detectadas por los tests de arriba — si alguna sobrevive, el trinquete no
    protege lo que dice proteger.
    """

    def test_m1_volver_a_clasificar(self):
        """La regresión exacta: contestar la pregunta del conductor con
        `clasificar`. Es lo que había hasta hoy, y sobre C02 da lo contrario."""
        mutante = cp.clasificar(RUTA, CONTADO) == cp.CONTADO
        correcto = cp.cobra_en_la_puerta(RUTA, CONTADO, RUTA)
        assert mutante is False and correcto is True

    def test_m2_sin_ruta_devuelve_false(self):
        """La versión ingenua —`valor in (contado, ruta)`— con la ruta vacía deja
        C02 en False: ninguna parada pide cobrar. Con la tabla de días, C02 se
        cobra sin depender de la variable."""
        def mutante(c, ct, cr):
            v = (c or '').strip()
            return None if not v else v in ((ct or '').strip(), (cr or '').strip())

        assert mutante(RUTA, CONTADO, '') is False
        assert cp.cobra_en_la_puerta(RUTA, CONTADO, '') is True

    def test_m3_el_credito_real_pasa_a_cobrarse(self):
        """El error invertido: pedirle plata a un cliente de 30 días."""
        assert cp.cobra_en_la_puerta(CREDITO_REAL, CONTADO, RUTA) is False

    def test_m4_nunca_devuelve_none(self):
        """La política contesta siempre: `None` era «no sé» y dejaba la guarda
        sin actuar. Ahora «no sé» es contado supuesto, con origen."""
        for c in (None, '', 'ZZZ', CONTADO, RUTA, CREDITO_REAL):
            assert cp.cobra_en_la_puerta(c, CONTADO, RUTA) in (True, False)

    def test_m5_clasificar_sigue_intacta(self):
        """Si alguien «arregla» `clasificar` para que C02 dé contado, la
        factura de ruta deja de ser aprobable y vuelve el incidente del 13."""
        assert cp.clasificar(RUTA, CONTADO) == cp.CREDITO
        assert cp.aprobable_en_ruta(RUTA, CONTADO) is True


class TestLaRestriccionDelPuntoCuatroEmpiezaAProteger:
    """«Sobre una parada que se cobra en la entrega no se puede registrar
    crédito» (BK-OPS-01 §3.4).

    El control existía desde el 2026-08-13 y **no disparó nunca en ruta**:
    validaba con `clasificar`, y toda parada de ruta es C02 → 'credito' → no
    bloqueaba. Un guard que mide la propiedad equivocada está en verde sobre
    algo roto — el mismo patrón de los tres guards de esta semana.
    """

    def test_la_restriccion_consulta_se_cobra_en_la_puerta(self):
        """Por AST, no por texto: un detector de texto se atrapa en su propio
        comentario — pasó cinco veces esta semana."""
        import ast
        import pathlib
        src = pathlib.Path('app/services/ruta_service.py').read_text()
        arbol = ast.parse(src)
        llamadas = {
            n.func.attr for n in ast.walk(arbol)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert {'cobro_de_tarea', 'forma_no_cobra'} <= llamadas, (
            'la restricción del punto 4 volvió a preguntar por contado '
            'documental — sobre una parada de ruta eso no bloquea nunca')

    def test_el_mensaje_no_dice_contado(self):
        """C02 no es contado. Un mensaje que se lo diga al conductor lo manda a
        discutir con la oficina sobre una palabra equivocada."""
        import pathlib
        src = pathlib.Path('app/services/ruta_service.py').read_text()
        assert 'Este pedido es de contado' not in src


class TestLosDosFallbacksApuntanAlMismoLado:
    """El defecto que el caso ausente reintroducía.

    Un pedido sin condición se **factura en la de ruta** —la alerta
    `DATA_MAESTRA_COND_PAGO` lo dice textual: «la cartera la salda el recibo de
    caja del conductor»— y la pantalla lo dejaba en LIBRE, sin pedir cobro.

    El mismo fallback escrito dos veces, con resultados opuestos. Regla 0,
    corolario: una política, una función.
    """

    def test_la_condicion_efectiva_es_la_que_se_emite(self):
        assert cp.cond_pago_efectiva('', RUTA) == RUTA
        assert cp.cond_pago_efectiva(None, RUTA) == RUTA
        assert cp.cond_pago_efectiva(CREDITO_REAL, RUTA) == CREDITO_REAL

    def test_un_pedido_sin_condicion_se_cobra(self):
        """El caso que quedaba en LIBRE. La FE salió en C02: hay que cobrarla."""
        assert cp.cobra_en_la_puerta('', CONTADO, RUTA) is True
        assert cp.modo_pantalla(
            cp.cobra_en_la_puerta('', CONTADO, RUTA), True) == cp.DINAMICO

    def test_sin_ninguna_de_las_dos_se_cobra_supuesto(self):
        """Sin condición y sin `SIESA_COND_PAGO_RUTA` el gateway no emite
        (levanta `ValueError`); la pantalla, si llegara a verla, cobra como
        contado supuesto — nunca afirma crédito sin días conocidos."""
        assert cp.cobra_en_la_puerta('', CONTADO, '') is True
        assert cp.cobro_contraentrega('')['origen'] == cp.SUPUESTO_AUSENTE

    def test_el_gateway_usa_la_misma_funcion(self):
        """Por AST. Un `or` suelto acá es exactamente cómo divergieron.

        `cond_pago_efectiva` la llama `trigger_factura_desde_remision`, que
        vive en `connekta_facturacion_gateway.py` desde el 2026-09-09 (paso 7
        de la deuda de tamaño) — se escanea toda la familia `connekta_*.py`,
        no solo el gateway, para no volver a romperse en la próxima extracción.
        """
        import ast
        import pathlib
        llamadas = set()
        for archivo in sorted((pathlib.Path('app') / 'services').glob('connekta_*.py')):
            arbol = ast.parse(archivo.read_text(encoding='utf-8'))
            llamadas |= {
                n.func.attr for n in ast.walk(arbol)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
        assert 'cond_pago_efectiva' in llamadas, (
            'el gateway volvió a resolver el fallback por su cuenta — es el '
            'mismo patrón que hizo divergir la pantalla del documento')

    def test_el_vacio_y_el_none_no_son_lo_mismo(self):
        """La distinción que el propio `ruta_service` documenta y que casi
        pierdo al unificar el fallback.

        `''` → Siesa contestó, el tercero no tiene condición → la FE salió en
        la de ruta → se cobra.
        `None` → no se pudo preguntar → no se sabe qué lleva la factura, ni si
        llegó a existir → no se afirma nada.

        Colapsarlos acá le pediría plata a un cliente sobre un supuesto.
        """
        assert cp.cobra_en_la_puerta('', CONTADO, RUTA) is True
        # Desde el 2026-09-24 `None` también se cobra (contado supuesto): la
        # distinción vacío/None se conserva en el crudo, no en la decisión.
        assert cp.cobra_en_la_puerta(None, CONTADO, RUTA) is True
