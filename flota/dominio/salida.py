"""
¿Puede salir este camión? — UNA política, tres pantallas.

Puro: no sabe que existe SQLAlchemy ni Flask. Los adaptadores le traen los
hechos ya leídos (`flota/adaptadores/salida.py` uno por uno, la bandeja en
bloque) y acá se decide el nivel y se escribe el texto.

## Por qué existe (2026-09-24)

El rediseño de flota lo hicieron cinco frentes en paralelo y cada uno contestó
esta pregunta por su lado:

| Pantalla | Quién | Qué sumaba | Qué se le escapaba |
|---|---|---|---|
| Semáforo de la bandeja | `senales.semaforo` | papeles, daños, inspección, preventivo | la orden de taller abierta |
| Advertencias al despachar | `senales_ruta.advertencias_de_flota` | papeles, inspección, taller, custodia | **el daño bloqueante y el preventivo vencido**: salía sin pedir motivo |
| Aviso del conductor | `flotaCondAvisos` (JS) | papeles, inspección, preventivo, daños | el taller; y decidía el color en el teléfono, con otro nombre («amarillo») |

Tres respuestas a la misma pregunta, con tres textos distintos para el mismo
SOAT («papel vencido: SOAT», «SOAT vencido desde 2026-09-19», «SOAT vencido hace
5 día(s)»). El día que alguien agrega una causa en una, las otras dos siguen
diciendo que el camión puede salir. Es el corolario de la regla 0 del WMS: una
política, una función.

## Un vocabulario

    rojo    no debería salir, o hay que actuar hoy
    ambar   hay trabajo con plazo, **o no se sabe** (lo que no se sabe no es verde)
    verde   sin pendientes conocidos — nunca «todo bien»: solo se conoce lo registrado

Nada fuera de este archivo decide el nivel de un vehículo. El trinquete
`tests/flota/test_una_politica_de_salida.py` lo exige por AST (Python) y por
texto acotado (JS).

## Cada motivo dice a quién le importa

    ambito = 'salida'    lo que el conductor tiene que saber antes de arrancar
    ambito = 'turno'     quién tiene el vehículo: al que despacha y al encargado
    ambito = 'gestion'   trabajo del encargado que no frena la salida

    frena = True         el despacho pide motivo escrito (FORZAR en la bitácora)

**Informa, no bloquea** (regla 1 de flota y «medir → corregir → imponer»):
`frena` no impide nada; hace que la salida con el problema quede escrita.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

# ── El vocabulario ──────────────────────────────────────────────────────────

ROJO = 'rojo'
AMBAR = 'ambar'
VERDE = 'verde'
NIVELES = (ROJO, AMBAR, VERDE)
_ORDEN_NIVEL = {ROJO: 0, AMBAR: 1, VERDE: 2}

SALIDA = 'salida'
TURNO = 'turno'
GESTION = 'gestion'
AMBITOS = (SALIDA, TURNO, GESTION)

#: El nivel de lo que se revisa de un TURNO y no del vehículo: un cierre
#: forzado, un turno sin fotos de inicio, un despacho que salió reconociendo
#: advertencias. Se pregunta con plazo; no dice nada de si el camión puede
#: salir hoy, y por eso no entra al semáforo.
NIVEL_TURNO_A_REVISAR = AMBAR

#: Con cuántos días de anticipación un papel pasa a «por vencer». El mismo
#: número que usa el health para contar (`MedidorSQL.documentos_por_vehiculo`
#: lo lee de acá).
DIAS_AVISO_PAPEL = 30

#: Los papeles sin los cuales el camión no debería circular. Los otros dos se
#: persiguen igual, pero su ausencia no frena un despacho.
PAPELES_PARA_CIRCULAR = ('soat', 'rtm')

#: Nombre y género de cada papel: «SOAT vencido», «revisión vencida».
NOMBRE_PAPEL: Dict[str, Tuple[str, str]] = {
    'soat': ('SOAT', 'o'),
    'rtm': ('revisión técnico-mecánica', 'a'),
    'poliza_rc': ('póliza de responsabilidad civil', 'a'),
    'tarjeta_propiedad': ('tarjeta de propiedad', 'a'),
}

# Estados de un papel.
VIGENTE = 'vigente'
VENCIDO = 'vencido'
POR_VENCER = 'por_vencer'
NO_ENCONTRADO = 'no_encontrado'
SIN_CARGAR = 'sin_cargar'
ESTADOS_PAPEL = (VIGENTE, VENCIDO, POR_VENCER, NO_ENCONTRADO, SIN_CARGAR)

#: Qué detector se queda ciego sin cada dato de la ficha. Lo que la bandeja
#: dice cuando la ficha no está completa: no «falta un campo», sino qué se
#: deja de ver.
DETECTOR_CIEGO = {
    'capacidad_tanque': ('capacidad del tanque',
                         'sin ella no se detecta un tanqueo que no cabe en el tanque'),
}


# ── Helpers de texto (uno para todas las pantallas) ─────────────────────────

def plural(n: int, singular: str, plural_: str) -> str:
    """«1 daño», «2 daños». Nunca «daño(s)»."""
    return f'{n} {singular if n == 1 else plural_}'


def fecha_larga(d: date) -> str:
    """dd/mm/aaaa — como se escribe en Colombia."""
    return d.strftime('%d/%m/%Y')


def km_legible(n: int) -> str:
    """Separador de miles con punto: 25.000."""
    return f'{int(n):,}'.replace(',', '.')


def _mayus(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def nombre_papel(tipo: str) -> str:
    """El nombre de un papel en minúscula de frase («revisión técnico-mecánica»).

    Indexado y no `.get`: un tipo desconocido revienta (regla 5).
    """
    return NOMBRE_PAPEL[tipo][0]


# ── Papeles: una sola definición de vencido y por vencer ────────────────────

def papel_vencido(vence: Optional[date], hoy: date) -> bool:
    """Vencido = su fecha ya pasó. Sin fecha no es vencido: es «no se sabe»."""
    return vence is not None and vence < hoy


def papel_por_vencer(vence: Optional[date], hoy: date) -> bool:
    return vence is not None and hoy <= vence <= hoy + timedelta(days=DIAS_AVISO_PAPEL)


def estado_de_papel(*, vence: Optional[date], no_encontrado: bool,
                    hoy: date) -> str:
    """El estado de UN papel registrado. Uno que no está registrado es
    `SIN_CARGAR` y lo decide quien sabe qué tipos faltan."""
    if no_encontrado:
        return NO_ENCONTRADO
    if papel_vencido(vence, hoy):
        return VENCIDO
    if papel_por_vencer(vence, hoy):
        return POR_VENCER
    return VIGENTE


# ── Los hechos ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Papel:
    tipo: str
    estado: str                 # uno de ESTADOS_PAPEL
    vence: Optional[date] = None


@dataclass(frozen=True)
class TareaVencida:
    """Una tarea del preventivo que pasó su kilometraje."""
    nombre: str
    km_pasado: Optional[int] = None     # positivo: cuánto se pasó; None = no se sabe


@dataclass(frozen=True)
class Hechos:
    """Lo que un adaptador sabe de un vehículo, ya leído.

    **Todos los campos son obligatorios.** Un hecho que el adaptador no pudo
    leer viaja como `None` (o `'sin_dato'`) y la política lo pinta ámbar con su
    motivo — nunca como cero. Un default en esta clase sería un lector que se
    olvidó de un hecho y un camión en verde por eso.
    """
    hoy: date
    papeles: Tuple[Papel, ...]
    # Disjuntos: un daño abierto cae en uno solo. Bloqueante primero.
    danos_bloqueantes: int
    danos_vencidos: int
    danos_en_plazo: int
    inspeccion: str             # apta | no_apta | incompleta | sin_hacer | sin_dato
    sale_hoy: bool
    preventivo_vencidas: Optional[Tuple[TareaVencida, ...]]   # None = no se pudo leer
    preventivo_por_vencer: int
    ot_abiertas: int
    km_conocido: bool
    km_dudoso: bool
    custodia: str               # conductor | sede | pendiente_sede | sin_turno
    custodio_conductor_id: Optional[int]
    #: Solo al despachar: el conductor de la ruta, para comparar contra quién
    #: tiene el turno. `None` = no se compara (la bandeja lo hace como señal).
    conductor_de_la_ruta: Optional[int]
    fuera_de_sede: bool
    ficha: str                  # completa | incompleta | sin_ficha
    ficha_falta: Tuple[str, ...]


@dataclass(frozen=True)
class Motivo:
    clave: str
    nivel: str
    ambito: str
    frena: bool
    texto: str
    corto: str

    def a_dict(self) -> dict:
        return {'clave': self.clave, 'nivel': self.nivel, 'ambito': self.ambito,
                'frena': self.frena, 'texto': self.texto, 'corto': self.corto}


# ── Un motivo por hecho ─────────────────────────────────────────────────────

def motivo_papel(p: Papel, hoy: date) -> Optional[Motivo]:
    """El motivo de un papel, o `None` si está vigente.

    El texto es el mismo en las tres pantallas: «SOAT vencido desde
    19/09/2026 (hace 5 días)».
    """
    nombre, gen = NOMBRE_PAPEL[p.tipo]
    circula = p.tipo in PAPELES_PARA_CIRCULAR
    if p.estado == VIGENTE:
        return None
    if p.estado == VENCIDO:
        dias = (hoy - p.vence).days
        return Motivo(f'{p.tipo}_vencido', ROJO, SALIDA, True,
                      _mayus(f'{nombre} vencid{gen} desde {fecha_larga(p.vence)} '
                             f'(hace {plural(dias, "día", "días")})'),
                      _mayus(f'{nombre} vencid{gen}'))
    if p.estado == POR_VENCER:
        dias = (p.vence - hoy).days
        cuando = 'hoy' if dias == 0 else f'en {plural(dias, "día", "días")}'
        return Motivo(f'{p.tipo}_por_vencer', AMBAR, GESTION, False,
                      _mayus(f'{nombre} vence el {fecha_larga(p.vence)} ({cuando})'),
                      _mayus(f'{nombre} por vencer'))
    if p.estado == NO_ENCONTRADO:
        pron = 'lo' if gen == 'o' else 'la'
        return Motivo(f'{p.tipo}_no_encontrado', AMBAR,
                      SALIDA if circula else GESTION, circula,
                      _mayus(f'{nombre}: nadie {pron} pudo mostrar (no se sabe si existe)'),
                      _mayus(f'{nombre} sin mostrar'))
    if p.estado == SIN_CARGAR:
        return Motivo(f'{p.tipo}_sin_registro', AMBAR,
                      SALIDA if circula else GESTION, circula,
                      _mayus(f'{nombre} sin cargar: no se sabe si está al día'),
                      _mayus(f'{nombre} sin cargar'))
    raise ValueError(f'estado de papel desconocido: {p.estado!r}')


def motivos_de_danos(bloqueantes: int, vencidos: int, en_plazo: int) -> List[Motivo]:
    salida = []
    if bloqueantes:
        t = plural(bloqueantes, 'daño bloqueante abierto', 'daños bloqueantes abiertos')
        salida.append(Motivo('dano_bloqueante', ROJO, SALIDA, True, _mayus(t), _mayus(t)))
    if vencidos:
        salida.append(Motivo(
            'dano_vencido', ROJO, SALIDA, True,
            _mayus(plural(vencidos, 'daño pasado de su fecha límite',
                          'daños pasados de su fecha límite')),
            _mayus(plural(vencidos, 'daño vencido', 'daños vencidos'))))
    if en_plazo:
        salida.append(Motivo(
            'dano_en_plazo', AMBAR, SALIDA, False,
            _mayus(plural(en_plazo, 'daño abierto dentro de plazo',
                          'daños abiertos dentro de plazo')),
            _mayus(plural(en_plazo, 'daño abierto', 'daños abiertos'))))
    return salida


def nivel_de_dano(*, criticidad: str, vencido: bool) -> str:
    """El nivel de UN daño en la cola de decisiones: el mismo criterio que el
    semáforo (bloqueante o vencido = rojo)."""
    return ROJO if (criticidad == 'bloqueante' or vencido) else AMBAR


def motivo_inspeccion(inspeccion: str, sale_hoy: bool) -> Optional[Motivo]:
    if inspeccion == 'no_apta':
        return Motivo('inspeccion_no_apta', ROJO, SALIDA, True,
                      'La inspección de hoy salió no apta', 'Inspección no apta')
    if inspeccion == 'sin_dato':
        return Motivo('inspeccion_sin_dato', AMBAR, SALIDA, True,
                      'No se pudo leer la inspección de hoy',
                      'Inspección sin dato')
    if not sale_hoy or inspeccion == 'apta':
        return None
    if inspeccion == 'incompleta':
        return Motivo('inspeccion_incompleta', ROJO, SALIDA, True,
                      'Tiene ruta hoy y la inspección quedó incompleta: no habilita salir',
                      'Inspección incompleta')
    if inspeccion == 'sin_hacer':
        return Motivo('sin_inspeccion_hoy', ROJO, SALIDA, True,
                      'Tiene ruta hoy y todavía no tiene la inspección de hoy',
                      'Sin inspección hoy')
    raise ValueError(f'inspección desconocida: {inspeccion!r}')


def motivo_preventivo_vencido(t: TareaVencida) -> Motivo:
    km = (f' ({km_legible(t.km_pasado)} km pasado)'
          if isinstance(t.km_pasado, int) and t.km_pasado > 0 else '')
    return Motivo('preventivo_vencido', ROJO, SALIDA, True,
                  f'Mantenimiento vencido: {t.nombre}{km}',
                  'Mantenimiento vencido')


def motivos_de_preventivo(vencidas: Optional[Sequence[TareaVencida]],
                          por_vencer: int) -> List[Motivo]:
    if vencidas is None:
        return [Motivo('preventivo_sin_dato', AMBAR, GESTION, False,
                       'No se pudo leer el plan de mantenimiento: no se sabe '
                       'si hay algo vencido', 'Mantenimiento sin dato')]
    salida = [motivo_preventivo_vencido(t) for t in vencidas]
    if por_vencer:
        t = plural(por_vencer, 'mantenimiento por vencer', 'mantenimientos por vencer')
        salida.append(Motivo('preventivo_por_vencer', AMBAR, GESTION, False,
                             _mayus(t), _mayus(t)))
    return salida


def motivo_taller(ot_abiertas: int) -> Optional[Motivo]:
    if not ot_abiertas:
        return None
    t = ('Tiene una orden de taller abierta' if ot_abiertas == 1
         else f'Tiene {ot_abiertas} órdenes de taller abiertas')
    return Motivo('en_taller', AMBAR, SALIDA, True, t, 'Orden de taller abierta')


def motivos_de_custodia(custodia: str, custodio_conductor_id: Optional[int],
                        conductor_de_la_ruta: Optional[int]) -> List[Motivo]:
    if custodia == 'sin_turno':
        return [Motivo('sin_custodia', AMBAR, TURNO, True,
                       'Nadie tiene el turno registrado', 'Sin turno')]
    salida = []
    if custodia == 'pendiente_sede':
        salida.append(Motivo('custodia_pendiente_sede', AMBAR, GESTION, False,
                             'El turno quedó en una sede que no está en el maestro',
                             'Sede sin maestro'))
    if conductor_de_la_ruta is None:
        return salida
    if custodia == 'conductor':
        if custodio_conductor_id != conductor_de_la_ruta:
            salida.append(Motivo('custodio_distinto', AMBAR, TURNO, True,
                                 'El turno del vehículo está a nombre de otro conductor',
                                 'Turno de otro conductor'))
    else:
        salida.append(Motivo('custodia_en_sede', AMBAR, TURNO, True,
                             'El vehículo sigue en custodia de la sede, no del '
                             'conductor de la ruta', 'Turno en la sede'))
    return salida


def motivos_de_km(km_conocido: bool, km_dudoso: bool) -> List[Motivo]:
    if not km_conocido:
        return [Motivo('km_sin_dato', AMBAR, GESTION, False,
                       'Sin ninguna lectura de kilometraje: no se sabe cuántos km tiene',
                       'Km sin dato')]
    if km_dudoso:
        return [Motivo('km_en_duda', AMBAR, GESTION, False,
                       'El último kilometraje está en duda: verifíquelo contra la foto',
                       'Km en duda')]
    return []


def motivos_de_ficha(ficha: str, falta: Sequence[str]) -> List[Motivo]:
    if ficha == 'completa':
        return []
    if ficha == 'sin_ficha':
        return [Motivo('sin_ficha', AMBAR, GESTION, False,
                       'Sin ficha técnica: sin capacidad de tanque, llantas ni '
                       'preventivo', 'Sin ficha')]
    return [Motivo('ficha_incompleta', AMBAR, GESTION, False,
                   texto_ficha_incompleta(falta), 'Ficha incompleta')]


def texto_ficha_incompleta(falta: Sequence[str]) -> str:
    """«Ficha técnica incompleta: falta capacidad del tanque (sin ella no se
    detecta un tanqueo que no cabe en el tanque)». Lo que se deja de ver, no
    solo el campo."""
    partes = []
    for f in falta:
        if f in DETECTOR_CIEGO:
            nombre, ciego = DETECTOR_CIEGO[f]
            partes.append(f'{nombre} ({ciego})')
        else:
            partes.append(str(f).replace('_', ' '))
    return 'Ficha técnica incompleta' + (': falta ' + ', '.join(partes) if partes else '')


# ── Renglones para leer ─────────────────────────────────────────────────────

_SUFIJO_SIN_CARGAR = '_sin_registro'


def lineas(motivos: Sequence[Motivo]) -> List[dict]:
    """Los motivos como renglones de pantalla. Los papeles sin cargar van en
    UNO («Sin cargar, no se sabe si están al día: SOAT, revisión
    técnico-mecánica»): cuatro renglones iguales salvo el nombre esconden lo
    que sí es distinto. El renglón agrupado va donde estaba el primero."""
    out: List[Optional[dict]] = []
    sin, pos = [], None
    for m in motivos:
        if m.clave.endswith(_SUFIJO_SIN_CARGAR):
            if pos is None:
                pos = len(out)
                out.append(None)
            sin.append(m)
        else:
            out.append({'nivel': m.nivel, 'texto': m.texto, 'corto': m.corto,
                        'clave': m.clave, 'frena': m.frena})
    if sin:
        if len(sin) == 1:
            m = sin[0]
            fila = {'nivel': m.nivel, 'texto': m.texto, 'corto': m.corto,
                    'clave': m.clave, 'frena': m.frena}
        else:
            nombres = [nombre_papel(m.clave[:-len(_SUFIJO_SIN_CARGAR)]) for m in sin]
            fila = {'nivel': color_de(sin),
                    'texto': 'Sin cargar, no se sabe si están al día: ' + ', '.join(nombres),
                    'corto': 'Papeles sin cargar',
                    'clave': 'papeles_sin_registro',
                    'frena': any(m.frena for m in sin)}
        out[pos] = fila
    return out


# ── La evaluación ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Evaluacion:
    color: str
    motivos: Tuple[Motivo, ...]

    def para_el_conductor(self) -> List[Motivo]:
        """Lo que el conductor tiene que saber antes de arrancar."""
        return [m for m in self.motivos if m.ambito == SALIDA]

    def para_despachar(self) -> List[Motivo]:
        """Lo que el despacho pide reconocer con motivo escrito."""
        return [m for m in self.motivos if m.frena]

    def color_para_el_conductor(self) -> str:
        return color_de(self.para_el_conductor())

    def semaforo(self) -> dict:
        """El semáforo de la bandeja: color y porqué, lo más grave primero."""
        porque = lineas(self.motivos)
        if not porque:
            porque = [{'nivel': VERDE, 'texto': 'Sin pendientes conocidos',
                       'corto': 'Sin pendientes conocidos',
                       'clave': 'sin_pendientes', 'frena': False}]
        return {'color': self.color, 'porque': porque}

    def aviso_del_conductor(self) -> dict:
        """Lo que viaja a la tarjeta del conductor: el color y los renglones
        de lo que tiene que saber antes de arrancar. El teléfono no decide
        nada: pinta esto."""
        return {'color': self.color_para_el_conductor(),
                'motivos': lineas(self.para_el_conductor())}


def color_de(motivos: Sequence[Motivo]) -> str:
    """Rojo si alguno es rojo; ámbar si hay alguno; verde si no hay nada."""
    if any(m.nivel == ROJO for m in motivos):
        return ROJO
    return AMBAR if motivos else VERDE


def orden_de_nivel(nivel: str) -> int:
    """Para ordenar lo más grave primero. Un nivel desconocido revienta."""
    return _ORDEN_NIVEL[nivel]


def evaluar(h: Hechos) -> Evaluacion:
    """La política. Lo más grave primero, y dentro del mismo nivel en el orden
    en que se atiende: papeles, daños, inspección, preventivo, taller, turno,
    km, ficha."""
    motivos: List[Motivo] = []
    for p in sorted(h.papeles, key=lambda p: list(NOMBRE_PAPEL).index(p.tipo)):
        m = motivo_papel(p, h.hoy)
        if m is not None:
            motivos.append(m)
    motivos += motivos_de_danos(h.danos_bloqueantes, h.danos_vencidos, h.danos_en_plazo)
    m = motivo_inspeccion(h.inspeccion, h.sale_hoy)
    if m is not None:
        motivos.append(m)
    motivos += motivos_de_preventivo(h.preventivo_vencidas, h.preventivo_por_vencer)
    m = motivo_taller(h.ot_abiertas)
    if m is not None:
        motivos.append(m)
    motivos += motivos_de_custodia(h.custodia, h.custodio_conductor_id,
                                   h.conductor_de_la_ruta)
    if h.fuera_de_sede:
        motivos.append(Motivo('fuera_de_sede', AMBAR, GESTION, False,
                              'Está pasando la noche fuera de sede', 'Fuera de sede'))
    motivos += motivos_de_km(h.km_conocido, h.km_dudoso)
    motivos += motivos_de_ficha(h.ficha, h.ficha_falta)
    # Estable: el orden de arriba se conserva dentro de cada nivel.
    motivos.sort(key=lambda m: _ORDEN_NIVEL[m.nivel])
    return Evaluacion(color_de(motivos), tuple(motivos))


__all__ = [
    'ROJO', 'AMBAR', 'VERDE', 'NIVELES', 'SALIDA', 'TURNO', 'GESTION', 'AMBITOS',
    'DIAS_AVISO_PAPEL', 'PAPELES_PARA_CIRCULAR', 'NOMBRE_PAPEL', 'ESTADOS_PAPEL',
    'VIGENTE', 'VENCIDO', 'POR_VENCER', 'NO_ENCONTRADO', 'SIN_CARGAR',
    'DETECTOR_CIEGO', 'NIVEL_TURNO_A_REVISAR', 'lineas', 'Papel', 'TareaVencida', 'Hechos', 'Motivo', 'Evaluacion',
    'evaluar', 'color_de', 'orden_de_nivel', 'estado_de_papel', 'papel_vencido',
    'papel_por_vencer', 'motivo_papel', 'motivos_de_danos', 'nivel_de_dano',
    'motivo_inspeccion', 'motivo_preventivo_vencido', 'motivos_de_preventivo',
    'motivo_taller', 'motivos_de_custodia', 'motivos_de_km', 'motivos_de_ficha',
    'texto_ficha_incompleta', 'plural', 'fecha_larga', 'km_legible', 'nombre_papel',
]
