"""
Adaptador de medición — cumple `flota.puertos.MedidorDeFlota` contra la base
real de WMS.

Regla de este archivo: **un campo devuelve el valor medido o `None`. Nunca 0
por defecto.** `None` se devuelve por una sola causa declarada —la tabla que
alimenta el campo todavía no existe— y esa causa se comprueba preguntándole al
inspector del motor, no atrapando excepciones.

La diferencia no es de estilo. `except Exception: return 0` produce un tablero
que dice "0 documentos vencidos" cuando lo que pasó es que la consulta reventó.
Eso es la regla 5, y es exactamente el `except Exception: pass` de
`ruta_service.py:633` que este módulo tiene prohibido heredar.
"""
from datetime import date as _date
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from typing import List, Optional

from sqlalchemy import inspect as _inspect

from app.extensions import db
from app.utils.fecha import dia_operativo

#: **Ya no gobierna nada.** Las fotos que el sistema exige salen de
#: `angulos_de_custodia(posiciones_llanta)` —11, 13 o 10 según el vehículo—
#: y no de una constante. Vivía del modelo viejo, cuando `llantas` era UNA
#: foto para todas las ruedas; la migración de ángulo lo cambió a una por
#: posición y esto quedó atrás, dando por completa una custodia con 9 de 13.
#: Se conserva solo porque `traspaso.py` la reexporta.
FOTOS_POR_CUSTODIA = 8


def _hoy() -> _date:
    """Fecha de corte de los vencimientos, en día operativo de Bogotá.

    Estaba aislada acá esperando exactamente este cambio. `date.today()` en
    Railway es UTC: a partir de las 7 p.m. Colombia daba el día de mañana, así
    que un SOAT que vence hoy aparecía vencido una noche antes — y el tablero
    de flota cuenta vencidos.
    """
    return dia_operativo()


# Tablas que la tanda 1 va a crear. Mientras no existan, los campos que
# dependen de ellas valen `None` — no cero.
_TABLAS_TANDA_1 = (
    'flota_ficha_tecnica',
    'flota_documento_vehiculo',
    'flota_lectura_odometro',
    'flota_custodia',
    'flota_foto',
)


def _tabla_existe(nombre: str) -> bool:
    """¿Existe la tabla en el motor conectado ahora mismo?

    Se le pregunta al inspector en cada llamada en vez de cachear: durante las
    migraciones de la tanda 1 la respuesta cambia, y un health que cachea el
    "todavía no" se queda diciéndolo después de que ya se creó.
    """
    return _inspect(db.engine).has_table(nombre)


def _contar(consulta) -> int:
    """Ejecuta un COUNT. Si falla, levanta — no devuelve cero."""
    return consulta.count()


