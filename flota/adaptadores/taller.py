"""
La puerta del taller — `flota_orden_trabajo` y `flota_intervencion`.

`flota/dominio/taller.py` tiene el vocabulario y el cálculo de la garantía.
Acá está lo único que escribe filas, y son cinco verbos:

```
abrir  →  ABIERTA ──── cerrar ────→ CERRADA     (y puede cerrar el hallazgo)
              │
              ├──────── anular ───→ ANULADA     (exige motivo escrito)
              │
              ├─ registrar_intervencion  → flota_intervencion (garantía calculada)
              └─ registrar_factura       → flota_gasto + vincula intervenciones
```

Y una lectura: `garantias_vigentes_de`, que es **lo que hace que la fase valga
la plata**. Al abrir una OT correctiva, el sistema busca intervenciones con
garantía vigente sobre el mismo sistema del mismo vehículo —por fecha **o** por
km— y lo muestra antes de mandar el camión. **No bloquea, propone.** El caso que
evita es el que se paga dos veces.

## La forma: una espina, extremidades especializadas

`flota_intervencion` es la **segunda extremidad de `flota_gasto`** (la primera es
`flota_tanqueo`). Si cada tabla llevara su propia columna de valor, el CPK sería
un `UNION` de N ramas y alguien va a olvidar la N+1 — que es `_BODEGA_CO_MAP`
otra vez, con plata.

Difiere de `flota_tanqueo` en una cosa y es deliberada: **`gasto_id` es nullable
y no es la PK**. Un tanqueo se paga en el surtidor, en el mismo instante; un
trabajo de taller se hace hoy y la factura llega el 30. Con `gasto_id` de PK,
«trabajos hechos sin factura recibida» sería un número imposible de contar,
porque esas filas no existirían todavía.

## Regla 3 — de dónde sale la lectura, y por qué la factura NO pide kilometraje

`flota_orden_trabajo.lectura_id` es NOT NULL y se ancla con
`hallazgos.anclar_odometro`, **no con una copia** (regla 0), con
`origen=OrigenLectura.OT`. Ese valor existía en el vocabulario desde la tanda 1
y no tenía nada detrás: esta tabla es lo que había detrás.

Y acá apareció algo que no estaba en el plan. `gastos.ORIGEN_DE_LECTURA` clasifica
`mantenimiento`, `repuesto` y `llanta` como categorías **de campo** —«alguien
está parado al lado del vehículo y ve el tablero»—, lo cual era cierto cuando el
gasto de taller era la única huella del taller. Con la OT existiendo, deja de
serlo: **la factura del taller se digita treinta días después, en una oficina,
sin el vehículo delante.**

Pedirle el kilometraje a quien digita esa factura produce una de dos cosas, las
dos malas: un número inventado, o el kilometraje del día del trabajo — que
`anclar_odometro` rechaza por retroceso de odómetro, porque el camión siguió
rodando. Así que `registrar_factura` **no pide km**: le pasa a `registrar_gasto`
la lectura de la OT, que es el kilometraje al que el trabajo se hizo y el ancla
correcta. Es la licencia que `anclar_odometro` ya declara —la lectura *no afirma
haberse tomado para este evento*— usada a propósito y no de contrabando.

## Lo que este adaptador NO hace

· **No decide quién paga.** Una garantía vigente es un argumento para no pagar
  dos veces, no una decisión tomada. No hay columna «cubierto por garantía» y no
  debe haberla: quién responde lo dice el taller cuando se le reclama.
· **No bloquea la OT sobre un sistema con garantía vigente.** Propone. Bloquear
  dejaría un camión roto en patio por un dato que puede estar mal levantado, y
  la operación desmonta el sistema en 48 horas — es la secuencia obligatoria del
  módulo: medir, corregir, imponer.
· **No nombra a nadie.** `abierta_por_usuario_id` y `registrada_por_usuario_id`
  son quién tecleó, un hecho (regla 2). Ninguna columna de mecánico.
· **No escribe una segunda vía de cierre del hallazgo.** `cerrar` pasa por
  `hallazgos.cerrar`. Un `UPDATE` propio sería la segunda vía, y la segunda es
  siempre la que se olvida de mover `cerrado_ts` — el campo contra el que
  `dias_hallazgo_abierto` se calcula.
"""
from datetime import date, datetime, timezone
from typing import List, Optional, Sequence

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA
from flota.adaptadores import gastos as adaptador_gastos
from flota.adaptadores import hallazgos as adaptador_hallazgos
from flota.adaptadores.hallazgos import anclar_odometro
from flota.adaptadores.modelos import (Gasto, Hallazgo, Intervencion,
                                       LecturaOdometro, OrdenTrabajo)
