from django.core.exceptions import ValidationError

BINARY_SENTINEL = b'AutoCAD Binary DXF'


def validate_dxf(upload):
    if not upload.name.lower().endswith('.dxf'):
        raise ValidationError('Ожидается файл с расширением .dxf.')

    head = upload.read(1024)
    upload.seek(0)

    if head.startswith(BINARY_SENTINEL):
        return

    text = head.decode('utf-8', errors='ignore').lstrip('\ufeff').lstrip()
    code = text.split(maxsplit=1)[0] if text.split() else ''
    if code not in ('0', '999') or 'SECTION' not in text:
        raise ValidationError('Содержимое файла не похоже на DXF.')
