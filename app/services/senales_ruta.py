"""
Señales de fuga en ruta — **una política, una función por pregunta.**

Lo que el conductor cobra y lo que declara en la calle es la parte del ciclo
que nadie más ve. Este módulo contesta, para cada excepción, *qué evidencia hace
falta* y *qué número la vuelve visible*. No sanciona a nadie: cada señal es un
dato para la cola del encargado (regla 2 de `flota/CLAUDE.md` — el sistema
propone, un humano decide). Por eso ninguna función de acá bloquea una entrega
ni cambia un estado: `ruta_service` y `liquidacion_service` las consultan.

| Pregunta | Función |
|---|---|
| ¿Esta forma de pago exige comprobante? | `requiere_comprobante` |
| ¿Qué referencia se guarda? | `limpiar_referencia` |
| ¿Este motivo exige foto y GPS? | `exige_evidencia` / `geo_fue_intentado` |
| ¿Qué hora dijo el teléfono y cuánto se desfasa? | `leer_ts` / `desfase_s` / `clasificar_hora` |
| ¿El rechazo se capturó lejos del cliente? | `senal_rechazo_lejos` |
| ¿Cuánto efectivo tiene cada conductor sin liquidar? | `efectivo_en_poder_por_conductor` |
| ¿Volvió lo que el conductor dijo que volvía? | `faltante_de_retorno` |
| ¿Qué señales tiene esta parada? | `senales_de_recaudo` |
| ¿El vehículo está en condición de salir? | `advertencias_de_flota` |

## Sin agregar pasos a la parada normal

Una entrega en efectivo, completa, no pide nada nuevo. La evidencia se pide
**solo en las excepciones**, que es donde está la plata: pago bancario
(comprobante) y «no pagó y se quedó con la mercancía» (foto + GPS).

## El formulario viejo en caché

La cola offline puede traer, días después del despliegue, confirmaciones
armadas por un PWA viejo que no sabía pedir la evidencia. Rechazarlas dejaría
esa entrega trabada en el teléfono para siempre (la cola no tiene cómo
corregir un ítem). Por eso la exigencia se aplica a los payloads que declaran
`version_formulario >= VERSION_FORMULARIO_CON_EVIDENCIA`, y lo que llega sin
evidencia de un cliente viejo **no se rechaza pero queda como señal**
(`sin_comprobante`, `sin_evidencia`): Regla 0, declarado, nunca silencioso.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

#: El PWA que pide comprobante y evidencia manda esto. Un payload sin el campo
#: es un formulario anterior (caché del service worker o cola vieja).
VERSION_FORMULARIO_CON_EVIDENCIA = 2


def formulario_con_evidencia(data: dict) -> bool:
    try:
        return int((data or {}).get('version_formulario') or 0) >= VERSION_FORMULARIO_CON_EVIDENCIA
    except (TypeError, ValueError):
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 1 · Comprobante de pago bancario
# ═════════════════════════════════════════════════════════════════════════════

#: Mínimo de caracteres de la referencia. «Los últimos dígitos» del
#: comprobante: con menos de 4 no se distingue de otra transferencia del día.
MIN_REFERENCIA = 4
#: `F358_REFERENCIA_OTROS` es Alfanumérico 30 en el DOCX del 142888.
MAX_REFERENCIA = 30


def requiere_comprobante(forma_pago) -> bool:
    """¿La plata de esta parada entró por un canal que deja comprobante?

    Transferencias (cualquier banco), consignación, tarjeta (voucher del
    datáfono) y cheque (número del cheque). **No**: efectivo (se cuenta en la
    liquidación), crédito y exento (no hay plata en la puerta).

    Es la frontera exacta de la fuga: una transferencia falsa no se detecta
    contando billetes, y sin referencia nadie puede cruzarla contra el extracto.
    """
    fp = (forma_pago or '').strip().upper()
    if not fp:
        return False
    return (fp.startswith('TRANSFERENCIA') or fp in ('CONSIGNACION', 'TARJETA', 'CHEQUE'))


def limpiar_referencia(ref) -> Optional[str]:
    """La referencia normalizada, o `None` si no alcanza a identificar nada.

    Se conservan letras, dígitos y guiones; se recortan a `MAX_REFERENCIA`
    desde la DERECHA (los últimos dígitos son los que identifican).
    """
    if ref is None:
        return None
    limpio = ''.join(ch for ch in str(ref).strip().upper() if ch.isalnum() or ch == '-')
    if sum(1 for ch in limpio if ch.isalnum()) < MIN_REFERENCIA:
        return None
    return limpio[-MAX_REFERENCIA:]


# ═════════════════════════════════════════════════════════════════════════════
# 2 · «No pagó y se quedó con la mercancía»
# ═════════════════════════════════════════════════════════════════════════════

def exige_evidencia(motivo_rechazo) -> bool:
    """Los motivos que dejan mercancía en la calle exigen foto y GPS.

    Se lee de `motivos_rechazo.SIN_RETORNO` y no de un literal: si mañana hay
    un segundo motivo sin retorno, exige lo mismo sin que nadie lo recuerde.
    """
    from app.services import motivos_rechazo as _mr
    return (motivo_rechazo or '').strip().upper() in _mr.SIN_RETORNO


def geo_fue_intentado(geo) -> bool:
    """¿El conductor tocó «Estoy aquí»?

    GPS **intentado**, no GPS obtenido: un teléfono sin señal o sin permiso no
    puede entregar una coordenada, y trabar la parada en la calle por eso no la
    desbloquea nadie. Lo que no se acepta es no haberlo intentado
    (`no_se_pidio`) o no haber mandado nada.
    """
    from app.services import geo_cliente as _geo
    if not isinstance(geo, dict):
        return False
    if geo.get('lat') is not None and geo.get('lon') is not None:
        return True
    return geo.get('motivo') not in (None, '', _geo.NO_SE_PIDIO)


# ═════════════════════════════════════════════════════════════════════════════
# 3 · La hora del teléfono
# ═════════════════════════════════════════════════════════════════════════════

def leer_ts(valor) -> Optional[datetime]:
    """Un instante del teléfono → `datetime` UTC naive, o `None`.

    Acepta ISO-8601 (con o sin zona; sin zona se asume UTC, que es lo que manda
    `Date.toISOString()`) y epoch en milisegundos (`Date.now()`). Un valor
    ilegible es `None`, no «ahora»: rellenar con la hora del servidor borraría
    justo la diferencia que esta columna existe para medir.
    """
    if valor is None or isinstance(valor, bool) or valor == '':
        return None
    try:
        if isinstance(valor, (int, float)):
            return datetime.fromtimestamp(float(valor) / 1000.0, tz=timezone.utc).replace(tzinfo=None)
        texto = str(valor).strip()
        if texto.endswith('Z'):
            texto = texto[:-1] + '+00:00'
        dt = datetime.fromisoformat(texto)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, OverflowError, OSError):
        logger.warning('[SENALES] hora del dispositivo ilegible: %r', valor)
        return None


def desfase_s(ts_envio_dispositivo, ahora_servidor: datetime) -> Optional[int]:
    """Teléfono − servidor, en segundos, medido en el mismo instante (el envío).

    Positivo = el teléfono va adelantado. Se mide al ENVIAR y no al confirmar:
    una confirmación que esperó tres horas en la cola no tiene un reloj
    desfasado tres horas — tiene tres horas sin señal.
    """
    if ts_envio_dispositivo is None or ahora_servidor is None:
        return None
    return int(round((ts_envio_dispositivo - ahora_servidor).total_seconds()))


def umbral_desfase_s() -> int:
    """A partir de cuántos segundos el reloj del teléfono se declara desfasado.

    300 s por defecto: la red del teléfono sincroniza al segundo, y un reloj
    que se va más de cinco minutos es un reloj tocado a mano o sin red hace
    días. Configurable (`SENAL_DESFASE_RELOJ_S`) porque es un umbral, no un
    hecho.
    """
    try:
        return max(1, int(os.environ.get('SENAL_DESFASE_RELOJ_S', '300')))
    except ValueError:
        return 300


#: Más de esto entre la hora del teléfono (corregida) y la llegada al
#: servidor = la parada se confirmó sin señal. Informativo, no sospechoso.
DIFERIDA_S = 30 * 60


def clasificar_hora(ts_dispositivo, ts_desfase, fecha_servidor, salida_ruta=None) -> dict:
    """Qué dice la hora del teléfono de esta parada.

    `estado`:
    · `sin_dato`         el cliente no mandó la hora (formulario viejo).
    · `reloj_desfasado`  |desfase| > umbral: la hora del teléfono no sirve
                         para ubicar el evento.
    · `antes_de_salir`   la hora del evento (corregida por el desfase) es
                         anterior a la salida del camión: una entrega
                         confirmada antes de salir del CD.
    · `diferida`         llegó más de 30 min después de ocurrir (sin señal).
    · `coherente`        nada que decir.
    """
    if ts_dispositivo is None:
        return {'estado': 'sin_dato', 'desfase_s': ts_desfase, 'retraso_s': None}
    evento = ts_dispositivo - timedelta(seconds=ts_desfase or 0)
    retraso = (int((fecha_servidor - evento).total_seconds())
               if fecha_servidor is not None else None)
    if ts_desfase is not None and abs(ts_desfase) > umbral_desfase_s():
        estado = 'reloj_desfasado'
    elif salida_ruta is not None and evento < salida_ruta - timedelta(seconds=umbral_desfase_s()):
        estado = 'antes_de_salir'
    elif retraso is not None and retraso > DIFERIDA_S:
        estado = 'diferida'
    else:
        estado = 'coherente'
    return {'estado': estado, 'desfase_s': ts_desfase, 'retraso_s': retraso}


# ═════════════════════════════════════════════════════════════════════════════
# 4 · El rechazo capturado lejos del cliente
# ═════════════════════════════════════════════════════════════════════════════

#: Motivos que AFIRMAN que el conductor estuvo en la puerta. «Dirección
#: errada» no: dice justamente que no la encontró, y la distancia no la juzga.
MOTIVOS_AFIRMAN_PRESENCIA = ('CLIENTE_CERRADO', 'FUERA_DE_HORARIO')


def umbral_rechazo_lejos_m() -> float:
    """Desde cuántos metros un «cliente cerrado» se declara lejos del cliente.

    Por defecto el mismo radio con que `geo_cliente` decide que dos capturas
    son «la misma tienda» (`RADIO_COHERENCIA_M`): una sola definición de
    «acá». Configurable con `SENAL_RECHAZO_LEJOS_M`.
    """
    from app.services import geo_cliente as _geo
    try:
        return float(os.environ.get('SENAL_RECHAZO_LEJOS_M', _geo.RADIO_COHERENCIA_M))
    except ValueError:
        return float(_geo.RADIO_COHERENCIA_M)


def distancia_al_maestro(geo, maestro) -> Optional[float]:
    """Metros entre la captura del payload y el punto del maestro, o `None`.

    `None` si no hubo coordenada o el cliente no tiene punto: **sin punto del
    cliente no hay señal** — no se compara contra el municipio ni contra nada
    inventado.
    """
    from app.services import geo_cliente as _geo
    captura = _geo.leer_captura_del_conductor(geo)
    if captura is None or captura.lat is None or maestro is None or maestro.lat is None:
        return None
    return round(_geo.distancia_m(captura.lat, captura.lon,
                                  float(maestro.lat), float(maestro.lon)), 1)


def senal_rechazo_lejos(recaudo) -> Optional[dict]:
    """La señal, si el motivo afirma presencia y la captura quedó lejos."""
    if (recaudo.motivo_rechazo or '') not in MOTIVOS_AFIRMAN_PRESENCIA:
        return None
    if recaudo.distancia_cliente_m is None:
        return None
    d = float(recaudo.distancia_cliente_m)
    umbral = umbral_rechazo_lejos_m()
    if d <= umbral:
        return None
    return {'distancia_m': d, 'umbral_m': umbral}


# ═════════════════════════════════════════════════════════════════════════════
# 5 · Efectivo en poder de cada conductor
# ═════════════════════════════════════════════════════════════════════════════

def efectivo_en_poder_por_conductor(hoy=None) -> list:
    """Lo que cada conductor cobró en EFECTIVO en rutas que nadie liquidó.

    Universo: rutas `EN_TRANSITO` o `ENTREGADA` con `estado_financiero` ≠
    `LIQUIDADA` — el camión todavía en la calle también lleva plata. Antigüedad:
    días (Bogotá) desde la confirmación de efectivo más vieja sin liquidar.

    Ordenado por antigüedad y después por monto: la plata que lleva más días
    fuera es la que más cuesta recuperar.
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
                     RutaDespacho.estado_financiero != EstadoFinancieroRuta.LIQUIDADA,
                     RecaudoEntrega.forma_pago == 'EFECTIVO')
             .all())
    por = {}
    for rec, ruta in filas:
        monto = float(rec.monto_cobrado or 0)
        if monto <= 0:
            continue
        g = por.setdefault(ruta.conductor_id, {
            'conductor_id': ruta.conductor_id, 'efectivo': 0.0, 'paradas': 0,
            'rutas': set(), 'desde': None})
        g['efectivo'] += monto
        g['paradas'] += 1
        g['rutas'].add(ruta.id)
        dia = dia_operativo_de(rec.fecha_confirmacion) if rec.fecha_confirmacion else None
        if dia is not None and (g['desde'] is None or dia < g['desde']):
            g['desde'] = dia
    nombres = dict(db.session.query(Conductor.id, Conductor.nombre)
                   .filter(Conductor.id.in_(list(por) or [-1])).all())
    salida = []
    for cid, g in por.items():
        salida.append({
            'conductor_id': cid,
            'conductor': nombres.get(cid),
            'efectivo': round(g['efectivo'], 2),
            'paradas': g['paradas'],
            'rutas': sorted(g['rutas']),
            'desde': g['desde'].isoformat() if g['desde'] else None,
            # Sin fecha de confirmación no hay antigüedad: `None`, no 0.
            'dias': (hoy - g['desde']).days if g['desde'] else None,
        })
    return sorted(salida, key=lambda f: (-(f['dias'] if f['dias'] is not None else 10**6),
                                         -f['efectivo']))


