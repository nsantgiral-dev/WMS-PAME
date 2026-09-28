"""**A quién se le puede dar este trabajo** — presencia + elegibilidad, una política.

## El modelo (2026-09-27)

**Pull por defecto.** El trabajo vive en una cola sin dueño; quien está
presente pide la siguiente tarea y la cola unificada
(`MobileService.get_tarea_actual`) decide la prioridad y aplica las reglas
(doble ciego, zona RESERVA, pedido chico). Quien pide trabajo está presente
por definición: pedirlo es su señal (`presencia.registrar_senal`).

**Push solo como excepción, y con vencimiento.** Cuando un líder (o el
sistema, para el 2º conteo) le pone dueño a una tarea, la persona tiene que
poder hacerla **y** estar disponible ahora. Si deja de estarlo —ausencia
declarada, o dos horas sin señal— lo que tenía asignado y no había empezado
vuelve a la cola sola (`barrer`, cada 15 min; `devolver_trabajo_de`, al
declarar la ausencia). Una asignación no es una propiedad: es una reserva que
vence.

## Lo que decide cada función

- `motivo_no_elegible(usuario, tipo, …)`: ¿su puesto hace este trabajo? El
  jefe de almacén y el supervisor no hacen conteos rutinarios (decisión del
  dueño: solo el definitivo); el liquidador no hace ninguno.
- `motivo_no_asignable(…)`: lo anterior **y** `presencia.estado`. Es la
  política; `exigir_asignable` (levanta, para las acciones del líder) y
  `asignable_o_none` (degrada a la cola, para los flujos del sistema) son sus
  dos usos.
- `candidatos(tipo, …)`: la lista de destinatarios que ve el líder: solo
  quienes pueden **y** están; aparte, con su motivo, quienes podrían pero no
  están (para que la pantalla diga «Luis: incapacidad — regresa el 30/09»).
- `plan_reparto_conteos` / `repartir_conteos`: «Repartir N pendientes». El
  número del botón, la vista previa y lo que se ejecuta salen de **la misma
  función**.

Trinquete: `tests/test_asignacion_presencia.py` — toda escritura de
`operario_id`/`abastecedor_id` distinta de `None` vive en una función que
llama a esta política, o en un inventario declarado de autoasignaciones (el
que pide es el asignado) que solo encoge.
"""
import logging
from datetime import datetime

from app.extensions import db
from app.services import presencia

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Tipos de trabajo
# ─────────────────────────────────────────────────────────────────────────────

CONTEO = 'CONTEO'                        # 1º y 2º conteo (rutinario)
CONTEO_DEFINITIVO = 'CONTEO_DEFINITIVO'  # 3º conteo (CC3), de supervisión
PICKING = 'PICKING'                      # picking de pedido
PICKING_TRASLADO = 'PICKING_TRASLADO'    # picking de traslado, por bodega origen
REPOSICION = 'REPOSICION'                # RESERVA → PICKING
TIPOS = (CONTEO, CONTEO_DEFINITIVO, PICKING, PICKING_TRASLADO, REPOSICION)

#: Por qué se soltó una asignación (va a la bitácora y a `conteos_descartados`).
MOTIVO_AUSENCIA = 'AUSENCIA'
MOTIVO_SIN_SENAL = 'SIN_SENAL'
MOTIVO_INACTIVO = 'INACTIVO'

#: Cupo de referencia para repartir a quien tiene «sin límite» (0) cuando nadie
#: más declara uno: el default de la columna `capacidad_diaria_conteo`.
CUPO_REFERENCIA = 15

_ROL_TEXTO = {
    'admin': 'Un administrador', 'supervisor': 'Un supervisor',
    'jefe_almacen': 'El jefe de almacén', 'gerente': 'Un gerente',
    'empacador': 'Un empacador', 'recepcionista': 'Un recepcionista',
    'tienda': 'Un usuario de tienda', 'conductor': 'Un conductor',
    'compras': 'Compras', 'control_flota': 'Control de flota',
    'packer_traslado': 'Un empacador de traslados', 'lider_cartera': 'El líder de cartera',
    'liquidador': 'Un liquidador',
}


class NoAsignable(ValueError):
    """La persona no puede recibir este trabajo ahora (puesto o presencia)."""


