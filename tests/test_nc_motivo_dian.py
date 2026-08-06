"""
Motivo DIAN: el paso 3 de 4 que contabilidad hacía a mano en cada devolución.

De los cuatro pasos manuales del procedimiento (buscar el documento, cruzar
cartera, poner el concepto DIAN, aprobar), cruzar ya se automatizó con 251126 y
el concepto se puede poner con un **segundo** POST a 251546 — verificado en
vivo el 2026-08-03 contra NCE-00000057.

Segundo POST, no el mismo: `Entidades dinámicas` exige el consecutivo REAL del
documento, y con `F_CONSEC_AUTO_REG=1` ese número todavía no existe cuando la
sección se procesa. Ese es todo el motivo de que esto sea un job encadenado y
no dos líneas más en el payload que ya funcionaba.

Lo que estos tests protegen, por orden de lo que cuesta si se rompe:

1. **La NC no puede quedar en riesgo por el motivo.** La nota ya existe en
   Siesa cuando el motivo se intenta; ningún fallo de este paso puede tocar
   `siesa_nc_triggered` ni impedir que la NC se cree.
2. **No escribirle el motivo a la nota equivocada.** Es un documento fiscal de
   un tercero. Ante dos candidatas, se falla (Regla 0).
3. **Que el paso manual, cuando siga siendo manual, se vea.**
"""
from unittest.mock import patch

import pytest

from app.services.connekta_gateway import ConnektaGateway


def _devolucion(db, almacen, **kw):
    from app.models.devolucion_cliente import DevolucionCliente
    from app.models.packing import TareaPacking
    tarea = TareaPacking(
        codigo='PK-DIAN', tipo_documento='PEDIDO', estado='DESPACHADO',
        almacen_id=almacen.id, numero_pedido_siesa='PD-DIAN',
        tipo_docto_pedido_siesa='PD', consec_docto_pedido_siesa='700',
        siesa_triggered=True,
    )
    db.session.add(tarea)
    db.session.flush()
    d = DevolucionCliente(
        codigo='DEVC-DIAN-001', tarea_packing_id=tarea.id,
        numero_pedido_siesa='PD-DIAN', tipo_docto_fe='FEW', consec_fe='5555',
        almacen_id=almacen.id, estado='CONFIRMADA', **kw
    )
    db.session.add(d)
    db.session.commit()
    return d


def _job_nc(db, devolucion, producto):
    from app.models.siesa_job import SiesaJob
    job = SiesaJob.encolar('NOTA_CREDITO_DEVOLUCION_CLIENTE', {
        'devolucion_id': devolucion.id,
        'tipo_docto_fe': 'FEW', 'consec_fe': '5555',
        'items_devueltos': [{'codigo': producto.codigo_siesa, 'cantidad_devuelta': 4}],
        'es_total': False,
    })
    db.session.commit()
    return job


def _mock_nc(mc, producto, puede_dian=True, rowid_antes=900):
    """Deja el mock del gateway con valores REALES donde el job los serializa.

    Un MagicMock en `rowid_antes` no revienta acá pero sí en `json.dumps` del
    payload del job encadenado — y como ese encolado está dentro de un
    try/except que solo loguea, el test pasaría en verde sobre un job que nunca
    se creó.
    """
    mc.modo_simulacion = False
    mc.causal_devolucion_default = '01'
    mc.motivo_ventas = '01'
    mc.uom_default = 'UND'
    mc.bodega = 'NB1'
    mc.bodega_averias = 'AV1'
    mc.tipo_docto_nota_credito = 'NCE'
    mc.concepto_dian_nc = '1'
    mc.puede_fijar_motivo_dian = puede_dian
    mc.get_max_rowid_nc.return_value = rowid_antes
    mc.get_rowids_factura.return_value = [{
        'f470_rowid': '999', 'f120_referencia': producto.codigo_siesa,
        'f470_cant_base': 10, 'f470_id_unidad_medida': 'UND', 'f150_id': 'NB1',
        'f470_vlr_neto': 1000,
    }]
    mc.trigger_nota_factura_crear_cruzar.return_value = {'codigo': 0}
    return mc


