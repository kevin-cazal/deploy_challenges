#!/usr/bin/env python3
"""
deploy_challenges.py — Deploy ctfcli-compatible challenges to a CTFd instance.

Clones a challenge repository (or uses a local directory), discovers all
challenge.yml files, installs them with ``ctf challenge install``, and
optionally syncs flags from private/flag.txt.gpg or private/flag.txt.

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
import os
import shutil
import subprocess
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

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
    return any(
        (yml.parent / "private" / "flag.txt.gpg").is_file()
        for yml in challenge_ymls
    )


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


def read_flag_plaintext(challenge_dir: Path) -> str | None:
    """Return flag value from private/flag.txt.gpg or private/flag.txt."""
    gpg_file = challenge_dir / "private" / "flag.txt.gpg"
    txt_file = challenge_dir / "private" / "flag.txt"

    if gpg_file.is_file():
        passphrase = os.environ.get("GPG_PASSPHRASE")
        if not passphrase:
            raise RuntimeError(
                f"{gpg_file} requires GPG_PASSPHRASE to be set in the environment"
            )
        value = decrypt_flag_file(gpg_file, passphrase)
        if not value:
            raise ValueError(f"decrypted flag is empty for {gpg_file}")
        return value

    if txt_file.is_file():
        value = txt_file.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"flag is empty in {txt_file}")
        return value

    return None


def get_challenge_name(yml: Path) -> str:
    """Read challenge display name from challenge.yml."""
    data = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Missing or invalid 'name' in challenge.yml")
    return name.strip()


def get_challenge_id(base_url: str, token: str, challenge_name: str) -> int | None:
    """Find challenge ID in CTFd by exact challenge name."""
    try:
        r = requests.get(
            f"{base_url}/api/v1/challenges",
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


def set_challenge_flag(
    base_url: str, token: str, challenge_id: int, flag_value: str
) -> None:
    """Create or update a static flag for a challenge via CTFd API."""
    headers = {
        "Authorization": f"Token {token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(f"{base_url}/api/v1/flags", headers=headers, timeout=15)
        r.raise_for_status()
        flags = r.json().get("data", [])
        existing = [
            f
            for f in flags
            if f.get("challenge_id") == challenge_id and isinstance(f.get("id"), int)
        ]

        payload = {
            "challenge_id": challenge_id,
            "type": "static",
            "content": flag_value,
            "data": "",
        }

        if existing:
            first = existing[0]
            requests.patch(
                f"{base_url}/api/v1/flags/{first['id']}",
                headers=headers,
                json=payload,
                timeout=15,
            ).raise_for_status()
            for extra in existing[1:]:
                requests.delete(
                    f"{base_url}/api/v1/flags/{extra['id']}",
                    headers=headers,
                    timeout=15,
                ).raise_for_status()
        else:
            requests.post(
                f"{base_url}/api/v1/flags",
                headers=headers,
                json=payload,
                timeout=15,
            ).raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"CTFd flag API error: {e}") from e


def sync_challenge_flag(
    yml: Path,
    base_url: str,
    token: str,
    *,
    sync_flags: bool,
) -> FlagSyncResult:
    """Update CTFd flag after a successful ctfcli install."""
    if not sync_flags:
        return FlagSyncResult.SKIPPED

    challenge_dir = yml.parent
    flag_value = read_flag_plaintext(challenge_dir)
    if flag_value is None:
        return FlagSyncResult.SKIPPED

    api_name = get_challenge_name(yml)
    challenge_id = get_challenge_id(base_url, token, api_name)
    if challenge_id is None:
        raise RuntimeError(f"challenge '{api_name}' not found in CTFd")

    set_challenge_flag(base_url, token, challenge_id, flag_value)
    return FlagSyncResult.SYNCED


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

def clone_or_pull(repo_url: str, dest: Path) -> None:
    """Clone the repo, or pull if it already exists."""
    if (dest / ".git").is_dir():
        print(f"  Pulling {repo_url} ...")
        subprocess.run(
            ["git", "-C", str(dest), "pull", "--ff-only"],
            check=True,
            capture_output=True,
        )
    else:
        print(f"  Cloning {repo_url} ...")
        subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            check=True,
            capture_output=True,
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
                    flag_result = sync_challenge_flag(
                        yml, base_url, token, sync_flags=sync_flags
                    )
                    if duplicate:
                        suffix = (
                            "already exists, flag synced"
                            if flag_result == FlagSyncResult.SYNCED
                            else "already exists"
                        )
                        print(f"    ⏭  {challenge_name} ({suffix})")
                    elif flag_result == FlagSyncResult.SYNCED:
                        print(f"    ✔ {challenge_name} (flag synced)")
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
            print(f"Error cloning repo: {stderr}", file=sys.stderr)
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
                "Error: at least one challenge has private/flag.txt.gpg "
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
        force=args.force,
        sync_flags=sync_flags,
    )

    if cleanup_dir:
        shutil.rmtree(cleanup_dir, ignore_errors=True)

    print(f"\n==> Done: {ok}/{len(challenge_ymls)} challenge(s) deployed.")
    if ok < len(challenge_ymls):
        sys.exit(1)


if __name__ == "__main__":
    main()
