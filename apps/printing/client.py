import os
from contextlib import suppress
from http.client import HTTPConnection, HTTPException
from socket import SHUT_RDWR
from threading import Timer
from time import monotonic
from urllib.parse import urlsplit

from .worker import MAX_HTML_BYTES, MAX_PDF_BYTES


REQUEST_TIMEOUT = 35


class PdfBusy(Exception):
    pass


class PdfUnavailable(Exception):
    pass


def _interrupt_request(sock):
    with suppress(OSError):
        sock.shutdown(SHUT_RDWR)


def render_pdf(markup: str) -> bytes:
    """Send application-rendered HTML to the configured internal printer."""
    body = markup.encode("utf-8")
    if not 0 < len(body) <= MAX_HTML_BYTES:
        raise PdfUnavailable("Invalid document size")

    try:
        endpoint = urlsplit(os.environ.get("ERP_PRINT_URL", ""))
        if (
            endpoint.scheme != "http" or not endpoint.hostname or endpoint.username is not None
            or endpoint.path != "/render" or endpoint.query or endpoint.fragment
        ):
            raise PdfUnavailable("Printer endpoint is not configured")
        connection = HTTPConnection(endpoint.hostname, endpoint.port, timeout=REQUEST_TIMEOUT)
        deadline = monotonic() + REQUEST_TIMEOUT
        timer = None
        try:
            connection.connect()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Printer request timed out")
            # Interrupt the socket even if the printer starts sending a partial response.
            timer = Timer(remaining, _interrupt_request, args=(connection.sock,))
            timer.daemon = True
            timer.start()
            connection.request("POST", endpoint.path, body=body, headers={
                "Content-Type": "text/html; charset=utf-8",
                "Accept": "application/pdf",
            })
            with connection.getresponse() as response:
                if response.status == 429:
                    raise PdfBusy
                if response.status != 200 or response.headers.get_content_type() != "application/pdf":
                    raise PdfUnavailable("Unexpected printer response")
                length = response.getheader("Content-Length", "")
                if not length.isascii() or not length.isdecimal() or response.getheader("Transfer-Encoding"):
                    raise PdfUnavailable("Invalid printer response length")
                size = int(length)
                if not 0 < size <= MAX_PDF_BYTES:
                    raise PdfUnavailable("Invalid printer response size")
                pdf = response.read(size)
                if len(pdf) != size or not pdf.startswith(b"%PDF-"):
                    raise PdfUnavailable("Invalid printer document")
                if monotonic() >= deadline:
                    raise TimeoutError("Printer request timed out")
                return pdf
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()
    except (HTTPException, OSError, ValueError) as exc:
        raise PdfUnavailable("Could not contact the printer") from exc
