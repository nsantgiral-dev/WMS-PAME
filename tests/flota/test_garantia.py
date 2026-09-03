"""
El cálculo de la garantía — `flota/dominio/taller.py`, sin base y sin framework.

`tests/flota/test_taller.py` prueba que la fila quede escrita y que la frontera
no afloje nada. Acá se prueba lo otro: **la aritmética y los tres estados**.

## Las dos direcciones, en todo

Cada regla se ejerce dos veces: que **dispare** cuando se la viola, y que **NO
dispare** sobre operación sana. Hay seis casos fechados en `CLAUDE.md` de guards
verdes sobre propiedades que la vía sana satisfacía por construcción — un test
que solo prueba el caso malo no distingue «la regla funciona» de «la regla
rechaza todo».

## Lo que este archivo existe para clavar

1. **El recorte de fin de mes.** 31 de enero + 1 mes = 28 de febrero, no el 2 de
   marzo. Con `timedelta(days=30)` la garantía sale dos días más larga, en la
   dirección de reclamar algo que ya venció.
2. **Que `sin_dato` NO cuente como vigente.** `SIN_DATO` es una cadena no vacía
   y por lo tanto **verdadera en contexto booleano**: un `if v['vigente']:`
   contaría como cubierta toda garantía que no se pudo juzgar. Es el default
   optimista de la regla 1 en el número que decide si se reclama o se paga.
3. **Que el `o` sea `o` y no `y`**, con las dos dimensiones publicadas por
   separado. La garantía comercial real es «lo que ocurra primero»; acá se
   propone con la más generosa a propósito, y quien llama al taller ve las dos.
"""
from datetime import date

import pytest

from flota.dominio import taller as dom
from flota.dominio.valores import SIN_DATO


# ══════════════════════════════════════════════════════════════════════════
# Vocabularios — cerrados, y el desconocido REVIENTA (regla 5)
# ══════════════════════════════════════════════════════════════════════════

class TestElCatalogoDeSistemasEsCerrado:
    """Si un sistema desconocido se degradara a `otro`, la búsqueda de garantía
    vigente no lo encontraría nunca — sin error, sin aviso, y con la reparación
    pagada dos veces."""

    def test_un_sistema_del_catalogo_pasa(self):
        assert dom.es_sistema('embrague') is True

    def test_un_sistema_inventado_levanta(self):
        with pytest.raises(ValueError) as e:
            dom.es_sistema('cardan')
        assert 'cardan' in str(e.value)
        # El mensaje enumera las conocidas: quien lo lea tiene que poder elegir.
        assert 'embrague' in str(e.value)

    def test_embrague_esta_aparte_de_transmision(self):
        """Es el ejemplo literal del plan («el embrague trae 6 meses») y es lo
        que un mecánico escribe en la factura. Un catálogo que obliga a traducir
        se llena de `otro`."""
        assert 'embrague' in dom.SISTEMAS
        assert 'transmision' in dom.SISTEMAS

    def test_solo_otro_exige_descripcion(self):
        assert dom.exige_descripcion('otro') is True
        assert dom.exige_descripcion('frenos') is False

    def test_exige_descripcion_tambien_revienta_con_desconocido(self):
        """La otra dirección del mismo guard: no se puede colar un sistema
        inventado por la puerta de la descripción."""
        with pytest.raises(ValueError):
            dom.exige_descripcion('turbina')


class TestElVocabularioDeGarantiaTieneTres:
    def test_sin_dato_es_un_valor_de_pleno_derecho(self):
        """Una factura que no dice nada NO es una factura sin garantía: es que
        no se preguntó. `no` cierra la puerta a reclamar; `sin_dato` la deja
        abierta con la duda escrita."""
        assert set(dom.GARANTIA_DECLARADA) == {'si', 'no', 'sin_dato'}

    def test_garantia_no_es_un_tipo_de_orden(self):
        """Quién paga es un hecho de la factura, no del motivo de la visita — y
        al abrir la orden todavía no se sabe."""
        assert 'garantia' not in dom.TIPOS_OT
        assert set(dom.TIPOS_OT) == {'correctiva', 'preventiva'}


