#!/usr/bin/env bash
# One-time Bluetooth setup for Tether messages, notifications and calls.
# Every step changes how this machine behaves over Bluetooth, which is why
# Tether leaves them to you. Run it yourself: ./setup-bluetooth.sh
set -euo pipefail

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if (( EUID == 0 )); then
  echo "Run this as your own user, not as root: it asks for sudo where it needs it."
  exit 1
fi

say "This changes Bluetooth on this machine:"
cat <<'EOF'
  1. bluetoothd runs with --experimental (sudo; restarts Bluetooth)
  2. tether-btclass@hci0 presents the adapter as A/V Hands-Free (sudo)
  3. ~/.config/wireplumber/wireplumber.conf.d/tether-no-hfp-hf.conf keeps
     PipeWire off the iPhone's hands-free link (restarts WirePlumber)
  4. tetherd call control is turned on
EOF
read -r -p "Continue? [y/N] " answer
[[ $answer == [yY]* ]] || { echo "Nothing changed."; exit 0; }

say "1/4  bluetoothd --experimental (LE bearer for notifications, Telephony for calls)"
echo "     Restarts Bluetooth: connected headphones drop for a few seconds."
sudo mkdir -p /etc/systemd/system/bluetooth.service.d
printf '[Service]\nExecStart=\nExecStart=/usr/lib/bluetooth/bluetoothd --experimental\n' |
  sudo tee /etc/systemd/system/bluetooth.service.d/experimental.conf >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart bluetooth

say "2/4  Present the adapter as A/V Hands-Free so iOS offers Messages and Contacts"
sudo systemctl enable --now tether-btclass@hci0

say "3/4  Keep PipeWire off the iPhone's hands-free link so Tether can control calls"
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
cat > ~/.config/wireplumber/wireplumber.conf.d/tether-no-hfp-hf.conf <<'EOF'
## Tether call control: without hfp_hf, a phone-initiated hands-free link goes
## to BlueZ's Telephony API instead of PipeWire. Call audio stays on the iPhone.
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source bap_sink bap_source asha_sink hfp_ag ]
}
EOF
systemctl --user restart wireplumber

say "4/4  Turn on call control in tetherd"
systemctl --user restart tetherd
sleep 2
tether bt calls-enable on

say "Remaining checks:"
tether --bt-setup || true

cat <<'EOF'

Next: pair the iPhone over Bluetooth.
  1. tether bt devices              # find the iPhone's address (keep its Bluetooth screen open)
  2. tether bt pair <address>       # confirm the code on both sides
  3. On the iPhone: Settings > Bluetooth > (i) next to this laptop,
     turn on "Show Notifications" and "Sync Contacts".
EOF