def _gw(**kw):
    g = ConnektaGateway()
    g.modo_simulacion = False
    g.centro_op = '003'
    g.tipo_docto_nota_credito = 'NCE'
    g.consulta_nc_consecutivo = 'papeleriamedellin_WMS_NC_Consecutivo'
    for k, v in kw.items():
        setattr(g, k, v)
    return g


def _fila(consec, total, rowid, fecha='2026-08-05', estado=0, co='003', tipo='NCE'):
    return {
        'f350_rowid': rowid,
        'f350_id_co': co,
        'f350_id_tipo_docto': tipo,
        'f350_consec_docto': consec,
        'f350_fecha': f'{fecha}T00:00:00',
        'f350_ind_estado': estado,
        'f350_total_db': total,
    }


class TestIdentificarLaNCReciénCreada:
    """El WMS nunca supo qué consecutivo le asignó Siesa.

    El POST no lo devuelve. Por eso contabilidad tenía que BUSCAR el documento
    en Auditoría antes de poder aprobarlo — y por eso el motivo DIAN no se
    podía automatizar aunque el conector funcionara.
    """

    def _resolver(self, filas, valor=73150.0, rowid_min=None):
        g = _gw()
        with patch.object(g, '_filas_nc_encabezado', return_value=filas):
            return g.get_consec_nc_creada(valor, '20260805', rowid_min)

    def test_una_sola_candidata_se_resuelve(self):
        assert self._resolver([_fila(57, 73150.0, 900)]) == 57

    def test_DOS_CANDIDATAS_FALLAN_no_se_elige_una(self):
        """El caso que justifica todo lo demás.

        Dos devoluciones del mismo valor el mismo día es perfectamente posible
        —el mismo cliente devolviendo la misma caja dos veces—. Elegir "la más
        reciente" pondría el concepto DIAN sobre el documento de otro tercero.
        Un paso manual más barato que un error fiscal.
        """
        with pytest.raises(Exception, match='ambigüedad'):
            self._resolver([_fila(57, 73150.0, 900), _fila(58, 73150.0, 901)])

    def test_sin_candidatas_falla_en_vez_de_devolver_None(self):
        """Devolver None haría que el POST saliera con consec vacío."""
        with pytest.raises(Exception, match='ambigüedad'):
            self._resolver([])

    def test_el_rowid_previo_desempata(self):
        """La marca de agua tomada ANTES del POST: la fila vieja no es nuestra.

        Sin ella el caso de arriba (dos NCs idénticas el mismo día) sería
        irresoluble aunque una de las dos fuera de hace tres horas.
        """
        assert self._resolver(
            [_fila(57, 73150.0, 900), _fila(58, 73150.0, 901)], rowid_min=900
        ) == 58

    def test_no_toma_una_ya_aprobada(self):
        """Estado 1 = aprobada: su motivo ya está puesto, no es la nuestra."""
        with pytest.raises(Exception):
            self._resolver([_fila(57, 73150.0, 900, estado=1)])

    def test_no_toma_una_de_otro_dia(self):
        with pytest.raises(Exception):
            self._resolver([_fila(57, 73150.0, 900, fecha='2026-08-04')])

    def test_no_toma_una_de_otro_centro_de_operacion(self):
        with pytest.raises(Exception):
            self._resolver([_fila(57, 73150.0, 900, co='001')])

    def test_no_toma_un_documento_de_otro_tipo(self):
        with pytest.raises(Exception):
            self._resolver([_fila(57, 73150.0, 900, tipo='FEW')])

    def test_el_valor_ya_no_filtra_porque_siempre_es_cero_en_elaboracion(self):
        """Verificado en vivo 2026-08-06 contra ~10 NCE reales de Siesa QA:
        f350_total_db es SIEMPRE 0 mientras f350_ind_estado==0 (Elaboración,
        el único estado en el que este método busca — Regla #21), y solo se
        llena al aprobar a mano. Filtrar por él acá eliminaba SIEMPRE a la
        única candidata real, así que el motivo DIAN nunca se ponía solo. El
        valor ahora es puramente diagnóstico: no importa si coincide o no."""
        assert self._resolver([_fila(57, 0.0, 900)], valor=73150.0) == 57
        assert self._resolver([_fila(57, 0.0, 900)], valor=1.0) == 57

    def test_el_mensaje_dice_cuantas_vio_y_con_que_filtros(self):
        """Un "no se pudo" sin datos obliga a reproducirlo para diagnosticar."""
        with pytest.raises(Exception) as exc:
            self._resolver([_fila(57, 1.0, 900), _fila(58, 1.0, 901)], valor=1.0)
        m = str(exc.value)
        assert '2 candidatas' in m and 'CO=003' in m and '20260805' in m