from flota.dominio import taller as dom
from flota.dominio.errores import ErrorFlota
from flota.dominio.hallazgo import EstadoHallazgo
from flota.dominio.valores import OrigenLectura


class OrdenInvalida(ErrorFlota):
    """La operación sobre el taller no se puede hacer. No se hace a medias.

    Es `ErrorFlota` y no `ValueError` para que la frontera lo traduzca a 400/409
    en vez de a un 500 — mismo criterio que `HallazgoInvalido` y `GastoInvalido`.
    Un dato malo no es una falla del sistema.
    """


#: Categorías de gasto con las que una factura de taller puede entrar.
#:
#: **Es un subconjunto declarado y se consulta con `in`, nunca con
#: `.get(cat, X)`** (regla 5). Son exactamente las tres que
#: `gastos.ORIGEN_DE_LECTURA` mapea a `OrigenLectura.OT`: si alguien pudiera
#: facturar una visita al taller como `soat`, el gasto entraría al CPK colgado de
#: una lectura que dice que nació de un trámite de oficina.
#:
#: `combustible` queda fuera aunque también sea de campo: un tanqueo tiene su
#: propia puerta y su propia extremidad, y por acá entraría sin galones.
CATEGORIAS_DE_TALLER = ('mantenimiento', 'repuesto', 'llanta')


def _dia_bogota(ts: datetime) -> date:
    """El día que una persona leería, a partir de un instante UTC-naive.

    Se pasa por Bogotá y no por UTC porque **el día que alguien lee es el suyo**
    (regla 5 del WMS). Un trabajo registrado a las 8 p.m. de Colombia ya es del
    día siguiente en UTC: con `utcnow().date()` la garantía de seis meses
    empezaría a contar un día tarde y vencería un día tarde, y el día en que se
    discuta va a ser exactamente el último.

    Misma conversión que `hallazgos._instante_limite`, y a propósito una sola
    línea: dos formas de contestar «qué día es hoy en Bogotá» son dos plazos que
    se separan.
    """
    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA).date()


def _texto_o_none(valor: Optional[str]) -> Optional[str]:
    """Texto limpio, o `None`. **Nunca cadena vacía.**

    Dos representaciones de la misma ausencia son dos consultas que dan números
    distintos. Los CHECK de la base también lo impiden; esto es para que el
    error no llegue hasta allá con forma de 500.
    """
    limpio = (valor or '').strip()
    return limpio or None


def _ultima_lectura(vehiculo_id: int) -> Optional[LecturaOdometro]:
    """La lectura más reciente del vehículo, desempatando por `id`.

    Mismo desempate que `anclar_odometro` y que `gastos._ultima_lectura`: con dos
    lecturas del mismo segundo —que las hay, `lecturas_ts_duplicado` las
    cuenta— el orden por `ts` solo es ambiguo, y una consulta ambigua devuelve
    una fila distinta cada vez.
    """
    filas = LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()
    return max(filas, key=lambda l: (l.ts, l.id), default=None)


def km_actual_de(vehiculo_id: int) -> Optional[int]:
    """El odómetro de hoy, o `None` si el vehículo no tiene ninguna lectura.

    `None` y **nunca 0**: un vehículo sin lecturas no recorrió cero kilómetros,
    no se sabe cuántos recorrió (regla 4). Con un 0 inventado, toda garantía por
    kilómetros saldría vigente sobre un parque sin odómetro — el default
    optimista que la regla 1 del módulo prohíbe, en el número que decide si se
    reclama o se paga.
    """
    ultima = _ultima_lectura(vehiculo_id)
    return None if ultima is None else ultima.valor_km


# ── Escritura ────────────────────────────────────────────────────────────────

