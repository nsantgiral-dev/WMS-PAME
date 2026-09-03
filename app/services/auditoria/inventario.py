"""
Invariantes del libro de movimientos: `MovimientoInventario` contra el saldo vivo.

## Por qué existe este archivo

`MovimientoInventario` guarda `saldo_antes` y `saldo_despues` **en cada
escritura** desde que existe la tabla. Un grep por `app/`, `tests/` y `scripts/`
el 2026-08-20 devolvió: se escriben, se serializan en `to_dict()`, y **nadie los
lee en una comparación**. Ningún invariante de este paquete tocaba
`MovimientoInventario`.

Es un libro completo que nadie cuadra. Y es el único cuadre del WMS que **no
necesita una llamada externa**: ni Siesa ni conteo físico — los dos lados del
igual ya están en la base.

## Las dos propiedades

    1 · cuadre   el último `saldo_despues` de una clave == la `cantidad` viva
    2 · cadena   el `saldo_antes` de un movimiento == el `saldo_despues` del anterior

La clave es `(ubicacion_id, producto_id)`: es el par que el operario ve cuando
va al bin, y el que todos los escritores usan para localizar la fila.

## La regla del guard, aplicada acá antes de escribir una línea

> ¿Qué escribe el valor que estoy comprobando, y puede el camino roto
> escribirlo igual?

`saldo_despues` lo escribe **el escritor del movimiento**, leyendo
`reg.cantidad` justo después de mutarla. `cantidad` la escribe **cualquiera de
los 27 sitios** que la mutan —26 asignaciones más un `update()` masivo, contados
por AST sobre `app/`, `flota/` y `scripts/` el 2026-08-20—, y solo diez de ellos
escriben un movimiento con saldos. La asimetría es lo que
hace que el guard pueda fallar: un camino que muta `cantidad` **sin escribir
movimiento** no tiene forma de mover el último `saldo_despues` — no está
tocando esa tabla. No puede escribirlo igual porque no lo escribe.

Por eso NO se implementó la comprobación que primero parece obvia
—`saldo_despues == saldo_antes + Δ` dentro de la misma fila—: los tres valores
los escribe **el mismo bloque de código, en la misma expresión**, así que la vía
sana la satisface por construcción y la rota también. Es exactamente la forma de
los seis guards del 2026-08-15.

## Qué es un descuadre y qué no — la severidad

Nadie **opera** sobre `saldo_despues`: el picker lee `UbicacionProducto`. Un
descuadre no bloquea la operación de mañana; dice que el kardex no se puede
reconstruir y que el rastro miente. Eso ya lo pondría en `AVISA`.

Y hay un segundo motivo, que es el de `VTA-21`: **el modelo no sabe responder
cuál de las causas es**. Un descuadre puede venir de

    · una vía legítima que escribe `cantidad` sin movimiento con saldos
      (reposición, auditoría de picking, short-pick: cinco sitios escriben el
      movimiento con `saldo_antes`/`saldo_despues` en NULL);
    · una vía que no escribe movimiento en absoluto (el ajuste de conteo del
      DLQ, la puesta en cero masiva del sync de Siesa);
    · un movimiento de nivel almacén sin `ubicacion_id`, que mueve bins que no
      nombra;
    · o un defecto real de descuento cruzado entre bodegas.

No se puede mandar a alguien a investigar con esa ambigüedad y llamarlo
bloqueante — un falso positivo quema la herramienta entera. `INV-01` e `INV-02`
avisan; `INV-09` publica el denominador para que el aviso se pueda leer.

`INV-03` sí bloquea, y por la razón contraria: en **trece de los quince**
escritores de `MovimientoInventario` sus dos valores vienen de **fuentes
distintas** (el almacén del bin y el almacén que firma el movimiento). En los
otros dos no, y están nombrados en su docstring: sobre esos dos el guard no
puede fallar. Y ninguno de los quince lo alcanza cuando el movimiento no nombra
un bin.

## Qué se lee una sola vez, y por qué el caché muere con la transacción

`INV-01`, `INV-02` e `INV-09` piden **el mismo universo**. Sin memoizar,
`_movimientos()` lo materializaba tres veces y `_saldos_actuales()` dos: medido
contra PostgreSQL con 48.000 movimientos, la consulta cuesta ~170 ms y
convertir 20.000 filas en objetos ORM se lleva el resto. El cuello no era SQL —
era hacer el mismo trabajo tres veces.

El caché **no puede sobrevivir a la corrida**: un auditor que mira datos viejos
es peor que uno lento. `auditar()` no crea un `ctx` —el endpoint llama
`auditar(flujo)` y `ctx` llega en `None`—, así que no hay dónde colgarlo por
corrida. Se cuelga de la **transacción de la sesión**, que es un alcance más
estrecho: cualquier `commit`, `rollback` o `flush` lo tira, y la sesión muere
con el contexto de aplicación. Un caché que cambia lo que el auditor ve es peor
que el costo que ahorra.

## La ventana

`movimientos_inventario` crece con cada picking. Se miran los últimos
`_VENTANA_DIAS` días y **la ventana se declara en el reporte** (`INV-09`): un
auditor que mira una parte y devuelve «0 hallazgos» miente sobre lo que no miró.
Un conteo sin su base no es una medición.
"""
from datetime import datetime, timedelta

