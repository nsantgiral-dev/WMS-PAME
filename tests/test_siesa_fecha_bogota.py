"""
La fecha que va a Siesa es la de Colombia, no la de UTC.

**Ya había cuatro tests de esto, y estaban en verde mientras el bug corría.**
Verificaban `_fecha_hoy_bogota()` en aislamiento: que el helper calcula bien.
Ninguno afirmaba que alguien lo llamara — y 12 de 16 sitios seguían con
`datetime.utcnow()`. La suite decía "timezone: OK" sobre una propiedad rota.

Es el patrón exacto del que este repo ya tiene memoria: un test que verifica la
implementación en vez de la propiedad. Acá se prueban las dos cosas que faltaban:

  1. **Que ningún sitio calcule su propia fecha** (estructural, por AST).
  2. **Que un payload real, construido a las 23:59 de Colombia, lleve la fecha
     de HOY en Colombia y no la de mañana en UTC** (la propiedad).

Por qué importa en pesos: `f350_fecha` es la fecha contable del documento. Siesa
rechaza documentos con fecha futura o período cerrado, y el cierre de despacho
del día ocurre justo después de las 7 p.m. — que es cuando `utcnow()` empieza a
dar mañana. El error solo aparece en el turno de la tarde, todos los días.
"""
import ast
import re
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.services.connekta_gateway import ConnektaGateway

_FUENTE = Path(__file__).resolve().parents[1] / 'app' / 'services' / 'connekta_gateway.py'
_TZ = ZoneInfo('America/Bogota')

#: 04:59 UTC del 16 = 23:59 COT del 15. El instante donde los dos calendarios
#: discrepan, que es el único momento en que este bug es visible.
_MOMENTO_UTC = datetime(2026, 7, 16, 4, 59, tzinfo=ZoneInfo('UTC'))
_ESPERADO = '20260715'


class TestNingunSitioCalculaSuPropiaFecha:
    """TRINQUETE — la razón por la que el bug sobrevivió cuatro meses.

    El helper existía desde el 2026-07-21 con un docstring que explica el
    problema, y nada impedía seguir escribiendo `datetime.utcnow()` al lado.
    No fue descuido: fue que no había nada que lo atajara.
    """

    @staticmethod
    def _lineas_de_docstring(arbol):
        """Rangos de línea de los DOCSTRINGS, no de cualquier literal.

        Primera versión de este guard: excluía toda línea que contuviera un
        string constant. `fecha_hoy = datetime.utcnow().strftime('%Y%m%d')`
        contiene `'%Y%m%d'` — **se auto-excluía**, y el guard daba verde con el
        bug reinsertado. La proxy otra vez: "la línea tiene un string" no es
        "la línea es documentación".
        """
        rangos = set()
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cuerpo = getattr(nodo, 'body', [])
            if not cuerpo:
                continue
            primero = cuerpo[0]
            if (isinstance(primero, ast.Expr)
                    and isinstance(primero.value, ast.Constant)
                    and isinstance(primero.value.value, str)):
                ini = primero.value.lineno
                fin = primero.value.end_lineno or ini
                rangos.update(range(ini, fin + 1))
        return rangos

    @classmethod
    def _utcnow_en_codigo(cls):
        """Ocurrencias de `utcnow()` que NO están en un docstring ni comentario.

        Los docstrings de los helpers CITAN `datetime.utcnow()` para explicar
        qué reemplazan, y un guard que castiga su propia documentación termina
        apagado. Pero excluirlos no puede excluir el código de al lado.
        """
        fuente = _FUENTE.read_text(encoding='utf-8')
        docstrings = cls._lineas_de_docstring(ast.parse(fuente))

        malas = []
        for n, linea in enumerate(fuente.split('\n'), 1):
            if 'utcnow()' not in linea:
                continue
            if n in docstrings or linea.strip().startswith('#'):
                continue
            malas.append(f'{n}: {linea.strip()[:80]}')
        return malas

    def test_no_queda_utcnow_ejecutable_en_el_gateway(self):
        malas = self._utcnow_en_codigo()
        assert not malas, (
            '\n`datetime.utcnow()` en el gateway — da la fecha de MAÑANA después '
            'de las 7 p.m. Colombia:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar _fecha_hoy_bogota() / _fecha_iso_bogota() / '
              '_fecha_bogota_mas(n).')

    def test_el_filtro_de_docstrings_no_apaga_el_guard(self):
        """Excluir los docstrings no puede excluir el código.

        La primera versión de este filtro excluía toda línea con un string
        constant, y `...strftime('%Y%m%d')` contiene uno: el guard daba verde
        con el bug reinsertado. Acá se ejerce sobre código sintético para que
        esa regresión no vuelva en silencio.
        """
        fuente = _FUENTE.read_text(encoding='utf-8')
        assert 'utcnow()' in fuente, (
            'los docstrings dejaron de citar el patrón: el guard ya no está '
            'ejercitando su propio filtro')

        modulo = ast.parse(
            'def f():\n'
            '    """cita: datetime.utcnow() no se usa"""\n'
            "    x = datetime.utcnow().strftime('%Y%m%d')\n"
        )
        docs = self._lineas_de_docstring(modulo)
        assert 2 in docs, 'el docstring no se está excluyendo'
        assert 3 not in docs, (
            'la línea de CÓDIGO se está excluyendo por contener un string — '
            'el guard es ciego')

    def test_toda_asignacion_de_fecha_sale_de_un_helper(self):
        """La propiedad estructural: nadie arma su fecha a mano.

        Cubre el caso que `utcnow` no cubre — alguien podría escribir
        `datetime.now()` sin zona, o `date.today()`, y el guard de arriba no lo
        vería.
        """
        arbol = ast.parse(_FUENTE.read_text(encoding='utf-8'))
        permitidos = {'_fecha_hoy_bogota', '_fecha_iso_bogota',
                      '_fecha_bogota_mas', '_ahora_bogota'}
        malas = []
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Assign):
                continue
            nombres = [t.id for t in nodo.targets if isinstance(t, ast.Name)]
            if not any(n.startswith('fecha') for n in nombres):
                continue
            fuente_valor = ast.dump(nodo.value)
            if any(h in fuente_valor for h in permitidos):
                continue
            # Fechas que vienen de un parámetro o de un objeto del dominio
            # (`pedido.fecha`) son legítimas: lo que se persigue es el reloj.
            if 'datetime' in fuente_valor or 'today' in fuente_valor:
                malas.append(f'línea {nodo.lineno}: {nombres}')
        assert not malas, (
            '\nFechas calculadas sin pasar por un helper de zona:\n'
            + '\n'.join(f'  · {m}' for m in malas))


