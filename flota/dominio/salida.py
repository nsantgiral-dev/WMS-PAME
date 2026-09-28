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

**Informa, no bloquea — salvo lo que la ley prohíbe y se SABE** (2026-09-27).
`frena` no impide nada; hace que la salida con el problema quede escrita. Pero
un SOAT, una revisión técnico-mecánica o una licencia de conducción **vencidos
con fecha registrada** no son una duda: son determinísticos, y su costo
(inmovilización, ADRES repitiendo contra la empresa, la póliza excluyendo el
siniestro) es irreversible. Esos motivos están en `BLOQUEAN_SALIDA`: el despacho
los rechaza y solo `ROLES_AUTORIZAN_SALIDA_PROHIBIDA` los deja pasar, con un
motivo de al menos `MOTIVO_MINIMO_SALIDA_PROHIBIDA` caracteres.

Lo que **no se sabe** (papel sin cargar, no encontrado, dato a corregir,
licencia sin cargar) sigue siendo «informa, no bloquea», y la pantalla lo
muestra en OTRO cuadro (`gravedad_para_despachar`): si lo prohibido y lo
desconocido van en la misma lista, el «ok» que se aprende a escribir para lo
segundo saca el camión ilegal.

La decisión es del dueño y vive en TRES constantes de este archivo:
`BLOQUEAN_SALIDA`, `ROLES_AUTORIZAN_SALIDA_PROHIBIDA` y
`MOTIVO_MINIMO_SALIDA_PROHIBIDA`. Cambiarla es cambiarlas a ellas.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

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
#: El papel está cargado pero sus fechas son imposibles (vence el mismo día en
#: que se expidió, año 0025): no se sabe cuándo vence de verdad. Ámbar, como
#: todo lo que no se sabe — no es «vencido», y tampoco es «al día».
DATO_A_CORREGIR = 'dato_a_corregir'
ESTADOS_PAPEL = (VIGENTE, VENCIDO, POR_VENCER, NO_ENCONTRADO, SIN_CARGAR,
                 DATO_A_CORREGIR)

# ── Lo que la ley prohíbe: la decisión del dueño, en tres constantes ────────

#: Los motivos que NO se reconocen con cualquier texto. Un vencimiento con
#: fecha registrada de un papel sin el cual el vehículo o el conductor no pueden
#: circular. Lo que no se sabe (sin cargar, no encontrado, dato a corregir) no
#: está acá a propósito: frena con motivo simple.
BLOQUEAN_SALIDA: FrozenSet[str] = frozenset({
    'soat_vencido', 'rtm_vencido', 'licencia_vencida'})

#: Quién puede autorizar que salga igual. El dueño puede agregar `gerente`.
ROLES_AUTORIZAN_SALIDA_PROHIBIDA: Tuple[str, ...] = ('admin',)

#: Largo mínimo del motivo de esa autorización. Veinte caracteres no prueban
#: nada, pero «ok» deja de caber: obliga a escribir una frase.
MOTIVO_MINIMO_SALIDA_PROHIBIDA = 20

# Cómo se muestra cada motivo al despachar — tres cuadros, no uno.
PROHIBIDO = 'prohibido'       # la ley lo prohíbe y se sabe
NO_SE_SABE = 'no_se_sabe'     # falta el dato para saber
ADVERTENCIA = 'advertencia'   # se sabe y no es ilegal: se reconoce con motivo
GRAVEDADES = (PROHIBIDO, NO_SE_SABE, ADVERTENCIA)

_SUFIJOS_NO_SE_SABE = ('_sin_registro', '_no_encontrado', '_dato_a_corregir',
                       '_sin_dato', '_sin_cargar')


def gravedad_para_despachar(clave: str) -> str:
    """En qué cuadro va un motivo al despachar. UNA función: la usan el 409
    del muelle y la regla de autorización del servicio."""
    if clave in BLOQUEAN_SALIDA:
        return PROHIBIDO
    if clave.endswith(_SUFIJOS_NO_SE_SABE):
        return NO_SE_SABE
    return ADVERTENCIA


def rol_autoriza_salida_prohibida(rol: Optional[str]) -> bool:
    return rol in ROLES_AUTORIZAN_SALIDA_PROHIBIDA


