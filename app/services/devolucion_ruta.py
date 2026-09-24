"""
Si el cliente no paga, la mercancía VUELVE — y vuelve a entrar al inventario.

Regla del dueño (2026-09-24): *«si el cliente no paga, la mercancía vuelve y el
producto tiene que volver a entrar al inventario, sin cabos sueltos»*.

## Lo que había

La `DevolucionCliente` de una parada RECHAZADA/PARCIAL nacía **recién al
liquidar** (`liquidacion_service._crear_devolucion_pendiente`). Entre que el
camión volvía y alguien liquidaba —horas, días— la mercancía no existía en el
WMS: el panel de recepción «BULTOS RECHAZADOS — RE-INGRESAR» era solo
informativo, y lo que el conductor dijo que volvía no estaba en ninguna cola.
Después podía quedar ABIERTA sin plazo con la ruta LIQUIDADA igual, si no volvía
nada había que CANCELARLA (y el faltante desaparecía de la medición), y una
devolución de mostrador sobre la misma factura reingresaba dos veces.

## Lo que hay

    confirmar_parada (RECHAZADO / PARCIAL)
        └─ sincronizar_con_parada ─→ DevolucionCliente EN_CAMION  (sin red)
    recepción «Llegó el camión»
        ├─ escanear_bulto_de_vuelta  → bulto RETORNADO
        └─ cerrar_llegada            → lo que no apareció: FALTANTE;
                                       devoluciones EN_CAMION → ABIERTA
    recepción cuenta (DevolucionClienteService.confirmar_entrada_fisica)
        ├─ vincular_a_factura (con red) → líneas amarradas a f470_rowid
        ├─ > 0 → CONFIRMADA: zona DEVOLUCION (no vendible) + NC
        └─ = 0 → FALTANTE_TOTAL: medido, sin NC, el RC sale por lo cobrado
    cron devolucion_nc_verificador (f350_ind_estado = 1)
        └─ NC aprobada → liberar_reingreso → picking

**Una función crea devoluciones de ruta: `_crear_de_ruta`**, y la llaman
`sincronizar_con_parada` (al confirmar la parada) y `asegurar_devolucion` (la
liquidación y el cierre forzado, para las paradas confirmadas antes de este
cambio). Trinquetes AST en `tests/test_devolucion_vuelve.py`.
"""
import logging
from datetime import datetime, timedelta

from app.extensions import db
from app.models.bulto import Bulto, EstadoBulto
from app.models.devolucion_cliente import (DevolucionCliente, EstadoDevolucionCliente as E,
                                           LineaDevolucionCliente)
from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega

logger = logging.getLogger(__name__)

#: El formulario del conductor que sabe declarar qué volvió en una PARCIAL. Con
#: `version_formulario >= 4` el servidor EXIGE `items_entregados` con al menos
#: una referencia devuelta. Un ítem viejo de la cola offline no se traba (se
#: quedaría en el teléfono para siempre): su devolución nace «sin
#: declaración» y recepción la cuenta contra la factura — una señal, no un 400.
VERSION_FORMULARIO_DEVOLUCION = 4

#: Los estados de parada que devuelven mercancía.
ESTADOS_QUE_DEVUELVEN = (EstadoEntrega.RECHAZADO, EstadoEntrega.PARCIAL)

#: Avisos (horas / días). Constantes y no variables: son umbrales de un aviso,
#: no de una decisión, y cambiarlos no mueve plata.
HORAS_SIN_CONTAR_AVISO = 24
HORAS_SIN_CONTAR_URGENTE = 48
DIAS_NC_SIN_APROBAR = 3
HORAS_RC_ESPERANDO_NC = 48

MOTIVO_AVERIADA = 'MERCANCIA_AVERIADA'


# ═════════════════════════════════════════════════════════════════════════════
# Lectura
# ═════════════════════════════════════════════════════════════════════════════

def devolucion_vigente(recaudo_id):
    """La devolución NO cancelada de este recaudo (el índice único
    `uq_devolucion_por_recaudo` garantiza que hay a lo sumo una)."""
    if not recaudo_id:
        return None
    return (DevolucionCliente.query
            .filter(DevolucionCliente.recaudo_entrega_id == recaudo_id,
                    DevolucionCliente.estado != E.CANCELADA)
            .first())


def tuvo_devolucion_cancelada(recaudo_id) -> bool:
    return DevolucionCliente.query.filter_by(
        recaudo_entrega_id=recaudo_id, estado=E.CANCELADA).first() is not None


def formulario_exige_items(data: dict) -> bool:
    try:
        return int((data or {}).get('version_formulario') or 0) >= VERSION_FORMULARIO_DEVOLUCION
    except (TypeError, ValueError):
        return False


def _devueltos_por_codigo(items) -> dict:
    """`{codigo: cantidad devuelta}` de un `items_entregados`. Sin defaults
    inventados: un ítem sin `cantidad_devuelta` no dice nada."""
    out = {}
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        cant = it.get('cantidad_devuelta')
        if cant is None:
            continue
        try:
            cant = float(cant)
        except (TypeError, ValueError):
            continue
        if cant > 0:
            out[str(it.get('codigo') or '')] = out.get(str(it.get('codigo') or ''), 0.0) + cant
    return out


# ═════════════════════════════════════════════════════════════════════════════
# La frontera: confirmar la parada
# ═════════════════════════════════════════════════════════════════════════════

def validar_parcial_del_conductor(estado_entrega: str, data: dict) -> None:
    """Una PARCIAL dice qué volvió. Solo al formulario que lo sabe decir.

    Antes una PARCIAL sin ítems llegaba a la liquidación y se convertía en
    devolución TOTAL (`es_total = not items_devueltos`): la factura entera a la
    nota crédito por una entrega que el conductor declaró parcial.
    """
    if estado_entrega != EstadoEntrega.PARCIAL or not formulario_exige_items(data):
        return
    items = data.get('items_entregados') or []
    devueltas = 0
    for it in items:
        if not isinstance(it, dict):
            raise ValueError('Cada referencia de una entrega parcial viene con lo pedido y '
                             'lo entregado')
        pedido, entregado = it.get('cantidad_pedida'), it.get('cantidad_entregada')
        if pedido is None or entregado is None:
            raise ValueError(
                f'La referencia {it.get("codigo") or "?"} no trae cuánto se pidió y cuánto '
                f'se entregó: una entrega parcial dice qué volvió, referencia por referencia')
        try:
            if int(pedido) - int(entregado) > 0:
                devueltas += 1
        except (TypeError, ValueError):
            raise ValueError(f'Cantidades inválidas en la referencia {it.get("codigo") or "?"}')
    if not devueltas:
        raise ValueError(
            'Una entrega parcial dice qué volvió: bajá la cantidad entregada de al menos una '
            'referencia. Si no volvió nada, es Entregado.')


