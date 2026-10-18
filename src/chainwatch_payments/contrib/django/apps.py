from django.apps import AppConfig


class ChainwatchConfig(AppConfig):
    name = "chainwatch_payments.contrib.django"
    label = "chainwatch"
    verbose_name = "Chainwatch payments"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        from django.core.signals import setting_changed

        from . import conf

        def _reset(setting, **kwargs):
            if setting == "CHAINWATCH":
                conf.reset()

        setting_changed.connect(_reset, weak=False, dispatch_uid="chainwatch_reset_conf")
