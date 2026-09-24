"""
Fotos diarias de Siesa (m036fotos) — la historia que Siesa no guarda.

Siesa contesta «cómo está ahora»: la existencia de hoy, el saldo abierto de
hoy. Lo que no se fotografía cada día no existe mañana. Estas cuatro tablas
son esa foto, tomada por `app/services/fotos_siesa_service.py` y por nadie más.

## La corrida es la unidad de completitud

Cada lectura a Siesa es una **corrida** (`fotos_siesa_corridas`), con su
`run_id`, su alcance (un CO, una bodega, toda la cartera), su día operativo
Bogotá y un veredicto: `completa` o no, con el motivo.

Las filas llevan el `run_id` de la corrida que las escribió por última vez y
su `completa`. **Para sumar un día se usan solo las filas de la última
corrida completa de ese alcance** (`fotos_siesa_service.filas_vigentes`): una
fila que una corrida posterior ya no trajo conserva su `run_id` viejo y queda
fuera sin que haga falta borrarla.

Lo que NO se hace nunca: escribir un cero cuando la lectura falla. Una
corrida fallida deja su fila en `fotos_siesa_corridas` con `completa=False` y
el motivo, **no pisa ninguna fila existente** y solo agrega las que no
estaban (marcadas `completa=False`: son hechos, pero no un total). Un hueco
declarado se ve; un cero fabricado es una caída de ventas que no ocurrió.

## Protegidas

Las cinco están en `PROTEGIDAS_ANALITICAS` del acta de corte y en
`IRRECUPERABLES` de `verificar_restauracion.py`. Sin FK hacia ninguna tabla
operativa (el corte las vacía).
"""
from datetime import datetime

from app.extensions import db


class TipoFoto:
    VENTAS = 'VENTAS'
    STOCK = 'STOCK'
    CARTERA = 'CARTERA'
    TODOS = (VENTAS, STOCK, CARTERA)


class FotoCorrida(db.Model):
    """Una lectura a Siesa y su veredicto. Una fila por alcance y corrida."""
    __tablename__ = 'fotos_siesa_corridas'

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.String(40), nullable=False, index=True)
    tipo = db.Column(db.String(10), nullable=False)
    #: El CO (ventas), la bodega (stock) o `TODAS` (cartera).
    alcance = db.Column(db.String(10), nullable=False)
    #: El día que se fotografía, en Bogotá.
    dia_operativo = db.Column(db.Date, nullable=False)
    iniciada_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    terminada_at = db.Column(db.DateTime)
    completa = db.Column(db.Boolean, nullable=False, default=False)
    filas = db.Column(db.Integer, nullable=False, default=0)
    paginas = db.Column(db.Integer, nullable=False, default=0)
    #: Por qué no está completa. `NULL` solo si `completa`.
    motivo = db.Column(db.Text)
    #: Datos del veredicto que no son una fila: filas rezagadas del stock,
    #: si el costo se trajo completo, etc. JSON.
    detalle = db.Column(db.Text)

    __table_args__ = (
        db.Index('ix_fotos_corridas_alcance', 'tipo', 'alcance', 'dia_operativo'),
        db.CheckConstraint("tipo IN ('VENTAS','STOCK','CARTERA')",
                           name='ck_fotos_corridas_tipo'),
        db.CheckConstraint('completa = false OR motivo IS NULL',
                           name='ck_fotos_corridas_completa_sin_motivo'),
    )

    def to_dict(self):
        return {
            'run_id': self.run_id, 'tipo': self.tipo, 'alcance': self.alcance,
            'dia_operativo': self.dia_operativo.isoformat(),
            'iniciada_at': self.iniciada_at.isoformat() if self.iniciada_at else None,
            'terminada_at': self.terminada_at.isoformat() if self.terminada_at else None,
            'completa': self.completa, 'filas': self.filas, 'paginas': self.paginas,
            'motivo': self.motivo,
        }


