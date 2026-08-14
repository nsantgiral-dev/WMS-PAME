"""
El conteo cíclico y sus invariantes.

**Un ajuste de inventario no lo reclama nadie.** Una venta mal facturada la
llama el cliente; un traslado atascado lo pregunta la tienda. Un ajuste de más
o de menos entra al ERP, cuadra el papel contra la realidad equivocada, y la
diferencia solo reaparece en el siguiente conteo físico — meses después, cuando
ya no se puede saber de dónde salió.

Por eso lo que se vigila no es que las cifras se muevan, sino que **cada ajuste
tenga detrás una cuenta física** y que el motivo coincida con el signo.
"""
import pytest

from app.services import auditoria


@pytest.fixture
def sesion(db, almacen, producto, ub_picking):
    """Una sesión de conteo con descuadre, lista para romper de a una cosa."""
    import uuid

    from app.models.conteo import EstadoConteo, SesionConteo
    s = SesionConteo(
        codigo=f'CC-{uuid.uuid4().hex[:6]}', tipo='DIARIO_ABC',
        ubicacion_id=ub_picking.id, almacen_id=almacen.id,
        producto_id=producto.id, producto_codigo_siesa=producto.codigo_siesa,
        estado=EstadoConteo.DESCUADRE,
        existencia_siesa=100, cantidad_fisica=95, diferencia=-5,
        motivo_codigo='AJ-SAL',
    )
    db.session.add(s)
    db.session.commit()
    return s


def _res(codigo):
    r = auditoria.auditar('conteo')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


class TestUnaSesionSanaNoRompeNada:

    def test_ningun_bloqueante(self, sesion):
        r = auditoria.auditar('conteo')
        rotos = [x for x in r['resultados']
                 if x['severidad'] == auditoria.BLOQUEA and x['total']]
        assert not rotos, [x['codigo'] for x in rotos]

    def test_ninguno_revienta(self, sesion):
        assert not auditoria.auditar('conteo')['errores']


class TestElDetectorNoEstaCiego:

    def test_ve_una_diferencia_que_no_es_la_resta(self, db, sesion):
        sesion.diferencia = -50          # las cifras dicen -5
        db.session.commit()
        assert _res('CNT-01')['total'] == 1

    def test_ve_el_motivo_cruzado(self, db, sesion):
        """`AJ-ENT` sobre un faltante mueve el inventario en una dirección y la
        contabilidad en la otra."""
        sesion.motivo_codigo = 'AJ-ENT'   # diferencia negativa = faltante
        db.session.commit()
        assert _res('CNT-02')['total'] == 1

    def test_una_diferencia_cero_no_exige_motivo(self, db, sesion):
        sesion.cantidad_fisica = 100
        sesion.diferencia = 0
        db.session.commit()
        assert _res('CNT-02')['total'] == 0

    def test_ve_un_ajuste_sin_cuenta_fisica(self, db, sesion):
        """`NULL` no es cero: cero es «la ubicación está vacía y lo verifiqué»,
        `NULL` es «nadie fue a mirar»."""
        sesion.cantidad_fisica = None
        sesion.siesa_triggered = True
        db.session.commit()
        assert _res('CNT-03')['total'] == 1

    def test_contar_cero_es_contar(self, db, sesion):
        sesion.cantidad_fisica = 0
        sesion.existencia_siesa = 0
        sesion.diferencia = 0
        sesion.siesa_triggered = True
        db.session.commit()
        assert _res('CNT-03')['total'] == 0

    def test_ve_un_ajuste_sin_segundo_conteo(self, db, sesion):
        sesion.estado = 'AJUSTADO'
        db.session.commit()
        assert _res('CNT-04')['total'] == 1

    def test_con_segundo_conteo_no_avisa(self, db, sesion, almacen, producto, ub_picking):
        import uuid

        from app.models.conteo import SesionConteo
        sesion.estado = 'AJUSTADO'
        db.session.add(SesionConteo(
            codigo=f'CC2-{uuid.uuid4().hex[:6]}', tipo='DIARIO_ABC',
            ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            producto_id=producto.id, estado='MATCH',
            es_segundo_conteo=True, sesion_origen_id=sesion.id))
        db.session.commit()
        assert _res('CNT-04')['total'] == 0

    def test_ve_una_sesion_atascada_ajustando(self, db, sesion):
        """`AJUSTANDO` es una transición, no un estado de reposo: ni contada ni
        ajustada, y ningún proceso la retoma."""
        sesion.estado = 'AJUSTANDO'
        db.session.commit()
        assert _res('CNT-05')['total'] == 1

    def test_cuenta_los_descuadres_abiertos(self, sesion):
        assert _res('CNT-06')['total'] == 1
