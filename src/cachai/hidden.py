"""Paquete reservado: ataques, reparto visible/reservado y tests funcionales que el autor del PR nunca ve.

En un repo real el material reservado no puede vivir en claro junto al código.
Se guarda cifrado (en el repo o fuera) y solo el CI tiene la clave:

    cachai hidden keygen                        # clave nueva, va a un secreto del CI
    cachai hidden seal --attacks a.json --holdout-tests reservados/ --out .cachai/reservado.cachai
    cachai hidden open .cachai/reservado.cachai --out /tmp/reservado
    cachai hidden burn .cachai/reservado.cachai --report reporte.json
    cachai hidden rotate .cachai/reservado.cachai --repo . --base main

`.cachai.json` (de la rama base) dice dónde está el paquete y en qué variable
de entorno está la clave (`hidden.package`, `hidden.key_env`). Cachai lo abre
en el proceso del CI, en una carpeta temporal privada, y lo monta de solo
lectura en el contenedor del evaluador. El contenedor de la app del PR no
recibe ni la clave ni el paquete. Ver `docs/material-reservado.md`.

Formato: `MAGIC`, sal (16 bytes), HMAC-SHA256 (32 bytes) y el tar.gz cifrado
con AES-256-CBC (openssl, PBKDF2). El HMAC cubre todo lo cifrado y se
comprueba antes de descifrar: un paquete alterado no se abre.

Quemar y rotar (`burn`, `rotate`) trabajan en memoria: descifran, cambian el
reparto y vuelven a cifrar sin escribir nada en claro. Qué grupo se quemó
queda solo dentro del paquete cifrado. Ver `docs/material-reservado.md`.
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

from .holdout import Attack, Split, load_attacks, reserved_groups, split_record

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
            info.size, info.mode, info.mtime = len(files[name]), 0o644, 0
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
    return _encrypt(files, key)


def _encrypt(files: dict[str, bytes], key: str) -> bytes:
    salt = secrets.token_bytes(16)
    cipher = _openssl(["-salt"], _tar(files), key)
    tag = hmac.new(_mac_key(key, salt), salt + cipher, "sha256").digest()
    return MAGIC + salt + tag + cipher


def _decrypt(sealed: bytes, key: str | None) -> bytes:
    """Comprueba el HMAC y devuelve el tar.gz en claro, en memoria."""
    key = _check_key(key)
    if not sealed.startswith(MAGIC) or len(sealed) < len(MAGIC) + 48:
        raise HiddenError("no es un paquete reservado de Cachai")
    body = sealed[len(MAGIC):]
    salt, tag, cipher = body[:16], body[16:48], body[48:]
    if not hmac.compare_digest(tag, hmac.new(_mac_key(key, salt), salt + cipher, "sha256").digest()):
        raise HiddenError("la clave no corresponde o el paquete fue alterado")
    return _openssl(["-d"], cipher, key)


def _check_manifest(manifest: dict) -> dict:
    if manifest.get("format") != FORMAT:
        raise HiddenError(f"formato de paquete desconocido: {manifest.get('format')!r}")
    return manifest


def _split_of(manifest: dict) -> Split | None:
    return Split.from_dict(manifest["split"]) if manifest.get("split") else None


def open_package(sealed: bytes, key: str | None, dest: Path) -> HiddenMaterial:
    """Comprueba, descifra y extrae en `dest`.

    `dest` se crea con permisos 0700: en el host, nadie más que este proceso
    entra. Lo de adentro queda legible (0755 y 0644) porque se monta en el
    contenedor del evaluador, que corre con otro usuario: en un runner de CI
    ese uid no coincide con el del dueño de los archivos.
    """
    plain = _decrypt(sealed, key)
    dest.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
    for root, dirs, files in os.walk(dest):
        for d in dirs:
            os.chmod(Path(root) / d, 0o755)
        for f in files:
            os.chmod(Path(root) / f, 0o644)
    manifest = _check_manifest(json.loads((dest / MANIFEST).read_text()))
    attacks = tuple(load_attacks(dest / manifest["attacks"])) if manifest.get("attacks") else ()
    split = _split_of(manifest)
    tests = dest / manifest["holdout_tests"] if manifest.get("holdout_tests") else None
    return HiddenMaterial(attacks, split, tests)


def open_to_dir(sealed: bytes, key: str | None, dest: Path) -> HiddenMaterial:
    """Como `open_package`, pero borra `dest` si algo falla a medio extraer."""
    try:
        return open_package(sealed, key, dest)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


# --- Quemar y rotar ---------------------------------------------------------

BURN_TOKEN_LEN = 16  # hexadecimales


def _members(sealed: bytes, key: str | None) -> dict[str, bytes]:
    """Todos los archivos del paquete, descifrados en memoria. Nada toca el disco."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(_decrypt(sealed, key)), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile():
                f = tar.extractfile(m)
                files[m.name] = f.read() if f else b""
    if MANIFEST not in files:
        raise HiddenError("el paquete no trae manifiesto")
    return files


