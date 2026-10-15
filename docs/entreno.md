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
Rodilla ≤ 90° con carga (cada ejercicio declara su flexión máxima) y nada de sentadilla profunda, búlgara, zancada larga, sissy, prensa sin tope ni saltos; nada de press tras nuca, remo al mentón, fondos ni press con barra; ≤ 6 series semanales de press por encima de la cabeza; 4 días de **4-6 ejercicios** y **≤ 60 min** por sesión (calentamiento + prevención del hombro incluidos; 2,5 min por serie en multiarticulares, 2 en aislados, superseries con descuento); volumen por músculo dentro de un rango. Si falla, se le devuelven los errores (2 veces); si sigue mal, se mantiene la rutina y avisa.

## Google Sheet
La Pi crea **«Health Sync · Gimnasio»** en tu Drive (permiso `drive.file`: solo ve lo que crea ella). Cada lunes, una pestaña «Semana dd-mm» con una fila por serie: variante (desplegable en la primera serie), objetivo, **Kg, Reps, RIR** (en amarillo, los rellenas tú), Notas y los objetivos de las alternativas. Las pestañas de semanas ya terminadas se **ocultan** solas (no se borran: se pueden volver a mostrar). `healthsync-sync` la lee cada 4 h (`entreno --leer`), guarda las series en la tabla `series` y, si cambias de variante en el desplegable, pone el objetivo de la nueva. **Los objetivos de la semana no cambian después del lunes:** el semáforo de recuperación es el del día en que se creó la pestaña (antes se recalculaba cada 4 h y podía pedir «una serie menos» con 3 filas en el Sheet).

En el bot: `/entreno` (la sesión de hoy o la siguiente) y `/analisis` (la valoración de tu último entreno).

## Ajuste de cada lunes (solo para la semana que empieza)
Al preparar la semana (`entreno --guardar`, lunes 10:00), Gemini recibe la rutina del bloque, la **revisión de los últimos 7 días** (hechos y valoración de cada sesión, y los días previstos sin series) y el **historial de ajustes** de las 3 semanas anteriores. Devuelve:
- **Balance de la semana:** lo bueno y lo que hay que mejorar (errores que se repiten, descansos, duración, RIR, sesiones saltadas).
- **La rutina ajustada para esa semana:** puede quitar o añadir ejercicios, series o repeticiones, cambiar el RIR o hacer superseries; y pone un **descanso recomendado** a cada ejercicio, que sale en el objetivo («· descanso 1,5 min»).
- **2-3 consejos.** Los pesos siguen siendo del código.

La Pi revisa la rutina ajustada con las mismas reglas que cualquier rutina de Gemini y con un tope de **3 cambios de estructura** (`MAX_CAMBIOS_SEMANA`: un ejercicio que entra o sale, o uno que sigue con otras series, repeticiones, RIR o variantes; una superserie nueva cuenta una vez; los descansos no cuentan). En **semana de descarga** o si ese lunes se ha renovado la rutina, el tope es 0. Si no cumple tras 2 correcciones, la semana va con la rutina del bloque y los consejos de siempre. Sin sesiones apuntadas en la semana, no hay ajuste.

El ajuste **vale solo para esa semana** (va en `entreno_semana.plan`, clave `ajuste`): el lunes siguiente se vuelve a partir de la rutina del bloque, y Gemini ve los ajustes anteriores para repetir los que funcionaron. Los días anteriores a la creación de la pestaña (rutina adelantada) no cuentan como sesiones saltadas.

Por Telegram llega un solo mensaje: balance, ajustes y por qué, consejos y un enlace «Abrir el Sheet» dentro del texto. Medido el 16/10/2026: ~3 min y ~38 k tokens (casi todo pensando), unos céntimos.

## Análisis de cada sesión
`healthsync/analisis.py`, lanzado por `entreno --leer` cada 4 h. Junta lo que apuntas en el Sheet con la sesión de fuerza del reloj de ese día (la del día previsto o, si cambiaste el día, la más cercana de esa semana) y manda por Telegram una valoración de Gemini Flash: veredicto 🟢/🟡/🔴, qué salió bien, qué mejorar y qué toca la próxima vez.

- **El código prepara los hechos; Gemini solo redacta.** Por ejercicio: el objetivo que ponía el Sheet al apuntarlo (el plan se recalcula durante la semana, así que se guarda el texto de la columna Objetivo con cada serie), las series hechas, notas, comprobaciones (fuera de rango, repeticiones que caen 3 o más, RIR 2 o más por encima del objetivo, series al fallo, peso distinto del objetivo o entre series), la sesión anterior con esa variante (volumen en kg·rep) y el objetivo de la próxima vez. De la sesión: duración frente a la estimada y al máximo de 60 min, pulso medio, zonas, kcal, sueño, VFC y recuperación. A Gemini no se le manda el peso corporal ni el % de grasa.
- Gemini **no cambia pesos ni ejercicios** (eso sigue siendo `entreno.objetivo`), y el pulso solo lo usa para la duración, los descansos y el asma, no para juzgar el esfuerzo de una serie.
- **Cuándo:** una sola vez por sesión, cuando el reloj la ha cerrado y están apuntados todos los ejercicios; si falta alguno, a las 12 h de parar el reloj; sin reloj, al día siguiente. Solo sesiones de los últimos 7 días. Si Gemini falla, se reintenta en la siguiente sincronización sin que esta falle.
- Tabla `analisis_entreno`. `python -m healthsync.analisis --ver` enseña los hechos y el prompt sin llamar a Gemini; `--rehacer` vuelve a analizar la última sesión (por ejemplo, después de corregir el Sheet).

## Puesta en marcha (una vez)
1. En el proyecto de Google Cloud de la app OAuth: activar **Google Sheets API** y **Google Drive API**, y añadir el permiso `.../auth/drive.file` en la pantalla de consentimiento.
2. Autorizar Drive con su **propio token** (`secrets/token_drive.json`): `hs auth-drive` (con el túnel SSH, como en la fase 0). No se puede usar el de Google Health: Health rechaza los tokens que llevan permisos de otras APIs (`403 DISALLOWED_OAUTH_SCOPES`).
3. `hs-update`, copiar `systemd/healthsync-{sync,calorias}.service` a `/etc/systemd/system/` y `systemctl daemon-reload`; reiniciar `healthsync-bot`.
4. Rutina, pesos de partida y Sheet:
   ```
   cd /opt/healthsync/app
   sudo -u healthsync /opt/healthsync/venv/bin/python -m healthsync.entreno --iniciar /ruta/pesos.json
   ```
   `pesos.json` = `{"Nombre exacto de la variante": {"kg": 28, "nota": "…"}, …}` (`kg: null` = calibrar). No va a GitHub.