def abrir(
    *,
    vehiculo_id: int,
    tipo: str,
    taller: str,
    descripcion: str,
    km: int,
    abierta_por_usuario_id: int,
    hallazgo_id: Optional[int] = None,
    ts: Optional[datetime] = None,
) -> OrdenTrabajo:
    """Manda el camión al taller. Devuelve la OT con su kilometraje anclado.

    QUÉ AFIRMA al devolver: que quedó escrito que este vehículo entró a este
    taller, a qué, con qué kilometraje y quién lo registró.

    QUÉ NO AFIRMA:

    · **Que el trabajo se haya hecho.** Eso lo dice una intervención, y una OT
      abierta sin intervenciones es un camión que está adentro ahora mismo.
    · **Que vaya a costar algo.** La OT no lleva valor: el camión entra hoy y la
      factura llega el 30 (ver el docstring del modelo).
    · **Que nadie sea responsable del daño** (regla 2). `abierta_por_usuario_id`
      es quién la registró.

    Juzga **antes** de escribir y no deja nada a medias: si algo revienta, la
    base queda como estaba.

    ## El `hallazgo_id` se valida, no se cree

    Si viene, tiene que ser del **mismo vehículo** y estar **abierto**. Un
    hallazgo de otro camión colgado de esta OT haría que cerrarla cerrara el daño
    equivocado —y `dias_hallazgo_abierto` se publicaría sobre una reparación que
    nunca ocurrió—. Uno ya cerrado no es un error del cuerpo: es que alguien lo
    resolvió mientras esta pantalla estaba abierta, y decirlo es más útil que un
    500.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    if tipo not in dom.TIPOS_OT:
        raise OrdenInvalida(
            f'tipo de orden desconocido: {tipo!r}. Conocidos: '
            f'{", ".join(dom.TIPOS_OT)}.')
    if not (taller or '').strip():
        raise OrdenInvalida(
            'sin taller no se puede contestar a quién reclamarle la garantía, '
            'que es media razón por la que esta tabla existe.')
    if not (descripcion or '').strip():
        raise OrdenInvalida(
            'una orden sin descripción es un camión que alguien mandó al taller '
            'y nadie sabe a qué: quien la lea mañana no va a poder revisar si lo '
            'que se hizo era lo que se pidió.')

    hallazgo = None
    if hallazgo_id is not None:
        hallazgo = Hallazgo.query.get(hallazgo_id)
        if hallazgo is None:
            raise OrdenInvalida(f'no existe el hallazgo {hallazgo_id}')
        if hallazgo.vehiculo_id != vehiculo_id:
            raise OrdenInvalida(
                f'el hallazgo {hallazgo_id} es de otro vehículo. Colgarlo de '
                f'esta orden haría que cerrarla cerrara el daño equivocado.')
        if hallazgo.estado != EstadoHallazgo.ABIERTO:
            raise OrdenInvalida(
                f'el hallazgo {hallazgo_id} ya está {hallazgo.estado}. Alguien '
                f'lo resolvió mientras esta pantalla estaba abierta.')

    try:
        lectura = anclar_odometro(vehiculo_id, km, abierta_por_usuario_id, ahora,
                                  origen=OrigenLectura.OT)
        fila = OrdenTrabajo(
            vehiculo_id=vehiculo_id,
            tipo=tipo,
            estado='abierta',
            taller=taller.strip(),
            descripcion=descripcion.strip(),
            lectura_id=lectura.id,
            hallazgo_id=hallazgo_id,
            abierta_ts=ahora,
            abierta_por_usuario_id=abierta_por_usuario_id,
        )
        db.session.add(fila)
        db.session.commit()
        return fila
    except Exception:
        # No se atrapa para seguir: se atrapa para dejar la base como estaba.
        # Una OT a medias —con lectura y sin fila— deja un kilometraje que dice
        # «acá hubo una orden de trabajo» sin ninguna orden al lado.
        db.session.rollback()
        raise


def registrar_intervencion(
    *,
    orden_trabajo_id: int,
    sistema: str,
    garantia_declarada: str,
    registrada_por_usuario_id: int,
    descripcion: Optional[str] = None,
    garantia_meses: Optional[int] = None,
    garantia_km: Optional[int] = None,
    ts: Optional[datetime] = None,
) -> Intervencion:
    """Registra un trabajo hecho, **con la garantía calculada al nacer**.

    QUÉ AFIRMA al devolver: que este trabajo quedó escrito sobre este sistema de
    este vehículo, con lo que la factura declara sobre su garantía y con el
    vencimiento ya calculado contra el día y el kilometraje del trabajo.

    QUÉ NO AFIRMA:

    · **Que el taller vaya a responder la garantía.** El vencimiento es lo que
      se pactó; quién lo honra es una conversación, y este número existe para
      poder tenerla.
    · **Que el trabajo haya costado algo todavía.** `gasto_id` nace `NULL` y eso
      es el estado normal hasta que llegue la factura — el número que el health
      cuenta como «trabajos sin factura recibida».
    · **Que el trabajo haya quedado bien.** Si vuelve a fallar, vuelve a nacer un
      hallazgo; lo que cambia es que ahora hay una garantía vigente que mostrar.

    ## `garantia_hasta_fecha` y `garantia_hasta_km` no son parámetros

    No están en la firma **a propósito y no por olvido**: es la regla 6 del
    módulo aplicada a la garantía. Si se pudieran teclear, «esta vence en marzo»
    sería una frase que alguien escribe y la garantía dejaría de significar algo.
    Salen de `dominio.taller`, y la frontera devuelve 400 —no un silencio— si el
    cuerpo los trae.

    ## Contra qué se calcula

    · **La fecha**, contra el día del registro **en Bogotá**. Es el día en que se
      declara el trabajo hecho, que es lo más cerca que el sistema está del día
      en que el vehículo volvió reparado.
    · **El kilometraje**, contra la lectura **de la orden de trabajo** — el
      odómetro al que se hizo el trabajo, no el de hoy. Contra el de hoy la
      garantía se llevaría de regalo todos los kilómetros que pasaron entre el
      taller y el momento en que alguien tecleó la factura.

    Los plazos solo se admiten con `garantia_declarada='si'`. Mandar seis meses
    junto a un `no` **levanta**, no se ignora: ignorarlo dejaría a quien lo mandó
    creyendo que la garantía quedó registrada, que es peor que un 400 — mismo
    criterio que `gastos._periodo`.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    # ── 1. Juzgar ANTES de escribir ──────────────────────────────────────
    orden = OrdenTrabajo.query.get(orden_trabajo_id)
    if orden is None:
        raise OrdenInvalida(f'no existe la orden de trabajo {orden_trabajo_id}')
    if orden.estado != 'abierta':
        raise OrdenInvalida(
            f'la orden {orden_trabajo_id} ya está {orden.estado}: no se le '
            f'pueden agregar trabajos. Una orden cerrada es una visita que '
            f'terminó, y agregarle un trabajo después movería lo que ya se '
            f'publicó sobre ella.')

    try:
        dom.es_sistema(sistema)
    except ValueError as e:
        raise OrdenInvalida(str(e))

    if garantia_declarada not in dom.GARANTIA_DECLARADA:
        raise OrdenInvalida(
            f'garantía declarada desconocida: {garantia_declarada!r}. '
            f'Conocidas: {", ".join(dom.GARANTIA_DECLARADA)}. `sin_dato` es una '
            f'respuesta legítima —una factura que no dice nada no es una factura '
            f'sin garantía— y hay que escribirla.')

    if dom.exige_descripcion(sistema) and not (descripcion or '').strip():
        raise OrdenInvalida(
            '`otro` exige escribir qué se hizo. Es la válvula del catálogo '
            'cerrado y no puede ser la salida cómoda: sin descripción, el '
            'desglose por sistema deja de decir nada — y la búsqueda de garantía '
            'vigente sobre `otro` no distingue un embrague de un espejo.')

    meses = _entero_positivo(garantia_meses, 'garantia_meses')
    kms = _entero_positivo(garantia_km, 'garantia_km')

    if garantia_declarada == 'si':
        if meses is None and kms is None:
            raise OrdenInvalida(
                'declarar que hay garantía sin decir hasta cuándo ni hasta '
                'cuántos kilómetros es una garantía que no se puede reclamar, y '
                'peor que ninguna: se ve verde y no cubre nada.')
    elif meses is not None or kms is not None:
        raise OrdenInvalida(
            f'`garantia_declarada={garantia_declarada}` no admite plazos. Si la '
            f'factura sí trae garantía, la palabra es `si`; si no se sabe, es '
            f'`sin_dato` — y en los dos casos el plazo no puede quedar guardado '
            f'bajo una palabra que dice lo contrario.')

    # ── 2. El cálculo, que NO es del que llama ───────────────────────────
    hasta_fecha = (dom.dia_fin_garantia(_dia_bogota(ahora), meses)
                   if meses is not None else None)
    hasta_km = (dom.km_fin_garantia(orden.lectura.valor_km, kms)
                if kms is not None else None)

    try:
        fila = Intervencion(
            orden_trabajo_id=orden.id,
            gasto_id=None,
            sistema=sistema,
            descripcion=_texto_o_none(descripcion),
            garantia_declarada=garantia_declarada,
            garantia_meses=meses,
            garantia_km=kms,
            garantia_hasta_fecha=hasta_fecha,
            garantia_hasta_km=hasta_km,
            registrada_ts=ahora,
            registrada_por_usuario_id=registrada_por_usuario_id,
        )
        db.session.add(fila)
        db.session.commit()
        return fila
    except Exception:
        db.session.rollback()
        raise


