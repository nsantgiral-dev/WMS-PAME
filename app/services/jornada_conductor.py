"""
La jornada del conductor — Fase 0 (2026-09-24).

La pregunta del dueño: *los conductores, que además de manejar entregan y
cobran, no son controlados y «van y hacen otras cosas».* Esta vista no
contesta eso. Contesta lo único que los datos que ya existen permiten
contestar con honestidad: **qué dejó registrado cada conductor en su día,
a qué hora, con qué confianza, y dónde hay tiempo que ningún registro
explica.** Cero pasos nuevos al conductor.

## Lo que es y lo que no es

- Reconstruye **conductor × día Bogotá** como una línea de tiempo de eventos
  que el WMS y flota ya guardan. Cada evento dice de dónde sale
  (`fuente` = tabla.columna), con qué hora (`hora_fuente`: teléfono o
  servidor) y cuánto se le puede creer (`confianza`).
- Un hueco entre dos eventos es **«no explicado»**, nunca «muerto»: el dato no
  dice qué hizo la persona, dice que no quedó registro. Un tramo largo puede
  ser tráfico, un cliente que no abrió, un encargo de la empresa.
- Las señales **proponen, no sancionan** (regla 2 de `flota/CLAUDE.md`). Cada
  una trae su evidencia concreta y su comparación contra los **compañeros de
  la misma ruta maestra**, con el `n` a la vista. Con menos de
  `n_minimo_pares` casos la comparación sale «sin base» y no se inventa.
- Un día con poca evidencia es **«jornada no reconstruible»**, no «limpio».

## Las horas y su confianza

| Evento | Hora | Confianza |
|---|---|---|
| Turno, preoperacional, tanqueo | servidor | alta — el gesto solo existe en línea (fetch directo), así que la hora del servidor es la del hecho |
| Parada, con hora del teléfono | teléfono (corregida por el desfase medido al sincronizar) | alta si el reloj estaba bien, media si no se midió o estaba corrido |
| Parada, sin hora del teléfono | servidor (`fecha_creacion` = primera confirmación) | **baja**: con la cola sin señal es la hora de SINCRONIZAR, no la de la entrega |

Las columnas de hora del teléfono (`ts_dispositivo`, `ts_desfase_s`,
`via_cola` en `recaudos_entrega`; `ts_dispositivo`, `pos_ts_dispositivo` en
`entregas_geo`) las agrega otra tanda. Se leen **por inspección de la base**,
no del modelo: con ellas o sin ellas esto funciona, y **sin ellas no calcula
ráfagas** —varias paradas confirmadas a la vez— y lo declara, porque con hora
del servidor una ráfaga de sincronización y una de «confirmé todo desde la
esquina» son indistinguibles.

## El punto para meter eventos nuevos (Fase 1 y Fase 3)

`FUENTES_DE_EVENTOS` es la lista de funciones que producen eventos. La tabla
`jornada_evento` de la Fase 1 (GPS automático al abrir ruta/parada, «salí» y
«volví») y el GPS del vehículo de la Fase 3 entran como **una función más** en
esa tupla, con su `fuente` y su `confianza`: la línea de tiempo, los huecos, la
cobertura y las señales no cambian.

Cero llamadas a Siesa. Todo sale de la base del WMS y de flota.
"""
import json
import logging
import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import sqlalchemy as sa

from app.extensions import db
from app.services.analitica_recorrido import mediana, percentil
from app.utils.fecha import (TZ_BOGOTA, ahora_bogota, dia_operativo, dia_operativo_de,
                             inicio_del_dia_utc)

logger = logging.getLogger(__name__)


class FiltroInvalido(ValueError):
    """Un parámetro que no se entiende. La ruta responde 400, nunca lo ignora."""


# ─────────────────────────────────────────────────────────────────────────────
# Umbrales — UN solo sitio. Declarados en cada respuesta.
# ─────────────────────────────────────────────────────────────────────────────

#: Todo número con el que se decide algo vive acá. Se pueden ajustar sin tocar
#: código con la variable `JORNADA_UMBRALES` (JSON con las claves a cambiar).
#: Son **provisionales**: ninguno es una meta del negocio, son el punto a partir
#: del cual algo merece que alguien lo mire.
UMBRALES_POR_DEFECTO = {
    # Con menos casos que esto, la comparación contra los compañeros sale
    # «sin base» y no se usa para proponer nada.
    'n_minimo_pares': 10,
    # Paradas propias mínimas del conductor para comparar SU tasa.
    'n_minimo_conductor': 10,
    # Días hacia atrás que forman la base de los compañeros.
    'ventana_pares_dias': 90,
    # Hueco de referencia cuando no hay base de la maestra (minutos).
    'hueco_sin_base_min': 60,
    # Huecos más cortos que esto no se listan (ruido de minutos).
    'hueco_minimo_listado_min': 10,
    # Tiempo no explicado del día a partir del cual se propone mirar.
    'no_explicado_min_para_senal': 30,
    # km que un vehículo sumó entre dos turnos de conductor (en patio).
    'km_entre_turnos_min': 1,
    # Tasa del conductor vs lo esperado según sus compañeros.
    'razon_vs_pares': 2.0,
    'diferencia_minima_puntos': 0.05,
    'observados_minimos': 2,
    # GPS con incertidumbre mayor que esto no ubica una puerta.
    'precision_anomala_m': 100,
    # Preoperacional más rápido que esto, cuando no hay base de compañeros.
    'inspeccion_segundos_sin_base': 60,
    # Reloj del teléfono corrido más de esto: la hora se corrige y baja a media.
    'desfase_reloj_max_s': 300,
    # Ráfaga: tantas paradas o más confirmadas dentro de esta ventana.
    'rafaga_ventana_s': 120,
    'rafaga_min_paradas': 3,
    # Por debajo de esta cobertura la jornada es «no reconstruible».
    'cobertura_minima': 0.5,
}


def umbrales() -> dict:
    """Los umbrales vigentes: los de este módulo, con lo que ajuste el entorno.

    Una clave desconocida o un valor no numérico **no se aplica** y queda
    declarado en `_rechazados`: un ajuste que no entró tiene que verse.
    """
    u = dict(UMBRALES_POR_DEFECTO)
    rechazados = {}
    crudo = os.environ.get('JORNADA_UMBRALES', '').strip()
    if crudo:
        try:
            pedidos = json.loads(crudo)
        except ValueError:
            pedidos, rechazados['JORNADA_UMBRALES'] = {}, 'no es JSON'
        if not isinstance(pedidos, dict):
            pedidos, rechazados['JORNADA_UMBRALES'] = {}, 'no es un objeto JSON'
        for k, v in pedidos.items():
            if k not in UMBRALES_POR_DEFECTO:
                rechazados[k] = 'clave desconocida'
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                rechazados[k] = f'valor inválido: {v!r}'
            else:
                u[k] = v
    u['_rechazados'] = rechazados
    return u


# ─────────────────────────────────────────────────────────────────────────────
# Vocabulario
# ─────────────────────────────────────────────────────────────────────────────

TITULOS = {
    'recibo_turno':        'Recibió el vehículo',
    'preoperacional':      'Inspección preoperacional',
    'cargue':              'Cargue en el muelle',
    'cierre_cargue':       'Cierre del cargue',
    'parada':              'Confirmó una parada',
    'parada_por_otro':     'Parada confirmada por otra persona',
    'parada_forzada':      'Parada cerrada por la oficina (cierre forzado)',
    'tanqueo':             'Tanqueo',
    'cierre_ruta':         'Ruta cerrada',
    'cierre_ruta_forzado': 'Ruta cerrada a la fuerza por la oficina',
    'entrega_turno':       'Entregó el vehículo',
    'liquidacion':         'Ruta liquidada en la oficina',
}

#: Orden de desempate entre eventos con la misma hora.
_ORDEN = {t: i for i, t in enumerate(TITULOS)}

#: Motivos de rechazo que se comparan contra los compañeros (contrato Fase 0).
MOTIVOS_VIGILADOS = ('CLIENTE_CERRADO', 'FUERA_DE_HORARIO', 'NO_PAGO_SE_QUEDO',
                     'DIRECCION_ERRADA')

TITULOS_MOTIVO = {
    'CLIENTE_CERRADO':  'cliente cerrado',
    'FUERA_DE_HORARIO': 'fuera de horario',
    'NO_PAGO_SE_QUEDO': 'no pagó y se quedó la mercancía',
    'DIRECCION_ERRADA': 'dirección errada',
}

_CONF_NUM = {'alta': 3, 'media': 2, 'baja': 1}

#: Lo que esta vista no puede ver con los datos de hoy. Va en cada respuesta,
#: para que nadie lea el silencio como «no pasó».
#: En palabras de bodega, sin nombres de funciones ni fases del proyecto
#: (2026-09-24): quien lo lee es el encargado, no quien mantiene el sistema.
NO_PUEDE_VER = (
    'A qué hora salió de verdad el camión: «cerró el cargue» es cuando el '
    'muelle terminó de cargarlo, no cuando arrancó. Todavía no hay un botón '
    'de «salí» ni de «volví».',
    'Dónde estuvo el camión entre dos registros: sin GPS en el vehículo, un '
    'tiempo sin explicar no dice a dónde fue.',
    'La hora real de una entrega que se confirmó sin señal: se ve la hora en '
    'que el teléfono la pudo enviar.',
    'Quién cerró la ruta: el sistema no lo guarda, así que ese cierre no se '
    'cuenta como un registro del conductor.',
    'El orden y la hora planeados de las entregas: la ruta solo dice qué '
    'municipios recorre.',
    'Horas extra, pausas y viáticos: no hay registro de jornada laboral. Esta '
    'vista no mide cumplimiento de horario.',
)

def _motivos_en_palabras() -> dict:
    """`{código: etiqueta}` del catálogo de motivos de rechazo."""
    from app.services.motivos_rechazo import MOTIVOS
    return {m.codigo: m.etiqueta for m in MOTIVOS}


AVISO_LEGAL = (
    'Datos personales (Ley 1581 de 2012): esta vista usa registros que el '
    'conductor produce en su jornada de trabajo. Antes de usarla para hablar '
    'con un conductor, él debe haber sido informado POR ESCRITO de esta '
    'finalidad —control operativo de la ruta, del vehículo y del dinero '
    'recaudado—, y solo cubre su jornada laboral. Una señal es una pregunta '
    'para hacerle, no una conclusión: él puede explicar con evidencia.'
)

QUE_HACER = ('Preguntarle al conductor. Puede haber una razón que el sistema '
             'no ve (tráfico, un cliente que no abrió, un encargo de la '
             'empresa). El sistema no concluye: propone una pregunta.')

#: Columnas de hora del teléfono que agrega otra tanda (m041flfugas). Se leen
#: por inspección de la base; su ausencia se declara, no se rellena.
COLUMNAS_TELEFONO = {
    'recaudos_entrega': ('ts_dispositivo', 'ts_desfase_s', 'via_cola'),
    'entregas_geo': ('ts_dispositivo', 'pos_ts_dispositivo'),
}


