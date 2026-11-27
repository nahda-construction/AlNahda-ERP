from django.conf import settings
from django.http.request import split_domain_port


class HostURLConfMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        host, _ = split_domain_port(request.get_host())
        if host in settings.ERP_OWNER_HOSTS:
            request.urlconf = "config.owner_urls"
        response = self.get_response(request)
        if request.path_info.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response