def _rol_texto(u) -> str:
    return _ROL_TEXTO.get(u.rol, f'El rol «{u.rol}»')


def tipo_de_sesion(sesion) -> str:
    """CONTEO_DEFINITIVO para un CC3, CONTEO para CC1/CC2."""
    from app.services.conteo_service import ConteoService
    return CONTEO_DEFINITIVO if ConteoService._es_conteo_definitivo(sesion) else CONTEO


def _almacen_de(u):
    from app.services.conteo_service import ConteoService
    return ConteoService.almacen_de_usuario(u)


def _pica(u) -> bool:
    return u.puede_picar is not False


# ─────────────────────────────────────────────────────────────────────────────
# Elegibilidad: ¿su puesto hace este trabajo?
# ─────────────────────────────────────────────────────────────────────────────

def motivo_no_elegible(usuario, tipo: str, *, almacen_id=None, bodega=None):
    """Texto de por qué el puesto de `usuario` no hace `tipo`, o `None`.

    No mira la presencia: eso es `motivo_no_asignable`. `almacen_id` y
    `bodega`, si se pasan, exigen que la persona sea de ahí (sin almacén
    resoluble no pertenece: Regla 0, como `alcance.bodega_del_usuario`).
    """
    from app.routes._auth_helpers import Roles
    if tipo not in TIPOS:
        raise ValueError(f'Tipo de trabajo desconocido: {tipo!r}')
    if usuario is None:
        return 'La persona no existe'
    if not usuario.activo:
        return f'{usuario.nombre} está inactivo en el maestro de usuarios'
    rol = usuario.rol

    if tipo == CONTEO:
        if not (rol == Roles.PICKER_TRASLADO or (rol == Roles.OPERARIO and _pica(usuario))):
            return (f'{_rol_texto(usuario)} no hace conteos rutinarios (1º y 2º): '
                    'esos los hacen los operarios que pican. '
                    + ('Supervisión hace el conteo definitivo.'
                       if rol in Roles.SUPERVISION else ''))
        if almacen_id and _almacen_de(usuario) != almacen_id:
            return f'{usuario.nombre} es de otro almacén'
        return None

    if tipo == CONTEO_DEFINITIVO:
        if rol not in Roles.SUPERVISION:
            return ('El conteo definitivo (CC3) solo lo hace un supervisor, '
                    'admin o jefe de almacén')
        return None

    if tipo == PICKING:
        if not (rol == Roles.OPERARIO and _pica(usuario)):
            return f'{_rol_texto(usuario)} no recibe picking de pedidos asignado'
        if almacen_id and _almacen_de(usuario) != almacen_id:
            return f'{usuario.nombre} es de otro almacén'
        return None

    if tipo == PICKING_TRASLADO:
        from app.services.alcance import bodega_del_usuario
        if not (rol == Roles.PICKER_TRASLADO or (rol == Roles.OPERARIO and _pica(usuario))):
            return f'{_rol_texto(usuario)} no pica traslados'
        if not bodega:
            return 'No se sabe la bodega de origen del traslado'
        if bodega_del_usuario(usuario) != bodega:
            return f'{usuario.nombre} no es de la bodega {bodega}, de donde sale el traslado'
        return None

    # REPOSICION
    if not usuario.puede_abastecer:
        return f'{usuario.nombre} no tiene el permiso de abastecedor'
    if almacen_id and _almacen_de(usuario) != almacen_id:
        return f'{usuario.nombre} es de otro almacén'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# La política: puesto + presencia
# ─────────────────────────────────────────────────────────────────────────────

def motivo_no_asignable(usuario, tipo: str, *, ahora=None, **ctx):
    """¿Por qué no se le puede asignar `tipo` a `usuario` ahora? Texto o `None`."""
    m = motivo_no_elegible(usuario, tipo, **ctx)
    if m:
        return m
    p = presencia.estado(usuario, ahora=ahora)
    if not p['disponible']:
        return f'{usuario.nombre} {p["texto"]}: no se le asigna trabajo'
    return None


