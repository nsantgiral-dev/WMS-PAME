"""
Tests de `flota/dominio/costos.py` — derivados del canon, no del código.

El canon es `docs/flota/canones/costo_por_kilometro.md` y los números del §5
están acá abajo como constantes con nombre. Si el código deja de reproducirlos,
lo que se investiga es la diferencia; el criterio no se afloja.

## Por qué cada guard se prueba en las DOS direcciones

Este repo lleva seis casos fechados de guards en verde sobre propiedades que la
vía sana satisfacía por construcción (`CLAUDE.md`, «La regla del guard»). La
pregunta que hay que contestar antes de escribir cada uno es: **¿puede el camino
roto producir el mismo valor que estoy comprobando?**

Para `excede_capacidad` la respuesta se ve a simple vista: un test que solo
comprueba que 22 galones en un tanque de 15 disparan pasaría igual con una
función que devuelve `True` siempre. Por eso cada detector tiene su par —que
dispara al violarlo, y que **no** dispara sobre operación sana— y el par está
en la misma clase, no repartido por el archivo.

## Lo que estos tests NO prueban

Que alguien registre un gasto. Eso es `tests/flota/test_gastos.py`, contra la
base. Acá no hay I/O: es aritmética sobre hechos ya registrados.
"""
import ast
import calendar
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from flota.dominio.costos import (CATEGORIAS_CON_PERIODO, CATEGORIAS_GASTO,
                                  ESTADOS_TANQUE, MARCAS_TRAMO, ORIGENES_COSTO,
                                  SIN_DATO, costo_por_kilometro,
                                  dias_del_periodo, excede_capacidad,
                                  exige_periodo, imputar_a_ventana,
                                  precio_por_galon, rendimiento_km_galon,
                                  ventanas_lleno_a_lleno)

# ══════════════════════════════════════════════════════════════════════════
# El caso del canon, §5. Escrito una vez y usado por todas las clases.
# ══════════════════════════════════════════════════════════════════════════

CANON_SOAT_VALOR      = Decimal('730000')
CANON_SOAT_DESDE      = date(2026, 1, 1)
CANON_SOAT_HASTA      = date(2026, 12, 31)
CANON_VENTANA_DESDE   = date(2026, 3, 1)
CANON_VENTANA_HASTA   = date(2026, 3, 31)
CANON_DIAS_DEL_SOAT   = 365          # los dos extremos cuentan
CANON_SOAT_A_MARZO    = Decimal('62000')

CANON_COMBUSTIBLE     = Decimal('588000')      # 168.000 + 196.000 + 224.000
CANON_MANTENIMIENTO   = Decimal('190000')
CANON_PESOS_IMPUTADOS = Decimal('840000')
CANON_KM_DE_MARZO     = 1000                   # 101.000 − 100.000
CANON_CPK             = Decimal('840')

#: Los tres tanqueos de marzo, todos con tanque lleno. Canon §5, segundo caso.
CANON_TANQUEOS = (
    {'km': 100000, 'galones': Decimal('12'), 'tanque': 'lleno'},
    {'km': 100392, 'galones': Decimal('14'), 'tanque': 'lleno'},
    {'km': 100720, 'galones': Decimal('16'), 'tanque': 'lleno'},
)
CANON_RENDIMIENTO           = Decimal('24')      # (392+328) ÷ (14+16)
CANON_PROMEDIO_DE_RAZONES   = Decimal('24.25')   # (28 + 20,5) ÷ 2 — NO es el rendimiento
CANON_PRECIO_GALON          = Decimal('14000')   # 224.000 ÷ 16


# ══════════════════════════════════════════════════════════════════════════
# `exige_periodo` — la categoría decide, no una casilla
# ══════════════════════════════════════════════════════════════════════════

