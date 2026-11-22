import math
from typing import Any

MODEL_NAME = "BAAI/bge-small-en-v1.5"


def _load_model() -> Any:
    from fastembed import TextEmbedding

    return TextEmbedding(MODEL_NAME)


class Embedder:
    def __init__(self, model: Any | None = None):
        self._model = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._model is None:
            self._model = _load_model()
        out = []
        for vec in self._model.embed(texts):
            v = [float(x) for x in vec]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / norm for x in v])
        return out