def puede_autorizar_salida_prohibida(rol: Optional[str], motivo: Optional[str]) -> Optional[str]:
    """`None` si este rol, con este motivo, puede sacar un vehículo con algo de
    `BLOQUEAN_SALIDA`; si no, el porqué en usted (lo que la pantalla muestra).

    UNA función: el servicio la llama antes de escribir el FORZAR, y el
    trinquete `tests/flota/test_salida_prohibida.py` exige por AST que todo
    escritor de un FORZAR de flota pase por acá."""
    if not rol_autoriza_salida_prohibida(rol):
        quien = ', '.join(ROLES_AUTORIZAN_SALIDA_PROHIBIDA)
        return (f'El vehículo no puede salir: la ley lo prohíbe. Solo un usuario '
                f'con rol {quien} puede autorizar la salida, con un motivo escrito.')
    texto = (motivo or '').strip()
    if len(texto) < MOTIVO_MINIMO_SALIDA_PROHIBIDA:
        return (f'Para autorizar una salida que la ley prohíbe escriba un motivo '
                f'de al menos {MOTIVO_MINIMO_SALIDA_PROHIBIDA} caracteres '
                f'(lleva {len(texto)}). Queda registrado con su nombre y la hora.')
    return None


# ── Fechas plausibles de un papel o una licencia ────────────────────────────

#: Un año antes de este es un error de digitación (la póliza con año 0025).
ANIO_MINIMO_PAPEL = 2000
#: Nada que se registre hoy vence más allá de esto.
ANIOS_MAXIMOS_ADELANTE = 15
#: El SOAT dura un año desde el inicio de vigencia; la expedición puede ser
#: unas semanas antes (se compra antes de que venza el anterior). Fuera de este
#: rango, el dato está mal escrito.
DIAS_VIGENCIA_SOAT = (330, 430)

#: Una licencia sin cargar pide motivo al despachar (FORZAR simple, en el
#: cuadro de «no se sabe»), igual que un SOAT sin cargar. No bloquea: no se
#: sabe. Apagarlo es cambiar esta constante.
LICENCIA_SIN_CARGAR_FRENA = True

#: Categorías de la licencia de conducción en Colombia (Ley 769 de 2002).
CATEGORIAS_LICENCIA = ('A1', 'A2', 'B1', 'B2', 'B3', 'C1', 'C2', 'C3')


def _fecha_texto(d: date) -> str:
    return f'{d.day:02d}/{d.month:02d}/{d.year:04d}'


def _tope_adelante(hoy: date) -> date:
    try:
        return hoy.replace(year=hoy.year + ANIOS_MAXIMOS_ADELANTE)
    except ValueError:          # 29 de febrero
        return hoy.replace(year=hoy.year + ANIOS_MAXIMOS_ADELANTE, day=28)


def fechas_imposibles(expedicion: Optional[date], vencimiento: Optional[date],
                      hoy: date) -> List[str]:
    """Lo que ninguna fecha real puede ser, en usted: un año antes de
    `ANIO_MINIMO_PAPEL` o a más de `ANIOS_MAXIMOS_ADELANTE`, una expedición en
    el futuro, un vencimiento igual o anterior a la expedición.

    Es lo que vuelve `DATO_A_CORREGIR` una fila **ya cargada** (la RTM de
    BDT261 que vence el mismo día en que se expidió; la póliza de TGZ653 con
    año 0025). Solo lo imposible: una regla de verosimilitud (el SOAT de un
    año) aplicada a lo viejo convertiría un SOAT vencido de verdad, con la
    expedición mal escrita, en «no se sabe» — y lo sacaría del bloqueo."""
    out = []
    for nombre, f in (('expedición', expedicion), ('vencimiento', vencimiento)):
        if f is None:
            continue
        if f.year < ANIO_MINIMO_PAPEL:
            out.append(f'La fecha de {nombre} ({_fecha_texto(f)}) tiene el año '
                       f'{f.year}: revise cómo quedó escrito el año.')
        elif f > _tope_adelante(hoy):
            out.append(f'La fecha de {nombre} ({_fecha_texto(f)}) está a más de '
                       f'{ANIOS_MAXIMOS_ADELANTE} años: revise el año.')
    if expedicion is not None and expedicion > hoy:
        out.append(f'La fecha de expedición ({_fecha_texto(expedicion)}) es '
                   f'posterior a hoy: un papel no se expide en el futuro.')
    if (expedicion is not None and vencimiento is not None
            and vencimiento <= expedicion):
        out.append(f'El vencimiento ({_fecha_texto(vencimiento)}) es igual o '
                   f'anterior a la expedición ({_fecha_texto(expedicion)}).')
    return out


