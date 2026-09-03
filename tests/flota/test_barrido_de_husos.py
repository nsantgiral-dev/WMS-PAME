"""
Las 24 horas del día contra toda función que convierte un instante en un «día».

El módulo guarda `datetime.utcnow()` —naive, en UTC— y el día operativo es
Bogotá (UTC−5). Entre las 19:00 y las 23:59 de Colombia, la fecha UTC ya es la
de mañana: **cinco horas al día, de un solo lado.**

El defecto que vivió ahí hasta el 2026-09-02 —el CPK salía al doble porque la
ventana se comparaba en UTC— sobrevivió a **1455 tests**, y no porque los tests
fueran flojos: porque todos usaban las 08:00 UTC, la única franja del día en que
la fecha UTC y la de Bogotá coinciden. Un caso elegido es un caso, y el espacio
tiene veinticuatro.

Este archivo hace dos cosas distintas y las dos hacen falta:

  1. **Un detector por AST** que enumera, sin lista escrita a mano, toda llamada
     a `.date()` sobre un instante dentro de `flota/`, y exige que pase por
     `astimezone(TZ_BOGOTA)`. Encuentra la próxima el día que se escriba.
  2. **Un barrido de comportamiento** por las 24 horas, el último y el primer
     día del mes y el cambio de año, sobre las funciones que hoy producen un día
     o cuentan días.

Lo que el barrido encontró está registrado abajo como `xfail(strict=True)`, no
escondido: `flota.dominio.hallazgo` cuenta días en UTC mientras el resto del
módulo —incluida la fecha límite del MISMO hallazgo— los cuenta en Bogotá.
"""
import ast
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.utils.fecha import TZ_BOGOTA

_RAIZ = Path(__file__).resolve().parents[2]
_FLOTA = _RAIZ / 'flota'

#: Las 24 horas del día, en hora de Colombia. El eje que nadie recorrió.
HORAS = tuple(range(24))

#: Fechas frontera: fin y principio de mes, mes corto, año bisiesto y cambio de
#: año. Se recorren enteras contra las 24 horas.
DIAS_FRONTERA = (
    date(2026, 8, 31),   # último día del mes
    date(2026, 9, 1),    # primero del siguiente
    date(2026, 2, 28),   # mes corto
    date(2028, 2, 29),   # bisiesto
    date(2026, 12, 31),  # último del año
    date(2027, 1, 1),    # primero del año
)


def _utc_desde_bogota(dia: date, hora: int) -> datetime:
    """El instante UTC-naive que el módulo guardaría para esa hora de Colombia.

    Se construye desde Bogotá y no desde UTC a propósito: la pregunta que
    importa es «¿qué pasa cuando el conductor cierra el turno a las 8 p.m.?», y
    esa hora es la suya, no la del servidor.
    """
    local = datetime(dia.year, dia.month, dia.day, hora, 0, tzinfo=TZ_BOGOTA)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _dia_bogota(ts: datetime) -> date:
    """La conversión de referencia, escrita una vez en este archivo."""
    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()


# ═══════════════════════════════════════════════════════════════════════════
# 1 · El detector: toda `.date()` sobre un instante pasa por Bogotá
# ═══════════════════════════════════════════════════════════════════════════

def _llamadas_a_date():
    """Toda `X.date()` sin argumentos dentro de `flota/`, por AST.

    Por AST y no por texto: los detectores de texto de este repo se atraparon en
    sus propios docstrings siete veces en una semana, y los docstrings de flota
    están llenos de la frase `utcnow().date()` explicando por qué no se usa.
    """
    salida = []
    for ruta in sorted(_FLOTA.rglob('*.py')):
        arbol = ast.parse(ruta.read_text(encoding='utf-8'), filename=str(ruta))
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == 'date'
                    and not nodo.args):
                receptor = ast.unparse(nodo.func.value)
                salida.append((
                    f'{ruta.relative_to(_RAIZ)}:{nodo.lineno}',
                    receptor,
                    'astimezone' in receptor,
                ))
    return salida


