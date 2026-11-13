# -*- coding: utf-8 -*-
"""提取指定群最近 N 天的全部消息到文本文件（基于本地微信数据库，和 collect_emojis.py 同一套）。

用法:
    python extract_group.py Agent交流群          # 默认最近 3 天 -> <群名>_消息提取.txt
    python extract_group.py Agent交流群 --days 7
"""
import os
import sys
import time

DAYS = 3
OUT_DIR = r'D:\wechat-deepseek'


def main() -> None:
    args = [a for a in sys.argv[1:]]
    days = DAYS
    if '--days' in args:
        i = args.index('--days')
        days = int(args[i + 1])
        del args[i:i + 2]
    if not args:
        print(__doc__)
        return
    group_name = args[0]
    cutoff = time.time() - days * 86400

    from wechatauto.db import WeChatDB
    db = WeChatDB()

    # 定位群
    username = None
    for g in db.get_groups():
        nick = str(g.get('nickname') or g.get('remark') or '')
        if group_name in nick:
            username = g.get('username')
            print(f'匹配群: {nick} -> {username}')
            break
    if not username:
        for c in db.search_contact(group_name):
            u = c.get('username') or ''
            if u.endswith('@chatroom'):
                username = u
                print(f"搜索命中: {c.get('nickname')} -> {u}")
                break
    if not username:
        print(f'未找到群: {group_name}')
        return

    # 群成员 wxid -> 昵称（成员表里 nickname 是空的，逐个查联系人表）
    members = {}
    try:
        wxids = [m.get('username') or '' for m in db.get_group_members(username)]
    except Exception as e:
        print(f'群成员读取失败(不影响消息): {e}')
        wxids = []
    for w in wxids:
        if not w:
            continue
        try:
            members[w] = db.get_nickname(w) or w
        except Exception:
            members[w] = w
    print(f'成员昵称映射: {len(members)} 人')
    # 先摸清谁是"刀哥"
    for w, n in members.items():
        if '刀' in n:
            print(f'  疑似: {n} ({w})')

    # 翻页拉取
    rows = []
    offset = 0
    PAGE = 500
    CAP = 100000
    while offset < CAP:
        batch = db.get_messages(username, limit=PAGE, offset=offset)
        if not batch:
            break
        stop = False
        for r in batch:
            ts = r.get('create_time') or 0
            if ts < cutoff:
                stop = True
                break
            rows.append(r)
        if stop or len(batch) < PAGE:
            break
        offset += PAGE
    print(f'共取到最近 {days} 天消息 {len(rows)} 条')
    if rows:
        print('字段样例:', sorted(rows[0].keys()))

    # 落盘（升序）
    rows.sort(key=lambda r: (r.get('create_time') or 0,
                             r.get('local_id') or 0))
    out = os.path.join(OUT_DIR, f'{group_name}_消息提取.txt')
    with open(out, 'w', encoding='utf-8') as f:
        for r in rows:
            ts = time.strftime('%m-%d %H:%M:%S',
                               time.localtime(r.get('create_time') or 0))
            sender = r.get('sender_username') or ''
            disp = members.get(sender, sender or '?')
            typ = r.get('type') or ''
            content = str(r.get('content') or '').replace('\r', '')
            # 群消息正文自带 "wxid:" 前缀，去掉重复
            pfx = sender + ':'
            if content.startswith(pfx):
                content = content[len(pfx):].lstrip()
            if len(content) > 500:
                content = content[:500] + f'…[长文{len(content)}字]'
            f.write(f'[{ts}] {disp}({typ}): {content}\n')
    print(f'已写出: {out}')


if __name__ == '__main__':
    main()
