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
from sqlalchemy import func as _func
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


def _motivo_cpk(r: dict) -> Optional[str]:
    """Por qué ESTE CPK no se pudo calcular. `None` cuando sí se pudo.

    Los caminos de `costo_por_kilometro` a `SIN_DATO` (canon §6) devuelven todos
    el mismo valor y **se corrigen llamando a personas distintas**: uno con una
    factura de contabilidad, otro con una lectura de odómetro del conductor, el
    tercero con una verificación contra la foto que hace control de flota. Un
    `sin_dato` sin motivo manda a mirar los tres para descubrir cuál — que es
    exactamente cómo se aprendió a ignorar los 639 avisos conocidos.

    ## El orden es el de `costo_por_kilometro`, y eso no es cosmético

    `costos.py:308-319` evalúa `marca == SIN_DATO` → `km <= 0` → `not
    hubo_gastos`. Un vehículo puede tener dos de esas mal a la vez, y si acá se
    preguntara en otro orden se publicaría un motivo que **no es el que produjo
    el `sin_dato`**. Es la regla 0 en miniatura: la explicación de una decisión
    tiene que seguir a la decisión, no re-derivarla por su cuenta.

    ## `marca is SIN_DATO` tiene DOS causas y se separan con `lecturas`

    `_tramo_de` (`gastos.py:644`) devuelve `SIN_DATO` tanto con menos de dos
    lecturas —donde no hay tramo que juzgar— como con dos extremos dudosos.
    Decirle «verificá el kilometraje» a quien no registró ninguno lo manda a
    hacer el trabajo equivocado, así que `cpk_de` publica `lecturas` para poder
    separarlas.
    """
    from flota.dominio.valores import SIN_DATO

    if r['cpk'] is not SIN_DATO:
        return None
    if r['marca'] is SIN_DATO:
        if r['lecturas'] < 2:
            return ('menos de dos lecturas de odómetro dentro del mes: no hay '
                    'tramo que dividir. Se resuelve registrando kilometraje.')
        return ('los dos extremos del tramo son dudosos: ninguno se verificó '
                'contra su foto. Se resuelve verificando kilometrajes.')
    if r['km'] <= 0:
        return ('el odómetro no avanzó dentro del mes: la primera y la última '
                'lectura marcan lo mismo, así que no hay entre qué dividir.')
    if not r['hubo_gastos']:
        return ('ningún gasto registrado contra este vehículo. No es que sea '
                'gratis: es que nadie ha cargado una factura.')
    # Los de arriba son los únicos caminos de `costo_por_kilometro` a
    # `SIN_DATO`. Si se llega acá apareció uno nuevo, y hay que nombrarlo — no
    # devolver `None`, que la pantalla leería como «sí se pudo calcular». Un
    # motivo feo es información; el silencio es evidencia falsa.
    return ('no se pudo calcular y el motivo no está clasificado: apareció un '
            'camino nuevo en `costo_por_kilometro` que nadie nombró acá.')


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

    # ── Analítica despromediada (2026-09-04) ─────────────────────────────
    #
    # Los contadores de arriba contestan «¿cuántos?». Estos contestan «¿cuál?»,
    # y con seis vehículos esa es la única pregunta que se puede contestar
    # honestamente: un p90 de seis datos es el máximo con otro nombre, y las
    # bisagras de un boxplot de seis SON dos camiones con placa.
    #
    # Salen TODOS los vehículos activos, incluidos los que no tienen nada — es
    # la misma decisión de `cpk_mes` y de `km_dia_por_vehiculo`, por el mismo
    # motivo: el que no tiene nada registrado es el caso a atender, y esconderlo
    # lo vuelve invisible.

    def procedencia_del_tablero(self) -> dict:
        """De qué mundo salen los números de este tablero, y de qué día.

        No mide ninguna tabla de flota —mide el sistema— y por eso está exento
        de la regla «`None` si falta la tabla», junto con `ambiente` y
        `datos_reales`. Forzarle esa regla lo volvería mentira.

        Existe porque la regla 13 se cumple a nivel de página además de a nivel
        de fila: `rutas_historicas_sin_placa` valió 0 en una SQLite vacía y se
        leyó como «todas las rutas tienen placa» **dos veces**, la segunda
        después de la advertencia. La defensa no es leer con más cuidado.

        `calculado_ts` va en Bogotá **con su offset explícito** (regla 5 del
        WMS): un tablero que dice «calculado a las 21:30» sin decir de qué huso
        se lee mal exactamente en la franja en que UTC ya cambió de día, que es
        la franja en la que se cierra el turno de la tarde.
        """
        from app.utils.fecha import ahora_bogota

        return {
            'ambiente': self.ambiente(),
            'datos_reales': self.datos_reales(),
            'dia_operativo': _hoy().isoformat(),
            'calculado_ts': ahora_bogota().isoformat(),
        }

    def lecturas_por_vehiculo(self) -> Optional[List[dict]]:
        """El kilómetro de cada camión: cuántas lecturas, y cuánto se le cree.

        **Es el único panel con datos hoy, y por eso va primero**: mientras esté
        en rojo, el CPK, el km/día y el km-por-llanta salen `sin_dato` por
        diseño. Es también la única métrica cuyo tablero es a la vez el trabajo
        — mirarla y arreglarla son el mismo gesto.

        Despromediado hacia adentro y no entre camiones: cada fila compara al
        vehículo **consigo mismo** (cuántas de sus lecturas tienen foto, cuántas
        siguen contando tras la última corrección). Un porcentaje de flota sobre
        26 lecturas escondería que las cuatro de un camión son todas dudosas.

        `vigentes` sale de `vigentes_tras_la_ultima_correccion`, la misma
        política que usan el CPK y el traspaso — **no un `WHERE` escrito acá**.
        La copia del tablero sería la que diverge, y sería la que decide si un
        número se publica (regla 0).
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import LecturaOdometro
        from flota.dominio import odometro as dom_odo
        from flota.dominio.procedencia import Ventana
        from flota.dominio.valores import Confianza, Lectura, OrigenLectura

        if not (_tabla_existe('flota_lectura_odometro')
                and _tabla_existe('vehiculos')):
            return None

        filas = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            todas = (LecturaOdometro.query
                     .filter_by(vehiculo_id=v.id)
                     .order_by(LecturaOdometro.ts, LecturaOdometro.id).all())
            vigentes, _ = dom_odo.vigentes_tras_la_ultima_correccion([
                Lectura(valor_km=l.valor_km, ts=l.ts,
                        origen=OrigenLectura(l.origen),
                        autor_usuario_id=l.autor_usuario_id,
                        motivo_correccion=l.motivo_correccion)
                for l in todas])
            # Los tres estados van SEPARADOS y ninguno se suma a otro: se
            # atienden distinto. `verificada` es trabajo hecho, `dudosa` es
            # trabajo pendiente, `declarada` es lo normal y no hay nada que
            # hacerle. Un solo total escondería cuál de los tres creció.
            por_confianza = {c.value: 0 for c in Confianza}
            for l in todas:
                if l.confianza in por_confianza:
                    por_confianza[l.confianza] += 1
            filas.append({
                'placa': v.placa,
                'n': len(todas),
                'con_foto': sum(1 for l in todas if l.foto_id is not None),
                'vigentes': len(vigentes),
                'por_confianza': por_confianza,
                'primera': todas[0].ts.date().isoformat() if todas else None,
                'ultima': todas[-1].ts.date().isoformat() if todas else None,
                # `motivo` y no un hueco mudo: un camión con cero lecturas y uno
                # con cuatro dudosas salen los dos sin CPK, y se arreglan
                # distinto.
                'motivo': (None if todas else
                           'ninguna lectura de odómetro registrada. Nace en el '
                           'recibo de turno, con la foto del tablero.'),
                # La base va en CADA fila y no una vez para la lista, aunque se
                # repita seis veces: una fila se copia sola a un WhatsApp o a un
                # correo, y ahí llega sin la cabecera que la explicaba.
                'base': 'lecturas de odómetro registradas contra este vehículo',
                'etiqueta': 'toda la historia registrada',
            })
        return filas

    def rendimiento_por_vehiculo(self) -> Optional[List[dict]]:
        """Los km/galón de cada camión, **con si ya se pueden sostener**.

        Es el mismo cálculo que ve el conductor (`rendimiento_publicable_de`),
        con una diferencia deliberada de frontera: acá el número viaja **aunque
        `publicable` sea False**, y allá no.

        No es una inconsistencia: es que las dos pantallas contestan preguntas
        distintas. La del conductor contesta «¿cómo vengo?», y un número que no
        se sostiene, con su nombre encima, es ruido que él no puede corregir. La
        de control de flota contesta «¿ya se puede medir esto?», y para eso hace
        falta ver el número provisional junto a lo que le falta.

        Qué se puede sostener lo decide **una sola función** (regla 0); quién lo
        pinta lo decide cada frontera.

        Y no hay ranking ni promedio de flota, acá tampoco: un motocarro y un
        NHR no rinden igual y la diferencia no dice nada. Es una lista ordenada
        por placa, no por rendimiento — ordenarla por el número la convertiría
        en un ranking sin que nadie lo hubiera decidido.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.gastos import (numero_legible,
                                              rendimiento_publicable_de)

        if not (_tabla_existe('flota_tanqueo') and _tabla_existe('vehiculos')):
            return None
        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            r = rendimiento_publicable_de(v.id)
            salida.append({
                'placa': v.placa,
                'km_galon': numero_legible(r['km_galon'], 2),
                'publicable': r['publicable'],
                'motivo': r['motivo'],
                'ventanas': r['ventanas'],
                'tanqueos': r['tanqueos'],
                'tanqueos_fuera_por_parcial': r['tanqueos_fuera_por_parcial'],
                'dias_historia': r['dias_historia'],
                'base': ('kilómetros y galones sumados sobre las ventanas de '
                         'tanque lleno a tanque lleno de este vehículo'),
                'etiqueta': 'toda la historia registrada',
            })
        return salida

    def cobertura_por_vehiculo(self) -> Optional[List[dict]]:
        """Qué le falta a cada camión para poder medirse. **El índice del tab.**

        Una grilla de seis filas y no un «73% de cobertura»: con seis vehículos
        el porcentaje es estrictamente MENOS información que la lista, porque
        el porcentaje no dice a cuál llamar.

        Cada celda es la condición de existencia de un número aguas abajo, y por
        eso el panel se lee como una lista de trabajo y no como un diagnóstico:
        sin `capacidad_tanque` no hay detector de sobre-tanqueo, sin
        `posiciones_llanta` no hay vida de llanta, sin `intervalos` no hay
        preventivo por kilómetro.

        `None` en una celda significa «no se pudo mirar porque falta la tabla»,
        y **no se colapsa a `False`**: un parque entero sin ficha levantada se
        vería idéntico a uno impecable, que es cómo un detector se apaga sin que
        nadie lo note.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import (DocumentoVehiculo, FichaTecnica,
                                               LecturaOdometro)

        if not _tabla_existe('vehiculos'):
            return None
        hay_ficha = _tabla_existe('flota_ficha_tecnica')
        hay_doc = _tabla_existe('flota_documento_vehiculo')
        hay_lect = _tabla_existe('flota_lectura_odometro')

        fichas = ({f.vehiculo_id: f for f in FichaTecnica.query.all()}
                  if hay_ficha else {})
        salida = []
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            f = fichas.get(v.id)
            salida.append({
                'placa': v.placa,
                'ficha': None if not hay_ficha else (f is not None),
                'ficha_completa': None if not hay_ficha else bool(f and f.completa()),
                'capacidad_tanque': (None if not hay_ficha else
                                     bool(f and f.capacidad_tanque_galones)),
                'posiciones_llanta': (None if not hay_ficha else
                                      bool(f and f.posiciones_llanta)),
                'documentos': (None if not hay_doc else bool(
                    DocumentoVehiculo.query.filter_by(vehiculo_id=v.id).first())),
                'lecturas': (None if not hay_lect else bool(
                    LecturaOdometro.query.filter_by(vehiculo_id=v.id).first())),
            })
        return salida

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

    def custodias_por_vehiculo(self) -> Optional[List[dict]]:
        """Los turnos que piden trabajo, **con placa**. Una fila por custodia.

        Dos de las cinco señales con las que se mide a control de flota
        —`custodias_cerradas_forzadas` y `custodias_sin_foto_completa`— salían
        como un número pelado. *«Si crece, nadie está cerrando turno»* es
        accionable; *«3»* no dice a qué camión ni a qué turno, y perseguirlo
        obligaba a abrir los seis expedientes.

        Los dos contadores derivan de esta lista por el mismo motivo que los de
        documentos: el predicado de «sin foto completa» es un recorrido con
        cuatro entradas —ficha, tipo de vehículo, ángulos y posiciones de
        llanta— y escrito dos veces diverge sin que nadie lo note, porque los
        dos siguen devolviendo un entero plausible.

        **El `elif` es load-bearing y se conserva:** una custodia con las dos
        mitades incompletas cuenta UNA vez, no dos. `sin_foto_completa` mide
        turnos, no mitades. La fila dice cuál falta en `mitad`, que es lo que
        hacía falta para poder ir a arreglarlo.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import Custodia, FichaTecnica, Foto
        from flota.dominio.valores import angulos_de_custodia, posiciones_llanta

        if not _tabla_existe('flota_custodia') or not _tabla_existe('flota_foto'):
            return None

        fichas = {f.vehiculo_id: f.posiciones_llanta
                  for f in FichaTecnica.query.all()} if _tabla_existe('flota_ficha_tecnica') else {}
        vehiculos = {v.id: v for v in Vehiculo.query.all()}
        custodias = Custodia.query.all()

        # ── Las fotos, en UNA consulta agrupada y no una por custodia ────
        #
        # La versión anterior hacía un COUNT por custodia (dos con la mitad de
        # cierre). Medido el 2026-09-09 sobre SQLite con 6 vehículos: el health
        # completo tardaba 61 ms con 26 custodias, 128 con 200 y 465 con 1000 —
        # lineal, y esta función corre **3 veces por petición** (la publica el
        # health y la consultan sus dos contadores derivados).
        #
        # Seis vehículos con dos turnos diarios producen ~4.400 custodias al
        # año, o sea ~2 s por petición dentro de doce meses. No es un problema
        # hoy y por eso no se cacheó —un medidor que cachea deja de medir—: se
        # quitó el N+1, que es la causa y no el síntoma.
        conteos = {(t, c.id): 0
                   for c in custodias
                   for t in ('custodia_inicio', 'custodia_fin')}
        if custodias:
            for tipo, cid, n in (Foto.query
                                 .with_entities(Foto.entidad_tipo, Foto.entidad_id,
                                                _func.count(Foto.id))
                                 .filter(Foto.entidad_tipo.in_(
                                     ('custodia_inicio', 'custodia_fin')))
                                 .group_by(Foto.entidad_tipo, Foto.entidad_id)
                                 .all()):
                # Solo las custodias vivas: una foto colgada de un id que ya no
                # existe no debe crear una entrada fantasma.
                if (tipo, cid) in conteos:
                    conteos[(tipo, cid)] = n

        def _cuantas(entidad_tipo, custodia_id):
            # Indexado y no `.get(k, 0)`: el diccionario se sembró en cero para
            # TODAS las custodias, así que una clave ausente es un bug de esta
            # función, no una custodia sin fotos.
            return conteos[(entidad_tipo, custodia_id)]

        def _exigidas(vehiculo_id):
            # `fichas.get(...)` sin default sí es legítimo: «este vehículo no
            # tiene ficha» es un estado real y `posiciones_llanta` lo resuelve
            # con el supuesto por tipo, diciendo que es un supuesto. Lo que no
            # se admite es inventar el vehículo.
            n, _fuente = posiciones_llanta(fichas.get(vehiculo_id),
                                           vehiculos[vehiculo_id].tipo)
            return len(angulos_de_custodia(n))

        salida = []
        for c in custodias:
            exigidas = _exigidas(c.vehiculo_id)
            inicio = _cuantas('custodia_inicio', c.id)
            mitad, tiene = None, None
            if inicio < exigidas:
                mitad, tiene = 'inicio', inicio
            elif c.fin_ts is not None:
                fin = _cuantas('custodia_fin', c.id)
                if fin < exigidas:
                    mitad, tiene = 'fin', fin
            forzada = bool(c.cierre_forzado)
            if mitad is None and not forzada:
                continue
            salida.append({
                # Mismo criterio que en documentos: sin respaldo silencioso.
                'placa': vehiculos[c.vehiculo_id].placa,
                'custodia_id': c.id,
                'inicio_ts': c.inicio_ts.isoformat() if c.inicio_ts else None,
                'fin_ts': c.fin_ts.isoformat() if c.fin_ts else None,
                'abierta': c.fin_ts is None,
                'cierre_forzado': forzada,
                'cierre_forzado_motivo': c.cierre_forzado_motivo,
                'sin_foto_completa': mitad is not None,
                'mitad_incompleta': mitad,
                'fotos': tiene,
                'fotos_exigidas': exigidas if mitad is not None else None,
                'base': 'custodias registradas contra el vehículo',
                'etiqueta': 'toda la historia registrada',
            })
        return sorted(salida, key=lambda f: (f['placa'], f['custodia_id']))

    def custodias_cerradas_forzadas(self) -> Optional[int]:
        """Turnos que cerró alguien que no era el custodio, sin fotos de cierre.

        **No mide una falla del sistema: mide una conducta.** Si crece, el
        problema no es el software — es que nadie está cerrando turno.
        """
        filas = self.custodias_por_vehiculo()
        return None if filas is None else sum(1 for f in filas
                                              if f['cierre_forzado'])

    def custodias_sin_foto_completa(self) -> Optional[int]:
        """Turnos sin el juego completo de fotos exigido por su ficha.

        Sin fotos comparables, un golpe nuevo no se le puede atribuir a nadie —
        ni al conductor ni al turno anterior.
        """
        filas = self.custodias_por_vehiculo()
        return None if filas is None else sum(1 for f in filas
                                              if f['sin_foto_completa'])

    def fotos_pendiente_evidencia(self) -> Optional[int]:
        """Fotos cuya compresión falló y quedaron declaradas rotas. Nunca `pass`."""
        from flota.adaptadores.modelos import Foto

        if not _tabla_existe('flota_foto'):
            return None
        return _contar(Foto.query.filter(Foto.estado == 'pendiente_evidencia'))

    def documentos_por_vehiculo(self) -> Optional[List[dict]]:
        """Los papeles que piden trabajo, **con placa**. Una fila por documento.

        ## Por qué existe, y por qué los tres contadores derivan de acá

        `documentos_vencidos` decía «1» y no decía de cuál camión. El trabajo
        escrito de control de flota es *«persigue lo vencido»*
        (`especialista-control-flota.md`), y un contador sin placa no dice a
        quién llamar — hay que abrir los seis expedientes a mano para saberlo.
        Es el criterio 1 del tab de analítica («ninguna cifra sin su
        enumeración al lado») incumplido en el panel donde más se nota.

        No se memoiza, y se intentó: un `MedidorSQL` reusado devolvía la lista
        de antes de la última escritura, y tres tests de `test_medicion_t1.py`
        —que miden, escriben y vuelven a medir sobre la misma instancia— lo
        cazaron en el primer intento. Un medidor que cachea deja de medir y
        sigue contestando 200, que es el defecto que este módulo entero existe
        para no tener. Sobre seis vehículos el recorrido no se nota.

        Los tres contadores **se calculan de esta lista** y no con su propio
        `filter`. Escritos dos veces, el día que alguien cambie «30 días» o
        agregue el filtro por vehículo activo, el número del tablero y la lista
        de abajo dejan de decir lo mismo — y no hay forma de saber cuál creer
        (regla 0 del WMS, corolario «una política, una función»).

        ## Tres banderas y no una categoría

        Las tres son disjuntas **hoy**, y no por esta función: lo impone el
        CHECK `ck_flota_doc_estado_coherente`, que exige `fecha_vencimiento IS
        NULL` cuando el estado es `no_encontrado`. O sea que un papel no puede
        estar vencido y no encontrado al mismo tiempo.

        Aun así van tres banderas y no un campo `clase`, por dos motivos:

        1. **Mapean uno a uno contra los tres contadores**, que es lo que hace
           la derivación verificable de un vistazo. Con un `clase` haría falta
           una tabla de traducción, y una traducción es donde un cambio futuro
           reclasifica en silencio sin que ningún test se ponga rojo.
        2. La disyunción vive en la base, no acá. Si mañana el CHECK se
           relaja —un `no_encontrado` al que se le quiera conservar la fecha—
           esta función sigue contando bien y la fila lo dice; con un `clase`
           habría que elegir una y el otro contador bajaría solo.

        `tests/flota/test_medicion_t1.py` afirma la disyunción contra la base
        real, en vez de dejarla como un supuesto de este comentario.

        ## Alcance heredado, y declarado

        **No filtra por vehículo activo**, porque los tres contadores tampoco lo
        hacían y cambiarlo acá movería sus números sin que nadie lo pidiera. La
        consecuencia es nueva y visible: la lista puede nombrar la placa de un
        vehículo dado de baja, y mandar a perseguir el SOAT de un camión que no
        rueda. Queda escrito para que sea una decisión y no un descubrimiento —
        si se filtra, se filtran los cuatro a la vez.

        Los tres se atienden distinto y por eso no se suman: *vencido* es un
        camión que no debería estar rodando, *por vencer* es una cita que hay
        que sacar, y *no encontrado* es un papel que hay que buscar
        (`ESPECIFICACION_T1.md:264`).
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import DocumentoVehiculo

        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        hoy = _hoy()
        limite = hoy + _timedelta(days=30)
        placas = {v.id: v.placa for v in Vehiculo.query.all()}
        salida = []
        for d in DocumentoVehiculo.query.all():
            vence = d.fecha_vencimiento
            # `vence is None` no es «no vence»: es que nadie escribió la fecha.
            # No se cuenta como vencido ni como al día — es el papel que hay que
            # ir a mirar, y sale por la bandera de `no_encontrado` si lo está.
            vencido = vence is not None and vence < hoy
            por_vencer = vence is not None and hoy <= vence <= limite
            no_encontrado = d.estado == 'no_encontrado'
            if not (vencido or por_vencer or no_encontrado):
                continue
            salida.append({
                # Indexado y no `.get(..., default)`: `placas` trae TODOS los
                # vehículos y la columna tiene FK, así que una placa ausente es
                # una FK rota, no un dato que falta. Un `'#12'` de respaldo se
                # publicaría con cara de placa y el panel mandaría a buscar un
                # camión que no existe (regla 5).
                'placa': placas[d.vehiculo_id],
                'tipo': d.tipo,
                'estado': d.estado,
                'vence': vence.isoformat() if vence else None,
                # Firmado: negativo = ya venció hace tantos días. El signo es la
                # diferencia entre «sacá la cita» y «bajá el camión».
                'dias': (vence - hoy).days if vence else None,
                'vencido': vencido,
                'por_vencer_30d': por_vencer,
                'no_encontrado': no_encontrado,
                'base': 'documentos registrados contra el vehículo',
                'etiqueta': 'estado al día de hoy',
            })
        # Lo peor primero: el más vencido arriba. Los que no tienen fecha van al
        # final, y no de primeros por un `None` que ordena raro.
        return sorted(salida, key=lambda f: (f['dias'] is None, f['dias'] or 0))

    def _cuantos_documentos(self, bandera) -> Optional[int]:
        filas = self.documentos_por_vehiculo()
        return None if filas is None else sum(1 for f in filas if f[bandera])

    def documentos_no_encontrados(self) -> Optional[int]:
        """Papeles que nadie pudo mostrar. Aparte de los vencidos a propósito:
        uno se renueva y el otro se busca, y sumarlos esconde el segundo."""
        return self._cuantos_documentos('no_encontrado')

    def documentos_vencidos(self) -> Optional[int]:
        """Papeles caducados. El vehículo no debería salir."""
        return self._cuantos_documentos('vencido')

    def documentos_por_vencer_30d(self) -> Optional[int]:
        """Vigentes que vencen dentro de 30 días. NO incluye los ya vencidos.

        Son dos números distintos a propósito: "por vencer" es una tarea con
        plazo, "vencido" es un camión que no debería estar rodando. Sumarlos
        esconde el segundo dentro del primero.
        """
        return self._cuantos_documentos('por_vencer_30d')

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

    def dias_hallazgo_abierto(self) -> Optional[dict]:
        """Cuánto tarda un daño en resolverse, **caso por caso y con placa**.

        `docs/procedimientos/roles/especialista-control-flota.md:119` promete
        «Días promedio de hallazgo abierto» como señal de desempeño de ese rol.
        El canon existe desde el 2026-08-03
        (`docs/flota/canones/dias_hallazgo_abierto.md`), `promedio_del_indicador`
        se escribió ese mismo día — y **no tenía un solo caller de producción**:
        figuraba en la lista de deuda declarada de
        `tests/flota/test_trinquetes_flota.py`. La ficha prometía un número que
        ninguna pantalla mostraba, que es justo lo que
        `docs/procedimientos/README.md:16` prohíbe.

        Este campo es ese caller, y sale despromediado: primero la lista de
        casos con su placa, después el promedio con su `n`. Con un puñado de
        hallazgos el promedio no dice a qué camión llamar, y el canon ya prohíbe
        compararlo entre zonas.

        `n_fuera` viaja porque es el denominador: un indicador que solo reporta
        lo que mira devolvería «0 días promedio» sobre una flota con veinte
        hallazgos de línea base, y eso se lee como «no hay demoras».

        Los dos números que el canon prohíbe mezclar salen separados por fila:
        `dias` es duración cerrada, `dias_lleva` es antigüedad viva. El aviso de
        WhatsApp usa el segundo; el indicador usa el primero.
        """
        from datetime import datetime

        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.modelos import Hallazgo
        from flota.dominio.hallazgo import indicador_dias_abierto
        from flota.dominio.valores import SIN_DATO

        if not _tabla_existe('flota_hallazgo'):
            return None
        filas = (db.session.query(Hallazgo, Vehiculo.placa)
                 .join(Vehiculo, Vehiculo.id == Hallazgo.vehiculo_id)
                 .order_by(Hallazgo.reportado_ts, Hallazgo.id).all())
        # El juicio lo emite el dominio de una sola pasada, y `juicios` vuelve
        # EN EL MISMO ORDEN — así el adaptador le pega la placa sin que el
        # dominio tenga que conocerla.
        r = indicador_dias_abierto([h.a_dominio() for h, _ in filas],
                                   datetime.utcnow())
        casos = []
        for (h, placa), j in zip(filas, r['juicios']):
            caso = {'placa': placa, 'reportado': h.reportado_ts.date().isoformat()}
            caso.update(j)
            # `dias` sale como palabra cuando el hallazgo sigue abierto: un 0 ahí
            # diría «se resolvió al instante».
            if j.get('dias') is SIN_DATO:
                caso['dias'] = str(SIN_DATO)
            casos.append(caso)

        promedio = r['promedio_dias']
        return {
            'casos': casos,
            # El promedio SIEMPRE con su n al lado, y detrás de los casos. Un
            # promedio de 2 y uno de 200 son el mismo número con distinta
            # autoridad.
            'promedio_dias': (str(SIN_DATO) if promedio is SIN_DATO
                              else round(float(promedio), 1)),
            'n': r['n'],
            'n_abiertos': r['n_abiertos'],
            'n_vencidos': r['n_vencidos'],
            'n_fuera': r['n_fuera'],
            'motivo': (None if r['n'] else
                       'ningún hallazgo cerrado todavía: no hay duración que '
                       'promediar. El indicador nace al cerrar el primer daño '
                       'con su odómetro y su evidencia.'),
            'base': ('hallazgos que entran al indicador — sin línea base, sin '
                     'descartados y sin no_aplica (canon §6)'),
            'etiqueta': 'toda la historia registrada',
        }

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
        """El costo por kilómetro del mes en curso. **Un hecho, de los seis.**

        Una lista y no un promedio de flota: el canon
        (`docs/flota/canones/costo_por_kilometro.md` §3) dice que el CPK **no
        compara vehículos**, y promediar el de un NHR con el de un motocarro
        mide la composición del parque, no la operación.

        ## Sale TODO vehículo activo, incluido el que no tiene un solo gasto

        Hasta el 2026-09-04 esto filtraba `Vehiculo.id.in_(con_gasto)` y encima
        devolvía `[]` temprano si nadie había registrado nada en toda la tabla.
        El motivo escrito era que meter a los demás *«llenaría el tablero de
        renglones vacíos el primer mes, que es cómo un tablero se deja de
        mirar»*.

        Ese motivo era falso por dos razones, y las dos se midieron:

        · **Con cero filas en `flota_gasto` —que es el estado de hoy— el filtro
          no escondía renglones vacíos: escondía el tablero entero.** El campo
          salía `[]`, que la pantalla lee igual que «no hay nada que reportar».
        · **El vehículo del que nadie registró nada ES el caso a atender.** Con
          seis vehículos, «no aparece» y «no costó nada» se leen igual, y el
          primero se corrige con una llamada.

        `km_dia_por_vehiculo`, en este mismo archivo, ya había tomado la
        decisión contraria y su docstring se contrastaba explícitamente con
        éste llamando a la diferencia «deliberada». El archivo se contradecía
        consigo mismo; ahora no.

        `hubo_gastos` viaja porque separa **los dos ceros del canon** (§6):
        `False` es «nadie registró nada» y `True` con `pesos = 0` es un cero
        medido. `cpk_de` ya lo devolvía y esta función lo tiraba.

        El mes se calcula con `dia_operativo()`: el 31 a las 8 p.m. de Colombia,
        `date.today()` en Railway ya es del mes siguiente y esto reportaría «el
        mes» sobre un solo día.
        """
        from app.models.vehiculo import Vehiculo
        from flota.adaptadores.gastos import cpk_de, numero_legible
        from flota.dominio.procedencia import Cifra, mes_en_curso_de
        from flota.dominio.valores import palabra_de_confianza

        if not _tabla_existe('flota_gasto'):
            return None
        hoy = _hoy()
        # La ventana la nombra el dominio. Escribir `hoy.replace(day=1)` acá y
        # otra vez en el próximo campo es cómo el mes del tablero y el mes del
        # expediente terminan siendo dos meses distintos.
        ventana = mes_en_curso_de(hoy)
        desde = ventana.desde

        salida = []
        # Mismo predicado que `vehiculos_activos()` y que `km_dia_por_vehiculo`:
        # la columna es nullable, y tres denominadores que se calculan distinto
        # hacen que el tablero diga «6 de 6» donde el health dice otra cosa.
        for v in (Vehiculo.query.filter(Vehiculo.activo.is_(True))
                  .order_by(Vehiculo.placa).all()):
            r = cpk_de(v.id, desde, hoy)
            # Regla 13 por construcción, no por disciplina: `Cifra` se niega a
            # existir sin base, sin ventana y sin `n`, y **se niega a llevar un
            # `sin_dato` sin motivo**. Escribir esas cinco claves a mano es lo
            # que se venía haciendo, y lo que se olvida en el campo número seis.
            #
            # `n` son las lecturas del tramo: un CPK sobre dos lecturas y uno
            # sobre veinte no se leen igual, y desde el número no se distinguen.
            cifra = Cifra(
                # El número **con sus dos insumos**: un CPK suelto no se puede
                # auditar, y éste va a un tablero.
                valor=numero_legible(r['cpk']),
                base=('gastos registrados contra el vehículo, con el de '
                      'período repartido por día calendario'),
                ventana=ventana,
                n=r['lecturas'],
                motivo=_motivo_cpk(r))
            salida.append({
                'placa': v.placa,
                # `clave_valor='cpk'`: el campo ya se publicaba con ese nombre y
                # la pantalla lo lee así. Renombrarlo a `valor` por uniformidad
                # rompería el expediente y sus tests por una razón estética.
                **cifra.a_json('cpk'),
                # `palabra_de_confianza` y no `str()`: hoy `costo_por_kilometro`
                # ya devuelve una cadena, así que `str()` es un no-op y esto NO
                # arregla un bug vivo. Se cambia porque el día que la marca
                # vuelva a ser un `Confianza`, `str()` publicaría
                # `'Confianza.DUDOSA'` — que es exactamente lo que pasó en
                # `km_dia_por_vehiculo` y para lo que existe esta función.
                'marca': palabra_de_confianza(r['marca']),
                'pesos': numero_legible(r['pesos']),
                'km': r['km'],
                'hubo_gastos': r['hubo_gastos'],
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
