"""
Resolución de códigos DANE → nombre de municipio colombiano.
Código DANE = departamento (2 dígitos) + municipio (3 dígitos).
Ejemplo: depto="41", ciudad="001" → "41001" → "Neiva"
"""

# Municipios más frecuentes en Colombia + todos los de Huila
DANE = {
    # Bogotá D.C.
    "11001": "Bogotá",
    # Antioquia
    "05001": "Medellín", "05088": "Bello", "05129": "Caldas", "05154": "Copacabana",
    "05212": "El Carmen de Viboral", "05266": "Envigado", "05282": "Fredonia",
    "05308": "Girardota", "05318": "Guarne", "05360": "Itagüí", "05376": "La Ceja",
    "05380": "La Estrella", "05440": "Marinilla", "05467": "Montebello",
    "05543": "Rionegro", "05607": "Sabaneta", "05615": "Salgar", "05631": "San Carlos",
    "05642": "Santa Bárbara", "05679": "Segovia", "05736": "Sonsón",
    # Valle del Cauca
    "76001": "Cali", "76054": "Buenaventura", "76111": "Cartago", "76122": "Caicedonia",
    "76248": "El Cerrito", "76306": "Ginebra", "76364": "La Unión", "76377": "La Victoria",
    "76400": "Obando", "76497": "Palmira", "76520": "Pradera", "76563": "Roldanillo",
    "76606": "San Pedro", "76616": "Sevilla", "76736": "Tulúa", "76823": "Versalles",
    "76834": "Vijes", "76845": "Yotoco", "76847": "Yumbo", "76863": "Zarzal",
    # Atlántico
    "08001": "Barranquilla", "08433": "Malambo", "08436": "Manatí", "08520": "Ponedera",
    "08573": "Puerto Colombia", "08634": "Sabanagrande", "08638": "Sabanalarga",
    # Bolívar
    "13001": "Cartagena", "13030": "Arjona", "13052": "Arroyo Hondo",
    # Santander
    "68001": "Bucaramanga", "68081": "Barrancabermeja", "68169": "Concepción",
    "68176": "Contratación", "68190": "Curití", "68245": "El Playón",
    "68307": "Girón", "68318": "Guaca", "68432": "Matanza", "68468": "Mogotes",
    "68500": "Palmas del Socorro", "68502": "Páramo", "68522": "Piedecuesta",
    "68547": "Pinchote", "68572": "Puente Nacional", "68615": "Rionegro",
    "68655": "Sabana de Torres", "68673": "San Gil", "68679": "San Joaquín",
    "68755": "Socorro", "68770": "Suaita", "68820": "Tona", "68855": "Vélez",
    # Cundinamarca
    "25001": "Agua de Dios", "25269": "Facatativá", "25290": "Fusagasugá",
    "25307": "Girardot", "25368": "La Mesa", "25386": "La Palma",
    "25473": "Mosquera", "25489": "Nilo", "25513": "Pacho", "25596": "Puerto Salgar",
    "25662": "San Bernardo", "25743": "Sibaté", "25745": "Silvania",
    "25754": "Soacha", "25758": "Sopó", "25769": "Subachoque", "25817": "Ubaté",
    "25843": "Venecia", "25873": "Villeta", "25899": "Zipaquirá",
    # Norte de Santander
    "54001": "Cúcuta", "54051": "Arboledas", "54099": "Bochalema",
    "54125": "Cáchira", "54128": "Cácota", "54172": "Chinácota",
    "54206": "Convención", "54223": "Durania", "54239": "El Carmen",
    "54245": "El Tarra", "54250": "El Zulia", "54261": "Gramalote",
    "54313": "Hacarí", "54344": "Herán", "54347": "Labateca",
    "54377": "La Esperanza", "54385": "La Playa", "54398": "Los Patios",
    "54405": "Lourdes", "54418": "Mutiscua", "54480": "Ocaña",
    "54498": "Pamplona", "54500": "Pamplonita", "54518": "Puerto Santander",
    "54553": "Ragonvalia", "54599": "Salazar", "54660": "San Calixto",
    "54670": "San Cayetano", "54673": "Santiago", "54680": "Sardinata",
    "54720": "Silos", "54743": "Teorama", "54800": "Tibú", "54810": "Toledo",
    "54820": "Villa Caro", "54871": "Villa del Rosario",
    # Tolima
    "73001": "Ibagué", "73024": "Alpujarra", "73026": "Alvarado",
    "73030": "Ambalema", "73043": "Anzoátegui", "73055": "Armero",
    "73067": "Ataco", "73124": "Cajamarca", "73148": "Carmen de Apicalá",
    "73152": "Casabianca", "73168": "Chaparral", "73200": "Coello",
    "73217": "Coyaima", "73226": "Cunday", "73236": "Dolores",
    "73268": "Espinal", "73270": "Falan", "73275": "Flandes",
    "73283": "Fresno", "73319": "Guamo", "73347": "Herveo",
    "73349": "Honda", "73352": "Icononzo", "73408": "Lérida",
    "73411": "Líbano", "73443": "Mariquita", "73449": "Melgar",
    "73461": "Murillo", "73483": "Natagaima", "73504": "Ortega",
    "73520": "Palocabildo", "73547": "Piedras", "73555": "Planadas",
    "73563": "Prado", "73585": "Purificación", "73616": "Rioblanco",
    "73622": "Roncesvalles", "73624": "Rovira", "73671": "Saldaña",
    "73675": "San Antonio", "73678": "San Luis", "73686": "Santa Isabel",
    "73770": "Suárez", "73854": "Valle de San Juan", "73861": "Venadillo",
    "73870": "Villahermosa", "73873": "Villarrica",
    # Caldas
    "17001": "Manizales", "17042": "Anserma", "17088": "Belalcázar",
    "17174": "Chinchiná", "17272": "Filadelfia", "17380": "La Dorada",
    "17388": "La Merced", "17433": "Manzanares", "17442": "Marmato",
    "17444": "Marquetalia", "17446": "Marulanda", "17486": "Neira",
    "17495": "Norcasia", "17513": "Pácora", "17524": "Palestina",
    "17541": "Pensilvania", "17616": "Riosucio", "17622": "Risaralda",
    "17653": "Salamina", "17662": "Samaná", "17665": "San José",
    "17777": "Supía", "17867": "Victoria", "17873": "Villamaría",
    "17877": "Viterbo",
    # Risaralda
    "66001": "Pereira", "66045": "Apía", "66075": "Balboa",
    "66088": "Belén de Umbría", "66170": "Dosquebradas", "66318": "Guática",
    "66383": "La Celia", "66400": "La Virginia", "66440": "Marsella",
    "66456": "Mistrató", "66572": "Pueblo Rico", "66594": "Quinchía",
    "66682": "Santa Rosa de Cabal", "66687": "Santuario",
    # Quindío
    "63001": "Armenia", "63111": "Buenavista", "63130": "Calarcá",
    "63190": "Circasia", "63212": "Córdoba", "63272": "Filandia",
    "63302": "Génova", "63401": "La Tebaida", "63470": "Montenegro",
    "63548": "Pijao", "63594": "Quimbaya", "63690": "Salento",
    # Nariño
    "52001": "Pasto", "52019": "Albán", "52022": "Aldana",
    "52036": "Ancuyá", "52051": "Arboleda", "52079": "Barbacoas",
    "52110": "Buesaco", "52203": "Colón", "52207": "Consacá",
    "52210": "Contadero", "52215": "Córdoba", "52224": "Cuaspud",
    "52227": "Cumbal", "52233": "Cumbitara", "52240": "Chachagüí",
    "52250": "El Charco", "52254": "El Peñol", "52256": "El Rosario",
    "52258": "El Tablón de Gómez", "52260": "El Tambo",
    "52287": "Funes", "52317": "Guachucal", "52320": "Guaitarilla",
    "52323": "Gualmatán", "52352": "Iles", "52354": "Imués",
    "52356": "Ipiales", "52378": "La Cruz", "52381": "La Florida",
    "52385": "La Llanada", "52390": "La Tola", "52399": "La Unión",
    "52405": "Leiva", "52411": "Linares", "52418": "Los Andes",
    "52427": "Magüí", "52435": "Mallama", "52473": "Mosquera",
    "52480": "Nariño", "52490": "Olaya Herrera", "52506": "Ospina",
    "52520": "Francisco Pizarro", "52540": "Policarpa",
    "52560": "Potosí", "52565": "Providencia", "52573": "Puerres",
    "52585": "Pupiales", "52612": "Ricaurte", "52621": "Roberto Payán",
    "52678": "Samaniego", "52683": "Sandoná", "52685": "San Bernardo",
    "52687": "San Lorenzo", "52693": "San Pablo", "52694": "San Pedro de Cartago",
    "52696": "Santa Bárbara", "52699": "Santacruz", "52720": "Sapuyes",
    "52786": "Taminango", "52788": "Tangua", "52835": "San Andres de Tumaco",
    "52838": "Túquerres", "52873": "Yacuanquer",
    # Huila — completo
    "41001": "Neiva", "41006": "Aipe", "41013": "Algeciras",
    "41016": "Altamira", "41020": "Baraya", "41078": "Campoalegre",
    "41132": "Colombia", "41206": "Elías", "41244": "Garzón",
    "41298": "Gigante", "41306": "Guadalupe", "41319": "Hobo",
    "41349": "Iquira", "41357": "Isnos", "41359": "La Argentina",
    "41378": "La Plata", "41396": "Nataga", "41483": "Oporapa",
    "41503": "Paicol", "41518": "Palermo", "41524": "Palestina",
    "41530": "Pital", "41548": "Pitalito", "41551": "Rivera",
    "41615": "Saladoblanco", "41660": "San Agustín", "41668": "Santa María",
    "41676": "Suaza", "41770": "Tarqui", "41791": "Tesalia",
    "41797": "Tello", "41799": "Teruel", "41801": "Timana",
    "41807": "Villavieja", "41872": "Yaguará",
    # Caquetá
    "18001": "Florencia", "18094": "Belén de los Andaquíes",
    "18150": "Cartagena del Chairá", "18205": "Curillo",
    "18247": "El Doncello", "18256": "El Paujil",
    "18410": "La Montañita", "18460": "Milán", "18479": "Morelia",
    "18592": "Puerto Rico", "18610": "San José del Fragua",
    "18753": "San Vicente del Caguán", "18756": "Solano",
    "18785": "Solita", "18860": "Valparaíso",
    # Putumayo
    "86001": "Mocoa", "86219": "Colón", "86320": "Orito",
    "86568": "Puerto Asís", "86569": "Puerto Caicedo",
    "86571": "Puerto Guzmán", "86573": "Puerto Leguízamo",
    "86749": "Sibundoy", "86755": "San Francisco",
    "86757": "San Miguel", "86760": "Santiago", "86865": "Valle del Guamuez",
    "86885": "Villagarzón",
}


def resolver_municipio(depto_cod: str, ciudad_cod: str) -> str:
    """
    Recibe los códigos de departamento y ciudad de Siesa/Connekta
    y devuelve el nombre del municipio.
    Ejemplo: resolver_municipio("41", "001") → "Neiva"
    """
    if not depto_cod or not ciudad_cod:
        return ''
    dane_key = f"{str(depto_cod).zfill(2)}{str(ciudad_cod).zfill(3)}"
    return DANE.get(dane_key, f"Ciudad {dane_key}")
