"""
Sincronización del catálogo de productos Siesa → WMS.

El sync puede tardar minutos (múltiples páginas × llamadas HTTP).
Para evitar timeout de gunicorn (30 seg), el sync manual corre en hilo de fondo
y el endpoint retorna inmediatamente con el estado.

## El contrato es `docs/siesa-specs/API_v2_Items.docx` — 31 campos `f120_*`

Este módulo leía dos campos que ese contrato **no declara**, y ninguno de los
dos fallaba: `.get()` devuelve el default y el default se escribía.

· `f120_ind_estado` → no existe. `activo` salía `True` siempre y el update lo
  escribía: **cada corrida deshacía la desactivación manual de un admin**
  (`productos.py::eliminar_producto`), sin log y sin aviso. Ahora, si la fuente
  no trae el estado, `activo` no se toca, y la corrida declara cuántas filas
  vinieron sin él (`sin_estado_en_origen`).

· `f120_id_unidad_medida_inventario` → el nombre real es
  `f120_id_unidad_inventario`. **Todo el catálogo nacía en 'UND'.** No hay
  backfill: la unidad se corrige sola en el próximo sync, por el mismo camino
  que trajo el error.

Trinquete: `tests/test_sync_catalogo_vs_contrato.py` cruza por AST todos los
`f120_*` que este archivo lee contra los del `.docx`. Agregar un campo al sync
sin cruzarlo contra el contrato pone el build en rojo.

## El barrido declara si terminó — y ahora puede terminar

Medido contra Postgres real con el catálogo de 28.000: **138 de 280 páginas en
307 s**, abortado por la cota temporal. Con el delay entre páginas en su default
(0,5 s × 280 = +140 s) y la latencia real de Connekta (+56 a +140 s), la corrida
real **abortaba siempre**, hacia la página 210-225, con el 75-80% del catálogo —
y el `resultado` no traía ningún campo de completitud, así que
`/api/siesa/sync-estado` publicaba eso como una corrida exitosa de 22.000 ítems.

Dos cosas, y hacen falta las dos:

· `paginacion_completa` + `motivo_incompleta` en el resultado, con la misma
  forma que `pedidos_sync_service` y `get_ordenes_compra_aprobadas` — hay tres
  finales cortos (tiempo, API caída, tope de páginas) y ninguno puede devolver
  lo mismo que «se agotó el catálogo».

· El N+1 del lookup de productos, que era el 100% de lo que consumía la cota:
  **200 SELECT por página** medidos, contra 2 ahora (`_mapas_de_productos`).
  Declarar sin esto habría dado un sync que declara, correctamente, que nunca
  termina.

Trinquete: `tests/test_sync_catalogo_completitud.py`, que mide las sentencias
SQL por página y exige que no crezcan con el número de ítems.
"""
import logging
import os
import threading
from datetime import datetime, timezone
from app.extensions import db
from app.models.producto import Producto
from app.models.siesa_mapeo_unidades import SiesaMapeoUnidades
from app.services.connekta_gateway import connekta, _exigir_datos

logger = logging.getLogger(__name__)

# Estado global del sync (acceso desde ruta y scheduler)
_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,   # dict con estadísticas del último sync exitoso
    'ultimo_error': None,
}
_MIN_INTERVALO_SEG = 4 * 60  # 4 min — deja margen al scheduler de 5 min

#: Cota temporal del barrido. **Módulo y no local a `_run_sync`** para que un
#: test pueda bajarla y ejercer el aborto: una cota que solo se puede probar
#: esperando cinco minutos no se prueba nunca, y ésta abortaba TODAS las
#: corridas reales sin que un solo test lo viera.
_MAX_MINUTOS_PAGINACION = 5

#: Tope de páginas. 500 × 100 = 50.000 ítems; el catálogo real son 28.000.
#: Agotarlo no significa «se terminó» sino «hay más del otro lado» — se declara
#: igual que los otros dos finales cortos.
_MAX_PAGINAS = 500

