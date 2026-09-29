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
  · PEDIDO → hasta que la OC aparece en el espejo, o hasta que ya debió
    llegar: lead time + 2σ del proveedor (`compras_fuentes.lead_time`; con el
    default nacional, 10 + 2 × 5 = 20 días). Se calcula AL LEER, no al
    registrar: si el lead time se mide después, la vigencia lo sigue. Vencido
    sin OC, deja de contarse y la línea vuelve MARCADA («ya se pidió el …, no
    ha llegado ni aparece en Siesa: confirme»). (Antes: 7 días fijos, menos
    que el lead time — validación del 2026-09-27, P1-C.)
  · La última decisión de una referencia manda: «No pedir» después de «Ya se
    pidió» deja de contar lo pedido.
  · POSPUESTO → la fecha elegida (mañana … 90 días).
  · DESCARTADO → un ciclo de compra (`ciclo_pedido_nacional`, 7).
"""
import json
import logging
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

PEDIDO, POSPUESTO, DESCARTADO = 'PEDIDO', 'POSPUESTO', 'DESCARTADO'
ACCIONES = (PEDIDO, POSPUESTO, DESCARTADO)

#: «Ya se pidió» cuenta como en camino hasta lead time + esto × σ_LT.
SIGMAS_PEDIDO_EN_CAMINO = 2
#: Una decisión más vieja que esto no puede estar vigente (posponer ≤ 90).
VENTANA_DECISIONES_DIAS = 120
#: Un «ya se pidió» vencido sin OC se sigue diciendo este tiempo.
DIAS_AVISO_PEDIDO_VENCIDO = 30
#: Una línea pospuesta o no pedida reaparece si lo que falta creció más que
#: esto sobre lo que se vio al decidir (supuesto declarado: 50 %).
CRECIMIENTO_REAPARECE = 0.5
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


class _Vigencias:
    """La vigencia de cada decisión, con el lead time de cada proveedor leído
    una vez por corrida."""

    def __init__(self):
        self._obs = None
        self._cache = {}

    def dias_pedido(self, proveedor=None) -> dict:
        if proveedor not in self._cache:
            from app.services import compras_fuentes as cf
            if self._obs is None:
                self._obs = cf.observaciones_lead_time()
            lt = cf.lead_time(proveedor=proveedor, origen=cf.ORIGEN_NACIONAL,
                              observaciones=self._obs)
            dias = max(1, int(math.ceil(float(lt['lt_dias'])
                                        + SIGMAS_PEDIDO_EN_CAMINO * float(lt['sigma_lt']))))
            self._cache[proveedor] = {
                'dias': dias, 'lt_dias': lt['lt_dias'], 'sigma_lt': lt['sigma_lt'],
                'fuente': lt['fuente'], 'proveedor': proveedor,
                'nota': (f'Un «ya se pidió» cuenta como en camino hasta que la orden aparece '
                         f'en Siesa o hasta {dias} días (lo que tarda en llegar, '
                         f'{lt["lt_dias"]:g} ± {lt["sigma_lt"]:g}, más dos veces su variación).')}
        return self._cache[proveedor]

    def vence(self, d):
        if d.accion == PEDIDO:
            return d.dia + timedelta(days=self.dias_pedido(d.proveedor_codigo)['dias'] - 1)
        return d.vigente_hasta


def dias_pedido_en_camino(proveedor=None) -> dict:
    """Cuántos días cuenta un «ya se pidió» como en camino (sin OC en el
    espejo): lead time + 2σ del proveedor, con su procedencia."""
    return _Vigencias().dias_pedido(proveedor)


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
        # Informativa: la que vale se recalcula al leer (`_Vigencias.vence`).
        vigente = hoy + timedelta(days=dias_pedido_en_camino(
            datos.get('proveedor_codigo') or None)['dias'] - 1)
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


def a_dict(d, hoy=None, nombre=None, vence=None) -> dict:
    hoy = hoy or _hoy()
    vence = vence or d.vigente_hasta
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
        'vigente_hasta': vence.isoformat() if vence else None,
        'vigente': bool(d.anulada_en is None and vence and vence >= hoy),
        'usuario_nombre': d.usuario_nombre,
        'creada_utc': d.creada_en.isoformat() if d.creada_en else None,
        'hace_min': hace_min,
        'anulada': d.anulada_en is not None,
        'anulada_motivo': d.anulada_motivo,
        'foto': foto,
    }


def _ultimas(refs=None, hoy=None):
    """La última decisión no anulada de cada referencia (de las que pueden
    estar vigentes). La última manda: una vencida tapa a una más vieja."""
    from app.models.decision_compra import DecisionCompra
    hoy = hoy or _hoy()
    q = DecisionCompra.query.filter(
        DecisionCompra.anulada_en.is_(None),
        DecisionCompra.dia >= hoy - timedelta(days=VENTANA_DECISIONES_DIAS))
    if refs is not None:
        refs = list({str(r).strip() for r in refs if r})
        if not refs:
            return {}
        q = q.filter(DecisionCompra.referencia.in_(refs))
    salida = {}
    for d in q.order_by(DecisionCompra.id).all():
        salida[d.referencia] = d          # la última gana
    return salida


def vigentes(refs=None, hoy=None) -> dict:
    """{ref: decisión} — la última no anulada, si su vigencia no pasó."""
    hoy = hoy or _hoy()
    vig = _Vigencias()
    ult = {r: (d, vig.vence(d)) for r, d in _ultimas(refs, hoy).items()}
    ult = {r: x for r, x in ult.items() if x[1] and x[1] >= hoy}
    nombres = _nombres(ult)
    return {r: a_dict(d, hoy, nombres.get(r), v) for r, (d, v) in ult.items()}


def recientes(dias=14, hoy=None) -> list:
    """«Ya decidido»: lo decidido en los últimos `dias` (anuladas incluidas,
    marcadas) MÁS toda decisión vigente aunque sea más vieja — una decisión
    que oculta una línea tiene que poder verse y deshacerse (P1-D). Lo más
    nuevo primero."""
    from app.models.decision_compra import DecisionCompra
    hoy = hoy or _hoy()
    desde = hoy - timedelta(days=max(1, int(dias)) - 1)
    filas = (DecisionCompra.query.filter(DecisionCompra.dia >= desde)
             .order_by(DecisionCompra.id.desc()).limit(300).all())
    vig = _Vigencias()
    nombres = _nombres(d.referencia for d in filas)
    salida = {d.id: a_dict(d, hoy, nombres.get(d.referencia), vig.vence(d)) for d in filas}
    for x in vigentes(None, hoy).values():
        salida.setdefault(x['id'], x)
    return sorted(salida.values(), key=lambda x: -x['id'])


def reaparece(decision, urgencia_actual, pedir_actual) -> dict:
    """¿Una línea pospuesta o no pedida vuelve a la bandeja? Sí, marcada, si
    se volvió URGENTE después de decidir, o si lo que falta creció más de
    `CRECIMIENTO_REAPARECE` sobre lo que se vio (un pospuesto ya urgente
    también vuelve si el faltante se dispara). «Ya se pidió» no oculta nada.

    Returns: {'reaparece': bool, 'motivo': SE_VOLVIO_URGENTE | FALTANTE_CRECIO
              | None, 'texto'}"""
    if not decision or decision.get('accion') == PEDIDO:
        return {'reaparece': False, 'motivo': None, 'texto': None}
    if urgencia_actual == 'URGENTE' and decision.get('urgencia_vista') != 'URGENTE':
        return {'reaparece': True, 'motivo': 'SE_VOLVIO_URGENTE',
                'texto': 'ahora es urgente: vuelva a mirarlo.'}
    vista = decision.get('cantidad_propuesta')
    if vista and pedir_actual and pedir_actual > vista * (1 + CRECIMIENTO_REAPARECE):
        return {'reaparece': True, 'motivo': 'FALTANTE_CRECIO',
                'texto': (f'ahora faltan {int(round(pedir_actual))} y cuando se decidió '
                          f'faltaban {int(round(vista))}: vuelva a mirarlo.')}
    return {'reaparece': False, 'motivo': None, 'texto': None}


def pedidos_sin_oc(filtro_skus=None, hoy=None) -> list:
    """Los «ya se pidió» que son la ÚLTIMA decisión de su referencia, con su
    vencimiento (lead time + 2σ del proveedor) y lo que hace falta para saber
    si la OC ya apareció en el espejo. **Solo datos**: qué se suma lo decide
    `compras_fuentes.en_camino` (la única función de «lo que viene»)."""
    hoy = hoy or _hoy()
    vig = _Vigencias()
    salida = []
    for ref, d in _ultimas(filtro_skus, hoy).items():
        if d.accion != PEDIDO:
            continue                      # «no pedir» después de pedir: manda lo último
        vence = vig.vence(d)
        if vence < hoy - timedelta(days=DIAS_AVISO_PEDIDO_VENCIDO):
            continue
        salida.append({'id': d.id, 'referencia': ref, 'dia': d.dia,
                       'vigente_hasta': vence, 'unidades': d.cantidad_decidida,
                       'oc_siesa': d.oc_siesa, 'usuario_nombre': d.usuario_nombre,
                       'vencida': vence < hoy})
    return salida


def sello() -> tuple:
    """Cambia con cada decisión registrada o deshecha (caché del ROP)."""
    from sqlalchemy import func
    from app.models.decision_compra import DecisionCompra
    return tuple(db.session.query(func.max(DecisionCompra.id),
                                  func.count(DecisionCompra.anulada_en)).one())


def consec_de(oc_texto):
    """El consecutivo de una OC escrita a mano («123», «OC-123», «003-OC-123»),
    o None si no se puede leer."""
    import re
    if not oc_texto:
        return None
    m = re.findall(r'\d+', str(oc_texto))
    return int(m[-1]) if m else None
