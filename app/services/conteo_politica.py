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

## Tolerancias y topes (2026-09-23)

Hasta hoy **toda** diferencia ≠ 0 mandaba a un segundo conteo de otra persona,
y toda coincidencia CC1 == CC2 ajustaba sola **sin mirar cuánta plata movía**.
Con tres operarios eso es un segundo conteo por cada unidad suelta —la deriva
chica nunca se corrige porque nadie llega a contarla dos veces— y, del otro
lado, un faltante de millones entraba a Siesa sin que nadie lo firmara.

| Variable | Defecto | Qué es |
|---|---|---|
| `CONTEO_TOLERANCIA_UNIDADES` | `{"A":0,"B":1,"C":1}` | Unidades de diferencia aceptables por clase |
| `CONTEO_TOLERANCIA_PCT` | `{"A":0.5,"B":2,"C":5}` | Porcentaje del teórico aceptable por clase |
| `CONTEO_TOLERANCIA_TOPE_VALOR` | `20000` | Pesos: una diferencia que vale más no está «en tolerancia» aunque sean pocas unidades |
| `CONTEO_TOPE_AUTOAJUSTE` | `100000` | Pesos: ningún ajuste AUTOMÁTICO que valga más (tolerancia o CC1 == CC2) |
| `CONTEO_TOPE_APROBACION_JEFE` | `0` | Pesos: hasta cuánto aprueba un jefe de almacén. `0` = nada, que es lo que había |

**Dentro de tolerancia** (`evaluar_tolerancia`): `|dif| ≤ max(unidades_clase,
pct_clase × teórico)` **y** `|dif| × costo ≤ tope de valor`. Las dos: la
primera mide si la diferencia es chica para ESE producto; la segunda, si es
chica para la empresa — un ítem de $400.000 con una unidad de diferencia no es
deriva, es plata.

De dónde salen los números: son los que decidió el usuario, y siguen la forma
de la práctica ASCM (A estricta, C holgada). A tiene 0 unidades a propósito:
un ítem A mueve plata o disponibilidad, y el 0,5 % solo deja pasar diferencias
en huecos grandes (≥ 200 und). B y C admiten una unidad suelta — el error de
conteo más común — o el porcentaje, lo que sea mayor.

**Sin costo** en la foto (`None` o ≤ 0): el criterio de valor no se puede
aplicar; cuenta solo el de unidades y **se declara** (`sin_costo`). Pero ese
ajuste nunca sale solo: `motivo_tope_autoajuste` lo manda a aprobación. Regla
0: sin costo no se sabe cuánto vale.

**Qué clase se usa.** La del conteo si es del plan (`DIARIO_ABC`,
`WATCHDOG_ABC`) y es A, B o C. Todo lo demás —sin clase, auditorías por
excepción, conteos manuales— usa la regla de **A**, la más estricta: un conteo
que alguien pidió por una sospecha no se cierra con la holgura de un C
(`clase_de_tolerancia`, y el resultado declara `regla_por_defecto`). El conteo
manual nace con clase `C` por defecto (`crear_conteo_manual`): por eso la clase
del conteo no alcanza, hay que mirar el tipo.

**Topes en pesos.** `CONTEO_TOPE_AUTOAJUSTE` y `CONTEO_TOPE_APROBACION_JEFE`
son independientes de la tolerancia: el primero corta **todo** ajuste
automático (también el de CC1 == CC2, que antes no tenía techo), el segundo
dice hasta dónde firma un jefe de almacén. Supervisor y admin aprueban
cualquier monto.

