"""¿Esta caja tiene documento en Siesa? ¿Puede salir? **Una política.**

## La clase

*«No pude preguntarle a Siesa» leído como «no existe» o como «ya está hecho».*
Las dos lecturas equivocadas cuestan lo mismo — mercancía sin documento fiscal
o un documento duplicado — y cada operación que deshace o rehace un empaque las
contestaba a su manera:

| Operación | Qué miraba | Qué dejaba pasar |
|---|---|---|
| `PackingService.cancelar` | jobs vivos | una caja con remisión y el job FALLIDO → otro empaque del mismo pedido → **RM #2** |
| `resetear_siesa` | `siesa_triggered` | la remisión ya creada con la factura fallida |
| `iniciar-despacho` / `crear_manual` | empaques no cancelados | un empaque CANCELADO con remisión |
| «devuelto al estante» | `rm_consec` o `siesa_triggered` | (bien, pero con su propia copia de la regla) |

Y el muelle dejaba subir cualquier bulto con `siesa_triggered`, bandera que la
rama `244328-AUTO` y la reconciliación por estado 9 encendían **sin remisión y
sin factura**.

## Las dos preguntas

- `tiene_documento_en_siesa(tarea)` — ¿hay (o PUEDE haber) un documento en
  Siesa por esta caja? Cualquier rastro cuenta: `siesa_triggered`, remisión,
  factura, o el pre-flag del 142945 (`rm_enviada_at`: el POST salió y no se
  sabe si entró — Regla 0, se trata como que sí). Con documento, nadie cancela,
  resetea ni abre otro empaque del pedido.
- `despachable(tarea)` — ¿puede ir al muelle y a la ruta? **Decisión del dueño
  (2026-09-25): ningún bulto sale sin remisión Y factura confirmadas.**

Cada una tiene su gemela SQL (`filtro_tiene_documento`, `filtro_despachable`)
y un test exige que digan lo mismo sobre las mismas filas.

Trinquete: `tests/test_documento_fiscal.py` — por AST, toda función de `app/`
que cancela o reabre un empaque, crea uno, o arma la cola del muelle, llama a
la política.
"""
from contextlib import contextmanager
from datetime import datetime

#: Decisión del dueño (2026-09-25): si Siesa está caído y no se puede
#: facturar, se para todo. Texto del servidor; la pantalla lo pinta tal cual.
MENSAJE_SIESA_NO_DISPONIBLE = ('Siesa no está disponible: no se puede facturar, '
                               'la caja queda esperando.')


class DocumentoEnSiesa(ValueError):
    """La operación desharía o duplicaría un documento que existe (o puede
    existir) en Siesa. Es `ValueError` para que las rutas que ya traducen
    `ValueError` a 400/409 no cambien."""


# ─────────────────────────────────────────────────────────────────────────
# ¿Tiene documento?
# ─────────────────────────────────────────────────────────────────────────

def rm_resultado_desconocido(tarea) -> bool:
    """El POST del 142945 salió y no se identificó la remisión. **La RM puede
    existir**: nadie reenvía el 142945 hasta que alguien la identifique
    (facturar-rm-manual) o declare que no existe."""
    return bool(getattr(tarea, 'rm_enviada_at', None)) and not getattr(tarea, 'rm_consec', None)


def fe_confirmada(tarea) -> bool:
    """La factura existe: consecutivo conocido, o el 142943 respondió bien."""
    return bool(getattr(tarea, 'fe_consec', None) or getattr(tarea, 'fe_confirmada_at', None))


def tiene_documento_en_siesa(tarea) -> bool:
    if tarea is None:
        return False
    return bool(tarea.siesa_triggered or tarea.rm_consec or fe_confirmada(tarea)
                or getattr(tarea, 'rm_enviada_at', None))


def que_documento(tarea) -> str:
    """El documento en palabras, para el mensaje."""
    partes = []
    if tarea.rm_consec:
        partes.append(f'la remisión {tarea.rm_tipo or "RM"}-{tarea.rm_consec}')
    elif rm_resultado_desconocido(tarea):
        partes.append('una remisión enviada a Siesa sin confirmar')
    if tarea.fe_consec:
        partes.append(f'la factura {tarea.fe_tipo or "FE"}-{tarea.fe_consec}')
    elif getattr(tarea, 'fe_confirmada_at', None):
        partes.append('la factura')
    if not partes and tarea.siesa_triggered:
        partes.append('un despacho marcado como procesado en Siesa')
    return ' y '.join(partes) or 'un documento'