def problemas_de_fechas(tipo: str, expedicion: Optional[date],
                        vencimiento: Optional[date], hoy: date) -> List[str]:
    """Por qué estas fechas no se pueden GUARDAR, en usted. Lista vacía =
    plausibles. Lo imposible (`fechas_imposibles`) y, para el SOAT, una
    vigencia que no es de un año (`DIAS_VIGENCIA_SOAT`).

    Es la puerta de entrada (`POST /flota/vehiculo/<placa>/documentos`); lo ya
    cargado se juzga solo con `fechas_imposibles`."""
    out = fechas_imposibles(expedicion, vencimiento, hoy)
    if (tipo == 'soat' and expedicion is not None and vencimiento is not None
            and vencimiento > expedicion):
        dias = (vencimiento - expedicion).days
        lo, hi = DIAS_VIGENCIA_SOAT
        if not lo <= dias <= hi:
            out.append(f'Un SOAT dura un año, y entre la expedición y el '
                       f'vencimiento hay {dias} días: revise las dos fechas.')
    return out


def _vencimiento_creible(expedicion: Optional[date], vencimiento: Optional[date],
                         hoy: date) -> bool:
    """¿Se le puede creer al vencimiento aunque la fila tenga fechas imposibles?

    Sí, si el vencimiento por sí solo es posible y lo imposible es la
    EXPEDICIÓN (año 0025, en el futuro). No, si las dos son posibles y no
    cuadran entre sí (vence el día en que se expidió): no se sabe cuál de las
    dos está mal escrita."""
    if vencimiento is None or fechas_imposibles(None, vencimiento, hoy):
        return False
    if expedicion is None:
        return True
    return bool(fechas_imposibles(expedicion, None, hoy))


def papel_cargado(tipo: str, *, no_encontrado: bool, expedicion: Optional[date],
                  vencimiento: Optional[date], hoy: date) -> 'Papel':
    """El `Papel` de una fila ya cargada. UNA función para la bandeja, el
    despacho, el correo y la puerta que reescribe el papel.

    Fechas imposibles → `DATO_A_CORREGIR`, **salvo** que el vencimiento sea
    creíble por sí solo y ya haya pasado (validación 2026-09-27): un SOAT
    vencido de verdad con la expedición mal escrita sigue VENCIDO. Lo contrario
    le daba una salida a lo que la ley prohíbe por un error de digitación."""
    if no_encontrado:
        return Papel(tipo, NO_ENCONTRADO)
    problemas = fechas_imposibles(expedicion, vencimiento, hoy)
    if problemas and _vencimiento_creible(expedicion, vencimiento, hoy) \
            and papel_vencido(vencimiento, hoy):
        return Papel(tipo, VENCIDO, vencimiento)
    estado = estado_de_papel(vence=vencimiento, no_encontrado=False, hoy=hoy,
                             fechas_imposibles=bool(problemas))
    return Papel(tipo, estado, vencimiento, problemas[0] if problemas else None)


def motivo_no_puede_reescribir_papel(*, tipo: str, antes: 'Papel', numero_antes: str,
                                     estado_nuevo: str, vence_nuevo: Optional[date],
                                     numero_nuevo: str, trae_archivo: bool,
                                     rol: Optional[str], hoy: date) -> Optional[str]:
    """`None` si este cambio del papel puede guardarse; si no, el porqué en
    usted (validación 2026-09-27, VAL-FL-1/2).

    El despacho frena un SOAT o una RTM vencidos, y el mismo rol que el
    despacho frena podía quitar el freno reescribiendo el papel: marcarlo «no
    encontrado» (lo bajaba a «no se sabe») o ponerle fechas nuevas sin
    escaneo. Ahora, para un papel para circular que HOY está vencido:

    · «no encontrado» no se acepta, de nadie: el vencimiento se sabe, y «no
      encontrado» lo escondería;
    · dejarlo al día exige el escaneo nuevo Y otro número (es otro papel), o
      un rol de `ROLES_AUTORIZAN_SALIDA_PROHIBIDA`;
    · un cambio que lo deja vencido (corregir la entidad) no se frena.
    """
    if tipo not in PAPELES_PARA_CIRCULAR or antes.estado != VENCIDO:
        return None
    nombre = NOMBRE_PAPEL[tipo][0]
    if estado_nuevo == NO_ENCONTRADO:
        return (f'{_mayus(nombre)} está vencido desde {fecha_larga(antes.vence)}: no '
                f'se puede marcar «no encontrado», porque se sabe que está vencido. '
                f'Cargue el nuevo con su escaneo.')
    if papel_vencido(vence_nuevo, hoy):
        return None
    if rol_autoriza_salida_prohibida(rol):
        return None
    otro_numero = bool((numero_nuevo or '').strip()) and \
        (numero_nuevo or '').strip() != (numero_antes or '').strip()
    if trae_archivo and otro_numero:
        return None
    return (f'{_mayus(nombre)} está vencido desde {fecha_larga(antes.vence)}. Para '
            f'darlo por renovado cargue el papel nuevo: su número (distinto del '
            f'anterior) y el escaneo. Sin eso, solo un administrador puede cambiarlo.')


