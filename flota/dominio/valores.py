"""
Vocabulario del dominio de flota. Sin I/O, sin framework.

Esto NO es el esquema de persistencia: son las palabras con las que el dominio
razona. Los modelos SQLAlchemy llegan después y deben mapear contra esto, no al
revés.

Alcance deliberado: solo el vocabulario que tocan los siete invariantes de la
tanda 1 (`docs/flota/ESPECIFICACION_T1.md` §6). Los enums de la ficha técnica
—combustible, sistema de frenos, distribución— nacen con su modelo, no antes.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


# ── Regla 4: un estado que puede ser "no sé" se modela con palabras ──────────
#
# SIN_DATO es una cadena, no None y no un booleano. Tres propiedades que se
# eligieron a propósito:
#
#   1. Es VERDADERO en contexto booleano. Un `sin_dato` falsy invita a
#      `valor or 0`, que es exactamente el default optimista que la regla 1
#      prohíbe.
#   2. Serializa a "sin_dato" en JSON sin conversión: lo que ve el conductor en
#      pantalla es la misma palabra que está en la base.
#   3. Nunca es igual a 0 ni a '' ni a None. `SIN_DATO == 0` es False.
#
# Un vehículo sin lecturas de odómetro no tiene 0 km recorridos: no sabemos
# cuántos tiene. Son estados distintos y el tipo lo obliga.
class _SinDato(str):
    __slots__ = ()

    def __repr__(self) -> str:
        return 'SIN_DATO'


SIN_DATO = _SinDato('sin_dato')


class OrigenLectura(str, Enum):
    """De qué gesto operativo nació una lectura de odómetro."""

    ENTREGA        = 'entrega'
    PREOPERACIONAL = 'preoperacional'
    CIERRE_DIA     = 'cierre_dia'
    OT             = 'ot'
    TANQUEO        = 'tanqueo'
    CORRECCION     = 'correccion'


#: Orígenes que el endpoint de **lectura suelta** puede registrar hoy.
#:
#: El enum de arriba nombra los seis gestos que existen en el modelo. Este
#: subconjunto declara cuáles de esos gestos **son este endpoint**. Los otros
#: tres quedan fuera por razones distintas y ninguna es "todavía no lo hicimos":
#:
#:   · `entrega` nace del traspaso atómico, que además cierra la custodia
#:     anterior y abre la nueva. Una lectura suelta que se declare `entrega`
#:     dice que hubo un cambio de turno que nunca ocurrió — y queda
#:     indistinguible de las reales en el histórico que alimenta el CPK.
#:   · `preoperacional` viene de la inspección diaria (tanda 2).
#:   · `ot` viene de la orden de trabajo (tanda 3).
#:
#: Para los dos últimos el problema no es que la pantalla los ofrezca: es que
#: **la lectura apuntaría a un padre que no puede existir**. Un `ot` sin OT es
#: una referencia colgada, y el día que la tanda 3 exista habrá filas viejas que
#: no se pueden reconciliar con ninguna orden.
#:
#: Se habilitan agregándolos acá, no editando la pantalla: el selector se
#: valida contra esta tupla (`tests/flota/test_origen_lectura.py`).
ORIGENES_LECTURA_SUELTA = (
    OrigenLectura.TANQUEO,
    OrigenLectura.CIERRE_DIA,
    OrigenLectura.CORRECCION,
)

#: Por qué cada origen excluido no se puede registrar como lectura suelta.
#:
#: Es **total sobre el complemento** de la tupla de arriba, y un test lo obliga.
#: Total a propósito: la frontera lo indexa directo, sin `.get(x, '')` — un
#: default vacío ahí sería un mensaje de error que no dice nada justo cuando
#: alguien más lo necesita, que es la regla 5 aplicada a un texto.
MOTIVO_ORIGEN_NO_SUELTO = {
    OrigenLectura.ENTREGA:
        'Una entrega se registra en el recibo de turno, que además cierra la '
        'custodia anterior y abre la nueva.',
    OrigenLectura.PREOPERACIONAL:
        'La inspección diaria todavía no existe (tanda 2).',
    OrigenLectura.OT:
        'Las órdenes de trabajo todavía no existen (tanda 3): la lectura '
        'quedaría apuntando a una OT imposible.',
}


#: Máximo de posiciones de llanta que el vocabulario contempla.
#:
#: La flota de hoy son furgones (4) y camiones (6). 12 deja aire para un
#: doble-troque sin abrir la puerta a un valor arbitrario del cliente. Si algún
#: día entra una tractomula, esto se sube **acá** y el CHECK lo sigue: el
#: número está en un lugar con nombre y no repartido en tres archivos.
MAX_POSICIONES_LLANTA = 12

#: Ángulos fijos de la evidencia de estado, en orden. El orden fijo es lo que
#: hace comparable un turno con otro.
ANGULOS_FIJOS = (
    'frontal', 'trasera', 'lateral_izq', 'lateral_der',
    'cajon_abierto', 'interior_cabina', 'tablero',
)

#: `tablero` se pide APARTE, no dentro de la grilla.
#:
#: Es la foto del odómetro: `foto_dato`, mínimo 1600 px, sin recompresión —
#: parámetros distintos de los otros seis, que son `evidencia_estado`. Estaba en
#: los dos sitios: el formulario mostraba «Foto del tablero (obligatoria)» y
#: además un botón `tablero` en la grilla, y el payload mandaba las DOS. Yesid
#: lo reportó el 2026-08-05: *"la opción para cargar la foto del tablero se
#: encuentra en el registro del kilometraje y en el registro general de las
#: partes del vehículo"*.
#:
#: Sigue en `ANGULOS_FIJOS` porque el ángulo `tablero` existe y se guarda; lo
#: que cambia es que la grilla no lo vuelva a pedir.
ANGULO_TABLERO = 'tablero'

#: Los ángulos que la GRILLA muestra. El tablero se pide en su propio campo.
ANGULOS_GRILLA = tuple(a for a in ANGULOS_FIJOS if a != ANGULO_TABLERO)

#: Vocabulario completo de `flota_foto.angulo`.
#:
#: Hasta el 2026-08-03 las fotos se guardaban **sin ángulo**: ocho archivos
#: anónimos colgados de una custodia. El orden tampoco las identificaba, porque
#: el frontend filtra las faltantes antes de enviar — con `frontal` sin tomar,
#: la primera foto del arreglo es `trasera` y todo queda corrido un lugar.
#:
#: La consecuencia práctica: era imposible decir *"el flanco herido está en la
#: llanta trasera derecha"*. Una evidencia que no se puede referir a una parte
#: del vehículo no sirve para atribuir un daño, que es para lo que se toma.
ANGULOS_FOTO = ANGULOS_FIJOS + tuple(
    f'llanta_{i}' for i in range(1, MAX_POSICIONES_LLANTA + 1)
)


def angulos_de_custodia(posiciones_llanta: int) -> tuple:
    """Los ángulos que se le piden a un vehículo con N posiciones de llanta.

    Una sola foto llamada `llantas` no ubica nada: una tuerca floja está en una
    rueda concreta. Por eso el formulario se arma contra la ficha técnica y no
    contra una constante.
    """
    if not 1 <= posiciones_llanta <= MAX_POSICIONES_LLANTA:
        raise ValueError(
            f'posiciones_llanta fuera de rango: {posiciones_llanta} '
            f'(1..{MAX_POSICIONES_LLANTA})'
        )
    return ANGULOS_FIJOS + tuple(
        f'llanta_{i}' for i in range(1, posiciones_llanta + 1))


#: Posiciones de llanta cuando el vehículo todavía no tiene ficha técnica.
#:
#: Las claves son los valores reales de `Vehiculo.tipo`, que es texto libre —
#: no el vocabulario del dominio. Se normaliza a minúsculas sin tildes.
#:
#: **Es un supuesto, no un dato.** Por eso `posiciones_llanta()` devuelve además
#: de dónde salió el número, y la pantalla lo dice. Un default silencioso haría
#: que un camión de 6 posiciones pidiera 4 fotos y nadie notara las dos que
#: faltan — la regla 1 aplicada a la forma del formulario, no al valor de un ítem.
POSICIONES_LLANTA_POR_TIPO = {
    'nhr': 6, 'turbo': 6, 'camion': 6, 'sencillo': 6,
    'van': 4, 'furgon': 4, 'furgon liviano': 4, 'camioneta': 4,
    'moto': 2, 'motocarro': 3,
}
POSICIONES_LLANTA_FALLBACK = 4


def _normalizar(texto: str) -> str:
    tildes = str.maketrans('áéíóúü', 'aeiouu')
    return (texto or '').strip().lower().translate(tildes)


def posiciones_llanta(ficha_posiciones, tipo_vehiculo):
    """Cuántas fotos de llanta pedir, y **de dónde salió ese número**.

    Devuelve `(n, fuente)` con fuente en {'ficha', 'tipo', 'fallback'}. Los tres
    valen números distintos de confianza y la pantalla los muestra distinto:
    con ficha es un dato levantado en campo; por tipo es una inferencia
    razonable; el fallback es no saber.

    Nunca levanta: dejar al conductor sin formulario a las 5 a.m. porque el
    vehículo no tiene ficha sería peor que pedirle cuatro fotos y decírselo.
    """
    if ficha_posiciones:
        return int(ficha_posiciones), 'ficha'
    n = POSICIONES_LLANTA_POR_TIPO.get(_normalizar(tipo_vehiculo))
    if n:
        return n, 'tipo'
    return POSICIONES_LLANTA_FALLBACK, 'fallback'


class CustodioTipo(str, Enum):
    """Quién responde por el vehículo.

    `taller` llega en la tanda 3 con la tabla de talleres. En tanda 1 un
    vehículo en taller queda bajo custodia de la sede que lo envió.
    """

    CONDUCTOR = 'conductor'
    SEDE      = 'sede'


class Ubicacion(str, Enum):
    """DÓNDE queda el vehículo. **No es lo mismo que quién responde.**

    Son dos hechos independientes y mezclarlos es el error que este módulo
    existe para impedir. El caso que lo rompe:

        El camión duerme en la casa del conductor. Si la ubicación arrastra la
        custodia a `sede`, se acaba de **descargar de responsabilidad a la única
        persona que efectivamente tiene el vehículo**. Si amanece rayado, el
        registro dice que estaba bajo custodia de una sede que no lo vio nunca.

    Por eso son dos campos, y por eso hay un CHECK que impide la combinación
    imposible (ver `modelos.Custodia`).
    """

    SEDE          = 'sede'           # patio — custodia de la sede
    TALLER        = 'taller'         # custodia de la sede que lo envió (tanda 1)
    FUERA_DE_SEDE = 'fuera_de_sede'  # sigue siendo del conductor, y exige motivo


#: Ángulos que se piden al ENTREGAR. Cuatro, no trece — asimetría deliberada.
#:
#: Recibir es exhaustivo porque **protege a quien asume** el vehículo: necesita
#: constancia detallada de cómo lo recibió. Entregar es rápido porque cierra el
#: reloj y detecta lo grueso.
#:
#: **El motivo de no pedir las trece al cerrar:** son las 6 p.m., el conductor
#: terminó y quiere irse. La primera semana toma las trece. La tercera saca
#: trece fotos del piso, y eso es evidencia peor que no tener nada — parece
#: registro y no lo es. Cuatro fotos que se toman bien valen más que trece que
#: se falsifican.
#:
#: Estas cuatro son las que detectan un golpe nuevo, que es lo que la entrega
#: tiene que detectar. Un daño en el cajón o un espejo roto no se ven acá: es un
#: costo aceptado a cambio de que el registro se haga de verdad.
ANGULOS_ENTREGA = ('frontal', 'trasera', 'lateral_izq', 'lateral_der')


def custodio_de_ubicacion(ubicacion: 'Ubicacion') -> CustodioTipo:
    """Quién responde cuando el vehículo queda en cada lugar.

    Es la única función que traduce una cosa en la otra, y existe para que la
    traducción esté **en un solo lugar con nombre** en vez de repartida en un
    `if` del adaptador y otro del frontend. La regla 0 aplicada a una relación:
    el mismo criterio en dos sitios diverge.

        sede          → la sede responde
        taller        → la sede que lo envió responde (tanda 1, sin talleres)
        fuera_de_sede → **el conductor sigue respondiendo**

    El tercero es el que importa: no es una excepción que se tolera, es el caso
    con riesgo real, y por eso además exige motivo escrito y sale marcado en el
    tablero de control de flota.
    """
    # `==` y no `is`: ver la nota en `flota/api/custodia.py`. Un enum comparado
    # por identidad depende de que su módulo se haya cargado una sola vez.
    if ubicacion == Ubicacion.FUERA_DE_SEDE:
        return CustodioTipo.CONDUCTOR
    return CustodioTipo.SEDE


class CustodioEstado(str, Enum):
    """Si el custodio declarado se pudo representar contra el maestro.

    `pendiente_sede` no es un error ni un NULL disfrazado: es la sede que el
    WMS todavía no tiene como fila. `almacenes` cubre 5 de los 9 centros
    (medido 2026-08-01) y flota no crea maestros ajenos para tapar el hueco.

    Regla 4 aplicada a una relación: lo que no se puede representar se dice
    con una palabra, y el health lo cuenta para que no se acumule callado.
    """

    RESUELTO       = 'resuelto'
    PENDIENTE_SEDE = 'pendiente_sede'


class QuienPide(str, Enum):
    """Desde dónde se pide recibir un vehículo. Cambia la respuesta, no el dato.

    Un conductor no puede quitarle el turno a otro: la conversación la tienen
    ellos dos. Un admin de zona sí, porque es la única salida cuando alguien se
    fue sin cerrar y el camión tiene que salir a las 5 a.m.
    """

    CONDUCTOR  = 'conductor'
    ADMIN_ZONA = 'admin_zona'


#: Los documentos que el módulo persigue. El vocabulario vive acá y la tabla lo
#: sigue, igual que los ángulos de foto y las clases de foto.
TIPOS_DOCUMENTO = ('soat', 'rtm', 'poliza_rc', 'tarjeta_propiedad')

#: Documentos que NO vencen. La tarjeta de propiedad acredita titularidad
#: mientras el vehículo sea del titular: no tiene fecha de vencimiento, ni en el
#: documento ni en el RUNT.
#:
#: Se descubrió el 2026-08-05, cargando el THP696: el invariante «un documento
#: vigente tiene vencimiento» era falso, y para poder guardar hubo que escribir
#: `2045-08-20` — «vence en 6955 días». Un dato inventado dentro del módulo cuyo
#: lema es que inventarlo es peor que no tenerlo.
#:
#: El invariante no se afloja para todos: se hace condicional al tipo, igual que
#: `custodio_estado` y que las dimensiones de foto.
TIPOS_SIN_VENCIMIENTO = ('tarjeta_propiedad',)


def exige_vencimiento(tipo_documento: str) -> bool:
    """¿Este tipo de documento tiene fecha de vencimiento?

    Sin `.get(tipo, True)`: un tipo desconocido tiene que reventar. Asumir que
    vence es el default seguro para el health, pero acá el costo de callar es
    que alguien agregue un tipo y nadie note que quedó fuera de la regla.
    """
    if tipo_documento not in TIPOS_DOCUMENTO:
        raise ValueError(
            f'tipo de documento desconocido: {tipo_documento!r}. '
            f'Conocidos: {", ".join(TIPOS_DOCUMENTO)}.'
        )
    return tipo_documento not in TIPOS_SIN_VENCIMIENTO


class ClaseFoto(str, Enum):
    """Regla 7: las clases no comparten parámetros.

    EVIDENCIA_ESTADO prueba cómo estaba algo. Se puede degradar.
    FOTO_DATO es la fuente de un número que alguien va a auditar. Degradarla
    es perder el respaldo del número.
    DOCUMENTO_ADJUNTO es un archivo que alguien SUBE, no una foto que la app
    toma: el SOAT llega por correo en PDF y fotografiar la pantalla donde se
    abre es un rodeo que además degrada el original.

    Por qué DOCUMENTO_ADJUNTO no es un FOTO_DATO más: el mínimo de 1600 px
    existe porque un odómetro de seis dígitos a 800×600 no se lee. Un PDF no
    tiene píxeles, y el dato que el documento respalda —el vencimiento— además
    se digita en su propio campo y se compara con él. Meterlo en FOTO_DATO
    obligaría a aflojar el umbral del odómetro, que es lo que ese umbral
    existe para impedir.
    """

    EVIDENCIA_ESTADO  = 'evidencia_estado'
    FOTO_DATO         = 'foto_dato'
    DOCUMENTO_ADJUNTO = 'documento_adjunto'


class EntidadFoto(str, Enum):
    """A qué cuelga una foto. Regla 7: un archivo sin padre es un bug."""

    CUSTODIA_INICIO = 'custodia_inicio'
    CUSTODIA_FIN    = 'custodia_fin'
    ODOMETRO        = 'odometro'
    DOCUMENTO       = 'documento'
    HALLAZGO        = 'hallazgo'


# ── Estructuras de trabajo ───────────────────────────────────────────────────
#
# Mínimas a propósito: solo los campos que los siete invariantes necesitan
# para pronunciarse. No son la tabla.

@dataclass(frozen=True)
class Lectura:
    """Una lectura de odómetro. Append-only: no se edita, se corrige."""

    valor_km: int
    ts: datetime
    origen: OrigenLectura
    autor_usuario_id: int
    motivo_correccion: Optional[str] = None


@dataclass(frozen=True)
class Custodia:
    """Un tramo de responsabilidad sobre un vehículo. `fin_ts=None` = activa."""

    vehiculo_id: int
    custodio_tipo: CustodioTipo
    inicio_ts: datetime
    registrado_por_usuario_id: int
    km_inicio: int
    fin_ts: Optional[datetime] = None
    km_fin: Optional[int] = None
    custodio_conductor_id: Optional[int] = None
    custodio_sede_id: Optional[int] = None
    custodio_estado: CustodioEstado = CustodioEstado.RESUELTO
    linea_base: bool = False
    cierre_forzado: bool = False


@dataclass(frozen=True)
class Dimensiones:
    """Tamaño de una imagen en píxeles."""

    ancho: int
    alto: int

    @property
    def lado_largo(self) -> int:
        return max(self.ancho, self.alto)


@dataclass(frozen=True)
class Foto:
    """Referencia a un archivo. El binario nunca vive en la base (regla 7)."""

    clase: ClaseFoto
    entidad_tipo: EntidadFoto
    entidad_id: int
    storage_ref: str
    hash_sha256: str
    bytes: int
    dimensiones: Dimensiones
    autor_usuario_id: int
    simulado: bool = False
