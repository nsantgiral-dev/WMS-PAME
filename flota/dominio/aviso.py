"""
Qué dice un aviso y cuándo se manda. Sin red, sin base, sin Gupshup.

Una plantilla de WhatsApp **no admite condicionales**: no puede concordar número
ni género, no puede formatear una fecha, no puede decidir si escribir "el" o
"la". Todo eso tiene que llegar resuelto dentro de la variable. Por eso el
armado del mensaje es una decisión de dominio y no una f-string en el adaptador
— ahí es donde se cuela *"venció hace 1 días"* y nadie lo revisa hasta que un
conductor lo lee.

Los parámetros son **posicionales**. Reordenarlos manda la fecha donde va la
placa y Gupshup responde exactamente igual de bien: `submitted`, sin error.
"""
from datetime import date
from typing import List, Sequence

from flota.dominio.errores import ErrorFlota

# ── Vocabulario ──────────────────────────────────────────────────────────────

#: Cuántos parámetros espera cada plantilla aprobada. El adaptador verifica
#: contra esto antes de mandar: una lista de largo distinto es un mensaje con
#: huecos, y Gupshup lo acepta sin quejarse.
PLANTILLAS = {
    'flota_documento_vence':      3,   # {{1}} placa, {{2}} documento, {{3}} fecha
    'flota_hallazgo_bloqueante':  2,   # {{1}} placa, {{2}} qué se encontró
    'flota_hallazgo_vencido':     3,   # {{1}} placa, {{2}} hallazgo, {{3}} antigüedad
}

#: Días de anticipación del aviso de vencimiento. Quince porque es lo que tarda
#: conseguir una cita de tecnomecánica en Neiva; menos no alcanza a evitar el
#: vehículo parado, que es el punto entero del aviso.
DIAS_AVISO_DOCUMENTO = 15

_MESES = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
          'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')

#: Cómo se lee cada tipo de documento en el mensaje. El cuerpo aprobado dice
#: "el documento {{2}}", no "el {{2}}", justamente para que "tecnomecánica" no
#: salga como "el tecnomecánica" — pero el nombre igual tiene que ser el que la
#: persona usa, no el código de la columna.
NOMBRE_DOCUMENTO = {
    'soat':              'SOAT',
    'rtm':               'tecnomecánica',
    'poliza_rc':         'póliza de responsabilidad civil',
    'tarjeta_propiedad': 'tarjeta de propiedad',
}


class AvisoInvalido(ErrorFlota):
    """El mensaje no se puede armar. Nunca se manda uno a medias."""


# ── Las tres trampas de redacción ────────────────────────────────────────────

def dias_en_palabras(dias: int) -> str:
    """`1` → `'1 día'`, `3` → `'3 días'`.

    La unidad va DENTRO de la variable porque la plantilla no puede concordar:
    con el entero suelto, el valor 1 produce "venció hace 1 días".
    """
    if dias < 0:
        raise AvisoInvalido(f'días negativos ({dias}): no hay forma de escribir eso')
    return f'{dias} día' if dias == 1 else f'{dias} días'


def fecha_en_palabras(f: date) -> str:
    """`date(2026, 8, 15)` → `'15 de agosto de 2026'`.

    Ni ISO ni numérico. El ISO se lee como un error del sistema —es el
    `2026-07-15` que salió a producción en cartera— y `15/08/2026` es ambiguo
    para quien está acostumbrado a leer mes primero.
    """
    if f is None:
        raise AvisoInvalido('no hay fecha que escribir')
    return f'{f.day} de {_MESES[f.month - 1]} de {f.year}'


def nombre_documento(tipo: str) -> str:
    """Cómo se nombra el documento en el mensaje.

    Sin `.get(tipo, tipo)`: un tipo desconocido mandaría `poliza_rc` con guion
    bajo a un WhatsApp que alguien lee. Es la regla 5 — o funciona, o falla.
    """
    if tipo not in NOMBRE_DOCUMENTO:
        raise AvisoInvalido(
            f'tipo de documento desconocido: {tipo!r}. Agregarlo a '
            f'NOMBRE_DOCUMENTO antes de avisar sobre él.'
        )
    return NOMBRE_DOCUMENTO[tipo]


