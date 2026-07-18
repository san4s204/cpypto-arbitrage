#!/bin/bash

set -euo pipefail

if ! command -v docker &> /dev/null || ! docker compose version &> /dev/null; then
    echo "Docker with the Compose plugin is required."
    exit 1
fi

# Check if .env file exists
if [ ! -f .env ]; then
    echo "No .env file found. Creating from sample..."
    
    if [ -f .env.sample ]; then
        cp .env.sample .env
        echo ".env file created. Please edit it with your configuration before continuing."
        exit 1
    else
        echo "Error: .env.sample file not found. Please create a .env file manually."
        exit 1
    fi
fi

echo "Starting Strategy Lab in paper mode..."
docker compose up --build
