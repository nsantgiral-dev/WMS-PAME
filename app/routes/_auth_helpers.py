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
    # `PACKER_TRASLADO` entra acá el 2026-09-21. No es un ensanche por
    # conveniencia: el endpoint YA estaba escrito para ellos —`scope_packing`
    # documenta y filtra su caso («bodega_siesa_id + solo TRASLADO»)— y su
    # gemelo `PICKER_TRASLADO` sí está en el guard de picking
    # (`app/routes/picking.py:22`). La puerta era lo único que faltaba.
    #
    # Hasta hoy funcionaba por una casilla: los cuatro packers de producción
    # tienen `puede_empacar=True`, un flag cuya etiqueta en la pantalla de
    # usuarios dice «Empacador / Auditor» —otro puesto— y que nace DESMARCADA.
    # El quinto packer que alguien cree queda mudo: la pantalla entera en rojo,
    # sin que nada explique por qué.
    #
    # El radio es exacto: `PACKING_ROLES` tiene UN solo consumidor
    # (`_puede_empacar`), y la autorización entra por la puerta mientras el
    # alcance decide qué ve adentro. Un packer de traslado sigue viendo solo
    # los traslados de su bodega.
    PACKING_ROLES  = (ADMIN, SUPERVISOR, EMPACADOR, PACKER_TRASLADO)
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


def _puede_organizar_layout():
    """Retorna el usuario si puede crear cuerpos/huecos y asignar SKU en Layout.

    Dos caminos: admin/jefe_almacen (control total del módulo, sin cambios) o
    cualquier operario/empacador con el flag `puede_organizar_layout=True` —
    mismo patrón que `puede_abastecer`/`puede_picar`/`puede_empacar`: una
    capacidad que se activa por persona, no un rol nuevo.

    Ojo: esto NO es lo mismo que "puede todo en Layout". Solo cubre crear
    cuerpo (POST) y asignar SKU (POST asignar) — editar, reclasificar, eliminar
    e importar Excel siguen exclusivos de `_es_admin_o_jefe()` en cada endpoint
    de `almacenes.py`. Un picker con el flag puede sumar ubicaciones y
    registrar qué SKU va en cada hueco; no puede borrar ni reestructurar lo
    que ya existe.
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or not u.activo:
        return None
    return u if u.rol in Roles.ALMACEN or bool(u.puede_organizar_layout) else None


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


def _puede_autorizar_cartera():
    """El usuario si puede autorizar una excepción de cartera desde el WMS.

    Un permiso por persona (`puede_autorizar_cartera`), no un rol: la vía
    principal es el Gestor de Cartera y esto es el respaldo. Nace apagado; ni
    el admin lo tiene por rol. Conductor y tienda nunca, aunque alguien les
    marque la casilla: no operan despachos.
    """
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or not u.activo:
        return None
    if u.rol in (Roles.CONDUCTOR, Roles.TIENDA):
        return None
    return u if bool(getattr(u, 'puede_autorizar_cartera', False)) else None


def _ve_cartera():
    """El usuario si puede VER las retenciones de cartera en el WMS: gestión
    (el tablero, la cola de despacho) o quien puede autorizarlas."""
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    if not u or not u.activo:
        return None
    if u.rol in Roles.GESTION:
        return u
    if u.rol in (Roles.CONDUCTOR, Roles.TIENDA):
        return None
    return u if bool(getattr(u, 'puede_autorizar_cartera', False)) else None


def exige_token_servicio(variable: str, que: str = ''):
    """Para lo que llama OTRO SISTEMA (el Gestor de Cartera), no una persona.

    `Authorization: Bearer <token>` comparado con `hmac.compare_digest` contra
    la variable de entorno. **Nace cerrado**: sin la variable, 503 y nada
    pasa. A diferencia de `flota.api._permisos.exige_secreto`, el token NO va
    en la URL (`?token=`): las URL quedan en los logs de acceso.

    Es un decorador con nombre para que `test_todo_endpoint_verifica_rol` lo
    reconozca: una ruta de servicio sin él sigue siendo un endpoint sin rol.
    """
    import hmac
    import os
    from functools import wraps

    def decorador(f):
        @wraps(f)
        def envoltura(*args, **kwargs):
            from flask import jsonify, request
            esperado = (os.getenv(variable) or '').strip()
            if not esperado:
                return jsonify({'error': f'{que or f.__name__} no está configurado',
                                'detalle': f'falta {variable}'}), 503
            cab = request.headers.get('Authorization') or ''
            dado = cab[7:].strip() if cab[:7].lower() == 'bearer ' else ''
            if not dado or not hmac.compare_digest(dado.encode(), esperado.encode()):
                return jsonify({'error': 'token inválido'}), 401
            return f(*args, **kwargs)
        return envoltura
    return decorador
