#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3.10 이상을 먼저 설치해 주세요."
  read -r -p "Enter를 누르면 닫습니다. "
  exit 1
fi
python3 ui.py
if [ "$?" -ne 0 ]; then
  read -r -p "오류를 확인한 뒤 Enter를 누르면 닫습니다. "
fi
