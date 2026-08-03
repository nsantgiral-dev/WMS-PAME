"""
El catálogo de ítems de inspección, en código y versionado.

Fuente de verdad para `furgon_liviano_v1` y `camion_v1`. Está acá y no en un
`INSERT` dentro de una migración porque **las plantillas son datos que se leen y
se auditan**, no esquema: quiero poder ver el diff cuando alguien cambie un
gesto, y quiero que el sembrado sea idempotente y re-ejecutable.

La versión está en el código de la plantilla (`camion_v1`) a propósito: **una
plantilla no se edita, se versiona.** Una inspección hecha bajo `camion_v1` tiene
que seguir siendo legible dentro de dos años; si los ítems cambiaran bajo sus
pies, el registro diría una cosa y significaría otra. Cambiar el catálogo es
crear `camion_v2` y desactivar la anterior.

Criterio de bloqueante — las cuatro condiciones, todas:

  1. puede causar accidente, inmovilización por autoridad o varada HOY
  2. el conductor lo verifica SIN HERRAMIENTA
  3. en MENOS DE 20 SEGUNDOS
  4. con respuesta BINARIA Y OBJETIVA, no un juicio

La cuarta es la que más se olvida: "¿está bien la suspensión?" no es binaria y
por eso no puede bloquear, por grave que sea.

LO QUE NO ENTRA ACÁ, y la lista importa tanto como la otra: espesor de pastillas
y bandas, amortiguadores, juego de terminales, rodamientos, compresión, turbo,
alineación, balanceo, correa de repartición. Nada de eso lo evalúa un conductor
en patio, y **cada ítem incontestable en la pantalla diaria entrena el reflejo de
marcar óptimo sin mirar** (regla 11). Va al plan preventivo por kilómetro.
"""

# (nombre, gesto, criticidad, periodicidad)
_BLOQUEANTES_COMUNES = [
    ('Freno de servicio',
     'Motor encendido, pisar a fondo y sostener 5 segundos. '
     '¿El pedal sigue hundiéndose o llega al piso?', 'bloqueante', 'diaria'),
    ('Freno de estacionamiento',
     'En pendiente, aplicar y soltar el pedal 3 segundos. ¿Se mueve el vehículo?',
     'bloqueante', 'diaria'),
    ('Llantas: flanco, labrado y tuercas',
     'Recorrer todas las posiciones. ¿Hay abultamiento o herida en el costado? '
     '¿El labrado llegó al testigo? ¿Falta o está floja alguna tuerca?',
     'bloqueante', 'diaria'),
    ('Nivel de refrigerante y aceite de motor',
     'Motor frío. ¿Alguno está por debajo del mínimo?', 'bloqueante', 'diaria'),
    ('Fuga activa de frenos, combustible o refrigerante',
     'Mirar el piso bajo el vehículo. ¿Hay charco o goteo activo? '
     '(Sudado de aceite sin goteo es hallazgo mayor, no bloqueante.)',
     'bloqueante', 'diaria'),
    ('Luces traseras: stop, direccionales y cocuyos',
     'Con ayuda o contra una pared. ¿Alguna no enciende?', 'bloqueante', 'diaria'),
    ('Limpiaparabrisas y lavador',
     'Activarlos. ¿Barren limpio o rayan? ¿Sale agua?', 'bloqueante', 'diaria'),
]

_BLOQUEANTE_FURGON = (
    'Puertas del furgón aseguran',
    'Cerrar y jalar. ¿Quedan trabadas?', 'bloqueante', 'diaria')

_BLOQUEANTE_DOCUMENTOS = (
    'Documentos y equipo reglamentario',
    # La lista sola no es contestable: enumerar no es preguntar. Termina en una
    # pregunta binaria porque el criterio de bloqueante lo exige, y porque
    # "revisar documentos" se responde de memoria a la tercera semana.
    'SOAT, tecnomecánica y licencia vigentes. Extintor con carga y sin vencer, '
    'dos señales, dos tacos, llanta de repuesto con aire, gato y cruceta. '
    '¿Falta alguno, o hay alguno vencido o descargado?',
    'bloqueante', 'diaria')

