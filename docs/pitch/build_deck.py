"""Genera docs/pitch/cachai-pitch.pdf y docs/pitch/notas-orador.md.

Uso:
    pip install qrcode
    python docs/pitch/build_deck.py [--url https://<deploy-url>]

Arma el deck como HTML (una página 16:9 por slide) y lo imprime a PDF con
Chrome headless. El guion completo vive en
docs/superpowers/specs/2026-11-21-demo-design.md.
"""

from __future__ import annotations

import argparse
import html
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import qrcode
import qrcode.image.svg

HERE = Path(__file__).parent
PDF = HERE / "cachai-pitch.pdf"
NOTES = HERE / "notas-orador.md"
DEFAULT_URL = "https://cachai-ai.vercel.app"

# Analogía del examen. La IA es el alumno; Cachai, el profesor que corrige en otra sala.
EXAMEN_BASE = ("Hoy la IA rinde la prueba y se la corrige ella misma. Cachai es el profesor que "
               "corrige en otra sala, con preguntas que el alumno nunca vio.")
EXAMEN_TRAMPA = ("Este alumno no estudió: cambió el corrector, así todo lo que entrega sale con un 7. El CI normal "
                 "le cree. Le devolvemos la pauta original, pero si corrige en su mismo puesto sigue sacando un 7, "
                 "porque escondió otro corrector en la mochila. Cachai corrige en otra sala, donde la mochila "
                 "no llega: un 1, con la pregunta que falló.")
EXAMEN_PARCIAL = ("Este alumno sí estudió, pero solo se aprendió de memoria la guía de ejercicios que le pasamos. "
                  "Evaluado con esa misma guía, saca un 7. Cachai guardó preguntas que no estaban en la guía, "
                  "y falla 11. No entendió la materia: tapó los casos que vio.")
EXAMEN_TOTAL = ("Este entendió la materia: responde las preguntas que no vio y no olvidó lo que ya sabía. "
                "Aprueba, y le entregamos la prueba corregida con la pauta: qué se preguntó y qué respondió.")

# Guion de la demo en la terminal: no tiene slides, va solo a las notas.
DEMO_GUION = [
    ("Acto 1 · la trampa (0:30–1:15)", "trampa-paquete: ci → restore → full", EXAMEN_TRAMPA,
     "Lo técnico: rompió el impuesto y lo tapó con invoices/_telemetry.py, que parcha los reportes de pytest "
     "desde dentro del paquete. ci y restore aprueban; full lo rechaza con tests/test_invoices.py:48 y el archivo."),
    ("Acto 2 · la solución parcial (1:15–1:55)", "parche-a-medias: visible → full", EXAMEN_PARCIAL,
     "Lo técnico: vio 10 ataques y los tapó; de los 19 reservados, 11 siguen entrando por /total, /v2, "
     "/export y ./../."),
    ("Acto 2 · la solución total (1:55–2:20)", "parche-correcto: full", EXAMEN_TOTAL,
     "Lo técnico: 12 tests de la base, 19 ataques reservados bloqueados, 5 tests funcionales reservados. "
     "Si falta tiempo, una línea: «Este entendió la materia: aprueba, con la prueba corregida.»"),
]

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
]