from sqlalchemy import event as _evento
from sqlalchemy.orm import Session as _SesionORM

from app.extensions import db
from app.services.auditoria.base import (
    _AUDITORIA_TRUNCADA, AVISA, BLOQUEA, OBSERVA, Hallazgo, invariante,
)

#: Un mes de operación. No es un umbral con teoría detrás —cualquier N lo
#: sería—; es el ciclo que ya usa el cierre contable, y por eso el que hace que
#: un descuadre se mire antes de que el mes se cierre encima. Lo que importa no
#: es el número sino que **esté declarado**: `INV-09` lo publica en cada corrida.
_VENTANA_DIAS = 30

#: Tope de filas leídas. Se toman las **más recientes** —`id.desc()` antes del
#: `limit`— porque el tope llenado con lo más viejo es como una auditoría deja
#: de ver las violaciones nuevas mientras informa «0 hallazgos».
_TOPE_MOVIMIENTOS = 20000
_TOPE_DESALINEADOS = 2000

#: Trozos para los `IN (...)`: PostgreSQL no se inmuta, SQLite sí tiene tope de
#: parámetros. Se trocea por ubicación y no por producto porque los bins son
#: pocos y cada uno guarda un puñado de SKUs.
_LOTE_IN = 400

#: Los invariantes que un movimiento **sin `ubicacion_id`** esquiva sin que
#: ninguno de ellos lo declare: `INV-02` porque no cae en ninguna cadena,
#: `INV-03` porque su `JOIN` con `Ubicacion` es interno. `INV-01` no está — mira
#: el bin víctima por el otro lado y a veces lo alcanza, así que afirmar que es
#: ciego sería exagerar en la dirección opuesta.
#:
#: Vive acá arriba y no dentro del `Hallazgo` porque es la misma lista que cita
#: la `consecuencia` de `INV-03`: si divergieran, el panel diría dos cosas.
_CIEGOS_SIN_UBICACION = ('INV-02', 'INV-03')

#: Dónde vive el universo ya leído, dentro de `Session.info`. Con nombre propio
#: y no un atributo suelto: `info` la comparten todos los que quieran usarla.
_CLAVE_CAJA = 'auditoria.inventario.universo'


@_evento.listens_for(_SesionORM, 'after_flush')
def _olvidar_el_universo(sesion, _contexto_de_flush):
    """El caché se tira en cada `flush`, no solo en cada `commit`.

    La identidad de la transacción ya cubre el commit y el rollback —los dos
    cierran la `SessionTransaction` y la siguiente es otro objeto—, pero **un
    `flush` cambia los datos sin cerrar nada**: pasa en los tests y en cualquier
    corrida que audite a mitad de una operación. Sin este oyente el auditor
    respondería con el universo de antes del cambio, que es exactamente el modo
    de fallo que hace inútil memoizar.

    Barato a propósito: un `pop` sobre un dict por flush. SQLAlchemy ni siquiera
    llama a `flush()` cuando no hay nada sucio, así que las consultas del propio
    auditor no lo disparan.
    """
    sesion.info.pop(_CLAVE_CAJA, None)