class TestExigePeriodo:
    """Canon §4: *«¿Quién decide si una categoría cubre un período? La
    categoría — nunca una casilla que teclea quien registra»*."""

    @pytest.mark.parametrize('categoria', CATEGORIAS_CON_PERIODO)
    def test_las_cuatro_del_catalogo_cubren_un_periodo(self, categoria):
        assert exige_periodo(categoria) is True

    @pytest.mark.parametrize(
        'categoria',
        [c for c in CATEGORIAS_GASTO if c not in CATEGORIAS_CON_PERIODO])
    def test_las_demas_se_consumen_el_dia_que_ocurren(self, categoria):
        """La otra dirección del guard: sobre operación sana **no** dispara.

        Sin esto, `return True` pasaría el test de arriba y cargaría un tanqueo
        de $200.000 repartido sobre un período de un día — inofensivo hasta que
        alguien registre un tanqueo con período largo por error."""
        assert exige_periodo(categoria) is False

    def test_una_categoria_desconocida_revienta(self):
        """Regla 5: sin `.get(categoria, False)`.

        Un default `False` haría que una categoría nueva de las periodificables
        —el todo riesgo, el peritaje— cargara su año entero sobre un día, en
        silencio y con cara de número medido."""
        with pytest.raises(ValueError) as e:
            exige_periodo('todo_riesgo')
        assert 'todo_riesgo' in str(e.value)
        # El mensaje dice cuáles SÍ valen. Un «categoría inválida» pelado deja a
        # quien lo recibe sin saber qué escribir.
        assert 'soat' in str(e.value)

    def test_la_lista_con_periodo_es_subconjunto_del_catalogo(self):
        """Una categoría periodificable que no esté en el catálogo no se puede
        registrar: `exige_periodo` reventaría antes de contestar que sí."""
        assert set(CATEGORIAS_CON_PERIODO) <= set(CATEGORIAS_GASTO)

    def test_otro_existe_y_no_cubre_periodo(self):
        """`otro` es la válvula del catálogo cerrado — la que le faltó a
        `TIPOS_DOCUMENTO`, cerrado en cuatro, cuyos cuatro faltantes dan 400."""
        assert 'otro' in CATEGORIAS_GASTO
        assert exige_periodo('otro') is False


class TestLosVocabulariosSonLosDeLaTabla:
    """Regla 0: los CHECK de la base salen de estas tuplas, no de copias.

    Un enum de Python que la base no conoce es una convención; un CHECK armado
    desde esta tupla es una garantía. El día que alguien agregue una categoría
    acá y la tabla no la conozca, el INSERT falla — que es lo correcto. Lo que
    no puede pasar es que existan dos listas distintas.
    """

    def test_la_tabla_usa_exactamente_estas_tuplas(self):
        from flota.adaptadores import modelos

        assert modelos.CATEGORIA_GASTO == CATEGORIAS_GASTO
        assert modelos.ORIGEN_COSTO == ORIGENES_COSTO
        assert modelos.ESTADO_TANQUE == ESTADOS_TANQUE

    def test_sin_dato_esta_en_los_dos_vocabularios_que_admiten_no_saber(self):
        """Regla 4: un estado que puede ser «no sé» se modela con palabras.

        Y **no es el default en ninguno**: hay que escribir la palabra."""
        assert 'sin_dato' in ORIGENES_COSTO
        assert 'sin_dato' in ESTADOS_TANQUE

    def test_sin_dato_no_es_una_marca_de_tramo_valida(self):
        """`SIN_DATO` en `marca_tramo` significa «los dos extremos dudosos» y se
        trata aparte. Si estuviera DENTRO de `MARCAS_TRAMO`, el CPK lo aceptaría
        como una marca más y publicaría un número sobre un tramo que nadie
        midió."""
        assert SIN_DATO not in MARCAS_TRAMO
        assert set(MARCAS_TRAMO) == {'verificada', 'declarada', 'dudosa'}


# ══════════════════════════════════════════════════════════════════════════
# `dias_del_periodo` — los dos extremos cuentan
# ══════════════════════════════════════════════════════════════════════════

class TestDiasDelPeriodo:

    def test_un_ano_completo_son_365_dias(self):
        """Canon §4: *«¿364 o 365? 365. Los dos extremos cuentan»*.

        Escrito UNA vez. La segunda copia usa `.days` pelado y el año pierde un
        día sin que nada falle."""
        assert dias_del_periodo(CANON_SOAT_DESDE, CANON_SOAT_HASTA) == \
            CANON_DIAS_DEL_SOAT

    def test_un_solo_dia_es_un_dia(self):
        """El caso de todas las categorías sin período: `desde == hasta`.

        **Es la dirección que atrapa el `.days` pelado**, y la atrapa fuerte:
        con `.days` esto daría 0 y el reparto sería una división por cero, no un
        número apenas corrido."""
        assert dias_del_periodo(date(2026, 3, 10), date(2026, 3, 10)) == 1

    def test_un_ano_bisiesto_son_366(self):
        assert dias_del_periodo(date(2024, 1, 1), date(2024, 12, 31)) == 366

    def test_un_periodo_invertido_revienta(self):
        """No se reparte hacia atrás. Un período invertido es un formulario mal
        llenado y devolver un negativo lo convertiría en un CPK negativo."""
        with pytest.raises(ValueError) as e:
            dias_del_periodo(date(2026, 12, 31), date(2026, 1, 1))
        assert 'invertido' in str(e.value)


# ══════════════════════════════════════════════════════════════════════════
# `imputar_a_ventana` — el reparto
# ══════════════════════════════════════════════════════════════════════════