def _entero_positivo(valor, campo: str) -> Optional[int]:
    """`None`, o un entero mayor que cero. Levanta con el nombre del campo.

    Sin `try: int(...) except: None`. Un plazo ilegible que se guarda como
    ausente produce una intervención que dice «garantía sí» y no cubre nada — y
    eso no se ve raro en ninguna pantalla hasta el día que se reclama.
    """
    if valor is None or valor == '':
        return None
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise OrdenInvalida(f'{campo} no es un número entero: {valor!r}')
    if n <= 0:
        raise OrdenInvalida(
            f'{campo} = {n} no es una garantía corta: es una garantía que no se '
            f'declaró. Para eso está `garantia_declarada`.')
    return n


def cerrar(*, orden_trabajo_id: int, usuario_id: int,
           cerrar_hallazgo: bool = False, nota: Optional[str] = None,
           ts: Optional[datetime] = None) -> OrdenTrabajo:
    """El vehículo volvió del taller. Y **puede** cerrar el hallazgo que la abrió.

    QUÉ AFIRMA: que la visita terminó y que quedaron escritos los trabajos que
    produjo.

    QUÉ NO AFIRMA: que esté pagada. La factura llega después y se registra
    aparte; hasta entonces esos trabajos cuentan en `trabajos_sin_factura`, que
    es el número que dice si la operación está entregando las facturas del
    taller.

    ## No se puede cerrar una orden sin un solo trabajo registrado

    Una OT cerrada con cero intervenciones es una visita al taller sin ningún
    registro de qué se hizo: el camión estuvo tres días adentro y el expediente
    quedó vacío. Y no es un caso teórico — es el camino barato de la regla 11,
    porque cerrar es lo que saca el renglón rojo del tablero.

    Si de verdad no se hizo nada, la salida es `anular` **con motivo escrito**.
    Lo que la hace cara no es prohibirla: es que quede escrito quién dijo que no
    se hizo nada y por qué.

    ## El hallazgo se cierra por `hallazgos.cerrar`, no con un UPDATE

    `cerrar_hallazgo` es explícito y **nace en `False`**. Dos motivos, y el
    segundo es el que importa:

    1. Un camión puede volver del taller con el daño todavía abierto —faltó un
       repuesto, se arregló otra cosa—. Cerrarlo por defecto inventaría una
       reparación.
    2. `cerrado_ts` del hallazgo es **cuando el vehículo vuelve reparado**, y de
       ese campo sale `dias_hallazgo_abierto`. Un `UPDATE` escrito acá sería la
       segunda vía de cierre, y la segunda es siempre la que se olvida de mover
       un campo o de rechazar el doble cierre.

    Si el hallazgo ya no está abierto —alguien lo cerró desde su pantalla
    mientras el camión estaba en el taller—, `hallazgos.cerrar` levanta y la
    orden **no se cierra**: las dos cosas van en la misma transacción. Cerrar la
    orden y dejar el error del hallazgo sin efecto dejaría a quien lo pidió
    creyendo que las dos ocurrieron.
    """
    ahora = ts if ts is not None else datetime.utcnow()
    fila = _abierta_o_error(orden_trabajo_id, 'cerrar')

    if not fila.intervenciones:
        raise OrdenInvalida(
            f'la orden {orden_trabajo_id} no tiene ningún trabajo registrado: '
            f'cerrarla dejaría una visita al taller sin registro de qué se hizo. '
            f'Si de verdad no se hizo nada, la salida es anularla con motivo '
            f'escrito.')

    if cerrar_hallazgo and fila.hallazgo_id is None:
        raise OrdenInvalida(
            f'la orden {orden_trabajo_id} no nació de ningún daño reportado: no '
            f'hay hallazgo que cerrar.')

    try:
        fila.estado = 'cerrada'
        fila.cerrada_ts = ahora
        fila.cerrada_por_usuario_id = usuario_id
        if _texto_o_none(nota) is not None:
            fila.motivo_cierre = nota.strip()
        db.session.flush()
        if cerrar_hallazgo:
            # Por la puerta del hallazgo, no por una segunda vía. Hace su propio
            # `commit`; lo que ya está en la sesión entra con él, que es lo que
            # se quiere — las dos cosas o ninguna.
            adaptador_hallazgos.cerrar(
                hallazgo_id=fila.hallazgo_id, usuario_id=usuario_id, ts=ahora,
                nota=f'Cerrado al cerrar la orden de trabajo {fila.id}')
        else:
            db.session.commit()
        return fila
    except Exception:
        db.session.rollback()
        raise


