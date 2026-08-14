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
