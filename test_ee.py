import ee

# Inicializa usando las credenciales configuradas en el entorno o archivo de servicio
ee.Initialize(project='project-d4cfdf75-119e-4422-b4a')

# Una consulta simple para probar la API (por ejemplo, obtener información de una colección de Sentinel-2)
collection = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED').limit(1)
info = collection.getInfo()
print("¡Conexión exitosa con Earth Engine!", info)