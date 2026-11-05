#!/usr/bin/env python3
"""Portable private reply delivery, using the real writer and reader."""
import hashlib
import os
from pathlib import Path
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ReplyFiles(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root/'runtime'
        self.runtime.mkdir(mode=0o700)
        self.omarchy = self.root/'omarchy'
        (self.omarchy/'shell').mkdir(parents=True)
        (self.omarchy/'shell/shell.qml').write_text('// fixture')
        self.env = patch.dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime),
                              OMARCHY_PATH=str(self.omarchy), WAYLAND_DISPLAY='wayland-0')
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        self.file = self.runtime/'grokbot-desktop/replies/fallback-test-123.txt'
    def write(self, answer='Resposta de teste', ask='fallback-test-123'):
        return subprocess.run([str(ROOT/'bin/grokbot-desktop-ctl'), 'reply', ask],
                              input=answer, text=True, capture_output=True)
    def consume(self):
        from agent_desktop.reply_files import consume
        return consume('fallback-test-123')
    def test_writer_reader_unicode_limit_and_removal(self):
        answer = '🦉' * 4000
        result = self.write(answer)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.file.read_text(), answer)
        for path, mode in [(self.file, 0o600), (self.file.parent, 0o700), (self.file.parent.parent, 0o700)]:
            self.assertEqual(path.stat().st_mode & 0o777, mode)
            self.assertEqual(path.stat().st_uid, os.getuid())
        self.assertEqual(self.consume(), answer)
        self.assertFalse(self.file.exists())
        self.assertIsNone(self.consume())
        self.assertNotEqual(self.write(answer+'x').returncode, 0)
        self.assertNotEqual(self.write(ask='../escape').returncode, 0)
    def test_runtime_and_directory_symlinks_or_permissions_fail_closed(self):
        self.runtime.chmod(0o755)
        self.assertNotEqual(self.write().returncode, 0)
        self.runtime.chmod(0o700)
        alias = self.root/'alias'
        alias.symlink_to(self.runtime, target_is_directory=True)
        with patch.dict(os.environ, XDG_RUNTIME_DIR=str(alias)):
            self.assertNotEqual(self.write().returncode, 0)
        (self.runtime/'grokbot-desktop').symlink_to(self.root, target_is_directory=True)
        self.assertNotEqual(self.write().returncode, 0)
    def test_unsafe_files_refused_by_both_sides(self):
        for kind in ('symlink', 'mode', 'fifo', 'large', 'hardlink', 'invalid_utf8'):
            with self.subTest(kind=kind):
                self.assertEqual(self.write().returncode, 0)
                if kind == 'symlink':
                    self.file.unlink(); self.file.symlink_to(self.root/'outside')
                elif kind == 'mode': self.file.chmod(0o644)
                elif kind == 'fifo': self.file.unlink(); os.mkfifo(self.file, 0o600)
                elif kind == 'large': self.file.write_bytes(b'x'*16001)
                elif kind == 'invalid_utf8': self.file.write_bytes(b'\xff')
                elif kind == 'hardlink': os.link(self.file, self.root/'linked')
                with self.assertRaises((OSError, ValueError)):
                    self.consume()
                self.assertNotEqual(self.write().returncode, 0)
                self.file.unlink()
                (self.root/'linked').unlink(missing_ok=True)
    def test_existing_socket_failure_never_creates_file(self):
        identity = hashlib.md5(f'{self.omarchy}/shell\nwayland-0'.encode()).hexdigest()[:16]
        path = self.runtime/f'omarchy-shell-{identity}.sock'
        loader = importlib.machinery.SourceFileLoader('ctl', str(ROOT/'bin/grokbot-desktop-ctl'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        ctl = importlib.util.module_from_spec(spec)
        loader.exec_module(ctl)
        with patch.object(ctl, 'dictation_socket', return_value=str(path)), patch.object(ctl.socket, 'socket', side_effect=ConnectionRefusedError):
            self.assertEqual(ctl.private_call('reply', 'fallback-test-123', 'private'), 1)
        self.assertFalse(self.file.exists())
        path.write_text('not a socket')
        self.assertNotEqual(self.write().returncode, 0)
        path.unlink()
        path.symlink_to(self.root/'absent')
        self.assertNotEqual(self.write().returncode, 0)
        self.assertFalse(self.file.exists())

    def test_socket_priority_and_uncertain_delivery(self):
        import struct
        loader = importlib.machinery.SourceFileLoader('ctl', str(ROOT/'bin/grokbot-desktop-ctl'))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        ctl = importlib.util.module_from_spec(spec)
        loader.exec_module(ctl)
        for response, expected in [(b'OK\x1fok\x1e', 0), (b'SKIP\x1e', 1), (b'OK', 1), (b'', 1)]:
            client = MagicMock()
            client.__enter__.return_value = client
            client.getsockopt.return_value = struct.pack('3i', 1, os.getuid(), os.getgid())
            client.recv.side_effect = [response, b'']
            with patch.object(ctl, 'dictation_socket', return_value='/existing.sock'), patch.object(ctl.socket, 'socket', return_value=client):
                self.assertEqual(ctl.private_call('reply', 'fallback-test-123', 'private'), expected)
            client.sendall.assert_called_once()
            self.assertFalse(self.file.exists())

    def test_atomic_failure_keeps_previous_reply_and_cleans_temporary(self):
        from agent_desktop.reply_files import write
        self.assertEqual(self.write('previous').returncode, 0)
        with patch('agent_desktop.reply_files.os.replace', side_effect=OSError):
            with self.assertRaises(OSError): write('fallback-test-123', 'next')
        self.assertEqual(self.file.read_text(), 'previous')
        self.assertEqual(list(self.file.parent.iterdir()), [self.file])

    def test_wrong_owner_and_nonprivate_directories(self):
        from agent_desktop.reply_files import check
        from types import SimpleNamespace
        with self.assertRaises(ValueError):
            check(SimpleNamespace(st_mode=0o100600, st_uid=os.getuid()+1, st_nlink=1, st_size=0))
        self.assertEqual(self.write().returncode, 0)
        self.file.parent.chmod(0o755)
        self.assertNotEqual(self.write().returncode, 0)
        with self.assertRaises(ValueError): self.consume()

    def test_desktop_reader_consumes_only_pending_ids(self):
        import json
        self.assertEqual(self.write().returncode, 0)
        result = subprocess.run(['/usr/bin/python3', '-I', str(ROOT/'bin/grokbot-reply-read')],
                                input=json.dumps(['fallback-test-123']), text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'fallback-test-123': 'Resposta de teste'})
        self.assertFalse(self.file.exists())


if __name__ == '__main__':
    unittest.main()
