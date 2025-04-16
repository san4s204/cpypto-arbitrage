#!/bin/bash

# Start script for Crypto Arbitrage Bot
# This script helps with starting the bot in development mode

# Check if Docker and Docker Compose are installed
if ! command -v docker &> /dev/null || ! command -v docker-compose &> /dev/null; then
    echo "Docker and/or Docker Compose not found. Please install them first."
    echo "You can run ./deploy.sh to install them automatically."
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

# Start containers in development mode
echo "Starting Crypto Arbitrage Bot in development mode..."
docker-compose up

# This script will keep running until Ctrl+C is pressed
# The logs will be displayed in the terminal
