from django.urls import path

from solverapp import views

urlpatterns = [
    path("", views.index, name="index"),
    path("masteries", views.masteries, name="masteries"),
    path("timeline", views.timeline, name="timeline"),
    path("api/catalogue", views.api_catalogue, name="api_catalogue"),
    path("api/solve", views.api_solve, name="api_solve"),
    path("api/mastery-costs", views.api_mastery_costs, name="api_mastery_costs"),
]
