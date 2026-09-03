"""
Persistencia de la tanda 1. Capa de adaptador: acá sí se importa SQLAlchemy.

**Los invariantes los impone la base, no solo el código.** Si un `INSERT` crudo
—desde psql, desde una migración, desde un script de alguien— puede violar un
invariante, el modelo está incompleto. Las políticas del dominio
(`flota/dominio/`) y las restricciones de acá dicen lo mismo dos veces a
propósito: el dominio para dar un error legible al usuario, la base para que no
exista camino que lo esquive.

Lo que cada invariante consigue de la base:

| # | Invariante | Mecanismo | Alcance |
|---|---|---|---|
| 1 | Monotonía del odómetro | trigger `BEFORE INSERT` + append-only | total |
| 2 | 0 o 1 custodia activa | índice único parcial | total |
| 3 | Cobertura temporal | trigger de no-solape | **parcial — ver nota** |
| 4 | Arco exclusivo | `CHECK` | total |

**Nota sobre el 3.** Un hueco de cobertura se produce por una escritura que NO
ocurre, y una restricción solo puede juzgar escrituras que sí ocurren: no se
puede constreñir una ausencia. Lo que la base sí impide es lo que un `INSERT`
puede romper —solapamiento y viaje en el tiempo—; el hueco se DETECTA con
`dominio.custodia.huecos_de_cobertura` y se cuenta en el health. Lo que lo
previene en la práctica es que el traspaso sea atómico.

Los triggers van como DDL colgada del `after_create` de cada tabla y no dentro
de la migración, para que existan también en la SQLite de los tests. Un
invariante que la base impone en producción pero no en tests es un invariante
que nadie ejerció.
"""
from datetime import datetime

from sqlalchemy import DDL, event, select

from app.extensions import db
from flota.dominio.costos import CATEGORIAS_GASTO as _CATEGORIAS_GASTO
from flota.dominio.costos import ESTADOS_TANQUE as _ESTADOS_TANQUE
from flota.dominio.costos import ORIGENES_COSTO as _ORIGENES_COSTO
from flota.dominio.hallazgo import EstadoHallazgo as _EstadoHallazgo
from flota.dominio.inspeccion import RESPUESTAS as _RESPUESTAS_INSP
from flota.dominio.inspeccion import VEREDICTOS as _VEREDICTOS_INSP
from flota.dominio.llantas import \
    MOTIVOS_DESMONTAJE as _MOTIVOS_DESMONTAJE
from flota.dominio.taller import ESTADOS_OT as _ESTADOS_OT
from flota.dominio.taller import GARANTIA_DECLARADA as _GARANTIA_DECLARADA
from flota.dominio.taller import SISTEMAS as _SISTEMAS
from flota.dominio.taller import TIPOS_OT as _TIPOS_OT
from flota.dominio.preventivo import TIPOS_TAREA as _TIPOS_TAREA
from flota.dominio.valores import FUENTES as _FUENTES
from flota.dominio.valores import (ANGULOS_FOTO, TIPOS_DOCUMENTO,
                                   TIPOS_SIN_VENCIMIENTO, ClaseFoto, Confianza,
                                   MAX_POSICIONES_LLANTA, OrigenLectura)

# ── Vocabularios permitidos ──────────────────────────────────────────────────
# Se declaran acá y se convierten en CHECK. Un enum de Python que la base no
# conoce es una convención; un CHECK es una garantía.

COMBUSTIBLE      = ('gasolina', 'diesel', 'sin_dato')
SISTEMA_FRENOS   = ('hidraulico', 'aire_sobre_hidraulico', 'aire_full', 'sin_dato')
SI_NO            = ('si', 'no', 'sin_dato')
DISTRIBUCION     = ('correa', 'cadena', 'sin_dato')
TRANSMISION_FINAL = ('cadena', 'correa', 'cardan', 'sin_dato')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Subio a
# `dominio/valores.py` el 2026-09-02, cuando el preventivo le dio un segundo
# consumidor: la tarea HEREDA la procedencia del campo que la sembro
# (`distribucion_km_cambio` -> `distribucion_fuente`), y el dominio no puede
# importar del adaptador. Una copia literal habria dejado las dos tuplas
# separandose en la primera fuente nueva.
FUENTE           = _FUENTES
TIPO_DOCUMENTO   = TIPOS_DOCUMENTO
ESTADO_DOCUMENTO = ('vigente', 'no_encontrado')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Escrito
# asi y no a mano porque el 2026-09-01 se le agrego 'hallazgo' y la copia
# literal de esta linea habria dejado el CHECK sin el valor nuevo — con el
# modelo aceptandolo y la base rechazandolo.
ORIGEN_LECTURA   = tuple(o.value for o in OrigenLectura)
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Regla 4 del
# modulo — un estado que puede ser «no se» se modela con palabras, y estas tres
# son las de `Confianza`. Escrito asi y no a mano por lo mismo que
# ORIGEN_LECTURA: una copia literal deja el CHECK sin el valor nuevo.
CONFIANZA        = tuple(c.value for c in Confianza)
CUSTODIO_TIPO    = ('conductor', 'sede')
CUSTODIO_ESTADO  = ('resuelto', 'pendiente_sede')
UBICACION        = ('sede', 'taller', 'fuera_de_sede')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Escrito
# asi y no a mano para que agregar una clase no exija acordarse de esta linea.
CLASE_FOTO       = tuple(c.value for c in ClaseFoto)
ENTIDAD_FOTO     = ('custodia_inicio', 'custodia_fin', 'odometro', 'documento', 'hallazgo')
ESTADO_FOTO      = ('ok', 'pendiente_evidencia')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves.
ANGULO_FOTO      = ANGULOS_FOTO
CRITICIDAD       = ('bloqueante', 'mayor', 'menor')
PERIODICIDAD     = ('diaria', 'semanal')
APLICA_A         = ('furgon_liviano', 'camion', 'motocarro')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Escrito
# asi y no a mano por lo mismo que ORIGEN_LECTURA: una copia literal deja el
# CHECK sin el valor nuevo el dia que alguien agregue una respuesta, con el
# modelo aceptandola y la base rechazandola.
RESPUESTA_ITEM   = tuple(str(r) for r in _RESPUESTAS_INSP)
VEREDICTO        = tuple(_VEREDICTOS_INSP)
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Armados
# desde `flota.dominio.costos` por el mismo motivo que ORIGEN_LECTURA: una copia
# literal deja el CHECK sin el valor nuevo el dia que alguien agregue una
# categoria, con el modelo aceptandola y la base rechazandola.
CATEGORIA_GASTO  = tuple(_CATEGORIAS_GASTO)
ORIGEN_COSTO     = tuple(_ORIGENES_COSTO)
ESTADO_TANQUE    = tuple(_ESTADOS_TANQUE)
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Armado desde
# `flota.dominio.llantas` por el mismo motivo que ORIGEN_LECTURA, y aca el costo
# de una copia que se quede atras es concreto: `desgaste_irregular` es la palabra
# que hace visible un eje que come flancos, y si el CHECK no la conoce la fila no
# entra — con el modelo aceptandola y la base rechazandola.
MOTIVO_DESMONTAJE = tuple(_MOTIVOS_DESMONTAJE)
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves. Armados
# desde `flota.dominio.taller` por el mismo motivo que ORIGEN_LECTURA: una copia
# literal deja el CHECK sin el valor nuevo el dia que alguien agregue un
# sistema, con el modelo aceptandolo y la base rechazandolo — y en el caso de
# SISTEMA el costo es peor que un 500: la busqueda de garantia vigente no
# encontraria nunca ese sistema, y la reparacion se paga dos veces.
TIPO_OT          = tuple(_TIPOS_OT)
ESTADO_OT        = tuple(_ESTADOS_OT)
SISTEMA          = tuple(_SISTEMAS)
GARANTIA_DECL    = tuple(_GARANTIA_DECLARADA)

# Regla 6: el plazo se calcula al nacer, no se elige a mano. Vive acá, en un
# solo lugar, y el hallazgo lo hereda — si un día alguien puede escribir "este
# bloqueante para el viernes", la severidad dejó de significar algo.
DIAS_DE_PLAZO = {'bloqueante': 0, 'mayor': 7, 'menor': 30}

LADO_LARGO_MINIMO_FOTO_DATO = 1600

#: Para los CHECK. Se arma del dominio para que agregar un tipo que no vence
#: no exija acordarse de dos literales SQL sueltos.
_SIN_VENCIMIENTO_SQL = '(%s)' % ', '.join(
    f"'{x}'" for x in TIPOS_SIN_VENCIMIENTO)


def _en(columna, valores):
    """CHECK de pertenencia a un vocabulario cerrado."""
    lista = ', '.join(f"'{v}'" for v in valores)
    return db.CheckConstraint(f'{columna} IN ({lista})', name=f'ck_flota_{columna}')


# ═══════════════════════════════════════════════════════════════════════════
# ficha_tecnica — 1:1 con vehículo
# ═══════════════════════════════════════════════════════════════════════════

class FichaTecnica(db.Model):
    """Ficha del vehículo. La PK es el vehículo: no puede haber dos.

    `km_inicial` es el ancla del sistema — todo kilometraje posterior se lee
    contra este. Por eso es NOT NULL y por eso el levantamiento de campo es la
    semilla de datos de la tanda: sin ancla no hay CPK ni preventivo por km.
    """

    __tablename__ = 'flota_ficha_tecnica'

    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'), primary_key=True)

    # Cada uno de estos puede ser "no sé", y "no sé" es una palabra (regla 4).
    combustible    = db.Column(db.String(20), nullable=False, server_default='sin_dato')
    sistema_frenos = db.Column(db.String(30), nullable=False, server_default='sin_dato')
    tiene_freno_escape = db.Column(db.String(10), nullable=False, server_default='sin_dato')
    distribucion   = db.Column(db.String(10), nullable=False, server_default='sin_dato')

    # Cómo llega la fuerza a la rueda. NO es lo mismo que `distribucion`, que es
    # la sincronización del motor: un motocarro puede tener distribución por
    # cadena Y transmisión final por cadena, y son dos mantenimientos distintos.
    # Sin este campo no se puede derivar la lubricación de cadena, que en un
    # motocarro es tarea de rutina y en un camión con cardán no existe.
    transmision_final = db.Column(db.String(10), nullable=False, server_default='sin_dato')

    distribucion_km_cambio  = db.Column(db.Integer, nullable=True)
    norma_emisiones         = db.Column(db.Text, nullable=True)
    aceite_motor_spec       = db.Column(db.Text, nullable=False, server_default='sin_dato')
    aceite_motor_litros     = db.Column(db.Numeric(6, 2), nullable=True)
    aceite_caja_spec        = db.Column(db.Text, nullable=True)
    aceite_diferencial_spec = db.Column(db.Text, nullable=True)
    refrigerante_spec       = db.Column(db.Text, nullable=True)

    # 4 en N300, 6 en camiones. Se cuenta a la vista: no admite "no sé".
    posiciones_llanta = db.Column(db.Integer, nullable=False)
    medida_llanta     = db.Column(db.Text, nullable=True)
    tiene_furgon      = db.Column(db.Boolean, nullable=False, server_default='0')

    km_inicial    = db.Column(db.Integer, nullable=False)
    km_inicial_ts = db.Column(db.DateTime, nullable=False)

    # ── Capacidad del tanque (fase 1, 2026-09-01) ────────────────────────────
    #
    # El ÚNICO dato que hace falta para el primer detector de la fase: un tanque
    # de 15 galones que recibe 22 no es error de medición. No necesita umbral, ni
    # canon, ni un mes de historia — por eso este campo se construye hoy y el
    # segundo detector (caída de rendimiento contra la propia historia del
    # vehículo) queda como deuda declarada en ESTADO.md.
    #
    # **Nullable a propósito, y es la decisión que hace que el detector valga.**
    # Un `server_default` numérico —60 galones, digamos— le pondría a las seis
    # fichas de producción una capacidad que nadie midió, y el detector estaría
    # comparando galones reales contra un número inventado: dispararía sobre
    # operación sana y se apagaría en una semana. Con NULL,
    # `costos.excede_capacidad` devuelve SIN_DATO —«no hay contra qué revisar»—
    # y jamás `False`, que sería «se revisó y está bien».
    capacidad_tanque_galones = db.Column(db.Numeric(6, 2), nullable=True)
    #: De dónde salió la capacidad. Mismo patrón que `distribucion_fuente` y
    #: `frenos_fuente`, y por el mismo motivo: este número va a producir un aviso
    #: sobre un tanqueo concreto. Un dato con autoridad sin procedencia es
    #: tradición oral con formato de columna, y acá la tradición oral termina en
    #: una conversación incómoda con un conductor.
    capacidad_tanque_fuente = db.Column(db.String(30), nullable=False,
                                        server_default='sin_dato')

    # Procedencia SOLO de los dos atributos que disparan tareas de seguridad.
    # Es el canon aplicado a un dato de ficha: un dato con autoridad lleva de
    # dónde salió. Los demás campos no la llevan porque no deciden nada solos.
    distribucion_fuente        = db.Column(db.String(30), nullable=False, server_default='sin_dato')
    distribucion_verificado_ts = db.Column(db.DateTime, nullable=True)
    frenos_fuente              = db.Column(db.String(30), nullable=False, server_default='sin_dato')
    frenos_verificado_ts       = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        _en('combustible', COMBUSTIBLE),
        _en('sistema_frenos', SISTEMA_FRENOS),
        _en('tiene_freno_escape', SI_NO),
        _en('distribucion', DISTRIBUCION),
        _en('transmision_final', TRANSMISION_FINAL),
        _en('distribucion_fuente', FUENTE),
        _en('frenos_fuente', FUENTE),
        db.CheckConstraint('posiciones_llanta > 0', name='ck_flota_posiciones_llanta'),
        db.CheckConstraint('km_inicial >= 0', name='ck_flota_km_inicial'),
        # Un dato conocido sin procedencia es tradición oral con formato de
        # columna: si se sabe la distribución, se sabe quién lo dijo.
        db.CheckConstraint(
            "distribucion = 'sin_dato' OR distribucion_fuente <> 'sin_dato'",
            name='ck_flota_distribucion_con_procedencia',
        ),
        db.CheckConstraint(
            "sistema_frenos = 'sin_dato' OR frenos_fuente <> 'sin_dato'",
            name='ck_flota_frenos_con_procedencia',
        ),
        _en('capacidad_tanque_fuente', FUENTE),
        # Cero galones de capacidad no es un tanque pequeño: es una fila mal
        # cargada que haría que TODO tanqueo de ese vehículo excediera.
        db.CheckConstraint(
            'capacidad_tanque_galones IS NULL OR capacidad_tanque_galones > 0',
            name='ck_flota_capacidad_tanque_positiva',
        ),
        # La misma exigencia que la distribución y los frenos: si se sabe, se
        # sabe quién lo dijo. Y al revés — sin capacidad no puede haber fuente:
        # una procedencia colgada de un dato ausente afirma un levantamiento que
        # no ocurrió, y es justo lo que haría creer que el detector está armado.
        db.CheckConstraint(
            "(capacidad_tanque_galones IS NULL "
            "  AND capacidad_tanque_fuente = 'sin_dato') OR "
            "(capacidad_tanque_galones IS NOT NULL "
            "  AND capacidad_tanque_fuente <> 'sin_dato')",
            name='ck_flota_capacidad_tanque_con_procedencia',
        ),
    )

    def atributos_sin_dato(self) -> list:
        """Los campos de esta ficha que están declarados como desconocidos."""
        return [
            campo for campo in
            ('combustible', 'sistema_frenos', 'tiene_freno_escape', 'distribucion',
             'transmision_final')
            if getattr(self, campo) == 'sin_dato'
        ]

    def completa(self) -> bool:
        return not self.atributos_sin_dato()


# ═══════════════════════════════════════════════════════════════════════════
# documento_vehiculo
# ═══════════════════════════════════════════════════════════════════════════