def _recordado(clave, calcular):
    """`calcular()` una vez por transacción de la sesión, no una vez por
    invariante.

    El alcance no es `ctx` porque **no hay `ctx`**: `auditar()` lo declara
    opcional y `app/routes/auditoria.py:37` llama `auditar(flujo)` — en
    producción llega `None`, y colgar el caché de `None` sería colgarlo del
    módulo, o sea de siempre. La transacción es el alcance correcto y además
    más estrecho que la petición: se cierra sola en cuanto alguien escribe.

    Guarda la transacción por **identidad** (`is`) y no por un contador: el
    objeto queda referenciado por el propio caché, así que su `id()` no se puede
    reciclar mientras la entrada exista.
    """
    sesion = db.session()
    caja = sesion.info.get(_CLAVE_CAJA)
    if caja is not None and caja[0] is sesion.get_transaction() \
            and clave in caja[1]:
        return caja[1][clave]

    valor = calcular()
    # Se relee DESPUÉS: la consulta de `calcular()` es la que abre la
    # transacción cuando la sesión venía limpia, y es la que puede haber
    # disparado el `after_flush` de arriba.
    transaccion = sesion.get_transaction()
    caja = sesion.info.get(_CLAVE_CAJA)
    if caja is None or caja[0] is not transaccion:
        caja = (transaccion, {})
        sesion.info[_CLAVE_CAJA] = caja
    caja[1][clave] = valor
    return valor


def _ventana(ctx=None):
    """`(dias, desde)`. `ctx` permite acotarla sin tocar el código —lo usan los
    tests para construir el caso a mano sin depender del reloj."""
    dias = _VENTANA_DIAS
    if isinstance(ctx, dict) and ctx.get('ventana_dias'):
        dias = int(ctx['ventana_dias'])
    return dias, datetime.utcnow() - timedelta(days=dias)


def _movimientos(ctx=None):
    """Los movimientos de la ventana, en **orden de escritura** (`id` ascendente).

    El orden es por `id` y no por `fecha`: `fecha` tiene `default=utcnow` y dos
    movimientos del mismo commit empatan. El encadenamiento pregunta «cuál se
    escribió después», y esa pregunta la contesta la secuencia, no el reloj.

    La piden `INV-01`, `INV-02` e `INV-09`. Se lee **una vez por transacción**
    (`_recordado`): materializar 20.000 objetos ORM tres veces era el 97 % del
    costo del flujo, y ninguno de los tres necesita una lectura propia.
    """
    from app.models.inventario import MovimientoInventario
    dias, desde = _ventana(ctx)

    def _leer():
        filas = (MovimientoInventario.query
                 .filter(MovimientoInventario.fecha >= desde)
                 .order_by(MovimientoInventario.id.desc())
                 .limit(_TOPE_MOVIMIENTOS)
                 .all())
        filas.reverse()
        return filas

    # La ventana entra en la clave: `ctx` puede acotarla y dos ventanas
    # distintas son dos universos distintos.
    filas = _recordado(('movimientos', dias), _leer)

    # La declaración de tope se rehace en CADA llamada y no dentro de `_leer`:
    # `auditar()` limpia `_AUDITORIA_TRUNCADA` al empezar cada corrida, así que
    # colgarla de la lectura haría que la segunda corrida sobre los mismos datos
    # informara «se miró todo» sin haberlo mirado. La declaración se deriva del
    # resultado, no del acto de consultar.
    if len(filas) >= _TOPE_MOVIMIENTOS:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:movimientos:{_TOPE_MOVIMIENTOS}')
    return filas


def _cadenas(ctx=None):
    """`(ubicacion_id, producto_id)` → sus movimientos en orden de escritura.

    Los movimientos **sin `ubicacion_id`** quedan fuera: no nombran ningún bin,
    así que no hay cadena a la que pertenezcan. No es un detalle menor — el
    fallback de despacho de traslados escribe uno de esos por producto, con los
    saldos agregados del almacén, después de descontar de N bins. `INV-09` los
    cuenta para que ese silencio se vea.
    """
    dias, _desde = _ventana(ctx)

    def _agrupar():
        out = {}
        for m in _movimientos(ctx):
            if m.ubicacion_id is None:
                continue
            out.setdefault((m.ubicacion_id, m.producto_id), []).append(m)
        return out

    # La piden `INV-01` e `INV-02`; agrupar 20.000 filas dos veces no cambia el
    # resultado, solo el tiempo.
    return _recordado(('cadenas', dias), _agrupar)


