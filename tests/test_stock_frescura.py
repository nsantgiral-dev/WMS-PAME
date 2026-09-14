"""
Frescura del stock mostrado en Pedir (tienda) — de dónde salió el número y
hace cuánto, no solo cuánto es.

Contexto (revisión 2026-08-27): la pantalla "Pedir" de tienda muestra
"Disponible" leyendo Siesa en vivo, con dos niveles de fallback silenciosos
si Siesa no responde — snapshot de `stock_siesa` en Postgres, y en último
caso stock físico WMS. La fórmula ya estaba verificada en vivo contra Siesa
(ver `docs/siesa/Ticket_Consultor_Siesa_Comprometido.md`), pero ningún
consumidor podía distinguir "esto es de Siesa ahora mismo" de "esto es lo
último que supimos" — la tienda podía armar un pedido sobre un número de
horas o días de antigüedad sin ninguna señal.

Al instrumentar esto apareció un bug independiente: `_guardar_stock_en_bd`
se llamaba incondicionalmente después de cada descarga, incluso cuando la
descarga vino degradada (Siesa no respondió y el propio `inventario_global`
es la misma fila que ya estaba en `stock_siesa`) — re-escribía `updated_at`
con `utcnow()` sobre un dato que seguía siendo tan viejo como antes. Mismo
patrón que ya se había corregido para el `ts` del cache en memoria
(ver comentario en `_descargar_inventario_siesa_raw`), un nivel más abajo.
"""
from datetime import datetime, timedelta

from app.models.stock_siesa import StockSiesa
from app.services import inventario_siesa_service as inv_service


class TestLeerStockDeBdDevuelveLaFilaMasVieja:

    def test_actualizado_en_es_el_minimo_no_el_maximo(self, db):
        vieja = datetime.utcnow() - timedelta(hours=5)
        nueva = datetime.utcnow()
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='A1', existencia=10, updated_at=vieja))
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='A2', existencia=20, updated_at=nueva))
        db.session.commit()

        inv, actualizado_en = inv_service._leer_stock_de_bd('NB1')

        assert set(inv.keys()) == {'A1', 'A2'}
        assert actualizado_en == vieja

    def test_bodega_sin_filas_devuelve_none(self, db):
        inv, actualizado_en = inv_service._leer_stock_de_bd('NB1')
        assert inv == {}
        assert actualizado_en is None


class TestGuardarStockEnBdNoResellaDatoDegradado:

    def test_degradado_no_escribe_ni_resella_updated_at(self, db):
        vieja = datetime.utcnow() - timedelta(days=2)
        db.session.add(StockSiesa(bodega='NB1', codigo_siesa='A1', existencia=10, updated_at=vieja))
        db.session.commit()

        # Simula el camino degradado: inventario_global es la misma BD leída de vuelta.
        inv_service._guardar_stock_en_bd({'NB1': {'A1': {
            'existencia': 10, 'comprometido': 0, 'salida_sin_conf': 0,
        }}}, degradado=True)

        fila = StockSiesa.query.filter_by(bodega='NB1', codigo_siesa='A1').first()
        assert fila.updated_at == vieja, (
            'degradado=True no debe re-sellar updated_at — sería un sello '
            'fresco sobre un dato que Siesa no confirmó esta corrida'
        )

    def test_no_degradado_si_escribe_y_actualiza(self, db):
        inv_service._guardar_stock_en_bd({'NB1': {'A1': {
            'existencia': 10, 'comprometido': 0, 'salida_sin_conf': 0,
        }}}, degradado=False)

        fila = StockSiesa.query.filter_by(bodega='NB1', codigo_siesa='A1').first()
        assert fila is not None
        assert fila.existencia == 10


class TestObtenerStockBodegaExponeMeta:

    def test_bd_snapshot_reporta_fuente_y_fecha_real(self, db):
        vieja = datetime.utcnow() - timedelta(hours=3)
        db.session.add(StockSiesa(bodega='NC1', codigo_siesa='B1', existencia=5, updated_at=vieja))
        db.session.commit()
        inv_service._cache_inventario_multibodega['data'] = None

        inv, meta = inv_service.obtener_stock_bodega('NC1')

        assert inv['B1']['existencia'] == 5
        assert meta['fuente'] == 'siesa_bd_snapshot'
        assert meta['actualizado_en'] == vieja.isoformat()

    def test_sin_dato_en_ningun_lado(self, db, monkeypatch):
        # No debe disparar el precalentamiento real (hilo de fondo contra Connekta)
        # solo para verificar el contrato de la respuesta cuando no hay nada.
        monkeypatch.setattr(inv_service, 'precalentar_cache_multibodega', lambda app=None: None)
        inv_service._cache_inventario_multibodega['data'] = None
        inv, meta = inv_service.obtener_stock_bodega('BODEGA_INEXISTENTE')

        assert inv == {}
        assert meta == {'fuente': 'sin_dato', 'actualizado_en': None}
