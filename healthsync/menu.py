"""Menú semanal: lo monta Gemini; la Pi solo comprueba que cuadra, lo guarda, lo imprime y lo manda.

Cada lunes, después de las calorías (healthsync.calorias), para la semana de martes a lunes:
  1. Gemini busca en internet productos actuales de tus supermercados que encajen (con sus valores).
  2. Gemini monta el menú en JSON: alimentos con valores por 100 g, platos, qué se come cada día,
     lista de la compra y consejos. Recibe las calorías y macros, no tu peso ni tu % de grasa.
  3. La Pi suma gramos × valores y comprueba cada día contra el plan (y los valores de los
     alimentos contra la tabla, si están en ella). Si algo no cuadra, se lo devuelve a Gemini
     para que lo corrija (2 veces como mucho); si sigue mal, repite el menú anterior si cuadra
     con las calorías nuevas, y si no, avisa.
  4. PDF con Typst → impresora (CUPS) → Telegram.

Uso (como usuario healthsync, desde /opt/healthsync/app):
  python -m healthsync.menu                    # monta el menú de la semana que viene y lo enseña
  python -m healthsync.menu --guardar          # además lo guarda, hace el PDF y lo manda por Telegram
  python -m healthsync.menu --guardar --imprimir
  python -m healthsync.menu --pdf              # rehace el PDF del menú guardado (sin llamar a Gemini)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from datetime import date, datetime, timedelta
from pathlib import Path

from . import alimentos as tabla_alimentos
from . import calorias
from .config import CONFIG_FILE, DATA_DIR, DB_FILE, TZ
from .db import conectar
from .gemini import GeminiError, generar
from .medidas import es

DIAS = ["martes", "miércoles", "jueves", "viernes", "sábado", "domingo", "lunes"]
COMIDAS = ["desayuno", "media_mañana", "comida", "merienda", "cena"]
NOMBRE_COMIDA = {"desayuno": "Desayuno", "media_mañana": "Media mañana", "comida": "Comida",
                 "merienda": "Merienda", "cena": "Cena"}
OBLIGATORIAS = ["desayuno", "comida", "merienda", "cena"]

# Tus preferencias. Se pueden cambiar en config.toml, sección [menu] (mismos nombres)
PREFERENCIAS = {
    # Alias: siempre el último Flash (los Pro, más caros y lentos, van aparte). El 14/10/2026 = 3.8 Flash
    "modelo": "gemini-flash-latest",
    "razonamiento": "high",          # cuánto piensa Gemini al montar el menú (low, medium, high)
    "supermercados": ["Mercadona", "Lidl", "Dia", "Ahorramás"],
    "horario": {"desayuno": "8:00", "media_mañana": "11:00", "comida": "14:00", "merienda": "18:00",
                "cena": "22:00"},
    "dias_entreno": ["lunes", "martes", "jueves", "viernes"],
    "hora_entreno": "18:00-19:00",
    # Lo que se toma todos los días, fuera de los platos: 3 cafés con leche (150 ml cada uno) y creatina
    "fijos": {"leche_semi": 450, "cafe": 150, "creatina": 5},
    "fijos_texto": "3 cafés con leche al día (150 ml de leche semidesnatada cada uno, sin azúcar) y 5 g de creatina",
    "no_come": ["lentejas", "judías secas (las judías verdes sí)", "marisco", "chocolate y cacao", "coliflor"],
    "poco": ["pescado: como mucho 2 días por semana, y mejor en formatos que gusten (atún, a la plancha con salsa)"],
    "cocina": ("Cocina a diario o en táper, cosas rápidas: plancha, freidora de aire (patata, verdura), "
               "brócoli al vapor o microondas. Entre semana nada de más de 15-20 minutos; el fin de semana "
               "vale algo más elaborado (por ejemplo, tortitas de avena en el desayuno)."),
    "otros": ("Repite platos: 2 desayunos de diario + 1 de fin de semana, 3 comidas y 3 cenas que se alternan, "
              "para que la compra y los tápers sean fáciles. Proteína en polvo aislada disponible (por ejemplo, "
              "en alguna merienda). Si una comida sale con demasiado volumen, puedes pasar parte a media mañana."),
}
# Palabras que no pueden aparecer en ningún alimento ni plato (la Pi lo comprueba)
PROHIBIDO = [r"lentejas?", r"jud[ií]as?(?!\s+verdes?)", r"alubias?", r"marisco", r"gambas?", r"langostinos?",
             r"mejill[oó]n(es)?", r"almejas?", r"berberechos?", r"calamar(es)?", r"pulpo", r"sepia",
             r"chipirones?", r"cigalas?", r"cangrejo", r"chocolate", r"cacao", r"coliflor"]

# Margen de cada día respecto al plan (proteína: mínimo -5 %, puede pasarse un poco)
MARGEN = {"kcal": (0.05, 0.05), "proteina": (0.05, 0.15), "grasa": (0.15, 0.15), "hidratos": (0.15, 0.15)}
CORRECCIONES = 2
# Tope de tokens generados por llamada (razonamiento incluido): lo que más cuesta
# Medido el 14/10/2026 con 3.8 Flash: búsqueda ~4 k (2,2 k + 1,8 k pensando); menú ~51 k (11 k + 40 k pensando)
MAX_SALIDA = {"busqueda": 16384, "menu": 65536}


class MenuError(Exception):
    pass


def preferencias() -> dict:
    p = dict(PREFERENCIAS)
    if CONFIG_FILE.exists():
        conf = tomllib.loads(CONFIG_FILE.read_text())
        p.update(conf.get("menu", {}))
        p["modelo"] = conf.get("menu", {}).get("modelo") or conf.get("ia", {}).get("modelo_menu") or p["modelo"]
    return p


# --------------------------------------------------------------------------
# Objetivos
# --------------------------------------------------------------------------
def objetivo_dia(plan: calorias.Propuesta) -> dict[str, float]:
    return {"kcal": plan.kcal, "proteina": plan.proteina_g, "grasa": plan.grasa_g, "hidratos": plan.hidratos_g}


def suma_fijos(prefs: dict) -> dict[str, float]:
    t = tabla_alimentos.tabla()
    total = dict.fromkeys(("kcal", "proteina", "grasa", "hidratos"), 0.0)
    for ident, g in prefs["fijos"].items():
        for k, v in t[ident].macros(g).items():
            if k in total:
                total[k] += v
    return total


def para_platos(objetivo: dict, fijos: dict) -> dict[str, int]:
    return {k: round(objetivo[k] - fijos[k]) for k in objetivo}


# --------------------------------------------------------------------------
# Peticiones a Gemini
# --------------------------------------------------------------------------
def prompt_busqueda(prefs: dict, semana: date) -> str:
    return f"""Eres un dietista-nutricionista en España. Busca en internet productos que se vendan AHORA
