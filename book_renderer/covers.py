"""Local cover extraction inside the import worker; publication by the host."""
import base64
from io import BytesIO
import os
from pathlib import PurePosixPath
import posixpath
import re
import shutil
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from .audio import audio_dir
from .config import DISK_RESERVE
from .diagnostics import failure_details, record, record_failure
from .platform import checkpoint
from .storage import database, digest, fetch_book


COVER_NAME = "cover.jpg"
MAX_SOURCE_BYTES = 16 * 1024 ** 2
MAX_COVER_BYTES = 5 * 1024 ** 2
MAX_PIXELS = 20_000_000
COVER_SIZE = (1200, 1600)


def save_image(image, output):
    from PIL import Image, ImageOps
    if not 0 < image.width * image.height <= MAX_PIXELS:
        raise ValueError("Обложка превышает предел 20 млн пикселей.")
    image.seek(0)
    image.thumbnail(COVER_SIZE, Image.Resampling.LANCZOS)
    oriented = ImageOps.exif_transpose(image)
    try:
        oriented.thumbnail(COVER_SIZE, Image.Resampling.LANCZOS)
        rgba = oriented.convert("RGBA")
        try:
            rgb = Image.new("RGB", rgba.size, "white")
            try:
                with rgba.getchannel("A") as alpha:
                    rgb.paste(rgba, mask=alpha)
                target = output / COVER_NAME
                rgb.save(target, format="JPEG", quality=88, optimize=True)
                if not 0 < target.stat().st_size <= MAX_COVER_BYTES:
                    raise ValueError("Слишком большой файл обложки.")
                return {"file": COVER_NAME, "mime_type": "image/jpeg", "width": rgb.width,
                        "height": rgb.height, "sha256": digest(target)}
            finally:
                rgb.close()
        finally:
            rgba.close()
    finally:
        oriented.close()