def exigir_sin_documento(tarea, accion: str):
    """Levanta `DocumentoEnSiesa` si la caja tiene (o puede tener) documento."""
    if tiene_documento_en_siesa(tarea):
        raise DocumentoEnSiesa(
            f'No se puede {accion}: la caja {tarea.codigo} ({tarea.numero_pedido_siesa}) '
            f'tiene {que_documento(tarea)} en Siesa. '
            + ('Identifique la remisión en Siesa y regístrela con «Facturar RM manual».'
               if rm_resultado_desconocido(tarea) else
               'Complete la factura desde «Facturar remisión»; si el pedido no debía '
               'salir, anule los documentos en Siesa primero.'))


def exigir_pedido_sin_documento(numero_pedido: str, accion: str, excluir_id: int = None):
    """Ningún empaque del pedido —cancelado incluido— tiene documento en Siesa.

    Solo puede haber un empaque vivo por pedido (índice `uq_packing_pedido_activo`),
    así que el segundo empaque de un pedido nace siempre sobre uno CANCELADO: si
    ese tenía remisión, el nuevo emitiría la segunda."""
    if not numero_pedido:
        return
    from app.models.packing import TareaPacking
    q = TareaPacking.query.filter(TareaPacking.numero_pedido_siesa == numero_pedido,
                                  filtro_tiene_documento())
    if excluir_id is not None:
        q = q.filter(TareaPacking.id != excluir_id)
    previo = q.order_by(TareaPacking.id).first()
    if previo is not None:
        raise DocumentoEnSiesa(
            f'No se puede {accion}: el pedido {numero_pedido} ya tiene '
            f'{que_documento(previo)} en Siesa por la caja {previo.codigo} '
            f'(estado {previo.estado}). Otra caja emitiría un segundo documento.')


def filtro_tiene_documento(T=None):
    """Gemela SQL de `tiene_documento_en_siesa`."""
    from sqlalchemy import or_
    if T is None:
        from app.models.packing import TareaPacking as T
    return or_(T.siesa_triggered.is_(True), T.rm_consec.isnot(None),
               T.fe_consec.isnot(None), T.fe_confirmada_at.isnot(None),
               T.rm_enviada_at.isnot(None))


# ─────────────────────────────────────────────────────────────────────────
# ¿Puede salir?
# ─────────────────────────────────────────────────────────────────────────

def _es_traslado(tarea) -> bool:
    return (getattr(tarea, 'tipo_documento', None) or '').upper() == 'TRASLADO'


def despachable(tarea) -> bool:
    """Decisión del dueño: un pedido sale al muelle y a la ruta solo con
    remisión Y factura confirmadas. El traslado conserva su regla (su
    documento es el STS, otro circuito)."""
    if tarea is None:
        return False
    if _es_traslado(tarea):
        return bool(tarea.siesa_triggered)
    return bool(tarea.siesa_triggered and tarea.rm_consec and fe_confirmada(tarea))


def motivo_no_despachable(tarea):
    """`None` si puede salir; si no, por qué — en palabras, para el muelle."""
    if despachable(tarea):
        return None
    pedido = getattr(tarea, 'numero_pedido_siesa', None) or getattr(tarea, 'codigo', '')
    if _es_traslado(tarea):
        return f'El traslado {pedido} aún no tiene su documento de salida en Siesa.'
    if not tarea.rm_consec:
        return (f'El pedido {pedido} no tiene remisión confirmada en Siesa: '
                f'no puede salir.')
    return (f'El pedido {pedido} tiene la remisión {tarea.rm_tipo or "RM"}-'
            f'{tarea.rm_consec} pero no la factura confirmada: no puede salir.')


def filtro_despachable(T=None):
    """Gemela SQL de `despachable`."""
    from sqlalchemy import and_, or_
    if T is None:
        from app.models.packing import TareaPacking as T
    traslado = T.tipo_documento == 'TRASLADO'
    return or_(
        and_(traslado, T.siesa_triggered.is_(True)),
        and_(or_(T.tipo_documento.is_(None), T.tipo_documento != 'TRASLADO'),
             T.siesa_triggered.is_(True), T.rm_consec.isnot(None),
             or_(T.fe_consec.isnot(None), T.fe_confirmada_at.isnot(None))),
    )


# ─────────────────────────────────────────────────────────────────────────
# ¿Se puede facturar ahora?
# ─────────────────────────────────────────────────────────────────────────

