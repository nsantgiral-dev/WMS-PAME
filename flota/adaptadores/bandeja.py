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
    'flota_tanqueo', 'flota_plan_tarea',
)


class BandejaNoDisponible(Exception):
    """Falta una tabla. Se declara cuál, no se contesta vacío."""


# ── Palabras en vez de códigos ───────────────────────────────────────────────

NOMBRE_DOCUMENTO = {
    'soat': 'SOAT',
    'rtm': 'revisión técnico-mecánica',
    'poliza_rc': 'póliza de responsabilidad civil',
    'tarjeta_propiedad': 'tarjeta de propiedad',
}

#: Los papeles sin los cuales un vehículo no debería circular. Uno sin cargar
#: pinta ámbar: no se sabe si está al día.
PAPELES_PARA_CIRCULAR = ('soat', 'rtm')

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
                                               Inspeccion, LecturaOdometro)
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

        self.docs_tipos = defaultdict(set)
        for d in DocumentoVehiculo.query.all():
            self.docs_tipos[d.vehiculo_id].add(d.tipo)

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

        # Las rutas, con el día en que ocurrieron. `fecha_programada` manda;
        # sin ella, el día en que salió o, en último caso, el que se creó.
        self.rutas = RutaDespacho.query.order_by(RutaDespacho.id).all()
        self.dia_ruta = {}
        for r in self.rutas:
            if r.fecha_programada is not None:
                self.dia_ruta[r.id] = r.fecha_programada
            elif r.fecha_cierre is not None:
                self.dia_ruta[r.id] = dia_operativo_de(r.fecha_cierre)
            elif r.fecha_creacion is not None:
                self.dia_ruta[r.id] = dia_operativo_de(r.fecha_creacion)
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
    from flota.dominio.inspeccion import APTO, INCOMPLETA, NO_APTO

    i = m.inspeccion_hoy[v.id] if v.id in m.inspeccion_hoy else None
    if i is None:
        estado = 'sin_hacer'
    else:
        estado = {APTO: 'apta', NO_APTO: 'no_apta',
                  INCOMPLETA: 'incompleta'}[i.veredicto]
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


def _documentos_por_placa(filas) -> Dict[str, dict]:
    salida = defaultdict(lambda: {'vencidos': [], 'por_vencer': [],
                                  'no_encontrados': []})
    for f in filas:
        nombre = _palabra(NOMBRE_DOCUMENTO, f['tipo'])
        if f['vencido']:
            salida[f['placa']]['vencidos'].append(nombre)
        elif f['por_vencer_30d']:
            salida[f['placa']]['por_vencer'].append(nombre)
        if f['no_encontrado']:
            salida[f['placa']]['no_encontrados'].append(nombre)
    return salida


def _danos_clasificados(m: _Mundo, v) -> dict:
    """Daños abiertos en tres cubos disjuntos (ver `HechosDelVehiculo`)."""
    from flota.dominio.hallazgo import vencido

    cubos = {'bloqueantes': 0, 'vencidos': 0, 'en_plazo': 0}
    for h in m.danos[v.id]:
        if h.criticidad == 'bloqueante':
            cubos['bloqueantes'] += 1
        elif vencido(h.a_dominio(), m.ahora):
            cubos['vencidos'] += 1
        else:
            cubos['en_plazo'] += 1
    return cubos


def _ficha_estado(m: _Mundo, v) -> str:
    if v.id not in m.fichas:
        return 'sin_ficha'
    return 'completa' if m.fichas[v.id].completa() else 'incompleta'


# ── Pendientes ───────────────────────────────────────────────────────────────

def _pendiente(*, clase, placa, urgencia, texto, detalle, accion, desde=None,
               **extra) -> dict:
    fila = {'clase': clase, 'placa': placa, 'urgencia': urgencia,
            'texto': texto, 'detalle': detalle, 'accion': accion,
            'desde': desde}
    fila.update(extra)
    return fila


