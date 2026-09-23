"""
Servicio ABC — El WMS NO calcula clasificación ABC.
Siesa Enterprise tiene su propio motor estadístico.
Este servicio consume la clasificación de Siesa y genera tareas de conteo.

**Cuánto y cada cuánto se cuenta NO vive acá**: vive en
`app/services/conteo_politica.py` —cupo diario por almacén, intervalo objetivo
por clase, orden de selección y de reparto—. Este módulo aplica esa política:

  · Generador diario (cron 2:00 a. m. Bogotá): crea como máximo
    `cupo − pendientes_vivas` conteos, y ninguno si lo pendiente ya cubre dos
    días de cupo. Elige primero los nunca contados de A, después el mayor
    atraso relativo (días desde el último conteo / intervalo de su clase).
  · AI Watchdog: un producto B o C con picks de la última semana por encima
    del umbral de su clase recibe un conteo inmediato — dentro del mismo cupo,
    y nunca sobre un hueco contado hace menos de
    `CONTEO_WATCHDOG_DIAS_SIN_REABRIR` días.
"""
import uuid
import logging
from datetime import datetime, timedelta
from sqlalchemy import func
from app.extensions import db
from app.models.conteo import SesionConteo
from app.models.producto import Producto
from app.models.ubicacion import Ubicacion
from app.models.inventario import UbicacionProducto
from app.models.producto_clasificacion_abc import ProductoClasificacionABC
from app.services.connekta_gateway import connekta
from app.utils.fecha import ahora_bogota as _ahora_bogota

logger = logging.getLogger(__name__)

from app.services import conteo_politica as politica
from app.services.conteo_politica import WATCHDOG_UMBRAL, WATCHDOG_VENTANA_DIAS

# stock_minimo derivado = % del stock WMS actual de esa referencia, por clase.
# Más severo en A a propósito: alta rotación, agotarse ahí cuesta más caro que
# en C. Decisión de negocio (2026-08-26), no un número técnico — ver
# poblar_stock_minimo_desde_abc().
PORCENTAJE_STOCK_MINIMO_ABC = {'A': 0.20, 'B': 0.12, 'C': 0.08}


def universo_conteo_ciclico(almacen_id: int, clasificacion: str) -> list:
    """Los productos que el plan ABC exige contar en `almacen_id` para una clase:
    filas `(id, codigo_siesa)`.

    **La definición del universo, en un solo sitio.** La usan el generador
    (`ABCService.generar_tareas_conteo_diario`, que de acá saca el tamaño del
    lote diario) y las estadísticas de conteo (`app/services/metricas/conteo.py`,
    que de acá saca la carga y la cobertura). Si el reporte midiera otro
    universo que el generador, la cobertura hablaría de un plan que nadie
    ejecuta.

    Producto clasificado en ESE almacén (el ABC vive en ítem × bodega) ∩
    producto activo ∩ con stock > 0 en alguna ubicación del almacén — sin stock
    no hay nada que contar.
    """
    return (
        Producto.query
        .join(ProductoClasificacionABC,
              ProductoClasificacionABC.producto_id == Producto.id)
        .join(UbicacionProducto,
              UbicacionProducto.producto_id == Producto.id)
        .join(Ubicacion,
              Ubicacion.id == UbicacionProducto.ubicacion_id)
        .filter(
            ProductoClasificacionABC.almacen_id == almacen_id,
            ProductoClasificacionABC.clasificacion == clasificacion,
            Producto.activo == True,
            Ubicacion.almacen_id == almacen_id,
            UbicacionProducto.cantidad > 0,
        )
        .with_entities(Producto.id, Producto.codigo_siesa)
        .distinct()
        .all()
    )


def huecos_con_stock(almacen_id: int, producto_ids: list) -> list:
    """Los `UbicacionProducto` con stock > 0 de esos productos en el almacén —
    los huecos que el generador puede convertir en tarea, uno por
    (producto, ubicación). Compartida con las estadísticas por la misma razón
    que `universo_conteo_ciclico`."""
    if not producto_ids:
        return []
    return (
        UbicacionProducto.query
        .join(Ubicacion)
        .filter(
            UbicacionProducto.producto_id.in_(producto_ids),
            UbicacionProducto.cantidad > 0,
            Ubicacion.almacen_id == almacen_id
        ).all()
    )


def ultimo_conteo_por_hueco(producto_ids: list, ubicacion_ids: list) -> dict:
    """`{(producto_id, ubicacion_id): fecha_cierre del último conteo completado}`.

    **Qué cuenta como «contado» para el plan, en un solo sitio.** La usan el
    generador (que no vuelve a pedir un hueco contado dentro de su intervalo)
    y la cobertura de las estadísticas de conteo. Dos definiciones de «último
    conteo» harían que el reporte diga «al día» sobre un hueco que el
    generador va a volver a pedir, o al revés.

    Completado = `MATCH` o `AJUSTADO`, en cualquier fila de la cadena (un CC2
    que cuadra cierra en MATCH con su propia fecha). GROUP BY en SQL: no carga
    el historial en memoria.

    Ojo, declarado y no corregido acá: la `fecha_cierre` de un AJUSTADO es la
    hora en que la DLQ recibió la respuesta de Siesa, no la del conteo. Para
    decidir «al día / vencido» con intervalos de meses la diferencia
    no cambia nada; para atribuir un conteo a un DÍA sí — por eso las
    estadísticas no usan esta fecha para eso.
    """
    if not producto_ids or not ubicacion_ids:
        return {}
    filas = (
        db.session.query(
            SesionConteo.producto_id,
            SesionConteo.ubicacion_id,
            func.max(SesionConteo.fecha_cierre).label('ultima')
        )
        .filter(
            SesionConteo.producto_id.in_(producto_ids),
            SesionConteo.ubicacion_id.in_(ubicacion_ids),
            SesionConteo.estado.in_(['MATCH', 'AJUSTADO'])
        )
        .group_by(SesionConteo.producto_id, SesionConteo.ubicacion_id)
        .all()
    )
    return {(r.producto_id, r.ubicacion_id): r.ultima for r in filas}


def umbral_al_dia(clasificacion: str, ahora: datetime = None) -> datetime:
    """Desde cuándo un conteo sigue «al día» para su clase: `ahora − intervalo`.

    Instante técnico en UTC naive —se compara contra `fecha_cierre`, que es
    UTC naive—, no un día que alguien lea: no es fecha de negocio (Regla 5).
    El intervalo es el de `conteo_politica.intervalo_dias` (clase desconocida →
    el más exigente).
    """
    return (ahora or datetime.utcnow()) - timedelta(days=politica.intervalo_dias(clasificacion))


class RezagoCambioDesdeLaVistaPrevia(ValueError):
    """Lo que se confirma tiene que ser lo que se vio: el rezago cambió entre
    la vista previa y la confirmación, y no se canceló nada."""


