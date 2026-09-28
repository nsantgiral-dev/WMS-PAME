"""
Quién recibe qué, y el registro de lo que salió.

Separado del canal a propósito: acá está la decisión (a quién avisar, cuándo,
una sola vez), allá está el transporte. El servicio no sabe que existe Gupshup
— recibe un `CanalDeAviso` y le pide que mande.

**Nace apagado** (regla 10): sin `FLOTA_AVISOS=true` el barrido no corre. Y aun
encendido, el canal por defecto es el simulado — encenderlo de verdad es una
segunda decisión explícita, `FLOTA_AVISOS_REALES=true`.

Los teléfonos van en configuración, a mano. Son cuatro números de empleados; no
salen de `TercerosContacto`, así que ni la paginación sin `ORDER BY` ni el caché
que reemplaza tocan este caso.
"""
import json
import logging
import os
from datetime import datetime

from app.extensions import db
from app.utils.fecha import dia_operativo
from flota.adaptadores.gupshup import AvisoNoEnviado, canal as canal_por_defecto
from flota.adaptadores.modelos import Aviso, DocumentoVehiculo
from flota.dominio.aviso import (
    AvisoInvalido,
    DIAS_AVISO_DOCUMENTO,
    clave_aviso,
    parametros_documento_vence,
    hito_semanal,
    toca_avisar_vencido,
    toca_avisar_vencimiento,
)

logger = logging.getLogger(__name__)


def avisos_encendidos() -> bool:
    return (os.getenv('FLOTA_AVISOS') or '').lower() == 'true'


def destinatarios(rol: str) -> list:
    """Teléfonos por rol, desde configuración.

    `FLOTA_AVISO_TELEFONOS` es JSON: `{"mantenimiento": ["573001112233"], ...}`.
    Sin default: un rol sin teléfonos configurados **no manda a nadie y lo
    dice**, en vez de caer a una lista global que avisaría al que no es.
    """
    crudo = (os.getenv('FLOTA_AVISO_TELEFONOS') or '').strip()
    if not crudo:
        return []
    try:
        mapa = json.loads(crudo)
    except ValueError as e:
        logger.error('[FLOTA/AVISO] FLOTA_AVISO_TELEFONOS no es JSON válido: %s', e)
        return []
    valor = mapa.get(rol) or []
    return [str(v).strip() for v in valor if str(v).strip()]


def _registrar(clave, plantilla, telefono, parametros, canal_usado, fila=None):
    """Manda y deja la fila. La fila se escribe SIEMPRE, salga o no.

    Primero se inserta `encolado` y se hace commit: si el proceso se muere entre
    el POST y el registro, la clave única impide que el próximo ciclo lo mande
    de nuevo. Un aviso duplicado no es grave; tres seguidos silencian el chat, y
    entonces el que importa llega a un silencio.

    `fila` = una fila `fallido` de la misma clave que se reintenta (2026-09-27):
    antes, un aviso que fallaba una vez no se reintentaba nunca — la clave ya
    existía y el barrido lo contaba como «ya avisado».
    """
    if fila is None:
        fila = Aviso(
            clave=clave, plantilla=plantilla, telefono=str(telefono),
            parametros=json.dumps(parametros, ensure_ascii=False),
            estado='encolado', simulado=bool(canal_usado.simulado),
        )
        db.session.add(fila)
    else:
        fila.parametros = json.dumps(parametros, ensure_ascii=False)
        fila.estado, fila.detalle = 'encolado', None
        fila.simulado = bool(canal_usado.simulado)
    db.session.commit()

    try:
        msg_id = canal_usado.enviar(telefono, plantilla, parametros)
    except (AvisoNoEnviado, AvisoInvalido) as e:
        fila.estado = 'fallido'
        fila.detalle = str(e)[:500]
        db.session.commit()
        logger.error('[FLOTA/AVISO] %s → %s falló: %s', plantilla, telefono, e)
        return fila

    fila.proveedor_msg_id = msg_id
    # `entregado_al_proveedor`, no `entregado`. Gupshup dijo "lo recibí".
    # Que haya llegado lo dice el evento de entrega, y hasta entonces este
    # aviso NO cuenta como avisado.
    fila.estado = 'entregado_al_proveedor'
    db.session.commit()
    return fila


def _pendientes(base: str, telefonos) -> list:
    """(índice, teléfono, fila a reintentar) de los destinatarios a los que
    todavía no les salió este aviso: sin fila, o con la fila `fallido`. UNA
    definición de «ya se avisó» para todo el barrido."""
    out = []
    for i, t in enumerate(telefonos):
        fila = Aviso.query.filter_by(clave=f'{base}:{i}').first()
        if fila is None or fila.estado == 'fallido':
            out.append((i, t, fila))
    return out


