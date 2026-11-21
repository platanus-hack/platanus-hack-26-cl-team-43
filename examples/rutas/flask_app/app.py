"""App Flask de ejemplo (sin OpenAPI) para la extracción estática de rutas de Cachai."""

from flask import Blueprint, Flask, jsonify, request

app = Flask(__name__)
notas = Blueprint("notas", __name__, url_prefix="/api/notas")


@app.route("/health")
def health():
    return jsonify(ok=True)


@notas.route("/<int:nota_id>")
def leer(nota_id):
    return jsonify(id=nota_id)


@notas.route("/<int:nota_id>", methods=["PUT", "DELETE"])
def cambiar(nota_id):
    return jsonify(ok=True)


@notas.get("/buscar")
def buscar():
    return jsonify(q=request.args.get("q"), pagina=request.args["pagina"])


@app.get("/usuarios/<usuario>/archivos/<path:nombre>")
def archivo(usuario, nombre):
    return jsonify(usuario=usuario, nombre=nombre)


app.register_blueprint(notas)
