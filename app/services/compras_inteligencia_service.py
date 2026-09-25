"""
ComprasInteligenciaService — acuerdos marco, memoria de precios, detector de deriva.

Tres ramas:
  1. Acuerdo vigente → OC al precio pactado (70-80% volumen, 30s de aprobación)
  2. Sin acuerdo, núcleo A → RFQ a 2-3 proveedores, comparador, humano decide
  3. Cola C → precio lista proveedor preferente, sin cotizar

Detector de deriva:
  Compara el precio de las OC de Siesa (con su cantidad) vs el pactado en el
  acuerdo. Es el ÚNICO comparador OC ↔ acuerdo del repo.
  Un 2-3% de deriva no detectada son decenas de millones al año.

Calendario de vencimientos:
  Avisa 3 semanas antes de que expire un acuerdo.
  Arma la agenda de renegociación trimestral.
"""
import logging
from datetime import date, timedelta
from app.extensions import db
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)

DIAS_ALERTA_VENCIMIENTO = 21  # 3 semanas antes


def precios_de_compra_recibidos(desde, producto_ids):
    """Precios de compra de las OC de Siesa, por línea, con su cantidad.

    Devuelve `([{producto_id, precio_unitario, moneda, cantidad, fecha,
    proveedor, proveedor_codigo, oc}], fuente)`.

    Fuente: `compras_fuentes.lineas_precio_oc` — el espejo de
    `API_v2_Compras_Ordenes` (m046compras), la única lectura del precio de una
    OC. Precio y cantidad por unidad BASE, en la moneda de la OC (la conversión
    es de quien compara: `a_cop`). Cuenta toda OC no anulada desde `desde`,
    abierta o cumplida: el precio se pactó al aprobarla, antes de recibir.

    La recepción del WMS no sirve: `ItemRecepcion` guarda unidades, no el
    precio de la factura del proveedor, y el kardex trae el costo PROMEDIO.
    Sin OCs sincronizadas la lista sale vacía y `detectar_deriva` lo declara.
    """
    from app.models.producto import Producto
    from app.services.compras_fuentes import lineas_precio_oc

    ids_por_ref = {}
    for pid, ref in (db.session.query(Producto.id, Producto.codigo_siesa)
                     .filter(Producto.id.in_(list(producto_ids or [])))
                     .all()):
        if ref:
            ids_por_ref[ref.strip()] = pid
    if not ids_por_ref:
        return [], 'OC_SIESA'
    salida = []
    for l in lineas_precio_oc(list(ids_por_ref), desde=desde):
        salida.append({
            'producto_id': ids_por_ref.get(l['referencia']),
            'precio_unitario': float(l['precio_base']),
            'moneda': l['moneda'],
            'cantidad': (float(l['cantidad_base'])
                         if l['cantidad_base'] is not None else None),
            'fecha': l['fecha_oc'],
            'proveedor': l['proveedor_codigo'],
            'proveedor_codigo': l['proveedor_codigo'],
            'oc': l['oc'],
        })
    return [r for r in salida if r['producto_id'] is not None], 'OC_SIESA'


