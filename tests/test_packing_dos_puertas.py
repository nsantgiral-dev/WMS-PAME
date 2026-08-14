"""
Packing tenía dos puertas y solo una con guardia.

    PUT  /api/packing/<id>/confirmar     → permiso de empaque + propiedad ✓
    POST /api/mobile/confirmar (PACKING) → ninguno de los dos ✗

La vía móvil llamaba `PackingService.confirmar_packing(tarea_id=tarea_id)`
**sin pasar el usuario**, así que el servicio no podía verificar nada:
cualquier operario confirmaba el packing de otro, y sin permiso de empaque.

## La causa es de capa, y picking ya la tenía bien

`confirmar_picking` verifica la propiedad **dentro del servicio**, así que toda
vía la hereda. Packing la tenía en la ruta — y una segunda ruta la esquivaba.

Un guard en la ruta protege esa ruta. Un guard en el servicio protege la
operación.
"""
import pytest

from app.services.packing_service import PackingService


@pytest.fixture
def tarea_de_otro(db, almacen):
    """Una tarea de packing asignada a un empacador, y otro operario distinto."""
    import uuid

    from app.models.packing import TareaPacking
    from app.models.usuario import Usuario

    def _u(email, rol, puede=False):
        x = Usuario.query.filter_by(email=email).first()
        if not x:
            x = Usuario(email=email, nombre=email, rol=rol, activo=True)
            x.set_password('t')
            if puede:
                x.puede_empacar = True
            db.session.add(x)
            db.session.flush()
        return x

    duenio = _u('emp_duenio@test.com', 'operario', puede=True)
    otro = _u('emp_otro@test.com', 'operario', puede=True)
    conductor = _u('emp_cond@test.com', 'conductor')

    tarea = TareaPacking(codigo=f'PK-2P-{uuid.uuid4().hex[:6]}', estado='EN_PROCESO',
                         almacen_id=almacen.id, empacador_id=duenio.id,
                         numero_pedido_siesa='PED-2P')
    db.session.add(tarea)
    db.session.commit()
    return tarea, duenio, otro, conductor


class TestElServicioVerificaLaPropiedad:
    """Donde tiene que estar: toda vía lo hereda."""

    def test_otro_empacador_no_puede_confirmar(self, db, tarea_de_otro):
        tarea, _duenio, otro, _c = tarea_de_otro
        with pytest.raises(ValueError, match='otro empacador'):
            PackingService.confirmar_packing(tarea_id=tarea.id, usuario_id=otro.id)

    def test_un_rol_sin_permiso_de_empaque_no_puede(self, db, tarea_de_otro):
        tarea, _d, _o, conductor = tarea_de_otro
        with pytest.raises(ValueError, match='no puede confirmar packing'):
            PackingService.confirmar_packing(tarea_id=tarea.id, usuario_id=conductor.id)

    def test_un_usuario_inexistente_no_pasa(self, db, tarea_de_otro):
        tarea, *_ = tarea_de_otro
        with pytest.raises(ValueError, match='no válido'):
            PackingService.confirmar_packing(tarea_id=tarea.id, usuario_id=999999)

    def test_supervision_puede_sobre_tarea_ajena(self, db, tarea_de_otro):
        """Un supervisor destraba la tarea de otro. Esa facultad ya existía en
        la ruta directa y no se pierde al mover el guard al servicio.

        Ojo con el matiz que este test fijó: la supervisión salta la
        **propiedad**, no el permiso de empaque. `jefe_almacen` NO está en
        `PACKING_ROLES`, así que sin el flag `puede_empacar` tampoco confirma —
        y eso ya era así en la ruta directa. El servicio replica la regla, no
        la reinventa.
        """
        from app.models.usuario import Usuario
        tarea, *_ = tarea_de_otro
        sup = Usuario.query.filter_by(email='sup_2p@test.com').first()
        if not sup:
            sup = Usuario(email='sup_2p@test.com', nombre='Supervisor',
                          rol='supervisor', activo=True)
            sup.set_password('t')
            db.session.add(sup); db.session.commit()
        # Pasa los dos guards y confirma. Sin ítems pendientes, la validación
        # de escaneo se cumple vacíamente.
        PackingService.confirmar_packing(tarea_id=tarea.id, usuario_id=sup.id)
        db.session.refresh(tarea)
        assert tarea.estado == 'VERIFICADO'

    def test_el_jefe_sin_flag_tampoco_empaca(self, db, tarea_de_otro):
        """Fija la regla existente para que mover el guard no la haya cambiado
        sin querer: `jefe_almacen` no está en `PACKING_ROLES`."""
        from app.models.usuario import Usuario
        tarea, *_ = tarea_de_otro
        jefe = Usuario.query.filter_by(email='jefe_2p@test.com').first()
        if not jefe:
            jefe = Usuario(email='jefe_2p@test.com', nombre='Jefe',
                           rol='jefe_almacen', activo=True)
            jefe.set_password('t')
            db.session.add(jefe); db.session.commit()
        with pytest.raises(ValueError, match='no puede confirmar packing'):
            PackingService.confirmar_packing(tarea_id=tarea.id, usuario_id=jefe.id)


class TestLaViaMovilPasaElUsuario:

    def test_mobile_service_lo_pasa(self):
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'mobile_service.py').read_text(encoding='utf-8')
        # Anclar en `confirmar_tarea`: hay OTRO `elif tipo == 'PACKING'` en
        # `escanear_item`, y buscar el primero miraba el bloque equivocado.
        _f = fuente.find('def confirmar_tarea')
        i = fuente.find("elif tipo == 'PACKING':", _f)
        j = fuente.find("elif tipo == 'CONTEO':", i)
        assert 'usuario_id=operario_id' in fuente[i:j], (
            '\nLa vía móvil volvió a confirmar packing sin identificar al '
            'usuario: cualquier operario confirma la tarea de otro.')

    def test_las_dos_puertas_usan_el_mismo_permiso(self):
        """`_puede_empacar` incluye el flag `puede_empacar`, no solo el rol.
        Reimplementarlo en el servicio sería la divergencia de siempre."""
        import pathlib
        fuente = (pathlib.Path(__file__).resolve().parents[1] / 'app' / 'services'
                  / 'packing_service.py').read_text(encoding='utf-8')
        assert '_puede_empacar' in fuente