**Recuentos con techo.** `RECUENTOS_PROPIOS_POR_CADENA` (1): fuera de
tolerancia, el mismo operario recuenta una vez, a ciegas, antes de gastar el
tiempo de otra persona. `MAX_RECUENTOS_POR_MOVIMIENTO` (2): si Siesa se sigue
moviendo mientras se cuenta, al tercer intento el conteo se bloquea para el
líder en vez de pedir recontar para siempre.
"""
import json
import logging
import math
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation

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

_ENV_TOL_UNIDADES = 'CONTEO_TOLERANCIA_UNIDADES'
_ENV_TOL_PCT = 'CONTEO_TOLERANCIA_PCT'
_ENV_TOL_TOPE_VALOR = 'CONTEO_TOLERANCIA_TOPE_VALOR'
_ENV_TOPE_AUTOAJUSTE = 'CONTEO_TOPE_AUTOAJUSTE'
_ENV_TOPE_JEFE = 'CONTEO_TOPE_APROBACION_JEFE'

#: Los nombres de las variables, para el trinquete de «un solo sitio».
VARIABLES_DE_ENTORNO = (_ENV_CUPO, _ENV_CUPO_BODEGA, _ENV_INTERVALOS, _ENV_WATCHDOG_DIAS,
                        _ENV_TOL_UNIDADES, _ENV_TOL_PCT, _ENV_TOL_TOPE_VALOR,
                        _ENV_TOPE_AUTOAJUSTE, _ENV_TOPE_JEFE)

#: Unidades de diferencia que se aceptan sin segundo conteo, por clase. Ver el
#: encabezado: A 0 (estricta), B y C una unidad suelta.
TOLERANCIA_UNIDADES_DEFECTO = {'A': 0, 'B': 1, 'C': 1}

#: Porcentaje del teórico que se acepta sin segundo conteo, por clase. En
#: PORCENTAJE (0,5 = 0,5 %), no en fracción: es como lo dice el negocio.
TOLERANCIA_PCT_DEFECTO = {'A': 0.5, 'B': 2.0, 'C': 5.0}

#: Pesos. Una diferencia que vale más que esto no es «chica» aunque sean pocas
#: unidades.
TOLERANCIA_TOPE_VALOR_DEFECTO = 20000

#: Pesos. Ningún ajuste automático —ni el de tolerancia ni el de CC1 == CC2—
#: sale a Siesa si vale más que esto: queda en DESCUADRE para que lo firme un
#: líder.
TOPE_AUTOAJUSTE_DEFECTO = 100000

#: Pesos. Hasta cuánto aprueba un jefe de almacén. 0 = nada: es exactamente lo
#: que había (la aprobación era solo de supervisor y admin) hasta que el
#: negocio decida otra cosa.
TOPE_APROBACION_JEFE_DEFECTO = 0

#: La clase cuya regla se aplica a lo que no es un conteo del plan con clase
#: A/B/C: la más estricta.
CLASE_REGLA_ESTRICTA = 'A'

#: Tipos de conteo cuya clase ABC decide la tolerancia. Los demás (auditoría
#: por excepción, manual) usan la regla estricta: alguien sospechaba algo.
TIPOS_CON_TOLERANCIA_DE_CLASE = ('DIARIO_ABC', 'WATCHDOG_ABC')

#: Cuántas veces el MISMO operario recuenta su conteo fuera de tolerancia antes
#: de que la cadena pase a un segundo conteo de otra persona. Una por cadena.
RECUENTOS_PROPIOS_POR_CADENA = 1

#: Cuántas veces se pide recontar por movimiento de Siesa durante el conteo.
#: Al siguiente la sesión se bloquea (`MOVIMIENTO_CONTINUO`) para el líder: un
#: producto que se vende sin parar no se cuenta insistiendo.
MAX_RECUENTOS_POR_MOVIMIENTO = 2


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


def _numero_no_negativo(valor):
    """Número finito ≥ 0 (entero o decimal), o `None`. Para porcentajes y
    pesos. `True`/`False` no son números acá."""
    if isinstance(valor, bool):
        return None
    try:
        n = float(str(valor).strip())
    except (TypeError, ValueError):
        return None
    return n if (math.isfinite(n) and n >= 0) else None


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

    tol_unidades = dict(TOLERANCIA_UNIDADES_DEFECTO)
    for clase, valor in (_json_env(_ENV_TOL_UNIDADES, advertencias) or {}).items():
        clase_n = str(clase).strip().upper()
        n = _entero_no_negativo(valor)
        if clase_n not in CLASES:
            advertencias.append(f'{_ENV_TOL_UNIDADES}: clase {clase!r} desconocida; se ignora')
        elif n is None:
            advertencias.append(f'{_ENV_TOL_UNIDADES}[{clase_n}]={valor!r} no es un entero '
                                f'≥ 0; se usa {TOLERANCIA_UNIDADES_DEFECTO[clase_n]}')
        else:
            tol_unidades[clase_n] = n

    tol_pct = dict(TOLERANCIA_PCT_DEFECTO)
    for clase, valor in (_json_env(_ENV_TOL_PCT, advertencias) or {}).items():
        clase_n = str(clase).strip().upper()
        n = _numero_no_negativo(valor)
        if clase_n not in CLASES:
            advertencias.append(f'{_ENV_TOL_PCT}: clase {clase!r} desconocida; se ignora')
        elif n is None or n > 100:
            advertencias.append(f'{_ENV_TOL_PCT}[{clase_n}]={valor!r} no es un porcentaje '
                                f'entre 0 y 100; se usa {TOLERANCIA_PCT_DEFECTO[clase_n]:g}')
        else:
            tol_pct[clase_n] = n

    def _pesos(nombre, defecto):
        crudo_p = os.environ.get(nombre, '').strip()
        if not crudo_p:
            return defecto
        n = _numero_no_negativo(crudo_p)
        if n is None:
            advertencias.append(f'{nombre}={crudo_p!r} no es un valor en pesos ≥ 0; '
                                f'se usa {defecto}')
            return defecto
        return n

    return {'cupo_global': cupo_global, 'cupo_por_bodega': por_bodega,
            'intervalos': intervalos, 'watchdog_dias': watchdog_dias,
            'tolerancia_unidades': tol_unidades, 'tolerancia_pct': tol_pct,
            'tolerancia_tope_valor': _pesos(_ENV_TOL_TOPE_VALOR, TOLERANCIA_TOPE_VALOR_DEFECTO),
            'tope_autoajuste': _pesos(_ENV_TOPE_AUTOAJUSTE, TOPE_AUTOAJUSTE_DEFECTO),
            'tope_aprobacion_jefe': _pesos(_ENV_TOPE_JEFE, TOPE_APROBACION_JEFE_DEFECTO),
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
        'tolerancia': descripcion_de_tolerancias(cfg),
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


def bloquear_cupo(almacen_id: int) -> None:
    """Serializa, dentro de la transacción en curso, a todo el que va a crear
    conteos contra el cupo de `almacen_id`.

    Sin esto, el cron y un «Generar lote» apretado a la misma hora leen las
    mismas pendientes, calculan el mismo tope y crean el doble. Es un lock de
    **transacción** (`lock_de_transaccion`), no de sesión: se suelta solo en
    el commit o el rollback, así que no puede quedar tomado en una conexión
    que vuelve al pool —el defecto que ya costó dos jobs muertos, ver
    `tests/test_advisory_locks.py`—. Espera en vez de rendirse: el que llega
    segundo lee las pendientes que el primero ya dejó. La clave sale del
    rango `RANGO_CUPO_CONTEO` del registro de `app/utils/lock.py`.
    """
    from app.utils.lock import RANGO_CUPO_CONTEO, clave_en_rango, lock_de_transaccion
    lock_de_transaccion(clave_en_rango(RANGO_CUPO_CONTEO, int(almacen_id)))


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

    **Mercancía en proceso** (2026-09-23): un SKU con un pedido recogido sin
    remisión, una recepción sin entrada a Siesa, una avería sin transferir, un
    traslado sin STS o una devolución sin NC no se programa. Su ajuste lo
    bloquearía igual (`motivo_bloqueo_ajuste`, caso 7): contarlo ahora gasta un
    cupo del día en un conteo que no puede servir. Se excluyen también los
    hallazgos «sin fecha» (Regla 0): no se sabe si la mercancía volvió al
    estante, y el conteo quedaría bloqueado. Esos no desaparecen: el mismo
    núcleo (`ConteoService.procesos_en_curso`) los lista con el documento a
    cerrar, y cerrarlo devuelve el SKU al plan.

    Un fallo al consultar NO se traga: sube, y la corrida diaria lo reporta
    por su correo de fallo. Generar sin saber qué está en proceso es
    exactamente lo que esta regla existe para impedir.
    """
    from app.services.conteo_service import ConteoService
    en_proceso = ConteoService.productos_con_mercancia_en_proceso(almacen_id)
    elegibles = [c for c in candidatos if c[0] not in en_proceso]
    excluidos = {}
    if len(elegibles) < len(candidatos):
        excluidos['mercancia_en_proceso'] = len(candidatos) - len(elegibles)
    return elegibles, excluidos


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


