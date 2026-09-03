"""
La política del preventivo, sin base y sin framework.

`diagnosticar` es **la única función que emite el estado de una tarea**. La
consumen el health, la pantalla y —el día que la plantilla de WhatsApp exista—
el aviso. Una segunda copia escrita dentro de un `SELECT` o dentro de un `if`
del JS sería la que diverge, y sería la que decide si un camión sale en rojo.

## Lo que este archivo afirma, y en las dos direcciones

| Regla | Que dispare | Que NO dispare |
|---|---|---|
| Sin intervalo no hay contra qué comparar | `sin_intervalo` | una tarea con intervalo nunca cae ahí |
| Sin línea base no hay desde dónde contar | `sin_linea_base` | con una ejecución registrada, nunca |
| Vencida = el odómetro pasó el punto de cambio | `vencida` | un kilómetro antes, no |
| Por vencer = llega dentro de la ventana | `por_vencer` | **sin ritmo medido NO se degrada a `por_vencer`** |
| La procedencia se hereda y se publica | `fuente_blanda` en `estimado` | `False` en `manual_fabricante` |

La cuarta fila es la que más importa. Un detector que marcara amarillo por no
saber gritaría sobre operación sana y se apagaría en una semana.
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from flota.dominio.odometro import RitmoDeUso, km_por_dia
from flota.dominio.preventivo import (DIAS_AVISO_PREVENTIVO, ESTADOS_TAREA,
                                      FUENTES, TIPOS_TAREA, EstadoTarea,
                                      PlanInvalido, Tarea, diagnosticar,
                                      dias_hasta, fuente_es_blanda,
                                      nombre_tarea, validar_fuente,
                                      validar_tipo)
from flota.dominio.valores import SIN_DATO, Confianza, Lectura, OrigenLectura

_T0 = datetime(2026, 3, 1, 6, 0)

#: Cien kilómetros por día, medidos y declarados. Es el ritmo de referencia de
#: todo el archivo: con él, «faltan 500 km» son exactamente cinco días.
RITMO_100 = km_por_dia([
    Lectura(valor_km=50000, ts=_T0, origen=OrigenLectura.ENTREGA,
            autor_usuario_id=1),
    Lectura(valor_km=51000, ts=_T0 + timedelta(days=10),
            origen=OrigenLectura.ENTREGA, autor_usuario_id=1),
])

#: Un vehículo del que no se puede decir a qué ritmo rueda. **No es cero.**
SIN_RITMO = km_por_dia([])


def _tarea(**extra):
    campos = dict(tipo='distribucion', intervalo_km=60000,
                  fuente='manual_fabricante', ultima_ejecucion_km=50000)
    campos.update(extra)
    return Tarea(**campos)


# ══════════════════════════════════════════════════════════════════════════
# Los dos estados que NO son «al día» ni «vencida» — regla 4
# ══════════════════════════════════════════════════════════════════════════

class TestUnaTareaQueNuncaSeEjecutoNoEstaAlDiaNiVencida:
    """**La decisión que hace que un preventivo recién sembrado no dispare
    cuarenta WhatsApp el día uno.**

    No hay contra qué comparar: el intervalo dice cada cuánto, y sin una
    ejecución no hay desde dónde contar. Devolver `al_dia` sería «se revisó y
    está bien»; devolver `vencida` acusaría al vehículo de algo que nadie sabe.
    """

    def test_sin_ejecucion_el_estado_es_sin_linea_base(self):
        d = diagnosticar(_tarea(ultima_ejecucion_km=None), 55000, RITMO_100)
        assert d.estado == EstadoTarea.SIN_LINEA_BASE

    def test_y_no_publica_kilometros_restantes_inventados(self):
        """`proximo_km` y `km_restante` salen `sin_dato`, no 60.000 ni 0: no se
        sabe desde dónde contar, así que no hay a dónde llegar."""
        d = diagnosticar(_tarea(ultima_ejecucion_km=None), 55000, RITMO_100)
        assert d.proximo_km is SIN_DATO
        assert d.km_restante is SIN_DATO
        assert d.dias_estimados is SIN_DATO

    def test_pero_SI_publica_el_intervalo_que_ya_conoce(self):
        """La otra mitad: que no haya línea base no borra lo que la ficha sí
        dice. Ocultarlo haría indistinguible esta tarea de una sin intervalo,
        que se corrige llamando a otra persona."""
        d = diagnosticar(_tarea(ultima_ejecucion_km=None), 55000, RITMO_100)
        assert d.intervalo_km == 60000

    def test_con_una_ejecucion_registrada_NO_cae_en_sin_linea_base(self):
        """La dirección contraria. Un guard que devolviera `sin_linea_base`
        siempre pasaría el test de arriba y no serviría para nada."""
        d = diagnosticar(_tarea(ultima_ejecucion_km=50000), 55000, RITMO_100)
        assert d.estado != EstadoTarea.SIN_LINEA_BASE


class TestSinIntervaloNoHayContraQueComparar:
    """La ficha dice **qué** aceite lleva el motor, no **cada cuántos km** se
    cambia. Ese número no existe en ninguna columna."""

    def test_sin_intervalo_el_estado_lo_dice(self):
        d = diagnosticar(_tarea(tipo='aceite_motor', intervalo_km=None,
                                fuente='sin_dato'), 55000, RITMO_100)
        assert d.estado == EstadoTarea.SIN_INTERVALO

    def test_gana_sobre_sin_linea_base_cuando_faltan_los_dos(self):
        """Y el orden no es arbitrario: sin intervalo, registrar la ejecución no
        destraba nada — seguirían sin poder compararse. Se reporta lo que hay
        que hacer PRIMERO."""
        d = diagnosticar(Tarea(tipo='aceite_motor', intervalo_km=None,
                               fuente='sin_dato', ultima_ejecucion_km=None),
                         55000, RITMO_100)
        assert d.estado == EstadoTarea.SIN_INTERVALO

    def test_una_tarea_con_intervalo_NUNCA_cae_ahi(self):
        for km in (1, 60000, 10 ** 9):
            d = diagnosticar(_tarea(intervalo_km=km), 55000, RITMO_100)
            assert d.estado != EstadoTarea.SIN_INTERVALO

    def test_un_intervalo_de_cero_no_se_acepta_en_silencio(self):
        """Dejaría la tarea vencida todos los días, sobre cualquier odómetro.
        Un estado devuelto ahí lo taparía."""
        with pytest.raises(PlanInvalido):
            diagnosticar(_tarea(intervalo_km=0), 55000, RITMO_100)


# ══════════════════════════════════════════════════════════════════════════
# Vencida — el único juicio que NO lleva umbral
# ══════════════════════════════════════════════════════════════════════════

class TestVencidaNoNecesitaNingunUmbral:
    """El kilometraje lo dijo el fabricante y está en la ficha desde la tanda 1.
    Lo único que faltaba era comparar el odómetro contra él."""

    def test_pasado_el_punto_de_cambio_esta_vencida(self):
        d = diagnosticar(_tarea(), 110001, RITMO_100)
        assert d.estado == EstadoTarea.VENCIDA
        assert d.km_restante == -1

    def test_exactamente_en_el_punto_de_cambio_YA_esta_vencida(self):
        """El borde se decide hacia el lado conservador: a los 110.000 justos
        el fabricante ya dijo que tocaba. Un `< 0` dejaría pasar el kilómetro
        exacto, y en una correa de distribución el lado barato del error es el
        otro."""
        d = diagnosticar(_tarea(), 110000, RITMO_100)
        assert d.estado == EstadoTarea.VENCIDA
        assert d.km_restante == 0

    def test_un_kilometro_antes_NO_esta_vencida(self):
        d = diagnosticar(_tarea(), 109999, RITMO_100)
        assert d.estado != EstadoTarea.VENCIDA
        assert d.km_restante == 1

    def test_una_vencida_no_publica_dias_estimados(self):
        """«Faltan −4 días» no es información: ya pasó. El dato que sirve es
        cuántos kilómetros se pasó, y ése sí sale."""
        d = diagnosticar(_tarea(), 115000, RITMO_100)
        assert d.dias_estimados is SIN_DATO
        assert d.km_restante == -5000


# ══════════════════════════════════════════════════════════════════════════
# Por vencer — EL único umbral, y sólo con ritmo medido
# ══════════════════════════════════════════════════════════════════════════

class TestPorVencerDependeDelRitmoMedidoYDeNadaMas:

    def test_dentro_de_la_ventana_esta_por_vencer(self):
        """A 100 km/día, 500 km son 5 días: dentro de los 15."""
        d = diagnosticar(_tarea(), 109500, RITMO_100)
        assert d.estado == EstadoTarea.POR_VENCER
        assert d.dias_estimados == 5

    def test_justo_en_el_borde_de_la_ventana_entra(self):
        """15 días exactos entran. El borde se declara acá y no se deja al azar
        de un `<` contra un `<=`."""
        km = 110000 - 100 * DIAS_AVISO_PREVENTIVO
        d = diagnosticar(_tarea(), km, RITMO_100)
        assert d.dias_estimados == DIAS_AVISO_PREVENTIVO
        assert d.estado == EstadoTarea.POR_VENCER

    def test_un_dia_mas_alla_de_la_ventana_esta_al_dia(self):
        km = 110000 - 100 * (DIAS_AVISO_PREVENTIVO + 1)
        d = diagnosticar(_tarea(), km, RITMO_100)
        assert d.estado == EstadoTarea.AL_DIA

    def test_SIN_RITMO_MEDIDO_NO_se_degrada_a_por_vencer(self):
        """**La dirección que más importa de todo el archivo.**

        A 500 km del cambio y sin saber a qué ritmo rueda el vehículo, el estado
        honesto es `al_dia` con `dias_estimados = sin_dato`: el odómetro afirma
        que todavía no llegó, y «no sé cuándo» no es «pronto».

        Marcarlo amarillo por no saber es cómo un detector empieza a gritar
        sobre operación sana, y un detector que grita se apaga en una semana.
        """
        d = diagnosticar(_tarea(), 109500, SIN_RITMO)
        assert d.estado == EstadoTarea.AL_DIA
        assert d.km_restante == 500
        assert d.dias_estimados is SIN_DATO

    def test_pero_sin_ritmo_una_VENCIDA_sigue_siendo_vencida(self):
        """El ritmo sólo interviene en `por_vencer`. Que no se sepa a qué ritmo
        rueda no borra que el odómetro ya pasó el punto de cambio."""
        d = diagnosticar(_tarea(), 115000, SIN_RITMO)
        assert d.estado == EstadoTarea.VENCIDA


class TestDiasHastaEsUnaProyeccionYLoDice:

    def test_quinientos_kilometros_a_cien_por_dia_son_cinco(self):
        assert dias_hasta(500, RITMO_100) == 5

    def test_redondea_hacia_arriba(self):
        """Con 0,4 días restantes la respuesta honesta es «un día». Cero se lee
        como «ya», y ya no es."""
        assert dias_hasta(40, RITMO_100) == 1

    def test_sin_ritmo_medido_devuelve_la_palabra(self):
        assert dias_hasta(500, SIN_RITMO) is SIN_DATO

    def test_un_vehiculo_QUIETO_no_produce_un_numero_enorme(self):
        """A ritmo cero no llega nunca, y «nunca» dividido no da días. Un entero
        enorme lo ordenaría al final de la lista como si fuera lo menos urgente
        — una afirmación que nadie midió."""
        quieto = RitmoDeUso(km_dia=Decimal('0'), marca=Confianza.DECLARADA,
                            n=2, dias=Decimal('7'))
        assert dias_hasta(500, quieto) is SIN_DATO


# ══════════════════════════════════════════════════════════════════════════
# La procedencia se hereda y viaja — decisión 2 del plan, regla 13
# ══════════════════════════════════════════════════════════════════════════

class TestUnNumeroSinProcedenciaSeLeeComoVerificado:

    def test_la_fuente_viaja_en_todos_los_estados(self):
        """Incluso en `sin_intervalo`, donde no hay número que respaldar. Que
        sea un campo del resultado y no un dato que el consumidor vaya a buscar
        es lo que impide que una pantalla la deje de pintar sin que se note."""
        for tarea in (_tarea(), _tarea(ultima_ejecucion_km=None),
                      _tarea(intervalo_km=None, fuente='sin_dato')):
            d = diagnosticar(tarea, 55000, RITMO_100)
            assert d.fuente in FUENTES
            assert isinstance(d.fuente_blanda, bool)

    def test_estimado_es_blanda_y_manual_fabricante_no(self):
        assert fuente_es_blanda('estimado') is True
        assert fuente_es_blanda('taller') is True
        assert fuente_es_blanda('sin_dato') is True
        assert fuente_es_blanda('manual_fabricante') is False
        assert fuente_es_blanda('concesionario') is False
        assert fuente_es_blanda('placa_motor') is False

    def test_una_tarea_estimada_llega_marcada_al_que_la_mira(self):
        d = diagnosticar(_tarea(fuente='estimado'), 55000, RITMO_100)
        assert d.fuente == 'estimado'
        assert d.fuente_blanda is True

    def test_y_una_documental_NO_se_marca(self):
        d = diagnosticar(_tarea(fuente='manual_fabricante'), 55000, RITMO_100)
        assert d.fuente_blanda is False

    def test_una_fuente_desconocida_revienta_en_vez_de_clasificarse(self):
        """Sin `.get(fuente, 'sin_dato')`: el camino barato para saltarse la
        procedencia sería escribir cualquier cosa, y una fuente desconocida
        clasificada como firme es peor que ninguna."""
        with pytest.raises(PlanInvalido):
            validar_fuente('me_lo_dijo_un_amigo')
        with pytest.raises(PlanInvalido):
            diagnosticar(_tarea(fuente='me_lo_dijo_un_amigo'), 55000, RITMO_100)


class TestElVocabularioEsCerradoYSeUsaEntero:

    def test_todo_tipo_del_vocabulario_tiene_nombre_legible(self):
        """Sin `.get(tipo, tipo)`: un tipo desconocido mostraría
        `aceite_diferencial` con guion bajo a quien decide si manda un camión al
        taller."""
        for tipo in TIPOS_TAREA:
            assert nombre_tarea(tipo)

    def test_un_tipo_fuera_del_vocabulario_revienta(self):
        with pytest.raises(PlanInvalido):
            validar_tipo('cambio_de_bujias')

    def test_los_cinco_estados_son_alcanzables(self):
        """Un estado que ninguna combinación de datos produce es superficie
        muerta. Se ejerce cada uno con el escenario que lo genera."""
        casos = {
            EstadoTarea.SIN_INTERVALO:
                (_tarea(intervalo_km=None, fuente='sin_dato'), 55000, RITMO_100),
            EstadoTarea.SIN_LINEA_BASE:
                (_tarea(ultima_ejecucion_km=None), 55000, RITMO_100),
            EstadoTarea.AL_DIA:      (_tarea(), 60000, RITMO_100),
            EstadoTarea.POR_VENCER:  (_tarea(), 109500, RITMO_100),
            EstadoTarea.VENCIDA:     (_tarea(), 120000, RITMO_100),
        }
        assert set(casos) == set(ESTADOS_TAREA), (
            'un estado del vocabulario quedó sin escenario que lo produzca')
        for esperado, (tarea, odo, ritmo) in casos.items():
            assert diagnosticar(tarea, odo, ritmo).estado == esperado


class TestUnaEjecucionSinOdometroEsUnaBaseRota:
    """`lectura_id` es NOT NULL con FK, así que esto no puede pasar sin que la
    base esté rota. **Devolver un estado ahí lo taparía**, y devolver `sin_dato`
    lo haría indistinguible de una tarea sin línea base — que es un problema muy
    distinto y se corrige de otra forma."""

    def test_levanta_en_vez_de_inventar_un_estado(self):
        with pytest.raises(PlanInvalido) as e:
            diagnosticar(_tarea(), SIN_DATO, RITMO_100)
        assert 'regla 3' in str(e.value)

    def test_pero_sin_linea_base_NO_exige_odometro(self):
        """La otra dirección: un vehículo sin ninguna lectura y sin ninguna
        ejecución es el arranque normal, no un error."""
        d = diagnosticar(_tarea(ultima_ejecucion_km=None), SIN_DATO, SIN_RITMO)
        assert d.estado == EstadoTarea.SIN_LINEA_BASE