def ventana_facturacion():
    """La ventana de la Regla 14: **la de Siesa, que es una**
    (`ventana_siesa.ventana()`, `None` = sin restricción). No es una
    definición propia: facturar es hablarle a Siesa como cualquier otro."""
    from app.services.ventana_siesa import ventana
    return ventana()


def _ahora_bogota():
    """El reloj de la ventana. Aparte para que los tests lo fijen (el reloj
    del CI no es el de Bogotá: `tests/conftest.py` lo deja en horario)."""
    from app.utils.fecha import ahora_bogota
    return ahora_bogota()


def siesa_disponible_para_facturar(ahora_bog: datetime = None):
    """`(True, None)` o `(False, motivo)`. **Sin red**: circuito abierto o
    fuera de ventana. En simulación no hay Siesa que cuidar.

    Con el circuito abierto se niega **solo mientras no toque probar**
    (`circuito_admite_intento`). Vencido el intervalo, deja pasar: la primera
    llamada que haga el que pregunta (el precheck del cierre, la reconciliación
    de la DLQ) es el probe que cierra o reabre el circuito. Leer
    `_cb_state == 'OPEN'` y negarse sin llamar dejaba el cierre en «Siesa no
    está disponible» para siempre en un worker cuya única charla con Siesa
    eran los cierres (H1, 2026-09-26): cada proceso de gunicorn tiene su
    propio breaker."""
    from app.services.connekta_gateway import connekta
    if connekta.modo_simulacion:
        return True, None
    if not connekta.circuito_admite_intento():
        return False, f'{MENSAJE_SIESA_NO_DISPONIBLE} (Siesa no responde desde hace un rato.)'
    if ahora_bog is None:
        ahora_bog = _ahora_bogota()
    from app.services.ventana_siesa import ventana_abierta
    if not ventana_abierta(ahora_bog):
        # Solo con `SIESA_VENTANA` configurada: sin ella no hay horario que
        # frene la facturación (tanda 2 · H).
        ini, fin = ventana_facturacion()
        return False, (f'{MENSAJE_SIESA_NO_DISPONIBLE} (Siesa factura de '
                       f'{ini.strftime("%H:%M")} a {fin.strftime("%H:%M")}.)')
    return True, None


# ─────────────────────────────────────────────────────────────────────────
# ¿Hay un envío vivo? — la otra mitad de «¿tiene documento?» (H2, 2026-09-26)
# ─────────────────────────────────────────────────────────────────────────
#
# Desde m048fiscal una caja cerrada queda VERIFICADA **sin** documento hasta
# que el DESPACHO_F470 sale de la cola. `exigir_sin_documento` la ve limpia
# (no hay pre-flag todavía) y `resetear_siesa` le borraba los bultos: después
# el job emitía RM + FE y la caja quedaba DESPACHADO sin nada que cargar.
# Cancelar ya lo miraba; resetear y re-confirmar no. **Una pregunta.**

class EnvioEnCurso(ValueError):
    """La caja tiene un envío a Siesa en la cola: deshacerla ahora la dejaría
    sin bultos (o con otras cantidades) cuando el envío emita. `ValueError`
    para que las rutas que traducen `ValueError` a 400/409 no cambien."""


def envio_vivo(tarea, bloquear: bool = True):
    """El `SiesaJob` vivo (PENDIENTE/PROCESANDO/REINTENTANDO) de la caja, o
    `None`. Con `bloquear`, `FOR UPDATE`: el DLQ no lo toma mientras quien
    pregunta decide."""
    if tarea is None or getattr(tarea, 'id', None) is None:
        return None
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    q = SiesaJob.query.filter(SiesaJob.referencia_tipo == 'TareaPacking',
                              SiesaJob.referencia_id == tarea.id,
                              SiesaJob.estado.in_(sorted(EstadoSiesaJob.ACTIVOS)))
    if bloquear:
        q = q.with_for_update()
    return q.order_by(SiesaJob.id).first()


def exigir_sin_envio_vivo(tarea, accion: str, permitir=None):
    """Levanta `EnvioEnCurso` si la caja tiene un envío vivo. `permitir(job)`
    —si se pasa y devuelve verdadero— lo deja pasar y devuelve el job (el
    único caso hoy: cancelar la caja que espera a cartera, que descarta ese
    job). Devuelve el job permitido o `None`."""
    job = envio_vivo(tarea)
    if job is None:
        return None
    if permitir is not None and permitir(job):
        return job
    raise EnvioEnCurso(
        f'No se puede {accion}: la caja {tarea.codigo} ({tarea.numero_pedido_siesa}) '
        f'está en la cola de facturación de Siesa (envío {job.id}, {job.estado}). '
        f'Espere a que termine: si falla, la caja queda para reintentar.')


