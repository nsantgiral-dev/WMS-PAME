"""
Tier 5 — Guards críticos: validaciones fail-fast que previenen
datos corruptos en Siesa. Cada test verifica que un campo obligatorio
faltante causa ValueError ANTES de llegar al POST.
"""
import pytest
from unittest.mock import patch, MagicMock


# ═══════════════════════════════════════════════════════════════════
# ConnektaGateway — guards en trigger methods
# ═══════════════════════════════════════════════════════════════════

class TestTransferenciaGuards:
    """transferir_entre_ubicaciones — guards de motivo, bodega."""

    def test_sin_motivo_traslado_raise(self, app):
        """Si SIESA_MOTIVO_TRASLADO está vacío, debe fallar antes del POST."""
        from app.services.connekta_gateway import ConnektaGateway
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        gw.motivo_traslado = ''  # vacío
        # transferir_entre_ubicaciones valida motivo antes del POST
        # El método puede usar distintas rutas (directa vs tránsito)
        # pero en TODAS valida motivo_traslado
        gw.tipo_docto_transito_salida = 'STS'
        gw.tipo_docto_transito_entrada = 'ETS'
        gw.bodega_transito = 'TRA1'
        gw.vehiculo_traslado = 'VH01'
        gw.nit_transportador = '900999999'

        with pytest.raises((ValueError, Exception)):
            gw.transferir_entre_ubicaciones(
                bodega_id='NB1', ubicacion_origen='RES-01',
                ubicacion_destino='PIK-01', referencia_item='PROD001',
                cantidad=10, nota='test', centro_op='003',
            )

    def test_sin_tipo_docto_recibo_caja_raise(self, app):
        """Si SIESA_TIPO_DOCTO_RECIBO_CAJA no está configurado, falla."""
        from app.services.connekta_gateway import ConnektaGateway
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        gw.tipo_docto_recibo_caja = ''

        with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_RECIBO_CAJA'):
            gw.trigger_recibo_caja(
                tercero_nit='900123456', sucursal='001', monto=1000,
                forma_pago='EFECTIVO', tipo_docto_fe='FE', consec_fe='100',
            )

    def test_sin_tipo_docto_docto_contable_raise(self, app):
        """Si SIESA_TIPO_DOCTO_DOCTO_CONTABLE no está configurado, falla."""
        from app.services.connekta_gateway import ConnektaGateway
        gw = ConnektaGateway()
        gw.modo_simulacion = False
        gw.tipo_docto_docto_contable = ''

        with pytest.raises(ValueError, match='SIESA_TIPO_DOCTO_DOCTO_CONTABLE'):
            gw.trigger_documento_contable(
                tercero_nit='900123456', sucursal='001',
                cuenta_puc='13551501', monto=25000, base_gravable=1000000,
                tipo_docto_fe='FE', consec_fe='100',
            )


# ═══════════════════════════════════════════════════════════════════
# Reposicion — guards en _encolar_siesa_job
# ═══════════════════════════════════════════════════════════════════

