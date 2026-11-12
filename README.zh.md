# 🎧 VoicePlayer — 专为开发者打造的 AI 语音播报播放器

<p align="center">
  <img src="web/icon.png" width="128" height="128" alt="VoicePlayer Icon" style="border-radius: 24px;" />
</p>

<p align="center">
  <b>面向 AI 开发者的免费桌面级语音助手</b><br/>
  将来自 <b>Antigravity</b>、<b>Codex</b>、<b>Claude Code</b> 和 <b>Cursor</b> 的回答与代码转化为自然流畅的人声播报。<br/>
  采用内置 Piper TTS 实现 100% 离线合成（零 API 费用、保护隐私） + 微软 Edge 神经网络语音。
</p>

<p align="center">
  <a href="https://github.com/infostroper-cloud/voice-player/releases/latest"><img src="https://img.shields.io/github/v/release/infostroper-cloud/voice-player?color=7ee2b8&style=flat-square" alt="Release"/></a>
  <img src="https://img.shields.io/badge/平台-Windows%20%7C%20macOS%20%7C%20Linux-brightgreen?style=flat-square" alt="Platforms"/>
  <img src="https://img.shields.io/badge/开源协议-MIT-blue?style=flat-square" alt="License"/>
  <a href="https://t.me/voiceplayer_ai"><img src="https://img.shields.io/badge/Telegram-@voiceplayer__ai-blue?style=flat-square&logo=telegram" alt="Telegram"/></a>
</p>

---

## 🌐 语言版本 / Other Languages

* [English](README.md)
* [Русский](README.ru.md)
* [Español](README.es.md)
* [中文（当前）](README.zh.md)
* [日本語](README.ja.md)

---

## ✨ 核心特性

- **🤖 自动捕获对话**：通过文件钩子与本地数据库，自动同步 Antigravity、Codex（桌面端及 CLI）、Claude Code 和 Cursor 的最新输出。
- **⚡ 100% 离线隐私保护**：内置 Piper ONNX 语音合成引擎，在普通 CPU 上可达 **25 倍实时速度**。您的代码和核心提示词绝不上传至任何云端。
- **🌍 8 种语言与自动语种识别**：智能检测输入文本语言，无缝切换中文、英语、日语、西班牙语、德语、法语、俄语和印地语原生发音人。
- **📊 智能表格与代码卡片**：将 Markdown 表格自动转为直观排版的卡片，提供句子与单词级逐字卡拉 OK 高亮，点击任意位置即可精准跳转。
- **⌨️ 全局快捷键朗读**：在屏幕上选中任何文本（PDF、网页、代码编辑器），按 `Ctrl+Alt+S`（Mac 上为 `Cmd+Shift+S`）即可即刻聆听。

---

## 📦 安装方法

### 🪟 Windows (10 / 11)
- **推荐方案（图形安装程序）**：
  👉 **[VoicePlayer-Setup-1.2.0.exe](https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-Setup-1.2.0.exe)**  
  *(自动静默检测 Python 3.12、解压文件、生成桌面与开始菜单快捷方式并直接启动)*
- **便携版**：下载 **[VoicePlayer-Installer-1.2.0.zip](https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-Installer-1.2.0.zip)**，解压后双击运行 `Install-VoicePlayer.cmd`。

### 🍎 macOS 与 🐧 Linux
在终端运行此单行命令即可快速完成安装：
```bash
curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/install.sh | bash
```

---

## 💚 经典的 Supporter 友好共享模式与 Pro 赞助徽章

VoicePlayer 对所有开发者 **完全免费**，没有任何功能阉割，也不包含烦人的弹窗广告。

如果您喜欢 VoicePlayer 带来的阅读体验与护眼便利，欢迎赞助独立作者并解锁终身 **Supporter (★ Pro)** 徽章：
- **Telegram (Tribute)**: [赞助 / 获取 Pro 激活码](https://t.me/tribute/app?startapp=dTZW)
- **Boosty**: [在 Boosty 购买专属赞助激活码](https://boosty.to/tikoulf/posts/2b4dd6a8-157f-4202-b8db-b4e019ae8a02)
- **官方 Telegram 频道**: [@voiceplayer_ai](https://t.me/voiceplayer_ai)