# ─────────────────────────────────────────────────────────────────────────────
# Evento
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Evento:
    tipo: str
    ts: datetime                      # UTC naive: la hora que se usa
    hora_fuente: str                  # 'telefono' | 'servidor'
    confianza: str                    # 'alta' | 'media' | 'baja'
    confianza_motivo: str
    propio: bool                      # gesto del propio conductor
    fuente: str                       # tabla.columna
    detalle: dict = field(default_factory=dict)
    ts_fin: Optional[datetime] = None
    ts_servidor: Optional[datetime] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    precision_m: Optional[float] = None
    ruta_id: Optional[int] = None
    recaudo_id: Optional[int] = None

    @property
    def es_parada(self):
        return self.tipo in ('parada', 'parada_por_otro', 'parada_forzada')

    def a_dict(self) -> dict:
        return {
            'tipo': self.tipo,
            'titulo': TITULOS[self.tipo],
            'ts': _iso(self.ts),
            'hora': _hora_local(self.ts),
            'ts_fin': _iso(self.ts_fin),
            'hora_fin': _hora_local(self.ts_fin) if self.ts_fin else None,
            'hora_fuente': self.hora_fuente,
            'hora_servidor': _hora_local(self.ts_servidor) if self.ts_servidor else None,
            'confianza': self.confianza,
            'confianza_motivo': self.confianza_motivo,
            'propio': self.propio,
            'fuente': self.fuente,
            'detalle': self.detalle,
            'lat': self.lat, 'lon': self.lon, 'precision_m': self.precision_m,
            'ruta_id': self.ruta_id,
        }


_EN_LINEA = ('alta', 'hora del servidor: este gesto solo se registra en línea, '
                     'así que es la hora del hecho')


def _iso(ts):
    return ts.isoformat() + 'Z' if ts else None


def _local(ts):
    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA)


def _hora_local(ts):
    return _local(ts).strftime('%H:%M') if ts else None


def _min_del_dia(ts):
    t = _local(ts)
    return t.hour * 60 + t.minute


def _cuando(ts):
    """«2026-09-15 07:30», en Bogotá: lo que lee una persona en la evidencia."""
    return _local(ts).strftime('%Y-%m-%d %H:%M') if ts else None


_ESTADO_RUTA = {'PROGRAMADO': 'programada', 'EN_CARGUE': 'en cargue',
                'EN_TRANSITO': 'en tránsito', 'ENTREGADA': 'cerrada'}


def _hhmm(minutos):
    if minutos is None:
        return None
    m = int(round(minutos))
    return f'{m // 60:02d}:{m % 60:02d}'


def _minutos(a, b):
    return (b - a).total_seconds() / 60.0


def _redondo(x, n=1):
    return None if x is None else round(float(x), n)


# ─────────────────────────────────────────────────────────────────────────────
# Validación de parámetros
# ─────────────────────────────────────────────────────────────────────────────

MAX_DIAS_RANGO = 92


def dia_de(texto, nombre='dia', defecto=None) -> date:
    texto = (texto or '').strip()
    if not texto:
        if defecto is not None:
            return defecto
        raise FiltroInvalido(f'{nombre} es obligatorio (AAAA-MM-DD)')
    try:
        d = date.fromisoformat(texto)
    except ValueError:
        raise FiltroInvalido(f'{nombre} debe ser una fecha AAAA-MM-DD (llegó {texto!r})')
    if d > dia_operativo():
        raise FiltroInvalido(f'{nombre} = {d} es futuro: no hay jornada que reconstruir')
    if d.year < 2020:
        raise FiltroInvalido(f'{nombre} = {d} es anterior a cualquier dato del WMS')
    return d


def entero_de(texto, nombre) -> int:
    texto = (texto or '').strip()
    if not texto:
        raise FiltroInvalido(f'{nombre} es obligatorio')
    if not texto.isdigit() or len(texto) > 9:
        raise FiltroInvalido(f'{nombre} debe ser un número (llegó {texto!r})')
    return int(texto)


def rango_de(args) -> tuple:
    hasta = dia_de(args.get('hasta'), 'hasta', defecto=dia_operativo())
    desde = dia_de(args.get('desde'), 'desde', defecto=hasta - timedelta(days=6))
    if desde > hasta:
        raise FiltroInvalido(f'desde ({desde}) es posterior a hasta ({hasta})')
    if (hasta - desde).days + 1 > MAX_DIAS_RANGO:
        raise FiltroInvalido(
            f'el rango tiene {(hasta - desde).days + 1} días; el máximo es {MAX_DIAS_RANGO}')
    return desde, hasta


# ─────────────────────────────────────────────────────────────────────────────
# Hora del teléfono — por inspección de la base
# ─────────────────────────────────────────────────────────────────────────────

def columnas_hora_del_telefono() -> dict:
    """Qué columnas de hora del teléfono existen HOY en la base.

    Se pregunta a la base y no al modelo: la columna puede estar en el modelo y
    no en la base (migración pendiente) o al revés, y lo que se puede leer es lo
    que está en la base.
    """
    insp = sa.inspect(db.engine)
    out = {}
    for tabla, cols in COLUMNAS_TELEFONO.items():
        presentes = {c['name'] for c in insp.get_columns(tabla)}
        out[tabla] = tuple(c for c in cols if c in presentes)
    return out


def _a_utc_naive(v):
    if v is None:
        return None
    if isinstance(v, str):
        v = datetime.fromisoformat(v.replace('Z', '+00:00'))
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _leer_columnas(tabla, clave, ids, cols) -> Dict[int, dict]:
    """`{clave: {col: valor}}` leyendo columnas que el modelo puede no tener.

    Los nombres salen de `COLUMNAS_TELEFONO` (lista blanca), nunca del request.
    """
    if not cols or not ids:
        return {}
    out = {}
    lista = sorted(ids)
    q = sa.text(f'SELECT {clave}, {", ".join(cols)} FROM {tabla} '
                f'WHERE {clave} IN :ids').bindparams(sa.bindparam('ids', expanding=True))
    for i in range(0, len(lista), 500):
        for fila in db.session.execute(q, {'ids': lista[i:i + 500]}):
            d = dict(zip(cols, fila[1:]))
            for c in ('ts_dispositivo', 'pos_ts_dispositivo'):
                if c in d:
                    d[c] = _a_utc_naive(d[c])
            if 'via_cola' in d and d['via_cola'] is not None:
                d['via_cola'] = bool(d['via_cola'])
            if 'ts_desfase_s' in d and d['ts_desfase_s'] is not None:
                d['ts_desfase_s'] = int(d['ts_desfase_s'])
            out[fila[0]] = d
    return out


# ─────────────────────────────────────────────────────────────────────────────
# El mundo: todo lo que hace falta, en pocas consultas
# ─────────────────────────────────────────────────────────────────────────────

