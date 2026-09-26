"""
Servicio de Picking con lógica FEFO
(First Expired First Out — primero vence, primero sale)
"""
import logging
from datetime import datetime
import uuid
from typing import NamedTuple
from sqlalchemy import case
from app.extensions import db
from app.utils.fecha import ahora_bogota as _ahora_bogota

logger = logging.getLogger(__name__)
from app.models.picking import TareaPicking, EstadoPicking
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.services.bitacora import registrar_accion, motivo_obligatorio, foto


# ═════════════════════════════════════════════════════════════════════════════
# LA POLÍTICA ÚNICA — «¿este stock se puede vender?»
# ═════════════════════════════════════════════════════════════════════════════
#
# Una política, una función (corolario de la Regla 0 de CLAUDE.md). El
# 2026-08-20 esta pregunta estaba escrita tres veces y las tres respondían
# distinto:
#
#   · `calcular_fefo`               → `tipo_zona != 'AVERIAS'`
#   · `consulta_productos_bajo_minimo` → `tipo_zona notin ('AVERIAS',)`
#   · `inventario_siesa_service._cuarentena_wms`
#         → `tipo == 'cuarentena' OR zona == 'CUARENTENA' OR tipo_zona == 'AVERIAS'`
#
# Y el escritor más grande de averías —`devolucion_cliente_service._resolver_ubicacion`,
# que es el VIVO; `devolucion_service` está DEPRECATED y sin callers—
# con `es_averiado=True`— escribía las DOS primeras señales y ninguna de las que
# las dos primeras consultas miran: el bin `AVERIADOS` nacía con el default del
# modelo, `tipo_zona='GENERAL'`. Los dos filtros nuevos quedaron verdes porque
# probaban al OTRO escritor (`layout_service.crear_ubicacion_averias`, que sí
# marca `tipo_zona`), mientras la mercancía devuelta como averiada salía al FEFO
# y tapaba el mínimo del tablero.
#
# ── Por qué UN campo canónico y no la condición de tres ──────────────────────
#
# `tipo_zona` es el campo que Siesa gobierna (API_v2_Ubicaciones), el que el
# sync clasifica, el que el modelo declara `nullable=False` con default, el
# único indexable con una sola condición, y ya es el que usa la dirección
# contraria de esta misma pregunta (`_ubicacion_averias_disponible`, «dame un
# bin de averías»). Una condición de tres campos hace lo opuesto de unificar:
# deja tres maneras válidas de decir lo mismo, así que un cuarto escritor puede
# marcar dos de tres y volver a pasar. Además `zona` y `tipo` son texto libre
# sin catálogo — nada impide que mañana alguien escriba 'Cuarentena'.
#
# El precio del campo único es el backfill de los bins ya creados en producción:
# `migrations/versions/m016_bin_averiados_sin_tipo_zona.py`.
#
# ── Dónde debería vivir ──────────────────────────────────────────────────────
#
# El sitio natural es `app/models/ubicacion.py`, junto a `CODIGO_GENERAL` y a
# `es_picking`/`es_reserva`: es una propiedad de la ubicación, no del picking.
# Ese archivo no es de este agente en esta tanda (2026-08-20) — queda acá, que
# es el consumidor caliente, y MOVERLO ES EL PRÓXIMO PASO, no un pendiente
# cosmético. Al moverlo hay que mover con él el trinquete
# `tests/test_politica_vendible_unica.py`, que apunta a este módulo por nombre.

ZONA_AVERIAS = 'AVERIAS'

# ── La segunda zona que no se vende (2026-09-24, m045devol) ──────────────────
#
# Mercancía que un cliente devolvió y cuya nota crédito **todavía no está
# aprobada en Siesa**. Está en la bodega pero no es de la empresa todavía: la NC
# en Elaboración no reingresa nada en Siesa (Regla 21), así que venderla es
# vender algo que el ERP sigue diciendo que tiene el cliente, y si la NC se anula
# ya se vendió. Al aprobarse la NC, `devolucion_cliente_service.liberar_reingreso`
# la mueve a picking con su movimiento de inventario.
#
# Es la MISMA pregunta que averías («¿se puede vender?»), así que entra a la
# MISMA política y no a una tupla nueva en cada consumidor: FEFO, alerta de
# mínimos, carga inicial, ABC y traslados la heredan sin tocarlos.
#
# `DEVOLUCION` y no `DEVOLUCIONES`: `ubicaciones.tipo_zona` es `String(10)`.
ZONA_DEVOLUCION = 'DEVOLUCION'
ZONAS_NO_VENDIBLES = (ZONA_AVERIAS, ZONA_DEVOLUCION)

# Señales legadas que los dos escritores de devoluciones ya venían poniendo en
# el bin `AVERIADOS`. NO son la política —`tipo_zona` lo es— pero se siguen
# escribiendo porque `inventario_siesa_service._cuarentena_wms` (que no es de
# este agente) mide la cuarentena con un OR sobre las tres: dejar de escribirlas
# cambiaría en silencio la población que ese reporte declara.
ZONA_LEGADA_CUARENTENA = 'CUARENTENA'
TIPO_LEGADO_CUARENTENA = 'cuarentena'


def filtro_ubicacion_vendible():
    """
    Cláusula SQLAlchemy: la ubicación contiene stock que SE PUEDE VENDER.

    Se usa como filtro sobre `Ubicacion` (o sobre cualquier consulta que ya
    tenga el join hecho). Es el complemento exacto de
    `filtro_ubicacion_averias()` — las dos leen el mismo campo, así que no
    pueden divergir.
    """
    return Ubicacion.tipo_zona.notin_(ZONAS_NO_VENDIBLES)


def filtro_ubicacion_averias():
    """
    Cláusula SQLAlchemy: la ubicación es zona de averías (mercancía dañada o en
    revisión, `app/models/ubicacion.py:39`).

    Dirección contraria de la misma pregunta. Existe para que «dame el stock
    averiado» y «excluí el stock averiado» no se escriban con criterios
    distintos, que es como empezó este defecto.
    """
    return Ubicacion.tipo_zona == ZONA_AVERIAS


def es_ubicacion_vendible(ubicacion) -> bool:
    """Versión en Python de `filtro_ubicacion_vendible()`, para objetos ya
    cargados. Misma respuesta que la de SQL, por construcción."""
    return getattr(ubicacion, 'tipo_zona', None) not in ZONAS_NO_VENDIBLES


def campos_ubicacion_devolucion() -> dict:
    """Los campos del bin de devoluciones de cliente con la NC sin aprobar.

    Mismo contrato que `campos_ubicacion_averias`: el escritor
    (`devolucion_cliente_service`) no decide qué hace no vendible a una
    ubicación, lo pide acá. `zona`/`tipo` NO llevan las señales legadas de
    cuarentena: esto no es mercancía dañada, y `_cuarentena_wms` la contaría
    como averías.
    """
    return {
        'tipo_zona': ZONA_DEVOLUCION,
        'zona': ZONA_DEVOLUCION,
        'tipo': 'devolucion',
    }


def campos_ubicacion_averias() -> dict:
    """
    Los campos que un escritor DEBE poner al crear un bin de averías para que la
    política de arriba lo reconozca.

    Existe para cerrar el hueco por el que entró este defecto: el escritor y el
    lector acordaban de palabra (un comentario que decía «el picking filtra tipo
    != cuarentena», un filtro que jamás existió) en vez de compartir código. Con
    esto, agregar un campo a la política lo heredan todos los escritores que la
    usan; el que no la use lo delata el trinquete
    `tests/test_politica_vendible_unica.py`.
    """
    return {
        'tipo_zona': ZONA_AVERIAS,
        'zona': ZONA_LEGADA_CUARENTENA,
        'tipo': TIPO_LEGADO_CUARENTENA,
    }


#: Los motivos con que un picker bloquea su tarea («Reportar problema»).
MOTIVOS_PROBLEMA_PICKING = ('UBICACION_VACIA', 'FALTANTE', 'MERCANCIA_AVERIADA',
                            'PRODUCTO_INCORRECTO')
#: El único motivo que DECLARA la cantidad por sí mismo: «no había nada» es 0.
MOTIVOS_QUE_DECLARAN_CERO = ('UBICACION_VACIA',)


class DeclaracionInvalida(ValueError):
    """Lo que el picker declaró no se puede registrar tal cual (400, no 404).

    Subclase de `ValueError` para que ningún llamador viejo cambie de rama."""


def cantidad_encontrada_declarada(motivo, valor, solicitada) -> int:
    """Cuántas unidades dice el picker que encontró. **Una política, una
    función** (la usan `/api/mobile/reportar-problema` y
    `/api/picking/<id>/reportar-problema`).

    ## Por qué existe (2026-09-25)

    La pantalla mandaba `cantidad_encontrada: 0` en los cuatro botones, y las
    dos rutas hacían `int(data.get('cantidad_encontrada', 0))`. Un picker que
    ya había escaneado 3 de 5 y reportaba «Agotado» quedaba registrado como
    «encontró 0»: las 3 que tenía en la mano no se descontaban del hueco, las 5
    quedaban bloqueadas y la tarea conservaba `cantidad_recogida = 3` de sus
    escaneos. Tres números que no cuadran entre sí, y ninguno era el real.

    Regla 0: **un faltante sin cantidad no se convierte en 0 — se pide.**
    `UBICACION_VACIA` es la única excepción, porque el motivo mismo lo dice
    («no había nada»); y justamente por eso, con una cantidad mayor que cero
    se contradice y se rechaza.
    """
    if motivo not in MOTIVOS_PROBLEMA_PICKING:
        raise DeclaracionInvalida(
            f'Motivo desconocido: {motivo!r}. Válidos: {", ".join(MOTIVOS_PROBLEMA_PICKING)}')
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if motivo in MOTIVOS_QUE_DECLARAN_CERO:
            return 0
        raise DeclaracionInvalida(
            'Falta cuántas unidades encontró. Un faltante sin cantidad no se '
            'registra como cero: indique el número, aunque sea 0.')
    if isinstance(valor, bool):
        raise DeclaracionInvalida('La cantidad encontrada tiene que ser un número')
    try:
        n = int(valor)
    except (TypeError, ValueError):
        raise DeclaracionInvalida(f'La cantidad encontrada no es un número: {valor!r}')
    if isinstance(valor, float) and n != valor:
        raise DeclaracionInvalida('La cantidad encontrada tiene que ser un número entero')
    if n < 0:
        raise DeclaracionInvalida('La cantidad encontrada no puede ser negativa')
    if solicitada is not None and n > int(solicitada):
        raise DeclaracionInvalida(
            f'Declaró {n} encontradas y la tarea pide {solicitada}: si sobra, '
            f'no es un problema de picking')
    if motivo in MOTIVOS_QUE_DECLARAN_CERO and n > 0:
        raise DeclaracionInvalida(
            '«Ubicación vacía» declara que no había nada. Si encontró unidades, '
            'reporte «Agotado» con la cantidad que encontró.')
    return n