def barrer_documentos_por_vencer(canal_usado=None, hoy=None) -> dict:
    """Avisa por cada documento que entró en la ventana de vencimiento.

    Devuelve un resumen, no `None`: un barrido que no dice qué hizo es
    indistinguible de uno que no corrió.

    Idempotente por `clave_aviso`, que lleva la fecha de vencimiento: correr dos
    veces el mismo día no repite, y un documento RENOVADO sí vuelve a avisar
    porque su hito cambió.
    """
    resumen = {'revisados': 0, 'en_ventana': 0, 'enviados': 0,
               'ya_avisados': 0, 'fallidos': 0, 'sin_destinatario': 0,
               'simulado': None}

    if not avisos_encendidos():
        resumen['motivo'] = 'FLOTA_AVISOS no está en true'
        return resumen

    hoy = hoy or dia_operativo()
    canal_usado = canal_usado or canal_por_defecto()
    resumen['simulado'] = bool(canal_usado.simulado)

    telefonos = destinatarios('mantenimiento')
    docs = (DocumentoVehiculo.query
            .filter(DocumentoVehiculo.estado == 'vigente')
            .filter(DocumentoVehiculo.fecha_vencimiento.isnot(None))
            .all())

    for doc in docs:
        resumen['revisados'] += 1
        if not toca_avisar_vencimiento(doc.fecha_vencimiento, hoy, DIAS_AVISO_DOCUMENTO):
            continue
        resumen['en_ventana'] += 1

        if not telefonos:
            # No se inventa un destinatario. Queda contado para que el health
            # pueda decir "hay avisos que nadie recibió por falta de números".
            resumen['sin_destinatario'] += 1
            continue

        # La clave se consulta EXACTAMENTE como se escribe, con el sufijo del
        # destinatario. Consultar la base y escribir la sufijada hacía que la
        # deduplicación nunca encontrara nada: el segundo barrido reventaba
        # contra el índice único en vez de saltarse el aviso.
        base = clave_aviso('flota_documento_vence', 'documento', doc.id,
                           doc.fecha_vencimiento.isoformat())
        pendientes = _pendientes(base, telefonos)
        if not pendientes:
            resumen['ya_avisados'] += 1
            continue

        placa = doc.vehiculo.placa if doc.vehiculo is not None else ''
        try:
            parametros = parametros_documento_vence(placa, doc.tipo, doc.fecha_vencimiento)
        except AvisoInvalido as e:
            logger.error('[FLOTA/AVISO] documento %s no se puede describir: %s', doc.id, e)
            resumen['fallidos'] += 1
            continue

        # Por destinatario y no por documento: si mañana se agrega un cuarto
        # número, esa persona recibe el aviso que todavía está vigente en vez de
        # quedar afuera para siempre porque "ya se avisó".
        for i, telefono, previa in pendientes:
            fila = _registrar(f'{base}:{i}', 'flota_documento_vence',
                              telefono, parametros, canal_usado, fila=previa)
            if fila.estado == 'fallido':
                resumen['fallidos'] += 1
            else:
                resumen['enviados'] += 1

    # ── Segunda pasada: los que YA vencieron ─────────────────────────────────
    #
    # `toca_avisar_vencimiento` los excluye a propósito —renovar antes y
    # circular ilegal no son el mismo mensaje— y remitía a «otra vía». Esa vía
    # era el contador `documentos_vencidos` del health, **un endpoint sin un
    # solo consumidor en el repo**. Así que si nadie renovaba dentro de los 15
    # días, el canal se callaba justo cuando el vehículo pasaba a ser ilegal.
    #
    # En producción hay una RTM vencida desde 2025-11-11 que nunca avisó.
    #
    # Se cuenta aparte de `en_ventana`: son dos poblaciones y sumarlas esconde
    # la segunda, que es la grave. Es el mismo criterio con el que
    # `documentos_no_encontrados` está separado de `documentos_vencidos`.
    resumen['vencidos_en_ventana'] = 0
    for doc in docs:
        if not toca_avisar_vencido(doc.fecha_vencimiento, hoy):
            continue
        resumen['vencidos_en_ventana'] += 1

        if not telefonos:
            resumen['sin_destinatario'] += 1
            continue

        # El hito es la SEMANA, no la fecha de vencimiento. Con el vencimiento
        # como hito avisaría una sola vez en la vida: si ese mensaje se pierde,
        # el camión queda ilegal y nadie vuelve a decir nada. Con la semana
        # insiste hasta que se renueve, y se apaga solo cuando cambia la fecha.
        base = clave_aviso('flota_documento_vencido', 'documento', doc.id,
                           hito_semanal(hoy))
        pendientes = _pendientes(base, telefonos)
        if not pendientes:
            resumen['ya_avisados'] += 1
            continue

        placa = doc.vehiculo.placa if doc.vehiculo is not None else ''
        try:
            parametros = parametros_documento_vence(
                placa, doc.tipo, doc.fecha_vencimiento)
        except AvisoInvalido as e:
            logger.error('[FLOTA/AVISO] documento vencido %s no se puede '
                         'describir: %s', doc.id, e)
            resumen['fallidos'] += 1
            continue

        for i, telefono, previa in pendientes:
            # **Se manda con la plantilla `flota_documento_vence`, no con una
            # propia.** Una plantilla nueva exige aprobación de Gupshup, y este
            # canal entero ya está bloqueado esperando los ids definitivos de
            # un tercero: crear una segunda dependencia del mismo tercero
            # dejaría el aviso de vencidos apagado por más tiempo que el de por
            # vencer.
            #
            # El mensaje sale correcto aunque en tiempo verbal incómodo — «el
            # documento RTM del TGZ653 vence el 11 de noviembre de 2025», leído
            # en septiembre de 2026, se entiende sin ambigüedad. La CLAVE sí
            # lleva `flota_documento_vencido`, así que los dos avisos no se
            # deduplican entre sí y el registro distingue de cuál se trata.
            #
            # Deuda declarada: cuando se pidan las plantillas a Gupshup, va una
            # propia con el texto correcto.
            fila = _registrar(f'{base}:{i}', 'flota_documento_vence',
                              telefono, parametros, canal_usado, fila=previa)
            if fila.estado == 'fallido':
                resumen['fallidos'] += 1
            else:
                resumen['enviados'] += 1

    logger.info('[FLOTA/AVISO] barrido de documentos: %s', resumen)
    return resumen


