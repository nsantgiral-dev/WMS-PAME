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
_MIN_INTERVALO_SEG = 4 * 60


def _run_sync(app):
    global _sync_estado

    ESTADOS_EXCLUIR = {0, 9}
    TAM_PAG = 100
    WORKERS = 10

    with app.app_context():
        upserts = 0
        eliminados = 0
        paginas_leidas = 0

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

            # Upsert en DB local
            claves_activas = set()
            for d in items_nb1:
                clave = (d['tipo_docto'], d['consec_docto'], d['centro_op'], d['bodega'], d['item_codigo'])
                claves_activas.add(clave)

                reg = PedidoSiesa.query.filter_by(
                    tipo_docto=d['tipo_docto'],
                    consec_docto=d['consec_docto'],
                    centro_op=d['centro_op'],
                    bodega=d['bodega'],
                    item_codigo=d['item_codigo']
                ).first()

                # Enriquecer con producto_id
                prod = (Producto.query.filter_by(codigo_siesa=d['item_codigo']).first()
                        or Producto.query.filter_by(codigo=d['item_codigo']).first())

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
                upserts += 1

            # Eliminar pedidos que ya no tienen pendiente (remisionados o anulados)
            todos = PedidoSiesa.query.all()
            for reg in todos:
                clave = (reg.tipo_docto, reg.consec_docto, reg.centro_op, reg.bodega, reg.item_codigo)
                if clave not in claves_activas:
                    db.session.delete(reg)
                    eliminados += 1

            db.session.commit()

            resultado = {
                'timestamp': datetime.utcnow().isoformat(),
                'total_siesa': len(all_items),
                'paginas_leidas': paginas_leidas,
                'items_nb1_pendientes': len(items_nb1),
                'upserts': upserts,
                'eliminados': eliminados,
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
    """Scheduler cada 5 min entre 7am y 8pm hora Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning('[PEDIDOS_SYNC] APScheduler no disponible')
        return

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(
        func=lambda: iniciar_sync_background(app),
        trigger=CronTrigger(minute='*/5', hour='7-20', timezone='America/Bogota'),
        id='pedidos_siesa_sync',
        replace_existing=True
    )
    scheduler.start()
    logger.info('[PEDIDOS_SYNC] Scheduler activo — sync cada 5 min entre 7am y 8pm')
