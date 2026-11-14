# Omatinuity

A bar widget for [Tether](https://github.com/zackb/tether), iPhone ↔ Linux continuity in the
Omarchy bar: read and reply to messages, answer and place calls, accept pairing requests, and
send the clipboard, copied files or the latest screenshot to the phone.

Built to the Omarchy plugin contract (<https://plugins.omarchy.org/develop.html>): one
`bar-widget` kind, `BarWidget.qml` as the entry point, and `Panel.qml` loaded by it.

## Files

```
io.github.sgatter.omatinuity/
├── manifest.json        bar-widget, entry point BarWidget.qml
├── BarWidget.qml        bar icon, Tether service, panel lifecycle, shell IPC
├── Panel.qml            details panel (KeyboardPanel, keyboard navigation)
├── Service.qml          state, polling and actions (shared by icon and panel)
├── backend.py           talks to tetherd over $XDG_RUNTIME_DIR/tether/tetherd.sock
├── bin/tether-gtk       notification-click redirect (see below)
└── setup-bluetooth.sh   one-time Bluetooth setup for messages and calls
```

## Dependencies and privileges

- An iPhone with the Tether app, on the same Wi-Fi as the computer.
- `tether` (AUR) for `tetherd`; `python3`; `wl-clipboard`; `libnotify` (`notify-send`);
  `util-linux` (`flock`).
- The widget runs unsandboxed in the Omarchy shell with your user permissions. It only talks
  to your own `tetherd` socket and runs `systemctl --user start|stop tetherd`.
- `setup-bluetooth.sh` uses `sudo` (bluetoothd flag, adapter class service). Read it first.

## Install

```sh
yay -S tether
systemctl --user enable --now tetherd.service
sudo ufw allow 5134/tcp && sudo ufw allow 5353/udp
omarchy plugin add https://github.com/sgatter/omatinuity.git --enable
```

Then open the Tether app on the iPhone and accept the pairing request from the panel.

### Messages and calls (Bluetooth)

Run `~/.config/omarchy/plugins/io.github.sgatter.omatinuity/setup-bluetooth.sh` once (or
"Set up Bluetooth" in the panel). It:

1. runs `bluetoothd --experimental` (`/etc/systemd/system/bluetooth.service.d/experimental.conf`,
   restarts Bluetooth),
2. enables `tether-btclass@hci0` so the adapter presents as A/V Hands-Free,
3. drops PipeWire's `hfp_hf` role (`~/.config/wireplumber/wireplumber.conf.d/tether-no-hfp-hf.conf`)
   so BlueZ's Telephony API gets the iPhone's hands-free link,
4. turns on call control (`tether bt calls-enable on`).

Then pair with `tether bt devices` / `tether bt pair <address>` and, on the iPhone, enable
"Show Notifications" and "Sync Contacts" for this computer. Call audio stays on the iPhone.

### Notification clicks open the panel (optional)

Clicking a Tether message notification opens the Tether app. To open that conversation in this
panel instead, put this plugin's `bin/` first on tetherd's PATH (and only tetherd's):

```sh
mkdir -p ~/.config/systemd/user/tetherd.service.d
cat > ~/.config/systemd/user/tetherd.service.d/omarchy-panel.conf <<'EOF'
[Service]
Environment=PATH=%h/.config/omarchy/plugins/io.github.sgatter.omatinuity/bin:/usr/share/omarchy/bin:/usr/local/sbin:/usr/local/bin:/usr/bin
EOF
systemctl --user daemon-reload && systemctl --user restart tetherd
```

tetherd launches `tether-gtk --thread=<key>` for a click; `bin/tether-gtk` turns that into
`omarchy-shell io.github.sgatter.omatinuity openThread <key>` and passes anything else to
`/usr/bin/tether-gtk`.

## Usage

Left click toggles the panel, middle click answers a ringing call (otherwise sends the
clipboard), right click refreshes. In the panel: arrows + Enter, `p` power, `c` clipboard,
`f` copied files, `s` screenshot, `n` call or message, `a` answer (or accept a pairing
request), `d` hang up, `r` refresh, Tab to the neighbouring panel, Esc back/close.

The bar draws one copy per monitor; call and pairing notifications are shown once.

## Configure

```sh
omarchy bar move io.github.sgatter.omatinuity --after omarchy.tray
```

Settings (inline on the widget's entry in `~/.config/omarchy/shell.json`):
`refreshIntervalSec` (default 10) and `notifyPairing` (default true).

## Shell commands

```sh
omarchy-shell shell summon io.github.sgatter.omatinuity '{}'
omarchy-shell shell hide io.github.sgatter.omatinuity
omarchy-shell io.github.sgatter.omatinuity sendClipboard
omarchy-shell io.github.sgatter.omatinuity sendScreenshot
omarchy-shell io.github.sgatter.omatinuity send /path/to/file
omarchy-shell io.github.sgatter.omatinuity answer
omarchy-shell io.github.sgatter.omatinuity hangup
omarchy-shell io.github.sgatter.omatinuity dial +15551234567
omarchy-shell io.github.sgatter.omatinuity dialer       # open the call-or-message picker
omarchy-shell io.github.sgatter.omatinuity openLatest   # open the most recent conversation
```

Example in `~/.config/hypr/bindings.lua`:
`o.bind("SUPER + ALT + I", "Send screenshot to iPhone", "omarchy-shell io.github.sgatter.omatinuity sendScreenshot")`

## Update

```sh
omarchy plugin update io.github.sgatter.omatinuity
```

## Remove

```sh
omarchy plugin remove io.github.sgatter.omatinuity
rm -f ~/.config/systemd/user/tetherd.service.d/omarchy-panel.conf
systemctl --user daemon-reload && systemctl --user restart tetherd
```

The Bluetooth setup stays in place, since Tether itself relies on it. To undo it as well:

```sh
tether bt calls-enable off
rm ~/.config/wireplumber/wireplumber.conf.d/tether-no-hfp-hf.conf && systemctl --user restart wireplumber
sudo systemctl disable --now tether-btclass@hci0
sudo rm /etc/systemd/system/bluetooth.service.d/experimental.conf
sudo systemctl daemon-reload && sudo systemctl restart bluetooth
```

## Validate

```sh
omarchy plugin validate ~/.config/omarchy/plugins/io.github.sgatter.omatinuity
qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml Panel.qml
```
