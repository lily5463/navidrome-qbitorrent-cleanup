#!/usr/bin/env python3
"""Manage hearted Navidrome tracks against qBittorrent torrent files.

The --ignore-missing-metadata option explicitly ignores
qBittorrent torrents stalled in metaDL/forcedMetaDL with NO available file list.
Their files cannot be matched and may accidentally be quarantined. Other API
failures or malformed lists still block quarantine rather than silently risk data.

Dry-run by default. --apply changes matched files to qBittorrent priority 0.
--move-unmatched can quarantine songs *not found in ANY torrent*, after
verifying the source on the server and requiring an explicit MOVE confirmation.
NEVER permanently deletes files or requests a torrent recheck.
Requires Python 3.10+; no third-party libraries. Run on the music-file server.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import http.cookiejar
import json
import os
import stat
import time
import re
import secrets
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

CLIENT = "MusicQbitPrio"


def base_url(value, label):
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError(f"{label} must be an HTTP(S) base URL without query or fragment")
    return value.rstrip("/")


def abs_path_parts(path):
    """Represent POSIX/Windows absolute paths without touching the local filesystem."""
    if not isinstance(path, str) or "\x00" in path:
        return None
    path = path.replace("\\", "/")
    if re.match(r"^[a-zA-Z]:/", path):
        volume, remainder, case_insensitive = path[:2].lower(), path[3:], True
    elif path.startswith("/") and not path.startswith("//"):
        volume, remainder, case_insensitive = "/", path[1:], False
    else:
        return None
    pieces = remainder.split("/")
    if any(p in (".", "..") for p in pieces):
        return None
    return volume, tuple(p for p in pieces if p), case_insensitive


def relative_path(full, root):
    actual, parent = abs_path_parts(full), abs_path_parts(root)
    if actual is None or parent is None or actual[0] != parent[0]:
        return None
    a, b = actual[1], parent[1]
    if len(a) <= len(b):
        return None
    ignore_case = parent[2] or actual[2]
    eq = (lambda x, y: x.casefold() == y.casefold()) if ignore_case else (lambda x, y: x == y)
    if not all(eq(x, y) for x, y in zip(a, b)):
        return None
    return a[len(b):]


def path_key(rel, insensitive):
    return tuple(s.casefold() for s in rel) if insensitive else tuple(rel)


def qb_path(save_path, relative_name):
    if not isinstance(relative_name, str) or not relative_name or "\x00" in relative_name:
        return None
    # qBittorrent files.name MUST be relative to the torrent's save_path.
    name = relative_name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[a-zA-Z]:", name):
        return None
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if abs_path_parts(save_path) is None:
        return None
    return save_path.rstrip("/\\") + "/" + name


def subsonic_call(server, user, password, method):
    salt = secrets.token_hex(12)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    params = {"u": user, "t": token, "s": salt, "v": "1.16.1", "c": CLIENT, "f": "json"}
    req = Request(f"{server}/rest/{method}.view?{urlencode(params)}", headers={"User-Agent": f"{CLIENT}/1.0"})
    with urlopen(req, timeout=35) as response:
        data = json.load(response).get("subsonic-response", {})
    if data.get("status") != "ok":
        raise RuntimeError("Navidrome API error: " + str(data.get("error", data)))
    return data


class QbClient:
    def __init__(self, url, user, password):
        self.url = url
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        response = self.request("auth/login", {"username": user, "password": password}, json_result=False)
        if response.strip() != "Ok.":
            raise RuntimeError(f"qBittorrent login unsuccessful (server said {response[:70]!r})")

    def request(self, method, values=None, json_result=True):
        url = f"{self.url}/api/v2/{method}"
        if method == "auth/login" or method == "torrents/filePrio":
            encoded = urlencode(values or {}).encode("utf-8")
            req = Request(url, data=encoded, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            if values:
                url += "?" + urlencode(values)
            req = Request(url)
        req.add_header("User-Agent", f"{CLIENT}/1.0")
        req.add_header("Referer", self.url + "/")
        with self.opener.open(req, timeout=35) as resp:
            data = resp.read().decode("utf-8-sig")
        return json.loads(data) if json_result else data


def plan_changes(songs, torrents, file_lookup, nav_root, qb_root, only_category=None):
    insensitive = abs_path_parts(qb_root)[2]
    index = defaultdict(list)
    # Index by full path relative to roots. Category by itself is not a unique identifier.
    for t in torrents:
        if only_category is not None and t.get("category", "") != only_category:
            continue
        save_path, thash = t.get("save_path"), t.get("hash")
        if not isinstance(save_path, str) or not isinstance(thash, str):
            continue
        files = file_lookup.get(thash, [])
        for position, file in enumerate(files):
            full = qb_path(save_path, file.get("name", ""))
            rel = relative_path(full, qb_root) if full else None
            if rel is None:
                continue
            try:
                size, idx = int(file["size"]), int(file.get("index", position))
            except (TypeError, ValueError, KeyError):
                continue
            if size < 0 or idx < 0:
                continue
            index[path_key(rel, insensitive)].append({"hash": thash, "index": idx,
                "size": size, "priority": file.get("priority"), "name": file.get("name"),
                "torrent_name": t.get("name", ""), "category": t.get("category", ""),
                "save_path": save_path, "file_count": len(files)})

    rows = []
    for song in songs:
        row = {"title": song.get("title", ""), "artist": song.get("artist", ""),
               "navidrome_path": song.get("path", ""), "navidrome_size": song.get("size", ""),
               "category": "", "torrent": "", "torrent_hash": "", "file_index": "",
               "qb_file": "", "qb_size": "", "old_priority": "", "status": "", "detail": "", "_save_path": ""}
        rel = relative_path(song.get("path"), nav_root)
        if rel is None:
            row.update(status="SKIP_PATH", detail="Not a real absolute path within Navidrome root; enable Report Real Path")
            rows.append(row)
            continue
        matches = index.get(path_key(rel, insensitive), [])
        if not matches:
            row.update(status="NOT_FOUND", detail="No qBittorrent file at this mapped relative path")
            rows.append(row)
            continue
        # Even identical file names in different torrents are ambiguous on purpose.
        if len(matches) != 1:
            row.update(status="AMBIGUOUS", detail=f"{len(matches)} qBittorrent entries have this path; skipped")
            rows.append(row)
            continue
        found = matches[0]
        row.update(category=found["category"], torrent=found["torrent_name"],
                   torrent_hash=found["hash"], file_index=found["index"], qb_file=found["name"],
                   qb_size=found["size"], old_priority=found["priority"], _save_path=found["save_path"])
        try:
            song_size = int(song["size"])
        except (TypeError, ValueError, KeyError):
            row.update(status="SKIP_SIZE", detail="Navidrome did not return a usable size")
        else:
            if found["size"] != song_size:
                row.update(status="SKIP_SIZE", detail="Navidrome and qBittorrent file sizes disagree")
            elif found["priority"] == 0:
                row.update(status="ALREADY_SKIPPED", detail="Priority already equals 0")
            elif found["file_count"] == 1:
                row.update(status="WHOLE_TORRENT", detail="Single-file torrent; entire torrent would be deselected")
            else:
                row.update(status="READY", detail="Exact path and byte size match")
        rows.append(row)
    return rows



def ensure_quarantine_config(host_music_root: Path, quarantine_root: Path):
    """Quarantine must be an actual sibling/outside destination on this machine."""
    host = host_music_root.expanduser().resolve(strict=True)
    dest = quarantine_root.expanduser().resolve(strict=False)
    if not host.is_dir():
        raise RuntimeError(f"Host music directory is not a folder: {host}")
    if dest == host or dest.is_relative_to(host) or host.is_relative_to(dest):
        raise RuntimeError("Quarantine must be OUTSIDE (and not a parent of) the music library")
    if dest == Path('/'):
        raise RuntimeError("Quarantine cannot be the filesystem root")
    if quarantine_root.expanduser().is_symlink() or (dest.exists() and not dest.is_dir()):
        raise RuntimeError("Quarantine path must be a directory, never a symlink or file")
    # Refuse to put the quarantine inside a physical directory that Navidrome sees.
    if host_music_root.expanduser().is_symlink():
        raise RuntimeError("The supplied host music root cannot be a symlink")
    return host, dest


def assess_quarantine(rows, nav_root: str, host_music_root: Path, quarantine_root: Path, min_age_seconds: int):
    """Turn only global NOT_FOUND results into candidates with on-disk checks."""
    candidates = [r for r in rows if r['status'] == 'NOT_FOUND']
    path_counts = Counter(str(r['navidrome_path']) for r in candidates)
    for row in candidates:
        rel = relative_path(row['navidrome_path'], nav_root)
        if rel is None or not rel:
            row.update(status='SKIP_PATH', detail='Real absolute Navidrome file path required')
            continue
        # Construct the server's physical path from the verified Navidrome path.
        src = host_music_root.joinpath(*rel)
        dst = quarantine_root.joinpath(*rel)
        row['_source_file'] = str(src)
        row['_target_file'] = str(dst)
        if path_counts[row['navidrome_path']] != 1:
            row.update(status='SKIP_DUPLICATE', detail='Repeated Navidrome path; no move')
            continue
        try:
            current = host_music_root
            for part in rel:
                current = current / part
                if current.is_symlink():
                    raise ValueError('A source file or directory is a symlink')
            source_stat = src.stat()
        except (OSError, ValueError) as exc:
            row.update(status='SKIP_MISSING', detail=f'Cannot read source safely: {exc}')
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            row.update(status='SKIP_NONFILE', detail='Source is not a regular file')
            continue
        try:
            expected_bytes = int(row['navidrome_size'])
        except (TypeError, ValueError):
            row.update(status='SKIP_SIZE', detail='Navidrome did not report a usable file size')
            continue
        if expected_bytes < 1 or source_stat.st_size != expected_bytes:
            row.update(status='SKIP_DISK_SIZE', detail='Physical file size disagrees with Navidrome')
            continue
        if time.time() - source_stat.st_mtime < min_age_seconds:
            row.update(status='SKIP_RECENT', detail='File was modified recently; likely still in use')
            continue
        if dst.exists() or dst.is_symlink():
            row.update(status='SKIP_EXISTS', detail='Target already exists; never overwrite')
            continue
        row['_source_stat'] = (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
        row.update(status='MOVE_READY', detail='No match in readable torrents; physical source checked; quarantine candidate')


def quarantine_file(row, host_music_root: Path, quarantine_root: Path, min_age_seconds: int):
    """No-clobber, same-filesystem hardlink + unlink; preserves link if unlink fails."""
    src, dst = Path(row['_source_file']), Path(row['_target_file'])
    rel = src.relative_to(host_music_root)
    if len(rel.parts) == 0 or dst != quarantine_root.joinpath(*rel.parts):
        raise RuntimeError('Unsafe source/destination pair')
    # Check all source components against symlink attacks.
    current = host_music_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('Source path now contains a symlink')
    pre = src.stat()
    signature = (pre.st_dev, pre.st_ino, pre.st_size, pre.st_mtime_ns)
    if not stat.S_ISREG(pre.st_mode) or signature != row.get('_source_stat'):
        raise RuntimeError('Source file changed since preview')
    if time.time() - pre.st_mtime < min_age_seconds:
        raise RuntimeError('File was modified too recently')
    if dst.exists() or dst.is_symlink():
        raise RuntimeError('Destination already exists')
    dst.parent.mkdir(parents=True, exist_ok=True)
    if quarantine_root.stat().st_dev != pre.st_dev:
        raise RuntimeError('Quarantine is on a different filesystem; refusing non-atomic copy')
    current = quarantine_root
    for part in rel.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError('Destination parent contains a symlink')
    # os.link fails rather than overwriting a preexisting destination.
    os.link(src, dst, follow_symlinks=False)
    try:
        linked = dst.stat()
        latest = src.stat()
        if (linked.st_dev, linked.st_ino) != (pre.st_dev, pre.st_ino) or (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns) != signature:
            raise RuntimeError('Source changed during move; quarantined hardlink preserved for inspection')
        os.unlink(src)
    except Exception:
        # If unlink failed, the source was not removed. Destination is left
        # untouched for manual recovery; never destroy files during error handling.
        raise


def torrent_directory_overlaps_music(save_path, qb_root):
    """Conservative: unknown path means POSSIBLY inside the music tree.

    Torrent file names are relative to torrent save_path. If that directory
    is neither inside nor an ancestor of qb_root, it cannot name a file in
    qb_root via a normal relative torrent file path.
    """
    save, root = abs_path_parts(save_path), abs_path_parts(qb_root)
    if save is None or root is None:
        return True
    if save[0] != root[0]:
        return False
    save_parts, root_parts = save[1], root[1]
    ignore_case = save[2] or root[2]
    common = min(len(save_parts), len(root_parts))
    for a, b in zip(save_parts[:common], root_parts[:common]):
        if (a.casefold() if ignore_case else a) != (b.casefold() if ignore_case else b):
            return False
    return True


def read_relevant_torrent_files(qb, torrents, qb_root, phase, protect_blocked_folders=False,
                                ignore_missing_metadata=False):
    """Index readable music torrents; optionally ignore only metadata-pending ones.

    Unavailable metadata means NOT KNOWN, never demonstrably NOT OWNED.
    --ignore-missing-metadata opts into treating that uncertainty as unmatched.
    API errors, malformed files, and missing hashes are not excused by this flag.
    """
    lookup = {}
    unrelated = 0
    blockers = []
    ignored_metadata = []
    checked = 0
    for index, torrent in enumerate(torrents, 1):
        torrent_hash = torrent.get('hash')
        path = torrent.get('save_path')
        if not torrent_directory_overlaps_music(path, qb_root):
            unrelated += 1
            continue
        if not isinstance(torrent_hash, str) or not torrent_hash:
            blockers.append((torrent, 'Missing torrent hash'))
            continue
        try:
            files = qb.request('torrents/files', {'hash': torrent_hash})
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            blockers.append((torrent, f'API error: {str(exc)[:100]}'))
            continue
        checked += 1
        if not isinstance(files, list) or not files:
            # A valid empty list while metadata is still pending is predictable.
            # User may explicitly choose to treat this torrent as absent for
            # matching purposes. Other empty lists remain blocking anomalies.
            state = torrent.get('state')
            if ignore_missing_metadata and files == [] and state in ('metaDL', 'forcedMetaDL'):
                ignored_metadata.append(torrent)
            else:
                blockers.append((torrent, 'File list unavailable or empty'))
            continue
        invalid = next((f for f in files if not isinstance(f, dict)
                        or qb_path(path, f.get('name', '')) is None
                        or not isinstance(f.get('size'), int)
                        or not isinstance(f.get('priority'), int)), None)
        if invalid is not None:
            blockers.append((torrent, 'Malformed file-list entry'))
            continue
        lookup[torrent_hash] = files
        if index % 100 == 0:
            print(f'  Scanned {index}/{len(torrents)} torrents')
    print(f'  {phase}: {len(lookup)} readable music-related torrents, '
          f'{unrelated} unrelated torrents skipped, '
          f'{len(ignored_metadata)} metadata-pending torrents deliberately ignored, '
          f'{len(blockers)} blocked')
    if ignored_metadata:
        print('  WARNING: these metadata-pending torrents cannot be checked against hearted songs:')
        for torrent in ignored_metadata[:20]:
            print(f"    [{torrent.get('state', '?')}] {torrent.get('name', '(unnamed)')!r} "
                  f"save_path={torrent.get('save_path', '(missing)')!r}")
        if len(ignored_metadata) > 20:
            print(f'    ... plus {len(ignored_metadata) - 20} more')
        print('  Files within these locations MAY be treated as unmatched and moved. '
              'This is an explicit user-selected risk.')
    guarded_folders = []
    if blockers:
        print('  qBittorrent torrents needing attention (first 12):', file=sys.stderr)
        for torrent, reason in blockers[:12]:
            print(f"    [{torrent.get('state', '?')}] {torrent.get('name', '(unnamed)')!r} "
                  f"save_path={torrent.get('save_path', '(missing)')!r}: {reason}",
                  file=sys.stderr)
        if len(blockers) > 12:
            print(f'    ... and {len(blockers) - 12} more', file=sys.stderr)
        if not protect_blocked_folders:
            raise RuntimeError('Quarantine safely blocked: some music-related torrents lack file lists. '
                               'Use --protect-blocked-folders to exclude their entire folders, '
                               'or resolve the torrents and retry.')

        root = abs_path_parts(qb_root)
        for torrent, reason in blockers:
            save = abs_path_parts(torrent.get('save_path'))
            # Only a known descendant of the root can be localized to a folder.
            # If a blocker could contain ANY path in the music root, we MUST fail.
            if (save is None or root is None or save[0] != root[0]
                    or len(save[1]) <= len(root[1])):
                raise RuntimeError('An unreadable torrent could own files anywhere in the music root; '
                                   'cannot safely protect only a subfolder. Nothing moved.')
            insensitive = save[2] or root[2]
            same_root = all(a.casefold() == b.casefold() if insensitive else a == b
                            for a, b in zip(save[1][:len(root[1])], root[1]))
            if not same_root:
                raise RuntimeError('Unreadable torrent path cannot be safely located within music root')
            guarded_folders.append(tuple(save[1][len(root[1]):]))
        guarded_folders = sorted(set(guarded_folders))
        print(f'  SAFETY: protecting {len(guarded_folders)} complete music subfolder(s) from ALL changes:')
        for parts in guarded_folders[:20]:
            print('   /' + '/'.join(parts))
    return lookup, guarded_folders


def guard_unknown_torrent_folders(rows, nav_root, guarded_folders):
    """Exclude songs in any subtree that might belong to an unreadable torrent."""
    if not guarded_folders:
        return
    for row in rows:
        rel = relative_path(row.get('navidrome_path'), nav_root)
        if rel is None:
            continue
        for prefix in guarded_folders:
            if len(rel) >= len(prefix) and rel[:len(prefix)] == prefix:
                row.update(status='SKIP_UNVERIFIED_TORRENT',
                           detail='A torrent with unavailable metadata could own files in this folder: /' + '/'.join(prefix))
                break


def refresh_torrent_index(qb, originals, nav_root, qb_root, current_songs,
                          protect_blocked_folders=False, ignore_missing_metadata=False):
    """Rescan all torrent contents just before moving to catch newly added ownership."""
    now = qb.request('torrents/info', {'filter': 'all'})
    if not isinstance(now, list) or not now:
        raise RuntimeError('Cannot safely quarantine: qBittorrent reports no torrents')
    old = {t['hash']: t.get('save_path') for t in originals}
    new = {t['hash']: t.get('save_path') for t in now}
    if any(new.get(key) != value for key, value in old.items()):
        raise RuntimeError('A torrent disappeared or its save path changed since preview; rerun dry-run')
    lookup, blocked = read_relevant_torrent_files(
        qb, now, qb_root, 'Pre-move verification', protect_blocked_folders,
        ignore_missing_metadata)
    rows = plan_changes(current_songs, now, lookup, nav_root, qb_root)
    guard_unknown_torrent_folders(rows, nav_root, blocked)
    return rows, now, lookup


def csv_safe(value):
    """Stop spreadsheet applications interpreting untrusted titles as formulas."""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, str) and value.startswith(("\t", "\r", "\n")):
        return "'" + value
    return value


def write_report(rows, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    names = ["status", "detail", "artist", "title", "navidrome_path", "navidrome_size",
             "category", "torrent", "torrent_hash", "file_index", "qb_file", "qb_size", "old_priority", "quarantine_target"]
    with target.open("w", newline="", encoding="utf-8-sig") as output:
        out = csv.DictWriter(output, fieldnames=names, extrasaction="ignore")
        out.writeheader()
        for row in rows:
            report_row = {**row, "quarantine_target": row.get("_target_file", "")}
            out.writerow({key: csv_safe(value) for key, value in report_row.items()})



def default_config_path():
    """Keep personal machine paths out of a cloned Git repository."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return config_home / "navidrome-qbit-cleanup" / "config.json"


