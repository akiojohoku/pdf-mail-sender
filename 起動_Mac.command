#!/bin/bash
# PDF個別メール送信システム 起動スクリプト(Mac用)
# このファイルをダブルクリックすると起動します。
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 が見つかりません。https://www.python.org/downloads/ からインストールしてください。"
  read -p "Enterキーで閉じます..."
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "初回セットアップ中です(1〜2分かかります)..."
  python3 -m venv .venv || { read -p "セットアップに失敗しました。Enterキーで閉じます..."; exit 1; }
  ./.venv/bin/pip install --quiet -r requirements.txt || {
    echo "ライブラリのインストールに失敗しました。ネット接続を確認してください。"
    rm -rf .venv
    read -p "Enterキーで閉じます..."
    exit 1
  }
  echo "セットアップが完了しました。"
fi

./.venv/bin/python app.py
read -p "Enterキーで閉じます..."
