#!/bin/bash
export DISPLAY=:99
export XAUTHORITY=/tmp/xvfb-run.pR8mnD/Xauthority
cd /opt/accounts/app
exec /opt/accounts/app/venv/bin/python3 server.py
