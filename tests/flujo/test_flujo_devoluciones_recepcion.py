"""
Devolución de cliente y recepción de compras — sus invariantes de frontera.

**Devolución** es el único flujo donde mercancía y dinero vuelven juntos, por
caminos distintos: el inventario lo reingresa el WMS al confirmar la recepción
física; la cartera la cierra la nota crédito en Siesa. Si uno ocurre y el otro
no, el desacuerdo no se ve desde ninguno de los dos lados.

**Recepción** es el espejo de la venta: allá lo caro es que salga mercancía sin
documento; acá, que entre mercancía que el ERP no registró.
"""
import pytest

from app.services import auditoria


# ── Devoluciones ─────────────────────────────────────────────────────────

@pytest.fixture
def devolucion(db, almacen, producto):
    import uuid

    from app.models.devolucion_cliente import (
        DevolucionCliente, EstadoDevolucionCliente, LineaDevolucionCliente)
    from app.models.packing import TareaPacking

    tarea = TareaPacking(codigo=f'PK-DV-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
                         almacen_id=almacen.id, numero_pedido_siesa='PED-DV')
    db.session.add(tarea); db.session.flush()

    d = DevolucionCliente(
        codigo=f'DV-{uuid.uuid4().hex[:6]}', tarea_packing_id=tarea.id,
        numero_pedido_siesa='PED-DV', tipo_docto_fe='FEW', consec_fe='1500',
        cliente='CLIENTE DV', almacen_id=almacen.id,
        estado=EstadoDevolucionCliente.ABIERTA, es_total=False)
    db.session.add(d); db.session.flush()
    db.session.add(LineaDevolucionCliente(
        devolucion_id=d.id, producto_id=producto.id,
        codigo_siesa=producto.codigo_siesa,
        cantidad_facturada=10, cantidad_devuelta=4))
    db.session.commit()
    return d


def _dev(codigo):
    r = auditoria.auditar('devoluciones')
    return next(x for x in r['resultados'] if x['codigo'] == codigo)


class TestDevolucionesSanas:

    def test_ningun_bloqueante(self, devolucion):
        r = auditoria.auditar('devoluciones')
        assert not [x for x in r['resultados']
                    if x['severidad'] == auditoria.BLOQUEA and x['total']]

    def test_ninguno_revienta(self, devolucion):
        assert not auditoria.auditar('devoluciones')['errores']


class TestDetectorDevoluciones:

    def test_ve_que_se_devuelve_mas_de_lo_facturado(self, db, devolucion):
        from app.models.devolucion_cliente import LineaDevolucionCliente
        ln = LineaDevolucionCliente.query.filter_by(devolucion_id=devolucion.id).first()
        ln.cantidad_devuelta = 15      # facturadas: 10
        db.session.commit()
        assert _dev('DEV-01')['total'] == 1

    def test_devolver_menos_es_normal(self, db, devolucion):
        """El caso que el prorrateo del 251126 existe para cubrir."""
        assert _dev('DEV-01')['total'] == 0

    def test_ve_una_confirmada_sin_nota_credito(self, db, devolucion):
        """El inventario ya volvió a la bodega; la cartera del cliente sigue
        con el cargo."""
        devolucion.estado = 'CONFIRMADA'
        db.session.commit()
        assert _dev('DEV-02')['total'] == 1

    def test_ve_una_cancelada_que_dejo_nota_credito(self, db, devolucion):
        devolucion.estado = 'CANCELADA'
        devolucion.siesa_nc_triggered = True
        db.session.commit()
        assert _dev('DEV-03')['total'] == 1

    def test_cuenta_las_nc_esperando_aprobacion(self, db, devolucion):
        """Aprobar es manual y es el estado FINAL, no un fallo (Regla 21) —
        pero una que lleva semanas se ve igual que una de ayer si nadie las
        cuenta."""
        devolucion.estado = 'CONFIRMADA'
        devolucion.siesa_nc_triggered = True
        devolucion.siesa_nc_consec = '57'
        devolucion.siesa_motivo_dian = '1'
        db.session.commit()
        assert _dev('DEV-04')['total'] == 1
        assert _dev('DEV-05')['total'] == 0

    def test_ve_una_nc_sin_motivo_dian(self, db, devolucion):
        """Sin concepto DIAN el botón Aprobar de Siesa ni se habilita."""
        devolucion.estado = 'CONFIRMADA'
        devolucion.siesa_nc_triggered = True
        devolucion.siesa_nc_consec = '58'
        devolucion.siesa_motivo_dian = None
        db.session.commit()
        assert _dev('DEV-05')['total'] == 1


# ── Recepción ────────────────────────────────────────────────────────────

@pytest.fixture
def recepcion(db, almacen, producto):
    import uuid

    from app.models.recepcion import ItemRecepcion, RecepcionMercancia as Recepcion
    r = Recepcion(codigo=f'RC-{uuid.uuid4().hex[:6]}', numero_oc_siesa='OC-900',
                  almacen_id=almacen.id, estado='ABIERTA',
                  proveedor_codigo='PRV1', proveedor_nombre='PROVEEDOR 1')
    db.session.add(r); db.session.flush()
    db.session.add(ItemRecepcion(recepcion_id=r.id, producto_id=producto.id,
                                 cantidad_ordenada=100, cantidad_recibida=98,
                                 tolerancia_exceso_pct=5.0))
    db.session.commit()
    return r