# ══════════════════════════════════════════════════════════════════════════
# `sumar_meses` — el recorte de fin de mes
# ══════════════════════════════════════════════════════════════════════════

class TestSumarMesesRecortaAlUltimoDiaQueExiste:

    def test_el_caso_que_rompe_todo_lo_demas(self):
        """**31 de enero más un mes.** No existe el 31 de febrero."""
        assert dom.sumar_meses(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_y_en_bisiesto_recorta_al_29(self):
        assert dom.sumar_meses(date(2028, 1, 31), 1) == date(2028, 2, 29)

    def test_no_son_treinta_dias_por_mes(self):
        """La otra dirección: `timedelta(days=30)` daría el 2 de marzo, o sea
        una garantía dos días más larga que la pactada — y el error va en la
        dirección de reclamar algo que ya venció."""
        assert dom.sumar_meses(date(2026, 1, 31), 1) != date(2026, 3, 2)

    def test_el_caso_normal_conserva_el_dia(self):
        """Seis meses dados el 15 de enero vencen el 15 de julio, que es lo que
        dice la factura y lo que va a decir el taller."""
        assert dom.sumar_meses(date(2026, 1, 15), 6) == date(2026, 7, 15)

    def test_cruza_el_año(self):
        assert dom.sumar_meses(date(2026, 11, 20), 3) == date(2027, 2, 20)

    def test_doce_meses_es_el_mismo_dia_del_año_siguiente(self):
        assert dom.sumar_meses(date(2026, 3, 5), 12) == date(2027, 3, 5)

    def test_cero_meses_es_el_mismo_dia(self):
        """`sumar_meses` es aritmética y admite el cero; quien lo prohíbe es
        `dia_fin_garantia`, que es donde el cero significa algo."""
        assert dom.sumar_meses(date(2026, 3, 5), 0) == date(2026, 3, 5)

    def test_meses_negativos_levantan(self):
        with pytest.raises(ValueError):
            dom.sumar_meses(date(2026, 3, 5), -1)


# ══════════════════════════════════════════════════════════════════════════
# Los dos vencimientos
# ══════════════════════════════════════════════════════════════════════════

class TestDiaFinGarantia:

    def test_el_ultimo_dia_cubierto_es_el_dia_calculado(self):
        assert dom.dia_fin_garantia(date(2026, 3, 5), 6) == date(2026, 9, 5)

    def test_cero_meses_levanta_en_vez_de_dar_el_mismo_dia(self):
        """Una garantía de cero meses no es una garantía corta: es una garantía
        que no se declaró, y para eso está `garantia_declarada`. Devolver el
        mismo día produciría una fila que dice «sí hay garantía» y vence hoy."""
        with pytest.raises(ValueError) as e:
            dom.dia_fin_garantia(date(2026, 3, 5), 0)
        assert 'no se declaró' in str(e.value)

    def test_negativos_tambien(self):
        with pytest.raises(ValueError):
            dom.dia_fin_garantia(date(2026, 3, 5), -3)


class TestKmFinGarantia:

    def test_se_suma_al_odometro_DEL_TRABAJO(self):
        """No al de hoy. Contra el de hoy la garantía se llevaría de regalo
        todos los kilómetros que pasaron entre el taller y el momento en que
        alguien tecleó la factura."""
        assert dom.km_fin_garantia(100000, 10000) == 110000

    def test_cero_kilometros_levanta(self):
        with pytest.raises(ValueError) as e:
            dom.km_fin_garantia(100000, 0)
        assert 'no se declaró' in str(e.value)

    def test_un_odometro_negativo_levanta(self):
        with pytest.raises(ValueError):
            dom.km_fin_garantia(-1, 10000)


# ══════════════════════════════════════════════════════════════════════════
# `vigencia` — las dos dimensiones, y el tercer estado
# ══════════════════════════════════════════════════════════════════════════

class TestVigenciaPorFecha:

    def test_dentro_del_plazo_cubre(self):
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=None,
                         dia=date(2026, 7, 1), km_actual=120000)
        assert v['por_fecha'] is True
        assert v['vigente'] is True

    def test_el_dia_del_limite_TODAVIA_cubre(self):
        """El lado conservador de la regla 0: un día de más cuesta una llamada
        al taller; uno de menos cuesta pagar dos veces la misma reparación, que
        es el caso entero que esto evita."""
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=None,
                         dia=date(2026, 9, 5), km_actual=None)
        assert v['por_fecha'] is True

    def test_el_dia_siguiente_ya_no(self):
        """La otra dirección. Sin esto, un `<=` cambiado por `True` fijo pasaría
        el test de arriba."""
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=None,
                         dia=date(2026, 9, 6), km_actual=None)
        assert v['por_fecha'] is False
        assert v['vigente'] is False

    def test_sin_plazo_por_fecha_es_SIN_DATO_no_False(self):
        """`False` diría «se revisó y ya venció». Lo que pasa es que esa
        dimensión no se declaró."""
        v = dom.vigencia(hasta_fecha=None, hasta_km=200000,
                         dia=date(2026, 9, 6), km_actual=100000)
        assert v['por_fecha'] is SIN_DATO


