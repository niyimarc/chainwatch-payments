from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.django_tests.testapp"
    label = "testapp"
    default_auto_field = "django.db.models.BigAutoField"
