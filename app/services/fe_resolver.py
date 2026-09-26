"""
Cuál es la factura electrónica de una tarea de packing. **Una función.**

El pedido y la factura son documentos distintos con numeraciones distintas, y
en las consultas de Siesa viven en prefijos distintos:

    f350_*  → el documento consultado (la FACTURA)
    f430_*  → el pedido que la originó

`get_rowids_factura` filtra por `f350_id_tipo_docto` y `f350_consec_docto`, o
sea que **necesita la factura**. Todo el flujo de liquidación le pasaba
`tarea.tipo_docto_pedido_siesa` / `consec_docto_pedido_siesa` — el pedido — en
cuatro sitios, con la variable llamada `tipo_docto_fe`.

El resultado, medido en producción el 2026-08-11 (job 440, ruta 15):

    GET API_v2_Ventas_Facturas_DesdePedido: 400 Client Error
    parametros=f350_id_tipo_docto = ''PD'' AND f350_consec_docto = 1308

`PD` es el tipo del pedido. La factura es `FEW-xxxx`. Ninguna nota crédito de
liquidación llegó nunca a Siesa.

## Por qué es un nombre que miente y no un descuido

La variable se llamaba `tipo_docto_fe` —FE, factura electrónica— y contenía el
pedido. Quien lea `get_rowids_factura(tipo_docto_fe, consec_fe)` no tiene motivo
para dudar. El error no está donde se usa: está donde se asigna.

## Y la lección ya estaba escrita

`devolucion_cliente_service` resuelve la FE **bien** desde hace tiempo, y su
código lleva el comentario:

    # Resolver tipo/consec REALES de la FE — nunca los _pedido_siesa

Liquidación hace exactamente lo que ese comentario prohíbe. Un comentario
protege al archivo donde está escrito y a ninguno más — por eso esto es una
función y no una nota.
"""
import logging

logger = logging.getLogger(__name__)


class FENoEncontrada(Exception):
    """No se pudo resolver la factura. **No se devuelve el pedido como
    consuelo**: seguir con el documento equivocado produce un 400 confuso o,
    peor, encuentra otro documento que sí existe con esa numeración."""


def resolver_fe(tarea, gateway=None, anotar=True) -> tuple:
    """`(tipo_docto_fe, consec_fe)` reales de la FE de esa tarea de packing.

    `TareaPacking` NO guarda la factura: guarda el pedido (`*_pedido_siesa`) y
    la remisión (`rm_tipo`/`rm_consec`). La FE la asigna Siesa al facturar
    desde la remisión, y hay que ir a buscarla.

    Levanta `FENoEncontrada` si no se puede resolver. Nunca cae al pedido.

    `gateway` se inyecta para que el llamador pase SU referencia a Connekta.
    Sin esto, esta función importa el singleton por su cuenta y los tests que
    parchean el `connekta` de su propio módulo —que son casi todos— dejan de
    controlar la resolución: el mock queda de adorno y el test verifica otra
    cosa. Un doble que no se usa es peor que no tenerlo.

    `anotar=False`: resuelve sin guardar (la lista de paradas del conductor es
    una lectura y no escribe — 2026-09-25). La anotación la hace quien escribe.
    """
    connekta = gateway
    if connekta is None:
        from app.services.connekta_gateway import connekta

    if tarea is None:
        raise FENoEncontrada('no hay tarea de packing')

    # Si ya se resolvió una vez, no se vuelve a preguntar. `TareaPacking` no
    # guardaba la factura y esa ausencia fue la causa raíz del job 440 — cada
    # consumidor tenía que ir a Siesa, y el que se equivocó pasó el pedido.
    if getattr(tarea, 'fe_tipo', None) and getattr(tarea, 'fe_consec', None):
        return tarea.fe_tipo, tarea.fe_consec

    filas = connekta.get_detalle_factura(
        tipo_docto_rm=tarea.rm_tipo or '',
        consec_rm=tarea.rm_consec or 0,
        consec_pedido=tarea.consec_docto_pedido_siesa,
    )
    # El doble de simulación va DESPUÉS de intentar de verdad, no antes: un
    # corto-circuito al principio atropella los mocks de los tests, que es
    # justo donde se verifica que la FE se resuelve bien.
    #
    # `SIMFE` y no el tipo del pedido: el doble tiene que ser distinguible del
    # dato real (regla 8 de flota). Un `SIMFE-1308` que aparezca en un payload
    # que salió de verdad se reconoce al instante; un `PD-1308` plausible, no.
    # `is True` y no truthy: en un `MagicMock` cualquier atributo existe y es
    # truthy, así que un `getattr(...)` a secas dispara el doble dentro de los
    # tests que mockean el gateway — y el test verifica el doble en vez de lo
    # que quería verificar.
    if not filas and getattr(connekta, 'modo_simulacion', False) is True:
        return 'SIMFE', str(tarea.consec_docto_pedido_siesa or '0')

    if not filas:
        raise FENoEncontrada(
            f'no se localizó la FE del pedido '
            f'{tarea.tipo_docto_pedido_siesa}-{tarea.consec_docto_pedido_siesa} '
            f'(RM {tarea.rm_tipo}-{tarea.rm_consec}) — ¿ya se generó la factura?'
        )

    cab = filas[0]
    tipo = str(cab.get('f350_id_tipo_docto') or '').strip()
    consec = str(cab.get('f350_consec_docto') or '').strip()
    if not tipo or not consec:
        raise FENoEncontrada(
            f'la FE del pedido {tarea.consec_docto_pedido_siesa} no trae '
            f'f350_id_tipo_docto/f350_consec_docto'
        )

    logger.info('[FE] pedido %s-%s → factura %s-%s',
                tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa,
                tipo, consec)
    if anotar:
        _anotar(tarea, tipo, consec)
    return tipo, consec