def problemas_de_licencia(numero: Optional[str], categoria: Optional[str],
                          vence: Optional[date], hoy: date) -> List[str]:
    """Por qué estos datos de licencia no se pueden guardar, en usted. Todo
    vacío es válido: es «sin cargar», que la política trata como no se sabe."""
    numero = (numero or '').strip()
    categoria = (categoria or '').strip().upper()
    if not numero and not categoria and vence is None:
        return []
    out = []
    if not numero:
        out.append('Falta el número de la licencia.')
    if categoria not in CATEGORIAS_LICENCIA:
        out.append('La categoría de la licencia debe ser una de: '
                   + ', '.join(CATEGORIAS_LICENCIA) + '.')
    if vence is None:
        out.append('Falta la fecha de vencimiento de la licencia.')
    else:
        out += fechas_imposibles(None, vence, hoy)
    return out

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
    """dd/mm/aaaa — como se escribe en Colombia. A mano y no con `strftime`:
    `%Y` de un año < 1000 sale sin ceros en unas plataformas y con ceros en
    otras, y el año 0025 de la póliza de TGZ653 tiene que leerse igual en las
    dos."""
    return _fecha_texto(d)


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
                    hoy: date, fechas_imposibles: bool = False) -> str:
    """El estado de UN papel registrado. Uno que no está registrado es
    `SIN_CARGAR` y lo decide quien sabe qué tipos faltan. Uno con fechas
    imposibles (`problemas_de_fechas`) es `DATO_A_CORREGIR`: su vencimiento no
    dice nada."""
    if no_encontrado:
        return NO_ENCONTRADO
    if fechas_imposibles:
        return DATO_A_CORREGIR
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
    #: Solo con `DATO_A_CORREGIR`: qué tienen de imposible las fechas.
    problema: Optional[str] = None


@dataclass(frozen=True)
class Licencia:
    """La licencia de conducción del conductor de la ruta. Mismos estados que
    un papel salvo `NO_ENCONTRADO` (nadie la busca: se carga o no)."""
    estado: str                 # VIGENTE | POR_VENCER | VENCIDO | SIN_CARGAR | DATO_A_CORREGIR
    vence: Optional[date] = None
    problema: Optional[str] = None


def estado_de_licencia(*, numero: Optional[str], categoria: Optional[str],
                       vence: Optional[date], hoy: date) -> Licencia:
    """Una licencia con número, categoría y vencimiento plausibles se juzga por
    su fecha; sin cargar es `SIN_CARGAR`; cargada con datos imposibles, dato a
    corregir."""
    if not (numero or '').strip() and vence is None:
        return Licencia(SIN_CARGAR)
    problemas = problemas_de_licencia(numero, categoria, vence, hoy)
    if problemas:
        return Licencia(DATO_A_CORREGIR, vence, problemas[0])
    return Licencia(estado_de_papel(vence=vence, no_encontrado=False, hoy=hoy), vence)


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
    #: La licencia del conductor de la ruta. **La única excepción a «todos los
    #: campos obligatorios»**, y por la misma razón que `conductor_de_la_ruta`
    #: puede ser `None`: la licencia es de una PERSONA, y la bandeja evalúa
    #: vehículos sin conductor. `None` = no se evalúa (no hay a quién). El
    #: despacho y el conductor la pasan siempre —`hechos_de_vehiculo` la lee
    #: cuando recibe `conductor_de_la_ruta`, y
    #: `tests/flota/test_salida_prohibida.py` lo exige por AST.
    licencia: Optional[Licencia] = None


@dataclass(frozen=True)
class Motivo:
    clave: str
    nivel: str
    ambito: str
    frena: bool
    texto: str
    corto: str

    @property
    def bloquea(self) -> bool:
        """La ley lo prohíbe y se sabe: solo sale con autorización (arriba)."""
        return self.clave in BLOQUEAN_SALIDA

    @property
    def gravedad(self) -> str:
        return gravedad_para_despachar(self.clave)

    def a_dict(self) -> dict:
        return {'clave': self.clave, 'nivel': self.nivel, 'ambito': self.ambito,
                'frena': self.frena, 'texto': self.texto, 'corto': self.corto,
                'bloquea': self.bloquea, 'gravedad': self.gravedad}


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
    if p.estado == DATO_A_CORREGIR:
        return Motivo(f'{p.tipo}_dato_a_corregir', AMBAR,
                      SALIDA if circula else GESTION, circula,
                      _mayus(f'{nombre}: dato a corregir. {p.problema or "Las fechas no son posibles."} '
                             f'Mientras no se corrija, no se sabe si está al día.'),
                      _mayus(f'{nombre}: dato a corregir'))
    raise ValueError(f'estado de papel desconocido: {p.estado!r}')


