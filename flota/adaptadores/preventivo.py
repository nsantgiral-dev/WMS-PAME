"""
La puerta del preventivo: sembrar el plan desde la ficha, y registrar que se hizo.

`flota/dominio/preventivo.py` tiene la política —cuándo una tarea está vencida,
qué significa que nunca se haya ejecutado, qué umbral existe y cuál no—. Este
adaptador es lo único que la conecta con la base.

## La siembra es una traducción, no una captura

Nadie escribe una fila de `flota_plan_tarea` a mano en el caso normal. La ficha
técnica **ya dice** cada cuántos kilómetros toca cambiar la correa
(`distribucion_km_cambio`), qué aceite lleva el motor (`aceite_motor_spec`) y si
la transmisión final es por cadena. Sembrar es leer eso y dejarlo en filas que
se puedan comparar contra el odómetro.

Por eso la siembra es **idempotente y no destructiva**:

  · si la tarea no existe, la crea;
  · si existe y salió de la ficha (`origen='ficha'`), la ACTUALIZA cuando la
    ficha cambió — un `distribucion_km_cambio` corregido de 60.000 a 50.000 que
    no llegue al plan deja al plan mintiendo;
  · si existe y la escribió una persona (`origen='manual'`), **no la toca
    nunca**. Ese es el punto entero de la columna: sin ella, el intervalo del
    aceite que alguien llamó a preguntar se perdería en el próximo ciclo del
    cron, en silencio.
  · **no desactiva ni borra nada.** Que la ficha deje de declarar una caja no
    significa que el vehículo no la tenga; retirar una tarea es una decisión de
    persona y se hace desactivándola.

## Regla 10 — el cron nace apagado

`FLOTA_PREVENTIVO` enciende la siembra automática. Sin ella, `sembrar_todo`
devuelve su resumen con el motivo y no escribe nada. Ver `init_scheduler` al
final del archivo para el razonamiento del primer ciclo.

## El aviso de WhatsApp NO se manda, y no es un olvido

`flota/dominio/aviso.py::PLANTILLAS` tiene tres plantillas y **ninguna sirve
para esto**: las de hallazgo hablan de un daño reportado y la de documento habla
de una fecha de vencimiento. Una plantilla nueva se aprueba en Meta, que es una
dependencia de un tercero, y el canal entero ya está esperando los ids
definitivos de ese mismo tercero.

Inventar una acá dejaría en el código una plantilla que Gupshup rechaza en
tiempo de ejecución: superficie construida que falla el día que se enciende. La
deuda está declarada en `docs/flota/ESTADO.md` con su condición de disparo, y
cuando la plantilla exista **no hay política nueva que escribir**: el barrido
consume `diagnosticar`, que es la misma función que ya alimenta el tablero y el
health. Una política, una función.
"""
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional

from app.extensions import db
from flota.adaptadores.hallazgos import anclar_odometro
from flota.adaptadores.modelos import (EjecucionTarea, FichaTecnica,
                                       LecturaOdometro, PlanTarea)
from flota.dominio import odometro as dom_odo
from flota.dominio import preventivo as dom_prev
from flota.dominio.errores import ErrorFlota
from flota.dominio.preventivo import EstadoTarea, PlanInvalido
from flota.dominio.valores import Lectura, OrigenLectura

logger = logging.getLogger(__name__)


class PreventivoInvalido(ErrorFlota):
    """La operación sobre el plan no se puede hacer. No se hace a medias."""


def preventivo_encendido() -> bool:
    """Regla 10. Mismo gesto que `avisos_encendidos()`, y por el mismo motivo.

    Se lee en cada llamada y no al importar: una variable leída al arranque
    obliga a reiniciar el proceso para apagar algo, y el momento en que alguien
    quiere apagar un cron es el peor momento para pedirle un despliegue.
    """
    return (os.getenv('FLOTA_PREVENTIVO') or '').lower() == 'true'