def verificar_reconfirmacion(recaudo_previo, nuevo_estado: str, data: dict,
                             bultos_tarea) -> None:
    """Re-confirmar una parada cuya devolución YA se recibió en bodega no puede
    cambiar lo que volvió.

    Lo que recepción contó (o los bultos que escaneó) es el dato físico; si la
    parada lo contradijera después, la nota crédito, el inventario y la
    liquidación hablarían de mercancías distintas. Observaciones y foto sí se
    editan (igual que el congelamiento del monto con el RC ya enviado).
    """
    if recaudo_previo is None:
        return
    dev = devolucion_vigente(recaudo_previo.id)
    contada = dev is not None and dev.estado in E.CONTADAS
    recibidos = [b for b in (bultos_tarea or [])
                 if b.estado in (EstadoBulto.RETORNADO, EstadoBulto.FALTANTE)]
    if not contada and not recibidos:
        return
    cambia = nuevo_estado != recaudo_previo.estado_entrega
    if not cambia and nuevo_estado == EstadoEntrega.PARCIAL:
        nuevos = data.get('items_entregados') or []
        nuevos_dev = {}
        for it in nuevos:
            if not isinstance(it, dict) or it.get('cantidad_pedida') is None \
                    or it.get('cantidad_entregada') is None:
                continue
            d = int(it.get('cantidad_pedida')) - int(it.get('cantidad_entregada'))
            if d > 0:
                nuevos_dev[str(it.get('codigo') or '')] = float(d)
        if nuevos and nuevos_dev != _devueltos_por_codigo(recaudo_previo.items_entregados):
            cambia = True
    if cambia:
        que = (f'la devolución {dev.codigo} ya se contó en bodega ({dev.estado})' if contada
               else f'{len(recibidos)} bulto(s) ya se recibieron en bodega')
        raise ValueError(
            f'No se puede cambiar lo que pasó en esta parada: {que}. Lo físico ya está '
            f'medido; las observaciones y la foto sí se pueden editar.')


def sincronizar_con_parada(recaudo: RecaudoEntrega, usuario_id=None) -> dict:
    """La devolución de la parada, al día con lo que el conductor declaró.

    **Sin red**: corre dentro de `confirmar_parada`, que tiene que funcionar
    offline (la cola del teléfono sincroniza después de las 8 p. m., cuando
    Siesa ya no opera — Regla 14). Las líneas salen de lo empacado
    (`ItemPacking`) y de lo declarado; amarrarlas a la factura es
    `vincular_a_factura`, después.

    - RECHAZADO / PARCIAL sin devolución → la crea EN_CAMION.
    - Con una activa (sin contar) → la rehace con la declaración nueva (un
      dedazo del conductor se corrige), bitácora EDITAR.
    - ENTREGADO / ENTREGADO_SIN_PAGO con una activa → la cancela el sistema
      (la mercancía ya no vuelve), bitácora CANCELAR con el motivo.
    - Una ya contada no se toca (`verificar_reconfirmacion` lo impide antes).

    Idempotente por recaudo. No hace commit.
    """
    from app.services.bitacora import foto, registrar_accion
    tarea = recaudo.tarea
    vigente = devolucion_vigente(recaudo.id)

    if recaudo.estado_entrega in ESTADOS_QUE_DEVUELVEN:
        if tarea is None:
            raise ValueError(f'Recaudo {recaudo.id} sin tarea: no se puede armar su devolución')
        if vigente is None:
            dev = _crear_de_ruta(recaudo, tarea)
            return {'accion': 'creada', 'devolucion': dev}
        if vigente.estado in E.CONTADAS:
            return {'accion': 'contada', 'devolucion': vigente}
        lineas, declaracion = _declaracion(recaudo, tarea)
        if _sin_tiempo(vigente.declaracion_conductor) == _sin_tiempo(declaracion):
            return {'accion': 'sin_cambio', 'devolucion': vigente}
        antes = {'declaracion_conductor': vigente.declaracion_conductor,
                 'lineas': len(vigente.lineas), 'es_total': vigente.es_total}
        _rehacer_lineas(vigente, lineas)
        vigente.declaracion_conductor = declaracion
        vigente.es_total = recaudo.estado_entrega == EstadoEntrega.RECHAZADO
        vigente.vinculada_factura_at = None
        vigente.problema_factura = None
        registrar_accion('EDITAR', vigente, usuario_id=usuario_id,
                         entidad_codigo=vigente.codigo,
                         motivo='El conductor reconfirmó la parada: cambió lo que vuelve',
                         antes=antes,
                         despues={'declaracion_conductor': declaracion,
                                  'lineas': len(vigente.lineas),
                                  'es_total': vigente.es_total})
        return {'accion': 'actualizada', 'devolucion': vigente}

    if vigente is not None and vigente.estado in E.ACTIVAS:
        from app.services.devolucion_cliente_service import cambiar_estado
        antes = foto(vigente, ['estado', 'observaciones'])
        cambiar_estado(vigente, E.CANCELADA)
        motivo = (f'La parada se reconfirmó como {recaudo.estado_entrega}: la mercancía '
                  f'no vuelve')
        vigente.observaciones = motivo
        registrar_accion('CANCELAR', vigente, usuario_id=usuario_id, motivo=motivo,
                         antes=antes, despues=foto(vigente, ['estado', 'observaciones']))
        return {'accion': 'cancelada', 'devolucion': vigente}
    return {'accion': 'ninguna', 'devolucion': vigente}


def asegurar_devolucion(recaudo: RecaudoEntrega, usuario_id=None) -> tuple:
    """`(devolucion, creada)` — la vigente de un RECHAZADO/PARCIAL, o la crea.

    Para las paradas confirmadas ANTES de que la devolución naciera en la
    parada (o por un camino que no pasó por `confirmar_parada`): la liquidación
    y el cierre forzado la ENCUENTRAN; solo si no existe la crean, con la misma
    función. Una devolución cancelada no se resucita: la canceló alguien con
    motivo. No hace commit.
    """
    if recaudo.estado_entrega not in ESTADOS_QUE_DEVUELVEN:
        return None, False
    vigente = devolucion_vigente(recaudo.id)
    if vigente is not None:
        return vigente, False
    if tuvo_devolucion_cancelada(recaudo.id):
        return None, False
    if recaudo.tarea is None:
        raise ValueError(f'Recaudo {recaudo.id} sin tarea: no se puede armar su devolución')
    return _crear_de_ruta(recaudo, recaudo.tarea), True


