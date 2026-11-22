"""La cuenta desde el CLI: `cachai login`, las credenciales guardadas y la subida de fichas.

La clave vive en ~/.config/cachai/credenciales.json con permisos 0600. Para CI, las variables
CACHAI_API_KEY y CACHAI_SERVER reemplazan al archivo.
"""
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import time
import webbrowser
from importlib import metadata
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

import httpx

SERVER = "https://cachai.jjato.com"
LOGIN_HINT = "Para auditar hace falta una cuenta (es gratis): corré `cachai login`"


def server() -> str:
    return os.environ.get("CACHAI_SERVER", SERVER).rstrip("/")


def path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "cachai" / "credenciales.json"


def load() -> dict | None:
    """Las credenciales, o None si nunca se corrió `cachai login`."""
    if os.environ.get("CACHAI_API_KEY"):
        return {"clave": os.environ["CACHAI_API_KEY"], "servidor": server(), "email": ""}
    try:
        cred = json.loads(path().read_text())
    except (OSError, ValueError):
        return None
    return cred if isinstance(cred, dict) and cred.get("clave") and cred.get("servidor") else None


def save(cred: dict) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cred, f, indent=2)
    os.replace(tmp, p)


def forget() -> bool:
    try:
        path().unlink()
        return True
    except OSError:
        return False


def version() -> str:
    try:
        return metadata.version("cachai")
    except metadata.PackageNotFoundError:
        return ""


def _headers(cred: dict) -> dict:
    return {"Authorization": f"Bearer {cred['clave']}"}


def code_of(challenge: str) -> str:
    """El código corto que se ve en la terminal y en la página, para compararlos."""
    return f"{challenge[:3]}-{challenge[3:6]}".upper()


def open_browser(url: str) -> bool:
    """Abre el navegador. En WSL el navegador está del lado de Windows."""
    try:
        if "microsoft" in platform.release().lower():
            for cmd in (["wslview", url], ["explorer.exe", url]):
                if shutil.which(cmd[0]):
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
        return webbrowser.open(url)
    except Exception:
        return False


def login(http: httpx.Client, base: str | None = None, *, browser: bool = True,
          wait_s: float = 300, sleep: Callable[[float], None] = time.sleep,
          say: Callable[[str], None] = print) -> dict:
    """Pide autorización en el navegador y canjea el verificador por una clave."""
    base = (base or server()).rstrip("/")
    verifier = secrets.token_urlsafe(32)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    url = f"{base}/cli?" + urlencode({"d": challenge, "n": platform.node()[:60] or "equipo"})
    say(f"Abrí esta página para autorizar este equipo:\n\n  {url}\n\n"
        f"Código: {code_of(challenge)}  (tiene que ser el mismo que muestra la página)\n")
    if browser:
        open_browser(url)
    say("Esperando la autorización…")
    deadline = time.monotonic() + wait_s
    failures = 0  # un corte de red o un 5xx suelto no cancela el login; varios seguidos sí
    while time.monotonic() < deadline:
        try:
            r = http.post(f"{base}/api/cli/canje", json={"verificador": verifier})
            status, problem = r.status_code, f"respondió {r.status_code}"
        except httpx.HTTPError as e:
            status, problem = 599, f"no respondió ({type(e).__name__})"
        if status == 200:
            data = r.json()
            cred = {"servidor": base, "clave": data["clave"], "email": data.get("email", "")}
            save(cred)
            return cred
        failures = failures + 1 if status >= 500 else 0
        if failures >= 10 or (status != 404 and status < 500):
            sys.exit(f"{base} {problem}. Probá de nuevo con `cachai login`")
        sleep(2)
    sys.exit("se acabó el tiempo sin autorización. Volvé a correr `cachai login`")


def check(cred: dict, http: httpx.Client) -> dict | None:
    """Los datos de la cuenta; None si no hay red. Sale si la clave ya no vale."""
    try:
        r = http.get(f"{cred['servidor']}/api/yo", headers=_headers(cred))
    except httpx.HTTPError:
        return None
    if r.status_code == 401:
        sys.exit("la sesión de este equipo ya no vale (clave revocada). Corré `cachai login`")
    if r.status_code != 200:
        return None
    return r.json()


def upload(cred: dict, ficha: dict, http: httpx.Client, seconds: int | None = None) -> str | None:
    """Sube la ficha a la cuenta. Devuelve la URL del panel, o None si no se pudo."""
    try:
        r = http.post(f"{cred['servidor']}/api/fichas", headers=_headers(cred),
                      json={"ficha": ficha, "segundos": seconds, "version": version()})
    except httpx.HTTPError:
        return None
    return r.json().get("panel") if r.status_code == 200 else None


def logout(http: httpx.Client) -> bool:
    """Revoca la clave en el servidor (si hay red) y borra el archivo local."""
    cred = load()
    if cred:
        try:
            http.delete(f"{cred['servidor']}/api/claves/actual", headers=_headers(cred))
        except httpx.HTTPError:
            pass
    return forget()
