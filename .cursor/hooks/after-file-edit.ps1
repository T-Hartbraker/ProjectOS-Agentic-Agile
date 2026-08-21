# Minimal afterFileEdit hook — no-op success.
$payload = [ordered]@{
  continue = $true
}
$payload | ConvertTo-Json -Compress
