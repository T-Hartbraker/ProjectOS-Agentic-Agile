# Local operator startup

Normal use is one start, not four terminals.

## Desktop app shortcut

From the repository root, once:

```powershell
.\scripts\install-desktop-shortcut.ps1
```

That creates:

- Desktop: **ProjectOS**
- Start Menu: **ProjectOS** and **Stop ProjectOS**

Double-click **ProjectOS**. It starts the API and daemon, waits until they answer, then opens `http://127.0.0.1:8787`. The built dashboard is served from that same address. A black console should not stay on screen.

If the dashboard never appears, health is not painted green. Run `python -m projectos status` or check `logs\operator`.

## One-command start

From the repository root:

```powershell
.\scripts\start-operator.ps1
```

This starts:

| Component | How | When |
|-----------|------|------|
| API | `python -m projectos api` on `127.0.0.1:8787` | always |
| Dashboard | built `web/dist` served by the API on `127.0.0.1:8787` | `dashboard.enabled` (Vite only if `web/dist` is missing) |
| Daemon | `python -m projectos daemon run` | `daemon.enabled` |
| Slack adapter | `python -m projectos.slack_adapter` | only if `slack_adapter.enabled` is true |

Stay attached (stop children on Ctrl+C):

```powershell
.\scripts\start-operator.ps1 -Wait
```

Stop:

```powershell
.\scripts\stop-operator.ps1
```

Equivalent CLI:

```text
python -m projectos start
python -m projectos start --wait
python -m projectos status
python -m projectos stop
```

## Config

`config/operator.json` controls bind addresses and which children are required. Slack adapter is on when `slack_adapter.enabled` is `true`. Incoming chat still uses the `/projectos` slash command through a public HTTPS tunnel.

Logs: `logs/operator/*.log`  
PID files: `state/run/*.pid`

## Health

`GET /health` and `GET /v1/health` return component readiness:

- `status` is `ok` only when every **enabled** component is ready.
- `ready` is false when a required component is stopped or in error.
- `notice` names the failing pieces.
- The dashboard header shows each component. A stopped daemon or dashboard is not painted as healthy.

`python -m projectos status` prints the same snapshot.

## Windows Task Scheduler

Optional logon start (same strategy as a local Windows service wrapper):

```powershell
.\scripts\install-operator-task.ps1
```

Remove:

```powershell
.\scripts\uninstall-operator-task.ps1
```

The task runs `start-operator.ps1 -Detach`. It does not hide child failures; `/v1/health` still reports stopped or error components.

## Manual fallback

If you must run pieces yourself:

```text
python -m projectos api --host 127.0.0.1 --port 8787
python -m projectos daemon run
Set-Location web; npm run dev
python -m projectos.slack_adapter
```