# ─────────────────────────────────────────────────────────────────────────────
# Tolerancias y topes en pesos
# ─────────────────────────────────────────────────────────────────────────────

#: Por qué un ajuste NO sale solo a Siesa (`motivo_tope_autoajuste`). Códigos
#: estables: las estadísticas agrupan por ellos, no por la prosa.
SIN_COSTO = 'SIN_COSTO'
SUPERA_TOPE = 'SUPERA_TOPE'


def pesos(valor) -> str:
    """`$1.234.567` — como se lee en Colombia."""
    return '$' + f'{float(valor):,.0f}'.replace(',', '.')


def costo_valido(costo):
    """El costo unitario con el que se puede valorizar, o `None`.

    `None`, ilegible, no finito o ≤ 0 → `None`: un costo cero diría «este
    ajuste no vale nada», y lo que pasa es que no se sabe cuánto vale (el
    mismo criterio de `ConteoService._costo_de_fila` y del reporte, que trata
    ≤ 0 como «sin valorizar»)."""
    if costo is None or isinstance(costo, bool):
        return None
    try:
        c = Decimal(str(costo))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return c if (c.is_finite() and c > 0) else None


def valor_de_diferencia(diferencia, costo):
    """`|diferencia| × costo` en pesos (`Decimal`), o `None` si no hay costo
    válido o no hay diferencia medida."""
    c = costo_valido(costo)
    if c is None or diferencia is None:
        return None
    return abs(Decimal(str(diferencia))) * c