class TestTodaConversionDeInstanteADiaPasaPorBogota:
    """El detector que encuentra la próxima, no las que ya se arreglaron.

    Un instante en UTC convertido a día con `.date()` pelado produce el día
    equivocado durante cinco horas de cada veinticuatro, **y no falla**: devuelve
    un número. Es la peor clase de dato — parece correcto y está mal.
    """

    def test_el_detector_encuentra_algo(self):
        """Si el AST deja de encontrar llamadas, el test de abajo pasa vacío.

        Es el modo de falla de todo detector: mide cero y se lee como limpio.
        """
        llamadas = _llamadas_a_date()
        assert len(llamadas) >= 6, (
            f'solo se detectaron {len(llamadas)} llamadas a `.date()` en flota/; '
            f'el extractor se rompió')

    def test_el_detector_reconoce_las_conversiones_buenas(self):
        """La otra dirección: no puede marcar como malas las que sí convierten.

        Un detector que dispara sobre operación sana se apaga en una semana.
        """
        buenas = [c for c in _llamadas_a_date() if c[2]]
        assert len(buenas) >= 4, (
            'ninguna conversión correcta reconocida — el detector estaría '
            'marcando todo, que es indistinguible de no medir nada')

    @pytest.mark.xfail(
        strict=True,
        reason='flota/dominio/hallazgo.py:103 y :115 cuentan días con `.date()` '
               'sobre instantes UTC-naive. El MISMO módulo calcula `fecha_limite` '
               'en Bogotá (`hallazgos._instante_limite`), así que un hallazgo '
               'puede salir «no vencido» y con más días transcurridos que su '
               'plazo, en la misma respuesta. Arreglarlo cruza la frontera '
               'hexagonal —el dominio no puede importar `app.utils.fecha` '
               '(trinquete 1)— y toca una métrica con canon; no es un arreglo '
               'pequeño ni seguro.')
    def test_ninguna_conversion_se_salta_la_zona(self):
        malas = [f'{d} → {r}' for (d, r, ok) in _llamadas_a_date() if not ok]
        assert not malas, (
            '\n`.date()` sobre un instante sin convertir a Bogotá — el día sale '
            'corrido cinco horas de cada veinticuatro y no falla:\n'
            + '\n'.join(f'  · {m}' for m in malas))


# ═══════════════════════════════════════════════════════════════════════════
# 2 · Las cuatro copias de «qué día es en Bogotá»
# ═══════════════════════════════════════════════════════════════════════════

def _implementaciones_del_dia():
    """Toda función de flota que traduce un instante al día de Bogotá.

    Son **cuatro**, escritas por separado y con la misma línea adentro. El
    corolario de la regla 0 dice qué pasa después: el mismo concepto en dos
    sitios divergió en tres horas. Acá se comparan las cuatro contra la misma
    referencia en las 24 horas de seis días frontera, de modo que la primera que
    se separe caiga en rojo.
    """
    from flota.adaptadores.gastos import _dia_operativo_de
    from flota.adaptadores.inspecciones import _dia_bogota as _insp
    from flota.adaptadores.taller import _dia_bogota as _taller

    return {
        'gastos._dia_operativo_de': _dia_operativo_de,
        'inspecciones._dia_bogota': _insp,
        'taller._dia_bogota': _taller,
    }


class TestLasCopiasDeQueDiaEsEnBogota:

    def test_el_inventario_no_encogio(self):
        assert len(_implementaciones_del_dia()) >= 3

    @pytest.mark.parametrize('dia', DIAS_FRONTERA, ids=str)
    @pytest.mark.parametrize('hora', HORAS)
    def test_las_tres_dan_el_mismo_dia_a_toda_hora(self, dia, hora):
        ts = _utc_desde_bogota(dia, hora)
        esperado = dia          # se construyó a esa hora de Bogotá, ese día
        divergen = {
            nombre: fn(ts) for nombre, fn in _implementaciones_del_dia().items()
            if fn(ts) != esperado
        }
        assert not divergen, (
            f'a las {hora:02d}:00 de Colombia del {dia} ({ts} UTC) no todas '
            f'contestan {esperado}: {divergen}')

    @pytest.mark.parametrize('hora', HORAS)
    def test_la_fecha_utc_y_la_de_bogota_solo_coinciden_fuera_de_la_franja(
            self, hora):
        """El hecho que hacía invisible el defecto, afirmado como hecho.

        De 19:00 a 23:59 de Colombia la fecha UTC ya es la de mañana. Un test
        que elija una hora dentro de las otras diecinueve no puede distinguir
        las dos implementaciones — y eso es lo que pasó con las 08:00 UTC.
        """
        ts = _utc_desde_bogota(date(2026, 8, 31), hora)
        coinciden = ts.date() == _dia_bogota(ts)
        assert coinciden == (hora < 19), (
            f'a las {hora:02d}:00 de Colombia, UTC dice {ts.date()} y Bogotá '
            f'{_dia_bogota(ts)}')


