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
    fuera de ventana. En simulación no hay Siesa que cuidar."""
    from app.services.connekta_gateway import connekta
    if connekta.modo_simulacion:
        return True, None
    if connekta._cb_state == 'OPEN':
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
