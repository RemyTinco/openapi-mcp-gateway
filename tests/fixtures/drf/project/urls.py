from django.urls import path
from rest_framework.routers import DefaultRouter

from orders_api.views import HealthView, OrderViewSet, ReportViewSet

router = DefaultRouter()
router.register("orders", OrderViewSet, basename="order")
router.register("reports", ReportViewSet, basename="report")

urlpatterns = [path("health/", HealthView.as_view())] + router.urls
