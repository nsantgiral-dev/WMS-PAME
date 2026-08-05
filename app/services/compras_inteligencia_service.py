"""
ComprasInteligenciaService — acuerdos marco, memoria de precios, detector de deriva.

Tres ramas:
  1. Acuerdo vigente → OC al precio pactado (70-80% volumen, 30s de aprobación)
  2. Sin acuerdo, núcleo A → RFQ a 2-3 proveedores, comparador, humano decide
  3. Cola C → precio lista proveedor preferente, sin cotizar

Detector de deriva:
  Compara precio facturado en recepción vs precio pactado en acuerdo.
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

        Cruza las recepciones recientes contra los acuerdos marco vigentes.
        Un 2-3% de deriva silenciosa son millones al año.

        Returns: {derivas: [{referencia, proveedor, precio_pactado, precio_facturado, diferencia_pct}]}
        """
        from app.models.acuerdo_marco import AcuerdoMarco
        from app.models.recepcion import RecepcionMercancia, ItemRecepcion
        from app.models.producto import Producto
        from sqlalchemy import func

        hoy = _dia_operativo()
        fecha_limite = hoy - timedelta(days=meses * 30)

        # Acuerdos vigentes indexados por producto
        acuerdos = AcuerdoMarco.query.filter(
            AcuerdoMarco.activo == True,
            AcuerdoMarco.vigencia_desde <= hoy,
            AcuerdoMarco.vigencia_hasta >= hoy,
        ).all()

        acuerdos_por_prod = {}
        for a in acuerdos:
            acuerdos_por_prod[a.producto_id] = a

        if not acuerdos_por_prod:
            return {'derivas': [], 'total': 0, 'nota': 'Sin acuerdos vigentes para comparar'}

        # Recepciones recientes con costo
        recepciones = (
            db.session.query(
                ItemRecepcion.producto_id,
                ItemRecepcion.costo_unitario,
                RecepcionMercancia.fecha_recepcion,
                RecepcionMercancia.proveedor_nombre,
            )
            .join(RecepcionMercancia, ItemRecepcion.recepcion_id == RecepcionMercancia.id)
            .filter(RecepcionMercancia.fecha_recepcion >= fecha_limite)
            .filter(ItemRecepcion.costo_unitario > 0)
            .all()
        )

        derivas = []
        for r in recepciones:
            acuerdo = acuerdos_por_prod.get(r.producto_id)
            if not acuerdo:
                continue

            precio_pactado = float(acuerdo.precio_unitario)
            precio_facturado = float(r.costo_unitario)
            if precio_pactado == 0:
                continue

            diferencia_pct = round((precio_facturado - precio_pactado) / precio_pactado * 100, 2)

            if abs(diferencia_pct) > 1.0:  # Solo reportar derivas >1%
                prod = db.session.get(Producto, r.producto_id)
                derivas.append({
                    'producto_id': r.producto_id,
                    'referencia': prod.codigo_siesa if prod else '?',
                    'nombre': prod.nombre if prod else '?',
                    'proveedor_acuerdo': acuerdo.proveedor.nombre if acuerdo.proveedor else '?',
                    'proveedor_factura': r.proveedor_nombre,
                    'precio_pactado': precio_pactado,
                    'precio_facturado': precio_facturado,
                    'diferencia_pct': diferencia_pct,
                    'fecha_recepcion': r.fecha_recepcion.isoformat() if r.fecha_recepcion else None,
                    'alerta': 'SOBRECOSTO' if diferencia_pct > 0 else 'SUBCOSTO',
                })

        derivas.sort(key=lambda x: abs(x['diferencia_pct']), reverse=True)

        impacto_total = sum(
            d['diferencia_pct'] / 100 * d['precio_pactado']
            for d in derivas if d['diferencia_pct'] > 0
        )

        return {
            'derivas': derivas,
            'total': len(derivas),
            'sobrecostos': sum(1 for d in derivas if d['alerta'] == 'SOBRECOSTO'),
            'impacto_estimado_por_unidad': round(impacto_total, 2),
            'periodo_meses': meses,
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