#: Eventos de Gupshup que SÍ mueven el estado. Los demás se ignoran.
_EVENTOS_PROVEEDOR = {'delivered': 'entregado', 'read': 'leido',
                      'failed': 'fallido', 'undelivered': 'fallido'}

#: Orden de avance de los estados. Total sobre `ESTADO_AVISO` a propósito —
#: `test_el_orden_cubre_todo_el_vocabulario` lo verifica contra la tabla.
#: `fallido` comparte nivel con `entregado_al_proveedor`: es un desenlace, no
#: un retroceso, y por eso se aplica siempre.
_ORDEN = {'encolado': 0, 'entregado_al_proveedor': 1, 'fallido': 1,
          'entregado': 2, 'leido': 3}


def registrar_entrega(proveedor_msg_id: str, estado_proveedor: str) -> bool:
    """Consume un evento de entrega. Devuelve si encontró la fila.

    Existe desde el día uno y no "cuando haga falta": sin esto, todo lo que el
    sistema puede afirmar es que Gupshup recibió el mensaje. El propósito del
    módulo es que un hallazgo vencido no se quede quieto — un aviso que no llega
    y nadie nota es el fallo exacto que hay que poder ver.
    """
    # Acá el `.get` SÍ corresponde y no es degradación: Gupshup manda decenas
    # de tipos de evento (`sent`, `enqueued`, `deleted`…) y los que no cambian
    # el estado se ignoran a propósito. La diferencia con el caso de arriba es
    # que el universo de entrada es ajeno y abierto, no un vocabulario nuestro.
    nuevo = _EVENTOS_PROVEEDOR.get((estado_proveedor or '').lower())
    if nuevo is None:
        logger.info('[FLOTA/AVISO] evento ignorado: %s', estado_proveedor)
        return False

    fila = Aviso.query.filter_by(proveedor_msg_id=(proveedor_msg_id or '').strip()).first()
    if fila is None:
        logger.warning('[FLOTA/AVISO] evento sin fila: %s', proveedor_msg_id)
        return False

    # `leido` no retrocede a `entregado`: los eventos pueden llegar desordenados
    # y un tablero que baja de estado se lee como un problema que no existe.
    #
    # El mapa es TOTAL sobre `ESTADO_AVISO` y se indexa directo, sin default: un
    # estado nuevo que alguien agregue al vocabulario y olvide acá tiene que
    # reventar, no ordenarse como 0 y hacer que todo lo demás lo pise en
    # silencio. Un `.get(x, 0)` acá es la regla 5.
    if _ORDEN[nuevo] < _ORDEN[fila.estado] and nuevo != 'fallido':
        return True

    fila.estado = nuevo
    if nuevo in ('entregado', 'leido') and fila.entregado_ts is None:
        fila.entregado_ts = datetime.utcnow()
    db.session.commit()
    return True


def avisos_sin_confirmar(horas: int = 6) -> int:
    """Cuántos salieron y nunca confirmaron entrega.

    Es el número que hace honesto al resto: si esto crece, el canal está
    aceptando mensajes que no llegan — el modo de fallo que ya pasó en cartera y
    que un contador de "enviados" no puede ver.
    """
    from datetime import timedelta

    corte = datetime.utcnow() - timedelta(hours=horas)
    # El correo no tiene evento de entrega: su confirmación es el id que Resend
    # devolvió. Contarlo acá lo dejaría «sin confirmar» para siempre.
    return (Aviso.query
            .filter(Aviso.estado == 'entregado_al_proveedor')
            .filter(Aviso.simulado.is_(False))
            .filter(Aviso.telefono != CANAL_CORREO)
            .filter(Aviso.creado_ts < corte)
            .count())


# ═══════════════════════════════════════════════════════════════════════════
# El correo (2026-09-27): el canal que no depende de un tercero
# ═══════════════════════════════════════════════════════════════════════════
#
# Hasta hoy el único canal era WhatsApp (plantillas sin aprobar, una línea que
# depende de Gupshup) y solo al rol `mantenimiento`; el único correo —el reporte
# semanal— filtraba los vencidos. En producción un papel vencido no le llegaba a
# nadie. El correo sale por Resend, como el resto de las alertas del WMS.

#: Marca de la columna `telefono` de una fila de correo. El destinatario real
#: (una lista) va en los parámetros; `telefono` es corto y obligatorio.
CANAL_CORREO = 'correo'

#: Destinatarios del correo de flota. Sin ella, la lista global de alertas del
#: WMS (`ALERTA_EMAIL_DEST`). Coma-separado.
VAR_CORREOS = 'FLOTA_AVISO_CORREOS'

#: El nombre del cron en `cron_latido` (el mismo `id` de APScheduler).
CRON_BARRIDO = 'flota_avisos_barrido'


def destinatarios_correo() -> tuple:
    """(dest, fuente): `dest` es lo que se le pasa a `enviar_email` —`None`
    hereda `ALERTA_EMAIL_DEST`—; `fuente` dice de dónde salió, o `None` si no
    hay ninguno configurado en ESTE proceso."""
    propio = (os.getenv(VAR_CORREOS) or '').strip()
    if propio:
        return propio, VAR_CORREOS
    if (os.getenv('ALERTA_EMAIL_DEST') or '').strip():
        return None, 'ALERTA_EMAIL_DEST'
    return None, None


