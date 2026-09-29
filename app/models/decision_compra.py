"""
Lo que el comprador decidió sobre una línea de la bandeja (m051comprasf).

La bandeja se recalcula en cada carga: sin esto, entre «Copiar OC» y que la OC
aparezca en el espejo de Siesa la línea seguía ahí —dos personas pedían dos
veces, o el lunes siguiente volvía sin rastro— y no quedaba quién decidió ni
contra qué número.

Una fila por decisión. Nada se borra: una decisión equivocada se ANULA (con
motivo, en la bitácora) y la vigente de una referencia es la última no anulada
cuya vigencia no pasó. La política vive en `app/services/compras_decisiones.py`.

| Acción | Qué es | Vigencia | Efecto |
|---|---|---|---|
| PEDIDO | «Ya se pidió» (OC de Siesa n.º, cantidad) | lead time + 2σ del proveedor (se calcula al leer; la columna es informativa) | Cuenta como «en camino» (`compras_fuentes.en_camino`, fuente `DECISION_WMS`) hasta que aparece la OC en el espejo |
| POSPUESTO | «Lo pospongo hasta <fecha>» | hasta esa fecha (≤ 90 días) | La línea sale de la bandeja, salvo que se vuelva urgente |
| DESCARTADO | «No lo pido» (motivo) | un ciclo de compra | Ídem |
"""
from datetime import datetime

from app.extensions import db


class DecisionCompra(db.Model):
    __tablename__ = 'decision_compra'

    id = db.Column(db.Integer, primary_key=True)
    referencia = db.Column(db.String(50), nullable=False)
    accion = db.Column(db.String(12), nullable=False)          # PEDIDO | POSPUESTO | DESCARTADO
    #: Lo que la bandeja proponía cuando se decidió (el número que se vio).
    cantidad_propuesta = db.Column(db.Numeric(16, 4))
    #: Lo que se pidió de verdad (PEDIDO).
    cantidad_decidida = db.Column(db.Numeric(16, 4))
    #: El número de OC de Siesa tal como lo escribió el comprador.
    oc_siesa = db.Column(db.String(40))
    proveedor_codigo = db.Column(db.String(20))
    motivo = db.Column(db.String(300))
    urgencia_vista = db.Column(db.String(12))
    #: Día Bogotá de la decisión y hasta qué día vale (inclusive).
    dia = db.Column(db.Date, nullable=False)
    vigente_hasta = db.Column(db.Date, nullable=False)
    usuario_id = db.Column(db.Integer)
    usuario_nombre = db.Column(db.String(120))
    creada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: La línea como la vio (JSON): urgencia, cantidad, porqué, precio.
    foto = db.Column(db.Text)
    anulada_en = db.Column(db.DateTime)
    anulada_por_id = db.Column(db.Integer)
    anulada_motivo = db.Column(db.String(300))

    __table_args__ = (
        db.CheckConstraint("accion IN ('PEDIDO', 'POSPUESTO', 'DESCARTADO')",
                           name='ck_decision_compra_accion'),
        db.Index('ix_decision_compra_ref_vigencia', 'referencia', 'vigente_hasta'),
        db.Index('ix_decision_compra_dia', 'dia'),
    )