class TestLaMarcaDeAguaNoPuedeRomperLaNC:
    """`get_max_rowid_nc` corre en el camino crítico de crear la nota.

    Si esa consulta explota —Connekta caído, consulta mal registrada, timeout—
    la nota crédito **igual tiene que crearse**. El motivo DIAN es una comodidad
    para contabilidad; la NC es plata del cliente.
    """

    def test_un_fallo_de_la_consulta_devuelve_None_y_no_levanta(self):
        g = _gw()
        with patch.object(g, '_filas_nc_encabezado', side_effect=RuntimeError('caído')):
            assert g.get_max_rowid_nc() is None

    def test_sin_filas_devuelve_None(self):
        g = _gw()
        with patch.object(g, '_filas_nc_encabezado', return_value=[]):
            assert g.get_max_rowid_nc() is None

    def test_devuelve_el_mayor(self):
        g = _gw()
        with patch.object(g, '_filas_nc_encabezado',
                          return_value=[_fila(1, 1, 900), _fila(2, 1, 903), _fila(3, 1, 901)]):
            assert g.get_max_rowid_nc() == 903


class TestElPayloadDelSegundoPOST:
    """Contra el payload REAL que devolvió `codigo:0` el 2026-08-03.

    Los códigos de maestro no son adivinables ni están en el texto de ayuda del
    Asistente (que ya se demostró desactualizado dos veces): salieron de
    consultar `t744/t747/t740/t741` a mano. Si alguien los "limpia" sin volver a
    consultarlos, Siesa rechaza y nadie sabrá por qué.
    """

    def _payload(self, **kw):
        g = _gw(**kw)
        capturado = {}

        def _falso(cid, nombre, payload, **kwargs):
            capturado.update({'id': cid, 'nombre': nombre, 'payload': payload,
                              'kwargs': kwargs})
            return {'codigo': 0}

        with patch.object(g, '_post', side_effect=_falso):
            g.trigger_motivo_dian_nc(57)
        return capturado

    def test_las_secciones_son_solo_tres(self):
        """Reenviar Docto/Movimientos/CuotasCxC crearía una NC nueva vacía."""
        p = self._payload()['payload']
        assert set(p) == {'Inicial', 'Entidades dinámicas', 'Final'}

    def test_los_codigos_de_maestro_son_los_verificados(self):
        e = self._payload()['payload']['Entidades dinámicas'][0]
        assert e['f753_id_grupo_entidad'] == 'FE_CONCEPTOS NC 2.1'  # guion bajo, no espacio
        assert e['f753_id_entidad'] == 'EUNOECO015'
        assert e['f753_id_atributo'] == 'co015_concepto_nc'
        assert e['f753_id_tipo_entidad'] == 'G504_1'
        assert e['f753_id_maestro'] == 'MUNOECO017'

    def test_el_concepto_va_como_detalle_de_maestro_no_como_numerico(self):
        """`co015_concepto_nc` es de tipo maestro genérico: rechaza el numérico
        suelto con "el código y el detalle del maestro es necesario"."""
        e = self._payload()['payload']['Entidades dinámicas'][0]
        assert e['f753_id_maestro_detalle'] == '1'
        assert e['f753_dato_numerico'] == 0

    def test_apunta_al_consecutivo_real_no_a_cero(self):
        """Con 0 Siesa responde "el documento o movimiento no existe" — es la
        diferencia entre este intento y los tres que fallaron antes."""
        e = self._payload()['payload']['Entidades dinámicas'][0]
        assert e['f350_consec_docto'] == 57
        assert e['F_ACTUALIZA_REG'] == 1

    def test_usa_el_conector_dinamico_con_idSistema(self):
        c = self._payload()
        assert c['id'] == '251546'
        assert 'idSistema' in c['kwargs'].get('extra_params', {})

    def test_el_concepto_es_configurable(self):
        """1=devolución parcial es el genérico; 2=anulación, 4=ajuste de precio
        existen y algún día el negocio los va a necesitar."""
        e = self._payload(concepto_dian_nc='2')['payload']['Entidades dinámicas'][0]
        assert e['f753_id_maestro_detalle'] == '2'


