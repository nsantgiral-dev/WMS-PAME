"""
Test 01 — Modelos: Ubicacion, LPN, ProductoEmpaque, SiesaJob, TareaReposicion.
Valida que los campos nuevos persisten y las propiedades calculadas son correctas.
"""
import pytest
from datetime import datetime, timedelta


class TestUbicacion:
    def test_campos_zona_persisten(self, db, almacen):
        from app.models.ubicacion import Ubicacion
        u = Ubicacion(
            codigo='PIK-05-B', almacen_id=almacen.id,
            tipo_zona='PICKING', stock_minimo=100, stock_maximo=500,
            secuencia_ruteo=3, activo=True,
        )
        db.session.add(u)
        db.session.commit()

        u2 = Ubicacion.query.filter_by(codigo='PIK-05-B').first()
        assert u2.tipo_zona == 'PICKING'
        assert u2.stock_minimo == 100
        assert u2.stock_maximo == 500
        assert u2.secuencia_ruteo == 3

    def test_es_picking_property(self, db, ub_picking):
        assert ub_picking.es_picking is True
        assert ub_picking.es_reserva is False

    def test_es_reserva_property(self, db, ub_reserva):
        assert ub_reserva.es_reserva is True
        assert ub_reserva.es_picking is False

    def test_general_no_es_picking_ni_reserva(self, db, ub_general):
        assert ub_general.es_picking is False
        assert ub_general.es_reserva is False

    def test_to_dict_incluye_campos_zona(self, db, ub_picking):
        d = ub_picking.to_dict()
        assert 'tipo_zona' in d
        assert 'stock_minimo' in d
        assert 'stock_maximo' in d
        assert 'secuencia_ruteo' in d
        assert d['tipo_zona'] == 'PICKING'

    def test_default_tipo_zona_es_general(self, db, almacen):
        from app.models.ubicacion import Ubicacion
        u = Ubicacion(codigo='SIN-ZONA', almacen_id=almacen.id, activo=True)
        db.session.add(u)
        db.session.commit()
        assert u.tipo_zona == 'GENERAL'


class TestLPN:
    def test_crear_lpn(self, db, lpn_activo):
        assert lpn_activo.codigo == 'LPN-0000001'
        assert lpn_activo.estado == 'ACTIVO'
        assert lpn_activo.cantidad_actual == 1240

    def test_consumir_lpn(self, db, lpn_activo):
        lpn_activo.consumir()
        db.session.commit()
        assert lpn_activo.estado == 'CONSUMIDO'
        assert lpn_activo.fecha_consumo is not None

    def test_generar_codigo_secuencial(self, db, producto, almacen, ub_reserva):
        from app.models.lpn import LPN
        cod1 = LPN.generar_codigo()
        lpn1 = LPN(codigo=cod1, producto_id=producto.id, almacen_id=almacen.id,
                   ubicacion_id=ub_reserva.id, factor_conversion=12,
                   cantidad_actual=12, estado='ACTIVO')
        db.session.add(lpn1)
        db.session.commit()

        cod2 = LPN.generar_codigo()
        assert cod2 != cod1
        assert cod2.startswith('LPN-')

    def test_buscar_activos_por_producto(self, db, lpn_activo, producto, almacen):
        from app.models.lpn import LPN
        activos = LPN.buscar_activos_por_producto(producto.id, almacen.id)
        assert len(activos) == 1
        assert activos[0].id == lpn_activo.id

    def test_to_dict_tiene_campos_esperados(self, db, lpn_activo):
        d = lpn_activo.to_dict()
        for campo in ['id', 'codigo', 'producto_id', 'estado', 'cantidad_actual',
                      'factor_conversion', 'almacen_id', 'ubicacion_id']:
            assert campo in d, f'Falta campo: {campo}'