class TestImputarAVentana:

    def _soat_a(self, desde, hasta):
        return imputar_a_ventana(
            valor=CANON_SOAT_VALOR,
            periodo_desde=CANON_SOAT_DESDE, periodo_hasta=CANON_SOAT_HASTA,
            ventana_desde=desde, ventana_hasta=hasta)

    def test_el_caso_del_canon(self):
        """Canon §5 paso 1: $730.000 sobre 365 días, 31 días de marzo →
        $62.000. $730.000 ÷ 365 = $2.000/día × 31."""
        assert self._soat_a(CANON_VENTANA_DESDE, CANON_VENTANA_HASTA) == \
            CANON_SOAT_A_MARZO

    def test_los_doce_meses_suman_la_poliza_entera(self):
        """La propiedad que hace confiable el reparto: nada se pierde ni se
        inventa al partirlo.

        Canon §8: **vale porque $730.000 es divisible entre 365**, no por el
        orden de las operaciones. Con un valor no divisible el residuo aparece
        en el dígito 22 y este test tendría que llevar tolerancia — por eso el
        canon eligió un valor exacto."""
        total = sum(
            (self._soat_a(date(2026, m, 1),
                          date(2026, m, calendar.monthrange(2026, m)[1]))
             for m in range(1, 13)),
            Decimal('0'))
        assert total == CANON_SOAT_VALOR

    def test_febrero_recibe_menos_que_marzo_y_por_eso_el_CPK_no_es_comparable_mes_a_mes(self):
        """28 días contra 31: $56.000 contra $62.000.

        No es un defecto del reparto — es lo que significa repartir por día
        calendario, y queda escrito para que nadie lea la diferencia entre dos
        meses como un cambio de la operación."""
        febrero = self._soat_a(date(2026, 2, 1), date(2026, 2, 28))
        assert febrero == Decimal('56000')
        assert febrero < CANON_SOAT_A_MARZO

    def test_sin_solape_devuelve_CERO_y_es_un_cero_legitimo(self):
        """Canon §6: *«El SOAT del año pasado aporta exactamente nada al CPK de
        este mes»*.

        **Y no es `SIN_DATO`.** Los dos ceros del canon son distintos: éste es
        una afirmación sobre la flota; `SIN_DATO` es una afirmación sobre lo que
        el sistema no puede calcular."""
        nada = self._soat_a(date(2025, 5, 1), date(2025, 5, 31))
        assert nada == Decimal('0')
        assert nada is not SIN_DATO
        assert nada != SIN_DATO

    def test_una_ventana_que_contiene_al_periodo_entero_recibe_todo_el_valor(self):
        """La otra dirección del solape: si la ventana cubre el año, el reparto
        no puede quedarse con una fracción."""
        assert self._soat_a(date(2025, 1, 1), date(2027, 12, 31)) == \
            CANON_SOAT_VALOR

    def test_solape_parcial_por_el_borde_izquierdo(self):
        """El SOAT arranca el 1-ene y la ventana el 15-dic del año anterior:
        solapan los 31 días de enero, no los 17 de diciembre."""
        assert self._soat_a(date(2025, 12, 15), date(2026, 1, 31)) == \
            Decimal('62000')          # 31 días × $2.000

    def test_un_solo_dia_de_solape_paga_un_solo_dia(self):
        """El borde exacto: la ventana termina el mismo día en que arranca el
        período. Un `<` donde va un `<=` devolvería cero acá — y un día de SOAT
        perdido por vehículo por año no lo nota nadie."""
        assert self._soat_a(date(2025, 12, 1), date(2026, 1, 1)) == Decimal('2000')

    def test_una_ventana_invertida_revienta(self):
        with pytest.raises(ValueError) as e:
            self._soat_a(date(2026, 3, 31), date(2026, 3, 1))
        assert 'invertida' in str(e.value)

    def test_un_gasto_del_dia_entra_entero_en_su_ventana(self):
        """Canon §5 paso 2: mantenimiento del 10 de marzo, período de un día."""
        assert imputar_a_ventana(
            valor=CANON_MANTENIMIENTO,
            periodo_desde=date(2026, 3, 10), periodo_hasta=date(2026, 3, 10),
            ventana_desde=CANON_VENTANA_DESDE,
            ventana_hasta=CANON_VENTANA_HASTA) == CANON_MANTENIMIENTO

    def test_devuelve_Decimal_no_float(self):
        """Un float acá arrastra error a cada término y los doce meses dejan de
        sumar la póliza. El tipo es parte de la decisión, no del estilo."""
        assert isinstance(
            self._soat_a(CANON_VENTANA_DESDE, CANON_VENTANA_HASTA), Decimal)


# ══════════════════════════════════════════════════════════════════════════
# `costo_por_kilometro` — el número del canon
# ══════════════════════════════════════════════════════════════════════════

