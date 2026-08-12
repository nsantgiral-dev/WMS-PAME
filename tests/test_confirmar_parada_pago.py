"""
Pago Total/Parcial en la pantalla de conductor (última milla, pedidos de
contado) — valida solo lo nuevo agregado a RutaService.confirmar_parada:
motivo_descuento debe existir en el catálogo real de retenciones, y
monto_descuento no puede ser negativo. El resto de confirmar_parada
(bultos, forma de pago, fotos) no se toca acá.
"""
import pytest


@pytest.fixture
def parada_lista(db, almacen):
    """Ruta EN_TRANSITO + tarea + bulto, listos para confirmar_parada."""
    from app.models.usuario import Usuario
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho
    from app.models.packing import TareaPacking
    from app.models.bulto import Bulto
    import uuid

    user = Usuario(email='cond_pago@test.com', nombre='Conductor Pago', rol='conductor', activo=True)
    user.set_password('test123')
    db.session.add(user)
    db.session.flush()
    conductor = Conductor(usuario_id=user.id, nombre='Conductor Pago', cedula='9998887', activo=True)
    db.session.add(conductor)
    db.session.flush()

    ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
    db.session.add(ruta)
    db.session.flush()

    tarea = TareaPacking(
        codigo=f'PK-PAGO-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
        almacen_id=almacen.id,
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1500,
        numero_pedido_siesa='PED-PAGO',
    )
    db.session.add(tarea)
    db.session.flush()

    bulto = Bulto(
        tarea_id=tarea.id, codigo_barras=f'PAGO-{uuid.uuid4().hex[:6]}',
        tipo='Caja', numero=1, total=1, estado='CARGADO',
        ruta_despacho_id=ruta.id,
    )
    db.session.add(bulto)
    db.session.commit()
    return ruta, tarea, conductor.usuario_id


class TestMotivoDescuento:

    def test_motivo_valido_se_guarda_con_monto_descuento(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': 'EFECTIVO',
            'monto_cobrado': 80000,
            'motivo_descuento': 'RETEFUENTE_2.5',
            'monto_descuento': 20000,
        })

        from app.models.recaudo_entrega import RecaudoEntrega
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        assert recaudo.motivo_descuento == 'RETEFUENTE_2.5'
        assert float(recaudo.monto_descuento) == 20000
        assert float(recaudo.monto_cobrado) == 80000

    def test_motivo_invalido_rechaza(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        with pytest.raises(ValueError, match='motivo_descuento inválido'):
            RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
                'estado_entrega': 'ENTREGADO',
                'forma_pago': 'EFECTIVO',
                'monto_cobrado': 80000,
                'motivo_descuento': 'ICA_3',  # clave vieja, ya no existe en el catálogo
            })

    def test_monto_descuento_negativo_rechaza(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        with pytest.raises(ValueError, match='monto_descuento no puede ser negativo'):
            RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
                'estado_entrega': 'ENTREGADO',
                'forma_pago': 'EFECTIVO',
                'monto_cobrado': 80000,
                'monto_descuento': -100,
            })

    def test_sin_motivo_no_rompe_flujo_existente(self, app, db, parada_lista):
        """El caso de siempre (crédito, o contado sin descuento) sigue igual."""
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
            'estado_entrega': 'ENTREGADO',
            'forma_pago': 'CREDITO',
            'monto_cobrado': 100000,
        })

        from app.models.recaudo_entrega import RecaudoEntrega
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        assert recaudo.motivo_descuento is None
        assert float(recaudo.monto_descuento) == 0


class TestParcialVuelveAExigirPago:
    """Con la pantalla unificada, 'Parcial' pasa por el mismo toggle
    Pago Total/Parcial que Entregado — forma de pago y monto>0 vuelven a
    ser obligatorios (se habían quitado en un intento anterior que rompía
    liquidar_ruta_siesa, ver conversación: _procesar_recaudo necesita
    forma_pago para distinguir crédito/contado y monto>0 para el RC)."""

    def test_parcial_sin_forma_pago_rechaza(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        with pytest.raises(ValueError, match='Forma de pago es obligatoria'):
            RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
                'estado_entrega': 'PARCIAL',
                'monto_cobrado': 40000,
                'observaciones': 'Cliente no quiso 2 unidades',
                'bultos_rechazados': [],
            })

    def test_parcial_sin_monto_rechaza(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        with pytest.raises(ValueError, match='monto cobrado debe ser mayor a 0'):
            RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
                'estado_entrega': 'PARCIAL',
                'forma_pago': 'EFECTIVO',
                'monto_cobrado': 0,
                'observaciones': 'Cliente no quiso 2 unidades',
                'bultos_rechazados': [],
            })

    def test_parcial_con_forma_pago_y_monto_pasa(self, app, db, parada_lista):
        from app.services.ruta_service import RutaService
        ruta, tarea, usuario_id = parada_lista

        recaudo_id, _ = RutaService.confirmar_parada(ruta.id, tarea.id, usuario_id, {
            'estado_entrega': 'PARCIAL',
            'forma_pago': 'EFECTIVO',
            'monto_cobrado': 40000,
            'observaciones': 'Cliente no quiso 2 unidades',
            'bultos_rechazados': [],
        })

        from app.models.recaudo_entrega import RecaudoEntrega
        recaudo = RecaudoEntrega.query.get(recaudo_id)
        assert recaudo.forma_pago == 'EFECTIVO'
        assert float(recaudo.monto_cobrado) == 40000


