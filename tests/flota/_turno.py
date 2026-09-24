"""
Darle a un conductor el turno de un vehículo, como lo hace la operación.

Desde el 2026-09-24 el conductor solo registra odómetro, inspección, daño y
tanqueo sobre el vehículo de su custodia ACTIVA (`flota/api/_permisos.py`,
«El rol no alcanza»). Los tests que ejercen esas pantallas con un token de
conductor tienen que dárselo primero — y se lo dan por el adaptador real, con
el conductor recibiéndolo con SU usuario, no insertando una fila de custodia a
mano: un arnés que escribe filas solo prueba que la base las acepta.

Un solo helper y no uno por archivo: son seis archivos de tests que lo
necesitan, y seis copias de «cómo se recibe un turno» divergen.
"""


def dar_turno(db, usuario_id, placa, km=0, nombre=None):
    """El conductor `usuario_id` recibe `placa` con `km`. Devuelve la custodia.

    Si el usuario no tiene ficha de `Conductor`, se le crea una vinculada: sin
    ella el sistema no sabe que es él (`Conductor.usuario_id`).
    """
    from app.models.conductor import Conductor
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores import traspaso
    from flota.dominio.valores import CustodioTipo, QuienPide

    c = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
    if c is None:
        c = Conductor(nombre=nombre or f'Conductor {usuario_id}',
                      cedula=f'TURNO-{usuario_id}', activo=True,
                      usuario_id=usuario_id)
        db.session.add(c)
        db.session.commit()
    v = Vehiculo.query.filter_by(placa=placa).one()
    return traspaso.traspasar(
        vehiculo_id=v.id, km=km, registrado_por_usuario_id=usuario_id,
        custodio_tipo=CustodioTipo.CONDUCTOR, custodio_conductor_id=c.id,
        quien_pide=QuienPide.CONDUCTOR,
    )