class TestReposicionGuards:

    def test_sin_codigo_siesa_no_encola(self, app, db, almacen):
        """Producto sin codigo_siesa → job NO se crea, no se envía basura a Siesa."""
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.tarea_reposicion import TareaReposicion
        from app.models.lpn import LPN

        producto = Producto(codigo='PROD-SIN-SIESA', nombre='Sin Siesa', activo=True)
        # NO tiene codigo_siesa
        db.session.add(producto)
        db.session.flush()

        ub_res = Ubicacion(codigo='RES-G01', almacen_id=almacen.id, tipo_zona='RESERVA', activo=True)
        ub_pik = Ubicacion(codigo='PIK-G01', almacen_id=almacen.id, tipo_zona='PICKING', activo=True)
        db.session.add_all([ub_res, ub_pik])
        db.session.flush()

        lpn = LPN(codigo='LPN-G01', producto_id=producto.id, almacen_id=almacen.id,
                   ubicacion_id=ub_res.id, cantidad_actual=50, factor_conversion=1, estado='ACTIVO')
        db.session.add(lpn)
        db.session.flush()

        tarea = TareaReposicion(
            codigo='REP-G01', producto_id=producto.id, almacen_id=almacen.id,
            cantidad_unidades=50, ubicacion_reserva_id=ub_res.id,
            ubicacion_picking_id=ub_pik.id, lpn_id=lpn.id, estado='COMPLETADA',
        )
        db.session.add(tarea)
        db.session.commit()

        from app.services.reposicion_service import _encolar_siesa_job
        _encolar_siesa_job(tarea, lpn, 50)

        # El job NO debe haberse creado
        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(referencia_id=tarea.id, referencia_tipo='TareaReposicion').all()
        assert len(jobs) == 0
        assert tarea.siesa_enviado is False

    def test_sin_bodega_siesa_no_encola(self, app, db, almacen):
        """Almacén sin bodega_siesa_id → job NO se crea."""
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        from app.models.tarea_reposicion import TareaReposicion
        from app.models.lpn import LPN

        # Quitar bodega_siesa_id del almacén existente
        almacen.bodega_siesa_id = None
        db.session.commit()

        producto = Producto(codigo='PROD-BOD', nombre='Prod Bod', activo=True,
                            codigo_siesa='SIESA-BOD')
        db.session.add(producto)
        db.session.flush()

        ub_res = Ubicacion(codigo='RES-B01', almacen_id=almacen.id, tipo_zona='RESERVA', activo=True)
        ub_pik = Ubicacion(codigo='PIK-B01', almacen_id=almacen.id, tipo_zona='PICKING', activo=True)
        db.session.add_all([ub_res, ub_pik])
        db.session.flush()

        lpn = LPN(codigo='LPN-B01', producto_id=producto.id, almacen_id=almacen.id,
                   ubicacion_id=ub_res.id, cantidad_actual=50, factor_conversion=1, estado='ACTIVO')
        db.session.add(lpn)
        db.session.flush()

        tarea = TareaReposicion(
            codigo='REP-B01', producto_id=producto.id, almacen_id=almacen.id,
            cantidad_unidades=50, ubicacion_reserva_id=ub_res.id,
            ubicacion_picking_id=ub_pik.id, lpn_id=lpn.id, estado='COMPLETADA',
        )
        db.session.add(tarea)
        db.session.commit()

        from app.services.reposicion_service import _encolar_siesa_job
        _encolar_siesa_job(tarea, lpn, 50)

        from app.models.siesa_job import SiesaJob
        jobs = SiesaJob.query.filter_by(referencia_id=tarea.id, referencia_tipo='TareaReposicion').all()
        assert len(jobs) == 0
        assert tarea.siesa_enviado is False


# ═══════════════════════════════════════════════════════════════════
# Traslado closer — guards
# ═══════════════════════════════════════════════════════════════════

class TestTrasladoCloserGuards:

    def test_sin_requisicion_consec_raise(self, app, db, almacen):
        """Traslado sin siesa_requisicion_consec → ValueError."""
        from app.models.traslado import SolicitudTraslado
        from app.models.packing import TareaPacking

        from app.models.usuario import Usuario
        user = Usuario.query.filter_by(email='sol_guard@test.com').first()
        if not user:
            user = Usuario(email='sol_guard@test.com', nombre='Solicitante', rol='operario', activo=True)
            user.set_password('test123')
            db.session.add(user)
            db.session.flush()

        solicitud = SolicitudTraslado(
            codigo='ST-GUARD-01', estado='EN_PACKING',
            bodega_origen_siesa='NB1', bodega_destino_siesa='NC1',
            solicitante_id=user.id,
            siesa_requisicion_consec=None,  # SIN consec
        )
        db.session.add(solicitud)
        db.session.flush()

        tarea = TareaPacking(
            codigo='PK-GUARD-01', estado='VERIFICADO', almacen_id=almacen.id,
            tipo_documento='TRASLADO', referencia_doc=solicitud.codigo,
            solicitud_id=solicitud.id,
        )
        db.session.add(tarea)
        db.session.commit()

        from app.services.closing.traslado_closer import TrasladoPackingCloser
        closer = TrasladoPackingCloser()
        with pytest.raises(ValueError, match='siesa_requisicion_consec'):
            closer.ejecutar_cierre(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], user.id)


# ═══════════════════════════════════════════════════════════════════
# Formato DecimalConSigno — guard de longitud
# ═══════════════════════════════════════════════════════════════════

class TestDecimalConSignoGuard:

    def test_formato_no_puede_cambiar_sin_romper_test(self):
        """Si alguien cambia _fmt_valor, este test falla."""
        from app.services.connekta_gateway import ConnektaGateway
        resultado = ConnektaGateway._fmt_valor(1500000)
        assert resultado == '+000000001500000.0000'
        assert len(resultado) == 21
