"""
La caja del conductor: qué tenía que entregar, qué entregó y quién responde por
la diferencia (m051liqcaja, 2026-09-27). **Una política, una función por
pregunta.**

## El defecto

El WMS cuadraba la plata consigo mismo y nunca contra la plata física.
«Liquidar» era un clic sin arqueo, y con un faltante quien liquidaba tenía tres
salidas y ninguna correcta: liquidar igual (Siesa registra en caja plata que no
está y el faltante no tiene dueño), «corregir el cobro» a la baja (el faltante
del conductor pasa a ser **deuda del cliente**, que ya pagó) o el ajuste al
peso (pérdida de la empresa). `efectivo_en_poder` daba la plata por entregada
con el clic de liquidar.

**La clase:** *se da por entregada plata que nadie contó.*

## Ahora

Un **acta de entrega de caja** por conductor: lo que declaró haber cobrado en
efectivo en sus rutas entregadas (`esperado_de_entrega`), lo que contó quien lo
recibió, los gastos que pagó con esa plata (se declaran con su foto; quien
recibe los **acepta o rechaza**, nunca se descuentan solos) y la diferencia:

    diferencia = contado + gastos aceptados − efectivo esperado

Negativa = faltante **a cargo del conductor**, visible para gerencia
(`diferencias_por_conductor`). No se descuenta de nómina ni se corrige el cobro
del cliente para tapar nada: el WMS registra, no sanciona. Una diferencia exige
motivo (la base también lo exige). El conductor confirma el acta en su
teléfono, o quien liquida declara que no la confirmó, con motivo.

`RutaService.liquidar_ruta` exige el acta de la ruta (`exigir_acta_para_liquidar`).

## Lo que NO cuenta el acta

Transferencias y consignaciones se verifican contra el banco
(`verificacion_banco`); tarjeta y cheque se listan y no se cuentan acá. El acta
cuenta **efectivo**.

| Pregunta | Función |
|---|---|
| ¿Cuánto efectivo puso esta parada en manos del conductor? | `efectivo_de_recaudo` |
| ¿Qué tiene que entregar este conductor ahora? | `esperado_de_entrega` |
| ¿Quiénes tienen caja por entregar? | `conductores_por_recibir` |
| Registrar / confirmar / declarar sin confirmar / anular | `registrar_acta` · `responder_conductor` · `marcar_sin_confirmar` · `anular_acta` |
| ¿Esta ruta necesita acta para liquidarse? | `exige_acta` / `exigir_acta_para_liquidar` |
| ¿Cuánto efectivo sigue en la calle? | `efectivo_en_poder_por_conductor` |
| ¿Cuánto le faltó a cada conductor? | `diferencias_por_conductor` |
| Lo que la reconciliación ve como verificado | `verificado_de_ruta` |
| El aviso del resumen diario | `lineas_de_aviso` |

Trinquete: `tests/test_caja_conductor.py`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

logger = logging.getLogger(__name__)

#: Los montos son pesos enteros; esto solo absorbe ruido de float.
TOLERANCIA = 0.5

#: El resumen diario avisa un acta sin respuesta del conductor pasado esto.
HORAS_SIN_CONFIRMAR = 24

#: Categorías de flota que se pagan en la calle y pueden salir del recaudo.
ORIGEN_EFECTIVO_CONDUCTOR = 'efectivo_conductor'


class CajaSinActa(ValueError):
    """La ruta tiene plata que nadie contó. El mensaje empieza con
    `caja_sin_acta:` — la pantalla lo reconoce y ofrece recibir la caja."""


def _d(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Lo que se esperaba
# ═════════════════════════════════════════════════════════════════════════════

def efectivo_de_recaudo(recaudo) -> float:
    """El efectivo que esta parada dejó en manos del conductor: lo que declaró
    cobrado, si la forma de pago que vale (`forma_pago_de`) es EFECTIVO. **La
    única** que lo contesta: el acta, el efectivo en poder y la reconciliación
    lo leen de acá."""
    from app.models.recaudo_entrega import forma_pago_de
    if recaudo is None:
        return 0.0
    if (forma_pago_de(recaudo.estado_entrega, recaudo.forma_pago) or '').upper() != 'EFECTIVO':
        return 0.0
    return max(0.0, _d(recaudo.monto_cobrado))


def medio_de_recaudo(recaudo) -> str | None:
    """El grupo del medio de una parada cobrada: EFECTIVO, BANCARIO, TARJETA,
    CHEQUE u OTRO. `None` si no hubo cobro. **La única** que agrupa: el acta,
    la cabecera de Liquidación y el teléfono del conductor la leen de acá."""
    from app.models.recaudo_entrega import forma_pago_de
    from app.services import verificacion_banco as vb
    fp = (forma_pago_de(recaudo.estado_entrega, recaudo.forma_pago) or '').upper()
    if not fp or _d(recaudo.monto_cobrado) <= 0:
        return None
    if fp == 'EFECTIVO':
        return 'EFECTIVO'
    if vb.es_bancaria(fp):
        return 'BANCARIO'
    if fp in ('TARJETA', 'CHEQUE'):
        return fp
    return 'OTRO'


def _conductor(conductor_id):
    from app.extensions import db
    from app.models.conductor import Conductor
    c = db.session.get(Conductor, conductor_id)
    if c is None:
        raise LookupError('Conductor no encontrado')
    return c


def rutas_sin_acta(conductor_id: int, incluir_en_camino: bool = False) -> list:
    """Las rutas del conductor cuya plata no entró a ninguna acta: entregadas
    (y, si se pide, en camino) y sin liquidar. Una ruta liquidada antes del
    acta (sin `entrega_caja_id`) no se vuelve a pedir."""
    from app.models.ruta_despacho import (EstadoFinancieroRuta, EstadoRutaDespacho,
                                          RutaDespacho)
    estados = [EstadoRutaDespacho.ENTREGADA]
    if incluir_en_camino:
        estados.append(EstadoRutaDespacho.EN_TRANSITO)
    return (RutaDespacho.query
            .filter(RutaDespacho.conductor_id == conductor_id,
                    RutaDespacho.estado.in_(estados),
                    RutaDespacho.entrega_caja_id.is_(None),
                    (RutaDespacho.estado_financiero.is_(None))
                    | (RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA))
            .order_by(RutaDespacho.id).all())


def gastos_por_legalizar(conductor) -> list:
    """Los gastos que el conductor pagó con el efectivo del recaudo
    (`flota_gasto.origen_costo = 'efectivo_conductor'`, registrados por su
    usuario) y que ninguna acta vigente decidió. Hasta el 2026-09-27 no tenían
    ningún lector: el tanqueo de $180.000 pagado con la plata de la ruta
    terminaba en un faltante del conductor o en un reembolso fuera del
    sistema."""
    from app.models.entrega_caja import EntregaCajaGasto
    from flota.adaptadores.modelos import Gasto
    if conductor is None or not conductor.usuario_id:
        return []
    ya = {g for (g,) in EntregaCajaGasto.query.with_entities(EntregaCajaGasto.gasto_id).all()}
    return [g for g in (Gasto.query
                        .filter(Gasto.origen_costo == ORIGEN_EFECTIVO_CONDUCTOR,
                                Gasto.registrado_por_usuario_id == conductor.usuario_id)
                        .order_by(Gasto.fecha, Gasto.id).all())
            if g.id not in ya]


def _fotos_de_gasto(gasto_id) -> list:
    from flota.adaptadores.modelos import Foto
    return [f.id for f in Foto.query.filter_by(entidad_tipo='gasto', entidad_id=gasto_id)
            .order_by(Foto.id).all()]


def _gasto_dict(g) -> dict:
    return {'gasto_id': g.id, 'valor': _d(g.valor), 'categoria': g.categoria,
            'fecha': g.fecha.isoformat() if g.fecha else None,
            'proveedor': g.proveedor, 'descripcion': g.descripcion,
            'placa': getattr(getattr(g, 'vehiculo', None), 'placa', None),
            'fotos': _fotos_de_gasto(g.id)}


def esperado_de_entrega(conductor_id: int) -> dict:
    """**Lo que el conductor tiene que entregar ahora**: la foto que el acta
    congela. Una sola función para la pantalla, el acta y el teléfono.

    `efectivo` = suma de `efectivo_de_recaudo` en sus rutas entregadas sin acta.
    Los demás medios se listan (no se cuentan acá). `cobro_menor_que_factura`
    es informativo: paradas donde lo cobrado quedó por debajo de lo que la
    política esperaba (`politica_cobro.esperado_en_caja`); eso lo mide la
    reconciliación, no el acta — un cliente que pagó menos no es un faltante
    del conductor.
    """
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    from app.services import politica_cobro as _pc
    from app.services import verificacion_banco as vb

    c = _conductor(conductor_id)
    rutas = rutas_sin_acta(conductor_id)
    ids = [r.id for r in rutas]
    recaudos = (RecaudoEntrega.query.filter(RecaudoEntrega.ruta_id.in_(ids)).all()
                if ids else [])
    por_ruta = {r.id: {'ruta_id': r.id, 'efectivo': 0.0, 'paradas_efectivo': 0,
                       'fecha': r.fecha_programada.isoformat() if r.fecha_programada else None,
                       'nombre': (r.ruta_maestra.nombre if r.ruta_maestra else r.tipo_ruta)}
                for r in rutas}
    paradas = []
    otros = {'BANCARIO': {'n': 0, 'valor': 0.0, 'por_verificar': 0, 'no_encontradas': 0},
             'TARJETA': {'n': 0, 'valor': 0.0}, 'CHEQUE': {'n': 0, 'valor': 0.0},
             'OTRO': {'n': 0, 'valor': 0.0}}
    menor = []
    for rec in sorted(recaudos, key=lambda x: x.id):
        medio = medio_de_recaudo(rec)
        t = rec.tarea
        ef = efectivo_de_recaudo(rec)
        if ef > 0:
            por_ruta[rec.ruta_id]['efectivo'] += ef
            por_ruta[rec.ruta_id]['paradas_efectivo'] += 1
            paradas.append({'recaudo_id': rec.id, 'ruta_id': rec.ruta_id,
                            'pedido': getattr(t, 'numero_pedido_siesa', None),
                            'cliente': getattr(t, 'cliente', None), 'monto': ef})
        elif medio is not None:
            o = otros[medio]
            o['n'] += 1
            o['valor'] += _d(rec.monto_cobrado)
            if medio == 'BANCARIO':
                e = vb.estado(rec)
                if e == vb.POR_VERIFICAR:
                    o['por_verificar'] += 1
                elif e == vb.NO_ENCONTRADA:
                    o['no_encontradas'] += 1
        if rec.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL) and medio:
            esperado, fuente = _pc.esperado_en_caja(rec, t)
            if fuente != 'DECLARADO' and esperado - (_d(rec.monto_cobrado) + _d(rec.monto_descuento)) > 1:
                menor.append({'recaudo_id': rec.id, 'pedido': getattr(t, 'numero_pedido_siesa', None),
                              'esperado': esperado, 'cobrado': _d(rec.monto_cobrado)})
    gastos = [_gasto_dict(g) for g in gastos_por_legalizar(c)]
    efectivo = round(sum(v['efectivo'] for v in por_ruta.values()), 2)
    for v in por_ruta.values():
        v['efectivo'] = round(v['efectivo'], 2)
    for o in otros.values():
        o['valor'] = round(o['valor'], 2)
    return {
        'conductor_id': c.id,
        'conductor': c.nombre,
        'tiene_usuario': bool(c.usuario_id),
        'rutas': list(por_ruta.values()),
        'efectivo': efectivo,
        'paradas_efectivo': paradas,
        'otros_medios': otros,
        'gastos': gastos,
        'gastos_total': round(sum(g['valor'] for g in gastos), 2),
        'cobro_menor_que_factura': menor,
    }


def conductores_por_recibir() -> list:
    """Quienes tienen caja por entregar: rutas entregadas sin acta con
    efectivo, o gastos del recaudo por legalizar. Lo primero que ve quien
    liquida."""
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho, EstadoFinancieroRuta, EstadoRutaDespacho
    ids = {cid for (cid,) in RutaDespacho.query.with_entities(RutaDespacho.conductor_id).filter(
        RutaDespacho.estado == EstadoRutaDespacho.ENTREGADA,
        RutaDespacho.entrega_caja_id.is_(None),
        (RutaDespacho.estado_financiero.is_(None))
        | (RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA)).distinct().all()}
    out = []
    for c in Conductor.query.filter(Conductor.id.in_(list(ids) or [-1])).all():
        e = esperado_de_entrega(c.id)
        if e['efectivo'] > TOLERANCIA or e['gastos']:
            out.append(e)
    return sorted(out, key=lambda e: (e['conductor'] or ''))


# ═════════════════════════════════════════════════════════════════════════════
# 2 · El acta
# ═════════════════════════════════════════════════════════════════════════════

def _num(valor, que) -> float:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        raise ValueError(f'{que}: escriba un número')
    if v < 0:
        raise ValueError(f'{que} no puede ser negativo')
    return round(v, 2)


def registrar_acta(conductor_id: int, contado_efectivo, usuario_id: int,
                   gastos: list = None, motivo_diferencia: str = None,
                   esperado_visto=None):
    """Quien recibe la caja registra lo que contó. **La única que crea un acta.**

    - `gastos`: `[{gasto_id, aceptado, motivo}]`, uno por cada gasto por
      legalizar (todos decididos: un gasto sin decidir es plata sin dueño).
      Rechazar exige motivo.
    - `esperado_visto`: el efectivo que mostraba la pantalla. Si entre tanto
      entró una parada nueva, no se firma una cifra que ya no es la del
      conductor: se pide recargar.
    - Con diferencia, `motivo_diferencia` es obligatorio. Nunca se ajusta.
    """
    from app.extensions import db
    from app.models.entrega_caja import EntregaCaja, EntregaCajaGasto, EstadoEntregaCaja
    from app.models.ruta_despacho import RutaDespacho
    from app.utils.fecha import dia_operativo
    from app.utils.lock import LOCK_ACTA_CAJA, lock_de_transaccion

    contado = _num(contado_efectivo, 'El efectivo contado')
    lock_de_transaccion(LOCK_ACTA_CAJA)
    esperado = esperado_de_entrega(conductor_id)
    if not esperado['rutas'] and not esperado['gastos']:
        raise ValueError('Este conductor no tiene rutas entregadas ni gastos pendientes de '
                         'entregar: no hay caja que recibir.')
    if esperado_visto is not None and abs(_num(esperado_visto, 'El esperado') -
                                          esperado['efectivo']) > TOLERANCIA:
        raise ValueError(
            f'Lo que el conductor tiene que entregar cambió mientras contaba '
            f'(ahora ${esperado["efectivo"]:,.0f}): vuelva a abrir la caja y cuente otra vez.')

    decididos = {}
    for g in gastos or []:
        try:
            gid = int(g.get('gasto_id'))
        except (TypeError, ValueError, AttributeError):
            raise ValueError('Un gasto viene sin identificador')
        decididos[gid] = g
    pendientes = {g['gasto_id']: g for g in esperado['gastos']}
    sin_decidir = [gid for gid in pendientes if gid not in decididos
                   or decididos[gid].get('aceptado') is None]
    if sin_decidir:
        raise ValueError(f'Hay {len(sin_decidir)} gasto(s) del conductor sin decidir: '
                         'acéptelos o rechácelos con un motivo.')
    ajenos = [gid for gid in decididos if gid not in pendientes]
    if ajenos:
        raise ValueError('Uno de los gastos no es de este conductor o ya se legalizó en otra acta.')
    filas_gasto = []
    aceptados = 0.0
    for gid, g in pendientes.items():
        dec = decididos[gid]
        acepta = bool(dec.get('aceptado'))
        motivo = (dec.get('motivo') or '').strip() or None
        if not acepta and not motivo:
            raise ValueError('Rechazar un gasto del conductor exige un motivo (queda en el acta).')
        if acepta:
            aceptados += g['valor']
        filas_gasto.append((gid, g['valor'], acepta, motivo))

    diferencia = round(contado + aceptados - esperado['efectivo'], 2)
    motivo = (motivo_diferencia or '').strip() or None
    if abs(diferencia) > TOLERANCIA and not motivo:
        tipo = 'faltan' if diferencia < 0 else 'sobran'
        raise ValueError(f'diferencia_sin_motivo: {tipo} ${abs(diferencia):,.0f}. Escriba qué '
                         'dijo el conductor: el acta queda con la diferencia a su cargo.')
    if abs(diferencia) <= TOLERANCIA:
        diferencia = 0.0

    decision = {gid: (acepta, motivo_g) for gid, _v, acepta, motivo_g in filas_gasto}
    detalle = {
        'rutas': esperado['rutas'],
        'paradas_efectivo': esperado['paradas_efectivo'],
        'otros_medios': esperado['otros_medios'],
        'gastos': [{**g, 'aceptado': decision[g['gasto_id']][0],
                    'motivo': decision[g['gasto_id']][1]} for g in esperado['gastos']],
        'cobro_menor_que_factura': esperado['cobro_menor_que_factura'],
    }
    acta = EntregaCaja(
        conductor_id=conductor_id, dia=dia_operativo(),
        estado=EstadoEntregaCaja.PENDIENTE_CONDUCTOR,
        esperado_efectivo=Decimal(str(esperado['efectivo'])),
        contado_efectivo=Decimal(str(contado)),
        gastos_declarados=Decimal(str(esperado['gastos_total'])),
        gastos_aceptados=Decimal(str(round(aceptados, 2))),
        diferencia=Decimal(str(diferencia)),
        motivo_diferencia=motivo if diferencia else (motivo or None),
        detalle=detalle, registrada_por_id=usuario_id, registrada_en=datetime.utcnow())
    db.session.add(acta)
    db.session.flush()
    for r in RutaDespacho.query.filter(
            RutaDespacho.id.in_([x['ruta_id'] for x in esperado['rutas']] or [-1])).all():
        r.entrega_caja_id = acta.id
    for gid, valor, acepta, motivo_g in filas_gasto:
        db.session.add(EntregaCajaGasto(entrega_id=acta.id, gasto_id=gid,
                                        valor=Decimal(str(valor)), aceptado=acepta,
                                        motivo=motivo_g))
    db.session.commit()
    return acta


def _acta(acta_id):
    from app.extensions import db
    from app.models.entrega_caja import EntregaCaja
    a = db.session.get(EntregaCaja, acta_id)
    if a is None:
        raise LookupError('Acta de caja no encontrada')
    return a


def responder_conductor(acta_id: int, usuario_id: int, de_acuerdo: bool,
                        comentario: str = None):
    """El conductor, en su teléfono: está de acuerdo o no, con comentario si
    no. Solo el conductor del acta, y una vez."""
    from app.extensions import db
    from app.models.entrega_caja import EstadoEntregaCaja
    a = _acta(acta_id)
    if a.conductor is None or not a.conductor.usuario_id or a.conductor.usuario_id != usuario_id:
        raise PermissionError('Esta acta no es suya')
    if a.estado != EstadoEntregaCaja.PENDIENTE_CONDUCTOR:
        raise ValueError('Esta acta ya tiene respuesta.')
    comentario = (comentario or '').strip() or None
    if not de_acuerdo and not comentario:
        raise ValueError('Si no está de acuerdo, escriba por qué: queda en el acta.')
    a.estado = EstadoEntregaCaja.CONFIRMADA if de_acuerdo else EstadoEntregaCaja.OBJETADA
    a.conductor_respuesta_en = datetime.utcnow()
    a.conductor_comentario = comentario
    db.session.commit()
    return a


def marcar_sin_confirmar(acta_id: int, usuario_id: int, motivo: str):
    """Quien liquida declara que el conductor no confirmó el acta (se fue, no
    tiene teléfono): con motivo, en la bitácora."""
    from app.extensions import db
    from app.models.entrega_caja import EstadoEntregaCaja
    from app.services.bitacora import foto as foto_fila, motivo_obligatorio, registrar_accion
    a = _acta(acta_id)
    if a.estado != EstadoEntregaCaja.PENDIENTE_CONDUCTOR:
        raise ValueError('Esta acta ya tiene respuesta.')
    motivo = motivo_obligatorio(motivo, 'declarar que el conductor no confirmó el acta')
    antes = foto_fila(a, ['estado'])
    a.estado = EstadoEntregaCaja.SIN_CONFIRMAR
    a.sin_confirmar_por_id = usuario_id
    a.sin_confirmar_en = datetime.utcnow()
    a.sin_confirmar_motivo = motivo
    registrar_accion('EDITAR', a, usuario_id=usuario_id, motivo=motivo,
                     entidad_codigo=f'ACTA-CAJA-{a.id}', antes=antes,
                     despues=foto_fila(a, ['estado', 'sin_confirmar_motivo']))
    db.session.commit()
    return a


def anular_acta(acta_id: int, usuario_id: int, motivo: str):
    """Deja el acta sin efecto (contó mal, entró una parada después): sus rutas
    y sus gastos vuelven a estar por entregar. No se puede con una ruta ya
    liquidada: esa plata ya se dio por recibida."""
    from app.extensions import db
    from app.models.entrega_caja import EntregaCajaGasto, EstadoEntregaCaja
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    from app.services.bitacora import foto as foto_fila, motivo_obligatorio, registrar_accion
    a = _acta(acta_id)
    if a.estado == EstadoEntregaCaja.ANULADA:
        raise ValueError('Esta acta ya está anulada.')
    motivo = motivo_obligatorio(motivo, 'anular un acta de caja')
    rutas = RutaDespacho.query.filter_by(entrega_caja_id=a.id).all()
    liquidadas = [r.id for r in rutas if r.estado_financiero == EstadoFinancieroRuta.LIQUIDADA]
    if liquidadas:
        raise ValueError(f'El acta cubre rutas ya liquidadas ({", ".join(map(str, liquidadas))}): '
                         'no se anula. Registre la diferencia con otra acta o con el administrador.')
    antes = foto_fila(a, ['estado', 'diferencia', 'contado_efectivo'])
    for r in rutas:
        r.entrega_caja_id = None
    for g in EntregaCajaGasto.query.filter_by(entrega_id=a.id).all():
        db.session.delete(g)
    a.estado = EstadoEntregaCaja.ANULADA
    a.anulada_por_id = usuario_id
    a.anulada_en = datetime.utcnow()
    a.anulada_motivo = motivo
    registrar_accion('ANULAR', a, usuario_id=usuario_id, motivo=motivo,
                     entidad_codigo=f'ACTA-CAJA-{a.id}', antes=antes,
                     despues={'estado': a.estado, 'rutas_liberadas': [r.id for r in rutas]})
    db.session.commit()
    return a


# ═════════════════════════════════════════════════════════════════════════════
# 3 · La liquidación exige el acta
# ═════════════════════════════════════════════════════════════════════════════

def efectivo_de_ruta(ruta) -> float:
    from app.models.recaudo_entrega import RecaudoEntrega
    return round(sum(efectivo_de_recaudo(r)
                     for r in RecaudoEntrega.query.filter_by(ruta_id=ruta.id).all()), 2)


def acta_de_ruta(ruta):
    """El acta vigente que recibió la plata de la ruta, o `None`."""
    from app.extensions import db
    from app.models.entrega_caja import EntregaCaja, EstadoEntregaCaja
    aid = getattr(ruta, 'entrega_caja_id', None)
    if not aid:
        return None
    a = db.session.get(EntregaCaja, aid)
    return a if a is not None and a.estado in EstadoEntregaCaja.VIGENTES else None


def exige_acta(ruta) -> bool:
    """¿Hay algo físico que contar? Efectivo cobrado en la ruta, o gastos del
    conductor pagados con el recaudo. Una ruta toda a crédito o por banco no
    pide acta: no hay billetes que contar (las transferencias se verifican en
    el banco)."""
    if efectivo_de_ruta(ruta) > TOLERANCIA:
        return True
    return bool(gastos_por_legalizar(ruta.conductor))


def exigir_acta_para_liquidar(ruta):
    """La puerta de `RutaService.liquidar_ruta`: el acta de la ruta, vigente y
    con el efectivo que la ruta tiene hoy. `CajaSinActa` si falta o si entró
    efectivo después de contarla. Devuelve el acta (o `None` si no hace falta).
    """
    acta = acta_de_ruta(ruta)
    if acta is None:
        if not exige_acta(ruta):
            return None
        e = esperado_de_entrega(ruta.conductor_id)
        raise CajaSinActa(
            f'caja_sin_acta: nadie recibió todavía la caja de '
            f'{e["conductor"] or "el conductor"} (efectivo ${e["efectivo"]:,.0f}'
            f'{", gastos $" + format(e["gastos_total"], ",.0f") if e["gastos"] else ""}). '
            f'Cuente la plata y registre el acta en Liquidación → Caja por recibir; la ruta se '
            f'liquida después.')
    en_acta = next((x['efectivo'] for x in (acta.detalle or {}).get('rutas', [])
                    if x.get('ruta_id') == ruta.id), None)
    ahora = efectivo_de_ruta(ruta)
    if en_acta is not None and abs(ahora - float(en_acta)) > TOLERANCIA:
        raise CajaSinActa(
            f'caja_sin_acta: el efectivo de la ruta cambió después del acta #{acta.id} '
            f'(el acta contó ${float(en_acta):,.0f}, hoy son ${ahora:,.0f}). Anule el acta y '
            f'reciba la caja otra vez.')
    return acta


# ═════════════════════════════════════════════════════════════════════════════
# 4 · Lecturas
# ═════════════════════════════════════════════════════════════════════════════

def efectivo_en_poder_por_conductor(hoy=None) -> list:
    """El efectivo que cada conductor cobró y **todavía no entregó en un acta**.

    Universo: rutas en camino o entregadas, sin acta y sin liquidar. Antes era
    «sin liquidar», y la plata se daba por entregada con el clic de liquidar.
    Antigüedad: días (Bogotá) desde la confirmación en efectivo más vieja.
    """
    from app.extensions import db
    from app.models.conductor import Conductor
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.models.ruta_despacho import EstadoFinancieroRuta, RutaDespacho
    from app.utils.fecha import ahora_bogota, dia_operativo_de

    hoy = hoy or ahora_bogota().date()
    filas = (db.session.query(RecaudoEntrega, RutaDespacho)
             .join(RutaDespacho, RutaDespacho.id == RecaudoEntrega.ruta_id)
             .filter(RutaDespacho.estado.in_(('EN_TRANSITO', 'ENTREGADA')),
                     RutaDespacho.entrega_caja_id.is_(None),
                     (RutaDespacho.estado_financiero.is_(None))
                     | (RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA))
             .all())
    por = {}
    for rec, ruta in filas:
        monto = efectivo_de_recaudo(rec)
        if monto <= 0:
            continue
        g = por.setdefault(ruta.conductor_id, {'efectivo': 0.0, 'paradas': 0,
                                               'rutas': set(), 'desde': None})
        g['efectivo'] += monto
        g['paradas'] += 1
        g['rutas'].add(ruta.id)
        dia = dia_operativo_de(rec.fecha_confirmacion) if rec.fecha_confirmacion else None
        if dia is not None and (g['desde'] is None or dia < g['desde']):
            g['desde'] = dia
    nombres = dict(db.session.query(Conductor.id, Conductor.nombre)
                   .filter(Conductor.id.in_(list(por) or [-1])).all())
    salida = [{
        'conductor_id': cid, 'conductor': nombres.get(cid),
        'efectivo': round(g['efectivo'], 2), 'paradas': g['paradas'],
        'rutas': sorted(g['rutas']),
        'desde': g['desde'].isoformat() if g['desde'] else None,
        'dias': (hoy - g['desde']).days if g['desde'] else None,
    } for cid, g in por.items()]
    return sorted(salida, key=lambda f: (-(f['dias'] if f['dias'] is not None else 10**6),
                                         -f['efectivo']))


def diferencias_por_conductor(desde=None, hasta=None) -> list:
    """**El número de gerencia:** por conductor, cuánto faltó y cuánto sobró en
    sus actas vigentes del rango (por día del acta), cuántas actas, cuántas
    objetó y cuántas no confirmó. El faltante es plata a su cargo; el WMS lo
    registra, no decide la sanción."""
    from app.models.entrega_caja import EntregaCaja, EstadoEntregaCaja
    q = EntregaCaja.query.filter(EntregaCaja.estado.in_(EstadoEntregaCaja.VIGENTES))
    if desde is not None:
        q = q.filter(EntregaCaja.dia >= desde)
    if hasta is not None:
        q = q.filter(EntregaCaja.dia <= hasta)
    por = {}
    for a in q.all():
        g = por.setdefault(a.conductor_id, {
            'conductor_id': a.conductor_id,
            'conductor': a.conductor.nombre if a.conductor else None,
            'actas': 0, 'faltante': 0.0, 'sobrante': 0.0, 'actas_con_diferencia': 0,
            'objetadas': 0, 'sin_confirmar': 0, 'pendientes_conductor': 0})
        g['actas'] += 1
        dif = _d(a.diferencia)
        if dif < -TOLERANCIA:
            g['faltante'] += -dif
        elif dif > TOLERANCIA:
            g['sobrante'] += dif
        if abs(dif) > TOLERANCIA:
            g['actas_con_diferencia'] += 1
        if a.estado == EstadoEntregaCaja.OBJETADA:
            g['objetadas'] += 1
        elif a.estado == EstadoEntregaCaja.SIN_CONFIRMAR:
            g['sin_confirmar'] += 1
        elif a.estado == EstadoEntregaCaja.PENDIENTE_CONDUCTOR:
            g['pendientes_conductor'] += 1
    for g in por.values():
        g['faltante'] = round(g['faltante'], 2)
        g['sobrante'] = round(g['sobrante'], 2)
    return sorted(por.values(), key=lambda g: (-g['faltante'], g['conductor'] or ''))


def actas_por_responder() -> list:
    """Actas que el conductor todavía no contestó: quien liquida puede
    declarar que no confirmó."""
    from app.models.entrega_caja import EntregaCaja, EstadoEntregaCaja
    ahora = datetime.utcnow()
    return [{**a.to_dict(), 'horas': round((ahora - a.registrada_en).total_seconds() / 3600, 1)}
            for a in EntregaCaja.query.filter_by(estado=EstadoEntregaCaja.PENDIENTE_CONDUCTOR)
            .order_by(EntregaCaja.registrada_en).all()]


def actas_del_conductor(usuario_id: int) -> list:
    """Las actas de este usuario-conductor que esperan su respuesta."""
    from app.models.conductor import Conductor
    from app.models.entrega_caja import EntregaCaja, EstadoEntregaCaja
    c = Conductor.query.filter_by(usuario_id=usuario_id).first()
    if c is None:
        return []
    return [a.to_dict() for a in EntregaCaja.query.filter_by(
        conductor_id=c.id, estado=EstadoEntregaCaja.PENDIENTE_CONDUCTOR)
        .order_by(EntregaCaja.id).all()]


def resumen_para_conductor(usuario_id: int) -> dict:
    """Lo que el conductor ve antes de entregar: cuánto efectivo tiene que
    entregar (rutas en camino y entregadas sin acta), qué va por banco, qué
    gastos declaró, y las actas que esperan su respuesta."""
    from app.models.conductor import Conductor
    c = Conductor.query.filter_by(usuario_id=usuario_id).first()
    if c is None:
        return {'conductor': None, 'efectivo': 0.0, 'rutas': [], 'actas': []}
    from app.models.recaudo_entrega import RecaudoEntrega
    from app.services import verificacion_banco as vb
    rutas = rutas_sin_acta(c.id, incluir_en_camino=True)
    ids = [r.id for r in rutas]
    rec = RecaudoEntrega.query.filter(RecaudoEntrega.ruta_id.in_(ids)).all() if ids else []
    efectivo = round(sum(efectivo_de_recaudo(r) for r in rec), 2)
    bancario = [r for r in rec if medio_de_recaudo(r) == 'BANCARIO']
    gastos = [_gasto_dict(g) for g in gastos_por_legalizar(c)]
    return {
        'conductor': c.nombre,
        'efectivo': efectivo,
        'paradas_efectivo': sum(1 for r in rec if efectivo_de_recaudo(r) > 0),
        'rutas': ids,
        'bancario': {'n': len(bancario),
                     'valor': round(sum(_d(r.monto_cobrado) for r in bancario), 2),
                     'por_verificar': sum(1 for r in bancario if vb.estado(r) == vb.POR_VERIFICAR)},
        'gastos': [{k: g[k] for k in ('gasto_id', 'valor', 'categoria', 'fecha')} for g in gastos],
        'gastos_total': round(sum(g['valor'] for g in gastos), 2),
        'actas': actas_del_conductor(usuario_id),
    }


def verificado_de_ruta(ruta) -> dict | None:
    """La cuarta columna de la reconciliación para una ruta: el efectivo que el
    acta recibió **de esta ruta**. `None` sin acta vigente (no se inventa).

    Un acta cubre varias rutas y cuenta el total: su diferencia se reparte
    entre las rutas en proporción a su efectivo esperado. Es un reparto, y se
    declara (`prorrateado`).
    """
    acta = acta_de_ruta(ruta)
    if acta is None:
        return None
    rutas = (acta.detalle or {}).get('rutas') or []
    mia = next((x for x in rutas if x.get('ruta_id') == ruta.id), None)
    if mia is None:
        return None
    total = sum(_d(x.get('efectivo')) for x in rutas)
    parte = (_d(mia.get('efectivo')) / total) if total > 0 else 0.0
    dif = round(_d(acta.diferencia) * parte, 2)
    return {'acta_id': acta.id, 'estado': acta.estado,
            'efectivo_esperado': round(_d(mia.get('efectivo')), 2),
            'efectivo_verificado': round(_d(mia.get('efectivo')) + dif, 2),
            'diferencia': dif, 'prorrateado': len(rutas) > 1}


def lineas_de_aviso(ahora=None) -> list:
    """El resumen diario: actas de ayer y hoy con faltante, y actas que el
    conductor no contestó en más de un día."""
    from app.models.entrega_caja import EntregaCaja, EstadoEntregaCaja
    from app.utils.fecha import dia_operativo
    ahora = ahora or datetime.utcnow()
    hoy = dia_operativo()
    lineas = []
    con_faltante = [a for a in EntregaCaja.query.filter(
        EntregaCaja.estado.in_(EstadoEntregaCaja.VIGENTES),
        EntregaCaja.dia >= hoy - timedelta(days=1)).all()
        if _d(a.diferencia) < -TOLERANCIA]
    if con_faltante:
        det = '; '.join(f"{a.conductor.nombre if a.conductor else '—'} ${-_d(a.diferencia):,.0f}"
                        for a in con_faltante[:10])
        lineas.append(f'🚨 {len(con_faltante)} entrega(s) de caja con faltante a cargo del '
                      f'conductor: {det}. Liquidación → Caja.')
    viejas = [a for a in EntregaCaja.query.filter_by(
        estado=EstadoEntregaCaja.PENDIENTE_CONDUCTOR).all()
        if ahora - a.registrada_en > timedelta(hours=HORAS_SIN_CONFIRMAR)]
    if viejas:
        lineas.append(f'⚠ {len(viejas)} acta(s) de caja sin respuesta del conductor hace más de '
                      f'{HORAS_SIN_CONFIRMAR} h: ' + ', '.join(
                          (a.conductor.nombre if a.conductor else '—') for a in viejas[:10]))
    return lineas


def foto_de_gasto(foto_id: int) -> dict:
    """La foto de un recibo de gasto pagado con el recaudo, como data URL. Solo
    fotos colgadas de un gasto `efectivo_conductor`: quien liquida no ve por
    acá otras fotos de flota."""
    import base64
    from app.extensions import db
    from flota.adaptadores.almacen_fotos import AlmacenLocal, ErrorAlmacen
    from flota.adaptadores.modelos import Foto, Gasto
    f = db.session.get(Foto, foto_id)
    if f is None or f.entidad_tipo != 'gasto':
        raise LookupError('Foto no encontrada')
    g = db.session.get(Gasto, f.entidad_id)
    if g is None or g.origen_costo != ORIGEN_EFECTIVO_CONDUCTOR:
        raise LookupError('Foto no encontrada')
    if f.estado == 'pendiente_evidencia':
        return {'foto': None, 'motivo': 'La foto nunca se guardó'}
    try:
        contenido = AlmacenLocal().leer(f.storage_ref)
    except ErrorAlmacen as e:
        return {'foto': None, 'motivo': str(e)}
    return {'foto': f'data:{f.mime};base64,' + base64.b64encode(contenido).decode('ascii')}
