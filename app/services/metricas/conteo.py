"""
Estadísticas del conteo cíclico — carga, cobertura, flujo, ajustes y exactitud.

Pura en el sentido del resto de `app/services/metricas/`: recibe fechas y
filtros ya resueltos (no lee `request`, no sabe de Flask) y **no habla con
Siesa**: todo sale de lo que el conteo ya guardó, incluida la foto de Siesa
del instante de contar. Un reporte que consultara Siesa mezclaría el instante
del conteo con el de la consulta — el mismo defecto que la foto existe para
cerrar — y además tardaría 8 s por SKU.

## Qué se midió en producción antes de escribir esto (2026-09-23)

El conteo casi no opera: 8.761 cadenas, 8.690 creadas de golpe en abril y
~70 desde entonces; 4.830 PENDIENTES colgadas desde abril; 25 MATCH y 5
AJUSTADO en total. El plan ABC de NB1 (A 15 d, B 90 d, C 180 d sobre ~26.000
productos) exige ~316 conteos por día.

Por eso **el primer bloque es carga y cobertura**: cuánto exige el plan, cuánto
se cuenta y a qué ritmo. Exactitud y tasas van a tener n chico durante meses,
y el reporte lo DICE (numerador, denominador y `excluidos` en cada métrica,
porcentaje `None` debajo de `MIN_N_EXACTITUD`) en vez de pintar un 100 % sobre
tres conteos.

## La unidad es la cadena, no la fila

Un conteo es CC1 (la raíz, `es_segundo_conteo=False`) + su CC2 (`hijo_conteo`)
+ su CC3 (el hijo del hijo). Contar filas cuenta el mismo hueco dos o tres
veces, y justo los que tuvieron diferencia — el error se infla solo. **Una
fila hija nunca es una cadena.**

## Qué NO hay acá, a propósito

Exactitud ni ranking por operario (decisión del usuario, ver
`_por_operario`).
"""
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import joinedload, selectinload

from app.extensions import db
from app.models.conteo import EstadoConteo, SesionConteo
from app.utils.fecha import dia_operativo_de

# ─────────────────────────────────────────────────────────────────────────────
# Veredicto de una cadena
# ─────────────────────────────────────────────────────────────────────────────

OK_CC1 = 'OK_CC1'                      # la raíz cuadró sola
OK_CC2 = 'OK_CC2'                      # CC1 no cuadró, CC2 sí
OK_CC3 = 'OK_CC3'                      # CC1 ≠ CC2, el definitivo cuadró
ERROR_CONFIRMADO = 'ERROR_CONFIRMADO'  # CC2 confirmó la diferencia de CC1
ERROR_CC3 = 'ERROR_CC3'                # el definitivo encontró diferencia
ERROR_AUDITORIA = 'ERROR_AUDITORIA'    # auditoría de picking: un conteo de supervisor
SIN_VEREDICTO = 'SIN_VEREDICTO'

VEREDICTOS_OK = frozenset({OK_CC1, OK_CC2, OK_CC3})
VEREDICTOS_ERROR = frozenset({ERROR_CONFIRMADO, ERROR_CC3, ERROR_AUDITORIA})
VEREDICTOS = tuple(sorted(VEREDICTOS_OK)) + tuple(sorted(VEREDICTOS_ERROR)) + (SIN_VEREDICTO,)

#: Por debajo de este n no se publica porcentaje de exactitud. Treinta no es un
#: número mágico de este negocio: es el tamaño a partir del cual una proporción
#: deja de moverse 3+ puntos por un solo conteo (1/30 ≈ 3,3 %). Con los números
#: de producción (25 MATCH en cinco meses) un porcentaje con n=4 sería un 100 %
#: o un 75 % según cayera UN conteo — ruido pintado como indicador. Se muestra
#: el n y el numerador; el porcentaje queda `None` y se dice por qué.
MIN_N_EXACTITUD = 30

#: Ventana del ritmo real (throughput): cuatro semanas enteras, para que el
#: promedio no dependa de qué día de la semana se consulta.
VENTANA_RITMO_DIAS = 28

#: Rango por defecto cuando la consulta no trae fechas: las mismas 4 semanas.
DIAS_POR_DEFECTO = 28

TOP_N = 20

#: Tramos de antigüedad del rezago, en días operativos desde la creación.
TRAMOS_REZAGO = (('0-2', 0, 2), ('3-7', 3, 7), ('8-30', 8, 30), ('>30', 31, None))

#: Los únicos motivos que son un AJUSTE a Siesa. `motivo_codigo` también trae
#: FALTANTE / UBICACION_VACIA / MERCANCIA_AVERIADA — lo que un picker reportó al
#: crear una auditoría por excepción— y eso es un diagnóstico, no un documento.
MOTIVOS_AJUSTE = ('AJ-ENT', 'AJ-SAL')

ETIQUETA_VALOR = 'estimado a costo de la foto del conteo'

_ESTADOS_RESUELTOS_A_MANO = (EstadoConteo.DESCUADRE, EstadoConteo.AJUSTANDO,
                             EstadoConteo.AJUSTADO)


