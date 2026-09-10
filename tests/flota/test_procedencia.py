"""
`flota/dominio/procedencia.py` — la regla 13 como tipo, y sus dos direcciones.

Cada clase de acá prueba una propiedad que, si se rompe, produce un tablero que
**se ve bien**: números sin procedencia, huecos sin motivo, o una ventana que
contesta una pregunta distinta de la que dice contestar.
"""
from datetime import date
from decimal import Decimal

import pytest

from flota.dominio.procedencia import (Cifra, ProcedenciaInvalida,
                                       Ventana, mes_en_curso_de)
from flota.dominio.valores import SIN_DATO


def _v(desde=date(2026, 9, 1), hasta=date(2026, 9, 4), etiqueta='el mes en curso'):
    return Ventana(desde=desde, hasta=hasta, etiqueta=etiqueta)


class TestLaVentanaSeNiegaASerAmbigua:

    def test_invertida_levanta(self):
        with pytest.raises(ProcedenciaInvalida, match='invertida'):
            Ventana(desde=date(2026, 9, 4), hasta=date(2026, 9, 1), etiqueta='x')

    def test_sin_etiqueta_levanta(self):
        """Dos fechas sueltas no dicen qué pregunta contestan. «Del 1 al 4» es
        el mes en curso, o los últimos cuatro días, o lo que va de la semana —
        y las tres se leen distinto al lado del mismo número."""
        with pytest.raises(ProcedenciaInvalida, match='etiqueta'):
            Ventana(desde=date(2026, 9, 1), hasta=date(2026, 9, 4), etiqueta='  ')

    def test_los_dias_incluyen_los_DOS_extremos(self):
        """Del 1 al 31 son 31 días, no 30. Un período contado con `.days`
        pelado pierde un día por cada gasto imputado."""
        assert _v(date(2026, 3, 1), date(2026, 3, 31)).dias == 31
        assert _v(date(2026, 3, 1), date(2026, 3, 1)).dias == 1


class TestLaCifraNoSePuedePublicarSinProcedencia:
    """El corazón del módulo: la regla 13 deja de depender de la disciplina."""

    def test_sin_base_levanta(self):
        with pytest.raises(ProcedenciaInvalida, match='sin base'):
            Cifra(valor='4780.00', base='', ventana=_v(), n=4)

    def test_base_en_blanco_no_cuenta_como_base(self):
        with pytest.raises(ProcedenciaInvalida, match='sin base'):
            Cifra(valor='4780.00', base='   \n ', ventana=_v(), n=4)

    def test_n_negativo_levanta(self):
        with pytest.raises(ProcedenciaInvalida, match='negativo'):
            Cifra(valor=3, base='hallazgos cerrados', ventana=_v(), n=-1)

    def test_n_booleano_no_pasa_por_entero(self):
        """`True` es un `int` en Python y `isinstance(True, int)` es verdadero.
        Un `n=True` colado por un `bool(...)` publicaría «calculado sobre 1»."""
        with pytest.raises(ProcedenciaInvalida, match='no es un entero'):
            Cifra(valor=3, base='hallazgos cerrados', ventana=_v(), n=True)

    def test_un_Decimal_no_llega_a_jsonify(self):
        """`jsonify` no sabe serializar `Decimal`, y el `float(...)` que alguien
        pondría para que no reviente pierde centavos en silencio. La conversión
        la hace el adaptador, que sabe cuántos decimales lleva cada magnitud."""
        with pytest.raises(ProcedenciaInvalida, match='Decimal'):
            Cifra(valor=Decimal('4780.00'), base='pesos ÷ km', ventana=_v(), n=4)


