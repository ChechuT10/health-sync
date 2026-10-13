# Guía de recuperación

Health Sync no guarda copias de seguridad: está diseñado para que la Raspberry sea **reemplazable**. Todo lo importante vive fuera de ella:

| Qué | Dónde vive | Cómo se recupera |
|---|---|---|
| Datos del reloj y del móvil | Google Health | Se vuelven a descargar |
| Peso y % de grasa | Google Health (los escribe el bot) | Se vuelven a descargar |
| Medidas de cuello y cintura | Google Sheets, pestaña "Medidas" | Se leen de la hoja |
| Entrenamientos anotados | Google Sheets | Se leen de la hoja |
| Comidas | Google Health | Se vuelven a descargar |
| Código | Este repositorio | `git clone` |
| Credenciales OAuth | Google Cloud Console | Se descarga otra vez el JSON del cliente |
| Configuración personal | `config.toml` (solo en la Raspberry) | **Guarda una copia en tu gestor de contraseñas** |

## Si la tarjeta microSD falla

1. Graba Raspberry Pi OS Lite (64 bits) con Raspberry Pi Imager: SSH activado, mismo usuario y nombre de equipo.
2. Elimina la huella antigua en tu ordenador: `ssh-keygen -R IP_DE_TU_RASPBERRY`
3. Entra por SSH y sigue los pasos de instalación del [README](../README.md#instalación).
4. Copia tu `config.toml` a `/opt/healthsync/app/`.
5. Autoriza con `fase0.py auth` y descarga con `fase0.py dump --desde AAAA-MM-DD`.

Tiempo estimado: unos 30 minutos, la mayor parte esperando a que se instalen los paquetes.