# ─────────────────────────────────────────────────────────────────────────
# ¿En qué va la emisión? — lo que la pantalla pinta (H2b, 2026-09-26)
# ─────────────────────────────────────────────────────────────────────────
#
# Las pantallas deducían «⚠ Error Siesa» de `VERIFICADO && !siesa_triggered`,
# que desde m048fiscal es también «en cola, esperando a Siesa». El servidor
# dice cuál de los dos es; la pantalla no adivina.

EMISION_EN_COLA = 'EN_COLA'                    # job vivo: esperar, sin botones
EMISION_RETENIDO = 'RETENIDO'                  # job en pausa por cartera
EMISION_FALLIDO = 'FALLIDO'                    # sin documento: reintentar / limpiar
EMISION_SIN_VERIFICAR = 'SIN_VERIFICAR'        # 142945 enviado sin identificar la RM
EMISION_REMISION_SIN_FACTURA = 'REMISION_SIN_FACTURA'   # RM sí, FE no: «Facturar remisión»
EMISION_FACTURA_SIN_REMISION = 'FACTURA_SIN_REMISION'   # FE sí, RM sin identificar
EMISION_DESPACHADO = 'DESPACHADO'              # RM + FE: puede salir

#: Los estados en que una persona tiene que hacer algo en Siesa → Recuperación.
EMISION_REQUIERE_ADMIN = (EMISION_FALLIDO, EMISION_SIN_VERIFICAR,
                          EMISION_REMISION_SIN_FACTURA, EMISION_FACTURA_SIN_REMISION)


def estado_emision(tarea, jobs=None, retenido: bool = None):
    """El estado de la emisión fiscal de una caja, o `None` si nunca se
    cerró. `jobs`: sus DESPACHO_F470 (si el que llama ya los cargó en lote);
    `retenido`: si cartera la tiene retenida (ídem).

    Una caja cuya RM se envió sin identificar pasa a SIN_VERIFICAR cuando la
    gracia de la Regla 20 vence **aunque su job siga esperando**: mientras
    no se pueda preguntar el job espera sin volverse FALLIDO (H4), y la
    salida humana tiene que estar a la vista igual."""
    if tarea is None:
        return None
    if _es_traslado(tarea):
        return EMISION_DESPACHADO if tarea.siesa_triggered else None
    if despachable(tarea):
        return EMISION_DESPACHADO
    if (tarea.siesa_triggered or fe_confirmada(tarea)) and not tarea.rm_consec:
        return EMISION_FACTURA_SIN_REMISION
    if jobs is None:
        from app.models.siesa_job import SiesaJob
        jobs = (SiesaJob.query.filter(SiesaJob.tipo == 'DESPACHO_F470',
                                      SiesaJob.referencia_tipo == 'TareaPacking',
                                      SiesaJob.referencia_id == tarea.id)
                .order_by(SiesaJob.id.desc()).all())
    from app.models.siesa_job import EstadoSiesaJob
    vivo = next((j for j in jobs if j.estado in EstadoSiesaJob.ACTIVOS), None)
    ultimo = jobs[0] if jobs else None
    sin_identificar = rm_resultado_desconocido(tarea)
    if vivo is not None:
        if sin_identificar and _gracia_vencida(tarea):
            return EMISION_SIN_VERIFICAR
        if retenido is None and vivo.estado == EstadoSiesaJob.PENDIENTE \
                and not tiene_documento_en_siesa(tarea) and tarea.pedido_clave:
            from app.services import cartera_service as _cartera
            retenido = _cartera.retencion_viva(tarea.pedido_clave) is not None
        if retenido and vivo.estado == EstadoSiesaJob.PENDIENTE:
            return EMISION_RETENIDO
        return EMISION_EN_COLA
    if sin_identificar:
        return EMISION_SIN_VERIFICAR
    if tarea.rm_consec:
        return EMISION_REMISION_SIN_FACTURA
    if ultimo is not None and ultimo.estado == EstadoSiesaJob.FALLIDO:
        return EMISION_FALLIDO
    return None


