"""Explicit, synthetic acceptance of a running --headless Windows bundle."""
import argparse
import hashlib
from http.cookiejar import CookieJar
import io
import json
from pathlib import Path
import time
import urllib.request
import zipfile


class Client:
    def __init__(self, root):
        self.root = root
        ready = json.loads((root / 'headless-ready.json').read_text())
        self.url, self.key = ready['url'], ready['token']
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                               urllib.request.HTTPCookieProcessor(CookieJar()))
        self.csrf = self.get('/api/state')['csrf_token']

    def request(self, path, data=None, headers=None):
        values = {'X-Zvukbook-UI-Key': self.key}
        if data is not None:
            values['X-CSRF-Token'] = self.csrf
        values.update(headers or {})
        with self.http.open(urllib.request.Request(self.url + path, data=data, headers=values), timeout=60) as response:
            return response.status, response.read()

    def get(self, path):
        return json.loads(self.request(path)[1])

    def post(self, path, value):
        return json.loads(self.request(path, json.dumps(value).encode(), {'Content-Type': 'application/json'})[1])

    def upload(self, name, content):
        boundary = 'ZvukbookSyntheticAcceptanceBoundary'
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
                'Content-Type: application/octet-stream\r\n\r\n').encode() + content + f'\r\n--{boundary}--\r\n'.encode()
        return json.loads(self.request('/api/books/upload', data,
                                      {'Content-Type': 'multipart/form-data; boundary=' + boundary})[1])

    def wait(self, job_id, expected='completed', timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = next(row for row in self.get('/api/state')['jobs'] if row['id'] == job_id)
            if job['status'] not in ('queued', 'running'):
                if job['status'] != expected:
                    log = self.root / 'library/jobs' / job_id / 'worker.log'
                    raise AssertionError(str(job) + '\n' + (log.read_text(errors='replace') if log.exists() else 'no log'))
                return job
            time.sleep(0.5)
        raise TimeoutError(job_id)

    def render(self, book_id, chapters):
        book = self.get('/api/books/' + book_id)
        return self.post('/api/books/' + book_id + '/render',
                         {'chapter_storage': 2, 'document_revision': book['document_revision'],
                          'voice': 'xenia', 'rate': 1, 'pitch': 0, 'chapters': chapters})['job_id']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True, type=Path)
    args = parser.parse_args()
    client = Client(args.data_root)
    assert client.request('/')[0] == 200
    text = ('Глава 1\n\nЭто вымышленная история для проверки локального приложения. '
            'Утром Никита вышел в сад. Там пели птицы, и светило солнце.\n\n'
            'Глава 2\n\nВечером друзья встретились у реки. Они поговорили о книгах '
            'и вернулись домой по тихой тропинке.\n')
    uploaded = client.upload('synthetic-acceptance.txt', text.encode())
    book_id = uploaded['book_id']
    job_id = (client.post('/api/books/' + book_id + '/import', {})['job_id']
              if uploaded.get('duplicate') else uploaded['job_id'])
    client.wait(job_id)
    book = client.get('/api/books/' + book_id)
    assert book['chapters'] == 2, book
    print('import: OK', flush=True)
    client.wait(client.render(book_id, [1, 2]))
    book = client.get('/api/books/' + book_id)
    assert all(ch['ready'] for ch in book['chapter_list']), book
    status, mp3 = client.request(f'/api/books/{book_id}/audio/1', headers={'Range': 'bytes=0-99'})
    assert status == 206 and len(mp3) == 100
    export = client.request('/api/books/' + book_id + '/export')[1]
    with zipfile.ZipFile(io.BytesIO(export)) as archive:
        assert set(archive.namelist()) == {'0001.mp3', '0002.mp3', 'manifest.json'}
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['complete'] and manifest['schema'] == 'projectpro-audiobook-v2'
    print('render, MP3 Range, ZIP: OK', flush=True)
    original = client.get(f'/api/books/{book_id}/chapters/1')
    client.post('/api/books/' + book_id + '/chapter',
                {'document_revision': original['document_revision'], 'index': 1, 'title': 'Глава 1',
                 'text': original['chapter']['text'] + ' Проверяем изменение только одной главы.'})
    changed = client.get('/api/books/' + book_id)
    assert not changed['chapter_list'][0]['ready'] and changed['chapter_list'][1]['ready']
    second_sha = changed['chapter_list'][1]['sha256']
    client.wait(client.render(book_id, [1]))
    changed = client.get('/api/books/' + book_id)
    assert all(ch['ready'] for ch in changed['chapter_list'])
    assert changed['chapter_list'][1]['sha256'] == second_sha
    print('chapter edit, selective replacement: OK', flush=True)
    (args.data_root / 'acceptance-basic.json').write_text(json.dumps(
        {'ok': True, 'book_id': book_id, 'device': client.get('/api/state')['limits']['device'],
         'checks': ['import', 'render', 'Range', 'ZIP', 'chapter edit', 'selective replacement']}, indent=2))


if __name__ == '__main__':
    main()
