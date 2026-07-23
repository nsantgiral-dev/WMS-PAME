"""
ProductoBloqueado — Lista de bloqueo de recompra.

Impide que SKUs muertos se recompren. El bloqueo opera en el
nacimiento de la OC (no en recepción). La recepción de un SKU
bloqueado no se rechaza — se registra como fuga.

Desbloqueo requiere persona autorizada (enumerada), cantidad
máxima, y vigencia. Desbloqueo vencido = bloqueado de nuevo.
"""
from datetime import datetime, date
from app.extensions import db


class ProductoBloqueado(db.Model):
    __tablename__ = 'productos_bloqueados'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    motivo = db.Column(db.String(50), nullable=False)
    bloqueado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    bloqueado_por_sistema = db.Column(db.Boolean, default=False)
    fecha_bloqueo = db.Column(db.DateTime, default=datetime.utcnow)
    notas_bloqueo = db.Column(db.Text, nullable=True)

    desbloqueado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    motivo_desbloqueo = db.Column(db.Text, nullable=True)
    cantidad_autorizada = db.Column(db.Integer, nullable=True)
    vigencia_desbloqueo = db.Column(db.Date, nullable=True)
    fecha_desbloqueo = db.Column(db.DateTime, nullable=True)
    activo = db.Column(db.Boolean, default=True, nullable=False)

    producto = db.relationship('Producto', backref='bloqueos', lazy=True)

    def esta_bloqueado(self):
        """True si el bloqueo está activo y no tiene desbloqueo vigente."""
        if not self.activo:
            return False
        if self.desbloqueado_por_id and self.vigencia_desbloqueo:
            if date.today() <= self.vigencia_desbloqueo:
                return False  # desbloqueo temporal vigente
            else:
                # Vigencia expirada → vuelve a estar bloqueado
                return True
        if self.desbloqueado_por_id and not self.vigencia_desbloqueo:
            return False  # desbloqueado sin vigencia (permanente)
        return True

    def to_dict(self):
        return {
            'id': self.id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo_siesa if self.producto else '',
            'producto_nombre': self.producto.nombre if self.producto else '',
            'motivo': self.motivo,
            'bloqueado_por_sistema': self.bloqueado_por_sistema,
            'fecha_bloqueo': self.fecha_bloqueo.isoformat() if self.fecha_bloqueo else None,
            'notas_bloqueo': self.notas_bloqueo or '',
            'esta_bloqueado': self.esta_bloqueado(),
            'desbloqueado_por_id': self.desbloqueado_por_id,
            'motivo_desbloqueo': self.motivo_desbloqueo or '',
            'cantidad_autorizada': self.cantidad_autorizada,
            'vigencia_desbloqueo': self.vigencia_desbloqueo.isoformat() if self.vigencia_desbloqueo else None,
            'fecha_desbloqueo': self.fecha_desbloqueo.isoformat() if self.fecha_desbloqueo else None,
            'activo': self.activo,
        }


class FugaRecompra(db.Model):
    __tablename__ = 'fugas_recompra'

    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    bloqueo_id = db.Column(db.Integer, db.ForeignKey('productos_bloqueados.id'), nullable=False)
    recepcion_id = db.Column(db.Integer, nullable=True)
    oc_siesa = db.Column(db.String(50), nullable=True)
    proveedor = db.Column(db.String(100), nullable=True)
    cantidad_recibida = db.Column(db.Integer, nullable=True)
    fecha = db.Column(db.DateTime, default=datetime.utcnow)
    notas = db.Column(db.Text, nullable=True)

    producto = db.relationship('Producto', lazy=True)
    bloqueo = db.relationship('ProductoBloqueado', lazy=True)