class DocumentoVehiculo(db.Model):
    """SOAT, tecnomecánica, póliza, tarjeta de propiedad.

    En tanda 1 solo reporta en el health (alerta a 30 y 15 días). No bloquea
    nada: medir → corregir → imponer, en ese orden.
    """

    __tablename__ = 'flota_documento_vehiculo'

    id          = db.Column(db.Integer, primary_key=True)
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'), nullable=False, index=True)
    tipo        = db.Column(db.String(20), nullable=False)
    numero      = db.Column(db.String(50), nullable=False, server_default='')
    entidad     = db.Column(db.String(100), nullable=False, server_default='')

    # Nullable desde el 2026-08-01: un documento `no_encontrado` no tiene fechas.
    fecha_expedicion  = db.Column(db.Date, nullable=True)
    fecha_vencimiento = db.Column(db.Date, nullable=True)
    foto_id     = db.Column(db.Integer, db.ForeignKey('flota_foto.id'), nullable=True)

    # `no_encontrado` NO es un campo vacío: es una afirmación. Un vehículo sin
    # SOAT vigente localizable es un hallazgo bloqueante, y si eso se registra
    # como "sin dato" queda indistinguible de "todavía no lo hemos mirado".
    #
    # Es la misma forma que `custodia.custodio_estado`: el invariante no se
    # afloja, se hace condicional a un estado que a su vez está constreñido.
    estado = db.Column(db.String(20), nullable=False, server_default='vigente')

    # Sin backref: `Vehiculo` vive en `app/` y este módulo no le agrega
    # atributos — la dirección de la dependencia es flota → app, nunca al revés.
    vehiculo = db.relationship('Vehiculo', lazy=True)

    __table_args__ = (
        _en('tipo', TIPO_DOCUMENTO),
        _en('estado', ESTADO_DOCUMENTO),
        # El vencimiento se exige SALVO para los tipos que no vencen. La tarjeta
        # de propiedad acredita titularidad y no caduca: exigirle fecha obligaba
        # a inventar una para poder guardar (el 2026-08-05 quedo '2045-08-20 --
        # vence en 6955 dias'). El invariante no se afloja para todos: se hace
        # condicional al tipo, como `custodio_estado` y como las dimensiones de
        # foto.
        db.CheckConstraint(
            "(estado = 'vigente' AND fecha_expedicion IS NOT NULL "
            " AND length(trim(numero)) > 0 AND length(trim(entidad)) > 0 "
            " AND (fecha_vencimiento IS NOT NULL OR tipo IN %s)) OR "
            "(estado = 'no_encontrado' AND fecha_expedicion IS NULL "
            " AND fecha_vencimiento IS NULL)" % _SIN_VENCIMIENTO_SQL,
            name='ck_flota_doc_estado_coherente',
        ),
        # Y al reves: un tipo que no vence tampoco puede TENER vencimiento. Sin
        # esto, la fila inventada de ayer seguiria siendo legal y el aviso de
        # renovacion la perseguiria como si fuera real.
        db.CheckConstraint(
            "tipo NOT IN %s OR fecha_vencimiento IS NULL" % _SIN_VENCIMIENTO_SQL,
            name='ck_flota_doc_sin_vencimiento',
        ),
        db.CheckConstraint(
            'fecha_vencimiento IS NULL OR fecha_vencimiento >= fecha_expedicion',
            name='ck_flota_doc_vigencia'),
        db.UniqueConstraint('vehiculo_id', 'tipo', 'numero', name='uq_flota_doc'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# lectura_odometro — append-only
# ═══════════════════════════════════════════════════════════════════════════

class LecturaOdometro(db.Model):
    """Append-only. Una lectura no se edita: se corrige con un registro nuevo.

    El `CHECK` de motivo obligatorio es el invariante 1 en su mitad declarativa;
    la monotonía es la otra mitad y va por trigger, porque compara contra otras
    filas y ningún `CHECK` puede hacerlo.

    ## `confianza` — el tercer estado (2026-09-02)

    `validar_lectura` contesta «¿entra o no entra?», y es binaria: **los
    16.697.948 km del THP696 entraron**, porque crecer es lo único que la
    monotonía exige. Una vez adentro, ese número es indistinguible de uno bueno
    para cualquier cálculo aguas abajo. `confianza` es la segunda pregunta —
    «¿me puedo apoyar en este número?»— y se contesta AL NACER la fila, en el
    `before_insert` de más abajo, con `dominio.odometro.confianza_al_nacer`.

    Regla 4 del módulo: se escribe con palabras, no con un booleano ni con NULL.

    **`verificada` no la escribe ningún automatismo.** Un trigger `BEFORE
    INSERT` lo impone en la base: ninguna fila nace verificada, venga de donde
    venga. La única vía es la cola de verificación
    (`flota/adaptadores/verificacion.py`), que es un humano con la foto al lado.
    """

    __tablename__ = 'flota_lectura_odometro'

    id          = db.Column(db.Integer, primary_key=True)
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'), nullable=False, index=True)
    valor_km    = db.Column(db.Integer, nullable=False)
    ts          = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    origen      = db.Column(db.String(20), nullable=False)
    foto_id     = db.Column(db.Integer, db.ForeignKey('flota_foto.id'), nullable=True)
    autor_usuario_id  = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    motivo_correccion = db.Column(db.Text, nullable=True)

    # NOT NULL y **sin `server_default`** a propósito. Un default en la columna
    # sería el default optimista que la regla 5 prohíbe: un `INSERT` crudo que
    # no dijera nada quedaría `declarada` en silencio. Sin default, ese INSERT
    # falla ruidosamente y el único camino que llena la columna es la política.
    confianza     = db.Column(db.String(20), nullable=False)
    #: Por qué está en duda, acumulando TODOS los motivos. Lo escribe
    #: `confianza_al_nacer`; quien revise la cola necesita verlos todos, porque
    #: quedarse con el primero es cómo un canal de avisos se vuelve ilegible.
    motivo_dudosa = db.Column(db.Text, nullable=True)
    #: Quién y cuándo miró la foto y dijo que sí. Las dos juntas o ninguna: una
    #: verificación sin nombre no es una verificación, es una casilla.
    verificada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    verificada_ts = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        _en('origen', ORIGEN_LECTURA),
        _en('confianza', CONFIANZA),
        db.CheckConstraint('valor_km >= 0', name='ck_flota_valor_km'),
        # Una corrección sin motivo es indistinguible de un error de digitación.
        db.CheckConstraint(
            "origen <> 'correccion' OR "
            "(motivo_correccion IS NOT NULL AND length(trim(motivo_correccion)) > 0)",
            name='ck_flota_correccion_con_motivo',
        ),
        # Una `dudosa` sin motivo escrito no la puede revisar nadie: quien abra
        # la cola vería un número marcado y ninguna razón para desconfiar de él,
        # y a la tercera vez deja de abrirla. Mismo criterio que la corrección.
        db.CheckConstraint(
            "confianza <> 'dudosa' OR "
            "(motivo_dudosa IS NOT NULL AND length(trim(motivo_dudosa)) > 0)",
            name='ck_flota_dudosa_con_motivo',
        ),
        # Y al revés: `declarada` significa «nada evidente la contradice». Una
        # fila que dijera eso Y trajera un motivo de duda afirmaría las dos
        # cosas a la vez. `verificada` SÍ conserva el motivo: es el registro de
        # por qué entró a la cola, y borrarlo al confirmar perdería la única
        # explicación de qué fue lo que alguien miró.
        db.CheckConstraint(
            "confianza <> 'declarada' OR motivo_dudosa IS NULL",
            name='ck_flota_declarada_sin_motivo',
        ),
        # Las dos direcciones, y la segunda es la que importa: sin ella, una
        # fila podría llevar autor y fecha de verificación sin estar verificada
        # —un nombre colgado de nada—, y el health contaría verificaciones que
        # no afirman nada.
        db.CheckConstraint(
            "(confianza = 'verificada' AND verificada_por_usuario_id IS NOT NULL "
            " AND verificada_ts IS NOT NULL) OR "
            "(confianza <> 'verificada' AND verificada_por_usuario_id IS NULL "
            " AND verificada_ts IS NULL)",
            name='ck_flota_verificada_con_autor',
        ),
    )

    def a_dominio(self):
        """La estructura que `flota.dominio.odometro` sabe juzgar.

        **Es el único traductor de fila a `Lectura`**, y por eso es un método y
        no una función suelta en cada adaptador. El docstring de
        `dominio.valores.Lectura` ya lo pedía por escrito: el default
        `DECLARADA` del dataclass existe solo para la lectura *propuesta* —la
        que todavía no está en la base—, y quien lee una fila persistida tiene
        que pasar la confianza real. Dos cargadores con criterios distintos
        serían el corolario de la regla 0 sobre el dato que decide si un CPK se
        puede calcular.
        """
        from flota.dominio.valores import Lectura

        return Lectura(
            valor_km=self.valor_km,
            ts=self.ts,
            origen=OrigenLectura(self.origen),
            autor_usuario_id=self.autor_usuario_id,
            motivo_correccion=self.motivo_correccion,
            confianza=Confianza(self.confianza),
        )


@event.listens_for(LecturaOdometro, 'before_insert')
def _marcar_confianza(mapper, connection, target):
    """La confianza se calcula al nacer la fila, y **acá es donde nace**.

    QUÉ AFIRMA: que ninguna lectura entra a esta tabla por el ORM sin su marca,
    venga del traspaso de turno, de la lectura suelta, del hallazgo, de la
    inspección, del gasto, de un script o de un test.

    QUÉ NO AFIRMA: que la marca sea cierta. `declarada` significa «nada
    evidente la contradice» y `dudosa` significa «no la uses para dividir hasta
    que alguien la mire» — ninguna de las dos afirma nada sobre el número.

    ## Por qué un `before_insert` y no una llamada en cada escritor

    Porque los escritores son cinco hoy y el sexto es el que va a olvidarse. El
    módulo lleva la semana pagando exactamente eso —una política escrita, y la
    vía que no la llama—, y el propio docstring de `confianza_al_nacer` está
    escrito para este llamador: *«quien llama a esta función está dentro de un
    `before_insert` y tiene columnas, no objetos de dominio»*.

    Una llamada por escritor sería la misma regla escrita cinco veces: la copia
    que se quede atrás no falla, deja pasar — y la lectura mala entra sin marca,
    que es indistinguible de una buena. Acá el camino es uno solo y no se puede
    esquivar sin borrar esta función.

    Lo que este gancho **no** puede cubrir es un `INSERT` crudo (SQL a mano, una
    migración): el ORM no lo ve. Contra eso están la columna `NOT NULL` sin
    default —un INSERT que calle, falla— y el trigger que impide nacer
    `verificada`. Es la doble escritura de siempre: el adaptador para dar un
    error legible, la base para que no exista camino que lo esquive.

    `ts` se resuelve acá si el llamador no lo puso. El `default=` de la columna
    se aplica DESPUÉS de este gancho, así que sin esto la regla del Δt = 0
    compararía contra `None` y no se podría aplicar justo en las filas que el
    reintento del 2026-08-03 produjo.
    """
    from flota.dominio.odometro import confianza_al_nacer

    if target.ts is None:
        target.ts = datetime.utcnow()

    if target.confianza == Confianza.VERIFICADA.value:
        # Legible acá, imposible en la base (trigger `flota_odometro_nace_no_
        # verificada`). Que una fila pudiera nacer verificada volvería
        # decorativa la única marca que afirma que una persona miró.
        raise ValueError(
            'ninguna lectura nace verificada: `verificada` la escribe una '
            'persona por la cola de verificación, con la foto al lado. '
            'Ver flota/adaptadores/verificacion.py.'
        )

    t = LecturaOdometro.__table__
    previa = connection.execute(
        select(t.c.valor_km, t.c.ts)
        .where(t.c.vehiculo_id == target.vehiculo_id)
        .order_by(t.c.ts.desc(), t.c.id.desc())
        .limit(1)
    ).first()

    confianza, motivo = confianza_al_nacer(
        valor_km=target.valor_km,
        ts=target.ts,
        tiene_foto=target.foto_id is not None,
        previa_valor_km=previa.valor_km if previa is not None else None,
        previa_ts=previa.ts if previa is not None else None,
    )
    target.confianza = confianza.value
    target.motivo_dudosa = motivo


# ═══════════════════════════════════════════════════════════════════════════
# custodia
# ═══════════════════════════════════════════════════════════════════════════

class Custodia(db.Model):
    """Un tramo de responsabilidad sobre un vehículo. `fin_ts IS NULL` = activa.

    Responsabilidad → conductor, porque lo que hace válida un acta es la cédula
    y la cédula vive en `conductores`. Autenticación → usuario, siempre NOT NULL.
    Si el jefe de sede registra la entrega porque el conductor no tiene cuenta,
    queda custodio = el conductor con su cédula y `registrado_por` = el jefe.
    Honesto y auditable, en vez de excluir gente en silencio.

    `custodio_sede_id` apunta a `almacenes` — la tabla que en WMS lleva
    `centro_op_siesa`, o sea la que hace de centro de costo. Ver ESTADO.md: que
    `almacenes` contenga TODAS las sedes que pueden tener custodia está sin
    verificar contra datos reales.
    """

    __tablename__ = 'flota_custodia'

    id          = db.Column(db.Integer, primary_key=True)
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'), nullable=False, index=True)

    custodio_tipo         = db.Column(db.String(20), nullable=False)
    custodio_conductor_id = db.Column(db.Integer, db.ForeignKey('conductores.id'), nullable=True)
    custodio_sede_id      = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=True)

    registrado_por_usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    inicio_ts = db.Column(db.DateTime, nullable=False)
    fin_ts    = db.Column(db.DateTime, nullable=True)
    km_inicio = db.Column(db.Integer, nullable=False)
    km_fin    = db.Column(db.Integer, nullable=True)

    # DÓNDE queda el vehículo. NO es lo mismo que quién responde — son dos
    # hechos independientes y mezclarlos descarga de responsabilidad a quien
    # efectivamente tiene el camión. Ver `valores.Ubicacion`.
    #
    # Nullable: las custodias anteriores al 2026-08-03 no lo registraron y no se
    # puede saber dónde quedaron. Se dice, no se rellena.
    ubicacion        = db.Column(db.String(20), nullable=True)
    ubicacion_motivo = db.Column(db.Text, nullable=True)

    # Arranque en frío: los daños de la primera custodia nacen preexistentes,
    # sin responsable. Nadie paga por lo que no sabemos cuándo apareció.
    linea_base = db.Column(db.Boolean, nullable=False, server_default='0')

    # `almacenes` cubre 5 de los 9 centros del mapa de C.O. (medido 2026-08-01).
    # Un vehículo que termina turno en una sede sin fila NO se cuelga de un
    # almacén cualquiera ni se guarda con el custodio en NULL y sin explicar:
    # se declara `pendiente_sede` y el health lo cuenta.
    #
    # Flota no crea almacenes — es maestro del WMS, atado a la parametrización
    # de Siesa. Una sede inventada desde acá reaparece después como un C.O. que
    # Siesa no reconoce.
    custodio_estado = db.Column(db.String(20), nullable=False, server_default='resuelto')

    # Cierre sin firma del custodio anterior. NO es una custodia normal cerrada:
    # no hay `fotos_fin`, así que el turno siguiente arranca sin comparación
    # posible y el próximo daño que aparezca no se le puede atribuir a nadie.
    # Si eso no queda distinguible, nadie va a saber por qué.
    custodio_conductor = db.relationship('Conductor', lazy='joined',
                                         foreign_keys=[custodio_conductor_id])

    cierre_forzado = db.Column(db.Boolean, nullable=False, server_default='0')
    cierre_forzado_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    cierre_forzado_motivo = db.Column(db.Text, nullable=True)

    __table_args__ = (
        _en('custodio_tipo', CUSTODIO_TIPO),
        _en('custodio_estado', CUSTODIO_ESTADO),
        # INVARIANTE 4 — exactamente un custodio, y del tipo que declara.
        #
        # `pendiente_sede` NO debilita el invariante: lo hace condicional a un
        # estado que a su vez está constreñido. Una fila `resuelto` sigue
        # exigiendo exactamente un custodio; una `pendiente_sede` exige tipo
        # sede Y los dos ids nulos. No queda ninguna combinación silenciosa:
        # no se puede estar `pendiente_sede` con un conductor puesto, ni
        # `resuelto` sin nadie.
        db.CheckConstraint(
            # CASE WHEN y no `(x IS NOT NULL) + (y IS NOT NULL)`: PostgreSQL no
            # tiene operador `boolean + boolean`. SQLite sí —trata los booleanos
            # como 0/1— y por eso los 25 tests de constraints pasaron en verde
            # mientras el CREATE TABLE reventaba en producción.
            "(custodio_estado = 'resuelto' AND "
            " (CASE WHEN custodio_conductor_id IS NOT NULL THEN 1 ELSE 0 END + "
            "  CASE WHEN custodio_sede_id IS NOT NULL THEN 1 ELSE 0 END) = 1) OR "
            "(custodio_estado = 'pendiente_sede' AND "
            " custodio_conductor_id IS NULL AND custodio_sede_id IS NULL)",
            name='ck_flota_custodia_arco_exclusivo',
        ),
        db.CheckConstraint(
            "custodio_estado = 'pendiente_sede' OR "
            "(custodio_tipo = 'conductor' AND custodio_conductor_id IS NOT NULL) OR "
            "(custodio_tipo = 'sede' AND custodio_sede_id IS NOT NULL)",
            name='ck_flota_custodia_tipo_coherente',
        ),
        # ── Ubicación: dónde está ≠ quién responde ──────────────────────────
        _en('ubicacion', UBICACION),

        # LA combinación que no puede existir.
        #
        # Un vehículo fuera de sede está en manos del conductor — en su casa, en
        # un hotel de ruta, donde sea. Dejar que la custodia pase a `sede` ahí
        # **descarga de responsabilidad a la única persona que lo tiene**: si
        # amanece rayado, el registro dice que respondía una sede que no lo vio
        # nunca.
        #
        # Va como CHECK y no como validación del adaptador a propósito: es
        # exactamente la clase de regla que un refactor futuro borra sin notarlo,
        # y su consecuencia solo aparece meses después, en una discusión sobre
        # quién paga un golpe.
        db.CheckConstraint(
            "ubicacion IS NULL OR ubicacion <> 'fuera_de_sede' "
            "OR custodio_tipo = 'conductor'",
            name='ck_flota_fuera_de_sede_responde_el_conductor',
        ),
        # Fuera de sede no es un caso normal: es un vehículo pasando la noche
        # fuera del control de la empresa. El motivo escrito es lo que hace que
        # sea una decisión y no una costumbre.
        db.CheckConstraint(
            "ubicacion IS NULL OR ubicacion <> 'fuera_de_sede' "
            "OR (ubicacion_motivo IS NOT NULL AND length(trim(ubicacion_motivo)) > 0)",
            name='ck_flota_fuera_de_sede_con_motivo',
        ),

        # Una custodia sin sede resoluble solo puede ser de tipo sede: un
        # conductor siempre tiene fila, es la cédula lo que hace válida el acta.
        db.CheckConstraint(
            "custodio_estado <> 'pendiente_sede' OR custodio_tipo = 'sede'",
            name='ck_flota_pendiente_sede_solo_es_sede',
        ),
        db.CheckConstraint('fin_ts IS NULL OR fin_ts >= inicio_ts',
                           name='ck_flota_custodia_cierre_posterior'),
        db.CheckConstraint('km_fin IS NULL OR km_fin >= km_inicio',
                           name='ck_flota_custodia_km_no_decrece'),
        # Un forzado sin autor ni motivo es un cierre anónimo: quedaría el
        # rastro de que pasó algo raro y ninguna forma de saber quién ni por qué.
        db.CheckConstraint(
            "(cierre_forzado = '0' AND cierre_forzado_por_usuario_id IS NULL "
            " AND cierre_forzado_motivo IS NULL) OR "
            "(cierre_forzado = '1' AND cierre_forzado_por_usuario_id IS NOT NULL "
            " AND length(trim(cierre_forzado_motivo)) > 0)",
            name='ck_flota_cierre_forzado_declarado',
        ),
        # INVARIANTE 2 — 0 o 1 activa. Índice único parcial: solo indexa las
        # filas abiertas, así que dos abiertas para el mismo vehículo colisionan
        # y cualquier cantidad de cerradas convive.
        db.Index('uq_flota_custodia_activa', 'vehiculo_id',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),
        # INVARIANTE 3 — un conductor, un vehículo. **El hermano que faltaba.**
        #
        # El de arriba impone 0-o-1 activa **por vehículo** y este 0-o-1 **por
        # conductor**. Son preguntas distintas y solo la primera tenía respaldo
        # en disco: la segunda vivía únicamente en
        # `dom.validar_un_vehiculo_por_conductor`, comprobada en Python con un
        # `.all()` sin bloqueo (`traspaso.py`).
        #
        # La diferencia importa por cómo falla cada una. Dos aperturas
        # simultáneas sobre el mismo vehículo chocan contra el índice y el
        # usuario ve un error feo — molesto, pero el dato queda íntegro. Dos
        # aperturas simultáneas del mismo conductor sobre vehículos distintos
        # **no chocaban con nada**: pasaban las dos, sin error, y dejaban el
        # invariante roto en silencio.
        #
        # Ya ocurrió sin necesidad de una carrera: el 2026-08-13 un conductor
        # acumuló tres custodias abiertas y tumbó `/flota/conductor/mi-turno`
        # con `MultipleResultsFound`. La validación de dominio se escribió por
        # eso; lo que no se escribió fue el respaldo.
        #
        # `custodio_conductor_id` es NULL cuando el custodio es una sede, y
        # Postgres trata los NULL como distintos: cualquier cantidad de
        # custodias de sede convive sin colisionar. No hace falta filtrar por
        # `custodio_tipo`.
        db.Index('uq_flota_custodia_conductor_activa', 'custodio_conductor_id',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),
    )