def anular(*, orden_trabajo_id: int, usuario_id: int, motivo: str,
           ts: Optional[datetime] = None) -> OrdenTrabajo:
    """La visita no ocurrió, o no era. Exige motivo escrito, y el CHECK lo respalda.

    Es la única salida que no requiere registrar ningún trabajo, así que es la
    que usaría quien no quiere hacer el trabajo (regla 11). Lo que la hace cara
    no es prohibirla: es que **queda escrito quién dijo que no fue nada y por
    qué**, y que una OT anulada no cuenta en ningún indicador — no mejora ningún
    promedio.

    **No toca el hallazgo.** Si la visita se anuló, el daño sigue ahí: cerrarlo
    acá inventaría una reparación que nadie hizo. La OT anulada suelta el
    hallazgo y alguien puede abrir otra orden.
    """
    ahora = ts if ts is not None else datetime.utcnow()
    if not (motivo or '').strip():
        raise OrdenInvalida(
            'anular exige motivo escrito: sin él, una visita que de verdad no '
            'ocurrió y una que alguien prefirió borrar se ven exactamente igual.')
    fila = _abierta_o_error(orden_trabajo_id, 'anular')
    if fila.intervenciones:
        raise OrdenInvalida(
            f'la orden {orden_trabajo_id} ya tiene {len(fila.intervenciones)} '
            f'trabajo(s) registrado(s): eso ya ocurrió y anularlo lo borraría del '
            f'expediente. Una visita con trabajos hechos se cierra.')
    fila.estado = 'anulada'
    fila.cerrada_ts = ahora
    fila.cerrada_por_usuario_id = usuario_id
    fila.motivo_cierre = motivo.strip()
    db.session.commit()
    return fila


