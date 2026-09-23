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

        ConteoService.verificar_puede_contar(sesion, operario_id)

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
            # Fuera del lock (el commit lo soltó) y antes de que el operario
            # reciba la tarea, o sea antes de que empiece a contar.
            ConteoService.registrar_foto_inicio(sesion)

        # Retornar SOLO vista ciega — sin cantidad esperada. Más lo que pinta
        # el HUD (producto, empaque, si la ubicación es física) y lo que ESTA
        # persona lleva contado en esta sesión — el suyo, nunca el de otro
        # conteo de la cadena: el HUD retoma desde ahí en vez de desde cero.
        return {**sesion.to_dict_operario(), **ConteoService.vista_hud(sesion),
                'cantidad_contada': sesion.cantidad_fisica or 0}

    @staticmethod
    def _bodega_siesa_de(sesion: SesionConteo):
        """La bodega Siesa del almacén de la sesión, o `None`."""
        if not sesion.almacen_id:
            return None
        from app.models.almacen import Almacen as _Alm
        alm = db.session.get(_Alm, sesion.almacen_id)
        return alm.bodega_siesa_id if alm else None

    @staticmethod
    def registrar_foto_inicio(sesion: SesionConteo) -> None:
        """Lee y guarda la foto de Siesa de la APERTURA del conteo.

        **Todo sitio que pasa un conteo a EN_PROCESO la llama** (trinquete:
        `tests/test_conteo_ventas_durante_conteo.py::TestTodaAperturaTomaFotoDeInicio`).
        Se llama DESPUÉS del commit que abre la tarea —la lectura HTTP no puede
        ir dentro del `with_for_update`— y antes de devolverle la tarea al
        operario, o sea antes de que empiece a contar.

        **Nunca levanta.** La apertura es lo que el operario ve: si Siesa no
        responde (timeout de 8 s, circuito abierto, después de las 8 p.m. —
        Regla 14), la tarea se abre igual y la foto queda vacía. Un conteo sin
        foto de inicio decide MATCH o segundo conteo, pero no ajusta
        (`motivo_bloqueo_ajuste`): Regla 0, y un ajuste siempre puede esperar.

        Se sobrescribe en cada apertura PENDIENTE → EN_PROCESO: una sesión que
        vuelve a PENDIENTE (zombi, pausa, liberada) se recuenta desde cero.
        """
        try:
            foto = None
            bodega = ConteoService._bodega_siesa_de(sesion)
            if sesion.producto_codigo_siesa and bodega:
                foto = ConteoService.consultar_foto_siesa(
                    producto_codigo_siesa=sesion.producto_codigo_siesa, bodega=bodega)
            ConteoService._grabar_foto_inicio(sesion, foto)
            db.session.commit()
            if foto is None:
                logger.warning(
                    f'[CONTEO] {sesion.codigo} abierto SIN foto de inicio de Siesa — '
                    f'se puede contar, pero este conteo no va a poder ajustar Siesa')
        except Exception as e:
            db.session.rollback()
            logger.error(f'[CONTEO] No se pudo guardar la foto de inicio de la sesión '
                         f'{getattr(sesion, "id", "?")}: {e} — la tarea se abre igual, '
                         f'sin foto de inicio (no ajustará)')

    @staticmethod
    def _grabar_foto_inicio(sesion: SesionConteo, foto) -> None:
        """La foto de inicio entera, o vacía entera — nunca a medias."""
        if foto is None:
            sesion.existencia_inicio_siesa = None
            sesion.cant_pos_inicio_siesa = None
            sesion.salida_sin_conf_inicio_siesa = None
            sesion.foto_inicio_at = None
            return
        sesion.existencia_inicio_siesa = foto['existencia']
        sesion.cant_pos_inicio_siesa = foto['cant_pos']
        sesion.salida_sin_conf_inicio_siesa = foto['salida_sin_conf']
        sesion.foto_inicio_at = foto['leido_at']

    #: Qué se compara entre la foto de inicio y la del cierre. La existencia y
    #: el POS son los dos términos del teórico. La salida sin confirmar también:
    #: si cambia sola —una remisión o un traslado que se crea o se anula
    #: mientras se cuenta— hay mercancía que se está moviendo del estante en
    #: ese intervalo, aunque el teórico todavía no lo refleje.
    CAMPOS_MOVIMIENTO = (
        ('existencia', 'existencia_inicio_siesa', 'existencia_siesa'),
        ('cant_pos', 'cant_pos_inicio_siesa', 'cant_pos_siesa'),
        ('salida_sin_conf', 'salida_sin_conf_inicio_siesa', 'salida_sin_conf_siesa'),
    )

    @staticmethod
    def movimiento_durante_conteo(sesion: SesionConteo, foto_cierre: dict = None):
        """¿Se movió Siesa mientras se contaba? Descripción del cambio, o `None`.

        **Una política, una función**: la usan `registrar_conteo` (para mandar
        a recontar) y `motivo_bloqueo_ajuste` (para no ajustar nunca un conteo
        contaminado, venga por donde venga).

        Compara la foto de inicio de la sesión contra `foto_cierre` (la foto
        recién leída, antes de grabarla) o, si no se pasa, contra la foto de
        cierre ya grabada en la sesión.

        `None` también cuando falta alguna de las dos fotos: acá no se puede
        saber, y esa ausencia la juzga `motivo_bloqueo_ajuste` por su cuenta.
        """
        if sesion.foto_inicio_at is None:
            return None
        cambios = []
        for clave, col_inicio, col_cierre in ConteoService.CAMPOS_MOVIMIENTO:
            inicio = getattr(sesion, col_inicio)
            cierre = (foto_cierre.get(clave) if foto_cierre is not None
                      else getattr(sesion, col_cierre))
            if inicio is None or cierre is None:
                return None
            # Las columnas son enteras; la foto recién leída viene en float.
            # Se comparan en la misma escala para no ver un cambio donde solo
            # hay una conversión.
            if int(round(float(inicio))) != int(round(float(cierre))):
                cambios.append(f'{clave} {float(inicio):g}→{float(cierre):g}')
        return ', '.join(cambios) or None

    #: Por qué se descartó un conteo (`conteos_descartados[*].motivo`). Las
    #: entradas anteriores a este campo son todas de movimiento: traen la clave
    #: `movimiento` (`motivo_de_descarte`).
    DESCARTE_MOVIMIENTO = 'MOVIMIENTO'
    DESCARTE_FUERA_DE_TOLERANCIA = 'FUERA_DE_TOLERANCIA'
    #: No es un conteo descartado: el líder reabrió la sesión bloqueada. Corta
    #: la cuenta de recuentos por movimiento (`recuentos_por_movimiento_vigentes`)
    #: — reabrir un MOVIMIENTO_CONTINUO es darle otra oportunidad en un momento
    #: más quieto, no bloquearlo otra vez al primer movimiento.
    EVENTO_REABIERTO = 'REABIERTO'

    @staticmethod
    def motivo_de_descarte(entrada):
        """El motivo de una entrada de `conteos_descartados`, o `None` si no es
        un conteo descartado (un evento, o una entrada ilegible)."""
        if not isinstance(entrada, dict) or 'evento' in entrada:
            return None
        if entrada.get('motivo'):
            return entrada['motivo']
        return ConteoService.DESCARTE_MOVIMIENTO if 'movimiento' in entrada else None

    @staticmethod
    def recuentos_por_movimiento_vigentes(sesion: SesionConteo) -> int:
        """Cuántos conteos de esta sesión se descartaron por movimiento desde
        la última vez que el líder la reabrió."""
        n = 0
        for entrada in reversed(sesion.lista_conteos_descartados()):
            if isinstance(entrada, dict) and entrada.get('evento') == ConteoService.EVENTO_REABIERTO:
                break
            if ConteoService.motivo_de_descarte(entrada) == ConteoService.DESCARTE_MOVIMIENTO:
                n += 1
        return n

    @staticmethod
    def recuentos_propios_usados(sesion: SesionConteo) -> int:
        """Cuántos recuentos propios por tolerancia ya se usaron en esta
        sesión. **No se reinicia al reabrir**: es uno por cadena."""
        return sum(1 for e in sesion.lista_conteos_descartados()
                   if ConteoService.motivo_de_descarte(e) == ConteoService.DESCARTE_FUERA_DE_TOLERANCIA)

    @staticmethod
    def _descartar_conteo(sesion: SesionConteo, operario_id: int, cantidad_fisica,
                          foto, *, motivo: str, **extra) -> None:
        """Guarda lo contado en `conteos_descartados` y deja la MISMA sesión
        lista para recontar desde cero, a nombre del mismo operario. No hace
        commit. Pre-condición: la sesión está bajo `with_for_update`.

        **Por qué la misma sesión y no una nueva.** La cadena CC1 → CC2 → CC3
        es de un hijo por padre (`hijo_conteo`, `uselist=False`), la cola del
        Conteo Definitivo se arma sobre ella y el índice único de sesiones
        activas no admite dos CC1 vivos del mismo hueco. Una sesión nueva
        rompería las tres cosas; recontar sobre la misma no toca ninguna.

        La foto del cierre del conteo descartado se vuelve la foto de INICIO
        del recuento: es la lectura más reciente de Siesa, anterior a que el
        operario vuelva a contar. Si algo se mueve entre esa lectura y el nuevo
        cierre, el recuento también se descarta. Sin foto del cierre (Siesa no
        respondió) se conserva la de inicio que había: el intervalo cubierto
        queda igual o mayor, nunca menor.
        """
        import json
        historial = sesion.lista_conteos_descartados()
        entrada = {
            'motivo': motivo,
            'cantidad_fisica': cantidad_fisica,
            'operario_id': operario_id,
            'inicio': {
                'existencia': sesion.existencia_inicio_siesa,
                'cant_pos': sesion.cant_pos_inicio_siesa,
                'salida_sin_conf': sesion.salida_sin_conf_inicio_siesa,
                'at': sesion.foto_inicio_at.isoformat() if sesion.foto_inicio_at else None,
            },
            'cierre': ({
                'existencia': foto['existencia'],
                'cant_pos': foto['cant_pos'],
                'salida_sin_conf': foto['salida_sin_conf'],
                'at': foto['leido_at'].isoformat() if foto.get('leido_at') else None,
            } if foto is not None else None),
            'descartado_at': datetime.utcnow().isoformat(),
        }
        entrada.update(extra)
        historial.append(entrada)
        sesion.conteos_descartados = json.dumps(historial, ensure_ascii=False, default=str)
        sesion.operario_id = operario_id
        sesion.cantidad_fisica = None
        sesion.diferencia = None
        sesion.estado = EstadoConteo.EN_PROCESO
        sesion.fecha_inicio = datetime.utcnow()
        if foto is not None:
            ConteoService._grabar_foto_inicio(sesion, foto)

    @staticmethod
    def _pedir_recuento(sesion: SesionConteo, operario_id: int, cantidad_fisica,
                        foto: dict, movimiento: str) -> dict:
        """El conteo se contaminó (Siesa se movió mientras se contaba): se
        descarta y la MISMA sesión queda lista para recontar
        (`_descartar_conteo`). Pre-condición: la sesión está bajo
        `with_for_update`.

        **Con techo** (2026-09-23). Sin él, un producto que se vende sin parar
        pedía recontar para siempre: el operario contaba, Siesa se movía, se
        descartaba, y otra vez. Después de `MAX_RECUENTOS_POR_MOVIMIENTO`
        recuentos (contados desde la última reapertura del líder) el siguiente
        movimiento **bloquea** la sesión con `MOVIMIENTO_CONTINUO`: va a la cola
        del líder, que la reabre en un momento quieto o la cancela. El conteo
        de ese intento también queda en `conteos_descartados`.
        """
        from app.models.conteo import MotivoBloqueoConteo
        from app.services import conteo_politica as politica
        previos = ConteoService.recuentos_por_movimiento_vigentes(sesion)
        ConteoService._descartar_conteo(
            sesion, operario_id, cantidad_fisica, foto,
            motivo=ConteoService.DESCARTE_MOVIMIENTO, movimiento=movimiento)
        bloquear = previos >= politica.MAX_RECUENTOS_POR_MOVIMIENTO
        if bloquear:
            sesion.estado = EstadoConteo.BLOQUEADO
            sesion.motivo_bloqueo = MotivoBloqueoConteo.MOVIMIENTO_CONTINUO
            sesion.bloqueado_en = datetime.utcnow()
            sesion.motivo_edicion = (
                f'[{MotivoBloqueoConteo.MOVIMIENTO_CONTINUO}] Siesa se movió mientras '
                f'se contaba en {previos + 1} intentos seguidos (último: {movimiento})')
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ValueError(f'Error al pedir el recuento: {e}') from e
        if bloquear:
            logger.warning(
                f'[CONTEO] {sesion.codigo}: Siesa se movió en {previos + 1} intentos '
                f'seguidos ({movimiento}) — BLOQUEADO para el líder (MOVIMIENTO_CONTINUO)')
            return {
                'resultado': 'BLOQUEADO',
                'motivo_bloqueo': MotivoBloqueoConteo.MOVIMIENTO_CONTINUO,
                'mensaje': ('Este producto se está vendiendo mientras contás: queda '
                            'para el líder. Seguí con la próxima tarea.'),
                'sesion_id': sesion.id,
            }
        logger.warning(
            f'[CONTEO] {sesion.codigo}: Siesa se movió mientras se contaba '
            f'({movimiento}) — conteo de {cantidad_fisica} DESCARTADO, se recuenta')
        # **A ciegas también acá** (2026-09-23): la respuesta va al operario,
        # y antes llevaba `movimiento` —«existencia 10→9, cant_pos 2→3»—, o sea
        # los números de Siesa que el conteo ciego existe para no mostrarle.
        # El detalle queda donde lo lee el líder: `conteos_descartados` y el
        # log de arriba.
        return {
            'resultado': 'RECONTAR',
            'mensaje': ('Recontar: hubo ventas o movimientos de este producto '
                        'mientras contabas. Este conteo no se usa — vuelve a '
                        'contar desde cero.'),
            'motivo': 'MOVIMIENTO_EN_SIESA',
            'sesion_id': sesion.id,
        }

    @staticmethod
    def _pedir_recuento_propio(sesion: SesionConteo, operario_id: int, cantidad_fisica,
                               foto, tolerancia: dict) -> dict:
        """El primer conteo quedó fuera de tolerancia: el MISMO operario lo
        recuenta una vez, **a ciegas**, antes de gastar el tiempo de otra
        persona en un segundo conteo. Pre-condición: `with_for_update`.

        A ciegas quiere decir que la respuesta no trae ni el teórico, ni la
        diferencia, ni si sobró o faltó: solo «recontá con cuidado». Lo que se
        contó y la evaluación de tolerancia quedan en `conteos_descartados`
        para el supervisor. El recuento REEMPLAZA al primer conteo: si cae
        dentro de tolerancia se acepta; si sigue fuera, segundo conteo de otra
        persona como siempre. Uno por cadena (`RECUENTOS_PROPIOS_POR_CADENA`).

        La foto del cierre del conteo descartado se borra de la sesión (queda
        en el historial): una sesión recontándose no tiene cierre.
        """
        diferencia = sesion.diferencia
        ConteoService._descartar_conteo(
            sesion, operario_id, cantidad_fisica, foto,
            motivo=ConteoService.DESCARTE_FUERA_DE_TOLERANCIA,
            diferencia=diferencia, tolerancia=tolerancia)
        ConteoService._grabar_foto(sesion, None)
        sesion.existencia_siesa = None
        sesion.fuente_existencia = None
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ValueError(f'Error al pedir el recuento: {e}') from e
        logger.info(
            f'[CONTEO] {sesion.codigo}: primer conteo fuera de tolerancia '
            f'({tolerancia.get("motivo")}) — recuento propio del operario #{operario_id}')
        return {
            'resultado': 'RECONTAR_TU',
            'mensaje': ('Recontá este producto con cuidado: revisá otra vez todos '
                        'los sitios donde puede estar y contá desde cero.'),
            'sesion_id': sesion.id,
        }

    @staticmethod
    def _aceptar_en_tolerancia(sesion: SesionConteo, tolerancia: dict) -> dict:
        """El primer conteo (o su único recuento propio) quedó dentro de
        tolerancia: se acepta sin segundo conteo y la diferencia chica se
        ajusta — dejarla para después es dejar que la deriva se acumule.

        El ajuste pasa por la política de siempre: `motivo_bloqueo_ajuste`
        (sin foto, salidas no POS, movimiento, traslado, en proceso…) y el tope
        en pesos de todo ajuste automático (`motivo_no_sale_solo`). Si alguna
        dice que no, la raíz queda en DESCUADRE con el motivo visible, para
        aprobación o recuento — nunca un segundo conteo por detrás.
        """
        diferencia = sesion.diferencia
        sesion.estado = EstadoConteo.DESCUADRE
        sesion.motivo_codigo = 'AJ-ENT' if diferencia > 0 else 'AJ-SAL'
        sesion.ajuste_por_tolerancia = True
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise ValueError(f'Error al registrar el conteo: {e}') from e

        bloqueo = ConteoService.motivo_bloqueo_ajuste(sesion)
        no_sale_solo = None if bloqueo else ConteoService.motivo_no_sale_solo(sesion)
        auto_encolado = False
        error = None
        if not bloqueo and not no_sale_solo:
            try:
                ConteoService._encolar_ajuste_fisico(sesion, aprobador_id=None)
                db.session.commit()
                auto_encolado = True
            except Exception as e:
                db.session.rollback()
                error = str(e)
                logger.error(f'[CONTEO] Ajuste por tolerancia de {sesion.codigo} no se '
                             f'encoló: {e} — queda en DESCUADRE')
        if auto_encolado:
            detalle = 'Diferencia dentro de tolerancia — ajuste encolado automáticamente'
        elif bloqueo:
            detalle = f'Diferencia dentro de tolerancia, pero el ajuste NO se envía: {bloqueo}'
        elif no_sale_solo:
            detalle = (f'Diferencia dentro de tolerancia, pero no sale sola: '
                       f'{no_sale_solo["mensaje"]}')
        else:
            detalle = (f'Diferencia dentro de tolerancia — el ajuste automático falló '
                       f'({error}); queda en DESCUADRE para aprobación manual')
        logger.info(f'[CONTEO] {sesion.codigo}: {detalle} ({tolerancia.get("motivo")})')
        return {
            'resultado': 'DENTRO_TOLERANCIA',
            # Lo que ve el operario: nada del teórico ni de la diferencia.
            'mensaje': 'Conteo registrado — gracias.',
            'detalle_ajuste': detalle,
            'auto_encolado': auto_encolado,
            'ajuste_bloqueado': bloqueo,
            'no_sale_solo': no_sale_solo,
            'sesion_id': sesion.id,
        }

    @staticmethod
    def _diferencia_del_ajuste(sesion: SesionConteo):
        """La diferencia que viajaría a Siesa: `cantidad_fisica − teorico_siesa`
        si hay foto, si no la `diferencia` guardada."""
        if sesion.cantidad_fisica is not None and sesion.teorico_siesa is not None:
            return sesion.cantidad_fisica - sesion.teorico_siesa
        return sesion.diferencia

    @staticmethod
    def valor_del_ajuste(sesion: SesionConteo):
        """Cuánto vale el ajuste de esta sesión en pesos (`Decimal`, sin signo),
        con el costo de su foto; `None` si no hay costo o diferencia."""
        from app.services import conteo_politica as politica
        return politica.valor_de_diferencia(ConteoService._diferencia_del_ajuste(sesion),
                                            sesion.costo_prom_uni_siesa)

    @staticmethod
    def motivo_no_sale_solo(sesion: SesionConteo):
        """Por qué el ajuste de esta sesión NO puede salir SOLO a Siesa
        (aunque un líder sí pueda aprobarlo): `{'codigo', 'mensaje'}` o `None`.

        **Una política, una función** para todo ajuste automático: el de
        tolerancia, el de CC1 == CC2, la pantalla (`to_dict`) y la guarda de
        `_encolar_ajuste_fisico` cuando no hay aprobador. Hoy: sin costo, o
        valor por encima de `CONTEO_TOPE_AUTOAJUSTE`
        (`conteo_politica.motivo_tope_autoajuste`).
        """
        from app.services import conteo_politica as politica
        diferencia = ConteoService._diferencia_del_ajuste(sesion)
        if not diferencia:
            return None
        return politica.motivo_tope_autoajuste(diferencia, sesion.costo_prom_uni_siesa)

    @staticmethod
    def motivo_no_puede_aprobar(usuario, sesion: SesionConteo):
        """¿Por qué ESTE usuario no puede aprobar ESTE ajuste? Texto, o `None`.

        **Aprobación por valor** (2026-09-23): supervisor y admin aprueban
        cualquier monto; un jefe de almacén, hasta `CONTEO_TOPE_APROBACION_JEFE`
        (por defecto 0: nada, que es lo que había). Sin costo no se sabe cuánto
        vale, así que el jefe no lo aprueba (Regla 0). Vive acá y no en la
        ruta: la ruta solo lo traduce a 403.
        """
        from app.routes._auth_helpers import Roles
        from app.services import conteo_politica as politica
        if usuario is None or not getattr(usuario, 'activo', True):
            return 'Usuario no encontrado o inactivo'
        if usuario.rol in Roles.LEAD:
            return None
        if usuario.rol != Roles.JEFE_ALMACEN:
            return ('Solo un supervisor o admin —o un jefe de almacén dentro de su '
                    'tope— puede aprobar ajustes de inventario')
        tope = politica.tope_aprobacion_jefe()
        if tope <= 0:
            return ('Un jefe de almacén no aprueba ajustes de inventario (tope de '
                    'aprobación en $0): lo aprueba un supervisor o admin')
        valor = ConteoService.valor_del_ajuste(sesion)
        if valor is None:
            return ('Este ajuste no tiene costo en la foto de Siesa: no se sabe cuánto '
                    'vale, así que lo aprueba un supervisor o admin')
        if valor > tope:
            return (f'Este ajuste vale {politica.pesos(valor)} y supera tu tope de '
                    f'aprobación de {politica.pesos(tope)}: lo aprueba un supervisor o admin')
        return None

    @staticmethod
    def _es_conteo_definitivo(sesion: SesionConteo) -> bool:
        """True si `sesion` es un CC3 — su padre (CC2) es a su vez es_segundo_conteo."""
        if not (sesion.es_segundo_conteo and sesion.sesion_origen_id):
            return False
        origen = db.session.get(SesionConteo, sesion.sesion_origen_id)
        return bool(origen and origen.es_segundo_conteo)

    @staticmethod
    def _operarios_previos_de_la_cadena(sesion: SesionConteo) -> set:
        """Quiénes ya contaron ANTES que esta sesión en su cadena: el operario
        de CC1 para un CC2; los de CC1 y CC2 para un CC3. Vacío para un CC1."""
        previos = set()
        origen_id = sesion.sesion_origen_id if sesion.es_segundo_conteo else None
        while origen_id:
            origen = db.session.get(SesionConteo, origen_id)
            if origen is None:
                break
            if origen.operario_id:
                previos.add(origen.operario_id)
            origen_id = origen.sesion_origen_id if origen.es_segundo_conteo else None
        return previos

    @staticmethod
    def motivo_no_puede_contar(sesion: SesionConteo, operario_id: int):
        """¿Por qué ESTE usuario no puede contar ESTA sesión? Texto, o `None`.

        **Una política, una función** (2026-09-23). Hasta hoy la regla vivía a
        medias en cada puerta: la cola de tienda excluía todo CC2/CC3, el
        despachador de NB1 no excluía nada, `asignar-lote` y `/editar`
        tampoco, y el control del CC3 solo corría si la sesión no tenía dueño
        —así que bastaba con que una de esas puertas le pusiera dueño para que
        dejara de correr—. Dos reglas:

        1. **El CC3 (conteo definitivo) lo cuenta supervisión** (decisión
           2026-09-04): define el ajuste, y un picker automático no debe ser
           quien desempata.
        2. **Doble ciego: nadie cuenta dos veces la misma cadena.** Un CC2
           hecho por quien hizo el CC1 no verifica nada: repite el mismo
           error con la misma mano, y CC1 == CC2 dispara el ajuste
           automático.

        Se aplica haya o no dueño: un dueño mal puesto no convierte en válido
        lo que la regla prohíbe.
        """
        if not sesion.es_segundo_conteo:
            return None
        if ConteoService._es_conteo_definitivo(sesion):
            from app.models.usuario import Usuario
            from app.routes._auth_helpers import Roles
            operario = db.session.get(Usuario, operario_id)
            if not operario or operario.rol not in Roles.SUPERVISION:
                return ('El conteo definitivo (CC3) solo lo puede tomar un '
                        'supervisor, admin o jefe de almacén')
        if operario_id in ConteoService._operarios_previos_de_la_cadena(sesion):
            return ('Doble ciego: ya contaste este producto en esta misma cadena '
                    'de conteo. La verificación la tiene que hacer otra persona.')
        return None

    @staticmethod
    def verificar_puede_contar(sesion: SesionConteo, operario_id: int) -> None:
        """`motivo_no_puede_contar` como excepción, para las puertas que abren,
        escanean o registran un conteo."""
        motivo = ConteoService.motivo_no_puede_contar(sesion, operario_id)
        if motivo:
            raise ValueError(motivo)

    @staticmethod
    def filtros_pool_sin_dueno(operario_id: int, almacen_id) -> list:
        """Filtros SQL de «qué conteo sin dueño se le puede dar a este operario».

        Es `motivo_no_puede_contar` escrita para una consulta: toda puerta que
        reparte conteos sin dueño —los despachadores móviles, el intercalado
        durante el picking, `asignar-lote`— la usa, y ninguna arma su propio
        filtro (`tests/test_conteo_pool_sin_dueno.py` lo exige por AST).

        - **De su almacén.** El despachador de NB1 no filtraba: un operario de
          NB1 podía recibir un conteo de NS1, el más viejo primero.
        - **Nunca un CC3**: su cola es `/api/conteo/definitivos`.
        - **Un CC2 solo si el CC1 no lo hizo él.** Un CC2 sin dueño es normal:
          nace así cuando no hay par disponible, y queda así cuando el barrido
          de zombis o la pausa de un conteo forzado lo liberan.

        Sin almacén conocido no se reparte nada (Regla 0): repartir sin saber
        la bodega es exactamente el defecto que esto cierra.
        """
        from sqlalchemy import false, or_
        from sqlalchemy.orm import aliased
        if not almacen_id:
            return [false()]
        Cc1 = aliased(SesionConteo)
        cc1_ajenos = (db.session.query(Cc1.id)
                      .filter(Cc1.es_segundo_conteo.is_(False),
                              or_(Cc1.operario_id.is_(None),
                                  Cc1.operario_id != operario_id))
                      .scalar_subquery())
        return [
            SesionConteo.estado == EstadoConteo.PENDIENTE,
            SesionConteo.operario_id.is_(None),
            SesionConteo.almacen_id == almacen_id,
            # CC1, o un CC2 cuyo origen es un CC1 ajeno. Un CC3 tiene por
            # origen un CC2, así que no entra por ninguna de las dos ramas.
            or_(SesionConteo.es_segundo_conteo.is_(False),
                SesionConteo.sesion_origen_id.in_(cc1_ajenos)),
        ]

    @staticmethod
    def almacen_de_usuario(usuario):
        """El `almacen_id` de un usuario: el suyo, o el de su `bodega_siesa_id`
        (un picker_traslado puede tener solo la bodega). `None` si no se sabe."""
        if usuario is None:
            return None
        if usuario.almacen_id:
            return usuario.almacen_id
        if usuario.bodega_siesa_id:
            from app.models.almacen import Almacen
            alm = Almacen.query.filter_by(bodega_siesa_id=usuario.bodega_siesa_id).first()
            return alm.id if alm else None
        return None

    # ── Lo que se cuenta: siempre declarado, nunca un default ────────────────

    @staticmethod
    def exigir_cantidad_declarada(cantidad_fisica, *, cero_confirmado: bool = False) -> int:
        """La cantidad con la que se cierra un conteo, validada. **Una política,
        una función** (P0-HUD, 2026-09-23): la llama `registrar_conteo`, que es
        por donde pasa TODO cierre de conteo, venga de la pantalla del
        operario, del Conteo Definitivo o de la API.

        La clase que cierra: **un conteo se confirma sin que alguien diga
        cuánto contó.** `MobileService.confirmar_tarea` hacía
        `cantidad_fisica if … is not None else 0`: un operario que no escaneó
        nada —porque no encontró el producto, en una bodega sin layout donde
        eso es lo normal— cerraba en CERO. CC1 = 0 y CC2 = 0 coinciden →
        ajuste automático a cero en Siesa. El defecto más caro del sistema.

        - `None` → se rechaza. Nadie dijo cuánto contó.
        - Negativo, fraccionario o booleano → se rechaza.
        - **Cero exige `cero_confirmado`**: «revisé y no hay ninguna» es un
          dato; «no escaneé nada» no lo es. Quien no lo encontró tiene su
          propio cierre (`bloquear_conteo` con `NO_ENCONTRADO`), que no es un
          cero y nunca ajusta.
        """
        if cantidad_fisica is None:
            raise ValueError(
                'Falta la cantidad contada: un conteo no se cierra sin que '
                'alguien diga cuánto contó. Si no encontraste el producto, usá '
                '«No lo encontré». Si la pantalla no te deja, cerrá y volvé a '
                'abrir la app (está desactualizada).')
        if isinstance(cantidad_fisica, bool):
            raise ValueError('La cantidad contada debe ser un número entero')
        if isinstance(cantidad_fisica, float) and cantidad_fisica.is_integer():
            cantidad_fisica = int(cantidad_fisica)
        if not isinstance(cantidad_fisica, int):
            raise ValueError('La cantidad contada debe ser un número entero')
        if cantidad_fisica < 0:
            raise ValueError('La cantidad contada no puede ser negativa')
        if cantidad_fisica == 0 and not cero_confirmado:
            raise ValueError(
                'Contaste 0: confirmá que NO hay ninguna unidad en la bodega. '
                'Si lo que pasa es que no lo encontraste, usá «No lo encontré».')
        return cantidad_fisica

    @staticmethod
    def vista_hud(sesion: SesionConteo) -> dict:
        """Lo que el HUD de conteo muestra del producto y del lugar — ciego:
        nunca la cantidad esperada. **Una sola definición** para la pantalla
        del operario (`MobileService._conteo_a_dict`) y la del Conteo
        Definitivo (`obtener_tarea_operario`), que antes armaban cada una su
        dict y divergían.

        `ubicacion_fisica` dice si la ubicación le sirve a alguien para buscar
        (`Ubicacion.es_fisica`). Sin layout, todo NB1 está en `SIESA-GENERAL`:
        ahí lo que se pinta en grande es el PRODUCTO y «buscalo en toda la
        bodega».
        """
        p = sesion.producto
        ub = sesion.ubicacion
        factor = (p.factor_conversion or 1) if p else 1
        return {
            'ubicacion': ub.codigo if ub else '',
            'ubicacion_fisica': bool(ub and ub.es_fisica),
            'producto_codigo': p.codigo if p else '',
            'producto_nombre': p.nombre if p else '',
            'producto_codigo_barras': (p.codigo_barras or '') if p else '',
            'unidad_empaque': (p.unidad_empaque or '').upper() if p else '',
            'factor_conversion': factor if factor > 1 else 1,
        }

    # ── Bloqueo: «no lo encontré» y otros problemas ──────────────────────────

    @staticmethod
    def bloquear_conteo(sesion_id: int, operario_id: int, motivo: str,
                        observaciones: str = None) -> dict:
        """El operario no puede cerrar su conteo con un número: lo bloquea y
        lo decide el líder (`reabrir_bloqueado` / `cancelar_bloqueado`).

        **«No lo encontré» (`NO_ENCONTRADO`) NO es un cero** — ver
        `MotivoBloqueoConteo`. Un conteo BLOQUEADO no produce MATCH, ni
        segundo conteo, ni ajuste: `registrar_conteo` y `confirmar_ajuste` lo
        rechazan por estado, y su raíz traba la generación de otra cadena del
        mismo hueco (`EstadoConteo.CADENA_EN_CURSO`).

        Vivía en la ruta `/api/mobile/reportar-problema`; la política va en el
        servicio, que protege la operación y no solo esa puerta.
        """
        from app.models.conteo import MotivoBloqueoConteo
        motivo = (motivo or '').strip().upper()
        if motivo not in MotivoBloqueoConteo.VALIDOS:
            raise ValueError(f'Motivo de bloqueo desconocido: {motivo or "(vacío)"}')
        observaciones = (observaciones or '').strip() or None
        if motivo == MotivoBloqueoConteo.OTRO and not observaciones:
            raise ValueError('Contá qué pasó: el líder necesita saberlo para decidir')

        sesion = (SesionConteo.query.filter_by(id=sesion_id)
                  .with_for_update().first())
        if not sesion:
            raise LookupError(f'Sesión de conteo {sesion_id} no encontrada')
        if sesion.operario_id != operario_id:
            raise PermissionError('Esta sesión de conteo no te está asignada')
        # Solo conteos activos: impide revertir AJUSTADO → BLOQUEADO.
        if sesion.estado not in (EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO):
            raise ValueError(
                f'No se puede reportar problema en un conteo con estado {sesion.estado}')

        sesion.estado = EstadoConteo.BLOQUEADO
        sesion.motivo_bloqueo = motivo
        sesion.bloqueado_en = datetime.utcnow()
        sesion.motivo_edicion = f'[{motivo}] {observaciones or ""}'.strip()
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise RuntimeError(f'Error al bloquear la sesión de conteo: {e}') from e
        logger.warning(f'[CONTEO] {sesion.codigo} BLOQUEADO por operario #{operario_id}: '
                       f'{motivo} {observaciones or ""}'.strip())
        return {
            'ok': True,
            'mensaje': ('Reportado: no lo encontraste. El líder decide si se '
                        'vuelve a buscar o se cancela — no se ajusta nada.'
                        if motivo == MotivoBloqueoConteo.NO_ENCONTRADO
                        else 'Problema de conteo reportado — el líder lo revisa'),
            'motivo': motivo,
            'tarea_id': sesion_id,
        }

    @staticmethod
    def _exigir_supervision(usuario_id: int):
        from app.models.usuario import Usuario
        from app.routes._auth_helpers import Roles
        u = db.session.get(Usuario, usuario_id)
        if not u or u.rol not in Roles.SUPERVISION:
            raise PermissionError('Solo un supervisor, admin o jefe de almacén puede hacer esto')
        return u

    @staticmethod
    def _raiz_de(sesion: SesionConteo) -> SesionConteo:
        raiz = sesion
        while raiz.es_segundo_conteo and raiz.sesion_origen_id:
            padre = db.session.get(SesionConteo, raiz.sesion_origen_id)
            if padre is None:
                break
            raiz = padre
        return raiz

    @staticmethod
    def nivel_en_cadena(sesion: SesionConteo) -> str:
        """'CC1' | 'CC2' | 'CC3' — para que el líder sepa qué está decidiendo."""
        if not sesion.es_segundo_conteo:
            return 'CC1'
        return 'CC3' if ConteoService._es_conteo_definitivo(sesion) else 'CC2'

    @staticmethod
    def listar_bloqueados(almacen_id: int = None) -> list:
        """Conteos BLOQUEADOS esperando al líder, con su motivo. Sin cantidades:
        el líder puede terminar haciendo el CC3 de la misma cadena."""
        q = SesionConteo.query.filter(SesionConteo.estado == EstadoConteo.BLOQUEADO)
        if almacen_id:
            q = q.filter(SesionConteo.almacen_id == almacen_id)
        filas = []
        for s in q.order_by(SesionConteo.id.desc()).all():
            filas.append({
                'id': s.id,
                'codigo': s.codigo,
                'nivel': ConteoService.nivel_en_cadena(s),
                'tipo': s.tipo,
                'almacen_id': s.almacen_id,
                'almacen_nombre': s.almacen.nombre if s.almacen else None,
                'producto_codigo': s.producto.codigo if s.producto else None,
                'producto_nombre': s.producto.nombre if s.producto else None,
                'ubicacion_codigo': s.ubicacion.codigo if s.ubicacion else None,
                'motivo_bloqueo': s.motivo_bloqueo or 'SIN_MOTIVO_REGISTRADO',
                'nota': s.motivo_edicion,
                'reportado_por_nombre': s.operario.nombre if s.operario else None,
                'bloqueado_en': s.bloqueado_en.isoformat() if s.bloqueado_en else None,
            })
        return filas

    @staticmethod
    def reabrir_bloqueado(sesion_id: int, usuario_id: int, nota: str = None,
                          operario_id: int = None) -> SesionConteo:
        """El líder devuelve un conteo BLOQUEADO a la cola: PENDIENTE, sin lo
        contado y sin foto de inicio (se relee al abrirlo). Sin dueño, o con
        el que el líder elija — que tiene que poder contarlo
        (`motivo_no_puede_contar`: el CC3 es de supervisión y el doble ciego
        no se dobla por reabrir).

        Una raíz bloqueada ANTERIOR a que BLOQUEADO trabara el hueco pudo
        quedar con otra cadena viva al lado: reabrirla serían dos cadenas del
        mismo hueco (el defecto «un solo ajuste por hueco»). Se niega; se
        cancela en su lugar.
        """
        lider = ConteoService._exigir_supervision(usuario_id)
        sesion = (SesionConteo.query.filter_by(id=sesion_id)
                  .with_for_update().first())
        if not sesion:
            raise LookupError('Sesión de conteo no encontrada')
        if sesion.estado != EstadoConteo.BLOQUEADO:
            raise ValueError(f'Solo se reabre un conteo BLOQUEADO (está {sesion.estado})')

        if not sesion.es_segundo_conteo:
            otra = SesionConteo.query.filter(
                SesionConteo.id != sesion.id,
                SesionConteo.producto_id == sesion.producto_id,
                SesionConteo.ubicacion_id == sesion.ubicacion_id,
                SesionConteo.almacen_id == sesion.almacen_id,
                SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True),
            ).first()
            if otra:
                raise ValueError(
                    f'Ya hay otro conteo vivo de este producto ({otra.codigo}, '
                    f'{otra.estado}). Reabrir este dejaría dos: cancelalo.')

        if operario_id is not None:
            from app.models.usuario import Usuario
            op = db.session.get(Usuario, operario_id)
            if not op or not op.activo:
                raise ValueError(f'Operario {operario_id} no encontrado o inactivo')
            no_puede = ConteoService.motivo_no_puede_contar(sesion, operario_id)
            if no_puede:
                raise ValueError(no_puede)

        antes = sesion.motivo_edicion or f'[{sesion.motivo_bloqueo or "?"}]'
        ahora = datetime.utcnow()
        # La reapertura queda en el historial de la sesión: corta la cuenta de
        # recuentos por movimiento (un MOVIMIENTO_CONTINUO reabierto vuelve a
        # tener sus recuentos) y deja rastro de quién la devolvió a la cola.
        import json
        historial = sesion.lista_conteos_descartados()
        historial.append({'evento': ConteoService.EVENTO_REABIERTO, 'at': ahora.isoformat(),
                          'por': usuario_id, 'motivo_bloqueo': sesion.motivo_bloqueo})
        sesion.conteos_descartados = json.dumps(historial, ensure_ascii=False, default=str)
        sesion.estado = EstadoConteo.PENDIENTE
        sesion.operario_id = operario_id
        sesion.cantidad_fisica = None
        sesion.fecha_inicio = None
        sesion.motivo_bloqueo = None
        sesion.bloqueado_en = None
        ConteoService._grabar_foto_inicio(sesion, None)
        nota = (nota or '').strip()
        sesion.motivo_edicion = f'REABIERTO por {lider.nombre}' + (f': {nota}' if nota else '') \
            + f' (estaba bloqueado: {antes})'
        sesion.editado_por = usuario_id
        sesion.editado_en = ahora
        db.session.commit()
        logger.info(f'[CONTEO] {sesion.codigo} reabierto por #{usuario_id} '
                    f'(operario → {operario_id or "sin dueño"})')
        return sesion

    @staticmethod
    def cancelar_bloqueado(sesion_id: int, usuario_id: int, motivo: str) -> SesionConteo:
        """El líder descarta un conteo BLOQUEADO — y con él **toda su cadena**.

        Una cadena no avanza sin ese eslabón: cancelar solo un CC2 bloqueado
        dejaba la raíz en SEGUNDO_CONTEO para siempre, trabando el hueco
        (`CADENA_EN_CURSO`). Si el líder quiere ajustar con lo que ya dijo el
        CC1, la herramienta es `omitir-segundo` sobre la raíz, que cancela el
        bloqueado igual; cancelar es «esta cadena no vale».
        """
        ConteoService._exigir_supervision(usuario_id)
        motivo = (motivo or '').strip()
        if not motivo:
            raise ValueError('Se requiere un motivo de cancelación')
        sesion = (SesionConteo.query.filter_by(id=sesion_id)
                  .with_for_update().first())
        if not sesion:
            raise LookupError('Sesión de conteo no encontrada')
        if sesion.estado != EstadoConteo.BLOQUEADO:
            raise ValueError(f'Solo se cancela por acá un conteo BLOQUEADO (está {sesion.estado})')

        ahora = datetime.utcnow()
        vivos = set(EstadoConteo.CADENA_EN_CURSO) - {EstadoConteo.AJUSTANDO}
        nodo = ConteoService._raiz_de(sesion)
        while nodo is not None:
            if nodo.estado in vivos:
                nodo.estado = EstadoConteo.CANCELADO
                nodo.fecha_cierre = ahora
                nodo.motivo_edicion = (f'CANCELADO: {motivo}' if nodo.id == sesion.id
                                       else f'CANCELADO (cadena de {sesion.codigo}): {motivo}')
                nodo.editado_por = usuario_id
                nodo.editado_en = ahora
            nodo = nodo.hijo_conteo
        db.session.commit()
        logger.info(f'[CONTEO] {sesion.codigo} (bloqueado) y su cadena cancelados '
                    f'por #{usuario_id}: {motivo}')
        return sesion

    # ── Novedades: «mercancía sin código» ────────────────────────────────────

    @staticmethod
    def registrar_novedad_sin_codigo(sesion_id: int, operario_id: int, descripcion: str):
        """El operario encontró mercancía que no puede escanear. Queda para el
        líder y **no toca el conteo**: ni su estado ni lo contado."""
        from app.models.conteo import NovedadConteo
        descripcion = (descripcion or '').strip()
        if len(descripcion) < 3:
            raise ValueError('Describí lo que encontraste (qué es, cuántas, dónde)')
        sesion = db.session.get(SesionConteo, sesion_id)
        if not sesion:
            raise LookupError('Sesión de conteo no encontrada')
        if sesion.operario_id != operario_id:
            raise PermissionError('Esta sesión de conteo no te está asignada')
        nov = NovedadConteo(tipo=NovedadConteo.TIPO_SIN_CODIGO, sesion_id=sesion.id,
                            almacen_id=sesion.almacen_id, reportado_por=operario_id,
                            descripcion=descripcion[:2000], estado=NovedadConteo.ABIERTA)
        db.session.add(nov)
        db.session.commit()
        logger.info(f'[CONTEO] Novedad #{nov.id} (mercancía sin código) en {sesion.codigo} '
                    f'por operario #{operario_id}')
        return nov

    @staticmethod
    def listar_novedades(almacen_id: int = None, solo_abiertas: bool = True) -> list:
        from app.models.conteo import NovedadConteo
        q = NovedadConteo.query
        if solo_abiertas:
            q = q.filter(NovedadConteo.estado == NovedadConteo.ABIERTA)
        if almacen_id:
            q = q.filter(NovedadConteo.almacen_id == almacen_id)
        return [n.to_dict() for n in q.order_by(NovedadConteo.fecha_creacion.desc()).all()]

    @staticmethod
    def resolver_novedad(novedad_id: int, usuario_id: int, nota: str):
        from app.models.conteo import NovedadConteo
        ConteoService._exigir_supervision(usuario_id)
        nota = (nota or '').strip()
        if not nota:
            raise ValueError('Contá qué se hizo con la mercancía')
        nov = db.session.get(NovedadConteo, novedad_id)
        if not nov:
            raise LookupError('Novedad no encontrada')
        if nov.estado != NovedadConteo.ABIERTA:
            raise ValueError('Esa novedad ya estaba resuelta')
        nov.estado = NovedadConteo.RESUELTA
        nov.resuelta_por = usuario_id
        nov.resuelta_en = datetime.utcnow()
        nov.nota_resolucion = nota[:2000]
        db.session.commit()
        return nov

    @staticmethod
    def registrar_conteo(
        sesion_id: int,
        operario_id: int,
        cantidad_fisica: int,
        lote_id: str = None,
        *,
        cero_confirmado: bool = False,
    ):
        """
        Registra el conteo físico del operario.
        0. La cantidad tiene que venir declarada (`exigir_cantidad_declarada`):
           nunca un default, y un cero solo confirmado.
        1. Valida lote si el producto lo requiere.
        2. Consulta stock WMS (UbicacionProducto.cantidad) — sin llamada HTTP.
        3. Adquiere lock, re-valida estado y guarda.
        4. Decide: MATCH; en el CC1, dentro de tolerancia (se ajusta sin CC2) o
           fuera (RECONTAR_TU una vez, después SEGUNDO_CONTEO); en CC2/CC3, como
           siempre (ver «Conteo: tolerancias y topes» en CLAUDE.md).
        """
        from app.models.inventario import UbicacionProducto

        cantidad_fisica = ConteoService.exigir_cantidad_declarada(
            cantidad_fisica, cero_confirmado=cero_confirmado)

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
        ConteoService.verificar_puede_contar(sesion_pre, operario_id)

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
        ConteoService.verificar_puede_contar(sesion, operario_id)

        # ¿Se movió Siesa entre la apertura y este cierre? Entonces el físico
        # y la foto miden instantes distintos: el conteo no produce MATCH, ni
        # segundo conteo, ni ajuste. Se descarta y se recuenta. Va ANTES de
        # tocar nada de la sesión: un conteo contaminado no escribe su
        # diferencia en ningún lado, ni en la raíz.
        if foto is not None:
            movimiento = ConteoService.movimiento_durante_conteo(sesion, foto)
            if movimiento:
                return ConteoService._pedir_recuento(
                    sesion, operario_id, cantidad_fisica, foto, movimiento)

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

        # **Tolerancia del primer conteo** (2026-09-23). Solo el CC1: el CC2 y
        # el CC3 existen porque el primero ya quedó fuera, y ahí decide la
        # comparación entre conteos, como siempre. Lo que dijo el PRIMER conteo
        # válido se guarda una vez (es el acierto de la exactitud con
        # tolerancia); el recuento propio no lo pisa.
        tolerancia = None
        if not sesion.es_segundo_conteo:
            if resultado_conciliacion['es_match']:
                if sesion.tolerancia_primer_conteo is None:
                    sesion.tolerancia_primer_conteo = 'EXACTO'
            else:
                from app.services import conteo_politica as _politica
                tolerancia = _politica.evaluar_tolerancia(
                    diferencia, ConteoService.base_de_comparacion(sesion),
                    sesion.costo_prom_uni_siesa,
                    tipo=sesion.tipo, clase=sesion.clasificacion_abc)
                if sesion.tolerancia_primer_conteo is None:
                    sesion.tolerancia_primer_conteo = 'DENTRO' if tolerancia['dentro'] else 'FUERA'

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
                    # CC1 AVALA el ajuste automático, así que se le pide lo
                    # mismo que a cualquier conteo que decide un ajuste: sus
                    # dos fotos. Se mira ANTES de copiarle la observación de
                    # CC2, que pisa las suyas.
                    cc1_no_avala = (ConteoService._conteo_sin_sus_dos_fotos(origen)
                                    if debe_auto_encolar else None)
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
                    # El tope en pesos de todo ajuste automático: antes CC1 ==
                    # CC2 ajustaba sola cualquier cifra.
                    no_sale_solo = (ConteoService.motivo_no_sale_solo(origen)
                                    if debe_auto_encolar and not bloqueo else None)
                    if bloqueo:
                        # No se intenta: no es un fallo del encolado, es una
                        # decisión. Queda en DESCUADRE con la foto grabada, y la
                        # aprobación manual la va a negar por la misma razón.
                        error_auto_encolado = bloqueo
                        logger.warning(
                            f'[CONTEO] CC1==CC2 en {origen.codigo} pero el ajuste '
                            f'NO se encola: {bloqueo}'
                        )
                    elif cc1_no_avala:
                        # CC2 tiene sus dos fotos y el ajuste es aprobable,
                        # pero no solo: la confirmación de CC1 no se puede
                        # creer. Queda para el supervisor — un conteo limpio más
                        # una firma humana es el estándar del CC3.
                        logger.warning(
                            f'[CONTEO] CC1==CC2 en {origen.codigo}, pero CC1 '
                            f'{cc1_no_avala} — sin ajuste automático'
                        )
                    elif no_sale_solo:
                        logger.warning(
                            f'[CONTEO] CC1==CC2 en {origen.codigo}, pero el ajuste no '
                            f'sale solo: {no_sale_solo["mensaje"]}'
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
                    elif cc1_no_avala:
                        mensaje = (
                            f'Ambos conteos coinciden, pero el primero {cc1_no_avala}: '
                            'no se ajusta solo. Queda en DESCUADRE para aprobación '
                            'manual — el segundo conteo sí tiene sus dos fotos.'
                        )
                    elif no_sale_solo:
                        mensaje = (
                            'Ambos conteos coinciden, pero el ajuste no sale solo: '
                            f'{no_sale_solo["mensaje"]}. Queda en DESCUADRE para aprobación.'
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
                        'no_sale_solo': no_sale_solo,
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

            # Primer conteo con diferencia. Dentro de tolerancia: se acepta y se
            # ajusta sin segundo conteo. Fuera, y sin su recuento propio: el
            # mismo operario recuenta a ciegas. Fuera otra vez: segundo conteo
            # de otra persona, como siempre.
            if tolerancia is not None and tolerancia['dentro']:
                return ConteoService._aceptar_en_tolerancia(sesion, tolerancia)
            from app.services import conteo_politica as _politica
            if (tolerancia is not None and ConteoService.recuentos_propios_usados(sesion)
                    < _politica.RECUENTOS_PROPIOS_POR_CADENA):
                return ConteoService._pedir_recuento_propio(
                    sesion, operario_id, cantidad_fisica, foto, tolerancia)

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

            {existencia, cant_pos, salida_sin_conf, comprometida, costo_prom_uni,
             teorico, leido_at}

        o **None** si Siesa no responde o la fila no trae alguno de los tres
        campos de `CAMPOS_FOTO`.

        Campo ausente ≠ 0 (Regla 0). Un `f400_cant_pos_1` que no vino no es
        «no hay POS pendiente»: es «no sé cuánto hay». Tomarlo como 0 reabre
        exactamente el doble descuento que esta foto existe para cerrar, y en
        la tienda donde más duele. Media foto se trata como ninguna.

        `comprometida` viaja solo como contexto —puede venir `None`—; no entra
        a ninguna cuenta. `costo_prom_uni` tampoco: solo valoriza el ajuste en
        las estadísticas (`_costo_de_fila`).
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
            'costo_prom_uni': ConteoService._costo_de_fila(fila),
            'teorico': ConteoService.teorico(valores['existencia'], valores['cant_pos']),
            'leido_at': datetime.utcnow(),
        }

    @staticmethod
    def _costo_de_fila(fila: dict):
        """`f400_costo_prom_uni` de la fila de InvFecha, o `None`.

        Opcional, igual que la comprometida: el costo no decide nada del
        conteo —solo valoriza el ajuste en las estadísticas—, así que su
        ausencia nunca vuelve incompleta la foto. Ausente, ilegible o no
        finito → `None`, **nunca 0**: cero afirma «este ajuste no vale plata»,
        y lo que de verdad pasa es que no se sabe cuánto vale (Regla 0).
        Un costo ≤ 0 sí se devuelve tal cual: es lo que Siesa dijo, y el
        reporte lo declara como «sin valorizar» en vez de esconderlo.
        """
        import math
        crudo = fila.get('f400_costo_prom_uni')
        if crudo is None:
            return None
        try:
            valor = float(crudo)
        except (TypeError, ValueError):
            return None
        return valor if math.isfinite(valor) else None

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
            sesion.costo_prom_uni_siesa = None
            return
        sesion.existencia_siesa = foto['existencia']
        sesion.cant_pos_siesa = foto['cant_pos']
        sesion.salida_sin_conf_siesa = foto['salida_sin_conf']
        sesion.teorico_siesa = foto['teorico']
        sesion.foto_siesa_at = foto['leido_at']
        # `.get`: una foto armada sin costo (los stubs de los tests, o una
        # versión vieja de la foto) es una foto sin costo, no una foto rota.
        sesion.costo_prom_uni_siesa = foto.get('costo_prom_uni')

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
        # El costo viaja con la foto: el ajuste sale de la raíz, y valorizarlo
        # con el costo de CC1 mezclaría el instante de un conteo con la
        # diferencia de otro — el mismo defecto que esta función existe para
        # cerrar, trasladado a la plata.
        destino.costo_prom_uni_siesa = origen.costo_prom_uni_siesa
        # Y la foto de inicio: el bloqueo del ajuste de la raíz se decide sobre
        # la apertura y el cierre del conteo que resolvió, no sobre los de CC1.
        destino.existencia_inicio_siesa = origen.existencia_inicio_siesa
        destino.cant_pos_inicio_siesa = origen.cant_pos_inicio_siesa
        destino.salida_sin_conf_inicio_siesa = origen.salida_sin_conf_inicio_siesa
        destino.foto_inicio_at = origen.foto_inicio_at
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
    def _conteo_sin_sus_dos_fotos(sesion: SesionConteo):
        """Qué le falta a este conteo para avalar un ajuste, o `None`.

        Un conteo avala un ajuste solo si se midió entre dos fotos de Siesa: la
        de la apertura y la del cierre. Sin la del cierre no hay contra qué
        medir; sin la de la apertura no se sabe si Siesa se movió mientras se
        contaba (una venta de caja en el medio). Los dos casos, Regla 0: el
        conteo sirve para decidir MATCH o segundo conteo, no para ajustar.
        """
        if (sesion.fuente_existencia != 'SIESA' or sesion.teorico_siesa is None
                or sesion.cant_pos_siesa is None
                or sesion.salida_sin_conf_siesa is None):
            return 'no tiene foto de Siesa del cierre'
        if (sesion.foto_inicio_at is None or sesion.existencia_inicio_siesa is None
                or sesion.cant_pos_inicio_siesa is None
                or sesion.salida_sin_conf_inicio_siesa is None):
            return 'no tiene foto de Siesa de la apertura'
        return None

    #: Cuántos días operativos (Bogotá) ANTES del día del conteo sigue contando
    #: como «recibido hace poco» un traslado entrante. Con 1, bloquea lo
    #: recibido el mismo día del conteo o el anterior.
    #:
    #: Por qué no «el mismo día»: la tienda confirma la recepción —y con ella
    #: el ETS 173079 sube la existencia en Siesa— al contar las cajas en la
    #: puerta, y abrirlas y ponerlas en el estante puede quedar para el turno
    #: siguiente. Recibido a las 7 p.m. y contado a las 8 a.m. es el caso
    #: normal, y un corte a medianoche lo dejaría pasar.
    #:
    #: Por qué no un número de horas: no hay un dato medido de cuánto tardan en
    #: abrirse las cajas; cualquier N sería inventado. «Hoy o ayer» es el
    #: menor corte que contiene la noche de por medio sin depender de la hora.
    #:
    #: El costo de pasarse es un ajuste que espera un día (Regla 0: un ajuste
    #: siempre puede esperar). El de quedarse corto es un AJ-SAL falso por las
    #: cajas sin abrir, que Siesa vuelve a contar cuando se abren: el faltante
    #: fabricado queda como merma.
    DIAS_OPERATIVOS_RECEPCION_RECIENTE = 1

    @staticmethod
    def traslado_entrante_vivo(sesion: SesionConteo):
        """¿Había un traslado entrando a esta bodega con este SKU cuando se
        contó? Descripción, o `None`.

        La salida sin confirmar no lo delata: es una ENTRADA. Y es un faltante
        falso en las dos puntas del viaje:

        - **en tránsito** (`EN_TRANSITO`): la mercancía está en un camión o en
          la puerta, y dónde la cuenta Siesa depende de si ya entró el ETS;
        - **recibido hace poco** (`ENTREGADA`, con `fecha_entrega` dentro de
          la ventana `DIAS_OPERATIVOS_RECEPCION_RECIENTE`): el ETS ya subió la
          existencia, pero las cajas pueden estar sin abrir.

        Se juzga contra el INSTANTE DEL CONTEO (`foto_siesa_at`), no contra
        ahora: la aprobación puede ser días después, y el delta se fijó al
        contar. Un traslado despachado después del conteo no lo afectó; uno
        entregado antes de la ventana ya estaba en el estante.

        Un traslado `ENTREGADA` sin `fecha_entrega` —no debería existir: los
        dos caminos que entregan la escriben— no se puede ubicar en el tiempo y
        cuenta como vivo (Regla 0). El mensaje nombra el traslado para que
        alguien lo corrija.

        Sin foto del cierre devuelve `None`: esa sesión ya está bloqueada por
        no tener foto.
        """
        if sesion.foto_siesa_at is None or not sesion.producto_id:
            return None
        bodega = ConteoService._bodega_siesa_de(sesion)
        if not bodega:
            return None
        partes = [texto for _pid, texto in ConteoService._traslados_entrantes_vivos(
            bodega, sesion.foto_siesa_at, producto_id=sesion.producto_id)]
        return '; '.join(partes) or None

    @staticmethod
    def _traslados_entrantes_vivos(bodega: str, instante: datetime, *,
                                   producto_id: int = None) -> list:
        """Núcleo de `traslado_entrante_vivo`: `[(producto_id, descripción)]` de
        los traslados que entraban a `bodega` en `instante`, para un producto o
        —con `producto_id=None`— para todos (lo usa el generador en lote).

        Una sola consulta sirve a los dos: la del ajuste, juzgada contra el
        instante del conteo, y la del generador, juzgada contra ahora.
        """
        from datetime import timedelta
        from sqlalchemy import or_
        from app.models.traslado import (EstadoTraslado, ItemSolicitudTraslado,
                                         SolicitudTraslado)
        from app.utils.fecha import dia_operativo_de, inicio_del_dia_utc

        desde = inicio_del_dia_utc(
            dia_operativo_de(instante)
            - timedelta(days=ConteoService.DIAS_OPERATIVOS_RECEPCION_RECIENTE))
        q = (db.session.query(SolicitudTraslado, ItemSolicitudTraslado.producto_id)
             .join(ItemSolicitudTraslado,
                   ItemSolicitudTraslado.solicitud_id == SolicitudTraslado.id)
             .filter(
                 SolicitudTraslado.bodega_destino_siesa == bodega,
                 SolicitudTraslado.estado.in_(
                     [EstadoTraslado.EN_TRANSITO, EstadoTraslado.ENTREGADA]),
                 # Despachado antes del conteo (sin fecha: no se sabe → vivo).
                 or_(SolicitudTraslado.fecha_despacho.is_(None),
                     SolicitudTraslado.fecha_despacho <= instante),
                 # Y no recibido antes de la ventana.
                 or_(SolicitudTraslado.fecha_entrega.is_(None),
                     SolicitudTraslado.fecha_entrega >= desde),
             ))
        if producto_id is not None:
            q = q.filter(ItemSolicitudTraslado.producto_id == producto_id)
        partes, vistos = [], set()
        for s, pid in q.order_by(SolicitudTraslado.id).all():
            if (s.id, pid) in vistos:
                continue
            vistos.add((s.id, pid))
            if s.estado == EstadoTraslado.EN_TRANSITO:
                texto = f'{s.codigo} en tránsito desde {s.bodega_origen_siesa}'
            elif s.fecha_entrega is None:
                texto = f'{s.codigo} entregado sin fecha de entrega registrada'
            else:
                texto = (f'{s.codigo} recibido el '
                         f'{dia_operativo_de(s.fecha_entrega).isoformat()}')
            partes.append((pid, texto))
        return partes

    # ── Mercancía en proceso (2026-09-23) ────────────────────────────────────
    #
    # El conteo compara el estante contra el teórico de Siesa. Entre que una
    # operación del WMS mueve mercancía físicamente y que su documento entra a
    # Siesa, los dos miden cosas distintas. Si el conteo se ajusta en ese
    # intervalo, el documento que llega después vuelve a mover lo mismo: doble
    # descuento (o doble entrada).
    #
    # **Qué NO está acá, y por qué** (verificado en el código, no supuesto):
    #
    # - Reposición RESERVA → PICKING: mueve entre zonas de la MISMA bodega Siesa
    #   y ya no manda nada a Siesa (`reposicion_service.confirmar_reposicion`,
    #   docstring: «100% WMS — nunca toca Siesa»). El teórico es por
    #   ítem × bodega (`_fila_invfecha`), así que su total no cambia.
    # - `TareaDevolucion` (logística inversa vieja): DEPRECATED desde el
    #   2026-07-28, sin escritor vivo (`devolucion_service.py`, encabezado).
    # - Avería que nunca se encoló a Siesa (almacén de otra bodega, producto sin
    #   código Siesa — `siesa_job_service.encolar_traslado_averias` la declara en
    #   el log y NO emite): ningún documento del WMS va a mover a Siesa después,
    #   así que no hay doble movimiento que evitar. La del producto sin código
    #   igual no ajusta nunca: sin código no hay foto.
    # - Traslado ENTRANTE: tiene su propio caso (`traslado_entrante_vivo`).
    #
    # Todas se juzgan contra un INSTANTE: el del conteo (`foto_siesa_at`) para
    # el ajuste, ahora para el generador. Un proceso que terminó antes del
    # instante ya estaba en el estante (o fuera) y en Siesa; uno que empezó
    # después no afectó lo contado. El estado que se mira es el de hoy (el WMS
    # no guarda historia de estados), acotado por las fechas que sí guarda.

    #: Cuántos documentos se nombran en el mensaje. El resto se cuenta: un
    #: mensaje de bloqueo de veinte renglones no lo lee nadie.
    MAX_PROCESOS_EN_MENSAJE = 5

    @staticmethod
    def procesos_en_curso(almacen_id: int, instante: datetime = None, *,
                          producto_id: int = None) -> list:
        """**El núcleo.** Mercancía que en `instante` ya se había movido
        físicamente en el almacén y cuyo documento todavía no había entrado a
        Siesa. `instante=None` es ahora; `producto_id=None` es todo el almacén
        (el generador en lote). Devuelve una lista de::

            {'producto_id', 'clase', 'documento', 'detalle', 'accion',
             'sin_fecha'}

        `clase`: VENTA · TRASLADO_SALIENTE · RECEPCION · AVERIA ·
        DEVOLUCION_CLIENTE. `sin_fecha=True` cuando el proceso cuenta como vivo
        porque algún instante no está registrado (Regla 0): el mensaje nombra
        el documento para que alguien lo corrija.

        Lo usan `mercancia_en_proceso` (el ajuste, contra el instante del
        conteo) y las dos variantes del generador. Una política, una función.
        """
        if not almacen_id:
            return []
        if instante is None:
            instante = datetime.utcnow()
        return (ConteoService._en_proceso_por_picking(almacen_id, instante, producto_id)
                + ConteoService._en_proceso_por_recepcion(almacen_id, instante, producto_id)
                + ConteoService._en_proceso_por_averia(almacen_id, instante, producto_id)
                + ConteoService._en_proceso_por_devolucion(almacen_id, instante, producto_id))

    @staticmethod
    def _en_proceso_por_picking(almacen_id, instante, producto_id) -> list:
        """VENTA y TRASLADO_SALIENTE: recogido del estante y sin salida en Siesa.

        Una tarea de picking sacó mercancía del estante si está EN_PROCESO o
        tiene `cantidad_recogida > 0` (COMPLETADO, el short-pick BLOQUEADO, o
        CANCELADO después de una auditoría). Lo que empezó a recoger antes del
        instante (`fecha_inicio`) sigue «en proceso» hasta que Siesa registra
        la salida de SU documento:

        - **Pedido**: la remisión 142945, que descarga el inventario. El WMS la
          marca en `TareaPacking.siesa_triggered_at`
          (`DespachoParialService._persistir_resultado`) — después de la FE, o
          sea un poco tarde: el error es hacia el lado conservador.
        - **Traslado**: la salida 173076/174930 (`siesa_salida_consec`),
          fechada con `fecha_despacho`.

        La salida tiene que caer entre el inicio del picking y el instante: una
        remisión anterior al picking es de otra tanda y no lo cubre.

        Sin `fecha_inicio`, o con un empaque cancelado sin remisión, no hay
        forma de ubicar en el tiempo cuándo volvió la mercancía (si volvió):
        cuenta como vivo y lo dice.
        """
        from sqlalchemy import and_, exists, or_
        from app.models.packing import EstadoPacking, TareaPacking
        from app.models.picking import EstadoPicking, TareaPicking as T
        from app.models.traslado import SolicitudTraslado

        remitido = exists().where(and_(
            TareaPacking.numero_pedido_siesa == T.referencia_documento,
            TareaPacking.siesa_triggered.is_(True),
            TareaPacking.siesa_triggered_at.isnot(None),
            TareaPacking.siesa_triggered_at >= T.fecha_inicio,
            TareaPacking.siesa_triggered_at <= instante))
        salio = exists().where(and_(
            SolicitudTraslado.codigo == T.referencia_documento,
            SolicitudTraslado.siesa_salida_consec.isnot(None),
            SolicitudTraslado.fecha_despacho.isnot(None),
            SolicitudTraslado.fecha_despacho >= T.fecha_inicio,
            SolicitudTraslado.fecha_despacho <= instante))
        es_traslado = T.tipo_documento == 'TRASLADO'
        es_pedido = or_(T.tipo_documento.is_(None), T.tipo_documento != 'TRASLADO')
        q = T.query.filter(
            T.almacen_id == almacen_id,
            T.estado != EstadoPicking.PENDIENTE,
            or_(T.estado == EstadoPicking.EN_PROCESO, T.cantidad_recogida > 0),
            or_(T.fecha_inicio.is_(None), T.fecha_inicio <= instante),
            or_(and_(es_traslado, ~salio), and_(es_pedido, ~remitido)),
        )
        if producto_id is not None:
            q = q.filter(T.producto_id == producto_id)
        tareas = q.order_by(T.id).all()
        if not tareas:
            return []

        # Solo para el texto: cómo quedó el empaque de cada pedido vivo.
        pedidos = {t.referencia_documento for t in tareas
                   if t.tipo_documento != 'TRASLADO' and t.referencia_documento}
        empaques = {}
        if pedidos:
            for p in TareaPacking.query.filter(
                    TareaPacking.numero_pedido_siesa.in_(pedidos)).all():
                empaques.setdefault(p.numero_pedido_siesa, []).append(p)

        grupos = {}
        for t in tareas:
            clase = 'TRASLADO_SALIENTE' if t.tipo_documento == 'TRASLADO' else 'VENTA'
            grupos.setdefault((t.producto_id, clase, t.referencia_documento), []).append(t)

        hallazgos = []
        for (pid, clase, ref), ts in grupos.items():
            sin_inicio = [t.codigo for t in ts if t.fecha_inicio is None]
            en_curso = any(t.estado == EstadoPicking.EN_PROCESO for t in ts)
            accion_picking = 'en picking' if en_curso else 'recogido en picking'
            sin_fecha = bool(sin_inicio)
            if not ref:
                detalle = (f'la tarea de picking {ts[0].codigo} sacó mercancía del '
                           f'estante y no tiene documento asociado')
                accion = (f'Revisar la tarea {ts[0].codigo} con el jefe de bodega: '
                          f'sin documento no hay salida que esperar')
                sin_fecha = True
            elif clase == 'TRASLADO_SALIENTE':
                detalle = (f'traslado {ref} {accion_picking} y sin salida (STS) '
                           f'en Siesa')
                accion = f'Recontar cuando el traslado {ref} salga en Siesa'
            else:
                pks = empaques.get(ref, [])
                rm = next((p for p in pks if p.rm_consec and not p.siesa_triggered), None)
                vivos = [p for p in pks if p.estado != EstadoPacking.CANCELADO]
                if rm is not None:
                    detalle = (f'pedido {ref} {accion_picking}; la remisión '
                               f'{rm.rm_tipo or "RM"}-{rm.rm_consec} existe pero el '
                               f'despacho no terminó de registrarse (sin fecha)')
                    accion = (f'Terminar el despacho del pedido {ref} en la cola de '
                              f'Siesa y recontar')
                    sin_fecha = True
                elif pks and not vivos:
                    detalle = (f'pedido {ref} {accion_picking} y su empaque se '
                               f'canceló sin remisión: no hay registro de que la '
                               f'mercancía haya vuelto al estante')
                    accion = (f'Revisar con el jefe de bodega dónde quedó la '
                              f'mercancía del pedido {ref}')
                    sin_fecha = True
                else:
                    detalle = f'pedido {ref} {accion_picking} y sin remisión en Siesa'
                    accion = f'Recontar cuando se remisione el pedido {ref}'
            if sin_inicio:
                detalle += (f' (picking {", ".join(sin_inicio[:3])} sin fecha de '
                            f'inicio registrada)')
            hallazgos.append({'producto_id': pid, 'clase': clase,
                              'documento': ref or ts[0].codigo, 'detalle': detalle,
                              'accion': accion, 'sin_fecha': sin_fecha})
        return hallazgos

    @staticmethod
    def _en_proceso_por_recepcion(almacen_id, instante, producto_id) -> list:
        """RECEPCION: mercancía recibida físicamente sin la entrada 142948.

        Vivo si la recepción está EN_PROCESO (el camión se está descargando) o
        CONFIRMADA con algo recibido de este SKU, empezó antes del instante
        (`fecha_inicio`, o `fecha_confirmacion` si no la tiene), y la entrada
        no estaba registrada en Siesa en el instante:

        - `siesa_triggered_at` ≤ instante: el pre-flag se marca justo antes del
          POST (`siesa_job_service._ejecutar_con_preflag`);
        - **salvo** que haya un job ENTRADA_OC FALLIDO: con la bandera puesta
          eso es un timeout (Regla 3, «posiblemente enviado») — no se sabe si
          entró;
        - o un job ENTRADA_OC COMPLETADO antes del instante (la corrección [C7]
          de `confirmar_recepcion` marca la bandera sin fecha).

        ABIERTA no cuenta: nadie empezó a descargar. CANCELADA tampoco: la
        mercancía no se recibió.
        """
        from sqlalchemy import and_, exists, func, or_
        from app.models.recepcion import (EstadoRecepcion, ItemRecepcion as I,
                                          RecepcionMercancia as R)
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob

        def _job(estado, *extra):
            return exists().where(and_(
                SiesaJob.tipo == 'ENTRADA_OC',
                SiesaJob.referencia_tipo == 'RecepcionMercancia',
                SiesaJob.referencia_id == R.id,
                SiesaJob.estado == estado, *extra))

        llegada = func.coalesce(R.fecha_inicio, R.fecha_confirmacion)
        entro_a_tiempo = or_(
            and_(R.siesa_triggered.is_(True), R.siesa_triggered_at.isnot(None),
                 R.siesa_triggered_at <= instante,
                 ~_job(EstadoSiesaJob.FALLIDO)),
            _job(EstadoSiesaJob.COMPLETADO, SiesaJob.fecha_completado <= instante),
        )
        q = (db.session.query(R, I.producto_id)
             .join(I, I.recepcion_id == R.id)
             .filter(
                 R.almacen_id == almacen_id,
                 or_(R.estado == EstadoRecepcion.EN_PROCESO,
                     and_(R.estado == EstadoRecepcion.CONFIRMADA,
                          I.cantidad_recibida > 0)),
                 or_(llegada.is_(None), llegada <= instante),
                 ~entro_a_tiempo,
             ))
        if producto_id is not None:
            q = q.filter(I.producto_id == producto_id)
        hallazgos, vistos = [], set()
        for r, pid in q.order_by(R.id).all():
            if (r.id, pid) in vistos:
                continue
            vistos.add((r.id, pid))
            sin_fecha = r.fecha_inicio is None and r.fecha_confirmacion is None
            if r.estado == EstadoRecepcion.EN_PROCESO:
                detalle = f'recepción {r.codigo} (OC {r.numero_oc_siesa}) en curso'
            elif r.siesa_triggered and (r.siesa_triggered_at is None
                                        or r.siesa_triggered_at <= instante):
                detalle = (f'recepción {r.codigo} (OC {r.numero_oc_siesa}) recibida; '
                           f'no se sabe si su entrada llegó a Siesa (resultado '
                           f'desconocido o sin fecha)')
                sin_fecha = True
            else:
                detalle = (f'recepción {r.codigo} (OC {r.numero_oc_siesa}) recibida '
                           f'y sin entrada en Siesa')
            if r.fecha_inicio is None and r.fecha_confirmacion is None:
                detalle += ' (sin fecha de recepción registrada)'
            hallazgos.append({
                'producto_id': pid, 'clase': 'RECEPCION', 'documento': r.codigo,
                'detalle': detalle,
                'accion': (f'Recontar cuando la entrada de la OC {r.numero_oc_siesa} '
                           f'quede registrada en Siesa'),
                'sin_fecha': sin_fecha})
        return hallazgos

    @staticmethod
    def _en_proceso_por_averia(almacen_id, instante, producto_id) -> list:
        """AVERIA: separada del estante y sin traslado a la bodega de averías.

        El ancla es el movimiento que registró la avería, con su job
        TRASLADO_AVERIAS (`encolar_traslado_averias`, el núcleo único del
        encolado: recepción y auditoría de picking). Vivo si el movimiento es
        anterior al instante y ningún job suyo se había COMPLETADO en el
        instante. Un job FALLIDO o DESCARTADO no llegó a Siesa: la mercancía
        sigue contada como vendible allá, y sigue vivo.

        `MovimientoInventario.siesa_sync = 'ENVIADO'` no sirve de fecha: no
        dice cuándo. `fecha_completado` del job sí.
        """
        import json as _json
        from sqlalchemy import and_, exists, or_
        from sqlalchemy.orm import aliased
        from app.models.inventario import MovimientoInventario as M
        from app.models.siesa_job import EstadoSiesaJob, SiesaJob

        _ref = dict(tipo='TRASLADO_AVERIAS', referencia_tipo='movimiento_averia')
        J2 = aliased(SiesaJob)
        enviado = exists().where(and_(
            J2.tipo == _ref['tipo'], J2.referencia_tipo == _ref['referencia_tipo'],
            J2.referencia_id == M.id, J2.estado == EstadoSiesaJob.COMPLETADO,
            J2.fecha_completado.isnot(None), J2.fecha_completado <= instante))
        q = (db.session.query(M, SiesaJob)
             .join(SiesaJob, and_(SiesaJob.referencia_id == M.id,
                                  SiesaJob.tipo == _ref['tipo'],
                                  SiesaJob.referencia_tipo == _ref['referencia_tipo']))
             .filter(M.almacen_id == almacen_id,
                     or_(M.fecha.is_(None), M.fecha <= instante),
                     ~enviado))
        if producto_id is not None:
            q = q.filter(M.producto_id == producto_id)
        por_mov = {}
        for m, j in q.order_by(M.id, SiesaJob.id).all():
            por_mov.setdefault(m.id, (m, []))[1].append(j)
        hallazgos = []
        for m, jobs in por_mov.values():
            ultimo = jobs[-1]
            try:
                referencia = (_json.loads(ultimo.payload or '{}').get('referencia')
                              or m.motivo or f'movimiento {m.id}')
            except (TypeError, ValueError):
                referencia = m.motivo or f'movimiento {m.id}'
            if ultimo.estado in (EstadoSiesaJob.FALLIDO, EstadoSiesaJob.DESCARTADO):
                estado = 'falló' if ultimo.estado == EstadoSiesaJob.FALLIDO else 'se descartó'
                accion = ('Reintentar el traslado a averías en la cola de Siesa (o '
                          'registrarlo en Siesa a mano) y recontar')
                detalle = (f'avería separada del estante ({referencia}) y su traslado '
                           f'a averías en Siesa {estado}')
            else:
                accion = ('Recontar cuando el traslado a averías quede registrado '
                          'en Siesa')
                detalle = (f'avería separada del estante ({referencia}) y sin '
                           f'traslado a averías en Siesa')
            hallazgos.append({
                'producto_id': m.producto_id, 'clase': 'AVERIA',
                'documento': referencia, 'detalle': detalle, 'accion': accion,
                'sin_fecha': m.fecha is None})
        return hallazgos

    @staticmethod
    def _en_proceso_por_devolucion(almacen_id, instante, producto_id) -> list:
        """DEVOLUCION_CLIENTE: la mercancía volvió a la bodega y la nota crédito
        no está aprobada en Siesa.

        La NC se crea en Elaboración (Regla 21) y **reingresa el inventario a
        Siesa al aprobarla** a mano en el escritorio (CLAUDE.md, «Procedimiento
        Manual», paso 5). Hasta entonces la mercancía está en la bodega y Siesa
        no la tiene: un sobrante falso que, ajustado, entra dos veces.

        La aprobación la anota contabilidad en el WMS
        (`DevolucionClienteService.marcar_nc_aprobada` →
        `nc_aprobada_siesa_at`). Vivo si la devolución se confirmó antes del
        instante y no estaba marcada aprobada en el instante. ABIERTA no cuenta:
        la mercancía todavía no entró al estante. Las líneas averiadas también
        cuentan: la NC las reingresa a la bodega igual.
        """
        from sqlalchemy import and_, or_
        from app.models.devolucion_cliente import (DevolucionCliente as D,
                                                   EstadoDevolucionCliente,
                                                   LineaDevolucionCliente as L)
        q = (db.session.query(D, L.producto_id)
             .join(L, L.devolucion_id == D.id)
             .filter(D.almacen_id == almacen_id,
                     D.estado == EstadoDevolucionCliente.CONFIRMADA,
                     L.cantidad_devuelta > 0,
                     or_(D.fecha_confirmacion.is_(None),
                         D.fecha_confirmacion <= instante),
                     ~and_(D.nc_aprobada_siesa.is_(True),
                           D.nc_aprobada_siesa_at.isnot(None),
                           D.nc_aprobada_siesa_at <= instante)))
        if producto_id is not None:
            q = q.filter(L.producto_id == producto_id)
        hallazgos, vistos = [], set()
        for d, pid in q.order_by(D.id).all():
            if (d.id, pid) in vistos:
                continue
            vistos.add((d.id, pid))
            nc = f'NC {d.siesa_nc_consec}' if d.siesa_nc_consec else 'nota crédito'
            detalle = (f'devolución {d.codigo} del pedido {d.numero_pedido_siesa or "?"} '
                       f'ya volvió a la bodega y su {nc} no consta aprobada en Siesa')
            if d.fecha_confirmacion is None:
                detalle += ' (sin fecha de confirmación registrada)'
            hallazgos.append({
                'producto_id': pid, 'clase': 'DEVOLUCION_CLIENTE',
                'documento': d.codigo, 'detalle': detalle,
                'accion': (f'Aprobar la {nc} en Siesa, marcarla aprobada en el WMS '
                           f'(devolución {d.codigo}) y recontar'),
                'sin_fecha': d.fecha_confirmacion is None})
        return hallazgos

    @staticmethod
    def describir_procesos(hallazgos: list):
        """Texto de una lista de `procesos_en_curso`: qué documento y qué hacer.
        `None` si la lista está vacía."""
        if not hallazgos:
            return None
        tope = ConteoService.MAX_PROCESOS_EN_MENSAJE
        partes = [f'{h["detalle"]} → {h["accion"]}' for h in hallazgos[:tope]]
        if len(hallazgos) > tope:
            partes.append(f'y {len(hallazgos) - tope} documento(s) más')
        return '; '.join(partes)

    @staticmethod
    def mercancia_en_proceso(sesion: SesionConteo):
        """¿Había mercancía de este SKU en proceso en el instante del conteo?
        Descripción legible (documento y qué hacer), o `None`.

        Caso 7 de `motivo_bloqueo_ajuste`. Se juzga contra `foto_siesa_at`,
        igual que `traslado_entrante_vivo`: la aprobación puede ser días
        después, y el delta se fijó al contar. Sin foto del cierre devuelve
        `None`: esa sesión ya está bloqueada por no tener foto.
        """
        if sesion.foto_siesa_at is None or not sesion.producto_id or not sesion.almacen_id:
            return None
        return ConteoService.describir_procesos(ConteoService.procesos_en_curso(
            sesion.almacen_id, sesion.foto_siesa_at, producto_id=sesion.producto_id))

    @staticmethod
    def _hallazgos_para_generador(almacen_id: int, instante: datetime,
                                  producto_id: int = None) -> list:
        """Lo del núcleo más los traslados entrantes, que para decidir si
        conviene contar AHORA son lo mismo: mercancía que está llegando."""
        hallazgos = ConteoService.procesos_en_curso(almacen_id, instante,
                                                    producto_id=producto_id)
        from app.models.almacen import Almacen as _Alm
        alm = db.session.get(_Alm, almacen_id) if almacen_id else None
        bodega = alm.bodega_siesa_id if alm else None
        if bodega:
            for pid, texto in ConteoService._traslados_entrantes_vivos(
                    bodega, instante, producto_id=producto_id):
                hallazgos.append({
                    'producto_id': pid, 'clase': 'TRASLADO_ENTRANTE',
                    'documento': texto.split(' ', 1)[0],
                    'detalle': f'traslado {texto}',
                    'accion': 'Contar cuando el traslado esté recibido y guardado',
                    'sin_fecha': 'sin fecha' in texto})
        return hallazgos

    @staticmethod
    def mercancia_en_proceso_ahora(producto_id: int, almacen_id: int):
        """Para el GENERADOR de conteos: ¿este SKU × almacén tiene mercancía en
        proceso AHORA? Descripción legible, o `None`.

        Mismo núcleo que el ajuste (`procesos_en_curso`), juzgado contra ahora,
        más los traslados entrantes. Contar un SKU en ese estado produce un
        conteo que no se va a poder ajustar.
        """
        return ConteoService.describir_procesos(ConteoService._hallazgos_para_generador(
            almacen_id, datetime.utcnow(), producto_id=producto_id))

    @staticmethod
    def hallazgos_que_sacan_del_plan(almacen_id: int, instante: datetime = None) -> list:
        """Los hallazgos crudos (con `sin_fecha`, `documento`, `accion`) por
        los que el generador deja un SKU fuera del plan en `instante` (ahora
        por defecto). Es exactamente lo que filtra `conteo_politica.
        filtrar_elegibles` —vía `productos_con_mercancia_en_proceso`—, sin
        agrupar: el tablero del líder necesita el documento para decir cuál
        cerrar. Una política, una función."""
        if instante is None:
            instante = datetime.utcnow()
        return ConteoService._hallazgos_para_generador(almacen_id, instante)

    @staticmethod
    def productos_con_mercancia_en_proceso(almacen_id: int, instante: datetime = None) -> dict:
        """Variante EN LOTE para el generador: `{producto_id: descripción}` de
        todos los SKU del almacén con mercancía en proceso en `instante`
        (ahora por defecto). Las mismas consultas, sin filtro de producto: un
        barrido por almacén en vez de una ronda por SKU."""
        if instante is None:
            instante = datetime.utcnow()
        por_producto = {}
        for h in ConteoService.hallazgos_que_sacan_del_plan(almacen_id, instante):
            por_producto.setdefault(h['producto_id'], []).append(h)
        return {pid: ConteoService.describir_procesos(hs)
                for pid, hs in por_producto.items()}

    @staticmethod
    def _ids_de_la_cadena(sesion: SesionConteo) -> set:
        """Ids de toda la cadena de `sesion`: su raíz y los descendientes."""
        raiz = sesion
        while raiz.es_segundo_conteo and raiz.sesion_origen_id:
            origen = db.session.get(SesionConteo, raiz.sesion_origen_id)
            if origen is None:
                break
            raiz = origen
        ids, nodo = set(), raiz
        while nodo is not None:
            ids.add(nodo.id)
            nodo = nodo.hijo_conteo
        return ids

    @staticmethod
    def observacion_que_la_vuelve_vieja(sesion: SesionConteo):
        """¿Otra cadena del mismo hueco dejó vieja la foto de esta? Descripción,
        o `None`.

        El ajuste es un delta fijado contra la foto de Siesa del conteo. Ese
        delta sigue valiendo mientras Siesa no haya recibido OTRO ajuste del
        mismo hueco que la foto no vio. Tres formas de que eso pase, todas
        desde otra cadena (la propia no cuenta: su observación es la misma):

        - **Otro conteo posterior** del hueco, con resultado: la diferencia la
          midió él también, y si los dos la ajustan, se descuenta dos veces.
        - **Otro ajuste en vuelo** (AJUSTANDO): no se sabe si ya llegó a Siesa
          antes o después de esta foto.
        - **Otro ajuste aceptado por Siesa DESPUÉS de esta foto**: la foto no lo
          incluye, así que esta diferencia ya está corregida.

        Es el cierre del doble ajuste del mismo hueco (2026-09-23): una cadena
        esperando al supervisor en DESCUADRE, y otra abierta encima que la
        ajustaba sola. Va acá, en la política de «puede salir este ajuste», y
        no solo en el generador, porque son varias las puertas que abren
        cadenas.
        """
        if sesion.foto_siesa_at is None or not sesion.producto_id:
            return None
        from sqlalchemy import and_, or_
        propias = ConteoService._ids_de_la_cadena(sesion)
        otra = (SesionConteo.query
                .filter(
                    SesionConteo.producto_id == sesion.producto_id,
                    SesionConteo.ubicacion_id == sesion.ubicacion_id,
                    ~SesionConteo.id.in_(propias),
                    or_(
                        and_(SesionConteo.foto_siesa_at > sesion.foto_siesa_at,
                             SesionConteo.cantidad_fisica.isnot(None),
                             SesionConteo.estado != EstadoConteo.CANCELADO),
                        SesionConteo.estado == EstadoConteo.AJUSTANDO,
                        and_(SesionConteo.estado == EstadoConteo.AJUSTADO,
                             SesionConteo.fecha_cierre > sesion.foto_siesa_at),
                    ),
                )
                .order_by(SesionConteo.id.desc())
                .first())
        if otra is None:
            return None
        if otra.estado == EstadoConteo.AJUSTANDO:
            return f'el conteo {otra.codigo} del mismo hueco tiene un ajuste en camino a Siesa'
        if otra.estado == EstadoConteo.AJUSTADO and (
                otra.foto_siesa_at is None or otra.foto_siesa_at <= sesion.foto_siesa_at):
            return (f'el conteo {otra.codigo} del mismo hueco ya se ajustó en Siesa '
                    f'después de esta foto')
        return f'el conteo {otra.codigo} del mismo hueco es posterior a éste'

    @staticmethod
    def motivo_bloqueo_ajuste(sesion: SesionConteo, *, exige_foto_inicio: bool = True):
        """Por qué el ajuste de esta sesión NO puede salir a Siesa, o `None`.

        La usan el auto-ajuste de CC1 == CC2, la aprobación del supervisor, la
        auditoría de picking y la pantalla (`to_dict`). Una regla, una función:
        si la pantalla la reimplementara, un día diría «aprobable» sobre algo
        que el servicio niega.

        `exige_foto_inicio=False` solo lo pasa quien no tiene apertura que
        fotografiar — hoy únicamente `ajustar_desde_auditoria_picking`, y está
        declarado con su motivo en el trinquete
        (`tests/test_conteo_ventas_durante_conteo.py::AJUSTES_SIN_FOTO_DE_INICIO`).

        Siete casos, todos Regla 0:

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
        3. **Sin foto de la apertura** (2026-09-23). No se sabe si Siesa se
           movió mientras se contaba. Siesa no respondió al abrir la tarea, o
           la sesión es anterior a la columna (m029): las dos se tratan igual.
        4. **Siesa se movió entre la apertura y el cierre.** `registrar_conteo`
           ya lo manda a recontar y nunca lo deja llegar acá; esto es la
           defensa para cualquier otro camino que escriba una sesión.
        5. **Un traslado entrante vivo** para esta bodega y este SKU en el
           instante del conteo (`traslado_entrante_vivo`).
        6. **Otra cadena del mismo hueco dejó vieja esta foto** (2026-09-23):
           un conteo posterior, un ajuste en vuelo, o uno aceptado por Siesa
           después de esta foto (`observacion_que_la_vuelve_vieja`).
        7. **Mercancía en proceso** (2026-09-23): en el instante del conteo
           había mercancía de este SKU que ya se había movido físicamente y
           cuyo documento todavía no estaba en Siesa — recogida y sin remisión,
           recogida para un traslado y sin STS, recibida y sin EntradaOC,
           averiada y sin traslado a averías, devuelta por el cliente y con la
           NC sin aprobar (`mercancia_en_proceso`). El documento que entra
           después vuelve a mover lo mismo que el ajuste.
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
        if exige_foto_inicio and ConteoService._conteo_sin_sus_dos_fotos(sesion):
            return (
                'El conteo no tiene foto de Siesa de la APERTURA de la tarea (Siesa '
                'no respondió al abrirla, o la sesión es anterior a esa foto). Sin '
                'ella no se sabe si hubo ventas mientras se contaba, así que el '
                'conteo no se convierte en ajuste. Recontar — un ajuste de '
                'inventario siempre puede esperar.'
            )
        movimiento = ConteoService.movimiento_durante_conteo(sesion)
        if movimiento:
            return (
                f'Siesa se movió mientras se contaba ({movimiento}): el físico y la '
                f'foto miden instantes distintos. Recontar.'
            )
        vieja = ConteoService.observacion_que_la_vuelve_vieja(sesion)
        if vieja:
            return (
                f'Este conteo quedó viejo: {vieja}. Ajustarlo descontaría la misma '
                f'diferencia dos veces en Siesa. Se decide sobre el conteo más '
                f'reciente del hueco; éste se cancela.'
            )
        traslado = ConteoService.traslado_entrante_vivo(sesion)
        if traslado:
            return (
                f'Había un traslado entrando a esta bodega con este producto cuando '
                f'se contó ({traslado}). La mercancía puede estar en camino o en '
                f'cajas sin abrir, y el conteo saldría con un faltante falso. '
                f'Recontar cuando el traslado esté recibido y guardado en el '
                f'estante: un conteo hecho '
                f'{ConteoService.DIAS_OPERATIVOS_RECEPCION_RECIENTE + 1} días '
                f'operativos después de la recepción ya no lo cuenta.'
            )
        en_proceso = ConteoService.mercancia_en_proceso(sesion)
        if en_proceso:
            return (
                f'Había mercancía de este producto en proceso que Siesa todavía no '
                f'había registrado cuando se contó: {en_proceso}. Mientras ese '
                f'documento no entre, el estante y Siesa no miden lo mismo, y '
                f'ajustar ahora movería esa mercancía dos veces en Siesa.'
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
    def _exigir_raiz_para_ajustar(sesion: SesionConteo) -> None:
        """Un ajuste sale SOLO de la raíz de la cadena (el CC1).

        El CC2 y el CC3 que resuelven una cadena quedan en DESCUADRE para
        siempre, con su propia `cantidad_fisica`, y su observación ya se copió a
        la raíz (`_copiar_observacion`), que es de donde sale el ajuste. La
        idempotencia de los jobs es por sesión (`ADJ-<id>`), así que aprobar el
        hijo encolaba un SEGUNDO AJUSTE_CONTEO con el mismo delta: el mismo
        faltante descontado dos veces en Siesa. Solo la pantalla lo evitaba,
        porque no le pinta el botón a los hijos; `PUT /api/conteo/<id>/ajustar`
        con el id del hijo lo hacía (2026-09-23). La guarda va en el servicio:
        protege la operación, no una puerta.
        """
        if sesion.es_segundo_conteo:
            raise ValueError(
                f'{sesion.codigo} es un conteo de verificación (CC2/CC3): el '
                'ajuste de la cadena se aprueba sobre el primer conteo, que ya '
                'tiene su resultado. Aprobarlo acá lo enviaría dos veces a Siesa.'
            )

    @staticmethod
    def _encolar_ajuste_fisico(sesion: SesionConteo, aprobador_id: int = None, *,
                               exige_foto_inicio: bool = True) -> None:
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

        ConteoService._exigir_raiz_para_ajustar(sesion)

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
        bloqueo = ConteoService.motivo_bloqueo_ajuste(
            sesion, exige_foto_inicio=exige_foto_inicio)
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
        # **Sin aprobador = automático**, y ningún automático pasa el tope en
        # pesos ni sale sin costo. La guarda va acá, en el único sitio que arma
        # el job: protege toda puerta automática, la de hoy y la que venga.
        if aprobador_id is None:
            no_sale_solo = ConteoService.motivo_no_sale_solo(sesion)
            if no_sale_solo:
                raise ValueError(
                    f'Ajuste automático de {sesion.producto_codigo_siesa} en '
                    f'{bodega_siesa} NO enviado: {no_sale_solo["mensaje"]}.')
        else:
            # **Con firma, la firma tiene que poder aprobar ESE monto.** Misma
            # regla que `confirmar_ajuste`, repetida acá como defensa del único
            # sitio que arma el job: la auditoría de picking la esquivaba
            # (firmaba un jefe de almacén cualquier monto), y la próxima puerta
            # que firme no tiene que acordarse de llamarla.
            from app.models.usuario import Usuario
            no_puede = ConteoService.motivo_no_puede_aprobar(
                db.session.get(Usuario, aprobador_id), sesion)
            if no_puede:
                raise PermissionError(no_puede)
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

        # La auditoría no tiene apertura que fotografiar: el supervisor cuenta
        # y resuelve en un solo gesto, y la foto se toma al resolver. Es un
        # hueco declarado —una venta de caja durante ese conteo no se ve— y
        # vive en el inventario del trinquete
        # (`tests/test_conteo_ventas_durante_conteo.py::AJUSTES_SIN_FOTO_DE_INICIO`).
        # El resto de la política —salidas no POS, traslado entrante— sí aplica.
        bloqueo = ConteoService.motivo_bloqueo_ajuste(sesion, exige_foto_inicio=False)
        if bloqueo:
            # Salidas sin confirmar que no son POS, o un traslado entrante
            # vivo: no se sabe dónde está esa mercancía, así que el ajuste no
            # sale. Pero la auditoría
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
        # **Aprobación por valor** (2026-09-23): quien audita firma el ajuste,
        # así que se le exige lo mismo que a quien aprueba un DESCUADRE en
        # `confirmar_ajuste`. Antes esta puerta ajustaba cualquier monto con la
        # firma de un jefe de almacén (la ruta de auditar admite SUPERVISION),
        # y la regla de valor quedaba esquivada. Igual que con un bloqueo, la
        # auditoría SÍ se cierra —no se traba el picking por una firma— y el
        # ajuste queda en DESCUADRE para que lo apruebe quien puede.
        from app.models.usuario import Usuario
        no_puede = ConteoService.motivo_no_puede_aprobar(
            db.session.get(Usuario, aprobador_id), sesion)
        if no_puede:
            logger.warning(
                f'[CONTEO] Auditoría de picking tarea={tarea.id} — '
                f'{producto.codigo_siesa}: ajuste de {diferencia} NO encolado. '
                f'{no_puede} Sesión {sesion.codigo} queda en DESCUADRE.'
            )
            return sesion
        ConteoService._encolar_ajuste_fisico(sesion, aprobador_id=aprobador_id,
                                             exige_foto_inicio=False)
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
            SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True),
        ).first()
        if existente:
            # Si lo que ya existe es un conteo del plan que nadie empezó, pasa a
            # ser la auditoría: el reparto pone los eventos primero
            # (`conteo_politica.orden_de_reparto`), y con un conteo del plan
            # pendiente sobre cada SKU —las 4.825 de abril en NB1— toda
            # auditoría por faltante se habría quedado con la prioridad de su
            # clase, detrás de miles. Mismo precedente que `crear_conteo_manual`
            # con el tipo MANUAL.
            if (existente.estado == EstadoConteo.PENDIENTE
                    and existente.tipo in ('DIARIO_ABC', 'WATCHDOG_ABC')):
                existente.tipo = 'EXCEPCION_PICKING'
                existente.tarea_picking_id = tarea_picking_id
                db.session.flush()
                logger.warning(
                    '[SUPERVISOR_GUARD] %s (conteo del plan pendiente) pasa a auditoría '
                    'por excepción de la tarea_picking #%s', existente.codigo, tarea_picking_id)
                return existente
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
        ConteoService._exigir_raiz_para_ajustar(sesion)

        # **Quién aprueba cuánto** (2026-09-23): la regla vive acá, no en la
        # ruta. Antes la ruta exigía supervisor/admin y el servicio no miraba
        # a nadie: cualquier otra puerta aprobaba sin control.
        from app.models.usuario import Usuario
        no_puede = ConteoService.motivo_no_puede_aprobar(
            db.session.get(Usuario, supervisor_id), sesion)
        if no_puede:
            raise PermissionError(no_puede)

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
                SesionConteo.raiz_con_cadena_viva(incluye_descuadre=False),
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