class MedidorSQL:
    """Mide contra la base de WMS. Implementa `MedidorDeFlota`."""

    # ── Declaración de a qué apunta el sistema ───────────────────────────────
    #
    # Esta política ya existe inline dos veces en `app/routes/health.py`
    # (`/ping` y `/siesa`). No se toca código global en este paso, así que aquí
    # hay una tercera. Lo que impide que diverja —que es lo que pasó con el
    # fallback de la descensura— es
    # `tests/flota/test_health_flota.py::TestAmbienteNoDiverge`, que compara
    # esta respuesta contra la de `/api/health/ping` y revienta si difieren.
    #
    # Deuda declarada, no accidental: cuando la tanda 2 pueda tocar `app/`, las
    # tres se reemplazan por una sola función y el test de comparación se cae
    # solo por falta de objeto que comparar.

    def ambiente(self) -> str:
        """`datos_de_prueba` | `ensayo` | `simulacion` | `produccion`.

        Mismo vocabulario que `/api/health/ping` a propósito: dos nombres para
        el mismo estado es un nombre que miente esperando su turno.
        """
        from app.services.connekta_gateway import connekta

        # Acceso directo, sin `getattr(..., default)`: si el método
        # desapareciera esto tiene que reventar — un default silencioso acá
        # devuelve 'produccion' con datos de QA, que es exactamente el
        # escenario para el que existe el campo.
        return connekta.modo_datos()

    def datos_reales(self) -> bool:
        """Solo un `produccion` explícito afirma que los números son reales.

        Ante estado desconocido, la respuesta es `False`. Es la regla 0 aplicada
        al mecanismo de aviso: el banner se apaga con una afirmación, no con la
        ausencia de una negación.
        """
        return self.ambiente() == 'produccion'

    # ── Medible hoy: las tablas ya existen ───────────────────────────────────

    def vehiculos_activos(self) -> Optional[int]:
        from app.models.vehiculo import Vehiculo

        if not _tabla_existe('vehiculos'):
            return None
        return _contar(Vehiculo.query.filter(Vehiculo.activo.is_(True)))

    def conductores_activos_sin_cuenta(self) -> Optional[int]:
        """Conductores activos que NO pueden autenticarse.

        No es trivia: `custodia.registrado_por_usuario_id` es NOT NULL. Cada
        conductor sin cuenta es alguien cuya entrega de turno la va a tener que
        registrar otro, y eso hay que saberlo antes de la compuerta de la tanda
        1, no el día que un conductor no puede entrar.
        """
        from app.models.conductor import Conductor

        if not _tabla_existe('conductores'):
            return None
        return _contar(
            Conductor.query.filter(
                Conductor.activo.is_(True),
                Conductor.usuario_id.is_(None),
            )
        )

    def rutas_historicas_sin_placa(self) -> Optional[int]:
        """Rutas sin `vehiculo_id`.

        La especificación §5 lo muestra en `null` = "aún no medido". Se mide:
        la tabla existe desde antes de este módulo. Un `0` medido y un `null`
        son afirmaciones distintas y esta es la primera, no la segunda.

        Importa porque `decision_ruta` (tanda 3) va a asumir placa. Los dos
        caminos de creación de `ruta_service` exigen `vehiculo_id`, pero la
        columna es nullable y las filas viejas pueden no tenerla.
        """
        from app.models.ruta_despacho import RutaDespacho

        if not _tabla_existe('rutas_despacho'):
            return None
        return _contar(RutaDespacho.query.filter(RutaDespacho.vehiculo_id.is_(None)))

    # ── Medible desde la tanda 1: las tablas ya existen ──────────────────────
    #
    # Cada uno de estos campos tiene su test que MUEVE UN DATO REAL y verifica
    # que el número se mueve. Un campo que devuelve una constante plausible es
    # indistinguible de uno medido hasta el día en que importa.

    def fichas_completas(self) -> Optional[int]:
        """Fichas sin ningún atributo en `sin_dato`.

        No cuenta fichas existentes: cuenta fichas que ya no tienen huecos. Una
        ficha creada con todo en `sin_dato` es una fila, no un dato.
        """
        from flota.adaptadores.modelos import FichaTecnica

        if not _tabla_existe('flota_ficha_tecnica'):
            return None
        return sum(1 for f in FichaTecnica.query.all() if f.completa())

    def atributos_sin_dato(self) -> Optional[List[str]]:
        """`['TGZ653.distribucion', ...]` — placa.atributo, no id.atributo.

        Con la placa, porque quien lee esto va a ir a buscar el camión, no la
        fila. Un tablero que obliga a traducir un id a una placa no se usa.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import FichaTecnica

        if not _tabla_existe('flota_ficha_tecnica'):
            return None
        filas = (
            db.session.query(FichaTecnica, Vehiculo.placa)
            .join(Vehiculo, Vehiculo.id == FichaTecnica.vehiculo_id)
            .all()
        )
        return sorted(
            f'{placa}.{atributo}'
            for ficha, placa in filas
            for atributo in ficha.atributos_sin_dato()
        )

    def vehiculos_sin_custodia_activa(self) -> Optional[int]:
        """Vehículos activos sin nadie que responda por ellos ahora mismo."""
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import Custodia

        if not _tabla_existe('flota_custodia'):
            return None
        con_custodia = (
            db.session.query(Custodia.vehiculo_id)
            .filter(Custodia.fin_ts.is_(None))
            .subquery()
        )
        return _contar(
            Vehiculo.query.filter(
                Vehiculo.activo.is_(True),
                ~Vehiculo.id.in_(db.session.query(con_custodia.c.vehiculo_id)),
            )
        )

    def custodias_pendiente_sede(self) -> Optional[int]:
        """Custodias cuya sede no existe como fila en `almacenes`.

        `almacenes` cubre 5 de los 9 centros del mapa de C.O. (medido
        2026-08-01). Flota no crea maestros ajenos para tapar ese hueco: declara
        lo que no puede representar y lo cuenta acá.

        Si este número crece y nadie da de alta la sede, el sistema está
        registrando custodias que no dicen de quién son. Por eso se cuenta,
        no se tolera.
        """
        from flota.adaptadores.modelos import Custodia

        if not _tabla_existe('flota_custodia'):
            return None
        return _contar(Custodia.query.filter(
            Custodia.custodio_estado == 'pendiente_sede'
        ))

    def custodias_cerradas_forzadas(self) -> Optional[int]:
        """Turnos cerrados sin la firma del custodio anterior.

        No mide un fallo del sistema: mide una conducta. **Si este número sube,
        el problema no es que se pueda forzar — es que los conductores no están
        cerrando turno**, y la corrección es esa, no seguir forzando.

        Cada uno de estos deja un turno siguiente sin fotos de cierre con qué
        comparar. Ocho en un mes significa ocho vehículos cuyo próximo daño no
        se le puede atribuir a nadie.
        """
        from flota.adaptadores.modelos import Custodia

        if not _tabla_existe('flota_custodia'):
            return None
        return _contar(Custodia.query.filter(Custodia.cierre_forzado.is_(True)))

    def custodias_sin_foto_completa(self) -> Optional[int]:
        """Custodias a las que les faltan fotos de las ocho del recibo de turno.

        Cuenta las abiertas sin sus 8 de inicio y las cerradas sin sus 8 de fin.
        Una custodia con 7 fotos no es "casi completa": el ángulo que falta es
        justo el que se va a discutir.
        """
        from flota.adaptadores.modelos import Custodia, Foto

        if not _tabla_existe('flota_custodia') or not _tabla_existe('flota_foto'):
            return None

        def _cuantas(entidad_tipo, custodia_id):
            return _contar(Foto.query.filter(
                Foto.entidad_tipo == entidad_tipo,
                Foto.entidad_id == custodia_id,
            ))

        # **Cuántas fotos pide el sistema NO es una constante.** El servidor le
        # arma al conductor `angulos_de_custodia(posiciones_llanta)`: 11 en un
        # furgón (4 llantas), 13 en un camión o NHR (6), 10 en un motocarro.
        # Medir contra 8 daba por completa una custodia con 9 de 13 — y lo que
        # falta son **posiciones de llanta**, que es justo donde está la tuerca
        # floja o la herida de flanco que el registro existe para atribuir.
        #
        # El 8 venía del modelo viejo, cuando `llantas` era UNA foto para todas
        # las ruedas; la migración de ángulo lo cambió a una por posición y esto
        # no se actualizó. El test tampoco lo vio: importaba la misma constante,
        # así que afirmaba la implementación en vez de la regla.
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import FichaTecnica
        from flota.dominio.valores import angulos_de_custodia, posiciones_llanta

        fichas = {f.vehiculo_id: f.posiciones_llanta
                  for f in FichaTecnica.query.all()} if _tabla_existe('flota_ficha_tecnica') else {}
        # Acceso directo: `Vehiculo.tipo` es NOT NULL, y la regla 5 de este
        # módulo prohíbe degradar hacia algo que se parezca al éxito. Un
        # default acá haría caer el resolutor al fallback en silencio.
        tipos = {v.id: v.tipo for v in Vehiculo.query.all()}

        def _exigidas(vehiculo_id):
            n, _fuente = posiciones_llanta(fichas.get(vehiculo_id), tipos.get(vehiculo_id))
            return len(angulos_de_custodia(n))

        incompletas = 0
        for c in Custodia.query.all():
            exigidas = _exigidas(c.vehiculo_id)
            if _cuantas('custodia_inicio', c.id) < exigidas:
                incompletas += 1
            elif c.fin_ts is not None and _cuantas('custodia_fin', c.id) < exigidas:
                incompletas += 1
        return incompletas

    def fotos_pendiente_evidencia(self) -> Optional[int]:
        """Fotos cuya compresión falló y quedaron declaradas rotas. Nunca `pass`."""
        from flota.adaptadores.modelos import Foto

        if not _tabla_existe('flota_foto'):
            return None
        return _contar(Foto.query.filter(Foto.estado == 'pendiente_evidencia'))

    def documentos_no_encontrados(self) -> Optional[int]:
        """Documentos que se buscaron y NO aparecieron.

        Contador aparte de `documentos_vencidos` a propósito: son dos
        afirmaciones distintas. "Vencido" es un papel que existe y caducó;
        "no encontrado" es que nadie pudo mostrar el papel. Sumarlos esconde el
        segundo, que es el más grave — un camión rodando sin SOAT localizable.

        Y sin este contador desaparecerían de los dos: sus fechas son NULL, y
        `fecha_vencimiento < hoy` no matchea NULL. Un cero silencioso.
        """
        from flota.adaptadores.modelos import DocumentoVehiculo

        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        return _contar(DocumentoVehiculo.query.filter(
            DocumentoVehiculo.estado == 'no_encontrado'))

    def documentos_vencidos(self) -> Optional[int]:
        from flota.adaptadores.modelos import DocumentoVehiculo

        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        return _contar(DocumentoVehiculo.query.filter(
            DocumentoVehiculo.fecha_vencimiento < _hoy()
        ))

    def documentos_por_vencer_30d(self) -> Optional[int]:
        """Vigentes que vencen dentro de 30 días. NO incluye los ya vencidos.

        Son dos números distintos a propósito: "por vencer" es una tarea con
        plazo, "vencido" es un camión que no debería estar rodando. Sumarlos
        esconde el segundo dentro del primero.
        """
        from flota.adaptadores.modelos import DocumentoVehiculo

        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        hoy = _hoy()
        return _contar(DocumentoVehiculo.query.filter(
            DocumentoVehiculo.fecha_vencimiento >= hoy,
            DocumentoVehiculo.fecha_vencimiento <= hoy + _timedelta(days=30),
        ))

    # ── Odómetro ─────────────────────────────────────────────────────────────
    #
    # El health no tenía **ni un solo campo** de odómetro: los 15 anteriores no
    # consultan `flota_lectura_odometro`, que aparecía en este archivo solo
    # dentro de `_TABLAS_TANDA_1` para declarar que existe.
    #
    # Por eso nada avisó de lo que había adentro. Medido el 2026-09-01 contra
    # producción: 26 lecturas, **cero con foto**, 20 duplicados exactos, y un
    # salto de +16.354.514 km en 312 horas que dejó al THP696 sin poder
    # registrar kilometraje.
    #
    # Estos campos publican **hechos, no umbrales**. Ninguno decide nada todavía
    # y ninguno lleva una constante inventada: el salto máximo del mes se
    # informa tal cual, para que dentro de un mes se pueda fijar
    # `km_dia_plausible_max` por vehículo **con dato** en vez de a ojo. Es la
    # regla 13 del módulo —ningún número sin decir contra qué base y en qué
    # fecha— aplicada antes de que el número exista.

    def vehiculos_sin_lectura(self) -> Optional[int]:
        """Vehículos activos que nunca tuvieron una lectura de odómetro.

        Distinto de «tiene 0 km» (regla 4): es el denominador de todo lo demás.
        Un CPK sobre un vehículo sin ninguna lectura no es bajo, es imposible.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        con_lectura = {r[0] for r in
                       db.session.query(LecturaOdometro.vehiculo_id).distinct()}
        return _contar(Vehiculo.query.filter(
            Vehiculo.activo.is_(True),
            ~Vehiculo.id.in_(con_lectura or [-1]),
        ))

    def lecturas_sin_foto(self) -> Optional[int]:
        """Lecturas sin evidencia fotográfica atada.

        `LecturaOdometro.foto_id` existía sin escritor hasta el 2026-09-01 — en
        producción eran 26 de 26. Ahora el traspaso lo escribe cuando hay
        `foto_dato`; este contador dice cuántas siguen sin respaldo, que es lo
        que separa «un número que alguien puede verificar» de «un número que
        alguien escribió».
        """
        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        return _contar(LecturaOdometro.query.filter(
            LecturaOdometro.foto_id.is_(None)))

    def lecturas_correccion_30d(self) -> Optional[int]:
        """Correcciones del último mes.

        Una corrección es legítima y deja rastro (exige motivo y autor), pero
        **salta la validación de monotonía entera**. Si este número crece, no es
        que haya más errores de digitación: es que la vía de escape se volvió la
        vía normal — que es exactamente lo que le pasó al THP696 cuando su serie
        quedó envenenada y ninguna lectura común entraba.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        return _contar(LecturaOdometro.query.filter(
            LecturaOdometro.origen == 'correccion',
            LecturaOdometro.ts >= _datetime.utcnow() - _timedelta(days=30),
        ))

    def salto_km_maximo_30d(self) -> Optional[dict]:
        """El salto más grande entre dos lecturas consecutivas del último mes.

        **Es un hecho, no un umbral.** No dice si está bien: dice cuánto fue, de
        qué vehículo y en cuántas horas. Con un mes de esto se puede fijar un
        `km_dia_plausible_max` por vehículo con procedencia, igual que
        `distribucion_fuente` en la ficha. Sin esto, cualquier techo que se
        escriba hoy sería un número a ojo.

        `horas = 0` no se convierte en velocidad infinita: se informa el par y
        quien lea decide. Dos lecturas en el mismo segundo son un hecho común
        acá — hay diez así en producción, del reintento del 2026-08-03.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        desde = _datetime.utcnow() - _timedelta(days=30)
        filas = (LecturaOdometro.query
                 .filter(LecturaOdometro.ts >= desde)
                 .order_by(LecturaOdometro.vehiculo_id, LecturaOdometro.ts,
                           LecturaOdometro.id)
                 .all())
        peor = None
        previa_por_vehiculo = {}
        for l in filas:
            anterior = previa_por_vehiculo.get(l.vehiculo_id)
            previa_por_vehiculo[l.vehiculo_id] = l
            if anterior is None:
                continue
            delta = (l.valor_km or 0) - (anterior.valor_km or 0)
            if delta <= 0:
                continue
            horas = None
            if l.ts is not None and anterior.ts is not None:
                horas = round((l.ts - anterior.ts).total_seconds() / 3600, 2)
            if peor is None or delta > peor['delta_km']:
                peor = {'vehiculo_id': l.vehiculo_id, 'delta_km': delta,
                        'horas': horas, 'valor_km': l.valor_km,
                        'origen': l.origen}
        # Sin saltos NO se devuelve `None`: eso ya significa «no hay tabla de
        # dónde sacarlo», y un mismo valor con dos significados es el defecto
        # que este módulo persigue. Un dict que dice por qué está vacío es una
        # medición; `None` sería una afirmación sobre el sistema.
        if peor is None:
            return {'delta_km': None,
                    'nota': 'ningún vehículo tuvo dos lecturas en la ventana'}
        return peor

    def lecturas_ts_duplicado(self) -> Optional[int]:
        """Lecturas que comparten vehículo y marca de tiempo con otra.

        En producción hay **diez con el mismo segundo** (2026-08-03 20:04:50),
        del reintento que también produjo nueve custodias. Importa porque el
        orden de la serie —y por lo tanto qué lectura supersede a cuál tras una
        corrección— se resuelve por `ts`. Un empate no rompe nada hoy (la
        ventana cuenta hacia el lado estricto), pero es ruido que hay que poder
        contar antes de calcular km/día sobre esta serie.
        """
        from sqlalchemy import func

        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        sub = (db.session.query(LecturaOdometro.vehiculo_id, LecturaOdometro.ts,
                                func.count().label('n'))
               .group_by(LecturaOdometro.vehiculo_id, LecturaOdometro.ts)
               .having(func.count() > 1)
               .all())
        return sum(r.n for r in sub)

    def fichas_con_ancla_incoherente(self) -> Optional[int]:
        """Fichas cuyo `km_inicial` contradice la primera lectura del vehículo.

        `km_inicial` se llama «el ancla del sistema» en el docstring del modelo
        y **ningún código lo lee contra nada** — cero lecturas por AST. En
        producción, **4 de 6 fichas** contradicen su propia serie: la del THP696
        dice 433.434 y su primera lectura es 55.349.

        Por eso el ancla NO se impone como piso: no se sabe cuál de los dos
        números miente, y ponerlo como techo trabaría el vehículo por una
        segunda vía. Se cuenta, se mira, y recién después se decide. Regla 0:
        ante dato en conflicto, declarar antes que elegir en silencio.
        """
        from sqlalchemy import func

        from flota.adaptadores.modelos import FichaTecnica, LecturaOdometro

        if not (_tabla_existe('flota_ficha_tecnica')
                and _tabla_existe('flota_lectura_odometro')):
            return None
        primeras = dict(
            db.session.query(LecturaOdometro.vehiculo_id,
                             func.min(LecturaOdometro.valor_km))
            .group_by(LecturaOdometro.vehiculo_id).all())
        # Sin rama para `km_inicial is None`: la columna es `nullable=False` en
        # el modelo Y en la migración (`f10ta1cimientos_flota_tanda1.py:121`).
        # Una guarda para un estado que la base no permite es una rama que nunca
        # se ejecuta y que sugiere que ese estado existe.
        incoherentes = 0
        for f in FichaTecnica.query.all():
            primera = primeras.get(f.vehiculo_id)
            if primera is not None and primera < f.km_inicial:
                incoherentes += 1
        return incoherentes

    # ── Confianza del kilómetro (2026-09-02) ─────────────────────────────
    #
    # La columna `confianza` nace con estos dos campos y con su pantalla, no un
    # mes después. Es la corrección explícita de lo que pasó con esta misma
    # tabla: `flota_lectura_odometro` vivió un mes sin un solo campo acá, y por
    # eso nadie vio las 26 lecturas sin foto ni el salto de 16,3 millones.

    def lecturas_dudosas_pendientes(self) -> Optional[int]:
        """Kilometrajes que nadie puede respaldar y que todavía deciden algo.

        **La cuenta la hace `verificacion.pendientes()`, no un `WHERE` escrito
        acá.** Ese `WHERE` sería la segunda copia de la política —qué lectura
        sigue contando después de una corrección— y la copia del tablero es la
        que diverge: el número del health diría una cosa y la pantalla mostraría
        otra lista, sin forma de saber cuál creer (regla 0 del WMS).

        Que suba no es un fallo del sistema: es la operación registrando
        kilometrajes sin foto. Que suba **y** `lecturas_verificadas_30d` siga en
        cero es otra cosa, y es la que hay que poder ver.
        """
        from flota.adaptadores.verificacion import pendientes

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        return len(pendientes())

    def lecturas_verificadas_30d(self) -> Optional[int]:
        """Cuántas confirmó una persona en los últimos 30 días.

        Se cuenta por `verificada_ts` —cuándo alguien miró— y no por la fecha de
        la lectura: lo que este campo mide es si la cola se está atendiendo, no
        cuándo se tomó el kilometraje. Con la otra fecha, verificar hoy una
        lectura de julio no movería el número y el trabajo hecho sería invisible.
        """
        from flota.adaptadores.modelos import LecturaOdometro

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        return _contar(LecturaOdometro.query.filter(
            LecturaOdometro.verificada_ts >= _datetime.utcnow() - _timedelta(days=30),
        ))

    # ── Hallazgos (2026-09-01) ───────────────────────────────────────────
    # `flota_hallazgo` nació con su pantalla por vehículo. Sin estas dos, la
    # única forma de saber si hay un daño vencido en la flota es abrir los seis
    # expedientes de a uno — y eso nadie lo hace el martes.
    #
    # Es el mismo defecto que este archivo acaba de arreglar para el odómetro:
    # un sistema de captura sin un solo lector. Cambia que esta vez la medida
    # nace con la tabla, no un mes después.

    def hallazgos_abiertos(self) -> Optional[int]:
        """Daños vivos en toda la flota. `None` si la tabla no existe todavía."""
        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import EstadoHallazgo

        if not _tabla_existe('flota_hallazgo'):
            return None
        return _contar(Hallazgo.query.filter_by(estado=EstadoHallazgo.ABIERTO))

    def hallazgos_vencidos(self) -> Optional[int]:
        """Daños que pasaron su fecha límite sin cerrarse.

        **Contador aparte del de abiertos, no un subconjunto sumado.** Tres
        abiertos y uno vencido se atienden distinto; un solo total esconde
        justo el que urge — la lección de los 639 avisos conocidos.

        El juicio lo emite el dominio (`vencido`), no un `WHERE fecha_limite <
        now()` escrito acá: la regla ya está escrita una vez y una segunda copia
        en SQL sería la que diverja cuando el aplazamiento cambie.
        """
        from datetime import datetime

        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import EstadoHallazgo, vencido

        if not _tabla_existe('flota_hallazgo'):
            return None
        ahora = datetime.utcnow()
        abiertos = Hallazgo.query.filter_by(estado=EstadoHallazgo.ABIERTO).all()
        return sum(1 for h in abiertos if vencido(h.a_dominio(), ahora))

    # ── Inspección diaria (2026-09-02) ───────────────────────────────────
    #
    # `flota_inspeccion` nació ayer con su adaptador y **sin un solo lector**:
    # 370 líneas que escribían un veredicto que nadie consultaba. Es el mismo
    # defecto que este archivo arregló para el odómetro un mes tarde; acá la
    # medida llega con la pantalla.
    #
    # Tres campos, y los tres son distintos a propósito: cuántos camiones no se
    # miraron, cuántos se miraron a medias, y cuánto se tardó en mirarlos.
    # Sumarlos daría un número que no se puede accionar.

    def vehiculos_sin_inspeccion_hoy(self) -> Optional[int]:
        """Vehículos activos sin ninguna inspección del día operativo de hoy.

        El día es el de **Bogotá**, igual que la columna `dia` que escribe el
        adaptador. Con `utcnow().date()` este contador saltaría a las 7 p.m.:
        todos los vehículos aparecerían sin inspeccionar en mitad del turno de
        la tarde. Es la regla 5 del WMS, y acá se ve porque el número es el que
        alguien mira para saber a quién llamar.

        Cuenta **inspecciones de cualquier veredicto**: una incompleta también
        es una inspección, y confundirlas dejaría al camión mirado a medias
        contado dos veces —acá y en `inspecciones_incompletas_hoy`— sin que se
        pueda saber cuántos son en total.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import Inspeccion

        if not _tabla_existe('flota_inspeccion'):
            return None
        inspeccionados = {
            r[0] for r in db.session.query(Inspeccion.vehiculo_id)
            .filter(Inspeccion.dia == _hoy()).distinct()
        }
        return _contar(Vehiculo.query.filter(
            Vehiculo.activo.is_(True),
            ~Vehiculo.id.in_(inspeccionados or [-1]),
        ))

    def inspecciones_incompletas_hoy(self) -> Optional[int]:
        """Inspecciones de hoy con algún ítem sin contestar.

        **`incompleta` no es `no_apto`** (regla 1): una dice «no sé» y la otra
        «sé que está mal». Las dos niegan el despacho y se corrigen distinto —
        `no_apto` se arregla en el taller, `incompleta` se arregla mirando—, así
        que se cuentan por separado. `no_apto` ya tiene su contador: cada uno
        nació con su hallazgo y entra en `hallazgos_abiertos`.

        El vocabulario sale del dominio, no de una cadena escrita acá: dos
        constantes con el mismo texto se separan el día que una de las dos
        cambie.
        """
        from flota.adaptadores.modelos import Inspeccion
        from flota.dominio.inspeccion import INCOMPLETA

        if not _tabla_existe('flota_inspeccion'):
            return None
        return _contar(Inspeccion.query.filter(
            Inspeccion.dia == _hoy(),
            Inspeccion.veredicto == INCOMPLETA,
        ))

    def segundos_llenado_30d(self) -> Optional[dict]:
        """Cuánto se tardó en contestar, sobre las inspecciones del último mes.

        **Es un hecho, no un umbral.** No dice si veinte segundos está mal:
        dice cuánto fue la más rápida y **sobre cuántos ítems** —veinte segundos
        para tres ítems no es lo mismo que para veintiocho—, de qué vehículo y
        con qué veredicto. Con un mes de esto se puede fijar un piso plausible
        con procedencia; sin esto, cualquier techo que se escriba hoy sería un
        número a ojo (regla 13).

        Se publica también la mediana, que es lo que dice cómo se contesta
        normalmente. Un promedio lo movería una sola inspección larga —el
        conductor que dejó la pantalla abierta— y justo el caso que interesa
        vive en el extremo bajo.

        Sin inspecciones NO devuelve `None`: eso ya significa «no hay tabla de
        dónde sacarlo». Un mismo valor con dos significados es el defecto que
        este módulo persigue, así que el vacío viene con su nota.
        """
        from flota.adaptadores.modelos import Inspeccion

        if not _tabla_existe('flota_inspeccion'):
            return None
        desde = _hoy() - _timedelta(days=30)
        filas = Inspeccion.query.filter(Inspeccion.dia >= desde).all()
        if not filas:
            return {'n': 0, 'minimo': None, 'mediana': None,
                    'nota': 'ninguna inspección en los últimos 30 días'}

        segundos = sorted(f.segundos_llenado for f in filas)
        medio = len(segundos) // 2
        mediana = (segundos[medio] if len(segundos) % 2
                   else (segundos[medio - 1] + segundos[medio]) // 2)
        peor = min(filas, key=lambda f: (f.segundos_llenado, f.id))
        return {
            'n': len(filas),
            'mediana': mediana,
            'minimo': {
                'segundos': peor.segundos_llenado,
                'items': peor.items_esperados,
                'inspeccion_id': peor.id,
                'vehiculo_id': peor.vehiculo_id,
                'veredicto': peor.veredicto,
            },
        }

    # ── La plata que sale (2026-09-02) ───────────────────────────────────
    #
    # Nacen con la tabla. `flota_lectura_odometro` vivió un mes con cero campos
    # acá y por eso nada avisó de las 26 lecturas sin foto ni del salto de 16,3
    # millones de km: un sistema de captura sin un solo lector.
    #
    # Y los tres primeros son CONTADORES SEPARADOS a propósito. «Tanqueos que
    # exceden la capacidad» y «tanqueos que no se pudieron mirar» sumados dan
    # un número que no significa nada — es la misma lección de `hallazgos_
    # vencidos` contra `hallazgos_abiertos`, y la de los 639 avisos conocidos.

    def gastos_sin_documento(self) -> Optional[int]:
        """Gastos que no se pueden cruzar con la causación de Siesa.

        No es un defecto del sistema: es **la medida de si la operación está
        entregando las facturas**, que hoy se pierden. Por eso se cuenta en vez
        de bloquearse — un formulario que exige la factura para registrar el
        gasto produce cero gastos registrados, no más facturas.
        """
        from flota.adaptadores.modelos import Gasto

        if not _tabla_existe('flota_gasto'):
            return None
        return _contar(Gasto.query.filter(Gasto.documento_numero.is_(None)))

    def _tanqueos_juzgados(self):
        """`(excedidos, sin_capacidad)` sobre todos los tanqueos registrados.

        Una sola pasada para los dos campos, y **el juicio lo emite el
        dominio**: `costos.excede_capacidad` vía `gastos.excede_capacidad_de`.
        Escribir acá un `galones > capacidad` en SQL sería la segunda copia de
        la política, y la copia del tablero es la que diverge el día que alguien
        decida que hace falta un margen.
        """
        from flota.adaptadores.gastos import excede_capacidad_de
        from flota.adaptadores.modelos import Tanqueo
        from flota.dominio.valores import SIN_DATO

        excedidos = sin_capacidad = 0
        for tq in Tanqueo.query.all():
            veredicto = excede_capacidad_de(tq)
            if veredicto is SIN_DATO:
                sin_capacidad += 1
            elif veredicto:
                excedidos += 1
        return excedidos, sin_capacidad

    def tanqueos_sobre_capacidad(self) -> Optional[int]:
        """Tanqueos con más galones de los que el tanque aguanta según la ficha.

        **No afirma que alguien se haya llevado nada.** Afirma que la capacidad
        de la ficha y los galones registrados no pueden ser los dos ciertos: una
        capacidad mal levantada, un tanque auxiliar que la ficha no conoce, dos
        vehículos en la misma factura o un dedo en el teclado producen el mismo
        número, y ninguna se investiga mejor si el sistema ya dictó sentencia
        (regla 2).
        """
        if not (_tabla_existe('flota_tanqueo')
                and _tabla_existe('flota_ficha_tecnica')):
            return None
        return self._tanqueos_juzgados()[0]

    def tanqueos_sin_capacidad_declarada(self) -> Optional[int]:
        """Tanqueos que el detector **no pudo mirar**, porque la ficha no dice
        cuántos galones caben.

        Sin este contador, un parque entero sin capacidad levantada se vería
        idéntico a uno sin un solo exceso. Es el `SIN_DATO` de
        `costos.excede_capacidad` hecho visible: `False` significaría «se revisó
        y está bien», y lo que pasa es que no hay contra qué revisar.
        """
        if not (_tabla_existe('flota_tanqueo')
                and _tabla_existe('flota_ficha_tecnica')):
            return None
        return self._tanqueos_juzgados()[1]

    def cpk_mes(self) -> Optional[List[dict]]:
        """El costo por kilómetro del mes en curso, por vehículo. **Un hecho.**

        Una lista y no un promedio de flota: el canon
        (`docs/flota/canones/costo_por_kilometro.md` §3) dice que el CPK **no
        compara vehículos**, y promediar el de un NHR con el de un motocarro
        mide la composición del parque, no la operación.

        Solo aparecen los vehículos con algún gasto registrado. Los demás no
        valen cero —«nadie registró nada» no es «no costó nada»— y meterlos con
        `sin_dato` llenaría el tablero de renglones vacíos el primer mes, que es
        cómo un tablero se deja de mirar.

        El mes se calcula con `dia_operativo()`: el 31 a las 8 p.m. de Colombia,
        `date.today()` en Railway ya es del mes siguiente y esto reportaría «el
        mes» sobre un solo día.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.gastos import cpk_de, numero_legible
        from flota.adaptadores.modelos import Gasto

        if not _tabla_existe('flota_gasto'):
            return None
        hoy = _hoy()
        desde = hoy.replace(day=1)
        con_gasto = {g.vehiculo_id for g in db.session.query(Gasto.vehiculo_id)}
        if not con_gasto:
            return []

        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.id.in_(con_gasto))
                  .order_by(Vehiculo.placa).all()):
            r = cpk_de(v.id, desde, hoy)
            salida.append({
                'placa': v.placa,
                # El número **con sus dos insumos**: un CPK suelto no se puede
                # auditar, y éste va a un tablero.
                'cpk': numero_legible(r['cpk']),
                'marca': str(r['marca']),
                'pesos': numero_legible(r['pesos']),
                'km': r['km'],
            })
        return salida

    # ── Taller y garantía (2026-09-02) ───────────────────────────────────
    #
    # Nacen con la tabla. `flota_lectura_odometro` vivió un mes con cero campos
    # acá y por eso nada avisó de las 26 lecturas sin foto ni del salto de 16,3
    # millones de km: un sistema de captura sin un solo lector.
    #
    # Los tres son CONTADORES SEPARADOS y no se suman. «Camiones en el taller»,
    # «trabajos que volvieron sin factura» y «garantías que todavía cubren»
    # contestan preguntas distintas y se atienden distinto — sumarlos daría un
    # número que no se puede accionar, que es la lección de los 639 avisos.

    def ot_abiertas(self) -> Optional[int]:
        """Órdenes de trabajo sin cerrar: camiones que están adentro ahora.

        **Publica un hecho y ningún umbral** (regla 13): no dice si tres es
        mucho, porque no hay una sola medición de cuánto dura una visita al
        taller. Se publica para poder fijar ese techo con dato dentro de un mes,
        que es lo mismo que se hizo con `salto_km_maximo_30d`.
        """
        from flota.adaptadores.modelos import OrdenTrabajo

        if not _tabla_existe('flota_orden_trabajo'):
            return None
        return _contar(OrdenTrabajo.query.filter_by(estado='abierta'))

    def trabajos_sin_factura(self) -> Optional[int]:
        """Trabajos de órdenes ya cerradas cuya factura no llegó.

        **Es el precio de la decisión que define la fase**: la OT no lleva valor
        porque el camión entra hoy y la factura llega el 30, y lo que hace que
        esa decisión no se vuelva un agujero es que la ausencia se cuente.

        Solo entran los de órdenes **cerradas**. Un trabajo de una orden abierta
        no es una deuda: es un camión que sigue adentro, y contarlo dejaría este
        campo permanentemente en rojo — que es exactamente cómo un tablero se
        deja de mirar. Esos se ven en `ot_abiertas`, aparte y sin sumar.

        No es un defecto del sistema, igual que `gastos_sin_documento`: es la
        medida de si la operación está entregando las facturas del taller. Por
        eso se cuenta en vez de bloquearse — exigir la factura para poder cerrar
        una orden produce órdenes que nunca se cierran, no más facturas.
        """
        from flota.adaptadores.modelos import Intervencion, OrdenTrabajo

        if not (_tabla_existe('flota_intervencion')
                and _tabla_existe('flota_orden_trabajo')):
            return None
        return _contar(
            Intervencion.query
            .join(OrdenTrabajo, Intervencion.orden_trabajo_id == OrdenTrabajo.id)
            .filter(OrdenTrabajo.estado == 'cerrada',
                    Intervencion.gasto_id.is_(None)))

    def garantias_vigentes(self) -> Optional[int]:
        """Reparaciones todavía dentro del plazo que la factura pactó.

        **Es el campo que dice si esta fase está haciendo algo.** Si vale 0
        durante meses, la búsqueda que evita pagar dos veces no tiene sobre qué
        pronunciarse; y eso es una conclusión distinta de «no hubo visitas al
        taller», que se lee en `ot_abiertas`. Sin este campo las dos se verían
        igual de vacías.

        El juicio lo emite el **dominio** vía `taller.garantias_vigentes_de`, no
        un `WHERE garantia_hasta_fecha >= hoy` escrito acá: la política ya está
        escrita una vez —incluida la decisión de que una garantía que no se pudo
        juzgar **no cuenta como vigente**— y una segunda copia en SQL sería la
        que diverja. Y divergiría hacia el lado caro: `SIN_DATO` contado como
        vigente es el default optimista de la regla 1.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.taller import garantias_vigentes_de

        if not (_tabla_existe('flota_intervencion')
                and _tabla_existe('flota_orden_trabajo')):
            return None
        hoy = _hoy()
        return sum(len(garantias_vigentes_de(v.id, hoy))
                   for v in Vehiculo.query.filter(Vehiculo.activo.is_(True)))

    # ── Llantas (2026-09-02) ─────────────────────────────────────────────
    #
    # Nacen con la tabla. Y los dos primeros son CONTADORES SEPARADOS a
    # propósito: «posiciones sin llanta» y «vehículos que no se pudieron mirar
    # porque no tienen ficha» sumados dan un número sin significado — es la
    # misma lección de `tanqueos_sobre_capacidad` contra
    # `tanqueos_sin_capacidad_declarada`, y la de los 639 avisos conocidos.

    def _flota_con_llantas(self):
        """`(vehiculos_con_ficha, vehiculos_sin_ficha)` entre los activos.

        Una sola pasada para los dos campos, igual que `_tanqueos_juzgados`.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import FichaTecnica

        con_ficha = {f.vehiculo_id: int(f.posiciones_llanta)
                     for f in FichaTecnica.query.all()}
        activos = Vehiculo.query.filter(Vehiculo.activo.is_(True)).all()
        return ([(v, con_ficha[v.id]) for v in activos if v.id in con_ficha],
                [v for v in activos if v.id not in con_ficha])

    def posiciones_sin_llanta(self) -> Optional[int]:
        """Posiciones declaradas en las fichas que hoy no tienen llanta montada.

        **No es una alarma mecánica.** Casi siempre significa que la llanta está
        puesta y nadie la registró: es la medida de cuánto le falta al inventario
        para describir el vehículo real. El renglón del tablero lo dice con esas
        palabras, porque «4 posiciones sin llanta» leído como falla manda a
        alguien a mirar un camión que está bien.

        El juicio es del dominio (`llantas.posiciones_sin_llanta` vía
        `adaptadores.llantas.posiciones_libres`) y no un `NOT IN` escrito acá:
        la copia del tablero es la que diverge.
        """
        from flota.adaptadores.llantas import posiciones_libres

        if not (_tabla_existe('flota_montaje_llanta')
                and _tabla_existe('flota_ficha_tecnica')):
            return None
        con_ficha, _sin = self._flota_con_llantas()
        total = 0
        for v, _posiciones in con_ficha:
            libres = posiciones_libres(v.id)
            # `SIN_DATO` no puede llegar acá —estos vehículos tienen ficha—,
            # pero si llegara, sumarle `len()` a una cadena contaría ocho
            # posiciones libres por el largo de la palabra. Se descarta con la
            # comparación explícita en vez de confiar en que no pase.
            if isinstance(libres, list):
                total += len(libres)
        return total

    def vehiculos_sin_posiciones_llanta(self) -> Optional[int]:
        """Vehículos activos sin ficha técnica: el contador de arriba **no los
        pudo mirar**.

        Sin este campo, un parque entero sin ficha se vería idéntico a uno con
        las 24 llantas registradas — las dos cosas dan `posiciones_sin_llanta =
        0`. Es exactamente la forma de `tanqueos_sin_capacidad_declarada`, y la
        de `REC-01` y las otras cinco de la auditoría del 2026-08-15.
        """
        if not (_tabla_existe('flota_montaje_llanta')
                and _tabla_existe('flota_ficha_tecnica')):
            return None
        return len(self._flota_con_llantas()[1])

    def llantas_montadas(self) -> Optional[int]:
        """Montajes vigentes en toda la flota.

        Aparte y no como porcentaje del anterior: «0 de 0» (una flota sin ficha)
        y «0 de 24» (nadie registró un solo montaje) dan el mismo porcentaje y
        son los dos estados que hay que poder distinguir.
        """
        from flota.adaptadores.modelos import MontajeLlanta

        if not _tabla_existe('flota_montaje_llanta'):
            return None
        return _contar(MontajeLlanta.query.filter(
            MontajeLlanta.fin_ts.is_(None)))

    def km_por_posicion(self) -> Optional[List[dict]]:
        """El hecho medido: kilómetros de las llantas ya desmontadas, por
        vehículo y posición.

        **Ningún umbral** (regla 13): no hay una sola llanta medida en esta
        flota. Cada entrada dice cuántas vidas completas se midieron y cuántas
        faltan para poder publicar una mediana con procedencia — el mismo patrón
        que `salto_km_maximo_30d` y `segundos_llenado_30d`.

        Solo salen las posiciones con al menos una vida cerrada. Las demás no
        valen cero —«nadie desmontó nada» no es «duró cero»— y llenarían el
        tablero de renglones vacíos el primer mes, que es cómo un tablero se deja
        de mirar.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.llantas import km_por_posicion as _por_posicion

        if not _tabla_existe('flota_montaje_llanta'):
            return None
        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            for fila in _por_posicion(v.id):
                salida.append({'placa': v.placa, **fila,
                               'mediana_km': str(fila['mediana_km'])})
        return salida

    # ── Preventivo (2026-09-02) ──────────────────────────────────────────
    #
    # `distribucion_km_cambio` está cargado desde la tanda 1 y **nadie lo
    # lee**. Estos cinco campos son el lector, y nacen con la tabla: la lección
    # de `flota_lectura_odometro`, que vivió un mes sin un solo campo acá y por
    # eso nada avisó de las 26 lecturas sin foto.
    #
    # Los cuatro contadores se calculan de UNA pasada por el plan de la flota
    # (`contar_por_estado`) y se sirven de ahí. No es una optimización: es que
    # cuatro consultas independientes pueden verse en estados distintos si algo
    # escribe en el medio, y entonces los números del mismo tablero se
    # contradicen entre sí.

    def _cuenta_preventivo(self):
        """Los cinco cubos del plan, en una pasada. El juicio es del dominio.

        `contar_por_estado` llama a `diagnosticar`, que es la misma función que
        alimenta la pantalla y —el día que la plantilla exista— el aviso. Una
        segunda copia escrita en SQL acá sería la que decide si un camión sale
        en rojo, y sería la que diverja el día que cambie la política.
        """
        from flota.adaptadores.preventivo import contar_por_estado

        return contar_por_estado()

    def tareas_vencidas(self) -> Optional[int]:
        """Tareas preventivas pasadas de su kilometraje de cambio.

        **Sin umbral y sin necesitarlo**: el número lo dijo el fabricante y
        está en la ficha desde la tanda 1. Lo único que faltaba era comparar el
        odómetro contra él.
        """
        from flota.dominio.preventivo import EstadoTarea

        if not _tabla_existe('flota_plan_tarea'):
            return None
        return self._cuenta_preventivo()[EstadoTarea.VENCIDA]

    def tareas_por_vencer(self) -> Optional[int]:
        """Tareas que llegan al cambio dentro de la ventana de anticipación.

        Es el único campo de flota que depende de un umbral declarado, y además
        de que el km/día del vehículo se haya podido medir. **Sin ritmo medido
        una tarea no cae acá**: se queda al día con `dias_estimados = sin_dato`,
        porque «no sé cuándo» no es «pronto» — degradarlo a amarillo sería
        exactamente el detector que grita sobre operación sana y se termina
        apagando.
        """
        from flota.dominio.preventivo import EstadoTarea

        if not _tabla_existe('flota_plan_tarea'):
            return None
        return self._cuenta_preventivo()[EstadoTarea.POR_VENCER]

    def tareas_sin_linea_base(self) -> Optional[int]:
        """Tareas con intervalo conocido y ninguna ejecución registrada.

        **No están al día ni vencidas** (regla 4): no hay contra qué comparar,
        y por eso el aviso no sale. Es lo que impide que un preventivo recién
        sembrado dispare cuarenta WhatsApp el día uno — y es el trabajo
        pendiente más barato de la fase, porque se cierra preguntando cuándo se
        hizo la última vez.
        """
        from flota.dominio.preventivo import EstadoTarea

        if not _tabla_existe('flota_plan_tarea'):
            return None
        return self._cuenta_preventivo()[EstadoTarea.SIN_LINEA_BASE]

    def tareas_sin_intervalo(self) -> Optional[int]:
        """Tareas que los tres contadores de arriba **no pudieron mirar**.

        La ficha declara qué aceite lleva el motor y no cada cuántos kilómetros
        se cambia. Sin este contador, un parque entero sin intervalos
        levantados se vería idéntico a uno sin una sola tarea vencida — que es
        cómo un detector se apaga sin que nadie lo note. Mismo motivo que
        `tanqueos_sin_capacidad_declarada`.
        """
        from flota.dominio.preventivo import EstadoTarea

        if not _tabla_existe('flota_plan_tarea'):
            return None
        return self._cuenta_preventivo()[EstadoTarea.SIN_INTERVALO]

    def km_dia_por_vehiculo(self) -> Optional[List[dict]]:
        """El ritmo de uso de cada vehículo. **El hecho que descarga la regla 13.**

        `DIAS_AVISO_PREVENTIVO` es el único umbral de esta fase y hoy no hay
        una sola medición de km/día contra la cual fijarlo. Este campo publica
        el hecho —con `n`, `dias` y la marca de confianza— para poder fijarlo
        con dato dentro de un mes, igual que `salto_km_maximo_30d` existe para
        poder fijar un techo de kilometraje.

        Sale **todo vehículo activo**, incluidos los que devuelven `sin_dato`.
        Es lo contrario de `cpk_mes`, que solo lista los que tienen gasto, y la
        diferencia es deliberada: acá el caso interesante es justamente el que
        no se pudo medir, porque es el que dice cuánto falta para que el umbral
        se pueda fijar. Cada entrada trae su `motivo`.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.preventivo import ritmo_de
        from flota.dominio.valores import SIN_DATO, palabra_de_confianza

        if not _tabla_existe('flota_lectura_odometro'):
            return None
        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            r = ritmo_de(v.id)
            salida.append({
                'placa': v.placa,
                # `str` y no `float`: un `Decimal` no es serializable y
                # redondearlo acá escondería sobre cuántos días está parado.
                # `SIN_DATO` ya es una cadena y pasa entero, que es el punto.
                'km_dia': str(round(r.km_dia, 2)) if r.km_dia is not SIN_DATO
                          else str(r.km_dia),
                # `palabra_de_confianza` y no `str(...)`: `Confianza` hereda de
                # `str` y de `Enum`, y en 3.11 `str(Confianza.DECLARADA)` es
                # `'Confianza.DECLARADA'`. Este campo publicaba eso mientras
                # `cpk_mes` —del mismo JSON— publicaba `'declarada'`, y
                # `flota.js:1745` lo imprimía tal cual en el panel de salud.
                # No lo veía nadie porque el único test que lee esta marca usa
                # un vehículo sin lecturas, donde vale `sin_dato` y `str()` es
                # un no-op. Lo destapó el día completo, que sí deja lecturas.
                'marca': palabra_de_confianza(r.marca),
                'n': r.n,
                'dias': str(round(r.dias, 1)) if r.dias is not SIN_DATO
                        else str(r.dias),
                'motivo': r.motivo,
            })
        return salida


__all__ = ['MedidorSQL', '_TABLAS_TANDA_1', 'FOTOS_POR_CUSTODIA']