def registrar_factura(
    *,
    orden_trabajo_id: int,
    intervencion_ids: Sequence[int],
    categoria: str,
    fecha: date,
    valor,
    proveedor: str,
    origen_costo: str,
    registrado_por_usuario_id: int,
    documento_numero: Optional[str] = None,
    centro_op: Optional[str] = None,
    descripcion: Optional[str] = None,
    ts: Optional[datetime] = None,
) -> Gasto:
    """La factura del taller: escribe el gasto y lo cuelga de los trabajos.

    QUÉ AFIRMA al devolver: que quedó registrado un peso que salió por este
    vehículo, anclado al kilometraje del trabajo, y que esos trabajos ya no
    cuentan como «sin factura recibida».

    QUÉ NO AFIRMA: **que ese valor sea el costo de cada trabajo por separado.**
    Una factura del taller cubre las tres cosas que se hicieron en la visita, y
    las tres apuntan al mismo `flota_gasto`. Repartirlo entre ellas sería
    inventar un desglose que la factura no trae, y ese número inventado
    terminaría en un CPK. El CPK suma la espina, no las extremidades: apuntar N
    trabajos al mismo gasto **no duplica un peso**.

    ## Por qué no pide kilometraje

    Ver el encabezado del módulo. La factura se digita treinta días después, en
    una oficina, sin el vehículo delante: pedirle el odómetro a quien la teclea
    produce un número inventado o uno que `anclar_odometro` rechaza por
    retroceso. Se usa **la lectura de la OT**, que es el kilometraje al que el
    trabajo se hizo.

    ## Una sola puerta para el gasto

    El `INSERT INTO flota_gasto` no se escribe acá: se llama a
    `gastos.registrar_gasto(commit=False)`. Una segunda vía de nacimiento de un
    gasto sería la que se olvide del período, del vocabulario de `origen_costo` o
    del CHECK de `otro` — y es exactamente la forma del defecto que la regla 6
    resolvió para el hallazgo. Las dos escrituras van en **una transacción**: un
    gasto escrito con las intervenciones sin vincular sería plata que entra al
    CPK y trabajos que siguen figurando sin factura.
    """
    ahora = ts if ts is not None else datetime.utcnow()

    orden = OrdenTrabajo.query.get(orden_trabajo_id)
    if orden is None:
        raise OrdenInvalida(f'no existe la orden de trabajo {orden_trabajo_id}')
    if categoria not in CATEGORIAS_DE_TALLER:
        raise OrdenInvalida(
            f'una factura de taller no se registra como {categoria!r}. '
            f'Categorías de taller: {", ".join(CATEGORIAS_DE_TALLER)}. Las otras '
            f'existen y se registran desde la pantalla de gastos: por acá el '
            f'gasto quedaría colgado de una lectura que dice que nació de una '
            f'orden de trabajo.')

    ids = list(intervencion_ids or [])
    if not ids:
        raise OrdenInvalida(
            'hay que decir qué trabajos cubre la factura. Sin eso, el gasto '
            'quedaría escrito y los trabajos seguirían contando como «sin '
            'factura recibida» — la plata adentro y el hueco también.')

    filas = Intervencion.query.filter(Intervencion.id.in_(ids)).all()
    encontradas = {f.id for f in filas}
    faltan = [i for i in ids if i not in encontradas]
    if faltan:
        raise OrdenInvalida(f'no existen las intervenciones {sorted(faltan)}')
    ajenas = [f.id for f in filas if f.orden_trabajo_id != orden.id]
    if ajenas:
        raise OrdenInvalida(
            f'las intervenciones {sorted(ajenas)} no son de la orden '
            f'{orden_trabajo_id}. Una factura de esta visita no puede cubrir '
            f'trabajos de otra.')
    ya_facturadas = [f.id for f in filas if f.gasto_id is not None]
    if ya_facturadas:
        raise OrdenInvalida(
            f'las intervenciones {sorted(ya_facturadas)} ya tienen factura. '
            f'Volver a facturarlas sumaría el mismo trabajo dos veces al costo '
            f'por kilómetro, y un CPK inflado es un número plausible que nadie '
            f'puede desmentir después.')

    try:
        gasto = adaptador_gastos.registrar_gasto(
            vehiculo_id=orden.vehiculo_id,
            categoria=categoria,
            fecha=fecha,
            valor=valor,
            proveedor=proveedor,
            origen_costo=origen_costo,
            registrado_por_usuario_id=registrado_por_usuario_id,
            lectura_id=orden.lectura_id,
            documento_numero=documento_numero,
            centro_op=centro_op,
            descripcion=descripcion,
            ts=ahora,
            commit=False,
        )
        for f in filas:
            f.gasto_id = gasto.id
        db.session.commit()
        return gasto
    except Exception:
        # El gasto se abrió con `commit=False`: la transacción es de acá y el
        # rollback también. Un gasto escrito sin vincular sería plata en el CPK
        # con los trabajos todavía contando como sin factura.
        db.session.rollback()
        raise


