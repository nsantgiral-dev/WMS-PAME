"""
Retención de cartera (m044cartera). Ver `app/services/cartera_service.py`.

Cuatro tablas, las cuatro OPERATIVAS en el acta de corte (el ensayo no deja
retenciones ni habilitaciones que valgan después del corte) y sin FK hacia
otras tablas operativas: apuntan por id suelto + código legible, igual que la
bitácora, para que el orden del corte no dependa de ellas.

· `retenciones_cartera`    un pedido de crédito real que la compuerta frenó, y
                           lo que pasó después (liberado, autorizado,
                           convertido a contado, cancelado).
· `cartera_cliente`        la última foto de la cartera de un NIT (maestro de
                           clientes + CxC abierta) que la compuerta leyó de
                           Siesa. Es el respaldo cuando Siesa no contesta:
                           vale 24 h, después es «sin dato».
· `cartera_habilitaciones` lo que el Gestor de Cartera empuja por cliente:
                           canal (INSTITUCIONAL…), acuerdo de pago vigente y
                           excepciones vigentes. Dato, con su `as_of`.
· `cartera_idempotencia`   la `Idempotency-Key` de cada POST del Gestor.
"""
from datetime import datetime

from app.extensions import db


class EstadoRetencion:
    RETENIDO = 'RETENIDO'
    LIBERADO_PAGO = 'LIBERADO_PAGO'            # la re-evaluación ya no la retiene
    AUTORIZADO = 'AUTORIZADO'                  # un usuario de cartera la dejó salir a crédito
    CONVERTIDO_CONTADO = 'CONVERTIDO_CONTADO'  # sale en contado contraentrega (C02)
    CANCELADO = 'CANCELADO'                    # el pedido o su packing dejaron de existir
    TODOS = (RETENIDO, LIBERADO_PAGO, AUTORIZADO, CONVERTIDO_CONTADO, CANCELADO)
    #: Estados en los que la retención ya no frena nada.
    RESUELTOS = (LIBERADO_PAGO, AUTORIZADO, CONVERTIDO_CONTADO, CANCELADO)


class Compuerta:
    INICIO = 'G1'      # al iniciar el despacho, antes de crear el picking
    CIERRE = 'G2'      # al cerrar el packing, antes del 244328 (último punto reversible)
    EMISION = 'EMISION'  # dentro de la emisión (DLQ o carril admin) sin decisión previa
    TODAS = (INICIO, CIERRE, EMISION)
    #: Cómo se dice en pantalla dónde se retuvo (el código queda en la API).
    PALABRAS = {INICIO: 'retenido al aprobar el pedido',
                CIERRE: 'retenido al cerrar la caja',
                EMISION: 'retenido al facturar'}


