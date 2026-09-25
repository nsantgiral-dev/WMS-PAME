"""
Analítica — capa semántica y KPI diario (Fase 1, 2026-09-24).

`/resumen` alimenta la portada 🎯 ¿Cómo vamos? (`analitica_portada.js`), que
también usa `/kpi/recalcular` (botón de admin cuando el cron está apagado);
`/metricas` alimenta «Cómo se mide cada cifra» de 🩺 Diagnóstico. `/serie`
sigue sin pantalla (tendencia diaria de una métrica: Fase 2, `DEUDA_SIN_UI`).

- `GET  /api/analitica/metricas` — el catálogo.
- `GET  /api/analitica/serie?metrica=&desde=&hasta=&almacen_id=` — un punto
  por día con su estado; el día en curso, en vivo y marcado.
- `GET  /api/analitica/resumen?desde=&hasta=&almacen_id=` — cada métrica:
  período, período anterior de igual duración, variación, semáforo contra la
  meta y alerta de cambio (CUSUM de Vigía); y `portada`: los seis indicadores
  de 🎯 ¿Cómo vamos? con tendencia de 8 semanas, porqué y qué hacer.
- `POST /api/analitica/kpi/recalcular` — solo admin, idempotente, con tope.

Lectura: gestión (`_es_gestion`), el mismo criterio que `/api/auditoria/flujo`
y `/api/analitica/bitacora`. Todo sale de la base: cero llamadas a Siesa.
"""
from datetime import date, datetime, timedelta

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required

from app.routes._auth_helpers import _es_gestion, _solo_admin

analitica_kpi_bp = Blueprint('analitica_kpi', __name__)

#: Por defecto: los últimos 30 días Bogotá, hoy incluido.
DIAS_POR_DEFECTO = 30


class _Invalido(ValueError):
    pass


def _fecha(valor, nombre):
    valor = (valor or '').strip() if isinstance(valor, str) else valor
    if not valor:
        return None
    try:
        return date.fromisoformat(str(valor))
    except ValueError:
        raise _Invalido(f'{nombre} tiene que ser una fecha YYYY-MM-DD: {valor!r}')


def _almacen_id(valor):
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    try:
        almacen_id = int(str(valor).strip())
    except ValueError:
        raise _Invalido(f'almacen_id tiene que ser un número: {valor!r}')
    from app.extensions import db
    from app.models.almacen import Almacen
    if db.session.get(Almacen, almacen_id) is None:
        raise _Invalido(f'almacén {almacen_id} no existe')
    return almacen_id


def _rango(origen, *, tope, hoy):
    """`desde`/`hasta` validados. Nada se ignora: una fecha ilegible, un rango
    invertido, un futuro o un rango más largo que el tope son 400."""
    hasta = _fecha(origen.get('hasta'), 'hasta') or hoy
    desde = _fecha(origen.get('desde'), 'desde') or (hasta - timedelta(days=DIAS_POR_DEFECTO - 1))
    if desde > hasta:
        raise _Invalido(f'desde ({desde}) es posterior a hasta ({hasta})')
    if hasta > hoy:
        raise _Invalido(f'hasta ({hasta}) es futuro: hoy es {hoy} (Bogotá)')
    if (hasta - desde).days + 1 > tope:
        raise _Invalido(f'el rango pasa de {tope} días')
    return desde, hasta


def _meta(desde, hasta, almacen_id):
    from app.services import analitica_kpi as kpi
    return {'desde': desde.isoformat() if desde else None,
            'hasta': hasta.isoformat() if hasta else None,
            'almacen_id': almacen_id,
            'calculado_en': datetime.utcnow().isoformat(),
            'fuentes': {'kpi_diario': kpi.estado()},
            # FECHA_INICIO_AUDITORIA: los días anteriores se muestran marcados
            # (`antes_del_corte`) y no entran al período ni a la alerta.
            'corte': _estado_corte(),
            'metricas_sin_corte': sorted(kpi.METRICAS_SIN_CORTE)}


def _estado_corte():
    from app.services import corte
    return corte.estado()


@analitica_kpi_bp.route('/metricas', methods=['GET'])
@jwt_required()
def listar_metricas():
    """El catálogo: qué mide cada métrica, en qué unidad, hacia dónde es bueno,
    quién la cuida, de dónde sale y cómo se agrega."""
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la analítica'}), 403
    from app.services import analitica_kpi as kpi
    return jsonify({'metricas': kpi.catalogo(), 'meta': _meta(None, None, None)}), 200


