# Memoria y CPU en la Raspberry Pi 3B+

La Pi tiene **905 MB de RAM** (más ~900 MB de zram) y se comparte con otros proyectos, así que cada servicio de Health Sync tiene un tope (`MemoryMax=`) y los trabajos periódicos van con prioridad baja (`Nice=10`).

| Servicio | Cuándo | Tope | Uso real |
|---|---|---|---|
| `healthsync-bot` | Siempre | 100 MB | ~27 MB |
| `healthsync-sync` | Cada 4 h (2-3 s) | 200 MB | — |
| `healthsync-calorias` (+ menú) | Lunes 10:00 (~10 min esperando a Gemini; Typst < 30 MB y 0,5 s) | 250 MB | — |
| `healthsync-recordatorio` | Cada día 6:00 | 100 MB | — |
| `healthsync-aviso@` | Si algo falla | 100 MB | — |

Si un servicio pasa de su tope, systemd lo para y salta el aviso por Telegram en vez de dejar sin memoria al resto.

**Cómo leer la memoria:** en `btop` o `free -m`, "libre" (*free*) parece poco porque Linux usa la RAM sobrante como caché de disco y la suelta cuando hace falta. La que cuenta es **disponible** (*available*). El 14/10/2026: 239 MB usados, 617 MB de caché y **665 MB disponibles**.
