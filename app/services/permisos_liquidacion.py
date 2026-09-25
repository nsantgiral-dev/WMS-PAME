"""
Quién puede hacer cada operación de la liquidación. **Una función por
operación** (decisión del dueño, 2026-09-25).

Matriz del dueño (2026-09-25), con los roles «liquidador» y «líder de
cartera» (`Roles.LIQUIDADOR`, `Roles.LIDER_CARTERA`):

| Operación | Quién |
|---|---|
| Liquidar, registrar cobros, reintentar RC/DC/NC, resolver un RC sin verificar | admin + liquidador |
| Registrar desde la oficina una parada tardía (ruta ya cerrada) | admin + liquidador |
| Confirmar la retención declarada, corregir un cobro | admin + líder de cartera |
| Autorizar como crédito una parada de contado sin plata (una o en lote) | admin + líder de cartera |
| Forzar el cierre de una ruta | admin |
| Ver Liquidación, su desglose y la reconciliación | admin + jefe + liquidador + líder de cartera |

**Cambio de comportamiento:** el jefe de almacén ya no registra cobros ni
reintenta documentos de la liquidación (P1-8), y tampoco confirma
retenciones ni corrige cobros: sigue VIENDO la liquidación.

La regla que ordena esto (P1-8): **un endpoint que encola un documento de
plata (RC, DC o NC) exige el permiso más estricto de lo que ejecuta.** Antes
«Registrar cobro» (encola RC y DC) pedía admin o jefe de almacén, y «Enviar a
Siesa» (encola lo mismo, en lote) pedía admin: la operación chica pedía menos
que la grande. `tests/test_permiso_compuesto.py` descubre por AST toda ruta que
llega a un encolador de plata y exige que llame a la función del permiso
declarado para ese encolador. `tests/test_roles_plata.py` fija la matriz.

Todas reciben el `Usuario` (o `None`) y devuelven `bool`. Ninguna consulta la
base.
"""
from app.routes._auth_helpers import Roles


def _activo(usuario) -> bool:
    return bool(usuario is not None and getattr(usuario, 'activo', False))


def _rol_en(usuario, roles) -> bool:
    return _activo(usuario) and usuario.rol in roles


def puede_liquidar(usuario) -> bool:
    """Liquidar una ruta y **encolar sus documentos de plata** (RC, DC, NC):
    «Liquidar», «Enviar a Siesa», «Registrar cobro», reintentar un documento
    de plata. Admin y liquidador."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.LIQUIDADOR))


def puede_registrar_parada_tardia(usuario) -> bool:
    """Registrar desde la oficina una parada que quedó sin gestionar cuando la
    ruta ya se cerró (`motivo_tardia`, FORZAR). Es trabajo de quien liquida:
    admin y liquidador."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.LIQUIDADOR))


def puede_forzar_cierre_ruta(usuario) -> bool:
    """Forzar el cierre de una ruta (decide por el conductor qué no se
    entregó). Admin."""
    return _rol_en(usuario, (Roles.ADMIN,))


def puede_autorizar_credito(usuario) -> bool:
    """Autorizar como crédito una parada de contado que no trajo plata (una, o
    en lote las anteriores a la regla de contado). Admin y líder de cartera."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.LIDER_CARTERA))


def puede_confirmar_retencion(usuario) -> bool:
    """Confirmar o rechazar la retención que declaró el conductor. Admin y
    líder de cartera."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.LIDER_CARTERA))


def puede_corregir_cobro(usuario) -> bool:
    """Corregir el monto que declaró el conductor (y, con la ruta en tránsito,
    registrar la parada por él desde la oficina). Admin y líder de cartera."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.LIDER_CARTERA))


def puede_resolver_documento(usuario) -> bool:
    """Decidir a mano el desenlace de un recibo de caja que el WMS no pudo
    verificar (entró / no entró). Es plata: el mismo permiso que liquidar."""
    return puede_liquidar(usuario)


def puede_ver_liquidacion(usuario) -> bool:
    """Ver la pantalla de Liquidación y sus lecturas (dashboard, detalle,
    desglose, reconciliación, planilla, vista previa, los envíos de la
    liquidación). Admin, jefe, liquidador y líder de cartera."""
    return _rol_en(usuario, (Roles.ADMIN, Roles.JEFE_ALMACEN,
                             Roles.LIQUIDADOR, Roles.LIDER_CARTERA))


#: Los documentos que encola la liquidación. Reintentar uno de estos es volver
#: a mandar un documento de plata del conductor: pide `puede_liquidar`. (La NC
#: de una devolución la encola recepción al contar: no está acá.)
TIPOS_JOB_DE_LIQUIDACION = ('RECIBO_CAJA', 'DOCUMENTO_CONTABLE_RET', 'NOTA_CREDITO_FACTURA')


def puede_reintentar_job(usuario, tipo_job: str) -> bool:
    """Volver a poner en cola un job FALLIDO. Uno de la liquidación pide
    `puede_liquidar`; los demás, supervisión (lo de siempre)."""
    if tipo_job in TIPOS_JOB_DE_LIQUIDACION:
        return puede_liquidar(usuario)
    return _rol_en(usuario, Roles.SUPERVISION)


def puede_ver_jobs(usuario, tipos) -> bool:
    """Listar los envíos a Siesa. Supervisión los ve todos; quien ve la
    liquidación, **solo** los de la liquidación (la pestaña «Envíos» de
    Liquidación pide exactamente esos). `tipos` vacío es «todos»: supervisión."""
    if _rol_en(usuario, Roles.SUPERVISION):
        return True
    tipos = [t for t in (tipos or []) if t]
    return (bool(tipos) and set(tipos) <= set(TIPOS_JOB_DE_LIQUIDACION)
            and puede_ver_liquidacion(usuario))
