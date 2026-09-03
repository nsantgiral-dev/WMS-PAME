"""
Dónde nace un daño — el pedido «llevar control sobre los daños que pasan».

`flota/dominio/hallazgo.py` tiene el canon completo desde el 2026-08-03: cuándo
un hallazgo entra al indicador, cuántos días estuvo abierto, cuándo está
vencido. Y hasta hoy **no tenía un solo caller**, porque no existía la tabla.
Política escrita, probada y sin nada sobre qué pronunciarse.

Este adaptador es la puerta. Tres verbos y ninguno más:

```
reportar  →  ABIERTO ──── cerrar ────→ CERRADO
                 │
                 ├──── descartar ────→ DESCARTADO   (exige motivo escrito)
                 └──── aplazar ─────→  ABIERTO      (fecha_limite + n, contador +1)
```

`no_aplica` no tiene verbo acá: significa «el vehículo se dio de baja antes de
reparar», y darlo de baja es otra operación — el hallazgo lo hereda, no lo elige.

## Las cuatro decisiones que no son del que llama

· **La fecha límite se calcula** (regla 6). El plazo sale de la criticidad, y la
  criticidad se declara. Si el plazo se pudiera escribir a dedo, «bloqueante»
  pasaría a ser una etiqueta que se negocia.
· **La custodia se deriva**, no se recibe. Es el hecho de bajo la custodia de
  quién apareció el daño; dejarlo en manos del que reporta sería dejarle elegir
  a quién se lo cuelga, y ningún automatismo imputa responsabilidad (regla 2).
· **`linea_base` se deriva** de la custodia. Un daño encontrado mientras todavía
  se está levantando lo que ya había no le cuenta a nadie.
· **El kilometraje entra por la misma puerta** (regla 3). No hay `reportar` sin
  odómetro, y el `lectura_id` es NOT NULL en la base para que no dependa de que
  alguien se acuerde.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA, inicio_del_dia_utc
from flota.adaptadores.modelos import Hallazgo, LecturaOdometro
from flota.adaptadores.traspaso import custodia_activa
from flota.dominio import inspeccion as dom_insp
from flota.dominio import odometro as dom_odo
from flota.dominio.errores import ErrorFlota
from flota.dominio.hallazgo import EstadoHallazgo
from flota.dominio.valores import Lectura, OrigenLectura


class HallazgoInvalido(ErrorFlota):
    """La operación sobre el hallazgo no se puede hacer. No se hace a medias."""


#: Cuántos días suma un aplazamiento. **Fijo, no elegido**: si quien aplaza
#: pudiera poner el número, el plazo volvería a ser negociable por la puerta de
#: atrás — que es justo lo que la regla 6 cierra por la de adelante. Siete días
#: porque es la ventana de un `mayor`: aplazar un bloqueante una semana ya es
#: una decisión que tiene que dejar rastro, y lo deja en `aplazado_veces`.
DIAS_POR_APLAZAMIENTO = 7


def _instante_limite(reportado_ts: datetime, criticidad: str) -> datetime:
    """La fecha límite como instante UTC-naive, que es como guarda el módulo.

    El dominio dice **qué día** es el último a tiempo. Acá se convierte a
    instante, y la conversión es una sola: la medianoche de Bogotá del día
    siguiente. Un `mayor` reportado el lunes vence cuando termina el lunes de
    la semana que viene, no a la hora exacta en que se reportó.

    Se pasa por Bogotá y no por UTC porque **el día que alguien lee es el suyo**
    (regla 5 del WMS). Un hallazgo reportado a las 8 p.m. de Colombia ya es del
    día siguiente en UTC: con `utcnow().date()` un bloqueante de la noche
    nacería vencido antes de que el mecánico llegue por la mañana.
    """
    dia_reporte = (reportado_ts.replace(tzinfo=timezone.utc)
                   .astimezone(TZ_BOGOTA).date())
    ultimo_dia = dom_insp.dia_limite(dia_reporte, criticidad)
    return inicio_del_dia_utc(ultimo_dia + timedelta(days=1))


def anclar_odometro(vehiculo_id: int, km: int, autor_usuario_id: int,
                    ahora: datetime,
                    origen: OrigenLectura = OrigenLectura.HALLAZGO
                    ) -> LecturaOdometro:
    """La lectura a la que se ata un evento de flota. Regla 3, con un hecho.

    QUÉ AFIRMA la lectura devuelta: que el odómetro de ese vehículo decía ese
    número. **No** afirma que se haya tomado para este evento.

    Si el número coincide con la última lectura, **se reutiliza esa fila**. No
    es un ahorro: escribir una lectura nueva idéntica cada vez que alguien
    reporta algo llenaría la serie de filas con el mismo segundo — exactamente
    el ruido que `lecturas_ts_duplicado` existe para contar, producido por el
    sistema en vez de por un reintento. Tres hallazgos de una misma revisión
    cuelgan de la misma lectura, que es lo que de verdad pasó. Desde el
    2026-09-01 la inspección diaria cuelga de esa misma lectura sus hallazgos y
    la inspección entera: una mirada al tablero, una lectura.

    Si el número es distinto, el odómetro se movió y eso es información nueva:
    nace una lectura. Descartar el número que el reportante tecleó y colgarlo
    de la lectura vieja guardaría un kilometraje que nadie midió — que es la
    falla que este módulo entero persigue.

    **`origen` es parámetro y no constante desde el 2026-09-01**, y por eso es
    pública: la inspección diaria necesitaba exactamente esta política —el
    mismo criterio de reutilización, la misma validación de monotonía— con otro
    gesto detrás. La alternativa era una segunda función igual con una palabra
    cambiada, que es cómo empieza la divergencia que la regla 0 del WMS cuenta;
    o dejar que la inspección escribiera `origen='hallazgo'`, una lectura que
    dice de dónde vino y miente. La columna `origen` existe justamente para no
    tener que adivinarlo.

    El default sigue siendo `HALLAZGO` para no cambiar el significado de la
    única llamada que existía.
    """
    previas = LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()

    ultima = max(previas, key=lambda l: (l.ts, l.id), default=None)
    if ultima is not None and ultima.valor_km == km:
        return ultima

    # El dominio juzga ANTES de escribir: un odómetro que retrocede no entra
    # por esta puerta más de lo que entra por el recibo de turno.
    dom_odo.validar_lectura(
        [Lectura(valor_km=l.valor_km, ts=l.ts, origen=OrigenLectura(l.origen),
                 autor_usuario_id=l.autor_usuario_id,
                 motivo_correccion=l.motivo_correccion)
         for l in previas],
        Lectura(valor_km=km, ts=ahora, origen=origen,
                autor_usuario_id=autor_usuario_id),
    )
    nueva = LecturaOdometro(
        vehiculo_id=vehiculo_id, valor_km=km, ts=ahora,
        origen=origen.value,
        autor_usuario_id=autor_usuario_id,
    )
    db.session.add(nueva)
    db.session.flush()
    return nueva


def reportar(
    *,
    vehiculo_id: int,
    criticidad: str,
    descripcion: str,
    km: int,
    reportado_por_usuario_id: int,
    item_id: Optional[int] = None,
    fotos: Optional[List[dict]] = None,
    ts: Optional[datetime] = None,
    commit: bool = True,
) -> Hallazgo:
    """Registra un daño. Devuelve el hallazgo con su reloj ya corriendo.

    QUÉ AFIRMA al devolver: que el daño quedó escrito con severidad, fecha
    límite calculada, kilometraje real y el rastro de bajo qué custodia
    apareció.

    QUÉ NO AFIRMA: que alguien sea responsable. `reportado_por_usuario_id` es
    quien lo vio y `custodia_id` es bajo la custodia de quién apareció — dos
    hechos. Quién responde lo decide un humano fuera de esta tabla (regla 2).

    Tampoco afirma que haya fotos. Se aceptan y se cuelgan, pero no se exigen:
    un daño reportado sin foto sigue siendo un daño reportado, y bloquear ahí
    enseña a no reportar.

    Levanta antes de tocar nada si la criticidad no existe o si la descripción
    viene vacía. Sin `.get(x, default)` en ninguna de las dos: una criticidad
    desconocida degradada a `menor` sería un bloqueante con treinta días de
    plazo (regla 5).

    ## `commit=False` — quién es dueño de la transacción

    Con el default (`True`) esto es lo de siempre: una llamada, una
    transacción, y si algo falla la base queda como estaba.

    `commit=False` existe para **una sola cosa**: que la inspección diaria
    pueda hacer nacer sus hallazgos DENTRO de su propia transacción. Es la
    regla 6 la que lo obliga — el plazo se calcula al nacer y hay una sola
    puerta donde eso pasa; escribir un segundo `INSERT INTO flota_hallazgo`
    desde la inspección habría sido la segunda vía de nacimiento, y la segunda
    es siempre la que se olvida de calcular la fecha límite.

    En ese modo esta función **no hace commit ni rollback**: hace `flush`, así
    que la fila ya tiene `id` para colgarle la respuesta del ítem, y deja la
    transacción abierta. **El que la abrió la cierra** — si algo revienta, la
    excepción sube intacta y es el llamador quien hace `rollback`. Un rollback
    acá adentro descartaría también la inspección a medio escribir de la que
    este hallazgo forma parte, sin que el llamador se entere de que perdió más
    de lo que pidió.

    Una inspección con la mitad de sus hallazgos escritos es peor que ninguna:
    el veredicto diría una cosa y las filas hijas otra.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    if not (descripcion or '').strip():
        raise HallazgoInvalido(
            'un hallazgo sin descripción no lo puede atender nadie: quien lo '
            'lea mañana no va a saber qué buscar en el vehículo.'
        )
    try:
        limite = _instante_limite(ahora, criticidad)
    except ValueError as e:
        # El dominio ya explica cuáles son válidas; se re-envuelve para que la
        # frontera devuelva 400 y no 500 — es un dato malo, no un fallo.
        raise HallazgoInvalido(str(e))

    custodia = custodia_activa(vehiculo_id)

    try:
        lectura = anclar_odometro(vehiculo_id, km, reportado_por_usuario_id, ahora)

        fila = Hallazgo(
            vehiculo_id=vehiculo_id,
            criticidad=criticidad,
            descripcion=descripcion.strip(),
            reportado_ts=ahora,
            reportado_por_usuario_id=reportado_por_usuario_id,
            lectura_id=lectura.id,
            custodia_id=custodia.id if custodia is not None else None,
            fecha_limite=limite,
            estado=EstadoHallazgo.ABIERTO,
            item_id=item_id,
            # Sin cadena de custodia establecida nadie recibió el vehículo
            # intacto, así que el daño no se le puede empezar a contar a nadie.
            # `custodia is None` cuenta como línea base por lo mismo: es el
            # lado conservador (regla 0), y lo conservador acá es NO imputar.
            linea_base=(custodia is None or bool(custodia.linea_base)),
        )
        db.session.add(fila)
        db.session.flush()

        _colgar_fotos(fotos, fila.id, reportado_por_usuario_id, ahora)

        if commit:
            db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Un hallazgo a medias —con lectura y sin fila— es peor que ninguno.
        #
        # Con `commit=False` la transacción es del llamador y el rollback
        # también: deshacerla acá se llevaría por delante la inspección que la
        # abrió, y el llamador vería una excepción sin saber cuánto perdió.
        if commit:
            db.session.rollback()
        raise


