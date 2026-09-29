"""
La demanda de compras, calculada en segundo plano y guardada (tanda G,
2026-09-29, m052comprasg).

**La clase:** *un request que calcula en caliente lo que tarda más que el
corte del servidor.* `rop_dual` leía un año de venta diaria y calculaba la
demanda del horizonte de cada SKU: 32–62 s a volumen de producción, contra los
60 s en que gunicorn corta un request. La bandeja, el porqué de un SKU y el
contenedor lo pedían entero.

Ahora el ROP tiene dos partes:

    lo caro   `ArmadorService.calcular_demanda_rop` — la venta, el horizonte,
              el lead time y el régimen de cada SKU. Lo calcula el worker (cron
              cada 10 min, solo si cambió algo que lee) o un hilo que dispara el
              primer request que lo encuentra viejo, con lock. Se guarda en
              `compras_rop_calculo`, una fila por nivel de servicio.
    lo barato `ArmadorService.componer_rop` — esa demanda contra la posición
              de AHORA: existencias, OCs, contenedores y los «ya se pidió» del
              comprador. Al leer: una OC o una decisión se ven en la siguiente
              carga, sin recalcular nada.

**Ningún request calcula lo caro.** Si lo guardado es viejo (llegó venta nueva
o cambió el día), se muestra con su fecha y «recalculando», y se encola el
cálculo. Si nunca se calculó, la respuesta lo dice (`SIN_CALCULO`) sin líneas.

Qué hace viejo lo guardado (`sello_de_la_demanda`): el día, la venta (la
cobertura de la fuente y la última lectura de un tipo que escribe venta —NO
todos los registros: el sync de pedidos abre uno por minuto—), el catálogo
(origen y marca deciden el régimen), las variables de la demanda y el lead
time por defecto. No lo hacen viejo las existencias, las OCs ni las
decisiones: esas se suman al leer.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from flask import current_app, has_request_context

from app.extensions import db
from app.utils.fecha import TZ_BOGOTA, dia_operativo

logger = logging.getLogger(__name__)

#: El nivel de servicio de la bandeja y del contenedor: el que calcula el cron.
NIVEL_POR_DEFECTO = 0.95
#: Cada cuánto mira el cron si lo guardado quedó viejo.
MINUTOS_CRON = 10
#: Variables que cambian la demanda (las de la posición no: se leen al componer).
_ENV_DEMANDA = ('ROP_', 'DEMANDA_', 'KARDEX_DIAS_FRESCURA')

#: {id de la fila: (calculado_en, demanda)}. Leer y parsear ~6 MB de JSON en
#: cada carga costaría lo que se ahorra: uno por proceso.
_PARSEADO = {}
_EN_CURSO = set()
_EN_CURSO_LOCK = threading.Lock()
#: En los tests el «segundo plano» no abre un hilo: queda anotado acá.
PEDIDOS_EN_TESTS = []


def _clave(nivel) -> float:
    return round(float(nivel), 4)


def encendido() -> bool:
    """El cron nace ENCENDIDO: solo escribe esta tabla derivada, no habla con
    Siesa ni mueve nada. `COMPRAS_ROP_CRON=false` lo apaga (los recálculos que
    piden los requests siguen)."""
    return (os.getenv('COMPRAS_ROP_CRON') or '').strip().lower() not in ('false', '0', 'no')


def sello_de_la_demanda(nivel) -> str:
    """Con qué datos se calculó la demanda. Consultas de agregado: milisegundos."""
    from sqlalchemy import func
    from app.models.producto import Producto
    from app.services.armador_service import R_CHINA_DIAS
    from app.services.compras_fuentes import default_lead_time
    from app.services.demanda_fuentes import fuente_de_demanda
    from app.services.kardex_service import sello_de_la_venta
    catalogo = db.session.query(
        func.max(Producto.id), func.count(Producto.origen),
        func.count(Producto.marca_codigo), func.count(Producto.marca_siesa)).one()
    return repr((
        _clave(nivel), dia_operativo().isoformat(),
        sello_de_la_venta(fuente_de_demanda()),
        tuple(catalogo),
        tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith(_ENV_DEMANDA))),
        tuple(sorted(default_lead_time('NACIONAL').items())),
        R_CHINA_DIAS,
    ))


def guardado(nivel):
    from app.models.compras_rop import CalculoRop
    return CalculoRop.query.filter(CalculoRop.nivel_servicio == _clave(nivel)).first()


def _demanda_de(fila) -> dict:
    x = _PARSEADO.get(fila.id)
    if x is None or x[0] != fila.calculado_en:
        x = (fila.calculado_en, json.loads(fila.demanda))
        _PARSEADO.clear()
        _PARSEADO[fila.id] = x
    return x[1]


def calcular_y_guardar(nivel):
    """Lo caro: calcula la demanda y reescribe la fila del nivel. No hace commit
    (lo decide quien llama). Un fallo queda escrito en la fila y se relanza."""
    from app.models.compras_rop import CalculoRop
    from app.services.armador_service import ArmadorService
    t0 = time.time()
    sello = sello_de_la_demanda(nivel)
    fila = guardado(nivel)
    try:
        demanda = ArmadorService.calcular_demanda_rop(_clave(nivel))
    except Exception as e:
        db.session.rollback()
        fila = guardado(nivel) or CalculoRop(nivel_servicio=_clave(nivel))
        fila.error = f'{type(e).__name__}: {e}'[:500]
        fila.error_en = datetime.utcnow()
        db.session.add(fila)
        db.session.commit()
        raise
    if fila is None:
        fila = CalculoRop(nivel_servicio=_clave(nivel))
        db.session.add(fila)
    fila.dia = dia_operativo()
    fila.sello = sello
    fila.calculado_en = datetime.utcnow()
    fila.duracion_s = round(time.time() - t0, 2)
    fila.skus = len(demanda['skus'])
    fila.demanda = json.dumps(demanda, default=str, separators=(',', ':'))
    fila.error = None
    fila.error_en = None
    db.session.flush()
    logger.info('[COMPRAS_ROP] demanda calculada: nivel %s, %d SKU, %.1f s',
                _clave(nivel), fila.skus, fila.duracion_s)
    return fila


def recalcular_si_hace_falta(nivel=NIVEL_POR_DEFECTO) -> dict:
    """La puerta del worker y del hilo: con lock, y solo si lo guardado quedó
    viejo. Nunca levanta."""
    from app.utils.lock import LOCK_COMPRAS_ROP, advisory_lock
    with advisory_lock(LOCK_COMPRAS_ROP, 'compras_rop') as tomado:
        if not tomado:
            return {'hecho': False, 'motivo': 'OTRO_PROCESO_CALCULANDO'}
        try:
            fila = guardado(nivel)
            if fila is not None and fila.demanda and fila.sello == sello_de_la_demanda(nivel):
                return {'hecho': False, 'motivo': 'AL_DIA'}
            fila = calcular_y_guardar(nivel)
            db.session.commit()
            return {'hecho': True, 'skus': fila.skus, 'duracion_s': fila.duracion_s}
        except Exception as e:                                 # noqa: BLE001
            db.session.rollback()
            logger.error('[COMPRAS_ROP] el recálculo falló: %s', e, exc_info=True)
            return {'hecho': False, 'motivo': 'ERROR', 'error': str(e)[:300]}


def _en_segundo_plano(app, nivel):
    try:
        with app.app_context():
            recalcular_si_hace_falta(nivel)
    finally:
        with _EN_CURSO_LOCK:
            _EN_CURSO.discard(nivel)


def pedir_recalculo(nivel) -> bool:
    """Encola el recálculo en un hilo (uno por nivel y por proceso; entre
    procesos, el lock). Devuelve True: hay un recálculo en curso."""
    app = current_app._get_current_object()
    clave = _clave(nivel)
    with _EN_CURSO_LOCK:
        if clave in _EN_CURSO:
            return True
        _EN_CURSO.add(clave)
    if app.config.get('TESTING') and not app.config.get('COMPRAS_ROP_HILO_EN_TESTS'):
        PEDIDOS_EN_TESTS.append(clave)
        with _EN_CURSO_LOCK:
            _EN_CURSO.discard(clave)
        return True
    threading.Thread(target=_en_segundo_plano, args=(app, clave), daemon=True,
                     name=f'compras-rop-{clave}').start()
    return True


def _modo() -> str:
    """SIEMPRE (tests: cada uno arma su mundo; se calcula, se guarda y se lee
    de vuelta del JSON) · SEGUNDO_PLANO (un request: nunca calcula) · AQUI
    (worker, scripts: si hace falta, calcula)."""
    cfg = current_app.config
    if cfg.get('TESTING') and not cfg.get('COMPRAS_ROP_PERSISTIDO_EN_TESTS'):
        return 'SIEMPRE'
    if has_request_context():
        return 'SEGUNDO_PLANO'
    return 'AQUI'


def _hora_bogota(momento_utc) -> str:
    if momento_utc is None:
        return 'sin fecha'
    return (momento_utc.replace(tzinfo=timezone.utc).astimezone(TZ_BOGOTA)
            .strftime('%d/%m a las %H:%M'))


def declaracion(fila, al_dia: bool, recalculando: bool) -> dict:
    """Lo que la pantalla tiene que decir de la demanda que muestra."""
    hay = fila is not None and bool(fila.demanda)
    if not hay:
        estado = 'SIN_CALCULO'
        texto = ('La demanda de compras todavía no se calculó'
                 + (': se está calculando, vuelva a cargar en unos minutos.' if recalculando else '.'))
    elif al_dia:
        estado = 'AL_DIA'
        texto = f'Demanda calculada el {_hora_bogota(fila.calculado_en)} (hora de Bogotá).'
    else:
        estado = 'DESACTUALIZADO'
        texto = (f'Se muestra la demanda calculada el {_hora_bogota(fila.calculado_en)}: llegó '
                 'venta nueva o cambió el día'
                 + (' y se está recalculando; vuelva a cargar en unos minutos.' if recalculando
                    else '.')
                 + ' Existencias, órdenes de compra y lo decidido sí son de ahora.')
    if fila is not None and fila.error:
        texto += (f' El último recálculo falló ({_hora_bogota(fila.error_en)}): '
                  f'{fila.error[:200]}')
    return {
        'estado': estado,
        'dia': fila.dia.isoformat() if hay and fila.dia else None,
        'calculado_utc': fila.calculado_en.isoformat() if hay and fila.calculado_en else None,
        'duracion_s': fila.duracion_s if hay else None,
        'skus': fila.skus if hay else None,
        'al_dia': bool(hay and al_dia),
        'de_hoy': bool(hay and fila.dia == dia_operativo()),
        'recalculando': bool(recalculando and not (hay and al_dia)),
        'error': fila.error if fila is not None else None,
        'texto': texto,
    }


def _sin_calculo(nivel, calculo: dict) -> dict:
    """Sin ninguna demanda guardada: vacío y dicho. El contenedor lo lee como
    NO APTO (`insumo_demanda` sin `apta_para`)."""
    from app.services.compras_fuentes import default_lead_time
    chi = default_lead_time('CHINA')
    return {
        'nivel_servicio': _clave(nivel),
        'calculo': calculo,
        'insumo_demanda': {'apta_para': {}, 'texto': calculo['texto'],
                           'no_apta_por': {'contenedor': calculo['texto']}},
        'insumo_en_camino': {}, 'insumo_origen': {}, 'demanda_horizonte': None,
        'nacional': {'total': 0, 'bajo_rop': 0, 'items': []},
        'china': {'total': 0, 'con_deficit': 0, 'items': [], 'lt_dias': chi['lt_dias'],
                  'sigma_lt': chi['sigma_lt'], 'sigma_lt_fuente': chi['fuente']},
    }


def rop(nivel=NIVEL_POR_DEFECTO) -> dict:
    """El ROP que ve quien pregunta: la demanda guardada + la posición de ahora.
    La única puerta de `ArmadorService.rop_dual`."""
    from app.services.armador_service import ArmadorService
    modo = _modo()
    fila = guardado(nivel)
    al_dia = bool(fila is not None and fila.demanda
                  and fila.sello == sello_de_la_demanda(nivel))
    recalculando = False
    if modo == 'SIEMPRE' or (modo == 'AQUI' and not al_dia):
        fila = calcular_y_guardar(nivel)
        if modo == 'AQUI':
            db.session.commit()
        al_dia = True
    elif not al_dia:
        recalculando = pedir_recalculo(nivel)
    calculo = declaracion(fila, al_dia, recalculando)
    if fila is None or not fila.demanda:
        return _sin_calculo(nivel, calculo)
    resultado = ArmadorService.componer_rop(_demanda_de(fila))
    resultado['calculo'] = calculo
    return resultado


def init_scheduler(app):
    """Cada 10 min: si la demanda guardada quedó vieja (venta nueva, otro día),
    la recalcula. Así la lectura diaria de la venta se ve en la bandeja sin que
    nadie espere."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.error('[COMPRAS_ROP] APScheduler no instalado')
        return None

    def _job():
        if not encendido():
            return
        with app.app_context():
            logger.info('[COMPRAS_ROP] %s', recalcular_si_hace_falta(NIVEL_POR_DEFECTO))

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    from app.services.cron_latido import con_latido
    scheduler.add_job(func=con_latido('compras_rop', _job),
                      trigger=IntervalTrigger(minutes=MINUTOS_CRON),
                      id='compras_rop', replace_existing=True,
                      max_instances=1, misfire_grace_time=600)
    scheduler.start()
    logger.info('[COMPRAS_ROP] Scheduler (encendido=%s)', encendido())
    return scheduler
