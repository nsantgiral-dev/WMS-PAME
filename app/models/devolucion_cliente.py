"""
DevolucionCliente — devolución física de un cliente atada al pedido/factura original.

Reemplaza TareaDevolucion (app/models/devolucion.py, DEPRECATED): en vez de
reaccionar a una reconciliación ciega de inventario, el recepcionista busca el
pedido ya despachado, cuenta físicamente cuánto se devuelve por línea (total o
parcial) y confirma. Al confirmar se ingresa el stock y se dispara
automáticamente la Nota Crédito (142946) hacia Siesa — ver
devolucion_cliente_service.py y siesa_job_service.py (job
NOTA_CREDITO_DEVOLUCION_CLIENTE).

Estados (m045devol, 2026-09-24 — ver «Si el cliente no paga, la mercancía
vuelve» en CLAUDE.md):

    EN_CAMION  → la parada de ruta se confirmó RECHAZADA/PARCIAL: la mercancía
                 viene en el camión. Nace en `ruta_service.confirmar_parada`
                 (`devolucion_ruta.sincronizar_con_parada`, la única función que
                 crea devoluciones de ruta), con lo que declaró el conductor.
    ABIERTA    → está en bodega sin contar (llegó el camión, o es de mostrador).
    CONFIRMADA → contada: entrada física a la zona DEVOLUCION (no vendible) +
                 NC encolada. Al aprobarse la NC se libera a picking.
    FALTANTE_TOTAL → contada en CERO: no volvió nada. Sin NC ni inventario; el
                 faltante queda medido (antes había que CANCELAR y el faltante
                 desaparecía de la medición).
    CANCELADA  → solo supervisión con motivo (bitácora), o el sistema cuando la
                 parada se reconfirma como entregada.

**Una función escribe el estado**: `devolucion_cliente_service.cambiar_estado`
(trinquete AST en `tests/test_devolucion_vuelve.py`).
"""
from datetime import datetime
from app.extensions import db


class EstadoDevolucionCliente:
    EN_CAMION      = 'EN_CAMION'
    ABIERTA        = 'ABIERTA'
    CONFIRMADA     = 'CONFIRMADA'
    FALTANTE_TOTAL = 'FALTANTE_TOTAL'
    CANCELADA      = 'CANCELADA'

    TODOS = (EN_CAMION, ABIERTA, CONFIRMADA, FALTANTE_TOTAL, CANCELADA)
    #: Sin contar todavía: la mercancía viene en el camión o espera en bodega.
    ACTIVAS = (EN_CAMION, ABIERTA)
    #: Ya contadas: lo que volvió (o que no volvió nada) está medido.
    CONTADAS = (CONFIRMADA, FALTANTE_TOTAL)
    #: Todo lo que no se canceló.
    VIGENTES = ACTIVAS + CONTADAS

    #: Transiciones permitidas. `cambiar_estado` rechaza cualquier otra.
    TRANSICIONES = {
        EN_CAMION: (ABIERTA, CONFIRMADA, FALTANTE_TOTAL, CANCELADA),
        ABIERTA: (CONFIRMADA, FALTANTE_TOTAL, CANCELADA),
        CONFIRMADA: (),
        FALTANTE_TOTAL: (),
        CANCELADA: (),
    }


