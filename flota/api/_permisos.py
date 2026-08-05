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
                   conductor entra acá porque el turno es suyo.

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

#: Maestros del vehículo: ficha y documentos. NO incluye conductor.
MAESTROS_FLOTA = tuple(Roles.GESTION) + (Roles.CONTROL_FLOTA,)


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


__all__ = ['exige', 'exige_secreto', 'MAESTROS_FLOTA']
