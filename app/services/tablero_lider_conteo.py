"""
Tablero del líder de bodega — «qué abro el lunes a las 7 a. m.» (2026-09-23).

Una sola pantalla con lo que SOLO el líder puede destrabar en el conteo
cíclico, lo urgente primero y cada fila con su acción. No calcula nada que no
exista ya: junta las políticas que viven en otros sitios y las pone en el
orden en que un jefe de bodega las atiende. **Cero Siesa**: todo sale de la
base del WMS (incluida la foto de Siesa que cada conteo guardó al contar).

| Bloque | De dónde sale (una política, una función) |
|---|---|
| Conteos bloqueados | `ConteoService.listar_bloqueados` |
| Mercancía sin código | `ConteoService.listar_novedades` |
| Ajustes en DESCUADRE | `ConteoService.motivo_bloqueo_ajuste` + `metricas.conteo.resumir_motivo_bloqueo` |
| Auditorías por faltante | `auditorias_por_faltante_vivas` (acá; la usa también el KPI del dashboard) |
| Ajustes rechazados por Siesa | jobs `AJUSTE_CONTEO` en FALLIDO (el mismo filtro de `/stats`) |
| Fuera del plan sin fecha | `ConteoService.hallazgos_que_sacan_del_plan` (lo que excluye el generador) |
| Hoy | `metricas.conteo.cadenas_cerradas_el_dia` + `conteo_politica.tope_de_generacion` |
| Rezago | `conteo_politica.tope_de_generacion` + `ABCService.plan_cancelar_rezago` |

**Nada de números sin su base**: cada contador viaja con su total o con el
denominador que le da sentido (cerrados hoy *de* un cupo, fallidos de este
almacén *de* los del sistema, SKUs sin fecha *y* los que salen solos).

**Qué NO hay acá, a propósito:** exactitud ni ranking por persona (decisión
del usuario, ver `metricas.conteo._por_operario`): «hoy» por persona es solo
volumen y va ordenado por nombre.
"""
from datetime import datetime

from app.extensions import db
from app.models.conteo import EstadoConteo, SesionConteo

FUENTE = 'solo base del WMS — cero llamadas a Siesa'

#: El orden de la cola de decisiones — lo fijó el usuario. Cada bloque se
#: pinta en este orden y el resumen lo respeta.
ORDEN_DE_URGENCIA = ('bloqueados', 'novedades', 'ajustes', 'auditorias', 'rechazados_siesa')

# ─────────────────────────────────────────────────────────────────────────────
# Auditorías por faltante de picking — la fuente única
# ─────────────────────────────────────────────────────────────────────────────

#: Estados de la RAÍZ de una auditoría por faltante que todavía necesita a una
#: persona: la cadena en curso (`CADENA_EN_CURSO`, que incluye BLOQUEADO) o
#: contada con diferencia esperando decisión (DESCUADRE). AJUSTANDO no: el
#: ajuste ya salió y lo lleva la cola de Siesa; si Siesa lo rechaza, aparece en
#: «Ajustes rechazados por Siesa».
ESTADOS_AUDITORIA_VIVA = tuple(
    e for e in EstadoConteo.CADENA_EN_CURSO if e != EstadoConteo.AJUSTANDO
) + (EstadoConteo.DESCUADRE,)


def auditorias_por_faltante_vivas(almacen_id: int) -> list:
    """Raíces `EXCEPCION_PICKING` del almacén que todavía esperan a alguien.

    **La única definición de «auditoría urgente»**: la usan este tablero y el
    KPI «Auditorías urgentes» del dashboard (`/api/dashboard/resumen-completo`).
    Antes el KPI contaba FILAS en PENDIENTE/EN_PROCESO/SEGUNDO_CONTEO/DESCUADRE:
    el CC2 hereda el tipo de su raíz, así que una auditoría en segundo conteo
    contaba dos, y el CC2 que resuelve queda en DESCUADRE para siempre —el KPI
    solo podía crecer—. Además le faltaban TERCER_CONTEO y BLOQUEADO. Ahora se
    cuenta la cadena (su raíz), una vez.
    """
    if not almacen_id:
        return []
    return (SesionConteo.query
            .filter(SesionConteo.almacen_id == almacen_id,
                    SesionConteo.tipo == 'EXCEPCION_PICKING',
                    SesionConteo.es_segundo_conteo.is_(False),
                    SesionConteo.estado.in_(ESTADOS_AUDITORIA_VIVA))
            .order_by(SesionConteo.fecha_creacion.asc(), SesionConteo.id.asc())
            .all())


