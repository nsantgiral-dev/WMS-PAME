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
from typing import List, Optional

from sqlalchemy import inspect as _inspect

from app.extensions import db


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
        import os
        from urllib.parse import urlparse

        from app.services.connekta_gateway import connekta

        # Acceso directo, sin `getattr(..., default)`: los tres atributos se
        # fijan en `Connekta.__init__`. Si alguno desapareciera, esto tiene que
        # reventar — un default silencioso acá devuelve 'produccion' con datos
        # de QA, que es exactamente el escenario para el que existe el campo.
        host = urlparse(connekta.url_get_dinamico or '').netloc.lower()
        if any(x in host for x in ('qa', 'test', 'dev', 'pruebas')):
            return 'datos_de_prueba'
        ensayo_wms = os.getenv('WMS_ENSAYO')
        if ensayo_wms is not None and ensayo_wms.lower() == 'true':
            return 'ensayo'
        if connekta.modo_simulacion:
            return 'simulacion'
        if connekta.modo_ensayo:
            return 'ensayo'
        return 'produccion'

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

    # ── No medible todavía: la tabla llega con la tanda 1 ────────────────────

    def fichas_completas(self) -> Optional[int]:
        if not _tabla_existe('flota_ficha_tecnica'):
            return None
        raise NotImplementedError('fichas_completas — la tabla existe, falta la medición')

    def atributos_sin_dato(self) -> Optional[List[str]]:
        if not _tabla_existe('flota_ficha_tecnica'):
            return None
        raise NotImplementedError('atributos_sin_dato — la tabla existe, falta la medición')

    def vehiculos_sin_custodia_activa(self) -> Optional[int]:
        if not _tabla_existe('flota_custodia'):
            return None
        raise NotImplementedError('vehiculos_sin_custodia_activa — la tabla existe, falta la medición')

    def custodias_sin_foto_completa(self) -> Optional[int]:
        if not _tabla_existe('flota_custodia'):
            return None
        raise NotImplementedError('custodias_sin_foto_completa — la tabla existe, falta la medición')

    def fotos_pendiente_evidencia(self) -> Optional[int]:
        if not _tabla_existe('flota_foto'):
            return None
        raise NotImplementedError('fotos_pendiente_evidencia — la tabla existe, falta la medición')

    def documentos_vencidos(self) -> Optional[int]:
        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        raise NotImplementedError('documentos_vencidos — la tabla existe, falta la medición')

    def documentos_por_vencer_30d(self) -> Optional[int]:
        if not _tabla_existe('flota_documento_vehiculo'):
            return None
        raise NotImplementedError('documentos_por_vencer_30d — la tabla existe, falta la medición')


__all__ = ['MedidorSQL', '_TABLAS_TANDA_1']
