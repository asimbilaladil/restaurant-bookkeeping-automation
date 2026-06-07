#!/bin/bash
# Setup script for Revel Fetcher
set -e

echo "=== Revel Fetcher Setup ==="

# Install Python dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium

# Create .env if not exists
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "Created .env — please fill in your REVEL_USER and REVEL_PASS"
fi

echo ""
echo "=== Setup complete ==="
echo "1. Edit .env with your Revel credentials"
echo "2. Run:  python server.py"
echo "3. Open: http://localhost:5050"
