from django import forms

from greening.recommend import PREFERENCES_MAX

from .validators import validate_dxf


class DxfUploadForm(forms.Form):
    file = forms.FileField(validators=[validate_dxf])


class ProcessUploadForm(DxfUploadForm):
    # необязательна: генплан часто уже содержит геоподоснову
    geobase = forms.FileField(required=False, validators=[validate_dxf])
    # пожелания к подбору растений — уходят в запрос LLM
    preferences = forms.CharField(required=False, max_length=PREFERENCES_MAX, strip=True)
    # места посадки: эвристика или модель, обученная на посадках проектировщиков; не указан — алгоритм
    placement = forms.ChoiceField(required=False, choices=(('algorithm', 'алгоритм'), ('model', 'модель')))


class ReviseForm(forms.Form):
    """Редактирование результата: пожелания к растениям (уровень 1) и/или параметры рассадки (уровень 2)."""
    preferences = forms.CharField(required=False, max_length=PREFERENCES_MAX, strip=True)
