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


# ─────────────────────────────────────────────────────────────────────────────
# La puerta de la confirmación: quién escribe una parada, y cómo
# ─────────────────────────────────────────────────────────────────────────────
#
# Validación de la plata (2026-09-26, P0). La puerta de la parada tardía
# aceptaba CUALQUIER parada de una ruta cerrada, no solo las sin gestionar:
# quien liquida (admin + liquidador) reescribía lo que el conductor había
# confirmado —un ENTREGADO en efectivo pasaba a «no pagó y se quedó»— sin el
# permiso de corregir cobros, que la matriz del dueño dio a admin + líder de
# cartera. La clase: *una puerta de oficina que reescribe lo confirmado sin el
# permiso de corregir cobros*. Ahora hay dos operaciones de oficina, y son
# distintas:
#
# | Operación | Sobre qué | Quién | Queda |
# |---|---|---|---|
# | Registrar | una parada SIN gestionar (ruta cerrada, o en camino) | cerrada: `puede_registrar_parada_tardia`; en camino: `puede_corregir_cobro` | motivo, FORZAR, `registrada_por_oficina` |
# | Corregir | una parada YA registrada (por quien sea) | `puede_corregir_cobro` | motivo, FORZAR `parada_confirmada_corregida_por_la_oficina`, `registrada_por_oficina` |
#
# Si la oficina abrió el formulario sobre una parada sin gestionar y mientras
# tanto entró la cola del conductor, su POST **no pisa**: 409.

#: Los modos que devuelve `puerta_de_confirmacion`.
CONDUCTOR = 'CONDUCTOR'
CONDUCTOR_COLA_TARDIA = 'CONDUCTOR_COLA_TARDIA'
OFICINA_REGISTRA = 'OFICINA_REGISTRA'
OFICINA_CORRIGE = 'OFICINA_CORRIGE'

MOTIVO_COLA_TARDIA = ('La confirmación llegó por la cola sin señal del conductor después '
                      'de cerrada la ruta')


class PuertaCerrada(ValueError):
    """La confirmación no entra por esta puerta. `status`: 403 (sin permiso),
    409 (la parada cambió mientras la oficina llenaba el formulario) o 400."""

    def __init__(self, mensaje: str, status: int = 400):
        super().__init__(mensaje)
        self.status = status


def es_oficina_de_paradas(usuario) -> bool:
    """¿Puede esta persona escribir paradas desde la oficina (registrar o
    corregir)? Solo decide si atraviesa la puerta; qué puede hacer adentro lo
    decide `puerta_de_confirmacion`."""
    from app.services import permisos_liquidacion as _pl
    return _pl.puede_registrar_parada_tardia(usuario) or _pl.puede_corregir_cobro(usuario)


