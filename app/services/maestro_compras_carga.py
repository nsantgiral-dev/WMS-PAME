"""
Origen, marca y fichas de importación: la carga que no tenía fuente.

`Producto.origen`, `Producto.marca_siesa` y `ficha_importacion` **no tenían
ningún escritor**: el sync de Siesa no los trae (`API_v2_Items` no tiene marca
ni origen), no había ruta ni script. Con eso el régimen China del armador no se
podía activar (`insumo_origen.regimen_china_operativo = False`) y el filtro de
marca del conteo devolvía vacío.

Tres vías, todas con **vista previa antes de escribir**:

| Vía | Quién | `*_fuente` que queda |
|---|---|---|
| Archivo CSV / Excel (`vista_previa` → `aplicar`) | admin y compras | `CARGA_ARCHIVO` |
| Un SKU a mano (`editar_producto`) | admin y compras | `MANUAL` |
| Marca desde la clasificación de Siesa (`leer_marca_siesa` en segundo plano → `marca_desde_siesa`) | admin y compras | `SIESA_CRITERIOS` |

## Reglas

- **La vista previa es el mismo cálculo que la aplicación**: `aplicar` vuelve a
  validar el archivo; nada que no se haya visto se escribe.
- **Nunca se borra un valor**: una celda vacía no toca el campo.
- **Toda sobrescritura de un valor que existía queda en la bitácora** (EDITAR,
  con antes y después).
- Un código que no está en el catálogo, un número ilegible o un valor fuera
  del vocabulario es una fila inválida, con su motivo — no se adivina.

## La marca en Siesa — `SIESA_CRITERIO_MARCA` (sin default)

En Siesa la marca es un **criterio de clasificación** del ítem (plan + criterio
mayor, la tabla t125). Se lee de la consulta estándar `API_v2_ItemsCriterios`
(**sin contrato** en `docs/siesa-specs/`; campos verificados en vivo el
2026-09-25: `f120_rowid`, `f120_id_cia`, `f120_referencia`, `f125_id_plan`,
`f105_descripcion` —nombre del plan—, `f125_id_criterio_mayor` —el código,
M001— y `f106_descripcion` —el nombre, NORMA—), filtrada por el plan con
`f125_id_plan = ''P03''`. En QA: P01 línea de negocio, P02 sub línea, **P03
marca** (26.053 ítems, 261 páginas, ~5 min). Qué plan es «marca» **lo decide
el dueño**: sin la variable no se lee nada.

Se guarda el NOMBRE en `Producto.marca_siesa` y el código en
`Producto.marca_codigo` (m047). **Ya no se usa el 238920**: su `.docx` es el
del plano de importación (escribir clasificación) y su GET da 401.

La lectura corre **en segundo plano** (`disparar_lectura_marca`, hilo con
`LOCK_MARCA_SIESA` y registro `compras_marca`): cinco minutos no caben en un
request (gunicorn corta a los 60 s). Queda en `marca_siesa_lectura`; la vista
previa y la aplicación leen de ahí, nunca de Siesa. **Una lectura incompleta**
(página que falla, tope, un `f120_rowid` repetido = paginación inestable) no
se guarda ni se aplica: se declara.
"""
import csv
import io
import logging
import os
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from app.extensions import db

logger = logging.getLogger(__name__)

TIPO_ORIGEN_MARCA = 'ORIGEN_MARCA'
TIPO_FICHAS = 'FICHAS'
TIPOS = (TIPO_ORIGEN_MARCA, TIPO_FICHAS)

ORIGENES = ('NACIONAL', 'CHINA', 'IMPORTADO')
FUENTES_FICHA = ('PACKING_LIST', 'AGENTE', 'ESTIMADO')

FUENTE_CARGA = 'CARGA_ARCHIVO'
FUENTE_MANUAL = 'MANUAL'
FUENTE_SIESA = 'SIESA_CRITERIOS'   # antes 'SIESA_238920' (nunca se escribió: el GET daba 401)

