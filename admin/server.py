# -*- coding: utf-8 -*-
"""「大肥鱼」管理后台：纯标准库 HTTP 服务 + 单页前端。

功能：机器人进程启停、运行状态、今日统计（解析 dafeiyu_bot.log）、
对话记录、实时日志、配置编辑（写 bot_config.json，机器人启动时覆盖常量）、
表情包管理、LLM 节点探测与链路测试。

只监听 127.0.0.1：页面能看到 API Key、能动键鼠，绝不暴露到局域网。

用法（仓库根目录）：
    python -m admin                 # 默认 http://127.0.0.1:8765
    python -m admin --port 9000
"""

import ast
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
BOT_SCRIPT = os.path.join(ROOT, 'dafeiyu_bot.py')
BOT_LOG = os.path.join(ROOT, 'dafeiyu_bot.log')
BOT_STDERR = os.path.join(ROOT, 'dafeiyu_bot.stderr.log')
CONFIG_FILE = os.path.join(ROOT, 'bot_config.json')
START_CMD = os.path.join(ROOT, 'start_bot.cmd')
STICKER_DIR = os.path.join(ROOT, 'stickers')
STICKER_EXTS = ('.gif', '.jpg', '.jpeg', '.png', '.webp', '.bmp')

# 管理页可编辑的配置项：键名与 dafeiyu_bot._OVERRIDABLE 保持一致
CONFIG_KEYS = ('API_HOSTS', 'API_PORT', 'API_KEY', 'MODEL', 'AT_NAMES',
               'RECENT_N', 'MAX_TOKENS', 'LLM_TIMEOUT', 'TOOL_ROUNDS',
               'SEARCH_DAYS', 'CONTEXT_TOKEN_LIMIT', 'CHATGPT_COOLDOWN',
               'CHATGPT_TIMEOUT', 'SEND_RETRIES', 'SYSTEM_PROMPT', 'FAIL_REPLY')
_ENV_KEYS = {'WECHAT_DEEPSEEK_API_HOSTS': 'API_HOSTS',
             'WECHAT_DEEPSEEK_API_PORT': 'API_PORT',
             'WECHAT_DEEPSEEK_API_KEY': 'API_KEY',
             'WECHAT_DEEPSEEK_MODEL': 'MODEL'}

_LINE_RE = re.compile(r'^\[(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\] (.*)$')
_TAG_RE = re.compile(r'^\[([^\]]+)\] ?(.*)$')
_RECV_RE = re.compile(r'^(?:<([^>]*)> )?(.*?) \| (.*?): (.*)$')   # 可选 <Agent> 前缀
_REPLY_RE = re.compile(r'^\(([\d.]+)s\) (.*)$')

_proc_lock = threading.Lock()


# ------------------------- 配置 -------------------------

def _source_defaults() -> dict:
    """从 dafeiyu_bot.py 源码里静态读出常量默认值（不 import，免得拉起 wechatauto）。"""
    out = {}
    try:
        with open(BOT_SCRIPT, encoding='utf-8') as f:
            tree = ast.parse(f.read())
    except Exception:
        return out
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id in CONFIG_KEYS:
            try:
                out[node.targets[0].id] = ast.literal_eval(node.value)
            except Exception:
                pass                    # API_HOSTS 等表达式型的跳过，走环境变量
    out.setdefault('API_HOSTS', ['127.0.0.1'])
    out.setdefault('API_PORT', 8000)
    out.setdefault('API_KEY', 'EMPTY')
    out.setdefault('MODEL', 'deepseek-v4')
    return out


def _start_cmd_env() -> dict:
    """解析 start_bot.cmd 里的 `set WECHAT_DEEPSEEK_*=...` 当初始值。"""
    out = {}
    try:
        with open(START_CMD, encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'\s*set\s+(\w+)=(.*)$', line, re.I)
                if m and m.group(1) in _ENV_KEYS:
                    out[_ENV_KEYS[m.group(1)]] = m.group(2).strip()
    except OSError:
        pass
    if 'API_HOSTS' in out:
        out['API_HOSTS'] = [h.strip() for h in out['API_HOSTS'].split(',') if h.strip()]
    if str(out.get('API_PORT', '')).isdigit():
        out['API_PORT'] = int(out['API_PORT'])
    return out


