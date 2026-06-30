"""
Servicio de Conteo Cíclico con Double-Blind Check.
Regla inquebrantable: el operario NUNCA ve la cantidad esperada.
Fuente de verdad: Siesa (consultado al registrar el conteo). WMS solo como fallback si Siesa no responde.
"""
import uuid
import logging
from datetime import datetime
from app.extensions import db
from app.models.conteo import SesionConteo, EstadoConteo
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)


class ConteoService:

    @staticmethod
    def obtener_tarea_operario(sesion_id: int, operario_id: int):
        """
        Devuelve la tarea al operario — SIN cantidad esperada.
        Solo: ubicacion, producto, descripcion.
        """
        sesion = (SesionConteo.query
                  .filter_by(id=sesion_id)
                  .with_for_update()
                  .first())
        if not sesion:
            raise ValueError('Sesión no encontrada')

        if sesion.operario_id and sesion.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')

        if sesion.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'Tarea en estado {sesion.estado} — no disponible')

        # Asignar operario si no tiene
        if not sesion.operario_id:
            sesion.operario_id = operario_id
            sesion.estado = EstadoConteo.EN_PROCESO
            sesion.fecha_inicio = datetime.utcnow()
            try:
                db.session.commit()
            except Exception as e_commit:
                db.session.rollback()
                raise ValueError(f'Error al asignar sesión de conteo: {e_commit}') from e_commit

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
        2. Consulta stock WMS (UbicacionProducto.cantidad) — sin llamada HTTP.
        3. Adquiere lock, re-valida estado y guarda.
        4. Decide: MATCH o SEGUNDO_CONTEO.
        """
        from app.models.inventario import UbicacionProducto

        # Lectura previa sin lock — validaciones básicas
        sesion_pre = SesionConteo.query.filter_by(id=sesion_id).first()
        if not sesion_pre:
            raise ValueError('Sesión no encontrada')
        if sesion_pre.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'No se puede registrar conteo en estado {sesion_pre.estado}')
        if sesion_pre.operario_id and sesion_pre.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')
        if sesion_pre.maneja_lote and not lote_id:
            raise ValueError('Este producto maneja lotes. El campo lote_id es obligatorio.')

        # Siesa es la fuente de verdad. Se consulta antes del lock para no
        # mantener la transacción abierta durante la llamada HTTP.
        _bodega_siesa = None
        if sesion_pre.almacen_id:
            from app.models.almacen import Almacen as _Alm
            _alm = _Alm.query.get(sesion_pre.almacen_id)
            _bodega_siesa = _alm.bodega_siesa_id if _alm else None

        existencia_ref = None
        if sesion_pre.producto_codigo_siesa and _bodega_siesa:
            existencia_ref = ConteoService.consultar_existencia_siesa(
                producto_codigo_siesa=sesion_pre.producto_codigo_siesa,
                bodega=_bodega_siesa,
            )

        if existencia_ref is None:
            # Fallback a WMS si Siesa no responde
            reg_inv = UbicacionProducto.query.filter_by(
                ubicacion_id=sesion_pre.ubicacion_id,
                producto_id=sesion_pre.producto_id,
            ).first()
            existencia_ref = float(reg_inv.cantidad) if reg_inv else 0.0
            logger.warning(
                f'[CONTEO] Siesa no respondió para {sesion_pre.codigo} '
                f'— diferencia calculada contra WMS ({existencia_ref})'
            )
        else:
            logger.info(
                f'[CONTEO] Referencia Siesa para {sesion_pre.codigo}: '
                f'{existencia_ref} und (bodega={_bodega_siesa})'
            )

        # Adquirir lock pesimista para el update atómico
        sesion = (SesionConteo.query
                  .filter_by(id=sesion_id)
                  .with_for_update()
                  .first())
        if not sesion:
            raise ValueError('Sesión no encontrada')

        # Re-validar estado bajo lock
        if sesion.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'No se puede registrar conteo en estado {sesion.estado}')
        if sesion.operario_id and sesion.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')

        sesion.operario_id = operario_id
        sesion.cantidad_fisica = cantidad_fisica
        sesion.existencia_siesa = existencia_ref
        sesion.lote_id = lote_id
        sesion.fecha_inicio = sesion.fecha_inicio or datetime.utcnow()

        resultado_conciliacion = ConteoService.reconciliar_cantidad(sesion, cantidad_fisica)
        diferencia = resultado_conciliacion['diferencia']

        if resultado_conciliacion['es_match']:
            # CC2 o CC3 cuadra con Siesa → cierra también el padre (sin ajuste)
            if sesion.es_segundo_conteo and sesion.sesion_origen_id:
                origen = SesionConteo.query.get(sesion.sesion_origen_id)
                if origen:
                    # CC3: raíz es el padre del padre (CC1)
                    es_tercer = origen.es_segundo_conteo and origen.sesion_origen_id
                    raiz = SesionConteo.query.get(origen.sesion_origen_id) if es_tercer else origen
                    if raiz and raiz.estado in (EstadoConteo.SEGUNDO_CONTEO, EstadoConteo.TERCER_CONTEO):
                        raiz.estado = EstadoConteo.MATCH
                        raiz.fecha_cierre = datetime.utcnow()
                        logger.info(f'[CONTEO] CC{"3" if es_tercer else "2"} MATCH — raíz {raiz.codigo} → MATCH')
            try:
                db.session.commit()
            except Exception as e_commit:
                db.session.rollback()
                logger.error(f'[CONTEO] Error al guardar MATCH para sesión {sesion_id}: {e_commit}')
                raise

            logger.info(
                f'[CONTEO] MATCH en {sesion.codigo} — '
                f'producto {sesion.producto.codigo} — operario_id={operario_id}'
            )
            return {
                'resultado': 'MATCH',
                'mensaje': 'Conteo correcto — inventario cuadra con WMS',
                'sesion_id': sesion.id
            }

        else:
            if sesion.es_segundo_conteo:
                origen = SesionConteo.query.get(sesion.sesion_origen_id) if sesion.sesion_origen_id else None
                es_tercer = origen is not None and origen.es_segundo_conteo

                if es_tercer:
                    # CC3 es definitivo: propaga su cantidad a la raíz (CC1) y cierra
                    raiz = SesionConteo.query.get(origen.sesion_origen_id) if origen.sesion_origen_id else None
                    sesion.estado = EstadoConteo.DESCUADRE
                    if raiz and raiz.estado == EstadoConteo.TERCER_CONTEO:
                        raiz.estado = EstadoConteo.DESCUADRE
                        raiz.cantidad_fisica = cantidad_fisica  # CC3 es la verdad definitiva
                        raiz.diferencia = diferencia
                        raiz.existencia_siesa = existencia_ref
                        raiz.motivo_codigo = 'AJ-ENT' if diferencia > 0 else 'AJ-SAL'
                        logger.warning(f'[CONTEO] CC3 DESCUADRE — raíz {raiz.codigo} → DESCUADRE, cantidad={cantidad_fisica}')
                    try:
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        raise ValueError(f'Error al registrar descuadre CC3: {e}')
                    return {
                        'resultado': 'DESCUADRE',
                        'mensaje': 'Tercer conteo registrado — el administrador debe revisar y aprobar el ajuste',
                        'sesion_id': sesion.id,
                    }

                # CC2 no cuadra con Siesa — comparar CC1 vs CC2
                cc1_cantidad = origen.cantidad_fisica if origen else None
                sesion.estado = EstadoConteo.DESCUADRE

                if cc1_cantidad is not None and cantidad_fisica == cc1_cantidad:
                    # CC1 == CC2: "verdad de bodega" — ajuste automático sin esperar admin
                    if origen and origen.estado == EstadoConteo.SEGUNDO_CONTEO:
                        origen.estado = EstadoConteo.DESCUADRE
                        try:
                            ConteoService._encolar_ajuste_fisico(origen, aprobador_id=None)
                            logger.warning(
                                f'[CONTEO] CC2 confirma CC1 ({cc1_cantidad} uds) — '
                                f'padre {origen.codigo} → AJUSTANDO (auto)'
                            )
                        except Exception as e_enq:
                            logger.error(
                                f'[CONTEO] Error al auto-encolar ajuste CC1==CC2 '
                                f'para sesion {origen.id}: {e_enq} — queda en DESCUADRE para revisión admin'
                            )
                    try:
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        raise ValueError(f'Error al marcar DESCUADRE y encolar ajuste: {e}')
                    return {
                        'resultado': 'DESCUADRE',
                        'mensaje': 'Ambos conteos coinciden — ajuste encolado automáticamente',
                        'sesion_id': sesion.id,
                    }

                # CC1 ≠ CC2: conteos discordantes → necesita CC3
                if origen and origen.estado == EstadoConteo.SEGUNDO_CONTEO:
                    origen.estado = EstadoConteo.TERCER_CONTEO
                try:
                    cc3 = ConteoService._crear_conteo_verificacion(
                        sesion_origen=sesion,
                        operario_excluido=operario_id,
                    )
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    logger.error(f'[CONTEO] Error al crear CC3 para sesión {sesion_id}: {e}')
                    raise ValueError(f'Error al crear tercer conteo: {e}')
                logger.warning(
                    f'[CONTEO] CC1≠CC2 ({cc1_cantidad} vs {cantidad_fisica}) — '
                    f'CC3 creado: {cc3.codigo}'
                )
                return {
                    'resultado': 'TERCER_CONTEO',
                    'mensaje': 'Los dos conteos no coinciden — se requiere un tercer conteo definitivo',
                    'sesion_id': sesion.id,
                    'tercer_conteo_id': cc3.id,
                }

            # Primer conteo con diferencia — generar segundo conteo (CC2)
            sesion.estado = EstadoConteo.SEGUNDO_CONTEO
            try:
                segundo_conteo = ConteoService._crear_conteo_verificacion(
                    sesion_origen=sesion,
                    operario_excluido=operario_id,
                )
                db.session.commit()
            except Exception as e_segundo:
                db.session.rollback()
                logger.error(f'[CONTEO] Error al crear segundo conteo para sesión {sesion_id}: {e_segundo}')
                raise ValueError(f'Error al crear segundo conteo: {e_segundo}')

            logger.warning(
                f'[CONTEO] DESCUADRE en {sesion.codigo} — '
                f'diferencia: {diferencia}. CC2: {segundo_conteo.codigo}'
            )
            return {
                'resultado': 'SEGUNDO_CONTEO',
                'mensaje': 'Diferencia detectada — se asignó un segundo conteo para verificación',
                'sesion_id': sesion.id,
                'segundo_conteo_id': segundo_conteo.id
            }

    @staticmethod
    def consultar_existencia_siesa(producto_codigo_siesa: str, bodega: str = None):
        """
        Consulta existencia fiscal en Siesa — solo para confirmar_ajuste().
        Retorna float o None si Siesa no responde.
        bodega: código de bodega Siesa (ej 'NB1'). Si None usa el default del gateway.
        """
        if connekta.modo_simulacion:
            return None  # en simulación no hay Siesa real
        try:
            response = connekta.get_inventario_fecha(producto_codigo_siesa, bodega=bodega)
            tabla = response.get('detalle', {}).get('Table', [])
            if not tabla:
                logger.warning(
                    f'[CONTEO] Siesa devolvió Table vacío para {producto_codigo_siesa} '
                    f'bodega={bodega} — no se puede obtener existencia fiscal.'
                )
                return None
            return float(tabla[0].get('f400_cant_existencia_1', 0))
        except Exception as e:
            logger.warning(f'[CONTEO] Error consultando Siesa para {producto_codigo_siesa}: {e}')
            return None

    @staticmethod
    def reconciliar_cantidad(sesion: SesionConteo, nueva_cantidad: int) -> dict:
        """
        Calcula diferencia y aplica transición MATCH si cuadra.
        Compara contra existencia_siesa (que ahora almacena stock WMS).

        Returns:
            {'es_match': bool, 'diferencia': float}
        """
        diferencia = nueva_cantidad - sesion.existencia_siesa
        sesion.diferencia = diferencia
        if diferencia == 0:
            sesion.estado = EstadoConteo.MATCH
            sesion.diferencia = 0
            sesion.fecha_cierre = datetime.utcnow()
            return {'es_match': True, 'diferencia': 0}
        return {'es_match': False, 'diferencia': diferencia}

    @staticmethod
    def _crear_conteo_verificacion(sesion_origen: SesionConteo, operario_excluido: int):
        """
        Crea CC2 o CC3 asignado a un operario diferente al excluido (double-blind).
        sesion_origen: CC1 para CC2, CC2 para CC3.
        """
        numero = 3 if (sesion_origen.es_segundo_conteo) else 2
        codigo = f'CC{numero}-{datetime.utcnow().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

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

        from app.models.usuario import Usuario
        from sqlalchemy import or_ as _or, and_ as _and

        # Determinar si el CC1 picker es un "picker par" que puede verificar en tienda.
        cc1_picker = Usuario.query.get(operario_excluido)
        _es_par = (
            cc1_picker is not None and (
                cc1_picker.rol == 'picker_traslado'
                or (cc1_picker.rol == 'operario' and bool(cc1_picker.puede_picar))
            )
        )

        otro_operario = None

        if _es_par and sesion_origen.almacen_id:
            from app.models.almacen import Almacen as _Alm
            _alm_sesion = _Alm.query.get(sesion_origen.almacen_id)
            _bodega_siesa = _alm_sesion.bodega_siesa_id if _alm_sesion else None

            # picker_traslado puede tener almacen_id=NULL y solo bodega_siesa_id='NS1'
            _filtros_almacen = [Usuario.almacen_id == sesion_origen.almacen_id]
            if _bodega_siesa:
                _filtros_almacen.append(Usuario.bodega_siesa_id == _bodega_siesa)
            _filtro_almacen = _or(*_filtros_almacen)

            # Pickers en conflicto: ya tienen tarea activa sobre el mismo producto+ubicación
            _ids_en_conflicto = (
                db.session.query(SesionConteo.operario_id)
                .filter(
                    SesionConteo.producto_id == sesion_origen.producto_id,
                    SesionConteo.ubicacion_id == sesion_origen.ubicacion_id,
                    SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO']),
                    SesionConteo.operario_id.isnot(None),
                )
            )
            otro_operario = (
                Usuario.query
                .filter(
                    Usuario.id != operario_excluido,
                    Usuario.activo == True,
                    _filtro_almacen,
                    _or(
                        Usuario.rol == 'picker_traslado',
                        _and(Usuario.rol == 'operario', Usuario.puede_picar == True),
                    ),
                    ~Usuario.id.in_(_ids_en_conflicto),
                )
                .first()
            )
            # Sin par disponible → queda sin asignar (PENDIENTE, panel admin lo escala)

        else:
            # Lógica existente para roles no-picker (jefe_almacen, admin, operario sin picar)
            _base = Usuario.query.filter(
                Usuario.id != operario_excluido,
                Usuario.activo == True,
            )
            if sesion_origen.almacen_id:
                otro_operario = _base.filter(
                    Usuario.almacen_id == sesion_origen.almacen_id,
                    Usuario.rol.in_(['operario', 'jefe_almacen', 'admin']),
                ).first()
            if not otro_operario:
                otro_operario = _base.filter(
                    Usuario.rol.in_(['jefe_almacen', 'admin']),
                ).first()

        if otro_operario:
            segundo.operario_id = otro_operario.id
            # Si el par ya tiene un CC1 activo sin contar aún, lo liberamos al pool
            # para que el CC2 sea su tarea inmediata en el siguiente get_tarea_actual.
            cc1_en_curso = SesionConteo.query.filter(
                SesionConteo.operario_id == otro_operario.id,
                SesionConteo.estado == EstadoConteo.EN_PROCESO,
                SesionConteo.es_segundo_conteo.is_(False),
                SesionConteo.cantidad_fisica.is_(None),
            ).first()
            if cc1_en_curso:
                cc1_en_curso.operario_id = None
                cc1_en_curso.estado = EstadoConteo.PENDIENTE
                cc1_en_curso.fecha_inicio = None
                logger.info(
                    '[CONTEO] CC1 %s liberado de picker %s para priorizar CC2 %s',
                    cc1_en_curso.codigo, otro_operario.id, segundo.codigo,
                )

        db.session.add(segundo)
        # No commit aquí — el caller (registrar_conteo) hace un único commit
        # que incluye tanto el estado SEGUNDO_CONTEO del padre como este hijo.
        return segundo

    @staticmethod
    def _encolar_ajuste_fisico(sesion: SesionConteo, aprobador_id: int = None) -> None:
        """
        SRP: única responsabilidad — consultar Siesa, calcular diferencia y encolar
        job AJUSTE_CONTEO en DLQ. No hace commit — el caller lo hace.
        Pre-condición: sesion.estado == DESCUADRE y sesion.cantidad_fisica is not None.
        """
        from app.models.almacen import Almacen
        from app.models.siesa_job import SiesaJob

        if not sesion.producto_codigo_siesa:
            raise ValueError(
                'Este producto no tiene código Siesa configurado — '
                'el ajuste de inventario no puede enviarse a Siesa.'
            )

        almacen = Almacen.query.get(sesion.almacen_id)
        bodega_siesa = almacen.bodega_siesa_id if almacen else None
        centro_op_siesa = almacen.centro_op_siesa if almacen else None

        existencia_siesa = ConteoService.consultar_existencia_siesa(
            producto_codigo_siesa=sesion.producto_codigo_siesa,
            bodega=bodega_siesa,
        )
        if existencia_siesa is not None:
            sesion.existencia_siesa = existencia_siesa
            logger.info(
                f'[CONTEO] Existencia fiscal Siesa para sesion {sesion.id}: '
                f'{existencia_siesa} (bodega={bodega_siesa}) — base del ajuste'
            )
        else:
            logger.warning(
                f'[CONTEO] Siesa no respondió para sesion {sesion.id} '
                f'— diferencia calculada contra WMS ({sesion.existencia_siesa}).'
            )

        diferencia = sesion.cantidad_fisica - sesion.existencia_siesa
        motivo_codigo = 'AJ-ENT' if diferencia > 0 else 'AJ-SAL'
        cantidad_ajuste = abs(diferencia)

        sesion.diferencia = diferencia
        sesion.motivo_codigo = motivo_codigo
        sesion.aprobador_id = aprobador_id
        sesion.idempotency_key = f'ADJ-{sesion.id}'
        sesion.estado = EstadoConteo.AJUSTANDO

        SiesaJob.encolar(
            'AJUSTE_CONTEO',
            {
                'sesion_id': sesion.id,
                'motivo_codigo': motivo_codigo,
                'item_codigo': sesion.producto_codigo_siesa,
                'cantidad': cantidad_ajuste,
                'referencia': sesion.codigo,
                'tarea_picking_id': sesion.tarea_picking_id,
                'ubicacion_id': sesion.ubicacion_id,
                'producto_id': sesion.producto_id,
                'bodega': bodega_siesa,
                'centro_op': centro_op_siesa,
            },
            referencia_tipo='SesionConteo',
            referencia_id=sesion.id,
            creado_por_id=aprobador_id,
        )
        logger.info(
            f'[CONTEO] Ajuste {motivo_codigo} encolado en DLQ '
            f'(aprobador={aprobador_id or "AUTO"}) — '
            f'{cantidad_ajuste} uds de {sesion.producto_codigo_siesa} — '
            f'bodega={bodega_siesa} — idempotency_key: ADJ-{sesion.id}'
        )

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
        Consulta existencia fiscal en Siesa en este momento (no durante el conteo).
        Resuelve bodega dinámicamente vía almacen.bodega_siesa_id.
        """
        from app.models.almacen import Almacen

        sesion = (SesionConteo.query
                  .filter_by(id=sesion_id)
                  .with_for_update()
                  .first())
        if not sesion:
            raise ValueError('Sesión no encontrada')

        # Idempotencia — si Siesa ya procesó este ajuste, devolver sin repetir
        if sesion.siesa_triggered:
            return sesion

        if sesion.estado == 'AJUSTANDO':
            from app.models.siesa_job import SiesaJob as _SJ
            job_activo = _SJ.query.filter_by(
                referencia_tipo='SesionConteo',
                referencia_id=sesion.id,
            ).filter(_SJ.estado.in_(['PENDIENTE', 'REINTENTANDO', 'PROCESANDO'])).first()
            if job_activo:
                return sesion  # En vuelo — la DLQ lo procesará

            job_completado = _SJ.query.filter_by(
                referencia_tipo='SesionConteo',
                referencia_id=sesion.id,
                estado='COMPLETADO',
            ).first()
            if job_completado:
                logger.warning(
                    f'[CONTEO] Sesión {sesion.id} stuck AJUSTANDO — job COMPLETADO encontrado → marcando AJUSTADO'
                )
                sesion.estado = EstadoConteo.AJUSTADO
                sesion.siesa_triggered = True
                sesion.fecha_cierre = sesion.fecha_cierre or datetime.utcnow()
                db.session.commit()
                return sesion

            # Re-encolar — crash antes de crear el job o job FALLIDO
            logger.error(
                f'[CONTEO] Sesión {sesion.id} stuck AJUSTANDO sin job DLQ — re-encolando'
            )
            diferencia_reenc = (sesion.cantidad_fisica or 0) - (sesion.existencia_siesa or 0)
            motivo_reenc = 'AJ-ENT' if diferencia_reenc > 0 else 'AJ-SAL'
            if not sesion.producto_codigo_siesa:
                raise ValueError(
                    f'Sesión {sesion.id} sin producto_codigo_siesa — '
                    'no se puede re-encolar ajuste a Siesa.'
                )
            # Resolver bodega del almacén
            _alm = Almacen.query.get(sesion.almacen_id)
            payload_reenc = {
                'sesion_id': sesion.id,
                'motivo_codigo': motivo_reenc,
                'item_codigo': sesion.producto_codigo_siesa,
                'cantidad': abs(diferencia_reenc),
                'referencia': sesion.codigo,
                'tarea_picking_id': sesion.tarea_picking_id,
                'ubicacion_id': sesion.ubicacion_id,
                'producto_id': sesion.producto_id,
                'bodega': _alm.bodega_siesa_id if _alm else None,
                'centro_op': _alm.centro_op_siesa if _alm else None,
            }
            from app.models.siesa_job import SiesaJob as _SJ2
            _SJ2.encolar('AJUSTE_CONTEO', payload_reenc,
                         referencia_tipo='SesionConteo', referencia_id=sesion.id,
                         creado_por_id=supervisor_id)
            db.session.commit()
            return sesion

        if sesion.estado != EstadoConteo.DESCUADRE:
            raise ValueError(f'No se puede ajustar en estado {sesion.estado} — debe estar en DESCUADRE')

        if sesion.cantidad_fisica is None:
            raise ValueError('Faltan datos del conteo para generar ajuste')

        ConteoService._encolar_ajuste_fisico(sesion, aprobador_id=supervisor_id)

        try:
            db.session.commit()
        except Exception as e_lock_release:
            db.session.rollback()
            raise ValueError(f'Error al registrar estado de conteo: {e_lock_release}') from e_lock_release

        logger.info(
            f'[SUPERVISOR_GUARD] Ajuste encolado en DLQ por usuario #{supervisor_id} '
            f'— sesion {sesion_id} — idempotency_key: ADJ-{sesion_id}'
        )

        return sesion

    @staticmethod
    def crear_conteo_manual(almacen_id: int, producto_codigo: str) -> dict:
        """
        Crea sesiones de conteo manual para todas las ubicaciones donde hay stock
        del producto en el almacén. Omite ubicaciones con conteo activo.
        Retorna dict con tareas_creadas, omitidas_ya_activas, producto_nombre, codigos.
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion

        codigo = producto_codigo.strip().upper()
        producto = Producto.query.filter(
            db.or_(Producto.codigo_siesa == codigo, Producto.codigo == codigo)
        ).first()
        if not producto:
            raise ValueError(f'Producto {codigo} no encontrado')

        registros = (
            UbicacionProducto.query
            .join(Ubicacion)
            .filter(
                UbicacionProducto.producto_id == producto.id,
                Ubicacion.almacen_id == almacen_id
            ).all()
        )
        if not registros:
            raise ValueError('El producto no tiene stock registrado en este almacén')

        # Pre-cargar sesiones activas en una sola query — evita N+1 en el loop
        ubicacion_ids = [r.ubicacion_id for r in registros]
        activos_set = {
            s.ubicacion_id
            for s in SesionConteo.query.filter(
                SesionConteo.producto_id == producto.id,
                SesionConteo.ubicacion_id.in_(ubicacion_ids),
                SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
            ).all()
        }

        creadas = []
        omitidas = 0
        hoy = datetime.utcnow().strftime('%Y%m%d')
        for reg in registros:
            if reg.ubicacion_id in activos_set:
                omitidas += 1
                continue
            sesion_codigo = f'CC-MANUAL-{hoy}-{str(uuid.uuid4())[:6].upper()}'
            sesion = SesionConteo(
                codigo=sesion_codigo,
                tipo='MANUAL',
                clasificacion_abc=producto.clasificacion_abc or 'C',
                ubicacion_id=reg.ubicacion_id,
                almacen_id=almacen_id,
                producto_id=producto.id,
                producto_codigo_siesa=producto.codigo_siesa,
                maneja_lote=False,
                estado='PENDIENTE'
            )
            db.session.add(sesion)
            creadas.append(sesion_codigo)

        try:
            db.session.commit()
        except Exception as e_commit:
            db.session.rollback()
            raise ValueError(f'Error al crear sesiones de conteo manual: {e_commit}') from e_commit

        return {
            'tareas_creadas': len(creadas),
            'omitidas_ya_activas': omitidas,
            'producto': codigo,
            'producto_nombre': producto.nombre or '',
            'codigos': creadas,
        }

    @staticmethod
    def liberar_tareas_zombi(timeout_horas: int = 2):
        """
        Libera tareas EN_PROCESO que llevan más de `timeout_horas` sin progreso.
        Devuelve la tarea a PENDIENTE sin operario para que otro la tome.
        """
        from datetime import timedelta
        umbral = datetime.utcnow() - timedelta(hours=timeout_horas)
        zombis = SesionConteo.query.filter(
            SesionConteo.estado == EstadoConteo.EN_PROCESO,
            SesionConteo.fecha_inicio < umbral,
        ).all()

        liberadas = 0
        for s in zombis:
            logger.warning(
                f'[CONTEO TIMEOUT] Sesion {s.codigo} (id={s.id}) EN_PROCESO '
                f'desde {s.fecha_inicio} — liberando (operario #{s.operario_id})'
            )
            s.estado = EstadoConteo.PENDIENTE
            s.operario_id = None
            s.fecha_inicio = None
            s.cantidad_fisica = None
            liberadas += 1

        if liberadas:
            db.session.commit()
            logger.info(f'[CONTEO TIMEOUT] {liberadas} tareas liberadas')
        return liberadas