def veredicto_cadena(raiz: SesionConteo) -> str:
    """Qué dijo la cadena que empieza en `raiz` sobre el inventario. **La única
    implementación** — la usan este reporte, el CSV de `/api/conteo/exportar` y
    cualquier otro que pregunte «¿este conteo encontró error?».

    Verificado regla por regla contra `ConteoService.registrar_conteo`:

    - **OK_CC1** — raíz MATCH sin hijo: CC1 cuadró con su foto.
    - **OK_CC2** — hijo MATCH (la raíz pasa a MATCH con él).
    - **OK_CC3** — nieto MATCH.
    - **ERROR_CONFIRMADO** — hijo DESCUADRE sin nieto: es la rama CC1 == CC2
      (cuando CC1 ≠ CC2 el mismo commit crea el CC3, así que un hijo en
      DESCUADRE sin nieto solo nace de la coincidencia). La raíz queda en
      DESCUADRE, AJUSTANDO o AJUSTADO según el bloqueo — el veredicto no
      depende de eso: el error se encontró aunque el ajuste espere.
    - **ERROR_CC3** — nieto DESCUADRE.
    - **ERROR_AUDITORIA** — raíz con `tarea_picking_id`, diferencia ≠ 0 y sin
      hijo, en DESCUADRE/AJUSTANDO/AJUSTADO. Es `ajustar_desde_auditoria_picking`:
      un supervisor cuenta y resuelve en un gesto, sin double-blind — el mismo
      rol que decide un CC3. **No estaba en la spec**: sin él, una auditoría que
      cuadra contaba como OK_CC1 y la que no cuadra desaparecía del
      denominador, y la exactitud de «otros tipos» salía inflada por
      construcción.
    - **SIN_VEREDICTO** — todo lo demás: cancelada, pendiente, en curso,
      omitida (CC2/CC3 cancelado por `omitir-segundo`), `BLOQUEADO` (estado
      legado que hoy nadie escribe). Una raíz CANCELADA no tiene veredicto
      aunque haya llegado a DESCUADRE: el supervisor decidió no creerle, y el
      reporte no le enmienda la plana. El porqué, en `motivo_sin_veredicto`.
    """
    if raiz.estado in (EstadoConteo.CANCELADO, EstadoConteo.BLOQUEADO):
        return SIN_VEREDICTO
    hijo = raiz.hijo_conteo
    if hijo is None:
        if raiz.estado == EstadoConteo.MATCH:
            return OK_CC1
        if (raiz.tarea_picking_id is not None
                and raiz.estado in _ESTADOS_RESUELTOS_A_MANO
                and raiz.diferencia not in (None, 0)):
            return ERROR_AUDITORIA
        return SIN_VEREDICTO
    nieto = hijo.hijo_conteo
    if nieto is None:
        if hijo.estado == EstadoConteo.MATCH:
            return OK_CC2
        if hijo.estado == EstadoConteo.DESCUADRE:
            return ERROR_CONFIRMADO
        return SIN_VEREDICTO
    if nieto.estado == EstadoConteo.MATCH:
        return OK_CC3
    if nieto.estado == EstadoConteo.DESCUADRE:
        return ERROR_CC3
    return SIN_VEREDICTO


def motivo_sin_veredicto(raiz: SesionConteo) -> str:
    """Por qué una cadena SIN_VEREDICTO no lo tiene. **Solo describe**: no
    decide el veredicto (eso es `veredicto_cadena`) y solo se llama sobre
    cadenas que ya lo tienen en SIN_VEREDICTO.

    `omitida` cubre también la raíz resuelta a mano con su CC3 todavía
    PENDIENTE: `omitir-segundo` dejaba el CC3 vivo y huérfano cuando la raíz
    estaba en TERCER_CONTEO (arreglado en b69bc78; el histórico anterior sigue
    así en la base). Leído como «pendiente» mandaría a buscar un conteo que ya
    nadie va a hacer.
    """
    if raiz.estado == EstadoConteo.CANCELADO:
        return 'cancelada'
    if raiz.estado == EstadoConteo.BLOQUEADO:
        return 'bloqueado_legado'
    hijo = raiz.hijo_conteo
    descendientes = [n for n in (hijo, hijo.hijo_conteo if hijo else None) if n is not None]
    if any(n.estado == EstadoConteo.CANCELADO for n in descendientes):
        return 'omitida'
    if hijo is not None and raiz.estado in _ESTADOS_RESUELTOS_A_MANO:
        return 'omitida'
    if hijo is None and raiz.estado in _ESTADOS_RESUELTOS_A_MANO:
        return 'descuadre_sin_segundo_conteo'
    if raiz.estado == EstadoConteo.PENDIENTE:
        return 'pendiente'
    return 'en_curso'


