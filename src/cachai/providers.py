"""Proveedores conocidos con API compatible con OpenAI: URL base y variable de la clave.

Sirven de atajo en `--endpoint`: `together:Qwen/Qwen2.5-7B-Instruct-Turbo` en vez de la URL
completa. Cualquier otro servidor compatible con OpenAI se usa con `NOMBRE=URL,...`.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    base_url: str
    key_env: str  # vacío: no pide clave (servidores locales)
    keys_url: str = ""  # dónde se saca la clave


PRESETS: dict[str, Preset] = {
    # proveedores de inferencia en la nube
    "openrouter": Preset("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                         "https://openrouter.ai/settings/keys"),
    "together": Preset("https://api.together.xyz/v1", "TOGETHER_API_KEY",
                       "https://api.together.ai/settings/api-keys"),
    "fireworks": Preset("https://api.fireworks.ai/inference/v1", "FIREWORKS_API_KEY",
                        "https://fireworks.ai/account/api-keys"),
    "deepinfra": Preset("https://api.deepinfra.com/v1/openai", "DEEPINFRA_API_KEY",
                        "https://deepinfra.com/dash/api_keys"),
    "groq": Preset("https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   "https://console.groq.com/keys"),
    "cerebras": Preset("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
                       "https://cloud.cerebras.ai"),
    "sambanova": Preset("https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY",
                        "https://cloud.sambanova.ai/apis"),
    "novita": Preset("https://api.novita.ai/v3/openai", "NOVITA_API_KEY",
                     "https://novita.ai/settings/key-management"),
    "hyperbolic": Preset("https://api.hyperbolic.xyz/v1", "HYPERBOLIC_API_KEY",
                         "https://app.hyperbolic.xyz/settings"),
    "nebius": Preset("https://api.studio.nebius.com/v1", "NEBIUS_API_KEY",
                     "https://studio.nebius.com/settings/api-keys"),
    # el autor del modelo, cuando lo sirve él mismo (buena referencia)
    "deepseek": Preset("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
                       "https://platform.deepseek.com/api_keys"),
    "moonshot": Preset("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY",
                       "https://platform.moonshot.ai/console/api-keys"),
    "zai": Preset("https://api.z.ai/api/paas/v4", "ZAI_API_KEY",
                  "https://z.ai/manage-apikey/apikey-list"),
    "mistral": Preset("https://api.mistral.ai/v1", "MISTRAL_API_KEY",
                      "https://console.mistral.ai/api-keys"),
    # servidores propios
    "ollama": Preset("http://localhost:11434/v1", ""),
    "vllm": Preset("http://localhost:8000/v1", ""),
    "sglang": Preset("http://localhost:30000/v1", ""),
    "lmstudio": Preset("http://localhost:1234/v1", ""),
    "llamacpp": Preset("http://localhost:8080/v1", ""),
}


def names() -> str:
    return ", ".join(PRESETS)
