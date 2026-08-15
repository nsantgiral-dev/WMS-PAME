"""
Todo trabajo que se encola tiene que tener quién lo ejecute.

`SiesaJob.encolar('X', ...)` acepta cualquier string. `_ejecutar_job` despacha
con una cadena de `if job.tipo == '...'` y termina en:

    raise ValueError(f'Tipo de job no reconocido: {job.tipo}')

Productor y consumidor **no comparten una enumeración**, así que un tipo nuevo
compila, se encola, y falla en runtime. Y no falla rápido: el error es
determinista, así que la DLQ lo reintenta con backoff `[5, 15, 45, 120, 180]`
—unas 6 horas— antes de darlo por `FALLIDO`. Seis horas de una cola que nunca
iba a poder avanzar.

## El caso que lo motivó: `COMPROMISOS_RIT`

`traslado_service.py:400` encola un job de ese tipo para reintentar el 174720
(compromisos de la requisición). No existe rama que lo despache. El comentario
de al lado promete el reintento; **nunca ocurrió ni una vez**: los compromisos
no se disparan y el traslado queda sin reserva en Siesa.

Es «una función sin caller» al revés — un caller sin función.

## Cómo se detecta, y el error que este archivo ya cometió

Por AST, no por texto. La **primera** versión del detector solo miraba
argumentos posicionales (`encolar('TIPO', ...)`) y encontró 3 de 12: los otros
9 pasan el tipo como palabra clave (`encolar(tipo='TIPO', ...)`), y entre esos
9 estaba justamente el roto. Un detector que encuentra 3 de 12 devuelve «todo
bien» con la misma cara que uno que los ve todos.

Por eso `test_el_detector_no_esta_ciego` fija un piso: si la cuenta de tipos
encolados se desploma, es que el detector dejó de reconocer la forma —no que
alguien limpió la cola.
"""
import ast
from pathlib import Path

_RAIZ = Path(__file__).resolve().parents[1]
_APP = _RAIZ / 'app'

#: Tipos que se encolan y **nadie ejecuta**. Solo puede ENCOGER.
#:
#: La razón no es «qué handler le falta»: es qué operación queda sin hacer y
#: qué cuesta. Un job que nadie despacha no es un hueco de código, es una
#: promesa que la operación cree cumplida.
SIN_HANDLER = {
    # Vacío a propósito. `COMPROMISOS_RIT` vivió acá hasta el 2026-08-15 y ya
    # tiene handler: el reintento del 174720 que el encolado prometía y que
    # nunca corrió. Ver `_ejecutar_job`.
    #
    # Una entrada nueva acá no es un lugar donde aparcar trabajo: es una
    # promesa rota que se declara con su costo, mientras se decide entre
    # escribir el handler o borrar el encolado.
}


def _lit(nodo):
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    return None


def _tipos_encolados():
    """{tipo: [archivo:línea, ...]} — todo `encolar(...)` del árbol.

    Acepta el tipo como posicional **y** como palabra clave: los dos existen en
    el repo, y mirar solo uno fue el defecto de la primera versión.
    """
    encontrados = {}
    for py in sorted(_APP.rglob('*.py')):
        try:
            arbol = ast.parse(py.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if not isinstance(n, ast.Call):
                continue
            nombre = (n.func.attr if isinstance(n.func, ast.Attribute)
                      else n.func.id if isinstance(n.func, ast.Name) else '')
            if nombre != 'encolar' and not nombre.endswith('SiesaJob'):
                continue
            candidatos = list(n.args) + [k.value for k in n.keywords
                                         if k.arg == 'tipo']
            for c in candidatos:
                v = _lit(c)
                if v and v.isupper():
                    encontrados.setdefault(v, []).append(
                        f'{py.relative_to(_RAIZ)}:{n.lineno}')
                    break
    return encontrados


def _tipos_despachados():
    """Todo string comparado contra `<algo>.tipo` — la cadena del dispatcher."""
    despachados = set()
    for py in sorted(_APP.rglob('*.py')):
        try:
            arbol = ast.parse(py.read_text(encoding='utf-8'))
        except SyntaxError:
            continue
        for n in ast.walk(arbol):
            if not (isinstance(n, ast.Compare)
                    and isinstance(n.left, ast.Attribute)
                    and n.left.attr == 'tipo'):
                continue
            for c in n.comparators:
                v = _lit(c)
                if v:
                    despachados.add(v)
                elif isinstance(c, (ast.Tuple, ast.List, ast.Set)):
                    for e in c.elts:
                        if v := _lit(e):
                            despachados.add(v)
    return despachados


class TestTodoLoQueSeEncolaSeEjecuta:

    def test_ningun_tipo_nuevo_sin_handler(self):
        """EL GUARD. Un tipo nuevo sin rama rompe el build.

        Si esto falla: escribí la rama en `_ejecutar_job`, o —si el encolado
        sobra— borralo. Declararlo en `SIN_HANDLER` es la tercera opción y
        exige escribir qué operación queda sin hacer.
        """
        encolados = _tipos_encolados()
        huerfanos = set(encolados) - _tipos_despachados() - set(SIN_HANDLER)
        assert not huerfanos, (
            '\n%d tipo(s) de job se encolan y nadie los ejecuta:\n' % len(huerfanos)
            + '\n'.join(f'  · {t} — {encolados[t][0]}' for t in sorted(huerfanos))
            + '\n\nCaen en el raise genérico de _ejecutar_job, queman los 5 '
              'reintentos (~6 h) y mueren FALLIDO. La operación que los '
              'disparó cree que quedó hecha.')

    def test_la_lista_solo_encoge(self):
        """Anti-podredumbre: lo que ya tiene handler sale de la lista.

        Sin esto la deuda declarada se vuelve un cementerio que nadie limpia, y
        deja de proteger: un handler podría borrarse sin que nadie se entere.
        """
        ya_resueltos = sorted(set(SIN_HANDLER) & _tipos_despachados())
        assert not ya_resueltos, (
            f'ya tienen handler — borralos de SIN_HANDLER: {ya_resueltos}')

    def test_la_lista_no_acumula_encolados_muertos(self):
        """Si nadie encola ese tipo, la deuda tampoco existe."""
        fantasmas = sorted(set(SIN_HANDLER) - set(_tipos_encolados()))
        assert not fantasmas, (
            f'ya no se encolan — borralos de SIN_HANDLER: {fantasmas}')

    def test_cada_deuda_declara_lo_que_queda_sin_hacer(self):
        """Sin la consecuencia escrita en términos de operación, la lista es un
        olvido con formato."""
        for tipo, razon in SIN_HANDLER.items():
            assert razon and len(razon) > 80, (
                f'{tipo} no explica qué operación queda sin hacer')

    def test_el_detector_no_esta_ciego(self):
        """Un detector que deja de reconocer la forma pasa vacío para siempre.

        La primera versión miraba solo argumentos posicionales y encontró 3 de
        12 — y el roto estaba entre los 9 que no vio.
        """
        encolados = _tipos_encolados()
        assert len(encolados) >= 10, (
            f'solo se detectaron {len(encolados)} tipos encolados. O la cola '
            f'encogió a la mitad, o el detector dejó de reconocer la forma de '
            f'la llamada — revisar cuál antes de bajar este número.')
        assert len(_tipos_despachados()) >= 10, 'el dispatcher no se detectó'
