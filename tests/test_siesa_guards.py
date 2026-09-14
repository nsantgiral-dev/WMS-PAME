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
    """Guards de motivo/bodega en los conectores de traslado que siguen en uso.

    transferir_entre_ubicaciones (173066 para RESERVA→PICKING) se retiró
    2026-09-07 — reposición dejó de tocar Siesa (misma bodega en las dos
    puntas, no hay documento real que declarar). Su test de guard vivía acá;
    se borró con la función. transferencia_directa (173066 para traslados
    reales inter-bodega, en siesa_traslado_adapter.py) sigue en uso y no
    tiene guard propio en este archivo.
    """

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
# Traslado closer — guards
# ═══════════════════════════════════════════════════════════════════

class TestTrasladoCloserGuards:

    def test_sin_requisicion_consec_usa_fallback_sin_rit(self, app, db, almacen, producto):
        """Traslado sin siesa_requisicion_consec → NO bloquea el cierre.

        El ejecutor de DESPACHO_TRASLADO (siesa_job_service.py) ya sabe
        despachar sin RIT (cae a 173076 directo, `registrar_salida_transito`)
        — exactamente lo que TrasladoService.despachar() hacía antes de que
        el cierre de PD y ST se unificara en este closer. Bloquear acá
        exigía más de lo que el job de verdad necesita: encontrado en vivo
        el 2026-08-25 (ST-20260825-8F64), donde una RIT huérfana (aceptada
        por Siesa, consecutivo ilegible — ver CLAUDE.md "Las 28
        requisiciones huérfanas") dejaba el traslado sin ninguna forma de
        cerrar caja desde la UI actual.
        """
        from app.models.traslado import SolicitudTraslado
        from app.models.packing import TareaPacking, ItemPacking
        from app.models.siesa_job import SiesaJob

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
            bodega_transito_siesa='TRA1',
            modo_transferencia='EN_TRANSITO',
            solicitante_id=user.id,
            siesa_requisicion_consec=None,  # SIN consec — el caso huérfano
        )
        db.session.add(solicitud)
        db.session.flush()

        tarea = TareaPacking(
            codigo='PK-GUARD-01', estado='VERIFICADO', almacen_id=almacen.id,
            tipo_documento='TRASLADO', referencia_doc=solicitud.codigo,
            solicitud_id=solicitud.id,
        )
        db.session.add(tarea)
        db.session.flush()
        item = ItemPacking(tarea_id=tarea.id, producto_id=producto.id,
                           cantidad_esperada=5, cantidad_real=5, verificado=True)
        db.session.add(item)
        db.session.commit()

        from app.services.closing.traslado_closer import TrasladoPackingCloser
        closer = TrasladoPackingCloser()
        resultado = closer.ejecutar_cierre(tarea.id, [{'tipo': 'Caja', 'cantidad': 1}], user.id)

        assert resultado.exitoso is True

        job = SiesaJob.query.filter_by(
            tipo='DESPACHO_TRASLADO', referencia_tipo='TareaPacking', referencia_id=tarea.id,
        ).first()
        assert job is not None
        payload = job.get_payload()
        # consec_rit ausente/None en el payload es justo lo que hace que el
        # ejecutor caiga al fallback 173076 en vez de 174930.
        assert not payload.get('consec_rit')
        assert payload.get('items')


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
