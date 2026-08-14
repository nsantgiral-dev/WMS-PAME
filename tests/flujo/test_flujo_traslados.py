"""
Un traslado recorriendo el WMS, y sus invariantes de frontera.

Es la cadena más larga —cuatro conectores— y la que tiene un limbo propio: en
modo `EN_TRANSITO` la mercancía sale del origen a una **bodega puente** y solo
llega al destino cuando entra el ETS. Si el STS disparó y el ETS no, el stock
no falta ni sobra: está donde nadie pregunta.

Y a diferencia de una venta, **nadie reclama**: el cliente que no recibe su
pedido llama; una tienda que no recibió un traslado que no pidió, no.
"""
import pytest

from app.services import auditoria
from tests.flujo import conductor_de_flujo as cf


@pytest.fixture
def actores_traslado(db):
    from app.models.usuario import Usuario
    out = {}
    for rol, email in (('tienda', 'tienda_tr@test.com'),
                       ('admin', 'admin_tr@test.com'),
                       ('operario', 'op_tr@test.com')):
        u = Usuario.query.filter_by(email=email).first()
        if not u:
            u = Usuario(email=email, nombre=rol, rol=rol, activo=True)
            u.set_password('t')
            db.session.add(u)
            db.session.flush()
        out[rol] = u
    db.session.commit()
    return out


@pytest.fixture
def traslado(db, almacen, actores_traslado):
    return cf.flujo_traslado(db, almacen,
                             actores_traslado['tienda'].id,
                             actores_traslado['admin'].id,
                             actores_traslado['operario'].id)


class TestElFlujoAvanzaConLosServiciosReales:

    def test_la_solicitud_queda_aprobada_y_recortada(self, traslado):
        from app.models.traslado import ItemSolicitudTraslado
        items = ItemSolicitudTraslado.query.filter_by(solicitud_id=traslado.id).all()
        assert items
        assert all(it.cantidad_aprobada == 8 for it in items)
        assert all(x.cantidad_aprobada < x.cantidad_solicitada for x in items), (
            'el recorte es el caso normal; un arnés que solo aprueba completo '
            'verifica un flujo que no es el de todos los días')


class TestLosInvariantesSeCumplenSobreUnTrasladoSano:

    def test_ningun_bloqueante_se_rompe(self, traslado):
        r = auditoria.auditar('traslados')
        rotos = [x for x in r['resultados']
                 if x['severidad'] == auditoria.BLOQUEA and x['total']]
        assert not rotos, '\n' + '\n'.join(
            f"{x['codigo']} ({x['frontera']}): {x['hallazgos'][0]['detalle']}"
            for x in rotos)

    def test_ninguno_revienta(self, traslado):
        assert not auditoria.auditar('traslados')['errores']


