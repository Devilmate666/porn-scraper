#!/bin/sh
pip install -r requirements.txt
cloudflared tunnel --url http://localhost:8080 &
python app.py
