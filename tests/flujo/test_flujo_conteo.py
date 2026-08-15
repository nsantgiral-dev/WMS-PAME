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
            es_segundo_conteo=True, sesion_origen_id=sesion.id,
            # **Con cantidad contada.** Una fila hija sin `cantidad_fisica` no
            # es una segunda cuenta: es una sesión que se abrió.
            cantidad_fisica=10))
        db.session.commit()
        assert _res('CNT-04')['total'] == 0

    def test_el_camino_que_SALTA_el_segundo_conteo_se_ve(self, db, sesion, almacen,
                                                         producto, ub_picking):
        """El detector ciego que faltaba, y el caso que el guard existe para ver.

        `POST /api/conteo/<id>/omitir-segundo` deja el hijo en `CANCELADO` **con
        su `sesion_origen_id` intacto**. Preguntando «¿existe una fila hija?»,
        el endpoint diseñado para saltarse la doble ciega producía exactamente
        el dato que hacía decir «sí, se contó dos veces».
        """
        import uuid

        from app.models.conteo import SesionConteo
        sesion.estado = 'AJUSTADO'
        db.session.add(SesionConteo(
            codigo=f'CC2-{uuid.uuid4().hex[:6]}', tipo='DIARIO_ABC',
            ubicacion_id=ub_picking.id, almacen_id=almacen.id,
            producto_id=producto.id, estado='CANCELADO',
            es_segundo_conteo=True, sesion_origen_id=sesion.id))
        db.session.commit()
        assert _res('CNT-04')['total'] == 1, (
            'el hijo cancelado por «omitir segundo conteo» se contó como una '
            'segunda cuenta — el ajuste descansa en una sola persona')

    def test_ve_una_sesion_atascada_ajustando(self, db, sesion):
        """`AJUSTANDO` es una transición, no un estado de reposo: ni contada ni
        ajustada, y ningún proceso la retoma."""
        sesion.estado = 'AJUSTANDO'
        db.session.commit()
        assert _res('CNT-05')['total'] == 1

    def test_cuenta_los_descuadres_abiertos(self, sesion):
        assert _res('CNT-06')['total'] == 1


# ── Reposición ───────────────────────────────────────────────────────────

@pytest.fixture
def reposicion(db, almacen, producto, ub_reserva, ub_picking):
    """Una reposición completada y enviada — el estado sano."""
    import uuid

    from app.models.tarea_reposicion import TareaReposicion
    t = TareaReposicion(
        codigo=f'REP-{uuid.uuid4().hex[:6]}', producto_id=producto.id,
        almacen_id=almacen.id, cantidad_unidades=50,
        ubicacion_reserva_id=ub_reserva.id, ubicacion_picking_id=ub_picking.id,
        estado='COMPLETADA', unidades_movidas=50, siesa_enviado=True)
    db.session.add(t)
    db.session.commit()
    return t


def _rep(codigo):
    r = auditoria.auditar('reposicion')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


class TestReposicionSana:

    def test_ningun_bloqueante(self, reposicion):
        r = auditoria.auditar('reposicion')
        assert not [x for x in r['resultados']
                    if x['severidad'] == auditoria.BLOQUEA and x['total']]

    def test_ninguno_revienta(self, reposicion):
        assert not auditoria.auditar('reposicion')['errores']


class TestDetectorReposicion:
    """El descuadre de este flujo es invisible para cualquier cuadre por sumas:
    el total no cambia, se mueve de una ubicación a otra."""

    def test_ve_una_completada_que_no_llego_a_siesa(self, db, reposicion):
        reposicion.siesa_enviado = False
        db.session.commit()
        assert _rep('REP-01')['total'] == 1

    def _job(self, db, reposicion, estado, intentos=0, error=None):
        from app.models.siesa_job import SiesaJob
        j = SiesaJob(tipo='TRANSFERENCIA_UBICACIONES', estado=estado,
                     referencia_tipo='TareaReposicion', referencia_id=reposicion.id,
                     intentos=intentos, error_ultimo=error)
        j.payload = '{}'
        db.session.add(j)
        db.session.commit()
        return j

    def test_ve_el_doble_envio(self, db, reposicion):
        """El riesgo que este flujo declara: **173066 no es idempotente**, así
        que dos jobs completados son dos movimientos en Siesa y uno solo en el
        WMS.

        La versión anterior de REP-02 pedía `siesa_enviado AND estado !=
        COMPLETADA` — y no había estado alcanzable que lo cumpliera: la bandera
        solo se escribe DENTRO del post-COMPLETADO. Un BLOQUEA en verde
        permanente sobre el único peligro que el módulo nombra.
        """
        self._job(db, reposicion, 'COMPLETADO')
        assert _rep('REP-02')['total'] == 0
        self._job(db, reposicion, 'COMPLETADO')
        assert _rep('REP-02')['total'] == 1

    def test_ve_un_reintento_sobre_un_post_que_pudo_entrar(self, db, reposicion):
        """La Regla 3 y el aborto del DLQ existen por esto; si dejó rastro, hay
        que mirarlo."""
        self._job(db, reposicion, 'FALLIDO', intentos=2, error='timeout')
        assert _rep('REP-02')['total'] == 1

    def test_un_envio_limpio_no_avisa(self, db, reposicion):
        self._job(db, reposicion, 'COMPLETADO')
        assert _rep('REP-02')['total'] == 0

    def test_ve_que_se_movio_mas_de_lo_pedido(self, db, reposicion):
        reposicion.unidades_movidas = 80      # pedidas: 50
        db.session.commit()
        assert _rep('REP-03')['total'] == 1

    def test_mover_menos_es_normal(self, db, reposicion):
        """La ubicación de origen puede no tener todo."""
        reposicion.unidades_movidas = 30
        db.session.commit()
        assert _rep('REP-03')['total'] == 0

    def test_ve_origen_igual_a_destino(self, db, reposicion, ub_picking):
        reposicion.ubicacion_reserva_id = ub_picking.id
        db.session.commit()
        assert _rep('REP-04')['total'] == 1

    def test_cuenta_las_que_estan_en_curso(self, db, reposicion):
        reposicion.estado = 'EN_PROCESO'
        reposicion.siesa_enviado = False
        db.session.commit()
        assert _rep('REP-05')['total'] == 1
