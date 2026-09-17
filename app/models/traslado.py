"""
Traslados entre Bodega Principal y Puntos de Venta.

Máquina de estados:
  BORRADOR → ENVIADA → EN_PICKING → EN_PACKING → PREPARADO → EN_TRANSITO → ENTREGADA
                     ↘ RECHAZADA                                          ↘ REVERTIDA
           ↘ CANCELADA  (tienda: BORRADOR/ENVIADA; admin: hasta PREPARADO)

  BORRADOR:    Tienda arma la solicitud
  ENVIADA:     Tienda envía al admin bodega
  EN_PICKING:  Admin aprueba; operario recoge ítems con TareaPicking
  EN_PACKING:  Picking confirmado + RIT 174646 disparada; operario verifica empaque
  PREPARADO:   Packing confirmado + Compromisos 174720 disparados; listo para despachar
  EN_TRANSITO: Admin despacha con 174930; mercancía en camino al PV
  ENTREGADA:   Tienda confirma recepción; ETS 173079 disparada
  REVERTIDA:   Admin revierte un traslado EN_TRANSITO; unidades devueltas al inventario origen
"""
from datetime import datetime
from app.extensions import db
from app.models.picking import TareaPicking


class EstadoTraslado:
    BORRADOR    = 'BORRADOR'
    ENVIADA     = 'ENVIADA'
    EN_PICKING  = 'EN_PICKING'
    EN_PACKING  = 'EN_PACKING'
    PREPARADO   = 'PREPARADO'
    EN_TRANSITO = 'EN_TRANSITO'
    ENTREGADA   = 'ENTREGADA'
    RECHAZADA   = 'RECHAZADA'
    CANCELADA   = 'CANCELADA'
    REVERTIDA   = 'REVERTIDA'


class ClaseTraslado:
    """**Qué clase de mercancía mueve** este traslado. No es un estado: no
    cambia nunca después de crearse.

    `NORMAL` es todo lo que existía hasta hoy — mercancía vendible que va del
    CD a un punto o entre puntos.

    `AVERIAS` es el traslado que nace en un punto de venta cuando su jefe de
    bodega encuentra mercancía dañada y la manda a NB1. Viaja por **la misma
    máquina de estados y los mismos conectores** (STS 173076 al despachar, ETS
    173079 al recibir): para Siesa es un traslado entre bodegas como cualquier
    otro, y por eso no hace falta parametrizar nada nuevo en el ERP.

    Lo que la clase decide es qué pasa **en el WMS al llegar**: un traslado
    NORMAL lo repuebla el sync desde la existencia de Siesa; uno de AVERIAS
    tiene que aterrizar en la zona de averías, que el sync no administra
    (`inventario_siesa_service.bins_administrados_por_el_sync`). Sin esta
    marca no hay forma de distinguirlos al recibir.
    """
    #: Los valores llevan el prefijo `TRASLADO_` **a propósito**, y no es
    #: cosmética: `'AVERIAS'` a secas ya significa otra cosa en esta base —es
    #: el `tipo_zona` con el que se decide si algo se puede vender
    #: (`picking_service.ZONA_AVERIAS`)—. Dos columnas distintas con el mismo
    #: literal es cómo alguien termina comparándolas, y el día que eso pase la
    #: clase de un documento va a decidir si una ubicación es vendible.
    #:
    #: El trinquete `test_politica_vendible_unica.py` lo detectó: busca el
    #: literal `'AVERIAS'` en todo `app/` porque una segunda copia de ese
    #: string suele ser una segunda definición de «¿esto se vende?». Acá era
    #: un homónimo, no una copia — pero meterlo en su lista de excepciones
    #: habría sido peor: esa lista solo encoge, sus filas dicen «falta
    #: migrar», y esta nunca migraría. Se arregla el homónimo, no el detector.
    NORMAL  = 'TRASLADO_NORMAL'
    AVERIAS = 'TRASLADO_AVERIAS'

    #: Las dos únicas válidas. El CHECK homónimo en Postgres las repite; esta
    #: tupla existe para que el código no escriba el literal suelto.
    TODAS = (NORMAL, AVERIAS)


