"""
Los hechos de «¿puede salir este camión?», leídos de la base.

La política vive en `flota/dominio/salida.py`. Acá solo se traducen filas a
hechos, **una función por hecho**, y la bandeja (que lee toda la flota de una
vez) y `evaluar_vehiculo` (que lee uno: el despacho y el conductor) usan las
mismas. Leer en bloque o de a uno cambia cuántas consultas se hacen, no qué
significa un daño vencido o un SOAT sin cargar.

Ningún `except` degrada a «está bien»: lo que no se pudo leer viaja como
`None`/`'sin_dato'` y la política lo pinta ámbar con su motivo (regla 5).
"""
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Tuple

from flota.dominio import salida as dom

# ── Una función por hecho ───────────────────────────────────────────────────

#: Cuál fila «gana» si un tipo tiene varias. Un papel con fechas imposibles
#: va DESPUÉS del vencido a propósito: una fila basura no puede esconder un
#: vencimiento conocido (Regla 0).
_RANGO_PAPEL = {dom.VIGENTE: 0, dom.POR_VENCER: 1, dom.VENCIDO: 2,
                dom.DATO_A_CORREGIR: 3, dom.NO_ENCONTRADO: 4}


def papel_de_fila(d, hoy) -> dom.Papel:
    """El `Papel` de UNA fila de `flota_documento_vehiculo`, con la política
    de lo ya cargado (`dom.papel_cargado`): fechas imposibles → «dato a
    corregir», salvo un vencimiento creíble que ya pasó."""
    return dom.papel_cargado(d.tipo, no_encontrado=d.estado == 'no_encontrado',
                             expedicion=d.fecha_expedicion,
                             vencimiento=d.fecha_vencimiento, hoy=hoy)


def papeles_de_filas(docs: Iterable, hoy) -> Tuple[dom.Papel, ...]:
    """Un `Papel` por tipo conocido; los que no tienen fila, `SIN_CARGAR`.

    Si un tipo tiene más de una fila (el API reemplaza, pero la tabla admite
    varias con número distinto) cuenta **la mejor**: un SOAT renovado no deja
    al camión con el viejo vencido. Empate → la que vence más tarde.
    """
    from flota.dominio.valores import TIPOS_DOCUMENTO

    mejor = {}
    for d in docs:
        papel = papel_de_fila(d, hoy)
        clave = (_RANGO_PAPEL[papel.estado], -(d.fecha_vencimiento.toordinal()
                                                if d.fecha_vencimiento else 0))
        if d.tipo not in mejor or clave < mejor[d.tipo][0]:
            mejor[d.tipo] = (clave, papel)
    return tuple(mejor[t][1] if t in mejor else dom.Papel(t, dom.SIN_CARGAR)
                 for t in TIPOS_DOCUMENTO)


def danos_de_filas(abiertos: Iterable, ahora: datetime) -> Tuple[int, int, int]:
    """(bloqueantes, vencidos, en plazo) — disjuntos, bloqueante primero."""
    from flota.dominio.hallazgo import vencido

    b = v = p = 0
    for h in abiertos:
        if h.criticidad == 'bloqueante':
            b += 1
        elif vencido(h.a_dominio(), ahora):
            v += 1
        else:
            p += 1
    return b, v, p


def inspeccion_de_fila(insp) -> str:
    """La última inspección del día → apta | no_apta | incompleta | sin_hacer."""
    from flota.dominio.inspeccion import APTO, INCOMPLETA, NO_APTO

    if insp is None:
        return 'sin_hacer'
    return {APTO: 'apta', NO_APTO: 'no_apta', INCOMPLETA: 'incompleta'}[insp.veredicto]


