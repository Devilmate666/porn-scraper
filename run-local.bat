@echo off
pip install -r requirements.txt
start "tunnel" cloudflared tunnel --url http://localhost:8080
python app.py