# ── Qué tarea propone cada campo de la ficha ─────────────────────────────────
#
# **Es una tabla y no seis `if`.** La correspondencia campo → tarea es el
# contenido entero de la decisión de siembra, y escrita como código se lee peor
# y se audita peor. Cada entrada dice: qué tipo de tarea, de qué campo sale el
# intervalo (o `None` si la ficha no lo tiene), y de qué campo sale la
# procedencia.
#
# `intervalo` es `None` en cinco de las seis porque **la ficha declara QUÉ lleva
# el vehículo y no CADA CUÁNTOS KM se cambia**. Ese número no existe en ninguna
# columna, y ponerle uno por omisión sería comparar un odómetro real contra algo
# inventado. Ver `PlanTarea.intervalo_km`.
#
# La única con intervalo es `distribucion`, que es exactamente la pieza que el
# plan llama «la de mayor valor y cuesta una consulta»: el número ya está
# cargado y nadie lo lee.

def _tiene_texto(valor) -> bool:
    """Si el campo de la ficha declara algo, y `'sin_dato'` no declara nada.

    Sin `.get` y sin `or ''` sobre el resultado: la pregunta es si alguien
    escribió una especificación, y `'sin_dato'` es precisamente la palabra con
    la que la ficha dice que no.
    """
    return bool((valor or '').strip()) and (valor or '').strip() != 'sin_dato'


#: `(tipo, condición sobre la ficha, campo del intervalo, campo de la fuente)`.
_SIEMBRA = (
    ('distribucion',
     lambda f: f.distribucion_km_cambio is not None,
     'distribucion_km_cambio', 'distribucion_fuente'),
    ('aceite_motor',
     lambda f: _tiene_texto(f.aceite_motor_spec), None, None),
    ('aceite_caja',
     lambda f: _tiene_texto(f.aceite_caja_spec), None, None),
    ('aceite_diferencial',
     lambda f: _tiene_texto(f.aceite_diferencial_spec), None, None),
    ('refrigerante',
     lambda f: _tiene_texto(f.refrigerante_spec), None, None),
    ('lubricacion_cadena',
     lambda f: f.transmision_final == 'cadena', None, None),
)


def _propuestas(ficha: FichaTecnica) -> List[dict]:
    """Qué tareas propone esta ficha, con su intervalo y su procedencia.

    QUÉ AFIRMA: que cada propuesta sale de un campo que la ficha declara.

    QUÉ NO AFIRMA: que el vehículo necesite esa tarea. Que alguien haya escrito
    `aceite_caja_spec` dice que se levantó una especificación, no que el
    vehículo tenga caja que mantener. Retirar la que no aplica es una decisión
    de persona (`activo=False`), no un juicio de esta función.

    La procedencia se **hereda del campo que sembró la tarea**, no se elige: es
    la decisión 2 del plan. Cuando el campo no tiene `*_fuente` en la ficha —los
    cinco que no traen intervalo— la propuesta nace sin intervalo y con
    `fuente='sin_dato'`, que es lo único cierto: no hay número, así que no hay
    de dónde diga que salió.
    """
    salida = []
    for tipo, aplica, campo_km, campo_fuente in _SIEMBRA:
        if not aplica(ficha):
            continue
        intervalo = getattr(ficha, campo_km) if campo_km else None
        # Acceso directo por nombre y no `getattr(ficha, campo, 'sin_dato')`:
        # un campo de fuente que dejara de existir tiene que reventar acá y no
        # sembrar un plan entero con la procedencia borrada (regla 5).
        fuente = getattr(ficha, campo_fuente) if campo_fuente else 'sin_dato'
        # El CHECK de la tabla exige que los dos vayan juntos o ninguno. Una
        # ficha con `distribucion_km_cambio` cargado y `distribucion_fuente` en
        # `sin_dato` no puede existir —lo impide
        # `ck_flota_distribucion_con_procedencia` desde la tanda 1— pero si un
        # día pudiera, sembrar el intervalo sin procedencia sería publicar un
        # número con autoridad y sin respaldo. Se siembra sin intervalo.
        if intervalo is not None and fuente == 'sin_dato':
            logger.warning(
                '[FLOTA/PREVENTIVO] vehiculo %s: %s tiene intervalo %s y la '
                'ficha no dice de dónde salió; se siembra sin intervalo',
                ficha.vehiculo_id, tipo, intervalo)
            intervalo = None
        if intervalo is None:
            fuente = 'sin_dato'
        salida.append({'tipo': dom_prev.validar_tipo(tipo),
                       'intervalo_km': intervalo,
                       'fuente': dom_prev.validar_fuente(fuente)})
    return salida


