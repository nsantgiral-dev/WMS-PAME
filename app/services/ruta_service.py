"""
RutaService — lógica de negocio de rutas de despacho.
Cubre: Conductores, Vehículos, Rutas Maestras, Rutas de Despacho y Última Milla.
"""
import logging
from datetime import datetime, date
from sqlalchemy.orm import selectinload as _sl, joinedload as _jl
from sqlalchemy.exc import IntegrityError as _IntegrityError
from app.extensions import db
from app.models.bulto import Bulto, EstadoBulto
from app.models.conductor import Conductor
from app.models.packing import TareaPacking, EstadoPacking
from app.models.recaudo_entrega import RecaudoEntrega, forma_pago_de
from app.models.vehiculo import Vehiculo
from app.models.ruta_maestra import RutaMaestra, RutaMaestraParada
from app.models.ruta_despacho import RutaDespacho, EstadoRutaDespacho, EstadoFinancieroRuta
from app.utils.fecha import dia_operativo as _dia_operativo
from app.services.documento_fiscal import filtro_despachable as _filtro_despachable
from app.services.bitacora import (registrar_accion, motivo_obligatorio, foto as foto_fila,
                                   FORZADO_ADVERTENCIAS_FLOTA, FORZADO_CIERRE_RUTA,
                                   FORZADO_LIQUIDACION_SIN_CONTAR, FORZADO_PARADA_TARDIA)

logger = logging.getLogger(__name__)


class ConflictError(ValueError):
    """Señala un conflicto de unicidad (HTTP 409)."""


class AdvertenciasDeFlota(ValueError):
    """El vehículo tiene algo que la flota sabe y quien despacha no reconoció.

    **Informa, no bloquea**: con un motivo escrito la ruta sale igual, y el
    motivo queda en la bitácora como FORZAR. Sin motivo, la ruta responde 409
    con la lista para que la pantalla la muestre y lo pida.
    """

    def __init__(self, advertencias):
        self.advertencias = list(advertencias)
        super().__init__('El vehículo tiene advertencias de flota: '
                         + '; '.join(a['texto'] for a in self.advertencias))


# `EstadoEntrega` vivía definida acá **y** en `models/recaudo_entrega.py`, con
# los mismos valores y distinto nombre de tupla. Se importa la del modelo: un
# estado agregado en un solo lado era cuestión de tiempo.
from app.models.recaudo_entrega import EstadoEntrega  # noqa: E402,F401

#: Lo que cada envío de una parada escribe aunque no cambie nada: la hora del
#: servidor, quién reenvió y la hora del teléfono. Por sí solos no son una
#: edición (reenvío idéntico de la cola, e2e 2026-09-25).
_VOLATILES_PARADA = ('fecha_confirmacion', 'editado_por', 'editado_en',
                     'ts_dispositivo', 'ts_desfase_s', 'via_cola')


def _mismo_valor(a, b) -> bool:
    """¿Dos valores de una foto de la parada son el mismo dato? Los montos se
    comparan como números (Decimal '35700.00' == float 35700.0) y las listas de
    ids sin orden."""
    if a == b:
        return True
    try:
        if a is not None and b is not None and not isinstance(a, (list, dict, bool)) \
                and not isinstance(b, (list, dict, bool)):
            return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        pass
    if isinstance(a, list) and isinstance(b, list):
        try:
            return sorted(a) == sorted(b)
        except TypeError:
            return False
    if (a in (None, '', [], {})) and (b in (None, '', [], {})):
        return True
    return False


class FormaPago:
    EFECTIVO      = 'EFECTIVO'
    TRANSFERENCIA = 'TRANSFERENCIA'
    CHEQUE        = 'CHEQUE'
    CREDITO       = 'CREDITO'
    EXENTO        = 'EXENTO'
    TARJETA       = 'TARJETA'
    #: Medios bancarios específicos — alineados 1:1 con `_forma_pago_map` de
    #: `connekta_gateway.py` y con `_FORMAS_PAGO_COBRO` de `rutas.js`. Antes
    #: de esto, `TRANSFERENCIA` a secas era el único medio bancario que el
    #: conductor podía declarar; el `<select>` se desglosó por banco pero
    #: esta validación se quedó con la lista vieja, y el conductor no podía
    #: confirmar ninguna parada pagada por transferencia (`forma_pago
    #: inválido`, rechazado en el servidor pese a venir de una opción real
    #: de la pantalla).
    TRANSFERENCIAS_BANCO = (
        'TRANSFERENCIA_BANCOLOMBIA_AH', 'TRANSFERENCIA_BANCOLOMBIA_CTE',
        'TRANSFERENCIA_BBVA', 'TRANSFERENCIA_BOGOTA',
        'TRANSFERENCIA_AGRARIO_AH', 'TRANSFERENCIA_AGRARIO_CTE',
        'TRANSFERENCIA_DAVIVIENDA', 'TRANSFERENCIA_IHO_CTE',
    )
    #: `CONSIGNACION` es el otro sinónimo retrocompatible de `_forma_pago_map`
    #: (no vive en el `<select>` actual, pero un `RecaudoEntrega` viejo o una
    #: reedición pueden seguir mandándolo).
    VALIDOS = (EFECTIVO, TRANSFERENCIA, 'CONSIGNACION', TARJETA, CHEQUE,
               CREDITO, EXENTO) + TRANSFERENCIAS_BANCO


