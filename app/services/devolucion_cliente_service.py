"""
DevolucionClienteService — devolución física de cliente atada al pedido/factura.

Reemplaza el flujo reactivo de TareaDevolucion (devolucion_service.py, DEPRECATED).
El recepcionista busca el pedido ya despachado, cuenta físicamente cuánto se
devuelve por línea (total o parcial, con o sin avería) y confirma en un solo
paso: entra al inventario del WMS y dispara automáticamente la Nota Crédito
(142946) hacia Siesa vía SiesaJob(tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE').

IMPORTANTE — F430_ID_TIPO_DOCTO/F430_CONSEC_DOCTO de 142946 deben ser el
tipo/consecutivo de la FACTURA ELECTRÓNICA real (spec DOCX), no del pedido.
Por eso este servicio resuelve tipo_docto_fe/consec_fe vía
connekta.get_detalle_factura() antes de tocar get_rowids_factura/
trigger_nota_factura — nunca reutiliza TareaPacking.tipo_docto_pedido_siesa/
consec_docto_pedido_siesa directamente para esos dos campos.
"""
import logging
from datetime import datetime
from app.extensions import db
from app.models.devolucion_cliente import (DevolucionCliente, LineaDevolucionCliente,
                                           EstadoDevolucionCliente, FuenteAprobacionNC)
from app.models.packing import TareaPacking
from app.models.producto import Producto
from app.models.inventario import UbicacionProducto, MovimientoInventario
from app.models.ubicacion import Ubicacion
from app.models.siesa_job import SiesaJob, EstadoSiesaJob
from app.services.connekta_gateway import connekta
from app.services.recepcion_service import RecepcionService
from app.utils.fecha import ahora_bogota as _ahora_bogota
# La política única de «esto no es vendible» — un solo sitio la define y este
# servicio, que es el escritor VIVO de bins de averías, la obedece en vez de
# re-escribirla. Ver el bloque de cabecera de picking_service.py.
from app.services.picking_service import (
    campos_ubicacion_averias as _campos_ubicacion_averias,
    campos_ubicacion_devolucion as _campos_ubicacion_devolucion,
    es_ubicacion_vendible as _es_ubicacion_vendible,
    ZONA_DEVOLUCION as _ZONA_DEVOLUCION,
)

logger = logging.getLogger(__name__)

_UBICACION_AVERIADOS = 'AVERIADOS'
#: El bin de lo devuelto SANO mientras la NC no está aprobada (zona
#: `DEVOLUCION`, no vendible — `picking_service.ZONAS_NO_VENDIBLES`).
_UBICACION_DEVOLUCIONES = 'DEVOLUCIONES'

_E = EstadoDevolucionCliente


def cambiar_estado(devolucion: DevolucionCliente, nuevo: str) -> None:
    """**La única función que escribe `DevolucionCliente.estado`** (salvo el
    estado inicial al crear, en `crear_devolucion`).

    Existe por dos cosas que dependen del estado y que nadie puede olvidar:

    · **la transición es válida** (`EstadoDevolucionCliente.TRANSICIONES`): una
      CONFIRMADA no vuelve a ABIERTA, una CANCELADA no se confirma;
    · **el índice de «una sola devolución activa por línea de factura»**
      (`f470_rowid_activo`) se mantiene: una devolución que deja de estar
      activa libera sus líneas de factura para la siguiente.

    Los sellos de tiempo del estado van acá también: `fecha_confirmacion` al
    contarla, `cancelada_at` al cancelarla. Trinquete AST:
    `tests/test_devolucion_vuelve.py::TestUnaFuncionEscribeElEstado`.
    """
    actual = devolucion.estado
    if nuevo == actual:
        return
    permitidas = _E.TRANSICIONES.get(actual, ())
    if nuevo not in permitidas:
        raise ValueError(
            f'La devolución {devolucion.codigo} está {actual}: no puede pasar a {nuevo}')
    devolucion.estado = nuevo
    ahora = datetime.utcnow()
    if nuevo in _E.CONTADAS and not devolucion.fecha_confirmacion:
        devolucion.fecha_confirmacion = ahora
    if nuevo == _E.CANCELADA:
        devolucion.cancelada_at = ahora
    sincronizar_rowid_activo(devolucion)


def sincronizar_rowid_activo(devolucion: DevolucionCliente) -> None:
    """`f470_rowid_activo` = `f470_rowid` mientras la devolución está activa;
    NULL si no. Lo llaman `cambiar_estado` y la vinculación a la factura."""
    activa = devolucion.estado in _E.ACTIVAS
    for linea in devolucion.lineas:
        linea.f470_rowid_activo = (str(linea.f470_rowid) if (activa and linea.f470_rowid)
                                   else None)


def ya_devuelto_de_la_factura(tipo_docto_fe: str, consec_fe, excluir_id: int = None) -> dict:
    """Lo ya devuelto de una factura por devoluciones CONTADAS, por línea.

    `{'rowid': {rowid: cant}, 'codigo': {codigo_siesa: cant}}` — la segunda
    clave para las líneas viejas sin `f470_rowid`. Es el «contando las previas»
    del tope `devuelto ≤ facturado`: la de mostrador y la de ruta sobre la
    misma factura se suman, y ninguna de las dos puede pasar lo facturado.
    """
    q = (db.session.query(LineaDevolucionCliente)
         .join(DevolucionCliente, DevolucionCliente.id == LineaDevolucionCliente.devolucion_id)
         .filter(DevolucionCliente.tipo_docto_fe == str(tipo_docto_fe or ''),
                 DevolucionCliente.consec_fe == str(consec_fe or ''),
                 DevolucionCliente.estado == _E.CONFIRMADA))
    if excluir_id is not None:
        q = q.filter(DevolucionCliente.id != excluir_id)
    por_rowid, por_codigo = {}, {}
    for ln in q.all():
        cant = float(ln.cantidad_devuelta or 0)
        if cant <= 0:
            continue
        if ln.f470_rowid:
            por_rowid[str(ln.f470_rowid)] = por_rowid.get(str(ln.f470_rowid), 0.0) + cant
        else:
            por_codigo[ln.codigo_siesa] = por_codigo.get(ln.codigo_siesa, 0.0) + cant
    return {'rowid': por_rowid, 'codigo': por_codigo}


def _ya_devuelto_de_linea(ya: dict, rowid, codigo_siesa) -> float:
    """Lo ya devuelto de UNA línea de factura. Por rowid; lo viejo sin rowid
    cuenta contra la referencia (lado conservador: descuenta de más, no de
    menos)."""
    total = 0.0
    if rowid:
        total += ya['rowid'].get(str(rowid), 0.0)
    total += ya['codigo'].get(codigo_siesa, 0.0)
    return total


