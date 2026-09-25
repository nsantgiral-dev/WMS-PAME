from datetime import datetime
from app.extensions import db

class Producto(db.Model):
    __tablename__ = 'productos'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    descripcion = db.Column(db.Text)
    categoria = db.Column(db.String(100))
    unidad_medida = db.Column(db.String(20), default='UND')
    peso = db.Column(db.Float)
    volumen = db.Column(db.Float)
    precio_compra = db.Column(db.Float, default=0)
    precio_venta = db.Column(db.Float, default=0)
    stock_minimo = db.Column(db.Integer, default=0)
    stock_maximo = db.Column(db.Integer, default=0)
    punto_pedido = db.Column(db.Integer, default=0)
    clasificacion_abc = db.Column(db.String(1))
    codigo_siesa = db.Column(db.String(50), index=True)
    codigo_barras = db.Column(db.String(100))      # EAN/UPC de la unidad suelta
    codigo_barras_empaque = db.Column(db.String(100))  # EAN de la caja/paca/paquete
    unidad_empaque = db.Column(db.String(20))     # ej. 'CJA', 'PAC', 'PQT'
    factor_conversion = db.Column(db.Integer, default=1)  # unidades por empaque
    unidad_negocio_id = db.Column(db.String(10))  # Unidad de negocio Siesa p.ej. '001'=PAPELERIA
    origen = db.Column(db.String(10))  # NACIONAL, CHINA — determina régimen de reposición
    #: El NOMBRE de la marca (`f106_descripcion` del criterio de Siesa: NORMA,
    #: SCRIBE…). Hasta m047 el comentario decía «código (M003…)» y la carga lo
    #: aceptaba así: un valor con dos significados. El código va aparte.
    marca_siesa = db.Column(db.String(50))
    #: El CÓDIGO del criterio de marca en Siesa (`f125_id_criterio_mayor`:
    #: M001, M003…), m047. Lo que compara `armador_service.MARCAS_CHINA`.
    marca_codigo = db.Column(db.String(20))
    #: Quién escribió `origen` / `marca_siesa` (m046compras): `CARGA_ARCHIVO`,
    #: `MANUAL` o `SIESA_CRITERIOS` (antes `SIESA_238920`). Sin esto una marca leída de Siesa y una
    #: tecleada son indistinguibles, y el armador decide régimen China con ella.
    origen_fuente = db.Column(db.String(20))
    marca_fuente = db.Column(db.String(20))
    activo = db.Column(db.Boolean, default=True)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relaciones
    ubicaciones = db.relationship('UbicacionProducto', backref='producto', lazy='subquery')
    movimientos = db.relationship('MovimientoInventario', backref='producto', lazy=True)

    @property
    def stock_total(self):
        """Suma de TODAS las ubicaciones — vendibles y averiadas.

        Es el total físico y por eso incluye la zona de averías. NO es la
        respuesta a «¿cuánto puedo vender?»: para eso está `stock_vendible`.
        Quien muestre este número debe mostrar `stock_averiado` al lado, o
        estará afirmando que la mercancía rota es buena.
        """
        return sum(u.cantidad for u in self.ubicaciones if u.cantidad > 0)

    @property
    def stock_averiado(self):
        """Parte del total que está en zona de averías.

        Pregunta por la política canónica (`picking_service`) en vez de repetir
        el literal de la zona — el trinquete `test_politica_vendible_unica`
        prohíbe la copia, pero lo que rompió acá fue no preguntar en absoluto.
        """
        from app.services.picking_service import es_ubicacion_vendible
        return sum(u.cantidad for u in self.ubicaciones
                   if u.cantidad > 0 and not es_ubicacion_vendible(u.ubicacion))

    @property
    def stock_vendible(self):
        """La parte que SÍ se puede vender.

        Se calcula preguntando la política en positivo, no restando — misma
        forma que `filtro_ubicacion_vendible()` y `filtro_ubicacion_averias()`,
        que son las dos direcciones de la misma pregunta y por eso no pueden
        divergir. Que `vendible + averiado == total` sale de que cada fila cae
        de un lado o del otro, no de una resta que habría que mantener.
        """
        from app.services.picking_service import es_ubicacion_vendible
        return sum(u.cantidad for u in self.ubicaciones
                   if u.cantidad > 0 and es_ubicacion_vendible(u.ubicacion))

    @property
    def stock_disponible(self):
        """Stock total menos reservado y bloqueado, en TODAS las zonas.

        Comparte el sesgo de `stock_total`: es un eje distinto (compromisos),
        no la pregunta de si se puede vender.
        """
        return sum(u.cantidad_disponible() for u in self.ubicaciones)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'categoria': self.categoria,
            'unidad_medida': self.unidad_medida,
            'precio_compra': self.precio_compra,
            'precio_venta': self.precio_venta,
            'stock_minimo': self.stock_minimo,
            'stock_maximo': self.stock_maximo,
            'punto_pedido': self.punto_pedido,
            'clasificacion_abc': self.clasificacion_abc,
            'codigo_siesa': self.codigo_siesa,
            'codigo_barras': self.codigo_barras,
            'codigo_barras_empaque': self.codigo_barras_empaque,
            'unidad_empaque': self.unidad_empaque,
            'factor_conversion': self.factor_conversion or 1,
            'unidad_negocio_id': self.unidad_negocio_id,
            'origen': self.origen,
            'marca_siesa': self.marca_siesa,
            'marca_codigo': self.marca_codigo,
            'origen_fuente': self.origen_fuente,
            'marca_fuente': self.marca_fuente,
            'stock_total': self.stock_total,
            'stock_averiado': self.stock_averiado,
            'stock_vendible': self.stock_vendible,
            'stock_disponible': self.stock_disponible,
            'activo': self.activo
        }