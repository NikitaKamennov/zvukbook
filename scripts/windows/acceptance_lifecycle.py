"""Explicit synthetic acceptance of retained MP3 publication across restart."""
import argparse
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import runpy
import subprocess
import time

Client = runpy.run_path(str(Path(__file__).with_name('acceptance.py')))['Client']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', required=True, type=Path)
    parser.add_argument('--bundle', required=True, type=Path)
    args = parser.parse_args()
    root = args.data_root.absolute()
    assert root.is_relative_to(Path(__file__).resolve().parents[2] / '.local-data/windows-x64/acceptance')
    basic = json.loads((root / 'acceptance-basic.json').read_text())
    client = Client(root)
    book_id = basic['book_id']
    chapter = client.get(f'/api/books/{book_id}/chapters/1')
    client.post(f'/api/books/{book_id}/chapter', {'index': 1, 'title': chapter['chapter']['title'],
                'text': chapter['chapter']['text'] + ' Сохраняем готовую запись при перезапуске приложения.',
                'document_revision': chapter['document_revision']})
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    ready = json.loads((root / 'headless-ready.json').read_text())
    process_handle = kernel.OpenProcess(0x100000, False, ready['pid'])
    if not process_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    audio = root / 'library/books' / book_id / 'mp3/0001.mp3'
    old_audio = audio.read_bytes()
    handle = kernel.CreateFileW(str(audio), 0x80000000, 1, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        job_id = client.render(book_id, [1])
        client.wait(job_id, expected='publication_pending')
        directory = root / 'library/jobs' / job_id
        original_report = (directory / 'host-report.json').read_bytes()
        (root / 'desktop-stop.request').write_text('close')
        assert kernel.WaitForSingleObject(process_handle, 20000) == 0
        print('close with pending publication: OK', flush=True)
        with (root / 'lifecycle-launch.log').open('wb') as log:
            process = subprocess.Popen([str(args.bundle.absolute() / 'Zvukbook.exe'), '--headless', '--device', 'cpu', '--data-root', str(root)],
                                       creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    path = root / 'headless-ready.json'
                    if path.exists() and json.loads(path.read_text())['pid'] == process.pid:
                        break
                    if process.poll() is not None:
                        raise RuntimeError('Restart failed; inspect lifecycle-launch.log')
                    time.sleep(0.2)
                else:
                    raise TimeoutError('Restart')
                client = Client(root)
                job = next(job for job in client.get('/api/state')['jobs'] if job['id'] == job_id)
                assert job['status'] == 'publication_pending'
                assert (directory / 'out/0001.mp3').is_file() and audio.read_bytes() == old_audio
                kernel.CloseHandle(handle)
                handle = None
                client.post('/api/jobs/' + job_id + '/publish', {})
                client.wait(job_id)
                assert audio.read_bytes() != old_audio and not (directory / 'out').exists()
                assert (directory / 'host-report.json').read_bytes() == original_report
                print('restart retains MP3; publish without worker/TTS: OK', flush=True)
            finally:
                (root / 'desktop-stop.request').write_text('close')
                process.wait(timeout=20)
                assert process.returncode == 0
    finally:
        if handle:
            kernel.CloseHandle(handle)
        kernel.CloseHandle(process_handle)
    (root / 'acceptance-lifecycle.json').write_text(json.dumps({'ok': True, 'checks': ['pending retained on restart', 'publication without TTS', 'graceful shutdown']}, indent=2))


if __name__ == '__main__':
    main()
