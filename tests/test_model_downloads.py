import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from model_downloads import (DownloadManager, DownloadError, DownloadConflict,
                             parse_source, filename, validate_remote, SafeRedirect)

URL = 'https://huggingface.co/owner/repo/blob/main/nested/model.gguf?download=true'


class Response(io.BytesIO):
    status = 200

    def __init__(self, content=b'GGUFdata', length='8'):
        super().__init__(content)
        self.headers = {} if length is None else {'Content-Length': length}

    def read1(self, size):
        return self.read(size)


class Opener:
    def __init__(self, response=None):
        self.response = response or Response()

    def open(self, req, timeout):
        return self.response


class DownloadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.net = patch('model_downloads.validate_remote')
        self.net.start()
        self.manager = DownloadManager(Opener())

    def tearDown(self):
        self.manager.close()
        self.net.stop()
        self.tmp.cleanup()

    def wait(self):
        self.manager.thread.join(3)
        self.assertFalse(self.manager.thread.is_alive())
        return self.manager.snapshot()[0]

    def test_url_and_filename(self):
        self.assertEqual(parse_source(URL), ('https://huggingface.co/owner/repo/resolve/main/nested/model.gguf', 'model.gguf'))
        self.assertIn('/feature%2Ftest/', parse_source(URL.replace('/main/', '/feature%2Ftest/'))[0])
        self.assertEqual(parse_source(URL.replace('model.gguf', 'a%20b.gguf'))[1], 'a b.gguf')
        self.assertEqual(filename('custom'), 'custom.gguf')
        for value in ['../x.gguf', 'x/y.gguf', 'x\\y.gguf', '.gguf', 'a\n.gguf', 'a.bin', ' a.gguf', 'a' * 256]:
            with self.subTest(value=value), self.assertRaises(DownloadError):
                filename(value)
        for value in [None, 42, URL.replace('https:', 'http:'), URL.replace('huggingface.co', 'huggingface.co.evil.com'), URL.replace('huggingface.co', 'user@huggingface.co'), URL.replace('huggingface.co', 'huggingface.co:443'), URL.replace('.gguf', '.txt'), URL.replace('/main/', '/../'), URL.replace('model.gguf', '%2Fetc.gguf')]:
            with self.subTest(value=value), self.assertRaises(DownloadError):
                parse_source(value)

    def test_all_destinations_and_rename(self):
        for sub in ['', 'mtp', 'mmproj', 'archived']:
            self.manager.opener = Opener()
            self.manager.start(self.root, URL, sub, 'renamed')
            job = self.wait()
            self.assertEqual(job['state'], 'completed')
            self.assertEqual(job['downloaded_bytes'], 8)
            self.assertEqual((self.root / sub / 'renamed.gguf').read_bytes(), b'GGUFdata')
            self.assertEqual(list((self.root / sub).glob('*.part')), [])

    def test_unknown_length(self):
        self.manager.opener = Opener(Response(length=None))
        self.manager.start(self.root, URL)
        self.assertEqual(self.wait()['state'], 'completed')
        self.assertIsNone(self.manager.snapshot()[0]['total_bytes'])

    def test_bad_content_and_length(self):
        for content, length in [(b'<html>bad', '9'), (b'GGUF', '100'), (b'GG', None)]:
            self.manager.opener = Opener(Response(content, length))
            self.manager.start(self.root, URL)
            self.assertEqual(self.wait()['state'], 'failed')
            self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_and_symlink(self):
        (self.root / 'model.gguf').write_bytes(b'original')
        with self.assertRaises(DownloadConflict):
            self.manager.start(self.root, URL)
        (self.root / 'mtp').symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(OSError):
            self.manager.start(self.root, URL, 'mtp')
        for sub in ['other', '../', None, 1]:
            with self.assertRaises(DownloadError):
                self.manager.start(self.root, URL, sub)
        self.assertEqual((self.root / 'model.gguf').read_bytes(), b'original')

    def test_collision_at_publication(self):
        response = Response()
        original_read = response.read
        def read(size):
            (self.root / 'model.gguf').write_bytes(b'keep')
            return original_read(size)
        response.read = read
        self.manager.opener = Opener(response)
        self.manager.start(self.root, URL)
        self.assertEqual(self.wait()['state'], 'failed')
        self.assertEqual((self.root / 'model.gguf').read_bytes(), b'keep')

    def test_cancel_concurrency_progress(self):
        entered, release = threading.Event(), threading.Event()
        response = Response()
        original_read = response.read
        def read(size):
            entered.set()
            release.wait(3)
            return original_read(size)
        response.read = read
        self.manager.opener = Opener(response)
        job = self.manager.start(self.root, URL)
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.manager.snapshot()[0]['state'], 'downloading')
            with self.assertRaises(DownloadConflict):
                self.manager.start(self.root, URL, name='other')
            self.manager.cancel(job['id'])
        finally:
            release.set()
        self.assertEqual(self.wait()['state'], 'cancelled')
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIsNone(self.manager.cancel('missing'))

    def test_http_and_disk_errors(self):
        for exc in [urllib.error.HTTPError(URL, 403, 'Forbidden', {}, None), OSError(28, 'No space left on device')]:
            with patch.object(self.manager.opener, 'open', side_effect=exc):
                self.manager.start(self.root, URL)
                self.assertEqual(self.wait()['state'], 'failed')
                self.assertNotIn('https://', self.manager.snapshot()[0]['error'])
                self.assertEqual(list(self.root.iterdir()), [])

    def test_low_space_and_interrupted_read(self):
        from types import SimpleNamespace
        with patch('os.fstatvfs', return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
            self.manager.start(self.root, URL)
            self.assertEqual(self.wait()['error'], 'Not enough free disk space')
        response = Response()
        response.read = lambda size: (_ for _ in ()).throw(TimeoutError('timeout'))
        self.manager.opener = Opener(response)
        self.manager.start(self.root, URL)
        self.assertEqual(self.wait()['state'], 'failed')
        self.assertEqual(list(self.root.iterdir()), [])

    def test_history_bounded_and_terminal_cancel_safe(self):
        for i in range(23):
            self.manager.opener = Opener()
            self.manager.start(self.root, URL, name='model-' + str(i))
            job = self.wait()
            self.assertEqual(self.manager.cancel(job['id'])['state'], 'completed')
        self.assertEqual(len(self.manager.snapshot()), 20)

    def test_thread_start_failure_releases_resources(self):
        with patch('threading.Thread.start', side_effect=RuntimeError('thread unavailable')):
            with self.assertRaises(RuntimeError):
                self.manager.start(self.root, URL)
        self.assertIsNone(self.manager.active)
        self.assertEqual(self.manager.snapshot(), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.manager.thread = None

    def test_stale_part_cleanup(self):
        stale = self.root / ('.llama-download-' + 'a' * 32 + '.part')
        stale.write_bytes(b'partial')
        unrelated = self.root / 'other.part'
        unrelated.write_bytes(b'keep')
        self.manager.start(self.root, URL)
        self.wait()
        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())


class RemoteValidationTest(unittest.TestCase):
    def test_hosts_and_dns(self):
        addr = [(2, 1, 6, '', ('8.8.8.8', 443))]
        with patch('socket.getaddrinfo', return_value=addr):
            for host in ['huggingface.co', 'cdn-lfs.huggingface.co', 'cdn-lfs-us-1.hf.co', 'cas-bridge.xethub.hf.co']:
                validate_remote('https://' + host + '/file')
            for url in ['http://huggingface.co/a', 'https://evil.com/a', 'https://hf.co.evil.com/a', 'https://user@hf.co/a', 'https://hf.co:8080/a']:
                with self.assertRaises(DownloadError):
                    validate_remote(url)
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]):
            with self.assertRaises(DownloadError):
                validate_remote('https://huggingface.co/a')

    def test_redirect_validation(self):
        import urllib.request
        req = urllib.request.Request('https://huggingface.co/a')
        handler = SafeRedirect()
        with self.assertRaises(DownloadError):
            handler.redirect_request(req, None, 302, '', {}, 'https://localhost/file')
        with patch('model_downloads.validate_remote') as validate:
            result = handler.redirect_request(req, None, 302, '', {}, 'https://cas-bridge.xethub.hf.co/file')
            validate.assert_called_once()
            self.assertEqual(result.host, 'cas-bridge.xethub.hf.co')