def sesion_resolutiva(raiz: SesionConteo, veredicto: str = None):
    """La fila cuyo conteo decidió el veredicto, o `None` si no hay veredicto."""
    veredicto = veredicto or veredicto_cadena(raiz)
    if veredicto in (OK_CC1, ERROR_AUDITORIA):
        return raiz
    if veredicto in (OK_CC2, ERROR_CONFIRMADO):
        return raiz.hijo_conteo
    if veredicto in (OK_CC3, ERROR_CC3):
        return raiz.hijo_conteo.hijo_conteo
    return None


def dia_de_atribucion(raiz: SesionConteo, veredicto: str = None):
    """Día operativo (Bogotá) al que pertenece la cadena, o `None` si no hay
    con qué saberlo — y entonces se declara en `excluidos`, no se adivina.

    Es el día en que se CONFIRMÓ el conteo que resolvió: su `foto_siesa_at`
    (se lee en el instante de confirmar); sin foto (Siesa no respondió y se
    comparó contra el WMS), su `fecha_cierre` solo si quedó en MATCH, que
    `reconciliar_cantidad` escribe en el mismo instante.

    **Nunca la `fecha_cierre` de una raíz AJUSTADO**: es la hora en que la DLQ
    recibió la respuesta del POST, que puede ser días después y cruzar un mes.
    Por eso solo se acepta `fecha_cierre` en MATCH.

    Sin veredicto (una raíz ajustada tras omitir el CC2) se usa la raíz misma,
    con la misma regla: su propia foto.
    """
    s = sesion_resolutiva(raiz, veredicto) or raiz
    if s.foto_siesa_at is not None:
        return dia_operativo_de(s.foto_siesa_at)
    if s.estado == EstadoConteo.MATCH and s.fecha_cierre is not None:
        return dia_operativo_de(s.fecha_cierre)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Carga de cadenas
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Cadena:
    raiz: SesionConteo
    veredicto: str
    dia: date = None
    motivo_sv: str = None
    resolutiva: SesionConteo = None
    nodos: list = field(default_factory=list)

    @property
    def hijo(self):
        return self.raiz.hijo_conteo

    @property
    def nieto(self):
        return self.hijo.hijo_conteo if self.hijo is not None else None

    @property
    def cerrada(self):
        return self.veredicto != SIN_VEREDICTO

    @property
    def dia_creacion(self):
        return dia_operativo_de(self.raiz.fecha_creacion) if self.raiz.fecha_creacion else None


def _cadena(raiz: SesionConteo) -> _Cadena:
    v = veredicto_cadena(raiz)
    nodos = [raiz]
    if raiz.hijo_conteo is not None:
        nodos.append(raiz.hijo_conteo)
        if raiz.hijo_conteo.hijo_conteo is not None:
            nodos.append(raiz.hijo_conteo.hijo_conteo)
    return _Cadena(
        raiz=raiz, veredicto=v, dia=dia_de_atribucion(raiz, v),
        motivo_sv=motivo_sin_veredicto(raiz) if v == SIN_VEREDICTO else None,
        resolutiva=sesion_resolutiva(raiz, v), nodos=nodos,
    )


def _cargar_cadenas(almacen_id=None) -> list:
    """Todas las raíces (del almacén, si se pide) con sus dos niveles de hijos
    precargados. Sin filtro de fecha a propósito: el rezago, la cobertura y los
    bloqueados son una foto de HOY, y una cadena creada en abril puede
    resolverse en septiembre. Con el volumen medido (~9.000 raíces) es una
    consulta y dos `selectin`, no N+1."""
    q = (SesionConteo.query
         .options(
             selectinload(SesionConteo.hijo_conteo).selectinload(SesionConteo.hijo_conteo),
             joinedload(SesionConteo.producto),
             joinedload(SesionConteo.almacen),
         )
         .filter(or_(SesionConteo.es_segundo_conteo.is_(False),
                     SesionConteo.es_segundo_conteo.is_(None))))
    if almacen_id:
        q = q.filter(SesionConteo.almacen_id == almacen_id)
    return [_cadena(r) for r in q.all()]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers de forma
# ─────────────────────────────────────────────────────────────────────────────

def _metrica(numerador, denominador, excluidos=None, *, min_n=None) -> dict:
    """La forma de toda métrica del reporte: nunca un porcentaje suelto."""
    pct = None
    motivo = None
    if not denominador:
        motivo = 'denominador cero'
    elif min_n is not None and denominador < min_n:
        motivo = f'n={denominador} < {min_n}: muestra chica, no se publica porcentaje'
    else:
        pct = round(100.0 * numerador / denominador, 1)
    salida = {'numerador': numerador, 'denominador': denominador, 'porcentaje': pct,
              'excluidos': dict(excluidos or {})}
    if motivo:
        salida['sin_porcentaje_por'] = motivo
    return salida


def _en(dia, desde, hasta):
    return dia is not None and desde <= dia <= hasta


def _float(v):
    return float(v) if v is not None else None


def _producto_fila(s: SesionConteo) -> dict:
    return {
        'producto_id': s.producto_id,
        'producto_codigo': s.producto.codigo if s.producto else s.producto_codigo_siesa,
        'producto_nombre': s.producto.nombre if s.producto else None,
        'almacen': s.almacen.nombre if s.almacen else None,
    }


