"""Verified SQLite snapshots. Run as an operator, not from a public endpoint.

The SQLite backup API captures a consistent live snapshot, including committed
WAL data. Only verified artifacts created by this utility are eligible for
retention pruning. These local snapshots must also be encrypted and copied to
an independently controlled offsite destination by the deployment operator.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time

FORMAT = 'fanntv-sqlite-backup-v1'
SNAPSHOT_NAME = re.compile(r'^fanntv-\d{8}T\d{12}Z-[a-f0-9]{8}\.sqlite3$')


def digest(path):
    hasher = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            hasher.update(block)
    return hasher.hexdigest()


def integrity_check(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
        raise sqlite3.DatabaseError('Snapshot integrity check failed.')


def readonly(path):
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)


def reserve(path):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(descriptor)


def prune_backups(directory, retain, protected=()):
    """Delete only checksum-matched, utility-owned snapshot/manifest pairs."""
    protected = {p.resolve() for p in protected}
    candidates = []
    for snapshot in directory.iterdir():
        if not SNAPSHOT_NAME.fullmatch(snapshot.name) or snapshot.is_symlink():
            continue
        manifest = snapshot.with_suffix(snapshot.suffix + '.json')
        if not snapshot.is_file() or manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            metadata = json.loads(manifest.read_text())
            if not isinstance(metadata, dict):
                continue
            if (metadata.get('format') != FORMAT or metadata.get('restore_verified') is not True
                    or metadata.get('file') != snapshot.name or metadata.get('sha256') != digest(snapshot)):
                continue
        except (OSError, ValueError, TypeError):
            continue
        candidates.append((snapshot, manifest))
    candidates.sort(key=lambda pair: pair[0].name, reverse=True)
    for snapshot, manifest in candidates[retain:]:
        if snapshot.resolve() in protected:
            continue
        snapshot.unlink()
        manifest.unlink()


def create_backup(database, output_dir, retain=14, timeout=120):
    if not isinstance(retain, int) or retain < 1:
        raise ValueError('Retention must be at least one snapshot.')
    database = Path(database).resolve(strict=True)
    if not database.is_file():
        raise ValueError('Database is not a regular file.')
    output_dir = Path(output_dir)
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise ValueError('Backup destination must be a real directory.')
    if stat.S_IMODE(output_dir.stat().st_mode) & 0o077:
        raise PermissionError('Backup directory must be private to its owner.')
    output_dir = output_dir.resolve()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    snapshot = output_dir / f'fanntv-{stamp}-{secrets.token_hex(4)}.sqlite3'
    pending = output_dir / f'.pending-{secrets.token_hex(16)}.sqlite3'
    metadata_pending = output_dir / f'.pending-{secrets.token_hex(16)}.json'
    manifest = snapshot.with_suffix(snapshot.suffix + '.json')
    deadline = time.monotonic() + timeout

    def progress(*_):
        if time.monotonic() > deadline:
            raise TimeoutError('Backup deadline exceeded.')

    try:
        reserve(pending)
        with closing(readonly(database)) as source, closing(sqlite3.connect(pending)) as target:
            source.backup(target, pages=256, progress=progress, sleep=0.1)
            integrity_check(target)
        # Restore into an independent temporary database and check that the
        # snapshot can actually be opened/copied, rather than only hashed.
        with tempfile.TemporaryDirectory(prefix='fanntv-restore-check-') as temporary:
            restored = Path(temporary) / 'restored.sqlite3'
            reserve(restored)
            with closing(readonly(pending)) as source, closing(sqlite3.connect(restored)) as target:
                source.backup(target, pages=256, progress=progress, sleep=0.1)
                integrity_check(target)
        with pending.open('rb') as verified:
            os.fsync(verified.fileno())
        metadata = {'format': FORMAT, 'file': snapshot.name, 'sha256': digest(pending),
                    'bytes': pending.stat().st_size, 'restore_verified': True}
        reserve(metadata_pending)
        metadata_pending.write_text(json.dumps(metadata, sort_keys=True) + '\n')
        with metadata_pending.open('rb') as verified:
            os.fsync(verified.fileno())
        os.replace(pending, snapshot)
        os.replace(metadata_pending, manifest)
        descriptor = os.open(output_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        prune_backups(output_dir, retain, protected=(database, snapshot))
        return metadata
    finally:
        pending.unlink(missing_ok=True)
        metadata_pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='Create, restore-check, and retain private FANnTV SQLite snapshots.')
    parser.add_argument('--database', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--retain', type=int, default=14)
    args = parser.parse_args()
    try:
        result = create_backup(args.database, args.output_dir, args.retain)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        # Do not dump database contents, full paths, emails, or credentials.
        print(f'Backup failed ({type(error).__name__}). No new verified snapshot was confirmed.', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