# ═════════════════════════════════════════════════════════════════════════════
# 6 · Faltante de retorno — declarado por el conductor vs contado por recepción
# ═════════════════════════════════════════════════════════════════════════════

def faltante_de_retorno(devolucion) -> Optional[dict]:
    """Declarado − contado, línea por línea, de una devolución de ruta.

    `None` si la devolución no viene de una ruta, no está confirmada todavía
    (no hay «contado»), o ninguna línea tiene lo declarado (se armó antes de
    que se guardara). **Faltante** = el conductor dijo que volvía y no llegó:
    esas unidades no están en el cliente (no las pagó) ni en bodega.
    **Sobrante** = llegó más de lo declarado; también se declara, porque dice
    que el conductor cobró por mercancía que volvió.
    """
    if devolucion is None or not devolucion.recaudo_entrega_id:
        return None
    # FALTANTE_TOTAL (m045devol) es una devolución contada en cero: el caso más
    # grande de faltante, no uno que se saque de la medición.
    if devolucion.estado not in ('CONFIRMADA', 'FALTANTE_TOTAL'):
        return None
    lineas, faltante, sobrante, medidas = [], 0.0, 0.0, 0

    def _sumar(codigo, producto_id, dec, cont):
        nonlocal faltante, sobrante
        dif = round(dec - cont, 4)
        if dif > 0:
            faltante += dif
        elif dif < 0:
            sobrante += -dif
        if dif:
            lineas.append({'codigo_siesa': codigo, 'producto_id': producto_id,
                           'declarado': dec, 'contado': cont, 'diferencia': dif})

    for l in devolucion.lineas:
        if l.cantidad_declarada is None:
            continue
        medidas += 1
        _sumar(l.codigo_siesa, l.producto_id, float(l.cantidad_declarada),
               float(l.cantidad_devuelta or 0))
    # Doble unidad declarada por referencia (`devolucion_ruta.vincular_a_factura`):
    # lo declarado vive por producto y lo contado en sus líneas.
    decl_prod = ((devolucion.declaracion_conductor or {}).get('declarado_por_producto') or {})
    for pid, dec in decl_prod.items():
        medidas += 1
        lns = [l for l in devolucion.lineas if str(l.producto_id) == str(pid)]
        _sumar(lns[0].codigo_siesa if lns else None, int(pid), float(dec or 0),
               sum(float(l.cantidad_devuelta or 0) for l in lns))
    if not medidas:
        return None
    return {'devolucion_id': devolucion.id, 'codigo': devolucion.codigo,
            'recaudo_id': devolucion.recaudo_entrega_id,
            'faltante_unidades': round(faltante, 4),
            'sobrante_unidades': round(sobrante, 4),
            'lineas': lineas}