class TestElInterruptorEsExplicito:
    """Falta registrar la consulta del consecutivo en Connekta.

    Hasta que exista, esto NO puede correr — y tampoco puede quedarse callado:
    un paso manual que nadie sabe si sigue vigente se salta.
    """

    def test_sin_consulta_configurada_no_se_automatiza(self):
        assert _gw(consulta_nc_consecutivo='').puede_fijar_motivo_dian is False

    def test_en_simulacion_tampoco(self):
        g = _gw()
        g.modo_simulacion = True
        assert g.puede_fijar_motivo_dian is False

    def test_con_todo_configurado_si(self):
        assert _gw().puede_fijar_motivo_dian is True

    def test_sin_automatizacion_la_devolucion_queda_marcada_MANUAL(
            self, app, db, almacen, producto):
        """No basta con no hacerlo: hay que decir que no se hizo.

        Si la devolución quedara con el campo en NULL, "no se intentó" y "se
        intentó y no se pudo" serían indistinguibles — y contabilidad no sabría
        si ese documento necesita el paso manual o ya lo tiene.
        """
        devolucion = _devolucion(db, almacen)
        job = _job_nc(db, devolucion, producto)

        with patch('app.services.connekta_gateway.connekta') as mc:
            _mock_nc(mc, producto, puede_dian=False)
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        db.session.refresh(devolucion)
        assert devolucion.siesa_motivo_dian == 'MANUAL'
        assert 'CONNEKTA_CONSULTA_NC_CONSECUTIVO' in devolucion.siesa_motivo_dian_detalle

    def test_el_health_declara_los_tres_pasos(self, client, jwt_token_admin):
        r = client.get('/api/health/siesa',
                       headers={'Authorization': f'Bearer {jwt_token_admin}'})
        assert r.status_code in (200, 503)
        pasos = r.get_json()['pasos_manuales_nc']
        assert pasos['cruzar_cartera']['automatizado'] is True
        assert pasos['aprobar']['automatizado'] is False, (
            'aprobar sigue sin solución de API — ver CLAUDE.md')
        assert 'motivo_dian' in pasos


