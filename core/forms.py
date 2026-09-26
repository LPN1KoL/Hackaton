from django import forms

from .validators import validate_dxf


class DxfUploadForm(forms.Form):
    file = forms.FileField(validators=[validate_dxf])


class ProcessUploadForm(DxfUploadForm):
    # необязательна: генплан часто уже содержит геоподоснову
    geobase = forms.FileField(required=False, validators=[validate_dxf])