def devolucion_de_ruta_activa(tarea_packing_id: int):
    """La devolución de RUTA sin contar de este pedido, si hay."""
    return (DevolucionCliente.query
            .filter(DevolucionCliente.tarea_packing_id == tarea_packing_id,
                    DevolucionCliente.recaudo_entrega_id.isnot(None),
                    DevolucionCliente.estado.in_(_E.ACTIVAS))
            .order_by(DevolucionCliente.id.desc())
            .first())


class DevolucionClienteService:

    @staticmethod
    def buscar_pedido(numero_pedido_siesa: str) -> dict:
        """
        Localiza el pedido ya despachado y trae, desde Siesa, el detalle real
        de la factura electrónica generada para ese pedido. No persiste nada.
        """
        tarea = (TareaPacking.query
                 .filter_by(numero_pedido_siesa=numero_pedido_siesa, tipo_documento='PEDIDO')
                 .order_by(TareaPacking.id.desc())
                 .first())
        if not tarea:
            raise ValueError(f'No se encontró el pedido {numero_pedido_siesa}')
        if not tarea.siesa_triggered:
            raise ValueError(
                f'El pedido {numero_pedido_siesa} aún no ha sido facturado en Siesa '
                '(despacho no confirmado) — no se puede procesar una devolución todavía'
            )

        # Resolver tipo/consec REALES de la FE — nunca los _pedido_siesa.
        #
        # Esto vivía acá y SOLO acá. Liquidación hacía lo que este mismo
        # comentario prohíbe, en seis sitios, y por eso ninguna nota crédito de
        # ruta llegó nunca a Siesa (job 440, 2026-08-11). Un comentario protege
        # al archivo donde está escrito y a ninguno más: ahora es una función.
        from app.services.fe_resolver import FENoEncontrada, resolver_fe
        try:
            tipo_docto_fe, consec_fe = resolver_fe(tarea, gateway=connekta)
        except FENoEncontrada as e:
            raise ValueError(
                f'No se pudo localizar la factura electrónica del pedido '
                f'{numero_pedido_siesa} en Siesa — {e}'
            )

        # Líneas reales de la FE (cantidad facturada, bodega, uom, rowid)
        rowids_data = connekta.get_rowids_factura(tipo_docto_fe, consec_fe)
        if not rowids_data:
            raise ValueError(
                f'La factura {tipo_docto_fe}-{consec_fe} no tiene líneas en Siesa'
            )

        # Lo que ya volvió de esta factura (otra devolución contada, de ruta o
        # de mostrador): el tope de cada línea es lo facturado MENOS eso.
        ya = ya_devuelto_de_la_factura(tipo_docto_fe, consec_fe)
        lineas = []
        # Una referencia de la factura sin producto en el WMS no se puede
        # recibir. Antes solo dejaba un WARNING en el log y la pantalla
        # mostraba la factura sin esa línea, como si no existiera. Ahora viaja
        # en la respuesta: la recepcionista ve qué falta y por qué.
        sin_producto = []
        for row in rowids_data:
            ref = (row.get('f120_referencia') or '').strip()
            if not ref:
                continue
            producto = Producto.query.filter_by(codigo_siesa=ref).first()
            if not producto:
                logger.warning(
                    '[DEV_CLIENTE] buscar_pedido: referencia Siesa %r sin producto WMS — omitida', ref
                )
                sin_producto.append(ref)
                continue
            _ya_linea = _ya_devuelto_de_linea(ya, row.get('f470_rowid'), ref)
            lineas.append({
                'ya_devuelto': _ya_linea,
                'producto_id': producto.id,
                'producto_codigo': producto.codigo,
                'producto_nombre': producto.nombre,
                'codigo_barras': producto.codigo_barras,
                'codigo_siesa': ref,
                'cantidad_facturada': float(row.get('f470_cant_base') or 0),
                'f470_id_unidad_medida': (row.get('f470_id_unidad_medida') or '').strip(),
                'f150_id_bodega': (row.get('f150_id') or '').strip(),
                'f470_rowid': str(row.get('f470_rowid') or ''),
            })

        activa = devolucion_de_ruta_activa(tarea.id)
        return {
            'tarea_packing_id': tarea.id,
            'numero_pedido_siesa': tarea.numero_pedido_siesa,
            'cliente': tarea.cliente,
            'almacen_id': tarea.almacen_id,
            'tipo_docto_fe': tipo_docto_fe,
            'consec_fe': consec_fe,
            'lineas': lineas,
            'referencias_sin_producto': sin_producto,
            # Si la factura tiene una devolución de ruta sin contar, lo que trae
            # el cliente se cuenta AHÍ: una segunda devolución sobre las mismas
            # líneas reingresaría dos veces (y `crear_devolucion` la rechaza).
            'devolucion_ruta_activa': ({'id': activa.id, 'codigo': activa.codigo,
                                        'estado': activa.estado}
                                       if activa else None),
        }

    @staticmethod
    def crear_devolucion(tarea_packing_id: int, tipo_docto_fe: str, consec_fe: str,
                         almacen_id: int, recepcionista_id: int, lineas: list,
                         es_total: bool = False, observaciones: str = None,
                         recaudo_entrega_id: int = None, commit: bool = True,
                         estado_inicial: str = EstadoDevolucionCliente.ABIERTA,
                         declaracion_conductor: dict = None) -> DevolucionCliente:
        """
        Crea la cabecera + líneas con cantidad_devuelta > 0.
        lineas: [{producto_id, codigo_siesa, cantidad_facturada, cantidad_devuelta,
                  es_averiado, cantidad_averiada, f470_id_unidad_medida,
                  f150_id_bodega, f470_rowid, cantidad_declarada}]

        `recaudo_entrega_id`: **solo** la pasa `devolucion_ruta` (la única
        función que crea devoluciones de ruta, trinquete AST). Con ella la
        devolución nace `EN_CAMION` (`estado_inicial`) y puede nacer sin líneas:
        una PARCIAL de un formulario viejo que no declaró qué volvió se cuenta
        contra la factura en recepción, no se inventa.

        Sin ella es la de mostrador: nace ABIERTA, con al menos una línea, y
        **se rechaza si el pedido tiene una devolución de ruta sin contar** —
        lo que trae el cliente se cuenta en esa, no en una segunda que
        reingresaría las mismas unidades dos veces.

        El tope es `ya devuelto (otras devoluciones contadas) + esta ≤
        facturado`, por línea de factura (`f470_rowid`).

        `commit=False`: para cuando el caller ya está dentro de su propia
        transacción y hace un solo commit al final.
        """
        tarea = db.session.get(TareaPacking, tarea_packing_id)
        if not tarea:
            raise ValueError(f'TareaPacking {tarea_packing_id} no existe')
        es_de_ruta = recaudo_entrega_id is not None
        if estado_inicial not in _E.ACTIVAS:
            raise ValueError(f'Una devolución nace EN_CAMION o ABIERTA, no {estado_inicial}')

        if not es_de_ruta:
            activa = devolucion_de_ruta_activa(tarea_packing_id)
            if activa:
                raise ValueError(
                    f'El pedido {tarea.numero_pedido_siesa} tiene la devolución de ruta '
                    f'{activa.codigo} ({activa.estado}) sin contar: lo que trajo el cliente '
                    f'se cuenta en esa — ábrela desde «Llegó el camión». Una devolución '
                    f'nueva reingresaría las mismas unidades dos veces.')

        lineas_validas = [l for l in lineas if float(l.get('cantidad_devuelta') or 0) > 0]
        if not lineas_validas and not es_de_ruta:
            raise ValueError('Debes indicar al menos una línea con cantidad devuelta > 0')

        ya = ya_devuelto_de_la_factura(tipo_docto_fe, consec_fe)
        for l in lineas_validas:
            cant_dev = float(l['cantidad_devuelta'])
            cant_fact = float(l.get('cantidad_facturada') or 0)
            if cant_dev > cant_fact:
                raise ValueError(
                    f'Producto {l.get("codigo_siesa")}: cantidad devuelta ({cant_dev}) '
                    f'no puede superar la cantidad facturada ({cant_fact})'
                )
            _ya = _ya_devuelto_de_linea(ya, l.get('f470_rowid'), l.get('codigo_siesa'))
            if _ya and cant_dev + _ya > cant_fact + 1e-9:
                raise ValueError(
                    f'Producto {l.get("codigo_siesa")}: de esta factura ya se devolvieron '
                    f'{_ya:g} (otra devolución contada) — con {cant_dev:g} más se pasaría '
                    f'de lo facturado ({cant_fact:g})')
            if l.get('f470_rowid'):
                ocupada = DevolucionClienteService._devolucion_activa_de_la_linea(
                    str(l['f470_rowid']))
                if ocupada is not None:
                    raise ValueError(
                        f'Producto {l.get("codigo_siesa")}: esa línea de la factura ya está '
                        f'en la devolución {ocupada.codigo} ({ocupada.estado}) sin contar')

        codigo = f'DEVC-{_ahora_bogota().strftime("%Y%m%d%H%M%S")}-{tarea_packing_id}'
        if es_de_ruta:
            # Dos paradas del mismo pedido confirmadas en el mismo segundo
            # chocarían en el índice único del código: el recaudo lo distingue.
            codigo = f'{codigo}-R{recaudo_entrega_id}'
        devolucion = DevolucionCliente(
            codigo=codigo,
            tarea_packing_id=tarea_packing_id,
            numero_pedido_siesa=tarea.numero_pedido_siesa,
            tipo_docto_fe=tipo_docto_fe or '',
            consec_fe=str(consec_fe or ''),
            cliente=tarea.cliente,
            almacen_id=almacen_id,
            recepcionista_id=recepcionista_id,
            estado=estado_inicial,
            es_total=bool(es_total),
            observaciones=observaciones,
            recaudo_entrega_id=recaudo_entrega_id,
            declaracion_conductor=declaracion_conductor,
        )
        db.session.add(devolucion)
        db.session.flush()

        for l in (lineas_validas if not es_de_ruta else lineas):
            DevolucionClienteService._agregar_linea(devolucion, l)
        sincronizar_rowid_activo(devolucion)

        if commit:
            db.session.commit()
        else:
            db.session.flush()
        logger.info('[DEV_CLIENTE] Devolución creada: %s (%s) · pedido %s · %d línea(s)',
                    codigo, estado_inicial, tarea.numero_pedido_siesa, len(devolucion.lineas))
        return devolucion

    @staticmethod
    def _agregar_linea(devolucion: DevolucionCliente, l: dict) -> LineaDevolucionCliente:
        cant_dev = l.get('cantidad_devuelta') or 0
        averiada = l.get('cantidad_averiada')
        if averiada is not None:
            averiada = max(0.0, min(float(averiada), float(cant_dev)))
        linea = LineaDevolucionCliente(
            producto_id=l['producto_id'],
            codigo_siesa=l.get('codigo_siesa') or '',
            cantidad_facturada=l.get('cantidad_facturada') or 0,
            cantidad_devuelta=cant_dev,
            cantidad_declarada=l.get('cantidad_declarada'),
            es_averiado=bool(l.get('es_averiado')),
            cantidad_averiada=averiada,
            f470_id_unidad_medida=l.get('f470_id_unidad_medida'),
            f150_id_bodega=l.get('f150_id_bodega'),
            f470_rowid=(str(l['f470_rowid']) if l.get('f470_rowid') else None),
        )
        devolucion.lineas.append(linea)
        return linea

    @staticmethod
    def _devolucion_activa_de_la_linea(rowid: str, excluir_id: int = None):
        """La devolución ACTIVA (EN_CAMION/ABIERTA) que ya tiene esta línea de
        factura, si hay. El índice único de `f470_rowid_activo` es el respaldo
        ante una carrera; esto es el mensaje claro antes de llegar ahí."""
        if not rowid:
            return None
        q = (db.session.query(DevolucionCliente)
             .join(LineaDevolucionCliente,
                   LineaDevolucionCliente.devolucion_id == DevolucionCliente.id)
             .filter(LineaDevolucionCliente.f470_rowid_activo == str(rowid)))
        if excluir_id is not None:
            q = q.filter(DevolucionCliente.id != excluir_id)
        return q.first()

    @staticmethod
    def confirmar_entrada_fisica(devolucion_id: int, recepcionista_id: int,
                                  lineas_ajustadas: list = None) -> DevolucionCliente:
        """
        Cuenta la devolución: ingresa el stock físico y encola la NC (251126).
        Calca el patrón de recepcion_service.confirmar_recepcion: GET a Siesa
        antes de los row-locks de inventario, with_for_update para serializar
        confirmaciones concurrentes, SiesaJob creado antes del commit final.

        `lineas_ajustadas`: [{linea_id | producto_id, cantidad_devuelta,
        cantidad_averiada | es_averiado}] — lo CONTADO por recepción. `linea_id`
        manda: en un producto de doble unidad dos líneas tienen el mismo
        producto. Ninguna línea se agrega ni se quita.

        ## Lo que cambió el 2026-09-24 (m045devol)

        · **Acepta EN_CAMION y ABIERTA.** La de ruta nace al confirmar la parada.
        · **Contar en cero es una respuesta, no un error** (solo en las de
          ruta): FALTANTE_TOTAL. No entra inventario ni sale NC, y el faltante
          queda medido — antes había que CANCELAR y el faltante desaparecía.
        · **Sanas y averiadas en la misma línea** (`cantidad_averiada`).
        · **Lo sano entra a la zona DEVOLUCION, no a picking**: no es vendible
          hasta que la NC esté aprobada en Siesa (`liberar_reingreso`).
        · **El tope cuenta las devoluciones anteriores** de la misma factura.
        """
        devolucion = (DevolucionCliente.query
                      .filter_by(id=devolucion_id)
                      .with_for_update()
                      .first())
        if not devolucion:
            raise ValueError(f'Devolución {devolucion_id} no existe')

        if devolucion.estado == EstadoDevolucionCliente.CONFIRMADA:
            job_existente = SiesaJob.query.filter_by(
                referencia_tipo='DevolucionCliente', referencia_id=devolucion.id,
            ).filter(SiesaJob.estado.in_([
                EstadoSiesaJob.PENDIENTE, EstadoSiesaJob.PROCESANDO,
                EstadoSiesaJob.REINTENTANDO, EstadoSiesaJob.COMPLETADO,
            ])).first()
            if job_existente:
                return devolucion
            logger.warning(
                '[DEV_CLIENTE] Devolución %s CONFIRMADA sin SiesaJob activo — no se re-encola '
                'automáticamente, requiere revisión manual', devolucion.codigo
            )
            return devolucion
        if devolucion.estado == EstadoDevolucionCliente.FALTANTE_TOTAL:
            return devolucion

        if devolucion.estado not in EstadoDevolucionCliente.ACTIVAS:
            raise ValueError(f'No se puede confirmar en estado {devolucion.estado}')

        # Lo que declaró el conductor NO se pisa: si la línea no lo trae (una
        # devolución de ruta armada antes de m041flfugas), lo vigente ANTES de
        # que recepción ajuste es lo declarado — se congela acá. Solo en las
        # armadas por el flujo viejo (sin `declaracion_conductor`): en las
        # nuevas, un `cantidad_declarada` NULL es deliberado — la línea de un
        # producto de doble unidad que el conductor declaró por referencia, o
        # una PARCIAL que no declaró nada. Congelar ahí inventaría un dato.
        if devolucion.recaudo_entrega_id and devolucion.declaracion_conductor is None:
            for linea in devolucion.lineas:
                if linea.cantidad_declarada is None:
                    linea.cantidad_declarada = linea.cantidad_devuelta

        if devolucion.es_de_ruta:
            # Las líneas de ruta nacieron sin red (la parada se confirma sin
            # señal): acá se amarran a las filas reales de la factura. Lo que
            # impida contarla (referencia sin producto WMS, factura no
            # localizada) se guarda y se muestra: un error visible, no un log.
            from app.services import devolucion_ruta as _dr
            _dr.vincular_a_factura(devolucion, gateway=connekta)
            if devolucion.problema_factura:
                _problema = devolucion.problema_factura
                db.session.commit()
                raise ValueError(_problema)

        if lineas_ajustadas:
            por_linea = {int(l['linea_id']): l for l in lineas_ajustadas
                         if l.get('linea_id') is not None}
            por_producto = {int(l['producto_id']): l for l in lineas_ajustadas
                            if l.get('linea_id') is None and l.get('producto_id') is not None}
            _cambios = []
            for linea in devolucion.lineas:
                ajuste = por_linea.get(linea.id) or por_producto.get(linea.producto_id)
                if not ajuste:
                    continue
                cant_fact = float(linea.cantidad_facturada or 0)
                cant_nueva = max(0.0, min(float(ajuste.get('cantidad_devuelta') or 0), cant_fact))
                if ajuste.get('cantidad_averiada') is not None:
                    averiada = max(0.0, min(float(ajuste.get('cantidad_averiada') or 0), cant_nueva))
                elif 'es_averiado' in ajuste:
                    averiada = cant_nueva if ajuste.get('es_averiado') else 0.0
                else:
                    averiada = min(linea.averiadas(), cant_nueva)
                if abs(cant_nueva - float(linea.cantidad_devuelta or 0)) > 1e-9:
                    _cambios.append({'linea_id': linea.id, 'codigo_siesa': linea.codigo_siesa,
                                     'declarado': (float(linea.cantidad_declarada)
                                                   if linea.cantidad_declarada is not None else None),
                                     'antes': float(linea.cantidad_devuelta or 0),
                                     'contado': cant_nueva})
                linea.cantidad_devuelta = cant_nueva
                linea.cantidad_averiada = averiada
                linea.es_averiado = bool(cant_nueva > 0 and averiada >= cant_nueva)
            if _cambios:
                # La corrección de recepción sobre lo declarado queda en la
                # bitácora: quién contó distinto, cuánto y en qué línea.
                from app.services.bitacora import registrar_accion
                registrar_accion(
                    'EDITAR', devolucion, usuario_id=recepcionista_id,
                    entidad_codigo=devolucion.codigo,
                    motivo='Recepción contó distinto de lo declarado',
                    antes={'lineas': [{k: c[k] for k in ('linea_id', 'codigo_siesa', 'declarado', 'antes')}
                                      for c in _cambios]},
                    despues={'lineas': [{k: c[k] for k in ('linea_id', 'codigo_siesa', 'contado')}
                                        for c in _cambios]})

        # Toda línea contada lleva `cantidad_averiada` escrita (0 incluido): es
        # la marca estructural de «contada con la política de m045devol», la
        # que los invariantes usan para no confundir el histórico con un
        # reingreso que se saltó la zona DEVOLUCION.
        for linea in devolucion.lineas:
            if linea.cantidad_averiada is None:
                linea.cantidad_averiada = linea.averiadas()

        if not any(float(l.cantidad_devuelta or 0) > 0 for l in devolucion.lineas):
            if not devolucion.es_de_ruta:
                raise ValueError(
                    'Ajustaste todas las líneas a 0 — si el cliente no devolvió nada, '
                    'cancela esta devolución en vez de confirmarla vacía'
                )
            # ── FALTANTE TOTAL: el conductor dijo que volvía y no volvió nada ──
            # No es un error de captura ni algo que se cancele: es el dato. Se
            # mide (declarado − contado = todo) y no toca inventario ni Siesa.
            # La factura queda con su saldo en cartera; el RC que esperaba la
            # NC sale por lo cobrado (`devolucion_ruta.nc_no_llegara`).
            DevolucionClienteService._marcar_contada(devolucion, recepcionista_id,
                                                     EstadoDevolucionCliente.FALTANTE_TOTAL)
            db.session.commit()
            from app.services import devolucion_ruta as _dr
            _dr.destrabar_rc_de(devolucion)
            logger.warning('[DEV_CLIENTE] Devolución %s contada en CERO — FALTANTE TOTAL '
                           '(pedido %s)', devolucion.codigo, devolucion.numero_pedido_siesa)
            return devolucion

        if devolucion.es_de_ruta:
            sin_linea = [l.codigo_siesa for l in devolucion.lineas
                         if float(l.cantidad_devuelta or 0) > 0 and not l.f470_rowid]
            if sin_linea:
                raise ValueError(
                    f'{", ".join(sin_linea)}: no está en la factura '
                    f'{devolucion.tipo_docto_fe}-{devolucion.consec_fe}. No puede entrar a la '
                    f'nota crédito — déjala en 0 (si volvió, es mercancía de otro documento).')

        # [C6] Re-GET a Siesa ANTES de tomar row-locks de inventario — revalida
        # que lo contado no exceda lo facturado ahora mismo (el dato pudo cambiar
        # entre la búsqueda y la confirmación).
        rowids_data = connekta.get_rowids_factura(devolucion.tipo_docto_fe, devolucion.consec_fe)
        if not rowids_data:
            raise ValueError(
                f'No se pudo revalidar la factura {devolucion.tipo_docto_fe}-{devolucion.consec_fe} '
                'en Siesa — no se confirma la devolución'
            )
        facturado_actual = {}
        facturado_por_rowid = {}
        for row in rowids_data:
            ref = (row.get('f120_referencia') or '').strip()
            if ref:
                facturado_actual[ref] = facturado_actual.get(ref, 0.0) + float(row.get('f470_cant_base') or 0)
            if row.get('f470_rowid'):
                facturado_por_rowid[str(row.get('f470_rowid')).strip()] = float(row.get('f470_cant_base') or 0)

        ya = ya_devuelto_de_la_factura(devolucion.tipo_docto_fe, devolucion.consec_fe,
                                       excluir_id=devolucion.id)
        for linea in devolucion.lineas:
            if float(linea.cantidad_devuelta or 0) <= 0:
                continue  # la recepcionista la ajustó a 0 — no hay nada que validar ni ingresar
            cant_fact_actual = (facturado_por_rowid.get(str(linea.f470_rowid))
                                if linea.f470_rowid and str(linea.f470_rowid) in facturado_por_rowid
                                else facturado_actual.get(linea.codigo_siesa))
            if cant_fact_actual is None:
                raise ValueError(
                    f'Producto {linea.codigo_siesa} ya no aparece en la factura '
                    f'{devolucion.tipo_docto_fe}-{devolucion.consec_fe} en Siesa'
                )
            if float(linea.cantidad_devuelta) > cant_fact_actual:
                raise ValueError(
                    f'Producto {linea.codigo_siesa}: cantidad devuelta ({linea.cantidad_devuelta}) '
                    f'supera la cantidad facturada actual en Siesa ({cant_fact_actual})'
                )
            _ya = _ya_devuelto_de_linea(ya, linea.f470_rowid, linea.codigo_siesa)
            if _ya and float(linea.cantidad_devuelta) + _ya > cant_fact_actual + 1e-9:
                raise ValueError(
                    f'Producto {linea.codigo_siesa}: de esta factura ya se devolvieron {_ya:g} '
                    f'en otra devolución contada — con {float(linea.cantidad_devuelta):g} más se '
                    f'pasa de lo facturado ({cant_fact_actual:g})')

        # Ingresar al inventario — locks adquiridos DESPUÉS del GET a Siesa.
        # Lo SANO a la zona DEVOLUCION (no vendible hasta la NC aprobada); lo
        # averiado al bin AVERIADOS. Nunca a picking: eso es `liberar_reingreso`.
        for linea in devolucion.lineas:
            if float(linea.cantidad_devuelta or 0) <= 0:
                continue
            sanas, averiadas = linea.sanas(), linea.averiadas()
            if sanas > 0:
                ub = DevolucionClienteService._ubicacion_devolucion(devolucion.almacen_id)
                linea.ubicacion_id = ub.id
                DevolucionClienteService._entrar(
                    ub, linea, sanas, devolucion, recepcionista_id,
                    tipo='DEVOLUCION_CLIENTE', clave=f'DEVC-{devolucion.id}-{linea.id}')
            if averiadas > 0:
                ub_av = DevolucionClienteService._resolver_ubicacion(
                    producto_id=linea.producto_id, almacen_id=devolucion.almacen_id,
                    es_averiado=True)
                if sanas <= 0:
                    linea.ubicacion_id = ub_av.id
                DevolucionClienteService._entrar(
                    ub_av, linea, averiadas, devolucion, recepcionista_id,
                    tipo='DEVOLUCION_CLIENTE_AVERIADO',
                    clave=(f'DEVC-{devolucion.id}-{linea.id}' if sanas <= 0
                           else f'DEVC-{devolucion.id}-{linea.id}-AV'))

        DevolucionClienteService._marcar_contada(devolucion, recepcionista_id,
                                                 EstadoDevolucionCliente.CONFIRMADA)

        # [P8] SiesaJob creado ANTES del commit final — atómico con el inventario.
        #
        # `f470_rowid` va en el payload porque es **la clave del cruce** de
        # `_construir_lineas_nc`, no un adorno de diagnóstico. Con solo la
        # referencia, un producto de doble unidad —la misma referencia facturada
        # en dos líneas, PQ y UND, cada una con su rowid y su valor— matcheaba
        # las DOS filas de la factura con el mismo item y las metía a la nota
        # crédito con la cantidad completa: `F353_VLR_CRUCE` de más contra la
        # cartera (Regla 21) y reingreso de inventario que nadie devolvió.
        #
        # **Línea mixta (sanas + averiadas):** la NC va entera a la bodega de la
        # factura — una sola línea por `f470_rowid_movto`, la forma verificada
        # del 251126; dos líneas con el mismo rowid y distinta bodega nunca se
        # probaron contra Siesa. Lo averiado se traslada a AV1 con el 142951
        # cuando la NC se aprueba (`liberar_reingreso`), que es cuando Siesa
        # recién tiene esas unidades. `es_averiado` en el payload solo cuando
        # la línea ENTERA volvió averiada (la NC la manda directo a AV1, como
        # siempre).
        items_devueltos = [
            {
                'codigo': l.codigo_siesa,
                'cantidad_devuelta': float(l.cantidad_devuelta),
                'es_averiado': bool(l.averiadas() > 0 and l.sanas() <= 0),
                'cantidad_averiada': l.averiadas(),
                'f470_rowid': l.f470_rowid,
                'f470_id_unidad_medida': l.f470_id_unidad_medida,
            }
            for l in devolucion.lineas
            if float(l.cantidad_devuelta or 0) > 0
        ]
        SiesaJob.encolar(
            tipo='NOTA_CREDITO_DEVOLUCION_CLIENTE',
            payload={
                'devolucion_id': devolucion.id,
                'tipo_docto_fe': devolucion.tipo_docto_fe,
                'consec_fe': devolucion.consec_fe,
                'es_total': devolucion.es_total,
                'items_devueltos': items_devueltos,
                'notas': devolucion.observaciones or '',
            },
            referencia_tipo='DevolucionCliente',
            referencia_id=devolucion.id,
        )

        db.session.commit()

        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception as e_dlq:
            logger.warning('[DEV_CLIENTE] disparar_dlq_inmediato falló (DLQ scheduler lo recogerá): %s', e_dlq)

        logger.info('[DEV_CLIENTE] Devolución %s confirmada — NC encolada', devolucion.codigo)
        return devolucion

    @staticmethod
    def _marcar_contada(devolucion, recepcionista_id, estado) -> None:
        cambiar_estado(devolucion, estado)
        devolucion.recepcionista_id = recepcionista_id
        if devolucion.es_de_ruta and devolucion.fecha_llegada is None:
            # Contarla es prueba de que llegó: si nadie cerró la llegada del
            # camión antes, la llegada es ahora.
            devolucion.fecha_llegada = datetime.utcnow()

    @staticmethod
    def _entrar(ubicacion, linea, cantidad: float, devolucion, usuario_id: int,
                tipo: str, clave: str) -> None:
        """Suma `cantidad` de la línea en `ubicacion` y escribe su movimiento."""
        reg = UbicacionProducto.query.filter_by(
            ubicacion_id=ubicacion.id, producto_id=linea.producto_id,
        ).with_for_update().first()
        saldo_antes = reg.cantidad if reg else 0
        if reg:
            reg.cantidad += float(cantidad)
            reg.row_version += 1
        else:
            reg = UbicacionProducto(
                ubicacion_id=ubicacion.id,
                producto_id=linea.producto_id,
                cantidad=float(cantidad),
                fecha_ingreso=datetime.utcnow(),
            )
            db.session.add(reg)
            db.session.flush()
        db.session.add(MovimientoInventario(
            producto_id=linea.producto_id,
            ubicacion_id=ubicacion.id,
            almacen_id=devolucion.almacen_id,
            tipo=tipo,
            cantidad=float(cantidad),
            saldo_antes=saldo_antes,
            saldo_despues=reg.cantidad,
            motivo=f'Devolución cliente {devolucion.codigo} · pedido {devolucion.numero_pedido_siesa}',
            numero_documento=devolucion.numero_pedido_siesa,
            usuario_id=usuario_id,
            idempotency_key=clave,
        ))

    @staticmethod
    def cancelar(devolucion_id: int, recepcionista_id: int, motivo: str = None,
                 *, por_el_sistema: bool = False) -> DevolucionCliente:
        """Solo antes de contar (EN_CAMION/ABIERTA) — nada tocó stock ni Siesa.

        El motivo es obligatorio y queda en la bitácora: una devolución que el
        conductor declaró y nadie recibió es exactamente lo que la liquidación
        necesita poder explicar. La ruta la restringe a supervisión.

        **Si no volvió nada, esto no es lo que corresponde**: se cuenta en cero
        (FALTANTE_TOTAL, medido). Cancelar es para una devolución que no debía
        existir — la parada se reconfirmó como entregada (`por_el_sistema`), o
        se armó por error.

        Si era de una ruta con recibo de caja esperando su NC, el recibo sale
        por lo cobrado (`devolucion_ruta.nc_no_llegara`).
        """
        from app.services.bitacora import motivo_obligatorio
        motivo = motivo_obligatorio(motivo, 'cancelar una devolución de cliente')
        devolucion = db.session.get(DevolucionCliente, devolucion_id)
        if not devolucion:
            raise ValueError(f'Devolución {devolucion_id} no existe')
        if devolucion.estado not in EstadoDevolucionCliente.ACTIVAS:
            raise ValueError(
                f'Solo se puede cancelar una devolución sin contar (estado actual: '
                f'{devolucion.estado})'
            )
        from app.services.bitacora import registrar_accion, foto
        antes = foto(devolucion, ['estado', 'recepcionista_id', 'observaciones'])
        cambiar_estado(devolucion, EstadoDevolucionCliente.CANCELADA)
        # `recepcionista_id` es quién la ATENDIÓ: si ya tenía uno, cancelar no
        # lo reemplaza (quién canceló queda en la bitácora).
        if not devolucion.recepcionista_id and not por_el_sistema:
            devolucion.recepcionista_id = recepcionista_id
        devolucion.observaciones = motivo or devolucion.observaciones
        registrar_accion('CANCELAR', devolucion, usuario_id=recepcionista_id,
                         motivo=motivo, antes=antes,
                         despues=foto(devolucion, ['estado', 'recepcionista_id', 'observaciones']))
        db.session.commit()
        if devolucion.es_de_ruta:
            from app.services import devolucion_ruta as _dr
            _dr.destrabar_rc_de(devolucion)
        return devolucion

    @staticmethod
    def listar_pendientes_de_ruta() -> list:
        """
        Devoluciones de ruta SIN CONTAR (EN_CAMION: viene en el camión;
        ABIERTA: ya en bodega). Nacen al confirmar la parada Rechazada/Parcial
        (`devolucion_ruta.sincronizar_con_parada`) con lo que declaró el
        conductor — la recepcionista cuenta y ajusta, no arma nada desde cero.
        Una por recaudo. Las más viejas primero: son las que más plata tienen
        en el aire.
        """
        devoluciones = (DevolucionCliente.query
                         .filter(DevolucionCliente.recaudo_entrega_id.isnot(None))
                         .filter(DevolucionCliente.estado.in_(EstadoDevolucionCliente.ACTIVAS))
                         .order_by(DevolucionCliente.fecha_creacion.desc())
                         .all())
        return [d.to_dict() for d in devoluciones]

    @staticmethod
    def listar_pendientes_aprobacion_nc() -> list:
        """
        NC ya creadas en Siesa (Elaboración, CLAUDE.md Regla #21) que todavía
        no constan aprobadas. Desde m045devol el cron `devolucion_nc_verificador`
        lee `f350_ind_estado` y las marca solo; esta lista es lo que falta.
        """
        devoluciones = DevolucionCliente.query.filter_by(
            siesa_nc_triggered=True,
            nc_aprobada_siesa=False,
        ).order_by(DevolucionCliente.siesa_nc_triggered_at.asc()).all()
        return [d.to_dict() for d in devoluciones]

    @staticmethod
    def marcar_nc_aprobada(devolucion_id: int, usuario_id: int,
                           motivo: str = None) -> DevolucionCliente:
        """**El respaldo manual**: alguien declara que aprobó la NC en Siesa.

        Hasta el 2026-09-24 era un clic sin verificar y lo único que decía que la
        NC estaba aprobada. Ahora la verificación es del cron (lee
        `f350_ind_estado`, `FuenteAprobacionNC.SIESA`); esto queda para cuando
        la consulta no alcanza (NC fuera de la ventana de 100, consecutivo
        desconocido, cron apagado) y **deja rastro**: motivo obligatorio y
        bitácora FORZAR — se salta la verificación.

        Al aprobarse, lo sano sale de la zona DEVOLUCION a picking
        (`liberar_reingreso`).
        """
        from app.services.bitacora import foto, motivo_obligatorio, registrar_accion
        motivo = motivo_obligatorio(motivo, 'marcar una nota crédito como aprobada sin '
                                            'verificarla en Siesa')
        devolucion = db.session.get(DevolucionCliente, devolucion_id)
        if not devolucion:
            raise ValueError(f'Devolución {devolucion_id} no existe')
        if not devolucion.siesa_nc_triggered:
            raise ValueError('Esta devolución todavía no tiene una NC creada en Siesa')
        if devolucion.nc_aprobada_siesa:
            return devolucion
        _campos = ['nc_aprobada_siesa', 'nc_aprobada_siesa_at', 'nc_aprobada_siesa_por',
                   'nc_aprobada_fuente']
        antes = foto(devolucion, _campos)
        DevolucionClienteService._aprobar(devolucion, FuenteAprobacionNC.MANUAL, usuario_id)
        registrar_accion('FORZAR', devolucion, usuario_id=usuario_id,
                         motivo=f'NC marcada aprobada a mano (sin verificar en Siesa): {motivo}',
                         antes=antes, despues=foto(devolucion, _campos))
        DevolucionClienteService.liberar_reingreso(devolucion, usuario_id)
        db.session.commit()
        return devolucion

    @staticmethod
    def marcar_nc_aprobada_desde_siesa(devolucion: DevolucionCliente) -> dict:
        """El cron leyó `f350_ind_estado = 1`: la NC está aprobada en Siesa.

        No hace commit (lo hace el cron, una vez por lote).
        """
        if devolucion.nc_aprobada_siesa:
            return {'ya_estaba': True}
        DevolucionClienteService._aprobar(devolucion, FuenteAprobacionNC.SIESA, None)
        return DevolucionClienteService.liberar_reingreso(devolucion, None)

    @staticmethod
    def _aprobar(devolucion, fuente: str, usuario_id) -> None:
        devolucion.nc_aprobada_siesa = True
        devolucion.nc_aprobada_siesa_at = datetime.utcnow()
        devolucion.nc_aprobada_siesa_por = usuario_id
        devolucion.nc_aprobada_fuente = fuente

    @staticmethod
    def liberar_reingreso(devolucion: DevolucionCliente, usuario_id=None) -> dict:
        """Lo SANO sale de la zona DEVOLUCION hacia inventario vendible.

        **La única puerta de una devolución al inventario vendible**, y solo con
        la NC aprobada (trinquete AST: `_destino_vendible` tiene un solo
        llamador, éste). Mientras la NC esté en Elaboración, Siesa no tiene esas
        unidades (la aprobación es la que las reingresa, Regla 21): venderlas
        antes es vender lo que el ERP sigue atribuyendo al cliente.

        Por línea: mueve `min(sanas, lo que hay en el bin)` al slot fijo del
        producto o a la ubicación óptima, con dos `MovimientoInventario`
        (salida del bin, entrada al destino). Lo averiado de una línea MIXTA
        —la NC la mandó entera a la bodega de la factura— se traslada a AV1 en
        Siesa ahora (`encolar_traslado_averias`), que es cuando Siesa ya tiene
        esas unidades. No hace commit.
        """
        if not devolucion.nc_aprobada_siesa:
            raise ValueError(
                f'La NC de la devolución {devolucion.codigo} no está aprobada: lo devuelto '
                f'sigue en la zona de devoluciones, no vendible')
        if devolucion.reingreso_liberado_at is not None:
            return {'ya_liberada': True, 'movidas': 0}
        movidas, faltaron = 0.0, []
        for linea in devolucion.lineas:
            sanas = linea.sanas()
            ub = linea.ubicacion
            if sanas <= 0 or ub is None or linea.ubicacion_liberada_id is not None:
                continue
            if getattr(ub, 'tipo_zona', None) != _ZONA_DEVOLUCION:
                # Reingreso anterior a m045devol: fue directo a picking. No hay
                # nada que mover (y ya se pudo vender).
                continue
            reg_bin = UbicacionProducto.query.filter_by(
                ubicacion_id=ub.id, producto_id=linea.producto_id).with_for_update().first()
            disponible = float(reg_bin.cantidad or 0) if reg_bin else 0.0
            mover = min(sanas, disponible)
            if mover < sanas:
                faltaron.append({'linea_id': linea.id, 'codigo': linea.codigo_siesa,
                                 'esperado': sanas, 'en_el_bin': disponible})
            if mover <= 0:
                continue
            destino = DevolucionClienteService._destino_vendible(
                linea.producto_id, devolucion.almacen_id)
            saldo_bin = reg_bin.cantidad
            # Traslado entre zonas de la misma bodega: la contrapartida es el
            # `UbicacionProducto` del destino, en la misma transacción.
            reg_bin.cantidad = saldo_bin - mover
            reg_bin.row_version = (reg_bin.row_version or 0) + 1
            db.session.add(MovimientoInventario(
                producto_id=linea.producto_id, ubicacion_id=ub.id,
                almacen_id=devolucion.almacen_id, tipo='LIBERACION_DEVOLUCION',
                cantidad=-int(round(mover)), saldo_antes=saldo_bin,
                saldo_despues=reg_bin.cantidad,
                motivo=f'NC aprobada · devolución {devolucion.codigo} → {destino.codigo}',
                numero_documento=devolucion.numero_pedido_siesa, usuario_id=usuario_id,
                idempotency_key=f'DEVC-LIB-{devolucion.id}-{linea.id}-S'))
            reg_dest = UbicacionProducto.query.filter_by(
                ubicacion_id=destino.id, producto_id=linea.producto_id).with_for_update().first()
            antes_dest = reg_dest.cantidad if reg_dest else 0
            if reg_dest:
                reg_dest.cantidad += mover
                reg_dest.row_version = (reg_dest.row_version or 0) + 1
            else:
                reg_dest = UbicacionProducto(ubicacion_id=destino.id,
                                             producto_id=linea.producto_id,
                                             cantidad=mover, fecha_ingreso=datetime.utcnow())
                db.session.add(reg_dest)
                db.session.flush()
            db.session.add(MovimientoInventario(
                producto_id=linea.producto_id, ubicacion_id=destino.id,
                almacen_id=devolucion.almacen_id, tipo='LIBERACION_DEVOLUCION',
                cantidad=int(round(mover)), saldo_antes=antes_dest,
                saldo_despues=reg_dest.cantidad,
                motivo=f'NC aprobada · devolución {devolucion.codigo} desde {ub.codigo}',
                numero_documento=devolucion.numero_pedido_siesa, usuario_id=usuario_id,
                idempotency_key=f'DEVC-LIB-{devolucion.id}-{linea.id}-E'))
            linea.ubicacion_liberada_id = destino.id
            movidas += mover

        traslados_averias = DevolucionClienteService._trasladar_averias_de_lineas_mixtas(devolucion)
        devolucion.reingreso_liberado_at = datetime.utcnow()
        if faltaron:
            logger.warning('[DEV_CLIENTE] Liberación de %s: el bin de devoluciones tenía menos '
                           'de lo contado: %s', devolucion.codigo, faltaron)
        return {'movidas': movidas, 'faltaron': faltaron,
                'traslados_averias': traslados_averias}

    @staticmethod
    def _trasladar_averias_de_lineas_mixtas(devolucion) -> int:
        """Una línea que volvió con sanas Y averiadas mandó la NC entera a la
        bodega de la factura. Con la NC aprobada, Siesa tiene esas unidades ahí:
        las averiadas se trasladan a AV1 (142951), anclado al movimiento que las
        metió al bin AVERIADOS."""
        from app.services.siesa_job_service import encolar_traslado_averias
        n = 0
        for linea in devolucion.lineas:
            if linea.averiadas() <= 0 or linea.sanas() <= 0:
                continue
            mov = MovimientoInventario.query.filter_by(
                idempotency_key=f'DEVC-{devolucion.id}-{linea.id}-AV').first()
            if encolar_traslado_averias(
                    mov, linea.codigo_siesa, int(round(linea.averiadas())),
                    bodega_del_almacen=(linea.f150_id_bodega or connekta.bodega),
                    referencia=f'Devolución {devolucion.codigo} (línea mixta)'):
                n += 1
        return n

    @staticmethod
    def _ubicacion_devolucion(almacen_id: int) -> Ubicacion:
        """El bin `DEVOLUCIONES` del almacén (find-or-create), zona DEVOLUCION.

        Mismo patrón que el de averías: los campos los pone la política
        (`picking_service.campos_ubicacion_devolucion`). Si ya existía marcado
        como vendible, se corrige: si no, lo devuelto con la NC sin aprobar
        saldría al FEFO.
        """
        from app.services.picking_service import campos_ubicacion_devolucion
        ub = Ubicacion.query.filter_by(codigo=_UBICACION_DEVOLUCIONES, almacen_id=almacen_id).first()
        if not ub:
            ub = Ubicacion(codigo=_UBICACION_DEVOLUCIONES, almacen_id=almacen_id, activo=True,
                           origen='MANUAL', **campos_ubicacion_devolucion())
            db.session.add(ub)
            db.session.flush()
        elif ub.tipo_zona != _ZONA_DEVOLUCION:
            for _campo, _valor in campos_ubicacion_devolucion().items():
                setattr(ub, _campo, _valor)
            logger.warning('[DEV_CLIENTE] El bin %s (id=%s) no estaba en la zona %s — '
                           'corregido', ub.codigo, ub.id, _ZONA_DEVOLUCION)
        return ub

    @staticmethod
    def _destino_vendible(producto_id: int, almacen_id: int) -> Ubicacion:
        """A dónde va lo sano cuando la NC se aprueba: el slot fijo del producto
        (Layout ↔ Picking) o la ubicación óptima (vendible por construcción).

        **Un solo llamador: `liberar_reingreso`** (trinquete AST). Es la única
        forma en que una devolución llega a inventario vendible.
        """
        slot_fijo = Ubicacion.query.filter_by(
            producto_asignado_id=producto_id, almacen_id=almacen_id, activo=True,
        ).first()
        if slot_fijo and _es_ubicacion_vendible(slot_fijo):
            return slot_fijo
        producto = db.session.get(Producto, producto_id)
        clasificacion = producto.clasificacion_abc if producto else 'C'
        ubicacion = RecepcionService._buscar_ubicacion_optima(
            almacen_id=almacen_id, clasificacion_abc=clasificacion,
        )
        if not ubicacion or not _es_ubicacion_vendible(ubicacion):
            raise ValueError(
                f'No hay ninguna ubicación vendible activa en el almacén {almacen_id} para '
                f'liberar la devolución')
        return ubicacion

    @staticmethod
    def _resolver_ubicacion(producto_id: int, almacen_id: int, es_averiado: bool) -> Ubicacion:
        """
        Put-away de lo devuelto: si es_averiado, bucket AVERIADOS (find-or-create,
        igual que devolucion_service.py). Si no, el bin DEVOLUCIONES (zona
        DEVOLUCION, no vendible) — desde m045devol, nunca picking directo.
        """
        if es_averiado:
            ub = Ubicacion.query.filter_by(codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id).first()
            if not ub:
                # Los campos los pone la política única (`picking_service.
                # campos_ubicacion_averias`), no este archivo.
                #
                # **Este es el escritor VIVO.** El arreglo del 2026-08-20 cayó
                # primero en `devolucion_service.py`, que está DEPRECATED desde
                # el 2026-07-28 y no tiene ningún caller de producción: el bin
                # se seguía creando mal por acá. La migración `m016` limpia los
                # que ya existen; sin esta línea, este camino los volvía a crear
                # con `tipo_zona` en el default del modelo ('GENERAL', o sea
                # VENDIBLE) y la mercancía recién declarada averiada volvía a
                # entrar al FEFO de pedidos de cliente.
                #
                # Con dos implementaciones y una sin caller, el arreglo cae en
                # la muerta y los tests no avisan: los de la muerta pasan.
                ub = Ubicacion(
                    codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id,
                    activo=True,
                    **_campos_ubicacion_averias(),
                )
                db.session.add(ub)
                try:
                    db.session.flush()
                except Exception:
                    db.session.rollback()
                    ub = Ubicacion.query.filter_by(codigo=_UBICACION_AVERIADOS, almacen_id=almacen_id).first()
                    if not ub:
                        raise
            elif _es_ubicacion_vendible(ub):
                # Bin de averías que YA existía sin marcar. `m016` corrige las
                # filas de producción, pero una base que no la haya corrido
                # todavía —o una copia— seguiría despachando esta mercancía a
                # clientes.
                #
                # **El docstring de m016 promete esta red** («el propio servicio
                # la vuelve a corregir la próxima vez que alguien confirme una
                # avería ahí») para justificar un `WHERE` estrecho. La rama se
                # escribió el 2026-08-20 en `devolucion_service.py`, que está
                # DEPRECATED y sin caller: la promesa no existía en el camino
                # vivo. Segunda mitad del mismo gemelo muerto.
                #
                # Misma condición estrecha del backfill: solo el bin de averías
                # de este servicio, solo cuando estamos metiéndole una avería.
                # Marcar de más sacaría del FEFO stock vendible — el error caro
                # en la dirección contraria.
                for _campo, _valor in _campos_ubicacion_averias().items():
                    setattr(ub, _campo, _valor)
                logger.warning(
                    '[DEV_CLIENTE] Ubicación %s (id=%s) estaba marcada como '
                    'vendible — corregida a zona de averías. Su stock estuvo '
                    'disponible para pedidos de cliente.',
                    ub.codigo, ub.id,
                )
            return ub

        # Lo SANO ya no va al slot de picking (m045devol): va a la zona
        # DEVOLUCION, no vendible, hasta que la NC esté aprobada. El camino a
        # picking es uno solo: `liberar_reingreso` → `_destino_vendible`.
        return DevolucionClienteService._ubicacion_devolucion(almacen_id)