def _gracia_vencida(tarea) -> bool:
    from app.services.despacho_parcial_service import GRACIA_IDENTIFICAR_RM
    enviada = getattr(tarea, 'rm_enviada_at', None)
    return enviada is not None and datetime.utcnow() - enviada >= GRACIA_IDENTIFICAR_RM


def estados_emision(tareas, retenidos=None) -> dict:
    """`{tarea_id: estado}` en lote: una consulta de jobs para todas.
    `retenidos`: `{numero_pedido: ...}` de `cartera_service.resumen_por_pedido`
    si el que llama ya lo tiene."""
    tareas = [t for t in tareas if t is not None and t.id is not None]
    if not tareas:
        return {}
    from app.models.siesa_job import SiesaJob
    por_tarea = {}
    for j in (SiesaJob.query.filter(SiesaJob.tipo == 'DESPACHO_F470',
                                    SiesaJob.referencia_tipo == 'TareaPacking',
                                    SiesaJob.referencia_id.in_([t.id for t in tareas]))
              .order_by(SiesaJob.id.desc()).all()):
        por_tarea.setdefault(j.referencia_id, []).append(j)
    out = {}
    for t in tareas:
        ret = None
        if retenidos is not None:
            ret = bool(retenidos.get(t.numero_pedido_siesa))
        out[t.id] = estado_emision(t, jobs=por_tarea.get(t.id, []), retenido=ret)
    return out


# ─────────────────────────────────────────────────────────────────────────
# Un carril a la vez por pedido (H3, 2026-09-26)
# ─────────────────────────────────────────────────────────────────────────
#
# `/facturar-remision` y `/facturar-rm-manual` posteaban el 142943 sin mirar
# si Siesa estaba disponible ni si el DESPACHO_F470 de la misma caja corría a
# la vez. El anti-duplicado de FE es por pedido y Siesa tarda 30–60 s en
# mostrar la factura recién hecha: dos carriles a la vez = FE duplicada.
#
# El candado es **por pedido**, no el de la DLQ: la DLQ lo tiene tomado casi
# todo el minuto en temporada, y con él un administrador recibía «intente en
# un minuto» cuatro veces de cinco. Por pedido, choca solo con quien emite
# ESE pedido — que es exactamente lo que no puede pasar.

class PermisoDeEmision:
    """Lo que rinde `emision_exclusiva`: verdadero si se puede emitir."""

    def __init__(self, ok: bool, motivo: str = None, status: int = 200,
                 estado: str = None):
        self.ok, self.motivo, self.status, self.estado = ok, motivo, status, estado

    def __bool__(self):
        return self.ok


MENSAJE_EMISION_EN_CURSO = ('Otro envío a Siesa de este pedido está en curso. '
                            'Intente de nuevo en un minuto.')


def _clave_emision(tarea) -> int:
    import zlib
    from app.utils.lock import RANGO_EMISION_PEDIDO, clave_en_rango
    ident = (getattr(tarea, 'pedido_clave', None) or getattr(tarea, 'numero_pedido_siesa', None)
             or f'tarea-{tarea.id}')
    return clave_en_rango(RANGO_EMISION_PEDIDO,
                          zlib.crc32(str(ident).encode('utf-8')) % RANGO_EMISION_PEDIDO[1])



@contextmanager
def emision_exclusiva(tarea, etiqueta: str = 'emision'):
    """**Todo carril que postea el 244328/142945/142943 entra por acá** (la
    DLQ, `/despachar`, `/facturar-remision`, `/facturar-rm-manual` y la
    declaración de RM inexistente, que quita el pre-flag). Rinde un
    `PermisoDeEmision`:

    · Siesa no disponible (circuito, ventana) → falso, 503;
    · otro carril emitiendo el mismo pedido → falso, 409;
    · si no, verdadero, con el candado del pedido tomado hasta salir del `with`.

    Trinquete: `tests/test_fiscal_v2.py::TestTodoCarrilDeEmisionTomaElCandado`."""
    ok, motivo = siesa_disponible_para_facturar()
    if not ok:
        yield PermisoDeEmision(False, motivo, 503, 'SIESA_NO_DISPONIBLE')
        return
    from app.utils.lock import advisory_lock
    with advisory_lock(_clave_emision(tarea), f'emision_{etiqueta}') as tomado:
        if not tomado:
            yield PermisoDeEmision(False, MENSAJE_EMISION_EN_CURSO, 409, 'EMISION_EN_CURSO')
            return
        yield PermisoDeEmision(True)