MAX_FILAS = 20000

COLUMNAS = {
    TIPO_ORIGEN_MARCA: {'obligatorias': ('codigo',),
                        'opcionales': ('origen', 'marca', 'marca_codigo')},
    TIPO_FICHAS: {'obligatorias': ('codigo', 'unidades_por_caja', 'cbm_por_caja',
                                   'peso_kg_por_caja'),
                  'opcionales': ('moq_cajas', 'proveedor_china', 'costo_fob_usd',
                                 'fuente')},
}

#: Encabezados que la gente escribe, llevados al nombre de la columna.
ALIAS = {
    'referencia': 'codigo', 'codigo_siesa': 'codigo', 'sku': 'codigo', 'item': 'codigo',
    'marca_siesa': 'marca', 'unidades_caja': 'unidades_por_caja',
    'cbm_caja': 'cbm_por_caja', 'peso_caja': 'peso_kg_por_caja',
    'peso_kg_caja': 'peso_kg_por_caja', 'moq': 'moq_cajas',
    'proveedor': 'proveedor_china', 'fob_usd': 'costo_fob_usd',
}


# ──────────────────────────────────────────────────────────────────────────────
# Lectura del archivo
# ──────────────────────────────────────────────────────────────────────────────

def _normalizar_encabezado(h) -> str:
    s = '' if h is None else str(h).strip().lower()
    s = ''.join(c for c in unicodedata.normalize('NFD', s)
                if unicodedata.category(c) != 'Mn')
    s = s.replace(' ', '_').replace('-', '_')
    return ALIAS.get(s, s)


def leer_archivo(nombre: str, contenido: bytes) -> list:
    """CSV (`,` o `;`, UTF-8 con o sin BOM) o Excel (.xlsx, primera hoja).
    Devuelve una lista de dicts con encabezados normalizados. Levanta
    `ValueError` con un mensaje para la persona."""
    nombre = (nombre or '').lower()
    if nombre.endswith('.xlsx'):
        try:
            import openpyxl
        except ImportError:
            raise ValueError('El servidor no tiene openpyxl para leer Excel: suba un CSV')
        try:
            wb = openpyxl.load_workbook(io.BytesIO(contenido), read_only=True, data_only=True)
        except Exception as e:
            raise ValueError(f'No se pudo abrir el Excel: {e}')
        filas = list(wb.worksheets[0].iter_rows(values_only=True))
        if not filas:
            raise ValueError('El Excel está vacío')
        enc = [_normalizar_encabezado(h) for h in filas[0]]
        salida = []
        for fila in filas[1:]:
            if fila is None or all(v in (None, '') for v in fila):
                continue
            salida.append({enc[i]: fila[i] for i in range(min(len(enc), len(fila))) if enc[i]})
        return salida
    if nombre and not nombre.endswith(('.csv', '.txt')):
        raise ValueError('Formato no soportado: suba un .csv o un .xlsx')
    try:
        texto = contenido.decode('utf-8-sig')
    except UnicodeDecodeError:
        texto = contenido.decode('latin-1')
    return filas_desde_texto(texto)


def filas_desde_texto(texto: str) -> list:
    texto = (texto or '').strip()
    if not texto:
        raise ValueError('El archivo está vacío')
    primera = texto.splitlines()[0]
    delim = ';' if primera.count(';') > primera.count(',') else ','
    lector = csv.reader(io.StringIO(texto), delimiter=delim)
    filas = list(lector)
    enc = [_normalizar_encabezado(h) for h in filas[0]]
    salida = []
    for fila in filas[1:]:
        if not any((c or '').strip() for c in fila):
            continue
        salida.append({enc[i]: fila[i] for i in range(min(len(enc), len(fila))) if enc[i]})
    return salida


# ──────────────────────────────────────────────────────────────────────────────
# Validación
# ──────────────────────────────────────────────────────────────────────────────

