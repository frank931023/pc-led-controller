"""Small JSON files in data/ (device list, alert rules), written so they can never end up half-written."""

import json
import os
import tempfile


def write_json_atomic(path, data):
    """
    Write `data` as JSON: temp file in the same directory, then os.replace().
    A crash or full disk leaves either the old file or the new one, never half a JSON.
    Raises OSError.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
