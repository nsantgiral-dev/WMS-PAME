"""
Retención de cartera: ¿este pedido de crédito real puede salir? **Una función.**

## La regla del dueño (2026-09-24)

- Solo aplica a CRÉDITO REAL: `cond_pago.cobro_contraentrega(cond)['cobrar']
  is False` (> 15 días, días CONOCIDOS). Contado (≤ 15, y todo lo supuesto)
  **siempre sale**: se cobra al entregar.
- Cliente con CUALQUIER factura vencida (saldo > `CARTERA_TOLERANCIA_SALDO`,
  $5.000) → no se despacha a crédito. Sin umbral de 30 días.
  Excepción: canal INSTITUCIONAL (licitaciones, colegios, entidades) nunca se
  retiene por mora — el cupo sí le aplica. Sin dato de canal no se exime.
- Con lo que ya debe + lo que el WMS ya inició y Siesa todavía no factura +
  este pedido, supera el cupo → no se despacha. Cupo exacto pasa.
- Crédito real sin cupo asignado (0 o nulo) → se retiene.
- Acuerdo de pago vigente con cartera → el pedido nuevo sale SOLO de contado.
- Lo que el conductor ya cobró y el recibo de caja todavía no aplicó en Siesa
  NO es deuda del cliente: es plata del conductor (EN_CAJA).
- La excepción la da un usuario de cartera, con motivo y nombre, desde el
  Gestor de Cartera (`/api/cartera/*`) o desde el WMS con
  `puede_autorizar_cartera`. Quien inició el pedido no puede autorizarlo.

## Por qué el WMS necesita su propia compuerta

Medido en vivo contra Siesa QA (2026-09-24, solo GET): Siesa retiene por cupo
y mora al aprobar, pero el mismo usuario la libera en ~4 s y sin motivo; 15 de
18 pedidos a crédito de clientes con vencidas no quedaron retenidos.

Y el «maestro de clientes duplicado» no era un duplicado: `API_v2_Clientes`
devuelve **las dos compañías** (`f201_id_cia` 1 y 2). De 3.783 NIT+sucursal,
2.217 tienen fila en las dos; dentro de la compañía 1 no hay ni un duplicado.
Los cupos viejos viven en la compañía 2 (135 clientes con cupo) y en la 1 solo
25 lo tienen. Se usa la fila de `SIESA_ID_CIA` (Regla 2) y la de la otra
compañía se DECLARA (`MAESTRO_DUPLICADO`, no retiene): es la explicación
probable de «tenía cupo y ahora no».

## Dónde se pregunta

| | Cuándo | Qué hace |
|---|---|---|
| G1 `compuerta_inicio` | «Aprobar» en la cola, antes de crear el picking | Evalúa con el valor pendiente del pedido. Retiene → 409 y fila en `retenciones_cartera` |
| G2 `compuerta_cierre` | Cerrar la caja, antes del 244328 | Evalúa con el valor EMPACADO. Retiene → la caja queda VERIFICADA con sus bultos |
| EMISIÓN `compuerta_emision` | Dentro de `despachar_parcial`, con la cabecera de Siesa en la mano | Sin decisión previa (job anterior, carril admin, condición que era supuesta y la cabecera dice crédito) evalúa ahí. Retiene → `RetenidoPorCartera` (el DLQ espera sin gastar reintento) |
| G3 `informe_de_tarea` | Iniciar/despachar la ruta | Solo informa |

Todo lo de Siesa son GET (Regla 3: reintentar es seguro). Nada de esto emite
un documento.
"""
import json
import logging
import os
import zlib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models.cartera import (CarteraCliente, CarteraHabilitacion, Compuerta,
                                EstadoRetencion, RetencionCartera)
from app.services import cond_pago as _cp
from app.services.siesa_job_service import DependenciaPendiente
from app.utils.fecha import ahora_bogota, dia_operativo, TZ_BOGOTA

logger = logging.getLogger(__name__)

#: Sube cada vez que cambia una regla de `evaluar`. Viaja en la API: el Gestor
#: tiene que poder saber con qué política se decidió una retención vieja.
VERSION_POLITICA = '2026-09-24.1'

TOLERANCIA_DEFECTO = Decimal('5000')
#: Una foto de cartera más vieja que esto no vale: es «sin dato».
FRESCURA_FOTO = timedelta(hours=24)
#: Dentro de una misma petición o barrido no se relee el mismo NIT.
REUSO_LECTURA = timedelta(minutes=2)
#: Regla 14: Siesa no opera después de ~8 p. m. Fuera de esta ventana no se le
#: pregunta (30 s de timeout por consulta) y se usa la foto.
VENTANA_SIESA = (time(6, 0), time(20, 0))
#: Un recibo de caja tarda en verse en la cartera (la indexación de una FE ya
#: tardó minutos en QA). Dentro de esta ventana lo cobrado se sigue restando.
MARGEN_RC = timedelta(hours=48)
#: Una retención viva más vieja que esto es una alerta.
ALERTA_RETENIDO = timedelta(days=3)
#: Tope de vigencia de una autorización (decisión del Gestor: Art. 10, ≤ 30 días).
VIGENCIA_MAX_AUTORIZACION = 30
CODIGOS_EXCEPCION = ('E1', 'E2a', 'E2b', 'E2c', 'E3', 'E4')

CANALES = ('MAYORISTA', 'INSTITUCIONAL', 'DETAL', 'RUTA', 'OTRO')
CANAL_EXENTO_DE_MORA = 'INSTITUCIONAL'

MODOS = ('RETIENE', 'INFORMA')

PASA = 'PASA'
RETIENE = 'RETIENE'

# Motivos: códigos estables (el Gestor los traduce) + texto + cifras.
MORA = 'MORA'
CUPO_EXCEDIDO = 'CUPO_EXCEDIDO'
SIN_CUPO = 'SIN_CUPO'
SIN_DATO = 'SIN_DATO'
ACUERDO_VIGENTE = 'ACUERDO_VIGENTE'
VALOR_DESCONOCIDO = 'VALOR_DESCONOCIDO'
MAESTRO_DUPLICADO = 'MAESTRO_DUPLICADO'   # se declara, nunca retiene
MORA_EXENTA = 'MORA_EXENTA'               # institucional: se declara, no retiene
CODIGOS_MOTIVO = (MORA, CUPO_EXCEDIDO, SIN_CUPO, SIN_DATO, ACUERDO_VIGENTE,
                  VALOR_DESCONOCIDO, MAESTRO_DUPLICADO, MORA_EXENTA)

# Clases de una fila de cartera.
DEUDA = 'DEUDA'
EN_RUTA = 'EN_RUTA'
EN_CAJA = 'EN_CAJA'
RESIDUO = 'RESIDUO'
DEVUELTA = 'DEVUELTA'
A_FAVOR = 'A_FAVOR'
SALDADA = 'SALDADA'

# Origen del dato de cartera.
SIESA = 'SIESA'
FOTO = 'FOTO'
SIN_DATO_ORIGEN = 'SIN_DATO'
SIMULACION = 'SIMULACION'

API_CLIENTES = 'API_v2_Clientes'
API_CARTERA = 'API_v2_CxC_General'
API_PEDIDOS = 'API_v2_Ventas_Pedidos'
#: Mismo universo que la foto diaria de cartera (`fotos_siesa_service`):
#: cuentas 1305 (clientes) abiertas. `f353_fecha_cancelacion IS NULL` es lo
#: que hace de 50 páginas una por NIT (medido: 0,5 s).
_FILTRO_CARTERA_NIT = ("f200_id = {nit} AND f353_fecha_cancelacion IS NULL "
                       "AND f253_id LIKE ''1305%''")


class CarteraNoDisponible(Exception):
    """Siesa no contestó (o contestó a medias) la cartera de un NIT."""


class RetenidoPorCartera(DependenciaPendiente):
    """La emisión de un crédito real quedó retenida por cartera.

    Hereda de `DependenciaPendiente` a propósito: en el DLQ esperar a que
    cartera decida **no es un fallo** y no gasta reintento. La autorización
    (o el pago) la destraba; `resolver` adelanta el job.
    """

    def __init__(self, mensaje: str, retencion_id: int = None):
        super().__init__(mensaje, espera_minutos=30)
        self.retencion_id = retencion_id


class DespachoEnCurso(Exception):
    """Otro despacho a crédito del mismo cliente se está evaluando ahora."""


# ═════════════════════════════════════════════════════════════════════════════
# Configuración
# ═════════════════════════════════════════════════════════════════════════════

def _dec(v, defecto=None):
    if v is None or v == '':
        return defecto
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return defecto


def tolerancia() -> tuple:
    """`(Decimal, problema)` de `CARTERA_TOLERANCIA_SALDO`. Inválido → 5.000,
    declarado."""
    crudo = os.getenv('CARTERA_TOLERANCIA_SALDO')
    if crudo is None or not str(crudo).strip():
        return TOLERANCIA_DEFECTO, None
    v = _dec(str(crudo).strip())
    if v is None or v < 0:
        return TOLERANCIA_DEFECTO, (f'CARTERA_TOLERANCIA_SALDO={crudo!r} no es un monto >= 0: '
                                    f'se usa {TOLERANCIA_DEFECTO}')
    return v, None


def modo() -> tuple:
    """`(modo, problema)` de `CARTERA_COMPUERTA`: `RETIENE` (defecto) o
    `INFORMA` (evalúa, registra y deja pasar — para el arranque). Un valor
    que no se entiende NO apaga la compuerta: queda RETIENE, declarado."""
    crudo = (os.getenv('CARTERA_COMPUERTA') or '').strip().upper()
    if not crudo:
        return 'RETIENE', None
    if crudo in MODOS:
        return crudo, None
    return 'RETIENE', f'CARTERA_COMPUERTA={crudo!r} no es RETIENE ni INFORMA: se usa RETIENE'


def id_cia() -> str:
    return (os.getenv('SIESA_ID_CIA') or '1').strip()


