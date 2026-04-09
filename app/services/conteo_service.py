"""
Servicio de Conteo Cíclico con Double-Blind Check.
Regla inquebrantable: el operario NUNCA ve la cantidad esperada.
La conciliación se hace en tiempo real contra Siesa.
"""
import uuid
import json
import logging
from datetime import datetime
from app.extensions import db
from app.models.conteo import SesionConteo
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


class ConteoService:

    @staticmethod
    def obtener_tarea_operario(sesion_id: int, operario_id: int):
        """
        Devuelve la tarea al operario — SIN cantidad esperada.
        Solo: ubicacion, producto, descripcion.
        """
        sesion = SesionConteo.query.get(sesion_id)
        if not sesion:
            raise ValueError('Sesión no encontrada')

        if sesion.operario_id and sesion.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')

        if sesion.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'Tarea en estado {sesion.estado} — no disponible')

        # Asignar operario si no tiene
        if not sesion.operario_id:
            sesion.operario_id = operario_id
            sesion.estado = 'EN_PROCESO'
            sesion.fecha_inicio = datetime.utcnow()
            db.session.commit()

        # Retornar SOLO vista ciega — sin cantidad esperada
        return sesion.to_dict_operario()

    @staticmethod
    def registrar_conteo(
        sesion_id: int,
        operario_id: int,
        cantidad_fisica: int,
        lote_id: str = None
    ):
        """
        Registra el conteo físico del operario.
        1. Valida lote si el producto lo requiere.
        2. Consulta existencia real en Siesa en tiempo real.
        3. Compara y decide: MATCH o SEGUNDO_CONTEO.
        """
        sesion = SesionConteo.query.get(sesion_id)
        if not sesion:
            raise ValueError('Sesión no encontrada')

        if sesion.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'No se puede registrar conteo en estado {sesion.estado}')

        if sesion.operario_id and sesion.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')

        # Validar lote obligatorio
        if sesion.maneja_lote and not lote_id:
            raise ValueError(
                f'Este producto maneja lotes. '
                f'El campo lote_id es obligatorio.'
            )

        # Consultar existencia en Siesa en tiempo real
        existencia_siesa = ConteoService._consultar_existencia_siesa(
            producto_codigo_siesa=sesion.producto_codigo_siesa,
            ubicacion_codigo=sesion.ubicacion.codigo if sesion.ubicacion else None,
            lote_id=lote_id
        )

        # Guardar conteo
        sesion.operario_id = operario_id
        sesion.cantidad_fisica = cantidad_fisica
        sesion.existencia_siesa = existencia_siesa
        sesion.lote_id = lote_id
        sesion.fecha_inicio = sesion.fecha_inicio or datetime.utcnow()

        # Conciliar
        diferencia = cantidad_fisica - existencia_siesa

        if diferencia == 0:
            # MATCH — cierre inmediato, cero trabajo administrativo
            sesion.estado = 'MATCH'
            sesion.diferencia = 0
            sesion.fecha_cierre = datetime.utcnow()
            db.session.commit()

            logger.info(
                f'[CONTEO] MATCH en {sesion.codigo} — '
                f'producto {sesion.producto.codigo}'
            )

            return {
                'resultado': 'MATCH',
                'mensaje': 'Conteo correcto — inventario cuadra con Siesa',
                'sesion_id': sesion.id
            }

        else:
            # DESCUADRE — generar segundo conteo por operario diferente
            sesion.estado = 'SEGUNDO_CONTEO'
            sesion.diferencia = diferencia
            db.session.commit()

            segundo_conteo = ConteoService._crear_segundo_conteo(
                sesion_origen=sesion,
                operario_excluido=operario_id
            )

            logger.warning(
                f'[CONTEO] DESCUADRE en {sesion.codigo} — '
                f'diferencia: {diferencia}. Segundo conteo: {segundo_conteo.codigo}'
            )

            return {
                'resultado': 'SEGUNDO_CONTEO',
                'mensaje': 'Diferencia detectada — se generó un segundo conteo para verificación',
                'sesion_id': sesion.id,
                'segundo_conteo_id': segundo_conteo.id
            }

    @staticmethod
    def _consultar_existencia_siesa(
        producto_codigo_siesa: str,
        ubicacion_codigo: str,
        lote_id: str = None
    ):
        """
        Consulta existencia real en Siesa en tiempo real.
        En modo simulación usa el stock local del WMS.
        """
        if connekta.modo_simulacion:
            # Simulación: usar stock local como referencia
            from app.models.inventario import UbicacionProducto
            from app.models.producto import Producto
            from app.models.ubicacion import Ubicacion

            producto = Producto.query.filter_by(
                codigo_siesa=producto_codigo_siesa
            ).first()

            if not producto:
                # Si no tiene codigo_siesa buscar por codigo normal
                producto = Producto.query.filter_by(
                    codigo=producto_codigo_siesa
                ).first()

            if not producto:
                return 0

            ubicacion = Ubicacion.query.filter_by(
                codigo=ubicacion_codigo
            ).first()

            if not ubicacion:
                return 0

            reg = UbicacionProducto.query.filter_by(
                producto_id=producto.id,
                ubicacion_id=ubicacion.id
            ).first()

            return reg.cantidad if reg else 0

        try:
            response = connekta.get_inventario_fecha(producto_codigo_siesa)
            tabla = response.get('detalle', {}).get('Table', [])
            if not tabla:
                return 0
            fila = tabla[0]
            return float(fila.get('cantidad_disponible', fila.get('f470_cant_base', 0)))

        except Exception as e:
            logger.error(f'[CONTEO] Error consultando Siesa: {str(e)}')
            raise Exception(f'Error consultando existencia en Siesa: {str(e)}')

    @staticmethod
    def _crear_segundo_conteo(sesion_origen: SesionConteo, operario_excluido: int):
        """
        Crea segundo conteo asignado a un operario diferente (double-blind).
        El nuevo operario no sabe el resultado del primero.
        """
        codigo = f'CC2-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        segundo = SesionConteo(
            codigo=codigo,
            tipo=sesion_origen.tipo,
            clasificacion_abc=sesion_origen.clasificacion_abc,
            ubicacion_id=sesion_origen.ubicacion_id,
            almacen_id=sesion_origen.almacen_id,
            producto_id=sesion_origen.producto_id,
            producto_codigo_siesa=sesion_origen.producto_codigo_siesa,
            maneja_lote=sesion_origen.maneja_lote,
            estado='PENDIENTE',
            es_segundo_conteo=True,
            sesion_origen_id=sesion_origen.id
        )

        # Buscar operario disponible diferente al primero
        from app.models.usuario import Usuario
        otro_operario = Usuario.query.filter(
            Usuario.id != operario_excluido,
            Usuario.activo == True,
            Usuario.rol.in_(['operario', 'jefe_almacen', 'admin'])
        ).first()

        if otro_operario:
            segundo.operario_id = otro_operario.id

        db.session.add(segundo)
        db.session.commit()
        return segundo

    @staticmethod
    def generar_auditoria_por_excepcion(
        tarea_picking_id: int,
        ubicacion_id: int,
        producto_id: int,
        almacen_id: int,
    ) -> 'SesionConteo':
        """
        Crea una SesionConteo tipo EXCEPCION_PICKING cuando un picker reporta faltante.
        La auditoría aparece en el dashboard del admin como "Urgente".
        El auditor realizará un conteo doble-ciego para determinar el ajuste real.
        """
        from app.models.producto import Producto
        producto = Producto.query.get(producto_id)
        codigo = f'AUD-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

        sesion = SesionConteo(
            codigo=codigo,
            tipo='EXCEPCION_PICKING',
            ubicacion_id=ubicacion_id,
            almacen_id=almacen_id,
            producto_id=producto_id,
            producto_codigo_siesa=producto.codigo_siesa if producto else None,
            maneja_lote=False,
            tarea_picking_id=tarea_picking_id,
            estado='PENDIENTE',
        )
        db.session.add(sesion)
        db.session.flush()

        logger.warning(
            f'[SUPERVISOR_GUARD] Auditoría urgente {codigo} creada '
            f'por excepción en tarea_picking #{tarea_picking_id}'
        )
        return sesion

    @staticmethod
    def confirmar_ajuste(sesion_id: int, supervisor_id: int):
        """
        Después del segundo conteo confirma el descuadre y dispara ajuste a Siesa.
        Siesa actualiza contabilidad automáticamente con el motivo correcto.
        """
        sesion = SesionConteo.query.get(sesion_id)
        if not sesion:
            raise ValueError('Sesión no encontrada')

        # Idempotencia — si Siesa ya procesó este ajuste, devolver sin repetir
        if sesion.siesa_triggered:
            return sesion

        if sesion.estado not in ['SEGUNDO_CONTEO', 'DESCUADRE']:
            raise ValueError(f'No se puede ajustar en estado {sesion.estado}')

        if sesion.cantidad_fisica is None or sesion.existencia_siesa is None:
            raise ValueError('Faltan datos del conteo para generar ajuste')

        diferencia = sesion.cantidad_fisica - sesion.existencia_siesa

        # Determinar motivo según diferencia
        if diferencia > 0:
            motivo_codigo = 'AJ-ENT'
            cantidad_ajuste = diferencia
        else:
            motivo_codigo = 'AJ-SAL'
            cantidad_ajuste = abs(diferencia)

        sesion.diferencia = diferencia
        sesion.motivo_codigo = motivo_codigo

        # Idempotency key estable — basado solo en sesion_id, no en timestamp
        # Así un reintento usa la misma key y Siesa puede deduplicar
        idem_key = f'ADJ-{sesion_id}'
        sesion.idempotency_key = idem_key
        sesion.aprobador_id = supervisor_id

        # Payload para Siesa
        payload_siesa = {
            'centro_operacion': '001',
            'bodega': sesion.ubicacion.almacen.codigo if sesion.ubicacion and sesion.ubicacion.almacen else '',
            'item_id': sesion.producto_codigo_siesa or sesion.producto.codigo,
            'ubicacion': sesion.ubicacion.codigo if sesion.ubicacion else '',
            'cantidad': cantidad_ajuste,
            'motivo_codigo': motivo_codigo,
            'lote': sesion.lote_id,
            'referencia': sesion.codigo,
            'fecha': datetime.utcnow().isoformat(),
            'origen': 'WMS_CONTEO_CICLICO'
        }

        # Trigger a Siesa
        tipo_ajuste = 'ENTRADA' if diferencia > 0 else 'SALIDA'

        try:
            respuesta = connekta.enviar_ajuste_inventario(
                motivo_codigo=motivo_codigo,
                item_codigo=sesion.producto_codigo_siesa or sesion.producto.codigo,
                cantidad=cantidad_ajuste,
                referencia=sesion.codigo
            )
            sesion.siesa_triggered = True
            sesion.siesa_response = json.dumps(respuesta)
            sesion.siesa_triggered_at = datetime.utcnow()
            sesion.estado = 'AJUSTADO'
            sesion.fecha_cierre = datetime.utcnow()

            # Desbloquear inventario si vino de una excepción de picking
            if sesion.tarea_picking_id:
                from app.models.inventario import UbicacionProducto
                inv = (UbicacionProducto.query
                       .filter_by(ubicacion_id=sesion.ubicacion_id,
                                  producto_id=sesion.producto_id)
                       .with_for_update().first())
                if inv:
                    inv.bloqueado = max(0, inv.bloqueado - cantidad_ajuste)
                    # Ajustar stock local para consistencia con Siesa
                    if motivo_codigo == 'AJ-SAL':
                        inv.cantidad = max(0, inv.cantidad - cantidad_ajuste)
                    else:
                        inv.cantidad += cantidad_ajuste

            logger.info(
                f'[SUPERVISOR_GUARD] Ajuste {motivo_codigo} aprobado por usuario #{supervisor_id} '
                f'— {cantidad_ajuste} unidades de {sesion.producto.codigo} '
                f'— idempotency_key: {idem_key}'
            )

        except Exception as e:
            logger.error(f'[CONTEO] Error enviando ajuste a Siesa: {str(e)}')
            sesion.siesa_triggered = False
            sesion.siesa_response = str(e)
            db.session.commit()
            raise Exception(f'Error enviando ajuste a Siesa: {str(e)}')

        db.session.commit()
        return sesion