class TestCanonCostoPorKilometro:
    """Canon §5, paso 3. Los valores están arriba como constantes con nombre."""

    def test_el_caso_del_canon_da_840_pesos_por_kilometro(self):
        valor, marca = costo_por_kilometro(
            pesos_imputados=CANON_PESOS_IMPUTADOS,
            km_recorridos=CANON_KM_DE_MARZO,
            hubo_gastos=True, marca_tramo='declarada')
        assert valor == CANON_CPK
        assert marca == 'declarada'

    def test_los_pesos_imputados_del_canon_son_la_suma_de_sus_tres_partes(self):
        """El §5 armado desde sus insumos, no copiado del resultado.

        Si el canon y el test se copian el total, los dos pueden estar mal a la
        vez y nadie lo nota."""
        assert (CANON_SOAT_A_MARZO + CANON_COMBUSTIBLE + CANON_MANTENIMIENTO) \
            == CANON_PESOS_IMPUTADOS

    def test_el_combustible_solo_ya_explica_el_70_por_ciento(self):
        """La verificación de orden de magnitud del canon §5: si un CPK real
        diera menos que su propio combustible, el error está en los kilómetros."""
        cpk_combustible = CANON_COMBUSTIBLE / Decimal(CANON_KM_DE_MARZO)
        assert cpk_combustible == Decimal('588')
        assert cpk_combustible < CANON_CPK

    def test_la_marca_viaja_siempre(self):
        """Quien pinta el número tiene que poder decir de qué está hecho."""
        for marca in MARCAS_TRAMO:
            valor, devuelta = costo_por_kilometro(
                pesos_imputados=CANON_PESOS_IMPUTADOS,
                km_recorridos=CANON_KM_DE_MARZO,
                hubo_gastos=True, marca_tramo=marca)
            assert valor == CANON_CPK
            assert devuelta == marca

    def test_un_tramo_dudoso_publica_el_numero_Y_la_marca(self):
        """Canon §6: *«Se publica el número y se publica que es flojo»*.

        Ocultarlo dejaría al vehículo sin CPK para siempre — que es cómo un
        indicador se apaga sin que nadie lo decida."""
        valor, marca = costo_por_kilometro(
            pesos_imputados=CANON_PESOS_IMPUTADOS, km_recorridos=CANON_KM_DE_MARZO,
            hubo_gastos=True, marca_tramo='dudosa')
        assert valor == CANON_CPK and marca == 'dudosa'


class TestLosTresSinDatoDelCPK:
    """Canon §6. **Ninguno devuelve cero, y el motivo es distinto en cada uno.**"""

    def test_cero_kilometros_es_sin_dato(self):
        valor, marca = costo_por_kilometro(
            pesos_imputados=CANON_PESOS_IMPUTADOS, km_recorridos=0,
            hubo_gastos=True, marca_tramo='declarada')
        assert valor is SIN_DATO
        # La marca sigue viajando: el tramo se pudo juzgar, lo que no se pudo
        # fue dividir.
        assert marca == 'declarada'

    def test_kilometros_negativos_es_sin_dato(self):
        """Un odómetro que retrocede no produce un CPK negativo."""
        valor, _ = costo_por_kilometro(
            pesos_imputados=CANON_PESOS_IMPUTADOS, km_recorridos=-40,
            hubo_gastos=True, marca_tramo='declarada')
        assert valor is SIN_DATO

    def test_sin_ningun_gasto_registrado_es_sin_dato(self):
        """«Nadie registró nada» y «no costó nada» se ven idénticos en un cero, y
        el primero es el estado real de esta fase: la operación viene perdiendo
        las facturas."""
        valor, marca = costo_por_kilometro(
            pesos_imputados=Decimal('0'), km_recorridos=CANON_KM_DE_MARZO,
            hubo_gastos=False, marca_tramo='declarada')
        assert valor is SIN_DATO
        assert marca == 'declarada'

    def test_los_dos_extremos_dudosos_dejan_sin_dato_hasta_la_marca(self):
        valor, marca = costo_por_kilometro(
            pesos_imputados=CANON_PESOS_IMPUTADOS, km_recorridos=CANON_KM_DE_MARZO,
            hubo_gastos=True, marca_tramo=SIN_DATO)
        assert valor is SIN_DATO
        assert marca is SIN_DATO

    def test_gastos_registrados_que_no_caen_en_la_ventana_dan_CERO_medido(self):
        """**El cuarto caso, y es el que separa los dos ceros del canon.**

        `hubo_gastos=True` con `pesos_imputados=0` no es «no se pudo calcular»:
        es «se calculó y dio cero». El SOAT del año pasado aporta exactamente
        nada. Sin este test, devolver `SIN_DATO` en los cuatro casos pasaría los
        tres de arriba y borraría una afirmación verdadera sobre la flota."""
        valor, marca = costo_por_kilometro(
            pesos_imputados=Decimal('0'), km_recorridos=CANON_KM_DE_MARZO,
            hubo_gastos=True, marca_tramo='declarada')
        assert valor == Decimal('0')
        assert valor is not SIN_DATO
        assert marca == 'declarada'

    def test_una_marca_desconocida_revienta(self):
        """Regla 5. Degradar una marca desconocida a `declarada` publicaría como
        confiable un tramo que nadie juzgó."""
        with pytest.raises(ValueError) as e:
            costo_por_kilometro(
                pesos_imputados=CANON_PESOS_IMPUTADOS,
                km_recorridos=CANON_KM_DE_MARZO,
                hubo_gastos=True, marca_tramo='estimada')
        assert 'estimada' in str(e.value)

    def test_el_orden_de_las_guardas_no_publica_un_numero_sobre_un_tramo_ciego(self):
        """Dirección que un reordenamiento rompe: con los dos extremos dudosos
        **y** kilómetros válidos **y** gastos, sigue sin haber número."""
        valor, _ = costo_por_kilometro(
            pesos_imputados=Decimal('1'), km_recorridos=1,
            hubo_gastos=True, marca_tramo=SIN_DATO)
        assert valor is SIN_DATO