# ═══════════════════════════════════════════════════════════════════════════
# 3 · Contar días: las 24 horas contra la cuenta de Bogotá
# ═══════════════════════════════════════════════════════════════════════════

class TestContarDiasDeUnHallazgo:
    """`dias_transcurridos` y `dias_hallazgo_abierto` — el reloj del hallazgo.

    Los dos restan `.date()` de dos instantes UTC. El resultado no es el número
    de días de Bogotá en **10 de las 24 horas**, y la discrepancia va en las dos
    direcciones: sobrecuenta un día en la franja de la tarde y subcuenta uno en
    la de la madrugada.
    """

    @staticmethod
    def _hallazgo(reportado_ts, cerrado_ts=None, criticidad='mayor'):
        from flota.dominio.hallazgo import Hallazgo

        return Hallazgo(
            reportado_ts=reportado_ts, criticidad=criticidad,
            estado='cerrado' if cerrado_ts else 'abierto',
            cerrado_ts=cerrado_ts)

    def _horas_que_discrepan(self, cuenta):
        """Las horas del día en que `cuenta(reporte, despues)` no da días de
        Bogotá. Barre las 24; no elige ninguna."""
        malas = []
        for hora in HORAS:
            reporte = _utc_desde_bogota(date(2026, 8, 31), hora)
            despues = reporte + timedelta(hours=6)
            esperado = (_dia_bogota(despues) - _dia_bogota(reporte)).days
            if cuenta(reporte, despues) != esperado:
                malas.append(hora)
        return malas

    def test_la_discrepancia_esta_medida_y_no_es_marginal(self):
        """El tamaño del agujero, medido y afirmado en verde.

        No es un `xfail`: es la **medición**. Diez de veinticuatro horas —el 42%
        del día— no es un caso borde; es la mitad del turno de la tarde más toda
        la madrugada. El día que el arreglo llegue, este test cae en rojo y
        obliga a venir a mirarlo en vez de dejar el número viejo escrito.
        """
        from flota.dominio.hallazgo import dias_hallazgo_abierto, dias_transcurridos

        vivas = self._horas_que_discrepan(
            lambda r, d: dias_transcurridos(self._hallazgo(r), d))
        cerradas = self._horas_que_discrepan(
            lambda r, d: dias_hallazgo_abierto(self._hallazgo(r, cerrado_ts=d)))
        assert (len(vivas), len(cerradas)) == (10, 10), (
            f'la medición cambió: dias_transcurridos discrepa en {vivas}, '
            f'dias_hallazgo_abierto en {cerradas}. Si el arreglo llegó, las dos '
            f'listas tienen que quedar vacías y este test se actualiza a (0, 0).')

    @pytest.mark.xfail(
        strict=True,
        reason='`dias_transcurridos` resta días UTC; `fecha_limite` se calcula '
               'en Bogotá. Las dos viven en el módulo del hallazgo y se '
               'contradicen 10 de 24 horas. Ver el xfail del detector por AST.')
    def test_los_dias_transcurridos_son_dias_de_bogota_a_toda_hora(self):
        """Las 24 horas en un solo test, no una por hora.

        Con `parametrize` + `xfail(strict)` las catorce horas correctas caen como
        XPASS y el registro del defecto se vuelve ilegible — que es la forma de
        ahogar un canal de advertencias con avisos conocidos.
        """
        from flota.dominio.hallazgo import dias_transcurridos

        malas = self._horas_que_discrepan(
            lambda r, d: dias_transcurridos(self._hallazgo(r), d))
        assert not malas, f'discrepan las horas de Colombia {malas}'

    @pytest.mark.xfail(
        strict=True,
        reason='`dias_hallazgo_abierto` es la métrica con canon '
               '(docs/flota/canones/dias_hallazgo_abierto.md) y cuenta días '
               'calendario en UTC, no en Bogotá. Un hallazgo reportado a las '
               '13:00 y cerrado a las 19:00 del MISMO día de Colombia se '
               'publica como «1 día».')
    def test_los_dias_del_indicador_son_dias_de_bogota_a_toda_hora(self):
        from flota.dominio.hallazgo import dias_hallazgo_abierto

        malas = self._horas_que_discrepan(
            lambda r, d: dias_hallazgo_abierto(self._hallazgo(r, cerrado_ts=d)))
        assert not malas, f'discrepan las horas de Colombia {malas}'

    @pytest.mark.xfail(
        strict=True,
        reason='La contradicción, en una sola respuesta: el hallazgo NO está '
               'vencido (la fecha límite es de Bogotá) y ya acumula más días '
               'transcurridos que su plazo (contados en UTC). El aviso de '
               'WhatsApp publica el segundo número.')
    def test_un_hallazgo_no_vencido_no_puede_llevar_mas_dias_que_su_plazo(self):
        """El caso a mano que vuelve la discrepancia indefendible.

        `menor` = 30 días de plazo. Reportado el 1 de septiembre a la 1 de la
        tarde de Colombia; se mira el 1 de octubre a las 9 de la noche, todavía
        dentro del plazo. `vencido` dice que no. `dias_transcurridos` dice 31.
        """
        from flota.dominio.hallazgo import dias_transcurridos, vencido

        reporte = _utc_desde_bogota(date(2026, 9, 1), 13)
        ahora = _utc_desde_bogota(date(2026, 10, 1), 21)
        # La fecha límite se calcula como la calcula el adaptador de verdad.
        from flota.adaptadores.hallazgos import _instante_limite
        h = self._hallazgo(reporte, criticidad='menor')
        from dataclasses import replace
        h = replace(h, fecha_limite=_instante_limite(reporte, 'menor'))

        assert not vencido(h, ahora), 'el caso perdió su premisa'
        assert dias_transcurridos(h, ahora) <= 30, (
            'no vencido y con 31 días de un plazo de 30: los dos números salen '
            'en la misma respuesta de /flota/hallazgos/<placa>')


