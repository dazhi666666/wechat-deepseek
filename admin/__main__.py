# -*- coding: utf-8 -*-
"""python -m admin [--port 8765] [--no-browser]"""

import argparse
import threading
import webbrowser

from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description='大肥鱼管理后台')
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    args = ap.parse_args()
    if not args.no_browser:
        threading.Timer(0.8, webbrowser.open, (f'http://127.0.0.1:{args.port}',)).start()
    serve(port=args.port)


if __name__ == '__main__':
    main()
