"""
Toda tabla tiene que estar clasificada antes del acta de corte.

`reset_transaccional.py` es *deny-by-default*: **solo vacía lo que está en
`OPERATIVAS`**. Eso protege la memoria analítica, y tiene una consecuencia que
no se ve: una tabla que no está en ninguna lista **sobrevive al corte con los
datos del ensayo**.

Cinco tablas estaban así el 2026-08-14, y dos eran `devoluciones_cliente` y
`lineas_devolucion_cliente` — justo las tres devoluciones de prueba del 28 de
julio que la auditoría de flujo venía reportando. Después del corte habrían
seguido ahí, reportándose para siempre como si fueran operación real.

## Y una peor: la clave foránea

`devoluciones_cliente` apunta a `tareas_packing` y a `recaudos_entrega`, que sí
se vacían. Sin borrarla antes, ese `DELETE` falla por FK — y el `except` del
bucle lo imprime como un aviso entre otros.

Es exactamente el tropiezo que ya costó una vez con `flota_lectura_odometro` y
que está documentado en el propio script. **Una tabla nueva sin clasificar lo
reintroduce**, y el día del corte es el peor momento para descubrirlo: es el
día en que alguien improvisa un DELETE a mano, que es lo que este script existe
para evitar.
"""
import pathlib
import re

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'reset_transaccional.py'


def _lista(nombre: str) -> set:
    src = _SCRIPT.read_text(encoding='utf-8')
    i = src.find(nombre)
    assert i != -1, f'no se encontró {nombre}'
    cierre = '\n}\n' if '{' in src[i:i + 80] else '\n]\n'
    return set(re.findall(r"'([a-z_0-9]+)'", src[i:src.find(cierre, i)]))


@pytest.fixture(scope='module')
def clasificacion():
    return {
        'operativas': _lista('OPERATIVAS = ['),
        'analiticas': _lista('PROTEGIDAS_ANALITICAS = {'),
        'maestras': _lista('PROTEGIDAS_MAESTRAS = {'),
    }


class TestNingunaTablaQuedaSinClasificar:

    def test_toda_tabla_esta_en_alguna_lista(self, app, clasificacion):
        from app.extensions import db
        with app.app_context():
            todas = set(db.metadata.tables.keys())
        conocidas = set().union(*clasificacion.values()) | {'alembic_version'}
        huerfanas = sorted(todas - conocidas)
        assert not huerfanas, (
            f'\n{len(huerfanas)} tabla(s) sin clasificar: {huerfanas}\n\n'
            f'El reset es deny-by-default: lo que no está en OPERATIVAS '
            f'**sobrevive al corte con los datos del ensayo**.\n'
            f'Decidí para cada una: se vacía (OPERATIVAS) o se conserva '
            f'(PROTEGIDAS_ANALITICAS / PROTEGIDAS_MAESTRAS), con el motivo.')

    def test_ninguna_tabla_esta_en_dos_listas(self, clasificacion):
        op, an, ma = (clasificacion['operativas'], clasificacion['analiticas'],
                      clasificacion['maestras'])
        assert not (op & an), f'operativa y analítica a la vez: {op & an}'
        assert not (op & ma), f'operativa y maestra a la vez: {op & ma}'


class TestElOrdenRespetaLasClavesForaneas:
    """Hijos antes que padres. Si no, el `DELETE` falla y el bucle lo traga."""

    def test_cada_tabla_se_borra_antes_que_aquellas_a_las_que_apunta(
            self, app, clasificacion):
        from app.extensions import db
        op = [t for t in re.findall(r"'([a-z_0-9]+)'",
                                    _SCRIPT.read_text(encoding='utf-8')
                                    .split('OPERATIVAS = [')[1].split('\n]\n')[0])]
        pos = {t: i for i, t in enumerate(op)}
        with app.app_context():
            problemas = []
            for nombre in op:
                tabla = db.metadata.tables.get(nombre)
                if tabla is None:
                    continue
                for col in tabla.columns:
                    for fk in col.foreign_keys:
                        padre = fk.column.table.name
                        if padre in pos and pos[padre] < pos[nombre]:
                            problemas.append(
                                f'{nombre} (pos {pos[nombre]}) apunta a {padre} '
                                f'(pos {pos[padre]}) — el padre se borra primero')
        assert not problemas, '\n' + '\n'.join(problemas)