def exigir_asignable(usuario_id, tipo: str, **ctx):
    """La persona, si se le puede asignar `tipo` ahora; si no, `NoAsignable`.
    Para las acciones de un líder: el motivo vuelve a la pantalla."""
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, int(usuario_id)) if usuario_id else None
    if u is None:
        raise LookupError(f'Operario {usuario_id} no encontrado')
    m = motivo_no_asignable(u, tipo, **ctx)
    if m:
        raise NoAsignable(m)
    return u


def asignable_o_none(usuario_id, tipo: str, **ctx):
    """`usuario_id` si se le puede asignar `tipo` ahora; si no, `None` — la
    tarea nace en la cola en vez de pegada a alguien que no está. Para los
    flujos del sistema, donde no hay a quién contestarle (Regla 0: la cola es
    lo reversible; una tarea pegada a un ausente no la hace nadie)."""
    if not usuario_id:
        return None
    from app.models.usuario import Usuario
    u = db.session.get(Usuario, int(usuario_id))
    m = motivo_no_asignable(u, tipo, **ctx) if u else 'no existe'
    if m:
        logger.warning('[ASIGNACION] %s no se asigna a #%s (%s): queda en la cola',
                       tipo, usuario_id, m)
        return None
    return u.id


# ─────────────────────────────────────────────────────────────────────────────
# Quiénes: la lista que ve el líder
# ─────────────────────────────────────────────────────────────────────────────

def _usuarios_activos():
    from app.models.usuario import Usuario
    return Usuario.query.filter(Usuario.activo.is_(True)).order_by(Usuario.nombre, Usuario.id).all()


def cupo_conteo(usuario, *, ahora=None) -> dict:
    """El cupo diario de conteo de la persona hoy.

    `usados_hoy`: conteos que empezó hoy (día operativo de Bogotá — la misma
    medida que el intercalado del picking); `en_cola`: los que tiene asignados
    y no empezó. `restante` es `None` si su cupo es 0 («sin límite»).
    """
    from datetime import timedelta
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.utils.fecha import dia_operativo_de, inicio_del_dia_utc
    dia = dia_operativo_de(ahora or datetime.utcnow())
    ini, fin = inicio_del_dia_utc(dia), inicio_del_dia_utc(dia + timedelta(days=1))
    usados = SesionConteo.query.filter(SesionConteo.operario_id == usuario.id,
                                       SesionConteo.fecha_inicio >= ini,
                                       SesionConteo.fecha_inicio < fin).count()
    en_cola = SesionConteo.query.filter(SesionConteo.operario_id == usuario.id,
                                        SesionConteo.estado == EstadoConteo.PENDIENTE).count()
    cupo = usuario.capacidad_diaria_conteo if usuario.capacidad_diaria_conteo is not None \
        else CUPO_REFERENCIA
    restante = None if cupo <= 0 else max(0, cupo - usados - en_cola)
    return {'cupo': cupo, 'usados_hoy': usados, 'en_cola': en_cola, 'restante': restante}


def candidatos(tipo: str, *, almacen_id=None, bodega=None, ahora=None) -> dict:
    """`{disponibles, no_disponibles}` para un desplegable de destinatarios.

    `disponibles`: pueden hacer `tipo` y están. `no_disponibles`: pueden
    hacerlo pero no están, cada uno con su motivo. Quien no hace ese trabajo
    (el jefe para un conteo rutinario) **no aparece en ninguna**.
    """
    usuarios = [u for u in _usuarios_activos()
                if motivo_no_elegible(u, tipo, almacen_id=almacen_id, bodega=bodega) is None]
    pres = presencia.estados(usuarios, ahora=ahora)
    disp, no_disp = [], []
    for u in usuarios:
        p = pres[u.id]
        fila = {'id': u.id, 'nombre': u.nombre, 'rol': u.rol,
                'presencia': p['codigo'], 'presencia_texto': p['texto']}
        if p['disponible']:
            if tipo == CONTEO:
                fila['cupo'] = cupo_conteo(u, ahora=ahora)
            disp.append(fila)
        else:
            no_disp.append(fila)
    return {'tipo': tipo, 'disponibles': disp, 'no_disponibles': no_disp}


# ─────────────────────────────────────────────────────────────────────────────
# «Repartir N pendientes» — una función para el botón, la vista previa y el POST
# ─────────────────────────────────────────────────────────────────────────────

