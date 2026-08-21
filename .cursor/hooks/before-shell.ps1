# Minimal beforeShellExecution hook — always allow.
# Cursor expects a single JSON object on stdout.
$payload = [ordered]@{
  permission = "allow"
}
$payload | ConvertTo-Json -Compress