#: Tamaño de página del contrato (Regla 10: `tamPag` máximo = 100). Una página
#: más corta que esto es la última.
_TAM_PAG = 100

#: Cuántos códigos entran en un `IN (...)`. La página trae 100, así que en la
#: práctica es un solo lote; el troceo existe para que un conector que algún día
#: devuelva páginas más grandes no arme un `IN` de miles de parámetros.
_TAM_LOTE_IN = 500

#: Respiro entre páginas. **Estaba en 0,5 s y no está definida en ningún sitio
#: del repo ni del deploy**, así que el default regía: 0,5 × 280 páginas = +140 s
#: sobre un presupuesto de 300. Se dimensionó cuando cada página emitía 250
#: sentencias SQL (ver `_mapas_de_productos`); ahora emite ~3, y el motivo del
#: throttle —dar respiro al pool que el worker comparte— se cumple con 0,1.
_SYNC_PAGE_DELAY_DEFAULT = '0.1'


def _mapas_de_productos(codigos):
    """Precarga los productos de una página en dos mapas: por `codigo_siesa` y por `codigo`.

    **Reemplaza el N+1 que hacía imposible terminar el barrido.** El código
    hacía, por ítem::

        Producto.query.filter_by(codigo_siesa=x).first()
        or Producto.query.filter_by(codigo=x).first()

    Medido: **2 SELECT por ítem**, 100-200 por página, 2,5 sentencias SQL por
    ítem contando los INSERT. Sobre 28.000 ítems son 70.000 sentencias y 2,2 s
    de base por página — el 100% de lo que consumía la cota temporal, con
    latencia HTTP en cero. Con dos `IN (...)` por página el costo deja de
    depender de cuántos ítems trae la página.

    Se devuelven **dos mapas y no uno** porque el `or` tiene una semántica que
    hay que preservar exactamente: primero `codigo_siesa`, después `codigo`. Un
    producto puede coincidir por los dos lados con **filas distintas**, y el
    sync tiene que seguir escribiendo sobre la misma que antes.

    `order_by(id)` + `setdefault` fija el desempate cuando dos productos
    comparten `codigo_siesa` (la columna es índice, no UNIQUE): antes ganaba
    «la que devolviera primero la base», que no está definido. Ahora gana la
    más antigua, que es lo que Postgres venía devolviendo.
    """
    unicos = [c for c in dict.fromkeys(codigos) if c]
    por_siesa, por_codigo = {}, {}
    for i in range(0, len(unicos), _TAM_LOTE_IN):
        lote = unicos[i:i + _TAM_LOTE_IN]
        for p in (Producto.query.filter(Producto.codigo_siesa.in_(lote))
                  .order_by(Producto.id).all()):
            por_siesa.setdefault(p.codigo_siesa, p)
        for p in (Producto.query.filter(Producto.codigo.in_(lote))
                  .order_by(Producto.id).all()):
            por_codigo.setdefault(p.codigo, p)
    return por_siesa, por_codigo


def _referencia_de(row):
    """La referencia de una fila, tolerante a basura — solo para la precarga.

    El bucle de proceso sigue leyendo `row.get(...)` dentro de su `try`, así que
    una fila que no es un dict sigue contando como `errores`. Acá no puede
    levantar: la precarga es un adelanto, no una validación, y hacerla fallar
    perdería la página entera por una fila mala.
    """
    try:
        return (row.get('f120_referencia') or '').strip()
    except AttributeError:
        return ''