def _saldos_actuales(claves):
    """`(ubicacion_id, producto_id)` → `(cantidad_total, filas)`.

    **`filas` importa tanto como la cantidad.** `ubicaciones_productos` es única
    por `(ubicacion, producto, lote)`, así que una clave puede tener varias
    filas; y todos los escritores localizan la suya con
    `filter_by(ubicacion_id, producto_id).first()`, ignorando el lote. En ese
    caso el `saldo_despues` guardado es el de **una** fila y compararlo contra
    la suma acusaría a quien no hizo nada. Con más de una fila la clave se
    declara no comparable (`INV-09`) en vez de inventar cuál era.

    La piden `INV-01` e `INV-09` con el mismo juego de claves, así que se lee
    una vez por transacción — la clave del caché es el propio juego de claves,
    no la ventana: dos llamadas con claves distintas son dos preguntas
    distintas y ninguna puede contestar por la otra.
    """
    from sqlalchemy import func

    from app.models.inventario import UbicacionProducto
    pedidas = frozenset(claves)

    def _leer():
        ubis = sorted({u for u, _ in pedidas})
        out = {}
        for i in range(0, len(ubis), _LOTE_IN):
            q = (db.session.query(UbicacionProducto.ubicacion_id,
                                  UbicacionProducto.producto_id,
                                  func.sum(UbicacionProducto.cantidad),
                                  func.count(UbicacionProducto.id))
                 .filter(UbicacionProducto.ubicacion_id.in_(
                     ubis[i:i + _LOTE_IN]))
                 .group_by(UbicacionProducto.ubicacion_id,
                           UbicacionProducto.producto_id))
            for ub, pr, suma, n in q.all():
                if (ub, pr) in pedidas:
                    out[(ub, pr)] = (int(suma or 0), int(n))
        return out

    return _recordado(('saldos', pedidas), _leer)


def _etiquetas(claves):
    """Códigos legibles para las claves que ya son hallazgo.

    Se resuelven al final y solo para las pocas que se van a reportar: la
    referencia es lo que alguien escribe en un buscador, y `producto#412` no lo
    es. Cargarlas para el universo entero sería pagar el precio por filas que
    nadie va a mirar.
    """
    from app.models.producto import Producto
    from app.models.ubicacion import Ubicacion
    claves = list(claves)
    ubis = {u for u, _ in claves}
    prods = {p for _, p in claves}
    cod_u = {u.id: u.codigo for u in
             Ubicacion.query.filter(Ubicacion.id.in_(ubis)).all()} if ubis else {}
    cod_p = {p.id: (p.codigo_siesa or p.codigo) for p in
             Producto.query.filter(Producto.id.in_(prods)).all()} if prods else {}
    return lambda ub, pr: (f'{cod_u.get(ub, f"ubicacion#{ub}")} / '
                           f'{cod_p.get(pr, f"producto#{pr}")}')


# ── Frontera: movimiento ↔ saldo de la ubicación ─────────────────────────

@invariante(
    codigo='INV-01',
    flujo='inventario',
    frontera='movimiento ↔ saldo de la ubicación',
    consecuencia='El kardex de esa ubicación dejó de cuadrar con lo que hay en '
                 'el estante: o alguien movió stock sin registrarlo, o el stock '
                 'salió de una bodega que no era. El picker lo descubre cuando '
                 'no encuentra lo que el sistema promete.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_inventario.py::TestElDetectorNoEstaCiego::test_ve_una_cantidad_movida_sin_movimiento',
)
def el_ultimo_saldo_declarado_es_el_saldo_vivo(ctx=None):
    """El último `saldo_despues` de una clave debe ser su `cantidad` de hoy.

    Se toma el **último movimiento de la clave, tenga saldos o no**. Si no los
    tiene, la clave se declara ciega y no se acusa a nadie: cinco escritores
    guardan el movimiento con los saldos en NULL, y tratarlos como cero sería
    fabricar un descuadre por cada uno (Regla 0). El siguiente movimiento con
    saldos vuelve a anclar la cadena solo, porque lee `cantidad` fresca.
    """
    cadenas = _cadenas(ctx)
    if not cadenas:
        return []
    dias, _desde = _ventana(ctx)
    saldos = _saldos_actuales(cadenas.keys())
    rotas = []
    for clave, movs in cadenas.items():
        ultimo = movs[-1]
        if ultimo.saldo_despues is None:
            continue
        suma, filas = saldos.get(clave, (0, 0))
        if filas > 1:
            continue
        if suma == ultimo.saldo_despues:
            continue
        rotas.append((clave, ultimo, suma, filas))

    ref = _etiquetas([c for c, _, _, _ in rotas])
    return [
        Hallazgo(
            referencia=ref(*clave),
            detalle=f'el libro declara {ultimo.saldo_despues} y la ubicación '
                    f'tiene {suma} ({suma - ultimo.saldo_despues:+d}) — último '
                    f'movimiento #{ultimo.id} {ultimo.tipo}'
                    + (' · la ubicación ya no tiene fila para ese producto'
                       if filas == 0 else ''),
            datos={'movimiento_id': ultimo.id, 'tipo': ultimo.tipo,
                   'saldo_despues': ultimo.saldo_despues,
                   'cantidad_actual': suma,
                   'diferencia': suma - ultimo.saldo_despues,
                   'motivo': (ultimo.motivo or '')[:120],
                   'ventana_dias': dias},
        )
        for clave, ultimo, suma, filas in rotas
    ]


