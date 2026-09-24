"""
Quién puede llamar a cada endpoint de flota.

**Hasta el 2026-08-03 la respuesta era: cualquiera con sesión.** Todos los
endpoints del módulo llevaban `@jwt_required()` y nada más. Un usuario de
tienda, un empacador, cualquier operario podía:

  · sobrescribir la ficha técnica de un vehículo, incluido `km_inicial` — el
    ancla contra la que se valida TODO el histórico de odómetro;
  · declarar un SOAT "vigente" con la fecha de vencimiento que quisiera;
  · registrar lecturas y tomar la custodia de cualquier camión.

Lo encontró el review automático, no una persona ni los tests: nadie intenta
sobrescribir una ficha desde una cuenta de tienda, y ningún test afirmaba que no
se pudiera. Es la contracara de la lección del mismo día — la persona encuentra
lo que nadie pensó en afirmar; el review encuentra lo que nadie iba a intentar.

La ironía que lo hace evidente: `/flota/health`, que es **solo lectura**, sí
tenía control de rol desde el primer día. Las escrituras no.

Dos niveles, y la diferencia es de procedimiento, no de comodidad:

  LECTURA_FLOTA  — gestión + conductor + control de flota. Operar el turno:
                   recibir, entregar, registrar odómetro, ver las fotos. El
                   conductor entra acá porque el turno es suyo — **y solo el
                   suyo**: la puerta dice qué ROL entra, `sin_derecho_sobre_*`
                   dice sobre qué VEHÍCULO. Ver «El rol no alcanza» abajo.

  MAESTROS       — gestión + control de flota. Ficha técnica y documentos son
                   maestros del vehículo, no operación del turno. Yesid entra
                   porque el levantamiento de campo es su trabajo (FLO-PR-01);
                   el conductor no, porque `km_inicial` mal escrito corrompe
                   todo lo que cuelga de él y no hay forma de detectarlo después.
"""
from functools import wraps

from flask import jsonify
from flask_jwt_extended import get_jwt_identity

from app.models.usuario import Usuario
from app.routes._auth_helpers import Roles

#: **Registro** del vehículo: escribir lo que pasó. NO incluye conductor.
#:
#: Nació el 2026-08-03 gateando dos endpoints —ficha y documentos— y su nombre
#: describía esos dos: «maestros del vehículo». Al 2026-09-09 gatea **25 pares
#: ruta×método** (contados contra `url_map`, no recordados), porque taller
#: (2026-09-01) y gastos (2026-09-02) colgaron de la tupla que ya existía. Los tres motivos escritos en ESTADO.md para elegirla —1256, 1257 y
#: 1502— dicen lo mismo: **«no el conductor»**. Ninguno se preguntó por el otro
#: borde, y así el nombre siguió prometiendo dos endpoints mientras el alcance
#: se multiplicaba por diez.
#:
#: Lo que autoriza hoy, dicho entero: ficha, documentos, verificar un odómetro
#: dudoso, registrar un gasto, registrar la factura de una orden, registrar una
#: intervención, sembrar y ejecutar preventivo, dar de alta y montar llantas,
#: barrer avisos. **Todo eso es registro: consignar un hecho que ya ocurrió.**
#:
#: Lo que NO autoriza está en `DECIDE_FLOTA`, y la frontera es esa: registrar un
#: hecho contra decidir uno.
MAESTROS_FLOTA = tuple(Roles.GESTION) + (Roles.CONTROL_FLOTA,)