(semana del {semana:%d/%m/%Y}) en {', '.join(prefs['supermercados'])} y que encajen en una dieta de
recomposición corporal (déficit moderado, mucha proteína): proteína magra (carne, huevos, lácteos altos
en proteína, alguna conserva), hidratos poco procesados, verdura y fruta de temporada, y productos
prácticos para cocinar rápido (plancha, freidora de aire, táper). Prioriza novedades sanas y buena
relación calidad-precio.

No come: {', '.join(prefs['no_come'])}. Poco: {', '.join(prefs['poco'])}.

Dame 15-25 productos: nombre exacto, supermercado, formato y precio aproximado, y valores por 100 g según
la etiqueta (kcal, proteína, grasa, hidratos), diciendo de dónde salen. Añade 5-10 frutas y verduras de
temporada en octubre en España. Sé concreto y breve: solo la lista."""


def prompt_menu(prefs: dict, semana: date, objetivo: dict, platos_obj: dict, investigacion: str,
                anteriores: list[str]) -> str:
    entreno = [d for d in DIAS if d in prefs["dias_entreno"]]
    tabla_csv = "\n".join(f"{a.id}: {a.nombre} ({a.estado}) {a.kcal:g} kcal, P {a.proteina:g}, G {a.grasa:g}, "
                          f"H {a.hidratos:g}" for a in tabla_alimentos.tabla().values() if a.grupo != "suplemento"
                          or a.id == "proteina_polvo")
    horario = ", ".join(f"{NOMBRE_COMIDA[c].lower()} {h}" for c, h in prefs["horario"].items())
    return f"""Eres un dietista-nutricionista en España. Monta el menú de una semana, de martes
{semana:%d/%m} a lunes {semana + timedelta(days=6):%d/%m} (se compra el martes).

