from rest_framework import serializers

from .models import Order


class ContactSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=30, allow_null=True, required=False)


class OrderSerializer(serializers.ModelSerializer):
    contact = ContactSerializer(required=False)

    class Meta:
        model = Order
        fields = ["id", "customer", "amount", "status", "closed_at", "note", "contact"]


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()


class ReportSerializer(serializers.Serializer):
    name = serializers.CharField()
    total = serializers.IntegerField()
