"""
Compras — lo que el comprador decidió (tanda F, 2026-09-27).

La bandeja se recalcula en cada carga y no guardaba nada: entre «Copiar OC» y
que la OC apareciera en el espejo de Siesa (30 min con el cron, nunca si está
apagado) la línea seguía ahí —dos personas pedían dos veces, o el lunes
siguiente volvía sin rastro— y no quedaba quién decidió ni contra qué número.

**Una política, una función por pregunta:**

| Pregunta | Función |
|---|---|
| ¿Quién puede decidir? | `puede_decidir(usuario)` — `Roles.COMPRAS_ESCRITURA` (admin, jefe de almacén, compras). El gerente mira, no escribe (decisión del dueño, 27-sep) |
| Registrar una decisión | `registrar(usuario, datos)` |
| Deshacerla | `anular(usuario, id, motivo)` — bitácora ANULAR |
| ¿Qué decisión vale hoy para cada referencia? | `vigentes(refs)` — la última no anulada cuya vigencia no pasó |
| ¿Qué se decidió estos días? | `recientes(dias)` |
| ¿Qué «lo pedí» siguen sin OC? | `pedidos_sin_oc(refs)` — la lee `compras_fuentes.en_camino` (fuente `DECISION_WMS`); nadie más suma lo pedido |

Vigencias (supuestos declarados, el dueño los corrige):
  · PEDIDO → `COMPRAS_PEDIDO_EN_CAMINO_DIAS` (7): lo que tarda en aparecer la
    OC en el espejo; si aparece antes, deja de contarse ahí (la cuenta el
    espejo). Si en 7 días no aparece, deja de contarse y se dice.
  · POSPUESTO → la fecha elegida (mañana … 90 días).
  · DESCARTADO → un ciclo de compra (`ciclo_pedido_nacional`, 7).
"""
import json
import logging
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

PEDIDO, POSPUESTO, DESCARTADO = 'PEDIDO', 'POSPUESTO', 'DESCARTADO'
ACCIONES = (PEDIDO, POSPUESTO, DESCARTADO)

DIAS_PEDIDO_EN_CAMINO_DEFAULT = 7
ENV_DIAS_PEDIDO = 'COMPRAS_PEDIDO_EN_CAMINO_DIAS'
POSPONER_MAX_DIAS = 90
POSPONER_SUGERIDO_DIAS = 7
#: Un mismo gesto repetido en menos de esto (doble toque) devuelve la misma
#: decisión en vez de crear otra.
SEGUNDOS_DOBLE_TOQUE = 60

#: Motivos de «No lo pido». El texto libre va aparte.
MOTIVOS_NO_PIDO = {
    'HAY_SUFICIENTE': 'Hay suficiente (el número no refleja la realidad)',
    'DESCONTINUADO': 'Producto descontinuado',
    'SIN_EXISTENCIAS_PROVEEDOR': 'El proveedor no tiene',
    'PRECIO': 'Precio muy alto: esperar',
    'OTRO': 'Otro motivo',
}

#: Las claves de la línea que se guardan como «lo que vio» (sin nada que no
#: sea de la bandeja: la foto no es un texto libre).
_CLAVES_FOTO = ('urgencia', 'pedir_unidades', 'pedir_empaques', 'valor_cop',
                'alcanza_dias', 'entrega_dias', 'proveedor_codigo', 'porque', 'precio',
                'vencido', 'si_no_llega_pedir')


class DecisionInvalida(ValueError):
    """El pedido de registrar/anular no se puede cumplir (400/404/409)."""

    def __init__(self, mensaje, estado=400):
        super().__init__(mensaje)
        self.estado = estado


def _hoy():
    from app.utils.fecha import dia_operativo
    return dia_operativo()