OBJETIVO DIARIO (igual todos los días): {objetivo['kcal']} kcal, {objetivo['proteina']} g de proteína,
{objetivo['grasa']} g de grasa y {objetivo['hidratos']} g de hidratos.
Fijos que ya están contados y NO debes incluir en los platos: {prefs['fijos_texto']}.
Por tanto, LA SUMA DE LOS PLATOS DE CADA DÍA debe dar {platos_obj['kcal']} kcal (±4 %),
{platos_obj['proteina']} g de proteína (o un poco más), {platos_obj['grasa']} g de grasa y
{platos_obj['hidratos']} g de hidratos (±10 %). Haz las cuentas con cuidado: un programa las comprueba.

HORARIO: {horario} (la media mañana es opcional). Entrena {', '.join(entreno)} a las
{prefs['hora_entreno']}: esos días la merienda es antes de entrenar (con hidratos fáciles de digerir y algo
de proteína) y la cena es después (con proteína e hidratos). Algunas semanas cambia un entreno al miércoles,
así que todos los días deben sumar lo mismo, para poder intercambiar el día entero.

NO COME: {', '.join(prefs['no_come'])}. POCO: {', '.join(prefs['poco'])}.
COCINA: {prefs['cocina']}
{prefs['otros']}
Supermercados: {', '.join(prefs['supermercados'])}.
{"Platos de las semanas anteriores (varía un poco, sin repetir todo): " + "; ".join(anteriores) if anteriores else ""}

PRODUCTOS ENCONTRADOS ESTA SEMANA (puedes usarlos, sobre todo si son novedades que encajan):
{investigacion or "(no hay)"}

TABLA DE REFERENCIA (alimentos habituales; valores por 100 g). Puedes usar cualquier otro alimento:
{tabla_csv}

CALIDAD (un programa revisa todo esto):
- Cantidades que una persona usaría de verdad: nada de 1-4 g de nada ("unas gotas", "spray", "2 mitades"
  de nuez). Aceite: 0 g o al menos 5 g (1 cucharadita). Frutos secos: 0 o al menos 10 g. Si sobra o falta
  grasa, ajusta la proteína (pollo ↔ muslo, claras ↔ huevo entero) o el aceite en otra comida.
- Variedad dentro de la repetición: al menos 4 proteínas principales distintas en la semana; el mismo día,
  la comida y la cena con proteínas distintas; 2 meriendas realmente distintas (no la misma con otro nombre).
- Cada plato debe tener sentido como plato (cómo se come y se cocina): no mezclas raras para cuadrar números.
- Los valores por 100 g, siempre en el mismo estado que los gramos (arroz y pasta en crudo: ~350 kcal).
- Antes de responder, suma tú mismo cada día y comprueba que cuadra.

CÓMO RESPONDER:
- "alimentos": cada alimento que uses, con valores por 100 g EN EL ESTADO EN QUE SE PESA (la carne, el
  pescado, el arroz y la pasta, en crudo). Si está en la tabla, pon su id en "id_tabla" y usa sus valores;
  si es un producto concreto, los de su etiqueta. "fuente": de dónde salen (BEDCA, etiqueta, web…).
