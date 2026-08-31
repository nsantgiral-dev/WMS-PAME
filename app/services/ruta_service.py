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
from app.models.recaudo_entrega import RecaudoEntrega
from app.models.vehiculo import Vehiculo
from app.models.ruta_maestra import RutaMaestra, RutaMaestraParada
from app.models.ruta_despacho import RutaDespacho, EstadoRutaDespacho, EstadoFinancieroRuta
from app.utils.fecha import dia_operativo as _dia_operativo

logger = logging.getLogger(__name__)


class ConflictError(ValueError):
    """Señala un conflicto de unicidad (HTTP 409)."""


# `EstadoEntrega` vivía definida acá **y** en `models/recaudo_entrega.py`, con
# los mismos valores y distinto nombre de tupla. Se importa la del modelo: un
# estado agregado en un solo lado era cuestión de tiempo.
from app.models.recaudo_entrega import EstadoEntrega  # noqa: E402,F401


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
        email = (email or '').strip().lower()
        if not email or not password:
            raise ValueError('Email y contraseña son obligatorios')
        if Usuario.query.filter_by(email=email).first():
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
    def actualizar_conductor(id: int, data: dict) -> Conductor:
        c = Conductor.query.get(id)
        if not c:
            raise LookupError('Conductor no encontrado')
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
        db.session.commit()
        db.session.refresh(c)
        return c

    @staticmethod
    def desactivar_conductor(id: int) -> None:
        c = Conductor.query.get(id)
        if not c:
            raise LookupError('Conductor no encontrado')
        c.activo = False
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
        v = Vehiculo(placa=placa, tipo=data['tipo'].strip(),
                     capacidad_kg=data.get('capacidad_kg') or None,
                     codigo_siesa=data.get('codigo_siesa', '').strip() or None)
        db.session.add(v)
        db.session.commit()
        return v

    @staticmethod
    def actualizar_vehiculo(id: int, data: dict) -> Vehiculo:
        v = Vehiculo.query.get(id)
        if not v:
            raise LookupError('Vehículo no encontrado')
        if 'tipo'         in data: v.tipo         = data['tipo'].strip()
        if 'capacidad_kg' in data: v.capacidad_kg = data['capacidad_kg'] or None
        if 'codigo_siesa' in data: v.codigo_siesa = (data['codigo_siesa'] or '').strip() or None
        if 'activo'       in data: v.activo       = bool(data['activo'])
        db.session.commit()
        return v

    @staticmethod
    def desactivar_vehiculo(id: int) -> None:
        v = Vehiculo.query.get(id)
        if not v:
            raise LookupError('Vehículo no encontrado')
        v.activo = False
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
    def actualizar_maestra(id: int, data: dict) -> RutaMaestra:
        m = RutaMaestra.query.get(id)
        if not m:
            raise LookupError('Ruta maestra no encontrada')
        if 'nombre'    in data: m.nombre    = data['nombre'].strip()
        if 'tipo_ruta' in data: m.tipo_ruta = data['tipo_ruta']
        if 'activa'    in data: m.activa    = bool(data['activa'])

        if 'paradas' in data:
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
        db.session.commit()
        db.session.refresh(m)
        return m

    @staticmethod
    def eliminar_maestra(id: int) -> None:
        m = RutaMaestra.query.get(id)
        if not m:
            raise LookupError('Ruta maestra no encontrada')
        if RutaDespacho.query.filter_by(ruta_maestra_id=id).first():
            raise ConflictError('No se puede eliminar: la ruta tiene viajes asociados. Desactívala en su lugar.')
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
        # Rango de fechas (default: hoy si no se especifica)
        if fecha_desde or fecha_hasta:
            fd = _date.fromisoformat(fecha_desde) if fecha_desde else _dia_operativo()
            fh = _date.fromisoformat(fecha_hasta) if fecha_hasta else fd
            if fh < fd:
                fd, fh = fh, fd
            q = q.filter(RutaDespacho.fecha_programada >= fd,
                         RutaDespacho.fecha_programada <= fh)
        else:
            # Sin filtro explícito → solo hoy para no cargar todo el histórico
            q = q.filter(RutaDespacho.fecha_programada == _dia_operativo())
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
    def iniciar_ruta(id: int) -> dict:
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.PROGRAMADO:
            raise ValueError(f'La ruta debe estar PROGRAMADO, está {ruta.estado}')

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
                    TareaPacking.siesa_triggered == True,
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
                TareaPacking.siesa_triggered == True,
                TareaPacking.estado != EstadoPacking.CANCELADO,
                Bulto.estado == EstadoBulto.PENDIENTE,
                Bulto.ruta_despacho_id == None,
            ).all())
        return [b.to_dict() for b in bultos_libres
                if (b.tarea.municipio or '').lower() in municipios]

    @staticmethod
    def cerrar_ruta(id: int) -> RutaDespacho:
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

        _n_bultos = len(ruta.bultos)
        _ruta_id  = ruta.id
        ruta.estado = EstadoRutaDespacho.EN_TRANSITO
        ruta.fecha_cierre = datetime.utcnow()
        db.session.commit()
        logger.info(f'[RUTAS] Ruta {_ruta_id} EN_CARGUE → EN_TRANSITO ({_n_bultos} bultos)')

        return (RutaDespacho.query
                .options(
                    _jl(RutaDespacho.conductor),
                    _jl(RutaDespacho.vehiculo),
                    _jl(RutaDespacho.ruta_maestra),
                    _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
                )
                .get(_ruta_id))

    @staticmethod
    def entregar_ruta(id: int, data: dict, usuario_id: int) -> dict:
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
            raise ValueError(f'La ruta debe estar EN_TRANSITO, está {ruta.estado}')

        ahora = datetime.utcnow()
        rechazados = 0
        confirmaciones = data.get('bultos', [])

        if confirmaciones:
            bultos_ruta = Bulto.query.filter_by(ruta_despacho_id=id).with_for_update().all()
            for bulto in bultos_ruta:
                conf = next((c for c in confirmaciones if c['id'] == bulto.id), None)
                entregado = conf.get('entregado', True) if conf else True
                if entregado:
                    bulto.estado = EstadoBulto.ENTREGADO
                    bulto.fecha_entrega = ahora
                else:
                    bulto.estado = EstadoBulto.RECHAZADO
                    bulto.fecha_entrega = ahora
                    bulto.motivo_rechazo = conf.get('motivo_rechazo', 'Sin especificar') if conf else 'Sin especificar'
                    rechazados += 1

        ruta.estado = EstadoRutaDespacho.ENTREGADA
        ruta.fecha_entregada = ahora
        _ruta_id = ruta.id
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
            'entregados': len(confirmaciones) - rechazados if confirmaciones else 0,
            'rechazados': rechazados,
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
        for tid, p in tareas_map.items():
            t = p.pop('_tarea')
            valor_factura, es_contado, valores_ref, cond_pago_crudo, base_gravable, iva_factura = \
                RutaService._valor_y_cond_pago(t)
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
            _hay_valor = valor_factura is not None and bool(p['items']) and all(
                it.get('valor_unitario') is not None for it in p['items'])
            # **No es `es_contado`.** Toda venta de ruta sale en C02, que no es
            # contado documental pero sí se cobra en la puerta; alimentar el
            # modo con `es_contado` dejaba TODA parada en CREDITO y el
            # conductor no veía el cobro en ninguna. Se deriva del código
            # crudo, que es la única fuente — no de `es_contado`, que ya es una
            # lectura y responde otra pregunta.
            p['se_cobra_en_puerta'] = _cpm.cobra_en_la_puerta(
                cond_pago_crudo, connekta.cond_pago_ventas, connekta.cond_pago_ruta)
            p['modo_pago'] = _cpm.modo_pantalla(p['se_cobra_en_puerta'], _hay_valor)

        paradas = sorted(tareas_map.values(), key=lambda x: (x['municipio'], x['cliente']))
        # Ojo: `paradas` está indexado por tarea_id, así que cada entrada es una
        # factura, no una parada física. La parada real es DISTINCT cliente.
        gestionadas = sum(1 for p in paradas if p['recaudo'])

        # Mismo catálogo que usa Liquidación de escritorio (CATALOGO_RETENCIONES
        # en liquidacion_service.py) — una sola fuente, el conductor y el admin
        # ven siempre las mismas cuentas/tasas.
        from app.services.liquidacion_service import CATALOGO_RETENCIONES
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
        }

    @staticmethod
    def _valor_y_cond_pago(tarea) -> tuple:
        """`(valor_factura, es_contado, valores_por_referencia, cond_pago_crudo,
        base_gravable, iva)` de la FE real de una tarea. Cualquiera puede salir
        `None`/`{}` si Siesa no responde — nunca levanta. Alimenta el toggle
        Pago Total/Parcial del conductor y el recálculo en vivo cuando ajusta
        cantidades entregadas; si falta el dato, el frontend cae al campo
        libre de siempre (sin bloquear la pantalla).

        `base_gravable`/`iva`: suma de `f470_vlr_bruto`/`f470_vlr_imp` de las
        mismas líneas que ya se leen para `valor_factura` — sin consulta
        extra. Existen solo para que la pantalla del conductor pueda mostrar
        el desglose real de la factura (base + IVA = total), no para decidir
        nada — `valor_factura` (el neto) sigue siendo el único valor que
        gobierna el cobro.

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
        """
        from app.services.connekta_gateway import connekta
        from app.services.fe_resolver import resolver_fe_o_none

        tipo_fe, consec_fe = resolver_fe_o_none(tarea)
        if not tipo_fe or not consec_fe:
            return None, None, {}, None, None, None

        valor_factura = None
        base_gravable = None
        iva_factura = None
        valores_por_referencia = {}
        try:
            lineas = connekta.get_rowids_factura(tipo_fe, consec_fe)
            if lineas:
                valor_factura = round(sum(float(ln.get('f470_vlr_neto', 0)) for ln in lineas), 2)
                base_gravable = round(sum(float(ln.get('f470_vlr_bruto', 0)) for ln in lineas), 2)
                iva_factura = round(sum(float(ln.get('f470_vlr_imp', 0)) for ln in lineas), 2)
                for ln in lineas:
                    codigo = str(ln.get('f120_referencia', '')).strip()
                    cant = float(ln.get('f470_cant_base', 0) or 0)
                    vlr_neto = float(ln.get('f470_vlr_neto', 0) or 0)
                    if codigo and cant > 0:
                        valores_por_referencia[codigo] = round(vlr_neto / cant, 4)
        except Exception as e:
            logger.warning('[RUTAS] valor_factura falló para tarea %s (FE %s-%s): %s',
                            tarea.id, tipo_fe, consec_fe, e)

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
                    _cp0.cobra_en_la_puerta(
                        tarea.cond_pago, connekta.cond_pago_ventas, connekta.cond_pago_ruta),
                    valores_por_referencia,
                    tarea.cond_pago,
                    base_gravable,
                    iva_factura)
        try:
            cabecera = connekta.get_pedido_cabecera(
                tarea.tipo_docto_pedido_siesa, tarea.consec_docto_pedido_siesa)
            from app.services import cond_pago as _cp
            cond_pago_siesa = (cabecera or {}).get('f430_id_cond_pago') or ''
            cond_pago_crudo = cond_pago_siesa
            es_contado = _cp.cobra_en_la_puerta(
                cond_pago_siesa, connekta.cond_pago_ventas, connekta.cond_pago_ruta)
            if es_contado is None:
                _cp.registrar_ausencia(
                    f'pedido {tarea.tipo_docto_pedido_siesa}-{tarea.consec_docto_pedido_siesa}')
            # Anotar no puede romper lo anotado: si el commit falla, el valor ya
            # se devolvió y solo se pierde el ahorro de la próxima consulta.
            try:
                from app.extensions import db as _db_cp
                tarea.cond_pago = cond_pago_crudo
                _db_cp.session.commit()
            except Exception as _e_cp:
                try:
                    from app.extensions import db as _db_cp2
                    _db_cp2.session.rollback()
                except Exception:
                    pass
                logger.warning('[RUTAS] no se pudo anotar cond_pago en la tarea %s: %s',
                               tarea.id, _e_cp)
        except Exception as e:
            logger.warning('[RUTAS] cond_pago falló para tarea %s: %s', tarea.id, e)

        return valor_factura, es_contado, valores_por_referencia, cond_pago_crudo, base_gravable, iva_factura

    @staticmethod
    def confirmar_parada(ruta_id: int, tarea_id: int, usuario_id: int, data: dict) -> tuple:
        """Registra entrega y recaudo de una parada. Retorna (recaudo_id, es_edicion)."""
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
        # `cobra_en_la_puerta`, no `clasificar(...) == CONTADO`: C02 (la
        # condición de ruta que el vendedor captura para que la FE se apruebe
        # en Siesa) es, de cara al cliente, tan de contado como C01 — la
        # misma razón por la que la pantalla del conductor la trata igual en
        # `_valor_y_cond_pago`. Antes de este cambio, un pedido capturado
        # como C02 podía marcarse `forma_pago=CREDITO` sin que nada lo
        # impidiera.
        #
        # Se valida contra lo ANOTADO en la tarea (`m005condpagoparada`), no
        # contra Siesa: esta confirmación **tiene que funcionar sin señal**. Si
        # la condición no se alcanzó a anotar, no se bloquea — no saber no es
        # evidencia de contado (Regla 0).
        # Acotada a los estados donde `forma_pago` significa algo. En RECHAZADO
        # no se pide forma de pago —el camino es el motivo tipificado— pero el
        # `<select>` puede conservar un valor de un render anterior, y con él
        # la restricción bloquearía `NO_PAGO_SE_QUEDO`: justo el caso que el
        # motivo existe para canalizar. Prohibir sin dar salida devuelve al
        # conductor al camino de menor resistencia (marcar «cliente cerrado»),
        # que convierte un impago en un faltante de inventario.
        if forma_pago == FormaPago.CREDITO and \
                estado_entrega in (EstadoEntrega.ENTREGADO, EstadoEntrega.PARCIAL):
            from app.services import cond_pago as _cp_r
            from app.services.connekta_gateway import connekta as _cx_r
            _tarea_cp = TareaPacking.query.get(tarea_id)
            _anotada = getattr(_tarea_cp, 'cond_pago', None) if _tarea_cp else None
            # `cobra_en_la_puerta`, **no `clasificar`**: la pregunta acá es
            # si en esta parada había que cobrar, no si la factura era de
            # contado documental. Con `clasificar` la restricción no se
            # disparaba NUNCA en ruta —toda venta de ruta es C02, que no es
            # contado— así que el conductor podía marcar CREDITO sobre
            # cualquier parada y el control existía sin proteger nada.
            #
            # `is True` y no truthiness: `None` es «no se pudo saber» y ahí no
            # se bloquea (Regla 0) — una parada trabada en la calle no la
            # desbloquea nadie.
            if _cp_r.cobra_en_la_puerta(
                    _anotada, _cx_r.cond_pago_ventas, _cx_r.cond_pago_ruta) is True:
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
            if forma_pago != FormaPago.CREDITO:
                monto = float(data.get('monto_cobrado') or 0)
                if monto <= 0:
                    raise ValueError('El monto cobrado debe ser mayor a 0 en una entrega parcial')
            if not (data.get('observaciones') or '').strip():
                raise ValueError('Las observaciones son obligatorias en una entrega parcial')

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
            except Exception:
                pass

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
        # PARCIAL no exige seleccionar bultos rechazados — la devolución se
        # rastrea por referencia (items_entregados), no por bulto completo.
        # Los bultos de esta tarea quedan ENTREGADO (el checklist de "bulto
        # rechazado" es exclusivo de RECHAZADO).

        ahora = datetime.utcnow()
        ids_rechazados_set = set(bultos_rechazados_ids)

        for b in bultos_tarea:
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
        if es_edicion and recaudo.siesa_rc_triggered:
            _nuevo_monto = float(data.get('monto_cobrado', 0) or 0)
            _cambios = []
            if abs(_nuevo_monto - float(recaudo.monto_cobrado or 0)) > 0.01:
                _cambios.append(
                    f'monto_cobrado ({recaudo.monto_cobrado} → {_nuevo_monto})')
            if abs(monto_descuento - float(recaudo.monto_descuento or 0)) > 0.01:
                _cambios.append(
                    f'monto_descuento ({recaudo.monto_descuento} → {monto_descuento})')
            if _cambios:
                raise ValueError(
                    'El recibo de caja de esta parada ya se registró en Siesa: '
                    f'{" y ".join(_cambios)} no se puede cambiar desde el WMS. '
                    'Una corrección de un valor ya contabilizado se hace con '
                    'nota crédito. Las observaciones y la foto sí se pueden '
                    'editar.')

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
        recaudo.forma_pago            = forma_pago
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
        recaudo.foto_entrega          = foto
        recaudo.bultos_rechazados_ids = list(ids_rechazados_set & ids_tarea)
        recaudo.fecha_confirmacion    = ahora

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

        db.session.commit()
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

        tareas = ruta.tareas_unicas()
        recaudos_map = {r.tarea_id: r for r in ruta.recaudos}
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
    def liquidar_ruta(id: int) -> dict:
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

        ruta.estado_financiero = EstadoFinancieroRuta.LIQUIDADA

        # PARCIAL/RECHAZADO caen solos al módulo de Devoluciones al liquidar —
        # ver LiquidacionService.crear_devoluciones_pendientes_ruta. RC/DC
        # siguen siendo manuales en el módulo Liquidación (revisión de
        # retenciones y monto Siesa vs. conductor).
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
    def forzar_cierre_ruta(id: int, admin_id: int) -> dict:
        ruta = RutaDespacho.query.get(id)
        if not ruta:
            raise LookupError('Ruta no encontrada')
        if ruta.estado != EstadoRutaDespacho.EN_TRANSITO:
            raise ValueError(f'La ruta debe estar EN_TRANSITO para forzar cierre (estado: {ruta.estado})')

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
            db.session.add(RecaudoEntrega(
                ruta_id=id,
                tarea_id=tarea.id,
                estado_entrega=EstadoEntrega.RECHAZADO,
                forma_pago=None,
                monto_cobrado=0,
                observaciones='Cierre forzado por administrador — parada no gestionada',
                confirmado_por=admin_id,
                fecha_creacion=ahora,
            ))
            auto_cerradas += 1

        ruta.estado = EstadoRutaDespacho.ENTREGADA
        ruta.estado_financiero = EstadoFinancieroRuta.LIQUIDADA
        ruta.fecha_cierre = ahora
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
            'mensaje': f'Ruta cerrada. {auto_cerradas} parada(s) registradas como rechazadas automáticamente.',
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
