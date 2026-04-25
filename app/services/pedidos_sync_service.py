"""
Sync de pedidos Siesa → Read Model local (tabla pedidos_siesa).

Estrategia:
  - Connekta no filtra por fecha, bodega ni CO — solo enteros con =
  - El sync pagina todo el histórico en paralelo (10 páginas concurrentes)
  - Python filtra NB1/CO003 y hace upsert solo de los pendientes
  - El endpoint /api/siesa/pedidos lee de la DB local: respuesta <5ms
  - El sync tarda 30-90s pero corre en background — no bloquea a nadie
"""
import logging
import threading
from datetime import datetime, timezone
from app.utils.dane_municipios import resolver_municipio

from app.extensions import db
from app.models.pedido_siesa import PedidoSiesa
from app.models.producto import Producto
from app.services.connekta_gateway import connekta

logger = logging.getLogger(__name__)

_sync_estado = {
    'en_curso': False,
    'ultimo_inicio': None,
    'ultimo_resultado': None,
    'ultimo_error': None,
}
_MIN_INTERVALO_SEG = 90  # mínimo 90s entre syncs manuales


def _run_sync(app):
    global _sync_estado

    ESTADOS_EXCLUIR = {0, 9}
    TAM_PAG = 100
    WORKERS = 10

    with app.app_context():
        upserts = 0
        eliminados = 0
        paginas_leidas = 0
        anulados_detectados = []

        try:
            # estado=3 → Comprometido: inventario físicamente reservado en Siesa
            # estado=2 (Aprobado) NO entra — el inventario no está reservado aún
            parametros = f"f430_id_co = ''{connekta.centro_op}'' AND f430_ind_estado = 3"

            all_items = []
            for pag in range(1, 200):
                try:
                    res = connekta._get(connekta.api_pedidos, {
                        'paginacion': f'numPag={pag}|tamPag={TAM_PAG}',
                        'parametros': parametros
                    })
                    rows = res.get('detalle', {}).get('Table', [])
                    all_items.extend(rows)
                    paginas_leidas += 1
                    if len(rows) < TAM_PAG:
                        break
                except Exception as e:
                    logger.warning(f'[PEDIDOS_SYNC] Página {pag}: {e}')
                    break

            # Filtrar bodega NB1 en Python (CO ya filtrado en Siesa)
            items_nb1 = []
            for item in all_items:
                if item.get('f150_id', '').strip() != connekta.bodega:
                    continue
                try:
                    cant_pedida = float(item.get('f431_cant1_pedida', 0))
                    cant_remisionada = float(item.get('f431_cant1_remisionada', 0))
                    cant_pendiente = cant_pedida - cant_remisionada
                    if cant_pendiente > 0:
                        items_nb1.append({
                            'tipo_docto':         (item.get('f430_id_tipo_docto') or '').strip(),
                            'consec_docto':       item.get('f430_consec_docto'),
                            'centro_op':          item.get('f430_id_co'),
                            'bodega':             item.get('f150_id'),
                            'numero_pedido':      f"{(item.get('f430_id_tipo_docto') or '').strip()}{item.get('f430_consec_docto', '')}",
                            'item_codigo':        item.get('f120_referencia'),
                            'item_descripcion':   item.get('f120_descripcion'),
                            'item_id_siesa':      item.get('f120_id'),
                            'cliente':            item.get('f200_razon_social_pedido_fact'),
                            'municipio':          resolver_municipio(item.get('f015_id_depto_pe', ''), item.get('f015_id_ciudad_pe', '')),
                            'fecha_entrega':      item.get('f430_fecha_entrega'),
                            'estado_siesa':       item.get('f430_ind_estado'),
                            'cantidad_pedida':    cant_pedida,
                            'cantidad_remisionada': cant_remisionada,
                            'cantidad_pendiente': cant_pendiente,
                        })
                except (ValueError, TypeError):
                    continue

            # ── Pre-cargar todos los productos en memoria para el upsert — elimina N+1 ──
            # Sin esto: 2 queries × N items (potencialmente 2000 queries para 1000 items)
            _todos_prods = Producto.query.all()
            _mapa_prod_siesa = {p.codigo_siesa: p for p in _todos_prods if p.codigo_siesa}
            _mapa_prod_codigo = {p.codigo: p for p in _todos_prods}

            # ── Pre-cargar todos los PedidoSiesa activos en memoria ──────────────────
            # Evita 1 query por item durante el upsert
            _pedidos_existentes = {
                (r.tipo_docto, r.consec_docto, r.centro_op, r.bodega, r.item_codigo): r
                for r in PedidoSiesa.query.all()
            }

            # Upsert en DB local
            claves_activas = set()
            for d in items_nb1:
                clave = (d['tipo_docto'], d['consec_docto'], d['centro_op'], d['bodega'], d['item_codigo'])
                claves_activas.add(clave)

                reg = _pedidos_existentes.get(clave)

                # Enriquecer con producto_id (O(1) lookup en vez de 2 queries)
                prod = _mapa_prod_siesa.get(d['item_codigo']) or _mapa_prod_codigo.get(d['item_codigo'])

                if reg:
                    reg.cantidad_pedida      = d['cantidad_pedida']
                    reg.cantidad_remisionada = d['cantidad_remisionada']
                    reg.cantidad_pendiente   = d['cantidad_pendiente']
                    reg.estado_siesa         = d['estado_siesa']
                    reg.cliente              = d['cliente']
                    reg.municipio            = d.get('municipio', '')
                    reg.fecha_entrega        = d['fecha_entrega']
                    reg.producto_id          = prod.id if prod else None
                    reg.sync_at              = datetime.utcnow()
                else:
                    reg = PedidoSiesa(
                        **{k: v for k, v in d.items()},
                        producto_id=prod.id if prod else None,
                        sync_at=datetime.utcnow()
                    )
                    db.session.add(reg)
                    _pedidos_existentes[clave] = reg  # evita duplicar si misma clave aparece dos veces
                upserts += 1

            # ── Detectar packings/pickings activos cuyo pedido desapareció de Siesa ──
            # Si un pedido que el WMS está procesando activamente ya no aparece como
            # Comprometido (estado=3) en Siesa, significa que fue Anulado (9) o Cumplido
            # externamente. Se marca el packing con pedido_anulado_siesa=True para que
            # el dashboard muestre la alerta en el próximo ciclo.
            try:
                from app.models.packing import TareaPacking
                from app.models.picking import TareaPicking

                numeros_activos_wms = set()
                # Packings no terminados
                packings_vivos = TareaPacking.query.filter(
                    TareaPacking.estado.notin_(['CANCELADO', 'DESPACHADO']),
                    TareaPacking.siesa_triggered == False,
                ).all()
                for pk in packings_vivos:
                    numeros_activos_wms.add(pk.numero_pedido_siesa)

                # numeros que el sync NB1 sigue viendo como comprometidos
                numeros_comprometidos_siesa = {
                    f"{d['tipo_docto']}{d['consec_docto']}"
                    for d in items_nb1
                }

                # Limitar a 10 verificaciones por ciclo — evita que el job tarde
                # 20+ minutos con 50 packings no comprometidos (N × 30s timeout)
                _MAX_VERIFICAR = 10
                _pendientes_verificar = [
                    pk for pk in packings_vivos
                    if pk.numero_pedido_siesa not in numeros_comprometidos_siesa
                ][:_MAX_VERIFICAR]
                if len(_pendientes_verificar) < sum(
                    1 for pk in packings_vivos
                    if pk.numero_pedido_siesa not in numeros_comprometidos_siesa
                ):
                    logger.warning(
                        f'[PEDIDOS_SYNC] Verificación de anulados limitada a {_MAX_VERIFICAR} '
                        f'por ciclo para proteger el scheduler. Habrá más en el próximo ciclo.'
                    )

                anulados_detectados = []
                for pk in _pendientes_verificar:
                    if pk.numero_pedido_siesa not in numeros_comprometidos_siesa:
                        # Verificar el estado real en Siesa para este pedido puntual
                        if not pk.tipo_docto_pedido_siesa:
                            logger.warning(
                                f'[PEDIDOS_SYNC] Packing {pk.id} ({pk.numero_pedido_siesa}) '
                                f'sin tipo_docto_pedido_siesa — no se puede verificar estado en Siesa'
                            )
                            continue
                        estado_real = connekta.get_estado_pedido(
                            pk.tipo_docto_pedido_siesa,
                            pk.consec_docto_pedido_siesa or pk.numero_pedido_siesa
                        )
                        # 1=En elaboración, 2=Aprobado, 3=Comprometido, 4=Cumplido: NO anular
                        # Solo marcar anulado para estado=9 (Anulado) u otros desconocidos
                        if estado_real is not None and str(estado_real) not in ('1', '2', '3', '4'):
                            if not getattr(pk, 'pedido_anulado_siesa', False):
                                pk.pedido_anulado_siesa = True
                                pk.pedido_estado_siesa_detectado = str(estado_real)
                                anulados_detectados.append(
                                    f'{pk.numero_pedido_siesa} (estado={estado_real})'
                                )
                                logger.warning(
                                    f'[PEDIDOS_SYNC] ⚠ PEDIDO ANULADO EN SIESA: '
                                    f'{pk.numero_pedido_siesa} estado={estado_real} '
                                    f'— packing id={pk.id} marcado para alerta'
                                )

                if anulados_detectados:
                    logger.warning(
                        f'[PEDIDOS_SYNC] {len(anulados_detectados)} packing(s) con pedido '
                        f'anulado/no comprometido en Siesa: {anulados_detectados}'
                    )
            except Exception as _e:
                logger.warning(f'[PEDIDOS_SYNC] Detección anulados falló silenciosamente: {_e}')
            # ────────────────────────────────────────────────────────────────────

            # [08] Eliminar pedidos que ya no tienen pendiente — bulk delete por IDs conocidos
            # evita cargar todos los registros en memoria con .all()
            todos_ids = db.session.query(
                PedidoSiesa.id,
                PedidoSiesa.tipo_docto,
                PedidoSiesa.consec_docto,
                PedidoSiesa.centro_op,
                PedidoSiesa.bodega,
                PedidoSiesa.item_codigo,
            ).all()
            ids_a_borrar = [
                r.id for r in todos_ids
                if (r.tipo_docto, r.consec_docto, r.centro_op, r.bodega, r.item_codigo) not in claves_activas
            ]
            if ids_a_borrar:
                PedidoSiesa.query.filter(PedidoSiesa.id.in_(ids_a_borrar)).delete(synchronize_session=False)
                eliminados = len(ids_a_borrar)

            db.session.commit()

            resultado = {
                'timestamp': datetime.utcnow().isoformat(),
                'total_siesa': len(all_items),
                'paginas_leidas': paginas_leidas,
                'items_nb1_pendientes': len(items_nb1),
                'upserts': upserts,
                'eliminados': eliminados,
                'anulados_detectados': len(anulados_detectados),
            }
            logger.info(f'[PEDIDOS_SYNC] OK: {resultado}')
            _sync_estado['ultimo_resultado'] = resultado
            _sync_estado['ultimo_error'] = None

        except Exception as e:
            logger.error(f'[PEDIDOS_SYNC] Error: {e}')
            db.session.rollback()
            _sync_estado['ultimo_error'] = str(e)
        finally:
            _sync_estado['en_curso'] = False


