#!/usr/bin/env python3
"""
deploy_challenges.py — Deploy ctfcli-compatible challenges to a CTFd instance.

Clones a challenge repository (or uses a local directory), discovers all
challenge.yml files, installs them with ``ctf challenge install``, and
optionally syncs flags from private/flag.yml, flag.txt, or GPG-encrypted variants.

Docker (recommended):

    docker run --rm \\
      -e GPG_PASSPHRASE='…' \\
      --add-host=host.docker.internal:host-gateway \\
      ghcr.io/kevin-cazal/deploy_challenges:latest \\
      https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges.git \\
      --subdir challenges \\
      --url http://host.docker.internal:9042/ctfd/default \\
      --token ctfd_…

Local:

    export GPG_PASSPHRASE='…'   # only if challenges use private/flag.txt.gpg
    pip install -r requirements.txt
    python3 deploy_challenges.py … --url … --token …

Provide --url and --token from your CTFd admin settings (not read from compose).
"""

from __future__ import annotations

import argparse
import configparser
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
import yaml

MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# ---------------------------------------------------------------------------
# Resolve ctfcli binary
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
VENV_CTF = SCRIPT_DIR / ".venv" / "bin" / "ctf"


def _find_ctf_bin() -> str:
    """Return the path to the ``ctf`` binary."""
    if VENV_CTF.is_file():
        return str(VENV_CTF)
    ctf = shutil.which("ctf")
    if ctf:
        return ctf
    print(
        "Error: ctfcli not found. Use the Docker image:\n"
        "  docker run --rm ghcr.io/kevin-cazal/deploy_challenges:latest --help\n"
        "Or install locally:\n"
        "  pip install -r requirements.txt",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# ctfcli project setup
# ---------------------------------------------------------------------------

def create_ctfcli_project(work_dir: Path, url: str, token: str) -> None:
    """Create a minimal .ctf/config so ctfcli can operate from *work_dir*."""
    ctf_dir = work_dir / ".ctf"
    ctf_dir.mkdir(parents=True, exist_ok=True)

    config = configparser.ConfigParser()
    config.optionxform = str
    config["config"] = {
        "url": url,
        "access_token": token,
    }
    config["challenges"] = {}

    with open(ctf_dir / "config", "w") as f:
        config.write(f)


# ---------------------------------------------------------------------------
# Challenge discovery
# ---------------------------------------------------------------------------

def find_challenge_ymls(root: Path) -> list[Path]:
    """Find all challenge.yml files under *root*, sorted by path."""
    return sorted(root.rglob("challenge.yml"))


def needs_gpg_passphrase(challenge_ymls: list[Path]) -> bool:
    """True if any challenge has an encrypted flag file."""
    for yml in challenge_ymls:
        priv = yml.parent / "private"
        if (priv / "flag.txt.gpg").is_file() or (priv / "flag.yml.gpg").is_file():
            return True
    return False


# ---------------------------------------------------------------------------
# Flag handling
# ---------------------------------------------------------------------------

class FlagSyncResult(Enum):
    SYNCED = "synced"
    SKIPPED = "skipped"


def decrypt_flag_file(flag_file: Path, passphrase: str) -> str:
    """Decrypt a GPG-encrypted flag file and return the plaintext flag."""
    with tempfile.NamedTemporaryFile(prefix="ctfd-flag-", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--decrypt",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                passphrase,
                "-o",
                str(out_path),
                str(flag_file),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return out_path.read_text(encoding="utf-8").strip()
    finally:
        out_path.unlink(missing_ok=True)


def decrypt_text_file(flag_file: Path) -> str:
    """Decrypt a GPG-encrypted text file and return plaintext."""
    passphrase = os.environ.get("GPG_PASSPHRASE")
    if not passphrase:
        raise RuntimeError(
            f"{flag_file} requires GPG_PASSPHRASE to be set in the environment"
        )
    value = decrypt_flag_file(flag_file, passphrase)
    if not value:
        raise ValueError(f"decrypted file is empty for {flag_file}")
    return value


def read_flag_plaintext(challenge_dir: Path) -> str | None:
    """Return static flag from private/flag.txt.gpg or private/flag.txt (legacy)."""
    priv = challenge_dir / "private"
    gpg_file = priv / "flag.txt.gpg"
    txt_file = priv / "flag.txt"

    if gpg_file.is_file():
        return decrypt_text_file(gpg_file)

    if txt_file.is_file():
        value = txt_file.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"flag is empty in {txt_file}")
        return value

    return None


def read_flag_yaml(challenge_dir: Path) -> dict | list | None:
    """Load private/flag.yml or flag.yml.gpg."""
    priv = challenge_dir / "private"
    gpg_file = priv / "flag.yml.gpg"
    yml_file = priv / "flag.yml"

    if gpg_file.is_file():
        text = decrypt_text_file(gpg_file)
        return yaml.safe_load(text)

    if yml_file.is_file():
        return yaml.safe_load(yml_file.read_text(encoding="utf-8"))

    return None


def normalize_flag_definitions(raw: dict | list | None) -> list[dict[str, Any]]:
    """Turn flag.yml content into a list of flag definition dicts."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [dict(f) for f in raw]
    if isinstance(raw, dict):
        if "flags" in raw and isinstance(raw["flags"], list):
            return [dict(f) for f in raw["flags"]]
        return [dict(raw)]
    raise ValueError(f"invalid flag.yml structure: {type(raw)}")


def load_flag_definitions(challenge_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Return (api_flags, plugin_specs).
    api_flags: static/regex for CTFd /api/v1/flags.
    plugin_specs: dynamic/custom for shell1_flags plugin.
    """
    raw = read_flag_yaml(challenge_dir)
    definitions = normalize_flag_definitions(raw)

    if not definitions:
        txt = read_flag_plaintext(challenge_dir)
        if txt:
            data = "case_sensitive"
            if re.fullmatch(r"shell1\{[A-D]\}", txt.strip(), re.IGNORECASE):
                data = "case_insensitive"
            definitions = [
                {"type": "static", "content": txt.strip(), "data": data}
            ]

    api_flags: list[dict[str, Any]] = []
    plugin_specs: list[dict[str, Any]] = []
    for spec in definitions:
        ftype = str(spec.get("type", "static")).lower()
        if ftype == "static":
            api_flags.append(spec)
        elif ftype in ("regex", "dynamic", "custom"):
            print(
                f"  ⚠ {challenge_dir.name}: ignoring flag.yml type={ftype} "
                f"(use private/flag.txt static flag only)",
                file=sys.stderr,
            )
        else:
            raise ValueError(f"unknown flag type '{ftype}' in {challenge_dir}")
    return api_flags, plugin_specs


def get_challenge_name(yml: Path) -> str:
    """Read challenge display name from challenge.yml."""
    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' in challenge.yml")
    return name.strip()


def parse_challenge_requirements(
    yml: Path,
) -> tuple[list[str], bool] | None:
    """Return (prerequisite challenge names, anonymize) or None if no requirements."""
    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    req = data.get("requirements")
    if not req:
        return None
    if isinstance(req, dict):
        raw = req.get("prerequisites") or []
        names = [str(x) for x in raw if x]
        anonymize = bool(req.get("anonymize", False))
        return names, anonymize
    if isinstance(req, list):
        return [str(x) for x in req if x], False
    return None


def sync_challenge_requirements(
    base_url: str,
    token: str,
    challenge_id: int,
    yml: Path,
) -> None:
    """PATCH CTFd prerequisites (hidden until unlocked when anonymize is false)."""
    parsed = parse_challenge_requirements(yml)
    if not parsed:
        return
    names, anonymize = parsed
    headers = _api_headers(token)
    r = requests.get(
        f"{base_url}/api/v1/challenges",
        params={"view": "admin"},
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    by_name = {c["name"]: c["id"] for c in r.json().get("data", []) if c.get("name")}
    prereq_ids: list[int] = []
    for name in names:
        cid = by_name.get(name)
        if isinstance(cid, int):
            prereq_ids.append(cid)
    prereq_ids = sorted(set(prereq_ids))
    if challenge_id in prereq_ids:
        prereq_ids.remove(challenge_id)
    requests.patch(
        f"{base_url}/api/v1/challenges/{challenge_id}",
        headers=headers,
        json={
            "requirements": {
                "prerequisites": prereq_ids,
                "anonymize": anonymize,
            }
        },
        timeout=15,
    ).raise_for_status()


def application_root(base_url: str) -> str:
    """Path prefix for CTFd (e.g. ``/ctfd/default``), or empty at site root."""
    return (urlparse(base_url.rstrip("/")).path or "").rstrip("/")


def challenge_file_url_map(
    base_url: str, token: str, challenge_id: int
) -> dict[str, str]:
    """Map attachment basename -> public ``/files/`` URL (no download token)."""
    headers = _api_headers(token)
    r = requests.get(
        f"{base_url.rstrip('/')}/api/v1/challenges/{challenge_id}",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", {})
    root = application_root(base_url)
    out: dict[str, str] = {}
    for file_url in data.get("files") or []:
        if not isinstance(file_url, str) or "/files/" not in file_url:
            continue
        path = file_url.split("?", 1)[0]
        location = path.split("/files/", 1)[-1]
        basename = location.rsplit("/", 1)[-1]
        out[basename] = f"{root}/files/{location}"
    return out


def rewrite_description_image_links(
    description: str, file_urls: dict[str, str]
) -> str:
    """Replace ``![alt](file.png)`` with CTFd ``/files/`` URLs when uploaded."""
    if not file_urls:
        return description

    def repl(match: re.Match[str]) -> str:
        alt, path = match.group(1), match.group(2)
        if path.startswith(("http://", "https://", "/")):
            return match.group(0)
        name = path.split("/")[-1]
        if name not in file_urls:
            return match.group(0)
        return f"![{alt}]({file_urls[name]})"

    return MD_IMAGE_RE.sub(repl, description)


def sync_challenge_description_images(
    base_url: str, token: str, challenge_id: int, yml: Path
) -> bool:
    """Patch challenge description so embedded markdown images use CTFd file URLs."""
    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    description = data.get("description")
    if not isinstance(description, str) or "![" not in description:
        return False
    file_urls = challenge_file_url_map(base_url, token, challenge_id)
    new_desc = rewrite_description_image_links(description, file_urls)
    if new_desc == description:
        return False
    requests.patch(
        f"{base_url.rstrip('/')}/api/v1/challenges/{challenge_id}",
        headers=_api_headers(token),
        json={"description": new_desc},
        timeout=15,
    ).raise_for_status()
    return True


def find_home_page(challenge_root: Path) -> Path | None:
    """Return challenges/index.html when present at the deploy root."""
    p = challenge_root / "index.html"
    return p if p.is_file() else None


def sync_ctfd_home_page(base_url: str, token: str, html_path: Path) -> None:
    """Create or update CTFd page route ``index`` from an HTML fragment file."""
    content = html_path.read_text(encoding="utf-8")
    headers = _api_headers(token)
    r = requests.get(
        f"{base_url}/api/v1/pages",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    pages = r.json().get("data", [])
    index_page = next(
        (p for p in pages if p.get("route") == "index" and isinstance(p.get("id"), int)),
        None,
    )
    payload: dict[str, Any] = {
        "route": "index",
        "content": content,
        "format": "html",
        "draft": False,
        "hidden": False,
        "auth_required": False,
    }
    if index_page:
        payload["title"] = index_page.get("title") or "Accueil"
        requests.patch(
            f"{base_url}/api/v1/pages/{index_page['id']}",
            headers=headers,
            json=payload,
            timeout=15,
        ).raise_for_status()
    else:
        payload["title"] = "Accueil"
        requests.post(
            f"{base_url}/api/v1/pages",
            headers=headers,
            json=payload,
            timeout=15,
        ).raise_for_status()


def get_challenge_id(base_url: str, token: str, challenge_name: str) -> int | None:
    """Find challenge ID in CTFd by exact challenge name."""
    try:
        r = requests.get(
            f"{base_url}/api/v1/challenges",
            params={"view": "admin"},
            headers={
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        r.raise_for_status()
        for item in r.json().get("data", []):
            if item.get("name") == challenge_name:
                cid = item.get("id")
                if isinstance(cid, int):
                    return cid
    except requests.RequestException:
        return None
    return None


def _api_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }


def delete_challenge_flags(base_url: str, token: str, challenge_id: int) -> None:
    headers = _api_headers(token)
    r = requests.get(f"{base_url}/api/v1/flags", headers=headers, timeout=15)
    r.raise_for_status()
    for f in r.json().get("data", []):
        if f.get("challenge_id") == challenge_id and isinstance(f.get("id"), int):
            requests.delete(
                f"{base_url}/api/v1/flags/{f['id']}",
                headers=headers,
                timeout=15,
            ).raise_for_status()


def post_challenge_flag(
    base_url: str, token: str, challenge_id: int, spec: dict[str, Any]
) -> str:
    """POST one static or regex flag. Returns type string for logging."""
    ftype = str(spec.get("type", "static")).lower()
    if ftype not in ("static", "regex"):
        raise ValueError(f"cannot POST flag type '{ftype}' to CTFd API")
    content = str(spec.get("content", "")).strip()
    if not content:
        raise ValueError("flag content is empty")
    data = spec.get("data") or (
        "case_insensitive" if ftype == "regex" else "case_sensitive"
    )
    payload = {
        "challenge_id": challenge_id,
        "type": ftype,
        "content": content,
        "data": data,
    }
    requests.post(
        f"{base_url}/api/v1/flags",
        headers=_api_headers(token),
        json=payload,
        timeout=15,
    ).raise_for_status()
    return ftype


def sync_challenge_flags(
    base_url: str, token: str, challenge_id: int, api_flags: list[dict[str, Any]]
) -> list[str]:
    """Replace all CTFd flags for a challenge with api_flags. Returns synced types."""
    delete_challenge_flags(base_url, token, challenge_id)
    synced: list[str] = []
    for spec in api_flags:
        synced.append(post_challenge_flag(base_url, token, challenge_id, spec))
    return synced


def patch_challenge_shell1_spec(
    base_url: str,
    token: str,
    challenge_id: int,
    plugin_specs: list[dict[str, Any]],
) -> None:
    """Store dynamic/custom flag specs in challenge connection_info JSON."""
    if not plugin_specs:
        return
    headers = _api_headers(token)
    r = requests.get(
        f"{base_url}/api/v1/challenges/{challenge_id}",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()
    chal = r.json().get("data") or {}
    conn_raw = chal.get("connection_info")
    if isinstance(conn_raw, str) and conn_raw.strip():
        try:
            conn = json.loads(conn_raw)
        except json.JSONDecodeError:
            conn = {}
    elif isinstance(conn_raw, dict):
        conn = conn_raw
    else:
        conn = {}
    conn["shell1_flag"] = (
        plugin_specs[0] if len(plugin_specs) == 1 else plugin_specs
    )
    requests.patch(
        f"{base_url}/api/v1/challenges/{challenge_id}",
        headers=headers,
        json={"connection_info": json.dumps(conn, ensure_ascii=False)},
        timeout=15,
    ).raise_for_status()


def write_flag_specs_manifest(challenge_root: Path, specs: dict[str, Any]) -> None:
    deploy_dir = challenge_root / ".deploy"
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "flag_specs.json").write_text(
        json.dumps(specs, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def sync_challenge_flag(
    yml: Path,
    base_url: str,
    token: str,
    *,
    sync_flags: bool,
    manifest: dict[str, Any],
) -> tuple[FlagSyncResult, str]:
    """Update CTFd flags and manifest after a successful ctfcli install."""
    if not sync_flags:
        return FlagSyncResult.SKIPPED, ""

    challenge_dir = yml.parent
    api_flags, plugin_specs = load_flag_definitions(challenge_dir)
    if not api_flags and not plugin_specs:
        return FlagSyncResult.SKIPPED, ""

    api_name = get_challenge_name(yml)
    challenge_id = get_challenge_id(base_url, token, api_name)
    if challenge_id is None:
        raise RuntimeError(f"challenge '{api_name}' not found in CTFd")

    detail_parts: list[str] = []
    if api_flags:
        types = sync_challenge_flags(base_url, token, challenge_id, api_flags)
        detail_parts.append(",".join(types))
    if plugin_specs:
        patch_challenge_shell1_spec(base_url, token, challenge_id, plugin_specs)
        root = Path(manifest["_root"])
        try:
            rel = str(yml.parent.relative_to(root))
        except ValueError:
            rel = str(yml.parent)
        manifest[api_name] = {"path": rel, "specs": plugin_specs}
        detail_parts.append("plugin")

    return FlagSyncResult.SYNCED, "+".join(detail_parts)


# ---------------------------------------------------------------------------
# Instance readiness
# ---------------------------------------------------------------------------

def wait_for_instance(base_url: str, timeout: int) -> bool:
    """Block until the CTFd instance responds (non-5xx)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(
                f"{base_url}/api/v1/challenges",
                timeout=5,
                allow_redirects=True,
            )
            if r.status_code < 500:
                return True
        except (requests.ConnectionError, requests.Timeout):
            pass
        time.sleep(2)
    return False


def verify_token(base_url: str, token: str) -> str | None:
    """Check that the API token works. Returns admin name or None."""
    try:
        r = requests.get(
            f"{base_url}/api/v1/users/me",
            headers={
                "Authorization": f"Token {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if r.ok:
            return r.json().get("data", {}).get("name")
    except requests.RequestException:
        pass
    return None


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _is_ssh_repo_url(repo_url: str) -> bool:
    return repo_url.startswith("git@") or repo_url.startswith("ssh://")


def _git_env_for_clone(repo_url: str) -> dict[str, str] | None:
    """Environment for git when cloning over SSH."""
    if not _is_ssh_repo_url(repo_url):
        return None
    env = os.environ.copy()
    if "GIT_SSH_COMMAND" not in env:
        # Host-mounted ~/.ssh/config often has wrong owner/perms inside Docker.
        env["GIT_SSH_COMMAND"] = "ssh -F /dev/null -o BatchMode=yes"
    return env


def _ssh_clone_hint(repo_url: str) -> str:
    if not _is_ssh_repo_url(repo_url):
        return ""
    return (
        "\n  SSH clone failed. Mount your key into the container, e.g.:\n"
        '    -v "$HOME/.ssh:/root/.ssh:ro"\n'
        "  Or mount only the private key (avoids config permission issues):\n"
        '    -v "$HOME/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro"\n'
        "  Or forward ssh-agent:\n"
        '    -v "$SSH_AUTH_SOCK:/ssh-agent" -e SSH_AUTH_SOCK=/ssh-agent'
    )


def clone_or_pull(repo_url: str, dest: Path) -> None:
    """Clone the repo, or pull if it already exists."""
    git_env = _git_env_for_clone(repo_url)
    if (dest / ".git").is_dir():
        print(f"  Pulling {repo_url} ...")
        subprocess.run(
            ["git", "-C", str(dest), "pull", "--ff-only"],
            check=True,
            capture_output=True,
            env=git_env,
        )
    else:
        print(f"  Cloning {repo_url} ...")
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            check=True,
            capture_output=True,
            env=git_env,
        )


# ---------------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------------

def _deploy_label(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.netloc:
        return parsed.netloc + (parsed.path or "")
    return base_url


def deploy_challenges(
    ctf_bin: str,
    challenge_ymls: list[Path],
    base_url: str,
    token: str,
    timeout: int,
    challenge_root: Path,
    *,
    force: bool = False,
    sync_flags: bool = True,
) -> int:
    """Install challenges to CTFd. Returns success count."""
    label = _deploy_label(base_url)
    print(f"\n── {label} ──")
    print(f"  {base_url}")

    print("  ⏳ Waiting for CTFd ...")
    if not wait_for_instance(base_url, timeout):
        print(f"  ✘ Timed out after {timeout}s")
        return 0

    admin_name = verify_token(base_url, token)
    if not admin_name:
        print("  ✘ API token rejected")
        return 0
    print(f"  ✔ Authenticated as '{admin_name}'")

    work_dir = Path(tempfile.mkdtemp(prefix="ctfcli-deploy-"))
    create_ctfcli_project(work_dir, base_url, token)

    manifest: dict[str, Any] = {"_root": str(challenge_root.resolve())}
    ok = 0
    for yml in challenge_ymls:
        challenge_dir = yml.parent
        challenge_name = challenge_dir.name
        abs_yml = str(yml.resolve())

        cmd = [ctf_bin, "challenge", "install", abs_yml]
        if force:
            cmd.append("--force")

        try:
            result = subprocess.run(
                cmd,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (result.stdout + result.stderr).strip()
            install_ok = result.returncode == 0
            duplicate = (
                not install_ok
                and "already existing challenge" in output.lower()
                and not force
            )

            if install_ok or duplicate:
                try:
                    api_name = get_challenge_name(yml)
                    cid = get_challenge_id(base_url, token, api_name)
                    if cid is not None:
                        sync_challenge_requirements(
                            base_url, token, cid, yml
                        )
                        sync_challenge_description_images(
                            base_url, token, cid, yml
                        )
                    flag_result, flag_detail = sync_challenge_flag(
                        yml, base_url, token, sync_flags=sync_flags, manifest=manifest
                    )
                    if duplicate:
                        suffix = (
                            f"already exists, flag synced ({flag_detail})"
                            if flag_result == FlagSyncResult.SYNCED
                            else "already exists"
                        )
                        print(f"    ⏭  {challenge_name} ({suffix})")
                    elif flag_result == FlagSyncResult.SYNCED:
                        print(f"    ✔ {challenge_name} (flag synced: {flag_detail})")
                    else:
                        print(f"    ✔ {challenge_name}")
                    ok += 1
                except Exception as e:
                    state = "exists but" if duplicate else "deployed but"
                    print(f"    ✘ {challenge_name}: {state} flag update failed: {e}")
            else:
                print(f"    ✘ {challenge_name}: {output}")
        except subprocess.TimeoutExpired:
            print(f"    ✘ {challenge_name}: timed out (120s)")
        except Exception as e:
            print(f"    ✘ {challenge_name}: {e}")

    shutil.rmtree(work_dir, ignore_errors=True)
    if sync_flags and len(manifest) > 1:
        write_flag_specs_manifest(challenge_root, manifest)
        print(f"  ✔ Wrote {challenge_root / '.deploy' / 'flag_specs.json'}")
    print(f"  => {ok}/{len(challenge_ymls)} challenge(s) deployed")
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy ctfcli-compatible challenges to a CTFd instance.",
    )
    parser.add_argument(
        "source",
        help="Git repo URL or local directory containing challenges",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="CTFd API base URL (e.g. http://localhost:9042/ctfd/default)",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="CTFd admin API token",
    )
    parser.add_argument(
        "--subdir",
        "-s",
        default=None,
        help="Subdirectory within the source that contains challenge folders",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Treat source as a local directory (skip git clone)",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Re-sync challenges that already exist on the instance",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=60,
        help="Seconds to wait for CTFd to become ready (default: 60)",
    )
    parser.add_argument(
        "--no-sync-flags",
        action="store_true",
        help="Skip GPG/API flag sync (ctfcli install only)",
    )
    args = parser.parse_args()

    ctf_bin = _find_ctf_bin()
    print(f"Using ctfcli: {ctf_bin}")

    base_url = args.url.rstrip("/")
    token = args.token
    sync_flags = not args.no_sync_flags

    cleanup_dir: str | None = None
    if args.no_clone or Path(args.source).is_dir():
        challenge_root = Path(args.source)
    else:
        cleanup_dir = tempfile.mkdtemp(prefix="ctfd-challenges-")
        challenge_root = Path(cleanup_dir)
        try:
            clone_or_pull(args.source, challenge_root)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else e.stderr
            print(f"Error cloning repo: {stderr}{_ssh_clone_hint(args.source)}", file=sys.stderr)
            shutil.rmtree(cleanup_dir, ignore_errors=True)
            sys.exit(1)

    if args.subdir:
        challenge_root = challenge_root / args.subdir

    if not challenge_root.is_dir():
        print(
            f"Error: Challenge directory '{challenge_root}' not found.",
            file=sys.stderr,
        )
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        sys.exit(1)

    challenge_ymls = find_challenge_ymls(challenge_root)
    if not challenge_ymls:
        print(f"No challenge.yml files found under {challenge_root}")
        if cleanup_dir:
            shutil.rmtree(cleanup_dir, ignore_errors=True)
        return

    if sync_flags and needs_gpg_passphrase(challenge_ymls):
        if not os.environ.get("GPG_PASSPHRASE"):
            print(
                "Error: at least one challenge has private/flag.txt.gpg or flag.yml.gpg "
                "but GPG_PASSPHRASE is not set.",
                file=sys.stderr,
            )
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            sys.exit(1)

    print(f"==> Found {len(challenge_ymls)} challenge(s) in {challenge_root}\n")
    for yml in challenge_ymls:
        rel = yml.parent.relative_to(challenge_root)
        print(f"    {rel}/")

    ok = deploy_challenges(
        ctf_bin=ctf_bin,
        challenge_ymls=challenge_ymls,
        base_url=base_url,
        token=token,
        timeout=args.wait,
        challenge_root=challenge_root,
        force=args.force,
        sync_flags=sync_flags,
    )

    home_page = find_home_page(challenge_root)
    if home_page:
        try:
            sync_ctfd_home_page(base_url, token, home_page)
            print("\n  ✔ CTFd home page (index) updated from index.html")
        except requests.RequestException as e:
            print(f"\n  ✘ CTFd home page sync failed: {e}", file=sys.stderr)
            if cleanup_dir:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
            sys.exit(1)

    if cleanup_dir:
        shutil.rmtree(cleanup_dir, ignore_errors=True)

    print(f"\n==> Done: {ok}/{len(challenge_ymls)} challenge(s) deployed.")
    if ok < len(challenge_ymls):
        sys.exit(1)


if __name__ == "__main__":
    main()