def _pool_conteo(almacen_id):
    from app.models.conteo import SesionConteo
    from app.services.conteo_service import ConteoService
    return SesionConteo.query.filter(*ConteoService.filtros_pool_sin_dueno(None, almacen_id))


def contar_pool_conteo(almacen_id=None) -> int:
    """Conteos sin dueño que el líder puede repartir (CC1 y CC2, nunca CC3).
    Sin almacén, la suma de todos: el número de la barra en «todos»."""
    if almacen_id:
        return _pool_conteo(almacen_id).count()
    from app.models.almacen import Almacen
    return sum(_pool_conteo(a.id).count() for a in Almacen.query.all())


def plan_reparto_conteos(almacen_id, *, cantidad=None, operario_ids=None, ahora=None,
                         _con_asignaciones=False) -> dict:
    """Cómo se repartirían los conteos sin dueño de `almacen_id` entre los
    presentes que cuentan, sin escribir nada.

    - Solo quienes hacen conteo rutinario y están disponibles
      (`candidatos(CONTEO)`); `operario_ids` restringe a algunos de ellos.
    - Cada uno recibe hasta su cupo restante del día; el reparto iguala la
      fracción de cupo usada (con cupo 20 y 20 conteos para tres personas
      frescas: 7, 7, 6). Quien tiene cupo 0 («sin límite») pesa como el mayor
      cupo declarado, o `CUPO_REFERENCIA`.
    - Doble ciego: un CC2 nunca va a quien contó su CC1.
    - En orden de reparto (`orden_de_reparto`): las auditorías primero.
    """
    from app.services.conteo_politica import orden_de_reparto
    from app.services.conteo_service import ConteoService

    pendientes = contar_pool_conteo(almacen_id) if almacen_id else 0
    cands = candidatos(CONTEO, almacen_id=almacen_id, ahora=ahora) if almacen_id \
        else {'disponibles': [], 'no_disponibles': []}
    disponibles = cands['disponibles']
    rechazados = []
    if operario_ids:
        pedidos = {int(i) for i in operario_ids}
        ids_disp = {c['id'] for c in disponibles}
        rechazados = sorted(pedidos - ids_disp)
        disponibles = [c for c in disponibles if c['id'] in pedidos]

    finitos = [c['cupo']['cupo'] for c in disponibles if c['cupo']['restante'] is not None]
    referencia = max(finitos) if finitos else CUPO_REFERENCIA
    personas = []
    for c in disponibles:
        cu = c['cupo']
        personas.append({
            'id': c['id'], 'nombre': c['nombre'],
            'cupo': cu['cupo'], 'usados_hoy': cu['usados_hoy'], 'en_cola': cu['en_cola'],
            'restante': cu['restante'], 'recibe': 0,
            '_peso': cu['cupo'] if cu['restante'] is not None else referencia,
        })

    capacidad = None if any(p['restante'] is None for p in personas) \
        else sum(p['restante'] for p in personas)
    tope = pendientes
    if cantidad is not None:
        tope = min(tope, max(0, int(cantidad)))
    if capacidad is not None:
        tope = min(tope, capacidad)

    asignaciones = []
    saltadas_doble_ciego = 0
    if tope > 0 and personas:
        # Margen para los CC2 que nadie de los elegidos puede hacer.
        tareas = _pool_conteo(almacen_id).order_by(*orden_de_reparto()).limit(tope * 2 + 20).all()
        for t in tareas:
            if len(asignaciones) >= tope:
                break
            previos = ConteoService._operarios_previos_de_la_cadena(t)
            aptos = [p for p in personas
                     if p['id'] not in previos and (p['restante'] is None or p['restante'] > 0)]
            if not aptos:
                saltadas_doble_ciego += 1
                continue
            p = min(aptos, key=lambda x: ((x['usados_hoy'] + x['en_cola'] + x['recibe']) / x['_peso'],
                                          x['nombre'], x['id']))
            asignaciones.append((t.id, p['id']))
            p['recibe'] += 1
            if p['restante'] is not None:
                p['restante'] -= 1

    a_repartir = len(asignaciones)
    motivo = None
    if not almacen_id:
        motivo = 'Elija un almacén: el reparto es por almacén.'
    elif pendientes == 0:
        motivo = 'No hay conteos sin dueño en este almacén.'
    elif not cands['disponibles']:
        motivo = ('Nadie que haga conteos está en turno ahora'
                  + (': ' + '; '.join(f'{n["nombre"]} {n["presencia_texto"]}'
                                      for n in cands['no_disponibles'])
                     if cands['no_disponibles'] else '')
                  + '. Los conteos siguen en la cola: el primero que entre los toma.')
    elif not personas:
        motivo = 'Ninguna de las personas elegidas está disponible.'
    elif a_repartir == 0 and capacidad == 0:
        motivo = 'Los presentes ya llenaron su cupo de conteo de hoy.'
    elif a_repartir == 0:
        motivo = ('Ninguno de los presentes puede hacer estos conteos: son segundos conteos '
                  'de algo que ellos mismos contaron (doble ciego).')

    resultado = {
        'almacen_id': almacen_id,
        'pendientes': pendientes,
        'a_repartir': a_repartir,
        'quedan_en_pool': pendientes - a_repartir,
        'por_persona': [{k: v for k, v in p.items() if not k.startswith('_')} for p in personas],
        'no_disponibles': cands['no_disponibles'],
        'rechazados': rechazados,
        'saltadas_doble_ciego': saltadas_doble_ciego,
        'motivo_sin_reparto': motivo,
    }
    if _con_asignaciones:
        resultado['_asignaciones'] = asignaciones
    return resultado


