"""Format adapters, executed only inside the worker's filesystem/network sandbox."""
import math
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
from urllib.parse import unquote, urlsplit
import zipfile

from lxml import etree, html

from .cleanup import clean_pages, visible_line
from .config import NORMALIZER_VERSION
from .platform import run_tool, tool
from .notes import clean_document_notes
from .diagnostics import record


MAX_XML = 32 * 1024 ** 2
MAX_TEXT = 20_000_000


def ocr_image(image, output):
    target = output / "ocr-text"
    run_tool([tool("tesseract"), str(image), str(target), "-l", "rus", "--psm", "3"],
                   check=True, timeout=120, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)
    text_path = target.with_suffix(".txt")
    if text_path.stat().st_size > MAX_XML:
        raise ValueError("Слишком большой результат OCR одной страницы.")
    text = text_path.read_text(encoding="utf-8")
    text_path.unlink()
    image.unlink()
    return text


def paged_document(source, pages, ocr_pages, options, kind, progress):
    progress("Очистка страниц " + kind, len(pages), len(pages))
    record("page_cleanup_started", format=kind, pages=len(pages), ocr_pages=len(ocr_pages))
    pages, excluded, cleanup = clean_pages(pages, options)
    text = "\n\n".join("\n".join(lines) for lines in pages)
    warnings = [kind + ": проверь порядок колонок, переносы, таблицы, формулы и исключённые строки."]
    if ocr_pages:
        warnings.append("Применён русский OCR к страницам: " + ", ".join(map(str, ocr_pages[:100])))
    if cleanup["removed_lines"]:
        warnings.append(f"Очистка исключила {cleanup['removed_lines']} строк, целых страниц: {cleanup['removed_pages']}. "
                        "Причины и текст доступны в отчёте; очистку можно отключить и извлечь текст заново.")
    record("page_cleanup_finished", removed_lines=cleanup["removed_lines"], removed_pages=cleanup["removed_pages"])
    progress("Создание глав " + kind, len(pages), len(pages))
    chapters = text_chapters(text, "Начало книги")
    record("chapters_created", chapters=len(chapters))
    return {"title": source.stem, "author": "", "chapters": chapters,
            "warnings": warnings, "excluded": excluded, "ocr_pages": ocr_pages, "cleanup": cleanup}


def clean(value):
    value = visible_line(value)
    value = value.replace("\u00ad", "").replace("\u00a0", " ")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
    return re.sub(r"[ \t]+", " ", value).strip()


def local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def plain(node):
    parts = [node.text or ""]
    for child in node:
        note = child.get("type") == "note" or child.get("{http://www.w3.org/1999/xlink}type") == "note"
        if local(child) not in ("binary", "image", "script", "style") and not note:
            parts.append(plain(child))
        parts.append(child.tail or "")
    return "".join(parts)


def xml(data):
    if len(data) > MAX_XML or re.search(rb"<!ENTITY|<!DOCTYPE[^>]*\[", data, re.I):
        raise ValueError("XML слишком велик или содержит запрещённый DTD/entities.")
    parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True, huge_tree=False)
    return etree.fromstring(data, parser)


class SafeZip:
    def __init__(self, source):
        self.archive = zipfile.ZipFile(source)
        infos = self.archive.infolist()
        if len(infos) > 10000 or sum(item.file_size for item in infos) > 500 * 1024 ** 2:
            raise ValueError("Превышены ограничения архива.")
        self.names = set()
        for item in infos:
            path = PurePosixPath(item.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in item.filename or "\x00" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                    or item.file_size > MAX_XML or item.filename in self.names):
                raise ValueError("Небезопасная структура или слишком большой файл внутри архива.")
            self.names.add(item.filename)

    def read(self, name):
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Неверный путь внутри книги.")
        with self.archive.open(name) as source:
            data = source.read(MAX_XML + 1)
        if len(data) > MAX_XML:
            raise ValueError("Превышен фактический размер файла внутри архива.")
        return data

    def close(self):
        self.archive.close()