def configuracion_de_este_servicio() -> dict:
    """Qué tiene ESTE proceso para avisar. Solo presencias y conteos, nunca un
    valor (ni teléfonos ni correos). El barrido la publica en su latido: es lo
    único que la web puede saber del worker."""
    from app.services.cron_latido import servicio
    crudo = (os.getenv('FLOTA_AVISO_TELEFONOS') or '').strip()
    telefonos, legibles = {}, True
    if crudo:
        try:
            mapa = json.loads(crudo)
            telefonos = {str(k): len(destinatarios(str(k))) for k in mapa}
        except ValueError:
            legibles = False
    _dest, fuente = destinatarios_correo()
    return {
        'servicio': servicio(),
        'FLOTA_AVISOS': avisos_encendidos(),
        'FLOTA_AVISOS_REALES': (os.getenv('FLOTA_AVISOS_REALES') or '').lower() == 'true',
        'telefonos_por_rol': telefonos,
        'telefonos_legibles': legibles,
        'correo_destinatarios': fuente,
        'resend': bool((os.getenv('RESEND_API_KEY') or '').strip()),
    }


def _fila_correo(clave):
    return Aviso.query.filter_by(clave=clave).first()


def _enviar_correo(clave: str, plantilla: str, asunto: str, texto: str,
                   html: str, fila=None) -> 'Aviso':
    """Manda un correo y deja su fila en `flota_aviso` (la misma tabla que el
    WhatsApp: una sola pantalla de «qué salió»). Primero la fila, después el
    envío (misma razón que `_registrar`). Sin Resend configurado, la fila queda
    `fallido` con el motivo: se reintenta en la próxima corrida."""
    from app.services.alertas_service import enviar_email

    dest, fuente = destinatarios_correo()
    parametros = {'asunto': asunto, 'destinatarios': fuente}
    if fila is None:
        fila = Aviso(clave=clave, plantilla=plantilla, telefono=CANAL_CORREO,
                     parametros=json.dumps(parametros, ensure_ascii=False),
                     estado='encolado', simulado=False)
        db.session.add(fila)
    else:
        fila.parametros = json.dumps(parametros, ensure_ascii=False)
        fila.estado, fila.detalle = 'encolado', None
    db.session.commit()
    try:
        msg_id = enviar_email(asunto, html, texto, dest=dest, devolver_id=True)
    except Exception as e:     # noqa: BLE001 — queda en la fila, se reintenta
        fila.estado, fila.detalle = 'fallido', str(e)[:500]
        db.session.commit()
        logger.error('[FLOTA/AVISO] correo %s falló: %s', clave, e)
        return fila
    if not msg_id:
        fila.estado = 'fallido'
        fila.detalle = ('Resend no está configurado en este servicio: falta '
                        'RESEND_API_KEY o destinatarios (FLOTA_AVISO_CORREOS o '
                        'ALERTA_EMAIL_DEST)')
        db.session.commit()
        return fila
    fila.proveedor_msg_id = msg_id
    fila.estado = 'entregado_al_proveedor'
    db.session.commit()
    return fila


def lo_que_hay_que_avisar(hoy=None, ahora=None) -> dict:
    """Las líneas del correo diario de flota, por sección, con los textos de la
    política de salida (el mismo SOAT se escribe igual acá, en la bandeja y en
    el muelle). Cada sección que revienta se declara en `no_se_pudo`, no tumba
    las demás ni se lee como «nada que avisar».

    · papeles de vehículos ACTIVOS: vencidos, por vencer (`DIAS_AVISO_PAPEL`),
      no encontrados, sin cargar y datos a corregir;
    · daños bloqueantes abiertos;
    · licencias de conducción de conductores activos (vencidas, por vencer, sin
      cargar, a corregir);
    · mantenimiento preventivo vencido.
    """
    from flota.adaptadores import salida as ad
    from flota.dominio import salida as dom

    hoy = hoy or dia_operativo()
    ahora = ahora or datetime.utcnow()
    out = {'papeles': [], 'danos': [], 'licencias': [], 'preventivo': [],
           'no_se_pudo': []}

    def _seguro(nombre, fn):
        try:
            fn()
        except Exception as e:     # noqa: BLE001
            logger.error('[FLOTA/AVISO] sección %s falló: %s', nombre, e, exc_info=True)
            out['no_se_pudo'].append(f'No se pudo revisar {nombre}: {str(e)[:160]}')

    def _papeles():
        for placa, papel in ad.papeles_de_la_flota(hoy):
            m = dom.motivo_papel(papel, hoy)
            if m is not None:
                out['papeles'].append({'placa': placa, 'clave': m.clave,
                                       'texto': m.texto, 'bloquea': m.bloquea,
                                       'orden': dom.orden_de_nivel(m.nivel)})
        out['papeles'].sort(key=lambda x: (x['orden'], x['placa']))

    def _danos():
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import EstadoHallazgo, dias_transcurridos
        filas = (db.session.query(Hallazgo, Vehiculo.placa)
                 .join(Vehiculo, Vehiculo.id == Hallazgo.vehiculo_id)
                 .filter(Hallazgo.estado == EstadoHallazgo.ABIERTO,
                         Hallazgo.criticidad == 'bloqueante').all())
        for h, placa in sorted(filas, key=lambda x: x[0].reportado_ts):
            out['danos'].append({
                'placa': placa, 'texto': ' '.join((h.descripcion or '').split())[:200],
                'dias': dias_transcurridos(h.a_dominio(), ahora)})

    def _licencias():
        for nombre, lic in ad.licencias_de_los_conductores(hoy):
            m = dom.motivo_licencia(lic, hoy)
            if m is not None:
                out['licencias'].append({'conductor': nombre, 'clave': m.clave,
                                         'texto': m.texto, 'bloquea': m.bloquea,
                                         'orden': dom.orden_de_nivel(m.nivel)})
        out['licencias'].sort(key=lambda x: (x['orden'], x['conductor']))

    def _preventivo():
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.preventivo import diagnostico_de_la_flota
        from flota.dominio.preventivo import EstadoTarea
        placa_de = {v.id: v.placa for v in Vehiculo.query.filter(Vehiculo.activo.is_(True))}
        for d in diagnostico_de_la_flota():
            if d['vehiculo_id'] not in placa_de or d['estado'] != EstadoTarea.VENCIDA:
                continue
            (tarea,), _n = ad.preventivo_de_diagnostico([d])
            out['preventivo'].append({'placa': placa_de[d['vehiculo_id']],
                                      'texto': dom.motivo_preventivo_vencido(tarea).texto})

    _seguro('los papeles de los vehículos', _papeles)
    _seguro('los daños bloqueantes', _danos)
    _seguro('las licencias de conducción', _licencias)
    _seguro('el mantenimiento preventivo', _preventivo)
    out['total'] = sum(len(out[k]) for k in ('papeles', 'danos', 'licencias', 'preventivo'))
    return out