def _run_sync(app):
    """Lógica real del sync — se ejecuta en un hilo separado con su propio app_context."""
    global _sync_estado

    with app.app_context():
        # Advisory lock de PostgreSQL — protege contra ejecución simultánea entre workers
        from sqlalchemy import text as _text
        lock_adquirido = db.session.execute(
            _text('SELECT pg_try_advisory_lock(:key)'), {'key': 2012}
        ).scalar()
        if not lock_adquirido:
            logger.warning('[SYNC] Otro worker ya ejecuta — omitido')
            _sync_estado['en_curso'] = False
            return

        creados = 0
        actualizados = 0
        errores = 0
        total_procesados = 0
        # Cuántas filas llegaron SIN `f120_ind_estado`. Ver el bloque de
        # `activo` más abajo: sin este número, «la fuente no trae el estado»
        # sería una creencia del docstring en vez de un hecho por corrida.
        sin_estado_en_origen = 0
        paginas_leidas = 0
        # ¿Se barrió el catálogo entero? **Sin este campo el resultado no
        # distingue «se leyó todo» de «se leyó lo que dio el tiempo»**, y
        # `/api/siesa/sync-estado` publicaba una corrida truncada al 75% como
        # exitosa. Misma forma que `pedidos_sync_service` y que
        # `get_ordenes_compra_aprobadas` — la tercera copia de esta política
        # sería la que diverge, así que se escribe igual que las otras dos.
        #
        # Empieza en `False` a propósito: completo es lo que hay que demostrar
        # (una página corta o vacía), no lo que se asume mientras nada falle.
        paginacion_completa = False
        motivo_incompleta = None

        # Se abre DESPUÉS del advisory lock: una corrida que no llegó a
        # ejecutar porque otro worker la tenía no es una corrida.
        from app.services import registro_sync_service as _reg
        _reg_id = _reg.abrir('catalogo')

        try:
            from datetime import datetime as _dt_sync
            _sync_inicio = _dt_sync.utcnow()

            # Cargar tabla de mapeo en memoria para evitar N queries por item
            mapeo_unidades = {
                m.tipo_inv_siesa: m.unidad_negocio_id
                for m in SiesaMapeoUnidades.query.all()
            }
            tipos_sin_mapeo = set()  # tipos que Siesa devuelve pero no están en nuestra tabla

            for pag in range(1, _MAX_PAGINAS + 1):
                _elapsed = (_dt_sync.utcnow() - _sync_inicio).total_seconds()
                if _elapsed > _MAX_MINUTOS_PAGINACION * 60:
                    # Se acabó el tiempo. NO es «no hay más»: quedó catálogo sin
                    # leer y hay que poder decir cuánto.
                    motivo_incompleta = (
                        f'cota temporal alcanzada tras {_elapsed:.0f}s en la '
                        f'página {pag} — {total_procesados} ítems leídos')
                    logger.warning('[SYNC] Paginación abortada: %s', motivo_incompleta)
                    break
                try:
                    resp = connekta.get_items_catalogo(pag)
                    # `_exigir_datos` en vez del `'alerta' in rows[0]` que había
                    # acá: una fila `{'alerta': ...}` es Siesa **rechazando la
                    # consulta con HTTP 200**, no una página vacía. El `break`
                    # la leía como fin de catálogo — «tu consulta fue rechazada»
                    # convertido en «no hay más productos», con la corrida
                    # reportando éxito. Es el modo de fallo que
                    # `ConnektaConsultaRechazada` existe para impedir, y la
                    # política es del gateway: acá se llama, no se reescribe.
                    rows = _exigir_datos(
                        resp.get('detalle', {}).get('Table', []),
                        'get_items_catalogo', f'numPag={pag}|tamPag={_TAM_PAG}')
                except Exception as e:
                    # La página no se pudo leer. Lo commiteado de las páginas
                    # anteriores YA está en la base: abortar por el `except`
                    # general dejaba `ultimo_resultado` en `None` y un error que
                    # no menciona los miles de productos que sí se escribieron.
                    # «Falló» y «se leyó el 40%» son afirmaciones distintas.
                    motivo_incompleta = f'la página {pag} falló: {e}'
                    logger.error('[SYNC] %s', motivo_incompleta)
                    break

                if not rows:
                    # Página vacía = se acabó el catálogo. Uno de los dos únicos
                    # caminos que dejan el barrido completo.
                    paginacion_completa = True
                    break

                # Precarga de la página: dos SELECT en vez de dos por ítem.
                por_siesa, por_codigo = _mapas_de_productos(
                    [_referencia_de(r) for r in rows])

                for row in rows:
                    try:
                        codigo_siesa = (row.get('f120_referencia') or '').strip()
                        nombre = (row.get('f120_descripcion') or '').strip()
                        # `activo`: el contrato NO trae estado — no se inventa.
                        #
                        # `API_v2_Items.docx` declara 31 campos y ninguno es un
                        # estado del ítem (`f120_ind_compra`/`f120_ind_venta`
                        # existen, pero dicen «se compra»/«se vende», no «está
                        # vigente» — usarlos sería un trinquete que mide una
                        # proxy). Este código leía `f120_ind_estado` con default
                        # `'1'`, así que `activo` salía `True` SIEMPRE y el
                        # update lo escribía sobre el producto.
                        #
                        # Lo que costaba: `productos.py::eliminar_producto` pone
                        # `activo = False` — un admin desactivando a mano. **Cada
                        # corrida del sync deshacía esa decisión**, sin log y sin
                        # aviso. Un default que revierte al operario no es el
                        # lado conservador de la Regla 0: es el destructivo.
                        #
                        # Decisión: `None` significa «la fuente no dijo nada» y
                        # abajo no se escribe nada. Si algún día un conector
                        # personalizado sí manda el campo, ese SÍ es dato de la
                        # fuente y manda — la Regla 0 gobierna el hueco, no lo
                        # que llegó. Las filas sin el campo se cuentan y la
                        # corrida lo declara en `sin_estado_en_origen`.
                        #
                        # **La clave presente y vacía no es un dato.** Esto
                        # preguntaba `'f120_ind_estado' in row`, es decir si
                        # llegó la CLAVE. Con `''` o `None` la clave está,
                        # `str(...) == '1'` da `False`, y el update de abajo
                        # escribe `activo = False` en TODAS las filas: **el
                        # catálogo entero desactivado en una corrida**, sin un
                        # error, sin un aviso y con el sync reportando éxito.
                        # Todo lo desactivado desaparece del picking.
                        # «La clave llegó vacía» es el mismo hueco de la Regla 0
                        # con otra forma: se trata como ausencia y se cuenta.
                        _estado_crudo = row.get('f120_ind_estado')
                        _estado = '' if _estado_crudo is None else str(_estado_crudo).strip()
                        trae_estado = _estado != ''
                        activo = (_estado == '1') if trae_estado else None
                        # `f120_id_unidad_inventario`, NO `..._unidad_medida_...`:
                        # ese segundo nombre no existe en el contrato, así que la
                        # unidad caía siempre al default fijo y **todo el catálogo
                        # nacía en 'UND'** aunque Siesa trajera CJA, PAQ o RES.
                        # Que fue un dedazo lo prueba este mismo archivo: los otros
                        # tres campos de unidad sí están bien nombrados. Y agrava
                        # el defecto de doble unidad (misma referencia en PQ y UND,
                        # `despacho_parcial_service.py:102-106`): con la unidad
                        # siempre en UND, son indistinguibles por ese campo.
                        unidad_medida = (row.get('f120_id_unidad_inventario') or '').strip() or None
                        tipo_inv = (row.get('f120_id_tipo_inv_serv') or '').strip()
                        unidad_negocio = mapeo_unidades.get(tipo_inv) if tipo_inv else None
                        # Código de barras EAN — campo puede variar según conector Siesa
                        codigo_barras = (row.get('f120_codigo_barras') or
                                        row.get('f120_id_barras') or
                                        row.get('f120_barras') or '').strip() or None

                        # Unidad de empaque y factor de conversión
                        # Siesa puede exponer el factor bajo distintos alias — probamos en orden
                        unidad_empaque = (row.get('f120_id_unidad_empaque') or '').strip() or None
                        factor_raw = (row.get('f421_factor') or
                                      row.get('f121_factor') or
                                      row.get('factor') or None)
                        try:
                            factor_conversion = int(float(factor_raw)) if factor_raw else 1
                        except (ValueError, TypeError):
                            factor_conversion = 1
                        if factor_conversion < 1:
                            factor_conversion = 1

                        if tipo_inv and unidad_negocio is None:
                            tipos_sin_mapeo.add(tipo_inv)

                        if not codigo_siesa:
                            continue

                        total_procesados += 1
                        # Se cuenta acá y no arriba para que el aviso diga «N de
                        # total_procesados»: una fila descartada por no traer
                        # referencia no es una fila sin estado, y mezclarlas
                        # daría una proporción que no cuadra con nada.
                        if not trae_estado:
                            sin_estado_en_origen += 1

                        # Mismo `or` de antes, mismo orden, sin la consulta:
                        # `por_siesa` primero, `por_codigo` después.
                        prod = por_siesa.get(codigo_siesa) or por_codigo.get(codigo_siesa)

                        if prod:
                            changed = False
                            if nombre and prod.nombre != nombre:
                                prod.nombre = nombre
                                changed = True
                            if prod.codigo_siesa != codigo_siesa:
                                prod.codigo_siesa = codigo_siesa
                                changed = True
                                # El mapa refleja la escritura: si otra fila de
                                # esta misma página trae la misma referencia,
                                # tiene que encontrar ESTE producto. Antes lo
                                # resolvía el autoflush de la consulta; con la
                                # precarga hay que mantenerlo a mano.
                                por_siesa.setdefault(codigo_siesa, prod)
                            # `activo is None` = la fuente no trajo el estado.
                            # No tocar: lo que hay en la base puede ser una
                            # decisión de un admin, y el sync no la revierte.
                            if activo is not None and prod.activo != activo:
                                prod.activo = activo
                                changed = True
                            if unidad_medida and prod.unidad_medida != unidad_medida:
                                prod.unidad_medida = unidad_medida
                                changed = True
                            # Solo sobreescribir si el mapeo existe; si no, no borrar lo que haya
                            if unidad_negocio and prod.unidad_negocio_id != unidad_negocio:
                                prod.unidad_negocio_id = unidad_negocio
                                changed = True
                            if codigo_barras and prod.codigo_barras != codigo_barras:
                                prod.codigo_barras = codigo_barras
                                changed = True
                            if unidad_empaque and prod.unidad_empaque != unidad_empaque:
                                prod.unidad_empaque = unidad_empaque
                                changed = True
                            if factor_conversion > 1 and prod.factor_conversion != factor_conversion:
                                prod.factor_conversion = factor_conversion
                                changed = True
                            if changed:
                                actualizados += 1
                        else:
                            prod = Producto(
                                codigo=codigo_siesa,
                                nombre=nombre or f'Producto {codigo_siesa}',
                                codigo_siesa=codigo_siesa,
                                # Un alta no tiene decisión previa que respetar:
                                # nace activa, como siempre. Lo que cambió es que
                                # el sync ya no pisa lo que un humano decidió
                                # DESPUÉS. Nacer inactivo sería el otro extremo:
                                # un producto invisible para picking, que es el
                                # agotado que la Regla 0 llama carísimo.
                                activo=True if activo is None else activo,
                                clasificacion_abc='C',
                                unidad_medida=unidad_medida or 'UND',
                                unidad_negocio_id=unidad_negocio,
                                codigo_barras=codigo_barras,
                                unidad_empaque=unidad_empaque,
                                factor_conversion=factor_conversion,
                            )
                            db.session.add(prod)
                            creados += 1
                            # Idem: dos filas de la misma página con la misma
                            # referencia son una creación y un update, no dos
                            # creaciones. `codigo` es UNIQUE — con una foto
                            # vieja, la segunda fila crearía un duplicado y el
                            # `commit()` de la página reventaría entero: 100
                            # productos perdidos por una referencia repetida.
                            por_siesa[codigo_siesa] = prod
                            por_codigo.setdefault(codigo_siesa, prod)

                    except Exception as e:
                        logger.warning(f'[SYNC] Item inválido: {e}')
                        errores += 1

                db.session.commit()
                paginas_leidas += 1
                logger.info(f'[SYNC] Página {pag}: {len(rows)} items · creados={creados}')

                if len(rows) < _TAM_PAG:
                    # Página corta = última página. El otro de los dos únicos
                    # caminos que dejan el barrido completo.
                    paginacion_completa = True
                    break

                # Throttle: dar respiro a la DB entre páginas (worker comparte pool)
                import time as _time_sync
                _time_sync.sleep(float(os.environ.get(
                    'SYNC_PAGE_DELAY_S', _SYNC_PAGE_DELAY_DEFAULT)))
            else:
                # Se agotaron las páginas sin que ninguna viniera corta: la
                # última llegó llena, así que hay más del otro lado. Mismo `else`
                # del `for` que `get_ordenes_compra_aprobadas`.
                motivo_incompleta = (
                    f'se agotó el tope de {_MAX_PAGINAS} páginas con '
                    f'{total_procesados} ítems leídos — el catálogo es mayor')
                logger.warning('[SYNC] %s', motivo_incompleta)

        except Exception as e:
            logger.error(f'[SYNC] Error durante sync: {e}')
            db.session.rollback()
            _sync_estado['en_curso'] = False
            _sync_estado['ultimo_error'] = str(e)
            _reg.cerrar_error(_reg_id, e)
            return
        finally:
            if lock_adquirido:
                try:
                    db.session.execute(_text('SELECT pg_advisory_unlock(:key)'), {'key': 2012})
                    db.session.commit()
                except Exception as _e:
                    logger.error('[SYNC] Error liberando advisory lock: %s', _e)

        if tipos_sin_mapeo:
            lista = sorted(tipos_sin_mapeo)
            logger.warning(
                f'[SYNC] ALERTA: {len(lista)} tipo(s) de inventario Siesa sin mapeo. '
                f'Insertados en siesa_mapeo_unidades con unidad_negocio_id=NULL. '
                f'Completa el mapeo en /api/config/mapeo-unidades: {lista}'
            )
            # Auto-insertar tipos desconocidos con unidad_negocio_id=NULL
            # El admin los ve en la pestaña Siesa → Unidades de negocio, marcados
            # en rojo, y los completa ahí. Esa pantalla no existió hasta el
            # 2026-08-05: el sync venía creando filas para un gesto inexistente.
            for tipo in lista:
                existe = SiesaMapeoUnidades.query.filter_by(tipo_inv_siesa=tipo).first()
                if not existe:
                    db.session.add(SiesaMapeoUnidades(
                        tipo_inv_siesa=tipo,
                        unidad_negocio_id='',  # vacío — admin debe completar
                        descripcion='Auto-descubierto por sync — asignar Unidad de Negocio'
                    ))
            db.session.commit()

        if sin_estado_en_origen:
            # UN aviso por corrida, no uno por fila: 28.000 líneas repitiendo lo
            # mismo es cómo un canal de advertencias deja de leerse, y entonces
            # el único aviso real se vuelve invisible.
            logger.warning(
                '[SYNC] %s de %s filas llegaron sin f120_ind_estado — el campo '
                'no existe en API_v2_Items. `activo` NO se sincroniza: lo que '
                'un admin desactive a mano se queda desactivado.',
                sin_estado_en_origen, total_procesados)

        if not paginacion_completa:
            logger.error(
                '[SYNC] BARRIDO INCOMPLETO (%s). %s páginas, %s ítems: el resto '
                'del catálogo NO se leyó. Los productos no leídos conservan lo '
                'que ya tenían — no se borra ni se desactiva nada por no haber '
                'llegado a leerlo.',
                motivo_incompleta, paginas_leidas, total_procesados)

        resultado = {
            'timestamp': datetime.utcnow().isoformat(),
            'total_procesados': total_procesados,
            'creados': creados,
            'actualizados': actualizados,
            'errores': errores,
            'tipos_sin_mapeo': sorted(tipos_sin_mapeo) if tipos_sin_mapeo else [],
            # Cuántas filas no traen estado. Con el contrato actual son todas;
            # el día que deje de serlo, este número lo dice sin que nadie tenga
            # que acordarse de revisar el DOCX.
            'sin_estado_en_origen': sin_estado_en_origen,
            'paginas_leidas': paginas_leidas,
            # **El denominador.** `total_procesados: 22.000` sobre un catálogo de
            # 28.000 se leía como el catálogo entero: un conteo sin su base no
            # es una medición. `paginacion_completa` dice si el barrido terminó
            # y `motivo_incompleta` distingue las tres formas de no terminar
            # —tiempo, API caída, tope de páginas—, porque «no sé» y «no hay
            # más» no pueden devolver lo mismo.
            'paginacion_completa': paginacion_completa,
            'motivo_incompleta': motivo_incompleta,
        }
        logger.info(f'[SYNC] Completado: {resultado}')
        _sync_estado['ultimo_resultado'] = resultado
        # El panel de sincronizadores (`app.js::syncEstadosCargar`) NO lee el
        # resultado: pinta verde cuando `ultimo_error` viene vacío. Un barrido
        # del 75% con `ultimo_error: null` es la corrida truncada publicada como
        # exitosa — el defecto entero visto desde la pantalla. Se deriva de
        # `motivo_incompleta` en un solo lugar para que no haya dos versiones de
        # cómo le fue a la corrida.
        _sync_estado['ultimo_error'] = (
            None if paginacion_completa else f'barrido incompleto: {motivo_incompleta}')
        _sync_estado['en_curso'] = False
        _reg.cerrar_ok(_reg_id, resultado)