def _txt(v):
    return '' if v is None else str(v).strip()


def _numero(v, campo, *, entero=False, minimo=None, maximo=None, errores=None):
    s = _txt(v)
    if not s:
        return None
    if ',' in s and '.' in s:
        errores.append(f'{campo}: «{s}» es ambiguo (punto y coma a la vez)')
        return None
    s = s.replace(',', '.')
    try:
        d = Decimal(s)
    except InvalidOperation:
        errores.append(f'{campo}: «{s}» no es un número')
        return None
    if entero and d != d.to_integral_value():
        errores.append(f'{campo}: «{s}» tiene que ser entero')
        return None
    if minimo is not None and d < minimo:
        errores.append(f'{campo}: {s} es menor que {minimo}')
        return None
    if maximo is not None and d > maximo:
        errores.append(f'{campo}: {s} es mayor que {maximo} — ¿unidad equivocada?')
        return None
    return int(d) if entero else d


def _productos_por_codigo(codigos):
    from app.models.producto import Producto
    codigos = [c for c in set(codigos) if c]
    mapa = {}
    for i in range(0, len(codigos), 500):
        parte = codigos[i:i + 500]
        for p in Producto.query.filter(Producto.codigo_siesa.in_(parte)).all():
            mapa[p.codigo_siesa] = p
        for p in Producto.query.filter(Producto.codigo.in_(parte)).all():
            mapa.setdefault(p.codigo, p)
    return mapa


def _cambio(antes, despues):
    if despues is None:
        return None
    a = float(antes) if isinstance(antes, Decimal) else antes
    d = float(despues) if isinstance(despues, Decimal) else despues
    if a == d:
        return None
    return {'antes': a, 'despues': d}


