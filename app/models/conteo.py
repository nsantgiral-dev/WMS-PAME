from datetime import datetime
from app.extensions import db


class EstadoConteo:
    PENDIENTE       = 'PENDIENTE'
    EN_PROCESO      = 'EN_PROCESO'
    BLOQUEADO       = 'BLOQUEADO'
    MATCH           = 'MATCH'
    DESCUADRE       = 'DESCUADRE'
    SEGUNDO_CONTEO  = 'SEGUNDO_CONTEO'
    TERCER_CONTEO   = 'TERCER_CONTEO'   # CC1 esperando CC3 porque CC1≠CC2
    AJUSTANDO       = 'AJUSTANDO'       # transición: lock liberado, Siesa en vuelo
    AJUSTADO        = 'AJUSTADO'
    CANCELADO       = 'CANCELADO'

    #: Estados de una RAÍZ cuya cadena todavía está en curso: nadie debe abrir
    #: otra sobre el mismo hueco. Antes cada sitio escribía su lista a mano
    #: —`PENDIENTE, EN_PROCESO, SEGUNDO_CONTEO`— y a las seis les faltaban
    #: `TERCER_CONTEO` y `AJUSTANDO` (2026-09-23).
    #:
    #: **`BLOQUEADO` también** (2026-09-23, P0-HUD). Un conteo bloqueado —el
    #: operario reportó «no lo encontré» u otro problema— espera que el líder
    #: lo reabra o lo cancele: la cadena sigue abierta. Sin él acá, el
    #: generador ABC abría otra cadena sobre el mismo hueco y la bloqueada
    #: quedaba huérfana para siempre (nadie la podía sacar: `/cancelar` no la
    #: aceptaba). Un hijo bloqueado ya estaba cubierto —su raíz está en
    #: SEGUNDO/TERCER_CONTEO—; la raíz bloqueada no.
    CADENA_EN_CURSO = (PENDIENTE, EN_PROCESO, SEGUNDO_CONTEO, TERCER_CONTEO, AJUSTANDO,
                       BLOQUEADO)

    #: Estados de un MIEMBRO de la cadena que todavía espera algo de un humano
    #: y que se puede cancelar (2026-09-23, «ninguna cadena queda sin salida»).
    #: Es `CADENA_EN_CURSO` sin `AJUSTANDO`: una sesión con el ajuste en vuelo a
    #: Siesa no se cancela —puede haber llegado (Regla 3)—, la resuelve la cola.
    MIEMBRO_VIVO = (PENDIENTE, EN_PROCESO, SEGUNDO_CONTEO, TERCER_CONTEO, BLOQUEADO)


class MotivoDescarteConteo:
    """Por qué un conteo en curso se descartó (`SesionConteo.conteos_descartados`).

    Lo que se descarta no se borra: queda en la lista con su motivo. Los
    motivos no pesan igual en las estadísticas — solo `MOVIMIENTO_SIESA` es un
    «recuento por venta durante el conteo»; los demás son la sesión devuelta a
    la cola (`ConteoService.devolver_al_pool`) y no dicen nada de Siesa.
    """
    #: Siesa se movió entre la foto de apertura y la del cierre: se recuenta.
    #: Las entradas anteriores a este campo no traen motivo y son todas de esta
    #: clase (era el único descarte que existía).
    MOVIMIENTO_SIESA = 'MOVIMIENTO_SIESA'
    #: El conteo cayó fuera de tolerancia y se le pidió al mismo operario un
    #: recuento propio, a ciegas (`ConteoService._pedir_recuento_propio`).
    #: Tampoco es una venta: va al denominador de la tasa, no al numerador.
    FUERA_DE_TOLERANCIA = 'FUERA_DE_TOLERANCIA'
    #: Nadie escaneó ni tecleó nada en `CONTEO_INACTIVIDAD_HORAS`: el barrido
    #: de zombis la devolvió a la cola.
    INACTIVIDAD = 'INACTIVIDAD'
    #: El líder le forzó otro conteo al mismo operario (`crear_conteo_manual`).
    CONTEO_FORZADO = 'CONTEO_FORZADO'
    #: La sesión era de otra bodega y el despachador se la quitó al operario.
    OTRA_BODEGA = 'OTRA_BODEGA'
    #: Estaba BLOQUEADA y el líder la devolvió a la cola (`reabrir_bloqueado`).
    REABIERTO = 'REABIERTO'
    #: Los que dejó `devolver_al_pool`: la sesión volvió a la cola. No son
    #: recuentos y ninguna estadística de recuento los cuenta.
    DE_LA_COLA = (INACTIVIDAD, CONTEO_FORZADO, OTRA_BODEGA, REABIERTO)
    VALIDOS = (MOVIMIENTO_SIESA, FUERA_DE_TOLERANCIA) + DE_LA_COLA