def load_config() -> dict:
    """生效配置 = 源码默认值 < start_bot.cmd 环境变量 < bot_config.json。"""
    cfg = _source_defaults()
    cfg.update(_start_cmd_env())
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in CONFIG_KEYS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(data: dict) -> dict:
    cfg = load_config()
    for k in CONFIG_KEYS:
        if k not in data:
            continue
        v = data[k]
        if k in ('API_HOSTS', 'AT_NAMES'):
            if isinstance(v, str):
                v = re.split(r'[,\s]+', v)
            v = [str(h).strip() for h in v if str(h).strip()]
        elif isinstance(cfg.get(k), int) or k in ('API_PORT',):
            v = int(v)
        else:
            v = str(v)
        cfg[k] = v
    raw = _read_raw_config()
    raw.update(cfg)                     # 保留 AGENTS 等非常量键
    _write_raw_config(raw)
    return cfg


def _read_raw_config() -> dict:
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_raw_config(raw: dict) -> None:
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ------------------------- 多群 Agent -------------------------

TOOL_NAMES = ('search_group_messages', 'get_ai_news', 'send_sticker',
              'say_to_group', 'ask_chatgpt', 'chatgpt_image')
KNOWN_CHATS_FILE = os.path.join(ROOT, 'known_chats.json')
USAGE_FILE = os.path.join(ROOT, 'usage.jsonl')


def load_agents() -> list:
    """bot_config.json 的 AGENTS 原样返回；没配时给一个绑定 "*" 的默认 Agent。"""
    agents = _read_raw_config().get('AGENTS') or []
    if not agents:
        agents = [{'id': 'default', 'name': load_config().get('AT_NAME', '大肥鱼'),
                   'enabled': True, 'at_name': '', 'groups': ['*'],
                   'system_prompt': '', 'model': '', 'temperature': 0.7,
                   'max_tokens': '', 'recent_n': '', 'tools': list(TOOL_NAMES)}]
    return agents


def save_agents(agents: list) -> list:
    if not isinstance(agents, list) or not agents:
        raise ValueError('至少保留一个 Agent')
    seen, out = set(), []
    for i, a in enumerate(agents):
        aid = re.sub(r'[^\w-]', '', str(a.get('id') or '')) or f'agent{int(time.time())}{i}'
        while aid in seen:
            aid += '_'
        seen.add(aid)
        name = str(a.get('name') or '').strip()
        if not name:
            raise ValueError(f'第 {i + 1} 个 Agent 没有名字')
        num = lambda k, f: f(a[k]) if str(a.get(k, '')).strip() not in ('', 'None') else ''
        out.append({
            'id': aid, 'name': name, 'enabled': bool(a.get('enabled', True)),
            'at_name': str(a.get('at_name') or '').strip(),
            'groups': [str(g).strip() for g in a.get('groups') or [] if str(g).strip()],
            'system_prompt': str(a.get('system_prompt') or ''),
            'model': str(a.get('model') or '').strip(),
            'temperature': num('temperature', float), 'max_tokens': num('max_tokens', int),
            'recent_n': num('recent_n', int),
            'tools': list(TOOL_NAMES) if a.get('tools') is None
            else [t for t in a['tools'] if t in TOOL_NAMES],
        })
    # 同一个群绑给多个启用的 Agent 会让路由取决于顺序，提前拦下
    owner = {}
    for a in out:
        if not a['enabled']:
            continue
        for g in a['groups']:
            if g in owner and g != '*':
                raise ValueError(f'群「{g}」同时绑定给了「{owner[g]}」和「{a["name"]}」')
            owner.setdefault(g, a['name'])
    if sum(1 for a in out if a['enabled'] and '*' in a['groups']) > 1:
        raise ValueError('只能有一个启用的兜底 Agent（绑定 *）')
    raw = _read_raw_config()
    if not raw:                         # 第一次保存：把当前生效配置一起落盘
        raw = load_config()
    raw['AGENTS'] = out
    _write_raw_config(raw)
    return out