class PickingService:

    #: El bin de respaldo, mismo literal que `_UBICACION_AVERIADOS` de
    #: `devolucion_cliente_service`. Se escribe acá porque este módulo es el
    #: canónico de la política y está exento del barrido de literales de zona
    #: (`tests/test_politica_vendible_unica.py`); ponerlo en otro archivo de
    #: `app/` pondría rojo ese trinquete.
    _CODIGO_BIN_AVERIAS_RESPALDO = 'AVERIADOS'

    @staticmethod
    def _destino_averias_garantizado(almacen_id: int, cantidad: int):
        """Destino OBLIGATORIO para mercancía que se declara averiada.

        Devuelve `(ubicacion, degradada)` y **nunca `None`**, que es el punto
        entero de esta función.

        ## Por qué existe

        `auditar_tarea` restaba del origen aunque `_ubicacion_averias_disponible`
        devolviera `None`, y escribía un movimiento que decía «trasladada a zona
        AVERIAS». Sin destino eso no es un traslado: es una **pérdida de
        inventario con un registro que miente**, y salía con HTTP 200. El caso
        se daba justo en el almacén que todavía no armó su zona de averías en el
        layout — o sea, el estado previo a que alguien la arme.

        ## La cascada, y por qué en este orden

        1. **`AVE-*` del layout.** Es la preferencia y no se negocia: tiene
           dirección física, que es lo que permite ir a buscar la caja. La
           decisión es del 2026-09-03 y su motivo está escrito: *«el averiado es
           dinero trazable y muchas veces se devuelve»*.
        2. **El bin `AVERIADOS`, DECLARADO como degradación.** Un destino con
           menos trazabilidad es peor que uno con dirección física, y mejor que
           perder las unidades. El motivo del movimiento dice que se degradó;
           un respaldo silencioso sería la mitad del defecto original.
        3. **Si no existe, se crea.** La asimetría que causaba el defecto era
           que el escritor de devoluciones crea su bin y el de picking solo
           leía. Acá se cierra.

        `_ubicacion_averias_disponible` **no cambia de contrato**: sigue
        significando «un bin del layout con cupo, o `None`». Meterle el respaldo
        adentro borraría la diferencia entre destino correcto y degradado justo
        donde su docstring documenta que excluir `AVERIADOS` fue una decisión.
        """
        ub = PickingService._ubicacion_averias_disponible(almacen_id, cantidad)
        if ub:
            return ub, False

        codigo = PickingService._CODIGO_BIN_AVERIAS_RESPALDO
        ub = Ubicacion.query.filter_by(codigo=codigo,
                                       almacen_id=almacen_id).first()
        if ub:
            # Misma red que el escritor vivo de devoluciones: si el bin quedó
            # marcado vendible (el defecto que `m016` vino a corregir), se
            # repara antes de meterle mercancía dañada.
            if es_ubicacion_vendible(ub):
                for campo, valor in campos_ubicacion_averias().items():
                    setattr(ub, campo, valor)
                logger.warning(
                    '[AUDITORIA] %s estaba marcada como vendible — corregida '
                    'antes de recibir mercancía averiada', ub.codigo)
            return ub, True

        # `flush()` desnudo, SIN el `try/except: rollback()` del escritor vivo:
        # ese rollback es de sesión completa y acá se llevaría por delante el
        # `reg.bloqueado` ya descongelado, dejando una tarea CANCELADA con el
        # inventario todavía congelado. Si el INSERT choca, que la excepción
        # suba y revierta la transacción entera — estado consistente, aunque feo.
        ub = Ubicacion(codigo=codigo, almacen_id=almacen_id, activo=True,
                       **campos_ubicacion_averias())
        db.session.add(ub)
        db.session.flush()
        logger.warning(
            '[AUDITORIA] almacén %s no tiene ninguna ubicación AVERIAS del '
            'layout — se creó el bin de respaldo %s. Armá la zona de averías '
            'en Layout para que la avería tenga dirección física.',
            almacen_id, codigo)
        return ub, True

    @staticmethod
    def _codigo_tarea() -> str:
        return f'PICK-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{str(uuid.uuid4())[:6].upper()}'

    @staticmethod
    def _cantidad_congelada(tarea, cantidad_faltante: int) -> int:
        """Unidades que esta tarea BLOQUEADA tiene congeladas en `bloqueado`.

        Una tarea con `bloqueo_sin_stock` nació bloqueada sin congelar nada (no
        había stock): restarle su faltante a `bloqueado` le quitaría unidades a
        otras tareas de la misma ubicación. Una política, tres sitios
        (cancelar, reabrir, auditar).
        """
        return 0 if tarea.bloqueo_sin_stock else cantidad_faltante

    @staticmethod
    def _ubicacion_averias_disponible(almacen_id: int, cantidad_necesaria: int):
        """
        Recorre las ubicaciones AVERIAS del almacén en orden de llenado
        (la más antigua primero) y devuelve la primera con capacidad
        disponible. Si ninguna tiene capacidad_maxima configurada, no bloquea
        el registro de la avería — usa la primera del orden como respaldo.

        Orden por `fecha_creacion`, no por el código: desde que AVERIAS se crea
        con dirección física real (`layout_service.crear_cuerpo()`) el código ya
        no trae un número de serie al final (era 'AVE1', 'AVE2'...; ahora es
        'AVE-A1-C01-E01-H01' o 'AVE-A1-EST01') — parsear un sufijo numérico que
        ya no existe habría dejado esta función sin orden real.

        ── El bin de devoluciones queda FUERA, y es un defecto del merge ──

        `AVERIADOS` es el bin donde `devolucion_cliente_service` deja lo que
        vuelve roto de un cliente. Hasta el merge del 2026-09-11 no era
        candidato acá **por accidente**: no estaba marcado como AVERIAS. La
        migración `m016` lo marcó, y el orden por `fecha_creacion` lo pone
        primero (existe desde mucho antes que los bins de cuerpo) — y como se
        creó sin `capacidad_maxima`, el `return` temprano lo entregaría
        **incondicionalmente**.

        O sea: la avería que un operario reporta en una auditoría de picking
        habría empezado a caer en el bin de devoluciones de cliente, en
        silencio y sin que nadie lo pidiera. El defecto **no está en ninguno de
        los dos lados**: aparece al juntarlos, porque cada uno quitó la mitad
        de la protección del otro.

        Se excluye por código, que es lo que conserva el destino de hoy. Es la
        misma intención que la rama de flota documentaba con su orden por
        regex, re-implementada sobre el cimiento nuevo. **Si se decide que una
        avería de picking SÍ puede caer ahí, se quita esta línea** — pero que
        sea una decisión, no un efecto colateral.
        """
        candidatas = (
            Ubicacion.query.filter(
                Ubicacion.almacen_id == almacen_id,
                filtro_ubicacion_averias(),
                Ubicacion.activo.is_(True),
                Ubicacion.codigo != 'AVERIADOS',
            )
            .order_by(Ubicacion.fecha_creacion.asc())
            .all()
        )

        for ub in candidatas:
            if ub.capacidad_maxima is None:
                return ub
            ocupado = db.session.query(
                db.func.coalesce(db.func.sum(UbicacionProducto.cantidad), 0)
            ).filter_by(ubicacion_id=ub.id).scalar()
            if ocupado + cantidad_necesaria <= ub.capacidad_maxima:
                return ub

        return candidatas[0] if candidatas else None

    @staticmethod
    def calcular_fefo(producto_id: int, cantidad_necesaria: int, almacen_id: int):
        """
        Calcula qué ubicaciones usar según FEFO.
        Prioriza lotes que vencen primero.
        Retorna lista de {ubicacion, cantidad, lote, fecha_vencimiento}

        AVERIAS queda FUERA — no es una prioridad más baja, es stock que no
        existe para un pedido. Quién es AVERIAS lo decide
        `filtro_ubicacion_vendible()`, la política única de este módulo, no un
        literal escrito acá: la copia de esa condición es exactamente lo que
        dejó pasar la mercancía del bin `AVERIADOS` de devoluciones.
        `app/models/ubicacion.py:39` la define como
        «productos dañados/en revisión», y ahí es donde `auditar_tarea` deja lo
        que un operario reportó roto. Ese stock no se mueve, así que su
        `fecha_ingreso` es casi siempre la más vieja del producto: sin excluirla
        **ganaba el FEFO**, se generaba una TareaPicking contra ella y la
        mercancía averiada salía hacia un cliente. Dejarla como última opción
        tampoco sirve: como respaldo taparía un faltante que hay que declarar
        (Regla 0) — que el pedido salga corto lo corrige alguien mañana; una
        caja rota entregada, no.

        Las zonas vendibles conservan su orden anterior sin cambios: ubicaciones
        reales de Layout (PICKING/RESERVA/IMPORTADOS) van antes que GENERAL
        (SIESA-GENERAL, el bucket sin ubicación física del sync de Siesa) — sin
        este criterio, un hueco recién organizado con fecha de ingreso reciente
        siempre pierde contra GENERAL mientras a este le quede stock, aunque sea
        de meses atrás: "antigüedad" no es lo mismo que "ubicación real".
        GENERAL sigue sirviendo de respaldo automático si el hueco real no
        alcanza a cubrir toda la cantidad pedida.

        Cross-Dock (`Ubicacion.tipo == 'cross_dock'`) va PRIMERO, antes que
        cualquier otra cosa — es mercancía que `recepcion_service._decidir_destino`
        ya enrutó ahí precisamente porque existía una TareaPicking PENDIENTE
        para ese producto: existe para cumplir un pedido ya en curso, no para
        quedarse en el estante. Antes de esta regla, `tipo_zona == 'GENERAL'`
        trataba Cross-Dock igual que SIESA-GENERAL y el desempate por
        `fecha_ingreso` casi siempre lo dejaba último — mientras
        SIESA-GENERAL tuviera con qué cubrir la demanda normal (y la Carga
        Inicial lo repone completo en cada sync), Cross-Dock nunca se tocaba:
        verificado en producción, 4,068 unidades en 10 SKUs quietas ahí desde
        marzo/junio sin que ninguna tarea de picking las haya usado.
        """
        _prioridad_zona = case(
            (Ubicacion.tipo == 'cross_dock', 0),
            (Ubicacion.tipo_zona == 'GENERAL', 2),
            else_=1,
        )

        registros = (
            UbicacionProducto.query
            .join(Ubicacion)
            .filter(
                UbicacionProducto.producto_id == producto_id,
                Ubicacion.almacen_id == almacen_id,
                filtro_ubicacion_vendible(),
                UbicacionProducto.cantidad > 0
            )
            .order_by(
                # Ubicaciones reales antes que el bucket GENERAL sin organizar
                _prioridad_zona.asc(),
                # Primero los que tienen fecha de vencimiento (FEFO)
                UbicacionProducto.fecha_vencimiento.asc().nullslast(),
                # Luego por fecha de ingreso (FIFO como fallback)
                UbicacionProducto.fecha_ingreso.asc()
            )
            .all()
        )

        asignaciones = []
        pendiente = cantidad_necesaria

        for reg in registros:
            if pendiente <= 0:
                break

            disponible = reg.cantidad_disponible()
            if disponible <= 0:
                continue

            tomar = min(disponible, pendiente)
            asignaciones.append({
                'ubicacion_id': reg.ubicacion_id,
                'ubicacion_codigo': reg.ubicacion.codigo,
                'producto_id': reg.producto_id,
                'cantidad': tomar,
                'lote': reg.lote,
                'fecha_vencimiento': reg.fecha_vencimiento
            })
            pendiente -= tomar

        return {
            'asignaciones': asignaciones,
            'cantidad_disponible': cantidad_necesaria - pendiente,
            'cantidad_faltante': pendiente,
            'completo': pendiente == 0
        }

    @staticmethod
    def orden_ruta_fisica():
        """
        Orden de caminata física: Pasillo -> Fila -> Cuerpo -> Nivel -> Hueco.

        El pasillo se ordena por (longitud, alfabético) en vez de solo
        alfabético — un ORDER BY ingenuo pondría 'AA' antes que 'Z'
        (comparación de string), cuando físicamente 'AA' es el pasillo 27,
        después de 'Z' (el 26). Longitud primero corrige eso: todos los
        pasillos de una letra ('A'..'Z') ordenan antes que cualquiera de dos
        letras ('AA'..'ZZ'), y dentro del mismo largo el alfabético ya es
        correcto.

        nullslast() en cada eje: ubicaciones sin dirección física (SIESA-
        GENERAL, AVERIAS, o el esquema legado de 'estante') van al final de
        la ruta — no son huecos reales que se puedan visitar en secuencia.

        Reutilizable donde haga falta un orden de recorrido — hoy en
        siguiente_tarea_para() para el dispensador, también aplicable a
        listados de solo lectura si conviene mostrarlos en orden de ruta.
        """
        return (
            db.func.length(Ubicacion.pasillo).asc().nullslast(),
            Ubicacion.pasillo.asc().nullslast(),
            Ubicacion.fila.asc().nullslast(),
            Ubicacion.cuerpo.asc().nullslast(),
            Ubicacion.nivel.asc().nullslast(),
            Ubicacion.hueco.asc().nullslast(),
        )

    @staticmethod
    def _documento_en_curso(operario_id: int) -> str | None:
        """
        referencia_documento (pedido o traslado — comparten esta misma cola)
        que el operario tocó más recientemente, EN_PROCESO o recién
        COMPLETADO. Si todavía le quedan tareas pendientes de ese mismo
        documento, siguiente_tarea_para() se las asigna antes de saltar a
        otro — evita que rebote entre pedidos/traslados distintos.
        """
        ultima = (
            TareaPicking.query
            .filter(
                TareaPicking.operario_id == operario_id,
                TareaPicking.estado.in_([EstadoPicking.EN_PROCESO, EstadoPicking.COMPLETADO]),
                TareaPicking.referencia_documento.isnot(None),
            )
            .order_by(
                db.func.coalesce(TareaPicking.fecha_completado, TareaPicking.fecha_inicio).desc()
            )
            .first()
        )
        return ultima.referencia_documento if ultima else None

    @staticmethod
    def siguiente_tarea_para(operario_id: int):
        """
        Selecciona (con row-lock) la próxima TareaPicking a asignar a un
        operario. Aplica igual a pedidos y traslados — ambos comparten esta
        cola vía referencia_documento.

        Orden de decisión:
          1. Prioridad — estricta, sin cambios de comportamiento: nunca se
             trabaja una prioridad más baja mientras haya una más alta
             pendiente en la cola.
          2. Dentro de esa prioridad máxima: si el operario ya está
             trabajando un documento (_documento_en_curso) y le quedan
             tareas de ese mismo documento en esa prioridad, se las asigna
             antes de saltar a otro.
          3. Orden físico de caminata (orden_ruta_fisica) — las líneas de
             un mismo documento en ubicaciones distintas llegan en secuencia
             de recorrido, no salteadas.
          4. fecha_creacion como último desempate (comportamiento previo).

        Retorna None si no hay tareas pendientes sin operario asignado.
        """
        base = (
            TareaPicking.query
            .join(Ubicacion, TareaPicking.ubicacion_id == Ubicacion.id)
            .filter(
                TareaPicking.estado == EstadoPicking.PENDIENTE,
                TareaPicking.operario_id.is_(None),
            )
        )

        prioridad_maxima = base.with_entities(db.func.max(TareaPicking.prioridad)).scalar()
        if prioridad_maxima is None:
            return None
        base = base.filter(TareaPicking.prioridad == prioridad_maxima)

        orden = PickingService.orden_ruta_fisica() + (TareaPicking.fecha_creacion.asc(),)

        documento_actual = PickingService._documento_en_curso(operario_id)
        if documento_actual:
            candidata = (
                base.filter(TareaPicking.referencia_documento == documento_actual)
                .order_by(*orden)
                .with_for_update(of=TareaPicking, skip_locked=True)
                .first()
            )
            if candidata:
                return candidata

        return (
            base.order_by(*orden)
            .with_for_update(of=TareaPicking, skip_locked=True)
            .first()
        )

    @staticmethod
    def crear_tareas(
        producto_id: int,
        cantidad: int,
        almacen_id: int,
        referencia_documento: str = None,
        tipo_documento: str = None,
        operario_id: int = None,
        prioridad: int = 1,
        bodega_origen_siesa: str = None,
    ):
        """
        Crea tareas de picking usando FEFO.
        Puede generar múltiples tareas si el stock está en varias ubicaciones.
        """
        producto = Producto.query.get(producto_id)
        if not producto:
            raise ValueError(f'Producto {producto_id} no encontrado')

        # ── Reposición Predictiva ──────────────────────────────────────────────
        # Antes de crear las tareas de picking, verificamos si el stock de la
        # zona PICKING es suficiente para toda la demanda. Si no, pre-disparamos
        # TareaReposicion al Abastecedor ANTES de que el picker empiece a caminar.
        try:
            from app.services.ola_predictiva_service import pre_verificar_ola
            pre_verificar_ola(
                items=[{'producto_id': producto_id, 'cantidad': cantidad}],
                almacen_id=almacen_id,
            )
        except Exception as _e:
            import logging as _log
            _log.getLogger(__name__).warning(f'[PICKING] Ola predictiva falló silenciosamente: {_e}')
        # ──────────────────────────────────────────────────────────────────────

        fefo = PickingService.calcular_fefo(producto_id, cantidad, almacen_id)

        if not fefo['completo']:
            raise ValueError(
                f'Stock insuficiente. Disponible: {fefo["cantidad_disponible"]}, '
                f'Solicitado: {cantidad}'
            )

        tareas_creadas = []

        # La clave del pedido (m036fotos): la misma que llevará su packing.
        # Una sola vez por llamada, no por asignación FEFO.
        from app.services.cadena_pedido import clave_de_tarea_picking
        _pedido_clave = clave_de_tarea_picking(
            referencia_documento, tipo_documento, almacen_id)

        for asig in fefo['asignaciones']:
            codigo = PickingService._codigo_tarea()

            tarea = TareaPicking(
                codigo=codigo,
                producto_id=producto_id,
                cantidad_solicitada=asig['cantidad'],
                ubicacion_id=asig['ubicacion_id'],
                almacen_id=almacen_id,
                lote=asig['lote'],
                fecha_vencimiento=asig['fecha_vencimiento'],
                operario_id=operario_id,
                estado=EstadoPicking.PENDIENTE,
                prioridad=prioridad,
                referencia_documento=referencia_documento,
                tipo_documento=tipo_documento,
                bodega_origen_siesa=bodega_origen_siesa,
                pedido_clave=_pedido_clave,
            )

            # Reservar el stock — lock a nivel de fila para evitar doble reserva concurrente
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=asig['ubicacion_id'],
                producto_id=producto_id
            ).with_for_update().first()

            if not reg or reg.cantidad_disponible() < asig['cantidad']:
                db.session.rollback()
                raise ValueError(
                    f'Stock insuficiente al reservar en ubicación {asig["ubicacion_codigo"]}. '
                    f'Otro proceso puede haber tomado el stock — reintente.'
                )
            reg.reservado += asig['cantidad']

            db.session.add(tarea)
            tareas_creadas.append(tarea)

        db.session.commit()
        return tareas_creadas

    @staticmethod
    def confirmar_picking(tarea_id: int, cantidad_recogida: int, usuario_id: int):
        """
        Confirma que el operario recogió la mercancía.
        Descuenta el stock real y libera la reserva.
        """
        # [A16] Row-lock en TareaPicking — serializa confirmaciones concurrentes del mismo tarea_id.
        # Sin este lock, dos workers leen estado=EN_PROCESO simultáneamente, ambos pasan el guard
        # y ambos decrementan el stock → double-decrement silencioso.
        tarea = TareaPicking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == EstadoPicking.COMPLETADO:
            raise ValueError('Tarea ya completada')

        if tarea.estado == EstadoPicking.CANCELADO:
            raise ValueError('Tarea cancelada')

        # Operario solo puede confirmar tareas asignadas a él mismo
        if tarea.operario_id and tarea.operario_id != usuario_id:
            from app.models.usuario import Usuario as _U
            u = _U.query.get(usuario_id)
            es_supervision = u and u.rol in ('admin', 'supervisor', 'jefe_almacen')
            if not es_supervision:
                raise ValueError('No puede confirmar una tarea asignada a otro operario')

        if cantidad_recogida > tarea.cantidad_solicitada:
            raise ValueError('Cantidad recogida supera la solicitada')

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()

        # Traslados: el inventario real vive en Siesa (STS/ETS lo mueve al cerrar packing).
        # Si WMS no tiene reg o tiene menos stock del recogido, dejamos pasar sin bloquear.
        _skip_stock = tarea.tipo_documento == 'TRASLADO' and (
            reg is None or reg.cantidad < cantidad_recogida
        )

        if not _skip_stock:
            if not reg or reg.cantidad < cantidad_recogida:
                raise ValueError('Stock insuficiente en ubicación')

            # Descontar stock real
            saldo_antes = reg.cantidad
            reg.cantidad -= cantidad_recogida
            reg.reservado = max(0, reg.reservado - tarea.cantidad_solicitada)
            reg.row_version += 1

            # Registrar movimiento
            movimiento = MovimientoInventario(
                producto_id=tarea.producto_id,
                ubicacion_id=tarea.ubicacion_id,
                almacen_id=tarea.almacen_id,
                tipo='SALIDA',
                cantidad=cantidad_recogida,
                saldo_antes=saldo_antes,
                saldo_despues=reg.cantidad,
                motivo=f'Picking {tarea.codigo}',
                numero_documento=tarea.referencia_documento,
                usuario_id=usuario_id,
                idempotency_key=f'PICK-{tarea.id}'
            )
            db.session.add(movimiento)

        # Actualizar tarea
        tarea.cantidad_recogida = cantidad_recogida
        tarea.estado = EstadoPicking.COMPLETADO
        tarea.fecha_completado = datetime.utcnow()
        if not tarea.fecha_inicio:
            tarea.fecha_inicio = datetime.utcnow()

        # Capturar antes del commit — expire_on_commit invalida atributos post-commit
        _ref_doc_cp = tarea.referencia_documento
        _tipo_doc_cp = tarea.tipo_documento

        db.session.commit()

        # Auto-sync packing asociado — ajusta cantidad_esperada al recogido real (solo PEDIDO)
        try:
            from app.models.packing import TareaPacking as _TPSync
            from app.services.packing_picking_sync_service import PackingPickingSyncService as _PPSync
            if _ref_doc_cp:
                _sync_pk = _TPSync.query.filter(
                    _TPSync.numero_pedido_siesa == _ref_doc_cp,
                    _TPSync.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                ).first()
                if _sync_pk:
                    _PPSync.sincronizar(_sync_pk.id)
        except Exception as _e_sync:
            logger.warning('[PICKING] Sync packing falló — cantidad_esperada puede estar desactualizada: %s', _e_sync)

        # Auto-trigger confirmar_picking_traslado cuando el último picking TRASLADO se completa.
        # Crea TareaPacking y mueve solicitud EN_PICKING → EN_PACKING para que el empacador la vea.
        if _tipo_doc_cp == 'TRASLADO' and _ref_doc_cp:
            try:
                pendientes = TareaPicking.query.filter_by(
                    referencia_documento=_ref_doc_cp,
                    tipo_documento='TRASLADO',
                ).filter(
                    TareaPicking.estado.in_([EstadoPicking.PENDIENTE, EstadoPicking.EN_PROCESO])
                ).count()
                if pendientes == 0:
                    from app.models.traslado import SolicitudTraslado as _ST, EstadoTraslado as _ET
                    _sol = _ST.query.filter_by(codigo=_ref_doc_cp).first()
                    if _sol and _sol.estado == _ET.EN_PICKING:
                        _picks_ok = TareaPicking.query.filter_by(
                            referencia_documento=_ref_doc_cp,
                            tipo_documento='TRASLADO',
                            estado=EstadoPicking.COMPLETADO,
                        ).all()
                        _recogido = {}
                        for _pk in _picks_ok:
                            _recogido[_pk.producto_id] = (
                                _recogido.get(_pk.producto_id, 0) + (_pk.cantidad_recogida or 0)
                            )
                        _items_conf = [
                            {'id': _it.id, 'cantidad_confirmada': _recogido.get(_it.producto_id, 0)}
                            for _it in _sol.items
                        ]
                        from app.services.traslado_service import TrasladoService as _TS
                        _TS.confirmar_picking_traslado(
                            solicitud_id=_sol.id,
                            usuario_id=usuario_id,
                            items_confirmados=_items_conf,
                        )
                        logger.info('[PICKING] TRASLADO %s: todos los pickings completados → EN_PACKING auto-trigger', _ref_doc_cp)
            except Exception as _e_trs:
                logger.error('[PICKING] Auto-trigger confirmar_picking_traslado falló para %s: %s',
                             _ref_doc_cp, _e_trs, exc_info=True)

        return tarea

    @staticmethod
    def iniciar_picking(tarea_id: int, operario_id: int):
        """Marca la tarea como en proceso."""
        # [A15] Row-lock — evita que dos operarios inicien la misma tarea simultáneamente
        tarea = TareaPicking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado != EstadoPicking.PENDIENTE:
            raise ValueError(f'No se puede iniciar una tarea en estado {tarea.estado}')

        # Impedir que un operario se apropie de una tarea asignada a otro
        if tarea.operario_id and tarea.operario_id != operario_id:
            from app.models.usuario import Usuario as _U
            u = _U.query.get(operario_id)
            es_supervision = u and u.rol in ('admin', 'supervisor', 'jefe_almacen')
            if not es_supervision:
                raise ValueError('Esta tarea ya está asignada a otro operario')

        tarea.estado = EstadoPicking.EN_PROCESO
        tarea.operario_id = operario_id
        tarea.fecha_inicio = datetime.utcnow()
        tarea.cantidad_recogida = 0
        tarea.empaques_escaneados = 0
        db.session.commit()
        return tarea

    @staticmethod
    def _soltar_operario(tarea) -> None:
        """Devuelve la tarea al pool **sin perder quién la tenía**.

        Reabrir, reportar un problema, auditar y el backorder ponen
        `operario_id` en None para que otro la tome. Eso borraba la
        atribución: una tarea bloqueada por faltante ya no decía quién caminó
        hasta el hueco vacío. `operario_id` sigue significando «de quién es
        ahora» (sus consumidores no cambian); la historia queda en
        `ultimo_operario_id`. Única función que suelta al operario — el
        trinquete de `tests/test_bitacora_acciones.py` lo exige.
        """
        if tarea.operario_id:
            tarea.ultimo_operario_id = tarea.operario_id
        tarea.operario_id = None

    @staticmethod
    def cancelar_picking(tarea_id: int, motivo: str = None, usuario_id: int = None):
        """Cancela una tarea y libera la reserva (y el bloqueado si aplica).

        El motivo es obligatorio y queda en la bitácora con quién canceló.
        Antes se recibía y se tiraba: una tarea cancelada no decía por qué.
        """
        motivo = motivo_obligatorio(motivo, 'cancelar una tarea de picking')
        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado == EstadoPicking.COMPLETADO:
            raise ValueError('No se puede cancelar una tarea completada')
        antes = foto(tarea, ['estado', 'operario_id', 'cantidad_solicitada',
                             'cantidad_recogida', 'motivo_bloqueo'])

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()
        if reg:
            reg.reservado = max(0, reg.reservado - tarea.cantidad_solicitada)
            # Si estaba bloqueada, liberar también el inventario congelado
            if tarea.estado == EstadoPicking.BLOQUEADO:
                cantidad_faltante = max(0, tarea.cantidad_solicitada - (tarea.cantidad_recogida or 0))
                reg.bloqueado = max(0, reg.bloqueado - PickingService._cantidad_congelada(tarea, cantidad_faltante))

        tarea.estado = EstadoPicking.CANCELADO
        registrar_accion('CANCELAR', tarea, usuario_id=usuario_id, motivo=motivo,
                         antes=antes, despues={'estado': tarea.estado},
                         entidad_codigo=tarea.codigo)
        db.session.commit()
        return tarea

    # ── Recogido sin despachar: la salida con fecha ─────────────────────────

    @staticmethod
    def _empaque_que_la_explica(referencia):
        """Condición SQL «hay un empaque del pedido `referencia` que explica
        la mercancía recogida»: vivo (no cancelado) o con remisión
        (`siesa_triggered`/`rm_consec`). **Una definición** para la lista de
        «devuelto al estante» y para `reabrir_picking`.
        """
        from sqlalchemy import and_, exists, or_
        from app.models.packing import EstadoPacking, TareaPacking
        from app.services.documento_fiscal import filtro_tiene_documento
        # «Con remisión» es la política única de `documento_fiscal` (incluye
        # la factura y el 142945 enviado sin confirmar).
        return exists().where(and_(
            TareaPacking.numero_pedido_siesa == referencia,
            or_(TareaPacking.estado != EstadoPacking.CANCELADO,
                filtro_tiene_documento(TareaPacking))))

    @staticmethod
    def _filtro_devolvible_al_estante():
        """Condición SQL: «tarea de un pedido cuya mercancía se recogió y no
        salió ni va en camino de salir». **Una definición** para la lista y para
        la declaración.

        - pedido (no traslado: su salida es el STS y tiene otro circuito);
        - picking COMPLETADO con algo recogido, sin regreso ya declarado;
        - **ningún empaque del pedido vivo o con remisión**: todos cancelados
          sin remisión, o ninguno (recogido y nunca empacado). Un empaque vivo
          es mercancía en proceso de salir; uno con remisión (`rm_consec` o
          `siesa_triggered`) ya salió en Siesa — declararla «de vuelta al
          estante» sería inventar un sobrante.
        """
        from sqlalchemy import and_, or_
        T = TareaPicking
        empaque_que_la_explica = PickingService._empaque_que_la_explica(T.referencia_documento)
        return and_(
            or_(T.tipo_documento.is_(None), T.tipo_documento != 'TRASLADO'),
            T.referencia_documento.isnot(None),
            T.estado == EstadoPicking.COMPLETADO,
            T.cantidad_recogida > 0,
            T.devuelto_estante_at.is_(None),
            ~empaque_que_la_explica,
        )

    @staticmethod
    def recogido_sin_despachar(almacen_id: int = None) -> list:
        """Pedidos con mercancía recogida que no salió ni está saliendo: el
        empaque se canceló sin remisión, o nunca se empacó. Uno por pedido ×
        almacén, con sus productos.

        Es la lista donde el líder declara «volvió al estante»
        (`declarar_devuelto_al_estante`). Hasta que lo haga, la guarda de
        mercancía en proceso del conteo (`ConteoService.procesos_en_curso`) no
        tiene fecha que juzgar y deja esos SKU fuera del plan de conteo.
        """
        from app.models.packing import TareaPacking
        q = TareaPicking.query.filter(PickingService._filtro_devolvible_al_estante())
        if almacen_id:
            q = q.filter(TareaPicking.almacen_id == almacen_id)
        tareas = q.order_by(TareaPicking.referencia_documento, TareaPicking.id).all()
        pedidos = {t.referencia_documento for t in tareas}
        empacados = {p.numero_pedido_siesa for p in TareaPacking.query.filter(
            TareaPacking.numero_pedido_siesa.in_(pedidos)).all()} if pedidos else set()
        from app.models.almacen import Almacen
        nombres = {a.id: a.nombre for a in Almacen.query.filter(
            Almacen.id.in_({t.almacen_id for t in tareas})).all()} if tareas else {}
        grupos = {}
        for t in tareas:
            g = grupos.setdefault((t.referencia_documento, t.almacen_id), {
                'pedido': t.referencia_documento,
                'almacen_id': t.almacen_id,
                'almacen_nombre': nombres.get(t.almacen_id),
                'situacion': ('EMPAQUE_CANCELADO' if t.referencia_documento in empacados
                              else 'NUNCA_EMPACADO'),
                'recogido_desde': None,
                'productos': [],
            })
            inicio = t.fecha_completado or t.fecha_inicio
            if inicio and (g['recogido_desde'] is None or inicio.isoformat() < g['recogido_desde']):
                g['recogido_desde'] = inicio.isoformat()
            g['productos'].append({
                'tarea': t.codigo,
                'producto_codigo': t.producto.codigo if t.producto else None,
                'producto_nombre': t.producto.nombre if t.producto else None,
                'cantidad_recogida': t.cantidad_recogida,
                'ubicacion_codigo': t.ubicacion.codigo if t.ubicacion else None,
            })
        return list(grupos.values())

    @staticmethod
    def declarar_devuelto_al_estante(pedido: str, almacen_id: int, usuario_id: int,
                                     nota: str = None) -> dict:
        """El líder declara que la mercancía recogida de `pedido` volvió al
        estante. Fecha cada tarea devolvible del pedido en ese almacén
        (`TareaPicking.devuelto_estante_at`) y hace commit.

        Es la **salida con fecha** que le faltaba a la guarda de mercancía en
        proceso del conteo: desde este instante el SKU deja de estar «en
        proceso» por este pedido; los conteos de ANTES siguen bloqueados (se
        juzga el instante del conteo, no el de ahora).

        **No mueve inventario** — ver `TareaPicking.devuelto_estante_at`.
        """
        from app.services.conteo_service import ConteoService
        ConteoService._exigir_supervision(usuario_id)
        pedido = str(pedido or '').strip()
        try:
            almacen_id = int(almacen_id)
        except (TypeError, ValueError):
            almacen_id = None
        if not pedido or not almacen_id:
            raise ValueError('Indique el pedido y el almacén')
        tareas = (TareaPicking.query
                  .filter(PickingService._filtro_devolvible_al_estante(),
                          TareaPicking.referencia_documento == pedido,
                          TareaPicking.almacen_id == almacen_id)
                  .with_for_update().all())
        if not tareas:
            raise LookupError(
                f'El pedido {pedido} no tiene mercancía recogida pendiente de '
                f'volver al estante (ya se declaró, o tiene un empaque vivo o '
                f'una remisión)')
        ahora = datetime.utcnow()
        nota = (nota or '').strip() or None
        for t in tareas:
            t.devuelto_estante_at = ahora
            t.devuelto_estante_por_id = usuario_id
            t.devuelto_estante_nota = nota
        db.session.commit()
        logger.info(f'[PICKING] Pedido {pedido} (almacén {almacen_id}): mercancía de '
                    f'{len(tareas)} tarea(s) declarada de vuelta al estante por #{usuario_id}')
        return {'pedido': pedido, 'almacen_id': almacen_id, 'tareas': len(tareas),
                'devuelto_estante_at': ahora.isoformat()}

    @staticmethod
    def reabrir_picking(tarea_id: int, usuario_id: int = None, motivo: str = None):
        """
        Reabre una tarea BLOQUEADA → PENDIENTE.
        Libera el inventario congelado por el bloqueo y la devuelve al pool.

        **Lo recogido antes del bloqueo ya salió de la ubicación.**
        `reportar_problema` descontó lo encontrado (SHORT_PICK). Poner
        `cantidad_recogida` en cero y dejar que el nuevo pick reste la
        cantidad completa lo restaba **dos veces** (P0-7, 2026-09-25). Dos
        caminos, según dónde esté esa mercancía:

        · **Hay un empaque que la explica** (vivo o con remisión — la misma
          condición de «devuelto al estante»): lo recogido está en la caja.
          La tarea original queda COMPLETADO por lo recogido y se crea una
          tarea PENDIENTE **solo por el faltante**.
        · **No hay empaque**: lo recogido vuelve a la ubicación. Se reingresa
          con su `MovimientoInventario` (REINGRESO_REAPERTURA) y la tarea
          vuelve a PENDIENTE por la cantidad completa, como antes.

        Devuelve la tarea que queda PENDIENTE (la original o la nueva).
        """
        motivo = motivo_obligatorio(motivo, 'reabrir una tarea de picking')
        tarea = TareaPicking.query.filter_by(id=tarea_id).with_for_update().first()
        if not tarea:
            raise ValueError('Tarea no encontrada')

        if tarea.estado != EstadoPicking.BLOQUEADO:
            raise ValueError(f'Solo se pueden reabrir tareas BLOQUEADAS (estado actual: {tarea.estado})')

        recogido = tarea.cantidad_recogida or 0
        cantidad_faltante = max(0, tarea.cantidad_solicitada - recogido)
        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()
        if reg:
            reg.bloqueado = max(0, reg.bloqueado - PickingService._cantidad_congelada(tarea, cantidad_faltante))

        antes = foto(tarea, ['estado', 'operario_id', 'cantidad_solicitada',
                             'cantidad_recogida', 'empaques_escaneados',
                             'motivo_bloqueo', 'observaciones_bloqueo'])

        en_caja = recogido > 0 and tarea.referencia_documento and db.session.query(
            PickingService._empaque_que_la_explica(tarea.referencia_documento)).scalar()

        if en_caja:
            # Lo recogido ya está empacado: la original se cierra por eso y el
            # faltante sale en una tarea nueva. Nada se reingresa ni se resta.
            tarea.cantidad_solicitada = recogido
            tarea.estado = EstadoPicking.COMPLETADO
            tarea.motivo_bloqueo = None
            if not tarea.fecha_completado:
                tarea.fecha_completado = datetime.utcnow()
            PickingService._soltar_operario(tarea)
            registrar_accion('REABRIR', tarea, usuario_id=usuario_id, motivo=motivo,
                             antes=antes, despues=foto(tarea, list(antes)),
                             entidad_codigo=tarea.codigo)
            if cantidad_faltante <= 0:
                db.session.commit()
                return tarea
            nueva = TareaPicking(
                codigo=PickingService._codigo_tarea(),
                producto_id=tarea.producto_id,
                cantidad_solicitada=cantidad_faltante,
                ubicacion_id=tarea.ubicacion_id,
                almacen_id=tarea.almacen_id,
                lote=tarea.lote,
                fecha_vencimiento=tarea.fecha_vencimiento,
                estado=EstadoPicking.PENDIENTE,
                prioridad=tarea.prioridad,
                referencia_documento=tarea.referencia_documento,
                tipo_documento=tarea.tipo_documento,
                bodega_origen_siesa=tarea.bodega_origen_siesa,
                pedido_clave=tarea.pedido_clave,
                observaciones_bloqueo=(f'Faltante de {tarea.codigo} reabierta: '
                                       f'lo recogido ({recogido}) ya estaba empacado.'),
            )
            db.session.add(nueva)
            if reg is not None:
                # La tarea nueva reserva lo suyo, como toda tarea que nace de
                # `crear_tareas`: su confirmación libera exactamente esto.
                reg.reservado = (reg.reservado or 0) + cantidad_faltante
            db.session.flush()
            registrar_accion('REABRIR', nueva, usuario_id=usuario_id, motivo=motivo,
                             antes={'origen': tarea.codigo},
                             despues={'cantidad_solicitada': cantidad_faltante},
                             entidad_codigo=nueva.codigo)
            db.session.commit()
            return nueva

        if recogido > 0 and reg is not None:
            # Sin empaque: lo recogido vuelve al hueco de donde salió.
            saldo_antes = reg.cantidad
            reg.cantidad = (reg.cantidad or 0) + recogido
            db.session.add(MovimientoInventario(
                producto_id=tarea.producto_id,
                ubicacion_id=tarea.ubicacion_id,
                almacen_id=tarea.almacen_id,
                tipo='REINGRESO_REAPERTURA',
                cantidad=recogido,
                saldo_antes=saldo_antes,
                saldo_despues=reg.cantidad,
                motivo=f'Reapertura de {tarea.codigo}: lo recogido vuelve a la ubicación',
                numero_documento=tarea.referencia_documento,
                usuario_id=usuario_id,
                idempotency_key=f'REAB-{tarea.id}-{int(datetime.utcnow().timestamp()*1000)}',
            ))
        if reg is not None and not tarea.bloqueo_sin_stock:
            # Vuelve al pool por la cantidad completa: reserva la cantidad
            # completa, que es lo que su confirmación libera.
            reg.reservado = (reg.reservado or 0) + tarea.cantidad_solicitada

        tarea.estado = EstadoPicking.PENDIENTE
        PickingService._soltar_operario(tarea)
        tarea.cantidad_recogida = 0
        tarea.empaques_escaneados = 0
        tarea.motivo_bloqueo = None
        registrar_accion('REABRIR', tarea, usuario_id=usuario_id, motivo=motivo,
                         antes=antes, despues=foto(tarea, list(antes)),
                         entidad_codigo=tarea.codigo)
        db.session.commit()
        return tarea

    @staticmethod
    def auditar_tarea(tarea_id: int, admin_id: int, resultado: str,
                      cantidad_hallada: int = 0, ubicacion_hallada: str = None,
                      observaciones: str = None) -> TareaPicking:
        """
        El admin cierra el ciclo de una tarea BLOQUEADA registrando qué encontró físicamente.

        Resultados y su efecto sobre el inventario WMS:
          ENCONTRADO_COMPLETO   → descongela bloqueado, cancela tarea. Además
                                   ajusta a Siesa como un conteo cíclico
                                   enfocado en este SKU (ver
                                   `ConteoService.ajustar_desde_auditoria_picking`)
          ENCONTRADO_PARCIAL    → descongela, reduce cantidad WMS a lo hallado,
                                   cancela tarea, y el mismo ajuste a Siesa que
                                   ENCONTRADO_COMPLETO
          NO_ENCONTRADO         → descongela, reduce inventario a 0 en esa ubicación, cancela tarea
          AVERIA                → descongela, mueve stock a ubicación AVERIAS, cancela tarea
          DISCREPANCIA_SIESA    → descongela, cancela tarea; registra para ajuste manual en Siesa
          ENCONTRADO            → descongela y cancela la tarea SIN tocar inventario, Siesa ni
                                   packing. El ajuste lo hace el conteo cíclico forzado que
                                   dispara la ruta (`ConteoService.forzar_desde_auditoria`):
                                   un solo sitio ajusta Siesa. Los COMPLETO/PARCIAL de arriba
                                   quedan por compatibilidad con datos y reportes viejos.

        ENCONTRADO_COMPLETO/PARCIAL levantan ValueError (sin persistir nada)
        si Siesa no responde la existencia del SKU — Regla 0, el ajuste nunca
        sale a ciegas contra el WMS. Reintentar la auditoría cuando Siesa
        responda.
        """
        RESULTADOS_VALIDOS = {
            'ENCONTRADO', 'ENCONTRADO_COMPLETO', 'ENCONTRADO_PARCIAL',
            'NO_ENCONTRADO', 'AVERIA', 'DISCREPANCIA_SIESA'
        }
        if resultado not in RESULTADOS_VALIDOS:
            raise ValueError(f'Resultado inválido: {resultado}')

        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError('Tarea no encontrada')
        if tarea.estado != EstadoPicking.BLOQUEADO:
            raise ValueError(f'Solo se pueden auditar tareas BLOQUEADAS (estado: {tarea.estado})')

        antes_auditoria = foto(tarea, ['estado', 'operario_id', 'cantidad_recogida',
                                       'motivo_bloqueo', 'observaciones_bloqueo'])
        cantidad_faltante = max(0, tarea.cantidad_solicitada - (tarea.cantidad_recogida or 0))
        # Backorder de Siesa: el WMS no perdió nada, Siesa simplemente no comprometió
        # esas unidades. Las auditorías de estas tareas no tocan el stock ni el
        # packing (este se armó solo con lo pickeable).
        es_backorder = tarea.motivo_bloqueo == 'BACKORDER_SIESA'

        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=tarea.ubicacion_id,
            producto_id=tarea.producto_id
        ).with_for_update().first()

        # 1. Descongelar inventario bloqueado en todos los casos
        if reg:
            reg.bloqueado = max(0, reg.bloqueado - PickingService._cantidad_congelada(tarea, cantidad_faltante))

        # 2. Ajuste de inventario según resultado
        if resultado == 'ENCONTRADO_COMPLETO':
            pass  # stock correcto, no hay ajuste

        elif resultado == 'ENCONTRADO_PARCIAL':
            if reg and cantidad_hallada < cantidad_faltante:
                diferencia = cantidad_faltante - cantidad_hallada
                reg.cantidad = max(0, reg.cantidad - diferencia)
                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    almacen_id=tarea.almacen_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-diferencia,
                    motivo=f'Auditoría tarea {tarea.codigo}: hallado {cantidad_hallada} de {tarea.cantidad_solicitada} solicitadas',
                    numero_documento=tarea.referencia_documento,
                    usuario_id=admin_id,
                ))

        elif resultado == 'NO_ENCONTRADO':
            # Una tarea `bloqueo_sin_stock` no tiene stock propio: apunta a la
            # ubicación de su hermana, y poner esa ubicación en 0 se llevaría
            # unidades que son de otras tareas.
            if reg and reg.cantidad and not (tarea.bloqueo_sin_stock or es_backorder):
                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    almacen_id=tarea.almacen_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-reg.cantidad,
                    motivo=f'Auditoría tarea {tarea.codigo}: faltante confirmado, unidades no encontradas',
                    numero_documento=tarea.referencia_documento,
                    usuario_id=admin_id,
                ))
                reg.cantidad = 0

        elif resultado == 'AVERIA':
            if reg and cantidad_hallada > 0:
                # ── Conservación: no se mueve más de lo que hay ──────────
                #
                # `cantidad_hallada` viene del formulario sin validar contra el
                # stock real. Con 50 sobre un origen de 10, el `max(0, ...)` de
                # abajo clampeaba el origen a 0 y el destino acreditaba 50:
                # cuarenta unidades averiadas **inventadas**. Se mueve lo que
                # existe y la diferencia se declara en el motivo.
                a_mover = min(cantidad_hallada, reg.cantidad)
                recortado = cantidad_hallada - a_mover

                # A partir de acá el destino EXISTE. Sin destino no se resta:
                # eso sería una pérdida, no un traslado.
                averia_ub, degradada = PickingService._destino_averias_garantizado(
                    tarea.almacen_id, a_mover)

                reg_averia = UbicacionProducto.query.filter_by(
                    ubicacion_id=averia_ub.id, producto_id=tarea.producto_id
                ).first()
                if reg_averia:
                    reg_averia.cantidad += a_mover
                else:
                    db.session.add(UbicacionProducto(
                        ubicacion_id=averia_ub.id,
                        producto_id=tarea.producto_id,
                        cantidad=a_mover,
                        # Lote y vencimiento viajan con la mercancía: se perdían
                        # en el traslado, y son lo que permite saber de qué
                        # entrada venía la caja rota.
                        lote=reg.lote,
                        fecha_vencimiento=reg.fecha_vencimiento,
                    ))

                reg.cantidad = max(0, reg.cantidad - a_mover)

                detalle = (f' (SIN zona AVERIAS de layout en el almacén — '
                           f'degradado al bin {averia_ub.codigo})' if degradada
                           else f' → {averia_ub.codigo}')
                if recortado:
                    detalle += (f'. Se declararon {cantidad_hallada} pero el '
                                f'origen solo tenía {a_mover}')
                base = (f'Auditoría tarea {tarea.codigo}: mercancía averiada '
                        f'trasladada a zona AVERIAS')
                motivo = (base + detalle)[:200]

                db.session.add(MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=tarea.ubicacion_id,
                    almacen_id=tarea.almacen_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=-a_mover,
                    motivo=motivo,
                    numero_documento=tarea.referencia_documento,
                    usuario_id=admin_id,
                ))
                # La pata que faltaba incluso en el camino feliz: el destino no
                # tenía ningún movimiento. Un traslado de una sola pata no es un
                # traslado — es un ajuste que miente sobre a dónde fue.
                mov_destino = MovimientoInventario(
                    producto_id=tarea.producto_id,
                    ubicacion_id=averia_ub.id,
                    almacen_id=tarea.almacen_id,
                    tipo='AJUSTE_AUDITORIA',
                    cantidad=a_mover,
                    motivo=motivo,
                    numero_documento=tarea.referencia_documento,
                    usuario_id=admin_id,
                    # Clave estable: es el ancla de idempotencia del traslado a
                    # Siesa. Sin ella, un reintento de la DLQ movería el stock
                    # dos veces y el saldo de la bodega puede quedar negativo.
                    idempotency_key=f'AUD-AVE-{tarea.id}',
                )
                db.session.add(mov_destino)
                db.session.flush()

                # ── Avisarle a Siesa ────────────────────────────────────────
                #
                # Sin esto la avería se queda dentro del WMS: sale del FEFO pero
                # Siesa la sigue contando como existencia vendible de la bodega,
                # así que un vendedor puede venderla y el pedido llega después
                # sin con qué surtirlo.
                #
                # A diferencia de la recepción, acá NO hay que esperar a ningún
                # documento previo: las unidades ya están en la bodega para
                # Siesa. Se encola en el acto.
                try:
                    from app.models.almacen import Almacen as _Alm
                    from app.services.siesa_job_service import (
                        encolar_traslado_averias as _encolar_ave)
                    _alm = db.session.get(_Alm, tarea.almacen_id)
                    _prod = tarea.producto
                    _encolar_ave(
                        movimiento=mov_destino,
                        codigo_siesa=(getattr(_prod, 'codigo_siesa', '') or '').strip(),
                        cantidad=a_mover,
                        bodega_del_almacen=getattr(_alm, 'bodega_siesa_id', None),
                        referencia=f'Avería detectada en picking · tarea {tarea.codigo}',
                    )
                except Exception as _e_ave:
                    # El stock ya se movió en el WMS y eso no se revierte por un
                    # fallo al encolar: se declara y la avería queda sin avisar
                    # a Siesa, que es el estado de siempre, no uno peor.
                    logger.error(
                        '[PICKING] tarea %s: avería registrada en el WMS pero no '
                        'se pudo encolar el traslado a averías en Siesa: %s. '
                        'La mercancía rota sigue contada como vendible en Siesa.',
                        tarea.codigo, _e_ave)

        elif resultado in ('DISCREPANCIA_SIESA', 'ENCONTRADO'):
            pass  # sin efecto en inventario: solo se registra (el conteo ajusta)

        # 2.5. ENCONTRADO_COMPLETO/PARCIAL son un conteo físico real, hecho por
        # quien ya es la autoridad (admin/supervisor) — se ajustan a Siesa con
        # la misma política que un conteo cíclico, enfocada en este SKU
        # puntual. NO_ENCONTRADO y AVERIA no entran aquí: ya generan su propio
        # MovimientoInventario local, y DISCREPANCIA_SIESA es a propósito
        # manual (ver rama de arriba).
        if resultado in ('ENCONTRADO_COMPLETO', 'ENCONTRADO_PARCIAL'):
            from app.services.conteo_service import ConteoService
            ConteoService.ajustar_desde_auditoria_picking(
                tarea,
                cantidad_fisica=(reg.cantidad if reg else 0),
                aprobador_id=admin_id,
            )

        # 3. Registrar auditoría y cancelar tarea
        tarea.auditoria_resultado         = resultado
        tarea.auditoria_cantidad_hallada  = cantidad_hallada
        tarea.auditoria_ubicacion_hallada = (ubicacion_hallada or '').strip() or None
        tarea.auditoria_observaciones     = (observaciones or '').strip() or None
        tarea.auditoria_resuelta_por_id   = admin_id
        tarea.fecha_auditoria             = datetime.utcnow()
        tarea.estado                      = EstadoPicking.CANCELADO
        PickingService._soltar_operario(tarea)
        registrar_accion(
            'CANCELAR', tarea, usuario_id=admin_id,
            motivo=(f'Auditoría {resultado}'
                    + (f': {tarea.auditoria_observaciones}' if tarea.auditoria_observaciones else '')),
            antes=antes_auditoria,
            despues={'estado': tarea.estado, 'auditoria_resultado': resultado,
                     'auditoria_cantidad_hallada': cantidad_hallada},
            entidad_codigo=tarea.codigo)

        # 4. Sincronizar el packing del pedido con el resultado de la auditoría.
        # total_disponible = lo que ya se había recogido antes del bloqueo
        # (short-pick previo, si lo hubo) + lo que la auditoría suma ahora.
        # Si el total es 0 (agotado real / avería total), se elimina la línea
        # del packing para que el pedido siga parcial — Siesa factura por lo
        # empacado, no por lo pedido (ver packing_service.cerrar_packing).
        # Dejarla en 0 en vez de eliminarla enviaría cantidad_empacada=0 sin
        # probar contra el conector de Siesa. Si el total es > 0, se ajusta
        # cantidad_esperada para que el empacador complete esa línea.
        if (tarea.referencia_documento and not es_backorder
                and resultado not in ('DISCREPANCIA_SIESA', 'ENCONTRADO')):
            from app.models.packing import TareaPacking as _TP, ItemPacking as _IP
            ya_recogido = tarea.cantidad_recogida or 0
            if resultado == 'ENCONTRADO_COMPLETO':
                total_disponible = tarea.cantidad_solicitada
            elif resultado == 'ENCONTRADO_PARCIAL':
                total_disponible = ya_recogido + cantidad_hallada
            else:  # NO_ENCONTRADO, AVERIA
                total_disponible = ya_recogido

            # Una línea puede tener VARIAS tareas (FEFO por ubicación, o el
            # backorder parcial de Siesa que la parte en pickeable + bloqueada).
            # El ítem del packing es de la línea completa: lo ya recogido por
            # las DEMÁS tareas cuenta. Sin esto, auditar la bloqueada (0
            # recogidas) borraba el ítem aunque la hermana ya hubiera recogido
            # 2 — PD1498, packing con 0 ítems.
            total_disponible += (
                db.session.query(db.func.coalesce(db.func.sum(TareaPicking.cantidad_recogida), 0))
                .filter(
                    TareaPicking.referencia_documento == tarea.referencia_documento,
                    TareaPicking.producto_id == tarea.producto_id,
                    TareaPicking.id != tarea.id,
                ).scalar()
            )

            packing = _TP.query.filter(
                _TP.numero_pedido_siesa == tarea.referencia_documento,
                _TP.estado.in_(['PENDIENTE', 'EN_PROCESO'])
            ).first()
            if packing:
                item = _IP.query.filter_by(
                    tarea_id=packing.id, producto_id=tarea.producto_id
                ).first()
                if item:
                    if total_disponible <= 0:
                        # La línea sale del empaque: el pedido sigue parcial.
                        # Lo único que queda de ella es este `antes`.
                        registrar_accion(
                            'ELIMINAR', item, usuario_id=admin_id,
                            motivo=f'Auditoría {resultado} de {tarea.codigo}: nada que empacar',
                            antes=foto(item), entidad_codigo=packing.codigo,
                            almacen_id=packing.almacen_id)
                        db.session.delete(item)
                    else:
                        item.cantidad_esperada = total_disponible
                        item.cantidad_real = 0
                        item.verificado = False

        db.session.commit()
        return tarea

    @staticmethod
    def reportar_problema(tarea_id: int, operario_id: int, motivo: str,
                           cantidad_encontrada=None, observaciones: str = None) -> dict:
        """
        Lógica compartida entre /picking/<id>/reportar-problema y /mobile/reportar-problema.
        Bloquea la tarea y registra short-pick si aplica. La tarea BLOQUEADA queda
        pendiente de resolución directa del admin/supervisor vía auditar_tarea()
        (pestaña Bodega) — sin conteo doble-ciego intermedio: es una excepción
        puntual, no un conteo cíclico de rutina.
        """
        from app.models.inventario import UbicacionProducto, MovimientoInventario
        from datetime import datetime as _dt

        tarea = TareaPicking.query.get(tarea_id)
        if not tarea:
            raise ValueError(f'Tarea picking {tarea_id} no encontrada')
        if tarea.operario_id != operario_id:
            raise PermissionError('Esta tarea no le pertenece')
        if tarea.estado not in (EstadoPicking.EN_PROCESO, EstadoPicking.PENDIENTE):
            raise ValueError(
                f'Solo se puede reportar problema en tareas EN_PROCESO o PENDIENTE '
                f'(estado actual: {tarea.estado})'
            )

        # Lo que el picker declaró, validado — nunca un 0 por omisión.
        cantidad_encontrada = cantidad_encontrada_declarada(
            motivo, cantidad_encontrada, tarea.cantidad_solicitada)
        cantidad_faltante = max(0, tarea.cantidad_solicitada - cantidad_encontrada)

        inv = (UbicacionProducto.query
               .filter_by(ubicacion_id=tarea.ubicacion_id, producto_id=tarea.producto_id)
               .with_for_update().first())

        if inv and cantidad_faltante > 0:
            # La porción no recogida deja de estar "reservada para picking en
            # curso" y pasa a "bloqueada pendiente de auditoría" — es la misma
            # unidad cambiando de estado, no una reserva nueva. Sin esto,
            # cantidad_disponible() (cantidad - reservado - bloqueado) la resta
            # dos veces, dejando el producto con menos disponible del real para
            # cualquier otro pedido que lo necesite.
            inv.bloqueado = inv.bloqueado + cantidad_faltante
            inv.reservado = max(0, inv.reservado - cantidad_faltante)

        # La cantidad recogida ES la declarada, también cuando es 0: antes solo
        # se escribía si era > 0 y una tarea con 3 escaneos reportada «encontré
        # 0» conservaba `cantidad_recogida = 3` sin haberlas descontado.
        tarea.cantidad_recogida = cantidad_encontrada
        if cantidad_encontrada > 0:
            if inv:
                inv.cantidad = max(0, inv.cantidad - cantidad_encontrada)
                inv.reservado = max(0, inv.reservado - cantidad_encontrada)
            db.session.add(MovimientoInventario(
                producto_id=tarea.producto_id,
                ubicacion_id=tarea.ubicacion_id,
                almacen_id=tarea.almacen_id,
                tipo='SHORT_PICK',
                cantidad=cantidad_encontrada,
                motivo=f'Short-pick — tarea {tarea.codigo} — faltó {cantidad_faltante}',
                numero_documento=tarea.referencia_documento,
                usuario_id=operario_id,
                idempotency_key=f'SP-{tarea.id}-{int(_dt.utcnow().timestamp()*1000)}',
            ))

        antes_bloqueo = foto(tarea, ['estado', 'operario_id', 'motivo_bloqueo'])
        tarea.estado = EstadoPicking.BLOQUEADO
        PickingService._soltar_operario(tarea)
        tarea.motivo_bloqueo = motivo
        tarea.observaciones_bloqueo = observaciones
        registrar_accion(
            'BLOQUEAR', tarea, usuario_id=operario_id,
            motivo=(motivo or '') + (f': {observaciones}' if observaciones else ''),
            antes=antes_bloqueo,
            despues={'estado': tarea.estado, 'motivo_bloqueo': motivo,
                     'cantidad_encontrada': cantidad_encontrada,
                     'cantidad_faltante': cantidad_faltante},
            entidad_codigo=tarea.codigo)

        # Snapshot para el tablero BI (métricas "SKU agotado" / "venta perdida $")
        # — solo agotado físico real, no el rechazo previo de Siesa (BACKORDER_SIESA
        # se dispara en bloquear_por_backorder_siesa(), antes de que nadie camine).
        # Misma transacción que el bloqueo: si el commit de abajo falla, no queda
        # un evento huérfano sin su tarea.
        if motivo == 'FALTANTE' and cantidad_faltante > 0:
            from app.services.eventos_agotado_service import registrar_evento_agotado
            registrar_evento_agotado(tarea, cantidad_faltante)

        # Capturar referencia antes del commit
        _ref_doc_rp = tarea.referencia_documento

        db.session.commit()

        # Auto-sync packing asociado — refleja el short-pick en cantidad_esperada del packing
        try:
            from app.models.packing import TareaPacking as _TPSyncRP
            from app.services.packing_picking_sync_service import PackingPickingSyncService as _PPSyncRP
            if _ref_doc_rp:
                _sync_pk_rp = _TPSyncRP.query.filter(
                    _TPSyncRP.numero_pedido_siesa == _ref_doc_rp,
                    _TPSyncRP.estado.in_(['PENDIENTE', 'EN_PROCESO'])
                ).first()
                if _sync_pk_rp:
                    _PPSyncRP.sincronizar(_sync_pk_rp.id)
        except Exception as _e_sync_rp:
            logger.warning('[PICKING] Sync packing (reportar_problema) falló: %s', _e_sync_rp)

        return {
            'ok': True,
            'mensaje': 'Problema reportado — el jefe de almacén lo resolverá',
            'motivo': motivo,
            'tarea_id': tarea_id,
            'cantidad_encontrada': cantidad_encontrada,
            'cantidad_faltante': cantidad_faltante,
        }

    @staticmethod
    def bloquear_por_backorder_siesa(tareas: list, detalle: str = None) -> None:
        """
        Bloquea tareas recién creadas (aún PENDIENTE, sin operario) porque
        Siesa no comprometió esa línea del pedido — ver
        `backorder_service.referencias_comprometidas_por_siesa`.

        Reutiliza el mismo ciclo BLOQUEADO → auditar_tarea() que ya existe
        para "el operario no lo encontró" (`reportar_problema`), en vez de
        una tabla nueva: mueve reservado→bloqueado exactamente igual, y
        `auditar_tarea(resultado='DISCREPANCIA_SIESA')` ya sabe descongelarlo
        y cancelar la tarea — esa opción existía desde antes precisamente
        para "hay una discrepancia con Siesa, ajustar allá manualmente".

        La diferencia con `reportar_problema` es la fuente: ahí el operario
        caminó y no lo encontró; acá Siesa lo canceló ANTES de que el
        operario llegara a intentarlo — el físico puede seguir estando en el
        estante, solo que Siesa no lo va a facturar en este pedido.
        """
        for tarea in tareas:
            reg = UbicacionProducto.query.filter_by(
                ubicacion_id=tarea.ubicacion_id,
                producto_id=tarea.producto_id,
            ).with_for_update().first()
            if reg:
                cant = tarea.cantidad_solicitada
                reg.bloqueado = reg.bloqueado + cant
                reg.reservado = max(0, reg.reservado - cant)

            tarea.estado = EstadoPicking.BLOQUEADO
            PickingService._soltar_operario(tarea)
            tarea.motivo_bloqueo = 'BACKORDER_SIESA'
            tarea.observaciones_bloqueo = detalle or (
                'Siesa no comprometió esta línea del pedido (backorder) — '
                'no se pickeó, el físico puede seguir disponible para otro pedido.'
            )
        db.session.commit()

    @staticmethod
    def crear_tareas_con_compromiso(
        *,
        producto_id: int,
        cantidad: int,
        compromiso_siesa,
        almacen_id: int,
        referencia_documento: str = None,
        tipo_documento: str = None,
        prioridad: int = 1,
        detalle: str = None,
    ) -> 'TareasConCompromiso':
        """
        Crea las tareas de una línea respetando lo que Siesa comprometió, y deja
        en cada una `cantidad_pedida` (lo que pedía la línea) para trazabilidad y
        para el aviso al operario.

        - `compromiso_siesa` >= cantidad, o `None` (no se pudo consultar):
          todo es pickeable, nada se bloquea (Regla 0).
        - 1 <= `compromiso_siesa` < cantidad (backorder parcial): solo el
          compromiso es pickeable; el resto queda BLOQUEADO (BACKORDER_SIESA) y
          se resuelve en Bodega → Auditoría, igual que una línea en 0. El
          operario no puede contar más de lo que Siesa facturará.

        El resto se reserva vía FEFO si hay stock. Si no lo hay (Siesa y el WMS
        cortos a la vez, el caso típico) se crea igual, bloqueado y sin congelar
        unidades (`bloqueo_sin_stock`): que no haya stock que reservar no puede
        ser motivo para que el faltante no llegue a Auditoría.

        Compone `crear_tareas` y `bloquear_por_backorder_siesa` sin modificarlas.
        """
        cant_pick = cantidad
        if compromiso_siesa is not None and 1 <= int(compromiso_siesa) < cantidad:
            cant_pick = int(compromiso_siesa)

        pickeables = PickingService.crear_tareas(
            producto_id=producto_id, cantidad=cant_pick, almacen_id=almacen_id,
            referencia_documento=referencia_documento,
            tipo_documento=tipo_documento, prioridad=prioridad,
        )
        bloqueadas = []
        if cant_pick < cantidad:
            resto = cantidad - cant_pick
            try:
                bloqueadas = PickingService.crear_tareas(
                    producto_id=producto_id, cantidad=resto, almacen_id=almacen_id,
                    referencia_documento=referencia_documento,
                    tipo_documento=tipo_documento, prioridad=prioridad,
                )
                PickingService.bloquear_por_backorder_siesa(bloqueadas, detalle=detalle)
            except ValueError as e:
                logger.warning(
                    '[PICKING] Backorder parcial: sin stock para reservar el resto (%s) — '
                    'se bloquea sin congelar unidades: %s', resto, e)
                db.session.rollback()
                bloqueadas = [PickingService._crear_bloqueada_sin_stock(
                    base=pickeables[0], cantidad=resto, detalle=detalle)]

        for t in pickeables + bloqueadas:
            t.cantidad_pedida = cantidad
        db.session.commit()
        return TareasConCompromiso(pickeables, bloqueadas, cant_pick)

    @staticmethod
    def _crear_bloqueada_sin_stock(*, base, cantidad: int, detalle: str = None):
        """Tarea BLOQUEADA (BACKORDER_SIESA) sobre la ubicación de `base`, sin
        tocar `reservado` ni `bloqueado`: no hay unidades que congelar. Existe
        para que el faltante quede visible en Bodega → Auditoría."""
        tarea = TareaPicking(
            codigo=PickingService._codigo_tarea(),
            producto_id=base.producto_id,
            cantidad_solicitada=cantidad,
            ubicacion_id=base.ubicacion_id,
            almacen_id=base.almacen_id,
            lote=base.lote,
            fecha_vencimiento=base.fecha_vencimiento,
            estado=EstadoPicking.BLOQUEADO,
            prioridad=base.prioridad,
            referencia_documento=base.referencia_documento,
            tipo_documento=base.tipo_documento,
            pedido_clave=base.pedido_clave,
            motivo_bloqueo='BACKORDER_SIESA',
            observaciones_bloqueo=detalle or (
                'Siesa comprometió menos de lo pedido — el resto es backorder y '
                'no había stock en el WMS para reservarlo.'),
            bloqueo_sin_stock=True,
        )
        db.session.add(tarea)
        return tarea


class TareasConCompromiso(NamedTuple):
    """Resultado de `PickingService.crear_tareas_con_compromiso`."""
    pickeables: list
    bloqueadas: list
    cantidad_pickeable: int