class MotivoBloqueoConteo:
    """Por qué un operario bloqueó su conteo (`SesionConteo.motivo_bloqueo`).

    **«No lo encontré» NO es un cero.** Un cero dice «revisé y no hay
    ninguna»; «no lo encontré» dice «no sé dónde está». Sin layout físico
    —todo NB1 vive en `SIESA-GENERAL`— «no lo encontré» es el caso frecuente,
    y confirmarlo como cero hacía CC1 = 0, CC2 = 0 → coinciden → **ajuste
    automático a cero en Siesa**. Por eso es un bloqueo que decide el líder
    (reabrir o cancelar) y nunca produce MATCH, segundo conteo ni ajuste.
    """
    NO_ENCONTRADO = 'NO_ENCONTRADO'
    OTRO = 'OTRO'
    #: Los que mandaba la pantalla vieja de «Reportar problema» (pensada para
    #: picking). Se aceptan para que una PWA todavía en caché no falle, y se
    #: guardan tal cual: son igual de bloqueantes.
    LEGADOS = ('UBICACION_VACIA', 'FALTANTE', 'MERCANCIA_AVERIADA', 'PRODUCTO_INCORRECTO')
    VALIDOS = (NO_ENCONTRADO, OTRO) + LEGADOS
    #: **Lo pone el sistema, no el operario** (2026-09-23): Siesa se movió
    #: mientras se contaba más veces de las que se pide recontar
    #: (`conteo_politica.MAX_RECUENTOS_POR_MOVIMIENTO`). Un producto que se
    #: vende sin parar no se cuenta insistiendo: lo decide el líder (reabrir en
    #: un momento quieto, o cancelar). No está en `VALIDOS`: no es una opción de
    #: la pantalla del operario.
    MOVIMIENTO_CONTINUO = 'MOVIMIENTO_CONTINUO'