def dias_pedido_en_camino() -> dict:
    """Cuántos días cuenta un «lo pedí» como en camino, con su procedencia."""
    crudo = (os.getenv(ENV_DIAS_PEDIDO) or '').strip()
    if crudo:
        try:
            n = int(crudo)
            if 1 <= n <= 60:
                return {'dias': n, 'fuente': 'CONFIGURADO', 'nota': None}
        except ValueError:
            pass
        return {'dias': DIAS_PEDIDO_EN_CAMINO_DEFAULT, 'fuente': 'DEFAULT_DECLARADO',
                'nota': f'{ENV_DIAS_PEDIDO}={crudo!r} no es un número de días entre 1 y 60: '
                        'se usó el default.'}
    return {'dias': DIAS_PEDIDO_EN_CAMINO_DEFAULT, 'fuente': 'DEFAULT_DECLARADO',
            'nota': (f'Un «lo pedí» cuenta como en camino {DIAS_PEDIDO_EN_CAMINO_DEFAULT} días '
                     'o hasta que la orden aparezca en Siesa (supuesto).')}


def puede_decidir(usuario) -> bool:
    from app.routes._auth_helpers import Roles
    return bool(usuario and getattr(usuario, 'activo', True)
                and usuario.rol in Roles.COMPRAS_ESCRITURA)


def posponer_hasta_sugerido():
    return (_hoy() + timedelta(days=POSPONER_SUGERIDO_DIAS)).isoformat()


def _cantidad(v, campo):
    if v is None or v == '':
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        raise DecisionInvalida(f'{campo} no es un número.')
    if d < 0 or d != d.to_integral_value():
        raise DecisionInvalida(f'{campo} tiene que ser un número entero, cero o más.')
    return d


def _foto(datos):
    f = datos.get('foto')
    if not isinstance(f, dict):
        return None
    limpia = {k: f.get(k) for k in _CLAVES_FOTO if k in f}
    texto = json.dumps(limpia, ensure_ascii=False, default=str)
    return texto[:8000]


def registrar(usuario, datos) -> dict:
    """Guarda lo que el comprador decidió sobre una referencia.

    `datos`: {referencia, accion, cantidad (PEDIDO), oc_siesa (PEDIDO,
    opcional), hasta (POSPUESTO, AAAA-MM-DD), motivo (DESCARTADO: código de
    `MOTIVOS_NO_PIDO` y/o texto), cantidad_propuesta, urgencia, proveedor_codigo,
    foto}.

    Raises: DecisionInvalida (400) · PermissionError (sin permiso)."""
    from app.models.decision_compra import DecisionCompra
    from app.services.armador_service import ciclo_pedido_nacional
    from app.services.bitacora import registrar_accion

    if not puede_decidir(usuario):
        raise PermissionError('Registrar lo que se hizo con una compra es de admin, '
                              'jefe de almacén o compras.')
    ref = (datos.get('referencia') or '').strip()
    accion = (datos.get('accion') or '').strip().upper()
    if not ref:
        raise DecisionInvalida('Falta la referencia.')
    if accion not in ACCIONES:
        raise DecisionInvalida('La decisión tiene que ser «lo pedí», «lo pospongo» o «no lo pido».')
    hoy = _hoy()
    cantidad = _cantidad(datos.get('cantidad'), 'La cantidad')
    propuesta = _cantidad(datos.get('cantidad_propuesta'), 'La cantidad propuesta')
    oc = (str(datos.get('oc_siesa') or '')).strip()[:40] or None
    motivo_cod = (datos.get('motivo_codigo') or '').strip().upper() or None
    motivo_txt = (datos.get('motivo') or '').strip()[:250] or None

    if accion == PEDIDO:
        if cantidad is None or cantidad <= 0:
            raise DecisionInvalida('Diga cuántas unidades pidió.')
        vigente = hoy + timedelta(days=dias_pedido_en_camino()['dias'] - 1)
        motivo = motivo_txt
    elif accion == POSPUESTO:
        try:
            hasta = datetime.strptime(str(datos.get('hasta') or '')[:10], '%Y-%m-%d').date()
        except ValueError:
            raise DecisionInvalida('Diga hasta qué fecha lo pospone (AAAA-MM-DD).')
        if hasta <= hoy or hasta > hoy + timedelta(days=POSPONER_MAX_DIAS):
            raise DecisionInvalida(f'La fecha tiene que estar entre mañana y dentro de '
                                   f'{POSPONER_MAX_DIAS} días.')
        vigente, cantidad, oc = hasta, None, None
        motivo = motivo_txt
    else:
        if motivo_cod and motivo_cod not in MOTIVOS_NO_PIDO:
            raise DecisionInvalida('Ese motivo no está en la lista.')
        if not motivo_cod and not motivo_txt:
            raise DecisionInvalida('Diga por qué no lo pide.')
        if motivo_cod == 'OTRO' and not motivo_txt:
            raise DecisionInvalida('Con «otro motivo», escriba cuál.')
        partes = [MOTIVOS_NO_PIDO[motivo_cod]] if motivo_cod else []
        if motivo_txt:
            partes.append(motivo_txt)
        motivo = ' — '.join(partes)[:300]
        vigente = hoy + timedelta(days=ciclo_pedido_nacional()['dias'] - 1)
        cantidad, oc = None, None

    # Doble toque: el mismo gesto de la misma persona hace un momento.
    hace = datetime.utcnow() - timedelta(seconds=SEGUNDOS_DOBLE_TOQUE)
    previa = (DecisionCompra.query
              .filter(DecisionCompra.referencia == ref, DecisionCompra.accion == accion,
                      DecisionCompra.usuario_id == getattr(usuario, 'id', None),
                      DecisionCompra.anulada_en.is_(None),
                      DecisionCompra.creada_en >= hace)
              .order_by(DecisionCompra.id.desc()).first())
    if previa is not None and previa.cantidad_decidida == cantidad and previa.oc_siesa == oc:
        return dict(a_dict(previa), repetida=True)

    d = DecisionCompra(
        referencia=ref, accion=accion, cantidad_propuesta=propuesta,
        cantidad_decidida=cantidad, oc_siesa=oc,
        proveedor_codigo=(datos.get('proveedor_codigo') or None),
        motivo=motivo, urgencia_vista=(datos.get('urgencia') or None),
        dia=hoy, vigente_hasta=vigente,
        usuario_id=getattr(usuario, 'id', None),
        usuario_nombre=(getattr(usuario, 'nombre', None) or getattr(usuario, 'email', None)),
        creada_en=datetime.utcnow(), foto=_foto(datos))
    db.session.add(d)
    db.session.flush()
    if accion == DESCARTADO:
        # «No lo pido» deja de hacer algo pendiente: la bitácora lo registra
        # (vocabulario cerrado: DESCARTAR), en la misma transacción.
        registrar_accion('DESCARTAR', d, usuario_id=d.usuario_id, motivo=motivo,
                         despues={'referencia': ref, 'cantidad_propuesta': float(propuesta)
                                  if propuesta is not None else None})
    db.session.commit()
    return a_dict(d)