def texto_del_correo(c: dict, hoy) -> tuple:
    """(asunto, texto, html) del correo diario. El texto lo escribieron
    personas (descripciones de daños): en el HTML va escapado."""
    from html import escape
    from flota.dominio.salida import fecha_larga, plural

    graves = (sum(1 for x in c['papeles'] if x['bloquea'])
              + sum(1 for x in c['licencias'] if x['bloquea']) + len(c['danos']))
    asunto = (f'Flota · {fecha_larga(hoy)} · '
              + (plural(graves, 'cosa que no deja salir un vehículo',
                        'cosas que no dejan salir un vehículo') if graves
                 else plural(c['total'], 'pendiente', 'pendientes')))
    secciones = [
        ('Papeles de los vehículos', [f"{x['placa']} — {x['texto']}" + (' · PROHIBIDO SALIR' if x['bloquea'] else '')
                                      for x in c['papeles']]),
        ('Daños bloqueantes abiertos', [f"{x['placa']} — {x['texto']} (lleva {plural(x['dias'], 'día', 'días')})"
                                        for x in c['danos']]),
        ('Licencias de conducción', [f"{x['conductor']} — {x['texto']}" + (' · PROHIBIDO SALIR' if x['bloquea'] else '')
                                     for x in c['licencias']]),
        ('Mantenimiento preventivo vencido', [f"{x['placa']} — {x['texto']}" for x in c['preventivo']]),
    ]
    lineas = [f'FLOTA · {fecha_larga(hoy)}', '']
    html = [f'<h2 style="font-family:system-ui,sans-serif">Flota · {escape(fecha_larga(hoy))}</h2>']
    for titulo, filas in secciones:
        if not filas:
            continue
        lineas += [f'{titulo} ({len(filas)})'] + [f'  · {f}' for f in filas] + ['']
        html.append(f'<h3 style="font-family:system-ui,sans-serif">{escape(titulo)} ({len(filas)})</h3><ul>'
                    + ''.join(f'<li>{escape(f)}</li>' for f in filas) + '</ul>')
    if c['no_se_pudo']:
        lineas += ['No se pudo revisar:'] + [f'  · {x}' for x in c['no_se_pudo']] + ['']
        html.append('<p><b>No se pudo revisar:</b></p><ul>'
                    + ''.join(f'<li>{escape(x)}</li>' for x in c['no_se_pudo']) + '</ul>')
    pie = ('Con un SOAT, una revisión técnico-mecánica o una licencia vencidos el '
           'vehículo no sale sin la autorización de un administrador. Lo que dice '
           '«sin cargar» no se sabe: cárguelo en Flota → Documentos o en Rutas → Conductores.')
    lineas.append(pie)
    html.append(f'<p style="color:#555">{escape(pie)}</p>')
    return asunto, '\n'.join(lineas), ''.join(html)


def correo_diario(hoy=None, ahora=None) -> dict:
    """El correo diario de flota: papeles vencidos Y por vencer, daños
    bloqueantes, licencias, preventivo vencido. UNO por día (clave
    `correo_flota_diario:<día>`); uno que falló se reintenta en la próxima
    corrida del mismo día. Sin nada que avisar no manda (y lo dice)."""
    hoy = hoy or dia_operativo()
    r = {'enviado': False, 'motivo': None, 'lineas': 0}
    if not avisos_encendidos():
        r['motivo'] = 'FLOTA_AVISOS no está en true en este servicio'
        return r
    c = lo_que_hay_que_avisar(hoy, ahora)
    r['lineas'] = c['total']
    r['no_se_pudo'] = c['no_se_pudo']
    if not c['total'] and not c['no_se_pudo']:
        r['motivo'] = 'nada que avisar'
        return r
    clave = f'correo_flota_diario:{hoy.isoformat()}'
    previa = _fila_correo(clave)
    if previa is not None and previa.estado != 'fallido':
        r['motivo'] = 'ya salió hoy'
        return r
    asunto, texto, html = texto_del_correo(c, hoy)
    fila = _enviar_correo(clave, 'correo_flota_diario', asunto, texto, html, fila=previa)
    r['enviado'] = fila.estado == 'entregado_al_proveedor'
    if not r['enviado']:
        r['motivo'] = fila.detalle
    return r


