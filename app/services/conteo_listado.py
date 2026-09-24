"""El listado de Conteos (Inventario Cíclico → Conteos) y su barra de números.

La ruta solo parsea: **qué muestra cada pestaña y qué deja pasar cada filtro
se decide acá**, y la barra de arriba cuenta con las MISMAS consultas que la
lista de abajo. Antes la pestaña se armaba en tres sitios que no coincidían
(2026-09-24):

| Qué | Antes | Ahora |
|---|---|---|
| **Pestaña ⚠ Acción** | El JS mandaba los estados y **filtraba los CC2/CC3 después de paginar**. Un CC2/CC3 que resuelve una cadena queda en DESCUADRE para siempre (`ConteoService._exigir_raiz_para_ajustar`): el total, el contador de la pestaña y las páginas los contaban, así que el contador crecía sin techo y había páginas con menos de 30 tarjetas, o vacías | `VISTAS['accion']` es solo de raíces, **en la consulta** |
| **Pestaña ✓ Resueltos** | Mostraba la raíz y su CC2 como dos resueltos del mismo hueco, y un CC2 cancelado por «saltar recuento» como resuelto mientras la raíz esperaba en Acción | Solo raíces: el resultado es el de la cadena |
| **Filtro «marca»** | Filtraba por `Producto.categoria`, que no es la marca y que ningún sync de Siesa llena | `Producto.marca_siesa`, y la respuesta **declara** si alguna fila la tiene. Ver `disponibilidad_marca` |
| **Almacén** | La barra, «Asignar» y «Exportar» leían el selector escondido de la pestaña ABC; la lista no filtraba por almacén | Un solo filtro de almacén para los cuatro |
| **«Hoy»** | Medianoche UTC: se reiniciaba a las 7 p. m. (Regla 5) | `inicio_del_dia_utc()` |

Trinquete: `tests/test_conteo_filtros.py`.
"""
from sqlalchemy import or_

from app.extensions import db
from app.models.conteo import EstadoConteo, SesionConteo


class FiltroInvalido(ValueError):
    """Un filtro que no se entiende es 400: ignorarlo en silencio devolvería
    la lista de otro filtro con cara de ser la pedida."""


E = EstadoConteo

#: Qué muestra cada pestaña de Conteos. `solo_raices`: la tarjeta de una raíz
#: ya pinta su CC2/CC3 (`to_dict()['segundo_conteo']`); el hijo suelto sería el
#: mismo hueco dos veces. En curso SÍ lleva hijos: un CC2 pendiente es una
#: tarea que alguien tiene que contar.
VISTAS = {
    'accion': {'estados': (E.SEGUNDO_CONTEO, E.TERCER_CONTEO, E.DESCUADRE),
               'solo_raices': True},
    'progreso': {'estados': (E.PENDIENTE, E.EN_PROCESO), 'solo_raices': False},
    'resueltos': {'estados': (E.MATCH, E.AJUSTADO, E.AJUSTANDO, E.CANCELADO),
                  'solo_raices': True},
}

ESTADOS_VALIDOS = frozenset(
    v for k, v in vars(EstadoConteo).items() if k.isupper() and isinstance(v, str))

POR_PAGINA = 30

#: Lo que la pantalla dice cuando se filtra por marca y ningún producto la
#: tiene. No se inventa la marca: el contrato del catálogo
#: (`docs/siesa-specs/API_v2_Items.docx`, 31 campos `f120_*`) no la trae, y en
#: Siesa la marca es un criterio de clasificación del ítem (`t125`, plan +
#: criterio mayor — el plano del 238920), que el WMS no lee por ningún lado.
AVISO_SIN_MARCA = (
    'Ningún producto tiene la marca cargada: el catálogo que baja de Siesa no '
    'la trae (la marca vive en los criterios de clasificación del ítem, que el '
    'WMS todavía no lee). Este filtro no puede encontrar nada hasta que se '
    'defina de dónde se lee.')


def _raiz():
    return or_(SesionConteo.es_segundo_conteo.is_(False),
               SesionConteo.es_segundo_conteo.is_(None))


def _texto(valor):
    return (valor or '').strip()


def _like(texto):
    seguro = texto.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    return f'%{seguro}%'


