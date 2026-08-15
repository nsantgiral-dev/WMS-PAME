"""
Qué flujos están vigilados — y, sobre todo, cuáles no.

Un auditor que cubre un flujo de seis se lee igual que uno que los cubre todos:
devuelve `0 hallazgos` para lo que no mira. Este archivo hace visible el
denominador.
"""
import pytest

from app.services import auditoria

#: Flujos con invariantes escritos. Esta lista **solo puede crecer**: si un
#: flujo desaparece de acá es que alguien borró sus invariantes, y eso tiene
#: que doler.
FLUJOS_CUBIERTOS = {'venta', 'traslados', 'conteo', 'devoluciones',
                    'recepcion', 'reposicion'}

#: Flujos del WMS que mueven inventario o dinero y **todavía no tienen
#: invariantes**. Escrito para que se vea, no para que se olvide.
SIN_CUBRIR = {
    # No mueve inventario, pero **sí registra quién responde por un camión de
    # seis toneladas**, y eso no lo vigila nadie. La auditoría del 2026-08-15
    # listó siete invariantes que faltan: cobertura sin huecos, un custodio por
    # vehículo, odómetro contra `km_inicial`, `km_fin` = `km_inicio` de la
    # siguiente, foto colgada de fila viva, `pendiente_sede` que no envejece, y
    # foto en `ok` con archivo en el volumen. Cualquier deriva se descubre el
    # día del siniestro.
    'flota': 'Custodia de vehículos. El paquete flota vive fuera de app/, no '
             'mueve inventario ni dinero, pero registra responsabilidad sobre '
             'un camión y hoy no tiene ningún invariante.',
    # **El motivo anterior era falso en los dos sentidos** y sobrevivió porque
    # el test solo exigía `len(v) > 20`. Tienda tiene DOS flujos: recibir
    # traslado (`traslado_service`, donde TRA-01 está ciego mientras tienda
    # mande el cuerpo vacío) y recibir OC (`recepcion_service`, que no tiene
    # nada que ver con traslados — lo dice el docstring de `routes/tienda_oc.py`).
    'tienda': 'DOS flujos: recibir traslado via traslado_service, con TRA-01 '
              'ciego mientras tienda escriba recibida=enviada, y recibir OC via '
              'recepcion_service. El riesgo propio no lo mira ningún invariante.',
    # El riesgo vive en el INSUMO, no en una frontera entre etapas:
    # `precios_realizados` vacía vuelve supuesto todo margen, y
    # `FichaImportacion`/`ItemEnTransito` no tienen un solo escritor.
    'compras_ia': 'Propuestas: armador_service y costo_service no ejecutan '
                  'movimientos de inventario. El riesgo es de insumo y no de '
                  'frontera entre etapas.',
    # La compuerta existe, pero mide la memoria de UN proceso: un dict de
    # módulo con `--workers=2`. Un POST que cae en el otro worker rechaza una
    # descarga que fue perfecta.
    'kardex': 'kardex_service reconstruye, no mueve. Su compuerta vive en un '
              'dict de proceso y con dos workers puede rechazar una descarga '
              'buena — es un guard de proceso, no de propiedad.',
    'vigia': 'vigia_service detecta corrimientos; su certificación es el arnés '
             'del canon, no un invariante de frontera. No mueve inventario ni '
             'dinero.',
}