class TestElDetectorNoEstaCiego:
    """Se rompe el flujo a propósito y se exige que el invariante lo vea."""

    @staticmethod
    def _res(codigo):
        r = auditoria.auditar('traslados')
        return next(x for x in r['resultados'] if x['codigo'] == codigo)

    def test_ve_que_se_envio_mas_de_lo_aprobado(self, db, traslado):
        from app.models.traslado import ItemSolicitudTraslado
        it = ItemSolicitudTraslado.query.filter_by(solicitud_id=traslado.id).first()
        it.cantidad_enviada = (it.cantidad_aprobada or 0) + 3
        db.session.commit()
        assert self._res('TRA-01')['total'] == 1

    def test_ve_que_se_recibio_mas_de_lo_enviado(self, db, traslado):
        from app.models.traslado import ItemSolicitudTraslado
        it = ItemSolicitudTraslado.query.filter_by(solicitud_id=traslado.id).first()
        it.cantidad_enviada = 5
        it.cantidad_recibida = 9
        db.session.commit()
        assert self._res('TRA-01')['total'] == 1

    def test_aprobada_en_None_no_es_aprobada_en_cero(self, db, traslado):
        """`NULL` significa «todavía no se aprobó». Tratarlo como 0 haría que
        toda solicitud sin aprobar apareciera como violación."""
        from app.models.traslado import ItemSolicitudTraslado
        for it in ItemSolicitudTraslado.query.filter_by(solicitud_id=traslado.id).all():
            it.cantidad_aprobada = None
            it.cantidad_enviada = 0
            it.cantidad_recibida = 0
        db.session.commit()
        assert self._res('TRA-01')['total'] == 0

    def test_ve_un_despacho_sin_salida_en_siesa(self, db, traslado):
        traslado.estado = 'EN_TRANSITO'
        db.session.commit()
        assert self._res('TRA-10')['total'] == 1

    def test_ve_la_mercancia_atrapada_en_la_bodega_puente(self, db, traslado):
        """El limbo: entregada al destino según el WMS, y en el ERP todavía en
        tránsito."""
        traslado.estado = 'ENTREGADA'
        traslado.modo_transferencia = 'EN_TRANSITO'
        traslado.siesa_salida_consec = 555
        traslado.siesa_entrada_consec = None
        db.session.commit()
        assert self._res('TRA-11')['total'] == 1

    def test_el_modo_directo_no_exige_entrada(self, db, traslado):
        """En modo directo (173066) no hay bodega puente: exigir el ETS ahí
        sería un aviso falso en cada traslado directo."""
        traslado.estado = 'ENTREGADA'
        traslado.modo_transferencia = 'DIRECTA'
        traslado.siesa_salida_consec = 555
        db.session.commit()
        assert self._res('TRA-11')['total'] == 0

    def test_ve_una_cancelada_que_ya_movio_inventario(self, db, traslado):
        traslado.estado = 'CANCELADA'
        traslado.siesa_salida_consec = 777
        db.session.commit()
        assert self._res('TRA-20')['total'] == 1

    def test_ve_una_entrada_sin_salida(self, db, traslado):
        traslado.siesa_entrada_consec = 888
        traslado.siesa_salida_consec = None
        db.session.commit()
        assert self._res('TRA-21')['total'] == 1

    def test_ve_un_error_de_siesa_sin_resolver(self, db, traslado):
        traslado.estado = 'EN_PACKING'
        traslado.siesa_error = 'motivo 607 inválido'
        db.session.commit()
        assert self._res('TRA-12')['total'] == 1

    def test_agrupa_por_causa_y_no_por_solicitud(self, db, almacen,
                                                 actores_traslado):
        """53 filas no se pueden triar; dos causas sí.

        La primera corrida contra producción devolvió 53 hallazgos de TRA-12 —
        traslados distintos con el mismo puñado de errores repetidos. Un
        hallazgo por fila convierte un problema en una lista, y una lista larga
        se ignora.
        """
        from tests.flujo import conductor_de_flujo as cf
        mismos = [cf.flujo_traslado(db, almacen, actores_traslado['tienda'].id,
                                    actores_traslado['admin'].id,
                                    actores_traslado['operario'].id)
                  for _ in range(3)]
        for i, s in enumerate(mismos):
            s.estado = 'EN_PACKING'
            # Mismo error, distinto número de documento: una sola causa.
            s.siesa_error = f'RIT 174646: error en la linea 3 del documento {900 + i}'
        db.session.commit()

        r = self._res('TRA-12')
        assert r['total'] == 1, 'tres traslados con la misma causa son un hallazgo'
        assert '3 traslado(s)' in r['hallazgos'][0]['referencia']

    def test_causas_distintas_no_se_mezclan(self, db, almacen, actores_traslado):
        from tests.flujo import conductor_de_flujo as cf
        a = cf.flujo_traslado(db, almacen, actores_traslado['tienda'].id,
                              actores_traslado['admin'].id,
                              actores_traslado['operario'].id)
        b = cf.flujo_traslado(db, almacen, actores_traslado['tienda'].id,
                              actores_traslado['admin'].id,
                              actores_traslado['operario'].id)
        a.estado = b.estado = 'EN_PACKING'
        a.siesa_error = 'RIT 174646: error al importar el plano'
        b.siesa_error = 'motivo de traslado invalido'
        db.session.commit()
        assert self._res('TRA-12')['total'] == 2

    def test_un_error_ya_superado_no_grita(self, db, traslado):
        """Con el movimiento de cierre presente, el error es ruido histórico —
        el reintento funcionó. Avisar igual entrena a ignorar el canal."""
        traslado.estado = 'ENTREGADA'
        traslado.modo_transferencia = 'EN_TRANSITO'
        traslado.siesa_error = 'timeout en el primer intento'
        traslado.siesa_salida_consec = 555
        traslado.siesa_entrada_consec = 666
        db.session.commit()
        assert self._res('TRA-12')['total'] == 0