def _crear_de_ruta(recaudo: RecaudoEntrega, tarea) -> DevolucionCliente:
    """**La única función que crea una devolución de ruta.** EN_CAMION, con lo
    declarado. Trinquete AST: nadie más pasa `recaudo_entrega_id` a
    `crear_devolucion`."""
    from app.services.devolucion_cliente_service import DevolucionClienteService
    lineas, declaracion = _declaracion(recaudo, tarea)
    es_total = recaudo.estado_entrega == EstadoEntrega.RECHAZADO
    ruta_txt = f'WMS Ruta #{recaudo.ruta_id}'
    return DevolucionClienteService.crear_devolucion(
        tarea_packing_id=tarea.id,
        tipo_docto_fe=tarea.fe_tipo or '',
        consec_fe=tarea.fe_consec or '',
        almacen_id=tarea.almacen_id,
        recepcionista_id=None,
        lineas=lineas,
        es_total=es_total,
        observaciones=f'{ruta_txt} | {recaudo.estado_entrega}'
                      + (f' | {recaudo.motivo_rechazo}' if recaudo.motivo_rechazo else ''),
        recaudo_entrega_id=recaudo.id,
        commit=False,
        estado_inicial=E.EN_CAMION,
        declaracion_conductor=declaracion,
    )


def _sin_tiempo(declaracion):
    if not isinstance(declaracion, dict):
        return declaracion
    return {k: v for k, v in declaracion.items() if k not in ('registrada_en',)}


def _rehacer_lineas(devolucion: DevolucionCliente, lineas: list) -> None:
    from app.services.devolucion_cliente_service import (DevolucionClienteService,
                                                        sincronizar_rowid_activo)
    for ln in list(devolucion.lineas):
        devolucion.lineas.remove(ln)
    db.session.flush()
    for l in lineas:
        DevolucionClienteService._agregar_linea(devolucion, l)
    sincronizar_rowid_activo(devolucion)


def _enviado_por_producto(tarea) -> dict:
    """`{producto_id: (producto, cantidad empacada)}` — lo que salió en el camión."""
    from app.models.packing import ItemPacking
    out = {}
    for i in ItemPacking.query.filter_by(tarea_id=tarea.id).all():
        p = i.producto
        if p is None:
            continue
        cant = float(i.cantidad_real or i.cantidad_esperada or 0)
        prev = out.get(p.id)
        out[p.id] = (p, (prev[1] if prev else 0.0) + cant)
    return out


def _declaracion(recaudo: RecaudoEntrega, tarea) -> tuple:
    """`(lineas, declaracion)` de la devolución, a partir de lo declarado.

    RECHAZADO: todo lo empacado vuelve (una línea por producto; si el motivo
    es MERCANCIA_AVERIADA, premarcada averiada — recepción la corrige).
    PARCIAL: lo que el conductor bajó en cada referencia. Si el líder corrigió
    la cantidad en la liquidación, `cantidad_devuelta` es la corregida y lo
    declarado sigue siendo del conductor (`cantidad_devuelta_conductor`).
    PARCIAL sin ítems (formulario viejo): sin líneas; recepción cuenta contra
    la factura (`sin_items`). Nada se inventa.
    """
    enviado = _enviado_por_producto(tarea)
    motivo = (recaudo.motivo_rechazo or '').strip().upper() or None
    declaracion = {
        'estado': recaudo.estado_entrega,
        'motivo': motivo,
        'recaudo_id': recaudo.id,
        'registrada_en': datetime.utcnow().isoformat(),
    }
    lineas = []
    if recaudo.estado_entrega == EstadoEntrega.RECHAZADO:
        averiada = motivo == MOTIVO_AVERIADA
        declaracion['total'] = True
        items = []
        for pid, (p, cant) in sorted(enviado.items()):
            if cant <= 0:
                continue
            lineas.append({
                'producto_id': pid, 'codigo_siesa': p.codigo_siesa or '',
                'cantidad_facturada': cant, 'cantidad_devuelta': cant,
                'cantidad_declarada': cant,
                'cantidad_averiada': cant if averiada else 0.0,
                'es_averiado': averiada,
            })
            items.append({'codigo': p.codigo, 'producto_id': pid, 'cantidad_devuelta': cant})
        declaracion['items'] = items
        return lineas, declaracion

    items_raw = recaudo.items_entregados or []
    if not items_raw:
        declaracion['sin_items'] = True
        declaracion['items'] = []
        return [], declaracion

    from app.models.producto import Producto
    por_codigo = {p.codigo: (p, cant) for (p, cant) in enviado.values()}
    items, sin_producto = [], []
    for it in items_raw:
        if not isinstance(it, dict):
            continue
        codigo = str(it.get('codigo') or '')
        dev = it.get('cantidad_devuelta')
        dev = float(dev) if dev is not None else 0.0
        decl = it['cantidad_devuelta_conductor'] if 'cantidad_devuelta_conductor' in it else dev
        decl = float(decl) if decl is not None else None
        if dev <= 0 and not decl:
            continue
        p_cant = por_codigo.get(codigo)
        p = p_cant[0] if p_cant else Producto.query.filter_by(codigo=codigo).first()
        if p is None:
            sin_producto.append(codigo)
            continue
        pedida = it.get('cantidad_pedida')
        facturada = float(pedida) if pedida is not None else (p_cant[1] if p_cant else dev)
        lineas.append({
            'producto_id': p.id, 'codigo_siesa': p.codigo_siesa or '',
            'cantidad_facturada': max(facturada, dev),
            'cantidad_devuelta': dev, 'cantidad_declarada': decl,
            'cantidad_averiada': 0.0,
            # Si el declarante sabe la línea de factura, manda (hoy el PWA del
            # conductor no la manda; la política es la de `_construir_lineas_nc`).
            'f470_rowid': (str(it['f470_rowid']).strip() if it.get('f470_rowid') else None),
        })
        items.append({'codigo': codigo, 'producto_id': p.id, 'cantidad_devuelta': decl,
                      'cantidad_pedida': pedida})
    declaracion['items'] = items
    if sin_producto:
        declaracion['sin_producto'] = sin_producto
    return lineas, declaracion


# ═════════════════════════════════════════════════════════════════════════════
# Amarrar a la factura (con red)
# ═════════════════════════════════════════════════════════════════════════════

