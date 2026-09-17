"""TRA-30 deja de emitir un aviso por cada traslado en vuelo.

## El defecto

Emitía un `Hallazgo` por CADA traslado `EN_TRANSITO`, sin umbral. Medido contra
producción el 2026-09-17: **19 en tránsito → 19 hallazgos, todos los días**.
Eso no es un detector; es ruido que entierra al que sí está atascado. Es la
lección que el repo ya tiene escrita sobre canales de advertencia ilegibles.

## El umbral, con su base

`DIAS_EN_TRANSITO_ANORMAL = 16`. Medido sobre los traslados ENTREGADA de
producción que tienen las dos fechas: **promedio 7,6 días, máximo 15,3**. Un
umbral de 2 o 3 días marcaría como problema la mitad de los despachos sanos.

## Y los que no tienen fecha

Los 19 de producción no tienen `fecha_despacho`: salieron por el cierre de
packing, que no la escribía hasta el 2026-09-16. Es un problema del REGISTRO,
no del traslado, así que va como **un solo hallazgo agregado** — 19 líneas
repitiendo lo mismo no agregan información. Y no se puede arreglar hacia atrás:
no hay de dónde deducir la fecha sin inventarla.
"""
from datetime import datetime, timedelta

import pytest

from app.models.traslado import SolicitudTraslado, EstadoTraslado
from app.services.auditoria.traslados import (
    DIAS_EN_TRANSITO_ANORMAL, se_pueden_contar_los_traslados_en_vuelo)


def _en_transito(db, usuario, codigo, dias=None):
    s = SolicitudTraslado(
        codigo=codigo, bodega_origen_siesa='NB1', bodega_destino_siesa='NS1',
        estado=EstadoTraslado.EN_TRANSITO, solicitante_id=usuario.id,
        fecha_despacho=(datetime.utcnow() - timedelta(days=dias)
                        if dias is not None else None))
    db.session.add(s); db.session.commit()
    return s


def _refs(hallazgos):
    return {h.referencia for h in hallazgos}


def test_un_traslado_reciente_no_se_reporta(db, usuario):
    """Siete días es operación normal: el promedio medido es 7,6."""
    _en_transito(db, usuario, 'ST-JOVEN', dias=7)
    assert _refs(se_pueden_contar_los_traslados_en_vuelo()) == set()


def test_uno_pasado_el_umbral_si_se_reporta(db, usuario):
    _en_transito(db, usuario, 'ST-VIEJO', dias=DIAS_EN_TRANSITO_ANORMAL + 5)
    h = se_pueden_contar_los_traslados_en_vuelo()
    assert 'ST-VIEJO' in _refs(h)
    assert any(x.datos.get('umbral') == DIAS_EN_TRANSITO_ANORMAL for x in h)


def test_el_borde_exacto_del_umbral_se_reporta(db, usuario):
    _en_transito(db, usuario, 'ST-BORDE', dias=DIAS_EN_TRANSITO_ANORMAL)
    assert 'ST-BORDE' in _refs(se_pueden_contar_los_traslados_en_vuelo())


def test_justo_debajo_del_umbral_no(db, usuario):
    _en_transito(db, usuario, 'ST-CASI', dias=DIAS_EN_TRANSITO_ANORMAL - 1)
    assert 'ST-CASI' not in _refs(se_pueden_contar_los_traslados_en_vuelo())


def test_los_sin_fecha_van_en_UN_solo_hallazgo(db, usuario):
    """Los 19 de producción están así. Diecinueve líneas repitiendo «no sé hace
    cuánto» no agregan información sobre una sola que lo diga."""
    for i in range(5):
        _en_transito(db, usuario, f'ST-SF-{i}', dias=None)
    h = se_pueden_contar_los_traslados_en_vuelo()
    agregados = [x for x in h if x.referencia == 'sin-fecha-de-despacho']
    assert len(agregados) == 1
    assert agregados[0].datos['total'] == 5
    assert len(h) == 1, 'emitió uno por cada uno además del agregado'


def test_sin_fecha_y_viejos_se_distinguen(db, usuario):
    """Dirección contraria: agregar los sin-fecha no puede tapar a los viejos."""
    for i in range(3):
        _en_transito(db, usuario, f'ST-SF2-{i}', dias=None)
    _en_transito(db, usuario, 'ST-ATASCADO', dias=DIAS_EN_TRANSITO_ANORMAL + 30)
    refs = _refs(se_pueden_contar_los_traslados_en_vuelo())
    assert 'ST-ATASCADO' in refs
    assert 'sin-fecha-de-despacho' in refs


def test_sin_traslados_en_vuelo_no_dice_nada(db):
    assert se_pueden_contar_los_traslados_en_vuelo() == []


def test_el_umbral_tiene_base_medida():
    """Regla 13: un número con nombre tiene base y fecha. Si alguien lo cambia
    sin medir, este test lo obliga a leer de dónde salió."""
    import inspect
    from app.services.auditoria import traslados as mod
    fuente = inspect.getsource(mod)
    i = fuente.index('DIAS_EN_TRANSITO_ANORMAL =')
    contexto = fuente[max(0, i - 700):i]
    assert '2026-09-17' in contexto, 'el umbral perdió su fecha de medición'
    assert 'promedio' in contexto, 'el umbral perdió la medición que lo funda'
