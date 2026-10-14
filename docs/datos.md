# Base de datos

`healthsync.sync` guarda los datos en `/var/lib/healthsync/db/salud.db` (SQLite, modo WAL) en dos capas:

1. **`raw`**: cada registro de la API tal cual, con una clave estable por tipo. Volver a descargar un registro no lo duplica; si cambió (un dato que llegó tarde), se actualiza.
2. **Tablas limpias** (`dias`, `sueno`, `entrenamientos`, `pesos`): se reconstruyen enteras desde `raw` en cada sincronización (unos 2 s en una Pi 3B+).

Además:

| Tabla | Qué guarda |
|---|---|
| `medidas` | Lo que mandas por el bot (`/peso`, `/medidas`): peso, cintura, cuello y % de grasa (Marina). `gh_peso` y `gh_grasa` guardan el id del dato escrito en Google Health; si están vacíos, Google lo rechazó y solo existe la copia de la Pi. |
| `plan_semana` | El objetivo de cada semana, de martes a lunes (`semana` = el martes; se calcula el lunes anterior): kcal, macros, metabolismo basal, gasto estimado y real, ritmo, fase (`deficit` o `descanso`) y el porqué. Lo escribe `healthsync.calorias --guardar`. |
| `menu_semana` | El menú de cada semana tal como lo devolvió Gemini (JSON), lo que encontró buscando productos, los totales por día que calculó la Pi y el origen (`ia`, `ia_corregido` o `repetido`). Ver `docs/menu.md`. |
| `rutina_bloque`, `pesos_iniciales`, `entreno_semana`, `series` | Gimnasio: rutina de cada bloque, pesos de partida, plan semanal con objetivos y cada serie que apuntas en el Sheet. Ver `docs/entreno.md`. |
| `comidas` | Lo que se come cada día: las del menú (se escriben a las 23:30), las de fuera (foto o descripción, con rango) y las saltadas, con el id en Google Health. Ver `docs/comidas.md`. |
| `estado` | Valores sueltos del programa, como el último mensaje leído por el bot (`bot_offset`). |
| `descartes` | Registros de la API que no deben contar (ver abajo). |
| `ejecuciones` | Cada sincronización con su estado (`ok`, `parcial`, `error`). |

**Tabla de alimentos:** no está en la base de datos sino en `healthsync/alimentos.csv` (valores por 100 g, BEDCA/USDA o la etiqueta), para poder revisarla y editarla a mano. `healthsync.alimentos` la carga; con ella el código recalcula los macros de cada menú que proponga la IA. `tests/test_alimentos.py` comprueba que las kcal de cada alimento cuadran con sus macros.

**`pesos`** une `weight` y `body-fat` de Google Health por hora de la muestra, y añade las medidas del bot que aún no están en Google Health (sin duplicar las que sí).

**Fecha de inicio:** las tablas limpias solo usan datos desde `FECHA_INICIO` (`healthsync/config.py`, por defecto 2026-09-01; se cambia con la variable `HEALTHSYNC_INICIO`). Lo anterior que devuelva la API se guarda en `raw`, pero no se usa.

**Descartes:** los registros que no deben contar (pruebas, errores) se marcan en la tabla `descartes`. Siguen en `raw`, pero las tablas limpias los ignoran. Se pueden marcar por clave o todos los de un tipo en un día:

```bash
python -m healthsync.sync --descartar weight 2026-09-23 --motivo "pesos de prueba"
```

## Qué se descarga

| Tipo | Cómo | En cada pasada |
|---|---|---|
| `daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `weight`, `body-fat` | `list` | Todo (son pocos registros) |
| `exercise` | `list` con filtro de fecha (25 por página) | Desde el día anterior al pedido |
| `sleep` | `list` sin filtro (la API no admite filtrar el sueño por fecha): de lo más nuevo hacia atrás | Hasta pasar la fecha pedida |
| `steps`, `distance`, `floors`, `active-energy-burned`, `active-zone-minutes`, `sedentary-period`, `time-in-heart-rate-zone`, `total-calories`, `heart-rate` | `dailyRollUp` (une reloj y móvil sin duplicar) | Los últimos 3 días |

Los agregados diarios se guardan en `raw` con el sufijo `:dia` (por ejemplo, `steps:dia`).

## Formato de la API (comprobado con datos reales)

- **Los resúmenes diarios (`daily-*`) no traen `name`**: su clave es la fecha. `respiratory-rate-sleep-summary` trae un `name` vacío (`…/dataPoints/`): su clave es la hora de la muestra.
- Muchos enteros llegan como **texto** (`"76"`) y las duraciones como `"1310.964s"`.
- Hay **ceros que significan "sin dato"** (frecuencia respiratoria en REM, pesos de 0 g). En las tablas limpias, un dato que falta es `NULL`, nunca 0.
- `sleep` no trae hora civil: el día se calcula con `endTime` + `endUtcOffset`.
- `mainSleep` y `nap` **no son excluyentes**: hay siestas de tarde con las dos marcas, y alguna sin ninguna. Noche = `mainSleep` y no `nap`.
- `daily-resting-heart-rate` indica si se calculó con sueño (`WITH_SLEEP`) o solo con datos despierto (`ONLY_WITH_AWAKE_DATA`). Para medir la recuperación solo vale la primera (`fc_reposo_noche = 1`).
- Hay registros de sueño `CLASSIC` (fases `ASLEEP`, `RESTLESS`, `AWAKE`) además de `STAGES` (`LIGHT`, `DEEP`, `REM`, `AWAKE`).

## Tipo de día

La **cobertura** es la fracción del día con pulso registrado (suma de `time-in-heart-rate-zone` / 24 h).

| Tipo | Condición | Qué se puede usar |
|---|---|---|
| `completo` | Noche registrada y cobertura ≥ 50 % | Todo |
| `solo_noche` | Noche registrada y cobertura < 50 % (reloj solo para dormir) | Sueño y recuperación; la actividad viene del móvil |
| `sin_noche` | Sin noche y cobertura ≥ 30 % | Actividad; sueño y VFC vacíos |
| `solo_movil` | Cobertura < 30 % y hay pasos | Solo pasos |
| `sin_datos` | Nada | Nada |

Los umbrales están en `healthsync/config.py`.

## Consultas útiles

```bash
sqlite3 /var/lib/healthsync/db/salud.db
```

```sql
-- Últimos 14 días
SELECT fecha, tipo_dia, pasos, sueno_min / 60.0 AS horas, fc_reposo, vfc_ms FROM dias ORDER BY fecha DESC LIMIT 14;
-- Últimas sincronizaciones
SELECT inicio, estado, substr(detalle, 1, 80) FROM ejecuciones ORDER BY id DESC LIMIT 5;
-- Medidas del bot y si llegaron a Google Health
SELECT fecha, hora, peso_kg, cintura_cm, grasa_pct, gh_peso IS NOT NULL AS en_google FROM medidas ORDER BY id DESC LIMIT 10;
-- Planes semanales
SELECT semana, numero, fase, kcal, proteina_g, grasa_g, hidratos_g, ritmo_pct FROM plan_semana ORDER BY semana DESC;
```