def cerrar(*, hallazgo_id: int, usuario_id: int, ts: Optional[datetime] = None,
           nota: Optional[str] = None) -> Hallazgo:
    """El vehículo volvió reparado.

    `cerrado_ts` es **cuando el vehículo vuelve**, no cuando se aprueba la OT ni
    cuando entra al taller — así lo fija el canon, y por eso un vehículo tres
    semanas en taller no muestra el indicador limpio.

    Cerrar dos veces levanta. Un segundo cierre movería `cerrado_ts` hacia
    adelante y acortaría o alargaría un número que ya se publicó; el silencio
    («ya estaba cerrado, no pasa nada») haría que un cierre equivocado se
    tapara con otro.
    """
    fila = _abierto_o_error(hallazgo_id, 'cerrar')
    fila.estado = EstadoHallazgo.CERRADO
    fila.cerrado_ts = ts if ts is not None else datetime.utcnow()
    fila.cerrado_por_usuario_id = usuario_id
    if (nota or '').strip():
        fila.motivo_cierre = nota.strip()
    db.session.commit()
    return fila


def descartar(*, hallazgo_id: int, usuario_id: int, motivo: str,
              ts: Optional[datetime] = None) -> Hallazgo:
    """No era un hallazgo. Exige motivo escrito, y el CHECK lo respalda.

    Es la única salida que no requiere reparar nada, así que es la que usaría
    quien no quiere hacer el trabajo (regla 11). Lo que la hace cara no es
    prohibirla: es que **queda escrito quién dijo que no era nada y por qué**, y
    que el descartado no entra al indicador ni como cerrado — no mejora ningún
    promedio.

    `cerrado_ts` se llena igual: un descarte también ocurre en un momento, y sin
    él no se puede saber cuánto tardó alguien en decidir que no era nada.
    """
    if not (motivo or '').strip():
        raise HallazgoInvalido(
            'descartar exige motivo escrito: sin él, un hallazgo incómodo y uno '
            'que de verdad no era nada se ven exactamente igual.'
        )
    fila = _abierto_o_error(hallazgo_id, 'descartar')
    fila.estado = EstadoHallazgo.DESCARTADO
    fila.cerrado_ts = ts if ts is not None else datetime.utcnow()
    fila.cerrado_por_usuario_id = usuario_id
    fila.motivo_cierre = motivo.strip()
    db.session.commit()
    return fila


