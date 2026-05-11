# payments/admin_forms.py
from django import forms
from payments.models import LevelConfig


class MissionGeneratorForm(forms.Form):
    level = forms.ModelChoiceField(
        queryset=LevelConfig.objects.all(),
        label="Mission Level"
    )

    title = forms.CharField(
        max_length=128,
        label="Mission Title"
    )

    image = forms.ImageField(
        label="Mission Image"
    )