class TestProductoEmpaque:
    def test_buscar_por_barcode(self, db, producto):
        from app.models.producto_empaque import ProductoEmpaque
        emp = ProductoEmpaque(
            producto_id=producto.id,
            referencia_item='PROD-001',
            codigo_barras='7701234567890',
            unidad_medida='CAJ',
            factor_conversion=12,
            activo=True,
        )
        db.session.add(emp)
        db.session.commit()

        encontrado = ProductoEmpaque.buscar_por_barcode('7701234567890')
        assert encontrado is not None
        assert encontrado.factor_conversion == 12

    def test_to_dict_incluye_producto_nombre(self, db, producto):
        from app.models.producto_empaque import ProductoEmpaque
        emp = ProductoEmpaque(
            producto_id=producto.id,
            referencia_item='PROD-001',
            codigo_barras='DUN14TEST',
            unidad_medida='PACA',
            factor_conversion=1240,
            activo=True,
        )
        db.session.add(emp)
        db.session.commit()

        d = emp.to_dict()
        assert d['producto_codigo'] == 'PROD-001'
        assert d['producto_nombre'] == 'Resma Carta 500h'
        assert d['factor_conversion'] == 1240


class TestSiesaJob:
    def test_encolar_job(self, db):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar(
            tipo='TRANSFERENCIA_UBICACIONES',
            payload={'bodega_id': 'NB1', 'cantidad': 100},
        )
        db.session.commit()

        assert job.id is not None
        assert job.estado == 'PENDIENTE'
        assert job.intentos == 0
        assert job.get_payload()['cantidad'] == 100

    def test_marcar_completado(self, db):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        db.session.commit()

        job.marcar_completado({'consecutivo': '999'})
        db.session.commit()

        assert job.estado == 'COMPLETADO'
        assert job.fecha_completado is not None

    def test_backoff_primer_fallo(self, db):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        db.session.commit()

        job.marcar_fallo('Connekta timeout')
        db.session.commit()

        assert job.estado == 'PENDIENTE'  # aún reintentable
        assert job.intentos == 1
        assert job.proximo_intento is not None
        # Backoff ~5 min
        diff = (job.proximo_intento - datetime.utcnow()).total_seconds()
        assert 200 < diff < 400, f'Backoff esperado ~5min, got {diff}s'

    def test_fallo_tras_max_intentos(self, db):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {'x': 1})
        db.session.commit()

        for _ in range(3):
            job.marcar_fallo('Error persistente')
        db.session.commit()

        assert job.estado == 'FALLIDO'
        assert job.intentos == 3

    def test_to_dict_campos_completos(self, db):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('TRANSFERENCIA_UBICACIONES', {},
                               referencia_tipo='TareaReposicion', referencia_id=7)
        db.session.commit()

        d = job.to_dict()
        assert d['tipo'] == 'TRANSFERENCIA_UBICACIONES'
        assert d['referencia_tipo'] == 'TareaReposicion'
        assert d['referencia_id'] == 7
        assert d['estado'] == 'PENDIENTE'


class TestTareaReposicion:
    def test_crear_tarea(self, db, producto, almacen, ub_reserva, ub_picking):
        from app.models.tarea_reposicion import TareaReposicion
        codigo = TareaReposicion.generar_codigo()
        t = TareaReposicion(
            codigo=codigo,
            producto_id=producto.id,
            almacen_id=almacen.id,
            cantidad_unidades=1240,
            ubicacion_reserva_id=ub_reserva.id,
            ubicacion_picking_id=ub_picking.id,
            estado='PENDIENTE',
        )
        db.session.add(t)
        db.session.commit()

        assert t.id is not None
        assert t.codigo.startswith('REP-')
        assert t.estado == 'PENDIENTE'

    def test_to_dict_campos_completos(self, db, producto, almacen,
                                       ub_reserva, ub_picking):
        from app.models.tarea_reposicion import TareaReposicion
        t = TareaReposicion(
            codigo='REP-0000001',
            producto_id=producto.id,
            almacen_id=almacen.id,
            cantidad_unidades=500,
            ubicacion_reserva_id=ub_reserva.id,
            ubicacion_picking_id=ub_picking.id,
        )
        db.session.add(t)
        db.session.commit()

        d = t.to_dict()
        assert d['tipo'] == 'REPOSICION'
        assert d['producto_codigo'] == 'PROD-001'
        assert d['ubicacion_reserva'] == 'RES-01-A'
        assert d['ubicacion_picking'] == 'PIK-01-A'
