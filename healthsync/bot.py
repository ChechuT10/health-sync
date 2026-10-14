"""Bot de Telegram de Health Sync (servicio permanente, long polling).

Solo responde a tu chat (secrets/telegram_chat_id); los demás mensajes se ignoran.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.bot                 # el servicio healthsync-bot
  python -m healthsync.bot recordatorio    # lo lanza un temporizador cada día a las 6:00
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
/hoy — el menú de hoy
/compra — la lista de la compra de la semana
/entreno — la sesión de gimnasio de hoy (o la siguiente)
/fuera pizza mediana y una caña — comida de fuera de casa (o mándame una foto)
/saltar merienda — una comida del menú que no hiciste hoy
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
    # El plan en vigor; el lunes, desde las 10:00, ya está guardado el que empieza mañana
    semanas = [calorias.martes_en_vigor(hoy)]
    if hoy.weekday() == 0:
        semanas.insert(0, calorias.proximo_martes(hoy))
    for semana in semanas:
        pr = calorias.plan_guardado(con, semana)
        if pr is not None:
            return calorias.texto(pr)
    pr = calorias.proponer(con, hoy)
    if pr is None:
        return calorias.texto(pr)
    return calorias.texto(pr) + "\n\n(Propuesta: el plan se fija el lunes a las 10:00 y va de martes a lunes.)"


def _menu_en_vigor(con, hoy: date, siguiente: bool = False):
    """Menú que se está siguiendo hoy; con siguiente=True, el lunes el de la semana que empieza mañana."""
    from . import calorias, menu
    semanas = [calorias.martes_en_vigor(hoy)]
    if siguiente and hoy.weekday() == 0:
        semanas.insert(0, calorias.proximo_martes(hoy))
    for s in semanas:
        r = menu.cargar(con, s)
        if r is not None:
            return r
    return None


def cmd_hoy(con, _args: str) -> str:
    from . import menu
    hoy = datetime.now(TZ).date()
    r = _menu_en_vigor(con, hoy)
    if r is None:
        return "Aún no hay menú para esta semana: sale el lunes a las 10:00, con las calorías."
    return menu.texto_dia(r, menu.DIAS[(hoy.weekday() - 1) % 7])


def cmd_compra(con, _args: str) -> str:
    from . import menu
    r = _menu_en_vigor(con, datetime.now(TZ).date(), siguiente=True)
    if r is None:
        return "Aún no hay menú: la lista de la compra sale el lunes a las 10:00."
    return menu.texto_compra(r)


def cmd_entreno(con, _args: str) -> str:
    """La sesión de hoy o, si hoy no toca, la siguiente de la semana (o de la que empieza mañana)."""
    from . import calorias, entreno
    hoy = datetime.now(TZ).date()
    for semana in (calorias.martes_en_vigor(hoy), calorias.proximo_martes(hoy)):
        f = con.execute("SELECT plan FROM entreno_semana WHERE semana = ?", (semana.isoformat(),)).fetchone()
        if not f:
            continue
        p = json.loads(f[0])
        for d in p["dias"]:
            if d["fecha"] >= hoy.isoformat():
                return entreno.texto_plan(p, solo_dia=d["id"]).strip()
    return "Aún no hay plan de gimnasio: sale el lunes a las 10:00."


# ---- Comidas de fuera y saltadas ----
OPCIONES_COMIDA = ["Desayuno", "Media mañana", "Comida", "Merienda", "Cena", "Extra", "Corregir", "Cancelar"]
estimacion: dict | None = None          # comida de fuera esperando a que digas qué sustituye
foto_pendiente: bytes | None = None     # su foto, que se guarda al confirmar


def _comida(texto: str) -> str:
    return texto.strip().lower().replace(" ", "_")


def preguntar_sustituye(est: dict, foto: bytes | None = None):
    global estimacion, pendiente, foto_pendiente
    estimacion, pendiente, foto_pendiente = est, "/sustituye", foto
    return (comidas_txt().texto_estimacion(est) + "\n\n¿Qué comida sustituye? («Extra» si fue algo de más; "
            "«Corregir» si alguna cantidad no cuadra)", OPCIONES_COMIDA)


def comidas_txt():
    from . import comidas
    return comidas


def cmd_fuera(con, args: str):
    try:
        est = comidas_txt().estimar(descripcion=args)
    except Exception as e:  # noqa: BLE001
        return f"❌ No pude estimarla ({type(e).__name__}). Prueba a describirla con más detalle o manda una foto."
    return preguntar_sustituye(est)


def atender_foto(con, foto: bytes, pie: str = ""):
    try:
        est = comidas_txt().estimar(descripcion=pie, foto=foto)
    except Exception as e:  # noqa: BLE001
        return f"❌ No pude estimar la foto ({type(e).__name__}). Prueba otra vez o usa /fuera con una descripción."
    return preguntar_sustituye(est, foto)


def cmd_sustituye(con, args: str):
    global estimacion, foto_pendiente, pendiente
    comidas = comidas_txt()
    c = _comida(args)
    if c == "cancelar" or estimacion is None:
        estimacion = foto_pendiente = None
        return "Vale, no la apunto."
    if c == "corregir":
        pendiente = "/corregir"
        return "✏️ ¿Qué corrijo? Dime cantidades o lo que falte, p. ej. «eran 2 huevos y unos 150 g de lomo» o «sin aceite»."
    if c not in comidas.SUSTITUIBLES + ["extra"]:
        raise medidas.MedidaError("Elige una de las opciones de los botones")
    ahora = datetime.now(TZ)
    fid, error = comidas.guardar_fuera(con, ahora.date(), c, estimacion, comidas.hora_de(c, ahora))
    if foto_pendiente:
        try:
            comidas.guardar_foto(fid, ahora.date(), foto_pendiente)
        except OSError as e:
            print(f"No se pudo guardar la foto: {e}", file=sys.stderr, flush=True)
    nombre, kcal = estimacion["descripcion"], estimacion["kcal"]
    estimacion = foto_pendiente = None
    destino = "como algo extra" if c == "extra" else f"en lugar de la {c.replace('_', ' ')} del menú"
    return (f"✅ Apuntado {destino}: {nombre} (~{comidas.calorias.kc(kcal)} kcal)."
            + (f"\n⚠️ Google Health no la aceptó: {error}. Se reintenta esta noche." if error
               else "\nGuardado también en Google Health."))


def cmd_corregir(con, args: str):
    if estimacion is None:
        return "No hay ninguna comida pendiente de corregir."
    if not args.strip():
        raise medidas.MedidaError("Escribe qué hay que corregir")
    try:
        est = comidas_txt().estimar(foto=foto_pendiente, correccion=args, previa=estimacion,
                                    descripcion=estimacion.get("descripcion", ""))
    except Exception as e:  # noqa: BLE001
        return preguntar_sustituye(estimacion, foto_pendiente)[0].replace("🍽️", f"❌ No pude rehacerla ({type(e).__name__}).\n🍽️", 1), OPCIONES_COMIDA
    return preguntar_sustituye(est, foto_pendiente)


def cmd_saltar(con, args: str) -> str:
    return comidas_txt().saltar(con, datetime.now(TZ).date(), _comida(args))


COMANDOS = {"/peso": cmd_peso, "/medidas": cmd_medidas, "/estado": cmd_estado, "/calorias": cmd_calorias,
            "/hoy": cmd_hoy, "/compra": cmd_compra, "/entreno": cmd_entreno, "/fuera": cmd_fuera,
            "/saltar": cmd_saltar, "/sustituye": cmd_sustituye, "/corregir": cmd_corregir}


# Lista que Telegram enseña al escribir «/» y en el botón Menú
MENU = [
    ("peso", "Anotar tu peso"),
    ("medidas", "Cintura y cuello → % de grasa"),
    ("calorias", "Calorías y macros de esta semana"),
    ("hoy", "El menú de hoy"),
    ("compra", "Lista de la compra de la semana"),
    ("entreno", "Sesión de gimnasio de hoy"),
    ("fuera", "Comida de fuera (o manda una foto)"),
    ("saltar", "Una comida del menú que no hiciste"),
    ("estado", "Última sincronización, peso y semana"),
    ("ayuda", "Comandos y cómo medirte"),
]
# Si mandas /peso o /medidas sin números, el bot los pregunta y espera tu respuesta
PREGUNTAS = {
    "/peso": "⚖️ ¿Cuánto pesas? Escribe solo el número, p. ej. 99,4",
    "/medidas": "📏 Escribe cintura y cuello en cm, p. ej. 102 41 (puedes añadir «peso 99,4»)",
    "/fuera": "🍽️ ¿Qué comiste? Descríbelo (p. ej. «hamburguesa con patatas y una cerveza») o mándame una foto",
    "/saltar": ("¿Qué comida del menú no has hecho hoy?", OPCIONES_COMIDA[:5]),
}
pendiente: str | None = None


def atender(con: sqlite3.Connection, texto: str):
    """Respuesta a un mensaje: texto, o (texto, botones) si hay que elegir una opción."""
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
        anterior, pendiente = pendiente, None
        try:
            respuesta = COMANDOS[orden](con, args)         # puede dejar otra pregunta pendiente
        except medidas.MedidaError as e:
            pendiente = anterior
            return f"❌ {e}" + ("\nPrueba otra vez o toca otro comando." if pendiente else "")
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
            if (msg.get("chat") or {}).get("id") != mi_chat or not (msg.get("text") or msg.get("photo")):
                continue                     # solo tu chat, y solo texto o fotos
            try:
                if msg.get("photo"):
                    telegram.enviar("📸 Recibida, la estoy mirando…")
                    foto = telegram.descargar_foto(msg["photo"][-1]["file_id"])
                    respuesta = atender_foto(con, foto, msg.get("caption", ""))
                else:
                    respuesta = atender(con, msg["text"])
            except Exception as e:  # noqa: BLE001
                respuesta = f"❌ Error interno: {type(e).__name__}: {e}"
                print(respuesta, file=sys.stderr, flush=True)
            if isinstance(respuesta, tuple):
                telegram.enviar(*respuesta)
            else:
                telegram.enviar(respuesta)
        if updates:
            con.execute("INSERT INTO estado VALUES ('bot_offset', ?) "
                        "ON CONFLICT (clave) DO UPDATE SET valor = excluded.valor", (str(offset),))
            con.commit()


def aviso_pesarse(hoy: date, ultimo: date | None) -> str | None:
    """Mensaje de las 6:00, o None si hoy no toca. Sin ninguna pesada: cada día. Lunes: si no
    te has pesado desde el viernes (a las 10:00 sale el plan de martes a lunes). Otros días: cada 3 días si
    llevas más de 10 sin pesarte."""
    dias = (hoy - ultimo).days if ultimo else None
    if dias is None:
        return ("⚖️ Buenos días. Aún no hay ninguna pesada: si puedes, pésate ahora en ayunas, "
                "después del baño.\n/peso 99,4 · /medidas cintura 102 cuello 41")
    if hoy.weekday() == 0 and dias >= 3:
        return ("⚖️ Buenos días, toca pesarse: en ayunas, después del baño. Si puedes, mídete también.\n"
                "/peso 99,4 · /medidas cintura 102 cuello 41\nA las 10:00 te llega el plan de martes a lunes.")
    if dias > 10 and dias % 3 == 2:
        return f"⚖️ Buenos días. Llevas {dias} días sin pesarte: si puedes, hazlo ahora en ayunas.\n/peso 99,4"
    return None


def recordatorio() -> None:
    con = conectar(DB_FILE)
    peso = ultimo_peso(con)
    con.close()
    txt = aviso_pesarse(datetime.now(TZ).date(), date.fromisoformat(peso[0]) if peso else None)
    if txt:
        telegram.enviar(txt)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "recordatorio":
        recordatorio()
    else:
        bucle()
    return 0


if __name__ == "__main__":
    sys.exit(main())