def motivo_licencia(lic: Optional[Licencia], hoy: date) -> Optional[Motivo]:
    """El motivo de la licencia del conductor de la ruta, o `None` si está
    vigente o no hay a quién evaluar."""
    if lic is None or lic.estado == VIGENTE:
        return None
    if lic.estado == VENCIDO:
        dias = (hoy - lic.vence).days
        return Motivo('licencia_vencida', ROJO, SALIDA, True,
                      f'Licencia de conducción del conductor vencida desde '
                      f'{fecha_larga(lic.vence)} (hace {plural(dias, "día", "días")})',
                      'Licencia vencida')
    if lic.estado == POR_VENCER:
        dias = (lic.vence - hoy).days
        cuando = 'hoy' if dias == 0 else f'en {plural(dias, "día", "días")}'
        return Motivo('licencia_por_vencer', AMBAR, GESTION, False,
                      f'Licencia de conducción vence el {fecha_larga(lic.vence)} ({cuando})',
                      'Licencia por vencer')
    if lic.estado == SIN_CARGAR:
        return Motivo('licencia_sin_cargar', AMBAR, TURNO, LICENCIA_SIN_CARGAR_FRENA,
                      'Licencia de conducción sin cargar: no se sabe si está al día',
                      'Licencia sin cargar')
    if lic.estado == DATO_A_CORREGIR:
        return Motivo('licencia_dato_a_corregir', AMBAR, TURNO, LICENCIA_SIN_CARGAR_FRENA,
                      f'Licencia de conducción: dato a corregir. {lic.problema or ""} '
                      f'Mientras no se corrija, no se sabe si está al día.'.strip(),
                      'Licencia: dato a corregir')
    raise ValueError(f'estado de licencia desconocido: {lic.estado!r}')


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


def motivo_sin_poder_mirar(clave: str, texto: str) -> Motivo:
    """Lo que el despacho advierte cuando no pudo preguntarle a la flota (sin
    vehículo, adaptador caído). Ámbar y frena: no saber no es «está en orden»."""
    return Motivo(clave, AMBAR, TURNO, True, texto, texto)


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
    m = motivo_licencia(h.licencia, h.hoy)
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
    'DATO_A_CORREGIR', 'BLOQUEAN_SALIDA', 'ROLES_AUTORIZAN_SALIDA_PROHIBIDA',
    'MOTIVO_MINIMO_SALIDA_PROHIBIDA', 'PROHIBIDO', 'NO_SE_SABE', 'ADVERTENCIA',
    'GRAVEDADES', 'gravedad_para_despachar', 'puede_autorizar_salida_prohibida',
    'rol_autoriza_salida_prohibida',
    'ANIO_MINIMO_PAPEL', 'ANIOS_MAXIMOS_ADELANTE', 'DIAS_VIGENCIA_SOAT',
    'CATEGORIAS_LICENCIA', 'LICENCIA_SIN_CARGAR_FRENA', 'fechas_imposibles', 'problemas_de_fechas', 'problemas_de_licencia',
    'Licencia', 'estado_de_licencia', 'motivo_licencia', 'motivo_sin_poder_mirar',
    'papel_cargado', 'motivo_no_puede_reescribir_papel',
    'DETECTOR_CIEGO', 'NIVEL_TURNO_A_REVISAR', 'lineas', 'Papel', 'TareaVencida', 'Hechos', 'Motivo', 'Evaluacion',
    'evaluar', 'color_de', 'orden_de_nivel', 'estado_de_papel', 'papel_vencido',
    'papel_por_vencer', 'motivo_papel', 'motivos_de_danos', 'nivel_de_dano',
    'motivo_inspeccion', 'motivo_preventivo_vencido', 'motivos_de_preventivo',
    'motivo_taller', 'motivos_de_custodia', 'motivos_de_km', 'motivos_de_ficha',
    'texto_ficha_incompleta', 'plural', 'fecha_larga', 'km_legible', 'nombre_papel',
]
