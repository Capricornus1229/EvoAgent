"""Run repository tests without loading local credentials or the live database."""
import argparse
import builtins
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test*.py')
    args = parser.parse_args()
    sys.path.insert(0, str(root))
    os.chdir(root)
    blocked = {str(root / '.env'), str(root / 'evoagent' / '.env')}
    original_open, original_connect = builtins.open, sqlite3.connect

    def isolated_open(path, *values, **options):
        if isinstance(path, (str, bytes, os.PathLike)) and os.path.abspath(os.fsdecode(path)) in blocked:
            raise FileNotFoundError(os.fsdecode(path))
        return original_open(path, *values, **options)

    def isolated_connect(path, *values, **options):
        if isinstance(path, (str, bytes, os.PathLike)) and os.path.abspath(os.fsdecode(path)) == str(root / 'evoagent.db'):
            raise RuntimeError('Tests must use temporary databases')
        return original_connect(path, *values, **options)

    local_bypass = '127.0.0.1,localhost,::1'
    for name in ('NO_PROXY', 'no_proxy'):
        if os.environ.get(name):
            local_bypass += ',' + os.environ[name]
    with patch.dict(os.environ, {'NO_PROXY': local_bypass, 'no_proxy': local_bypass}), \
            patch('builtins.open', isolated_open), patch('sqlite3.connect', isolated_connect):
        suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern=args.pattern)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