class TestVigenciaPorKm:

    def test_debajo_del_tope_cubre(self):
        v = dom.vigencia(hasta_fecha=None, hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=105000)
        assert v['por_km'] is True
        assert v['vigente'] is True

    def test_el_kilometro_del_limite_todavia_cubre(self):
        v = dom.vigencia(hasta_fecha=None, hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=110000)
        assert v['por_km'] is True

    def test_uno_mas_ya_no(self):
        v = dom.vigencia(hasta_fecha=None, hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=110001)
        assert v['por_km'] is False
        assert v['vigente'] is False

    def test_sin_odometro_conocido_es_SIN_DATO(self):
        """Un vehículo sin lecturas **no recorrió cero kilómetros**: no se sabe
        cuántos. Con un 0 inventado, toda garantía por kilómetros saldría
        vigente sobre un parque sin odómetro."""
        v = dom.vigencia(hasta_fecha=None, hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=None)
        assert v['por_km'] is SIN_DATO
        assert v['vigente'] is SIN_DATO


class TestLasDosDimensionesSeCombinanConO:
    """Y la decisión está escrita: la garantía comercial real dice «6 meses **o**
    10.000 km, lo que ocurra primero» —o sea `y` sobre la vigencia—. Acá se usa
    `o` porque esto **propone y no bloquea**, y los dos errores no cuestan igual:
    no mostrar una garantía que sí cubría cuesta pagar dos veces; mostrar una
    vencida por kilómetros cuesta una llamada."""

    def test_vencida_por_km_pero_viva_por_fecha_SIGUE_proponiendose(self):
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=150000)
        assert v['por_fecha'] is True
        assert v['por_km'] is False
        assert v['vigente'] is True

    def test_vencida_por_fecha_pero_viva_por_km_tambien(self):
        v = dom.vigencia(hasta_fecha=date(2026, 5, 5), hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=105000)
        assert v['por_fecha'] is False
        assert v['por_km'] is True
        assert v['vigente'] is True

    def test_vencida_por_las_dos_NO_se_propone(self):
        """La otra dirección: un `or` degenerado en `True` fijo pasaría los dos
        de arriba y fallaría acá."""
        v = dom.vigencia(hasta_fecha=date(2026, 5, 5), hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=150000)
        assert v['vigente'] is False

    def test_las_dos_dimensiones_viajan_aunque_una_sola_alcance(self):
        """Es la contracara del `o`: quien llama al taller tiene que poder ver
        que está vigente por fecha y pasada de kilómetros."""
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=150000)
        assert set(v) == {'por_fecha', 'por_km', 'vigente'}