def text_chapters(text, title):
    lines = text.splitlines()
    chapters = []
    current = []
    paragraph = []
    heading = title

    def flush():
        nonlocal current
        value = "\n\n".join(part for part in current if part.strip()).strip()
        if value:
            chapters.append({"title": heading[:200], "text": value})
        current = []

    def flush_paragraph():
        if paragraph:
            current.append(" ".join(paragraph))
            paragraph.clear()

    for raw in lines:
        line = clean(raw)
        if not line:
            flush_paragraph()
            continue
        is_heading = len(line) <= 160 and re.match(r"^(?:глава|часть|раздел|введение|заключение|эпилог|пролог)(?:\s|[.:]|$)", line, re.I)
        if is_heading:
            flush_paragraph()
            flush()
            heading = line
            current.append(line)
            continue
        elif sum(len(part) for part in current) + sum(map(len, paragraph)) > 30000:
            flush_paragraph()
            flush()
            heading = "Часть " + str(len(chapters) + 1) + " · без оглавления"
        paragraph.append(line)
    flush_paragraph()
    flush()
    return chapters


def fb2(source, options):
    archive = None
    if source.name.endswith(".zip"):
        archive = SafeZip(source)
        names = [name for name in archive.names if name.lower().endswith(".fb2")]
        if len(names) != 1:
            raise ValueError("В FB2.zip должна быть ровно одна FB2-книга.")
        data = archive.read(names[0])
        archive.close()
    else:
        if source.stat().st_size > MAX_XML:
            raise ValueError("FB2 превышает 32 MiB текста/XML.")
        data = source.read_bytes()
    root = xml(data)
    if local(root) != "FictionBook":
        raise ValueError("Файл не является FB2.")
    titles = root.xpath("./*[local-name()='description']/*[local-name()='title-info']")
    title_info = titles[0] if titles else None
    title = ""
    author = ""
    if title_info is not None:
        title = " ".join(clean(plain(node)) for node in title_info if local(node) == "book-title")
        author = ", ".join(" ".join(clean(plain(part)) for part in node if local(part) in ("first-name", "middle-name", "last-name"))
                           for node in title_info if local(node) == "author")
    chapters = []

    def section(node, fallback):
        headings = [clean(plain(child)) for child in node if local(child) == "title"]
        heading = " ".join(headings).strip() or fallback
        paragraphs = []

        def flush():
            if paragraphs:
                chapters.append({"title": heading[:200], "text": "\n\n".join(paragraphs)})
                paragraphs.clear()

        for child in node:
            tag = local(child)
            if tag == "section":
                flush()
                section(child, heading)
            elif tag not in ("image", "binary"):
                value = clean(plain(child))
                if value:
                    paragraphs.append(value)
        flush()

    for body in root:
        if local(body) == "body" and (body.get("name") != "notes" or options.get("include_notes")):
            section(body, "Примечания" if body.get("name") == "notes" else title or "Книга")
    return {"title": title, "author": author, "chapters": chapters, "warnings": []}


def epub(source, options):
    archive = SafeZip(source)
    try:
        container = xml(archive.read("META-INF/container.xml"))
        roots = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not roots:
            raise ValueError("В EPUB отсутствует package document.")
        package_name = roots[0]
        package = xml(archive.read(package_name))
        items = {node.get("id"): node.get("href") for node in package.xpath("//*[local-name()='manifest']/*[local-name()='item']")}
        title = " ".join(package.xpath("//*[local-name()='metadata']/*[local-name()='title']/text()"))
        author = ", ".join(package.xpath("//*[local-name()='metadata']/*[local-name()='creator']/text()"))
        chapters = []
        for reference in package.xpath("//*[local-name()='spine']/*[local-name()='itemref']"):
            if reference.get("linear") == "no":
                continue
            href = urlsplit(items.get(reference.get("idref"), ""))
            if href.scheme or href.netloc or not href.path:
                raise ValueError("Недопустимая ссылка в EPUB spine.")
            name = posixpath.normpath(posixpath.join(posixpath.dirname(package_name), unquote(href.path)))
            page_data = archive.read(name)
            # XHTML in EPUB defaults to UTF-8; HTML's legacy Latin-1 default
            # corrupts Cyrillic when a page omits an explicit charset.
            page_encoding = "utf-16" if page_data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
            page = html.fromstring(page_data, parser=html.HTMLParser(no_network=True, encoding=page_encoding))
            for node in list(page.xpath("//script|//style|//nav")):
                node.drop_tree()
            if not options.get("include_notes"):
                for node in list(page.xpath("//*[@role='doc-footnote' or @*[local-name()='type']='footnote']")):
                    node.drop_tree()
            blocks = []
            for node in page.iter():
                if local(node) in ("p", "h1", "h2", "h3", "h4", "li", "pre"):
                    if any(local(child) in ("p", "li", "h1", "h2", "h3", "h4") for child in node.iterdescendants()):
                        continue
                    value = clean(plain(node))
                    if value:
                        blocks.append(value)
            text = "\n\n".join(blocks) or clean(plain(page))
            if text:
                headings = page.xpath("//h1|//h2|//h3")
                heading = clean(plain(headings[0])) if headings else "Раздел " + str(len(chapters) + 1)
                chapters.append({"title": heading[:200], "text": text})
        return {"title": title, "author": author, "chapters": chapters,
                "warnings": ["EPUB прочитан по spine. Проверь служебные разделы, таблицы и сноски перед рендером."]}
    finally:
        archive.close()