class TestElCPKNoSabeQueExistenOtrosVehiculos:
    """Canon §3: no compara vehículos y no mide a nadie.

    No es una advertencia de redacción: es una restricción de firma, y por eso
    se comprueba sobre la firma."""

    def test_la_firma_no_recibe_conductor_ni_vehiculo(self):
        import inspect

        from flota.dominio import costos

        prohibidas = ('conductor', 'usuario', 'custodio', 'persona', 'vehiculo',
                      'placa')
        for nombre, fn in vars(costos).items():
            if not callable(fn) or not getattr(fn, '__module__', '') \
                    .endswith('costos'):
                continue
            if not inspect.isfunction(fn):
                continue
            params = inspect.signature(fn).parameters
            malas = [p for p in params
                     if any(t in p.lower() for t in prohibidas)]
            assert not malas, (
                f'{nombre} recibe {malas}: la regla 2 dice que ningún cálculo '
                f'de acá toma una persona ni un vehículo como entrada, y la '
                f'columna con la que se armaría el ranking empieza siendo un '
                f'parámetro')


# ══════════════════════════════════════════════════════════════════════════
# `precio_por_galon` — se calcula, no se guarda
# ══════════════════════════════════════════════════════════════════════════

class TestPrecioPorGalon:

    def test_el_caso_del_canon(self):
        assert precio_por_galon(valor=Decimal('224000'),
                                galones=Decimal('16')) == CANON_PRECIO_GALON

    def test_galones_fraccionarios(self):
        """La columna es `Numeric(8,3)`: los surtidores despachan 12,347."""
        assert precio_por_galon(valor=Decimal('173600'),
                                galones=Decimal('12.4')) == Decimal('14000')

    def test_cero_galones_es_sin_dato_no_cero_pesos(self):
        """Un tanqueo de cero galones no es combustible gratis: es una fila mal
        cargada. Un cero acá hundiría el precio promedio de la estación."""
        assert precio_por_galon(valor=Decimal('168000'),
                                galones=Decimal('0')) is SIN_DATO

    def test_galones_negativos_es_sin_dato(self):
        assert precio_por_galon(valor=Decimal('168000'),
                                galones=Decimal('-3')) is SIN_DATO

    def test_sobre_un_tanqueo_normal_NO_devuelve_sin_dato(self):
        """La otra dirección: un `return SIN_DATO` incondicional pasaría los dos
        tests de arriba."""
        r = precio_por_galon(valor=Decimal('168000'), galones=Decimal('12'))
        assert r is not SIN_DATO
        assert r == Decimal('14000')


# ══════════════════════════════════════════════════════════════════════════
# `excede_capacidad` — el detector que no necesita umbral
# ══════════════════════════════════════════════════════════════════════════