class TestElCorteAfirmaQueCorto:
    """Un script que dice «RESET COMPLETO» con tablas llenas es peor que uno
    que falla: da permiso para arrancar."""

    def test_las_sobrantes_impiden_declarar_exito(self):
        fuente = _SCRIPT.read_text(encoding='utf-8')
        assert 'corto_de_verdad' in fuente
        assert 'if ok and corto_de_verdad:' in fuente, (
            'el éxito volvió a depender solo de que la memoria sobreviviera')

    def test_la_memoria_analitica_sigue_protegida(self, clasificacion):
        """Lo que el corte NUNCA debe tocar. Sin las 26 semanas de referencia el
        CUSUM queda ciego ~6 meses."""
        for t in ('serie_vigia', 'alarma_vigia', 'kardex_movimientos',
                  'stock_diario', 'precios_realizados'):
            assert t in clasificacion['analiticas'], t
            assert t not in clasificacion['operativas'], t


class TestNadieVaciaUnaBaseSinNombrarla:
    """El script no verificaba **contra qué base** borraba.

    Tomaba `DATABASE_URL` y ejecutaba. Y el `DATABASE_URL` de una sesión de
    desarrollo apunta a la base de **producción** en Railway — comprobado el
    2026-08-14: `metro.proxy.rlwy.net/railway`, 51.808 filas.

    Con eso, un `--ejecutar` recuperado del historial de la terminal vacía
    producción. No hace falta equivocarse: basta con repetir un comando.

    Por eso `--ejecutar` ya no alcanza: hay que **escribir el host**. Es el
    único gesto que no se puede hacer por inercia.
    """

    @staticmethod
    def _correr(*args):
        """Invoca el script de verdad, contra una base temporal descartable.

        Las dos primeras versiones de estos tests buscaban `_destino_confirmado`
        en el fuente. Quitar la LLAMADA dejaba la DEFINICIÓN intacta y el test
        seguía verde; moverla después de abrir la app, también. Es el quinto
        tropiezo igual del repo: un detector de texto no sabe si el código se
        ejecuta.

        Con un `DATABASE_URL` a un sqlite temporal, si el guard desapareciera el
        script correría contra una base vacía — sin daño, pero con otro código
        de salida.
        """
        import os
        import subprocess
        import sys
        import tempfile

        raiz = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            entorno = dict(os.environ)
            entorno['DATABASE_URL'] = f'sqlite:///{tmp}/corte.db'
            entorno['SYNC_SCHEDULER'] = 'false'
            return subprocess.run(
                [sys.executable, str(raiz / 'scripts' / 'reset_transaccional.py'),
                 *args],
                cwd=str(raiz), env=entorno, capture_output=True, text=True,
                timeout=120)

    def test_ejecutar_solo_no_alcanza(self):
        r = self._correr('--ejecutar')
        assert r.returncode == 2, (
            f'\n`--ejecutar` sin nombrar el destino NO fue bloqueado '
            f'(exit={r.returncode}).\n{r.stdout[-600:]}')
        assert 'confirmar-destino' in r.stdout

    def test_un_destino_que_no_coincide_se_rechaza(self):
        r = self._correr('--ejecutar', '--confirmar-destino', 'otra-base')
        assert r.returncode == 2
        assert 'no coincide' in r.stdout

    def test_el_simulacro_no_necesita_confirmacion(self):
        """El simulacro tiene que seguir siendo trivial de correr: es lo que
        alguien hace para decidir, y ponerle fricción lo empuja a saltárselo."""
        r = self._correr()
        assert r.returncode == 0, r.stdout[-600:]
        assert 'DESTINO' in r.stdout


    def test_el_destino_se_describe_sin_la_contrasena(self, monkeypatch, capsys):
        """El host y la base sí; las credenciales no.

        Se verifica EJECUTANDO con una URL que trae usuario y contraseña, y
        exigiendo que ninguno aparezca en lo que el script escribe. Buscar
        `u.password` en el fuente no sirve: `urlparse(url)` ya contiene la
        cadena `url)` y el test se atrapaba solo — el mismo tropiezo de
        siempre.
        """
        import argparse
        import importlib.util

        spec = importlib.util.spec_from_file_location('_rt3', _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        monkeypatch.setenv(
            'DATABASE_URL',
            'postgresql://usuario_secreto:CLAVE_SECRETA@host-real:5432/railway')
        host, base = mod._describir_destino()
        assert host == 'host-real' and base == 'railway'

        mod._destino_confirmado(argparse.Namespace(
            ejecutar=False, confirmar_destino=None))
        salida = capsys.readouterr().out
        assert 'CLAVE_SECRETA' not in salida
        assert 'usuario_secreto' not in salida
        assert 'host-real' in salida, 'el destino tiene que verse'

    @pytest.mark.parametrize('argv,esperado', [
        (['--ejecutar'], False),
        (['--ejecutar', '--confirmar-destino', 'base-que-no-es'], False),
        ([], True),
    ])
    def test_bloquea_salvo_que_el_destino_coincida(self, argv, esperado,
                                                   monkeypatch, capsys):
        import importlib.util
        spec = importlib.util.spec_from_file_location('_rt', _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        monkeypatch.setattr(mod, '_describir_destino',
                            lambda: ('host-real', 'railway'))

        import argparse
        args = argparse.Namespace(
            ejecutar='--ejecutar' in argv,
            confirmar_destino=(argv[argv.index('--confirmar-destino') + 1]
                               if '--confirmar-destino' in argv else None))
        assert mod._destino_confirmado(args) is esperado
        capsys.readouterr()

    def test_con_el_destino_correcto_deja_pasar(self, monkeypatch):
        import argparse
        import importlib.util
        spec = importlib.util.spec_from_file_location('_rt2', _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, '_describir_destino', lambda: ('host-real', 'railway'))
        args = argparse.Namespace(ejecutar=True, confirmar_destino='host-real')
        assert mod._destino_confirmado(args) is True


# ═══════════════════════════════════════════════════════════════════════════
# Lo que sobrevive no puede apuntar a lo que se vacía (2026-09-24)
# ═══════════════════════════════════════════════════════════════════════════

def _script():
    import importlib.util
    spec = importlib.util.spec_from_file_location('_rt_fk', _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fks_de_protegidas_a_operativas(metadata, operativas, protegidas):
    """[(hija, columna, padre, ondelete, nullable)] — el escáner, aparte del
    veredicto, para que los meta-tests lo ejerciten sobre un esquema fabricado."""
    out = []
    for nombre in sorted(protegidas):
        tabla = metadata.tables.get(nombre)
        if tabla is None:
            continue
        for fk in tabla.foreign_keys:
            padre = fk.column.table.name
            if padre in operativas:
                out.append((nombre, fk.parent.name, padre,
                            (fk.ondelete or '').upper(), fk.parent.nullable))
    return out


def problemas_de_referencia(fks, declaradas):
    """Veredicto: una FK de protegida a operativa está resuelta si el esquema
    la suelta sola (`ON DELETE SET NULL` sobre columna nullable) o si el script
    la declara en REFERENCIAS_PROTEGIDAS con modo y motivo."""
    malos = []
    for hija, col, padre, ondelete, nullable in fks:
        if ondelete == 'SET NULL' and nullable:
            continue
        decl = declaradas.get((hija, col, padre))
        if decl is None:
            malos.append(f'{hija}.{col} → {padre} (ondelete={ondelete or "—"}, '
                         f'nullable={nullable}): sin resolver')
        elif decl[0] == 'desligar' and not nullable:
            malos.append(f'{hija}.{col} → {padre}: «desligar» sobre NOT NULL')
        elif decl[0] not in ('desligar', 'conservar'):
            malos.append(f'{hija}.{col} → {padre}: modo {decl[0]!r} desconocido')
    return malos


class TestLoProtegidoNoApuntaAlEnsayo:
    """El trinquete de orden solo cruzaba FKs **entre operativas**: hijos antes
    que padres. No veía la clase en que el hijo NO se borra.

    `eventos_stock_agotado` (PROTEGIDA_ANALITICA) tenía `tarea_picking_id` NOT
    NULL sin `ondelete` hacia `tareas_picking` (OPERATIVA): en Postgres el
    `DELETE FROM tareas_picking` del corte fallaba con un solo evento guardado.
    Y había otras tres, nullable, que hacían fallar igual el DELETE del padre.
    """

    def _todo(self, app, clasificacion):
        from app.extensions import db
        with app.app_context():
            return fks_de_protegidas_a_operativas(
                db.metadata, set(clasificacion['operativas']),
                clasificacion['analiticas'] | clasificacion['maestras'])

    def test_toda_referencia_de_lo_protegido_al_ensayo_esta_resuelta(
            self, app, clasificacion):
        malos = problemas_de_referencia(self._todo(app, clasificacion),
                                        _script().REFERENCIAS_PROTEGIDAS)
        assert not malos, (
            '\nUna tabla que el corte CONSERVA apunta a una que VACÍA. En '
            'Postgres el DELETE del padre falla y el corte queda a medias.\n'
            'Resolverla en el esquema (nullable + ondelete=SET NULL, y que la '
            'fila guarde el contexto que necesita) o declararla en '
            'REFERENCIAS_PROTEGIDAS con su modo y su porqué:\n  '
            + '\n  '.join(malos))

    def test_las_declaradas_existen_y_dicen_por_que(self, app, clasificacion):
        """Solo encoge: una declaración que ya no corresponde a ninguna FK es
        un «resuelto» que nadie verifica."""
        reales = {(h, c, p) for h, c, p, *_ in self._todo(app, clasificacion)}
        decl = _script().REFERENCIAS_PROTEGIDAS
        assert set(decl) <= reales, f'declaradas sin FK real: {set(decl) - reales}'
        assert all(len(razon) > 40 for _, razon in decl.values())

    def test_el_evento_de_agotado_la_resuelve_en_el_esquema(self, app, clasificacion):
        """El caso que originó la regla: no se desliga en el script, se arregla
        en el esquema (m034agotado) — el evento guarda su contexto."""
        fks = {(h, c): (o, n) for h, c, _p, o, n in self._todo(app, clasificacion)}
        assert fks[('eventos_stock_agotado', 'tarea_picking_id')] == ('SET NULL', True)

    def test_piso(self, app, clasificacion):
        """Si el escáner deja de ver FKs devuelve cero, y cero se lee «todo bien»."""
        assert len(self._todo(app, clasificacion)) >= 4


class TestElDetectorDeReferenciasMuerde:

    @staticmethod
    def _meta(ondelete=None, nullable=False):
        import sqlalchemy as sa
        md = sa.MetaData()
        sa.Table('op', md, sa.Column('id', sa.Integer, primary_key=True))
        sa.Table('prot', md, sa.Column('id', sa.Integer, primary_key=True),
                 sa.Column('op_id', sa.Integer,
                           sa.ForeignKey('op.id', ondelete=ondelete),
                           nullable=nullable))
        sa.Table('op2', md, sa.Column('id', sa.Integer, primary_key=True),
                 sa.Column('op_id', sa.Integer, sa.ForeignKey('op.id')))
        return md

    def test_ve_la_forma_rota(self):
        """NOT NULL sin ondelete: el caso de eventos_stock_agotado."""
        fks = fks_de_protegidas_a_operativas(self._meta(), {'op', 'op2'}, {'prot'})
        assert fks == [('prot', 'op_id', 'op', '', False)]
        assert problemas_de_referencia(fks, {})

    def test_nullable_sin_ondelete_tambien_rompe(self):
        fks = fks_de_protegidas_a_operativas(self._meta(nullable=True), {'op'}, {'prot'})
        assert problemas_de_referencia(fks, {})

    def test_no_marca_lo_sano(self):
        fks = fks_de_protegidas_a_operativas(
            self._meta('SET NULL', True), {'op', 'op2'}, {'prot'})
        assert problemas_de_referencia(fks, {}) == []
        # declarada con modo válido, también sana
        fks = fks_de_protegidas_a_operativas(self._meta(nullable=True), {'op'}, {'prot'})
        assert problemas_de_referencia(fks, {('prot', 'op_id', 'op'): ('desligar', 'x')}) == []

    def test_desligar_una_not_null_no_vale(self):
        fks = fks_de_protegidas_a_operativas(self._meta(), {'op'}, {'prot'})
        assert problemas_de_referencia(fks, {('prot', 'op_id', 'op'): ('desligar', 'x')})


class TestElCorteCorreConClavesForaneasDeVerdad:
    """Ejecuta `_vaciar` —lo que corre `--ejecutar`— sobre la base de tests con
    `PRAGMA foreign_keys=ON`, que es lo que Postgres hace siempre. Un detector
    del esquema dice que la FK está resuelta; esto dice que el corte corre."""

    def test_el_evento_sobrevive_y_el_ensayo_se_va(
            self, app, db, almacen, producto, ub_picking, usuario):
        from sqlalchemy import text
        from app.models.picking import TareaPicking
        from app.models.evento_stock_agotado import EventoStockAgotado
        from app.services.eventos_agotado_service import registrar_evento_agotado
        mod = _script()

        t = TareaPicking(codigo='TP-CORTE-1', producto_id=producto.id,
                         cantidad_solicitada=5, ubicacion_id=ub_picking.id,
                         almacen_id=almacen.id, estado='BLOQUEADO',
                         referencia_documento='PD-CORTE', tipo_documento='PEDIDO_SIESA')
        db.session.add(t)
        db.session.flush()
        registrar_evento_agotado(t, 3)
        rec_id = db.session.execute(text(
            "INSERT INTO recepciones (codigo, numero_oc_siesa, almacen_id, estado) "
            "VALUES ('REC-CORTE', 'OC-1', :a, 'ABIERTA') RETURNING id"),
            {'a': almacen.id}).scalar()
        db.session.execute(text(
            "INSERT INTO lpn (codigo, producto_id, factor_conversion, cantidad_actual, "
            "estado, recepcion_id) VALUES ('LPN-CORTE', :p, 1, 1, 'ACTIVO', :r)"),
            {'p': producto.id, 'r': rec_id})
        db.session.commit()

        db.session.execute(text('PRAGMA foreign_keys=ON'))
        try:
            assert db.session.execute(text('PRAGMA foreign_keys')).scalar() == 1
            errores = mod._vaciar(db.session)
            db.session.commit()
            propios = [(tb, str(e)) for tb, e in errores
                       if tb in ('tareas_picking', 'recepciones', 'lpn.recepcion_id')]
            assert not propios, propios

            assert db.session.execute(text('SELECT COUNT(*) FROM tareas_picking')).scalar() == 0
            assert db.session.execute(text('SELECT COUNT(*) FROM recepciones')).scalar() == 0
            ev = EventoStockAgotado.query.one()
            db.session.refresh(ev)
            assert ev.tarea_picking_id is None
            assert (ev.tarea_codigo, ev.cantidad_solicitada, ev.pedido_siesa_ref,
                    ev.cantidad_faltante) == ('TP-CORTE-1', 5, 'PD-CORTE', 3)
            assert db.session.execute(text(
                "SELECT recepcion_id FROM lpn WHERE codigo='LPN-CORTE'")).scalar() is None
        finally:
            db.session.rollback()
            db.session.execute(text('PRAGMA foreign_keys=OFF'))
            db.session.commit()

    def test_conservar_deja_en_pie_lo_que_una_protegida_referencia(self):
        mod = _script()
        donde = mod._donde_se_vacia('flota_foto')
        assert 'NOT IN (SELECT foto_id FROM flota_documento_vehiculo' in donde
        assert mod._donde_se_vacia('tareas_picking') == ''