def _descartes(s: SesionConteo):
    """Las entradas de `conteos_descartados` con su día operativo, o `None`
    si la entrada no trae una fecha legible."""
    for entrada in s.lista_conteos_descartados():
        crudo = entrada.get('descartado_at') if isinstance(entrada, dict) else None
        try:
            dia = dia_operativo_de(datetime.fromisoformat(crudo)) if crudo else None
        except (TypeError, ValueError):
            dia = None
        yield dia


# ─────────────────────────────────────────────────────────────────────────────
# a · Carga y cobertura
# ─────────────────────────────────────────────────────────────────────────────

def _carga_y_cobertura(cadenas, almacen_id, clase, hoy, ahora) -> dict:
    """Cuánto exige el plan ABC y cuánto se está contando. **Foto de hoy**: no
    depende de `desde`/`hasta` (el universo es el stock de hoy; no hay historia
    de `UbicacionProducto` para reconstruir el de otro día), y el filtro `tipo`
    no aplica — cualquier conteo de un hueco lo deja al día, venga del plan, de
    un conteo manual o de una auditoría.

    El universo, los huecos y «el último conteo» salen de las MISMAS funciones
    que usa el generador (`abc_service`). La cobertura es por hueco
    (producto × ubicación), que es lo que el generador programa y lo que
    `ultimo_conteo_por_hueco` fecha; la exigencia diaria es `ceil(productos /
    frecuencia)`, el lote que el generador crea cada día.

    El ritmo cuenta las cadenas CERRADAS (con veredicto) en los últimos 28 días
    operativos sobre huecos de ese universo. Una cerrada con error que todavía
    no se ajustó (DESCUADRE) no deja el hueco «al día» para el generador — si
    hay muchas, el ciclo real es más largo que la estimación.
    """
    from app.models.almacen import Almacen
    from app.models.producto_clasificacion_abc import ProductoClasificacionABC
    from app.services.abc_service import (FRECUENCIA_DIAS, huecos_con_stock,
                                          ultimo_conteo_por_hueco, umbral_al_dia,
                                          universo_conteo_ciclico)

    q_alm = db.session.query(ProductoClasificacionABC.almacen_id).distinct()
    if almacen_id:
        q_alm = q_alm.filter(ProductoClasificacionABC.almacen_id == almacen_id)
    almacen_ids = sorted(a for (a,) in q_alm.all())
    clases = [clase] if clase else sorted(FRECUENCIA_DIAS)

    ventana_desde = hoy - timedelta(days=VENTANA_RITMO_DIAS - 1)
    cerradas_por_hueco = Counter(
        (c.raiz.almacen_id, c.raiz.producto_id, c.raiz.ubicacion_id)
        for c in cadenas if c.cerrada and _en(c.dia, ventana_desde, hoy))
    sin_dia = sum(1 for c in cadenas if c.cerrada and c.dia is None)

    filas = []
    for alm_id in almacen_ids:
        alm = db.session.get(Almacen, alm_id)
        for cl in clases:
            frecuencia = FRECUENCIA_DIAS[cl]
            productos = universo_conteo_ciclico(alm_id, cl)
            ids = [p.id for p in productos]
            pares = {(h.producto_id, h.ubicacion_id) for h in huecos_con_stock(alm_id, ids)}
            ultimo = ultimo_conteo_por_hueco(ids, sorted({u for _, u in pares}))
            umbral = umbral_al_dia(cl, ahora)
            al_dia = sum(1 for par in pares if ultimo.get(par) and ultimo[par] >= umbral)
            nunca = sum(1 for par in pares if not ultimo.get(par))
            sin_contar = len(pares) - al_dia
            cerradas = sum(cerradas_por_hueco.get((alm_id, p, u), 0) for p, u in pares)
            por_dia = cerradas / VENTANA_RITMO_DIAS
            if por_dia > 0:
                dias_ciclo, motivo_ciclo = round(sin_contar / por_dia, 1), None
            else:
                # Ni infinito ni cero: con ritmo cero no hay estimación, hay
                # una ausencia que se nombra.
                dias_ciclo = None
                motivo_ciclo = (f'sin ritmo medible: ninguna cadena cerrada en los '
                                f'últimos {VENTANA_RITMO_DIAS} días sobre este universo')
            filas.append({
                'almacen_id': alm_id,
                'almacen': alm.nombre if alm else None,
                'bodega_siesa': alm.bodega_siesa_id if alm else None,
                'clase': cl,
                'frecuencia_dias': frecuencia,
                'universo_productos': len(productos),
                'universo_huecos': len(pares),
                'contados_en_frecuencia': _metrica(al_dia, len(pares)),
                'nunca_contados': nunca,
                'sin_contar_en_ventana': sin_contar,
                'exigencia_diaria': math.ceil(len(productos) / frecuencia) if productos else 0,
                'ritmo': {
                    'cadenas_cerradas': cerradas,
                    'dias_ventana': VENTANA_RITMO_DIAS,
                    'por_dia': round(por_dia, 2),
                },
                'dias_para_cerrar_ciclo': dias_ciclo,
                'sin_estimacion_por': motivo_ciclo,
            })
    return {
        'al_dia_operativo': hoy.isoformat(),
        'nota': ('Foto de hoy: no depende del rango de fechas, y el filtro de tipo '
                 'no aplica (cualquier conteo deja el hueco al día).'),
        'filas': filas,
        'excluidos': {'cerradas_sin_fecha_de_confirmacion': sin_dia} if sin_dia else {},
    }


