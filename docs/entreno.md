# Gimnasio

Rutina torso/pierna de 4 días (`healthsync/rutina_base.json`, de la nota 04 de la bóveda), con tus días: **martes Pierna 1 (cadera), jueves Torso 1 (empuje), viernes Pierna 2 (cuádriceps) y lunes Torso 2 (tirón y hombro)**. Si un día lo cambias al miércoles, haces esa sesión y ya está.

## Quién decide qué

| Qué | Quién | Cuándo |
|---|---|---|
| **Pesos y repeticiones de cada semana** | El código (doble progresión, sin IA) | Cada lunes, y cada 4 h para lo que aún no has hecho |
| **Cambiar ejercicios** | Gemini propone, la Pi revisa las reglas | Cada 12 semanas (bloque nuevo), o antes si se estancan 3 ejercicios o uno da dolor dos veces |
| **Consejos de la semana** | Gemini (3-5 viñetas) | Cada lunes, por Telegram |

## Progresión (nota 04)
- Cada **variante** tiene su propio historial. Primera vez: el peso que contaste al empezar (`pesos_iniciales`) o **calibración** (eliges un peso con RIR 2).
- Todas las series al máximo del rango con RIR ≥ 1 → **+incremento** (2,5 kg en máquina, 2 kg con mancuernas, 1 kg en elevaciones…) y vuelta al mínimo del rango. Si no, mismo peso y «busca N+».
- Las repeticiones bajan 2 o más dos sesiones seguidas → **−10 %**. «Dolor» o «molestia» en Notas → **−20 %** (y si se repite, Gemini cambia el ejercicio).
- **Descarga** cada 6.ª semana del bloque: mitad de series, mismo peso, RIR 3-4.
- **Recuperación** (VFC y sueño de 7 días frente a los 60 anteriores): verde normal; ámbar, sin subidas; rojo, una serie menos y RIR +1; sin datos (< 4 noches con reloj), se decide por el rendimiento.

## Reglas que la Pi comprueba en cualquier rutina de Gemini
Rodilla ≤ 90° con carga (cada ejercicio declara su flexión máxima) y nada de sentadilla profunda, búlgara, zancada larga, sissy, prensa sin tope ni saltos; nada de press tras nuca, remo al mentón, fondos ni press con barra; ≤ 6 series semanales de press por encima de la cabeza; 4 días de 4-8 ejercicios y ~60 min; volumen por músculo dentro de un rango. Si falla, se le devuelven los errores (2 veces); si sigue mal, se mantiene la rutina y avisa.

## Google Sheet
La Pi crea **«Health Sync · Gimnasio»** en tu Drive (permiso `drive.file`: solo ve lo que crea ella). Cada lunes, una pestaña «Semana dd-mm» con una fila por serie: variante (desplegable en la primera serie), objetivo, **Kg, Reps, RIR** (en amarillo, los rellenas tú), Notas y los objetivos de las alternativas. `healthsync-sync` la lee cada 4 h (`entreno --leer`), guarda las series en la tabla `series` y actualiza los objetivos de lo que aún no has hecho.

En el bot: `/entreno` (la sesión de hoy o la siguiente).

## Puesta en marcha (una vez)
1. En el proyecto de Google Cloud de la app OAuth: activar **Google Sheets API** y **Google Drive API**, y añadir el permiso `.../auth/drive.file` en la pantalla de consentimiento.
2. Volver a autorizar con el permiso nuevo: `hs auth` (con el túnel SSH, como en la fase 0).
3. `hs-update`, copiar `systemd/healthsync-{sync,calorias}.service` a `/etc/systemd/system/` y `systemctl daemon-reload`; reiniciar `healthsync-bot`.
4. Rutina, pesos de partida y Sheet:
   ```
   cd /opt/healthsync/app
   sudo -u healthsync /opt/healthsync/venv/bin/python -m healthsync.entreno --iniciar /ruta/pesos.json
   ```
   `pesos.json` = `{"Nombre exacto de la variante": {"kg": 28, "nota": "…"}, …}` (`kg: null` = calibrar). No va a GitHub.
