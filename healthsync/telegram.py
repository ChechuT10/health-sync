"""Avisos por Telegram (bot de la API de Telegram, sin librerías externas).

Secretos en /var/lib/healthsync/secrets/ (permisos 600, usuario healthsync):
  telegram_token    token de @BotFather
  telegram_chat_id  tu chat; lo guarda 'chat-id'

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.telegram chat-id       # tras escribir al bot: guarda tu chat_id
  python -m healthsync.telegram prueba        # manda un mensaje de prueba
  python -m healthsync.telegram fallo UNIDAD  # aviso de fallo (lo lanza systemd con OnFailure=)
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.parse
import urllib.request

from .config import SECRETS

TOKEN_FILE = SECRETS / "telegram_token"
CHAT_FILE = SECRETS / "telegram_chat_id"


def _api(metodo: str, **params) -> dict:
    token = TOKEN_FILE.read_text().strip()
    datos = urllib.parse.urlencode(params).encode()
    espera = int(params.get("timeout", 0)) + 30          # getUpdates espera hasta 'timeout' s
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{metodo}", datos, timeout=espera) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {metodo}: {res.get('description')}")
    return res["result"]


def enviar(texto: str, botones: list[str] | None = None) -> None:
    """Manda un mensaje a tu chat (texto plano; Telegram corta a 4096 caracteres). Con botones, enseña
    un teclado de respuesta rápida (cada botón manda su texto); sin ellos, quita el que hubiera."""
    teclado = ({"keyboard": [[{"text": b} for b in botones[i:i + 3]] for i in range(0, len(botones), 3)],
                "one_time_keyboard": True, "resize_keyboard": True} if botones else {"remove_keyboard": True})
    _api("sendMessage", chat_id=CHAT_FILE.read_text().strip(), text=texto[:4000],
         reply_markup=json.dumps(teclado, ensure_ascii=False))


def descargar_foto(file_id: str) -> bytes:
    """Descarga una foto que te han mandado (la versión más grande que pase el bot)."""
    ruta = _api("getFile", file_id=file_id)["file_path"]
    token = TOKEN_FILE.read_text().strip()
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{ruta}", timeout=60) as r:
        return r.read()



def enviar_archivo(ruta, texto: str = "") -> None:
    """Manda un archivo (el PDF del menú) a tu chat, con un pie de hasta 1024 caracteres."""
    import uuid
    from pathlib import Path
    ruta = Path(ruta)
    limite = uuid.uuid4().hex
    campos = {"chat_id": CHAT_FILE.read_text().strip(), "caption": texto[:1000]}
    cuerpo = b"".join(f"--{limite}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
                      for k, v in campos.items())
    cuerpo += (f"--{limite}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{ruta.name}\"\r\n"
               f"Content-Type: application/pdf\r\n\r\n").encode() + ruta.read_bytes() + f"\r\n--{limite}--\r\n".encode()
    token = TOKEN_FILE.read_text().strip()
    pet = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument", cuerpo,
                                 headers={"Content-Type": f"multipart/form-data; boundary={limite}"})
    with urllib.request.urlopen(pet, timeout=60) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram sendDocument: {res.get('description')}")

def guardar_chat_id() -> int:
    chats = {}
    for u in _api("getUpdates"):
        chat = (u.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats[chat["id"]] = chat.get("username") or chat.get("first_name")
    if not chats:
        print("No hay mensajes privados. Escribe algo a tu bot en Telegram y repite.")
        return 1
    if len(chats) > 1:
        print("Hay mensajes de varias personas; guarda a mano el tuyo en", CHAT_FILE)
        for cid, nombre in chats.items():
            print(f"  {cid}  {nombre}")
        return 1
    (cid, nombre), = chats.items()
    CHAT_FILE.write_text(f"{cid}\n")
    CHAT_FILE.chmod(0o600)
    print(f"Guardado el chat de {nombre} en {CHAT_FILE}")
    enviar("✅ Health Sync conectado. Aquí te llegarán los avisos.")
    return 0


def aviso_fallo(unidad: str) -> None:
    """Últimas líneas del registro de la unidad que falló (necesita el grupo systemd-journal)."""
    log = subprocess.run(
        ["journalctl", "-u", unidad, "-n", "15", "--no-pager", "-o", "cat"],
        capture_output=True, text=True, check=False).stdout.strip()
    enviar(f"⚠️ Health Sync: falló {unidad}\n\n{log or '(sin registro)'}\n\n"
           f"Más detalle: journalctl -u {unidad}")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("chat-id", "prueba", "fallo"):
        print(__doc__)
        return 2
    orden = sys.argv[1]
    if orden == "chat-id":
        return guardar_chat_id()
    if orden == "prueba":
        enviar("🧪 Mensaje de prueba de Health Sync.")
    else:
        aviso_fallo(sys.argv[2] if len(sys.argv) > 2 else "healthsync-sync.service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
