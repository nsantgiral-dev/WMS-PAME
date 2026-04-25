"""
Servicio de Conteo Cíclico con Double-Blind Check.
Regla inquebrantable: el operario NUNCA ve la cantidad esperada.
La conciliación se hace en tiempo real contra Siesa.
"""
import uuid
import json
import logging
import threading
from datetime import datetime
from app.extensions import db
from app.models.conteo import SesionConteo
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

# Caché de existencias Siesa: evita llamadas HTTP duplicadas cuando varios
# operarios cuentan el mismo producto en la misma ventana de ~90s.
# El conteo cíclico no necesita exactitud al segundo — 90s es irrelevante operativamente.
_existencia_cache: dict = {}   # {(codigo, ubicacion, lote): (existencia, ts)}
_existencia_cache_lock = threading.Lock()
_CACHE_TTL_SEGUNDOS = 300  # 5 min — reduce llamadas HTTP en turno; pre-warm al generar sesiones


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
            sesion.estado = 'EN_PROCESO'
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
        2. Consulta existencia real en Siesa en tiempo real (FUERA del row-lock).
        3. Adquiere lock, re-valida estado y guarda.
        4. Decide: MATCH o SEGUNDO_CONTEO.

        La llamada HTTP a Siesa ocurre ANTES de with_for_update() para no mantener
        el row-lock de PostgreSQL durante los ~10s de timeout de red.
        """
        # Lectura previa sin lock — solo para obtener datos necesarios para Siesa
        sesion_pre = SesionConteo.query.filter_by(id=sesion_id).first()
        if not sesion_pre:
            raise ValueError('Sesión no encontrada')
        if sesion_pre.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'No se puede registrar conteo en estado {sesion_pre.estado}')
        if sesion_pre.operario_id and sesion_pre.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')
        if sesion_pre.maneja_lote and not lote_id:
            raise ValueError('Este producto maneja lotes. El campo lote_id es obligatorio.')

        # Consultar Siesa ANTES del lock — puede tardar hasta 10s (timeout de red)
        existencia_siesa = ConteoService._consultar_existencia_siesa(
            producto_codigo_siesa=sesion_pre.producto_codigo_siesa,
            ubicacion_codigo=sesion_pre.ubicacion.codigo if sesion_pre.ubicacion else None,
            lote_id=lote_id
        )

        # Ahora adquirir el lock pesimista para el update atómico
        sesion = (SesionConteo.query
                  .filter_by(id=sesion_id)
                  .with_for_update()
                  .first())
        if not sesion:
            raise ValueError('Sesión no encontrada')

        # Re-validar estado bajo lock (pudo cambiar mientras esperábamos Siesa)
        if sesion.estado not in ['PENDIENTE', 'EN_PROCESO']:
            raise ValueError(f'No se puede registrar conteo en estado {sesion.estado}')

        if sesion.operario_id and sesion.operario_id != operario_id:
            raise ValueError('Esta tarea no está asignada a ti')

        if existencia_siesa is None:
            # Cache miss: Siesa no respondió aún. El refresco de fondo ya está en vuelo.
            # Error reintentable — en 3-5s el caché tendrá el valor real.
            raise ValueError('Conectando con Siesa — reintenta en unos segundos')

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
                'mensaje': 'Conteo correcto — inventario cuadra con Siesa',
                'sesion_id': sesion.id
            }

        else:
            # DESCUADRE — generar segundo conteo por operario diferente
            sesion.estado = 'SEGUNDO_CONTEO'
            sesion.diferencia = diferencia

            # Commit único: SEGUNDO_CONTEO + hijo atómicos.
            # Si el proceso muere entre ambas escrituras, el padre queda en SEGUNDO_CONTEO
            # sin hijo, dejando al operario atascado. Con un solo commit, ambas son
            # todo-o-nada — Railway restart no puede dejar estado intermedio inválido.
            try:
                segundo_conteo = ConteoService._crear_segundo_conteo(
                    sesion_origen=sesion,
                    operario_excluido=operario_id
                )
                db.session.commit()
            except Exception as e_segundo:
                db.session.rollback()
                logger.error(f'[CONTEO] Error al crear segundo conteo para sesión {sesion_id}: {e_segundo}')
                raise ValueError(f'Error al crear segundo conteo: {e_segundo}')

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
    def prewarm_existencia_cache(sesiones: list):
        """
        Pre-calienta el caché de existencia Siesa para una lista de SesionConteo.
        Corre en un hilo daemon — no bloquea al caller.
        Llamar después de generar sesiones de conteo ABC para evitar HTTP sync en el turno.
        """
        from flask import current_app

        def _worker(items, app):
            # Thread daemon no hereda el app context de Flask — debe crearse explícitamente.
            # Sin esto, las queries de modo_simulacion fallan con RuntimeError (no app context).
            with app.app_context():
                for codigo_siesa, ub_codigo in items:
                    try:
                        ConteoService._consultar_existencia_siesa(
                            producto_codigo_siesa=codigo_siesa,
                            ubicacion_codigo=ub_codigo or '',
                        )
                    except Exception:
                        logger.warning(f'[PREWARM] Fallo pre-calentando caché para {codigo_siesa}/{ub_codigo}')

        pares = []
        for s in sesiones:
            if getattr(s, 'producto_codigo_siesa', None):
                ub = getattr(s, 'ubicacion', None)
                ub_codigo = ub.codigo if ub else ''
                pares.append((s.producto_codigo_siesa, ub_codigo))

        if pares:
            app = current_app._get_current_object()
            t = threading.Thread(target=_worker, args=(pares, app), daemon=True)
            t.start()
            logger.info(f'[CONTEO] Pre-warm existencia_siesa iniciado para {len(pares)} productos')

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
            # Simulación: devolver stock WMS local como si fuera Siesa
            return ConteoService._stock_local(producto_codigo_siesa, ubicacion_codigo)

        # Caché de 90s — reduce llamadas HTTP cuando varios operarios
        # cuentan el mismo producto en una ventana corta
        _cache_key = (producto_codigo_siesa, ubicacion_codigo, lote_id)
        with _existencia_cache_lock:
            _cached = _existencia_cache.get(_cache_key)
            if _cached:
                _val, _ts = _cached
                if (datetime.utcnow() - _ts).total_seconds() < _CACHE_TTL_SEGUNDOS:
                    logger.info(f'[CONTEO] existencia_siesa desde caché para {producto_codigo_siesa}')
                    return _val

        # Cache miss — disparar refresco de fondo y retornar None.
        # Retornar stock WMS local como fallback es INCORRECTO: si WMS y Siesa difieren
        # (que es el punto del conteo), el operador ve un diferencial falso.
        # El caller debe manejar None lanzando un error reintentable (~3s más tarde el
        # caché ya tiene el valor real de Siesa).
        ConteoService._refrescar_cache_en_background(producto_codigo_siesa, ubicacion_codigo, lote_id)
        return None

    @staticmethod
    def _stock_local(producto_codigo_siesa: str, ubicacion_codigo: str) -> float:
        """Retorna stock local WMS. Prueba codigo_siesa primero, luego codigo como fallback."""
        from app.models.inventario import UbicacionProducto
        from app.models.producto import Producto
        from app.models.ubicacion import Ubicacion
        producto = Producto.query.filter(
            Producto.codigo_siesa == producto_codigo_siesa
        ).first()
        if not producto:
            producto = Producto.query.filter_by(codigo=producto_codigo_siesa).first()
        if not producto:
            return 0
        ubicacion = Ubicacion.query.filter_by(codigo=ubicacion_codigo).first()
        if not ubicacion:
            return 0
        reg = UbicacionProducto.query.filter_by(
            producto_id=producto.id, ubicacion_id=ubicacion.id
        ).first()
        return float(reg.cantidad) if reg else 0

    @staticmethod
    def _refrescar_cache_en_background(producto_codigo_siesa: str, ubicacion_codigo: str, lote_id: str = None):
        """Dispara thread de fondo para actualizar cache de existencia Siesa (fire-and-forget)."""
        from flask import current_app
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            return  # fuera de contexto Flask, no hay nada que hacer

        _cache_key = (producto_codigo_siesa, ubicacion_codigo, lote_id)

        def _worker():
            with app.app_context():
                try:
                    response = connekta.get_inventario_fecha(producto_codigo_siesa)
                    tabla = response.get('detalle', {}).get('Table', [])
                    existencia = float(tabla[0].get('f400_cant_existencia_1', 0)) if tabla else 0.0
                    with _existencia_cache_lock:
                        _existencia_cache[_cache_key] = (existencia, datetime.utcnow())
                    logger.info(f'[CONTEO] Cache Siesa actualizado para {producto_codigo_siesa}: {existencia}')
                except Exception as e:
                    logger.warning(f'[CONTEO] Refresco cache background falló para {producto_codigo_siesa}: {e}')

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

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
        # No commit aquí — el caller (registrar_conteo) hace un único commit
        # que incluye tanto el estado SEGUNDO_CONTEO del padre como este hijo.
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
            # Verificar si el DLQ tiene un job activo para esta sesión
            from app.models.siesa_job import SiesaJob as _SJ
            job_activo = _SJ.query.filter_by(
                referencia_tipo='SesionConteo',
                referencia_id=sesion.id,
            ).filter(_SJ.estado.in_(['PENDIENTE', 'REINTENTANDO', 'PROCESANDO'])).first()
            if job_activo:
                # En vuelo — la DLQ lo procesará
                return sesion
            # Sin job activo: sesión stuck (crash post-commit). Recuperar marcando AJUSTADO.
            logger.warning(
                f'[CONTEO] Sesión {sesion.id} stuck AJUSTANDO sin job DLQ activo — recuperando'
            )
            sesion.estado = 'AJUSTADO'
            sesion.fecha_cierre = sesion.fecha_cierre or datetime.utcnow()
            if not sesion.siesa_response:
                sesion.siesa_response = '{"recuperado": true, "motivo": "stuck_ajustando_sin_job"}'
            db.session.commit()
            return sesion
        if sesion.estado not in ['SEGUNDO_CONTEO', 'DESCUADRE']:
            raise ValueError(f'No se puede ajustar en estado {sesion.estado}')

        if sesion.cantidad_fisica is None:
            raise ValueError('Faltan datos del conteo para generar ajuste')

        if sesion.existencia_siesa is None:
            # Sesión creada con cache miss — re-fetch desde Siesa (el caché ya debe estar caliente)
            existencia_refetch = ConteoService._consultar_existencia_siesa(
                producto_codigo_siesa=sesion.producto_codigo_siesa,
                ubicacion_codigo=sesion.ubicacion.codigo if sesion.ubicacion else None,
                lote_id=sesion.lote_id,
            )
            if existencia_refetch is None:
                raise ValueError('Siesa aún no respondió — reintenta el ajuste en unos segundos')
            sesion.existencia_siesa = existencia_refetch

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
        idem_key = f'ADJ-{sesion_id}'
        sesion.idempotency_key = idem_key
        sesion.aprobador_id = supervisor_id

        # CRÍTICO: marcar AJUSTANDO antes de liberar el lock.
        # Esto bloquea requests concurrentes (doble-tap del supervisor) que llegarían
        # tras el commit y verían siesa_triggered=False — sin este estado, ambas
        # requests pasarían el guard y llamarían a Siesa → doble ajuste de inventario.
        sesion.estado = 'AJUSTANDO'

        # Capturar referencias a atributos ANTES del commit
        # (expire_on_commit invalida el objeto tras el commit)
        producto_ref = sesion.producto
        item_codigo = sesion.producto_codigo_siesa or (producto_ref.codigo if producto_ref else '')
        sesion_codigo = sesion.codigo
        ubicacion_id = sesion.ubicacion_id
        producto_id = sesion.producto_id
        tarea_picking_id = sesion.tarea_picking_id
        producto_codigo_log = producto_ref.codigo if producto_ref else item_codigo

        # Payload para la DLQ — incluye todos los campos que el worker necesita
        # para llamar Siesa Y actualizar el estado local tras completar.
        payload_dlq = {
            'sesion_id': sesion_id,
            'motivo_codigo': motivo_codigo,
            'item_codigo': item_codigo,
            'cantidad': cantidad_ajuste,
            'referencia': sesion_codigo,
            'tarea_picking_id': tarea_picking_id,
            'ubicacion_id': ubicacion_id,
            'producto_id': producto_id,
        }

        # Commit único: AJUSTANDO + SiesaJob son atómicos.
        # Si Railway reinicia después de este commit, el job DLQ se procesa al volver.
        # Si reinicia antes de este commit, el estado vuelve a SEGUNDO_CONTEO (no hubo cambio).
        # En ambos casos no hay estado intermedio inválido ni Siesa se llama dos veces.
        from app.models.siesa_job import SiesaJob
        SiesaJob.encolar(
            'AJUSTE_CONTEO',
            payload_dlq,
            referencia_tipo='SesionConteo',
            referencia_id=sesion_id,
            creado_por_id=supervisor_id,
        )
        try:
            db.session.commit()
        except Exception as e_lock_release:
            db.session.rollback()
            raise ValueError(f'Error al registrar estado de conteo: {e_lock_release}') from e_lock_release

        logger.info(
            f'[SUPERVISOR_GUARD] Ajuste {motivo_codigo} encolado en DLQ por usuario #{supervisor_id} '
            f'— {cantidad_ajuste} unidades de {producto_codigo_log} '
            f'— idempotency_key: {idem_key}'
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