def preventivo_de_diagnostico(diag: Optional[Sequence[dict]]
                              ) -> Tuple[Optional[Tuple[dom.TareaVencida, ...]], int]:
    """(tareas vencidas, cuántas por vencer). `None` = no se pudo leer.

    Una tarea **sin línea base** no entra: no está al día ni vencida (su estado
    no es VENCIDA), y decir que «venció» algo que nadie hizo nunca es inventar
    una deuda.
    """
    from flota.dominio.preventivo import EstadoTarea

    if diag is None:
        return None, 0
    vencidas = tuple(
        dom.TareaVencida(d['nombre'],
                         (-d['km_restante'] if isinstance(d['km_restante'], int)
                          and d['km_restante'] < 0 else None))
        for d in diag if d['estado'] == EstadoTarea.VENCIDA)
    por_vencer = sum(1 for d in diag if d['estado'] == EstadoTarea.POR_VENCER)
    return vencidas, por_vencer


def ot_abiertas_de(ordenes: Iterable) -> int:
    return sum(1 for o in ordenes if o.estado == 'abierta')


def custodia_de(c) -> Tuple[str, Optional[int], bool]:
    """(tipo, conductor, fuera de sede) de la custodia activa (o de ninguna)."""
    if c is None:
        return 'sin_turno', None, False
    tipo = ('pendiente_sede' if c.custodio_estado == 'pendiente_sede'
            else c.custodio_tipo)
    return tipo, c.custodio_conductor_id, c.ubicacion == 'fuera_de_sede'


def ficha_de(ficha) -> Tuple[str, Tuple[str, ...]]:
    """(estado, qué falta). **Completa exige la capacidad del tanque**: sin ella
    el detector de sobre-tanqueo está ciego, y una ficha que se declara completa
    con un detector ciego es la evidencia falsa de la regla 1."""
    if ficha is None:
        return 'sin_ficha', ()
    falta = ficha.faltantes()        # la única definición (modelos.FichaTecnica)
    return ('completa' if not falta else 'incompleta'), tuple(falta)


def licencia_de(conductor, hoy) -> Optional[dom.Licencia]:
    """La licencia de un conductor, o `None` si no hay conductor que evaluar."""
    if conductor is None:
        return None
    return dom.estado_de_licencia(numero=conductor.licencia_numero,
                                  categoria=conductor.licencia_categoria,
                                  vence=conductor.licencia_vence, hoy=hoy)


def papeles_de_la_flota(hoy) -> List[Tuple[str, dom.Papel]]:
    """(placa, papel) de cada papel de cada vehículo **activo**, incluidos los
    sin cargar. UNA lectura para el correo diario y el reporte semanal: los dos
    preguntan lo mismo y, escrito dos veces, uno se quedaba sin los vencidos."""
    from app.models.vehiculo import Vehiculo
    from flota.adaptadores.modelos import DocumentoVehiculo

    vehiculos = (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                 .order_by(Vehiculo.placa).all())
    docs = {}
    for d in DocumentoVehiculo.query.filter(
            DocumentoVehiculo.vehiculo_id.in_([v.id for v in vehiculos] or [0])).all():
        docs.setdefault(d.vehiculo_id, []).append(d)
    return [(v.placa, p) for v in vehiculos
            for p in papeles_de_filas(docs[v.id] if v.id in docs else (), hoy)]


def licencias_de_los_conductores(hoy) -> List[Tuple[str, dom.Licencia]]:
    """(nombre, licencia) de cada conductor **activo**."""
    from app.models.conductor import Conductor

    return [(c.nombre, licencia_de(c, hoy)) for c in
            Conductor.query.filter(Conductor.activo.is_(True))
            .order_by(Conductor.nombre).all()]


def dia_de_ruta(r):
    """El día en que ocurrió una ruta: `fecha_programada` manda; sin ella, el
    día en que se cerró el cargue o, en último caso, el que se creó. `None` si
    no tiene ninguna."""
    from app.utils.fecha import dia_operativo_de

    if r.fecha_programada is not None:
        return r.fecha_programada
    if r.fecha_cierre is not None:
        return dia_operativo_de(r.fecha_cierre)
    if r.fecha_creacion is not None:
        return dia_operativo_de(r.fecha_creacion)
    return None


# ── Un vehículo: el despacho y el conductor ─────────────────────────────────