def iniciar_sync_background(app, forzar=False):
    """
    Arranca el sync en un hilo de fondo y retorna inmediatamente.
    forzar=True: el admin dispara manualmente — ignora el guard de intervalo.
    forzar=False: llamada automática del scheduler — respeta el guard.
    """
    global _sync_estado

    ahora = datetime.now(timezone.utc)

    if _sync_estado['en_curso']:
        return {'en_curso': True, 'mensaje': 'Sync ya en proceso — espera que termine'}

    if not forzar:
        ultimo = _sync_estado.get('ultimo_inicio')
        if ultimo and (ahora - ultimo).total_seconds() < _MIN_INTERVALO_SEG:
            return {'omitido': True, 'mensaje': 'Sync reciente — scheduler omite esta vuelta'}

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    _sync_estado['en_curso'] = True
    _sync_estado['ultimo_inicio'] = ahora

    hilo = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    hilo.start()

    return {'iniciado': True, 'mensaje': 'Sync iniciado en background — refresca en ~30 seg'}


def estado_sync():
    """Retorna el estado actual del último sync."""
    return {
        'en_curso': _sync_estado['en_curso'],
        'ultimo_inicio': _sync_estado['ultimo_inicio'].isoformat() if _sync_estado['ultimo_inicio'] else None,
        'ultimo_resultado': _sync_estado['ultimo_resultado'],
        'ultimo_error': _sync_estado['ultimo_error'],
    }


def ejecutar_sync(app=None):
    """
    Compatibilidad con el scheduler de APScheduler (corre en su propio hilo).
    Llama directamente a _run_sync() ya que el scheduler maneja el threading.
    """
    if app:
        _run_sync(app)
    # Si no hay app, no puede correr (necesita contexto)


def init_scheduler(app):
    """Scheduler cada 5 min entre 7am y 8pm hora Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[SYNC] APScheduler no instalado')
        return None

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=ejecutar_sync,
        trigger=CronTrigger(hour='7-20', minute='*/30', timezone='America/Bogota'),
        kwargs={'app': app},
        id='sync_productos_siesa',
        name='Sync catálogo Siesa → WMS cada 30 min',
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=300
    )
    scheduler.start()
    logger.info('[SYNC] Scheduler iniciado — sync cada 30 min 7am–8pm (Bogotá)')
    return scheduler