def anular(usuario, decision_id, motivo) -> dict:
    """Deshace una decisión (queda, marcada). Bitácora ANULAR con el motivo."""
    from app.models.decision_compra import DecisionCompra
    from app.services.bitacora import motivo_obligatorio, registrar_accion
    if not puede_decidir(usuario):
        raise PermissionError('Deshacer una decisión de compra es de admin, jefe de almacén '
                              'o compras.')
    d = db.session.get(DecisionCompra, int(decision_id))
    if d is None:
        raise DecisionInvalida('Esa decisión no existe.', 404)
    if d.anulada_en is not None:
        raise DecisionInvalida('Esa decisión ya estaba deshecha.', 409)
    motivo = motivo_obligatorio(motivo, 'deshacer una decisión de compra')
    antes = a_dict(d)
    d.anulada_en = datetime.utcnow()
    d.anulada_por_id = getattr(usuario, 'id', None)
    d.anulada_motivo = motivo[:300]
    registrar_accion('ANULAR', d, usuario_id=d.anulada_por_id, motivo=motivo,
                     antes={'accion': antes['accion'], 'cantidad': antes['cantidad'],
                            'oc_siesa': antes['oc_siesa'], 'usuario': antes['usuario_nombre']})
    db.session.commit()
    return a_dict(d)


def _nombres(refs):
    from app.models.producto import Producto
    refs = list({r for r in refs if r})
    if not refs:
        return {}
    return {p.codigo_siesa: p.nombre for p in
            Producto.query.filter(Producto.codigo_siesa.in_(refs)).all()}