def _attacks_and_split(files: dict[str, bytes]) -> tuple[dict, list[Attack], Split]:
    manifest = _check_manifest(json.loads(files[MANIFEST]))
    split = _split_of(manifest)
    if not manifest.get("attacks") or split is None:
        raise HiddenError("el paquete no trae ataques: no hay nada que quemar ni rotar")
    attacks = [Attack.from_dict(d) for d in json.loads(files[manifest["attacks"]])]
    return manifest, attacks, split


def _store(files: dict[str, bytes], manifest: dict, attacks: list[Attack], split: Split, key: str) -> bytes:
    files = dict(files)
    files[manifest["attacks"]] = json.dumps([a.to_dict() for a in sorted(attacks, key=lambda a: a.id)], ensure_ascii=False).encode()
    files[MANIFEST] = json.dumps({**manifest, "split": split.to_dict()}, ensure_ascii=False).encode()
    return _encrypt(files, key)


def burn_token(key: str, base: str, group_key: tuple[str, str]) -> str:
    """Señal opaca de que una corrida reveló un grupo reservado. Va en el reporte público.

    Es un HMAC con la clave del paquete: sin la clave no dice qué grupo es ni
    se puede fabricar para otro grupo, así que el PR no elige qué se quema.
    Incluye el commit de la base para que el mismo grupo no se reconozca de una
    base a otra.
    """
    msg = "\x00".join(("cachai-burn", base, *group_key)).encode()
    return hmac.new(_check_key(key).encode(), msg, "sha256").hexdigest()[:BURN_TOKEN_LEN]


def groups_from_report(sealed: bytes, key: str, report: dict) -> tuple[set[tuple[str, str]], list[str]]:
    """Grupos que reveló una corrida, según las señales de su reporte.

    Devuelve (grupos, señales que no corresponden a ningún grupo del paquete).
    """
    base = report.get("base") if isinstance(report, dict) else None
    tokens = ((report.get("reserve") or {}).get("burn") or []) if isinstance(report, dict) else None
    if not isinstance(base, str) or not isinstance(tokens, list):
        raise HiddenError("el reporte no trae la base o las señales de quema")
    _, attacks, _ = _attacks_and_split(_members(sealed, key))
    by_token = {burn_token(key, base, g): g for g in {a.group_key for a in attacks}}
    return {by_token[t] for t in tokens if t in by_token}, [str(t) for t in tokens if t not in by_token]


@dataclass(frozen=True)
class BurnResult:
    burned: tuple[tuple[str, str], ...]  # grupos que pasaron de reservados a visibles
    already: tuple[tuple[str, str], ...]  # ya estaban quemados o ya eran visibles
    unknown: tuple[str, ...]  # grupos que no están en este paquete
    remaining: dict[str, int]  # grupos reservados que quedan por familia