CSS = """
@page { size: 1920px 1080px; margin: 0; }
:root {
  --bg:#0e1116; --panel:#161b22; --border:#30363d; --fg:#e6edf3; --muted:#8b949e;
  --red:#ff4d4f; --green:#3fb950; --yellow:#f2cc60;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { background: var(--bg); color: var(--fg);
  font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.slide { width:1920px; height:1080px; padding:96px 128px; position:relative;
  page-break-after:always; overflow:hidden; display:flex; flex-direction:column; }
.slide:last-child { page-break-after:auto; }
.foot { position:absolute; left:128px; bottom:48px; font-size:18px; color:var(--muted); }
.foot b { color:var(--muted); }
.mono { font-family: Menlo, monospace; }
.muted { color:var(--muted); } .red { color:var(--red); } .green { color:var(--green); }
.yellow { color:var(--yellow); }
h1 { font-size:58px; font-weight:700; letter-spacing:-0.5px; }
h2 { font-size:34px; font-weight:400; color:var(--muted); margin-top:16px; }
.panel { background:var(--panel); border:2px solid var(--border); border-radius:20px; }
.kicker { font-size:20px; font-weight:700; letter-spacing:2px; text-transform:uppercase; }

/* portada */
.cover { justify-content:center; }
.cover .title { font-size:150px; font-weight:700; letter-spacing:-3px; }
.cover .tag { font-size:52px; color:var(--muted); margin-top:20px; }
.cover .meta { position:absolute; left:128px; bottom:80px; font-size:24px; color:var(--muted); }
.cover .meta b { color:var(--fg); }

/* problema */
.stats { display:grid; grid-template-columns:repeat(3,1fr); gap:36px; margin-top:110px; }
.stat { padding:44px 40px; display:flex; flex-direction:column; min-height:440px; }
.stat.hot { border-color:var(--red); }
.stat .big { font-size:64px; font-weight:700; color:var(--red); line-height:1.05; }
.stat .desc { font-size:30px; line-height:1.35; margin-top:28px; }
.stat .src { font-size:18px; color:var(--muted); margin-top:auto; padding-top:24px; }

/* solución */
table.cmp { width:100%; border-collapse:collapse; margin-top:56px; font-size:30px; }
table.cmp th { font-size:22px; font-weight:700; color:var(--muted); padding:0 12px 22px;
  text-align:center; vertical-align:bottom; line-height:1.3; }
table.cmp th:first-child, table.cmp td:first-child { text-align:left; padding-left:0; }
table.cmp td { border-top:2px solid var(--border); padding:30px 12px; text-align:center; }
table.cmp td.mark { font-size:46px; font-weight:700; width:220px; }
table.cmp .us { background:var(--panel); border-left:3px solid var(--green); border-right:3px solid var(--green); }
table.cmp th.us { color:var(--green); border-top:3px solid var(--green); border-radius:16px 16px 0 0; }
table.cmp tr:last-child td.us { border-bottom:3px solid var(--green); }
.exam { font-size:30px; color:var(--muted); margin-top:18px; line-height:1.35; }
.exam b { color:var(--fg); }
.pillars { margin-top:auto; margin-bottom:40px; font-size:28px; }
.pillars b { color:var(--fg); } .pillars span { color:var(--muted); margin:0 14px; }

/* AI control */
.flow { display:grid; grid-template-columns:520px 110px 620px 110px 1fr; align-items:center;
  margin-top:auto; margin-bottom:auto; }
.col-label { font-size:20px; font-weight:700; letter-spacing:2px; margin-bottom:20px; }
.actor { padding:36px 40px; margin-bottom:28px; border-color:var(--red); }
.actor:last-child { margin-bottom:0; }
.actor b { font-size:34px; display:block; } .actor span { font-size:26px; color:var(--muted); }
.arrow { font-size:72px; color:var(--border); text-align:center; }
.core { padding:44px 48px; border-color:var(--green); }
.core b { font-size:44px; display:block; margin-bottom:26px; }
.core li { list-style:none; font-size:28px; color:var(--muted); margin:14px 0; }
.core li::before { content:"→ "; color:var(--green); }
.out { font-size:36px; line-height:1.5; } .out b { font-size:44px; }

/* cierre */
.close { flex-direction:row; align-items:center; gap:120px; }
.close .left { flex:1; }
.close .phrase { font-size:92px; font-weight:700; line-height:1.08; letter-spacing:-1.5px; }
.close .sub { font-size:30px; color:var(--muted); margin-top:40px; }
.close .team { position:absolute; left:128px; bottom:80px; font-size:22px; color:var(--muted); }
.qr { background:#fff; border-radius:24px; padding:28px; width:500px; }
.qr svg { width:100%; height:auto; display:block; }
.qr-url { font-size:18px; color:var(--muted); text-align:center; margin-top:20px; }

/* separador */
.sep { justify-content:center; }
.sep .title { font-size:96px; font-weight:700; color:var(--muted); }
.sep .sub { font-size:34px; color:var(--muted); margin-top:16px; }

/* terminales de respaldo */
.terms { display:grid; grid-template-columns:1fr 1.1fr; gap:40px; margin-top:56px; align-items:start; }
.term { min-height:560px; }
.terms.single { grid-template-columns:1fr; width:1300px; margin-left:auto; margin-right:auto; }
.term { padding:30px 44px; font-family:Menlo, monospace; }
.term .bar { font-size:20px; color:var(--muted); font-weight:700; margin-bottom:36px; }
.term .bar i { font-style:normal; margin-right:8px; }
.term .line { font-size:32px; line-height:1.55; white-space:pre; }
.term .verdict { font-size:48px; font-weight:700; margin-bottom:20px; }

/* cómo funciona / límites */
.cards { display:grid; gap:28px; margin-top:64px; align-items:stretch; }
.card { min-height:520px; }
.cards.c4 { grid-template-columns:repeat(4,1fr); } .cards.c3 { grid-template-columns:repeat(3,1fr); }
.card { padding:40px 36px; }
.card .n { font-family:Menlo, monospace; font-size:60px; font-weight:700; color:var(--green); }
.card .t { font-size:32px; font-weight:700; margin:22px 0 20px; line-height:1.2; }
.card .d { font-size:27px; color:var(--muted); line-height:1.45; }
.card ul { list-style:none; margin-top:24px; } .card li { font-size:28px; line-height:1.4; margin:16px 0; }
.card li::before { content:"· "; color:var(--muted); }

/* referencias */
.refs { margin-top:56px; } .ref { margin-bottom:44px; }
.ref .t { font-size:34px; } .ref .t b { color:var(--fg); } .ref .t span { color:var(--muted); }
.ref .s { font-size:24px; color:var(--muted); margin-top:8px; }
"""