class RutaService:

    #: Estados en los que la ruta ya no recibe entregas: un segundo «cerrar»
    #: sobre ellos no escribe nada (lo reenvía la cola del conductor).
    ESTADOS_RUTA_CERRADA = (EstadoRutaDespacho.ENTREGADA,)

    # ── Conductores ──────────────────────────────────────────────────

    @staticmethod
    def listar_conductores(solo_activos: bool, puede_ver_datos_personales: bool) -> list:
        q = Conductor.query.options(_sl(Conductor.usuario)).order_by(Conductor.nombre)
        if solo_activos:
            q = q.filter_by(activo=True)

        def _safe(c):
            d = c.to_dict()
            if not puede_ver_datos_personales:
                d.pop('cedula', None)
                d.pop('telefono', None)
                d.pop('usuario_email', None)
            return d

        return [_safe(c) for c in q.all()]

    @staticmethod
    def crear_conductor(data: dict) -> Conductor:
        for campo in ['nombre', 'cedula']:
            if not data.get(campo):
                raise ValueError(f'Campo requerido: {campo}')
        if Conductor.query.filter_by(cedula=data['cedula']).first():
            raise ConflictError('Ya existe un conductor con esa cédula')
        c = Conductor(
            nombre=data['nombre'].strip(),
            cedula=data['cedula'].strip(),
            telefono=data.get('telefono', '').strip() or None,
            usuario_id=data.get('usuario_id') or None,
        )
        db.session.add(c)
        db.session.commit()
        db.session.refresh(c)
        return c

    @staticmethod
    def crear_cuenta_para_conductor(conductor_id: int, email: str, password: str) -> tuple:
        """Le da cuenta PWA a un conductor que YA EXISTE. No crea otra fila.

        Es lo que faltaba: `POST /api/auth/register` con rol=conductor siempre
        hace `Conductor(...)` nuevo, y `conductores.cedula` es único. Para un
        conductor ya registrado eso deja dos salidas y las dos malas — 409 por
        cédula repetida, o una segunda fila con el mismo nombre donde el
        histórico de rutas queda en una y la cuenta en la otra.

        Acá solo se escribe `Conductor.usuario_id`. Nada más de la fila del
        conductor se toca: ni el nombre, ni la cédula, ni sus rutas.

        Si ya tiene cuenta, **falla ruidosamente**. Sobrescribir el vínculo
        dejaría al conductor anterior sin acceso sin que nadie se entere.
        """
        from app.models.usuario import Usuario

        conductor = db.session.get(Conductor, conductor_id)
        if not conductor:
            raise LookupError(f'Conductor {conductor_id} no existe')
        if conductor.usuario_id is not None:
            raise ConflictError(
                f'{conductor.nombre} ya tiene cuenta ({conductor.usuario.email}). '
                f'Vincularlo a otra dejaría la anterior sin dueño y sin aviso.'
            )
        from app.models.usuario import normalizar_email
        email = normalizar_email(email)
        if not email or not password:
            raise ValueError('Email y contraseña son obligatorios')
        if Usuario.por_email(email):
            raise ConflictError(f'Ya existe un usuario con el email {email}')

        usuario = Usuario(email=email, nombre=conductor.nombre,
                          rol='conductor', activo=True)
        usuario.set_password(password)
        db.session.add(usuario)
        db.session.flush()
        conductor.usuario_id = usuario.id
        db.session.commit()
        return conductor, usuario

    @staticmethod
    def actualizar_conductor(id: int, data: dict, usuario_id: int = None) -> Conductor:
        c = Conductor.query.get(id)
        if not c:
            raise LookupError('Conductor no encontrado')
        _activo_antes = c.activo
        if 'nombre'     in data: c.nombre     = (data['nombre'] or '').strip()
        if 'telefono'   in data: c.telefono   = (data['telefono'] or '').strip() or None
        if 'activo' in data:
            c.activo = bool(data['activo'])
            if c.usuario_id:
                from app.models.usuario import Usuario
                u = Usuario.query.get(c.usuario_id)
                if u:
                    u.activo = c.activo
        if 'usuario_id' in data: c.usuario_id = data['usuario_id'] or None
        RutaService._registrar_cambio_de_activo(c, _activo_antes, usuario_id,
                                                data.get('motivo'))
        db.session.commit()
        db.session.refresh(c)
        return c

    @staticmethod
    def _registrar_cambio_de_activo(fila, activo_antes, usuario_id, motivo) -> None:
        """Baja o reactivación de un maestro: quién, cuándo y por qué.

        Una sola función para conductores y vehículos, por las dos puertas
        (DELETE y PUT con `activo`). Sin cambio de `activo`, no registra nada:
        editar un teléfono no es una baja.
        """
        if bool(activo_antes) == bool(fila.activo):
            return
        registrar_accion(
            'DESACTIVAR' if not fila.activo else 'EDITAR', fila,
            usuario_id=usuario_id, motivo=motivo,
            antes={'activo': bool(activo_antes)}, despues={'activo': bool(fila.activo)})

    @staticmethod
    def desactivar_conductor(id: int, usuario_id: int = None, motivo: str = None) -> None:
        c = Conductor.query.get(id)
        if not c:
            raise LookupError('Conductor no encontrado')
        _activo_antes = c.activo
        c.activo = False
        RutaService._registrar_cambio_de_activo(c, _activo_antes, usuario_id, motivo)
        db.session.commit()

    # ── Vehículos ─────────────────────────────────────────────────────

    @staticmethod
    def listar_vehiculos(solo_activos: bool) -> list:
        q = Vehiculo.query.order_by(Vehiculo.placa)
        if solo_activos:
            q = q.filter_by(activo=True)
        return [v.to_dict() for v in q.all()]

    @staticmethod
    def crear_vehiculo(data: dict) -> Vehiculo:
        for campo in ['placa', 'tipo']:
            if not data.get(campo):
                raise ValueError(f'Campo requerido: {campo}')
        placa = data['placa'].strip().upper()
        if Vehiculo.query.filter_by(placa=placa).first():
            raise ConflictError(f'Ya existe un vehículo con placa {placa}')
        from flota.dominio.valores import tipo_de_vehiculo
        v = Vehiculo(placa=placa, tipo=tipo_de_vehiculo(data['tipo']),
                     capacidad_kg=data.get('capacidad_kg') or None,
                     codigo_siesa=data.get('codigo_siesa', '').strip() or None)
        db.session.add(v)
        db.session.commit()
        return v

    @staticmethod
    def actualizar_vehiculo(id: int, data: dict, usuario_id: int = None) -> Vehiculo:
        v = Vehiculo.query.get(id)
        if not v:
            raise LookupError('Vehículo no encontrado')
        _activo_antes = v.activo
        if 'tipo' in data:
            # Se valida solo si CAMBIA: un vehículo viejo con un tipo fuera del
            # catálogo se sigue pudiendo editar en lo demás.
            from flota.dominio.valores import normalizar_tipo, tipo_de_vehiculo
            if normalizar_tipo(data['tipo'] if isinstance(data['tipo'], str) else '') \
                    != normalizar_tipo(v.tipo):
                v.tipo = tipo_de_vehiculo(data['tipo'])
        if 'capacidad_kg' in data: v.capacidad_kg = data['capacidad_kg'] or None
        if 'codigo_siesa' in data: v.codigo_siesa = (data['codigo_siesa'] or '').strip() or None
        if 'activo'       in data: v.activo       = bool(data['activo'])
        RutaService._registrar_cambio_de_activo(v, _activo_antes, usuario_id,
                                                data.get('motivo'))
        db.session.commit()
        return v

    @staticmethod
    def desactivar_vehiculo(id: int, usuario_id: int = None, motivo: str = None) -> None:
        v = Vehiculo.query.get(id)
        if not v:
            raise LookupError('Vehículo no encontrado')
        _activo_antes = v.activo
        v.activo = False
        RutaService._registrar_cambio_de_activo(v, _activo_antes, usuario_id, motivo)
        db.session.commit()

    # ── Rutas Maestras ───────────────────────────────────────────────

    @staticmethod
    def listar_maestras(solo_activas: bool) -> list:
        q = RutaMaestra.query.options(_sl(RutaMaestra.paradas)).order_by(RutaMaestra.nombre)
        if solo_activas:
            q = q.filter_by(activa=True)
        return [m.to_dict() for m in q.all()]

    @staticmethod
    def obtener_maestra(id: int) -> RutaMaestra:
        m = RutaMaestra.query.get(id)
        if not m:
            raise LookupError('Ruta maestra no encontrada')
        return m

    @staticmethod
    def crear_maestra(data: dict) -> RutaMaestra:
        for campo in ['nombre', 'tipo_ruta']:
            if not data.get(campo):
                raise ValueError(f'Campo requerido: {campo}')
        if data['tipo_ruta'] not in ('Urbana', 'Municipal'):
            raise ValueError('tipo_ruta debe ser Urbana o Municipal')
        if RutaMaestra.query.filter_by(nombre=data['nombre'].strip()).first():
            raise ConflictError('Ya existe una ruta maestra con ese nombre')

        m = RutaMaestra(nombre=data['nombre'].strip(), tipo_ruta=data['tipo_ruta'])
        db.session.add(m)
        db.session.flush()

        for i, municipio in enumerate(data.get('paradas', [])):
            nombre = municipio['municipio'] if isinstance(municipio, dict) else municipio
            nombre = (nombre or '').strip()
            if nombre:
                db.session.add(RutaMaestraParada(
                    ruta_maestra_id=m.id,
                    municipio=nombre,
                    orden=i + 1,
                ))
        try:
            db.session.commit()
        except _IntegrityError:
            db.session.rollback()
            raise ConflictError('Ya existe una ruta maestra con ese nombre')
        db.session.refresh(m)
        return m

    @staticmethod
    def actualizar_maestra(id: int, data: dict, usuario_id: int = None) -> RutaMaestra:
        m = RutaMaestra.query.get(id)
        if not m:
            raise LookupError('Ruta maestra no encontrada')
        _campos = ['nombre', 'tipo_ruta', 'activa']
        antes = {**foto_fila(m, _campos), 'paradas': [p.municipio for p in m.paradas]}
        if 'nombre'    in data: m.nombre    = data['nombre'].strip()
        if 'tipo_ruta' in data: m.tipo_ruta = data['tipo_ruta']
        if 'activa'    in data: m.activa    = bool(data['activa'])

        if 'paradas' in data:
            # Las paradas se reescriben enteras: las viejas quedan en el
            # `antes` del registro EDITAR de abajo.
            for p in m.paradas:
                db.session.delete(p)
            db.session.flush()
            for i, municipio in enumerate(data['paradas']):
                nombre = municipio['municipio'] if isinstance(municipio, dict) else municipio
                nombre = (nombre or '').strip()
                if nombre:
                    db.session.add(RutaMaestraParada(
                        ruta_maestra_id=m.id,
                        municipio=nombre,
                        orden=i + 1,
                    ))
        db.session.flush()
        db.session.expire(m, ['paradas'])
        despues = {**foto_fila(m, _campos), 'paradas': [p.municipio for p in m.paradas]}
        cambios = [k for k in antes if antes[k] != despues[k]]
        if cambios:
            registrar_accion(
                'DESACTIVAR' if cambios == ['activa'] and not m.activa else 'EDITAR',
                m, usuario_id=usuario_id, motivo=data.get('motivo'),
                antes={k: antes[k] for k in cambios},
                despues={k: despues[k] for k in cambios})
        db.session.commit()
        db.session.refresh(m)
        return m

    @staticmethod
    def eliminar_maestra(id: int, usuario_id: int = None, motivo: str = None) -> None:
        m = RutaMaestra.query.get(id)
        if not m:
            raise LookupError('Ruta maestra no encontrada')
        if RutaDespacho.query.filter_by(ruta_maestra_id=id).first():
            raise ConflictError('No se puede eliminar: la ruta tiene viajes asociados. Desactívala en su lugar.')
        registrar_accion('ELIMINAR', m, usuario_id=usuario_id, motivo=motivo,
                         antes={**foto_fila(m), 'paradas': [foto_fila(p) for p in m.paradas]})
        for p in m.paradas:
            db.session.delete(p)
        db.session.delete(m)
        db.session.commit()

    # ── Programar viaje desde plantilla ─────────────────────────────

    @staticmethod
    def programar_viaje(data: dict) -> RutaDespacho:
        for campo in ['ruta_maestra_id', 'fecha_programada', 'conductor_id', 'vehiculo_id']:
            if not data.get(campo):
                raise ValueError(f'Campo requerido: {campo}')

        maestra = RutaMaestra.query.get(data['ruta_maestra_id'])
        if not maestra or not maestra.activa:
            raise LookupError('Ruta maestra no encontrada o inactiva')

        conductor = Conductor.query.get(data['conductor_id'])
        if not conductor or not conductor.activo:
            raise LookupError('Conductor no encontrado o inactivo')

        vehiculo = Vehiculo.query.get(data['vehiculo_id'])
        if not vehiculo or not vehiculo.activo:
            raise LookupError('Vehículo no encontrado o inactivo')

        try:
            fecha = date.fromisoformat(data['fecha_programada'])
        except ValueError:
            raise ValueError('fecha_programada debe ser YYYY-MM-DD')

        ruta = RutaDespacho(
            ruta_maestra_id=maestra.id,
            conductor_id=conductor.id,
            vehiculo_id=vehiculo.id,
            tipo_ruta=maestra.tipo_ruta,
            fecha_programada=fecha,
            notas=data.get('notas', '').strip() or None,
            estado=EstadoRutaDespacho.PROGRAMADO,
        )
        db.session.add(ruta)
        db.session.flush()
        ruta_id = ruta.id
        db.session.commit()
        return RutaDespacho.query.options(
            _jl(RutaDespacho.conductor),
            _jl(RutaDespacho.vehiculo),
            _jl(RutaDespacho.ruta_maestra),
        ).get(ruta_id)

    # ── Rutas de Despacho ────────────────────────────────────────────

    @staticmethod
    def listar_rutas(conductor_id=None, vehiculo_id=None, estado=None,
                     fecha_desde=None, fecha_hasta=None, page=1):
        from datetime import date as _date
        q = (RutaDespacho.query
             .options(
                 _jl(RutaDespacho.conductor),
                 _jl(RutaDespacho.vehiculo),
                 _jl(RutaDespacho.ruta_maestra),
                 _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
             )
             .order_by(RutaDespacho.fecha_programada.desc().nullslast(),
                       RutaDespacho.fecha_creacion.desc()))
        if conductor_id:
            q = q.filter_by(conductor_id=conductor_id)
        if vehiculo_id:
            q = q.filter_by(vehiculo_id=vehiculo_id)
        if estado:
            q = q.filter_by(estado=estado)
        # Rango de fechas (default: hoy si no se especifica).
        #
        # `fecha_programada IS NULL` cuenta como "siempre visible", no como
        # "nunca" — que es lo que `>=`/`<=`/`==` normales le hacen a NULL en
        # SQL. `crear_ruta()` (ad-hoc, sin RutaMaestra) podía dejarla sin
        # asignar, y con un filtro estricto esa ruta no aparecía en NINGÚN
        # rango de fechas posible, ni siquiera uno amplio a propósito. Con
        # `crear_ruta()` ya asignándola (Regla 0), esto queda como red de
        # seguridad para lo que quedó huérfano en producción.
        if fecha_desde or fecha_hasta:
            fd = _date.fromisoformat(fecha_desde) if fecha_desde else _dia_operativo()
            fh = _date.fromisoformat(fecha_hasta) if fecha_hasta else fd
            if fh < fd:
                fd, fh = fh, fd
            q = q.filter(db.or_(
                RutaDespacho.fecha_programada.is_(None),
                db.and_(RutaDespacho.fecha_programada >= fd,
                        RutaDespacho.fecha_programada <= fh),
            ))
        else:
            # Sin filtro explícito → hoy (+ huérfanas sin fecha) para no
            # cargar todo el histórico.
            q = q.filter(db.or_(
                RutaDespacho.fecha_programada.is_(None),
                RutaDespacho.fecha_programada == _dia_operativo(),
            ))
        return q.paginate(page=page, per_page=50, error_out=False)

    @staticmethod
    def crear_ruta(data: dict) -> RutaDespacho:
        for campo in ['conductor_id', 'vehiculo_id', 'tipo_ruta']:
            if not data.get(campo):
                raise ValueError(f'Campo requerido: {campo}')
        if data['tipo_ruta'] not in ('Urbana', 'Municipal'):
            raise ValueError('tipo_ruta debe ser Urbana o Municipal')

        conductor = Conductor.query.get(data['conductor_id'])
        if not conductor or not conductor.activo:
            raise LookupError('Conductor no encontrado o inactivo')
        vehiculo = Vehiculo.query.get(data['vehiculo_id'])
        if not vehiculo or not vehiculo.activo:
            raise LookupError('Vehículo no encontrado o inactivo')

        ruta = RutaDespacho(
            conductor_id=data['conductor_id'],
            vehiculo_id=data['vehiculo_id'],
            tipo_ruta=data['tipo_ruta'],
            notas=data.get('notas', '').strip() or None,
            estado=EstadoRutaDespacho.EN_CARGUE,
            # A diferencia de `programar_viaje` (desde RutaMaestra, exige la
            # fecha), esta ruta ad-hoc del muelle nunca traía una — quedaba
            # `fecha_programada IS NULL` para siempre (nada la asigna después:
            # `cerrar_ruta`/`entregar_ruta` solo tocan `fecha_cierre`/
            # `fecha_entregada`). Cualquier consulta filtrada por rango de
            # fechas —el dashboard de Liquidación, el panel de Rutas sin
            # filtro explícito— excluye NULL por construcción en SQL: la ruta
            # quedaba invisible para liquidar sin importar qué fecha se
            # pidiera. `_dia_operativo()` (Bogotá, Regla 5) porque es el día
            # en que se despachó de verdad.
            fecha_programada=_dia_operativo(),
        )
        db.session.add(ruta)
        db.session.flush()
        ruta_id = ruta.id
        db.session.commit()
        return RutaDespacho.query.options(
            _jl(RutaDespacho.conductor),
            _jl(RutaDespacho.vehiculo),
        ).get(ruta_id)

    @staticmethod
    def obtener_ruta(id: int) -> RutaDespacho:
        ruta = (RutaDespacho.query
                .options(
                    _jl(RutaDespacho.conductor),
                    _jl(RutaDespacho.vehiculo),
                    _jl(RutaDespacho.ruta_maestra),
                    _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                )
                .get(id))
        if not ruta:
            raise LookupError('Ruta no encontrada')
        return ruta

    @staticmethod
    def _reconocer_advertencias_flota(ruta, momento: str, motivo=None,
                                      usuario_id: int = None) -> list:
        """Lo que la flota sabe del vehículo, reconocido o no. **Una función**
        para las dos puertas (iniciar el cargue y despachar).

        Sin advertencias → nada. Con advertencias ya reconocidas en esta ruta
        (un FORZAR anterior cuyas claves las cubren todas) → nada: no se pide
        el mismo motivo dos veces en el mismo viaje. Con advertencias nuevas y
        sin motivo → `AdvertenciasDeFlota`. Con motivo → FORZAR en la bitácora
        (misma transacción que la transición) y la ruta sigue.
        """
        from app.models.bitacora import BitacoraAccion
        from app.services import senales_ruta as _sr
        advertencias = _sr.advertencias_de_flota(ruta)
        if not advertencias:
            return []
        claves = {a['clave'] for a in advertencias}
        reconocidas = set()
        for fila in (BitacoraAccion.query
                     .filter_by(accion='FORZAR', entidad='RutaDespacho', entidad_id=ruta.id)
                     .all()):
            reconocidas |= set((fila.despues or {}).get('advertencias_flota') or [])
        if claves <= reconocidas:
            return advertencias
        texto = str(motivo).strip() if motivo is not None else ''
        if not texto:
            raise AdvertenciasDeFlota(advertencias)
        registrar_accion(
            'FORZAR', ruta, usuario_id=usuario_id, motivo=texto,
            entidad_codigo=f'RUTA-{ruta.id}',
            antes={'estado': ruta.estado},
            despues={'forzado': FORZADO_ADVERTENCIAS_FLOTA,
                     'momento': momento,
                     'advertencias_flota': sorted(claves),
                     'detalle': advertencias})
        return advertencias

    #: Tope de consultas de cartera por despacho: el informe es un aviso y no
    #: puede demorar la salida del camión.
    _TOPE_CARTERA_DESPACHO = 40

    @staticmethod
    def _informe_de_cobro(ruta, consultar_cartera: bool = True) -> list:
        """Lo que conviene saber de cada factura ANTES de que salga el camión.
        **Informa, no bloquea** (a diferencia de la flota, no pide motivo): el
        conductor igual la cobra, porque la política cobra ante la duda.

        De paso escribe el snapshot de clasificación de cada tarea (respaldo,
        sin red): es lo que el teléfono cachea si al abrir las paradas no hay
        señal. El que llama hace el commit.

        · `cobro_supuesto` — sin condición de pago conocida (o fuera de la
          tabla): se cobra como contado y conviene confirmarlo con el asesor.
        · `fe_contado` — el pedido declara la condición de CONTADO documental.
        · `fe_saldada` — la cartera dice que la factura ya está pagada: el
          conductor no debería cobrarla otra vez. Solo si `cxc_cruce` lo sabe
          (`None` → no se dice nada), con tope de consultas y sin insistir si
          Siesa falla.
        """
        from app.services import cond_pago as _cp
        from app.services.connekta_gateway import connekta
        tareas, vistas = [], set()
        for b in (ruta.bultos or []):
            t = b.tarea
            if t is None or t.id in vistas or t.tipo_documento == 'TRASLADO':
                continue
            vistas.add(t.id)
            tareas.append(t)
        informe = []
        consultas, cartera_viva = 0, consultar_cartera and not connekta.modo_simulacion
        for t in tareas:
            cobro = _cp.anotar_en_tarea(t)
            base = {'tarea_id': t.id, 'pedido': t.numero_pedido_siesa,
                    'cliente': t.cliente, 'cond_pago': cobro['codigo'],
                    'dias_credito': cobro['dias'], 'clasif_origen': cobro['origen']}
            if cobro['origen'] != _cp.MAESTRO:
                informe.append({**base, 'clave': 'cobro_supuesto',
                                'texto': 'Sin condición de pago conocida: el conductor la '
                                         'cobra como contado. Confirmala con el asesor.'})
            # Cartera (G3): autorizado, convertido a contado o retenido. Sin red.
            from app.services.cartera_service import informe_de_tarea as _inf_cartera
            informe.extend(_inf_cartera(t, base))
            if cobro['codigo'] and _cp.clasificar(
                    cobro['codigo'], connekta.cond_pago_ventas) == _cp.CONTADO:
                informe.append({**base, 'clave': 'fe_contado',
                                'texto': f'El pedido declara contado ({cobro["codigo"]}).'})
            if (cartera_viva and cobro['cobrar'] and t.fe_tipo and t.fe_consec
                    and consultas < RutaService._TOPE_CARTERA_DESPACHO):
                consultas += 1
                try:
                    from app.services import cxc_cruce as _cx
                    from app.services.liquidacion_service import _obtener_tercero
                    nit, _suc = _obtener_tercero(t)
                    saldada = (_cx.esta_saldada(
                        connekta.get_cxc_general(nit), t.tipo_docto_pedido_siesa,
                        t.consec_docto_pedido_siesa, t.fe_tipo, t.fe_consec)
                        if nit else None)
                except Exception as e:  # noqa: BLE001 — un aviso no insiste
                    logger.warning('[RUTAS] informe de cobro: cartera no disponible (%s); '
                                   'no se sigue preguntando', e)
                    cartera_viva, saldada = False, None
                if saldada is True:
                    informe.append({**base, 'clave': 'fe_saldada',
                                    'texto': 'La cartera de Siesa dice que esta factura ya '
                                             'está pagada: no la cobres otra vez.'})
        return informe

    @staticmethod
    def iniciar_ruta(id: int, motivo_advertencias: str = None,
                     usuario_id: int = None) -> dict:
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.PROGRAMADO:
            raise ValueError(f'La ruta debe estar PROGRAMADO, está {ruta.estado}')

        advertencias = RutaService._reconocer_advertencias_flota(
            ruta, 'iniciar_cargue', motivo_advertencias, usuario_id)
        informe_cobro = RutaService._informe_de_cobro(ruta, consultar_cartera=False)
        ruta.estado = EstadoRutaDespacho.EN_CARGUE
        ruta_id = ruta.id
        db.session.commit()
        ruta = RutaDespacho.query.options(
            _jl(RutaDespacho.conductor),
            _jl(RutaDespacho.vehiculo),
            _jl(RutaDespacho.ruta_maestra),
        ).get(ruta_id)

        sugeridos_ids = []
        if ruta.ruta_maestra:
            municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
            bultos_libres = (Bulto.query
                .options(_sl(Bulto.tarea))
                .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
                .filter(
                    _filtro_despachable(TareaPacking),
                    TareaPacking.estado != EstadoPacking.CANCELADO,
                    Bulto.estado == EstadoBulto.PENDIENTE,
                    Bulto.ruta_despacho_id == None,
                ).all())
            sugeridos_ids = [b.id for b in bultos_libres
                             if (b.tarea.municipio or '').lower() in municipios]

        return {
            'ok': True,
            'ruta': ruta.to_dict(),
            'sugeridos_count': len(sugeridos_ids),
            'sugeridos_ids': sugeridos_ids,
            'advertencias_flota': advertencias,
            'informe_cobro': informe_cobro,
        }

    @staticmethod
    def obtener_sugeridos(ruta_id: int) -> list:
        ruta = RutaDespacho.query.get(ruta_id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if not ruta.ruta_maestra:
            return []

        municipios = {p.municipio.lower() for p in ruta.ruta_maestra.paradas}
        bultos_libres = (Bulto.query
            .options(_sl(Bulto.tarea))
            .join(TareaPacking, Bulto.tarea_id == TareaPacking.id)
            .filter(
                _filtro_despachable(TareaPacking),
                TareaPacking.estado != EstadoPacking.CANCELADO,
                Bulto.estado == EstadoBulto.PENDIENTE,
                Bulto.ruta_despacho_id == None,
            ).all())
        return [b.to_dict() for b in bultos_libres
                if (b.tarea.municipio or '').lower() in municipios]

    @staticmethod
    def cerrar_ruta(id: int, motivo_advertencias: str = None,
                    usuario_id: int = None) -> RutaDespacho:
        ruta = (RutaDespacho.query
                .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
                .get(id))
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_CARGUE:
            raise ValueError(f'La ruta ya está en estado {ruta.estado}')
        if not ruta.bultos:
            raise ValueError('No hay bultos asignados a esta ruta')

        sin_confirmar = Bulto.query.filter_by(ruta_despacho_id=ruta.id, estado=EstadoBulto.PENDIENTE).count()
        if sin_confirmar > 0:
            raise ValueError(
                f'Faltan {sin_confirmar} bulto{"s" if sin_confirmar != 1 else ""} por confirmar. '
                f'Escanéalos en el muelle antes de cerrar la ruta.'
            )

        # La ruta no sale con un bulto cuya caja no tiene remisión y factura
        # confirmadas (decisión del dueño, 2026-09-25). Ya cargado o no: un
        # bulto que entró antes de la regla tampoco sale sin documento.
        from app.services.documento_fiscal import motivo_no_despachable
        _sin_doc = sorted({m for b in ruta.bultos if b.tarea is not None
                           for m in [motivo_no_despachable(b.tarea)] if m})
        if _sin_doc:
            raise ValueError('La ruta no puede salir. ' + ' '.join(_sin_doc))

        # El despacho es cuando el camión sale: la última puerta donde la flota
        # puede decir algo. Informa, no bloquea — con motivo, sale.
        RutaService._reconocer_advertencias_flota(
            ruta, 'despachar', motivo_advertencias, usuario_id)
        # Informa, no bloquea: y escribe el snapshot de cobro (misma transacción).
        informe_cobro = RutaService._informe_de_cobro(ruta)

        _n_bultos = len(ruta.bultos)
        _ruta_id  = ruta.id
        ruta.estado = EstadoRutaDespacho.EN_TRANSITO
        ruta.fecha_cierre = datetime.utcnow()
        db.session.commit()
        logger.info(f'[RUTAS] Ruta {_ruta_id} EN_CARGUE → EN_TRANSITO ({_n_bultos} bultos)')

        ruta = (RutaDespacho.query
                .options(
                    _jl(RutaDespacho.conductor),
                    _jl(RutaDespacho.vehiculo),
                    _jl(RutaDespacho.ruta_maestra),
                    _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                )
                .get(_ruta_id))
        # Transitorio (no es columna): lo lee la ruta HTTP para mostrarlo.
        ruta.informe_cobro = informe_cobro
        return ruta

    @staticmethod
    def entregar_ruta(id: int, data: dict, usuario_id: int) -> dict:
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
            raise ValueError(f'La ruta debe estar EN_TRANSITO, está {ruta.estado}')

        ahora = datetime.utcnow()
        rechazados = 0
        entregados = 0
        confirmaciones = data.get('bultos', []) or []

        # ── Solo se marca lo que alguien DECLARÓ ────────────────────────────
        #
        # Antes: todo bulto de la ruta que no venía en el payload —o venía sin
        # `entregado`— quedaba ENTREGADO. Un bulto que el conductor había
        # marcado RECHAZADO en la parada, y que después no venía en este cierre,
        # se volvía «entregado» y salía de la lista de reingreso: la caja
        # desaparecía del inventario sin que nadie la hubiera entregado.
        #
        # Ahora un bulto se toca solo si su entrada trae `entregado` como
        # booleano literal. El resto conserva su estado (el de la parada, si la
        # hubo) y se DECLARA en `sin_declarar` con el que tenía. El cierre del
        # conductor —y su cola offline— manda `bultos: []` porque cada parada
        # ya marcó sus bultos: ese flujo no cambia.
        por_id = {}
        for c in confirmaciones:
            if isinstance(c, dict) and c.get('id') is not None:
                por_id[c['id']] = c
        bultos_ruta = Bulto.query.filter_by(ruta_despacho_id=id).with_for_update().all()
        sin_declarar = []
        for bulto in bultos_ruta:
            conf = por_id.get(bulto.id)
            entregado = conf.get('entregado') if conf else None
            if entregado is True:
                bulto.estado = EstadoBulto.ENTREGADO
                bulto.fecha_entrega = ahora
                entregados += 1
            elif entregado is False:
                bulto.estado = EstadoBulto.RECHAZADO
                bulto.fecha_entrega = ahora
                bulto.motivo_rechazo = (conf.get('motivo_rechazo') or 'Sin especificar')[:100]
                rechazados += 1
            elif bulto.estado not in (EstadoBulto.ENTREGADO, EstadoBulto.RECHAZADO):
                # Ni el cierre ni una parada dijeron qué pasó con este bulto.
                sin_declarar.append({'id': bulto.id, 'codigo_barras': bulto.codigo_barras,
                                     'estado': bulto.estado, 'tarea_id': bulto.tarea_id})

        # Las paradas que nadie gestionó (P1-4). El cierre no se niega —la
        # cola sin señal manda el cierre de un conductor que sí entregó, y un
        # cierre trabado en el teléfono no lo destraba nadie—, pero lo DICE:
        # la ruta queda ENTREGADA con N paradas por gestionar, y la salida es
        # confirmarlas después del cierre (con motivo, FORZAR) o forzar el
        # cierre de lo que falta. `liquidar_ruta` sigue exigiéndolas todas.
        _gestionadas = {r.tarea_id for r in RecaudoEntrega.query.filter_by(ruta_id=id).all()}
        paradas_sin_gestionar = [
            {'tarea_id': t.id, 'pedido': t.numero_pedido_siesa, 'cliente': t.cliente}
            for t in ruta.tareas_unicas() if t.id not in _gestionadas]

        ruta.estado = EstadoRutaDespacho.ENTREGADA
        ruta.fecha_entregada = ahora
        _ruta_id = ruta.id
        db.session.commit()
        if sin_declarar:
            logger.warning('[RUTAS] Ruta %s entregada con %d bulto(s) sin declarar: %s',
                           _ruta_id, len(sin_declarar), [b['id'] for b in sin_declarar])
        if paradas_sin_gestionar:
            logger.warning('[RUTAS] Ruta %s entregada con %d parada(s) sin gestionar: %s',
                           _ruta_id, len(paradas_sin_gestionar),
                           [x['tarea_id'] for x in paradas_sin_gestionar])

        ruta = (RutaDespacho.query
                .options(
                    _jl(RutaDespacho.conductor),
                    _jl(RutaDespacho.vehiculo),
                    _jl(RutaDespacho.ruta_maestra),
                )
                .get(_ruta_id))
        return {
            'ok': True,
            'entregados': entregados,
            'rechazados': rechazados,
            # Bultos que nadie declaró, con el estado que conservan. No es un
            # error del cierre: es lo que alguien tiene que ir a mirar.
            'sin_declarar': sin_declarar,
            # Paradas sin confirmar: la ruta no se liquida hasta gestionarlas.
            'paradas_sin_gestionar': paradas_sin_gestionar,
            'ruta': ruta.to_dict(),
        }

    @staticmethod
    def mis_rutas(usuario_id: int) -> dict:
        conductor = Conductor.query.filter_by(usuario_id=usuario_id, activo=True).first()
        if not conductor:
            raise LookupError('Tu cuenta no está vinculada a ningún conductor')

        rutas = (RutaDespacho.query
                 .options(
                     _jl(RutaDespacho.conductor),
                     _jl(RutaDespacho.vehiculo),
                     _jl(RutaDespacho.ruta_maestra),
                     _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                 )
                 .filter_by(conductor_id=conductor.id, estado=EstadoRutaDespacho.EN_TRANSITO)
                 .order_by(RutaDespacho.fecha_cierre.desc())
                 .all())
        return {
            'conductor': conductor.to_dict(),
            'rutas': [r.to_dict(include_bultos=True) for r in rutas],
        }

    # ── Última Milla: paradas y recaudos ─────────────────────────────

    @staticmethod
    def listar_paradas(ruta_id: int) -> dict:
        ruta = (RutaDespacho.query
                .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
                .get(ruta_id))
        if not ruta:
            raise LookupError('Ruta no encontrada')

        from app.models.packing import ItemPacking
        tareas_map: dict = {}
        for b in ruta.bultos:
            if not b.tarea:
                continue
            tid = b.tarea_id
            if tid not in tareas_map:
                t = b.tarea
                items_raw = (ItemPacking.query
                             .filter_by(tarea_id=tid)
                             .options(_sl(ItemPacking.producto))
                             .all())
                tareas_map[tid] = {
                    '_tarea':        t,  # retirado antes de devolver — ver más abajo
                    'tarea_id':      tid,
                    'numero_pedido': t.numero_pedido_siesa,
                    'cliente':       t.cliente or '',
                    'municipio':     t.municipio or '',
                    'bultos':        [],
                    'recaudo':       None,
                    'items': [
                        {
                            'producto_id':    i.producto_id,
                            'codigo':         i.producto.codigo if i.producto else '',
                            'nombre':         i.producto.nombre if i.producto else '',
                            'unidad':         i.producto.unidad_empaque if i.producto else 'und',
                            'cantidad_pedida': i.cantidad_real or i.cantidad_esperada,
                        }
                        for i in items_raw
                    ],
                }
            tareas_map[tid]['bultos'].append({
                'id':            b.id,
                'codigo_barras': b.codigo_barras,
                'tipo':          b.tipo,
                'numero':        b.numero,
                'total':         b.total,
                'estado':        b.estado,
            })

        for r in RecaudoEntrega.query.filter_by(ruta_id=ruta_id).all():
            if r.tarea_id in tareas_map:
                tareas_map[r.tarea_id]['recaudo'] = r.to_dict()

        # Precarga valor_factura/es_contado/valores por referencia — una vez,
        # cuando el conductor abre la lista. El resultado completo (`d`) se
        # cachea entero en el dispositivo (ver condAbrirParadas en rutas.js),
        # así que esto es lo único que necesita conectividad; la confirmación
        # de cada parada ya funciona offline sin volver a tocar Siesa.
        # Vendedores — una sola llamada para toda la ruta (100 filas fijas,
        # no una por tarea). Si Siesa no responde, el mapa queda vacío y
        # cada tarea simplemente no muestra el bloque de asesor.
        from app.services.connekta_gateway import connekta as _connekta_vend
        # Si el maestro de condiciones tiene consulta dinámica, este es un
        # momento con señal para refrescarlo (TTL; sin consulta, no hace nada).
        from app.services import cond_pago as _cp_tabla
        _cp_tabla.refrescar_tabla_desde_siesa()
        vendedores_map: dict = {}
        try:
            for v in _connekta_vend.get_vendedor_contacto():
                cod = str(v.get('codigo_vendedor', '')).strip()
                if not cod:
                    continue
                nombre = ' '.join(filter(None, [
                    str(v.get('f200_nombres', '') or '').strip(),
                    str(v.get('f200_apellido1', '') or '').strip(),
                    str(v.get('f200_apellido2', '') or '').strip(),
                ])).strip()
                vendedores_map[cod] = {
                    'nombre': nombre or str(v.get('f200_razon_social', '') or '').strip() or None,
                    'telefono': str(v.get('f015_telefono', '') or '').strip() or None,
                }
        except Exception as e:
            logger.warning('[RUTAS] no se pudo cargar vendedores_contacto: %s', e)

        # Base y numerador de la medición que se publica abajo: de cuántas
        # referencias se pudo sacar un unitario y cuántas quedaron sin poder
        # desambiguar. Se acumulan sobre TODAS las paradas de la ruta porque la
        # pregunta operativa ("¿cuánto de esta ruta va a salir en campo
        # libre?") es de la ruta, no de una factura.
        _refs_valoradas = _refs_ambiguas = 0
        for tid, p in tareas_map.items():
            t = p.pop('_tarea')
            _diag = {'valoradas': 0, 'ambiguas': 0, 'referencias': []}
            # Siete elementos desde el merge con main (2026-09-11): los tres
            # últimos son suyos —base gravable, IVA y código de vendedor— y no
            # tocan ningún campo del reparto por referencia.
            (valor_factura, es_contado, valores_ref, cond_pago_crudo,
             base_gravable, iva_factura, codigo_vendedor) = \
                RutaService._valor_y_cond_pago(t, diagnostico=_diag)
            _refs_valoradas += _diag['valoradas']
            _refs_ambiguas  += _diag['ambiguas']
            # Campos NUEVOS (aditivos, no reemplazan a nadie): el consumidor
            # puede distinguir "esta referencia no vino en la factura" de "vino
            # y no se pudo saber a qué precio". Sin esto, las dos llegaban como
            # `valor_unitario: null` y eran indistinguibles.
            p['referencias_valoradas'] = _diag['valoradas']
            p['referencias_ambiguas']  = _diag['ambiguas']
            _ambiguas = set(_diag['referencias'])
            p['valor_factura'] = valor_factura
            # Desglose informativo (base + IVA = valor_factura) para que la
            # pantalla del conductor lo muestre igual que en la factura real.
            # `None` si Siesa no respondió — el frontend ya sabe caer al total
            # solo cuando falta cualquiera de los dos.
            p['base_gravable'] = base_gravable
            p['iva_factura']   = iva_factura
            p['es_contado']    = es_contado
            # `''` = Siesa respondió sin condición. `None` = no se pudo
            # preguntar. Son cosas distintas y colapsarlas fue el defecto.
            p['cond_pago']     = cond_pago_crudo
            _vend = vendedores_map.get(str(codigo_vendedor or '').strip()) or {}
            p['vendedor_nombre']   = _vend.get('nombre')
            p['vendedor_telefono'] = _vend.get('telefono')
            # El modo lo calculaba `rutas.js` y se descartaba: el desglose
            # sabía qué eligió el conductor, no qué opciones tenía enfrente.
            from app.services import cond_pago as _cpm
            from app.services.connekta_gateway import connekta
            # PRIMERO se llenan los `valor_unitario` — `_hay_valor` los lee.
            # Estaba al revés: `_hay_valor` se calculaba antes de este bucle,
            # así que `it.get('valor_unitario')` encontraba la clave ausente en
            # TODOS los ítems (nunca `None` puesto a propósito, sino no
            # asignado todavía) y `_hay_valor` daba `False` siempre — el modo
            # DINAMICO no se disparaba nunca, con o sin C02, con o sin precio.
            for item in p['items']:
                item['valor_unitario'] = valores_ref.get(item['codigo'])
                item['valor_unitario_ambiguo'] = item['codigo'] in _ambiguas
            # Un solo ítem sin unitario ya manda la parada entera al campo
            # libre, y eso es lo correcto: un total parcial es más engañoso que
            # ninguno — el conductor no tiene cómo ver que le faltó un renglón.
            _hay_valor = valor_factura is not None and bool(p['items']) and all(
                it.get('valor_unitario') is not None for it in p['items'])
            # **No es `es_contado`.** Toda venta de ruta sale en C02, que no es
            # contado documental pero sí se cobra en la puerta; alimentar el
            # modo con `es_contado` dejaba TODA parada en CREDITO y el
            # conductor no veía el cobro en ninguna. Se deriva del código
            # crudo, que es la única fuente — no de `es_contado`, que ya es una
            # lectura y responde otra pregunta.
            #
            # Desde el 2026-09-24 sale del SNAPSHOT de la tarea
            # (`cond_pago.anotar_en_tarea`): la condición de la FE si se leyó,
            # si no la del pedido, y si no, contado supuesto (se cobra, y la
            # pantalla lo dice). Lo que viaja acá se cachea entero en el
            # teléfono: la parada se confirma offline con esto.
            _cobro = _cpm.anotar_en_tarea(t)
            p['se_cobra_en_puerta'] = _cobro['cobrar']
            p['modo_pago'] = _cpm.modo_pantalla(p['se_cobra_en_puerta'], _hay_valor)
            p['cobro_contraentrega'] = _cobro['cobrar']
            p['dias_credito'] = _cobro['dias']
            p['cond_pago_fe'] = t.cond_pago_fe
            p['clasif_origen'] = _cobro['origen']
            p['cond_pago_difiere'] = _cobro['difiere_del_pedido']
            p['cobro_etiqueta'] = _cpm.etiqueta_conductor(_cobro)

            # ── Dónde queda este cliente, si alguien ya estuvo ─────────────
            #
            # `None` significa **«nadie capturó nada todavía»**; un dict con
            # `lat: null` significa «se capturó y no se pudo elegir un punto».
            # La pantalla los pinta distinto a propósito: el primero se arregla
            # tocando el botón, el segundo mirando por qué. Colapsarlos en un
            # `if (!p.geo)` es la ausencia muda que la Regla 4 persigue.
            #
            # Viaja en el mismo payload que la pantalla ya cachea entero para
            # trabajar offline (`condAbrirParadas`): un botón de navegación que
            # necesita red es un botón que no existe en carretera.
            from app.services import geo_cliente as _geo_c
            _m = _geo_c.maestro_de(t.cliente, t.municipio)
            p['geo'] = _m.to_dict() if _m is not None else None

        # El respaldo del snapshot, sin red: lo que `anotar_en_tarea` escribió
        # arriba. Anotar no puede romper la lista del conductor.
        try:
            db.session.commit()
        except Exception as _e_snap:
            db.session.rollback()
            logger.warning('[RUTAS] no se pudo guardar la clasificación de cobro de la '
                           'ruta %s: %s', ruta_id, _e_snap)

        paradas = sorted(tareas_map.values(), key=lambda x: (x['municipio'], x['cliente']))
        # Ojo: `paradas` está indexado por tarea_id, así que cada entrada es una
        # factura, no una parada física. La parada real es DISTINCT cliente.
        gestionadas = sum(1 for p in paradas if p['recaudo'])

        # Mismo catálogo que usa Liquidación de escritorio (CATALOGO_RETENCIONES
        # en liquidacion_service.py) — una sola fuente, el conductor y el admin
        # ven siempre las mismas cuentas/tasas.
        from app.services.liquidacion_service import CATALOGO_RETENCIONES
        from app.services.liquidacion_service import tope_diferencia_recaudo as _tope_cobro
        from app.services import senales_ruta as _sr_p
        from app.services import devolucion_ruta as _dr_p
        retenciones_disponibles = [
            {'tipo': k, 'nombre': v['nombre'], 'puc': v['puc'], 'tasa': v['tasa']}
            for k, v in CATALOGO_RETENCIONES.items()
        ]

        return {
            'paradas':                 paradas,
            'total_paradas':           len(paradas),
            'facturas_gestionadas':    gestionadas,
            # Alias de transición para PWA en caché — retirar post go-live
            'paradas_gestionadas':     gestionadas,
            'retenciones_disponibles': retenciones_disponibles,
            # Campos NUEVOS y aditivos. `referencias_ambiguas` sin
            # `referencias_valoradas` no se puede leer: "4 ambiguas" es una
            # ruta rota si la base es 5 y ruido si es 300. Van juntos o no van.
            'referencias_valoradas':   _refs_valoradas,
            'referencias_ambiguas':    _refs_ambiguas,
            # Qué formas de pago piden comprobante. Del servidor y no de una
            # lista en el JS (`senales_ruta.requiere_comprobante`), y dentro del
            # payload que la pantalla cachea: tiene que saberlo sin señal.
            'formas_con_comprobante':  [f for f in FormaPago.VALIDOS
                                        if _sr_p.requiere_comprobante(f)],
            # Las que declaran «no entró plata»: la pantalla las quita del
            # select de una parada de contado. Del servidor, no del JS.
            'formas_que_no_cobran':    list(_cp_tabla.FORMAS_QUE_NO_COBRAN),
            # Lo mismo que tolera la guarda del servidor al comparar lo cobrado
            # con el valor de la factura.
            'tolerancia_cobro':        _tope_cobro(),
            # La versión que este servidor sabe exigir: evidencia (2) y
            # contado contraentrega sin crédito en el select (3).
            # Y una PARCIAL que dice qué volvió (4, m045devol).
            'version_formulario':      max(_sr_p.VERSION_FORMULARIO_CON_EVIDENCIA,
                                           _cp_tabla.VERSION_FORMULARIO_CONTADO,
                                           _dr_p.VERSION_FORMULARIO_DEVOLUCION),
        }

    # La unidad que el sync roto inventó en TODO el catálogo. No es una lista
    # de unidades prohibidas: es exactamente el valor que
    # `siesa_sync_service.py:19-22` documenta como escrito por error
    # (`f120_id_unidad_medida_inventario` no existe, `.get()` devolvía el
    # default del modelo) y del que se decidió NO hacer backfill. Mientras no
    # corra un sync completo, un `unidad_medida == 'UND'` sin `unidad_empaque`
    # es indistinguible de "nunca se pobló".
    _UOM_DEFAULT_DEL_SYNC_ROTO = 'UND'

    @staticmethod
    def _uom_declarada(producto) -> str:
        """Unidad que el WMS **declara** para un producto, o `''` si no declara
        ninguna. No es `unidad_empaque or unidad_medida`.

        `unidad_empaque` es un campo que solo se llena a mano o desde
        `f120_id_unidad_empaque`, que sí existe en el contrato: si trae algo,
        es una declaración.

        `unidad_medida` no vale lo mismo. Todo el catálogo nació con `'UND'`
        ahí por un campo mal nombrado en el sync, y no hay backfill. Un `'UND'`
        sin `unidad_empaque` puede ser la unidad real o puede ser el residuo
        del error, y no hay forma de distinguirlos desde la base. Tratarlo como
        declaración es lo que hacía que el desempate del unitario **degradara
        al defecto original justo donde hacía falta**: ganaba la línea UND de
        la factura, que es exactamente la que dividía el precio por el factor
        de empaque (2.425 en vez de 24.250).

        Cualquier otro valor —'PQ', 'CJA', 'KG'— no lo pudo escribir el sync
        roto, así que sí es evidencia y se usa. El día que corra un sync
        completo, `unidad_medida` vuelve a valer para todos y esta excepción
        se puede retirar; hasta entonces retirarla reabre el defecto.
        """
        empaque = (getattr(producto, 'unidad_empaque', '') or '').strip().upper()
        if empaque:
            return empaque
        medida = (getattr(producto, 'unidad_medida', '') or '').strip().upper()
        if medida and medida != RutaService._UOM_DEFAULT_DEL_SYNC_ROTO:
            return medida
        return ''

    @staticmethod
    def _unitarios_por_referencia(lineas, uom_wms: dict) -> tuple:
        """`({referencia: unitario_o_None}, diagnostico)` — el valor neto por
        unidad de cada referencia de la factura, o `None` **declarado** cuando
        no se puede saber cuál es.

        ── Por qué `None` y no "la línea más grande" ──

        Este número viaja a `rutas.js:2199`, donde se multiplica por lo que el
        conductor recibe de vuelta en la puerta del cliente. Un producto de
        doble unidad (PAPELSP6741) sale en la FE como dos líneas con la misma
        `f120_referencia`, una en PQ y otra en UND, y sus unitarios difieren
        por el factor de empaque: 24.250 contra 2.425.

        Elegir "la de mayor cantidad" parece conservador y no lo es. Cuando el
        WMS no declara la unidad del producto, no sabemos **en qué unidad está
        expresada la cantidad que el conductor va a devolver** — `listar_paradas`
        ni siquiera puede rotularla en pantalla, porque `items[].unidad` sale
        del mismo `unidad_empaque` vacío. Un precio por unidad desconocida
        multiplicado por una cantidad de unidad desconocida no es una
        estimación con sesgo: es un número sin dimensión. Y el error no es
        simétrico ni pequeño — es un factor 10 en cualquiera de los dos
        sentidos, sobre plata que se cobra o se deja de cobrar en la calle, sin
        que ningún tablero lo note (`valor_factura` sigue sumando bien).

        Así que lo conservador acá es **no mostrar el número**, no mostrarlo
        sesgado hacia abajo o hacia arriba. Regla 0: ante dato ausente, fallar
        hacia el lado conservador Y DECLARARLO. El camino de salida ya existe y
        ya está probado: sin `valor_unitario` en todos los ítems, `_hay_valor`
        da `False`, `modo_pantalla` devuelve LIBRE y el conductor escribe el
        monto mirando la factura que tiene en la mano — el mismo camino al que
        cae `es_contado = None`. Se pierde una comodidad; no se pierde la
        entrega, y nadie cobra de más ni de menos por un decimal corrido.

        ── Criterios, en orden ──

        1. Las líneas cuya `f470_id_unidad_medida` coincide con la unidad
           declarada del producto (`_uom_declarada`). Es la unidad en la que el
           conductor cuenta lo devuelto.
        2. Si ninguna coincide (o el WMS no declara unidad), se miran TODAS las
           líneas de esa referencia. Si todas dan el mismo unitario no hay nada
           que desambiguar —caso de una sola línea, o de una línea partida en
           dos en la misma unidad— y ese unitario es la respuesta.
        3. Si las candidatas discrepan en el unitario: `None`, contada como
           ambigua.

        ── Por qué no queda ninguna dependencia del orden ──

        El resultado se decide sobre el **conjunto** de unitarios de las
        candidatas, no recorriendo filas y quedándose con la que gane. Un
        auditor midió el defecto anterior: `24250.0` y `2425.0` con las mismas
        dos líneas en orden invertido, porque el `>` estricto sobre el puntaje
        dejaba ganar a la primera fila ante empate. Con un conjunto, invertir
        las filas no puede cambiar nada — y la lista de referencias del
        diagnóstico sale ordenada por la misma razón.

        `diagnostico`: `{'valoradas': N, 'ambiguas': M, 'referencias': [...]}`.
        `N` es la base —referencias de la factura con cantidad > 0—, `M` las
        que no se pudieron desambiguar. Un conteo sin su base no es una
        medición: sin `N`, "3 ambiguas" no distingue una factura rota de una
        operación normal con tres productos de doble unidad.
        """
        por_referencia = {}
        for ln in (lineas or []):
            codigo = str(ln.get('f120_referencia', '')).strip()
            cant = float(ln.get('f470_cant_base', 0) or 0)
            vlr_neto = float(ln.get('f470_vlr_neto', 0) or 0)
            # Cantidad 0 no da unitario y tampoco entra a la base del conteo:
            # inflar el denominador con líneas que nunca se pudieron valorar
            # haría ver mejor la medición justo cuando la factura viene peor.
            if not codigo or cant <= 0:
                continue
            uom_fe = str(ln.get('f470_id_unidad_medida', '') or '').strip().upper()
            por_referencia.setdefault(codigo, []).append(
                (uom_fe, round(vlr_neto / cant, 4)))

        valores, ambiguas = {}, []
        for codigo, filas in por_referencia.items():
            uom_ref = uom_wms.get(codigo) or ''
            candidatas = [f for f in filas if uom_ref and f[0] == uom_ref]
            if not candidatas:
                candidatas = filas
            unitarios = {u for _uom, u in candidatas}
            if len(unitarios) == 1:
                valores[codigo] = unitarios.pop()
            else:
                # Se deja la clave puesta en `None` a propósito, en vez de
                # omitirla: "la factura no trae esta referencia" y "la trae y
                # no sé a qué precio" son cosas distintas, y el consumidor las
                # tiene que poder separar.
                valores[codigo] = None
                ambiguas.append(codigo)

        return valores, {'valoradas': len(por_referencia),
                         'ambiguas': len(ambiguas),
                         'referencias': sorted(ambiguas)}

    @staticmethod
    def _valor_y_cond_pago(tarea, diagnostico: dict = None) -> tuple:
        """`(valor_factura, es_contado, valores_por_referencia, cond_pago_crudo,
        base_gravable, iva, codigo_vendedor)` de la FE real de una tarea.
        Cualquiera puede salir `None`/`{}` si Siesa no responde — nunca
        levanta. Alimenta el toggle Pago Total/Parcial del conductor y el
        recálculo en vivo cuando ajusta cantidades entregadas; si falta el
        dato, el frontend cae al campo libre de siempre (sin bloquear la
        pantalla).

        `base_gravable`/`iva`: suma de `f470_vlr_bruto`/`f470_vlr_imp` de las
        mismas líneas que ya se leen para `valor_factura` — sin consulta
        extra. Existen solo para que la pantalla del conductor pueda mostrar
        el desglose real de la factura (base + IVA = total), no para decidir
        nada — `valor_factura` (el neto) sigue siendo el único valor que
        gobierna el cobro.

        `codigo_vendedor`: `f200_id_vendedor` crudo de la FE (viene en la
        misma respuesta que ya trae `get_rowids_factura`, sin llamada extra
        a Siesa). El caller lo cruza contra `get_vendedor_contacto()` para
        mostrarle al conductor nombre y teléfono del asesor. Puede ser
        `"Generico"` en pedidos donde Siesa no asignó vendedor real — ese
        código no cruza con ningún vendedor real y el frontend simplemente
        no muestra el bloque.

        ⚠️ Esta función tiene tres `return` — uno de ellos ya se rompió antes
        (ver `tests/test_cond_pago.py::TestSalidaAntesDeConsultarSiesa`, "quedó
        con 3 valores en vez de 4") por editar dos y olvidar el tercero.
        Cualquier campo nuevo va en LOS TRES.

        `es_contado`: `True` | `False` | **`None` cuando no se sabe**.

        Hasta el 2026-08-13 un `f430_id_cond_pago` vacío daba `True` —se
        trataba como contado— y `rutas.js` mostraba «Valor a Cobrar». O sea que
        al conductor se le pedía cobrarle a un cliente cuya condición nadie
        conocía. Ahora devuelve `None` y la pantalla cae al campo libre, que es
        lo que ya hacía cuando faltaba el valor de la factura.

        La política de cómo leer el vacío vive en `services/cond_pago.py`, no
        acá: estaba escrita en dos sitios y los dos hacia contado.

        `valores_por_referencia`: `{codigo: valor_neto_unitario}` — el valor
        real de Siesa por unidad de cada línea (`f470_vlr_neto / f470_cant_base`),
        para poder restar exactamente lo que vale una devolución parcial en
        vez de prorratear el total de la factura a ojo.

        **La referencia sola no identifica una línea de factura.** Un producto
        de doble unidad sale en la FE como dos líneas con la misma
        `f120_referencia`, una en PQ y otra en UND (PAPELSP6741 — el mismo caso
        que ya costó el error 244328, ver `despacho_parcial_service.py:102-106`).
        Asignando por referencia sola ganaba **la última**, la de UND, y el
        unitario quedaba dividido por el factor de empaque: 2.425 en vez de
        24.250.

        Lo que lo volvía invisible es la asimetría: `valor_factura` **suma
        bien** (las dos líneas entran a la suma), así que el total se ve
        correcto y nada parece roto. El unitario, en cambio, es lo que
        `listar_paradas` cuelga de cada ítem y `rutas.js:2199` multiplica por
        lo devuelto — **plata mal descontada en la puerta del cliente**, sin
        que ningún tablero lo note.

        La desambiguación arranca por la unidad **declarada** del producto en
        el WMS: la línea cuya `f470_id_unidad_medida` coincide con ella es la
        que el conductor cuenta en la puerta. Cuando esa evidencia no existe,
        el unitario se declara desconocido (`None`) en vez de elegirse — ver
        `_unitarios_por_referencia`, que es donde vive la política entera y el
        porqué. Lo que ya no decide nada es el orden de llegada de las filas.

        `diagnostico`: parámetro de SALIDA opcional. Si se pasa un dict, se
        llena con `{'valoradas': N, 'ambiguas': M, 'referencias': [...]}` de la
        corrida. Es un parámetro y no un quinto elemento de la tupla a
        propósito: la aridad de 4 la desempaquetan `listar_paradas` y tres
        archivos de test, y cambiarla para publicar una medición habría roto
        código sano.
        """
        from app.services.connekta_gateway import connekta
        from app.services.fe_resolver import resolver_fe_o_none

        tipo_fe, consec_fe = resolver_fe_o_none(tarea)
        if not tipo_fe or not consec_fe:
            # Siete elementos, no tres. Este `return` devolvía una tupla
            # corta mientras los callers desempaquetan la larga: una tarea sin
            # FE resoluble reventaba con `ValueError: not enough values to
            # unpack` y se llevaba la lista de paradas ENTERA — el camino que
            # el `None` de `es_contado` existe justamente para no romper.
            # Main arregló el mismo bug por su cuenta (`0b398e8`) y después
            # amplió la tupla a siete; se toma la suya.
            return None, None, {}, None, None, None, None

        valor_factura = None
        base_gravable = None
        iva_factura = None
        valores_por_referencia = {}
        codigo_vendedor = None
        #: `f461_id_cond_pago` de la FE — viene en la misma respuesta, sin
        #: llamada extra. Es la condición que la factura lleva de verdad y
        #: manda sobre la del pedido (`cond_pago.anotar_en_tarea`).
        cond_fe = None
        diag = {'valoradas': 0, 'ambiguas': 0, 'referencias': []}
        # Unidad con la que el WMS maneja cada referencia. Se indexa por las
        # dos formas del código (Siesa e interna) porque el consumidor
        # (`listar_paradas`) busca por `producto.codigo` y la factura viene
        # por `f120_referencia` — cuál de las dos coincide depende del maestro.
        _uom_wms = {}
        for _it in (getattr(tarea, 'items', None) or []):
            _p = getattr(_it, 'producto', None)
            if _p is None:
                continue
            _uom = RutaService._uom_declarada(_p)
            if not _uom:
                continue
            for _ref in ((getattr(_p, 'codigo_siesa', '') or '').strip(),
                         (getattr(_p, 'codigo', '') or '').strip()):
                if _ref:
                    _uom_wms.setdefault(_ref, _uom)
        try:
            lineas = connekta.get_rowids_factura(tipo_fe, consec_fe)
            if lineas:
                # `valor_factura` suma TODAS las líneas y no lo toca nada de lo
                # de abajo: es el dato sano de esta función. Que el unitario de
                # una referencia se declare desconocido no puede alterarlo.
                valor_factura = round(sum(float(ln.get('f470_vlr_neto', 0)) for ln in lineas), 2)
                base_gravable = round(sum(float(ln.get('f470_vlr_bruto', 0)) for ln in lineas), 2)
                iva_factura = round(sum(float(ln.get('f470_vlr_imp', 0)) for ln in lineas), 2)
                # `f200_id_vendedor` es el NIT del vendedor, no su código — nunca
                # cruza con `get_vendedor_contacto()` (que llave por `codigo_vendedor`,
                # ej. '002'). El código real es `f210_codigo_vendedor`. Verificado en
                # vivo 2026-09-02 con PD1447/FE-1444 (vendedor real, no "Generico"):
                # f200_id_vendedor='53051164' (NIT) vs f210_codigo_vendedor='002 '
                # (coincide con el maestro — FIGUEROA ANACONA LEIDA YOANA).
                codigo_vendedor = str(lineas[0].get('f210_codigo_vendedor') or '').strip() or None
                cond_fe = str(lineas[0].get('f461_id_cond_pago') or '').strip() or None
                valores_por_referencia, diag = \
                    RutaService._unitarios_por_referencia(lineas, _uom_wms)
                if diag['ambiguas']:
                    # Con su base: un conteo sin denominador no es una medición.
                    logger.warning(
                        '[RUTAS] unitario no desambiguable en %s de %s referencias '
                        '(tarea %s, FE %s-%s): %s. La parada cae al campo libre.',
                        diag['ambiguas'], diag['valoradas'], tarea.id, tipo_fe,
                        consec_fe, ', '.join(diag['referencias']))
        except Exception as e:
            logger.warning('[RUTAS] valor_factura falló para tarea %s (FE %s-%s): %s',
                            tarea.id, tipo_fe, consec_fe, e)

        if diagnostico is not None:
            diagnostico.update(diag)

        # Se anota igual que la FE. Sin esto, la distribución de valores por
        # parada —el insumo del tope de contado declarado— exige volver a
        # consultar Siesa parada por parada.
        #
        # Anotar no puede romper lo anotado: si el commit falla, el valor ya se
        # devolvió y lo único que se pierde es el ahorro de la próxima consulta.
        if valor_factura is not None:
            try:
                from app.extensions import db as _db
                if tarea.valor_factura is None or \
                        float(tarea.valor_factura) != float(valor_factura):
                    tarea.valor_factura = valor_factura
                    _db.session.commit()
            except Exception as _e_val:
                try:
                    from app.extensions import db as _db2
                    _db2.session.rollback()
                except Exception:
                    pass
                logger.warning('[RUTAS] no se pudo anotar valor_factura en la tarea %s: %s',
                               tarea.id, _e_val)

        # Snapshot de cobro con la condición de la FE (sin red: ya se leyó).
        if cond_fe:
            try:
                from app.extensions import db as _db_fe
                from app.services import cond_pago as _cp_fe
                _cp_fe.anotar_en_tarea(tarea, cond_fe=cond_fe)
                _db_fe.session.commit()
            except Exception as _e_fe:
                try:
                    from app.extensions import db as _db_fe2
                    _db_fe2.session.rollback()
                except Exception:
                    pass
                logger.warning('[RUTAS] no se pudo anotar la condición de la FE en la '
                               'tarea %s: %s', tarea.id, _e_fe)

        es_contado = None
        # El valor CRUDO de Siesa, además del derivado. `es_contado` lo produce
        # esta misma función, así que usarlo para verificar el supuesto que la
        # gobierna no verifica nada: da `None` tanto si la condición falta como
        # si la consulta falló, y `True` tanto con C01 como —antes— con vacío.
        # Con el crudo, «qué condición llevan los pedidos de ruta» y «¿el
        # fallback se disparó?» se responden sin depender de nuestra lectura.
        cond_pago_crudo = None
        # Si ya se anotó, no se vuelve a preguntar. Además de ahorrar una
        # consulta por parada en cada carga de ruta, es lo que permite validar
        # la entrega **sin red**: `confirmar_parada` no puede ir a Siesa.
        # `cobra_en_la_puerta`, no `es_contado_o_none`: la pantalla del
        # conductor pregunta "¿cobro acá?", no "¿es literalmente el código de
        # contado?" — C02 (condición de ruta) también se cobra en la puerta,
        # es el código que el vendedor captura para que la FE se apruebe en
        # Siesa. Ver `cond_pago.cobra_en_la_puerta`.
        if getattr(tarea, 'cond_pago', None) is not None:
            from app.services import cond_pago as _cp0
            return (valor_factura,
                    _cp0.cobro_de_tarea(tarea)['cobrar'],
                    valores_por_referencia,
                    tarea.cond_pago,
                    base_gravable,
                    iva_factura,
                    codigo_vendedor)
        try:
            cabecera = connekta.get_pedido_cabecera(
                tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa)
            from app.services import cond_pago as _cp
            cond_pago_siesa = (cabecera or {}).get('f430_id_cond_pago') or ''
            cond_pago_crudo = cond_pago_siesa
            if not cond_pago_siesa:
                _cp.registrar_ausencia(
                    f'pedido {tarea.tipo_docto_pedido_siesa}-{tarea.consec_docto_pedido_siesa}')
            # Anotar no puede romper lo anotado: si el commit falla, el valor ya
            # se devolvió y solo se pierde el ahorro de la próxima consulta.
            try:
                from app.extensions import db as _db_cp
                tarea.cond_pago = cond_pago_crudo
                _cp.anotar_en_tarea(tarea)
                _db_cp.session.commit()
            except Exception as _e_cp:
                try:
                    from app.extensions import db as _db_cp2
                    _db_cp2.session.rollback()
                except Exception:
                    pass
                logger.warning('[RUTAS] no se pudo anotar cond_pago en la tarea %s: %s',
                               tarea.id, _e_cp)
            es_contado = _cp.cobro_de_tarea(tarea)['cobrar']
        except Exception as e:
            logger.warning('[RUTAS] cond_pago falló para tarea %s: %s', tarea.id, e)

        return (valor_factura, es_contado, valores_por_referencia, cond_pago_crudo,
                base_gravable, iva_factura, codigo_vendedor)

    @staticmethod
    def confirmar_parada(ruta_id: int, tarea_id: int, usuario_id: int, data: dict,
                         motivo_tardia: str = None) -> tuple:
        """Registra entrega y recaudo de una parada. Retorna (recaudo_id, es_edicion).

        **Con la ruta ya ENTREGADA** (P1-4): la cola sin señal del conductor
        podía mandar el cierre aunque una confirmación hubiera fallado, y
        después esa parada no tenía salida —confirmar exigía EN_TRANSITO,
        forzar también, y liquidar exige todas gestionadas—. Una parada tardía
        entra con `motivo_tardia` (obligatorio) y queda FORZAR en la bitácora.
        Nunca sobre una ruta LIQUIDADA.
        """
        _ruta = db.session.get(RutaDespacho, ruta_id)
        if _ruta is None:
            raise LookupError('Ruta no encontrada')
        _tardia = None
        if _ruta.estado == EstadoRutaDespacho.ENTREGADA:
            if (_ruta.estado_financiero or '') == EstadoFinancieroRuta.LIQUIDADA:
                raise ValueError('La ruta ya está liquidada: la parada no se puede '
                                 'confirmar ni corregir desde acá')
            _tardia = motivo_obligatorio(
                motivo_tardia, 'confirmar una parada después del cierre de la ruta')
        elif _ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
            raise ValueError(f'La ruta debe estar EN_TRANSITO, está {_ruta.estado}')
        bultos_tarea = (Bulto.query
                        .filter_by(tarea_id=tarea_id, ruta_despacho_id=ruta_id)
                        .with_for_update()
                        .all())
        if not bultos_tarea:
            raise ValueError('Esta factura no pertenece a la ruta')

        estado_entrega = data.get('estado_entrega', '').upper()
        # `ACEPTADOS_DEL_CONDUCTOR` y no `VALIDOS`: `ENTREGADO_SIN_PAGO` es un
        # estado real pero **no un botón**. Lo deriva el servidor unas líneas
        # más abajo, del motivo del rechazo.
        if estado_entrega not in EstadoEntrega.ACEPTADOS_DEL_CONDUCTOR:
            raise ValueError(
                f'estado_entrega debe ser {", ".join(EstadoEntrega.ACEPTADOS_DEL_CONDUCTOR)}')

        forma_pago = (data.get('forma_pago') or '').upper() or None
        if forma_pago and forma_pago not in FormaPago.VALIDOS:
            raise ValueError(f'forma_pago inválido. Válidos: {", ".join(FormaPago.VALIDOS)}')

        # ── La restricción del diseño: lo que se cobra en la puerta no se
        # convierte en crédito ──
        #
        # «Sobre una parada declarada de contado no se puede registrar forma de
        # pago a crédito» (BK-OPS-01 §3.4). El conductor no tiene facultad de
        # otorgar crédito en la puerta; si el cliente se lleva la mercancía sin
        # pagar, el camino es el motivo tipificado, que deja documento.
        #
        # Contra lo ANOTADO en la tarea, no contra Siesa: esta confirmación
        # **tiene que funcionar sin señal**.
        # Acotada a los estados donde `forma_pago` significa algo. En RECHAZADO
        # no se pide forma de pago —el camino es el motivo tipificado— pero el
        # `<select>` puede conservar un valor de un render anterior, y con él
        # la restricción bloquearía `NO_PAGO_SE_QUEDO`: justo el caso que el
        # motivo existe para canalizar. Prohibir sin dar salida devuelve al
        # conductor al camino de menor resistencia (marcar «cliente cerrado»),
        # que convierte un impago en un faltante de inventario.
        # ── Contado contraentrega: el conductor no otorga crédito ──────────
        #
        # Regla del dueño (2026-09-24): ≤ 15 días de crédito es CONTADO — se
        # cobra al entregar. Sobre una parada así no se registra CREDITO ni
        # EXENTO: si el cliente no pagó, el camino es «No pagó» (la mercancía
        # vuelve) o «No pagó y se quedó» con evidencia — los dos siempre
        # disponibles, en RECHAZADO, que esta guarda no toca (una parada
        # trabada en la calle no la desbloquea nadie).
        #
        # Se juzga contra el SNAPSHOT de la tarea (`cond_pago.cobro_de_tarea`),
        # sin red. Sin clasificación, la política cobra (contado supuesto): la
        # guarda también actúa ahí. Antes, «no sé» dejaba pasar cualquier cosa.
        #
        # Solo al formulario que sabe ofrecer el select sin crédito
        # (`version_formulario >= 3`). Un ítem viejo de la cola no se rechaza
        # por la regla nueva —quedaría trabado para siempre en el teléfono—:
        # conserva la restricción de antes y lo demás llega a la liquidación
        # como `credito_no_autorizado`, que no lo deja pasar sin autorización.
        from app.services import cond_pago as _cp_r
        _tarea_cp = TareaPacking.query.get(tarea_id)
        _cobro_parada = _cp_r.cobro_de_tarea(_tarea_cp)
        _entrega_con_cobro = estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)
        if _entrega_con_cobro and _cobro_parada['cobrar']:
            if _cp_r.formulario_sabe_de_contado(data):
                if _cp_r.forma_no_cobra(forma_pago):
                    raise ValueError(
                        'Esta factura se cobra al entregar (contado contraentrega): no '
                        'se puede registrar como crédito ni exento. Si el cliente no '
                        'pagó, marcá Rechazado → «No pagó» (la mercancía vuelve).')
                if estado_entrega == EstadoEntrega.ENTREGADO:
                    _monto_e = float(data.get('monto_cobrado') or 0)
                    _desc_e = float(data.get('monto_descuento') or 0)
                    _valor_e = (float(_tarea_cp.valor_factura)
                                if _tarea_cp is not None and _tarea_cp.valor_factura is not None
                                else None)
                    from app.services.liquidacion_service import tope_diferencia_recaudo
                    if _monto_e <= 0 or (_valor_e is not None and
                                         _monto_e + _desc_e < _valor_e - tope_diferencia_recaudo()):
                        raise ValueError(
                            'Esta factura se cobra al entregar y el monto no alcanza el '
                            'valor de la factura. Si el cliente no pagó, marcá «No pagó»; '
                            'si pagó una parte, marcá Parcial y ajustá lo entregado.')
            else:
                from app.services.connekta_gateway import connekta as _cx_r
                if _cp_r.regla_anterior_rechaza_credito(
                        forma_pago, _cp_r.codigo_vigente(_tarea_cp),
                        _cx_r.cond_pago_ventas, _cx_r.cond_pago_ruta):
                    raise ValueError(
                        'Este pedido se cobra en la entrega: no se puede registrar '
                        'como crédito. Si el cliente no pagó, marcá Rechazado y elegí '
                        'el motivo — ahí queda registrado si la mercancía volvió o se '
                        'quedó con él.')

        # Validación de campos obligatorios por estado. Entregado y Parcial
        # pasan por el mismo toggle Pago Total/Parcial en el conductor — los
        # dos capturan forma de pago y monto contra el valor real (completo o
        # ajustado por lo devuelto).
        if estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
            if not forma_pago:
                raise ValueError('Forma de pago es obligatoria para registrar una entrega')
        if estado_entrega == EstadoEntrega.RECHAZADO:
            # Motivo TIPIFICADO, no texto libre. RECHAZADO era el estado más
            # barato de los tres —solo pedía prosa— mientras ENTREGADO pedía
            # forma de pago. Con el control «si no paga, no se entrega», el
            # camino de menor resistencia era marcar rechazo, y eso convierte
            # un faltante de inventario en una devolución falsa.
            from app.services import motivos_rechazo as _mr
            if not _mr.valido(data.get('motivo_rechazo')):
                raise ValueError(
                    'Elegí el motivo del rechazo de la lista — el texto libre '
                    'no dice si la mercancía volvió al camión')
            if not (data.get('observaciones') or '').strip():
                raise ValueError('El detalle del rechazo es obligatorio')

            # ── De motivo a estado, UNA vez y acá ──────────────────────────
            #
            # El conductor contesta la pregunta que sabe contestar («¿volvió la
            # mercancía?»). El servidor traduce eso al estado que corresponde.
            #
            # Antes esta traducción no existía y el estado quedaba en
            # RECHAZADO: **cada consumidor tenía que acordarse de mirar el
            # motivo**, y uno se olvidó — la lista de reingreso mandaba a
            # bodega a buscar cajas que nunca volvieron. Traducir en la
            # frontera lo arregla para todos los consumidores a la vez, los de
            # hoy y los que se escriban después.
            if not _mr.retorna_mercancia(data.get('motivo_rechazo')):
                estado_entrega = EstadoEntrega.ENTREGADO_SIN_PAGO

        if estado_entrega == EstadoEntrega.PARCIAL:
            # monto>0 solo aplica si de verdad se cobra en la puerta — un
            # pedido a crédito con devolución parcial no cobra nada ahí (solo
            # genera NC por lo devuelto), forzar monto>0 bloquearía ese caso.
            if not _cp_r.forma_no_cobra(forma_pago):
                monto = float(data.get('monto_cobrado') or 0)
                if monto <= 0:
                    raise ValueError('El monto cobrado debe ser mayor a 0 en una entrega parcial')
            if not (data.get('observaciones') or '').strip():
                raise ValueError('Las observaciones son obligatorias en una entrega parcial')
            # Una PARCIAL dice QUÉ volvió (formulario >= 4). Sin esto la
            # liquidación la convertía en devolución TOTAL.
            from app.services import devolucion_ruta as _dr_v
            _dr_v.validar_parcial_del_conductor(estado_entrega, data)

        # Motivo del descuento (Pago Parcial de contado, ver pantalla del
        # conductor) — no se revalida contra Siesa acá (rompería el flujo
        # offline de esta pantalla); solo se valida que el tipo exista en el
        # catálogo real. El admin en Liquidación es el filtro final antes de
        # tocar Siesa.
        motivo_descuento = (data.get('motivo_descuento') or '').strip() or None
        if motivo_descuento:
            from app.services.liquidacion_service import CATALOGO_RETENCIONES
            if motivo_descuento not in CATALOGO_RETENCIONES:
                raise ValueError(f'motivo_descuento inválido: {motivo_descuento}')
        monto_descuento = float(data.get('monto_descuento') or 0)
        if monto_descuento < 0:
            raise ValueError('monto_descuento no puede ser negativo')

        foto = data.get('foto_entrega', '') or None
        if foto and len(foto) > 2_000_000:
            raise ValueError('Foto demasiado grande. Máximo ~1.5MB.')
        if foto:
            try:
                import base64
                from io import BytesIO
                from PIL import Image
                _foto_raw = foto.split(',', 1)[-1] if ',' in foto else foto
                _img_bytes = base64.b64decode(_foto_raw)
                _img = Image.open(BytesIO(_img_bytes))
                _max_dim = 1200
                if max(_img.size) > _max_dim:
                    _img.thumbnail((_max_dim, _max_dim), Image.LANCZOS)
                _buf = BytesIO()
                _img.convert('RGB').save(_buf, format='JPEG', quality=40, optimize=True)
                foto = base64.b64encode(_buf.getvalue()).decode('ascii')
            except Exception as _e_foto:
                # Se guarda tal como llegó —una foto sin comprimir sigue siendo
                # evidencia— pero ya no en silencio: si esto empieza a pasar,
                # hay un cliente mandando algo que no es una imagen.
                logger.warning('[RUTAS] foto de la parada %s/%s no se pudo comprimir, '
                               'se guarda como llegó: %s', ruta_id, tarea_id, _e_foto)

        # ── Evidencia de las excepciones (`senales_ruta`) ──────────────────
        #
        # La parada normal (efectivo, completa) no pide nada nuevo. Se exige
        # evidencia donde está la plata: el pago bancario y «no pagó y se quedó
        # con la mercancía». Solo al formulario que sabe pedirla
        # (`version_formulario`): un ítem viejo de la cola offline que se
        # rechazara quedaría trabado en el teléfono para siempre. Lo que llega
        # sin evidencia de un formulario viejo NO se rechaza: queda como señal
        # en la liquidación (`senales_ruta.senales_de_recaudo`).
        from app.services import senales_ruta as _sr
        from app.models.geo_entrega import EntregaGeo as _EntregaGeo
        _previo = RecaudoEntrega.query.filter_by(ruta_id=ruta_id, tarea_id=tarea_id).first()
        _exige = _sr.formulario_con_evidencia(data)

        referencia_pago = _sr.limpiar_referencia(data.get('referencia_pago'))
        foto_comprobante = data.get('foto_comprobante') or None
        if foto_comprobante and len(foto_comprobante) > 2_500_000:
            raise ValueError('La foto del comprobante es demasiado grande. Máximo ~1.8MB.')
        _cobra = estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL)
        _monto_cobrado = float(data.get('monto_cobrado') or 0)
        if _exige and _cobra and _sr.requiere_comprobante(forma_pago) and _monto_cobrado > 0:
            if not referencia_pago:
                raise ValueError(
                    'Escribí la referencia del comprobante (al menos los últimos '
                    f'{_sr.MIN_REFERENCIA} dígitos): sin ella el pago no se puede '
                    'cruzar contra el banco')
            if not (foto_comprobante or (_previo and _previo.foto_comprobante)):
                raise ValueError('Tomale una foto al comprobante del pago')

        if _exige and estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO:
            if not (foto or (_previo and _previo.foto_entrega)):
                raise ValueError(
                    'Si el cliente se quedó con la mercancía sin pagar, tomá una foto '
                    '(la mercancía en el local o la fachada)')
            _geo_previo = (_EntregaGeo.query.filter_by(recaudo_id=_previo.id).first()
                           if _previo else None)
            if not (_sr.geo_fue_intentado(data.get('geo')) or _geo_previo is not None):
                raise ValueError(
                    'Tocá «Estoy aquí» para registrar la ubicación antes de confirmar')

        # La hora del teléfono, aparte de la del servidor. Nunca la reemplaza.
        _ts_disp = _sr.leer_ts(data.get('ts_dispositivo'))
        _desfase = _sr.desfase_s(_sr.leer_ts(data.get('ts_envio')), datetime.utcnow())
        _via_cola = data.get('via_cola')
        _via_cola = _via_cola if isinstance(_via_cola, bool) else None

        ids_tarea = {b.id for b in bultos_tarea}
        bultos_rechazados_ids = data.get('bultos_rechazados', [])
        if estado_entrega == EstadoEntrega.RECHAZADO and not bultos_rechazados_ids:
            bultos_rechazados_ids = [b.id for b in bultos_tarea]
        elif estado_entrega == EstadoEntrega.ENTREGADO_SIN_PAGO:
            # Los bultos NO vuelven: físicamente se entregaron. Marcarlos
            # RECHAZADO diría que están en el camión, que es la mentira de
            # inventario que este estado vino a impedir. Lo que falta es el
            # pago, y eso lo dice `estado_entrega`, no el bulto.
            bultos_rechazados_ids = []
        # PARCIAL no EXIGE seleccionar bultos rechazados — la devolución se
        # rastrea por referencia (items_entregados), no por bulto completo, y
        # sin selección los bultos de esta tarea quedan ENTREGADO.
        #
        # Pero si el conductor SÍ marca alguno ("BULTOS CON DEVOLUCIÓN" en la
        # pantalla, opcional, solo visible cuando hay ítems devueltos) ese
        # bulto entra por acá igual que en RECHAZADO — es la misma casilla de
        # `bultos_rechazados`, y el bloque de abajo no distingue el estado.
        # No es una contradicción: a nivel de bulto solo existe ENTREGADO/
        # RECHAZADO, nunca "parcial", y la caja física no se puede separar en
        # la puerta — marcarla dice «esta caja concreta vuelve completa a
        # bodega para que ahí la abran y cuenten», no «todo lo que traía se
        # devolvió» (eso lo sigue definiendo `items_entregados`). Entra a la
        # cola de reingreso (`bultos_rechazados()`) igual que un RECHAZADO
        # total.

        ahora = datetime.utcnow()
        ids_rechazados_set = set(bultos_rechazados_ids)

        # Lo que recepción ya recibió o contó no lo cambia una reconfirmación
        # desde la calle (m045devol). Observaciones y foto sí.
        from app.services import devolucion_ruta as _dr
        _dr.verificar_reconfirmacion(_previo, estado_entrega, data, bultos_tarea)

        for b in bultos_tarea:
            if b.estado in (EstadoBulto.RETORNADO, EstadoBulto.FALTANTE):
                # Ya pasó por recepción: su estado es el dato físico.
                continue
            if b.id in ids_rechazados_set:
                b.estado = EstadoBulto.RECHAZADO
                b.motivo_rechazo = data.get('observaciones', 'Rechazado en entrega')[:100]
                b.fecha_entrega = ahora
            else:
                b.estado = EstadoBulto.ENTREGADO
                b.fecha_entrega = ahora

        recaudo = RecaudoEntrega.query.filter_by(ruta_id=ruta_id, tarea_id=tarea_id).first()
        es_edicion = recaudo is not None

        # ── La plata que ya viajó al ERP no se reescribe acá ────────────────
        # Re-confirmar una parada es una función deseada (un dedazo se
        # corrige), pero **el recibo de caja se arma desde `monto_cobrado`**:
        # si el campo se mueve después de que el RC salió, el WMS y Siesa
        # quedan diciendo cifras distintas y ningún reintento lo reconcilia.
        #
        # Se congela con `siesa_rc_triggered`, que es la bandera de PRE-envío
        # (Regla 6) y se revierte sola si el POST falla. Congelar recién con
        # el job COMPLETADO dejaría abierta la ventana en que el POST está en
        # vuelo — que es exactamente cuando la divergencia se vuelve
        # irrecuperable. Regla 0: el lado conservador.
        #
        # `monto_descuento` entra en el congelamiento aunque no viaje en el
        # RC: la reconciliación lo resta para decidir si hubo cobro
        # incompleto, así que dejarlo abierto permitiría **tapar un faltante
        # con un descuento escrito después**.
        #
        # Todo lo demás —observaciones, foto, motivo de rechazo— sigue
        # editable: nada de eso cambia una cifra que Siesa ya tiene.
        #
        # Desde el 2026-09-25 se congela **al encolar** el recibo (o una
        # retención), no al enviarlo: `puede_editar_cobro` (P1-3). Entre
        # encolar y el POST, el monto y el estado se reescribían y el RC salía
        # con la cifra vieja. Y congela también el estado y la forma de pago:
        # una parada ENTREGADA con su RC en cola que pasa a RECHAZADA dejaba un
        # recibo por plata que ya no se declara.
        from app.services import politica_cobro as _pc_cp
        if es_edicion and not _pc_cp.puede_editar_cobro(recaudo):
            _nuevo_monto = float(data.get('monto_cobrado', 0) or 0)
            _cambios = []
            if abs(_nuevo_monto - float(recaudo.monto_cobrado or 0)) > 0.01:
                _cambios.append(
                    f'monto_cobrado ({recaudo.monto_cobrado} → {_nuevo_monto})')
            if abs(monto_descuento - float(recaudo.monto_descuento or 0)) > 0.01:
                _cambios.append(
                    f'monto_descuento ({recaudo.monto_descuento} → {monto_descuento})')
            if (motivo_descuento or None) != (recaudo.motivo_descuento or None):
                _cambios.append(
                    f'motivo_descuento ({recaudo.motivo_descuento} → {motivo_descuento})')
            if estado_entrega != recaudo.estado_entrega:
                _cambios.append(
                    f'estado_entrega ({recaudo.estado_entrega} → {estado_entrega})')
            _fp_nueva = forma_pago_de(estado_entrega, forma_pago)
            if (_fp_nueva or None) != (forma_pago_de(recaudo.estado_entrega,
                                                      recaudo.forma_pago) or None):
                _cambios.append(f'forma_pago ({recaudo.forma_pago} → {_fp_nueva})')
            if _cambios:
                raise ValueError(
                    f'El cobro de esta parada ya no se puede cambiar: '
                    f'{_pc_cp.motivo_cobro_congelado(recaudo)}. '
                    f'{" y ".join(_cambios)} no se puede cambiar desde el WMS. '
                    'Una corrección de un valor ya contabilizado se hace con '
                    'nota crédito. Las observaciones y la foto sí se pueden '
                    'editar.')

        # Re-confirmar pisa montos, estado y `fecha_confirmacion`: lo que
        # decía antes queda en la bitácora (EDITAR), no solo quién editó.
        _CAMPOS_PARADA = ('estado_entrega', 'forma_pago', 'monto_cobrado',
                          'cobro_contraentrega',
                          'monto_descuento', 'motivo_descuento', 'motivo_rechazo',
                          'referencia_pago',
                          'observaciones', 'bultos_rechazados_ids',
                          'items_entregados', 'fecha_confirmacion',
                          'confirmado_por', 'editado_por',
                          # La hora del teléfono de la confirmación ANTERIOR:
                          # re-confirmar la pisa, y sin esto se perdía (QA e2e
                          # 2026-09-24). Queda en el EDITAR.
                          'ts_dispositivo', 'ts_desfase_s', 'via_cola')
        antes_parada = foto_fila(recaudo, list(_CAMPOS_PARADA)) if recaudo else None
        # Lo que una re-confirmación pisa aunque no cambie nada. Si el reenvío
        # resulta idéntico (la cola sin señal manda dos veces lo mismo), se
        # restituye y no hay EDITAR.
        _volatiles_previos = ({k: getattr(recaudo, k) for k in _VOLATILES_PARADA}
                              if recaudo else None)

        if not recaudo:
            recaudo = RecaudoEntrega(
                ruta_id=ruta_id,
                tarea_id=tarea_id,
                fecha_creacion=ahora,
                confirmado_por=usuario_id,
            )
            db.session.add(recaudo)
        else:
            recaudo.editado_por = usuario_id
            recaudo.editado_en  = ahora

        recaudo.estado_entrega        = estado_entrega
        # Sin cobro (rechazo, «no pagó y se quedó») no hay forma de pago,
        # aunque el select traiga la de antes.
        recaudo.forma_pago            = forma_pago_de(estado_entrega, forma_pago)
        # La clasificación con la que se juzgó esta confirmación, CONGELADA:
        # la liquidación la lee de acá aunque la tabla cambie después. Solo si
        # era LEÍDA (MAESTRO): un supuesto no se congela —queda NULL,
        # declarado— para que la condición real, si aparece después (la FE se
        # lee al listar), decida.
        recaudo.cobro_contraentrega   = (bool(_cobro_parada['cobrar'])
                                         if _cobro_parada['origen'] == _cp_r.MAESTRO
                                         else None)
        # Lo que el conductor tenía enfrente, no solo lo que eligió. Se acepta
        # del cliente porque el modo depende de datos de Siesa que la parada ya
        # resolvió; recalcularlo acá exigiría volver a consultar y la
        # confirmación tiene que poder funcionar offline.
        _modo = (data.get('modo_pantalla') or '').upper() or None
        if _modo in ('CREDITO', 'DINAMICO', 'LIBRE'):
            recaudo.modo_pantalla = _modo
        from app.services import motivos_rechazo as _mr2
        _mot = (data.get('motivo_rechazo') or '').strip().upper() or None
        recaudo.motivo_rechazo = _mot if _mr2.valido(_mot) else None
        recaudo.monto_cobrado         = data.get('monto_cobrado', 0) or 0
        recaudo.motivo_descuento      = motivo_descuento
        recaudo.monto_descuento       = monto_descuento
        recaudo.observaciones         = data.get('observaciones', '') or None
        # Una edición sin foto nueva CONSERVA la anterior. Antes la borraba: la
        # pantalla decía «Foto guardada — toma una nueva para reemplazarla» y
        # corregir un dedazo en el monto dejaba la parada sin evidencia.
        if foto or not es_edicion:
            recaudo.foto_entrega      = foto
        recaudo.bultos_rechazados_ids = list(ids_rechazados_set & ids_tarea)
        recaudo.fecha_confirmacion    = ahora
        # La referencia se guarda solo donde significa algo: si la parada deja
        # de ser un cobro bancario, una referencia vieja sería un comprobante
        # de un pago que ya no se declara.
        if _cobra and _sr.requiere_comprobante(forma_pago):
            if referencia_pago or not es_edicion:
                recaudo.referencia_pago = referencia_pago
            if foto_comprobante:
                recaudo.foto_comprobante = foto_comprobante
        else:
            recaudo.referencia_pago = None
        recaudo.ts_dispositivo        = _ts_disp
        recaudo.ts_desfase_s          = _desfase
        recaudo.via_cola              = _via_cola

        # Detalle de referencias para entrega PARCIAL
        items_raw = data.get('items_entregados') or []
        if estado_entrega == EstadoEntrega.PARCIAL and items_raw:
            items_limpios = []
            for it in items_raw:
                pedido    = int(it.get('cantidad_pedida', 0))
                entregado = max(0, min(int(it.get('cantidad_entregada', pedido)), pedido))
                items_limpios.append({
                    'codigo':             str(it.get('codigo', '')),
                    'nombre':             str(it.get('nombre', '')),
                    'unidad':             str(it.get('unidad', 'und')),
                    'cantidad_pedida':    pedido,
                    'cantidad_entregada': entregado,
                    'cantidad_devuelta':  pedido - entregado,
                })
            recaudo.items_entregados = items_limpios
        elif estado_entrega == EstadoEntrega.ENTREGADO:
            recaudo.items_entregados = None

        # ── La devolución nace ACÁ, con el rechazo (m045devol) ──────────────
        #
        # Antes nacía al liquidar: entre que el camión volvía y alguien
        # liquidaba, la mercancía no existía en el WMS. Ahora una RECHAZADA o
        # PARCIAL deja su devolución EN_CAMION con lo declarado, y recepción la
        # tiene en «Llegó el camión». Sin red (la parada se confirma offline).
        #
        # En un SAVEPOINT: la entrega no se traba por la devolución. Si armarla
        # falla, la parada se confirma igual, el error queda en el log y el
        # invariante DEV-06 la muestra; la liquidación la asegura después con
        # la misma función.
        db.session.flush()
        try:
            with db.session.begin_nested():
                _dr.sincronizar_con_parada(recaudo, usuario_id)
        except Exception as _e_dev:
            logger.error('[RUTAS] parada %s/%s: no se pudo armar la devolución: %s',
                         ruta_id, tarea_id, _e_dev)

        if es_edicion:
            despues_parada = foto_fila(recaudo, list(_CAMPOS_PARADA))
            # Un cambio es de VALOR, no de representación: 35700.00 (Decimal
            # guardado) y 35700.0 (float recibido) son el mismo monto. Y la
            # hora del teléfono y la del servidor cambian en cada envío: por sí
            # solas no son una edición.
            _cambios = [k for k in _CAMPOS_PARADA
                        if k not in _VOLATILES_PARADA
                        and not _mismo_valor(antes_parada.get(k), despues_parada.get(k))]
            if not _cambios:
                # Reenvío idéntico (la cola del conductor manda dos veces lo
                # mismo): no es una edición. Se deja la parada como estaba.
                for _k, _v in (_volatiles_previos or {}).items():
                    setattr(recaudo, _k, _v)
            else:
                # Con un cambio real, la hora anterior del teléfono también
                # queda escrita (QA e2e 2026-09-24: re-confirmar la pisaba).
                _reg = _cambios + [k for k in _CAMPOS_PARADA
                                   if k in _VOLATILES_PARADA
                                   and k not in ('fecha_confirmacion', 'editado_por')
                                   and antes_parada.get(k) != despues_parada.get(k)]
                _reg += ['fecha_confirmacion', 'editado_por']
                registrar_accion(
                    'EDITAR', recaudo, usuario_id=usuario_id,
                    entidad_codigo=(bultos_tarea[0].tarea.numero_pedido_siesa
                                    if bultos_tarea[0].tarea else None),
                    motivo=(data.get('motivo_edicion') or None),
                    antes={k: antes_parada[k] for k in _reg},
                    despues={k: despues_parada[k] for k in _reg})

        if _tardia:
            registrar_accion(
                'FORZAR', recaudo, usuario_id=usuario_id, motivo=_tardia,
                entidad_codigo=(bultos_tarea[0].tarea.numero_pedido_siesa
                                if bultos_tarea[0].tarea else None),
                despues={'forzado': FORZADO_PARADA_TARDIA,
                         'ruta_id': ruta_id, 'estado_ruta': _ruta.estado,
                         'estado_entrega': recaudo.estado_entrega})

        db.session.commit()

        # ── La coordenada, DESPUÉS del commit y en su propia transacción ────
        #
        # El orden no es casual y es la decisión de diseño de todo este bloque:
        # **la entrega no se traba por la geografía**. Si la captura entrara en
        # la misma transacción, un CHECK que rechaza una coordenada rara —un
        # cliente viejo mandando 0,0, una precisión negativa— haría fallar el
        # `commit` de la entrega entera, y una parada trabada en la calle no la
        # desbloquea nadie. Es el mismo criterio que esta función ya aplica con
        # la condición de pago que no se alcanzó a anotar (Regla 0: no saber no
        # bloquea).
        #
        # El costo del orden inverso es que un fallo acá pierde UNA captura de
        # las muchas que va a tener ese cliente. Se registra como advertencia
        # para que se pueda contar si empieza a pasar seguido.
        try:
            from app.services import geo_cliente as _geo
            _t_geo = db.session.get(TareaPacking, tarea_id)
            # La distancia al cliente se mide ANTES de que esta captura vote en
            # el maestro: si no, un «cliente cerrado» registrado a 2 km movería
            # el punto hacia sí mismo y se mediría contra su propia sombra.
            # Solo en la primera captura (la geografía de una parada no se
            # pisa al re-confirmar — ver `registrar_captura`).
            if recaudo.distancia_cliente_m is None:
                _dist = _sr.distancia_al_maestro(
                    data.get('geo'),
                    _geo.maestro_de(getattr(_t_geo, 'cliente', None),
                                    getattr(_t_geo, 'municipio', None)))
                if _dist is not None:
                    recaudo.distancia_cliente_m = _dist
            _pos_ts = (_sr.leer_ts(data['geo'].get('pos_ts'))
                       if isinstance(data.get('geo'), dict) else None)
            _res_geo = _geo.registrar_captura(
                recaudo.id,
                getattr(_t_geo, 'cliente', None),
                getattr(_t_geo, 'municipio', None),
                data.get('geo'),
                ahora=ahora,
                ts_dispositivo=_ts_disp,
                pos_ts_dispositivo=_pos_ts)
            if _res_geo not in ('no_vino', 'ya_capturada') or db.session.dirty:
                db.session.commit()
        except Exception as _e_geo:
            try:
                db.session.rollback()
            except Exception:
                pass
            logger.warning('[GEO] no se pudo registrar la captura de la parada '
                           '%s/%s: %s', ruta_id, tarea_id, _e_geo)

        return recaudo.id, es_edicion

    @staticmethod
    def planilla_ruta(id: int) -> dict:
        ruta = (RutaDespacho.query
                .options(
                    _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                    _sl(RutaDespacho.recaudos),
                )
                .get(id))
        if not ruta:
            raise LookupError('Ruta no encontrada')

        from app.services import politica_cobro as _pc_pl
        tareas = ruta.tareas_unicas()
        recaudos_map = {r.tarea_id: r for r in ruta.recaudos}
        _rc_llegaron = _pc_pl.rc_llegaron(ruta.recaudos)
        paradas = []
        totales = {'EFECTIVO': 0, 'TRANSFERENCIA': 0, 'TARJETA': 0, 'CHEQUE': 0,
                   'CREDITO': 0, 'EXENTO': 0}
        sin_gestionar = 0

        for t in tareas:
            r = recaudos_map.get(t.id)
            bultos_t = [b for b in ruta.bultos if b.tarea_id == t.id]
            ids_rechazados = set(r.bultos_rechazados_ids or []) if r else set()
            parada = {
                'tarea_id':           t.id,
                'numero_pedido':      t.numero_pedido_siesa,
                'cliente':            t.cliente or '',
                'municipio':          t.municipio or '',
                'bultos_total':       len(bultos_t),
                'bultos_entregados':  sum(1 for b in bultos_t if b.estado == EstadoBulto.ENTREGADO),
                'bultos_rechazados':  sum(1 for b in bultos_t if b.estado == EstadoBulto.RECHAZADO),
                'bultos_detalle':     [
                    {'id': b.id, 'codigo_barras': b.codigo_barras, 'tipo': b.tipo,
                     'numero': b.numero, 'total': b.total, 'rechazado': b.id in ids_rechazados}
                    for b in bultos_t
                ],
                'recaudo':            r.to_dict(include_foto=True) if r else None,
                # Qué documento de esta parada falta mandar a Siesa, según la
                # política (`politica_cobro.documentos_pendientes`): el botón
                # «Enviar a Siesa» no lo recalcula en el teléfono. Una
                # devolución contada en cero no va a tener NC.
                'pendiente_siesa':    (_pc_pl.documentos_pendientes(r, t) if r else []),
                # El recibo, con señal positiva (no la bandera de pre-envío).
                'rc_llego':           bool(r and r.id in _rc_llegaron),
            }
            paradas.append(parada)
            if r:
                fp = (r.forma_pago or '').upper()
                # Mismo criterio que `liquidacion_dashboard` (rutas.py): un
                # `==` fijo contra `'TRANSFERENCIA'` dejaba de bucketizar
                # cualquier medio por banco (`TRANSFERENCIA_BANCOLOMBIA_AH`,
                # etc.) — el monto seguía sumando a `total_recaudado()`
                # (agnóstico de forma_pago), solo desaparecía de este
                # desglose por medio.
                if fp.startswith('TRANSFERENCIA') or fp == 'CONSIGNACION':
                    totales['TRANSFERENCIA'] += float(r.monto_cobrado or 0)
                elif fp in totales:
                    totales[fp] += float(r.monto_cobrado or 0)
            else:
                sin_gestionar += 1

        return {
            'ruta':              ruta.to_dict(),
            'paradas':           sorted(paradas, key=lambda x: (x['municipio'], x['cliente'])),
            'total_paradas':     len(paradas),
            'sin_gestionar':     sin_gestionar,
            'total_recaudado':   ruta.total_recaudado(),
            'totales_por_forma': totales,
            'estado_financiero': ruta.estado_financiero or EstadoFinancieroRuta.PENDIENTE,
        }

    @staticmethod
    def _marcar_liquidada(ruta, usuario_id: int = None, motivo: str = None) -> None:
        """La ruta pasa a LIQUIDADA **con fecha y autor**.

        Única función que escribe `EstadoFinancieroRuta.LIQUIDADA`: antes lo
        hacían dos sitios y ninguno guardaba cuándo ni quién. Una liquidación
        que se repite no pisa la primera (ni la fecha ni el autor).
        """
        antes = foto_fila(ruta, ['estado_financiero', 'liquidada_en', 'liquidada_por_id'])
        ruta.estado_financiero = EstadoFinancieroRuta.LIQUIDADA
        if not ruta.liquidada_en:
            ruta.liquidada_en = datetime.utcnow()
            ruta.liquidada_por_id = usuario_id
        registrar_accion(
            'LIQUIDAR', ruta, usuario_id=usuario_id, motivo=motivo,
            entidad_codigo=f'RUTA-{ruta.id}',
            antes=antes,
            despues=foto_fila(ruta, ['estado_financiero', 'liquidada_en', 'liquidada_por_id']))

    @staticmethod
    def liquidar_ruta(id: int, usuario_id: int = None,
                      motivo_devoluciones: str = None) -> dict:
        """La ruta pasa a LIQUIDADA.

        **No con mercancía sin contar** (m045devol): si una parada
        RECHAZADA/PARCIAL tiene su devolución sin contar (o no la tiene), la
        ruta no se liquida — la regla del dueño es que lo que vuelve entre al
        inventario, y liquidar era el momento en que eso se olvidaba. Se puede
        FORZAR con `motivo_devoluciones` (bitácora FORZAR con la lista): la
        devolución sigue viva en recepción y en los avisos.
        """
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado not in (EstadoRutaDespacho.EN_TRANSITO, EstadoRutaDespacho.ENTREGADA):
            raise ValueError(f'No se puede liquidar una ruta en estado {ruta.estado}')

        tareas = ruta.tareas_unicas()
        gestionadas = RecaudoEntrega.query.filter_by(ruta_id=id).count()
        sin_gestionar = len(tareas) - gestionadas
        if sin_gestionar > 0:
            raise ValueError(
                f'Faltan {sin_gestionar} parada{"s" if sin_gestionar != 1 else ""} por gestionar antes de liquidar.'
            )

        # Contado que salió sin plata y sin autorización: la ruta no se da por
        # liquidada. Cobrarlo, o «autorizar como crédito» con razón.
        from app.services import cond_pago as _cp_lq
        _sin_aut = [r for r in RecaudoEntrega.query.filter_by(ruta_id=id).all()
                    if _cp_lq.credito_no_autorizado(r)]
        if _sin_aut:
            _peds = ', '.join((r.tarea.numero_pedido_siesa if r.tarea else f'tarea {r.tarea_id}')
                              or f'tarea {r.tarea_id}' for r in _sin_aut[:10])
            raise ValueError(
                f'credito_no_autorizado: {len(_sin_aut)} parada'
                f'{"s" if len(_sin_aut) != 1 else ""} de contado contraentrega '
                f'sin plata y sin autorización ({_peds}). Cobralas o autorizalas como '
                f'crédito con una razón antes de liquidar.')

        from app.services import devolucion_ruta as _dr_lq
        _sin_contar = _dr_lq.pendientes_de_conteo(id)
        if _sin_contar:
            if not (motivo_devoluciones or '').strip():
                _peds = ', '.join(str(p['pedido'] or f'tarea {p["tarea_id"]}')
                                  for p in _sin_contar[:10])
                raise ValueError(
                    f'devoluciones_sin_contar: {len(_sin_contar)} parada'
                    f'{"s" if len(_sin_contar) != 1 else ""} rechazada/parcial con la '
                    f'mercancía sin contar en bodega ({_peds}). Recepción la cuenta en '
                    f'«Llegó el camión»; si hay que liquidar igual, forzá con un motivo.')
            motivo_devoluciones = motivo_obligatorio(
                motivo_devoluciones, 'liquidar con devoluciones sin contar')
            registrar_accion(
                'FORZAR', ruta, usuario_id=usuario_id, motivo=motivo_devoluciones,
                entidad_codigo=f'RUTA-{ruta.id}',
                despues={'forzado': FORZADO_LIQUIDACION_SIN_CONTAR,
                         'liquidada_con_devoluciones_sin_contar': _sin_contar})

        RutaService._marcar_liquidada(
            ruta, usuario_id,
            motivo=(f'Con devoluciones sin contar: {motivo_devoluciones}'
                    if _sin_contar else None))

        # Las devoluciones ya nacieron al confirmar cada parada; esto las
        # ENCUENTRA y solo crea (con la función única) las de paradas viejas.
        from app.services.liquidacion_service import LiquidacionService
        resumen_devoluciones = LiquidacionService.crear_devoluciones_pendientes_ruta(id)

        db.session.commit()
        return {
            'ok':              True,
            'total_recaudado': ruta.total_recaudado(),
            'ruta':            ruta.to_dict(),
            'devoluciones_pendientes_creadas': resumen_devoluciones['creadas'],
        }

    @staticmethod
    def forzar_cierre_ruta(id: int, admin_id: int, motivo: str = None) -> dict:
        """Cierra una ruta dando por rechazadas las paradas sin gestionar.

        Motivo obligatorio: es un FORZAR que decide por el conductor. Antes
        pisaba `fecha_cierre` (que es cuándo SALIÓ la ruta) con la hora del
        forzado y dejaba `fecha_entregada` en blanco; ahora la salida se
        conserva y el cierre queda en `fecha_entregada`.

        **Deja la ruta ENTREGADA, no LIQUIDADA** (P1-5, 2026-09-25). Antes la
        marcaba liquidada saltándose las guardas de `liquidar_ruta`
        (`credito_no_autorizado`, `devoluciones_sin_contar`): el cierre forzado
        era una puerta trasera a la liquidación. Solo `liquidar_ruta` llama a
        `_marcar_liquidada` (trinquete AST). Sirve también sobre una ruta ya
        ENTREGADA con paradas sin gestionar (P1-4): la salida que no tenía.
        """
        motivo = motivo_obligatorio(motivo, 'forzar el cierre de una ruta')
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado not in (EstadoRutaDespacho.EN_TRANSITO, EstadoRutaDespacho.ENTREGADA):
            raise ValueError(f'La ruta debe estar EN_TRANSITO o ENTREGADA para forzar cierre '
                             f'(estado: {ruta.estado})')
        if (ruta.estado_financiero or '') == EstadoFinancieroRuta.LIQUIDADA:
            raise ValueError('La ruta ya está liquidada')
        antes_ruta = foto_fila(ruta, ['estado', 'estado_financiero', 'fecha_cierre',
                                 'fecha_entregada'])

        tareas = ruta.tareas_unicas()
        recaudos_existentes = {r.tarea_id for r in RecaudoEntrega.query.filter_by(ruta_id=id).all()}
        pendientes = [t for t in tareas if t.id not in recaudos_existentes]
        ahora = datetime.utcnow()

        bultos_por_tarea: dict = {}
        for b in Bulto.query.filter_by(ruta_despacho_id=id).all():
            bultos_por_tarea.setdefault(b.tarea_id, []).append(b)

        auto_cerradas = 0
        for tarea in pendientes:
            for b in bultos_por_tarea.get(tarea.id, []):
                b.estado = EstadoBulto.RECHAZADO
                b.motivo_rechazo = 'Cierre forzado por admin'
                b.fecha_entrega = ahora
            _rec_forzado = RecaudoEntrega(
                ruta_id=id,
                tarea_id=tarea.id,
                estado_entrega=EstadoEntrega.RECHAZADO,
                forma_pago=None,
                monto_cobrado=0,
                observaciones='Cierre forzado por administrador — parada no gestionada',
                confirmado_por=admin_id,
                fecha_creacion=ahora,
            )
            db.session.add(_rec_forzado)
            db.session.flush()
            # Se da por rechazada: la mercancía "vuelve". Su devolución nace
            # acá, EN_CAMION, con la misma función que la de una parada
            # confirmada; recepción dirá si de verdad volvió (o FALTANTE).
            from app.services import devolucion_ruta as _dr_fz
            _dr_fz.sincronizar_con_parada(_rec_forzado, admin_id)
            auto_cerradas += 1

        ruta.estado = EstadoRutaDespacho.ENTREGADA
        if not ruta.fecha_cierre:
            ruta.fecha_cierre = ahora
        ruta.fecha_entregada = ruta.fecha_entregada or ahora
        registrar_accion(
            'FORZAR', ruta, usuario_id=admin_id, motivo=motivo,
            entidad_codigo=f'RUTA-{ruta.id}',
            antes=antes_ruta,
            despues={'forzado': FORZADO_CIERRE_RUTA,
                     **foto_fila(ruta, ['estado', 'fecha_cierre', 'fecha_entregada']),
                     'paradas_auto_rechazadas': [t.id for t in pendientes]})
        _ruta_id = ruta.id
        db.session.flush()

        from app.services.liquidacion_service import LiquidacionService
        LiquidacionService.crear_devoluciones_pendientes_ruta(_ruta_id)

        db.session.commit()

        ruta = (RutaDespacho.query
                .options(
                    _jl(RutaDespacho.conductor),
                    _jl(RutaDespacho.vehiculo),
                    _jl(RutaDespacho.ruta_maestra),
                )
                .get(_ruta_id))
        return {
            'ok': True,
            'paradas_auto_cerradas': auto_cerradas,
            'mensaje': (f'Ruta cerrada. {auto_cerradas} parada(s) registradas como rechazadas '
                        f'automáticamente. Falta liquidarla.'),
            'ruta': ruta.to_dict(),
        }

    # ── Auditoría ────────────────────────────────────────────────────

    @staticmethod
    def bultos_rechazados(page: int, limit: int) -> dict:
        """La lista de trabajo para reingresar mercancía — **sin la que no volvió**.

        Un bulto queda `RECHAZADO` tanto si el cliente lo devolvió como si se
        lo quedó sin pagar (`NO_PAGO_SE_QUEDO`). Los dos casos entraban a la
        misma lista, y alguien iba a ir a buscar cajas que no están en el
        camión.

        Es el defecto que el motivo tipificado vino a impedir y que quedó a
        medias: el flag `retorna` se construyó y no se cableó a la ruta física.
        Con el control «si no paga completo, no se entrega» esto pasa de raro a
        cotidiano.

        Los que no volvieron **no se ocultan**: van en su propio bloque. Uno es
        «andá a buscarlos»; el otro es «estos no están y alguien tiene que
        responder por ellos». Filtrarlos en silencio convertiría una pérdida de
        inventario en una fila que desaparece.
        """
        from app.models.recaudo_entrega import RecaudoEntrega

        # Las paradas donde la mercancía se quedó con el cliente. Se consulta
        # por **estado** y no por motivo: desde el 2026-08-13 eso es
        # `ENTREGADO_SIN_PAGO`, un estado propio. Preguntarle al motivo era
        # justamente lo que había que recordar hacer en cada consumidor.
        sin_retorno = {
            (r.ruta_id, r.tarea_id)
            for r in RecaudoEntrega.query
            .filter(RecaudoEntrega.estado_entrega ==
                    EstadoEntrega.ENTREGADO_SIN_PAGO).all()
        }

        # Los bultos de esas paradas ya NO se marcan `RECHAZADO` —no volvieron,
        # así que quedan `ENTREGADO`—, pero los históricos sí lo están. Se
        # traen los dos: el bloque lo define la parada, no el bulto.
        q = (Bulto.query
             .options(_sl(Bulto.tarea))
             .filter(db.or_(
                 Bulto.estado == EstadoBulto.RECHAZADO,
                 db.tuple_(Bulto.ruta_despacho_id, Bulto.tarea_id).in_(
                     list(sin_retorno)) if sin_retorno else db.false()))
             .order_by(Bulto.fecha_entrega.desc()))
        todos = q.all()

        def _quedo_con_cliente(b):
            return (b.ruta_despacho_id, b.tarea_id) in sin_retorno

        retornados = [b for b in todos if not _quedo_con_cliente(b)]
        no_retornados = [b for b in todos if _quedo_con_cliente(b)]

        total = len(retornados)
        pagina = retornados[(page - 1) * limit:(page - 1) * limit + limit]
        return {
            'bultos': [b.to_dict() for b in pagina],
            'total':  total,
            'page':   page,
            'pages':  (total + limit - 1) // limit,
            # Mercancía que salió y no volvió. NO es trabajo de bodega: es una
            # pérdida que alguien tiene que cerrar contablemente.
            'no_retornados': {
                'total': len(no_retornados),
                'bultos': [b.to_dict() for b in no_retornados[:limit]],
                'nota': ('Se quedaron con el cliente sin pago. No están en el '
                         'camión: no hay nada que reingresar, hay algo que '
                         'cobrar o que dar de baja.'),
            },
        }

    @staticmethod
    def usuarios_conductores() -> list:
        from app.models.usuario import Usuario
        usuarios = (Usuario.query
                    .filter_by(rol='conductor', activo=True)
                    .order_by(Usuario.nombre)
                    .all())
        return [{'id': u.id, 'nombre': u.nombre, 'email': u.email} for u in usuarios]
