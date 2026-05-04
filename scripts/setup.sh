#!/usr/bin/env bash
# Script to setup python environment for auto-travel-blogger
python3 -m venv venv
source venv/bin/activate
pip install requests beautifulsoup4
echo "Môi trường đã được cài đặt. Kích hoạt bằng lệnh: source venv/bin/activate"