@invariante(
    codigo='INV-02',
    flujo='inventario',
    frontera='movimiento ↔ movimiento anterior',
    consecuencia='Entre dos movimientos de la misma ubicación el stock cambió '
                 'sin que nadie lo escribiera: el kardex no se puede '
                 'reconstruir y la diferencia no tiene dueño ni fecha.',
    severidad=AVISA,
    detector_ciego='tests/flujo/test_flujo_inventario.py::TestElDetectorNoEstaCiego::test_ve_la_cadena_rota_entre_dos_movimientos',
)
def la_cadena_de_saldos_no_tiene_saltos(ctx=None):
    """`saldo_antes[n] == saldo_despues[n-1]` sobre movimientos **adyacentes**.

    Adyacentes y no «el anterior con saldos»: si en medio hay un movimiento que
    dejó los saldos en NULL, ese movimiento **sí movió stock** y el salto que se
    vería sería suyo, no de un escritor fantasma. Comparar por encima del hueco
    convertiría los cinco escritores sin saldos en cinco hallazgos falsos por
    cada bin que tocan.

    Un hallazgo por clave y no por eslabón: una tarde de descuadre en un bin
    rompe todos los eslabones que siguen, y una lista larga se ignora.
    """
    dias, _desde = _ventana(ctx)
    rotas = []
    for clave, movs in _cadenas(ctx).items():
        saltos = []
        for a, b in zip(movs, movs[1:]):
            if a.saldo_despues is None or b.saldo_antes is None:
                continue
            if b.saldo_antes != a.saldo_despues:
                saltos.append((a, b))
        if saltos:
            rotas.append((clave, saltos))

    ref = _etiquetas([c for c, _ in rotas])
    return [
        Hallazgo(
            referencia=ref(*clave),
            detalle=f'{len(saltos)} salto(s) en la cadena · #{a.id} {a.tipo} '
                    f'cerró en {a.saldo_despues} y #{b.id} {b.tipo} abrió en '
                    f'{b.saldo_antes} ({b.saldo_antes - a.saldo_despues:+d})',
            datos={'movimientos': [x.id for _p in saltos for x in _p][:20],
                   'saltos': len(saltos),
                   'primer_salto': [a.id, b.id],
                   'diferencia': b.saldo_antes - a.saldo_despues,
                   'ventana_dias': dias},
        )
        for clave, saltos in rotas
        for a, b in [saltos[0]]
    ]


# ── Frontera: movimiento ↔ almacén que lo firma ──────────────────────────

