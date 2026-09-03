"""
Dónde queda el cliente — el conductor como geocodificador.

El sistema **no sabe dónde queda ningún cliente**, y no es que las direcciones
estén sucias: no hay direcciones. `pedidos_sync_service.py:124` lee
`f015_id_depto_pe` y `f015_id_ciudad_pe` —códigos de departamento y ciudad, no
calle— y lo que persiste es el nombre del municipio. Una «parada» es un
municipio. Verificado por grep el 2026-09-02: `direccion` no aparece en ningún
modelo del WMS salvo `almacenes.direccion`, que es de nosotros, no del cliente.

Y geocodificar por dirección tampoco es la salida: en Neiva hay **72** números
de casa mapeados en todo el municipio, 10 en Pitalito, ~31 en Florencia
(medido contra Overpass, `WMS_plan_flota.md` §3). Con esos números el
conductor que ya estuvo parado en la puerta es estrictamente mejor que
cualquier API — y Nominatim además **prohíbe explícitamente** este caso de uso
(*«package/vehicle tracking applications must run their own service»*).

## Por qué son DOS tablas y no una

Son dos hechos distintos, y colapsarlos pierde uno de los dos:

| | Qué afirma | Cuándo cambia |
|---|---|---|
| `entregas_geo` | **dónde estuvo el camión** el día que entregó esa factura | nunca — es un evento pasado |
| `clientes_geo` | **dónde queda la tienda** | cada vez que llega una captura nueva que mueva la elección |

El plan quiere lo segundo para rutear, pero lo segundo **se construye a partir
de los primeros**: con una sola tabla, la segunda captura tendría que pisar a
la primera y no habría contra qué comparar — ni mediana, ni dispersión, ni
forma de notar que dos tiendas distintas comparten razón social.

Y hay una consecuencia de corte que sola justifica la separación: el evento es
registro del ensayo y se vacía en el acta de corte; **el maestro es el activo
que tarda tres meses en construirse y NO se vacía**. Una sola tabla obliga a
elegir, y cualquiera de las dos elecciones está mal. Ver `OPERATIVAS` y
`PROTEGIDAS_MAESTRAS` en `scripts/reset_transaccional.py`.

## Regla 4 del módulo de flota — la ausencia se modela con palabras

Un GPS negado, apagado o sin señal **no es la coordenada 0,0**. 0,0 es un punto
real en el Golfo de Guinea, a 5.000 km de Neiva, y es el valor al que caen
todos los `float` sin inicializar del mundo. Acá «no sé» es `fuente='sin_dato'`
con `motivo_sin_dato` escrito, y `lat`/`lon` en NULL — nunca un número.

El CHECK `ck_entrega_geo_coordenada_o_motivo` lo hace estructural: no se puede
guardar una coordenada sin procedencia ni un «no sé» sin motivo.

## Regla 13 — procedencia pegada al dato

Mismo patrón que `distribucion_fuente` y `frenos_fuente` en
`flota_ficha_tecnica`, y por el mismo motivo: este número va a decidir a dónde
manda el sistema a un conductor. Un dato con autoridad y sin procedencia es
tradición oral con formato de columna.
"""
from datetime import datetime

from app.extensions import db
from app.services import geo_cliente as _geo

#: Caja de Colombia continental + insular, generosa. Un CHECK de rango no es
#: cosmética acá: atrapa las **dos** formas en que una coordenada llega
#: equivocada y ninguna de las dos falla sola.
#:
#: · `0,0` — el default de todo `float` no inicializado. Cae en el Golfo de
#:   Guinea, que es un punto perfectamente válido para cualquier validación de
#:   rango global (-90..90 / -180..180).
#: · **ejes cambiados** — `lat=-75.28, lon=2.93` en vez de al revés. También
#:   pasa las validaciones globales, y pone la tienda en el Pacífico Sur.
#:
#: Se acota a Colombia y no al Huila porque PAME despacha a Caquetá y el techo
#: de crecimiento del negocio no es una decisión de esta tabla. Si algún día
#: entrega fuera del país, esto se cambia a mano y con motivo escrito — que es
#: exactamente la fricción que se quiere.
LAT_MIN, LAT_MAX = -4.5, 13.7
LON_MIN, LON_MAX = -82.2, -66.5


