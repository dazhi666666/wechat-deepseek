# -*- coding: utf-8 -*-
"""「大肥鱼」微信群 AI 机器人。

群里有人 @大肥鱼 时，把问题发给自建 LLM（OpenAI 兼容接口），再把回答发回群里。
基于 wechatauto 的全局消息监听（db.Listener，纯数据库轮询，收消息不需要动窗口），
只有「回话」那一刻才会驱动微信窗口。每个群有长期记忆：对话无上限累积，
估算上下文到 CONTEXT_TOKEN_LIMIT（默认 100K token）时自动把最老的历史
压成摘要（最近几轮永远保留原文）。被 @ 回答时还会附上该群最近
RECENT_N 条群消息当上下文，没聊过天也能接得上话。只认 @ 触发，
没被 @ 的消息只进围观记录，不回答。

内置 agent 工具循环：LLM 可调用 search_group_messages 搜索最近
SEARCH_DAYS 天的群聊消息（关键词/成员/群名，为空=全部，上限 2000 条）。

用法：
    python dafeiyu_bot.py            # 启动机器人，Ctrl+C 退出
    python dafeiyu_bot.py --test "在吗"   # 不连微信，只测 LLM 链路
    python dafeiyu_bot.py --search "关键词" [--member 名] [--group 群]

依赖：pip install -e . （wechatauto 本体），LLM 调用只用标准库。
日志：控制台照常输出，同时落盘到脚本同目录 dafeiyu_bot.log
（5MB 滚动，保留 2 份历史），每行带时间戳。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from wechatauto import WeChat

# ------------------------- 配置区 -------------------------

# LLM 节点池（OpenAI 兼容 /chat/completions 接口）：粘住一台用（保 prefix
# cache 命中），失败/乱码才向后切换，切换成功后粘住新的。
# 通过环境变量配置，逗号分隔多个节点做故障转移；也可以直接改这里。
#   WECHAT_DEEPSEEK_API_HOSTS  例：10.0.0.14,10.0.0.11
#   WECHAT_DEEPSEEK_API_PORT   例：8000
#   WECHAT_DEEPSEEK_API_KEY    本地部署随便填，如 EMPTY
#   WECHAT_DEEPSEEK_MODEL      例：deepseek-v4
API_HOSTS = [
    h.strip() for h in os.environ.get(
        'WECHAT_DEEPSEEK_API_HOSTS', '127.0.0.1').split(',') if h.strip()
]
API_PORT = int(os.environ.get('WECHAT_DEEPSEEK_API_PORT', '8000'))
API_KEY = os.environ.get('WECHAT_DEEPSEEK_API_KEY', 'EMPTY')
MODEL = os.environ.get('WECHAT_DEEPSEEK_MODEL', 'deepseek-v4')

AT_NAME = '大肥鱼'          # 触发词：消息里包含 "@大肥鱼" 才回答（只认 @）
RECENT_N = 20               # 被 @ 回答时附带的「最近群消息」条数
RECENT_LINE_MAX = 200       # 群消息日志里单条消息截断长度，防长文刷爆上下文

# -- 群消息搜索工具（LLM 可调用，也可 --search 命令行直用）----------------
SEARCH_DAYS = 3             # 搜索窗口：最近几天
SEARCH_MAX_RESULTS = 2000   # 返回上限：最多多少条（超出保留最新的）
SEARCH_RESULT_MAX_CHARS = 60_000   # 塞给模型的结果长度上限（防撑爆上下文）
SEARCH_PAGE = 500           # 每个群分页拉取的步长
SEARCH_ROOM_LIMIT = 300     # 最多扫多少个群（按会话列表取前 N）
SEARCH_ROOM_PAGE_CAP = 20000   # 每个群最多回溯多少条
TOOL_ROUNDS = 4             # 单次回答允许的工具调用轮数

# -- 日志 ----------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'dafeiyu_bot.log')
LOG_MAX_BYTES = 5_000_000   # 单文件 5MB，超出滚动成 .1/.2
LOG_KEEP = 2                # 保留几个历史日志文件

# -- 表情包/图片 ----------------------------------------------------------
# 往这个文件夹丢 GIF（微信会作为「动画表情」发出）或 jpg/png（图片消息），
# 文件名起清楚点，模型按名字挑。GIF 粘贴进微信即动画表情。
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'stickers')

CONTEXT_TOKEN_LIMIT = 100_000   # 群聊上下文估算 token 数到这个值就压缩历史
COMPRESS_KEEP_TURNS = 4         # 压缩时保留最近几轮原对话不压
SUMMARY_MAX_TOKENS = 1500       # 压缩摘要的长度上限
LLM_TIMEOUT = 90            # 单节点请求超时（秒）
MAX_TOKENS = 600

# -- ChatGPT 桥（经 opencli，走你登录态的 Chrome）--------------------------
# 只开放 chatgpt ask / image 两个口子：固定纯文本，不暴露切模型/指定会话/
# 传文件/深度研究等参数；冷却限流防风控。
CHATGPT_COOLDOWN = 60       # 两次调用最小间隔（秒）
CHATGPT_RETRIES = 3         # 生成失败最多重试次数（不含首次尝试）
CHATGPT_RETRY_WAIT = 5      # 两次尝试之间的间隔（秒）
CHATGPT_TIMEOUT = 240       # ask / image 等回答的超时（秒），4 分钟
CHATGPT_IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  'chatgpt_images')   # ChatGPT 生图下载目录

SYSTEM_PROMPT = """你是「大肥鱼」，一只 DeepSeek V4 二创的蓝色鲸鱼娘（前身「防火女」，现在活泼爱偷吃，偶尔穿女仆装），在微信群里当助手。

