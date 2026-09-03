#!/usr/bin/env python
"""
Reset transaccional del acta de corte — borra el ensayo, conserva la memoria.

Existe porque el riesgo real no es que este script se equivoque: es que alguien
escriba un DELETE a mano el día del corte y se lleve por delante la memoria
analítica. Mejor que exista uno correcto a que se improvise uno.

LA FRONTERA
  Tablas OPERATIVAS  → nacen limpias en el acta de corte.
    Picks, packing, bultos, rutas, recaudos, recepciones, traslados, conteos,
    movimientos y jobs generados validando la app.

  Tablas ANALÍTICAS  → NUNCA se tocan.
    serie_vigia, alarma_vigia, kardex_movimientos, stock_diario,
    juicios_temporada. Sin las 26 semanas de referencia el CUSUM queda ciego
    ~6 meses y se pierde la alarma de Florencia — la primera certificada.
    TSB, ROP y newsvendor consumen esa misma historia.

  Tablas MAESTRAS    → NUNCA se tocan.
    Productos, ubicaciones, usuarios, proveedores, acuerdos, vehículos, y el
    expediente de flota: ficha técnica, documentos y plantillas de inspección.

ARCHIVOS DE FOTOS — lo que este script NO puede limpiar
  Vaciar `flota_foto` borra las filas, no los archivos del volumen. Quedan
  huérfanos ocupando disco. Se limpian aparte con `--fotos`, y solo entonces:
  borrar archivos antes que filas dejaría referencias apuntando a nada, que es
  peor que un archivo de más.

Deny-by-default: solo se vacía lo que está en OPERATIVAS. Si alguien agrega una
tabla protegida a esa lista, el script se niega a correr.

Uso:
    venv/bin/python scripts/reset_transaccional.py              # simulacro
    venv/bin/python scripts/reset_transaccional.py --ejecutar   # de verdad

Después de rellenar, correr scripts/verificar_carga_vigia.py: con los mismos
hashes debe dar CERTIFICADO idéntico. Ese es el mejor test del reset — si el
canon sigue reproduciendo S⁻=6.30, la limpieza fue quirúrgica.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VERDE, ROJO, AMAR, GRIS, FIN = '\033[92m', '\033[91m', '\033[93m', '\033[90m', '\033[0m'

# Se vacían. Orden importa: hijos antes que padres por las FKs.
OPERATIVAS = [
    # Devolución de cliente PRIMERO: apunta a `tareas_packing` y a
    # `recaudos_entrega`, que se vacían más abajo. Sin este orden el DELETE de
    # esas dos falla por clave foránea, el `except` de abajo lo imprime como un
    # aviso más, y el corte termina a medias.
    #
    # Es el mismo tropiezo que ya costó una vez con `flota_lectura_odometro`.
    # Estas dos tablas simplemente **no estaban en ninguna lista**: ni operativa
    # ni protegida, así que sobrevivían al corte con los datos del ensayo — las
    # tres devoluciones de prueba del 28 de julio que la auditoría reporta.
    'lineas_devolucion_cliente',
    'devoluciones_cliente',
    'items_packing',
    'bultos',
    # `entregas_geo` apunta a `recaudos_entrega`, así que va ANTES — misma
    # forma que ya costó tres veces (`devoluciones_cliente`, `sesiones_conteo`,
    # las cuatro de flota): el DELETE del padre falla por FK y el `except` del
    # bucle lo imprime como un aviso entre otros.
    #
    # **Es operativa, y su maestro NO.** Una fila de acá dice «el camión estuvo
    # en este punto el día que se hizo esta entrega de prueba»: es registro del
    # ensayo, como las lecturas de odómetro. `clientes_geo` —dónde queda cada
    # tienda— está en PROTEGIDAS_MAESTRAS, y por eso son dos tablas: si fueran
    # una sola, el corte obligaría a elegir entre borrar el ensayo y conservar
    # el activo, y cualquiera de las dos elecciones está mal.
    'entregas_geo',
    'recaudos_entrega',
    'tareas_packing',
    # `sesiones_conteo` apunta a `tareas_picking` (la tarea que genera un
    # conteo tras un descuadre), así que va ANTES. Estaba catorce posiciones
    # más abajo: el DELETE de `tareas_picking` fallaba por FK y el bucle lo
    # imprimía como un aviso. Lo encontró el trinquete de orden, no una
    # corrida — y una corrida solo lo habría mostrado el día del corte.
    'sesiones_conteo',
    'tareas_picking',
    'tareas_devolucion',
    'tareas_reposicion',
    'items_recepcion',
    'recepciones',
    'items_solicitud_traslado',
    'solicitudes_traslado',
    'rutas_despacho',
    'movimientos_inventario',
    'siesa_jobs',
    'pedidos_siesa',
    'ubicaciones_huerfanas',
    'fugas_recompra',
    # ── Flota (agregado 2026-08-03) ────────────────────────────────────────
    # Registros del ensayo: turnos, lecturas y fotos. Se vacían.
    #
    # NO están acá `flota_ficha_tecnica` ni `flota_documento_vehiculo`: son el
    # levantamiento de campo. Media mañana recorriendo cinco vehículos, con la
    # foto del tablero y la medida de llanta en la mano. Borrarlas en el corte
    # obliga a hacerlo dos veces, y la segunda nadie la hace.
    # La lectura apunta a la foto del odómetro, así que va antes. El script
    # ya compensaba este orden deshabilitando triggers en Postgres; con el
    # orden correcto ese apaño queda de más, pero se conserva —quitarlo el día
    # del corte es una decisión aparte.
    # Orden por FK, hijos antes que padres. Las cuatro de abajo llegaron el
    # 2026-09-02 y **las encontró el trinquete, no una corrida**: entraron a
    # los modelos sin pasar por ninguna lista, y una tabla sin clasificar
    # sobrevive al corte con los datos del ensayo. Es el tercer episodio de la
    # misma forma (antes: `devoluciones_cliente` y `sesiones_conteo`).
    #
    # `flota_respuesta_item` apunta a `flota_inspeccion` Y a `flota_hallazgo`;
    # `flota_tanqueo` a `flota_gasto`. Los cuatro apuntan además a
    # `flota_lectura_odometro`, que se vacía más abajo.
    'flota_respuesta_item',
    'flota_inspeccion',
    # ── Taller (agregado 2026-09-02, fase 2) ───────────────────────────────
    # `flota_intervencion` apunta a `flota_gasto` Y a `flota_orden_trabajo`, así
    # que va antes que las dos. `flota_orden_trabajo` apunta a `flota_hallazgo`
    # y a `flota_lectura_odometro`, así que va antes que ésas.
    #
    # Es el mismo orden por FK que ya costó tres veces en este archivo
    # (`devoluciones_cliente`, `sesiones_conteo`, las cuatro de flota del
    # 2026-09-02): el DELETE del padre falla por clave foránea, el `except` del
    # bucle lo imprime como un aviso más, y el corte termina a medias.
    #
    # Las dos son OPERATIVAS y no expediente: una visita al taller durante el
    # ensayo es un registro de la prueba. El expediente del vehículo son la
    # ficha y los documentos, que siguen protegidos abajo.
    'flota_intervencion',
    # ── Llantas (agregado 2026-09-02, fase 4) ──────────────────────────────
    # **La clasificación se pensó tabla por tabla, y las dos NO van juntas.**
    #
    # `flota_montaje_llanta` es OPERATIVA: cada fila dice «esta llanta estuvo en
    # esta posición de este camión entre estos dos kilometrajes», y los
    # kilometrajes del ensayo son del ensayo. Se vacía.
    #
    # `flota_llanta` es MAESTRA y está abajo, en PROTEGIDAS_MAESTRAS. **No es
    # registro de una prueba: es un activo comprado que existe físicamente.** El
    # día después del corte las 24 llantas siguen atornilladas a los camiones, y
    # su código —lo único que las distingue de otras cinco iguales— está marcado
    # en el caucho. Borrarlas obliga a recorrer el patio leyendo flancos, que es
    # el mismo argumento por el que `flota_ficha_tecnica` está protegida: la
    # segunda vez nadie la hace.
    #
    # La consecuencia de partirlas se declara en vez de disimularse: después del
    # corte las llantas quedan en el catálogo y **sin montaje**, o sea que el
    # sistema dirá que ninguna está puesta. Eso NO queda en silencio — es
    # exactamente lo que cuenta `posiciones_sin_llanta` en `/flota/health` y lo
    # que pinta el bloque de salud del tablero, y volver a registrarlas son 24
    # formularios cortos con el código ya en la lista. La alternativa —proteger
    # las dos— dejaría montajes del ensayo apuntando a lecturas de odómetro
    # borradas, que es un hueco silencioso contra un número visible.
    #
    # Va ANTES que `flota_gasto` (apunta a él por `gasto_id`), que
    # `flota_lectura_odometro` (dos FK, regla 3) y que `flota_llanta`.
    'flota_montaje_llanta',
    'flota_tanqueo',
    'flota_gasto',
    'flota_orden_trabajo',
    # `flota_hallazgo` PRIMERO: apunta a `flota_lectura_odometro` (regla 3,
    # NOT NULL) y a `flota_custodia`, las dos de abajo. Es la misma forma que
    # ya costó dos veces —`devoluciones_cliente` y `sesiones_conteo`—: el
    # DELETE del padre falla por FK, el `except` del bucle lo imprime como un
    # aviso entre otros, y el corte termina a medias.
    #
    # Y es operativa, no expediente: los daños del ensayo son daños de
    # vehículos que se estaban probando. El expediente del vehículo son la
    # ficha y los documentos, que siguen protegidos abajo.
    'flota_hallazgo',
    # ── Preventivo (agregado 2026-09-02, fase 3) ───────────────────────────
    # **Las dos tablas se clasificaron por separado y no cayeron del mismo
    # lado.** La pregunta es una sola: ¿esto es registro del ensayo, o es algo
    # que el día después del corte sigue siendo cierto del vehículo?
    #
    # `flota_ejecucion_tarea` es OPERATIVA y va acá. Cada fila dice «esta tarea
    # se hizo a este kilometraje», y **cuelga de `flota_lectura_odometro` con
    # FK NOT NULL** (regla 3), que se vacía cuatro líneas más abajo. No hay
    # forma de conservarla: protegerla dejaría cada ejecución apuntando a una
    # lectura que ya no existe, que es un hueco silencioso — exactamente el
    # error que este archivo lleva pagando cuatro veces. Su ancla es del
    # ensayo, así que ella también lo es.
    #
    # La consecuencia se declara en vez de disimularse: después del corte todas
    # las tareas vuelven a `sin_linea_base` y el sistema dice, correctamente,
    # que no sabe cuándo se hizo el último cambio. Eso NO queda en silencio —
    # es lo que cuenta `tareas_sin_linea_base` en `/flota/health` y lo que
    # pinta el bloque de salud del tablero.
    #
    # `flota_plan_tarea` es MAESTRA y está abajo. **No es registro de una
    # prueba: es una propiedad del vehículo** —«esta correa se cambia cada
    # 60.000 km»— y el día después del corte sigue siendo cierta. Se siembra
    # desde la ficha, que también está protegida, pero re-sembrarla recupera
    # solo las filas de origen `ficha`: el intervalo que alguien averiguó
    # llamando al concesionario está en las de origen `manual` y no se puede
    # reconstruir de ninguna parte. Es el mismo argumento de
    # `flota_ficha_tecnica`: la segunda vez nadie la hace.
    #
    # Va ANTES que `flota_lectura_odometro` por la FK.
    'flota_ejecucion_tarea',
    'flota_lectura_odometro',
    'flota_foto',
    'flota_custodia',
    # Avisos enviados durante el ensayo. Como las fotos y las lecturas: es
    # registro de la prueba, no expediente del vehículo.
    'flota_aviso',
    # Bitácora de corridas de sincronización. Se vuelve a llenar sola en la
    # primera sync después del corte.
    'registros_sync',
    # Declaraciones de ambiente. **Se borran a propósito, y es la decisión
    # menos obvia de esta lista.**
    #
    # Una declaración dice «fulano cuadró esta cifra contra el mundo, con esta
    # configuración». El módulo ya la invalida sola si cambia el host o la
    # compañía — pero el post-mortem del Gestor (2026-08-19) demuestra que
    # **el destino de la conexión puede cambiar sin que el host cambie**: eso
    # fue exactamente su incidente, dos hosts distintos apuntando a la misma
    # base.
    #
    # Con la huella insuficiente, la pregunta es qué dice una declaración
    # hecha ANTES del corte sobre el después. La respuesta honesta es nada, y
    # el corte es el momento de máximo riesgo. Regla 0: arrancar en ALARMA y
    # obligar a declarar de nuevo cuesta media hora; heredar un verde de QA
    # cuesta ocho horas escribiendo en la base equivocada.
    'declaraciones_ambiente',
]

# NUNCA. Si aparecen en OPERATIVAS, el script aborta.
PROTEGIDAS_ANALITICAS = {
    # NO es una lista de precios: es `valor / cantidad` sobre ventas reales,
    # neto de descuentos. Alimenta el Cu del newsvendor —margen medido en vez
    # de supuesto— y mide la escalera de precios entre C.O. Borrarlo devuelve
    # los modelos al margen supuesto sin que nadie lo note.
    'precios_realizados': 'margen medido del newsvendor y escalera de precios',
    'serie_vigia': 'línea base del CUSUM — sin ella, ciego ~6 meses',
    'alarma_vigia': 'la alarma de Florencia, primera certificada',
    'kardex_movimientos': 'historia de demanda que alimenta los 4 modelos',
    'stock_diario': 'denominador de la descensura',
    'juicios_temporada': 'juicio humano registrado, no recalculable',
}

PROTEGIDAS_MAESTRAS = {
    'productos', 'ubicaciones', 'ubicaciones_productos', 'usuarios', 'almacenes',
    'proveedores', 'acuerdos_marco', 'precios_proveedor', 'vehiculos',
    'conductores', 'rutas_maestras', 'rutas_maestras_paradas', 'lpn',
    'producto_empaques', 'siesa_mapeo_unidades', 'productos_bloqueados',
    'producto_clasificacion_abc', 'contenedores', 'ficha_importacion',
    'items_en_transito', 'stock_siesa',
    # Flota: el expediente del vehículo y el catálogo de inspección.
    # `flota_ficha_tecnica` es levantamiento de campo, no registro de ensayo.
    # `flota_documento_vehiculo` es la vigencia real de SOAT y tecnomecánica.
    # Las plantillas son el catálogo versionado — borrarlas dejaría las
    # inspecciones viejas apuntando a ítems que ya no existen.
    'flota_ficha_tecnica', 'flota_documento_vehiculo',
    'flota_plantilla_inspeccion', 'flota_item_inspeccion',
    # `flota_llanta` es un **activo comprado que existe físicamente**, no un
    # registro del ensayo: el día después del corte las 24 llantas siguen
    # atornilladas a los camiones, y su código está marcado en el caucho.
    # Borrarlas obliga a recorrer el patio leyendo flancos — el mismo argumento
    # de la ficha técnica, y la segunda vez nadie la hace.
    #
    # Su tabla de MONTAJES sí es operativa y sí se vacía (ver arriba): dónde
    # estuvo puesta cada llanta durante la prueba es registro de la prueba. La
    # asimetría es deliberada y su consecuencia es visible, no silenciosa —
    # `posiciones_sin_llanta` en el health cuenta exactamente los montajes que
    # hay que volver a registrar.
    'flota_llanta',
    # `flota_plan_tarea` es una **propiedad del vehículo**, no un registro del
    # ensayo: «esta correa se cambia cada 60.000 km» sigue siendo cierto el día
    # después del corte. Es la misma naturaleza que la ficha de la que se
    # siembra, y está protegida por el mismo motivo.
    #
    # Re-sembrar desde la ficha recupera las filas de origen `ficha` — pero NO
    # las de origen `manual`: el intervalo del aceite que alguien averiguó
    # llamando al concesionario no está en ninguna columna de la ficha y no se
    # puede reconstruir de ninguna parte.
    #
    # Su tabla de EJECUCIONES sí es operativa y sí se vacía (ver arriba):
    # cuelga de `flota_lectura_odometro` con FK NOT NULL y esa se vacía, así
    # que conservarla dejaría cada ejecución apuntando a una lectura que ya no
    # existe. La consecuencia —todas las tareas vuelven a `sin_linea_base`— la
    # cuenta `tareas_sin_linea_base` en el health, y por eso es visible en vez
    # de silenciosa.
    'flota_plan_tarea',
    # Dónde queda cada tienda. **Es el activo que tarda tres meses en
    # construirse**, no un registro del ensayo: cada fila es la mediana de las
    # veces que un conductor tocó «Estoy aquí» parado en la puerta de un
    # cliente, y no hay forma de recuperarla salvo volver a recorrer las rutas.
    # El WMS no tiene direcciones de clientes (`pedidos_sync_service` lee
    # códigos de municipio, no calles) y las tres ciudades tienen 72, 10 y ~31
    # números de casa mapeados: no existe ninguna API con la que rehacer esto.
    #
    # Es exactamente el caso que `precios_realizados` documenta más arriba —
    # borrarla no rompe nada visible y devuelve el sistema al estado anterior
    # sin que nadie lo note.
    'clientes_geo',
}

CANONES = ['docs/canon_florencia.json', 'docs/canon_PLANTILLA.json',
           'docs/canones/facturas_co.json', 'docs/canones/rop_dual.json',
           'docs/canones/clasificacion_sb.json']


def _guard():
    """El script se niega a correr si la lista fue contaminada."""
    protegidas = set(PROTEGIDAS_ANALITICAS) | PROTEGIDAS_MAESTRAS
    invasoras = [t for t in OPERATIVAS if t in protegidas]
    if invasoras:
        print(f'\n  {ROJO}ABORTADO — hay tablas protegidas en la lista de borrado:{FIN}')
        for t in invasoras:
            razon = PROTEGIDAS_ANALITICAS.get(t, 'tabla maestra')
            print(f'    · {t} — {razon}')
        print()
        return False
    return True


def _limpiar_fotos_huerfanas(db):
    """Borra del volumen los archivos que ya no tiene ninguna fila.

    Vaciar `flota_foto` borra las filas, no los archivos: quedan ocupando disco
    sin que nadie los reclame. El volumen de Railway tiene tamaño fijo, y
    **llenarse en silencio es el próximo modo de fallo de este diseño** — a
    partir de ahí toda foto nueva cae en `pendiente_evidencia`.

    El orden importa y es este: **primero las filas, después los archivos.**
    Al revés dejaría referencias apuntando a nada, que es peor que un archivo
    de más — un hueco silencioso contra disco ocupado.
    """
    import os
    from pathlib import Path as _P

    from sqlalchemy import text

    raiz = os.getenv('FLOTA_FOTOS_DIR')
    if raiz is None or not raiz.strip():
        return 'FLOTA_FOTOS_DIR no está configurada — no hay volumen que limpiar.'

    vivas = {r[0] for r in db.session.execute(
        text('SELECT storage_ref FROM flota_foto'))}
    base = _P(raiz.strip())
    borrados = liberado = 0
    for archivo in base.rglob('*'):
        if not archivo.is_file():
            continue
        rel = str(archivo.relative_to(base))
        if rel in vivas:
            continue
        liberado += archivo.stat().st_size
        archivo.unlink()
        borrados += 1
    return f'{borrados} archivos huérfanos borrados · {liberado / 1_048_576:.1f} MB liberados'


def _describir_destino():
    """`(host, base)` de la conexión, SIN la contraseña."""
    import os
    from urllib.parse import urlparse
    url = os.getenv('DATABASE_URL', '')
    if not url:
        from app import create_app
        url = create_app().config.get('SQLALCHEMY_DATABASE_URI', '')
    u = urlparse(url)
    return (u.hostname or 'local', (u.path or '').lstrip('/') or '(archivo)')


def _destino_confirmado(args) -> bool:
    """Nadie vacía una base sin nombrarla.

    El script no verificaba **contra qué base** borraba: tomaba `DATABASE_URL`
    y ejecutaba. Y el `DATABASE_URL` de una sesión de desarrollo apunta a la
    base de **producción** en Railway — comprobado el 2026-08-14, `metro.proxy
    .rlwy.net/railway`, 51.808 filas.

    Con eso, `--ejecutar` recuperado del historial de la terminal vacía
    producción. No hace falta equivocarse: basta con repetir un comando.

    Por eso `--ejecutar` ya no alcanza: hay que **escribir el host**. Es el
    único gesto que no se puede hacer por inercia, y obliga a mirar a dónde va
    el borrado antes de que ocurra.
    """
    host, base = _describir_destino()
    print()
    print(f'  {AMAR}DESTINO:{FIN} {host} · base {base}')
    if not args.ejecutar:
        return True
    if not args.confirmar_destino:
        print(f'  {ROJO}Falta --confirmar-destino.{FIN} Para vaciar hay que '
              f'nombrar la base:')
        print(f'    {GRIS}--ejecutar --confirmar-destino {host}{FIN}\n')
        return False
    if args.confirmar_destino != host:
        print(f'  {ROJO}El destino no coincide.{FIN} Escribiste '
              f'{args.confirmar_destino!r} y la conexión apunta a {host!r}.')
        print(f'  {GRIS}Si de verdad querías la otra base, cambiá DATABASE_URL '
              f'— no el parámetro.{FIN}\n')
        return False
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--ejecutar', action='store_true',
                   help='Borra de verdad. Sin esto solo simula.')
    p.add_argument('--fotos', action='store_true',
                   help='Borra tambien los archivos huerfanos del volumen de '
                        'flota. Solo DESPUES de vaciar las filas.')
    p.add_argument('--confirmar-destino', metavar='HOST',
                   help='El host de la base que se va a vaciar. Obligatorio '
                        'junto con --ejecutar: hay que escribirlo, no basta '
                        'con repetir el comando.')
    args = p.parse_args()

    if not _guard():
        return 2

    if not _destino_confirmado(args):
        return 2

    from app import create_app
    from app.extensions import db
    from sqlalchemy import text, func

    app = create_app()
    with app.app_context():
        print()
        print('  RESET TRANSACCIONAL — ACTA DE CORTE')
        print('  ' + '─' * 54)
        if not args.ejecutar:
            print(f'  {AMAR}SIMULACRO — nada se borra. Usa --ejecutar para hacerlo real.{FIN}')
        print()

        # Lo que se conserva, contado ANTES
        print(f'  {VERDE}Se conservan (memoria analítica):{FIN}')
        antes = {}
        for t, razon in PROTEGIDAS_ANALITICAS.items():
            try:
                n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            except Exception:
                n = None
            antes[t] = n
            print(f'    {n if n is not None else "?":>9}  {t}  {GRIS}{razon}{FIN}')

        print(f'\n  {ROJO}Se vacían (transaccional de ensayo):{FIN}')
        total = 0
        for t in OPERATIVAS:
            try:
                n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            except Exception:
                print(f'    {GRIS}{"—":>9}  {t} (no existe){FIN}')
                continue
            total += n or 0
            print(f'    {n:>9}  {t}')

        print(f'\n  {GRIS}Total de filas a borrar: {total:,}{FIN}')

        if not args.ejecutar:
            print(f'\n  {AMAR}Simulacro terminado. Nada se tocó.{FIN}\n')
            return 0

        # `flota_lectura_odometro` tiene un trigger BEFORE DELETE en PostgreSQL:
        # una lectura no se borra, se corrige con otra. Es correcto y es a
        # propósito — pero el acta de corte SÍ tiene que poder vaciarla.
        #
        # Sin esto el DELETE fallaba, el `except` de abajo lo imprimía como un
        # "aviso" entre otros, y el corte terminaba dejando las lecturas vivas
        # con sus custodias borradas. Un error tragado por un manejador que
        # existe para otra cosa: exactamente lo que la regla 5 prohíbe.
        es_pg = db.engine.dialect.name == 'postgresql'
        if es_pg:
            db.session.execute(text(
                'ALTER TABLE flota_lectura_odometro DISABLE TRIGGER USER'))
        try:
            for t in OPERATIVAS:
                try:
                    db.session.execute(text(f'DELETE FROM {t}'))
                except Exception as e:
                    print(f'  {AMAR}aviso: {t} — {e}{FIN}')
        finally:
            if es_pg:
                db.session.execute(text(
                    'ALTER TABLE flota_lectura_odometro ENABLE TRIGGER USER'))
        db.session.commit()

        # Verificar que las operativas quedaron en cero. Un `except` que imprime
        # un aviso no es una verificación: el corte tiene que afirmar que cortó.
        sobrantes = []
        for t in OPERATIVAS:
            try:
                n = db.session.execute(text(f'SELECT count(*) FROM {t}')).scalar()
                if n:
                    sobrantes.append(f'{t}: {n}')
            except Exception:
                pass
        # Hasta hoy esto se imprimía en rojo y el script devolvía 0 igual: el
        # corte terminaba diciendo «RESET COMPLETO» con tablas operativas
        # llenas. `ok` solo medía que la memoria analítica hubiera sobrevivido
        # — media verificación. Un corte tiene que afirmar que cortó.
        corto_de_verdad = not sobrantes
        if sobrantes:
            print(f'\n  {ROJO}NO quedaron vacías:{FIN} ' + ', '.join(sobrantes))
            print(f'  {GRIS}Suele ser una clave foránea: una tabla que apunta a '
                  f'ésta se borra después, o no está en OPERATIVAS.{FIN}')

        if args.fotos:
            print(f'\n  {VERDE}Limpiando archivos huérfanos de flota…{FIN}')
            print(f'  {_limpiar_fotos_huerfanas(db)}')

        # Verificación posterior: la memoria sigue ahí
        print(f'\n  {VERDE}Verificando que la memoria sobrevivió…{FIN}')
        ok = True
        for t, n_antes in antes.items():
            if n_antes is None:
                continue
            n = db.session.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar()
            marca = f'{VERDE}✓{FIN}' if n == n_antes else f'{ROJO}✗{FIN}'
            if n != n_antes:
                ok = False
            print(f'    {marca} {t}: {n_antes} → {n}')

        faltan = [c for c in CANONES
                  if not os.path.exists(os.path.join(
                      os.path.dirname(os.path.dirname(os.path.abspath(__file__))), c))]
        if faltan:
            ok = False
            print(f'\n  {ROJO}Canones faltantes:{FIN}')
            for c in faltan:
                print(f'    · {c}')

        print('\n  ' + '─' * 54)
        if ok and corto_de_verdad:
            print(f'  {VERDE}RESET COMPLETO{FIN} — memoria analítica intacta.')
            print(f'  {GRIS}Siguiente: rellenar y correr scripts/verificar_carga_vigia.py.')
            print(f'  Con los mismos hashes debe dar CERTIFICADO idéntico — si el canon')
            print(f'  sigue reproduciendo S-=6.30, la limpieza fue quirúrgica.{FIN}\n')
            return 0
        if not corto_de_verdad:
            print(f'  {ROJO}RESET INCOMPLETO{FIN} — quedaron tablas operativas '
                  f'con datos. NO se puede arrancar sobre esto.\n')
        else:
            print(f'  {ROJO}RESET CON PÉRDIDAS{FIN} — revisar antes de continuar.\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