def repartir_conteos(almacen_id, *, por_id: int, cantidad=None, operario_ids=None) -> dict:
    """Ejecuta `plan_reparto_conteos` —el mismo cálculo que vio el líder— y
    deja cada asignación en la bitácora. Hace commit.

    Cada conteo se vuelve a verificar bajo lock (sigue sin dueño y la persona
    sigue pudiendo contarlo): entre la vista previa y el clic alguien pudo
    tomarlo de la cola, y eso no es un error — simplemente no se reparte.
    """
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.models.usuario import Usuario
    from app.services.bitacora import registrar_accion
    from app.services.conteo_service import ConteoService

    plan = plan_reparto_conteos(almacen_id, cantidad=cantidad, operario_ids=operario_ids,
                                _con_asignaciones=True)
    asignaciones = plan.pop('_asignaciones')
    hechas = {}
    for sid, uid in asignaciones:
        s = (SesionConteo.query.filter_by(id=sid)
             .with_for_update(skip_locked=True).first())
        if s is None or s.estado != EstadoConteo.PENDIENTE or s.operario_id is not None:
            continue
        u = db.session.get(Usuario, uid)
        if motivo_no_asignable(u, CONTEO, almacen_id=almacen_id) \
                or ConteoService.motivo_no_puede_contar(s, uid):
            continue
        s.operario_id = uid
        registrar_accion('REASIGNAR', s, usuario_id=por_id,
                         motivo='Reparto de conteos entre los presentes',
                         antes={'operario_id': None}, despues={'operario_id': uid})
        hechas[uid] = hechas.get(uid, 0) + 1
    db.session.commit()
    for p in plan['por_persona']:
        p['recibe'] = hechas.get(p['id'], 0)
    plan['asignadas'] = sum(hechas.values())
    plan['quedan_en_pool'] = contar_pool_conteo(almacen_id)
    logger.info('[ASIGNACION] reparto de conteos en almacén %s por #%s: %s',
                almacen_id, por_id, hechas)
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# Asignar una tarea puntual (push del líder)
# ─────────────────────────────────────────────────────────────────────────────

