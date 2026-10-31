# wechat-deepseek —— 微信大肥鱼 DeepSeek

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![License](https://img.shields.io/github/license/dazhi666666/wechat-deepseek)
![GitHub stars](https://img.shields.io/github/stars/dazhi666666/wechat-deepseek)

把本地大模型（DeepSeek / 任意 OpenAI 兼容接口）接进微信：一个开箱即用的
**微信群聊 AI 机器人**，加上一套微信 4.x Windows 客户端自动化库
（基于 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica) 二次开发，Apache-2.0）。

## 🐳 大肥鱼：微信群 AI 机器人

`dafeiyu_bot.py` 监听所有会话，别人在群里 @ 它就把问题发给你自建的 LLM，
回答直接发回群里。

### 功能

- **@ 触发 + 免 @ 跟进**：@ 一次后 5 分钟内连续对话不用再 @，冷场自动退出；别人 @ 其他人的消息不抢答
- **群聊长期记忆**：每个群的对话无限累积，估算上下文到 100K token 自动把最老的历史压成摘要（最近几轮原文保留）（可以改）
- **群消息搜索工具**：LLM 可自主调用，搜索**本群**最近 3 天的消息——按关键词、按成员（昵称/备注/wxid），为空返回全部，上限 2000 条；严格限定当前群，杜绝跨群内容串台
- **多节点粘性故障转移**：多个 LLM 节点粘住一台用（保 prefix cache 命中），报错/乱码才切换，切完继续粘
- **纯文本友好**：回复自动禁 Markdown（`**加粗**`、`- 列表`、代码围栏在微信里就是乱码）
- **安全防护**：任何键盘操作前必须确认前台是微信；剪贴板粘贴多次失败直接**暂停机器人**而不是盲打拼音；发送失败只听不回，等人工重启
- **日志落盘**：`dafeiyu_bot.log`，每行带时间戳，5MB 自动轮转

### 安装

```bash
git clone https://github.com/dazhi666666/wechat-deepseek.git
cd wechat-deepseek
pip install -e .
pip install pypinyin        # 可选
```

要求：Windows 10/11、**64 位** Python 3.9+、微信 4.x（4.1.12+）已登录。

### 配置与运行

LLM 通过环境变量配置（OpenAI 兼容 `/v1/chat/completions`）：

| 环境变量 | 说明 | 示例 |
|---|---|---|
| `WECHAT_DEEPSEEK_API_HOSTS` | LLM 节点，逗号分隔做故障转移 | `10.0.0.14,10.0.0.11` |
| `WECHAT_DEEPSEEK_API_PORT` | 端口 | `8000` |
| `WECHAT_DEEPSEEK_API_KEY` | API Key（本地部署可随意） | `EMPTY` |
| `WECHAT_DEEPSEEK_MODEL` | 模型名 | `deepseek-v4` |

```bash
python dafeiyu_bot.py                    # 启动机器人，群里 @大肥鱼 触发
python dafeiyu_bot.py --test "在吗"      # 只测 LLM 链路，不连微信
python dafeiyu_bot.py --search "关键词"  # 命令行搜近 3 天群消息（可配 --member/--group）
```

人设、触发词、记忆阈值等都在 `dafeiyu_bot.py` 顶部「配置区」里改。

### 使用前提

- 回复需要驱动微信窗口：**桌面解锁、微信可见**（收消息锁屏也能跑）
- 微信重启后 UIA 控件树会自动热激活，无需手动干预
- 记忆在内存里，重启程序后从零开始

## 库本体（wechatauto）

机器人底层的微信 4.x 自动化能力继承自上游：本地 SQLCipher 解密读取消息/
联系人/朋友圈、UIA+OCR 混合发送、实时监听、媒体下载。命令行工具：

```bash
wechatauto          # 体检 + 交互菜单
wechatauto sessions # 列出会话
wechatauto doctor   # 账号 / 密钥 / 控件树自检
```

详细文档见 [GUIDE.md](GUIDE.md) 与 [docs/技术文档.md](docs/技术文档.md)。

## 致谢与许可

- 上游 [wechatauto-replica](https://github.com/fanyuantaier/wechatauto-replica)（wxauto 复刻版）
- [Apache-2.0](LICENSE) 许可证