class Mundo:
    """Las filas de un rango de días Bogotá, cargadas una vez.

    El rango incluye la ventana de los compañeros hacia atrás: la base de
    comparación de un día sale de los `ventana_pares_dias` anteriores.
    """

    def __init__(self, lo_dia: date, hi_dia: date, U: dict):
        from app.models.bulto import Bulto
        from app.models.conductor import Conductor
        from app.models.geo_entrega import EntregaGeo
        from app.models.packing import TareaPacking
        from app.models.recaudo_entrega import RecaudoEntrega
        from app.models.ruta_despacho import RutaDespacho
        from app.models.ruta_maestra import RutaMaestra
        from app.models.vehiculo import Vehiculo

        self.U = U
        self.lo_dia, self.hi_dia = lo_dia, hi_dia
        self.lo, self.hi = inicio_del_dia_utc(lo_dia), inicio_del_dia_utc(hi_dia + timedelta(days=1))
        lo, hi = self.lo, self.hi
        self.fuentes_no_disponibles = []

        self.conductores = {c.id: c for c in Conductor.query.all()}
        self.conductor_de_usuario = {c.usuario_id: c.id for c in self.conductores.values()
                                     if c.usuario_id}
        self.placas = {v.id: v.placa for v in Vehiculo.query.all()}
        self.maestras = {m.id: m.nombre for m in RutaMaestra.query.all()}

        R = RutaDespacho
        self.rutas = {r.id: r for r in R.query.filter(sa.or_(
            R.fecha_cierre.between(lo, hi), R.fecha_entregada.between(lo, hi),
            R.liquidada_en.between(lo, hi), R.fecha_programada.between(lo_dia, hi_dia),
            sa.and_(R.estado == 'EN_TRANSITO', R.fecha_cierre < hi),
        )).all()}
        ids = sorted(self.rutas)

        self.recaudos_por_ruta = {}
        self.forzadas = set()
        self.bultos = {}
        recaudos = []
        for i in range(0, len(ids), 500):
            trozo = ids[i:i + 500]
            recaudos += RecaudoEntrega.query.filter(RecaudoEntrega.ruta_id.in_(trozo)).all()
            for rid, mn, mx, nb, nt in db.session.query(
                    Bulto.ruta_despacho_id, sa.func.min(Bulto.fecha_cargado),
                    sa.func.max(Bulto.fecha_cargado), sa.func.count(Bulto.id),
                    sa.func.count(sa.distinct(Bulto.tarea_id))
            ).filter(Bulto.ruta_despacho_id.in_(trozo)).group_by(Bulto.ruta_despacho_id):
                self.bultos[rid] = {'primero': _a_utc_naive(mn), 'ultimo': _a_utc_naive(mx),
                                    'bultos': nb, 'paradas': nt}
            from app.models.bitacora import BitacoraAccion
            for (eid,) in db.session.query(BitacoraAccion.entidad_id).filter(
                    BitacoraAccion.accion == 'FORZAR', BitacoraAccion.entidad == 'RutaDespacho',
                    BitacoraAccion.entidad_id.in_(trozo)):
                self.forzadas.add(eid)
        for r in recaudos:
            self.recaudos_por_ruta.setdefault(r.ruta_id, []).append(r)
        rec_ids = {r.id for r in recaudos}
        tarea_ids = sorted({r.tarea_id for r in recaudos})
        self.tareas = {}
        for i in range(0, len(tarea_ids), 500):
            for tid, cli, mun, ped in db.session.query(
                    TareaPacking.id, TareaPacking.cliente, TareaPacking.municipio,
                    TareaPacking.numero_pedido_siesa).filter(
                    TareaPacking.id.in_(tarea_ids[i:i + 500])):
                self.tareas[tid] = {'cliente': cli, 'municipio': mun, 'pedido': ped}
        self.geo = {}
        lista = sorted(rec_ids)
        for i in range(0, len(lista), 500):
            for g in EntregaGeo.query.filter(EntregaGeo.recaudo_id.in_(lista[i:i + 500])):
                self.geo[g.recaudo_id] = g

        self.columnas_telefono = columnas_hora_del_telefono()
        self.telefono = _leer_columnas('recaudos_entrega', 'id', rec_ids,
                                       self.columnas_telefono['recaudos_entrega'])
        self.telefono_geo = _leer_columnas('entregas_geo', 'recaudo_id', rec_ids,
                                           self.columnas_telefono['entregas_geo'])

        # ── flota: si el paquete no carga, se declara y la jornada sigue ──
        self.custodias, self.inspecciones, self.tanqueos, self.sedes = [], [], [], {}
        try:
            from flota.adaptadores.modelos import Custodia, Inspeccion, LecturaOdometro
        except Exception as e:  # el WMS arranca sin flota (app/routes/__init__.py)
            self.fuentes_no_disponibles.append(
                f'flota ({type(e).__name__}): sin turnos, preoperacionales ni tanqueos')
        else:
            from app.models.almacen import Almacen
            self.sedes = {a.id: a.nombre for a in Almacen.query.all()}
            # TODA la historia de custodias: la tabla es chica (unas pocas por
            # vehículo y día), el turno anterior de un vehículo puede ser de hace
            # semanas y el siguiente —el que recibe con km de más— de después
            # del rango.
            self.custodias = (Custodia.query
                              .order_by(Custodia.vehiculo_id, Custodia.inicio_ts,
                                        Custodia.id).all())
            self.inspecciones = Inspeccion.query.filter(
                Inspeccion.respondida_ts >= lo, Inspeccion.respondida_ts < hi).all()
            self.tanqueos = LecturaOdometro.query.filter(
                LecturaOdometro.origen == 'tanqueo',
                LecturaOdometro.ts >= lo, LecturaOdometro.ts < hi).all()

        self._por_vehiculo = {}
        for k in self.custodias:
            self._por_vehiculo.setdefault(k.vehiculo_id, []).append(k)
        self._pos = {k.id: i for lista in self._por_vehiculo.values()
                     for i, k in enumerate(lista)}
        self._hechos = {}
        self._muestras = None
        self._dias_cache = {}
        self._rutas_por_dia = None
        self._casos_km = None
        self._lecturas_borde = None
        self._agregados = {}
        self._referencias = {}

    # ── utilidades ──────────────────────────────────────────────────────────

    def usuario_de(self, conductor_id):
        c = self.conductores.get(conductor_id)
        return c.usuario_id if c else None

    def nombre(self, conductor_id):
        c = self.conductores.get(conductor_id)
        return c.nombre if c else f'conductor {conductor_id}'

    def vecinas(self, k):
        """(anterior, siguiente) custodias del mismo vehículo."""
        lista = self._por_vehiculo.get(k.vehiculo_id, [])
        i = self._pos[k.id]
        return (lista[i - 1] if i > 0 else None,
                lista[i + 1] if i + 1 < len(lista) else None)

    def custodio_txt(self, k):
        if k is None:
            return 'nadie registrado'
        if k.custodio_conductor_id:
            return self.nombre(k.custodio_conductor_id)
        if k.custodio_sede_id:
            return f'la sede {self.sedes.get(k.custodio_sede_id, k.custodio_sede_id)}'
        return 'sede sin resolver'

    def hora_parada(self, r):
        """(ts, fuente, confianza, motivo) de la PRIMERA confirmación."""
        U = self.U
        srv = r.fecha_creacion or r.fecha_confirmacion
        tel = self.telefono.get(r.id) or {}
        ts_tel = tel.get('ts_dispositivo')
        if ts_tel is not None:
            des = tel.get('ts_desfase_s')
            if des is None:
                return ts_tel, 'telefono', 'media', ('hora del teléfono; no se midió '
                                                     'si su reloj estaba corrido')
            corregida = ts_tel - timedelta(seconds=des)
            if abs(des) > U['desfase_reloj_max_s']:
                return corregida, 'telefono', 'media', (
                    f'hora del teléfono corregida: su reloj estaba corrido '
                    f'{round(abs(des) / 60)} min')
            return corregida, 'telefono', 'alta', 'hora del teléfono'
        via = tel.get('via_cola')
        if via is False:
            return srv, 'servidor', 'alta', ('hora del servidor; llegó en línea, así '
                                             'que es la hora del hecho')
        if via is True:
            return srv, 'servidor', 'baja', ('hora del servidor al sincronizar: llegó '
                                             'por la cola sin señal, el hecho fue antes')
        return srv, 'servidor', 'baja', ('hora del servidor: si no había señal es la '
                                         'hora en que sincronizó, no la de la entrega')

    def es_forzada_auto(self, r):
        return (r.observaciones or '').startswith('Cierre forzado')

    def hechos_ruta(self, ruta_id):
        """Lo que una ruta deja medir, calculado una vez."""
        if ruta_id in self._hechos:
            return self._hechos[ruta_id]
        r = self.rutas[ruta_id]
        uid = self.usuario_de(r.conductor_id)
        forzada = ruta_id in self.forzadas
        paradas = []
        for rec in self.recaudos_por_ruta.get(ruta_id, []):
            ts, fuente, conf, motivo = self.hora_parada(rec)
            auto = self.es_forzada_auto(rec)
            propio = (uid is not None and rec.confirmado_por == uid and not auto)
            paradas.append({'rec': rec, 'ts': ts, 'fuente': fuente, 'confianza': conf,
                            'motivo': motivo, 'propio': propio, 'auto': auto})
        paradas.sort(key=lambda p: (p['ts'], p['rec'].id))
        propias = [p for p in paradas if p['propio']]
        dia = (dia_operativo_de(r.fecha_cierre) if r.fecha_cierre
               else r.fecha_programada or dia_operativo_de(r.fecha_creacion))
        h = {
            'ruta': r, 'dia': dia, 'forzada': forzada, 'paradas': paradas,
            'propias': propias, 'maestra_id': r.ruta_maestra_id,
            'duracion_h': None, 'salida_min': None, 'entre_paradas_min': [],
            'regreso_min': None, 'liquidar_h': None, 'hora_cierre_min': None,
        }
        if r.fecha_cierre:
            h['hora_cierre_min'] = _min_del_dia(r.fecha_cierre)
        if r.fecha_cierre and r.fecha_entregada and not forzada:
            h['duracion_h'] = (r.fecha_entregada - r.fecha_cierre).total_seconds() / 3600
        if propias and r.fecha_cierre:
            h['salida_min'] = _minutos(r.fecha_cierre, propias[0]['ts'])
        for a, b in zip(propias, propias[1:]):
            h['entre_paradas_min'].append(_minutos(a['ts'], b['ts']))
        if propias and r.fecha_entregada and not forzada:
            h['regreso_min'] = _minutos(propias[-1]['ts'], r.fecha_entregada)
        if r.fecha_entregada and r.liquidada_en:
            h['liquidar_h'] = (r.liquidada_en - r.fecha_entregada).total_seconds() / 3600
        self._hechos[ruta_id] = h
        return h

    # ── Muestras de los compañeros ──────────────────────────────────────────

    def muestras(self):
        """`{métrica: {maestra: [(día, conductor, valor), ...] ordenado}}` y
        las muestras por parada, por cierre de turno y por inspección."""
        if self._muestras is not None:
            return self._muestras
        por_ruta = {k: {} for k in ('duracion_h', 'salida_min', 'entre_paradas_min',
                                    'regreso_min', 'liquidar_h', 'hora_cierre_min')}
        paradas = []
        for rid in self.rutas:
            h = self.hechos_ruta(rid)
            r, M = h['ruta'], h['maestra_id']
            for k in por_ruta:
                v = h[k]
                vals = v if isinstance(v, list) else ([] if v is None else [v])
                for x in vals:
                    por_ruta[k].setdefault(M, []).append((h['dia'], r.conductor_id, x))
            for p in h['propias']:
                rec = p['rec']
                paradas.append((dia_operativo_de(p['ts']), r.conductor_id, M,
                                _banderas_parada(self, rec)))
        for k in por_ruta:
            for M in por_ruta[k]:
                por_ruta[k][M].sort(key=lambda t: t[0])
        paradas.sort(key=lambda t: t[0])
        cierres = []
        for k in self.custodias:
            if k.custodio_conductor_id and k.fin_ts is not None:
                _, nxt = self.vecinas(k)
                sigue = (nxt is not None and nxt.inicio_ts == k.fin_ts
                         and nxt.custodio_conductor_id == k.custodio_conductor_id)
                if sigue and nxt.ubicacion != 'fuera_de_sede':
                    continue    # se re-declaró a sí mismo: no cerró un turno
                fuera = sigue and nxt.ubicacion == 'fuera_de_sede'
                cierres.append((dia_operativo_de(k.fin_ts), k.custodio_conductor_id,
                                None, {'fuera_de_sede': fuera}))
        cierres.sort(key=lambda t: t[0])
        insp = sorted(((dia_operativo_de(i.respondida_ts), i.inspeccionada_por_usuario_id,
                        i.plantilla_id, i.segundos_llenado) for i in self.inspecciones),
                      key=lambda t: t[0])
        self._muestras = {'ruta': por_ruta, 'paradas': paradas, 'cierres': cierres,
                          'inspecciones': insp}
        return self._muestras


def _ventana(m, lista, desde, hasta):
    """Tramo de una lista ordenada por día con día en [desde, hasta]."""
    dias = m._dias_cache.get(id(lista))
    if dias is None:
        dias = m._dias_cache[id(lista)] = [t[0] for t in lista]
    return lista[bisect_left(dias, desde):bisect_right(dias, hasta)]


def _banderas_parada(m, rec):
    g = m.geo.get(rec.id)
    con_gps = g is not None and g.lat is not None
    rechazo = rec.estado_entrega in ('RECHAZADO', 'ENTREGADO_SIN_PAGO')
    prec = float(g.precision_m) if (con_gps and g.precision_m is not None) else None
    return {
        'motivo': rec.motivo_rechazo,
        'rechazo': rechazo,
        'sin_gps': not con_gps,
        'rechazo_sin_gps': rechazo and not con_gps,
        'sin_foto': not bool(rec.foto_entrega),
        'precision_anomala': prec is not None and prec > m.U['precision_anomala_m'],
        'descuento': float(rec.monto_descuento or 0) > 0,
    }


def referencia(m: Mundo, metrica, maestra_id, dia, excluir_conductor, p=90):
    """Mediana y percentil de los COMPAÑEROS (otros conductores) en la misma
    ruta maestra, en la ventana `[dia − ventana, dia]`.

    Una ruta sin maestra no tiene compañeros comparables: sale «sin base».
    """
    clave = (metrica, maestra_id, dia, excluir_conductor, p)
    if clave not in m._referencias:
        m._referencias[clave] = _referencia(m, metrica, maestra_id, dia, excluir_conductor, p)
    return m._referencias[clave]