def a_dict(d, hoy=None, nombre=None) -> dict:
    hoy = hoy or _hoy()
    hace_min = None
    if d.creada_en:
        hace_min = int((datetime.utcnow() - d.creada_en).total_seconds() // 60)
    try:
        foto = json.loads(d.foto) if d.foto else None
    except ValueError:
        foto = None
    return {
        'id': d.id, 'referencia': d.referencia, 'nombre': nombre, 'accion': d.accion,
        'cantidad': float(d.cantidad_decidida) if d.cantidad_decidida is not None else None,
        'cantidad_propuesta': (float(d.cantidad_propuesta)
                               if d.cantidad_propuesta is not None else None),
        'oc_siesa': d.oc_siesa, 'proveedor_codigo': d.proveedor_codigo,
        'motivo': d.motivo, 'urgencia_vista': d.urgencia_vista,
        'dia': d.dia.isoformat() if d.dia else None,
        'vigente_hasta': d.vigente_hasta.isoformat() if d.vigente_hasta else None,
        'vigente': bool(d.anulada_en is None and d.vigente_hasta and d.vigente_hasta >= hoy),
        'usuario_nombre': d.usuario_nombre,
        'creada_utc': d.creada_en.isoformat() if d.creada_en else None,
        'hace_min': hace_min,
        'anulada': d.anulada_en is not None,
        'anulada_motivo': d.anulada_motivo,
        'foto': foto,
    }


def vigentes(refs=None, hoy=None) -> dict:
    """{ref: decisión} — la última no anulada cuya vigencia no pasó."""
    from app.models.decision_compra import DecisionCompra
    hoy = hoy or _hoy()
    q = DecisionCompra.query.filter(DecisionCompra.anulada_en.is_(None),
                                    DecisionCompra.vigente_hasta >= hoy)
    if refs is not None:
        refs = list({str(r).strip() for r in refs if r})
        if not refs:
            return {}
        q = q.filter(DecisionCompra.referencia.in_(refs))
    salida = {}
    for d in q.order_by(DecisionCompra.id).all():
        salida[d.referencia] = d          # la última gana
    nombres = _nombres(salida)
    return {r: a_dict(d, hoy, nombres.get(r)) for r, d in salida.items()}


def recientes(dias=14, hoy=None) -> list:
    """Lo decidido en los últimos `dias` (anuladas incluidas, marcadas), lo
    más nuevo primero."""
    from app.models.decision_compra import DecisionCompra
    hoy = hoy or _hoy()
    desde = hoy - timedelta(days=max(1, int(dias)) - 1)
    filas = (DecisionCompra.query.filter(DecisionCompra.dia >= desde)
             .order_by(DecisionCompra.id.desc()).limit(300).all())
    nombres = _nombres(d.referencia for d in filas)
    return [a_dict(d, hoy, nombres.get(d.referencia)) for d in filas]


def pedidos_sin_oc(filtro_skus=None, hoy=None) -> list:
    """Los «lo pedí» vigentes, con lo que hace falta para saber si la OC ya
    apareció en el espejo. **Solo datos**: qué se suma lo decide
    `compras_fuentes.en_camino` (la única función de «lo que viene»)."""
    from app.models.decision_compra import DecisionCompra
    hoy = hoy or _hoy()
    # Los vencidos se miran un mes más (para decir «la OC nunca apareció»);
    # después ya no son noticia.
    q = DecisionCompra.query.filter(DecisionCompra.accion == PEDIDO,
                                    DecisionCompra.anulada_en.is_(None),
                                    DecisionCompra.vigente_hasta >= hoy - timedelta(days=30))
    salida = []
    ultimas = {}
    for d in q.order_by(DecisionCompra.id).all():
        if filtro_skus is not None and d.referencia not in filtro_skus:
            continue
        ultimas[d.referencia] = d         # una por referencia: la última
    for d in ultimas.values():
        salida.append({'id': d.id, 'referencia': d.referencia, 'dia': d.dia,
                       'vigente_hasta': d.vigente_hasta, 'unidades': d.cantidad_decidida,
                       'oc_siesa': d.oc_siesa, 'usuario_nombre': d.usuario_nombre,
                       'vencida': d.vigente_hasta < hoy})
    return salida


def consec_de(oc_texto):
    """El consecutivo de una OC escrita a mano («123», «OC-123», «003-OC-123»),
    o None si no se puede leer."""
    import re
    if not oc_texto:
        return None
    m = re.findall(r'\d+', str(oc_texto))
    return int(m[-1]) if m else None