class SesionConteo(db.Model):
    __tablename__ = 'sesiones_conteo'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(50), unique=True, nullable=False)

    # Origen de la tarea
    # DIARIO_ABC (generado automático desde Siesa) o MANUAL
    tipo = db.Column(db.String(20), default='DIARIO_ABC', nullable=False)
    clasificacion_abc = db.Column(db.String(1))  # A, B, C

    # Ubicación a contar
    ubicacion_id = db.Column(db.Integer, db.ForeignKey('ubicaciones.id'), nullable=False)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=False)

    # Producto
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    producto_codigo_siesa = db.Column(db.String(50))
    maneja_lote = db.Column(db.Boolean, default=False)

    # Operario asignado
    operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Estado
    # PENDIENTE → EN_PROCESO → MATCH → DESCUADRE → SEGUNDO_CONTEO → AJUSTADO → CANCELADO
    estado = db.Column(db.String(20), default='PENDIENTE', nullable=False)

    # Conteo — NUNCA exponer existencia_siesa al operario
    existencia_siesa = db.Column(db.Integer)  # Oculto — viene de Siesa en tiempo real
    #: **Contra qué se comparó de verdad.** `'SIESA'` | `'WMS'` | `None`.
    #:
    #: El nombre `existencia_siesa` promete procedencia y no la tenía: cuando
    #: Siesa no respondía, el servicio caía al stock del WMS **con solo un
    #: WARNING** y lo guardaba en la misma columna. El propio docstring de
    #: `comparar_conteo` lo admitía — «compara contra existencia_siesa (que
    #: ahora almacena stock WMS)».
    #:
    #: Y el ajuste que sale a Siesa es un DELTA: `fisica − existencia_siesa`.
    #: Si esa base fue el número del WMS, Siesa queda en
    #: `siesa_real + (fisica − wms)` en vez de en `fisica`. Es decir:
    #: **precisamente cuando WMS y Siesa discrepan —la única razón para
    #: contar— el ajuste empeora el descuadre.**
    #:
    #: Sin esta columna ningún invariante podía comprobarlo: el defecto no era
    #: solo indetectable, era inauditable.
    fuente_existencia = db.Column(db.String(10), nullable=True)
    #: **La foto de Siesa tomada al contar** (2026-09-23). Las cuatro juntas o
    #: ninguna — `ConteoService.consultar_foto_siesa` no devuelve media foto.
    #:
    #: `existencia_siesa` sola no es contra qué comparar en una tienda:
    #: `f400_cant_existencia_1` todavía incluye la venta POS que Siesa no ha
    #: acumulado (`f400_cant_pos_1`). La mercancía ya se fue del estante, así
    #: que un conteo honesto sale «corto» exactamente en esa cantidad; si se
    #: ajusta, la acumulación del POS la vuelve a descontar al día siguiente.
    #: Doble descuento. La base es el **teórico**: `existencia − cant_pos`.
    #:
    #: Y la foto se guarda —no se vuelve a leer al aprobar— porque el ajuste es
    #: un DELTA medido en un instante: `cantidad_fisica − teorico_siesa`. Leer
    #: la existencia en otro instante mezcla dos momentos, y cualquier
    #: movimiento entre medio se cuela en el ajuste (hasta con el signo
    #: invertido).
    #:
    #: `salida_sin_conf_siesa` va aparte porque es la que decide si el delta se
    #: puede creer: el POS pendiente vive dentro de ella (medido: 600/600 filas
    #: con POS en Siesa QA, 2026-09-23). Si hay salidas sin confirmar que NO son
    #: POS, no se sabe si esa mercancía ya salió del estante y el ajuste no se
    #: aprueba — ver `ConteoService.motivo_bloqueo_ajuste`.
    cant_pos_siesa = db.Column(db.Integer, nullable=True)
    salida_sin_conf_siesa = db.Column(db.Integer, nullable=True)
    teorico_siesa = db.Column(db.Integer, nullable=True)
    foto_siesa_at = db.Column(db.DateTime, nullable=True)
    #: **El costo promedio unitario de la foto del cierre** (`f400_costo_prom_uni`,
    #: m030). Es lo único que permite decir cuánta PLATA mueve un ajuste de
    #: conteo, y se guarda en el instante del conteo por la misma razón que la
    #: foto: el costo promedio cambia con cada entrada, y valorizar hoy un
    #: ajuste de hace tres meses con el costo de hoy es otra cifra.
    #:
    #: **No es parte de la foto obligatoria** (`ConteoService.CAMPOS_FOTO`): un
    #: conteo se decide con existencia y POS, y que Siesa no mande el costo no
    #: puede bloquearlo. Ausente o ilegible → `None`, nunca 0: un cero diría
    #: «este ajuste no vale nada», y lo que pasa es que no se sabe cuánto vale.
    #: Un costo ≤ 0 se guarda tal cual (es lo que Siesa dijo) y el reporte lo
    #: trata como «sin valorizar».
    costo_prom_uni_siesa = db.Column(db.Numeric(14, 4), nullable=True)
    #: **La foto de Siesa tomada al ABRIR la tarea** (2026-09-23, m029). La de
    #: arriba es la del cierre: se lee al confirmar. El físico se cuenta en el
    #: intervalo entre las dos, así que si Siesa se movió en el medio —un
    #: cliente paga en caja una unidad que ya estaba contada, una unidad se
    #: vende antes de que el contador llegue al estante, el POS se acumula—
    #: el físico y la foto del cierre miden instantes distintos y el conteo
    #: fabrica un sobrante o un faltante que no existe.
    #:
    #: Con las dos fotos se sabe: si existencia, POS o salida sin confirmar
    #: cambiaron entre la apertura y el cierre, el conteo está CONTAMINADO y se
    #: recuenta (`ConteoService.movimiento_durante_conteo`). Sin la de inicio
    #: —Siesa no respondió al abrir, o la sesión es anterior a la columna— no
    #: se sabe, y el conteo sirve para decidir MATCH o segundo conteo pero no
    #: para ajustar (`ConteoService.motivo_bloqueo_ajuste`). Las cuatro juntas
    #: o ninguna, igual que la del cierre.
    existencia_inicio_siesa = db.Column(db.Integer, nullable=True)
    cant_pos_inicio_siesa = db.Column(db.Integer, nullable=True)
    salida_sin_conf_inicio_siesa = db.Column(db.Integer, nullable=True)
    foto_inicio_at = db.Column(db.DateTime, nullable=True)
    #: Los conteos que se descartaron, en JSON (lista), cada uno con su
    #: `motivo` (`MotivoDescarteConteo`; sin motivo = anterior a m032ciclo, y
    #: entonces es de movimiento). Por movimiento durante el conteo, o porque
    #: la sesión volvió a la cola (inactividad, conteo forzado, otra bodega,
    #: reabierta): lo contado no se borra sin rastro.
    #: El recuento se hace sobre la MISMA sesión —no se crea otra: la
    #: cadena CC1 → CC2 → CC3 es de un hijo por padre y la cola del Conteo
    #: Definitivo cuelga de ella—, así que lo que se contó y se descartó queda
    #: acá para auditoría: físico, las dos fotos, quién y cuándo.
    conteos_descartados = db.Column(db.Text, nullable=True)
    cantidad_fisica = db.Column(db.Integer)   # Lo que contó el operario
    #: **La última vez que alguien contó algo en esta sesión** (m032ciclo): un
    #: escaneo o un total tecleado. El barrido de zombis libera por
    #: INACTIVIDAD —`coalesce(ultima_actividad_at, fecha_inicio)`— y no por
    #: antigüedad: antes miraba solo `fecha_inicio`, así que un conteo largo (o
    #: uno pospuesto por un picking en NB1) perdía lo contado a las 2 h aunque
    #: el operario siguiera escaneando.
    ultima_actividad_at = db.Column(db.DateTime, nullable=True)
    lote_id = db.Column(db.String(50))        # Obligatorio si maneja_lote=True

    # Diferencia calculada al conciliar
    diferencia = db.Column(db.Integer)
    motivo_codigo = db.Column(db.String(20))  # AJ-ENT o AJ-SAL

    # Segundo conteo (double-blind)
    es_segundo_conteo = db.Column(db.Boolean, default=False)
    sesion_origen_id = db.Column(db.Integer, db.ForeignKey('sesiones_conteo.id'), nullable=True)
    segundo_operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Origen de la excepción (si vino de picking)
    tarea_picking_id = db.Column(db.Integer, db.ForeignKey('tareas_picking.id'), nullable=True)

    # Trigger a Siesa
    siesa_triggered   = db.Column(db.Boolean, default=False)
    siesa_response    = db.Column(db.Text)
    siesa_triggered_at = db.Column(db.DateTime)
    # Quién aprobó el ajuste (admin — nunca el operario que contó)
    aprobador_id      = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    # Clave de idempotencia — ADJ-{id}-{timestamp_ms}
    idempotency_key   = db.Column(db.String(80), unique=True, nullable=True)

    # Tiempos
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_inicio = db.Column(db.DateTime)
    fecha_cierre = db.Column(db.DateTime)

    # Auditoría de edición admin
    editado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    editado_en = db.Column(db.DateTime)
    motivo_edicion = db.Column(db.Text)

    #: **Por qué está BLOQUEADO** (`MotivoBloqueoConteo`), m031hud. Antes el
    #: motivo viajaba solo dentro del texto de `motivo_edicion`
    #: (`'[UBICACION_VACIA] …'`), que además pisa cualquier edición de un
    #: admin: un dato parseado de una prosa no es un campo. `None` en un
    #: bloqueo anterior a la columna: se lista como «sin motivo registrado».
    #: **No confundir con `ConteoService.motivo_bloqueo_ajuste`**, que dice por
    #: qué un DESCUADRE no se puede ajustar: acá es por qué no se pudo contar.
    motivo_bloqueo = db.Column(db.String(30), nullable=True)
    bloqueado_en = db.Column(db.DateTime, nullable=True)

    #: **Qué dijo la tolerancia del primer conteo válido de la cadena** (m032tol):
    #: `EXACTO` | `DENTRO` | `FUERA`, evaluado con la configuración vigente en
    #: ese instante (`conteo_politica.evaluar_tolerancia`). Solo en la raíz. Es
    #: el acierto de la exactitud con tolerancia (ASCM); `None` en las cadenas
    #: anteriores a la regla, que las estadísticas declaran en vez de adivinar.
    tolerancia_primer_conteo = db.Column(db.String(10), nullable=True)
    #: **La cadena se cerró sin segundo conteo porque la diferencia estaba dentro
    #: de tolerancia** (m032tol). Solo la escribe esa rama de
    #: `ConteoService.registrar_conteo`. El invariante CNT-04 la lee para no
    #: confundir esta decisión de producto con el salto indebido del doble ciego.
    ajuste_por_tolerancia = db.Column(db.Boolean, nullable=True)

    # Relaciones
    ubicacion = db.relationship('Ubicacion', backref='sesiones_conteo', lazy=True)
    almacen = db.relationship('Almacen', backref='sesiones_conteo', lazy=True)
    producto = db.relationship('Producto', backref='sesiones_conteo', lazy=True)
    operario = db.relationship('Usuario', foreign_keys=[operario_id],
                               backref='conteos_asignados', lazy=True)
    segundo_operario = db.relationship('Usuario', foreign_keys=[segundo_operario_id],
                                       backref='segundos_conteos', lazy=True)
    aprobador = db.relationship('Usuario', foreign_keys=[aprobador_id],
                                backref='ajustes_aprobados', lazy=True)
    editor = db.relationship('Usuario', foreign_keys=[editado_por],
                             backref='conteos_editados', lazy=True)
    # Relación padre → hijo (segundo conteo generado por este).
    # uselist=False: cada sesión tiene máximo un hijo directo.
    # remote_side=[id] en el backref desambigua la dirección self-referencial.
    hijo_conteo = db.relationship(
        'SesionConteo',
        foreign_keys=[sesion_origen_id],
        backref=db.backref('sesion_padre', remote_side=[id], lazy='select'),
        uselist=False,
        lazy='select',
    )

    __table_args__ = (
        # El índice parcial que impide dos sesiones activas del mismo
        # (producto, ubicación, almacén) — creado por
        # `f4b84ad06843_add_sesion_conteo_unique_idx` para una carrera real
        # entre la API y el scheduler.
        #
        # Se declara acá con el MISMO predicado que la migración. Si aun así
        # `flask db check` lo sigue reportando, no es deriva: es que
        # autogenerate no sabe comparar predicados parciales. Eso se declara
        # en `tests/test_deriva_esquema.py` como excepción conocida, con su
        # motivo — no se silencia sin decir por qué.
        db.Index('ix_sesion_conteo_activa_unica',
                 'producto_id', 'ubicacion_id', 'almacen_id',
                 unique=True,
                 postgresql_where=db.text(
                     "estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO') "
                     "AND es_segundo_conteo = false"),
                 #: **`sqlite_where` también, o el índice se vuelve TOTAL en
                 #: los tests.** Sin él, SQLite crea un único sobre
                 #: (producto, ubicación, almacén) sin predicado: dos sesiones
                 #: CERRADAS del mismo hueco —el historial normal— colisionan,
                 #: y 13 tests se caen sobre operación legítima.
                 #: Es la misma precaución que llevan los cuatro índices de
                 #: m012; acá se me pasó y la suite lo dijo enseguida.
                 sqlite_where=db.text(
                     "estado IN ('PENDIENTE', 'EN_PROCESO', 'SEGUNDO_CONTEO') "
                     "AND es_segundo_conteo = 0")),
    )

    @classmethod
    def raiz_con_cadena_viva(cls, *, incluye_descuadre: bool):
        """Condición SQL: «esta fila es la raíz de una cadena viva del hueco».

        **Una definición para toda puerta que abre una cadena** — el generador
        ABC, el watchdog, la auditoría por excepción y el conteo manual. Se
        mira la raíz, no los hijos: un CC2 o CC3 vivo implica una raíz en
        SEGUNDO/TERCER_CONTEO, y un hijo que resolvió queda en DESCUADRE para
        siempre (contarlo como vivo trabaría el hueco sin fin).

        `incluye_descuadre` es la única diferencia entre puertas, y es a
        propósito:

        - **True — generación automática.** Una raíz en DESCUADRE espera que
          un supervisor apruebe o cancele. Abrir otra cadena encima era contar
          dos veces la misma diferencia: la nueva la ajustaba sola (CC1 == CC2)
          y después el supervisor aprobaba la vieja — **el mismo faltante
          descontado dos veces en Siesa**.
        - **False — conteo manual.** Un humano que pide recontar un DESCUADRE
          está haciendo exactamente lo que el bloqueo del ajuste le pide
          («recontar»). Que el recuento no duplique el ajuste lo garantiza
          `ConteoService.motivo_bloqueo_ajuste` (una observación más reciente
          del hueco deja vieja a la anterior), no esta condición.

        Una raíz BLOQUEADA traba las dos puertas (está en `CADENA_EN_CURSO`):
        lo que el líder hace con ella es reabrirla o cancelarla
        (`ConteoService.reabrir_bloqueado` / `cancelar_bloqueado`), no abrir
        otra cadena al lado.

        El índice único `ix_sesion_conteo_activa_unica` sigue con su predicado
        original (PENDIENTE/EN_PROCESO/SEGUNDO_CONTEO): cambiarlo exige
        migración, y la carrera que ese índice cierra —dos CC1 simultáneos
        creados por la API y el scheduler— ocurre en esos tres estados.
        """
        estados = list(EstadoConteo.CADENA_EN_CURSO)
        if incluye_descuadre:
            estados.append(EstadoConteo.DESCUADRE)
        return db.and_(cls.es_segundo_conteo.is_(False), cls.estado.in_(estados))

    def lista_conteos_descartados(self) -> list:
        """`conteos_descartados` leído. Un JSON ilegible no se oculta: se
        devuelve tal cual, marcado, para que la auditoría lo vea."""
        if not self.conteos_descartados:
            return []
        import json
        try:
            valor = json.loads(self.conteos_descartados)
        except (TypeError, ValueError):
            return [{'ilegible': self.conteos_descartados}]
        return valor if isinstance(valor, list) else [valor]

    def to_dict_operario(self):
        """Vista para el operario — SIN cantidad esperada (conteo ciego).
        No exponer es_segundo_conteo: el operario no debe saber si está verificando."""
        return {
            'id': self.id,
            'codigo': self.codigo,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'maneja_lote': self.maneja_lote,
            'estado': self.estado,
        }

    def to_dict(self):
        """Vista completa para supervisores."""
        return {
            'id': self.id,
            'codigo': self.codigo,
            'tipo': self.tipo,
            'clasificacion_abc': self.clasificacion_abc,
            'ubicacion_id': self.ubicacion_id,
            'ubicacion_codigo': self.ubicacion.codigo if self.ubicacion else None,
            'almacen_id': self.almacen_id,
            'almacen_nombre': self.almacen.nombre if self.almacen else None,
            'bodega_siesa_id': self.almacen.bodega_siesa_id if self.almacen else None,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'maneja_lote': self.maneja_lote,
            'operario_id': self.operario_id,
            'estado': self.estado,
            'existencia_siesa': self.existencia_siesa,
            'fuente_existencia': self.fuente_existencia,
            'cant_pos_siesa': self.cant_pos_siesa,
            'salida_sin_conf_siesa': self.salida_sin_conf_siesa,
            'teorico_siesa': self.teorico_siesa,
            'foto_siesa_at': self.foto_siesa_at.isoformat() if self.foto_siesa_at else None,
            'costo_prom_uni_siesa': (float(self.costo_prom_uni_siesa)
                                     if self.costo_prom_uni_siesa is not None else None),
            'existencia_inicio_siesa': self.existencia_inicio_siesa,
            'cant_pos_inicio_siesa': self.cant_pos_inicio_siesa,
            'salida_sin_conf_inicio_siesa': self.salida_sin_conf_inicio_siesa,
            'foto_inicio_at': self.foto_inicio_at.isoformat() if self.foto_inicio_at else None,
            'conteos_descartados': self.lista_conteos_descartados(),
            'cantidad_fisica': self.cantidad_fisica,
            'lote_id': self.lote_id,
            'diferencia': self.diferencia,
            'motivo_codigo': self.motivo_codigo,
            'es_segundo_conteo': self.es_segundo_conteo,
            'sesion_origen_id': self.sesion_origen_id,
            'tarea_picking_id':  self.tarea_picking_id,
            'siesa_triggered':   self.siesa_triggered,
            'siesa_triggered_at': self.siesa_triggered_at.isoformat() if self.siesa_triggered_at else None,
            'aprobador_id':      self.aprobador_id,
            'aprobador_nombre':  self.aprobador.nombre if self.aprobador else None,
            'idempotency_key':   self.idempotency_key,
            'fecha_creacion': self.fecha_creacion.isoformat(),
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'fecha_cierre': self.fecha_cierre.isoformat() if self.fecha_cierre else None,
            'editado_por': self.editado_por,
            'editado_por_nombre': self.editor.nombre if self.editor else None,
            'editado_en': self.editado_en.isoformat() if self.editado_en else None,
            'motivo_edicion': self.motivo_edicion,
            # Por qué este ajuste no se puede aprobar, dicho por la MISMA
            # función que lo niega — la pantalla no reimplementa la regla.
            # Solo en DESCUADRE: es el único estado desde el que
            # `confirmar_ajuste` encola, y la pantalla solo lo pinta ahí. En
            # los demás la respuesta no significa nada, y desde m029 cuesta
            # una consulta de traslados por fila del listado.
            'bloqueo_ajuste': (_motivo_bloqueo_ajuste(self)
                               if self.estado == EstadoConteo.DESCUADRE else None),
            # Por qué este DESCUADRE no salió solo a Siesa aunque se pueda
            # aprobar (supera el tope en pesos, o no tiene costo), y cuánto
            # vale — la MISMA función que decidió no enviarlo.
            'no_sale_solo': (_motivo_no_sale_solo(self)
                             if self.estado == EstadoConteo.DESCUADRE else None),
            'valor_ajuste': (_valor_ajuste(self)
                             if self.estado == EstadoConteo.DESCUADRE else None),
            'tolerancia_primer_conteo': self.tolerancia_primer_conteo,
            'ajuste_por_tolerancia': bool(self.ajuste_por_tolerancia),
            'motivo_bloqueo': self.motivo_bloqueo,
            # Datos del segundo conteo (hijo) embebidos para evitar N+1.
            # Si CC1≠CC2, hijo_conteo.hijo_conteo es el CC3.
            'segundo_conteo': {
                'id': self.hijo_conteo.id,
                'estado': self.hijo_conteo.estado,
                'cantidad_fisica': self.hijo_conteo.cantidad_fisica,
                # CC1 y CC2 coinciden por DIFERENCIA contra su propia foto, no
                # por cantidad física — la pantalla necesita las dos para
                # decir lo mismo que el servicio.
                'diferencia': self.hijo_conteo.diferencia,
                'teorico_siesa': self.hijo_conteo.teorico_siesa,
                'operario_id': self.hijo_conteo.operario_id,
                'operario_nombre': (
                    self.hijo_conteo.operario.nombre
                    if self.hijo_conteo.operario else None
                ),
                'fecha_cierre': (
                    self.hijo_conteo.fecha_cierre.isoformat()
                    if self.hijo_conteo.fecha_cierre else None
                ),
                'tercer_conteo': {
                    'id': self.hijo_conteo.hijo_conteo.id,
                    'estado': self.hijo_conteo.hijo_conteo.estado,
                    'cantidad_fisica': self.hijo_conteo.hijo_conteo.cantidad_fisica,
                    'operario_id': self.hijo_conteo.hijo_conteo.operario_id,
                    'operario_nombre': (
                        self.hijo_conteo.hijo_conteo.operario.nombre
                        if self.hijo_conteo.hijo_conteo.operario else None
                    ),
                } if self.hijo_conteo.hijo_conteo else None,
            } if self.hijo_conteo else None,
        }