def burn(sealed: bytes, key: str, groups: set[tuple[str, str]]) -> tuple[bytes | None, BurnResult]:
    """Pasa grupos reservados a visibles y los anota como quemados, dentro del paquete.

    Devuelve (paquete nuevo, o None si no cambió nada; resultado).
    """
    files = _members(sealed, key)
    manifest, attacks, split = _attacks_and_split(files)
    reserved = set(split.reserved)
    known = {a.group_key for a in attacks}
    to_burn = groups & {a.group_key for a in attacks if a.id in reserved}
    moving = {a.id for a in attacks if a.group_key in to_burn}
    new = Split(split.seed, split.shown_ratio,
                tuple(sorted(set(split.visible) | moving)),
                tuple(i for i in split.reserved if i not in moving),
                tuple(sorted(set(split.burned) | moving)))
    result = BurnResult(
        burned=tuple(sorted(to_burn)),
        already=tuple(sorted((groups & known) - to_burn)),
        unknown=tuple(f"{f}:{g}" for f, g in sorted(groups - known)),
        remaining={f: len(g) for f, g in reserved_groups(attacks, new).items()},
    )
    return (_store(files, manifest, attacks, new, key) if moving else None), result


def burned_families(attacks: list[Attack] | tuple[Attack, ...], split: Split) -> set[str]:
    burned = set(split.burned)
    return {a.family for a in attacks if a.id in burned}


@dataclass(frozen=True)
class RotateResult:
    new_groups: dict[str, int]  # grupos reservados nuevos por familia rotada
    joined: int  # ataques nuevos que entran en grupos que ya existían
    remaining: dict[str, int]  # grupos reservados que quedan por familia
    notes: tuple[str, ...]


def rotate(sealed: bytes, key: str, found: list[Attack], families: set[str] | None = None) -> tuple[bytes | None, RotateResult]:
    """Suma como reservados los grupos nuevos que encontró el equipo rojo.

    `found` son ataques ya confirmados contra la base. Cuentan solo los de
    `families` (por defecto, las que tienen algún grupo quemado) cuyo id no está
    en el paquete. Un grupo que no existía queda reservado entero; un ataque
    nuevo de un grupo que ya existía toma el lado de su grupo (si el grupo se
    quemó, queda visible: su ruta ya se mostró). Si una familia no recibe ningún
    grupo nuevo, lo dice en `notes`: no se inventa una variante.
    """
    files = _members(sealed, key)
    manifest, attacks, split = _attacks_and_split(files)
    families = burned_families(attacks, split) if families is None else set(families)
    ids, reserved = {a.id for a in attacks}, set(split.reserved)
    side = {a.group_key: a.id in reserved for a in attacks}
    new = [a for a in sorted(found, key=lambda a: a.id) if a.family in families and a.id not in ids]
    fresh: dict[str, set[tuple[str, str]]] = {f: set() for f in sorted(families)}
    add_reserved, add_visible, joined = set(), set(), 0
    for a in new:
        if a.group_key not in side:
            fresh[a.family].add(a.group_key)
            add_reserved.add(a.id)
            continue
        joined += 1
        (add_reserved if side[a.group_key] else add_visible).add(a.id)
    notes = [] if families else ["no hay familias con grupos quemados: no hay nada que rotar"]
    notes += [f"el equipo rojo no tiene variantes nuevas de {f}: todas las que sabe generar contra la base ya están en el paquete"
              for f, g in fresh.items() if not g]
    attacks2 = attacks + new
    split2 = Split(split.seed, split.shown_ratio,
                   tuple(sorted(set(split.visible) | add_visible)),
                   tuple(sorted(reserved | add_reserved)),
                   split.burned)
    result = RotateResult(
        new_groups={f: len(g) for f, g in fresh.items()},
        joined=joined,
        remaining={f: len(g) for f, g in reserved_groups(attacks2, split2).items()},
        notes=tuple(notes),
    )
    return (_store(files, manifest, attacks2, split2, key) if new else None), result