def list_chats() -> list:
    """机器人见过的会话（known_chats.json）+ 日志里出现过的群名，供绑定时挑选。"""
    try:
        with open(KNOWN_CHATS_FILE, encoding='utf-8') as f:
            known = json.load(f)
    except (OSError, ValueError):
        known = {}
    out = [{'id': k, 'name': v.get('name') or k, 'last': v.get('last'),
            'group': v.get('group', k.endswith('@chatroom'))} for k, v in known.items()]
    names = {c['name'] for c in out}
    for c in build_conversations(parse_events()):
        if c['group'] not in names:
            names.add(c['group'])
            out.append({'id': '', 'name': c['group'], 'last': c['ts'], 'group': True})
    out.sort(key=lambda c: (not c['group'], -(c['last'] or 0)))
    return out


# ------------------------- 用量统计 -------------------------

def _read_usage(since: float) -> list:
    recs = []
    for path in (USAGE_FILE + '.1', USAGE_FILE):
        try:
            with open(path, encoding='utf-8') as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get('ts', 0) >= since:
                        recs.append(r)
        except OSError:
            pass
    return recs


def _agg(recs) -> dict:
    p = sum(r.get('prompt', 0) for r in recs)
    c = sum(r.get('cached', 0) for r in recs)
    return {'calls': len(recs), 'prompt': p, 'cached': c,
            'completion': sum(r.get('completion', 0) for r in recs),
            'hit_rate': round(c / p * 100, 1) if p else None,
            'avg_secs': round(sum(r.get('secs', 0) for r in recs) / len(recs), 2) if recs else None}


def usage_stats(days: int = 7) -> dict:
    now = datetime.now()
    day0 = datetime(now.year, now.month, now.day)
    start = day0 - timedelta(days=days - 1)
    recs = _read_usage(start.timestamp())
    today = [r for r in recs if r['ts'] >= day0.timestamp()]

    daily = []
    for i in range(days):
        d0 = start + timedelta(days=i)
        rs = [r for r in recs if d0.timestamp() <= r['ts'] < (d0 + timedelta(days=1)).timestamp()]
        daily.append({'label': d0.strftime('%m-%d'), **_agg(rs)})
    hourly = []
    for h in range(24):
        h0 = (day0 + timedelta(hours=h)).timestamp()
        hourly.append(_agg([r for r in today if h0 <= r['ts'] < h0 + 3600]))

    def group_by(key, rs):
        g = {}
        for r in rs:
            g.setdefault(r.get(key) or '（未知）', []).append(r)
        return sorted(({'key': k, **_agg(v)} for k, v in g.items()),
                      key=lambda x: -(x['prompt'] + x['completion']))

    names = {a['id']: a['name'] for a in load_agents()}
    by_agent = group_by('agent', recs)
    for a in by_agent:
        a['name'] = names.get(a['key'], a['key'])
    return {
        'days': days, 'total': _agg(recs), 'today': _agg(today),
        'estimated': any(r.get('cached_src') == 'est' for r in recs),
        'server_reported': any(r.get('cached_src') == 'server' for r in recs),
        'daily': daily, 'hourly': hourly,
        'by_agent': by_agent, 'by_chat': group_by('chat_name', recs)[:12],
        'by_kind': group_by('kind', recs), 'by_host': group_by('host', recs),
        'recent': [{**r, 'time': datetime.fromtimestamp(r['ts']).strftime('%m-%d %H:%M:%S'),
                    'agent_name': names.get(r.get('agent'), r.get('agent'))}
                   for r in recs[-30:][::-1]],
    }


# ------------------------- 进程管理 -------------------------

def _bot_env() -> dict:
    """子进程环境：把生效的 LLM 配置（含 start_bot.cmd 里的）透传成环境变量。

    不传的话，没有 bot_config.json 时管理页拉起的机器人/链路测试会退回
    127.0.0.1 默认节点。
    """
    cfg = load_config()
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    env['WECHAT_DEEPSEEK_API_HOSTS'] = ','.join(cfg.get('API_HOSTS') or [])
    env['WECHAT_DEEPSEEK_API_PORT'] = str(cfg.get('API_PORT', ''))
    env['WECHAT_DEEPSEEK_API_KEY'] = str(cfg.get('API_KEY', ''))
    env['WECHAT_DEEPSEEK_MODEL'] = str(cfg.get('MODEL', ''))
    return env