def _anotar(tarea, tipo: str, consec: str):
    """Guarda la FE resuelta. **Anotar no puede romper lo anotado.**

    Si el commit falla, la resolución ya es correcta y el llamador la recibe
    igual: lo único que se pierde es el ahorro de la próxima consulta. Levantar
    acá convertiría un problema de caché en uno de facturación.

    El doble de simulación NO se persiste: un `SIMFE` guardado sobreviviría al
    modo simulación y después se leería como una factura real (regla 8 de
    flota — un doble se declara y no se confunde con el dato).

    Hoy ese guard **no se alcanza desde `resolver_fe`**: la rama de simulación
    retorna antes de llegar acá. Se conserva y se prueba directamente porque
    protege el día que alguien mueva esa rama — y porque un guard sin ejercicio
    es indistinguible de uno que no está. Lo descubrió una mutación que no
    falló: borrarlo no rompía nada.
    """
    if tipo == 'SIMFE':
        return
    try:
        from app.extensions import db
        if getattr(tarea, 'fe_tipo', None) == tipo and \
                getattr(tarea, 'fe_consec', None) == consec:
            return
        tarea.fe_tipo = tipo
        tarea.fe_consec = str(consec)
        db.session.commit()
    except Exception as e:
        try:
            from app.extensions import db as _db
            _db.session.rollback()
        except Exception:
            pass
        logger.warning('[FE] no se pudo anotar %s-%s en la tarea %s: %s',
                       tipo, consec, getattr(tarea, 'id', '?'), e)


def resolver_fe_o_none(tarea, gateway=None, anotar=True) -> tuple:
    """Igual, pero `(None, None)` cuando no se puede — para las pantallas.

    Existe porque hay dos necesidades distintas y merecen respuestas distintas:
    disparar una nota crédito con el documento equivocado es un error fiscal y
    tiene que levantar; pintar un panel de liquidación sin los totales de Siesa
    es una molestia y no justifica un 500.

    Lo que NO hace ninguna de las dos es devolver el pedido.
    """
    try:
        return resolver_fe(tarea, gateway=gateway, anotar=anotar)
    except Exception as e:
        logger.warning('[FE] no se pudo resolver para la tarea %s: %s',
                       getattr(tarea, 'id', '?'), e)
        return None, None


# ─────────────────────────────────────────────────────────────────────────
# El consecutivo de la FE que el 142943 no devolvió (2026-09-26)
# ─────────────────────────────────────────────────────────────────────────
#
# En el camino normal del cierre la tarea queda con `fe_confirmada_at` (la FE
# existe) y SIN `fe_consec`: el 142943 no devuelve el consecutivo, y solo el
# camino idempotente lo anota. Quien necesita el número (la cartera para no
# contar dos veces, la nota crédito, la foto de Siesa) lo resolvía cada uno
# por su cuenta o se quedaba sin él. Este barrido lo anota.