def clase_de_tolerancia(tipo, clase) -> tuple:
    """`(clase cuya regla se aplica, regla_por_defecto)`.

    La clase del conteo solo decide si es un conteo del plan
    (`TIPOS_CON_TOLERANCIA_DE_CLASE`) con clase A, B o C. Lo demás —sin clase,
    auditoría por excepción, manual— usa la regla estricta (`A`), y se declara:
    un conteo pedido por una sospecha no se cierra con la holgura de un C, y
    el manual nace con clase `C` por defecto aunque nadie la haya decidido."""
    if tipo in TIPOS_CON_TOLERANCIA_DE_CLASE and clase in CLASES:
        return clase, False
    return CLASE_REGLA_ESTRICTA, True


def evaluar_tolerancia(diferencia, teorico, costo, *, tipo, clase) -> dict:
    """¿Esta diferencia es chica? **La única implementación** — la usan el
    registro del conteo (qué hacer con el primer conteo) y nadie más decide
    tolerancia por su cuenta.

    Dentro si `|dif| ≤ max(unidades_clase, pct_clase × teórico)` **y**
    `|dif| × costo ≤ tope de valor`. Sin costo válido el segundo criterio no se
    aplica y se declara (`sin_costo`). Justo en el límite es DENTRO (≤).

    Un teórico negativo (Siesa con más POS pendiente que existencia) no da
    holgura: el porcentaje se calcula sobre `max(teórico, 0)`. Regla 0.
    """
    cfg = _leer()
    regla, por_defecto = clase_de_tolerancia(tipo, clase)
    unidades = Decimal(str(cfg['tolerancia_unidades'][regla]))
    pct = Decimal(str(cfg['tolerancia_pct'][regla]))
    base = max(Decimal(str(teorico)) if teorico is not None else Decimal(0), Decimal(0))
    limite = max(unidades, pct * base / Decimal(100))
    dif = Decimal(str(diferencia or 0))
    dentro_unidades = abs(dif) <= limite
    valor = valor_de_diferencia(dif, costo)
    tope = Decimal(str(cfg['tolerancia_tope_valor']))
    dentro_valor = None if valor is None else valor <= tope
    dentro = bool(dentro_unidades and dentro_valor is not False)
    if dentro:
        motivo = 'dentro de tolerancia'
    elif not dentro_unidades:
        motivo = (f'la diferencia ({abs(dif):g} und) supera la tolerancia de la regla '
                  f'{regla} ({limite:g} und)')
    else:
        motivo = (f'la diferencia vale {pesos(valor)}, más que el tope de tolerancia '
                  f'de {pesos(tope)}')
    return {
        'dentro': dentro,
        'regla_clase': regla,
        'regla_por_defecto': por_defecto,
        'limite_unidades': float(limite),
        'dentro_por_unidades': bool(dentro_unidades),
        'valor': float(valor) if valor is not None else None,
        'tope_valor': float(tope),
        'dentro_por_valor': dentro_valor,
        'sin_costo': valor is None,
        'motivo': motivo,
    }