def vista_previa(tipo: str, filas: list, max_filas: int = MAX_FILAS) -> dict:
    """Qué pasaría si se aplica. No escribe nada.

    Returns: {tipo, validas[{fila, codigo, producto_id, cambios, nuevo}],
              invalidas[{fila, codigo, errores}], sin_cambio, columnas_desconocidas,
              faltan_columnas, resumen}
    """
    from app.models.importacion import FichaImportacion

    if tipo not in TIPOS:
        raise ValueError(f'Tipo de carga desconocido: {tipo!r}. Válidos: {", ".join(TIPOS)}')
    if max_filas is not None and len(filas) > max_filas:
        raise ValueError(f'El archivo tiene {len(filas)} filas: el máximo es {max_filas}')
    cols = COLUMNAS[tipo]
    presentes = set().union(*[set(f) for f in filas]) if filas else set()
    faltan = [c for c in cols['obligatorias'] if c not in presentes]
    desconocidas = sorted(presentes - set(cols['obligatorias']) - set(cols['opcionales']))
    if faltan:
        return {'tipo': tipo, 'validas': [], 'invalidas': [], 'sin_cambio': 0,
                'faltan_columnas': faltan, 'columnas_desconocidas': desconocidas,
                'resumen': {'filas': len(filas), 'validas': 0, 'invalidas': 0}}
    if tipo == TIPO_ORIGEN_MARCA and not ({'origen', 'marca', 'marca_codigo'} & presentes):
        return {'tipo': tipo, 'validas': [], 'invalidas': [], 'sin_cambio': 0,
                'faltan_columnas': ['origen o marca'], 'columnas_desconocidas': desconocidas,
                'resumen': {'filas': len(filas), 'validas': 0, 'invalidas': 0}}

    productos = _productos_por_codigo(_txt(f.get('codigo')) for f in filas)
    fichas = {}
    if tipo == TIPO_FICHAS and productos:
        ids = [p.id for p in productos.values()]
        for i in range(0, len(ids), 500):
            for fi in FichaImportacion.query.filter(
                    FichaImportacion.producto_id.in_(ids[i:i + 500])).all():
                fichas[fi.producto_id] = fi

    validas, invalidas, sin_cambio = [], [], 0
    vistos = {}
    for n, f in enumerate(filas, start=2):          # fila 1 = encabezados
        errores = []
        codigo = _txt(f.get('codigo'))
        prod = productos.get(codigo)
        if not codigo:
            errores.append('sin código')
        elif prod is None:
            errores.append(f'«{codigo}» no existe en el catálogo del WMS')
        elif codigo in vistos:
            errores.append(f'repetido: ya viene en la fila {vistos[codigo]}')
        if codigo and codigo not in vistos:
            vistos[codigo] = n

        cambios, nuevo = {}, False
        if tipo == TIPO_ORIGEN_MARCA:
            origen = _txt(f.get('origen')).upper() or None
            if origen:
                origen = ''.join(c for c in unicodedata.normalize('NFD', origen)
                                 if unicodedata.category(c) != 'Mn')
                if origen not in ORIGENES:
                    errores.append(f'origen «{origen}» no es {", ".join(ORIGENES)}')
                    origen = None
            marca = _txt(f.get('marca')).upper() or None
            if marca and len(marca) > 50:
                errores.append('marca: más de 50 caracteres')
                marca = None
            marca_codigo = _txt(f.get('marca_codigo')).upper() or None
            if marca_codigo and len(marca_codigo) > 20:
                errores.append('marca_codigo: más de 20 caracteres')
                marca_codigo = None
            if not errores and prod is not None:
                for campo, valor in (('origen', origen), ('marca_siesa', marca),
                                     ('marca_codigo', marca_codigo)):
                    c = _cambio(getattr(prod, campo), valor)
                    if c:
                        cambios[campo] = c
        else:
            upc = _numero(f.get('unidades_por_caja'), 'unidades_por_caja', entero=True,
                          minimo=1, errores=errores)
            cbm = _numero(f.get('cbm_por_caja'), 'cbm_por_caja', minimo=Decimal('0.0001'),
                          maximo=Decimal('10'), errores=errores)
            peso = _numero(f.get('peso_kg_por_caja'), 'peso_kg_por_caja',
                           minimo=Decimal('0.01'), maximo=Decimal('1000'), errores=errores)
            for campo, valor in (('unidades_por_caja', upc), ('cbm_por_caja', cbm),
                                 ('peso_kg_por_caja', peso)):
                if valor is None and not any(e.startswith(campo) for e in errores):
                    errores.append(f'{campo}: obligatorio')
            moq = _numero(f.get('moq_cajas'), 'moq_cajas', entero=True, minimo=1,
                          errores=errores)
            fob = _numero(f.get('costo_fob_usd'), 'costo_fob_usd', minimo=0, errores=errores)
            prov = _txt(f.get('proveedor_china'))[:100] or None
            fuente = _txt(f.get('fuente')).upper() or None
            if fuente and fuente not in FUENTES_FICHA:
                errores.append(f'fuente «{fuente}» no es {", ".join(FUENTES_FICHA)}')
            if not errores and prod is not None:
                actual = fichas.get(prod.id)
                nuevo = actual is None
                if nuevo:
                    moq = moq or 1
                    fuente = fuente or 'ESTIMADO'
                for campo, valor in (('unidades_por_caja', upc), ('cbm_por_caja', cbm),
                                     ('peso_kg_por_caja', peso), ('moq_cajas', moq),
                                     ('proveedor_china', prov), ('costo_fob_usd', fob),
                                     ('fuente', fuente)):
                    c = _cambio(getattr(actual, campo, None), valor)
                    if c:
                        cambios[campo] = c

        if errores:
            invalidas.append({'fila': n, 'codigo': codigo or None, 'errores': errores})
        elif not cambios:
            sin_cambio += 1
        else:
            validas.append({'fila': n, 'codigo': codigo, 'producto_id': prod.id,
                            'nombre': prod.nombre, 'nuevo': nuevo, 'cambios': cambios})

    return {
        'tipo': tipo, 'validas': validas, 'invalidas': invalidas,
        'sin_cambio': sin_cambio, 'faltan_columnas': [],
        'columnas_desconocidas': desconocidas,
        'resumen': {'filas': len(filas), 'validas': len(validas),
                    'invalidas': len(invalidas), 'sin_cambio': sin_cambio,
                    'sobrescriben': sum(1 for v in validas if any(
                        c['antes'] not in (None, '') for c in v['cambios'].values()))},
    }