def aplazar(*, hallazgo_id: int, usuario_id: int, motivo: str) -> Hallazgo:
    """Corre la fecha límite y **deja el contador subiendo**.

    QUÉ AFIRMA: que hay más plazo.

    QUÉ NO AFIRMA: que el hallazgo esté gestionado. `reportado_ts` no se toca,
    así que `dias_transcurridos` sigue contando desde el día uno — aplazar
    cambia cuándo se considera vencido, no borra el tiempo que lleva abierto.

    `aplazado_veces` es lo que vuelve visible el aplazamiento crónico: uno
    aplazado cuatro veces no está gestionado, está evitado, y sin el contador
    los dos casos se ven igual de verdes.
    """
    if not (motivo or '').strip():
        raise HallazgoInvalido(
            'aplazar exige motivo escrito: un plazo que se mueve sin razón '
            'anotada es un plazo que no existe.'
        )
    fila = _abierto_o_error(hallazgo_id, 'aplazar')
    fila.fecha_limite = fila.fecha_limite + timedelta(days=DIAS_POR_APLAZAMIENTO)
    fila.aplazado_veces = (fila.aplazado_veces or 0) + 1
    # A la BITÁCORA, no a `motivo_cierre`: el hallazgo sigue abierto y no se
    # está cerrando nada. Se acumula en vez de sobrescribir — el tercer
    # aplazamiento no borra por qué se hizo el primero, que es justo lo que hay
    # que poder leer después.
    sello = f'[aplazado x{fila.aplazado_veces} por usuario {usuario_id}] {motivo.strip()}'
    fila.bitacora = f'{fila.bitacora}\n{sello}' if fila.bitacora else sello
    db.session.commit()
    return fila


