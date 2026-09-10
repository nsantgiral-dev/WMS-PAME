"""
TRINQUETE — ninguna ficha de rol promete un número que el sistema no publique.

`docs/procedimientos/README.md:16` fija la regla que gobierna todos los
procedimientos:

> **Ningún procedimiento puede prometer lo que el sistema niega, ni permitir lo
> que el sistema impide.**

Y explica por qué, mejor de lo que se podría reescribir: *«Si el papel dice "el
supervisor de flota autoriza reparaciones" y el sistema le devuelve 403, la
persona aprende dos cosas el primer día: que el documento miente y que los
errores se ignoran. A partir de ahí, ningún procedimiento vale.»*

## Lo que costó no tenerlo

Medido el 2026-09-04, sobre las dos fichas de flota:

| Ficha | Qué prometía | Desde | Qué mostraba el sistema |
|---|---|---|---|
| `especialista-control-flota.md:119` | «Días promedio de hallazgo abierto» | 2026-08-04 | nada — el canon y la función existían desde el 08-03, **sin un solo caller** |
| `piso-conductor.md:149` | «Rendimiento km/galón del vehículo» | 2026-08-04 | nada |

Un mes de dos fichas prometiendo indicadores de desempeño que ninguna pantalla
mostraba. Nadie lo notó porque **no había forma de notarlo**: la única manera de
comprobarlo era abrir las diez fichas y buscar cada número a mano.

## Por qué un ancla y no leer la prosa

La tentación es buscar los nombres de métrica dentro del texto en castellano.
Este repo atrapó detectores de texto en sus propios docstrings **nueve veces en
una semana** — la novena fue el regex que medía la regla del detector ciego y se
le escapó un invariante diez minutos después de que se escribiera la
advertencia.

Así que la ficha **nombra el campo**, en una forma que una máquina puede leer:

    | Días promedio de hallazgo abierto `health:dias_hallazgo_abierto` | ... |
    | Recaudo cuadrado `sin_sistema` | Liquidación |

Es el mismo movimiento que `@invariante(detector_ciego=...)`: la afirmación
tiene que nombrar lo que la respalda, y el nombre tiene que resolver.
"""
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ROLES = RAIZ / 'docs' / 'procedimientos' / 'roles'

_SECCION = 'Cómo se sabe que lo estás haciendo bien'
_ANCLA = re.compile(r'`(health:[a-z0-9_]+|sin_sistema)`')

#: Las fichas que ya cumplen. **Solo puede CRECER** — hay un test que lo obliga.
#:
#: Arranca con las dos de flota porque son las que tenían el desfase medido y
#: porque son las que este cambio arregla. Las otras ocho quedan abajo como
#: deuda heredada: triarlas toca módulos de otras manos y es un trabajo aparte.
#: Es el mismo patrón que `BASELINE_HEREDADO` en el guard de rutas huérfanas —
#: pasa con lo que hay, revienta con lo nuevo.
FICHAS_ANCLADAS = {
    'especialista-control-flota.md',
    'piso-conductor.md',
}

#: Deuda heredada, con nombre. Cada una es una ficha cuyas señales de desempeño
#: nadie puede verificar contra el sistema sin abrirla y buscar a mano.
#:
#: **Solo puede ENCOGER.** Una ficha que sale de acá entra a `FICHAS_ANCLADAS` y
#: no vuelve.
SIN_ANCLAR_HEREDADO = {
    'especialista-tienda.md',
    'especialista-compras.md',
    'gestion-admin.md',
    'gestion-supervisor.md',
    'gestion-jefe-almacen.md',
    'piso-empacador.md',
    'piso-recepcionista.md',
    'piso-operario.md',
}


def _filas_de_señales(texto: str):
    """Las filas de la tabla «Cómo se sabe...», sin el encabezado.

    Se corta en el siguiente `##` y no se lee el archivo entero: otras tablas de
    la misma ficha (facultades, primera semana) no son señales de desempeño y
    exigirles ancla convertiría el trinquete en burocracia.
    """
    if _SECCION not in texto:
        return []
    resto = texto.split(_SECCION, 1)[1]
    resto = re.split(r'\n---|\n## ', resto)[0]
    filas = []
    for linea in resto.splitlines():
        linea = linea.strip()
        if not linea.startswith('|') or set(linea) <= set('|- '):
            continue
        if linea.lower().startswith('| señal'):
            continue
        filas.append(linea)
    return filas


def _fichas():
    return sorted(p for p in ROLES.glob('*.md'))


