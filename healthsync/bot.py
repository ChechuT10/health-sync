"""Bot de Telegram de Health Sync (servicio permanente, long polling).

Solo responde a tu chat (secrets/telegram_chat_id); los demás mensajes se ignoran.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.bot                 # el servicio healthsync-bot
  python -m healthsync.bot recordatorio    # lo lanza un temporizador cada tarde
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
from datetime import date, datetime

from . import medidas, telegram
from .medidas import es
from .config import DB_FILE, TZ
from .db import conectar

AYUDA = """Comandos de Health Sync:

/peso 99,4 — anota tu peso (kg)
/medidas cintura 102 cuello 41 — calcula tu % de grasa (método de la Marina); puedes añadir «peso 99,4»
/calorias — calorías y macros de esta semana, y por qué
/estado — última sincronización, último peso y la semana
/ayuda — esta ayuda

Sin números, /peso y /medidas te preguntan. También los tienes en el botón Menú o escribiendo «/».

Pésate y mídete siempre igual: por la mañana, en ayunas, después del baño. La cintura a la altura del ombligo y el cuello justo bajo la nuez, en cm."""


def ultimo_peso(con: sqlite3.Connection) -> tuple[str, float] | None:
    return con.execute("SELECT fecha, peso_kg FROM pesos WHERE peso_kg IS NOT NULL "
                       "ORDER BY fecha DESC, hora DESC, rowid DESC LIMIT 1").fetchone()


def reconstruir(con: sqlite3.Connection) -> None:
    from .normalizar import reconstruir as rehacer
    rehacer(con)


def cmd_peso(con, args: str) -> str:
    peso = medidas.leer_peso(args)
    anterior = ultimo_peso(con)
    m = medidas.guardar(con, peso=peso)
    errores = medidas.subir_a_google(con, m, peso)
    reconstruir(con)
    txt = f"✅ Anotado: {es(peso)} kg ({m['local']:%d/%m %H:%M})."
    if anterior:
        dif = peso - anterior[1]
        txt += f"\nAnterior: {es(anterior[1])} kg el {date.fromisoformat(anterior[0]):%d/%m} ({'+' if dif >= 0 else ''}{es(dif, 1)} kg)."
    return txt + aviso_google(errores)


def cmd_medidas(con, args: str) -> str:
    v = medidas.leer_medidas(args)
    m = medidas.guardar(con, peso=v.get("peso"), cintura=v["cintura"], cuello=v["cuello"])
    errores = medidas.subir_a_google(con, m, v.get("peso"))
    reconstruir(con)
    txt = (f"✅ Cintura {es(v['cintura'])} cm, cuello {es(v['cuello'])} cm"
           + (f", peso {es(v['peso'])} kg" if "peso" in v else "")
           + f"\n% de grasa (Marina): {es(m['grasa'], 1)} %")
    previa = con.execute(
        "SELECT fecha, grasa_pct, cintura_cm FROM medidas WHERE grasa_pct IS NOT NULL AND id < ? "
        "ORDER BY id DESC LIMIT 1", (m["id"],)).fetchone()
    if previa:
        dg, dc = m["grasa"] - previa[1], v["cintura"] - previa[2]
        txt += (f"\nAnterior ({date.fromisoformat(previa[0]):%d/%m}): {es(previa[1], 1)} % "
                f"({'+' if dg >= 0 else ''}{es(dg, 1)}), cintura {'+' if dc >= 0 else ''}{es(dc, 1)} cm")
    return txt + "\nOjo: el método tiene ±3-4 puntos de error; lo que importa es la tendencia." + aviso_google(errores)


def aviso_google(errores: list[str]) -> str:
    if not errores:
        return "\nGuardado también en Google Health."
    return "\n⚠️ Guardado en la Pi, pero Google Health lo rechazó:\n" + "\n".join(errores)


def cmd_estado(con, _args: str) -> str:
    sync = con.execute("SELECT inicio, estado FROM ejecuciones WHERE estado IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    peso = ultimo_peso(con)
    semana = con.execute(
        """SELECT count(*), sum(sueno_min IS NOT NULL), avg(pasos), avg(sueno_min),
                  (SELECT count(*) FROM entrenamientos WHERE fecha >= date('now', 'localtime', '-7 days'))
           FROM dias WHERE fecha >= date('now', 'localtime', '-7 days')""").fetchone()
    lineas = []
    if sync:
        lineas.append(f"Última sincronización: {datetime.fromisoformat(sync[0]):%d/%m %H:%M} ({sync[1]})")
    lineas.append(f"Último peso: {es(peso[1])} kg el {date.fromisoformat(peso[0]):%d/%m}" if peso
                  else "Aún no hay ningún peso desde el 01/09: mándalo con /peso")
    dias, noches, pasos, sueno, entrenos = semana
    if dias:
        lineas.append(f"Últimos 7 días: {round(pasos or 0):,} pasos de media".replace(",", "."))
        lineas.append(f"Sueño: {noches} de {dias} noches con reloj"
                      + (f", {es(sueno / 60, 1)} h de media" if sueno else ""))
        lineas.append(f"Entrenamientos: {entrenos}")
    return "\n".join(lineas)


def cmd_calorias(con, _args: str) -> str:
    from . import calorias
    hoy = datetime.now(TZ).date()
    guardado = con.execute("SELECT 1 FROM plan_semana WHERE semana = ?",
                           (calorias.lunes(hoy).isoformat(),)).fetchone()
    pr = calorias.proponer(con, hoy)
    if pr is None or guardado:
        return calorias.texto(pr)
    return calorias.texto(pr) + "\n\n(Propuesta: el plan de la semana se fija el lunes a las 10:00.)"


COMANDOS = {"/peso": cmd_peso, "/medidas": cmd_medidas, "/estado": cmd_estado, "/calorias": cmd_calorias}


# Lista que Telegram enseña al escribir «/» y en el botón Menú
MENU = [
    ("peso", "Anotar tu peso"),
    ("medidas", "Cintura y cuello → % de grasa"),
    ("calorias", "Calorías y macros de esta semana"),
    ("estado", "Última sincronización, peso y semana"),
    ("ayuda", "Comandos y cómo medirte"),
]
# Si mandas /peso o /medidas sin números, el bot los pregunta y espera tu respuesta
PREGUNTAS = {
    "/peso": "⚖️ ¿Cuánto pesas? Escribe solo el número, p. ej. 99,4",
    "/medidas": "📏 Escribe cintura y cuello en cm, p. ej. 102 41 (puedes añadir «peso 99,4»)",
}
pendiente: str | None = None


def atender(con: sqlite3.Connection, texto: str) -> str:
    global pendiente
    texto = texto.strip()
    if texto.startswith("/"):
        orden, _, args = texto.partition(" ")
        orden = orden.split("@")[0].lower()     # /peso@jt_salud_pi_bot
        pendiente = None
    elif pendiente:                              # respuesta a una pregunta del bot
        orden, args = pendiente, texto
    else:
        return "No te entiendo. Toca el botón Menú o escribe /ayuda."
    if orden in ("/start", "/ayuda", "/help"):
        return AYUDA
    if orden in PREGUNTAS and not args.strip():
        pendiente = orden
        return PREGUNTAS[orden]
    if orden in COMANDOS:
        try:
            respuesta = COMANDOS[orden](con, args)
        except medidas.MedidaError as e:
            return f"❌ {e}" + ("\nPrueba otra vez o toca otro comando." if pendiente else "")
        pendiente = None
        return respuesta
    return "No te entiendo. Toca el botón Menú o escribe /ayuda."


def bucle() -> None:
    mi_chat = int(telegram.CHAT_FILE.read_text().strip())
    con = conectar(DB_FILE)
    fila = con.execute("SELECT valor FROM estado WHERE clave = 'bot_offset'").fetchone()
    offset = int(fila[0]) if fila else 0
    try:
        telegram._api("setMyCommands", commands=json.dumps(
            [{"command": c, "description": d} for c, d in MENU], ensure_ascii=False))
    except Exception as e:  # noqa: BLE001  (sin menú el bot funciona igual)
        print(f"No se pudo registrar el menú: {e}", flush=True)
    print(f"Bot en marcha (offset {offset})", flush=True)
    while True:
        try:
            updates = telegram._api("getUpdates", offset=offset, timeout=50,
                                    allowed_updates='["message"]')
        except (urllib.error.URLError, TimeoutError, RuntimeError, OSError) as e:
            print(f"Telegram no responde: {e}; reintento en 30 s", flush=True)
            time.sleep(30)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if (msg.get("chat") or {}).get("id") != mi_chat or not msg.get("text"):
                continue                     # solo tu chat y solo texto
            try:
                respuesta = atender(con, msg["text"])
            except Exception as e:  # noqa: BLE001
                respuesta = f"❌ Error interno: {type(e).__name__}: {e}"
                print(respuesta, file=sys.stderr, flush=True)
            telegram.enviar(respuesta)
        if updates:
            con.execute("INSERT INTO estado VALUES ('bot_offset', ?) "
                        "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor", (str(offset),))
            con.commit()


def recordatorio() -> None:
    """Domingo: toca pesarse y medirse (si no lo has hecho en 3 días). Otros días: aviso cada
    3 días si llevas más de 10 sin pesarte."""
    con = conectar(DB_FILE)
    peso = ultimo_peso(con)
    con.close()
    hoy = datetime.now(TZ).date()
    dias = (hoy - date.fromisoformat(peso[0])).days if peso else None
    if hoy.weekday() == 6 and (dias is None or dias >= 3):
        telegram.enviar("⚖️ Domingo: si mañana puedes, pésate y mídete en ayunas.\n"
                        "/peso 99,4 · /medidas cintura 102 cuello 41")
    elif dias is not None and dias > 10 and dias % 3 == 2:
        telegram.enviar(f"⚖️ Llevas {dias} días sin pesarte. Cuando puedas: /peso 99,4")


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "recordatorio":
        recordatorio()
    else:
        bucle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
