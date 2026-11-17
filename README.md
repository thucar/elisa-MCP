# elisa-tv-mcp

MCP server that lets an AI agent on your LAN use the **Elisa Elamus** TV guide and control an
**Android / Google TV** (tested: Sony BRAVIA 4K VH21, Android 12).

- **Guide**: the Elisa guide (14 days back, 7 ahead) for the channels in your subscription is kept in a
  local SQLite/FTS5 database, refreshed every 2 hours, and searched in milliseconds
  ("Pilvede all", "the episode before S08E12", "what's on ETV tonight").
- **Play from the guide**: `elisa_play(listing_id)` opens any catch-up or currently airing programme
  directly in the Elisa app and starts it (~10–20 s, also from the TV home screen or a cold app start).
- **TV**: power, volume, remote keys, app launch, reading the screen as text, screenshots (non-DRM)
  — over ADB network debugging.
- **Transport**: streamable HTTP on the LAN (`http://<host>:8765/mcp`), protected by an API key.

## Install

Requirements: Linux with systemd (user services), Python 3.10+ with `venv`, the TV and this machine
on the same LAN, and **network debugging enabled on the TV**
(Settings → System → About → click *Build* 7× → Developer options → *USB debugging* + *Network debugging*).

```bash
git clone https://github.com/thucar/elisa-MCP.git && cd elisa-MCP
./install.sh
```

The wizard:
1. finds `adb` or downloads Google's platform-tools,
2. scans the local network for TVs with ADB open (falls back to asking the IP) and connects —
   **accept "Allow network debugging" on the TV** (tick *always allow*),
3. links your **paying Elisa Elamus account** with a pairing code, exactly like the Elisa TV app:
   it shows a short code and a QR code; scan it with your phone (or open elisaelamus.ee/lisa),
   log in and confirm. The channel list then follows your subscription automatically.
   Skipping this uses guest mode plus a package you pick,
4. generates the API key, downloads the guide, installs and starts the `elisa-tv` systemd user service,
5. prints how to connect an agent, e.g.
   ```bash
   claude mcp add --transport http elisa-tv http://192.168.1.10:8765/mcp --header "Authorization: Bearer <key>"
   ```

Non-interactive: `./install.sh --non-interactive --tv-host 192.168.1.50 --skip-elisa-login --package "Pisike Elamus"`.

## Commands

| | |
|---|---|
| `elisa-tv status` | TV, Elisa login, guide and service health |
| `elisa-tv client-config` | print the URL / API key / client snippets again |
| `elisa-tv elisa-login` | (re)link the Elisa account with a new pairing code |
| `elisa-tv elisa-logout` | forget the account (guest mode) |
| `elisa-tv sync [--force]` | download the guide now |
| `elisa-tv setup` | re-run the wizard (keeps existing settings/key) |
| `journalctl --user -u elisa-tv -f` | server logs |
| `./uninstall.sh [--purge]` | remove (purge also deletes config, tokens, guide) |

Files: config + API key `~/.config/elisa-tv/config.json`; Elisa tokens, guide DB, adb
`~/.local/share/elisa-tv/` (secrets are mode 0600).

## Security notes

- Anyone with the API key can control the TV. HTTP on the LAN is not encrypted — keep it on a trusted
  network (or put it behind a TLS reverse proxy / VPN). Rotate the key by deleting `http.api_key`
  from the config and re-running `elisa-tv setup`.
- The TV trusts this computer's ADB key (`~/.android/adbkey`); revoke under Developer options.
- The Elisa login is a long-lived device token (like the TV app's), stored locally only and used only
  against elisaelamus.ee. It may be listed among your Elisa devices; removing it
  there revokes access.
- Only guide data for subscribed channels is requested from Elisa.

## Tools

Guide: `search_guide`, `series_episodes`, `channel_schedule`, `whats_on`, `list_channels`,
`program_details`, `guide_status`, `refresh_guide`.
Playback: `elisa_play` (catch-up and live, via the guide's listing id), `elisa_tune_channel`
(live TV by channel number or name, ~6 s, confirmed from the on-screen banner).
TV: `tv_status`, `tv_keys`, `tv_type_text`, `tv_power`, `tv_volume`, `tv_launch_app`,
`tv_installed_apps`, `tv_screen`, `tv_screenshot`.

## Known limits

- The Elisa API is the private frontend API of elisaelamus.ee; it may change without notice.
- Screenshots are refused by the TV while DRM video (e.g. Elisa) is on screen; `tv_screen` then
  shows nothing either unless a menu is open.
- `elisa_play` uses the Elisa app's own deep link to a programme's page and presses its play button:
  a new episode plays from the start ("Vaata"), a partly watched one continues ("Jätka"); there is
  no "from the beginning" button for partly watched items.
- Sony TVs send number keys injected by software (`input keyevent`) to Sony's own tuner app. The
  server therefore types digits through a temporary virtual USB keyboard (Android's `hid` tool), which
  the TV treats like a real remote, so `tv_keys` digits and `elisa_tune_channel` behave like the remote.