@invariante(
    codigo='INV-03',
    flujo='inventario',
    frontera='movimiento ↔ almacén que lo firma',
    consecuencia='Un movimiento que nombra un bin de una bodega quedó firmado '
                 'contra otra: el kardex de las dos bodegas queda mal a la vez '
                 'y ninguna de las dos sabe cuál es su saldo. '
                 'Alcance: SOLO los movimientos que nombran un bin. El '
                 'descuento agregado por almacén se escribe sin ubicación y '
                 'este invariante no lo ve; INV-09 los cuenta en '
                 '«movimientos sin ubicación».',
    severidad=BLOQUEA,
    detector_ciego='tests/flujo/test_flujo_inventario.py::TestElDetectorNoEstaCiego::test_ve_un_movimiento_firmado_contra_otro_almacen',
)
def el_movimiento_se_firma_contra_el_almacen_de_su_bin(ctx=None):
    """`MovimientoInventario.almacen_id` == `Ubicacion.almacen_id` de su bin.

    Es el único bloqueante del flujo, y bloquea porque en **trece de los quince**
    escritores de `MovimientoInventario` los dos valores vienen de **fuentes
    distintas**: el del bin lo pone la consulta que elige de dónde descontar, el
    del movimiento lo pone la operación que lo firma. Si la elección de bin
    dejara de acotarse al almacén —que es exactamente el defecto que costó el
    arreglo de `traslado_service`— los dos valores se separarían y esto lo ve.

    ## Los dos sitios donde el guard no puede fallar

    Contados por AST sobre `app/`, `flota/` y `scripts/` el 2026-08-20: de los
    quince escritores, **dos** ponen `almacen_id=ubicacion.almacen_id`, leído
    del propio bin:

        app/routes/inventario.py:101        (movimiento manual de inventario)
        app/services/layout_service.py:871  (ASIGNACION_LAYOUT)

    Ahí no hay segunda fuente: el bin equivocado firma su propio movimiento, y
    la vía rota escribe los dos valores iguales **por construcción**. Sobre esos
    dos, «0 hallazgos» de este invariante no significa nada — es la pregunta que
    hunde a un guard, y la respuesta es que acá la hunde en dos de quince.

    No se cuentan como ciegos los que firman con un `almacen_id` propio y buscan
    el bin con `filter_by(almacen_id=...)` —`picking_service`,
    `layout_service:757`, `inventario_siesa_service:771`, `devolucion_service`—:
    ahí el filtro es justo lo que el defecto rompería, y al romperse los dos
    valores se separan. Que hoy coincidan es la vía sana funcionando, no el
    guard tapado.

    ## Y lo que no nombra un bin no entra

    El `JOIN` con `Ubicacion` es interno: un movimiento con `ubicacion_id` NULL
    no aparece en ninguna fila del resultado. Es el descuento agregado por
    almacén del fallback de despacho de traslados, que descuenta de N bins y
    escribe **un** movimiento con los saldos del almacén y sin ubicación. Sobre
    esa fila esta frontera no se puede evaluar: el dato que habría que cruzar
    —qué bin— no está escrito en ninguna parte.

    Ampliar la cobertura a ese caso se evaluó y **se descartó**: lo único
    comparable sería el saldo agregado del almacén, y cualquier picking normal
    entre dos movimientos agregados lo mueve legítimamente. El invariante
    dispararía sobre operación sana, y un falso positivo quema la herramienta
    entera. `INV-09` publica el conteo para que el hueco se pueda leer.

    Se agrupa por par de bodegas y no por fila: cincuenta movimientos del mismo
    descuento cruzado son **una** causa, y una lista de cincuenta no se tría.
    """
    from app.models.inventario import MovimientoInventario
    from app.models.ubicacion import Ubicacion
    _dias, desde = _ventana(ctx)
    filas = (db.session.query(MovimientoInventario.id,
                              MovimientoInventario.tipo,
                              MovimientoInventario.almacen_id,
                              Ubicacion.almacen_id,
                              MovimientoInventario.numero_documento)
             .join(Ubicacion, Ubicacion.id == MovimientoInventario.ubicacion_id)
             .filter(MovimientoInventario.fecha >= desde)
             .filter(MovimientoInventario.almacen_id != Ubicacion.almacen_id)
             .order_by(MovimientoInventario.id.desc())
             .limit(_TOPE_DESALINEADOS)
             .all())
    if len(filas) >= _TOPE_DESALINEADOS:
        _AUDITORIA_TRUNCADA.add(f'{__name__}:desalineados:{_TOPE_DESALINEADOS}')

    grupos = {}
    for mid, tipo, alm_mov, alm_ub, doc in filas:
        grupos.setdefault((alm_mov, alm_ub), []).append((mid, tipo, doc))

    return [
        Hallazgo(
            referencia=f'{len(v)} movimiento(s) · ej. movimiento#{v[0][0]}'
                       + (f' / {v[0][2]}' if v[0][2] else ''),
            detalle=f'firmados contra el almacén {alm_mov} sobre bins del '
                    f'almacén {alm_ub}',
            datos={'almacen_firmado': alm_mov, 'almacen_del_bin': alm_ub,
                   'movimientos': [x[0] for x in v[:15]],
                   'tipos': sorted({x[1] for x in v})},
        )
        for (alm_mov, alm_ub), v in sorted(grupos.items(), key=lambda kv: -len(kv[1]))
    ]


# ── Cobertura de la propia auditoría ─────────────────────────────────────