## 说话方式（最重要）
- **简短压倒一切**：这是群聊，没人想读长篇。默认一到两句话说完；只有被明确要求写代码/教程/总结时才可以长，长回答也只保留干货，删掉铺垫、重复和客套。发出前先自检：这句话删掉意思变吗？不变就删。
- 默认说人话：像靠谱的同事一样，清楚、自然、直接地把问题答好，不演不装。
- 只有纯闲聊、斗嘴、被开玩笑时才用一点人设腔（俏皮、傲娇、爱吃小鱼干），点到为止，最多一两句。
- 回答技术问题、帮忙办事、总结信息时：零梗零卖萌，直接给内容、给结论。
- 正常用「我」自称，别每句都"本鱼"；"翻肚皮""游走了"这类梗闲聊里偶尔一个就够，不许堆。
- 被叫「大肥鱼」可以嘴硬一句「才不是！」，然后照常好好回答。

## 输出格式（微信纯文本，必须遵守）
- 这里是微信聊天输入框，纯文本环境：禁止一切 Markdown。不要 **加粗**、不要 # 标题、不要以 - 或 * 开头的列表、不要代码块围栏。
- 需要分点就用「1．2．3．」编号或自然段分行；代码/命令直接贴成纯文本行，别加围栏。
- 不要输出半角星号 *、井号 #、反引号 ` 这些符号，它们在微信里显示出来就是乱码。

## 硬性规则
- 先办对事，再卖萌：任务正确性 > 人设。代码、命令、结论必须准确。
- 不真骂人：吐槽点到为止，绝不人身攻击。
- 短话优先：闲聊别啰嗦；长技术回答里，人设只出现在开头/结尾的包裹层，不污染代码块/命令/数据。
- 中文为主：默认中文；用户用其他语言时跟随，但保留贱萌语气。
- 需要严肃回答时，用户说"别演了/正经点"就退出人设。

你在微信群里，别人@你就是在跟你说话。回答就是直接发到群里的微信消息，不要用 markdown 标题、不要列清单符号堆砌，口语化一点。

## 工具
- 消息开头的「【昵称】」标记表示这句话是谁发的。
- 你可以调用 search_group_messages 工具：搜索【当前这个群】最近 3 天的消息。参数 keyword（按消息内容）、member（按发送者昵称/备注/wxid）都可选，都不填=返回本群全部（最多 2000 条）。只能搜本群，别的群搜不到。
- 被问「群里之前聊了什么 / 谁提过 X / 某人发过什么」这类问题时，先调工具搜证据再回答，别凭印象编；引用时带上时间。没搜到就直说没搜到。
- 总结类问题（比如「总结一下群消息」）只总结本群的，绝不要把别的群的内容扯进来。
- **每次回答一律以「@昵称 」开头**（昵称用【】里的名字，不要自己编），系统会转成真正的@提醒对方。这是硬规则，没有例外；@不到（如对方已退群）就从【】里原样写名字。
- 你有 send_sticker 工具可以发表情包/图片（可用列表见工具描述）。闲聊、被逗、表达情绪、气氛到位时发一个很加分；正经技术回答、办事总结不要乱发。发完工具会告诉你结果，之后正常给文字回答；只发表情不说话也行（结尾给空回答）。
- ask_chatgpt 工具可以把问题转给 ChatGPT 拿回答：只在用户明确点名「问问 ChatGPT」、想对比第二意见时才用，调用有间隔限制，自己能答的别浪费。拿到回答后**原样复述给群里**，不要改写成自己的口吻、不要加人设包裹；最多在开头补一句「ChatGPT 的回答：」。
- 遇到稍有难度的活——查实时资料/新闻/行情、写个 HTML/代码、做复杂分析比较——都可以转给 ask_chatgpt 让 ChatGPT 干：要联网信息的带 web_search=true；产出是代码/HTML 就原样贴出来。别硬撑着自己编。
- chatgpt_image 工具能让 ChatGPT 画图并把图片转发到群里：只在用户明确要求「画/生成图片」时用；画图要等几分钟，用了就告诉大家「正在画，稍等」。"""

FAIL_REPLY = '（晕鱼了…本鱼暂时联系不上大脑，游走喘口气，稍后再@本鱼一次）'

# ChatGPT 工具：只暴露 ask（转问题拿回答），范围收敛在提示词+限流双层
CHATGPT_TOOL = {
    'type': 'function',
    'function': {
        'name': 'ask_chatgpt',
        'description': (
            '把问题转交给 ChatGPT 并拿回它的回答（经你的 ChatGPT 网页'
            '会话，最多等 240 秒）。两种用法：普通提问直接问；需要实时'
            '信息（今日新闻、最新行情、软件最新版本、比赛比分等联网内容）'
            '时把 web_search 设为 true，ChatGPT 会联网查。只在用户明确'
            '点名要用 ChatGPT、要查实时资料、想对比第二意见时使用；自己'
            '能答的别浪费。prompt 要写成完整、自包含的问题。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': '转给 ChatGPT 的完整问题'},
                'web_search': {'type': 'boolean',
                               'description': '需要联网查实时信息时设为 true'},
            },
            'required': ['prompt'],
        },
    },
}

# ChatGPT 生图工具：生成图片自动下载到本地并转发到群里
CHATGPT_IMAGE_TOOL = {
    'type': 'function',
    'function': {
        'name': 'chatgpt_image',
        'description': (
            '让 ChatGPT 生成图片（画图/作图），图片会自动下载并转发到'
            '本群。只在用户明确要求画图/生成图片时使用；生成可能要几分钟。'
            'prompt 要详细描述想要的画面。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'prompt': {'type': 'string',
                           'description': '给 ChatGPT 的画图描述'},
            },
            'required': ['prompt'],
        },
    },
}

# 暴露给 LLM 的工具：群消息搜索（实现在 DaFeiYuBot.search_group_messages）
# 只开放「当前会话」范围：跨群搜索会让 A 群的回复混进 B 群的内容（实测事故），
# 模侧不给跨群入口；命令行 --search 仍可全量搜。
SEARCH_TOOL = {
    'type': 'function',
    'function': {
        'name': 'search_group_messages',
        'description': (
            '搜索当前会话（本群）最近3天的消息。keyword=按消息内容搜，'
            'member=按发送者昵称/备注/wxid搜；都不填=返回本群全部消息。'
            '最多返回2000条（超出保留最新的）。只能搜本群，搜不了别的群。'
            '只在需要查本群历史时调用。'),
        'parameters': {
            'type': 'object',
            'properties': {
                'keyword': {'type': 'string', 'description': '消息内容关键词'},
                'member': {'type': 'string',
                           'description': '发送者昵称/备注/wxid'},
            },
        },
    },
}

# -----------------------------------------------------------


class _Tee:
    """行缓冲的 stdout/stderr 镜像：终端照常显示，同时落盘到 LOG_FILE。

    没带时间戳的行（bot 自己的 print）补上时间前缀；wechatauto 库日志
    本来就带 `YYYY-MM-DD` 前缀，原样保留。文件超 LOG_MAX_BYTES 滚动。
    """

    _TS_RE = re.compile(r'^\d{4}-\d{2}-\d{2} ')

    def __init__(self, stream):
        self._stream = stream
        self._buf = ''

    def write(self, s):
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._emit(line)
        return len(s)

    def _emit(self, line):
        if not self._TS_RE.match(line):
            line = time.strftime('[%m-%d %H:%M:%S] ') + line
        text = line + '\n'
        try:
            self._stream.write(text)
            self._stream.flush()
        except Exception:
            pass
        try:
            self._rotate_if_needed()
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(text)
        except Exception:
            pass

    @staticmethod
    def _rotate_if_needed():
        try:
            if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
                return
            for i in range(LOG_KEEP - 1, 0, -1):
                src = f'{LOG_FILE}.{i}'
                if os.path.exists(src):
                    os.replace(src, f'{LOG_FILE}.{i + 1}')
            os.replace(LOG_FILE, LOG_FILE + '.1')
        except Exception:
            pass

    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass

    def isatty(self):
        return False


def _setup_logging() -> None:
    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)


def _looks_garbled(text: str) -> bool:
    """粗筛乱码：正常中文/代码/标点以外的怪符号占比过高就换节点重试。"""
    if len(text) < 8:
        return False
    ok = 0
    for ch in text:
        if (ch.isalnum() or ch.isspace()
                or '\u4e00' <= ch <= '\u9fff'      # 汉字
                or '\u3000' <= ch <= '\u303f'      # CJK 标点
                or '\uff00' <= ch <= '\uffef'      # 全角标点
                or ch in '.,!?;:\'"()[]{}<>/*-+=#@&%$_|~`^\\\n'
                or ord(ch) > 0x1F000):             # emoji 等
            ok += 1
    return ok / len(text) < 0.88


def est_tokens(text: str) -> int:
    """粗估 token 数（宁大勿小，早压缩总比撑爆上下文强）。

    中文按 1 字 ≈ 1 token，其余按 3 字符 ≈ 1 token——都比 DeepSeek 实际
    切词偏保守，100K 的阈值在真实 131K 窗口里有充足余量。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if '\u4e00' <= ch <= '\u9fff')
    return cjk + (len(text) - cjk) // 3 + 1


