"""
Líneas de orden de compra de Siesa, espejadas en el WMS (m046compras).

Una fila por línea de OC (`f421_rowid`), leída de `API_v2_Compras_Ordenes` —la
única consulta de compras con contrato completo en `docs/siesa-specs/` (89
campos, Regla 1)—.

Para qué existe: compras no tenía datos de entrada que se llenaran solos. El
término «en tránsito» del armador (`posicion = stock + en_transito − …`) leía
`ItemEnTransito`, que **no tenía ningún escritor**, así que valía siempre 0.
Con esta tabla, lo que Siesa ya tiene pedido y no ha entrado es «en camino».

**No es solo lo abierto.** También guarda las líneas cumplidas que trae el
historial (`compras_oc_sync.sincronizar_historial`): de ahí salen el lead time
medido por proveedor y el precio de compra por SKU. Por eso se llama
`oc_linea_siesa` y no `oc_abierta_linea`: una tabla que se llamara «abierta» y
guardara cerradas mentiría en su nombre.

Nada se borra. Una línea que deja de aparecer entre las abiertas en un barrido
**completo** se marca `abierta=False` con `cerrada_en`; con un barrido
incompleto no se toca (ver `compras_oc_sync`).

Quién calcula qué:
  · el pendiente de una línea → `compras_fuentes.pendiente_de_linea`
  · «en camino» por SKU       → `compras_fuentes.en_camino`
  · el lead time              → `compras_fuentes.lead_time`
Ningún otro sitio: `tests/test_compras_fuentes_trinquetes.py`.
"""
from datetime import datetime

from app.extensions import db


class OcLineaSiesa(db.Model):
    __tablename__ = 'oc_linea_siesa'

    id = db.Column(db.Integer, primary_key=True)

    # ── Identidad (Regla 18: CO + tipo + consecutivo; la línea por su rowid) ──
    rowid_linea = db.Column(db.BigInteger, nullable=False)      # f421_rowid
    rowid_oc = db.Column(db.BigInteger)                         # f420_rowid
    co = db.Column(db.String(10))                               # f420_id_co
    tipo_docto = db.Column(db.String(10))                       # f420_id_tipo_docto
    consec_docto = db.Column(db.Integer)                        # f420_consec_docto

    # ── Encabezado ──
    fecha_oc = db.Column(db.Date)                               # f420_fecha
    estado_oc = db.Column(db.SmallInteger)                      # f420_ind_estado 1/2/3/9
    fecha_aprobacion = db.Column(db.DateTime)                   # f420_fecha_ts_aprobacion
    fecha_parcial = db.Column(db.DateTime)                      # f420_fecha_ts_parcial
    fecha_cumplido = db.Column(db.DateTime)                     # f420_fecha_ts_cumplido
    moneda = db.Column(db.String(5))                            # f420_id_moneda_docto
    tasa_conv = db.Column(db.Numeric(18, 6))                    # f420_tasa_conv

    proveedor_codigo = db.Column(db.String(20))                 # f200_id_prov
    proveedor_nit = db.Column(db.String(20))                    # f200_nit_prov
    proveedor_nombre = db.Column(db.String(200))                # f200_razon_social_prov
    proveedor_sucursal = db.Column(db.String(10))               # f202_id_sucursal_prov
    proveedor_id = db.Column(db.Integer, db.ForeignKey('proveedores.id'))

    # ── Línea ──
    referencia = db.Column(db.String(50))                       # f120_referencia
    bodega = db.Column(db.String(10))                           # f150_id
    co_movto = db.Column(db.String(10))                         # f421_id_co_movto
    estado_linea = db.Column(db.SmallInteger)                   # f421_ind_estado
    ind_obsequio = db.Column(db.SmallInteger)                   # f421_ind_obsequio
    unidad_medida = db.Column(db.String(10))                    # f421_id_unidad_medida
    factor = db.Column(db.Numeric(18, 6))                       # f421_factor
    cant_pedida = db.Column(db.Numeric(18, 4))                  # f421_cant_pedida
    cant_entrada = db.Column(db.Numeric(18, 4))                 # f421_cant_entrada
    cant_pedida_base = db.Column(db.Numeric(18, 4))             # f421_cant_pedida_base
    cant_entrada_base = db.Column(db.Numeric(18, 4))            # f421_cant_entrada_base
    cant_importacion_base = db.Column(db.Numeric(18, 4))        # f421_cant_importacion_base
    #: En unidad de INVENTARIO (base). `None` = no se pudo llevar a base: el
    #: dato no se inventa y `en_camino` lo cuenta como línea sin unidad.
    pendiente_base = db.Column(db.Numeric(18, 4))
    precio_unitario = db.Column(db.Numeric(18, 4))              # f421_precio_unitario (por unidad de la OC)
    fecha_entrega = db.Column(db.Date)                          # f421_fecha_entrega

    # ── Ciclo de vida en el WMS (UTC, técnicos) ──
    abierta = db.Column(db.Boolean, nullable=False, default=True)
    primera_vista_en = db.Column(db.DateTime, default=datetime.utcnow)
    vista_en = db.Column(db.DateTime)
    cerrada_en = db.Column(db.DateTime)
    #: `NO_APARECE_EN_ABIERTAS` (barrido completo sin ella) · `CUMPLIDA_EN_HISTORIAL`.
    motivo_cierre = db.Column(db.String(40))

    __table_args__ = (
        db.Index('uq_oc_linea_siesa_rowid', 'rowid_linea', unique=True),
        db.Index('ix_oc_linea_siesa_ref_abierta', 'referencia', 'abierta'),
        db.Index('ix_oc_linea_siesa_oc', 'co', 'tipo_docto', 'consec_docto'),
        db.Index('ix_oc_linea_siesa_proveedor', 'proveedor_codigo'),
    )

    @property
    def oc_referencia(self) -> str:
        """`CO-TIPO-CONSEC`, la clave con que un contenedor la cita."""
        return f'{self.co or ""}-{self.tipo_docto or ""}-{self.consec_docto or ""}'

    def to_dict(self):
        def _f(v):
            return float(v) if v is not None else None
        return {
            'rowid_linea': self.rowid_linea,
            'oc': self.oc_referencia,
            'fecha_oc': self.fecha_oc.isoformat() if self.fecha_oc else None,
            'estado_oc': self.estado_oc,
            'proveedor_codigo': self.proveedor_codigo,
            'proveedor_nombre': self.proveedor_nombre,
            'referencia': self.referencia,
            'bodega': self.bodega,
            'pendiente_base': _f(self.pendiente_base),
            'precio_unitario': _f(self.precio_unitario),
            'moneda': self.moneda,
            'fecha_entrega': self.fecha_entrega.isoformat() if self.fecha_entrega else None,
            'abierta': self.abierta,
        }
