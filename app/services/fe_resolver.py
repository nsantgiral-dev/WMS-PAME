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


def resolver_fe(tarea, gateway=None) -> tuple:
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


def resolver_fe_o_none(tarea, gateway=None) -> tuple:
    """Igual, pero `(None, None)` cuando no se puede — para las pantallas.

    Existe porque hay dos necesidades distintas y merecen respuestas distintas:
    disparar una nota crédito con el documento equivocado es un error fiscal y
    tiene que levantar; pintar un panel de liquidación sin los totales de Siesa
    es una molestia y no justifica un 500.

    Lo que NO hace ninguna de las dos es devolver el pedido.
    """
    try:
        return resolver_fe(tarea, gateway=gateway)
    except Exception as e:
        logger.warning('[FE] no se pudo resolver para la tarea %s: %s',
                       getattr(tarea, 'id', '?'), e)
        return None, None