class TestCatalogoRetencionesUnaFuente:
    """El bug que originó esta sesión: 3 diccionarios paralelos que
    divergieron. Verifica que RETENCION_PUC/RETENCION_TASA sigan siendo
    vistas derivadas de CATALOGO_RETENCIONES, nunca datos independientes."""

    def test_puc_y_tasa_derivan_del_mismo_catalogo(self):
        from app.services.liquidacion_service import (
            CATALOGO_RETENCIONES, RETENCION_PUC, RETENCION_TASA,
        )
        assert set(RETENCION_PUC) == set(CATALOGO_RETENCIONES)
        assert set(RETENCION_TASA) == set(CATALOGO_RETENCIONES)
        for tipo, datos in CATALOGO_RETENCIONES.items():
            assert RETENCION_PUC[tipo] == datos['puc']
            assert RETENCION_TASA[tipo] == datos['tasa']

    def test_ica_puc_tasa_correctos_verificado_2026_08_12(self):
        """Valores confirmados contra el gestor de cartera real de Siesa —
        antes del fix, la tasa correcta estaba pegada a la cuenta PUC
        equivocada en las 5 cuentas ICA."""
        from app.services.liquidacion_service import CATALOGO_RETENCIONES
        esperado = {
            'ICA_4X1000':  ('13551801', 0.004),
            'ICA_3X1000':  ('13551802', 0.003),
            'ICA_6X1000':  ('13551803', 0.006),
            'ICA_10X1000': ('13551804', 0.010),
            'ICA_11X1000': ('13551805', 0.011),
        }
        for tipo, (puc, tasa) in esperado.items():
            assert CATALOGO_RETENCIONES[tipo]['puc'] == puc
            assert CATALOGO_RETENCIONES[tipo]['tasa'] == tasa


class TestListarParadasValorPorReferencia:
    """listar_paradas ahora anota cada item con valor_unitario (Siesa) para
    que el conductor pueda recalcular el valor a cobrar si ajusta cantidades
    en una entrega — sin esto, "Entrega con ajuste" no puede confiar en un
    Valor a Cobrar dinámico y cae al campo libre (ver Regla 0)."""

    def test_items_traen_valor_unitario_real(self, app, db, almacen):
        from unittest.mock import patch
        from app.models.usuario import Usuario
        from app.models.conductor import Conductor
        from app.models.ruta_despacho import RutaDespacho
        from app.models.packing import TareaPacking, ItemPacking
        from app.models.producto import Producto
        from app.models.bulto import Bulto
        import uuid

        user = Usuario(email='cond_valor@test.com', nombre='Conductor Valor', rol='conductor', activo=True)
        user.set_password('test123')
        db.session.add(user)
        db.session.flush()
        conductor = Conductor(usuario_id=user.id, nombre='Conductor Valor', cedula='9998888', activo=True)
        db.session.add(conductor)
        db.session.flush()

        ruta = RutaDespacho(conductor_id=conductor.id, tipo_ruta='Urbana', estado='EN_TRANSITO')
        db.session.add(ruta)
        db.session.flush()

        tarea = TareaPacking(
            codigo=f'PK-VAL-{uuid.uuid4().hex[:6]}', estado='DESPACHADO',
            almacen_id=almacen.id,
            tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa=1600,
            numero_pedido_siesa='PED-VAL',
        )
        db.session.add(tarea)
        db.session.flush()

        producto = Producto(codigo=f'PROD-{uuid.uuid4().hex[:6]}', nombre='Resma de prueba')
        db.session.add(producto)
        db.session.flush()

        item = ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                            cantidad_esperada=5, cantidad_real=5)
        db.session.add(item)

        bulto = Bulto(tarea_id=tarea.id, codigo_barras=f'VAL-{uuid.uuid4().hex[:6]}',
                       tipo='Caja', numero=1, total=1, estado='CARGADO',
                       ruta_despacho_id=ruta.id)
        db.session.add(bulto)
        db.session.commit()

        with patch('app.services.fe_resolver.resolver_fe_o_none', return_value=('FEW', '1600')), \
             patch('app.services.connekta_gateway.connekta.get_rowids_factura', return_value=[
                 {'f120_referencia': producto.codigo, 'f470_cant_base': 5, 'f470_vlr_neto': 50000},
             ]), \
             patch('app.services.connekta_gateway.connekta.get_pedido_cabecera',
                   return_value={'f430_id_cond_pago': 'C01'}), \
             patch('app.services.connekta_gateway.connekta.cond_pago_ventas', 'C01'):
            from app.services.ruta_service import RutaService
            resultado = RutaService.listar_paradas(ruta.id)

        parada = resultado['paradas'][0]
        assert parada['valor_factura'] == 50000
        assert parada['es_contado'] is True
        assert parada['items'][0]['valor_unitario'] == 10000  # 50000 / 5 unidades