def hechos_de_vehiculo(vehiculo_id: int, *, ahora: Optional[datetime] = None,
                       conductor_de_la_ruta: Optional[int] = None,
                       sale_hoy: Optional[bool] = None) -> dom.Hechos:
    """Los hechos de UN vehículo. `sale_hoy=None` → se mira si tiene una ruta
    hoy (el mismo criterio que la bandeja)."""
    from app.extensions import db
    from app.models.conductor import Conductor
    from app.models.ruta_despacho import RutaDespacho
    from app.models.vehiculo import Vehiculo
    from app.utils.fecha import dia_operativo_de
    from flota.adaptadores import hallazgos as _hall
    from flota.adaptadores import inspecciones as _insp
    from flota.adaptadores import preventivo as _prev
    from flota.adaptadores import taller as _taller
    from flota.adaptadores import traspaso as _trasp
    from flota.adaptadores.modelos import (DocumentoVehiculo, FichaTecnica,
                                           LecturaOdometro)
    from flota.adaptadores.verificacion import pendientes as _dudosas
    from flota.dominio import odometro as dom_odo
    from flota.dominio.valores import SIN_DATO

    ahora = ahora or datetime.utcnow()
    hoy = dia_operativo_de(ahora)
    placa = db.session.get(Vehiculo, vehiculo_id).placa

    insp = _insp.del_dia(vehiculo_id, hoy)
    try:
        diag = _prev.diagnostico_de(vehiculo_id)
    except Exception:          # el conductor recibe su turno igual; se DECLARA
        diag = None
    vencidas, por_vencer = preventivo_de_diagnostico(diag)
    tipo, custodio, fuera = custodia_de(_trasp.custodia_activa(vehiculo_id))
    lecturas = [l.a_dominio() for l in
                LecturaOdometro.query.filter_by(vehiculo_id=vehiculo_id).all()]
    km = dom_odo.odometro_actual(lecturas)
    estado_ficha, falta = ficha_de(
        FichaTecnica.query.filter_by(vehiculo_id=vehiculo_id).first())
    b, v, p = danos_de_filas(_hall.abiertos_de(vehiculo_id), ahora)
    if sale_hoy is None:
        sale_hoy = any(dia_de_ruta(r) == hoy for r in
                       RutaDespacho.query.filter_by(vehiculo_id=vehiculo_id).all())
    return dom.Hechos(
        hoy=hoy,
        papeles=papeles_de_filas(
            DocumentoVehiculo.query.filter_by(vehiculo_id=vehiculo_id).all(), hoy),
        danos_bloqueantes=b, danos_vencidos=v, danos_en_plazo=p,
        inspeccion=inspeccion_de_fila(insp[0] if insp else None),
        sale_hoy=bool(sale_hoy),
        preventivo_vencidas=vencidas, preventivo_por_vencer=por_vencer,
        ot_abiertas=ot_abiertas_de(_taller.ordenes_de(vehiculo_id)),
        km_conocido=km is not SIN_DATO,
        km_dudoso=any(pl == placa for _l, pl in _dudosas()),
        custodia=tipo, custodio_conductor_id=custodio,
        conductor_de_la_ruta=conductor_de_la_ruta,
        fuera_de_sede=fuera, ficha=estado_ficha, ficha_falta=falta,
        # La licencia es de la persona: se evalúa cuando hay a quién (el
        # conductor de la ruta al despachar). Sin él, `None` = no se evalúa.
        licencia=licencia_de(db.session.get(Conductor, conductor_de_la_ruta)
                             if conductor_de_la_ruta is not None else None, hoy),
    )


def evaluar_vehiculo(vehiculo_id: int, **kw) -> dom.Evaluacion:
    return dom.evaluar(hechos_de_vehiculo(vehiculo_id, **kw))


__all__ = ['papel_de_fila', 'licencia_de', 'papeles_de_la_flota',
           'licencias_de_los_conductores', 'papeles_de_filas', 'danos_de_filas', 'inspeccion_de_fila',
           'preventivo_de_diagnostico', 'ot_abiertas_de', 'custodia_de',
           'ficha_de', 'dia_de_ruta', 'hechos_de_vehiculo', 'evaluar_vehiculo']
