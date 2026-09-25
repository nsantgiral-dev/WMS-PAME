"""
`GET /api/almacenes/` no listaba NS2, FP1 ni FF1 en QA (2026-09-25).

No es un filtro: el endpoint lista `almacenes` activos, y una bodega operada
existe como sede recién cuando alguien la usa (la primera recepción de OC de
tienda o el primer traslado desde ella crean su almacén). NS2 (parqueo de
licitaciones), FP1 y FF1 (ferias temporales) nunca se usaron. Ahora
`bodegas.almacenes_faltantes()` lo dice y `flask asegurar-almacenes --ejecutar`
los crea con la misma función del primer uso.
"""


def _nb1(db):
    from app.models.almacen import Almacen
    db.session.add(Almacen(codigo='NB1', nombre='CD', bodega_siesa_id='NB1', activo=True))
    db.session.commit()


class TestLasBodegasOperadasTienenSede:

    def test_faltan_las_que_nadie_uso(self, db):
        from app.services.bodegas import almacenes_faltantes
        _nb1(db)
        faltan = {f['bodega'] for f in almacenes_faltantes()}
        assert {'NS2', 'FP1', 'FF1'} <= faltan and 'NB1' not in faltan

    def test_el_simulacro_no_escribe(self, db):
        from app.models.almacen import Almacen
        from app.services.bodegas import asegurar_almacenes
        _nb1(db)
        r = asegurar_almacenes()
        assert r['creados'] == [] and Almacen.query.count() == 1

    def test_ejecutar_los_crea_con_su_co_y_salen_en_la_lista(self, client, db, jwt_token_admin):
        from app.models.almacen import Almacen
        from app.services.bodegas import almacenes_faltantes, asegurar_almacenes
        _nb1(db)
        asegurar_almacenes(ejecutar=True)
        assert almacenes_faltantes() == []
        assert Almacen.query.filter_by(bodega_siesa_id='NS2').one().centro_op_siesa == '001'
        r = client.get('/api/almacenes/', headers={'Authorization': f'Bearer {jwt_token_admin}'})
        bodegas = {a.get('bodega_siesa_id') for a in r.get_json()}
        assert {'NS2', 'FP1', 'FF1'} <= bodegas

    def test_uno_inactivo_no_se_reactiva_solo(self, db):
        from app.models.almacen import Almacen
        from app.services.bodegas import almacenes_faltantes, asegurar_almacenes
        _nb1(db)
        db.session.add(Almacen(codigo='FF1', nombre='Feria', bodega_siesa_id='FF1', activo=False))
        db.session.commit()
        asegurar_almacenes(ejecutar=True)
        assert Almacen.query.filter_by(bodega_siesa_id='FF1').count() == 1
        assert {'bodega': 'FF1', 'co': '009', 'estado': 'INACTIVO'} in almacenes_faltantes()