class EntregaGeo(db.Model):
    """Dónde estuvo el camión cuando se confirmó **esta** parada.

    QUÉ AFIRMA: que en el instante `capturado_en`, el dispositivo del conductor
    reportó esa posición con esa precisión (o que se le preguntó y no se pudo
    saber, y por qué).

    QUÉ NO AFIRMA: que ahí quede la tienda. Un conductor puede confirmar la
    parada desde el camión a media cuadra, o —re-confirmando un dedazo— desde
    la bodega al día siguiente. Traducir de «acá estuvo el camión» a «acá queda
    la tienda» es trabajo de `geo_cliente.elegir_coordenada_del_maestro`, sobre
    varias filas de éstas.

    **Una fila por recaudo, y la coordenada no se pisa.** Re-confirmar una
    parada es una función deseada (`confirmar_parada` lo admite a propósito),
    pero lo que se corrige ahí son montos, fotos y motivos — no geografía. Si
    la segunda confirmación pisara a la primera, una corrección hecha en la
    oficina al día siguiente escribiría la coordenada de la bodega sobre la de
    la tienda, y el maestro se movería solo hacia el CD. Por eso
    `geo_cliente.registrar_captura` solo escribe sobre una fila que todavía no
    tiene coordenada.
    """

    __tablename__ = 'entregas_geo'

    id = db.Column(db.Integer, primary_key=True)

    #: El evento del que cuelga. `UNIQUE`, no solo FK: dos capturas para la
    #: misma parada serían dos respuestas a una sola pregunta.
    recaudo_id = db.Column(db.Integer, db.ForeignKey('recaudos_entrega.id'),
                           nullable=False, unique=True)

    #: A qué cliente le suma esta captura, **congelado al momento de capturar**.
    #: No es una FK a `clientes_geo` a propósito: el maestro puede no existir
    #: todavía (esta captura puede ser la primera), y una FK obligaría a crear
    #: la fila del maestro antes de tener con qué llenarla — o sea, a inventar
    #: un maestro vacío que después habría que distinguir de uno real.
    cliente_clave = db.Column(db.String(220), nullable=False, index=True)

    #: `NULL` cuando no se supo. **Jamás 0,0** — ver el encabezado del módulo.
    lat = db.Column(db.Numeric(9, 6), nullable=True)
    lon = db.Column(db.Numeric(9, 6), nullable=True)

    #: Lo que el navegador declaró como radio de incertidumbre, en metros
    #: (`GeolocationCoordinates.accuracy`). `NULL` = el dispositivo no lo
    #: reportó, que **no es lo mismo que precisión perfecta**: una captura sin
    #: precisión conocida se guarda igual —es información— pero no participa de
    #: la elección del maestro. Regla 0: no saber qué tan bueno es un punto no
    #: autoriza a usarlo para mandar un camión.
    precision_m = db.Column(db.Numeric(8, 1), nullable=True)

    #: `gps_conductor` | `corregida_a_mano` | `sin_dato`.
    fuente = db.Column(db.String(20), nullable=False,
                       server_default=_geo.SIN_DATO)

    #: Por qué no hay coordenada. Catálogo en `geo_cliente.MOTIVOS_SIN_DATO`.
    motivo_sin_dato = db.Column(db.String(24), nullable=True)

    capturado_en = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "fuente IN ('gps_conductor','corregida_a_mano','sin_dato')",
            name='ck_entrega_geo_fuente'),
        db.CheckConstraint(
            "motivo_sin_dato IS NULL OR motivo_sin_dato IN ("
            "'permiso_denegado','sin_senal','no_soportado','timeout',"
            "'no_se_pidio','fuera_de_rango','no_declarado')",
            name='ck_entrega_geo_motivo'),
        # El invariante que da sentido a la Regla 4 acá: o hay coordenada CON
        # procedencia, o hay «no sé» CON motivo. Nunca las mezclas: una
        # coordenada `sin_dato` sería un número sin autoridad, y un `sin_dato`
        # sin motivo sería la misma ausencia muda que este modelo vino a
        # reemplazar.
        db.CheckConstraint(
            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato' "
            "     AND motivo_sin_dato IS NULL) "
            "OR (lat IS NULL AND lon IS NULL AND fuente = 'sin_dato' "
            "     AND motivo_sin_dato IS NOT NULL)",
            name='ck_entrega_geo_coordenada_o_motivo'),
        db.CheckConstraint(
            f"lat IS NULL OR (lat BETWEEN {LAT_MIN} AND {LAT_MAX})",
            name='ck_entrega_geo_lat_colombia'),
        db.CheckConstraint(
            f"lon IS NULL OR (lon BETWEEN {LON_MIN} AND {LON_MAX})",
            name='ck_entrega_geo_lon_colombia'),
        # Cero metros de incertidumbre no es un GPS perfecto: es un campo mal
        # llenado, y dejaría pasar a la elección del maestro justo la captura
        # de la que menos se sabe.
        db.CheckConstraint('precision_m IS NULL OR precision_m > 0',
                           name='ck_entrega_geo_precision_positiva'),
    )

    recaudo = db.relationship('RecaudoEntrega', backref='geo', lazy=True)

    def to_dict(self):
        return {
            'recaudo_id': self.recaudo_id,
            'cliente_clave': self.cliente_clave,
            'lat': float(self.lat) if self.lat is not None else None,
            'lon': float(self.lon) if self.lon is not None else None,
            'precision_m': float(self.precision_m) if self.precision_m is not None else None,
            'fuente': self.fuente,
            'motivo_sin_dato': self.motivo_sin_dato,
            'capturado_en': self.capturado_en.isoformat() if self.capturado_en else None,
        }