def motivo_tope_autoajuste(diferencia, costo):
    """Por qué un ajuste de `diferencia` unidades con este costo NO puede salir
    solo a Siesa, o `None` si puede. `{'codigo', 'mensaje'}`.

    Vale para TODO ajuste automático: el de tolerancia y el de CC1 == CC2.
    Sin costo válido no sale solo (Regla 0: no se sabe cuánto vale); si vale
    más que `CONTEO_TOPE_AUTOAJUSTE`, tampoco. En los dos casos el conteo
    queda en DESCUADRE y lo aprueba un líder (`ConteoService.confirmar_ajuste`).
    """
    valor = valor_de_diferencia(diferencia, costo)
    if valor is None:
        return {'codigo': SIN_COSTO, 'mensaje': (
            'sin costo en la foto de Siesa: no se sabe cuánto vale este ajuste, '
            'así que no sale solo — lo aprueba un líder')}
    tope = Decimal(str(_leer()['tope_autoajuste']))
    if valor > tope:
        return {'codigo': SUPERA_TOPE, 'mensaje': (
            f'el ajuste vale {pesos(valor)} y supera el tope de {pesos(tope)} para '
            'ajustes automáticos — lo aprueba un líder')}
    return None


def tope_aprobacion_jefe() -> Decimal:
    """Pesos hasta los que un jefe de almacén aprueba un ajuste. `0` = nada."""
    return Decimal(str(_leer()['tope_aprobacion_jefe']))


def descripcion_de_tolerancias(cfg: dict = None) -> dict:
    """Los valores VIGENTES de tolerancias y topes, para la pantalla y las
    estadísticas."""
    cfg = cfg or _leer()
    return {
        'unidades': dict(cfg['tolerancia_unidades']),
        'porcentaje': dict(cfg['tolerancia_pct']),
        'tope_valor_tolerancia': cfg['tolerancia_tope_valor'],
        'tope_autoajuste': cfg['tope_autoajuste'],
        'tope_aprobacion_jefe': cfg['tope_aprobacion_jefe'],
        'regla_sin_clase': CLASE_REGLA_ESTRICTA,
        'recuentos_propios_por_cadena': RECUENTOS_PROPIOS_POR_CADENA,
        'max_recuentos_por_movimiento': MAX_RECUENTOS_POR_MOVIMIENTO,
    }