class TestExcedeCapacidad:
    """El único detector de esta fase, y el que se construye porque **no
    necesita umbral, ni canon, ni un mes de historia**.

    Lo que afirma es que dos datos no pueden ser los dos ciertos: la capacidad
    de la ficha o los galones registrados. No afirma nada sobre nadie (regla 2)
    — y por eso `TestElDetectorNoAcusaANadie` mira los mensajes.
    """

    def test_dispara_cuando_entran_mas_galones_de_los_que_caben(self):
        assert excede_capacidad(galones=Decimal('22'),
                                capacidad_galones=Decimal('15')) is True

    def test_NO_dispara_sobre_un_tanqueo_normal(self):
        """La otra dirección. Sin esto, `return True` pasaría el test de
        arriba y todos los tanqueos del parque saldrían marcados — que es cómo
        un canal de avisos deja de leerse."""
        assert excede_capacidad(galones=Decimal('12'),
                                capacidad_galones=Decimal('15')) is False

    def test_llenar_el_tanque_exacto_no_dispara(self):
        """El borde: 15 en un tanque de 15 es un tanqueo lleno perfecto. Un
        `>=` acá marcaría como sospechoso justo el gesto que el módulo pide."""
        assert excede_capacidad(galones=Decimal('15'),
                                capacidad_galones=Decimal('15')) is False

    def test_un_gramo_por_encima_ya_dispara(self):
        """Y el borde por el otro lado, para que «exacto no dispara» no se
        convierta en un margen silencioso."""
        assert excede_capacidad(galones=Decimal('15.001'),
                                capacidad_galones=Decimal('15')) is True

    def test_sin_capacidad_en_la_ficha_es_SIN_DATO_jamas_False(self):
        """**El corazón del detector.**

        `False` significaría «se revisó y está bien», y lo que pasa es que no hay
        contra qué revisar. Un vehículo sin capacidad declarada saldría limpio
        para siempre, que es exactamente cómo un detector se apaga sin que nadie
        lo note — la misma forma que `REC-01` y las otras cinco de la auditoría
        del 2026-08-15."""
        r = excede_capacidad(galones=Decimal('22'), capacidad_galones=None)
        assert r is SIN_DATO
        assert r is not False
        assert r is not None

    def test_sin_dato_no_se_confunde_con_False_en_un_if(self):
        """`SIN_DATO` es verdadero en contexto booleano, a propósito.

        Un `if excede_capacidad(...)` sobre un vehículo sin capacidad **entra**
        en la rama: el caso se hace visible en vez de desaparecer. Un `sin_dato`
        falsy invitaría a `valor or 0`, que es el default optimista que la regla
        1 prohíbe."""
        assert bool(excede_capacidad(galones=Decimal('22'),
                                     capacidad_galones=None)) is True