#: **Decisión**: cerrar el ciclo de un daño o de una visita al taller.
#:
#: `GESTION` a secas — control de flota queda **fuera**, y es el único sitio del
#: módulo donde queda fuera de algo que MAESTROS_FLOTA le daba.
#:
#: Dos motivos, y el segundo es el que no se puede negociar:
#:
#: 1. Su procedimiento lo dice literal desde el 2026-08-04: *«ves el tablero y
#:    escalás, pero no aprobás órdenes de trabajo ni gastos»*
#:    (`especialista-control-flota.md:38`). El código decía lo contrario y el
#:    documento afirmaba que el código lo imponía — el desfase exacto que ese
#:    mismo párrafo nombra como «lo que vuelve decorativo un procedimiento».
#:
#: 2. **Es la regla 11 un nivel más arriba.** `dias_hallazgo_abierto` y
#:    `hallazgos_vencidos` son dos de las cinco señales con las que se mide a
#:    control de flota (`especialista-control-flota.md:112`, *«Cómo se sabe que
#:    lo estás haciendo bien»*). La regla 11 pregunta cómo maximiza una métrica
#:    quien no quiere hacer el trabajo; acá la respuesta era de un clic:
#:    «Reparado», «No era nada», «Aplazar 7 días». Aplazar no para el reloj de
#:    los días abiertos, pero sí borra el «vencido», que es justo lo que su
#:    ficha le manda perseguir. **Quien es medido por un contador no puede tener
#:    el botón que lo baja.**
#:
#: Lo que sigue pudiendo hacer: ver todo, reportar el daño, registrar el gasto y
#: la factura de la reparación. Lo que ya no: mandar el camión al taller, darlo
#: por vuelto, anular la visita, y cerrar, descartar o aplazar el daño.
#:
#: El costo operativo está medido y es cero hoy: al 2026-09-09 no hay una sola
#: orden de trabajo ni un solo hallazgo en la base (ESTADO.md:1164, 1429). El
#: costo futuro es real y es el precio de la regla: para mandar un camión al
#: taller, control de flota escala a gestión — que es literalmente lo que su
#: ficha dice que hace («no ordena: señala plazos vencidos y escala»).
DECIDE_FLOTA = tuple(Roles.GESTION)


def _usuario():
    try:
        uid = int(get_jwt_identity())
    except (TypeError, ValueError):
        return None
    u = Usuario.query.get(uid)
    return u if u and u.activo else None


def exige(roles, que=''):
    """Decorador: exige que el rol del usuario esté en `roles`.

    Va DESPUÉS de `@jwt_required()` en el orden de decoradores — necesita la
    identidad ya resuelta.

    El 403 dice qué hace falta, no solo que no. Un "sin permiso" pelado deja a
    quien lo recibe sin saber a quién pedirle qué, y termina en un mensaje de
    WhatsApp al desarrollador.
    """
    def decorador(f):
        @wraps(f)
        def envoltura(*args, **kwargs):
            u = _usuario()
            if u is None or u.rol not in roles:
                return jsonify({
                    'error': f'Sin permiso para {que or f.__name__}',
                    'tu_rol': u.rol if u else None,
                    'roles_permitidos': sorted(roles),
                }), 403
            return f(*args, **kwargs)
        return envoltura
    return decorador


def exige_secreto(variable: str, que: str = ''):
    """Para lo que llama una máquina, no una persona.

    Un webhook de proveedor no puede llevar JWT: quien lo invoca es Gupshup, no
    un usuario. La alternativa NO es dejarlo abierto —cualquiera podría declarar
    entregado un aviso que nunca llegó, y ese es justo el dato que el módulo
    existe para hacer confiable— sino un secreto compartido en la URL.

    **Nace cerrado.** Sin la variable configurada responde 503 y no acepta nada.
    Un webhook que se abre solo porque falta configuración es la regla 10 al
    revés: lo peligroso pasa cuando alguien NO hizo algo.

    Es un decorador propio y con nombre para que el trinquete de permisos pueda
    reconocerlo. Una ruta sin `exige` ni `exige_secreto` sigue fallando el
    trinquete — que es el punto.
    """
    import os

    def decorador(f):
        @wraps(f)
        def envoltura(*args, **kwargs):
            from flask import request

            esperado = (os.getenv(variable) or '').strip()
            if not esperado:
                return jsonify({
                    'error': f'{que or f.__name__} no está configurado',
                    'detalle': f'falta {variable}',
                }), 503
            if request.args.get('token') != esperado:
                return jsonify({'error': 'token inválido'}), 403
            return f(*args, **kwargs)
        return envoltura
    return decorador


