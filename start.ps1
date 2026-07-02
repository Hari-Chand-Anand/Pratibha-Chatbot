# Pratibha Chatbot — Start Script
# Run this from the project folder: .\start.ps1

$ProjectDir = "C:\Users\ADMIN\Desktop\Pratibha Chatbot"

# 1. Start Docker containers (postgres + pratibha-agent)
Write-Host "Starting Docker containers..."
Set-Location $ProjectDir
docker compose up -d

# 2. Wait for Python agent to be healthy
Write-Host "Waiting for agent to be ready..."
$attempts = 0
do {
    Start-Sleep -Seconds 3
    $attempts++
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8001/health" -UseBasicParsing -ErrorAction Stop
        if ($r.Content -match "ok") { break }
    } catch {}
} while ($attempts -lt 15)

if ($attempts -ge 15) {
    Write-Host "ERROR: Agent did not start in time. Check: docker logs pratibha_agent"
    exit 1
}
Write-Host "Agent is ready."

# 3. Start Node.js backend
Write-Host "Starting Node.js backend..."
Start-Process -FilePath "node" -ArgumentList "server.js" -WorkingDirectory "$ProjectDir\backend" -WindowStyle Normal

Start-Sleep -Seconds 2

# 4. Verify backend
try {
    $b = Invoke-WebRequest -Uri "http://localhost:3002/api/health" -UseBasicParsing -ErrorAction Stop
    Write-Host "Backend is ready."
} catch {
    Write-Host "WARNING: Backend may not have started. Check manually."
}

# 5. Open browser
Write-Host "Opening chatbot..."
Start-Process "http://localhost:3002/pratibha.html"

Write-Host ""
Write-Host "All services running:"
Write-Host "  Chatbot UI  -> http://localhost:3002/pratibha.html"
Write-Host "  Agent API   -> http://localhost:8001/health"
Write-Host "  Postgres    -> localhost:5433"
Write-Host ""
Write-Host "To stop everything, run: .\stop.ps1"