class DownloadApiTest(unittest.TestCase):
    def setUp(self):
        import app
        self.app = app
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = DownloadManager(Opener())
        self.patches = [patch.object(app, '_downloads', self.manager),
                        patch.dict(os.environ, MODELS_DIR=self.tmp.name),
                        patch('model_downloads.validate_remote')]
        for p in self.patches:
            p.start()
        self.client = app.app.test_client()

    def tearDown(self):
        self.manager.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_api_and_picker(self):
        # No models.ini exists: downloader must not depend on its write gate.
        response = self.client.post('/api/models/downloads', json={'url': URL, 'subdirectory': 'mmproj', 'filename': 'vision'})
        self.assertEqual(response.status_code, 202)
        self.manager.thread.join(3)
        data = self.client.get('/api/models/downloads').get_json()
        self.assertEqual(data['jobs'][0]['state'], 'completed')
        files = self.client.get('/api/models/files').get_json()
        self.assertEqual(files['mmproj'][0]['rel'], 'mmproj/vision.gguf')
        job_id = data['jobs'][0]['id']
        self.assertEqual(self.client.post('/api/models/downloads/' + job_id + '/cancel', json={}).status_code, 200)
        self.assertEqual(self.client.post('/api/models/downloads/missing/cancel', json={}).status_code, 404)
        self.assertFalse((Path(self.tmp.name) / 'models.ini').exists())

    def test_validation(self):
        for body in [{}, {'url': URL, 'filename': 3}, {'url': URL, 'subdirectory': []}]:
            self.assertEqual(self.client.post('/api/models/downloads', json=body).status_code, 400)
        self.assertEqual(self.client.post('/api/models/downloads', json={'url': URL}, headers={'Origin': 'https://evil.com'}).status_code, 403)
        self.assertEqual(self.client.post('/api/models/downloads', data='bad').status_code, 400)