def sembrar_desde_ficha(vehiculo_id: int, ahora: Optional[datetime] = None,
                        commit: bool = True) -> dict:
    """Traduce la ficha técnica de un vehículo a filas de plan.

    Devuelve un resumen —`{'creadas', 'actualizadas', 'sin_cambio',
    'respetadas_manual', 'propuestas'}`— y no `None`: una siembra que no dice
    qué hizo es indistinguible de una que no corrió.

    QUÉ AFIRMA al volver: que el plan de ese vehículo refleja lo que su ficha
    declara hoy, sin haber pisado nada que una persona hubiera escrito a mano.

    QUÉ NO AFIRMA: que el plan esté completo. Cinco de las seis tareas nacen
    sin intervalo porque la ficha no lo tiene, y quedan en `sin_intervalo` hasta
    que alguien lo levante. Eso **se cuenta** en el health, no se rellena.

    Levanta `PreventivoInvalido` si el vehículo no tiene ficha. Sembrar sin
    ficha no es sembrar poco: no hay de dónde sacar una sola tarea, y devolver
    un resumen en ceros haría creer que el vehículo no necesita mantenimiento.
    """
    ahora = ahora if ahora is not None else datetime.utcnow()
    ficha = FichaTecnica.query.get(vehiculo_id)
    if ficha is None:
        raise PreventivoInvalido(
            f'el vehículo {vehiculo_id} no tiene ficha técnica: el plan '
            f'preventivo se siembra DESDE la ficha, así que no hay de dónde '
            f'sacar una sola tarea. Se carga en «Ficha técnica».')

    existentes = {t.tipo: t for t in
                  PlanTarea.query.filter_by(vehiculo_id=vehiculo_id).all()}
    resumen = {'creadas': 0, 'actualizadas': 0, 'sin_cambio': 0,
               'respetadas_manual': 0, 'propuestas': 0}

    try:
        for p in _propuestas(ficha):
            resumen['propuestas'] += 1
            fila = existentes.get(p['tipo'])
            if fila is None:
                db.session.add(PlanTarea(
                    vehiculo_id=vehiculo_id, tipo=p['tipo'],
                    intervalo_km=p['intervalo_km'], fuente=p['fuente'],
                    origen='ficha', activo=True, sembrado_ts=ahora))
                resumen['creadas'] += 1
                continue

            # Lo que una persona escribió no se pisa. Es el motivo entero de
            # que exista la columna `origen`: sin ella, el intervalo que
            # alguien llamó a preguntar volvería al de la ficha en el próximo
            # ciclo del cron y nadie se enteraría.
            if fila.origen == 'manual':
                resumen['respetadas_manual'] += 1
                continue

            if (fila.intervalo_km == p['intervalo_km']
                    and fila.fuente == p['fuente']):
                resumen['sin_cambio'] += 1
                continue

            # La ficha cambió y esta fila salió de la ficha: sigue a la ficha.
            # No seguirla dejaría al plan comparando el odómetro contra un
            # intervalo que ya nadie sostiene.
            fila.intervalo_km = p['intervalo_km']
            fila.fuente = p['fuente']
            resumen['actualizadas'] += 1

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return resumen
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Un plan a medias es peor que ninguno — el tablero mostraría tres
        # tareas de seis y quien lo mire creería que ésas son todas.
        if commit:
            db.session.rollback()
        raise


