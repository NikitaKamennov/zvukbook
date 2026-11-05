"""Extract pinned vendor payloads; never install runtimes into Windows."""
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import shutil
import struct
import subprocess
import zipfile


def run(arguments):
    subprocess.run([str(value) for value in arguments], check=True, stdout=subprocess.DEVNULL,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def original_filename(path):
    version = ctypes.WinDLL('version', use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD))
    version.GetFileVersionInfoW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p)
    version.VerQueryValueW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT))
    ignored = wintypes.DWORD()
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
    if not 0 < size < 8 * 1024 ** 2:
        return None
    data = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, data):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not version.VerQueryValueW(data, '\\VarFileInfo\\Translation', ctypes.byref(pointer), ctypes.byref(length)) or length.value < 4:
        return None
    language, codepage = struct.unpack('<HH', ctypes.string_at(pointer, 4))
    query = f'\\StringFileInfo\\{language:04x}{codepage:04x}\\OriginalFilename'
    if not version.VerQueryValueW(data, query, ctypes.byref(pointer), ctypes.byref(length)):
        return None
    return ctypes.wstring_at(pointer).strip()


def is_amd64(path):
    with path.open('rb') as source:
        if source.read(2) != b'MZ':
            return False
        source.seek(60)
        header = source.read(4)
        if len(header) != 4:
            return False
        source.seek(struct.unpack('<I', header)[0])
        return source.read(6) == b'PE\0\0d\x86'


def extract_crt(downloads, seven):
    target = downloads / 'msvc-14.44.35211-x64'
    if (target / 'native-ready.json').is_file():
        return target
    target.mkdir(exist_ok=True)
    data = (downloads / 'vc-redist-x64.exe').read_bytes()
    cabinets = downloads / 'msvc-cabinets'
    cabinets.mkdir(exist_ok=True)
    offset, number = 0, 0
    while True:
        offset = data.find(b'MSCF', offset)
        if offset < 0:
            break
        if offset + 36 <= len(data):
            size = struct.unpack_from('<I', data, offset + 8)[0]
            file_offset = struct.unpack_from('<I', data, offset + 16)[0]
            if 36 <= file_offset < size and offset + size <= len(data) and data[offset+24:offset+26] == b'\x03\x01':
                # Cabinet version is minor 3, major 1.
                cabinet = cabinets / f'payload-{number}.cab'
                cabinet.write_bytes(data[offset:offset+size])
                run([seven, 'x', cabinet, '-o' + str(cabinets / f'payload-{number}'), '-y'])
                number += 1
        offset += 4
    for source in list(cabinets.rglob('*')):
        if not source.is_file() or source.suffix == '.cab':
            continue
        with source.open('rb') as stream:
            signature = stream.read(4)
        if signature == b'MSCF':
            run([seven, 'x', source, '-o' + str(cabinets / ('nested-' + source.parent.name + '-' + source.name)), '-y'])
    names = set()
    for source in sorted(cabinets.rglob('*')):
        if not source.is_file() or not is_amd64(source):
            continue
        name = original_filename(source)
        if not name or Path(name).name != name or not name.lower().endswith('.dll'):
            continue
        if not name.lower().startswith(('msvcp140', 'vcruntime140', 'concrt140', 'vcomp140', 'vccorlib140')):
            continue
        name = name.lower()
        shutil.copy2(source, target / name)
        names.add(name)
    required = {'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}
    if not required <= names:
        raise RuntimeError('MSVC extraction is incomplete: missing ' + ', '.join(sorted(required - names)))
    # EULA comes from the same authenticated redistributable's UX cabinet.
    for source in cabinets.rglob('*'):
        if source.is_file() and source.stat().st_size < 100_000 and source.read_bytes()[:5] == b'{\\rtf':
            shutil.copy2(source, target / 'LICENSE.rtf')
            break
    (target / 'native-ready.json').write_text(json.dumps({'version': '14.44.35211', 'files': sorted(names)}, indent=2))
    return target


def extract_browser(downloads):
    target = downloads / 'webview2-156.0.4314.58-x64'
    marker = target / 'native-ready.json'
    if marker.is_file():
        return target
    target.mkdir(exist_ok=True)
    run(['expand.exe', '-F:*', downloads / 'webview2-fixed-156.0.4314.58-x64.cab', target])
    candidates = list(target.rglob('msedgewebview2.exe'))
    if len(candidates) != 1:
        raise RuntimeError('Unexpected WebView2 cabinet layout')
    browser = candidates[0].parent
    marker.write_text(json.dumps({'version': '156.0.4314.58', 'folder': str(browser.relative_to(target))}, indent=2))
    return browser


def browser_folder(downloads):
    target = downloads / 'webview2-156.0.4314.58-x64'
    marker = target / 'native-ready.json'
    if marker.is_file():
        return target / json.loads(marker.read_text())['folder']
    return extract_browser(downloads)


def extract_compiler(downloads, build_tools):
    target = build_tools / 'inno-package'
    compiler = target / 'tools/ISCC.exe'
    if not compiler.is_file():
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(downloads / 'tools.innosetup.7.1.0.nupkg') as archive:
            archive.extractall(target)
    if not compiler.is_file():
        raise RuntimeError('Inno Setup compiler is missing')
    return compiler
