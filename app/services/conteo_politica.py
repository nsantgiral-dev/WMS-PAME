"""
Política del conteo cíclico: **cuánto se cuenta por día, cada cuánto se
recuenta cada clase y en qué orden se reparte.** Una sola fuente.

## Por qué existe (2026-09-23)

El generador ABC calculaba su lote como `ceil(productos_de_la_clase /
frecuencia)` con `FRECUENCIA_DIAS = {'A': 15, 'B': 90, 'C': 180}` escrito en
`abc_service`. Tres defectos de una misma forma —**el plan no miraba la
capacidad de nadie**—:

1. Cada noche sumaba otro lote aunque el anterior siguiera sin contar. El
   rezago se acumulaba sin techo.
2. «Forzar todo» creaba todo lo elegible sin límite: 8.527 tareas el 24-abr en
   cuatro minutos. En producción siguen 4.825 PENDIENTES de ese día, y como
   saturan todos los huecos, el cron no genera nada nuevo desde entonces.
3. Con 15/90/180 días sobre NB1 el plan exigía ~316 conteos por día a tres
   operarios. Un plan que nadie puede cumplir no ordena el trabajo: lo entierra.

Ahora **la capacidad fija el ritmo**: cada almacén tiene un cupo diario, el
generador crea como máximo `cupo − pendientes_vivas`, y nada si lo pendiente ya
cubre dos días de cupo. Los intervalos por clase dejan de ser un mandato y
pasan a ser la vara con que se mide el atraso de cada hueco.

## Los números por defecto, y de dónde salen

La tabla de capacidad que decidió el usuario: **~60 conteos por día en NB1**
(tres operarios, ~20 cada uno). Sobre el catálogo clasificado de NB1 medido en
producción —A 2.326, B 4.657, C 19.311 productos— esos 60 alcanzan para:

    A cada 150 días  → 2.326 / 150 ≈ 15,5 por día
    B cada 300 días  → 4.657 / 300 ≈ 15,5 por día
    C cada 600 días  → 19.311 / 600 ≈ 32,2 por día
                                       ≈ 63 por día

El universo que de verdad se programa es menor —solo lo que tiene existencia:
4.842 SKUs (A 1.822, B 1.922, C 1.098)—, así que con esos intervalos el régimen
estable pide ~20 por día y el cupo de 60 es techo, no meta: la primera pasada
sobre los 4.842 nunca contados tarda ~81 días hábiles de cupo lleno, y después
el generador solo crea lo que se vence.

El cupo de 60 se aplica a todo almacén que no tenga uno propio. **Las tiendas
están fuera de la v1**: para dejarlas sin conteo programado se declara su cupo
en 0 con `CONTEO_CUPO_POR_BODEGA` (p. ej. `{"NC1": 0, "PC1": 0}`), y el
resultado del generador lo dice.

## Variables de entorno (se leen acá y en ningún otro sitio)

| Variable | Defecto | Qué es |
|---|---|---|
| `CONTEO_CUPO_DIARIO` | `60` | Conteos por día por almacén |
| `CONTEO_CUPO_POR_BODEGA` | — | JSON `{bodega_siesa: cupo}`, gana sobre el global |
| `CONTEO_INTERVALOS_DIAS` | `{"A":150,"B":300,"C":600}` | JSON, se puede dar una sola clase |
| `CONTEO_WATCHDOG_DIAS_SIN_REABRIR` | `30` | El watchdog no reabre un hueco contado hace menos |

Un valor ilegible (no entero, negativo, JSON roto) **no se inventa**: se usa el
defecto y se declara en `advertencias_de_configuracion()`, que el generador
devuelve en su resultado y escribe en el log. Regla 0: ante dato ausente, lado
conservador y declararlo.

`tests/test_conteo_cupo.py::TestUnSoloSitioLeeLaPolitica` exige por AST que
ningún otro módulo de `app/` lea estas variables ni escriba un mapa clase→días.
"""
import json
import logging
import math
import os
from datetime import datetime

from sqlalchemy import case

