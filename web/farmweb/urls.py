from django.urls import path

from solverapp import views

urlpatterns = [
    path("", views.index, name="index"),
    path("api/solve", views.api_solve, name="api_solve"),
]