class ComprasInteligenciaService:

    @staticmethod
    def clasificar_sku_compra(producto_id: int) -> dict:
        """
        Determina la rama de compra para un SKU.

        Rama 1: tiene acuerdo marco vigente → precio pactado
        Rama 2: núcleo A/B sin acuerdo → necesita cotización
        Rama 3: cola C → precio lista, sin cotizar

        Returns: {rama, acuerdo, precios_recientes, precio_sugerido}
        """
        from app.models.acuerdo_marco import AcuerdoMarco, PrecioProveedor
        from app.models.producto import Producto

        producto = db.session.get(Producto, producto_id)
        if not producto:
            return {'error': f'Producto {producto_id} no encontrado'}

        hoy = _dia_operativo()

        # Buscar acuerdo vigente
        acuerdo = AcuerdoMarco.query.filter(
            AcuerdoMarco.producto_id == producto_id,
            AcuerdoMarco.activo == True,
            AcuerdoMarco.vigencia_desde <= hoy,
            AcuerdoMarco.vigencia_hasta >= hoy,
        ).first()

        if acuerdo:
            return {
                'rama': 1,
                'rama_nombre': 'ACUERDO_VIGENTE',
                'precio_sugerido': float(acuerdo.precio_unitario),
                'proveedor_id': acuerdo.proveedor_id,
                'acuerdo': acuerdo.to_dict(),
                'dias_para_vencer': acuerdo.dias_para_vencer,
            }

        # Sin acuerdo: ¿es A/B o C?
        clase = (producto.clasificacion_abc or 'C').upper()

        # Últimos 3 precios cotizados
        precios = PrecioProveedor.query.filter_by(
            producto_id=producto_id
        ).order_by(PrecioProveedor.fecha.desc()).limit(5).all()

        if clase in ('A', 'B'):
            return {
                'rama': 2,
                'rama_nombre': 'REQUIERE_COTIZACION',
                'clase': clase,
                'precios_recientes': [p.to_dict() for p in precios],
                'precio_sugerido': float(precios[0].precio_unitario) if precios else None,
                'nota': 'Núcleo A/B sin acuerdo — cotizar con 2-3 proveedores',
            }

        # Cola C
        mejor_precio = precios[0] if precios else None
        return {
            'rama': 3,
            'rama_nombre': 'LISTA_PROVEEDOR',
            'clase': clase,
            'precio_sugerido': float(mejor_precio.precio_unitario) if mejor_precio else None,
            'proveedor_id': mejor_precio.proveedor_id if mejor_precio else None,
            'nota': 'Cola C — precio lista del proveedor preferente, sin cotizar',
        }

    @staticmethod
    def detectar_deriva(meses: int = 3) -> dict:
        """
        Dock Lock de compras: detecta diferencia entre precio facturado y precio pactado.

        Cruza los precios de compra de las OC de Siesa
        (`precios_de_compra_recibidos` → `compras_fuentes.lineas_precio_oc`)
        contra los acuerdos marco vigentes y activos, en la misma moneda
        (`costo_service.a_cop`). Compara toda OC del SKU, sea o no del
        proveedor del acuerdo: comprarle más caro a otro también es plata de
        más. `mismo_proveedor` lo distingue (None = no se sabe).

        ANTES (D10): leía `ItemRecepcion.costo_unitario` y
        `RecepcionMercancia.fecha_recepcion`, dos columnas que NO EXISTEN. Con
        cero acuerdos devolvía la nota y nadie lo notaba; con el primer acuerdo
        registrado, `AttributeError` → 500. El WMS no guarda el precio de la
        factura del proveedor: la recepción cuenta unidades, no pesos. Mientras
        no haya fuente, la deriva lo DECLARA («sin precio de compra recibido»)
        en vez de reventar o de pintar «los precios coinciden».

        `impacto_estimado_cop` = Σ (OC − pactado) × cantidad pedida en la OC,
        solo sobrecostos: la plata de más, no un porcentaje sin volumen. Una
        línea sin cantidad en unidad base aporta 0 y se cuenta
        (`sin_cantidad`).

        Returns: {derivas, total, sobrecostos, impacto_estimado_cop, fuente_precio_compra,
                  sin_precio_de_compra, nota}
        """
        from app.models.acuerdo_marco import AcuerdoMarco
        from app.models.producto import Producto
        from app.services.costo_service import a_cop

        hoy = _dia_operativo()
        fecha_limite = hoy - timedelta(days=meses * 30)

        # Acuerdos vigentes indexados por producto
        acuerdos = AcuerdoMarco.query.filter(
            AcuerdoMarco.activo == True,  # noqa: E712
            AcuerdoMarco.vigencia_desde <= hoy,
            AcuerdoMarco.vigencia_hasta >= hoy,
        ).all()

        acuerdos_por_prod = {}
        for a in acuerdos:
            acuerdos_por_prod[a.producto_id] = a

        if not acuerdos_por_prod:
            return {'derivas': [], 'total': 0, 'nota': 'Sin acuerdos vigentes para comparar'}

        recibidos, fuente = precios_de_compra_recibidos(
            fecha_limite, list(acuerdos_por_prod))

        productos = {p.id: p for p in Producto.query.filter(
            Producto.id.in_(list(acuerdos_por_prod))).all()}
        con_precio = {r['producto_id'] for r in recibidos}
        sin_precio = sorted(
            (productos[pid].codigo_siesa if pid in productos else str(pid))
            for pid in acuerdos_por_prod if pid not in con_precio)

        derivas = []
        sin_cantidad = 0
        for r in recibidos:
            acuerdo = acuerdos_por_prod.get(r['producto_id'])
            if not acuerdo:
                continue
            precio_pactado, _c1 = a_cop(acuerdo.precio_unitario, acuerdo.moneda)
            precio_facturado, _c2 = a_cop(r['precio_unitario'], r.get('moneda', 'COP'))
            if not precio_pactado or precio_facturado is None:
                continue

            diferencia_pct = round((precio_facturado - precio_pactado) / precio_pactado * 100, 2)

            if abs(diferencia_pct) > 1.0:  # Solo reportar derivas >1%
                prod = productos.get(r['producto_id'])
                if r.get('cantidad') is None:
                    sin_cantidad += 1
                cantidad = float(r.get('cantidad') or 0)
                prov_oc = r.get('proveedor_codigo')
                prov_acuerdo = acuerdo.proveedor.codigo if acuerdo.proveedor else None
                derivas.append({
                    'producto_id': r['producto_id'],
                    'referencia': prod.codigo_siesa if prod else '?',
                    'nombre': prod.nombre if prod else '?',
                    'proveedor_acuerdo': acuerdo.proveedor.nombre if acuerdo.proveedor else '?',
                    'proveedor_factura': r.get('proveedor'),
                    'mismo_proveedor': (prov_oc == prov_acuerdo
                                        if prov_oc and prov_acuerdo else None),
                    'oc': r.get('oc'),
                    'precio_pactado': round(precio_pactado, 2),
                    'precio_facturado': round(precio_facturado, 2),
                    'cantidad': cantidad,
                    'impacto_cop': round((precio_facturado - precio_pactado) * cantidad, 2),
                    'diferencia_pct': diferencia_pct,
                    'fecha_recepcion': r['fecha'].isoformat() if r.get('fecha') else None,
                    'alerta': 'SOBRECOSTO' if diferencia_pct > 0 else 'SUBCOSTO',
                })

        derivas.sort(key=lambda x: abs(x['diferencia_pct']), reverse=True)

        impacto_total = sum(d['impacto_cop'] for d in derivas if d['impacto_cop'] > 0)

        nota = None
        if not recibidos:
            from app.services.compras_fuentes import frescura_oc
            espejo = frescura_oc()
            nota = (f'Sin precio de compra recibido para comparar ({fuente}): ninguna '
                    f'OC de Siesa de los últimos {meses} meses para los SKU con acuerdo. '
                    f'{len(acuerdos_por_prod)} acuerdo(s) vigente(s) sin contrastar.'
                    + (f' {espejo["nota"]}' if espejo.get('nota') else ''))
        return {
            'derivas': derivas,
            'total': len(derivas),
            'sobrecostos': sum(1 for d in derivas if d['alerta'] == 'SOBRECOSTO'),
            'impacto_estimado_cop': round(impacto_total, 2),
            'periodo_meses': meses,
            'fuente_precio_compra': fuente,
            'sin_precio_de_compra': sin_precio,
            'sin_cantidad': sin_cantidad,
            'nota': nota,
        }

    @staticmethod
    def calendario_vencimientos() -> dict:
        """
        Agenda de renegociación: acuerdos que vencen en los próximos 60 días.

        Incluye:
        - Acuerdos por vencer (3 semanas de anticipación)
        - Ya vencidos (últimos 30 días, no renovados)
        - SKUs de rama 2 que pidieron cotización >2 veces este trimestre
          → candidatos a entrar al próximo acuerdo

        Returns: {por_vencer, vencidos, candidatos_acuerdo}
        """
        from app.models.acuerdo_marco import AcuerdoMarco, PrecioProveedor
        from app.models.producto import Producto
        from sqlalchemy import func

        hoy = _dia_operativo()
        limite_alerta = hoy + timedelta(days=DIAS_ALERTA_VENCIMIENTO * 2)
        inicio_trimestre = hoy - timedelta(days=90)

        # Acuerdos por vencer
        por_vencer = AcuerdoMarco.query.filter(
            AcuerdoMarco.activo == True,
            AcuerdoMarco.vigencia_hasta <= limite_alerta,
            AcuerdoMarco.vigencia_hasta >= hoy,
        ).order_by(AcuerdoMarco.vigencia_hasta).all()

        # Acuerdos ya vencidos (últimos 30 días)
        vencidos = AcuerdoMarco.query.filter(
            AcuerdoMarco.vigencia_hasta < hoy,
            AcuerdoMarco.vigencia_hasta >= hoy - timedelta(days=30),
        ).order_by(AcuerdoMarco.vigencia_hasta.desc()).all()

        # Candidatos: SKUs cotizados >2 veces este trimestre sin acuerdo
        cotizaciones_frecuentes = (
            db.session.query(
                PrecioProveedor.producto_id,
                func.count(PrecioProveedor.id).label('cotizaciones'),
            )
            .filter(PrecioProveedor.fecha >= inicio_trimestre)
            .filter(PrecioProveedor.fuente == 'COTIZACION')
            .group_by(PrecioProveedor.producto_id)
            .having(func.count(PrecioProveedor.id) > 2)
            .all()
        )

        # Filtrar los que ya tienen acuerdo vigente
        con_acuerdo = set(
            a.producto_id for a in AcuerdoMarco.query.filter(
                AcuerdoMarco.activo == True,
                AcuerdoMarco.vigencia_hasta >= hoy,
            ).all()
        )

        candidatos = []
        for row in cotizaciones_frecuentes:
            if row.producto_id in con_acuerdo:
                continue
            prod = db.session.get(Producto, row.producto_id)
            candidatos.append({
                'producto_id': row.producto_id,
                'referencia': prod.codigo_siesa if prod else '?',
                'nombre': prod.nombre if prod else '?',
                'cotizaciones_trimestre': row.cotizaciones,
                'sugerencia': 'Negociar acuerdo marco — cotizado >2 veces sin acuerdo',
            })

        return {
            'por_vencer': [a.to_dict() for a in por_vencer],
            'vencidos': [a.to_dict() for a in vencidos],
            'candidatos_acuerdo': candidatos,
            'resumen': {
                'acuerdos_por_vencer': len(por_vencer),
                'acuerdos_vencidos': len(vencidos),
                'candidatos_nuevo_acuerdo': len(candidatos),
            },
        }

    @staticmethod
    def comparador_precios(producto_id: int) -> dict:
        """
        Los 3 mejores precios vigentes lado a lado para armar borrador OC.

        Returns: {precios: [{proveedor, precio, fecha, fuente, tiene_acuerdo}], mejor}
        """
        from app.models.acuerdo_marco import AcuerdoMarco, PrecioProveedor

        hoy = _dia_operativo()

        # Acuerdos vigentes
        acuerdos = AcuerdoMarco.query.filter(
            AcuerdoMarco.producto_id == producto_id,
            AcuerdoMarco.activo == True,
            AcuerdoMarco.vigencia_desde <= hoy,
            AcuerdoMarco.vigencia_hasta >= hoy,
        ).all()

        # Cotizaciones recientes (últimos 90 días)
        cotizaciones = PrecioProveedor.query.filter(
            PrecioProveedor.producto_id == producto_id,
            PrecioProveedor.fecha >= hoy - timedelta(days=90),
        ).order_by(PrecioProveedor.precio_unitario).all()

        precios = []

        for a in acuerdos:
            precios.append({
                'proveedor_id': a.proveedor_id,
                'proveedor': a.proveedor.nombre if a.proveedor else '?',
                'precio': float(a.precio_unitario),
                'moneda': a.moneda,
                'fuente': 'ACUERDO_MARCO',
                'vigencia_hasta': a.vigencia_hasta.isoformat(),
                'tiene_acuerdo': True,
            })

        proveedores_acuerdo = {a.proveedor_id for a in acuerdos}
        for c in cotizaciones:
            if c.proveedor_id in proveedores_acuerdo:
                continue  # Ya está el precio del acuerdo
            precios.append({
                'proveedor_id': c.proveedor_id,
                'proveedor': c.proveedor.nombre if c.proveedor else '?',
                'precio': float(c.precio_unitario),
                'moneda': c.moneda,
                'fuente': c.fuente,
                'fecha': c.fecha.isoformat(),
                'tiene_acuerdo': False,
            })

        precios.sort(key=lambda x: x['precio'])

        return {
            'producto_id': producto_id,
            'precios': precios[:5],
            'mejor': precios[0] if precios else None,
            'total_opciones': len(precios),
        }
