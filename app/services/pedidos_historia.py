"""
Historia de las líneas de pedido — la escribe el sync de pedidos, y solo él.

Ver `app/models/pedido_historia.py` para el porqué. Acá, las reglas:

1. **Toda línea leída se registra**, venga el barrido completo o no: que se vio
   es un hecho aunque falten páginas. Primera vez → fila nueva con
   `primera_vez_vista_at`; después → cantidades y `ultima_vez_vista_at`.
2. **Ninguna salida se registra desde un barrido incompleto.** Con la
   paginación cortada a medias, las líneas de las páginas no leídas se leen
   igual que las que ya no existen — es la misma regla que protege el borrado
   de `pedidos_siesa` (Regla 0).
3. `ultima_vez_vista_at` se refresca como mucho cada `REFRESCO_MINUTOS`, salvo
   que cambie una cantidad o el estado. El sync corre cada minuto; escribir
   todas las líneas cada minuto es carga sin información.
4. Una línea que ya salió y vuelve a verse se reabre (`reapariciones += 1`).
5. Las salidas `DESAPARECIDO` se clasifican después preguntando el estado del
   pedido a Siesa, **como mucho `MAX_CLASIFICAR_POR_CICLO` pedidos por ciclo**
   (cada consulta puede tardar 30 s). Sin respuesta, se queda DESAPARECIDO.

Nunca levanta hacia el sync: la historia no puede tumbar la sincronización de
la que depende el picking. Corre dentro de un SAVEPOINT; si algo falla, se
revierte solo la historia y se declara en el resultado.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.pedido_historia import MotivoSalidaPedido, PedidoHistoria
from app.services.cadena_pedido import clave_pedido
from app.utils.fecha import dia_operativo_de

logger = logging.getLogger(__name__)

REFRESCO_MINUTOS = 10
MAX_CLASIFICAR_POR_CICLO = 3


def _dec(v):
    if v is None or v == '':
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _fecha(v):
    s = str(v or '').strip()
    if len(s) >= 10 and s[4] == '-':
        try:
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.strptime(s[:8], '%Y%m%d').date()
        except ValueError:
            return None
    return None


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _campos_de_fila(item: dict) -> dict:
    """Lo que se guarda de una fila de `API_v2_Ventas_Pedidos`."""
    from app.utils.dane_municipios import resolver_municipio
    tipo = (item.get('f430_id_tipo_docto') or '').strip()
    consec = _int(item.get('f430_consec_docto'))
    co = (str(item.get('f430_id_co') or '')).strip()
    return {
        'pedido_clave': clave_pedido(co, tipo, consec),
        'co': co or None,
        'tipo_docto': tipo or None,
        'consec_docto': consec,
        'bodega': (item.get('f150_id') or '').strip() or None,
        'item_codigo': (item.get('f120_referencia') or '').strip() or None,
        'item_id_siesa': str(item.get('f120_id')) if item.get('f120_id') is not None else None,
        'cliente_id': (str(item.get('f200_id_pedido_fact') or '')).strip() or None,
        'cliente': item.get('f200_razon_social_pedido_fact'),
        'municipio': resolver_municipio(item.get('f015_id_depto_pe', ''),
                                        item.get('f015_id_ciudad_pe', '')) or None,
        'vendedor_id': (str(item.get('f200_id_pedido_vend') or '')).strip() or None,
        'cond_pago': (str(item.get('f430_id_cond_pago') or '')).strip() or None,
        'fecha_pedido': _fecha(item.get('f430_id_fecha')),
        'fecha_entrega': _fecha(item.get('f430_fecha_entrega')),
        'cantidad_pedida': _dec(item.get('f431_cant1_pedida')),
        'cantidad_remisionada': _dec(item.get('f431_cant1_remisionada')),
        'cantidad_comprometida': _dec(item.get('f431_cant1_comprometida')),
        'vlr_neto': _dec(item.get('f431_vlr_neto')),
        'estado_siesa': _int(item.get('f430_ind_estado')),
    }


def _cumplida(campos: dict) -> bool:
    p, r = campos.get('cantidad_pedida'), campos.get('cantidad_remisionada')
    return p is not None and r is not None and p > 0 and r >= p


def registrar_barrido(filas: list, paginacion_completa: bool, ahora: datetime = None,
                      co_barrido: str = None) -> dict:
    """Registra lo que el sync acaba de leer. Ver las reglas del módulo.

    `filas`: TODAS las filas leídas de Siesa (todas las bodegas del CO), no
    solo las pendientes de NB1. `ahora`: UTC naive. `co_barrido`: el CO que el
    sync le pidió a Siesa — las salidas se deciden solo dentro de él (una
    línea de otro CO no está ausente: no se preguntó por ella).
    """
    ahora = ahora or datetime.utcnow()
    dia = dia_operativo_de(ahora)
    res = {'nuevas': 0, 'actualizadas': 0, 'cumplidas': 0, 'desaparecidas': 0,
           'reabiertas': 0, 'sin_rowid': 0, 'salidas_registradas': paginacion_completa}
    try:
        with db.session.begin_nested():
            vistas = {}
            for item in filas or []:
                rowid = _int(item.get('f431_rowid'))
                if rowid is None:
                    res['sin_rowid'] += 1
                    continue
                vistas[rowid] = _campos_de_fila(item)

            existentes = {}
            if vistas:
                ids = list(vistas)
                for i in range(0, len(ids), 500):
                    for h in PedidoHistoria.query.filter(
                            PedidoHistoria.linea_rowid.in_(ids[i:i + 500])).all():
                        existentes[h.linea_rowid] = h

            umbral = ahora - timedelta(minutes=REFRESCO_MINUTOS)
            for rowid, campos in vistas.items():
                h = existentes.get(rowid)
                if h is None:
                    h = PedidoHistoria(
                        linea_rowid=rowid,
                        cantidad_pedida_inicial=campos['cantidad_pedida'],
                        primera_vez_vista_at=ahora, primer_dia_visto=dia,
                        ultima_vez_vista_at=ahora, ultimo_dia_visto=dia,
                        reapariciones=0,
                        **campos)
                    db.session.add(h)
                    res['nuevas'] += 1
                else:
                    if h.salida_at is not None and h.motivo_salida != MotivoSalidaPedido.CUMPLIDO:
                        # Volvió a verse: el pedido regresó al estado 3.
                        h.salida_at = h.salida_dia = h.motivo_salida = None
                        h.estado_siesa_salida = None
                        h.reapariciones = (h.reapariciones or 0) + 1
                        res['reabiertas'] += 1
                    cambio = any(
                        getattr(h, k) != v for k, v in campos.items()
                        if k in ('cantidad_pedida', 'cantidad_remisionada',
                                 'cantidad_comprometida', 'estado_siesa'))
                    if cambio or h.ultima_vez_vista_at is None or h.ultima_vez_vista_at <= umbral:
                        for k, v in campos.items():
                            setattr(h, k, v)
                        h.ultima_vez_vista_at = ahora
                        h.ultimo_dia_visto = dia
                        res['actualizadas'] += 1

                if paginacion_completa and h.salida_at is None and _cumplida(campos):
                    h.salida_at, h.salida_dia = ahora, dia
                    h.motivo_salida = MotivoSalidaPedido.CUMPLIDO
                    res['cumplidas'] += 1

            if paginacion_completa:
                # Lo que estaba abierto y este barrido COMPLETO ya no trae.
                q = PedidoHistoria.query.filter(PedidoHistoria.salida_at.is_(None))
                cos = ({str(co_barrido).strip()} if co_barrido
                       else {c['co'] for c in vistas.values() if c.get('co')})
                if not cos:
                    # Barrido completo, vacío y sin CO declarado: no se sabe de
                    # qué universo se ausentan las abiertas. No se decide.
                    return res
                q = q.filter(PedidoHistoria.co.in_(sorted(cos)))
                for h in q.all():
                    if h.linea_rowid in vistas:
                        continue
                    h.salida_at, h.salida_dia = ahora, dia
                    h.motivo_salida = MotivoSalidaPedido.DESAPARECIDO
                    res['desaparecidas'] += 1
    except Exception as e:
        logger.error('[PEDIDOS_HISTORIA] no se registró el barrido: %s', e)
        res['error'] = str(e)[:300]
    return res


def clasificar_desaparecidas(gateway=None, maximo: int = MAX_CLASIFICAR_POR_CICLO) -> dict:
    """Pregunta a Siesa el estado de los pedidos DESAPARECIDOS sin estado.

    `get_estado_pedido` devuelve `None` ante error de red: se deja como está y
    se reintenta el próximo ciclo. Nunca levanta.
    """
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    res = {'consultados': 0, 'clasificados': 0}
    try:
        pendientes = (db.session.query(PedidoHistoria.tipo_docto, PedidoHistoria.consec_docto,
                                       PedidoHistoria.co)
                      .filter(PedidoHistoria.motivo_salida == MotivoSalidaPedido.DESAPARECIDO,
                              PedidoHistoria.estado_siesa_salida.is_(None),
                              PedidoHistoria.tipo_docto.isnot(None),
                              PedidoHistoria.consec_docto.isnot(None))
                      .distinct().limit(maximo).all())
        for tipo, consec, co in pendientes:
            res['consultados'] += 1
            estado = gateway.get_estado_pedido(tipo, consec)
            if estado is None:
                continue
            try:
                estado = int(estado)
            except (TypeError, ValueError):
                continue
            motivo = {4: MotivoSalidaPedido.CUMPLIDO,
                      9: MotivoSalidaPedido.ANULADO}.get(estado, MotivoSalidaPedido.OTRO_ESTADO)
            if estado == 3:
                # Sigue comprometido: el barrido lo reabrirá cuando lo vea.
                # Se marca para no volver a preguntar en cada ciclo.
                motivo = MotivoSalidaPedido.DESAPARECIDO
            n = (PedidoHistoria.query
                 .filter(PedidoHistoria.tipo_docto == tipo,
                         PedidoHistoria.consec_docto == consec,
                         PedidoHistoria.co == co,
                         PedidoHistoria.motivo_salida == MotivoSalidaPedido.DESAPARECIDO,
                         PedidoHistoria.estado_siesa_salida.is_(None))
                 .update({'motivo_salida': motivo, 'estado_siesa_salida': estado},
                         synchronize_session=False))
            res['clasificados'] += n
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning('[PEDIDOS_HISTORIA] clasificación de salidas falló: %s', e)
        res['error'] = str(e)[:300]
    return res