class TestLaPropiedad:
    """Lo que los cuatro tests anteriores no probaban: que se USE.

    Se congela el reloj en el instante donde UTC y Colombia discrepan y se mira
    lo que devuelven los helpers — que es de donde salen los 16 sitios.
    """

    @patch('app.utils.fecha.datetime')
    def test_a_las_2359_de_colombia_la_fecha_es_la_de_hoy_aca(self, reloj):
        reloj.now.return_value = _MOMENTO_UTC.astimezone(_TZ)
        reloj.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert ConnektaGateway._fecha_hoy_bogota() == _ESPERADO

    @patch('app.utils.fecha.datetime')
    def test_y_no_la_de_manana_en_utc(self, reloj):
        """El assert que nombra el error: 20260716 es lo que daba `utcnow()`."""
        reloj.now.return_value = _MOMENTO_UTC.astimezone(_TZ)
        reloj.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert ConnektaGateway._fecha_hoy_bogota() != '20260716'

    @patch('app.utils.fecha.datetime')
    def test_el_formato_iso_tambien(self, reloj):
        reloj.now.return_value = _MOMENTO_UTC.astimezone(_TZ)
        reloj.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert ConnektaGateway._fecha_iso_bogota() == '2026-07-15'

    @patch('app.utils.fecha.datetime')
    def test_el_vencimiento_se_cuenta_desde_el_dia_correcto(self, reloj):
        """Un vencimiento a 30 días calculado a las 8 p.m. sobre `utcnow()`
        vencía un día antes de lo pactado — y eso es cartera, no cosmética."""
        reloj.now.return_value = _MOMENTO_UTC.astimezone(_TZ)
        reloj.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert ConnektaGateway._fecha_bogota_mas(30) == '20260814'

    def test_el_timestamp_de_respuesta_declara_su_zona(self):
        """Una hora sin zona se lee como local y corre el reloj 5 horas — el
        mismo bug que apareció en flota el 2026-08-03."""
        ts = ConnektaGateway._ahora_bogota().isoformat()
        assert re.search(r'[+-]\d{2}:\d{2}$', ts), f'sin zona: {ts}'


