# Pratibha Chatbot — Stop Script
# Run this from the project folder: .\stop.ps1

$ProjectDir = "C:\Users\ADMIN\Desktop\Pratibha Chatbot"

# Stop Node.js backend
Write-Host "Stopping Node.js backend..."
Stop-Process -Name "node" -Force -ErrorAction SilentlyContinue
Write-Host "Node.js stopped."

# Stop Docker containers
Write-Host "Stopping Docker containers..."
Set-Location $ProjectDir
docker compose down
Write-Host "Docker containers stopped."
