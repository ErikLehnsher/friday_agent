$repo = "D:\projects\friday_agent\friday_agent"
$dockerExe = "C:\Program Files\Docker\Docker\resources\bin\docker.exe"
$logFile = Join-Path $repo "deploy.log"

Set-Location $repo

git fetch origin main --quiet 2>&1 | Out-Null

$local = git rev-parse HEAD
$remote = git rev-parse origin/main

if ($local -ne $remote) {
    Add-Content -Path $logFile -Value "$(Get-Date -Format o) - New commit detected ($local -> $remote). Pulling and rebuilding..."
    git pull origin main 2>&1 | Add-Content -Path $logFile
    & $dockerExe compose build 2>&1 | Add-Content -Path $logFile
    & $dockerExe compose up -d 2>&1 | Add-Content -Path $logFile
    Add-Content -Path $logFile -Value "$(Get-Date -Format o) - Deploy finished."
}
