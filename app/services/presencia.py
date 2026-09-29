"""**¿Quién está trabajando ahora?** — una sola respuesta para todo el sistema.

## Por qué existe (2026-09-27)

Hasta hoy nada en el WMS sabía si una persona había venido. El trabajo se le
daba a quien estuviera `activo` en el maestro de usuarios, y `activo` no
significa «vino hoy»: significa «tiene cuenta». Consecuencias, todas medidas en
código:

- el 2º conteo (CC2) se le ponía al operario de id más bajo del almacén, viniera
  o no: con él incapacitado, **todo** CC2 quedaba asignado a alguien que no iba
  a contar, y el barrido de zombis no lo soltaba nunca (solo mira EN_PROCESO);
- «Asignar N pendientes» cargaba el lote entero a una persona elegida de una
  lista donde aparecían también quienes no habían venido;
- un traslado aprobado «para Pedro» quedaba con sus tareas pegadas a Pedro,
  aunque Pedro estuviera de vacaciones.

## La política

Una persona está **disponible** si y solo si las tres cosas son ciertas:

1. está activa en el maestro de usuarios;
2. no tiene una ausencia declarada que cubra hoy (incapacidad, vacaciones,
   permiso… con fecha de regreso — `AusenciaUsuario`);
3. dio señal de vida en las últimas `VENTANA_SENAL_HORAS` horas: toda
   petición autenticada que escribe la renueva (`registrar_senal_de_peticion`,
   desde el `before_request`), y pedir trabajo también (`registrar_senal`, en el
   dispensador). Los GET no: un GET no escribe.

La señal es automática a propósito: la ausencia que nadie declara —el que
simplemente no llegó— es la más común, y una política que dependa de que el
líder se acuerde de marcarla falla justo ese día. La declarada existe para lo
que se sabe de antemano y para que la pantalla diga **hasta cuándo**.

La ventana es 2 h, la misma del barrido de conteos zombi
(`ConteoService.CONTEO_INACTIVIDAD_HORAS`): la PWA del operario pregunta por
trabajo cada pocos segundos mientras está abierta, así que dos horas sin una
sola petición es alguien que no está.

## Dos escrituras de la misma regla

`estado()` la contesta en Python (para una persona) y `condicion_sql_disponible`
en SQL (para filtrar en una consulta). Son la misma política escrita dos veces
porque una consulta no puede llamar a Python; `tests/test_asignacion_presencia.py`
exige que las dos contesten igual sobre la matriz completa de casos. Si divergen,
el test se pone rojo.
"""
import logging
from datetime import datetime, timedelta

from app.extensions import db

logger = logging.getLogger(__name__)

#: Horas sin señal tras las que alguien deja de estar disponible.
VENTANA_SENAL_HORAS = 2

EN_TURNO = 'EN_TURNO'
SIN_SENAL = 'SIN_SENAL'
AUSENTE = 'AUSENTE'
INACTIVO = 'INACTIVO'

_NO_CARGADA = object()


def _ahora(ahora=None) -> datetime:
    return ahora or datetime.utcnow()


def umbral_senal(ahora=None) -> datetime:
    return _ahora(ahora) - timedelta(hours=VENTANA_SENAL_HORAS)


def _dia(ahora=None):
    from app.utils.fecha import dia_operativo_de
    return dia_operativo_de(_ahora(ahora))


# ─────────────────────────────────────────────────────────────────────────────
# La señal
# ─────────────────────────────────────────────────────────────────────────────

def registrar_senal(usuario_id, ahora=None) -> None:
    """Esta persona está haciendo algo ahora. **Hace commit.**

    Escribe solo si la señal guardada tiene más de un minuto: con la PWA
    preguntando cada pocos segundos, escribir en cada petición sería una
    escritura por segundo por operario para no cambiar nada que importe.
    Nunca levanta: perder una señal cuesta, a lo sumo, que alguien figure
    «sin señal» un rato; tumbar la petición que la trae cuesta el trabajo.
    """
    if not usuario_id:
        return
    t = _ahora(ahora)
    try:
        r = db.session.execute(
            db.text('UPDATE usuarios SET ultima_senal_at = :t '
                    'WHERE id = :uid AND (ultima_senal_at IS NULL OR ultima_senal_at < :antes)'),
            {'t': t, 'uid': int(usuario_id), 'antes': t - timedelta(minutes=1)})
        if r.rowcount:
            cerrar_ausencia_si_volvio(int(usuario_id), ahora=t)
        db.session.commit()
    except Exception as e:  # pragma: no cover — defensivo
        db.session.rollback()
        logger.warning('[PRESENCIA] no se pudo registrar la señal de #%s: %s', usuario_id, e)