# ═══════════════════════════════════════════════════════════════════════════
# 4 · La ventana del CPK, las 24 horas y las fronteras de mes y de año
# ═══════════════════════════════════════════════════════════════════════════

class TestLaVentanaDelCPKPorLaViaReal:
    """El defecto que ya se arregló, barrido entero en vez de en seis horas.

    `test_cpk_frontera_de_mes.py` recorre seis horas (0, 6, 12, 19, 20, 23).
    Acá van las veinticuatro, sobre seis días frontera — incluido el cambio de
    año y el 29 de febrero, que ninguna de las dos visitaba.
    """

    @pytest.fixture
    def vehiculo(self, db):
        from app.models.vehiculo import Vehiculo

        v = Vehiculo(placa='HUS100', tipo='Turbo', activo=True)
        db.session.add(v)
        db.session.commit()
        return v

    @pytest.mark.parametrize('dia', DIAS_FRONTERA, ids=str)
    @pytest.mark.parametrize('hora', HORAS)
    def test_una_lectura_cuenta_en_el_dia_en_que_la_tomaron(
            self, db, vehiculo, dia, hora):
        """La lectura pertenece al día de Colombia en que se registró.

        Dos lecturas del mismo día de Bogotá tienen que producir un tramo; el
        CPK sale `sin_dato` si la ventana no las ve a las dos, y `sin_dato` es
        justo el síntoma que un test de las 08:00 UTC no puede provocar.
        """
        from flota.adaptadores.gastos import _dia_operativo_de
        from flota.adaptadores.modelos import LecturaOdometro

        ts = _utc_desde_bogota(dia, hora)
        fila = LecturaOdometro(
            vehiculo_id=vehiculo.id, valor_km=1000 + hora, ts=ts,
            origen='tanqueo', autor_usuario_id=1, confianza='declarada')
        db.session.add(fila)
        db.session.commit()

        assert _dia_operativo_de(fila.ts) == dia, (
            f'la lectura de las {hora:02d}:00 del {dia} en Colombia cayó en '
            f'{_dia_operativo_de(fila.ts)}')

    @pytest.mark.parametrize('hora', HORAS)
    def test_el_ultimo_turno_del_mes_no_se_fuga_al_mes_siguiente(
            self, db, vehiculo, hora):
        """La forma exacta del defecto del 2026-09-02, a las 24 horas.

        Un turno del 31 contado en el 1 del mes siguiente le quita kilómetros al
        mes que termina y se los regala al que empieza: **el CPK del primero
        sale al doble** y el del segundo, hundido.
        """
        from flota.adaptadores.gastos import _dia_operativo_de
        from flota.adaptadores.modelos import LecturaOdometro

        ts = _utc_desde_bogota(date(2026, 8, 31), hora)
        fila = LecturaOdometro(
            vehiculo_id=vehiculo.id, valor_km=5000, ts=ts,
            origen='cierre_dia', autor_usuario_id=1, confianza='declarada')
        db.session.add(fila)
        db.session.commit()

        cae = _dia_operativo_de(fila.ts)
        assert cae.month == 8 and cae.day == 31, (
            f'el turno de las {hora:02d}:00 del 31 de agosto quedó contado en '
            f'{cae}')


