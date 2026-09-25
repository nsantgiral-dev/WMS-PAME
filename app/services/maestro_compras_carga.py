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
| Marca desde la clasificación de Siesa (`marca_desde_siesa`) | admin y compras | `SIESA_238920` |

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
mayor, la tabla t125; el plano del conector 238920 lo describe así:
`f125_id_plan`, `f125_id_criterio_mayor`). Qué plan es «marca» **lo decide el
dueño**: sin la variable, `marca_desde_siesa` no lee nada y lo dice. La
respuesta GET del 238920 **no tiene contrato** (el `.docx` es el del plano de
importación): los nombres de campo se buscan entre los del plano y, si no
aparecen, se devuelven los que sí vinieron para configurarlo
(`scripts/qa_compras_fuentes_real.py` los muestra).
"""
import csv
import io
import logging
import os
import unicodedata
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
FUENTE_SIESA = 'SIESA_238920'

MAX_FILAS = 20000

COLUMNAS = {
    TIPO_ORIGEN_MARCA: {'obligatorias': ('codigo',),
                        'opcionales': ('origen', 'marca')},
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
            raise ValueError('El servidor no tiene openpyxl para leer Excel: subí un CSV')
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
        raise ValueError('Formato no soportado: subí un .csv o un .xlsx')
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


def vista_previa(tipo: str, filas: list) -> dict:
    """Qué pasaría si se aplica. No escribe nada.

    Returns: {tipo, validas[{fila, codigo, producto_id, cambios, nuevo}],
              invalidas[{fila, codigo, errores}], sin_cambio, columnas_desconocidas,
              faltan_columnas, resumen}
    """
    from app.models.importacion import FichaImportacion

    if tipo not in TIPOS:
        raise ValueError(f'Tipo de carga desconocido: {tipo!r}. Válidos: {", ".join(TIPOS)}')
    if len(filas) > MAX_FILAS:
        raise ValueError(f'El archivo tiene {len(filas)} filas: el máximo es {MAX_FILAS}')
    cols = COLUMNAS[tipo]
    presentes = set().union(*[set(f) for f in filas]) if filas else set()
    faltan = [c for c in cols['obligatorias'] if c not in presentes]
    desconocidas = sorted(presentes - set(cols['obligatorias']) - set(cols['opcionales']))
    if faltan:
        return {'tipo': tipo, 'validas': [], 'invalidas': [], 'sin_cambio': 0,
                'faltan_columnas': faltan, 'columnas_desconocidas': desconocidas,
                'resumen': {'filas': len(filas), 'validas': 0, 'invalidas': 0}}
    if tipo == TIPO_ORIGEN_MARCA and not ({'origen', 'marca'} & presentes):
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
            if not errores and prod is not None:
                for campo, valor in (('origen', origen), ('marca_siesa', marca)):
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
            motivo: str = None) -> dict:
    """Escribe las filas válidas de `vista_previa` (recalculada acá). Commit."""
    from app.models.producto import Producto
    from app.models.importacion import FichaImportacion
    from app.services.bitacora import registrar_accion

    prev = vista_previa(tipo, filas)
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
# Marca desde Siesa (238920)
# ──────────────────────────────────────────────────────────────────────────────

_CLAVES_PLAN = ('f125_id_plan', 'f106_id_plan', 'id_plan', 'plan')
_CLAVES_CRITERIO = ('f125_id_criterio_mayor', 'f106_id_criterio_mayor', 'f106_id',
                    'id_criterio_mayor', 'criterio_mayor')
_CLAVES_REF = ('f120_referencia', 'referencia')


def criterio_marca():
    """`SIESA_CRITERIO_MARCA` = el plan de clasificación que es «marca».
    **Sin default**: lo decide el dueño. `None` = no configurado."""
    v = (os.getenv('SIESA_CRITERIO_MARCA') or '').strip()
    return v or None


def _clave(fila, candidatas):
    minus = {str(k).lower(): k for k in fila}
    for c in candidatas:
        if c in minus:
            return minus[c]
    return None


def filas_marca_desde_siesa(gateway=None, max_paginas: int = 200) -> dict:
    """Lee la clasificación y devuelve las filas `{codigo, marca}` del plan
    configurado. Nunca levanta."""
    plan = criterio_marca()
    if not plan:
        return {'omitido': 'SIESA_CRITERIO_MARCA no está configurada: el dueño '
                           'tiene que decidir qué plan de clasificación de Siesa es '
                           '«marca». No se lee nada hasta entonces.'}
    if gateway is None:
        from app.services.connekta_gateway import connekta as gateway
    if getattr(gateway, 'modo_simulacion', False):
        return {'omitido': 'Connekta en modo simulación'}
    filas, completa, motivo, claves_vistas = [], True, None, None
    k_plan = k_crit = k_ref = None
    for pag in range(1, max_paginas + 1):
        try:
            resp = gateway.get_clasificacion_items(pagina=pag)
        except Exception as e:
            completa, motivo = False, f'página {pag}: {str(e)[:300]}'
            break
        if resp is None:
            completa, motivo = False, f'página {pag}: circuito de Siesa abierto'
            break
        det = resp.get('detalle') if isinstance(resp, dict) else None
        rows = None
        if isinstance(det, dict):
            rows = det.get('Table', det.get('Datos'))
        if rows is None:
            completa, motivo = False, f'página {pag}: respuesta sin Table ni Datos'
            break
        if rows and k_plan is None:
            claves_vistas = sorted(str(k) for k in rows[0])
            k_plan, k_crit, k_ref = (_clave(rows[0], _CLAVES_PLAN),
                                     _clave(rows[0], _CLAVES_CRITERIO),
                                     _clave(rows[0], _CLAVES_REF))
            if not (k_plan and k_crit and k_ref):
                return {'campos_no_reconocidos': claves_vistas,
                        'nota': ('La respuesta del 238920 no trae los campos del plano '
                                 '(plan, criterio mayor, referencia). Con estos nombres '
                                 'hay que ajustar `_CLAVES_*` — no se adivina.')}
        for r in rows:
            if _txt(r.get(k_plan)) == plan:
                filas.append({'codigo': _txt(r.get(k_ref)), 'marca': _txt(r.get(k_crit))})
        if len(rows) < 100:
            break
    else:
        completa, motivo = False, f'tope de {max_paginas} páginas'
    return {'filas': filas, 'completa': completa, 'motivo_incompleta': motivo,
            'plan': plan, 'campos': claves_vistas}


def marca_desde_siesa(gateway=None, aplicar_=False, usuario_id=None) -> dict:
    """Vista previa (o aplicación) de la marca leída de Siesa."""
    leido = filas_marca_desde_siesa(gateway)
    if 'filas' not in leido:
        return leido
    extra = {k: leido[k] for k in ('completa', 'motivo_incompleta', 'plan', 'campos')}
    if not leido['filas']:
        return {'resumen': {'filas': 0}, 'validas': [], 'invalidas': [], **extra,
                'nota': f'Ningún ítem clasificado en el plan {leido["plan"]}.'}
    if aplicar_:
        return {**aplicar(TIPO_ORIGEN_MARCA, leido['filas'], usuario_id=usuario_id,
                          fuente=FUENTE_SIESA, motivo='Marca leída de Siesa (238920)'),
                **extra}
    return {**vista_previa(TIPO_ORIGEN_MARCA, leido['filas']), **extra}
