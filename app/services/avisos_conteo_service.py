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
    hijos = SesionConteo.query.filter(
        SesionConteo.operario_id == operario_id,
        SesionConteo.es_segundo_conteo.is_(True),
    ).all()

    if not hijos:
        return []

    # Batch-load: recoger todos los sesion_origen_id para resolver raíces en 1-2 queries
    origen_ids = {h.sesion_origen_id for h in hijos if h.sesion_origen_id}
    if not origen_ids:
        return []
    origenes = {s.id: s for s in SesionConteo.query.filter(SesionConteo.id.in_(origen_ids)).all()}

    # Segundo nivel: CC3 → CC2 → CC1
    abuelo_ids = {
        s.sesion_origen_id for s in origenes.values()
        if s.es_segundo_conteo and s.sesion_origen_id and s.sesion_origen_id not in origenes
    }
    if abuelo_ids:
        abuelos = {s.id: s for s in SesionConteo.query.filter(SesionConteo.id.in_(abuelo_ids)).all()}
        origenes.update(abuelos)

    # Resolver raíces en memoria
    raices = {}  # hijo_id → raiz sesion
    for hijo in hijos:
        if not hijo.sesion_origen_id:
            continue
        origen = origenes.get(hijo.sesion_origen_id)
        if not origen:
            continue
        if origen.es_segundo_conteo and origen.sesion_origen_id:
            raiz = origenes.get(origen.sesion_origen_id) or origen
        else:
            raiz = origen
        if raiz.estado in _ESTADOS_ATASCADOS:
            raices[hijo.id] = raiz

    if not raices:
        return []

    # Batch-load SiesaJobs para todas las raíces atascadas — 1 query en vez de N
    raiz_ids = {r.id for r in raices.values()}
    jobs = SiesaJob.query.filter(
        SiesaJob.referencia_tipo == 'SesionConteo',
        SiesaJob.referencia_id.in_(raiz_ids),
        SiesaJob.tipo == 'AJUSTE_CONTEO',
        SiesaJob.estado == EstadoSiesaJob.FALLIDO,
    ).all()

    # Agrupar: raiz_id → job más reciente
    jobs_por_raiz = {}
    for j in jobs:
        prev = jobs_por_raiz.get(j.referencia_id)
        if not prev or j.id > prev.id:
            jobs_por_raiz[j.referencia_id] = j

    avisos = []
    seen_raiz = set()
    for raiz in raices.values():
        if raiz.id in seen_raiz:
            continue
        seen_raiz.add(raiz.id)

        job = jobs_por_raiz.get(raiz.id)
        if not job or not job.error_ultimo:
            continue
        if _SENTINEL_DISPONIBLE not in job.error_ultimo.lower():
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
