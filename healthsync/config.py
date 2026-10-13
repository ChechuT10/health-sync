"""Rutas y constantes compartidas. HEALTHSYNC_DATA permite usar otra carpeta para pruebas."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

DATA_DIR = Path(os.environ.get("HEALTHSYNC_DATA", "/var/lib/healthsync"))
SECRETS = DATA_DIR / "secrets"
CLIENT_FILE = SECRETS / "client_secret.json"
TOKEN_FILE = SECRETS / "token.json"
DB_FILE = DATA_DIR / "db" / "salud.db"
# Configuración personal (no se sube a GitHub): copia de config.example.toml
CONFIG_FILE = Path(os.environ.get("HEALTHSYNC_CONFIG", Path(__file__).resolve().parent.parent / "config.toml"))

API = "https://health.googleapis.com/v4/users/me"
TZ = ZoneInfo("Europe/Madrid")

# Las tablas limpias empiezan aquí; lo anterior se guarda en raw pero no se usa
FECHA_INICIO = date.fromisoformat(os.environ.get("HEALTHSYNC_INICIO", "2026-09-01"))

# Tipo de día (ver docs/datos.md): cobertura = fracción del día con pulso registrado
COBERTURA_SIN_RELOJ = 0.30   # por debajo, el reloj no se llevó de día
COBERTURA_COMPLETO = 0.50    # con noche y al menos esto, el día es completo