@analitica_kpi_bp.route('/serie', methods=['GET'])
@jwt_required()
def serie_metrica():
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la analítica'}), 403
    from app.services import analitica_kpi as kpi
    from app.utils.fecha import dia_operativo
    clave = (request.args.get('metrica') or '').strip()
    if clave not in kpi.METRICAS:
        return jsonify({'error': f'metrica desconocida: {clave!r}',
                        'metricas': sorted(kpi.METRICAS)}), 400
    hoy = dia_operativo()
    try:
        almacen_id = _almacen_id(request.args.get('almacen_id'))
        desde, hasta = _rango(request.args, tope=kpi.TOPE_DIAS_CONSULTA, hoy=hoy)
    except _Invalido as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({
        'metrica': kpi.METRICAS[clave].to_dict(),
        'serie': kpi.serie(clave, desde, hasta, almacen_id, hoy=hoy),
        'meta': _meta(desde, hasta, almacen_id),
    }), 200


@analitica_kpi_bp.route('/resumen', methods=['GET'])
@jwt_required()
def resumen_metricas():
    if not _es_gestion():
        return jsonify({'error': 'Solo gestión puede consultar la analítica'}), 403
    from app.services import analitica_kpi as kpi
    from app.utils.fecha import dia_operativo
    hoy = dia_operativo()
    try:
        almacen_id = _almacen_id(request.args.get('almacen_id'))
        desde, hasta = _rango(request.args, tope=kpi.TOPE_DIAS_CONSULTA, hoy=hoy)
    except _Invalido as e:
        return jsonify({'error': str(e)}), 400
    from app.services import analitica_portada as port
    # Una sola lectura de la cohorte y de las fugas para las dos respuestas:
    # la cifra del catálogo y la de la portada son la misma, no dos cálculos.
    medicion = port.Medicion(desde, hasta, almacen_id, hoy)
    return jsonify({
        'metricas': kpi.resumen(desde, hasta, almacen_id, hoy=hoy, medicion=medicion),
        'portada': port.portada(desde, hasta, almacen_id, hoy=hoy, medicion=medicion),
        'meta': _meta(desde, hasta, almacen_id),
    }), 200


@analitica_kpi_bp.route('/kpi/recalcular', methods=['POST'])
@jwt_required()
def recalcular_kpi():
    """Recalcula y guarda un rango de días a mano. Solo admin: escribe la tabla
    que alimenta las tendencias. Idempotente; tope de días por llamada; el día
    en curso no se guarda. No depende de `ANALITICA_KPI`, que es el interruptor
    del cron: esto es una decisión explícita de una persona."""
    if not _solo_admin():
        return jsonify({'error': 'Solo el administrador puede recalcular el KPI diario'}), 403
    from app.services import analitica_kpi as kpi
    from app.utils.fecha import dia_operativo
    datos = request.get_json(silent=True) or {}
    hoy = dia_operativo()
    try:
        almacen_id = _almacen_id(datos.get('almacen_id'))
        desde = _fecha(datos.get('desde'), 'desde')
        hasta = _fecha(datos.get('hasta'), 'hasta')
        if desde is None or hasta is None:
            raise _Invalido('desde y hasta son obligatorios')
        if desde > hasta:
            raise _Invalido(f'desde ({desde}) es posterior a hasta ({hasta})')
        if hasta >= hoy:
            raise _Invalido(f'hasta ({hasta}) tiene que ser anterior a hoy ({hoy}): '
                            'el día en curso no se guarda')
        if (hasta - desde).days + 1 > kpi.TOPE_DIAS_RECALCULO:
            raise _Invalido(f'el rango pasa de {kpi.TOPE_DIAS_RECALCULO} días: '
                            'pedilo en tandas (es idempotente)')
    except _Invalido as e:
        return jsonify({'error': str(e)}), 400
    try:
        resultado = kpi.recalcular_rango(desde, hasta, almacen_id, hoy=hoy)
    except kpi.RecalculoEnCurso as e:
        return jsonify({'error': str(e)}), 409
    return jsonify(resultado), 200
