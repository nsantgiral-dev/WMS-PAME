"""
Avisos de solo lectura para operarios de conteo cíclico.
No persiste estado ni modifica sesiones/jobs — se recalcula en cada consulta
y desaparece solo cuando la sesión raíz deja de estar atascada.
"""
from app.models.conteo import SesionConteo
from app.models.siesa_job import SiesaJob, EstadoSiesaJob

_SENTINEL_DISPONIBLE = 'sin cantidad disponible'
_ESTADOS_ATASCADOS = ('AJUSTANDO', 'DESCUADRE')


def _resolver_raiz(sesion: SesionConteo):
    """CC2 -> CC1, o CC3 -> CC2 -> CC1."""
    if not sesion.sesion_origen_id:
        return None
    origen = SesionConteo.query.get(sesion.sesion_origen_id)
    if not origen:
        return None
    if origen.es_segundo_conteo and origen.sesion_origen_id:
        return SesionConteo.query.get(origen.sesion_origen_id) or origen
    return origen


def obtener_avisos_pendientes(operario_id: int) -> list:
    """
    Avisos para el operario que hizo un CC2 o CC3 cuyo ajuste raíz falló
    definitivamente en Siesa por disponible negativo (ver diagnóstico
    conteo cíclico 2026-07-02). No expone cifras ni cambia ningún estado.
    """
    avisos = []
    hijos = SesionConteo.query.filter(
        SesionConteo.operario_id == operario_id,
        SesionConteo.es_segundo_conteo.is_(True),
    ).all()

    for hijo in hijos:
        raiz = _resolver_raiz(hijo)
        if not raiz or raiz.estado not in _ESTADOS_ATASCADOS:
            continue

        job_fallido = (
            SiesaJob.query
            .filter_by(
                referencia_tipo='SesionConteo',
                referencia_id=raiz.id,
                tipo='AJUSTE_CONTEO',
                estado=EstadoSiesaJob.FALLIDO,
            )
            .order_by(SiesaJob.id.desc())
            .first()
        )
        if not job_fallido or not job_fallido.error_ultimo:
            continue
        if _SENTINEL_DISPONIBLE not in job_fallido.error_ultimo.lower():
            continue

        avisos.append({
            'sesion_codigo': raiz.codigo,
            'mensaje': (
                'El ajuste de tu conteo no se pudo aplicar por inventario '
                'comprometido en pedidos — tu supervisor debe revisarlo.'
            ),
            'tipo': 'advertencia',
        })

    return avisos