def image_bytes(data, output):
    from PIL import Image
    if not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise ValueError("Размер исходной картинки обложки должен быть не больше 16 MiB.")
    with Image.open(BytesIO(data)) as image:
        if image.format not in ("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "JPEG2000"):
            raise ValueError("Формат картинки обложки не поддерживается.")
        return save_image(image, output)


def local_reference(base, href):
    reference = urlsplit(href or "")
    if reference.scheme or reference.netloc or not reference.path:
        raise ValueError("Обложка должна находиться внутри книги.")
    value = unquote(reference.path)
    if "\\" in value or "\x00" in value or PurePosixPath(value).is_absolute():
        raise ValueError("Недопустимая ссылка на обложку.")
    name = posixpath.normpath(posixpath.join(posixpath.dirname(base), value))
    if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
        raise ValueError("Обложка должна находиться внутри книги.")
    return name


def fb2_cover(source, output):
    from .extract import SafeZip, xml
    if source.name.lower().endswith(".zip"):
        archive = SafeZip(source)
        try:
            names = [name for name in archive.names if name.lower().endswith(".fb2")]
            if len(names) != 1:
                return None
            data = archive.read(names[0])
        finally:
            archive.close()
    else:
        if source.stat().st_size > 32 * 1024 ** 2:
            raise ValueError("Слишком большой FB2 для извлечения обложки.")
        data = source.read_bytes()
    root = xml(data)
    references = root.xpath("./*[local-name()='description']/*[local-name()='title-info']"
                            "/*[local-name()='coverpage']/*[local-name()='image']/@*[local-name()='href']")
    for href in references[:4]:
        if not href.startswith("#"):
            continue
        for binary in root.xpath("./*[local-name()='binary']"):
            if binary.get("id") != href[1:]:
                continue
            encoded = re.sub(r"\s+", "", binary.text or "")
            if len(encoded) > 4 * ((MAX_SOURCE_BYTES + 2) // 3):
                raise ValueError("Слишком большая встроенная обложка FB2.")
            return image_bytes(base64.b64decode(encoded, validate=True), output)
    return None


def epub_cover(source, output):
    from lxml import html
    from .extract import SafeZip, xml
    archive = SafeZip(source)
    try:
        container = xml(archive.read("META-INF/container.xml"))
        roots = container.xpath("//*[local-name()='rootfile']/@full-path")
        if not roots:
            return None
        package_name = roots[0]
        package = xml(archive.read(package_name))
        items = {item.get("id"): item for item in package.xpath("//*[local-name()='manifest']/*[local-name()='item']")}
        candidates = []
        for item in items.values():
            if "cover-image" in item.get("properties", "").split():
                candidates.append(item.get("href"))
        for identifier in package.xpath("//*[local-name()='metadata']/*[local-name()='meta' and @name='cover']/@content"):
            if identifier in items:
                candidates.append(items[identifier].get("href"))
        candidates.extend(package.xpath("//*[local-name()='guide']/*[local-name()='reference' and @type='cover']/@href"))
        tried = set()
        last_error = None
        for href in candidates[:12]:
            checkpoint()
            try:
                name = local_reference(package_name, href)
                if name in tried:
                    continue
                tried.add(name)
                data = archive.read(name)
                if name.lower().endswith((".xhtml", ".html", ".htm", ".svg", ".xml")):
                    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8"
                    page = html.fromstring(data, parser=html.HTMLParser(no_network=True, encoding=encoding))
                    images = page.xpath("//*[local-name()='img']/@src | //*[local-name()='image' or name()='svg:image']"
                                        "/@*[local-name()='href' or name()='xlink:href']")
                    for image_href in images[:8]:
                        try:
                            image_name = local_reference(name, image_href)
                            return image_bytes(archive.read(image_name), output)
                        except (OSError, ValueError, KeyError) as error:
                            last_error = error
                else:
                    return image_bytes(data, output)
            except (OSError, ValueError, KeyError) as error:
                last_error = error
        if last_error is not None:
            raise ValueError("Встроенная обложка EPUB недоступна или имеет неподдерживаемый формат.") from last_error
        return None
    finally:
        archive.close()


def extract_cover(source, kind, output, document):
    """An optional image failure never hides a successful text import."""
    if kind not in ("fb2", "fb2.zip", "epub"):
        return
    checkpoint()
    record("cover_extraction_started", format=kind)
    try:
        if kind in ("fb2", "fb2.zip"):
            cover = fb2_cover(source, output)
        else:
            cover = epub_cover(source, output)
        if cover:
            document["cover"] = cover
            record("cover_extraction_finished", width=cover["width"], height=cover["height"])
        else:
            record("cover_not_found", format=kind)
    except (MemoryError, InterruptedError):
        raise
    except Exception as error:
        try:
            (output / COVER_NAME).unlink(missing_ok=True)
        except OSError:
            pass  # Scratch cleanup must not fail a successful text extraction.
        reason = failure_details(error)["error"][:240]
        document.setdefault("warnings", []).append("Обложку извлечь не удалось; импорт текста продолжен. Причина: " + reason)
        record_failure("cover_extraction_failed", error, format=kind)
    checkpoint()


def stored_cover(book_id, connection=None):
    path = audio_dir(book_id, connection) / COVER_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_COVER_BYTES:
        raise ValueError("Недопустимый файл обложки.")
    return path


def visible_cover(book, connection=None):
    # Older imports saved the first scanned page as a cover. Keep the file on
    # disk, but don't present or export it as a real cover unless user supplied it.
    if book["format"] in ("pdf", "djvu", "djv") and not book.get("custom_cover"):
        return None
    return stored_cover(book["id"], connection)


def publish_cover(book_id, output, description, *, custom=False):
    if not description:
        return False
    if not custom and fetch_book(book_id).get("custom_cover"):
        return False
    source = output / COVER_NAME
    if (description.get("file") != COVER_NAME or source.is_symlink() or not source.is_file()
            or not 0 < source.stat().st_size <= MAX_COVER_BYTES or digest(source) != description.get("sha256")):
        raise ValueError("Обработчик вернул недопустимый файл обложки.")
    target = audio_dir(book_id)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(target).free < DISK_RESERVE + source.stat().st_size:
        raise OSError("Недостаточно места для обложки сверх резерва 5 GiB.")
    partial = target / (".cover-" + uuid4().hex + ".part")
    try:
        shutil.copyfile(source, partial)
        os.replace(partial, target / COVER_NAME)
        if custom:
            with database() as connection:
                connection.execute("UPDATE books SET custom_cover=1 WHERE id=?", (book_id,))
        return True
    finally:
        partial.unlink(missing_ok=True)