class ClienteGeo(db.Model):
    """Dónde queda la tienda — el maestro, derivado de las capturas.

    QUÉ AFIRMA: que `geo_cliente.elegir_coordenada_del_maestro` corrió sobre
    todas las capturas de ese cliente el `elegido_en` y eligió ese punto, con
    esa política. `capturas_consideradas` y `capturas_descartadas` son el
    denominador y el descarte de esa corrida.

    QUÉ NO AFIRMA: que ahí esté la puerta. La precisión que se guarda es la
    mediana de las precisiones que reportaron los teléfonos, no una medición
    del error real; y con una sola captura de 80 m, el punto puede estar a una
    cuadra. Sirve para que el conductor de la próxima ruta abra Waze cerca, que
    es exactamente el problema que hoy no tiene solución — no para catastro.

    **Es una tabla persistida, no una vista.** Se podría recalcular siempre
    desde `entregas_geo`, y eso sería más limpio hasta el día del acta de
    corte: ahí las capturas se vacían (son registro del ensayo) y el maestro
    quedaría en cero. Lo que tarda tres meses en construirse no puede depender
    de una tabla que se vacía.
    """

    __tablename__ = 'clientes_geo'

    #: Razón social + municipio, normalizados. Ver
    #: `geo_cliente.clave_de_cliente` — y su deuda declarada: el NIT sería la
    #: clave correcta y el WMS no lo persiste en ninguna tabla.
    cliente_clave = db.Column(db.String(220), primary_key=True)

    #: Lo último que el sistema le mostró a alguien, para poder leer el maestro
    #: sin volver a resolver la clave.
    cliente_nombre = db.Column(db.String(200), nullable=True)
    municipio = db.Column(db.String(100), nullable=True)

    lat = db.Column(db.Numeric(9, 6), nullable=True)
    lon = db.Column(db.Numeric(9, 6), nullable=True)
    precision_m = db.Column(db.Numeric(8, 1), nullable=True)

    fuente = db.Column(db.String(20), nullable=False,
                       server_default=_geo.SIN_DATO)

    #: Por qué el maestro no tiene punto pese a existir la fila. Catálogo
    #: propio, distinto del de la captura: acá los motivos son de la
    #: **elección** (`sin_capturas`, `precision_insuficiente`,
    #: `capturas_dispersas`), no del dispositivo. Compartir catálogo haría que
    #: «el conductor negó el permiso» y «las capturas no coinciden entre sí»
    #: se leyeran como el mismo problema, y se arreglan distinto.
    motivo_sin_maestro = db.Column(db.String(28), nullable=True)

    capturas_consideradas = db.Column(db.Integer, nullable=False, server_default='0')
    capturas_descartadas = db.Column(db.Integer, nullable=False, server_default='0')

    elegido_en = db.Column(db.DateTime, nullable=True)

    __table_args__ = (
        db.CheckConstraint(
            "fuente IN ('gps_conductor','corregida_a_mano','sin_dato')",
            name='ck_cliente_geo_fuente'),
        db.CheckConstraint(
            "motivo_sin_maestro IS NULL OR motivo_sin_maestro IN ("
            "'sin_capturas','precision_insuficiente','capturas_dispersas')",
            name='ck_cliente_geo_motivo'),
        db.CheckConstraint(
            "(lat IS NOT NULL AND lon IS NOT NULL AND fuente <> 'sin_dato' "
            "     AND motivo_sin_maestro IS NULL) "
            "OR (lat IS NULL AND lon IS NULL AND fuente = 'sin_dato' "
            "     AND motivo_sin_maestro IS NOT NULL)",
            name='ck_cliente_geo_coordenada_o_motivo'),
        db.CheckConstraint(
            f"lat IS NULL OR (lat BETWEEN {LAT_MIN} AND {LAT_MAX})",
            name='ck_cliente_geo_lat_colombia'),
        db.CheckConstraint(
            f"lon IS NULL OR (lon BETWEEN {LON_MIN} AND {LON_MAX})",
            name='ck_cliente_geo_lon_colombia'),
        db.CheckConstraint('precision_m IS NULL OR precision_m > 0',
                           name='ck_cliente_geo_precision_positiva'),
        db.CheckConstraint('capturas_consideradas >= 0 AND capturas_descartadas >= 0',
                           name='ck_cliente_geo_conteos_no_negativos'),
    )

    def tiene_coordenada(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_dict(self):
        """Lo que la pantalla del conductor necesita para decidir si pinta un
        botón de navegación **o dice que no se sabe**."""
        return {
            'lat': float(self.lat) if self.lat is not None else None,
            'lon': float(self.lon) if self.lon is not None else None,
            'precision_m': float(self.precision_m) if self.precision_m is not None else None,
            'fuente': self.fuente,
            'motivo_sin_maestro': self.motivo_sin_maestro,
            'capturas_consideradas': self.capturas_consideradas or 0,
            'capturas_descartadas': self.capturas_descartadas or 0,
            'elegido_en': self.elegido_en.isoformat() if self.elegido_en else None,
        }
