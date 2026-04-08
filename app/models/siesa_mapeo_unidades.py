from app.extensions import db


class SiesaMapeоUnidades(db.Model):
    """
    Tabla de configuración administrable que mapea el Tipo de Inventario
    de Siesa (f120_id_tipo_inv_serv) a la Unidad de Negocio (f441_id_un_movto).

    El administrador mantiene esta tabla vía UI o API. El sync nocturno la
    consulta para poblar productos.unidad_negocio_id automáticamente.

    Si Siesa crea un tipo de inventario nuevo, el producto queda con
    unidad_negocio_id=NULL y se genera una alerta en admin_alertas.
    """
    __tablename__ = 'siesa_mapeo_unidades'

    id = db.Column(db.Integer, primary_key=True)
    tipo_inv_siesa = db.Column(db.String(50), unique=True, nullable=False,
                               comment='Valor exacto de f120_id_tipo_inv_serv en Siesa')
    unidad_negocio_id = db.Column(db.String(10), nullable=False,
                                  comment='Código Siesa: 001=PAPELERIA 011=HOGAR 014=IMPRESION etc.')
    descripcion = db.Column(db.String(200),
                            comment='Nota libre del administrador')

    def to_dict(self):
        return {
            'id': self.id,
            'tipo_inv_siesa': self.tipo_inv_siesa,
            'unidad_negocio_id': self.unidad_negocio_id,
            'descripcion': self.descripcion,
        }