class TestElTercerEstadoNoSeColaComoVigente:
    """`SIN_DATO` es una cadena no vacía y por lo tanto **verdadera en contexto
    booleano** (ver `valores.SIN_DATO`). Un `if v['vigente']:` contaría como
    cubierta toda garantía que no se pudo juzgar — el default optimista de la
    regla 1, en el número que decide si se reclama o se paga."""

    def test_sin_ninguna_dimension_juzgable_el_veredicto_es_SIN_DATO(self):
        v = dom.vigencia(hasta_fecha=None, hasta_km=None,
                         dia=date(2026, 7, 1), km_actual=None)
        assert v['vigente'] is SIN_DATO

    def test_SIN_DATO_es_verdadero_en_contexto_booleano(self):
        """El hecho del que sale todo lo demás. Si esto cambiara, `cubre()`
        dejaría de hacer falta — y este test lo diría."""
        assert bool(SIN_DATO) is True

    def test_cubre_devuelve_False_sobre_SIN_DATO(self):
        v = dom.vigencia(hasta_fecha=None, hasta_km=110000,
                         dia=date(2026, 7, 1), km_actual=None)
        assert v['vigente'] is SIN_DATO
        assert dom.cubre(v) is False

    def test_cubre_devuelve_True_sobre_una_garantia_viva(self):
        """La otra dirección: un `cubre()` que devolviera `False` siempre
        pasaría el test de arriba y apagaría la búsqueda entera."""
        v = dom.vigencia(hasta_fecha=date(2026, 9, 5), hasta_km=None,
                         dia=date(2026, 7, 1), km_actual=None)
        assert dom.cubre(v) is True

    def test_cubre_devuelve_False_sobre_una_vencida(self):
        v = dom.vigencia(hasta_fecha=date(2026, 5, 5), hasta_km=None,
                         dia=date(2026, 7, 1), km_actual=None)
        assert dom.cubre(v) is False


class TestElDominioNoNombraANadie:
    """Regla 2 como restricción de firma, no como advertencia de redacción.

    Ninguna función de este módulo toma un conductor, un mecánico o un custodio
    como entrada. Se verifica sobre las firmas reales y no leyendo el código: un
    parámetro nuevo llamado `mecanico_id` haría fallar esto el día que se
    escriba, que es cuando hay que discutirlo.
    """

    def test_ninguna_firma_admite_una_persona(self):
        import inspect

        prohibidos = ('conductor', 'mecanico', 'custodio', 'responsable',
                      'culpable', 'usuario')
        malas = []
        for nombre in dom.__all__:
            obj = getattr(dom, nombre)
            if not callable(obj) or isinstance(obj, type):
                continue
            for p in inspect.signature(obj).parameters:
                if any(x in p.lower() for x in prohibidos):
                    malas.append(f'{nombre}({p})')
        assert not malas, (
            f'El dominio del taller empezó a recibir personas: {malas}. '
            f'Quién manejaba está en la custodia y se cruza a mano (regla 2).')

    def test_el_detector_ve_el_patron(self):
        """Sin esto, el test de arriba pasa vacío para siempre el día que
        `__all__` deje de tener funciones."""
        import inspect

        def _falso(mecanico_id: int):
            return mecanico_id

        assert 'mecanico' in list(inspect.signature(_falso).parameters)[0]

    def test_ninguna_funcion_devuelve_pesos(self):
        """La OT no lleva el costo, y el dominio que la sostiene tampoco tiene
        dónde escribirlo. El trinquete del plan es literal: *si aparece una
        columna `valor`, `costo` o `monto` en una tabla `flota_*` fuera de
        `flota_gasto`, la frontera se cruzó.*"""
        import inspect

        fuente = inspect.getsource(dom)
        # En CÓDIGO, no en prosa: el docstring del módulo explica justamente por
        # qué no hay plata acá, y una búsqueda de texto crudo se atraparía en su
        # propia explicación — que es el defecto que este repo lleva ocho veces.
        codigo = [l.split('#')[0] for l in fuente.splitlines()
                  if not l.strip().startswith(('#', '·', '"""', "'''"))]
        for palabra in ('valor', 'costo', 'monto', 'precio'):
            asignaciones = [l for l in codigo if f'{palabra} =' in l
                            or f'{palabra}:' in l]
            assert not asignaciones, (
                f'apareció `{palabra}` en el dominio del taller: {asignaciones}')