def _rezago(cadenas, hoy) -> dict:
    """Cadenas abiertas por antigüedad desde que se crearon (días operativos).
    Foto de hoy. `pendiente` = nadie la contó todavía; `en_curso` = CC1 contado,
    esperando CC2/CC3 o la confirmación de un recuento."""
    tramos = {'pendiente': {t[0]: 0 for t in TRAMOS_REZAGO},
              'en_curso': {t[0]: 0 for t in TRAMOS_REZAGO}}
    sin_fecha = 0
    for c in cadenas:
        if c.motivo_sv not in tramos:
            continue
        if c.dia_creacion is None:
            sin_fecha += 1
            continue
        edad = max(0, (hoy - c.dia_creacion).days)
        for nombre, lo, hi in TRAMOS_REZAGO:
            if edad >= lo and (hi is None or edad <= hi):
                tramos[c.motivo_sv][nombre] += 1
                break
    return {
        'al_dia_operativo': hoy.isoformat(),
        'pendiente': tramos['pendiente'],
        'en_curso': tramos['en_curso'],
        'total_pendiente': sum(tramos['pendiente'].values()),
        'total_en_curso': sum(tramos['en_curso'].values()),
        'excluidos': {'sin_fecha_de_creacion': sin_fecha} if sin_fecha else {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# b · Volumen y flujo
# ─────────────────────────────────────────────────────────────────────────────

def _lunes(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _volumen(cadenas, desde, hasta) -> dict:
    iniciadas = [c for c in cadenas if _en(c.dia_creacion, desde, hasta)]
    cerradas = [c for c in cadenas if c.cerrada and _en(c.dia, desde, hasta)]
    sin_dia = sum(1 for c in cadenas if c.cerrada and c.dia is None)

    def _por(clave):
        out = defaultdict(lambda: {'iniciadas': 0, 'cerradas': 0})
        for c in iniciadas:
            out[clave(c) or '?']['iniciadas'] += 1
        for c in cerradas:
            out[clave(c) or '?']['cerradas'] += 1
        return dict(sorted(out.items()))

    con_cc2 = sum(1 for c in cerradas if c.hijo is not None)
    con_cc3 = sum(1 for c in cerradas if c.nieto is not None)
    sin_veredicto = Counter(c.motivo_sv for c in iniciadas if not c.cerrada)
    excl = {'cerradas_sin_fecha_de_confirmacion': sin_dia} if sin_dia else {}
    return {
        'unidad': 'cadena (CC1 + CC2 + CC3); una fila hija nunca cuenta como conteo',
        'iniciadas': len(iniciadas),
        'iniciadas_definicion': ('raíz creada en el rango (fecha_creacion). No se usa '
                                 'fecha_inicio: se borra al liberar zombis y al pausar.'),
        'cerradas': len(cerradas),
        'skus_cerrados': len({c.raiz.producto_id for c in cerradas}),
        'por_almacen': _por(lambda c: c.raiz.almacen.nombre if c.raiz.almacen else None),
        'por_clase': _por(lambda c: c.raiz.clasificacion_abc),
        'por_tipo': _por(lambda c: c.raiz.tipo),
        'por_veredicto': {v: sum(1 for c in cerradas if c.veredicto == v)
                          for v in VEREDICTOS if v != SIN_VEREDICTO},
        'a_cc2': _metrica(con_cc2, len(cerradas), excl),
        'a_cc3': _metrica(con_cc3, len(cerradas), excl),
        'omitidas': sin_veredicto.get('omitida', 0),
        'sin_veredicto': dict(sorted(sin_veredicto.items())),
        'excluidos': excl,
    }


def _recuentos(cadenas, desde, hasta) -> dict:
    """Conteos descartados porque Siesa se movió mientras se contaba (m029),
    contados por el día en que se descartaron.

    El denominador son los intentos que PODÍAN descartarse: conteos confirmados
    en el rango con foto de inicio, más los propios descartes. Un conteo sin
    foto de inicio (anterior a m029, o Siesa no respondió al abrir) no puede
    descartarse nunca, así que meterlo abajo diluiría la tasa con conteos que no
    la miden — va a `excluidos`.
    """
    recuentos = 0
    por_producto = Counter()
    confirmados = 0
    excl = Counter()
    for c in cadenas:
        for s in c.nodos:
            for dia in _descartes(s):
                if dia is None:
                    excl['descarte_sin_fecha'] += 1
                elif _en(dia, desde, hasta):
                    recuentos += 1
                    por_producto[s.producto_id] += 1
            if s.foto_siesa_at is not None and _en(dia_operativo_de(s.foto_siesa_at), desde, hasta):
                if s.foto_inicio_at is not None:
                    confirmados += 1
                else:
                    excl['confirmado_sin_foto_de_inicio'] += 1
    return {'metrica': _metrica(recuentos, confirmados + recuentos, excl),
            'por_producto': por_producto}


def _por_semana(cerradas, ajustes) -> list:
    semanas = defaultdict(lambda: {'cerradas': 0, 'ok': 0, 'error': 0,
                                   'ajustes': 0, 'unidades_ajustadas': 0})
    for c in cerradas:
        f = semanas[_lunes(c.dia)]
        f['cerradas'] += 1
        f['ok'] += c.veredicto in VEREDICTOS_OK
        f['error'] += c.veredicto in VEREDICTOS_ERROR
    for c in ajustes:
        f = semanas[_lunes(c.dia)]
        f['ajustes'] += 1
        f['unidades_ajustadas'] += abs(c.raiz.diferencia)
    return [{'semana': k.isoformat(), **v} for k, v in sorted(semanas.items())]


# ─────────────────────────────────────────────────────────────────────────────
# c · Ajustes
# ─────────────────────────────────────────────────────────────────────────────

#: Resumen del texto de `ConteoService.motivo_bloqueo_ajuste`. La función
#: devuelve prosa para la pantalla; acá se agrupa. Si alguien cambia la prosa y
#: una frase deja de aparecer, el motivo cae en `OTRO` — visible, no perdido — y
#: `TestBloqueadosSeAgrupan` se pone rojo.
_CLAVES_BLOQUEO = (
    ('no tiene foto de Siesa (existencia', 'SIN_FOTO_CIERRE'),
    ('salidas sin confirmar que NO', 'SALIDAS_NO_POS'),
    ('de la APERTURA', 'SIN_FOTO_APERTURA'),
    ('se movió mientras se contaba', 'MOVIMIENTO_DURANTE_CONTEO'),
    ('quedó viejo', 'CONTEO_VIEJO'),
    ('traslado entrando', 'TRASLADO_ENTRANTE'),
    ('mercancía de este producto en proceso', 'MERCANCIA_EN_PROCESO'),
)


def resumir_motivo_bloqueo(texto: str) -> str:
    for frase, clave in _CLAVES_BLOQUEO:
        if frase in texto:
            return clave
    return 'OTRO'


def _es_ajuste(c: _Cadena) -> bool:
    r = c.raiz
    return (r.estado in (EstadoConteo.AJUSTADO, EstadoConteo.AJUSTANDO)
            and r.motivo_codigo in MOTIVOS_AJUSTE)


def _es_ensayo(r: SesionConteo) -> bool:
    """AJUSTADO sin `siesa_triggered`: `_ejecutar_con_preflag` revierte la
    marca cuando el POST fue en modo ensayo — el WMS cerró la sesión y a Siesa
    no llegó nada. Contarlo como ajuste sería reportar un documento que no
    existe."""
    return r.estado == EstadoConteo.AJUSTADO and not r.siesa_triggered


def _ajustes(cadenas, desde, hasta) -> tuple:
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    from app.services.conteo_service import ConteoService

    excl = Counter()
    validos = []
    for c in cadenas:
        if not _es_ajuste(c):
            continue
        if c.dia is None:
            excl['sin_fecha_de_confirmacion'] += 1
            continue
        if not _en(c.dia, desde, hasta):
            continue
        if _es_ensayo(c.raiz):
            excl['modo_ensayo'] += 1
            continue
        if c.raiz.diferencia is None:
            excl['sin_diferencia'] += 1
            continue
        validos.append(c)

    ent = sum(c.raiz.diferencia for c in validos if c.raiz.diferencia > 0)
    sal = sum(-c.raiz.diferencia for c in validos if c.raiz.diferencia < 0)
    valor_ent = valor_sal = 0.0
    valorizados = sin_costo = 0
    for c in validos:
        costo = _float(c.raiz.costo_prom_uni_siesa)
        if costo is None or costo <= 0:
            sin_costo += 1
            continue
        valorizados += 1
        if c.raiz.diferencia > 0:
            valor_ent += c.raiz.diferencia * costo
        else:
            valor_sal += -c.raiz.diferencia * costo

    # Bloqueados: foto de HOY — raíces en DESCUADRE esperando decisión.
    bloqueados = Counter()
    aprobables = 0
    for c in cadenas:
        if c.raiz.estado != EstadoConteo.DESCUADRE:
            continue
        motivo = ConteoService.motivo_bloqueo_ajuste(c.raiz)
        if motivo:
            bloqueados[resumir_motivo_bloqueo(motivo)] += 1
        else:
            aprobables += 1

    ids_raices = {c.raiz.id for c in cadenas}
    fallidos = SiesaJob.query.filter(SiesaJob.tipo == 'AJUSTE_CONTEO',
                                     SiesaJob.estado == EstadoSiesaJob.FALLIDO).all()
    jobs_fallidos = sum(1 for j in fallidos if j.referencia_id in ids_raices)

    auditoria = Counter(c.raiz.motivo_codigo for c in cadenas
                        if _en(c.dia_creacion, desde, hasta)
                        and c.raiz.motivo_codigo
                        and c.raiz.motivo_codigo not in MOTIVOS_AJUSTE)
    bloque = {
        'cantidad': len(validos),
        'en_vuelo': sum(1 for c in validos if c.raiz.estado == EstadoConteo.AJUSTANDO),
        'automaticos': sum(1 for c in validos if c.raiz.aprobador_id is None),
        'aprobados_por_supervisor': sum(1 for c in validos if c.raiz.aprobador_id is not None),
        'unidades_ent': ent,
        'unidades_sal': sal,
        'unidades_neto': ent - sal,
        'valor': {
            'etiqueta': ETIQUETA_VALOR,
            'ent': round(valor_ent, 2),
            'sal': round(valor_sal, 2),
            'neto': round(valor_ent - valor_sal, 2),
            'ajustes_valorizados': valorizados,
            'ajustes_sin_costo': sin_costo,
        },
        'excluidos': dict(excl),
        'bloqueados_hoy': {'total': sum(bloqueados.values()),
                           'por_motivo': dict(sorted(bloqueados.items())),
                           'descuadres_aprobables': aprobables},
        'jobs_fallidos_hoy': jobs_fallidos,
        'motivos_auditoria_picking': {
            'nota': 'diagnóstico del picker al crear la auditoría — NO son ajustes',
            'por_motivo': dict(sorted(auditoria.items())),
        },
    }
    return bloque, validos


# ─────────────────────────────────────────────────────────────────────────────
# d · Exactitud de inventario (IRA)
# ─────────────────────────────────────────────────────────────────────────────

def _exactitud(cerradas) -> dict:
    """OK / (OK + ERROR) por clase, **solo** sobre cadenas cuyo conteo
    resolutivo se midió contra una foto de Siesa (`teorico_siesa` y fuente
    SIESA). Un conteo comparado contra el stock del WMS mide si el WMS cuadra
    consigo mismo, no si el inventario cuadra con el ERP.

    DIARIO_ABC separado del resto: el plan elige huecos por antigüedad; manual,
    watchdog y auditoría eligen huecos donde ya se sospechaba un problema.
    Mezclarlos baja la exactitud por sesgo de selección, no por el inventario.
    """
    grupos = defaultdict(lambda: {'ok': 0, 'err': 0})
    excl = Counter()
    for c in cerradas:
        s = c.resolutiva
        if s is None or s.teorico_siesa is None or s.fuente_existencia != 'SIESA':
            excl['sin_foto_siesa'] += 1
            continue
        grupo = 'DIARIO_ABC' if c.raiz.tipo == 'DIARIO_ABC' else 'OTROS'
        g = grupos[(c.raiz.clasificacion_abc or '?', grupo)]
        if c.veredicto in VEREDICTOS_OK:
            g['ok'] += 1
        else:
            g['err'] += 1
    por_clase = defaultdict(dict)
    for (cl, grupo), g in sorted(grupos.items()):
        por_clase[cl][grupo] = _metrica(g['ok'], g['ok'] + g['err'], min_n=MIN_N_EXACTITUD)
    return {
        'definicion': 'OK / (OK + ERROR) por cadena, contra la foto de Siesa del conteo que resolvió',
        'min_n': MIN_N_EXACTITUD,
        'por_clase': dict(por_clase),
        'excluidos': dict(excl),
    }


# ─────────────────────────────────────────────────────────────────────────────
# e · Productos problema
# ─────────────────────────────────────────────────────────────────────────────

def _productos_problema(cerradas, ajustes, recuentos_por_producto, muestra) -> dict:
    errores = [c for c in cerradas if c.veredicto in VEREDICTOS_ERROR
               and c.resolutiva is not None and c.resolutiva.diferencia is not None]
    errores.sort(key=lambda c: (-abs(c.resolutiva.diferencia), c.raiz.id))
    por_diferencia = [{**_producto_fila(c.raiz), 'diferencia': c.resolutiva.diferencia,
                       'veredicto': c.veredicto, 'dia': c.dia.isoformat(),
                       'codigo': c.raiz.codigo}
                      for c in errores[:TOP_N]]

    def _top(contador):
        filas = sorted(contador.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_N]
        return [{**_producto_fila(muestra[pid]), 'n': n} for pid, n in filas if pid in muestra]

    return {
        'por_diferencia': por_diferencia,
        'por_ajustes': _top(Counter(c.raiz.producto_id for c in ajustes)),
        'por_recuentos': _top(recuentos_por_producto),
    }


# ─────────────────────────────────────────────────────────────────────────────
# f · Por operario — SOLO volumen
# ─────────────────────────────────────────────────────────────────────────────

def _por_operario(cerradas) -> dict:
    """Cuántas cadenas cerradas en el rango tuvieron un conteo de cada persona.

    **Sin exactitud y sin ranking, por decisión del usuario**, y por tres
    razones que valen más allá de hoy:

    1. **Muestra chica.** Con la operación medida (25 MATCH en cinco meses),
       el «acierto» de una persona sale de dos o tres conteos: ruido con nombre
       propio.
    2. **Sesgo de selección del CC2.** El segundo conteo solo existe cuando el
       primero NO cuadró, así que a quien le tocan CC2 le tocan exactamente los
       huecos difíciles. Su «exactitud» mide a qué huecos lo mandaron, no cómo
       cuenta.
    3. **Uso punitivo.** Un porcentaje por persona empuja a contar para cuadrar
       — a «encontrar» lo que el sistema espera—, que es la manera más barata de
       volver ciego el double-blind.

    Por eso la lista sale ordenada por nombre, no por volumen: ordenarla por
    cantidad ya es un ranking.
    """
    from app.models.usuario import Usuario

    cadenas_por = defaultdict(set)
    conteos_por = Counter()
    sin_operario = 0
    for c in cerradas:
        for s in c.nodos:
            if s.cantidad_fisica is None:
                continue
            if s.operario_id is None:
                sin_operario += 1
                continue
            cadenas_por[s.operario_id].add(c.raiz.id)
            conteos_por[s.operario_id] += 1
    nombres = {}
    if cadenas_por:
        nombres = {u.id: u.nombre for u in
                   Usuario.query.filter(Usuario.id.in_(list(cadenas_por))).all()}
    filas = [{'operario_id': uid, 'nombre': nombres.get(uid),
              'cadenas': len(cids), 'conteos': conteos_por[uid]}
             for uid, cids in cadenas_por.items()]
    filas.sort(key=lambda f: ((f['nombre'] or '').lower(), f['operario_id']))
    return {
        'nota': ('Solo volumen. Sin exactitud ni ranking por persona: muestra chica, '
                 'sesgo de selección del segundo conteo y uso punitivo.'),
        'filas': filas,
        'excluidos': {'conteo_sin_operario': sin_operario} if sin_operario else {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Entrada
# ─────────────────────────────────────────────────────────────────────────────

def resolver_rango(fecha_desde=None, fecha_hasta=None, ahora: datetime = None) -> tuple:
    """El rango efectivo, en días operativos. Por defecto las últimas
    `DIAS_POR_DEFECTO` días hasta hoy. Un rango invertido es un error del que
    pregunta, no algo que se da vuelta en silencio."""
    hoy = dia_operativo_de(ahora or datetime.utcnow())
    fecha_hasta = fecha_hasta or hoy
    fecha_desde = fecha_desde or (fecha_hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if fecha_desde > fecha_hasta:
        raise ValueError(f'desde ({fecha_desde}) es posterior a hasta ({fecha_hasta})')
    return fecha_desde, fecha_hasta


def calcular_estadisticas_conteo(fecha_desde: date = None, fecha_hasta: date = None, *,
                                 almacen_id: int = None, clase: str = None,
                                 tipo: str = None, ahora: datetime = None) -> dict:
    """El reporte completo. `ahora` (UTC naive) se inyecta en los tests; la
    fecha de negocio sale siempre de `dia_operativo_de`, nunca de
    `utcnow().date()` (Regla 5)."""
    from app.services.abc_service import FRECUENCIA_DIAS

    if clase is not None and clase not in FRECUENCIA_DIAS:
        raise ValueError(f'clase inválida: {clase!r} (A, B o C)')
    ahora = ahora or datetime.utcnow()
    hoy = dia_operativo_de(ahora)
    desde, hasta = resolver_rango(fecha_desde, fecha_hasta, ahora)

    todas = _cargar_cadenas(almacen_id)
    filtradas = [c for c in todas
                 if (clase is None or c.raiz.clasificacion_abc == clase)
                 and (tipo is None or c.raiz.tipo == tipo)]
    cerradas = [c for c in filtradas if c.cerrada and _en(c.dia, desde, hasta)]

    volumen = _volumen(filtradas, desde, hasta)
    recuentos = _recuentos(filtradas, desde, hasta)
    volumen['recuentos'] = recuentos['metrica']
    ajustes, ajustes_validos = _ajustes(filtradas, desde, hasta)
    volumen['por_semana'] = _por_semana(cerradas, ajustes_validos)
    muestra = {}
    for c in filtradas:
        muestra.setdefault(c.raiz.producto_id, c.raiz)

    return {
        'parametros': {'desde': desde.isoformat(), 'hasta': hasta.isoformat(),
                       'almacen_id': almacen_id, 'clase': clase, 'tipo': tipo},
        'generado_al_dia_operativo': hoy.isoformat(),
        'fuente': 'solo base del WMS — cero llamadas a Siesa',
        'carga_cobertura': _carga_y_cobertura(todas, almacen_id, clase, hoy, ahora),
        'rezago': _rezago(filtradas, hoy),
        'volumen': volumen,
        'ajustes': ajustes,
        'exactitud': _exactitud(cerradas),
        'productos_problema': _productos_problema(cerradas, ajustes_validos,
                                                  recuentos['por_producto'], muestra),
        'por_operario': _por_operario(cerradas),
    }