def _abierto_o_error(hallazgo_id: int, verbo: str) -> Hallazgo:
    """La fila, si está abierta. Levanta con el estado real si no.

    Sin `.first()` silencioso ni `or None`: un id inexistente y uno ya cerrado
    son dos problemas distintos y el mensaje lo dice, porque quien los va a leer
    está mirando una pantalla que ya se le desactualizó.
    """
    fila = Hallazgo.query.get(hallazgo_id)
    if fila is None:
        raise HallazgoInvalido(f'no existe el hallazgo {hallazgo_id}')
    if fila.estado != EstadoHallazgo.ABIERTO:
        raise HallazgoInvalido(
            f'no se puede {verbo}: el hallazgo {hallazgo_id} ya está '
            f'{fila.estado}. Alguien lo resolvió mientras esta pantalla estaba '
            f'abierta.'
        )
    return fila


def abiertos_de(vehiculo_id: int) -> List[Hallazgo]:
    """Los hallazgos vivos del vehículo, el bloqueante primero y el más viejo antes.

    El orden no es cosmético: `bloqueante` es hoy, y dentro de la misma
    severidad el que lleva más tiempo es el que peor está.
    """
    _PESO = {'bloqueante': 0, 'mayor': 1, 'menor': 2}
    filas = Hallazgo.query.filter_by(
        vehiculo_id=vehiculo_id, estado=EstadoHallazgo.ABIERTO).all()
    return sorted(filas, key=lambda h: (_PESO[h.criticidad], h.reportado_ts))


def _colgar_fotos(fotos, hallazgo_id, autor_id, ahora):
    """Cuelga las fotos del hallazgo. La política es del almacén, no de acá.

    `entidad_tipo='hallazgo'` ya estaba en el CHECK desde la tanda 1 —esperando
    esto— y las fotos de hallazgo son **evidencia de estado**, no foto-dato: lo
    que muestran es un golpe, no un número de seis dígitos que haya que leer.
    """
    from flota.adaptadores.almacen_fotos import colgar_fotos

    return colgar_fotos(fotos, 'hallazgo', hallazgo_id, autor_id, ahora)


__all__ = ['reportar', 'cerrar', 'descartar', 'aplazar', 'abiertos_de',
           'anclar_odometro', 'HallazgoInvalido', 'DIAS_POR_APLAZAMIENTO']