def vincular_a_factura(devolucion: DevolucionCliente, gateway=None, forzar: bool = False) -> dict:
    """Amarra las líneas a las filas reales de la factura (`f470_rowid`).

    Con red: la llaman recepción al abrir/contar, la liquidación y el cierre de
    llegada. Idempotente. **No levanta por datos**: lo que impide contarla
    queda escrito en `problema_factura` (y la cuenta lo muestra y se niega);
    solo levanta si Siesa misma falla (red), y ahí no escribe nada.

    - RECHAZADO (total): una línea por fila de la factura, con lo facturado
      menos lo que ya volvió en otra devolución. La doble unidad no es ambigua
      acá: volvió todo.
    - PARCIAL: cada línea declarada se amarra a su fila. Si el producto tiene
      DOS filas (doble unidad, PQ + UND) y el conductor declaró por referencia,
      **no se reparte**: se abre una línea por fila en 0 y recepción cuenta
      cuál volvió. Lo declarado por referencia queda en
      `declaracion_conductor['declarado_por_producto']` para medir el faltante.
    - PARCIAL sin declaración: una línea por fila en 0; recepción cuenta.
    - Una referencia de la factura sin producto WMS que vuelve → problema
      bloqueante, visible («sincronizá el catálogo»). Antes se omitía con un
      WARNING y la devolución salía corta.
    """
    if devolucion.estado not in E.ACTIVAS:
        return {'ok': devolucion.vinculada_factura_at is not None, 'motivo': 'no_activa'}
    if devolucion.vinculada_factura_at is not None and not forzar:
        return {'ok': True, 'ya_vinculada': True}
    from app.services.devolucion_cliente_service import (DevolucionClienteService,
                                                        sincronizar_rowid_activo,
                                                        ya_devuelto_de_la_factura,
                                                        _ya_devuelto_de_linea)
    from app.services.fe_resolver import FENoEncontrada, resolver_fe
    from app.models.producto import Producto
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    tarea = devolucion.tarea_packing
    tipo, consec = devolucion.tipo_docto_fe, devolucion.consec_fe
    if not tipo or not consec:
        try:
            tipo, consec = resolver_fe(tarea, gateway=gateway)
        except FENoEncontrada as e:
            devolucion.problema_factura = (
                f'No se localizó la factura del pedido {devolucion.numero_pedido_siesa}: {e}. '
                f'Sin factura no se sabe qué líneas puede cruzar la nota crédito.')
            return {'ok': False, 'problema': devolucion.problema_factura}
    filas_fe = gateway.get_rowids_factura(tipo, consec)
    if not filas_fe:
        devolucion.problema_factura = (
            f'La factura {tipo}-{consec} no trae líneas en Siesa: no se puede contar '
            f'contra ella todavía.')
        return {'ok': False, 'problema': devolucion.problema_factura}
    devolucion.tipo_docto_fe, devolucion.consec_fe = str(tipo), str(consec)

    filas, sin_producto = [], []
    for row in filas_fe:
        ref = (row.get('f120_referencia') or '').strip()
        if not ref:
            continue
        p = Producto.query.filter_by(codigo_siesa=ref).first()
        cant = float(row.get('f470_cant_base') or 0)
        neto = row.get('f470_vlr_neto')
        fila = {'producto': p, 'ref': ref, 'rowid': str(row.get('f470_rowid') or '').strip() or None,
                'cant': cant, 'uom': (row.get('f470_id_unidad_medida') or '').strip(),
                'bodega': (row.get('f150_id') or '').strip(),
                'unitario': (float(neto) / cant if (neto is not None and cant) else None)}
        if p is None:
            sin_producto.append(ref)
            continue
        filas.append(fila)
    ya = ya_devuelto_de_la_factura(tipo, consec, excluir_id=devolucion.id)
    decl = dict(devolucion.declaracion_conductor or {})
    total = bool(decl.get('total')) or devolucion.es_total
    sin_items = bool(decl.get('sin_items'))
    averiada = (decl.get('motivo') or '') == MOTIVO_AVERIADA
    problemas, avisos = [], []

    def _base(f):
        return {'producto_id': f['producto'].id, 'codigo_siesa': f['ref'],
                'cantidad_facturada': f['cant'], 'f470_rowid': f['rowid'],
                'f470_id_unidad_medida': f['uom'], 'f150_id_bodega': f['bodega']}

    if total or sin_items:
        if sin_producto:
            problemas.append(
                f'La factura {tipo}-{consec} trae {", ".join(sorted(set(sin_producto)))} que no '
                f'existe(n) en el catálogo del WMS: no se puede recibir ni contar. Sincronizá el '
                f'catálogo y volvé a abrir la devolución.')
        nuevas = []
        for f in filas:
            restante = max(0.0, f['cant'] - _ya_devuelto_de_linea(ya, f['rowid'], f['ref']))
            if total:
                l = {**_base(f), 'cantidad_devuelta': restante, 'cantidad_declarada': restante,
                     'cantidad_averiada': restante if averiada else 0.0, 'es_averiado': averiada}
            else:
                l = {**_base(f), 'cantidad_devuelta': 0.0, 'cantidad_declarada': None,
                     'cantidad_averiada': 0.0}
            l['_unitario'] = f['unitario']
            nuevas.append(l)
        _rehacer_con_unitario(devolucion, nuevas)
    else:
        por_producto = {}
        for f in filas:
            por_producto.setdefault(f['producto'].id, []).append(f)
        lineas_por_producto = {}
        for ln in list(devolucion.lineas):
            lineas_por_producto.setdefault(ln.producto_id, []).append(ln)
        declarado_por_producto = {}
        nuevas = []

        def _dec(ln):
            return (float(ln.cantidad_declarada) if ln.cantidad_declarada is not None
                    else None)

        def _amarrada(ln, f, dev, decl):
            restante = max(0.0, f['cant'] - _ya_devuelto_de_linea(ya, f['rowid'], f['ref']))
            dev = min(float(dev or 0), restante)
            return {**_base(f), 'cantidad_devuelta': dev, 'cantidad_declarada': decl,
                    'cantidad_averiada': min(ln.averiadas(), dev) if ln is not None else 0.0,
                    '_unitario': f['unitario']}

        for pid, lns in lineas_por_producto.items():
            rows = por_producto.get(pid, [])
            rowids = {f['rowid']: f for f in rows if f['rowid']}
            # Lo declarado CON rowid manda: la correspondencia es exacta.
            con_rowid = [ln for ln in lns if ln.f470_rowid and str(ln.f470_rowid) in rowids]
            sin_rowid = [ln for ln in lns if ln not in con_rowid]
            usadas = set()
            for ln in con_rowid:
                f = rowids[str(ln.f470_rowid)]
                usadas.add(f['rowid'])
                nuevas.append(_amarrada(ln, f, ln.cantidad_devuelta, _dec(ln)))
            restantes = [f for f in rows if f['rowid'] not in usadas]
            if not sin_rowid:
                continue
            if not restantes:
                for ln in sin_rowid:
                    avisos.append(f'{ln.codigo_siesa or ln.producto_id}: el conductor lo '
                                  f'declaró y no está en la factura {tipo}-{consec} — '
                                  f'contalo en 0')
                    nuevas.append({'producto_id': ln.producto_id,
                                   'codigo_siesa': ln.codigo_siesa,
                                   'cantidad_facturada': float(ln.cantidad_facturada or 0),
                                   'cantidad_devuelta': float(ln.cantidad_devuelta or 0),
                                   'cantidad_declarada': _dec(ln),
                                   'cantidad_averiada': ln.averiadas(), 'f470_rowid': None,
                                   '_unitario': None})
                continue
            if len(restantes) == 1:
                # Una sola línea de factura para ese producto: todo lo declarado
                # sin rowid le corresponde a ella (dos declaraciones se suman —
                # no hay a qué otra línea atribuirlas).
                f = restantes[0]
                dev = sum(float(ln.cantidad_devuelta or 0) for ln in sin_rowid)
                decs = [_dec(ln) for ln in sin_rowid]
                decl_total = None if any(d is None for d in decs) else sum(decs)
                nuevas.append(_amarrada(sin_rowid[0], f, dev, decl_total))
                continue
            # Doble unidad (PQ + UND) declarada por referencia: NO se reparte
            # (adivinar cruza cartera de más, Regla 21). Una línea por fila en
            # 0 y recepción cuenta cuál volvió; lo declarado queda por producto.
            decs = [_dec(ln) if _dec(ln) is not None else float(ln.cantidad_devuelta or 0)
                    for ln in sin_rowid]
            declarado_por_producto[str(pid)] = sum(decs)
            avisos.append(f'{sin_rowid[0].codigo_siesa}: va en {len(restantes)} líneas de la '
                          f'factura (doble unidad) — contá cuánto volvió de cada una')
            for f in restantes:
                nuevas.append({**_base(f), 'cantidad_devuelta': 0.0,
                               'cantidad_declarada': None, 'cantidad_averiada': 0.0,
                               '_unitario': f['unitario']})
        if declarado_por_producto:
            decl['declarado_por_producto'] = declarado_por_producto
        _rehacer_con_unitario(devolucion, nuevas)

    for ln in devolucion.lineas:
        if not ln.f470_rowid:
            continue
        ocupada = DevolucionClienteService._devolucion_activa_de_la_linea(
            ln.f470_rowid, excluir_id=devolucion.id)
        if ocupada is not None:
            problemas.append(
                f'{ln.codigo_siesa}: esa línea de la factura ya está en la devolución '
                f'{ocupada.codigo} ({ocupada.estado}) sin contar. Una línea de factura, una '
                f'devolución activa: contá o cancelá la otra primero.')
    if avisos:
        decl['avisos_vinculacion'] = avisos
    if devolucion.declaracion_conductor is not None or decl:
        # Una devolución armada por el flujo viejo (sin declaración) sigue sin
        # ella: `None` es la marca con la que la cuenta congela lo declarado.
        devolucion.declaracion_conductor = decl
    devolucion.problema_factura = ' · '.join(problemas) or None
    if problemas:
        # Sin el índice activo: la otra devolución lo tiene y chocaría.
        for ln in devolucion.lineas:
            ln.f470_rowid_activo = None
        devolucion.vinculada_factura_at = None
    else:
        sincronizar_rowid_activo(devolucion)
        devolucion.vinculada_factura_at = datetime.utcnow()
    db.session.flush()
    return {'ok': not problemas, 'problema': devolucion.problema_factura, 'avisos': avisos}