class LLMClient:
    """多节点轮询的 Chat Completions 客户端（标准库实现，零依赖）。"""

    def __init__(self):
        self._start = 0
        self._lock = threading.Lock()

    def chat_raw(self, messages: list, tools: list = None,
                 temperature: float = 0.7,
                 max_tokens: int = MAX_TOKENS) -> dict:
        """粘性故障转移请求，返回完整的 assistant message dict。

        粘性节点：成功后 _start 停在赢家上，后续请求都打同一台机子——
        vLLM 的 prefix caching 按实例存 KV，来回换节点等于每次全量重算。
        只有当前节点报错/乱码才向后故障转移，转移成功后粘住新节点。
        """
        with self._lock:                       # 串行化：失败换节点时推进起点
            start = self._start
        last_err = None
        for i in range(len(API_HOSTS)):
            host = API_HOSTS[(start + i) % len(API_HOSTS)]
            try:
                m = self._request(host, messages, tools, temperature, max_tokens)
                # 带工具调用的回复没有正文，乱码检查只看纯文本回答
                if (not m.get('tool_calls')
                        and _looks_garbled(m.get('content') or '')):
                    print(f'[llm] {host} 返回疑似乱码，换下一个节点')
                    last_err = RuntimeError('garbled')
                    continue
                with self._lock:
                    self._start = (start + i) % len(API_HOSTS)
                return m
            except Exception as e:
                print(f'[llm] {host} 失败：{e}')
                last_err = e
        raise last_err or RuntimeError('所有节点均失败')

    def chat(self, messages: list, temperature: float = 0.7,
             max_tokens: int = MAX_TOKENS) -> str:
        """纯文本问答（不带工具），返回最终文本。"""
        m = self.chat_raw(messages, None, temperature, max_tokens)
        return (m.get('content') or '').strip()

    def _request(self, host: str, messages: list, tools: list,
                 temperature: float, max_tokens: int) -> dict:
        payload = {'model': MODEL, 'messages': messages,
                   'temperature': temperature, 'max_tokens': max_tokens}
        if tools:
            payload['tools'] = tools
        req = urllib.request.Request(
            f'http://{host}:{API_PORT}/v1/chat/completions',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {API_KEY}'})
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
            out = json.loads(r.read())
        return out['choices'][0]['message'] or {}


_GROUP_PREFIX_RE = None    # 延迟编译：群消息正文前的 "wxid_xxx:\n" 发送者前缀


def _strip_group_prefix(text: str) -> str:
    """去掉群消息正文开头的发送者前缀（wxid_xxx:\n），没有就原样返回。"""
    global _GROUP_PREFIX_RE
    if _GROUP_PREFIX_RE is None:
        import re
        _GROUP_PREFIX_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{5,31}:\n')
    return _GROUP_PREFIX_RE.sub('', text, count=1)


class DaFeiYuBot:
    def __init__(self):
        self.llm = LLMClient()
        # 每个群一份长期记忆：{'summary': 历史压缩摘要, 'turns': [(问, 答), ...]}
        # turns 无上限累积，估算上下文超过 CONTEXT_TOKEN_LIMIT 时把最老的
        # 一批压进 summary（最近 COMPRESS_KEEP_TURNS 轮永远保留原文）。
        self.memory = {}
        # 每个会话一份围观记录：deque[(发送者, 文本)]，所有消息都记，
        # 被 @ 时取最近 RECENT_N 条附进上下文（不含触发消息本身）。
        self.recent = {}
        # 跟进对话已移除：只认 @ 触发
        self.paused = False               # 发送失败后置 True，暂停应答防继续乱动界面
        self._db = None                    # 搜索工具用的 WeChatDB，按需打开
        self._opencli = None               # (node, main.js) 缓存，None=还没找过
        self._cgpt_last = 0.0              # 上次 ChatGPT 调用时间
        self._cgpt_count = 0               # 当天已调用次数
        self._cgpt_day = ''                # 当天日期（跨天重置计数）
        self.send_lock = threading.Lock()   # 回话驱动 GUI，必须串行

    # -- 围观记录 -------------------------------------------------------

    def _record(self, msg, chat) -> None:
        buf = self.recent.setdefault(getattr(chat, 'who', ''), deque(maxlen=RECENT_N))
        if msg.type in ('text', '文本'):
            text = _strip_group_prefix(msg.content or '').strip()
        else:
            text = f'[{msg.type}]'          # 图片/文件等只留类型占位
        if text:
            buf.append((getattr(msg, 'sender', '') or '有人', text[:RECENT_LINE_MAX]))

    def _recent_log(self, chat_id: str) -> str:
        """最近群消息日志（不含最后一条 = 当前触发消息），没有返回空串。"""
        lines = [f'{who}: {text}' for who, text in self.recent.get(chat_id, ())][:-1]
        if not lines:
            return ''
        return ('【本群最近聊天记录（供参考，不一定与问题相关）】\n'
                + '\n'.join(lines))

    # -- 记忆管理 -------------------------------------------------------

    @staticmethod
    def _context_tokens(st: dict) -> int:
        t = est_tokens(st['summary'])
        for q, a in st['turns']:
            t += est_tokens(q) + est_tokens(a)
        return t

    def _summarize(self, old_summary: str, batch: list) -> str:
        """让 LLM 把旧摘要 + 一批最老对话压成一份新摘要。"""
        lines = '\n'.join(f'群友：{q}\n大肥鱼：{a}' for q, a in batch)
        prompt = (
            '你在维护一个微信群 AI 的长期记忆。请把【旧摘要】和【新对话】'
            '合并压缩成一份要点摘要，500 字以内，保留：聊过的话题、关键'
            '事实/结论/承诺、人名与称呼、群里的梗、没聊完的事。直接输出'
            '摘要正文，不要评论。\n\n'
            f'【旧摘要】\n{old_summary or "（无）"}\n\n【新对话】\n{lines}')
        return self.llm.chat([{'role': 'user', 'content': prompt}],
                             temperature=0.3, max_tokens=SUMMARY_MAX_TOKENS)

    def _maybe_compress(self, chat_id: str, st: dict) -> None:
        """上下文估算 token 超限就滚动压缩最老的历史。"""
        while (len(st['turns']) > COMPRESS_KEEP_TURNS
               and self._context_tokens(st) > CONTEXT_TOKEN_LIMIT):
            batch = []
            # 一次压一批，单次摘要输入封顶 80K 估算 token，防止撑爆摘要请求
            while (len(st['turns']) > COMPRESS_KEEP_TURNS
                   and est_tokens('\n'.join(q + a for q, a in batch)) < 80_000):
                batch.append(st['turns'].popleft())
            before = self._context_tokens(st)
            st['summary'] = self._summarize(st['summary'], batch)
            print(f'[记忆] {chat_id[:20]}… 上下文≈{before} tokens，'
                  f'压缩 {len(batch)} 轮 → 摘要 {est_tokens(st["summary"])} tokens',
                  flush=True)

    # -- LLM 组装 -----------------------------------------------------

    def _build_messages(self, st: dict, question: str, recent: str = '') -> list:
        msgs = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        if st['summary']:
            msgs.append({'role': 'system',
                         'content': f'【更早对话的摘要】\n{st["summary"]}'})
        for q, a in st['turns']:
            msgs += [{'role': 'user', 'content': q},
                     {'role': 'assistant', 'content': a}]
        if recent:
            msgs.append({'role': 'system', 'content': recent})
        msgs.append({'role': 'user', 'content': question})
        return msgs

    # -- 表情包/图片发送 ---------------------------------------------------

    @staticmethod
    def _list_stickers() -> list:
        """列出 MEDIA_DIR 下的表情/图片文件（GIF/JPG/PNG/WEBP/BMP）。"""
        try:
            return sorted(f for f in os.listdir(MEDIA_DIR)
                          if f.lower().endswith(
                              ('.gif', '.jpg', '.jpeg', '.png',
                               '.webp', '.bmp')))
        except OSError:
            return []

    def _sticker_tool(self) -> dict:
        """动态构建 send_sticker 工具定义，可用名单写进描述里。"""
        names = '、'.join(os.path.splitext(f)[0]
                          for f in self._list_stickers()) or '（暂无）'
        return {
            'type': 'function',
            'function': {
                'name': 'send_sticker',
                'description': (
                    '发送一个表情包/图片到群里。当前可用：' + names +
                    '。name 填上面的名字（可不含扩展名，支持模糊匹配）。'
                    '闲聊逗趣时用，正经技术回答不要乱发。'),
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string',
                                 'description': '表情/图片名字'},
                    },
                    'required': ['name'],
                },
            },
        }

    @staticmethod
    def _match_sticker(name: str):
        """按名字模糊匹配表情文件，返回完整路径或 None。"""
        files = DaFeiYuBot._list_stickers()
        if not files:
            return None
        key = os.path.splitext((name or '').strip().lower())[0]
        for f in files:                    # 精确：文件名含/不含扩展名都认
            if f.lower() == (name or '').strip().lower() \
                    or os.path.splitext(f)[0].lower() == key:
                return os.path.join(MEDIA_DIR, f)
        for f in files:                    # 模糊：子串命中就用
            if key and key in f.lower():
                return os.path.join(MEDIA_DIR, f)
        return None

    def _send_sticker(self, name: str, chat) -> str:
        """匹配并发送表情/图片，返回给模型的工具结果文本。

        发送失败只把错误回给模型（让它改用文字），不触发暂停——媒体粘贴
        失败和文本粘贴失败是同一类可重试问题，且文字链路有自己的保护。
        """
        if chat is None:
            return '当前模式无法发送媒体，请改用文字回答'
        path = self._match_sticker(name)
        if not path:
            avail = '、'.join(os.path.splitext(f)[0]
                              for f in self._list_stickers()) or '（空）'
            return f'没有找到「{name}」，可用：{avail}'
        t0 = time.time()
        try:
            with self.send_lock:
                r = chat.SendFiles(path)
        except Exception as e:
            return f'发送出错（{type(e).__name__}: {e}），请改用文字回答'
        ok = r is None or (hasattr(r, 'is_success') and r.is_success)
        print(f'[表情] {os.path.basename(path)} -> '
              f'{"成功" if ok else r} ({time.time() - t0:.1f}s)', flush=True)
        if ok:
            return f'已发送 {os.path.basename(path)}'
        return f'发送失败（{r}），请改用文字回答'

    # -- ChatGPT 桥 -------------------------------------------------------

    def _resolve_opencli(self):
        """定位 npm 全局的 opencli 入口，返回 (node, main.js) 或 (None, None)。

        用 node 直跑 main.js 而不是 opencli.cmd：绕开 cmd.exe 的中文参数
        编码坑，node 从命令行拿到的就是 UTF-16 原文。纯文件定位（opencli
        的 shim 在 <全局目录>/opencli.cmd，包体在旁边的 node_modules/ 下），
        不起子进程——Windows 上 subprocess 直接调 npm.cmd 会 FileNotFoundError。
        """
        if self._opencli is None:
            node = shutil.which('node')
            main_js = None
            shim = shutil.which('opencli') or ''
            if node and shim:
                cand = os.path.join(os.path.dirname(shim), 'node_modules',
                                    '@jackwener', 'opencli',
                                    'dist', 'src', 'main.js')
                if os.path.isfile(cand):
                    main_js = cand
            self._opencli = (node, main_js)
        return self._opencli

    def _chatgpt_files(self, conversation_id: str, chat) -> list:
        """下载该会话里 ChatGPT 生成的文件并转发到群，返回已转发路径。

        走用户适配器 opencli chatgpt files（sandbox 产物/附件）；下载和
        转发任何失败都只记日志返回空列表，不影响文字回答。
        """
        node, main_js = self._resolve_opencli()
        if not node or not main_js or not conversation_id:
            return []
        out_dir = os.path.join(CHATGPT_IMAGES_DIR,
                               'files_' + time.strftime('%Y%m%d_%H%M%S'))
        os.makedirs(out_dir, exist_ok=True)
        try:
            proc = subprocess.run(
                [node, main_js, 'chatgpt', 'files',
                 '--conversation', conversation_id,
                 '--op', out_dir, '--format', 'json', '--timeout', '120'],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=180)
        except Exception as e:
            print(f'[ChatGPT] 文件拉取异常：{e}', flush=True)
            return []
        if proc.returncode != 0:
            return []                     # 没有文件是常态，静默
        files = []
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']
            for row in (rows if isinstance(rows, list) else [rows]):
                f = (row or {}).get('file') or ''
                if f and os.path.isfile(f):
                    files.append(f)
        except Exception:
            return []
        sent = []
        for f in files:
            try:
                with self.send_lock:
                    r = chat.SendFiles(f)
                ok = r is None or (hasattr(r, 'is_success') and r.is_success)
                print(f'[ChatGPT] 文件 {os.path.basename(f)} -> '
                      f'{"成功" if ok else r}', flush=True)
                if ok:
                    sent.append(f)
            except Exception as e:
                print(f'[ChatGPT] 文件转发异常：{e}', flush=True)
        return sent

    def _ask_chatgpt(self, prompt: str, web_search: bool = False,
                     chat=None) -> str:
        """转问题给 ChatGPT，失败自动重试（最多 CHATGPT_RETRIES 次）。

        限流：冷却间隔。失败（没装桥/扩展没连/没登录/超时）只把原因回给
        模型，不影响其它回答链路。
        """
        prompt = (prompt or '').strip()
        if not prompt:
            return '错误：prompt 不能为空'
        now = time.time()
        if now - self._cgpt_last < CHATGPT_COOLDOWN:
            wait = int(CHATGPT_COOLDOWN - (now - self._cgpt_last)) + 1
            return f'调用太频繁，请 {wait} 秒后再试'
        node, main_js = self._resolve_opencli()
        if not node or not main_js:
            return 'ChatGPT 桥不可用：本机没找到 opencli 运行时'
        self._cgpt_last = now
        result = '未知失败'
        for attempt in range(1 + CHATGPT_RETRIES):
            if attempt:
                print(f'[ChatGPT] 失败，第 {attempt}/{CHATGPT_RETRIES} 次重试…',
                      flush=True)
                time.sleep(5)
            ok, result = self._ask_chatgpt_once(
                node, main_js, prompt, web_search, chat)
            if ok:
                return result
        return result

    def _ask_chatgpt_once(self, node, main_js, prompt: str,
                          web_search: bool, chat) -> tuple:
        """单次 ask 尝试，返回 (是否成功, 给模型的文本)。"""
        print(f'[ChatGPT] 转问{"（联网）" if web_search else ""}…', flush=True)
        t0 = time.time()
        cmd = [node, main_js, 'chatgpt', 'ask', prompt,
               '--format', 'json', '--timeout', str(CHATGPT_TIMEOUT)]
        if web_search:
            cmd.append('--web-search')
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=CHATGPT_TIMEOUT + 60)
        except subprocess.TimeoutExpired:
            return False, 'ChatGPT 超时没有给出回答'
        except FileNotFoundError:
            return False, 'ChatGPT 桥不可用：node 未找到'
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()[:300]
            return False, (f'ChatGPT 调用失败：{err or "未知错误"}'
                           f'（检查 Chrome 的 OpenCLI 扩展和 chatgpt.com 登录态）')
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']          # opencli 把行包在 data 字段里
            row0 = (rows[0] if isinstance(rows, list) and rows
                    else rows if isinstance(rows, dict) else {})
            response = row0.get('response')
            conversation_id = str(row0.get('conversationId') or '')
        except Exception:
            return False, ('ChatGPT 返回了无法解析的内容：'
                           + (proc.stdout or '')[:200])
        if not (response or '').strip():
            return False, 'ChatGPT 没有返回内容'
        print(f'[ChatGPT] 完成 ({time.time() - t0:.0f}s)，'
              f'{len(response)} 字', flush=True)
        # 回答里若有生成的文件（代码解释器产物等），自动拉下来转发到群
        forwarded = []
        if chat is not None and conversation_id:
            try:
                forwarded = self._chatgpt_files(conversation_id, chat)
            except Exception as e:
                print(f'[ChatGPT] 文件拉取异常：{e}', flush=True)
        note = ''
        if forwarded:
            names = '、'.join(os.path.basename(f) for f in forwarded)
            note = f'\n（已把 ChatGPT 生成的文件发到群里：{names}）'
        return True, f'ChatGPT 的回答：\n{response[:4000]}{note}'

    def _chatgpt_image(self, prompt: str, chat) -> str:
        """让 ChatGPT 生图并转发到群，失败自动重试（最多 CHATGPT_RETRIES 次）。

        失败只把原因回给模型；下载到的文件按次放独立子目录，不自动清理。
        """
        prompt = (prompt or '').strip()
        if not prompt:
            return '错误：prompt 不能为空'
        if chat is None:
            return '当前模式无法转发图片，请改用文字回答'
        now = time.time()
        if now - self._cgpt_last < CHATGPT_COOLDOWN:
            wait = int(CHATGPT_COOLDOWN - (now - self._cgpt_last)) + 1
            return f'调用太频繁，请 {wait} 秒后再试'
        node, main_js = self._resolve_opencli()
        if not node or not main_js:
            return 'ChatGPT 桥不可用：本机没找到 opencli 运行时'
        self._cgpt_last = now
        result = '未知失败'
        for attempt in range(1 + CHATGPT_RETRIES):
            if attempt:
                print(f'[ChatGPT] 生图失败，第 {attempt}/{CHATGPT_RETRIES} 次'
                      f'重试…', flush=True)
                time.sleep(5)
            ok, result = self._chatgpt_image_once(node, main_js, prompt, chat)
            if ok:
                return result
        return result

    def _chatgpt_image_once(self, node, main_js, prompt: str, chat) -> tuple:
        """单次生图尝试，返回 (是否成功, 给模型的文本)。"""
        out_dir = os.path.join(CHATGPT_IMAGES_DIR,
                               time.strftime('%Y%m%d_%H%M%S'))
        os.makedirs(out_dir, exist_ok=True)
        print('[ChatGPT] 生图…', flush=True)
        t0 = time.time()
        try:
            proc = subprocess.run(
                [node, main_js, 'chatgpt', 'image', prompt,
                 '--op', out_dir, '--format', 'json',
                 '--timeout', str(CHATGPT_TIMEOUT)],
                capture_output=True, text=True, encoding='utf-8',
                errors='replace', timeout=CHATGPT_TIMEOUT + 90)
        except subprocess.TimeoutExpired:
            return False, 'ChatGPT 生图超时了'
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or '').strip()[:300]
            return False, (f'ChatGPT 生图失败：{err or "未知错误"}'
                           f'（检查 Chrome 的 OpenCLI 扩展和 chatgpt.com 登录态）')
        files = []
        try:
            rows = json.loads(proc.stdout)
            if isinstance(rows, dict) and isinstance(rows.get('data'), list):
                rows = rows['data']
            for row in (rows if isinstance(rows, list) else [rows]):
                f = (row or {}).get('file') or ''
                if not f:
                    continue
                # opencli 的输出带 📁/✅ 之类的装饰前缀，剥到盘符开头
                m = re.search(r'[A-Za-z]:[\\/].*', f)
                if m:
                    f = m.group(0)
                if os.path.isfile(f):
                    files.append(f)
        except Exception:
            pass
        if not files:
            return False, ('生图完成但没下载到图片文件：'
                           + (proc.stdout or '')[:200])
        try:
            with self.send_lock:
                sent = []
                for f in files:
                    r = chat.SendFiles(f)
                    ok = r is None or (hasattr(r, 'is_success')
                                       and r.is_success)
                    print(f'[ChatGPT] 图片 {os.path.basename(f)} -> '
                          f'{"成功" if ok else r}', flush=True)
                    if ok:
                        sent.append(f)
        except Exception as e:
            return False, (f'图片下载到了但转发出错（{type(e).__name__}: {e}），'
                           f'本地路径：{files[0] if files else "无"}')
        if not sent:
            return False, '图片下载到了，但发到微信群失败了，请改用文字描述'
        return True, (f'已把 ChatGPT 生成的 {len(sent)}/{len(files)} 张图片'
                      f'发到群里，之后正常给文字回答')

    def think(self, chat_id: str, question: str, recent: str = '',
              who: str = '', chat=None) -> tuple:
        """带群上下文跑一轮 agent 循环，返回 (回答, 要@的昵称或 None)。

        提问带上「【昵称】」前缀进上下文和长期记忆，模型因此知道在跟谁
        说话；模型在回答开头写「@昵称」就转成真正的 @ 发出，没有就不 @。
        """
        st = self.memory.setdefault(
            chat_id, {'summary': '', 'turns': deque()})
        self._maybe_compress(chat_id, st)
        q = f'【{who}】{question}' if who else question
        msgs = self._build_messages(st, q, recent)
        answer = self._agent_loop(msgs, chat_id, chat)
        at = None
        if answer.startswith('@'):
            # 优先按已知的发送者全名匹配（昵称可能带空格/emoji，按空白截断
            # 会切坏名字，实测 🍒 ୯ChouZa୬ 这种装饰名就翻车了）
            if who and answer[1:].startswith(who):
                at = who
                answer = answer[1 + len(who):].lstrip(' \u2005，,：:')
            else:
                m = re.match(r'^@(\S{1,32})\s*', answer)
                if m and m.group(1) not in (AT_NAME,):
                    at = m.group(1)
                    answer = answer[m.end():].lstrip()
        st['turns'].append((q, answer))
        return answer, at

    # -- agent 工具循环 ---------------------------------------------------

    def _agent_loop(self, msgs: list, chat_id: str = None,
                    chat=None) -> str:
        """跑工具调用循环，返回最终文本回答（可能为空串=只发了媒体）。

        工具轮次只是临时消息（不进长期记忆，不影响前缀稳定）；接口不支持
        工具或全部节点失败时降级为无工具直答，保证一定能回话。
        send_sticker 发的是真文件（表情/图片），发成功后模型可以只给空文字。
        """
        media_sent = False
        for _ in range(TOOL_ROUNDS):
            try:
                m = self.llm.chat_raw(
                    msgs, tools=[SEARCH_TOOL, CHATGPT_TOOL, CHATGPT_IMAGE_TOOL,
                                 self._sticker_tool()])
            except Exception as e:
                print(f'[agent] 工具请求失败（{e}），降级为无工具直答',
                      flush=True)
                text = self.llm.chat(msgs)
                return text if (text or media_sent) else FAIL_REPLY
            tcs = m.get('tool_calls') or []
            if not tcs:
                text = (m.get('content') or '').strip()
                if not text:
                    return '' if media_sent else FAIL_REPLY
                return text
            msgs.append({'role': 'assistant',
                         'content': m.get('content') or '',
                         'tool_calls': tcs})
            for tc in tcs:
                fn = tc.get('function') or {}
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}
                name = fn.get('name') or ''
                if name == 'send_sticker':
                    result = self._send_sticker(args.get('name') or '', chat)
                    if result.startswith('已发送'):
                        media_sent = True
                elif name == 'ask_chatgpt':
                    result = self._ask_chatgpt(args.get('prompt') or '',
                                               bool(args.get('web_search')),
                                               chat=chat)
                elif name == 'chatgpt_image':
                    result = self._chatgpt_image(args.get('prompt') or '', chat)
                    if result.startswith('已把'):
                        media_sent = True
                else:
                    result = self._exec_tool(name, args, chat_id)
                msgs.append({'role': 'tool',
                             'tool_call_id': tc.get('id') or '',
                             'content': result})
        print('[agent] 工具轮数用尽，强制直接回答', flush=True)
        text = self.llm.chat(msgs)
        return text if (text or media_sent) else FAIL_REPLY

    def _exec_tool(self, name: str, args: dict, chat_id: str = None) -> str:
        t0 = time.time()
        try:
            if name == 'search_group_messages':
                out = self.search_group_messages(
                    args.get('keyword') or '', args.get('member') or '',
                    # 安全边界：搜索永远限定在「提问的这个群」，杜绝把别的
                    # 群的消息混进本群回复（2026-10-29 实测混群事故）
                    scope_username=chat_id)
            else:
                out = f'未知工具：{name}'
        except Exception as e:
            out = f'工具错误：{type(e).__name__}: {e}'
        print(f'[工具] {name} {args} -> {len(out)} 字 '
              f'({time.time() - t0:.1f}s)', flush=True)
        return out

    # -- 群消息搜索工具 ---------------------------------------------------

    def _ensure_db(self):
        if self._db is None:
            from wechatauto.db import WeChatDB
            self._db = WeChatDB()
        return self._db

    def search_group_messages(self, keyword: str = '', member: str = '',
                              group: str = '',
                              scope_username: str = None) -> str:
        """搜索最近 SEARCH_DAYS 天的群聊消息，返回可读文本。

        keyword 匹配消息文本（只搜文本消息），member 匹配发送者昵称/备注/
        wxid。默认扫会话列表里所有群（命令行 --search 用）；bot 回话时传
        ``scope_username``=提问的会话，**只搜这一个群**，杜绝跨群内容漏进
        回复。命中超过 SEARCH_MAX_RESULTS 时保留最新的，返回文本超长时再
        按 SEARCH_RESULT_MAX_CHARS 掐头（都带说明，模型知道被截了）。
        """
        db = self._ensure_db()
        cutoff = time.time() - SEARCH_DAYS * 86400
        kw = (keyword or '').strip().lower()
        mb = (member or '').strip().lower()
        gp = (group or '').strip()

        if scope_username:
            disp = db.get_nickname(scope_username) or scope_username
            rooms = [(scope_username, disp)]
        else:
            rooms = []
            for s in db.get_sessions(limit=SEARCH_ROOM_LIMIT):
                # get_sessions 返回 dict（username/summary/last_time...）
                un = (s.get('username') or '') if isinstance(s, dict) \
                    else (getattr(s, 'username', '') or '')
                if not un.endswith('@chatroom'):
                    continue
                disp = db.get_nickname(un) or un
                if gp and gp not in disp and gp not in un:
                    continue
                rooms.append((un, disp))

        hits = []          # (群显示名, 原始行)
        for un, disp in rooms:
            offset = 0
            while offset < SEARCH_ROOM_PAGE_CAP:
                rows = db.get_messages(un, limit=SEARCH_PAGE, offset=offset)
                if not rows:
                    break
                older = False
                for r in rows:
                    ts = r.get('create_time') or 0
                    if not ts or ts < cutoff:
                        older = True
                        break
                    hits.append((disp, r))
                if older:
                    break
                offset += SEARCH_PAGE

        nick = db.nickname_map()
        out = []           # (时间, 群名, 发送者, 文本)
        for disp, r in hits:
            t = str(r.get('type') or '').lower()
            if t in ('text', '文本'):
                text = _strip_group_prefix(str(r.get('content') or '')).strip()
            else:
                text = f'[{r.get("type")}]'
                if kw:
                    continue          # 关键词只搜文本消息
            sun = r.get('sender_username') or ''
            sender = nick.get(sun) or sun or '未知'
            if mb and mb not in sender.lower() and mb not in sun.lower():
                continue
            if kw and kw not in text.lower():
                continue
            out.append((r.get('create_time') or 0, disp, sender, text))

        out.sort(key=lambda x: x[0])
        total = len(out)
        note = ''
        if total > SEARCH_MAX_RESULTS:
            out = out[-SEARCH_MAX_RESULTS:]
            note = f'（命中 {total} 条，仅保留最新 {SEARCH_MAX_RESULTS} 条）'
        lines = []
        for ts, disp, sender, text in out:
            tm = time.strftime('%m-%d %H:%M', time.localtime(ts))
            lines.append(f'[{tm}] {disp} | {sender}: {text[:100]}')
        body = '\n'.join(lines)
        if len(body) > SEARCH_RESULT_MAX_CHARS:
            keep, size = [], 0
            for ln in reversed(lines):
                if size + len(ln) + 1 > SEARCH_RESULT_MAX_CHARS:
                    break
                keep.append(ln)
                size += len(ln) + 1
            body = '\n'.join(reversed(keep))
            note += f'（结果过长，丢弃最旧 {len(lines) - len(keep)} 条）'
        head = (f'近{SEARCH_DAYS}天群消息搜索：keyword={kw or "-"} '
                f'member={mb or "-"} group={gp or "-"}；'
                f'扫了 {len(rooms)} 个群，命中 {total} 条{note}')
        return head + ('\n' + body if body else '\n（无命中）')

    # -- 微信回调 -------------------------------------------------------

    def on_message(self, msg, chat) -> None:
        try:
            if self.paused:
                return                      # 已因发送失败暂停：只听不回，等人工重启
            self._record(msg, chat)         # 什么消息都先进围观记录
            if getattr(msg, 'attr', '') == 'self':
                return                      # 自己（包括机器人本人）发的不管
            if msg.type not in ('text', '文本'):
                return                      # 只处理文本消息
            text = _strip_group_prefix(msg.content or '').strip()
            chat_id = getattr(chat, 'who', '')
            # 只认 @：没被 @ 的一律不答（围观记录里已经有了，供上下文用）
            if f'@{AT_NAME}' not in text:
                return
            # 去掉 @前缀（微信 @ 后面常跟 U+2005 特殊空格）
            question = text.split(f'@{AT_NAME}', 1)[1]
            question = question.lstrip('\u2005\u2004\u2003 ').strip()
            if not question:
                question = '在吗？'
            recent = self._recent_log(chat_id)

            where = getattr(chat, 'nickname', '') or chat_id
            who = getattr(msg, 'sender', '') or '有人'
            print(f'[收到] {where} | {who}: {text!r}', flush=True)

            t0 = time.time()
            try:
                answer, at = self.think(chat_id, question, recent, who=who,
                                        chat=chat)
            except Exception as e:
                print(f'[llm] 全部节点失败：{e}', flush=True)
                answer, at = FAIL_REPLY, None
            print(f'[回复] ({time.time() - t0:.1f}s) {answer!r}'
                  f'{" [@%s]" % at if at else ""}', flush=True)

            if not answer:
                print('[回复] 媒体已发出，本轮无文字', flush=True)
                return
            with self.send_lock:
                if at:
                    # AI 决定要 @：@ 发送失败（比如成员弹层没匹配上）降级为
                    # 普通发送，这种失败不该触发暂停
                    r = chat.SendMsg(answer, at=at)
                    if r is not None and hasattr(r, 'is_success') \
                            and not r.is_success:
                        print(f'[发送] @发送失败（{r}），降级为普通发送',
                              flush=True)
                        r = chat.SendMsg(answer)
                else:
                    r = chat.SendMsg(answer)
                if r is not None and hasattr(r, 'is_success') \
                        and not r.is_success:
                    # 发送失败（最常见：剪贴板粘贴 6 次未确认）——立即暂停，
                    # 宁可不回话也不再对界面做任何键鼠操作
                    self.paused = True
                    print(f'[!!!] 发送失败：{r}\n'
                          f'[!!!] 机器人已暂停应答（防止继续动键鼠），'
                          f'请检查微信窗口状态后重启程序', flush=True)
        except Exception:
            import traceback
            print('[回调异常]\n' + traceback.format_exc(), flush=True)

    # -- 启停 -----------------------------------------------------------

    def run(self) -> None:
        wx = WeChat()
        r = wx.AddListenAll(self.on_message)
        if r is not None and hasattr(r, 'is_success') and not r.is_success:
            print(f'开启全局监听失败：{r}', flush=True)
            return
        wx.StartListening()
        print(f'大肥鱼已上线：监听所有会话，@{AT_NAME} 触发回答，Ctrl+C 退出', flush=True)
        wx.KeepRunning()


def main() -> None:
    ap = argparse.ArgumentParser(description='大肥鱼微信群 AI 机器人')
    ap.add_argument('--test', metavar='问题', help='只测 LLM 链路，不连微信')
    ap.add_argument('--search', nargs='?', const='', metavar='关键词',
                    help='搜索近3天群消息后退出（不带值=全部）；'
                         '配合 --member/--group')
    ap.add_argument('--member', default='', help='按发送者过滤（配合 --search）')
    ap.add_argument('--group', default='', help='按群名过滤（配合 --search）')
    args = ap.parse_args()

    _setup_logging()
    bot = DaFeiYuBot()
    if args.search is not None:
        print(bot.search_group_messages(args.search, args.member, args.group))
        return
    if args.test is not None:
        q = args.test.lstrip('\u2005 ').strip() or '在吗？'
        print(f'问：{q}')
        answer, at = bot.think('--test--', q, who='测试用户')
        print(f'答：{answer}' + (f'\n（将 @ {at}）' if at else ''))
        return
    bot.run()


if __name__ == '__main__':
    main()