def sembrar_todo(ahora: Optional[datetime] = None) -> dict:
    """Siembra el plan de **todos** los vehículos activos con ficha.

    Es lo que corre el cron. Devuelve un resumen agregado y **nunca levanta por
    un vehículo**: un vehículo sin ficha se cuenta en `sin_ficha` y el barrido
    sigue. Es la única degradación de este archivo y no es hacia el éxito —
    queda contada, sale en el log y no se parece a «todo bien».

    Regla 10: sin `FLOTA_PREVENTIVO=true` no escribe nada y devuelve el motivo.
    """
    from app.models.vehiculo import Vehiculo

    resumen = {'vehiculos': 0, 'sin_ficha': 0, 'creadas': 0,
               'actualizadas': 0, 'sin_cambio': 0, 'respetadas_manual': 0}
    if not preventivo_encendido():
        resumen['motivo'] = 'FLOTA_PREVENTIVO no está en true'
        return resumen

    ahora = ahora if ahora is not None else datetime.utcnow()
    for v in Vehiculo.query.filter(Vehiculo.activo.is_(True)).all():
        resumen['vehiculos'] += 1
        try:
            r = sembrar_desde_ficha(v.id, ahora=ahora)
        except PreventivoInvalido:
            resumen['sin_ficha'] += 1
            continue
        for k in ('creadas', 'actualizadas', 'sin_cambio', 'respetadas_manual'):
            resumen[k] += r[k]
    return resumen


# ── Registrar que se hizo ────────────────────────────────────────────────────

