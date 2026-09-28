#!/bin/bash
set -e

echo "======================================"
echo " Facebook UID Monitor VPS Installer"
echo "======================================"

if ! command -v python3 >/dev/null 2>&1; then
    echo "Python3 not found."

    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-pip python3-venv
    else
        echo "Please install Python 3 manually, then run this script again."
        exit 1
    fi
fi

PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 python3 -m pip install --no-cache-dir -r requirements.txt

echo ""
echo "Installation complete."
echo ""
echo "Set your NEW Telegram bot token:"
echo 'export BOT_TOKEN="YOUR_NEW_BOT_TOKEN"'
echo 'export ADMIN_USER_ID="6656858850"'
echo 'export ALLOWED_CHAT_ID="6656858850"'
echo ""
echo "Then run:"
echo "python3 facebook_uid_monitor.py"