class FuenteAprobacionNC:
    """Quién dijo que la NC está aprobada en Siesa."""
    #: El cron leyó `f350_ind_estado = 1` en `t350_co_docto_contable`.
    SIESA = 'SIESA'
    #: Alguien apretó «Ya la aprobé» (respaldo, con bitácora).
    MANUAL = 'MANUAL'


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

    # ── m045devol: la devolución nace con el rechazo, no con la liquidación ──
    #: Lo que el conductor declaró, tal cual llegó (estado, motivo, ítems).
    #: Nunca se pisa: es la otra mitad del faltante de retorno, y en un producto
    #: de doble unidad es lo único que dice cuánto se declaró por referencia.
    declaracion_conductor = db.Column(db.JSON, nullable=True)
    #: Cuándo recepción recibió el camión (EN_CAMION → ABIERTA).
    fecha_llegada = db.Column(db.DateTime, nullable=True)
    #: Cuándo las líneas se amarraron a las filas reales de la factura
    #: (`f470_rowid`). La creación NO sale a la red (la parada se confirma sin
    #: señal): esto se hace después, con Siesa arriba.
    vinculada_factura_at = db.Column(db.DateTime, nullable=True)
    #: Lo que impide contarla, en palabras (referencia sin producto WMS, factura
    #: no localizada…). NULL = nada lo impide. Un error visible, no un log.
    problema_factura = db.Column(db.Text, nullable=True)
    #: SIESA (verificada por el cron) | MANUAL (el botón de respaldo).
    nc_aprobada_fuente = db.Column(db.String(10), nullable=True)
    #: Último `f350_ind_estado` leído de la NC (0 elaboración, 1 aprobada,
    #: 2 anulada) y cuándo. NULL = nunca se pudo leer.
    nc_estado_siesa = db.Column(db.SmallInteger, nullable=True)
    nc_estado_leido_at = db.Column(db.DateTime, nullable=True)
    #: Cuándo lo sano salió de la zona DEVOLUCION hacia inventario vendible.
    reingreso_liberado_at = db.Column(db.DateTime, nullable=True)
    #: Cuándo se canceló (la medición de «rechazo con devolución cancelada»).
    cancelada_at = db.Column(db.DateTime, nullable=True)

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
        db.CheckConstraint(
            "estado IN ('EN_CAMION','ABIERTA','CONFIRMADA','FALTANTE_TOTAL','CANCELADA')",
            name='ck_devolucion_cliente_estado'),
        db.CheckConstraint(
            "nc_aprobada_fuente IS NULL OR nc_aprobada_fuente IN ('SIESA','MANUAL')",
            name='ck_devolucion_nc_fuente'),
    )

    @property
    def es_de_ruta(self) -> bool:
        return self.recaudo_entrega_id is not None

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
            'es_de_ruta': self.es_de_ruta,
            'declaracion_conductor': self.declaracion_conductor,
            'fecha_llegada': self.fecha_llegada.isoformat() if self.fecha_llegada else None,
            'vinculada_factura_at': (self.vinculada_factura_at.isoformat()
                                     if self.vinculada_factura_at else None),
            'problema_factura': self.problema_factura,
            'nc_aprobada_fuente': self.nc_aprobada_fuente,
            'nc_estado_siesa': self.nc_estado_siesa,
            'nc_estado_leido_at': (self.nc_estado_leido_at.isoformat()
                                   if self.nc_estado_leido_at else None),
            'reingreso_liberado_at': (self.reingreso_liberado_at.isoformat()
                                      if self.reingreso_liberado_at else None),
            'cancelada_at': self.cancelada_at.isoformat() if self.cancelada_at else None,
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
    #: Lo que el CONDUCTOR declaró que volvía (m041flfugas). Solo en las
    #: devoluciones que arma Liquidación de ruta; `NULL` en las de mostrador
    #: (ahí quien declara y quien cuenta es la misma recepcionista).
    #:
    #: Existe porque `confirmar_entrada_fisica` **sobrescribe**
    #: `cantidad_devuelta` con lo contado — que es lo correcto para el
    #: inventario y la NC — y con eso se perdía la única evidencia de un
    #: retorno inflado: el conductor declara 10 devueltas, llegan 7, y las 3
    #: que faltan no están ni en el cliente (no las pagó) ni en bodega. Nunca
    #: se pisa: `senales_ruta.faltante_de_retorno` lee la diferencia.
    cantidad_declarada = db.Column(db.Numeric(14, 4), nullable=True)

    es_averiado = db.Column(db.Boolean, nullable=False, default=False)
    #: Cuántas de las devueltas vinieron averiadas (m045devol). Parte la línea
    #: sin partir la línea de factura: las sanas van a la zona DEVOLUCION, las
    #: averiadas al bin AVERIADOS. NULL = línea vieja, manda `es_averiado`.
    cantidad_averiada = db.Column(db.Numeric(14, 4), nullable=True)
    #: Dónde entró lo SANO (zona DEVOLUCION, no vendible hasta la NC aprobada).
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)
    #: A dónde se movió lo sano cuando la NC se aprobó (picking / su slot).
    ubicacion_liberada_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=True)

    # Insumos para construir la línea de la NC (142946) — capturados en la búsqueda
    f470_id_unidad_medida = db.Column(db.String(20))
    f150_id_bodega = db.Column(db.String(20))
    f470_rowid = db.Column(db.String(20))
    #: `f470_rowid` mientras la devolución está ACTIVA (EN_CAMION/ABIERTA);
    #: NULL en cualquier otro estado. Existe solo para el índice único de abajo:
    #: **una sola devolución activa por línea de factura**, en la base y no en
    #: un `.first()` sin lock. Lo mantiene `devolucion_cliente_service.
    #: cambiar_estado` (y la vinculación a la factura), nadie más.
    f470_rowid_activo = db.Column(db.String(20), nullable=True)
    #: Valor neto (con IVA) por unidad de la línea de factura
    #: (`f470_vlr_neto / f470_cant_base`), leído al vincular. Valoriza el
    #: faltante de retorno con el precio del documento, no con un promedio.
    valor_unitario = db.Column(db.Numeric(14, 4), nullable=True)

    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        # `ConteoService.procesos_en_curso` (devolución con la NC sin aprobar). m031.
        db.Index('ix_lineas_devolucion_cliente_producto', 'producto_id'),
        db.Index('uq_linea_devolucion_rowid_activo', 'f470_rowid_activo', unique=True,
                 postgresql_where=db.text('f470_rowid_activo IS NOT NULL'),
                 sqlite_where=db.text('f470_rowid_activo IS NOT NULL')),
        db.Index('ix_lineas_devolucion_cliente_rowid', 'f470_rowid'),
        db.CheckConstraint(
            'cantidad_averiada IS NULL OR (cantidad_averiada >= 0 '
            'AND cantidad_averiada <= cantidad_devuelta)',
            name='ck_linea_devolucion_averiada'),
    )

    # Relaciones
    producto = db.relationship('Producto', lazy=True)
    ubicacion = db.relationship('Ubicacion', lazy=True, foreign_keys=[ubicacion_id])
    ubicacion_liberada = db.relationship('Ubicacion', lazy=True,
                                         foreign_keys=[ubicacion_liberada_id])

    def averiadas(self) -> float:
        """Cuántas de las devueltas vinieron averiadas. Una política: la línea
        vieja (sin `cantidad_averiada`) es toda averiada o toda sana según
        `es_averiado`."""
        dev = float(self.cantidad_devuelta or 0)
        if self.cantidad_averiada is not None:
            return max(0.0, min(float(self.cantidad_averiada), dev))
        return dev if self.es_averiado else 0.0

    def sanas(self) -> float:
        return max(0.0, float(self.cantidad_devuelta or 0) - self.averiadas())

    def to_dict(self):
        return {
            'id': self.id,
            'devolucion_id': self.devolucion_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'codigo_barras': self.producto.codigo_barras if self.producto else None,
            'codigo_siesa': self.codigo_siesa,
            'cantidad_facturada': float(self.cantidad_facturada or 0),
            'cantidad_devuelta': float(self.cantidad_devuelta or 0),
            'cantidad_declarada': (float(self.cantidad_declarada)
                                   if self.cantidad_declarada is not None else None),
            'es_averiado': self.es_averiado,
            'cantidad_averiada': (float(self.cantidad_averiada)
                                  if self.cantidad_averiada is not None else None),
            'averiadas': self.averiadas(),
            'sanas': self.sanas(),
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'ubicacion_liberada_id': self.ubicacion_liberada_id,
            'f470_rowid': self.f470_rowid,
            'valor_unitario': (float(self.valor_unitario)
                               if self.valor_unitario is not None else None),
            'f470_id_unidad_medida': self.f470_id_unidad_medida,
            'f150_id_bodega': self.f150_id_bodega,
        }