def iniciar_sync_background(app, forzar=False):
    global _sync_estado

    if connekta.modo_simulacion:
        return {'simulado': True, 'mensaje': 'Modo simulación — conecta credenciales Siesa'}

    ahora = datetime.now(timezone.utc)

    if _sync_estado['en_curso']:
        return {'en_curso': True, 'mensaje': 'Sync ya en proceso'}

    if not forzar:
        ultimo = _sync_estado.get('ultimo_inicio')
        if ultimo and (ahora - ultimo).total_seconds() < _MIN_INTERVALO_SEG:
            return {'omitido': True, 'mensaje': 'Sync reciente — omitido'}

    _sync_estado['en_curso'] = True
    _sync_estado['ultimo_inicio'] = ahora

    hilo = threading.Thread(target=_run_sync, args=(app,), daemon=True)
    hilo.start()
    return {'iniciado': True, 'mensaje': 'Sync de pedidos iniciado en background'}


def estado_sync():
    return {
        'en_curso': _sync_estado['en_curso'],
        'ultimo_inicio': _sync_estado['ultimo_inicio'].isoformat() if _sync_estado['ultimo_inicio'] else None,
        'ultimo_resultado': _sync_estado['ultimo_resultado'],
        'ultimo_error': _sync_estado['ultimo_error'],
    }


def init_scheduler(app):
    """Scheduler cada 2 min entre 7am y 8pm hora Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning('[PEDIDOS_SYNC] APScheduler no disponible')
        return

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=lambda: iniciar_sync_background(app),
        trigger=CronTrigger(minute='*/2', hour='7-20', timezone='America/Bogota'),
        id='pedidos_siesa_sync',
        replace_existing=True,
        max_instances=1,        # [44] Evita ejecuciones concurrentes del sync
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info('[PEDIDOS_SYNC] Scheduler activo — sync cada 2 min entre 7am y 8pm')