#: **Cerrar el turno de otro sin su firma** (cierre forzado): gestión y control
#: de flota. Decidido el 2026-09-24.
#:
#: Hasta ese día `control_flota` veía en el recibo de escritorio el campo
#: «Motivo del cierre forzado», lo llenaba, y el backend lo trataba como
#: CONDUCTOR y le contestaba 409: la pantalla le ofrecía un gesto que el sistema
#: le negaba. Se le da el permiso y no se le esconde el campo porque **el patio
#: lo opera él**: a las 5 a.m. el camión tiene que salir, el que lo tenía se fue
#: sin cerrar, y gestión no está en el patio. Su procedimiento ya le manda
#: «llamá a los dos» cuando pasa (`especialista-control-flota.md`).
#:
#: No es `DECIDE_FLOTA` por el argumento de la regla 11: el cierre forzado no
#: BAJA ningún contador por el que se lo mida — lo SUBE (`cierres forzados por
#: semana` es una de sus señales, y «cero en un mes» su criterio para crecer).
#: Para que siga así, **a él las fotos de cierre no lo eximen**: todo cierre de
#: un turno ajeno que hace control de flota pide motivo escrito y queda marcado
#: forzado, con su nombre (`Veredicto.fotos_no_eximen`). Si las fotos lo
#: eximieran, tendría una forma de cerrar turnos ajenos sin que el contador que
#: lo mide se entere.
#:
#: La pantalla lo lee de `FLOTA_ROLES_FUERZAN_CIERRE` (`flota.js`), y
#: `tests/flota/test_derecho_del_conductor.py` compara las dos listas.
FUERZA_CIERRE = tuple(Roles.GESTION) + (Roles.CONTROL_FLOTA,)


def quien_pide_de(usuario):
    """Traduce el ROL de la sesión a `QuienPide`. Una sola traducción.

    Sale del rol de la base, jamás del cuerpo del request: un conductor que
    mandara `admin_zona` le cerraría el turno a otro con un campo. Un rol que no
    es gestión ni control de flota es CONDUCTOR, que es el que menos puede — un
    rol nuevo nace con el mínimo (Regla 0), no con el máximo.
    """
    from flota.dominio.valores import QuienPide

    if usuario is not None and usuario.rol in Roles.GESTION:
        return QuienPide.ADMIN_ZONA
    if usuario is not None and usuario.rol == Roles.CONTROL_FLOTA:
        return QuienPide.CONTROL_FLOTA
    return QuienPide.CONDUCTOR


# ═══════════════════════════════════════════════════════════════════════════
# El rol no alcanza: sobre QUÉ vehículo (2026-09-24)
# ═══════════════════════════════════════════════════════════════════════════
#
# `LECTURA_FLOTA` deja entrar al conductor a registrar odómetro, inspección,
# daño y tanqueo, y bajar fotos. **Sobre cualquier placa y cualquier foto.** La
# auditoría del 2026-09-24 lo ejecutó: un conductor mandaba
# `POST /flota/odometro {placa: <la de otro>, origen: correccion}` y, como gana
# la lectura más reciente, le cambiaba el `odometro_actual` —el CPK, el
# preventivo— a un camión que no manejaba; y `GET /flota/foto/<id>` le devolvía
# el escaneo del SOAT o de la tarjeta de propiedad de cualquier vehículo.
#
# Es la forma de «un guard cuya precondición la manda quien pide»: la puerta
# mira el rol, y el vehículo lo elige el cuerpo del request.
#
# **La regla: el conductor opera sobre el vehículo de su custodia ACTIVA.** No
# el de su ruta de hoy, aunque `mi-turno` lo sugiera. Tres motivos:
#
#   1. La pantalla ya lo dice así: Inspección, Reportar daño, Tanqueo y
#      Odómetro solo aparecen con el turno abierto (`flotaCondRender`).
#   2. La ruta es SUGERENCIA (`dominio/turno.py`): el conductor puede tomar
#      otro camión. Autorizar por la ruta autorizaría sobre el camión que el
#      conductor NO tiene.
#   3. Regla 0: ante la duda, lo conservador. Sin custodia no hay nadie que
#      responda por el camión, y un registro sobre él no tiene quién lo firme.
#
# Gestión y control de flota no pasan por esto: operan la flota entera desde el
# escritorio, y su puerta ya es la de ellos.
#
# Vive acá, en la frontera, y no en los adaptadores: las cuatro operaciones
# tienen UNA puerta cada una (verificado 2026-09-24: ningún otro sitio llama a
# `registrar_tanqueo`, `hallazgos.reportar` desde afuera, `inspecciones.registrar`
# ni escribe `LecturaOdometro` suelta), y el trinquete de
# `tests/flota/test_derecho_del_conductor.py` recorre por AST **toda** ruta de
# flota que admita al conductor — una puerta nueva sin la pregunta lo pone rojo.
# El traspaso sí lo resuelve el adaptador (`traspaso.traspasar`), porque ahí
# la regla depende de quién es el custodio actual y eso ya lo sabe él.