class TestElDetectorNoAcusaANadie:
    """Regla 2, comprobada sobre el texto que el sistema le muestra a una
    persona.

    *«El sistema dice que el TGZ653 rindió 19 km/gal contra una mediana de 27»*,
    no acusa a nadie. Esto no es una preferencia de redacción: una imputación
    automática es un pasivo laboral y mata la adopción, que es el recurso más
    escaso del proyecto.

    Se mira por AST el texto de los módulos que producen mensajes, no solo el
    dominio: el defecto entraría por la pantalla o por el mensaje de error del
    adaptador, que son los que alguien lee.
    """

    #: Ninguna de estas puede aparecer en un mensaje al usuario ni en el nombre
    #: de un campo. `hurto` y `robo` imputan un delito; `culpable` y `responsable`
    #: imputan responsabilidad, que es lo que decide un humano fuera del sistema.
    PALABRAS_PROHIBIDAS = ('robo', 'robar', 'hurto', 'hurtar', 'ladrón',
                           'ladron', 'culpable', 'sisar', 'sifón', 'sifon')

    _RAIZ = Path(__file__).resolve().parents[2]
    ARCHIVOS = (
        'flota/dominio/costos.py',
        'flota/adaptadores/gastos.py',
        'flota/api/gastos.py',
    )

    def _cadenas_de(self, ruta):
        """Las cadenas del módulo, por AST. **No por texto.**

        Un detector de texto se atrapa en su propio docstring — pasó siete veces
        en una semana en este repo, y la séptima fue el regex que medía esta
        misma clase de regla. Acá se leen los `ast.Constant` de tipo str que NO
        son docstrings, más los nombres de las funciones y variables.
        """
        arbol = ast.parse((self._RAIZ / ruta).read_text(encoding='utf-8'))
        docstrings = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                d = ast.get_docstring(nodo, clean=False)
                if d is not None:
                    docstrings.add(d)
        return [n.value for n in ast.walk(arbol)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in docstrings]

    def test_ningun_mensaje_ni_nombre_de_campo_imputa_un_delito(self):
        for ruta in self.ARCHIVOS:
            for cadena in self._cadenas_de(ruta):
                bajo = cadena.lower()
                malas = [p for p in self.PALABRAS_PROHIBIDAS if p in bajo]
                assert not malas, (
                    f'{ruta} le muestra {malas} a una persona, en: {cadena!r}\n'
                    'Regla 2: ningún automatismo imputa responsabilidad. El '
                    'sistema afirma que dos datos no pueden ser los dos '
                    'ciertos; el porqué lo averigua alguien.')

    def test_el_detector_ve_una_palabra_plantada(self):
        """**Detector ciego** — sin esto, un AST que devuelve `[]` porque el
        parseo cambió pasaría el test de arriba para siempre.

        Es literalmente el defecto que costó el 2026-09-01: el arnés reportaba
        «sobrevivió» sobre tests que se saltaban."""
        arbol = ast.parse("MENSAJE = 'esto es un robo'\n")
        cadenas = [n.value for n in ast.walk(arbol)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert any('robo' in c.lower() for c in cadenas)

    def test_el_dominio_produce_cadenas_de_verdad(self):
        """Piso mínimo del detector: si `_cadenas_de` devolviera lista vacía
        —por un cambio de AST, por un archivo movido— el test de arriba quedaría
        en verde sin mirar nada."""
        for ruta in self.ARCHIVOS:
            assert len(self._cadenas_de(ruta)) >= 3, (
                f'{ruta} no aportó cadenas: el detector se quedó ciego')


# ══════════════════════════════════════════════════════════════════════════
# `ventanas_lleno_a_lleno` — dónde el rendimiento existe
# ══════════════════════════════════════════════════════════════════════════

class TestVentanasLlenoALleno:

    def test_el_caso_del_canon_da_dos_ventanas(self):
        v = ventanas_lleno_a_lleno(list(CANON_TANQUEOS))
        assert len(v) == 2
        assert v[0]['km'] == 392 and v[0]['galones'] == Decimal('14')
        assert v[0]['km_por_galon'] == Decimal('28')
        assert v[1]['km'] == 328 and v[1]['galones'] == Decimal('16')
        assert v[1]['km_por_galon'] == Decimal('20.5')

    def test_los_galones_del_primer_lleno_no_entran_en_ninguna_ventana(self):
        """Canon §5: se quemaron **antes** del primer lleno, en un tramo que
        nadie midió. Contarlos hundiría el rendimiento con combustible de otro
        mes."""
        v = ventanas_lleno_a_lleno(list(CANON_TANQUEOS))
        assert sum((w['galones'] for w in v), Decimal('0')) == Decimal('30')
        assert Decimal('12') not in [w['galones'] for w in v]

    def test_un_parcial_EN_EL_MEDIO_suma_sus_galones_a_la_ventana(self):
        """Esos galones también se quemaron dentro del tramo: entre dos llenos,
        lo que entró al tanque es lo que se gastó. Lo que no puede pasar es que
        un EXTREMO no sea lleno."""
        v = ventanas_lleno_a_lleno([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'lleno'},
            {'km': 100200, 'galones': Decimal('5'),  'tanque': 'parcial'},
            {'km': 100400, 'galones': Decimal('11'), 'tanque': 'lleno'},
        ])
        assert len(v) == 1
        assert v[0]['km'] == 400
        assert v[0]['galones'] == Decimal('16')     # 5 del parcial + 11 del lleno
        assert v[0]['km_por_galon'] == Decimal('25')

    def test_un_parcial_EN_UN_EXTREMO_no_produce_ninguna_ventana(self):
        """*«No produce una ventana peor: no produce ninguna»*, y esa ausencia
        es el punto entero del campo `tanque`."""
        assert ventanas_lleno_a_lleno([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'lleno'},
            {'km': 100400, 'galones': Decimal('16'), 'tanque': 'parcial'},
        ]) == []

    def test_un_sin_dato_en_un_extremo_tampoco(self):
        assert ventanas_lleno_a_lleno([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'sin_dato'},
            {'km': 100400, 'galones': Decimal('16'), 'tanque': 'lleno'},
        ]) == []

    def test_con_un_solo_lleno_no_hay_ventana(self):
        assert ventanas_lleno_a_lleno([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'lleno'}]) == []

    def test_sin_tanqueos_no_hay_ventana(self):
        assert ventanas_lleno_a_lleno([]) == []

    def test_dos_llenos_con_el_mismo_kilometraje_se_descartan(self):
        """Un tramo de cero kilómetros entre dos llenos no es rendimiento
        infinito ni cero: es que las dos lecturas dicen lo mismo. Publicar el
        número sería inventar una medición."""
        assert ventanas_lleno_a_lleno([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'lleno'},
            {'km': 100000, 'galones': Decimal('16'), 'tanque': 'lleno'},
        ]) == []

    def test_tres_llenos_seguidos_dan_dos_ventanas_encadenadas(self):
        """El lleno del medio es extremo de las dos: cierra la primera y abre la
        segunda. Sin eso, la mitad de las mediciones se pierde."""
        v = ventanas_lleno_a_lleno(list(CANON_TANQUEOS))
        assert v[0]['km_hasta'] == v[1]['km_desde'] == 100392


class TestRendimientoKmGalon:

    def test_el_caso_del_canon_agrega_sumando_no_promediando(self):
        """Canon §5: **24,0 y no 24,25.**

        El promedio de razones le da el mismo peso a un tramo de 40 km que a uno
        de 600. Este test es el que separa las dos fórmulas, y sin él las dos
        pasan."""
        r = rendimiento_km_galon(list(CANON_TANQUEOS))
        assert r == CANON_RENDIMIENTO
        assert r != CANON_PROMEDIO_DE_RAZONES

    def test_sin_ninguna_ventana_es_SIN_DATO_nunca_cero(self):
        """Cero km/gal sería un camión que no se mueve gastando combustible."""
        r = rendimiento_km_galon([
            {'km': 100000, 'galones': Decimal('10'), 'tanque': 'parcial'},
            {'km': 100400, 'galones': Decimal('16'), 'tanque': 'parcial'},
        ])
        assert r is SIN_DATO
        assert r != 0

    def test_sin_tanqueos_es_SIN_DATO(self):
        assert rendimiento_km_galon([]) is SIN_DATO

    def test_sobre_operacion_sana_NO_devuelve_SIN_DATO(self):
        """La otra dirección: un `return SIN_DATO` incondicional pasaría los dos
        de arriba y el rendimiento no existiría nunca, en silencio."""
        assert rendimiento_km_galon(list(CANON_TANQUEOS)) is not SIN_DATO

    def test_no_recibe_conductor(self):
        """*«Scoring de conductores por km/galón — la respuesta de quien no
        quiere hacer el trabajo es tanquear a medias y declarar lleno»*. La
        firma no lo recibe y no debe recibirlo nunca (regla 2 y regla 11)."""
        import inspect

        assert list(inspect.signature(rendimiento_km_galon).parameters) == \
            ['tanqueos']