class RetencionCartera(db.Model):
    __tablename__ = 'retenciones_cartera'

    id = db.Column(db.Integer, primary_key=True)
    creada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    #: Cualquier cambio (re-evaluación incluida) la mueve: es el cursor
    #: `cambiados_desde` del Gestor.
    actualizada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow,
                               index=True)

    pedido_clave = db.Column(db.String(40), nullable=False, index=True)
    numero_pedido = db.Column(db.String(50))
    nit = db.Column(db.String(40), nullable=False, index=True)
    sucursal = db.Column(db.String(10))
    cliente = db.Column(db.String(200))
    vendedor_id = db.Column(db.String(40))
    #: Sin FK a propósito (ver el encabezado del módulo).
    tarea_packing_id = db.Column(db.Integer, index=True)
    almacen_id = db.Column(db.Integer)
    compuerta = db.Column(db.String(10), nullable=False)

    #: La condición del pedido (`f430_id_cond_pago`). No se llama `cond_pago`
    #: a propósito: el trinquete de contado vigila ese nombre como decisión de cobro.
    condicion_pago = db.Column(db.String(10))
    dias_credito = db.Column(db.Integer)
    valor = db.Column(db.Numeric(16, 2))
    #: `[{codigo, texto, ...cifras}]` — códigos: MORA, CUPO_EXCEDIDO, SIN_CUPO,
    #: SIN_DATO, ACUERDO_VIGENTE, VALOR_DESCONOCIDO. MAESTRO_DUPLICADO se
    #: declara pero nunca retiene por sí solo.
    motivos = db.Column(db.JSON)
    #: La salida completa de `cartera_service.evaluar` que produjo la
    #: retención (o la última re-evaluación).
    evaluacion = db.Column(db.JSON)
    as_of = db.Column(db.DateTime)
    origen_dato = db.Column(db.String(12))

    estado = db.Column(db.String(20), nullable=False, default=EstadoRetencion.RETENIDO)

    #: Quién inició el pedido en el WMS (el que tocó «Aprobar» o cerró la caja)
    #: y quién lo creó en Siesa. Ninguno de los dos puede autorizarlo.
    iniciado_por_id = db.Column(db.Integer)
    iniciado_por = db.Column(db.String(160))
    siesa_usuario_creacion = db.Column(db.String(80))
    #: `f430_ind_retenido_*`, `f430_usuario_retenido`, `f430_usuario_aprobacion*`.
    contexto_siesa = db.Column(db.JSON)

    resuelta_en = db.Column(db.DateTime)
    #: Texto: el usuario del Gestor no existe en `usuarios`. `resuelta_por_id`
    #: solo cuando la resolvió alguien del WMS.
    resuelta_por = db.Column(db.String(160))
    resuelta_por_id = db.Column(db.Integer)
    resuelta_origen = db.Column(db.String(20))   # WMS | GESTOR | BARRIDO | SISTEMA
    motivo_resolucion = db.Column(db.Text)
    codigo_excepcion = db.Column(db.String(10))
    tope_valor = db.Column(db.Numeric(16, 2))
    vence_en = db.Column(db.Date)

    reevaluada_en = db.Column(db.DateTime)
    reevaluaciones = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('RETENIDO','LIBERADO_PAGO','AUTORIZADO',"
            "'CONVERTIDO_CONTADO','CANCELADO')", name='ck_retencion_cartera_estado'),
        db.CheckConstraint("compuerta IN ('G1','G2','EMISION')",
                           name='ck_retencion_cartera_compuerta'),
        # Una sola retención viva por pedido: dos requests simultáneos del
        # mismo pedido no pueden dejar dos filas RETENIDO.
        db.Index('uq_retencion_cartera_viva', 'pedido_clave', unique=True,
                 postgresql_where=db.text("estado = 'RETENIDO'"),
                 sqlite_where=db.text("estado = 'RETENIDO'")),
    )


class CarteraCliente(db.Model):
    """La última lectura de Siesa por NIT. Ver `cartera_service.cartera_de`."""
    __tablename__ = 'cartera_cliente'

    id = db.Column(db.Integer, primary_key=True)
    nit = db.Column(db.String(40), nullable=False, unique=True)
    #: UTC de la última lectura COMPLETA. `None` = nunca se leyó entera.
    as_of = db.Column(db.DateTime)
    clientes = db.Column(db.JSON)
    filas = db.Column(db.JSON)
    ultimo_intento_en = db.Column(db.DateTime)
    ultimo_error = db.Column(db.Text)


class CarteraHabilitacion(db.Model):
    """Lo que el Gestor de Cartera sabe de un cliente y el WMS no."""
    __tablename__ = 'cartera_habilitaciones'

    id = db.Column(db.Integer, primary_key=True)
    nit = db.Column(db.String(40), nullable=False)
    #: `''` = todas las sucursales del NIT (no NULL: la unicidad lo necesita).
    sucursal = db.Column(db.String(10), nullable=False, default='')
    canal = db.Column(db.String(20))
    acuerdo_vigente = db.Column(db.Boolean)
    acuerdo_vence = db.Column(db.Date)
    #: `[{codigo, tope, vence}]`
    excepciones = db.Column(db.JSON)
    #: Cuándo el Gestor dice que es cierto esto (su reloj) y cuándo llegó.
    as_of = db.Column(db.DateTime, nullable=False)
    recibido_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    enviado_por = db.Column(db.String(160))

    __table_args__ = (
        db.UniqueConstraint('nit', 'sucursal', name='uq_cartera_habilitacion_nit_suc'),
    )


class CarteraIdempotencia(db.Model):
    """`Idempotency-Key` de los POST del Gestor: la misma clave devuelve la
    misma respuesta, y no vuelve a ejecutar nada."""
    __tablename__ = 'cartera_idempotencia'

    id = db.Column(db.Integer, primary_key=True)
    clave = db.Column(db.String(120), nullable=False, unique=True)
    endpoint = db.Column(db.String(120), nullable=False)
    retencion_id = db.Column(db.Integer)
    status = db.Column(db.Integer, nullable=False)
    respuesta = db.Column(db.JSON)
    creada_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
