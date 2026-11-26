"""Preserve source HDF5 fields alongside a standard LeRobot market episode.

Arrays keep their source dtype and precision. The optional video representation
retains a field's metadata and points at the corresponding LeRobot camera
stream. Variable-length values use a byte stream and length/shape records, with
no Python pickle.
"""
import io
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile

import h5py
import numpy as np

from .export import path_for
from .io import CAMS


SCHEMA = 'market_hdf5_fields_v1'
CAMERA_KEYS = {f'observation/images/rs/{cam}/color': f'observation.images.{cam}' for cam in CAMS}
CAMERA_MODES = ('video', 'original')


def archive_path(root, episode_index):
    return Path(root) / 'meta' / 'source_fields' / f'episode_{episode_index:06d}.zip'


def source_identity(path):
    value = Path(path).lstat()
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f'Not a regular HDF5 source file: {path}')
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns]


def _array(zf, member, value):
    with zf.open(member, 'w', force_zip64=True) as target:
        np.lib.format.write_array(target, np.asarray(value), allow_pickle=False)


def _byte_parts(value, codec):
    if isinstance(value, str):
        return value.encode(codec or 'utf-8')
    return bytes(value)


def _store(zf, member, dtype, shape, values, scalar, encoding=None):
    entry = dict(shape=list(shape), dtype=dtype.str, member=member)
    if not dtype.hasobject:
        _array(zf, member, scalar())
        return dict(entry, storage='npy')
    element = h5py.check_vlen_dtype(dtype)
    text = None
    if element in (bytes, str) or encoding:
        text = encoding or ('ascii' if element is bytes else 'utf-8')
    elif element is None:
        supplied = scalar()
        if not all(isinstance(v, (bytes, str)) for v in np.asarray(supplied, dtype=object).flat):
            raise ValueError('Unsupported object field in source HDF5')
        text = 'utf-8'
    extent = []
    with zf.open(member, 'w', force_zip64=True) as target:
        for value in values():
            if text is None:
                array = np.asarray(value, dtype=element)
                raw = array.tobytes(order='C')
                extent.append([len(raw), list(array.shape)])
            else:
                raw = _byte_parts(value, text)
                extent.append([len(raw), None])
            target.write(raw)
    if len(extent) != int(np.prod(shape, dtype=np.int64)):
        raise ValueError('Wrong element count while preserving variable-length HDF5 field')
    return dict(entry, storage='vlen', elements=extent,
                element_dtype=np.dtype(element).str if text is None else None,
                string_encoding=text)


def _values(dataset):
    if dataset.ndim == 0:
        yield dataset[()]
    else:
        for begin in range(0, dataset.shape[0], 32):
            yield from np.asarray(dataset[begin:begin + 32], dtype=object).flat


def _store_attrs(zf, prefix, attrs):
    result = {}
    for i, (key, value) in enumerate(attrs.items()):
        a = np.asarray(value)
        result[key] = _store(zf, f'{prefix}/a{i}', a.dtype, a.shape,
                             lambda: iter(a.flat), lambda: a)
    return result


def _video_fields(root, index, fields):
    for field in fields.values():
        if field['storage'] == 'video':
            value = Path(root) / field['video']
            if not value.is_file() or value.is_symlink() or value.stat().st_size == 0:
                raise ValueError(f'Missing LeRobot video for HDF5 field: {value}')
            if value != path_for(root, index, field['lerobot_feature']):
                raise ValueError(f'Unexpected video path for HDF5 field: {value}')


