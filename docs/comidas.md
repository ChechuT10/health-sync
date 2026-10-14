# Comidas (fase 3)

Lo que se come de verdad, en la tabla `comidas` de la Pi y en Google Health (`nutrition-log`).

## Cada noche, 23:30 (`healthsync-comidas.timer`)
- Se escriben en Google Health las comidas del menú de ese día (con los macros que suma la Pi a partir del menú) y los fijos (los 3 cafés con leche), con la hora de cada comida y su tipo (desayuno, comida, cena o tentempié).
- No se escriben las que hayas **saltado** (`/saltar merienda`) ni las **sustituidas** por una comida de fuera.
- Se completan también los 3 días anteriores si alguna noche la Pi estuvo apagada, y se reintenta lo que Google rechazó (sin `Persistent=`: si la Pi arranca por la mañana, no apunta las comidas del día antes de comerlas).
- El último día del mes llega el **resumen mensual**: días registrados y según el plan, comidas fuera, saltadas y diferencia media con el objetivo. Solo informa: lo de fuera no se compensa con más déficit.

## Comidas de fuera (bot)
1. Mandas una **foto** (con pie opcional: «media ración», «con salsa»…) o `/fuera hamburguesa con patatas y una caña`.
2. Gemini estima kcal y macros con un **rango** (con foto, el error típico es del ±30-50 %) y la confianza. Si la estimación no tiene sentido (no es comida, rango absurdo), se descarta.
3. El bot pregunta con botones qué comida **sustituye** (o «Extra» si fue algo de más) y la escribe en ese momento en Google Health. Si esa comida del menú ya estaba escrita, cuenta como extra.

## Gasto real
`healthsync.calorias` usa como «lo comido» la media de los días registrados de las últimas 4 semanas si hay al menos 10; si no, supone que se cumplieron los planes.

## Puesta en marcha
```
hs-update
sudo cp /opt/healthsync/app/systemd/healthsync-comidas.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now healthsync-comidas.timer
sudo systemctl restart healthsync-bot
```
