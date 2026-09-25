"""
Quién puede hacer cada operación de la liquidación. **Una función por
operación** (decisión del dueño, 2026-09-25).

Los roles «líder de cartera» (autoriza retenciones y crédito) y probablemente
«liquidador» (liquida rutas sin ser admin) se van a crear en una pasada
posterior. Cuando existan, agregarlos es **una línea** en la función de su
operación — no un `if` nuevo en cada ruta.

La regla que ordena esto (P1-8): **un endpoint que encola un documento de
plata (RC, DC o NC) exige el permiso más estricto de lo que ejecuta.** Antes
«Registrar cobro» (encola RC y DC) pedía admin o jefe de almacén, y «Enviar a
Siesa» (encola lo mismo, en lote) pedía admin: la operación chica pedía menos
que la grande. `tests/test_permiso_compuesto.py` descubre por AST toda ruta que
llega a un encolador de plata y exige que llame a la función del permiso
declarado para ese encolador.

Todas reciben el `Usuario` (o `None`) y devuelven `bool`. Ninguna consulta la
base.
"""
from app.routes._auth_helpers import Roles


def _activo(usuario) -> bool:
    return bool(usuario is not None and getattr(usuario, 'activo', False))


def puede_liquidar(usuario) -> bool:
    """Liquidar una ruta y **encolar sus documentos de plata** (RC, DC, NC):
    «Liquidar», «Enviar a Siesa», «Registrar cobro», reintentar un documento
    de plata. Hoy: admin. El rol «liquidador» se agrega acá."""
    return _activo(usuario) and usuario.rol == Roles.ADMIN


def puede_forzar_cierre_ruta(usuario) -> bool:
    """Forzar el cierre de una ruta (decide por el conductor qué no se
    entregó). Hoy: admin."""
    return _activo(usuario) and usuario.rol == Roles.ADMIN


def puede_autorizar_credito(usuario) -> bool:
    """Autorizar como crédito una parada de contado que no trajo plata. Hoy:
    admin. El «líder de cartera» se agrega acá."""
    return _activo(usuario) and usuario.rol == Roles.ADMIN


def puede_confirmar_retencion(usuario) -> bool:
    """Confirmar o rechazar la retención que declaró el conductor. Hoy: admin
    y jefe de almacén (lo de siempre). El «líder de cartera» se agrega acá."""
    return _activo(usuario) and usuario.rol in Roles.ALMACEN


def puede_corregir_cobro(usuario) -> bool:
    """Corregir el monto que declaró el conductor, o confirmar una parada
    después del cierre de la ruta. Hoy: admin y jefe de almacén."""
    return _activo(usuario) and usuario.rol in Roles.ALMACEN


def puede_resolver_documento(usuario) -> bool:
    """Decidir a mano el desenlace de un recibo de caja que el WMS no pudo
    verificar (entró / no entró). Es plata: el mismo permiso que liquidar."""
    return puede_liquidar(usuario)


def puede_ver_liquidacion(usuario) -> bool:
    """Ver la pantalla de Liquidación y sus lecturas. Hoy: admin y jefe."""
    return _activo(usuario) and usuario.rol in Roles.ALMACEN


#: Los documentos que encola la liquidación. Reintentar uno de estos es volver
#: a mandar un documento de plata del conductor: pide `puede_liquidar`. (La NC
#: de una devolución la encola recepción al contar: no está acá.)
TIPOS_JOB_DE_LIQUIDACION = ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA')


def puede_reintentar_job(usuario, tipo_job: str) -> bool:
    """Volver a poner en cola un job FALLIDO. Uno de la liquidación pide
    `puede_liquidar`; los demás, supervisión (lo de siempre)."""
    if tipo_job in TIPOS_JOB_DE_LIQUIDACION:
        return puede_liquidar(usuario)
    return _activo(usuario) and usuario.rol in Roles.SUPERVISION