def _rehacer_con_unitario(devolucion, nuevas: list) -> None:
    from app.services.devolucion_cliente_service import DevolucionClienteService
    for ln in list(devolucion.lineas):
        devolucion.lineas.remove(ln)
    db.session.flush()
    for l in nuevas:
        unitario = l.pop('_unitario', None)
        linea = DevolucionClienteService._agregar_linea(devolucion, l)
        linea.valor_unitario = unitario


# ═════════════════════════════════════════════════════════════════════════════
# Recepción: «Llegó el camión»
# ═════════════════════════════════════════════════════════════════════════════

def _recaudos_de_ruta(ruta_id: int) -> list:
    return RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all()


def _devoluciones_de_ruta(ruta_id: int, estados=None) -> list:
    q = (DevolucionCliente.query
         .join(RecaudoEntrega, RecaudoEntrega.id == DevolucionCliente.recaudo_entrega_id)
         .filter(RecaudoEntrega.ruta_id == ruta_id))
    if estados:
        q = q.filter(DevolucionCliente.estado.in_(estados))
    return q.order_by(DevolucionCliente.id).all()


def cola_llegadas() -> list:
    """Una fila por ruta con algo por recibir o contar.

    «Algo» es: bultos que el conductor declaró de vuelta y nadie escaneó
    (RECHAZADO), o devoluciones de ruta sin contar. La lista se vacía sola:
    nada que dependa de que alguien se acuerde.
    """
    from app.models.ruta_despacho import RutaDespacho
    rutas_bultos = {r for (r,) in db.session.query(Bulto.ruta_despacho_id)
                    .filter(Bulto.estado == EstadoBulto.RECHAZADO,
                            Bulto.ruta_despacho_id.isnot(None)).distinct().all()}
    rutas_dev = {r for (r,) in db.session.query(RecaudoEntrega.ruta_id)
                 .join(DevolucionCliente, DevolucionCliente.recaudo_entrega_id == RecaudoEntrega.id)
                 .filter(DevolucionCliente.estado.in_(E.ACTIVAS)).distinct().all()}
    ahora = datetime.utcnow()
    out = []
    for ruta_id in sorted(rutas_bultos | rutas_dev):
        ruta = db.session.get(RutaDespacho, ruta_id)
        if ruta is None:
            continue
        devs = _devoluciones_de_ruta(ruta_id, E.ACTIVAS)
        mas_vieja = min((d.fecha_creacion for d in devs if d.fecha_creacion), default=None)
        out.append({
            'ruta_id': ruta_id,
            'conductor': ruta.conductor.nombre if getattr(ruta, 'conductor', None) else None,
            'placa': ruta.vehiculo.placa if getattr(ruta, 'vehiculo', None) else None,
            'estado_ruta': ruta.estado,
            'estado_financiero': ruta.estado_financiero,
            'llegada_cerrada_at': (ruta.llegada_cerrada_at.isoformat()
                                   if ruta.llegada_cerrada_at else None),
            'cuadre': cuadre_de_bultos(ruta_id),
            'horas_sin_contar': (round((ahora - mas_vieja).total_seconds() / 3600, 1)
                                 if mas_vieja else None),
            'devoluciones': [_resumen_devolucion(d) for d in devs],
        })
    out.sort(key=lambda r: -(r['horas_sin_contar'] or 0))
    return out