def normalizar_filtros(*, vista=None, estados=None, estado=None, almacen_id=None,
                       clasificacion=None, operario_id=None, marca=None,
                       categoria=None) -> dict:
    """Valida y normaliza. Todo lo que no se entiende levanta `FiltroInvalido`."""
    vista = _texto(vista).lower() or None
    if vista and vista not in VISTAS:
        raise FiltroInvalido(f'vista inválida: {vista!r} ({", ".join(VISTAS)})')
    lista = [e.strip().upper() for e in (estados or '').split(',') if e.strip()]
    if not lista and _texto(estado):
        lista = [_texto(estado).upper()]
    malos = [e for e in lista if e not in ESTADOS_VALIDOS]
    if malos:
        raise FiltroInvalido(f'estado inválido: {", ".join(malos)}')
    if vista and lista:
        raise FiltroInvalido('vista y estados no van juntos: la vista ya dice qué estados muestra')
    clase = _texto(clasificacion).upper() or None
    if clase:
        from app.services.conteo_politica import CLASES
        if clase not in CLASES:
            raise FiltroInvalido(f'clasificacion inválida: {clase!r} (A, B o C)')
    return {'vista': vista, 'estados': lista, 'almacen_id': almacen_id or None,
            'clasificacion': clase, 'operario_id': operario_id or None,
            'marca': _texto(marca) or None, 'categoria': _texto(categoria) or None}


def consulta(filtros: dict):
    """La consulta de sesiones para `filtros` ya normalizados, sin orden ni
    carga de relaciones: la usan la lista y los contadores."""
    q = SesionConteo.query
    if filtros.get('vista'):
        v = VISTAS[filtros['vista']]
        q = q.filter(SesionConteo.estado.in_(v['estados']))
        if v['solo_raices']:
            q = q.filter(_raiz())
    elif filtros.get('estados'):
        q = q.filter(SesionConteo.estado.in_(filtros['estados']))
    if filtros.get('almacen_id'):
        q = q.filter(SesionConteo.almacen_id == filtros['almacen_id'])
    if filtros.get('clasificacion'):
        q = q.filter(SesionConteo.clasificacion_abc == filtros['clasificacion'])
    if filtros.get('operario_id'):
        q = q.filter(SesionConteo.operario_id == filtros['operario_id'])
    if filtros.get('marca') or filtros.get('categoria'):
        from app.models.producto import Producto
        q = q.join(Producto, SesionConteo.producto_id == Producto.id)
        if filtros.get('marca'):
            q = q.filter(Producto.marca_siesa.ilike(_like(filtros['marca']), escape='\\'))
        if filtros.get('categoria'):
            q = q.filter(Producto.categoria.ilike(_like(filtros['categoria']), escape='\\'))
    return q


def disponibilidad_marca() -> dict:
    """Cuántos productos tienen marca. Con cero, filtrar por marca devuelve
    siempre vacío, y eso se dice en vez de pintarse como «no hay conteos»."""
    from app.models.producto import Producto
    n = (db.session.query(db.func.count(Producto.id))
         .filter(Producto.marca_siesa.isnot(None),
                 db.func.trim(Producto.marca_siesa) != '')
         .scalar()) or 0
    return {'productos_con_marca': n, 'aviso': None if n else AVISO_SIN_MARCA}


def listar(filtros: dict, page: int = 1) -> dict:
    """Una página de la lista. El total y las páginas salen de la MISMA
    consulta que las filas: lo que se cuenta es lo que se pinta."""
    from sqlalchemy.orm import joinedload as _jl
    page = max(1, page or 1)
    q = (consulta(filtros)
         .options(
             _jl(SesionConteo.producto),
             _jl(SesionConteo.ubicacion),
             _jl(SesionConteo.almacen),
             _jl(SesionConteo.operario),
             _jl(SesionConteo.aprobador),
             _jl(SesionConteo.editor),
             _jl(SesionConteo.hijo_conteo).joinedload(SesionConteo.operario),
             _jl(SesionConteo.hijo_conteo).joinedload(SesionConteo.hijo_conteo)
             .joinedload(SesionConteo.operario),
         )
         .order_by(SesionConteo.fecha_creacion.desc(), SesionConteo.id.desc()))
    pagina = q.paginate(page=page, per_page=POR_PAGINA, error_out=False)
    resultado = {
        'sesiones': [s.to_dict() for s in pagina.items],
        'total': pagina.total,
        'pagina_actual': page,
        'total_paginas': pagina.pages,
        'por_pagina': POR_PAGINA,
        'filtros': filtros,
    }
    if filtros.get('marca'):
        resultado['marca'] = disponibilidad_marca()
    return resultado