def aplicar(tipo: str, filas: list, usuario_id=None, fuente: str = FUENTE_CARGA,
            motivo: str = None, max_filas: int = MAX_FILAS) -> dict:
    """Escribe las filas válidas de `vista_previa` (recalculada acá). Commit."""
    from app.models.producto import Producto
    from app.models.importacion import FichaImportacion
    from app.services.bitacora import registrar_accion

    prev = vista_previa(tipo, filas, max_filas=max_filas)
    if prev['faltan_columnas']:
        return {'ok': False, 'error': 'Faltan columnas: ' + ', '.join(prev['faltan_columnas']),
                **prev}
    escritas = 0
    for v in prev['validas']:
        cambios = v['cambios']
        sobrescribe = {k: c for k, c in cambios.items() if c['antes'] not in (None, '')}
        if tipo == TIPO_ORIGEN_MARCA:
            prod = db.session.get(Producto, v['producto_id'])
            if 'origen' in cambios:
                prod.origen = cambios['origen']['despues']
                prod.origen_fuente = fuente
            if 'marca_siesa' in cambios:
                prod.marca_siesa = cambios['marca_siesa']['despues']
                prod.marca_fuente = fuente
            if 'marca_codigo' in cambios:
                prod.marca_codigo = cambios['marca_codigo']['despues']
                prod.marca_fuente = fuente
            entidad = prod
        else:
            ficha = FichaImportacion.query.filter_by(producto_id=v['producto_id']).first()
            if ficha is None:
                ficha = FichaImportacion(producto_id=v['producto_id'])
                db.session.add(ficha)
            for campo, c in cambios.items():
                setattr(ficha, campo, c['despues'])
            db.session.flush()
            entidad = ficha
        if sobrescribe:
            registrar_accion('EDITAR', entidad, usuario_id=usuario_id,
                             motivo=motivo or f'Carga {fuente}',
                             antes={k: c['antes'] for k, c in sobrescribe.items()},
                             despues={k: c['despues'] for k, c in sobrescribe.items()})
        escritas += 1
    db.session.commit()
    return {'ok': True, 'escritas': escritas, 'fuente': fuente, **prev}


def editar_producto(codigo: str, origen=None, marca=None, usuario_id=None) -> dict:
    """Un SKU a mano: el mismo camino que una fila de archivo, con fuente MANUAL."""
    fila = {'codigo': codigo}
    if origen is not None:
        fila['origen'] = origen
    if marca is not None:
        fila['marca'] = marca
    return aplicar(TIPO_ORIGEN_MARCA, [fila], usuario_id=usuario_id,
                   fuente=FUENTE_MANUAL, motivo='Edición manual del producto')


# ──────────────────────────────────────────────────────────────────────────────
# Marca desde Siesa (`API_v2_ItemsCriterios`, en segundo plano)
# ──────────────────────────────────────────────────────────────────────────────

REGISTRO_MARCA = 'compras_marca'
#: Una lectura abierta hace más que esto se da por muerta (reinicio, deploy):
#: no bloquea una nueva.
TECHO_LECTURA_MIN = 20
_CAMPOS_CRITERIOS = ('f120_referencia', 'f125_id_plan', 'f125_id_criterio_mayor',
                     'f106_descripcion')