@invariante(
    codigo='INV-09',
    flujo='inventario',
    frontera='cobertura de la propia auditoría',
    consecuencia='Sin esta línea, «0 hallazgos» de INV-01/INV-02/INV-03 no se '
                 'distingue de «no se miró»: ni cuántos días se leyeron, ni '
                 'cuántas claves quedaron ciegas porque su último movimiento no '
                 'declaró saldos, ni cuántos movimientos se escribieron sin '
                 'nombrar un bin — esos últimos quedan fuera del alcance de '
                 'INV-02 e INV-03, que solo saben mirar movimientos con '
                 'ubicación.',
    severidad=OBSERVA,
    detector_ciego='tests/flujo/test_flujo_inventario.py::TestLaVentanaSeDeclara::test_declara_las_claves_ciegas',
)
def la_ventana_mirada_se_declara(ctx=None):
    """El denominador de INV-01, INV-02 e INV-03, en una sola línea.

    No es un defecto —por eso `OBSERVA`, como `TRA-30`—: es lo que hace legible
    el silencio. Un auditor que mira treinta días de una tabla de años y
    devuelve «0 hallazgos» está afirmando algo sobre lo que no leyó.

    ## Los movimientos sin ubicación son el hueco de INV-02 e INV-03

    El conteo ya estaba; lo que faltaba era decir **qué** deja de mirarse. Un
    movimiento sin `ubicacion_id` no cae en ninguna cadena (`INV-02`) y no
    sobrevive al `JOIN` con `Ubicacion` (`INV-03`): los dos devuelven cero sobre
    él sin haberlo evaluado. Publicar el número sin nombrar a los ciegos deja al
    que lee el panel concluyendo que ese riesgo está vigilado — que es la forma
    más cara de fallo que documenta este repo, porque nadie va a ir a mirar.

    `sin_ubicacion_fuera_de` es esa lista, y va en `datos` **y** en el detalle:
    quien mira el panel no abre `datos`.

    Sobre una base sin movimientos no dice nada: no hay universo que declarar, y
    una instalación recién desplegada no necesita una línea que interpretar.
    """
    from app.models.inventario import MovimientoInventario
    dias, desde = _ventana(ctx)
    movs = _movimientos(ctx)
    # Un movimiento sin `fecha` no cae en ninguna ventana — se cuenta aparte en
    # vez de dejarlo invisible (Regla 0: el dato ausente se declara).
    sin_fecha = (MovimientoInventario.query
                 .filter(MovimientoInventario.fecha.is_(None))
                 .count())
    if not movs and not sin_fecha:
        return []

    cadenas = {}
    sin_ubicacion = 0
    for m in movs:
        if m.ubicacion_id is None:
            sin_ubicacion += 1
        else:
            cadenas.setdefault((m.ubicacion_id, m.producto_id), []).append(m)

    saldos = _saldos_actuales(cadenas.keys()) if cadenas else {}
    ciegas = sum(1 for ms in cadenas.values() if ms[-1].saldo_despues is None)
    multilote = sum(1 for c in cadenas if saldos.get(c, (0, 0))[1] > 1)
    comparadas = len(cadenas) - ciegas - multilote

    return [Hallazgo(
        referencia=f'ventana {dias} día(s) · {len(cadenas)} clave(s)',
        detalle=(f'{len(movs)} movimiento(s) desde {desde.date().isoformat()} · '
                 f'{comparadas} clave(s) comparada(s), {ciegas} ciega(s) '
                 f'(último movimiento sin saldos), {multilote} con varios lotes '
                 f'(no comparable), {sin_ubicacion} movimiento(s) sin ubicación '
                 f'(mueven bins que no nombran — fuera del alcance de '
                 f'{" y ".join(_CIEGOS_SIN_UBICACION)})'
                 + (f', {sin_fecha} sin fecha (fuera de toda ventana)'
                    if sin_fecha else '')),
        datos={'ventana_dias': dias, 'desde': desde.isoformat(),
               'movimientos_leidos': len(movs), 'tope': _TOPE_MOVIMIENTOS,
               'claves': len(cadenas), 'claves_comparadas': comparadas,
               'claves_ciegas': ciegas, 'claves_multilote': multilote,
               'movimientos_sin_ubicacion': sin_ubicacion,
               'sin_ubicacion_fuera_de': list(_CIEGOS_SIN_UBICACION),
               'movimientos_sin_fecha': sin_fecha},
    )]
