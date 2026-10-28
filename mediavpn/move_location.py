"""Last-moment validation inside the owned torrent client's filesystem."""

PREPARE_MOVE_CODE = r'''
def prepare_move(identifier, target, send):
    import base64,json,os,pathlib
    def read(path):
        return json.loads(base64.b64decode(send(path)['body']))
    rows=read('torrents/info?hashes='+identifier)
    if not isinstance(rows,list) or len(rows)!=1:raise ValueError('Torrent identity unavailable')
    row=rows[0]
    if (row.get('hash','').lower()!=identifier.lower() or row.get('progress')!=1
            or row.get('state') not in {'uploading','stalledUP','forcedUP','stoppedUP','pausedUP','queuedUP'}
            or row.get('auto_tmm') is True):raise ValueError('Torrent is not ready for moving')
    if not target.is_dir() or target.resolve()!=target or not os.access(target,os.W_OK|os.X_OK):
        raise ValueError('Destination unavailable')
    source=pathlib.Path(row.get('save_path',''))
    if not source.is_absolute() or not source.is_relative_to('/downloads') or source.resolve()!=source:
        raise ValueError('Source unavailable')
    files=read('torrents/files?hash='+identifier)
    if not isinstance(files,list) or not 1<=len(files)<=20000:raise ValueError('File list unavailable')
    for entry in files:
        name=entry.get('name','');relative=pathlib.PurePosixPath(name)
        if (not name or not relative.parts or relative.is_absolute() or str(relative)!=name or '\\' in name
                or '..' in relative.parts or entry.get('progress')!=1):raise ValueError('Invalid file')
        if (target/relative.parts[0]).exists() or (target/relative.parts[0]).is_symlink():
            raise ValueError('Destination collision')
        path=source.joinpath(*relative.parts)
        if path.resolve()!=path or not path.is_file() or path.stat().st_size!=entry.get('size'):
            raise ValueError('Source file unavailable')
'''
