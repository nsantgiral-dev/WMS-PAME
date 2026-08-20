"""
DevolucionCliente — devolución física de un cliente atada al pedido/factura original.

Reemplaza TareaDevolucion (app/models/devolucion.py, DEPRECATED): en vez de
reaccionar a una reconciliación ciega de inventario, el recepcionista busca el
pedido ya despachado, cuenta físicamente cuánto se devuelve por línea (total o
parcial) y confirma. Al confirmar se ingresa el stock y se dispara
automáticamente la Nota Crédito (142946) hacia Siesa — ver
devolucion_cliente_service.py y siesa_job_service.py (job
NOTA_CREDITO_DEVOLUCION_CLIENTE).

Estados: ABIERTA (líneas armadas, aún no confirmada) → CONFIRMADA (entrada
física + NC encolada) | CANCELADA (antes de confirmar, nada tocó stock/Siesa).
"""
from datetime import datetime
from app.extensions import db


class EstadoDevolucionCliente:
    ABIERTA    = 'ABIERTA'
    CONFIRMADA = 'CONFIRMADA'
    CANCELADA  = 'CANCELADA'


class DevolucionCliente(db.Model):
    __tablename__ = 'devoluciones_cliente'

    id = db.Column(db.Integer, primary_key=True)
    #: Sin `unique=True`: la unicidad la impone el índice único
    #: `ix_devolucion_cliente_codigo`, declarado abajo con el nombre que
    #: tiene en la base. Con `unique=True` el modelo pedía un CONSTRAINT y la
    #: base tiene un ÍNDICE — mismo efecto, distinta forma, deriva perpetua.
    codigo = db.Column(db.String(50), nullable=False)

    # Ancla al pedido ya despachado — no se vuelve a buscar en Siesa desde cero
    tarea_packing_id = db.Column(db.Integer, db.ForeignKey('tareas_packing.id'), nullable=False)
    numero_pedido_siesa = db.Column(db.String(50))  # solo referencia/display

    # NULL = devolución armada desde cero por la recepcionista (flujo original).
    # No-NULL = se originó en una entrega Parcial/Rechazada de ruta (Liquidación
    # "Liquidar en WMS") — al confirmar esta devolución, el job
    # NOTA_CREDITO_DEVOLUCION_CLIENTE también marca siesa_nc_triggered=True en
    # ese RecaudoEntrega, destrabando el RECIBO_CAJA que depende de esa NC.
    recaudo_entrega_id = db.Column(db.Integer, db.ForeignKey('recaudos_entrega.id'), nullable=True)

    # Tipo/consec REALES de la factura electrónica (de connekta.get_detalle_factura),
    # NUNCA los _pedido_siesa de TareaPacking — ver hallazgo del DOCX 142946 en el plan.
    tipo_docto_fe = db.Column(db.String(20), nullable=False)
    consec_fe = db.Column(db.String(30), nullable=False)

    cliente = db.Column(db.String(200))
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    recepcionista_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    estado = db.Column(db.String(20), nullable=False, default=EstadoDevolucionCliente.ABIERTA)
    es_total = db.Column(db.Boolean, nullable=False, default=False)
    observaciones = db.Column(db.Text)

    # Idempotencia NC — propio de esta tabla, no toca RecaudoEntrega.siesa_nc_triggered
    siesa_nc_triggered = db.Column(db.Boolean, default=False, nullable=False, server_default='false')
    siesa_nc_triggered_at = db.Column(db.DateTime, nullable=True)
    siesa_nc_response = db.Column(db.Text, nullable=True)

    # Consecutivo real que Siesa le asignó a la NC. El WMS nunca lo sabía: el
    # POST no lo devuelve, así que contabilidad tenía que BUSCAR el documento en
    # Auditoría para aprobarlo. Se resuelve por consulta después de crear.
    siesa_nc_consec = db.Column(db.String(30), nullable=True)

    # Motivo DIAN (paso 3 del procedimiento manual): 'AUTOMATICO' si el WMS se
    # lo puso vía 251546, 'MANUAL' si no pudo y queda para contabilidad. NULL =
    # todavía sin intentar. Es un tri-estado a propósito: "no lo intenté" y "lo
    # intenté y no pude" mandan a la misma persona a hacer cosas distintas.
    siesa_motivo_dian = db.Column(db.String(20), nullable=True)
    siesa_motivo_dian_at = db.Column(db.DateTime, nullable=True)
    siesa_motivo_dian_detalle = db.Column(db.Text, nullable=True)

    # 142946 se crea en Elaboración (CLAUDE.md Regla #21) — Siesa no cruza la
    # cartera solo ni al crear ni al aprobar el documento (verificado con NCE-
    # 00000048). Estos campos son seguimiento interno del WMS, NO un estado de
    # Siesa: contabilidad marca aquí cuándo aprobó+cruzó manualmente en Siesa.
    nc_aprobada_siesa = db.Column(db.Boolean, default=False, nullable=False, server_default='false')
    nc_aprobada_siesa_at = db.Column(db.DateTime, nullable=True)
    nc_aprobada_siesa_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_confirmacion = db.Column(db.DateTime, nullable=True)

    # Relaciones
    tarea_packing = db.relationship('TareaPacking', lazy=True)
    recaudo_entrega = db.relationship('RecaudoEntrega', lazy=True)
    almacen = db.relationship('Almacen', lazy=True)
    recepcionista = db.relationship('Usuario', lazy=True, foreign_keys=[recepcionista_id])
    nc_aprobada_por = db.relationship('Usuario', lazy=True, foreign_keys=[nc_aprobada_siesa_por])
    lineas = db.relationship('LineaDevolucionCliente', backref='devolucion',
                              lazy=True, cascade='all, delete-orphan')

        # Declarado con el nombre EXACTO que tiene en la base. Existía en
        # migraciones y no en el modelo, así que `flask db check` lo
        # reportaba como sobrante — trece líneas de ruido que volvían
        # inservible el único detector que atrapa una columna sin
        # migración (lo de `puede_usar_camara`, 2026-08-20).
    __table_args__ = (
        db.Index('ix_devolucion_cliente_codigo', 'codigo', unique=True),
        db.Index('ix_devolucion_cliente_pedido', 'numero_pedido_siesa'),
        # Una devolución activa por recaudo. `crear_devoluciones_pendientes_ruta`
        # se invoca desde `liquidar_ruta` Y desde `forzar_cierre_ruta`; el
        # guard de `liquidacion_service.py:1033` es un `.first()` sin lock.
        # Dos filas → la recepcionista confirma las dos → dos notas crédito
        # 251126 sobre la misma factura, con cruce de cartera automático.
        db.Index('uq_devolucion_por_recaudo', 'recaudo_entrega_id',
                 unique=True,
                 postgresql_where=db.text(
                     "estado <> 'CANCELADA' AND recaudo_entrega_id IS NOT NULL"),
                 sqlite_where=db.text(
                     "estado <> 'CANCELADA' AND recaudo_entrega_id IS NOT NULL")),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'tarea_packing_id': self.tarea_packing_id,
            'numero_pedido_siesa': self.numero_pedido_siesa,
            'recaudo_entrega_id': self.recaudo_entrega_id,
            'tipo_docto_fe': self.tipo_docto_fe,
            'consec_fe': self.consec_fe,
            'cliente': self.cliente,
            'almacen_id': self.almacen_id,
            'recepcionista_id': self.recepcionista_id,
            'estado': self.estado,
            'es_total': self.es_total,
            'observaciones': self.observaciones,
            'siesa_nc_triggered': self.siesa_nc_triggered,
            'siesa_nc_triggered_at': self.siesa_nc_triggered_at.isoformat() if self.siesa_nc_triggered_at else None,
            'siesa_nc_consec': self.siesa_nc_consec,
            'siesa_motivo_dian': self.siesa_motivo_dian,
            'siesa_motivo_dian_detalle': self.siesa_motivo_dian_detalle,
            'nc_aprobada_siesa': self.nc_aprobada_siesa,
            'nc_aprobada_siesa_at': self.nc_aprobada_siesa_at.isoformat() if self.nc_aprobada_siesa_at else None,
            'nc_aprobada_siesa_por': self.nc_aprobada_por.nombre if self.nc_aprobada_por else None,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_confirmacion': self.fecha_confirmacion.isoformat() if self.fecha_confirmacion else None,
            'lineas': [l.to_dict() for l in self.lineas],
        }