def load_config(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Configuration must be a JSON object: {path}")
    allowed = {"nav_url", "qb_url", "nav_root", "qb_root", "host_music_root",
               "quarantine_root", "nav_user", "qb_user", "ignore_missing_metadata",
               "protect_blocked_folders"}
    unknown = set(data) - allowed
    if unknown:
        raise RuntimeError(f"Unknown configuration settings in {path}: {', '.join(sorted(unknown))}")
    for key, val in data.items():
        if key in ("ignore_missing_metadata", "protect_blocked_folders"):
            if not isinstance(val, bool):
                raise RuntimeError(f"Invalid boolean value for {key} in {path}")
        elif not isinstance(val, str):
            raise RuntimeError(f"Invalid text value for {key} in {path}")
    return data


def save_config(path, config):
    """Save non-secret settings. Passwords are always prompted during actual runs."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Refusing to overwrite non-regular configuration: {path}")
    if path.exists():
        os.chmod(path, 0o600)
    # Atomic replacement: don't leave a half-written config on crash.
    import tempfile
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            os.chmod(temp_path, 0o600)
            json.dump(config, file, indent=2, ensure_ascii=False)
            file.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def docker_defaults():
    """Best-effort detection on a local Docker host; return suggestions only."""
    try:
        proc = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True,
                              timeout=6, check=True)
        ids = [x.strip() for x in proc.stdout.splitlines() if x.strip()]
        if not ids:
            return {}
        proc = subprocess.run(["docker", "inspect", *ids], capture_output=True,
                              text=True, timeout=12, check=True)
        containers = json.loads(proc.stdout)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, OSError):
        return {}
    found = {}
    for item in containers:
        name = str(item.get("Name", "")).lower()
        image_name = str(item.get("Config", {}).get("Image", "")).lower()
        for key, hints in (("nav", ("navidrome",)),
                           ("qb", ("qbittorrent", "qbittorrent-nox"))):
            if key not in found and any(h in name or h in image_name for h in hints):
                found[key] = item
    if not ("nav" in found and "qb" in found):
        return {}
    nav, qb = found["nav"], found["qb"]
    mounts = nav.get("Mounts", [])
    possible = [m for m in mounts if m.get("Type") == "bind" and
                "music" in m.get("Destination", "").lower()]
    if len(possible) != 1:
        return {}
    music_mount = possible[0]
    host = Path(music_mount["Source"])
    candidates = []
    for m in qb.get("Mounts", []):
        if m.get("Type") != "bind":
            continue
        src = Path(m.get("Source", ""))
        try:
            tail = host.relative_to(src)
        except ValueError:
            continue
        candidates.append((len(src.parts), str(PurePosixPath(m["Destination"]).joinpath(*tail.parts))))
    if not candidates:
        return {}
    qb_root = max(candidates)[1]

    def port(container, internal, fallback):
        bindings = container.get("NetworkSettings", {}).get("Ports", {}).get(f"{internal}/tcp") or []
        for b in bindings:
            port_value = b.get("HostPort", "")
            if str(port_value).isdigit():
                return int(port_value)
        return fallback

    return {
        "nav_url": f"http://127.0.0.1:{port(nav, 4533, 4533)}",
        "qb_url": f"http://127.0.0.1:{port(qb, 8080, 8080)}",
        "nav_root": music_mount["Destination"],
        "qb_root": qb_root,
        "host_music_root": str(host),
        "quarantine_root": str(host.parent / ".MusicQuarantine"),
    }


def ask_setting(label, default=None, explanation=None):
    if explanation:
        print("  " + explanation)
    while True:
        hint = f" [{default}]" if default not in (None, "") else ""
        try:
            result = input(f"{label}{hint}: ").strip()
        except EOFError as exc:
            raise RuntimeError("Setup requires an interactive terminal") from exc
        if result:
            return result
        if default not in (None, ""):
            return str(default)
        print("  This value is required.")


def interactive_setup(path):
    if not sys.stdin.isatty():
        raise RuntimeError("Run --setup from an interactive terminal")
    print("\nNAVIDROME × QBITTORRENT CLEANUP: FIRST-TIME SETUP")
    print("This saves folder mappings and URLs, NOT passwords.")
    print("For moving unmatched files, run this script on the machine holding your music.\n")
    old = load_config(path)
    auto = docker_defaults()
    if auto:
        print("Detected local Docker containers and a shared music directory.")
        print(f"  Music files on this machine: {auto['host_music_root']}")
        print(f"  Seen by Navidrome:          {auto['nav_root']}")
        print(f"  Seen by qBittorrent:        {auto['qb_root']}")
        print("Press Enter to accept each suggestion, or type a different value.\n")
    else:
        print("Couldn't determine a shared Docker music mount automatically.")
        print("Use each app's music folder path. In Docker, inspect its volume mounts.")
        print("See the README's manual setup section for examples.\n")

    choices = (
        ("nav_url", "Navidrome URL", "Address reachable from THIS machine (often http://127.0.0.1:4533)"),
        ("qb_url", "qBittorrent Web UI URL", "Address reachable from THIS machine (commonly http://127.0.0.1:8080)"),
        ("nav_root", "Navidrome music root", "Music folder AS NAVIDROME SEES IT, e.g. /music"),
        ("qb_root", "qBittorrent music root", "Same physical folder AS QBITTORRENT SEES IT"),
        ("host_music_root", "Music folder on this machine", "Where the files really live; required to move songs"),
        ("quarantine_root", "Quarantine folder", "Must be outside the music library, ideally beside it"),
    )
    config = {}
    for key, label, helptext in choices:
        default = old.get(key, auto.get(key))
        if key == "quarantine_root" and not default and config.get("host_music_root"):
            default = str(Path(config["host_music_root"]).parent / ".MusicQuarantine")
        config[key] = ask_setting(label, default, helptext)
    config["nav_user"] = ask_setting("Navidrome username", old.get("nav_user"),
                                      "Same username you use to sign into Navidrome")
    config["qb_user"] = ask_setting("qBittorrent username", old.get("qb_user"),
                                     "Same username you use for qBittorrent Web UI")
    response = ask_setting("Ignore torrents stuck waiting for metadata? (y/n)",
                           "y" if old.get("ignore_missing_metadata") else "n",
                           "YES may quarantine hearted files owned by those unknown torrents")
    if response.lower() not in ("y", "yes", "n", "no"):
        raise RuntimeError("Answer y or n for missing metadata; no settings saved")
    config["ignore_missing_metadata"] = response.lower() in ("y", "yes")
    config["protect_blocked_folders"] = bool(old.get("protect_blocked_folders", False))
    try:
        base_url(config["nav_url"], "Navidrome URL")
        base_url(config["qb_url"], "qBittorrent URL")
        if abs_path_parts(config["nav_root"]) is None or abs_path_parts(config["qb_root"]) is None:
            raise ValueError("Application music roots must be absolute paths")
        ensure_quarantine_config(Path(config["host_music_root"]), Path(config["quarantine_root"]))
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Setup not saved: {exc}") from exc
    print("\nSave these settings? No tracks will be changed.")
    if input("Type SAVE to continue: ").strip() != "SAVE":
        print("Cancelled. Configuration not saved.")
        return 0
    save_config(path, config)
    print(f"\nConfiguration saved to: {path}")
    print("Passwords are not stored. They will be requested when you run a scan.")
    print("First run this dry run: python3 navidrome_to_qbittorrent_quarantine.py")
    print("In Navidrome: Settings → Players → MusicQbitPrio → Report Real Path (then retry).")
    print("After reviewing the CSV: python3 navidrome_to_qbittorrent_quarantine.py --clean")
    return 0

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--nav-url", help="Navidrome base URL")
    p.add_argument("--qb-url", help="qBittorrent WebUI base URL, often http://127.0.0.1:8080")
    p.add_argument("--nav-root", help="Navidrome path prefix, e.g. /music")
    p.add_argument("--qb-root", help="Equivalent qBittorrent path prefix, e.g. /downloads/music")
    p.add_argument("--nav-user", help="Navidrome username; prompted if omitted")
    p.add_argument("--qb-user", help="qBittorrent username; prompted if omitted")
    p.add_argument("--category", help="Optional exact qBittorrent category to restrict search")
    p.add_argument("--report", type=Path, help="Where to save the CSV report")
    p.add_argument("--apply", action="store_true", help="Request priority=0 after explicit confirmation")
    p.add_argument("--host-music-root", type=Path, help="Real local filesystem music root on this machine")
    p.add_argument("--quarantine-root", type=Path, help="Target directory OUTSIDE Navidrome's music root")
    p.add_argument("--move-unmatched", action="store_true", help="Quarantine globally unmatched songs after explicit MOVE confirmation")
    p.add_argument("--protect-blocked-folders", action="store_true", default=None, help="Exclude entire folders of unreadable music torrents; fail if their scope cannot be contained")
    p.add_argument("--ignore-missing-metadata", action="store_true", default=None, help="Treat metaDL/forcedMetaDL torrents with empty file lists as unknown-and-unmatched for quarantine; MAY MOVE THEIR FILES; other API problems still block")
    p.add_argument("--min-age-minutes", type=int, default=10, help="Skip files modified in last N minutes (default 10)")
    p.add_argument("--max-moves", type=int, default=200, help="Maximum quarantine moves in one run (default 200)")
    p.add_argument("--allow-whole-torrent", action="store_true", help="Also request priority=0 for single-file torrents / all selected files in a torrent")
    p.add_argument("--max-changes", type=int, default=2000, help="Safety limit on priority changes per run (default 2000)")
    p.add_argument("--config", type=Path, default=default_config_path(), help="Configuration file (default: ~/.config/navidrome-qbit-cleanup/config.json)")
    p.add_argument("--setup", action="store_true", help="Guided first-time setup; detects local Docker paths when possible")
    p.add_argument("--clean", action="store_true", help="Apply priority=0 and quarantine unmatched songs, after confirmation")
    p.add_argument("--version", action="version", version="%(prog)s 1.1.0")
    args = p.parse_args()
    if args.setup:
        return interactive_setup(args.config.expanduser())
    config_path = args.config.expanduser()
    settings = load_config(config_path)
    required = ("nav_url", "qb_url", "nav_root", "qb_root")
    if not settings and any(getattr(args, key) is None for key in required):
        if sys.stdin.isatty():
            print("No saved setup found. Starting the guided setup.\n")
            return interactive_setup(config_path)
        p.error("Missing connection settings. Run --setup interactively or provide all URL/root options.")
    for key in ("nav_url", "qb_url", "nav_root", "qb_root", "host_music_root",
                "quarantine_root", "nav_user", "qb_user", "ignore_missing_metadata",
                "protect_blocked_folders"):
        if getattr(args, key) is None and key in settings:
            value = settings[key]
            if key in ("host_music_root", "quarantine_root"):
                value = Path(value)
            setattr(args, key, value)
    if args.clean:
        args.apply = True
        args.move_unmatched = True
    if any(getattr(args, key) is None for key in required):
        p.error("Connection settings missing; run --setup to finish configuration")
    if args.max_changes < 1 or args.max_moves < 1 or args.min_age_minutes < 0:
        p.error("--max-changes and --max-moves must be positive; --min-age-minutes nonnegative")
    quarantine_requested = args.quarantine_root is not None or args.host_music_root is not None or args.move_unmatched
    if quarantine_requested and (args.quarantine_root is None or args.host_music_root is None):
        p.error("Quarantine needs BOTH --host-music-root and --quarantine-root")
    if quarantine_requested and args.category is not None:
        p.error("Cannot quarantine with --category: filtered NOT_FOUND does not mean globally unmatched. Remove --category.")
    if args.ignore_missing_metadata and not quarantine_requested:
        p.error("--ignore-missing-metadata has no effect without quarantine; provide both quarantine paths")
    if quarantine_requested:
        try:
            host_music_root, quarantine_root = ensure_quarantine_config(args.host_music_root, args.quarantine_root)
        except (OSError, RuntimeError) as exc:
            p.error(str(exc))
    try:
        nav_url, qb_url = base_url(args.nav_url, "--nav-url"), base_url(args.qb_url, "--qb-url")
    except ValueError as e:
        p.error(str(e))
    if abs_path_parts(args.nav_root) is None or abs_path_parts(args.qb_root) is None:
        p.error("--nav-root and --qb-root must be ABSOLUTE POSIX or Windows filesystem paths")
    for name, url in (("Navidrome", nav_url), ("qBittorrent", qb_url)):
        parsed = urlsplit(url)
        if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            print(f"WARNING: {name} uses unencrypted HTTP; only use trusted local networks or HTTPS.", file=sys.stderr)

    nav_user = args.nav_user or input("Navidrome username: ").strip()
    nav_pass = os.getenv("NAVIDROME_PASSWORD") or getpass.getpass("Navidrome password: ")
    qb_user = args.qb_user or input("qBittorrent WebUI username: ").strip()
    qb_pass = os.getenv("QBITTORRENT_PASSWORD") or getpass.getpass("qBittorrent WebUI password: ")
    if not all((nav_user, nav_pass, qb_user, qb_pass)):
        p.error("Both applications require nonempty credentials")

    starred = subsonic_call(nav_url, nav_user, nav_pass, "getStarred2").get("starred2", {}).get("song", []) or []
    if not isinstance(starred, list):
        raise RuntimeError("Unexpected Navidrome song-list response")
    # If the same song is returned twice, only request one change.
    songs = list({str(song.get("id", i)): song for i, song in enumerate(starred)}.values())
    qb = QbClient(qb_url, qb_user, qb_pass)
    torrents = qb.request("torrents/info", {"filter": "all"})
    if not isinstance(torrents, list):
        raise RuntimeError("Unexpected qBittorrent torrent-list response")
    if args.category is not None:
        torrents = [t for t in torrents if t.get("category", "") == args.category]

    print(f"Reading {len(torrents)} torrents and {len(songs)} hearted tracks...")
    if quarantine_requested:
        file_lookup, blocked = read_relevant_torrent_files(
            qb, torrents, args.qb_root, 'Initial scan', args.protect_blocked_folders,
            args.ignore_missing_metadata)
    else:
        file_lookup = {}
        for i, t in enumerate(torrents, 1):
            if isinstance(t.get('hash'), str):
                file_lookup[t['hash']] = qb.request('torrents/files', {'hash': t['hash']})
            if i % 100 == 0:
                print(f'  Scanned {i}/{len(torrents)} torrents')

    rows = plan_changes(songs, torrents, file_lookup, args.nav_root, args.qb_root, args.category)
    if quarantine_requested:
        guard_unknown_torrent_folders(rows, args.nav_root, blocked)
    if quarantine_requested:
        if not torrents:
            raise RuntimeError("No torrents loaded; refusing to interpret every hearted song as unmatched")
        # A healthy cleanup may have ZERO starred tracks left in the registered
        # torrents: previously matched tracks can disappear from Navidrome after
        # qBittorrent relocates them to .unwanted. Do not require a positive match
        # amongst the CURRENT starred tracks; instead independently verify that
        # the qBittorrent root is mapped to ACTUAL torrent-file paths.
        indexed_qb_files = sum(
            1
            for t in torrents
            if t.get('hash') in file_lookup
            for f in file_lookup[t['hash']]
            if (full := qb_path(t.get('save_path'), f.get('name'))) is not None
            and relative_path(full, args.qb_root) is not None
        )
        if not indexed_qb_files:
            raise RuntimeError(
                "No qBittorrent file paths fall under --qb-root; refusing quarantine. "
                "Check Docker mount mappings and the qBittorrent save paths."
            )
        nav_paths = sum(1 for song in songs if relative_path(song.get('path'), args.nav_root) is not None)
        if songs and not nav_paths:
            raise RuntimeError(
                "None of the Navidrome starred paths fall under --nav-root; refusing quarantine. "
                "Verify Settings > Players > Report Real Path and your Docker mapping."
            )
        print(f'  Root mapping check: {indexed_qb_files} torrent-file paths within --qb-root; '
              f'{nav_paths}/{len(songs)} Navidrome paths within --nav-root')
        if not any(r['status'] in ('READY', 'ALREADY_SKIPPED', 'WHOLE_TORRENT', 'SKIP_SIZE') for r in rows):
            print('  No CURRENT hearted tracks match qBittorrent. This can be normal after '
                  'previous priority-0 changes; checking unmatched files individually.')
        assess_quarantine(rows, args.nav_root, host_music_root, quarantine_root, args.min_age_minutes * 60)
    if args.allow_whole_torrent:
        for row in rows:
            if row["status"] == "WHOLE_TORRENT":
                row["status"], row["detail"] = "READY", "Entire single-file torrent selected with --allow-whole-torrent"

    # Skip if ALL files of any torrent would be priority 0; by default, preserve its seeding.
    by_hash = defaultdict(list)
    for row in rows:
        if row["status"] == "READY":
            by_hash[row["torrent_hash"]].append(row)
    for t in torrents:
        candidates = by_hash.get(t.get("hash"), [])
        if not candidates:
            continue
        files = file_lookup[t["hash"]]
        selected = [f for f in files if f.get("priority", 0) != 0]
        changed = {int(r["file_index"]) for r in candidates}
        if len(changed) >= len(selected) and not args.allow_whole_torrent:
            for row in candidates:
                row["status"] = "WHOLE_TORRENT"
                row["detail"] = "Would deselect every remaining file; add --allow-whole-torrent to permit"

    counts = Counter(r["status"] for r in rows)
    print("\nStatus summary:")
    for status, count in counts.most_common():
        print(f"  {status}: {count}")
    print("\nPreview (first 25):")
    for row in rows[:25]:
        print(f"  {row['status']:<18} {row['artist']} - {row['title']}")
        if row["status"] != "READY":
            print(f"      {row['detail']}")

    ready = [row for row in rows if row["status"] == "READY"]
    if args.apply and ready:
        if len(ready) > args.max_changes:
            raise RuntimeError(f"Refusing {len(ready)} changes (limit={args.max_changes})")
        print(f"\nWill request Do not download for {len(ready)} EXACTLY matched files.")
        print("No files will be deleted, moved, or rechecked. Some completed torrents may reject changes.")
        if input("Type APPLY to continue: ").strip() != "APPLY":
            print("Cancelled. No changes made.")
        else:
            by_hash = defaultdict(list)
            for row in ready:
                by_hash[row["torrent_hash"]].append(row)
            insensitive = abs_path_parts(args.qb_root)[2]
            for torrent_hash, group in by_hash.items():
                try:
                    latest_torrents = qb.request("torrents/info", {"hashes": torrent_hash})
                    latest = next((t for t in latest_torrents if t.get("hash") == torrent_hash), None)
                    if latest is None:
                        raise RuntimeError("Torrent disappeared")
                    if latest.get("save_path") != group[0]["_save_path"]:
                        raise RuntimeError("Torrent save path changed")
                    files_now = qb.request("torrents/files", {"hash": torrent_hash})
                    if not isinstance(files_now, list) or not files_now:
                        raise RuntimeError("Torrent file list unavailable at apply time")
                    # Index once; don't scan the whole torrent for each candidate.
                    current_by_index = {int(f.get("index", i)): f
                                        for i, f in enumerate(files_now)}
                    pending = []
                    for row in group:
                        match = current_by_index.get(int(row["file_index"]))
                        if (match is None or match.get("name") != row["qb_file"] or
                                int(match.get("size", -1)) != int(row["qb_size"])):
                            row.update(status="SKIP_CHANGED", detail="Torrent file changed since preview")
                        elif match.get("priority") == 0:
                            row.update(status="ALREADY_SKIPPED", detail="Already priority 0 at apply time")
                        else:
                            full = qb_path(latest.get("save_path"), match.get("name", ""))
                            rel = relative_path(full, args.qb_root) if full else None
                            expected = relative_path(row["navidrome_path"], args.nav_root)
                            if rel is None or expected is None or path_key(rel, insensitive) != path_key(expected, insensitive):
                                row.update(status="SKIP_CHANGED", detail="Full torrent file path changed since preview")
                            else:
                                pending.append(row)
                    if pending:
                        # Recheck the whole-torrent protection just before sending the API call.
                        # Other applications/users could have changed selections since preview.
                        if not args.allow_whole_torrent:
                            selected_now = {idx for idx, f in current_by_index.items()
                                            if f.get("priority") != 0}
                            pending_indices = {int(r["file_index"]) for r in pending}
                            if selected_now and selected_now.issubset(pending_indices):
                                for row in pending:
                                    row.update(status="WHOLE_TORRENT",
                                               detail="Would deselect every remaining selected file at apply time")
                                pending = []
                    if pending:
                        ids = "|".join(str(row["file_index"]) for row in pending)
                        qb.request("torrents/filePrio", {"hash": torrent_hash, "id": ids, "priority": 0}, json_result=False)
                        confirmed = qb.request("torrents/files", {"hash": torrent_hash})
                        current = {int(f.get("index", i)): f for i, f in enumerate(confirmed)}
                        for row in pending:
                            found = current.get(int(row["file_index"]))
                            if found and found.get("priority") == 0:
                                row.update(status="APPLIED", detail="Verified priority=0 by rereading API")
                            else:
                                row.update(status="REFUSED", detail="qBittorrent did not confirm priority=0 (common for completed files)")
                except (RuntimeError, HTTPError, URLError, KeyError, ValueError, TypeError) as exc:
                    for row in group:
                        if row["status"] == "READY":
                            row.update(status="ERROR", detail=str(exc)[:260])
    if quarantine_requested:
        candidates = [r for r in rows if r['status'] == 'MOVE_READY']
        print(f"\nQuarantine candidates: {len(candidates)} (only unmatched across ALL categories)")
        print(f"Destination: {quarantine_root}")
        print("No files are permanently deleted. Physical files must be old enough and sizes must match.")
        if args.ignore_missing_metadata:
            print("WARNING: you opted to IGNORE torrents without metadata. These are NOT proven unowned files.")
        if args.move_unmatched and candidates:
            if len(candidates) > args.max_moves:
                raise RuntimeError(f"Refusing {len(candidates)} moves: --max-moves={args.max_moves}")
            if input(f"Type MOVE to quarantine {len(candidates)} files: ").strip() != 'MOVE':
                print('Cancelled quarantine. No files moved.')
            else:
                # IMPORTANT: do not rely on a stale torrent list, or on a list
                # filtered to any particular category, to determine ownership.
                fresh_songs = subsonic_call(nav_url, nav_user, nav_pass, 'getStarred2').get('starred2', {}).get('song', []) or []
                fresh_songs = list({str(song.get('id', i)): song for i, song in enumerate(fresh_songs)}.values())
                fresh_rows, now, fresh_lookup = refresh_torrent_index(
                    qb, torrents, args.nav_root, args.qb_root, fresh_songs,
                    args.protect_blocked_folders, args.ignore_missing_metadata)
                now_candidates = {r['navidrome_path']: r for r in fresh_rows if r['status'] == 'NOT_FOUND'}
                for row in candidates:
                    latest = now_candidates.get(row['navidrome_path'])
                    if latest is None or str(latest['navidrome_size']) != str(row['navidrome_size']):
                        row.update(status='SKIP_CHANGED', detail='No longer hearted or no longer globally unmatched')
                        continue
                    # Refuse if a torrent entry was added with the same path,
                    # even if it was not present during the initial dry run.
                    try:
                        quarantine_file(row, host_music_root, quarantine_root, args.min_age_minutes * 60)
                        row.update(status='MOVED', detail='Quarantined successfully; original removed from Navidrome music root')
                    except (OSError, RuntimeError, ValueError) as exc:
                        row.update(status='MOVE_ERROR', detail=str(exc)[:260])
    report = args.report or Path(__file__).resolve().parent / ("navidrome_qbittorrent_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv")
    write_report(rows, report.expanduser())
    print("\nFinal status:")
    for status, count in Counter(r["status"] for r in rows).most_common():
        print(f"  {status}: {count}")
    print(f"Report saved to: {report}")
    if not args.apply and not args.move_unmatched:
        print("DRY RUN ONLY: nothing was changed. Use --apply for priority, --move-unmatched for quarantine.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (HTTPError, URLError, TimeoutError, RuntimeError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