def _abierta_o_error(orden_trabajo_id: int, verbo: str) -> OrdenTrabajo:
    """La fila, si está abierta. Levanta con el estado real si no.

    Sin `.first()` silencioso ni `or None`: un id inexistente y una ya cerrada
    son dos problemas distintos y el mensaje lo dice, porque quien lo va a leer
    está mirando una pantalla que ya se le desactualizó.
    """
    fila = OrdenTrabajo.query.get(orden_trabajo_id)
    if fila is None:
        raise OrdenInvalida(f'no existe la orden de trabajo {orden_trabajo_id}')
    if fila.estado != 'abierta':
        raise OrdenInvalida(
            f'no se puede {verbo}: la orden {orden_trabajo_id} ya está '
            f'{fila.estado}. Alguien la resolvió mientras esta pantalla estaba '
            f'abierta.')
    return fila


# ── Lectura ──────────────────────────────────────────────────────────────────

def ordenes_de(vehiculo_id: int) -> List[OrdenTrabajo]:
    """Las órdenes del vehículo, la abierta primero y la más reciente antes.

    El orden no es cosmético: una OT abierta es un camión que está en el taller
    ahora mismo, y es lo que hay que ver al abrir el expediente.
    """
    filas = OrdenTrabajo.query.filter_by(vehiculo_id=vehiculo_id).all()
    return sorted(filas, key=lambda o: (o.estado != 'abierta',
                                        -o.abierta_ts.timestamp(), -o.id))


def garantias_de(orden: OrdenTrabajo, dia: date,
                 km_actual: Optional[int]) -> List[dict]:
    """Las intervenciones de una orden, cada una con su veredicto de vigencia.

    El juicio lo emite el **dominio** (`taller.vigencia`), no un `if` escrito
    acá: una regla escrita dos veces diverge, y la copia que diverge es siempre
    la de la pantalla — la que la gente mira.
    """
    return [vigencia_de(i, dia, km_actual) for i in orden.intervenciones]


