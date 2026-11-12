# 🎧 VoicePlayer — AI開発者のための音声読み上げプレーヤー

<p align="center">
  <img src="web/icon.png" width="128" height="128" alt="VoicePlayer Icon" style="border-radius: 24px;" />
</p>

<p align="center">
  <b>AIコーディング開発者のための無料デスクトップ音声プレーヤー</b><br/>
  <b>Antigravity</b>、<b>Codex</b>、<b>Claude Code</b>、<b>Cursor</b> からの回答やコードを、自然な人間らしいAI音声で読み上げます。<br/>
  Piper TTS による 100% オフライン合成（完全プライベート・API費用不要） + 高品質 Edge TTS。
</p>

<p align="center">
  <a href="https://github.com/infostroper-cloud/voice-player/releases/latest"><img src="https://img.shields.io/github/v/release/infostroper-cloud/voice-player?color=7ee2b8&style=flat-square" alt="Release"/></a>
  <img src="https://img.shields.io/badge/プラットフォーム-Windows%20%7C%20macOS%20%7C%20Linux-brightgreen?style=flat-square" alt="Platforms"/>
  <img src="https://img.shields.io/badge/ライセンス-MIT-blue?style=flat-square" alt="License"/>
  <a href="https://t.me/voiceplayer_ai"><img src="https://img.shields.io/badge/Telegram-@voiceplayer__ai-blue?style=flat-square&logo=telegram" alt="Telegram"/></a>
</p>

---

## 🌐 言語 / Other Languages

* [English](README.md)
* [Русский](README.ru.md)
* [Español](README.es.md)
* [中文](README.zh.md)
* [日本語（現在）](README.ja.md)

---

## ✨ 主な機能

- **🤖 自動会話キャプチャ**: フックとローカルDBを通じて、Antigravity、Codex（デスクトップ & CLI）、Claude Code、Cursor からの出力を自動同期します。
- **⚡ 100% オフライン安心設計**: 組み込み Piper ONNX 音声合成エンジンにより、標準CPUでも **実時間の25倍速** で高速処理。コードやプロンプトが外部サーバーに送信されることはありません。
- **🌍 8言語対応 & 自動言語判定**: 入力テキストの言語を自動で検出し、日本語（Nanami）、英語、スペイン語、中国語、ヒンディー語、ドイツ語、フランス語、ロシア語のネイティブ話者へ瞬時に切り替えます。
- **📊 スマートテーブル & コードカード**: Markdownテーブルを視覚的なカードに変換。文や単語単位のリアルタイムカラオケハイライト表示と、クリックによる直感的なシークが可能です。
- **⌨️ グローバルショートカット**: 画面上の任意のテキスト（PDF、ブラウザ、エディタ）を選択し、`Ctrl+Alt+S`（Macでは `Cmd+Shift+S`）を押すだけで即座に再生。

---

## 📦 インストール方法

### 🪟 Windows (10 / 11)
- **方法 1（推奨・インストーラー）**:  
  👉 **[VoicePlayer-Setup-1.2.0.exe](https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-Setup-1.2.0.exe)**  
  *(Python 3.12 の自動判定・展開・ショートカット登録・起動をワンストップで行います)*
- **方法 2（ポータブル版）**: **[VoicePlayer-Installer-1.2.0.zip](https://github.com/infostroper-cloud/voice-player/releases/latest/download/VoicePlayer-Installer-1.2.0.zip)** をダウンロードして展開し、`Install-VoicePlayer.cmd` を実行。

### 🍎 macOS & 🐧 Linux
ターミナルで以下のワンライナーを実行してください:
```bash
curl -fsSL https://raw.githubusercontent.com/infostroper-cloud/voice-player/main/install.sh | bash
```

---

## 💚 WinRARスタイルの公平なモデル & サポーター Pro

VoicePlayer はすべての開発者に対して **完全無料** で提供されており、機能制限や不快な広告は一切ありません。

長時間の画面凝視から目を休める助けになった場合は、個人の開発者をサポートし、生涯 **Supporter (★ Pro)** バッジを取得できます:
- **Telegram (Tribute)**: [寄付 / Proキーを取得](https://t.me/tribute/app?startapp=dTZW)
- **Boosty**: [Boosty でキーを購入](https://boosty.to/tikoulf/posts/2b4dd6a8-157f-4202-b8db-b4e019ae8a02)
- **公式 Telegram チャンネル**: [@voiceplayer_ai](https://t.me/voiceplayer_ai)