def _rec(codigo):
    x = auditoria.auditar('recepcion')
    return next(i for i in x['resultados'] if i['codigo'] == codigo)


class TestRecepcionSana:

    def test_ningun_bloqueante(self, recepcion):
        r = auditoria.auditar('recepcion')
        assert not [x for x in r['resultados']
                    if x['severidad'] == auditoria.BLOQUEA and x['total']]

    def test_ninguno_revienta(self, recepcion):
        assert not auditoria.auditar('recepcion')['errores']


class TestDetectorRecepcion:

    def test_ve_una_confirmada_sin_entrada_en_siesa(self, db, recepcion):
        recepcion.estado = 'CONFIRMADA'
        db.session.commit()
        assert _rec('REC-01')['total'] == 1

    def test_ve_la_bandera_que_forzo_el_bloque_de_emergencia(self, db, recepcion):
        """El caso que estaba invisible, y es el peor de los dos.

        El job de ENTRADA_OC fuerza `siesa_triggered = True` **cuando la
        respuesta NO se pudo guardar** — correcto, porque sin eso el reintento
        del DLQ crea una entrada duplicada en la cuenta 1435. Pero convierte la
        bandera en «se intentó y quizá entró».

        Leyéndola sola, el guard estaba en verde exactamente sobre el caso donde
        nadie sabe qué pasó. La señal honesta es `siesa_response`: existe solo si
        hubo respuesta y se guardó, que es justo lo que la emergencia no logró.
        """
        recepcion.estado = 'CONFIRMADA'
        recepcion.siesa_triggered = True
        recepcion.siesa_response = None
        db.session.commit()
        assert _rec('REC-01')['total'] == 1, (
            'una recepción marcada como enviada SIN respuesta guardada pasó como '
            'conforme — es la bandera de emergencia, no una entrada confirmada')

    def test_una_entrada_con_respuesta_guardada_no_avisa(self, db, recepcion):
        recepcion.estado = 'CONFIRMADA'
        recepcion.siesa_triggered = True
        recepcion.siesa_response = '{"codigo": 0, "consecutivo": 812}'
        db.session.commit()
        assert _rec('REC-01')['total'] == 0

    def test_el_estado_fantasma_no_vuelve(self):
        """El filtro incluía `'CERRADA'`, que no existe en `EstadoRecepcion`
        (ABIERTA · EN_PROCESO · CONFIRMADA · CANCELADA). Un filtro por un valor
        imposible no acota: decora."""
        import ast
        import pathlib

        from app.models.recepcion import EstadoRecepcion

        # **Por AST, no por texto.** La primera versión de este test buscaba la
        # cadena en el fuente y se puso roja por su PROPIO docstring, que la
        # nombra para explicar el defecto. Es la sexta vez que pasa en este
        # repo: un detector de texto no distingue una constante de una
        # explicación.
        arbol = ast.parse(pathlib.Path('app/services/auditoria/recepcion.py').read_text())
        estados = {
            n.value for n in ast.walk(arbol)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value.isupper() and n.value.isalpha()
        }
        validos = {v for k, v in vars(EstadoRecepcion).items() if not k.startswith('_')}
        fantasmas = {e for e in estados if e.endswith('ADA') or e.endswith('ESO')} - validos
        assert not fantasmas, (
            f'filtra por estados que no existen en EstadoRecepcion: {fantasmas}. '
            f'Un filtro por un valor imposible no acota: decora.')

    def test_un_exceso_dentro_de_tolerancia_no_avisa(self, db, recepcion):
        """Los proveedores mandan de más; por eso hay tolerancia declarada."""
        from app.models.recepcion import ItemRecepcion
        it = ItemRecepcion.query.filter_by(recepcion_id=recepcion.id).first()
        it.cantidad_recibida = 104        # 100 + 5% = 105
        db.session.commit()
        assert _rec('REC-02')['total'] == 0

    def test_ve_el_exceso_por_encima_de_la_tolerancia(self, db, recepcion):
        from app.models.recepcion import ItemRecepcion
        it = ItemRecepcion.query.filter_by(recepcion_id=recepcion.id).first()
        it.cantidad_recibida = 130
        db.session.commit()
        assert _rec('REC-02')['total'] == 1

    def test_ve_una_entrada_por_cero(self, db, recepcion):
        """Una entrada disparada con todo en cero no es una recepción parcial:
        es un documento sin movimiento que el WMS cuenta como trabajo hecho."""
        from app.models.recepcion import ItemRecepcion
        it = ItemRecepcion.query.filter_by(recepcion_id=recepcion.id).first()
        it.cantidad_recibida = 0
        recepcion.siesa_triggered = True
        db.session.commit()
        assert _rec('REC-03')['total'] == 1

    def test_cuenta_las_abiertas(self, recepcion):
        assert _rec('REC-04')['total'] == 1
