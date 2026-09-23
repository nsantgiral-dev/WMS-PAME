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
from app.utils.fecha import ahora_bogota as _ahora_bogota

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

        ConteoService.verificar_puede_tomar_definitivo(sesion, operario_id)

        # PENDIENTE → EN_PROCESO al abrirla — sin importar si YA tenía dueño.
        # `crear_conteo_manual(operario_id=...)` y `asignar-lote` pre-asignan
        # operario_id dejando estado=PENDIENTE ("asignada pero no iniciada",
        # el mismo patrón que ya usan las tareas DIARIO_ABC). Comprobar
        # `not sesion.operario_id` acá dejaba esas sesiones sin transición
        # nunca: el operario asignado la abría, la contaba, y la sesión
        # seguía viéndose PENDIENTE — fecha_inicio nunca se registraba.
        if sesion.estado == EstadoConteo.PENDIENTE:
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
    def _es_conteo_definitivo(sesion: SesionConteo) -> bool:
        """True si `sesion` es un CC3 — su padre (CC2) es a su vez es_segundo_conteo."""
        if not (sesion.es_segundo_conteo and sesion.sesion_origen_id):
            return False
        origen = db.session.get(SesionConteo, sesion.sesion_origen_id)
        return bool(origen and origen.es_segundo_conteo)

    @staticmethod
    def verificar_puede_tomar_definitivo(sesion: SesionConteo, operario_id: int) -> None:
        """El CC3 nace SIN operario a propósito (`_crear_conteo_verificacion`)
        — lo toma un supervisor desde `/api/conteo/definitivos`, nunca un
        picker automático (decisión 2026-09-04). Sin este chequeo, cualquier
        operario de almacén podía saltarse esa cola llamando directo a
        `GET /api/conteo/<id>/tarea` o a `/api/mobile/escanear` con el
        sesion_id del CC3 — la ruta que lista la cola ya exige
        `Roles.SUPERVISION`, pero nada exigía lo mismo al TOMARLO.

        No hace nada si la sesión ya tiene dueño (ese caso lo cubre el
        chequeo de ownership de cada caller) ni si no es un CC3.
        """
        if sesion.operario_id or not ConteoService._es_conteo_definitivo(sesion):
            return
        from app.models.usuario import Usuario
        from app.routes._auth_helpers import Roles
        operario = db.session.get(Usuario, operario_id)
        if not operario or operario.rol not in Roles.SUPERVISION:
            raise ValueError(
                'El conteo definitivo (CC3) solo lo puede tomar un supervisor, '
                'admin o jefe de almacén'
            )

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
        ConteoService.verificar_puede_tomar_definitivo(sesion_pre, operario_id)

        # Siesa es la fuente de verdad. Se consulta antes del lock para no
        # mantener la transacción abierta durante la llamada HTTP.
        _bodega_siesa = None
        if sesion_pre.almacen_id:
            from app.models.almacen import Almacen as _Alm
            _alm = _Alm.query.get(sesion_pre.almacen_id)
            _bodega_siesa = _alm.bodega_siesa_id if _alm else None

        # La foto se toma AHORA, en el instante del conteo, y queda guardada:
        # el ajuste que salga de esta sesión es un delta contra ESTA foto, no
        # contra una existencia leída cuando alguien apruebe.
        foto = None
        if sesion_pre.producto_codigo_siesa and _bodega_siesa:
            foto = ConteoService.consultar_foto_siesa(
                producto_codigo_siesa=sesion_pre.producto_codigo_siesa,
                bodega=_bodega_siesa,
            )

        # La procedencia se declara SIEMPRE, no solo cuando falla: un campo que
        # solo se escribe en el caso malo deja el caso bueno indistinguible del
        # histórico sin dato.
        _fuente_existencia = 'SIESA'
        existencia_wms = None
        if foto is None:
            # Fallback a WMS si Siesa no responde (o la foto vino incompleta).
            # Sirve para decidir MATCH / segundo conteo; NO para ajustar:
            # `motivo_bloqueo_ajuste` niega cualquier ajuste sin foto.
            reg_inv = UbicacionProducto.query.filter_by(
                ubicacion_id=sesion_pre.ubicacion_id,
                producto_id=sesion_pre.producto_id,
            ).first()
            existencia_wms = float(reg_inv.cantidad) if reg_inv else 0.0
            _fuente_existencia = 'WMS'
            logger.warning(
                f'[CONTEO] Siesa no dio foto completa para {sesion_pre.codigo} '
                f'— diferencia calculada contra WMS ({existencia_wms}); '
                f'esta sesión no podrá ajustar Siesa'
            )
        else:
            logger.info(
                f'[CONTEO] Foto Siesa para {sesion_pre.codigo} (bodega={_bodega_siesa}): '
                f'existencia={foto["existencia"]} pos={foto["cant_pos"]} '
                f'salida_sin_conf={foto["salida_sin_conf"]} → teórico={foto["teorico"]}'
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
        # Re-verificado bajo lock: sesion_pre y sesion son lecturas distintas —
        # el mismo criterio que ya aplican estado y ownership dos líneas arriba.
        ConteoService.verificar_puede_tomar_definitivo(sesion, operario_id)

        sesion.operario_id = operario_id
        sesion.cantidad_fisica = cantidad_fisica
        ConteoService._grabar_foto(sesion, foto)
        if foto is None:
            sesion.existencia_siesa = existencia_wms
        sesion.fuente_existencia = _fuente_existencia
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
                # Decía «cuadra con WMS» aunque hubiera comparado contra
                # Siesa: dos confusiones de procedencia en la misma función.
                # Quien lee esto necesita saber contra QUÉ cuadró.
                'mensaje': ('Conteo correcto — inventario cuadra con '
                            + ('Siesa' if _fuente_existencia == 'SIESA' else 'WMS')),
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
                        # CC3 es la verdad definitiva: su físico Y su foto.
                        ConteoService._copiar_observacion(raiz, sesion)
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
                        # id de CC1 (la raíz) — quien llama necesita este id para
                        # PUT /api/conteo/<raiz_id>/ajustar, no el de CC3 mismo.
                        'raiz_id': raiz.id if raiz else None,
                        # Se avisa ya, no cuando el supervisor apriete aprobar.
                        'ajuste_bloqueado': ConteoService.motivo_bloqueo_ajuste(raiz if raiz else sesion),
                    }

                # CC2 no cuadra con Siesa — comparar CC1 vs CC2
                cc1_cantidad = origen.cantidad_fisica if origen else None
                sesion.estado = EstadoConteo.DESCUADRE

                if origen is not None and ConteoService.conteos_coinciden(origen, sesion):
                    # CC1 == CC2 (en diferencia contra su foto): "verdad de
                    # bodega" — ajuste automático sin esperar admin.
                    debe_auto_encolar = bool(origen.estado == EstadoConteo.SEGUNDO_CONTEO)
                    if debe_auto_encolar:
                        origen.estado = EstadoConteo.DESCUADRE
                        # La raíz se queda con la observación que resolvió —
                        # la de CC2, la más reciente— para que el ajuste y su
                        # bloqueo se decidan sobre UNA foto.
                        ConteoService._copiar_observacion(origen, sesion)
                        origen.motivo_codigo = 'AJ-ENT' if diferencia > 0 else 'AJ-SAL'

                    try:
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        raise ValueError(f'Error al marcar DESCUADRE: {e}')

                    auto_encolado = False
                    error_auto_encolado = None
                    bloqueo = (ConteoService.motivo_bloqueo_ajuste(origen)
                               if debe_auto_encolar else None)
                    if bloqueo:
                        # No se intenta: no es un fallo del encolado, es una
                        # decisión. Queda en DESCUADRE con la foto grabada, y la
                        # aprobación manual la va a negar por la misma razón.
                        error_auto_encolado = bloqueo
                        logger.warning(
                            f'[CONTEO] CC1==CC2 en {origen.codigo} pero el ajuste '
                            f'NO se encola: {bloqueo}'
                        )
                    elif debe_auto_encolar:
                        try:
                            ConteoService._encolar_ajuste_fisico(origen, aprobador_id=None)
                            db.session.commit()
                            auto_encolado = True
                            logger.warning(
                                f'[CONTEO] CC2 confirma CC1 ({cc1_cantidad} uds) — '
                                f'padre {origen.codigo} → AJUSTANDO (auto)'
                            )
                        except Exception as e_enq:
                            db.session.rollback()
                            error_auto_encolado = str(e_enq)
                            logger.error(
                                f'[CONTEO] Error al auto-encolar ajuste CC1==CC2 '
                                f'para sesion {origen.id}: {e_enq} — queda en DESCUADRE para revisión admin'
                            )

                    # El mensaje refleja lo que de verdad pasó — antes decía
                    # "encolado automáticamente" incluso cuando la excepción se
                    # tragaba en el `except` de arriba y nada llegaba a la DLQ.
                    if auto_encolado:
                        mensaje = 'Ambos conteos coinciden — ajuste encolado automáticamente'
                    elif bloqueo:
                        mensaje = (
                            'Ambos conteos coinciden, pero el ajuste NO se envía: '
                            f'{bloqueo}'
                        )
                    elif debe_auto_encolar:
                        mensaje = (
                            'Ambos conteos coinciden — el ajuste automático falló '
                            f'({error_auto_encolado}); queda en DESCUADRE para aprobación manual'
                        )
                    else:
                        mensaje = 'Ambos conteos coinciden — queda en DESCUADRE para aprobación manual'

                    return {
                        'resultado': 'DESCUADRE',
                        'mensaje': mensaje,
                        'auto_encolado': auto_encolado,
                        'ajuste_bloqueado': bloqueo,
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
                    f'[CONTEO] CC1≠CC2 (físico {cc1_cantidad} vs {cantidad_fisica}; '
                    f'diferencia {origen.diferencia if origen else None} vs {diferencia}) — '
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
    def _fila_invfecha(producto_codigo_siesa: str, bodega: str = None):
        """La fila cruda de `API_v2_Inventarios_InvFecha` para ítem × bodega, o
        `None` si Siesa no la dio. **La única lectura HTTP del conteo**: la
        existencia suelta y la foto completa salen de acá, para que el sobre de
        rechazo y la tabla vacía se traten igual en las dos.
        """
        if connekta.modo_simulacion:
            return None  # en simulación no hay Siesa real
        try:
            response = connekta.get_inventario_fecha(producto_codigo_siesa, bodega=bodega)
            tabla = response.get('detalle', {}).get('Table', [])
            # El sobre de rechazo de Connekta —HTTP 200, una fila, una clave
            # `alerta`— NO es una tabla vacía: pasa el `if not tabla` de abajo
            # y `tabla[0].get('f400_cant_existencia_1', 0)` devuelve **0.0**.
            # Y `0.0 is not None`, así que el llamador lo trata como dato
            # bueno: `_encolar_ajuste_fisico` graba `existencia_siesa = 0`,
            # marca `fuente_existencia='SIESA'` —afirmando que se verificó
            # contra el ERP— y encola un AJ-ENT por TODO el conteo físico.
            # El stock fiscal queda en `real + físico`, y un ajuste no lo
            # reclama nadie: reaparece meses después.
            from app.services.connekta_gateway import _alerta_de
            alerta = _alerta_de(tabla)
            if alerta:
                logger.error(
                    '[CONTEO] Siesa RECHAZÓ la consulta de existencia para %s '
                    'bodega=%s: %s. No se devuelve 0 — no saber la existencia '
                    'no es saber que es cero.',
                    producto_codigo_siesa, bodega, alerta)
                return None
            if not tabla:
                logger.warning(
                    f'[CONTEO] Siesa devolvió Table vacío para {producto_codigo_siesa} '
                    f'bodega={bodega} — no se puede obtener existencia fiscal.'
                )
                return None
            return tabla[0]
        except Exception as e:
            logger.warning(f'[CONTEO] Error consultando Siesa para {producto_codigo_siesa}: {e}')
            return None

    @staticmethod
    def consultar_existencia_siesa(producto_codigo_siesa: str, bodega: str = None):
        """
        `f400_cant_existencia_1` sola, como float, o None si Siesa no responde.

        **No es la base de un conteo** (desde 2026-09-23): en una tienda incluye
        la venta POS que Siesa todavía no acumuló. Para comparar un conteo se
        usa `consultar_foto_siesa`. Esta queda para quien solo quiere MIRAR la
        existencia —los scripts de prueba real, el prewarm del turno— y no
        decide ningún ajuste (ver `tests/test_conteo_teorico_pos.py`, el
        inventario de lecturas).
        bodega: código de bodega Siesa (ej 'NB1'). Si None usa el default del gateway.
        """
        fila = ConteoService._fila_invfecha(producto_codigo_siesa, bodega)
        if fila is None:
            return None
        # El campo tiene que venir. Si falta, la fila no es la que
        # creemos —otra API, otro alias— y un default de 0 sería la misma
        # mentira por otra puerta.
        crudo = fila.get('f400_cant_existencia_1')
        if crudo is None:
            logger.error(
                '[CONTEO] la fila de existencia de %s bodega=%s no trae '
                'f400_cant_existencia_1 (claves: %s). No se asume cero.',
                producto_codigo_siesa, bodega, list(fila.keys()))
            return None
        try:
            return float(crudo)
        except (TypeError, ValueError):
            logger.error('[CONTEO] f400_cant_existencia_1 ilegible para %s bodega=%s: %r',
                         producto_codigo_siesa, bodega, crudo)
            return None

    #: Los tres campos sin los cuales la foto no sirve. `f400_cant_comprometida_1`
    #: NO está: lo comprometido sigue físicamente en el estante, así que no
    #: entra al teórico, y exigirlo bloquearía conteos por un dato que no se usa.
    CAMPOS_FOTO = {
        'existencia': 'f400_cant_existencia_1',
        'cant_pos': 'f400_cant_pos_1',
        'salida_sin_conf': 'f400_cant_salida_sin_conf_1',
    }

    @staticmethod
    def teorico(existencia, cant_pos):
        """**Lo que debería haber en el estante según Siesa**: `existencia − cant_pos`.

        La única implementación de la fórmula (Regla 0, corolario: una política,
        una función).

        `f400_cant_pos_1` es venta de caja que Siesa aún no acumuló: la
        mercancía ya salió, pero la existencia todavía la cuenta. Comparar el
        físico contra la existencia cruda fabrica un faltante igual al POS; si
        se ajusta, la acumulación del POS lo vuelve a descontar al día
        siguiente. Doble descuento.

        **No se resta la comprometida.** Un pedido comprometido sigue en el
        estante hasta que alguien lo saca: el contador lo ve y lo cuenta.
        """
        return existencia - cant_pos

    @staticmethod
    def consultar_foto_siesa(producto_codigo_siesa: str, bodega: str = None):
        """
        La foto de Siesa contra la que se mide un conteo, tomada en el instante
        de contar::

            {existencia, cant_pos, salida_sin_conf, comprometida, teorico, leido_at}

        o **None** si Siesa no responde o la fila no trae alguno de los tres
        campos de `CAMPOS_FOTO`.

        Campo ausente ≠ 0 (Regla 0). Un `f400_cant_pos_1` que no vino no es
        «no hay POS pendiente»: es «no sé cuánto hay». Tomarlo como 0 reabre
        exactamente el doble descuento que esta foto existe para cerrar, y en
        la tienda donde más duele. Media foto se trata como ninguna.

        `comprometida` viaja solo como contexto —puede venir `None`—; no entra
        a ninguna cuenta.
        """
        fila = ConteoService._fila_invfecha(producto_codigo_siesa, bodega)
        if fila is None:
            return None
        valores = {}
        for clave, campo in ConteoService.CAMPOS_FOTO.items():
            crudo = fila.get(campo)
            if crudo is None:
                logger.error(
                    '[CONTEO] foto INCOMPLETA de %s bodega=%s: falta %s '
                    '(claves: %s). No se asume cero: no se compara contra '
                    'una base a medias.',
                    producto_codigo_siesa, bodega, campo, list(fila.keys()))
                return None
            try:
                valores[clave] = float(crudo)
            except (TypeError, ValueError):
                logger.error('[CONTEO] %s ilegible para %s bodega=%s: %r',
                             campo, producto_codigo_siesa, bodega, crudo)
                return None
        comprometida = fila.get('f400_cant_comprometida_1')
        try:
            comprometida = float(comprometida) if comprometida is not None else None
        except (TypeError, ValueError):
            comprometida = None
        return {
            **valores,
            'comprometida': comprometida,
            'teorico': ConteoService.teorico(valores['existencia'], valores['cant_pos']),
            'leido_at': datetime.utcnow(),
        }

    @staticmethod
    def _grabar_foto(sesion: SesionConteo, foto) -> None:
        """Escribe la foto completa en la sesión, o la deja vacía entera.

        Nunca a medias: una sesión con `teorico_siesa` de un instante y
        `salida_sin_conf_siesa` de otro sería una base que no midió nadie.
        `existencia_siesa` y `fuente_existencia` NO se tocan con foto `None`:
        las escribe el fallback al WMS, que declara su propia procedencia.
        """
        if foto is None:
            sesion.cant_pos_siesa = None
            sesion.salida_sin_conf_siesa = None
            sesion.teorico_siesa = None
            sesion.foto_siesa_at = None
            return
        sesion.existencia_siesa = foto['existencia']
        sesion.cant_pos_siesa = foto['cant_pos']
        sesion.salida_sin_conf_siesa = foto['salida_sin_conf']
        sesion.teorico_siesa = foto['teorico']
        sesion.foto_siesa_at = foto['leido_at']

    @staticmethod
    def _copiar_observacion(destino: SesionConteo, origen: SesionConteo) -> None:
        """El conteo que RESOLVIÓ (el CC2 que confirma a CC1, o el CC3) se
        copia a la raíz entero: físico, foto, procedencia y diferencia.

        Entero porque el ajuste se calcula SOLO con lo que tiene la raíz
        (`cantidad_fisica − teorico_siesa`) y porque el bloqueo por salidas no
        POS se decide sobre esa misma foto. Copiar el físico sin la foto —o la
        foto sin el físico— produce un delta entre dos instantes, que es el
        defecto que esto cierra. El conteo propio de CC1 no se pierde del
        todo: cuando CC1 y CC2 coinciden, coinciden en diferencia, que es lo
        que viaja a Siesa.
        """
        destino.cantidad_fisica = origen.cantidad_fisica
        destino.existencia_siesa = origen.existencia_siesa
        destino.fuente_existencia = origen.fuente_existencia
        destino.cant_pos_siesa = origen.cant_pos_siesa
        destino.salida_sin_conf_siesa = origen.salida_sin_conf_siesa
        destino.teorico_siesa = origen.teorico_siesa
        destino.foto_siesa_at = origen.foto_siesa_at
        destino.diferencia = origen.diferencia

    @staticmethod
    def base_de_comparacion(sesion: SesionConteo):
        """Contra qué se compara el físico de esta sesión.

        El teórico si hay foto de Siesa; si no, `existencia_siesa`, que en ese
        caso es el stock del WMS (`fuente_existencia='WMS'`) o el histórico
        anterior a la foto. Sirve para decidir MATCH / segundo conteo; **no**
        para ajustar: sin foto no se ajusta (`motivo_bloqueo_ajuste`).
        """
        if sesion.teorico_siesa is not None:
            return sesion.teorico_siesa
        return sesion.existencia_siesa

    @staticmethod
    def conteos_coinciden(cc1: SesionConteo, cc2: SesionConteo) -> bool:
        """¿CC2 confirma a CC1? Por **diferencia contra su propia foto**, no por
        cantidad física.

        Entre CC1 y CC2 la tienda sigue vendiendo. Ejemplo: CC1 cuenta 7 con
        Siesa en existencia 10 / POS 2 (teórico 8, diferencia −1). Se vende 1
        por caja. CC2 cuenta 6 con existencia 10 / POS 3 (teórico 7,
        diferencia −1). Comparar físicos (7 ≠ 6) manda a un tercer conteo por
        una venta que las dos cuentas registraron bien; comparar diferencias
        (−1 == −1) dice lo que pasó: los dos vieron el mismo faltante de 1.

        Solo se comparan diferencias si **las dos** tienen foto de Siesa: una
        diferencia contra el stock del WMS y otra contra el teórico de Siesa
        miden cosas distintas. Sin las dos fotos se compara el físico, como
        antes — y ese ajuste igual queda bloqueado por falta de foto.
        """
        if cc1.teorico_siesa is not None and cc2.teorico_siesa is not None:
            return (cc1.diferencia is not None
                    and cc1.diferencia == cc2.diferencia)
        return (cc1.cantidad_fisica is not None
                and cc1.cantidad_fisica == cc2.cantidad_fisica)

    @staticmethod
    def motivo_bloqueo_ajuste(sesion: SesionConteo):
        """Por qué el ajuste de esta sesión NO puede salir a Siesa, o `None`.

        La usan el auto-ajuste de CC1 == CC2, la aprobación del supervisor, la
        auditoría de picking y la pantalla (`to_dict`). Una regla, una función:
        si la pantalla la reimplementara, un día diría «aprobable» sobre algo
        que el servicio niega.

        Dos casos, los dos Regla 0:

        1. **Sin foto de Siesa.** El delta se mide contra la foto del conteo;
           sin ella solo queda la base del WMS, y un delta sobre esa base deja
           a Siesa en `siesa_real + (fisico − wms)`: peor que antes, justo
           cuando las bases discrepan. Un ajuste de inventario siempre puede
           esperar.
        2. **Salidas sin confirmar que no son POS.** El POS pendiente vive
           dentro de la salida sin confirmar (600/600 filas con POS medidas en
           Siesa QA, 2026-09-23). Si `salida_sin_conf ≠ cant_pos` sobra una
           salida que no es de caja —una remisión, un traslado sin confirmar—
           y no se sabe si esa mercancía ya dejó el estante. Si ya salió, el
           conteo sale corto en esa cantidad y Siesa la descuenta otra vez al
           confirmarla: el mismo doble descuento por otra puerta.
        """
        if (sesion.fuente_existencia != 'SIESA' or sesion.teorico_siesa is None
                or sesion.cant_pos_siesa is None
                or sesion.salida_sin_conf_siesa is None):
            return (
                'El conteo no tiene foto de Siesa (existencia y POS pendiente '
                'leídos al contar). El ajuste NO se aprueba contra el stock del '
                'WMS ni contra una existencia leída en otro momento: saldría '
                'como delta sobre una base equivocada. Recontar con Siesa '
                'respondiendo — un ajuste de inventario siempre puede esperar.'
            )
        if sesion.salida_sin_conf_siesa != sesion.cant_pos_siesa:
            no_pos = sesion.salida_sin_conf_siesa - sesion.cant_pos_siesa
            return (
                f'Siesa tenía {no_pos:g} und en salidas sin confirmar que NO '
                f'son venta POS (salida sin confirmar '
                f'{sesion.salida_sin_conf_siesa:g}, POS {sesion.cant_pos_siesa:g}). '
                f'No se sabe si esa mercancía ya salió del estante, así que el '
                f'conteo no se puede convertir en ajuste. Recontar cuando esas '
                f'salidas estén confirmadas en Siesa.'
            )
        return None

    @staticmethod
    def reconciliar_cantidad(sesion: SesionConteo, nueva_cantidad: int) -> dict:
        """
        Calcula diferencia y aplica transición MATCH si cuadra.
        Compara contra `base_de_comparacion`: el teórico de la foto de Siesa
        (`existencia − POS pendiente`) o, sin foto, el stock del WMS.

        Returns:
            {'es_match': bool, 'diferencia': float}
        """
        diferencia = nueva_cantidad - ConteoService.base_de_comparacion(sesion)
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
        Crea CC2 (asignado a otro operario, double-blind) o CC3 (sin asignar —
        ver más abajo). sesion_origen: CC1 para CC2, CC2 para CC3.
        """
        numero = 3 if (sesion_origen.es_segundo_conteo) else 2
        codigo = f'CC{numero}-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

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

        # CC3 (el "conteo definitivo"): NO se empareja con otro picker. Antes
        # esta función corría exactamente la misma búsqueda para CC2 y CC3, y
        # el único filtro era `!= operario_excluido` (el de CC2) — nada
        # impedía que le tocara de vuelta al mismo operario que hizo CC1 en un
        # equipo chico, rompiendo el doble-ciego justo en el conteo que
        # DEFINE el ajuste. Ahora CC3 nace sin operario_id y aparece en
        # `/api/conteo/definitivos` — decisión 2026-09-04: lo hace un
        # supervisor desde la pestaña "Conteo Definitivo", nunca un picker
        # automático.
        if numero == 3:
            db.session.add(segundo)
            logger.warning(
                '[CONTEO] CC3 %s creado SIN ASIGNAR — espera en la cola de '
                'Conteo Definitivo (solo supervisor/admin/jefe_almacén)',
                segundo.codigo,
            )
            return segundo

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
    def listar_definitivos(almacen_id: int = None) -> list:
        """
        Cola de "Conteo Definitivo" (CC3): sesiones donde CC1 y CC2 NO
        coincidieron y esperan el conteo que rompe el empate. Nace sin
        operario asignado (ver `_crear_conteo_verificacion`) — un
        supervisor la toma desde acá, nunca un picker automático.

        Vista ciega (`to_dict_operario`): quien va a hacer el conteo
        definitivo tampoco puede ver cuánto contaron CC1 y CC2 — mostrarle
        esos números rompería el double-blind justo en el conteo que
        DEFINE el ajuste, exactamente el caso que más lo necesita.
        """
        from sqlalchemy.orm import aliased
        origen = aliased(SesionConteo)
        query = (
            SesionConteo.query
            .join(origen, SesionConteo.sesion_origen_id == origen.id)
            .filter(
                SesionConteo.es_segundo_conteo.is_(True),
                origen.es_segundo_conteo.is_(True),
                SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO']),
            )
        )
        if almacen_id:
            query = query.filter(SesionConteo.almacen_id == almacen_id)
        return [
            {
                **s.to_dict_operario(),
                'almacen_id': s.almacen_id,
                'almacen_nombre': s.almacen.nombre if s.almacen else None,
                'operario_id': s.operario_id,
                'operario_nombre': s.operario.nombre if s.operario else None,
                'fecha_creacion': s.fecha_creacion.isoformat() if s.fecha_creacion else None,
            }
            for s in query.order_by(SesionConteo.fecha_creacion).all()
        ]

    @staticmethod
    def _encolar_ajuste_fisico(sesion: SesionConteo, aprobador_id: int = None) -> None:
        """
        SRP: única responsabilidad — calcular el delta con la foto de Siesa que
        la sesión guardó AL CONTAR (`cantidad_fisica − teorico_siesa`) y encolar
        el job AJUSTE_CONTEO en DLQ. **No consulta Siesa**: una lectura acá
        mezclaría el instante de la aprobación con el del conteo.
        No hace commit — el caller lo hace.
        Pre-condición: sesion.estado == DESCUADRE y sesion.cantidad_fisica is not None.
        """
        from app.models.almacen import Almacen
        from app.models.siesa_job import SiesaJob, EstadoSiesaJob

        # Guard idempotencia: no crear job duplicado si ya hay uno activo para esta sesión
        job_activo = SiesaJob.query.filter(
            SiesaJob.referencia_tipo == 'SesionConteo',
            SiesaJob.referencia_id == sesion.id,
            SiesaJob.tipo == 'AJUSTE_CONTEO',
            SiesaJob.estado.in_([EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.PROCESANDO]),
        ).first()
        if job_activo:
            logger.warning(
                '[CONTEO] Job AJUSTE_CONTEO ya existe (id=%s, estado=%s) para sesion %s — skip',
                job_activo.id, job_activo.estado, sesion.id,
            )
            return

        if not sesion.producto_codigo_siesa:
            raise ValueError(
                'Este producto no tiene código Siesa configurado — '
                'el ajuste de inventario no puede enviarse a Siesa.'
            )

        almacen = Almacen.query.get(sesion.almacen_id)
        bodega_siesa = almacen.bodega_siesa_id if almacen else None
        centro_op_siesa = almacen.centro_op_siesa if almacen else None
        if not bodega_siesa:
            raise ValueError(
                f'Almacén {sesion.almacen_id} sin bodega_siesa_id — '
                f'configurar en /api/almacenes antes de aprobar ajustes'
            )
        if not centro_op_siesa:
            raise ValueError(
                f'Almacén {sesion.almacen_id} sin centro_op_siesa — '
                f'configurar en /api/almacenes antes de aprobar ajustes'
            )

        # **El delta se fijó al contar.** Hasta el 2026-09-23 esta función
        # volvía a leer la existencia de Siesa al aprobar y recalculaba
        # `fisica − existencia_de_ahora`: dos instantes en una resta. Cualquier
        # movimiento entre el conteo y la aprobación se colaba en el ajuste —
        # conteo 95 contra 100; sale una remisión de 10 → 90; al aprobar
        # 95 − 90 = +5 AJ-ENT, cuando lo contado era un faltante de 5—. El
        # movimiento de después ya está en los dos lados (salió del estante y
        # salió de Siesa), así que el faltante medido al contar sigue siendo
        # exactamente el mismo: se manda ése.
        #
        # **No se ajusta a ciegas.** Decisión de Operaciones (2026-08-15): sin
        # foto de Siesa solo queda la base del WMS, y un delta sobre esa base
        # deja a Siesa en `siesa_real + (fisica − wms)` — peor, justo cuando las
        # dos bases discrepan. El ajuste de inventario es la única transacción
        # de esta operación que SIEMPRE puede esperar: no detiene una venta, ni
        # un despacho, ni un recaudo.
        bloqueo = ConteoService.motivo_bloqueo_ajuste(sesion)
        if bloqueo:
            raise ValueError(
                f'Ajuste de {sesion.producto_codigo_siesa} en {bodega_siesa} NO '
                f'aprobado. {bloqueo}'
            )

        diferencia = sesion.cantidad_fisica - sesion.teorico_siesa
        if diferencia == 0:
            raise ValueError(
                f'El conteo de {sesion.producto_codigo_siesa} cuadra con el '
                f'teórico de Siesa de su foto ({sesion.teorico_siesa:g}) — no hay '
                f'ajuste que mandar.'
            )
        logger.info(
            f'[CONTEO] Ajuste de sesion {sesion.id} con la foto del conteo '
            f'({sesion.foto_siesa_at}): físico {sesion.cantidad_fisica} − teórico '
            f'{sesion.teorico_siesa} (existencia {sesion.existencia_siesa} − POS '
            f'{sesion.cant_pos_siesa}) = {diferencia}'
        )
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
    def ajustar_desde_auditoria_picking(tarea, *, cantidad_fisica: int,
                                         aprobador_id: int) -> SesionConteo:
        """
        Ajuste a Siesa de un SKU puntual — la MISMA política que el conteo
        cíclico normal (`_encolar_ajuste_fisico`, conector 142951), pero sin
        double-blind: la llama `PickingService.auditar_tarea` cuando el
        resultado es ENCONTRADO_COMPLETO o ENCONTRADO_PARCIAL, y en esos dos
        casos quien resuelve YA es la autoridad que hizo un conteo físico
        propio — el mismo rol que decide un CC3 (Conteo Definitivo), no un
        picker que hay que verificar.

        No se reinventa el envío: se construye una `SesionConteo` (tipo
        `EXCEPCION_PICKING`, el mismo que usa `generar_auditoria_por_excepcion`
        cuando un picker reporta faltante sin bloquear) y se delega en
        `_encolar_ajuste_fisico`, el único código que arma el job
        AJUSTE_CONTEO — Regla 0, una política un solo sitio.

        Regla 0 — "ante dato ausente, declararlo": si Siesa no responde la
        existencia, levanta ValueError SIN persistir nada — el caller
        (`auditar_tarea`) no ha comiteado todavía, así que la auditoría entera
        se puede reintentar cuando Siesa responda, en vez de quedar resuelta
        localmente con el ajuste a Siesa perdido y sin rastro.
        """
        from app.models.almacen import Almacen

        producto = tarea.producto
        if not producto or not producto.codigo_siesa:
            raise ValueError(
                'Este producto no tiene código Siesa configurado — el ajuste '
                'de esta auditoría no puede enviarse a Siesa.'
            )

        almacen = Almacen.query.get(tarea.almacen_id)
        bodega_siesa = almacen.bodega_siesa_id if almacen else None
        if not bodega_siesa:
            raise ValueError(
                f'Almacén {tarea.almacen_id} sin bodega Siesa configurada — '
                'configúrala en /api/almacenes antes de auditar.'
            )

        # La auditoría ES el conteo: la foto se toma ahora, con la misma
        # función y la misma fórmula que el conteo cíclico. Comparar contra la
        # existencia cruda acá reabriría el doble descuento del POS por la
        # puerta de la auditoría.
        foto = ConteoService.consultar_foto_siesa(
            producto_codigo_siesa=producto.codigo_siesa, bodega=bodega_siesa)
        if foto is None:
            raise ValueError(
                f'Siesa no respondió la existencia de {producto.codigo_siesa} '
                f'en {bodega_siesa} (o la respondió sin el POS pendiente) — el '
                f'ajuste de esta auditoría no se manda a ciegas contra el WMS. '
                f'Reintenta la auditoría cuando Siesa responda.'
            )

        diferencia = cantidad_fisica - foto['teorico']
        codigo = f'AUD-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'
        sesion = SesionConteo(
            codigo=codigo,
            tipo='EXCEPCION_PICKING',
            ubicacion_id=tarea.ubicacion_id,
            almacen_id=tarea.almacen_id,
            producto_id=tarea.producto_id,
            producto_codigo_siesa=producto.codigo_siesa,
            maneja_lote=False,
            tarea_picking_id=tarea.id,
            cantidad_fisica=cantidad_fisica,
            fuente_existencia='SIESA',
            diferencia=diferencia,
            aprobador_id=aprobador_id,
            fecha_inicio=datetime.utcnow(),
        )
        ConteoService._grabar_foto(sesion, foto)

        if diferencia == 0:
            # Cuadra con Siesa — sin ajuste que mandar, igual que un MATCH de
            # conteo cíclico normal (no se encola nada en 0).
            sesion.estado = EstadoConteo.MATCH
            sesion.fecha_cierre = datetime.utcnow()
            db.session.add(sesion)
            logger.info(
                f'[CONTEO] Auditoría de picking tarea={tarea.id} — '
                f'{producto.codigo_siesa} cuadra con el teórico de Siesa '
                f'({foto["teorico"]}) — sin ajuste'
            )
            return sesion

        sesion.estado = EstadoConteo.DESCUADRE
        sesion.motivo_codigo = 'AJ-ENT' if diferencia > 0 else 'AJ-SAL'
        db.session.add(sesion)
        db.session.flush()  # necesita sesion.id antes de encolar el job

        bloqueo = ConteoService.motivo_bloqueo_ajuste(sesion)
        if bloqueo:
            # Salidas sin confirmar que no son POS: no se sabe si esa
            # mercancía ya salió, así que el ajuste no sale. Pero la auditoría
            # SÍ se cierra — a diferencia de «Siesa no responde», esto puede
            # durar días, y trabar la tarea de picking todo ese tiempo castiga
            # la operación por un problema del ERP. La sesión queda en
            # DESCUADRE con la foto y el motivo, visible y sin aprobar.
            logger.warning(
                f'[CONTEO] Auditoría de picking tarea={tarea.id} — '
                f'{producto.codigo_siesa}: ajuste de {diferencia} NO encolado. '
                f'{bloqueo} Sesión {sesion.codigo} queda en DESCUADRE.'
            )
            return sesion
        ConteoService._encolar_ajuste_fisico(sesion, aprobador_id=aprobador_id)
        logger.warning(
            f'[CONTEO] Auditoría de picking tarea={tarea.id} — '
            f'{producto.codigo_siesa} ajuste {sesion.motivo_codigo} '
            f'{abs(sesion.diferencia)} uds — sesión {sesion.codigo}'
        )
        return sesion

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

        # Evitar duplicados: si ya existe un conteo activo para este (producto, ubicacion)
        # no crear otro — la excepción ya fue reportada.
        existente = SesionConteo.query.filter(
            SesionConteo.producto_id == producto_id,
            SesionConteo.ubicacion_id == ubicacion_id,
            SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO']),
            SesionConteo.es_segundo_conteo.is_(False),
        ).first()
        if existente:
            logger.info(
                '[SUPERVISOR_GUARD] Auditoría omitida — ya existe %s para producto %s ubicación %s',
                existente.codigo, producto_id, ubicacion_id,
            )
            return existente

        codigo = f'AUD-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

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
            # El delta que se fijó al encolar la primera vez — `diferencia` —,
            # no una resta nueva: recalcular contra `existencia_siesa` cruda
            # reabría el doble descuento del POS justo en la recuperación.
            if sesion.diferencia is None:
                raise ValueError(
                    f'Sesión {sesion.id} AJUSTANDO sin diferencia registrada — '
                    'no se re-encola un ajuste sin el delta que se aprobó.'
                )
            diferencia_reenc = sesion.diferencia
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
    def crear_conteo_manual(almacen_id: int, producto_codigo: str, operario_id: int = None) -> dict:
        """
        Crea sesiones de conteo manual para todas las ubicaciones donde hay stock
        del producto en el almacén.

        Si una ubicación ya tiene una sesión PENDIENTE (creada por el barrido
        DIARIO_ABC u otro conteo manual, pero nadie la ha abierto todavía), la
        reclama en vez de omitirla: le reasigna el operario forzado y la cuenta
        como parte del resultado. Solo se omite (`omitidas_ya_activas`) una
        ubicación cuyo conteo YA está siendo contado de verdad —
        EN_PROCESO o SEGUNDO_CONTEO — porque ahí sí hay trabajo físico en
        marcha que no se puede pisar a ciegas.

        operario_id (opcional): fuerza el CC1 a ese operario específico —
        mismo estado PENDIENTE-pero-asignado que ya usan las tareas DIARIO_ABC
        (`obtener_tarea_operario` lo pasa a EN_PROCESO cuando el operario abre
        la tarea). Sin esto, la sesión queda sin dueño para el dispatcher
        automático (`_next_conteo_tienda`), que la reparte a quien la pida
        primero. El CC2, si CC1 sale discordante, lo sigue eligiendo
        `_crear_conteo_verificacion` — este parámetro solo controla CC1.

        Si el operario forzado ya tiene OTRO conteo cíclico EN_PROCESO (uno
        de un SKU distinto), ese se pausa — mismo patrón que
        `liberar_tareas_zombi`: vuelve a PENDIENTE, sin dueño, con
        `cantidad_fisica`/`fecha_inicio` en None — para que el conteo forzado
        sea lo próximo que el dispensador (`get_tarea_actual`) le entregue, en
        vez de seguir devolviéndole el que ya tenía en curso. Nunca se pausa
        un picking, packing o traslado activo — solo otro conteo: interrumpir
        una operación física en curso (bultos ya escaneados, LPN abierto) es
        un riesgo distinto, y ese conteo simplemente espera su turno en la
        cola normal del operario.

        Retorna dict con tareas_creadas, tareas_reclamadas, omitidas_ya_activas,
        producto_nombre, codigos.
        """
        from app.models.producto import Producto
        from app.models.inventario import UbicacionProducto
        from app.models.ubicacion import Ubicacion
        from app.models.usuario import Usuario

        codigo = producto_codigo.strip().upper()
        producto = Producto.query.filter(
            db.or_(
                Producto.codigo_siesa == codigo,
                Producto.codigo == codigo,
                db.func.upper(Producto.codigo_barras) == codigo,
                db.func.upper(Producto.codigo_barras_empaque) == codigo,
            )
        ).first()
        if not producto:
            raise ValueError(f'Producto {codigo} no encontrado')

        operario_forzado = None
        if operario_id is not None:
            operario_forzado = db.session.get(Usuario, operario_id)
            if not operario_forzado or not operario_forzado.activo:
                raise ValueError(f'Operario {operario_id} no encontrado o inactivo')

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
        activas_por_ubicacion = {
            s.ubicacion_id: s
            for s in SesionConteo.query.filter(
                SesionConteo.producto_id == producto.id,
                SesionConteo.ubicacion_id.in_(ubicacion_ids),
                SesionConteo.estado.in_(['PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO'])
            ).all()
        }

        creadas = []
        reclamadas = []
        omitidas = 0
        from app.utils.fecha import fecha_hoy_bogota
        hoy = fecha_hoy_bogota()
        for reg in registros:
            existente = activas_por_ubicacion.get(reg.ubicacion_id)
            if existente:
                # Sin operario forzado no hay nada que reclamar — mismo
                # comportamiento de siempre (evita duplicar sobre un PENDIENTE
                # que ya espera a que alguien lo tome).
                if existente.estado != EstadoConteo.PENDIENTE or not operario_forzado:
                    omitidas += 1
                    continue
                existente.operario_id = operario_forzado.id
                # Pasa a ser un conteo forzado: el dispensador prioriza tipo
                # MANUAL (ver MobileService._orden_cola_preasignada). Sin esto
                # conservaba su lugar viejo en la cola del operario.
                existente.tipo = 'MANUAL'
                reclamadas.append(existente.codigo)
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
                estado='PENDIENTE',
                operario_id=operario_forzado.id if operario_forzado else None,
            )
            db.session.add(sesion)
            creadas.append(sesion_codigo)

        # Pausar el otro conteo EN_PROCESO del operario forzado (si tiene uno) —
        # solo si de verdad le vamos a asignar algo nuevo. No hace falta excluir
        # lo que acabamos de crear/reclamar: ambas ramas de arriba solo dejan
        # sesiones en PENDIENTE (nunca EN_PROCESO), así que no pueden aparecer
        # en esta consulta.
        #
        # Versión anterior excluía por ubicacion_id — incorrecto: ubicaciones
        # genéricas como SIESA-GENERAL las comparten productos distintos, así
        # que un EN_PROCESO de OTRO SKU en la misma ubicación quedaba sin
        # pausar por error (bug real, encontrado en vivo 2026-09-14 — dos
        # sesiones de SKUs distintos en ubicacion_id=20, la del operario
        # forzado sobrevivía intacta).
        if operario_forzado and (creadas or reclamadas):
            en_proceso_otro = SesionConteo.query.filter(
                SesionConteo.operario_id == operario_forzado.id,
                SesionConteo.estado == EstadoConteo.EN_PROCESO,
            ).all()
            for s in en_proceso_otro:
                logger.info(
                    f'[CONTEO MANUAL] Pausando sesión {s.codigo} (id={s.id}) EN_PROCESO '
                    f'del operario #{operario_forzado.id} — reemplazada por conteo forzado de {codigo}'
                )
                s.estado = EstadoConteo.PENDIENTE
                s.operario_id = None
                s.fecha_inicio = None
                s.cantidad_fisica = None

        try:
            db.session.commit()
        except Exception as e_commit:
            db.session.rollback()
            raise ValueError(f'Error al crear sesiones de conteo manual: {e_commit}') from e_commit

        return {
            'tareas_creadas': len(creadas) + len(reclamadas),
            'tareas_nuevas': len(creadas),
            'tareas_reclamadas': len(reclamadas),
            'omitidas_ya_activas': omitidas,
            'producto': codigo,
            'producto_nombre': producto.nombre or '',
            'codigos': creadas + reclamadas,
            'operario_id': operario_forzado.id if operario_forzado else None,
            'operario_nombre': operario_forzado.nombre if operario_forzado else None,
        }

    #: Resultados de Auditoría de picking que piden un conteo cíclico forzado del
    #: SKU. La auditoría diagnostica; el conteo mide y ajusta Siesa. Añadir un
    #: resultado aquí no exige tocar `auditar_tarea`.
    RESULTADOS_DE_AUDITORIA_QUE_FUERZAN_CONTEO = frozenset({'ENCONTRADO'})

    @staticmethod
    def forzar_desde_auditoria(tarea, operario_id: int = None) -> dict:
        """Conteo cíclico forzado del SKU de una tarea auditada.

        Reutiliza `crear_conteo_manual` (mismo camino, misma prioridad de cola).
        Nunca levanta: un fallo al generar el conteo no puede deshacer una
        auditoría ya cerrada, pero SÍ se declara — `{'ok': False, 'error': ...}`
        — para que quien audita sepa que el conteo no quedó creado.
        """
        producto = tarea.producto
        codigo = (producto.codigo_siesa or producto.codigo) if producto else None
        if not codigo:
            return {'ok': False, 'error': 'La tarea no tiene producto para contar'}
        try:
            r = ConteoService.crear_conteo_manual(
                tarea.almacen_id, codigo, operario_id=operario_id)
        except ValueError as e:
            return {'ok': False, 'error': str(e)}
        return {'ok': True, 'producto': codigo,
                'tareas_creadas': r['tareas_creadas'], 'codigos': r['codigos'],
                'operario_nombre': r.get('operario_nombre')}

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