def puerta_de_confirmacion(ruta, previa, usuario, *, es_conductor: bool, data: dict,
                           motivo_tardia=None, motivo_correccion=None,
                           adopta_version: bool = False) -> dict:
    """**La única que decide** si una confirmación de parada entra y cómo:
    `{modo, motivo}`. La llaman la ruta HTTP y el servicio (`confirmar_parada`):
    un guard en la ruta protege la ruta; en el servicio, la operación.

    - `es_conductor`: el conductor de la ruta desde su teléfono. Con la ruta en
      camino confirma y corrige lo suyo; con la ruta cerrada, solo la primera
      confirmación de una parada sin gestionar que llega por la cola sin señal.
      Una parada que registró la oficina no la pisa: eso lo resuelve quien llama
      antes (`version_del_conductor`).
    - Oficina, **registrar** (`motivo_tardia`, o sin motivo explícito sobre una
      parada sin gestionar): solo si la parada sigue sin gestionar; si alguien
      la registró mientras tanto → 409.
    - Oficina, **corregir** (`motivo_correccion`, o sin motivo explícito sobre
      una parada ya registrada): `puede_corregir_cobro`, motivo obligatorio.

    Toda escritura de la oficina exige el formulario vigente
    (`version_formulario`): un formulario viejo no sabe pedir lo que el servidor
    valida hoy. Levanta `PuertaCerrada`.
    """
    from app.models.ruta_despacho import EstadoFinancieroRuta, EstadoRutaDespacho
    from app.services import permisos_liquidacion as _pl
    from app.services.bitacora import MotivoRequerido, motivo_obligatorio
    data = data or {}
    cerrada = ruta.estado == EstadoRutaDespacho.ENTREGADA
    if (ruta.estado_financiero or '') == EstadoFinancieroRuta.LIQUIDADA:
        raise PuertaCerrada('La ruta ya está liquidada: la parada no se puede confirmar ni '
                            'corregir desde acá')
    if ruta.estado not in (EstadoRutaDespacho.EN_TRANSITO, EstadoRutaDespacho.ENTREGADA):
        raise PuertaCerrada(f'La ruta debe estar EN_TRANSITO, está {ruta.estado}')

    if es_conductor:
        if not cerrada:
            return {'modo': CONDUCTOR, 'motivo': None}
        if previa is None:
            motivo = (motivo_tardia if (motivo_tardia or '').strip()
                      else (MOTIVO_COLA_TARDIA if data.get('via_cola') is True else None))
            if motivo:
                return {'modo': CONDUCTOR_COLA_TARDIA, 'motivo': motivo.strip()}
        raise PuertaCerrada('La ruta ya se cerró: esta parada la registra (o la corrige) la '
                            'oficina, con un motivo.')

    quiere_corregir = (motivo_correccion is not None
                       or (previa is not None and motivo_tardia is None))
    if quiere_corregir:
        # Adoptar la versión del conductor antes de que nada salga a Siesa lo
        # hace también quien liquida (decisión del CTO, 2026-09-26).
        from app.services import politica_cobro as _pc_p
        adopta_sin_envio = bool(adopta_version and previa is not None
                                and _pl.puede_liquidar(usuario)
                                and _pc_p.puede_editar_cobro(previa))
        if not (_pl.puede_corregir_cobro(usuario) or adopta_sin_envio):
            raise PuertaCerrada(
                'Esta parada ya está registrada: cambiarla es corregir el cobro, y eso lo '
                'hacen el administrador o el líder de cartera, con un motivo.', status=403)
        if previa is None:
            raise PuertaCerrada('Esta parada no tiene nada registrado todavía: regístrela, no '
                                'hay qué corregir.', status=409)
        try:
            motivo = motivo_obligatorio(motivo_correccion,
                                        'corregir una parada ya registrada')
        except MotivoRequerido as e:
            raise PuertaCerrada(str(e)) from e
        _exigir_formulario_vigente(data)
        return {'modo': OFICINA_CORRIGE, 'motivo': motivo}

    permiso = _pl.puede_registrar_parada_tardia if cerrada else _pl.puede_corregir_cobro
    if not permiso(usuario):
        raise PuertaCerrada(
            ('Con la ruta cerrada, la parada sin gestionar la registra quien liquida '
             '(administrador o liquidador).') if cerrada else
            ('Con la ruta en camino, la parada la confirma el conductor; por él solo la '
             'registran el administrador o el líder de cartera.'), status=403)
    if previa is not None:
        raise PuertaCerrada(
            'Esta parada ya se registró mientras usted llenaba el formulario (el conductor la '
            'confirmó, o la registró otra persona). No se pisó nada: revise lo registrado. Si '
            'hay que cambiarlo, es corregir el cobro (administrador o líder de cartera), con '
            'un motivo.', status=409)
    try:
        motivo = motivo_obligatorio(
            motivo_tardia, 'registrar una parada después del cierre de la ruta' if cerrada
            else 'registrar la parada por el conductor con la ruta en camino')
    except MotivoRequerido as e:
        raise PuertaCerrada(str(e)) from e
    _exigir_formulario_vigente(data)
    return {'modo': OFICINA_REGISTRA, 'motivo': motivo}


def _exigir_formulario_vigente(data: dict) -> None:
    try:
        v = int((data or {}).get('version_formulario') or 0)
    except (TypeError, ValueError):
        v = 0
    if v < version_formulario_actual():
        raise PuertaCerrada(
            'El formulario de la oficina está desactualizado: recargue la pantalla y vuelva '
            'a registrarlo (el servidor exige datos que ese formulario no pide).')


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
        # Lo necesario para ADOPTARLA después (`resolver_version`): el cuerpo
        # tal como lo mandó el teléfono. Las fotos no viajan a las pantallas
        # (`RecaudoEntrega.to_dict` las quita).
        'cuerpo': {k: data.get(k) for k in CAMPOS_DEL_CUERPO if data.get(k) is not None},
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


#: Lo que se guarda del cuerpo del teléfono para poder adoptarlo después.
CAMPOS_DEL_CUERPO = ('estado_entrega', 'forma_pago', 'motivo_rechazo', 'monto_cobrado',
                     'monto_descuento', 'motivo_descuento', 'observaciones',
                     'referencia_pago', 'items_entregados', 'bultos_rechazados',
                     'foto_entrega', 'foto_comprobante', 'geo', 'version_formulario',
                     'ts_dispositivo', 'ts_envio', 'via_cola', 'modo_pantalla')
