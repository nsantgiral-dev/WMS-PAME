"""
La parada tardía: una ruta cerrada con paradas que nadie gestionó
(tanda 2 · B, decisión del dueño 2026-09-25). **Una función por pregunta.**

La cola sin señal del conductor puede mandar el cierre de la ruta antes que
una confirmación (o una confirmación puede no llegar nunca). La ruta queda
ENTREGADA con esa parada sin gestionar, y la liquidación la exige toda
gestionada: sin salida, salvo cerrar lo que falta como rechazado.

| Pregunta | Función |
|---|---|
| ¿Qué paradas de esta ruta nadie gestionó, cuánto valen, hace cuánto? | `sin_gestionar(ruta)` |
| ¿Cuáles llevan más de un día así, en todas las rutas? | `sin_gestionar_viejas()` |
| ¿Qué le dice el resumen diario? | `lineas_de_aviso()` |
| Lo que el teléfono manda DESPUÉS de que la oficina la registró | `version_del_conductor(recaudo, data)` |

**La oficina registra; el conductor no pisa.** La oficina (quien liquida:
`permisos_liquidacion.puede_liquidar`) la registra con el formulario de
Liquidación —mismas validaciones que la pantalla del conductor, motivo
obligatorio, FORZAR en la bitácora— y la parada queda
`registrada_por_oficina`. Si después llega la confirmación del teléfono, se
guarda aparte (`version_conductor`) y se marca si difiere
(`diferencia_conductor`): nada de lo que la oficina registró cambia sin que
una persona lo mire. Es señal, no sanción (regla 2 de flota).
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: Desde cuántas horas una parada sin gestionar va al resumen diario.
HORAS_AVISO_SIN_GESTIONAR = 24


def _inicio_del_hueco(ruta):
    """Desde cuándo la parada espera: el cierre de la ruta. Sin él (una ruta
    vieja), la salida del camión; sin nada, `None` (no se inventa)."""
    return (getattr(ruta, 'fecha_entregada', None) or getattr(ruta, 'fecha_cierre', None)
            or getattr(ruta, 'fecha_creacion', None))


def version_formulario_actual() -> int:
    """La versión de formulario que este servidor sabe exigir: evidencia (2),
    contado contraentrega sin crédito en el select (3) y una PARCIAL que dice
    qué volvió (4). **La única**: la lista del conductor y el formulario de la
    oficina la leen de acá."""
    from app.services import cond_pago as _cp
    from app.services import devolucion_ruta as _dr
    from app.services import senales_ruta as _sr
    return max(_sr.VERSION_FORMULARIO_CON_EVIDENCIA, _cp.VERSION_FORMULARIO_CONTADO,
               _dr.VERSION_FORMULARIO_DEVOLUCION)


def formulario_oficina() -> dict:
    """Lo que el formulario de la oficina necesita del servidor (y no copia
    en el JS): la versión que se exige, qué formas piden comprobante y cuáles
    no cobran."""
    from app.services.ruta_service import FormaPago
    from app.services import cond_pago as _cp
    from app.services import senales_ruta as _sr
    return {'version_formulario': version_formulario_actual(),
            'formas_con_comprobante': [f for f in FormaPago.VALIDOS if _sr.requiere_comprobante(f)],
            'formas_que_no_cobran': list(_cp.FORMAS_QUE_NO_COBRAN)}


def sin_gestionar(ruta, ahora: datetime = None) -> list:
    """Las paradas de `ruta` sin `RecaudoEntrega`, con lo que la oficina
    necesita para registrarlas: `{tarea_id, pedido, cliente, municipio, valor,
    horas, items[]}`. `valor` es la factura anotada (o `None`: sin valor, no
    cero). `horas` cuenta desde el cierre de la ruta (o `None`)."""
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import cond_pago as _cp
    ahora = ahora or datetime.utcnow()
    gestionadas = {r.tarea_id for r in RecaudoEntrega.query.filter_by(ruta_id=ruta.id).all()}
    t0 = _inicio_del_hueco(ruta)
    horas = round((ahora - t0).total_seconds() / 3600, 1) if t0 else None
    out = []
    for t in ruta.tareas_unicas():
        if t.id in gestionadas:
            continue
        items = sorted(t.items or [], key=lambda i: i.id or 0)
        out.append({
            'tarea_id': t.id,
            'pedido': t.numero_pedido_siesa,
            'cliente': t.cliente or '',
            'municipio': getattr(t, 'municipio', None) or '',
            'valor': float(t.valor_factura) if t.valor_factura is not None else None,
            'horas': horas,
            # Si se cobra al entregar (la misma política de la pantalla del
            # conductor, sin red): el select de la oficina no ofrece crédito.
            'cobro_contraentrega': bool(_cp.cobro_de_tarea(t)['cobrar']),
            'items': [{
                'codigo': i.producto.codigo if i.producto else '',
                'nombre': i.producto.nombre if i.producto else '',
                'unidad': i.producto.unidad_empaque if i.producto else 'und',
                'cantidad_pedida': i.cantidad_real or i.cantidad_esperada,
            } for i in items],
        })
    return out


def sin_gestionar_viejas(horas: float = HORAS_AVISO_SIN_GESTIONAR, ahora: datetime = None) -> list:
    """Paradas sin gestionar hace más de `horas` en rutas ENTREGADAS y sin
    liquidar, desde el corte (`rezago_liquidacion.rutas_entregadas_sin_liquidar`,
    el mismo universo que la alerta de rutas). `[{ruta_id, conductor, pedido,
    cliente, valor, horas}]`, lo más viejo primero. Una ruta sin fecha cuenta
    (Regla 0: no saber cuándo no la vuelve reciente)."""
    from app.services.rezago_liquidacion import rutas_entregadas_sin_liquidar
    ahora = ahora or datetime.utcnow()
    out = []
    for ruta in rutas_entregadas_sin_liquidar():
        for p in sin_gestionar(ruta, ahora):
            if p['horas'] is not None and p['horas'] < horas:
                continue
            out.append({'ruta_id': ruta.id,
                        'conductor': getattr(getattr(ruta, 'conductor', None), 'nombre', None),
                        'pedido': p['pedido'], 'cliente': p['cliente'],
                        'valor': p['valor'], 'horas': p['horas']})
    return sorted(out, key=lambda x: -(x['horas'] if x['horas'] is not None else 1e9))


def lineas_de_aviso(ahora: datetime = None) -> list:
    """Para el resumen diario por correo."""
    viejas = sin_gestionar_viejas(ahora=ahora)
    if not viejas:
        return []
    rutas = sorted({v['ruta_id'] for v in viejas})
    return [f'⚠ {len(viejas)} parada(s) sin gestionar hace más de '
            f'{HORAS_AVISO_SIN_GESTIONAR} h en rutas ya cerradas (ruta '
            + ', '.join(str(r) for r in rutas[:10])
            + '): pídale al conductor que abra la app con señal; si no llega, '
              'regístrela desde Liquidación.']


# ─────────────────────────────────────────────────────────────────────────────
# La versión del conductor después de la oficina
# ─────────────────────────────────────────────────────────────────────────────

def _normalizar(estado, forma_pago, motivo_rechazo, monto_cobrado, monto_descuento, items):
    """Lo que se compara, con la traducción del servidor ya aplicada: un
    «no pagó y se quedó» del teléfono es ENTREGADO_SIN_PAGO, y una parada sin
    cobro no tiene forma de pago."""
    from app.models.recaudo_entrega import EstadoEntrega, forma_pago_de
    from app.services import motivos_rechazo as _mr
    est = (estado or '').upper()
    mot = (motivo_rechazo or '').strip().upper() or None
    if est == EstadoEntrega.RECHAZADO and mot and not _mr.retorna_mercancia(mot):
        est = EstadoEntrega.ENTREGADO_SIN_PAGO
    devueltas = {}
    if est == EstadoEntrega.PARCIAL:
        for it in items or []:
            # Sin cantidades no se compara (no se rellena: un dato de entrega
            # ausente no es «se entregó todo»).
            if not isinstance(it, dict) or it.get('cantidad_pedida') is None \
                    or it.get('cantidad_entregada') is None:
                continue
            try:
                d = int(it['cantidad_pedida']) - int(it['cantidad_entregada'])
            except (TypeError, ValueError):
                continue
            if d > 0:
                devueltas[str(it.get('codigo', ''))] = d
    return {
        'estado_entrega': est,
        'forma_pago': (forma_pago_de(est, (forma_pago or '').upper() or None) or None),
        'motivo_rechazo': mot if est in (EstadoEntrega.RECHAZADO,
                                         EstadoEntrega.ENTREGADO_SIN_PAGO) else None,
        'monto_cobrado': round(float(monto_cobrado or 0), 2),
        'monto_descuento': round(float(monto_descuento or 0), 2),
        'devueltas': devueltas,
    }


def version_del_conductor(recaudo, data: dict) -> dict:
    """Guarda en `recaudo` lo que mandó el teléfono después de lo registrado
    por la oficina, **sin tocar lo registrado**, y dice si difiere.

    `{difiere, campos}`. No hace commit. Solo sobre una parada
    `registrada_por_oficina` (quien llama lo verifica)."""
    oficina = _normalizar(recaudo.estado_entrega, recaudo.forma_pago, recaudo.motivo_rechazo,
                          recaudo.monto_cobrado, recaudo.monto_descuento,
                          recaudo.items_entregados)
    telefono = _normalizar(data.get('estado_entrega'), data.get('forma_pago'),
                           data.get('motivo_rechazo'), data.get('monto_cobrado'),
                           data.get('monto_descuento'), data.get('items_entregados'))
    campos = [k for k in oficina
              if (abs(oficina[k] - telefono[k]) > 0.01 if isinstance(oficina[k], float)
                  else oficina[k] != telefono[k])]
    recaudo.version_conductor = {
        **telefono,
        'observaciones': (data.get('observaciones') or '')[:500] or None,
        'referencia_pago': (data.get('referencia_pago') or None),
        'tiene_foto': bool(data.get('foto_entrega') or data.get('foto_comprobante')),
        'ts_dispositivo': data.get('ts_dispositivo'),
        'via_cola': data.get('via_cola') if isinstance(data.get('via_cola'), bool) else None,
        'campos_que_difieren': campos,
    }
    recaudo.version_conductor_en = datetime.utcnow()
    recaudo.diferencia_conductor = bool(campos)
    if campos:
        logger.warning('[RUTAS] parada %s/%s: el conductor mandó una versión distinta de la '
                       'registrada por la oficina (%s) — queda para revisar',
                       recaudo.ruta_id, recaudo.tarea_id, ', '.join(campos))
    return {'difiere': bool(campos), 'campos': campos}


#: Palabras de los campos que pueden diferir (para las pantallas y la señal).
CAMPOS_EN_PALABRAS = {
    'estado_entrega': 'el resultado de la entrega',
    'forma_pago': 'la forma de pago',
    'motivo_rechazo': 'el motivo',
    'monto_cobrado': 'el monto cobrado',
    'monto_descuento': 'el descuento',
    'devueltas': 'lo devuelto',
}


def texto_diferencia(recaudo) -> str | None:
    """La señal en palabras, o `None` si no hay versión del conductor o coincide."""
    if not recaudo.diferencia_conductor:
        return None
    campos = (recaudo.version_conductor or {}).get('campos_que_difieren') or []
    lista = ', '.join(CAMPOS_EN_PALABRAS.get(c, c) for c in campos) or 'algún dato'
    return (f'El conductor mandó después otra versión de esta parada ({lista}): lo '
            f'registrado por la oficina no cambió. Revíselo con él.')
