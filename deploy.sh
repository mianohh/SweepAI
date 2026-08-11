#!/usr/bin/env bash
# SweepAI Deployment Script for Google Cloud
# Usage: bash deploy.sh

set -euo pipefail

echo "=== SweepAI Deployment Script ==="
echo ""

# Check if running as root
if [[ $EUID -eq 0 ]]; then
   echo "ERROR: Do not run this script as root"
   exit 1
fi

# Step 1: System updates
echo "[1/7] Updating system packages..."
sudo apt update -qq && sudo apt upgrade -y -qq

# Step 2: Install dependencies
echo "[2/7] Installing dependencies..."
sudo apt install -y -qq python3 python3-pip python3-venv git curl

# Step 3: Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

# Step 4: Setup project directory
echo "[3/7] Setting up SweepAI..."
if [ ! -d "$HOME/SweepAI" ]; then
    git clone https://github.com/mianohh/SweepAI.git "$HOME/SweepAI"
fi
cd "$HOME/SweepAI"

# Step 5: Create virtual environment
echo "[4/7] Creating virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# Step 6: Install SweepAI
echo "[5/7] Installing SweepAI..."
pip install --upgrade pip -q
pip install -e . -q

# Step 7: Verify installation
echo "[6/7] Verifying installation..."
if command -v sweepai &> /dev/null; then
    echo "✓ sweepai CLI installed"
else
    echo "✗ sweepai CLI not found"
    exit 1
fi

# Step 8: Check for config files
echo "[7/7] Checking configuration..."
if [ ! -f ".env" ]; then
    echo "⚠ .env file not found"
    echo "  Copy from .env.example and add your KEEPERHUB_API_KEY:"
    echo "    cp .env.example .env"
    echo "    nano .env"
fi

if [ ! -f "config/config.toml" ]; then
    echo "⚠ config/config.toml not found"
    echo "  Copy from config/config.example.toml:"
    echo "    cp config/config.example.toml config/config.toml"
    echo "    nano config/config.toml"
fi

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "Next steps:"
echo "  1. Configure .env with your KEEPERHUB_API_KEY"
echo "  2. Configure config/config.toml with your wallet settings"
echo "  3. Run: sweepai doctor"
echo "  4. Run: sweepai observe"
echo "  5. Install service: sudo cp sweepai.service /etc/systemd/system/"
echo "  6. Enable service: sudo systemctl enable sweepai && sudo systemctl start sweepai"