def elegir_para_segundo_conteo(cc2, *, excluir_ids=()):
    """A quién se le da un CC2 recién nacido: la persona **presente** que hace
    conteo rutinario en ese almacén, no contó nada de la cadena, no tiene ya un
    conteo vivo sobre ese mismo hueco, y va con menos carga. `None` si no hay
    nadie: el CC2 queda en la cola (cualquiera presente lo toma, y el doble
    ciego lo protege ahí también).

    Antes era «el operario de id más bajo del almacén», viniera o no; y si el
    primer conteo lo había hecho un jefe o un admin, el CC2 iba a un jefe o a
    un admin — que no hacen conteos rutinarios.
    """
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.services.conteo_service import ConteoService
    previos = ConteoService._operarios_previos_de_la_cadena(cc2) | set(excluir_ids)
    en_conflicto = {r[0] for r in db.session.query(SesionConteo.operario_id).filter(
        SesionConteo.producto_id == cc2.producto_id,
        SesionConteo.ubicacion_id == cc2.ubicacion_id,
        SesionConteo.estado.in_([EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO]),
        SesionConteo.operario_id.isnot(None)).all()}
    aptos = [c for c in candidatos(CONTEO, almacen_id=cc2.almacen_id)['disponibles']
             if c['id'] not in previos and c['id'] not in en_conflicto]
    if not aptos:
        return None

    def carga(c):
        cu = c['cupo']
        return (cu['usados_hoy'] + cu['en_cola'], c['id'])
    return min(aptos, key=carga)['id']


# ─────────────────────────────────────────────────────────────────────────────
# Soltar: ausencia declarada, o sin señal
# ─────────────────────────────────────────────────────────────────────────────

def devolver_trabajo_de(usuario_id: int, *, motivo: str, incluir_en_curso: bool,
                        por_id: int = None, origen: str = None) -> dict:
    """Devuelve a la cola lo que `usuario_id` tenía asignado. No hace commit.

    - Conteos PENDIENTES con su nombre → cola (`devolver_al_pool`).
    - Picking PENDIENTE con su nombre (un traslado asignado) → cola
      (`PickingService._soltar_operario`, que conserva quién lo tenía).
    - Con `incluir_en_curso` (ausencia declarada, usuario inactivo): también
      sus conteos EN_PROCESO (lo contado queda en `conteos_descartados`) y su
      reposición EN_PROCESO.
    - **Nunca** un picking o un empaque EN_PROCESO: hay mercancía física a
      medio mover (en un carro, en una caja). Esos se listan en
      `requieren_decision` y los ve el líder (`equipo`).
    """
    from app.models.conteo import EstadoConteo, MotivoDescarteConteo, SesionConteo
    from app.models.packing import TareaPacking
    from app.models.picking import EstadoPicking, TareaPicking
    from app.models.tarea_reposicion import TareaReposicion
    from app.services.bitacora import registrar_accion
    from app.services.conteo_service import ConteoService
    from app.services.picking_service import PickingService
    from app.services.reposicion_service import soltar_abastecedor

    motivo_conteo = MotivoDescarteConteo.SIN_SENAL if motivo == MOTIVO_SIN_SENAL \
        else MotivoDescarteConteo.AUSENCIA
    texto = {MOTIVO_AUSENCIA: 'Su dueño está ausente',
             MOTIVO_SIN_SENAL: f'Su dueño lleva más de {presencia.VENTANA_SENAL_HORAS} h sin señal',
             MOTIVO_INACTIVO: 'Su dueño está inactivo'}.get(motivo, motivo)
    origen = origen or ('barrido de asignaciones' if por_id is None else None)
    out = {'conteos': 0, 'picking': 0, 'reposicion': 0, 'requieren_decision': []}

    estados_conteo = [EstadoConteo.PENDIENTE] + ([EstadoConteo.EN_PROCESO] if incluir_en_curso else [])
    for s in SesionConteo.query.filter(SesionConteo.operario_id == usuario_id,
                                       SesionConteo.estado.in_(estados_conteo)).all():
        antes = {'operario_id': s.operario_id, 'estado': s.estado}
        ConteoService.devolver_al_pool(s, motivo_conteo)
        registrar_accion('DESASIGNAR', s, usuario_id=por_id, motivo=f'{texto}: vuelve a la cola',
                         antes=antes, despues={'operario_id': None, 'estado': s.estado},
                         origen=origen)
        out['conteos'] += 1

    for t in TareaPicking.query.filter(TareaPicking.operario_id == usuario_id,
                                       TareaPicking.estado == EstadoPicking.PENDIENTE).all():
        PickingService._soltar_operario(t)
        registrar_accion('DESASIGNAR', t, usuario_id=por_id, motivo=f'{texto}: vuelve a la cola',
                         antes={'operario_id': usuario_id}, despues={'operario_id': None},
                         origen=origen)
        out['picking'] += 1

    if incluir_en_curso:
        for r in TareaReposicion.query.filter(TareaReposicion.abastecedor_id == usuario_id,
                                              TareaReposicion.estado == 'EN_PROCESO').all():
            soltar_abastecedor(r)
            registrar_accion('DESASIGNAR', r, usuario_id=por_id, motivo=f'{texto}: vuelve a la cola',
                             antes={'abastecedor_id': usuario_id}, despues={'abastecedor_id': None},
                             origen=origen)
            out['reposicion'] += 1

    for t in TareaPicking.query.filter(TareaPicking.operario_id == usuario_id,
                                       TareaPicking.estado == EstadoPicking.EN_PROCESO).all():
        out['requieren_decision'].append({'tipo': 'PICKING', 'id': t.id, 'codigo': t.codigo,
                                          'referencia': t.referencia_documento})
    for k in TareaPacking.query.filter(TareaPacking.empacador_id == usuario_id,
                                       TareaPacking.estado == 'EN_PROCESO').all():
        out['requieren_decision'].append({'tipo': 'PACKING', 'id': k.id,
                                          'codigo': getattr(k, 'codigo', None),
                                          'referencia': k.numero_pedido_siesa})
    return out


