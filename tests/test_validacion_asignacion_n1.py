"""Validación de la asignación por presencia (integración n1, 2026-09-29).

Cada test reproduce un hallazgo de la validación. Nacieron en
`xfail(strict=True)` (val-asig-n1, e81561ef); cerrados el 2026-09-29, la marca
se quitó y quedan como trinquete del arreglo.
"""
from datetime import datetime, timedelta

import pytest

from tests.test_asignacion_presencia import _conteo, _persona, _sesion  # noqa: F401
from tests.test_asignacion_presencia import nb1  # noqa: F401  (fixture)


def _ausencia_sin_regreso(db, u):
    from app.models.ausencia import AusenciaUsuario
    from app.services import presencia
    a = AusenciaUsuario(usuario_id=u.id, motivo='INCAPACIDAD', desde=presencia._dia(),
                        regreso=None, registrada_en=datetime.utcnow())
    db.session.add(a)
    db.session.commit()
    return a


class TestAusenteQueVolvioYTrabaja:
    """P1. La pantalla deja «Regresa el» vacío por defecto («sin fecha todavía»),
    y una incapacidad casi nunca trae fecha cierta. La persona vuelve, abre la
    PWA y **el dispensador le da trabajo** (el pull no mira la presencia de quien
    pide). A los 15 min el barrido la ve AUSENTE y le quita de las manos, con
    `incluir_en_curso=True`, el conteo que está contando: lo contado va a
    `conteos_descartados` y la sesión vuelve a la cola. Cada 15 min, todo el
    día, hasta que alguien se acuerde de quitar la ausencia."""

    def test_el_conteo_que_le_dio_el_dispensador_no_se_lo_quita_el_barrido(self, db, nb1):
        from app.services import asignacion
        from app.services.mobile_service import MobileService
        luis = nb1['luis']
        _ausencia_sin_regreso(db, luis)
        _conteo(db, nb1['almacen'])

        t = MobileService.get_tarea_actual(luis.id)
        if t is None:
            return  # arreglo válido: al ausente no se le da trabajo (y se le dice por qué)
        assert t['tipo'] == 'CONTEO'
        s = _sesion(db, t['id'])
        assert (s.operario_id, s.estado) == (luis.id, 'EN_PROCESO')
        s.cantidad_fisica = 37          # lleva 37 escaneadas
        db.session.commit()

        asignacion.barrer()
        s = _sesion(db, t['id'])
        assert (s.operario_id, s.cantidad_fisica) == (luis.id, 37), (
            'el barrido le quitó de las manos el conteo que el propio dispensador le dio '
            f'(operario={s.operario_id}, estado={s.estado}, descartados='
            f'{s.lista_conteos_descartados()})')


class TestRepartoReverificaBajoLock:
    """P2. `repartir_conteos` dice «cada conteo se vuelve a verificar bajo lock»,
    pero la fila que relee con `with_for_update()` ya está en el identity map
    (la cargó `plan_reparto_conteos` en la misma sesión) y SQLAlchemy no la
    refresca sin `populate_existing()`: lee `operario_id=None`/`PENDIENTE`
    viejos y sobrescribe al dueño que la tomó entre el plan y el lock."""

    def test_no_pisa_un_conteo_que_otro_tomo_entre_el_plan_y_el_lock(self, db, nb1, monkeypatch):
        from app.services import asignacion
        s = _conteo(db, nb1['almacen'])
        ana = nb1['ana']
        original = asignacion.plan_reparto_conteos

        def plan_y_carrera(*a, **kw):
            r = original(*a, **kw)
            # Mientras tanto, Ana lo toma de la cola (otra transacción ya confirmada).
            db.session.execute(db.text(
                "UPDATE sesiones_conteo SET operario_id=:u, estado='EN_PROCESO' WHERE id=:i"),
                {'u': ana.id, 'i': s.id})
            return r

        monkeypatch.setattr(asignacion, 'plan_reparto_conteos', plan_y_carrera)
        asignacion.repartir_conteos(nb1['almacen'].id, por_id=nb1['maria'].id,
                                    operario_ids=[nb1['luis'].id])
        s = _sesion(db, s.id)
        assert s.operario_id == ana.id, (
            f'el reparto le pasó a otro ({s.operario_id}) un conteo que Ana ya estaba contando')


class TestLoSoltadoNoSeSigueEscribiendo:
    """P1 (consecuencia del anterior, y el mismo hueco que ya tenía el barrido de
    zombis, ahora cada 15 min con gente activa): a la PWA nadie le avisa que el
    conteo se soltó, y `_sesion_conteo_para_contar` acepta escribir en una
    sesión **sin dueño** (`if sesion.operario_id and ...`). El dueño anterior
    sigue tecleando en una sesión que ya está en la cola; el siguiente que la
    toma ve en su HUD (`cantidad_contada`) lo que contó otro: el conteo deja de
    ser ciego y los dos totales se mezclan."""

    def test_quien_la_tenia_no_escribe_en_una_sesion_que_volvio_a_la_cola(self, db, nb1):
        from app.services import asignacion
        from app.services.mobile_service import MobileService
        luis = nb1['luis']
        s = _conteo(db, nb1['almacen'], operario=luis, estado='EN_PROCESO')
        # (Adaptado a la API del arreglo: lo en curso solo se suelta si estaba
        # abandonado respecto de la ausencia — `asignacion.en_curso_abandonado`.)
        s.fecha_inicio = datetime.utcnow() - timedelta(hours=3)
        db.session.commit()
        asignacion.devolver_trabajo_de(luis.id, motivo=asignacion.MOTIVO_AUSENCIA,
                                       en_curso_desde=datetime.utcnow())
        db.session.commit()
        assert _sesion(db, s.id).operario_id is None
        try:
            MobileService.fijar_total_conteo(luis.id, s.id, 55)
        except ValueError:
            return
        s = _sesion(db, s.id)
        assert s.cantidad_fisica is None, (
            f'Luis escribió {s.cantidad_fisica} en una sesión sin dueño que está en la cola: '
            'el próximo que la tome la verá en su HUD')
