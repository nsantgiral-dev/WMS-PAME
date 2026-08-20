"""
El literal de un filtro de consulta dinámica de Siesa. **Una función.**

La Regla 15 dice que un string dentro de `parametros` va entre doble comilla
simple: `f120_referencia = ''ARTESA898''`. Eso se venía armando a mano en **36
sitios de 4 archivos**, con f-strings del tipo

    f"f120_referencia = ''{referencia}''"

y el saneamiento existía en **dos** de esos 36 — `.replace("'", "")` en
`buscar_barras_por_referencia` y `buscar_item_por_referencia`. La política
estaba escrita, era correcta, y no cubría a sus llamadores. Es la misma forma
que ya costó con el mapa bodega→CO, con la fórmula de retención y con el match
de cartera.

## Qué pasa si un valor trae una comilla

El valor termina el literal antes de tiempo y lo que sigue lo interpreta Siesa
como más filtro. En una consulta de **lectura** eso no borra nada, pero sí
puede **ensanchar el universo**: traer filas de otro ítem, de otra bodega o de
otro tercero. Y algunas de esas lecturas alimentan escrituras —
`get_inventario_fecha` da la existencia con la que el conteo cíclico calcula el
ajuste que se manda a Siesa por el 142951.

## Por qué levanta excepción y no limpia el valor

Quitar la comilla en silencio, que es lo que hacían los dos sitios saneados,
**cambia la pregunta sin decirlo**: se consulta por un código que no es el que
pidió el llamador y se devuelve un número que parece bueno. Un ajuste de
inventario calculado sobre la existencia del ítem equivocado no lo reclama
nadie — entra al ERP, cuadra el papel contra la realidad de otro SKU, y
reaparece meses después en el conteo físico.

Regla 0: ante dato que no se puede honrar, fallar hacia el lado conservador y
declararlo. Una consulta que revienta se ve; una que contesta de más, no.

El vacío se rechaza por lo mismo: `f120_referencia = ''''` no es «buscá el
ítem sin código», es un filtro que no filtra.
"""
import re

#: Comilla simple, comodines y caracteres de control.
#:
#: · `'` rompe el literal y deja lo que sigue como más filtro.
#: · `%` y `_` **son comodines de `LIKE`, que estos filtros aceptan** — y eso
#:   ensancha la consulta sin necesidad de ninguna comilla. Se descubrió por
#:   las lecciones del Gestor de Cartera (2026-08-19), que documentan `LIKE`
#:   como sintaxis soportada; la primera versión de este módulo solo miraba
#:   la comilla y los dejaba pasar.
#: · Los de control pueden partir el string de parámetros.
#:
#: ⚠️ Esta lista es **más permisiva de lo que probablemente haga falta**. El
#: Gestor usa `^[A-Za-z0-9_.\-]{1,64}$` y argumenta bien: escapar exige
#: conocer el parser del proveedor, que no está documentado. Acá se dejan
#: pasar espacios, `/` y acentos porque **no se ha medido qué caracteres
#: traen de verdad** los códigos del maestro. Medirlo y estrechar esto es
#: trabajo pendiente — ver `docs/medir_codigos_siesa.sql`. Hasta entonces la
#: lista prohíbe lo que rompe la consulta, no todo lo que podría.
_PROHIBIDOS = re.compile(r"['%\x00-\x1f\x7f]")


def lit(valor, campo: str = None) -> str:
    """`''valor''` con el valor verificado. `campo` solo mejora el mensaje.

    Levanta `ValueError` si el valor está vacío o trae algo que rompería el
    literal — nunca lo modifica en silencio.
    """
    s = '' if valor is None else str(valor).strip()
    donde = f' para {campo}' if campo else ''
    if not s:
        raise ValueError(
            f'Filtro Siesa sin valor{donde}: un literal vacío no filtra nada '
            f'y la consulta devolvería un universo que el llamador no pidió.')
    malo = _PROHIBIDOS.search(s)
    if malo:
        raise ValueError(
            f'Filtro Siesa con carácter inválido{donde}: {malo.group()!r} en '
            f"{s!r}. Rompería el literal ''valor'' y ensancharía la consulta. "
            f'No se limpia en silencio: se rechaza.')
    return f"''{s}''"