def cerrar_ausencia_si_volvio(usuario_id: int, *, ahora=None):
    """La persona está trabajando (dio señal) y tiene una ausencia vigente
    que **no dice hasta cuándo** y **empezó antes de hoy**: volvió. La ausencia
    se cierra sola —queda anulada, con «regresó antes» en la bitácora— y la
    persona recibe trabajo como cualquiera. No hace commit.

    Existe porque «Regresa el» vacío es lo normal en una incapacidad (nadie
    sabe cuándo termina), y sin esto el que volvió trabajaba con la ausencia
    puesta: el barrido le quitaba de las manos lo que estaba contando cada
    15 min hasta que el líder se acordara de quitarla (validación n1, P1-A).

    Lo que **no** se cierra solo, a propósito:
    - una ausencia con fecha de regreso futura: la declaró alguien que sabe
      cuándo vuelve (unas vacaciones); si volvió antes, que la quite el líder;
    - una que empezó **hoy**: puede ser la salida anticipada de quien está
      terminando lo que tiene. Mientras tanto no recibe trabajo nuevo
      (`motivo_sin_trabajo_nuevo`) y lo que tiene en curso no se le quita
      (`asignacion.en_curso_abandonado`).

    Devuelve la ausencia cerrada, o `None`.
    """
    from app.services.bitacora import registrar_accion
    t = _ahora(ahora)
    hoy = _dia(t)
    a = ausencias_vigentes([usuario_id], hoy).get(usuario_id)
    if a is None or a.regreso is not None or a.desde >= hoy:
        return None
    antes = a.to_dict()
    a.anulada_en = t
    a.anulada_por_id = None
    a.motivo_anulacion = 'Regresó antes: volvió a trabajar (la ausencia no tenía fecha de regreso)'
    registrar_accion('ANULAR', a, usuario_id=None, motivo=a.motivo_anulacion,
                     antes=antes, despues=a.to_dict(), origen='señal de trabajo')
    logger.info('[PRESENCIA] #%s volvió: se cierra su ausencia %s sin fecha de regreso',
                usuario_id, a.id)
    return a


def motivo_sin_trabajo_nuevo(usuario, *, ahora=None):
    """Por qué a ESTA persona, que está pidiendo trabajo, no se le da una tarea
    nueva — o `None`. **Una política** para toda puerta de pull (cola unificada,
    abastecedor, siguiente-tarea): el texto es el que ve en su pantalla.

    Solo la ausencia vigente la frena (la que no se cerró sola por
    `cerrar_ausencia_si_volvio`). Lo que ya tiene empezado lo puede terminar.
    """
    if usuario is None:
        return None
    e = estado(usuario, ahora=ahora)
    if e['codigo'] != AUSENTE:
        return None
    return (f'Usted figura ausente ({texto_ausencia_de(e["ausencia"])}), así que no recibe '
            'tareas nuevas. Si ya volvió a trabajar, pídale a su jefe que le quite la '
            'ausencia en Operarios.')


def texto_ausencia_de(d: dict) -> str:
    """`texto_ausencia` a partir del `to_dict()` de la ausencia."""
    from app.models.ausencia import MotivoAusencia
    base = MotivoAusencia.TEXTO.get(d.get('motivo'), d.get('motivo') or 'ausencia').lower()
    if d.get('regreso'):
        r = datetime.strptime(d['regreso'], '%Y-%m-%d').date()
        return f'{base} — regresa el {_fecha_corta(r)}'
    return f'{base} — sin fecha de regreso'


def registrar_senal_de_peticion() -> None:
    """Para el `before_request`: si la petición **escribe** (POST, PUT, PATCH,
    DELETE) y trae un JWT válido, su dueño dio señal. Sin JWT, o con uno
    vencido o inválido, no hace nada: la ruta decidirá qué contestar.

    Los GET no dejan señal: **un GET no escribe** (`test_lista_paradas_no_escribe`,
    la regla del repo), y la señal es una escritura. No hace falta: quien
    trabaja escanea y confirma (POST), y pedir trabajo —el sondeo de la PWA,
    que sí es GET— la deja desde el servicio (`MobileService.get_tarea_actual`),
    que ya escribe porque asigna.
    """
    from flask import request
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return
    if not request.path.startswith('/api/'):
        return
    cabecera = request.headers.get('Authorization', '')
    if not cabecera.startswith('Bearer '):
        return
    try:
        from flask_jwt_extended import decode_token
        uid = decode_token(cabecera[7:].strip()).get('sub')
        uid = int(uid)
    except Exception:
        return
    registrar_senal(uid)


