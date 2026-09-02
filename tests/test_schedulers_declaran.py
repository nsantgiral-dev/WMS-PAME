"""
Lo que arrancó de verdad, no lo que la variable dice.

`/api/health/siesa` publica `schedulers.activos` para contestar exactamente
esa pregunta — CLAUDE.md lo dice así: *«un import que revienta deja la variable
en `true` y el cron sin correr»*.

**Y contestaba que sí a todo.** El registro era

    getattr(mod, 'init_scheduler')(app)
    app.config['SCHEDULERS_ACTIVOS'].append(tag)

es decir, agregaba a la lista con solo **no lanzar excepción**. Y ninguno lanza
cuando falta APScheduler: los trece hacen `except ImportError: log; return None`.

En un proceso sin APScheduler —un build de Railway donde la dependencia no
quedó instalada— los doce se reportaban activos y **ninguno corría**: DLQ, sync
de pedidos, alertas por correo, conteo cíclico ABC. Todo apagado, tablero en
verde.

Es la forma exacta de `trinquete-que-mide-una-proxy`: el instrumento medía «la
llamada no reventó» en vez de «el cron quedó programado». Y el instrumento era
justamente el que existía para detectar este fallo.

## Los dos errores, y por qué hacen falta los dos tests

Mirar el retorno arregla el falso positivo. Pero introduce el inverso: un
`init_scheduler` que no devuelve nada al tener éxito se reporta omitido para
siempre, y una alarma que siempre suena es una alarma apagada. Por eso el
primer test exige la convención por AST.
"""
import ast
import pathlib

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parent.parent

#: Los que `app/__init__.py` registra. Se declara acá a propósito: si alguien
#: agrega un scheduler y no lo registra, este archivo no lo cubre y el segundo
#: test lo caza.
REGISTRADOS = {
    'siesa_job_service': 'init_scheduler',
    'pedidos_sync_service': 'init_scheduler',
    'vigia_service': 'init_scheduler',
    'siesa_sync_service': 'init_scheduler',
    'siesa_barcode_sync_service': 'init_scheduler',
    'empaques_sync_service': 'init_scheduler',
    'ubicaciones_sync_service': 'init_scheduler',
    'reconciliacion_service': 'init_scheduler',
    'traslado_service': 'init_scheduler',
    'alertas_service': 'init_scheduler',
    'traslado_monitor_service': 'init_scheduler',
    'reposicion_service': 'init_scheduler',
    'abc_service': 'init_scheduler',
    'inventario_siesa_service': 'iniciar_refresh_periodico',
}

#: Fuera de `app/services/`. El barrido de vencimientos de flota vivía sin
#: cron: su único caller en producción era el botón «Revisar vencimientos
#: ahora», así que el aviso de SOAT dependía de que alguien se acordara.
REGISTRADOS_EXTERNOS = {
    'flota.adaptadores.avisos': 'init_scheduler',
}


def _funcion(modulo: str, nombre: str):
    arbol = ast.parse((_RAIZ / 'app' / 'services' / f'{modulo}.py').read_text())
    for n in ast.walk(arbol):
        if isinstance(n, ast.FunctionDef) and n.name == nombre:
            return n
    return None


class TestTodosDevuelvenSuScheduler:
    """La convención, por AST. Un `grep` acá se atraparía en los docstrings —
    ya pasó cinco veces esta semana."""

    @pytest.mark.parametrize('modulo,fn', sorted(REGISTRADOS.items()))
    def test_devuelve_algo_al_tener_exito(self, modulo, fn):
        f = _funcion(modulo, fn)
        assert f is not None, f'{modulo}.{fn} no existe'
        con_valor = [
            r for r in ast.walk(f)
            if isinstance(r, ast.Return) and r.value is not None
            and not (isinstance(r.value, ast.Constant) and r.value.value is None)
        ]
        assert con_valor, (
            f'{modulo}.{fn} no devuelve nada al tener éxito. El registro usa el '
            f'retorno para distinguir «arrancó» de «la llamada no reventó», así '
            f'que esto se reportaría OMITIDO siempre — y una alarma que suena '
            f'siempre es una alarma apagada.')


