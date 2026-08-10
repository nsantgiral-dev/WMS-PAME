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
from datetime import timedelta as _timedelta
from typing import List, Optional

from sqlalchemy import inspect as _inspect

from app.extensions import db
from app.utils.fecha import dia_operativo

# Las ocho del recibo de turno: frontal, trasera, lateral izq, lateral der,
# cajón abierto, interior cabina, tablero, llantas.
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

        incompletas = 0
        for c in Custodia.query.all():
            if _cuantas('custodia_inicio', c.id) < FOTOS_POR_CUSTODIA:
                incompletas += 1
            elif c.fin_ts is not None and _cuantas('custodia_fin', c.id) < FOTOS_POR_CUSTODIA:
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


__all__ = ['MedidorSQL', '_TABLAS_TANDA_1', 'FOTOS_POR_CUSTODIA']