def faltantes_de_retorno_de_recaudos(recaudo_ids) -> dict:
    """`{recaudo_id: faltante_de_retorno(...)}` solo de los que tienen algo."""
    from app.models.devolucion_cliente import DevolucionCliente
    ids = [i for i in (recaudo_ids or []) if i]
    if not ids:
        return {}
    salida = {}
    for dev in (DevolucionCliente.query
                .filter(DevolucionCliente.recaudo_entrega_id.in_(ids),
                        DevolucionCliente.estado == 'CONFIRMADA').all()):
        f = faltante_de_retorno(dev)
        if f and (f['faltante_unidades'] or f['sobrante_unidades']):
            salida[dev.recaudo_entrega_id] = f
    return salida


# ═════════════════════════════════════════════════════════════════════════════
# 7 · Las señales de UNA parada — lo que ve quien liquida
# ═════════════════════════════════════════════════════════════════════════════

def senales_de_recaudo(recaudo, ruta=None, faltante=None) -> list:
    """Las señales de fuga de una parada, en palabras. Nunca un veredicto.

    Cada una: `{clave, texto}`. Una lista vacía = nada que mirar, no «está
    todo verificado».
    """
    from app.models.recaudo_entrega import EstadoEntrega
    senales = []
    cobra = recaudo.estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)
    if cobra and requiere_comprobante(recaudo.forma_pago) and float(recaudo.monto_cobrado or 0) > 0:
        if not recaudo.referencia_pago:
            senales.append({'clave': 'sin_comprobante',
                            'texto': 'Pago bancario sin referencia del comprobante'})
        elif not recaudo.foto_comprobante:
            senales.append({'clave': 'sin_foto_comprobante',
                            'texto': 'Pago bancario sin foto del comprobante'})
    if recaudo.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO and not recaudo.foto_entrega:
        senales.append({'clave': 'sin_evidencia',
                        'texto': 'Se quedó con la mercancía sin foto de evidencia'})
    lejos = senal_rechazo_lejos(recaudo)
    if lejos:
        senales.append({'clave': 'rechazo_lejos',
                        'texto': (f'Rechazo por «cliente cerrado» registrado a '
                                  f'{lejos["distancia_m"]:.0f} m del punto del cliente '
                                  f'(umbral {lejos["umbral_m"]:.0f} m)'),
                        'distancia_m': lejos['distancia_m']})
    hora = clasificar_hora(recaudo.ts_dispositivo, recaudo.ts_desfase_s,
                           recaudo.fecha_confirmacion,
                           getattr(ruta, 'fecha_cierre', None) if ruta is not None else None)
    if hora['estado'] == 'reloj_desfasado':
        senales.append({'clave': 'reloj_desfasado',
                        'texto': f'El reloj del teléfono estaba desfasado {hora["desfase_s"]} s'})
    elif hora['estado'] == 'antes_de_salir':
        senales.append({'clave': 'antes_de_salir',
                        'texto': 'La hora del teléfono es anterior a la salida del camión'})
    if faltante and faltante.get('faltante_unidades'):
        senales.append({'clave': 'faltante_retorno',
                        'texto': (f'Declaró devolver más de lo que llegó a bodega: '
                                  f'faltan {faltante["faltante_unidades"]:g} und'),
                        'unidades': faltante['faltante_unidades']})
    if faltante and faltante.get('sobrante_unidades'):
        senales.append({'clave': 'sobrante_retorno',
                        'texto': (f'Llegó a bodega más de lo declarado: '
                                  f'{faltante["sobrante_unidades"]:g} und de más'),
                        'unidades': faltante['sobrante_unidades']})
    return senales


