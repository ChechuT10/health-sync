# Menú semanal

Cada lunes a las 10:00, `healthsync-calorias.service` calcula las calorías de la semana (de martes a lunes) y después lanza `healthsync.menu --guardar --imprimir`:

1. **Gemini busca** (con Google Search) productos actuales de los supermercados de `config.toml` que encajen, y frutas y verduras de temporada.
2. **Gemini monta el menú** en JSON con esquema fijo: alimentos con valores por 100 g y su fuente, platos con gramos, qué se come cada día, lista de la compra y consejos. Recibe las calorías y macros del plan y las preferencias; **nunca el peso ni el % de grasa**.
3. **La Pi solo revisa** (no ajusta nada): suma gramos × valores de cada día más los fijos (los cafés y la creatina) y lo compara con el plan (kcal ±5 %, proteína −5/+15 %, grasa e hidratos ±15 %). También comprueba que las kcal de cada alimento cuadran con sus macros, que los alimentos que están en `alimentos.csv` tienen valores parecidos, que no aparece nada de lo que no comes y que los días de entreno están bien.
4. Si algo falla, **le devuelve los errores a Gemini** para que lo corrija (2 veces como mucho). Si sigue mal, repite el menú anterior si cuadra con las calorías nuevas; si no, avisa por Telegram.
5. **PDF** con Typst (3 hojas A4 apaisadas: la semana, los platos y la lista de la compra con los consejos) → impresora → Telegram.

Con `gemini-flash-latest` tarda unos 10 minutos; la Pi solo espera la respuesta. Pro (3.1 preview) tarda varios minutos por respuesta y con búsqueda no llegó a responder, por eso se usa Flash.

En el bot: `/hoy` (menú del día) y `/compra` (lista de la compra; el lunes, la de la semana que empieza al día siguiente).

## Puesta en marcha (una vez)

1. **Clave de la API de Gemini** de un proyecto en el **nivel de pago** (en el gratuito, Google puede usar los datos para entrenar):
   ```
   sudo -u healthsync sh -c 'umask 077; cat > /var/lib/healthsync/secrets/gemini_api_key'
   ```
   (pegar la clave, Enter, Ctrl+D)
2. **Typst** (lo instala `scripts/setup_rpi.sh`; a mano):
   ```
   curl -fsSL https://github.com/typst/typst/releases/download/v0.15.1/typst-aarch64-unknown-linux-musl.tar.xz \
     | sudo tar -xJ -C /usr/local/bin --strip-components=1 typst-aarch64-unknown-linux-musl/typst
   ```
3. Servicio actualizado: copiar `systemd/healthsync-calorias.service` a `/etc/systemd/system/` y `sudo systemctl daemon-reload`.

## Probar a mano

```
cd /opt/healthsync/app
sudo -u healthsync /opt/healthsync/venv/bin/python -m healthsync.menu            # solo lo enseña
sudo -u healthsync /opt/healthsync/venv/bin/python -m healthsync.menu --guardar  # guarda, PDF y Telegram
sudo -u healthsync /opt/healthsync/venv/bin/python -m healthsync.menu --pdf --imprimir   # reimprimir
```
Sin plan de calorías guardado, la prueba usa la propuesta de `/calorias` (con `--guardar` exige el plan).