class TestElRegistroNoMiente:
    """Comportamiento, no forma: se ejerce `_registrar_scheduler` de verdad."""

    def _app_falsa(self):
        class _App:
            config = {'SCHEDULERS_ACTIVOS': [], 'SCHEDULERS_OMITIDOS': []}
        return _App()

    def _il_falso(self, retorno=None, revienta=False):
        class _Mod:
            @staticmethod
            def init_scheduler(app, **kw):
                if revienta:
                    raise RuntimeError('boom')
                return retorno

        class _IL:
            @staticmethod
            def import_module(_):
                return _Mod
        return _IL

    def test_un_scheduler_que_devuelve_none_no_se_declara_activo(self):
        """El caso del ImportError silencioso: `return None` sin excepción."""
        import logging

        from app import _registrar_scheduler
        app = self._app_falsa()
        _registrar_scheduler(app, self._il_falso(retorno=None), logging.getLogger(),
                             'x', 'init_scheduler', '[X]')
        assert app.config['SCHEDULERS_ACTIVOS'] == [], (
            'un scheduler que no arrancó se declaró ACTIVO — es la regresión '
            'exacta: la lista mide que la llamada no reventó')
        assert len(app.config['SCHEDULERS_OMITIDOS']) == 1
        assert 'NO está programado' in app.config['SCHEDULERS_OMITIDOS'][0]

    def test_uno_que_arranca_si_se_declara(self):
        import logging

        from app import _registrar_scheduler
        app = self._app_falsa()
        _registrar_scheduler(app, self._il_falso(retorno=object()), logging.getLogger(),
                             'x', 'init_scheduler', '[X]')
        assert app.config['SCHEDULERS_ACTIVOS'] == ['[X]']
        assert app.config['SCHEDULERS_OMITIDOS'] == []

    def test_una_excepcion_tambien_se_declara(self):
        """Un import que revienta no puede quedar solo en el log."""
        import logging

        from app import _registrar_scheduler
        app = self._app_falsa()
        _registrar_scheduler(app, self._il_falso(revienta=True), logging.getLogger(),
                             'x', 'init_scheduler', '[X]')
        assert app.config['SCHEDULERS_ACTIVOS'] == []
        assert 'boom' in app.config['SCHEDULERS_OMITIDOS'][0]


class TestNingunSchedulerQuedaFueraDelRegistro:
    """El hueco que destapó todo esto.

    `ABCService.init_scheduler` y `iniciar_refresh_periodico` se registraban
    **fuera del bucle**, con un `try/except` que solo logueaba: no entraban ni
    en ACTIVOS ni en OMITIDOS. El endpoint que existe para decir qué está
    corriendo tenía dos puntos ciegos, y uno era el conteo cíclico — y **nadie
    reclama un conteo que no se pidió**.
    """

    def test_todo_init_scheduler_del_repo_esta_registrado(self):
        encontrados = set()
        for p in (_RAIZ / 'app' / 'services').glob('*.py'):
            arbol = ast.parse(p.read_text())
            for n in ast.walk(arbol):
                if isinstance(n, ast.FunctionDef) and n.name in (
                        'init_scheduler', 'iniciar_refresh_periodico'):
                    encontrados.add(p.stem)
        sin_registrar = encontrados - set(REGISTRADOS)
        assert not sin_registrar, (
            f'definen un scheduler y nadie los registra: {sorted(sin_registrar)}. '
            f'Un cron que no se registra no falla — no existe, y su silencio se '
            f'lee igual que «no hubo nada que hacer».')

    def test_el_registro_pasa_por_el_helper_y_no_a_mano(self):
        """Por AST. Un `try: X.init_scheduler(app)` suelto vuelve a crear un
        punto ciego, que es exactamente como estaban los dos de arriba."""
        arbol = ast.parse((_RAIZ / 'app' / '__init__.py').read_text())
        sueltas = [
            n.lineno for n in ast.walk(arbol)
            if isinstance(n, ast.Call)
            and (getattr(n.func, 'attr', None) in ('init_scheduler',
                                                   'iniciar_refresh_periodico'))
        ]
        assert not sueltas, (
            f'hay schedulers registrados a mano en app/__init__.py (líneas '
            f'{sueltas}) en vez de por `_registrar_scheduler`. Los registrados a '
            f'mano no entran ni en ACTIVOS ni en OMITIDOS: son invisibles para '
            f'/api/health/siesa.')


class TestLosDeFueraDeServices:
    """`flota/` vive fuera de `app/`, y un `grep` acotado a `app/services` no lo
    ve — la misma trampa que documenta CLAUDE.md."""

    @pytest.mark.parametrize('mod,fn', sorted(REGISTRADOS_EXTERNOS.items()))
    def test_devuelve_su_scheduler(self, mod, fn):
        import ast

        ruta = _RAIZ / (mod.replace('.', '/') + '.py')
        arbol = ast.parse(ruta.read_text())
        f = next((n for n in ast.walk(arbol)
                  if isinstance(n, ast.FunctionDef) and n.name == fn), None)
        assert f is not None, f'{mod}.{fn} no existe'
        con_valor = [
            r for r in ast.walk(f)
            if isinstance(r, ast.Return) and r.value is not None
            and not (isinstance(r.value, ast.Constant) and r.value.value is None)
        ]
        assert con_valor, f'{mod}.{fn} no devuelve su scheduler'

    def test_el_barrido_de_flota_esta_registrado(self):
        """Tenía un solo caller —un botón— y ningún cron."""
        import ast

        arbol = ast.parse((_RAIZ / 'app' / '__init__.py').read_text())
        literales = {n.value for n in ast.walk(arbol)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert 'flota.adaptadores.avisos' in literales, (
            'el barrido de vencimientos volvió a quedar sin cron: el aviso de '
            'SOAT y tecnomecánica depende de que alguien apriete un botón, y la '
            'ventana son 15 días')