def _motivo_bloqueo_ajuste(sesion):
    """Import diferido: la política vive en el servicio, no en el modelo."""
    from app.services.conteo_service import ConteoService
    return ConteoService.motivo_bloqueo_ajuste(sesion)


def _motivo_no_sale_solo(sesion):
    from app.services.conteo_service import ConteoService
    return ConteoService.motivo_no_sale_solo(sesion)


def _valor_ajuste(sesion):
    from app.services.conteo_service import ConteoService
    valor = ConteoService.valor_del_ajuste(sesion)
    return float(valor) if valor is not None else None


class NovedadConteo(db.Model):
    """«Encontré mercancía sin código» — lo que un operario vio mientras
    contaba y no puede escanear: unidades sin etiqueta, una caja sin
    referencia, algo que no sabe qué es.

    **No bloquea el conteo del SKU** (m031hud): el operario sigue contando lo
    que sí reconoce, y esto queda para que el líder lo identifique. Antes no
    había dónde dejarlo, y lo que el operario hacía era no contarlo o
    contarlo como el SKU que tenía en pantalla — las dos cosas terminan en un
    ajuste equivocado.

    Solo texto: el HUD de conteo no captura fotos (su cámara es el lector de
    códigos). `sesion_id` es el conteo que se estaba haciendo cuando se vio,
    no necesariamente el producto de la novedad.
    """
    __tablename__ = 'novedades_conteo'

    ABIERTA = 'ABIERTA'
    RESUELTA = 'RESUELTA'
    TIPO_SIN_CODIGO = 'MERCANCIA_SIN_CODIGO'

    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(30), nullable=False, default=TIPO_SIN_CODIGO)
    sesion_id = db.Column(db.Integer, db.ForeignKey('sesiones_conteo.id'), nullable=True)
    almacen_id = db.Column(db.Integer, db.ForeignKey('almacenes.id'), nullable=True)
    reportado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    descripcion = db.Column(db.Text, nullable=False)
    estado = db.Column(db.String(15), nullable=False, default=ABIERTA)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    resuelta_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    resuelta_en = db.Column(db.DateTime, nullable=True)
    nota_resolucion = db.Column(db.Text, nullable=True)

    sesion = db.relationship('SesionConteo', foreign_keys=[sesion_id], lazy=True)
    almacen = db.relationship('Almacen', foreign_keys=[almacen_id], lazy=True)
    reportante = db.relationship('Usuario', foreign_keys=[reportado_por], lazy=True)
    resolutor = db.relationship('Usuario', foreign_keys=[resuelta_por], lazy=True)

    def to_dict(self):
        s = self.sesion
        return {
            'id': self.id,
            'tipo': self.tipo,
            'sesion_id': self.sesion_id,
            'sesion_codigo': s.codigo if s else None,
            'producto_en_conteo': (s.producto.codigo if s and s.producto else None),
            'almacen_id': self.almacen_id,
            'almacen_nombre': self.almacen.nombre if self.almacen else None,
            'reportado_por': self.reportado_por,
            'reportado_por_nombre': self.reportante.nombre if self.reportante else None,
            'descripcion': self.descripcion,
            'estado': self.estado,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'resuelta_por_nombre': self.resolutor.nombre if self.resolutor else None,
            'resuelta_en': self.resuelta_en.isoformat() if self.resuelta_en else None,
            'nota_resolucion': self.nota_resolucion,
        }
