from django.db import models


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACTIVE = "active", "Active"
        CLOSED = "closed", "Closed"

    customer = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    closed_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=200, null=True, blank=True)