def contar_auditorias_urgentes(almacen_id: int) -> int:
    """El número del KPI del dashboard. Es `len` de la lista del tablero: los
    dos no pueden decir cosas distintas."""
    return len(auditorias_por_faltante_vivas(almacen_id))


# ─────────────────────────────────────────────────────────────────────────────
# Textos operativos
# ─────────────────────────────────────────────────────────────────────────────

MOTIVO_BLOQUEO_TEXTO = {
    'NO_ENCONTRADO': 'No lo encontró',
    'MOVIMIENTO_CONTINUO': 'Se vendía mientras se contaba (reabrilo en un momento quieto)',
    'OTRO': 'Otro problema',
    'SIN_MOTIVO_REGISTRADO': 'Sin motivo registrado',
    # Los que mandaba la pantalla vieja de «Reportar problema».
    'UBICACION_VACIA': 'Ubicación vacía',
    'FALTANTE': 'Faltante',
    'MERCANCIA_AVERIADA': 'Mercancía averiada',
    'PRODUCTO_INCORRECTO': 'Producto incorrecto',
}

#: Qué hacer con un ajuste que no se puede aprobar, por el motivo resumido de
#: `resumir_motivo_bloqueo`. Todos los casos de `motivo_bloqueo_ajuste` se
#: juzgan contra el instante del conteo, así que un DESCUADRE bloqueado no se
#: desbloquea solo: se recuenta (el conteo manual sí abre cadena sobre un
#: DESCUADRE, y el nuevo deja «viejo» a éste — caso 6) o se cancela.
ACCION_POR_MOTIVO_AJUSTE = {
    'SIN_FOTO_CIERRE': ('RECONTAR', 'Recontar con Siesa respondiendo'),
    'SIN_FOTO_APERTURA': ('RECONTAR', 'Recontar: no se sabe si hubo ventas mientras se contaba'),
    'MOVIMIENTO_DURANTE_CONTEO': ('RECONTAR', 'Recontar en un momento sin ventas de ese producto'),
    'SALIDAS_NO_POS': ('RECONTAR', 'Que confirmen en Siesa las salidas pendientes; después recontar'),
    'TRASLADO_ENTRANTE': ('RECONTAR', 'Recibir y guardar el traslado; después recontar'),
    'MERCANCIA_EN_PROCESO': ('RECONTAR', 'Cerrar el documento que está en proceso; después recontar'),
    'CONTEO_VIEJO': ('CANCELAR', 'Cancelar este conteo: hay otro más reciente del mismo producto'),
    'OTRO': ('REVISAR', 'Leer el motivo y decidir: recontar o cancelar'),
}

ESTADO_TEXTO = {
    EstadoConteo.PENDIENTE: 'Pendiente',
    EstadoConteo.EN_PROCESO: 'Contándose',
    EstadoConteo.SEGUNDO_CONTEO: 'Esperando el 2º conteo',
    EstadoConteo.TERCER_CONTEO: 'Esperando el conteo definitivo',
    EstadoConteo.BLOQUEADO: 'Bloqueado',
    EstadoConteo.DESCUADRE: 'Contado con diferencia',
}


# ─────────────────────────────────────────────────────────────────────────────
# Permisos — qué botones puede usar quien mira
# ─────────────────────────────────────────────────────────────────────────────

def _quien(rol):
    """Un usuario de ese rol, para preguntarle a la política de aprobación
    (que solo mira `rol` y `activo`) sin buscar a nadie en la base."""
    from types import SimpleNamespace
    return SimpleNamespace(rol=rol, activo=True)