def criterio_marca():
    """`SIESA_CRITERIO_MARCA` = el plan de clasificación que es «marca» (en QA,
    `P03`). **Sin default**: lo decide el dueño. `None` = no configurado."""
    v = (os.getenv('SIESA_CRITERIO_MARCA') or '').strip()
    return v or None


def _api_criterios() -> str:
    return (os.getenv('CONNEKTA_API_ITEMS_CRITERIOS') or 'API_v2_ItemsCriterios').strip()


def _max_paginas_marca() -> int:
    """Tope de páginas de la lectura. P03 son 261 en QA: el default (400)
    alcanza con margen; con la última página llena en el tope la lectura queda
    INCOMPLETA, no se trunca callada."""
    try:
        return max(1, int(os.getenv('COMPRAS_MARCA_MAX_PAGINAS', '400')))
    except ValueError:
        return 400


def _pausa_marca() -> float:
    try:
        return max(0.0, float(os.getenv('COMPRAS_MARCA_PAUSA_S', '0.2')))
    except ValueError:
        return 0.2


def _sin_plan():
    return {'omitido': 'SIESA_CRITERIO_MARCA no está configurada: el dueño tiene que '
                       'decidir qué plan de clasificación de Siesa es «marca» (en QA, '
                       'P03). No se lee nada hasta entonces.'}


def leer_marca_siesa(gateway=None, pausa_s=None, ahora=None) -> dict:
    """Descarga el plan de marca COMPLETO y lo guarda en `marca_siesa_lectura`.
    **No toca ningún producto** (eso es `marca_desde_siesa(aplicar_=True)`).

    Corre en segundo plano (`disparar_lectura_marca`); se puede llamar directo
    desde un script. Nunca levanta. Una lectura incompleta no escribe nada y
    deja el motivo en `registros_sync` (`compras_marca`).
    """
    from app.models.compras_fuentes import MarcaSiesaLectura
    from app.services import registro_sync_service as _reg
    from app.services.compras_oc_sync import _descargar, id_cia
    from app.services.siesa_filtro import lit

    plan = criterio_marca()
    if not plan:
        return _sin_plan()
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    if getattr(gateway, 'modo_simulacion', False):
        return {'omitido': 'Connekta en modo simulación'}
    pausa = _pausa_marca() if pausa_s is None else pausa_s
    ahora = ahora or datetime.utcnow()
    registro = _reg.abrir(REGISTRO_MARCA)
    if registro is None:
        return {'ok': False, 'error': 'No se pudo anotar la corrida en registros_sync: '
                                      'sin eso la lectura no se podría identificar.'}
    t0 = time.monotonic()
    try:
        parametros = f'f125_id_plan = {lit(plan, "SIESA_CRITERIO_MARCA")}'
    except ValueError as e:
        _reg.cerrar_error(registro, str(e))
        return {'ok': False, 'error': str(e)}
    # Clave (ítem, plan): si Siesa ignorara el filtro, el mismo ítem vendría una
    # vez por plan y eso no es una página repetida — se cuenta como otro plan.
    filas, completa, motivo, paginas = _descargar(
        gateway, _api_criterios(), parametros, pausa,
        clave=lambda r: (r.get('f120_rowid'), (r.get('f125_id_plan') or '').strip()),
        tope=_max_paginas_marca())

    cont = Counter()
    por_ref = {}
    campos = sorted(str(k) for k in filas[0]) if filas else []
    faltan = [c for c in _CAMPOS_CRITERIOS if filas and c not in filas[0]]
    if faltan:
        completa, motivo = False, (f'la respuesta no trae {", ".join(faltan)} — '
                                   f'vinieron: {", ".join(campos)}. No se adivina.')
    for f in filas if completa else []:
        if _txt(f.get('f125_id_plan')) != plan:
            cont['otro_plan'] += 1
            continue
        cia = f.get('f120_id_cia')
        if cia is not None and str(cia).strip() != str(id_cia()):
            cont['otra_compania'] += 1
            continue
        ref = _txt(f.get('f120_referencia'))[:50]
        if not ref:
            cont['sin_referencia'] += 1
            continue
        if ref in por_ref:
            cont['referencia_repetida'] += 1
        por_ref[ref] = (_txt(f.get('f125_id_criterio_mayor'))[:20] or None,
                        _txt(f.get('f106_descripcion'))[:100] or None)

    resultado = {
        'plan': plan, 'nombre_plan': _txt((filas or [{}])[0].get('f105_descripcion')) or None,
        'paginas': paginas, 'filas_recibidas': len(filas), 'items': len(por_ref),
        'criterios_distintos': len({v[0] for v in por_ref.values()}),
        'segundos': round(time.monotonic() - t0, 1), 'campos': campos, **dict(cont),
    }
    if not completa:
        _reg.cerrar_error(registro, f'lectura incompleta: {motivo}', resultado)
        logger.warning('[MARCA_SIESA] incompleta, no se guarda: %s', motivo)
        return {'ok': False, 'completa': False, 'motivo_incompleta': motivo, **resultado}
    try:
        existentes = {m.referencia: m for m in
                      MarcaSiesaLectura.query.filter_by(plan=plan).all()}
        for ref, (codigo, nombre) in por_ref.items():
            m = existentes.get(ref)
            if m is None:
                m = MarcaSiesaLectura(plan=plan, referencia=ref)
                db.session.add(m)
            m.criterio_codigo, m.criterio_nombre = codigo, nombre
            m.registro_id, m.vista_en = registro, ahora
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        _reg.cerrar_error(registro, f'escritura: {e}', resultado)
        return {'ok': False, 'error': str(e)[:300], **resultado}
    _reg.cerrar_ok(registro, resultado)
    logger.info('[MARCA_SIESA] %s', resultado)
    return {'ok': True, 'completa': True, **resultado}


