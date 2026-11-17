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

bluetoothd=/usr/lib/bluetooth/bluetoothd
dropin=/etc/systemd/system/bluetooth.service.d/experimental.conf
roles_dir=$HOME/.config/wireplumber/wireplumber.conf.d
roles_file=$roles_dir/tether-no-hfp-hf.conf

say "This changes Bluetooth on this machine:"
cat <<EOF
  1. bluetoothd runs with --experimental, which switches on BlueZ's
     experimental interfaces for every Bluetooth device
     (sudo; writes $dropin; restarts Bluetooth)
  2. tether-btclass@hci0 presents the adapter to all devices as an
     A/V Hands-Free unit (sudo)
  3. $roles_file
     sets PipeWire's Bluetooth roles to Tether's list, which leaves out
     hfp_hf, for all Bluetooth audio devices (restarts WirePlumber)
  4. tetherd is restarted and its call control is turned on

A file that is already there is left alone, and the step that needs it
is skipped with a note. Afterwards Tether's own checklist
(tether --bt-setup) is printed; it changes nothing.
EOF
read -r -p "Continue? [y/N] " answer
[[ $answer == [yY]* ]] || { echo "Nothing changed."; exit 0; }

say "1/4  bluetoothd --experimental (LE bearer for notifications, Telephony for calls)"
if systemctl show bluetooth -p ExecStart --value | grep -q -- '--experimental'; then
  echo "     Already on."
elif [[ -e $dropin ]]; then
  echo "     $dropin is already there; left alone."
  echo "     Add --experimental to bluetoothd's ExecStart in it yourself."
elif [[ ! -x $bluetoothd ]]; then
  echo "     $bluetoothd is not there; Bluetooth left alone."
  echo "     Add --experimental to your bluetooth unit's ExecStart yourself."
else
  echo "     Restarts Bluetooth: connected headphones drop for a few seconds."
  sudo mkdir -p "${dropin%/*}"
  printf '[Service]\nExecStart=\nExecStart=%s --experimental\n' "$bluetoothd" |
    sudo tee "$dropin" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart bluetooth
fi

say "2/4  Present the adapter as A/V Hands-Free so iOS offers Messages and Contacts"
sudo systemctl enable --now tether-btclass@hci0

say "3/4  Keep PipeWire off the iPhone's hands-free link so Tether can control calls"
# Only one file may set bluez5.roles; a second one would silently win or lose.
others=$(grep -ls 'bluez5\.roles' "$roles_dir"/*.conf /etc/wireplumber/wireplumber.conf.d/*.conf 2>/dev/null |
  grep -vxF "$roles_file" || true)
if [[ -n $others ]]; then
  echo "     bluez5.roles is already set in these files; left alone:"
  printf '       %s\n' $others
  echo "     Take hfp_hf out of that list yourself, then restart WirePlumber."
elif [[ -e $roles_file ]]; then
  echo "     $roles_file is already there; left alone."
else
  mkdir -p "$roles_dir"
  cat > "$roles_file" <<'ROLES'
## Tether call control: without hfp_hf, a phone-initiated hands-free link goes
## to BlueZ's Telephony API instead of PipeWire. Call audio stays on the iPhone.
## This is the list from Tether's docs/BLUETOOTH.md. It applies to every
## Bluetooth audio device.
monitor.bluez.properties = {
  bluez5.roles = [ a2dp_sink a2dp_source bap_sink bap_source asha_sink hfp_ag ]
}
ROLES
  systemctl --user restart wireplumber
fi

say "4/4  Restart tetherd and turn on its call control"
systemctl --user restart tetherd
sleep 2
tether bt calls-enable on

say "Tether's own checklist:"
tether --bt-setup || true

cat <<'EOF'

Next: pair the iPhone over Bluetooth.
  1. tether bt devices              # find the iPhone's address (keep its Bluetooth screen open)
  2. tether bt pair <address>       # confirm the code on both sides
  3. On the iPhone: Settings > Bluetooth > (i) next to this laptop,
     turn on "Show Notifications" and "Sync Contacts". That gives this
     computer all of the iPhone's notifications and its contacts.
EOF
