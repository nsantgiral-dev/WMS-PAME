"""
La bandeja del encargado de flota: **lo de hoy, lo pendiente y lo raro**.

Lectura pura. No escribe nada, no llama a Siesa, no manda avisos.

## Por qué existe

El encargado entraba a una lista de 41 renglones de prosa («Salud de la flota»)
sin una placa ni un botón, y después a seis tarjetas con nueve botones iguales.
Para saber qué camión atender tenía que abrir los seis expedientes. Esta lectura
le contesta tres preguntas en un solo viaje a la base:

    hoy          una fila por vehículo activo: quién lo tiene, dónde, cuántos km
                 y cuánto se les cree, cómo salió la inspección de hoy, qué ruta
                 tiene, y un semáforo **con su porqué**
    pendientes   lo que alguien tiene que hacer, con placa y la acción
    senales      lo que no cuadra, con su evidencia — PROPONE, no culpa

## No calcula nada que ya exista

Cada número sale de la función que ya lo decide en otra pantalla (regla 0 del
WMS, corolario «una política, una función»):

| Qué | De dónde |
|---|---|
| papel vencido / por vencer / no encontrado | `MedidorSQL.documentos_por_vehiculo` |
| turnos sin fotos, cierres forzados | `MedidorSQL.custodias_por_vehiculo` |
| km en duda | `verificacion.pendientes` |
| km actual | `odometro.odometro_actual` |
| lecturas que siguen contando | `odometro.vigentes_tras_la_ultima_correccion` |
| confianza de un tramo | `odometro.confianza_del_tramo` |
| daño vencido, días abierto | `hallazgo.vencido`, `hallazgo.dias_transcurridos` |
| preventivo | `preventivo.diagnostico_de_la_flota` |
| rendimiento y ventanas | `gastos.rendimiento_publicable_de`, `costos.ventanas_lleno_a_lleno` |
| precio del galón | `costos.precio_por_galon` |
| día operativo | `app.utils.fecha.dia_operativo_de` |

Lo que sí decide esta capa —el semáforo y las cinco señales— vive en
`flota/dominio/senales.py`, puro y con sus umbrales publicados.

## Qué NO hace, a propósito

- **No nombra culpables.** Una señal dice a nombre de quién estaba el turno como
  contexto, nunca como veredicto (regla 2).
- **No convierte «no sé» en «normal».** Lo que no se pudo evaluar va a
  `senales_no_evaluables` con el motivo.
- **No incluye vehículos dados de baja.** Sus papeles vencidos no son trabajo.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from sqlalchemy import inspect as _inspect

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA, dia_operativo_de

#: Las tablas sin las cuales la bandeja no puede decir nada honesto. Si falta
#: una, la bandeja no se arma: una bandeja sin daños que dice «sin pendientes»
#: es evidencia falsa de seguridad (regla 5).
TABLAS_REQUERIDAS = (
    'vehiculos', 'conductores', 'rutas_despacho', 'flota_ficha_tecnica',
    'flota_documento_vehiculo', 'flota_lectura_odometro', 'flota_custodia',
    'flota_foto', 'flota_hallazgo', 'flota_inspeccion', 'flota_gasto',
    'flota_tanqueo', 'flota_plan_tarea', 'flota_orden_trabajo',
)


class BandejaNoDisponible(Exception):
    """Falta una tabla. Se declara cuál, no se contesta vacío."""


# ── Palabras en vez de códigos ───────────────────────────────────────────────

#: El nombre de cada papel sale de la política de salida: un solo texto para
#: el mismo SOAT en las tres pantallas.
from flota.dominio.salida import NOMBRE_PAPEL as _NOMBRE_PAPEL  # noqa: E402
NOMBRE_DOCUMENTO = {t: n for t, (n, _g) in _NOMBRE_PAPEL.items()}

ESTADO_RUTA = {
    'PROGRAMADO': 'programada',
    'EN_CARGUE': 'cargando',
    'EN_TRANSITO': 'en la calle',
    'ENTREGADA': 'entregada',
}

INSPECCION_TEXTO = {
    'apta': 'inspección de hoy: apta',
    'no_apta': 'inspección de hoy: NO apta',
    'incompleta': 'inspección de hoy: incompleta (no habilita salir)',
    'sin_hacer': 'sin inspección hoy',
}

CONFIANZA_TEXTO = {
    'verificada': 'verificado contra la foto',
    'declarada': 'declarado, sin verificar',
    'dudosa': 'EN DUDA — verificar contra la foto',
}


def _palabra(mapa: dict, codigo) -> str:
    """El nombre legible, o el código tal cual si no está en el mapa.

    Indexado con `in` y no con `.get(codigo, codigo)`: el trinquete 2 del
    módulo prohíbe el `get` con default, y acá mostrar el código crudo es la
    respuesta correcta —se ve, y alguien lo agrega al mapa—.
    """
    return mapa[codigo] if codigo in mapa else str(codigo)


def _tabla_existe(nombre: str) -> bool:
    return _inspect(db.engine).has_table(nombre)


def _local(ts: datetime) -> datetime:
    """Un instante UTC naive de la base, en hora de Bogotá."""
    return ts.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA)


def _cuando(ts: Optional[datetime], hoy) -> str:
    """«hoy a las 06:05», «ayer a las 18:40», «el 20/09 a las 07:12».

    La hora de la base es UTC; lo que alguien lee es Bogotá (regla 5 del WMS).
    """
    if ts is None:
        return 'sin hora registrada'
    local = _local(ts)
    hora = local.strftime('%H:%M')
    if local.date() == hoy:
        return f'hoy a las {hora}'
    if local.date() == hoy - timedelta(days=1):
        return f'ayer a las {hora}'
    return f'el {local.strftime("%d/%m")} a las {hora}'


def _dias(desde, hasta) -> List:
    """Los días operativos de `desde` a `hasta`, inclusive."""
    salida, d = [], desde
    while d <= hasta:
        salida.append(d)
        d += timedelta(days=1)
    return salida


def _num(valor, decimales: int = 2) -> str:
    """Un número para leer: sin notación científica, redondeado."""
    return f'{Decimal(valor):,.{decimales}f}'.replace(',', '_').replace(
        '.', ',').replace('_', '.')


# ── La lectura ───────────────────────────────────────────────────────────────

class _Mundo:
    """Todo lo que la bandeja lee, cargado una vez.

    Se carga completo y después se juzga en memoria: con seis vehículos es más
    barato que una consulta por pregunta, y sobre todo garantiza que las tres
    secciones hablan del MISMO instante — una bandeja cuyo «hoy» y cuyos
    «pendientes» se leyeron en dos momentos puede contradecirse.
    """

    def __init__(self, almacen_id: Optional[int], ahora: datetime):
        from app.models.almacen import Almacen
        from app.models.conductor import Conductor
        from app.models.ruta_despacho import RutaDespacho
        from app.models.ruta_maestra import RutaMaestra
        from app.models.usuario import Usuario
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import (Custodia, DocumentoVehiculo,
                                               FichaTecnica, Hallazgo,
                                               Inspeccion, LecturaOdometro,
                                               OrdenTrabajo)
        from flota.dominio.hallazgo import EstadoHallazgo

        self.ahora = ahora
        self.hoy = dia_operativo_de(ahora)

        todos = (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                 .order_by(Vehiculo.placa).all())
        self.custodias = Custodia.query.order_by(Custodia.inicio_ts,
                                                 Custodia.id).all()
        self.activa = {c.vehiculo_id: c for c in self.custodias
                       if c.fin_ts is None}

        # ── El filtro de almacén ──────────────────────────────────────────
        #
        # Un vehículo no tiene almacén en el maestro. Su sede de referencia es
        # la del ÚLTIMO turno que quedó en una sede. Un vehículo que nunca
        # pasó por una sede no se asigna a ninguna: con el filtro puesto queda
        # afuera y se cuenta, no se esconde.
        sede_de = {}
        for c in self.custodias:
            if c.custodio_sede_id is not None:
                sede_de[c.vehiculo_id] = c.custodio_sede_id
        self.filtro = None
        if almacen_id is None:
            self.vehiculos = todos
        else:
            self.vehiculos = [v for v in todos
                              if v.id in sede_de and sede_de[v.id] == almacen_id]
            self.filtro = {
                'almacen_id': almacen_id,
                'fuera_del_filtro': len(todos) - len(self.vehiculos),
                'sin_sede_conocida': sum(1 for v in todos if v.id not in sede_de),
                'regla': ('un vehículo pertenece a la sede donde quedó su '
                          'último turno de sede; uno que nunca pasó por una '
                          'sede no pertenece a ninguna'),
            }
        self.ids = {v.id for v in self.vehiculos}
        self.placas = {v.placa for v in self.vehiculos}

        self.conductores = {c.id: c for c in Conductor.query.all()}
        self.sedes = {a.id: a for a in Almacen.query.all()}
        self.usuarios = {u.id: u for u in Usuario.query.all()}
        self.maestras = {r.id: r for r in RutaMaestra.query.all()}
        self.fichas = {f.vehiculo_id: f for f in FichaTecnica.query.all()}

        self.docs = defaultdict(list)
        for d in DocumentoVehiculo.query.all():
            self.docs[d.vehiculo_id].append(d)

        self.ordenes = defaultdict(list)
        for o in OrdenTrabajo.query.all():
            self.ordenes[o.vehiculo_id].append(o)

        self.lecturas = defaultdict(list)
        for l in (LecturaOdometro.query
                  .order_by(LecturaOdometro.ts, LecturaOdometro.id).all()):
            self.lecturas[l.vehiculo_id].append(l)

        self.danos = defaultdict(list)
        for h in (Hallazgo.query.filter_by(estado=EstadoHallazgo.ABIERTO)
                  .order_by(Hallazgo.fecha_limite, Hallazgo.id).all()):
            self.danos[h.vehiculo_id].append(h)

        self.inspeccion_hoy = {}
        for i in (Inspeccion.query.filter(Inspeccion.dia == self.hoy)
                  .order_by(Inspeccion.respondida_ts, Inspeccion.id).all()):
            self.inspeccion_hoy[i.vehiculo_id] = i      # la última del día

        # Las rutas, con el día en que ocurrieron (`salida.dia_de_ruta`: el
        # mismo criterio con el que el despacho decide si «sale hoy»).
        from flota.adaptadores.salida import dia_de_ruta
        self.rutas = RutaDespacho.query.order_by(RutaDespacho.id).all()
        self.dia_ruta = {}
        for r in self.rutas:
            d = dia_de_ruta(r)
            if d is not None:
                self.dia_ruta[r.id] = d
        self.dias_con_ruta = defaultdict(set)
        self.rutas_por_dia = defaultdict(list)
        self.dias_con_ruta_sin_placa = set()
        for r in self.rutas:
            if r.id not in self.dia_ruta:
                continue
            d = self.dia_ruta[r.id]
            if r.vehiculo_id is None:
                self.dias_con_ruta_sin_placa.add(d)
            else:
                self.dias_con_ruta[r.vehiculo_id].add(d)
                self.rutas_por_dia[(r.vehiculo_id, d)].append(r)

    # ── Lectores pequeños ────────────────────────────────────────────────

    def nombre_conductor(self, conductor_id) -> str:
        if conductor_id is None:
            return 'sin conductor'
        if conductor_id in self.conductores:
            return self.conductores[conductor_id].nombre
        return f'conductor #{conductor_id} (ya no existe)'

    def nombre_usuario(self, usuario_id) -> str:
        if usuario_id is None:
            return 'alguien sin registrar'
        if usuario_id in self.usuarios:
            return self.usuarios[usuario_id].nombre
        return f'usuario #{usuario_id} (ya no existe)'

    def nombre_sede(self, sede_id) -> str:
        if sede_id in self.sedes:
            s = self.sedes[sede_id]
            return f'{s.codigo} · {s.nombre}'
        return 'una sede que ya no existe'

    def vigentes(self, vehiculo_id) -> list:
        """Las lecturas que siguen contando: una corrección deja atrás las
        anteriores. La política es del dominio; acá solo se aplica el corte."""
        from flota.dominio.odometro import vigentes_tras_la_ultima_correccion

        filas = self.lecturas[vehiculo_id]
        _v, desde = vigentes_tras_la_ultima_correccion(
            [l.a_dominio() for l in filas])
        return [l for l in filas if desde is None or l.ts >= desde]

    def turnos_entre(self, vehiculo_id, desde_ts, hasta_ts) -> List[str]:
        """A nombre de quién estuvo el vehículo entre dos instantes. Contexto,
        no acusación: se usa para decir «el turno estaba a nombre de…»."""
        nombres = []
        for c in self.custodias:
            if c.vehiculo_id != vehiculo_id or c.custodio_tipo != 'conductor':
                continue
            fin = c.fin_ts or self.ahora
            if c.inicio_ts <= hasta_ts and fin >= desde_ts:
                n = self.nombre_conductor(c.custodio_conductor_id)
                if n not in nombres:
                    nombres.append(n)
        return nombres


# ── Hoy ──────────────────────────────────────────────────────────────────────

def _custodio(m: _Mundo, v) -> Optional[dict]:
    c = m.activa[v.id] if v.id in m.activa else None
    if c is None:
        return None
    if c.custodio_estado == 'pendiente_sede':
        quien, tipo = 'una sede que no está en el maestro', 'pendiente_sede'
    elif c.custodio_tipo == 'conductor':
        quien, tipo = m.nombre_conductor(c.custodio_conductor_id), 'conductor'
    else:
        quien, tipo = m.nombre_sede(c.custodio_sede_id), 'sede'
    prefijo = 'turno de' if tipo == 'conductor' else 'en custodia de'
    return {
        'custodia_id': c.id,
        'tipo': tipo,
        'nombre': quien,
        'desde': c.inicio_ts,
        'texto': f'{prefijo} {quien} desde {_cuando(c.inicio_ts, m.hoy)}',
        'linea_base': bool(c.linea_base),
    }


def _donde(m: _Mundo, v) -> dict:
    c = m.activa[v.id] if v.id in m.activa else None
    if c is None:
        return {'codigo': 'sin_dato', 'texto': 'no se sabe: nadie tiene el turno'}
    if c.ubicacion == 'fuera_de_sede':
        return {'codigo': 'fuera_de_sede',
                'texto': 'fuera de sede — ' + (c.ubicacion_motivo
                                               or 'sin motivo escrito')}
    if c.ubicacion == 'taller':
        return {'codigo': 'taller', 'texto': 'en el taller'}
    if c.custodio_estado == 'pendiente_sede':
        return {'codigo': 'pendiente_sede',
                'texto': 'en una sede que no está en el maestro'}
    if c.custodio_tipo == 'sede':
        return {'codigo': 'sede', 'texto': 'en ' + m.nombre_sede(c.custodio_sede_id)}
    return {'codigo': 'con_el_conductor',
            'texto': 'con el conductor (no se registra dónde)'}


def _km(m: _Mundo, v, dudosas_por_placa: Dict[str, int]) -> dict:
    from flota.dominio import odometro as dom_odo
    from flota.dominio.valores import SIN_DATO

    filas = m.lecturas[v.id]
    valor = dom_odo.odometro_actual([l.a_dominio() for l in filas])
    if valor is SIN_DATO:
        return {'valor': str(SIN_DATO), 'confianza': str(SIN_DATO), 'ts': None,
                'en_duda': False,
                'texto': 'sin ninguna lectura: no se sabe cuántos km tiene'}
    ts_max = max(l.ts for l in filas)
    ultima = next(l for l in filas if l.ts == ts_max and l.valor_km == valor)
    en_duda = v.placa in dudosas_por_placa
    return {
        'valor': valor,
        'confianza': ultima.confianza,
        'ts': ultima.ts,
        'en_duda': en_duda,
        'texto': (f'{_num(valor, 0)} km · '
                  f'{_palabra(CONFIANZA_TEXTO, ultima.confianza)} · '
                  f'{_cuando(ultima.ts, m.hoy)}'),
    }


def _inspeccion(m: _Mundo, v) -> dict:
    from flota.adaptadores.salida import inspeccion_de_fila

    i = m.inspeccion_hoy[v.id] if v.id in m.inspeccion_hoy else None
    estado = inspeccion_de_fila(i)
    return {'estado': estado, 'texto': INSPECCION_TEXTO[estado],
            'inspeccion_id': i.id if i else None}


def _rutas_hoy(m: _Mundo, v) -> List[dict]:
    salida = []
    clave = (v.id, m.hoy)
    for r in (m.rutas_por_dia[clave] if clave in m.rutas_por_dia else []):
        maestra = (m.maestras[r.ruta_maestra_id].nombre
                   if r.ruta_maestra_id in m.maestras else 'sin ruta maestra')
        salida.append({
            'ruta_id': r.id,
            'nombre': maestra,
            'conductor': m.nombre_conductor(r.conductor_id),
            'estado': r.estado,
            'texto': (f'{maestra} con {m.nombre_conductor(r.conductor_id)} · '
                      f'{_palabra(ESTADO_RUTA, r.estado)}'),
        })
    return salida


def _ficha_estado(m: _Mundo, v) -> str:
    from flota.adaptadores.salida import ficha_de
    return ficha_de(m.fichas[v.id] if v.id in m.fichas else None)[0]


def _hechos(m: _Mundo, v, *, km_conocido: bool, km_dudoso: bool,
            prev_diag: list, sale_hoy: bool):
    """Los hechos de salida de un vehículo, con las MISMAS funciones que usa
    `salida.hechos_de_vehiculo` (el despacho y el conductor). Leer en bloque
    cambia las consultas, no el significado."""
    from flota.adaptadores import salida as ad
    from flota.dominio import salida as dom_sal

    c = m.activa[v.id] if v.id in m.activa else None
    tipo, custodio, fuera = ad.custodia_de(c)
    vencidas, por_vencer = ad.preventivo_de_diagnostico(prev_diag)
    estado_ficha, falta = ad.ficha_de(m.fichas[v.id] if v.id in m.fichas else None)
    b, ve, p = ad.danos_de_filas(m.danos[v.id], m.ahora)
    return dom_sal.Hechos(
        hoy=m.hoy, papeles=ad.papeles_de_filas(m.docs[v.id], m.hoy),
        danos_bloqueantes=b, danos_vencidos=ve, danos_en_plazo=p,
        inspeccion=_inspeccion(m, v)['estado'], sale_hoy=sale_hoy,
        preventivo_vencidas=vencidas, preventivo_por_vencer=por_vencer,
        ot_abiertas=ad.ot_abiertas_de(m.ordenes[v.id]),
        km_conocido=km_conocido, km_dudoso=km_dudoso,
        custodia=tipo, custodio_conductor_id=custodio,
        # La bandeja no compara el turno con la ruta en el semáforo: eso es la
        # señal `turno_de_la_ruta`, con su contexto.
        conductor_de_la_ruta=None,
        fuera_de_sede=fuera, ficha=estado_ficha, ficha_falta=falta)


# ── Pendientes ───────────────────────────────────────────────────────────────

def _pendiente(*, clase, placa, urgencia, texto, detalle, accion, desde=None,
               **extra) -> dict:
    fila = {'clase': clase, 'placa': placa, 'urgencia': urgencia,
            'texto': texto, 'detalle': detalle, 'accion': accion,
            'desde': desde}
    fila.update(extra)
    return fila


#: Qué se aconseja según el papel más grave del vehículo.
_CONSEJO_PAPEL = {
    'vencido': 'Cargá el nuevo. Con el papel vencido el vehículo no debería salir.',
    'no_encontrado': 'No es lo mismo que vencido: no se sabe si existe. Buscalo y cargalo.',
    'sin_cargar': 'Mientras no se cargue, no se sabe si está al día.',
    'por_vencer': 'Sacá la cita o compralo antes de que venza.',
}


def _pendientes(m: _Mundo, medidor, dudosas, diag_prev,
                forzados: List[dict]) -> List[dict]:
    """Lo que alguien tiene que hacer, con placa y acción.

    **El nivel y el texto de todo lo que es del vehículo salen de la política
    de salida** (`flota/dominio/salida.py`): el SOAT vencido se dice igual acá,
    en el semáforo, al despachar y en el teléfono del conductor. Lo que es de un
    TURNO (cierre forzado, fotos que faltan, despacho con advertencias) va con
    `salida.NIVEL_TURNO_A_REVISAR`.
    """
    from flota.adaptadores import salida as ad
    from flota.dominio import salida as dom_sal
    from flota.dominio.hallazgo import dias_transcurridos, vencido
    from flota.dominio.senales import VENTANA_RECIENTE_DIAS

    salida = []
    limite_reciente = m.ahora - timedelta(days=VENTANA_RECIENTE_DIAS)
    por_id = {v.id: v for v in m.vehiculos}
    placa_de = {v.id: v.placa for v in m.vehiculos}

    # 1 · Papeles — UNO por vehículo, con sus renglones (los sin cargar juntos)
    for v in m.vehiculos:
        motivos = [x for x in (dom_sal.motivo_papel(p, m.hoy)
                               for p in ad.papeles_de_filas(m.docs[v.id], m.hoy))
                   if x is not None]
        if not motivos:
            continue
        motivos.sort(key=lambda x: dom_sal.orden_de_nivel(x.nivel))
        lineas = dom_sal.lineas(motivos)
        peor = motivos[0].clave
        consejo = next(c for sufijo, c in (
            ('_vencido', _CONSEJO_PAPEL['vencido']),
            ('_no_encontrado', _CONSEJO_PAPEL['no_encontrado']),
            ('_sin_registro', _CONSEJO_PAPEL['sin_cargar']),
            ('_por_vencer', _CONSEJO_PAPEL['por_vencer'])) if peor.endswith(sufijo))
        salida.append(_pendiente(
            clase='documento', placa=v.placa, urgencia=dom_sal.color_de(motivos),
            texto=' · '.join(l['texto'] for l in lineas), detalle=consejo,
            accion={'tipo': 'expediente', 'pestana': 'documentos'},
            lineas=[l['texto'] for l in lineas]))

    # 2 · Daños — la cola de decisiones de toda la flota
    for v in m.vehiculos:
        for h in m.danos[v.id]:
            dom = h.a_dominio()
            es_vencido = vencido(dom, m.ahora)
            dias = dias_transcurridos(dom, m.ahora)
            salida.append(_pendiente(
                clase='dano', placa=v.placa,
                urgencia=dom_sal.nivel_de_dano(criticidad=h.criticidad,
                                               vencido=es_vencido),
                texto=h.descripcion,
                detalle=(f'{h.criticidad} · lleva {dom_sal.plural(dias, "día", "días")} · '
                         + ('VENCIDO' if es_vencido
                            else f'límite {_cuando(h.fecha_limite, m.hoy)}')
                         + (f' · aplazado {dom_sal.plural(h.aplazado_veces, "vez", "veces")}'
                            if h.aplazado_veces else '')
                         + (' · preexistente (no le cuenta a nadie)'
                            if h.linea_base else '')),
                accion={'tipo': 'decidir_dano', 'hallazgo_id': h.id,
                        'pestana': 'danos'},
                desde=h.reportado_ts, hallazgo_id=h.id,
                criticidad=h.criticidad, vencido=es_vencido,
                dias_abierto=dias, aplazado_veces=h.aplazado_veces))

    # 3 · Kilometrajes en duda — UNO por placa («GAL001: 8 lecturas en duda»)
    por_placa = defaultdict(list)
    for l, placa in dudosas:
        if placa in m.placas:
            por_placa[placa].append(l)
    nivel_km = dom_sal.motivos_de_km(True, True)[0].nivel
    for placa in sorted(por_placa):
        filas = sorted(por_placa[placa], key=lambda l: (l.ts, l.id))
        ultima = filas[-1]
        motivo = ultima.motivo_dudosa or 'sin motivo escrito'
        salida.append(_pendiente(
            clase='km_dudoso', placa=placa, urgencia=nivel_km,
            texto=(dom_sal.plural(len(filas), 'lectura de kilometraje en duda',
                                  'lecturas de kilometraje en duda')),
            detalle=(f'La última: {_num(ultima.valor_km, 0)} km, registrada '
                     f'{_cuando(ultima.ts, m.hoy)} · {motivo}'),
            accion={'tipo': 'verificar_km', 'lectura_id': ultima.id},
            desde=filas[0].ts, lectura_id=ultima.id,
            lectura_ids=[l.id for l in filas]))

    # 4 y 5 · Turnos: cierres forzados recientes y turnos sin fotos de inicio
    custodia_por_id = {c.id: c for c in m.custodias}
    for f in (medidor.custodias_por_vehiculo() or []):
        c = custodia_por_id[f['custodia_id']]
        if c.vehiculo_id not in m.ids:
            continue
        a_nombre = (m.nombre_conductor(c.custodio_conductor_id)
                    if c.custodio_tipo == 'conductor' else 'la sede')
        if f['cierre_forzado'] and c.fin_ts is not None \
                and c.fin_ts >= limite_reciente:
            salida.append(_pendiente(
                clase='cierre_forzado', placa=f['placa'],
                urgencia=dom_sal.NIVEL_TURNO_A_REVISAR,
                texto=(f'turno de {a_nombre} cerrado a la fuerza por '
                       f'{m.nombre_usuario(c.cierre_forzado_por_usuario_id)} '
                       f'{_cuando(c.fin_ts, m.hoy)}'),
                detalle='Motivo: ' + (c.cierre_forzado_motivo or 'sin motivo')
                        + '. Sin fotos de cierre: el turno siguiente arrancó '
                          'sin nada con qué comparar.',
                accion={'tipo': 'fotos_turno', 'custodia_id': c.id},
                desde=c.fin_ts, custodia_id=c.id))
        if f['mitad_incompleta'] == 'inicio' and (
                c.fin_ts is None or c.inicio_ts >= limite_reciente):
            salida.append(_pendiente(
                clase='turno_sin_fotos', placa=f['placa'],
                urgencia=dom_sal.NIVEL_TURNO_A_REVISAR,
                texto=(f'turno de {a_nombre} desde {_cuando(c.inicio_ts, m.hoy)}'
                       f' sin fotos de inicio ({f["fotos"]} de '
                       f'{f["fotos_exigidas"]})'),
                detalle=('Sin la línea base de cómo lo recibió, un golpe nuevo '
                         'no se puede ubicar en ningún turno.'),
                accion={'tipo': 'fotos_turno', 'custodia_id': c.id},
                desde=c.inicio_ts, custodia_id=c.id))

    # 6 · Despachos que salieron reconociendo advertencias de flota (el FORZAR
    # del muelle). control_flota no ve 📈: si no está acá, no lo ve nadie de
    # flota.
    for f in forzados:
        if f['vehiculo_id'] not in m.ids:
            continue
        textos = [a['texto'] for a in f['advertencias'] if 'texto' in a] \
            or [str(c) for c in f['claves']]
        salida.append(_pendiente(
            clase='despacho_forzado', placa=placa_de[f['vehiculo_id']],
            urgencia=dom_sal.NIVEL_TURNO_A_REVISAR,
            texto=(f'salió {_cuando(f["ts"], m.hoy)} con '
                   + dom_sal.plural(len(textos), 'advertencia', 'advertencias')
                   + ': ' + '; '.join(textos)),
            detalle=(f'Lo autorizó {m.nombre_usuario(f["usuario_id"])} · ruta '
                     f'#{f["ruta_id"]} · motivo: {f["motivo"] or "sin motivo"}'),
            accion={'tipo': 'expediente', 'pestana': 'resumen'},
            desde=f['ts'], ruta_id=f['ruta_id']))

    # 7 · Ficha
    for v in m.vehiculos:
        estado, falta = ad.ficha_de(m.fichas[v.id] if v.id in m.fichas else None)
        for mo in dom_sal.motivos_de_ficha(estado, falta):
            salida.append(_pendiente(
                clase='ficha', placa=v.placa, urgencia=mo.nivel,
                texto=('sin ficha técnica: crear la primera' if estado == 'sin_ficha'
                       else mo.texto),
                detalle=('Sin ficha no hay capacidad de tanque, ni posiciones '
                         'de llanta, ni preventivo.' if estado == 'sin_ficha'
                         else 'Completala en la pestaña Ficha del expediente.'),
                accion={'tipo': 'expediente', 'pestana': 'ficha'}))

    # 8 · Preventivo vencido
    from flota.dominio.preventivo import EstadoTarea
    for d in diag_prev:
        if d['vehiculo_id'] not in m.ids or d['estado'] != EstadoTarea.VENCIDA:
            continue
        (tarea,), _n = ad.preventivo_de_diagnostico([d])
        mo = dom_sal.motivo_preventivo_vencido(tarea)
        salida.append(_pendiente(
            clase='preventivo', placa=por_id[d['vehiculo_id']].placa,
            urgencia=mo.nivel, texto=mo.texto,
            detalle=(f'tocaba a los {_num(d["proximo_km"], 0)} km'
                     if isinstance(d['proximo_km'], int) else
                     'vencido según el plan'),
            accion={'tipo': 'expediente', 'pestana': 'preventivo'},
            plan_id=d['plan_id']))

    orden_clase = {c: i for i, c in enumerate((
        'dano', 'documento', 'preventivo', 'despacho_forzado', 'cierre_forzado',
        'turno_sin_fotos', 'km_dudoso', 'ficha'))}
    salida.sort(key=lambda p: (dom_sal.orden_de_nivel(p['urgencia']),
                               orden_clase[p['clase']], p['placa']))
    return salida


# ── Señales ──────────────────────────────────────────────────────────────────

class _Recolector:
    def __init__(self):
        self.senales: List[dict] = []
        self._no_eval: Dict[tuple, dict] = {}

    def senal(self, **fila):
        self.senales.append(fila)

    def no_evaluable(self, clase, placa, motivo):
        clave = (clase, placa, motivo)
        if clave not in self._no_eval:
            self._no_eval[clave] = {'clase': clase, 'placa': placa,
                                    'motivo': motivo, 'casos': 0}
        self._no_eval[clave]['casos'] += 1

    def no_evaluables(self) -> List[dict]:
        return sorted(self._no_eval.values(),
                      key=lambda f: (f['clase'], f['placa'] or '', f['motivo']))


def _senales_km_sin_ruta(m: _Mundo, r: _Recolector):
    from flota.dominio import senales as dom
    from flota.dominio.odometro import confianza_del_tramo

    desde_dia = m.hoy - timedelta(days=dom.VENTANA_SENALES_DIAS)
    for v in m.vehiculos:
        if not m.dias_con_ruta[v.id]:
            if m.lecturas[v.id]:
                r.no_evaluable('km_sin_ruta', v.placa,
                               'ninguna ruta registra esta placa: no hay días '
                               'de ruta contra los cuales cruzar el kilometraje')
            continue
        filas = m.vigentes(v.id)
        for a, b in zip(filas, filas[1:]):
            dia_b = dia_operativo_de(b.ts)
            if dia_b < desde_dia:
                continue
            dias = _dias(dia_operativo_de(a.ts), dia_b)
            km = b.valor_km - a.valor_km
            ver = dom.km_sin_ruta(
                km=km, dias=dias, dias_con_ruta=m.dias_con_ruta[v.id],
                marca=confianza_del_tramo(a.a_dominio(), b.a_dominio()))
            if ver.estado == dom.NO_EVALUABLE:
                r.no_evaluable('km_sin_ruta', v.placa, ver.motivo)
                continue
            if ver.estado != dom.SENAL:
                continue
            if any(d in m.dias_con_ruta_sin_placa for d in dias):
                # Ese día salió una ruta sin placa: pudo ser este vehículo.
                # Regla 0 — ante la duda, no se señala.
                r.no_evaluable('km_sin_ruta', v.placa,
                               'en esos días salió una ruta sin placa: pudo '
                               'hacerla este vehículo')
                continue
            turnos = m.turnos_entre(v.id, a.ts, b.ts)
            r.senal(
                clase='km_sin_ruta', placa=v.placa,
                titulo=f'{_num(km, 0)} km en días sin ruta',
                texto=(f'Entre {_cuando(a.ts, m.hoy)} y {_cuando(b.ts, m.hoy)} '
                       f'el odómetro pasó de {_num(a.valor_km, 0)} a '
                       f'{_num(b.valor_km, 0)} km y el vehículo no tenía '
                       f'ninguna ruta esos {len(dias)} día(s).'),
                contexto=('turno a nombre de ' + ', '.join(turnos)
                          if turnos else 'sin turno de conductor en ese tramo'),
                propone=('Preguntar a qué se fue: taller, tanqueo, un encargo. '
                         'Si hay explicación, no hay nada más que hacer.'),
                evidencia={'km_desde': a.valor_km, 'km_hasta': b.valor_km,
                           'km': km, 'desde': a.ts, 'hasta': b.ts,
                           'dias': [d.isoformat() for d in dias],
                           'rutas_del_vehiculo_esos_dias': 0,
                           'lectura_desde_id': a.id, 'lectura_hasta_id': b.id},
                caso={'tipo': 'expediente', 'pestana': 'resumen'})


def _km_de_un_dia(m: _Mundo, vehiculo_id, dia, filas):
    """Los km del vehículo en un día: de su lectura más baja a la más alta de
    ese día. `None` si no hay dos lecturas o si el tramo no se puede sostener
    — la misma forma de elegir los extremos que `gastos._tramo_de`."""
    from flota.dominio.odometro import confianza_del_tramo
    from flota.dominio.valores import SIN_DATO, Confianza

    del_dia = [l for l in filas if dia_operativo_de(l.ts) == dia]
    if len(del_dia) < 2:
        return None
    bajo = min(del_dia, key=lambda l: (l.valor_km, l.ts, l.id))
    alto = max(del_dia, key=lambda l: (l.valor_km, l.ts, l.id))
    marca = confianza_del_tramo(bajo.a_dominio(), alto.a_dominio())
    if marca is SIN_DATO or marca == Confianza.DUDOSA:
        return None
    return alto.valor_km - bajo.valor_km


def _senales_km_de_ruta(m: _Mundo, r: _Recolector):
    from flota.dominio import senales as dom

    desde_dia = m.hoy - timedelta(days=dom.VENTANA_SENALES_DIAS)
    # Toda la historia de TODAS las placas mide lo normal de una ruta; solo
    # las del filtro se juzgan.
    medidos = defaultdict(list)        # maestra → [(ruta, km, dia)]
    vigentes = {}
    for (vid, dia), rutas in m.rutas_por_dia.items():
        if len(rutas) != 1 or rutas[0].ruta_maestra_id is None:
            continue
        if vid not in vigentes:
            vigentes[vid] = m.vigentes(vid)
        km = _km_de_un_dia(m, vid, dia, vigentes[vid])
        if km is not None and km > 0:
            medidos[rutas[0].ruta_maestra_id].append((rutas[0], km, dia))

    for maestra_id, casos in medidos.items():
        for ruta, km, dia in casos:
            if ruta.vehiculo_id not in m.ids or dia < desde_dia:
                continue
            placa = next(v.placa for v in m.vehiculos if v.id == ruta.vehiculo_id)
            otros = [k for (rr, k, _d) in casos if rr.id != ruta.id]
            ver = dom.km_de_ruta(km=km, historico=otros)
            nombre = (m.maestras[maestra_id].nombre if maestra_id in m.maestras
                      else f'ruta maestra #{maestra_id}')
            if ver.estado == dom.NO_EVALUABLE:
                r.no_evaluable('km_de_ruta', placa, f'{nombre}: {ver.motivo}')
                continue
            if ver.estado != dom.SENAL:
                continue
            med = ver.datos['mediana']
            r.senal(
                clase='km_de_ruta', placa=placa,
                titulo=f'{_num(km, 0)} km en {nombre}',
                texto=(f'La ruta {nombre} del {dia.strftime("%d/%m")} marcó '
                       f'{_num(km, 0)} km; lo habitual para esa ruta es '
                       f'{_num(med, 0)} km (mediana de {ver.datos["n"]} '
                       f'recorridos medidos).'),
                contexto=f'ruta con {m.nombre_conductor(ruta.conductor_id)}',
                propone=('Preguntar por el desvío: una parada extra, una vía '
                         'cerrada, un encargo fuera de ruta.'),
                evidencia={'ruta_id': ruta.id, 'dia': dia.isoformat(), 'km': km,
                           'mediana_km': med, 'n': ver.datos['n'],
                           'ruta_maestra': nombre},
                caso={'tipo': 'expediente', 'pestana': 'resumen'})


def _senales_combustible(m: _Mundo, r: _Recolector):
    from flota.adaptadores.gastos import rendimiento_publicable_de, tanqueos_de
    from flota.adaptadores.modelos import Gasto, Tanqueo
    from flota.dominio import costos
    from flota.dominio import senales as dom
    from flota.dominio.odometro import confianza_del_tramo
    from flota.dominio.valores import SIN_DATO, Confianza

    desde_dia = m.hoy - timedelta(days=dom.VENTANA_SENALES_DIAS)

    # ── Galones contra lo que el recorrido debió gastar ──────────────────
    for v in m.vehiculos:
        tanqueos = tanqueos_de(v.id)
        recientes = [t for t in tanqueos if t['fecha'] >= desde_dia]
        if not recientes:
            continue
        rp = rendimiento_publicable_de(v.id)
        ventanas = [w for w in costos.ventanas_lleno_a_lleno(tanqueos)
                    if tanqueos[w['i_hasta']]['fecha'] >= desde_dia]
        if not ventanas:
            r.no_evaluable('galones', v.placa,
                           'ningún tramo reciente de tanque lleno a tanque '
                           'lleno: solo así se sabe cuánto se gastó')
            continue
        lectura_por_id = {l.id: l for l in m.lecturas[v.id]}
        for w in ventanas:
            cierre = tanqueos[w['i_hasta']]
            # El mismo criterio que Pendientes y que `km_sin_ruta`: un km en
            # duda no es evidencia. Antes, «kilometraje en duda» aparecía en
            # Pendientes y el mismo número sostenía «galones de más» en Señales.
            a_l = lectura_por_id[tanqueos[w['i_desde']]['lectura_id']]
            b_l = lectura_por_id[cierre['lectura_id']]
            marca = confianza_del_tramo(a_l.a_dominio(), b_l.a_dominio())
            if marca is SIN_DATO or marca == Confianza.DUDOSA:
                r.no_evaluable('galones', v.placa, dom.MOTIVO_TRAMO_EN_DUDA)
                continue
            ver = dom.galones_de_ventana(
                km=w['km'], galones=w['galones'], km_galon=rp['km_galon'],
                publicable=rp['publicable'], motivo_rendimiento=rp['motivo'])
            if ver.estado == dom.NO_EVALUABLE:
                r.no_evaluable('galones', v.placa, ver.motivo)
                continue
            if ver.estado != dom.SENAL:
                continue
            esp = ver.datos['esperados']
            r.senal(
                clase='galones', placa=v.placa,
                titulo=f'{_num(w["galones"])} galones donde se esperaban {_num(esp)}',
                texto=(f'Del tanque lleno en {_num(w["km_desde"], 0)} km al de '
                       f'{_num(w["km_hasta"], 0)} km ({_num(w["km"], 0)} km) '
                       f'entraron {_num(w["galones"])} galones. Con el '
                       f'rendimiento medido de este vehículo '
                       f'({_num(rp["km_galon"])} km/galón, {rp["ventanas"]} '
                       f'ventanas) se esperaban {_num(esp)}.'),
                contexto=f'tanqueo del {cierre["fecha"].strftime("%d/%m")} en '
                         f'{cierre["estacion"]}',
                propone=('Mirar la factura del tanqueo y si hubo un viaje '
                         'cargado o un tanque auxiliar que la ficha no conoce.'),
                evidencia={'km_desde': w['km_desde'], 'km_hasta': w['km_hasta'],
                           'km': w['km'], 'galones': w['galones'],
                           'galones_esperados': esp,
                           'rendimiento_km_galon': rp['km_galon'],
                           'ventanas_del_rendimiento': rp['ventanas'],
                           'fecha': cierre['fecha'].isoformat(),
                           'estacion': cierre['estacion'],
                           'gasto_id': cierre['gasto_id']},
                caso={'tipo': 'expediente', 'pestana': 'gastos',
                      'gasto_id': cierre['gasto_id']})

    # ── Precio del galón contra el resto de la flota ─────────────────────
    desde_precio = m.hoy - timedelta(days=dom.VENTANA_PRECIO_DIAS)
    filas = (db.session.query(Tanqueo, Gasto)
             .join(Gasto, Tanqueo.gasto_id == Gasto.id)
             .filter(Gasto.fecha >= desde_precio).all())
    precios = []
    for t, g in filas:
        p = costos.precio_por_galon(valor=Decimal(g.valor),
                                    galones=Decimal(t.galones))
        precios.append((t, g, p))
    placa_de = {v.id: v.placa for v in m.vehiculos}
    for t, g, p in precios:
        if g.vehiculo_id not in m.ids or g.fecha < desde_dia:
            continue
        otros = [q for (tt, _g, q) in precios
                 if tt.gasto_id != t.gasto_id and q is not SIN_DATO]
        ver = dom.precio_de_galon(precio=p, historico=otros)
        placa = placa_de[g.vehiculo_id]
        if ver.estado == dom.NO_EVALUABLE:
            r.no_evaluable('precio_galon', placa, ver.motivo)
            continue
        if ver.estado != dom.SENAL:
            continue
        med = ver.datos['mediana']
        r.senal(
            clase='precio_galon', placa=placa,
            titulo=f'galón a ${_num(p, 0)} (lo habitual: ${_num(med, 0)})',
            texto=(f'El tanqueo del {g.fecha.strftime("%d/%m")} en {t.estacion} '
                   f'costó ${_num(g.valor, 0)} por {_num(t.galones)} galones: '
                   f'${_num(p, 0)} el galón. La mediana de la flota en '
                   f'{dom.VENTANA_PRECIO_DIAS} días es ${_num(med, 0)} '
                   f'({ver.datos["n"]} tanqueos).'),
            contexto='factura ' + (g.documento_numero or 'sin número'),
            propone=('Revisar la factura: un valor mal digitado, galones de '
                     'menos, o una estación que cobra de más.'),
            evidencia={'gasto_id': g.id, 'fecha': g.fecha.isoformat(),
                       'estacion': t.estacion, 'valor': g.valor,
                       'galones': t.galones, 'precio_galon': p,
                       'mediana_flota': med, 'n': ver.datos['n']},
            caso={'tipo': 'expediente', 'pestana': 'gastos', 'gasto_id': g.id})


def _vigente_en(c, ts) -> bool:
    """¿El turno `c` estaba abierto en `ts`? Un turno que empieza justo en
    `ts` no cuenta (es el que sigue); uno que termina justo en `ts` sí."""
    return c.inicio_ts < ts and (c.fin_ts is None or c.fin_ts >= ts)


def _turno_en(m: '_Mundo', vehiculo_id, ts):
    """El turno del vehículo vigente en `ts` (el último que abrió), o `None`."""
    vigentes = [c for c in m.custodias
                if c.vehiculo_id == vehiculo_id and _vigente_en(c, ts)]
    return vigentes[-1] if vigentes else None


def _senales_turno_de_la_ruta(m: _Mundo, r: _Recolector):
    from flota.dominio import senales as dom

    otro_vehiculo = {}
    for c in m.activa.values():
        if c.custodio_tipo == 'conductor' and c.custodio_conductor_id is not None:
            otro_vehiculo[c.custodio_conductor_id] = c.vehiculo_id
    placa_de = {}
    for v in m.vehiculos:
        placa_de[v.id] = v.placa

    for ruta in m.rutas:
        if ruta.id not in m.dia_ruta or m.dia_ruta[ruta.id] != m.hoy:
            continue
        if ruta.vehiculo_id is None:
            r.no_evaluable('turno_de_la_ruta', None,
                           f'la ruta #{ruta.id} de hoy no tiene placa')
            continue
        if ruta.vehiculo_id not in m.ids:
            continue
        if ruta.estado in dom.ESTADOS_RUTA_TERMINADA:
            # La ruta ya volvió: el turno de AHORA no dice quién la hizo (al
            # final de un día normal el camión está en la sede, y eso no es
            # «salió sin turno»). Se juzga contra el turno vigente cuando la
            # ruta se cerró. Sin la hora del cierre, no se sabe cuál era.
            if ruta.fecha_entregada is None:
                r.no_evaluable('turno_de_la_ruta', placa_de[ruta.vehiculo_id],
                               f'la ruta #{ruta.id} está entregada sin hora de '
                               'cierre: no se sabe qué turno tenía el vehículo')
                continue
            c = _turno_en(m, ruta.vehiculo_id, ruta.fecha_entregada)
            cond_otro = next((x.vehiculo_id for x in m.custodias
                              if x.custodio_tipo == 'conductor'
                              and x.custodio_conductor_id == ruta.conductor_id
                              and x.vehiculo_id != ruta.vehiculo_id
                              and _vigente_en(x, ruta.fecha_entregada)), None)
        else:
            c = m.activa[ruta.vehiculo_id] if ruta.vehiculo_id in m.activa else None
            cond_otro = None
            if ruta.conductor_id in otro_vehiculo \
                    and otro_vehiculo[ruta.conductor_id] != ruta.vehiculo_id:
                cond_otro = otro_vehiculo[ruta.conductor_id]
        ver = dom.turno_de_la_ruta(
            conductor_ruta_id=ruta.conductor_id, estado_ruta=ruta.estado,
            custodia_tipo=(None if c is None else
                           ('pendiente_sede' if c.custodio_estado == 'pendiente_sede'
                            else c.custodio_tipo)),
            custodio_conductor_id=None if c is None else c.custodio_conductor_id,
            otro_vehiculo_del_conductor=cond_otro)
        placa = placa_de[ruta.vehiculo_id]
        if ver.estado == dom.NO_EVALUABLE:
            r.no_evaluable('turno_de_la_ruta', placa, ver.motivo)
            continue
        if ver.estado != dom.SENAL:
            continue
        maneja = m.nombre_conductor(ruta.conductor_id)
        forma = ver.datos['forma']
        if forma == 'turno_de_otro':
            texto = (f'La ruta de hoy la hace {maneja}, pero el turno de '
                     f'{placa} está a nombre de '
                     f'{m.nombre_conductor(c.custodio_conductor_id)}.')
        elif forma == 'salio_sin_turno' and ruta.estado in dom.ESTADOS_RUTA_TERMINADA:
            texto = (f'La ruta de hoy de {placa} se cerró '
                     f'({_palabra(ESTADO_RUTA, ruta.estado)}) y en ese momento el '
                     'vehículo ' + ('no tenía turno abierto.' if c is None else
                                    'estaba en custodia de la sede.'))
        elif forma == 'salio_sin_turno':
            texto = (f'La ruta de hoy de {placa} ya salió '
                     f'({_palabra(ESTADO_RUTA, ruta.estado)}) y el vehículo '
                     + ('no tiene turno abierto.' if c is None else
                        'sigue en custodia de la sede.'))
        else:
            otra = (next((v.placa for v in m.vehiculos if v.id == cond_otro), None)
                    or 'otro vehículo')
            texto = (f'{maneja} hace la ruta de hoy en {placa} pero tiene '
                     f'abierto el turno de {otra}.')
        r.senal(
            clase='turno_de_la_ruta', placa=placa,
            titulo='la ruta de hoy no coincide con el turno',
            texto=texto,
            contexto=f'ruta #{ruta.id} · {_palabra(ESTADO_RUTA, ruta.estado)}',
            propone=('Hacer el traspaso en la app: si pasa algo en la calle, '
                     'el registro tiene que apuntar a quien maneja.'),
            evidencia={'ruta_id': ruta.id, 'forma': forma,
                       'conductor_de_la_ruta': maneja,
                       'custodia_id': None if c is None else c.id},
            caso={'tipo': 'expediente', 'pestana': 'resumen'})


# ── La entrada ───────────────────────────────────────────────────────────────

def armar_bandeja(*, almacen_id: Optional[int] = None,
                  ahora: Optional[datetime] = None) -> dict:
    """La bandeja completa. Levanta `BandejaNoDisponible` si falta una tabla."""
    from flota.adaptadores.medicion import MedidorSQL
    from flota.adaptadores.preventivo import diagnostico_de_la_flota
    from flota.adaptadores.verificacion import pendientes as dudosas_pendientes
    from flota.dominio import senales as dom

    faltan = [t for t in TABLAS_REQUERIDAS if not _tabla_existe(t)]
    if faltan:
        raise BandejaNoDisponible('faltan tablas: ' + ', '.join(faltan))

    m = _Mundo(almacen_id, ahora or datetime.utcnow())
    medidor = MedidorSQL()
    dudosas = dudosas_pendientes()
    dudosas_por_placa = defaultdict(int)
    for _l, placa in dudosas:
        dudosas_por_placa[placa] += 1
    diag_prev = diagnostico_de_la_flota()
    prev_de = defaultdict(list)
    for d in diag_prev:
        prev_de[d['vehiculo_id']].append(d)

    from app.services.senales_ruta import despachos_forzados
    from flota.dominio.senales import VENTANA_RECIENTE_DIAS
    forzados = despachos_forzados(
        desde=m.ahora - timedelta(days=VENTANA_RECIENTE_DIAS))

    pendientes = _pendientes(m, medidor, dudosas, diag_prev, forzados)
    rec = _Recolector()
    _senales_turno_de_la_ruta(m, rec)
    _senales_km_sin_ruta(m, rec)
    _senales_km_de_ruta(m, rec)
    _senales_combustible(m, rec)

    from flota.dominio import salida as dom_sal
    hoy = []
    for pos, v in enumerate(m.vehiculos):
        custodio = _custodio(m, v)
        donde = _donde(m, v)
        km = _km(m, v, dudosas_por_placa)
        insp = _inspeccion(m, v)
        rutas = _rutas_hoy(m, v)
        ficha = _ficha_estado(m, v)
        hechos = _hechos(m, v, km_conocido=km['ts'] is not None,
                         km_dudoso=v.placa in dudosas_por_placa,
                         prev_diag=prev_de[v.id], sale_hoy=bool(rutas))
        evaluacion = dom_sal.evaluar(hechos)
        hoy.append({
            'pos': pos,
            'vehiculo_id': v.id,
            'placa': v.placa,
            'tipo': v.tipo,
            'capacidad_kg': (None if v.capacidad_kg is None
                             else float(v.capacidad_kg)),
            'custodio': custodio,
            'donde': donde,
            'km': km,
            'inspeccion': insp,
            'rutas_hoy': rutas,
            'ficha': ficha,
            'danos_abiertos': (hechos.danos_bloqueantes + hechos.danos_vencidos
                               + hechos.danos_en_plazo),
            'semaforo': evaluacion.semaforo(),
            'pendientes': sum(1 for p in pendientes if p['placa'] == v.placa),
            'senales': sum(1 for s in rec.senales if s['placa'] == v.placa),
        })

    return {
        'dia_operativo': m.hoy.isoformat(),
        'calculado_ts': m.ahora,
        'filtro': m.filtro,
        'hoy': hoy,
        'pendientes': pendientes,
        'senales': rec.senales,
        'senales_no_evaluables': rec.no_evaluables(),
        'umbrales': dom.umbrales(),
        'base': ('vehículos activos; señales de los últimos '
                 f'{dom.VENTANA_SENALES_DIAS} días'),
    }


__all__ = ['armar_bandeja', 'BandejaNoDisponible', 'TABLAS_REQUERIDAS',
           'NOMBRE_DOCUMENTO', 'ESTADO_RUTA']
