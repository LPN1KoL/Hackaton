"""python -m greening ГЕНПЛАН.dxf [--geobase ОСНОВА.dxf] [--out ПАПКА] [--overlay]"""

import argparse
import json
import sys
from pathlib import Path

from . import export, parse


def main():
    parser = argparse.ArgumentParser(description='Разметка генплана: территория, поверхности, структуры, секции зелёных зон')
    parser.add_argument('plan', help='генплан, DXF')
    parser.add_argument('--geobase', help='геоподоснова, DXF (необязательно)')
    parser.add_argument('--out', default='.', help='папка для результата')
    parser.add_argument('--overlay', action='store_true', help='слои разметки поверх копии генплана, а не отдельным файлом')
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(args.plan).stem
    result = parse(args.plan, args.geobase, log=lambda message: print(message, file=sys.stderr, flush=True))

    geojson = out / f'{stem}.greening.geojson'
    dxf = out / f'{stem}.greening.dxf'
    export.write_geojson(result, geojson)
    export.write_dxf(result, dxf, base=args.plan if args.overlay else None)
    print(json.dumps({'geojson': str(geojson), 'dxf': str(dxf), 'summary': result.summary,
                      'timings_s': result.timings}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