def contar_vista(vista: str, almacen_id=None) -> int:
    """El número de la pestaña, contado igual que su lista."""
    return consulta(normalizar_filtros(vista=vista, almacen_id=almacen_id)).count()


def barra(almacen_id=None) -> dict:
    """Los números de la barra de Conteos (`GET /api/conteo/stats`)."""
    from datetime import datetime, timedelta
    from sqlalchemy import func
    from app.models.siesa_job import SiesaJob
    from app.utils.fecha import inicio_del_dia_utc

    def _del_almacen(q):
        return q.filter(SesionConteo.almacen_id == almacen_id) if almacen_id else q

    counts = dict(_del_almacen(
        db.session.query(SesionConteo.estado, func.count(SesionConteo.id))
    ).group_by(SesionConteo.estado).all())

    # «Hoy» es el día operativo de Bogotá, no el UTC (Regla 5).
    hoy_completados = _del_almacen(SesionConteo.query.filter(
        SesionConteo.estado.in_([E.MATCH, E.AJUSTADO]),
        SesionConteo.fecha_cierre >= inicio_del_dia_utc())).count()
    atrasados = _del_almacen(SesionConteo.query.filter(
        SesionConteo.estado == E.PENDIENTE,
        SesionConteo.fecha_creacion < datetime.utcnow() - timedelta(days=2))).count()
    # Lo que «Asignar N pendientes» puede repartir: sin dueño y sin CC3, que
    # es de supervisión (`ConteoService.filtros_pool_sin_dueno`) y nunca se
    # asigna en lote. Contarlo prometía un número que el botón no entrega.
    from sqlalchemy.orm import aliased
    origen = aliased(SesionConteo)
    cc3 = (db.session.query(SesionConteo.id)
           .join(origen, SesionConteo.sesion_origen_id == origen.id)
           .filter(SesionConteo.es_segundo_conteo.is_(True),
                   origen.es_segundo_conteo.is_(True)))
    sin_asignar = _del_almacen(SesionConteo.query.filter(
        SesionConteo.estado == E.PENDIENTE,
        SesionConteo.operario_id.is_(None),
        SesionConteo.id.notin_(cc3))).count()
    fallos_dlq = SiesaJob.query.filter(
        SiesaJob.tipo == 'AJUSTE_CONTEO', SiesaJob.estado == 'FALLIDO').count()

    return {
        'pendientes': counts.get(E.PENDIENTE, 0),
        'en_proceso': counts.get(E.EN_PROCESO, 0),
        'segundo_conteo': counts.get(E.SEGUNDO_CONTEO, 0),
        'descuadre': counts.get(E.DESCUADRE, 0),
        'hoy_completados': hoy_completados,
        'atrasados_2d': atrasados,
        'sin_asignar': sin_asignar,
        'accion_requerida': contar_vista('accion', almacen_id),
        'resueltos': contar_vista('resueltos', almacen_id),
        'fallos_dlq': fallos_dlq,
        'almacen_id': almacen_id,
    }


def rango_de_dias(desde=None, hasta=None) -> tuple:
    """`desde`/`hasta` (YYYY-MM-DD, días operativos Bogotá, ambos incluidos)
    como cotas UTC naive `(inicio, fin_exclusivo)`; `None` en la cota que no
    se pidió. El CSV de «Exportar» cortaba por día UTC —lo exportado «hasta el
    10» terminaba a las 7 p. m. del 10 y arrancaba a las 7 p. m. del día
    anterior (Regla 5)— y una fecha ilegible se ignoraba: exportaba TODO con
    cara de ser el rango pedido."""
    from datetime import datetime, timedelta
    from app.utils.fecha import inicio_del_dia_utc
    cotas = []
    for nombre, crudo in (('desde', desde), ('hasta', hasta)):
        crudo = _texto(crudo)
        if not crudo:
            cotas.append(None)
            continue
        try:
            cotas.append(datetime.strptime(crudo, '%Y-%m-%d').date())
        except ValueError:
            raise FiltroInvalido(f'{nombre} inválida: {crudo!r} (formato YYYY-MM-DD)')
    d, h = cotas
    if d and h and d > h:
        raise FiltroInvalido(f'desde ({d}) es posterior a hasta ({h})')
    return (inicio_del_dia_utc(d) if d else None,
            inicio_del_dia_utc(h + timedelta(days=1)) if h else None)