# ═════════════════════════════════════════════════════════════════════════════
# 8 · Condición del vehículo al despachar — informa, no bloquea
# ═════════════════════════════════════════════════════════════════════════════

def advertencias_de_flota(ruta, hoy=None) -> list:
    """Lo que la flota sabe de este vehículo que debería frenar un despacho.

    **No decide nada acá.** La pregunta «¿puede salir este camión?» tiene UNA
    política, `flota/dominio/salida.py`, la misma que pinta el semáforo de la
    bandeja y el aviso del conductor. Esta función le pasa el conductor de la
    ruta (para comparar contra quién tiene el turno) y devuelve los motivos que
    `frenan`: papeles para circular vencidos o sin cargar, daño bloqueante o
    vencido, preventivo vencido, orden de taller abierta, inspección de hoy y
    custodia. Hasta el 2026-09-24 esta lista se armaba a mano y **no sumaba el
    daño bloqueante ni el preventivo vencido**: un camión con el freno reportado
    como bloqueante salía sin que nadie tuviera que escribir por qué.

    Cada advertencia: `{clave, texto}`. La clave es estable (sirve para saber si
    ya se reconoció en un FORZAR anterior); el texto es el mismo que ve el
    conductor.

    Si la flota no está disponible (tablas ausentes, import que falla) se
    devuelve UNA advertencia `flota_sin_dato` — no una lista vacía: «no pude
    mirar» no es «está en orden» (Regla 0).
    """
    if ruta is None or not ruta.vehiculo_id:
        return [{'clave': 'sin_vehiculo', 'texto': 'La ruta no tiene vehículo asignado'}]
    try:
        from flota.adaptadores.salida import evaluar_vehiculo
    except Exception as e:  # pragma: no cover — el módulo vive en el mismo repo
        logger.warning('[SENALES] flota no disponible: %s', e)
        return [{'clave': 'flota_sin_dato', 'texto': 'No se pudo consultar el estado del vehículo'}]
    try:
        ev = evaluar_vehiculo(ruta.vehiculo_id, conductor_de_la_ruta=ruta.conductor_id,
                              sale_hoy=True)
    except Exception as e:
        logger.warning('[SENALES] no se pudo leer el estado de flota del vehículo %s: %s',
                       ruta.vehiculo_id, e)
        return [{'clave': 'flota_sin_dato',
                 'texto': 'No se pudo consultar el estado completo del vehículo'}]
    return [{'clave': m.clave, 'texto': m.texto} for m in ev.para_despachar()]


