@echo off
cd /d "%~dp0"

rem ============================================================
rem  大肥鱼机器人一键启动
rem  改下面四行成你自己的 LLM 配置，然后双击本文件即可。
rem  1) 本地服务：HOSTS=127.0.0.1  PORT=8000
rem  2) DeepSeek 官方：HOSTS=https://api.deepseek.com  MODEL=deepseek-chat
rem  3) 中转站：HOSTS=https://你的地址  MODEL=对方给的模型名
rem ============================================================
set "WECHAT_DEEPSEEK_API_HOSTS=127.0.0.1"
set "WECHAT_DEEPSEEK_API_PORT=8000"
set "WECHAT_DEEPSEEK_API_KEY=EMPTY"
set "WECHAT_DEEPSEEK_MODEL=deepseek-v4"

set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" dafeiyu_bot.py %*

echo.
echo 程序已退出，按任意键关闭窗口...
pause >nul