#: Tareas por corrida. Rotan por `reconciliacion_intento_at` (la misma columna
#: del barrido de reconciliación: los dos conjuntos no se cruzan — aquél mira
#: `siesa_triggered = False`, éste FE ya confirmada).
LOTE_ANOTAR_FE = 20

#: Solo las FE de los últimos días: el histórico no se recorre en cada corrida.
DIAS_ANOTAR_FE = 30


def fe_de_pedido(tarea, gateway=None):
    """`(tipo, consec)` de la FE del pedido de la tarea, preguntando **por el
    pedido, en su CO**, o `None` si Siesa respondió y no hay exactamente una.

    Se descarta la fila cuyo tipo de pedido (`f430_id_tipo_docto`) o remisión
    (`f460_consec_docto`) contradiga los de la tarea, si la fila los trae. Con
    cero o con varias FE candidatas no se anota nada: anotar la factura de otro
    pedido es peor que no anotar ninguna.

    Levanta si no se pudo preguntar (sin clave de pedido, red, rechazo)."""
    connekta = gateway
    if connekta is None:
        from app.services.connekta_gateway import connekta
    partes = str(getattr(tarea, 'pedido_clave', None) or '').split('-')
    if len(partes) != 3 or not partes[2].isdigit():
        raise FENoEncontrada(f'la tarea {getattr(tarea, "id", "?")} no tiene clave de pedido')
    co, tipo, consec = partes[0], partes[1].upper(), int(partes[2])
    candidatos = set()
    for r in connekta.get_facturas_de_pedido(co, consec) or []:
        tp = str(r.get('f430_id_tipo_docto') or '').strip().upper()
        if tp and tp != tipo:
            continue
        rm = str(r.get('f460_consec_docto') or '').strip()
        if rm and getattr(tarea, 'rm_consec', None) and rm != str(tarea.rm_consec):
            continue
        ft = str(r.get('f350_id_tipo_docto') or '').strip()
        fc = str(r.get('f350_consec_docto') or '').strip()
        if ft and fc:
            candidatos.add((ft, fc))
    return candidatos.pop() if len(candidatos) == 1 else None


def anotar_fe_emitidas(gateway=None, lote: int = LOTE_ANOTAR_FE, ahora=None) -> dict:
    """Anota el consecutivo de las FE emitidas sin él (`fe_confirmada_at` sin
    `fe_consec`), de los últimos `DIAS_ANOTAR_FE` días, un lote por corrida.
    Solo GET. Corre con el barrido de cartera (`cartera_service.init_scheduler`,
    misma ventana y mismo latido)."""
    from datetime import datetime, timedelta

    from app.extensions import db
    from app.models.packing import EstadoPacking, TareaPacking as T
    from app.utils.lock import LOCK_ANOTAR_FE, advisory_lock
    ahora = ahora or datetime.utcnow()
    res = {'revisadas': 0, 'anotadas': 0, 'sin_factura_unica': 0, 'no_se': 0}
    with advisory_lock(LOCK_ANOTAR_FE, 'anotar_fe') as tomado:
        if not tomado:
            return {'omitido': 'otro worker ya anota'}
        tareas = (T.query
                  .filter(T.fe_consec.is_(None), T.fe_confirmada_at.isnot(None),
                          T.fe_confirmada_at >= ahora - timedelta(days=DIAS_ANOTAR_FE),
                          T.pedido_clave.isnot(None),
                          T.estado != EstadoPacking.CANCELADO,
                          db.or_(T.tipo_documento.is_(None), T.tipo_documento != 'TRASLADO'))
                  .order_by(T.reconciliacion_intento_at.isnot(None),
                            T.reconciliacion_intento_at, T.fe_confirmada_at.desc())
                  .limit(lote).all())
        for t in tareas:
            res['revisadas'] += 1
            t.reconciliacion_intento_at = ahora
            try:
                fe = fe_de_pedido(t, gateway=gateway)
            except Exception as e:  # noqa: BLE001 — «no pude preguntar»: la próxima
                res['no_se'] += 1
                logger.warning('[FE] no se pudo anotar la FE de la tarea %s: %s', t.id, e)
                continue
            if fe is None:
                res['sin_factura_unica'] += 1
                continue
            _anotar(t, *fe)
            res['anotadas'] += int(bool(t.fe_consec))
        db.session.commit()
    return res
