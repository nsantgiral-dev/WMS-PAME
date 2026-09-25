"""
Compras — la bandeja del comprador (2026-09-25).

**Este módulo COMPONE. No calcula.** Cada número que entrega ya lo calculó su
dueño, y acá solo se junta, se ordena y se etiqueta para que un comprador lo
lea de una vez:

| Lo que pinta | Quién lo calcula |
|---|---|
| Punto de pedido, reserva de seguridad, nivel objetivo, déficit, disponible, posición, «se agota antes de que llegue», cuándo llegaría | `armador_service.ArmadorService.rop_dual` (+ `nivel_objetivo`) |
| Empaques y MOQ | `armador_service.pedido_en_empaques` (`cajas_a_pedir`) sobre `compras_fuentes.empaque_de_compra` |
| Proveedor habitual, NIT | `compras_fuentes.proveedor_habitual` / `proveedores_info` |
| Precio y su fuente | `costo_service.resolver_costos` |
| Valor y subtotales | `costo_service.valorizar` / `sumar_valores` |
| Bloqueados | `bloqueo_recompra_service.BloqueoRecompraService.verificar_oc` |
| ¿Está al día el kardex? | `kardex_service.salud_kardex` |
| Existencias por sede | `analitica_salud.fuente_stock` |
| OCs sincronizadas, OCs abiertas, llegadas | `compras_fuentes.frescura_oc` / `ocs_abiertas` / `llegadas_recientes` |
| Contenedor | `ArmadorService.armar_contenedor` + `composicion_por_proveedor` |
| Temporada | `TemporadaService.preparar_pedido_temporada` + `fechas_limite_pedido` + `conciliar_con_contenedor` |
| Deriva de precios | `compras_inteligencia_service.detectar_deriva` |

`tests/test_compras_bandeja.py::TestLaBandejaNoCalcula` lo exige por AST: acá
no hay un solo operador aritmético (`+ - * / // % **`, ni su forma `+=`, ni el
signo menos), ni `sum/max/min/abs/round/pow/divmod`, ni `math`/`statistics`,
ni una consulta a la base. Una fórmula nueva que haga falta va en su dueño,
con su test, y la bandeja la lee.

Lo único que este módulo decide son **etiquetas** sobre números ya calculados,
con comparaciones: la urgencia (`_urgencia`) y el estado de cada pestaña.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

#: La meta de nivel de servicio con la que se calcula la bandeja. La misma que
#: usa el ROP por defecto (decisión abierta del dueño: ¿distinta para la
#: canasta constitucional?).
NIVEL_SERVICIO = 0.95

#: Ventana de PRESENTACIÓN de «Próximas»: los SKU que todavía no cruzan su
#: punto de pedido pero lo cruzan dentro de esta cantidad de días, a su tasa de
#: hoy. No cambia ninguna cantidad: solo decide si una fila se muestra.
DIAS_PROXIMAS = 7

URGENTE, ESTA_SEMANA, PROXIMAS = 'URGENTE', 'ESTA_SEMANA', 'PROXIMAS'
ORDEN_URGENCIA = {URGENTE: 0, ESTA_SEMANA: 1, PROXIMAS: 2}

#: Cómo se le explica al comprador de dónde salió cada cosa (sin jerga).
TEXTO_FUENTE_COSTO = {
    'ACUERDO_VIGENTE': 'acuerdo vigente',
    'COTIZACION': 'cotización',
    'OC_SIESA': 'última orden de compra',
    'KARDEX_PROMEDIO': 'costo promedio de compras pasadas',
    'MAESTRO': 'maestro de productos',
    'SIN_COSTO': 'sin precio conocido',
}
TEXTO_FUENTE_EMPAQUE = {
    'OC_SIESA': 'empaque de la última orden de compra',
    'MAESTRO_SIESA': 'empaque del catálogo de Siesa',
    'SIN_EMPAQUE': 'sin empaque conocido: por unidad',
}
TEXTO_FUENTE_LT = {
    'MEDIDO': 'medido con sus órdenes',
    'PARCIAL': 'medido con pocas órdenes (se usa el mayor)',
    'CONFIGURADO': 'configurado',
    'DEFAULT_CONSERVADOR': 'supuesto conservador',
}


def _urgencia(fila):
    """URGENTE = aun pidiendo hoy, se agota antes de que llegue. ESTA SEMANA =
    ya está bajo su punto de pedido. PRÓXIMAS = lo cruza dentro de
    `DIAS_PROXIMAS` días. Solo compara lo que `rop_dual` ya calculó."""
    if fila.get('bajo_rop'):
        return URGENTE if fila.get('se_agota_antes_de_llegar') else ESTA_SEMANA
    dias = fila.get('dias_hasta_punto_de_pedido')
    if dias is not None and dias <= DIAS_PROXIMAS:
        return PROXIMAS
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Franja de confianza del dato
# ══════════════════════════════════════════════════════════════════════════════

def _nivel_salud(nivel):
    return {'ok': 'ok', 'advertencia': 'aviso', 'critico': 'mal'}.get(nivel, 'aviso')


def confianza() -> dict:
    """«¿Puedo decidir con estos números?» — arriba de todas las pestañas.

    Cada renglón es el veredicto de su dueño, traducido: nada se pone verde
    por omisión. `decidir` es False si algún renglón está en `mal`."""
    from app.services import compras_fuentes
    from app.services.analitica_salud import fuente_stock
    from app.services.armador_service import (ArmadorService, ciclo_pedido_nacional,
                                              insumo_origen)
    from app.services.kardex_service import salud_kardex

    renglones = []

    # 1 · Kardex: de él sale la demanda de TODO.
    try:
        k = salud_kardex()
        prob = (k.get('problemas') or [{}])[0]
        mov = k.get('movimientos') or {}
        renglones.append({
            'clave': 'kardex',
            'nivel': 'ok' if k.get('confiable') else 'mal',
            'titulo': ('Ventas (kardex) al día' if k.get('confiable')
                       else 'Ventas (kardex) no están al día: no decidir con estos números'),
            'detalle': (f"Último movimiento: {mov.get('ultima_fecha') or 'ninguno'}"
                        if k.get('confiable') else prob.get('titulo')),
            'que_hacer': None if k.get('confiable') else prob.get('que_hacer'),
        })
    except Exception as e:                                    # noqa: BLE001
        logger.exception('[BANDEJA] salud del kardex')
        renglones.append({'clave': 'kardex', 'nivel': 'mal',
                          'titulo': 'No se pudo verificar el kardex: no decidir con estos números',
                          'detalle': str(e)[:200], 'que_hacer': None})

    # 2 · Existencias por sede.
    try:
        fs = fuente_stock(datetime.utcnow())
        bodegas = (fs.get('detalle') or {}).get('bodegas') or []
        renglones.append({
            'clave': 'existencias',
            'nivel': _nivel_salud(fs.get('nivel')),
            'titulo': f"Existencias por sede: {fs.get('veredicto_texto') or fs.get('veredicto')}",
            'detalle': fs.get('motivo'),
            'que_hacer': fs.get('que_hacer'),
            'sedes': [{'bodega': b.get('bodega'), 'minutos': b.get('edad_operativa_min'),
                       'atrasada': bool(b.get('atrasada')), 'sin_dato': not b.get('filas')}
                      for b in bodegas],
        })
    except Exception as e:                                    # noqa: BLE001
        logger.exception('[BANDEJA] frescura de existencias')
        renglones.append({'clave': 'existencias', 'nivel': 'mal',
                          'titulo': 'No se pudo leer la frescura de las existencias',
                          'detalle': str(e)[:200], 'que_hacer': None})

    # 3 · Órdenes de compra sincronizadas (lo que viene).
    try:
        f = compras_fuentes.frescura_oc()
        if f.get('completa_utc'):
            renglones.append({'clave': 'ocs', 'nivel': 'ok',
                              'titulo': 'Órdenes de compra de Siesa sincronizadas',
                              'detalle': None, 'minutos': f.get('completa_hace_min'),
                              'que_hacer': None})
        else:
            renglones.append({'clave': 'ocs', 'nivel': 'aviso',
                              'titulo': 'Órdenes de compra nunca sincronizadas: «ya pedido» vale 0 porque no se sabe',
                              'detalle': f.get('nota') or f.get('error_lectura'),
                              'minutos': None,
                              'que_hacer': 'Sincronizar en 🧾 Fuentes (y encender COMPRAS_OC_SYNC en el worker).',
                              'que_hacer_otros': ('Sincronizar en 🧾 Fuentes. Para que se sincronice sola, '
                                                  'pídale a administración que la encienda.')})
    except Exception as e:                                    # noqa: BLE001
        renglones.append({'clave': 'ocs', 'nivel': 'aviso',
                          'titulo': 'No se pudo leer el estado de las órdenes de compra',
                          'detalle': str(e)[:200], 'que_hacer': None})

    # 4 · Origen (China / nacional) y fichas de importación.
    try:
        ins = insumo_origen()
        g5 = ArmadorService.verificar_g5()
        fichas = g5.get('g5_1') or {}
        if not ins.get('regimen_china_operativo'):
            renglones.append({
                'clave': 'origen', 'nivel': 'mal',
                'titulo': 'Ningún producto tiene origen: no se sabe qué se trae de China',
                'detalle': (f"{ins.get('skus_sin_origen')} productos sin origen. El contenedor no se puede calcular."
                            if ins.get('skus_totales') else 'No hay productos en el catálogo del WMS.'),
                'que_hacer': 'Cargar origen y marca en 🧾 Fuentes.'})
        else:
            renglones.append({
                'clave': 'origen',
                'nivel': 'ok' if fichas.get('ok') else 'aviso',
                'titulo': ('Origen y fichas de importación cargados' if fichas.get('ok')
                           else 'Faltan fichas de importación (caja, CBM, peso)'),
                'detalle': f"{ins.get('skus_sin_origen')} productos sin origen · fichas: {fichas.get('detalle')}",
                'que_hacer': None if fichas.get('ok') else 'Cargar fichas en 🧾 Fuentes.'})
    except Exception as e:                                    # noqa: BLE001
        renglones.append({'clave': 'origen', 'nivel': 'aviso',
                          'titulo': 'No se pudo leer el origen de los productos',
                          'detalle': str(e)[:200], 'que_hacer': None})

    # 5 · Supuestos declarados (lead time nacional y ciclo de compra).
    try:
        lt = compras_fuentes.lead_time(origen=compras_fuentes.ORIGEN_NACIONAL)
        ciclo = ciclo_pedido_nacional()
        supuesto = lt.get('fuente') == 'DEFAULT_CONSERVADOR' or ciclo.get('fuente') != 'CONFIGURADO'
        renglones.append({
            'clave': 'supuestos', 'nivel': 'aviso' if supuesto else 'ok',
            'titulo': ('Tiempo de entrega nacional y ciclo de compra supuestos' if supuesto
                       else 'Tiempo de entrega y ciclo de compra medidos o configurados'),
            'detalle': (f"Entrega nacional {lt.get('lt_dias'):g} días "
                        f"({TEXTO_FUENTE_LT.get(lt.get('fuente'), 'sin fuente')}) · "
                        f"se compra cada {ciclo.get('dias')} días"),
            'que_hacer': ('Fijar ROP_LT_NACIONAL_DIAS y ROP_CICLO_NACIONAL_DIAS, o sincronizar '
                          'órdenes cumplidas para medirlo.') if supuesto else None,
            'que_hacer_otros': ('Pídale a administración que fije el tiempo de entrega y el ciclo '
                                'de compra nacional, o sincronice órdenes cumplidas en 🧾 Fuentes '
                                'para medirlo.') if supuesto else None})
    except Exception as e:                                    # noqa: BLE001
        renglones.append({'clave': 'supuestos', 'nivel': 'aviso',
                          'titulo': 'No se pudo leer el tiempo de entrega',
                          'detalle': str(e)[:200], 'que_hacer': None})

    return {
        'renglones': renglones,
        'decidir': not any(r['nivel'] == 'mal' for r in renglones if r['clave'] != 'origen'),
        'contenedor_calculable': not any(
            r['nivel'] == 'mal' for r in renglones if r['clave'] in ('kardex', 'origen')),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 🛒 Bandeja — la reposición nacional con cantidad, proveedor y precio
# ══════════════════════════════════════════════════════════════════════════════

def _falta_kardex(salud):
    """Qué falta para que la bandeja pueda proponer, y cómo encenderlo."""
    falta = [{'titulo': p.get('titulo'), 'que_hacer': p.get('que_hacer')}
             for p in (salud.get('problemas') or [])]
    falta.append({'titulo': 'Nada descarga el kardex solo mientras KARDEX_AUTO esté apagado.',
                  'que_hacer': 'En Siesa: dar permiso a la consulta del kardex (hoy 401 en QA). '
                               'En Railway: KARDEX_AUTO=true en el worker con HEAVY_SCHEDULERS=true. '
                               'Después: Inventario › Datos › Descargar y Reconstruir stock diario.',
                  'que_hacer_otros': 'Pídale a administración que habilite la descarga automática '
                                     'del kardex (el permiso en Siesa y el encendido en el servidor).'})
    return falta


def para_quien_mira(respuesta, es_admin: bool):
    """Las instrucciones que solo administración puede cumplir (variables del
    servidor, permisos en Siesa) se le dicen así a quien no lo es (e2e
    2026-09-25: la bandeja le pedía al rol compras «KARDEX_AUTO=true en el
    worker»). Donde hay `que_hacer_otros`, quien no es admin lee ese; la clave
    no viaja. Recorre la respuesta entera: dict y listas."""
    if isinstance(respuesta, list):
        return [para_quien_mira(x, es_admin) for x in respuesta]
    if not isinstance(respuesta, dict):
        return respuesta
    salida = {k: para_quien_mira(v, es_admin) for k, v in respuesta.items()
              if k != 'que_hacer_otros'}
    if 'que_hacer_otros' in respuesta and not es_admin:
        salida['que_hacer'] = respuesta['que_hacer_otros']
    return salida


def _porque(fila, pedido, nivel_servicio):
    """La aritmética en palabras: los números del motor, sin recalcular."""
    return {
        'existencia': fila.get('stock_actual'),
        'comprometido': fila.get('comprometido'),
        'salida_sin_confirmar': fila.get('salida_sin_conf'),
        'disponible': fila.get('disponible'),
        'ya_pedido': fila.get('en_transito'),
        'posicion': fila.get('posicion'),
        'vende_dia': fila.get('d_avg_diaria'),
        'punto_de_pedido': fila.get('rop'),
        'reserva_seguridad': fila.get('safety_stock'),
        'nivel_objetivo': fila.get('nivel_objetivo'),
        'falta_para_objetivo': fila.get('deficit'),
        'redondeo_empaque': pedido.get('redondeo_unidades'),
        'entrega_dias': fila.get('lt_dias'),
        'entrega_fuente': TEXTO_FUENTE_LT.get(fila.get('lt_fuente'), fila.get('lt_fuente')),
        'entrega_observaciones': fila.get('lt_n'),
        'ciclo_dias': fila.get('ciclo_dias'),
        'meta_servicio': nivel_servicio,
        'faltan_datos_de_agotados': bool(fila.get('censurado')),
        'dias_con_existencias': fila.get('dias_con_stock'),
        'existencias_de_hace_dias': fila.get('stock_dias_de_antiguedad'),
    }


def bandeja(nivel_servicio: float = NIVEL_SERVICIO) -> dict:
    """La bandeja: por SKU nacional bajo su punto de pedido (o por cruzarlo),
    cuánto pedir, a quién, a qué precio y por qué, agrupado por proveedor."""
    from app.services import compras_fuentes
    from app.services.armador_service import ArmadorService, pedido_en_empaques
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    from app.services.bodegas import co_de_bodega
    from app.services.costo_service import resolver_costos, sumar_valores, valorizar
    from app.services.kardex_service import salud_kardex

    salud = salud_kardex()
    base = {'nivel_servicio': nivel_servicio, 'proveedores': [], 'excluidos': {},
            'kardex_confiable': bool(salud.get('confiable')),
            'generado_utc': datetime.utcnow().isoformat()}
    if not (salud.get('movimientos') or {}).get('total'):
        return dict(base, estado='SIN_KARDEX', falta=_falta_kardex(salud),
                    resumen={'lineas': 0})

    rop = ArmadorService.rop_dual(nivel_servicio=nivel_servicio)
    nac = rop.get('nacional') or {}
    filas = nac.get('items') or []
    if not filas:
        return dict(base, estado='SIN_DEMANDA', falta=_falta_kardex(salud),
                    resumen={'lineas': 0})

    con_urgencia = [(f, _urgencia(f)) for f in filas]
    candidatas = [(f, u) for f, u in con_urgencia
                  if u is not None and not f.get('sin_venta_reciente')]
    parados = [f for f, u in con_urgencia if u is not None and f.get('sin_venta_reciente')]
    sin_stock = [f for f, _u in candidatas if f.get('frescura_stock') is None]
    candidatas = [(f, u) for f, u in candidatas if f.get('frescura_stock') is not None]

    refs = [f['referencia'] for f, _u in candidatas]
    bloqueos = BloqueoRecompraService.verificar_oc(refs).get('bloqueados') or []
    bloqueados = {b['codigo'] for b in bloqueos}
    candidatas = [(f, u) for f, u in candidatas if f['referencia'] not in bloqueados]
    refs = [f['referencia'] for f, _u in candidatas]

    empaques = compras_fuentes.empaque_de_compra(refs)
    costos = resolver_costos(refs) if refs else {}
    habitual = compras_fuentes.proveedor_habitual(refs) if refs else {}
    info_prov = compras_fuentes.proveedores_info(sorted(set(habitual.values())))
    bodega = compras_fuentes._bodega_cdi()
    destino = {'bodega': bodega, 'co': co_de_bodega(bodega)}

    lineas = []
    for f, urg in candidatas:
        ref = f['referencia']
        emp = empaques.get(ref) or {}
        pedido = pedido_en_empaques(f.get('deficit') or 0, emp.get('unidades_por_empaque'),
                                    emp.get('moq_empaques'))
        if not pedido['unidades']:
            continue
        c = costos.get(ref) or {}
        val = valorizar(pedido['unidades'], c)
        lineas.append({
            'referencia': ref,
            'nombre': c.get('nombre') or None,
            'urgencia': urg,
            'pedir_unidades': pedido['unidades'],
            'pedir_empaques': pedido['empaques'],
            'empaque': {'unidad': emp.get('unidad') or 'UND',
                        'unidades_por_empaque': pedido['unidades_por_empaque'],
                        'moq_empaques': pedido['moq_empaques'],
                        'moq_conocido': emp.get('moq_fuente') not in (None, 'SIN_DATO'),
                        'fuente': TEXTO_FUENTE_EMPAQUE.get(emp.get('fuente'), 'sin empaque conocido'),
                        'oc': emp.get('oc')},
            'precio': {'unitario_cop': val['costo_unitario'],
                       'fuente': TEXTO_FUENTE_COSTO.get(val['fuente'], val['fuente']),
                       'fuente_codigo': val['fuente'],
                       'fecha': c.get('fecha_costo') or c.get('vigencia_hasta'),
                       'viejo': bool(c.get('anejo')),
                       'convertido_de_usd': bool(c.get('conversion'))},
            'valor_cop': val['valor_cop'],
            'alcanza_dias': f.get('cobertura_dias'),
            'entrega_dias': f.get('lt_dias'),
            'dias_hasta_punto_de_pedido': f.get('dias_hasta_punto_de_pedido'),
            'fecha_entrega_sugerida': f.get('llegaria_el'),
            'proveedor_codigo': habitual.get(ref),
            'porque': _porque(f, pedido, nivel_servicio),
        })

    grupos = {}
    for l in lineas:
        grupos.setdefault(l['proveedor_codigo'], []).append(l)
    proveedores = []
    for cod, ls in grupos.items():
        ls.sort(key=lambda l: (ORDEN_URGENCIA[l['urgencia']], l['alcanza_dias']))
        info = info_prov.get(cod) or {}
        sub = sumar_valores([l['valor_cop'] for l in ls])
        proveedores.append({
            'codigo': cod,
            'nombre': info.get('nombre') if cod else None,
            'nit': info.get('nit') if cod else None,
            'conocido': cod is not None,
            'lineas': ls,
            'urgentes': len([l for l in ls if l['urgencia'] == URGENTE]),
            'subtotal_cop': sub['total_cop'],
            'lineas_sin_precio': sub['sin_valor'],
            'subtotal_es_cota_inferior': sub['es_cota_inferior'],
        })
    proveedores.sort(key=lambda p: (not p['conocido'], p['urgentes'] == 0,
                                    ORDEN_URGENCIA[p['lineas'][0]['urgencia']],
                                    p['nombre'] or ''))

    total = sumar_valores([l['valor_cop'] for l in lineas])
    return dict(
        base,
        estado='OK',
        destino=destino,
        ciclo=nac.get('ciclo'),
        entrega_nacional={'dias': nac.get('lt_dias'),
                          'fuente': TEXTO_FUENTE_LT.get(nac.get('lt_fuente'), nac.get('lt_fuente'))},
        proveedores=proveedores,
        resumen={
            'lineas': len(lineas),
            'urgentes': len([l for l in lineas if l['urgencia'] == URGENTE]),
            'esta_semana': len([l for l in lineas if l['urgencia'] == ESTA_SEMANA]),
            'proximas': len([l for l in lineas if l['urgencia'] == PROXIMAS]),
            'proveedores': len(proveedores),
            'sin_proveedor': len(grupos.get(None) or []),
            'valor_total_cop': total['total_cop'],
            'lineas_sin_precio': total['sin_valor'],
            'valor_es_cota_inferior': total['es_cota_inferior'],
            'china_van_por_contenedor': (rop.get('china') or {}).get('total') or 0,
        },
        excluidos={
            'bloqueados': [{'referencia': b['codigo'], 'nombre': b.get('nombre'),
                            'motivo': b.get('motivo')} for b in bloqueos],
            'sin_existencias_conocidas': [{'referencia': f['referencia']} for f in sin_stock],
            'sin_venta_reciente': [{'referencia': f['referencia'],
                                    'motivo': f.get('motivo_d_cero')} for f in parados],
        },
    )


def explicar_sku(referencia: str) -> dict:
    """¿Por qué esta referencia está (o no) en la bandeja? — para el enlace
    desde «Venta perdida por agotados» cuando la fila no aparece."""
    from app.services.armador_service import ArmadorService
    from app.services.bloqueo_recompra_service import BloqueoRecompraService
    ref = (referencia or '').strip()
    rop = ArmadorService.rop_dual(nivel_servicio=NIVEL_SERVICIO)
    nac = {f['referencia']: f for f in (rop.get('nacional') or {}).get('items') or []}
    chi = {f['referencia'] for f in (rop.get('china') or {}).get('items') or []}
    bloq = BloqueoRecompraService.verificar_oc([ref]).get('bloqueados') or []
    if bloq:
        return {'referencia': ref, 'motivo': 'BLOQUEADO', 'texto': 'Está bloqueado para recompra.'}
    if ref in chi:
        return {'referencia': ref, 'motivo': 'CHINA', 'texto': 'Se trae de China: va por 🚢 Contenedor.'}
    f = nac.get(ref)
    if f is None:
        return {'referencia': ref, 'motivo': 'SIN_DEMANDA',
                'texto': 'No tiene ventas en el kardex de los últimos 12 meses.'}
    if f.get('sin_venta_reciente'):
        return {'referencia': ref, 'motivo': 'SIN_VENTA_RECIENTE', 'texto': f.get('motivo_d_cero')}
    urg = _urgencia(f)
    if urg is not None:
        return {'referencia': ref, 'motivo': 'EN_BANDEJA', 'urgencia': urg,
                'texto': 'Está en la bandeja.'}
    return {'referencia': ref, 'motivo': 'SOBRE_PUNTO_DE_PEDIDO',
            'dias_hasta_punto_de_pedido': f.get('dias_hasta_punto_de_pedido'),
            'alcanza_dias': f.get('cobertura_dias'),
            'texto': 'Hoy no está bajo su punto de pedido.'}


# ══════════════════════════════════════════════════════════════════════════════
# 🚢 Contenedor
# ══════════════════════════════════════════════════════════════════════════════

def contenedor(tipo: str = '40STD') -> dict:
    """La propuesta de contenedor, o —si no se puede calcular— por qué no.
    Sin origen o sin fichas NO se devuelve una propuesta que parezca real."""
    from app.services.armador_service import (ArmadorService, composicion_por_proveedor,
                                              insumo_origen, tipos_contenedor)
    from app.services.temporada_service import fechas_limite_pedido

    ins = insumo_origen()
    limites = fechas_limite_pedido()
    base = {'tipos': tipos_contenedor(), 'tipo': tipo, 'insumo_origen': ins,
            'temporada': limites}
    if not ins.get('regimen_china_operativo'):
        return dict(base, estado='SIN_ORIGEN', skus_sin_origen=ins.get('skus_sin_origen'),
                    titulo='No se puede calcular el contenedor: ningún producto tiene origen',
                    que_hacer='Cargar el origen (China / nacional) y la marca en 🧾 Fuentes.')

    p = ArmadorService.armar_contenedor(tipo)
    excl = p.get('excluidos') or []
    sin_ficha = [e for e in excl if e.get('motivo') in ('SIN_FICHA', 'FICHA_ESTIMADA')]
    items = p.get('items') or []
    grupos = composicion_por_proveedor(items)
    ventana = p.get('ventana_llegada') or {}
    china = limites['por_origen'].get('CHINA') or {}
    llega_tarde = bool(ventana.get('hasta') and not limites.get('en_curso')
                       and ventana['hasta'] >= limites['inicio'])
    comun = dict(base, sin_ficha=len(sin_ficha),
                 sin_ficha_refs=[e.get('referencia') for e in sin_ficha][:50],
                 modo=p.get('modo'), cobertura_fichas_pct=p.get('cobertura_fichas_pct'),
                 # P0-10: la aptitud la decide `aptitud_de_la_propuesta`.
                 apta=p.get('apta'), no_apta_por=p.get('no_apta_por') or [])
    if not items and sin_ficha:
        return dict(comun, estado='SIN_FICHAS',
                    titulo=f'No se puede armar: {len(sin_ficha)} productos de China con faltante no tienen ficha verificada',
                    que_hacer='Cargar las fichas (unidades por caja, CBM, peso) en 🧾 Fuentes.')
    if not items:
        return dict(comun, estado='SIN_FALTANTE',
                    titulo='Hoy no hace falta contenedor: ningún producto de China está por debajo de su nivel objetivo.')
    return dict(
        comun,
        estado='PROPUESTA',
        contenedor_etiqueta=p.get('contenedor_etiqueta'),
        proveedores=grupos,
        barras=p.get('barras'),
        valor_fob_usd=p.get('valor_fob_usd'),
        valor_nacionalizado_cop=p.get('valor_nacionalizado_cop_estimado'),
        conversion=p.get('conversion_moneda'),
        ventana_llegada=ventana,
        # El lead time de la ventana es el de China por origen (el mismo con
        # que `armar_contenedor` arma `ventana_llegada`).
        lead_time={'dias': china.get('lt_dias'), 'variacion_dias': china.get('sigma_lt'),
                   'fuente': TEXTO_FUENTE_LT.get(china.get('fuente'), china.get('fuente'))},
        alerta_temporada=({
            'titulo': 'Llega con la temporada escolar ya empezada',
            'temporada_inicio': limites['inicio'],
            'fecha_limite_pedido': china.get('fecha_limite'),
            'vencida': china.get('vencida'),
        } if llega_tarde else None),
        moq_interpretacion=p.get('moq_interpretacion'),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 🎒 Temporada
# ══════════════════════════════════════════════════════════════════════════════

def temporada() -> dict:
    """tener − hay − viene = pedir, por SKU, con su fecha límite por origen y
    conciliado con el contenedor (una cifra por SKU)."""
    from app.services.armador_service import ArmadorService, insumo_origen
    from app.services.temporada_service import (TemporadaService, conciliar_con_contenedor,
                                                fechas_limite_pedido)
    limites = fechas_limite_pedido()
    r = TemporadaService.preparar_pedido_temporada()
    if r.get('error'):
        return {'estado': 'SIN_DATOS', 'titulo': r['error'], 'temporada': limites, 'filas': []}
    filas = [f for f in (r.get('items') or []) if not f.get('error')]
    conciliacion = {}
    nota_conc = None
    if insumo_origen().get('regimen_china_operativo'):
        try:
            prop = ArmadorService.armar_contenedor()
            conciliacion = conciliar_con_contenedor(filas, prop.get('items') or [])
        except Exception as e:                                # noqa: BLE001
            logger.exception('[BANDEJA] conciliación temporada/contenedor')
            nota_conc = f'No se pudo cruzar con el contenedor: {str(e)[:160]}'
    salida = []
    for f in filas:
        ref = f.get('referencia')
        origen = (f.get('origen') or '').upper() or None
        salida.append({
            'referencia': ref, 'nombre': f.get('nombre'),
            'tener': f.get('tener'), 'hay': f.get('hay'), 'viene': f.get('viene'),
            'pedir': f.get('pedir'), 'hay_sin_dato': bool(f.get('hay_sin_dato')),
            'inversion_cop': f.get('inversion_pedido'),
            'precio_fuente': TEXTO_FUENTE_COSTO.get(f.get('fuente_costo'), f.get('fuente_costo')),
            'faltan_datos_de_agotados': bool(f.get('demanda_censurada')),
            'una_sola_temporada': bool(f.get('advertencia_1_temporada')),
            'contenedor': conciliacion.get(ref),
            'origen': origen,
        })
    salida.sort(key=lambda x: (x['pedir'] or 0) == 0)
    cob = r.get('cobertura') or {}
    return {
        'estado': 'OK', 'temporada': limites, 'filas': salida,
        'total_pedir_unidades': r.get('total_pedir_unidades'),
        'total_inversion_cop': r.get('total_inversion_pedido'),
        'cubiertos': cob.get('cubiertos_por_modelo'), 'skus_temporada': cob.get('skus_temporada'),
        'advertencia_cobertura': cob.get('advertencia'),
        'nota_conciliacion': nota_conc,
        'nota_hay': 'Lo que hay es de hoy: lo que se venda de aquí a diciembre no está descontado.',
    }


# ══════════════════════════════════════════════════════════════════════════════
# 📦 Lo pedido
# ══════════════════════════════════════════════════════════════════════════════

def lo_pedido(dias_llegadas: int = 30) -> dict:
    """OCs abiertas por proveedor (atrasadas primero), lo que llegó y la
    deriva de precios contra los acuerdos. Una sección que falla se declara;
    la pantalla no recibe un 500."""
    from app.services import compras_fuentes
    from app.services.compras_inteligencia_service import ComprasInteligenciaService

    def _seguro(nombre, fn):
        try:
            return fn(), None
        except Exception as e:                                # noqa: BLE001
            logger.exception('[BANDEJA] lo pedido: %s', nombre)
            return None, f'No se pudo leer {nombre}: {str(e)[:160]}'

    ocs, e1 = _seguro('las órdenes abiertas', compras_fuentes.ocs_abiertas)
    llegadas, e2 = _seguro('las llegadas', lambda: compras_fuentes.llegadas_recientes(dias_llegadas))
    deriva, e3 = _seguro('la deriva de precios', lambda: ComprasInteligenciaService.detectar_deriva(3))
    return {'ocs': ocs, 'llegadas': llegadas, 'deriva': deriva,
            'errores': [e for e in (e1, e2, e3) if e]}