def _resumen_devolucion(d: DevolucionCliente) -> dict:
    return {
        'id': d.id, 'codigo': d.codigo, 'estado': d.estado,
        'pedido': d.numero_pedido_siesa, 'cliente': d.cliente, 'es_total': d.es_total,
        'lineas': len(d.lineas), 'problema_factura': d.problema_factura,
        'motivo': (d.declaracion_conductor or {}).get('motivo'),
        'sin_declaracion': bool((d.declaracion_conductor or {}).get('sin_items')),
        'fecha_creacion': d.fecha_creacion.isoformat() if d.fecha_creacion else None,
    }


def escanear_bulto_de_vuelta(ruta_id: int, codigo_barras: str, usuario_id: int) -> dict:
    """Recepción escanea un bulto que volvió: RETORNADO.

    Un bulto declarado de vuelta (RECHAZADO) o dado por faltante que después
    aparece → RETORNADO. Uno ENTREGADO de una parada PARCIAL también se acepta
    (el conductor devuelve la mercancía en una caja que no marcó) y se declara
    `no_declarado`. Uno ENTREGADO de una parada entregada entera, no: el dato
    dice que el cliente lo tiene, y recibirlo sin corregir la parada dejaría la
    factura cobrada con la mercancía en bodega.
    """
    codigo = (codigo_barras or '').strip()
    if not codigo:
        raise ValueError('Escaneá o escribí el código del bulto')
    b = Bulto.query.filter_by(codigo_barras=codigo).with_for_update().first()
    if b is None:
        raise LookupError(f'No existe el bulto {codigo}')
    if b.ruta_despacho_id != ruta_id:
        raise ValueError(f'El bulto {codigo} es de la ruta #{b.ruta_despacho_id}, no de ésta')
    if b.estado == EstadoBulto.RETORNADO:
        return {'bulto': b.to_dict(), 'ya_estaba': True, 'cuadre': cuadre_de_bultos(ruta_id)}
    no_declarado = False
    if b.estado == EstadoBulto.ENTREGADO:
        rec = RecaudoEntrega.query.filter_by(ruta_id=ruta_id, tarea_id=b.tarea_id).first()
        if rec is None or rec.estado_entrega != EstadoEntrega.PARCIAL:
            raise ValueError(
                f'El conductor declaró el bulto {codigo} entregado'
                + (f' ({rec.estado_entrega})' if rec else '')
                + '. Si volvió, hay que corregir la parada antes de recibirlo.')
        no_declarado = True
    elif b.estado not in (EstadoBulto.RECHAZADO, EstadoBulto.FALTANTE):
        raise ValueError(f'La parada del bulto {codigo} no está confirmada todavía '
                         f'(estado {b.estado})')
    b.estado = EstadoBulto.RETORNADO
    b.fecha_retorno = datetime.utcnow()
    b.retorno_por_id = usuario_id
    db.session.commit()
    return {'bulto': b.to_dict(), 'no_declarado': no_declarado,
            'cuadre': cuadre_de_bultos(ruta_id)}


def cerrar_llegada(ruta_id: int, usuario_id: int) -> dict:
    """Recepción da por recibido el camión.

    Lo que el conductor dijo que volvía y no se escaneó queda FALTANTE — medido,
    no borrado. Las devoluciones EN_CAMION pasan a ABIERTA (en bodega, por
    contar), y se intenta amarrarlas a la factura (con red; si Siesa no
    responde, se hace al contar).
    """
    from app.models.ruta_despacho import EstadoRutaDespacho, RutaDespacho
    from app.services.devolucion_cliente_service import cambiar_estado
    ruta = db.session.get(RutaDespacho, ruta_id)
    if ruta is None:
        raise LookupError('Ruta no encontrada')
    if ruta.estado not in (EstadoRutaDespacho.EN_TRANSITO, EstadoRutaDespacho.ENTREGADA):
        raise ValueError(f'La ruta está {ruta.estado}: no hay camión que recibir')
    ahora = datetime.utcnow()
    faltantes = 0
    for b in (Bulto.query.filter_by(ruta_despacho_id=ruta_id, estado=EstadoBulto.RECHAZADO)
              .with_for_update().all()):
        b.estado = EstadoBulto.FALTANTE
        b.fecha_retorno = ahora
        b.retorno_por_id = usuario_id
        faltantes += 1
    abiertas = 0
    for d in _devoluciones_de_ruta(ruta_id, (E.EN_CAMION,)):
        cambiar_estado(d, E.ABIERTA)
        d.fecha_llegada = ahora
        abiertas += 1
    ruta.llegada_cerrada_at = ahora
    ruta.llegada_cerrada_por_id = usuario_id
    db.session.commit()
    vinculadas = 0
    for d in _devoluciones_de_ruta(ruta_id, E.ACTIVAS):
        try:
            if vincular_a_factura(d).get('ok'):
                vinculadas += 1
            db.session.commit()
        except Exception as e:  # Siesa caído: se vincula al contar
            db.session.rollback()
            logger.warning('[DEVOLUCION_RUTA] no se pudo vincular %s a su factura: %s',
                           d.codigo, e)
    return {'ok': True, 'bultos_faltantes': faltantes, 'devoluciones_en_bodega': abiertas,
            'vinculadas': vinculadas, 'cuadre': cuadre_de_bultos(ruta_id)}


def cuadre_de_bultos(ruta_id: int) -> dict:
    """salieron = entregados + retornados + faltantes, **exacto**.

    `exacto` solo cuando no queda nada colgando: ni bultos en el camión
    (RECHAZADO sin recibir) ni paradas sin confirmar. Hasta ahí el cuadre se
    muestra con lo que falta, no se da por bueno.
    """
    bultos = Bulto.query.filter_by(ruta_despacho_id=ruta_id).all()
    c = {}
    for b in bultos:
        c[b.estado] = c.get(b.estado, 0) + 1
    salieron = len(bultos)
    entregados = c.get(EstadoBulto.ENTREGADO, 0)
    retornados = c.get(EstadoBulto.RETORNADO, 0)
    faltantes = c.get(EstadoBulto.FALTANTE, 0)
    en_camion = c.get(EstadoBulto.RECHAZADO, 0)
    sin_confirmar = salieron - entregados - retornados - faltantes - en_camion
    return {'salieron': salieron, 'entregados': entregados, 'retornados': retornados,
            'faltantes': faltantes, 'en_camion_sin_recibir': en_camion,
            'sin_confirmar': sin_confirmar,
            'exacto': (en_camion == 0 and sin_confirmar == 0
                       and salieron == entregados + retornados + faltantes)}