def _duenos_con_trabajo():
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.models.picking import EstadoPicking, TareaPicking
    from app.models.tarea_reposicion import TareaReposicion
    ids = set()
    ids |= {r[0] for r in db.session.query(SesionConteo.operario_id).filter(
        SesionConteo.operario_id.isnot(None),
        SesionConteo.estado.in_([EstadoConteo.PENDIENTE, EstadoConteo.EN_PROCESO])).distinct()}
    ids |= {r[0] for r in db.session.query(TareaPicking.operario_id).filter(
        TareaPicking.operario_id.isnot(None),
        TareaPicking.estado == EstadoPicking.PENDIENTE).distinct()}
    ids |= {r[0] for r in db.session.query(TareaReposicion.abastecedor_id).filter(
        TareaReposicion.abastecedor_id.isnot(None),
        TareaReposicion.estado == 'EN_PROCESO').distinct()}
    return ids


def barrer(ahora=None) -> dict:
    """Suelta lo asignado a quien no está disponible. Hace commit.

    - Ausente o inactivo: todo lo que se puede soltar, empezado o no.
    - Sin señal: solo lo que no empezó. Lo empezado lo cuidan los barridos de
      inactividad de conteo y reposición (2 h desde el último escaneo), que
      miden la tarea y no a la persona.
    """
    from app.models.usuario import Usuario
    ids = _duenos_con_trabajo()
    if not ids:
        return {'personas': 0, 'conteos': 0, 'picking': 0, 'reposicion': 0}
    usuarios = Usuario.query.filter(Usuario.id.in_(ids)).all()
    pres = presencia.estados(usuarios, ahora=ahora)
    tot = {'personas': 0, 'conteos': 0, 'picking': 0, 'reposicion': 0}
    for u in usuarios:
        p = pres[u.id]
        if p['disponible']:
            continue
        motivo = {presencia.AUSENTE: MOTIVO_AUSENCIA, presencia.INACTIVO: MOTIVO_INACTIVO}.get(
            p['codigo'], MOTIVO_SIN_SENAL)
        r = devolver_trabajo_de(u.id, motivo=motivo,
                                incluir_en_curso=p['codigo'] != presencia.SIN_SENAL)
        if r['conteos'] or r['picking'] or r['reposicion']:
            tot['personas'] += 1
            for k in ('conteos', 'picking', 'reposicion'):
                tot[k] += r[k]
    db.session.commit()
    if tot['personas']:
        logger.info('[ASIGNACION] barrido: %s', tot)
    return tot


# ─────────────────────────────────────────────────────────────────────────────
# El equipo: lo que ve el líder
# ─────────────────────────────────────────────────────────────────────────────