_MAYORES_COMUNES = [
    ('Espejos', '¿Están completos y sin fisuras que distorsionen?', 'mayor', 'diaria'),
    ('Batería y bornes', '¿Hay sulfatación visible o bornes flojos?', 'mayor', 'diaria'),
    ('Escape y soportes',
     '¿Está suelto o rompiendo? Si entra gas a la cabina, es bloqueante.',
     'mayor', 'diaria'),
    ('Sudado de aceite', '¿Hay humedad de aceite sin goteo activo?', 'mayor', 'diaria'),
    ('Luces de reversa', '¿Encienden al poner reversa?', 'mayor', 'diaria'),
    ('Cinturón de seguridad',
     '¿Retrae y traba? ¿Tiene cortes o deshilachado?', 'mayor', 'diaria'),
    ('Holguras de suspensión',
     '¿Se sienten golpes secos al pasar un resalto?', 'mayor', 'diaria'),
    ('Botiquín', '¿Completo y sin elementos vencidos?', 'mayor', 'diaria'),
    ('Chaleco reflectivo', '¿Está en el vehículo?', 'mayor', 'diaria'),
    ('Linterna', '¿Está y enciende?', 'mayor', 'diaria'),
    ('Caja de herramienta', '¿Está completa?', 'mayor', 'diaria'),
]

_MAYORES_CAMION = [
    ('Alarma de retroceso', '¿Suena al poner reversa?', 'mayor', 'diaria'),
    # Semanal, no diaria: preguntarlo todos los días lo vuelve ruido, y el ruido
    # entrena a marcar sin leer.
    ('Drenaje del separador de agua',
     'Drenar y observar. ¿Sale agua?', 'mayor', 'semanal'),
]

_MENORES = [
    ('Golpes y rayones de carrocería', '¿Hay daño nuevo desde el último turno?', 'menor', 'diaria'),
    ('Tapones de ruedas', '¿Están todos?', 'menor', 'diaria'),
    ('Radio y antena', '¿Funcionan?', 'menor', 'diaria'),
    ('Aire acondicionado', '¿Enfría?', 'menor', 'diaria'),
    ('Limpieza interior y exterior', '¿Está presentable?', 'menor', 'diaria'),
    ('Accesorios varios', '¿Hay algo suelto o faltante?', 'menor', 'diaria'),
]


def _plantilla(codigo, nombre, aplica_a, version, items):
    return {'codigo': codigo, 'nombre': nombre, 'aplica_a': aplica_a,
            'version': version, 'items': items}


CATALOGO = [
    _plantilla(
        'furgon_liviano_v1', 'Furgón liviano — v1', 'furgon_liviano', 1,
        # 8 bloqueantes: los comunes + documentos. Sin puertas de furgón.
        _BLOQUEANTES_COMUNES + [_BLOQUEANTE_DOCUMENTOS] + _MAYORES_COMUNES + _MENORES,
    ),
    _plantilla(
        'camion_v1', 'Camión con furgón — v1', 'camion', 1,
        # 9 bloqueantes: los comunes + puertas del furgón + documentos.
        _BLOQUEANTES_COMUNES + [_BLOQUEANTE_FURGON, _BLOQUEANTE_DOCUMENTOS]
        + _MAYORES_COMUNES + _MAYORES_CAMION + _MENORES,
    ),
]


def sembrar(db):
    """Crea las plantillas que falten. Idempotente: no pisa las que existen.

    No actualiza una plantilla existente **a propósito**: editarla en sitio
    cambiaría el significado de las inspecciones ya hechas bajo ella. Para
    cambiar el catálogo se crea `<tipo>_v2`.

    Devuelve `{codigo: 'creada' | 'ya existía'}`.
    """
    from flota.adaptadores.modelos import ItemInspeccion, PlantillaInspeccion

    resultado = {}
    for p in CATALOGO:
        if PlantillaInspeccion.query.filter_by(codigo=p['codigo']).first():
            resultado[p['codigo']] = 'ya existía'
            continue
        plantilla = PlantillaInspeccion(
            codigo=p['codigo'], nombre=p['nombre'],
            aplica_a=p['aplica_a'], version=p['version'], activa=True,
        )
        db.session.add(plantilla)
        db.session.flush()
        for orden, (nombre, gesto, criticidad, periodicidad) in enumerate(p['items'], 1):
            db.session.add(ItemInspeccion(
                plantilla_id=plantilla.id, orden=orden, nombre=nombre,
                gesto=gesto, criticidad=criticidad, periodicidad=periodicidad,
            ))
        resultado[p['codigo']] = 'creada'
    db.session.commit()
    return resultado


__all__ = ['CATALOGO', 'sembrar']