# ═════════════════════════════════════════════════════════════════════════════
# Liquidación y DLQ
# ═════════════════════════════════════════════════════════════════════════════

def pendientes_de_conteo(ruta_id: int) -> list:
    """Las paradas RECHAZADAS/PARCIALES de la ruta cuya mercancía nadie contó.

    Sin devolución (parada vieja) o con una activa. Una cancelada no: la
    canceló alguien con motivo, y eso queda en la bitácora.
    """
    out = []
    for r in RecaudoEntrega.query.filter_by(ruta_id=ruta_id).filter(
            RecaudoEntrega.estado_entrega.in_(ESTADOS_QUE_DEVUELVEN)).all():
        d = devolucion_vigente(r.id)
        if d is None and tuvo_devolucion_cancelada(r.id):
            continue
        if d is None or d.estado in E.ACTIVAS:
            out.append({'recaudo_id': r.id, 'tarea_id': r.tarea_id,
                        'pedido': r.tarea.numero_pedido_siesa if r.tarea else None,
                        'estado_entrega': r.estado_entrega,
                        'devolucion': d.codigo if d else None,
                        'estado_devolucion': d.estado if d else None})
    return out


def nc_no_llegara(recaudo) -> bool:
    """¿La NC de la que depende el RC de este recaudo ya no va a salir?

    Sí si su devolución terminó sin nota crédito: contada en cero
    (FALTANTE_TOTAL) o cancelada. Entonces el recibo de caja sale por lo
    cobrado —sin romper NC → RC → DC, que ya no tiene NC que esperar— y la
    factura queda con el saldo de lo que no volvió, en cartera y a la vista.
    Antes el RC quedaba esperando para siempre.
    """
    if recaudo is None or recaudo.siesa_nc_triggered:
        return False
    d = devolucion_vigente(recaudo.id)
    if d is None:
        return tuvo_devolucion_cancelada(recaudo.id)
    return d.estado == E.FALTANTE_TOTAL


def destrabar_rc_de(devolucion: DevolucionCliente) -> int:
    """El RC que esperaba la NC de esta devolución se reprograma YA (no en 30
    min): su dependencia se resolvió sin NC. Nunca levanta."""
    try:
        if not devolucion.recaudo_entrega_id:
            return 0
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob
        n = 0
        for job in SiesaJob.query.filter(
                SiesaJob.tipo == 'RECIBO_CAJA',
                SiesaJob.referencia_tipo == 'RecaudoEntrega',
                SiesaJob.referencia_id == devolucion.recaudo_entrega_id,
                SiesaJob.estado == EstadoSiesaJob.PENDIENTE).all():
            job.proximo_intento = datetime.utcnow()
            n += 1
        if n:
            db.session.commit()
        return n
    except Exception as e:
        db.session.rollback()
        logger.warning('[DEVOLUCION_RUTA] no se pudo reprogramar el RC de %s: %s',
                       devolucion.codigo, e)
        return 0


# ═════════════════════════════════════════════════════════════════════════════
# Avisos y medición
# ═════════════════════════════════════════════════════════════════════════════

def _momento_del_rechazo(d: DevolucionCliente):
    rec = d.recaudo_entrega
    if rec is not None and rec.fecha_confirmacion:
        return rec.fecha_confirmacion
    return d.fecha_creacion


def avisos(ahora: datetime = None) -> dict:
    """Lo que lleva demasiado esperando. Para el tablero y el resumen diario.

    - devolución de ruta sin contar ≥ 24 h (aviso) / ≥ 48 h (urgente);
    - NC sin aprobar > 3 días, y NC ANULADA en Siesa (la devolución ya
      reingresó y la cartera no se cruzó);
    - RC esperando su NC > 48 h.
    """
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    ahora = ahora or datetime.utcnow()
    sin_contar, urgentes = [], []
    for d in (DevolucionCliente.query
              .filter(DevolucionCliente.recaudo_entrega_id.isnot(None),
                      DevolucionCliente.estado.in_(E.ACTIVAS)).all()):
        t0 = _momento_del_rechazo(d)
        if t0 is None:
            continue
        horas = (ahora - t0).total_seconds() / 3600
        if horas >= HORAS_SIN_CONTAR_AVISO:
            fila = {**_resumen_devolucion(d), 'horas': round(horas, 1)}
            (urgentes if horas >= HORAS_SIN_CONTAR_URGENTE else sin_contar).append(fila)
    nc_viejas, nc_anuladas = [], []
    for d in DevolucionCliente.query.filter_by(siesa_nc_triggered=True,
                                               nc_aprobada_siesa=False).all():
        if d.nc_estado_siesa == 2:
            nc_anuladas.append({'id': d.id, 'codigo': d.codigo, 'nc': d.siesa_nc_consec,
                                'cliente': d.cliente})
            continue
        if d.siesa_nc_triggered_at and (ahora - d.siesa_nc_triggered_at) > timedelta(
                days=DIAS_NC_SIN_APROBAR):
            nc_viejas.append({'id': d.id, 'codigo': d.codigo, 'nc': d.siesa_nc_consec,
                              'cliente': d.cliente,
                              'dias': round((ahora - d.siesa_nc_triggered_at).total_seconds()
                                            / 86400, 1)})
    rc_esperando = []
    limite = ahora - timedelta(hours=HORAS_RC_ESPERANDO_NC)
    for job in SiesaJob.query.filter(SiesaJob.tipo == 'RECIBO_CAJA',
                                     SiesaJob.estado == EstadoSiesaJob.PENDIENTE,
                                     SiesaJob.fecha_creacion <= limite).all():
        try:
            import json as _json
            payload = _json.loads(job.payload or '{}')
        except (TypeError, ValueError):
            payload = {}
        if not payload.get('depende_de_nc'):
            continue
        rec = db.session.get(RecaudoEntrega, payload.get('recaudo_id') or job.referencia_id)
        if rec is None or rec.siesa_nc_triggered:
            continue
        rc_esperando.append({'job_id': job.id, 'recaudo_id': rec.id,
                             'horas': round((ahora - job.fecha_creacion).total_seconds() / 3600, 1)})
    return {
        'sin_contar_24h': sin_contar,
        'sin_contar_48h': urgentes,
        'nc_sin_aprobar_3d': nc_viejas,
        'nc_anuladas': nc_anuladas,
        'rc_esperando_nc_48h': rc_esperando,
        'total': (len(sin_contar) + len(urgentes) + len(nc_viejas) + len(nc_anuladas)
                  + len(rc_esperando)),
    }