def write_source_fields(source, root, episode_index, *, camera_mode='video'):
    """Write and verify an atomic, pickle-free archive for one episode."""
    if camera_mode not in CAMERA_MODES:
        raise ValueError(f'Unknown source camera mode: {camera_mode}')
    source, root = Path(source), Path(root)
    before = source_identity(source)
    target = archive_path(root, episode_index)
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = dict(schema=SCHEMA, episode_index=episode_index, source_hdf5=str(source),
                    source_identity=before, camera_mode=camera_mode, fields={}, groups={})
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.partial', delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with h5py.File(source, 'r') as original, ZipFile(temporary, 'w', ZIP_DEFLATED, compresslevel=3) as zf:
            manifest['groups']['/'] = _store_attrs(zf, 'groups/root', original.attrs)

            def visit(name, item):
                if isinstance(item, h5py.Group):
                    manifest['groups'][name] = _store_attrs(zf, f"groups/g{len(manifest['groups'])}", item.attrs)
                    return
                if not isinstance(item, h5py.Dataset) or item.shape is None:
                    raise ValueError(f'Unsupported HDF5 field: {name}')
                prefix = f"fields/f{len(manifest['fields'])}"
                if camera_mode == 'video' and name in CAMERA_KEYS:
                    feature = CAMERA_KEYS[name]
                    entry = dict(storage='video', lerobot_feature=feature, shape=list(item.shape),
                                 dtype=item.dtype.str, video=str(path_for(root, episode_index, feature).relative_to(root)))
                else:
                    string = h5py.check_string_dtype(item.dtype)
                    entry = _store(zf, f'{prefix}/value', item.dtype, item.shape,
                                   lambda: _values(item), lambda: item[()], string.encoding if string else None)
                entry['attributes'] = _store_attrs(zf, prefix, item.attrs)
                enum = h5py.check_enum_dtype(item.dtype)
                if enum:
                    entry['enum'] = enum
                manifest['fields'][name] = entry

            original.visititems(visit)
            zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False))
        if source_identity(source) != before:
            raise ValueError('Source HDF5 changed during field export')
        validate_source_fields(temporary, root, episode_index)
        with temporary.open('rb') as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target.relative_to(root))


def validate_source_fields(archive, root, episode_index):
    with ZipFile(archive) as zf:
        corrupt = zf.testzip()
        if corrupt:
            raise ValueError(f'Corrupt HDF5 source field: {corrupt}')
        value = json.loads(zf.read('manifest.json'))
        if value['schema'] != SCHEMA or value['episode_index'] != episode_index or not value['fields']:
            raise ValueError('Invalid HDF5 source field manifest')
        _video_fields(root, episode_index, value['fields'])
        return value



def relocate_source_fields(source_root, old_index, target_root, new_index):
    """Copy a field archive and retarget its video links during LeRobot regrouping."""
    source_root, target_root = Path(source_root), Path(target_root)
    source = archive_path(source_root, old_index)
    target = archive_path(target_root, new_index)
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as before, ZipFile(target, 'w', ZIP_DEFLATED, compresslevel=3) as after:
        manifest = json.loads(before.read('manifest.json'))
        if manifest['schema'] != SCHEMA or manifest['episode_index'] != old_index:
            raise ValueError('Unexpected HDF5 source field archive while regrouping')
        manifest['episode_index'] = new_index
        for field in manifest['fields'].values():
            if field['storage'] == 'video':
                field['video'] = str(path_for(target_root, new_index, field['lerobot_feature']).relative_to(target_root))
        for name in before.namelist():
            if name != 'manifest.json':
                with before.open(name) as value, after.open(name, 'w', force_zip64=True) as dest:
                    shutil.copyfileobj(value, dest)
        after.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, allow_nan=False))
    return str(target.relative_to(target_root))


def _load_entry(zf, entry):
    if entry['storage'] == 'npy':
        return np.load(io.BytesIO(zf.read(entry['member'])), allow_pickle=False)
    if entry['storage'] != 'vlen':
        raise ValueError(f"Unknown source value encoding: {entry['storage']}")
    result = np.empty(entry['shape'], dtype=object)
    with zf.open(entry['member']) as fh:
        item_dtype = entry['element_dtype']
        for i, (size, shape) in enumerate(entry['elements']):
            raw = fh.read(size)
            if len(raw) != size:
                raise ValueError('Truncated variable-length source field')
            result.flat[i] = (raw if item_dtype is None
                              else np.frombuffer(raw, dtype=item_dtype).reshape(shape).copy())
        if fh.read(1):
            raise ValueError('Unexpected trailing variable-length source data')
    return result


def read_source_field(root, episode_index, name):
    """Return a raw HDF5 value or the video path for a mapped camera field."""
    root = Path(root)
    with ZipFile(archive_path(root, episode_index)) as zf:
        manifest = json.loads(zf.read('manifest.json'))
        if manifest['schema'] != SCHEMA or manifest['episode_index'] != episode_index:
            raise ValueError('Unexpected source field archive')
        entry = manifest['fields'][name.lstrip('/')]
        if entry['storage'] != 'video':
            return _load_entry(zf, entry)
        rel = PurePosixPath(entry['video'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValueError('Invalid archived video path')
        return root / rel