class ABCService:

    @staticmethod
    def sincronizar_clasificacion_desde_siesa(api_abc: str = None):
        """
        Extrae la clasificación ABC de Siesa y actualiza los productos en el WMS.
        El WMS no calcula — solo sincroniza lo que Siesa ya calculó.

        API_v2_Items (ID 27) NO expone clasificación ABC — solo datos maestros.
        Se requiere un endpoint custom del Gestor de Consultas de Connekta V2.
        El parámetro api_abc acepta el nombre de esa consulta cuando el consultor
        la entregue (ej. 'API_custom_ABC_Rotacion').

        Mientras no exista ese endpoint, este método retorna pending=True y
        el scheduler diario lo omite sin error — el sistema sigue funcionando
        con la clasificación que Siesa estampe en el maestro de productos.
        """
        if connekta.modo_simulacion:
            logger.info('[ABC] Modo simulación — sincronización ABC omitida')
            return {'simulado': True, 'productos_actualizados': 0}

        if not api_abc:
            logger.info('[ABC] Endpoint ABC no configurado — pendiente Gestor de Consultas Connekta')
            return {
                'pending': True,
                'mensaje': (
                    'Requiere endpoint custom ABC del Gestor de Consultas de Connekta V2. '
                    'API_v2_Items (ID 27) no expone clasificación ABC. '
                    'Pedir al consultor: SELECT f120_referencia, clasificacion_abc FROM ... '
                    'y publicarla como API en el Gestor de Consultas.'
                ),
                'productos_actualizados': 0
            }

        try:
            # Paginar — puede haber miles de items
            _MAX_PAGS_ABC = 50  # 50 × 200 = 10 000 items — holgado para catálogo real
            actualizados = 0
            _errores_consec = 0
            pag = 1
            while pag <= _MAX_PAGS_ABC:
                try:
                    res = connekta._get(api_abc, {'paginacion': f'numPag={pag}|tamPag=200'})
                    _errores_consec = 0
                except Exception as _e_pag:
                    _errores_consec += 1
                    logger.warning(f'[ABC] Error en pag {pag}: {_e_pag} (consecutivos: {_errores_consec})')
                    if _errores_consec >= 3:
                        raise RuntimeError(f'ABC sync abortado tras 3 errores consecutivos en pag {pag}') from _e_pag
                    pag += 1
                    continue
                rows = res.get('detalle', {}).get('Table', [])
                if not rows:
                    break

                # Pre-cargar todos los productos de la página en un solo query
                codigos_pagina = list({
                    (r.get('f120_referencia') or '').strip()
                    for r in rows
                    if (r.get('f120_referencia') or '').strip()
                })
                productos_mapa = {
                    p.codigo_siesa: p
                    for p in Producto.query.filter(
                        Producto.codigo_siesa.in_(codigos_pagina)
                    ).all()
                }

                for row in rows:
                    codigo = (row.get('f120_referencia') or '').strip()
                    clasificacion = (row.get('clasificacion_abc') or '').strip().upper()
                    if not codigo or clasificacion not in ('A', 'B', 'C'):
                        continue
                    producto = productos_mapa.get(codigo)
                    if producto and producto.clasificacion_abc != clasificacion:
                        producto.clasificacion_abc = clasificacion
                        actualizados += 1

                if len(rows) < 200:
                    break
                pag += 1
            else:
                logger.critical(f'[ABC] Alcanzó MAX_PAGS={_MAX_PAGS_ABC} — posible loop infinito en endpoint Connekta')

            db.session.commit()
            logger.info(f'[ABC] {actualizados} productos reclasificados desde Siesa')
            return {'productos_actualizados': actualizados}

        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC] Error sincronizando desde Siesa: {e}')
            raise

    @staticmethod
    def poblar_stock_minimo_desde_abc(porcentajes: dict = None, dry_run: bool = False) -> dict:
        """
        Deriva Producto.stock_minimo = round(stock_wms_total × porcentaje_de_su_clase).

        stock_wms_total es la MISMA suma que usa DashboardService.alertas_stock()
        (todas las ubicaciones del WMS para ese producto) — el umbral se ancla en
        la misma métrica contra la que después se compara; si usara otra base
        (ej. existencia Siesa), el corte no significaría lo mismo en los dos lados.

        Porcentajes por clase — decisión de negocio (2026-08-26), confirmada con
        el usuario: A=20%, B=12%, C=8%. Más severo en A porque agotarse ahí
        cuesta más caro (alta rotación); C tolera un colchón proporcionalmente
        más chico.

        Reglas:
          - NO pisa un stock_minimo ya configurado — solo llena productos en
            NULL o en 0. El propio modelo (`Producto.stock_minimo = Column(...,
            default=0)`) usa 0 como valor de columna cuando nadie lo especifica
            — verificado en producción el 2026-08-26: de 26,294 productos,
            CERO están en NULL, 26,286 están en 0 y 8 tienen un valor real. Un
            filtro que solo mirara `IS NULL` no habría tocado ninguno. `0` es
            además el mismo valor que `alertas_stock()` ya trata como "sin
            umbral" (su filtro es `stock_minimo > 0`), así que tratarlo aquí
            como "no configurado" es consistente con esa semántica existente,
            no una nueva.
          - Productos sin ubicación con stock (total WMS = 0) se OMITEN, no se
            ponen en 0 ni en un valor inventado — sin un stock real de dónde
            partir, cualquier número sería fabricado (Regla 0: ante dato
            ausente, declarar, no inventar). Vuelven contados aparte en
            `sin_stock_omitidos` para que quien lo pida decida a mano si alguno
            necesita un mínimo aunque hoy esté en cero.
          - Piso de 1 unidad: con stock chico (ej. 3 unidades clase C, 8% = 0.24)
            redondear a 0 dejaría `stock_minimo=0`, que el propio filtro de
            alertas_stock() (`Producto.stock_minimo > 0`) excluye para siempre
            — un SKU real con algo de stock quedaría invisible sin que nadie lo
            note. Se sube a 1 como piso.

        dry_run=True calcula y devuelve el resultado sin escribir nada — para
        revisar el impacto (cuántos, y con qué números) antes de aplicar.
        """
        from app.services.picking_service import (
            filtro_ubicacion_vendible as _filtro_vendible_abc)
        porcentajes = porcentajes or PORCENTAJE_STOCK_MINIMO_ABC

        stock_por_producto = {
            row.producto_id: int(row.total)
            for row in (
                db.session.query(
                    UbicacionProducto.producto_id,
                    func.sum(UbicacionProducto.cantidad).label('total')
                )
                # El docstring de arriba promete que esta es «la MISMA suma que
                # DashboardService.alertas_stock()». Esa suma filtra por
                # `filtro_ubicacion_vendible()` y ésta no lo hacía: no tenía ni
                # join a `Ubicacion`. El umbral quedaba anclado en una base que
                # incluye las averías y se comparaba después contra una que las
                # excluye — el corte dejaba de significar lo mismo en los dos
                # lados, que es exactamente lo que el docstring dice que no
                # puede pasar. Con cero bins de averías en producción (medido el
                # 2026-09-14) las dos bases dan idéntico; el join las mantiene
                # idénticas el día que dejen de darlo.
                #
                # Lo que este join NO arregla, y hay que decirlo: la alerta
                # filtra además por almacén (`dashboard_service.py:85`) y esta
                # suma no — agrega la red entera. El umbral que se escribe es
                # una columna única de `Producto`, así que no puede ser por
                # almacén sin cambiar el modelo. Con stock repartido, el umbral
                # queda por encima de lo que un almacén puede tener y el SKU
                # entra en «bajo mínimo» de forma permanente. Es el mismo eje de
                # al lado del defecto de la zona, y sigue abierto.
                .join(Ubicacion, Ubicacion.id == UbicacionProducto.ubicacion_id)
                .filter(_filtro_vendible_abc())
                .group_by(UbicacionProducto.producto_id)
                .all()
            )
        }

        candidatos = Producto.query.filter(
            db.or_(Producto.stock_minimo.is_(None), Producto.stock_minimo == 0),
            Producto.clasificacion_abc.in_(list(porcentajes.keys())),
            Producto.activo == True,
        ).all()

        actualizados = 0
        sin_stock_omitidos = 0
        detalle_por_clase = {c: 0 for c in porcentajes}

        for p in candidatos:
            stock_total = stock_por_producto.get(p.id, 0)
            if stock_total <= 0:
                sin_stock_omitidos += 1
                continue
            pct = porcentajes.get(p.clasificacion_abc, 0)
            nuevo_minimo = max(1, round(stock_total * pct))
            if not dry_run:
                p.stock_minimo = nuevo_minimo
            actualizados += 1
            detalle_por_clase[p.clasificacion_abc] = detalle_por_clase.get(p.clasificacion_abc, 0) + 1

        if not dry_run:
            db.session.commit()
            logger.info(
                f'[ABC] stock_minimo poblado: {actualizados} productos '
                f'({sin_stock_omitidos} omitidos por no tener stock WMS)'
            )

        return {
            'dry_run': dry_run,
            'actualizados': actualizados,
            'sin_stock_omitidos': sin_stock_omitidos,
            'total_candidatos': len(candidatos),
            'por_clase': detalle_por_clase,
            'porcentajes_usados': porcentajes,
        }

    @staticmethod
    def watchdog_anomalias(almacen_id: int) -> list:
        """Los overrides del watchdog, como lista (contrato de siempre).
        El informe completo —qué se dejó de crear y por qué— lo da
        `ABCService.watchdog_con_informe`."""
        return ABCService.watchdog_con_informe(almacen_id)['overrides']

    @staticmethod
    def watchdog_con_informe(almacen_id: int) -> dict:
        """
        AI Watchdog — detecta productos cuya rotación real supera su clase ABC.

        Cuenta los picks COMPLETADOS de cada producto B o C en los últimos
        `WATCHDOG_VENTANA_DIAS` días, en ESTE almacén. Si superan el umbral de
        su clase (`WATCHDOG_UMBRAL`) crea una `SesionConteo` WATCHDOG_ABC con
        clase A, sin esperar al plan. Dos límites, los dos de
        `conteo_politica` (2026-09-23):

        - **No reabre lo recién contado.** Un hueco contado (MATCH/AJUSTADO)
          hace menos de `watchdog_dias_sin_reabrir()` días no se reabre. Antes
          el watchdog lo reabría CADA NOCHE mientras el SKU siguiera rotando —
          que es justamente lo que un SKU de alta rotación hace.
        - **Respeta el cupo del almacén.** Cuenta dentro del mismo cupo que el
          generador (`tope_de_generacion`), y corre ANTES que él en la corrida
          diaria: una anomalía vale más que un conteo del plan, así que toma su
          lugar en vez de sumarse encima. Si el tope es cero no crea nada, y el
          informe dice cuántos overrides quedaron sin crear (`omitidos_por_cupo`)
          — un override perdido en silencio es un watchdog apagado.

        Retorna `{overrides: [...], omitidos_por_cupo, omitidos_recien_contados,
        omitidos_ya_activos, excluidos_elegibilidad, cupo}`.
        """
        from app.models.picking import TareaPicking
        from app.models.inventario import UbicacionProducto

        # [A3] Advisory lock por almacén — evita ejecuciones concurrentes (scheduler + API manual).
        # Clave: 3000 + almacen_id (distinto de 2003 del scheduler general para no bloquear entre sí).
        _lock_key = 3000 + almacen_id
        _lock_acquired = db.session.execute(
            db.text(f'SELECT pg_try_advisory_lock({_lock_key})')
        ).scalar()
        informe = {'overrides': [], 'omitidos_por_cupo': 0, 'omitidos_recien_contados': 0,
                   'omitidos_ya_activos': 0, 'excluidos_elegibilidad': {}, 'cupo': None}
        if not _lock_acquired:
            logger.info(f'[ABC WATCHDOG] Almacén {almacen_id} — lock no disponible, omitiendo ejecución concurrente')
            informe['cupo'] = {'mensaje': 'otro proceso está corriendo el watchdog de este almacén'}
            return informe

        ventana = datetime.utcnow() - timedelta(days=WATCHDOG_VENTANA_DIAS)
        sin_reabrir_desde = datetime.utcnow() - timedelta(days=politica.watchdog_dias_sin_reabrir())
        overrides = informe['overrides']

        # [A1] Wrap everything in try/finally so advisory lock is always released,
        # even if an exception occurs during queries before the commit.
        try:
            # El cupo se lee con el lock del cupo tomado: el generador no puede
            # estar creando conteos del mismo almacén entre la lectura y el insert.
            politica.bloquear_cupo(almacen_id)
            tope = politica.tope_de_generacion(almacen_id)
            informe['cupo'] = tope

            # Pre-cargar todos los conteos activos del almacén en un set (producto_id, ubicacion_id)
            # para evitar N+1 en el check de duplicados dentro del loop
            conteos_activos = {
                (sc.producto_id, sc.ubicacion_id)
                for sc in SesionConteo.query.filter(
                    SesionConteo.almacen_id == almacen_id,
                    SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True)
                ).with_entities(SesionConteo.producto_id, SesionConteo.ubicacion_id).all()
            }

            candidatos = []   # (clave, producto, reg, clase, picks, umbral)
            # Clases susceptibles de override — consulta por almacén
            for clase, umbral in WATCHDOG_UMBRAL.items():
                productos_clase = (
                    Producto.query
                    .join(ProductoClasificacionABC,
                          ProductoClasificacionABC.producto_id == Producto.id)
                    .filter(
                        ProductoClasificacionABC.almacen_id == almacen_id,
                        ProductoClasificacionABC.clasificacion == clase,
                        Producto.activo == True
                    ).all()
                )

                if not productos_clase:
                    continue

                # Pre-cargar todos los counts de picks en un solo query GROUP BY.
                #
                # El filtro por almacén NO es una optimización: es lo que hace que
                # el número signifique lo que el umbral supone. Sin él se contaban
                # los picks de TODOS los almacenes, y un producto clase C de una
                # tienda cuyos 30 picks ocurrieron en el CD superaba el umbral de C
                # (10) y disparaba un override en la tienda: SesionConteo
                # WATCHDOG_ABC sobre una ubicación que nadie tenía razón para
                # contar, cupo diario del operario consumido, y un ajuste 142951
                # contra Siesa al final. Ese flujo es el de riesgo silencioso —
                # nadie reclama un ajuste. Toda la demás evidencia que consulta
                # esta función ya está acotada al almacén (clase ABC, ubicaciones
                # con stock, conteos activos); esta era la única que no.
                producto_ids_clase = [p.id for p in productos_clase]
                picks_rows = (
                    db.session.query(TareaPicking.producto_id, func.count(TareaPicking.id))
                    .filter(
                        TareaPicking.producto_id.in_(producto_ids_clase),
                        TareaPicking.almacen_id == almacen_id,
                        TareaPicking.estado == 'COMPLETADO',  # [A] corrección typo: era 'COMPLETADA'
                        TareaPicking.fecha_completado >= ventana
                    )
                    .group_by(TareaPicking.producto_id)
                    .all()
                )
                picks_por_producto = {pid: cnt for pid, cnt in picks_rows}

                # Pre-cargar UbicacionProducto solo para productos que superan el umbral
                ids_sobre_umbral = [
                    p.id for p in productos_clase
                    if picks_por_producto.get(p.id, 0) >= umbral
                ]
                if not ids_sobre_umbral:
                    continue
                registros_por_prod: dict = {}
                for reg in (
                    UbicacionProducto.query
                    .join(Ubicacion)
                    .filter(
                        UbicacionProducto.producto_id.in_(ids_sobre_umbral),
                        UbicacionProducto.cantidad > 0,
                        Ubicacion.almacen_id == almacen_id
                    ).all()
                ):
                    registros_por_prod.setdefault(reg.producto_id, []).append(reg)
                ultimo = ultimo_conteo_por_hueco(
                    ids_sobre_umbral,
                    sorted({r.ubicacion_id for regs in registros_por_prod.values() for r in regs}))

                for producto in productos_clase:
                    picks = picks_por_producto.get(producto.id, 0)
                    if picks < umbral:
                        continue
                    for reg in registros_por_prod.get(producto.id, []):
                        if (producto.id, reg.ubicacion_id) in conteos_activos:
                            informe['omitidos_ya_activos'] += 1
                            continue
                        ult = ultimo.get((producto.id, reg.ubicacion_id))
                        if ult and ult >= sin_reabrir_desde:
                            informe['omitidos_recien_contados'] += 1
                            continue
                        # Más anómalo primero: picks sobre su propio umbral.
                        clave = (-(picks / umbral), producto.id, reg.ubicacion_id)
                        candidatos.append((clave, producto, reg, clase, picks, umbral))

            elegibles, excluidos = politica.filtrar_elegibles(
                almacen_id, [(c[1].id, c[2].ubicacion_id) for c in candidatos])
            informe['excluidos_elegibilidad'] = excluidos
            elegibles = set(elegibles)
            candidatos = sorted((c for c in candidatos if (c[1].id, c[2].ubicacion_id) in elegibles),
                                key=lambda c: c[0])
            informe['omitidos_por_cupo'] = max(0, len(candidatos) - tope['tope'])

            for _, producto, reg, clase, picks, umbral in candidatos[:tope['tope']]:
                # Verificación en DB justo antes del insert — reduce ventana de race condition
                # entre dos workers que hayan pasado simultáneamente el check en memoria.
                ya_existe = SesionConteo.query.filter(
                    SesionConteo.producto_id == producto.id,
                    SesionConteo.ubicacion_id == reg.ubicacion_id,
                    SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True)
                ).first()
                if ya_existe:
                    conteos_activos.add((producto.id, reg.ubicacion_id))
                    continue

                codigo = (
                    f'CC-WATCHDOG-'
                    f'{_ahora_bogota().strftime("%Y%m%d")}-'
                    f'{str(uuid.uuid4())[:6].upper()}'
                )
                sesion = SesionConteo(
                    codigo=codigo,
                    tipo='WATCHDOG_ABC',
                    clasificacion_abc='A',   # override temporal
                    ubicacion_id=reg.ubicacion_id,
                    almacen_id=almacen_id,
                    producto_id=producto.id,
                    producto_codigo_siesa=producto.codigo_siesa,
                    maneja_lote=bool(getattr(reg, 'lote', None)),
                    estado='PENDIENTE'
                )
                from sqlalchemy.exc import IntegrityError as _IE_wd
                _sp = db.session.begin_nested()
                try:
                    db.session.add(sesion)
                    db.session.flush()
                    _sp.commit()
                    conteos_activos.add((producto.id, reg.ubicacion_id))  # evita duplicar en misma corrida
                    overrides.append({
                        'producto_id': producto.id,
                        'producto_codigo': producto.codigo_siesa or producto.codigo,
                        'clase_actual': clase,
                        'picks_7dias': picks,
                        'umbral': umbral,
                        'sesion_codigo': codigo,
                    })
                except _IE_wd:
                    _sp.rollback()
                    logger.warning(f'[ABC WATCHDOG] Sesión duplicada ignorada — prod {producto.id} ubic {reg.ubicacion_id}')

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC WATCHDOG] Error en watchdog: {e}')
            raise
        finally:
            # [M2] Envolver en try propio: excepción aquí no debe reemplazar el resultado exitoso
            # del try ni del except principal (enmascararía overrides correctamente aplicados).
            try:
                db.session.execute(db.text(f'SELECT pg_advisory_unlock({_lock_key})'))
                db.session.commit()
            except Exception as _fe:
                logger.error(f'[ABC WATCHDOG] Error liberando advisory lock {_lock_key}: {_fe}')

        if overrides:
            logger.warning(
                f'[ABC WATCHDOG] {len(overrides)} overrides en almacén {almacen_id}: '
                + ', '.join(f"{o['producto_codigo']} ({o['clase_actual']}→A, {o['picks_7dias']} picks)" for o in overrides)
            )
        else:
            logger.info(f'[ABC WATCHDOG] Sin anomalías nuevas en almacén {almacen_id}')
        if informe['omitidos_por_cupo']:
            logger.warning(
                f'[ABC WATCHDOG] Almacén {almacen_id}: {informe["omitidos_por_cupo"]} '
                f'override(s) sin crear por cupo — {tope.get("mensaje") or "cupo agotado"}')

        return informe

    @staticmethod
    def generar_tareas_conteo_diario(almacen_id: int, clasificacion: str = None,
                                     adelantar: bool = False, *, ahora: datetime = None):
        """
        Genera los conteos del plan de un almacén **dentro de su cupo diario**.

        Cuánto: como máximo `cupo − pendientes_vivas`, y nada si las pendientes
        ya cubren dos días de cupo (`conteo_politica.tope_de_generacion`). El
        resultado dice por qué se creó lo que se creó y cuánto quedó esperando
        (`cupo.mensaje`, `omitidos_por_cupo`). Antes el lote era
        `ceil(productos_de_la_clase / frecuencia)`, sumado cada noche sin mirar
        si alguien había contado el anterior.

        Qué: los huecos (producto × ubicación con stock) del universo ABC del
        almacén que están vencidos —nunca contados, o contados hace más que el
        intervalo de su clase— y que no tienen una cadena viva. `clasificacion`
        (A, B o C) restringe a una clase; `None` las considera todas juntas, que
        es lo que hace el cron: el cupo es del almacén, no de la clase.

        En qué orden: nunca contados de A primero; después el mayor atraso
        relativo (días desde el último conteo / intervalo de su clase)
        — `conteo_politica.clave_de_seleccion`.

        `adelantar=True` (el botón que antes se llamaba «Forzar todo» y creó
        8.527 tareas el 24-abr): también considera huecos todavía al día, los
        más atrasados primero. **El cupo se respeta igual**: adelantar trabajo
        es elegir entre más candidatos, no crear más.
        """
        clases = [clasificacion] if clasificacion else list(politica.CLASES)
        if clasificacion and clasificacion not in politica.CLASES:
            raise ValueError(f'clase inválida: {clasificacion!r} (A, B o C)')
        ahora = ahora or datetime.utcnow()

        # El lock del cupo antes de leer las pendientes: dos generaciones
        # simultáneas (cron + botón) no pueden calcular el mismo tope.
        politica.bloquear_cupo(almacen_id)
        tope = politica.tope_de_generacion(almacen_id)

        candidatos = []          # (clave, clase, producto, reg)
        total_universo = 0
        omitidos_por_pendiente = 0
        omitidos_por_intervalo = 0
        for clase in clases:
            # Solo productos con stock > 0 en este almacén — sin stock no hay nada que contar
            todos_productos = universo_conteo_ciclico(almacen_id, clase)
            total_universo += len(todos_productos)
            if not todos_productos:
                continue
            producto_por_id = {p.id: p for p in todos_productos}
            producto_ids = list(producto_por_id)
            todos_registros = huecos_con_stock(almacen_id, producto_ids)
            ubic_ids_all = sorted({r.ubicacion_id for r in todos_registros})
            if not ubic_ids_all:
                continue
            activos_set = {
                (s.ubicacion_id, s.producto_id)
                for s in SesionConteo.query.filter(
                    SesionConteo.producto_id.in_(producto_ids),
                    SesionConteo.ubicacion_id.in_(ubic_ids_all),
                    SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True)
                ).with_entities(SesionConteo.ubicacion_id, SesionConteo.producto_id).all()
            }
            # GROUP BY en SQL evita cargar todas las filas históricas en memoria
            ultimo_por_par = ultimo_conteo_por_hueco(producto_ids, ubic_ids_all)
            umbral = umbral_al_dia(clase, ahora)

            for reg in todos_registros:
                if (reg.ubicacion_id, reg.producto_id) in activos_set:
                    omitidos_por_pendiente += 1
                    continue
                ultimo_fecha = ultimo_por_par.get((reg.producto_id, reg.ubicacion_id))
                if ultimo_fecha and ultimo_fecha >= umbral and not adelantar:
                    omitidos_por_intervalo += 1
                    continue
                atraso = politica.atraso_relativo(ultimo_fecha, clase, ahora)
                clave = politica.clave_de_seleccion(
                    atraso, clase, (reg.producto_id, reg.ubicacion_id))
                candidatos.append((clave, clase, producto_por_id[reg.producto_id], reg))

        elegibles, excluidos = politica.filtrar_elegibles(
            almacen_id, [(p.id, r.ubicacion_id) for _, _, p, r in candidatos])
        elegibles = set(elegibles)
        candidatos = sorted((c for c in candidatos if (c[2].id, c[3].ubicacion_id) in elegibles),
                            key=lambda c: c[0])
        seleccion = candidatos[:tope['tope']]

        # Crear tareas — savepoint por ítem para tolerar race conditions entre
        # scheduler y API sin perder todos los inserts por un único conflicto.
        # La re-verificación dentro del savepoint cierra la ventana entre el
        # activos_set (leído antes) y el INSERT efectivo.
        from sqlalchemy.exc import IntegrityError as _IE
        creadas_por_clase = {c: 0 for c in clases}
        for _, clase, producto, reg in seleccion:
            sp = db.session.begin_nested()
            try:
                ya_existe = SesionConteo.query.filter(
                    SesionConteo.ubicacion_id == reg.ubicacion_id,
                    SesionConteo.producto_id == producto.id,
                    SesionConteo.raiz_con_cadena_viva(incluye_descuadre=True)
                ).first()
                if ya_existe:
                    sp.rollback()
                    continue

                codigo = (
                    f'CC-{clase}-'
                    f'{_ahora_bogota().strftime("%Y%m%d")}-'
                    f'{str(uuid.uuid4())[:6].upper()}'
                )
                sesion = SesionConteo(
                    codigo=codigo,
                    tipo='DIARIO_ABC',
                    clasificacion_abc=clase,
                    ubicacion_id=reg.ubicacion_id,
                    almacen_id=almacen_id,
                    producto_id=producto.id,
                    producto_codigo_siesa=producto.codigo_siesa,
                    maneja_lote=bool(getattr(reg, 'lote', None)),
                    estado='PENDIENTE'
                )
                db.session.add(sesion)
                db.session.flush()
                sp.commit()
                creadas_por_clase[clase] += 1
            except _IE:
                sp.rollback()
                logger.warning(f'[ABC] Sesión duplicada ignorada — prod {producto.id} ubic {reg.ubicacion_id}')

        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logger.error(f'[ABC] Error guardando tareas de conteo: {e}')
            raise

        creadas = sum(creadas_por_clase.values())
        omitidos_por_cupo = max(0, len(candidatos) - tope['tope'])
        if creadas:
            mensaje = (f'se crearon {creadas} conteo(s) de un cupo de {tope["cupo_diario"]}/día '
                       f'({tope["pendientes_vivas"]} ya pendientes)')
        elif tope['mensaje']:
            mensaje = tope['mensaje']
        else:
            mensaje = 'no se generó: no hay huecos vencidos sin una cadena viva'
        if omitidos_por_cupo:
            mensaje += f' · {omitidos_por_cupo} vencido(s) esperan cupo'
        advertencias = politica.advertencias_de_configuracion()

        logger.info(
            f'[ABC] Almacén {almacen_id} · clase {clasificacion or "A+B+C"} · {mensaje} · '
            f'{omitidos_por_pendiente} con cadena viva · {omitidos_por_intervalo} dentro de su intervalo'
            + (f' · CONFIGURACIÓN: {advertencias}' if advertencias else '')
        )

        return {
            'tareas_creadas': creadas,
            'por_clase': creadas_por_clase,
            'mensaje': mensaje,
            'cupo': tope,
            'candidatos': len(candidatos),
            'omitidos_por_cupo': omitidos_por_cupo,
            'omitidos_por_pendiente': omitidos_por_pendiente,
            'omitidos_por_intervalo': omitidos_por_intervalo,
            'excluidos_elegibilidad': excluidos,
            'total_universo': total_universo,
            'clasificacion': clasificacion or 'todas',
            'intervalos_dias': {c: politica.intervalo_dias(c) for c in clases},
            'almacen_id': almacen_id,
            'modo': 'adelantado' if adelantar else 'lote_diario',
            'advertencias_configuracion': advertencias,
        }

    @staticmethod
    def generar_todas_las_clases(almacen_id: int, adelantar: bool = False):
        """
        La corrida diaria de un almacén: Watchdog y después el plan A+B+C,
        los dos dentro del mismo cupo. Usado por el scheduler de las 2:00 a. m.
        (Bogotá) y por el botón «Generar lote del día».

        El watchdog va PRIMERO a propósito (antes iba después): sus conteos
        son anomalías de rotación y valen más que un conteo del plan, así que
        toman su lugar en el cupo en vez de sumarse encima de un cupo ya lleno.
        Si el watchdog falla, el plan corre igual y el fallo se avisa por correo.
        """
        resultados = {}
        total = 0

        try:
            wd = ABCService.watchdog_con_informe(almacen_id)
            total += len(wd['overrides'])
            resultados['watchdog'] = {
                'overrides': len(wd['overrides']),
                'detalle': wd['overrides'],
                'omitidos_por_cupo': wd['omitidos_por_cupo'],
                'omitidos_recien_contados': wd['omitidos_recien_contados'],
            }
        except Exception as e:
            # Productos que debían reclasificarse a A quedan en B/C → sin conteo urgente.
            logger.error(
                f'[ABC WATCHDOG] Error en almacén {almacen_id}: {e} '
                f'— overrides no aplicados; productos de alta rotación sin reclasificar',
                exc_info=True
            )
            resultados['watchdog'] = {'error': str(e)}
            # Intentar notificar por email (best-effort — no lanzar si falla)
            try:
                # [M26] Usar DLQ para que si Resend falla, el job quede visible en la queue
                # y se reintente — sin DLQ, la falla del watchdog queda completamente invisible.
                from app.services.alertas_service import _enviar_email_con_dlq
                _enviar_email_con_dlq(
                    asunto=f'[WMS ALERTA] ABC Watchdog falló — almacén {almacen_id}',
                    cuerpo_texto=(
                        f'El watchdog ABC del almacén {almacen_id} falló con error:\n{e}\n\n'
                        'Los productos de alta rotación no fueron reclasificados a clase A. '
                        'El conteo cíclico urgente no se generará automáticamente.'
                    ),
                    cuerpo_html=None,
                    tipo_alerta=f'abc_watchdog_fallo_{almacen_id}',
                )
            except Exception as _e_email_watchdog:
                logger.critical(
                    f'[ABC WATCHDOG] Email de alerta también falló — la falla del watchdog '
                    f'queda completamente invisible: {_e_email_watchdog}'
                )

        plan = ABCService.generar_tareas_conteo_diario(almacen_id, None, adelantar=adelantar)
        total += plan['tareas_creadas']
        for clase in politica.CLASES:
            resultados[clase] = {'tareas_creadas': plan['por_clase'].get(clase, 0)}
        # Prewarm se ejecuta en _prewarm_pre_turno (5:55am) — no aquí (2am) porque
        # el caché expira en ~5min y el turno empieza a las 6am.

        logger.info(f'[ABC] Generación completa · {total} tareas nuevas en almacén {almacen_id} · {plan["mensaje"]}')
        return {'total_tareas_creadas': total, 'por_clase': resultados,
                'plan': plan, 'mensaje': plan['mensaje']}

    @staticmethod
    def init_scheduler(app):
        """Scheduler del conteo cíclico ABC: la corrida diaria (watchdog + plan,
        dentro del cupo) a las 2:00 a. m. Bogotá, el pre-calentamiento del
        caché de Siesa a las 5:55 a. m. y la liberación de conteos zombi cada
        30 minutos."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from apscheduler.triggers.cron import CronTrigger
            import atexit
        except ImportError:
            logger.error('[ABC] APScheduler no instalado')
            return None

        def _job():
            with app.app_context():
                from app.extensions import db as _db
                # Advisory lock 2003 — garantiza que solo un worker Gunicorn ejecuta
                # este job a la vez. Si el lock no está disponible (otro worker ganó),
                # salir silenciosamente en vez de generar tareas duplicadas.
                lock_acquired = _db.session.execute(
                    _db.text('SELECT pg_try_advisory_lock(2003)')
                ).scalar()
                if not lock_acquired:
                    logger.info('[ABC] Job omitido — otro worker ya lo está ejecutando')
                    return
                try:
                    from app.models.almacen import Almacen
                    try:
                        almacenes = Almacen.query.filter_by(activo=True).all()
                    except Exception as _ex_setup:
                        logger.error('[ABC] Job falló consultando almacenes — sin tareas generadas', exc_info=True)
                        try:
                            from app.services.alertas_service import _enviar_email_con_dlq
                            _enviar_email_con_dlq(
                                asunto='[WMS ALERTA] ABC scheduler falló en setup (almacenes)',
                                cuerpo_texto=(
                                    f'El scheduler ABC no pudo obtener la lista de almacenes:\n{_ex_setup}\n\n'
                                    'No se generaron tareas de conteo cíclico hoy. '
                                    'Usar /api/abc/generar para re-disparar manualmente.'
                                ),
                                cuerpo_html=None,
                                tipo_alerta='abc_scheduler_setup_fallo',
                            )
                        except Exception as _e_em:
                            logger.critical('[ABC] Email de alerta de setup también falló: %s', _e_em)
                        return
                    logger.info(f'[ABC] Job iniciado — {len(almacenes)} almacén(es)')
                    completados = []
                    parciales = []   # [M28] Completados con watchdog fallido internamente
                    fallidos = []
                    for a in almacenes:
                        try:
                            res = ABCService.generar_todas_las_clases(a.id)
                            # [M28] generar_todas_las_clases no lanza excepción si el watchdog
                            # falla — lo registra en resultados['watchdog']['error'].
                            # Separar "completo" de "parcial" para que el log/email sea preciso.
                            watchdog_err = (res or {}).get('por_clase', {}).get('watchdog', {}).get('error')
                            if watchdog_err:
                                parciales.append(a.id)
                                logger.warning(
                                    f'[ABC] Almacén {a.id} parcial — watchdog falló: {watchdog_err}'
                                )
                            else:
                                completados.append(a.id)
                                logger.info(f'[ABC] Almacén {a.id} completado')
                        except Exception as ex:
                            fallidos.append(a.id)
                            logger.error(f'[ABC] Error almacén {a.id}: {ex}')
                    logger.info(
                        f'[ABC] Job finalizado — OK: {completados} | PARCIALES: {parciales} | FALLIDOS: {fallidos}'
                    )
                    if fallidos or parciales:
                        # SF_JOB_SILENCIOSO: almacenes fallidos quedan sin tareas de conteo hoy.
                        # Enviar alerta para que ops pueda disparar manualmente.
                        try:
                            from app.services.alertas_service import _enviar_email_con_dlq
                            _enviar_email_con_dlq(
                                asunto=f'[WMS ALERTA] ABC scheduler: {len(fallidos)} fallidos, {len(parciales)} parciales',
                                cuerpo_texto=(
                                    f'El scheduler ABC (2am Bogotá) tuvo problemas:\n'
                                    f'FALLIDOS (sin tareas generadas): {fallidos}\n'
                                    f'PARCIALES (watchdog sin reclasificar A): {parciales}\n'
                                    f'COMPLETADOS: {completados}\n\n'
                                    'Usa /api/abc/generar para re-disparar almacenes fallidos.'
                                ),
                                cuerpo_html=None,
                                tipo_alerta=f'abc_scheduler_fallo',
                            )
                        except Exception as _e_email_sched:
                            logger.critical(
                                f'[ABC] Email de alerta del scheduler también falló — '
                                f'falla de almacenes {fallidos} completamente invisible: {_e_email_sched}'
                            )
                finally:
                    try:
                        _db.session.rollback()
                        _db.session.execute(_db.text('SELECT pg_advisory_unlock(2003)'))
                        _db.session.commit()
                    except Exception as _fe:
                        logger.error(f'[ABC] Error liberando advisory lock 2003: {_fe}')

        def _prewarm_pre_turno(app):
            """
            Precalienta el caché de existencia Siesa 5 min antes del turno (5:55am).
            Para cada sesión PENDIENTE, consulta get_inventario_fecha para que el
            primer conteo del día no sufra cold cache (SEGUNDO_CONTEO falso).
            """
            with app.app_context():
                try:
                    from app.models.conteo import SesionConteo, EstadoConteo
                    from app.services.conteo_service import ConteoService
                    from sqlalchemy.orm import selectinload
                    sesiones_pendientes = SesionConteo.query.options(
                        selectinload(SesionConteo.producto),
                        selectinload(SesionConteo.ubicacion).selectinload(Ubicacion.almacen),
                    ).filter(
                        SesionConteo.estado == EstadoConteo.PENDIENTE
                    ).all()
                    logger.info(
                        f'[ABC] Pre-turno prewarm (5:55am): {len(sesiones_pendientes)} sesiones PENDIENTE'
                    )
                    # Prewarm: consultar existencia Siesa para cada producto pendiente
                    # Paralelizado (max_workers=3) para completar dentro del
                    # misfire_grace_time=300s cuando hay backlog >100 sesiones.
                    from concurrent.futures import ThreadPoolExecutor
                    _warmed = 0

                    def _warm_one(_s):
                        _bodega = _s.ubicacion.almacen.bodega_siesa if _s.ubicacion and _s.ubicacion.almacen else None
                        ConteoService.consultar_existencia_siesa(
                            _s.producto.codigo_siesa, bodega=_bodega
                        )

                    _targets = [s for s in sesiones_pendientes if s.producto and s.producto.codigo_siesa]
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        futures = {pool.submit(_warm_one, s): s for s in _targets}
                        for fut in futures:
                            try:
                                fut.result(timeout=10)
                                _warmed += 1
                            except Exception:
                                pass  # prewarm best-effort — no bloquea
                    logger.info(f'[ABC] Pre-turno prewarm completado: {_warmed}/{len(_targets)} productos')
                except Exception as e:
                    logger.error(f'[ABC] Pre-turno prewarm falló: {e}', exc_info=True)

        def _liberar_zombis():
            """Libera tareas EN_PROCESO >2h sin progreso — cada 30 min.

            El lock 2015 se tomaba y **no se liberaba nunca**. Los advisory
            locks de sesión viven en la CONEXIÓN: la conexión volvía al pool
            tomada, y la corrida siguiente se encontraba el lock ocupado y hacía
            `return`. El job dejaba de correr, en silencio y sin error — las
            tareas zombi se quedaban EN_PROCESO para siempre.
            """
            from app.utils.lock import advisory_lock

            with app.app_context():
                try:
                    with advisory_lock(2015, 'liberar_zombis') as tomado:
                        if not tomado:
                            return
                        from app.services.conteo_service import ConteoService
                        ConteoService.liberar_tareas_zombi(timeout_horas=2)
                except Exception as e:
                    logger.error(f'[ABC] Liberación de tareas zombi falló: {e}', exc_info=True)
                    try:
                        from app.services.alertas_service import _enviar_email_con_dlq
                        _enviar_email_con_dlq(
                            '[WMS] Job liberar_zombis falló',
                            f'<p><b>Error:</b> {str(e)[:400]}</p>',
                            f'Error: {str(e)[:400]}',
                            'conteo_liberar_zombis_fallo',
                        )
                    except Exception:
                        pass

        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = BackgroundScheduler(timezone='America/Bogota')
        scheduler.add_job(
            func=_job,
            trigger=CronTrigger(hour=2, minute=0, timezone='America/Bogota'),
            id='abc_conteo_diario',
            name='Generar tareas conteo cíclico ABC — 2am Bogotá',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        scheduler.add_job(
            func=_prewarm_pre_turno,
            trigger=CronTrigger(hour=5, minute=55, timezone='America/Bogota'),
            kwargs={'app': app},
            id='abc_prewarm_pre_turno',
            name='Pre-calentar caché Siesa antes del turno — 5:55am Bogotá',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        scheduler.add_job(
            func=_liberar_zombis,
            trigger=IntervalTrigger(minutes=30),
            id='conteo_liberar_zombis',
            name='Liberar conteos EN_PROCESO >2h — cada 30 min',
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=120,
        )
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown(wait=False))
        logger.info('[ABC] Scheduler configurado — ABC 2am + prewarm 5:55am + zombis cada 30min')
        return scheduler

    @staticmethod
    def resumen_abc(almacen_id: int):
        """
        Distribución ABC del almacén y **el plan vigente**: intervalo de cada
        clase, cupo diario y lo pendiente. Los textos salen de
        `conteo_politica`, no de una tabla escrita aparte — la anterior decía
        «semanal / mensual / trimestral» mientras el generador usaba 15/90/180
        días, y la pantalla repetía otra copia.
        """
        from app.extensions import db as database
        from sqlalchemy import func

        # Una sola query GROUP BY en lugar de 3 COUNTs separados
        counts_rows = (
            database.session.query(
                ProductoClasificacionABC.clasificacion,
                func.count(ProductoClasificacionABC.id)
            )
            .filter(ProductoClasificacionABC.almacen_id == almacen_id)
            .group_by(ProductoClasificacionABC.clasificacion)
            .all()
        )
        counts_map = {clase: cnt for clase, cnt in counts_rows}
        rotacion = {'A': 'Alta rotación', 'B': 'Rotación media', 'C': 'Baja rotación'}
        plan = politica.descripcion_del_plan(almacen_id)
        resumen = {
            cls: {
                'total_productos': counts_map.get(cls, 0),
                'intervalo_dias': plan['intervalos_dias'][cls],
                'descripcion': (f'{rotacion[cls]} — contar cada '
                                f'{plan["intervalos_dias"][cls]} días'),
            }
            for cls in politica.CLASES
        }
        tope = politica.tope_de_generacion(almacen_id)

        return {
            'almacen_id': almacen_id,
            'distribucion_abc': resumen,
            'plan': {**plan, 'pendientes_vivas': tope['pendientes_vivas'],
                     'dias_de_cupo_pendientes': tope['dias_de_cupo_pendientes'],
                     'generaria_hoy': tope['tope'], 'mensaje': tope['mensaje']},
            'fuente': 'Siesa Enterprise' if not connekta.modo_simulacion else 'WMS local (simulación)'
        }

    #: Los tipos que el plan crea solo. Son los únicos que la cancelación del
    #: rezago puede tocar: un MANUAL lo pidió una persona y una
    #: EXCEPCION_PICKING la abrió un faltante real.
    TIPOS_DEL_PLAN = ('DIARIO_ABC', 'WATCHDOG_ABC')

    @staticmethod
    def _filtros_rezago_cancelable(almacen_id: int, clasificacion: str = None) -> list:
        """**La única definición de «qué del rezago se puede cancelar».** La usan
        la vista previa y la cancelación: si fueran dos cálculos, la vista
        previa mentiría el día que uno cambie.

        Solo raíces del plan (DIARIO_ABC / WATCHDOG_ABC) en PENDIENTE, sin
        dueño y sin hijos: una tarea que nadie tomó y que no empezó ninguna
        cadena. Nunca un CC2/CC3 (su raíz quedaría huérfana en SEGUNDO/
        TERCER_CONTEO — lo que hacía el DELETE anterior), nunca un MANUAL ni una
        EXCEPCION_PICKING, nunca algo que un operario ya tiene asignado.
        """
        from sqlalchemy import exists
        from sqlalchemy.orm import aliased
        from app.models.conteo import EstadoConteo
        hijo = aliased(SesionConteo)
        filtros = [
            SesionConteo.almacen_id == almacen_id,
            SesionConteo.estado == EstadoConteo.PENDIENTE,
            SesionConteo.operario_id.is_(None),
            SesionConteo.es_segundo_conteo.is_(False),
            SesionConteo.tipo.in_(ABCService.TIPOS_DEL_PLAN),
            ~exists().where(hijo.sesion_origen_id == SesionConteo.id),
        ]
        if clasificacion:
            filtros.append(SesionConteo.clasificacion_abc == clasificacion)
        return filtros

    @staticmethod
    def plan_cancelar_rezago(almacen_id: int, clasificacion: str = None,
                             *, ahora: datetime = None) -> dict:
        """Qué cancelaría «Limpiar cola» — **sin tocar nada**.

        Cuántas, por clase, por tipo y por antigüedad (días operativos desde
        que se crearon), y también **lo que NO se toca** con su motivo: un
        líder que solo ve «se cancelan 4.825» no sabe que quedan 3 auditorías
        por faltante y dos segundos conteos esperando.
        """
        from app.models.conteo import EstadoConteo
        from app.services.metricas.conteo import TRAMOS_REZAGO
        from app.utils.fecha import dia_operativo_de
        if clasificacion and clasificacion not in politica.CLASES:
            raise ValueError(f'clase inválida: {clasificacion!r} (A, B o C)')
        ahora = ahora or datetime.utcnow()
        hoy = dia_operativo_de(ahora)

        cancelables = (SesionConteo.query
                       .filter(*ABCService._filtros_rezago_cancelable(almacen_id, clasificacion))
                       .with_entities(SesionConteo.id, SesionConteo.tipo,
                                      SesionConteo.clasificacion_abc,
                                      SesionConteo.fecha_creacion)
                       .order_by(SesionConteo.id)
                       .all())
        ids = [r.id for r in cancelables]
        por_clase, por_tipo = {}, {}
        por_antiguedad = {t[0]: 0 for t in TRAMOS_REZAGO}
        for r in cancelables:
            clase = r.clasificacion_abc or 'sin clase'
            por_clase[clase] = por_clase.get(clase, 0) + 1
            por_tipo[r.tipo] = por_tipo.get(r.tipo, 0) + 1
            if r.fecha_creacion is None:
                # Sin fecha → el tramo más viejo: no saber cuándo nació no la
                # hace reciente.
                tramo = TRAMOS_REZAGO[-1][0]
            else:
                edad = max(0, (hoy - dia_operativo_de(r.fecha_creacion)).days)
                tramo = next(n for n, lo, hi in TRAMOS_REZAGO
                             if edad >= lo and (hi is None or edad <= hi))
            por_antiguedad[tramo] += 1

        # Lo que queda: todo lo vivo sin contar que NO entra, con su porqué.
        id_set = set(ids)
        con_hijo = {sid for (sid,) in db.session.query(SesionConteo.sesion_origen_id)
                    .filter(SesionConteo.almacen_id == almacen_id,
                            SesionConteo.sesion_origen_id.isnot(None)).all()}
        vivas = (SesionConteo.query
                 .filter(SesionConteo.almacen_id == almacen_id,
                         SesionConteo.estado.in_([EstadoConteo.PENDIENTE,
                                                  EstadoConteo.EN_PROCESO]))
                 .with_entities(SesionConteo.id, SesionConteo.tipo, SesionConteo.estado,
                                SesionConteo.operario_id, SesionConteo.es_segundo_conteo,
                                SesionConteo.clasificacion_abc)
                 .all())
        no_se_tocan = {}
        for v in vivas:
            if v.id in id_set or (clasificacion and v.clasificacion_abc != clasificacion):
                continue
            if v.es_segundo_conteo:
                motivo = 'verificacion_cc2_cc3'
            elif v.tipo == 'EXCEPCION_PICKING':
                motivo = 'auditoria_por_faltante'
            elif v.tipo == 'MANUAL':
                motivo = 'conteo_manual'
            elif v.estado == EstadoConteo.EN_PROCESO:
                motivo = 'en_proceso'
            elif v.operario_id:
                motivo = 'asignada_a_un_operario'
            elif v.id in con_hijo:
                motivo = 'con_cadena_iniciada'
            else:
                motivo = f'otro_tipo_{v.tipo}'
            no_se_tocan[motivo] = no_se_tocan.get(motivo, 0) + 1

        return {
            'almacen_id': almacen_id,
            'clasificacion': clasificacion or 'todas',
            'al_dia_operativo': hoy.isoformat(),
            'a_cancelar': len(ids),
            'por_clase': por_clase,
            'por_tipo': por_tipo,
            'por_antiguedad_dias': por_antiguedad,
            'no_se_tocan': no_se_tocan,
            'ids': ids,
        }

    @staticmethod
    def cancelar_rezago(almacen_id: int, *, motivo: str, usuario_id: int,
                        clasificacion: str = None, esperadas: int = None) -> dict:
        """Cancela el rezago del plan: **cancelar, no borrar**.

        El endpoint anterior hacía un DELETE físico de toda PENDIENTE del
        almacén: borraba CC2 (la raíz quedaba huérfana en SEGUNDO_CONTEO para
        siempre), conteos MANUAL y auditorías por faltante, sin dejar rastro de
        quién ni por qué. Ahora: estado CANCELADO, `fecha_cierre`,
        `motivo_edicion` obligatorio y `editado_por`, solo sobre lo que define
        `_filtros_rezago_cancelable` —el mismo cálculo que la vista previa—.

        `esperadas`: el `a_cancelar` que el líder vio en la vista previa. Si el
        rezago cambió desde entonces (alguien tomó conteos, el cron generó
        otros) no se cancela nada y se levanta `ValueError`: lo que se confirma
        tiene que ser lo que se vio.
        """
        from sqlalchemy import false
        from app.models.conteo import EstadoConteo
        motivo = (motivo or '').strip()
        if not motivo:
            raise ValueError('Se requiere un motivo para cancelar el rezago')
        plan = ABCService.plan_cancelar_rezago(almacen_id, clasificacion)
        if esperadas is not None and int(esperadas) != plan['a_cancelar']:
            raise RezagoCambioDesdeLaVistaPrevia(
                f'El rezago cambió desde la vista previa: se vieron {esperadas} y '
                f'ahora son {plan["a_cancelar"]}. Volvé a abrir la vista previa.')

        ahora = datetime.utcnow()
        # Mismo filtro bajo lock de fila: una tarea que un operario toma entre
        # la vista previa y este UPDATE ya no cumple «sin dueño» y queda fuera.
        filas = (SesionConteo.query
                 .filter(SesionConteo.id.in_(plan['ids']) if plan['ids'] else false(),
                         *ABCService._filtros_rezago_cancelable(almacen_id, clasificacion))
                 .with_for_update(skip_locked=True)
                 .all())
        for s in filas:
            s.estado = EstadoConteo.CANCELADO
            s.fecha_cierre = ahora
            s.motivo_edicion = f'CANCELADO (rezago del plan): {motivo}'
            s.editado_por = usuario_id
            s.editado_en = ahora
        db.session.commit()
        logger.warning(
            '[ABC] Rezago cancelado en almacén %s por usuario #%s: %s de %s '
            '(clase %s) — %s', almacen_id, usuario_id, len(filas), plan['a_cancelar'],
            clasificacion or 'todas', motivo)
        plan.pop('ids', None)
        return {**plan, 'canceladas': len(filas), 'motivo': motivo, 'ejecutado': True}

    @staticmethod
    def procesar_csv_abc(file_obj, ext: str, almacen_id: int = None) -> dict:
        """
        Parsea el archivo CSV/Excel del reporte "Recalculo de rotación ABC" de Siesa
        y actualiza clasificacion_abc en la tabla productos.

        Detecta automáticamente:
        - Las filas basura del encabezado (empresa, NIT, fechas, filtros)
        - Las columnas Referencia y Clasificación
        - El separador del CSV (coma, punto y coma, tab)

        Retorna resumen con actualizados, no_encontrados, distribucion.
        """
        import csv
        import io
        import re
        from collections import Counter

        def _norm(s):
            s = str(s).lower().strip()
            for a, b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n')]:
                s = s.replace(a, b)
            return re.sub(r'[^\w\s]', '', s).strip()

        ALIASES_REF = ['referencia', 'ref', 'codigo', 'codigo siesa', 'cod item',
                       'f120 referencia', 'item ref']
        ALIASES_ABC = ['clasificacion', 'clasificacion abc', 'abc', 'clase',
                       'clasif', 'rotacion abc']

        def _col(headers_norm, aliases):
            for alias in aliases:
                an = _norm(alias)
                for i, h in enumerate(headers_norm):
                    if an == h or an in h or h in an:
                        return i
            return None

        # ── Leer contenido ─────────────────────────────────────────────────
        contenido = file_obj.read()

        if ext in ('xlsx', 'xls'):
            try:
                import openpyxl
                wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
                ws = wb.active
                filas = [[str(c) if c is not None else '' for c in row]
                         for row in ws.iter_rows(values_only=True)]
            except ImportError:
                raise RuntimeError('openpyxl no instalado en el servidor')
        else:
            # Detectar encoding — Siesa exporta en latin-1/cp1252, no en UTF-8
            texto = None
            for enc in ('utf-8-sig', 'utf-8', 'latin-1', 'cp1252'):
                try:
                    texto = contenido.decode(enc)
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if texto is None:
                texto = contenido.decode('latin-1', errors='replace')

            # Auto-detectar separador
            muestra = texto[:4096]
            sep = ';' if muestra.count(';') >= muestra.count(',') else ','
            if muestra.count('\t') > muestra.count(sep):
                sep = '\t'
            filas = list(csv.reader(io.StringIO(texto), delimiter=sep))

        if not filas:
            raise ValueError('El archivo está vacío')

        # ── Encontrar fila de headers (saltar basura Siesa) ─────────────
        # La primera fila que contenga "referencia" o "clasificaci" es el header
        palabras_clave = {'referencia', 'clasificaci', 'abc', 'clasif', 'item'}
        idx_h = None
        for i, fila in enumerate(filas):
            norm_celdas = [_norm(c) for c in fila]
            hits = sum(1 for c in norm_celdas if any(p in c for p in palabras_clave) and len(c) > 2)
            if hits >= 2:
                idx_h = i
                break

        if idx_h is None:
            # Fallback: primera fila no vacía
            for i, fila in enumerate(filas):
                if any(c.strip() for c in fila):
                    idx_h = i
                    break

        if idx_h is None:
            raise ValueError('No se encontró la fila de encabezados. '
                             'Verifica que el archivo sea el reporte ABC de Siesa.')

        headers = filas[idx_h]
        hn = [_norm(h) for h in headers]

        i_ref = _col(hn, ALIASES_REF)
        i_abc = _col(hn, ALIASES_ABC)

        if i_ref is None:
            raise ValueError(f'No encontré columna "Referencia". Columnas detectadas: {headers}')
        if i_abc is None:
            raise ValueError(f'No encontré columna "Clasificación". Columnas detectadas: {headers}')

        # ── Parsear datos ───────────────────────────────────────────────
        datos = []
        omitidos = 0
        for fila in filas[idx_h + 1:]:
            if not fila or not any(c.strip() for c in fila):
                continue
            try:
                # strip() quita espacios; rstrip('-') quita guión de extensión de Siesa
                referencia = fila[i_ref].strip().rstrip('-').strip()
                clasificacion = fila[i_abc].strip().upper()
            except IndexError:
                omitidos += 1
                continue
            if not referencia or referencia in ('-', '') or clasificacion not in ('A', 'B', 'C'):
                omitidos += 1
                continue
            datos.append((referencia, clasificacion))

        if not datos:
            raise ValueError(f'No hay filas válidas en el archivo. Omitidos: {omitidos}')

        dist = Counter(c for _, c in datos)

        # ── Upsert en producto_clasificacion_abc ───────────────────────
        actualizados = 0
        no_encontrados = []
        LOTE = 500

        for i in range(0, len(datos), LOTE):
            lote = datos[i:i + LOTE]
            refs = [r for r, _ in lote]

            # Mapa referencia → Producto (prioridad: codigo_siesa sobre codigo para evitar colisiones)
            _prods_lote = Producto.query.filter(
                db.or_(
                    Producto.codigo_siesa.in_(refs),
                    Producto.codigo.in_(refs)
                ),
                Producto.activo == True
            ).all()
            existentes: dict = {}
            for _p in _prods_lote:
                # Indexar por codigo_siesa primero; fallback a codigo
                if _p.codigo_siesa and _p.codigo_siesa not in existentes:
                    existentes[_p.codigo_siesa] = _p
                if _p.codigo and _p.codigo not in existentes:
                    existentes[_p.codigo] = _p

            # Pre-cargar registros ABC existentes para el lote completo
            ids_lote = [existentes[r].id for r, _ in lote if existentes.get(r)]
            if almacen_id and ids_lote:
                abc_existentes = {
                    reg.producto_id: reg
                    for reg in ProductoClasificacionABC.query.filter(
                        ProductoClasificacionABC.producto_id.in_(ids_lote),
                        ProductoClasificacionABC.almacen_id == almacen_id
                    ).all()
                }
            else:
                abc_existentes = {}

            for referencia, clasificacion in lote:
                producto = existentes.get(referencia)
                if not producto:
                    no_encontrados.append(referencia)
                    continue

                if almacen_id:
                    # Upsert por (producto_id, almacen_id) usando el mapa pre-cargado
                    registro = abc_existentes.get(producto.id)
                    if registro:
                        registro.clasificacion = clasificacion
                        registro.updated_at = datetime.utcnow()
                    else:
                        nuevo = ProductoClasificacionABC(
                            producto_id=producto.id,
                            almacen_id=almacen_id,
                            clasificacion=clasificacion
                        )
                        db.session.add(nuevo)
                        abc_existentes[producto.id] = nuevo  # evita duplicados dentro del lote
                else:
                    # Fallback legacy: actualizar campo global
                    producto.clasificacion_abc = clasificacion

                actualizados += 1

            # flush sin commit — la transacción completa se confirma al final
            # para que un Railway restart no deje clasificaciones parcialmente actualizadas
            try:
                db.session.flush()
            except Exception as _flush_err:
                db.session.rollback()
                logger.error(
                    f'[ABC CSV] flush() falló en lote {i}–{i + LOTE}: {_flush_err}',
                    exc_info=True
                )
                raise

        try:
            db.session.commit()
        except Exception as _commit_err:
            db.session.rollback()
            logger.error(f'[ABC CSV] Error en commit final: {_commit_err}')
            raise

        logger.info(
            f'[ABC CSV] almacen={almacen_id} · {actualizados} upserted · '
            f'{len(no_encontrados)} no encontrados · '
            f'A={dist["A"]} B={dist["B"]} C={dist["C"]}'
        )

        return {
            'actualizados': actualizados,
            'no_encontrados': len(no_encontrados),
            'no_encontrados_muestra': no_encontrados[:20],
            'omitidos_formato': omitidos,
            'distribucion': {'A': dist['A'], 'B': dist['B'], 'C': dist['C']},
            'total_procesados': len(datos),
            'filas_encabezado_saltadas': idx_h,
            'almacen_id': almacen_id,
        }