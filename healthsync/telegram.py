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
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{metodo}", datos, timeout=30) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {metodo}: {res.get('description')}")
    return res["result"]


def enviar(texto: str) -> None:
    """Manda un mensaje a tu chat (texto plano; Telegram corta a 4096 caracteres)."""
    _api("sendMessage", chat_id=CHAT_FILE.read_text().strip(), text=texto[:4000])


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
