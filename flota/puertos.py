"""
Puertos del módulo de flota — lo que el dominio y la API necesitan del mundo.

Son `Protocol`, no clases base: el dominio declara la forma que necesita y los
adaptadores la cumplen sin heredar de nada. La dirección de la dependencia
queda invertida a propósito — el dominio no sabe que existe SQLAlchemy.

Sin I/O en este archivo. Solo firmas.
"""
from typing import Dict, List, Optional, Protocol, Union

# Un campo del health es un número medido, una lista medida, o `None`.
#
# `None` significa exactamente una cosa: NO SE PUDO MEDIR TODAVÍA, porque la
# fuente aún no existe. Nunca significa cero. La diferencia importa: 0 documentos
# vencidos es una afirmación sobre la flota; `null` es una afirmación sobre el
# sistema. Confundirlas es lo que convierte un tablero en decoración.
CampoMedido = Union[int, bool, str, List[str], None]


class MedidorDeFlota(Protocol):
    """Lo que el health necesita saber del estado real de la flota.

    Cada método devuelve el valor medido o `None`. Ninguno devuelve 0 por
    defecto — si la tabla que lo alimenta no existe, la respuesta honesta es
    `None`, no un cero que se lee como "todo en orden".

    Si una consulta falla por una razón distinta a "la fuente no existe", el
    error se propaga. Un health que se cae es información; un health que
    responde ceros porque algo reventó adentro es evidencia falsa (regla 5).
    """

    def ambiente(self) -> str:
        """A qué ambiente apunta el sistema: `produccion`, `qa`, `simulacion`..."""
        ...

    def datos_reales(self) -> bool:
        """Si los números que se están mostrando son la operación real."""
        ...

    def vehiculos_activos(self) -> Optional[int]:
        ...

    def fichas_completas(self) -> Optional[int]:
        ...

    def atributos_sin_dato(self) -> Optional[List[str]]:
        """`['TGZ653.distribucion', ...]` — placa.atributo, no id.atributo."""
        ...

    def vehiculos_sin_custodia_activa(self) -> Optional[int]:
        ...

    def custodias_pendiente_sede(self) -> Optional[int]:
        """Custodias cuya sede el WMS todavía no tiene como fila en `almacenes`."""
        ...

    def custodias_cerradas_forzadas(self) -> Optional[int]:
        """Turnos cerrados sin firma del custodio anterior. Mide conducta, no fallas."""
        ...

    def custodias_sin_foto_completa(self) -> Optional[int]:
        ...

    def fotos_pendiente_evidencia(self) -> Optional[int]:
        ...

    def conductores_activos_sin_cuenta(self) -> Optional[int]:
        ...

    def documentos_no_encontrados(self) -> Optional[int]:
        """Documentos que se buscaron y no aparecieron. Distinto de vencido."""
        ...

    def documentos_vencidos(self) -> Optional[int]:
        ...

    def documentos_por_vencer_30d(self) -> Optional[int]:
        ...

    def rutas_historicas_sin_placa(self) -> Optional[int]:
        ...

    def lecturas_dudosas_pendientes(self) -> Optional[int]:
        """Kilometrajes marcados `dudosa` que todavía esperan a una persona.

        Es la deuda del kilómetro: mientras esa lectura sea uno de los extremos
        de un tramo, el CPK de ese vehículo sale `sin_dato` — no bajo, no cero:
        no calculable. **No cuenta las que una corrección posterior ya dejó
        atrás**: pedirle a alguien que confirme un número que el sistema
        reemplazó es hacerle perder el tiempo, y es cómo una cola se abandona.
        """
        ...

    def lecturas_verificadas_30d(self) -> Optional[int]:
        """Kilometrajes que una persona confirmó contra la foto este mes.

        Contador APARTE del de arriba y no un porcentaje: **cero verificadas con
        cero dudosas es una flota sana, y cero verificadas con veinte dudosas es
        una cola que nadie abre.** Un solo número no distingue esos dos estados,
        y son los dos que hay que poder distinguir.
        """
        ...

    def hallazgos_abiertos(self) -> Optional[int]:
        """Daños vivos en toda la flota."""
        ...

    def hallazgos_vencidos(self) -> Optional[int]:
        """Daños que pasaron su fecha límite. Contador APARTE de los abiertos:
        sumarlos esconde el único que urge."""
        ...

    def vehiculos_sin_inspeccion_hoy(self) -> Optional[int]:
        """Vehículos activos que hoy nadie inspeccionó.

        Es el denominador de todo lo demás: un veredicto `apto` sobre cinco
        camiones no dice nada si los otros veinte no se miraron.
        """
        ...

    def inspecciones_incompletas_hoy(self) -> Optional[int]:
        """Inspecciones de hoy con algún ítem en `sin_dato`.

        Contador APARTE de las que faltan: «no se inspeccionó» y «se inspeccionó
        a medias» se corrigen distinto —una hablando con el conductor, la otra
        mirando por qué la pantalla se abandona a la mitad— y un solo total
        esconde la segunda.
        """
        ...

    def segundos_llenado_30d(self) -> Optional[dict]:
        """Cuánto se tarda en contestar la inspección. **Hecho, no umbral.**

        Regla 11: la forma de maximizar este registro sin hacer el trabajo es
        marcar todo óptimo en veinte segundos. Regla 13: no hay una sola
        medición todavía, así que no se publica ningún techo — se publica el
        mínimo con cuántos ítems tenía al lado, que es lo único que permite
        fijar el techo con dato dentro de un mes.
        """
        ...

    # ── La plata que sale (2026-09-02) ───────────────────────────────────
    #
    # Las tres nacen con la tabla, no un mes después. `flota_lectura_odometro`
    # vivió un mes con cero campos en el health y por eso nada avisó de las 26
    # lecturas sin foto ni del salto de 16,3 millones de km.

    def gastos_sin_documento(self) -> Optional[int]:
        """Gastos sin `documento_numero`: no se pueden cruzar con la causación.

        **Es el número que contesta si la operación está entregando las
        facturas**, que hoy se están perdiendo. No es un error del sistema: es
        una medida de la operación, y por eso se cuenta en vez de bloquearse.
        """
        ...

    def tanqueos_sobre_capacidad(self) -> Optional[int]:
        """Tanqueos con más galones de los que el tanque, según la ficha, aguanta.

        **No afirma que alguien se haya llevado nada** (regla 2): afirma que dos
        datos no pueden ser los dos ciertos. Los tanqueos de vehículos sin
        capacidad declarada **no cuentan acá y no cuentan como limpios**: son
        `sin_dato`, y se ven en `tanqueos_sin_capacidad_declarada`.
        """
        ...

    def tanqueos_sin_capacidad_declarada(self) -> Optional[int]:
        """Tanqueos que el detector de arriba **no pudo mirar**.

        Va aparte y no sumado a cero: sin este campo, un parque entero sin
        capacidad en la ficha se vería idéntico a un parque sin un solo exceso
        — que es exactamente cómo un detector se apaga sin que nadie lo note.
        """
        ...

    def cpk_mes(self) -> Optional[List[dict]]:
        """El costo por kilómetro del mes en curso, **por vehículo y como hecho**.

        Una lista y no un promedio de la flota: el canon dice que el CPK no
        compara vehículos, y un promedio entre un NHR y un motocarro mide la
        composición del parque. Cada entrada trae `pesos` y `km` junto al
        número, porque un CPK suelto no se puede auditar.
        """
        ...

    # ── Taller y garantía (2026-09-02) ───────────────────────────────────
    #
    # Los tres nacen con la tabla, no un mes después, y **no se suman**:
    # contestan preguntas distintas y sumarlos escondería la peor. Es la misma
    # disciplina de `hallazgos_vencidos` contra `hallazgos_abiertos` y la
    # lección de los 639 avisos conocidos.

    def ot_abiertas(self) -> Optional[int]:
        """Órdenes de trabajo sin cerrar: camiones que están en el taller ahora.

        **No afirma que estén demorados** — no hay todavía una sola medición de
        cuánto tarda una visita, así que no hay umbral que publicar (regla 13).
        Afirma cuántos camiones no están en la calle, que es la pregunta que hoy
        no tiene dónde contestarse.
        """
        ...

    def trabajos_sin_factura(self) -> Optional[int]:
        """Trabajos hechos, vehículo devuelto, y **la factura no llegó**.

        Es el número que el plan pide contar en vez de exigir: la OT no lleva
        valor porque el camión entra hoy y la factura llega el 30, y el precio de
        esa decisión es que la ausencia se pueda medir.

        Cuenta solo los de órdenes **cerradas**. Una orden abierta sin factura no
        es una deuda: es un camión que todavía está adentro, y contarlo dejaría
        este campo permanentemente en rojo — que es cómo un tablero se deja de
        mirar. Los que están adentro se ven en `ot_abiertas`, aparte.
        """
        ...

    def garantias_vigentes(self) -> Optional[int]:
        """Reparaciones que todavía están dentro del plazo que la factura pactó.

        **Es el campo que dice si la fase 2 está haciendo algo.** Si vale 0
        durante meses, la búsqueda que evita pagar dos veces no tiene sobre qué
        pronunciarse — y eso es una conclusión distinta de «no hay OT», que se
        lee en el campo de arriba.

        Solo cuenta las que cubren de verdad: una garantía que **no se pudo
        juzgar** —sin odómetro con qué comparar— no entra acá. Contarla sería el
        default optimista de la regla 1 en el número que decide si se reclama.
        """
        ...

    # ── Llantas (2026-09-02) ─────────────────────────────────────────────
    #
    # Los cuatro nacen con la tabla. `flota_lectura_odometro` vivió un mes con
    # cero campos acá y por eso nada avisó de las 26 lecturas sin foto ni del
    # salto de 16,3 millones de km. Un campo que nadie mira es el defecto que
    # este módulo lleva toda la semana arreglando.

    def posiciones_sin_llanta(self) -> Optional[int]:
        """Posiciones de llanta de la flota que **no tienen ninguna montada**.

        **Casi siempre significa que la llanta está puesta y nadie la
        registró**, no que el camión ande sin rueda. Es la medida de cuánto le
        falta al inventario para describir el vehículo real — y el día del corte
        de producción es el número que va a decir cuántos montajes hay que
        volver a registrar, porque `flota_montaje_llanta` se vacía y
        `flota_llanta` no.

        Solo cuenta vehículos **con ficha**: `posiciones_llanta` vive ahí. Los
        que no la tienen se cuentan aparte, en `vehiculos_sin_posiciones_llanta`.
        """
        ...

    def vehiculos_sin_posiciones_llanta(self) -> Optional[int]:
        """Vehículos activos que el contador de arriba **no pudo mirar**.

        Va aparte y no sumado a cero, por el mismo motivo que
        `tanqueos_sin_capacidad_declarada`: sin ficha no se sabe cuántas
        posiciones tiene el vehículo, y contarlo como «0 posiciones sin llanta»
        haría que un parque entero sin ficha se viera idéntico a uno con todas
        las llantas registradas. Es cómo se apaga un detector sin que nadie lo
        note.
        """
        ...

    def llantas_montadas(self) -> Optional[int]:
        """Montajes vigentes en toda la flota (`fin_ts IS NULL`).

        El numerador del anterior. Los dos separados y no un porcentaje: «0 de
        0» (una flota sin ficha) y «0 de 24» (nadie registró nada) dan el mismo
        porcentaje y son dos estados que hay que poder distinguir.
        """
        ...

    def km_por_posicion(self) -> Optional[List[dict]]:
        """Kilómetros de las llantas ya desmontadas, **por vehículo y posición**.

        Un HECHO, no una vida útil (regla 13): **no hay una sola llanta medida
        en esta flota**. Cada entrada trae cuántas vidas completas se midieron en
        esa posición, cuáles fueron y cuántas faltan para poder publicar una
        mediana con dato en vez de a ojo.

        Por vehículo y posición, nunca agregado sobre la flota: el modo de fallo
        caro es que se gasten MAL —desalineación, presión, un eje que come el
        flanco interno— y eso solo se ve por posición. Un promedio de flota
        mediría la composición del parque, que es lo mismo que el canon del CPK
        prohíbe.

        `[]` significa «todavía no se desmontó una sola llanta», y no es un cero:
        `None` es «no existe la tabla».
        """
        ...

    # ── Preventivo (2026-09-02) ──────────────────────────────────────────
    #
    # `distribucion_km_cambio` está cargado en la base desde la tanda 1 y nadie
    # lo lee. Estos campos son el lector.
    #
    # **Los cuatro contadores van separados y ninguno se suma a otro**, y no es
    # simetría estética: cada uno se corrige llamando a una persona distinta.
    # Vencida → al taller. Por vencer → a conseguir el repuesto. Sin línea base
    # → a quien sepa cuándo se hizo la última vez. Sin intervalo → al
    # concesionario. Un solo total no dice a quién llamar, que es la lección de
    # los 639 avisos conocidos.

    def tareas_vencidas(self) -> Optional[int]:
        """Tareas preventivas que pasaron su kilometraje de cambio.

        **No lleva umbral y no lo necesita**: el kilometraje lo dijo el
        fabricante y está en la ficha. Lo único que hace este número es
        comparar el odómetro contra él, que es exactamente lo que nadie estaba
        haciendo.

        No cuenta las tareas retiradas (`activo=False`): una tarea que alguien
        decidió que no aplica no está vencida, está retirada.
        """
        ...

    def tareas_por_vencer(self) -> Optional[int]:
        """Tareas que llegan al cambio dentro de la ventana de anticipación.

        **Es el único campo de flota que depende de un umbral declarado**
        (`DIAS_AVISO_PREVENTIVO`), y depende además de que el km/día del
        vehículo se haya podido medir. Sin ritmo medido una tarea NO cae acá:
        se queda en al día con `dias_estimados = sin_dato`, porque «no sé
        cuándo» no es «pronto».
        """
        ...

    def tareas_sin_linea_base(self) -> Optional[int]:
        """Tareas con intervalo conocido y **ninguna ejecución registrada**.

        No están al día ni vencidas: no hay contra qué comparar. Es la decisión
        que hace que un preventivo recién sembrado no dispare cuarenta avisos el
        día uno — y es también el trabajo pendiente más barato de la fase, porque
        se resuelve preguntando cuándo se hizo la última vez.
        """
        ...

    def tareas_sin_intervalo(self) -> Optional[int]:
        """Tareas que los contadores de arriba **no pudieron mirar**.

        La ficha dice qué aceite lleva el motor y no cada cuántos kilómetros se
        cambia: ese número no existe en ninguna columna. Sin este campo, un
        parque entero sin intervalos levantados se vería idéntico a uno sin una
        sola tarea vencida — que es exactamente cómo un detector se apaga sin
        que nadie lo note. Mismo motivo que
        `tanqueos_sin_capacidad_declarada`.
        """
        ...

    def km_dia_por_vehiculo(self) -> Optional[List[dict]]:
        """El ritmo de uso de cada vehículo. **Un hecho, y el que descarga la
        regla 13.**

        `DIAS_AVISO_PREVENTIVO` es el único umbral de la fase y hoy no hay una
        sola medición de km/día con la que fijarlo. Este campo publica el hecho
        —con `n`, `dias` y la marca de confianza al lado— para poder fijarlo con
        dato dentro de un mes en vez de a ojo hoy, igual que
        `salto_km_maximo_30d`.

        Una lista y no un promedio de flota: un motocarro de reparto urbano y un
        NHR de ruta intermunicipal no se comparan por km/día, por el mismo
        motivo por el que no se comparan por CPK.
        """
        ...