def permisos_de(rol: str) -> dict:
    """Qué acciones del tablero le responden a este rol, con las MISMAS tuplas
    de `Roles` que exige cada endpoint. La pantalla no pinta un botón que va a
    devolver 403; `tests/test_tablero_lider_conteo.py` cruza cada entrada
    contra la respuesta real de su endpoint, rol por rol."""
    from app.routes._auth_helpers import Roles
    from app.services.conteo_service import ConteoService
    return {
        'reabrir_cancelar_bloqueado': rol in Roles.SUPERVISION,
        'resolver_novedad': rol in Roles.SUPERVISION,
        'aprobar_ajuste': ConteoService.motivo_no_aprueba_ningun_ajuste(_quien(rol)) is None,
        'recontar': rol in Roles.LEAD,
        'cancelar_conteo': rol in Roles.SUPERVISION,
        'reintentar_descartar_fallos': rol in Roles.LEAD,
        'cancelar_rezago': rol == Roles.ADMIN,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Bloques
# ─────────────────────────────────────────────────────────────────────────────

def _orden_mas_viejo_primero(fecha_iso):
    # Sin fecha va primero: no saber cuándo pasó no lo hace reciente (Regla 0).
    return (fecha_iso is not None, fecha_iso or '')


def _bloqueados(almacen_id) -> dict:
    from app.services.conteo_service import ConteoService
    filas = ConteoService.listar_bloqueados(almacen_id=almacen_id)
    por_motivo = {}
    for f in filas:
        f['motivo_texto'] = MOTIVO_BLOQUEO_TEXTO.get(f['motivo_bloqueo'], f['motivo_bloqueo'])
        f['es_auditoria'] = f.get('tipo') == 'EXCEPCION_PICKING'
        por_motivo[f['motivo_bloqueo']] = por_motivo.get(f['motivo_bloqueo'], 0) + 1
    filas.sort(key=lambda f: _orden_mas_viejo_primero(f['bloqueado_en']))
    return {'total': len(filas), 'por_motivo': dict(sorted(por_motivo.items())),
            'filas': filas}


def _novedades(almacen_id) -> dict:
    from app.services.conteo_service import ConteoService
    filas = ConteoService.listar_novedades(almacen_id=almacen_id, solo_abiertas=True)
    filas.sort(key=lambda f: _orden_mas_viejo_primero(f['fecha_creacion']))
    return {'total': len(filas), 'filas': filas}


def _fila_conteo(s: SesionConteo) -> dict:
    return {
        'id': s.id,
        'codigo': s.codigo,
        'tipo': s.tipo,
        'es_auditoria': s.tipo == 'EXCEPCION_PICKING',
        'producto_codigo': s.producto.codigo if s.producto else s.producto_codigo_siesa,
        'producto_nombre': s.producto.nombre if s.producto else None,
        'estado': s.estado,
        'estado_texto': ESTADO_TEXTO.get(s.estado, s.estado),
    }


def _ajustes(almacen_id, rol: str = None) -> dict:
    """Raíces en DESCUADRE: las que se pueden aprobar, con su plata, y las que
    no, con el porqué resumido y la acción. Solo raíces: el ajuste sale de la
    raíz (`_exigir_raiz_para_ajustar`); el CC2/CC3 que resolvió queda en
    DESCUADRE para siempre y no es una decisión pendiente."""
    from app.services.conteo_service import ConteoService
    from app.services.metricas.conteo import resumir_motivo_bloqueo
    from app.utils.fecha import dia_operativo_de
    raices = (SesionConteo.query
              .filter(SesionConteo.almacen_id == almacen_id,
                      SesionConteo.es_segundo_conteo.is_(False),
                      SesionConteo.estado == EstadoConteo.DESCUADRE)
              .order_by(SesionConteo.id.asc())
              .all())
    aprobables, bloqueados = [], []
    valor_total, sin_costo, por_motivo = 0.0, 0, {}
    for s in raices:
        fila = _fila_conteo(s)
        motivo = ConteoService.motivo_bloqueo_ajuste(s)
        if motivo:
            clave = resumir_motivo_bloqueo(motivo)
            tipo_accion, texto = ACCION_POR_MOTIVO_AJUSTE.get(clave, ACCION_POR_MOTIVO_AJUSTE['OTRO'])
            fila.update(motivo_clave=clave, motivo=motivo,
                        accion={'tipo': tipo_accion, 'texto': texto})
            por_motivo[clave] = por_motivo.get(clave, 0) + 1
            bloqueados.append(fila)
            continue
        dif = s.diferencia
        costo = float(s.costo_prom_uni_siesa) if s.costo_prom_uni_siesa is not None else None
        valor = None
        if dif is not None and costo is not None and costo > 0:
            valor = round(abs(dif) * costo, 2)
            valor_total += valor
        else:
            # Ni cero ni costo de hoy: el ajuste se valoriza con el costo de
            # SU foto, y si no lo trajo, no se sabe cuánto vale.
            sin_costo += 1
        fila.update(
            diferencia=dif,
            direccion=(None if dif is None else ('ENTRADA' if dif > 0 else 'SALIDA')),
            unidades=abs(dif) if dif is not None else None,
            costo_unitario=costo,
            valor=valor,
            dia_conteo=(dia_operativo_de(s.foto_siesa_at).isoformat()
                        if s.foto_siesa_at else None),
            # Por qué QUIEN MIRA no aprueba ESTE ajuste (el tope del jefe es
            # por monto): la pantalla muestra el motivo en vez del botón.
            no_puede_aprobar=(ConteoService.motivo_no_puede_aprobar(_quien(rol), s)
                              if rol else None),
        )
        aprobables.append(fila)
    # Más plata primero; sin costo ADELANTE — que no se sepa cuánto vale no lo
    # vuelve chico (Regla 0).
    aprobables.sort(key=lambda f: (f['valor'] is not None, -(f['valor'] or 0), f['id']))
    return {
        'total_descuadres': len(raices),
        'aprobables': {'total': len(aprobables), 'valor_total': round(valor_total, 2),
                       'valorizados': len(aprobables) - sin_costo, 'sin_costo': sin_costo,
                       'etiqueta_valor': 'diferencia × costo promedio de la foto del conteo',
                       'filas': aprobables},
        'bloqueados': {'total': len(bloqueados), 'por_motivo': dict(sorted(por_motivo.items())),
                       'filas': bloqueados},
    }


def _nodo_vivo(raiz: SesionConteo) -> SesionConteo:
    """El eslabón de la cadena que tiene la pelota: el más profundo que existe."""
    nodo = raiz
    while nodo.hijo_conteo is not None:
        nodo = nodo.hijo_conteo
    return nodo


def _accion_auditoria(raiz: SesionConteo) -> dict:
    nodos = [raiz]
    while nodos[-1].hijo_conteo is not None:
        nodos.append(nodos[-1].hijo_conteo)
    if any(n.estado == EstadoConteo.BLOQUEADO for n in nodos):
        return {'tipo': 'VER_BLOQUEADOS', 'texto': 'Bloqueada — decidí en «Conteos bloqueados»',
                'decide_el_lider': False}
    if raiz.estado == EstadoConteo.DESCUADRE:
        return {'tipo': 'VER_AJUSTES', 'texto': 'Contada con diferencia — decidí en «Ajustes»',
                'decide_el_lider': False}
    if raiz.estado == EstadoConteo.TERCER_CONTEO:
        return {'tipo': 'CONTAR_DEFINITIVO',
                'texto': 'Los dos conteos no coinciden: falta el definitivo, lo hace un supervisor',
                'decide_el_lider': True}
    vivo = _nodo_vivo(raiz)
    if raiz.estado == EstadoConteo.SEGUNDO_CONTEO:
        quien = vivo.operario.nombre if vivo.operario else None
        return {'tipo': 'EN_CURSO',
                'texto': (f'Esperando el 2º conteo — lo tiene {quien}' if quien
                          else 'Esperando el 2º conteo — lo toma otro operario'),
                'decide_el_lider': False}
    if vivo.operario is not None:
        verbo = 'La está contando' if vivo.estado == EstadoConteo.EN_PROCESO else 'Asignada a'
        return {'tipo': 'EN_CURSO', 'texto': f'{verbo} {vivo.operario.nombre}',
                'decide_el_lider': False}
    return {'tipo': 'EN_COLA',
            'texto': 'Sale primera en la cola: la cuenta el próximo operario libre',
            'decide_el_lider': False}


def _auditorias(almacen_id, hoy) -> dict:
    from app.models.picking import TareaPicking
    from app.utils.fecha import dia_operativo_de
    raices = auditorias_por_faltante_vivas(almacen_id)
    ids_tp = {r.tarea_picking_id for r in raices if r.tarea_picking_id}
    pedidos = {}
    if ids_tp:
        pedidos = {tp.id: tp.referencia_documento for tp in
                   TareaPicking.query.filter(TareaPicking.id.in_(sorted(ids_tp))).all()}
    filas = []
    for r in raices:
        fila = _fila_conteo(r)
        dia = dia_operativo_de(r.fecha_creacion) if r.fecha_creacion else None
        fila.update(
            pedido=pedidos.get(r.tarea_picking_id),
            creada_dia=dia.isoformat() if dia else None,
            dias_abierta=(hoy - dia).days if dia else None,
            accion=_accion_auditoria(r),
        )
        filas.append(fila)
    return {'total': len(filas),
            'esperan_al_lider': sum(1 for f in filas if f['accion']['decide_el_lider']),
            'filas': filas}


def _rechazados_siesa(almacen_id) -> dict:
    """Ajustes que Siesa rechazó después de los reintentos. Los botones que
    existen (`reintentar-fallos`, `descartar-fallos`) actúan sobre TODOS los
    almacenes: por eso viaja el total del sistema junto al de este almacén."""
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    fallidos = (SiesaJob.query
                .filter(SiesaJob.tipo == 'AJUSTE_CONTEO',
                        SiesaJob.estado == EstadoSiesaJob.FALLIDO)
                .order_by(SiesaJob.id.asc())
                .all())
    filas, sin_sesion = [], 0
    for job in fallidos:
        payload = job.get_payload() or {}
        sid = payload.get('sesion_id') or (job.referencia_id
                                           if job.referencia_tipo == 'SesionConteo' else None)
        s = db.session.get(SesionConteo, int(sid)) if sid else None
        if s is None:
            # Sin sesión no se sabe de qué almacén es: se cuenta en el total del
            # sistema (los botones lo tocan) y se declara aparte.
            sin_sesion += 1
            continue
        if s.almacen_id != almacen_id:
            continue
        fila = _fila_conteo(s)
        fila.update(job_id=job.id, intentos=job.intentos,
                    unidades=payload.get('cantidad'), motivo_codigo=payload.get('motivo_codigo'),
                    error=(job.error_ultimo or '')[:300] or None,
                    fecha=job.fecha_creacion.isoformat() if job.fecha_creacion else None)
        filas.append(fila)
    return {'total': len(filas), 'total_sistema': len(fallidos),
            'sin_sesion_identificable': sin_sesion, 'filas': filas}


def _fuera_del_plan(almacen_id) -> dict:
    """SKUs que el generador deja fuera del plan por mercancía en proceso
    **sin fecha** (Regla 0: no se sabe si volvió al estante). No tienen salida
    automática: siguen fuera hasta que alguien cierre el documento. Se listan
    por documento, que es lo que hay que ir a cerrar. Los que tienen fecha se
    cuentan al lado: salen solos cuando su documento entra a Siesa."""
    from app.models.producto import Producto
    from app.services.conteo_service import ConteoService
    hallazgos = ConteoService.hallazgos_que_sacan_del_plan(almacen_id)
    sin_fecha = [h for h in hallazgos if h.get('sin_fecha')]
    skus_sin_fecha = {h['producto_id'] for h in sin_fecha}
    skus_con_fecha = {h['producto_id'] for h in hallazgos if not h.get('sin_fecha')} - skus_sin_fecha
    codigos = {}
    if skus_sin_fecha:
        codigos = {p.id: p.codigo for p in
                   Producto.query.filter(Producto.id.in_(sorted(skus_sin_fecha))).all()}
    por_doc = {}
    for h in sin_fecha:
        clave = (h.get('clase'), h.get('documento'))
        d = por_doc.setdefault(clave, {'documento': h.get('documento'), 'clase': h.get('clase'),
                                       'detalle': h.get('detalle'), 'accion': h.get('accion'),
                                       'skus': set()})
        d['skus'].add(h['producto_id'])
    filas = []
    for d in por_doc.values():
        skus = sorted(codigos.get(pid) or f'#{pid}' for pid in d['skus'])
        filas.append({**d, 'skus': skus, 'n_skus': len(skus)})
    filas.sort(key=lambda f: (-f['n_skus'], str(f['documento'])))
    return {'skus_sin_fecha': len(skus_sin_fecha), 'documentos': len(filas),
            'skus_con_fecha_salen_solos': len(skus_con_fecha), 'filas': filas}


def _hoy(almacen_id, hoy, tope) -> dict:
    from app.services.metricas.conteo import (cadenas_cerradas_el_dia,
                                              participacion_por_persona)
    cerradas = cadenas_cerradas_el_dia(hoy, almacen_id)
    return {
        'dia': hoy.isoformat(),
        'cerrados': len(cerradas),
        'unidad': 'cadena de conteo (1º + 2º + definitivo) cerrada hoy con resultado',
        'cupo_diario': tope['cupo_diario'],
        'pendientes_vivas': tope['pendientes_vivas'],
        'dias_de_cupo_pendientes': tope['dias_de_cupo_pendientes'],
        'mensaje_generador': tope['mensaje'],
        'por_persona': participacion_por_persona(cerradas),
    }


def _rezago(almacen_id, tope) -> dict:
    """Aviso de rezago: solo cuando el generador está DETENIDO por rezago
    (`tope_de_generacion` → REZAGO) y hay conteos del plan que nadie tomó y se
    pueden cancelar. Un rezago que no frena al generador es la cola normal."""
    from app.services import conteo_politica as politica
    from app.services.abc_service import ABCService
    salida = {'hay_aviso': False, 'generador_detenido': tope['motivo'] == politica.REZAGO,
              'pendientes_vivas': tope['pendientes_vivas'], 'cupo_diario': tope['cupo_diario'],
              'dias_de_cupo_pendientes': tope['dias_de_cupo_pendientes'],
              'mensaje_generador': tope['mensaje']}
    if not salida['generador_detenido']:
        return salida
    plan = ABCService.plan_cancelar_rezago(almacen_id)
    salida.update(a_cancelar=plan['a_cancelar'], por_antiguedad_dias=plan['por_antiguedad_dias'],
                  no_se_tocan=plan['no_se_tocan'], hay_aviso=plan['a_cancelar'] > 0)
    return salida


# ─────────────────────────────────────────────────────────────────────────────
# Entrada
# ─────────────────────────────────────────────────────────────────────────────

def tablero(almacen_id: int, *, rol: str = None, ahora: datetime = None) -> dict:
    """El tablero completo de un almacén. `ahora` (UTC naive) se inyecta en
    los tests; el día es siempre el operativo de Bogotá (Regla 5)."""
    from app.models.almacen import Almacen
    from app.services import conteo_politica as politica
    from app.utils.fecha import dia_operativo_de
    alm = db.session.get(Almacen, almacen_id)
    if alm is None:
        raise LookupError(f'Almacén {almacen_id} no encontrado')
    ahora = ahora or datetime.utcnow()
    hoy = dia_operativo_de(ahora)

    decisiones = {
        'bloqueados': _bloqueados(almacen_id),
        'novedades': _novedades(almacen_id),
        'ajustes': _ajustes(almacen_id, rol),
        'auditorias': _auditorias(almacen_id, hoy),
        'rechazados_siesa': _rechazados_siesa(almacen_id),
    }
    tope = politica.tope_de_generacion(almacen_id)
    ajustes = decisiones['ajustes']
    # Lo que espera al líder, sin contar dos veces: una auditoría bloqueada o
    # en DESCUADRE ya está en su bloque, y una en cola la resuelve un operario.
    por_bloque = {
        'bloqueados': decisiones['bloqueados']['total'],
        'novedades': decisiones['novedades']['total'],
        'ajustes': ajustes['aprobables']['total'] + ajustes['bloqueados']['total'],
        'auditorias': decisiones['auditorias']['esperan_al_lider'],
        'rechazados_siesa': decisiones['rechazados_siesa']['total'],
    }
    return {
        'almacen_id': alm.id,
        'almacen': alm.nombre,
        'bodega_siesa': alm.bodega_siesa_id,
        'al_dia_operativo': hoy.isoformat(),
        'fuente': FUENTE,
        'orden': list(ORDEN_DE_URGENCIA),
        'resumen': {'decisiones_pendientes': sum(por_bloque.values()),
                    'por_bloque': por_bloque},
        'decisiones': decisiones,
        'fuera_del_plan': _fuera_del_plan(almacen_id),
        'hoy': _hoy(almacen_id, hoy, tope),
        'rezago': _rezago(almacen_id, tope),
        'permisos': permisos_de(rol),
    }