def despachos_forzados(desde=None, hasta=None) -> list:
    """Las rutas que salieron **reconociendo** advertencias de flota: el FORZAR
    que escribe `RutaService._reconocer_advertencias_flota`.

    **Una función** para leerlos. La bitácora guarda con la misma acción
    `FORZAR` el cierre forzado de una ruta (`forzar_cierre`), que es otra cosa:
    acá se distinguen por lo que el escritor puso en `despues` —
    `advertencias_flota`—, no por la acción. Quien lea la bitácora por su
    cuenta y cuente todos los FORZAR como «despachó con advertencias» (o todos
    como «cierre forzado») mezcla los dos.

    Cada fila: ruta, vehículo, cuándo (UTC naive), quién lo autorizó, el motivo
    escrito, el momento (iniciar el cargue / despachar) y las advertencias tal
    como se vieron. Sin placa, sin nombre: eso lo pone quien pinta.
    """
    from app.extensions import db
    from app.models.bitacora import BitacoraAccion
    from app.models.ruta_despacho import RutaDespacho
    q = BitacoraAccion.query.filter(BitacoraAccion.accion == 'FORZAR',
                                    BitacoraAccion.entidad == 'RutaDespacho')
    if desde is not None:
        q = q.filter(BitacoraAccion.ocurrido_en >= desde)
    if hasta is not None:
        q = q.filter(BitacoraAccion.ocurrido_en < hasta)
    salida = []
    for b in q.order_by(BitacoraAccion.ocurrido_en, BitacoraAccion.id).all():
        despues = b.despues if isinstance(b.despues, dict) else {}
        if 'advertencias_flota' not in despues:
            continue            # cierre forzado de la ruta, no un despacho
        ruta = db.session.get(RutaDespacho, b.entidad_id) if b.entidad_id else None
        detalle = despues['detalle'] if isinstance(despues.get('detalle'), list) else []
        salida.append({
            'bitacora_id': b.id,
            'ruta_id': b.entidad_id,
            'vehiculo_id': ruta.vehiculo_id if ruta is not None else None,
            'conductor_id': ruta.conductor_id if ruta is not None else None,
            'ts': b.ocurrido_en,
            'usuario_id': b.usuario_id,
            'motivo': b.motivo,
            'momento': despues.get('momento'),
            'claves': list(despues['advertencias_flota'] or []),
            'advertencias': [a for a in detalle if isinstance(a, dict)],
        })
    return salida


__all__ = [
    'VERSION_FORMULARIO_CON_EVIDENCIA', 'formulario_con_evidencia',
    'requiere_comprobante', 'limpiar_referencia', 'exige_evidencia', 'geo_fue_intentado',
    'leer_ts', 'desfase_s', 'umbral_desfase_s', 'clasificar_hora',
    'MOTIVOS_AFIRMAN_PRESENCIA', 'umbral_rechazo_lejos_m', 'distancia_al_maestro',
    'senal_rechazo_lejos', 'efectivo_en_poder_por_conductor', 'faltante_de_retorno',
    'faltantes_de_retorno_de_recaudos', 'senales_de_recaudo', 'advertencias_de_flota',
    'despachos_forzados',
]