class AlmacenDeFotos(Protocol):
    """Object storage. La base guarda referencia, hash, bytes y dimensiones.

    Nunca el binario — la columna `Text` con base64 de `recaudo_entrega` es
    justamente lo que este módulo no hereda.
    """

    def guardar(self, contenido: bytes, mime: str) -> str:
        """Devuelve `storage_ref`. Levanta si no pudo guardar."""
        ...

    def dimensiones(self, storage_ref: str) -> Dict[str, int]:
        ...


class CanalDeAviso(Protocol):
    """Por dónde sale un aviso hacia una persona.

    Detrás de un `Protocol` para que el servicio que decide QUÉ avisar no sepa
    que existe Gupshup — y para que el doble de pruebas sea un objeto, no un
    monkeypatch. El doble deja rastro propio (`simulado=True` en la fila, regla
    8): `CanalNotificacionDev` ya costó una hora de creer que 1.485 personas
    habían recibido un cobro que nunca salió.

    `enviar` devuelve el id que asignó el proveedor, o levanta. **No devuelve
    `None` ni `False` ante un fallo**: un canal que degrada hacia algo que se
    parece al éxito es la regla 5, y acá el costo es que un hallazgo vencido se
    quede quieto creyendo que se avisó.
    """

    def enviar(self, telefono: str, plantilla: str,
               parametros: List[str]) -> str:
        """Devuelve el id del proveedor. Levanta `AvisoNoEnviado` si no salió."""
        ...

    @property
    def simulado(self) -> bool:
        """¿Este canal manda de verdad? La fila lo guarda, no solo el log."""
        ...


__all__ = ['CampoMedido', 'MedidorDeFlota', 'AlmacenDeFotos', 'CanalDeAviso']
