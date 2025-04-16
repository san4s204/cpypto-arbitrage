#!/bin/bash

# Deployment script for Crypto Arbitrage Bot
# This script helps with deploying the bot to a production environment

# Check if Docker and Docker Compose are installed
if ! command -v docker &> /dev/null || ! command -v docker-compose &> /dev/null; then
    echo "Docker and/or Docker Compose not found. Installing..."
    
    # Update package lists
    sudo apt-get update
    
    # Install prerequisites
    sudo apt-get install -y apt-transport-https ca-certificates curl software-properties-common
    
    # Add Docker's official GPG key
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -
    
    # Add Docker repository
    sudo add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable"
    
    # Update package lists again
    sudo apt-get update
    
    # Install Docker
    sudo apt-get install -y docker-ce
    
    # Install Docker Compose
    sudo curl -L "https://github.com/docker/compose/releases/download/v2.18.1/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
    sudo chmod +x /usr/local/bin/docker-compose
    
    echo "Docker and Docker Compose installed successfully."
else
    echo "Docker and Docker Compose are already installed."
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

# Check if user wants to run in production or development mode
read -p "Deploy in production mode? (y/n): " production_mode

if [ "$production_mode" = "y" ] || [ "$production_mode" = "Y" ]; then
    echo "Deploying in production mode..."
    
    # Pull latest changes if in a git repository
    if [ -d .git ]; then
        git pull
    fi
    
    # Build and start containers in production mode
    docker-compose -f docker-compose.yml up -d --build
    
    echo "Waiting for services to start..."
    sleep 10
    
    # Initialize database
    docker-compose exec -T market_data python -c "from common.database import init_db; import asyncio; asyncio.run(init_db())"
    
    echo "Deployment completed successfully!"
    echo "Dashboard available at: http://$(hostname -I | awk '{print $1}'):8000"
else
    echo "Deploying in development mode..."
    
    # Build and start containers in development mode with logs
    docker-compose up --build
fi

# Display container status
echo "Container status:"
docker-compose ps
