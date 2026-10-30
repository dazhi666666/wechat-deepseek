# -*- coding: utf-8 -*-
"""从指定会话的聊天记录里定向下载表情（消息类型 47）进 stickers。

原理：表情消息的 XML 里带 md5 和 cdnurl（腾讯表情 CDN 直链，无需登录态）。
按 md5 去重后按时间倒序列出，指定序号才下载——不全下。

用法：
    python collect_emojis.py filehelper            # 列出文件传输助手里的表情
    python collect_emojis.py filehelper --all      # 全部下载进 stickers/
    python collect_emojis.py filehelper 1 3        # 只下第 1、3 个
    python collect_emojis.py filehelper --all --out sticker_pool
"""

import json
import os
import re
import sys
import time
import urllib.request
from html import unescape

# 和机器人统一：只用仓库那一份 stickers（wechat-deepseek）
STICKER_DIR = r'D:\wechat-deepseek\stickers'
INDEX_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'emoji_index.json')
SCAN_PAGE = 500
SCAN_ROOM_CAP = 20000
LOOKBACK_DAYS = 30           # 列表回溯窗口

ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
EMOJI_TAG_RE = re.compile(r'<emoji\b[^>]*>', re.I)


def _ext(data: bytes) -> str:
    """按文件头判断表情格式；微信表情基本是 gif/png。"""
    if data[:3] == b'GIF':
        return 'gif'
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if data[:2] == b'\xff\xd8':
        return 'jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return ''


def scan(db, username: str) -> list:
    """扫一个会话里的表情消息，按 md5 去重、时间倒序返回。"""
    cutoff = time.time() - LOOKBACK_DAYS * 86400
    seen = {}
    offset = 0
    while offset < SCAN_ROOM_CAP:
        rows = db.get_messages(username, limit=SCAN_PAGE, offset=offset)
        if not rows:
            break
        older = False
        for r in rows:
            ts = r.get('create_time') or 0
            if not ts or ts < cutoff:
                older = True
                break
            if str(r.get('type')) not in ('表情', '动画表情', '47', 'emoji'):
                continue
            m = EMOJI_TAG_RE.search(str(r.get('content') or ''))
            if not m:
                continue
            a = dict(ATTR_RE.findall(m.group(0)))
            md5 = a.get('md5')
            # XML 属性里的 & 转义成了 &amp;，直接拿来请求会 400
            url = unescape(a.get('cdnurl') or '')
            if not md5 or not url:
                continue
            if md5 not in seen:
                seen[md5] = {'md5': md5, 'url': url, 'time': ts}
        if older:
            break
        offset += SCAN_PAGE
    return sorted(seen.values(), key=lambda e: -e['time'])


def download(entry: dict, path: str) -> str:
    """下载一个表情并按文件头落盘，返回实际文件名；失败返回空串。"""
    req = urllib.request.Request(
        entry['url'], headers={'User-Agent': 'Mozilla/5.0'})
    data = urllib.request.urlopen(req, timeout=20).read()
    ext = _ext(data)
    if not ext:
        return ''
    path = os.path.splitext(path)[0] + '.' + ext
    with open(path, 'wb') as f:
        f.write(data)
    return os.path.basename(path)


def main() -> None:
    args = [a for a in sys.argv[1:]]
    if not args:
        print(__doc__)
        return
    username = args.pop(0)
    out_dir = STICKER_DIR
    if '--out' in args:
        out_dir = args[args.index('--out') + 1]
        args.remove('--out')
        args.pop(args.index(out_dir))
    pick_all = '--all' in args
    picks = [int(a) for a in args if a.isdigit()]

    from wechatauto.db import WeChatDB
    entries = scan(WeChatDB(), username)
    if not entries:
        print(f'{username} 最近 {LOOKBACK_DAYS} 天没有表情消息')
        return
    print(f'{username}：{len(entries)} 个不重复表情（时间倒序）')
    for i, e in enumerate(entries, 1):
        t = time.strftime('%m-%d %H:%M', time.localtime(e['time']))
        print(f'  {i}. {e["md5"][:10]}  {t}')
    with open(INDEX_FILE, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)

    targets = range(1, len(entries) + 1) if pick_all else picks
    os.makedirs(out_dir, exist_ok=True)
    for n in targets:
        if not 1 <= n <= len(entries):
            continue
        try:
            name = download(entries[n - 1],
                            os.path.join(out_dir, f'表情{n}'))
        except Exception as e:
            print(f'  第 {n} 个下载失败：{type(e).__name__}: {e}')
            continue
        print(f'  已下载第 {n} 个 -> {os.path.join(out_dir, name)}'
              if name else f'  第 {n} 个下载失败（未知格式）')


if __name__ == '__main__':
    main()
