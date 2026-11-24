# Tailscale tunnel watchdog -- runs every 5 min via schtasks (dafeiyu-tailscale-tunnel).
# The existing dafeiyu-watchdog only checks that the Tailscale SERVICE is running;
# this one checks the tunnel actually works (can we reach the Muse VM?).
# If the tunnel is dead, restart the Tailscale service (max 1 restart per 30 min).
# Never restart anything else. Log everything.
#
# BUG FIX 2026-11-24: the old version never restarted when the ping failed
# but `tailscale status` still showed the muse peer as "active" -- and the
# peer shows "active" even when the data path is broken (campus WiFi
# throttles UDP; control plane stays up, pings die). On 2026-11-23 the ping
# failed 6 times in a row (14:31-14:56) and the watchdog did nothing.
# Fix: count consecutive ping failures; restart after 2 in a row (10 min),
# even if the peer still shows active. The ping is the ground truth.

$logFile = "C:\dafeiyu-open\tailscale_tunnel.log"
$stateFile = "C:\dafeiyu-open\tailscale_tunnel_state.txt"
$failFile = "C:\dafeiyu-open\tailscale_tunnel_fails.txt"
$vmIp = "100.109.80.58"
$tailscaleExe = "C:\Program Files\Tailscale\tailscale.exe"
$failThreshold = 2   # consecutive ping failures before a restart

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

function Get-FailCount {
    try {
        $raw = (Get-Content $failFile -Raw -ErrorAction Stop).Trim()
        return [int]$raw
    } catch { return 0 }
}

function Set-FailCount($n) {
    try { Set-Content -Path $failFile -Value ([string]$n) -Encoding UTF8 } catch { }
}

# 1. Ping the VM through the tunnel.
$pingOk = $false
try { $pingOk = Test-Connection -ComputerName $vmIp -Count 1 -Quiet } catch { $pingOk = $false }
if ($pingOk) {
    Set-FailCount 0
    Write-Log "tunnel OK (ping $vmIp)"
    exit 0
}

# 2. Ping failed -- count consecutive failures.
$fails = (Get-FailCount) + 1
Set-FailCount $fails

# 3. Is the peer up according to tailscale? (Unreliable when the data path
#    is broken -- the peer can show "active" while pings die -- so it only
#    triggers an IMMEDIATE restart when the peer is gone; it never vetoes
#    a restart after repeated failures.)
$peerActive = $false
try {
    $status = & $tailscaleExe status 2>$null
    if ($status -match "100.109.80.58.*active") { $peerActive = $true }
} catch { $peerActive = $false }

$needRestart = $false
$reason = ""
if (-not $peerActive) {
    $needRestart = $true
    $reason = "ping fail + peer not active"
} elseif ($fails -ge $failThreshold) {
    $needRestart = $true
    $reason = "ping failed $fails times in a row (peer still shows active)"
} else {
    Write-Log "ping failed (1st in a row), peer active; will restart if it fails again"
    exit 0
}

# 4. Cooldown: at most one restart per 30 minutes.
$now = Get-Date
if (Test-Path $stateFile) {
    try {
        $last = [datetime]::Parse((Get-Content $stateFile -Raw).Trim())
        if (($now - $last).TotalMinutes -lt 30) {
            Write-Log "tunnel DOWN ($reason) but restarted less than 30 min ago; skip"
            exit 0
        }
    } catch { }
}

Write-Log "tunnel DOWN ($reason); restarting Tailscale service"
try {
    Restart-Service -Name Tailscale -Force -ErrorAction Stop
    Write-Log "Tailscale service restart issued"
} catch {
    Write-Log "Tailscale service restart FAILED: $($_.Exception.Message)"
    exit 0
}
Set-FailCount 0
Start-Sleep -Seconds 30
$pingOk2 = $false
try { $pingOk2 = Test-Connection -ComputerName $vmIp -Count 1 -Quiet } catch { $pingOk2 = $false }
if ($pingOk2) { Write-Log "tunnel BACK after restart" } else { Write-Log "tunnel STILL DOWN after restart" }
Set-Content -Path $stateFile -Value ($now.ToString("o")) -Encoding UTF8
