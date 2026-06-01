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

logger = logging.getLogger(__name__)


class ConflictError(ValueError):
    """Señala un conflicto de unicidad (HTTP 409)."""


class EstadoEntrega:
    ENTREGADO = 'ENTREGADO'
    PARCIAL   = 'PARCIAL'
    RECHAZADO = 'RECHAZADO'
    VALIDOS   = (ENTREGADO, PARCIAL, RECHAZADO)


class FormaPago:
    EFECTIVO      = 'EFECTIVO'
    TRANSFERENCIA = 'TRANSFERENCIA'
    CHEQUE        = 'CHEQUE'
    CREDITO       = 'CREDITO'
    EXENTO        = 'EXENTO'
    VALIDOS       = (EFECTIVO, TRANSFERENCIA, CHEQUE, CREDITO, EXENTO)


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
                     capacidad_kg=data.get('capacidad_kg') or None)
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
    def listar_rutas(conductor_id=None, vehiculo_id=None, estado=None, fecha=None, page=1):
        q = (RutaDespacho.query
             .options(
                 _jl(RutaDespacho.conductor),
                 _jl(RutaDespacho.vehiculo),
                 _jl(RutaDespacho.ruta_maestra),
                 _sl(RutaDespacho.bultos).joinedload(Bulto.tarea),
             )
             .order_by(RutaDespacho.fecha_creacion.desc()))
        if conductor_id:
            q = q.filter_by(conductor_id=conductor_id)
        if vehiculo_id:
            q = q.filter_by(vehiculo_id=vehiculo_id)
        if estado:
            q = q.filter_by(estado=estado)
        if fecha:
            from sqlalchemy import func
            q = q.filter(func.date(RutaDespacho.fecha_creacion) == fecha)
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
                .options(_sl(RutaDespacho.bultos).joinedload(Bulto.tarea))
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

        paradas = sorted(tareas_map.values(), key=lambda x: (x['municipio'], x['cliente']))
        return {
            'paradas':             paradas,
            'total_paradas':       len(paradas),
            'paradas_gestionadas': sum(1 for p in paradas if p['recaudo']),
        }

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
        if estado_entrega not in EstadoEntrega.VALIDOS:
            raise ValueError(f'estado_entrega debe ser {", ".join(EstadoEntrega.VALIDOS)}')

        forma_pago = data.get('forma_pago', '').upper() or None
        if forma_pago and forma_pago not in FormaPago.VALIDOS:
            raise ValueError(f'forma_pago inválido. Válidos: {", ".join(FormaPago.VALIDOS)}')

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
        if estado_entrega == EstadoEntrega.PARCIAL and not bultos_rechazados_ids:
            raise ValueError('Para entrega parcial debes indicar cuáles bultos fueron rechazados')

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
        recaudo.monto_cobrado         = data.get('monto_cobrado', 0) or 0
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
        totales = {'EFECTIVO': 0, 'TRANSFERENCIA': 0, 'CHEQUE': 0, 'CREDITO': 0, 'EXENTO': 0}
        sin_gestionar = 0

        for t in tareas:
            r = recaudos_map.get(t.id)
            bultos_t = [b for b in ruta.bultos if b.tarea_id == t.id]
            parada = {
                'tarea_id':           t.id,
                'numero_pedido':      t.numero_pedido_siesa,
                'cliente':            t.cliente or '',
                'municipio':          t.municipio or '',
                'bultos_total':       len(bultos_t),
                'bultos_entregados':  sum(1 for b in bultos_t if b.estado == EstadoBulto.ENTREGADO),
                'bultos_rechazados':  sum(1 for b in bultos_t if b.estado == EstadoBulto.RECHAZADO),
                'recaudo':            r.to_dict() if r else None,
            }
            paradas.append(parada)
            if r:
                fp = (r.forma_pago or '').upper()
                if fp in totales:
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
        db.session.commit()
        return {
            'ok':              True,
            'total_recaudado': ruta.total_recaudado(),
            'ruta':            ruta.to_dict(),
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
        q = (Bulto.query
             .options(_sl(Bulto.tarea))
             .filter_by(estado=EstadoBulto.RECHAZADO)
             .order_by(Bulto.fecha_entrega.desc()))
        total  = q.count()
        bultos = q.offset((page - 1) * limit).limit(limit).all()
        return {
            'bultos': [b.to_dict() for b in bultos],
            'total':  total,
            'page':   page,
            'pages':  (total + limit - 1) // limit,
        }

    @staticmethod
    def usuarios_conductores() -> list:
        from app.models.usuario import Usuario
        usuarios = (Usuario.query
                    .filter_by(rol='conductor', activo=True)
                    .order_by(Usuario.nombre)
                    .all())
        return [{'id': u.id, 'nombre': u.nombre, 'email': u.email} for u in usuarios]