def lineas_de_aviso(a: dict = None) -> list:
    """El mismo `avisos()` en frases, para el resumen diario por correo."""
    a = a if a is not None else avisos()
    out = []
    if a['sin_contar_48h']:
        out.append(f'🚨 {len(a["sin_contar_48h"])} devolución(es) de ruta sin contar hace más '
                   f'de {HORAS_SIN_CONTAR_URGENTE} h')
    if a['sin_contar_24h']:
        out.append(f'⚠ {len(a["sin_contar_24h"])} devolución(es) de ruta sin contar hace más '
                   f'de {HORAS_SIN_CONTAR_AVISO} h')
    if a['nc_sin_aprobar_3d']:
        out.append(f'⚠ {len(a["nc_sin_aprobar_3d"])} nota(s) crédito de devolución sin aprobar '
                   f'en Siesa hace más de {DIAS_NC_SIN_APROBAR} días')
    if a['nc_anuladas']:
        out.append(f'🚨 {len(a["nc_anuladas"])} nota(s) crédito de devolución ANULADA(S) en Siesa')
    if a['rc_esperando_nc_48h']:
        out.append(f'⚠ {len(a["rc_esperando_nc_48h"])} recibo(s) de caja esperando su nota '
                   f'crédito hace más de {HORAS_RC_ESPERANDO_NC} h')
    return out


def _percentil(valores: list, p: float):
    if not valores:
        return None
    v = sorted(valores)
    k = (len(v) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def faltante_valorizado(d: DevolucionCliente) -> dict:
    """Declarado − contado de una devolución contada, en unidades y en plata.

    Por línea donde hay declarado; por PRODUCTO donde el conductor declaró por
    referencia y la factura lo partió (doble unidad) — ahí la plata no se
    valoriza: PQ y UND valen distinto y no se sabe cuál faltó (Regla 0).
    """
    unidades, valor, sin_valor = 0.0, 0.0, 0
    decl_prod = ((d.declaracion_conductor or {}).get('declarado_por_producto') or {})
    for l in d.lineas:
        if l.cantidad_declarada is None:
            continue
        dif = float(l.cantidad_declarada) - float(l.cantidad_devuelta or 0)
        if dif <= 0:
            continue
        unidades += dif
        if l.valor_unitario is not None:
            valor += dif * float(l.valor_unitario)
        else:
            sin_valor += 1
    for pid, dec in decl_prod.items():
        contado = sum(float(l.cantidad_devuelta or 0) for l in d.lineas
                      if str(l.producto_id) == str(pid))
        dif = float(dec or 0) - contado
        if dif > 0:
            unidades += dif
            sin_valor += 1
    return {'unidades': round(unidades, 4), 'valor': round(valor, 2),
            'lineas_sin_valor': sin_valor}


def medicion(desde: datetime = None, hasta: datetime = None) -> dict:
    """Cuánto tarda la mercancía en volver a ser inventario, y cuánto no vuelve.

    - horas rechazo → conteo (mediana y p90) de las devoluciones de ruta
      contadas;
    - horas conteo → aprobación de la NC (mediana y p90), separando la
      verificada por Siesa de la marcada a mano;
    - faltante de retorno valorizado (declarado − contado × valor de la línea
      de factura);
    - rutas con el cuadre de bultos exacto / no exacto.

    Sin fechas: los últimos 30 días (por `fecha_creacion`).
    """
    from app.models.ruta_despacho import RutaDespacho
    hasta = hasta or datetime.utcnow()
    desde = desde or (hasta - timedelta(days=30))
    devs = (DevolucionCliente.query
            .filter(DevolucionCliente.recaudo_entrega_id.isnot(None),
                    DevolucionCliente.fecha_creacion >= desde,
                    DevolucionCliente.fecha_creacion <= hasta).all())
    h_conteo, h_aprob, h_aprob_siesa = [], [], []
    falt_u, falt_v, falt_sin_valor, contadas, faltante_total = 0.0, 0.0, 0, 0, 0
    por_estado = {}
    for d in devs:
        por_estado[d.estado] = por_estado.get(d.estado, 0) + 1
        if d.estado in E.CONTADAS:
            contadas += 1
            t0 = _momento_del_rechazo(d)
            if t0 and d.fecha_confirmacion:
                h_conteo.append((d.fecha_confirmacion - t0).total_seconds() / 3600)
            f = faltante_valorizado(d)
            falt_u += f['unidades']
            falt_v += f['valor']
            falt_sin_valor += f['lineas_sin_valor']
            if d.estado == E.FALTANTE_TOTAL:
                faltante_total += 1
        if d.nc_aprobada_siesa and d.nc_aprobada_siesa_at and d.fecha_confirmacion:
            h = (d.nc_aprobada_siesa_at - d.fecha_confirmacion).total_seconds() / 3600
            h_aprob.append(h)
            if d.nc_aprobada_fuente == 'SIESA':
                h_aprob_siesa.append(h)
    rutas_ids = {d.recaudo_entrega.ruta_id for d in devs if d.recaudo_entrega is not None}
    cuadres = [dict(cuadre_de_bultos(r), ruta_id=r) for r in sorted(rutas_ids)
               if db.session.get(RutaDespacho, r) is not None]
    return {
        'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
        'devoluciones_de_ruta': len(devs), 'por_estado': por_estado,
        'contadas': contadas, 'faltante_total': faltante_total,
        'horas_rechazo_a_conteo': {'n': len(h_conteo), 'mediana': _percentil(h_conteo, 0.5),
                                   'p90': _percentil(h_conteo, 0.9)},
        'horas_conteo_a_aprobacion': {'n': len(h_aprob), 'mediana': _percentil(h_aprob, 0.5),
                                      'p90': _percentil(h_aprob, 0.9),
                                      'verificadas_por_siesa': len(h_aprob_siesa)},
        'faltante_de_retorno': {'unidades': round(falt_u, 4), 'valor': round(falt_v, 2),
                                'lineas_sin_valor': falt_sin_valor,
                                'nota': ('valor = (declarado − contado) × valor neto unitario de '
                                         'la línea de factura; las líneas sin valor (doble '
                                         'unidad, sin vincular) se cuentan aparte, no en cero')},
        'cuadre_de_bultos': {
            'rutas': len(cuadres),
            'exactas': sum(1 for c in cuadres if c['exacto']),
            'no_exactas': [c for c in cuadres if not c['exacto']][:50],
        },
    }
