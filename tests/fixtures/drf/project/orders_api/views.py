from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Order
from .serializers import HealthSerializer, OrderSerializer, ReportSerializer


class HealthView(APIView):
    authentication_classes: list[type] = []
    permission_classes: list[type] = []

    @extend_schema(responses=HealthSerializer)
    def get(self, request):
        return Response({"status": "ok"})


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                "status",
                str,
                enum=["pending", "active", "closed"],
                description="Only orders in this status.",
            )
        ]
    )
)
class OrderViewSet(viewsets.ModelViewSet):
    queryset = Order.objects.all()
    serializer_class = OrderSerializer


class ReportViewSet(viewsets.ViewSet):
    @extend_schema(responses=ReportSerializer(many=True))
    def list(self, request):
        return Response([])