# ── El daño bloqueante avisa al nacer, no al otro día ────────────────────────

def avisar_dano_bloqueante(hallazgo_id: int, canal_usado=None) -> dict:
    """Aviso inmediato de UN daño bloqueante: correo (y WhatsApp a
    `mantenimiento` con la plantilla `flota_hallazgo_bloqueante`, simulado
    salvo `FLOTA_AVISOS_REALES`). Idempotente por hallazgo; un envío fallido se
    reintenta al volver a llamar. Nace apagado con `FLOTA_AVISOS` de ESTE
    servicio (el que registró el daño); el correo diario lo repite igual."""
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import Hallazgo
    from flota.dominio.aviso import parametros_hallazgo_bloqueante

    r = {'correo': None, 'whatsapp': 0, 'motivo': None}
    if not avisos_encendidos():
        r['motivo'] = 'FLOTA_AVISOS no está en true en este servicio'
        return r
    h = db.session.get(Hallazgo, hallazgo_id)
    if h is None or h.criticidad != 'bloqueante':
        r['motivo'] = 'no es un daño bloqueante'
        return r
    v = db.session.get(Vehiculo, h.vehiculo_id)
    placa = v.placa if v is not None else ''
    descripcion = ' '.join((h.descripcion or '').split())
    base = clave_aviso('flota_hallazgo_bloqueante', 'hallazgo', h.id, 'nacio')

    clave = f'{base}:{CANAL_CORREO}'
    previa = _fila_correo(clave)
    if previa is None or previa.estado == 'fallido':
        from html import escape
        asunto = f'Flota · {placa}: daño bloqueante — no debería salir'
        texto = (f'{placa} — daño bloqueante reportado: {descripcion}\n\n'
                 'Un daño bloqueante se atiende el mismo día. Mírelo en Flota → Pendientes.')
        fila = _enviar_correo(clave, 'correo_flota_dano_bloqueante', asunto, texto,
                              f'<p><b>{escape(placa)}</b> — daño bloqueante reportado: '
                              f'{escape(descripcion)}</p><p>Un daño bloqueante se atiende '
                              f'el mismo día. Mírelo en Flota → Pendientes.</p>', fila=previa)
        r['correo'] = fila.estado
    else:
        r['correo'] = 'ya_avisado'

    telefonos = destinatarios('mantenimiento')
    if telefonos:
        try:
            parametros = parametros_hallazgo_bloqueante(placa, descripcion)
        except AvisoInvalido as e:
            r['motivo'] = str(e)
            return r
        canal_usado = canal_usado or canal_por_defecto()
        for i, telefono, previa_wa in _pendientes(base, telefonos):
            fila = _registrar(f'{base}:{i}', 'flota_hallazgo_bloqueante', telefono,
                              parametros, canal_usado, fila=previa_wa)
            if fila.estado != 'fallido':
                r['whatsapp'] += 1
    return r


#: Cómo se lanza el aviso inmediato (un hilo: el correo no demora la respuesta
#: del conductor). Los tests lo cambian por una llamada directa.
def _lanzar(fn):
    import threading
    threading.Thread(target=fn, daemon=True).start()


def _despachar_danos_bloqueantes(session):
    """Después del COMMIT (nunca antes): si la transacción se deshace, el daño
    no existe y no se avisa. Toma los ids anotados por el evento del modelo."""
    ids = session.info.pop('flota_danos_bloqueantes', None)
    if not ids or not avisos_encendidos():
        return
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except RuntimeError:
        logger.warning('[FLOTA/AVISO] daño bloqueante sin app: queda para el correo diario')
        return

    def _correr():
        with app.app_context():
            for hid in sorted(ids):
                try:
                    avisar_dano_bloqueante(hid)
                except Exception:     # noqa: BLE001 — el correo diario lo repite
                    logger.exception('[FLOTA/AVISO] aviso del daño %s falló', hid)
                    db.session.rollback()
    _lanzar(_correr)


def escuchar_danos_bloqueantes():
    """Registra, una vez, los eventos que avisan un daño bloqueante AL NACER
    (o al volverse bloqueante), venga de la puerta que venga — reporte suelto,
    inspección, recibo del turno. Un guard en el modelo protege la operación;
    uno en la ruta protegería esa ruta."""
    from sqlalchemy import event, inspect as sa_inspect
    from sqlalchemy.orm import Session, object_session
    from flota.adaptadores.modelos import Hallazgo

    def _anotar(target):
        sesion = object_session(target)
        if sesion is not None and target.id is not None:
            sesion.info.setdefault('flota_danos_bloqueantes', set()).add(target.id)

    def _al_insertar(_mapper, _conn, target):
        if target.criticidad == 'bloqueante':
            _anotar(target)

    def _al_actualizar(_mapper, _conn, target):
        hist = sa_inspect(target).attrs.criticidad.history
        if hist.has_changes() and target.criticidad == 'bloqueante':
            _anotar(target)

    def _al_deshacer(session, _previous_transaction=None):
        session.info.pop('flota_danos_bloqueantes', None)

    for objetivo, nombre, fn in ((Hallazgo, 'after_insert', _al_insertar),
                                 (Hallazgo, 'after_update', _al_actualizar),
                                 (Session, 'after_commit', _despachar_danos_bloqueantes),
                                 (Session, 'after_soft_rollback', _al_deshacer)):
        if not event.contains(objetivo, nombre, fn):
            event.listen(objetivo, nombre, fn)


