"""
Dominio Consultas (GETs de catálogo/stock/pedidos/OC/CxC) del gateway
Connekta — extraído de ConnektaGateway (2026-09-09, paso 5 de la deuda de
tamaño; ver pasos 1-4: app/utils/siesa_formato.py,
connekta_circuit_breaker.py, connekta_compras_gateway.py,
connekta_ajustes_gateway.py).

Es el dominio más grande del gateway (29 métodos en el análisis original) y
sus métodos NO están contiguos en connekta_gateway.py — están intercalados
entre Traslados y NC/Liquidación. Por eso esta extracción avanza en
sub-lotes, no de un tirón: este archivo arranca con el clúster
bodegas/ubicaciones/stock (el más autocontenido), y sumará más métodos de
Consultas en pasos siguientes sin tocar los ya movidos.

**No recibe config propia** — mismo patrón que los pasos anteriores: toma
la instancia completa de `ConnektaGateway` como `core`. `ConnektaGateway`
conserva cada método como delegado delgado; el singleton `connekta` y sus
callers no cambian. Confirmado con `tests/test_alertas_dedup.py`, que
mockea `core._get` y llama `gw._fetch_stock_pages(...)` directo — como
`core` es la MISMA instancia (no una copia), el mock se ve igual.
"""
import logging

logger = logging.getLogger(__name__)


