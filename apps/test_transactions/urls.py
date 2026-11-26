from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from . import views
from .forms import EmployeeAuthenticationForm


app_name = "test_transactions"

urlpatterns = [
    path("login/", LoginView.as_view(
        template_name="test_transactions/login.html", authentication_form=EmployeeAuthenticationForm,
    ), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("projects/", views.projects, name="projects"),
    path("projects/<uuid:project_id>/", views.projects, name="project"),
    path("projects/<uuid:project_id>/transactions/new/", views.create_transaction, name="create"),
    path("projects/<uuid:project_id>/transactions/<uuid:transaction_id>/",
         views.transaction_detail, name="detail"),
    path("projects/<uuid:project_id>/transactions/<uuid:transaction_id>/print/",
         views.print_transaction, name="print"),
    path("projects/<uuid:project_id>/transactions/<uuid:transaction_id>/approve/",
         views.approve_transaction, name="approve"),
    path("projects/<uuid:project_id>/transactions/<uuid:transaction_id>/attachments/<uuid:attachment_id>/",
         views.download_attachment, name="download"),
]
