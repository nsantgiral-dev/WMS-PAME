"""
Invariantes de frontera. **Se declaran una vez y se corren de dos formas.**

## El hueco que esto viene a tapar

La suite tiene 1897 tests y ninguno puede encontrar esta clase de defecto,
porque **cada archivo de test arma su propia `TareaPacking(...)` desde cero**.
Ninguno recibe la del paso anterior. Cada etapa se verifica con datos que el
propio test fabricó coherentes, así que la coherencia entre etapas —que es
donde vive el defecto— no se ejercita nunca.

No es un fallo de los tests: es su forma. Un test unitario que construyera todo
el flujo dejaría de ser unitario.

## Un invariante, dos fuentes de datos

    tests/  → un flujo sintético recorrido con los servicios REALES
    /api/auditoria → los datos que ya están en la base

Es la misma regla evaluada sobre dos poblaciones. Escribirla dos veces sería la
divergencia que la Regla 0 prohíbe: el test pasaría y la auditoría diría otra
cosa, o al revés, y nadie sabría cuál creer.

## Un hallazgo no es un booleano

`False` no sirve para trabajar: hay que saber **cuáles** filas y **por qué**.
Un contador dice «pasa N veces»; esto dice qué mirar. Es la misma diferencia
que hubo entre saber que había 159 jobs en la DLQ y poder triarlos.

## Severidad: qué se rompe, no cuánto molesta

`BLOQUEA`  — el dato ya está mal y alguien va a operar sobre él.
`AVISA`    — es correcto hoy pero se degrada solo (un maestro incompleto).
`OBSERVA`  — vale la pena contarlo; todavía no hay evidencia de daño.

La distinción existe para que el canal siga siendo legible. Un auditor que
devuelve trescientas líneas de la misma clase entrena a que lo ignoren.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

BLOQUEA = 'BLOQUEA'
AVISA = 'AVISA'
OBSERVA = 'OBSERVA'

_SEVERIDADES = (BLOQUEA, AVISA, OBSERVA)
#: Consultas que llenaron su tope. **No es lo mismo que `truncado`**, que cuenta
#: hallazgos: esto dice que la auditoría no llegó a MIRAR todo el universo, y
#: por eso «0 hallazgos» puede significar «dejó de buscar».
#:
#: Se limpia al empezar cada corrida — si persistiera entre corridas, un tope
#: alcanzado una vez ensuciaría el reporte para siempre.
_AUDITORIA_TRUNCADA = set()


@dataclass
class Hallazgo:
    """Una fila concreta que viola el invariante.

    `referencia` es lo que alguien escribe en un buscador: un número de pedido,
    un código de tarea. Sin eso el hallazgo es una queja, no trabajo.
    """
    referencia: str
    detalle: str
    datos: dict = field(default_factory=dict)
    #: Cuándo nació la entidad (UTC naive, o día Bogotá). Es lo que decide si
    #: el hallazgo es de hoy o es anterior al corte (`FECHA_INICIO_AUDITORIA`)
    #: o a la corrección de su defecto (`vigente_desde`). **Sin fecha cuenta
    #: como vigente**: no saber cuándo pasó no lo vuelve viejo (Regla 0).
    #: El trinquete de `tests/test_analitica_solo_lo_actual.py` exige que todo
    #: `Hallazgo(...)` la pase, salvo los invariantes de estado actual
    #: declarados en su inventario.
    fecha: object = None


@dataclass
class Invariante:
    """Una regla que debe cumplirse al cruzar de una etapa a la siguiente."""
    codigo: str
    flujo: str
    frontera: str
    #: Qué se rompe si no se cumple. En términos de operación, no de código:
    #: quien lea el reporte no necesariamente sabe qué es un `producto_id`.
    consecuencia: str
    severidad: str
    fn: Callable
    #: `archivo::Clase::test` del test que hace DISPARAR este invariante
    #: construyendo la violación. Ver el docstring de `invariante()`.
    detector_ciego: str = None
    #: Invariante que vigila un defecto YA CORREGIDO: `(commit, fecha ISO)`.
    #: Lo anterior a la corrección se reporta como «artefacto de <commit>: N
    #: casos» y **no sube el nivel**: no es un error de hoy, es la huella de
    #: uno que ya no se puede repetir. Lo posterior sí bloquea: si reaparece,
    #: el arreglo se cayó. Ver `vigente_desde_utc`.
    defecto_corregido: tuple = None

    def __post_init__(self):
        assert self.severidad in _SEVERIDADES, f'severidad inválida: {self.severidad}'
        if self.defecto_corregido is not None:
            commit, fecha = self.defecto_corregido
            assert commit and fecha, f'{self.codigo}: defecto_corregido pide (commit, fecha)'
            self.vigente_desde_utc   # valida la fecha al registrar, no al auditar

    @property
    def vigente_desde_utc(self):
        """La fecha de la corrección en UTC naive (`None` si no vigila un
        defecto corregido). Se interpreta con la misma regla que el corte."""
        if self.defecto_corregido is None:
            return None
        from app.services.corte import _parsear
        utc, motivo = _parsear(self.defecto_corregido[1])
        assert utc is not None, f'{self.codigo}: vigente_desde ilegible ({motivo})'
        return utc

    def evaluar(self, ctx=None) -> List[Hallazgo]:
        return list(self.fn(ctx) or [])


_REGISTRO: List[Invariante] = []


def invariante(codigo: str, flujo: str, frontera: str, consecuencia: str,
               severidad: str = BLOQUEA, detector_ciego: str = None,
               defecto_corregido: tuple = None):
    """Registra la función como invariante.

    El decorador y no una lista al final del archivo: una lista que hay que
    acordarse de actualizar es un invariante que algún día no corre y nadie
    nota — el mismo patrón que dejó `trigger_factura()` sin caller.

    ## `detector_ciego` — el nombre del test que lo hace DISPARAR

    No el que lo hace pasar: **el que construye la violación y exige que la
    vea**. Es la lección más cara de la auditoría del 2026-08-15, donde SEIS
    guards estaban en verde sobre propiedades que la vía sana satisface por
    construcción:

        TRA-01  sobre dos campos que la escritura iguala
        REP-02  sobre un flag que solo existe en el estado que exige
        CNT-04  sobre una fila que el camino de salto conserva
        REC-01  sobre un booleano que se fuerza AL FALLAR
        bodegas sobre una de tres copias del mismo mapa
        nivel 4 sobre una URL dentro de una función sin llamador

    Los seis tenían test de «no dispara cuando está sano». Ninguno tenía el
    otro. **Los seis se habrían caído al primer intento.**

    La regla que sale de ahí, y que vale más que los seis arreglos:

    > ¿Qué escribe el valor que estoy comprobando, y puede el camino roto
    > escribirlo igual? Si la respuesta es sí, el guard no sirve.

    Se pide como referencia `archivo::Clase::test` y
    `tests/test_detector_ciego_obligatorio.py` verifica que **exista**: una
    referencia que no resuelve es peor que ninguna, porque afirma cobertura.

    ## `defecto_corregido` — `(commit, 'AAAA-MM-DDTHH:MM:SS-05:00')`

    Para el invariante que vigila un defecto ya arreglado: lo anterior a esa
    fecha es artefacto del defecto, se cuenta aparte y no bloquea. La fecha es
    la del commit que lo corrigió (la más temprana posible del despliegue:
    ante la duda, se muestra de más).
    """
    def _wrap(fn):
        assert not any(i.codigo == codigo for i in _REGISTRO), \
            f'código de invariante duplicado: {codigo}'
        _REGISTRO.append(Invariante(
            codigo=codigo, flujo=flujo, frontera=frontera,
            consecuencia=consecuencia, severidad=severidad, fn=fn,
            detector_ciego=detector_ciego, defecto_corregido=defecto_corregido))
        return fn
    return _wrap


def registrados(flujo: Optional[str] = None) -> List[Invariante]:
    _cargar()
    return [i for i in _REGISTRO if flujo is None or i.flujo == flujo]


def flujos() -> List[str]:
    _cargar()
    return sorted({i.flujo for i in _REGISTRO})


_cargado = False


def _cargar():
    """Importa los módulos de invariantes.

    Sin esto el registro está vacío y **todo pasa** — el modo de fallo
    peligroso, porque un auditor que no encuentra nada se lee igual que uno que
    no corrió. `tests/test_auditoria.py` exige que el registro no esté vacío.
    """
    global _cargado
    if _cargado:
        return
    from app.services.auditoria import (  # noqa: F401
        conteo, devoluciones, inventario, recepcion, reposicion, traslados,
        venta,
    )
    _cargado = True


def auditar(flujo: Optional[str] = None, ctx=None) -> dict:
    """Corre los invariantes y devuelve el reporte.

    Un invariante que revienta se reporta como `error`, no tumba la corrida:
    el auditor es lo que se mira cuando algo va mal y no puede ser lo siguiente
    que se rompe.
    """
    # Limpio en cada corrida: un tope alcanzado una vez no puede ensuciar el
    # reporte para siempre.
    from app.services import corte as _corte
    _AUDITORIA_TRUNCADA.clear()
    inv = registrados(flujo)
    corte = _corte.inicio_auditoria()
    resultados, total, total_antes, total_artefactos = [], 0, 0, 0
    for i in inv:
        try:
            crudos = i.evaluar(ctx)
            error = None
        except Exception as e:                      # noqa: BLE001
            crudos, error = [], f'{type(e).__name__}: {e}'
        hallazgos, antes, artefactos = _clasificar(i, crudos, corte)
        total += len(hallazgos)
        total_antes += antes
        total_artefactos += artefactos
        resultados.append({
            'codigo': i.codigo,
            'flujo': i.flujo,
            'frontera': i.frontera,
            'severidad': i.severidad,
            'consecuencia': i.consecuencia,
            'error': error,
            'hallazgos': [
                {'referencia': h.referencia, 'detalle': h.detalle, 'datos': h.datos,
                 # El resumen diario cuenta los NUEVOS con esto (P1-14).
                 'fecha': h.fecha.isoformat() if h.fecha else None}
                for h in hallazgos[:100]
            ],
            'total': len(hallazgos),
            'truncado': len(hallazgos) > 100,
            # Lo anterior al corte y los artefactos de un defecto corregido:
            # contados, nunca en `total` ni en `bloqueantes`.
            'antes_del_corte': antes,
            'artefacto': _artefacto(i, artefactos),
            'sin_fecha': sum(1 for h in hallazgos if h.fecha is None),
        })
    rotos = [r for r in resultados if r['total'] or r['error']]
    consultas_truncadas = sorted(_AUDITORIA_TRUNCADA)
    return {
        'invariantes_corridos': len(inv),
        # El universo mirado. Si esto trae algo, «0 hallazgos» significa «no se
        # buscó en todo», y hay que subir el tope antes de creerle al panel.
        'consultas_truncadas': consultas_truncadas,
        'invariantes_rotos': len(rotos),
        'hallazgos_totales': total,
        # Sin esto, «0 hallazgos» no se distingue de «no corrió nada».
        'bloqueantes': sum(r['total'] for r in resultados
                           if r['severidad'] == BLOQUEA),
        'errores': [r['codigo'] for r in resultados if r['error']],
        'antes_del_corte': total_antes,
        'artefactos': total_artefactos,
        'corte': _corte.estado(),
        'resultados': resultados,
    }


def _clasificar(inv: Invariante, hallazgos, corte):
    """`(vigentes, n_antes_del_corte, n_artefactos)`. **La única** que decide
    si un hallazgo es de hoy: primero el corte, después la corrección del
    defecto. Sin fecha → vigente."""
    from app.services.corte import es_anterior
    desde = inv.vigente_desde_utc
    vigentes, antes, artefactos = [], 0, 0
    for h in hallazgos:
        if es_anterior(h.fecha, corte):
            antes += 1
        elif desde is not None and es_anterior(h.fecha, desde):
            artefactos += 1
        else:
            vigentes.append(h)
    return vigentes, antes, artefactos


def _artefacto(inv: Invariante, n: int):
    if inv.defecto_corregido is None:
        return None
    commit, fecha = inv.defecto_corregido
    return {'commit': commit, 'vigente_desde': fecha, 'casos': n,
            'texto': f'artefacto de {commit}: {n} caso(s) anteriores a la corrección'}
