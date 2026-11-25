import hashlib
import os
import re
import stat
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import UploadedFile
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


MAX_PDF_BYTES = 5 * 1024 * 1024
PDF_MEDIA_TYPE = "application/pdf"
PDF_SIGNATURE = b"%PDF-"
STORAGE_KEY = re.compile(r"[0-9a-f]{32}\.pdf")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class PrivateStorageError(Exception):
    pass


def _check_private_directory(fd):
    info = os.fstat(fd)
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PrivateStorageError("Private storage requires an owner-only directory.")


@contextmanager
def _private_root():
    path = Path(settings.ERP_PRIVATE_ROOT or "")
    if not path.is_absolute():
        raise PrivateStorageError("Private storage requires an absolute directory.")
    try:
        resolved_path = path.resolve()
        for public_root in (settings.MEDIA_ROOT, settings.STATIC_ROOT):
            if not public_root:
                continue
            public_path = Path(public_root).resolve()
            if resolved_path.is_relative_to(public_path) or public_path.is_relative_to(resolved_path):
                raise PrivateStorageError("Private storage overlaps a public directory.")
        fd = os.open(path, DIRECTORY_FLAGS)
    except (OSError, ValueError, RuntimeError) as exc:
        raise PrivateStorageError("Private storage directory is unavailable.") from exc
    try:
        _check_private_directory(fd)
        yield fd
    finally:
        os.close(fd)


@dataclass(frozen=True)
class _TemporaryFile:
    root_fd: int
    directory_fd: int
    name: str
    stream: BinaryIO


@contextmanager
def _temporary_file():
    with _private_root() as root_fd:
        try:
            os.mkdir("uploads", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        directory_fd = os.open("uploads", DIRECTORY_FLAGS, dir_fd=root_fd)
        try:
            _check_private_directory(directory_fd)
            name = f"{uuid.uuid4().hex}.tmp"
            fd = os.open(
                name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                mode=0o600, dir_fd=directory_fd,
            )
            try:
                with os.fdopen(fd, "w+b") as stream:
                    yield _TemporaryFile(root_fd, directory_fd, name, stream)
            finally:
                os.unlink(name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)


class RejectFileUploadHandler(FileUploadHandler):
    def new_file(self, *args, **kwargs):
        self.request.unexpected_file_upload = True
        raise StopUpload(connection_reset=True)


class _HTTPUpload(UploadedFile):
    def __init__(self, temporary, **kwargs):
        super().__init__(file=temporary.stream, **kwargs)
        self.temporary = temporary


class PrivatePDFUploadHandler(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.error = None
        self.file = None
        self._temporary = None
        self._files = ExitStack()

    def new_file(self, *args, **kwargs):
        if self.file is not None:
            self.error = ValidationError("يُسمح بمرفق PDF واحد.")
            raise StopUpload(connection_reset=True)
        super().new_file(*args, **kwargs)
        try:
            self._temporary = self._files.enter_context(_temporary_file())
            self.file = self._temporary.stream
        except (OSError, PrivateStorageError):
            self.error = PrivateStorageError("Private upload staging is unavailable.")
            raise StopUpload(connection_reset=True)

    def receive_data_chunk(self, raw_data, start):
        if start + len(raw_data) > MAX_PDF_BYTES:
            self.error = ValidationError("الحد الأقصى لحجم المرفق 5 MiB.")
            raise StopUpload(connection_reset=True)
        try:
            self.file.write(raw_data)
        except OSError:
            self.error = PrivateStorageError("Private upload staging failed.")
            raise StopUpload(connection_reset=True)

    def file_complete(self, file_size):
        self.file.seek(0)
        return _HTTPUpload(
            self._temporary, name=self.file_name, content_type=self.content_type,
            size=file_size, charset=self.charset, content_type_extra=self.content_type_extra,
        )

    def close(self):
        self._files.close()


def _pdf_display_name(uploaded_file):
    name = unicodedata.normalize("NFKC", uploaded_file.name or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    stem, extension = os.path.splitext(name.strip())
    if not stem.strip() or extension.lower() != ".pdf" or uploaded_file.content_type != PDF_MEDIA_TYPE:
        raise ValidationError("اختر ملف PDF صالحًا.")
    return stem[:251] + extension


@dataclass(frozen=True)
class StagedPDF:
    temporary: _TemporaryFile
    original_name: str
    size_bytes: int
    sha256: str


@contextmanager
def stage_pdf(uploaded_file):
    original_name = _pdf_display_name(uploaded_file)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with ExitStack() as files:
            if isinstance(uploaded_file, _HTTPUpload):
                temporary = uploaded_file.temporary
            else:
                temporary = files.enter_context(_temporary_file())
            for data in uploaded_file.chunks(chunk_size=64 * 1024):
                size_bytes += len(data)
                if size_bytes > MAX_PDF_BYTES:
                    raise ValidationError("الحد الأقصى لحجم المرفق 5 MiB.")
                digest.update(data)
                if not isinstance(uploaded_file, _HTTPUpload):
                    temporary.stream.write(data)
            temporary.stream.seek(0)
            if temporary.stream.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
                raise ValidationError("اختر ملف PDF صالحًا.")
            yield StagedPDF(temporary, original_name, size_bytes, digest.hexdigest())
    except OSError as exc:
        raise PrivateStorageError("Preparing the private attachment failed.") from exc


def publish(staged_file):
    key = f"{uuid.uuid4().hex}.pdf"
    temporary = staged_file.temporary
    try:
        temporary.stream.flush()
        os.fsync(temporary.stream.fileno())
        os.link(
            temporary.name, key, src_dir_fd=temporary.directory_fd,
            dst_dir_fd=temporary.root_fd, follow_symlinks=False,
        )
        os.fsync(temporary.root_fd)
    except OSError as exc:
        raise PrivateStorageError("Installing the private attachment failed.") from exc
    return key


def open_private_file(key):
    if not STORAGE_KEY.fullmatch(key):
        raise PrivateStorageError("Invalid private storage key.")
    try:
        with _private_root() as root_fd:
            fd = os.open(key, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root_fd)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) & 0o077
                ):
                    raise PrivateStorageError("Invalid private attachment file.")
                return os.fdopen(fd, "rb")
            except BaseException:
                os.close(fd)
                raise
    except OSError as exc:
        raise PrivateStorageError("Private attachment file is unavailable.") from exc