class ConnektaConsultasGateway:

    # Límite de página de Connekta para API_v2_Inventarios_InvFecha.
    # tamPag=120+ devuelve alerta "registros exceden el permitido"; 100 es el máximo seguro.
    _CONNEKTA_MAX_TAM_PAG = 100
    # Páginas paralelas por bodega — 3 es seguro sin riesgo de 429 (backoff=5min).
    _STOCK_BATCH_SIZE = 3

    def __init__(self, core):
        self._core = core

    def get_bodegas_siesa(self):
        """API_v2_Bodegas (ID 2) — lista todas las bodegas configuradas en Siesa.
        Usar para descubrir los IDs de bodega de los puntos de venta sin esperar a Siesa."""
        return self._core._get('API_v2_Bodegas', {
            'paginacion': 'numPag=1|tamPag=200'
        })

    def get_ubicaciones_siesa(self, bodega_id: str = None, pagina: int = 1):
        """
        API_v2_Ubicaciones (ID 43) — maestro de ubicaciones auxiliares de una bodega.

        Campos certificados (PDF oficial Connekta):
          f150_id          → código de bodega           (string, ej. 'NB1')
          f155_id_cia      → id empresa ERP             (number)
          f155_id          → código de la ubicación     (string, ej. 'PIK-01-A')
          f155_descripcion → descripción de la ubicación (string)
          f155_ind_estado  → 0=Inactivo, 1=Activo       (number)

        IMPORTANTE: La API no expone stock_minimo/stock_maximo — esos campos
        se configuran directamente en el WMS (tabla ubicaciones).

        El tipo_zona se deduce del prefijo del código:
          PIK* → PICKING   |   RES* → RESERVA   |   resto → GENERAL
        """
        import os

        from app.services.siesa_filtro import lit as _lit

        api_name = os.getenv('CONNEKTA_API_UBICACIONES', 'API_v2_Ubicaciones')
        params: dict = {'paginacion': f'numPag={pagina}|tamPag=100'}
        if bodega_id:
            params['parametros'] = f"f150_id = {_lit(bodega_id)}"
        return self._core._get(api_name, params)

    # transferir_entre_ubicaciones() (conector 173066 para RESERVA→PICKING) se
    # retiró 2026-09-07: ambas ubicaciones son la misma bodega Siesa (NB1 no
    # tiene sub-bodegas para picking/reserva), así que no había ningún
    # documento real que declarar. Probado en vivo contra Siesa QA el mismo
    # día: rechazado por tamaño de registro (2658 vs 2700 exigidos) — nunca
    # se había ejercido contra Siesa real antes de esa prueba, pese al
    # comentario que afirmaba lo contrario. transferencia_directa() (en
    # ConnektaGateway) es la función hermana que SÍ sigue en uso — traslados
    # reales inter-bodega, donde origen y destino son bodegas Siesa distintas.

    def get_stock_bodega(self, bodega_id: str):
        """API_v2_Inventarios_InvFecha — existencia real en una bodega específica.

        La API de Connekta usa paginación offset-based que no es determinística
        bajo requests concurrentes: filas se barajan entre páginas, causando que
        productos se dupliquen en una página y desaparezcan de otra.

        Estrategia: dos pasadas paralelas + merge por f120_id para maximizar
        cobertura. La probabilidad de que el mismo producto falte en AMBAS
        pasadas es despreciable (~0.01%)."""
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        if core.modo_simulacion:
            return core._get(core.api_inventario, {
                'paginacion': 'numPag=1|tamPag=3',
                'parametros': f"f150_id = {_lit(bodega_id)} AND f400_cant_existencia_1 > 0"
            })

        rows_pass1 = self._fetch_stock_pages(bodega_id)
        by_id = {r.get('f120_id'): r for r in rows_pass1}
        dupes = len(rows_pass1) - len(by_id)

        if dupes > 0:
            logger.warning(
                '[CONNEKTA] stock %s pass1: %d filas, %d duplicados por f120_id — '
                'paginación inestable, ejecutando segunda pasada',
                bodega_id, len(rows_pass1), dupes,
            )
            rows_pass2 = self._fetch_stock_pages(bodega_id)
            for r in rows_pass2:
                fid = r.get('f120_id')
                if fid not in by_id:
                    by_id[fid] = r
            logger.info(
                '[CONNEKTA] stock %s merge: %d únicos (pass1=%d, pass2=%d, nuevos=%d)',
                bodega_id, len(by_id), len(rows_pass1), len(rows_pass2),
                len(by_id) - (len(rows_pass1) - dupes),
            )
        else:
            logger.info('[CONNEKTA] stock %s: %d filas, 0 duplicados — paginación OK', bodega_id, len(by_id))

        return {'detalle': {'Table': list(by_id.values())}}

    def _fetch_stock_pages(self, bodega_id: str):
        """Una pasada completa de paginación paralela (batch=3)."""
        from concurrent.futures import ThreadPoolExecutor

        from app.services.connekta_gateway import ConnektaPaginacionError, _exigir_datos
        from app.services.siesa_filtro import lit as _lit

        core = self._core
        tam = self._CONNEKTA_MAX_TAM_PAG
        batch = self._STOCK_BATCH_SIZE
        all_rows = []
        pag = 1

        while pag <= 200:
            pages_in_batch = list(range(pag, min(pag + batch, 201)))

            def _fetch(p, _bod=bodega_id, _tam=tam):
                try:
                    res = core._get(core.api_inventario, {
                        'paginacion': f'numPag={p}|tamPag={_tam}',
                        'parametros': f"f150_id = {_lit(_bod)} AND f400_cant_existencia_1 > 0"
                    })
                    # `_get` devuelve None cuando el circuit breaker bloquea o
                    # la respuesta no es 200. Sin esto, el `res.get('_error')`
                    # de abajo reventaba con AttributeError sobre None.
                    if res is None:
                        return {'_error': True, 'motivo': 'sin respuesta',
                                'pagina': p, 'detalle': {'Table': []}}
                    return res
                except Exception as e:
                    return {'_error': True, 'motivo': str(e)[:120],
                            'pagina': p, 'detalle': {'Table': []}}

            with ThreadPoolExecutor(max_workers=batch) as ex:
                batch_results = list(ex.map(_fetch, pages_in_batch))

            done = False
            for res in batch_results:
                if res.get('_error'):
                    # NO se sigue de largo. Saltar una página deja su stock
                    # afuera del resultado, y el llamador recibe un inventario
                    # INCOMPLETO que no se distingue de uno completo: los
                    # productos de esa página aparecen como si no existieran.
                    #
                    # Antes esto era `continue` a secas — sin log, sin contador
                    # y sin abortar. Una consulta de diagnóstico que descarta
                    # páginas en silencio informa lo contrario de lo que pasó.
                    logger.error(
                        '[CONNEKTA] stock %s: página %s falló (%s) — se aborta '
                        'la consulta: un inventario parcial que parece completo '
                        'es peor que un error',
                        bodega_id, res.get('pagina', '?'),
                        res.get('motivo', 'sin motivo'))
                    raise ConnektaPaginacionError(
                        f'La consulta de stock de {bodega_id} falló en la página '
                        f'{res.get("pagina", "?")}: {res.get("motivo", "")}. '
                        f'No se devuelve un inventario parcial.'
                    )
                rows = res.get('detalle', {}).get('Table', [])
                # Este módulo ya se niega a devolver un inventario parcial
                # (`ConnektaPaginacionError`, justo arriba). Un rechazo de
                # filtro leído como «se acabaron las páginas» abría el mismo
                # agujero por la otra puerta.
                _exigir_datos(rows, '_fetch_stock_pages',
                              f'bodega={bodega_id}')
                if not rows:
                    done = True
                    break
                all_rows.extend(rows)
                if len(rows) < tam:
                    done = True
                    break

            if done:
                break
            pag += batch

        return all_rows


__all__ = ['ConnektaConsultasGateway']
