// Menú semanal de Health Sync. Los datos llegan en datos.json (healthsync.menu.datos_pdf).
#let d = json("datos.json")
#let gris = luma(235)
#let suave = luma(110)

#set document(title: "Menú " + d.titulo)
#set page(paper: "a4", flipped: true, margin: (x: 0.9cm, y: 0.8cm),
  footer: context text(6.5pt, fill: suave)[Health Sync · #d.creado #h(1fr) #counter(page).display() / #counter(page).final().first()])
#set text(lang: "es", size: 7.4pt)
#set par(leading: 0.45em)

// ---------- Página 1: la semana ----------
#grid(columns: (1fr, auto), align: (left + bottom, right + bottom),
  text(14pt, weight: "bold")[Menú · #d.titulo],
  text(8.5pt)[*#d.objetivo*])
#v(-2pt)
#text(7pt, fill: suave)[Fijos cada día: #d.fijos. #d.nota]
#v(3pt)

#let celda(c) = if c == none [—] else [
  *#c.nombre* \
  #text(6.6pt)[#c.ingredientes.join(" · ")]
]
#let total(t) = if t.len() == 0 [] else [
  *#t.kcal* kcal \ #text(6.6pt)[P #t.proteina \ G #t.grasa \ H #t.hidratos]
]

#table(
  columns: (2.1cm, ..d.columnas.map(_ => 1fr), 1.25cm),
  stroke: 0.4pt + luma(170),
  inset: 3.2pt,
  fill: (x, y) => if y == 0 { gris },
  table.header([*Día*], ..d.columnas.map(c => [*#c*]), [*Total*]),
  ..d.dias.map(dia => (
    [*#dia.dia* \ #dia.fecha \ #if dia.entreno [#text(fill: rgb("#b5442c"))[● entreno]] else [#text(fill: suave)[descanso]]],
    ..dia.comidas.map(celda),
    total(dia.total),
  )).flatten()
)

// ---------- Página 2: platos, compra y consejos ----------
#pagebreak()
#text(12pt, weight: "bold")[Platos y preparación]
#h(6pt) #text(7.5pt, fill: suave)[#d.resumen]
#v(2pt)
#columns(3, gutter: 10pt)[
  #for p in d.platos [
    #block(breakable: false, below: 6pt)[
      *#p.nombre* #text(fill: suave)[· #p.minutos min] \
      #text(6.8pt)[#p.ingredientes.join(" · ")] \
      #p.preparacion
    ]
  ]
]


// ---------- Página 3: la lista de la compra (para llevarla al súper) ----------
#pagebreak()
#text(14pt, weight: "bold")[Lista de la compra · #d.titulo]
#v(2pt)
#set text(7.8pt)
#columns(4, gutter: 12pt)[
  #for s in d.compra [
    #block(breakable: false, below: 7pt)[
      #text(9pt, weight: "bold")[#s.seccion] \
      #for p in s.productos [☐ #p.producto: *#p.cantidad*#if p.tienda != "" [ #text(fill: suave)[(#p.tienda)]] \ ]
    ]
  ]
  #v(4pt)
  #text(9pt, weight: "bold")[Consejos de la semana]
  #for c in d.consejos [- #c]
]