def registrar_ejecucion(*, plan_id: int, km: int, usuario_id: int,
                        taller: Optional[str] = None,
                        nota: Optional[str] = None,
                        ts: Optional[datetime] = None) -> EjecucionTarea:
    """Se hizo el mantenimiento. **Establece la línea base de la tarea.**

    QUÉ AFIRMA: que alguien registró que esta tarea se ejecutó, a ese
    kilometraje, en ese momento.

    QUÉ NO AFIRMA: que se haya hecho bien, ni que la haya hecho el taller que
    dice `taller`. Es un registro, no una verificación — igual que un hallazgo
    reportado no afirma que alguien sea responsable.

    El kilometraje entra por la misma puerta que todos los eventos de flota
    (regla 3) y con la misma política: `anclar_odometro` reutiliza la última
    lectura si el número coincide y sólo escribe una nueva si el odómetro se
    movió. Escribir una lectura por cada ejecución llenaría la serie de filas
    con el mismo segundo — el ruido que `lecturas_ts_duplicado` existe para
    contar, producido por el sistema en vez de por un reintento. Un cambio de
    aceite y una lubricación de cadena hechos en la misma visita al taller
    cuelgan de la misma lectura, que es lo que de verdad pasó.

    `origen=OT` y no un valor nuevo: la lectura nace porque el vehículo entró a
    mantenimiento, que es exactamente lo que ese origen nombra. Un
    `origen='preventivo'` sería una séptima palabra para el mismo gesto.

    Levanta si la tarea no existe, si está desactivada o si no tiene intervalo.
    **La última es la que importa**: registrar una ejecución sobre una tarea sin
    intervalo produce una línea base que no se puede comparar contra nada, y
    deja la tarea en `sin_intervalo` igual que antes. El que la registró se iría
    creyendo que quedó al día.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    fila = PlanTarea.query.get(plan_id)
    if fila is None:
        raise PreventivoInvalido(f'no existe la tarea de plan {plan_id}')
    if not fila.activo:
        raise PreventivoInvalido(
            f'la tarea {fila.tipo!r} del vehículo {fila.vehiculo_id} está '
            f'retirada: alguien decidió que no aplica. Reactivarla es una '
            f'decisión, no un efecto de registrar una ejecución.')
    if fila.intervalo_km is None:
        raise PreventivoInvalido(
            f'la tarea {fila.tipo!r} no tiene intervalo: la ficha no dice cada '
            f'cuántos kilómetros toca. Registrar la ejecución dejaría una línea '
            f'base que no se puede comparar contra nada, y quien la registre se '
            f'iría creyendo que el vehículo quedó al día. Primero se fija el '
            f'intervalo, con su procedencia.')

    try:
        lectura = anclar_odometro(fila.vehiculo_id, km, usuario_id, ahora,
                                  origen=OrigenLectura.OT)
        ejecucion = EjecucionTarea(
            plan_id=fila.id, lectura_id=lectura.id, ejecutado_ts=ahora,
            registrado_por_usuario_id=usuario_id,
            taller=(taller or '').strip() or None,
            nota=(nota or '').strip() or None)
        db.session.add(ejecucion)
        db.session.commit()
        return ejecucion
    except Exception:
        db.session.rollback()
        raise


def fijar_intervalo(*, plan_id: int, intervalo_km: Optional[int],
                    fuente: str, activo: Optional[bool] = None,
                    nota: Optional[str] = None) -> PlanTarea:
    """Lo que la ficha no dice y alguien averiguó. **Con su procedencia.**

    Es la puerta por la que las cinco tareas que nacen en `sin_intervalo`
    dejan de estarlo: alguien llama al concesionario, mira el manual, o le
    pregunta al taller, y escribe el número **junto con de dónde salió**.

    `fuente` es obligatoria y se valida contra el vocabulario de la ficha. No
    tiene default: un intervalo escrito sin procedencia se lee después como si
    alguien lo hubiera verificado, y ésa es la forma en que un dato inventado se
    vuelve un dato de autoridad. El CHECK de la tabla lo respalda en las dos
    direcciones — sin intervalo tampoco puede haber fuente.

    La fila pasa a `origen='manual'` y **la siembra deja de tocarla**. Es lo que
    impide que el próximo ciclo del cron devuelva el intervalo al de la ficha,
    en silencio.

    `activo=False` retira la tarea sin borrarla: borrarla la haría volver en la
    próxima siembra y dejaría huérfanas sus ejecuciones, que son historia real.
    """
    fila = PlanTarea.query.get(plan_id)
    if fila is None:
        raise PreventivoInvalido(f'no existe la tarea de plan {plan_id}')

    if intervalo_km is not None:
        if intervalo_km <= 0:
            raise PreventivoInvalido(
                f'intervalo de {intervalo_km} km: un intervalo de cero o '
                f'negativo dejaría la tarea vencida todos los días, sobre '
                f'cualquier odómetro.')
        if dom_prev.validar_fuente(fuente) == 'sin_dato':
            raise PreventivoInvalido(
                'un intervalo sin procedencia se lee después como si alguien '
                'lo hubiera verificado. Decí de dónde salió: '
                + ', '.join(f for f in dom_prev.FUENTES if f != 'sin_dato'))
    else:
        # Sin intervalo no puede haber fuente: una procedencia colgada de un
        # dato ausente afirma un levantamiento que no ocurrió, y es justo lo
        # que haría creer que la tarea ya está armada.
        dom_prev.validar_fuente(fuente)
        fuente = 'sin_dato'

    fila.intervalo_km = intervalo_km
    fila.fuente = fuente
    fila.origen = 'manual'
    if activo is not None:
        fila.activo = bool(activo)
    if nota is not None:
        fila.nota = (nota or '').strip() or None
    db.session.commit()
    return fila


# ── Leer: el diagnóstico ─────────────────────────────────────────────────────

def _lecturas_dominio(vehiculo_id: int) -> List[Lectura]:
    """Las lecturas del vehículo como las entiende el dominio.

    `confianza` se pasa REAL y no por default: `Lectura` la trae en
    `DECLARADA` para los llamadores que construyen una lectura propuesta, y
    usarla acá diría que ninguna es dudosa. El ritmo saldría publicable sobre
    los mismos números que `confianza_del_tramo` existe para excluir.
    """
    from flota.dominio.valores import Confianza

    return [Lectura(valor_km=l.valor_km, ts=l.ts,
                    origen=OrigenLectura(l.origen),
                    autor_usuario_id=l.autor_usuario_id,
                    motivo_correccion=l.motivo_correccion,
                    confianza=Confianza(l.confianza))
            for l in LecturaOdometro.query.filter_by(
                vehiculo_id=vehiculo_id).all()]


def ritmo_de(vehiculo_id: int) -> dom_odo.RitmoDeUso:
    """El km/día del vehículo. **El cálculo es del dominio, no de una consulta.**

    Una regla escrita dentro de un `SELECT` es una regla que se copia a la
    segunda pantalla y diverge — el corolario de la regla 0, que en este repo ya
    costó 25× de sobrecompra. `km_por_dia` vive en `flota/dominio/odometro.py`
    junto a `confianza_del_tramo`, y acá sólo se le traen las filas.
    """
    return dom_odo.km_por_dia(_lecturas_dominio(vehiculo_id))


def _ultimas_ejecuciones(plan_ids) -> Dict[int, int]:
    """`{plan_id: km de la última ejecución}`, en una sola pasada.

    De una pasada y no una consulta por tarea: el health recorre el plan de la
    flota entera, y seis vehículos por seis tareas son treinta y seis viajes a
    la base para contestar una pregunta.

    «La última» es la de mayor `ejecutado_ts`, con desempate por `id`. **No la
    de mayor kilometraje**: si alguien registró una ejecución con un odómetro
    mal tecleado, la línea base tiene que ser la última que ocurrió y no la que
    quedó más arriba — con la segunda, un dedo torcido correría el próximo
    cambio años hacia adelante y la tarea nunca volvería a aparecer.
    """
    if not plan_ids:
        return {}
    filas = (db.session.query(EjecucionTarea, LecturaOdometro)
             .join(LecturaOdometro,
                   EjecucionTarea.lectura_id == LecturaOdometro.id)
             .filter(EjecucionTarea.plan_id.in_(list(plan_ids))).all())
    ultima: Dict[int, tuple] = {}
    for ej, lec in filas:
        clave = (ej.ejecutado_ts, ej.id)
        if ej.plan_id not in ultima or clave > ultima[ej.plan_id][0]:
            ultima[ej.plan_id] = (clave, lec.valor_km)
    return {plan_id: km for plan_id, (_c, km) in ultima.items()}


def diagnostico_de(vehiculo_id: int) -> List[dict]:
    """El plan del vehículo, cada tarea con su estado y sus insumos.

    Devuelve una lista de dicts listos para la pantalla y el health. Cada uno
    trae el estado **y** el intervalo, el próximo kilometraje, los kilómetros
    que faltan, los días estimados y la procedencia — porque un estado suelto no
    se puede auditar ni explicar, igual que un CPK suelto.

    Sólo las tareas **activas**: una retirada no está al día ni vencida, está
    retirada, y contarla en cualquiera de los dos cubos volvería a meter en el
    tablero lo que alguien decidió sacar.

    El orden es el de `TIPOS_TAREA`, que es el de gravedad de la falla que
    previenen. Una correa de distribución vencida y una cadena sin lubricar no
    se leen en el orden en que la base las devuelva.
    """
    filas = [t for t in
             PlanTarea.query.filter_by(vehiculo_id=vehiculo_id).all()
             if t.activo]
    if not filas:
        return []

    ritmo = ritmo_de(vehiculo_id)
    odometro = dom_odo.odometro_actual(_lecturas_dominio(vehiculo_id))
    ultimas = _ultimas_ejecuciones([t.id for t in filas])

    orden = {tipo: i for i, tipo in enumerate(dom_prev.TIPOS_TAREA)}
    salida = []
    for t in sorted(filas, key=lambda f: orden[f.tipo]):
        # `ultimas.get(t.id)` de UN argumento: la ausencia de clave significa
        # «nunca se ejecutó», que es el significado documentado de `None` en
        # `Tarea.ultima_ejecucion_km` y sale traducido a la palabra
        # `sin_linea_base`. Un segundo argumento con default sería la
        # degradación que el trinquete 2 persigue.
        ultima_km = ultimas[t.id] if t.id in ultimas else None
        d = dom_prev.diagnosticar(t.a_dominio(ultima_km), odometro, ritmo)
        salida.append({
            'plan_id': t.id,
            'tipo': d.tipo,
            'nombre': dom_prev.nombre_tarea(d.tipo),
            'estado': d.estado,
            'intervalo_km': d.intervalo_km,
            'proximo_km': d.proximo_km,
            'km_restante': d.km_restante,
            'dias_estimados': d.dias_estimados,
            'fuente': d.fuente,
            # Se publica calculado y no se deja deducir de `fuente`: deducirlo
            # en la pantalla sería la segunda copia de la política, y la copia
            # de la pantalla es la que la gente mira.
            'fuente_blanda': d.fuente_blanda,
            'origen': t.origen,
            # La palabra y no `null`: «nunca se ejecutó» tiene que llegar a la
            # pantalla como algo que se lee, no como un hueco que invite a
            # `valor || 0` del otro lado (regla 4).
            'ultima_ejecucion_km': (ultima_km if ultima_km is not None
                                    else dom_prev.SIN_DATO),
            'nota': t.nota,
        })
    return salida


def diagnostico_de_la_flota() -> List[dict]:
    """El diagnóstico de todos los vehículos activos, para el health.

    Se recorre por vehículo y no con una consulta que junte todo: el ritmo de
    uso y el odómetro son por vehículo, y una consulta única tendría que
    reimplementar `km_por_dia` en SQL. Con seis vehículos el costo es
    irrelevante y la política queda escrita una sola vez.
    """
    from app.models.vehiculo import Vehiculo

    salida = []
    for v in Vehiculo.query.filter(Vehiculo.activo.is_(True)).all():
        for d in diagnostico_de(v.id):
            d = dict(d)
            d['vehiculo_id'] = v.id
            d['placa'] = v.placa
            salida.append(d)
    return salida


def contar_por_estado() -> Dict[str, int]:
    """Cuántas tareas de la flota hay en cada estado. **Total sobre el
    vocabulario**, así que ningún estado nuevo se puede quedar sin contar en
    silencio.

    Los cinco cubos van separados y no se suman: `vencida` pide taller,
    `sin_intervalo` pide una llamada al concesionario y `sin_linea_base` pide
    registrar lo que ya se hizo. Un solo total no dice a quién llamar, que es
    la lección de los 639 avisos conocidos.
    """
    cuenta = {e: 0 for e in dom_prev.ESTADOS_TAREA}
    for d in diagnostico_de_la_flota():
        # Indexado directo, sin `.setdefault`: un estado fuera del vocabulario
        # tiene que reventar acá y no sumarse a un cubo inventado.
        cuenta[d['estado']] += 1
    return cuenta


__all__ = ['PreventivoInvalido', 'preventivo_encendido', 'sembrar_desde_ficha',
           'sembrar_todo', 'registrar_ejecucion', 'fijar_intervalo',
           'ritmo_de', 'diagnostico_de', 'diagnostico_de_la_flota',
           'contar_por_estado', 'EstadoTarea', 'PlanInvalido']


def init_scheduler(app):
    """Cron diario 05:30 Bogotá — la siembra del plan desde la ficha.

    **Nace apagado** (regla 10): sin `FLOTA_PREVENTIVO=true`, `sembrar_todo`
    devuelve `{'motivo': 'FLOTA_PREVENTIVO no está en true'}` sin escribir una
    fila. El interruptor es uno solo y vive dentro de la función que escribe,
    igual que `avisos_encendidos()` — dos sitios donde apagar lo mismo garantizan
    que se olvide el segundo.

    ## Qué hace el PRIMER ciclo, que es la pregunta que manda

    Con las seis fichas de producción, el primer ciclo escribe hasta treinta y
    seis filas de plan y **manda cero avisos**. No por suerte y no porque la
    variable esté apagada: porque ninguna de esas tareas tiene una ejecución
    registrada, y una tarea sin línea base **no está al día ni vencida** — no
    hay contra qué comparar. El escenario que la regla 10 teme —cuarenta tareas
    «vencidas» el día uno y cuarenta WhatsApp a las seis de la mañana— está
    cerrado en el dominio, antes de que la variable haga falta.

    Y aun así nace apagada, por dos razones que no dependen de que ese análisis
    sea correcto:

    · **La regla 10 no se dobla con un argumento.** Si el razonamiento de
      arriba tuviera un hueco, el costo de descubrirlo con la variable apagada
      es leer un log; con la variable encendida es un canal silenciado.
    · **El ciclo peligroso no es el primero.** El día que alguien cargue de
      corrido el historial del taller —diez ejecuciones viejas de un golpe—, el
      ciclo SIGUIENTE es el que puede pasar media flota a `vencida` a la vez. Es
      ese ciclo el que hay que poder mirar mientras corre, y no se puede mirar
      un cron que ya venía andando solo.

    A las 05:30 y no a las 06:00: el barrido de avisos corre a las 06:00 y lee
    el plan. Al revés, el aviso del día miraría el plan de ayer — media hora de
    diferencia que no cuesta nada y que evita una clase entera de «ayer no
    salió y hoy tampoco sé por qué».
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[FLOTA_PREVENTIVO] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import advisory_lock

            # 2017 — libre; 2016 lo usa el barrido de avisos. Con
            # `--workers=2` dos procesos disparan el mismo cron, y dos siembras
            # simultáneas chocan contra `uq_flota_plan_tarea`: la segunda
            # reventaría con un IntegrityError en el log, todos los días.
            with advisory_lock(2017, 'flota_preventivo_siembra') as tomado:
                if not tomado:
                    return
                try:
                    r = sembrar_todo()
                except Exception as e:
                    logger.exception('[FLOTA_PREVENTIVO] la siembra falló: %s', e)
                    return
                if r.get('motivo'):
                    logger.info('[FLOTA_PREVENTIVO] siembra sin efecto — %s',
                                r['motivo'])
                elif r.get('creadas') or r.get('actualizadas') or r.get('sin_ficha'):
                    # `sin_ficha` va al mismo nivel que un cambio y no al de
                    # «no pasó nada»: un vehículo sin ficha no tiene plan, y un
                    # vehículo sin plan es invisible en el tablero de
                    # preventivo. La ausencia no se ve sola.
                    logger.warning(
                        '[FLOTA_PREVENTIVO] %s vehículos · %s creadas · '
                        '%s actualizadas · %s respetadas (manual) · '
                        '%s sin ficha',
                        r.get('vehiculos'), r.get('creadas'),
                        r.get('actualizadas'), r.get('respetadas_manual'),
                        r.get('sin_ficha'))
                else:
                    logger.info('[FLOTA_PREVENTIVO] %s vehículos revisados, '
                                'el plan ya reflejaba la ficha',
                                r.get('vehiculos'))

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        _job, CronTrigger(hour=5, minute=30),
        id='flota_preventivo_siembra', replace_existing=True,
        max_instances=1, misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info('[FLOTA_PREVENTIVO] Scheduler activo — siembra diaria 05:30 Bogotá')
    # El retorno es lo que `_registrar_scheduler` usa para no mentir sobre lo
    # que está corriendo: agregarse a la lista por «no lanzar excepción» es cómo
    # doce crones apagados se reportaron activos. Ver `app/__init__.py`.
    return scheduler