class SolicitudTraslado(db.Model):
    __tablename__ = 'solicitudes_traslado'

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(30), unique=True, nullable=False)  # ST-20260403-001

    # Bodegas Siesa
    bodega_origen_siesa = db.Column(db.String(20), nullable=False)   # 'NB1'
    bodega_destino_siesa = db.Column(db.String(20), nullable=False)  # 'TP1'
    nombre_punto_venta = db.Column(db.String(100))

    # Estado
    estado = db.Column(db.String(30), default='BORRADOR', nullable=False)

    # DIRECTA → 173066 una sola pasada
    # EN_TRANSITO → 173076 (salida) + 173079 (entrada)
    modo_transferencia = db.Column(db.String(20), default='EN_TRANSITO')
    bodega_transito_siesa = db.Column(db.String(20))  # solo si EN_TRANSITO

    # Usuarios
    solicitante_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False)
    aprobador_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    operario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # Siesa — consecutivos de los documentos creados
    siesa_requisicion_consec = db.Column(db.Integer)  # 174646
    siesa_salida_consec = db.Column(db.Integer)       # 173076 o 173066
    siesa_entrada_consec = db.Column(db.Integer)      # 173079
    siesa_error = db.Column(db.Text)                  # último error de Siesa (para debug)
    #: ¿El 174720 registró los compromisos sobre la RIT? **Decide qué conector
    #: despacha**, y por eso no puede vivir en `siesa_error`.
    #:
    #: El 174930 no manda cantidades: Siesa las toma de lo comprometido en la
    #: RIT. Si el 174720 no entró, ahí siguen las **originales del 174646**, y
    #: el STS sale por lo pedido en vez de por lo empacado. El 173076 sí lleva
    #: `cantidad_enviada`, que es lo real.
    #:
    #: **Se enciende DESPUÉS del POST, no antes.** Es al revés de la Regla 6 a
    #: propósito: acá la bandera no evita un duplicado, **abre una compuerta**.
    #: Un pre-flag dejaría la puerta abierta ante un crash entre el POST y el
    #: commit, y despacharíamos sobre una suposición. Ante la duda, el lado
    #: barato es reenviar el 174720 (reafirma las mismas cantidades sobre la
    #: misma RIT); el caro es mandar a Siesa un STS por cantidades que nadie
    #: empacó.
    siesa_compromisos_ok = db.Column(db.Boolean, default=False,
                                     server_default='false', nullable=False)
    inventario_descontado = db.Column(db.Boolean, default=False, server_default='false')

    # ── Averías ────────────────────────────────────────────────────────────
    #: Ver `ClaseTraslado`. `server_default` cubre las 174 solicitudes que ya
    #: existen: todas son NORMAL, porque hasta hoy no había otra cosa.
    clase_traslado = db.Column(db.String(20), nullable=False,
                               default=ClaseTraslado.NORMAL,
                               server_default=ClaseTraslado.NORMAL)

    #: Lo que el administrador del punto escribió al aprobar la avería: qué
    #: revisó, qué encontró, cómo lo constató. Es el segundo de los cuatro
    #: momentos de validación del proceso (el primero es crear la solicitud,
    #: que ya queda firmado por `solicitante_id` + `fecha_creacion`).
    #:
    #: Vive acá y no en `observaciones` porque `observaciones` es un campo
    #: libre que cualquiera pisa en cualquier estado; esto es la firma de un
    #: acto concreto y tiene que poder distinguirse de una nota al paso.
    averia_evidencia = db.Column(db.Text)

    #: **Tri-estado a propósito** — `None` no es «no».
    #:
    #:   · `None`  → nadie de NB1 miró todavía. La mercancía llegó y está en
    #:               el limbo: contada en la recepción, sin ubicar.
    #:   · `True`  → sí estaba averiada. Se ubica en la zona de averías y sale
    #:               el documento a AV1.
    #:   · `False` → no estaba averiada. Vuelve al inventario vendible de NB1
    #:               y NO sale ningún documento de avería.
    #:
    #: Los tres desenlaces son distintos y tienen consecuencias operativas
    #: distintas, así que un booleano de dos valores no alcanza — es el mismo
    #: argumento de `RecaudoEntrega.retencion_confirmada`
    #: (`app/models/recaudo_entrega.py:99`), que ya pagó esta lección.
    averia_veredicto = db.Column(db.Boolean, nullable=True)
    averia_veredicto_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'),
                                     nullable=True)
    averia_veredicto_at = db.Column(db.DateTime, nullable=True)
    averia_veredicto_nota = db.Column(db.String(200), nullable=True)

    # Timestamps
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_envio = db.Column(db.DateTime)
    fecha_aprobacion = db.Column(db.DateTime)
    fecha_despacho = db.Column(db.DateTime)
    fecha_entrega = db.Column(db.DateTime)

    observaciones = db.Column(db.Text)
    motivo_rechazo = db.Column(db.String(200))

    # Relationships
    solicitante = db.relationship('Usuario', foreign_keys=[solicitante_id],
                                  backref='solicitudes_traslado_creadas')
    aprobador = db.relationship('Usuario', foreign_keys=[aprobador_id],
                                backref='solicitudes_traslado_aprobadas')
    operario = db.relationship('Usuario', foreign_keys=[operario_id],
                               backref='solicitudes_traslado_asignadas')
    items = db.relationship('ItemSolicitudTraslado', backref='solicitud',
                            lazy=True, cascade='all, delete-orphan')
    averia_veredicto_usuario = db.relationship(
        'Usuario', foreign_keys=[averia_veredicto_por],
        backref='traslados_averia_dictaminados')

    __table_args__ = (
        # Las dos únicas clases que el código sabe leer. Un valor desconocido
        # no daría error: se comportaría como NORMAL en todos los `if`, y un
        # traslado de averías se ubicaría como mercancía vendible.
        db.CheckConstraint(
            "clase_traslado IN ('TRASLADO_NORMAL', 'TRASLADO_AVERIAS')",
            name='ck_traslado_clase_conocida'),

        # Un traslado NORMAL no puede cargar veredicto de avería.
        #
        # No es cosmética. Los cinco campos se leen para decidir **si la
        # mercancía se ubica en la zona de averías**; si alguien los escribe
        # sobre un traslado normal —un `PATCH` mal apuntado, un copiado de
        # solicitud, una migración futura— el traslado normal empieza a
        # comportarse como uno de averías y el stock vendible desaparece del
        # pool sin que nadie lo haya pedido.
        #
        # La dirección contraria (un AVERIAS sin veredicto) sí es válida y es
        # el estado inicial: `None` significa «nadie miró todavía».
        db.CheckConstraint(
            "clase_traslado = 'TRASLADO_AVERIAS' OR ("
            " averia_veredicto IS NULL AND"
            " averia_evidencia IS NULL AND"
            " averia_veredicto_por IS NULL AND"
            " averia_veredicto_at IS NULL AND"
            " averia_veredicto_nota IS NULL)",
            name='ck_traslado_veredicto_solo_en_averias'),
    )

    def es_averia(self) -> bool:
        """¿Este traslado mueve mercancía averiada?

        **Única implementación de la pregunta.** El repo ya pagó la factura de
        tener el mismo criterio escrito en dos sitios
        (`memoria: una-politica-una-funcion`): acá el criterio es uno y los
        llamadores lo consultan, no lo copian. Si mañana la clase se decide
        por otra cosa, cambia este método y lo heredan todos.
        """
        return self.clase_traslado == ClaseTraslado.AVERIAS

    def to_dict(self):
        # El error de Siesa es relevante (requiere acción) solo cuando no hay
        # consecutivo de cierre — significa que el movimiento nunca llegó a Siesa.
        consec_cierre = (self.siesa_entrada_consec if self.modo_transferencia == 'EN_TRANSITO'
                         else self.siesa_salida_consec)

        # ENTREGADA **no** silencia el error, y esa era la trampa (2026-08-14):
        # `confirmar_recepcion` pone ENTREGADA aunque el 173079 haya fallado
        # —el estado describe el hecho físico, la mercancía llegó—, así que un
        # traslado con la entrada nunca registrada en Siesa se pintaba en verde
        # y sin aviso. Es exactamente el limbo que los invariantes de traslado
        # existen para detectar: el stock no falta ni sobra, está en la bodega
        # puente, y nadie reclama.
        #
        # Las otras tres sí callan con razón: un traslado RECHAZADO, CANCELADO
        # o REVERTIDO no debe tener documento de cierre en Siesa, así que un
        # error viejo ahí no pide ninguna acción.
        #
        # El `not consec_cierre` sigue siendo la guarda que evita el ruido: si
        # el consecutivo existe, el movimiento llegó y no se avisa nada.
        estados_sin_cierre_esperado = (
            EstadoTraslado.RECHAZADA, EstadoTraslado.CANCELADA,
            EstadoTraslado.REVERTIDA,
        )
        siesa_necesita_atencion = (
            bool(self.siesa_error) and
            not consec_cierre and
            self.estado not in estados_sin_cierre_esperado
        )
        return {
            'id': self.id,
            'codigo': self.codigo,
            'bodega_origen_siesa': self.bodega_origen_siesa,
            'bodega_destino_siesa': self.bodega_destino_siesa,
            'nombre_punto_venta': self.nombre_punto_venta,
            'estado': self.estado,
            'modo_transferencia': self.modo_transferencia,
            'bodega_transito_siesa': self.bodega_transito_siesa,
            'solicitante_id': self.solicitante_id,
            'solicitante_nombre': self.solicitante.nombre if self.solicitante else None,
            'aprobador_id': self.aprobador_id,
            'aprobador_nombre': self.aprobador.nombre if self.aprobador else None,
            'operario_id': self.operario_id,
            'operario_nombre': self.operario.nombre if self.operario else None,
            'siesa_requisicion_consec': self.siesa_requisicion_consec,
            'siesa_salida_consec': self.siesa_salida_consec,
            'siesa_entrada_consec': self.siesa_entrada_consec,
            'siesa_error': self.siesa_error if siesa_necesita_atencion else None,
            'siesa_necesita_atencion': siesa_necesita_atencion,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_envio': self.fecha_envio.isoformat() if self.fecha_envio else None,
            'fecha_aprobacion': self.fecha_aprobacion.isoformat() if self.fecha_aprobacion else None,
            'fecha_despacho': self.fecha_despacho.isoformat() if self.fecha_despacho else None,
            'fecha_entrega': self.fecha_entrega.isoformat() if self.fecha_entrega else None,
            'observaciones': self.observaciones,
            'motivo_rechazo': self.motivo_rechazo,
            'clase_traslado': self.clase_traslado,
            'es_averia': self.es_averia(),
            'averia_evidencia': self.averia_evidencia,
            'averia_veredicto': self.averia_veredicto,
            'averia_veredicto_por': self.averia_veredicto_por,
            'averia_veredicto_nombre': (self.averia_veredicto_usuario.nombre
                                        if self.averia_veredicto_usuario else None),
            'averia_veredicto_at': (self.averia_veredicto_at.isoformat()
                                    if self.averia_veredicto_at else None),
            'averia_veredicto_nota': self.averia_veredicto_nota,
            'items': [i.to_dict() for i in self.items],
            'total_items': len(self.items),
            'picking_progreso': self._picking_progreso(),
            'packing_info': self._packing_info(),
        }

    def _picking_progreso(self):
        """Progreso de TareasPicking — solo relevante en EN_PICKING/PREPARADO.
        [M9] Single query with conditional count instead of 2 separate COUNT queries.
        """
        if self.estado not in (EstadoTraslado.EN_PICKING, EstadoTraslado.PREPARADO):
            return None
        from sqlalchemy import func as _func, case as _case
        from app.extensions import db as _db
        row = _db.session.query(
            _func.count().label('total'),
            _func.count(_case((TareaPicking.estado == 'COMPLETADO', 1))).label('completadas'),
        ).select_from(TareaPicking).filter(
            TareaPicking.referencia_documento == self.codigo,
            TareaPicking.tipo_documento == 'TRASLADO',
        ).first()
        total, completadas = row.total, row.completadas
        if total == 0:
            return {'total': 0, 'completadas': 0, 'sin_tareas': True}
        return {
            'total': total,
            'completadas': completadas,
            'sin_tareas': False,
            'porcentaje': round(completadas / total * 100),
        }

    def _packing_info(self):
        """TareaPacking activa — relevante en EN_PACKING y PREPARADO (despacho pendiente)."""
        if self.estado not in (EstadoTraslado.EN_PACKING, EstadoTraslado.PREPARADO):
            return None
        tareas = self.tareas_packing
        if not tareas:
            return None
        t = tareas[0]
        return {
            'id': t.id,
            'codigo': t.codigo,
            'estado': t.estado,
            'empacador': t.empacador.nombre if t.empacador else None,
        }


