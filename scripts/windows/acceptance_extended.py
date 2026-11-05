"""Synthetic Windows formats, replacement locks, cancellation and deletion."""
import argparse
import ctypes
from ctypes import wintypes
import io
import json
from pathlib import Path
import runpy
import subprocess
import time
import zipfile

from PIL import Image, ImageDraw, ImageFont
from docx import Document

Client = runpy.run_path(str(Path(__file__).with_name('acceptance.py')))['Client']


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as target:
        for name, value in files.items():
            target.writestr(name, value)
    return stream.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--formats', nargs='*')
    parser.add_argument('--no-scenarios', action='store_true')
    args = parser.parse_args()
    root, bundle = args.data_root.absolute(), args.bundle.absolute()
    client = Client(root)
    fixtures = root / 'synthetic-fixtures'
    fixtures.mkdir(exist_ok=True)
    phrase = 'Это вымышленная книга. Сегодня мы проверяем чтение русского текста. В саду поют птицы, а над рекой светит солнце.'
    fb2 = ('<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
           '<description><title-info><book-title>Проверка FB2</book-title><lang>ru</lang></title-info></description>'
           '<body><section><title><p>Глава первая</p></title><p>' + phrase + '</p></section></body></FictionBook>').encode()
    doc = Document()
    doc.add_heading('Глава первая', 1)
    doc.add_paragraph(phrase + ' Это документ Word.')
    doc.save(fixtures / 'synthetic.docx')
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 44)
    image = Image.new('RGB', (1800, 1200), 'white')
    draw = ImageDraw.Draw(image)
    draw.multiline_text((100, 120), 'Глава первая\n\nЭто вымышленная книга. Сегодня мы проверяем\nчтение русского текста. В саду поют птицы,\nа над рекой светит солнце.', font=font, fill='black', spacing=25)
    image.save(fixtures / 'synthetic.pdf', 'PDF', resolution=150)
    image.convert('1').save(fixtures / 'synthetic.pbm')
    subprocess.run([str(bundle / 'tools/djvulibre/cjb2.exe'), str(fixtures / 'synthetic.pbm'), str(fixtures / 'synthetic.djvu')],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    epub = archive({
        'mimetype': 'application/epub+zip',
        'META-INF/container.xml': '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/book.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        'OEBPS/book.opf': '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Проверка EPUB</dc:title><dc:language>ru</dc:language><dc:identifier id="id">synthetic</dc:identifier></metadata><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/></spine></package>',
        'OEBPS/one.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Глава</title></head><body><h1>Глава первая</h1><p>' + phrase + ' Это EPUB.</p></body></html>'})
    book_ids = []
    formats = []
    for name, data in [('synthetic.fb2', fb2), ('synthetic.fb2.zip', archive({'synthetic.fb2': fb2})),
                       ('synthetic.epub', epub)] + [(f'synthetic.{ext}', (fixtures / f'synthetic.{ext}').read_bytes()) for ext in ('docx', 'pdf', 'djvu')]:
        if args.formats is not None and name.removeprefix('synthetic.') not in args.formats:
            continue
        uploaded = client.upload(name, data)
        book_id = uploaded['book_id']
        job_id = (client.post('/api/books/' + book_id + '/import', {})['job_id']
                  if uploaded.get('duplicate') else uploaded['job_id'])
        client.wait(job_id, timeout=240)
        book_ids.append(book_id)
        chapter = client.get(f'/api/books/{book_id}/chapters/1')['chapter']['text']
        assert 'вымышленная' in chapter.casefold(), (name, chapter)
        print('import ' + name + ': OK', flush=True)
        formats.append(name.removeprefix('synthetic.'))
    if args.no_scenarios:
        return
    basic = json.loads((root / 'acceptance-basic.json').read_text())
    book_id = basic['book_id']
    current = client.get(f'/api/books/{book_id}/chapters/1')
    client.post(f'/api/books/{book_id}/chapter', {'index': 1, 'title': current['chapter']['title'],
                'text': current['chapter']['text'] + ' Новый текст для проверки занятого файла.',
                'document_revision': current['document_revision']})
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    path = root / 'library/books' / book_id / 'mp3/0001.mp3'
    previous = path.read_bytes()
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        job_id = client.render(book_id, [1])
        client.wait(job_id, expected='publication_pending')
        directory = root / 'library/jobs' / job_id
        assert (directory / 'out/0001.mp3').is_file() and path.read_bytes() == previous
        host_report = (directory / 'host-report.json').read_bytes()
        client.post('/api/jobs/' + job_id + '/publish', {})
        client.wait(job_id, expected='publication_pending')
        assert (directory / 'host-report.json').read_bytes() == host_report
    finally:
        kernel.CloseHandle(handle)
    client.post('/api/jobs/' + job_id + '/publish', {})
    client.wait(job_id)
    assert path.read_bytes() != previous and not (directory / 'out').exists()
    assert (directory / 'host-report.json').read_bytes() == host_report
    print('external MP3 lock, bounded retry, publication without TTS: OK', flush=True)
    cancel_id = client.render(book_id, [1, 2])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (root / 'library/jobs' / cancel_id / 'input/launch.json').exists():
            break
        time.sleep(0.2)
    else:
        raise TimeoutError('cancel worker did not start')
    client.post('/api/jobs/' + cancel_id + '/cancel', {})
    client.wait(cancel_id, expected='cancelled', timeout=60)
    assert path.is_file()
    print('running cancellation preserves old MP3: OK', flush=True)
    client.wait(client.render(book_id, [1, 2]))
    assert all(chapter['ready'] for chapter in client.get('/api/books/' + book_id)['chapter_list'])
    print('explicit resume after cancellation: OK', flush=True)
    for identity in book_ids:
        client.post('/api/books/' + identity + '/delete', {})
        assert not (root / 'library/books' / identity).exists()
    print('delete synthetic books: OK', flush=True)
    (root / 'acceptance-extended.json').write_text(json.dumps({'ok': True, 'formats': formats,
        'checks': ['external MP3 lock', 'publish without TTS', 'cancel', 'delete']}, indent=2))


if __name__ == '__main__':
    main()