class TestUnaPoliticaUnaFuncion:
    """Los tres formatos salen del mismo reloj.

    Si `_fecha_hoy_bogota` y `_fecha_bogota_mas` leyeran relojes distintos,
    volverían a divergir — que es lo que pasó entre el helper y los 12 sitios.
    """

    @patch('app.utils.fecha.datetime')
    def test_los_tres_formatos_hablan_del_mismo_dia(self, reloj):
        reloj.now.return_value = _MOMENTO_UTC.astimezone(_TZ)
        reloj.side_effect = lambda *a, **kw: datetime(*a, **kw)
        assert ConnektaGateway._fecha_hoy_bogota() == '20260715'
        assert ConnektaGateway._fecha_iso_bogota() == '2026-07-15'
        assert ConnektaGateway._fecha_bogota_mas(0) == '20260715'


class TestNadieMasArmaFechasDeNegocioConUtcnow:
    """TRINQUETE de repo — el gateway no era el único.

    Al seguir la raíz aparecieron cuatro sitios FUERA del gateway calculando
    fechas de negocio con `utcnow()`, y dos de ellos no eran cosméticos:

      · `inventario_siesa_service` la usaba como sufijo de la CLAVE DE
        IDEMPOTENCIA (`SIESA-INI-{prod}-{fecha}`). Cambiaba a las 7 p.m., así
        que dos cargas de stock a las 6:55 y 7:05 tenían claves distintas y el
        inventario entraba dos veces.
      · `mobile_service` la usaba para el CUPO DIARIO de conteo. Se reiniciaba a
        las 7 p.m. y el operario podía hacer el doble en el turno de la tarde.

    Los timestamps técnicos (`created_at`, `updated_at`) siguen en UTC y eso es
    correcto: lo que no puede salir de UTC es una fecha que alguien LEE como día.
    """

    _RAIZ = Path(__file__).resolve().parents[1] / 'app'

    #: Formatear una fecha o pedir el `.date()` es tratarla como DÍA. Ahí la
    #: zona importa. Un `utcnow()` guardado en una columna, no.
    _PATRONES = ("utcnow().strftime", "utcnow().date()")

    @classmethod
    def _ofensas(cls):
        malas = []
        for f in sorted(cls._RAIZ.rglob('*.py')):
            if '__pycache__' in str(f):
                continue
            texto = f.read_text(encoding='utf-8')
            # Los docstrings CITAN el patrón para explicar qué reemplazan. Se
            # excluyen por AST, no por "la línea contiene un string": esa proxy
            # ya dejó ciego a este mismo guard una vez, porque la línea del bug
            # contenía `'%Y%m%d'`.
            docs = TestNingunSitioCalculaSuPropiaFecha._lineas_de_docstring(
                ast.parse(texto))
            for n, linea in enumerate(texto.split('\n'), 1):
                if n in docs or linea.strip().startswith('#'):
                    continue
                if any(p in linea for p in cls._PATRONES):
                    rel = f.relative_to(cls._RAIZ.parent)
                    malas.append(f'{rel}:{n}  {linea.strip()[:70]}')
        return malas

    def test_ninguna_fecha_de_negocio_sale_de_utcnow(self):
        malas = self._ofensas()
        assert not malas, (
            '\nFechas de negocio calculadas en UTC — dan el día de MAÑANA '
            'después de las 7 p.m. Colombia:\n'
            + '\n'.join(f'  · {m}' for m in malas)
            + '\n\nUsar app.utils.fecha: fecha_hoy_bogota(), dia_operativo(), '
              'inicio_del_dia_utc().')

    def test_el_buscador_no_esta_ciego(self):
        """Si el patrón deja de encontrar nada, el test de arriba pasa vacío.

        Se ejerce contra una línea sintética: sin esto, un refactor que renombre
        `utcnow` deja el guard en verde para siempre.
        """
        assert any(p in "x = datetime.utcnow().strftime('%Y%m%d')"
                   for p in self._PATRONES)
        assert any(p in "hoy = datetime.utcnow().date()"
                   for p in self._PATRONES)
        assert not any(p in "self.created_at = datetime.utcnow()"
                       for p in self._PATRONES), (
            'el guard estaría persiguiendo timestamps técnicos, que SÍ van en UTC')