# ─────────────────────────────────────────────────────────────────────────────
# Ausencias
# ─────────────────────────────────────────────────────────────────────────────

def ausencias_vigentes(usuario_ids, dia=None) -> dict:
    """`{usuario_id: AusenciaUsuario}` de las ausencias que cubren `dia`."""
    from app.models.ausencia import AusenciaUsuario
    ids = [int(u) for u in usuario_ids if u]
    if not ids:
        return {}
    d = dia or _dia()
    filas = (AusenciaUsuario.query
             .filter(AusenciaUsuario.usuario_id.in_(ids),
                     AusenciaUsuario.anulada_en.is_(None),
                     AusenciaUsuario.desde <= d,
                     db.or_(AusenciaUsuario.regreso.is_(None), AusenciaUsuario.regreso > d))
             .order_by(AusenciaUsuario.desde.desc(), AusenciaUsuario.id.desc())
             .all())
    out = {}
    for a in filas:
        out.setdefault(a.usuario_id, a)
    return out


def _fecha_corta(d) -> str:
    return d.strftime('%d/%m') if d else ''


def texto_ausencia(a) -> str:
    from app.models.ausencia import MotivoAusencia
    base = MotivoAusencia.TEXTO.get(a.motivo, a.motivo).lower()
    if a.regreso:
        return f'{base} — regresa el {_fecha_corta(a.regreso)}'
    return f'{base} — sin fecha de regreso'


# ─────────────────────────────────────────────────────────────────────────────
# La política
# ─────────────────────────────────────────────────────────────────────────────

def estado(usuario, *, ahora=None, ausencia=_NO_CARGADA) -> dict:
    """Presencia de `usuario` ahora. **La** respuesta.

    `{codigo, disponible, texto, visto_at, ausencia}`. `ausencia` puede venir
    precargada (`estados` la trae en una consulta para muchos).
    """
    t = _ahora(ahora)
    visto = getattr(usuario, 'ultima_senal_at', None)
    base = {'visto_at': visto.isoformat() if visto else None, 'ausencia': None}
    if usuario is None or not usuario.activo:
        return {**base, 'codigo': INACTIVO, 'disponible': False,
                'texto': 'está inactivo en el maestro de usuarios'}
    if ausencia is _NO_CARGADA:
        ausencia = ausencias_vigentes([usuario.id], _dia(t)).get(usuario.id)
    if ausencia is not None:
        return {**base, 'codigo': AUSENTE, 'disponible': False,
                'texto': f'está ausente: {texto_ausencia(ausencia)}',
                'ausencia': ausencia.to_dict()}
    if visto is None or visto < umbral_senal(t):
        if visto is None:
            texto = 'no ha dado señal (no ha pedido tarea ni registrado nada en la aplicación)'
        else:
            from app.utils.fecha import TZ_BOGOTA
            from datetime import timezone
            local = visto.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA)
            texto = f'sin señal desde el {local.strftime("%d/%m %H:%M")}'
        return {**base, 'codigo': SIN_SENAL, 'disponible': False, 'texto': texto}
    return {**base, 'codigo': EN_TURNO, 'disponible': True, 'texto': 'en turno'}


def estados(usuarios, *, ahora=None) -> dict:
    """`{usuario_id: estado}` para muchos, con una sola consulta de ausencias."""
    usuarios = [u for u in usuarios if u is not None]
    aus = ausencias_vigentes([u.id for u in usuarios], _dia(ahora))
    return {u.id: estado(u, ahora=ahora, ausencia=aus.get(u.id)) for u in usuarios}


def esta_disponible(usuario, *, ahora=None) -> bool:
    return estado(usuario, ahora=ahora)['disponible']