class TestNingunaFichaPrometeLoQueElSistemaNiega:

    def test_las_fichas_ancladas_nombran_TODAS_sus_señales(self):
        """Cada fila de la tabla de desempeño lleva su ancla.

        Sin ancla en TODAS, el trinquete mide las que alguien se acordó de
        anclar — que es el defecto del guard de bodegas, que leía una copia
        cuando la propiedad era «todas coinciden».
        """
        sin_ancla = []
        for ruta in _fichas():
            if ruta.name not in FICHAS_ANCLADAS:
                continue
            for fila in _filas_de_señales(ruta.read_text(encoding='utf-8')):
                if not _ANCLA.search(fila):
                    sin_ancla.append(f'{ruta.name}: {fila[:70]}')
        assert not sin_ancla, (
            '\nSeñales de desempeño sin ancla:\n'
            + '\n'.join(f'  · {s}' for s in sin_ancla)
            + '\n\nCada fila lleva `health:<campo>` si el sistema la publica, o '
              '`sin_sistema` si de verdad no la publica y se mide de otro modo.')

    def test_todo_health_X_resuelve_contra_un_campo_REAL(self):
        """El corazón: la ficha no puede nombrar un campo que no existe.

        Es lo que habría atrapado las dos promesas rotas — y lo que atrapa la
        tercera, que va a ser un campo renombrado sin tocar el documento.
        """
        from flota.api.health import _CAMPOS

        rotas = []
        for ruta in _fichas():
            texto = ruta.read_text(encoding='utf-8')
            for m in _ANCLA.finditer(texto):
                if not m.group(1).startswith('health:'):
                    continue
                campo = m.group(1).split(':', 1)[1]
                if campo not in _CAMPOS:
                    rotas.append(f'{ruta.name} → health:{campo}')
        assert not rotas, (
            '\nFichas que nombran un campo del health que NO existe:\n'
            + '\n'.join(f'  · {r}' for r in rotas)
            + '\n\nO el campo se renombró y el documento quedó atrás, o la '
              'ficha promete algo que nadie construyó. Las dos se arreglan, '
              'ninguna se ignora.')

    def test_el_detector_ve_un_ancla_rota_de_verdad(self):
        """La otra dirección. Un detector que no sabe reconocer un ancla rota
        devuelve lista vacía sobre una ficha que miente, y eso se lee como
        «todo coherente»."""
        from flota.api.health import _CAMPOS

        assert 'campo_que_ninguna_ficha_deberia_nombrar' not in _CAMPOS
        falsa = '| Señal inventada `health:campo_que_ninguna_ficha_deberia_nombrar` | x |'
        assert _ANCLA.search(falsa), 'el regex no reconoce un ancla bien formada'
        campo = _ANCLA.search(falsa).group(1).split(':', 1)[1]
        assert campo not in _CAMPOS, 'el detector no vería este caso'

    def test_el_detector_encuentra_filas_de_verdad(self):
        """El otro modo de que esto se apague en silencio: si `_filas_de_señales`
        dejara de encontrar filas —un cambio de formato, un encabezado
        distinto—, TODAS las fichas pasarían con cero filas que revisar.

        Un detector que no lee nada declara todo correcto.
        """
        for nombre in FICHAS_ANCLADAS:
            filas = _filas_de_señales((ROLES / nombre).read_text(encoding='utf-8'))
            assert len(filas) >= 3, (
                f'{nombre}: el lector encontró {len(filas)} fila(s) de señales. '
                f'O la tabla se movió, o el formato cambió — en los dos casos '
                f'el trinquete dejó de mirar.')


class TestLaDeudaHeredadaSoloEncoge:
    """Mismo criterio que `BASELINE_HEREDADO` en el guard de rutas huérfanas:
    pasa con lo que hay, revienta con lo nuevo."""

    def test_las_dos_listas_cubren_todas_las_fichas_CON_tabla(self):
        """Una ficha nueva con tabla de desempeño no puede quedar en ninguna de
        las dos listas por omisión: eso la haría invisible para el trinquete."""
        con_tabla = {p.name for p in _fichas()
                     if _SECCION in p.read_text(encoding='utf-8')}
        huerfanas = con_tabla - FICHAS_ANCLADAS - SIN_ANCLAR_HEREDADO
        assert not huerfanas, (
            f'\nFichas con tabla de desempeño que ninguna lista declara: '
            f'{sorted(huerfanas)}\n'
            'Anclá sus señales y agregala a `FICHAS_ANCLADAS`, o declarala en '
            '`SIN_ANCLAR_HEREDADO` con la intención de anclarla.')

    def test_ninguna_ficha_esta_en_las_DOS_listas(self):
        assert not (FICHAS_ANCLADAS & SIN_ANCLAR_HEREDADO)

    def test_la_deuda_no_crecio(self):
        """El techo. Si sube, alguien agregó una ficha sin anclar en vez de
        anclarla — y la lista de exenciones que solo crece termina eximiendo
        todo."""
        assert len(SIN_ANCLAR_HEREDADO) <= 8, (
            f'la deuda creció a {len(SIN_ANCLAR_HEREDADO)}. Solo puede encoger.')
