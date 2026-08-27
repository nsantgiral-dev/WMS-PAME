from datetime import datetime
from app.extensions import db


class Ubicacion(db.Model):
    __tablename__ = 'ubicaciones'

    # Código del bucket sin ubicación física real donde aterriza el stock
    # recién sincronizado de Siesa (ver inventario_siesa_service.py). Única
    # fuente de verdad del literal — cualquier servicio que necesite
    # reconocer o crear este bucket debe referenciar esta constante, nunca
    # repetir el string.
    CODIGO_GENERAL = 'SIESA-GENERAL'

    # Zonas donde 1 hueco == 1 SKU (nunca compartido). Layout la usa para
    # decidir si capacidad_maxima tiene sentido; Reposición la usa para
    # decidir si stock_minimo/stock_maximo tienen sentido. Es la misma
    # pregunta ("¿este hueco es de un solo SKU?") para dos dominios distintos
    # — una sola constante, no dos tuplas que alguien puede desincronizar.
    ZONAS_SLOT_UNICO = ('PICKING', 'IMPORTADOS')

    id = db.Column(db.Integer, primary_key=True)
    # Único DENTRO de un almacén, no en todo el WMS (m016) — SIESA-GENERAL es
    # un bucket genérico que cada almacén necesita poder tener con el mismo
    # código sin chocar con el de otro.
    codigo = db.Column(db.String(50), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)
    zona = db.Column(db.String(50))
    pasillo = db.Column(db.String(10))
    estante = db.Column(db.String(10))
    tipo = db.Column(db.String(30), default='estanteria')

    # ── Direccion fisica de 5 ejes (Layout) ──────────────────────────────
    # Pasillo -> Fila (1/2, lado del pasillo) -> Cuerpo (bahia) -> Nivel
    # (entrepano, altura relativa dentro del cuerpo) -> Hueco (espacio
    # dentro del entrepano — varios SKUs pueden compartir un mismo Nivel).
    # 'estante' es el campo legado (fila plana pre-rediseño) — las
    # ubicaciones creadas con el mecanismo nuevo no lo usan, quedan solo
    # con fila/cuerpo/nivel/hueco.
    fila = db.Column(db.Integer, nullable=True)      # 1 o 2 — lado del pasillo
    cuerpo = db.Column(db.Integer, nullable=True)     # bahia dentro de la fila
    nivel = db.Column(db.Integer, nullable=True)      # entrepano, 1 = piso
    hueco = db.Column(db.Integer, nullable=True)      # espacio dentro del entrepano

    # ── Campos maestros sincronizados desde Siesa (API_v2_Ubicaciones ID 43) ──
    # Siesa es el dueño — el WMS solo obedece. No editar manualmente.
    tipo_zona = db.Column(db.String(10), nullable=False, default='GENERAL')
    # PICKING = piso, unidades sueltas | RESERVA = alto, LPNs sellados
    # AVERIAS = productos dañados/en revisión | GENERAL = sin rol especial (legado)
    stock_minimo = db.Column(db.Integer, nullable=True)   # f152_cant_minima en Siesa
    stock_maximo = db.Column(db.Integer, nullable=True)   # f152_cant_maxima en Siesa
    secuencia_ruteo = db.Column(db.Integer, nullable=True)  # f152_secuencia — menor = primero

    # capacidad_maxima se mantiene por retrocompatibilidad; stock_maximo es el campo canónico
    capacidad_maxima = db.Column(db.Integer)

    # origen: SIESA (la crea/clasifica el sync nocturno) | MANUAL (la crea el jefe de
    # bodega desde el módulo de Layout). El sync nunca toca una ubicación MANUAL.
    origen = db.Column(db.String(10), nullable=False, default='MANUAL')

    # Slot fijo de PICKING: 1 SKU ↔ 1 ubicación PICKING por almacén. Solo se usa
    # cuando tipo_zona='PICKING' — en RESERVA/AVERIAS queda en None.
    producto_asignado_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=True)

    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    # Relaciones
    productos = db.relationship('UbicacionProducto', backref='ubicacion', lazy=True)
    producto_asignado = db.relationship('Producto', foreign_keys=[producto_asignado_id], lazy=True)

    @property
    def es_picking(self):
        return self.tipo_zona == 'PICKING'

    @property
    def es_reserva(self):
        return self.tipo_zona == 'RESERVA'

    __table_args__ = (
        # Declarado con el nombre EXACTO que tiene en la base. Existía en
        # migraciones y no en el modelo, así que `flask db check` lo
        # reportaba como sobrante — trece líneas de ruido que volvían
        # inservible el único detector que atrapa una columna sin
        # migración (lo de `puede_usar_camara`, 2026-08-20).
        db.Index('ix_ubicaciones_producto_asignado', 'producto_asignado_id'),
        # m016 (2026-08-27): antes `codigo` era único GLOBAL — bloqueaba que
        # dos almacenes tuvieran cada uno su propio SIESA-GENERAL. El
        # invariante real siempre fue "único dentro del almacén".
        db.UniqueConstraint('almacen_id', 'codigo', name='uq_ubicacion_almacen_codigo'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'almacen_id': self.almacen_id,
            'zona': self.zona,
            'pasillo': self.pasillo,
            'estante': self.estante,
            'fila': self.fila,
            'cuerpo': self.cuerpo,
            'nivel': self.nivel,
            'hueco': self.hueco,
            'tipo': self.tipo,
            'tipo_zona': self.tipo_zona,
            'stock_minimo': self.stock_minimo,
            'stock_maximo': self.stock_maximo,
            'secuencia_ruteo': self.secuencia_ruteo,
            'capacidad_maxima': self.capacidad_maxima,
            'origen': self.origen,
            'producto_asignado_id': self.producto_asignado_id,
            'producto_asignado_codigo': self.producto_asignado.codigo if self.producto_asignado else None,
            'producto_asignado_nombre': self.producto_asignado.nombre if self.producto_asignado else None,
            'activo': self.activo,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
        }