"""
Servicio de Recepción OC para Puntos de Venta.
Orquesta la lógica específica de tienda y delega a RecepcionService
para las operaciones compartidas (crear, iniciar, escanear, confirmar).

SOLID: este servicio NO toca recepcion_service.py — lo consume por composición.
"""
import logging
from app.extensions import db
from app.models.producto import Producto
from app.models.recepcion import RecepcionMercancia
from app.services.connekta_gateway import connekta
from app.services.recepcion_service import RecepcionService

logger = logging.getLogger(__name__)


def _buscar_producto(codigo):
    return (Producto.query.filter_by(codigo=codigo).first() or
            Producto.query.filter_by(codigo_siesa=codigo).first() or
            Producto.query.filter_by(codigo_barras=codigo).first() or
            Producto.query.filter_by(codigo_barras_empaque=codigo).first())


class TiendaOCService:

    @staticmethod
    def listar_ocs(co: str, bodega: str) -> dict:
        """
        Consulta OCs aprobadas en Siesa y filtra por CO/bodega de la tienda.
        Misma API de Connekta que usa el recepcionista, diferente filtro.
        """
        resultado = connekta.get_ordenes_compra_aprobadas()

        if resultado.get('simulado'):
            return resultado

        items_raw = resultado.get('detalle', {}).get('Table', [])
        items_raw = [
            r for r in items_raw
            if r.get('f420_id_co', '').strip() == co
            and r.get('f150_id', '').strip() == bodega
        ]

        ordenes = {}
        for row in items_raw:
            try:
                cant_pedida = float(row.get('f421_cant_pedida', 0))
                cant_recibida = float(row.get('f421_cant_entrada', 0))
                cant_pendiente = cant_pedida - cant_recibida
                if cant_pendiente <= 0:
                    continue

                tipo_docto = row.get('f420_id_tipo_docto', '').strip()
                consec_docto = str(row.get('f420_consec_docto', ''))
                numero_oc = f"{tipo_docto}{consec_docto}"
                item_codigo = row.get('f120_referencia', '').strip()
                prod = _buscar_producto(item_codigo)

                if numero_oc not in ordenes:
                    ordenes[numero_oc] = {
                        'numero_oc': numero_oc,
                        'tipo_docto': tipo_docto,
                        'consec_docto': consec_docto,
                        'co': row.get('f420_id_co', '').strip(),
                        'proveedor': row.get('f200_razon_social_prov', ''),
                        'proveedor_codigo': (row.get('f200_nit_prov', '') or row.get('f200_id_prov', '')).strip(),
                        'sucursal_prov': row.get('f202_id_sucursal_prov', '').strip(),
                        'cond_pago': row.get('f420_id_cond_pago', '').strip(),
                        'items': []
                    }
                ordenes[numero_oc]['items'].append({
                    'item_codigo': item_codigo,
                    'item_descripcion': row.get('f120_descripcion', ''),
                    'producto_id': prod.id if prod else None,
                    'producto_nombre_wms': prod.nombre if prod else None,
                    'cantidad_ordenada': cant_pedida,
                    'cantidad_pendiente': cant_pendiente,
                    'factor_conversion': int(float(row.get('f421_factor') or 1)),
                })
            except (ValueError, TypeError):
                continue

        numeros_oc = list(ordenes.keys())
        recepciones_wms = RecepcionMercancia.query.filter(
            RecepcionMercancia.numero_oc_siesa.in_(numeros_oc)
        ).filter(RecepcionMercancia.estado.notin_(['CANCELADA'])).all()
        estado_por_oc = {r.numero_oc_siesa: r.estado for r in recepciones_wms}
        id_por_oc = {r.numero_oc_siesa: r.id for r in recepciones_wms}

        for numero_oc, oc in ordenes.items():
            oc['recepcion_wms_estado'] = estado_por_oc.get(numero_oc)
            oc['recepcion_wms_id'] = id_por_oc.get(numero_oc)

        lista = sorted(ordenes.values(), key=lambda o: int(o.get('consec_docto') or 0), reverse=True)
        return {'ordenes': lista, 'total': len(lista)}

    @staticmethod
    def resolver_almacen(bodega_siesa_id: str, co_siesa: str):
        """
        Busca o crea el Almacen + Ubicacion para una tienda.
        Se ejecuta una sola vez por tienda (la primera recepción OC).
        """
        from app.models.almacen import Almacen
        from app.models.ubicacion import Ubicacion

        almacen = Almacen.query.filter_by(bodega_siesa_id=bodega_siesa_id).first()
        if almacen:
            return almacen

        almacen = Almacen(
            codigo=f'PV-{bodega_siesa_id}',
            nombre=f'Punto de Venta {bodega_siesa_id}',
            bodega_siesa_id=bodega_siesa_id,
            centro_op_siesa=co_siesa,
            activo=True,
        )
        db.session.add(almacen)
        db.session.flush()

        ubicacion = Ubicacion(
            almacen_id=almacen.id,
            codigo=f'{bodega_siesa_id}-GENERAL',
            tipo='inventario',
            tipo_zona='PICKING',
            activo=True,
        )
        db.session.add(ubicacion)
        db.session.commit()

        logger.info(
            f'[TIENDA-OC] Auto-creado Almacen id={almacen.id} ({bodega_siesa_id}) '
            f'+ Ubicacion {ubicacion.codigo}'
        )
        return almacen

    @staticmethod
    def iniciar_recepcion(oc_data: dict, usuario) -> RecepcionMercancia:
        """
        Orquesta la creación de recepción OC para una tienda:
        1. Resolver almacen_id
        2. Validar tipo proveedor
        3. Actualizar factor_conversion en productos
        4. Delegar a RecepcionService.crear_recepcion()
        5. Delegar a RecepcionService.iniciar()
        """
        almacen = TiendaOCService.resolver_almacen(
            usuario.bodega_siesa_id, usuario.siesa_co_id
        )

        items_validos = [i for i in oc_data.get('items', []) if i.get('producto_id')]
        if not items_validos:
            raise ValueError('Ningún producto de la OC está registrado en el WMS')

        nit_proveedor = (oc_data.get('proveedor_codigo') or '').strip()
        if nit_proveedor:
            val = connekta.validar_tipo_proveedor(nit_proveedor)
            if val.get('configurado') is False:
                raise ValueError(val['mensaje'])
            tipo = val.get('tipo_proveedor') or ''
            if tipo and tipo not in ('0001', '0002'):
                raise ValueError(
                    f'El proveedor NIT {nit_proveedor} tiene Tipo de Proveedor {tipo}, '
                    f'el cual no tiene cuenta de Pasivo Estimado configurada. '
                    f'Solo los tipos 0001 (Nacionales) y 0002 (Extranjero) pueden '
                    f'generar entradas de inventario.'
                )

        existente = RecepcionMercancia.query.filter_by(
            numero_oc_siesa=oc_data['numero_oc']
        ).filter(RecepcionMercancia.estado.notin_(['CANCELADA'])).first()

        if existente:
            if existente.estado == 'EN_PROCESO':
                return existente
            if existente.estado == 'CONFIRMADA':
                raise ValueError(
                    f'La OC {oc_data["numero_oc"]} ya fue recepcionada y confirmada '
                    f'(recepción {existente.codigo}).'
                )
            if existente.estado == 'ABIERTA':
                return RecepcionService.iniciar(existente.id, usuario.id)

        for i in items_validos:
            fc = int(float(i.get('factor_conversion') or 1))
            if fc > 1 and i.get('producto_id'):
                prod = Producto.query.get(i['producto_id'])
                if prod and (prod.factor_conversion or 1) != fc:
                    prod.factor_conversion = fc
        db.session.flush()

        items_recepcion = [{
            'producto_id': i['producto_id'],
            'cantidad_ordenada': int(i['cantidad_ordenada']),
            'tolerancia_exceso_pct': 0.0
        } for i in items_validos]

        recepcion = RecepcionService.crear_recepcion(
            numero_oc_siesa=oc_data['numero_oc'],
            almacen_id=almacen.id,
            proveedor_codigo=nit_proveedor,
            proveedor_nombre=oc_data.get('proveedor', ''),
            items=items_recepcion,
            co_oc_siesa=oc_data.get('co', usuario.siesa_co_id or ''),
            tipo_docto_oc_siesa=oc_data['tipo_docto'],
            consec_docto_oc_siesa=oc_data['consec_docto'],
            cond_pago_siesa=oc_data.get('cond_pago', ''),
            sucursal_prov_siesa=oc_data.get('sucursal_prov', ''),
            num_remision_prov=oc_data.get('num_remision_prov', ''),
        )
        recepcion = RecepcionService.iniciar(recepcion.id, usuario.id)
        return recepcion
