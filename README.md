# deploy_challenges

Deploy [ctfcli](https://github.com/CTFd/ctfcli)-compatible challenge repositories to a CTFd instance.

Challenge repositories must follow the [ctfcli-compatible layout](CHALLENGE_REPOSITORY.md). Reference implementation: [osint_investigator_ctf_challenges](https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges).

## Docker image

Published to [GitHub Container Registry](https://github.com/kevin-cazal/deploy_challenges/pkgs/container/deploy_challenges):

```text
ghcr.io/kevin-cazal/deploy_challenges:latest
```

Tags: `latest` (default branch), commit SHA, and `v*` semver on git tags.

### Deploy from a git repo (CTFd on the host)

```bash
export GPG_PASSPHRASE='…'   # required if challenges use private/flag.txt.gpg

docker run --rm \
  -e GPG_PASSPHRASE \
  --add-host=host.docker.internal:host-gateway \
  ghcr.io/kevin-cazal/deploy_challenges:latest \
  https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges.git \
  --subdir challenges \
  --url http://host.docker.internal:9042/ctfd/default \
  --token ctfd_YOUR_ADMIN_TOKEN
```

On macOS/Windows Docker Desktop, `host.docker.internal` is usually available without `--add-host`.

### Local challenge directory

```bash
docker run --rm \
  -e GPG_PASSPHRASE \
  -v "$PWD/challenges:/challenges:ro" \
  --add-host=host.docker.internal:host-gateway \
  ghcr.io/kevin-cazal/deploy_challenges:latest \
  /challenges --no-clone \
  --url http://host.docker.internal:9042/ctfd/default \
  --token ctfd_YOUR_ADMIN_TOKEN
```

### Options

| Flag | Description |
|------|-------------|
| `--url` | CTFd API base URL (required) |
| `--token` | Admin API token (required) |
| `--subdir` | Subdirectory inside the source repo |
| `--no-clone` | Source is a local path |
| `--force` | Re-sync existing challenges |
| `--wait` | Seconds to wait for CTFd (default 60) |
| `--no-sync-flags` | Skip GPG/API flag sync |
| `GPG_PASSPHRASE` | Decrypt `private/flag.txt.gpg` or `flag.yml.gpg` |
| `SHELL1_FLAG_SECRET` | CTFd env for `type: dynamic` flags (plugin) |

### CTFd home page

If the challenge root contains **`index.html`** (HTML fragment), deploy updates the CTFd page with route **`index`** (create or patch). Omit the file to leave the instance home page unchanged.

Flag sync supports **`private/flag.yml`** (`static`, `regex`, `dynamic`, `custom`) and legacy **`private/flag.txt`**. See [CHALLENGE_REPOSITORY.md](CHALLENGE_REPOSITORY.md).

## Build locally

```bash
docker build -t deploy-challenges .
docker run --rm deploy-challenges --help
```

## Run without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export GPG_PASSPHRASE='…'
.venv/bin/python deploy_challenges.py … --url … --token …
```

Requires **git** and **gpg** on the host for clone and encrypted flags.

## License

See repository defaults.