def condicion_sql_disponible(columna_usuario_id, *, ahora=None):
    """La misma política, como condición SQL sobre una columna de id de
    usuario: `columna IN (usuarios disponibles)`. Ver el docstring del
    módulo: el test exige que coincida con `estado()`."""
    from app.models.ausencia import AusenciaUsuario
    from app.models.usuario import Usuario
    t = _ahora(ahora)
    d = _dia(t)
    ausente = (db.session.query(AusenciaUsuario.id)
               .filter(AusenciaUsuario.usuario_id == Usuario.id,
                       AusenciaUsuario.anulada_en.is_(None),
                       AusenciaUsuario.desde <= d,
                       db.or_(AusenciaUsuario.regreso.is_(None), AusenciaUsuario.regreso > d))
               .correlate(Usuario)
               .exists())
    disponibles = (db.session.query(Usuario.id)
                   .filter(Usuario.activo.is_(True),
                           Usuario.ultima_senal_at.isnot(None),
                           Usuario.ultima_senal_at >= umbral_senal(t),
                           db.not_(ausente)))
    return columna_usuario_id.in_(disponibles.scalar_subquery())


# ─────────────────────────────────────────────────────────────────────────────
# Declarar y anular ausencias
# ─────────────────────────────────────────────────────────────────────────────

def _parse_fecha(valor, campo):
    from datetime import date
    if valor is None or valor == '':
        return None
    if isinstance(valor, date):
        return valor
    try:
        return datetime.strptime(str(valor), '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f'{campo} inválida: {valor!r} (formato AAAA-MM-DD)')


def declarar_ausencia(usuario_id: int, motivo: str, *, por_id: int, desde=None,
                      regreso=None, nota: str = None) -> dict:
    """Registra una ausencia y, si cubre hoy, **devuelve a la cola** lo que la
    persona tenía asignado (`asignacion.devolver_trabajo_de`). Hace commit.

    `desde` por defecto es hoy (día operativo de Bogotá). `regreso` es el
    primer día de vuelta; sin él la ausencia no vence sola.
    """
    from app.models.ausencia import AusenciaUsuario, MotivoAusencia
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, usuario_id)
    if u is None:
        raise LookupError(f'Usuario {usuario_id} no encontrado')
    motivo = (motivo or '').strip().upper()
    if motivo not in MotivoAusencia.VALIDOS:
        raise ValueError(f'Motivo de ausencia inválido: {motivo!r} '
                         f'(válidos: {", ".join(MotivoAusencia.VALIDOS)})')
    hoy = _dia()
    d_desde = _parse_fecha(desde, 'La fecha de inicio') or hoy
    d_regreso = _parse_fecha(regreso, 'La fecha de regreso')
    if d_regreso is not None and d_regreso <= d_desde:
        raise ValueError('La fecha de regreso tiene que ser posterior al primer día de ausencia.')
    a = AusenciaUsuario(usuario_id=u.id, motivo=motivo, desde=d_desde, regreso=d_regreso,
                        nota=(nota or '').strip()[:300] or None, registrada_por_id=por_id,
                        registrada_en=datetime.utcnow())
    # La fila misma es el registro (quién, cuándo, por qué): la bitácora la
    # llevan las tareas que esto devuelve a la cola, una por una.
    db.session.add(a)
    db.session.flush()
    devuelto = {}
    if a.cubre(hoy):
        from app.services import asignacion
        devuelto = asignacion.devolver_trabajo_de(
            u.id, motivo=asignacion.MOTIVO_AUSENCIA, en_curso_desde=a.registrada_en, por_id=por_id)
    db.session.commit()
    logger.info('[PRESENCIA] Ausencia %s de #%s (%s → %s) por #%s; devuelto: %s',
                motivo, u.id, d_desde, d_regreso, por_id, devuelto)
    return {'ausencia': a.to_dict(), 'devuelto': devuelto}


def anular_ausencia(ausencia_id: int, *, por_id: int, motivo: str = None) -> dict:
    """La persona volvió antes, o la ausencia se declaró por error. No se
    borra: queda anulada con quién y por qué. Hace commit."""
    from app.models.ausencia import AusenciaUsuario
    from app.services.bitacora import registrar_accion
    a = db.session.get(AusenciaUsuario, ausencia_id)
    if a is None:
        raise LookupError(f'Ausencia {ausencia_id} no encontrada')
    if a.anulada_en is not None:
        raise ValueError('Esta ausencia ya estaba anulada.')
    antes = a.to_dict()
    a.anulada_en = datetime.utcnow()
    a.anulada_por_id = por_id
    a.motivo_anulacion = (motivo or '').strip()[:300] or None
    registrar_accion('ANULAR', a, usuario_id=por_id,
                     motivo=a.motivo_anulacion or 'Ausencia cerrada: la persona volvió',
                     antes=antes, despues=a.to_dict())
    db.session.commit()
    return a.to_dict()