from app.extensions import db
from app.models.conteo import EstadoConteo, SesionConteo

logger = logging.getLogger(__name__)

CLASES = ('A', 'B', 'C')

#: Días objetivo entre dos conteos del mismo hueco, por clase. Ver la tabla del
#: encabezado: con ~60 conteos por día sobre el catálogo clasificado de NB1.
INTERVALO_DIAS_DEFECTO = {'A': 150, 'B': 300, 'C': 600}

#: Conteos por día por almacén. La tabla de capacidad del usuario: tres
#: operarios en NB1, ~20 cada uno.
CUPO_DIARIO_DEFECTO = 60

#: Si lo pendiente cubre esta cantidad de días de cupo, no se genera nada: el
#: equipo ya tiene trabajo para más de un turno de atraso, y sumarle otro lote
#: solo esconde lo viejo debajo de lo nuevo.
DIAS_DE_REZAGO_MAXIMO = 2

#: El watchdog no reabre un hueco contado hace menos de estos días. Su señal
#: —picks de la última semana por encima del umbral de su clase— existe para
#: adelantarse al recálculo del ABC en Siesa, que es mensual: un conteo por
#: ciclo de recálculo alcanza. Sin esta regla reabría el mismo SKU cada noche
#: mientras siguiera rotando, contado o no.
WATCHDOG_DIAS_SIN_REABRIR_DEFECTO = 30

#: Watchdog: picks en `WATCHDOG_VENTANA_DIAS` que disparan un conteo inmediato
#: de un producto B o C.
WATCHDOG_UMBRAL = {'B': 25, 'C': 10}
WATCHDOG_VENTANA_DIAS = 7

#: Los conteos que alguien pidió por un motivo concreto van primero en el
#: reparto, antes que cualquier conteo del plan. En este orden:
#:   - EXCEPCION_PICKING: un picker no encontró la mercancía; hay un pedido o
#:     un traslado esperando saber si existe.
#:   - MANUAL: un líder pidió contar ese SKU (o la auditoría lo forzó).
TIPOS_EVENTO = ('EXCEPCION_PICKING', 'MANUAL')

#: Rango de clase en el reparto. Sin clase (una auditoría) va al final dentro
#: de su nivel — pero las auditorías ya van primero por ser evento.
RANGO_CLASE = {'A': 1, 'B': 2, 'C': 3}

_ENV_CUPO = 'CONTEO_CUPO_DIARIO'
_ENV_CUPO_BODEGA = 'CONTEO_CUPO_POR_BODEGA'
_ENV_INTERVALOS = 'CONTEO_INTERVALOS_DIAS'
_ENV_WATCHDOG_DIAS = 'CONTEO_WATCHDOG_DIAS_SIN_REABRIR'

#: Los nombres de las variables, para el trinquete de «un solo sitio».
VARIABLES_DE_ENTORNO = (_ENV_CUPO, _ENV_CUPO_BODEGA, _ENV_INTERVALOS, _ENV_WATCHDOG_DIAS)


# ─────────────────────────────────────────────────────────────────────────────
# Lectura de la configuración
# ─────────────────────────────────────────────────────────────────────────────