def _referencia(m: Mundo, metrica, maestra_id, dia, excluir_conductor, p):
    U = m.U
    desde = dia - timedelta(days=U['ventana_pares_dias'])
    if maestra_id is None:
        return {'n': 0, 'mediana': None, 'p': None, 'base': 'sin_base',
                'pares': 'ruta sin maestra: no hay compañeros comparables'}
    lista = m.muestras()['ruta'][metrica].get(maestra_id, [])
    vals = [v for d, c, v in _ventana(m, lista, desde, dia) if c != excluir_conductor]
    n = len(vals)
    base = 'con_base' if n >= U['n_minimo_pares'] else 'sin_base'
    return {
        'n': n, 'mediana': mediana(vals), 'p': percentil(vals, p) if vals else None,
        'percentil': p, 'base': base,
        'pares': (f'otros conductores en la ruta {m.maestras.get(maestra_id, maestra_id)}, '
                  f'del {desde} al {dia}'),
    }


def _agregado(m: Mundo, muestra, bandera):
    """`[(día, {(conductor, maestra): (casos, con_bandera)})]` ordenado por día.

    La tasa se pide por cada conductor × día del resumen: sumar por día una
    sola vez convierte cada pedido en una pasada por los días de la ventana, no
    por cada parada. `bandera` es una clave de las banderas o `('motivo', X)`.
    """
    clave = (muestra, bandera)
    cache = m._agregados
    if clave in cache:
        return cache[clave]
    por_dia = {}
    for d, c, M, b in m.muestras()[muestra]:
        hit = (b['motivo'] == bandera[1]) if isinstance(bandera, tuple) else bool(b[bandera])
        dia = por_dia.setdefault(d, {})
        n, h = dia.get((c, M), (0, 0))
        dia[(c, M)] = (n + 1, h + (1 if hit else 0))
    cache[clave] = sorted(por_dia.items(), key=lambda t: t[0])
    return cache[clave]