# ── El barrido diario y lo que la pantalla sabe de él ────────────────────────

def barrido_diario(canal_usado=None, hoy=None) -> dict:
    """Lo que corre el cron de las 06:00 (y el botón «Revisar ahora»): el
    WhatsApp de papeles y el correo diario. Devuelve su resumen CON la
    configuración del servicio que lo corrió: el latido lo guarda y el panel de
    la web lo lee — es lo único que la web puede saber del worker."""
    config = configuracion_de_este_servicio()
    wa = barrer_documentos_por_vencer(canal_usado=canal_usado, hoy=hoy)
    try:
        correo = correo_diario(hoy=hoy)
    except Exception as e:     # noqa: BLE001 — se declara, no tumba el WhatsApp
        logger.exception('[FLOTA/AVISO] el correo diario reventó')
        correo = {'enviado': False, 'motivo': f'error: {str(e)[:200]}', 'lineas': None}
    resumen = dict(wa)
    resumen.update({'correo': correo, 'config': config})
    return resumen


def que_falta(barridos: list, esta_web: dict, ahora=None) -> list:
    """Qué le falta a cada servicio para que los avisos salgan, en usted, con
    DÓNDE va cada variable. `barridos`: las filas de `cron_latido` del barrido
    (una por servicio, `to_dict`). `esta_web`: la configuración del proceso que
    contesta. Si no se puede saber, lo dice — no supone.

    Cada renglón: `{servicio, texto, grave}`; `grave=False` es una nota."""
    from datetime import timedelta
    ahora = ahora or datetime.utcnow()
    out = []

    def _falta(srv, texto, grave=True):
        out.append({'servicio': srv, 'texto': texto, 'grave': grave})

    vigentes = [b for b in barridos if b.get('ultimo_inicio')]
    if not vigentes:
        _falta(None, 'El barrido diario de avisos no ha corrido en ningún servicio. '
                     'Corre en el servicio que tiene HEAVY_SCHEDULERS=true (hoy, el '
                     'WMS-Worker). Hasta que corra una vez no se puede saber qué '
                     'variables tiene ese servicio.')
    else:
        b = max(vigentes, key=lambda x: x['ultimo_inicio'])
        srv = b.get('servicio')
        inicio = datetime.fromisoformat(b['ultimo_inicio'])
        if ahora - inicio > timedelta(hours=26):
            _falta(srv, f'El barrido no corre desde hace más de un día (último: '
                        f'{inicio:%d/%m/%Y %H:%M} UTC en «{srv}»). Revise ese servicio.')
        if b.get('ultimo_ok') is False:
            _falta(srv, f'La última corrida en «{srv}» falló: '
                        f'{(b.get("ultimo_error") or "sin detalle")[:200]}')
        resumen = b.get('ultimo_resumen')
        config = resumen.get('config') if isinstance(resumen, dict) else None
        if not isinstance(config, dict):
            _falta(srv, f'El barrido corrió en «{srv}» con una versión que no publicaba '
                        f'su configuración: no se sabe qué variables tiene. Se sabrá en '
                        f'la próxima corrida.')
        else:
            if not config.get('FLOTA_AVISOS'):
                _falta(srv, f'En el servicio «{srv}» (el que corre el barrido diario) '
                            f'falta FLOTA_AVISOS=true: no sale ni el correo ni el WhatsApp.')
            if not config.get('resend') or not config.get('correo_destinatarios'):
                _falta(srv, f'En «{srv}» el correo diario no puede salir: falta '
                            f'RESEND_API_KEY o los destinatarios (FLOTA_AVISO_CORREOS, '
                            f'o ALERTA_EMAIL_DEST).')
            if config.get('telefonos_legibles') is False:
                _falta(srv, f'En «{srv}» FLOTA_AVISO_TELEFONOS no es JSON válido.')
            elif not (config.get('telefonos_por_rol') or {}).get('mantenimiento'):
                _falta(srv, f'En «{srv}» falta FLOTA_AVISO_TELEFONOS con el rol '
                            f'«mantenimiento»: el WhatsApp no tiene a quién.', grave=False)
            if not config.get('FLOTA_AVISOS_REALES'):
                _falta(srv, f'En «{srv}» el WhatsApp está en modo simulado: se registra y no '
                            f'sale. Para mandarlo de verdad, FLOTA_AVISOS_REALES=true y la '
                            f'línea de Gupshup aprobada.', grave=False)
            correo = resumen.get('correo') if isinstance(resumen, dict) else None
            if isinstance(correo, dict) and correo.get('motivo') and not correo.get('enviado') \
                    and correo.get('motivo') not in ('nada que avisar', 'ya salió hoy'):
                _falta(srv, f'El último correo diario no salió: {correo["motivo"]}')
    web = esta_web.get('servicio')
    if not esta_web.get('FLOTA_AVISOS'):
        _falta(web, f'En este servicio («{web}», donde se registran los daños) falta '
                    f'FLOTA_AVISOS=true: el aviso inmediato de un daño bloqueante no sale; '
                    f'lo recoge el correo diario del otro servicio.', grave=False)
    elif not esta_web.get('resend') or not esta_web.get('correo_destinatarios'):
        _falta(web, f'En este servicio («{web}») falta RESEND_API_KEY o los destinatarios: '
                    f'el aviso inmediato de un daño bloqueante no sale por correo.', grave=False)
    return out