def docx(source, options):
    from docx import Document
    from docx.text.paragraph import Paragraph
    archive = SafeZip(source)
    warnings = []
    try:
        main_xml = archive.read("word/document.xml")
        xml(main_xml)
        if b"<w:ins" in main_xml or b"<w:del" in main_xml:
            warnings.append("В Word есть исправления Track Changes: их извлечение может быть неполным.")
    finally:
        archive.close()
    document = Document(str(source))
    chapters = []
    title = document.core_properties.title or source.stem
    heading = title
    paragraphs = []

    def flush():
        if paragraphs:
            chapters.append({"title": heading[:200], "text": "\n\n".join(paragraphs)})
            paragraphs.clear()

    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            value = clean(block.text)
            style = block.style.style_id if block.style else ""
            if re.match(r"Heading[12]$", style) and value:
                flush()
                heading = value
            if value:
                paragraphs.append(value)
        else:
            paragraphs.append("Здесь находится таблица. При необходимости посмотри её в исходной книге.")
            warnings.append("Таблица Word не озвучивается построчно.")
    flush()
    return {"title": title, "author": document.core_properties.author or "", "chapters": chapters,
            "warnings": list(dict.fromkeys(warnings))}


def pdf(source, options, output, progress):
    import pdfplumber
    import pypdfium2
    pages = []
    ocr_pages = []
    with pdfplumber.open(source) as document:
        if len(document.pages) > 2000:
            raise ValueError("PDF превышает 2000 страниц.")
        renderer = None
        try:
            for number, page in enumerate(document.pages, 1):
                progress("Извлечение PDF", number - 1, len(document.pages))
                text = page.extract_text() or ""
                useful = sum(character.isalpha() for character in text)
                large_image = any((image.get("width", 0) * image.get("height", 0)) > page.width * page.height * 0.5
                                  for image in page.images)
                if options.get("force_ocr") or useful < 40 and page.images or large_image and useful < 150:
                    if renderer is None:
                        renderer = pypdfium2.PdfDocument(str(source))
                    rendered_page = renderer[number - 1]
                    width, height = rendered_page.get_size()
                    low_memory = os.name == 'nt' and os.environ.get('ZVUKBOOK_LOW_MEMORY') == '1'
                    scale = min((160 if low_memory else 250) / 72,
                                math.sqrt((6_000_000 if low_memory else 20_000_000) / max(width * height, 1)))
                    bitmap = rendered_page.render(scale=scale)
                    image = bitmap.to_pil()
                    image.save(output / "ocr-page.png")
                    image.close()
                    bitmap.close()
                    rendered_page.close()
                    progress("OCR страницы " + str(number), number - 1, len(document.pages))
                    text = ocr_image(output / "ocr-page.png", output)
                    ocr_pages.append(number)
                pages.append([clean(line) for line in text.splitlines() if clean(line)])
                page.flush_cache()
                if sum(sum(len(line) for line in lines) for lines in pages) > MAX_TEXT:
                    raise ValueError("Превышен предел объёма текста.")
        finally:
            if renderer is not None:
                renderer.close()
    return paged_document(source, pages, ocr_pages, options, "PDF", progress)