def _no_es_tuyo(mensaje: str):
    """El 403 del derecho, distinguible del 403 del rol.

    Lleva `motivo: 'sin_derecho'` y no `roles_permitidos`: el que lo recibe no
    necesita otro rol, necesita el vehículo. Mandarlo a pedir un permiso sería
    mandarlo a la persona equivocada.
    """
    return jsonify({'error': mensaje, 'motivo': 'sin_derecho'}), 403


def conductor_de(usuario):
    """La ficha de `Conductor` vinculada a este usuario, o `None`.

    Misma consulta que `api/conductor._conductor_del_token`: activo y por
    `usuario_id`. Un usuario conductor sin ficha no opera ningún vehículo.
    """
    from app.models.conductor import Conductor

    if usuario is None:
        return None
    return Conductor.query.filter_by(usuario_id=usuario.id, activo=True).first()


def _es_conductor(usuario) -> bool:
    return usuario is not None and usuario.rol == Roles.CONDUCTOR


def vehiculo_en_custodia_de(usuario):
    """El `vehiculo_id` de la custodia activa del conductor, o `None`."""
    from flota.adaptadores.modelos import Custodia

    c = conductor_de(usuario)
    if c is None:
        return None
    vigente = Custodia.query.filter(
        Custodia.custodio_conductor_id == c.id,
        Custodia.fin_ts.is_(None),
    ).first()
    return vigente.vehiculo_id if vigente is not None else None


def sin_derecho_sobre_vehiculo(vehiculo, que: str):
    """`None` si quien pide puede operar sobre este vehículo; el 403 si no.

    Gestión y control de flota: siempre. Conductor: solo el de su custodia
    activa. Se llama DESPUÉS de validar el cuerpo y de resolver la placa: un 400
    o un 404 no dicen nada de nadie, y así el 403 queda para lo que es.
    """
    u = _usuario()
    if not _es_conductor(u):
        return None
    if conductor_de(u) is None:
        return _no_es_tuyo(
            f'No podés {que}: tu usuario no está vinculado a un conductor. '
            f'Pedile a administración que te vincule la ficha.')
    if vehiculo_en_custodia_de(u) == vehiculo.id:
        return None
    return _no_es_tuyo(
        f'No podés {que} sobre el {vehiculo.placa}: no está en tu turno. '
        f'Solo se registra sobre el vehículo que recibiste — si es el que tenés '
        f'enfrente, recibí el turno primero.')


def sin_derecho_sobre_custodia(custodia, que: str):
    """Conductor: solo sus custodias (la de hoy y las pasadas). Resto: todas."""
    u = _usuario()
    if not _es_conductor(u):
        return None
    c = conductor_de(u)
    if c is not None and custodia.custodio_conductor_id == c.id:
        return None
    return _no_es_tuyo(f'No podés {que}: ese turno no es tuyo.')


def _foto_de_custodia_propia(foto, conductor, usuario):
    from app.extensions import db
    from flota.adaptadores.modelos import Custodia

    padre = db.session.get(Custodia, foto.entidad_id)
    return padre is not None and padre.custodio_conductor_id == conductor.id


def _foto_de_hallazgo_propio(foto, conductor, usuario):
    from app.extensions import db
    from flota.adaptadores.modelos import Hallazgo

    padre = db.session.get(Hallazgo, foto.entidad_id)
    return padre is not None and padre.reportado_por_usuario_id == usuario.id


def _foto_de_lectura_propia(foto, conductor, usuario):
    from app.extensions import db
    from flota.adaptadores.modelos import LecturaOdometro

    padre = db.session.get(LecturaOdometro, foto.entidad_id)
    return padre is not None and padre.autor_usuario_id == usuario.id


