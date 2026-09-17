"""Regression coverage for the codebase review fixes."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import app
from models_ini import parse


class ParserRegressionTests(unittest.TestCase):
    def test_append_key_without_final_newline(self):
        for text in ('[a]\nx = 1', '[a]'):
            with self.subTest(text=text):
                doc = parse(text)
                doc.upsert_key('a', 'y', '2')
                reparsed = parse(doc.render())
                self.assertIn(('y', '2'), reparsed.block('a').keys())
                if 'x' in text:
                    self.assertIn(('x', '1'), reparsed.block('a').keys())

    def test_keys_after_blank_lines_belong_to_section(self):
        for prefix in ('', '# '):
            text = f'{prefix}[a]\n{prefix}x = 1\n\n# note\n{prefix}y = 2\n'
            doc = parse(text)
            self.assertEqual(doc.render(), text)
            self.assertEqual(doc.block('a', bool(prefix)).keys(), [('x', '1'), ('y', '2')])
            doc.upsert_key('a', 'y', '3', bool(prefix))
            self.assertEqual(parse(doc.render()).block('a', bool(prefix)).keys(),
                             [('x', '1'), ('y', '3')])

    def test_append_keeps_crlf(self):
        doc = parse('[a]\r\nx = 1')
        doc.upsert_key('a', 'y', '2')
        self.assertEqual(doc.render(), '[a]\r\nx = 1\r\ny = 2\r\n')


class ApiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'models.ini'
        self.path.write_text('[a]\nx = 1\n')
        self.env = patch.dict(os.environ, MODELS_INI_FILE=str(self.path),
                              MODELS_INI_DIR=self.tmp.name)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.undo = patch.dict(app._last_raw, text=None)
        self.undo.start()
        self.addCleanup(self.undo.stop)
        self.client = app.app.test_client()

    def test_api_preserves_crlf(self):
        self.path.write_bytes(b'[a]\r\nx = 1\r\n')
        response = self.client.post('/api/models/section/edit', json={'name': 'a', 'set': {'x': '2'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.path.read_bytes(), b'[a]\r\nx = 2\r\n')

    def test_failed_edit_and_raw_save_preserve_undo(self):
        app._last_raw['text'] = 'previous undo'
        for endpoint, body in (
            ('/api/models/section/edit', {'name': 'a', 'set': {'x': '2'}}),
            ('/api/models/raw', {'text': '[a]\nx = 2\n'}),
        ):
            with self.subTest(endpoint=endpoint), patch.object(app, '_write_text', side_effect=OSError('disk full')):
                self.assertEqual(self.client.post(endpoint, json=body).status_code, 500)
            self.assertEqual(app._last_raw['text'], 'previous undo')
            self.assertEqual(self.path.read_text(), '[a]\nx = 1\n')

    def test_failed_undo_can_be_retried(self):
        app._last_raw['text'] = '[a]\nx = 0\n'
        with patch.object(app, '_write_text', side_effect=OSError('disk full')):
            self.assertEqual(self.client.post('/api/models/undo', json={}).status_code, 500)
        self.assertEqual(app._last_raw['text'], '[a]\nx = 0\n')
        self.assertEqual(self.client.post('/api/models/undo', json={}).status_code, 200)
        self.assertIsNone(app._last_raw['text'])

    def test_non_object_json_rejected(self):
        for body in (['bad'], 'bad', 42, None):
            self.assertEqual(self.client.post('/api/models/raw/check', json=body).status_code, 400)

    def test_parameter_and_model_newlines_rejected(self):
        for key in ('x\ny', '#comment', ';comment', 'white space'):
            response = self.client.post('/api/models/section/edit', json={'name': 'a', 'set': {key: '2'}})
            self.assertEqual(response.status_code, 400)
        for value in ('/a\n[b]', '/a\r[b]', '/a\u2028[b]'):
            response = self.client.post('/api/models/section/add', json={'name': 'b', 'model': value})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.path.read_text(), '[a]\nx = 1\n')

    def test_cross_origin_restart_rejected_before_subprocess(self):
        for headers in ({'Origin': 'https://other.example'}, {'Sec-Fetch-Site': 'cross-site'}, {'Origin': 'null'}):
            with patch.object(app.subprocess, 'run') as run:
                response = self.client.post('/api/restart-llama', headers=headers)
                self.assertEqual(response.status_code, 403)
                run.assert_not_called()

    def test_stale_revision_rejects_edit_raw_and_undo(self):
        revision = app._revision(self.path.read_text())
        headers = {'If-Match': '"' + revision + '"'}
        response = self.client.post('/api/models/section/edit', headers=headers,
                                    json={'name': 'a', 'set': {'x': '2'}})
        self.assertEqual(response.status_code, 200)
        for endpoint, body in (
            ('/api/models/section/edit', {'name': 'a', 'set': {'x': '3'}}),
            ('/api/models/raw', {'text': '[a]\nx = 3\n'}),
            ('/api/models/undo', {}),
        ):
            with self.subTest(endpoint=endpoint):
                response = self.client.post(endpoint, headers=headers, json=body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.path.read_text(), '[a]\nx = 2\n')

    def test_section_save_returns_new_revision(self):
        for value in ('2', '2'):  # changed save followed by no-op
            response = self.client.post('/api/models/section/edit',
                                        json={'name': 'a', 'set': {'x': value}})
            self.assertEqual(response.get_json()['revision'], app._revision(self.path.read_text()))

    def test_raw_diff_reports_newline_only_changes(self):
        response = self.client.post('/api/models/raw/diff', json={'text': '[a]\nx = 1'})
        data = response.get_json()
        self.assertTrue(data['changed'])
        self.assertTrue(data['diff'])
        self.assertEqual(data['revision'], app._revision(self.path.read_text()))

    def test_raw_save_returns_new_revision(self):
        response = self.client.post('/api/models/raw', json={'text': '[a]\nx = 3\n'})
        self.assertEqual(response.get_json()['revision'], app._revision(self.path.read_text()))

    def test_parallel_log_viewers_and_disconnect_cleanup(self):
        processes = []
        popen = subprocess.Popen
        def spawn(*args, **kwargs):
            proc = popen(*args, **kwargs)
            processes.append(proc)
            return proc
        cmd = [sys.executable, '-u', '-c',
               'import time; print("ready"); time.sleep(60)']
        with patch.object(app, '_journalctl_cmd', return_value=cmd), \
                patch.object(app.subprocess, 'Popen', side_effect=spawn), \
                patch.object(app, '_LOG_HEARTBEAT_S', 0.01):
            first = self.client.get('/api/logs/llama-server', buffered=False)
            self.addCleanup(first.close)
            second = self.client.get('/api/logs/llama-server', buffered=False)
            self.addCleanup(second.close)
            self.assertIsNone(processes[0].poll())
            self.assertIsNone(processes[1].poll())
            # Quiet subprocesses still yield so disconnects get noticed.
            iterator = iter(first.response)
            for _ in range(20):
                if b': heartbeat' in next(iterator):
                    break
            else:
                self.fail('no heartbeat')
            first.close()
            self.assertIsNotNone(processes[0].poll())
            self.assertIsNone(processes[1].poll())
            second.close()
            self.assertIsNotNone(processes[1].poll())

    def test_log_viewer_limit(self):
        import threading
        slots = threading.BoundedSemaphore(1)
        slots.acquire()
        with patch.object(app, '_LOG_SLOTS', slots), patch.object(app.subprocess, 'Popen') as spawn:
            response = self.client.get('/api/logs/llama-server')
            self.assertIn(b'Too many log viewers', response.data)
            spawn.assert_not_called()
        slots.release()

    def test_configured_ini_path_survives_initialization(self):
        env = dict(os.environ, MODELS_INI_PATH='/custom/models.ini')
        result = subprocess.check_output([sys.executable, '-c',
            'import app; print(app.MODELS_INI_PATH)'], env=env, text=True)
        self.assertEqual(result.strip(), '/custom/models.ini')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', self.tmp.name, *args], text=True).strip()

    def init_git(self):
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('add', 'models.ini')
        self.git('commit', '-qm', 'initial')

    def test_git_counters_with_real_repository(self):
        self.init_git()
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')
        with patch.object(app, '_refresh_remote_ref'):
            status = app._git_status()
            self.assertEqual((status['ahead'], status['behind']), (0, 0))
            self.path.write_text('[a]\nx = 2\n')
            self.git('commit', '-qam', 'edit')
            status = app._git_status()
            self.assertEqual((status['ahead'], status['behind']), (1, 0))
            self.git('update-ref', '-d', 'refs/remotes/origin/main')
            status = app._git_status()
            self.assertEqual((status['ahead'], status['behind']), (None, None))

    def test_git_mutations_hold_file_and_git_locks(self):
        def git(*args, **kwargs):
            if args[0] in ('add', 'commit', 'pull'):
                self.assertTrue(app._write_lock.locked())
                self.assertTrue(app._git_lock.locked())
            return 0, {'rev-parse': 'main', 'status': ' M models.ini',
                       'log': 'abc test'}.get(args[0], ''), ''
        with patch.object(app, '_git', side_effect=git):
            for action in ('commit', 'pull'):
                response = self.client.post('/api/models/git', json={'action': action})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(app._write_lock.locked())
                self.assertFalse(app._git_lock.locked())

    def test_commit_leaves_unrelated_staged_file_out(self):
        self.init_git()
        other = Path(self.tmp.name) / 'other.txt'
        other.write_text('unrelated')
        self.git('add', 'other.txt')
        self.path.write_text('[a]\nx = 2\n')
        response = self.client.post('/api/models/git', json={'action': 'commit', 'message': 'dashboard'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.git('diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'), 'models.ini')
        self.assertEqual(self.git('diff', '--cached', '--name-only'), 'other.txt')


if __name__ == '__main__':
    unittest.main()