class TestElDenominadorEsVisible:

    def test_los_flujos_cubiertos_siguen_estando(self):
        faltan = FLUJOS_CUBIERTOS - set(auditoria.flujos())
        assert not faltan, f'se perdieron invariantes de: {faltan}'

    def test_cada_flujo_tiene_al_menos_un_bloqueante(self):
        """Un flujo con solo `OBSERVA` no vigila: informa."""
        for f in FLUJOS_CUBIERTOS:
            inv = auditoria.registrados(f)
            assert any(i.severidad == auditoria.BLOQUEA for i in inv), f

    def test_todo_invariante_declara_su_consecuencia(self):
        """En términos de operación, no de código: quien lee el reporte no
        necesariamente sabe qué es un `producto_id`."""
        for i in auditoria.registrados():
            assert i.consecuencia and len(i.consecuencia) > 40, i.codigo
            assert i.frontera, i.codigo

    def test_los_codigos_no_se_repiten(self):
        codigos = [i.codigo for i in auditoria.registrados()]
        assert len(codigos) == len(set(codigos))

    def test_lo_que_falta_esta_escrito(self):
        """El punto del archivo. Si alguien agrega un flujo nuevo sin
        invariantes, tiene que decidirlo a propósito: o lo cubre, o lo declara
        acá con el motivo."""
        assert SIN_CUBRIR
        assert all(len(v) > 20 for v in SIN_CUBRIR.values())

    def test_cada_motivo_nombra_algo_que_existe(self):
        """`len(v) > 20` deja pasar cualquier prosa, y por eso sobrevivió un
        motivo falso: «tienda se apoya en traslados» —cuando también se apoya en
        `recepcion_service`, y del lado de traslados el invariante está ciego—.

        Un motivo que no se puede contrastar contra el código no es una
        declaración: es una frase.

        **Incluye `flota/`, que vive fuera de `app/`.** La primera versión de
        este test solo miraba `app/services` y rebotó el motivo de flota por
        eso — la misma trampa que CLAUDE.md advierte.
        """
        import pathlib as _p
        import re

        servicios = {f.stem for f in _p.Path('app/services').glob('*.py')}
        servicios |= {f.stem for f in _p.Path('flota').rglob('*.py')}
        servicios |= {'flota', 'app'}
        universo = servicios | set(auditoria.flujos()) | FLUJOS_CUBIERTOS

        for flujo, motivo in SIN_CUBRIR.items():
            citados = set(re.findall(r'[a-z_]+_service|\b[a-z_]{4,}\b', motivo))
            assert citados & universo, (
                f'el motivo de «{flujo}» no nombra ningún servicio ni flujo que '
                f'exista en el código: {motivo!r}. Un motivo que no se puede '
                f'contrastar es una frase, y así sobrevivió el de tienda.')

    def test_ningun_flujo_declarado_sin_cubrir_tiene_invariantes(self):
        """Coherencia entre las dos listas: si alguien cubre un flujo y olvida
        sacarlo de `SIN_CUBRIR`, la lista miente sobre lo que falta."""
        solapan = SIN_CUBRIR.keys() & set(auditoria.flujos())
        assert not solapan, f'ya están cubiertos y siguen listados como pendientes: {solapan}'


class TestTodosCorrenSobreUnaBaseVacia:
    """Sin datos no debe romperse nada ni inventarse hallazgos.

    Es el estado de una instalación recién desplegada, y es cuando alguien va a
    abrir el panel por primera vez.
    """

    @pytest.mark.parametrize('flujo', sorted(FLUJOS_CUBIERTOS))
    def test_sin_datos_no_hay_hallazgos_ni_errores(self, app, db, flujo):
        r = auditoria.auditar(flujo)
        assert r['errores'] == []
        assert r['hallazgos_totales'] == 0
        assert r['invariantes_corridos'] > 0


class TestElUniversoMiradoSeDeclara:
    """«0 hallazgos» puede significar «dejó de buscar».

    Las **veinte** consultas del módulo traían `.limit()` sin `order_by`, y el
    tope corta ANTES de que los invariantes filtren —el predicado corre en
    Python sobre lo que ya volvió—. Así que en cuanto un flujo pasaba el
    límite, la auditoría dejaba de ver las violaciones nuevas: el tope se
    llenaba con las filas más VIEJAS, que es lo contrario de lo que una
    auditoría necesita.

    `truncado` no cubría esto: cuenta HALLAZGOS. Son dos truncamientos
    distintos y uno estaba invisible.
    """

    def test_el_reporte_declara_las_consultas_truncadas(self, db):
        r = auditoria.auditar()
        assert 'consultas_truncadas' in r, (
            'el reporte dejó de declarar si la consulta llegó al tope — «0 '
            'hallazgos» vuelve a ser indistinguible de «no se miró todo»')

    def test_una_corrida_no_arrastra_el_tope_de_la_anterior(self, db):
        from app.services.auditoria.base import _AUDITORIA_TRUNCADA
        _AUDITORIA_TRUNCADA.add('sucio:1')
        assert 'sucio:1' not in auditoria.auditar()['consultas_truncadas']

    def test_toda_consulta_limitada_ordena(self):
        """Por AST sobre los seis módulos."""
        import ast
        import pathlib

        sin_orden = []
        for ruta in pathlib.Path('app/services/auditoria').glob('*.py'):
            arbol = ast.parse(ruta.read_text())
            for n in ast.walk(arbol):
                if (isinstance(n, ast.Call)
                        and getattr(n.func, 'attr', None) == 'limit'
                        and 'order_by' not in ast.dump(n)):
                    sin_orden.append(f'{ruta.name}:{n.lineno}')
        assert not sin_orden, (
            f'consultas con `.limit()` y sin `order_by`: {sin_orden}. El tope se '
            f'llena con lo más viejo y las violaciones nuevas quedan fuera.')
