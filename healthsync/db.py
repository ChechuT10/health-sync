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

-- Medidas que mandas por el bot. También se escriben en Google Health (gh_peso, gh_grasa
-- guardan su id); mientras no estén allí, las tablas limpias usan esta copia.
CREATE TABLE IF NOT EXISTS medidas (
    id          INTEGER PRIMARY KEY,
    momento     TEXT NOT NULL,    -- UTC, ISO (…Z)
    fecha       TEXT NOT NULL,    -- fecha civil de Madrid
    hora        TEXT NOT NULL,
    peso_kg     REAL,
    cintura_cm  REAL,
    cuello_cm   REAL,
    grasa_pct   REAL,             -- método de la Marina de EE. UU.
    gh_peso     TEXT,
    gh_grasa    TEXT
);

-- Objetivo de cada semana (lo calcula healthsync.calorias; la IA no lo toca)
CREATE TABLE IF NOT EXISTS plan_semana (
    semana             TEXT PRIMARY KEY,  -- martes en que empieza (de martes a lunes)
    numero             INTEGER NOT NULL,
    kcal               INTEGER NOT NULL,
    proteina_g         INTEGER NOT NULL,
    grasa_g            INTEGER NOT NULL,
    hidratos_g         INTEGER NOT NULL,
    peso_kg            REAL,
    metabolismo_basal  INTEGER,
    gasto_estimado     INTEGER,
    gasto_real         INTEGER,
    ritmo_pct          REAL,              -- % del peso perdido por semana (positivo = baja)
    fase               TEXT NOT NULL,     -- deficit o descanso
    motivo             TEXT,
    creado             TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Menú de cada semana: lo monta Gemini; healthsync.menu solo comprueba que cuadra
CREATE TABLE IF NOT EXISTS menu_semana (
    semana         TEXT PRIMARY KEY,  -- martes en que empieza, como plan_semana
    menu           TEXT NOT NULL,     -- JSON tal como lo devolvió Gemini
    investigacion  TEXT,              -- lo que encontró Gemini buscando productos esa semana
    revision       TEXT,              -- totales por día calculados por la Pi (JSON)
    origen         TEXT NOT NULL,     -- ia, ia_corregido (tras devolverle errores) o repetido
    creado         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Gimnasio (healthsync.entreno). Bloque = rutina de varias semanas; empieza un martes
CREATE TABLE IF NOT EXISTS rutina_bloque (
    inicio   TEXT PRIMARY KEY,  -- martes de la primera semana
    rutina   TEXT NOT NULL,     -- JSON: días, ejercicios, variantes, series, reps, RIR…
    origen   TEXT NOT NULL,     -- base, ia (renovación) o ia_parcial (cambio de algunos ejercicios)
    motivo   TEXT,
    creado   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Peso de partida por variante, antes de tener historial (lo que contaste al empezar o lo que estima Gemini)
CREATE TABLE IF NOT EXISTS pesos_iniciales (
    variante  TEXT PRIMARY KEY,
    kg        REAL,
    nota      TEXT
);

-- Cada semana: el plan con los objetivos, la pestaña del Sheet y los consejos de Gemini
CREATE TABLE IF NOT EXISTS entreno_semana (
    semana         TEXT PRIMARY KEY,  -- martes
    plan           TEXT NOT NULL,     -- JSON: días con ejercicios, variante elegida y objetivos
    pestana        TEXT,
    recuperacion   TEXT,              -- verde, ambar, rojo o sin_datos
    consejos       TEXT,
    creado         TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cada serie que apuntas en el Sheet (clave: id oculto de la fila)
CREATE TABLE IF NOT EXISTS series (
    id          TEXT PRIMARY KEY,   -- semana|día|ejercicio|serie
    semana      TEXT NOT NULL,
    dia         TEXT NOT NULL,      -- P1, T1, P2, T2
    ejercicio   TEXT NOT NULL,
    variante    TEXT NOT NULL,
    serie       INTEGER NOT NULL,
    kg          REAL,
    reps        INTEGER,
    rir         REAL,
    notas       TEXT,
    leido       TEXT NOT NULL DEFAULT (datetime('now')),
    objetivo    TEXT                -- el que ponía el Sheet al apuntarla (columna Objetivo)
);
CREATE INDEX IF NOT EXISTS series_variante ON series (variante, semana);

-- Valoración de cada sesión de gimnasio (Gemini, con el Sheet y el reloj); una por día de la rutina
CREATE TABLE IF NOT EXISTS analisis_entreno (
    id      TEXT PRIMARY KEY,   -- semana|día
    fecha   TEXT NOT NULL,      -- día en que entrenaste
    sesion  TEXT,               -- id del entrenamiento del reloj (NULL si no lo llevabas)
    texto   TEXT NOT NULL,
    creado  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Lo que se come cada día (healthsync.comidas): las del menú (se escriben a las 23:30), las de fuera
-- (foto o descripción al bot, estimadas por Gemini) y las saltadas. Una fila por comida.
CREATE TABLE IF NOT EXISTS comidas (
    id          INTEGER PRIMARY KEY,
    fecha       TEXT NOT NULL,      -- día civil (Madrid)
    comida      TEXT NOT NULL,      -- desayuno, media_mañana, comida, merienda, cena, fijos o extra
    origen      TEXT NOT NULL,      -- plan, fuera o saltada
    nombre      TEXT NOT NULL,
    kcal        REAL,
    proteina    REAL,
    grasa       REAL,
    hidratos    REAL,
    kcal_min    REAL,               -- comidas de fuera: rango de la estimación
    kcal_max    REAL,
    hora        TEXT,               -- HH:MM
    gh_name     TEXT,               -- id del dato en Google Health (vacío = no escrito)
    creado      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS comidas_fecha ON comidas (fecha);

-- Valores sueltos del sistema (p. ej. el último mensaje leído por el bot)
CREATE TABLE IF NOT EXISTS estado (
    clave  TEXT PRIMARY KEY,
    valor  TEXT
);

-- Registros de la API que no se usan (pruebas, errores). Se quedan en raw, pero las
-- tablas limpias los ignoran. Se añaden con: python -m healthsync.sync --descartar TIPO CLAVE
CREATE TABLE IF NOT EXISTS descartes (
    tipo    TEXT NOT NULL,
    clave   TEXT NOT NULL,
    motivo  TEXT,
    fecha   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tipo, clave)
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
    con = sqlite3.connect(ruta, timeout=30)   # el bot y la sincronización pueden coincidir
    con.execute("PRAGMA journal_mode=WAL")    # menos escrituras en la microSD
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(ESQUEMA)
    # Columnas añadidas después de crear la base de datos de la Pi
    if "objetivo" not in {f[1] for f in con.execute("PRAGMA table_info(series)")}:
        con.execute("ALTER TABLE series ADD COLUMN objetivo TEXT")
    return con