def djvu(source, options, output, progress):
    if not Path(tool("ddjvu")).is_file() or not Path(tool("djvused")).is_file():
        raise ValueError("Для DJVU нужен системный пакет djvulibre-bin. См. инструкцию запуска.")
    count = run_tool([tool("djvused"), str(source), "-e", "n"], check=True, timeout=30,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        total = int(count.stdout.strip())
    except ValueError as error:
        raise ValueError("Не удалось определить число страниц DJVU.") from error
    if not 1 <= total <= 2000:
        raise ValueError("В DJVU должно быть от 1 до 2000 страниц.")
    pages, ocr_pages = [], []
    text_size = 0
    for number in range(1, total + 1):
        progress("Извлечение DJVU: страница " + str(number), number - 1, total)
        text = ""
        if not options.get("force_ocr"):
            # Use an output file rather than unbounded subprocess PIPE for text.
            layer = output / "djvu-layer.txt"
            with layer.open("wb") as target:
                run_tool([tool("djvused"), "-u", str(source), "-e", f"select {number}; print-pure-txt"],
                               check=True, timeout=30, stdin=subprocess.DEVNULL, stdout=target, stderr=subprocess.PIPE)
            if layer.stat().st_size > MAX_XML:
                raise ValueError("Слишком большой текстовый слой DJVU одной страницы.")
            text = layer.read_text(encoding="utf-8", errors="replace")
            layer.unlink()
        letters = [character for character in text if character.isalpha()]
        russian = sum("а" <= character.casefold() <= "я" or character.casefold() == "ё" for character in letters)
        if options.get("force_ocr") or len(letters) < 40 or russian / max(len(letters), 1) < 0.4:
            image = output / "ocr-page.pgm"
            progress("OCR DJVU: страница " + str(number), number - 1, total)
            # Exact page, grayscale, bounded dimensions, preserved aspect ratio.
            size = '-size=2000x2600' if os.name == 'nt' and os.environ.get('ZVUKBOOK_LOW_MEMORY') == '1' else '-size=3500x4500'
            run_tool([tool("ddjvu"), "-format=pgm", f"-page={number}", size, "-aspect=yes",
                            str(source), str(image)], check=True, timeout=90, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            text = ocr_image(image, output)
            ocr_pages.append(number)
        lines = [clean(line) for line in text.splitlines() if clean(line)]
        text_size += sum(map(len, lines))
        if text_size > MAX_TEXT:
            raise ValueError("Превышен предел объёма текста DJVU.")
        pages.append(lines)
    return paged_document(source, pages, ocr_pages, options, "DJVU", progress)


def extract(source, kind, options, output, progress, *, include_cover=True):
    record("format_adapter_started", format=kind)
    if kind == "txt":
        data = source.read_bytes()
        if len(data) > MAX_XML:
            raise ValueError("TXT превышает 32 MiB.")
        encoding = options.get("encoding", "utf-8-sig")
        if encoding not in ("utf-8-sig", "cp1251"):
            raise ValueError("Неизвестная кодировка.")
        result = {"title": "", "author": "", "chapters": text_chapters(data.decode(encoding), "Книга"),
                  "warnings": ["TXT: главы определены по явным заголовкам; при их отсутствии созданы части."]}
    elif kind in ("fb2", "fb2.zip"):
        result = fb2(source, options)
    elif kind == "epub":
        result = epub(source, options)
    elif kind == "docx":
        result = docx(source, options)
    elif kind == "doc":
        from .config import WINDOWS
        if WINDOWS:
            raise ValueError("DOC пока не поддерживается в Windows. Используй DOCX.")
        profile_root = Path("/tmp/lo-profile")
        profile = profile_root / "user"
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse">'
            '<value>3</value></prop></item></oor:items>', encoding="utf-8")
        converted = output / "converted"
        converted.mkdir()
        run_tool([tool("libreoffice"), "-env:UserInstallation=" + profile_root.as_uri(), "--headless", "--convert-to", "docx",
                        "--outdir", str(converted), str(source)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, check=True, timeout=90)
        result = docx(converted / (source.stem + ".docx"), options)
        result["warnings"].append("DOC преобразован LibreOffice. Проверь полноту текста и глав.")
    elif kind == "pdf":
        result = pdf(source, options, output, progress)
    elif kind in ("djvu", "djv"):
        result = djvu(source, options, output, progress)
    else:
        raise ValueError("Формат не поддерживается.")
    progress("Очистка примечаний", 1, 1)
    record("note_cleanup_started")
    clean_document_notes(result, options.get("remove_editor_notes", True))
    record("note_cleanup_finished")
    progress("Проверка глав", 1, 1)
    chapters = result["chapters"]
    if not chapters or len(chapters) > 2000 or sum(len(chapter["text"]) for chapter in chapters) > MAX_TEXT:
        raise ValueError("Не получен читаемый текст или превышен предел книги.")
    for index, chapter in enumerate(chapters, 1):
        chapter["index"] = index
        chapter["title"] = clean(chapter["title"])[:200] or "Глава " + str(index)
    result.update(schema="projectpro-local-book-v1", normalizer_version=NORMALIZER_VERSION,
                  import_options=options)
    if include_cover and kind in ("fb2", "fb2.zip", "epub"):
        from .covers import extract_cover
        progress("Извлечение обложки", 1, 1)
        extract_cover(source, kind, output, result)
    record("document_validated", chapters=len(chapters))
    return result
