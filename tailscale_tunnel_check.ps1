# Tailscale tunnel watchdog -- runs every 5 min via schtasks (dafeiyu-tailscale-tunnel).
# The existing dafeiyu-watchdog only checks that the Tailscale SERVICE is running;
# this one checks the tunnel actually works (can we reach the Muse VM?).
# If the tunnel is dead, restart the Tailscale service (max 1 restart per 30 min).
# Never restart anything else. Log everything.

$logFile = "C:\dafeiyu-open\tailscale_tunnel.log"
$stateFile = "C:\dafeiyu-open\tailscale_tunnel_state.txt"
$vmIp = "100.109.80.58"
$tailscaleExe = "C:\Program Files\Tailscale\tailscale.exe"

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logFile -Value "[$ts] $msg" -Encoding UTF8
    # rotate: keep log under ~200KB
    try {
        if ((Get-Item $logFile).Length -gt 200KB) {
            $lines = Get-Content $logFile -Tail 500
            Set-Content -Path $logFile -Value $lines -Encoding UTF8
        }
    } catch { }
}

# 1. Ping the VM through the tunnel.
$pingOk = $false
try { $pingOk = Test-Connection -ComputerName $vmIp -Count 1 -Quiet } catch { $pingOk = $false }
if ($pingOk) { Write-Log "tunnel OK (ping $vmIp)"; exit 0 }

# 2. Ping failed -- is the peer actually up according to tailscale?
$peerActive = $false
try {
    $status = & $tailscaleExe status 2>$null
    if ($status -match "100.109.80.58.*active") { $peerActive = $true }
} catch { $peerActive = $false }
if ($peerActive) { Write-Log "ping failed but tailscale shows muse peer active; no restart"; exit 0 }

# 3. Tunnel looks dead -- cooldown: at most one restart per 30 minutes.
$now = Get-Date
if (Test-Path $stateFile) {
    try {
        $last = [datetime]::Parse((Get-Content $stateFile -Raw).Trim())
        if (($now - $last).TotalMinutes -lt 30) {
            Write-Log "tunnel DOWN but restarted less than 30 min ago; skip"
            exit 0
        }
    } catch { }
}

Write-Log "tunnel DOWN (ping fail + peer not active); restarting Tailscale service"
try {
    Restart-Service -Name Tailscale -Force -ErrorAction Stop
    Write-Log "Tailscale service restart issued"
} catch {
    Write-Log "Tailscale service restart FAILED: $($_.Exception.Message)"
    exit 0
}
Start-Sleep -Seconds 30
$pingOk2 = $false
try { $pingOk2 = Test-Connection -ComputerName $vmIp -Count 1 -Quiet } catch { $pingOk2 = $false }
if ($pingOk2) { Write-Log "tunnel BACK after restart" } else { Write-Log "tunnel STILL DOWN after restart" }
Set-Content -Path $stateFile -Value ($now.ToString("o")) -Encoding UTF8
