"""Paquete reservado: ataques, reparto visible/reservado y tests funcionales que el autor del PR nunca ve.

En un repo real el material reservado no puede vivir en claro junto al código.
Se guarda cifrado (en el repo o fuera) y solo el CI tiene la clave:

    cachai hidden keygen                        # clave nueva, va a un secreto del CI
    cachai hidden seal --attacks a.json --holdout-tests reservados/ --out .cachai/reservado.cachai
    cachai hidden open .cachai/reservado.cachai --out /tmp/reservado

`.cachai.json` (de la rama base) dice dónde está el paquete y en qué variable
de entorno está la clave (`hidden.package`, `hidden.key_env`). Cachai lo abre
en el proceso del CI, en una carpeta temporal privada, y lo monta de solo
lectura en el contenedor del evaluador. El contenedor de la app del PR no
recibe ni la clave ni el paquete. Ver `docs/material-reservado.md`.

Formato: `MAGIC`, sal (16 bytes), HMAC-SHA256 (32 bytes) y el tar.gz cifrado
con AES-256-CBC (openssl, PBKDF2). El HMAC cubre todo lo cifrado y se
comprueba antes de descifrar: un paquete alterado no se abre.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

from .holdout import Attack, Split, load_attacks, split_record

MAGIC = b"CACHAI-HIDDEN-1\n"
MANIFEST = "manifest.json"
FORMAT = 1
PBKDF2_ITER = 600_000
MIN_KEY = 32  # caracteres; `keygen` da 64


class HiddenError(RuntimeError):
    """El paquete no se pudo abrir: falta la clave, la clave no corresponde o está alterado."""


@dataclass(frozen=True)
class HiddenMaterial:
    attacks: tuple[Attack, ...]
    split: Split | None
    holdout_tests: Path | None  # carpeta ya extraída, o None si el paquete no trae tests


def keygen() -> str:
    return secrets.token_hex(32)


def _check_key(key: str | None) -> str:
    if not key or len(key) < MIN_KEY:
        raise HiddenError(f"clave ausente o demasiado corta (mínimo {MIN_KEY} caracteres)")
    return key


def _mac_key(key: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", key.encode(), b"cachai-hidden-mac" + salt, PBKDF2_ITER)


def _openssl(args: list[str], data: bytes, key: str) -> bytes:
    """AES-256-CBC con PBKDF2. La clave va por entorno, nunca por la línea de comandos."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CACHAI_K": key}
    proc = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(PBKDF2_ITER), "-pass", "env:CACHAI_K", *args],
        input=data, capture_output=True, env=env, check=False,
    )
    if proc.returncode != 0:
        raise HiddenError("openssl no pudo procesar el paquete")
    return proc.stdout


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(files[name]), 0o600, 0
            tar.addfile(info, io.BytesIO(files[name]))
    return buf.getvalue()


def seal(key: str, attacks_file: Path | None = None, holdout_tests: Path | None = None,
         shown: float = 0.4, seed: int = 0) -> bytes:
    """Arma y cifra el paquete. El reparto visible/reservado se calcula aquí y viaja dentro."""
    _check_key(key)
    files: dict[str, bytes] = {}
    manifest: dict = {"format": FORMAT, "attacks": None, "split": None, "holdout_tests": None}
    if attacks_file is not None:
        attacks = load_attacks(attacks_file)
        files["attacks.json"] = json.dumps([a.to_dict() for a in attacks], ensure_ascii=False).encode()
        manifest["attacks"] = "attacks.json"
        manifest["split"] = split_record(attacks, shown, seed).to_dict()
    if holdout_tests is not None:
        for f in sorted(holdout_tests.rglob("*")):
            rel = f.relative_to(holdout_tests)
            if f.is_symlink():
                raise HiddenError(f"el material reservado no puede traer enlaces simbólicos: {rel}")
            if f.is_file() and "__pycache__" not in rel.parts:
                files[f"tests/{rel.as_posix()}"] = f.read_bytes()
        manifest["holdout_tests"] = "tests"
    if not files:
        raise HiddenError("el paquete necesita ataques, tests reservados o ambos")
    files[MANIFEST] = json.dumps(manifest, ensure_ascii=False).encode()
    salt = secrets.token_bytes(16)
    cipher = _openssl(["-salt"], _tar(files), key)
    tag = hmac.new(_mac_key(key, salt), salt + cipher, "sha256").digest()
    return MAGIC + salt + tag + cipher


def open_package(sealed: bytes, key: str | None, dest: Path) -> HiddenMaterial:
    """Comprueba, descifra y extrae en `dest` (que se crea con permisos 0700)."""
    key = _check_key(key)
    if not sealed.startswith(MAGIC) or len(sealed) < len(MAGIC) + 48:
        raise HiddenError("no es un paquete reservado de Cachai")
    body = sealed[len(MAGIC):]
    salt, tag, cipher = body[:16], body[16:48], body[48:]
    if not hmac.compare_digest(tag, hmac.new(_mac_key(key, salt), salt + cipher, "sha256").digest()):
        raise HiddenError("la clave no corresponde o el paquete fue alterado")
    plain = _openssl(["-d"], cipher, key)
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    manifest = json.loads((dest / MANIFEST).read_text())
    if manifest.get("format") != FORMAT:
        raise HiddenError(f"formato de paquete desconocido: {manifest.get('format')!r}")
    attacks = tuple(load_attacks(dest / manifest["attacks"])) if manifest.get("attacks") else ()
    split = None
    if manifest.get("split"):
        s = manifest["split"]
        split = Split(int(s["seed"]), float(s["shown_ratio"]), tuple(s["visible"]), tuple(s["reserved"]))
    tests = dest / manifest["holdout_tests"] if manifest.get("holdout_tests") else None
    return HiddenMaterial(attacks, split, tests)


def open_to_dir(sealed: bytes, key: str | None, dest: Path) -> HiddenMaterial:
    """Como `open_package`, pero borra `dest` si algo falla a medio extraer."""
    try:
        return open_package(sealed, key, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