class TestLaCadenaNoPoneEnRiesgoLaNotaCredito:
    """La razón de que esto sea un job aparte y no dos líneas más.

    Cuando el motivo se intenta, la NC **ya existe en Siesa**. Si el motivo
    fallara dentro del mismo job, el DLQ reintentaría el job entero y entraría
    por la guarda `siesa_nc_triggered` → devolvería `{'idempotente': True}` y no
    volvería a intentar el motivo nunca. El bug no sería una NC duplicada: sería
    un reintento que parece exitoso y no hace nada.
    """

    def test_el_motivo_va_en_un_job_encadenado_aparte(self, app, db, almacen, producto):
        from app.models.siesa_job import SiesaJob

        devolucion = _devolucion(db, almacen)
        job = _job_nc(db, devolucion, producto)

        with patch('app.services.connekta_gateway.connekta') as mc:
            _mock_nc(mc, producto)
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        encadenado = SiesaJob.query.filter_by(tipo='MOTIVO_DIAN_NC').one()
        p = encadenado.get_payload()
        assert p['devolucion_id'] == devolucion.id
        assert p['valor_cruce'] == 400.0     # el mismo prorrateo que se cruzó
        assert p['rowid_antes'] == 900       # la marca de agua previa al POST
        assert len(p['fecha']) == 8          # YYYYMMDD Bogotá, no UTC

    def test_la_marca_de_agua_se_toma_ANTES_de_crear(self, app, db, almacen, producto):
        """Tomarla después la haría inútil: incluiría nuestra propia NC."""
        orden = []
        devolucion = _devolucion(db, almacen)
        job = _job_nc(db, devolucion, producto)

        with patch('app.services.connekta_gateway.connekta') as mc:
            _mock_nc(mc, producto)
            mc.get_max_rowid_nc.side_effect = lambda: (orden.append('rowid'), 900)[1]
            mc.trigger_nota_factura_crear_cruzar.side_effect = \
                lambda **kw: (orden.append('crear'), {'codigo': 0})[1]
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        assert orden == ['rowid', 'crear']

    def test_en_modo_ensayo_no_encadena_nada(self, app, db, almacen, producto):
        """No se creó ninguna NC en Siesa: no hay a qué ponerle motivo."""
        from app.models.siesa_job import SiesaJob

        devolucion = _devolucion(db, almacen)
        job = _job_nc(db, devolucion, producto)

        with patch('app.services.connekta_gateway.connekta') as mc:
            _mock_nc(mc, producto)
            mc.trigger_nota_factura_crear_cruzar.return_value = {'modo_ensayo': True}
            from app.services.siesa_job_service import _ejecutar_job
            _ejecutar_job(job)

        assert SiesaJob.query.filter_by(tipo='MOTIVO_DIAN_NC').count() == 0
        db.session.refresh(devolucion)
        assert devolucion.siesa_nc_triggered is False