# ═══════════════════════════════════════════════════════════════════════════
# foto
# ═══════════════════════════════════════════════════════════════════════════

class Foto(db.Model):
    """Referencia a un archivo en object storage. Nunca el binario.

    `entidad_tipo` + `entidad_id` es una paternidad polimórfica y por eso NO
    puede ser una FK: se valida en el dominio (invariante 5) y se cuenta en el
    health. Es la única de las cinco tablas donde la base no puede sola, y queda
    dicho en vez de disimulado.
    """

    __tablename__ = 'flota_foto'

    id     = db.Column(db.Integer, primary_key=True)
    clase  = db.Column(db.String(20), nullable=False)
    entidad_tipo = db.Column(db.String(20), nullable=False)
    entidad_id   = db.Column(db.Integer, nullable=False)

    storage_ref = db.Column(db.Text, nullable=False)
    hash_sha256 = db.Column(db.String(64), nullable=False)
    bytes       = db.Column(db.Integer, nullable=False)
    # NULL solo para `documento_adjunto`: un PDF no tiene pixeles. Guardarle
    # 0x0 para satisfacer un NOT NULL seria un numero que miente sobre un
    # archivo que si existe. El CHECK de abajo impone exactamente eso, y
    # `flota.dominio.fotos.exige_dimensiones` dice lo mismo del lado del codigo.
    ancho       = db.Column(db.Integer, nullable=True)
    alto        = db.Column(db.Integer, nullable=True)
    mime        = db.Column(db.String(40), nullable=False)

    # Qué parte del vehículo muestra. NULL a proposito y no un default:
    # las fotos anteriores al 2026-08-03 se guardaron sin angulo y NO se puede
    # saber cual era cual — inventarselo seria peor que decir que no se sabe.
    # El health cuenta las que quedaron sin angulo.
    angulo = db.Column(db.String(24), nullable=True)

    ts_captura = db.Column(db.DateTime, nullable=False)
    gps_lat    = db.Column(db.Float, nullable=True)
    gps_lon    = db.Column(db.Float, nullable=True)

    autor_usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    # Un doble de prueba deja rastro distinguible del real (regla 8).
    simulado = db.Column(db.Boolean, nullable=False, server_default='0')

    # Si la compresión de una foto_dato falla, el registro queda acá y el health
    # lo cuenta. Nunca `pass`. El campo NO está en §1 de la especificación: se
    # agregó porque §1 exige el estado `pendiente_evidencia` sin darle columna.
    estado = db.Column(db.String(30), nullable=False, server_default='ok')

    __table_args__ = (
        _en('clase', CLASE_FOTO),
        _en('entidad_tipo', ENTIDAD_FOTO),
        # Condicional: si hay angulo tiene que ser uno del vocabulario. La
        # ausencia se permite (filas viejas); un valor inventado no.
        db.CheckConstraint(
            'angulo IS NULL OR angulo IN (%s)' % ', '.join(f"'{a}'" for a in ANGULO_FOTO),
            name='ck_flota_angulo'),
        _en('estado', ESTADO_FOTO),
        # Dimensiones condicionales por clase. Un archivo de 0 bytes nunca es
        # valido; unas dimensiones ausentes solo lo son para un adjunto, y solo
        # si faltan LAS DOS: una sola no describe nada.
        db.CheckConstraint(
            "bytes > 0 AND ("
            "(ancho IS NOT NULL AND alto IS NOT NULL AND ancho > 0 AND alto > 0) OR "
            "(clase = 'documento_adjunto' AND ancho IS NULL AND alto IS NULL))",
            name='ck_flota_foto_medidas'),
        # Regla 7 — el binario nunca vive en la base. Se impide en la base y no
        # solo en la revisión: `recaudo_entrega.foto_entrega` es base64 en una
        # columna Text y así empezó.
        db.CheckConstraint(
            "storage_ref NOT LIKE 'data:%' AND length(storage_ref) < 500",
            name='ck_flota_foto_es_referencia_no_binario',
        ),
        # Una foto_dato por debajo del mínimo no es evidencia: es un odómetro
        # que no se puede verificar. Se admite guardarla, pero declarada rota.
        db.CheckConstraint(
            "clase <> 'foto_dato' OR estado = 'pendiente_evidencia' OR "
            f'ancho >= {LADO_LARGO_MINIMO_FOTO_DATO} OR alto >= {LADO_LARGO_MINIMO_FOTO_DATO}',
            name='ck_flota_foto_dato_resolucion',
        ),
        db.Index('ix_flota_foto_padre', 'entidad_tipo', 'entidad_id'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Triggers — lo que ningún CHECK puede hacer porque mira otras filas
# ═══════════════════════════════════════════════════════════════════════════
#
# Van colgados del `after_create` de la tabla para que existan también en la
# SQLite de los tests. Un invariante impuesto en producción pero no en tests es
# un invariante que nadie ejerció.

_MSG_MONOTONIA = 'flota: el odometro no puede decrecer sin origen=correccion'
_MSG_APPEND_ONLY = 'flota: lectura_odometro es append-only — se corrige con un registro nuevo'
_MSG_SOLAPE = 'flota: la custodia nueva se solapa o antecede a la anterior'
_MSG_NACE_NO_VERIFICADA = (
    'flota: ninguna lectura nace verificada — esa marca la escribe una persona '
    'por la cola de verificacion')
_MSG_ANCLA_BAJA = (
    'flota: km_inicial no puede bajar por debajo del maximo ya registrado en '
    'flota_lectura_odometro para ese vehiculo')

# ── Lo ÚNICO que se puede escribir sobre una lectura ya escrita ──────────────
#
# El bloqueo de `UPDATE` existe desde la tanda 1 y su motivo es el número: una
# lectura no se edita, se corrige con un registro nuevo. Lo que la verificación
# humana escribe **no es el número**: es que alguien lo miró contra la foto.
#
# Por eso el trigger deja de ser «ningún byte cambia» y pasa a ser una lista
# explícita de lo que tiene que quedar idéntico. Es más estricto que el
# anterior en una cosa que el anterior no decía: la transición de confianza
# solo puede ir HACIA `verificada`, y una vez ahí no se sale. Sin eso, un
# `UPDATE` podría desverificar una lectura y nadie lo sabría.
#
# La alternativa era una tabla aparte para la verificación. Se descartó: dejaría
# la confianza real repartida entre dos tablas, y `confianza_del_tramo` recibe
# UN campo. El día que alguien consultara la lectura sin el JOIN publicaría un
# CPK sobre una verificación que no vio.
_COLUMNAS_INMUTABLES = (
    'id', 'vehiculo_id', 'valor_km', 'ts', 'origen', 'foto_id',
    'autor_usuario_id', 'motivo_correccion', 'motivo_dudosa',
)


def _identicas(comparador: str) -> str:
    """`NEW.x <cmp> OLD.x AND …` para las columnas que no se pueden tocar.

    Escrito desde `_COLUMNAS_INMUTABLES` y no a mano en los dos dialectos: una
    columna nueva que se agregue a la lista entra a los dos triggers a la vez.
    Dos listas literales se separan, y la que se quede corta deja pasar el
    `UPDATE` que el trigger existe para bloquear.

    `comparador` cambia porque el NULL-safe de SQLite es `IS` y el de
    PostgreSQL es `IS NOT DISTINCT FROM`. Con `=` a secas, un `motivo_dudosa`
    NULL daría NULL en vez de verdadero y el trigger abortaría sobre la
    verificación legítima.
    """
    return ' AND '.join(f'NEW.{c} {comparador} OLD.{c}'
                        for c in _COLUMNAS_INMUTABLES)

# ASIMETRÍA DECLARADA — el bloqueo de DELETE existe en PostgreSQL y no en SQLite.
#
# No es una preferencia: el teardown de `tests/conftest.py` limpia con
# `DELETE FROM` sobre cada tabla de la metadata, y ese bucle tiene un
# `except Exception: rollback()` que, al fallar UNA tabla, descarta también los
# deletes ya hechos de todas las anteriores. Con el trigger puesto en SQLite,
# 24 tests ajenos a flota empezaron a fallar por datos que sobrevivían de un
# test al siguiente.
#
# Se eligió no tocar el conftest global en esta tanda. La consecuencia es que el
# DELETE queda protegido donde importa —producción— y sin ejercitar donde no
# corre. Para que la asimetría no se olvide, `test_constraints_t1.py` verifica
# que el DDL de PostgreSQL sí lo contenga. Y el `except` del conftest queda
# anotado en ESTADO.md: una tabla que falla no debería poder deshacer la
# limpieza de las otras.
_SQLITE_DDL = f"""
CREATE TRIGGER flota_odometro_monotonia
BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW WHEN NEW.origen <> 'correccion' AND EXISTS (
    SELECT 1 FROM flota_lectura_odometro
    WHERE vehiculo_id = NEW.vehiculo_id AND valor_km > NEW.valor_km
      AND ts >= COALESCE(
          (SELECT MAX(ts) FROM flota_lectura_odometro
            WHERE vehiculo_id = NEW.vehiculo_id AND origen = 'correccion'),
          ts))
BEGIN SELECT RAISE(ABORT, '{_MSG_MONOTONIA}'); END;

CREATE TRIGGER flota_odometro_no_update
BEFORE UPDATE ON flota_lectura_odometro
FOR EACH ROW WHEN NOT (
    {_identicas('IS')}
    AND OLD.confianza <> 'verificada'
    AND NEW.confianza = 'verificada')
BEGIN SELECT RAISE(ABORT, '{_MSG_APPEND_ONLY}'); END;

CREATE TRIGGER flota_odometro_nace_no_verificada
BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW WHEN NEW.confianza = 'verificada'
BEGIN SELECT RAISE(ABORT, '{_MSG_NACE_NO_VERIFICADA}'); END;
"""

# ── El trigger hermano del ancla — SOLO la mitad segura ──────────────────────
#
# `km_inicial` es el ancla del sistema y **nada lo lee contra nada** (medido por
# AST el 2026-09-01). Este trigger cierra una sola de las dos direcciones: que
# el ancla no se pueda BAJAR por debajo de lo que la serie de ese vehículo ya
# tiene escrito. Sin él, el piso se mueve bajo el techo y el invariante se rompe
# sin dar error.
#
# **La otra mitad NO se implementa hoy**: el piso duro
# `MAX(histórico, km_inicial)` dentro del trigger de monotonía. Medido contra
# producción el 2026-09-01, **4 de 6 fichas contradicen su propia serie** —la
# del THP696 dice 433.434 y su primera lectura es 55.349—, así que imponerlo
# hoy trabaría vehículos por una segunda vía. `fichas_con_ancla_incoherente` ya
# lo cuenta y hay que dejarlo correr un mes. La condición de disparo está en
# `docs/flota/ESTADO.md`.
#
# ── Por qué la condición exige TAMBIÉN que el valor baje ─────────────────────
# `NEW.km_inicial < OLD.km_inicial` no es decoración. Sin esa mitad, la
# condición sería «el ancla quedó por debajo del máximo» — y eso ya es cierto
# HOY para las cuatro fichas incoherentes, así que **cualquier** edición de esas
# fichas (el aceite, la medida de llanta, lo que sea) quedaría bloqueada aunque
# nadie tocara `km_inicial`. Eso es exactamente el «trabar vehículos por una
# segunda vía» que esta tanda decidió no hacer. Con las dos mitades, lo que se
# impide es el gesto —bajar el ancla—, no el estado heredado.
_SQLITE_DDL_FICHA = f"""
CREATE TRIGGER flota_ficha_ancla_no_baja
BEFORE UPDATE ON flota_ficha_tecnica
FOR EACH ROW WHEN NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (
    SELECT COALESCE(MAX(valor_km), NEW.km_inicial)
      FROM flota_lectura_odometro WHERE vehiculo_id = NEW.vehiculo_id)
BEGIN SELECT RAISE(ABORT, '{_MSG_ANCLA_BAJA}'); END;
"""

_PG_DDL_FICHA = f"""
CREATE OR REPLACE FUNCTION flota_ficha_ancla_no_baja() RETURNS trigger AS $$
BEGIN
  IF NEW.km_inicial < OLD.km_inicial AND NEW.km_inicial < (
        SELECT COALESCE(MAX(l.valor_km), NEW.km_inicial)
          FROM flota_lectura_odometro l
         WHERE l.vehiculo_id = NEW.vehiculo_id) THEN
    RAISE EXCEPTION '{_MSG_ANCLA_BAJA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_ficha_ancla_no_baja BEFORE UPDATE ON flota_ficha_tecnica
FOR EACH ROW EXECUTE FUNCTION flota_ficha_ancla_no_baja();
"""

_SQLITE_DDL_CUSTODIA = f"""
CREATE TRIGGER flota_custodia_no_solapa
BEFORE INSERT ON flota_custodia
FOR EACH ROW WHEN EXISTS (
    SELECT 1 FROM flota_custodia
    WHERE vehiculo_id = NEW.vehiculo_id AND inicio_ts > NEW.inicio_ts)
   OR EXISTS (
    SELECT 1 FROM flota_custodia
    WHERE vehiculo_id = NEW.vehiculo_id AND fin_ts > NEW.inicio_ts)
BEGIN SELECT RAISE(ABORT, '{_MSG_SOLAPE}'); END;
"""

_PG_DDL = f"""
CREATE OR REPLACE FUNCTION flota_odometro_monotonia() RETURNS trigger AS $$
BEGIN
  IF NEW.origen <> 'correccion' AND EXISTS (
      SELECT 1 FROM flota_lectura_odometro l
      WHERE l.vehiculo_id = NEW.vehiculo_id AND l.valor_km > NEW.valor_km
        AND l.ts >= COALESCE(
            (SELECT MAX(c.ts) FROM flota_lectura_odometro c
              WHERE c.vehiculo_id = NEW.vehiculo_id
                AND c.origen = 'correccion'),
            l.ts)) THEN
    RAISE EXCEPTION '{_MSG_MONOTONIA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_monotonia BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_monotonia();

CREATE OR REPLACE FUNCTION flota_odometro_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '{_MSG_APPEND_ONLY}'; END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION flota_odometro_solo_verificacion() RETURNS trigger AS $$
BEGIN
  IF {_identicas('IS NOT DISTINCT FROM')}
     AND OLD.confianza <> 'verificada' AND NEW.confianza = 'verificada' THEN
    RETURN NEW;
  END IF;
  RAISE EXCEPTION '{_MSG_APPEND_ONLY}';
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_no_update BEFORE UPDATE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_solo_verificacion();

CREATE TRIGGER flota_odometro_no_delete BEFORE DELETE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only();

CREATE OR REPLACE FUNCTION flota_odometro_nace_no_verificada() RETURNS trigger AS $$
BEGIN
  IF NEW.confianza = 'verificada' THEN
    RAISE EXCEPTION '{_MSG_NACE_NO_VERIFICADA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_nace_no_verificada BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_nace_no_verificada();
"""

_PG_DDL_CUSTODIA = f"""
CREATE OR REPLACE FUNCTION flota_custodia_no_solapa() RETURNS trigger AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM flota_custodia
             WHERE vehiculo_id = NEW.vehiculo_id
               AND (inicio_ts > NEW.inicio_ts OR fin_ts > NEW.inicio_ts)) THEN
    RAISE EXCEPTION '{_MSG_SOLAPE}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_custodia_no_solapa BEFORE INSERT ON flota_custodia
FOR EACH ROW EXECUTE FUNCTION flota_custodia_no_solapa();
"""


def _colgar(tabla, sqlite_sql, pg_sql):
    @event.listens_for(tabla, 'after_create')
    def _crear(target, connection, **kw):
        sql = sqlite_sql if connection.dialect.name == 'sqlite' else pg_sql
        for sentencia in [s.strip() for s in sql.split(';\n\n') if s.strip()]:
            connection.execute(DDL(sentencia))


_colgar(LecturaOdometro.__table__, _SQLITE_DDL, _PG_DDL)
_colgar(Custodia.__table__, _SQLITE_DDL_CUSTODIA, _PG_DDL_CUSTODIA)
# Va colgado de la tabla de LECTURAS y no de la ficha a propósito: el trigger
# consulta `flota_lectura_odometro`, y en `create_all()` la ficha se crea antes.
# Colgado de la ficha, el `CREATE TRIGGER` correría contra una tabla de lecturas
# que todavía no existe — en PostgreSQL eso es un error duro.
_colgar(LecturaOdometro.__table__, _SQLITE_DDL_FICHA, _PG_DDL_FICHA)


__all__ = [
    'FichaTecnica', 'DocumentoVehiculo', 'LecturaOdometro', 'Custodia', 'Foto',
    'PlantillaInspeccion', 'ItemInspeccion', 'Inspeccion', 'RespuestaItem',
    'LADO_LARGO_MINIMO_FOTO_DATO', 'DIAS_DE_PLAZO',
]


# ═══════════════════════════════════════════════════════════════════════════
# Plantillas de inspección — el catálogo, versionado
# ═══════════════════════════════════════════════════════════════════════════

class PlantillaInspeccion(db.Model):
    """Un catálogo de ítems, congelado en una versión.

    **Las plantillas no se editan: se versionan.** Una inspección hecha bajo
    `camion_v1` tiene que seguir siendo legible dentro de dos años, y si los
    ítems cambiaran bajo sus pies, el registro diría una cosa y significaría
    otra. Cambiar el catálogo es crear `camion_v2` y desactivar la anterior.

    Por eso el código lleva la versión adentro (`camion_v1`) y no hay UPDATE
    previsto sobre los ítems.
    """

    __tablename__ = 'flota_plantilla_inspeccion'

    id       = db.Column(db.Integer, primary_key=True)
    codigo   = db.Column(db.String(40), nullable=False, unique=True)
    nombre   = db.Column(db.String(100), nullable=False)
    version  = db.Column(db.Integer, nullable=False)
    aplica_a = db.Column(db.String(20), nullable=False)
    activa   = db.Column(db.Boolean, nullable=False, server_default='1')
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship('ItemInspeccion', backref='plantilla', lazy=True,
                            order_by='ItemInspeccion.orden')

    __table_args__ = (
        _en('aplica_a', APLICA_A),
        db.CheckConstraint('version > 0', name='ck_flota_plantilla_version'),
        db.UniqueConstraint('aplica_a', 'version', name='uq_flota_plantilla_version'),
    )

    def bloqueantes(self):
        return [i for i in self.items if i.criticidad == 'bloqueante']


class ItemInspeccion(db.Model):
    """Un ítem del catálogo, con su gesto y su criticidad.

    `gesto` es NOT NULL y no puede estar vacío. **Sin el gesto la criticidad es
    decorativa**: "revisar frenos" no dice qué hacer y termina en un óptimo
    marcado sin mirar; "pisar a fondo y sostener 5 segundos, ¿el pedal sigue
    hundiéndose?" sí. Va en pantalla, no en un manual aparte.

    `periodicidad` existe porque no todo lo que se inspecciona se inspecciona a
    diario: el drenaje del separador de agua es semanal. Sin el campo, o se
    pregunta todos los días —y se vuelve ruido que entrena a marcar sin leer— o
    no se pregunta nunca.
    """

    __tablename__ = 'flota_item_inspeccion'

    id           = db.Column(db.Integer, primary_key=True)
    plantilla_id = db.Column(db.Integer, db.ForeignKey('flota_plantilla_inspeccion.id'),
                             nullable=False, index=True)
    orden        = db.Column(db.Integer, nullable=False)
    nombre       = db.Column(db.String(120), nullable=False)
    gesto        = db.Column(db.Text, nullable=False)
    criticidad   = db.Column(db.String(20), nullable=False)
    periodicidad = db.Column(db.String(20), nullable=False, server_default='diaria')

    __table_args__ = (
        _en('criticidad', CRITICIDAD),
        _en('periodicidad', PERIODICIDAD),
        db.CheckConstraint('length(trim(gesto)) > 0', name='ck_flota_item_gesto_no_vacio'),
        db.CheckConstraint('length(trim(nombre)) > 0', name='ck_flota_item_nombre_no_vacio'),
        db.UniqueConstraint('plantilla_id', 'orden', name='uq_flota_item_orden'),
    )

    @property
    def dias_de_plazo(self) -> int:
        """Regla 6: se calcula, no se elige."""
        return DIAS_DE_PLAZO[self.criticidad]


# ═══════════════════════════════════════════════════════════════════════════
# aviso — qué se le mandó a quién, y si llegó
# ═══════════════════════════════════════════════════════════════════════════

ESTADO_AVISO = ('encolado', 'entregado_al_proveedor', 'entregado', 'leido', 'fallido')

# `entregado_al_proveedor` y `entregado` son estados distintos A PROPÓSITO.
#
# Gupshup responde `submitted` y eso significa "lo recibí", no "llegó". En
# cartera esa confusión costó semanas: el tablero decía enviado y el teléfono
# nunca sonó. Acá pesa más — el sistema existe para que un hallazgo vencido no
# se quede quieto; si el aviso no llega y nadie se entera, falló justo donde
# tenía que funcionar.


class Aviso(db.Model):
    """Un aviso mandado (o intentado) hacia una persona.

    Fila por evento, no por consulta: `clave` es única, así que el cron puede
    correr todas las noches sin repetir el mismo vencimiento. Un documento
    RENOVADO genera clave nueva porque la clave lleva la fecha del hito.
    """

    __tablename__ = 'flota_aviso'

    id    = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(200), nullable=False, unique=True)

    plantilla = db.Column(db.String(60), nullable=False)
    telefono  = db.Column(db.String(30), nullable=False)
    # Los parámetros posicionales, tal como se mandaron. Se guardan para poder
    # RECONSTRUIR el texto que la persona leyó: sin esto, un mensaje mal armado
    # es imposible de diagnosticar después.
    parametros = db.Column(db.Text, nullable=False)

    estado = db.Column(db.String(30), nullable=False, server_default='encolado')
    # Id del proveedor. Es lo único que permite cruzar un evento de entrega
    # entrante con la fila que lo espera.
    proveedor_msg_id = db.Column(db.String(120), nullable=True)
    detalle = db.Column(db.Text, nullable=True)

    # Regla 8: el doble deja rastro en el REGISTRO, no solo en el código.
    simulado = db.Column(db.Boolean, nullable=False, server_default='0')

    creado_ts   = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    entregado_ts = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        _en('estado', ESTADO_AVISO),
        db.CheckConstraint("length(trim(telefono)) > 0", name='ck_flota_aviso_telefono'),
        # Un id de proveedor es obligatorio en cuanto salió: sin él no hay forma
        # de saber si llegó, y el aviso se vuelve incomprobable.
        db.CheckConstraint(
            "estado IN ('encolado', 'fallido') OR "
            "(proveedor_msg_id IS NOT NULL AND length(trim(proveedor_msg_id)) > 0)",
            name='ck_flota_aviso_id_proveedor',
        ),
        db.Index('ix_flota_aviso_proveedor', 'proveedor_msg_id'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# hallazgo — un daño que alguien vio, con su reloj
# ═══════════════════════════════════════════════════════════════════════════

ESTADO_HALLAZGO = tuple(
    getattr(_EstadoHallazgo, n)
    for n in ('ABIERTO', 'CERRADO', 'DESCARTADO', 'NO_APLICA')
)


class Hallazgo(db.Model):
    """Un daño detectado en un vehículo, con severidad, plazo y desenlace.

    **La tabla que faltaba.** `flota/dominio/hallazgo.py` tiene 153 líneas de
    política —canon cerrado por Santiago, 23 tests escritos antes del cálculo— y
    hasta el 2026-09-01 **cero callers de producción**: no había dónde escribir
    un hallazgo. Los 53 ítems de inspección estaban sembrados en producción
    esperando una tabla que no existía. Un daño no podía nacer.

    ## Qué NO hace esta tabla

    No es la inspección. Un hallazgo puede venir de una inspección diaria, del
    recibo de un turno, o de alguien que vio un golpe en el patio. Atarlo a una
    inspección lo dejaría sin poder nacer por las otras dos vías, que son las
    que ocurren hoy — porque la pantalla de inspección todavía no existe.

    `inspeccion_id` llegará cuando exista; se deja fuera en vez de nullable para
    no crear una columna que apunta a una tabla ausente.

    ## Las tres reglas del módulo que la tabla impone

    · **Regla 6 — el plazo se calcula al nacer, no se elige.** `fecha_limite`
      sale de `DIAS_DE_PLAZO[criticidad]` en el adaptador. Si alguien pudiera
      escribir «este bloqueante para el viernes», la severidad dejaría de
      significar algo. Y no hay transición directa `abierto → cerrado`: se
      cierra con fecha, o se `descarta` con motivo escrito.

    · **Regla 3 — sin odómetro no se persiste ningún evento de flota.**
      `lectura_id` es NOT NULL. Un daño sin kilometraje no se puede cruzar
      después contra un mantenimiento ni contra un CPK.

    · **Regla 2 — ningún automatismo imputa responsabilidad.** No hay columna
      de «culpable». Está `reportado_por_usuario_id`, que es quién lo vio, y
      `custodia_id`, que dice bajo la custodia de quién apareció — dos hechos.
      Quién responde lo decide un humano, fuera de esta tabla.

    ## `linea_base`

    La primera inspección de un vehículo levanta lo que ya estaba. Esos
    hallazgos nacen preexistentes: sin responsable y **sin entrar al indicador**
    (`entra_al_indicador` los excluye). El desorden viejo no le cuenta a quien
    lo encontró. Es la misma idea que `Custodia.linea_base`.
    """

    __tablename__ = 'flota_hallazgo'

    id          = db.Column(db.Integer, primary_key=True)
    #: Sin `index=True`: el compuesto `ix_flota_hallazgo_abiertos` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)
    criticidad  = db.Column(db.String(20), nullable=False)
    descripcion = db.Column(db.Text, nullable=False)

    #: No cambia NUNCA. El aplazamiento mueve `fecha_limite`, no el origen del
    #: reloj — por eso son campos distintos (ver el dataclass del dominio).
    reportado_ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    reportado_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    #: Regla 3. NOT NULL: un daño sin kilometraje no se cruza con nada después.
    lectura_id = db.Column(db.Integer, db.ForeignKey('flota_lectura_odometro.id'),
                           nullable=False)

    #: Bajo qué custodia apareció. Es un hecho, no una imputación (regla 2).
    custodia_id = db.Column(db.Integer, db.ForeignKey('flota_custodia.id'),
                            nullable=True, index=True)

    #: Se calcula al nacer desde `DIAS_DE_PLAZO`. Aplazar la mueve y suma en
    #: `aplazado_veces`, que es lo que hace visible el aplazamiento crónico:
    #: un hallazgo aplazado cuatro veces no está gestionado, está evitado.
    fecha_limite   = db.Column(db.DateTime, nullable=False)
    aplazado_veces = db.Column(db.Integer, nullable=False, server_default='0')

    estado     = db.Column(db.String(20), nullable=False,
                           server_default=_EstadoHallazgo.ABIERTO, index=True)
    cerrado_ts = db.Column(db.DateTime, nullable=True)
    cerrado_por_usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'),
                                       nullable=True)

    #: Obligatorio al descartar. Un descarte sin motivo es indistinguible de
    #: hacer desaparecer un hallazgo incómodo.
    motivo_cierre = db.Column(db.Text, nullable=True)

    #: Lo que se dijo sobre el hallazgo **mientras estuvo abierto**: hoy, los
    #: motivos de cada aplazamiento.
    #:
    #: Columna aparte y no reusar `motivo_cierre`, que fue la primera versión
    #: de esto: un motivo de aplazamiento guardado en una columna llamada
    #: «motivo_cierre» sobre una fila abierta es un nombre que promete una cosa
    #: y contiene otra — se lee con confianza y se lee mal, que es el modo de
    #: falla más caro de este repo.
    bitacora = db.Column(db.Text, nullable=True)

    linea_base = db.Column(db.Boolean, nullable=False, server_default='0')

    #: Qué ítem del catálogo lo produjo, si vino de una inspección. Nullable
    #: porque un golpe visto en el patio no sale de ningún ítem.
    item_id = db.Column(db.Integer, db.ForeignKey('flota_item_inspeccion.id'),
                        nullable=True)

    vehiculo = db.relationship('Vehiculo')

    __table_args__ = (
        _en('criticidad', CRITICIDAD),
        _en('estado', ESTADO_HALLAZGO),
        db.CheckConstraint('length(trim(descripcion)) > 0',
                           name='ck_flota_hallazgo_descripcion'),
        db.CheckConstraint('aplazado_veces >= 0',
                           name='ck_flota_hallazgo_aplazos'),
        # Coherencia de desenlace, en una sola expresión y no en tres CHECK
        # sueltos: los tres estados terminales y el abierto son un vocabulario,
        # y partirlo deja huecos entre las piezas.
        #
        # `CASE WHEN` y no aritmética sobre predicados: el 2026-08-01 un CHECK
        # escrito como `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests
        # contra SQLite y reventó el `CREATE TABLE` en el release, porque
        # PostgreSQL no suma booleanos.
        db.CheckConstraint(
            "CASE"
            "  WHEN estado = 'abierto' THEN cerrado_ts IS NULL"
            "  WHEN estado = 'cerrado' THEN cerrado_ts IS NOT NULL"
            "  WHEN estado = 'descartado' THEN"
            "       motivo_cierre IS NOT NULL AND length(trim(motivo_cierre)) > 0"
            "  ELSE 1 = 1"
            " END",
            name='ck_flota_hallazgo_desenlace',
        ),
        db.Index('ix_flota_hallazgo_abiertos', 'vehiculo_id', 'estado'),
    )

    def a_dominio(self):
        """La estructura que `flota.dominio.hallazgo` sabe juzgar.

        El cálculo no vive acá: vive en el dominio, contra el canon. Esta fila
        se traduce, no se pronuncia — igual que `Custodia._a_dominio`.
        """
        from flota.dominio.hallazgo import Hallazgo as HallazgoDom

        return HallazgoDom(
            reportado_ts=self.reportado_ts,
            criticidad=self.criticidad,
            estado=self.estado,
            cerrado_ts=self.cerrado_ts,
            linea_base=self.linea_base,
            fecha_limite=self.fecha_limite,
            aplazado_veces=self.aplazado_veces,
        )


# ═══════════════════════════════════════════════════════════════════════════
# inspeccion — donde el conductor contesta, y lo que la base no le deja decir
# ═══════════════════════════════════════════════════════════════════════════


class Inspeccion(db.Model):
    """Una inspección preoperacional ejecutada: quién, cuándo, sobre qué y con
    qué resultado.

    **La tabla que faltaba, otra vez.** `flota/dominio/inspeccion.py` tenía la
    política de ordenamiento y periodicidad desde la tanda 1, y los 53 ítems
    estaban sembrados en producción — sin ningún lugar donde responderlos. El
    conductor no tenía dónde contestar.

    ## El veredicto tiene respaldo en disco, no solo en Python

    Un `CHECK` no puede mirar las filas hijas, así que la fila padre lleva
    escritos los tres conteos de los que el veredicto se deriva
    (`items_esperados`, `items_sin_dato`, `bloqueantes_no_aptos`) y un `CHECK`
    exige que digan lo mismo. Sin eso, un `UPDATE` desde psql podría dejar
    `veredicto='apto'` sobre una inspección con nueve huecos, y el sistema
    entero existe para que eso no se pueda escribir.

    Los conteos NO son caché: son la parte del hecho que la base puede juzgar.
    Se calculan una sola vez, en `flota.dominio.inspeccion.conteos`, junto con
    el veredicto — contarlos aparte sería la segunda implementación de la misma
    política y la que diverja.

    ## Las tres reglas que la tabla impone

    · **Regla 1 — ningún ítem tiene valor por defecto.** `veredicto` no admite
      dos valores sino tres, y `incompleta` no es `no_apto`: uno es «sé que
      está mal» y el otro «no sé». Ninguno habilita despacho. Con dos valores,
      «no sé» tendría que disfrazarse de una de las dos afirmaciones, y la
      cómoda es `apto`.

    · **Regla 3 — sin odómetro no se persiste ningún evento de flota.**
      `lectura_id` es NOT NULL, con la lectura anclada en la misma transacción
      por `hallazgos.anclar_odometro` — la misma función que usa el hallazgo,
      no una copia (regla 0 del WMS).

    · **Regla 11 — `segundos_llenado`.** Se guarda porque la forma de
      maximizar este registro sin hacer el trabajo es marcar todo óptimo en
      veinte segundos. **Sin umbral** (regla 13): no hay una sola medición
      todavía. Se publica el hecho, como `salto_km_maximo_30d`.

    ## Lo que la tabla NO impone, y por qué

    No hay UNIQUE sobre `(vehiculo_id, dia)`. Dos turnos sobre el mismo
    vehículo en un día son reales, y un UNIQUE dejaría al segundo conductor sin
    poder registrar —empujándolo a no inspeccionar, que es el fracaso que la
    regla 12 persigue—. Lo que impide que reinspeccionar sea una vía de escape
    es otra cosa: **los hallazgos ya nacidos no se borran**, y cerrarlos es
    `MAESTROS_FLOTA`. Una segunda inspección «toda óptima» deja las dos filas a
    la vista y el daño con su reloj corriendo igual.
    """

    __tablename__ = 'flota_inspeccion'

    id          = db.Column(db.Integer, primary_key=True)
    #: Sin `index=True`: el compuesto `ix_flota_inspeccion_dia` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)

    #: Bajo qué versión del catálogo se respondió. NOT NULL y con FK: una
    #: inspección hecha bajo `camion_v1` tiene que seguir siendo legible dentro
    #: de dos años, y sin esta columna «se contestaron 28 ítems» no dice cuáles.
    plantilla_id = db.Column(
        db.Integer, db.ForeignKey('flota_plantilla_inspeccion.id'),
        nullable=False)

    #: El día operativo de **Bogotá**, no la fecha UTC de `respondida_ts`. Con
    #: `utcnow().date()`, una inspección de las 8 p.m. en Colombia cuenta para
    #: mañana: el vehículo aparecería sin inspección hoy y con dos mañana.
    #: Es la regla 5 del WMS — lo que alguien LEE como día se calcula en su
    #: zona; el timestamp técnico sigue en UTC.
    dia = db.Column(db.Date, nullable=False)

    respondida_ts = db.Column(db.DateTime, nullable=False,
                              default=datetime.utcnow)
    inspeccionada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    #: Regla 3. NOT NULL: una inspección sin kilometraje no se cruza después
    #: contra un preventivo por km ni contra un CPK.
    lectura_id = db.Column(db.Integer,
                           db.ForeignKey('flota_lectura_odometro.id'),
                           nullable=False)

    #: Bajo qué custodia se inspeccionó. Es un hecho, no una imputación
    #: (regla 2). Nullable: un vehículo sin custodia abierta se puede
    #: inspeccionar igual, y negarlo dejaría sin inspección justo al que nadie
    #: recibió.
    custodia_id = db.Column(db.Integer, db.ForeignKey('flota_custodia.id'),
                            nullable=True, index=True)

    veredicto = db.Column(db.String(20), nullable=False)

    #: Los tres conteos que el CHECK cruza contra el veredicto.
    items_esperados      = db.Column(db.Integer, nullable=False)
    items_sin_dato       = db.Column(db.Integer, nullable=False)
    bloqueantes_no_aptos = db.Column(db.Integer, nullable=False)

    #: Regla 11. **Sin umbral y sin `server_default`**: un default de 0 haría
    #: que una inspección insertada sin medir el tiempo se vea idéntica a una
    #: contestada instantáneamente, que es justo el caso que este campo existe
    #: para poder ver.
    segundos_llenado = db.Column(db.Integer, nullable=False)

    observacion = db.Column(db.Text, nullable=True)

    respuestas = db.relationship(
        'RespuestaItem', backref='inspeccion', lazy=True,
        order_by='RespuestaItem.orden_mostrado')

    vehiculo = db.relationship('Vehiculo')

    __table_args__ = (
        _en('veredicto', VEREDICTO),
        # Una inspección de cero ítems no es «todo bien»: es que no se miró
        # nada, y con `items_esperados = 0` el CHECK de coherencia de abajo la
        # declararía `apto` sin que nadie mirara un tornillo.
        db.CheckConstraint('items_esperados > 0',
                           name='ck_flota_insp_items_esperados'),
        db.CheckConstraint(
            'items_sin_dato >= 0 AND items_sin_dato <= items_esperados',
            name='ck_flota_insp_sin_dato_en_rango'),
        db.CheckConstraint(
            'bloqueantes_no_aptos >= 0 '
            'AND bloqueantes_no_aptos <= items_esperados',
            name='ck_flota_insp_bloqueantes_en_rango'),
        # Regla 11: se guarda el hecho. **Ningún techo** — regla 13, no hay
        # una sola medición todavía. Lo único que no puede ser es negativo:
        # eso no es un llenado rápido, es un reloj al revés.
        db.CheckConstraint('segundos_llenado >= 0',
                           name='ck_flota_insp_segundos_no_negativos'),
        # ═══ EL CHECK DE LA REGLA 1 ═══
        #
        # `incompleta` ≠ `no_apto`, en disco y no solo en Python. Las tres
        # ramas son totales sobre el vocabulario y `ELSE 1 = 0` cierra la
        # cuarta: si mañana alguien agrega un veredicto y se olvida de esta
        # expresión, la fila NO entra — el fallo es ruidoso y del lado
        # conservador, en vez de un veredicto nuevo sin ninguna regla.
        #
        # `CASE WHEN` y no aritmética sobre predicados: el 2026-08-01 un CHECK
        # escrito como `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests
        # contra SQLite y reventó el `CREATE TABLE` en el release, porque
        # PostgreSQL no suma booleanos.
        db.CheckConstraint(
            "CASE"
            "  WHEN veredicto = 'no_apto'    THEN bloqueantes_no_aptos > 0"
            "  WHEN veredicto = 'incompleta' THEN bloqueantes_no_aptos = 0"
            "                                 AND items_sin_dato > 0"
            "  WHEN veredicto = 'apto'       THEN bloqueantes_no_aptos = 0"
            "                                 AND items_sin_dato = 0"
            "  ELSE 1 = 0"
            " END",
            name='ck_flota_insp_veredicto_coherente',
        ),
        db.Index('ix_flota_inspeccion_dia', 'vehiculo_id', 'dia'),
    )


class RespuestaItem(db.Model):
    """Lo que se contestó sobre UN ítem, y en qué posición se lo vio.

    **Se escribe una fila por cada ítem del día, incluidos los que nadie tocó.**
    Un ítem sin fila y un ítem contestado `sin_dato` se leerían igual desde el
    tablero y significan cosas distintas: el primero no permite distinguir «no
    lo miró» de «ese día el ítem no aplicaba». La regla 1 no es solo que el
    default no sea `optimo`: es que la ausencia quede escrita como ausencia.

    `orden_mostrado` es lo que hace **auditable el barajado**. El orden lo
    decide `dominio.inspeccion.items_del_dia` a partir de la fecha, así que es
    reconstruible; guardarlo igual permite comprobar que lo que se mostró fue
    de verdad lo que la política dice, en vez de creerle a la política.

    Se llena en el SERVIDOR, no con lo que manda el cliente: si viniera del
    JSON, quien quisiera podría declarar cualquier orden y el registro
    afirmaría una pantalla que nadie vio.
    """

    __tablename__ = 'flota_respuesta_item'

    id            = db.Column(db.Integer, primary_key=True)
    inspeccion_id = db.Column(db.Integer, db.ForeignKey('flota_inspeccion.id'),
                              nullable=False, index=True)
    item_id       = db.Column(db.Integer,
                              db.ForeignKey('flota_item_inspeccion.id'),
                              nullable=False)

    #: **Sin `server_default`, a propósito.** Un `server_default='sin_dato'`
    #: sería cómodo y sería la regla 1 rota por la puerta de atrás: quien
    #: inserta tendría permitido no decir qué contestó. Acá no hay valor por
    #: defecto para ningún ítem, ni siquiera el conservador.
    respuesta = db.Column(db.String(20), nullable=False)

    orden_mostrado = db.Column(db.Integer, nullable=False)

    #: Lo que el conductor escribió sobre ESTE ítem. Opcional: exigir texto
    #: para marcar una falla le pone precio a reportarla, y lo que se paga con
    #: eso es que se marque óptimo.
    nota = db.Column(db.Text, nullable=True)

    #: El daño que nació de esta respuesta, si nació. Nullable porque la
    #: mayoría de las respuestas son `optimo` y no producen ninguno.
    hallazgo_id = db.Column(db.Integer, db.ForeignKey('flota_hallazgo.id'),
                            nullable=True)

    item = db.relationship('ItemInspeccion', lazy='joined')

    __table_args__ = (
        _en('respuesta', RESPUESTA_ITEM),
        db.CheckConstraint('orden_mostrado > 0',
                           name='ck_flota_resp_orden_positivo'),
        # **Solo un `no_apto` cuelga un hallazgo.**
        #
        # Las dos direcciones que esto cierra son distintas y las dos importan:
        #  · un `optimo` con hallazgo es un daño que el registro dice que no
        #    existe — la fila afirma dos cosas contrarias a la vez;
        #  · un `sin_dato` con hallazgo es peor: convierte «no sé» en «está
        #    mal», que es exactamente la confusión que separa `incompleta` de
        #    `no_apto`. No saber no produce daños; produce una inspección que
        #    no autoriza.
        db.CheckConstraint(
            "hallazgo_id IS NULL OR respuesta = 'no_apto'",
            name='ck_flota_resp_hallazgo_solo_si_no_apto'),
        # El mismo ítem no se contesta dos veces en la misma inspección: con
        # dos filas, los conteos del padre dejan de describir a los hijos y el
        # CHECK del veredicto pasa a validar contra un número inventado.
        db.UniqueConstraint('inspeccion_id', 'item_id',
                            name='uq_flota_resp_item'),
        # Y dos ítems no ocupan la misma posición en pantalla: `orden_mostrado`
        # promete ser la pantalla que se vio, y una pantalla con dos ítems en
        # el renglón 3 no existió.
        db.UniqueConstraint('inspeccion_id', 'orden_mostrado',
                            name='uq_flota_resp_orden'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# gasto — la espina de la plata que sale (fase 1, 2026-09-01)
# ═══════════════════════════════════════════════════════════════════════════

class Gasto(db.Model):
    """Un peso que salió por un vehículo, con su kilometraje y su referencia.

    ## Una espina, no una tabla por tipo de gasto

    Combustible, taller, SOAT, llantas y peajes escriben **acá**, y lo que
    cambia entre ellos cuelga de una extremidad especializada
    (`flota_tanqueo` hoy; `flota_intervencion` y `flota_montaje_llanta` en las
    fases 2 y 4). Si cada tabla llevara su propia columna de valor, el CPK sería
    un `UNION` de N ramas y alguien va a olvidar la N+1.

    **Eso ya pasó en este repo, con plata.** `_BODEGA_CO_MAP` existía tres veces
    con 10, 9 y 8 entradas; la de 8 era la que usaba la vía viva, `.get('NS2')`
    devolvía `None` y el documento salía con el centro de operación equivocado.
    El trinquete estaba en verde porque leía la copia buena. Ver CLAUDE.md,
    «Bodega → CO: y ningún otro sitio».

    Y el trinquete del plan es literal: *si aparece una columna `valor`, `costo`
    o `monto` en una tabla `flota_*` fuera de `flota_gasto`, la frontera se
    cruzó.*

    ## Qué significa `valor`, y qué NO significa

    El §4 del plan («Dónde vive el dinero») ya lo decidió: **el WMS registra qué
    se hizo, a qué vehículo, con qué kilometraje y quién lo registró; el valor,
    el proveedor, el impuesto y la causación viven en Siesa**, y el puente es una
    **referencia** (`documento_numero`), no una copia.

    `valor` existe para poder dividirlo entre kilómetros. No reemplaza la
    contabilidad, no cuadra con un balance y no se manda a ningún conector: los
    16 conectores registrados hacen otras cosas —142948 va contra el catálogo de
    ítems, 142951 es ajuste de inventario, 142882 son retenciones— y **causar una
    factura de mantenimiento es una cuenta por pagar, para la que no hay
    conector**. La pregunta que este número contesta —*«¿cuánto me cuesta el
    kilómetro del TGZ653?»*— el ERP no la contesta hoy, y no porque le falte la
    plata: le falta el kilómetro.

    ## `proveedor` es texto, y es a propósito

    `Proveedor` existe en el WMS y su `codigo` es el código Siesa, `unique NOT
    NULL`. El taller de la esquina de Pitalito no lo tiene hasta que contabilidad
    lo dé de alta con NIT y retenciones. Reusar esa tabla obligaría a **inventar
    un código de maestro** — el movimiento exacto que la lección de `almacenes`
    prohíbe, esta vez con consecuencia fiscal. El plan lo lista bajo «lo que NO
    haría»: texto normalizado más un desplegable de los ya usados.

    ## Lo que esta tabla NO tiene, y por qué

    · **Ninguna columna de conductor ni de custodia.** No es un olvido: es la
      regla 2 escrita en el esquema. Quién manejaba está en la custodia y se
      cruza a mano cuando alguien decida investigar. Una FK acá sería la columna
      con la que se arma el ranking de conductores por km/galón que el plan
      prohíbe expresamente — y cuya respuesta, para quien no quiere hacer el
      trabajo, es **tanquear a medias y declarar lleno**.
    · **Ninguna columna de aprobación.** En esta fase nadie aprueba un gasto
      dentro del WMS. La aprobación real es la causación en Siesa, y un segundo
      «aprobado» acá sería un estado que contradice al ERP sin poder corregirlo.
      Además `control_flota` —el rol que registraría— tiene escrito en
      `Roles.GESTION` que **no aprueba gastos** (FLO-PR-01).
    · **Ninguna columna `centro_op` derivada de una bodega.** `centro_op` se
      declara, no se deduce: `app/services/bodegas.py::co_de_bodega()` es la
      única función que contesta bodega→CO en este repo y esta columna no la
      duplica.

    ## Regla 3 — `lectura_id` NOT NULL

    Un gasto sin kilometraje no se puede dividir entre kilómetros, que es lo
    único para lo que existe. La lectura la ancla `adaptadores.gastos`
    reutilizando la política de `hallazgos._anclar_odometro`: si el número
    coincide con la última, se reutiliza esa fila en vez de fabricar ruido que
    `lecturas_ts_duplicado` existe para contar.
    """

    __tablename__ = 'flota_gasto'

    id          = db.Column(db.Integer, primary_key=True)
    #: Sin `index=True`: el compuesto `ix_flota_gasto_ventana` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)

    categoria = db.Column(db.String(20), nullable=False)

    #: El día del hecho: cuándo se tanqueó, cuándo entró al taller, cuándo se
    #: pagó el SOAT. **No es el período que cubre** — para eso están las dos
    #: columnas de abajo, y confundirlos es cargarle el SOAT entero al CPK de un
    #: día.
    fecha = db.Column(db.Date, nullable=False)

    #: Pesos. Ver el docstring: existe para dividirlo entre kilómetros.
    valor = db.Column(db.Numeric(14, 2), nullable=False)

    #: Regla 3. NOT NULL: un gasto sin kilometraje no alimenta ningún CPK.
    lectura_id = db.Column(db.Integer,
                           db.ForeignKey('flota_lectura_odometro.id'),
                           nullable=False)

    #: Texto normalizado, no FK a `proveedores`. Ver el docstring.
    proveedor = db.Column(db.Text, nullable=False)

    #: **El puente hacia Siesa.** Nullable, y el NULL es un estado que se cuenta
    #: (`gastos_sin_documento` en el health): significa que el gasto existe
    #: operativamente —se sabe qué se hizo, a qué vehículo y con qué
    #: kilometraje— y **no se puede cruzar con la causación**. Es exactamente el
    #: número que dice si la operación está entregando las facturas, que hoy se
    #: están perdiendo.
    #:
    #: NULL y no cadena vacía: dos representaciones de la misma ausencia son dos
    #: consultas que dan números distintos. El CHECK impide la cadena vacía.
    documento_numero = db.Column(db.String(60), nullable=True)

    #: Qué período cubre el gasto. **El SOAT no se carga al CPK del día que se
    #: pagó.** Para lo que se consume el mismo día, los dos valen `fecha` — y eso
    #: lo decide `costos.exige_periodo(categoria)`, no una casilla que quien
    #: registra pueda dejar sin marcar (regla 11).
    periodo_desde = db.Column(db.Date, nullable=False)
    periodo_hasta = db.Column(db.Date, nullable=False)

    #: Centro de operación al que se imputa. Declarado, nunca deducido de una
    #: bodega. NULL = no se declaró.
    centro_op = db.Column(db.String(10), nullable=True)

    #: De dónde salió la plata. **Sin `server_default`**: hay que escribir la
    #: palabra, aunque la palabra sea `sin_dato`. Es la pregunta que el dueño no
    #: contestó todavía —¿tarjeta/convenio o efectivo del conductor?— y la
    #: columna aguanta las dos respuestas sin migrar. Si es efectivo, el registro
    #: es además una legalización de gasto y cambia quién aprueba.
    origen_costo = db.Column(db.String(20), nullable=False)

    #: Qué fue, cuando la categoría no alcanza. Obligatoria para `otro` y libre
    #: para el resto. Es la válvula que le faltó al catálogo de documentos: los
    #: cuatro tipos que el dueño pidió después de cerrarlo **dan 400**.
    descripcion = db.Column(db.Text, nullable=True)

    #: Quién lo registró. Un hecho, no una imputación (regla 2).
    registrado_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    creado_ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    vehiculo = db.relationship('Vehiculo')
    lectura  = db.relationship('LecturaOdometro')

    __table_args__ = (
        _en('categoria', CATEGORIA_GASTO),
        _en('origen_costo', ORIGEN_COSTO),
        # Un gasto de $0 no es un gasto barato: es una fila que alguien guardó
        # sin saber el valor. Rechazar en vez de sumarlo como cero mantiene el
        # CPK diciendo la verdad sobre lo que se registró.
        db.CheckConstraint('valor > 0', name='ck_flota_gasto_valor_positivo'),
        db.CheckConstraint('length(trim(proveedor)) > 0',
                           name='ck_flota_gasto_proveedor'),
        # NULL o un número de verdad. La cadena vacía sería una tercera forma de
        # decir «no hay» que ninguna consulta cuenta igual que las otras dos.
        db.CheckConstraint(
            'documento_numero IS NULL OR length(trim(documento_numero)) > 0',
            name='ck_flota_gasto_documento_no_vacio'),
        db.CheckConstraint(
            'centro_op IS NULL OR length(trim(centro_op)) > 0',
            name='ck_flota_gasto_centro_op_no_vacio'),
        db.CheckConstraint('periodo_hasta >= periodo_desde',
                           name='ck_flota_gasto_periodo_coherente'),
        # `otro` es la válvula del catálogo cerrado y no puede ser la salida
        # cómoda: cuesta escribir qué fue. Sin esto, `otro` se convierte en el
        # 40% de los gastos y el desglose por categoría deja de decir nada.
        db.CheckConstraint(
            "categoria <> 'otro' OR "
            "(descripcion IS NOT NULL AND length(trim(descripcion)) > 0)",
            name='ck_flota_gasto_otro_con_descripcion'),
        # **La misma factura no se registra dos veces sobre el mismo vehículo.**
        #
        # Es el invariante de idempotencia de esta fase: un CPK que cuenta dos
        # veces el mismo tanqueo no se ve mal —es un número plausible— y no hay
        # forma de detectarlo después. Va en la base y no en el adaptador porque
        # la vía típica de la doble carga es un reintento del formulario, y el
        # adaptador ya devolvió.
        #
        # Por vehículo y no global: una misma factura puede cubrir dos vehículos
        # tanqueados en la misma parada, y eso pasa de verdad. Los NULL son
        # distintos entre sí en PostgreSQL y en SQLite, así que cualquier
        # cantidad de gastos sin documento convive.
        db.UniqueConstraint('vehiculo_id', 'documento_numero',
                            name='uq_flota_gasto_documento'),
        # El índice de la consulta del CPK: gastos de un vehículo cuyo período
        # toca una ventana. `periodo_hasta` primero porque es el extremo que más
        # descarta.
        db.Index('ix_flota_gasto_ventana', 'vehiculo_id', 'periodo_hasta',
                 'periodo_desde'),
    )


class Tanqueo(db.Model):
    """Lo que solo tiene un tanqueo. Cuelga de la espina, no la reemplaza.

    La PK **es** el gasto: no puede haber dos tanqueos del mismo gasto, y no hay
    tanqueo sin gasto. Misma forma que `FichaTecnica`, cuya PK es el vehículo.

    `precio_galon` **no está, y no es un olvido.** Con `valor` en el gasto y
    `galones` acá, una tercera columna con el precio es un dato que puede
    contradecir a los otros dos: el día que alguien corrija el valor de una
    factura mal digitada, el precio queda apuntando al número viejo. Es la misma
    decisión que el plan toma para el `km_acumulado` de una llanta —*no se
    guarda, se calcula*— por el mismo motivo: **denormalizar es garantizar
    divergencia**. Lo que el plan pide —«precio del galón por estación»— sale de
    `costos.precio_por_galon()` agrupado por `estacion`, y no se pierde nada.

    ## `tanque` es el campo del que depende que el rendimiento exista

    `lleno | parcial | sin_dato`, **sin `server_default`**. El rendimiento solo
    es calculable de tanque lleno a tanque lleno: sobre uno parcial, dividir
    kilómetros entre galones da un número que varía con lo que quedaba adentro.

    Un `lleno` puesto por inercia no produce un error — produce una ventana
    basura que infla o hunde la métrica sin que nada se vea raro. Por eso las
    tres opciones se eligen a mano y ninguna viene preseleccionada: es la regla 1
    del módulo aplicada al único campo de esta fase que decide si una medición
    existe.

    Y es también la respuesta a la regla 11: quien no quiere hacer el trabajo
    marca `lleno` sin mirar. Lo que lo contiene no es prohibirlo, es que
    `sin_dato` sea una respuesta legítima y visible — y que el rendimiento que
    sale de ventanas basura no le mejore el número a nadie, porque **el
    rendimiento no se publica por conductor**.
    """

    __tablename__ = 'flota_tanqueo'

    gasto_id = db.Column(db.Integer, db.ForeignKey('flota_gasto.id'),
                         primary_key=True)

    galones = db.Column(db.Numeric(8, 3), nullable=False)

    #: `lleno | parcial | sin_dato`. Ver el docstring: sin default, a propósito.
    tanque = db.Column(db.String(12), nullable=False)

    #: Dónde se tanqueó. Texto normalizado, sin maestro — igual que `proveedor`
    #: en la espina, y por el mismo motivo. Es lo que permite contestar «cuánto
    #: cuesta el galón en cada estación» sin crear un formulario más antes de
    #: registrar un gasto, que es el gesto que no se hace.
    estacion = db.Column(db.Text, nullable=False)

    gasto = db.relationship('Gasto', backref=db.backref('tanqueo', uselist=False))

    __table_args__ = (
        _en('tanque', ESTADO_TANQUE),
        # Cero galones no es un tanqueo gratis: es una fila mal cargada, y
        # dividir por ella es lo que produce un rendimiento infinito en un
        # tablero.
        db.CheckConstraint('galones > 0', name='ck_flota_tanqueo_galones'),
        db.CheckConstraint('length(trim(estacion)) > 0',
                           name='ck_flota_tanqueo_estacion'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# taller — la visita, y los trabajos que produjo (fase 2)
# ═══════════════════════════════════════════════════════════════════════════


class OrdenTrabajo(db.Model):
    """Una visita al taller: por qué entró el camión, a dónde, y cómo terminó.

    ## La decisión que define esta tabla: **NO lleva el costo**

    El camión entra hoy y la factura llega el 30. Una OT con `valor NOT NULL`
    obliga a inventar un número para poder cerrarla — y un número inventado
    dentro del módulo cuyo lema es que inventarlo es peor que no tenerlo es
    exactamente lo que ya pasó con el `2045-08-20` de la tarjeta de propiedad.

    La plata llega después, apuntando acá desde `flota_intervencion.gasto_id`, y
    **mientras tanto la ausencia se cuenta**: `trabajos_sin_factura` en el
    health. Es la misma disciplina de `gastos_sin_documento` — un hueco que se
    mide no es un hueco que se esconde.

    El trinquete del plan es literal y hay un test que lo ejerce: *si aparece una
    columna `valor`, `costo` o `monto` en una tabla `flota_*` fuera de
    `flota_gasto`, la frontera se cruzó.*

    ## Lo que esta tabla NO tiene, y por qué

    · **Ninguna columna de garantía.** Va en la intervención. Una visita produce
      varios trabajos con garantías distintas —el embrague trae 6 meses, el
      cambio de aceite ninguna— y un campo acá obliga a dar una sola respuesta
      para una bolsa mezclada. La respuesta única que no miente es «ninguna», que
      es la peor de todas.
    · **Ninguna columna de mecánico ni de responsable.** Regla 2 escrita en el
      esquema. Está `abierta_por_usuario_id`, que es quién la registró: un hecho.
    · **Ninguna lectura de cierre.** El kilometraje que importa es el del
      trabajo, porque es contra ése que se calcula la garantía por kilómetros.
      Una segunda lectura al devolver el vehículo sería el ruido que
      `lecturas_ts_duplicado` existe para contar, producido por el sistema. Queda
      declarado en ESTADO.md con su condición de disparo.

    ## Regla 3 — `lectura_id` NOT NULL

    El `flota/CLAUDE.md` nombra la orden de trabajo por su nombre: *«Inspección,
    custodia, orden de trabajo, tanqueo: todos llevan kilometraje»*. Sin él no se
    puede auditar si un mantenimiento era necesario, ni calcular hasta qué
    kilómetro cubre lo que se reparó.

    La lectura la ancla `adaptadores.taller` reutilizando
    `hallazgos.anclar_odometro` con `origen='ot'` — no una copia (regla 0). El
    valor `ot` estaba en `OrigenLectura` desde la tanda 1 y **sin nada detrás**;
    esta tabla es lo que había detrás.
    """

    __tablename__ = 'flota_orden_trabajo'

    id = db.Column(db.Integer, primary_key=True)
    #: Sin `index=True`: el compuesto `ix_flota_ot_abiertas` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)

    #: `correctiva | preventiva`. Ver el vocabulario en `dominio/taller.py`:
    #: `garantia` NO es un tipo — quién paga es un hecho de la factura, y al
    #: abrir la OT todavía no se sabe.
    tipo = db.Column(db.String(20), nullable=False)

    estado = db.Column(db.String(20), nullable=False,
                       server_default='abierta', index=True)

    #: A dónde se mandó. Texto normalizado, **no FK a `proveedores`** — igual
    #: que `flota_gasto.proveedor` y por el mismo motivo: el taller de la esquina
    #: de Pitalito no tiene código Siesa hasta que contabilidad lo dé de alta con
    #: NIT y retenciones, y reusar esa tabla obligaría a inventar un código de
    #: maestro. La lección de `almacenes`, esta vez con consecuencia fiscal.
    taller = db.Column(db.Text, nullable=False)

    #: Qué se pidió hacer. NOT NULL: una OT sin descripción es un camión que
    #: alguien mandó al taller y nadie sabe a qué.
    descripcion = db.Column(db.Text, nullable=False)

    #: Regla 3. NOT NULL: el kilometraje del trabajo es contra el que se calcula
    #: la garantía por kilómetros, y sin él esa mitad no existe.
    lectura_id = db.Column(db.Integer,
                           db.ForeignKey('flota_lectura_odometro.id'),
                           nullable=False)

    #: De qué daño nació, si nació de uno. Nullable porque una preventiva no
    #: sale de ningún hallazgo, y porque un camión se puede mandar al taller sin
    #: haber reportado el daño primero.
    #:
    #: Cerrar la OT **puede** cerrar el hallazgo, y lo hace pasando por
    #: `hallazgos.cerrar` — no con un `UPDATE` propio. Una segunda vía de cierre
    #: sería la que se olvide de mover `cerrado_ts`, y `dias_hallazgo_abierto`
    #: se calcula contra ese campo.
    hallazgo_id = db.Column(db.Integer, db.ForeignKey('flota_hallazgo.id'),
                            nullable=True, index=True)

    abierta_ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    abierta_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    cerrada_ts = db.Column(db.DateTime, nullable=True)
    cerrada_por_usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'),
                                       nullable=True)

    #: Obligatorio al ANULAR. Un anulado sin motivo es indistinguible de hacer
    #: desaparecer una visita incómoda — mismo criterio que `motivo_cierre` del
    #: hallazgo, y el CHECK lo respalda.
    motivo_cierre = db.Column(db.Text, nullable=True)

    vehiculo = db.relationship('Vehiculo')
    lectura  = db.relationship('LecturaOdometro')
    hallazgo = db.relationship('Hallazgo')

    __table_args__ = (
        _en('tipo', TIPO_OT),
        _en('estado', ESTADO_OT),
        db.CheckConstraint('length(trim(taller)) > 0',
                           name='ck_flota_ot_taller'),
        db.CheckConstraint('length(trim(descripcion)) > 0',
                           name='ck_flota_ot_descripcion'),
        # Coherencia de desenlace, en UNA expresión y no en tres CHECK sueltos:
        # los tres estados son un vocabulario y partirlo deja huecos entre las
        # piezas. Misma forma que `ck_flota_hallazgo_desenlace`.
        #
        # `CASE WHEN` y no aritmética sobre predicados: el 2026-08-01 un CHECK
        # escrito como `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests
        # contra SQLite y reventó el `CREATE TABLE` en el release, porque
        # PostgreSQL no suma booleanos.
        db.CheckConstraint(
            "CASE"
            "  WHEN estado = 'abierta' THEN cerrada_ts IS NULL"
            "  WHEN estado = 'cerrada' THEN cerrada_ts IS NOT NULL"
            "  WHEN estado = 'anulada' THEN"
            "       cerrada_ts IS NOT NULL AND motivo_cierre IS NOT NULL"
            "       AND length(trim(motivo_cierre)) > 0"
            "  ELSE 1 = 1"
            " END",
            name='ck_flota_ot_desenlace',
        ),
        db.Index('ix_flota_ot_abiertas', 'vehiculo_id', 'estado'),
    )


class Intervencion(db.Model):
    """Un trabajo concreto hecho en una visita, con su garantía calculada.

    **Es la segunda extremidad de la espina `flota_gasto`**, después de
    `flota_tanqueo`. Si cada tabla llevara su propia columna de valor, el CPK
    sería un `UNION` de N ramas y alguien va a olvidar la N+1 — que es
    `_BODEGA_CO_MAP` otra vez, con plata.

    ## Se parece a `flota_tanqueo` y difiere en UNA cosa, que lo cambia todo

    `flota_tanqueo` tiene `gasto_id` de PK: no hay tanqueo sin gasto porque se
    paga en el surtidor, en el mismo instante. Acá `gasto_id` es **nullable y no
    es la PK**, porque el trabajo se hace hoy y la factura llega el 30. Con
    `gasto_id` de PK, una intervención no podría nacer hasta que llegara la
    factura — y entonces «trabajos hechos sin factura recibida» sería un número
    imposible de contar, porque esas filas no existirían.

    Y **no es único**: una factura del taller cubre las tres cosas que se
    hicieron en la visita, y las tres apuntan al mismo `flota_gasto`. Eso no
    duplica plata — el CPK suma la espina, no las extremidades — y es la razón
    exacta por la que el valor vive en un solo lugar.

    QUÉ NO AFIRMA `gasto_id`: que ese gasto sea el costo de ESTA intervención.
    Afirma que la factura que lo respalda cubre este trabajo. Repartir el valor
    entre los trabajos de una visita es inventar un desglose que la factura no
    trae, y ese número inventado terminaría en un CPK.

    ## La garantía se CALCULA al nacer

    `garantia_hasta_fecha` y `garantia_hasta_km` no se teclean nunca: salen de
    `dominio/taller.py` a partir de lo que la factura declara (`garantia_meses`,
    `garantia_km`) y del día y el kilometraje del trabajo. Es la regla 6 del
    módulo aplicada a otra cosa — si alguien puede escribir «ésta vence en
    marzo», la garantía dejó de significar algo.

    Lo que el CHECK puede respaldar y lo que no, dicho sin disimulo:

    · **Sí** impide una fila con `garantia_hasta_fecha` y sin `garantia_meses`,
      en las dos direcciones. O sea: no se puede escribir un vencimiento que no
      salga de un plazo declarado.
    · **No** puede verificar la aritmética. Que el número sea el que la función
      calcula lo garantiza que haya **una sola puerta** (`adaptadores.taller`) y
      que la frontera devuelva 400 —no un silencio— si el cuerpo trae los campos
      derivados. Un CHECK no puede sumar meses.

    ## `garantia_declarada` no tiene default, y son TRES valores

    Una factura que no dice nada **no es una factura sin garantía**: es que no se
    preguntó. `no` cierra la puerta a reclamar; `sin_dato` la deja abierta con la
    duda escrita. Regla 4, y sin `server_default`: hay que escribir la palabra.

    ## Lo que esta tabla NO tiene

    · **Ninguna columna de valor, costo ni monto.** Ver arriba.
    · **Ninguna lectura propia.** El kilometraje del trabajo es el de la OT: la
      intervención no es un evento independiente, existe solo dentro de una
      visita, y la visita ya lleva su lectura anclada por la regla 3. Dos
      lecturas para una misma visita serían el ruido que `lecturas_ts_duplicado`
      cuenta, producido desde adentro del sistema.
    · **Ninguna columna de «cubierto por garantía».** Quién paga lo decide el
      taller cuando se le reclama; el sistema propone el caso y no lo cierra.
    """

    __tablename__ = 'flota_intervencion'

    id = db.Column(db.Integer, primary_key=True)

    #: Sin `index=True`: el compuesto `ix_flota_interv_sistema` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    orden_trabajo_id = db.Column(
        db.Integer, db.ForeignKey('flota_orden_trabajo.id'), nullable=False)

    #: **La plata, cuando llegue.** NULL no es un error: es «el trabajo está
    #: hecho y la factura no llegó», que es el estado normal entre el día 1 y el
    #: día 30 y el número que el health cuenta.
    gasto_id = db.Column(db.Integer, db.ForeignKey('flota_gasto.id'),
                         nullable=True, index=True)

    #: Qué parte se tocó. **Es la clave de la búsqueda de garantía vigente**: se
    #: compara sistema contra sistema del mismo vehículo. Por eso es vocabulario
    #: cerrado y no texto libre — «caja» y «transmisión» son la misma pieza
    #: escrita de dos formas, y el día que no coincidan se paga dos veces.
    sistema = db.Column(db.String(30), nullable=False)

    #: Qué se hizo. Obligatoria para `otro` (CHECK) y libre para el resto, igual
    #: que en `flota_gasto`: `otro` es la válvula del catálogo cerrado y no puede
    #: ser la salida cómoda.
    descripcion = db.Column(db.Text, nullable=True)

    #: `si | no | sin_dato`. **Sin `server_default`**: ver el docstring.
    garantia_declarada = db.Column(db.String(12), nullable=False)

    #: Lo que la factura dice. Los dos nullables porque una garantía puede venir
    #: solo por meses, solo por kilómetros, o por las dos.
    garantia_meses = db.Column(db.Integer, nullable=True)
    garantia_km    = db.Column(db.Integer, nullable=True)

    #: **Calculados, nunca tecleados.** `hasta_fecha` es el ÚLTIMO día cubierto
    #: (inclusive) y no un instante: un plazo guardado como instante vence a la
    #: hora en que se hizo el trabajo, y una garantía dada a las 4 p.m. estaría
    #: vencida a las 4:01 p.m. del último día para quien la reclama a las 5.
    garantia_hasta_fecha = db.Column(db.Date, nullable=True)
    garantia_hasta_km    = db.Column(db.Integer, nullable=True)

    registrada_ts = db.Column(db.DateTime, nullable=False,
                              default=datetime.utcnow)
    registrada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    orden = db.relationship('OrdenTrabajo',
                            backref=db.backref('intervenciones', lazy='select'))
    gasto = db.relationship('Gasto')

    __table_args__ = (
        _en('sistema', SISTEMA),
        _en('garantia_declarada', GARANTIA_DECL),
        db.CheckConstraint(
            "sistema <> 'otro' OR "
            "(descripcion IS NOT NULL AND length(trim(descripcion)) > 0)",
            name='ck_flota_interv_otro_con_descripcion'),
        # Una garantía de cero meses no es una garantía corta: es una garantía
        # que no se declaró, y para eso está `garantia_declarada`.
        db.CheckConstraint('garantia_meses IS NULL OR garantia_meses > 0',
                           name='ck_flota_interv_meses_positivos'),
        db.CheckConstraint('garantia_km IS NULL OR garantia_km > 0',
                           name='ck_flota_interv_km_positivos'),
        # **El vencimiento no existe sin el plazo del que sale**, en las dos
        # direcciones y por cada dimensión. Es lo que la base SÍ puede respaldar
        # de la regla «se calcula, no se teclea»: no puede verificar la suma,
        # pero sí puede impedir una fecha de vencimiento que no venga de ningún
        # plazo declarado — que es la forma en que alguien la escribiría a mano.
        #
        # `CASE WHEN` y no `(a IS NULL) = (b IS NULL)`: ver
        # `ck_flota_hallazgo_desenlace`. PostgreSQL no suma booleanos y el
        # release del 2026-08-01 se perdió por eso.
        db.CheckConstraint(
            "CASE WHEN garantia_meses IS NULL"
            "     THEN garantia_hasta_fecha IS NULL"
            "     ELSE garantia_hasta_fecha IS NOT NULL END",
            name='ck_flota_interv_fecha_viene_de_meses'),
        db.CheckConstraint(
            "CASE WHEN garantia_km IS NULL"
            "     THEN garantia_hasta_km IS NULL"
            "     ELSE garantia_hasta_km IS NOT NULL END",
            name='ck_flota_interv_km_viene_de_km'),
        # Y la coherencia con la palabra: `no` y `sin_dato` no pueden traer
        # plazos. Sin esto, una intervención podría decir «no hay garantía» y
        # traer un vencimiento adentro — un nombre que promete una cosa y
        # contiene otra, que es el modo de falla más caro de este repo.
        #
        # `si` **sí exige al menos una dimensión**: declarar que hay garantía sin
        # decir hasta cuándo ni hasta cuántos kilómetros es una garantía que no
        # se puede reclamar, y peor que ninguna porque se ve verde.
        db.CheckConstraint(
            "CASE"
            "  WHEN garantia_declarada = 'si'"
            "  THEN garantia_meses IS NOT NULL OR garantia_km IS NOT NULL"
            "  ELSE garantia_meses IS NULL AND garantia_km IS NULL"
            " END",
            name='ck_flota_interv_garantia_coherente'),
        # El índice de la búsqueda que hace que esto valga la plata: las
        # intervenciones de un vehículo sobre un sistema. El vehículo no está en
        # esta tabla —está en la OT—, así que el índice va por la OT y el
        # sistema, que es el par que la consulta filtra después del join.
        db.Index('ix_flota_interv_sistema', 'orden_trabajo_id', 'sistema'),
    )


# ═══════════════════════════════════════════════════════════════════════════
# llanta + montaje — la llanta es una entidad, no un renglón de factura
# (fase 4, 2026-09-02)
# ═══════════════════════════════════════════════════════════════════════════

class Llanta(db.Model):
    """Una llanta física, con identidad que **sobrevive al vehículo**.

    ## Por qué es una entidad y no un tipo de repuesto

    Una llanta rota de posición, pasa a otro camión, va a reencauche y vuelve.
    Como renglón de una factura de repuesto, la pregunta que importa —*«¿cuánto
    duran?»*— **no se puede ni formular**: la factura dice que se compraron seis,
    no cuál está dónde ni desde cuándo. Y el modo de fallo caro no es que se
    gasten, sino que se gasten **mal** —desalineación, presión, un eje que come
    el flanco interno—, y eso solo se ve por posición a lo largo del tiempo. Sin
    identidad no hay «a lo largo del tiempo».

    ## Ninguna columna de dinero, y no es un olvido

    La compra es un `flota_gasto` con `categoria='llanta'` (ya estaba en el
    vocabulario). El trinquete del plan es literal: *si aparece una columna
    `valor`, `costo` o `monto` en una tabla `flota_*` fuera de `flota_gasto`, la
    frontera se cruzó* — porque con el valor repartido en N tablas el CPK se
    vuelve un `UNION` de N ramas y alguien va a olvidar la N+1. Ya pasó en este
    repo con `_BODEGA_CO_MAP`, y con plata.

    El puente entre las dos cosas es `MontajeLlanta.gasto_id`, y es nullable:
    montar una llanta que ya se tenía —una rotación— no cuesta una factura nueva.

    ## Ninguna columna `km_acumulado`

    **Se calcula, no se guarda** (`flota.dominio.llantas.km_acumulado`). Con 24
    llantas, denormalizar es garantizar divergencia: el día que alguien corrija
    un kilometraje de montaje, la columna queda apuntando al número viejo y nadie
    se entera. Es la misma decisión que `flota_tanqueo` toma con `precio_galon`.

    ## Ninguna columna de estado

    `en_uso` / `de_baja` / `en_reencauche` **no existe a propósito**, y la mitad
    del motivo es que sería una segunda fuente de verdad: «montada» ya lo
    contesta `flota_montaje_llanta` con `fin_ts IS NULL`, y una columna que dijera
    lo mismo podría contradecirla sin que ningún CHECK pudiera verlo (un CHECK no
    mira otras filas). La otra mitad es la regla 12: **todavía no se dio de baja
    una sola llanta**, y el gesto lo define la primera que ocurra de verdad. Ver
    la deuda declarada en `docs/flota/ESTADO.md`.
    """

    __tablename__ = 'flota_llanta'

    id = db.Column(db.Integer, primary_key=True)

    #: Lo que identifica a ESTA llanta y no a otra igual. Es la marcación que la
    #: operación le pone (o el DOT si se puede leer), y es **única en toda la
    #: flota**: dos llantas con el mismo código son dos historias mezcladas, y
    #: mezcladas no se pueden separar después.
    #:
    #: Único global y no por vehículo a propósito: la identidad de una llanta
    #: sobrevive al camión, que es la razón entera de que esta tabla exista.
    codigo = db.Column(db.String(40), nullable=False, unique=True)

    #: Marca del fabricante. `sin_dato` es una respuesta legítima y es el
    #: `server_default` porque **no decide nada**: no cambia dónde se puede
    #: montar ni cómo se calcula un kilómetro. Los campos que sí deciden
    #: (`medida`, `posicion`) no tienen default.
    marca = db.Column(db.String(40), nullable=False, server_default='sin_dato')

    #: `215/75R17.5` y demás. Se lee del flanco. **Sin default**: es el dato que
    #: dice en qué vehículos entra, y se compara contra
    #: `flota_ficha_tecnica.medida_llanta`.
    #:
    #: La base NO impide montar una llanta cuya medida no coincida con la de la
    #: ficha, y eso está declarado en `ESTADO.md` en vez de disimulado: la medida
    #: de la ficha es nullable, así que exigir coincidencia trabaría los
    #: vehículos cuyo levantamiento no la trae — imponer antes de medir.
    medida = db.Column(db.Text, nullable=False)

    #: Quién la dio de alta. Un hecho, no una imputación (regla 2).
    registrada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    creada_ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint('length(trim(codigo)) > 0',
                           name='ck_flota_llanta_codigo_no_vacio'),
        # Una llanta sin medida no se puede comparar con la ficha de ningún
        # vehículo, y la comparación es media razón por la que la tabla existe.
        db.CheckConstraint('length(trim(medida)) > 0',
                           name='ck_flota_llanta_medida_no_vacia'),
    )


class MontajeLlanta(db.Model):
    """Una llanta puesta en una posición de un vehículo. **Es la forma de
    `flota_custodia`**: intervalo abierto, lectura de apertura y de cierre, y
    `fin_ts IS NULL` = vigente.

    No es parecido por gusto: es el mismo problema. Una custodia responde «quién
    tiene este camión desde cuándo y hasta cuándo»; un montaje responde «qué
    llanta está en esta posición desde cuándo y hasta cuándo». Las dos tienen las
    mismas dos combinaciones imposibles y se impiden con el mismo mecanismo — dos
    índices únicos parciales sobre las filas abiertas.

    ## Los dos índices, y qué impide cada uno

    | Índice | Combinación imposible que impide |
    |---|---|
    | `uq_flota_montaje_posicion_vigente` (`vehiculo_id`, `posicion`) | **Dos llantas en la misma posición del mismo vehículo.** Una posición tiene una llanta |
    | `uq_flota_montaje_llanta_vigente` (`llanta_id`) | **Una llanta montada en dos sitios a la vez** — dos vehículos, o dos posiciones del mismo |

    Los dos son PARCIALES sobre `fin_ts IS NULL`, así que solo indexan las filas
    abiertas: cualquier cantidad de montajes **cerrados** de la misma posición
    convive, que es la historia entera de esa posición y es exactamente lo que hay
    que poder leer. Es lo mismo que hace `Custodia`.

    Y van en la base y no en el adaptador porque **un `check-then-insert` sin
    índice detrás no es un invariante: es una carrera**. Ya costó una vez en este
    módulo — `validar_un_vehiculo_por_conductor` se comprobaba en Python con un
    `.all()` sin bloqueo, y el 2026-08-13 un conductor acumuló tres custodias
    abiertas y tumbó `/flota/conductor/mi-turno` con `MultipleResultsFound`.

    ## La posición tiene que existir en ESE vehículo

    Dos capas, las dos en la base:

    · el `CHECK` de abajo impone `1 ≤ posicion ≤ MAX_POSICIONES_LLANTA`, que es
      lo que un `CHECK` puede saber solo;
    · el trigger `flota_montaje_posicion_de_la_ficha` compara contra
      `flota_ficha_tecnica.posiciones_llanta` **de ese vehículo**, porque eso vive
      en otra fila y ningún `CHECK` puede mirarla.

    Una llanta en la posición 6 de un furgón de 4 no es un dato raro: es un dato
    imposible, y si entra, el km por posición de ese vehículo queda hablando de
    una rueda que no existe.

    ## Regla 3 — las dos lecturas son NOT NULL cuando corresponden

    `lectura_inicio_id` es NOT NULL siempre: un montaje sin kilometraje no se
    puede restar contra nada, que es lo único para lo que existe.
    `lectura_fin_id` es NOT NULL **en cuanto hay `fin_ts`**, y el `CHECK` de
    desenlace lo impone: un desmontaje sin kilometraje deja un tramo que nunca se
    va a poder cerrar.

    Las dos las ancla `hallazgos.anclar_odometro` — la misma política, no una
    copia (regla 0 del WMS).

    ## Ninguna columna de dinero ni de conductor

    · El valor vive en `flota_gasto` (ver `Llanta`). Acá está `gasto_id`, que es
      la **referencia** al gasto bajo el cual ocurrió este montaje, y es nullable:
      una rotación no compra nada.
    · No hay columna de conductor ni de custodia. Regla 2 escrita en el esquema:
      una llanta que se gastó irregularmente es una desalineación, una presión mal
      llevada o un eje torcido, y ninguna de las tres se investiga mejor con un
      nombre al lado. Quién manejaba está en la custodia y se cruza a mano.
    """

    __tablename__ = 'flota_montaje_llanta'

    id = db.Column(db.Integer, primary_key=True)

    llanta_id = db.Column(db.Integer, db.ForeignKey('flota_llanta.id'),
                          nullable=False)
    #: Sin `index=True`: el compuesto `ix_flota_montaje_vehiculo` lo cubre por
    #: prefijo, y un índice de más se paga en cada INSERT sin devolver nada.
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)

    #: 1..N según la ficha del vehículo. **Sin default**: no hay una posición
    #: «por omisión», y una llanta cuya posición no se sabe no sirve para el
    #: único análisis que esta tabla habilita.
    posicion = db.Column(db.Integer, nullable=False)

    inicio_ts = db.Column(db.DateTime, nullable=False)
    #: `NULL` = **vigente**, que es un tercer estado (regla 4) y no «duración
    #: desconocida» ni «0 km».
    fin_ts = db.Column(db.DateTime, nullable=True)

    #: Regla 3. Las dos puntas de la resta que da los kilómetros de la llanta.
    lectura_inicio_id = db.Column(
        db.Integer, db.ForeignKey('flota_lectura_odometro.id'), nullable=False)
    lectura_fin_id = db.Column(
        db.Integer, db.ForeignKey('flota_lectura_odometro.id'), nullable=True)

    #: Por qué salió. Obligatorio al desmontar y NULL mientras esté puesta.
    #: `desgaste_irregular` es la palabra que hace visible un eje que come
    #: flancos; sin ella esa llanta y una que cumplió su vida quedan iguales en
    #: el histórico. `rotacion` existe para que una llanta que salió a cambiar de
    #: posición no hunda la vida útil medida de esa posición.
    motivo_desmontaje = db.Column(db.String(30), nullable=True)

    montada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    desmontada_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    #: El gasto bajo el cual ocurrió este montaje, si lo hubo. **Nullable y NO
    #: único**: una factura de seis llantas es UN `flota_gasto` (la clave
    #: `uq_flota_gasto_documento` es por vehículo y documento) del que cuelgan
    #: seis montajes. El valor no se reparte entre ellos — se queda entero en la
    #: espina, que es donde el CPK lo divide entre los kilómetros del vehículo.
    gasto_id = db.Column(db.Integer, db.ForeignKey('flota_gasto.id'),
                         nullable=True)

    observacion = db.Column(db.Text, nullable=True)

    llanta = db.relationship('Llanta', backref=db.backref('montajes', lazy=True))
    vehiculo = db.relationship('Vehiculo')
    # `foreign_keys` explícito: dos FK a la misma tabla dejan la relación
    # ambigua, y SQLAlchemy revienta al configurar los mappers si no se dice.
    lectura_inicio = db.relationship('LecturaOdometro',
                                     foreign_keys=[lectura_inicio_id])
    lectura_fin = db.relationship('LecturaOdometro',
                                  foreign_keys=[lectura_fin_id])
    gasto = db.relationship('Gasto')

    __table_args__ = (
        # El techo del vocabulario. La comparación contra la ficha de ESE
        # vehículo la hace el trigger, porque vive en otra fila.
        db.CheckConstraint(
            f'posicion >= 1 AND posicion <= {MAX_POSICIONES_LLANTA}',
            name='ck_flota_montaje_posicion_en_rango'),
        db.CheckConstraint('fin_ts IS NULL OR fin_ts >= inicio_ts',
                           name='ck_flota_montaje_cierre_posterior'),
        # Condicional: si hay motivo tiene que ser uno del vocabulario. La
        # ausencia se permite (montaje vigente); un valor inventado no.
        db.CheckConstraint(
            'motivo_desmontaje IS NULL OR motivo_desmontaje IN (%s)'
            % ', '.join(f"'{m}'" for m in MOTIVO_DESMONTAJE),
            name='ck_flota_montaje_motivo'),
        # ═══ EL CHECK DEL TERCER ESTADO ═══
        #
        # `fin_ts IS NULL` significa VIGENTE, y vigente tiene que verse distinto
        # de cerrado en TODAS las columnas del cierre, no solo en una. Sin las
        # dos ramas queda la fila silenciosa: un `fin_ts` puesto sin lectura de
        # cierre —o al revés, un `lectura_fin_id` sobre un montaje abierto—, y
        # cualquiera de las dos deja un tramo que el cálculo no puede cerrar
        # pero que la pantalla muestra como terminado.
        #
        # `CASE WHEN` y no aritmética sobre predicados: el 2026-08-01 un CHECK
        # escrito como `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests
        # contra SQLite y reventó el `CREATE TABLE` en el release, porque
        # PostgreSQL no suma booleanos.
        db.CheckConstraint(
            "CASE"
            "  WHEN fin_ts IS NULL THEN lectura_fin_id IS NULL"
            "                       AND desmontada_por_usuario_id IS NULL"
            "                       AND motivo_desmontaje IS NULL"
            "  ELSE lectura_fin_id IS NOT NULL"
            "       AND desmontada_por_usuario_id IS NOT NULL"
            "       AND motivo_desmontaje IS NOT NULL"
            " END",
            name='ck_flota_montaje_desenlace'),
        # ── INVARIANTE A — una posición, una llanta ──────────────────────────
        # Índice único PARCIAL: solo indexa las filas abiertas, así que dos
        # montajes vigentes de la misma posición del mismo vehículo colisionan y
        # cualquier cantidad de cerrados convive. Es la historia de esa posición,
        # y es lo que hace calculable la vida útil por posición.
        db.Index('uq_flota_montaje_posicion_vigente', 'vehiculo_id', 'posicion',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),
        # ── INVARIANTE B — una llanta, un sitio ──────────────────────────────
        # El hermano del de arriba, y **son preguntas distintas**: aquel impide
        # dos llantas en un lugar; éste impide que una llanta esté en dos lugares.
        # Sin éste, una rotación mal registrada —montar sin desmontar antes— deja
        # la misma llanta viva en dos posiciones, y sus kilómetros se cuentan dos
        # veces sin que nada falle. Es el caso que en `Custodia` no chocaba con
        # nada y dejaba el invariante roto en silencio.
        db.Index('uq_flota_montaje_llanta_vigente', 'llanta_id',
                 unique=True,
                 postgresql_where=db.text('fin_ts IS NULL'),
                 sqlite_where=db.text('fin_ts IS NULL')),
        # La consulta del expediente: los montajes de un vehículo por posición.
        db.Index('ix_flota_montaje_vehiculo', 'vehiculo_id', 'posicion'),
        # La otra consulta: la historia de UNA llanta, en orden.
        db.Index('ix_flota_montaje_historia', 'llanta_id', 'inicio_ts'),
    )


# ── El trigger que ningún CHECK puede hacer: la posición vive en la ficha ────
#
# `posiciones_llanta` está en `flota_ficha_tecnica`, o sea en otra fila y otra
# tabla. Un `CHECK` no puede mirarla, y un `if` en el adaptador no es un
# invariante: es una validación que el próximo escritor se olvida de copiar.
#
# **Sin ficha, rechaza.** No es una guarda defensiva: es el lado conservador
# (regla 0). `posiciones_llanta` es NOT NULL en la ficha, así que la única forma
# de no saber cuántas posiciones tiene un vehículo es que nadie haya levantado su
# ficha — y ahí no hay contra qué validar. Aceptar por omisión dejaría entrar la
# posición 9 de un motocarro sin que nada fallara, y el fallback por tipo de
# `dominio.valores.posiciones_llanta()` existe para decidir cuántas FOTOS pedir,
# no para autorizar un dato que después decide una vida útil.
_MSG_POSICION_INVALIDA = (
    'flota: esa posicion no existe en ese vehiculo segun su ficha tecnica '
    '(o el vehiculo no tiene ficha, y entonces no hay contra que validarla)')

_SQLITE_DDL_MONTAJE = f"""
CREATE TRIGGER flota_montaje_posicion_de_la_ficha
BEFORE INSERT ON flota_montaje_llanta
FOR EACH ROW WHEN NEW.posicion > COALESCE(
    (SELECT posiciones_llanta FROM flota_ficha_tecnica
      WHERE vehiculo_id = NEW.vehiculo_id), 0)
BEGIN SELECT RAISE(ABORT, '{_MSG_POSICION_INVALIDA}'); END;
"""

_PG_DDL_MONTAJE = f"""
CREATE OR REPLACE FUNCTION flota_montaje_posicion_de_la_ficha() RETURNS trigger AS $$
BEGIN
  IF NEW.posicion > COALESCE(
        (SELECT f.posiciones_llanta FROM flota_ficha_tecnica f
          WHERE f.vehiculo_id = NEW.vehiculo_id), 0) THEN
    RAISE EXCEPTION '{_MSG_POSICION_INVALIDA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_montaje_posicion_de_la_ficha BEFORE INSERT ON flota_montaje_llanta
FOR EACH ROW EXECUTE FUNCTION flota_montaje_posicion_de_la_ficha();
"""

# Colgado de `flota_montaje_llanta` y no de la ficha: el trigger CONSULTA la
# ficha, y en `create_all()` la ficha —que solo depende de `vehiculos`— se crea
# antes que el montaje, que depende de media docena de tablas. Al revés, el
# `CREATE TRIGGER` correría contra una tabla de montajes que todavía no existe, y
# en PostgreSQL eso es un error duro. Es el mismo razonamiento que dejó
# `_SQLITE_DDL_FICHA` colgado de la tabla de lecturas.
_colgar(MontajeLlanta.__table__, _SQLITE_DDL_MONTAJE, _PG_DDL_MONTAJE)


# ═══════════════════════════════════════════════════════════════════════════
# preventivo — el plan que la ficha ya contenía, y lo que se hizo de verdad
# ═══════════════════════════════════════════════════════════════════════════
#
# `flota_ficha_tecnica.distribucion_km_cambio` está en la base desde la tanda 1
# y **nadie lo lee**. Es la tabla diciendo a qué kilometraje toca cambiar la
# correa, mientras la correa envejece.
#
# Dos tablas y no una, por lo mismo que `flota_llanta` no es una columna de
# `flota_montaje_llanta`: el PLAN es una propiedad del vehículo («esta correa se
# cambia cada 60.000 km») y la EJECUCIÓN es un evento («el 3 de marzo, a 55.400
# km, se cambió»). Metidas en una fila, registrar el segundo cambio obligaría a
# pisar el primero, y «cuándo se hizo la vez pasada» dejaría de existir — que es
# justo el dato del que cuelga todo lo demás.

TIPO_TAREA = tuple(_TIPOS_TAREA)
ORIGEN_PLAN = ('ficha', 'manual')


class PlanTarea(db.Model):
    """Una tarea de mantenimiento preventivo de un vehículo. Sembrada, no escrita.

    **No es una lista de pendientes.** Es la traducción a filas de lo que la
    ficha técnica ya declaraba: si la ficha dice `distribucion_km_cambio =
    60000`, existe la tarea `distribucion` con ese intervalo; si dice
    `transmision_final = 'cadena'`, existe `lubricacion_cadena`. La siembra la
    hace `flota/adaptadores/preventivo.py::sembrar_desde_ficha`, y nadie
    escribe una fila de éstas a mano en el caso normal.

    ## `intervalo_km` es NULLABLE, y es la decisión que hace que la tabla valga

    La ficha declara **qué** aceite lleva el motor (`aceite_motor_spec`) y no
    **cada cuántos kilómetros** se cambia: ese número no existe en ninguna
    columna. Un `server_default` de 5.000 km le pondría a las seis fichas de
    producción un intervalo que nadie levantó, y el sistema empezaría a decir
    «vencida» sobre una comparación contra un número inventado. En una semana
    nadie lo miraría más.

    Con `NULL`, el dominio devuelve `sin_intervalo` —«no hay contra qué
    comparar»— y **jamás `al_dia`**, que sería «se revisó y está bien». Es la
    misma decisión que `capacidad_tanque_galones`, tomada por el mismo motivo, y
    tiene su propio contador en el health para que el detector no se pueda
    apagar sin que nadie lo note.

    ## `fuente` — la procedencia se hereda (regla 13 del módulo)

    Una tarea sembrada desde un `distribucion_km_cambio` cuyo
    `distribucion_fuente` es `estimado` **no vale lo mismo** que una de
    `manual_fabricante`, y quien la vea tiene que saberlo sin abrir la ficha.
    Un número con autoridad y sin procedencia es tradición oral con formato de
    columna.

    El CHECK la ata al intervalo en las dos direcciones, igual que
    `ck_flota_capacidad_tanque_con_procedencia`: sin intervalo no puede haber
    fuente —una procedencia colgada de un dato ausente afirma un levantamiento
    que no ocurrió— y con intervalo no puede faltar.

    ## `origen` — quién escribió el intervalo, no de dónde salió el dato

    `fuente` dice de dónde salió el NÚMERO (manual del fabricante, taller…).
    `origen` dice quién puso la FILA: la siembra automática (`ficha`) o una
    persona (`manual`). Son dos preguntas distintas y por eso son dos columnas:
    la re-siembra **actualiza las filas `ficha`** cuando la ficha cambia, y
    **no toca nunca las `manual`**. Sin esta columna, corregir a mano el
    intervalo del aceite se perdería en el siguiente ciclo del cron, en
    silencio.

    ## `activo` — se retira, no se borra

    Una tarea que no aplica (la ficha declaraba una caja que el vehículo no
    tiene) se desactiva. Borrarla la haría volver en la próxima siembra, y
    además dejaría huérfanas sus ejecuciones — que son historia real del
    vehículo.
    """

    __tablename__ = 'flota_plan_tarea'

    id          = db.Column(db.Integer, primary_key=True)
    vehiculo_id = db.Column(db.Integer, db.ForeignKey('vehiculos.id'),
                            nullable=False)
    tipo        = db.Column(db.String(30), nullable=False)

    #: NULL = la ficha no dice cada cuántos km. Ver el docstring: es la decisión
    #: que impide comparar un odómetro real contra un número inventado.
    intervalo_km = db.Column(db.Integer, nullable=True)

    #: Heredada del campo `*_fuente` de la ficha que sembró esta tarea.
    fuente = db.Column(db.String(30), nullable=False, server_default='sin_dato')

    origen = db.Column(db.String(10), nullable=False, server_default='ficha')
    activo = db.Column(db.Boolean, nullable=False, server_default='1')

    sembrado_ts = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    #: Lo que alguien quiera dejar dicho sobre ESTA tarea de ESTE vehículo.
    #: No reemplaza a `fuente`: una nota es prosa y no se puede contar.
    nota = db.Column(db.Text, nullable=True)

    vehiculo = db.relationship('Vehiculo')

    __table_args__ = (
        _en('tipo', TIPO_TAREA),
        _en('fuente', FUENTE),
        _en('origen', ORIGEN_PLAN),
        # Un intervalo de cero o negativo dejaría la tarea vencida para
        # siempre, sobre cualquier odómetro y todos los días. El dominio
        # también lo rechaza; acá está para que ningún INSERT lo esquive.
        db.CheckConstraint(
            'intervalo_km IS NULL OR intervalo_km > 0',
            name='ck_flota_plan_intervalo_positivo'),
        # La procedencia atada al dato en las dos direcciones. Misma forma que
        # `ck_flota_capacidad_tanque_con_procedencia`, y escrita como OR de
        # ANDs y no como suma de predicados: el 2026-08-01 un
        # `(a IS NOT NULL) + (b IS NOT NULL) = 1` pasó 25 tests contra SQLite y
        # reventó el CREATE TABLE del release, porque PostgreSQL no suma
        # booleanos.
        db.CheckConstraint(
            "(intervalo_km IS NULL AND fuente = 'sin_dato') OR "
            "(intervalo_km IS NOT NULL AND fuente <> 'sin_dato')",
            name='ck_flota_plan_intervalo_con_procedencia'),
        # Una tarea por tipo y por vehículo. Sin esto, cada corrida del cron de
        # siembra agregaría una fila más: seis vehículos por seis tipos por
        # trescientos días, y el tablero contaría diez mil tareas vencidas.
        # La idempotencia de la siembra descansa acá y no en un `if` del
        # adaptador, que es lo que dos procesos concurrentes esquivan.
        db.UniqueConstraint('vehiculo_id', 'tipo', name='uq_flota_plan_tarea'),
        db.Index('ix_flota_plan_vigentes', 'vehiculo_id', 'activo'),
    )

    def a_dominio(self, ultima_ejecucion_km=None):
        """La estructura que `flota.dominio.preventivo` sabe juzgar.

        El cálculo no vive acá: esta fila se traduce, no se pronuncia — igual
        que `Hallazgo.a_dominio` y `Custodia._a_dominio`.

        `ultima_ejecucion_km` entra por parámetro y no se lee acá adentro a
        propósito: leerlo dispararía una consulta por tarea, y el health
        recorre el plan entero de la flota. El adaptador lo trae de una sola
        pasada y lo pasa; esta función sigue sin tocar la sesión.
        """
        from flota.dominio.preventivo import Tarea

        return Tarea(tipo=self.tipo, intervalo_km=self.intervalo_km,
                     fuente=self.fuente,
                     ultima_ejecucion_km=ultima_ejecucion_km)


class EjecucionTarea(db.Model):
    """Se hizo. Cuándo, a qué kilometraje y quién lo registró.

    **Append-only por disciplina, no por trigger.** No hay verbo de edición ni
    de borrado en el adaptador: una ejecución mal registrada se corrige
    registrando la siguiente con la nota que lo diga. Poder editarla movería
    hacia adelante la línea base de la que cuelga todo el cálculo, y el número
    que ya se publicó cambiaría sin dejar rastro.

    ## `lectura_id` es NOT NULL — regla 3, impuesta por la base

    Una ejecución sin kilometraje no sirve para nada: **el kilometraje ES la
    línea base**. Sin él, «cada 60.000 km» no tiene desde dónde contar y la
    tarea vuelve a `sin_linea_base` aunque el cambio se haya hecho. Es
    exactamente la misma columna, con el mismo motivo, que `flota_hallazgo`.

    El kilómetro **no se copia acá**: se lee por la lectura. Con `km` propio,
    una corrección del odómetro dejaría los dos números diciendo cosas
    distintas y ganaría el que la pantalla mira, que es el copiado. Es la misma
    decisión que el plan toma para `km_acumulado` de una llanta.

    ## `taller` es texto libre, y es una decisión

    El plan lo dice explícito: **no se construye un maestro de talleres**. El
    taller de la esquina de Pitalito no tiene código Siesa hasta que
    contabilidad lo dé de alta, y un formulario más antes de registrar la
    ejecución es el gesto que no se hace. Texto y desplegable de los ya usados.

    ## Lo que esta tabla NO tiene, y no es un olvido

    No tiene `valor`, `costo` ni `monto`. El trinquete del plan es explícito:
    si aparece una columna de plata en una tabla `flota_*` fuera de
    `flota_gasto`, la frontera se cruzó. Lo que costó el cambio de aceite se
    registra como gasto; lo que esta tabla afirma es que **se hizo**.
    """

    __tablename__ = 'flota_ejecucion_tarea'

    id      = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey('flota_plan_tarea.id'),
                        nullable=False)

    #: Regla 3. NOT NULL: sin kilometraje no hay línea base, y sin línea base
    #: la ejecución no cambia nada de lo que el sistema puede afirmar.
    lectura_id = db.Column(db.Integer,
                           db.ForeignKey('flota_lectura_odometro.id'),
                           nullable=False)

    ejecutado_ts = db.Column(db.DateTime, nullable=False,
                             default=datetime.utcnow)
    registrado_por_usuario_id = db.Column(
        db.Integer, db.ForeignKey('usuarios.id'), nullable=False)

    taller = db.Column(db.Text, nullable=True)
    nota   = db.Column(db.Text, nullable=True)

    plan    = db.relationship('PlanTarea')
    lectura = db.relationship('LecturaOdometro')

    __table_args__ = (
        # El compuesto sirve las dos consultas que existen: «las ejecuciones de
        # esta tarea» y «la última de esta tarea». Un índice suelto sobre
        # `plan_id` sería un prefijo de éste y se pagaría en cada INSERT sin
        # devolver nada.
        db.Index('ix_flota_ejecucion_plan', 'plan_id', 'ejecutado_ts'),
    )