class LineaDevolucionCliente(db.Model):
    __tablename__ = 'lineas_devolucion_cliente'

    id = db.Column(db.Integer, primary_key=True)
    devolucion_id = db.Column(db.Integer, db.ForeignKey('devoluciones_cliente.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    codigo_siesa = db.Column(db.String(50), nullable=False)  # snapshot — match con f120_referencia

    cantidad_facturada = db.Column(db.Numeric(14, 4), nullable=False, default=0)  # tope, solo referencia
    cantidad_devuelta = db.Column(db.Numeric(14, 4), nullable=False, default=0)   # puede ser < facturada

    es_averiado = db.Column(db.Boolean, nullable=False, default=False)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)

    # Insumos para construir la línea de la NC (142946) — capturados en la búsqueda
    f470_id_unidad_medida = db.Column(db.String(20))
    f150_id_bodega = db.Column(db.String(20))
    f470_rowid = db.Column(db.String(20))

    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    producto = db.relationship('Producto', lazy=True)
    ubicacion = db.relationship('Ubicacion', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'devolucion_id': self.devolucion_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'codigo_siesa': self.codigo_siesa,
            'cantidad_facturada': float(self.cantidad_facturada or 0),
            'cantidad_devuelta': float(self.cantidad_devuelta or 0),
            'es_averiado': self.es_averiado,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'f470_id_unidad_medida': self.f470_id_unidad_medida,
            'f150_id_bodega': self.f150_id_bodega,
        }
