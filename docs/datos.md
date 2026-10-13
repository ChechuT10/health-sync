# Base de datos

`healthsync.sync` guarda los datos en `/var/lib/healthsync/db/salud.db` (SQLite, modo WAL) en dos capas:

1. **`raw`**: cada registro de la API tal cual, con una clave estable por tipo. Volver a descargar un registro no lo duplica; si cambió (un dato que llegó tarde), se actualiza.
2. **Tablas limpias** (`dias`, `sueno`, `entrenamientos`, `pesos`): se reconstruyen enteras desde `raw` en cada sincronización (unos 2 s en una Pi 3B+).

`ejecuciones` registra cada sincronización con su estado (`ok`, `parcial`, `error`).

## Qué se descarga

| Tipo | Cómo | En cada pasada |
|---|---|---|
| `daily-resting-heart-rate`, `daily-heart-rate-variability`, `daily-oxygen-saturation`, `daily-respiratory-rate`, `weight`, `body-fat` | `list` | Todo (son pocos registros) |
| `sleep`, `exercise` | `list` con filtro de fecha (25 por página) | Desde el día anterior al pedido |
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
```