def _pendientes(m: _Mundo, medidor, docs_filas, dudosas, diag_prev) -> List[dict]:
    from flota.dominio.hallazgo import dias_transcurridos, vencido
    from flota.dominio.senales import VENTANA_RECIENTE_DIAS
    from flota.dominio.valores import TIPOS_DOCUMENTO

    salida = []
    limite_reciente = m.ahora - timedelta(days=VENTANA_RECIENTE_DIAS)
    por_id = {v.id: v for v in m.vehiculos}

    # 1 · Papeles
    for f in docs_filas:
        if f['placa'] not in m.placas:
            continue
        nombre = _palabra(NOMBRE_DOCUMENTO, f['tipo'])
        if f['vencido']:
            texto = f'{nombre} vencido hace {-f["dias"]} día(s)'
            urg, det = 'rojo', 'Cargá el nuevo. Con el papel vencido el vehículo no debería salir.'
        elif f['por_vencer_30d']:
            texto = f'{nombre} vence en {f["dias"]} día(s)'
            urg, det = 'ambar', 'Sacá la cita o compralo antes de que venza.'
        else:
            texto = f'{nombre}: nadie lo pudo mostrar'
            urg, det = 'ambar', 'No es lo mismo que vencido: no se sabe si existe.'
        salida.append(_pendiente(
            clase='documento', placa=f['placa'], urgencia=urg, texto=texto,
            detalle=det, accion={'tipo': 'expediente', 'pestana': 'documentos'},
            desde=f['vence']))
    for v in m.vehiculos:
        faltan = [t for t in TIPOS_DOCUMENTO if t not in m.docs_tipos[v.id]]
        if faltan:
            salida.append(_pendiente(
                clase='documento_sin_cargar', placa=v.placa, urgencia='ambar',
                texto='sin cargar: ' + ', '.join(
                    _palabra(NOMBRE_DOCUMENTO, t) for t in faltan),
                detalle='Mientras no se cargue, no se sabe si está al día.',
                accion={'tipo': 'expediente', 'pestana': 'documentos'}))

    # 2 · Daños — la cola de decisiones de toda la flota
    for v in m.vehiculos:
        for h in m.danos[v.id]:
            dom = h.a_dominio()
            es_vencido = vencido(dom, m.ahora)
            dias = dias_transcurridos(dom, m.ahora)
            urg = 'rojo' if (h.criticidad == 'bloqueante' or es_vencido) else 'ambar'
            salida.append(_pendiente(
                clase='dano', placa=v.placa, urgencia=urg,
                texto=h.descripcion,
                detalle=(f'{h.criticidad} · lleva {dias} día(s) · '
                         + ('VENCIDO' if es_vencido
                            else f'límite {_cuando(h.fecha_limite, m.hoy)}')
                         + (f' · aplazado {h.aplazado_veces} vez/veces'
                            if h.aplazado_veces else '')
                         + (' · preexistente (no le cuenta a nadie)'
                            if h.linea_base else '')),
                accion={'tipo': 'decidir_dano', 'hallazgo_id': h.id,
                        'pestana': 'danos'},
                desde=h.reportado_ts, hallazgo_id=h.id,
                criticidad=h.criticidad, vencido=es_vencido,
                dias_abierto=dias, aplazado_veces=h.aplazado_veces))

    # 3 · Kilometrajes en duda
    for l, placa in dudosas:
        if placa not in m.placas:
            continue
        salida.append(_pendiente(
            clase='km_dudoso', placa=placa, urgencia='ambar',
            texto=f'kilometraje {_num(l.valor_km, 0)} en duda',
            detalle=(l.motivo_dudosa or 'sin motivo escrito')
                    + f' · registrado {_cuando(l.ts, m.hoy)}',
            accion={'tipo': 'verificar_km', 'lectura_id': l.id},
            desde=l.ts, lectura_id=l.id))

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
                clase='cierre_forzado', placa=f['placa'], urgencia='ambar',
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
                clase='turno_sin_fotos', placa=f['placa'], urgencia='ambar',
                texto=(f'turno de {a_nombre} desde {_cuando(c.inicio_ts, m.hoy)}'
                       f' sin fotos de inicio ({f["fotos"]} de '
                       f'{f["fotos_exigidas"]})'),
                detalle=('Sin la línea base de cómo lo recibió, un golpe nuevo '
                         'no se puede ubicar en ningún turno.'),
                accion={'tipo': 'fotos_turno', 'custodia_id': c.id},
                desde=c.inicio_ts, custodia_id=c.id))

    # 6 · Ficha
    for v in m.vehiculos:
        estado = _ficha_estado(m, v)
        if estado == 'sin_ficha':
            salida.append(_pendiente(
                clase='ficha', placa=v.placa, urgencia='ambar',
                texto='sin ficha técnica: crear la primera',
                detalle=('Sin ficha no hay capacidad de tanque, ni posiciones '
                         'de llanta, ni preventivo.'),
                accion={'tipo': 'expediente', 'pestana': 'ficha'}))
        elif estado == 'incompleta':
            falta = m.fichas[v.id].atributos_sin_dato()
            salida.append(_pendiente(
                clase='ficha', placa=v.placa, urgencia='ambar',
                texto='ficha técnica incompleta',
                detalle='Falta: ' + ', '.join(str(x).replace('_', ' ')
                                               for x in falta),
                accion={'tipo': 'expediente', 'pestana': 'ficha'}))

    # 7 · Preventivo vencido
    from flota.dominio.preventivo import EstadoTarea
    for d in diag_prev:
        if d['vehiculo_id'] not in m.ids or d['estado'] != EstadoTarea.VENCIDA:
            continue
        salida.append(_pendiente(
            clase='preventivo', placa=por_id[d['vehiculo_id']].placa,
            urgencia='rojo', texto=f'{d["nombre"]}: mantenimiento vencido',
            detalle=(f'tocaba a los {_num(d["proximo_km"], 0)} km'
                     if isinstance(d['proximo_km'], int) else
                     'vencido según el plan'),
            accion={'tipo': 'expediente', 'pestana': 'preventivo'},
            plan_id=d['plan_id']))

    orden_clase = {c: i for i, c in enumerate((
        'dano', 'documento', 'preventivo', 'cierre_forzado', 'turno_sin_fotos',
        'km_dudoso', 'ficha', 'documento_sin_cargar'))}
    salida.sort(key=lambda p: (p['urgencia'] != 'rojo', orden_clase[p['clase']],
                               p['placa']))
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
    from flota.dominio.valores import SIN_DATO

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
        for w in ventanas:
            cierre = tanqueos[w['i_hasta']]
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
    from flota.dominio.preventivo import EstadoTarea

    faltan = [t for t in TABLAS_REQUERIDAS if not _tabla_existe(t)]
    if faltan:
        raise BandejaNoDisponible('faltan tablas: ' + ', '.join(faltan))

    m = _Mundo(almacen_id, ahora or datetime.utcnow())
    medidor = MedidorSQL()
    docs_filas = medidor.documentos_por_vehiculo() or []
    docs = _documentos_por_placa(docs_filas)
    dudosas = dudosas_pendientes()
    dudosas_por_placa = defaultdict(int)
    for _l, placa in dudosas:
        dudosas_por_placa[placa] += 1
    diag_prev = diagnostico_de_la_flota()
    prev = defaultdict(lambda: {'vencidas': 0, 'por_vencer': 0})
    for d in diag_prev:
        if d['estado'] == EstadoTarea.VENCIDA:
            prev[d['vehiculo_id']]['vencidas'] += 1
        elif d['estado'] == EstadoTarea.POR_VENCER:
            prev[d['vehiculo_id']]['por_vencer'] += 1

    pendientes = _pendientes(m, medidor, docs_filas, dudosas, diag_prev)
    rec = _Recolector()
    _senales_turno_de_la_ruta(m, rec)
    _senales_km_sin_ruta(m, rec)
    _senales_km_de_ruta(m, rec)
    _senales_combustible(m, rec)

    hoy = []
    for pos, v in enumerate(m.vehiculos):
        custodio = _custodio(m, v)
        donde = _donde(m, v)
        km = _km(m, v, dudosas_por_placa)
        insp = _inspeccion(m, v)
        rutas = _rutas_hoy(m, v)
        danos = _danos_clasificados(m, v)
        ficha = _ficha_estado(m, v)
        d = docs[v.placa] if v.placa in docs else {
            'vencidos': [], 'por_vencer': [], 'no_encontrados': []}
        # Solo los dos que habilitan circular pintan el semáforo; los otros
        # dos van a la lista de pendientes.
        sin_cargar = [_palabra(NOMBRE_DOCUMENTO, t) for t in PAPELES_PARA_CIRCULAR
                      if t not in m.docs_tipos[v.id]]
        c = m.activa[v.id] if v.id in m.activa else None
        hechos = dom.HechosDelVehiculo(
            documentos_vencidos=d['vencidos'],
            documentos_por_vencer=d['por_vencer'],
            documentos_no_encontrados=d['no_encontrados'],
            documentos_sin_cargar=sin_cargar,
            danos_bloqueantes=danos['bloqueantes'],
            danos_vencidos=danos['vencidos'],
            danos_en_plazo=danos['en_plazo'],
            inspeccion=insp['estado'],
            sale_hoy=bool(rutas),
            preventivo_vencidas=prev[v.id]['vencidas'],
            preventivo_por_vencer=prev[v.id]['por_vencer'],
            km_conocido=km['ts'] is not None,
            km_dudoso=v.placa in dudosas_por_placa,
            custodia=('sin_turno' if c is None else
                      'pendiente_sede' if c.custodio_estado == 'pendiente_sede'
                      else c.custodio_tipo),
            fuera_de_sede=donde['codigo'] == 'fuera_de_sede',
            ficha=ficha,
        )
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
            'danos_abiertos': sum(danos.values()),
            'semaforo': dom.semaforo(hechos),
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
