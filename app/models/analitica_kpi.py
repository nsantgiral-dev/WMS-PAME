"""
KPI diario de la analítica (m037kpi) — una fila por día × almacén × métrica.

La capa semántica (`app/services/analitica_kpi.py`) define cada métrica UNA
vez; esta tabla guarda lo que esa definición dio cada día operativo (Bogotá).
Es la base de las tendencias y las alertas: el día a día de la operación se
borra con el acta de corte (tareas, recaudos, jobs), y lo que ya se midió no.

- `almacen_id` NULL = el total de la empresa. Único por (día, almacén,
  métrica) con dos índices parciales, porque un UNIQUE normal deja pasar dos
  NULL (PostgreSQL trata cada NULL como distinto).
- `estado`: `OK` · `INCOMPLETO` (hay valor, pero la fuente estaba incompleta:
  es cota, no total) · `AUSENTE` (no hay valor — **nunca un 0**). Lo que no
  es OK dice por qué en `motivo`.
- Toda tasa guarda `numerador` y `denominador` para poder agregarse bien (la
  tasa de una semana es Σnum/Σden, no el promedio de las tasas diarias) y `n`
  para que el denominador se vea.

**Sin claves foráneas, a propósito**: está en `PROTEGIDAS_ANALITICAS` del acta
de corte e `IRRECUPERABLES` en la verificación de respaldo. Las filas de las
que salió cada número (tareas, recaudos, jobs) se vacían en el corte; el número
no se puede volver a calcular después.
"""
import json
from datetime import datetime

from app.extensions import db


class EstadoKpi:
    OK = 'OK'
    INCOMPLETO = 'INCOMPLETO'
    AUSENTE = 'AUSENTE'
    TODOS = (OK, INCOMPLETO, AUSENTE)


class AnaliticaKpiDiario(db.Model):
    __tablename__ = 'analitica_kpi_diario'

    id = db.Column(db.Integer, primary_key=True)
    dia_operativo = db.Column(db.Date, nullable=False)
    #: NULL = total. Sin FK: el almacén es maestro, pero la fila tiene que
    #: sobrevivir a cualquier limpieza de la que el almacén forme parte.
    almacen_id = db.Column(db.Integer, nullable=True)
    metrica = db.Column(db.String(40), nullable=False)
    valor = db.Column(db.Numeric(20, 6), nullable=True)
    n = db.Column(db.Integer, nullable=True)
    numerador = db.Column(db.Numeric(20, 4), nullable=True)
    denominador = db.Column(db.Numeric(20, 4), nullable=True)
    estado = db.Column(db.String(12), nullable=False)
    motivo = db.Column(db.Text, nullable=True)
    fuente = db.Column(db.String(160), nullable=True)
    #: JSON con el desglose que explica el número (sin precio, excluidos…).
    detalle = db.Column(db.Text, nullable=True)
    calculado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.Index('uq_kpi_diario_almacen', 'dia_operativo', 'almacen_id', 'metrica',
                 unique=True,
                 postgresql_where=db.text('almacen_id IS NOT NULL'),
                 sqlite_where=db.text('almacen_id IS NOT NULL')),
        db.Index('uq_kpi_diario_total', 'dia_operativo', 'metrica', unique=True,
                 postgresql_where=db.text('almacen_id IS NULL'),
                 sqlite_where=db.text('almacen_id IS NULL')),
        db.Index('ix_kpi_diario_metrica_dia', 'metrica', 'dia_operativo'),
        db.CheckConstraint("estado IN ('OK','INCOMPLETO','AUSENTE')",
                           name='ck_kpi_diario_estado'),
        # AUSENTE no lleva valor; OK e INCOMPLETO sí. Un AUSENTE con 0 es
        # exactamente el cero fabricado que esta tabla existe para impedir.
        db.CheckConstraint("(estado = 'AUSENTE') = (valor IS NULL)",
                           name='ck_kpi_diario_ausente_sin_valor'),
        db.CheckConstraint("estado = 'OK' OR motivo IS NOT NULL",
                           name='ck_kpi_diario_motivo'),
    )

    def detalle_dict(self) -> dict:
        try:
            return json.loads(self.detalle) if self.detalle else {}
        except (TypeError, ValueError):
            return {'ilegible': True}

    def to_dict(self) -> dict:
        def _f(v):
            return float(v) if v is not None else None
        return {
            'dia': self.dia_operativo.isoformat(),
            'almacen_id': self.almacen_id,
            'metrica': self.metrica,
            'valor': _f(self.valor),
            'n': self.n,
            'numerador': _f(self.numerador),
            'denominador': _f(self.denominador),
            'estado': self.estado,
            'motivo': self.motivo,
            'fuente': self.fuente,
            'detalle': self.detalle_dict(),
            'calculado_en': self.calculado_en.isoformat() if self.calculado_en else None,
        }
