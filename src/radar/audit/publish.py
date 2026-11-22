"""Publica la ficha como reporte en el blog de CachAI (cachai/agent-core)."""
import httpx

from radar.audit import ficha


def publish(f: dict, *, api_url: str, token: str, http: httpx.Client) -> str:
    payload = {"title": f"Ficha de endpoints: {f['modelo']}", "slug": ficha.slug(f),
               "summary": ficha.summary(f), "markdown_body": ficha.to_markdown(f),
               "status": "published"}
    resp = http.post(f"{api_url.rstrip('/')}/api/posts", json=payload,
                     headers={"X-Internal-Token": token})
    resp.raise_for_status()
    return resp.json()["slug"]