def estado_del_barrido() -> list:
    """Las filas de `cron_latido` del barrido, de la base (lo que corrió de
    verdad, en el servicio que sea). Nunca levanta: sin tabla, vacío."""
    try:
        from app.models.cron_latido import CronLatido
        return [f.to_dict() for f in
                CronLatido.query.filter_by(nombre=CRON_BARRIDO).all()]
    except Exception as e:     # noqa: BLE001
        logger.warning('[FLOTA/AVISO] no se pudo leer el latido: %s', e)
        return []


__all__ = ['avisos_encendidos', 'destinatarios', 'barrer_documentos_por_vencer',
           'registrar_entrega', 'avisos_sin_confirmar', 'CANAL_CORREO', 'VAR_CORREOS',
           'CRON_BARRIDO', 'destinatarios_correo', 'configuracion_de_este_servicio',
           'lo_que_hay_que_avisar', 'texto_del_correo', 'correo_diario',
           'avisar_dano_bloqueante', 'escuchar_danos_bloqueantes', 'barrido_diario',
           'que_falta', 'estado_del_barrido']


def init_scheduler(app):
    """Cron diario 06:00 Bogotá — el barrido de documentos por vencer.

    Hasta el 2026-08-15 `barrer_documentos_por_vencer` tenía **un solo caller en
    producción**: el botón «Revisar vencimientos ahora». El aviso de SOAT y
    tecnomecánica dependía de que una persona se acordara de entrar al tab y
    apretarlo.

    La ventana es de 15 días (`DIAS_AVISO_DOCUMENTO`) — que es lo que tarda una
    cita de tecnomecánica en Neiva. Si nadie aprieta el botón dentro de esa
    ventana, el camión queda parado o lo inmoviliza la autoridad.

    El propio docstring del endpoint decía que existía *«para poder ejercerlo
    antes de encender el cron»*. El cron nunca se escribió.

    ## Sobre la regla 10 de este módulo

    «Todo cron que escribe nace apagado.» Se cumple sin una variable nueva: el
    barrido ya consulta `avisos_encendidos()` y con `FLOTA_AVISOS` en false
    devuelve `{'revisados': 0, ..., 'motivo': 'FLOTA_AVISOS no está en true'}`
    sin mandar nada. Agregar un segundo interruptor daría dos sitios donde
    apagar lo mismo, y el segundo se olvida.

    Lo que sí cambia es que **el silencio deja de ser total**: hoy, apagado o
    no, no había ni un log diario. Ahora hay una línea por día diciendo qué
    pasó, que es la diferencia entre degradarse y callarse.
    """
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[FLOTA_AVISOS] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            from app.utils.lock import LOCK_FLOTA_AVISOS, advisory_lock

            # Clave propia del registro de app/utils/lock.py. Decía «2016 —
            # libre», y dos semanas después reposición tomó el mismo 2016. Con `--workers=2` dos procesos disparan el mismo
            # cron, y un aviso duplicado por WhatsApp a las 6 de la mañana es la
            # forma más rápida de que alguien silencie el canal.
            with advisory_lock(LOCK_FLOTA_AVISOS, 'flota_avisos_barrido') as tomado:
                if not tomado:
                    return {'motivo': 'otro proceso lo está corriendo',
                            'config': configuracion_de_este_servicio()}
                # Si revienta, el latido lo registra como fallo (antes se
                # atrapaba y el latido decía «bien»).
                r = barrido_diario()
                if r.get('motivo'):
                    logger.info('[FLOTA_AVISOS] barrido sin efecto — %s', r['motivo'])
                elif r.get('enviados') or r.get('fallidos') or r.get('sin_destinatario'):
                    # `sin_destinatario` no deja fila: el aviso no se manda Y no
                    # queda rastro de que ese vencimiento pasó por la ventana.
                    # Por eso va en el mismo nivel que un fallo.
                    logger.warning(
                        '[FLOTA_AVISOS] %s en ventana · %s enviados · %s fallidos · '
                        '%s sin destinatario%s',
                        r.get('en_ventana'), r.get('enviados'), r.get('fallidos'),
                        r.get('sin_destinatario'),
                        ' (SIMULADO)' if r.get('simulado') else '')
                else:
                    logger.info('[FLOTA_AVISOS] %s documentos revisados, ninguno '
                                'en ventana', r.get('revisados'))
                # El resumen va al latido (`con_latido` guarda un dict): es lo
                # que el panel de la web lee del worker.
                return r

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido  # P1-11
    scheduler.add_job(
        con_latido('flota_avisos_barrido', _job), CronTrigger(hour=6, minute=0),
        id='flota_avisos_barrido', replace_existing=True,
        max_instances=1, misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info('[FLOTA_AVISOS] Scheduler activo — barrido diario 06:00 Bogotá')
    # El retorno es lo que `_registrar_scheduler` usa para no mentir sobre lo
    # que está corriendo. Ver `app/__init__.py`.
    return scheduler
