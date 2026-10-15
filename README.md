# Navidrome × qBittorrent Cleanup

**Use Navidrome's ❤️ starred songs as a cleanup queue.** The script finds their corresponding qBittorrent files, marks matched tracks **Do not download**, and moves unmatched tracks into a quarantine folder outside your music library.

![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue) ![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen) ![Default](https://img.shields.io/badge/default-preview_only-orange)

> [!CAUTION]
> **Starred means unwanted** in this project. Don't use it if you heart songs you want to keep! The script does **not permanently delete** files, but moving them or changing torrent priorities can affect torrents and seeding.

## What it does

| Starred song | Result |
| --- | --- |
| Exactly matches a qBittorrent file (path + size) | Set **Do not download** (priority `0`) |
| Does not match a readable torrent | Move to a **quarantine** folder outside Navidrome |
| Is ambiguous, unsafe, or has an unexpected size | **Skip** and explain why in a CSV report |

**Preview first.** No files are changed until you explicitly choose to clean up and confirm the actions.

## Requirements

- **Python 3.10+**. No `pip install` needed.
- [Navidrome](https://www.navidrome.org/) and [qBittorrent](https://www.qbittorrent.org/) with its **Web UI** enabled.
- Run the script where both apps' APIs are reachable. **For quarantine, run it on the computer/server that stores the music files** and has permission to move them.

Docker is supported but **not required**.

### Advanced Usage (Manual Configuration)

If you prefer to configure everything manually instead of using the interactive setup, run:

```bash
python3 ~/navidrome_to_qbittorrent_quarantine2.py \
  --nav-url http://127.0.0.1:4533 \
  --qb-url http://127.0.0.1:8181 \
  --nav-root /music \
  --qb-root /DATA/Media/Music \
  --host-music-root /DATA/Media/Music \
  --quarantine-root /DATA/Media/.MusicQuarantine \
  --ignore-missing-metadata \
  --report ~/navidrome_cleanup_result.csv \
  --apply \
  --move-unmatched
```

> [!NOTE]
> Replace the example URLs and paths with your own configuration. This command changes matching torrent-file priorities and moves unmatched starred tracks to quarantine. Review a dry run before applying changes.

**Key options:**
- `--apply`: Set matching torrent files to *Do not download*.
- `--move-unmatched`: Move unmatched starred songs to quarantine.
- `--ignore-missing-metadata`: Ignore torrents without metadata, which may cause their files to be treated as unmatched.

## Quick start

**1. Download** `navidrome_to_qbittorrent_quarantine.py`, or clone this repository. Open a terminal in its folder.

**2. Set up once:**

```bash
python3 navidrome_to_qbittorrent_quarantine.py --setup
```

The setup assistant will explain the questions, **suggest paths and ports automatically for supported local Docker setups**, and save your answers. It does **not** save passwords. If Docker detection isn't possible, see [Finding your settings](#finding-your-settings) below.

**3. Preview your cleanup:**

```bash
python3 navidrome_to_qbittorrent_quarantine.py
```

Sign in when prompted, then review the generated `.csv` report. **Nothing changes yet.**

**4. Run both cleanup actions** once the preview looks right:

```bash
python3 navidrome_to_qbittorrent_quarantine.py --clean
```

Confirm with uppercase **`APPLY`** to change qBittorrent priorities and **`MOVE`** to quarantine unmatched tracks. The script will only ask for a confirmation when that type of action is needed.

> [!IMPORTANT]
> After your first API request, open **Navidrome → Settings → Players → MusicQbitPrio**, enable **Report Real Path**, then run the preview again. Without this setting, Navidrome may return virtual paths that don't match your torrents.

### Where do quarantined songs go?

The setup assistant suggests a separate folder **beside** your music folder, for example:

```text
/storage/
├── Music/               # Navidrome scans this
└── .MusicQuarantine/    # Moved songs, still recoverable
```

Quarantined files remain on disk. **To reclaim that storage space, inspect them and delete them yourself.** The script never permanently deletes tracks. Similarly, setting qBittorrent priority `0` does not guarantee that already-downloaded bytes disappear.

## Finding your settings

The setup assistant asks for the following information. Most users with both applications running in Docker on one server can just accept its suggestions.

| Question | Where to find it |
| --- | --- |
| **Navidrome URL** | Address used to open Navidrome, e.g. `http://127.0.0.1:4533` when running the script on the server |
| **qBittorrent URL** | **Tools → Options → Web UI**. Docker host ports can differ from the container's port. |
| **Navidrome music root** | Navidrome's configured music path; in Docker, look at the **container-side** mount (often `/music`) |
| **qBittorrent music root** | The **same music folder** as seen by qBittorrent; may differ from Navidrome's path |
| **Music folder on this computer** | The **real host directory** holding the files, e.g. `/DATA/Media/Music` |
| **Quarantine folder** | Any suitable sibling folder **outside** Navidrome's music directory, on the same filesystem |

**If Docker auto-detection fails**, check volume mounts with:

```bash
docker inspect navidrome qbittorrent \
  --format '{{.Name}}:{{range .Mounts}}{{printf "\n  %s -> %s" .Source .Destination}}{{end}}{{println}}'
```

Example: if Navidrome maps `/DATA/Media/Music → /music` and qBittorrent maps `/DATA → /DATA`, then the music roots are **`/music`** (Navidrome), **`/DATA/Media/Music`** (qBittorrent), and **`/DATA/Media/Music`** (the server). That's three names for the same physical directory, not three copies.

For **non-Docker** installations, both application roots may simply be the same filesystem directory. Paths must point to the **same actual files**.

Run `--setup` again whenever you move your library or change ports. Your settings are stored at `~/.config/navidrome-qbit-cleanup/config.json` (or under `$XDG_CONFIG_HOME`). **Passwords are not saved.**

## Options

| Command or flag | Action |
| --- | --- |
| `--setup` | Run the guided setup again |
| *(no flags)* | Preview without changes |
| `--clean` | Apply priority `0` **and** quarantine unmatched files |
| `--apply` | Change matching qBittorrent priorities only |
| `--move-unmatched` | Quarantine unmatched songs only |
| `--ignore-missing-metadata` | Opt into treating torrents with no metadata as unknown; their songs **may** be quarantined |
| `--protect-blocked-folders` | Instead, protect folders of unreadable torrents where possible |
| `--allow-whole-torrent` | Permit deselecting the last selected file in a torrent |
| `--config PATH` | Use a different settings file |
| `--report PATH` | Choose a report CSV location |

**Advanced users:** You can still pass `--nav-url`, `--qb-url`, `--nav-root`, `--qb-root`, `--host-music-root`, and `--quarantine-root` directly. CLI values override saved settings. Run `--help` for every available flag.

## Safety notes

- **Missing torrent metadata:** By default, uninspectable music torrents block quarantine. Opting into `--ignore-missing-metadata` deliberately accepts that a hearted song may belong to one of them.
- **Shared torrent pieces:** Other tracks can temporarily show incomplete if you physically remove data belonging to a torrent. qBittorrent may re-download boundary pieces. The script doesn't force rechecks or delete `.unwanted` folders.
- **Quarantine:** Files are moved without overwriting existing files. Symlinks, unsafe paths, recently modified files, file-size mismatches, and ambiguous ownership are skipped.
- **Security:** Use localhost, HTTPS, or a trusted tunnel for app APIs. Don't commit local config files, passwords, or your CSV reports to GitHub.

### Troubleshooting

| Problem | Try this |
| --- | --- |
| No `MusicQbitPrio` player | Run a preview once, then check Navidrome **Settings → Players** |
| All tracks `SKIP_PATH` | Enable **Report Real Path**, then preview again |
| Unexpected `NOT_FOUND` results | Check the Docker mounts and all three music root paths |
| Quarantine blocked by metadata | Wait for torrent metadata, protect those folders, or explicitly accept the `--ignore-missing-metadata` risk |
| Priority change `REFUSED` | qBittorrent may not accept changes to an already completed file |
| `MOVE_ERROR` | Check directory permissions, same-filesystem requirement, and any existing destination files |
