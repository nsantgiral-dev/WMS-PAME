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

from sqlalchemy import DDL, event

from app.extensions import db
from flota.dominio.valores import (ANGULOS_FOTO, TIPOS_DOCUMENTO,
                                   TIPOS_SIN_VENCIMIENTO, ClaseFoto)

# ── Vocabularios permitidos ──────────────────────────────────────────────────
# Se declaran acá y se convierten en CHECK. Un enum de Python que la base no
# conoce es una convención; un CHECK es una garantía.

COMBUSTIBLE      = ('gasolina', 'diesel', 'sin_dato')
SISTEMA_FRENOS   = ('hidraulico', 'aire_sobre_hidraulico', 'aire_full', 'sin_dato')
SI_NO            = ('si', 'no', 'sin_dato')
DISTRIBUCION     = ('correa', 'cadena', 'sin_dato')
TRANSMISION_FINAL = ('cadena', 'correa', 'cardan', 'sin_dato')
FUENTE           = ('manual_fabricante', 'concesionario', 'placa_motor',
                    'taller', 'estimado', 'sin_dato')
# El vocabulario vive en el dominio: la tabla lo sigue, no al reves.
TIPO_DOCUMENTO   = TIPOS_DOCUMENTO
ESTADO_DOCUMENTO = ('vigente', 'no_encontrado')
ORIGEN_LECTURA   = ('entrega', 'preoperacional', 'cierre_dia', 'ot', 'tanqueo', 'correccion')
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

    __table_args__ = (
        _en('origen', ORIGEN_LECTURA),
        db.CheckConstraint('valor_km >= 0', name='ck_flota_valor_km'),
        # Una corrección sin motivo es indistinguible de un error de digitación.
        db.CheckConstraint(
            "origen <> 'correccion' OR "
            "(motivo_correccion IS NOT NULL AND length(trim(motivo_correccion)) > 0)",
            name='ck_flota_correccion_con_motivo',
        ),
    )


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
    WHERE vehiculo_id = NEW.vehiculo_id AND valor_km > NEW.valor_km)
BEGIN SELECT RAISE(ABORT, '{_MSG_MONOTONIA}'); END;

CREATE TRIGGER flota_odometro_no_update
BEFORE UPDATE ON flota_lectura_odometro
BEGIN SELECT RAISE(ABORT, '{_MSG_APPEND_ONLY}'); END;
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
      SELECT 1 FROM flota_lectura_odometro
      WHERE vehiculo_id = NEW.vehiculo_id AND valor_km > NEW.valor_km) THEN
    RAISE EXCEPTION '{_MSG_MONOTONIA}';
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_monotonia BEFORE INSERT ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_monotonia();

CREATE OR REPLACE FUNCTION flota_odometro_append_only() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION '{_MSG_APPEND_ONLY}'; END; $$ LANGUAGE plpgsql;

CREATE TRIGGER flota_odometro_no_update BEFORE UPDATE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only();

CREATE TRIGGER flota_odometro_no_delete BEFORE DELETE ON flota_lectura_odometro
FOR EACH ROW EXECUTE FUNCTION flota_odometro_append_only();
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


__all__ = [
    'FichaTecnica', 'DocumentoVehiculo', 'LecturaOdometro', 'Custodia', 'Foto',
    'PlantillaInspeccion', 'ItemInspeccion',
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