def _lectura_en_curso():
    """La corrida abierta y reciente, si la hay (una abierta hace más de
    `TECHO_LECTURA_MIN` se da por muerta)."""
    from app.services import registro_sync_service as _reg
    ult = _reg.ultimo(REGISTRO_MARCA)
    if not isinstance(ult, dict) or ult.get('_error_lectura') or ult.get('ok') is not None:
        return None
    try:
        inicio = datetime.fromisoformat(ult['inicio'])
    except (TypeError, ValueError, KeyError):
        return None
    return ult if datetime.utcnow() - inicio < timedelta(minutes=TECHO_LECTURA_MIN) else None


def lectura_vigente():
    """La última lectura COMPLETA: `{registro_id, plan, leida_utc, resultado}`
    o `None`."""
    from app.services import registro_sync_service as _reg
    ok = _reg.ultimo_ok(REGISTRO_MARCA)
    if not isinstance(ok, dict) or ok.get('_error_lectura'):
        return None
    res = ok.get('resultado') or {}
    return {'registro_id': ok['id'], 'plan': res.get('plan'), 'leida_utc': ok.get('fin'),
            'resultado': res}


def estado_lectura_marca() -> dict:
    """Para la pantalla: plan configurado, lectura en curso, la última
    (completa o no) y la vigente. No toca Siesa."""
    from app.services import registro_sync_service as _reg
    ult = _reg.ultimo(REGISTRO_MARCA)
    vig = lectura_vigente()
    plan = criterio_marca()
    return {
        'plan_configurado': plan,
        'en_curso': _lectura_en_curso() is not None,
        'ultima': ({k: ult.get(k) for k in ('inicio', 'fin', 'ok', 'error', 'resultado')}
                   if isinstance(ult, dict) else None),
        'vigente': vig,
        'vigente_es_del_plan': bool(vig and plan and vig['plan'] == plan),
    }