class FotoVentaLinea(db.Model):
    """Una línea de factura de `API_v2_Ventas_Facturas_DesdePedido`.

    Upsert por `f470_rowid` (id único del movimiento en T470): la misma línea
    vista dos veces es una fila. `dia_operativo` es el día del DOCUMENTO
    (`f350_fecha`), que es el día de negocio — no el de la captura.
    """
    __tablename__ = 'foto_ventas_lineas'

    id = db.Column(db.Integer, primary_key=True)
    f470_rowid = db.Column(db.BigInteger, nullable=False)
    run_id = db.Column(db.String(40), nullable=False, index=True)
    completa = db.Column(db.Boolean, nullable=False, default=False)
    dia_operativo = db.Column(db.Date, nullable=False)
    co = db.Column(db.String(10), nullable=False)

    # Documento (la factura)
    f350_rowid = db.Column(db.BigInteger)
    tipo_docto = db.Column(db.String(10))
    consec_docto = db.Column(db.Integer)
    clase_docto = db.Column(db.Integer)
    estado_docto = db.Column(db.Integer)
    # Pedido de origen y su clave de cadena
    pedido_tipo = db.Column(db.String(10))
    pedido_consec = db.Column(db.Integer)
    pedido_co = db.Column(db.String(10))
    pedido_clave = db.Column(db.String(40), index=True)
    # Quién
    cliente_nit = db.Column(db.String(40))
    cliente_razon_social = db.Column(db.String(200))
    cliente_sucursal = db.Column(db.String(10))
    vendedor_codigo = db.Column(db.String(20))
    vendedor_id = db.Column(db.String(40))
    cond_pago = db.Column(db.String(10))
    # Qué y dónde
    bodega = db.Column(db.String(10))
    item_id = db.Column(db.Integer)
    referencia = db.Column(db.String(60))
    concepto = db.Column(db.Integer)
    motivo = db.Column(db.String(10))
    causal_devolucion = db.Column(db.String(10))
    naturaleza = db.Column(db.Integer)
    unidad_negocio = db.Column(db.String(10))
    # Cuánto
    cantidad = db.Column(db.Numeric(16, 4))
    precio_uni = db.Column(db.Numeric(16, 4))
    vlr_bruto = db.Column(db.Numeric(16, 2))
    vlr_dscto = db.Column(db.Numeric(16, 2))
    vlr_imp = db.Column(db.Numeric(16, 2))
    vlr_neto = db.Column(db.Numeric(16, 2))
    costo_prom_tot = db.Column(db.Numeric(16, 2))

    capturada_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    actualizada_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('f470_rowid', name='uq_foto_ventas_f470_rowid'),
        db.Index('ix_foto_ventas_co_dia', 'co', 'dia_operativo'),
        db.Index('ix_foto_ventas_documento', 'tipo_docto', 'consec_docto'),
    )


class FotoStockDiaria(db.Model):
    """Una fila por día × bodega × SKU, copiada de `stock_siesa`.

    `stock_actualizado_at` es la frescura de ESA fila en `stock_siesa` (su
    `updated_at`): la tabla es acumulativa y una fila que Siesa dejó de
    reportar conserva su último valor. `rezagada` marca las que no se
    refrescaron en la última descarga de su bodega — pueden ser un cero que
    nadie escribió.

    El costo sale de `API_v2_Inventarios_InvFecha`, no de `stock_siesa` (que no
    lo tiene). `NULL` = no se pudo traer, **no** «cuesta cero».

    Solo filas con alguna cantidad distinta de cero: dentro de una corrida
    completa, un SKU ausente es un SKU en cero.
    """
    __tablename__ = 'foto_stock_diaria'

    id = db.Column(db.Integer, primary_key=True)
    dia_operativo = db.Column(db.Date, nullable=False)
    bodega = db.Column(db.String(20), nullable=False)
    codigo_siesa = db.Column(db.String(60), nullable=False)
    run_id = db.Column(db.String(40), nullable=False, index=True)
    completa = db.Column(db.Boolean, nullable=False, default=False)
    #: `True` solo para las bodegas que el WMS opera (`_BODEGAS_PV`). AV1 y TRA1
    #: se fotografían pero no son existencia vendible.
    vendible = db.Column(db.Boolean, nullable=False, default=False)
    existencia = db.Column(db.Float)
    comprometido = db.Column(db.Float)
    salida_sin_conf = db.Column(db.Float)
    stock_actualizado_at = db.Column(db.DateTime)
    rezagada = db.Column(db.Boolean, nullable=False, default=False)
    costo_prom_uni = db.Column(db.Numeric(16, 4))
    costo_prom_tot = db.Column(db.Numeric(18, 2))
    capturada_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('dia_operativo', 'bodega', 'codigo_siesa',
                            name='uq_foto_stock_dia_bodega_sku'),
    )


class FotoCarteraDiaria(db.Model):
    """Saldo abierto por documento de `API_v2_CxC_General` (cuentas 1305).

    Una fila por día × `f353_rowid`. `saldo` = débitos − créditos del
    documento. `dias_vencido` se cuenta contra el día de la foto (negativo =
    todavía no vence).
    """
    __tablename__ = 'foto_cartera_diaria'

    id = db.Column(db.Integer, primary_key=True)
    dia_operativo = db.Column(db.Date, nullable=False)
    f353_rowid = db.Column(db.BigInteger, nullable=False)
    run_id = db.Column(db.String(40), nullable=False, index=True)
    completa = db.Column(db.Boolean, nullable=False, default=False)
    co = db.Column(db.String(10))
    tipo_docto = db.Column(db.String(10))
    consec_docto = db.Column(db.Integer)
    nro_cuota = db.Column(db.Integer)
    tercero_id = db.Column(db.String(40))
    sucursal = db.Column(db.String(10))
    cuenta = db.Column(db.String(20))
    unidad_negocio = db.Column(db.String(10))
    fecha_docto = db.Column(db.Date)
    fecha_vcto = db.Column(db.Date)
    total_db = db.Column(db.Numeric(18, 2))
    total_cr = db.Column(db.Numeric(18, 2))
    saldo = db.Column(db.Numeric(18, 2))
    dias_vencido = db.Column(db.Integer)
    capturada_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('dia_operativo', 'f353_rowid',
                            name='uq_foto_cartera_dia_rowid'),
        db.Index('ix_foto_cartera_documento', 'tipo_docto', 'consec_docto'),
    )