#: Las decisiones de la oficina sobre la versión del conductor.
ADOPTAR = 'ADOPTAR'
MANTENER = 'MANTENER'


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


def resolver_version(recaudo_id: int, usuario, decision: str, motivo) -> dict:
    """La salida de «el conductor mandó otra versión» (validación e2e,
    2026-09-26; decisión del CTO). **La única** que resuelve la diferencia.

    - `MANTENER`: queda lo de la oficina; la diferencia se da por revisada.
      Quien liquida o quien corrige cobros, con motivo (bitácora EDITAR).
    - `ADOPTAR`: la versión del conductor pasa a ser la parada. Si nada de la
      parada salió a Siesa (`politica_cobro.puede_editar_cobro`: ni RC ni
      retención encolados), la adopta quien liquida; si ya salió, es corregir
      el cobro (admin + líder de cartera) — y lo que ya viajó al ERP no se
      reescribe (la guarda del congelamiento de `confirmar_parada` sigue). Por
      `RutaService.confirmar_parada` como corrección: mismas validaciones,
      FORZAR `parada_confirmada_corregida_por_la_oficina`.

    Levanta `PuertaCerrada` (403/409/400)."""
    from app.extensions import db
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import permisos_liquidacion as _pl
    from app.services import politica_cobro as _pc
    from app.services.bitacora import MotivoRequerido, motivo_obligatorio, registrar_accion
    rec = db.session.get(RecaudoEntrega, recaudo_id)
    if rec is None:
        raise LookupError('Parada no encontrada')
    if not rec.version_conductor or not rec.diferencia_conductor:
        raise PuertaCerrada('No hay una versión del conductor que difiera: nada que resolver.',
                            status=409)
    try:
        texto = motivo_obligatorio(motivo, 'resolver la versión del conductor')
    except MotivoRequerido as e:
        raise PuertaCerrada(str(e)) from e
    decision = (decision or '').upper()
    if decision not in (ADOPTAR, MANTENER):
        raise PuertaCerrada('La decisión es ADOPTAR o MANTENER')
    editable = _pc.puede_editar_cobro(rec)
    if decision == MANTENER or editable:
        permitido = _pl.puede_liquidar(usuario) or _pl.puede_corregir_cobro(usuario)
    else:
        permitido = _pl.puede_corregir_cobro(usuario)
    if not permitido:
        raise PuertaCerrada(
            'Esta parada ya salió a Siesa: adoptar la versión del conductor es corregir el '
            'cobro, y eso lo hacen el administrador o el líder de cartera.' if not editable
            else 'La versión del conductor la resuelve quien liquida la ruta.', status=403)
    uid = getattr(usuario, 'id', None)
    revision = {'decision': decision, 'por': uid, 'en': datetime.utcnow().isoformat(),
                'motivo': texto}
    if decision == MANTENER:
        antes = {'diferencia_conductor': rec.diferencia_conductor}
        rec.version_conductor = {**rec.version_conductor, 'revision': revision}
        rec.diferencia_conductor = False
        registrar_accion('EDITAR', rec, usuario_id=uid, motivo=texto, antes=antes,
                         despues={'diferencia_conductor': False, 'version_conductor': 'MANTENIDA'})
        db.session.commit()
        return {'recaudo': rec.to_dict(), 'decision': decision}
    from app.services.ruta_service import RutaService
    cuerpo = dict((rec.version_conductor or {}).get('cuerpo') or {})
    if not cuerpo.get('estado_entrega'):
        raise PuertaCerrada('La versión del conductor se guardó sin el detalle para adoptarla: '
                            'corríjala a mano con lo que él dice.', status=409)
    cuerpo['version_formulario'] = version_formulario_actual()
    cuerpo.pop('via_cola', None)
    version = dict(rec.version_conductor)
    ruta_id, tarea_id = rec.ruta_id, rec.tarea_id
    RutaService.confirmar_parada(ruta_id, tarea_id, uid, cuerpo, por_oficina=True,
                                 motivo_correccion=f'Adoptó la versión del conductor: {texto}',
                                 adopta_version=True)
    rec = db.session.get(RecaudoEntrega, recaudo_id)
    rec.version_conductor = {**version, 'revision': revision}
    rec.diferencia_conductor = False
    db.session.commit()
    return {'recaudo': rec.to_dict(), 'decision': decision}