def parametros() -> dict:
    tol, p_tol = tolerancia()
    m, p_modo = modo()
    umbral, p_umbral = _cp.umbral_dias()
    return {
        'version': VERSION_POLITICA,
        'modo': m,
        'tolerancia_saldo': float(tol),
        'umbral_contado_dias': umbral,
        'frescura_foto_horas': int(FRESCURA_FOTO.total_seconds() // 3600),
        'margen_recibo_caja_horas': int(MARGEN_RC.total_seconds() // 3600),
        'vigencia_max_autorizacion_dias': VIGENCIA_MAX_AUTORIZACION,
        'canal_exento_de_mora': CANAL_EXENTO_DE_MORA,
        'id_cia': id_cia(),
        'problemas': [p for p in (p_tol, p_modo, p_umbral) if p],
    }


def nit_normalizado(nit) -> str:
    """NIT sin dígito de verificación, como lo trae `f200_id`. Acepta
    `'900123456-7'` del Gestor y lo deja en `'900123456'`."""
    s = str(nit or '').strip()
    if '-' in s:
        s = s.split('-', 1)[0]
    return s.replace('.', '').replace(' ', '')


def sucursal_normalizada(suc) -> str:
    s = str(suc or '').strip()
    return s.zfill(3) if s.isdigit() else s


# ═════════════════════════════════════════════════════════════════════════════
# La fuente: Siesa (solo GET). Un puerto, para que los tests lo reemplacen.
# ═════════════════════════════════════════════════════════════════════════════

class FuenteSiesa:
    """Lee de Siesa lo que la compuerta necesita. Levanta
    `CarteraNoDisponible` ante cualquier lectura incompleta: una cartera a
    medias afirma una deuda que no es la verdadera."""

    def __init__(self, gateway=None):
        self._gw = gateway

    @property
    def gateway(self):
        if self._gw is None:
            from app.services.connekta_gateway import connekta
            return connekta
        return self._gw

    def simulada(self) -> bool:
        return bool(getattr(self.gateway, 'modo_simulacion', False))

    def en_ventana(self, ahora=None) -> bool:
        h = (ahora or ahora_bogota()).time()
        return VENTANA_SIESA[0] <= h < VENTANA_SIESA[1]

    def _leer(self, api, filtro, clave):
        from app.services.fotos_siesa_service import leer_paginado
        lectura = leer_paginado(self.gateway, api, filtro, clave=clave)
        if not lectura.completa:
            raise CarteraNoDisponible(f'{api}: {lectura.motivo}')
        return lectura.filas

    def clientes(self, nit: str) -> list:
        from app.services.siesa_filtro import lit
        return self._leer(API_CLIENTES, f'f200_id = {lit(nit)}',
                          clave=lambda f: (f.get('f201_id_cia'), f.get('f200_rowid'),
                                           f.get('f201_id_sucursal')))

    def cartera(self, nit: str) -> list:
        from app.services.siesa_filtro import lit
        return self._leer(API_CARTERA, _FILTRO_CARTERA_NIT.format(nit=lit(nit)),
                          clave=lambda f: f.get('f353_rowid'))

    def pedido(self, co, tipo, consec) -> list:
        from app.services.siesa_filtro import lit
        try:
            consec_i = int(str(consec).strip())
        except (TypeError, ValueError):
            return []
        filtro = (f'f430_id_co = {lit(co)} AND f430_id_tipo_docto = {lit(tipo)} '
                  f'AND f430_consec_docto = {consec_i}')
        return self._leer(API_PEDIDOS, filtro, clave=lambda f: f.get('f431_rowid'))


_FUENTE = None


def fuente():
    return _FUENTE or FuenteSiesa()


def usar_fuente(f):
    """Reemplaza la fuente (tests: un FakeSiesa). `None` vuelve a Siesa."""
    global _FUENTE
    _FUENTE = f


# ═════════════════════════════════════════════════════════════════════════════
# La cartera de un NIT, con foto de respaldo
# ═════════════════════════════════════════════════════════════════════════════

def cartera_de(nit: str, forzar: bool = False) -> dict:
    """`{clientes, filas, as_of, origen, error, edad_horas}`.

    Origen: `SIESA` (se acaba de leer), `FOTO` (Siesa no contestó o es de
    noche, y la última lectura completa tiene < 24 h), `SIN_DATO` (ni una ni
    otra) o `SIMULACION` (sin credenciales: no hay cartera que leer). Nunca
    levanta; escribe la foto sin commit (va en la transacción de quien llama).
    """
    nit = nit_normalizado(nit)
    f = fuente()
    foto = CarteraCliente.query.filter_by(nit=nit).first()
    ahora = datetime.utcnow()
    if f.simulada():
        return {'clientes': [], 'filas': [], 'as_of': None, 'origen': SIMULACION,
                'error': None, 'edad_horas': None}
    if (not forzar and foto is not None and foto.as_of is not None
            and ahora - foto.as_of < REUSO_LECTURA):
        return _de_foto(foto, SIESA, ahora)
    error = None
    if f.en_ventana():
        try:
            clientes = f.clientes(nit)
            filas = f.cartera(nit)
            if foto is None:
                foto = CarteraCliente(nit=nit)
                db.session.add(foto)
            foto.clientes, foto.filas = clientes, filas
            foto.as_of = ahora
            foto.ultimo_intento_en = ahora
            foto.ultimo_error = None
            db.session.flush()
            return _de_foto(foto, SIESA, ahora)
        except Exception as e:  # noqa: BLE001 — se declara, no se levanta
            error = str(e)[:300]
            logger.warning('[CARTERA] NIT %s: Siesa no dio la cartera (%s)', nit, error)
    else:
        error = 'fuera de la ventana de Siesa (Regla 14): se usa la foto'
    if foto is not None:
        foto.ultimo_intento_en = ahora
        foto.ultimo_error = error
        if foto.as_of is not None and ahora - foto.as_of < FRESCURA_FOTO:
            r = _de_foto(foto, FOTO, ahora)
            r['error'] = error
            return r
    return {'clientes': [], 'filas': [], 'as_of': foto.as_of if foto else None,
            'origen': SIN_DATO_ORIGEN, 'error': error,
            'edad_horas': _edad_horas(foto.as_of, ahora) if foto and foto.as_of else None}


def _edad_horas(as_of, ahora):
    return round((ahora - as_of).total_seconds() / 3600, 1)


def _de_foto(foto, origen, ahora):
    return {'clientes': list(foto.clientes or []), 'filas': list(foto.filas or []),
            'as_of': foto.as_of, 'origen': origen, 'error': None,
            'edad_horas': _edad_horas(foto.as_of, ahora)}


# ═════════════════════════════════════════════════════════════════════════════
# Maestro de clientes: cupo, gracia, duplicados
# ═════════════════════════════════════════════════════════════════════════════

def _ts(fila):
    return str(fila.get('f201_ts') or '')


def maestro(clientes: list) -> dict:
    """Cupo del NIT (suma de sus sucursales en la compañía propia), gracia por
    sucursal y lo que hay que declarar del maestro.

    La fila de la compañía propia es la que Siesa usa para ESTA compañía. Si
    hay dos de la misma compañía y sucursal (duplicado de verdad), se usa la
    más reciente (`f201_ts`) y se declara. Las de otra compañía se declaran
    con su cupo — no se usan.
    """
    cia = id_cia()
    propias, otras = {}, []
    duplicadas = []
    for f in clientes or []:
        suc = sucursal_normalizada(f.get('f201_id_sucursal'))
        if str(f.get('f201_id_cia', cia)).strip() != cia:
            otras.append({'cia': f.get('f201_id_cia'), 'sucursal': suc,
                          'cupo': float(_dec(f.get('f201_cupo_credito'), Decimal(0)))})
            continue
        previa = propias.get(suc)
        if previa is not None:
            duplicadas.append(suc)
            if _ts(f) <= _ts(previa):
                continue
        propias[suc] = f
    sucursales = {}
    cupo_total = Decimal(0)
    hay_cupo_leido = False
    for suc, f in propias.items():
        cupo = _dec(f.get('f201_cupo_credito'))
        if cupo is not None:
            hay_cupo_leido = True
            cupo_total += cupo
        sucursales[suc] = {
            'cupo': float(cupo) if cupo is not None else None,
            'dias_gracia': int(_dec(f.get('f201_dias_gracia'), Decimal(0))),
            'cond_pago': f.get('f201_id_cond_pago'),
            'bloqueo_cupo': f.get('f201_ind_bloqueo_cupo'),
            'bloqueo_mora': f.get('f201_ind_bloqueo_mora'),
            'tipo_cliente': f.get('f201_id_tipo_cli'),
            'nombre': f.get('f201_descripcion_sucursal'),
        }
    return {
        'encontrado': bool(propias),
        'cupo': cupo_total if hay_cupo_leido else None,
        'sucursales': sucursales,
        'duplicado_misma_cia': sorted(set(duplicadas)),
        'otras_companias': otras,
    }


# ═════════════════════════════════════════════════════════════════════════════
# Clasificación de las filas de cartera
# ═════════════════════════════════════════════════════════════════════════════

def _fecha(v):
    s = str(v or '').strip()
    try:
        if len(s) >= 10 and s[4] == '-':
            return datetime.strptime(s[:10], '%Y-%m-%d').date()
        if len(s) >= 8 and s[:8].isdigit():
            return datetime.strptime(s[:8], '%Y%m%d').date()
    except ValueError:
        return None
    return None


def _tareas_por_factura(filas: list) -> dict:
    """`{(tipo, consec): TareaPacking}` de las FE del WMS que aparecen en la
    cartera. La cartera de las FE del WMS se indexa por FACTURA (hallazgo
    2026-09-24, `cxc_cruce`)."""
    from app.models.packing import TareaPacking
    consecs = {str(f.get('f353_consec_docto_cruce') or '').strip() for f in filas}
    consecs.discard('')
    if not consecs:
        return {}
    out = {}
    for t in TareaPacking.query.filter(TareaPacking.fe_consec.in_(list(consecs))).all():
        out[((t.fe_tipo or '').strip(), str(t.fe_consec).strip())] = t
    return out


def _recaudo_de(tarea):
    from app.models.recaudo_entrega import RecaudoEntrega
    return (RecaudoEntrega.query.filter_by(tarea_id=tarea.id)
            .order_by(RecaudoEntrega.id.desc()).first())


def _retenciones_de(recaudo) -> Decimal:
    total = Decimal(0)
    for r in (getattr(recaudo, 'retenciones_detalle', None) or []):
        if isinstance(r, dict):
            total += _dec(r.get('monto'), Decimal(0))
    return total


def _vence_real(fila, tarea):
    """Vencimiento con el que se juzga la mora. FE del WMS: la política
    (`cond_pago.vencimiento_fe` sobre la fecha del documento); las emitidas
    antes del 2026-09-24 vencen a +30 fijo en Siesa y eso es un artefacto.
    FE que no emitió el WMS: `f353_fecha_vcto` de Siesa."""
    if tarea is None:
        return _fecha(fila.get('f353_fecha_vcto'))
    base = _fecha(fila.get('f353_fecha'))
    if base is None:
        return _fecha(fila.get('f353_fecha_vcto'))
    try:
        return datetime.strptime(
            _cp.vencimiento_fe(base, _cp.codigo_vigente(tarea)), '%Y%m%d').date()
    except Exception:  # noqa: BLE001
        return _fecha(fila.get('f353_fecha_vcto'))


def clasificar_filas(filas: list, gracia_por_sucursal: dict = None,
                     hoy: date = None, ahora: datetime = None) -> list:
    """Una entrada por fila abierta: qué es, cuánto cuenta como deuda del
    cliente y si está vencida.

    · A_FAVOR   saldo negativo (anticipo, NC sin aplicar): resta.
    · SALDADA   |saldo| ≤ tolerancia.
    · DEUDA     FE ajena al WMS; o del WMS entregada sin pago, crédito real o
                autorizado, crédito no autorizado, o el resto de un parcial.
    · EN_RUTA   FE del WMS todavía sin confirmar entrega: cuenta, con su
                vencimiento real.
    · EN_CAJA   el conductor cobró y el RC todavía no se aplicó (o se aplicó
                hace < 48 h): se resta lo cobrado. No es deuda del cliente.
    · RESIDUO   RC aplicado y lo que queda ≤ retenciones + descuento +
                tolerancia: no es mora ni cuenta.
    · DEVUELTA  la mercancía volvió (RECHAZADO), o parcial con la NC ya
                creada y pendiente de aprobar: no cuenta.
    """
    from app.models.recaudo_entrega import EstadoEntrega
    tol, _ = tolerancia()
    hoy = hoy or dia_operativo()
    ahora = ahora or datetime.utcnow()
    gracia_por_sucursal = gracia_por_sucursal or {}
    cia = id_cia()
    tareas = _tareas_por_factura(filas)
    out = []
    for f in filas or []:
        if 'f353_id_cia' in f and str(f.get('f353_id_cia')).strip() != cia:
            continue
        saldo = _dec(f.get('f353_total_db'), Decimal(0)) - _dec(f.get('f353_total_cr'), Decimal(0))
        tipo = str(f.get('f353_id_tipo_docto_cruce') or '').strip()
        consec = str(f.get('f353_consec_docto_cruce') or '').strip()
        suc = sucursal_normalizada(f.get('f201_id_sucursal'))
        tarea = tareas.get((tipo, consec))
        e = {'documento': f'{tipo}-{consec}' if tipo else consec,
             'co': str(f.get('f353_id_co_cruce') or '').strip() or None,
             'tipo': tipo, 'consec': consec, 'sucursal': suc,
             'fecha': (_fecha(f.get('f353_fecha')) or None),
             'vence_siesa': _fecha(f.get('f353_fecha_vcto')),
             'saldo': saldo, 'wms': tarea is not None,
             'tarea_id': tarea.id if tarea is not None else None,
             'pedido': getattr(tarea, 'numero_pedido_siesa', None),
             'cuenta': Decimal(0), 'vencida': False, 'dias_vencida': None,
             'restado': Decimal(0)}
        if saldo < -tol:
            e.update(clase=A_FAVOR, a_favor=-saldo)
            out.append(e)
            continue
        if saldo <= tol:
            e.update(clase=SALDADA)
            out.append(e)
            continue
        vence = _vence_real(f, tarea)
        e['vence'] = vence
        cuenta, clase = saldo, DEUDA
        if tarea is not None:
            r = _recaudo_de(tarea)
            if r is None:
                clase = EN_RUTA
            elif r.estado_entrega == EstadoEntrega.RECHAZADO:
                clase, cuenta = DEVUELTA, Decimal(0)
            elif r.estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO:
                clase = DEUDA
            else:
                trato = _cp.trato_de_cobro(r, tarea)
                if trato == _cp.TRATO_CONTADO:
                    cobrado = (_dec(r.monto_cobrado, Decimal(0))
                               + _dec(r.monto_descuento, Decimal(0)) + _retenciones_de(r))
                    rc_aplicado = (r.siesa_rc_resultado in ('ENVIADO', 'YA_SALDADA')
                                   and r.siesa_rc_at is not None
                                   and ahora - r.siesa_rc_at >= MARGEN_RC)
                    if not rc_aplicado:
                        restante = max(Decimal(0), saldo - cobrado)
                        e['restado'] = saldo - restante
                        clase, cuenta = EN_CAJA, restante
                        if restante > tol and r.estado_entrega == EstadoEntrega.PARCIAL:
                            clase = DEVUELTA if r.siesa_nc_triggered else DEUDA
                            cuenta = Decimal(0) if r.siesa_nc_triggered else restante
                        elif restante > tol:
                            clase = DEUDA
                    else:
                        residuo_ok = (_dec(r.monto_descuento, Decimal(0))
                                      + _retenciones_de(r) + tol)
                        if saldo <= residuo_ok:
                            clase, cuenta = RESIDUO, Decimal(0)
                        elif r.estado_entrega == EstadoEntrega.PARCIAL and r.siesa_nc_triggered:
                            clase, cuenta = DEVUELTA, Decimal(0)
                        else:
                            clase = DEUDA
                else:
                    clase = DEUDA
        e.update(clase=clase, cuenta=cuenta)
        if cuenta > tol and vence is not None:
            gracia = int((gracia_por_sucursal.get(suc) or {}).get('dias_gracia') or 0)
            dias = (hoy - vence).days
            if dias > gracia:
                e.update(vencida=True, dias_vencida=dias)
        out.append(e)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Lo que el WMS ya inició y Siesa todavía no factura
# ═════════════════════════════════════════════════════════════════════════════

def lineas_de_pedido(pedido_clave: str) -> list:
    """Líneas del pedido desde la historia del sync (sin red)."""
    from app.models.pedido_historia import PedidoHistoria
    if not pedido_clave:
        return []
    return [{'item_codigo': (h.item_codigo or '').strip(), 'vlr_neto': h.vlr_neto,
             'pedida': h.cantidad_pedida, 'remisionada': h.cantidad_remisionada,
             'cliente_id': h.cliente_id, 'cliente': h.cliente,
             'vendedor_id': h.vendedor_id, 'cond_pago': h.cond_pago}
            for h in PedidoHistoria.query.filter_by(pedido_clave=pedido_clave).all()]


def lineas_de_siesa(filas: list) -> list:
    out = []
    for f in filas or []:
        out.append({'item_codigo': str(f.get('f120_referencia') or '').strip(),
                    'vlr_neto': _dec(f.get('f431_vlr_neto')),
                    'pedida': _dec(f.get('f431_cant1_pedida')),
                    'remisionada': _dec(f.get('f431_cant1_remisionada'), Decimal(0)),
                    'cliente_id': str(f.get('f200_id_pedido_fact') or '').strip() or None,
                    'cliente': f.get('f200_razon_social_pedido_fact'),
                    'vendedor_id': str(f.get('f200_id_pedido_vend') or '').strip() or None,
                    'cond_pago': str(f.get('f430_id_cond_pago') or '').strip() or None})
    return out


def _pendiente_linea(l) -> Decimal:
    """Valor pendiente de una línea: neto × (pedida − remisionada) / pedida.
    `None` si la línea no trae con qué calcularlo."""
    neto, ped = _dec(l.get('vlr_neto')), _dec(l.get('pedida'))
    rem = _dec(l.get('remisionada'), Decimal(0))
    if neto is None or ped is None or ped <= 0:
        return None
    return neto * max(Decimal(0), ped - rem) / ped


def valor_de_items(lineas: list, items: list = None) -> tuple:
    """`(valor, completo)`. Con `items` (G1: `[{item_codigo,
    cantidad_pendiente}]`, en la unidad del pedido, igual que la historia):
    Σ cantidad × neto/pedida. Sin `items`: todo lo pendiente del pedido."""
    if not lineas:
        return None, False
    if items is None:
        total, completo = Decimal(0), True
        for l in lineas:
            v = _pendiente_linea(l)
            if v is None:
                completo = False
                continue
            total += v
        return total, completo
    por_item = {}
    for l in lineas:
        por_item.setdefault(l['item_codigo'], []).append(l)
    total, completo = Decimal(0), True
    for it in items:
        cod = str(it.get('item_codigo') or '').strip()
        ls = por_item.get(cod) or []
        neto = sum((_dec(l.get('vlr_neto'), Decimal(0)) for l in ls), Decimal(0))
        ped = sum((_dec(l.get('pedida'), Decimal(0)) for l in ls), Decimal(0))
        cant = _dec(it.get('cantidad_pendiente'))
        if not ls or ped <= 0 or cant is None:
            completo = False
            continue
        total += cant * neto / ped
    return total, completo


def valor_empacado(tarea, lineas: list = None) -> tuple:
    """`(valor, completo)` de lo que va en la caja: por ítem, lo pendiente de
    su(s) línea(s) × `min(1, empacado / esperado)`. Las cantidades del packing
    van en UND y las del pedido en su unidad; la razón empacado/esperado no
    depende de la unidad."""
    lineas = lineas if lineas is not None else lineas_de_pedido(tarea.pedido_clave)
    if not lineas:
        return None, False
    por_item = {}
    for l in lineas:
        por_item.setdefault(l['item_codigo'], []).append(l)
    total, completo = Decimal(0), True
    for it in tarea.items:
        prod = it.producto
        cod = ((getattr(prod, 'codigo_siesa', None) or getattr(prod, 'codigo', None) or '')
               .strip())
        pend = [_pendiente_linea(l) for l in por_item.get(cod, [])]
        if not pend or any(p is None for p in pend):
            completo = False
            continue
        esperado = Decimal(it.cantidad_esperada or 0)
        real = Decimal(it.cantidad_real if it.cantidad_real is not None else 0)
        if esperado <= 0:
            continue
        total += sum(pend, Decimal(0)) * min(Decimal(1), real / esperado)
    return total, completo


def _claves_del_nit(nit: str) -> set:
    from app.models.pedido_historia import PedidoHistoria
    return {c for (c,) in db.session.query(PedidoHistoria.pedido_clave)
            .filter(PedidoHistoria.cliente_id == nit).distinct().all() if c}


def consumo_wms(nit: str, excluir_pedido: str = None, filas: list = None) -> dict:
    """Pedidos de crédito real del NIT que el WMS ya inició (tienen packing
    vivo) y cuya factura todavía no está en la cartera leída. Sin esto, dos
    pedidos seguidos del mismo cliente ven el mismo cupo libre."""
    from app.models.packing import EstadoPacking, TareaPacking
    claves = _claves_del_nit(nit)
    claves.discard(excluir_pedido)
    if not claves:
        return {'total': Decimal(0), 'pedidos': []}
    en_cartera = {(str(f.get('f353_id_tipo_docto_cruce') or '').strip(),
                   str(f.get('f353_consec_docto_cruce') or '').strip())
                  for f in (filas or [])}
    total, pedidos = Decimal(0), []
    for t in TareaPacking.query.filter(TareaPacking.pedido_clave.in_(list(claves)),
                                       TareaPacking.estado != EstadoPacking.CANCELADO).all():
        if (t.tipo_documento or '').upper() == 'TRASLADO':
            continue
        if _cp.cobro_de_tarea(t)['cobrar']:
            continue                      # contado: no consume cupo
        if t.fe_consec and ((t.fe_tipo or '').strip(), str(t.fe_consec).strip()) in en_cartera:
            continue                      # ya está en la cartera: no se cuenta dos veces
        if t.valor_factura is not None:
            v, fuente_v = _dec(t.valor_factura), 'factura'
        else:
            v, _c = valor_de_items(lineas_de_pedido(t.pedido_clave))
            fuente_v = 'estimado'
        if v is None:
            v, fuente_v = Decimal(0), 'desconocido'
        total += v
        pedidos.append({'pedido': t.numero_pedido_siesa, 'tarea_id': t.id,
                        'estado': t.estado, 'valor': float(v), 'valor_fuente': fuente_v})
    return {'total': total, 'pedidos': pedidos}


# ═════════════════════════════════════════════════════════════════════════════
# Habilitaciones que empuja el Gestor
# ═════════════════════════════════════════════════════════════════════════════

def habilitacion_de(nit: str, sucursal: str = None, hoy: date = None) -> dict:
    """Canal, acuerdo vigente y excepciones vigentes del cliente. La fila de
    la sucursal manda sobre la del NIT entero (`sucursal=''`)."""
    hoy = hoy or dia_operativo()
    nit = nit_normalizado(nit)
    suc = sucursal_normalizada(sucursal) if sucursal else ''
    filas = CarteraHabilitacion.query.filter_by(nit=nit).all()
    fila = next((f for f in filas if suc and f.sucursal == suc), None) \
        or next((f for f in filas if f.sucursal == ''), None)
    if fila is None:
        return {'existe': False, 'canal': None, 'acuerdo_vigente': False,
                'excepciones': [], 'as_of': None}
    acuerdo = bool(fila.acuerdo_vigente) and (fila.acuerdo_vence is None
                                               or fila.acuerdo_vence >= hoy)
    excepciones = []
    for x in fila.excepciones or []:
        vence = _fecha(x.get('vence')) if isinstance(x, dict) else None
        tope = _dec(x.get('tope')) if isinstance(x, dict) else None
        if vence is not None and vence >= hoy and tope is not None:
            excepciones.append({'codigo': x.get('codigo'), 'tope': float(tope),
                                'vence': vence.isoformat()})
    return {'existe': True, 'canal': (fila.canal or None), 'acuerdo_vigente': acuerdo,
            'acuerdo_vence': fila.acuerdo_vence.isoformat() if fila.acuerdo_vence else None,
            'excepciones': excepciones,
            'as_of': fila.as_of.isoformat() if fila.as_of else None,
            'edad_horas': _edad_horas(fila.as_of, datetime.utcnow()) if fila.as_of else None}


# ═════════════════════════════════════════════════════════════════════════════
# LA función
# ═════════════════════════════════════════════════════════════════════════════

def _motivo(codigo, texto, retiene=True, **cifras):
    m = {'codigo': codigo, 'texto': texto, 'retiene': retiene}
    for k, v in cifras.items():
        m[k] = float(v) if isinstance(v, Decimal) else v
    return m


def _pesos(v) -> str:
    return '$' + f'{int(round(float(v or 0))):,}'.replace(',', '.')


def evaluar(nit, sucursal, valor, cond, contexto: dict = None) -> dict:
    """**LA** respuesta a «¿este pedido puede salir a crédito?».

    `{decision: PASA|RETIENE, motivos[], vencidas_n, vencido, vencido_neto,
    saldo_a_favor, dias_max, cupo, saldo, consumo_wms, disponible, exceso,
    as_of, origen, ...}`.

    `contexto`: `pedido_clave` (se excluye de lo ya iniciado), `cartera`
    (una lectura ya hecha de `cartera_de`), `forzar` (releer Siesa).

    Contado (incluido el supuesto) PASA sin mirar la cartera: se cobra al
    entregar. Nada de acá escribe una retención — eso es de las compuertas.
    """
    contexto = contexto or {}
    nit = nit_normalizado(nit)
    sucursal = sucursal_normalizada(sucursal) if sucursal else None
    cobro = _cp.cobro_contraentrega(cond)
    valor = _dec(valor)
    tol, _ = tolerancia()
    base = {
        'nit': nit, 'sucursal': sucursal, 'valor': float(valor) if valor is not None else None,
        'cond_pago': cobro['codigo'], 'dias_credito': cobro['dias'],
        'cobro_origen': cobro['origen'], 'credito_real': not cobro['cobrar'],
        'version': VERSION_POLITICA, 'parametros': parametros(),
        'evaluado_en': datetime.utcnow().isoformat(),
    }
    if cobro['cobrar']:
        return {**base, 'decision': PASA, 'aplica': False, 'motivos': [],
                'nota': 'Contado contraentrega: se cobra al entregar, no se mira la cartera.'}

    cartera = contexto.get('cartera') or cartera_de(nit, forzar=bool(contexto.get('forzar')))
    hab = habilitacion_de(nit, sucursal)
    canal = hab.get('canal')
    motivos = []
    base.update({'aplica': True, 'origen': cartera['origen'],
                 'as_of': cartera['as_of'].isoformat() if cartera.get('as_of') else None,
                 'edad_horas': cartera.get('edad_horas'), 'error_lectura': cartera.get('error'),
                 'canal': canal, 'canal_fuente': 'GESTOR' if canal else None,
                 'habilitacion': hab})
    if cartera['origen'] == SIMULACION:
        return {**base, 'decision': PASA, 'motivos': [],
                'nota': 'Modo simulación: sin cartera que leer, no se retiene.'}

    if hab.get('acuerdo_vigente'):
        motivos.append(_motivo(ACUERDO_VIGENTE,
                               'Tiene un acuerdo de pago vigente con cartera: el pedido '
                               'nuevo sale solo de contado.',
                               acuerdo_vence=hab.get('acuerdo_vence')))

    if cartera['origen'] == SIN_DATO_ORIGEN:
        motivos.append(_motivo(SIN_DATO,
                               'Sin dato de cartera: Siesa no respondió y no hay foto de '
                               'menos de 24 h. No se afirma que el cliente esté al día.',
                               error=cartera.get('error')))
        return _cerrar(base, motivos, contexto)

    ma = maestro(cartera['clientes'])
    filas = clasificar_filas(cartera['filas'], ma['sucursales'])
    vencidas = [f for f in filas if f['vencida']]
    vencido = sum((f['cuenta'] for f in vencidas), Decimal(0))
    a_favor = sum((f.get('a_favor', Decimal(0)) for f in filas if f['clase'] == A_FAVOR),
                  Decimal(0))
    saldo = sum((f['cuenta'] for f in filas), Decimal(0)) - a_favor
    consumo = consumo_wms(nit, contexto.get('pedido_clave'), cartera['filas'])
    dias_max = max((f['dias_vencida'] for f in vencidas), default=None)
    base.update({
        'maestro': {k: v for k, v in ma.items() if k != 'cupo'},
        'vencidas_n': len(vencidas), 'vencido': float(vencido),
        'vencido_neto': float(max(Decimal(0), vencido - a_favor)),
        'saldo_a_favor': float(a_favor), 'dias_max': dias_max,
        'saldo': float(saldo), 'consumo_wms': float(consumo['total']),
        'pedidos_wms': consumo['pedidos'],
        'vencidas': [_fila_publica(f) for f in vencidas],
        'filas': [_fila_publica(f) for f in filas if f['clase'] != SALDADA],
    })

    if vencidas:
        if canal == CANAL_EXENTO_DE_MORA:
            motivos.append(_motivo(
                MORA_EXENTA, f'{len(vencidas)} factura(s) vencida(s) por {_pesos(vencido)}; '
                'canal INSTITUCIONAL: no se retiene por mora (el cupo sí aplica).',
                retiene=False, vencidas_n=len(vencidas), vencido=vencido, dias_max=dias_max))
        else:
            motivos.append(_motivo(
                MORA, f'{len(vencidas)} factura(s) vencida(s) por {_pesos(vencido)} '
                f'(la más vieja, {dias_max} días)'
                + ('' if canal else ' · canal desconocido: no se exime'),
                vencidas_n=len(vencidas), vencido=vencido, dias_max=dias_max))

    cupo = ma['cupo']
    base['cupo'] = float(cupo) if cupo is not None else None
    if not ma['encontrado']:
        motivos.append(_motivo(SIN_CUPO, 'El cliente no está en el maestro de Siesa de esta '
                                         'compañía: no tiene cupo asignado.', cupo=None))
    elif cupo is None or cupo <= 0:
        motivos.append(_motivo(SIN_CUPO, 'Crédito real sin cupo asignado en Siesa.',
                               cupo=cupo or Decimal(0)))
    elif valor is None:
        motivos.append(_motivo(VALOR_DESCONOCIDO, 'No se pudo calcular el valor del pedido: '
                                                  'no se puede saber si cabe en el cupo.'))
    else:
        disponible = cupo - saldo - consumo['total'] - valor
        exceso = max(Decimal(0), -disponible)
        base.update({'disponible': float(disponible), 'exceso': float(exceso)})
        if disponible < 0:
            motivos.append(_motivo(
                CUPO_EXCEDIDO, f'Supera el cupo por {_pesos(exceso)}: cupo {_pesos(cupo)}, '
                f'debe {_pesos(saldo)}, en curso en el WMS {_pesos(consumo["total"])}, '
                f'este pedido {_pesos(valor)}.',
                cupo=cupo, saldo=saldo, consumo_wms=consumo['total'], valor=valor,
                exceso=exceso))
    if ma['duplicado_misma_cia'] or ma['otras_companias']:
        otras = ', '.join(f'cía {o["cia"]} suc {o["sucursal"]} cupo {_pesos(o["cupo"])}'
                          for o in ma['otras_companias'])
        motivos.append(_motivo(
            MAESTRO_DUPLICADO,
            'El maestro de clientes trae más de una fila para este NIT'
            + (f' (otra compañía: {otras})' if otras else '')
            + (f' (duplicada en sucursal {", ".join(ma["duplicado_misma_cia"])}: se usó la '
               'más reciente)' if ma['duplicado_misma_cia'] else '')
            + f'. Se usa la de la compañía {id_cia()}.',
            retiene=False, otras_companias=ma['otras_companias'],
            duplicado_misma_cia=ma['duplicado_misma_cia']))
    return _cerrar(base, motivos, contexto, hab=hab, valor=valor)


def _cerrar(base, motivos, contexto, hab=None, valor=None):
    retiene = [m for m in motivos if m['retiene']]
    decision = RETIENE if retiene else PASA
    cubierta = None
    if retiene and hab and not any(m['codigo'] == ACUERDO_VIGENTE for m in retiene):
        for x in hab.get('excepciones') or []:
            if valor is not None and Decimal(str(x['tope'])) >= valor:
                cubierta = x
                break
    if cubierta is not None:
        decision = PASA
    return {**base, 'decision': decision, 'motivos': motivos,
            'excepcion_gestor': cubierta,
            'resumen': '; '.join(m['texto'] for m in retiene) or None}


def _fila_publica(f) -> dict:
    return {'documento': f['documento'], 'co': f.get('co'), 'sucursal': f.get('sucursal'),
            'fecha': f['fecha'].isoformat() if f.get('fecha') else None,
            'vence': f['vence'].isoformat() if f.get('vence') else None,
            'vence_siesa': f['vence_siesa'].isoformat() if f.get('vence_siesa') else None,
            'dias': f.get('dias_vencida'), 'saldo': float(f['saldo']),
            'cuenta': float(f['cuenta']), 'clase': f['clase'], 'wms': f['wms'],
            'pedido': f.get('pedido'), 'restado': float(f.get('restado') or 0)}


# ═════════════════════════════════════════════════════════════════════════════
# Retenciones
# ═════════════════════════════════════════════════════════════════════════════

def retencion_viva(pedido_clave: str):
    return RetencionCartera.query.filter_by(
        pedido_clave=pedido_clave, estado=EstadoRetencion.RETENIDO).first()


def resolucion_vigente(pedido_clave: str, valor=None, hoy: date = None):
    """La autorización o conversión que deja salir a este pedido, o `None`.

    AUTORIZADO cubre hasta su `tope_valor` y hasta su `vence_en`: si el valor
    empacado supera el tope, vuelve a retener (decisión del Gestor).
    CONVERTIDO_CONTADO no vence: el pedido sale en contado.
    """
    hoy = hoy or dia_operativo()
    valor = _dec(valor)
    for r in (RetencionCartera.query
              .filter(RetencionCartera.pedido_clave == pedido_clave,
                      RetencionCartera.estado.in_((EstadoRetencion.AUTORIZADO,
                                                   EstadoRetencion.CONVERTIDO_CONTADO)))
              .order_by(RetencionCartera.resuelta_en.desc()).all()):
        if r.estado == EstadoRetencion.CONVERTIDO_CONTADO:
            return r
        if r.vence_en is not None and r.vence_en < hoy:
            continue
        if r.tope_valor is not None and valor is not None and valor > _dec(r.tope_valor):
            continue
        return r
    return None


def _json(evaluacion: dict) -> dict:
    return json.loads(json.dumps(evaluacion, default=str))


def _registrar_retencion(ev: dict, compuerta: str, pedido: dict, tarea=None,
                         iniciado_por_id=None):
    """Crea (o refresca) la retención viva del pedido. Sin commit."""
    from app.services.bitacora import registrar_accion
    viva = retencion_viva(pedido['pedido_clave'])
    motivos = [m for m in ev['motivos']]
    ahora = datetime.utcnow()
    if viva is not None:
        viva.motivos, viva.evaluacion = motivos, _json(ev)
        viva.valor = ev.get('valor')
        viva.as_of = _as_of(ev)
        viva.origen_dato = ev.get('origen')
        viva.actualizada_en = ahora
        if tarea is not None and viva.tarea_packing_id is None:
            viva.tarea_packing_id = tarea.id
        if viva.compuerta != compuerta:
            viva.compuerta = compuerta
        return viva
    iniciador = _usuario(iniciado_por_id)
    r = RetencionCartera(
        creada_en=ahora, actualizada_en=ahora,
        pedido_clave=pedido['pedido_clave'], numero_pedido=pedido.get('numero_pedido'),
        nit=ev['nit'], sucursal=ev.get('sucursal'), cliente=pedido.get('cliente'),
        vendedor_id=pedido.get('vendedor_id'),
        tarea_packing_id=getattr(tarea, 'id', None),
        almacen_id=pedido.get('almacen_id') or getattr(tarea, 'almacen_id', None),
        compuerta=compuerta, cond_pago=ev.get('cond_pago'), dias_credito=ev.get('dias_credito'),
        valor=ev.get('valor'), motivos=motivos, evaluacion=_json(ev),
        as_of=_as_of(ev), origen_dato=ev.get('origen'),
        estado=EstadoRetencion.RETENIDO,
        iniciado_por_id=iniciado_por_id,
        iniciado_por=(f'{iniciador.nombre} <{iniciador.email}>' if iniciador else None),
        siesa_usuario_creacion=(pedido.get('contexto_siesa') or {}).get('usuario_creacion'),
        contexto_siesa=pedido.get('contexto_siesa'),
        reevaluaciones=0)
    db.session.add(r)
    db.session.flush()
    registrar_accion('BLOQUEAR', r, usuario_id=iniciado_por_id,
                     entidad_codigo=pedido.get('numero_pedido'),
                     motivo=f'Retenido por cartera ({compuerta}): {ev.get("resumen")}',
                     despues={'motivos': [m['codigo'] for m in motivos if m['retiene']],
                              'valor': ev.get('valor'), 'nit': ev['nit']},
                     almacen_id=r.almacen_id,
                     origen=None if _en_request() else 'cartera_service')
    return r


def _en_request() -> bool:
    try:
        from flask import has_request_context
        return has_request_context()
    except Exception:  # noqa: BLE001
        return False


def _as_of(ev):
    s = ev.get('as_of')
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _usuario(uid):
    if not uid:
        return None
    from app.models.usuario import Usuario
    return db.session.get(Usuario, uid)


# ═════════════════════════════════════════════════════════════════════════════
# Datos del pedido
# ═════════════════════════════════════════════════════════════════════════════

_CAMPOS_CONTEXTO = ('f430_ind_retenido_margen', 'f430_ind_retenido_cupo',
                    'f430_ind_retenido_mora', 'f430_ind_retenido_bloqueo',
                    'f430_ind_retenido_anticipo', 'f430_usuario_retenido',
                    'f430_fecha_ts_retenido', 'f430_usuario_aprobacion',
                    'f430_usuario_aprobacion_cart', 'f430_fecha_ts_aprobacion_cart',
                    'f430_usuario_creacion')


def contexto_siesa(filas: list) -> dict:
    """Lo que Siesa hizo con el pedido: sus propias retenciones y quién las
    liberó. Contexto para el Gestor, no decide nada."""
    if not filas:
        return {}
    f = filas[0]
    out = {k.replace('f430_', ''): (str(f.get(k)) if f.get(k) is not None else None)
           for k in _CAMPOS_CONTEXTO}
    return out


def datos_pedido(pedido_clave, numero_pedido=None, tipo=None, consec=None, co=None,
                 con_siesa: bool = True) -> dict:
    """NIT, sucursal, condición, cliente, vendedor y líneas del pedido.

    La historia del sync primero (sin red). Siesa (`API_v2_Ventas_Pedidos`)
    solo si hace falta y se puede: da la sucursal, el contexto de retención
    de Siesa y sirve de respaldo si la historia no tiene el pedido.
    """
    lineas = lineas_de_pedido(pedido_clave)
    out = {'pedido_clave': pedido_clave, 'numero_pedido': numero_pedido,
           'lineas': lineas, 'fuente_lineas': 'HISTORIA' if lineas else None,
           'sucursal': None, 'contexto_siesa': {}}
    if lineas:
        l0 = lineas[0]
        out.update(nit=nit_normalizado(l0['cliente_id']) or None, cliente=l0['cliente'],
                   vendedor_id=l0['vendedor_id'], cond_pago=l0['cond_pago'])
    else:
        out.update(nit=None, cliente=None, vendedor_id=None, cond_pago=None)
    if not con_siesa or fuente().simulada():
        return out
    if not (co and tipo and consec):
        partes = (pedido_clave or '').split('-')
        if len(partes) == 3:
            co, tipo, consec = partes
    try:
        filas = fuente().pedido(co, tipo, consec) if (co and tipo and consec) else []
    except Exception as e:  # noqa: BLE001 — el contexto es opcional
        logger.info('[CARTERA] pedido %s: Siesa no dio el detalle (%s)', pedido_clave, e)
        filas = []
    if filas:
        f0 = filas[0]
        out['sucursal'] = sucursal_normalizada(f0.get('f430_id_sucursal_pedido_fact'))
        out['contexto_siesa'] = contexto_siesa(filas)
        if not lineas:
            out['lineas'] = lineas_de_siesa(filas)
            out['fuente_lineas'] = 'SIESA'
            out.update(nit=nit_normalizado(f0.get('f200_id_pedido_fact')) or None,
                       cliente=f0.get('f200_razon_social_pedido_fact'),
                       vendedor_id=str(f0.get('f200_id_pedido_vend') or '').strip() or None,
                       cond_pago=str(f0.get('f430_id_cond_pago') or '').strip() or None)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Lock por cliente
# ═════════════════════════════════════════════════════════════════════════════

def clave_lock_nit(nit: str) -> int:
    from app.utils.lock import RANGO_CARTERA_NIT, clave_en_rango
    return clave_en_rango(RANGO_CARTERA_NIT,
                          zlib.crc32(nit_normalizado(nit).encode()) % RANGO_CARTERA_NIT[1])


def tomar_lock_nit(nit: str):
    """Lock de sesión (conexión dedicada) por NIT, sin esperar. Lo suelta el
    teardown de la petición (`soltar_locks_de_la_peticion`) o quien lo tomó."""
    from app.utils.lock import tomar_lock_de_sesion
    lock = tomar_lock_de_sesion(clave_lock_nit(nit), f'cartera_nit_{nit}')
    if lock.tomado and _en_request():
        from flask import g
        g.setdefault('_cartera_locks', []).append(lock)
    return lock


def soltar_locks_de_la_peticion(_exc=None):
    try:
        from flask import g
        locks = g.pop('_cartera_locks', [])
    except Exception:  # noqa: BLE001
        return
    for lock in locks:
        lock.liberar()


# ═════════════════════════════════════════════════════════════════════════════
# Compuertas
# ═════════════════════════════════════════════════════════════════════════════

class Paso:
    """Lo que una compuerta decidió. `pasa` es lo único que el llamador mira
    para seguir; el resto es para mostrar."""

    def __init__(self, pasa: bool, decision: str, evaluacion: dict = None,
                 retencion=None, mensaje: str = None, habria_retenido: bool = False):
        self.pasa = pasa
        self.decision = decision
        self.evaluacion = evaluacion or {}
        self.retencion = retencion
        self.mensaje = mensaje
        self.habria_retenido = habria_retenido

    def cuerpo(self) -> dict:
        return {'error': self.mensaje, 'retenido_por_cartera': True,
                'retencion': retencion_publica(self.retencion) if self.retencion else None}


def _mensaje(ev: dict, retencion) -> str:
    return (f'Retenido por cartera: {ev.get("resumen") or "sin detalle"}. '
            f'Lo libera un usuario de cartera (Gestor de Cartera) o el pago del cliente'
            + (f' — retención #{retencion.id}' if retencion is not None else '') + '.')


def _aplicar_modo(ev: dict, pedido: dict, compuerta: str, tarea=None,
                  iniciado_por_id=None) -> 'Paso':
    if ev['decision'] == PASA:
        dec = 'NO_APLICA' if not ev.get('aplica') else 'PASA'
        return Paso(True, dec, ev)
    m, _ = modo()
    if m == 'INFORMA':
        logger.warning('[CARTERA] %s %s habría quedado retenido (modo INFORMA): %s',
                       compuerta, pedido.get('numero_pedido'), ev.get('resumen'))
        return Paso(True, 'PASA', ev, habria_retenido=True)
    r = _registrar_retencion(ev, compuerta, pedido, tarea, iniciado_por_id)
    return Paso(False, RETIENE, ev, r, _mensaje(ev, r))


def compuerta_inicio(numero_pedido, tipo, consec, co=None, items=None, almacen_id=None,
                     usuario_id=None) -> Paso:
    """G1 — al tocar «Aprobar», antes de crear el picking.

    Contado → pasa sin red. Crédito real → toma el lock del NIT (lo suelta el
    teardown de la petición, después de crear el packing: así el pedido
    siguiente del mismo cliente ya lo ve en `consumo_wms`) y evalúa con el
    valor pendiente. Si retiene, deja la retención comiteada.
    """
    from app.services.cadena_pedido import clave_de_documento
    clave = clave_de_documento(numero_pedido=numero_pedido, tipo=tipo, consec=consec,
                               almacen_id=almacen_id, co=co)
    if not clave:
        return Paso(True, 'NO_APLICA', {'nota': 'pedido sin clave: no se puede evaluar'})
    pedido = datos_pedido(clave, numero_pedido, con_siesa=False)
    cobro = _cp.cobro_contraentrega(pedido.get('cond_pago'))
    if cobro['cobrar']:
        return Paso(True, 'NO_APLICA', {'cobro': cobro})
    pedido = datos_pedido(clave, numero_pedido, tipo, consec, co)
    pedido['almacen_id'] = almacen_id
    nit = pedido.get('nit')
    lock = tomar_lock_nit(nit or clave)
    if not lock.tomado:
        raise DespachoEnCurso(f'Otro despacho a crédito del cliente {nit} se está evaluando '
                              'en este momento. Reintentá en unos segundos.')
    if not _en_request():
        lock.liberar()
    valor, completo = valor_de_items(pedido['lineas'],
                                     [{'item_codigo': i.get('item_codigo'),
                                       'cantidad_pendiente': i.get('cantidad_pendiente')}
                                      for i in (items or [])] if items else None)
    res = resolucion_vigente(clave, valor)
    if res is not None:
        return Paso(True, 'CONTADO' if res.estado == EstadoRetencion.CONVERTIDO_CONTADO
                    else 'AUTORIZADO', {'resolucion_id': res.id})
    ev = evaluar(nit, pedido.get('sucursal'), valor if completo else None,
                 pedido.get('cond_pago'), {'pedido_clave': clave})
    paso = _aplicar_modo(ev, pedido, Compuerta.INICIO, iniciado_por_id=usuario_id)
    if not paso.pasa:
        db.session.commit()
    return paso


def anotar_inicio(packing, usuario_id):
    """Después de crear el packing en G1: quién lo inició (sin commit)."""
    if packing is not None and usuario_id and not packing.despacho_iniciado_por_id:
        packing.despacho_iniciado_por_id = usuario_id


def _marcar(tarea, decision):
    tarea.cartera_decision = decision
    tarea.cartera_decidido_en = datetime.utcnow()


def _convertir_en_tarea(tarea):
    """La FE de un pedido convertido a contado sale en la condición de ruta
    (C02). Se anota como la condición de la FE: el snapshot queda contado,
    `difiere_del_pedido` lo declara, y el conductor lo cobra."""
    from app.services.connekta_gateway import connekta
    ruta = (connekta.cond_pago_ruta or '').strip()
    if ruta:
        _cp.anotar_en_tarea(tarea, cond_fe=ruta)


def compuerta_cierre(tarea, usuario_id=None) -> Paso:
    """G2 — al cerrar la caja, antes del 244328. Con el valor EMPACADO.

    Si la tarea ya tiene decisión (un reintento del cierre), no se vuelve a
    preguntar. No escribe la decisión si retiene: la tarea sigue VERIFICADA.
    El que llama hace el commit.
    """
    if (tarea.tipo_documento or '').upper() == 'TRASLADO':
        return Paso(True, 'NO_APLICA')
    if tarea.cartera_decision:
        return Paso(True, tarea.cartera_decision)
    clave = tarea.pedido_clave
    valor, completo = valor_empacado(tarea)
    res = resolucion_vigente(clave, valor) if clave else None
    if res is not None and res.estado == EstadoRetencion.CONVERTIDO_CONTADO:
        _convertir_en_tarea(tarea)
        _marcar(tarea, 'CONTADO')
        return Paso(True, 'CONTADO', {'resolucion_id': res.id})
    cobro = _cp.cobro_de_tarea(tarea)
    if cobro['cobrar'] or not clave:
        # Contado, o supuesto: sale. Si la cabecera de Siesa dice crédito, la
        # emisión lo vuelve a mirar (`compuerta_emision`).
        _marcar(tarea, 'NO_APLICA')
        return Paso(True, 'NO_APLICA', {'cobro': cobro})
    if res is not None:
        _marcar(tarea, 'AUTORIZADO')
        return Paso(True, 'AUTORIZADO', {'resolucion_id': res.id})
    pedido = datos_pedido(clave, tarea.numero_pedido_siesa,
                          tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa)
    pedido['almacen_id'] = tarea.almacen_id
    if valor is None or not completo:
        v_items, c_items = valor_empacado(tarea, pedido['lineas'])
        valor, completo = (v_items, c_items) if v_items is not None else (valor, completo)
    ev = evaluar(pedido.get('nit'), pedido.get('sucursal'), valor if completo else None,
                 _cp.codigo_vigente(tarea), {'pedido_clave': clave})
    paso = _aplicar_modo(ev, pedido, Compuerta.CIERRE, tarea,
                         tarea.despacho_iniciado_por_id or usuario_id)
    if paso.pasa:
        _marcar(tarea, paso.decision if paso.decision in ('PASA', 'NO_APLICA') else 'PASA')
    return paso


def cabecera_para_factura(tarea, cabecera: dict) -> dict:
    """La cabecera con la condición que la FE tiene que llevar. Un pedido
    convertido a contado por cartera sale en la condición de ruta (C02): el
    gateway la manda en `F461_ID_COND_PAGO` y la vence a 15 días."""
    if not cabecera or tarea is None or not tarea.pedido_clave:
        return cabecera
    res = resolucion_vigente(tarea.pedido_clave)
    if res is None or res.estado != EstadoRetencion.CONVERTIDO_CONTADO:
        return cabecera
    from app.services.connekta_gateway import connekta
    ruta = (connekta.cond_pago_ruta or '').strip()
    if not ruta:
        raise ValueError('El pedido fue convertido a contado por cartera, pero '
                         'SIESA_COND_PAGO_RUTA no está configurada: no se emite una FE '
                         'a crédito que cartera no autorizó.')
    return {**cabecera, 'f430_id_cond_pago': ruta}


def compuerta_emision(tarea, cabecera: dict) -> dict:
    """La última puerta, dentro de la emisión (244328/142945/142943), con la
    condición que la FE va a llevar DE VERDAD (la cabecera de Siesa).

    · Contado (después de una conversión, si la hay) → sigue.
    · Crédito real con decisión PASA/AUTORIZADO/CONTADO → sigue sin red.
    · Crédito real sin decisión, o con NO_APLICA (era supuesto y la cabecera
      dice crédito) → evalúa ahora. Retiene → `RetenidoPorCartera`.

    Devuelve la cabecera a usar (con la condición de una conversión).
    """
    cab = cabecera_para_factura(tarea, cabecera)
    if (tarea.tipo_documento or '').upper() == 'TRASLADO' or not cab:
        return cab
    cond = (cab.get('f430_id_cond_pago') or '').strip() or None
    if _cp.cobro_contraentrega(cond)['cobrar']:
        return cab
    if tarea.cartera_decision in ('PASA', 'AUTORIZADO', 'CONTADO'):
        return cab
    clave = tarea.pedido_clave
    valor, completo = valor_empacado(tarea)
    res = resolucion_vigente(clave, valor) if clave else None
    if res is not None:
        _marcar(tarea, 'AUTORIZADO' if res.estado == EstadoRetencion.AUTORIZADO else 'CONTADO')
        db.session.commit()
        return cab
    viva = retencion_viva(clave) if clave else None
    if viva is not None:
        raise RetenidoPorCartera(_mensaje(viva.evaluacion or {}, viva), viva.id)
    nit = nit_normalizado(cab.get('f200_id_pedido_fact'))
    sucursal = cab.get('f430_id_sucursal_pedido_fact') or cab.get('f461_id_sucursal_pedido_rem')
    ev = evaluar(nit, sucursal, valor if completo else None, cond, {'pedido_clave': clave})
    pedido = {'pedido_clave': clave or f'TAREA-{tarea.id}',
              'numero_pedido': tarea.numero_pedido_siesa, 'cliente': tarea.cliente,
              'almacen_id': tarea.almacen_id, 'contexto_siesa': contexto_siesa([cab])}
    paso = _aplicar_modo(ev, pedido, Compuerta.EMISION, tarea, tarea.despacho_iniciado_por_id)
    if paso.pasa:
        _marcar(tarea, 'PASA')
        db.session.commit()
        return cab
    db.session.commit()
    raise RetenidoPorCartera(paso.mensaje, paso.retencion.id if paso.retencion else None)


# ═════════════════════════════════════════════════════════════════════════════
# Resolver una retención
# ═════════════════════════════════════════════════════════════════════════════

class AccionRechazada(ValueError):
    """La acción no se puede hacer sobre esta retención (409)."""

    def __init__(self, mensaje, estado=None):
        super().__init__(mensaje)
        self.estado = estado


def _texto_usuario(usuario) -> str:
    if isinstance(usuario, dict):
        partes = [usuario.get('nombre') or usuario.get('username') or '']
        if usuario.get('email'):
            partes.append(f'<{usuario["email"]}>')
        if usuario.get('rol'):
            partes.append(f'({usuario["rol"]})')
        return ' '.join(p for p in partes if p).strip()[:160]
    return str(usuario or '').strip()[:160]


def _identidades(usuario) -> set:
    """Lo que identifica a una persona, normalizado, para compararla contra
    quien inició el pedido."""
    out = set()
    if isinstance(usuario, dict):
        for k in ('username', 'email', 'nombre'):
            v = str(usuario.get(k) or '').strip().lower()
            if v:
                out.add(v)
                if '@' in v:
                    out.add(v.split('@', 1)[0])
    elif usuario:
        v = str(usuario).strip().lower()
        out.add(v)
    return out


def iniciadores(r) -> set:
    out = set()
    u = _usuario(r.iniciado_por_id)
    if u is not None:
        for v in (u.email, u.nombre):
            v = str(v or '').strip().lower()
            if v:
                out.add(v)
                if '@' in v:
                    out.add(v.split('@', 1)[0])
    if r.siesa_usuario_creacion:
        out.add(str(r.siesa_usuario_creacion).strip().lower())
    return out


def _exigir_viva(r):
    if r.estado != EstadoRetencion.RETENIDO:
        raise AccionRechazada(f'La retención #{r.id} ya está {r.estado}', r.estado)


def _adelantar_emision(r):
    """Si la retención frenó una emisión en el DLQ, el job no espera sus 30
    minutos: se adelanta."""
    if r.compuerta != Compuerta.EMISION or not r.tarea_packing_id:
        return
    from app.models.siesa_job import EstadoSiesaJob, SiesaJob
    job = (SiesaJob.query.filter(SiesaJob.tipo == 'DESPACHO_F470',
                                 SiesaJob.referencia_tipo == 'TareaPacking',
                                 SiesaJob.referencia_id == r.tarea_packing_id,
                                 SiesaJob.estado == EstadoSiesaJob.PENDIENTE).first())
    if job is not None:
        job.proximo_intento = datetime.utcnow()


def _disparar_dlq_si_emision(r):
    if r.compuerta == Compuerta.EMISION:
        try:
            from app.services.siesa_job_service import disparar_dlq_inmediato
            disparar_dlq_inmediato()
        except Exception as e:  # noqa: BLE001
            logger.warning('[CARTERA] no se pudo disparar el DLQ: %s', e)


def autorizar(retencion_id: int, usuario, motivo: str, tope_valor, vence_en=None,
              codigo_excepcion: str = None, origen: str = 'GESTOR',
              usuario_id: int = None) -> RetencionCartera:
    """Deja salir el pedido a crédito igual. Motivo y tope obligatorios;
    vigencia ≤ 30 días; quien inició el pedido no puede autorizarlo."""
    from app.services.bitacora import motivo_obligatorio, registrar_accion
    r = db.session.get(RetencionCartera, retencion_id)
    if r is None:
        raise LookupError(f'Retención {retencion_id} no encontrada')
    _exigir_viva(r)
    texto = motivo_obligatorio(motivo, 'la autorización de cartera')
    quien = _texto_usuario(usuario)
    if not quien:
        raise ValueError('La autorización exige el nombre de quien autoriza')
    if origen == 'WMS' and usuario_id and usuario_id == r.iniciado_por_id:
        raise AccionRechazada('Quien inició el pedido no puede autorizar su propia '
                              'excepción de cartera', r.estado)
    if _identidades(usuario) & iniciadores(r):
        raise AccionRechazada('Quien inició el pedido no puede autorizar su propia '
                              'excepción de cartera', r.estado)
    tope = _dec(tope_valor)
    if tope is None or tope <= 0:
        raise ValueError('tope_valor es obligatorio y mayor que cero')
    hoy = dia_operativo()
    vence = _fecha(vence_en) if vence_en else hoy + timedelta(days=VIGENCIA_MAX_AUTORIZACION)
    if vence is None:
        raise ValueError('vence_en no es una fecha (YYYY-MM-DD)')
    if vence < hoy or vence > hoy + timedelta(days=VIGENCIA_MAX_AUTORIZACION):
        raise ValueError(f'vence_en debe estar entre hoy y {VIGENCIA_MAX_AUTORIZACION} días')
    if codigo_excepcion and codigo_excepcion not in CODIGOS_EXCEPCION:
        raise ValueError(f'codigo_excepcion debe ser uno de {", ".join(CODIGOS_EXCEPCION)}')
    antes = {'estado': r.estado}
    ahora = datetime.utcnow()
    r.estado = EstadoRetencion.AUTORIZADO
    r.resuelta_en = r.actualizada_en = ahora
    r.resuelta_por, r.resuelta_por_id, r.resuelta_origen = quien, usuario_id, origen
    r.motivo_resolucion = texto
    r.tope_valor, r.vence_en, r.codigo_excepcion = tope, vence, codigo_excepcion
    registrar_accion('FORZAR', r, usuario_id=usuario_id, motivo=texto,
                     entidad_codigo=r.numero_pedido, antes=antes,
                     despues={'estado': r.estado, 'autorizado_por': quien,
                              'origen': origen, 'tope_valor': float(tope),
                              'vence_en': vence.isoformat(),
                              'codigo_excepcion': codigo_excepcion,
                              'usuario': usuario if isinstance(usuario, dict) else None},
                     almacen_id=r.almacen_id,
                     origen='gestor_cartera' if origen == 'GESTOR' else None)
    _adelantar_emision(r)
    db.session.commit()
    _disparar_dlq_si_emision(r)
    return r


def convertir_a_contado(retencion_id: int, usuario, motivo: str, origen: str = 'GESTOR',
                        usuario_id: int = None) -> RetencionCartera:
    """El pedido sale, pero de contado contraentrega: la FE en la condición de
    ruta (C02) y el conductor la cobra."""
    from app.models.packing import TareaPacking
    from app.services.bitacora import motivo_obligatorio, registrar_accion
    from app.services.connekta_gateway import connekta
    r = db.session.get(RetencionCartera, retencion_id)
    if r is None:
        raise LookupError(f'Retención {retencion_id} no encontrada')
    _exigir_viva(r)
    texto = motivo_obligatorio(motivo, 'la conversión a contado')
    quien = _texto_usuario(usuario)
    if not quien:
        raise ValueError('La conversión exige el nombre de quien la decide')
    ruta = (connekta.cond_pago_ruta or '').strip()
    if not ruta or not _cp.cobro_contraentrega(ruta)['cobrar']:
        raise AccionRechazada('SIESA_COND_PAGO_RUTA no está configurada (o no es de contado): '
                              'no hay condición de contado con la cual emitir', r.estado)
    antes = {'estado': r.estado}
    ahora = datetime.utcnow()
    r.estado = EstadoRetencion.CONVERTIDO_CONTADO
    r.resuelta_en = r.actualizada_en = ahora
    r.resuelta_por, r.resuelta_por_id, r.resuelta_origen = quien, usuario_id, origen
    r.motivo_resolucion = texto
    tarea = db.session.get(TareaPacking, r.tarea_packing_id) if r.tarea_packing_id else None
    if tarea is not None and not tarea.siesa_triggered:
        _convertir_en_tarea(tarea)
    registrar_accion('EDITAR', r, usuario_id=usuario_id, motivo=texto,
                     entidad_codigo=r.numero_pedido, antes=antes,
                     despues={'estado': r.estado, 'convertido_por': quien, 'origen': origen,
                              'cond_pago_fe': ruta, 'cobro_contraentrega': True,
                              'clasif_origen': 'CARTERA',
                              'usuario': usuario if isinstance(usuario, dict) else None},
                     almacen_id=r.almacen_id,
                     origen='gestor_cartera' if origen == 'GESTOR' else None)
    _adelantar_emision(r)
    db.session.commit()
    _disparar_dlq_si_emision(r)
    return r


def cancelar(r, motivo: str, usuario_id=None, origen: str = 'SISTEMA'):
    """El pedido o su packing dejaron de existir. Sin commit."""
    from app.services.bitacora import registrar_accion
    antes = {'estado': r.estado}
    r.estado = EstadoRetencion.CANCELADO
    r.resuelta_en = r.actualizada_en = datetime.utcnow()
    r.resuelta_origen = origen
    r.resuelta_por_id = usuario_id
    r.motivo_resolucion = motivo
    registrar_accion('CANCELAR', r, usuario_id=usuario_id, motivo=motivo,
                     entidad_codigo=r.numero_pedido, antes=antes,
                     despues={'estado': r.estado}, almacen_id=r.almacen_id,
                     origen=None if _en_request() else 'cartera_barrido')


def reevaluar(retencion_id: int, usuario=None, origen: str = 'WMS',
              usuario_id: int = None, cartera: dict = None) -> RetencionCartera:
    """Vuelve a mirar la cartera (releyendo Siesa). Si ya no retiene, queda
    LIBERADO_PAGO y el pedido puede volver a intentarse. Commit."""
    from app.models.packing import TareaPacking
    r = db.session.get(RetencionCartera, retencion_id)
    if r is None:
        raise LookupError(f'Retención {retencion_id} no encontrada')
    _exigir_viva(r)
    tarea = db.session.get(TareaPacking, r.tarea_packing_id) if r.tarea_packing_id else None
    valor = r.valor
    if tarea is not None and r.compuerta != Compuerta.INICIO:
        v, completo = valor_empacado(tarea)
        valor = v if completo else valor
    ctx = {'pedido_clave': r.pedido_clave, 'forzar': cartera is None}
    if cartera is not None:
        ctx['cartera'] = cartera
    ev = evaluar(r.nit, r.sucursal, valor, r.cond_pago, ctx)
    ahora = datetime.utcnow()
    r.reevaluada_en = r.actualizada_en = ahora
    r.reevaluaciones = (r.reevaluaciones or 0) + 1
    r.evaluacion, r.motivos = _json(ev), ev['motivos']
    r.as_of, r.origen_dato = _as_of(ev), ev.get('origen')
    if ev['decision'] == PASA:
        r.estado = EstadoRetencion.LIBERADO_PAGO
        r.resuelta_en = ahora
        r.resuelta_origen = origen
        r.resuelta_por = _texto_usuario(usuario) or None
        r.resuelta_por_id = usuario_id
        r.motivo_resolucion = ('Re-evaluada: ' + (
            f'excepción vigente del Gestor {ev["excepcion_gestor"]["codigo"]}'
            if ev.get('excepcion_gestor') else 'ya no hay mora ni exceso de cupo'))
        _adelantar_emision(r)
    db.session.commit()
    if r.estado == EstadoRetencion.LIBERADO_PAGO:
        _disparar_dlq_si_emision(r)
    return r


# ═════════════════════════════════════════════════════════════════════════════
# Barrido (cron cada 30 min, 7:00–19:30 Bogotá)
# ═════════════════════════════════════════════════════════════════════════════

VENTANA_BARRIDO = (time(7, 0), time(19, 30))


def barrido(ahora_bog: datetime = None) -> dict:
    """Re-evalúa las retenciones vivas (una lectura por NIT) y cancela las de
    pedidos que ya no existen. Solo NIT retenidos: no recorre la cartera
    entera."""
    from app.models.packing import EstadoPacking, TareaPacking
    from app.models.pedido_historia import MotivoSalidaPedido, PedidoHistoria
    from app.utils.lock import LOCK_CARTERA_BARRIDO, advisory_lock
    ahora_bog = ahora_bog or ahora_bogota()
    if not (VENTANA_BARRIDO[0] <= ahora_bog.time() < VENTANA_BARRIDO[1]):
        return {'omitido': 'fuera de la ventana 7:00–19:30 Bogotá'}
    res = {'reevaluadas': 0, 'liberadas': 0, 'canceladas': 0, 'errores': 0, 'alertas': []}
    with advisory_lock(LOCK_CARTERA_BARRIDO, 'cartera_barrido') as tomado:
        if not tomado:
            return {'omitido': 'otro worker ya corre el barrido'}
        vivas = RetencionCartera.query.filter_by(estado=EstadoRetencion.RETENIDO).all()
        por_nit = {}
        for r in vivas:
            tarea = db.session.get(TareaPacking, r.tarea_packing_id) if r.tarea_packing_id else None
            anulado = (PedidoHistoria.query
                       .filter(PedidoHistoria.pedido_clave == r.pedido_clave,
                               PedidoHistoria.motivo_salida == MotivoSalidaPedido.ANULADO)
                       .first())
            if (tarea is not None and tarea.estado == EstadoPacking.CANCELADO) or anulado:
                cancelar(r, 'El pedido fue anulado en Siesa o su packing se canceló',
                         origen='BARRIDO')
                res['canceladas'] += 1
                continue
            por_nit.setdefault(r.nit, []).append(r)
        db.session.commit()
        for nit, rs in por_nit.items():
            try:
                cartera = cartera_de(nit, forzar=True)
                for r in rs:
                    r2 = reevaluar(r.id, origen='BARRIDO', cartera=cartera)
                    res['reevaluadas'] += 1
                    if r2.estado == EstadoRetencion.LIBERADO_PAGO:
                        res['liberadas'] += 1
            except Exception as e:  # noqa: BLE001 — un NIT no tumba el barrido
                db.session.rollback()
                res['errores'] += 1
                logger.warning('[CARTERA] barrido NIT %s: %s', nit, e)
        viejas = [r for r in RetencionCartera.query.filter_by(
            estado=EstadoRetencion.RETENIDO).all()
            if datetime.utcnow() - r.creada_en > ALERTA_RETENIDO]
        for r in viejas:
            res['alertas'].append({'id': r.id, 'pedido': r.numero_pedido,
                                   'dias': (datetime.utcnow() - r.creada_en).days})
        if viejas:
            logger.warning('[CARTERA] %d pedido(s) retenidos hace más de 3 días: %s',
                           len(viejas), [a['pedido'] for a in res['alertas']])
    return res


def init_scheduler(app):
    """Cada 30 min; el barrido mismo respeta la ventana 7:00–19:30 Bogotá."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.error('[CARTERA] APScheduler no instalado')
        return None

    def _job():
        with app.app_context():
            try:
                r = barrido()
                logger.info('[CARTERA] barrido: %s', r)
            except Exception as e:  # noqa: BLE001
                db.session.rollback()
                logger.error('[CARTERA] barrido falló: %s', e, exc_info=True)

    scheduler = BackgroundScheduler(timezone='America/Bogota')
    scheduler.add_job(func=_job, trigger=CronTrigger(minute='0,30', hour='7-19',
                                                     timezone='America/Bogota'),
                      id='cartera_barrido', replace_existing=True, max_instances=1,
                      misfire_grace_time=600)
    scheduler.start()
    logger.info('[CARTERA] Scheduler de re-evaluación cada 30 min (7:00–19:30 Bogotá)')
    return scheduler


# ═════════════════════════════════════════════════════════════════════════════
# Lectura: lo que ven el Gestor y el WMS
# ═════════════════════════════════════════════════════════════════════════════

def _iso(v):
    return v.isoformat() if v is not None else None


def retencion_publica(r) -> dict:
    """Una retención como la lee el Gestor (contrato `/api/cartera/*`)."""
    ev = r.evaluacion or {}
    iniciador = _usuario(r.iniciado_por_id)
    ahora = datetime.utcnow()
    return {
        'id': r.id, 'estado': r.estado, 'compuerta': r.compuerta,
        'pedido': r.numero_pedido, 'pedido_clave': r.pedido_clave,
        'tarea_packing_id': r.tarea_packing_id,
        'cliente': r.cliente, 'nit': r.nit, 'sucursal': r.sucursal,
        'vendedor': r.vendedor_id,
        'cond_pago': r.cond_pago, 'dias_credito': r.dias_credito,
        'valor': float(r.valor) if r.valor is not None else None,
        'motivos': r.motivos or [],
        'motivos_codigos': [m.get('codigo') for m in (r.motivos or []) if m.get('retiene')],
        'resumen': ev.get('resumen'),
        'vencidas': ev.get('vencidas') or [],
        'vencidas_n': ev.get('vencidas_n'), 'vencido': ev.get('vencido'),
        'vencido_neto': ev.get('vencido_neto'), 'saldo_a_favor': ev.get('saldo_a_favor'),
        'dias_max': ev.get('dias_max'),
        'cupo': ev.get('cupo'), 'saldo': ev.get('saldo'), 'consumo_wms': ev.get('consumo_wms'),
        'pedidos_wms': ev.get('pedidos_wms') or [],
        'disponible': ev.get('disponible'), 'exceso': ev.get('exceso'),
        'canal': ev.get('canal'), 'habilitacion': ev.get('habilitacion'),
        'as_of': _iso(r.as_of), 'origen_dato': r.origen_dato,
        'politica': {'version': ev.get('version'), 'parametros': ev.get('parametros')},
        'iniciado_por': ({'id': iniciador.id, 'nombre': iniciador.nombre,
                          'email': iniciador.email} if iniciador else
                         ({'texto': r.iniciado_por} if r.iniciado_por else None)),
        'siesa_usuario_creacion': r.siesa_usuario_creacion,
        'contexto_siesa': r.contexto_siesa or {},
        'creada_en': _iso(r.creada_en), 'actualizada_en': _iso(r.actualizada_en),
        'antiguedad_horas': round((ahora - r.creada_en).total_seconds() / 3600, 1),
        'resolucion': ({'por': r.resuelta_por, 'por_id': r.resuelta_por_id,
                        'origen': r.resuelta_origen, 'en': _iso(r.resuelta_en),
                        'motivo': r.motivo_resolucion, 'codigo_excepcion': r.codigo_excepcion,
                        'tope_valor': float(r.tope_valor) if r.tope_valor is not None else None,
                        'vence_en': r.vence_en.isoformat() if r.vence_en else None}
                       if r.estado != EstadoRetencion.RETENIDO else None),
        'reevaluaciones': r.reevaluaciones, 'reevaluada_en': _iso(r.reevaluada_en),
    }


def listar(estado=None, nit=None, cambiados_desde=None, limite: int = 200) -> dict:
    q = RetencionCartera.query
    if estado:
        q = q.filter(RetencionCartera.estado == estado)
    if nit:
        q = q.filter(RetencionCartera.nit == nit_normalizado(nit))
    if cambiados_desde is not None:
        q = q.filter(RetencionCartera.actualizada_en > cambiados_desde)
    filas = q.order_by(RetencionCartera.actualizada_en.asc(),
                       RetencionCartera.id.asc()).limit(max(1, min(int(limite), 500))).all()
    cursor = _iso(filas[-1].actualizada_en) if filas else (
        _iso(cambiados_desde) if cambiados_desde else None)
    return {'retenciones': [retencion_publica(r) for r in filas], 'cursor': cursor,
            'politica': {'version': VERSION_POLITICA, 'parametros': parametros()},
            'generado_en': datetime.utcnow().isoformat()}


def salud() -> dict:
    """Frescura de la cartera y retenidos por antigüedad."""
    ahora = datetime.utcnow()
    vivas = RetencionCartera.query.filter_by(estado=EstadoRetencion.RETENIDO).all()
    tramos = {'<1d': 0, '1-3d': 0, '>3d': 0}
    for r in vivas:
        d = (ahora - r.creada_en).total_seconds() / 86400
        tramos['<1d' if d < 1 else '1-3d' if d <= 3 else '>3d'] += 1
    fotos = CarteraCliente.query.all()
    frescas = [f for f in fotos if f.as_of and ahora - f.as_of < FRESCURA_FOTO]
    ultima = max((f.as_of for f in fotos if f.as_of), default=None)
    con_error = [f for f in fotos if f.ultimo_error]
    habs = CarteraHabilitacion.query.all()
    ultima_hab = max((h.recibido_en for h in habs), default=None)
    m, p_modo = modo()
    try:
        from app.services.connekta_gateway import connekta
        ruta = (connekta.cond_pago_ruta or '').strip()
        simulacion = bool(connekta.modo_simulacion)
    except Exception:  # noqa: BLE001
        ruta, simulacion = '', None
    return {
        'modo': m, 'simulacion': simulacion,
        'politica': {'version': VERSION_POLITICA, 'parametros': parametros()},
        'retenidos': len(vivas), 'retenidos_por_antiguedad': tramos,
        'alertas': [{'id': r.id, 'pedido': r.numero_pedido, 'cliente': r.cliente,
                     'dias': (ahora - r.creada_en).days}
                    for r in vivas if ahora - r.creada_en > ALERTA_RETENIDO],
        'cartera': {'nits_en_foto': len(fotos), 'frescas_24h': len(frescas),
                    'ultima_lectura': _iso(ultima),
                    'edad_horas': _edad_horas(ultima, ahora) if ultima else None,
                    'con_error': [{'nit': f.nit, 'error': f.ultimo_error,
                                   'intento': _iso(f.ultimo_intento_en)} for f in con_error[:20]]},
        'habilitaciones': {'clientes': len(habs), 'ultima_recibida': _iso(ultima_hab)},
        'conversion_a_contado': {'cond_pago_ruta': ruta or None,
                                 'disponible': bool(ruta) and _cp.cobro_contraentrega(ruta)['cobrar']},
        'gestor_token_configurado': bool((os.getenv('CARTERA_GESTOR_TOKEN') or '').strip()),
        'problemas': parametros()['problemas'],
        'generado_en': ahora.isoformat(),
    }


def resumen_por_pedido(numeros: list) -> dict:
    """`{numero_pedido: retención viva publicable}` para la cola de despacho."""
    if not numeros:
        return {}
    out = {}
    for r in RetencionCartera.query.filter(RetencionCartera.numero_pedido.in_(list(numeros)),
                                           RetencionCartera.estado == EstadoRetencion.RETENIDO):
        out[r.numero_pedido] = {'id': r.id, 'resumen': (r.evaluacion or {}).get('resumen'),
                                'motivos': [m.get('codigo') for m in (r.motivos or [])
                                            if m.get('retiene')],
                                'compuerta': r.compuerta, 'desde': _iso(r.creada_en)}
    return out


def informe_de_tarea(tarea, base: dict) -> list:
    """G3 — al iniciar/despachar la ruta: solo informa, sin red."""
    clave = getattr(tarea, 'pedido_clave', None)
    if not clave or (tarea.tipo_documento or '').upper() == 'TRASLADO':
        return []
    out = []
    res = resolucion_vigente(clave)
    if res is not None and res.estado == EstadoRetencion.AUTORIZADO:
        out.append({**base, 'clave': 'cartera_autorizado',
                    'texto': f'Crédito autorizado por cartera ({res.resuelta_por}): '
                             f'{res.motivo_resolucion}'})
    elif res is not None and res.estado == EstadoRetencion.CONVERTIDO_CONTADO:
        out.append({**base, 'clave': 'cartera_contado',
                    'texto': 'Cartera lo convirtió a contado: se cobra al entregar.'})
    if retencion_viva(clave) is not None:
        out.append({**base, 'clave': 'cartera_retenido',
                    'texto': 'Este pedido tiene una retención de cartera viva.'})
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Habilitaciones que empuja el Gestor
# ═════════════════════════════════════════════════════════════════════════════

def registrar_habilitacion(datos: dict, enviado_por: str = None) -> CarteraHabilitacion:
    """Upsert por (nit, sucursal). `as_of` obligatorio: un dato sin fecha no
    se puede fechar después. Una habilitación más vieja que la guardada no la
    pisa. Sin commit."""
    nit = nit_normalizado(datos.get('nit'))
    if not nit:
        raise ValueError('nit es obligatorio')
    suc = sucursal_normalizada(datos.get('sucursal')) if datos.get('sucursal') else ''
    as_of_txt = datos.get('as_of')
    try:
        as_of = datetime.fromisoformat(str(as_of_txt).replace('Z', '+00:00'))
        if as_of.tzinfo is not None:
            as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        raise ValueError('as_of es obligatorio (ISO 8601)')
    canal = (datos.get('canal') or '').strip().upper() or None
    if canal and canal not in CANALES:
        raise ValueError(f'canal debe ser uno de {", ".join(CANALES)}')
    excepciones = []
    for x in datos.get('excepciones') or []:
        if not isinstance(x, dict):
            raise ValueError('excepciones: cada una es {codigo, tope, vence}')
        cod = str(x.get('codigo') or '').strip()
        if cod not in CODIGOS_EXCEPCION:
            raise ValueError(f'excepciones: codigo {cod!r} no es uno de {", ".join(CODIGOS_EXCEPCION)}')
        tope, vence = _dec(x.get('tope')), _fecha(x.get('vence'))
        if tope is None or tope <= 0 or vence is None:
            raise ValueError('excepciones: tope > 0 y vence (YYYY-MM-DD) obligatorios')
        if vence > dia_operativo() + timedelta(days=VIGENCIA_MAX_AUTORIZACION):
            raise ValueError(f'excepciones: vigencia máxima {VIGENCIA_MAX_AUTORIZACION} días')
        excepciones.append({'codigo': cod, 'tope': float(tope), 'vence': vence.isoformat()})
    fila = CarteraHabilitacion.query.filter_by(nit=nit, sucursal=suc).first()
    if fila is not None and fila.as_of and as_of < fila.as_of:
        return fila
    if fila is None:
        fila = CarteraHabilitacion(nit=nit, sucursal=suc)
        db.session.add(fila)
    fila.canal = canal
    fila.acuerdo_vigente = bool(datos.get('acuerdo_vigente'))
    fila.acuerdo_vence = _fecha(datos.get('acuerdo_vence')) if datos.get('acuerdo_vence') else None
    fila.excepciones = excepciones
    fila.as_of = as_of
    fila.recibido_en = datetime.utcnow()
    fila.enviado_por = (enviado_por or '')[:160] or None
    return fila


# ═════════════════════════════════════════════════════════════════════════════
# Paradas anteriores a la regla de contado: autorización en lote
# ═════════════════════════════════════════════════════════════════════════════

def fecha_despliegue_contado():
    """`CONTADO_DESPLIEGUE_FECHA` (YYYY-MM-DD, Bogotá): el día en que
    m043contado entró a producción. Sin default: no se adivina."""
    crudo = (os.getenv('CONTADO_DESPLIEGUE_FECHA') or '').strip()
    if not crudo:
        return None
    return _fecha(crudo)


def recaudos_anteriores_a_contado() -> list:
    """Paradas `credito_no_autorizado` confirmadas ANTES del despliegue de
    m043contado: la regla no existía cuando el conductor las marcó."""
    from app.models.recaudo_entrega import EstadoEntrega, RecaudoEntrega
    corte = fecha_despliegue_contado()
    if corte is None:
        return []
    inicio_utc = datetime.combine(corte, time(0, 0), tzinfo=TZ_BOGOTA).astimezone(
        timezone.utc).replace(tzinfo=None)
    out = []
    for r in (RecaudoEntrega.query
              .filter(RecaudoEntrega.fecha_confirmacion < inicio_utc,
                      RecaudoEntrega.estado_entrega.in_((EstadoEntrega.ENTREGADO,
                                                         EstadoEntrega.PARCIAL)),
                      RecaudoEntrega.credito_autorizado_en.is_(None)).all()):
        if _cp.credito_no_autorizado(r, r.tarea):
            out.append(r)
    return out


def autorizar_lote_anteriores(motivo: str, admin_id: int, ids: list = None) -> dict:
    """Autoriza en lote, con un motivo común, las paradas anteriores a la
    regla de contado. Bitácora por parada. Solo las que `recaudos_anteriores_
    a_contado` devuelve: una parada posterior al despliegue se autoriza una
    por una, en la liquidación."""
    from app.services.bitacora import motivo_obligatorio, registrar_accion
    texto = motivo_obligatorio(motivo, 'la autorización en lote')
    if fecha_despliegue_contado() is None:
        raise AccionRechazada('CONTADO_DESPLIEGUE_FECHA no está configurada: no se adivina '
                              'qué paradas son anteriores a la regla de contado')
    candidatas = recaudos_anteriores_a_contado()
    if ids is not None:
        pedidos = {int(i) for i in ids}
        candidatas = [r for r in candidatas if r.id in pedidos]
    ahora = datetime.utcnow()
    hechas = []
    for r in candidatas:
        antes = {'credito_autorizado_por': None, 'credito_autorizado_en': None,
                 'credito_autorizado_razon': None}
        r.credito_autorizado_por = admin_id
        r.credito_autorizado_en = ahora
        r.credito_autorizado_razon = texto
        tarea = r.tarea
        registrar_accion('EDITAR', r, usuario_id=admin_id, motivo=texto,
                         entidad_codigo=getattr(tarea, 'numero_pedido_siesa', None),
                         antes=antes,
                         despues={'credito_autorizado_por': admin_id,
                                  'credito_autorizado_en': ahora.isoformat(),
                                  'credito_autorizado_razon': texto, 'lote': True,
                                  'anterior_a': fecha_despliegue_contado().isoformat()})
        hechas.append({'recaudo_id': r.id, 'ruta_id': r.ruta_id,
                       'pedido': getattr(tarea, 'numero_pedido_siesa', None)})
    db.session.commit()
    return {'autorizadas': len(hechas), 'paradas': hechas,
            'corte': fecha_despliegue_contado().isoformat()}