- "platos": cada plato una vez, con los gramos de cada ingrediente (múltiplos de 5 g; huevos y fruta en
  unidades enteras: pon el peso y en "medida" algo como "2 unidades"). "preparacion": 1-3 frases.
- "dias": los 7 días, en orden de martes a lunes, cada uno con su tipo (entreno o descanso) y el id del plato
  de cada comida. Si un plato necesita otra cantidad otro día, crea otro plato (p. ej. "C1b").
- "compra": la lista de la compra para toda la semana, por sección del súper, con cantidades en formatos
  reales (bandejas, paquetes, unidades) y la tienda. La compra se hace de una vez el martes: usa UN
  supermercado principal y como mucho otro para algo concreto. Incluye la leche de los cafés.
- "consejos": 2-5 consejos prácticos (qué cocinar el martes para varios días, cómo guardar los tápers…).
- "resumen": una frase sobre el menú de la semana."""


def esquema() -> dict:
    num = {"type": "number"}
    txt = {"type": "string"}

    def obj(props: dict) -> dict:
        return {"type": "object", "properties": props, "required": list(props)}

    return obj({
        "resumen": txt,
        "alimentos": {"type": "array", "items": obj({
            "id": txt, "nombre": txt, "tienda": txt,
            "estado": {"type": "string", "enum": ["crudo", "cocido", "tal cual", "escurrido", "pelado"]},
            "kcal": num, "proteina": num, "grasa": num, "hidratos": num, "id_tabla": txt, "fuente": txt})},
        "platos": {"type": "array", "items": obj({
            "id": txt, "comida": {"type": "string", "enum": COMIDAS}, "nombre": txt, "preparacion": txt,
            "minutos": {"type": "integer"},
            "ingredientes": {"type": "array", "items": obj({"alimento": txt, "gramos": num, "medida": txt})}})},
        "dias": {"type": "array", "items": obj({
            "dia": {"type": "string", "enum": DIAS}, "tipo": {"type": "string", "enum": ["entreno", "descanso"]},
            "comidas": {"type": "array", "items": obj({
                "comida": {"type": "string", "enum": COMIDAS}, "plato": txt})}})},
        "compra": {"type": "array", "items": obj({"producto": txt, "cantidad": txt, "tienda": txt, "seccion": txt})},
        "consejos": {"type": "array", "items": txt},
    })


# --------------------------------------------------------------------------
# Revisión (lo único que calcula la Pi)
# --------------------------------------------------------------------------
def revisar(menu: dict, objetivo: dict, fijos: dict, prefs: dict) -> tuple[list[str], dict]:
    """Errores (vacío = vale) y totales de cada día, fijos incluidos."""
    errores: list[str] = []
    ref = tabla_alimentos.tabla()
    # «sin coliflor» no cuenta: solo si no va justo después de «sin»
    prohibido = re.compile(r"(?<!\bsin )\b(" + "|".join(PROHIBIDO) + r")\b", re.IGNORECASE)

    alim = {a["id"]: a for a in menu.get("alimentos", [])}
    for a in alim.values():
        calc = 4 * a["proteina"] + 9 * a["grasa"] + 4 * a["hidratos"]
        if a["proteina"] + a["grasa"] + a["hidratos"] > 101 or min(a["kcal"], a["proteina"], a["grasa"], a["hidratos"]) < 0:
            errores.append(f"Alimento {a['id']}: valores imposibles por 100 g.")
        elif abs(calc - a["kcal"]) > max(25, 0.2 * a["kcal"]):
            errores.append(f"Alimento {a['id']}: {a['kcal']:g} kcal no cuadra con sus macros ({calc:.0f} kcal).")
        r = ref.get(a.get("id_tabla") or "")
        if r and (abs(r.kcal - a["kcal"]) > max(25, 0.2 * r.kcal) or abs(r.proteina - a["proteina"]) > max(3, 0.2 * r.proteina)):
            errores.append(f"Alimento {a['id']}: valores distintos de la tabla ({r.id}: {r.kcal:g} kcal, "
                           f"{r.proteina:g} g de proteína por 100 g).")
        if m := prohibido.search(a["nombre"]):
            errores.append(f"Alimento {a['id']} ({a['nombre']}): lleva «{m.group(0)}», que no come.")

    platos = {p["id"]: p for p in menu.get("platos", [])}
    for p in platos.values():
        if m := prohibido.search(p["nombre"]):
            errores.append(f"Plato {p['id']} ({p['nombre']}): lleva «{m.group(0)}», que no come.")
        for i in p["ingredientes"]:
            if i["alimento"] not in alim:
                errores.append(f"Plato {p['id']}: el alimento {i['alimento']} no está en «alimentos».")
            elif not 5 <= i["gramos"] <= 800:
                errores.append(f"Plato {p['id']}: {i['gramos']:g} g de {i['alimento']} no es una cantidad "
                               "razonable (mínimo 5 g; si no hace falta, quítalo).")

    dias = menu.get("dias", [])
    if [d["dia"] for d in dias] != DIAS:
        errores.append(f"«dias» debe tener los 7 días en orden ({', '.join(DIAS)}).")
    totales: dict[str, dict] = {}
    for d in dias:
        tipo_bueno = "entreno" if d["dia"] in prefs["dias_entreno"] else "descanso"
        if d["tipo"] != tipo_bueno:
            errores.append(f"El {d['dia']} es de {tipo_bueno}, no de {d['tipo']}.")
        faltan = [c for c in OBLIGATORIAS if c not in {x["comida"] for x in d["comidas"]}]
        if faltan:
            errores.append(f"Al {d['dia']} le falta: {', '.join(faltan)}.")
        t = dict(fijos)
        for c in d["comidas"]:
            p = platos.get(c["plato"])
            if p is None:
                errores.append(f"El {d['dia']} usa el plato {c['plato']}, que no existe.")
                continue
            for i in p["ingredientes"]:
                a = alim.get(i["alimento"])
                if a:
                    for k in t:
                        t[k] += a[k] * i["gramos"] / 100
        totales[d["dia"]] = {k: round(v) for k, v in t.items()}
        fuera = []
        for k, (abajo, arriba) in MARGEN.items():
            if not objetivo[k] * (1 - abajo) <= t[k] <= objetivo[k] * (1 + arriba):
                fuera.append(f"{k} {t[k]:.0f} (objetivo {objetivo[k]:.0f})")
        if fuera:
            errores.append(f"El {d['dia']} no cuadra (con los fijos): {'; '.join(fuera)}. "
                           f"Los platos suman {t['kcal'] - fijos['kcal']:.0f} kcal y deberían sumar "
                           f"{objetivo['kcal'] - fijos['kcal']:.0f}.")
    return errores, totales


# --------------------------------------------------------------------------
# Montar el menú
# --------------------------------------------------------------------------
def anteriores(con: sqlite3.Connection, semana: date) -> list[dict]:
    filas = con.execute("SELECT menu FROM menu_semana WHERE semana < ? ORDER BY semana DESC LIMIT 2",
                        (semana.isoformat(),)).fetchall()
    return [json.loads(f[0]) for f in filas]


def montar(con: sqlite3.Connection, semana: date, plan: calorias.Propuesta, log=print) -> dict:
    """Pide el menú a Gemini y lo revisa. Devuelve {menu, investigacion, revision, origen}."""
    prefs = preferencias()
    objetivo = objetivo_dia(plan)
    fijos = suma_fijos(prefs)
    previos = anteriores(con, semana)
    nombres = sorted({p["nombre"] for m in previos for p in m.get("platos", [])})

    investigacion = ""
    try:
        log("Gemini: buscando productos…")
        investigacion = generar(prefs["modelo"], prompt_busqueda(prefs, semana), buscar=True,
                                max_salida=MAX_SALIDA["busqueda"])
    except GeminiError as e:
        log(f"Sin búsqueda de productos ({e}); sigo con la tabla.")

    prompt = prompt_menu(prefs, semana, objetivo, para_platos(objetivo, fijos), investigacion, nombres)
    errores: list[str] = []
    menu = None
    for intento in range(1 + CORRECCIONES):
        log(f"Gemini: montando el menú (intento {intento + 1})…")
        texto = prompt
        if menu is not None:
            texto += ("\n\nTU PROPUESTA ANTERIOR:\n" + json.dumps(menu, ensure_ascii=False) +
                      "\n\nUN PROGRAMA LA HA REVISADO Y HA ENCONTRADO ESTOS ERRORES. Corrígelos (ajusta gramos "
                      "o cambia platos) y devuelve el menú completo:\n- " + "\n- ".join(errores))
        try:
            menu = json.loads(generar(prefs["modelo"], texto, esquema=esquema(),
                                      razonamiento=prefs["razonamiento"], max_salida=MAX_SALIDA["menu"],
                                      timeout=1200))
        except (GeminiError, json.JSONDecodeError) as e:
            errores = [f"Respuesta no válida: {e}"]
            log(errores[0])
            menu = None                                       # se vuelve a pedir desde cero
            continue
        errores, totales = revisar(menu, objetivo, fijos, prefs)
        if not errores:
            return {"menu": menu, "investigacion": investigacion, "revision": totales,
                    "origen": "ia_corregido" if intento else "ia"}
        log("La revisión encontró:\n- " + "\n- ".join(errores))

    for m in previos[:1]:                                     # el último menú, si cuadra con el plan nuevo
        err, totales = revisar(m, objetivo, fijos, prefs)
        if not err:
            log("Se repite el menú de la semana anterior.")
            return {"menu": m, "investigacion": investigacion, "revision": totales, "origen": "repetido"}
    raise MenuError("Gemini no ha dado un menú que cuadre:\n- " + "\n- ".join(errores[:8]))


def guardar(con: sqlite3.Connection, semana: date, r: dict) -> None:
    con.execute(
        """INSERT INTO menu_semana (semana, menu, investigacion, revision, origen) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (semana) DO UPDATE SET menu = excluded.menu, investigacion = excluded.investigacion,
               revision = excluded.revision, origen = excluded.origen, creado = datetime('now')""",
        (semana.isoformat(), json.dumps(r["menu"], ensure_ascii=False), r["investigacion"],
         json.dumps(r["revision"], ensure_ascii=False), r["origen"]))
    con.commit()


def cargar(con: sqlite3.Connection, semana: date) -> dict | None:
    f = con.execute("SELECT menu, investigacion, revision, origen FROM menu_semana WHERE semana = ?",
                    (semana.isoformat(),)).fetchone()
    if f is None:
        return None
    return {"menu": json.loads(f[0]), "investigacion": f[1], "revision": json.loads(f[2] or "{}"), "origen": f[3]}


# --------------------------------------------------------------------------
# Texto (bot) y PDF
# --------------------------------------------------------------------------
def _ingrediente(i: dict, alim: dict) -> str:
    nombre = alim.get(i["alimento"], {}).get("nombre", i["alimento"])
    return f"{nombre} {i['gramos']:g} g" + (f" ({i['medida']})" if i.get("medida") else "")


def texto_dia(r: dict, dia: str, prefs: dict | None = None) -> str:
    prefs = prefs or preferencias()
    menu = r["menu"]
    alim = {a["id"]: a for a in menu["alimentos"]}
    platos = {p["id"]: p for p in menu["platos"]}
    d = next(x for x in menu["dias"] if x["dia"] == dia)
    lineas = [f"🍽️ {dia.capitalize()} ({'entreno' if d['tipo'] == 'entreno' else 'descanso'})"]
    for c in sorted(d["comidas"], key=lambda x: COMIDAS.index(x["comida"])):
        p = platos[c["plato"]]
        lineas += ["", f"{NOMBRE_COMIDA[c['comida']]} ({prefs['horario'].get(c['comida'], '')}): {p['nombre']}"]
        lineas += [f"  • {_ingrediente(i, alim)}" for i in p["ingredientes"]]
    t = r["revision"].get(dia)
    if t:
        lineas += ["", f"Total con los fijos: {calorias.kc(t['kcal'])} kcal · P {t['proteina']} · "
                       f"G {t['grasa']} · H {t['hidratos']}"]
    lineas.append(f"Fijos: {prefs['fijos_texto']}.")
    return "\n".join(lineas)


def texto_compra(r: dict) -> str:
    por_seccion: dict[str, list[str]] = {}
    for c in r["menu"]["compra"]:
        por_seccion.setdefault(c["seccion"] or "Otros", []).append(
            f"  ☐ {c['producto']}: {c['cantidad']}" + (f" ({c['tienda']})" if c["tienda"] else ""))
    return "🛒 Lista de la compra\n" + "\n".join(f"\n{s}\n" + "\n".join(v) for s, v in por_seccion.items())


def datos_pdf(semana: date, plan: calorias.Propuesta, r: dict, prefs: dict) -> dict:
    menu = r["menu"]
    alim = {a["id"]: a for a in menu["alimentos"]}
    platos = {p["id"]: p for p in menu["platos"]}
    con_media = any(c["comida"] == "media_mañana" for d in menu["dias"] for c in d["comidas"])
    cols = [c for c in COMIDAS if c != "media_mañana" or con_media]
    dias = []
    for n, d in enumerate(menu["dias"]):
        celdas = {c["comida"]: c["plato"] for c in d["comidas"]}
        dias.append({
            "dia": d["dia"].capitalize(), "fecha": f"{semana + timedelta(days=n):%d/%m}",
            "entreno": d["tipo"] == "entreno",
            "comidas": [{"nombre": platos[celdas[c]]["nombre"],
                         "ingredientes": [_ingrediente(i, alim) for i in platos[celdas[c]]["ingredientes"]]}
                        if c in celdas and celdas[c] in platos else None for c in cols],
            "total": r["revision"].get(d["dia"], {}),
        })
    secciones: dict[str, list] = {}
    for c in menu["compra"]:
        secciones.setdefault(c["seccion"] or "Otros", []).append(c)
    return {
        "titulo": f"Semana {plan.numero} · martes {semana:%d/%m} – lunes {semana + timedelta(days=6):%d/%m}",
        "objetivo": f"{calorias.kc(plan.kcal)} kcal · proteína {plan.proteina_g} g · grasa {plan.grasa_g} g · "
                    f"hidratos {plan.hidratos_g} g" + (" · descanso de dieta" if plan.fase == "descanso" else ""),
        "fijos": prefs["fijos_texto"],
        "columnas": [f"{NOMBRE_COMIDA[c]} {prefs['horario'].get(c, '')}" for c in cols],
        "dias": dias,
        "platos": [{"nombre": p["nombre"], "minutos": p["minutos"], "preparacion": p["preparacion"],
                    "ingredientes": [_ingrediente(i, alim) for i in p["ingredientes"]]}
                   for p in menu["platos"]],
        "compra": [{"seccion": s, "productos": v} for s, v in secciones.items()],
        "consejos": menu["consejos"],
        "resumen": menu["resumen"],
        "nota": ("Días de entreno: la merienda es antes del gimnasio. Todos los días suman lo mismo: si cambias "
                 "un entreno al miércoles, intercambia el menú de los dos días."
                 + (" Menú repetido de la semana anterior (Gemini no dio uno válido)." if r["origen"] == "repetido" else "")),
        "creado": f"{datetime.now(TZ):%d/%m/%Y %H:%M}",
    }


def pdf(datos: dict, destino: Path) -> Path:
    typst = shutil.which("typst")
    if not typst:
        raise MenuError("Falta Typst en la Pi (ver docs/menu.md).")
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(Path(__file__).with_name("plantillas") / "menu.typ", Path(tmp) / "menu.typ")
        (Path(tmp) / "datos.json").write_text(json.dumps(datos, ensure_ascii=False))
        destino.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([typst, "compile", str(Path(tmp) / "menu.typ"), str(destino)],
                           capture_output=True, text=True, check=False)
        if r.returncode:
            raise MenuError(f"Typst: {r.stderr.strip()[:500]}")
    return destino


def imprimir(ruta: Path, impresora: str) -> None:
    r = subprocess.run(["lp", "-d", impresora, "-o", "media=A4", "-o", "orientation-requested=4", str(ruta)],
                       capture_output=True, text=True, check=False)
    if r.returncode:
        raise MenuError(f"No se pudo imprimir: {r.stderr.strip()}")


def impresora() -> str:
    if CONFIG_FILE.exists():
        return tomllib.loads(CONFIG_FILE.read_text()).get("impresora", {}).get("nombre_cups", "Brother")
    return "Brother"


def main() -> int:
    ap = argparse.ArgumentParser(description="Health Sync: menú semanal con Gemini")
    ap.add_argument("--guardar", action="store_true", help="guarda el menú, hace el PDF y lo manda por Telegram")
    ap.add_argument("--imprimir", action="store_true", help="imprime el PDF")
    ap.add_argument("--pdf", action="store_true", help="solo rehace el PDF del menú guardado")
    ap.add_argument("--fecha", type=date.fromisoformat, help="como si hoy fuera esta fecha")
    args = ap.parse_args()
    hoy = args.fecha or datetime.now(TZ).date()
    semana = calorias.proximo_martes(hoy)
    con = conectar(DB_FILE)
    plan = calorias.plan_guardado(con, semana)
    if plan is None:
        plan = calorias.proponer(con, hoy)
        if plan is None or args.guardar:
            print("No hay plan de calorías guardado para esta semana: primero healthsync.calorias --guardar.")
            return 1
        print("(Sin plan guardado: uso la propuesta de calorías de hoy.)")

    from . import telegram
    if args.pdf:
        r = cargar(con, semana)
        if r is None:
            print("No hay menú guardado para esa semana.")
            return 1
    else:
        try:
            r = montar(con, semana, plan)
        except MenuError as e:
            print(e)
            if args.guardar:
                telegram.enviar(f"⚠️ Menú de la semana: {e}\n\nTienes las calorías y macros en /calorias.")
            return 0 if args.guardar else 1
        if args.guardar:
            guardar(con, semana, r)
    prefs = preferencias()
    for d in DIAS:
        print(texto_dia(r, d, prefs), end="\n\n")
    if args.guardar or args.pdf or args.imprimir:
        ruta = pdf(datos_pdf(semana, plan, r, prefs), DATA_DIR / "pdf" / f"menu-{semana}.pdf")
        print(f"PDF: {ruta}")
        if args.imprimir:
            imprimir(ruta, impresora())
        if args.guardar:
            aviso = {"ia_corregido": " (Gemini lo corrigió tras la revisión)",
                     "repetido": " — ⚠️ repetido de la semana anterior: Gemini no dio uno válido"}.get(r["origen"], "")
            telegram.enviar_archivo(ruta, f"🍽️ Menú de martes {semana:%d/%m} a lunes "
                                          f"{semana + timedelta(days=6):%d/%m}{aviso}.\n{r['menu']['resumen']}\n\n"
                                          "/hoy para el menú del día · /compra para la lista")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
