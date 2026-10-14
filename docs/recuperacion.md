# Guía de recuperación

Health Sync no guarda copias de seguridad: está diseñado para que la Raspberry sea **reemplazable**. Todo lo importante vive fuera de ella:

| Qué | Dónde vive | Cómo se recupera |
|---|---|---|
| Datos del reloj y del móvil | Google Health | Se vuelven a descargar |
| Peso y % de grasa | Google Health (los escribe el bot) | Se vuelven a descargar |
| Comidas (del menú y de fuera) | Google Health (`nutrition-log`) | Están allí; la tabla `comidas` de la Pi no se reconstruye sola |
| Series del gimnasio | Google Sheets (una pestaña por semana) | Siguen en el Sheet; **la Pi no las vuelve a cargar sola** (pendiente) |
| Código | Este repositorio | `git clone` |
| Credenciales OAuth | Google Cloud Console | Se descarga otra vez el JSON del cliente y se repiten `hs auth` y `hs auth-drive` |
| Clave de Gemini y bot de Telegram | AI Studio y @BotFather | Se vuelven a guardar en `secrets/` |
| Configuración personal | `config.toml` (solo en la Raspberry) | **Guarda una copia en tu gestor de contraseñas** |
| Rutina del bloque, pesos de partida, planes, menús, cintura y cuello, fotos | **Solo en la Raspberry** | Se pierden si muere la microSD (copias de seguridad: pendiente, fase 4) |

## Si la tarjeta microSD falla

1. Graba Raspberry Pi OS Lite (64 bits) con Raspberry Pi Imager: SSH activado, mismo usuario y nombre de equipo.
2. Elimina la huella antigua en tu ordenador: `ssh-keygen -R IP_DE_TU_RASPBERRY`
3. Entra por SSH y sigue la [guía de instalación](guia.md) (secciones 3 a 12).
4. Copia tu `config.toml` a `/opt/healthsync/app/`.
5. Autoriza con `hs auth` y `hs auth-drive`, y carga los datos con `healthsync.sync --desde AAAA-MM-DD`.

Tiempo estimado: unos 30 minutos, la mayor parte esperando a que se instalen los paquetes.