class TestElHuecoTieneQueDecirPorQue:
    """Las DOS direcciones. La primera es la que importa; la segunda impide
    que el campo se llene por costumbre y deje de significar algo."""

    def test_sin_dato_sin_motivo_levanta(self):
        with pytest.raises(ProcedenciaInvalida, match='POR QUÉ'):
            Cifra(valor=SIN_DATO, base='pesos ÷ km', ventana=_v(), n=0)

    def test_sin_dato_con_motivo_se_construye(self):
        c = Cifra(valor=SIN_DATO, base='pesos ÷ km', ventana=_v(), n=0,
                  motivo='ningún gasto registrado contra este vehículo.')
        assert c.a_json()['motivo'].startswith('ningún gasto')

    def test_una_cifra_CON_valor_y_con_motivo_levanta(self):
        """Un motivo al lado de un número presente se lee como una advertencia
        sobre un dato sano — y el que lo lee no sabe cuál de los dos creer."""
        with pytest.raises(ProcedenciaInvalida, match='contradice'):
            Cifra(valor='4780.00', base='pesos ÷ km', ventana=_v(), n=4,
                  motivo='no se pudo calcular')


class TestElUnicoSerializador:

    def test_a_json_trae_siempre_las_mismas_claves(self):
        """Que sea el único es lo que hace exigible el trinquete de la regla 13:
        recorre el payload y pide estas claves. Un número que no pasó por acá no
        las tiene."""
        j = Cifra(valor='4780.00', base='pesos ÷ km',
                  ventana=_v(date(2026, 9, 1), date(2026, 9, 4)), n=4).a_json()
        assert set(j) == {'valor', 'base', 'desde', 'hasta', 'etiqueta', 'n', 'motivo'}
        assert j['desde'] == '2026-09-01' and j['hasta'] == '2026-09-04'

    def test_el_valor_NO_puede_pisar_una_clave_de_procedencia(self):
        """`a_json('base')` pondría el número donde va su base, y la cifra se
        publicaría tapando exactamente lo que la hace auditable.

        No estaba probado: la mutación que apaga esta guarda **sobrevivió** el
        2026-09-04. Lo había verificado a mano en una consola y no lo escribí,
        que es la forma más común de que una guarda quede sin red.
        """
        c = Cifra(valor='4780.00', base='pesos ÷ km', ventana=_v(), n=4)
        for clave in ('base', 'desde', 'hasta', 'etiqueta', 'n', 'motivo'):
            with pytest.raises(ProcedenciaInvalida, match='pisaría'):
                c.a_json(clave)
        # Y la otra dirección: un nombre libre SÍ se acepta, o la guarda estaría
        # bloqueando el caso para el que se escribió.
        assert 'cpk' in c.a_json('cpk')

    def test_las_fechas_salen_en_ISO_y_no_como_objetos(self):
        j = Cifra(valor=0, base='x', ventana=_v(), n=1).a_json()
        assert isinstance(j['desde'], str) and isinstance(j['hasta'], str)




class TestElMesEnCurso:

    def test_arranca_el_primero_y_no_hace_30_dias(self):
        """El CPK se compara contra el mes anterior, y una ventana móvil no se
        puede comparar con nada."""
        v = mes_en_curso_de(date(2026, 9, 4))
        assert (v.desde, v.hasta) == (date(2026, 9, 1), date(2026, 9, 4))

    def test_el_primero_del_mes_da_una_ventana_de_un_dia(self):
        v = mes_en_curso_de(date(2026, 9, 1))
        assert v.dias == 1 and v.desde == v.hasta


class TestElDominioSigueSiendoPuro:

    def test_las_ventanas_reciben_el_dia_y_no_lo_calculan(self):
        """`flota/dominio/` no puede importar `app.utils.fecha`, y eso es
        correcto: qué día es hoy depende de en qué zona corre el proceso, que es
        infraestructura. El adaptador pasa `dia_operativo()`.

        La consecuencia práctica es esta prueba: las ventanas se barren sin
        monkeypatchear un reloj, que es cómo se encontró que la ventana del CPK
        corría cinco horas.
        """
        import inspect

        from flota.dominio import procedencia

        for fn in (procedencia.mes_en_curso_de,):
            params = list(inspect.signature(fn).parameters)
            assert params == ['dia'], f'{fn.__name__} recibe {params}'
