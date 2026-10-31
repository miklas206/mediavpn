"""Storage preparation executed as the torrent client's uid in its mount namespace."""

PREPARE_INCOMPLETE_CODE = '''
def prepare_incomplete(destination):
    import os
    import tempfile
    from pathlib import Path
    parent = Path(destination)
    staging = parent / '.incomplete'
    if not parent.is_dir() or parent.resolve(strict=True) != parent:
        raise OSError('Final download folder is unavailable')
    staging.mkdir(exist_ok=True)
    if (not staging.is_dir() or staging.resolve(strict=True) != staging
            or staging.stat().st_dev != parent.stat().st_dev
            or os.path.ismount(staging)
            or not os.access(staging, os.W_OK | os.X_OK)):
        raise OSError('Incomplete download folder is unavailable')
    with tempfile.TemporaryFile(dir=staging) as probe:
        probe.write(b'\\0')
        probe.flush()
        os.fsync(probe.fileno())
    return str(staging)
'''
