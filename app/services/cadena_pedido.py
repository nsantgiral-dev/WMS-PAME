"""
La clave del pedido que une picking, packing, historia y fotos. **Una función.**

## Por qué existe

Hasta el 2026-09-24 picking y packing de un mismo pedido se unían **por un
string**: `TareaPicking.referencia_documento` contra
`TareaPacking.numero_pedido_siesa` (`'PD1502'`). El packing nace en paralelo
con `crear_manual` (`routes/siesa.py`, `iniciar_despacho`), no desde el
picking, así que no hay ninguna clave que diga «estas tareas son del mismo
pedido» — solo que el texto coincide. Y el texto no lleva el CO, que es parte
de la clave documental (Regla 18: CO + tipo + consecutivo).

La analítica que reconstruye pedido → picking → packing → FE → ruta → recaudo
→ caja necesita una clave que no dependa de que dos strings armados en sitios
distintos coincidan por casualidad.

## Forma

    '003-PD-1502'    CO de 3 dígitos · tipo sin espacios, en mayúscula · consecutivo entero

`None` cuando falta cualquiera de las tres partes. **No se inventa**: una
clave sin CO que se completara con el CO por defecto uniría el pedido de otra
sede al primero que pase (Regla 0).

## De dónde sale el CO

1. `pedidos_siesa.centro_op` del mismo tipo + consecutivo — lo que dijo Siesa.
2. `almacenes.centro_op_siesa` (vía `co_de_bodega`) del almacén de la tarea.
   El sync de pedidos solo trae `f430_id_co = CONNEKTA_CENTRO_OP` para
   `CONNEKTA_BODEGA`, así que hoy el CO del pedido y el de su almacén son el
   mismo; si eso cambia, la fuente 1 manda.
3. Ninguna → `None`, declarado.

La migración `m036fotos` rellena el histórico con **la misma regla** escrita
en SQL (una migración no importa código de la app); el test
`test_cadena_pedido.py::TestElBackfillUsaLaMismaRegla` las compara.
"""
import logging
import re

logger = logging.getLogger(__name__)

#: Tipos de tarea que son de un pedido de venta. `TRASLADO` y el resto no.
TIPOS_DE_PEDIDO = ('PEDIDO', 'PEDIDO_SIESA')

_NUMERO = re.compile(r'^\s*([A-Za-z]+)\s*-?\s*0*(\d+)\s*$')


def _co_normalizado(co):
    s = '' if co is None else str(co).strip()
    if not s:
        return None
    return s.zfill(3) if s.isdigit() else s.upper()


def _tipo_normalizado(tipo):
    s = '' if tipo is None else str(tipo).strip().upper()
    return s or None


def _consec_normalizado(consec):
    s = '' if consec is None else str(consec).strip()
    if not s.isdigit():
        return None
    return int(s)


def clave_pedido(co, tipo, consec):
    """`'003-PD-1502'`, o `None` si falta alguna parte. **La única que arma la clave.**"""
    c, t, n = _co_normalizado(co), _tipo_normalizado(tipo), _consec_normalizado(consec)
    if not (c and t and n is not None):
        return None
    return f'{c}-{t}-{n}'


def partir_numero_pedido(numero):
    """`'PD1502'` → `('PD', 1502)`. `None` si el texto no tiene esa forma."""
    m = _NUMERO.match(str(numero or ''))
    if not m:
        return None
    return m.group(1).upper(), int(m.group(2))


def co_del_pedido(tipo, consec, almacen_id=None):
    """El CO de un pedido, o `None`. Ver el encabezado del módulo."""
    from app.extensions import db
    from app.models.pedido_siesa import PedidoSiesa

    t, n = _tipo_normalizado(tipo), _consec_normalizado(consec)
    if t and n is not None:
        cos = {(r[0] or '').strip() for r in db.session.query(PedidoSiesa.centro_op)
               .filter(PedidoSiesa.tipo_docto == t, PedidoSiesa.consec_docto == n)
               .distinct().all()}
        cos.discard('')
        if len(cos) == 1:
            return _co_normalizado(cos.pop())
        if len(cos) > 1:
            # Mismo tipo+consecutivo en dos CO: sin el CO no se sabe cuál es.
            logger.warning('[CADENA] %s-%s existe en varios CO (%s) — clave sin '
                           'resolver', t, n, sorted(cos))
            return None
    if almacen_id:
        from app.models.almacen import Almacen
        from app.services.bodegas import co_de_bodega
        alm = db.session.get(Almacen, almacen_id)
        if alm is not None:
            co = (alm.centro_op_siesa or '').strip() or co_de_bodega(alm.bodega_siesa_id)
            return _co_normalizado(co)
    return None


def clave_de_documento(numero_pedido=None, tipo=None, consec=None, almacen_id=None,
                       co=None):
    """La clave de un pedido a partir de lo que tenga a mano quien crea la tarea.

    `tipo`/`consec` explícitos mandan; si faltan se parte `numero_pedido`. El
    CO, si no viene, sale de `co_del_pedido`. Nunca levanta: una clave que no
    se pudo armar es `None`, y crear la tarea no puede depender de ella.
    """
    try:
        t, n = _tipo_normalizado(tipo), _consec_normalizado(consec)
        if not (t and n is not None):
            partes = partir_numero_pedido(numero_pedido)
            if not partes:
                return None
            t, n = partes
        c = _co_normalizado(co) or co_del_pedido(t, n, almacen_id)
        return clave_pedido(c, t, n)
    except Exception as e:     # pragma: no cover — defensivo, la creación manda
        logger.warning('[CADENA] no se pudo armar la clave de %s: %s', numero_pedido, e)
        return None


def clave_de_tarea_picking(referencia_documento, tipo_documento, almacen_id):
    """La clave para una tarea de picking. `None` si no es de un pedido.

    Solo con `tipo_documento` de pedido: una referencia libre de una tarea
    manual (`'MANUAL-1'`) tiene la misma forma que `'PD1502'` y armaría una
    clave de un pedido que no existe.
    """
    if (tipo_documento or '').upper() not in TIPOS_DE_PEDIDO:
        return None
    if not referencia_documento:
        return None
    return clave_de_documento(numero_pedido=referencia_documento, almacen_id=almacen_id)
