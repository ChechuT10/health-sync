"""Base de datos SQLite: capa en bruto (raw) y capa limpia (dias, sueno, entrenamientos, pesos)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ESQUEMA = """
-- Cada registro de la API tal cual. Clave estable por tipo: nunca se duplica.
CREATE TABLE IF NOT EXISTS raw (
    tipo        TEXT NOT NULL,
    clave       TEXT NOT NULL,
    fecha       TEXT,             -- fecha civil (Madrid) del registro, AAAA-MM-DD
    json        TEXT NOT NULL,
    actualizado TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tipo, clave)
);
CREATE INDEX IF NOT EXISTS raw_fecha ON raw (tipo, fecha);

-- Capa limpia: se reconstruye entera desde raw en cada sincronización.
-- Un dato que falta es NULL, nunca 0.
CREATE TABLE IF NOT EXISTS dias (
    fecha             TEXT PRIMARY KEY,
    tipo_dia          TEXT NOT NULL,  -- completo, solo_noche, sin_noche, solo_movil, sin_datos
    pasos             INTEGER,
    distancia_km      REAL,
    pisos             INTEGER,
    kcal_total        REAL,
    kcal_activas      REAL,
    min_sedentario    INTEGER,
    azm_quema_grasa   INTEGER,        -- minutos en zona activa (Active Zone Minutes)
    azm_cardio        INTEGER,
    azm_pico          INTEGER,
    cobertura_fc      REAL,           -- fracción del día con pulso registrado (0-1)
    fc_media          REAL,
    fc_max            INTEGER,
    fc_reposo         INTEGER,
    fc_reposo_noche   INTEGER,        -- 1 si la FC en reposo se calculó con sueño (fiable)
    vfc_ms            REAL,
    vfc_profundo_ms   REAL,
    spo2              REAL,
    resp_rpm          REAL,
    sueno_min         INTEGER         -- minutos dormidos de la noche que termina este día
);

CREATE TABLE IF NOT EXISTS sueno (
    id              TEXT PRIMARY KEY,
    fecha           TEXT NOT NULL,    -- día en que te despiertas
    inicio          TEXT NOT NULL,    -- hora local, ISO
    fin             TEXT NOT NULL,
    siesta          INTEGER NOT NULL, -- 0 = noche principal
    tipo            TEXT,             -- STAGES (con fases) o CLASSIC
    min_dormido     INTEGER,
    min_despierto   INTEGER,
    min_ligero      INTEGER,
    min_profundo    INTEGER,
    min_rem         INTEGER,
    min_inquieto    INTEGER           -- solo CLASSIC
);
CREATE INDEX IF NOT EXISTS sueno_fecha ON sueno (fecha);

CREATE TABLE IF NOT EXISTS entrenamientos (
    id              TEXT PRIMARY KEY,
    fecha           TEXT NOT NULL,    -- día local en que empieza
    inicio          TEXT NOT NULL,
    fin             TEXT,
    tipo            TEXT,             -- STRENGTH_TRAINING, WALKING...
    nombre          TEXT,
    duracion_min    REAL,
    kcal            REAL,
    fc_media        INTEGER,
    pasos           INTEGER,
    distancia_km    REAL,
    azm             INTEGER,
    zona_ligera_min    REAL,
    zona_moderada_min  REAL,
    zona_intensa_min   REAL,
    zona_pico_min      REAL
);
CREATE INDEX IF NOT EXISTS entrenamientos_fecha ON entrenamientos (fecha);

CREATE TABLE IF NOT EXISTS pesos (
    id        TEXT PRIMARY KEY,
    fecha     TEXT NOT NULL,
    hora      TEXT,
    peso_kg   REAL,
    grasa_pct REAL,
    origen    TEXT                    -- MANUAL, báscula...
);

-- Registro de cada sincronización, para avisos y depuración
CREATE TABLE IF NOT EXISTS ejecuciones (
    id       INTEGER PRIMARY KEY,
    inicio   TEXT NOT NULL,
    fin      TEXT,
    estado   TEXT,                    -- ok, parcial, error
    detalle  TEXT
);
"""


def conectar(ruta: Path) -> sqlite3.Connection:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta)
    con.execute("PRAGMA journal_mode=WAL")    # menos escrituras en la microSD
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(ESQUEMA)
    return con