def find_bot() -> psutil.Process:
    """找正在跑的机器人进程（不论是管理页拉起的还是 start_bot.cmd 起的）。"""
    for p in psutil.process_iter(['name', 'cmdline']):
        try:
            cmd = p.info['cmdline'] or []
            if 'python' not in (p.info['name'] or '').lower():
                continue
            if any(c.endswith('dafeiyu_bot.py') for c in cmd) \
                    and '--test' not in cmd and '--search' not in cmd:
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def start_bot() -> dict:
    with _proc_lock:
        if find_bot():
            return {'ok': False, 'error': '机器人已经在运行'}
        env = _bot_env()
        flags = 0
        if os.name == 'nt':
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        # stdout 丢弃：机器人自己会 tee 到 dafeiyu_bot.log；stderr 留一份兜底
        # 抓 _setup_logging 之前的崩溃（比如 import 失败）
        err = open(BOT_STDERR, 'ab')
        subprocess.Popen([sys.executable, BOT_SCRIPT], cwd=ROOT, env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=err, creationflags=flags)
        err.close()
    for _ in range(20):                 # 等进程出现，最多 2 秒
        time.sleep(0.1)
        if find_bot():
            break
    return {'ok': True}


def stop_bot() -> dict:
    with _proc_lock:
        p = find_bot()
        if not p:
            return {'ok': False, 'error': '机器人没在运行'}
        procs = [p] + p.children(recursive=True)
        for q in procs:
            try:
                q.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(procs, timeout=5)
        for q in alive:
            try:
                q.kill()
            except psutil.NoSuchProcess:
                pass
    print(f'[{time.strftime("%m-%d %H:%M:%S")}] [管理] 机器人已被管理页停止', flush=True)
    return {'ok': True}


# ------------------------- 日志解析 -------------------------

def _read_log_lines() -> list:
    """按时间顺序读 .2/.1/当前日志的所有行。"""
    lines = []
    for path in (BOT_LOG + '.2', BOT_LOG + '.1', BOT_LOG):
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                lines.extend(f.read().splitlines())
        except OSError:
            pass
    return lines


def _unrepr(s: str) -> str:
    try:
        v = ast.literal_eval(s)
        return v if isinstance(v, str) else s
    except Exception:
        return s


def parse_events() -> list:
    """把日志解析成事件列表：[{t, tag, body}]；多行异常栈归到上一事件。"""
    events = []
    for raw in _read_log_lines():
        m = _LINE_RE.match(raw)
        if not m:
            if events and not raw.startswith('20'):   # 库日志行跳过，续行并入
                events[-1]['extra'] = events[-1].get('extra', '') + raw + '\n'
            continue
        dt = _mk_dt(m)
        rest = m.group(6)
        tm = _TAG_RE.match(rest)
        if tm:
            events.append({'t': dt, 'tag': tm.group(1), 'body': tm.group(2)})
        elif '已上线' in rest:
            events.append({'t': dt, 'tag': '上线', 'body': rest})
        else:
            events.append({'t': dt, 'tag': '', 'body': rest})
    return events


def _mk_dt(m) -> datetime:
    now = datetime.now()
    dt = datetime(now.year, int(m.group(1)), int(m.group(2)),
                  int(m.group(3)), int(m.group(4)), int(m.group(5)))
    if dt > now + timedelta(days=1):    # 跨年：去年 12 月的日志行
        dt = dt.replace(year=now.year - 1)
    return dt


def build_conversations(events: list) -> list:
    """收到/回复配对成对话，中间的工具调用挂到对话的 steps 上。

    不同群的回调在各自线程里跑，可能交错；按 FIFO 配对，绝大多数情况准确。
    """
    convs, pending = [], []
    # 多 Agent 之前的日志没有 <Agent> 前缀：算在兜底 Agent 头上
    fallback = next((a['name'] for a in load_agents() if '*' in (a.get('groups') or [])), '')
    for ev in events:
        tag, body = ev['tag'], ev['body']
        if tag == '收到':
            m = _RECV_RE.match(body)
            if not m:
                continue
            c = {'time': ev['t'].strftime('%Y-%m-%d %H:%M:%S'), 'ts': ev['t'].timestamp(),
                 'agent': m.group(1) or fallback, 'group': m.group(2), 'who': m.group(3),
                 'question': _unrepr(m.group(4)),
                 'answer': None, 'latency': None, 'steps': [], 'status': 'pending'}
            convs.append(c)
            pending.append(c)
        elif tag == '回复' and pending:
            m = _REPLY_RE.match(body)
            c = pending.pop(0)
            if m:
                c['latency'] = float(m.group(1))
                ans = m.group(2)
                at = re.search(r" \[@(.+)\]$", ans)
                if at:
                    ans = ans[:at.start()]
                c['answer'] = _unrepr(ans)
                c['status'] = 'fail' if '晕鱼了' in c['answer'] else 'ok'
            else:
                c['answer'] = body
                c['status'] = 'ok'
        elif tag in ('工具', '表情', 'say', 'ChatGPT', 'agent', '!!!', 'llm') and pending:
            pending[0]['steps'].append({'time': ev['t'].strftime('%H:%M:%S'),
                                        'tag': tag, 'body': body})
            if tag == '!!!':
                pending[0]['status'] = 'fail'
        elif tag == '!!!' and convs and '发送失败' in body:
            convs[-1]['status'] = 'fail'          # 回复生成了但没发出去
    return convs