def tasa_vs_pares(m: Mundo, conductor_id, desde, hasta, bandera, muestra='paradas'):
    """Cuántas veces le pasó `bandera` al conductor, contra cuántas se
    esperaban si le pasara lo que a sus compañeros **en las mismas rutas
    maestras** (o, para cierres de turno, lo que a los demás conductores).

    `esperado = Σ_maestra paradas_del_conductor × tasa_de_los_compañeros`.
    Solo entran las maestras con al menos `n_minimo_pares` casos de los
    compañeros; el resto se cuenta aparte, sin comparar.
    """
    U = m.U
    suyo, pares = {}, {}
    for _d, por_clave in _ventana(m, _agregado(m, muestra, bandera), desde, hasta):
        for (c, M), (n, h) in por_clave.items():
            destino = suyo if c == conductor_id else pares
            n0, h0 = destino.get(M, (0, 0))
            destino[M] = (n0 + n, h0 + h)
    observado = sum(h for _n, h in suyo.values())
    n_conductor = sum(n for n, _h in suyo.values())
    esperado, obs_base, n_base, n_pares, sin_base = 0.0, 0, 0, 0, 0
    for M, (n, h) in suyo.items():
        np_, hp = pares.get(M, (0, 0))
        if M is None and muestra != 'cierres':
            sin_base += n
            continue
        if np_ >= U['n_minimo_pares']:
            esperado += n * hp / np_
            obs_base += h
            n_base += n
            n_pares += np_
        else:
            sin_base += n
    base = 'con_base' if n_base >= U['n_minimo_conductor'] and n_pares else 'sin_base'
    senal = (base == 'con_base'
             and obs_base >= U['observados_minimos']
             and obs_base >= U['razon_vs_pares'] * esperado
             and (obs_base - esperado) / n_base >= U['diferencia_minima_puntos'])
    return {
        'observado': observado, 'n_conductor': n_conductor,
        'observado_comparable': obs_base, 'n_comparable': n_base,
        'esperado_pares': round(esperado, 1), 'n_pares': n_pares,
        'sin_base': sin_base, 'base': base, 'senal': senal,
        'ventana': {'desde': desde.isoformat(), 'hasta': hasta.isoformat()},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fuentes de eventos — el punto de extensión de las fases 1 y 3
# ─────────────────────────────────────────────────────────────────────────────

def _eventos_de_turno(m: Mundo, c, lo, hi) -> List[Evento]:
    """Recibo y entrega de turno (`flota_custodia`)."""
    uid = c.usuario_id
    out = []
    for k in m.custodias:
        if k.custodio_conductor_id != c.id:
            continue
        prev, nxt = m.vecinas(k)
        placa = m.placas.get(k.vehiculo_id, k.vehiculo_id)
        continua = (prev is not None and prev.custodio_conductor_id == c.id
                    and prev.fin_ts == k.inicio_ts)
        if lo <= k.inicio_ts < hi and not continua:
            out.append(Evento(
                'recibo_turno', k.inicio_ts, 'servidor', *_EN_LINEA,
                propio=(uid is not None and k.registrado_por_usuario_id == uid),
                fuente='flota_custodia.inicio_ts',
                detalle={'placa': placa, 'km': k.km_inicio,
                         'entregado_por': m.custodio_txt(prev),
                         'registrado_por_otra_persona': k.registrado_por_usuario_id != uid,
                         'linea_base': bool(k.linea_base)},
                ts_servidor=k.inicio_ts))
        if k.fin_ts is not None and lo <= k.fin_ts < hi:
            sigue = (nxt is not None and nxt.inicio_ts == k.fin_ts
                     and nxt.custodio_conductor_id == c.id)
            if sigue and nxt.ubicacion != 'fuera_de_sede':
                continue            # se re-declaró a sí mismo: no hubo entrega
            quien_registra = nxt.registrado_por_usuario_id if nxt is not None else None
            out.append(Evento(
                'entrega_turno', k.fin_ts, 'servidor', *_EN_LINEA,
                propio=(uid is not None and quien_registra == uid and not k.cierre_forzado),
                fuente='flota_custodia.fin_ts',
                detalle={'placa': placa, 'km': k.km_fin,
                         'km_turno': (k.km_fin - k.km_inicio) if k.km_fin is not None else None,
                         'queda': (nxt.ubicacion if nxt is not None else None),
                         'motivo_ubicacion': (nxt.ubicacion_motivo if nxt is not None else None),
                         'recibe': m.custodio_txt(nxt) if not sigue else None,
                         'cierre_forzado': bool(k.cierre_forzado),
                         'motivo_cierre_forzado': k.cierre_forzado_motivo},
                ts_servidor=k.fin_ts))
    return out


def _eventos_de_inspeccion(m: Mundo, c, lo, hi) -> List[Evento]:
    uid = c.usuario_id
    suyas = {k.id for k in m.custodias if k.custodio_conductor_id == c.id}
    out = []
    for i in m.inspecciones:
        if not (lo <= i.respondida_ts < hi):
            continue
        propio = uid is not None and i.inspeccionada_por_usuario_id == uid
        if not propio and i.custodia_id not in suyas:
            continue
        out.append(Evento(
            'preoperacional', i.respondida_ts, 'servidor', *_EN_LINEA, propio=propio,
            fuente='flota_inspeccion.respondida_ts',
            detalle={'placa': m.placas.get(i.vehiculo_id, i.vehiculo_id),
                     'veredicto': i.veredicto, 'segundos_llenado': i.segundos_llenado,
                     'items': i.items_esperados, 'sin_dato': i.items_sin_dato,
                     'plantilla_id': i.plantilla_id, 'inspeccion_id': i.id,
                     'hecha_por_otra_persona': not propio},
            ts_servidor=i.respondida_ts))
    return out


def _eventos_de_ruta(m: Mundo, c, lo, hi) -> List[Evento]:
    """Cargue, cierre del cargue, paradas, cierre y liquidación de SUS rutas."""
    out = []
    for rid, r in m.rutas.items():
        if r.conductor_id != c.id:
            continue
        h = m.hechos_ruta(rid)
        maestra = m.maestras.get(r.ruta_maestra_id) if r.ruta_maestra_id else None
        base = {'ruta_id': rid, 'maestra': maestra,
                'placa': m.placas.get(r.vehiculo_id) if r.vehiculo_id else None}
        b = m.bultos.get(rid)
        if b and b['primero'] and lo <= b['primero'] < hi:
            out.append(Evento(
                'cargue', b['primero'], 'servidor', *_EN_LINEA, propio=False,
                fuente='bultos.fecha_cargado', ts_fin=b['ultimo'],
                detalle={**base, 'bultos': b['bultos'],
                         'nota': 'lo escanea el muelle, no el conductor'},
                ts_servidor=b['primero'], ruta_id=rid))
        if r.fecha_cierre and lo <= r.fecha_cierre < hi:
            out.append(Evento(
                'cierre_cargue', r.fecha_cierre, 'servidor', *_EN_LINEA, propio=False,
                fuente='rutas_despacho.fecha_cierre',
                detalle={**base, 'paradas': b['paradas'] if b else None,
                         'nota': 'cierre del manifiesto en el muelle, no la salida física'},
                ts_servidor=r.fecha_cierre, ruta_id=rid))
        for p in h['paradas']:
            if not (lo <= p['ts'] < hi):
                continue
            rec = p['rec']
            tipo = 'parada_forzada' if p['auto'] else ('parada' if p['propio'] else 'parada_por_otro')
            g = m.geo.get(rec.id)
            t = m.tareas.get(rec.tarea_id, {})
            tel = m.telefono.get(rec.id, {})
            tel_geo = m.telefono_geo.get(rec.id, {})
            out.append(Evento(
                tipo, p['ts'], p['fuente'], p['confianza'], p['motivo'], propio=p['propio'],
                fuente=('recaudos_entrega.ts_dispositivo' if p['fuente'] == 'telefono'
                        else 'recaudos_entrega.fecha_creacion'),
                detalle={**base, 'cliente': t.get('cliente'), 'municipio': t.get('municipio'),
                         'pedido': t.get('pedido'), 'estado': rec.estado_entrega,
                         'motivo_rechazo': rec.motivo_rechazo, 'forma_pago': rec.forma_pago,
                         'monto_cobrado': float(rec.monto_cobrado or 0),
                         'monto_descuento': float(rec.monto_descuento or 0),
                         'motivo_descuento': rec.motivo_descuento,
                         'con_foto': bool(rec.foto_entrega),
                         'reconfirmada': rec.editado_en is not None,
                         'gps': (None if g is None else
                                 {'fuente': g.fuente, 'motivo_sin_dato': g.motivo_sin_dato,
                                  'precision_m': _redondo(g.precision_m)}),
                         'via_cola': tel.get('via_cola'),
                         'desfase_s': tel.get('ts_desfase_s'),
                         'gps_hora': _hora_local(tel_geo.get('pos_ts_dispositivo'))},
                ts_servidor=rec.fecha_creacion,
                lat=float(g.lat) if g is not None and g.lat is not None else None,
                lon=float(g.lon) if g is not None and g.lon is not None else None,
                precision_m=_redondo(g.precision_m) if g is not None else None,
                ruta_id=rid, recaudo_id=rec.id))
        if r.fecha_entregada and lo <= r.fecha_entregada < hi:
            forz = h['forzada']
            out.append(Evento(
                'cierre_ruta_forzado' if forz else 'cierre_ruta', r.fecha_entregada,
                'servidor', 'media', 'hora del servidor; no se guarda quién cerró la ruta',
                propio=False, fuente='rutas_despacho.fecha_entregada', detalle=dict(base),
                ts_servidor=r.fecha_entregada, ruta_id=rid))
        if r.liquidada_en and lo <= r.liquidada_en < hi:
            out.append(Evento(
                'liquidacion', r.liquidada_en, 'servidor', *_EN_LINEA, propio=False,
                fuente='rutas_despacho.liquidada_en', detalle=dict(base),
                ts_servidor=r.liquidada_en, ruta_id=rid))
    return out


def _eventos_de_tanqueo(m: Mundo, c, lo, hi) -> List[Evento]:
    uid = c.usuario_id
    if uid is None:
        return []
    return [Evento('tanqueo', l.ts, 'servidor', *_EN_LINEA, propio=True,
                   fuente='flota_lectura_odometro.ts (origen tanqueo)',
                   detalle={'placa': m.placas.get(l.vehiculo_id, l.vehiculo_id),
                            'km': l.valor_km},
                   ts_servidor=l.ts)
            for l in m.tanqueos if l.autor_usuario_id == uid and lo <= l.ts < hi]


#: El punto de extensión. Fase 1: `_eventos_de_jornada_evento` (tabla
#: `jornada_evento`: GPS al abrir ruta y parada, «salí», «volví»). Fase 3:
#: `_eventos_de_gps_vehiculo` (API del proveedor del GPS). Cada una devuelve
#: `Evento`s con su `fuente` y `confianza`; nada más cambia.
FUENTES_DE_EVENTOS = (
    _eventos_de_turno,
    _eventos_de_inspeccion,
    _eventos_de_ruta,
    _eventos_de_tanqueo,
)


def eventos_del_conductor(m: Mundo, c, lo, hi) -> List[Evento]:
    ev = []
    for fuente in FUENTES_DE_EVENTOS:
        ev += fuente(m, c, lo, hi)
    ev.sort(key=lambda e: (e.ts, _ORDEN.get(e.tipo, 99)))
    return ev


# ─────────────────────────────────────────────────────────────────────────────
# Cobertura, huecos e indicadores
# ─────────────────────────────────────────────────────────────────────────────

def _rutas_del_dia(m: Mundo, conductor_id, dia):
    if m._rutas_por_dia is None:
        m._rutas_por_dia = {}
        for rid in sorted(m.rutas):
            h = m.hechos_ruta(rid)
            m._rutas_por_dia.setdefault((h['ruta'].conductor_id, h['dia']), []).append(h)
    return m._rutas_por_dia.get((conductor_id, dia), [])


def _cobertura(eventos, rutas_dia):
    """Qué fracción de la evidencia esperable de un día existe.

    Cuatro anclas: apertura (recibió el vehículo o hizo el preoperacional),
    salida (cierre del cargue), paradas (confirmadas por él / paradas de sus
    rutas) y cierre (entregó el vehículo). Sin rutas, solo cuentan apertura y
    cierre. Es cobertura del REGISTRO, no del trabajo.
    """
    tipos_propios = {e.tipo for e in eventos if e.propio}
    anclas, faltan = {}, []
    anclas['apertura'] = 1.0 if tipos_propios & {'recibo_turno', 'preoperacional'} else 0.0
    if not anclas['apertura']:
        faltan.append('No hay recibo del vehículo ni preoperacional suyos en el sistema')
    if rutas_dia:
        anclas['salida'] = 1.0 if any(e.tipo == 'cierre_cargue' for e in eventos) else 0.0
        if not anclas['salida']:
            faltan.append('Ninguna de sus rutas cerró el cargue ese día')
        total = sum((m_b or 0) for m_b in (h.get('_paradas_total') for h in rutas_dia))
        propias = sum(len(h['propias']) for h in rutas_dia)
        if total:
            anclas['paradas'] = min(1.0, propias / total)
            if propias < total:
                faltan.append(f'Confirmó {propias} de {total} paradas de sus rutas')
    anclas['cierre'] = 1.0 if 'entrega_turno' in tipos_propios else 0.0
    if not anclas['cierre']:
        faltan.append('No hay registro de que entregó el vehículo')
    valor = sum(anclas.values()) / len(anclas) if anclas else 0.0
    return {'valor': round(valor, 2), 'anclas': anclas, 'faltan': faltan}


#: Estados de la jornada. `parcial`: hay registros suficientes entre el primero
#: y el último, pero falta la apertura o el cierre del día — se ve el tramo, no
#: la jornada. `no_reconstruible`: ni eso.
ESTADOS = ('reconstruida', 'parcial', 'no_reconstruible', 'sin_actividad')
CON_TRAMO = ('reconstruida', 'parcial')


def estado_de(eventos, cob, U):
    """`(estado, motivo)` de un día. Una sola definición: la usan el detalle,
    el resumen y la pantalla (que solo lee `estado`)."""
    propios = [e for e in eventos if e.propio]
    if not eventos:
        return 'sin_actividad', 'Ningún registro suyo ni de sus rutas ese día.'
    if len(propios) < 2 or cob['valor'] < U['cobertura_minima']:
        return 'no_reconstruible', (
            'Con la evidencia de ese día no se puede reconstruir la jornada: '
            + ('; '.join(cob['faltan']) or 'menos de dos registros propios') + '.')
    if not (cob['anclas']['apertura'] and cob['anclas']['cierre']):
        return 'parcial', (
            'Se ve el tramo entre su primer y su último registro, no la jornada: '
            + '; '.join(f for f in cob['faltan']
                        if 'preoperacional' in f or 'entregó el vehículo' in f) + '.')
    return 'reconstruida', None


def _categoria(a: Evento, b: Evento):
    if a.tipo == 'cierre_cargue' and b.es_parada and a.ruta_id == b.ruta_id:
        return 'salida', 'salida_min'
    if a.es_parada and b.es_parada and a.ruta_id == b.ruta_id:
        return 'entre_paradas', 'entre_paradas_min'
    if a.es_parada and b.tipo in ('cierre_ruta', 'entrega_turno'):
        return 'regreso', 'regreso_min'
    return 'otro', None


_TXT_CATEGORIA = {
    'salida': 'del cierre del cargue a la primera parada',
    'entre_paradas': 'entre dos paradas',
    'regreso': 'de la última parada al cierre',
    'otro': 'entre dos registros',
}


def _huecos(m: Mundo, conductor_id, dia, eventos):
    """Los tramos sin registro dentro de la jornada, con su referencia.

    La jornada va del primer al último gesto PROPIO. Dentro, cada par de
    registros consecutivos deja un tramo; lo que el cargue cubre no cuenta. La
    parte «no explicada» de un tramo es lo que **excede** la referencia —el p90
    de los compañeros en esa maestra, o el umbral fijo si no hay base—: se
    descuenta el tiempo que es normal, a favor de la persona.
    """
    U = m.U
    propios = [e for e in eventos if e.propio]
    if len(propios) < 2:
        return []
    ini, fin = propios[0].ts, propios[-1].ts
    marcas = [e for e in eventos if ini <= e.ts <= fin and e.tipo != 'liquidacion']
    cubiertos = [(e.ts, e.ts_fin) for e in eventos if e.ts_fin]
    out = []
    for a, b in zip(marcas, marcas[1:]):
        bruto = _minutos(a.ts, b.ts)
        cubierto = sum(max(0.0, _minutos(max(a.ts, x), min(b.ts, y)))
                       for x, y in cubiertos)
        efectivo = bruto - cubierto
        if efectivo < U['hueco_minimo_listado_min']:
            continue
        cat, metrica = _categoria(a, b)
        ref = None
        if metrica:
            ruta = m.rutas.get(a.ruta_id if a.ruta_id else b.ruta_id)
            ref = referencia(m, metrica, ruta.ruta_maestra_id if ruta else None, dia,
                             conductor_id)
        if ref and ref['base'] == 'con_base':
            limite, base = ref['p'], 'con_base'
        else:
            limite, base = U['hueco_sin_base_min'], 'sin_base'
        no_expl = max(0.0, efectivo - limite)
        conf = min((a.confianza, b.confianza), key=lambda x: _CONF_NUM[x])
        out.append({
            # Posición del evento que abre el tramo en la línea de tiempo: la
            # pantalla intercala el hueco ahí sin comparar horas.
            'despues_de': next(i for i, e in enumerate(eventos) if e is a),
            'desde': _hora_local(a.ts), 'hasta': _hora_local(b.ts),
            'desde_tipo': a.tipo, 'hasta_tipo': b.tipo,
            'categoria': cat, 'que_es': _TXT_CATEGORIA[cat],
            'minutos': round(efectivo), 'referencia_min': round(limite),
            'base': base, 'n_pares': ref['n'] if ref else 0,
            'pares': ref['pares'] if ref else 'sin métrica de compañeros para este tramo',
            'no_explicado_min': round(no_expl), 'confianza': conf,
            'ruta_id': a.ruta_id or b.ruta_id,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Señales (nivel CONDUCTOR). Las de vehículo son de la bandeja del encargado.
# ─────────────────────────────────────────────────────────────────────────────

def _senal(clave, titulo, texto, evidencia, comparacion, confianza='alta', nivel='revisar'):
    return {'clave': clave, 'titulo': titulo, 'nivel': nivel, 'texto': texto,
            'evidencia': evidencia, 'comparacion': comparacion, 'confianza': confianza,
            'que_hacer': QUE_HACER}


def _lectura_del_borde(m: Mundo, vehiculo_id, ts, km):
    """La lectura de odómetro que escribió el traspaso en ese borde de turno.

    `traspasar` escribe UNA lectura con `ts` = el instante del traspaso y el
    mismo km: es la que sostiene (o no) el número del turno. `None` si no está
    (un turno de antes de que el traspaso escribiera su lectura)."""
    if m._lecturas_borde is None:
        from flota.adaptadores.modelos import LecturaOdometro
        ids = {k.vehiculo_id for k in m.custodias}
        m._lecturas_borde = {}
        if ids:
            for l in LecturaOdometro.query.filter(
                    LecturaOdometro.vehiculo_id.in_(ids)).all():
                m._lecturas_borde.setdefault((l.vehiculo_id, l.ts, l.valor_km), l)
    return m._lecturas_borde.get((vehiculo_id, ts, km))


def _casos_km_entre_turnos(m: Mundo):
    """`[(antes, k, km, en_medio, veredicto)]`: el turno de conductor `k`
    recibió el vehículo con otros km que con los que lo entregó el turno de
    conductor anterior (`antes`), con una o más custodias de sede en medio.

    **El juicio es `senales.km_sin_explicar`, el mismo de la bandeja** (km en
    días sin ruta): con una de las dos lecturas en duda —o sin lectura— el
    tramo NO se juzga (`no_evaluable`, con su motivo). Hasta el 2026-09-24 la
    jornada restaba los dos km sin mirar si se les podía creer: un número
    tecleado sin foto salía como «km que no le tocan a ningún turno» al lado del
    nombre de dos conductores, mientras la bandeja lo declaraba «verificalo
    primero».

    Un traspaso directo conductor → conductor no entra: `traspasar` escribe el
    mismo km en el cierre y en la apertura, así que ahí la diferencia es cero
    por construcción y medirla no diría nada.
    """
    if m._casos_km is not None:
        return m._casos_km
    from flota.dominio import senales as dom_sen
    from flota.dominio.odometro import confianza_del_tramo

    casos = []
    for lista in m._por_vehiculo.values():
        ultimo = None           # índice del último turno de conductor
        for i, k in enumerate(lista):
            if not k.custodio_conductor_id:
                continue
            if ultimo is not None and ultimo < i - 1:
                antes = lista[ultimo]
                if antes.km_fin is not None:
                    km = k.km_inicio - antes.km_fin
                    a = _lectura_del_borde(m, k.vehiculo_id, antes.fin_ts, antes.km_fin)
                    b = _lectura_del_borde(m, k.vehiculo_id, k.inicio_ts, k.km_inicio)
                    if a is None or b is None:
                        ver = dom_sen.Veredicto(
                            dom_sen.NO_EVALUABLE,
                            'falta la lectura de odómetro de uno de los dos '
                            'traspasos: el tramo no se puede sostener')
                    else:
                        marca = confianza_del_tramo(a.a_dominio(), b.a_dominio())
                        ver = dom_sen.km_sin_explicar(
                            km=km, marca=marca,
                            # «desde cuántos km se marca» → tolerancia = uno menos
                            tolerancia=m.U['km_entre_turnos_min'] - 1)
                    if ver.estado != dom_sen.NORMAL:
                        casos.append((antes, k, km, lista[ultimo + 1:i], ver))
            ultimo = i
    m._casos_km = casos
    return casos


def _s_km_entre_turnos(ctx):
    """km que el vehículo sumó entre el turno de un conductor y el siguiente,
    con la sede como custodio: no le tocan a ningún turno. Aparece en el día
    de quien entregó ANTES y en el de quien recibió DESPUÉS, **sin nombrar a
    ninguno de los dos en el título ni en el texto**: los turnos van en la
    evidencia como contexto (regla 2 de flota). Un tramo en duda no es señal:
    va a `senales_no_evaluables` con su motivo."""
    from flota.dominio import senales as dom_sen

    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    lo, hi = inicio_del_dia_utc(dia), inicio_del_dia_utc(dia + timedelta(days=1))
    out = []
    for antes, k, km, medio, ver in _casos_km_entre_turnos(m):
        recibe_hoy = k.custodio_conductor_id == c.id and lo <= k.inicio_ts < hi
        entrego_hoy = (antes.custodio_conductor_id == c.id and antes.fin_ts is not None
                       and lo <= antes.fin_ts < hi)
        if not (recibe_hoy or entrego_hoy):
            continue
        placa = m.placas.get(k.vehiculo_id, k.vehiculo_id)
        if ver.estado == dom_sen.NO_EVALUABLE:
            ctx['no_evaluables'].append({
                'clave': 'km_entre_turnos', 'placa': placa,
                'titulo': 'Kilómetros entre dos turnos que no se pueden juzgar',
                'motivo': ver.motivo,
                'texto': (f'El {placa} marca {km} km entre la entrega '
                          f'({_cuando(antes.fin_ts)}) y el recibo siguiente '
                          f'({_cuando(k.inicio_ts)}), pero {ver.motivo}.')})
            continue
        en_medio = [{'custodio': m.custodio_txt(x), 'desde': _cuando(x.inicio_ts),
                     'ubicacion': x.ubicacion, 'km_inicio': x.km_inicio, 'km_fin': x.km_fin}
                    for x in medio]
        out.append(_senal(
            'km_entre_turnos', 'Kilómetros que no le tocan a ningún turno',
            (f'El {placa} sumó {km} km mientras lo tenía la sede: se entregó con '
             f'{antes.km_fin} km ({_cuando(antes.fin_ts)}) y el turno siguiente lo '
             f'recibió con {k.km_inicio} km ({_cuando(k.inicio_ts)}).'),
            [{'placa': placa, 'km': km, 'rol': 'recibió' if recibe_hoy else 'entregó antes',
              'entrego': m.nombre(antes.custodio_conductor_id), 'km_entrega': antes.km_fin,
              'entrega': _cuando(antes.fin_ts), 'recibio': m.nombre(k.custodio_conductor_id),
              'km_recibo': k.km_inicio, 'recibo': _cuando(k.inicio_ts), 'en_medio': en_medio}],
            {'referencia': 'un vehículo bajo custodia de la sede no debería sumar km',
             'base': 'regla', 'n': None}))
    return out


def _s_duracion_ruta(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    out = []
    for h in ctx['rutas_dia']:
        if h['duracion_h'] is None:
            continue
        ref = referencia(m, 'duracion_h', h['maestra_id'], dia, c.id)
        h['_ref_duracion'] = ref
        if ref['base'] != 'con_base' or h['duracion_h'] <= ref['p']:
            continue
        r = h['ruta']
        out.append(_senal(
            'duracion_ruta', 'Ruta más larga que la de sus compañeros',
            (f'La ruta {m.maestras.get(h["maestra_id"], "")} le tomó '
             f'{_num(h["duracion_h"])} h del cierre del cargue al cierre de la ruta; '
             f'a sus compañeros en esa ruta les toma {_num(ref["mediana"])} h '
             f'(el 90 % termina en {_num(ref["p"])} h o menos).'),
            [{'ruta_id': r.id, 'cierre_cargue': _hora_local(r.fecha_cierre),
              'cierre_ruta': _hora_local(r.fecha_entregada),
              'horas': round(h['duracion_h'], 2), 'paradas': len(h['paradas'])}],
            {'valor': round(h['duracion_h'], 2), 'mediana': _redondo(ref['mediana'], 2),
             'p90': _redondo(ref['p'], 2), 'n': ref['n'], 'base': ref['base'],
             'pares': ref['pares']},
            confianza='media'))
    return out


def _s_hueco(ctx):
    m, huecos = ctx['m'], ctx['huecos']
    if ctx['estado'] not in CON_TRAMO:
        return []
    total = sum(x['no_explicado_min'] for x in huecos)
    if total < m.U['no_explicado_min_para_senal']:
        return []
    usados = [x for x in huecos if x['no_explicado_min'] > 0]
    con_base = any(x['base'] == 'con_base' for x in usados)
    conf = min((x['confianza'] for x in usados), key=lambda v: _CONF_NUM[v])
    return [_senal(
        'tiempo_no_explicado', 'Tiempo que ningún registro explica',
        (f'{_duracion_txt(total)} de su jornada no quedan explicados por ningún registro, '
         f'descontando lo normal para sus compañeros. No dice qué hizo: dice que no '
         f'quedó registro.'),
        usados,
        {'valor': total, 'base': 'con_base' if con_base else 'sin_base',
         'n': max(x['n_pares'] for x in usados),
         'referencia': 'p90 de los compañeros en la misma maestra; sin base, '
                       f'{m.U["hueco_sin_base_min"]} min fijos'},
        confianza=conf, nivel='revisar' if con_base else 'observa')]


def _s_rafaga(ctx):
    """Varias paradas confirmadas en pocos minutos **según el teléfono**. Sin
    la hora del teléfono no se calcula: una ráfaga de sincronización y una de
    «confirmé todo junto» se ven iguales con la hora del servidor."""
    m = ctx['m']
    U = m.U
    if not m.columnas_telefono['recaudos_entrega']:
        return []
    paradas = [e for e in ctx['eventos'] if e.tipo == 'parada' and e.hora_fuente == 'telefono']
    out, i = [], 0
    ventana = timedelta(seconds=U['rafaga_ventana_s'])
    while i < len(paradas):
        j = i
        while j + 1 < len(paradas) and paradas[j + 1].ts - paradas[i].ts <= ventana:
            j += 1
        grupo = paradas[i:j + 1]
        if len(grupo) >= U['rafaga_min_paradas']:
            from app.services.geo_cliente import distancia_m
            puntos = [(e.lat, e.lon) for e in grupo if e.lat is not None]
            dist = (max(distancia_m(a[0], a[1], b[0], b[1])
                        for a in puntos for b in puntos) if len(puntos) >= 2 else None)
            out.append(_senal(
                'rafaga_de_confirmaciones', 'Varias paradas confirmadas a la vez',
                (f'Confirmó {len(grupo)} paradas en '
                 f'{max(1, round(_minutos(grupo[0].ts, grupo[-1].ts)))} min según la hora '
                 f'de su teléfono'
                 + (f', todas a menos de {round(dist)} m una de otra' if dist is not None else '')
                 + '. ¿Las confirmó en la puerta de cada cliente?'),
                [{'hora': _hora_local(e.ts), 'hora_gps': e.detalle.get('gps_hora'),
                  'cliente': e.detalle.get('cliente'), 'estado': e.detalle.get('estado'),
                  'lat': e.lat, 'lon': e.lon} for e in grupo],
                {'valor': len(grupo), 'ventana_s': U['rafaga_ventana_s'],
                 'distancia_max_m': _redondo(dist), 'base': 'regla', 'n': None},
                confianza=min((e.confianza for e in grupo), key=lambda v: _CONF_NUM[v])))
            i = j + 1
        else:
            i += 1
    return out


def _s_rechazos(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    desde = dia - timedelta(days=m.U['ventana_pares_dias'])
    out = []
    hoy = [e for e in ctx['eventos'] if e.tipo == 'parada']
    for motivo in MOTIVOS_VIGILADOS:
        del_dia = [e for e in hoy if e.detalle.get('motivo_rechazo') == motivo]
        if not del_dia:
            continue
        t = _tasa_motivo(m, c.id, desde, dia, motivo)
        if not t['senal']:
            continue
        out.append(_senal(
            f'rechazo_{motivo.lower()}', f'Rechazos por {TITULOS_MOTIVO[motivo]}',
            (f'En los últimos {m.U["ventana_pares_dias"]} días registró '
             f'{t["observado_comparable"]} paradas con «{TITULOS_MOTIVO[motivo]}» en '
             f'{t["n_comparable"]}; a sus compañeros en esas mismas rutas les pasaría '
             f'{_num(t["esperado_pares"])}.'),
            [{'hora': _hora_local(e.ts), 'cliente': e.detalle.get('cliente'),
              'municipio': e.detalle.get('municipio'), 'con_gps': e.lat is not None,
              'con_foto': e.detalle.get('con_foto')} for e in del_dia],
            t, confianza='alta'))
    return out


def _tasa_motivo(m, conductor_id, desde, hasta, motivo):
    """La tasa de un motivo de rechazo contra los compañeros."""
    return tasa_vs_pares(m, conductor_id, desde, hasta, ('motivo', motivo))


def _s_cruza_dia(ctx):
    m, dia = ctx['m'], ctx['dia']
    fin_dia = inicio_del_dia_utc(dia + timedelta(days=1))
    out = []
    for h in ctx['rutas_dia']:
        r = h['ruta']
        if not r.fecha_cierre:
            continue
        if r.fecha_entregada is not None and r.fecha_entregada < fin_dia:
            continue
        sigue = r.fecha_entregada is None
        out.append(_senal(
            'ruta_cruza_el_dia', 'Ruta que no se cerró el mismo día',
            (f'La ruta {m.maestras.get(h["maestra_id"], "sin maestra")} cerró el cargue a '
             f'las {_hora_local(r.fecha_cierre)} y '
             + ('sigue en tránsito.' if sigue else
                f'se cerró el {dia_operativo_de(r.fecha_entregada)} a las '
                f'{_hora_local(r.fecha_entregada)}.')
             + ' La mercancía y el dinero pasaron la noche fuera.'),
            [{'ruta_id': r.id, 'estado': _ESTADO_RUTA.get(r.estado, r.estado),
              'cierre_cargue': _cuando(r.fecha_cierre),
              'cierre_ruta': _cuando(r.fecha_entregada)}],
            {'base': 'regla', 'n': None,
             'referencia': 'una ruta sale y se cierra el mismo día'},
            nivel='observa'))
    return out


def _s_fuera_de_sede(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    hoy = [e for e in ctx['eventos'] if e.tipo == 'entrega_turno'
           and e.detalle.get('queda') == 'fuera_de_sede']
    if not hoy:
        return []
    desde = dia - timedelta(days=m.U['ventana_pares_dias'])
    t = tasa_vs_pares(m, c.id, desde, dia, 'fuera_de_sede', muestra='cierres')
    if not t['senal']:
        return []
    return [_senal(
        'fuera_de_sede_frecuente', 'Deja el vehículo fuera de sede más que los demás',
        (f'En los últimos {m.U["ventana_pares_dias"]} días dejó el vehículo fuera de sede '
         f'{t["observado_comparable"]} veces en {t["n_comparable"]} cierres de turno; '
         f'con la frecuencia de los demás conductores serían {_num(t["esperado_pares"])}.'),
        [{'hora': _hora_local(e.ts), 'placa': e.detalle.get('placa'),
          'motivo': e.detalle.get('motivo_ubicacion')} for e in hoy],
        {**t, 'pares': 'los demás conductores (la custodia no tiene ruta maestra)'})]


def _s_evidencia(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    rech = [e for e in ctx['eventos'] if e.tipo == 'parada'
            and e.detalle.get('estado') in ('RECHAZADO', 'ENTREGADO_SIN_PAGO')
            and e.lat is None]
    if not rech:
        return []
    desde = dia - timedelta(days=m.U['ventana_pares_dias'])
    t = tasa_vs_pares(m, c.id, desde, dia, 'rechazo_sin_gps')
    if not t['senal']:
        return []
    return [_senal(
        'rechazos_sin_ubicacion', 'Rechazos sin ubicación',
        (f'En los últimos {m.U["ventana_pares_dias"]} días {t["observado_comparable"]} de sus '
         f'paradas fueron rechazos sin ubicación del teléfono; con lo que les pasa a sus '
         f'compañeros en esas rutas serían {_num(t["esperado_pares"])}. Un rechazo sin '
         f'ubicación no se puede contrastar con el sitio del cliente.'),
        [{'hora': _hora_local(e.ts), 'cliente': e.detalle.get('cliente'),
          'estado': e.detalle.get('estado'), 'motivo_rechazo': e.detalle.get('motivo_rechazo'),
          'gps': e.detalle.get('gps')} for e in rech],
        t)]


def _s_inspeccion(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    U = m.U
    out = []
    desde = dia - timedelta(days=U['ventana_pares_dias'])
    for e in ctx['eventos']:
        if e.tipo != 'preoperacional' or not e.propio:
            continue
        seg = e.detalle['segundos_llenado']
        vals = [s for d, u, p, s in _ventana(m, m.muestras()['inspecciones'], desde, dia)
                if u != c.usuario_id and p == e.detalle['plantilla_id']]
        if len(vals) >= U['n_minimo_pares']:
            limite, base = percentil(vals, 10), 'con_base'
        else:
            limite, base = U['inspeccion_segundos_sin_base'], 'sin_base'
        if seg >= limite:
            continue
        out.append(_senal(
            'preoperacional_rapido', 'Preoperacional contestado muy rápido',
            (f'Contestó {e.detalle["items"]} ítems del {e.detalle["placa"]} en {seg} s. '
             + (f'El 90 % de sus compañeros tarda más de {limite} s.' if base == 'con_base'
                else f'Sin base de compañeros: se compara contra {limite} s fijos.')),
            [{'hora': _hora_local(e.ts), 'placa': e.detalle['placa'], 'segundos': seg,
              'veredicto': e.detalle['veredicto'], 'sin_dato': e.detalle['sin_dato']}],
            {'valor': seg, 'p10': limite if base == 'con_base' else None,
             'umbral': limite if base == 'sin_base' else None, 'n': len(vals), 'base': base,
             'pares': 'otros conductores, misma plantilla de inspección'},
            nivel='revisar' if base == 'con_base' else 'observa'))
    return out


def _s_descuentos(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    hoy = [e for e in ctx['eventos'] if e.tipo == 'parada'
           and (e.detalle.get('monto_descuento') or 0) > 0]
    if not hoy:
        return []
    desde = dia - timedelta(days=m.U['ventana_pares_dias'])
    t = tasa_vs_pares(m, c.id, desde, dia, 'descuento')
    if not t['senal']:
        return []
    return [_senal(
        'descuentos_en_la_puerta', 'Descuentos en la puerta más frecuentes',
        (f'En los últimos {m.U["ventana_pares_dias"]} días aplicó descuento en '
         f'{t["observado_comparable"]} de {t["n_comparable"]} paradas; a sus compañeros en '
         f'esas rutas les pasaría {_num(t["esperado_pares"])}. El descuento lo declara él '
         f'y lo confirma la oficina.'),
        [{'hora': _hora_local(e.ts), 'cliente': e.detalle.get('cliente'),
          'monto': e.detalle.get('monto_descuento'), 'motivo': e.detalle.get('motivo_descuento')}
         for e in hoy],
        t)]


def _s_liquidar(ctx):
    m, c, dia = ctx['m'], ctx['c'], ctx['dia']
    out = []
    for h in ctx['rutas_dia']:
        r = h['ruta']
        if not r.fecha_entregada or h['forzada']:
            continue
        ref = referencia(m, 'liquidar_h', h['maestra_id'], dia, c.id)
        if r.liquidada_en:
            horas, pendiente = h['liquidar_h'], False
        else:
            ahora = datetime.utcnow()
            horas, pendiente = (ahora - r.fecha_entregada).total_seconds() / 3600, True
        h['_liquidar'] = {'horas': _redondo(horas), 'pendiente': pendiente, 'ref': ref}
        if ref['base'] != 'con_base' or horas <= ref['p']:
            continue
        out.append(_senal(
            'tiempo_hasta_liquidar', 'La plata de la ruta tardó en liquidarse',
            ((f'La ruta sigue sin liquidar {horas:.0f} h después de cerrarse' if pendiente
              else f'La ruta se liquidó {horas:.0f} h después de cerrarse')
             + f'; en esa ruta el 90 % se liquida en {ref["p"]:.0f} h o menos. La '
               f'liquidación la hace la oficina, pero el efectivo lo trae el conductor.'),
            [{'ruta_id': r.id, 'cierre_ruta': _cuando(r.fecha_entregada),
              'liquidada': _cuando(r.liquidada_en), 'pendiente': pendiente}],
            {'valor': _redondo(horas), 'mediana': _redondo(ref['mediana']),
             'p90': _redondo(ref['p']), 'n': ref['n'], 'base': ref['base'],
             'pares': ref['pares']},
            nivel='observa'))
    return out


#: Señales de nivel conductor (contrato Fase 0). Las de VEHÍCULO —km en días sin
#: ruta, custodio distinto del conductor de la ruta, galones— son de la bandeja
#: del encargado y se unifican al integrar: no se reimplementan acá.
SENALES = (
    _s_km_entre_turnos, _s_duracion_ruta, _s_hueco, _s_rafaga, _s_rechazos,
    _s_cruza_dia, _s_fuera_de_sede, _s_evidencia, _s_inspeccion, _s_descuentos,
    _s_liquidar,
)


def _num(x):
    return f'{x:.1f}'.replace('.', ',') if x is not None else 'sin dato'


def _duracion_txt(minutos):
    m = int(round(minutos))
    return f'{m // 60} h {m % 60} min' if m >= 60 else f'{m} min'


# ─────────────────────────────────────────────────────────────────────────────
# La jornada — una política, una función
# ─────────────────────────────────────────────────────────────────────────────

def _jornada_de(m: Mundo, c, dia, eventos) -> dict:
    U = m.U
    rutas_dia = _rutas_del_dia(m, c.id, dia)
    for h in rutas_dia:
        b = m.bultos.get(h['ruta'].id)
        h['_paradas_total'] = b['paradas'] if b else len(h['paradas'])
    cob = _cobertura(eventos, rutas_dia)
    propios = [e for e in eventos if e.propio]
    estado, motivo = estado_de(eventos, cob, U)
    huecos = _huecos(m, c.id, dia, eventos) if estado in CON_TRAMO else []
    ctx = {'m': m, 'c': c, 'dia': dia, 'eventos': eventos, 'huecos': huecos,
           'rutas_dia': rutas_dia, 'estado': estado, 'no_evaluables': []}
    senales = []
    for s in SENALES:
        senales += s(ctx)

    paradas = [e for e in eventos if e.tipo == 'parada']
    if estado in CON_TRAMO:
        span = _minutos(propios[0].ts, propios[-1].ts)
        no_expl = sum(x['no_explicado_min'] for x in huecos)
        jornada = {'primer_evento': _hora_local(propios[0].ts),
                   'ultimo_evento': _hora_local(propios[-1].ts),
                   # Sin apertura o sin cierre, del primer al último registro
                   # no es la jornada: es el tramo que se alcanza a ver.
                   'horas': round(span / 60, 2) if estado == 'reconstruida' else None,
                   'tramo_observado_h': round(span / 60, 2),
                   'no_explicado_min': round(no_expl),
                   'pct_no_explicado': round(no_expl / span, 3) if span > 0 else None,
                   'por_que_no': motivo}
    else:
        jornada = {'primer_evento': _hora_local(propios[0].ts) if propios else None,
                   'ultimo_evento': _hora_local(propios[-1].ts) if propios else None,
                   'horas': None, 'tramo_observado_h': None, 'no_explicado_min': None,
                   'pct_no_explicado': None, 'por_que_no': motivo}
    rutas = []
    for h in rutas_dia:
        r = h['ruta']
        ref = h.get('_ref_duracion') or (referencia(m, 'duracion_h', h['maestra_id'], dia, c.id)
                                         if h['duracion_h'] is not None else None)
        refc = referencia(m, 'hora_cierre_min', h['maestra_id'], dia, c.id, p=50)
        rutas.append({
            'ruta_id': r.id, 'maestra': m.maestras.get(h['maestra_id']), 'estado': r.estado,
            'placa': m.placas.get(r.vehiculo_id) if r.vehiculo_id else None,
            'cierre_cargue': _hora_local(r.fecha_cierre),
            'cierre_cargue_pares': _hhmm(refc['mediana']), 'n_pares_cierre': refc['n'],
            'cierre_ruta': _hora_local(r.fecha_entregada), 'forzada': h['forzada'],
            'duracion_h': _redondo(h['duracion_h'], 2),
            'duracion_pares': (None if ref is None else
                               {'mediana': _redondo(ref['mediana'], 2),
                                'p90': _redondo(ref['p'], 2), 'n': ref['n'],
                                'base': ref['base']}),
            'paradas': h['_paradas_total'], 'paradas_propias': len(h['propias']),
            'liquidacion': h.get('_liquidar'),
        })
    por_motivo = {}
    for e in paradas:
        mot = e.detalle.get('motivo_rechazo')
        if mot:
            por_motivo[mot] = por_motivo.get(mot, 0) + 1
    evidencia = {
        'paradas': len(paradas),
        'sin_gps': sum(1 for e in paradas if e.lat is None),
        'rechazos_sin_gps': sum(1 for e in paradas if e.lat is None and
                                e.detalle.get('estado') in ('RECHAZADO', 'ENTREGADO_SIN_PAGO')),
        'sin_foto': sum(1 for e in paradas if not e.detalle.get('con_foto')),
        'precision_anomala': sum(1 for e in paradas if e.precision_m is not None
                                 and e.precision_m > U['precision_anomala_m']),
        'con_hora_telefono': sum(1 for e in paradas if e.hora_fuente == 'telefono'),
    }
    return {
        'dia': dia.isoformat(),
        'conductor': {'id': c.id, 'nombre': c.nombre, 'tiene_cuenta': c.usuario_id is not None},
        'estado': estado, 'motivo_estado': motivo,
        'cobertura': cob,
        'jornada': jornada,
        'eventos': [e.a_dict() for e in eventos],
        'huecos': huecos,
        'rutas': rutas,
        'paradas': {'propias': len(paradas),
                    'por_otro': sum(1 for e in eventos if e.tipo == 'parada_por_otro'),
                    'forzadas': sum(1 for e in eventos if e.tipo == 'parada_forzada'),
                    'rechazos_por_motivo': por_motivo,
                    'descuentos': {'n': sum(1 for e in paradas
                                            if (e.detalle.get('monto_descuento') or 0) > 0),
                                   'monto': round(sum(e.detalle.get('monto_descuento') or 0
                                                      for e in paradas), 2)},
                    'evidencia': evidencia},
        'senales': senales,
        # Lo que se miró y no se pudo juzgar (un tramo de km en duda): no es
        # «normal», y se dice (regla 14 de flota).
        'senales_no_evaluables': ctx['no_evaluables'],
    }


def _hora_del_telefono(m: Mundo) -> dict:
    cols = m.columnas_telefono
    disponible = bool(cols['recaudos_entrega'])
    return {
        'columnas': {t: list(v) for t, v in cols.items()},
        'disponible': disponible,
        'rafagas_calculadas': disponible,
        'declaracion': (
            'Las paradas que traen hora del teléfono se ubican a esa hora; las que no, '
            'a la hora del servidor, con confianza baja.' if disponible else
            'La base todavía no guarda la hora del teléfono: toda parada se ubica a la '
            'hora en que llegó al servidor, que sin señal es la de sincronizar. Por eso '
            'NO se calculan ráfagas (varias paradas confirmadas a la vez) ni se confía '
            'en los tramos entre paradas.'),
    }


def _envoltura(m: Mundo, **cuerpo) -> dict:
    U = dict(m.U)
    rechazados = U.pop('_rechazados', {})
    return {
        **cuerpo,
        'hora_del_telefono': _hora_del_telefono(m),
        'umbrales': U, 'umbrales_rechazados': rechazados,
        'no_puede_ver': list(NO_PUEDE_VER),
        # Las palabras de los motivos de rechazo salen del catálogo único
        # (`motivos_rechazo`), no de una copia en la pantalla.
        'motivos_rechazo': _motivos_en_palabras(),
        'fuentes_no_disponibles': m.fuentes_no_disponibles,
        'aviso_legal': AVISO_LEGAL,
        'procedencia': {
            'calculado': ahora_bogota().isoformat(),
            'base': 'tablas del WMS y de flota; cero llamadas a Siesa',
            'pares': (f'otros conductores en la misma ruta maestra, '
                      f'{U["ventana_pares_dias"]} días hacia atrás de cada día'),
        },
    }


def jornada(conductor_id: int, dia: date) -> Optional[dict]:
    """Línea de tiempo, indicadores y señales de un conductor en un día.
    `None` si el conductor no existe."""
    from app.models.conductor import Conductor
    c = db.session.get(Conductor, conductor_id)
    if c is None:
        return None
    U = umbrales()
    m = Mundo(dia - timedelta(days=U['ventana_pares_dias']), dia, U)
    lo, hi = inicio_del_dia_utc(dia), inicio_del_dia_utc(dia + timedelta(days=1))
    c = m.conductores[conductor_id]
    return _envoltura(m, **_jornada_de(m, c, dia, eventos_del_conductor(m, c, lo, hi)))


def resumen(desde: date, hasta: date) -> dict:
    """Por conductor, en el período: jornadas, horas, tiempo no explicado,
    rutas contra la mediana, rechazos contra sus compañeros, km entre turnos,
    cobertura y señales. Cada día sale de la MISMA función que el detalle."""
    U = umbrales()
    m = Mundo(desde - timedelta(days=U['ventana_pares_dias']), hasta, U)
    lo, hi = inicio_del_dia_utc(desde), inicio_del_dia_utc(hasta + timedelta(days=1))
    filas = []
    for cid in sorted(m.conductores, key=lambda i: (m.conductores[i].nombre or '', i)):
        c = m.conductores[cid]
        eventos = eventos_del_conductor(m, c, lo, hi)
        por_dia = {}
        for e in eventos:
            por_dia.setdefault(dia_operativo_de(e.ts), []).append(e)
        if not por_dia and not c.activo:
            continue
        dias = [_jornada_de(m, c, d, por_dia[d]) for d in sorted(por_dia)]
        filas.append(_fila_resumen(m, c, desde, hasta, dias))
    filas.sort(key=lambda f: (f['jornadas'] == 0, -f['senales_abiertas'], f['conductor']['nombre']))
    return _envoltura(m, desde=desde.isoformat(), hasta=hasta.isoformat(),
                      dias=(hasta - desde).days + 1, conductores=filas)


def _fila_resumen(m: Mundo, c, desde, hasta, dias) -> dict:
    rec = [d for d in dias if d['estado'] == 'reconstruida']
    con_tramo = [d for d in dias if d['estado'] in CON_TRAMO]
    horas = [d['jornada']['horas'] for d in rec]
    span_min = sum(d['jornada']['tramo_observado_h'] * 60 for d in con_tramo)
    no_expl = sum(d['jornada']['no_explicado_min'] for d in con_tramo)
    senales = [s for d in dias for s in d['senales']]
    por_clave = {}
    for s in senales:
        por_clave[s['clave']] = por_clave.get(s['clave'], 0) + 1
    km = sum(e['km'] for s in senales if s['clave'] == 'km_entre_turnos' for e in s['evidencia'])

    # Hora de cierre del cargue: la suya contra la de sus compañeros en las
    # mismas maestras, en el período.
    suyas, pares = [], []
    maestras = set()
    for rid, r in m.rutas.items():
        h = m.hechos_ruta(rid)
        if r.conductor_id == c.id and desde <= h['dia'] <= hasta and h['hora_cierre_min'] is not None:
            suyas.append(h['hora_cierre_min'])
            maestras.add(h['maestra_id'])
    for M in maestras - {None}:
        pares += [v for d, cid, v in _ventana(m, m.muestras()['ruta']['hora_cierre_min'].get(M, []),
                                              desde, hasta) if cid != c.id]
    rutas = [r for d in dias for r in d['rutas']]
    con_base = [r for r in rutas if r['duracion_pares'] and r['duracion_pares']['base'] == 'con_base']
    razones = [r['duracion_h'] / r['duracion_pares']['mediana'] for r in con_base
               if r['duracion_pares']['mediana']]
    rechazos = {mot: _tasa_motivo(m, c.id, desde, hasta, mot) for mot in MOTIVOS_VIGILADOS}
    return {
        'conductor': {'id': c.id, 'nombre': c.nombre, 'activo': bool(c.activo),
                      'tiene_cuenta': c.usuario_id is not None},
        'jornadas': len(dias), 'reconstruidas': len(rec),
        'parciales': sum(1 for d in dias if d['estado'] == 'parcial'),
        'no_reconstruibles': sum(1 for d in dias if d['estado'] == 'no_reconstruible'),
        'cobertura_media': (round(sum(d['cobertura']['valor'] for d in dias) / len(dias), 2)
                            if dias else None),
        'horas_jornada': {'mediana': _redondo(mediana(horas), 2), 'n': len(horas)},
        'no_explicado': {'minutos': round(no_expl) if con_tramo else None,
                         'pct': round(no_expl / span_min, 3) if span_min else None,
                         'n_jornadas': len(con_tramo)},
        'cierre_cargue': {'mediana': _hhmm(mediana(suyas)), 'n': len(suyas),
                          'pares_mediana': _hhmm(mediana(pares)), 'n_pares': len(pares),
                          'base': 'con_base' if len(pares) >= m.U['n_minimo_pares'] else 'sin_base'},
        'rutas': {'n': len(rutas), 'con_base': len(con_base),
                  'sobre_p90': sum(1 for r in con_base if r['duracion_h'] > r['duracion_pares']['p90']),
                  'razon_mediana': _redondo(mediana(razones), 2)},
        'paradas': {'propias': sum(d['paradas']['propias'] for d in dias),
                    'por_otro': sum(d['paradas']['por_otro'] for d in dias)},
        'rechazos': rechazos,
        'descuentos': tasa_vs_pares(m, c.id, desde, hasta, 'descuento'),
        'km_entre_turnos': {'km': km, 'casos': por_clave.get('km_entre_turnos', 0)},
        'senales_abiertas': len(senales), 'senales_por_clave': por_clave,
        'dias': [{'dia': d['dia'], 'estado': d['estado'], 'cobertura': d['cobertura']['valor'],
                  'primer_evento': d['jornada']['primer_evento'],
                  'ultimo_evento': d['jornada']['ultimo_evento'],
                  'horas': d['jornada']['horas'],
                  'tramo_observado_h': d['jornada']['tramo_observado_h'],
                  'no_explicado_min': d['jornada']['no_explicado_min'],
                  'paradas': d['paradas']['propias'], 'senales': len(d['senales'])}
                 for d in reversed(dias)],
    }
