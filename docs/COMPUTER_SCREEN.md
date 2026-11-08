# The screen of this computer

The bighelp app can show the real desktop of the computer that runs Hermes, and
let the person use its mouse and keyboard. This is the computer itself (macOS,
Linux or Windows), not the Bot Desktop that Hermes starts for an agent.

The plugin does not draw the screen. It passes bytes between the app and the
VNC server of the computer. That VNC server must run on the same computer. The
app speaks the VNC protocol (RFB), and it answers the password of the VNC
server itself. The plugin never sees, keeps or logs that password.

Feature: `native-computer-screen-v1`. The feature is in the context list on
every host. The `status` route tells if a VNC server answers.

## Turn on the VNC server

The computer needs a VNC server on `127.0.0.1`, port 5900 (or the port in
`BIGHELP_COMPUTER_SCREEN_PORT`). Set a VNC password.

- **macOS:** System Settings > General > Sharing > Screen Sharing: turn it on.
  Press the (i) button, and turn on "VNC viewers may control screen with
  password". Set the password.
- **Linux:** Use `x11vnc` (X11) or `wayvnc` (Wayland), or the remote desktop of
  your desktop (for example GNOME Remote Desktop) with VNC and a password. For
  example: `x11vnc -display :0 -rfbauth ~/.vnc/passwd -localhost`.
- **Windows:** Install a VNC server such as TightVNC or UltraVNC. Set a
  password. Keep it on port 5900.
- **Hermes in WSL:** The VNC server runs in Windows, and `127.0.0.1` in WSL is
  not Windows. Turn on WSL mirrored networking (`networkingMode=mirrored` in
  `.wslconfig`), so that WSL reaches the VNC server on `127.0.0.1`.

Keep the VNC server on local addresses only. The plugin never connects to
another computer.

## Setting

| Variable | Meaning |
|---|---|
| `BIGHELP_COMPUTER_SCREEN_PORT` | The port of the VNC server on `127.0.0.1`. From 1 to 65535. The default is 5900. A value that is not valid means 5900. |

## Routes

All routes are under `/api/plugins/loopdy/native`. The two `POST` routes take
the usual native headers (`If-Match` context ETag and a lowercase
`X-Loopdy-Request-ID`) and an empty JSON body, `{}`. They have no other
fields.

### `POST /computer-screen/status`

```json
{"available": true, "os": "macos", "reason": null}
```

- `available`: `true` only when a server on the port sends an RFB banner
  (`RFB 003.008`, 12 bytes) in 1 second.
- `os`: `macos`, `linux`, `windows` or `other`. The app uses it to say what to
  turn on.
- `reason`: `null`, `no_server` (nothing listens) or `not_vnc` (a server
  listens, but it is not a VNC server).
- The answer never has the port, an address or a path. It is kept for 2
  seconds.

### `POST /computer-screen/observe`

```json
{"ticket": "k3J...43 characters...", "path": "/api/plugins/loopdy/native/computer-screen/ws"}
```

- The ticket is 32 random bytes, URL-safe. It works one time and for 30
  seconds. At most 16 tickets are open. When there are more, the oldest ticket
  stops.
- `503 computer_screen_unavailable`: no VNC server answers (or the feature is
  not in the context list).

### WebSocket `/computer-screen/ws?ticket=<ticket>`

The app opens it with the ticket from `observe`.

The plugin accepts the socket, then checks the ticket (constant-time compare;
the ticket is spent at once). Then it connects to the VNC server, checks that
it sends an RFB banner, and sends the banner as the first message. After that,
every byte goes both ways as binary messages of at most 64 KB.

The plugin closes the socket with these codes:

| Code | Meaning |
|---|---|
| 1008 | The ticket is missing, wrong, used or too old. Call `observe` again. |
| 1013 | 4 viewers are open already. Try again later. |
| 1011 | No VNC server answers, or it is not a VNC server. |
| 1003 | The app sent a text message. RFB is binary. |
| 1000 | The VNC server ended the connection, or the app left. |

When one side ends, the plugin closes the other side.

### What the app must send on the handshake

Hermes puts its own gate in front of every plugin WebSocket
(`PluginApiWebSocketGate`). HTTP checks of the dashboard do not run for a
WebSocket upgrade, so the ticket is the only credential. The gate checks only:

- `Host`: the host and port that the app used for the other plugin calls (the
  same host check as HTTP). Any other `Host` is closed with 4403 before the
  upgrade, and the client sees an HTTP 403.
- `Origin`: the app sends none. If it sends one, it must be the same host as
  `Host`.
- The peer address: on a dashboard that binds to loopback, only a local peer
  passes. A signed-in dashboard (gated mode) or a dashboard on a public bind
  accepts any peer.
- `X-Hermes-Gateway-Ticket`: the app must not send this header on the upgrade.
  When it is there, Hermes spends it for the upgrade, and it must be a fresh
  grant for this plugin.
- No cookie, bearer token or `?token=` is needed, and no `profile` query.

## Security

- To get a ticket, the app must pass the same dashboard sign-in as every other
  plugin call. Only then can the plugin hand out a ticket.
- To see the screen, the app also needs the VNC password of the computer.
  Someone with only the dashboard sign-in cannot use the screen.
- The plugin connects only to `127.0.0.1`, on the one port from the setting.
  The app cannot choose an address or a port.
- At most 4 viewers at one time. A viewer ends when either side ends.
- Logs have fixed words only: never the port, an address, a ticket or a
  password.
- RFB without TLS goes over the connection between the app and the dashboard,
  which is the connection that the app uses for all plugin calls. Keep the
  dashboard on a connection that is encrypted (for example HTTPS) when the
  phone is not on the same local network.
- When a second profile runs the plugin in its own plugin host, Hermes does not
  carry plugin WebSockets for it yet (it answers HTTP 501). Use the screen from
  the profile that the dashboard runs.