def _foto_de_gasto_propio(foto, conductor, usuario):
    """La foto del recibo de SU tanqueo: el gasto cuelga de la lectura que él
    registró (`Gasto.lectura_id` → `LecturaOdometro.autor_usuario_id`). Un gasto
    sin lectura lo cargó gestión, y no es suyo."""
    from app.extensions import db
    from flota.adaptadores.modelos import Gasto, LecturaOdometro

    gasto = db.session.get(Gasto, foto.entidad_id)
    if gasto is None or gasto.lectura_id is None:
        return False
    lectura = db.session.get(LecturaOdometro, gasto.lectura_id)
    return lectura is not None and lectura.autor_usuario_id == usuario.id


def _nunca(foto, conductor, usuario):
    return False


#: Qué fotos ve un conductor, **por padre** y no por autor: la foto de su
#: custodia que tomó gestión en el recibo de escritorio también es suya — es
#: cómo estaba el camión cuando se lo entregaron, y es su respaldo (regla 2).
#:
#: Total sobre `EntidadFoto`, y un test lo obliga: un padre nuevo que no se
#: agregue acá cae en `_nunca`, pero el test pone rojo antes para que alguien lo
#: decida en vez de heredar el «no» por omisión.
#:
#: `documento` es `_nunca` **a propósito**: SOAT, tarjeta de propiedad y póliza
#: son maestros del vehículo (MAESTROS_FLOTA) y el escaneo trae datos del
#: propietario. El conductor no los ve ni del camión que maneja: lo que necesita
#: saber —que están vencidos— ya le llega en `mi-turno`.
FOTO_DEL_CONDUCTOR = {
    'custodia_inicio': _foto_de_custodia_propia,
    'custodia_fin': _foto_de_custodia_propia,
    'hallazgo': _foto_de_hallazgo_propio,
    'odometro': _foto_de_lectura_propia,
    'gasto': _foto_de_gasto_propio,
    'documento': _nunca,
}


def sin_derecho_sobre_foto(foto, que: str):
    """Conductor: solo las fotos de sus custodias, sus daños y sus lecturas."""
    u = _usuario()
    if not _es_conductor(u):
        return None
    c = conductor_de(u)
    # Un padre que el mapa no conoce es un «no», escrito y no heredado de un
    # `.get(x, default)` (regla 5 del módulo): el test de totalidad obliga a
    # decidirlo antes de que llegue acá.
    juez = (FOTO_DEL_CONDUCTOR[foto.entidad_tipo]
            if foto.entidad_tipo in FOTO_DEL_CONDUCTOR else _nunca)
    if c is not None and juez(foto, c, u):
        return None
    return _no_es_tuyo(f'No podés {que}: esa foto no es de un turno ni de un '
                       f'reporte tuyo.')


def sin_permiso_de_corregir():
    """Corregir un odómetro es MAESTROS_FLOTA, aunque la puerta sea LECTURA.

    `origen=correccion` no es una lectura más: gana por ser la más reciente y
    reescribe el `odometro_actual` del que cuelgan el CPK y el preventivo. Quien
    la escribe está diciendo «la anterior estaba mal», y eso es verificar un
    kilometraje — que ya es de `MAESTROS_FLOTA` (`/odometro/<id>/verificar`).
    El conductor registra lo que ve en el tablero; si se equivocó, avisa.
    """
    u = _usuario()
    if u is not None and u.rol in MAESTROS_FLOTA:
        return None
    return jsonify({
        'error': 'Corregir un odómetro lo hace control de flota o gestión. Si '
                 'registraste un número equivocado, avisale al encargado: él '
                 'lo corrige con la foto del tablero.',
        'tu_rol': u.rol if u else None,
        'roles_permitidos': sorted(MAESTROS_FLOTA),
    }), 403


__all__ = ['exige', 'exige_secreto', 'MAESTROS_FLOTA', 'DECIDE_FLOTA',
           'FUERZA_CIERRE', 'quien_pide_de', 'conductor_de',
           'vehiculo_en_custodia_de', 'sin_derecho_sobre_vehiculo',
           'sin_derecho_sobre_custodia', 'sin_derecho_sobre_foto',
           'sin_permiso_de_corregir', 'FOTO_DEL_CONDUCTOR']