def vigencia_de(i: Intervencion, dia: date,
                km_actual: Optional[int]) -> dict:
    """Una intervención con su vigencia y **los dos insumos al lado**.

    `hasta_fecha`, `hasta_km` y `km_actual` viajan con el veredicto porque un
    `sin_dato` suelto no se puede interpretar: quien lo mire tiene que poder ver
    si la dimensión no se declaró o si el vehículo no tiene odómetro. Es el mismo
    criterio del CPK, que sale con `pesos` y `km`.
    """
    v = dom.vigencia(hasta_fecha=i.garantia_hasta_fecha,
                     hasta_km=i.garantia_hasta_km, dia=dia, km_actual=km_actual)
    return {
        'intervencion_id': i.id,
        'orden_trabajo_id': i.orden_trabajo_id,
        'sistema': i.sistema,
        'descripcion': i.descripcion,
        'garantia_declarada': i.garantia_declarada,
        'garantia_meses': i.garantia_meses,
        'garantia_km': i.garantia_km,
        'hasta_fecha': (i.garantia_hasta_fecha.isoformat()
                        if i.garantia_hasta_fecha else None),
        'hasta_km': i.garantia_hasta_km,
        'km_actual': km_actual,
        'gasto_id': i.gasto_id,
        'por_fecha': v['por_fecha'],
        'por_km': v['por_km'],
        'vigente': v['vigente'],
        'cubre': dom.cubre(v),
    }


def garantias_vigentes_de(vehiculo_id: int, dia: date,
                          sistema: Optional[str] = None) -> List[dict]:
    """**Lo que hace que la fase valga la plata.**

    Las intervenciones de este vehículo cuya garantía todavía cubre, opcionalmente
    filtradas por sistema. Se muestran **antes de mandar el camión**, y el caso
    que evitan es el que se paga dos veces.

    QUÉ AFIRMA: que sobre lo registrado, estas reparaciones todavía están dentro
    del plazo que la factura pactó.

    QUÉ NO AFIRMA: que el taller las vaya a responder. Es un argumento para
    llamar antes de mandar el camión, no una decisión tomada — **no bloquea,
    propone**. Bloquear dejaría un camión roto en patio por un dato que puede
    estar mal levantado, y la operación desmonta el sistema en 48 horas.

    Solo entran las que `cubre()` da `True`: `SIN_DATO` **no cuenta como
    vigente**. Contarlo sería el default optimista de la regla 1 —«no se pudo
    juzgar» leído como «sí cubre»— en el número que decide si se reclama. Las
    que no se pudieron juzgar siguen visibles en el expediente de su orden, que
    es donde se ven con su motivo al lado.
    """
    if sistema is not None:
        dom.es_sistema(sistema)
    km = km_actual_de(vehiculo_id)
    consulta = (db.session.query(Intervencion)
                .join(OrdenTrabajo,
                      Intervencion.orden_trabajo_id == OrdenTrabajo.id)
                .filter(OrdenTrabajo.vehiculo_id == vehiculo_id,
                        # Una orden anulada no ocurrió: su garantía tampoco.
                        OrdenTrabajo.estado != 'anulada',
                        Intervencion.garantia_declarada == 'si'))
    if sistema is not None:
        consulta = consulta.filter(Intervencion.sistema == sistema)

    salida = [vigencia_de(i, dia, km) for i in consulta.all()]
    # Ordenadas por la que vence más tarde primero: es la que más margen tiene
    # para reclamar y la que alguien va a querer ver arriba.
    vigentes = [s for s in salida if s['cubre']]
    return sorted(vigentes, key=lambda s: (s['sistema'],
                                           s['hasta_fecha'] or '',
                                           s['intervencion_id']))


def sin_factura_de(orden: OrdenTrabajo) -> List[Intervencion]:
    """Los trabajos de esta orden que todavía no tienen factura.

    `gasto_id IS NULL` **no es un error**: es el estado normal entre el día en
    que el camión vuelve y el día en que llega la factura. Se lista para que la
    pantalla pueda ofrecer registrarla, no para acusar a nadie.
    """
    return [i for i in orden.intervenciones if i.gasto_id is None]


__all__ = [
    'abrir', 'registrar_intervencion', 'cerrar', 'anular', 'registrar_factura',
    'ordenes_de', 'garantias_de', 'garantias_vigentes_de', 'sin_factura_de',
    'vigencia_de',
    'km_actual_de', 'CATEGORIAS_DE_TALLER', 'OrdenInvalida',
]