def compute_stats() -> dict:
    events = parse_events()
    convs = build_conversations(events)
    now = datetime.now()
    today = now.date()
    day_ago = now - timedelta(hours=24)

    today_convs = [c for c in convs if datetime.fromtimestamp(c['ts']).date() == today]
    lat = [c['latency'] for c in today_convs if c['latency'] is not None]
    hourly = [0] * 24
    for c in convs:
        dt = datetime.fromtimestamp(c['ts'])
        if dt >= day_ago:
            idx = 23 - int((now - dt).total_seconds() // 3600)
            if 0 <= idx < 24:
                hourly[idx] += 1
    groups = Counter(c['group'] for c in convs if datetime.fromtimestamp(c['ts']) >= now - timedelta(days=7))
    users = Counter(c['who'] for c in convs if datetime.fromtimestamp(c['ts']) >= now - timedelta(days=7))
    tools = Counter()
    for ev in events:
        if ev['t'].date() != today:
            continue
        if ev['tag'] == '工具':
            tools[ev['body'].split(' ', 1)[0]] += 1
        elif ev['tag'] in ('表情', 'say'):
            tools['send_sticker' if ev['tag'] == '表情' else 'say_to_group'] += 1
        elif ev['tag'] == 'ChatGPT' and '转问' in ev['body']:
            tools['ask_chatgpt'] += 1
    errors = [{'time': e['t'].strftime('%m-%d %H:%M:%S'), 'tag': e['tag'], 'body': e['body'][:300]}
              for e in events if e['tag'] in ('!!!', '回调异常', 'llm')
              or (e['tag'] == 'ChatGPT' and '失败' in e['body'])]

    # 当前运行状态：最后一次上线之后有没有出现「已暂停」
    paused = False
    last_online = None
    for ev in events:
        if ev['tag'] == '上线':
            paused, last_online = False, ev['t']
        elif ev['tag'] == '!!!' and '暂停' in ev['body']:
            paused = True
    return {
        'today': {
            'questions': len(today_convs),
            'replied': sum(1 for c in today_convs if c['status'] == 'ok'),
            'failed': sum(1 for c in today_convs if c['status'] == 'fail'),
            'avg_latency': round(sum(lat) / len(lat), 1) if lat else None,
            'p_max_latency': round(max(lat), 1) if lat else None,
            'tool_calls': sum(tools.values()),
        },
        'hourly': hourly,
        'hour_labels': [(now - timedelta(hours=23 - i)).strftime('%H') for i in range(24)],
        'groups': groups.most_common(8),
        'users': users.most_common(8),
        'tools': tools.most_common(),
        'errors': errors[-30:][::-1],
        'paused': paused,
        'last_online': last_online.strftime('%Y-%m-%d %H:%M:%S') if last_online else None,
        'total_convs': len(convs),
        'usage_today': _agg(_read_usage(datetime(now.year, now.month, now.day).timestamp())),
        'agents_today': Counter(c['agent'] or '未分配' for c in today_convs).most_common(),
    }


def bot_status() -> dict:
    p = find_bot()
    st = {'running': bool(p)}
    if p:
        try:
            st.update(pid=p.pid, started=p.create_time(),
                      uptime=int(time.time() - p.create_time()),
                      memory_mb=round(p.memory_info().rss / 1048576, 1),
                      cpu=p.cpu_percent(interval=0.1))
        except psutil.Error:
            pass
    return st


def tail_log(offset: int, initial_bytes: int = 64_000) -> dict:
    try:
        size = os.path.getsize(BOT_LOG)
    except OSError:
        return {'offset': 0, 'text': '', 'reset': True}
    reset = offset < 0 or offset > size        # 首次读取 / 日志滚动过
    start = max(0, size - initial_bytes) if reset else offset
    with open(BOT_LOG, 'rb') as f:
        f.seek(start)
        data = f.read(min(size - start, 1_000_000))
    text = data.decode('utf-8', errors='replace')
    if reset and start > 0:                    # 丢掉截断的半行
        text = text.split('\n', 1)[-1]
    return {'offset': start + len(data), 'text': text, 'reset': reset}


# ------------------------- LLM 探测 -------------------------

def probe_nodes() -> list:
    cfg = load_config()
    hosts = cfg.get('API_HOSTS') or []
    results = [None] * len(hosts)

    def one(i, host):
        url = f'http://{host}:{cfg["API_PORT"]}/v1/models'
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {cfg["API_KEY"]}'})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=4) as r:
                body = json.loads(r.read())
            models = [m.get('id') for m in body.get('data', [])]
            results[i] = {'host': host, 'ok': True, 'ms': int((time.time() - t0) * 1000),
                          'models': models, 'has_model': cfg['MODEL'] in models}
        except Exception as e:
            results[i] = {'host': host, 'ok': False, 'ms': int((time.time() - t0) * 1000),
                          'error': str(e)[:200]}

    ts = [threading.Thread(target=one, args=(i, h)) for i, h in enumerate(hosts)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return results


def test_llm(question: str, agent: str = '') -> dict:
    """跑 `dafeiyu_bot.py --test`：走完整的提示词 + 工具链路，不连微信。"""
    t0 = time.time()
    try:
        cmd = [sys.executable, BOT_SCRIPT, '--test', question or '在吗？']
        if agent:
            cmd += ['--agent', agent]
        r = subprocess.run(cmd,
                           cwd=ROOT, capture_output=True, timeout=240,
                           env=_bot_env(),
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        out = (r.stdout + r.stderr).decode('utf-8', errors='replace')
        ok = r.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = '超时（240s）', False
    return {'ok': ok, 'output': out.strip(), 'seconds': round(time.time() - t0, 1)}


# ------------------------- 表情包 -------------------------

def _safe_sticker(name: str) -> str:
    name = os.path.basename(name or '').strip()
    if not name or name.startswith('.') or not name.lower().endswith(STICKER_EXTS):
        raise ValueError('文件名不合法（只支持 gif/jpg/png/webp/bmp）')
    return os.path.join(STICKER_DIR, name)


def list_stickers() -> list:
    out = []
    try:
        for f in sorted(os.listdir(STICKER_DIR)):
            if f.lower().endswith(STICKER_EXTS):
                st = os.stat(os.path.join(STICKER_DIR, f))
                out.append({'file': f, 'name': os.path.splitext(f)[0],
                            'size': st.st_size, 'mtime': st.st_mtime})
    except OSError:
        pass
    usage = Counter()
    for ev in parse_events():
        if ev['tag'] == '表情':
            usage[ev['body'].split(' ->', 1)[0].strip()] += 1
    for s in out:
        s['used'] = usage.get(s['file'], 0)
    return out


# ------------------------- HTTP -------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'DaFeiYuAdmin/1.0'

    def log_message(self, fmt, *args):         # 静音访问日志（前端每秒轮询）
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path, ctype=None):
        if not os.path.isfile(path):
            return self._json({'error': 'not found'}, 404)
        with open(path, 'rb') as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        ctype = ctype or {'.html': 'text/html; charset=utf-8', '.gif': 'image/gif',
                          '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                          '.webp': 'image/webp', '.bmp': 'image/bmp',
                          '.svg': 'image/svg+xml'}.get(ext, 'application/octet-stream')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        if os.path.basename(os.path.dirname(path)) == 'assets':   # 素材图不常变，让浏览器缓存
            self.send_header('Cache-Control', 'max-age=86400')
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode('utf-8'))

    def _same_origin(self) -> bool:
        """写操作只接受本页发起的请求，挡掉别的网页对 127.0.0.1 的跨站 POST。"""
        origin = self.headers.get('Origin')
        host = self.headers.get('Host', '')
        return origin is None or urllib.parse.urlparse(origin).netloc == host

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        try:
            if p in ('/', '/index.html'):
                return self._file(os.path.join(STATIC_DIR, 'index.html'))
            if p == '/api/status':
                return self._json({'bot': bot_status(), **compute_stats()})
            if p == '/api/conversations':
                convs = build_conversations(parse_events())[::-1]
                kw = (q.get('q') or [''])[0].strip().lower()
                if kw:
                    convs = [c for c in convs if kw in (c['question'] or '').lower()
                             or kw in (c['answer'] or '').lower()
                             or kw in c['who'].lower() or kw in c['group'].lower()]
                return self._json(convs[:300])
            if p == '/api/logs':
                return self._json(tail_log(int((q.get('offset') or ['-1'])[0])))
            if p == '/api/config':
                return self._json({'config': load_config(),
                                   'file_exists': os.path.exists(CONFIG_FILE)})
            if p == '/api/agents':
                d = load_config()
                at = (d.get('AT_NAMES') or ['大肥鱼'])[0]
                return self._json({'agents': load_agents(), 'tools': TOOL_NAMES,
                                   'defaults': {'AT_NAME': at,
                                                'MODEL': d.get('MODEL'),
                                                'MAX_TOKENS': d.get('MAX_TOKENS'),
                                                'RECENT_N': d.get('RECENT_N')}})
            if p == '/api/chats':
                return self._json(list_chats())
            if p == '/api/usage':
                return self._json(usage_stats(max(1, min(90, int((q.get('days') or ['7'])[0])))))
            if p == '/api/nodes':
                return self._json(probe_nodes())
            if p == '/api/stickers':
                return self._json(list_stickers())
            if p.startswith('/assets/'):
                name = os.path.basename(urllib.parse.unquote(p[len('/assets/'):]))
                return self._file(os.path.join(STATIC_DIR, 'assets', name))
            if p.startswith('/stickers/'):
                return self._file(_safe_sticker(urllib.parse.unquote(p[len('/stickers/'):])))
            return self._json({'error': 'not found'}, 404)
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)

    def do_POST(self):
        if not self._same_origin():
            return self._json({'error': 'cross-origin request refused'}, 403)
        p = urllib.parse.urlparse(self.path).path
        try:
            body = self._body()
            if p == '/api/bot/start':
                return self._json(start_bot())
            if p == '/api/bot/stop':
                return self._json(stop_bot())
            if p == '/api/bot/restart':
                stop_bot()
                time.sleep(1)
                return self._json(start_bot())
            if p == '/api/config':
                cfg = save_config(body)
                return self._json({'ok': True, 'config': cfg})
            if p == '/api/test':
                return self._json(test_llm(body.get('question', ''), body.get('agent', '')))
            if p == '/api/agents':
                return self._json({'ok': True, 'agents': save_agents(body.get('agents'))})
            if p == '/api/stickers/upload':
                path = _safe_sticker(body.get('file'))
                if os.path.exists(path) and not body.get('overwrite'):
                    return self._json({'error': '同名文件已存在'}, 409)
                data = base64.b64decode(body.get('data', '').split(',')[-1])
                if len(data) > 10 * 1024 * 1024:
                    return self._json({'error': '文件超过 10MB'}, 400)
                os.makedirs(STICKER_DIR, exist_ok=True)
                with open(path, 'wb') as f:
                    f.write(data)
                return self._json({'ok': True})
            if p == '/api/stickers/rename':
                src = _safe_sticker(body.get('file'))
                ext = os.path.splitext(src)[1]
                dst = _safe_sticker(body.get('name', '').strip() + ext)
                if os.path.exists(dst):
                    return self._json({'error': '同名文件已存在'}, 409)
                os.replace(src, dst)
                return self._json({'ok': True, 'file': os.path.basename(dst)})
            if p == '/api/stickers/delete':
                os.remove(_safe_sticker(body.get('file')))
                return self._json({'ok': True})
            return self._json({'error': 'not found'}, 404)
        except ValueError as e:
            return self._json({'error': str(e)}, 400)
        except FileNotFoundError:
            return self._json({'error': '文件不存在'}, 404)
        except Exception as e:
            return self._json({'error': f'{type(e).__name__}: {e}'}, 500)


def serve(host: str = '127.0.0.1', port: int = 8765) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f'大肥鱼管理后台：http://{host}:{port}  （Ctrl+C 退出）', flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
