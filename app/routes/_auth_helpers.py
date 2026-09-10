"""Helpers de autorización compartidos entre blueprints."""
from flask_jwt_extended import get_jwt_identity
from app.models.usuario import Usuario


class Roles:
    ADMIN          = 'admin'
    SUPERVISOR     = 'supervisor'
    JEFE_ALMACEN   = 'jefe_almacen'
    GERENTE        = 'gerente'
    OPERARIO          = 'operario'
    EMPACADOR         = 'empacador'
    RECEPCIONISTA     = 'recepcionista'
    TIENDA            = 'tienda'
    CONDUCTOR         = 'conductor'
    COMPRAS           = 'compras'
    CONTROL_FLOTA     = 'control_flota'     # dueño del registro de flota — NO aprueba
    PICKER_TRASLADO   = 'picker_traslado'   # picking de solicitudes de traslado
    PACKER_TRASLADO   = 'packer_traslado'   # packing/verificacion de solicitudes de traslado

    # Grupos reutilizables
    #
    # CONTROL_FLOTA NO está en GESTION a propósito. El procedimiento FLO-PR-01
    # dice que ve el tablero y escala, pero no aprueba órdenes de trabajo ni
    # gastos. Meterlo en GESTION le daría permiso sobre liquidación, traslados
    # y config de Siesa — y el documento diría una cosa mientras el sistema
    # permite otra. Ese desfase es lo que vuelve decorativo un procedimiento.
    GESTION        = (ADMIN, SUPERVISOR, JEFE_ALMACEN, GERENTE)
    ALMACEN        = (ADMIN, JEFE_ALMACEN)
    DESPACHO       = (ADMIN, SUPERVISOR, GERENTE, JEFE_ALMACEN)
    SUPERVISION    = (ADMIN, SUPERVISOR, JEFE_ALMACEN)
    PACKING_ROLES  = (ADMIN, SUPERVISOR, EMPACADOR)
    RECEPCION_ROLES = (ADMIN, JEFE_ALMACEN, RECEPCIONISTA)
    COMPRAS_ROLES  = (ADMIN, JEFE_ALMACEN, GERENTE, COMPRAS)
    LEAD           = (ADMIN, SUPERVISOR)
    TRASLADO_OPS   = (PICKER_TRASLADO, PACKER_TRASLADO)
    # Quién puede LEER los maestros que la pantalla de flota necesita para
    # funcionar: vehículos, conductores y sedes. Solo lectura — no habilita
    # crear, desactivar ni aprobar nada.
    #
    # Existe como grupo con nombre y no repetido en cada endpoint porque ese
    # fue el bug: `control_flota` se agregó a `Roles` y a la pantalla, y los
    # tres guards siguieron con su lista propia. Yesid entró, vio su pestaña y
    # recibió "Sin permiso para listar vehículos".
    #: **Operar un turno sobre UN vehículo.** El conductor entra porque el turno
    #: es suyo: recibe, inspecciona, reporta un daño, tanquea, entrega.
    #:
    #: El nombre dice `LECTURA` y autoriza escrituras —traspaso, odómetro,
    #: inspección, hallazgo, tanqueo—. Se conserva porque lo consumen
    #: `rutas.py` y `almacenes.py` fuera de flota, y renombrarlo en el mismo
    #: cambio que reparte permisos mezclaría dos decisiones. Queda anotado como
    #: nombre que miente, con su condición: el día que flota sea el único
    #: consumidor, pasa a llamarse `TURNO_FLOTA`.
    LECTURA_FLOTA  = GESTION + (CONDUCTOR, CONTROL_FLOTA)

    #: **Ver la flota ENTERA.** Sin el conductor, y esa ausencia es el punto.
    #:
    #: Hasta el 2026-09-03 los tableros de flota completa —avisos, vehículos
    #: fuera de sede, cierres forzados, y el vehículo de OTRO conductor— pedían
    #: `LECTURA_FLOTA`, así que cualquier conductor podía consultarlos. Ninguna
    #: pantalla se los ofrecía, pero la API contestaba.
    #:
    #: `cierres-forzados` es el caso que lo vuelve concreto: el propio módulo lo
    #: describe como *«mide conducta, no fallas»*. Un conductor no tiene por qué
    #: leer el registro de conducta de sus compañeros, y el permiso no puede ser
    #: más ancho que el gesto.
    #:
    #: No lo vio ningún trinquete porque todos miden **presencia** de
    #: `exige(...)` o que el rol declarado entre y el no declarado no —ninguno
    #: pregunta si el rol declarado DEBERÍA estar en la tupla.
    VISTA_FLOTA    = GESTION + (CONTROL_FLOTA,)


def _puede_empacar(usuario) -> bool:
    """Autorizado para operaciones de packing: rol en PACKING_ROLES O flag puede_empacar=True."""
    return usuario.rol in Roles.PACKING_ROLES or bool(usuario.puede_empacar)


def _solo_admin():
    """Devuelve el usuario actual si es admin, o None."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo and u.rol == Roles.ADMIN else None


def _es_admin_o_jefe():
    """Retorna el usuario si tiene rol admin o jefe_almacen, None en caso contrario."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo and u.rol in Roles.ALMACEN else None


def _es_gestion():
    """Retorna el usuario si tiene rol de gestión (admin/supervisor/jefe_almacen/gerente)."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo and u.rol in Roles.GESTION else None


def _es_control_flota():
    """Devuelve el usuario si puede LEER el tablero de flota.

    Incluye a gestión —quien puede todo, puede ver esto— más el rol dedicado.
    Es solo lectura: no habilita aprobar nada. Yesid no ordena, señala plazos
    vencidos y escala; la autoridad de la instrucción es del sistema, que
    calcula severidad y fecha límite por regla.
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or not u.activo:
        return None
    return u if u.rol in Roles.GESTION or u.rol == Roles.CONTROL_FLOTA else None


def _lee_flota():
    """Devuelve el usuario si puede LEER datos que la pantalla de flota usa.

    Más ancho que `_es_control_flota` en una sola cosa: incluye al conductor.
    Al entregar el turno tiene que declarar dónde queda el vehículo, y para eso
    necesita la lista de sedes. Sin ella el desplegable sale vacío y la custodia
    queda `pendiente_sede` sin razón.

    Solo lectura de maestros, nunca operación: el conductor no crea ni edita
    almacenes. Se separa de `_es_personal_almacen` por eso — ese helper autoriza
    operaciones, y ensancharlo daría permisos que ningún procedimiento concede.
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or not u.activo:
        return None
    return u if u.rol in Roles.LECTURA_FLOTA else None


def _es_personal_almacen():
    """Retorna el usuario si pertenece al personal de almacén (excluye conductor y tienda)."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo and u.rol not in (Roles.CONDUCTOR, Roles.TIENDA) else None


def _es_compras():
    """Retorna el usuario si tiene acceso a paneles de compras."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo and u.rol in Roles.COMPRAS_ROLES else None


def _get_uid():
    """Convierte get_jwt_identity() a int de forma segura. Retorna None si falla."""
    try:
        return int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
