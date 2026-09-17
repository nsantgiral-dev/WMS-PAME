"""«¿De qué bodega es esta persona?» tiene UNA respuesta.

## El defecto que cierra

La pertenencia se expresa de dos formas según dónde trabaje la gente: los de
los puntos por `bodega_siesa_id`, los del CD por `almacen_id`. Medido en
producción el 2026-09-17: de 27 usuarios, los 13 de puntos tienen `almacen_id`
NULL y los 5 de NB1 tienen `bodega_siesa_id` NULL.

Un guard que mire un solo campo no ve a la mitad de la gente. Y los que existen
están escritos `if u.almacen_id and ...` — con el campo vacío **dejan pasar**.

## El contrato: falla cerrado

El único precedente de acotamiento de escritura del repo
(`tienda_oc._validar_recepcion_tienda`) falla ABIERTO: un recurso sin almacén
pasa. Acá no. Sin bodega resoluble, no pertenece — un permiso que no se puede
comprobar no se concede.
"""
import pytest

from app.models.almacen import Almacen
from app.models.usuario import Usuario
from app.services.alcance import (
    bodega_de_la_recepcion, bodega_del_usuario, usuario_es_de_la_bodega)


def _usuario(db, rol, bodega=None, almacen=None):
    u = Usuario(nombre=f'U {rol}', email=f'{rol}{bodega}{almacen and almacen.id}@x.co',
                rol=rol, activo=True,
                bodega_siesa_id=bodega,
                almacen_id=almacen.id if almacen else None)
    u.set_password('x' * 10)
    db.session.add(u); db.session.commit()
    return u


# ── Los dos mecanismos ─────────────────────────────────────────────────────

def test_resuelve_por_el_campo_directo(db):
    """Así están los 13 usuarios de puntos en producción."""
    u = _usuario(db, 'tienda', bodega='NS1')
    assert bodega_del_usuario(u) == 'NS1'


def test_resuelve_por_el_almacen(db, almacen):
    """Así están los 5 de NB1: sin `bodega_siesa_id`, con `almacen_id`."""
    u = _usuario(db, 'jefe_almacen', almacen=almacen)
    assert u.bodega_siesa_id is None
    assert bodega_del_usuario(u) == almacen.bodega_siesa_id


def test_el_campo_directo_gana_sobre_el_almacen(db, almacen):
    """Si los dos están puestos, el explícito manda: es el que alguien eligió."""
    u = _usuario(db, 'recepcionista', bodega='NC1', almacen=almacen)
    assert almacen.bodega_siesa_id != 'NC1'
    assert bodega_del_usuario(u) == 'NC1'


# ── Falla cerrado ──────────────────────────────────────────────────────────

def test_sin_ninguno_de_los_dos_no_se_sabe(db):
    """El admin de producción está así: sin bodega y sin almacén."""
    u = _usuario(db, 'admin')
    assert bodega_del_usuario(u) is None


def test_no_saber_no_es_pertenecer(db):
    u = _usuario(db, 'admin')
    assert usuario_es_de_la_bodega(u, 'NB1') is False


def test_almacen_sin_bodega_tampoco_alcanza(db):
    a = Almacen(codigo='SIN-BOD', nombre='Sin bodega', activo=True)
    db.session.add(a); db.session.commit()
    u = _usuario(db, 'operario', almacen=a)
    assert bodega_del_usuario(u) is None
    assert usuario_es_de_la_bodega(u, 'NB1') is False


def test_un_recurso_sin_bodega_no_lo_hace_de_nadie(db):
    """El precedente del repo falla ABIERTO acá. Este no."""
    u = _usuario(db, 'tienda', bodega='NS1')
    assert usuario_es_de_la_bodega(u, None) is False
    assert usuario_es_de_la_bodega(u, '') is False
    assert usuario_es_de_la_bodega(u, '   ') is False


def test_usuario_nulo_no_pertenece_a_nada(db):
    assert bodega_del_usuario(None) is None
    assert usuario_es_de_la_bodega(None, 'NB1') is False


# ── Pertenencia ────────────────────────────────────────────────────────────

def test_pertenece_a_la_suya_y_no_a_otra(db):
    u = _usuario(db, 'tienda', bodega='NS1')
    assert usuario_es_de_la_bodega(u, 'NS1') is True
    assert usuario_es_de_la_bodega(u, 'NB1') is False


def test_el_del_cd_pertenece_a_la_de_su_almacen(db, almacen):
    u = _usuario(db, 'supervisor', almacen=almacen)
    assert usuario_es_de_la_bodega(u, almacen.bodega_siesa_id) is True
    assert usuario_es_de_la_bodega(u, 'PC1') is False


def test_los_espacios_no_cuentan_como_bodega(db):
    u = _usuario(db, 'tienda', bodega='   ')
    assert bodega_del_usuario(u) is None


# ── La bodega de una recepción ─────────────────────────────────────────────

def test_la_recepcion_resuelve_su_bodega_por_el_almacen(db, almacen):
    from app.models.recepcion import RecepcionMercancia
    r = RecepcionMercancia(codigo='REC-1', numero_oc_siesa='OC-1',
                           almacen_id=almacen.id, estado='ABIERTA')
    db.session.add(r); db.session.commit()
    assert bodega_de_la_recepcion(r) == almacen.bodega_siesa_id


def test_una_recepcion_en_un_almacen_sin_bodega_no_tiene_bodega(db):
    """`RecepcionMercancia.almacen_id` es NOT NULL, así que una recepción
    huérfana de almacén no es alcanzable. El caso que SÍ puede darse es un
    almacén al que nadie le asignó bodega Siesa — y ahí el precedente del repo
    falla abierto. Este devuelve None, que después se traduce en «no
    pertenece»."""
    from app.models.recepcion import RecepcionMercancia
    a = Almacen(codigo='ALM-NOBOD', nombre='Sin bodega', activo=True)
    db.session.add(a); db.session.commit()
    r = RecepcionMercancia(codigo='REC-2', numero_oc_siesa='OC-2',
                           almacen_id=a.id, estado='ABIERTA')
    db.session.add(r); db.session.commit()
    assert bodega_de_la_recepcion(r) is None
