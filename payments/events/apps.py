# events/apps.py

from django.apps import AppConfig


class EventsConfig(AppConfig):
    name = "payments.events"

    def ready(self):
        from . import signals