def equipo(almacen_id=None) -> dict:
    """Quién está, qué tiene cada uno y qué quedó sin dueño que decidir.

    `personas`: los del almacén (o todos) que hacen trabajo de bodega, con su
    presencia, su ausencia vigente y su carga. `requieren_decision`: picking o
    empaque EN_PROCESO de alguien que no está — no se sueltan solos porque hay
    mercancía a medio mover.
    """
    from app.models.conteo import EstadoConteo, SesionConteo
    from app.models.packing import TareaPacking
    from app.models.picking import EstadoPicking, TareaPicking
    from app.models.tarea_reposicion import TareaReposicion
    from app.routes._auth_helpers import Roles
    oficios = (Roles.OPERARIO, Roles.PICKER_TRASLADO, Roles.PACKER_TRASLADO, Roles.EMPACADOR,
               Roles.SUPERVISOR, Roles.JEFE_ALMACEN, Roles.RECEPCIONISTA)
    usuarios = [u for u in _usuarios_activos() if u.rol in oficios
                and (not almacen_id or _almacen_de(u) == almacen_id)]
    pres = presencia.estados(usuarios)
    personas, decidir = [], []
    for u in usuarios:
        p = pres[u.id]
        carga = {
            'conteos_en_cola': SesionConteo.query.filter_by(
                operario_id=u.id, estado=EstadoConteo.PENDIENTE).count(),
            'conteos_en_curso': SesionConteo.query.filter_by(
                operario_id=u.id, estado=EstadoConteo.EN_PROCESO).count(),
            'picking_asignado': TareaPicking.query.filter_by(
                operario_id=u.id, estado=EstadoPicking.PENDIENTE).count(),
            'picking_en_curso': TareaPicking.query.filter_by(
                operario_id=u.id, estado=EstadoPicking.EN_PROCESO).count(),
            'reposicion_en_curso': TareaReposicion.query.filter_by(
                abastecedor_id=u.id, estado='EN_PROCESO').count(),
            'empaque_en_curso': TareaPacking.query.filter_by(
                empacador_id=u.id, estado='EN_PROCESO').count(),
        }
        hace = [t for t in (CONTEO, CONTEO_DEFINITIVO, PICKING, REPOSICION)
                if motivo_no_elegible(u, t) is None]
        personas.append({'id': u.id, 'nombre': u.nombre, 'rol': u.rol,
                         'presencia': p['codigo'], 'presencia_texto': p['texto'],
                         'disponible': p['disponible'], 'visto_at': p['visto_at'],
                         'ausencia': p['ausencia'], 'hace': hace, 'carga': carga})
        if not p['disponible'] and (carga['picking_en_curso'] or carga['empaque_en_curso']):
            decidir.append({'id': u.id, 'nombre': u.nombre, 'presencia_texto': p['texto'],
                            'picking_en_curso': carga['picking_en_curso'],
                            'empaque_en_curso': carga['empaque_en_curso']})
    orden = {presencia.EN_TURNO: 0, presencia.SIN_SENAL: 1, presencia.AUSENTE: 2}
    personas.sort(key=lambda x: (orden.get(x['presencia'], 3), x['nombre']))
    return {
        'almacen_id': almacen_id,
        'en_turno': sum(1 for x in personas if x['disponible']),
        'personas': personas,
        'requieren_decision': decidir,
        'ventana_senal_horas': presencia.VENTANA_SENAL_HORAS,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Scheduler
# ─────────────────────────────────────────────────────────────────────────────

def _correr_barrido(app):
    from app.utils.lock import LOCK_ASIGNACION_BARRIDO, advisory_lock
    with app.app_context():
        try:
            with advisory_lock(LOCK_ASIGNACION_BARRIDO, 'asignacion_barrido') as tomado:
                if not tomado:
                    return
                barrer()
        except Exception as e:
            db.session.rollback()
            logger.error('[ASIGNACION] barrido falló: %s', e, exc_info=True)


def init_scheduler(app):
    """Cada 15 min: suelta lo asignado a quien no está. Esencial (no pesado):
    si no corre, lo asignado a un ausente espera en silencio, que es el
    defecto que esto cierra."""
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from app.services.cron_latido import con_latido
    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(func=con_latido('asignacion_barrido', _correr_barrido), args=[app],
                      trigger=IntervalTrigger(minutes=15), id='asignacion_barrido',
                      name='Soltar lo asignado a quien no está', replace_existing=True)
    scheduler.start()
    return scheduler
