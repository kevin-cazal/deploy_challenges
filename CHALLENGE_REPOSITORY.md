# Challenge repository format

This document describes the **ctfcli-compatible** layout expected by [deploy_challenges](README.md). It matches repositories such as [osint_investigator_ctf_challenges](https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges).

For the upstream ctfcli specification, see [CTFd/ctfcli](https://github.com/CTFd/ctfcli).

## Repository layout

A challenge repo is a git tree (or local directory) containing one folder per challenge. Each folder has a `challenge.yml` at its root. Folders may be nested by category:

```text
my_ctf_challenges/
├── README.md
├── index.html              # optional — CTFd home page (HTML fragment); see deploy_challenges
├── encrypt.sh              # optional — GPG workflow at repo root
├── decrypt.sh
├── challenges/             # common root; use --subdir challenges when deploying
│   ├── introduction/
│   │   └── tutoriel/
│   │       ├── challenge.yml
│   │       ├── evidence.png          # optional player files
│   │       └── private/
│   │           ├── flag.txt.gpg      # committed (encrypted)
│   │           └── writeup.md.gpg    # optional, not used by deploy_challenges
│   └── osint/
│       └── some_challenge/
│           ├── challenge.yml
│           └── private/
│               └── flag.txt.gpg
└── .gitignore              # should ignore decrypted secrets
```

`deploy_challenges` discovers every `challenge.yml` recursively under the source path (after `--subdir`, if set).

## `challenge.yml`

Each challenge is defined by a YAML file processed by **ctfcli** `challenge install`. Required field for deployment:

| Field | Required | Notes |
|-------|----------|--------|
| `name` | yes | Display name in CTFd; used to match the challenge when syncing flags |
| `category` | recommended | Category slug or label |
| `description` | recommended | Markdown shown to players |
| `value` | recommended | Point value |
| `type` | recommended | Usually `standard` |

Other common ctfcli fields (see ctfcli docs for the full schema):

| Field | Purpose |
|-------|---------|
| `author`, `attribution` | Credits |
| `tags` | List of tags |
| `state` | e.g. `visible`, `hidden` |
| `files` | List of paths to upload as challenge attachments |
| `connection_info` | Host/port or connection hint for players |
| `attempts` | Max attempts (0 = unlimited) |
| `logic` | Flag matching logic (e.g. `any`) |

Minimal example:

```yaml
name: My challenge
author: author
category: misc
description: |
  Solve this puzzle.
value: 100
type: standard
state: visible
tags:
  - easy
```

Reference example: [tutoriel/challenge.yml](https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges/blob/main/challenges/introduction/tutoriel/challenge.yml).

### Player files

Binary or text assets players download belong **next to** `challenge.yml` (not under `private/`). Reference them in `files:` when using ctfcli’s file upload:

```yaml
files:
  - evidence.png
  - notes.txt
```

Embed images in the `description` with markdown, using the **filename only**:

```yaml
description: |
  ![Screenshot](evidence.png)
files:
  - evidence.png
```

`deploy_challenges` uploads the files then rewrites those links to CTFd `/files/…` URLs (including your instance path prefix, e.g. `/ctfd/default/files/…`).

Challenges with only a `description` and no attachments are valid (description-only / awaiting assets).

## Flags and `private/`

Flags must not be committed in plaintext when they reveal exact answers. Layout:

| Path | Committed | Used by deploy_challenges |
|------|-----------|---------------------------|
| `private/flag.txt` | **no** (gitignored) | **Static** flag synced to CTFd API |
| `private/flag.txt.gpg` | yes | Decrypted with `GPG_PASSPHRASE` |
| `private/writeup.md` | yes (optional) | Organizer only; not deployed |

QCM challenges use one static flag (e.g. `shell1{B}`). MCQ flags are deployed with `case_insensitive` matching.

Legacy `private/flag.yml` with `regex`, `dynamic`, or `custom` is **ignored** (deploy prints a warning).

After `ctf challenge install`, the deployer syncs **static** flags via the CTFd API.

### Flag in description only

Some challenges (e.g. **Tutoriel**) embed the flag in the public `description` for onboarding. They may have no flag files. Deploy still succeeds; flag sync is skipped when nothing is defined.

### GPG workflow (challenge repo)

At the **challenges/** root (shell-1) or challenge repo root:

```bash
export GPG_PASSPHRASE='…'   # never commit .gpg-passphrase
./decrypt.sh    # before editing flags
./encrypt.sh    # before git commit
```

`deploy_challenges` decrypts `flag.txt.gpg` in-process when `GPG_PASSPHRASE` is set.

## How deploy_challenges uses a repo

1. Clone the git URL (or use `--no-clone` on a local path).
2. Optionally restrict to `--subdir` (e.g. `challenges`).
3. Find all `challenge.yml` files.
4. For each file: run `ctf challenge install <path>` against `--url` / `--token`.
5. Sync flags from `private/flag.txt` (unless `--no-sync-flags`): POST static flags to CTFd.

| Deploy flag | Effect |
|-------------|--------|
| `--subdir challenges` | Root for discovery is `<repo>/challenges/` |
| `--force` | Pass `--force` to ctfcli (overwrite existing challenges) |
| `--no-sync-flags` | Skip step 5 |
| `GPG_PASSPHRASE` | Required when any challenge has `flag.txt.gpg` |

Prerequisites, unlock chains, and other CTFd relationships are configured in the CTFd admin UI after deploy.

## Checklist for a new challenge repo

1. One directory per challenge with `challenge.yml` (valid `name`, `description`, `value`, `type`, …).
2. Player files beside `challenge.yml`; list them in `files:` if needed.
3. `private/flag.txt.gpg` for real flags; never commit `private/flag.txt` or `.gpg-passphrase`.
4. `.gitignore` for decrypted secrets and local passphrase files.
5. Deploy with `--subdir` pointing at the folder that contains category subfolders or flat challenge dirs.

## Reference repository

[github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges](https://github.com/Manta-Epitech-Academy/osint_investigator_ctf_challenges) — 29 challenges under `challenges/`, GPG-encrypted flags, ctfcli format.