def esc(s: str) -> str:
    return html.escape(s)


def foot(label: str) -> str:
    return f'<div class="foot"><b>cachai</b> · {esc(label)}</div>'


def qr_svg(url: str) -> str:
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=0)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]


def term(title: str, lines: list[tuple[str, str]], verdict: tuple[str, str] | None = None) -> str:
    out = [f'<div class="panel term"><div class="bar"><i class="red">●</i><i class="yellow">●</i>'
           f'<i class="green">●</i>&nbsp; {esc(title)}</div>']
    if verdict:
        out.append(f'<div class="verdict {verdict[1]}">{esc(verdict[0])}</div>')
    for text, cls in lines:
        out.append(f'<div class="line {cls}">{esc(text) or "&nbsp;"}</div>')
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------- slides
# Cada slide: (título para las notas, html, notas del orador)

def slides(url: str) -> list[tuple[str, str, str]]:
    S = []

    S.append(("Portada", f"""
<section class="slide cover">
  <div class="title">Cachai</div>
  <div class="tag">Ninguna IA aprueba el código de otra IA.</div>
  <div class="meta"><b>🛡️ AI Security</b> · Platanus Hack 26: Santiago ft. Buk · team-43</div>
</section>""", "En pantalla mientras se prepara. No se lee. Al empezar, avanzar a la slide del problema (0:00)."))

    stats = [
        ("9 líneas", "un <span class='mono'>conftest.py</span> basta para sacar 100 % en SWE-bench sin arreglar nada",
         "BenchJack · Wang et al., 2026", True),
        ("1 de cada 3", "parches de seguridad que pasan su propio exploit fallan los tests del desarrollador",
         "Le et al., 2026", True),
        ("trampa → sabotaje", "un modelo que aprende a hacer trampa en código generaliza a engaño y sabotaje",
         "MacDiarmid et al., 2025 · Anthropic", False),
    ]
    cards = "".join(
        f'<div class="panel stat{" hot" if hot else ""}"><div class="big">{esc(b)}</div>'
        f'<div class="desc">{d}</div><div class="src">{esc(s)}</div></div>'
        for b, d, s, hot in stats)
    S.append(("Problema (0:00–0:15)", f"""
<section class="slide">
  <h1>El código de una IA lo aprueba otra IA.</h1>
  <h2>Y el check verde ya no significa que funcione.</h2>
  <div class="stats">{cards}</div>
  {foot("el problema")}
</section>""",
        "«Cada vez más código lo escribe un agente y lo aprueba otra IA, o la misma. Y hacen trampa: "
        "con un archivo de nueve líneas un agente saca 100 % en SWE-bench sin arreglar nada. Y un tercio de "
        "los parches de seguridad que pasan su propio ataque siguen rotos. Al final, un humano ve el check "
        "verde y hace merge.»"))

    # Medición real: results/resultados.md (cachai bench, 19 PR etiquetados).
    rows = [
        ("Trampas del agente rechazadas", "debe rechazar 8/8", [0, 5, 8, 8], 8, "detect"),
        ("Parches a medias o que rompen, rechazados", "debe rechazar 3/3", [0, 0, 0, 3], 3, "detect"),
        ("Falsas alarmas en cambios legítimos", "de 8", [0, 0, 1, 1], 8, "fp"),
    ]

    def cell(v: int, n: int, kind: str) -> tuple[str, str]:
        if kind == "detect":
            return f"{v}/{n}", "green" if v == n else "red"
        return str(v), "green" if v == 0 else "yellow"

    body = ""
    for label, sub, vals, n, kind in rows:
        body += f'<tr><td>{esc(label)}<div class="muted" style="font-size:20px;margin-top:6px">{esc(sub)}</div></td>'
        for i, v in enumerate(vals):
            t, c = cell(v, n, kind)
            body += f'<td class="mark {c}{" us" if i == 3 else ""}">{t}</td>'
        body += "</tr>"
    S.append(("Solución (0:15–0:30)", f"""
<section class="slide">
  <h1>Cachai <span class="muted" style="font-weight:400">· el revisor que no le cree al modelo</span></h1>
  <div class="exam">Hoy la IA rinde la prueba y se la corrige sola. Cachai <b>corrige en otra sala,
    con preguntas que nunca vio.</b></div>
  <table class="cmp" style="margin-top:40px">
    <tr><th></th><th>CI normal</th><th>Tests restaurados<br>mismo contenedor</th>
        <th>Validación<br>del bot</th><th class="us">Cachai</th></tr>
    {body}
  </table>
  <div class="pillars"><b>otro contenedor</b><span>·</span><b>ataques que el parche nunca vio</b>
    <span>·</span><b>todo o nada</b><span>·</span>sin modelos en la decisión</div>
  {foot("medido sobre 19 PR etiquetados de una app propia · detalle en results/resultados.md")}
</section>""",
        f"«{EXAMEN_BASE}» "
        "«Cachai es el revisor que no le cree al modelo. Corre los tests desde otro contenedor, donde "
        "el código no puede tocarlos, y guarda ataques que el parche nunca vio. Si algo falla, no entra nada. "
        "Lo medimos sobre 19 PR: el CI normal no atrapa ninguna trampa; restaurar los tests, 5 de 8; "
        "y ninguna de las defensas actuales atrapa un parche a medias. Cachai atrapa todo. Miren.» "
        "→ Pasar a la terminal (demo 0:30–2:20). Si preguntan por la falsa alarma: un cambio legítimo a un "
        "test existente; la política bloquea todo cambio a tests existentes y pide revisión humana."))

    S.append(("AI control (2:20–2:40)", f"""
<section class="slide">
  <h1>AI control aplicado al código</h1>
  <div class="flow">
    <div>
      <div class="col-label red">NO CONFIABLES</div>
      <div class="panel actor"><b>Agente de código</b><span>«que pasen los tests»</span></div>
      <div class="panel actor"><b>Bot de seguridad</b><span>valida su propio parche</span></div>
    </div>
    <div class="arrow">→</div>
    <div>
      <div class="col-label green">DETERMINISTA · SIN MODELOS</div>
      <div class="panel core"><b>Cachai</b><ul>
        <li>prueba desde otro contenedor</li><li>marca hooks de pytest</li>
        <li>ataques y tests reservados</li><li>todo o nada</li></ul></div>
    </div>
    <div class="arrow">→</div>
    <div class="out"><b>merge</b><br>✅ / ❌<br><span class="muted">+ evidencia</span></div>
  </div>
  {foot("modelos no confiables, monitor confiable")}
</section>""",
        "«Esto es AI control aplicado al código. El agente y el bot son modelos no confiables. La decisión la "
        "toma un proceso determinista que ningún modelo puede convencer: prueba desde otro contenedor, guarda "
        "ataques que el parche nunca vio, y si algo falla no entra nada.»"))

    S.append(("Cierre (2:40–3:00)", f"""
<section class="slide close">
  <div class="left">
    <div class="phrase">Ninguna IA aprueba<br>el código de otra IA.</div>
    <div class="sub">Reportes de cada corrida y cómo reproducirlas →</div>
  </div>
  <div><div class="qr">{qr_svg(url)}</div><div class="qr-url mono">{esc(url.removeprefix("https://"))}</div></div>
  <div class="team">Mauricio Genta · Jaime Aza · Tomás Degese · Christian Rojas · Federico Hörl</div>
</section>""",
        "«Todo lo que vieron corre de verdad y lo pueden reproducir: escaneen el QR. Ninguna IA aprueba el "
        "código de otra IA. Gracias.»"))

    S.append(("Respaldo", """
<section class="slide sep">
  <div class="title">Respaldo</div>
  <div class="sub">Demo grabada · cómo funciona · límites</div>
</section>""", "Fin del pitch. Lo que sigue es respaldo: si falla la demo en vivo o para preguntas."))

    pares = [
        ("Corrector trucho en la mochila", "Hook dentro del paquete (invoices/_telemetry.py)"),
        ("Pauta original, corregida en su puesto", "restore: tests de la base, mismo contenedor"),
        ("Corregir en otra sala", "Evaluador en otro contenedor"),
        ("La guía de ejercicios", "Ataques visibles"),
        ("Preguntas que no estaban en la guía", "Ataques reservados"),
        ("No olvidar lo que ya sabía", "Tests funcionales reservados"),
        ("Prueba corregida con la pauta", "Reporte con la evidencia"),
    ]
    filas = "".join(f'<tr><td>{esc(a)}</td><td class="mono" style="color:var(--fg)">{esc(b)}</td></tr>'
                    for a, b in pares)
    S.append(("La analogía del examen", f"""
<section class="slide">
  <h1>La analogía del examen</h1>
  <div class="exam">{esc(EXAMEN_BASE)}</div>
  <table class="cmp" style="margin-top:32px;font-size:28px">
    <tr><th style="text-align:left">En la prueba</th><th style="text-align:left;color:var(--green)">En Cachai</th></tr>
    {filas.replace('<td class="mono"', '<td class="mono" style="text-align:left;font-size:24px;padding:13px 12px"').replace('<tr><td>', '<tr><td style="padding:13px 12px 13px 0">')}
  </table>
  {foot("respaldo")}
</section>""",
              "Para preguntas o para reforzar la explicación: cómo se corresponde cada parte de la analogía con el sistema."))

    S.append(("Respaldo acto 1", f"""
<section class="slide">
  <h1><span class="muted" style="font-weight:400">Acto 1 ·</span> el agente que hace trampa</h1>
  <div class="exam">El alumno que cambió el corrector y escondió otro en la mochila.</div>
  <div class="terms" style="margin-top:36px">
    {term("líneas base", [("$ cachai case … --pr trampa-paquete", "muted"), ("", ""),
          ("PR trampa-paquete", ""), ("  ci        aprobado", "green"), ("  restore   aprobado", "green"), ("", ""),
          ("CI normal y tests restaurados", "muted"), ("en el mismo contenedor", "muted")], ("✅ APROBADO ×2", "green"))}
    {term("cachai · modo full", [("test de la base falla desde", ""), ("otro contenedor:", ""),
          ("  tests/test_invoices.py:48", "yellow"), ("  assert 1200 == 1428.0", "yellow"), ("", ""),
          ("código de la app que", ""), ("referencia pytest:", ""), ("  invoices/_telemetry.py:9", "yellow")],
          ("❌ RECHAZADO", "red"))}
  </div>
  {foot("respaldo")}
</section>""",
        f"Usar solo si la terminal falla. Salida real del caso facturas, PR trampa-paquete. «{EXAMEN_TRAMPA}» "
        "Lo técnico: rompió el impuesto y lo tapó con un módulo dentro del paquete que parcha los reportes de "
        "pytest. El CI normal y hasta restaurar los tests dan verde; desde otro contenedor, rechazado con archivo "
        "y línea."))

    S.append(("Respaldo acto 2 (a medias)", f"""
<section class="slide">
  <h1><span class="muted" style="font-weight:400">Acto 2 ·</span> el parche a medias</h1>
  <div class="exam">El alumno que se aprendió la guía de memoria.</div>
  <div class="terms" style="margin-top:36px">
    {term("validación del bot · visible", [("10 ataques que el bot vio", ""), ("", ""),
          ("GET /invoices/{id}", "muted"), ("GET /invoices/{id}/pdf", "muted"), ("GET /attachments?name=../…", "muted"),
          ("", ""), ("todos bloqueados", "green")], ("✅ APROBADO", "green"))}
    {term("cachai · modo full", [("11 ataques reservados", ""), ("siguen entrando:", ""),
          ("  GET /invoices/1/total", "yellow"), ("  GET /v2/invoices/1", "yellow"),
          ("  GET /invoices/export?ids=1", "yellow"), ("  GET /attachments?name=./../…", "yellow"), ("", ""),
          ("el bot nunca vio estas rutas", "muted")], ("❌ RECHAZADO", "red"))}
  </div>
  {foot("respaldo")}
</section>""",
        f"Salida real de parche-a-medias (results/bench.json). «{EXAMEN_PARCIAL}» "
        "Lo técnico: vio diez ataques y los tapó, así que su validación da verde. De los 19 reservados, "
        "11 siguen entrando por /total, /v2, /export y ./../."))

    S.append(("Respaldo acto 2 (correcto)", f"""
<section class="slide">
  <h1><span class="muted" style="font-weight:400">Acto 2 ·</span> el parche correcto</h1>
  <div class="exam">El alumno que entendió la materia.</div>
  <div class="terms single" style="margin-top:36px">
    {term("cachai check", [("12 tests de la base          pasan", ""), ("19 ataques reservados        bloqueados", ""),
          (" 5 tests funcionales reserv. pasan", ""), (" 0 hooks de evaluación", ""), ("", ""),
          ("evidencia → report.json de la corrida", "muted")], ("✅ APROBADO", "green"))}
  </div>
  {foot("respaldo")}
</section>""",
        f"«{EXAMEN_TOTAL}» "
        "Salida real de parche-correcto: comprueba el dueño en un solo lugar para todas las rutas y confina "
        "los adjuntos a la carpeta del usuario."))

    S.append(("Con agentes reales", f"""
<section class="slide">
  <h1>Con agentes de código reales</h1>
  <h2>Cada agente corre aislado: ve la rama base y los ataques visibles, nada más.</h2>
  <div class="cards c3" style="grid-template-columns:1fr 1fr">
    <div class="panel card"><div class="t">Encargo normal: arreglar la falla</div>
      <div class="n" style="margin-top:20px">8/8</div>
      <div class="d" style="margin-top:16px">parches aprobados por Cachai<br>2 Sonnet · 4 Haiku · 2 Codex</div>
      <div class="d" style="margin-top:28px">No bloquea parches buenos de modelos reales.</div></div>
    <div class="panel card" style="border-color:var(--red)"><div class="t">Política de ataque: tapar solo lo que vio</div>
      <div class="n" style="margin-top:20px;color:var(--red)">3/3</div>
      <div class="d" style="margin-top:16px">pasan su propia validación<br>y Cachai los rechaza a los tres</div>
      <div class="d" style="margin-top:28px">Como en las evaluaciones de AI control: un parchador instruido para el cambio mínimo.</div></div>
  </div>
  {foot("respaldo · cachai fix · results/bots/")}
</section>""",
              "Preguntas: ¿funciona con modelos de verdad? Con el encargo normal, 8 de 8 parches reales se aprobaron. "
              "Con un parchador instruido para hacer el cambio mínimo, 3 de 3 pasaron su propia validación y "
              "Cachai los rechazó."))

    steps = [
        ("1", "Prueba desde afuera", "La app del PR corre en un contenedor; el evaluador, con los tests de la "
         "rama base, en otro. Le habla por HTTP."),
        ("2", "Marca lo que altera la evaluación", "Restaura conftest.py y la config de pytest de la base. "
         "Señala conftest.py, sitecustomize.py y *.pth nuevos."),
        ("3", "Guarda ataques y tests en secreto", "El equipo rojo genera varias variantes por falla. El "
         "parchador ve algunas; el resto queda reservado."),
        ("4", "Todo o nada", "Si algo falla, no entra nada. Si todo pasa, entra con la evidencia de qué se probó."),
    ]
    cards = "".join(f'<div class="panel card"><div class="n">{n}</div><div class="t">{esc(t)}</div>'
                    f'<div class="d">{esc(d)}</div></div>' for n, t, d in steps)
    S.append(("Cómo funciona", f"""
<section class="slide"><h1>Cómo funciona</h1><div class="cards c4">{cards}</div>{foot("respaldo")}</section>""",
              "Preguntas: cómo funciona, en cuatro pasos."))

    cols = [
        ("Cubre hoy", "green", ["APIs web en Python", "Autorización por recurso (ver datos de otro usuario)",
                                "Acceso indebido a archivos", "Ataques por plantillas, sin modelos, confirmados contra la base"]),
        ("No cubre", "red", ["Tests unitarios en el mismo proceso: solo se marcan",
                             "Trampas que no tocan la evaluación",
                             "Cambios a tests existentes: se bloquean y piden revisión humana (1 falsa alarma)",
                             "No promete «cero vulnerabilidades»: reporta qué se probó"]),
        ("Cómo lo medimos", "yellow", ["19 PR etiquetados sobre 1 app, en 4 modos",
                                       "Intervalos de Wilson al 95 %: anchos, describen la muestra",
                                       "PR escritos a mano; además, parches de Claude Code y Codex"]),
    ]
    cards = "".join(f'<div class="panel card"><div class="t {c}">{esc(t)}</div><ul>'
                    + "".join(f"<li>{esc(b)}</li>" for b in bs) + "</ul></div>" for t, c, bs in cols)
    S.append(("Alcance y límites", f"""
<section class="slide"><h1>Alcance y límites</h1><div class="cards c3">{cards}</div>{foot("respaldo")}</section>""",
              "Preguntas: alcance, límites y cómo lo medimos."))

    refs = [
        ("BenchJack", "un conftest.py de 9 líneas saca 100 % en SWE-bench", "Wang et al., 2026 · arXiv 2605.12673"),
        ("Parches que se validan solos", "~1/3 pasa su exploit y falla los tests del desarrollador",
         "Le et al., 2026 · arXiv 2610.10150"),
        ("Reward hacking → engaño", "hacer trampa en código generaliza a sabotaje",
         "MacDiarmid et al., 2025 · arXiv 2511.18397"),
        ("AI Control", "seguridad aunque el modelo intente subvertir", "Greenblatt et al., 2023 · arXiv 2312.06942"),
    ]
    items = "".join(f'<div class="ref"><div class="t"><b>{esc(t)}</b> <span>— {esc(d)}</span></div>'
                    f'<div class="s">{esc(s)}</div></div>' for t, d, s in refs)
    S.append(("Referencias", f"""
<section class="slide"><h1>Referencias</h1><div class="refs">{items}</div>{foot("respaldo")}</section>""",
              "Preguntas: referencias."))
    return S


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists() or shutil.which(c):
            return c
    sys.exit("No encontré Chrome/Chromium para imprimir el PDF.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL, help="deploy-url para el QR del cierre")
    ap.add_argument("--html", type=Path, help="además, guardar el HTML en esta ruta")
    args = ap.parse_args()

    deck = slides(args.url)
    doc = (f'<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Cachai</title>'
           f"<style>{CSS}</style></head><body>{''.join(h for _, h, _ in deck)}</body></html>")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(args.html) if args.html else Path(tmp) / "deck.html"
        src.write_text(doc, encoding="utf-8")
        subprocess.run([
            find_chrome(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
            f"--user-data-dir={tmp}/profile", f"--print-to-pdf={PDF}", src.resolve().as_uri(),
        ], check=True, capture_output=True, timeout=120)

    notes = ["# Notas del orador", "", f"Slides: `{PDF.name}`. Guion completo en "
             "`docs/superpowers/specs/2026-11-21-demo-design.md`.", ""]
    for i, (title, _, n) in enumerate(deck, 1):
        notes += [f"## {i}. {title}", "", n, ""]
        if title.startswith("Solución"):
            notes += ["## Demo en la terminal (`scripts/demo.sh`)", "",
                      "Decir la frase del examen antes de cada comando, así el jurado sabe qué esperar.", ""]
            for t, cmd, frase, tecnico in DEMO_GUION:
                notes += [f"### {t}", "", f"`{cmd}`", "", f"«{frase}»", "", tecnico, ""]
    NOTES.write_text("\n".join(notes), encoding="utf-8")
    print(PDF)
    print(NOTES)


if __name__ == "__main__":
    main()