# ── Armado de parámetros ─────────────────────────────────────────────────────

def parametros_documento_vence(placa: str, tipo: str, vencimiento: date) -> List[str]:
    """`['TGZ653', 'SOAT', '15 de agosto de 2026']`.

    El orden es el de la plantilla aprobada y no se puede cambiar sin volver a
    aprobarla en Meta.
    """
    if not (placa or '').strip():
        raise AvisoInvalido('un aviso sin placa no le sirve a nadie')
    return [placa.strip().upper(), nombre_documento(tipo), fecha_en_palabras(vencimiento)]


def validar_parametros(plantilla: str, parametros: Sequence[str]) -> None:
    """Cantidad y forma. Un hueco en el mensaje no produce error en Gupshup.

    Se valida acá y no en el adaptador para que el trinquete pueda ejercerlo sin
    red: la forma del mensaje es una regla, no un detalle de transporte.
    """
    if plantilla not in PLANTILLAS:
        raise AvisoInvalido(
            f'plantilla desconocida: {plantilla!r}. Las aprobadas son: '
            f'{", ".join(sorted(PLANTILLAS))}.'
        )
    esperados = PLANTILLAS[plantilla]
    if len(parametros) != esperados:
        raise AvisoInvalido(
            f'{plantilla} espera {esperados} parámetros y recibió {len(parametros)}. '
            f'Son posicionales: uno de menos corre todos los demás de lugar.'
        )
    for i, p in enumerate(parametros, 1):
        if not isinstance(p, str) or not p.strip():
            raise AvisoInvalido(
                f'{plantilla} parámetro {{{{{i}}}}} vacío o no textual ({p!r}). '
                f'WhatsApp lo manda igual y el mensaje sale con un hueco.'
            )
        if p.strip().lower() in ('none', 'null', 'sin_dato'):
            # `str(None)` es `'None'` y es truthy: el guard de forma que ya costó
            # una vez en cartera, aplicado al cuerpo del mensaje.
            raise AvisoInvalido(
                f'{plantilla} parámetro {{{{{i}}}}} vale {p!r} — es un dato ausente '
                f'convertido a texto, no un dato.'
            )


# ── Cuándo se manda ──────────────────────────────────────────────────────────

def toca_avisar_vencimiento(vencimiento: date, hoy: date,
                            dias_antes: int = DIAS_AVISO_DOCUMENTO) -> bool:
    """¿Este documento entró hoy en la ventana de aviso?

    **Ventana, no día exacto.** Si fuera `dias_restantes == 15`, un día que el
    cron no corra —deploy, caída, la variable apagada— se salta el aviso para
    siempre y nadie se entera. Con la ventana, el primer ciclo que corra lo
    manda; la idempotencia se encarga de que no se repita.

    Un documento ya vencido NO entra acá: eso no es un aviso de renovación, es
    un vehículo que no debería salir, y se atiende por otra vía.
    """
    if vencimiento is None:
        return False
    restantes = (vencimiento - hoy).days
    return 0 <= restantes <= dias_antes


def clave_aviso(plantilla: str, entidad: str, entidad_id: int, hito: str) -> str:
    """Identidad del aviso: una notificación por EVENTO, no por consulta.

    Si el cron reenviara el mismo vencimiento cada noche, en tres días el chat
    estaría silenciado y el aviso que importa llegaría a un silencio.

    El `hito` es lo que hace que un documento RENOVADO vuelva a avisar: la clave
    lleva la fecha de vencimiento, así que un SOAT nuevo es un evento nuevo.
    """
    return f'{plantilla}:{entidad}:{entidad_id}:{hito}'


__all__ = [
    'PLANTILLAS', 'DIAS_AVISO_DOCUMENTO', 'NOMBRE_DOCUMENTO', 'AvisoInvalido',
    'dias_en_palabras', 'fecha_en_palabras', 'nombre_documento',
    'parametros_documento_vence', 'validar_parametros',
    'toca_avisar_vencimiento', 'clave_aviso',
]