class TestElJobDelMotivo:

    def _correr(self, db, devolucion, ajustar=None):
        from app.models.siesa_job import SiesaJob
        job = SiesaJob.encolar('MOTIVO_DIAN_NC', {
            'devolucion_id': devolucion.id, 'valor_cruce': 400.0,
            'fecha': '20260805', 'rowid_antes': 900,
        })
        db.session.commit()

        with patch('app.services.connekta_gateway.connekta') as mc:
            mc.modo_simulacion = False
            mc.tipo_docto_nota_credito = 'NCE'
            mc.concepto_dian_nc = '1'
            mc.get_consec_nc_creada.return_value = 57
            mc.trigger_motivo_dian_nc.return_value = {'codigo': 0}
            if ajustar:
                ajustar(mc)
            from app.services.siesa_job_service import _ejecutar_job
            return mc, _ejecutar_job(job)

    def test_resuelve_el_consecutivo_y_dispara_el_motivo(self, app, db, almacen):
        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        mc, res = self._correr(db, devolucion)

        mc.get_consec_nc_creada.assert_called_once_with(
            valor_cruce=400.0, fecha='20260805', rowid_minimo=900)
        mc.trigger_motivo_dian_nc.assert_called_once_with(57)
        db.session.refresh(devolucion)
        assert devolucion.siesa_nc_consec == '57'
        assert devolucion.siesa_motivo_dian == 'AUTOMATICO'

    def test_el_consecutivo_se_guarda_aunque_el_motivo_falle(self, app, db, almacen):
        """Saber QUÉ NCE es ya vale por sí solo: contabilidad la tenía que
        buscar a mano en Auditoría para aprobarla. Se guarda ANTES del POST."""
        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)

        def _revienta(mc):
            mc.trigger_motivo_dian_nc.side_effect = RuntimeError('Connekta caído')

        with pytest.raises(RuntimeError):
            self._correr(db, devolucion, _revienta)

        db.session.refresh(devolucion)
        assert devolucion.siesa_nc_consec == '57'
        assert devolucion.siesa_motivo_dian is None, (
            'el motivo no se puso: marcarlo AUTOMATICO sería mentir')

    def test_reintento_no_vuelve_a_resolver_el_consecutivo(self, app, db, almacen):
        """Ya guardado, la consulta no se repite — y sobre todo no puede
        resolver a un documento DISTINTO en el reintento."""
        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        devolucion.siesa_nc_consec = '57'
        db.session.commit()

        mc, _ = self._correr(db, devolucion)
        mc.get_consec_nc_creada.assert_not_called()
        mc.trigger_motivo_dian_nc.assert_called_once_with(57)

    def test_es_idempotente(self, app, db, almacen):
        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        devolucion.siesa_motivo_dian = 'AUTOMATICO'
        db.session.commit()

        mc, res = self._correr(db, devolucion)
        assert res.get('idempotente') is True
        mc.trigger_motivo_dian_nc.assert_not_called()

    def test_la_ambiguedad_falla_el_job_en_vez_de_adivinar(self, app, db, almacen):
        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        def _ambiguo(mc):
            mc.get_consec_nc_creada.side_effect = Exception('2 candidatas, ambigüedad')

        with pytest.raises(Exception, match='ambigüedad'):
            self._correr(db, devolucion, _ambiguo)

    def test_agotados_los_reintentos_la_devolucion_queda_MANUAL(self, app, db, almacen):
        """El tri-estado tiene que cerrarse. Si el job muere en FALLIDO y la
        devolución se queda en NULL, nadie sabe que ese documento necesita el
        paso a mano."""
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _marcar_motivo_dian_manual

        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        job = SiesaJob.encolar('MOTIVO_DIAN_NC', {'devolucion_id': devolucion.id})
        db.session.commit()

        _marcar_motivo_dian_manual(job, 'Connekta no respondió tras 3 intentos')

        db.session.refresh(devolucion)
        assert devolucion.siesa_motivo_dian == 'MANUAL'
        assert 'Connekta' in devolucion.siesa_motivo_dian_detalle

    def test_un_fallido_de_otro_tipo_no_toca_el_motivo(self, app, db, almacen):
        from app.models.siesa_job import SiesaJob
        from app.services.siesa_job_service import _marcar_motivo_dian_manual

        devolucion = _devolucion(db, almacen, siesa_nc_triggered=True)
        job = SiesaJob.encolar('DESPACHO_F470', {'devolucion_id': devolucion.id})
        db.session.commit()

        _marcar_motivo_dian_manual(job, 'otro fallo')
        db.session.refresh(devolucion)
        assert devolucion.siesa_motivo_dian is None