def _entero_no_negativo(valor):
    """`int` ≥ 0, o `None` si no lo es. `True`/`False` no son enteros acá."""
    if isinstance(valor, bool):
        return None
    try:
        n = int(str(valor).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _json_env(nombre: str, advertencias: list):
    crudo = os.environ.get(nombre, '').strip()
    if not crudo:
        return None
    try:
        valor = json.loads(crudo)
    except ValueError:
        advertencias.append(f'{nombre} no es JSON válido ({crudo[:60]!r}); se ignora')
        return None
    if not isinstance(valor, dict):
        advertencias.append(f'{nombre} tiene que ser un objeto JSON; se ignora')
        return None
    return valor


def _leer() -> dict:
    """Toda la configuración de una vez, con lo que no se pudo leer declarado.
    Se relee en cada llamada: cambiar una variable en Railway reinicia el
    proceso, y un test puede ajustarla con `monkeypatch.setenv`."""
    advertencias = []

    cupo_global = CUPO_DIARIO_DEFECTO
    crudo = os.environ.get(_ENV_CUPO, '').strip()
    if crudo:
        n = _entero_no_negativo(crudo)
        if n is None:
            advertencias.append(f'{_ENV_CUPO}={crudo!r} no es un entero ≥ 0; '
                                f'se usa {CUPO_DIARIO_DEFECTO}')
        else:
            cupo_global = n

    por_bodega = {}
    for bodega, valor in (_json_env(_ENV_CUPO_BODEGA, advertencias) or {}).items():
        n = _entero_no_negativo(valor)
        if n is None:
            advertencias.append(f'{_ENV_CUPO_BODEGA}[{bodega!r}]={valor!r} no es un '
                                f'entero ≥ 0; esa bodega usa el cupo global')
        else:
            por_bodega[str(bodega).strip().upper()] = n

    intervalos = dict(INTERVALO_DIAS_DEFECTO)
    for clase, valor in (_json_env(_ENV_INTERVALOS, advertencias) or {}).items():
        clase_n = str(clase).strip().upper()
        n = _entero_no_negativo(valor)
        if clase_n not in CLASES:
            advertencias.append(f'{_ENV_INTERVALOS}: clase {clase!r} desconocida; se ignora')
        elif not n:
            # Cero tampoco: «recontar cada 0 días» no es un intervalo.
            advertencias.append(f'{_ENV_INTERVALOS}[{clase_n}]={valor!r} no es un entero '
                                f'> 0; se usa {INTERVALO_DIAS_DEFECTO[clase_n]}')
        else:
            intervalos[clase_n] = n

    watchdog_dias = WATCHDOG_DIAS_SIN_REABRIR_DEFECTO
    crudo = os.environ.get(_ENV_WATCHDOG_DIAS, '').strip()
    if crudo:
        n = _entero_no_negativo(crudo)
        if n is None:
            advertencias.append(f'{_ENV_WATCHDOG_DIAS}={crudo!r} no es un entero ≥ 0; '
                                f'se usa {WATCHDOG_DIAS_SIN_REABRIR_DEFECTO}')
        else:
            watchdog_dias = n

    return {'cupo_global': cupo_global, 'cupo_por_bodega': por_bodega,
            'intervalos': intervalos, 'watchdog_dias': watchdog_dias,
            'advertencias': advertencias}


def advertencias_de_configuracion() -> list:
    """Lo que no se pudo leer de la configuración, en español. Vacía si todo
    está bien."""
    return list(_leer()['advertencias'])


def intervalos_dias() -> dict:
    """`{'A': días, 'B': días, 'C': días}` vigentes."""
    return dict(_leer()['intervalos'])


def intervalo_dias(clase) -> int:
    """Días objetivo de una clase. Clase desconocida o ausente → el intervalo
    más corto (el más exigente): no saber la clase no es razón para contar
    menos (Regla 0, el mismo criterio que el generador ya tenía)."""
    intervalos = _leer()['intervalos']
    return intervalos.get(clase) or min(intervalos.values())


def _bodega_de(almacen) -> str:
    if almacen is None:
        return ''
    if isinstance(almacen, int):
        from app.models.almacen import Almacen
        almacen = db.session.get(Almacen, almacen)
    return ((getattr(almacen, 'bodega_siesa_id', None) or '').strip().upper()
            if almacen is not None else '')


def cupo_diario(almacen) -> int:
    """Conteos por día que se le programan a un almacén (objeto o id). El de su
    bodega en `CONTEO_CUPO_POR_BODEGA` si lo tiene; si no, el global."""
    cfg = _leer()
    return cfg['cupo_por_bodega'].get(_bodega_de(almacen), cfg['cupo_global'])


def watchdog_dias_sin_reabrir() -> int:
    return _leer()['watchdog_dias']


def descripcion_del_plan(almacen=None) -> dict:
    """Lo que la pantalla ABC y las estadísticas muestran como «el plan»: los
    valores VIGENTES, no un texto escrito aparte que se desactualiza."""
    cfg = _leer()
    return {
        'intervalos_dias': dict(cfg['intervalos']),
        'cupo_diario': (cupo_diario(almacen) if almacen is not None else cfg['cupo_global']),
        'dias_de_rezago_maximo': DIAS_DE_REZAGO_MAXIMO,
        'watchdog_dias_sin_reabrir': cfg['watchdog_dias'],
        'watchdog_umbral_picks': dict(WATCHDOG_UMBRAL),
        'watchdog_ventana_dias': WATCHDOG_VENTANA_DIAS,
        'hora_del_generador': '2:00 a. m. (Bogotá)',
        'advertencias': list(cfg['advertencias']),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cuánto se puede generar hoy
# ─────────────────────────────────────────────────────────────────────────────

def pendientes_vivas(almacen_id: int) -> int:
    """Conteos del almacén que todavía nadie terminó de contar: PENDIENTE o
    EN_PROCESO, de cualquier tipo y de cualquier eslabón de la cadena (un CC2
    también es trabajo del equipo). Es lo que el cupo de hoy ya tiene
    comprometido."""
    return (SesionConteo.query
            .filter(SesionConteo.almacen_id == almacen_id,
                    SesionConteo.estado.in_([EstadoConteo.PENDIENTE,
                                             EstadoConteo.EN_PROCESO]))
            .count())


#: Base de la clave del lock que serializa a quienes consumen el cupo de un
#: almacén (generador y watchdog). No choca con las claves fijas 1003/20xx ni
#: con el lock de sesión del watchdog (3000 + almacén).
LOCK_CUPO_BASE = 4000


def bloquear_cupo(almacen_id: int) -> None:
    """Serializa, dentro de la transacción en curso, a todo el que va a crear
    conteos contra el cupo de `almacen_id`.

    Sin esto, el cron y un «Generar lote» apretado a la misma hora leen las
    mismas pendientes, calculan el mismo tope y crean el doble. Es un lock de
    **transacción** (`pg_advisory_xact_lock`), no de sesión: se suelta solo en
    el commit o el rollback, así que no puede quedar tomado en una conexión
    que vuelve al pool —el defecto que ya costó dos jobs muertos, ver
    `tests/test_advisory_locks.py`—. Espera en vez de rendirse: el que llega
    segundo lee las pendientes que el primero ya dejó.
    """
    from sqlalchemy import text
    db.session.execute(text('SELECT pg_advisory_xact_lock(:k)'),
                       {'k': LOCK_CUPO_BASE + int(almacen_id)})


SIN_CUPO = 'SIN_CUPO'
REZAGO = 'REZAGO'
CUPO_CUBIERTO = 'CUPO_CUBIERTO'


def tope_de_generacion(almacen) -> dict:
    """Cuántos conteos del plan se pueden crear ahora en un almacén.

    `tope = max(0, cupo − pendientes)`, y **cero** si las pendientes cubren
    `DIAS_DE_REZAGO_MAXIMO` días de cupo. La segunda regla ya queda dentro de
    la primera (con pendientes ≥ 2 × cupo, cupo − pendientes < 0); existe
    aparte para que el resultado **diga cuál de las dos cosas pasa**: «hoy ya
    está cubierto» y «hay dos días de atraso» piden reacciones distintas.

    Devuelve siempre el porqué cuando el tope es cero (`motivo`, `mensaje`).
    Un generador que devuelve `0 tareas` sin decir por qué se lee igual que
    «no había nada que contar».
    """
    from app.models.almacen import Almacen
    if isinstance(almacen, int):
        almacen = db.session.get(Almacen, almacen)
    almacen_id = almacen.id if almacen is not None else None
    cupo = cupo_diario(almacen)
    pendientes = pendientes_vivas(almacen_id) if almacen_id else 0
    dias = round(pendientes / cupo, 1) if cupo else None
    info = {'cupo_diario': cupo, 'pendientes_vivas': pendientes,
            'dias_de_cupo_pendientes': dias, 'tope': 0, 'motivo': None,
            'mensaje': None}
    if cupo == 0:
        info.update(motivo=SIN_CUPO, mensaje=(
            'no se generó: el cupo diario de esta bodega es 0 '
            '(conteo cíclico no programado)'))
    elif pendientes >= DIAS_DE_REZAGO_MAXIMO * cupo:
        info.update(motivo=REZAGO, mensaje=(
            f'no se generó: hay {pendientes} pendientes = {dias} días de cupo '
            f'(cupo {cupo}/día; se detiene con {DIAS_DE_REZAGO_MAXIMO} o más)'))
    elif pendientes >= cupo:
        info.update(motivo=CUPO_CUBIERTO, mensaje=(
            f'no se generó: las {pendientes} pendientes ya cubren el cupo de '
            f'hoy ({cupo}/día)'))
    else:
        info['tope'] = cupo - pendientes
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Qué se cuenta primero
# ─────────────────────────────────────────────────────────────────────────────

def atraso_relativo(ultima_fecha, clase, ahora: datetime = None) -> float:
    """Días desde el último conteo, divididos por el intervalo de la clase.
    `1.0` = justo vencido; `2.0` = vencido hace un intervalo entero. Nunca
    contado → infinito: no hay medida más atrasada que «nunca»."""
    if ultima_fecha is None:
        return math.inf
    dias = ((ahora or datetime.utcnow()) - ultima_fecha).total_seconds() / 86400
    return max(dias, 0.0) / intervalo_dias(clase)


def clave_de_seleccion(atraso: float, clase, desempate) -> tuple:
    """Orden del generador: mayor atraso relativo primero; entre iguales —y
    todos los nunca contados son iguales: infinito—, A antes que B antes que C.
    De ahí sale «nunca contados de A primero». `desempate` lo fija el llamador
    (ids) para que dos corridas elijan lo mismo."""
    return (-atraso, RANGO_CLASE.get(clase, len(RANGO_CLASE) + 1), desempate)


def filtrar_elegibles(almacen_id: int, candidatos: list) -> tuple:
    """**Punto de extensión: qué huecos NO se deben programar hoy aunque estén
    vencidos.** Recibe `[(producto_id, ubicacion_id), ...]` y devuelve
    `(elegibles, excluidos)`, donde `excluidos` es `{motivo: cantidad}`.

    Hoy no excluye nada. Acá —y en ningún otro sitio— se conecta la exclusión
    de SKUs con mercancía en proceso (`ConteoService.mercancia_en_proceso*`,
    en construcción en paralelo): contar un SKU con un picking o una recepción
    a medias mide un número que se está moviendo. Lo usan el generador y el
    watchdog, así que una regla puesta acá rige para las dos puertas.
    """
    return list(candidatos), {}


def orden_de_reparto() -> tuple:
    """El orden en que se reparten los conteos a quien pide trabajo, como
    cláusulas `ORDER BY`. **Una sola definición** para el despachador de NB1,
    el de tienda, el intercalado durante el picking, la cola ya asignada de un
    operario, `asignar-lote` y `mis-tareas` (antes cada uno tenía el suyo: NB1
    solo por antigüedad, tienda A→B→C con las auditorías al final).

    1. Eventos (`TIPOS_EVENTO`): una auditoría por faltante antes que un
       conteo manual, y los dos antes que el plan.
    2. Clase: A, B, C, sin clase.
    3. Antigüedad, y el id para que el orden sea total.

    Decide el ORDEN, no QUÉ se reparte: eso es `filtros_pool_sin_dueno`.
    """
    nivel_evento = case(
        *[(SesionConteo.tipo == t, i) for i, t in enumerate(TIPOS_EVENTO)],
        else_=len(TIPOS_EVENTO),
    )
    rango_clase = case(RANGO_CLASE, value=SesionConteo.clasificacion_abc,
                       else_=len(RANGO_CLASE) + 1)
    return (nivel_evento, rango_clase, SesionConteo.fecha_creacion.asc(),
            SesionConteo.id.asc())