def disparar_lectura_marca(app, lanzar=None) -> dict:
    """El botón «Leer de Siesa»: la lectura corre en un hilo, con
    `LOCK_MARCA_SIESA`, y la respuesta vuelve ya. La Regla 14 no la dobla un
    botón (fuera de 7:00–19:30 → no). `lanzar` es para los tests."""
    import threading
    from app.services.ventana_siesa import texto_ventana, ventana_abierta

    if not criterio_marca():
        return {'ok': False, 'codigo': 400, **_sin_plan()}
    if not ventana_abierta():
        return {'ok': False, 'codigo': 409,
                'error': f'Fuera de la ventana de Siesa ({texto_ventana()}).'}
    en_curso = _lectura_en_curso()
    if en_curso:
        return {'ok': False, 'codigo': 409,
                'error': f'Ya hay una lectura en curso (empezó {en_curso["inicio"]} UTC).'}

    def _run():
        from app.utils.lock import LOCK_MARCA_SIESA, advisory_lock
        with app.app_context():
            with advisory_lock(LOCK_MARCA_SIESA, 'marca_siesa') as tomado:
                if not tomado:
                    logger.info('[MARCA_SIESA] otro proceso ya está leyendo')
                    return
                try:
                    leer_marca_siesa()
                except Exception as e:      # noqa: BLE001 — un hilo no tiene a quién avisar
                    db.session.rollback()
                    logger.error('[MARCA_SIESA] falló: %s', e, exc_info=True)

    (lanzar or (lambda fn: threading.Thread(target=fn, daemon=True).start()))(_run)
    return {'ok': True, 'codigo': 202,
            'mensaje': 'Lectura de Siesa iniciada en segundo plano (≈5 min). '
                       'Después, «Ver qué cambiaría».'}


def marca_desde_siesa(aplicar_=False, usuario_id=None) -> dict:
    """Vista previa (o aplicación) de la marca **leída y guardada**: nunca
    consulta Siesa. Solo los ítems que existen en el catálogo del WMS; los
    demás se cuentan (`fuera_del_catalogo`)."""
    from app.models.compras_fuentes import MarcaSiesaLectura

    plan = criterio_marca()
    if not plan:
        return _sin_plan()
    vig = lectura_vigente()
    if vig is None:
        return {'sin_lectura': 'Todavía no hay una lectura completa de la marca de Siesa: '
                               'toque «Leer de Siesa» y vuelva en unos minutos.',
                'estado': estado_lectura_marca()}
    if vig['plan'] != plan:
        return {'sin_lectura': f'La última lectura completa es del plan {vig["plan"]} y '
                               f'SIESA_CRITERIO_MARCA dice {plan}: vuelva a leer.',
                'estado': estado_lectura_marca()}
    lecturas = MarcaSiesaLectura.query.filter_by(plan=plan,
                                                 registro_id=vig['registro_id']).all()
    productos = _productos_por_codigo(m.referencia for m in lecturas)
    filas = [{'codigo': m.referencia, 'marca': m.criterio_nombre or '',
              'marca_codigo': m.criterio_codigo or ''}
             for m in lecturas if m.referencia in productos]
    extra = {'plan': plan, 'leida_utc': vig['leida_utc'], 'items_leidos': len(lecturas),
             'fuera_del_catalogo': len(lecturas) - len(filas), 'lectura': vig['resultado']}
    if not filas:
        return {'resumen': {'filas': 0}, 'validas': [], 'invalidas': [], **extra,
                'nota': f'Ningún ítem del catálogo del WMS está clasificado en el plan {plan}.'}
    if aplicar_:
        return {**aplicar(TIPO_ORIGEN_MARCA, filas, usuario_id=usuario_id, fuente=FUENTE_SIESA,
                          motivo=f'Marca leída de Siesa (API_v2_ItemsCriterios, plan {plan})',
                          max_filas=None), **extra}
    return {**vista_previa(TIPO_ORIGEN_MARCA, filas, max_filas=None), **extra}