class TestVerificarLaConsultaAntesDeEncender:
    """`/api/health/nc-consecutivo` — probar sin escribir, como Vigía.

    Sin esto, el único método para saber si la consulta quedó bien registrada
    sería encenderla y esperar la próxima devolución. Y el fallo no es ruidoso:
    una columna con otro nombre hace que no haya candidatas, el job falle y el
    motivo quede manual — **indistinguible de no haberlo configurado nunca**.
    """

    def _pedir(self, client, token, **qs):
        from urllib.parse import urlencode
        url = '/api/health/nc-consecutivo'
        if qs:
            url += '?' + urlencode(qs)
        return client.get(url, headers={'Authorization': f'Bearer {token}'})

    def test_un_operario_no_entra(self, client, jwt_token):
        assert self._pedir(client, jwt_token).status_code == 403

    def test_sin_configurar_lo_dice_y_no_finge(self, client, jwt_token_admin):
        r = self._pedir(client, jwt_token_admin)
        assert r.status_code == 200
        assert r.get_json()['apto'] is False

    def test_una_consulta_que_trae_todo_es_apta(self, client, jwt_token_admin):
        from app.services.connekta_gateway import connekta

        with patch.object(type(connekta), '_filas_nc_encabezado',
                          lambda self: [_fila(57, 100.0, 900)]):
            r = self._pedir(client, jwt_token_admin, consulta='q_ok')
        d = r.get_json()
        assert d['apto'] is True
        assert d['columnas_faltantes'] == []
        assert 'CONNEKTA_CONSULTA_NC_CONSECUTIVO=q_ok' in d['siguiente_paso']

    def test_una_columna_ausente_NO_es_apta_y_la_nombra(self, client, jwt_token_admin):
        """El modo de fallo real: la consulta responde, con otras columnas."""
        from app.services.connekta_gateway import connekta

        fila = _fila(57, 100.0, 900)
        del fila['f350_ind_estado']
        with patch.object(type(connekta), '_filas_nc_encabezado', lambda self: [fila]):
            r = self._pedir(client, jwt_token_admin, consulta='q_incompleta')
        d = r.get_json()
        assert d['apto'] is False
        assert 'f350_ind_estado' in d['columnas_faltantes']

    def test_cero_filas_no_se_da_por_bueno(self, client, jwt_token_admin):
        """Una consulta correcta sobre una empresa sin NC y una consulta rota
        se ven igual desde acá. No se distinguen, así que no se aprueba."""
        from app.services.connekta_gateway import connekta

        with patch.object(type(connekta), '_filas_nc_encabezado', lambda self: []):
            r = self._pedir(client, jwt_token_admin, consulta='q_vacia')
        assert r.get_json()['apto'] is False

    def test_si_la_consulta_revienta_lo_reporta_sin_500(self, client, jwt_token_admin):
        from app.services.connekta_gateway import connekta

        def _explota(self):
            raise RuntimeError('no existe la consulta')

        with patch.object(type(connekta), '_filas_nc_encabezado', _explota):
            r = self._pedir(client, jwt_token_admin, consulta='q_mala')
        assert r.status_code == 200
        assert r.get_json()['apto'] is False
        assert 'no existe la consulta' in r.get_json()['motivo']

    def test_probar_una_candidata_no_pisa_la_configurada(self, client, jwt_token_admin):
        """Se prueba en caliente cambiando un atributo del gateway: si no se
        restaurara, una prueba dejaría la integración apuntando a otra cosa."""
        from app.services.connekta_gateway import connekta

        connekta.consulta_nc_consecutivo = 'la_de_produccion'
        try:
            with patch.object(type(connekta), '_filas_nc_encabezado',
                              lambda self: [_fila(57, 100.0, 900)]):
                self._pedir(client, jwt_token_admin, consulta='q_candidata')
            assert connekta.consulta_nc_consecutivo == 'la_de_produccion'
        finally:
            connekta.consulta_nc_consecutivo = ''

    def test_las_columnas_que_exige_son_las_que_el_gateway_usa(self):
        """TRINQUETE — si el gateway empieza a leer otra columna y esta lista
        no se mueve, el verificador aprueba una consulta que no sirve."""
        import inspect

        from app.routes.health import _COLUMNAS_NC_CONSECUTIVO
        from app.services.connekta_gateway import ConnektaGateway

        fuente = inspect.getsource(ConnektaGateway.get_consec_nc_creada)
        for col in ('f350_id_co', 'f350_id_tipo_docto', 'f350_ind_estado',
                    'f350_fecha', 'f350_rowid', 'f350_consec_docto'):
            assert col in fuente
            assert col in _COLUMNAS_NC_CONSECUTIVO, (
                f'{col} se usa para identificar la NC y el verificador no la exige')
        assert 'f350_total_db' not in _COLUMNAS_NC_CONSECUTIVO, (
            'ya no filtra por este campo (siempre 0 en Elaboración, ver '
            '2026-08-06) — exigirlo en el verificador sería pedir de más')