# ═══════════════════════════════════════════════════════════════════════════
# 5 · La inspección del día y el plazo del hallazgo, por la vía real
# ═══════════════════════════════════════════════════════════════════════════

class TestElDiaDeLaInspeccionYElPlazoDelHallazgo:

    @pytest.mark.parametrize('dia', DIAS_FRONTERA, ids=str)
    @pytest.mark.parametrize('hora', HORAS)
    def test_la_inspeccion_cuenta_para_el_dia_en_que_se_hizo(self, dia, hora):
        """Con `utcnow().date()` un vehículo inspeccionado a las 8 p.m. sale
        «sin inspección hoy» y aparece con dos mañana."""
        from flota.adaptadores.inspecciones import _dia_bogota

        assert _dia_bogota(_utc_desde_bogota(dia, hora)) == dia

    @pytest.mark.parametrize('hora', HORAS)
    def test_un_bloqueante_de_la_noche_no_nace_vencido(self, hora):
        """Regla 6: bloqueante vence el mismo día. Ese día es el de Colombia.

        Un bloqueante reportado a las 8 p.m. ya es de mañana en UTC: con el día
        equivocado nacería vencido antes de que el mecánico llegue.
        """
        from flota.adaptadores.hallazgos import _instante_limite

        reporte = _utc_desde_bogota(date(2026, 8, 31), hora)
        limite = _instante_limite(reporte, 'bloqueante')
        assert limite > reporte, (
            f'reportado a las {hora:02d}:00 de Colombia, el plazo ya venció '
            f'({limite} <= {reporte})')
        # El último instante a tiempo es la medianoche de Bogotá del día
        # siguiente al del reporte — nunca más de 24 h después de esa medianoche.
        assert (limite - reporte) <= timedelta(hours=24)

    @pytest.mark.parametrize('criticidad,plazo', [('bloqueante', 0),
                                                  ('mayor', 7),
                                                  ('menor', 30)])
    @pytest.mark.parametrize('hora', HORAS)
    def test_el_plazo_cubre_los_dias_completos_que_promete(
            self, hora, criticidad, plazo):
        """`mayor` = 7 días quiere decir siete días de Colombia, a toda hora."""
        from flota.adaptadores.hallazgos import _instante_limite

        reporte = _utc_desde_bogota(date(2026, 12, 31), hora)   # cambio de año
        limite = _instante_limite(reporte, criticidad)
        ultimo_dia_a_tiempo = _dia_bogota(limite - timedelta(seconds=1))
        assert ultimo_dia_a_tiempo == _dia_bogota(reporte) + timedelta(days=plazo), (
            f'{criticidad} reportado a las {hora:02d}:00 del 31-dic vence el '
            f'{ultimo_dia_a_tiempo}, no {plazo} días después del reporte')
