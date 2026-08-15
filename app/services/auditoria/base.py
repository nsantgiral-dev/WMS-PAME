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

    def __post_init__(self):
        assert self.severidad in _SEVERIDADES, f'severidad inválida: {self.severidad}'

    def evaluar(self, ctx=None) -> List[Hallazgo]:
        return list(self.fn(ctx) or [])


_REGISTRO: List[Invariante] = []


def invariante(codigo: str, flujo: str, frontera: str, consecuencia: str,
               severidad: str = BLOQUEA):
    """Registra la función como invariante.

    El decorador y no una lista al final del archivo: una lista que hay que
    acordarse de actualizar es un invariante que algún día no corre y nadie
    nota — el mismo patrón que dejó `trigger_factura()` sin caller.
    """
    def _wrap(fn):
        assert not any(i.codigo == codigo for i in _REGISTRO), \
            f'código de invariante duplicado: {codigo}'
        _REGISTRO.append(Invariante(
            codigo=codigo, flujo=flujo, frontera=frontera,
            consecuencia=consecuencia, severidad=severidad, fn=fn))
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
        conteo, devoluciones, recepcion, reposicion, traslados, venta,
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
    _AUDITORIA_TRUNCADA.clear()
    inv = registrados(flujo)
    resultados, total = [], 0
    for i in inv:
        try:
            hallazgos = i.evaluar(ctx)
            error = None
        except Exception as e:                      # noqa: BLE001
            hallazgos, error = [], f'{type(e).__name__}: {e}'
        total += len(hallazgos)
        resultados.append({
            'codigo': i.codigo,
            'flujo': i.flujo,
            'frontera': i.frontera,
            'severidad': i.severidad,
            'consecuencia': i.consecuencia,
            'error': error,
            'hallazgos': [
                {'referencia': h.referencia, 'detalle': h.detalle, 'datos': h.datos}
                for h in hallazgos[:100]
            ],
            'total': len(hallazgos),
            'truncado': len(hallazgos) > 100,
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
        'resultados': resultados,
    }