class ItemSolicitudTraslado(db.Model):
    __tablename__ = 'items_solicitud_traslado'

    id = db.Column(db.Integer, primary_key=True)
    solicitud_id = db.Column(db.Integer, db.ForeignKey('solicitudes_traslado.id'), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey('productos.id'), nullable=False)
    producto_codigo_siesa = db.Column(db.String(50))

    cantidad_solicitada = db.Column(db.Integer, nullable=False)
    cantidad_aprobada = db.Column(db.Integer)    # admin ajusta al aprobar
    cantidad_enviada = db.Column(db.Integer, default=0)   # picking confirmado
    cantidad_recibida = db.Column(db.Integer, default=0)  # recepción en tienda

    disponible_siesa = db.Column(db.Integer)  # snapshot en bodega origen al crear

    #: Por qué está averiada ESTA línea. Solo aplica cuando la solicitud es de
    #: `ClaseTraslado.AVERIAS`.
    #:
    #: **No hay `cantidad_averiada` acá, y es deliberado.** En un traslado de
    #: averías la mercancía averiada es *todo* el documento: la cantidad ya la
    #: dice `cantidad_solicitada`, y la cadena
    #: `solicitada ≥ aprobada ≥ enviada ≥ recibida` la sigue recortando paso a
    #: paso como en cualquier traslado. Una segunda columna con el mismo
    #: número sería una segunda fuente de verdad que puede divergir de la
    #: primera — exactamente el defecto que `ck_traslado_cadena_no_crece`
    #: existe para impedir.
    #:
    #: (En `ItemRecepcion` sí hay `cantidad_averiada`, y ahí es correcto: en
    #: una recepción por OC la avería es un *subconjunto* de lo recibido, no
    #: el documento entero.)
    motivo_averia = db.Column(db.String(200))

    producto = db.relationship('Producto', backref='items_traslado', lazy=True)

    __table_args__ = (
        # TRA-01 en disco: `solicitada ≥ aprobada ≥ enviada ≥ recibida`.
        #
        # Cada paso puede recortar; ninguno puede inventar. Una desigualdad al
        # revés es mercancía que apareció de la nada entre dos etapas — y
        # `cantidad_recibida` alimenta el payload del ETS 173079, así que una
        # tienda que se equivoca al contar mete en la bodega destino más
        # unidades de las que salieron del origen.
        #
        # El invariante estaba declarado como `BLOQUEA` en
        # `app/services/auditoria/traslados.py`, pero **es detective**: solo
        # aparece si alguien abre el panel de auditoría. Las cuatro columnas
        # viven en la misma fila, así que el CHECK es trivial y es preventivo.
        #
        # Los NULL se dejan pasar: `cantidad_aprobada` es NULL hasta que el
        # admin aprueba, y un CHECK que los rechazara impediría crear la
        # solicitud. Cada comparación solo aplica cuando sus dos lados existen.
        db.CheckConstraint(
            '(cantidad_aprobada IS NULL OR cantidad_aprobada <= cantidad_solicitada) AND '
            '(cantidad_enviada IS NULL OR cantidad_aprobada IS NULL OR '
            ' cantidad_enviada <= cantidad_aprobada) AND '
            '(cantidad_recibida IS NULL OR cantidad_enviada IS NULL OR '
            ' cantidad_recibida <= cantidad_enviada)',
            name='ck_traslado_cadena_no_crece'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'solicitud_id': self.solicitud_id,
            'producto_id': self.producto_id,
            'producto_codigo': self.producto.codigo if self.producto else None,
            'producto_nombre': self.producto.nombre if self.producto else None,
            'producto_codigo_siesa': self.producto_codigo_siesa,
            'cantidad_solicitada': self.cantidad_solicitada,
            'cantidad_aprobada': self.cantidad_aprobada,
            'cantidad_enviada': self.cantidad_enviada,
            'cantidad_recibida': self.cantidad_recibida,
            'disponible_siesa': self.disponible_siesa,
            'motivo_averia': self.motivo_averia,
        }