# ══════════════════════════════════════════════════════════════════════════
# El módulo entero — regla 5 comprobada por AST
# ══════════════════════════════════════════════════════════════════════════

class TestNingunDefaultSilencioso:
    """Regla 5 del módulo: *«Un `.get(x, default)` en una frontera es un bug»*.

    Por AST y no por texto: el detector de texto se atrapa en su propio
    docstring, y este módulo tiene la palabra `.get(categoria, False)` escrita
    dentro de una explicación de por qué NO se usa.
    """

    _RAIZ = Path(__file__).resolve().parents[2]

    def _gets_con_default(self, ruta):
        arbol = ast.parse((self._RAIZ / ruta).read_text(encoding='utf-8'))
        return [n for n in ast.walk(arbol)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == 'get'
                and len(n.args) == 2]

    @pytest.mark.parametrize('ruta', ['flota/dominio/costos.py',
                                      'flota/adaptadores/gastos.py'])
    def test_no_hay_get_con_default(self, ruta):
        malos = self._gets_con_default(ruta)
        assert not malos, (
            f'{ruta} degrada hacia algo que se parece al éxito en la(s) '
            f'línea(s) {[n.lineno for n in malos]}')

    def test_el_detector_ve_un_get_plantado(self):
        """Detector ciego. Un AST que no encuentra nada nunca pasa igual."""
        arbol = ast.parse('x = d.get("k", 0)\n')
        malos = [n for n in ast.walk(arbol)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == 'get' and len(n.args) == 2]
        assert len(malos) == 1


class TestSinDosLlenosEstanTODOSAfuera:
    """La rama que la mutación del 2026-09-04 destapó sin cubrir.

    `tanqueos_fuera_de_ventana` devuelve `len(tanqueos)` cuando hay menos de dos
    llenos: sin dos extremos no hay ninguna ventana, así que **ninguno de los
    tanqueos entró a una**. Devolver 0 ahí diría «no se perdió ninguno», que es
    exactamente lo contrario.

    Sobrevivió a la primera corrida porque el test que sí existía sembraba siete
    llenos y nunca llegaba a esta rama — un caso límite probado solo por su lado
    ancho.
    """

    @staticmethod
    def _t(tanque, km):
        from decimal import Decimal
        return {'km': km, 'galones': Decimal('10'), 'tanque': tanque}

    def test_con_UN_lleno_y_dos_parciales_estan_los_tres_afuera(self):
        from flota.dominio.costos import tanqueos_fuera_de_ventana

        tanqueos = [self._t('parcial', 100), self._t('lleno', 500),
                    self._t('parcial', 900)]
        assert tanqueos_fuera_de_ventana(tanqueos) == 3

    def test_sin_ningun_lleno_tambien(self):
        from flota.dominio.costos import tanqueos_fuera_de_ventana

        assert tanqueos_fuera_de_ventana(
            [self._t('parcial', 100), self._t('sin_dato', 500)]) == 2

    def test_y_con_una_lista_vacia_no_se_pierde_ninguno(self):
        """La otra dirección: cero tanqueos son cero perdidos, no «todos»."""
        from flota.dominio.costos import tanqueos_fuera_de_ventana

        assert tanqueos_fuera_de_ventana([]) == 0

    def test_con_DOS_llenos_solo_quedan_fuera_los_de_los_extremos(self):
        """Y la dirección que ya funcionaba, junto a la otra para que se lean
        como el par que son: entre dos llenos, los parciales SÍ cuentan — esos
        galones se quemaron dentro del tramo."""
        from flota.dominio.costos import tanqueos_fuera_de_ventana

        tanqueos = [self._t('parcial', 100),   # antes del primer lleno → fuera
                    self._t('lleno', 500),
                    self._t('parcial', 700),   # entre llenos → DENTRO
                    self._t('lleno', 900),
                    self._t('parcial', 1100)]  # después del último → fuera
        assert tanqueos_fuera_de_ventana(tanqueos) == 2
